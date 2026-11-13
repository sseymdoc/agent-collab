import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

from agent_collab import artifacts, cli, credstore, diagnostics as d

SERVICE_STATUS = d.installer.Services.status
SERVICE_AUTOSTART = d.installer.Services.autostart
SERVICE_OWNERSHIP = d.installer.Services.ownership


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'synth-user'
        self.prefix = self.home / 'code'
        self.state = self.home / 'state'
        self.output = self.root / 'export.zip'
        self.project = self.root / 'private-parent' / 'demo-project'
        self.attempt = self.state / 'worker/attempts/11111111-1111-4111-8111-111111111111'
        self.write(self.prefix / 'installation.json', dict(version='1.2.3', desired={'worker': 'stopped'}, config=dict(
            prefix=str(self.prefix), state_dir=str(self.state), role='worker', service=True, instance='')))
        self.write(self.state / 'worker/agent.json', dict(node_id='synth-node', controller='https://controller.invalid'))
        self.write(self.state / 'worker/project-bindings.json', {'demo': {'path': str(self.project)}})
        self.write(self.state / 'worker/status.json', dict(version='1.2.3', version_dir=str(self.prefix / 'versions/1.2.3'),
            last_heartbeat_ok_at='2026-11-12T00:00:00Z', last_error='failed as synth-user on synth-host',
            input='task-input-must-not-be-read-into-summary'))
        self.write(self.prefix / 'versions/1.2.3/release-manifest.json', dict(version='1.2.3', git_commit='abc123',
            git_dirty=False, protocol_version=1, features=['remote_apply'], controller_schema_min=0, controller_schema_max=1,
            files={'do-not-export.py': 'source-must-not-appear'}))
        self.write(self.home / '.codex/auth.json', {'token': 'official-codex-secret'})
        self.write(self.home / '.claude/settings.json', {'token': 'official-claude-secret'})
        self.write(self.state / 'controller/controller.db', 'database-must-not-be-read')
        self.write(self.state / 'worker/node.token', 'fake-node-short')
        self.write(self.state / 'controller/admin.token', 'fake-admin-short')
        self.write(self.attempt / 'lease.token', 'fake-lease-short')
        self.write(self.attempt / 'stdout.jsonl', 'TASK-OUTPUT password="fake-task-password"')
        self.write(self.attempt / 'stderr.log', 'error TASK-ERROR fake-lease-short')
        for target, value in (('pathlib.Path.home', self.home), ('getpass.getuser', 'synth-user'),
                              ('platform.node', 'synth-host'), ('agent_collab.release.release_info', {'version': '1.2.3', 'git_commit': 'abc123'}),
                              ('agent_collab.release.version_directory', self.prefix / 'versions/1.2.3'),
                              ('agent_collab.installer.Services.status', {'worker': 'inactive'}),
                              ('agent_collab.installer.Services.autostart', {'worker': 'disabled'}),
                              ('agent_collab.installer.Services.ownership', {'worker': 'this_prefix'}),
                              ('agent_collab.installer.service_context', {'worker': {'scope': 'user-session'}})):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value) if isinstance(value, dict) else value, encoding='utf-8')

    def run_cli(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = d.main(['export', '--prefix', str(self.prefix), '--state-dir', str(self.state),
                             '--out', str(self.output)] + list(extra))
        return status, out.getvalue(), err.getvalue()

    def report(self):
        with zipfile.ZipFile(self.output) as archive:
            return json.loads(archive.read('diagnostics.json'))

    def test_export_redacts_synthetic_secrets_and_never_opens_excluded_files(self):
        jwt = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlIn0.c3ludGhldGljc2lnbmF0dXJl'
        random = 'Q7uN4zY9tR2kP8mV6xB3wF5hJ1cD0sLa'
        hex_token = '0123456789abcdef0123456789abcdef'
        secrets = ['fake-node-short', 'fake-admin-short', 'fake-lease-short', 'fake-bearer', 'fake-coordinator',
                   'fake-password', random, hex_token, jwt, 'sk-fake-api-key', 'FAKE-PRIVATE-LINE']
        log = '\n'.join(['error ' + str(self.home) + ' synth-user synth-host ' + str(self.project),
                         'fake-node-short fake-admin-short fake-lease-short', 'Authorization: Bearer fake-bearer',
                         'X-Coordinator-Token: fake-coordinator', 'password="fake-password"', random, hex_token, jwt,
                         'sk-fake-api-key', '-----BEGIN PRIVATE KEY-----\nFAKE-PRIVATE-LINE\n-----END PRIVATE KEY-----'])
        self.write(self.state / 'worker/agent.log', log)
        opened = []
        original, original_os_open = io.open, os.open

        def check(file):
            if isinstance(file, (str, os.PathLike)):
                path = str(file)
                opened.append(path)
                self.assertNotIn('.codex', path)
                self.assertNotIn('.claude', path)
                self.assertNotIn('controller.db', path)
                self.assertNotIn('stdout.jsonl', path)
                self.assertNotIn('stderr.log', path)

        def guarded(file, *args, **kwargs):
            check(file)
            return original(file, *args, **kwargs)

        def guarded_os_open(file, *args, **kwargs):
            check(file)
            return original_os_open(file, *args, **kwargs)

        with mock.patch('io.open', side_effect=guarded), mock.patch('os.open', side_effect=guarded_os_open), \
                mock.patch('agent_collab.cli.request') as request, \
                mock.patch('socket.create_connection') as network, mock.patch('agent_collab.cli.profile_token') as credentials:
            status, output, error = self.run_cli()
        self.assertEqual((status, error), (0, ''))
        request.assert_not_called()
        network.assert_not_called()
        credentials.assert_not_called()
        with zipfile.ZipFile(self.output) as archive:
            exported = '\n'.join(archive.read(name).decode('utf-8') for name in archive.namelist())
            self.assertEqual(set(archive.namelist()), {'diagnostics.json', 'logs/000.txt'})
        for secret in secrets + [str(self.home), 'synth-user', 'synth-host', str(self.project), 'source-must-not-appear']:
            self.assertNotIn(secret, exported)
            self.assertNotIn(secret, output)
        report = self.report()
        self.assertEqual(report['logs'][0]['redactions']['known_token'], 3)
        self.assertEqual(report['logs'][0]['redactions']['authorization'], 2)
        self.assertEqual(report['logs'][0]['redactions']['private_key'], 1)
        self.assertEqual(report['logs'][0]['redactions']['jwt'], 1)
        self.assertEqual(report['logs'][0]['redactions']['random_token'], 1)
        self.assertEqual(report['logs'][0]['redactions']['hex_token'], 1)
        self.assertEqual(report['logs'][0]['redactions']['api_key'], 1)
        totals = d.Counter()
        for record in list(report['sections'].values()) + report['logs']:
            totals.update(record.get('redactions', {}))
        self.assertEqual(report['redactions'], dict(totals))
        self.assertIn('demo-project', exported)
        self.assertEqual(report['sections']['deployment']['data']['desired'], {'worker': 'stopped'})
        self.assertEqual(json.loads(output), d.preview(self.output))

    def test_redactor_counts_fields_paths_and_jsonl_exactly(self):
        redactor = d.Redactor(home='/home/synth-user', username='synth-user', hostname='synth-host')
        clean = redactor.value({'token': 'tiny', 'secret': 'small', 'password': 'short', 'code': 123,
                                'key': 'mini', 'clientToken': 'hidden', 'note': '/home/synth-user/a synth-user synth-host'})
        self.assertEqual(dict(redactor.counts), {'field': 6, 'home': 1, 'username': 1, 'hostname': 1})
        self.assertEqual(clean['note'], '~/a <user> <host>')
        value = redactor.text(json.dumps({'password': 'with spaces', 'path': r'C:\private\demo',
                                         'message': '-----BEGIN PRIVATE KEY-----\nSHORT\n-----END PRIVATE KEY-----'}))
        self.assertNotIn('SHORT', value)
        self.assertNotIn('with spaces', value)
        self.assertIn('demo', value)
        self.assertEqual(redactor.counts['private_key'], 1)

    def test_log_tail_and_total_limit_record_original_sizes(self):
        log = 'OLD-HEAD\n' + ('line\n' * 100) + 'TAIL-END\n'
        self.write(self.state / 'worker/agent.log', log)
        self.write(self.state / 'logs/worker.log', 'second\n' * 100)
        status, _, error = self.run_cli('--max-bytes', '90')
        self.assertEqual((status, error), (0, ''))
        report = self.report()
        self.assertEqual(report['logs'][0]['original_bytes'], (self.state / 'worker/agent.log').stat().st_size)
        self.assertTrue(all(record['truncated'] for record in report['logs']))
        self.assertLessEqual(sum(record['read_bytes'] for record in report['logs']), 90)
        self.assertLessEqual(sum(record['bytes'] for record in report['logs']), 90)
        self.assertIn('TAIL-END', d.preview(self.output, 'logs/000.txt'))
        self.assertNotIn('OLD-HEAD', d.preview(self.output, 'logs/000.txt'))

    def test_per_file_limit_discards_partial_first_line(self):
        raw = b'x' * (d.FILE_LIMIT + 100) + b'\nlast-line\n'
        path = self.state / 'worker/agent.log'
        path.write_bytes(raw)
        data, meta = d.read_file(path, d.FILE_LIMIT, tail=True)
        self.assertEqual(data, b'last-line\n')
        self.assertTrue(meta['truncated'])
        self.assertEqual(meta['original_bytes'], len(raw))

    def test_task_logs_are_opt_in_and_warn_with_redaction(self):
        status, output, error = self.run_cli()
        self.assertEqual((status, error), (0, ''))
        self.assertNotIn('TASK-OUTPUT', output)
        self.output = self.root / 'tasks.zip'
        self.write(self.attempt / 'checks/0/stderr.log', 'error password=synthetic-check')
        self.write(self.attempt / 'step-1/stdout.jsonl', '{"token":"synthetic-step"}')
        status, output, error = self.run_cli('--include-task-logs')
        self.assertEqual(status, 0)
        self.assertIn(d.TASK_WARNING, error)
        with zipfile.ZipFile(self.output) as archive:
            data = b'\n'.join(archive.read(name) for name in archive.namelist()).decode()
        self.assertIn('TASK-OUTPUT', data)
        for secret in ('fake-task-password', 'fake-lease-short', 'synthetic-check', 'synthetic-step'):
            self.assertNotIn(secret, data)
        self.assertEqual(len(self.report()['logs']), 4)

    def test_controller_info_is_sanitized_without_network_or_profile(self):
        info = self.root / 'controller.json'
        self.write(info, {'client': {'token': 'client-fake-secret'}, 'nodes': [{'username': 'remote-user', 'hostname': 'remote-host'}],
                         'doctor': {'last_error': 'Authorization: Bearer remote-fake-secret'},
                         'events': [{'input': 'PRIVATE-TASK-INPUT', 'note': '/Users/remote-user/demo'}]})
        with mock.patch('agent_collab.cli.Conn', side_effect=AssertionError('credentials')), \
                mock.patch('agent_collab.cli.request') as request, mock.patch('urllib.request.urlopen') as network:
            status, output, error = self.run_cli('--controller-info', str(info))
        self.assertEqual((status, error), (0, ''))
        request.assert_not_called()
        network.assert_not_called()
        for secret in ('client-fake-secret', 'remote-fake-secret', 'remote-user', 'remote-host', 'PRIVATE-TASK-INPUT'):
            self.assertNotIn(secret, output)
        self.assertIn('controller', self.report()['sections'])

    def test_profile_fails_closed_before_any_read_or_network(self):
        with mock.patch.object(d, 'collect') as collect, mock.patch('agent_collab.cli.Conn') as connection:
            status, output, error = self.run_cli('--profile', 'default')
        self.assertEqual(status, 2)
        self.assertEqual(output, '')
        self.assertIn('--controller-info', error)
        collect.assert_not_called()
        connection.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_preview_lists_and_shows_without_mutation(self):
        self.write(self.state / 'worker/agent.log', 'error synthetic failure')
        self.assertEqual(self.run_cli()[0], 0)
        before = self.output.stat()
        digest = hashlib.sha256(self.output.read_bytes()).hexdigest()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(d.main(['preview', str(self.output), '--show', 'logs/000.txt']), 0)
        self.assertEqual(out.getvalue().strip(), 'error synthetic failure')
        self.assertEqual(hashlib.sha256(self.output.read_bytes()).hexdigest(), digest)
        self.assertEqual(self.output.stat().st_mtime_ns, before.st_mtime_ns)

    def test_atomic_publish_and_current_user_only_permissions(self):
        original = os.replace
        replacements = []

        def replace(source, target):
            self.assertEqual(Path(source).parent, self.output.parent)
            self.assertTrue(str(source).endswith('.tmp'))
            self.assertFalse(self.output.exists())
            self.assertIsNone(credstore.file_problem(source))
            replacements.append((source, target))
            return original(source, target)

        runner = subprocess.run
        with mock.patch('os.replace', side_effect=replace), mock.patch('subprocess.run', wraps=runner) as child:
            self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(len(replacements), 1)
        self.assertIsNone(credstore.file_problem(self.output))
        if os.name == 'nt':
            self.assertTrue(child.call_args_list)
            for call in child.call_args_list:
                self.assertEqual(call.kwargs['creationflags'], subprocess.CREATE_NO_WINDOW)
        else:
            self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.root.glob('.*.tmp')), [])

    def test_atomic_failure_cleans_temp_and_preserves_existing_output(self):
        with mock.patch('os.replace', side_effect=OSError('fake failure')):
            self.assertEqual(self.run_cli()[0], 2)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob('.*.tmp')), [])
        self.output.write_bytes(b'keep-existing-file')
        self.assertEqual(self.run_cli()[0], 2)
        self.assertEqual(self.output.read_bytes(), b'keep-existing-file')

    def test_permission_failure_does_not_write_archive(self):
        with mock.patch('agent_collab.artifacts.owner_only', side_effect=OSError('denied')):
            self.assertEqual(self.run_cli()[0], 2)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob('.*.tmp')), [])

    def test_excluded_directory_reparse_and_hardlink_are_rejected(self):
        for folder in ('.codex', '.claude', '.ssh'):
            with self.assertRaises(ValueError):
                d.read_file(self.home / folder / 'auth.json', 100)
        link = self.state / 'worker/agent.log'
        self.write(link, 'fake-link-content')
        original = d.is_link
        with mock.patch.object(d, 'is_link', side_effect=lambda path: path == link or original(path)):
            with self.assertRaises(ValueError):
                d.read_file(link, 100)
        second = self.root / 'hardlink'
        os.link(link, second)
        with self.assertRaises(ValueError):
            d.read_file(link, 100)

    def test_oversized_json_is_not_partially_parsed(self):
        info = self.root / 'huge.json'
        self.write(info, '{"note":"' + 'A' * d.JSON_LIMIT + '"}')
        self.assertEqual(self.run_cli('--controller-info', str(info))[0], 0)
        self.assertEqual(self.report()['sections']['controller']['status'], 'unavailable')

    def test_preview_rejects_unknown_names_duplicates_and_oversized_text(self):
        for i, names in enumerate((['../escape.txt'], ['diagnostics.json', 'diagnostics.json'], ['logs/000.txt'])):
            path = self.root / ('bad%d.zip' % i)
            with zipfile.ZipFile(path, 'w') as archive:
                for name in names:
                    with self.assertWarns(UserWarning) if i == 1 and name in archive.namelist() else contextlib.nullcontext():
                        archive.writestr(name, '{}')
            with self.assertRaises(ValueError):
                d.preview(path)
        path = self.root / 'large.zip'
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('diagnostics.json', '{}')
            archive.writestr('logs/000.txt', 'a' * (d.FILE_LIMIT + 1))
        with self.assertRaises(ValueError):
            d.preview(path, 'logs/000.txt')
        with self.assertRaises(ValueError):
            d.preview(path)

    def test_camel_case_fields_in_text_and_embedded_json(self):
        redactor = d.Redactor(home='/home/synth-user', username='synth-user', hostname='synth-host')
        line = 'error clientToken=short-one apiKey="short two" details={password:short-three} note="token=short-four"'
        clean = redactor.text(line)
        for secret in ('short-one', 'short two', 'short-three', 'short-four'):
            self.assertNotIn(secret, clean)
        self.assertEqual(redactor.counts['field'], 4)
        value = redactor.value({'frozenInput': {'note': 'private-task'}, 'instructions': 'private-source'})
        self.assertNotIn('private-task', json.dumps(value))
        self.assertNotIn('private-source', json.dumps(value))
        self.assertEqual(redactor.counts['excluded_content'], 2)

    def test_sensitive_controller_info_paths_are_not_opened(self):
        for suffix in ('.token', '.pem', '.key', '.db', '.bak'):
            with self.subTest(suffix=suffix):
                info = self.root / ('private' + suffix)
                self.write(info, {'token': 'synthetic-secret'})
                with mock.patch.object(d.os, 'open', side_effect=AssertionError('must not open')):
                    with self.assertRaises(ValueError):
                        d.read_json(info)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'POSIX FIFO')
    def test_fifo_log_is_rejected_without_opening_or_blocking(self):
        path = self.state / 'worker/agent.log'
        os.mkfifo(path)
        with mock.patch.object(d.os, 'open', side_effect=AssertionError('must not open')):
            with self.assertRaises(ValueError):
                d.read_file(path, d.FILE_LIMIT)
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(self.report()['logs'][0]['status'], 'unavailable')

    def test_windows_acl_is_restricted_before_writing_with_no_window(self):
        flags = {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)}
        name, sid = r'SYNTH-HOST\synth-user', 'S-1-5-21-100-200-300-1001'
        calls = []
        removed = False

        def run(command, **kwargs):
            nonlocal removed
            calls.append(command)
            self.assertEqual(kwargs['creationflags'], flags['creationflags'])
            self.assertFalse(self.output.exists())
            if command[0].endswith('whoami.exe'):
                return subprocess.CompletedProcess(command, 0, '"%s","%s"\n' % (name, sid))
            path = Path(command[1])
            self.assertEqual(path.stat().st_size, 0)  # 权限收紧之前尚未写入诊断数据。
            if '/remove' in command:
                self.assertEqual(command[-1], '*S-1-5-18')
                removed = True
            output = str(path) + ' ' + name + ':(F)\n'
            if not removed:
                output += ' S-1-5-18:(F)\n'
            return subprocess.CompletedProcess(command, 0, output)

        fake_os = SimpleNamespace(name='nt', environ=os.environ, path=os.path,
                                  open=os.open, close=os.close, fsync=os.fsync, replace=os.replace,
                                  O_WRONLY=os.O_WRONLY, O_CREAT=os.O_CREAT, O_EXCL=os.O_EXCL)
        artifacts._current_user.cache_clear()
        self.addCleanup(artifacts._current_user.cache_clear)
        with mock.patch.object(artifacts, 'os', fake_os), mock.patch.object(artifacts, 'NO_WINDOW', flags), \
                mock.patch('subprocess.run', side_effect=run):
            self.assertEqual(self.run_cli()[0], 0)
        self.assertTrue(removed)
        self.assertTrue(any('/inheritance:r' in command and '/grant:r' in command for command in calls))
        self.assertTrue(zipfile.is_zipfile(self.output))

    def test_windows_service_queries_use_installer_functions_and_no_window(self):
        config, _ = d.configuration(type('Args', (), dict(prefix=self.prefix, state_dir=self.state, instance=None))())
        config['system'] = 'Windows'
        manager = d.installer.Services(config, self.prefix / 'versions/1.2.3')
        flags = {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)}

        def run(command, **kwargs):
            self.assertEqual(kwargs['creationflags'], flags['creationflags'])
            if command[0] == 'schtasks':
                output = '<Task><RegistrationInfo><Description>agent-collab prefix: %s</Description></RegistrationInfo></Task>' % self.prefix
            else:
                self.assertIn('-NonInteractive', command)
                output = 'Running'
            return subprocess.CompletedProcess(command, 0, output)

        with mock.patch.object(d.installer, 'NO_WINDOW', flags), mock.patch('subprocess.run', side_effect=run) as child:
            self.assertEqual(SERVICE_STATUS(manager), {'worker': 'active'})
            self.assertEqual(SERVICE_AUTOSTART(manager), {'worker': 'enabled'})
            self.assertEqual(SERVICE_OWNERSHIP(manager), {'worker': 'this_prefix'})
        self.assertEqual(child.call_count, 3)

    def test_recent_errors_are_bounded(self):
        self.write(self.state / 'worker/agent.log', '\n'.join('error %d' % i for i in range(100)))
        self.assertEqual(self.run_cli()[0], 0)
        errors = self.report()['recent_errors']
        self.assertEqual(len(errors), d.ERROR_LIMIT)
        self.assertEqual(errors[-1]['text'], 'error 99')

    def test_instance_uses_desktop_layout_and_registered_cli(self):
        args = type('Args', (), dict(prefix=None, state_dir=None, instance='cand'))()
        config, old = d.configuration(args)
        self.assertIsNone(old)
        self.assertEqual(Path(config['prefix']), self.home / '.local/share/agent-collab-cand')
        self.assertEqual(Path(config['state_dir']), self.home / '.local/state/agent-collab-cand')
        self.assertEqual(cli.MODULES['diagnostics'], 'agent_collab.diagnostics')
        with mock.patch.object(d, 'main', return_value=0) as main:
            self.assertEqual(cli.main(['diagnostics', 'preview', 'fake.zip']), 0)
            main.assert_called_once_with(['preview', 'fake.zip'])


if __name__ == '__main__':
    unittest.main()
