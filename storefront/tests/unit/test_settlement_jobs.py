"""Unit tests for the polling-mode settlement flow.

Covers:
- escrows table round-trip through SQLiteClient helpers.
- start_settlement_job: refuses missing thread, non-terminal thread,
  no-agreed-price thread, missing seller order.
- Idempotence: second start on the same escrow_uid returns existing row.
- Background task: mocked fulfill_compute_obligation drives the row to
  ready / failed states.
- Response serializer omits None fields and parses tenant_credentials JSON.
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from service.schemas import ProvisionTerms

from market_storefront.utils.sqlite_client import SQLiteClient
from market_storefront.utils.settlement_jobs import (
    _run_settlement_job_bg,
    serialize_settlement_job,
    start_settlement_job,
)


# ---------------------------------------------------------------------------
# SQLiteClient escrows helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    return SQLiteClient(db_path=str(tmp_path / "agent.db"))


@pytest.fixture(autouse=True)
def _anvil_chain(monkeypatch):
    """Inject a synthetic [chains.anvil] entry so start_settlement_job's
    ``CHAINS.get(chain_name)`` lookup resolves in tests that don't write a
    full storefront.toml."""
    from service.config_loader import ChainConfig
    from market_storefront.utils import config as agent_config

    monkeypatch.setattr(
        agent_config,
        "CHAINS",
        {
            "anvil": ChainConfig(
                name="anvil",
                rpc_url="http://localhost:8545",
                chain_id=31337,
                alkahest_address_config_path=None,
            ),
        },
        raising=False,
    )


@pytest.mark.asyncio
async def test_insert_escrow_happy_path(client):
    ok = await client.insert_escrow(
        escrow_uid="0xescrow-1",
        negotiation_id="neg-1",
        chain_name="anvil",
        escrow_address="0x" + "aa" * 20,
    )
    assert ok is True
    row = await client.load_escrow(escrow_uid="0xescrow-1")
    assert row is not None
    assert row["negotiation_id"] == "neg-1"
    assert row["status"] == "provisioning"
    assert row["chain_name"] == "anvil"
    assert row["escrow_address"] == "0x" + "aa" * 20
    assert row["is_primary"] is True
    assert row["fulfillment_uid"] is None


@pytest.mark.asyncio
async def test_insert_is_idempotent_by_primary_key(client):
    assert await client.insert_escrow(
        escrow_uid="0xescrow-1",
        negotiation_id="neg-1",
        chain_name="anvil",
        escrow_address="0x" + "aa" * 20,
    ) is True
    # Second insert for same escrow returns False, doesn't overwrite.
    assert await client.insert_escrow(
        escrow_uid="0xescrow-1",
        negotiation_id="neg-DIFFERENT",
        chain_name="other",
        escrow_address="0x" + "bb" * 20,
    ) is False
    row = await client.load_escrow(escrow_uid="0xescrow-1")
    assert row["negotiation_id"] == "neg-1"
    assert row["chain_name"] == "anvil"


@pytest.mark.asyncio
async def test_update_escrow_patches_only_provided_fields(client):
    await client.insert_escrow(
        escrow_uid="0xescrow-1", negotiation_id="neg-1",
        chain_name="anvil", escrow_address="0x" + "aa" * 20,
    )
    await client.update_escrow(
        escrow_uid="0xescrow-1",
        status="ready",
        fulfillment_uid="0xfulfill",
        connection_details="ssh alice@vm1",
    )
    row = await client.load_escrow(escrow_uid="0xescrow-1")
    assert row["status"] == "ready"
    assert row["fulfillment_uid"] == "0xfulfill"
    assert row["connection_details"] == "ssh alice@vm1"
    # reason left untouched
    assert row["reason"] is None


@pytest.mark.asyncio
async def test_load_missing_escrow_returns_none(client):
    assert await client.load_escrow(escrow_uid="0xnope") is None


@pytest.mark.asyncio
async def test_load_primary_escrow_for_negotiation(client):
    await client.insert_escrow(
        escrow_uid="0xprimary", negotiation_id="neg-1",
        chain_name="anvil", escrow_address="0x" + "aa" * 20,
        is_primary=True,
    )
    await client.insert_escrow(
        escrow_uid="0xbond", negotiation_id="neg-1",
        chain_name="anvil", escrow_address="0x" + "bb" * 20,
        is_primary=False,
    )
    primary = await client.load_primary_escrow_for_negotiation(negotiation_id="neg-1")
    assert primary is not None
    assert primary["escrow_uid"] == "0xprimary"
    assert primary["is_primary"] is True


# ---------------------------------------------------------------------------
# start_settlement_job — validation + idempotence
# ---------------------------------------------------------------------------


async def _seed_negotiation(
    client: SQLiteClient,
    *,
    neg_id: str = "neg-1",
    our_listing_id: str = "seller-ord-1",
    terminal: str | None = "success",
    agreed_price: float | None = 10**18,
    agreed_duration_seconds: int | None = 3600,
) -> None:
    conn = sqlite3.connect(client.db_path)
    try:
        conn.execute(
            """INSERT OR REPLACE INTO negotiation_threads
               (negotiation_id, our_listing_id, their_listing_id,
                our_agent_id, their_agent_id, status,
                created_at, updated_at, terminal_state,
                agreed_price, agreed_duration_seconds, agreed_at)
               VALUES (?, ?, 'buyer-ord-1',
                       'http://seller:8001', 'http://buyer:8000', 'active',
                       '2026-04-23T00:00:00Z', '2026-04-23T00:00:00Z', ?,
                       ?, ?, ?)""",
            (
                neg_id, our_listing_id, terminal,
                agreed_price, agreed_duration_seconds,
                "2026-04-23T00:00:00Z" if agreed_price is not None else None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def _seed_seller_order(client: SQLiteClient, listing_id: str = "seller-ord-1") -> None:
    conn = sqlite3.connect(client.db_path)
    try:
        conn.execute(
            """INSERT INTO listings (listing_id, status, created_at, updated_at,
                                   offer_resource, max_duration_seconds,
                                   seller, accepted_escrows)
               VALUES (?, 'open', '2026-04-23T00:00:00Z', '2026-04-23T00:00:00Z',
                       '{}', 3600, 'http://seller:8001',
                       '[{"chain_name": "anvil", "escrow_address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]')""",
            (listing_id,),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["valid", "foreign_buyer", "forged_attester",
    "forged_schema", "wrong_uid", "reference", "nonrevocable", "ownerless", "wrong_chain"])
async def test_real_verifier_guards_initial_settlement_before_insert(client, monkeypatch, mutation):
    """Actual orchestrator + verifier + codec + SQLite; only transport and work are fake."""
    import asyncio
    from types import SimpleNamespace
    from market_storefront.utils.escrow_verification import (
        EscrowVerificationError, erc20_escrow_schema_uid,
    )
    buyer, seller, escrow, token, arbiter = ["0x" + c * 40 for c in "12345"]
    uid = "0x" + "ab" * 32
    await _seed_seller_order(client)
    await _seed_negotiation(client)
    await client.bind_authenticated_negotiation_buyer(negotiation_id="neg-1", buyer=buyer)
    accepted = [{"chain_name": "anvil", "escrow_address": escrow,
                 "literal_fields": {"token": token}}]
    with sqlite3.connect(client.db_path) as con:
        con.execute("UPDATE listings SET accepted_escrows=? WHERE listing_id='seller-ord-1'",
                    (json.dumps(accepted),))
        if mutation == "ownerless":
            con.execute("UPDATE negotiation_threads SET buyer=NULL WHERE negotiation_id='neg-1'")
        if mutation == "wrong_chain":
            con.execute("UPDATE negotiation_threads SET buyer_escrow_proposal=? WHERE negotiation_id='neg-1'",
                        (json.dumps({"chain_name": "base_sepolia", "escrow_address": escrow,
                                     "literal_fields": {"token": token},
                                     "expiration_unix": 4_000_000_000}),))
    envelope = SimpleNamespace(uid=uid, recipient=buyer, attester=escrow,
        schema=erc20_escrow_schema_uid(escrow), ref_uid="0x" + "00" * 32,
        revocable=True, time=1_600_000_000, expiration_time=4_000_000_000, revocation_time=0)
    changes = {"foreign_buyer": ("recipient", seller), "forged_attester": ("attester", buyer),
        "forged_schema": ("schema", "0x" + "cd" * 32), "wrong_uid": ("uid", "0x" + "ef" * 32),
        "reference": ("ref_uid", uid), "nonrevocable": ("revocable", False)}
    if mutation in changes:
        setattr(envelope, *changes[mutation])
    obligation = dict(token=token, amount=10**18, arbiter=arbiter, demand=b"\x12\x34")
    getter = AsyncMock(return_value={"attestation": envelope, "data": SimpleNamespace(**obligation)})
    sdk = SimpleNamespace(erc20=SimpleNamespace(escrow=SimpleNamespace(
        non_tierable=SimpleNamespace(get_obligation=getter))))
    monkeypatch.setattr("service.clients.alkahest.get_erc20_escrow_obligation_nontierable",
                        lambda *args, **kwargs: escrow)
    monkeypatch.setattr("service.clients.alkahest.build_payment_obligation_data",
                        lambda **kwargs: obligation)
    monkeypatch.setattr("market_storefront.utils.config.settings",
                        SimpleNamespace(wallet=SimpleNamespace(address=seller)))
    bg = AsyncMock()
    with patch("market_storefront.utils.settlement_jobs._run_settlement_job_bg", bg):
        if mutation == "valid":
            result = await start_settlement_job(escrow_uid=uid, negotiation_id="neg-1",
                ssh_public_key="ssh-ed25519 synthetic", sqlite_client=client,
                alkahest_client=sdk, chain_name="anvil")
            await asyncio.sleep(0)
            assert result["status"] == "provisioning"
            assert (await client.load_escrow(escrow_uid=uid))["negotiation_id"] == "neg-1"
            bg.assert_awaited_once()
        else:
            with pytest.raises(EscrowVerificationError):
                await start_settlement_job(escrow_uid=uid, negotiation_id="neg-1",
                    ssh_public_key="ssh-ed25519 synthetic", sqlite_client=client,
                    alkahest_client=sdk, chain_name="anvil")
            assert await client.load_escrow(escrow_uid=uid) is None
            bg.assert_not_called()
    if mutation in {"ownerless", "wrong_chain"}:
        getter.assert_not_awaited()
    else:
        getter.assert_awaited_once_with(uid)


@pytest.mark.asyncio
async def test_start_refuses_unknown_negotiation(client):
    await _seed_seller_order(client)
    with pytest.raises(ValueError, match="Unknown negotiation"):
        await start_settlement_job(
            escrow_uid="0xescrow",
            negotiation_id="nope",
            ssh_public_key="ssh-rsa ...",
            sqlite_client=client,
            alkahest_client=MagicMock(),
            chain_name='anvil',
        )


@pytest.mark.asyncio
async def test_start_refuses_non_terminal_thread(client):
    await _seed_seller_order(client)
    await _seed_negotiation(client, terminal=None, agreed_price=None)
    with pytest.raises(ValueError, match="not terminal-success"):
        await start_settlement_job(
            escrow_uid="0xescrow",
            negotiation_id="neg-1",
            ssh_public_key="ssh-rsa ...",
            sqlite_client=client,
            alkahest_client=MagicMock(),
            chain_name='anvil',
        )


@pytest.mark.asyncio
async def test_start_refuses_terminal_without_agreed_price(client):
    await _seed_seller_order(client)
    await _seed_negotiation(client, agreed_price=None)
    with pytest.raises(ValueError, match="no agreed_price"):
        await start_settlement_job(
            escrow_uid="0xescrow",
            negotiation_id="neg-1",
            ssh_public_key="ssh-rsa ...",
            sqlite_client=client,
            alkahest_client=MagicMock(),
            chain_name='anvil',
        )


@pytest.mark.asyncio
async def test_start_refuses_when_seller_order_gone(client):
    # No seller order seeded.
    await _seed_negotiation(client, our_listing_id="seller-gone")
    with pytest.raises(ValueError, match="is gone from the local DB"):
        await start_settlement_job(
            escrow_uid="0xescrow",
            negotiation_id="neg-1",
            ssh_public_key="ssh-rsa ...",
            sqlite_client=client,
            alkahest_client=MagicMock(),
            chain_name='anvil',
        )


@pytest.mark.asyncio
async def test_start_happy_path_inserts_row_and_kicks_off_task(client):
    await _seed_seller_order(client)
    await _seed_negotiation(client)

    # Prevent the background task from doing real work during the test.
    # Bypass on-chain escrow verification — covered in test_escrow_verification.py.
    with patch(
        "market_storefront.utils.settlement_jobs._run_settlement_job_bg",
        new=AsyncMock(),
    ), patch(
        "market_storefront.utils.escrow_verification.verify_escrow_for_settlement",
        new=AsyncMock(),
    ):
        result = await start_settlement_job(
            escrow_uid="0xescrow",
            negotiation_id="neg-1",
            ssh_public_key="ssh-rsa ...",
            sqlite_client=client,
            alkahest_client=MagicMock(),
            chain_name='anvil',
        )

    assert result["status"] == "provisioning"
    assert result["escrow_uid"] == "0xescrow"
    row = await client.load_escrow(escrow_uid="0xescrow")
    assert row is not None
    assert row["status"] == "provisioning"
    # chain_name / escrow_address pinned from the listing's accepted_escrows[0]
    # when the thread has no buyer_escrow_proposal.
    assert row["chain_name"] == "anvil"
    assert row["escrow_address"] == "0x" + "a" * 40
    assert row["is_primary"] is True


@pytest.mark.asyncio
async def test_start_aborts_when_escrow_verification_rejects(client):
    """If on-chain verification fails, no escrows row is inserted and no
    background task is scheduled — fail-closed."""
    from market_storefront.utils.escrow_verification import EscrowVerificationError

    await _seed_seller_order(client)
    await _seed_negotiation(client)

    bg_mock = AsyncMock()
    verify_mock = AsyncMock(side_effect=EscrowVerificationError("amount insufficient"))
    with patch(
        "market_storefront.utils.settlement_jobs._run_settlement_job_bg",
        new=bg_mock,
    ), patch(
        "market_storefront.utils.escrow_verification.verify_escrow_for_settlement",
        new=verify_mock,
    ):
        with pytest.raises(EscrowVerificationError, match="amount insufficient"):
            await start_settlement_job(
                escrow_uid="0xescrow",
                negotiation_id="neg-1",
                ssh_public_key="ssh-rsa ...",
                sqlite_client=client,
                alkahest_client=MagicMock(),
            chain_name='anvil',
            )

    # No DB row, no background task.
    assert await client.load_escrow(escrow_uid="0xescrow") is None
    bg_mock.assert_not_called()


@pytest.mark.asyncio
async def test_start_is_idempotent_by_escrow_uid(client):
    await _seed_seller_order(client)
    await _seed_negotiation(client)

    with patch(
        "market_storefront.utils.settlement_jobs._run_settlement_job_bg",
        new=AsyncMock(),
    ), patch(
        "market_storefront.utils.escrow_verification.verify_escrow_for_settlement",
        new=AsyncMock(),
    ):
        first = await start_settlement_job(
            escrow_uid="0xescrow", negotiation_id="neg-1",
            ssh_public_key="ssh-rsa ...",
            sqlite_client=client, alkahest_client=MagicMock(),
            chain_name='anvil',
        )
        # Flip the existing job to 'ready' to prove the second call reads, not overwrites.
        await client.update_escrow(
            escrow_uid="0xescrow", status="ready",
            fulfillment_uid="0xattest", connection_details="ssh bob@vm",
        )
        second = await start_settlement_job(
            escrow_uid="0xescrow", negotiation_id="neg-1",
            ssh_public_key="ssh-rsa ...",
            sqlite_client=client, alkahest_client=MagicMock(),
            chain_name='anvil',
        )

    assert first["status"] == "provisioning"
    # Second call returned existing row, did not overwrite to provisioning again.
    assert second.get("status") == "ready"
    assert second.get("fulfillment_uid") == "0xattest"


# ---------------------------------------------------------------------------
# _run_settlement_job_bg — patches escrow row from fulfill_compute_obligation result
# ---------------------------------------------------------------------------


async def _seed_escrow_provisioning(
    client: SQLiteClient,
    *,
    escrow_uid: str = "0xescrow",
    negotiation_id: str = "neg-1",
) -> None:
    await client.insert_escrow(
        escrow_uid=escrow_uid,
        negotiation_id=negotiation_id,
        chain_name="anvil",
        escrow_address="0x" + "aa" * 20,
    )


@pytest.mark.asyncio
async def test_background_task_writes_ready_on_success(client):
    await _seed_escrow_provisioning(client)
    mock_fulfill = AsyncMock(return_value={
        "status": "fulfilled",
        "fulfillment_uid": "0xattest",
        "connection_details": "ssh alice@vm1",
        "tenant_credentials": {"password": "secret"},
    })

    with patch(
        "market_storefront.utils.action_executor.fulfill_compute_obligation",
        new=mock_fulfill,
    ):
        await _run_settlement_job_bg(
            escrow_uid="0xescrow",
            provision=ProvisionTerms(duration_seconds=3600, ssh_public_key="ssh-rsa ..."),
            listing_id="seller-ord-1",
            order_dict={"listing_id": "seller-ord-1", "max_duration_seconds": 3600},
            sqlite_client=client,
            alkahest_client=MagicMock(),
        )

    row = await client.load_escrow(escrow_uid="0xescrow")
    assert row["status"] == "ready"
    assert row["fulfillment_uid"] == "0xattest"
    assert row["connection_details"] == "ssh alice@vm1"
    assert json.loads(row["tenant_credentials"]) == {"password": "secret"}


@pytest.mark.asyncio
async def test_background_task_writes_failed_on_exception(client):
    await _seed_escrow_provisioning(client)
    mock_fulfill = AsyncMock(side_effect=RuntimeError("vm host unreachable"))

    with patch(
        "market_storefront.utils.action_executor.fulfill_compute_obligation",
        new=mock_fulfill,
    ):
        await _run_settlement_job_bg(
            escrow_uid="0xescrow",
            provision=ProvisionTerms(duration_seconds=3600, ssh_public_key="ssh-rsa ..."),
            listing_id="seller-ord-1",
            order_dict={"listing_id": "seller-ord-1", "max_duration_seconds": 3600},
            sqlite_client=client,
            alkahest_client=MagicMock(),
        )

    row = await client.load_escrow(escrow_uid="0xescrow")
    assert row["status"] == "failed"
    assert "vm host unreachable" in row["reason"]


@pytest.mark.asyncio
async def test_uncertain_preparation_preserves_escrow_fence_without_retry(client):
    await _seed_seller_order(client)
    await _seed_negotiation(client)
    await _seed_escrow_provisioning(client)
    with patch("market_storefront.utils.action_executor.fulfill_compute_obligation",
               new=AsyncMock(return_value={"status": "uncertain"})):
        await _run_settlement_job_bg(
            escrow_uid="0xescrow", provision=ProvisionTerms(duration_seconds=3600, ssh_public_key=""),
            listing_id="seller-ord-1", order_dict={}, sqlite_client=client, alkahest_client=MagicMock(),
        )
    row = await client.load_escrow(escrow_uid="0xescrow")
    assert row["status"] == "provisioning"
    assert "reconciliation_required" in row["reason"]
    assert not row.get("fulfillment_uid") and not row.get("connection_details")
    with patch("market_storefront.utils.settlement_jobs._run_settlement_job_bg", new=AsyncMock()) as bg, \
         patch("market_storefront.utils.escrow_verification.verify_escrow_for_settlement", new=AsyncMock()):
        again = await start_settlement_job(
            escrow_uid="0xescrow", negotiation_id="neg-1", ssh_public_key="",
            sqlite_client=client, alkahest_client=MagicMock(), chain_name="anvil",
        )
    assert again["reason"] == row["reason"]
    bg.assert_not_called()


@pytest.mark.asyncio
async def test_background_task_leaves_listing_open_on_failure(client):
    """A failed deal updates only per-escrow state; listing state is unchanged."""
    await _seed_seller_order(client, listing_id="seller-ord-1")
    await _seed_escrow_provisioning(client)
    mock_fulfill = AsyncMock(side_effect=RuntimeError("vm host unreachable"))

    with patch(
        "market_storefront.utils.action_executor.fulfill_compute_obligation",
        new=mock_fulfill,
    ):
        await _run_settlement_job_bg(
            escrow_uid="0xescrow",
            provision=ProvisionTerms(duration_seconds=3600, ssh_public_key="ssh-rsa ..."),
            listing_id="seller-ord-1",
            order_dict={"listing_id": "seller-ord-1", "max_duration_seconds": 3600},
            sqlite_client=client,
            alkahest_client=MagicMock(),
        )

    assert (await client.load_escrow(escrow_uid="0xescrow"))["status"] == "failed"
    listing = await client.load_listing(listing_id="seller-ord-1")
    assert listing["status"] == "open"


@pytest.mark.asyncio
async def test_background_task_writes_failed_on_non_fulfilled_status(client):
    """fulfill_compute_obligation returned a non-exception but non-success result."""
    await _seed_escrow_provisioning(client)
    mock_fulfill = AsyncMock(return_value={
        "status": "error",
        "message": "Provisioning failed: No available compute VM",
    })

    with patch(
        "market_storefront.utils.action_executor.fulfill_compute_obligation",
        new=mock_fulfill,
    ):
        await _run_settlement_job_bg(
            escrow_uid="0xescrow",
            provision=ProvisionTerms(duration_seconds=3600, ssh_public_key="ssh-rsa ..."),
            listing_id="seller-ord-1",
            order_dict={"listing_id": "seller-ord-1", "max_duration_seconds": 3600},
            sqlite_client=client,
            alkahest_client=MagicMock(),
        )

    row = await client.load_escrow(escrow_uid="0xescrow")
    assert row["status"] == "failed"
    assert "No available compute VM" in row["reason"]


# ---------------------------------------------------------------------------
# serialize_settlement_job
# ---------------------------------------------------------------------------


def test_serialize_omits_none_fields():
    raw = {
        "escrow_uid": "0xe",
        "negotiation_id": "neg-1",
        "status": "provisioning",
        "fulfillment_uid": None,
        "connection_details": None,
        "tenant_credentials": None,
        "reason": None,
        "created_at": "2026-04-23T00:00:00Z",
        "updated_at": "2026-04-23T00:00:00Z",
    }
    out = serialize_settlement_job(raw)
    assert "reason" not in out
    assert "attestation_uid" not in out
    assert "fulfillment_uid" not in out
    assert "tenant_credentials" not in out
    assert out["status"] == "provisioning"


def test_serialize_parses_tenant_credentials_json():
    raw = {
        "escrow_uid": "0xe",
        "negotiation_id": "neg-1",
        "status": "ready",
        "fulfillment_uid": "0xa",
        "connection_details": "ssh alice@vm",
        "tenant_credentials": json.dumps({"password": "secret"}),
        "reason": None,
        "created_at": "2026-04-23T00:00:00Z",
        "updated_at": "2026-04-23T00:00:00Z",
    }
    out = serialize_settlement_job(raw)
    assert out["tenant_credentials"] == {"password": "secret"}
    assert out["fulfillment_uid"] == "0xa"
    assert "attestation_uid" not in out
