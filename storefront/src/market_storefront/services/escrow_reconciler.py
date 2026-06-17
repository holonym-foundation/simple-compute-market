"""Escrow ↔ allocation reconciliation (settlement safety net).

The lease watchdog (provisioning-service) releases capacity on TIME —
``lease_end_utc < now``. But an escrow can disappear on-chain EARLY: the buyer
reclaims it, or it gets revoked / expires before ``lease_end``. When that
happens the storefront allocation is still "held", so the resource stays
reserved with no paying escrow behind it — a **stale hold** (silently lost
capacity). This reconciler cross-checks each held allocation's escrow against
the chain and reports (and optionally releases) stale holds.

Design: the pure core (``classify`` + ``reconcile``) takes an injected
escrow-state reader, so it is chain- and DB-agnostic and fully unit-testable.
``read_escrow_status`` (chain adapter, reusing the escrow codec) and
``load_held_allocations`` (DB reader) wire it to the live storefront; the CLI
``escrow reconcile`` command drives it.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable

# Allocation states that mean "capacity is held for a buyer". 'releasing' is
# excluded — the watchdog is already tearing those down, so they aren't drift.
ACTIVE_HOLD_STATES = ("reserved", "provisioning", "leased", "held")


class EscrowStatus(str, Enum):
    ACTIVE = "active"          # on-chain, not revoked, not expired
    GONE = "gone"             # revoked, expired, or no longer readable on-chain
    UNREADABLE = "unreadable"  # chain read failed — inconclusive, retry next cycle


class Drift(str, Enum):
    OK = "ok"                 # held + escrow ACTIVE → nothing to do
    STALE_HOLD = "stale_hold"  # held + escrow GONE → release the resource
    UNKNOWN = "unknown"       # escrow UNREADABLE → recheck next cycle, don't act


@dataclass(frozen=True)
class HeldAllocation:
    allocation_id: str
    resource_id: str
    escrow_uid: str
    state: str
    lease_end_utc: str | None = None


@dataclass(frozen=True)
class Finding:
    allocation: HeldAllocation
    escrow_status: EscrowStatus
    drift: Drift


@dataclass(frozen=True)
class ReconcileReport:
    findings: tuple[Finding, ...]

    @property
    def stale_holds(self) -> list[Finding]:
        return [f for f in self.findings if f.drift is Drift.STALE_HOLD]

    @property
    def unknown(self) -> list[Finding]:
        return [f for f in self.findings if f.drift is Drift.UNKNOWN]

    @property
    def ok(self) -> list[Finding]:
        return [f for f in self.findings if f.drift is Drift.OK]

    def summary(self) -> dict[str, int]:
        return {
            "checked": len(self.findings),
            "stale_holds": len(self.stale_holds),
            "unknown": len(self.unknown),
            "ok": len(self.ok),
        }


def _finding_dict(f: "Finding") -> dict[str, Any]:
    return {
        "allocation_id": f.allocation.allocation_id,
        "resource_id": f.allocation.resource_id,
        "escrow_uid": f.allocation.escrow_uid,
        "state": f.allocation.state,
        "escrow_status": f.escrow_status.value,
        "drift": f.drift.value,
    }


def report_to_dict(report: "ReconcileReport") -> dict[str, Any]:
    """JSON-serializable view of a report (the CLI's --json contract)."""
    return {
        "summary": report.summary(),
        "stale_holds": [_finding_dict(f) for f in report.stale_holds],
        "unknown": [_finding_dict(f) for f in report.unknown],
    }


def exit_code_for(report: "ReconcileReport") -> int:
    """0 = clean; 3 = stale holds found (monitoring-friendly non-zero)."""
    return 3 if report.stale_holds else 0


def classify(allocation: HeldAllocation, escrow_status: EscrowStatus) -> Drift:
    """Map (held allocation, on-chain escrow status) → drift classification."""
    if escrow_status is EscrowStatus.GONE:
        return Drift.STALE_HOLD
    if escrow_status is EscrowStatus.UNREADABLE:
        return Drift.UNKNOWN
    return Drift.OK


def reconcile(
    held: Iterable[HeldAllocation],
    read_status: Callable[[str], EscrowStatus],
) -> ReconcileReport:
    """Classify every held allocation against its on-chain escrow.

    ``read_status(escrow_uid) -> EscrowStatus`` is injected so this is pure +
    testable. A read that raises is treated as UNREADABLE (inconclusive — never
    misclassify a transient RPC error as a stale hold and release real capacity).
    """
    findings: list[Finding] = []
    for alloc in held:
        try:
            status = read_status(alloc.escrow_uid)
        except Exception:
            status = EscrowStatus.UNREADABLE
        findings.append(Finding(alloc, status, classify(alloc, status)))
    return ReconcileReport(tuple(findings))


# --------------------------------------------------------------------------
# DB reader — held allocations with an escrow uid, from the storefront SQLite.
# --------------------------------------------------------------------------
def load_held_allocations(db_path: str) -> list[HeldAllocation]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro&nolock=1", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='compute_allocations'"
        ).fetchone()
        if not exists:
            return []
        placeholders = ", ".join("?" for _ in ACTIVE_HOLD_STATES)
        rows = conn.execute(
            f"""
            SELECT allocation_id, resource_id, escrow_uid, state, lease_end_utc
            FROM compute_allocations
            WHERE state IN ({placeholders})
              AND escrow_uid IS NOT NULL AND escrow_uid != ''
            """,
            ACTIVE_HOLD_STATES,
        ).fetchall()
    finally:
        conn.close()
    return [
        HeldAllocation(
            allocation_id=str(r["allocation_id"]),
            resource_id=str(r["resource_id"]),
            escrow_uid=str(r["escrow_uid"]),
            state=str(r["state"]),
            lease_end_utc=r["lease_end_utc"],
        )
        for r in rows
    ]


# --------------------------------------------------------------------------
# Chain adapter — escrow uid → EscrowStatus via the escrow codec's get_obligation.
# Mirrors escrow_verification's attestation-envelope checks (revoked / expired).
# get_obligation_fn is injectable (test seam); a missing/unreadable escrow → GONE
# vs UNREADABLE is decided by the caller's exception shape.
# --------------------------------------------------------------------------
async def read_escrow_status(
    *,
    escrow_uid: str,
    alkahest_client: Any,
    get_obligation_fn: Callable[[Any, str], Any],
    now_unix: int | None = None,
) -> EscrowStatus:
    """Read an escrow's on-chain attestation and reduce it to ACTIVE / GONE.

    GONE = revoked, expired, or the attestation can't be found on-chain.
    Raises only on inconclusive RPC errors — the caller maps that to UNREADABLE.
    """
    now = int(now_unix) if now_unix is not None else int(time.time())
    decoded = await get_obligation_fn(alkahest_client, escrow_uid)
    if not decoded or decoded.get("attestation") is None:
        return EscrowStatus.GONE
    att = decoded["attestation"]
    if getattr(att, "revocation_time", None):
        return EscrowStatus.GONE
    exp = getattr(att, "expiration_time", None)
    if exp and int(exp) <= now:
        return EscrowStatus.GONE
    return EscrowStatus.ACTIVE


# --------------------------------------------------------------------------
# Orchestrator — load held allocations from the DB, read each escrow on-chain,
# return the drift report. Report-only (no remediation) by design: releasing
# capacity is a side-effect the operator/CLI opts into after reviewing.
# A per-escrow chain-read failure → UNREADABLE (never a false stale-hold).
# --------------------------------------------------------------------------
async def reconcile_db(
    *,
    db_path: str,
    alkahest_client: Any,
    get_obligation_fn: Callable[[Any, str], Any],
    now_unix: int | None = None,
) -> ReconcileReport:
    held = load_held_allocations(db_path)
    findings: list[Finding] = []
    for alloc in held:
        try:
            status = await read_escrow_status(
                escrow_uid=alloc.escrow_uid,
                alkahest_client=alkahest_client,
                get_obligation_fn=get_obligation_fn,
                now_unix=now_unix,
            )
        except Exception:
            status = EscrowStatus.UNREADABLE
        findings.append(Finding(alloc, status, classify(alloc, status)))
    return ReconcileReport(tuple(findings))
