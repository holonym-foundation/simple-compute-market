"""Fail-closed tests for the inert storefront activation boundary."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

import market_storefront.agent as agent
import market_storefront.container as container
import market_storefront.server as server
import market_storefront.utils.config as storefront_config
from market_storefront.services.system_service import SystemService
from market_storefront.utils.config import validate_activation_mode
from market_storefront.utils.sqlite_client import SQLiteClient


def test_activation_mode_validation_is_explicit_and_fail_closed():
    assert validate_activation_mode("ACTIVE") == "active"
    assert validate_activation_mode(" inert ") == "inert"
    for value in (None, "", "paused", "observe", True):
        with pytest.raises(ValueError, match="activation_mode"):
            validate_activation_mode(value)


def test_inert_mode_is_permanently_paused(monkeypatch):
    monkeypatch.setattr(storefront_config, "ACTIVATION_MODE", "inert")
    monkeypatch.setattr(server, "_GLOBALLY_PAUSED", False)

    assert server.is_inert_mode() is True
    assert server.is_globally_paused() is True
    with pytest.raises(RuntimeError, match="cannot be resumed"):
        server._set_globally_paused(False)
    assert server.is_globally_paused() is True


@pytest.mark.asyncio
async def test_inert_http_boundary_denies_actions_before_handlers(monkeypatch):
    monkeypatch.setattr(storefront_config, "ACTIVATION_MODE", "inert")
    called = False
    app = FastAPI()
    app.middleware("http")(server.enforce_activation_mode)

    @app.post("/api/v1/settle/{escrow_uid}")
    async def settle(escrow_uid: str):
        nonlocal called
        called = True
        return {"escrow_uid": escrow_uid}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/settle/escrow-1", json={})

    assert response.status_code == 503
    assert response.json() == {
        "error": "storefront_inert",
        "detail": (
            "This storefront is running in inert observation mode; "
            "seller and buyer actions are disabled."
        ),
        "activation_mode": "inert",
    }
    assert response.headers["cache-control"] == "no-store"
    assert called is False


@pytest.mark.asyncio
async def test_inert_http_boundary_allows_only_exact_observation_paths(monkeypatch):
    monkeypatch.setattr(storefront_config, "ACTIVATION_MODE", "inert")
    app = FastAPI()
    app.middleware("http")(server.enforce_activation_mode)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/v1/listings")
    async def listings():
        return {"listings": []}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health_response = await client.get("/health")
        listings_response = await client.get("/api/v1/listings")
        docs_response = await client.get("/openapi.json")

    assert health_response.status_code == 200
    assert health_response.json() == {"status": "ok"}
    assert listings_response.status_code == 503
    assert docs_response.status_code == 503


@pytest.mark.asyncio
async def test_active_mode_preserves_existing_action_routes(monkeypatch):
    monkeypatch.setattr(storefront_config, "ACTIVATION_MODE", "active")
    app = FastAPI()
    app.middleware("http")(server.enforce_activation_mode)

    @app.post("/action")
    async def action():
        return {"status": "handled"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/action")

    assert response.status_code == 200
    assert response.json() == {"status": "handled"}


@pytest.mark.asyncio
async def test_inert_startup_skips_external_and_background_seams(monkeypatch):
    monkeypatch.setattr(storefront_config, "ACTIVATION_MODE", "inert")
    probe_chain = AsyncMock(side_effect=AssertionError("chain probe must not run"))
    preflight = AsyncMock()
    seed = AsyncMock(return_value={"seeded": True, "imported_count": 1, "source": "test"})

    # The production helper is synchronous; a plain lambda ensures any call
    # fails immediately while keeping the assertion independent of await rules.
    monkeypatch.setattr(
        agent,
        "_maybe_join_zerotier_network",
        lambda: (_ for _ in ()).throw(AssertionError("zerotier must not run")),
    )
    monkeypatch.setattr(agent, "_probe_chain_addresses", probe_chain)
    monkeypatch.setattr(agent, "_preflight_provisioning", preflight)
    thread_store = Mock(side_effect=AssertionError("negotiation state must not initialize"))
    monkeypatch.setattr("market_policy.negotiation_thread.get_thread_store", thread_store)
    monkeypatch.setattr(
        container,
        "resolved_sqlite_client",
        SimpleNamespace(),
    )
    monkeypatch.setattr(
        container,
        "resolved_system_service",
        SimpleNamespace(seed_resources_if_empty=seed),
    )

    await agent._startup_tasks()

    probe_chain.assert_not_awaited()
    preflight.assert_awaited_once_with()
    seed.assert_not_awaited()
    thread_store.assert_not_called()


@pytest.mark.asyncio
async def test_inert_lifespan_never_constructs_signer_clients(monkeypatch):
    import market_storefront.services.alkahest_service as alkahest_service
    import market_storefront.services.listing_service as listing_service
    import market_storefront.services.negotiation_service as negotiation_service
    import market_storefront.services.system_service as system_service

    monkeypatch.setattr(storefront_config, "ACTIVATION_MODE", "inert")
    monkeypatch.setattr(server, "get_sqlite_client", lambda: object())
    monkeypatch.setattr(
        alkahest_service,
        "build_clients",
        lambda: (_ for _ in ()).throw(
            AssertionError("signer/chain clients must not be constructed")
        ),
    )
    listing_constructor = Mock(side_effect=AssertionError("listing service must not initialize"))
    negotiation_constructor = Mock(
        side_effect=AssertionError("negotiation service must not initialize")
    )
    monkeypatch.setattr(listing_service, "ListingService", listing_constructor)
    monkeypatch.setattr(
        negotiation_service, "NegotiationService", negotiation_constructor
    )
    monkeypatch.setattr(system_service, "SystemService", lambda **_: object())
    startup = AsyncMock()
    monkeypatch.setattr(agent, "_startup_tasks", startup)

    async with server.lifespan(server.app):
        assert container.resolved_alkahest_clients == {}
        assert container.resolved_listing_service is None
        assert container.resolved_negotiation_service is None

    startup.assert_awaited_once_with()
    listing_constructor.assert_not_called()
    negotiation_constructor.assert_not_called()


@pytest.mark.asyncio
async def test_inert_health_reports_disabled_capabilities(monkeypatch, tmp_path):
    monkeypatch.setattr(storefront_config, "ACTIVATION_MODE", "inert")
    monkeypatch.setattr(container, "resolved_alkahest_clients", {})
    db = SQLiteClient(db_path=str(tmp_path / "inert-health.db"))
    result = await SystemService(sqlite_client=db).get_health()

    assert result["status"] == "ok"
    assert result["checks"]["alkahest"] == "disabled"
    assert result["activation_mode"] == "inert"
    assert result["signing_enabled"] is False
    assert result["external_actions_enabled"] is False
    assert result["background_tasks_enabled"] is False


@pytest.mark.asyncio
async def test_inert_status_proves_private_dependency_health(monkeypatch, tmp_path):
    monkeypatch.setattr(storefront_config, "ACTIVATION_MODE", "inert")
    monkeypatch.setattr(container, "resolved_alkahest_clients", {})
    db = SQLiteClient(db_path=str(tmp_path / "inert-status.db"))
    service = SystemService(sqlite_client=db)
    registry = AsyncMock(return_value="ok")
    registry_auth = AsyncMock(return_value="ok")
    provisioning = AsyncMock(return_value="ok")
    monkeypatch.setattr(service, "registry_check", registry)
    monkeypatch.setattr(service, "registry_auth_check", registry_auth)
    monkeypatch.setattr(service, "provisioning_check", provisioning)

    result = await service.get_health(include_registry=True)

    assert result["status"] == "ok"
    assert result["checks"]["registry"] == "ok"
    assert result["checks"]["registry_auth"] == "ok"
    assert result["checks"]["provisioning"] == "ok"
    assert result["checks"]["negotiation_strategy"] == "disabled"
    registry.assert_awaited_once_with()
    registry_auth.assert_awaited_once_with()
    provisioning.assert_awaited_once_with()


def test_inert_entrypoint_never_starts_zerotier_daemon(tmp_path):
    storefront_root = Path(__file__).resolve().parents[2]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "sudo-called"
    fake_sudo = fake_bin / "sudo"
    fake_sudo.write_text(
        "#!/bin/sh\n"
        ': > "$INERT_SUDO_MARKER"\n'
        "exit 99\n"
    )
    fake_sudo.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "STOREFRONT_ACTIVATION_MODE": "inert",
            "INERT_SUDO_MARKER": str(marker),
        }
    )
    result = subprocess.run(
        [
            "/bin/sh",
            str(storefront_root / "entrypoint.sh"),
            "python3",
            "-c",
            "print('inert-entrypoint-ok')",
        ],
        cwd=storefront_root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert marker.exists() is False
    assert "Inert activation mode: ZeroTier daemon disabled." in result.stdout
    assert "inert-entrypoint-ok" in result.stdout
