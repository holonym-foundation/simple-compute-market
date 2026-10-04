"""Real temporary SQLite and actual consumer bodies; no wallet or network imports."""
import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

from market_storefront.utils import capability_settlement_handoff as handoff
from market_storefront.utils import settlement_continuation as recovery

UID = '0x' + 'ab' * 32
JOB = str(uuid.uuid5(uuid.NAMESPACE_URL, 'scm-container-lease:' + UID))
OP = 'sha256:' + '1' * 64
TX = '0x' + '2' * 64
ADMISSION = '10000000-0000-4000-8000-000000000001'
UTILS = Path(handoff.__file__).parent


def wire(operation=OP, outcome='submitted', tx=TX, nonce='0'):
    return json.dumps({'ok': True, 'result': {'schema': 1, 'operationId': operation,
        'outcome': outcome, 'txHash': tx, 'nonce': nonce}}).encode()


def actual_method(filename, class_name, name, namespace):
    tree = ast.parse(filename.read_text())
    owner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    method = next(n for n in owner.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), method], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(filename), 'exec'), namespace)
    return namespace[name]


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name).resolve() / 'seller.db'
        with sqlite3.connect(self.path) as con:
            con.executescript('''
              CREATE TABLE escrows (escrow_uid TEXT PRIMARY KEY, negotiation_id TEXT,
                status TEXT, fulfillment_uid TEXT, provisioning_job_id TEXT,
                chain_name TEXT, escrow_address TEXT, reason TEXT, settlement_mode TEXT);
              CREATE TABLE compute_allocations (allocation_id TEXT PRIMARY KEY,
                escrow_uid TEXT, resource_id TEXT, listing_id TEXT, state TEXT, vm_host TEXT, vm_target TEXT);
              CREATE TABLE negotiation_threads (negotiation_id TEXT PRIMARY KEY, our_listing_id TEXT);
            ''')
            con.execute("INSERT INTO escrows VALUES (?, 'neg-1', 'provisioning', NULL, NULL, 'base_sepolia', 'synthetic', NULL, 'capability')", (UID,))
            con.execute("INSERT INTO compute_allocations VALUES ('alloc-1', ?, 'resource-1', 'listing-1', 'reserved', NULL, NULL)", (UID,))
            con.execute("INSERT INTO negotiation_threads VALUES ('neg-1','listing-1')")
        con.close()
        params = dict(vm_host='host-a', vm_action='create', vm_target='tenant-' + uuid.UUID(JOB).hex,
            provisioning_type='container', container_image='example.invalid/image@sha256:' + 'a' * 64,
            container_env={'AEX_CAPABILITY_DIRECTORY': '/run/synthetic', 'AEX_ENV': 'staging'},
            lease_id=UID, max_retries=0)
        recovery.freeze_continuation(self.path, escrow_uid=UID, allocation_id='alloc-1', resource_id='resource-1',
            listing_id='listing-1', order={'listing_id': 'listing-1', 'price': 123}, demand_hex='deadbeef',
            duration_seconds=300, params=params, now=1000)
        job = dict(job_id=JOB, escrow_uid=UID, status='succeeded', retry_count=0, max_retries=0,
            next_retry_at=None, error=None, params={**params, '_container_preparation': {
                'version': 1, 'request_digest': recovery.request_digest(params), 'expires_at': 4600,
                'admission_id': ADMISSION}}, result={'container_name': params['vm_target'], 'container_id': 'd' * 64, 'running': True})
        asyncio.run(recovery.reconcile_continuation(self.path, UID, get_job=AsyncMock(return_value=job), now=1100))
        row = recovery.load_continuation(self.path, UID)
        self.approval = dict(schema=1, databasePath=str(handoff.DATABASE), databaseUid=1001, databaseGid=1001,
            stage='fulfill', operationId=OP, escrowUid=UID, jobId=JOB, snapshotDigest=handoff.digest(json.loads(row['snapshot'])),
            requestDigest=recovery.request_digest(params), admissionId=ADMISSION,
            resultDigest='sha256:' + json.loads(row['observation'])['result_sha256'],
            notBefore=1100, expiresAt=1250, fulfillment=None)

    def run_handoff(self, **kwargs):
        return handoff.handoff(self.path, kwargs.pop('approval', self.approval), mode=kwargs.pop('mode', 'dispatch'),
            now=kwargs.pop('now', 1150), **kwargs)

    def test_committed_intent_precedes_fixed_reference_and_restart_never_redispatches(self):
        def invoke(mode, reference):
            self.assertEqual((mode, reference), ('dispatch', {'schema': 1, 'operationId': OP}))
            with handoff.connection(self.path, readonly=True) as con:
                self.assertEqual(con.execute('SELECT operation_id FROM capability_operation_handoffs').fetchone()[0], OP)
            return wire()
        call = Mock(side_effect=invoke)
        first = self.run_handoff(invoke=call)
        self.assertEqual(first['status'], 'submitted')
        self.assertFalse(first['settlementVerified'])
        self.assertEqual(self.run_handoff(invoke=call)['status'], 'reconciliation-required')
        call.assert_called_once()
        with handoff.connection(self.path, readonly=True) as con:
            self.assertEqual(tuple(con.execute('SELECT status,fulfillment_uid FROM escrows').fetchone()), ('provisioning', None))
            self.assertEqual(con.execute('SELECT state FROM compute_allocations').fetchone()[0], 'held')

    def test_competing_dispatches_launch_at_most_once(self):
        call = Mock(return_value=wire())
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(lambda _: self.run_handoff(invoke=call), range(2)))
        call.assert_called_once()
        self.assertEqual({r['status'] for r in result}, {'submitted', 'reconciliation-required'})

    def test_lost_ack_observe_after_expiry_never_retries_dispatch(self):
        call = Mock(side_effect=TimeoutError('must not echo synthetic detail'))
        self.assertEqual(self.run_handoff(invoke=call)['status'], 'uncertain')
        self.assertEqual(self.run_handoff(invoke=call)['status'], 'reconciliation-required')
        call.assert_called_once()
        observe = Mock(return_value=wire(outcome='uncertain'))
        result = self.run_handoff(mode='observe', now=9999, invoke=observe)
        self.assertEqual((result['status'], result['txHash']), ('uncertain', TX))
        self.assertEqual(observe.call_args.args[0], 'observe')

    def test_observe_cannot_create_intent_or_report_submitted(self):
        call = Mock(return_value=wire())
        with self.assertRaises(ValueError):
            self.run_handoff(mode='observe', invoke=call)
        call.assert_not_called()
        self.run_handoff(invoke=call)
        result = self.run_handoff(mode='observe', invoke=call)
        self.assertEqual((result['status'], result['txHash']), ('uncertain', None))

    def test_lost_commit_ack_or_failed_post_result_write_cannot_open_retry(self):
        original = handoff.database_action
        def lost(path, action, *args):
            value = original(path, action, *args)
            if action == 'prepare':
                raise OSError('synthetic lost committed acknowledgement')
            return value
        call = Mock(return_value=wire())
        with patch.object(handoff, 'database_action', side_effect=lost):
            with self.assertRaises(OSError):
                self.run_handoff(invoke=call)
        call.assert_not_called()
        self.assertEqual(self.run_handoff(invoke=call)['status'], 'reconciliation-required')

    def test_lost_result_record_ack_preserves_intent_and_allows_only_observe(self):
        original = handoff.database_action
        def lost(path, action, *args):
            value = original(path, action, *args)
            if action == 'record':
                raise OSError('synthetic lost committed acknowledgement')
            return value
        call = Mock(return_value=wire())
        with patch.object(handoff, 'database_action', side_effect=lost):
            with self.assertRaises(OSError):
                self.run_handoff(invoke=call)
        self.assertEqual(self.run_handoff(invoke=call)['status'], 'reconciliation-required')
        call.assert_called_once()

    def test_actual_fork_worker_bounded_reply_and_lost_ack(self):
        # Fork/pipe/SQLite execute for real under the TEST USER. Mock only the
        # root/ownership/drop boundary; never change local credentials or chown.
        identity = {'uid': 1001, 'gid': 1001, 'inode': (1, 2)}
        with patch.object(handoff, 'DATABASE', self.path), \
             patch.object(handoff.os, 'geteuid', return_value=0), \
             patch.object(handoff, 'check_database', return_value=(1, 2)), \
             patch.object(handoff, 'drop_database_privileges'):
            self.assertTrue(handoff.database_worker(self.path, identity, 'identity', self.approval, 'dispatch', 1150))
            with patch.object(handoff.os, 'write', side_effect=OSError('lost committed worker ACK')):
                with self.assertRaises(ValueError):
                    handoff.database_worker(self.path, identity, 'prepare', self.approval, 'dispatch', 1150)
        call = Mock(return_value=wire())
        self.assertEqual(self.run_handoff(invoke=call)['status'], 'reconciliation-required')
        call.assert_not_called()

    def test_worker_drop_failure_prevents_database_work(self):
        identity = {'uid': 1001, 'gid': 1001, 'inode': (1, 2)}
        with patch.object(handoff, 'DATABASE', self.path), \
             patch.object(handoff.os, 'geteuid', return_value=0), \
             patch.object(handoff, 'check_database', return_value=(1, 2)), \
             patch.object(handoff, 'drop_database_privileges', side_effect=PermissionError):
            with self.assertRaises(ValueError):
                handoff.database_worker(self.path, identity, 'prepare', self.approval, 'dispatch', 1150)
        with handoff.connection(self.path, readonly=True) as con:
            self.assertIsNone(con.execute("SELECT 1 FROM sqlite_master WHERE name='capability_operation_handoffs'").fetchone())

    def test_exact_approval_no_historical_path_unknown_identity_or_caller_payload(self):
        for changes in ({'databasePath': '/var/lib/aex-scm/seller/agent.db'}, {'databaseUid': 0},
                        {'databaseGid': True}, {'databaseUid': 2147483648}, {'argv': []},
                        {'chainId': 8453}, {'stage': 'collect'}, {'fulfillment': {}},
                        {'jobId': 'invalid'}, {'expiresAt': 5000}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                handoff.validate_approval({**self.approval, **changes})

    def test_recheck_changed_after_intent_does_not_invoke_or_allow_retry(self):
        call = Mock(return_value=wire())
        result = self.run_handoff(invoke=call, recheck=lambda: {**self.approval, 'resultDigest': 'sha256:' + 'f' * 64})
        self.assertEqual(result['status'], 'uncertain')
        call.assert_not_called()
        self.assertEqual(self.run_handoff(invoke=call)['status'], 'reconciliation-required')

    def test_changed_approval_original_tuple_window_or_live_allocation_refuses(self):
        call = Mock(return_value=wire())
        for changes in ({'snapshotDigest': 'sha256:' + 'f' * 64}, {'requestDigest': 'sha256:' + 'f' * 64},
                        {'admissionId': '20000000-0000-4000-8000-000000000001'}, {'resultDigest': 'sha256:' + 'f' * 64},
                        {'expiresAt': 1400}, {'notBefore': 1200}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.run_handoff(approval={**self.approval, **changes}, invoke=call)
        with handoff.connection(self.path) as con:
            con.execute("UPDATE compute_allocations SET state='released'")
        with self.assertRaises(ValueError):
            self.run_handoff(invoke=call)
        call.assert_not_called()

    def test_claim_requires_exact_prior_fulfillment_and_remains_unsettled(self):
        claim_op = 'sha256:' + '3' * 64
        claim = {**self.approval, 'stage': 'claim', 'operationId': claim_op, 'fulfillment': {
            'operationId': OP, 'uid': '0x' + '4' * 64, 'txHash': TX,
            'receiptDigest': 'sha256:' + '5' * 64, 'canonicalConfirmationDigest': 'sha256:' + '6' * 64}}
        call = Mock(return_value=wire(claim_op))
        with self.assertRaises(ValueError):
            self.run_handoff(approval=claim, invoke=call)
        call.assert_not_called()
        self.run_handoff(invoke=Mock(return_value=wire()))
        with self.assertRaises(ValueError):
            self.run_handoff(approval={**claim, 'fulfillment': {**claim['fulfillment'], 'txHash': '0x' + 'f' * 64}}, invoke=call)
        self.assertEqual(self.run_handoff(approval=claim, invoke=call)['status'], 'submitted')
        with handoff.connection(self.path) as con:
            self.assertEqual(con.execute('SELECT fulfillment_uid FROM escrows').fetchone()[0], None)
            con.execute('PRAGMA recursive_triggers=OFF')
            for table in ('capability_operation_handoffs', 'capability_operation_observations'):
                with self.assertRaises(sqlite3.IntegrityError):
                    con.execute('DELETE FROM ' + table)
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable'):
                    con.execute('INSERT OR REPLACE INTO ' + table + ' SELECT * FROM ' + table + ' LIMIT 1')
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable'):
                con.execute('INSERT OR REPLACE INTO capability_operation_handoffs VALUES(?,?,?,?,?)',
                            (UID,'fulfill','sha256:'+'f'*64,'sha256:'+'f'*64,9999))

    def test_result_parser_failures_are_uncertain_and_never_repeat_dispatch(self):
        call = Mock(return_value=b'{"ok":true,"result":{"private":"do not echo"}}')
        result = self.run_handoff(invoke=call)
        self.assertEqual((result['status'], result['txHash']), ('uncertain', None))
        self.assertNotIn('private', json.dumps(result))
        self.run_handoff(invoke=call)
        call.assert_called_once()

    def test_actual_claim_consumer_blocks_capability_unknown_and_missing_state(self):
        claim = actual_method(UTILS.parent / 'services/listing_service.py', 'ListingService', 'claim',
            dict(datetime=datetime, stage_event=lambda *a, **k: None))
        collect = AsyncMock(return_value='synthetic')
        chain = SimpleNamespace(erc20=SimpleNamespace(escrow=SimpleNamespace(non_tierable=SimpleNamespace(collect=collect))))
        owner = SimpleNamespace(_db=SimpleNamespace(db_path=self.path, update_listing=AsyncMock()),
            _alkahest_available=True, _resolve_chain_for_escrow=AsyncMock(return_value=('base_sepolia', chain)))
        payload = SimpleNamespace(escrow_uid=UID, fulfillment_uid='0x' + 'b' * 64)
        self.assertEqual(asyncio.run(claim(owner, 'listing-1', payload))[0], 409)
        # Positive legacy mode but contradictory retained capability proof also refuses.
        with handoff.connection(self.path) as con:
            con.execute("UPDATE escrows SET settlement_mode='legacy',provisioning_job_id=NULL")
        self.assertEqual(asyncio.run(claim(owner, 'listing-1', payload))[0], 409)
        with handoff.connection(self.path) as con:
            con.execute('DROP TABLE capability_settlement_continuations')
            con.execute('UPDATE escrows SET settlement_mode=NULL')
        self.assertEqual(asyncio.run(claim(owner, 'listing-1', payload))[0], 409)
        collect.assert_not_awaited()
        owner._resolve_chain_for_escrow.assert_not_awaited()
        owner._db.update_listing.assert_not_awaited()
        with handoff.connection(self.path) as con:
            con.execute("UPDATE escrows SET settlement_mode='legacy'")
        self.assertEqual(asyncio.run(claim(owner, 'wrong-listing', payload))[0], 409)
        self.assertEqual(asyncio.run(claim(owner, 'listing-1', payload))[0], 200)
        collect.assert_awaited_once_with(UID, payload.fulfillment_uid)
        owner._db.update_listing.assert_awaited_once()
        owner._db.db_path = Path(self.tmp.name) / 'missing.db'
        self.assertEqual(asyncio.run(claim(owner, 'listing-1', payload))[0], 409)
        self.assertFalse(owner._db.db_path.exists())


class ContractTests(unittest.TestCase):
    def test_actual_initial_dispatch_commits_original_mode_before_background(self):
        tree = ast.parse((UTILS / 'settlement_jobs.py').read_text())
        start_node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'start_settlement_job')
        namespace = dict(ProvisionTerms=SimpleNamespace, _resolve_duration_seconds=lambda *a: 300,
            _resolve_compute_resource=lambda *a: {}, _run_settlement_job_bg=AsyncMock(),
            logger=SimpleNamespace(info=lambda *a: None))
        module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), start_node], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), 'settlement_jobs.py', 'exec'), namespace)
        insert = actual_method(UTILS / 'sqlite_client.py', 'SQLiteClient', 'insert_escrow',
                               dict(asyncio=asyncio, sqlite3=sqlite3, datetime=datetime))
        source = ast.parse((UTILS / 'sqlite_client.py').read_text())
        schema = next(n.value for n in ast.walk(source) if isinstance(n, ast.Constant)
                      and isinstance(n.value, str) and 'CREATE TABLE IF NOT EXISTS escrows (' in n.value)
        config = SimpleNamespace(CHAINS={'base_sepolia': SimpleNamespace(alkahest_address_config_path=None)},
                                 settings=SimpleNamespace(wallet=SimpleNamespace(address='synthetic')))
        verifier = SimpleNamespace(verify_escrow_for_settlement=AsyncMock())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'seller.db'
            with sqlite3.connect(path) as con:
                con.execute(schema)
            con.close()
            db = SimpleNamespace(db_path=path,
                load_negotiation_thread_row=AsyncMock(return_value={'terminal_state': 'success', 'agreed_price': 1, 'our_listing_id': 'listing'}),
                load_listing=AsyncMock(return_value={'listing_id': 'listing'}))
            db.insert_escrow = lambda **kwargs: insert(db, **kwargs)
            expected = []
            def schedule(coroutine):
                coroutine.close()
                with handoff.connection(path, readonly=True) as committed:
                    mode = committed.execute('SELECT settlement_mode FROM escrows WHERE escrow_uid=?', (expected[-1][0],)).fetchone()[0]
                    self.assertEqual(mode, expected[-1][1])
            namespace['asyncio'] = SimpleNamespace(create_task=schedule)
            with patch.dict(sys.modules, {'market_storefront.utils.config': config,
                    'market_storefront.utils.escrow_verification': verifier}):
                for uid, env, mode in [('cap', {'AEX_CAPABILITY_DIRECTORY': '/run/exact'}, 'capability'),
                                       ('legacy', {}, 'legacy')]:
                    expected.append((uid, mode))
                    asyncio.run(namespace['start_settlement_job'](escrow_uid=uid, negotiation_id='neg', ssh_public_key='',
                        container_env=env, sqlite_client=db, alkahest_client=object(), chain_name='base_sepolia'))
            self.assertEqual(namespace['_run_settlement_job_bg'].call_count, 2)

    def test_strict_scope_and_public_result(self):
        raw = (handoff.canonical({'schema': 1, 'operationId': OP}) + '\n').encode()
        self.assertEqual(handoff.scope(raw)['operationId'], OP)
        for bad in (raw[:-1], raw + raw, raw.replace(b'"schema":1', b'"schema":1,"schema":1'),
                    b'{}', b'x' * 257):
            with self.subTest(raw=bad), self.assertRaises((ValueError, TypeError)):
                handoff.scope(bad)
        self.assertEqual(handoff.parse_result(wire(), OP)['txHash'], TX)
        for bad in (wire() + wire(), b'progress\n' + wire(), b'x' * 4097,
                    wire().replace(b'"ok": true', b'"ok": true, "ok": true'),
                    wire(operation='sha256:' + '9' * 64), wire(tx='0x' + '0' * 64),
                    wire(tx=None), wire(nonce='01'), wire(outcome='settled')):
            with self.subTest(raw=bad), self.assertRaises((ValueError, TypeError)):
                handoff.parse_result(bad, OP)

    def test_child_privilege_drop_is_permanent_and_clears_groups_before_uid(self):
        calls = []
        with patch.object(handoff.os, 'setgroups', side_effect=lambda value: calls.append(('groups', value))), \
             patch.object(handoff.os, 'setgid', side_effect=lambda value: calls.append(('gid', value))), \
             patch.object(handoff.os, 'setuid', side_effect=lambda value: calls.append(('uid', value))), \
             patch.object(handoff.os, 'getuid', return_value=1001), patch.object(handoff.os, 'geteuid', return_value=1001), \
             patch.object(handoff.os, 'getgid', return_value=1002), patch.object(handoff.os, 'getegid', return_value=1002), \
             patch.object(handoff.os, 'getgroups', return_value=[]):
            handoff.drop_database_privileges({'uid': 1001, 'gid': 1002})
        self.assertEqual(calls, [('groups', []), ('gid', 1002), ('uid', 1001)])
        with patch.object(handoff.os, 'setgroups', side_effect=PermissionError), patch.object(handoff.os, 'setuid') as uid:
            with self.assertRaises(PermissionError):
                handoff.drop_database_privileges({'uid': 1001, 'gid': 1002})
            uid.assert_not_called()

    def test_actual_sql_original_mode_guards_block_laundering_without_backfill(self):
        tree = ast.parse((UTILS / 'sqlite_client.py').read_text())
        sql = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
               and 'CREATE TRIGGER IF NOT EXISTS escrow_settlement_mode_' in node.value]
        self.assertEqual(len(sql), 3)
        with sqlite3.connect(':memory:') as con:
            con.execute('CREATE TABLE escrows(escrow_uid TEXT PRIMARY KEY,negotiation_id TEXT,settlement_mode TEXT,status TEXT)')
            con.execute("INSERT INTO escrows VALUES ('old','neg',NULL,'provisioning')")
            for statement in sql:
                con.execute(statement)
            con.execute("INSERT INTO escrows VALUES ('fresh','neg','capability','provisioning')")
            for statement in ("UPDATE escrows SET settlement_mode='legacy' WHERE escrow_uid='old'",
                              "UPDATE escrows SET settlement_mode='legacy' WHERE escrow_uid='fresh'",
                              "UPDATE escrows SET escrow_uid='other' WHERE escrow_uid='fresh'",
                              "DELETE FROM escrows WHERE escrow_uid='fresh'",
                              "INSERT OR REPLACE INTO escrows VALUES ('fresh','neg','legacy','provisioning')"):
                with self.subTest(sql=statement), self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable'):
                    con.execute(statement)
            con.execute("UPDATE escrows SET status='uncertain' WHERE escrow_uid='fresh'")
            self.assertIsNone(con.execute("SELECT settlement_mode FROM escrows WHERE escrow_uid='old'").fetchone()[0])


if __name__ == '__main__':
    unittest.main()
