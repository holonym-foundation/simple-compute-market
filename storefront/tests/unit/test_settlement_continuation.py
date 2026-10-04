"""Synthetic retained-job recovery, including the actual orchestration bodies.

No config, wallet, model, inference, transport, or signing imports. AST extraction
executes the checked-in async entry points with inert dependencies, not replicas.
"""
import ast
import asyncio
import dataclasses
import json
from pathlib import Path
import sqlite3
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from market_storefront.utils import settlement_continuation as recovery

UTILS = Path(recovery.__file__).parent
UID = "0x" + "ab" * 32
JOB = str(uuid.uuid5(uuid.NAMESPACE_URL, "scm-container-lease:" + UID))
TARGET = "tenant-" + uuid.UUID(JOB).hex
PARAMS = dict(vm_host="host-a", vm_action="create", vm_target=TARGET,
              provisioning_type="container", container_image="example.invalid/image@sha256:" + "a" * 64,
              container_env={"AEX_CAPABILITY_DIRECTORY": "/run/synthetic", "AEX_ENV": "staging"},
              lease_id=UID, max_retries=0)


def actual_function(filename, name, namespace):
    tree = ast.parse((UTILS / filename).read_text())
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    source = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    namespace.update(__package__="market_storefront.utils")
    exec(compile(ast.fix_missing_locations(source), filename, "exec"), namespace)
    return namespace[name]


class ContinuationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "seller.db")
        with sqlite3.connect(self.path) as con:
            con.executescript("""
                CREATE TABLE escrows (escrow_uid TEXT PRIMARY KEY, negotiation_id TEXT,
                    status TEXT, fulfillment_uid TEXT, provisioning_job_id TEXT,
                    chain_name TEXT, escrow_address TEXT, reason TEXT);
                CREATE TABLE compute_allocations (allocation_id TEXT PRIMARY KEY,
                    escrow_uid TEXT, resource_id TEXT, listing_id TEXT, state TEXT,
                    vm_host TEXT, vm_target TEXT);
            """)
            con.execute("INSERT INTO escrows VALUES (?, 'neg-1', 'provisioning', NULL, NULL, 'base_sepolia', 'synthetic', NULL)", (UID,))
            con.execute("INSERT INTO compute_allocations VALUES ('alloc-1', ?, 'resource-1', 'listing-1', 'reserved', NULL, NULL)", (UID,))

    def freeze(self, **changes):
        args = dict(escrow_uid=UID, allocation_id="alloc-1", resource_id="resource-1",
                    listing_id="listing-1", order={"listing_id": "listing-1", "price": 123},
                    demand_hex="deadbeef", duration_seconds=300, params=PARAMS, now=1000)
        args.update(changes)
        return recovery.freeze_continuation(self.path, **args)

    def job(self, status="succeeded"):
        return dict(job_id=JOB, escrow_uid=UID, status=status, retry_count=0, max_retries=0,
                    next_retry_at=None, error=None, params={**PARAMS, "_container_preparation": {
                        "version": 1, "request_digest": recovery.request_digest(PARAMS),
                        "expires_at": 4600, "admission_id": "10000000-0000-4000-8000-000000000001"}},
                    result=dict(container_name=TARGET, container_id="d" * 64, running=True) if status == "succeeded" else None)

    def assert_fenced(self):
        with sqlite3.connect(self.path) as con:
            self.assertEqual(con.execute("SELECT status,fulfillment_uid FROM escrows").fetchone(), ("provisioning", None))
            self.assertEqual(con.execute("SELECT state FROM compute_allocations").fetchone()[0], "held")

    async def test_restart_late_success_same_job_never_marks_settled(self):
        self.assertEqual(self.freeze(), JOB)
        missing = AsyncMock(side_effect=TimeoutError)
        self.assertEqual(await recovery.reconcile_continuation(self.path, UID, get_job=missing, now=1010), "waiting")
        # New reader invocation/new DB connection represents process restart.
        getter = AsyncMock(return_value=self.job())
        self.assertEqual(await recovery.reconcile_continuation(self.path, UID, get_job=getter, now=1100), "provisioned_pending_settlement")
        getter.assert_awaited_once_with(JOB)
        await recovery.reconcile_continuation(self.path, UID, get_job=getter, now=1150)
        getter.assert_awaited_once()
        saved = recovery.load_continuation(self.path, UID)
        self.assertEqual(json.loads(saved["snapshot"])["deadline"], 1300)
        self.assertFalse(json.loads(saved["observation"])["settlement_verified"])
        self.assert_fenced()

    async def test_expiry_does_not_poll_renew_or_release(self):
        self.freeze()
        getter = AsyncMock(return_value=self.job())
        self.assertEqual(await recovery.reconcile_continuation(self.path, UID, get_job=getter, now=1300), "expired")
        getter.assert_not_awaited()
        self.assert_fenced()

    async def test_duplicate_freeze_refuses_changed_original_terms(self):
        self.freeze()
        before = recovery.load_continuation(self.path, UID)
        with self.assertRaisesRegex(ValueError, "already_frozen"):
            self.freeze(duration_seconds=100, order={"price": 999})
        self.assertEqual(before, recovery.load_continuation(self.path, UID))

    async def test_unknown_legacy_row_has_no_recovery_authority(self):
        getter = AsyncMock()
        self.assertIsNone(await recovery.reconcile_continuation(self.path, UID, get_job=getter, now=1100))
        getter.assert_not_awaited()

    async def test_static_credentials_rejected_before_snapshot_write(self):
        for key in ("OPENAI_API_KEY", "WAAP_PRIVATE_KEY", "TAP_API_KEY", "AEX_INGEST_KEY"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "static_credentials"):
                    self.freeze(params={**PARAMS, "container_env": {**PARAMS["container_env"], key: "synthetic-secret"}})
                self.assertIsNone(recovery.load_continuation(self.path, UID))

    async def test_changed_job_request_retry_admission_or_result_stays_fenced(self):
        self.freeze()
        mutations = [lambda j: j.update(job_id=str(uuid.uuid4())),
                     lambda j: j.update(escrow_uid="other"), lambda j: j.update(retry_count=1),
                     lambda j: j["params"].update(vm_host="other"),
                     lambda j: j["params"]["_container_preparation"].update(admission_id=None),
                     lambda j: j["params"]["_container_preparation"].update(admission_id=JOB),
                     lambda j: j["params"]["_container_preparation"].update(extra="unknown"),
                     lambda j: j.update(unknown=True),
                     lambda j: j["result"].update(running=False),
                     lambda j: j["result"].update(container_id="short-id"),
                     lambda j: j["result"].update(authentication={"synthetic": True}),
                     lambda j: j["result"].update(container_name="other")]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                job = self.job(); mutate(job)
                self.assertEqual(await recovery.reconcile_continuation(self.path, UID, get_job=AsyncMock(return_value=job), now=1100), "waiting")
                self.assert_fenced()

    async def test_change_during_read_cannot_commit_success(self):
        self.freeze()
        async def changed(_):
            with sqlite3.connect(self.path) as con:
                con.execute("UPDATE compute_allocations SET state='released'")
            return self.job()
        with self.assertRaisesRegex(ValueError, "not_held"):
            await recovery.reconcile_continuation(self.path, UID, get_job=changed, now=1100)
        self.assertEqual(recovery.load_continuation(self.path, UID)["state"], "waiting")

    async def test_concurrent_observers_preserve_first_success(self):
        self.freeze()
        await asyncio.gather(*[recovery.reconcile_continuation(
            self.path, UID, get_job=AsyncMock(return_value=self.job()), now=1100) for _ in range(8)])
        self.assertEqual(recovery.load_continuation(self.path, UID)["state"], "provisioned_pending_settlement")
        self.assert_fenced()

    async def test_original_background_exception_cannot_fail_a_frozen_attempt(self):
        self.freeze()
        db = SimpleNamespace(db_path=self.path, update_escrow=AsyncMock())
        run = actual_function("settlement_jobs.py", "_run_settlement_job_bg", dict(
            logger=SimpleNamespace(exception=lambda *a: None)))
        with patch.dict(sys.modules, {
            "market_storefront.utils.action_executor": SimpleNamespace(fulfill_compute_obligation=AsyncMock(side_effect=OSError("synthetic"))),
            "market_storefront.utils.config": SimpleNamespace(settings=SimpleNamespace(wallet=SimpleNamespace(address="synthetic"))),
        }):
            await run(escrow_uid=UID, provision=SimpleNamespace(ssh_public_key="", container_env=PARAMS["container_env"], duration_seconds=300),
                      listing_id="listing-1", order_dict={}, sqlite_client=db, alkahest_client=object())
        db.update_escrow.assert_not_awaited()
        self.assert_fenced()

    async def test_actual_repeat_post_observes_retained_job_without_new_background_work(self):
        self.freeze()
        async def load(**kwargs):
            with sqlite3.connect(self.path) as con:
                con.row_factory = sqlite3.Row
                return dict(con.execute("SELECT * FROM escrows WHERE escrow_uid=?", (UID,)).fetchone())
        listing = {"listing_id": "listing-1", "offer_resource": {}, "accepted_escrows": []}
        db = SimpleNamespace(db_path=self.path,
            load_negotiation_thread_row=AsyncMock(return_value={"terminal_state": "success", "agreed_price": 123,
                "our_listing_id": "listing-1"}), load_listing=AsyncMock(return_value=listing),
            insert_escrow=AsyncMock(return_value=False), load_escrow=load)
        get_job = AsyncMock(return_value=self.job())
        async def observe(**kwargs):
            return await recovery.reconcile_continuation(self.path, UID, get_job=get_job, now=1100)
        namespace = dict(ProvisionTerms=SimpleNamespace,
            _resolve_duration_seconds=lambda *a: 300, _resolve_compute_resource=lambda *a: {},
            logger=SimpleNamespace(info=lambda *a: None), reconcile_retained_settlement=observe)
        start = actual_function("settlement_jobs.py", "start_settlement_job", namespace)
        config = SimpleNamespace(CHAINS={"base_sepolia": SimpleNamespace(alkahest_address_config_path=None)},
                                 settings=SimpleNamespace(wallet=SimpleNamespace(address="synthetic")))
        verifier = SimpleNamespace(verify_escrow_for_settlement=AsyncMock())
        with patch.dict(sys.modules, {"market_storefront.utils.config": config,
                "market_storefront.utils.escrow_verification": verifier}):
            for _ in range(2):
                result = await start(escrow_uid=UID, negotiation_id="neg-1", ssh_public_key="", sqlite_client=db,
                                    alkahest_client=object(), chain_name="base_sepolia")
                self.assertEqual(result["status"], "provisioning")
                self.assertIn("provisioned_pending_settlement", result["reason"])
            # A competing owner can win the INSERT after the controller's
            # preflight. The actual conflict branch must refuse BEFORE GET.
            db.load_escrow = AsyncMock(return_value={"negotiation_id": "foreign-negotiation"})
            forbidden_observer = AsyncMock()
            namespace["reconcile_retained_settlement"] = forbidden_observer
            with self.assertRaisesRegex(ValueError, "not bound"):
                await start(escrow_uid=UID, negotiation_id="neg-1", ssh_public_key="", sqlite_client=db,
                            alkahest_client=object(), chain_name="base_sepolia")
            forbidden_observer.assert_not_awaited()
        get_job.assert_awaited_once_with(JOB)
        self.assert_fenced()

    async def test_actual_authenticated_status_get_observes_then_returns_unsettled_row(self):
        self.freeze()
        calls = []
        def authenticate(*args): calls.append("auth")
        async def observe(**kwargs):
            self.assertEqual(calls, ["auth", "read", "owner"])
            calls.append("observe")
            return await recovery.reconcile_continuation(self.path, UID,
                get_job=AsyncMock(return_value=self.job()), now=1100)
        async def load(**kwargs):
            calls.append("read")
            with sqlite3.connect(self.path) as con:
                con.row_factory = sqlite3.Row
                return dict(con.execute("SELECT * FROM escrows WHERE escrow_uid=?", (UID,)).fetchone())
        tree = ast.parse((UTILS.parent / "controllers/settle_controller.py").read_text())
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "settle_status")
        node.decorator_list = []
        async def owner(*args): calls.append("owner")
        namespace = dict(Query=lambda **kwargs: None, buyer_auth=SimpleNamespace(_verify=authenticate),
                         require_settlement_owner=owner,
                         SettleStatusResponse=SimpleNamespace)
        source = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
        exec(compile(ast.fix_missing_locations(source), "settle_controller.py", "exec"), namespace)
        with patch("market_storefront.utils.settlement_jobs.reconcile_retained_settlement", new=observe):
            response = await namespace["settle_status"](SimpleNamespace(_db=SimpleNamespace(load_escrow=load)),
                UID, object(), buyer_address="synthetic")
        self.assertEqual(calls, ["auth", "read", "owner", "observe", "read"])
        self.assertEqual(response.status, "provisioning")
        self.assertIn("provisioned_pending_settlement", response.reason)
        self.assertFalse(hasattr(response, "fulfillment_uid"))

    async def test_actual_capability_fulfill_freezes_before_submit_and_skips_signing_tail(self):
        await self._exercise_original_fulfill(timeout=False)

    async def test_actual_capability_timeout_retains_snapshot_and_allocation(self):
        await self._exercise_original_fulfill(timeout=True)

    async def _exercise_original_fulfill(self, timeout):
        reserve = AsyncMock(return_value=dict(allocation_id="alloc-1", resource_id="resource-1",
            vm_host="host-a", attributes={"virtualization_type": "container", "container_image": PARAMS["container_image"]}))
        db = SimpleNamespace(db_path=self.path, reserve_available_compute_vm=reserve,
            update_escrow=AsyncMock(), update_compute_allocation_state=AsyncMock())
        @dataclasses.dataclass
        class DTO:
            vm_host: str
            vm_action: str
            vm_target: str
            provisioning_type: str
            container_image: str
            container_env: dict
            lease_id: str
            max_retries: int
        class Request:
            def __init__(self, **kwargs): self.kwargs = kwargs
            def to_ansible_job_params(self, host):
                return DTO(host, "create", self.kwargs["container_target"], "container",
                           self.kwargs["container_image"], self.kwargs["container_env"], UID, 0)
        async def submit(*args, **kwargs):
            saved = recovery.load_continuation(self.path, UID)
            self.assertEqual(json.loads(saved["snapshot"])["job_id"], JOB)
            self.assert_fenced()
            if timeout:
                raise TimeoutError("synthetic")
            return {"container_name": TARGET}
        provision = AsyncMock(side_effect=submit)
        namespace = dict(uuid=uuid, dataclasses=dataclasses, json=json, logger=SimpleNamespace(info=lambda *a: None),
            get_sqlite_client=lambda: db, extract_compute_from_order=lambda o: {},
            _token_resource_from_accepted_escrow=lambda o: {}, encode_compute_lease=lambda **kw: b"exact-original",
            stage_event=lambda *a, **kw: None, close_stale_compute_listings_after_capacity_change=AsyncMock(return_value=[]),
            _do_provision=provision, CreateContainerRequest=Request)
        fulfill = actual_function("action_executor.py", "fulfill_compute_obligation", namespace)
        with patch("market_storefront.utils.settlement_jobs.reconcile_retained_settlement", new=AsyncMock()) as observe:
            result = await fulfill(client=object(), escrow_uid=UID, ssh_public_key="", oracle_address="synthetic",
                order={"listing_id": "listing-1", "accepted_escrows": [{}]}, listing_id="listing-1",
                duration_seconds=300, container_env=PARAMS["container_env"])
        self.assertEqual(result["status"], "uncertain")
        self.assertIsNone(result["connection_details"])
        reserve.assert_awaited_once(); provision.assert_awaited_once()
        if timeout:
            observe.assert_not_awaited()
            db.update_compute_allocation_state.assert_awaited_once()
        else:
            observe.assert_awaited_once()
        # No signing/lease/date dependencies were supplied: reaching the legacy
        # completion tail would fail this test instead of accidentally signing.
        self.assertEqual(json.loads(recovery.load_continuation(self.path, UID)["snapshot"])["demand_hex"], b"exact-original".hex())


if __name__ == "__main__":
    unittest.main()
