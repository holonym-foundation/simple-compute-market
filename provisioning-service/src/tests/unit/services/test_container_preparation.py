"""Real SQLite admission/worker boundaries; no Docker or network access."""
import asyncio
import copy
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import AnsibleJob, Base
from models.container_request_model import AdmitContainerRequest, CreateContainerRequest
from services.container_preparation import METADATA, admit
from services.job_service import AnsibleJobService


@pytest.fixture
def setup(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "jobs.db"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    settings = SimpleNamespace(storefront_admin_key="seller", container_admission_key="coordinator",
                               default_max_retries=3, default_vm_host="host",
                               frp_server_addr="", frp_domain="", frp_dashboard_password="")
    ansible = MagicMock()
    svc = AnsibleJobService(settings, factory, ansible)
    queue = SimpleNamespace(enqueue=AsyncMock())
    params = CreateContainerRequest(
        container_target="tenant-one", container_image="registry/runtime@sha256:" + "a" * 64,
        lease_id="escrow-one", container_env={"AEX_CAPABILITY_DIRECTORY": "/run/aex/capabilities",
                                            "AEX_ENV": "staging", "OTHER": "value"},
    ).to_ansible_job_params("host")
    yield svc, factory, settings, queue, params, ansible
    engine.dispose()


def admission(factory, job_id):
    with factory() as db:
        metadata = db.get(AnsibleJob, job_id).params[METADATA]
        return AdmitContainerRequest(request_digest=metadata["request_digest"], admission_id=uuid.uuid4())


async def test_prepare_is_durable_nonexecuting_and_idempotent(setup):
    svc, factory, settings, queue, params, ansible = setup
    first = await svc.submit(params, queue)
    restarted = AnsibleJobService(settings, factory, ansible)
    second = await restarted.submit(params, queue)
    assert first == second and first.status == "prepared"
    queue.enqueue.assert_not_awaited()
    # Even an accidentally delivered queue message must not execute preparation.
    await restarted._process_job(first.job_id)
    ansible.build_vars_file.assert_not_called()
    assert await restarted.requeue_due_retries(queue) == 0
    with factory() as db:
        assert db.query(AnsibleJob).count() == 1
        row = db.get(AnsibleJob, first.job_id)
        assert row.status == "prepared" and row.max_retries == 0
        assert row.escrow_uid == "escrow-one"


@pytest.mark.parametrize("field,value", [("vm_host", "other"), ("vm_target", "other"),
    ("container_image", "registry/runtime@sha256:" + "b" * 64),
    ("container_env", {"AEX_CAPABILITY_DIRECTORY": "/other", "AEX_ENV": "staging"})])
async def test_changed_request_cannot_rebind_escrow(setup, field, value):
    svc, _, _, queue, params, _ = setup
    await svc.submit(params, queue)
    changed = copy.deepcopy(params)
    setattr(changed, field, value)
    with pytest.raises(HTTPException) as exc:
        await svc.submit(changed, queue)
    assert exc.value.status_code == 409
    queue.enqueue.assert_not_awaited()


@pytest.mark.parametrize("admin,key,expected", [("", "", 401), ("seller", "", 401),
    ("seller", "wrong", 401), ("wrong", "coordinator", 401)])
async def test_admission_requires_both_credentials(setup, admin, key, expected):
    svc, factory, settings, queue, params, _ = setup
    job = await svc.submit(params, queue)
    with pytest.raises(HTTPException) as exc:
        await admit(factory, settings, queue, "host", job.job_id, admission(factory, job.job_id), admin, key)
    assert exc.value.status_code == expected
    queue.enqueue.assert_not_awaited()


@pytest.mark.parametrize("field,value", [("storefront_admin_key", ""),
    ("container_admission_key", ""), ("container_admission_key", "seller")])
async def test_unconfigured_or_shared_admission_key_fails_closed(setup, field, value):
    svc, factory, settings, queue, params, _ = setup
    job = await svc.submit(params, queue)
    setattr(settings, field, value)
    with pytest.raises(HTTPException) as exc:
        await admit(factory, settings, queue, "host", job.job_id, admission(factory, job.job_id), "seller", "coordinator")
    assert exc.value.status_code == 503


async def test_admit_exact_request_duplicate_and_lost_enqueue(setup):
    svc, factory, settings, queue, params, _ = setup
    job = await svc.submit(params, queue)
    body = admission(factory, job.job_id)
    queue.enqueue.side_effect = RuntimeError("lost enqueue")
    with pytest.raises(RuntimeError):
        await admit(factory, settings, queue, "host", job.job_id, body, "seller", "coordinator")
    with factory() as db:
        assert db.get(AnsibleJob, job.job_id).status == "queued"
    queue.enqueue.side_effect = None
    recovered = await admit(factory, settings, queue, "host", job.job_id, body, "seller", "coordinator")
    assert recovered.status == "queued" and recovered.job_id == job.job_id
    other = admission(factory, job.job_id)
    with pytest.raises(HTTPException) as exc:
        await admit(factory, settings, queue, "host", job.job_id, other, "seller", "coordinator")
    assert exc.value.status_code == 409


async def test_expiry_not_extended_and_cancelled_not_admitted(setup):
    svc, factory, settings, queue, params, _ = setup
    job = await svc.submit(params, queue)
    with factory() as db:
        row = db.get(AnsibleJob, job.job_id)
        row.params = {**row.params, METADATA: {**row.params[METADATA], "expires_at": 1}}
        db.commit()
    await svc.submit(params, queue)
    with pytest.raises(HTTPException) as exc:
        await admit(factory, settings, queue, "host", job.job_id, admission(factory, job.job_id), "seller", "coordinator")
    assert exc.value.status_code == 409
    assert svc.cancel_job(job.job_id)["status"] == "cancelled"
    await svc._process_job(job.job_id)
    queue.enqueue.assert_not_awaited()


@pytest.mark.parametrize("corruption", ["expired", "changed", "unadmitted"])
async def test_worker_rechecks_admission_before_ansible(setup, corruption):
    svc, factory, settings, queue, params, ansible = setup
    job = await svc.submit(params, queue)
    await admit(factory, settings, queue, "host", job.job_id, admission(factory, job.job_id), "seller", "coordinator")
    with factory() as db:
        row = db.get(AnsibleJob, job.job_id)
        raw = copy.deepcopy(row.params)
        if corruption == "expired":
            raw[METADATA]["expires_at"] = 1
        elif corruption == "changed":
            raw["container_env"]["OTHER"] = "changed"
        else:
            raw[METADATA]["admission_id"] = None
        row.params = raw
        db.commit()
    await svc._process_job(job.job_id)
    ansible.build_vars_file.assert_not_called()
    with factory() as db:
        assert db.get(AnsibleJob, job.job_id).status == "cancelled"


async def test_worker_claim_once_and_uncertain_outcome_never_retries(setup):
    svc, factory, settings, queue, params, ansible = setup
    job = await svc.submit(params, queue)
    body = admission(factory, job.job_id)
    await admit(factory, settings, queue, "host", job.job_id, body, "seller", "coordinator")
    # Failure after claim is deliberately uncertain. A duplicate dispatch must
    # not retry, and retrying the admission must only report that same outcome.
    ansible.build_vars_file.side_effect = RuntimeError("unknown boundary result")
    await asyncio.gather(svc._process_job(job.job_id), svc._process_job(job.job_id))
    ansible.build_vars_file.assert_called_once()
    with factory() as db:
        assert db.get(AnsibleJob, job.job_id).status == "uncertain"
    assert await svc.requeue_due_retries(queue) == 0
    queue.enqueue.reset_mock()
    result = await admit(factory, settings, queue, "host", job.job_id, body, "seller", "coordinator")
    assert result.status == "uncertain"
    queue.enqueue.assert_not_awaited()


async def test_concurrent_prepares_share_one_durable_identity(setup):
    svc, factory, _, queue, params, _ = setup
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    barrier = Barrier(2)

    def run():
        barrier.wait(timeout=5)
        return asyncio.run(svc.submit(copy.deepcopy(params), queue))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: run(), range(2)))
    assert first == second
    with factory() as db:
        assert db.query(AnsibleJob).count() == 1
    queue.enqueue.assert_not_awaited()


async def test_wrong_host_digest_and_preparation_metadata_removal_refused(setup):
    svc, factory, settings, queue, params, ansible = setup
    job = await svc.submit(params, queue)
    body = admission(factory, job.job_id)
    with pytest.raises(HTTPException) as exc:
        await admit(factory, settings, queue, "wrong-host", job.job_id, body, "seller", "coordinator")
    assert exc.value.status_code == 404
    bad = body.model_copy(update={"request_digest": "sha256:" + "0" * 64})
    with pytest.raises(HTTPException) as exc:
        await admit(factory, settings, queue, "host", job.job_id, bad, "seller", "coordinator")
    assert exc.value.status_code == 409
    with factory() as db:
        row = db.get(AnsibleJob, job.job_id)
        row.params = {k: v for k, v in row.params.items() if k != METADATA}
        row.status = "queued"
        db.commit()
    await svc._process_job(job.job_id)
    ansible.build_vars_file.assert_not_called()
