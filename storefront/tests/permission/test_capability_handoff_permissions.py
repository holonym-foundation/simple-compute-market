"""Disposable Linux CI permission fixture; never targets a seller installation.

Requires explicit root execution on Linux. No Docker, network, signer or host
command. All state is below a new temporary /run directory, removed at teardown.
"""
import os
from pathlib import Path
import pwd
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from market_storefront.utils import capability_settlement_handoff as handoff


@unittest.skipUnless(sys.platform == 'linux' and os.geteuid() == 0, 'explicit disposable root Linux fixture only')
class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aex-scm-permission-', dir='/run')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o755)
        nobody = pwd.getpwnam('nobody')
        self.uid, self.gid = nobody.pw_uid, nobody.pw_gid
        self.assertGreater(self.uid, 0)
        self.assertGreater(self.gid, 0)
        leaf = self.root / 'seller'
        leaf.mkdir(mode=0o700)
        os.chown(leaf, self.uid, self.gid)
        self.path = leaf / 'agent.db'
        with sqlite3.connect(self.path) as con:
            con.execute('PRAGMA journal_mode=WAL')
            handoff.tables(con)
        con.close()
        os.chown(self.path, self.uid, self.gid)
        self.path.chmod(0o600)
        self.identity = {'uid': self.uid, 'gid': self.gid,
                         'inode': handoff.check_database(self.path, self.uid, self.gid)}

    def test_real_drop_and_child_sqlite_write_preserve_owner_and_parent(self):
        operation = 'sha256:' + '1' * 64
        result = {'schema': 1, 'operationId': operation, 'outcome': 'uncertain', 'txHash': None, 'nonce': None}
        before = (os.getuid(), os.geteuid(), os.getgid(), os.getegid(), os.getgroups())
        with patch.object(handoff, 'DATABASE', self.path):
            self.assertTrue(handoff.database_worker(self.path, self.identity, 'record',
                {'operationId': operation}, 'observe', 1000, result))
        self.assertEqual(before, (os.getuid(), os.geteuid(), os.getgid(), os.getegid(), os.getgroups()))
        self.assertEqual(self.path.stat().st_uid, self.uid)
        self.assertEqual(self.path.stat().st_gid, self.gid)
        # The worker closed/checkpointed WAL before ACK. Immutable read cannot
        # itself create root-owned sidecars while checking the committed row.
        with sqlite3.connect(self.path.as_uri() + '?mode=ro&immutable=1', uri=True) as con:
            self.assertEqual(con.execute('SELECT operation_id FROM capability_operation_observations').fetchone()[0], operation)
        con.close()
        handoff.check_database(self.path, self.uid, self.gid)

    def test_nontraversable_ancestor_and_wrong_leaf_owner_refuse(self):
        self.root.chmod(0o700)
        with self.assertRaises(ValueError):
            handoff.check_database(self.path, self.uid, self.gid)
        self.root.chmod(0o755)
        os.chown(self.path.parent, 0, 0)
        with self.assertRaises(ValueError):
            handoff.check_database(self.path, self.uid, self.gid)

    def test_sidecar_symlink_and_inode_replacement_refuse(self):
        side = Path(str(self.path) + '-wal')
        side.symlink_to(self.path)
        with self.assertRaises(ValueError):
            handoff.check_database(self.path, self.uid, self.gid)
        side.unlink()
        changed = {**self.identity, 'inode': (0, 0)}
        with patch.object(handoff, 'DATABASE', self.path), self.assertRaises(ValueError):
            handoff.database_worker(self.path, changed, 'identity', {}, 'observe', 1000)

    def test_protected_file_read_is_repeatable_and_rejects_writable_content(self):
        approval = self.root / 'synthetic.json'
        approval.write_bytes(b'{}\n')
        approval.chmod(0o444)
        self.assertEqual(handoff.protected_read(approval, 16), b'{}\n')
        self.assertEqual(handoff.protected_read(approval, 16), b'{}\n')
        approval.chmod(0o644)
        with self.assertRaises(ValueError):
            handoff.protected_read(approval, 16)


if __name__ == '__main__':
    unittest.main()
