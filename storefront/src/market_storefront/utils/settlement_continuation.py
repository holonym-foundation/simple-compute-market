"""Observe one retained capability job; never authorize settlement or retry create.

Snapshots are private seller state, frozen before the first submission. A successful
provisioner report is NOT utility, admission, or chain receipt verification. Even
after success the escrow and allocation stay fenced pending a separately reviewed
settlement adapter. No signer, admission, provision, or resource-release API is
available here. Legacy rows without a snapshot are deliberately not reconstructed.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
import re
from contextlib import contextmanager


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def request_digest(params):
    return "sha256:" + hashlib.sha256(canonical({
        k: v for k, v in params.items() if k != "_container_preparation"
    }).encode()).hexdigest()


@contextmanager
def _connect(path):
    con = sqlite3.connect(path, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        with con:
            yield con
    finally:
        con.close()


def _table(con):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                       "AND name='capability_settlement_continuations'").fetchone()


def _bound_rows(con, snapshot):
    escrow = con.execute("SELECT * FROM escrows WHERE escrow_uid=?",
                         (snapshot["escrow_uid"],)).fetchone()
    allocations = con.execute("SELECT * FROM compute_allocations WHERE escrow_uid=?",
                              (snapshot["escrow_uid"],)).fetchall()
    if (not escrow or escrow["status"] != "provisioning"
            or escrow["fulfillment_uid"] is not None
            or escrow["provisioning_job_id"] != snapshot["job_id"]
            or escrow["negotiation_id"] != snapshot["negotiation_id"]
            or escrow["chain_name"] != snapshot["chain_name"]
            or escrow["escrow_address"] != snapshot["escrow_address"]
            or len(allocations) != 1):
        raise ValueError("continuation_binding_changed")
    allocation = allocations[0]
    if any(allocation[k] != snapshot[k] for k in
           ("allocation_id", "resource_id", "listing_id", "vm_host", "vm_target")):
        raise ValueError("continuation_allocation_changed")
    if allocation["state"] != "held":
        raise ValueError("continuation_allocation_not_held")
    return escrow


def freeze_continuation(db_path, *, escrow_uid, allocation_id, resource_id,
                        listing_id, order, demand_hex, duration_seconds, params,
                        now=None):
    """Commit exact original terms and job identity BEFORE create can be sent.

    The finite observation window starts here and is never renewed by polling.
    This conservative deadline is not a new on-chain or delivered-lease promise.
    """
    now = int(time.time()) if now is None else now
    env = params.get("container_env") if isinstance(params, dict) else None
    # Match the capability prepared-host boundary. This is not a general secret
    # detector: the private snapshot is still protected configuration state.
    secret_keys = ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "AEX_INFERENCE_API_KEY", "AEX_INGEST_KEY",
                   "INGEST_API_KEY", "TAP_API_KEY", "TAP_APP_KEY", "WAAP_PRIVATE_KEY")
    if not isinstance(env, dict) or any(env.get(k) for k in secret_keys):
        raise ValueError("capability_static_credentials_refused")
    if (type(now) is not int or type(duration_seconds) is not int
            or not 0 < duration_seconds <= 3600 or not isinstance(order, dict)
            or not isinstance(params, dict) or params.get("lease_id") != escrow_uid
            or params.get("provisioning_type") != "container"
            or params.get("vm_action") != "create" or params.get("max_retries") != 0
            or "AEX_CAPABILITY_DIRECTORY" not in (params.get("container_env") or {})):
        raise ValueError("invalid_continuation")
    job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "scm-container-lease:" + escrow_uid))
    if params.get("vm_target") != "tenant-" + uuid.UUID(job_id).hex:
        raise ValueError("invalid_continuation_target")
    with _connect(db_path) as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute("""CREATE TABLE IF NOT EXISTS capability_settlement_continuations (
            escrow_uid TEXT PRIMARY KEY, snapshot TEXT NOT NULL,
            state TEXT NOT NULL, observation TEXT)""")
        if con.execute("SELECT 1 FROM capability_settlement_continuations WHERE escrow_uid=?",
                       (escrow_uid,)).fetchone():
            raise ValueError("continuation_already_frozen")
        escrow = con.execute("SELECT * FROM escrows WHERE escrow_uid=?", (escrow_uid,)).fetchone()
        allocations = con.execute("SELECT * FROM compute_allocations WHERE escrow_uid=?",
                                  (escrow_uid,)).fetchall()
        if (not escrow or escrow["status"] != "provisioning"
                or escrow["fulfillment_uid"] is not None or escrow["provisioning_job_id"] is not None
                or len(allocations) != 1 or allocations[0]["allocation_id"] != allocation_id
                or allocations[0]["resource_id"] != resource_id
                or allocations[0]["listing_id"] != listing_id
                or allocations[0]["state"] != "reserved"):
            raise ValueError("invalid_continuation_rows")
        snapshot = dict(schema=1, escrow_uid=escrow_uid, job_id=job_id,
                        negotiation_id=escrow["negotiation_id"], chain_name=escrow["chain_name"],
                        escrow_address=escrow["escrow_address"], allocation_id=allocation_id,
                        resource_id=resource_id, listing_id=listing_id, order=order,
                        demand_hex=demand_hex, duration_seconds=duration_seconds,
                        started_at=now, deadline=now + duration_seconds,
                        vm_host=params["vm_host"], vm_target=params["vm_target"],
                        params=params, request_digest=request_digest(params))
        frozen = canonical(snapshot)  # reject non-JSON inputs before mutations
        con.execute("INSERT INTO capability_settlement_continuations VALUES (?,?,'waiting',NULL)",
                    (escrow_uid, frozen))
        con.execute("UPDATE escrows SET provisioning_job_id=? WHERE escrow_uid=?", (job_id, escrow_uid))
        con.execute("UPDATE compute_allocations SET state='held',vm_host=?,vm_target=? WHERE allocation_id=?",
                    (params["vm_host"], params["vm_target"], allocation_id))
    return job_id


def load_continuation(db_path, escrow_uid):
    with _connect(db_path) as con:
        if not _table(con):
            return None
        row = con.execute("SELECT * FROM capability_settlement_continuations WHERE escrow_uid=?",
                          (escrow_uid,)).fetchone()
        return dict(row) if row else None


def _observe(snapshot, job, now):
    if now >= snapshot["deadline"]:
        return "expired", None
    params = job.get("params") or {}
    metadata = params.get("_container_preparation") or {}
    if (set(job) != {"job_id", "status", "params", "result", "error", "retry_count", "max_retries", "next_retry_at", "escrow_uid"}
            or set(metadata) != {"version", "request_digest", "expires_at", "admission_id"}
            or job.get("job_id") != snapshot["job_id"] or job.get("escrow_uid") != snapshot["escrow_uid"]
            or request_digest(params) != snapshot["request_digest"]
            or type(metadata.get("version")) is not int or metadata["version"] != 1
            or type(metadata.get("expires_at")) is not int
            or metadata["expires_at"] <= snapshot["started_at"]
            or metadata.get("request_digest") != snapshot["request_digest"]
            or type(job.get("retry_count")) is not int or job["retry_count"] != 0
            or type(job.get("max_retries")) is not int or job["max_retries"] != 0
            or job.get("next_retry_at") is not None or job.get("error") is not None):
        raise ValueError("invalid_continuation_job")
    if job.get("status") in ("prepared", "queued", "running"):
        if job.get("result") is not None:
            raise ValueError("premature_continuation_result")
        if job["status"] == "prepared" and metadata["admission_id"] is not None:
            raise ValueError("premature_continuation_admission")
        return "waiting", None
    if job.get("status") != "succeeded":
        return "uncertain", None
    admission = metadata.get("admission_id")
    try:
        if str(uuid.UUID(admission)) != admission or uuid.UUID(admission).version != 4:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError("continuation_admission_missing") from None
    result = job.get("result")
    if (not isinstance(result, dict) or result.get("container_name") != snapshot["vm_target"]
            or result.get("running") is not True or not isinstance(result.get("container_id"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", result["container_id"])
            or result.get("authentication") is not None):
        raise ValueError("invalid_continuation_result")
    # A private digest, not a connection/credential publication or readiness proof.
    return "provisioned_pending_settlement", {
        "job_id": snapshot["job_id"], "request_digest": snapshot["request_digest"],
        "admission_id": admission, "result_sha256": hashlib.sha256(canonical(result).encode()).hexdigest(),
        "observed_at": now, "settlement_verified": False,
    }


async def reconcile_continuation(db_path, escrow_uid, *, get_job, now=None):
    """Read only the saved job and persist observation; concurrent polls are safe.

    get_job is the authenticated provisioner GET adapter, never a submitter.
    Source wiring calls this from existing POST/status orchestration. No legacy
    row is retrofitted and no successful observation dispatches completion work.
    """
    retained = load_continuation(db_path, escrow_uid)
    if retained is None:
        return None
    snapshot = json.loads(retained["snapshot"])
    with _connect(db_path) as con:
        _bound_rows(con, snapshot)
    clock = lambda: int(time.time()) if now is None else now
    if retained["state"] != "waiting":
        return retained["state"]
    if clock() >= snapshot["deadline"]:
        state, observation = "expired", None
    else:
        try:
            job = await get_job(snapshot["job_id"])
            if hasattr(job, "model_dump"):
                job = job.model_dump(mode="json")
            state, observation = _observe(snapshot, job, clock())
        except Exception:
            # A missing response is not failure/absence and never permits retry create.
            return "waiting"
    with _connect(db_path) as con:
        con.execute("BEGIN IMMEDIATE")
        _bound_rows(con, snapshot)
        current = con.execute("SELECT * FROM capability_settlement_continuations WHERE escrow_uid=?",
                              (escrow_uid,)).fetchone()
        if current["snapshot"] != retained["snapshot"]:
            raise ValueError("continuation_snapshot_changed")
        if current["state"] != "waiting":
            return current["state"]
        con.execute("UPDATE capability_settlement_continuations SET state=?,observation=? WHERE escrow_uid=?",
                    (state, canonical(observation) if observation else None, escrow_uid))
        if state != "waiting":
            con.execute("UPDATE escrows SET reason=? WHERE escrow_uid=? AND status='provisioning'",
                        ("capability_" + state + ": capacity held; settlement not verified", escrow_uid))
    return state
