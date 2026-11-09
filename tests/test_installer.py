"""Installer acceptance: only repo-local files, no live services or sockets."""
from contextlib import closing, redirect_stdout, redirect_stderr
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET
import zipfile

from agent_collab import installer as ins
from agent_collab.client import Client
from agent_collab.controller import App
from agent_collab.node_agent import AgentLock
from helpers import ControllerCase

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('build_release', ROOT / 'scripts/build_release.py')
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def hashes(path):
    return {p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob('*') if p.is_file()}


class InstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ROOT / '.dev/tmp').mkdir(parents=True, exist_ok=True)
        cls.shared = Path(tempfile.mkdtemp(dir=str(ROOT / '.dev/tmp')))
        cls.v1, cls.zip1 = builder.build_release(ROOT, cls.shared, '1.0.0')
        cls.v2, _ = builder.build_release(ROOT, cls.shared, '2.0.0')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(str(cls.shared))

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(dir=str(ROOT / '.dev/tmp')))
        self.addCleanup(shutil.rmtree, str(self.temp))
        self.prefix = self.temp / 'code with spaces'
        self.state = self.temp / 'state with spaces'

    def args(self, role='controller'):
        return ['--role', role, '--prefix', str(self.prefix), '--state-dir', str(self.state),
                '--python', sys.executable, '--no-service']

    def invoke(self, *extra, role='controller', status=0):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = ins.main(self.args(role) + list(extra))
        self.assertEqual(result, status, output.getvalue())
        return output.getvalue()

    def build_modified(self, version, mutate):
        manifest, files = ins.read_release(self.v1)
        manifest['version'] = version
        mutate(manifest, files)
        files[ins.MANIFEST] = json.dumps(manifest).encode()
        path = self.temp / (version + '.zip')
        with zipfile.ZipFile(str(path), 'w') as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        return path

    def test_release_manifest_matches_both_formats_and_excludes_private_files(self):
        first, _ = ins.read_release(self.v1)
        second, _ = ins.read_release(self.zip1)
        self.assertEqual(first, second)
        self.assertEqual(first['version'], '1.0.0')
        self.assertEqual(first['python_requires'], '>=3.9')
        self.assertEqual(first['ui_built'], (ROOT / 'agent_collab/static/index.html').exists())
        for name in first['files']:
            self.assertTrue(name.startswith(('agent_collab/', 'installer/')))
            self.assertFalse(name.endswith(('.db', '.token', '.pyc')))
            self.assertNotIn('__pycache__', name)
        self.assertIn('installer/install.ps1', first['files'])

    def test_builder_excludes_secrets_and_includes_built_ui(self):
        source = self.temp / 'source'
        for folder in ('agent_collab', 'installer'):
            shutil.copytree(str(ROOT / folder), str(source / folder))
        for name in ('agent_collab/private.token', 'agent_collab/secret.db', 'installer/local.db-wal'):
            (source / name).write_text('secret')
        (source / 'agent_collab/static').mkdir(exist_ok=True)
        (source / 'agent_collab/static/index.html').write_text('<html>built</html>')
        tar, _ = builder.build_release(source, self.temp / 'out', 'ui-test')
        manifest, files = ins.read_release(tar)
        self.assertTrue(manifest['ui_built'])
        self.assertIn('agent_collab/static/index.html', files)
        self.assertFalse(any('secret' in name or name.endswith('.token') for name in files))

    def test_tamper_rejected_before_any_prefix_or_state_write(self):
        def tamper(manifest, files):
            files['agent_collab/__init__.py'] += b'\n# tampered'
        bad = self.build_modified('tamper', tamper)
        self.invoke('--release', str(bad), status=1)
        self.assertFalse(self.prefix.exists())
        self.assertFalse(self.state.exists())

    def test_tar_symlink_duplicate_and_zip_traversal_rejected(self):
        for name in ('../escape', '/escape', 'C:/escape', 'installer/CON', 'installer/a\\b'):
            bad = self.build_modified('unsafe', lambda manifest, files: files.update({name: b'evil'}))
            self.invoke('--release', str(bad), status=1)
            self.assertFalse(self.prefix.exists())
        bad = self.temp / 'symlink.tar.gz'
        with tarfile.open(str(bad), 'w:gz') as archive:
            info = tarfile.TarInfo('installer/link')
            info.type, info.linkname = tarfile.SYMTYPE, '/outside'
            archive.addfile(info)
        with self.assertRaises(ValueError):
            ins.read_release(bad)
        with tarfile.open(str(bad), 'w:gz') as archive:
            for _ in range(2):
                info = tarfile.TarInfo('installer/file')
                info.size = 1
                archive.addfile(info, io.BytesIO(b'x'))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            ins.read_release(bad)

    def test_case_collisions_and_extra_files_rejected(self):
        def mutate(manifest, files):
            files['agent_collab/A/x'] = b'x'
            files['agent_collab/a/y'] = b'y'
            for name in ('agent_collab/A/x', 'agent_collab/a/y'):
                manifest['files'][name] = dict(size=1, sha256=hashlib.sha256(files[name]).hexdigest())
        bad = self.build_modified('case', mutate)
        self.invoke('--release', str(bad), status=1)
        self.assertFalse(self.prefix.exists())

    def test_dry_run_creates_nothing_and_does_not_call_mutations(self):
        before = hashes(self.temp)
        with patch.object(ins, 'create_root', side_effect=AssertionError('write')), patch.object(ins, 'transaction', side_effect=AssertionError('mutation')):
            output = self.invoke('--release', str(self.v1), '--dry-run')
        self.assertIn('no filesystem writes', output)
        self.assertEqual(hashes(self.temp), before)
        self.assertEqual(list(self.temp.iterdir()), [])

    @unittest.skipIf(os.name == 'nt', 'POSIX shell entry point')
    def test_shell_dry_run_writes_nothing(self):
        result = subprocess.run(['sh', str(ROOT / 'installer/install.sh')] + self.args() +
            ['--release', str(self.v1), '--dry-run'], cwd=str(ROOT), capture_output=True, text=True,
            env=dict(os.environ, TMPDIR=str(ROOT / '.dev/tmp'), PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(list(self.temp.iterdir()), [])

    @unittest.skipUnless(shutil.which('pwsh') or shutil.which('powershell.exe'), 'PowerShell parser unavailable')
    def test_powershell_entry_parses(self):
        executable = shutil.which('pwsh') or shutil.which('powershell.exe')
        path = str(ROOT / 'installer/install.ps1').replace("'", "''")
        command = ("$tokens=$null; $errors=$null; "
                   "[System.Management.Automation.Language.Parser]::ParseFile('" + path +
                   "', [ref]$tokens, [ref]$errors) > $null; if ($errors.Count) { exit 1 }")
        result = subprocess.run([executable, '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_paired_worker_drain_timeout_keeps_current_and_never_stops_services(self):
        self.invoke('--release', str(self.v1), role='worker')
        (self.state / 'worker/agent.json').write_text(json.dumps(dict(node_id='node-a', controller='https://example.invalid')))
        (self.state / 'worker/node.token').write_text('identity')
        with patch.object(ins, 'drain_client', return_value=(object(), 'node-a')):
            with patch.object(ins, 'drain_and_wait', side_effect=ValueError('drain timed out')):
                with patch.object(ins.Services, 'stop', side_effect=AssertionError('must not stop')):
                    self.invoke('--release', str(self.v2), role='worker', status=1)
        self.assertEqual(ins.current_version(self.prefix), '1.0.0')

    @unittest.skipIf(os.name == 'nt', 'POSIX shell entry point; Windows uses install.ps1')
    def test_shell_install_and_repeat_preserve_node_identity_and_state(self):
        worker = self.state / 'worker'
        worker.mkdir(parents=True)
        (worker / 'agent.json').write_text(json.dumps(dict(node_id='node-a', controller='https://example.invalid')))
        (worker / 'node.token').write_text('unchanged-node-token')
        (worker / 'business.txt').write_bytes(b'unchanged\x00business data')
        identity_before = hashes(worker)
        args = ['sh', str(ROOT / 'installer/install.sh')] + self.args('worker') + [
            '--release', str(self.v1), '--pairing-code', 'unused-code', '--node-id', 'node-a',
            '--controller-url', 'https://example.invalid']
        env = dict(os.environ, TMPDIR=str(ROOT / '.dev/tmp'), PYTHONDONTWRITEBYTECODE='1')
        first = subprocess.run(args, cwd=str(ROOT), env=env, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        for name, digest in identity_before.items():
            self.assertEqual(hashes(worker)[name], digest)
        original = hashes(self.state)
        second = subprocess.run(args, cwd=str(ROOT), env=env, capture_output=True, text=True)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn('Already installed', second.stdout)
        self.assertEqual(hashes(self.state), original)
        self.assertEqual((worker / 'node.token').read_text(), 'unchanged-node-token')

    def test_upgrade_rollback_backup_and_uninstall_preserve_history(self):
        self.invoke('--release', str(self.v1))
        data = self.state / 'controller'
        (data / 'business.txt').write_text('history survives')
        token = (data / 'admin.token').read_bytes()
        self.invoke('--release', str(self.v2))
        self.assertEqual(ins.current_version(self.prefix), '2.0.0')
        self.assertTrue(list((self.state / 'backups').glob('*/backup-manifest.json')))
        self.invoke('--rollback')
        self.assertEqual(ins.current_version(self.prefix), '1.0.0')
        self.assertEqual((data / 'admin.token').read_bytes(), token)
        before = hashes(self.state)
        self.invoke('--uninstall', '--purge-code')
        self.assertIsNone(ins.current_version(self.prefix))
        self.assertFalse((self.prefix / 'versions').exists())
        self.assertEqual(hashes(self.state), before)
        self.invoke('--uninstall', '--purge-code')

    def test_default_uninstall_retains_archived_code_and_supports_reinstall(self):
        self.invoke('--release', str(self.v1))
        self.invoke('--release', str(self.v2))
        token = (self.state / 'controller/admin.token').read_bytes()
        self.invoke('--uninstall')
        self.assertTrue((self.prefix / 'versions/1.0.0').exists())
        self.assertFalse((self.prefix / 'versions/2.0.0').exists())
        self.invoke('--release', str(self.v2))
        self.assertEqual(ins.current_version(self.prefix), '2.0.0')
        self.assertEqual((self.state / 'controller/admin.token').read_bytes(), token)

    def test_first_install_failure_can_be_retried_without_resetting_state(self):
        with patch.object(ins, 'wait_doctor', side_effect=RuntimeError('first health failure')):
            self.invoke('--release', str(self.v1), status=1)
        self.assertIsNone(ins.current_version(self.prefix))
        token = (self.state / 'controller/admin.token').read_bytes()
        self.invoke('--release', str(self.v1))
        self.assertEqual(ins.current_version(self.prefix), '1.0.0')
        self.assertEqual((self.state / 'controller/admin.token').read_bytes(), token)

    def test_schema_version_and_unversioned_ddl_block_rollback(self):
        self.invoke('--release', str(self.v1))
        self.invoke('--release', str(self.v2))
        database = self.state / 'controller/controller.db'
        with closing(sqlite3.connect(str(database))) as db, db:
            db.execute('PRAGMA user_version=1')
        text = self.invoke('--rollback', status=1)
        self.assertIn('authority_generation', text)
        self.assertEqual(ins.current_version(self.prefix), '2.0.0')
        with closing(sqlite3.connect(str(database))) as db, db:
            db.execute('PRAGMA user_version=0')
            db.execute('CREATE TABLE future_schema(value TEXT)')
        self.invoke('--rollback', status=1)
        self.assertEqual(ins.current_version(self.prefix), '2.0.0')

    def test_failed_doctor_automatically_returns_previous_version(self):
        self.invoke('--release', str(self.v1))
        with patch.object(ins, 'wait_doctor', side_effect=RuntimeError('health failed')):
            output = self.invoke('--release', str(self.v2), status=1)
        self.assertIn('Restored previous version', output)
        self.assertEqual(ins.current_version(self.prefix), '1.0.0')
        self.assertEqual(ins.read_json(self.prefix / 'installation.json')['version'], '1.0.0')
        self.assertEqual(ins.doctor(ins.read_json(self.prefix / 'installation.json')['config'], offline=True)['manifest'], 'verified')

    def test_partial_service_stop_failure_restarts_old_services_without_switch(self):
        self.invoke('--release', str(self.v1), role='worker')
        old = ins.read_json(self.prefix / 'installation.json')
        config = dict(old['config'], service=True)
        old['config'] = config
        manifest, files = ins.read_release(self.v2)
        target_manager, old_manager = Mock(), Mock()
        old_manager.stop.side_effect = RuntimeError('simulated partial stop')
        # All service operations are fakes; the only writes are staging under our test prefix.
        with patch.object(ins, 'Services', side_effect=[target_manager, old_manager]):
            with self.assertRaisesRegex(RuntimeError, 'partial stop'):
                with redirect_stderr(io.StringIO()):
                    ins.transaction(config, old, manifest, files, ins.parser().parse_args(['--no-service']))
        old_manager.start.assert_called_once_with()
        target_manager.start.assert_not_called()
        self.assertEqual(ins.current_version(self.prefix), '1.0.0')

    def test_automatic_rollback_refuses_incompatible_schema(self):
        self.invoke('--release', str(self.v1))
        def fail_after_migration(*args):
            with closing(sqlite3.connect(str(self.state / 'controller/controller.db'))) as db, db:
                db.execute('PRAGMA user_version=1')
            raise RuntimeError('migrated and failed')
        with patch.object(ins, 'wait_doctor', side_effect=fail_after_migration):
            output = self.invoke('--release', str(self.v2), status=1)
        self.assertIn('Automatic rollback refused/failed', output)
        self.assertEqual(ins.current_version(self.prefix), '2.0.0')

    def test_changed_version_cannot_replace_immutable_install(self):
        self.invoke('--release', str(self.v1))
        bad = self.build_modified('1.0.0', lambda manifest, files: manifest.update(built_at='2099-01-01T00:00:00+00:00'))
        self.invoke('--release', str(bad), status=1)
        self.assertEqual(ins.current_version(self.prefix), '1.0.0')

    def test_no_service_upgrade_refuses_a_running_manual_worker(self):
        self.invoke('--release', str(self.v1), role='worker')
        with AgentLock(self.state / 'worker'):
            self.invoke('--release', str(self.v2), role='worker', status=1)
        self.assertEqual(ins.current_version(self.prefix), '1.0.0')

    def test_paths_cannot_overlap_and_symlink_version_root_is_refused(self):
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertEqual(ins.main(['--role', 'controller', '--release', str(self.v1), '--no-service',
                '--prefix', str(self.prefix), '--state-dir', str(self.prefix / 'state')]), 1)
        self.assertFalse(self.prefix.exists())
        self.invoke('--release', str(self.v1))
        if os.name != 'nt':
            versions = self.prefix / 'versions'
            versions.rename(self.prefix / 'moved')
            versions.symlink_to(self.prefix / 'moved', target_is_directory=True)
            self.invoke('--release', str(self.v2), status=1)

    def test_doctor_offline_reports_unknown_and_detects_tampering(self):
        self.invoke('--release', str(self.v1))
        config = ins.read_json(self.prefix / 'installation.json')['config']
        result = ins.doctor(config, offline=True)
        self.assertEqual(result['services']['controller'], 'unknown')
        self.assertEqual(result['components']['controller'], 'unknown')
        self.assertTrue(result['ok'])
        (self.prefix / 'versions/1.0.0/agent_collab/cli.py').write_text('tampered')
        self.assertFalse(ins.doctor(config, offline=True)['ok'])

    def test_service_templates_use_fixed_code_and_current_user(self):
        self.invoke('--release', str(self.v1))
        config = ins.read_json(self.prefix / 'installation.json')['config']
        version = self.prefix / 'versions/1.0.0'
        linux = ins.render_services(dict(config, role='all-in-one', system='Linux'), version)
        controller = linux['agent-collab-controller.service'].decode()
        self.assertIn('User=agent-collab', controller)
        self.assertIn('NoNewPrivileges=true', controller)
        self.assertIn('ProtectSystem=strict', controller)
        # systemd 双引号内反斜杠需转义；在 Windows 上运行本测试时路径含反斜杠。
        quoted = lambda path: str(path).replace('\\', '\\\\')
        self.assertNotIn(str(self.prefix / 'current'), controller)
        self.assertIn('ReadWritePaths="' + quoted(self.state / 'controller') + '"', controller)
        # systemd 不解析 WorkingDirectory 的引号：必须是未加引号的绝对路径（ECS 实装回归）。
        for unit in linux.values():
            line = next(x for x in unit.decode().splitlines() if x.startswith('WorkingDirectory='))
            self.assertEqual(line, 'WorkingDirectory=' + str(version).replace('%', '%%'))
        self.assertNotIn('User=', linux['agent-collab-worker.service'].decode())
        mac = ins.render_services(dict(config, role='worker', system='Darwin'), version)
        plist = plistlib.loads(mac['agent-collab-worker.plist'])
        self.assertNotIn('UserName', plist)
        self.assertEqual(plist['WorkingDirectory'], str(version))
        self.assertIn('--foreground', plist['ProgramArguments'])
        windows = ins.render_services(dict(config, role='worker', system='Windows'), version, 'S-1-5-21-100-200-300-1001')
        text = windows['agent-collab-worker.xml'].decode('utf-16')
        ET.fromstring(text)
        self.assertNotIn('SYSTEM', text.upper())
        self.assertIn('InteractiveToken', text)
        self.assertIn('LeastPrivilege', text)
        self.assertIn('launcher.py', text)
        with self.assertRaises(ValueError):
            ins.render_services(dict(config, role='worker', system='Windows'), version, 'S-1-5-18')

    def test_windows_pointer_switch_is_atomic_and_validated(self):
        self.prefix.mkdir()
        ins.switch_current(self.prefix, '1.0.0', 'Windows')
        self.assertEqual(ins.current_version(self.prefix, 'Windows'), '1.0.0')
        ins.switch_current(self.prefix, '2.0.0', 'Windows')
        self.assertEqual(ins.current_version(self.prefix, 'Windows'), '2.0.0')
        self.assertEqual(sorted(p.name for p in self.prefix.iterdir()), ['current.txt'])
        (self.prefix / 'current.txt').write_text('../escape')
        with self.assertRaises(ValueError):
            ins.current_version(self.prefix, 'Windows')


class DrainTests(ControllerCase):
    def client(self):
        def transport(method, url, headers, body):
            response = self.app.handle(method, url.split('https://fixture')[1], headers, body)
            return response.status, dict(response.headers), response.body
        return Client('https://fixture', self.admin, transport=transport)

    def test_real_api_drains_and_preserves_existing_drain_state(self):
        self.node('node-a')
        client = self.client()
        prior = ins.drain_and_wait(client, 'node-a', 0)
        self.assertFalse(prior)
        self.assertTrue(ins.get_node(client, 'node-a')['drain'])
        self.assertTrue(ins.drain_and_wait(client, 'node-a', 0))
        ins.set_drain(client, 'node-a', False)
        self.assertFalse(ins.get_node(client, 'node-a')['drain'])

    def test_active_attempt_timeout_restores_drain_without_stopping_worker(self):
        self.node('node-a')
        self.job()
        client = self.client()
        with self.assertRaisesRegex(ValueError, 'timed out'):
            ins.drain_and_wait(client, 'node-a', 0)
        self.assertFalse(ins.get_node(client, 'node-a')['drain'])
        self.assertEqual(ins.get_node(client, 'node-a')['active_attempts'], 1)

    def test_missing_active_count_is_not_treated_as_idle(self):
        self.node('node-a')
        client = self.client()
        original = ins.get_node
        def missing(*args):
            result = original(*args)
            result.pop('active_attempts', None)
            return result
        with patch.object(ins, 'get_node', side_effect=missing):
            with self.assertRaisesRegex(ValueError, 'timed out'):
                ins.drain_and_wait(client, 'node-a', 0)


if __name__ == '__main__':
    unittest.main()
