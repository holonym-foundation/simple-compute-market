"""
Integration tests for POST /api/v1/hosts/{host}/containers/ (container tenants).

Exercises the HTTP API → job_service routing (provisioning_type=container) →
the REAL parse_playbook_result (container branch) round-trip, via the mock
Ansible boundary (the same fixture VM tests use; parse is delegated to a real
AnsibleService instance). The actual playbook + Docker are covered by the
container-management role E2E, not here.

Mirrors test_vms_api.py conventions.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest  # noqa: F401

from models.container_request_model import CreateContainerRequest
from services.ansible_service import AnsibleResult
from services.async_job_queue import AsyncJobQueue


HOST = "aex-native-scm"
CNAME = "aex-t-1"

CONTAINER_CREATE_STDOUT = """\
PLAY [Container Management Operations] ****************************************

TASK [debug] ******************************************************************
ok: [aex-native-scm] => {
    "container_creation_data": {
        "container_name": "aex-t-1",
        "container_id": "deadbeefcafe",
        "image": "nginx:alpine",
        "status": "running",
        "running": true,
        "host": "aex-native-scm",
        "home_volume": "aex-t-1-home",
        "lease_id": ""
    }
}
"""


def _make_event_seam(job_queue: AsyncJobQueue) -> asyncio.Event:
    """Inject an asyncio.Event that fires when the first job is dispatched."""
    dispatched = asyncio.Event()
    original = job_queue._on_job_started

    def _on_started(job_id: str) -> None:
        dispatched.set()
        if original is not None:
            original(job_id)

    job_queue._on_job_started = _on_started
    return dispatched


def _stdout(fake_ansible, stdout: str) -> None:
    """Make the mock playbook emit the given stdout (real parse runs on it)."""
    fake_ansible.wait_for_playbook = AsyncMock(
        return_value=AnsibleResult(stdout=stdout, stderr="", process_id=99999)
    )


class TestHttpValidation:
    async def test_create_missing_container_target_returns_422(self, client_and_queue):
        from httpx import ASGITransport, AsyncClient
        from main import app
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.post(
                f"/api/v1/hosts/{HOST}/containers/",
                json={"container_image": "nginx:alpine"},
            )
        assert resp.status_code == 422


class TestCreateContainerViaClient:
    async def test_create_returns_queued_job(self, client_and_queue, fake_ansible):
        client, job_queue = client_and_queue
        _stdout(fake_ansible, CONTAINER_CREATE_STDOUT)
        dispatched = _make_event_seam(job_queue)

        submit = await client.create_container(
            HOST, CreateContainerRequest(container_target=CNAME, container_image="nginx:alpine")
        )
        assert submit.status == "queued"
        assert len(submit.job_id) > 0
        await asyncio.wait_for(dispatched.wait(), timeout=5.0)

    async def test_create_job_succeeds_with_container_result(self, client_and_queue, fake_ansible):
        client, job_queue = client_and_queue
        _stdout(fake_ansible, CONTAINER_CREATE_STDOUT)
        dispatched = _make_event_seam(job_queue)

        submit = await client.create_container(
            HOST, CreateContainerRequest(container_target=CNAME, container_image="nginx:alpine")
        )
        await asyncio.wait_for(dispatched.wait(), timeout=5.0)
        final = await client.poll_until_complete(submit.job_id, timeout=5.0, poll_interval=0.05)

        assert final.status == "succeeded"
        assert final.result is not None
        # Proves the REAL container parse branch ran (only it extracts container_creation_data).
        assert final.result["container_name"] == CNAME
        assert final.result["running"] is True
        # Containers are outbound-only — no SSH connection string.
        assert final.result.get("ssh_port") in (None, "")

    async def test_create_calls_playbook_for_host(self, client_and_queue, fake_ansible):
        client, job_queue = client_and_queue
        _stdout(fake_ansible, CONTAINER_CREATE_STDOUT)
        dispatched = _make_event_seam(job_queue)

        await client.create_container(HOST, CreateContainerRequest(container_target=CNAME))
        await asyncio.wait_for(dispatched.wait(), timeout=5.0)
        await asyncio.sleep(0.1)

        fake_ansible.start_playbook.assert_called()
        assert fake_ansible.start_playbook.call_args.kwargs.get("limit") == HOST
