"""Native service lifecycle and effective diagnostics: no live managers or models."""
from contextlib import redirect_stderr, redirect_stdout
import datetime
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_collab import cli, installer as ins, node_agent as node, protocol as p
from agent_collab import domain as d
from agent_collab.adapters import claude, codex
from agent_collab.adapters.commands import resolve_commands
import test_installer as legacy

ROOT = Path(__file__).resolve().parents[1]


class ServiceTests(unittest.TestCase):
    setUpClass = classmethod(legacy.InstallerTests.setUpClass.__func__)
    tearDownClass = classmethod(legacy.InstallerTests.tearDownClass.__func__)
    setUp = legacy.InstallerTests.setUp

    def installation(self, system='Darwin', role='worker'):
        self.prefix = self.temp / (system + '-' + role)
        self.prefix.mkdir(exist_ok=True)
        self.state = self.temp / (system + '-' + role + '-state')
        for kind in ('worker', 'controller'):
            (self.state / kind).mkdir(parents=True, exist_ok=True)
        config = dict(prefix=str(self.prefix), state_dir=str(self.state), system=system,
                      service=True, role=role, python=sys.executable)
        manifest, files = ins.read_release(self.v1)
        directory = ins.install_version(self.prefix, manifest, files)
        ins.switch_current(self.prefix, manifest['version'], system)
        ins.write_json(self.prefix / 'installation.json', dict(version=manifest['version'], config=config))
        return config, directory, manifest

    def invoke(self, action, config, runner, *extra, real_ownership=False, listening=True):
        out, err = io.StringIO(), io.StringIO()
        # 所有权检查的 POSIX 分支读当前用户主目录下的服务定义，测试机上可能真有一份安装：默认替换掉。
        ownership = ins.Services.check_ownership if real_ownership else lambda manager: None
        with patch.object(ins.platform, 'system', return_value=config['system']), \
                patch.object(ins, 'run', side_effect=runner), \
                patch.object(ins.Services, 'check_ownership', new=ownership), \
                patch.object(ins, 'controller_listening', return_value=listening), \
                patch.object(ins, 'admin_token', side_effect=AssertionError('no admin token')), \
                patch.object(ins, 'drain_client', side_effect=AssertionError('no controller API')), \
                patch('agent_collab.client.Client.json', side_effect=AssertionError('no controller API')), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(['service', action, '--prefix', config['prefix'], '--timeout', '0'] + list(extra))
        return code, json.loads(out.getvalue()), err.getvalue()

    def status(self, config, directory, manifest, **changes):
        status = dict(version_dir=str(directory), version=manifest['version'], git_commit=manifest.get('git_commit'),
                      pid=42, last_heartbeat_ok_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
        status.update(changes)
        node.save(Path(config['state_dir']) / 'worker/status.json', status)
        return status

    def test_stop_disables_before_ending_for_all_platforms_and_is_idempotent(self):
        for system in ('Windows', 'Darwin', 'Linux'):
            with self.subTest(system=system), patch.object(ins.os, 'getuid', return_value=501, create=True):
                config, directory, manifest = self.installation(system)
                calls = []
                def runner(argv, **kwargs):
                    calls.append(argv)
                    # End/bootout may fail when already stopped; the state lock is decisive.
                    return subprocess.CompletedProcess(argv, 1 if '/End' in argv or 'bootout' in argv else 0, '', '')
                expected = {
                    'Windows': [['schtasks', '/Change', '/TN', 'agent-collab-worker', '/DISABLE'],
                                ['schtasks', '/End', '/TN', 'agent-collab-worker']],
                    'Darwin': [['launchctl', 'disable', 'gui/501/org.agent-collab.worker'],
                               ['launchctl', 'bootout', 'gui/501/org.agent-collab.worker']],
                    'Linux': [['systemctl', '--user', 'disable', '--now', 'agent-collab-worker.service']],
                }[system]
                for unused in range(2):
                    calls.clear()
                    code, report, err = self.invoke('stop', config, runner)
                    self.assertEqual((code, err), (0, ''))
                    self.assertEqual(report['autostart'], 'disabled')
                    self.assertTrue(report['stopped'])
                    self.assertEqual(calls, expected)
                    self.assertTrue((self.state / 'worker/agent.lock').exists())

    def test_stop_cannot_succeed_until_worker_lock_is_released(self):
        for system in ('Windows', 'Darwin', 'Linux'):
            with self.subTest(system=system), patch.object(ins.os, 'getuid', return_value=501, create=True):
                config, directory, manifest = self.installation(system)
                runner = Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
                with node.AgentLock(self.state / 'worker'):
                    code, report, err = self.invoke('stop', config, runner)
                    self.assertEqual(code, 1)
                    self.assertFalse(report['stopped'])
                    self.assertIn('locks', report['error'])
                code, report, err = self.invoke('stop', config, runner)
                self.assertEqual(code, 0)
                self.assertTrue(report['stopped'])

    def test_stop_waits_through_a_busy_lock(self):
        config, directory, manifest = self.installation('Windows')
        original = ins.wait_lock
        attempts = []
        def wait(factory, timeout):
            self.assertGreater(timeout, 0)
            real = factory()
            def delayed():
                attempts.append(1)
                if len(attempts) == 1:
                    raise ValueError('busy')
                return real.__enter__()
            lock = Mock()
            lock.__enter__ = Mock(side_effect=delayed)
            lock.__exit__ = real.__exit__
            return original(lambda: lock, timeout=timeout, sleep=lambda seconds: None)
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
        with patch.object(ins, 'wait_lock', side_effect=wait):
            code, report, err = self.invoke('stop', config, runner, '--timeout', '1')
        self.assertEqual(code, 0)
        self.assertEqual(len(attempts), 2)

    def test_active_attempt_refused_before_any_service_command_unless_allowed(self):
        config, directory, manifest = self.installation('Windows')
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
        for state in ('preparing', 'running'):
            with self.subTest(state=state):
                node.save(self.state / 'worker/attempts/a/meta.json', {'state': state})
                code, report, err = self.invoke('stop', config, runner)
                self.assertEqual(code, 1)
                self.assertIn('--allow-active', err)
                runner.assert_not_called()
        self.assertEqual(self.invoke('stop', config, runner, '--allow-active')[0], 0)

    def test_default_stop_keeps_posix_autostart_and_linux_controller_uses_privilege_path(self):
        for system in ('Windows', 'Darwin', 'Linux'):
            with self.subTest(system=system), patch.object(ins.os, 'getuid', return_value=501, create=True):
                config, directory, manifest = self.installation(system, 'all-in-one')
                with patch.object(ins, 'run') as runner:
                    manager = ins.Services(config, directory)
                    manager.stop()
                    commands = [call.args[0] for call in runner.call_args_list]
                    if system == 'Windows':
                        # 每分钟的触发器会与安装器抢状态锁：升级路径的停止也先禁用（尽力而为），再结束。
                        self.assertEqual(commands, [
                            ['schtasks', '/Change', '/TN', 'agent-collab-worker', '/DISABLE'],
                            ['schtasks', '/End', '/TN', 'agent-collab-worker'],
                            ['schtasks', '/Change', '/TN', 'agent-collab-controller', '/DISABLE'],
                            ['schtasks', '/End', '/TN', 'agent-collab-controller']])
                        self.assertFalse(any(call.kwargs.get('check', True) for call in runner.call_args_list))
                    else:
                        self.assertFalse(any('disable' in c for c in commands))
                    if system == 'Linux':
                        self.assertIn(['sudo', '--', 'systemctl', 'stop', 'agent-collab-controller.service'], commands)
                    runner.reset_mock()
                    manager.stop(disable_autostart=True)
                    commands = [call.args[0] for call in runner.call_args_list]
                    if system == 'Linux':
                        self.assertEqual(commands, [
                            ['systemctl', '--user', 'disable', '--now', 'agent-collab-worker.service'],
                            ['sudo', '--', 'systemctl', 'disable', '--now', 'agent-collab-controller.service']])

    def test_start_enables_before_starting_and_accepts_only_new_matching_heartbeat(self):
        for system in ('Windows', 'Darwin', 'Linux'):
            with self.subTest(system=system), patch.object(ins.os, 'getuid', return_value=501, create=True):
                config, directory, manifest = self.installation(system)
                calls = []
                def runner(argv, **kwargs):
                    calls.append(argv)
                    self.status(config, directory, manifest)
                    return subprocess.CompletedProcess(argv, 0, 'state = running' if system == 'Darwin' else 'Running' if system == 'Windows' else 'active', '')
                for unused in range(2):
                    calls.clear()
                    code, report, err = self.invoke('start', config, runner)
                    self.assertEqual((code, err), (0, ''))
                    self.assertTrue(report['ready'])
                    self.assertEqual(report['worker']['pid'], 42)
                    self.assertEqual(report['worker']['version'], manifest['version'])
                    self.assertEqual(report['worker']['git_commit'], manifest.get('git_commit'))
                    self.assertNotIn('-k', [arg for c in calls for arg in c])
                    if system == 'Windows':
                        self.assertEqual(calls[:2], [['schtasks', '/Change', '/TN', 'agent-collab-worker', '/ENABLE'],
                                                     ['schtasks', '/Run', '/TN', 'agent-collab-worker']])
                    elif system == 'Darwin':
                        self.assertEqual(calls[:3], [['launchctl', 'enable', 'gui/501/org.agent-collab.worker'],
                                                    ['launchctl', 'print', 'gui/501/org.agent-collab.worker'],
                                                    ['launchctl', 'kickstart', 'gui/501/org.agent-collab.worker']])
                    else:
                        self.assertEqual(calls[0], ['systemctl', '--user', 'enable', '--now', 'agent-collab-worker.service'])

    def test_mac_unloaded_service_bootstraps_after_enable(self):
        config, directory, manifest = self.installation('Darwin')
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1 if 'print' in argv else 0, '', '')
        with patch.object(ins, 'run', side_effect=runner), patch.object(ins.os, 'getuid', return_value=501, create=True):
            ins.Services(config, directory).start()
        self.assertEqual([c[1] for c in calls], ['enable', 'print', 'bootstrap'])
        self.assertNotIn('-k', [arg for c in calls for arg in c])

    def test_missing_stale_or_mismatched_heartbeat_never_reports_ready(self):
        config, directory, manifest = self.installation('Windows')
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, 'Running', ''))
        cases = [None, {}, dict(last_heartbeat_ok_at='2000-01-01T00:00:00Z'), dict(pid=0), dict(pid=True),
                 dict(version='old'), dict(git_commit='old'), dict(version_dir=str(directory.parent / 'old')),
                 dict(last_heartbeat_ok_at=None), dict(last_heartbeat_ok_at=0)]
        for changed in cases:
            with self.subTest(changed=changed):
                if changed is not None:
                    self.status(config, directory, manifest, **changed)
                code, report, err = self.invoke('start', config, runner)
                self.assertEqual(code, 1)
                self.assertFalse(report['ready'])
                self.assertIn('heartbeat', err)

    def test_manager_errors_are_sanitized_and_never_claim_success(self):
        config, directory, manifest = self.installation('Windows')
        for action in ('start', 'stop'):
            runner = Mock(side_effect=RuntimeError('secret-credential-in-exception'))
            code, report, err = self.invoke(action, config, runner)
            self.assertEqual(code, 1)
            self.assertNotIn('secret-credential', json.dumps(report) + err)
            self.assertFalse((self.prefix / '.install-lock').exists())

    def test_status_reports_autostart_three_states_without_writing(self):
        for system in ('Windows', 'Darwin', 'Linux'):
            with self.subTest(system=system), patch.object(ins.os, 'getuid', return_value=501, create=True):
                config, directory, manifest = self.installation(system)
                for state in ('enabled', 'disabled', 'unknown'):
                    def runner(argv, **kwargs):
                        if 'print-disabled' in argv:
                            text = 'disabled services = {\n "org.agent-collab.worker" => ' + ('true' if state == 'disabled' else 'false') + '\n}'
                        elif 'is-enabled' in argv:
                            text = state
                        elif system == 'Windows':
                            text = {'enabled': 'Running', 'disabled': 'Disabled', 'unknown': ''}[state]
                        else:
                            text = 'state = running' if system == 'Darwin' else 'active'
                        code = 1 if state == 'unknown' or ('is-enabled' in argv and state == 'disabled') else 0
                        return subprocess.CompletedProcess(argv, code, text, '')
                    with patch.object(ins, 'atomic_write', side_effect=AssertionError('read only')), \
                            patch.object(ins, 'stopped_locks', side_effect=AssertionError('read only')):
                        code, report, err = self.invoke('status', config, runner)
                    self.assertEqual(report['autostart'], {'worker': state})
                    self.assertEqual(code, 0 if state != 'unknown' and (system != 'Windows' or state == 'enabled') else 1)
                    self.assertIsNone(report['worker'])
                    self.assertEqual(report['current'], manifest['version'])
                    self.assertFalse((self.prefix / '.install-lock').exists())

    def test_status_heartbeat_age_and_unreadable_status(self):
        config, directory, manifest = self.installation('Windows')
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, 'Running', ''))
        self.status(config, directory, manifest, last_heartbeat_ok_at='2020-01-01T00:00:00Z')
        code, report, err = self.invoke('status', config, runner)
        self.assertEqual(code, 0)
        self.assertGreater(report['worker']['heartbeat_age_s'], 100)
        self.assertEqual(report['worker']['pid'], 42)
        for text in ('not JSON', '[]', 'null'):
            (self.state / 'worker/status.json').write_text(text)
            self.assertIsNone(self.invoke('status', config, runner)[1]['worker'])

    def test_no_service_or_no_installation_rejected_without_commands(self):
        config, directory, manifest = self.installation('Windows')
        runner = Mock(side_effect=AssertionError('not a managed installation'))
        for record in (dict(version=manifest['version'], config=dict(config, service=False)),
                       dict(version=None, config=config), None):
            if record is None:
                (self.prefix / 'installation.json').unlink()
            else:
                ins.write_json(self.prefix / 'installation.json', record)
            for action in ('start', 'stop', 'status'):
                code, report, err = self.invoke(action, config, runner)
                self.assertEqual(code, 1)
                self.assertTrue(err.startswith('Installer: '))
        runner.assert_not_called()

    def test_upgrade_and_rollback_keep_a_previously_disabled_service_stopped(self):
        config, directory, manifest = self.installation('Windows')
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, 'Running', ''))
        self.assertEqual(self.invoke('stop', config, runner)[0], 0)
        old = ins.read_json(self.prefix / 'installation.json')
        new, files = ins.read_release(self.v2)
        args = ins.parser().parse_args([])
        for target, payload in ((new, files), (manifest, None)):
            args.rollback = payload is None
            calls = []
            def record(argv, **kwargs):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, '', '')
            with patch.object(ins, 'run', side_effect=record), patch.object(ins.Services, 'register'), \
                    patch.object(ins, 'wait_worker_status'), patch.object(ins, 'wait_doctor', return_value={'ok': True}):
                ins.transaction(config, old, target, payload, args)
            enable = ['schtasks', '/Change', '/TN', 'agent-collab-worker', '/ENABLE']
            start = ['schtasks', '/Run', '/TN', 'agent-collab-worker']
            self.assertNotIn(enable, calls)
            self.assertNotIn(start, calls)
            # 升级期间先禁用再结束，成功后的期望状态仍为停用。
            disable = ['schtasks', '/Change', '/TN', 'agent-collab-worker', '/DISABLE']
            self.assertLess(calls.index(disable), calls.index(['schtasks', '/End', '/TN', 'agent-collab-worker']))
            self.assertEqual(ins.current_version(self.prefix, 'Windows'), target['version'])
            old = ins.read_json(self.prefix / 'installation.json')
            self.assertEqual(old['desired'], {'worker': 'stopped'})

    def test_controller_start_uses_local_doctor_without_admin_or_api(self):
        config, directory, manifest = self.installation('Windows', 'controller')
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, 'Running', ''))
        with patch.object(ins, 'doctor', wraps=ins.doctor) as doctor:
            code, report, err = self.invoke('start', config, runner)
        self.assertEqual((code, err), (0, ''))
        self.assertTrue(report['ready'])
        self.assertTrue(doctor.call_args.kwargs['offline'])
        # 服务管理器已报告 active，但 controller 还没有应答本机 /healthz：不能报告就绪。
        code, report, err = self.invoke('start', config, runner, listening=False)
        self.assertEqual(code, 1)
        self.assertFalse(report['ready'])

    def test_controller_listening_probes_loopback_healthz_without_a_token(self):
        seen = []
        def transport(method, url, headers, body):
            seen.append((method, url, 'authorization' in headers))
            return 200, {}, b'{"ok": true}'
        with patch('agent_collab.client.Client._transport', side_effect=transport):
            self.assertTrue(ins.controller_listening(dict(bind='127.0.0.1:18669')))
        self.assertEqual(seen, [('GET', 'http://127.0.0.1:18669/api/v1/healthz', False)])
        with patch('agent_collab.client.Client._transport', side_effect=OSError('refused')):
            self.assertFalse(ins.controller_listening(dict(bind='127.0.0.1:18669')))
        self.assertFalse(ins.controller_listening({}))

    def test_service_definition_of_another_prefix_is_neither_queried_further_nor_changed(self):
        config, directory, manifest = self.installation('Windows')
        task = '<Task><RegistrationInfo><Description>agent-collab prefix: {}</Description></RegistrationInfo></Task>'
        query = ['schtasks', '/Query', '/TN', 'agent-collab-worker', '/XML']
        for answer, error_type in ((task.format('C:\\another prefix'), 'ValueError'), ('not XML', 'ParseError')):
            calls = []
            def runner(argv, **kwargs):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, answer if '/Query' in argv else 'Running', '')
            for action in ('status', 'stop', 'start'):
                with self.subTest(action=action, error_type=error_type):
                    calls.clear()
                    code, report, err = self.invoke(action, config, runner, real_ownership=True)
                    self.assertEqual(code, 1)
                    self.assertEqual(report['error_type'], error_type)
                    self.assertIn('another installation', err)
                    self.assertEqual(calls, [query])
                    self.assertFalse((self.prefix / '.install-lock').exists())
        runner = Mock(side_effect=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, task.format(self.prefix) if '/Query' in argv else 'Running', ''))
        self.assertEqual(self.invoke('status', config, runner, real_ownership=True)[0], 0)

    def test_linux_controller_stop_checks_privileged_lock_helper(self):
        config, directory, manifest = self.installation('Linux', 'controller')
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
        for locked in (True, False):
            child = Mock(returncode=0)
            child.stdout.readline.return_value = 'locked\n' if locked else ''
            child.poll.return_value = 0
            with patch.object(ins.subprocess, 'Popen', return_value=child) as popen, \
                    patch.object(ins.os, 'getuid', return_value=501, create=True):
                code, report, err = self.invoke('stop', config, runner)
            self.assertEqual(code, 0 if locked else 1)
            self.assertEqual(report['stopped'], locked)
            command = popen.call_args.args[0]
            self.assertEqual(command[:4], ['sudo', '-u', 'agent-collab', '--'])
            self.assertIn('wait_lock(lambda: ControllerLock(', command[-1])
            self.assertIn('timeout=0', command[-1])
            child.communicate.assert_called_once()

    def test_fresh_worker_heartbeat_does_not_override_inactive_service(self):
        config, directory, manifest = self.installation('Windows')
        def runner(argv, **kwargs):
            self.status(config, directory, manifest)
            return subprocess.CompletedProcess(argv, 0, 'Ready', '')
        code, report, err = self.invoke('start', config, runner)
        self.assertEqual(code, 1)
        self.assertFalse(report['ready'])

    def test_generated_launcher_and_posix_bin_forward_service_arguments(self):
        config, directory, manifest = self.installation(ins.platform.system())
        ins.write_launchers(config)
        launchers = [[sys.executable, '-B', str(self.prefix / 'launcher.py')]]
        if os.name != 'nt':
            launchers.append([str(self.prefix / 'bin/agent-collab')])
        for launcher in launchers:
            # An absent installation fails before any service-manager command.
            result = subprocess.run(launcher + ['service', 'status', '--prefix', str(self.temp / 'absent')],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(json.loads(result.stdout)['action'], 'status')
            self.assertIn('Installer:', result.stderr)

    def test_macos_missing_override_is_enabled_and_query_failure_is_unknown(self):
        config, directory, manifest = self.installation('Darwin')
        manager = ins.Services(config, directory)
        for text, code, expected in [('disabled services = {\n}', 0, 'enabled'),
                                     # macOS 27 的实际输出格式
                                     ('disabled services = {\n\t"org.agent-collab.worker" => disabled\n}', 0, 'disabled'),
                                     ('disabled services = {\n\t"org.agent-collab.worker" => enabled\n}', 0, 'enabled'),
                                     ('garbled', 0, 'unknown'), ('state = running', 1, 'unknown')]:
            with patch.object(ins, 'run', return_value=subprocess.CompletedProcess([], code, text, '')), \
                    patch.object(ins.os, 'getuid', return_value=501, create=True):
                self.assertEqual(manager.autostart()['worker'], expected)
                if code:
                    self.assertEqual(manager.status()['worker'], 'unknown')


class EffectiveTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.dev/tmp').mkdir(parents=True, exist_ok=True)
        self.temp = Path(tempfile.mkdtemp(dir=str(ROOT / '.dev/tmp')))
        self.addCleanup(shutil.rmtree, str(self.temp))
        self.config = dict(node_id='fixture', controller='https://controller.invalid', physical_host_id='host',
                           failure_domain='domain', node_token_sha256=p.sha256_bytes(b'node-secret-value'),
                           slots=2, host_slots=3, labels=['custom'], codex_command=[sys.executable, 'wrapper'],
                           claude_command=sys.executable, codex_windows_sandbox='unelevated',
                           codex_home=str(self.temp / 'codex-home'), admin_token='admin-secret-value')
        node.save(self.temp / 'agent.json', self.config)
        (self.temp / 'node.token').write_text('node-secret-value')
        (self.temp / 'node.token').chmod(0o600)
        node.save(self.temp / 'attempts/one/meta.json', dict(state='reported'))
        (self.temp / 'attempts/one/lease.token').write_text('lease-secret-value')
        (self.temp / 'attempts/one/lease.token').chmod(0o600)
        self.environment = dict(PATH='private-inherited-path', CODEX_THREAD_ID='thread-secret-value',
                                CODEX_INTERNAL_ORIGINATOR_OVERRIDE='origin-secret-value', SECRET='env-secret-value')

    def doctor(self, config=None, system='Windows'):
        config = self.config if config is None else config
        node.save(self.temp / 'agent.json', config)
        commands = resolve_commands(config, self.environment, system='Linux')
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, '{"loggedIn": true}' if 'auth' in argv else 'fixture 1', '')
        transport = lambda *args: (200, {}, b'{"ok": true}')
        # 固定内核版本字符串：在 WSL 上运行时 detect 会多报一个 wsl 标签，测试结果不应随宿主变化。
        with patch.object(node.shutil, 'which', return_value=None), \
                patch.object(node.platform, 'release', return_value='6.1-generic'):
            return node.doctor(self.temp, transport, commands=commands, runner=runner, system=system), commands

    def test_effective_is_allowlisted_and_uses_the_actual_argv_builders(self):
        with patch.object(codex, 'build_argv', wraps=codex.build_argv) as codex_builder, \
                patch.object(claude, 'build_argv', wraps=claude.build_argv) as claude_builder:
            report, commands = self.doctor()
        self.assertTrue(report['ok'])
        effective = report['effective']
        self.assertEqual(effective['node'], dict(node_id='fixture', controller=self.config['controller'],
            physical_host_id='host', failure_domain='domain', slots=2, host_slots=3, labels=['custom'],
            workspace_root=None, data_dir=str(self.temp)))
        self.assertEqual(set(effective['agent']), {'version', 'git_commit', 'version_dir', 'python', 'user', 'platform'})
        self.assertEqual(effective['agent']['python'], sys.executable)
        codex_builder.assert_called_once()
        claude_builder.assert_called_once()
        # 配置里附带的参数（这里是 codex 的 'wrapper'）只显示占位符，可执行文件原样显示。
        self.assertEqual(commands['codex']['argv'][1:], ['wrapper'])
        shown = {'codex': commands['codex']['argv'][:1] + ['<configured-argument>'], 'claude': commands['claude']['argv']}
        expected = codex.build_argv(commands['codex']['argv'], '<work>', '<attempt>', dict(model='model', effort='effort'),
                                    system='Windows', windows_sandbox='unelevated')
        expected[expected.index('--sandbox') + 1] = '<sandbox>'
        expected[expected.index('--model') + 1] = '<model>'
        expected = [s.replace('model_reasoning_effort="effort"', 'model_reasoning_effort="<effort>"') for s in expected]
        self.assertEqual(effective['harness']['codex']['managed_argv'], shown['codex'] + expected[2:])
        session = '00000000-0000-4000-8000-000000000000'
        expected = claude.build_argv(commands['claude']['argv'], dict(model='<profile.model>', effort='<profile.effort>'), d.DECISION_SCHEMA, session)
        expected[expected.index('--json-schema') + 1] = '<decision-schema>'
        expected[expected.index('--session-id') + 1] = '<session-id>'
        self.assertEqual(effective['harness']['claude']['managed_argv'], expected)
        self.assertEqual(effective['harness']['claude']['managed_env'], {'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1'})
        self.assertNotIn('managed_env', effective['harness']['codex'])
        for name in ('codex', 'claude'):
            entry = effective['harness'][name]
            self.assertEqual(entry['argv'], shown[name])
            self.assertEqual(entry['version'], 'fixture 1')
            self.assertEqual(entry['path_prepend'], str(Path(sys.executable).parent))
            self.assertIsNone(entry['disabled'])
        self.assertEqual(effective['harness']['codex']['codex_home'], self.config['codex_home'])
        self.assertEqual(effective['harness']['codex']['cleared_env'], ['CODEX_THREAD_ID', 'CODEX_INTERNAL_ORIGINATOR_OVERRIDE'])
        serialized = json.dumps(report)
        for secret in ('node-secret-value', 'lease-secret-value', 'node_token_sha256', 'admin-secret-value',
                       'private-inherited-path', 'thread-secret-value', 'origin-secret-value', 'env-secret-value'):
            self.assertNotIn(secret, serialized)

    def test_doctor_separates_its_own_auth_probe_from_the_worker_service_result(self):
        # doctor 在 Mac 的 SSH 会话里读不到钥匙串：自己的结果是 unknown 并写明原因，worker 在图形会话里验证过的 ok 原样另列。
        node.save(self.temp / 'agent.json', self.config)
        commands = resolve_commands(self.config, self.environment, system='Linux')
        keychain = [36]
        def runner(argv, **kwargs):
            if argv[0] == '/usr/bin/security':
                return subprocess.CompletedProcess(argv, keychain[0], '', 'User interaction is not allowed.')
            if 'auth' in argv:
                return subprocess.CompletedProcess(argv, 1, '{"loggedIn": false, "authMethod": "none"}', '')
            return subprocess.CompletedProcess(argv, 0, 'Logged in using ChatGPT' if 'login' in argv else 'fixture 1', '')
        def doctor(ssh):
            env = {k: v for k, v in os.environ.items() if k not in ('SSH_CONNECTION', 'SSH_CLIENT')}
            env.update({'SSH_CONNECTION': '192.0.2.1 50000 192.0.2.2 22'} if ssh else {})
            with patch.object(node.shutil, 'which', return_value=None), patch.dict(os.environ, env, clear=True), \
                    patch.object(node.platform, 'release', return_value='6.1-generic'):
                return node.doctor(self.temp, lambda *args: (200, {}, b'{"ok": true}'), commands=commands, runner=runner, system='Darwin')
        report = doctor(True)
        self.assertEqual(report['auth_health'], {'codex': 'ok', 'claude': 'unknown'})
        source = report['auth_source']
        self.assertEqual((source['context'], source['via_ssh']), ('doctor_process', True))
        self.assertEqual(source['reasons'], {'codex': 'logged_in', 'claude': 'credential_store_unreadable'})
        self.assertTrue(source['checked_at'].endswith('Z'))
        self.assertIsNone(report['worker_auth'])  # 没有 status.json
        node.save(self.temp / 'status.json', dict(version='x', pid=42, last_heartbeat_ok_at='2026-11-11T00:00:00Z'))
        self.assertIsNone(doctor(False)['worker_auth'])  # 旧版本 worker 没写认证字段
        node.save(self.temp / 'status.json', dict(version='x', pid=42, last_heartbeat_ok_at='2026-11-11T00:00:05Z',
                  auth_health={'codex': 'ok', 'claude': 'ok'}, auth_checked_at='2026-11-11T00:00:00Z'))
        report = doctor(False)
        self.assertFalse(report['auth_source']['via_ssh'])
        self.assertEqual(report['auth_health']['claude'], 'unknown')
        self.assertEqual(report['worker_auth'], dict(context='worker_service', auth_health={'codex': 'ok', 'claude': 'ok'},
            checked_at='2026-11-11T00:00:00Z', pid=42, last_heartbeat_ok_at='2026-11-11T00:00:05Z'))
        # 钥匙串可读时，同样的 CLI 输出是有依据的失效。
        keychain[0] = 0
        report = doctor(False)
        self.assertEqual((report['auth_health']['claude'], report['auth_source']['reasons']['claude']), ('expired', 'not_logged_in'))

    def test_effective_is_redacted_even_when_display_fields_contain_credentials(self):
        config = dict(self.config, labels=['node-secret-value', 'lease-secret-value'])
        report, commands = self.doctor(config)
        self.assertEqual(report['effective']['node']['labels'], ['[REDACTED]', '[REDACTED]'])

    def test_configured_harness_arguments_are_never_printed(self):
        # 自定义命令里的参数可能是凭据，脱敏只认得本节点的令牌：除可执行文件外一律显示占位符。
        config = dict(self.config, codex_command=[sys.executable, '--api-key', 'sk-inline-credential'],
                      claude_command=[sys.executable, 'token=inline-credential'])
        report, commands = self.doctor(config)
        serialized = json.dumps(report)
        for secret in ('sk-inline-credential', '--api-key', 'inline-credential'):
            self.assertNotIn(secret, serialized)
        for name, extra in (('codex', 2), ('claude', 1)):
            entry = report['effective']['harness'][name]
            shown = commands[name]['argv'][:1] + ['<configured-argument>'] * extra
            self.assertEqual(entry['argv'], shown)
            self.assertEqual(entry['managed_argv'][:len(shown)], shown)
            self.assertGreater(len(entry['managed_argv']), len(shown))

    def test_malformed_configuration_fails_the_check_but_keeps_the_report(self):
        report, commands = self.doctor(dict(self.config, labels=['custom', 1]))
        self.assertFalse(report['ok'])
        self.assertFalse(report['checks']['configuration'])
        self.assertEqual(report['effective'], {'error_type': 'TypeError'})
        self.assertEqual(set(report['auth_health']), {'codex', 'claude'})
        self.assertTrue(report['checks']['workspace_writable'])

    def test_absent_and_disabled_harness_have_null_executable_version_and_managed_argv(self):
        config = dict(self.config, codex_command=str(self.temp / 'absent'), disabled_harnesses={'claude': 'maintenance'})
        report, commands = self.doctor(config)
        self.assertTrue(report['ok'])  # Harness presence does not change the existing ok gate.
        for name in ('codex', 'claude'):
            entry = report['effective']['harness'][name]
            for field in ('argv', 'version', 'path_prepend', 'managed_argv'):
                self.assertIsNone(entry[field])
        self.assertEqual(report['effective']['harness']['claude']['disabled'], 'maintenance')

    def test_configured_workspace_and_posix_sandbox_diagnostics(self):
        report, commands = self.doctor(dict(self.config, workspace_root=str(self.temp / 'work')), system='Darwin')
        self.assertEqual(report['effective']['node']['workspace_root'], str(self.temp / 'work'))
        self.assertFalse(any('windows.sandbox=' in arg for arg in report['effective']['harness']['codex']['managed_argv']))


class RunDecodingTests(unittest.TestCase):
    def test_non_utf8_tool_output_is_replaced_not_fatal(self):
        # 本地化 Windows 的 schtasks/icacls 按控制台代码页输出（这里是 GBK 的“成功”“信息”）：
        # 读取不能失败，可解析的 ASCII 部分要保留。
        code = ("import sys; sys.stdout.buffer.write(b'\\xb3\\xc9\\xb9\\xa6: Running\\n'); "
                "sys.stderr.buffer.write(b'\\xd0\\xc5\\xcf\\xa2')")
        result = ins.run([sys.executable, '-c', code])
        self.assertIn('Running', result.stdout)
        self.assertIsInstance(result.stderr, str)
