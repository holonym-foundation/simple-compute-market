"""Real SQLite + extracted actual allocator/controller consumers, no signing/network."""
import ast
import asyncio
import dataclasses
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from market_storefront.utils import capacity_holds as holds

UTILS = Path(holds.__file__).parent
BUYER, SELLER = '0x' + '1' * 40, '0x' + '2' * 40
ENV = {'AEX_CAPABILITY_DIRECTORY': '/run/test', 'AEX_ENV': 'staging', 'TEXT': 'café'}
UID = '0x' + 'a' * 64
NEG = 'neg_10000000000040008000000000000001'


def allocator_class():
    tree = ast.parse((UTILS / 'sqlite_client.py').read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'SQLiteClient')
    methods = {'reserve_available_compute_vm', '_requested_gpu_count', '_compute_attrs_from_raw',
        '_compute_candidate_rows', '_compute_resource_matches', '_resource_total_gpu_count',
        '_held_gpu_count', '_sync_compute_resource_state'}
    cls.body = [n for n in cls.body if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in methods)
        or (isinstance(n, ast.Assign) and any(isinstance(t,ast.Name) and t.id=='_COMPUTE_HELD_ALLOCATION_STATES' for t in n.targets))]
    namespace = dict(__package__='market_storefront.utils', asyncio=asyncio, sqlite3=sqlite3,
        datetime=datetime, json=json, time=time, uuid=uuid,
        settings=SimpleNamespace(wallet=SimpleNamespace(address=SELLER)))
    mod = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), cls], type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod), str(UTILS / 'sqlite_client.py'), 'exec'), namespace)
    client = namespace['SQLiteClient']
    return client


class CapacityHoldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = allocator_class()()
        self.db.db_path = str(Path(self.tmp.name) / 'seller.db')
        self.proposal = dict(chain_name='base_sepolia', escrow_address='0x'+'3'*40,
            fields={}, literal_fields={'token':'0x'+'4'*40}, expiration_unix=5000)
        self.binding = dict(schema=1, requestDigest='a'*64, approvalDigest='sha256:'+'b'*64,
            policyRevision='sha256:'+'c'*64, checkoutHoldId=str(uuid.uuid4()), checkoutIntentDigest='d'*64,
            ownerWallet='0x'+'5'*40, buyer=BUYER, seller=SELLER, listingId='listing-1', negotiationId=NEG,
            configDigest=holds.digest(ENV), proposalDigest=holds.digest(self.proposal), amountAtomic='123',
            durationSeconds=300, chainId=84532)
        with closing(sqlite3.connect(self.db.db_path)) as con, con:
            con.executescript('''
              CREATE TABLE negotiation_threads (negotiation_id TEXT PRIMARY KEY, buyer TEXT,
                terminal_state TEXT, our_listing_id TEXT, agreed_price TEXT, agreed_duration_seconds INTEGER,
                buyer_escrow_proposal TEXT);
              CREATE TABLE listings (listing_id TEXT PRIMARY KEY, seller TEXT, paused INTEGER, status TEXT,
                offer_resource TEXT, max_duration_seconds INTEGER, oracle_address TEXT, accepted_escrows TEXT, demands TEXT);
              CREATE TABLE resources (resource_id TEXT PRIMARY KEY, resource_subtype TEXT, unit TEXT,
                state TEXT, value REAL, attributes TEXT, updated_at TEXT);
              CREATE TABLE compute_inventory_pools (pool_id TEXT PRIMARY KEY, resource_type TEXT, status TEXT);
              CREATE TABLE compute_pool_members (pool_id TEXT, member_id TEXT, resource_id TEXT, gpu_count INTEGER, status TEXT);
              CREATE TABLE compute_allocations (allocation_id TEXT PRIMARY KEY, pool_id TEXT, member_id TEXT,
                resource_id TEXT, listing_id TEXT, escrow_uid TEXT, gpu_count INTEGER, state TEXT,
                created_at TEXT, updated_at TEXT);
              CREATE TABLE resource_transition_events (event_id TEXT,resource_id TEXT,event_type TEXT,set_value REAL,
                set_state TEXT,set_attribute_json TEXT,idempotency_key TEXT,occurred_at TEXT);
            ''')
            con.execute('INSERT INTO negotiation_threads VALUES (?,?,?,?,?,?,?)',
                (NEG, BUYER, 'success', 'listing-1', '123', 300, json.dumps(self.proposal)))
            con.execute('INSERT INTO listings VALUES (?,?,?,?,?,?,?,?,?)', ('listing-1', 'http://synthetic.invalid',
                0, 'open', json.dumps({'gpu_model':'CPU','gpu_count':0}), 3600, SELLER, '[]', '[]'))
            con.execute('INSERT INTO resources VALUES (?,?,?,?,?,?,?)', ('resource-1', 'agent', 'count', 'available',
                1, json.dumps({'gpu_model':'CPU','vm_host':'host-a','virtualization_type':'container','sla':99.0,
                    'container_image':'example.invalid/image@sha256:'+'a'*64}), 'now'))
            con.execute("INSERT INTO compute_inventory_pools VALUES ('pool-1','compute.container','active')")
            con.execute("INSERT INTO compute_pool_members VALUES ('pool-1','member-1','resource-1',1,'active')")
            holds.tables(con.cursor())
        self.clock = patch('time.time', return_value=1000)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def reserve(self, binding=None):
        return asyncio.run(self.db.reserve_available_compute_vm(listing_id='listing-1', capacity_hold_binding=binding or self.binding))

    def action(self, receipt, action, **kwargs):
        return holds.transition(self.db, negotiation_id=NEG, hold_id=receipt['holdId'], buyer=BUYER,
            seller=SELLER, action=action, **kwargs)

    def consume(self, receipt, **kwargs):
        return holds.consume(self.db.db_path, hold_id=receipt['holdId'], negotiation_id=NEG,
            escrow_uid=kwargs.pop('escrow_uid', UID), container_env=kwargs.pop('container_env', ENV), seller=SELLER, **kwargs)

    def scalar(self, query):
        with closing(sqlite3.connect(self.db.db_path)) as con:
            return con.execute(query).fetchone()[0]

    def test_real_allocator_single_capacity_and_lost_ack_same_receipt(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            receipts = list(executor.map(lambda _: self.reserve(), range(2)))
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(self.scalar('SELECT count(*) FROM compute_allocations'), 1)
        self.assertEqual(receipts[0]['status'], 'held')
        self.assertNotIn('vm_host', json.dumps(receipts[0]))
        self.assertIsNone(asyncio.run(self.db.reserve_available_compute_vm(listing_id='unrelated')))

    def test_arm_is_one_shot_and_expiry_cancel_never_release_payment_pending(self):
        receipt = self.reserve()
        self.assertTrue(self.action(receipt, 'arm')['paymentAuthorized'])
        self.assertFalse(self.action(receipt, 'arm')['paymentAuthorized'])
        for action in ('status', 'cancel', 'arm'):
            result = self.action(receipt, action, now=9000)
            self.assertEqual(result['status'], 'payment_pending')
            self.assertFalse(result['paymentAuthorized'])
        self.assertEqual(self.scalar('SELECT state FROM compute_allocations'), 'reserved')

    def test_expiry_and_cancel_only_release_unarmed_hold(self):
        receipt = self.reserve()
        result = self.action(receipt, 'arm', now=1300)
        self.assertEqual(result['status'], 'expired')
        self.assertFalse(result['paymentAuthorized'])
        self.assertEqual(self.scalar('SELECT state FROM compute_allocations'), 'released')
        self.assertEqual(self.reserve()['holdId'], receipt['holdId'])
        self.assertEqual(self.reserve()['status'], 'expired')

    def test_allocator_reclaims_only_expired_unarmed_capacity(self):
        receipt = self.reserve()
        with patch('time.time', return_value=1300):
            replacement = asyncio.run(self.db.reserve_available_compute_vm(listing_id='legacy-explicit'))
        self.assertIsNotNone(replacement)
        self.assertNotEqual(replacement['allocation_id'], receipt['allocationId'])
        self.assertEqual(self.scalar('SELECT status FROM buyer_capacity_holds'), 'expired')

    def test_allocator_cannot_reclaim_armed_capacity_after_any_ttl(self):
        receipt = self.reserve()
        self.action(receipt,'arm')
        with patch('time.time', return_value=9000):
            self.assertIsNone(asyncio.run(self.db.reserve_available_compute_vm(listing_id='legacy-explicit')))
        self.assertEqual(self.scalar('SELECT status FROM buyer_capacity_holds'), 'payment_pending')

    def test_two_buyers_compete_for_one_real_slot(self):
        second = {**self.binding, 'negotiationId':'neg_'+uuid.uuid4().hex,'requestDigest':'b'*64,'buyer':'0x'+'6'*40}
        with closing(sqlite3.connect(self.db.db_path)) as con, con:
            con.execute('INSERT INTO negotiation_threads VALUES (?,?,?,?,?,?,?)',
                (second['negotiationId'],second['buyer'],'success','listing-1','123',300,json.dumps(self.proposal)))
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(self.reserve,[self.binding,second]))
        self.assertEqual(sum(result is not None for result in results),1)
        self.assertEqual(self.scalar('SELECT count(*) FROM compute_allocations'),1)

    def test_consume_exact_allocation_after_arm_rejects_changed_escrow_and_config(self):
        receipt = self.reserve()
        with self.assertRaises(ValueError):
            self.consume(receipt)
        self.action(receipt, 'arm')
        with self.assertRaises(ValueError):
            self.consume(receipt, container_env={**ENV, 'changed':'yes'})
        self.assertEqual(self.consume(receipt), receipt['allocationId'])
        self.assertEqual(self.consume(receipt), receipt['allocationId'])
        with self.assertRaises(ValueError):
            self.consume(receipt, escrow_uid='0x'+'b'*64)
        reserved = holds.consumed_reservation(self.db.db_path, escrow_uid=UID, listing_id='listing-1',
            duration_seconds=300, container_env=ENV, seller=SELLER)
        self.assertEqual(reserved['allocation_id'], receipt['allocationId'])
        self.assertEqual(self.scalar('SELECT count(*) FROM compute_allocations'), 1)

    def test_original_identity_terms_config_are_immutable_and_current_before_arm(self):
        receipt = self.reserve()
        for field, value in [('buyer', '0x'+'6'*40), ('amountAtomic','124'),
            ('configDigest','sha256:'+'f'*64), ('checkoutHoldId',str(uuid.uuid4()))]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.reserve({**self.binding, field:value})
        with closing(sqlite3.connect(self.db.db_path)) as con, con:
            con.execute("UPDATE negotiation_threads SET buyer=?", ('0x'+'6'*40,))
        with self.assertRaises(ValueError):
            self.action(receipt, 'arm')

    def test_sql_replace_and_generic_release_cannot_launder_armed_allocation(self):
        receipt = self.reserve()
        self.action(receipt, 'arm')
        with closing(sqlite3.connect(self.db.db_path)) as con, con:
            self.assertEqual(con.execute('PRAGMA recursive_triggers').fetchone()[0], 0)
            for query in ("UPDATE buyer_capacity_holds SET status='cancelled'",
                "INSERT OR REPLACE INTO buyer_capacity_holds SELECT * FROM buyer_capacity_holds",
                "DELETE FROM buyer_capacity_holds", "UPDATE compute_allocations SET state='released'",
                "UPDATE compute_allocations SET resource_id='replacement'",
                "INSERT OR REPLACE INTO compute_allocations SELECT * FROM compute_allocations"):
                with self.subTest(query=query), self.assertRaises(sqlite3.IntegrityError):
                    con.execute(query)

    def test_all_unique_keys_including_escrow_refuse_replace_with_recursive_triggers_off(self):
        held = self.reserve()
        self.action(held,'arm')
        self.consume(held)
        with closing(sqlite3.connect(self.db.db_path)) as con, con:
            self.assertEqual(con.execute('PRAGMA recursive_triggers').fetchone()[0],0)
            row = holds.one(con.cursor(),'SELECT * FROM buyer_capacity_holds WHERE hold_id=?',(held['holdId'],))
            for key in ('hold_id','negotiation_id','request_digest','allocation_id','escrow_uid'):
                candidate = {**row,'hold_id':str(uuid.uuid4()),'negotiation_id':str(uuid.uuid4()),
                    'request_digest':'e'*64,'allocation_id':str(uuid.uuid4()),'status':'held','escrow_uid':None}
                candidate[key] = row[key]
                with self.subTest(unique=key), self.assertRaises(sqlite3.IntegrityError):
                    con.execute('INSERT OR REPLACE INTO buyer_capacity_holds VALUES (?,?,?,?,?,?,?,?,?,?)',tuple(candidate.values()))
            self.assertEqual(con.execute('SELECT count(*) FROM buyer_capacity_holds').fetchone()[0],1)
            self.assertEqual(con.execute('SELECT escrow_uid FROM buyer_capacity_holds').fetchone()[0],UID)

    def test_changed_listing_or_resource_refuses_arm_without_reallocation(self):
        receipt = self.reserve()
        with closing(sqlite3.connect(self.db.db_path)) as con, con:
            con.execute("UPDATE listings SET oracle_address='changed'")
        with self.assertRaises(ValueError):
            self.action(receipt, 'arm')
        self.assertEqual(self.scalar('SELECT count(*) FROM compute_allocations'), 1)

    def test_actual_settlement_consumes_only_after_chain_verification_and_cannot_downgrade(self):
        receipt = self.reserve()
        self.action(receipt, 'arm')
        tree = ast.parse((UTILS / 'settlement_jobs.py').read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'start_settlement_job')
        ns = dict(asyncio=asyncio, ProvisionTerms=SimpleNamespace, EscrowProposal=SimpleNamespace(model_validate=lambda x:x),
            _resolve_duration_seconds=lambda *a:300, _resolve_compute_resource=lambda *a:{},
            logger=SimpleNamespace(info=lambda *a:None), reconcile_retained_settlement=AsyncMock())
        mod = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), fn], type_ignores=[])
        exec(compile(ast.fix_missing_locations(mod), 'settlement_jobs.py', 'exec'), ns)
        thread = dict(terminal_state='success', agreed_price=123, buyer=BUYER, our_listing_id='listing-1')
        db = SimpleNamespace(db_path=self.db.db_path, load_negotiation_thread_row=AsyncMock(return_value=thread),
            load_listing=AsyncMock(return_value={'accepted_escrows':[{'chain_name':'base_sepolia'}]}),
            insert_escrow=AsyncMock(return_value=False), load_escrow=AsyncMock(return_value={'negotiation_id':NEG,'status':'provisioning'}))
        config = SimpleNamespace(CHAINS={'base_sepolia':SimpleNamespace(chain_id=84532,alkahest_address_config_path=None)},
            settings=SimpleNamespace(wallet=SimpleNamespace(address=SELLER)))
        verifier = AsyncMock(side_effect=ValueError('unverified escrow'))
        args = dict(escrow_uid=UID, negotiation_id=NEG, ssh_public_key='', container_env=ENV,
            capacity_hold_id=receipt['holdId'], sqlite_client=db, alkahest_client=object(), chain_name='base_sepolia')
        with patch.dict(sys.modules, {'market_storefront.utils.config':config,
                'market_storefront.utils.escrow_verification':SimpleNamespace(verify_escrow_for_settlement=verifier)}):
            with self.assertRaisesRegex(ValueError, 'unverified'):
                asyncio.run(ns['start_settlement_job'](**args))
            self.assertEqual(self.scalar('SELECT status FROM buyer_capacity_holds'), 'payment_pending')
            db.insert_escrow.assert_not_awaited()
            verifier.side_effect = None
            asyncio.run(ns['start_settlement_job'](**args))
            self.assertEqual(self.scalar('SELECT escrow_uid FROM compute_allocations'), UID)
            with self.assertRaises(ValueError):
                asyncio.run(ns['start_settlement_job'](**{**args,'container_env':{},'capacity_hold_id':None}))
            with self.assertRaises(ValueError):
                asyncio.run(ns['start_settlement_job'](**{**args,'capacity_hold_id':None}))

    def test_actual_fulfill_uses_same_consumed_allocation_then_retains_timeout(self):
        from market_storefront.utils import settlement_continuation
        held = self.reserve()
        self.action(held, 'arm')
        self.consume(held)
        with closing(sqlite3.connect(self.db.db_path)) as con, con:
            con.executescript('''
              ALTER TABLE compute_allocations ADD COLUMN vm_host TEXT;
              ALTER TABLE compute_allocations ADD COLUMN vm_target TEXT;
              CREATE TABLE escrows (escrow_uid TEXT PRIMARY KEY, negotiation_id TEXT, status TEXT,
                fulfillment_uid TEXT, provisioning_job_id TEXT, chain_name TEXT, escrow_address TEXT);
            ''')
            con.execute("INSERT INTO escrows VALUES (?,?,'provisioning',NULL,NULL,'base_sepolia',?)", (UID,NEG,'0x'+'3'*40))
        self.db.reserve_available_compute_vm = AsyncMock(side_effect=AssertionError('second allocation forbidden'))
        self.db.update_compute_allocation_state = AsyncMock()
        self.db.update_escrow = AsyncMock()
        @dataclasses.dataclass
        class Params:
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
                return Params(host,'create',self.kwargs['container_target'],'container',
                    self.kwargs['container_image'],self.kwargs['container_env'],UID,0)
        async def submit(*args, **kwargs):
            saved = settlement_continuation.load_continuation(self.db.db_path, UID)
            self.assertEqual(json.loads(saved['snapshot'])['allocation_id'], held['allocationId'])
            self.assertEqual(self.scalar('SELECT state FROM compute_allocations'), 'held')
            raise TimeoutError('synthetic lost reply')
        provision = AsyncMock(side_effect=submit)
        ns = dict(__package__='market_storefront.utils', asyncio=asyncio, uuid=uuid, dataclasses=dataclasses, json=json,
            get_sqlite_client=lambda:self.db, extract_compute_from_order=lambda _: {},
            _token_resource_from_accepted_escrow=lambda _: {}, encode_compute_lease=lambda **kw:b'exact',
            logger=SimpleNamespace(info=lambda *a:None,error=lambda *a:None,warning=lambda *a:None),
            stage_event=lambda *a,**kw:None, close_stale_compute_listings_after_capacity_change=AsyncMock(return_value=[]),
            _do_provision=provision, CreateContainerRequest=Request)
        tree = ast.parse((UTILS/'action_executor.py').read_text())
        fn = next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='fulfill_compute_obligation')
        mod = ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),fn],type_ignores=[])
        exec(compile(ast.fix_missing_locations(mod),'action_executor.py','exec'),ns)
        result = asyncio.run(ns['fulfill_compute_obligation'](client=object(),escrow_uid=UID,ssh_public_key='',
            oracle_address=SELLER,order={'listing_id':'listing-1','accepted_escrows':[{}]},listing_id='listing-1',
            duration_seconds=300,container_env=ENV))
        self.assertEqual(result['status'],'uncertain')
        provision.assert_awaited_once()
        self.db.reserve_available_compute_vm.assert_not_awaited()
        self.assertEqual(self.scalar('SELECT count(*) FROM compute_allocations'),1)


if __name__ == '__main__':
    unittest.main()
