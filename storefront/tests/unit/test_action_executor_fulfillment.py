from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

import pytest

from client import provisioning_client
from market_storefront.utils import action_executor
from market_storefront.services.compute_listing_reconciler import record_derived_listing
from market_storefront.utils.sqlite_client import SQLiteClient


@pytest.fixture
def client(tmp_path):
    return SQLiteClient(db_path=str(tmp_path / "agent.db"))


async def _seed_compute_pool(client: SQLiteClient) -> None:
    await client.upsert_resource(
        resource_id="pool-h200-1",
        resource_type="compute.gpu",
        resource_subtype="h200",
        unit="count",
        value=1,
        state="available",
        attributes={
            "gpu_model": "H200",
            "region": "California, US",
            "vm_host": "host-1",
        },
    )


async def _seed_compute_listings(client: SQLiteClient, *, max_gpu_count: int) -> None:
    for gpu_count in range(1, max_gpu_count + 1):
        listing_id = f"listing-{gpu_count}x"
        await client.upsert_listing(
            listing_id=listing_id,
            status="open",
            created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00",
            offer_resource={
                "resource_id": "pool-h200-1",
                "gpu_model": "H200",
                "gpu_count": gpu_count,
                "region": "California, US",
                "sla": 99.0,
            },
            accepted_escrows=_compute_listing(gpu_count=gpu_count)["accepted_escrows"],
            demands=[],
            fulfillment_resource=None,
            max_duration_seconds=3600,
            seller="http://seller",
        )
        record_derived_listing(
            client.db_path,
            listing_id=listing_id,
            resource_id="pool-h200-1",
            gpu_count=gpu_count,
        )


def _compute_listing(*, gpu_count: int = 1) -> dict:
    return {
        "listing_id": f"listing-{gpu_count}x",
        "offer_resource": {
            "resource_id": "pool-h200-1",
            "gpu_model": "H200",
            "gpu_count": gpu_count,
            "region": "California, US",
            "sla": 99.0,
        },
        "accepted_escrows": [
            {
                "chain_name": "anvil",
                "escrow_address": "0x" + "11" * 20,
                "literal_fields": {
                    "token": "0x" + "22" * 20,
                },
                "rates": [{"amount": 100}],
            }
        ],
    }


@pytest.mark.asyncio
async def test_fulfill_compute_obligation_reports_error_when_onchain_fulfillment_fails(
    client,
    monkeypatch,
):
    class FakeProvisioningClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def register_lease(self, **kwargs):
            return {"id": "lease-1", **kwargs}

    await _seed_compute_pool(client)
    monkeypatch.setattr(action_executor, "get_sqlite_client", lambda: client)
    monkeypatch.setattr(
        provisioning_client,
        "ProvisioningClient",
        FakeProvisioningClient,
    )
    monkeypatch.setattr(
        action_executor,
        "_do_provision",
        AsyncMock(return_value={"ssh": "ssh tenant@example"}),
    )
    monkeypatch.setattr(action_executor, "_do_shutdown", AsyncMock())

    alkahest = MagicMock()
    alkahest.string_obligation.do_obligation = AsyncMock(
        side_effect=RuntimeError("contract reverted")
    )
    alkahest.oracle.request_arbitration = AsyncMock()

    result = await action_executor.fulfill_compute_obligation(
        client=alkahest,
        escrow_uid="escrow-1",
        ssh_public_key="ssh-ed25519 AAAA",
        oracle_address="0x" + "33" * 20,
        order=_compute_listing(),
        duration_seconds=3600,
        listing_id="listing-1",
    )

    assert result["status"] == "error"
    assert action_executor._do_provision.await_args.kwargs["lease_id"] == "escrow-1"
    assert "contract reverted" in result["message"]
    assert result["connection_details"] is None
    alkahest.oracle.request_arbitration.assert_not_called()

    selected = await client.select_available_compute_vm(
        required_attributes={"resource_id": "pool-h200-1", "gpu_count": 1},
    )
    assert selected is None
    resource = await client.get_resource(resource_id="pool-h200-1")
    assert resource is not None
    assert resource["state"] == "leased"


@pytest.mark.asyncio
async def test_reservation_closes_oversized_dynamic_listings(client, monkeypatch):
    class FakeProvisioningClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def register_lease(self, **kwargs):
            return {"id": "lease-1", **kwargs}

    await _seed_compute_pool(client)
    await client.upsert_resource(
        resource_id="pool-h200-1",
        resource_type="compute.gpu",
        resource_subtype="h200",
        unit="count",
        value=4,
        state="available",
        attributes={
            "gpu_model": "H200",
            "region": "California, US",
            "vm_host": "host-1",
        },
    )
    await _seed_compute_listings(client, max_gpu_count=4)
    monkeypatch.setattr(action_executor, "get_sqlite_client", lambda: client)
    monkeypatch.setattr(
        provisioning_client,
        "ProvisioningClient",
        FakeProvisioningClient,
    )
    monkeypatch.setattr(
        action_executor,
        "_do_provision",
        AsyncMock(return_value={"ssh": "ssh tenant@example"}),
    )
    monkeypatch.setattr(action_executor, "_do_shutdown", AsyncMock())

    result = await action_executor.fulfill_compute_obligation(
        client=None,
        escrow_uid="escrow-2x",
        ssh_public_key="ssh-ed25519 AAAA",
        order=_compute_listing(gpu_count=2),
        duration_seconds=3600,
        listing_id="listing-2x",
    )

    assert result["status"] == "fulfilled"
    assert action_executor._do_provision.await_args.kwargs["lease_id"] == "escrow-2x"
    statuses = {
        gpu_count: (await client.load_listing(listing_id=f"listing-{gpu_count}x"))[
            "status"
        ]
        for gpu_count in range(1, 5)
    }
    assert statuses == {
        1: "open",
        2: "open",
        3: "closed",
        4: "closed",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("lease_id", ["0x" + "ab" * 32, None])
async def test_container_provisioning_preserves_settled_lease_binding(monkeypatch, lease_id):
    transport = MagicMock()
    transport.__aenter__ = AsyncMock(return_value=transport)
    transport.__aexit__ = AsyncMock(return_value=None)
    transport.create_container = AsyncMock(return_value=SimpleNamespace(job_id="job-1"))
    transport.poll_until_complete = AsyncMock(
        return_value=SimpleNamespace(result={"container_name": "tenant-test", "lease_id": lease_id})
    )
    factory = MagicMock(return_value=transport)
    monkeypatch.setattr(action_executor, "ProvisioningClient", factory)
    # A buyer's environment cannot select the provisioning request's lease ID.
    env = {"AEX_AGENT_ID": "test-agent", "lease_id": "buyer-controlled"}
    submitted = AsyncMock()
    result = await action_executor._do_provision(
        "", vm_host="host-1", vm_target="tenant-test", virtualization_type="container",
        container_image="registry.example/runtime@sha256:" + "a" * 64,
        container_env=env, lease_id=lease_id, on_job_submitted=submitted,
    )
    host, request = transport.create_container.await_args.args
    assert host == "host-1"
    assert request.lease_id == lease_id
    assert request.container_env == env
    assert request.container_target == "tenant-test"
    params = request.to_ansible_job_params(host)
    assert params.lease_id == lease_id
    assert params.container_env == env
    assert result["lease_id"] == lease_id
    submitted.assert_awaited_once_with("job-1")
    transport.create_container.assert_awaited_once()


@pytest.mark.asyncio
async def test_capability_timeout_keeps_real_allocation_occupied(client, monkeypatch):
    await _seed_compute_pool(client)
    monkeypatch.setattr(action_executor, "get_sqlite_client", lambda: client)
    provision = AsyncMock(side_effect=TimeoutError("reply lost after create request"))
    monkeypatch.setattr(action_executor, "_do_provision", provision)
    alkahest = MagicMock()
    result = await action_executor.fulfill_compute_obligation(
        client=alkahest, escrow_uid="escrow-timeout", ssh_public_key="",
        oracle_address="0x" + "33" * 20, order=_compute_listing(), duration_seconds=3600,
        listing_id="listing-1", container_env={"AEX_CAPABILITY_DIRECTORY": "/run/aex/capabilities"},
    )
    assert result["status"] == "uncertain"
    selected = await client.select_available_compute_vm(
        required_attributes={"resource_id": "pool-h200-1", "gpu_count": 1})
    assert selected is None
    import sqlite3
    import uuid
    with sqlite3.connect(client.db_path) as db:
        state, target = db.execute(
            "SELECT state, vm_target FROM compute_allocations WHERE escrow_uid = ?",
            ("escrow-timeout",),
        ).fetchone()
    assert state == "held"
    assert target == "tenant-" + uuid.uuid5(uuid.NAMESPACE_URL, "scm-container-lease:escrow-timeout").hex
    alkahest.string_obligation.do_obligation.assert_not_called()
