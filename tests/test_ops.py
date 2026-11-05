import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
ops=load('mindul_ops',ROOT/'ops/mindul_ops.py')
volume=load('volume_snapshot',ROOT/'ops/volume_snapshot.py')


class SQLiteBackupTests(unittest.TestCase):
    def test_online_backup_includes_committed_wal_and_restores_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';source.mkdir()
            db=sqlite3.connect(source/'app.sqlite')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE items (value TEXT)');db.commit()
            db.execute('INSERT INTO items VALUES (?)',('retained data',));db.commit()
            snapshot=root/'snapshot'
            with patch.dict(os.environ,{'OUT_UID':str(os.getuid()),'OUT_GID':str(os.getgid())}):
                volume.snapshot(source,snapshot,set())
            expected=json.loads((snapshot/'sqlite.json').read_text())
            self.assertEqual(expected['app.sqlite']['items'],1)
            self.assertFalse((snapshot/'data/app.sqlite-wal').exists())
            target=root/'restore';target.mkdir()
            volume.restore(snapshot,target)
            self.assertEqual(volume.inspect_sqlite(target/'app.sqlite')['items'],1)
            db.close()

    def test_snapshot_enforces_plaintext_size_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';source.mkdir()
            (source/'large').write_bytes(b'12345678')
            with patch.dict(os.environ,{'OUT_UID':str(os.getuid()),'OUT_GID':str(os.getgid()),'MAX_BYTES':'4'}):
                with self.assertRaises(RuntimeError):volume.snapshot(source,root/'snapshot',set())

    def test_restore_refuses_nonempty_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            target=Path(temporary);(target/'existing').write_text('preserve')
            with self.assertRaises(RuntimeError):volume.restore(target,target)
            self.assertEqual((target/'existing').read_text(),'preserve')

    def test_absolute_symlinks_are_metadata_not_followed_during_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';source.mkdir()
            (source/'extensions').symlink_to('/config/extensions')
            snapshot=root/'snapshot'
            with patch.dict(os.environ,{'OUT_UID':str(os.getuid()),'OUT_GID':str(os.getgid())}):
                volume.snapshot(source,snapshot,set())
            self.assertFalse((snapshot/'data/extensions').is_symlink())
            self.assertEqual(json.loads((snapshot/'links.json').read_text())['extensions'],'/config/extensions')
            target=root/'restore';target.mkdir();volume.restore(snapshot,target)
            self.assertEqual(os.readlink(target/'extensions'),'/config/extensions')


@unittest.skipUnless(shutil.which('openssl'),'OpenSSL required')
class EncryptedBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.root=Path(self.temporary.name)
        self.key=self.root/'key';self.key.write_text('test-only-passphrase\n')
        self.patchers=[patch.object(ops,'KEY',self.key),patch.object(ops,'STATE',self.root)]
        for p in self.patchers:p.start()

    def tearDown(self):
        for p in reversed(self.patchers):p.stop()
        self.temporary.cleanup()

    def encrypted(self):
        stage=self.root/'stage';stage.mkdir()
        (stage/'inventory.json').write_text('{"volumes":[]}')
        (stage/'secret').write_text('private-sample-value')
        output=self.root/'backup.tar.gz.enc'
        ops.encrypt(stage,output)
        sha,signature=ops.archive_signature(output)
        ops.write(output.with_suffix('.json'),{'volumes':[],'sha256':sha,'hmac_sha256':signature})
        return output

    def test_roundtrip_and_private_modes(self):
        backup=self.encrypted()
        self.assertNotIn(b'private-sample-value',backup.read_bytes())
        self.assertEqual(backup.stat().st_mode&0o777,0o600)
        with ops.decrypted(backup) as (snapshot,_):
            self.assertEqual((snapshot/'secret').read_text(),'private-sample-value')

    def test_tampering_is_rejected_before_decryption(self):
        backup=self.encrypted()
        content=bytearray(backup.read_bytes());content[-5]^=1;backup.write_bytes(content)
        with self.assertRaisesRegex(RuntimeError,'signature'):
            with ops.decrypted(backup):pass

    def test_changed_manifest_cannot_hide_missing_volumes(self):
        backup=self.encrypted()
        manifest=json.loads(backup.with_suffix('.json').read_text());manifest['volumes']=[{'name':'forged'}]
        backup.with_suffix('.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(RuntimeError,'manifest'):
            with ops.decrypted(backup):pass

    def test_restore_directory_must_be_empty(self):
        backup=self.encrypted()
        destination=self.root/'destination';destination.mkdir();(destination/'file').write_text('keep')
        with self.assertRaisesRegex(RuntimeError,'empty'):ops.restore_files(backup,destination)

    def test_failed_verification_preserves_previous_backup_and_removes_new_artifact(self):
        backups=self.root/'backups';backups.mkdir()
        old=backups/'mindul-old.tar.gz.enc';old.write_bytes(b'previous-successful-backup')
        with patch.object(ops,'BACKUPS',backups),patch.object(ops,'copy_configs'),\
             patch.object(ops,'backup_volumes',return_value=[]),\
             patch.object(ops,'verify_backup',side_effect=RuntimeError('verification failed')):
            with self.assertRaisesRegex(RuntimeError,'verification failed'):ops.create_backup()
        self.assertEqual(list(backups.iterdir()),[old])
        self.assertEqual(old.read_bytes(),b'previous-successful-backup')


class ManagedConfigTests(unittest.TestCase):
    def test_dashboard_uids_and_alert_rule_uids_are_unique(self):
        dashboards=[json.loads(p.read_text()) for p in (ROOT/'monitoring/grafana/dashboards').glob('*.json')]
        self.assertEqual(len(dashboards),len({d['uid'] for d in dashboards}))
        rules=json.loads((ROOT/'monitoring/grafana/provisioning/alerting/mindul.json').read_text())['groups'][0]['rules']
        self.assertEqual(len(rules),len({r['uid'] for r in rules}))
        self.assertTrue(all(r['data'][0]['datasourceUid']=='prometheus' for r in rules))

    def test_backups_exclude_redownloadable_model_blobs(self):
        settings=json.loads((ROOT/'ops/config.json').read_text())
        model=next(v for v in settings['volumes'] if v['name'].endswith('ollama_data'))
        self.assertIn('models/blobs',model['exclude'])
        self.assertLessEqual(settings['backup_keep_count'],7)


if __name__=='__main__':unittest.main()
