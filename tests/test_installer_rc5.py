"""Windows commands and worker upgrade safety without registering real services."""
import datetime
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
import sqlite3
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from agent_collab import installer as ins
from agent_collab.node_agent import save
import test_installer as legacy


class WorkerInstallTests(unittest.TestCase):
    setUpClass = classmethod(legacy.InstallerTests.setUpClass.__func__)
    tearDownClass = classmethod(legacy.InstallerTests.tearDownClass.__func__)
    setUp = legacy.InstallerTests.setUp
    args = legacy.InstallerTests.args
    invoke = legacy.InstallerTests.invoke

    def installed_worker(self):
        self.invoke('--release', str(self.v1), '--workspace-root', str(self.temp / 'workspace'), role='worker')
        old = ins.read_json(self.prefix / 'installation.json')
        return old, dict(old['config'])

    def test_new_prefix_with_retained_incompatible_db_refuses_before_target_code(self):
        self.invoke('--release', str(self.v1))  # controller
        database, token = self.state / 'controller/controller.db', self.state / 'controller/admin.token'
        with closing(sqlite3.connect(str(database))) as db, db:
            db.execute('PRAGMA user_version=99')
            events = db.execute('SELECT count(*) FROM events').fetchone()[0]
        token.unlink()
        self.prefix = self.temp / 'another prefix'  # 新前缀：没有旧安装记录，但数据目录保留着旧数据库
        output = self.invoke('--release', str(self.v1), status=1)
        self.assertIn('unsupported', output)
        self.assertFalse(token.exists())  # 目标代码没有运行 admin-token
        with closing(sqlite3.connect(str(database))) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0], events)

    def test_bytecode_cache_is_not_release_payload(self):
        self.invoke('--release', str(self.v1))
        version = self.prefix / 'versions/1.0.0'
        cache = version / 'agent_collab/__pycache__'
        cache.mkdir(exist_ok=True)
        (cache / 'cli.cpython-313.pyc').write_bytes(b'cache')
        self.assertEqual(ins.read_installed(version)['version'], '1.0.0')
        if os.name != 'nt':  # 缓存目录里的符号链接仍被拒绝（Windows 建链接需要特权）
            (self.temp / 'elsewhere.pyc').write_bytes(b'tampered')
            (cache / 'evil.cpython-313.pyc').symlink_to(self.temp / 'elsewhere.pyc')
            with self.assertRaisesRegex(ValueError, 'symlink'):
                ins.read_installed(version)
            (cache / 'evil.cpython-313.pyc').unlink()
        (version / 'agent_collab/extra.py').write_text('x')  # 真正的多余文件仍被拒绝
        with self.assertRaises(ValueError):
            ins.read_installed(version)

    def test_windows_launcher_has_utf8_bom(self):
        old, config = self.installed_worker()
        config.update(system='Windows', python='C:\\Users\\Jos\u00e9\\Python\\python.exe')
        ins.write_launchers(config)
        data = (Path(config['prefix']) / 'bin/agent-collab.ps1').read_bytes()
        self.assertTrue(data.startswith(b'\xef\xbb\xbf'))  # PowerShell 5.1 才会按 UTF-8 读
        self.assertIn('Jos\u00e9', data.decode('utf-8-sig'))

    @unittest.skipIf(os.name == 'nt', 'symlink creation needs privileges on Windows')
    def test_admin_token_file_symlink_is_not_resolved(self):
        target = self.temp / 'real.token'
        target.write_text('t')
        link = self.temp / 'link.token'
        link.symlink_to(target)
        args = ins.parser().parse_args(['--role', 'worker', '--no-service', '--admin-token-file', str(link)])
        self.assertEqual(ins.configuration(args, None, 'Linux')['admin_token_file'], str(link))

    def test_task_uses_pythonw_and_unbounded_repetition_with_ignore_new(self):
        old, config = self.installed_worker()
        python = self.temp / 'Python/python.exe'
        python.parent.mkdir()
        python.write_bytes(b'fixture')
        pythonw = python.with_name('pythonw.exe')
        config.update(system='Windows', python=str(python))
        directory = self.prefix / 'versions' / old['version']
        def task():
            return ET.fromstring(ins.render_services(config, directory, 'S-1-5-21-1-2-3-4')['agent-collab-worker.xml'])
        self.assertEqual(task().find('.//{*}Command').text, str(python))
        pythonw.write_bytes(b'fixture')
        xml = task()
        self.assertEqual(xml.find('.//{*}Command').text, str(pythonw))
        # controller 也是后台服务：同样不用会新建控制台的 python.exe（all-in-one 的两个任务都用 pythonw.exe）。
        both = ins.render_services(dict(config, role='all-in-one'), directory, 'S-1-5-21-1-2-3-4')
        self.assertEqual({name: ET.fromstring(data).find('.//{*}Command').text for name, data in both.items()},
                         {'agent-collab-controller.xml': str(pythonw), 'agent-collab-worker.xml': str(pythonw)})
        # 重复挂在时间触发器上（注册即生效）；登录触发器只负责登录时立即启动。
        self.assertIsNone(xml.find('.//{*}LogonTrigger/{*}Repetition'))
        timer = xml.find('.//{*}TimeTrigger')
        repetition = timer.find('{*}Repetition')
        self.assertEqual(repetition.find('{*}Interval').text, 'PT1M')
        self.assertEqual(repetition.find('{*}StopAtDurationEnd').text, 'false')
        self.assertIsNone(repetition.find('{*}Duration'))
        # 起点在过去（注册后立即排定下一次运行），不带结束时间
        self.assertLess(datetime.datetime.fromisoformat(timer.find('{*}StartBoundary').text), datetime.datetime.now())
        self.assertIsNone(timer.find('{*}EndBoundary'))
        self.assertEqual(xml.find('.//{*}MultipleInstancesPolicy').text, 'IgnoreNew')
        self.assertIsNotNone(xml.find('.//{*}RestartOnFailure'))
        self.assertEqual(ins.worker_python(r'C:\Python315\python.exe', exists=lambda path: True), r'C:\Python315\pythonw.exe')

    def test_windows_default_and_override_workspace_config(self):
        self.assertEqual(ins.default_windows_workspace({'SystemDrive': 'D:'}, 'user'), r'D:\agent-collab-work\user')
        args = ins.parser().parse_args(['--workspace-root', str(self.temp / 'explicit'), '--no-service'])
        config = ins.configuration(args, None, 'Windows')
        self.assertEqual(config['workspace_root'], str(self.temp / 'explicit'))

    def test_acl_command_builder_limits_sandbox_group_to_folder_rx(self):
        sid = 'S-1-5-21-1-2-3-4'
        commands = ins.workspace_acl_commands(r'C:\agent-collab-work', r'C:\agent-collab-work\user', sid, True)
        self.assertEqual(len(commands), 4)
        for index in (1, 3):
            self.assertIn('/inheritance:r', commands[index])
            self.assertIn('*' + sid + ':(OI)(CI)F', commands[index])
            self.assertIn('*S-1-5-18:(OI)(CI)F', commands[index])
            self.assertIn('*S-1-5-32-544:(OI)(CI)F', commands[index])
            self.assertIn('CodexSandboxUsers:RX', commands[index])
            self.assertNotIn('/T', commands[index])
        self.assertFalse(any('CodexSandboxUsers' in arg for command in ins.workspace_acl_commands('root', 'user', sid) for arg in command))

    def test_workspace_setup_uses_injected_acl_runner_and_preserves_all_custom_keys(self):
        old, config = self.installed_worker()
        workspace = self.temp / 'agent-collab-work/user'
        custom = dict(node_id='worker', codex_command=['custom', 'arg'], labels=['private'], auth_probe_s=120,
                      workspace_root=str(workspace), extension={'future': True})
        path = self.state / 'worker/agent.json'
        save(path, custom)
        before = path.read_bytes()
        config.update(system='Windows', workspace_root=str(self.temp / 'ignored'))
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, '"user","S-1-5-21-1-2-3-4"\n', '')
        with patch.object(ins, 'default_windows_workspace', return_value=str(workspace)):
            ins.configure_worker_workspace(config, runner)  # 生成的缺省路径：共享父目录与用户目录都设 ACL
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(workspace.is_dir())
        self.assertEqual([c[0] for c in calls], ['whoami', 'net', 'icacls', 'icacls', 'icacls', 'icacls'])
        self.assertEqual({c[1] for c in calls if c[0] == 'icacls'}, {str(workspace.parent), str(workspace)})
        calls.clear()
        ins.configure_worker_workspace(config, runner)  # 同一路径但不是缺省值：只改它自己
        self.assertEqual({c[1] for c in calls if c[0] == 'icacls'}, {str(workspace)})
        calls.clear()
        custom.pop('workspace_root')
        save(path, custom)
        config['workspace_root'] = str(workspace)
        ins.configure_worker_workspace(config, runner)
        data = ins.read_json(path)
        self.assertEqual(data.pop('workspace_root'), str(workspace))
        self.assertEqual(data, custom)
        save(path, dict(custom, workspace_root=None))
        calls.clear()
        ins.configure_worker_workspace(config, runner)
        self.assertFalse(calls)
        self.assertIsNone(ins.read_json(path)['workspace_root'])

    def test_active_attempt_refuses_upgrade_before_service_or_staging_and_allow_override(self):
        old, config = self.installed_worker()
        meta = self.state / 'worker/attempts/fixture/meta.json'
        for state in ('preparing', 'running'):
            save(meta, {'state': state})
            with patch.object(ins.Services, 'stop', side_effect=AssertionError('must not stop')):
                output = self.invoke('--release', str(self.v2), role='worker', status=1)
            self.assertIn('drain first', output)
            self.assertFalse((self.prefix / 'versions/2.0.0').exists())
        self.invoke('--release', str(self.v2), '--allow-active', role='worker')
        self.assertEqual(ins.current_version(self.prefix), '2.0.0')
        self.invoke('--rollback', role='worker', status=1)
        self.assertEqual(ins.current_version(self.prefix), '2.0.0')
        self.invoke('--rollback', '--allow-active', role='worker')
        self.assertEqual(ins.current_version(self.prefix), '1.0.0')

    def test_new_worker_status_must_be_fresh_and_match_directory_version_commit(self):
        old, config = self.installed_worker()
        directory = self.prefix / 'versions/2.0.0'
        manifest = dict(version='2.0.0', git_commit='new-commit')
        started = 1804800000.0
        at = datetime.datetime.fromtimestamp(started + 1, datetime.timezone.utc).isoformat()
        good = dict(version='2.0.0', git_commit='new-commit', version_dir=str(directory), pid=42, last_heartbeat_ok_at=at)
        path = self.state / 'worker/status.json'
        for changed in (dict(version='1.0.0'), dict(git_commit='old'), dict(version_dir=str(directory.parent / '1.0.0')),
                        dict(pid=0), dict(last_heartbeat_ok_at='2020-01-01T00:00:00Z')):
            save(path, dict(good, **changed))
            with self.assertRaisesRegex(RuntimeError, 'successful heartbeat'):
                ins.wait_worker_status(config, directory, manifest, started, timeout=0)
        save(path, good)
        self.assertEqual(ins.wait_worker_status(config, directory, manifest, started, timeout=0), good)

    def test_status_deadline_follows_the_caller(self):
        # service start --timeout 可以等满自己的时限；安装器的 60 秒上限由 transaction 传入（见下一个用例）。
        old, config = self.installed_worker()
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
        with self.assertRaises(RuntimeError):
            ins.wait_worker_status(config, self.prefix / 'new', {'version': '2'}, 0,
                                   timeout=120, clock=lambda: clock[0], sleep=sleep)
        self.assertEqual(clock[0], 120)

    def test_missing_new_heartbeat_rolls_back_pointer_and_restarts_previous_worker(self):
        old, config = self.installed_worker()
        config['service'] = True
        old['config'] = config
        manifest, files = ins.read_release(self.v2)
        new_manager, old_manager = Mock(), Mock()
        with patch.object(ins, 'Services', side_effect=[new_manager, old_manager]), \
             patch.object(ins, 'wait_worker_status', side_effect=RuntimeError('health confirmation failed')) as health, \
             patch.object(ins, 'wait_doctor', side_effect=AssertionError('must check running code first')), \
             redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'health confirmation failed'):
                ins.transaction(config, old, manifest, files, ins.parser().parse_args(['--health-timeout', '600']))
        health.assert_called_once()
        self.assertEqual(health.call_args.kwargs['timeout'], 60)  # 安装与升级的心跳确认最多等 60 秒
        new_manager.start.assert_called_once()
        new_manager.stop.assert_called_once()
        old_manager.start.assert_called_once()
        self.assertEqual(ins.current_version(self.prefix), old['version'])
        self.assertEqual(ins.read_json(self.prefix / 'installation.json')['version'], old['version'])

    def test_successful_upgrade_keeps_agent_configuration_bytes(self):
        old, config = self.installed_worker()
        path = self.state / 'worker/agent.json'
        custom = dict(node_id='node-a', controller='https://example.invalid', disabled_harnesses={'codex': 'review'},
                      labels=['custom'], provider_account_refs={'codex': 'local'}, extension={'untouched': [1, 2]},
                      workspace_root=str(self.temp / 'workspace'))
        save(path, custom)
        (self.state / 'worker/node.token').write_text('fixture')
        before = path.read_bytes()
        setup = ins.configure_worker_workspace
        def acl_runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, '"user","S-1-5-21-1-2-3-4"\n', '')
        with patch.object(ins, 'drain_client', return_value=(object(), 'node-a')), \
             patch.object(ins, 'drain_and_wait', return_value=False), patch.object(ins, 'set_drain'), \
             patch.object(ins, 'configure_worker_workspace', side_effect=lambda c: setup(c, acl_runner)):
            self.invoke('--release', str(self.v2), role='worker')
        self.assertEqual(path.read_bytes(), before)


class WaitLockTests(unittest.TestCase):
    """Windows schtasks /End 返回时进程可能尚未退出：锁短暂被占用不应中止升级。"""

    def test_retries_while_controller_lock_is_held(self):
        attempts = []

        class ControllerBusy:
            def __enter__(self):
                attempts.append(1)
                if len(attempts) < 3:  # ControllerLock 被占用时抛 RuntimeError
                    raise RuntimeError('controller.lock is held; stop the controller before restore/another serve')
                return self

        clock = iter(range(100))
        self.assertIsInstance(ins.wait_lock(ControllerBusy, timeout=10, clock=lambda: next(clock), sleep=lambda s: None),
                              ControllerBusy)
        self.assertEqual(len(attempts), 3)

    def test_retries_until_released_then_times_out(self):
        attempts = []

        class Busy:
            def __enter__(self):
                attempts.append(1)
                if len(attempts) < 3:
                    raise ValueError('another agent owns this data directory')
                return self

            def __exit__(self, *args):
                pass

        clock = iter(range(100))
        lock = ins.wait_lock(Busy, timeout=10, clock=lambda: next(clock), sleep=lambda s: None)
        self.assertIsInstance(lock, Busy)
        self.assertEqual(len(attempts), 3)

        class Held(Busy):
            def __enter__(self):
                raise ValueError('another agent owns this data directory')

        clock = iter(range(100))
        with self.assertRaisesRegex(ValueError, 'another agent'):
            ins.wait_lock(Held, timeout=3, clock=lambda: next(clock), sleep=lambda s: None)


class ReleaseAndCliReviewTests(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        root = Path(__file__).resolve().parents[1]
        (root / '.dev/tmp').mkdir(parents=True, exist_ok=True)
        self.temp = Path(tempfile.mkdtemp(dir=str(root / '.dev/tmp')))
        self.addCleanup(shutil.rmtree, str(self.temp))
        self.root = root

    def test_version_rejects_windows_device_names_and_trailing_dots(self):
        for good in ('1.0.0', '0.1.0rc7', 'ui-test'):
            self.assertEqual(ins.validate_version(good), good)
        for bad in ('CON', 'nul', 'Nul.1', 'COM1', '1.0.', 'lpt9.tar'):
            with self.assertRaises(ValueError):
                ins.validate_version(bad)

    def test_release_tree_check_is_linear_and_finds_collisions(self):
        import time
        deep = 'agent_collab/' + '/'.join(['d'] * 20000) + '/f.py'
        started = time.monotonic()
        ins.check_release_tree([deep, 'agent_collab/x.py'])
        self.assertLess(time.monotonic() - started, 2)  # 旧实现按前缀拼接字符串，深度 2 万时为平方级
        for names in (['agent_collab/A/x.py', 'agent_collab/a/y.py'],      # 目录别名
                      ['agent_collab/x.py', 'agent_collab/X.py'],          # 文件别名
                      ['agent_collab/caf\u00e9.py', 'agent_collab/cafe\u0301.py'],  # NFC 后相同
                      ['agent_collab/x', 'agent_collab/x/y.py']):          # 文件又是目录
            with self.assertRaises(ValueError):
                ins.check_release_tree(names)

    def test_interrupted_build_leaves_no_partial_release(self):
        out = self.temp / 'staged'
        real = legacy.builder.zipfile.ZipFile

        def broken(*args, **kwargs):
            raise OSError('disk full')

        with patch.object(legacy.builder.zipfile, 'ZipFile', side_effect=broken):
            with self.assertRaises(OSError):
                legacy.builder.build_release(self.root, out, '7.7.7')
        self.assertEqual(list(out.iterdir()), [])  # 连 tar.gz 也没有发布，临时文件已清理
        self.assertIs(legacy.builder.zipfile.ZipFile, real)
        paths = legacy.builder.build_release(self.root, out, '7.7.7')  # 同版本可以直接重试
        self.assertTrue(all(p.exists() for p in paths))

    def test_build_validates_before_writing_and_refuses_existing_version(self):
        out = self.temp / 'out'
        with patch.object(legacy.builder, 'validate_payload', side_effect=ValueError('collision')):
            with self.assertRaises(ValueError):
                legacy.builder.build_release(self.root, out, '9.9.9')
        self.assertFalse(list(out.glob('*')) if out.exists() else [])  # 校验失败时一个包都不生成
        legacy.builder.build_release(self.root, out, '9.9.9')
        with self.assertRaisesRegex(ValueError, 'new version'):
            legacy.builder.build_release(self.root, out, '9.9.9')

    def test_caddy_comment_braces_do_not_change_block_depth(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('caddy_site', self.root / 'installer/caddy_site.py')
        caddy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(caddy)
        text = ('a.example {\n\trespond ok\n}\n# controller\nhttps://x.example:8443 {\n\t# 上游见下 }\n'
                '\treverse_proxy 127.0.0.1:18669 # 本机 {端口}\n}\nb.example {\n\trespond b\n}\n')
        self.assertEqual(caddy.remove_site(text, 'https://x.example:8443'),
                         'a.example {\n\trespond ok\n}\nb.example {\n\trespond b\n}\n')

    def test_cli_counts_only_2xx_as_success_and_never_follows_redirects(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading
        from agent_collab import cli
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get('Authorization')))
                self.send_response(302)
                self.send_header('Location', 'http://127.0.0.1:%d/elsewhere' % self.server.server_port)
                self.end_headers()
            do_POST = do_GET

            def log_message(self, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        status, _ = cli.request('http://127.0.0.1:%d' % server.server_port, 'secret', 'GET', '/overview')
        self.assertEqual(status, 302)
        self.assertEqual([path for path, _ in seen], ['/api/v1/overview'])  # 令牌不会被带到重定向目标
        token = self.temp / 'admin.token'
        token.write_text('secret')
        for code, expected in ((200, 0), (204, 0), (307, 2), (308, 2), (404, 2)):
            with patch.object(cli, 'request', return_value=(code, None)), redirect_stdout(io.StringIO()):
                self.assertEqual(cli._client(['GET', '/overview', '--token-file', str(token)], False), expected)
