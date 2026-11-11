"""使用合成记录验证盘点、保密和文件不变性；不连接任何服务。"""
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent_collab import preflight
from agent_collab.installer import render_services
from agent_collab.store import SCHEMA


ROOT = Path(__file__).resolve().parents[1]
TOP_KEYS = {'schema', 'generated_at', 'read_only', 'host', 'controller', 'node_installs',
            'installations', 'project_bindings', 'legacy', 'plan', 'conflicts', 'risks', 'missing',
            'cutover_preconditions'}
PRECONDITIONS = {'no_active_attempts', 'no_non_terminal_jobs', 'dispatch_paused', 'backup_present',
                 'single_controller_identity', 'no_duplicate_node_ids', 'nodes_declare_models', 'legacy_tasks_idle'}


def snapshot(root):
    result = {}
    for path in sorted(root.rglob('*')):
        info = path.lstat()
        content = os.readlink(str(path)) if path.is_symlink() else path.read_bytes() if path.is_file() else None
        result[str(path.relative_to(root))] = (info.st_mode, info.st_size, info.st_mtime_ns, content)
    return result


class PreflightTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / '.dev' / 'tmp'
        parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=str(parent)))
        self.addCleanup(shutil.rmtree, self.root)
        self.controller = self.root / 'state' / 'controller'
        self.node = self.root / 'node'
        self.install = self.root / 'install'
        self.legacy = self.root / 'legacy'
        self.scans = self.root / 'documents'
        self.canaries = ['fake-admin-credential-111', 'fake-node-credential-222',
                         'fake-config-credential-333', 'fake-install-credential-444',
                         'fake-legacy-credential-555', 'fake-scan-credential-666',
                         'fake-node-hash-777', 'fake-database-hash-888', 'fake-environment-999']

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def put(self, kind, ident, value):
        with closing(sqlite3.connect(str(self.controller / 'controller.db'))) as db, db:
            db.execute('INSERT OR REPLACE INTO records VALUES(?,?,?)', (kind, ident, json.dumps(value)))

    def seed(self, idle=False):
        self.controller.mkdir(parents=True)
        with closing(sqlite3.connect(str(self.controller / 'controller.db'))) as db, db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO events(event_id,job_id,data) VALUES('event-a','job-a','{}')")
            db.execute("INSERT INTO events(event_id,job_id,data) VALUES('event-b','job-a','{}')")
        self.put('meta', 'controller', dict(controller_id='controller-a', authority_generation=3,
                                          dispatch_paused=idle, version=5))
        self.put('job', 'job-a', dict(job_id='job-a', work_package_id='package-a', title='合成任务',
                                    status='completed' if idle else 'running', integration_status='none',
                                    contract=dict(harness='codex', integration=dict(target_node_id='node-a',
                                                  target_path='/Volumes/shared/project'))))
        self.put('attempt', 'attempt-a', dict(attempt_id='attempt-a', job_id='job-a', node_id='node-a',
                                            status='completed' if idle else 'leased', epoch=2, authority_generation=3))
        self.put('node', 'node-a', dict(node_id='node-a', agent_version='test', physical_host_id='host-a',
                                      drain=False, last_seen_at='2026-01-01T00:00:00Z',
                                      runtime_profile=dict(user='fixture-user'),
                                      capabilities=dict(os='synthetic', arch='test', harness={'codex': 'test'},
                                                        **({'models': {}} if idle else {}))))
        self.put('node_token', 'node-a', dict(hash=self.canaries[7]))
        self.put('client', 'client-a', dict(revoked_at=None, token=self.canaries[7]))
        self.put('client', 'client-b', dict(revoked_at='2026-01-01T00:00:00Z'))
        self.put('session', 'session-a', dict(cookie=self.canaries[7]))
        self.put('settings', 'models', dict(revision=4))
        self.put('coordinator', 'package-a', dict(holder_kind='managed', expires_at='2999-01-01T00:00:00Z',
                                                authority_generation=3, token_hash=self.canaries[7]))
        self.put('coordinator', 'package-b', dict(holder_kind='external', expires_at='2000-01-01T00:00:00Z',
                                                authority_generation=3, token_hash=self.canaries[7]))
        self.write(self.controller / 'config.json', dict(public_origin='https://controller.invalid',
                   deployment_profile='local', password=self.canaries[2],
                   managed_coordinator=dict(enabled=True, profile_id='fixture'),
                   standby_profiles=dict(fixture=dict(harness='claude', model='test-model', effort='high', node_ids=['node-a']))))
        (self.controller / 'admin.token').write_text(self.canaries[0])
        (self.controller / 'admin.token').chmod(0o600)
        blob = self.controller / 'blobs' / 'aa' / 'bb' / ('a' * 64)
        blob.parent.mkdir(parents=True)
        blob.write_bytes(b'blob')
        self.write(self.controller.parent / 'backups' / 'set-a' / 'backup-manifest.json', {'version': 1})
        self.write(self.node / 'agent.json', dict(node_id='node-a', controller='https://controller.invalid',
                   controller_id='controller-a', authority_generation=3, physical_host_id='host-a', failure_domain='domain-a',
                   slots=2, host_slots=2, labels=['fixture'], codex_command=['codex'], claude_command=['claude'],
                   codex_home='/model-home', disabled_harnesses={}, node_token_sha256=self.canaries[6]))
        (self.node / 'node.token').write_text(self.canaries[1])
        self.write(self.node / 'status.json', dict(version='test', git_commit='a' * 40, pid=123,
                   last_heartbeat_ok_at='2026-01-01T00:00:00Z', auth_health={'codex': 'ok'}, auth_checked_at=None))
        self.write(self.node / 'attempts' / 'attempt-a' / 'meta.json', dict(state='reported' if idle else 'running'))
        self.write(self.node / 'attempts' / 'attempt-b' / 'meta.json', dict(state='reported'))
        self.write(self.install / 'installation.json', dict(version='test-v1', config=dict(
                   system='Linux', role='worker', service=True, admin_token=self.canaries[3],
                   nested={'password': self.canaries[3]}, env={'VALUE': self.canaries[8]})))
        (self.install / 'versions' / 'test-v1').mkdir(parents=True)
        (self.install / 'current').symlink_to('versions/test-v1')
        services = self.install / 'services'
        services.mkdir()
        (services / 'agent-collab-worker.service').write_text(
            '[Service]\nUser=fixture-user\nExecStart=python -m agent_collab.node_agent run\n'
            'Environment=VALUE=' + self.canaries[8] + '\n')
        self.write(self.legacy / 'tasks' / 'synthetic' / 'task-a' / 'state.json',
                   dict(task_id='task-a', status='completed' if idle else 'running', token=self.canaries[4]))
        self.scans.mkdir()
        (self.scans / 'guide.md').write_text(
            'python launcher.py codex-collaborate\n'
            'skills/example/SKILL.md 使用 Skill\n'
            'workbench exec submit\n'
            '\\\\filehost\\share\\project\n'
            'ac_session /session 控制台登录\n'
            'agent-collab --token-file admin.token\n'
            'launcher.py password="' + self.canaries[5] + '"\n'
            'launcher.py Authorization: Bearer ' + self.canaries[0] + '\n'
            'launcher.py --token ' + self.canaries[1] + '\n'
            'launcher.py export VALUE=' + self.canaries[8] + '\n', encoding='utf-8')

    def report(self, **overrides):
        args = dict(controller_data_dir=self.controller, node_data_dirs=[self.node], install_roots=[self.install],
                    legacy_state_roots=[self.legacy], scan_paths=[self.scans])
        args.update(overrides)
        return preflight.inventory(**args)

    def test_inventory_is_read_only_and_redacts_all_sources(self):
        self.seed()
        before = snapshot(self.root)
        with patch('subprocess.run') as run, patch('subprocess.Popen') as popen, \
                patch('agent_collab.store.Store', side_effect=AssertionError('不得实例化 Store')), \
                patch('agent_collab.controller.App', side_effect=AssertionError('不得实例化 App')):
            report = self.report()
        run.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(snapshot(self.root), before)
        serialized = json.dumps(report, ensure_ascii=False)
        for secret in self.canaries:
            self.assertNotIn(secret, serialized)
        self.assertEqual(set(report), TOP_KEYS)
        c = report['controller']
        self.assertTrue(c['database']['readable'])
        self.assertIsNone(c['database']['error'])
        self.assertEqual(c['database']['record_counts']['job'], 1)
        self.assertEqual(c['database']['events'], dict(count=2, min_seq=1, max_seq=2))
        self.assertIn('records', c['database']['tables'])
        self.assertEqual(c['meta']['metadata_version'], 5)
        self.assertEqual(c['config']['managed_coordinator']['status'], 'ready')
        self.assertEqual(c['jobs']['by_status'], {'running': 1})
        self.assertEqual(c['jobs']['non_terminal'][0]['harness'], 'codex')
        self.assertEqual(c['attempts']['active'][0]['epoch'], 2)
        self.assertEqual([x['active'] for x in c['coordinator_leases']], [True, False])
        self.assertEqual(c['node_credentials'], {'count': 1})
        self.assertEqual(c['clients'], dict(count=2, revoked=1))
        self.assertEqual(c['sessions'], {'count': 1})
        self.assertEqual(c['model_settings'], dict(present=True, revision=4))
        self.assertEqual(c['blobs'], dict(count=1, bytes=4))
        self.assertEqual(c['backups']['sets'][0]['name'], 'set-a')
        if os.name == 'posix':  # Windows 的权限位不反映访问控制
            self.assertEqual(c['secret_files'][0]['mode'], '0o600')
        self.assertEqual(report['node_installs'][0]['attempts'], dict(total=2, unreported=['attempt-a']))
        self.assertEqual(report['installations'][0]['current_version'], 'test-v1')
        self.assertIsNone(report['installations'][0]['services'][0]['enabled'])

    def test_secret_files_are_never_opened_even_under_scan(self):
        self.seed()
        for name in ('auth.json', '.credentials', '.credentials.extra', 'identity.pem', 'identity.key',
                     'id_rsa', 'id_ed25519', 'id_ed25519_custom', 'id_ecdsa', 'other.token'):
            (self.scans / name).write_text('launcher.py ' + self.canaries[0])
        original = Path.open

        def guard(path, *args, **kwargs):
            if preflight._secret(path):
                raise AssertionError('不得读取凭据文件')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'open', guard):
            report = self.report(scan_paths=[self.root])
        self.assertNotIn(self.canaries[0], json.dumps(report))
        self.assertFalse(any(preflight._secret(Path(r['file'])) for r in report['legacy']['references']))

    def test_three_conflicts_and_false_preconditions(self):
        self.seed()
        other = self.root / 'node-other'
        self.write(other / 'agent.json', dict(node_id='node-a', controller_id='controller-b', authority_generation=4))
        report = self.report(node_data_dirs=[self.node, other])
        self.assertEqual({c['code'] for c in report['conflicts']},
                         {'duplicate_node_id', 'controller_identity_mismatch', 'authority_generation_ahead'})
        checks = {c['id']: c['ok'] for c in report['cutover_preconditions']}
        self.assertEqual(set(checks), PRECONDITIONS)
        self.assertEqual(checks, {key: key == 'backup_present' for key in PRECONDITIONS})

    def test_all_preconditions_true_for_idle_fixture(self):
        self.seed(idle=True)
        report = self.report()
        self.assertEqual({c['id'] for c in report['cutover_preconditions']}, PRECONDITIONS)
        self.assertTrue(all(c['ok'] is True for c in report['cutover_preconditions']))

    def test_bindings_all_path_kinds_and_empty_binding_omitted(self):
        self.seed()
        for i, path in enumerate(('\\\\filehost\\share', '//filehost/share', 'smb://filehost/share',
                                  '/Volumes/shared/project', '/workspace/project', 'C:\\workspace\\project', 'relative')):
            self.put('job', 'binding-' + str(i), dict(job_id=str(i), status='completed',
                     contract=dict(integration=dict(target_path=path))))
        self.put('job', 'no-binding', dict(status='failed', contract=dict(integration={})))
        bindings = self.report()['project_bindings']
        kinds = {b['target_path']: b['path_kind'] for b in bindings}
        self.assertEqual(len(bindings), 8)
        self.assertEqual(set(kinds.values()), {'unc', 'smb', 'local', 'unknown'})
        self.assertEqual(kinds['C:\\workspace\\project'], 'local')
        self.assertEqual(kinds['//filehost/share'], 'unc')
        self.assertEqual(kinds['relative'], 'unknown')

    def test_references_all_kinds_line_numbers_and_truncation(self):
        self.seed()
        (self.scans / 'long.txt').write_text('launcher.py ' + 'x' * 400)
        refs = self.report()['legacy']['references']
        self.assertEqual({r['kind'] for r in refs}, {'launcher', 'skill', 'workbench', 'smb', 'web_console', 'admin_token'})
        self.assertTrue(all(0 < r['line'] and len(r['text']) <= 200 for r in refs))
        self.assertTrue(any(r['line'] == 3 and r['kind'] == 'workbench' for r in refs))

    def test_text_scan_skips_binary_large_files_and_symlinks(self):
        self.seed()
        (self.scans / 'binary').write_bytes(b'launcher.py\x00secret')
        (self.scans / 'large').write_bytes(b'launcher.py' + b'x' * preflight.MAX_SCAN_BYTES)
        (self.scans / 'invalid-utf8').write_bytes(b'launcher.py\xff')
        (self.scans / 'alias').symlink_to(self.controller / 'admin.token')
        (self.scans / 'loop').symlink_to(self.scans, target_is_directory=True)
        report = self.report()
        self.assertFalse(any(Path(r['file']).name in ('binary', 'large', 'invalid-utf8', 'alias')
                             for r in report['legacy']['references']))
        self.assertIn('scan_too_large', {r['code'] for r in report['risks']})

    def test_multiline_json_secrets_and_echoes_are_redacted(self):
        self.seed()
        canary = 'fake-nested-credential-123'
        (self.scans / 'secret.json').write_text(json.dumps({
            'access_token': {'nested': [canary]}, 'example': 'launcher.py ' + canary}, indent=2))
        self.put('job', 'echo', dict(status='running', title=canary, secret=canary))
        report = self.report()
        self.assertNotIn(canary, json.dumps(report))

    def test_json_command_arguments_do_not_leak_credentials(self):
        self.seed()
        canary = 'fake-command-credential-321'
        self.write(self.scans / 'command.json', {'command': ['launcher.py', '--token', canary]})
        config = json.loads((self.node / 'agent.json').read_text())
        config['codex_command'] = ['codex', '--password', canary]
        self.write(self.node / 'agent.json', config)
        self.assertNotIn(canary, json.dumps(self.report()))

    def test_plan_follows_observed_objects(self):
        self.seed()
        plan = self.report()['plan']
        self.assertTrue(all(plan.values()))
        self.assertTrue(any(p['what'] == 'controller.db' for p in plan['keep']))
        self.assertTrue(any(p['what'] == '模型 CLI 登录状态' for p in plan['keep']))
        self.assertTrue(any(p['what'] == '项目目录绑定' for p in plan['migrate']))
        self.assertTrue(any('客户端凭据' in p['new_entry'] for p in plan['migrate']))
        self.assertTrue(any('POST /clients/pair' in p['new_entry'] for p in plan['retire']))
        for bucket in plan.values():
            self.assertTrue(all(set(p) == {'what', 'where', 'why', 'new_entry'} for p in bucket))

    def test_no_input_has_complete_shape_and_unknown_controller_checks(self):
        with patch('subprocess.run') as run, patch('subprocess.Popen') as popen:
            report = preflight.inventory()
        run.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(set(report), TOP_KEYS)
        self.assertEqual(report['schema'], 1)
        self.assertTrue(report['read_only'])
        self.assertTrue(report['generated_at'].endswith('Z'))
        self.assertEqual(set(report['host']), {'os', 'arch', 'python', 'user', 'hostname'})
        self.assertIsNone(report['controller'])
        for key in ('node_installs', 'installations', 'project_bindings', 'conflicts', 'risks'):
            self.assertEqual(report[key], [])
        self.assertEqual(report['legacy'], dict(launchers=[], references=[]))
        self.assertEqual(report['plan'], dict(migrate=[], keep=[], archive=[], retire=[]))
        self.assertEqual({m['code'] for m in report['missing']}, {'controller_data_dir', 'backup'})
        checks = {c['id']: c['ok'] for c in report['cutover_preconditions']}
        self.assertEqual(set(checks), PRECONDITIONS)
        self.assertIsNone(checks['no_active_attempts'])
        self.assertIsNone(checks['single_controller_identity'])

    def call_main(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = preflight.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_out_inside_each_inspected_directory_is_rejected_without_writes(self):
        self.seed()
        for flag, directory in (('--controller-data-dir', self.controller), ('--node-data-dir', self.node),
                                ('--install-root', self.install), ('--legacy-state-root', self.legacy),
                                ('--scan', self.scans)):
            with self.subTest(flag=flag):
                before = snapshot(self.root)
                code, out, _ = self.call_main([flag, str(directory), '--out', str(directory / 'report.json')])
                self.assertEqual((code, out), (2, ''))
                self.assertEqual(snapshot(self.root), before)

    def test_out_rejects_backup_scan_file_symlink_and_hardlink_aliases(self):
        self.seed()
        scan = self.scans / 'guide.md'
        symbolic = self.root / 'alias'
        symbolic.symlink_to(self.node)
        escape = self.node / 'escape'
        escape.symlink_to(self.root)
        hardlink = self.root / 'hardlink'
        os.link(str(self.node / 'agent.json'), str(hardlink))
        cases = [(['--controller-data-dir', str(self.controller)], self.controller.parent / 'backups' / 'out'),
                 (['--scan', str(scan)], scan),
                 (['--node-data-dir', str(self.node)], symbolic / 'out'),
                 (['--node-data-dir', str(self.node)], hardlink),
                 (['--node-data-dir', str(self.node)], escape / 'report.json')]
        for args, output in cases:
            with self.subTest(output=output):
                before = snapshot(self.root)
                code, _, _ = self.call_main(args + ['--out', str(output)])
                self.assertEqual(code, 2)
                self.assertEqual(snapshot(self.root), before)

    def test_main_stdout_outside_output_errors_and_conflicts_exit_zero(self):
        self.seed()
        code, out, _ = self.call_main([])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['schema'], 1)
        other = self.root / 'other'
        self.write(other / 'agent.json', dict(node_id='node-a'))
        output = self.root / 'report.json'
        code, out, _ = self.call_main(['--node-data-dir', str(self.node), '--node-data-dir', str(other), '--out', str(output)])
        self.assertEqual((code, out), (0, ''))
        self.assertEqual(json.loads(output.read_text())['conflicts'][0]['code'], 'duplicate_node_id')
        self.assertEqual(self.call_main(['--not-an-option'])[0], 2)
        self.assertEqual(self.call_main(['--out'])[0], 2)

    def test_database_uses_readonly_uri_and_selects_only(self):
        self.seed()
        original = sqlite3.connect
        statements = []
        uris = []

        def connect(database, **kwargs):
            uris.append((database, kwargs))
            db = original(database, **kwargs)
            db.set_trace_callback(statements.append)
            return db

        with patch('agent_collab.preflight.sqlite3.connect', side_effect=connect):
            self.report()
        self.assertEqual(len(uris), 1)
        self.assertIn('?mode=ro&immutable=1', uris[0][0])
        self.assertTrue(uris[0][1]['uri'])
        self.assertTrue(all(s.startswith('SELECT ') or s == 'PRAGMA journal_mode' for s in statements))

    def test_missing_corrupt_and_malformed_database_do_not_create_files(self):
        for content in (None, b'not a database'):
            with self.subTest(content=content):
                directory = self.root / ('missing' if content is None else 'corrupt')
                directory.mkdir()
                if content is not None:
                    (directory / 'controller.db').write_bytes(content)
                before = snapshot(directory)
                report = preflight.inventory(controller_data_dir=directory)
                self.assertEqual(snapshot(directory), before)
                self.assertFalse(report['controller']['database']['readable'])
                self.assertTrue(report['controller']['database']['error'])
                self.assertEqual({m['code'] for m in report['missing']}, {'database_unreadable', 'backup'})
        self.seed()
        with closing(sqlite3.connect(str(self.controller / 'controller.db'))) as db, db:
            db.execute("UPDATE records SET data='[]' WHERE kind='job'")
        self.assertFalse(self.report()['controller']['database']['readable'])

    def test_live_wal_is_not_ignored_and_no_sidecar_is_modified(self):
        self.seed()
        db = sqlite3.connect(str(self.controller / 'controller.db'))
        self.addCleanup(db.close)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute("INSERT INTO records VALUES('session', 'pending', '{}')")
        db.commit()
        before = snapshot(self.controller)
        report = self.report()
        self.assertEqual(snapshot(self.controller), before)
        self.assertFalse(report['controller']['database']['readable'])
        self.assertEqual(report['controller']['database']['error'], '_UnsafeJournal')
        self.assertIn('sqlite_sidecar_requires_snapshot', {r['code'] for r in report['risks']})
        self.assertIsNone(next(c['ok'] for c in report['cutover_preconditions'] if c['id'] == 'no_active_attempts'))

    def test_snapshot_database_is_read_while_the_live_one_has_a_wal(self):
        self.seed()
        db = sqlite3.connect(str(self.controller / 'controller.db'))
        self.addCleanup(db.close)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute("INSERT INTO records VALUES('session', 'pending', '{}')")
        db.commit()
        copies = self.root / 'snapshot-set'
        copies.mkdir()
        target = sqlite3.connect(str(copies / 'controller-snapshot.db'))
        db.backup(target)
        target.close()
        before = snapshot(self.controller), snapshot(copies)
        report = self.report(controller_db=copies / 'controller-snapshot.db')
        self.assertEqual((snapshot(self.controller), snapshot(copies)), before)
        database = report['controller']['database']
        self.assertEqual((database['readable'], database['source'], database['record_counts']['session']), (True, 'snapshot', 2))  # 夹具自带一条，加上写在 WAL 里的这一条
        self.assertEqual(report['controller']['data_dir'], str(self.controller.resolve()))
        self.assertEqual(self.report()['controller']['database']['source'], 'data_dir')
        # 只有快照（如拉到别处的备份集）时，以它所在目录为数据目录；报告不能写进这个目录。
        alone = preflight.inventory(controller_db=copies / 'controller-snapshot.db')
        self.assertEqual((alone['controller']['data_dir'], alone['controller']['database']['readable']), (str(copies.resolve()), True))
        with redirect_stderr(io.StringIO()):
            self.assertEqual(preflight.main(['--controller-db', str(copies / 'controller-snapshot.db'),
                                             '--out', str(copies / 'report.json')]), 2)
        self.assertFalse((copies / 'report.json').exists())

    def test_closed_wal_database_reads_without_creating_sidecars(self):
        self.seed()
        db = sqlite3.connect(str(self.controller / 'controller.db'))
        db.execute('PRAGMA journal_mode=WAL')
        db.close()
        before = snapshot(self.controller)
        report = self.report()
        self.assertEqual(snapshot(self.controller), before)
        self.assertTrue(report['controller']['database']['readable'])
        self.assertEqual(report['controller']['database']['journal_mode'], 'wal')

    def test_services_match_actual_installer_formats(self):
        for system in ('Linux', 'Darwin', 'Windows'):
            with self.subTest(system=system):
                prefix = self.root / system
                config = dict(prefix=str(prefix), state_dir=str(self.root / 'state'), system=system,
                              role='all-in-one', python='python', bind='127.0.0.1:1', controller_url='https://controller.invalid')
                self.write(prefix / 'installation.json', dict(version='v1', config=config))
                (prefix / 'versions' / 'v1').mkdir(parents=True)
                (prefix / 'current.txt').write_text('v1\n')
                (prefix / 'services').mkdir()
                for name, content in render_services(config, ROOT, user_sid='S-1-5-21-100-100-100-100').items():
                    (prefix / 'services' / name).write_bytes(content)
                before = snapshot(prefix)
                installation = preflight.inventory(install_roots=[prefix])['installations'][0]
                self.assertEqual(snapshot(prefix), before)
                self.assertEqual(installation['current_version'], 'v1')
                self.assertEqual(installation['versions'], ['v1'])
                self.assertEqual(len(installation['services']), 2)
                self.assertEqual({s['kind'] for s in installation['services']},
                                 {{'Linux': 'systemd', 'Darwin': 'launchd', 'Windows': 'schtasks'}[system]})
                self.assertTrue(all(s['command'] and s['enabled'] is None for s in installation['services']))

    def test_service_queries_are_readonly_bounded_and_fail_to_null(self):
        self.seed()
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, b'enabled\n', b'')) as run:
            service = self.report(probe_services=True)['installations'][0]['services'][0]
        self.assertTrue(service['enabled'])
        run.assert_called_once()
        self.assertEqual(run.call_args[0][0], ['systemctl', '--user', 'show', 'agent-collab-worker.service',
                                             '--property=UnitFileState', '--value'])
        self.assertEqual(run.call_args[1]['timeout'], 5)
        for failure in (FileNotFoundError(), subprocess.TimeoutExpired('systemctl', 5)):
            with patch('subprocess.run', side_effect=failure) as run:
                service = self.report(probe_services=True)['installations'][0]['services'][0]
                self.assertIsNone(service['enabled'])
                run.assert_called_once()
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 1, b'', b'private error')) as run:
            report = self.report(probe_services=True)
        self.assertIsNone(report['installations'][0]['services'][0]['enabled'])
        self.assertNotIn('private error', json.dumps(report))

    def test_launchd_and_schtasks_queries_parse_without_reporting_output(self):
        for kind, name, stdout, expected in (
            ('launchd', 'org.agent-collab.worker', b'disabled services = { "org.agent-collab.worker" => false }', True),
            ('launchd', 'org.agent-collab.worker', b'disabled services = { "org.agent-collab.worker" => true }', False),
            ('schtasks', 'agent-collab-worker', b'<Task><Settings><Enabled>false</Enabled></Settings></Task>', False),
        ):
            if kind == 'launchd' and not hasattr(os, 'getuid'):
                continue  # launchd 只在有用户 ID 的系统上查询
            with self.subTest(kind=kind), patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, stdout, b'')) as run:
                self.assertIs(preflight._probe(dict(kind=kind, name=name)), expected)
                run.assert_called_once()
                self.assertIn('print-disabled' if kind == 'launchd' else '/query', run.call_args[0][0])

    def test_service_command_credentials_are_redacted(self):
        self.seed()
        path = self.install / 'services' / 'agent-collab-worker.plist'
        path.write_bytes(plistlib.dumps(dict(Label='org.agent-collab.worker',
                         ProgramArguments=['python', '--token', self.canaries[0]],
                         EnvironmentVariables={'VALUE': self.canaries[8]})))
        report = self.report()
        self.assertNotIn(self.canaries[0], json.dumps(report))
        self.assertNotIn(self.canaries[8], json.dumps(report))

    def test_unknown_legacy_tasks_and_unreadable_optional_json(self):
        self.seed(idle=True)
        bad = self.legacy / 'tasks' / 'synthetic' / 'task-b' / 'state.json'
        self.write(bad, [])
        (self.node / 'status.json').write_text('broken')
        (self.install / 'installation.json').write_text('broken')
        before = snapshot(self.root)
        report = self.report()
        self.assertEqual(snapshot(self.root), before)
        self.assertIsNone(report['node_installs'][0]['status'])
        self.assertIsNone(report['installations'][0]['installation'])
        self.assertEqual(report['legacy']['launchers'][0]['tasks']['by_status'], {'completed': 1, 'unknown': 1})
        self.assertFalse(next(c['ok'] for c in report['cutover_preconditions'] if c['id'] == 'legacy_tasks_idle'))

    def test_missing_legacy_task_tree_is_unknown_not_idle(self):
        self.legacy.mkdir()
        report = preflight.inventory(legacy_state_roots=[self.legacy])
        self.assertIsNone(next(c['ok'] for c in report['cutover_preconditions'] if c['id'] == 'legacy_tasks_idle'))
        self.assertIn('legacy_tasks_unknown', {r['code'] for r in report['risks']})

    def test_released_or_old_generation_coordinator_is_not_active(self):
        self.seed()
        self.put('coordinator', 'released', dict(expires_at='2999-01-01T00:00:00Z', authority_generation=3, token_hash=None))
        self.put('coordinator', 'old', dict(expires_at='2999-01-01T00:00:00Z', authority_generation=2, token_hash='fake-hash'))
        leases = {l['work_package_id']: l for l in self.report()['controller']['coordinator_leases']}
        self.assertFalse(leases['released']['active'])
        self.assertFalse(leases['old']['active'])

    def test_drained_nodes_do_not_block_the_model_catalog_precondition(self):
        self.seed(idle=True)
        declared = lambda: next(c['ok'] for c in self.report()['cutover_preconditions'] if c['id'] == 'nodes_declare_models')
        self.assertTrue(declared())
        old = dict(agent_version='old', physical_host_id='host-b', capabilities=dict(os='linux', arch='x86_64', harness={}))
        self.put('node', 'retired', dict(old, node_id='retired', drain=True))  # 早已停用、记录还在的节点
        self.assertTrue(declared())
        self.put('node', 'still-serving', dict(old, node_id='still-serving', drain=False))
        self.assertFalse(declared())

    def test_packaged_backup_sets_and_status_are_recognized(self):
        self.seed(idle=True)
        backups = self.controller.parent / 'backups'
        packed = backups / '20260101T000000000000Z-abcdef012345'
        packed.mkdir(parents=True)
        (packed / 'backup.tar').write_bytes(b'tar bytes')
        self.write(packed / 'COMPLETE.json', dict(id=packed.name, created_at='2026-01-01T00:00:00.000Z', db_sha256='ab' * 32,
                                               tar_name='backup.tar', tar_bytes=9, tar_sha256='cd' * 32, authority_generation=3))
        self.write(backups / 'status.json', dict(local=dict(last_success_at='2026-01-01T00:00:01.000Z', last_success_id=packed.name, last_error=None),
                                               offsite=dict(last_confirmed_at=None)))
        (backups / '.lock').write_bytes(b'')
        before = snapshot(self.root)
        report = self.report()
        self.assertEqual(snapshot(self.root), before)
        found = {s['name']: s for s in report['controller']['backups']['sets']}
        self.assertEqual((found[packed.name]['kind'], found[packed.name]['created_at'], found[packed.name]['db_sha256']),
                         ('packaged', '2026-01-01T00:00:00.000Z', 'ab' * 32))
        self.assertIn('directory', {s['kind'] for s in found.values()})
        self.assertEqual(report['controller']['backups']['status']['local']['last_success_id'], packed.name)
        self.assertTrue(next(c['ok'] for c in report['cutover_preconditions'] if c['id'] == 'backup_present'))
        self.assertNotIn('backup_set_unrecognized', {r['code'] for r in report['risks']})  # 状态与锁文件不算无法识别的备份集

    def test_unrecognized_backup_entries_do_not_satisfy_precondition(self):
        self.seed()
        (self.controller.parent / 'backups' / 'set-a' / 'backup-manifest.json').unlink()
        report = self.report()
        self.assertEqual(report['controller']['backups']['sets'], [])
        self.assertIn('backup', {m['code'] for m in report['missing']})
        self.assertFalse(next(c['ok'] for c in report['cutover_preconditions'] if c['id'] == 'backup_present'))


if __name__ == '__main__':
    unittest.main()
