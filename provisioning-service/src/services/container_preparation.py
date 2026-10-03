"""Durable, non-executing preparation for capability-enabled containers.

The admission key belongs to the outside-tenant enrollment coordinator, not
the seller or app. A digest match is not enrollment evidence: the coordinator
must verify primary authority and the protected host mount before admission.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import secrets
import time
import uuid

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from db.models import AnsibleJob, JobStatus
from models.jobs_model import JobSubmitResponse

METADATA = "_container_preparation"


def requires_preparation(params):
    return (params.provisioning_type == "container" and params.vm_action == "create"
            and isinstance(params.container_env, dict)
            and "AEX_CAPABILITY_DIRECTORY" in params.container_env)


def digest_request(raw):
    request = {key: value for key, value in raw.items() if key != METADATA}
    return "sha256:" + hashlib.sha256(json.dumps(
        request, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def prepare(session_factory, params):
    """Create once per escrow; any changed request conflicts, including host."""
    env = params.container_env
    if (not requires_preparation(params) or not params.lease_id
            or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", params.vm_target or "")
            or not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", params.container_image or "")
            or env.get("AEX_ENV") != "staging"
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())):
        raise HTTPException(422, "Invalid immutable staging container preparation")
    raw = dataclasses.asdict(params)
    # No automated retry after a potentially effective remote action.
    raw["max_retries"] = 0
    request_digest = digest_request(raw)
    job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "scm-container-lease:" + params.lease_id))
    metadata = {"version": 1, "request_digest": request_digest,
                "expires_at": int(time.time()) + 3600, "admission_id": None}
    with session_factory() as db:
        existing = db.get(AnsibleJob, job_id)
        if existing is None:
            db.add(AnsibleJob(id=job_id, status="prepared", params={**raw, METADATA: metadata},
                             escrow_uid=params.lease_id, max_retries=0, retry_count=0))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()  # another identical preparation won the unique job id
            existing = db.get(AnsibleJob, job_id)
        stored = (existing.params or {}).get(METADATA, {}) if existing else {}
        if (stored.get("request_digest") != request_digest
                or digest_request(existing.params) != request_digest):
            raise HTTPException(409, "Escrow already bound to a different container request")
        # Retries never renew the window, generate a new identity, or enqueue.
        return JobSubmitResponse(job_id=job_id, status=existing.status)


async def admit(session_factory, settings, job_queue, host, job_id, body, admin_key, admission_key):
    configured_admin = getattr(settings, "storefront_admin_key", "")
    configured_admission = getattr(settings, "container_admission_key", "")
    if (not isinstance(configured_admin, str) or not configured_admin
            or not isinstance(configured_admission, str) or not configured_admission
            or secrets.compare_digest(configured_admin, configured_admission)):
        raise HTTPException(503, "Container admission is not configured")
    if (not secrets.compare_digest(admin_key, configured_admin)
            or not secrets.compare_digest(admission_key, configured_admission)):
        raise HTTPException(401, "Missing or invalid admission credentials")
    with session_factory() as db:
        job = db.get(AnsibleJob, job_id)
        if job is None or job.params.get("vm_host") != host:
            raise HTTPException(404, "Prepared container not found")
        metadata = job.params.get(METADATA, {})
        if (metadata.get("request_digest") != body.request_digest
                or digest_request(job.params) != body.request_digest):
            raise HTTPException(409, "Prepared request digest mismatch")
        if metadata.get("admission_id") not in (None, str(body.admission_id)):
            raise HTTPException(409, "Container already bound to another admission")
        if job.status == "prepared":
            if metadata.get("expires_at", 0) <= time.time():
                raise HTTPException(409, "Container preparation expired")
            raw = {**job.params, METADATA: {**metadata, "admission_id": str(body.admission_id)}}
            changed = db.query(AnsibleJob).filter(
                AnsibleJob.id == job_id, AnsibleJob.status == "prepared",
            ).update({"status": "queued", "params": raw}, synchronize_session=False)
            db.commit()
            db.expire_all()
            job = db.get(AnsibleJob, job_id)
            if not changed and job.params[METADATA].get("admission_id") != str(body.admission_id):
                raise HTTPException(409, "Concurrent admission conflict")
        elif metadata.get("admission_id") is None:
            raise HTTPException(409, "Container preparation is not admissible")
        current_status = job.status
    # Retrying the SAME admission repairs a lost enqueue after COMMIT. Atomic
    # queued->running claim in the worker makes duplicate queue entries harmless.
    if current_status == JobStatus.queued.value:
        await job_queue.enqueue(job_id)
    return JobSubmitResponse(job_id=job_id, status=current_status)
