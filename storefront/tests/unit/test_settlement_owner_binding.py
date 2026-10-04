"""Actual HTTP method/auth/binder bodies with synthetic signatures and storage."""
import ast
import asyncio
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import HTTPException
from market_storefront.middleware import buyer_auth
from market_storefront.utils import settlement_continuation

ROOT = Path(settlement_continuation.__file__).parents[1]


def function(path, name, namespace):
    node = next(n for n in ast.walk(ast.parse((ROOT / path).read_text()))
                if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    node.decorator_list = []
    tree = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(tree), str(path), "exec"), namespace)
    return namespace[name]


class OwnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "owner.db")
        self.owner = Account.from_key("11" * 32)
        self.foreign = Account.from_key("22" * 32)
        with sqlite3.connect(self.path) as con:
            con.execute("CREATE TABLE negotiation_threads(negotiation_id TEXT PRIMARY KEY,buyer TEXT,their_agent_id TEXT)")
        self.db = SimpleNamespace(db_path=self.path)
        binder = function("utils/sqlite_client.py", "bind_authenticated_negotiation_buyer",
                          dict(asyncio=asyncio, sqlite3=sqlite3))
        self.db.bind_authenticated_negotiation_buyer = lambda **kw: binder(self.db, **kw)
        async def load(*, negotiation_id):
            with sqlite3.connect(self.path) as con:
                con.row_factory = sqlite3.Row
                row = con.execute("SELECT * FROM negotiation_threads WHERE negotiation_id=?", (negotiation_id,)).fetchone()
                return dict(row) if row else None
        self.db.load_negotiation_thread_row = load
        self.db.load_escrow = AsyncMock(return_value={"escrow_uid": "escrow", "negotiation_id": "neg-1",
            "status": "provisioning", "tenant_credentials": '{"password":"synthetic-private"}'})

    def signed(self, account, operation, resource):
        ts = str(int(time.time()))
        sig = account.sign_message(encode_defunct(text=f"{operation}:{resource}:{ts}")).signature.hex()
        return SimpleNamespace(headers={"X-Signature": sig, "X-Timestamp": ts})

    async def negotiate(self):
        async def start(**kwargs):
            with sqlite3.connect(self.path) as con:
                con.execute("INSERT INTO negotiation_threads VALUES ('neg-1',NULL,?)", (kwargs["their_agent_url"],))
            return {"negotiation_id": "neg-1"}
        endpoint = function("controllers/negotiate_controller.py", "negotiate_new", dict(
            buyer_auth=buyer_auth, HTTPException=HTTPException, NegotiateNewResponse=SimpleNamespace))
        # Even an address-shaped routing URL cannot become ownership authority.
        body = SimpleNamespace(listing_id="listing-1", buyer_address=self.owner.address,
            buyer_agent_url=self.foreign.address, provision_terms=None, proposal=None)
        with patch.dict(sys.modules, {
            "market_storefront.utils.config": SimpleNamespace(BASE_URL_OVERRIDE=""),
            "market_storefront.utils.sync_negotiation": SimpleNamespace(start_sync_negotiation=start,
                OfferUnfulfillableError=type("OfferError", (Exception,), {}),
                StorefrontPausedError=type("Paused", (Exception,), {})),
        }):
            await endpoint(SimpleNamespace(_db=self.db), body, self.signed(self.owner, "negotiate_new", "listing-1"))
        self.assertEqual((await self.db.load_negotiation_thread_row(negotiation_id="neg-1"))["buyer"], self.owner.address.lower())

    def status_endpoint(self):
        require = function("controllers/settle_controller.py", "require_settlement_owner", dict(buyer_auth=buyer_auth))
        return function("controllers/settle_controller.py", "settle_status", dict(
            buyer_auth=buyer_auth, require_settlement_owner=require, HTTPException=HTTPException,
            Query=lambda **kwargs: None, SettleStatusResponse=SimpleNamespace))

    async def test_authenticated_creation_same_owner_status_and_foreign_signature_refusal(self):
        await self.negotiate()
        endpoint = self.status_endpoint()
        with patch("market_storefront.utils.settlement_jobs.reconcile_retained_settlement", new=AsyncMock()) as observe:
            result = await endpoint(SimpleNamespace(_db=self.db), "escrow",
                self.signed(self.owner, "settle_status", "escrow"), buyer_address=self.owner.address)
            self.assertEqual(result.status, "provisioning")
            observe.assert_awaited_once()
            observe.reset_mock()
            with self.assertRaises(HTTPException) as denied:
                await endpoint(SimpleNamespace(_db=self.db), "escrow",
                    self.signed(self.foreign, "settle_status", "escrow"), buyer_address=self.foreign.address)
            self.assertEqual(denied.exception.status_code, 404)
            observe.assert_not_awaited()

    async def test_foreign_post_cannot_start_settlement_or_resolve_chain(self):
        await self.negotiate()
        require = function("controllers/settle_controller.py", "require_settlement_owner", dict(buyer_auth=buyer_auth))
        endpoint = function("controllers/settle_controller.py", "settle_escrow", dict(
            buyer_auth=buyer_auth, require_settlement_owner=require, HTTPException=HTTPException))
        with patch("market_storefront.utils.settlement_jobs.start_settlement_job", new=AsyncMock()) as start:
            with self.assertRaises(HTTPException) as denied:
                await endpoint(SimpleNamespace(_db=self.db), "escrow", SimpleNamespace(
                    buyer_address=self.foreign.address, negotiation_id="neg-1"),
                    self.signed(self.foreign, "settle_escrow", "escrow"))
            self.assertEqual(denied.exception.status_code, 404)
            start.assert_not_awaited()

    async def test_same_owner_post_reaches_existing_settlement_orchestration(self):
        await self.negotiate()
        require = function("controllers/settle_controller.py", "require_settlement_owner", dict(buyer_auth=buyer_auth))
        chain = SimpleNamespace(get_alkahest_client=lambda _: object())
        endpoint = function("controllers/settle_controller.py", "settle_escrow", dict(
            buyer_auth=buyer_auth, require_settlement_owner=require, HTTPException=HTTPException,
            _container=chain, JSONResponse=lambda **kwargs: kwargs))
        with patch("market_storefront.utils.settlement_jobs.start_settlement_job", new=AsyncMock(
                return_value={"escrow_uid": "escrow", "status": "provisioning"})) as start:
            response = await endpoint(SimpleNamespace(_db=self.db), "escrow", SimpleNamespace(
                buyer_address=self.owner.address, negotiation_id="neg-1", chain_name="base_sepolia",
                ssh_public_key="", container_env=None), self.signed(self.owner, "settle_escrow", "escrow"))
            self.assertEqual(response["status_code"], 202)
            start.assert_awaited_once()

    async def test_legacy_route_label_never_supplies_buyer_authority(self):
        with sqlite3.connect(self.path) as con:
            con.execute("INSERT INTO negotiation_threads VALUES ('neg-1',NULL,?)", (self.foreign.address,))
        with self.assertRaises(HTTPException):
            await buyer_auth.require_negotiation_owner(self.db, "neg-1", self.foreign.address,
                self.signed(self.foreign, "settle_status", "escrow"))

    async def test_persisted_owner_cannot_be_rebound(self):
        await self.negotiate()
        with self.assertRaisesRegex(ValueError, "conflict"):
            await self.db.bind_authenticated_negotiation_buyer(negotiation_id="neg-1", buyer=self.foreign.address.lower())
