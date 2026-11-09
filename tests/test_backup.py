import io
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from types import SimpleNamespace
from unittest import mock

from agent_collab import backup as b, domain as d, protocol as p
from agent_collab.controller import App, restore
from helpers import ControllerCase, manifest, uid


class FakeRemote:
    def __init__(self, root):
        self.root = root
        self.downloads = []
        self.confirmations = []

    def list(self):
        return b.list_complete(self.root)

    def download(self, ident, local_path):
        self.downloads.append(ident)
        shutil.copyfile(str(self.root / ident / 'backup.tar'), str(local_path))

    def confirm(self, ident, sha, location):
        self.confirmations.append(ident)
        return b.confirm_offsite(self.root, ident, sha, location)


class ScheduledBackupTests(ControllerCase):
    def setUp(self):
        super().setUp()
        self.root = self.temp / 'backups'
        self.dest = self.temp / 'offsite'
        self.remote = FakeRemote(self.root)

    def local(self, keep=8, max_bytes=500000000):
        return b.run_local(self.temp / 'data', self.root, keep, max_bytes)

    def cli(self, *args):
        root = Path(__file__).resolve().parents[1]
        return subprocess.run([sys.executable, '-B', '-m', 'agent_collab.backup'] + list(args),
                              cwd=str(root), env=dict(os.environ, TMPDIR=str(root / '.dev/tmp')),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_local_complete_status_and_deterministic_archive(self):
        complete = self.local()
        directory = self.root / complete['id']
        self.assertEqual(b.read_json(directory / 'COMPLETE.json'), complete)
        self.assertEqual(b.list_complete(self.root), [complete])
        self.assertEqual(b.status(self.root)['local']['last_success_id'], complete['id'])
        self.assertIsNone(b.status(self.root)['local']['last_error'])
        self.assertEqual(b.file_hash(directory / 'backup.tar'), complete['tar_sha256'])
        extracted = self.temp / 'extracted'
        b.verify_archive(directory / 'backup.tar', complete, extracted)
        for path in extracted.rglob('*'):
            os.utime(str(path), (42, 42))
        b.deterministic_tar(extracted, self.temp / 'again.tar')
        self.assertEqual(b.file_hash(self.temp / 'again.tar'), complete['tar_sha256'])
        with tarfile.open(str(directory / 'backup.tar')) as archive:
            self.assertEqual(archive.getnames(), sorted(archive.getnames()))
            for entry in archive:
                self.assertEqual((entry.mtime, entry.uid, entry.gid, entry.uname, entry.gname), (0, 0, 0, '', ''))
        self.assertEqual({p.name for p in directory.iterdir()}, {'COMPLETE.json', 'backup.tar'})

    def test_lock_rejects_second_process_without_touching_state_or_tmp(self):
        self.local()
        before = (self.root / 'status.json').read_bytes()
        abandoned = self.root / '.tmp-interrupted'
        abandoned.mkdir()
        with b.BackupLock(self.root):
            result = self.cli('run-local', '--data-dir', str(self.temp / 'data'), '--backups-dir', str(self.root))
            self.assertEqual(result.returncode, b.LOCKED_EXIT, result.stderr)
            self.assertEqual((self.root / 'status.json').read_bytes(), before)
            self.assertTrue(abandoned.exists())
        self.local()
        self.assertFalse(abandoned.exists())

    def test_partial_local_sets_ignored_and_cleaned_after_crash(self):
        first = self.local()
        incomplete = self.root / 'no-complete'
        incomplete.mkdir()
        abandoned = self.root / '.tmp-interrupted'
        abandoned.mkdir()
        b.atomic_json(abandoned / 'COMPLETE.json', first)
        self.assertEqual(b.list_complete(self.root), [first])
        self.local()
        self.assertFalse(abandoned.exists())
        self.assertTrue(incomplete.exists())

    def test_local_failure_preserves_success_and_retry_works(self):
        self.local()
        old = b.status(self.root)['local'].copy()
        with mock.patch.object(b, 'backup', side_effect=RuntimeError('snapshot unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'snapshot unavailable'):
                self.local()
        failed = b.status(self.root)['local']
        self.assertEqual(failed['last_success_at'], old['last_success_at'])
        self.assertEqual(failed['last_success_id'], old['last_success_id'])
        self.assertEqual(failed['last_error'], 'snapshot unavailable')
        self.assertTrue(failed['last_failure_at'])
        self.local()
        self.assertIsNone(b.status(self.root)['local']['last_error'])
        self.assertFalse(list(self.root.glob('.tmp-*')))

    def test_retention_protects_latest_and_latest_confirmed_and_upgrade(self):
        first = self.local()
        b.confirm_offsite(self.root, first['id'], first['tar_sha256'], 'mac:/copy')
        upgrade = self.root / 'upgrade-saved'
        upgrade.mkdir()
        (upgrade / 'keep').write_text('upgrade', encoding='utf-8')
        self.local()
        latest = self.local(keep=1, max_bytes=1)
        self.assertEqual({item['id'] for item in b.list_complete(self.root)}, {first['id'], latest['id']})
        self.assertTrue((upgrade / 'keep').exists())
        b.confirm_offsite(self.root, latest['id'], latest['tar_sha256'], 'mac:/copy')
        last_confirmed = b.status(self.root)['offsite']
        b.confirm_offsite(self.root, first['id'], first['tar_sha256'], 'mac:/late')
        self.assertEqual(b.status(self.root)['offsite'], last_confirmed)
        newest = self.local(keep=1, max_bytes=1)
        self.assertEqual({item['id'] for item in b.list_complete(self.root)}, {latest['id'], newest['id']})

    def test_retention_always_keeps_one_without_any_offsite_copy(self):
        self.local(keep=1, max_bytes=1)
        second = self.local(keep=1, max_bytes=1)
        self.assertEqual(b.list_complete(self.root), [second])

    def test_confirmation_validates_hash_is_idempotent_and_recovers_status(self):
        complete = self.local()
        before = b.status(self.root)
        with self.assertRaises(ValueError):
            b.confirm_offsite(self.root, complete['id'], 'f' * 64, 'mac:/copy')
        self.assertEqual(b.status(self.root), before)
        marker = b.confirm_offsite(self.root, complete['id'], complete['tar_sha256'], 'mac:/copy')
        b.atomic_json(self.root / 'status.json', before)
        self.assertEqual(b.confirm_offsite(self.root, complete['id'], complete['tar_sha256'], 'mac:/copy'), marker)
        self.assertEqual(b.status(self.root)['offsite']['last_confirmed_at'], marker['confirmed_at'])

    def test_pull_verifies_confirms_and_noop_does_not_advance_success(self):
        first = self.local()
        latest = self.local()
        self.assertEqual(b.pull(self.dest, self.remote), latest)
        receipt = b.read_json(self.dest / (latest['id'] + '.json'))
        self.assertTrue(receipt['verified_at'])
        self.assertTrue(receipt['confirmed_at'])
        self.assertEqual(b.status(self.root)['offsite']['last_confirmed_id'], latest['id'])
        self.assertEqual(self.remote.downloads, [latest['id']])
        state = b.status(self.dest)
        self.assertIsNone(b.pull(self.dest, self.remote))
        self.assertEqual(b.status(self.dest), state)
        self.assertEqual(self.remote.confirmations, [latest['id']])
        self.assertFalse((self.dest / (first['id'] + '.tar')).exists())

    def test_pull_lock_does_not_change_state(self):
        self.local()
        b.pull(self.dest, self.remote)
        before = (self.dest / 'status.json').read_bytes()
        with b.BackupLock(self.dest):
            with self.assertRaises(b.BackupBusy):
                b.pull(self.dest, self.remote)
        self.assertEqual((self.dest / 'status.json').read_bytes(), before)

    def test_pull_network_hash_and_size_failures_preserve_existing_copy_then_retry(self):
        old = self.local()
        b.pull(self.dest, self.remote)
        saved = (self.dest / (old['id'] + '.tar')).read_bytes()
        success = b.status(self.dest)['last_success_at']
        latest = self.local()
        def bad_download(ident, path):
            path.write_bytes(b'bad')
        for method, effect in [('list', RuntimeError('remote unavailable')),
                               ('download', RuntimeError('download failed')), ('download', bad_download)]:
            with self.subTest(method=method, effect=str(effect)):
                with mock.patch.object(self.remote, method, side_effect=effect):
                    with self.assertRaises((RuntimeError, ValueError)):
                        b.pull(self.dest, self.remote, keep_offsite=1)
                state = b.status(self.dest)
                self.assertEqual(state['last_success_at'], success)
                self.assertTrue(state['last_error'])
                self.assertTrue(state['last_failure_at'])
                self.assertEqual((self.dest / (old['id'] + '.tar')).read_bytes(), saved)
        download = self.remote.download
        def same_size_bad_hash(ident, path):
            download(ident, path)
            raw = bytearray(path.read_bytes())
            raw[-1] ^= 1
            path.write_bytes(raw)
        with mock.patch.object(self.remote, 'download', side_effect=same_size_bad_hash):
            with self.assertRaisesRegex(ValueError, 'hash/size'):
                b.pull(self.dest, self.remote)
        self.assertEqual(b.status(self.dest)['last_success_at'], success)
        self.assertEqual(b.pull(self.dest, self.remote, keep_offsite=1), latest)
        self.assertFalse((self.dest / (old['id'] + '.tar')).exists())

    def test_lost_ack_retries_verified_copy_before_newer_snapshot(self):
        complete = self.local()
        confirm = self.remote.confirm
        def lost_ack(*args):
            confirm(*args)
            raise RuntimeError('confirmation response lost')
        with mock.patch.object(self.remote, 'confirm', side_effect=lost_ack):
            with self.assertRaisesRegex(RuntimeError, 'response lost'):
                b.pull(self.dest, self.remote)
        self.assertNotIn('last_success_at', b.status(self.dest))
        self.assertTrue((self.dest / (complete['id'] + '.tar')).exists())
        marker = b.read_json(self.root / complete['id'] / 'OFFSITE.json')
        newer = self.local()
        self.assertEqual(b.pull(self.dest, self.remote), complete)
        self.assertEqual(self.remote.downloads, [complete['id']])
        self.assertEqual(b.read_json(self.root / complete['id'] / 'OFFSITE.json'), marker)
        self.assertEqual(b.pull(self.dest, self.remote), newer)

    def test_interrupted_download_reuses_complete_partial_on_restart(self):
        complete = self.local()
        download = self.remote.download
        def interrupt(ident, path):
            download(ident, path)
            raise KeyboardInterrupt()
        with mock.patch.object(self.remote, 'download', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                b.pull(self.dest, self.remote)
        self.assertFalse((self.dest / 'status.json').exists())
        self.assertEqual(b.pull(self.dest, self.remote), complete)
        self.assertEqual(self.remote.downloads, [complete['id']])

    def test_pending_copy_pruned_on_remote_does_not_block_new_backups(self):
        old = self.local()
        with mock.patch.object(self.remote, 'confirm', side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):
                b.pull(self.dest, self.remote)
        newer = self.local(keep=1)
        self.assertFalse((self.root / old['id']).exists())
        with mock.patch.object(self.remote, 'download', side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):
                b.pull(self.dest, self.remote, keep_offsite=1)
        self.assertTrue((self.dest / (old['id'] + '.tar')).exists())
        self.assertEqual(b.pull(self.dest, self.remote, keep_offsite=2), newer)
        self.assertTrue((self.dest / (old['id'] + '.tar')).exists())
        self.assertIsNone(b.pull(self.dest, self.remote, keep_offsite=1))
        self.assertFalse((self.dest / (old['id'] + '.tar')).exists())
        self.assertEqual(b.status(self.dest)['last_success_id'], newer['id'])

    def test_interrupted_publish_and_status_recover_without_redownload(self):
        complete = self.local()
        atomic = b.atomic_json
        def crash_before_json(path, value):
            if Path(path) == self.dest / (complete['id'] + '.json'):
                raise KeyboardInterrupt()
            atomic(path, value)
        with mock.patch.object(b, 'atomic_json', side_effect=crash_before_json):
            with self.assertRaises(KeyboardInterrupt):
                b.pull(self.dest, self.remote)
        self.assertTrue((self.dest / (complete['id'] + '.tar')).exists())
        def crash_before_status(path, value):
            if Path(path) == self.dest / 'status.json':
                raise KeyboardInterrupt()
            atomic(path, value)
        with mock.patch.object(b, 'atomic_json', side_effect=crash_before_status):
            with self.assertRaises(KeyboardInterrupt):
                b.pull(self.dest, self.remote)
        receipt = b.read_json(self.dest / (complete['id'] + '.json'))
        self.assertTrue(receipt['confirmed_at'])
        self.assertIsNone(b.pull(self.dest, self.remote))
        self.assertEqual(b.status(self.dest)['last_success_at'], receipt['confirmed_at'])
        self.assertEqual(self.remote.downloads, [complete['id']])

    def test_sqlite_integrity_failure_does_not_confirm_or_replace_previous_copy(self):
        old = self.local()
        b.pull(self.dest, self.remote)
        before = b.status(self.dest)['last_success_at']
        latest = self.local()
        directory = self.root / latest['id']
        extracted = self.temp / 'corrupt-db'
        manifest = b.verify_archive(directory / 'backup.tar', latest, extracted)
        database = extracted / manifest['database']
        raw = bytearray(database.read_bytes())
        raw[0:16] = b'invalid database'
        database.write_bytes(raw)
        digest = b.file_hash(database)
        manifest.update(database='controller-' + digest + '.db', db_sha256=digest)
        database.rename(extracted / manifest['database'])
        b.atomic_json(extracted / 'backup-manifest.json', manifest)
        b.deterministic_tar(extracted, directory / 'backup.tar')
        bad = dict(latest, db_sha256=digest, tar_sha256=b.file_hash(directory / 'backup.tar'),
                   tar_bytes=(directory / 'backup.tar').stat().st_size)
        b.atomic_json(directory / 'COMPLETE.json', bad)
        with self.assertRaises(sqlite3.DatabaseError):
            b.pull(self.dest, self.remote)
        self.assertEqual(b.status(self.dest)['last_success_at'], before)
        self.assertTrue((self.dest / (old['id'] + '.tar')).exists())
        self.assertFalse((directory / 'OFFSITE.json').exists())
        newer = self.local()
        self.assertEqual(b.pull(self.dest, self.remote), newer)

    def test_unsafe_tar_and_manifest_mismatch_rejected(self):
        complete = self.local()
        archive_path = self.temp / 'unsafe.tar'
        for name, kind in [('../escape', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE),
                           ('backup-manifest.json', tarfile.SYMTYPE)]:
            with self.subTest(name=name, kind=kind):
                with tarfile.open(str(archive_path), 'w') as archive:
                    info = tarfile.TarInfo(name)
                    info.type = kind
                    info.linkname = '../escape' if kind == tarfile.SYMTYPE else ''
                    archive.addfile(info, io.BytesIO(b''))
                bad = dict(complete, tar_sha256=b.file_hash(archive_path), tar_bytes=archive_path.stat().st_size)
                target = self.temp / ('unsafe-' + uid())
                with self.assertRaisesRegex(ValueError, 'tar member'):
                    b.verify_archive(archive_path, bad, target)
        bad = dict(complete, authority_generation=99)
        with self.assertRaisesRegex(ValueError, 'manifest/COMPLETE'):
            b.verify_archive(self.root / complete['id'] / 'backup.tar', bad, self.temp / 'bad-manifest')
        self.assertFalse((self.temp / 'escape').exists())

    def test_pulled_backup_restores_and_old_tokens_and_coordinator_cannot_revive(self):
        node = self.node()
        job = self.job()
        assignment = node.claim()
        node.upload(b'recover me')
        cp = node.checkpoint(assignment, manifest(b'recover me'))
        lease_path = '/packages/' + job['work_package_id'] + '/coordinator'
        lease = self.call('POST', lease_path, dict(request_id=uid(), holder_kind='interactive',
                          holder_ref='old', ttl_s=90, takeover=False, reason=''))
        complete = self.local()
        b.pull(self.dest, self.remote)
        extracted = self.temp / 'restore-source'
        b.verify_archive(self.dest / (complete['id'] + '.tar'), complete, extracted)
        isolated = self.temp / 'isolated'
        self.assertEqual(restore(isolated, extracted, self.clock), complete['authority_generation'] + 1)
        self.app.close()
        self.app = App(isolated, clock=self.clock)
        old_admin = self.admin
        self.admin = (isolated / 'admin.token').read_text().strip()
        self.assertNotEqual(self.admin, old_admin)
        self.call('GET', '/overview', token=old_admin, status=401)
        self.assertTrue(self.app.meta()['dispatch_paused'])
        self.assertIsNone(self.call('GET', lease_path))
        renewed = self.call('POST', lease_path, dict(request_id=uid(), holder_kind='interactive',
                            holder_ref='new', ttl_s=90, takeover=False, reason=''))
        self.assertNotEqual(renewed['coordinator_token'], lease['coordinator_token'])
        self.call('DELETE', lease_path, headers={'authorization': 'Bearer ' + self.admin,
                  'x-coordinator-token': lease['coordinator_token']}, status=409)
        self.assertEqual(node.complete(assignment, checkpoint=cp, status=409)['error']['details']['reason'], 'stale_generation')
        self.assertTrue(node.heartbeat()['reconcile_required'])
        node.claim(status=409)

    def test_overview_doctor_times_ages_errors_and_unknown(self):
        fields = {'local_last_success_at', 'local_age_s', 'offsite_last_confirmed_at', 'offsite_age_s', 'last_error'}
        for endpoint in ('/overview', '/doctor'):
            self.assertEqual(self.call('GET', endpoint)['backup'], {key: 'unknown' for key in fields})
        self.root.mkdir()
        b.atomic_json(self.root / 'status.json', {
            'local': {'last_success_at': d.timestamp(self.clock() - 60), 'last_error': 'disk full'},
            'offsite': {'last_confirmed_at': d.timestamp(self.clock() - 180)}})
        for endpoint in ('/overview', '/doctor'):
            result = self.call('GET', endpoint)['backup']
            self.assertEqual(result['local_age_s'], 60)
            self.assertEqual(result['offsite_age_s'], 180)
            self.assertEqual(result['local_last_success_at'], d.timestamp(self.clock() - 60))
            self.assertEqual(result['offsite_last_confirmed_at'], d.timestamp(self.clock() - 180))
            self.assertEqual(result['last_error'], 'disk full')
        self.assertEqual(self.call('GET', '/doctor')['backup_age_s'], 60)
        (self.root / 'status.json').write_text('{bad', encoding='utf-8')
        self.assertIn('unreadable', self.call('GET', '/overview')['backup']['last_error'])

    def test_cli_local_list_confirm_status_and_errors(self):
        result = self.cli('run-local', '--data-dir', str(self.temp / 'data'), '--backups-dir', str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        complete = json.loads(result.stdout)
        self.assertEqual(json.loads(self.cli('list', '--backups-dir', str(self.root)).stdout), [complete])
        result = self.cli('confirm', '--backups-dir', str(self.root), '--id', complete['id'],
                          '--sha256', complete['tar_sha256'], '--location', 'mac:/copy')
        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in ('--dest', '--backups-dir'):
            result = self.cli('status', flag, str(self.root))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), b.status(self.root))
        result = self.cli('run-local', '--data-dir', str(self.temp / 'data'), '--backups-dir', str(self.root), '--keep', '0')
        self.assertEqual(result.returncode, 1)


class WorkbenchTests(ControllerCase):
    def test_command_construction_and_strict_remote_paths(self):
        root = '/var/lib/agent-collab/backups'
        command = b.remote_command(root, '/usr/bin/python3', 'list')
        self.assertTrue(command.startswith('cd /opt/agent-collab/current && '))
        self.assertEqual(shlex.split(command.split(' && ', 1)[1]), [
            'runuser', '-u', 'agent-collab', '--', '/usr/bin/python3', '-B', '-m',
            'agent_collab.backup', 'list', '--backups-dir', root])
        location = "/Volumes/My Drive/a'; $(touch nope)"
        command = b.remote_command(root, 'python3', 'confirm', 'safe-id', 'a' * 64, location)
        self.assertEqual(shlex.split(command.split(' && ', 1)[1])[-1], location)
        self.assertEqual(b.download_command('i-example', '/bin/workbench', root, 'safe-id', '/local path'),
                         ['/bin/workbench', 'download', root + '/safe-id/backup.tar', '/local path',
                          '--instance-id', 'i-example', '-f'])
        for bad in ('../escape', '/escape', 'a/b', 'a\\b', '-option', 'x;id', 'upgrade-old', '', '.', '..', 'status', 'NUL'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    b.download_command('i-example', 'workbench', root, bad, '/local')
        for bad in ('relative', '/', '/a/../b', '/a/./b', '/a//b', '/a/', '/a;id', '/a b', '/a\n'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    b.remote_command(bad, 'python3', 'list')
        for bad in ('python3 -c', 'python3;id', '-python', '/python\n'):
            with self.assertRaises(ValueError):
                b.remote_command(root, bad, 'list')

    def test_workbench_json_exit_code_and_download_argv(self):
        remote = b.WorkbenchRemote('i-example')
        success = SimpleNamespace(stdout=json.dumps(dict(exit_code=0, stdout='[]', stderr='')), returncode=0)
        with mock.patch.object(b.subprocess, 'run', return_value=success) as run:
            self.assertEqual(remote.list(), [])
            self.assertEqual(run.call_args[0][0][:4], ['/usr/local/bin/workbench', 'exec', '--instance-id', 'i-example'])
            self.assertTrue(run.call_args[1]['check'])
            remote.download('safe-id', self.temp / 'file.tar')
            self.assertEqual(run.call_args[0][0], b.download_command('i-example', remote.workbench, remote.root,
                                                                    'safe-id', self.temp / 'file.tar'))
        for exit_code in (1, 75, None, False, '0'):
            failure = SimpleNamespace(stdout=json.dumps(dict(exit_code=exit_code, stdout='[]', stderr='failure')))
            with mock.patch.object(b.subprocess, 'run', return_value=failure):
                with self.assertRaisesRegex(RuntimeError, 'workbench exec failed'):
                    remote.list()
        with mock.patch.object(b.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['workbench'])):
            with self.assertRaises(subprocess.CalledProcessError):
                remote.list()

    def test_service_templates_are_explicit_and_parseable(self):
        root = Path(__file__).resolve().parents[1] / 'installer/services'
        service = (root / 'agent-collab-backup.service').read_text(encoding='utf-8')
        timer = (root / 'agent-collab-backup.timer').read_text(encoding='utf-8')
        self.assertIn('Type=oneshot', service)
        self.assertIn('User=agent-collab', service)
        self.assertIn('run-local', service)
        self.assertIn('Persistent=true', timer)
        self.assertIn('RandomizedDelaySec=10min', timer)
        self.assertIn('OnCalendar=*-*-* 00,06,12,18:00:00', timer)
        with (root / 'org.agent-collab.backup-pull.plist').open('rb') as stream:
            agent = plistlib.load(stream)
        self.assertEqual(agent['StartInterval'], 21600)
        self.assertFalse(agent['RunAtLoad'])
        self.assertIn('pull', agent['ProgramArguments'])
        self.assertTrue(agent['StandardOutPath'].startswith('@STATE_DIR@'))
