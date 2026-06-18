"""Unit tests for the escrow ↔ allocation reconciler (settlement safety net)."""
from __future__ import annotations

import asyncio
import sqlite3
import types

import pytest

from market_storefront.services.escrow_reconciler import (
    ACTIVE_HOLD_STATES,
    Drift,
    EscrowStatus,
    Finding,
    HeldAllocation,
    ReconcileReport,
    classify,
    exit_code_for,
    load_held_allocations,
    read_escrow_status,
    reconcile,
    remediate_stale_holds,
    report_to_dict,
)


def _alloc(uid: str = "0xesc", state: str = "leased") -> HeldAllocation:
    return HeldAllocation(allocation_id="a1", resource_id="r1", escrow_uid=uid, state=state)


class TestClassify:
    def test_gone_is_stale_hold(self):
        assert classify(_alloc(), EscrowStatus.GONE) is Drift.STALE_HOLD

    def test_active_is_ok(self):
        assert classify(_alloc(), EscrowStatus.ACTIVE) is Drift.OK

    def test_unreadable_is_unknown(self):
        assert classify(_alloc(), EscrowStatus.UNREADABLE) is Drift.UNKNOWN


class TestReconcile:
    def test_buckets_by_status(self):
        held = [_alloc("0xa"), _alloc("0xb"), _alloc("0xc")]
        status = {"0xa": EscrowStatus.ACTIVE, "0xb": EscrowStatus.GONE, "0xc": EscrowStatus.UNREADABLE}
        report = reconcile(held, lambda uid: status[uid])
        assert report.summary() == {"checked": 3, "stale_holds": 1, "unknown": 1, "ok": 1}
        assert report.stale_holds[0].allocation.escrow_uid == "0xb"

    def test_reader_exception_is_unknown_not_stale(self):
        # A transient RPC error must NEVER be misread as a stale hold (which
        # would release real, paid-for capacity).
        def boom(_uid: str) -> EscrowStatus:
            raise RuntimeError("rpc timeout")

        report = reconcile([_alloc("0xz")], boom)
        assert report.stale_holds == []
        assert len(report.unknown) == 1
        assert report.unknown[0].escrow_status is EscrowStatus.UNREADABLE

    def test_empty(self):
        assert reconcile([], lambda uid: EscrowStatus.ACTIVE).summary()["checked"] == 0


def _att(revocation_time=0, expiration_time=0):
    return types.SimpleNamespace(revocation_time=revocation_time, expiration_time=expiration_time)


def _status(decoded, now=1000):
    async def get_obligation_fn(_client, _uid):
        return decoded

    return asyncio.run(
        read_escrow_status(
            escrow_uid="0x1",
            alkahest_client=object(),
            get_obligation_fn=get_obligation_fn,
            now_unix=now,
        )
    )


class TestReadEscrowStatus:
    def test_active_when_live_and_unexpired(self):
        assert _status({"attestation": _att(expiration_time=2000)}, now=1000) is EscrowStatus.ACTIVE

    def test_gone_when_revoked(self):
        assert _status({"attestation": _att(revocation_time=900, expiration_time=2000)}) is EscrowStatus.GONE

    def test_gone_when_expired(self):
        assert _status({"attestation": _att(expiration_time=900)}, now=1000) is EscrowStatus.GONE

    def test_gone_when_missing(self):
        assert _status({"attestation": None}) is EscrowStatus.GONE
        assert _status(None) is EscrowStatus.GONE


class TestLoadHeldAllocations:
    def _db(self, tmp_path, rows):
        db = str(tmp_path / "sf.db")
        conn = sqlite3.connect(db)
        conn.execute(
            """CREATE TABLE compute_allocations (
                 allocation_id TEXT PRIMARY KEY, resource_id TEXT, escrow_uid TEXT,
                 state TEXT, lease_end_utc TEXT)"""
        )
        conn.executemany(
            "INSERT INTO compute_allocations VALUES (?,?,?,?,?)", rows
        )
        conn.commit()
        conn.close()
        return db

    def test_returns_only_held_with_escrow(self, tmp_path):
        db = self._db(
            tmp_path,
            [
                ("a1", "r1", "0xheld", "leased", None),       # held + escrow → yes
                ("a2", "r2", "0xres", "reserved", None),      # held + escrow → yes
                ("a3", "r3", "0xrel", "releasing", None),     # releasing (excluded) → no
                ("a4", "r4", "0xfree", "available", None),    # not held → no
                ("a5", "r5", None, "leased", None),           # held but no escrow → no
                ("a6", "r6", "", "leased", None),             # held but empty escrow → no
            ],
        )
        out = load_held_allocations(db)
        ids = {a.allocation_id for a in out}
        assert ids == {"a1", "a2"}

    def test_missing_table_returns_empty(self, tmp_path):
        db = str(tmp_path / "empty.db")
        sqlite3.connect(db).close()
        assert load_held_allocations(db) == []

    def test_active_hold_states_exclude_releasing(self):
        assert "releasing" not in ACTIVE_HOLD_STATES
        assert set(ACTIVE_HOLD_STATES) == {"reserved", "provisioning", "leased", "held"}


class TestReportSerialization:
    def _report(self):
        held = [_alloc("0xa", "leased"), _alloc("0xb", "reserved"), _alloc("0xc", "held")]
        status = {"0xa": EscrowStatus.ACTIVE, "0xb": EscrowStatus.GONE, "0xc": EscrowStatus.UNREADABLE}
        return reconcile(held, lambda uid: status[uid])

    def test_exit_code_3_when_stale(self):
        assert exit_code_for(self._report()) == 3

    def test_exit_code_0_when_clean(self):
        clean = reconcile([_alloc("0xa")], lambda uid: EscrowStatus.ACTIVE)
        assert exit_code_for(clean) == 0

    def test_report_to_dict_shape(self):
        d = report_to_dict(self._report())
        assert d["summary"] == {"checked": 3, "stale_holds": 1, "unknown": 1, "ok": 1}
        assert [f["escrow_uid"] for f in d["stale_holds"]] == ["0xb"]
        assert d["stale_holds"][0]["drift"] == "stale_hold"
        assert [f["escrow_uid"] for f in d["unknown"]] == ["0xc"]
        # JSON-serializable
        import json
        json.dumps(d)


class TestRemediateStaleHolds:
    def _report(self):
        held = [_alloc("0xstale", "leased"), _alloc("0xok", "leased"), _alloc("0xunk", "held")]
        status = {"0xstale": EscrowStatus.GONE, "0xok": EscrowStatus.ACTIVE, "0xunk": EscrowStatus.UNREADABLE}
        return reconcile(held, lambda uid: status[uid])

    def test_releases_only_stale_holds(self):
        released = []
        async def release_fn(alloc):
            released.append(alloc.escrow_uid)
        out = asyncio.run(remediate_stale_holds(self._report(), release_fn))
        # only the GONE escrow is released — never the ACTIVE or UNREADABLE ones
        assert released == ["0xstale"]
        assert out == [{"allocation_id": "a1", "resource_id": "r1", "released": True}]

    def test_release_failure_recorded_and_loop_continues(self):
        report = reconcile(
            [HeldAllocation("a1", "r1", "0xs1", "leased"), HeldAllocation("a2", "r2", "0xs2", "leased")],
            lambda uid: EscrowStatus.GONE,
        )
        calls = []
        async def release_fn(alloc):
            calls.append(alloc.allocation_id)
            if alloc.allocation_id == "a1":
                raise RuntimeError("transition rejected")
        out = asyncio.run(remediate_stale_holds(report, release_fn))
        assert calls == ["a1", "a2"]  # didn't abort after a1 failed
        assert out[0] == {"allocation_id": "a1", "resource_id": "r1", "released": False, "error": "transition rejected"}
        assert out[1]["released"] is True

    def test_no_stale_holds_is_noop(self):
        report = reconcile([_alloc("0xok")], lambda uid: EscrowStatus.ACTIVE)
        called = False
        async def release_fn(alloc):
            nonlocal called
            called = True
        out = asyncio.run(remediate_stale_holds(report, release_fn))
        assert out == []
        assert called is False


class TestReconcileDb:
    def _db(self, tmp_path, rows):
        db = str(tmp_path / "sf.db")
        conn = sqlite3.connect(db)
        conn.execute(
            """CREATE TABLE compute_allocations (
                 allocation_id TEXT PRIMARY KEY, resource_id TEXT, escrow_uid TEXT,
                 state TEXT, lease_end_utc TEXT)"""
        )
        conn.executemany("INSERT INTO compute_allocations VALUES (?,?,?,?,?)", rows)
        conn.commit()
        conn.close()
        return db

    def test_end_to_end_classifies_each_held_allocation(self, tmp_path):
        from market_storefront.services.escrow_reconciler import reconcile_db

        db = self._db(
            tmp_path,
            [
                ("a1", "r1", "0xactive", "leased", None),
                ("a2", "r2", "0xgone", "reserved", None),
                ("a3", "r3", "0xfree", "available", None),  # not held → ignored
            ],
        )
        # chain reader: 0xactive is live; 0xgone is expired.
        async def get_obligation_fn(_client, uid):
            if uid == "0xactive":
                return {"attestation": _att(expiration_time=9999)}
            return {"attestation": _att(expiration_time=1)}  # expired

        report = asyncio.run(
            reconcile_db(
                db_path=db,
                alkahest_client=object(),
                get_obligation_fn=get_obligation_fn,
                now_unix=1000,
            )
        )
        assert report.summary() == {"checked": 2, "stale_holds": 1, "unknown": 0, "ok": 1}
        assert report.stale_holds[0].allocation.allocation_id == "a2"

    def test_chain_read_error_is_unknown_not_stale(self, tmp_path):
        from market_storefront.services.escrow_reconciler import reconcile_db

        db = self._db(tmp_path, [("a1", "r1", "0xboom", "leased", None)])

        async def get_obligation_fn(_client, _uid):
            raise RuntimeError("rpc down")

        report = asyncio.run(
            reconcile_db(
                db_path=db, alkahest_client=object(), get_obligation_fn=get_obligation_fn
            )
        )
        assert report.stale_holds == []
        assert len(report.unknown) == 1
