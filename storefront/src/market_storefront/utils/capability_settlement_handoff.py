"""Manual reference-only handoff of one retained capability settlement stage.

Never imported by HTTP polling. A separately reviewed root approval and host
launcher are REQUIRED and are not produced/installed here. Submitted is not
settled; no escrow, allocation, fulfillment or listing completion is written.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import sqlite3
import stat
import subprocess
import sys
import threading
import time

from .settlement_continuation import canonical, _bound_rows

APPROVAL = Path('/run/aex-scm-authority/continuation.json')
DATABASE = Path('/var/lib/aex-scm/fresh-staging/seller/agent.db')
COMMANDS = {'dispatch': '/usr/local/lib/aex-scm-action-host/dispatch',
            'observe': '/usr/local/lib/aex-scm-action-host/observe'}
HOST_TIMEOUT_SECONDS = 510
DIGEST = r'sha256:[0-9a-f]{64}'
HASH = r'0x[0-9a-f]{64}'
UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[45][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}'


def require(value):
    if not value:
        raise ValueError('capability_handoff_unavailable')


def exact(value, keys):
    require(type(value) is dict and set(value) == set(keys))


def matches(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def digest(value):
    return 'sha256:' + hashlib.sha256(canonical(value).encode()).hexdigest()


def decode(raw, maximum):
    require(isinstance(raw, bytes) and 0 < len(raw) <= maximum)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result)
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: require(False))


def scope(raw):
    value = decode(raw, 256)
    exact(value, ('schema', 'operationId'))
    require(type(value['schema']) is int and value['schema'] == 1 and matches(value['operationId'], DIGEST))
    require(raw == (canonical(value)+'\n').encode())
    return value


def validate_approval(value):
    exact(value, ('schema','databasePath','databaseUid','databaseGid','stage','operationId','escrowUid','jobId','snapshotDigest',
                  'requestDigest','admissionId','resultDigest','notBefore','expiresAt','fulfillment'))
    require(type(value['schema']) is int and value['schema'] == 1 and value['databasePath'] == str(DATABASE))
    require(all(type(value[k]) is int and 0 < value[k] <= 2147483647 for k in ('databaseUid','databaseGid')))
    require(value['stage'] in ('fulfill','claim') and matches(value['escrowUid'], HASH)
            and value['escrowUid'] != '0x'+'0'*64)
    require(all(matches(value[k], DIGEST) for k in ('operationId','snapshotDigest','requestDigest','resultDigest')))
    require(matches(value['jobId'], UUID) and matches(value['admissionId'], UUID)
            and value['admissionId'][14] == '4')
    require(all(type(value[k]) is int and 0 <= value[k] <= 9007199254740991 for k in ('notBefore','expiresAt'))
            and 0 < value['expiresAt']-value['notBefore'] <= 3600)
    if value['stage'] == 'fulfill':
        require(value['fulfillment'] is None)
    else:
        f = value['fulfillment']
        exact(f, ('operationId','uid','txHash','receiptDigest','canonicalConfirmationDigest'))
        require(all(matches(f[k], DIGEST) for k in ('operationId','receiptDigest','canonicalConfirmationDigest'))
                and all(matches(f[k], HASH) and f[k] != '0x'+'0'*64 for k in ('uid','txHash'))
                and f['operationId'] != value['operationId'])
    return value


def protected_read(path, maximum, *, executable=False):
    require(path.is_absolute() and path.resolve(strict=True) == path)
    for parent in path.parents:
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as file:
        before = os.fstat(file.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1
                and stat.S_IMODE(before.st_mode) == (0o555 if executable else 0o444)
                and 0 < before.st_size <= maximum)
        raw = file.read(maximum+1)
        after = os.fstat(file.fileno())
        fields = ('st_dev','st_ino','st_mode','st_uid','st_gid','st_nlink','st_size','st_mtime_ns','st_ctime_ns')
        current = path.lstat()
        require(len(raw) == before.st_size and all(getattr(before,k) == getattr(after,k) == getattr(current,k) for k in fields))
    return raw


def read_approval():
    raw = protected_read(APPROVAL, 16384)
    value = validate_approval(decode(raw, 16384))
    require(raw == (canonical(value)+'\n').encode())
    return value


def check_database(path, uid, gid):
    require(path.is_absolute() and path.resolve(strict=True) == path)
    for parent in path.parents:
        info = parent.lstat()
        if parent == path.parent:
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == uid and info.st_gid == gid
                    and stat.S_IMODE(info.st_mode) == 0o700)
        else:
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0
                    and stat.S_IMODE(info.st_mode) in (0o711, 0o755))
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == uid and info.st_gid == gid
                and stat.S_IMODE(info.st_mode) == 0o600 and os.read(descriptor,16) == b'SQLite format 3\x00')
        for suffix in ('-wal','-shm','-journal'):
            side = Path(str(path)+suffix)
            if not os.path.lexists(side):continue
            side_info = side.lstat()
            require(stat.S_ISREG(side_info.st_mode) and side_info.st_nlink == 1
                    and side_info.st_uid == uid and side_info.st_gid == gid and not side_info.st_mode & 0o077)
            side_fd = os.open(side, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                opened = os.fstat(side_fd)
                current = side.lstat()
                fields = ('st_dev','st_ino','st_mode','st_uid','st_gid','st_nlink')
                require(all(getattr(side_info,k) == getattr(opened,k) == getattr(current,k) for k in fields))
            finally:
                os.close(side_fd)
        return (info.st_dev, info.st_ino)
    finally:
        os.close(descriptor)


@contextmanager
def connection(path, readonly=False):
    # mode=rw never creates a missing database. Production checks ancestry and
    # inode before and after connect; tests use only their explicit temporary DB.
    con = sqlite3.connect(Path(path).as_uri()+('?mode=ro' if readonly else '?mode=rw'), uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        if not readonly:
            con.execute('PRAGMA synchronous=FULL')
        with con:
            yield con
    finally:
        con.close()


def bound(con, approval, now):
    row = con.execute('SELECT * FROM capability_settlement_continuations WHERE escrow_uid=?',
                      (approval['escrowUid'],)).fetchone()
    require(row is not None and row['state'] == 'provisioned_pending_settlement')
    snapshot, observation = json.loads(row['snapshot']), json.loads(row['observation'])
    _bound_rows(con, snapshot)
    escrow = con.execute('SELECT settlement_mode FROM escrows WHERE escrow_uid=?', (approval['escrowUid'],)).fetchone()
    require(escrow['settlement_mode'] == 'capability')
    require(digest(snapshot) == approval['snapshotDigest'] and snapshot['escrow_uid'] == approval['escrowUid']
            and snapshot['job_id'] == approval['jobId'] and snapshot['request_digest'] == approval['requestDigest']
            and snapshot['chain_name'] == 'base_sepolia' and now < snapshot['deadline']
            and snapshot['started_at'] <= approval['notBefore'] < approval['expiresAt'] <= snapshot['deadline'])
    exact(observation, ('job_id','request_digest','admission_id','result_sha256','observed_at','settlement_verified'))
    require(observation['job_id'] == approval['jobId'] and observation['request_digest'] == approval['requestDigest']
            and observation['admission_id'] == approval['admissionId']
            and 'sha256:'+observation['result_sha256'] == approval['resultDigest']
            and observation['settlement_verified'] is False and type(observation['observed_at']) is int
            and snapshot['started_at'] <= observation['observed_at'] <= now)


def tables(con):
    con.execute('''CREATE TABLE IF NOT EXISTS capability_operation_handoffs (
      escrow_uid TEXT NOT NULL, stage TEXT NOT NULL, operation_id TEXT NOT NULL UNIQUE,
      approval_digest TEXT NOT NULL, intent_at INTEGER NOT NULL, PRIMARY KEY(escrow_uid,stage))''')
    con.execute('''CREATE TABLE IF NOT EXISTS capability_operation_observations (
      operation_id TEXT NOT NULL, result_digest TEXT NOT NULL, result TEXT NOT NULL,
      observed_at INTEGER NOT NULL, PRIMARY KEY(operation_id,result_digest))''')
    for table in ('capability_operation_handoffs','capability_operation_observations'):
        for action in ('UPDATE','DELETE'):
            con.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()}_immutable
                BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'handoff evidence is immutable'); END''')
    con.execute('''CREATE TRIGGER IF NOT EXISTS capability_operation_handoffs_no_replace
        BEFORE INSERT ON capability_operation_handoffs WHEN EXISTS (
          SELECT 1 FROM capability_operation_handoffs WHERE operation_id=NEW.operation_id
            OR (escrow_uid=NEW.escrow_uid AND stage=NEW.stage))
        BEGIN SELECT RAISE(ABORT,'handoff evidence is immutable'); END''')
    con.execute('''CREATE TRIGGER IF NOT EXISTS capability_operation_observations_no_replace
        BEFORE INSERT ON capability_operation_observations WHEN EXISTS (
          SELECT 1 FROM capability_operation_observations WHERE operation_id=NEW.operation_id
            AND result_digest=NEW.result_digest)
        BEGIN SELECT RAISE(ABORT,'handoff evidence is immutable'); END''')


def parse_result(raw, operation_id):
    value = decode(raw,4096)
    exact(value, ('ok','result')); require(value['ok'] is True)
    result = value['result']
    exact(result, ('schema','operationId','outcome','txHash','nonce'))
    require(type(result['schema']) is int and result['schema'] == 1 and result['operationId'] == operation_id
            and result['outcome'] in ('submitted','uncertain'))
    if result['txHash'] is None or result['nonce'] is None:
        require(result['txHash'] is None and result['nonce'] is None and result['outcome'] == 'uncertain')
    else:
        require(matches(result['txHash'], HASH) and result['txHash'] != '0x'+'0'*64
                and matches(result['nonce'], r'0|[1-9][0-9]{0,15}') and int(result['nonce']) <= 9007199254740991)
    return result


def run_host(mode, reference):
    command = Path(COMMANDS[mode])
    protected_read(command, 1048576, executable=True)
    process = subprocess.Popen([str(command)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, cwd='/', env={'PATH':'/usr/bin:/bin','LANG':'C'})
    output = bytearray()
    try:
        process.stdin.write((canonical(reference)+'\n').encode()); process.stdin.close()
        deadline = time.monotonic()+HOST_TIMEOUT_SECONDS
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout,selectors.EVENT_READ)
            while True:
                require(time.monotonic() < deadline)
                events = selector.select(max(0,deadline-time.monotonic()))
                require(events)
                part = os.read(process.stdout.fileno(),4097-len(output))
                if not part:break
                output.extend(part);require(len(output)<=4096)
        require(process.wait(timeout=max(0.01,deadline-time.monotonic())) == 0)
        return bytes(output)
    finally:
        # A timeout does not prove that the host/executor stopped or did not sign.
        # Preserve the intent and require the signer-free observe path thereafter.
        if process.poll() is None:
            process.kill();process.wait()
        process.stdin.close()
        process.stdout.close()


def database_action(path, action, approval, mode, now, result=None):
    """Fixed synchronous operations; never accepts code, SQL or a callback."""
    require(action in ('identity', 'prepare', 'check', 'record'))
    if action == 'identity':
        with connection(path, readonly=True) as con:
            require(con.execute('PRAGMA database_list').fetchone()[2] == str(path))
        return True
    if action == 'check':
        with connection(path, readonly=True) as con:
            bound(con, approval, now)
        return True
    operation_id, approval_digest = approval['operationId'], digest(approval)
    with connection(path) as con:
        con.execute('BEGIN IMMEDIATE')
        if action == 'record':
            result_digest, result_json = digest(result), canonical(result)
            existing = con.execute('SELECT result FROM capability_operation_observations WHERE operation_id=? AND result_digest=?',
                                   (operation_id,result_digest)).fetchone()
            if existing is not None:
                require(existing['result'] == result_json)
                return True
            con.execute('INSERT INTO capability_operation_observations VALUES(?,?,?,?)',
                        (operation_id,result_digest,result_json,now))
            return True
        tables(con)
        existing = con.execute('SELECT * FROM capability_operation_handoffs WHERE escrow_uid=? AND stage=?',
                               (approval['escrowUid'],approval['stage'])).fetchone()
        if existing:
            require(existing['operation_id'] == operation_id and existing['approval_digest'] == approval_digest)
            if mode == 'dispatch':
                return False
        else:
            require(mode == 'dispatch' and approval['notBefore'] <= now < approval['expiresAt'])
            bound(con,approval,now)
            if approval['stage'] == 'claim':
                f = approval['fulfillment']
                prior = con.execute("SELECT * FROM capability_operation_handoffs WHERE escrow_uid=? AND stage='fulfill'",
                                    (approval['escrowUid'],)).fetchone()
                require(prior is not None and prior['operation_id'] == f['operationId'])
                results = con.execute('SELECT result FROM capability_operation_observations WHERE operation_id=?',
                                      (f['operationId'],)).fetchall()
                require(any(json.loads(r['result'])['txHash'] == f['txHash'] for r in results))
            con.execute('INSERT INTO capability_operation_handoffs VALUES(?,?,?,?,?)',
                        (approval['escrowUid'],approval['stage'],operation_id,approval_digest,now))
    return True


def drop_database_privileges(identity):
    # This is called ONLY in a fresh child. There is no saved-root restoration.
    os.setgroups([])
    os.setgid(identity['gid'])
    os.setuid(identity['uid'])
    require(os.getuid() == os.geteuid() == identity['uid']
            and os.getgid() == os.getegid() == identity['gid'] and os.getgroups() == [])


def database_worker(path, identity, action, approval, mode, now, result=None):
    """Root parent launches one bounded, permanently unprivileged DB child.

    The manual CLI is single-threaded. No HTTP caller, injected transport or
    application callback executes in this child. SQLite sidecars belong to the
    independently pinned seller UID/GID, never root.
    """
    require(os.geteuid() == 0 and threading.active_count() == 1 and Path(path) == DATABASE)
    require(check_database(Path(path), identity['uid'], identity['gid']) == identity['inode'])
    reader, writer = os.pipe()
    try:
        pid = os.fork()
    except BaseException:
        os.close(reader)
        os.close(writer)
        raise
    if pid == 0:
        code = 3
        try:
            os.close(reader)
            for descriptor in (0, 1, 2):
                if descriptor != writer:
                    os.close(descriptor)
            drop_database_privileges(identity)
            require(check_database(Path(path), identity['uid'], identity['gid']) == identity['inode'])
            value = database_action(path, action, approval, mode, now, result)
            require(type(value) is bool)
            require(check_database(Path(path), identity['uid'], identity['gid']) == identity['inode'])
            os.write(writer, b'true' if value else b'false')
            code = 0
        except BaseException:
            pass
        finally:
            os._exit(code)
    os.close(writer)
    reaped = False
    try:
        output = bytearray()
        deadline = time.monotonic() + 15
        with selectors.DefaultSelector() as selector:
            selector.register(reader, selectors.EVENT_READ)
            while True:
                require(time.monotonic() < deadline and selector.select(max(0, deadline-time.monotonic())))
                part = os.read(reader, 7-len(output))
                if not part:
                    break
                output.extend(part)
                require(len(output) <= 5)
        _, status = os.waitpid(pid, 0)
        reaped = True
        require(os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0 and bytes(output) in (b'true', b'false'))
        require(check_database(Path(path), identity['uid'], identity['gid']) == identity['inode'])
        return bytes(output) == b'true'
    finally:
        os.close(reader)
        if not reaped:
            # An interrupted worker may already have committed; no signing retry.
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)


def handoff(path, approval, *, mode, invoke=run_host, now=None, recheck=None, identity=None):
    """Actual manual coordinator; injectable transport exists only for fixtures."""
    approval = validate_approval(json.loads(canonical(approval)))
    require(mode in COMMANDS)
    clock = lambda: int(time.time()) if now is None else now
    operation_id, approval_digest = approval['operationId'], digest(approval)
    reference = {'schema':1,'operationId':operation_id}
    def perform(action, result=None):
        if identity is None:
            return database_action(path, action, approval, mode, clock(), result)
        return database_worker(path, identity, action, approval, mode, clock(), result)
    if not perform('prepare'):
        return {'status':'reconciliation-required','operationId':operation_id,'retry':False,'settlementVerified':False}
    # A committed intent fences every later dispatch, even if this call dies here.
    result = {'schema':1,'operationId':operation_id,'outcome':'uncertain','txHash':None,'nonce':None}
    try:
        if recheck is not None:require(digest(recheck()) == approval_digest)
        if mode == 'dispatch':
            require(approval['notBefore'] <= clock() < approval['expiresAt'])
            perform('check')
        observed = parse_result(invoke(mode,reference),operation_id)
        if mode == 'observe':require(observed['outcome'] == 'uncertain')
        result = observed
    except Exception:
        # Fixed uncertainty, never echo command output or retry signing.
        pass
    perform('record', result)
    return {'status':result['outcome'],'operationId':operation_id,'txHash':result['txHash'],
            'nonce':result['nonce'],'retry':False,'settlementVerified':False}


def legacy_claim_permitted(path, escrow_uid, listing_id):
    """Positive original-mode proof, not an absent continuation heuristic.

    Old NULL rows deliberately refuse until separately classified outside this
    source rollout. A corrupt/missing database cannot open the legacy signer.
    """
    try:
        with connection(path,readonly=True) as con:
            row = con.execute('''SELECT e.settlement_mode,e.provisioning_job_id FROM escrows e
                JOIN negotiation_threads n ON n.negotiation_id=e.negotiation_id
                WHERE e.escrow_uid=? AND n.our_listing_id=?''',(escrow_uid,listing_id)).fetchone()
            if row is None or row['settlement_mode'] != 'legacy':return False
            import uuid
            if row['provisioning_job_id'] == str(uuid.uuid5(uuid.NAMESPACE_URL,'scm-container-lease:'+escrow_uid)):
                return False
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='capability_settlement_continuations'").fetchone():
                if con.execute('SELECT 1 FROM capability_settlement_continuations WHERE escrow_uid=?',(escrow_uid,)).fetchone():
                    return False
            return True
    except Exception:
        return False


def main():
    require(os.geteuid() == 0)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('dispatch','observe'))
    args = parser.parse_args()
    reference = scope(sys.stdin.buffer.read(257))
    approval = read_approval();require(reference['operationId'] == approval['operationId'])
    before = check_database(DATABASE,approval['databaseUid'],approval['databaseGid'])
    identity = {'uid':approval['databaseUid'],'gid':approval['databaseGid'],'inode':before}
    # Read-only identity before opening the durable journal transaction.
    database_worker(DATABASE,identity,'identity',approval,args.mode,int(time.time()))
    output = handoff(DATABASE,approval,mode=args.mode,recheck=read_approval,identity=identity)
    print(canonical(output))


if __name__ == '__main__':
    try:main()
    except Exception:
        print('{"retry":false,"status":"reconciliation-required","settlementVerified":false}')
        raise SystemExit(3)
