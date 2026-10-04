"""Buyer-authenticated pre-escrow holds over the existing allocation ledger.

An armed hold never expires or cancels automatically: a missing payment reply is
not proof that no escrow exists. Receipts prove seller bookkeeping only, not an
AEX checkout, controller approval, chain receipt, admission, or live tenant.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import re
import sqlite3
import time
import uuid


FIELDS = frozenset(('schema', 'requestDigest', 'approvalDigest', 'policyRevision',
    'checkoutHoldId', 'checkoutIntentDigest', 'ownerWallet', 'buyer', 'seller',
    'listingId', 'negotiationId', 'configDigest', 'proposalDigest', 'amountAtomic',
    'durationSeconds', 'chainId'))
UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}'


def require(ok):
    if not ok:
        raise ValueError('capacity_hold_unavailable')


def canonical(value):
    def check(item):
        if item is None or type(item) is bool:
            return
        if type(item) is int:
            require(abs(item) <= 2 ** 53 - 1)
        elif isinstance(item, str):
            item.encode('utf-8', errors='strict')
        elif isinstance(item, list):
            for v in item:
                check(v)
        elif isinstance(item, dict):
            for k, v in item.items():
                require(isinstance(k, str) and k.isascii())
                check(v)
        else:
            require(False)
    check(value)
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    return 'sha256:' + hashlib.sha256(canonical(value).encode()).hexdigest()


def validate_binding(value):
    require(isinstance(value, dict) and set(value) == FIELDS)
    require(type(value['schema']) is int and value['schema'] == 1)
    require(type(value['chainId']) is int and value['chainId'] == 84532)
    for name in ('requestDigest', 'checkoutIntentDigest'):
        require(isinstance(value[name], str) and re.fullmatch(r'[0-9a-f]{64}', value[name]))
    for name in ('approvalDigest', 'policyRevision', 'configDigest', 'proposalDigest'):
        require(isinstance(value[name], str) and re.fullmatch(r'sha256:[0-9a-f]{64}', value[name]))
    for name in ('buyer', 'seller', 'ownerWallet'):
        require(isinstance(value[name], str) and re.fullmatch(r'0x[0-9a-f]{40}', value[name])
                and value[name] != '0x' + '0' * 40)
    require(value['buyer'] != value['seller'])
    require(isinstance(value['checkoutHoldId'], str) and re.fullmatch(UUID, value['checkoutHoldId']))
    # Actual sync_negotiation producer is 'neg_' + uuid.uuid4().hex.
    require(isinstance(value['negotiationId'], str)
            and re.fullmatch(r'neg_[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}', value['negotiationId']))
    require(isinstance(value['listingId'], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', value['listingId']))
    amount = value['amountAtomic']
    require(isinstance(amount, str) and re.fullmatch(r'[1-9][0-9]{0,77}', amount)
            and int(amount) < 2 ** 256)
    require(type(value['durationSeconds']) is int and 0 < value['durationSeconds'] <= 3600)
    return value


def tables(cur):
    # Called by schema initialization, never while a caller transaction is open.
    cur.executescript('''
      CREATE TABLE IF NOT EXISTS buyer_capacity_holds (
        hold_id TEXT PRIMARY KEY, negotiation_id TEXT NOT NULL UNIQUE,
        request_digest TEXT NOT NULL UNIQUE, binding TEXT NOT NULL,
        allocation_id TEXT NOT NULL UNIQUE, reservation TEXT NOT NULL,
        created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('held','payment_pending','consumed','expired','cancelled')),
        escrow_uid TEXT UNIQUE,
        FOREIGN KEY(allocation_id) REFERENCES compute_allocations(allocation_id)
      );
      CREATE TRIGGER IF NOT EXISTS buyer_capacity_hold_no_replace BEFORE INSERT ON buyer_capacity_holds
      WHEN NEW.status <> 'held' OR NEW.escrow_uid IS NOT NULL
        OR EXISTS (SELECT 1 FROM escrows WHERE negotiation_id=NEW.negotiation_id)
        OR EXISTS (SELECT 1 FROM buyer_capacity_holds WHERE hold_id=NEW.hold_id
        OR negotiation_id=NEW.negotiation_id OR request_digest=NEW.request_digest
        OR allocation_id=NEW.allocation_id
        OR (NEW.escrow_uid IS NOT NULL AND escrow_uid=NEW.escrow_uid))
      BEGIN SELECT RAISE(ABORT,'immutable capacity hold'); END;
      CREATE TRIGGER IF NOT EXISTS buyer_capacity_hold_no_delete BEFORE DELETE ON buyer_capacity_holds
      BEGIN SELECT RAISE(ABORT,'immutable capacity hold'); END;
      CREATE TRIGGER IF NOT EXISTS buyer_capacity_hold_guard BEFORE UPDATE ON buyer_capacity_holds
      WHEN NEW.hold_id IS NOT OLD.hold_id OR NEW.negotiation_id IS NOT OLD.negotiation_id
        OR NEW.request_digest IS NOT OLD.request_digest OR NEW.binding IS NOT OLD.binding
        OR NEW.allocation_id IS NOT OLD.allocation_id OR NEW.reservation IS NOT OLD.reservation
        OR NEW.created_at IS NOT OLD.created_at OR NEW.expires_at IS NOT OLD.expires_at
        OR NOT ((NEW.status=OLD.status AND NEW.escrow_uid IS OLD.escrow_uid)
          OR (OLD.status='held' AND NEW.status IN ('payment_pending','expired','cancelled') AND NEW.escrow_uid IS NULL)
          OR (OLD.status='payment_pending' AND NEW.status='consumed' AND NEW.escrow_uid IS NOT NULL))
      BEGIN SELECT RAISE(ABORT,'immutable capacity hold'); END;
      CREATE TRIGGER IF NOT EXISTS buyer_capacity_allocation_guard BEFORE UPDATE ON compute_allocations
      WHEN EXISTS (SELECT 1 FROM buyer_capacity_holds h WHERE h.allocation_id=OLD.allocation_id
        AND (NEW.allocation_id IS NOT OLD.allocation_id OR NEW.resource_id IS NOT OLD.resource_id
          OR NEW.pool_id IS NOT OLD.pool_id OR NEW.member_id IS NOT OLD.member_id
          OR NEW.listing_id IS NOT OLD.listing_id OR NEW.gpu_count IS NOT OLD.gpu_count
          OR NEW.escrow_uid IS NOT h.escrow_uid
          OR (h.status IN ('held','payment_pending') AND NEW.state NOT IN ('reserved','held'))))
      BEGIN SELECT RAISE(ABORT,'immutable capacity allocation'); END;
      CREATE TRIGGER IF NOT EXISTS buyer_capacity_escrow_mode_guard BEFORE INSERT ON escrows
      WHEN EXISTS (SELECT 1 FROM buyer_capacity_holds h WHERE h.negotiation_id=NEW.negotiation_id
        AND (NEW.settlement_mode IS NOT 'capability' OR h.status<>'consumed' OR h.escrow_uid IS NOT NEW.escrow_uid))
      BEGIN SELECT RAISE(ABORT,'capacity hold settlement conflict'); END;
      CREATE TRIGGER IF NOT EXISTS buyer_capacity_allocation_no_delete BEFORE DELETE ON compute_allocations
      WHEN EXISTS (SELECT 1 FROM buyer_capacity_holds WHERE allocation_id=OLD.allocation_id)
      BEGIN SELECT RAISE(ABORT,'immutable capacity allocation'); END;
      CREATE TRIGGER IF NOT EXISTS buyer_capacity_allocation_no_replace BEFORE INSERT ON compute_allocations
      WHEN EXISTS (SELECT 1 FROM buyer_capacity_holds WHERE allocation_id=NEW.allocation_id)
      BEGIN SELECT RAISE(ABORT,'immutable capacity allocation'); END;
    ''')


def one(cur, sql, params):
    row = cur.execute(sql, params).fetchone()
    return dict(zip((d[0] for d in cur.description), row)) if row else None


def original(cur, binding, now, *, new=False):
    """Reconstruct authoritative negotiation/listing inside the allocation lock."""
    validate_binding(binding)
    thread = one(cur, 'SELECT * FROM negotiation_threads WHERE negotiation_id=?', (binding['negotiationId'],))
    require(thread and thread['buyer'] == binding['buyer'] and thread['terminal_state'] == 'success'
            and thread['our_listing_id'] == binding['listingId']
            and str(thread['agreed_price']) == binding['amountAtomic']
            and thread['agreed_duration_seconds'] == binding['durationSeconds'])
    proposal = json.loads(thread['buyer_escrow_proposal'])
    require(isinstance(proposal, dict) and proposal.get('chain_name') == 'base_sepolia'
            and digest(proposal) == binding['proposalDigest'])
    expiration = proposal.get('expiration_unix')
    require(type(expiration) is int and expiration > now)
    listing = one(cur, 'SELECT * FROM listings WHERE listing_id=?', (binding['listingId'],))
    # listings.seller is a routing URL, not a wallet. The HTTP/settlement callers
    # separately compare binding.seller with the configured chain signer identity.
    require(listing and not listing['paused'])
    if new:
        require(listing['status'] == 'open')
        require(cur.execute('SELECT 1 FROM escrows WHERE negotiation_id=? LIMIT 1',
                            (binding['negotiationId'],)).fetchone() is None)
    resource = json.loads(listing['offer_resource'])
    require(isinstance(resource, dict) and 'gpu_model' in resource)
    attributes = {key: resource[key] for key in ('pool_id', 'resource_id', 'region', 'gpu_model', 'gpu_count')
                  if resource.get(key) is not None}
    # Never allow a container hold to take a GPU pool merely because filters match.
    attributes['_capacity_hold_container'] = True
    snapshot = {key: listing[key] for key in ('offer_resource', 'max_duration_seconds',
        'seller', 'oracle_address', 'accepted_escrows', 'demands')}
    return attributes, expiration, snapshot


def receipt(row):
    reservation = json.loads(row['reservation'])
    return {'schema': 1, 'holdId': row['hold_id'], 'binding': json.loads(row['binding']),
        'allocationId': row['allocation_id'], 'resourceId': reservation['resource_id'],
        'status': row['status'], 'expiresAt': row['expires_at'], 'escrowUid': row['escrow_uid']}


def existing(cur, binding):
    row = one(cur, 'SELECT * FROM buyer_capacity_holds WHERE negotiation_id=? OR request_digest=?',
              (binding['negotiationId'], binding['requestDigest']))
    if row:
        require(row['binding'] == canonical(binding))
    return row


def insert(cur, binding, reservation, now, expiration):
    row = dict(hold_id=str(uuid.uuid4()), negotiation_id=binding['negotiationId'],
        request_digest=binding['requestDigest'], binding=canonical(binding),
        allocation_id=reservation['allocation_id'], reservation=json.dumps(reservation, sort_keys=True, allow_nan=False),
        created_at=now, expires_at=min(now + 300, expiration), status='held', escrow_uid=None)
    cur.execute('INSERT INTO buyer_capacity_holds VALUES (?,?,?,?,?,?,?,?,?,?)', tuple(row.values()))
    return receipt(row)


def environment_digest(env):
    require(isinstance(env, dict) and 'AEX_CAPABILITY_DIRECTORY' in env
            and all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()))
    return digest(env)


def release_unarmed(cur, db, row, target):
    require(row['status'] == 'held' and row['escrow_uid'] is None and target in ('expired', 'cancelled'))
    cur.execute('UPDATE buyer_capacity_holds SET status=? WHERE hold_id=?', (target, row['hold_id']))
    cur.execute("UPDATE compute_allocations SET state='released' WHERE allocation_id=? AND escrow_uid IS NULL", (row['allocation_id'],))
    require(cur.rowcount == 1)
    stored = json.loads(row['reservation'])
    db._sync_compute_resource_state(cur, resource_id=stored['resource_id'], total_gpu_count=int(stored['value']))
    row['status'] = target


def expire_due(cur, db, now):
    # Opportunistic reclamation under the SAME allocator lock. No timer/service,
    # and never touches payment_pending or consumed allocations, even after TTL.
    cur.execute("SELECT * FROM buyer_capacity_holds WHERE status='held' AND expires_at<=? LIMIT 1000", (now,))
    columns = [d[0] for d in cur.description]
    for raw in cur.fetchall():
        release_unarmed(cur, db, dict(zip(columns, raw)), 'expired')


def check_retained(cur, row, now):
    binding = json.loads(row['binding'])
    _, _, snapshot = original(cur, binding, now)
    stored = json.loads(row['reservation'])
    require(snapshot == stored['_hold_listing'])
    allocation = one(cur, 'SELECT * FROM compute_allocations WHERE allocation_id=?', (row['allocation_id'],))
    require(allocation and allocation['state'] in ('reserved', 'held')
            and allocation['escrow_uid'] == row['escrow_uid'])
    for key in ('pool_id', 'member_id', 'resource_id'):
        require(allocation[key] == stored[key])
    resource = one(cur, 'SELECT attributes FROM resources WHERE resource_id=?', (stored['resource_id'],))
    require(resource and json.loads(resource['attributes']) == stored['attributes'])


def transition(db, *, negotiation_id, hold_id, buyer, seller, action, now=None):
    require(action in ('status', 'arm', 'cancel'))
    with closing(sqlite3.connect(db.db_path)) as con, con:
        cur = con.cursor()
        cur.execute('BEGIN IMMEDIATE')
        # Clock must be sampled after waiting for the writer lock, never before.
        now = int(time.time()) if now is None else now
        row = one(cur, 'SELECT * FROM buyer_capacity_holds WHERE hold_id=? AND negotiation_id=?', (hold_id, negotiation_id))
        require(row and json.loads(row['binding'])['buyer'] == buyer)
        binding = json.loads(row['binding'])
        require(binding['seller'] == seller)
        # Even a status query cannot claim current ownership from a stale receipt.
        thread = one(cur, 'SELECT buyer FROM negotiation_threads WHERE negotiation_id=?', (negotiation_id,))
        require(thread and thread['buyer'] == buyer)
        granted = False
        if row['status'] == 'held':
            target = 'expired' if now >= row['expires_at'] else ('cancelled' if action == 'cancel' else None)
            if action == 'arm' and target is None:
                check_retained(cur, row, now)
                target, granted = 'payment_pending', True
            if target:
                if target in ('expired', 'cancelled'):
                    release_unarmed(cur, db, row, target)
                else:
                    cur.execute('UPDATE buyer_capacity_holds SET status=? WHERE hold_id=?', (target, hold_id))
                    row['status'] = target
        result = receipt(row)
        result['paymentAuthorized'] = granted
        result['retry'] = False
        return result


def has_hold(db_path, negotiation_id):
    with closing(sqlite3.connect(db_path)) as con:
        return con.execute('SELECT 1 FROM buyer_capacity_holds WHERE negotiation_id=?', (negotiation_id,)).fetchone() is not None


def consume(db_path, *, hold_id, negotiation_id, escrow_uid, container_env, seller, now=None):
    require(isinstance(escrow_uid, str) and re.fullmatch(r'0x[0-9a-f]{64}', escrow_uid)
            and escrow_uid != '0x' + '0' * 64)
    with closing(sqlite3.connect(db_path)) as con, con:
        cur = con.cursor()
        cur.execute('BEGIN IMMEDIATE')
        now = int(time.time()) if now is None else now
        row = one(cur, 'SELECT * FROM buyer_capacity_holds WHERE hold_id=? AND negotiation_id=?', (hold_id, negotiation_id))
        require(row and row['status'] in ('payment_pending', 'consumed'))
        binding = json.loads(row['binding'])
        require(binding['seller'] == seller and environment_digest(container_env) == binding['configDigest'])
        if row['status'] == 'consumed':
            require(row['escrow_uid'] == escrow_uid)
        else:
            check_retained(cur, row, now)
            cur.execute("UPDATE buyer_capacity_holds SET status='consumed',escrow_uid=? WHERE hold_id=?", (escrow_uid, hold_id))
            cur.execute('UPDATE compute_allocations SET escrow_uid=? WHERE allocation_id=? AND escrow_uid IS NULL', (escrow_uid, row['allocation_id']))
            require(cur.rowcount == 1)
        return row['allocation_id']


def consumed_reservation(db_path, *, escrow_uid, listing_id, duration_seconds, container_env, seller):
    with closing(sqlite3.connect(db_path)) as con:
        cur = con.cursor()
        row = one(cur, "SELECT * FROM buyer_capacity_holds WHERE escrow_uid=? AND status='consumed'", (escrow_uid,))
        require(row)
        binding = json.loads(row['binding'])
        require(binding['seller'] == seller and binding['listingId'] == listing_id and binding['durationSeconds'] == duration_seconds
                and binding['configDigest'] == environment_digest(container_env))
        check_retained(cur, row, int(time.time()))
        allocation = one(cur, 'SELECT * FROM compute_allocations WHERE allocation_id=?', (row['allocation_id'],))
        require(allocation and allocation['escrow_uid'] == escrow_uid and allocation['state'] in ('reserved', 'held'))
        return json.loads(row['reservation'])
