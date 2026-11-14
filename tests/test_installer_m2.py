"""Desktop installer contracts: local fixtures and service-manager command stubs only."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from agent_collab import installer as ins
import test_installer as legacy


# 在 Windows 主机上模拟 POSIX 系统的升级需要替换 current 符号链接，普通用户做不到（与既有用例的跳过条件相同）；
# 这类用例在 Windows 上改用 Windows 的服务管理器来跑同一条性质，纯 launchd 断言的用例跳过。
HOST = 'Windows' if os.name == 'nt' else 'Darwin'


class InstallerM2Tests(unittest.TestCase):
    setUpClass = classmethod(legacy.InstallerTests.setUpClass.__func__)
    tearDownClass = classmethod(legacy.InstallerTests.tearDownClass.__func__)
    setUp = legacy.InstallerTests.setUp

    def config(self, system='Darwin', role='worker', instance=''):
        args = ins.parser().parse_args(['--prefix', str(self.prefix), '--state-dir', str(self.state),
                                       '--role', role, '--instance=' + instance])
        return ins.configuration(args, None, system)

    def installation(self, system='Darwin', role='worker', desired=None, instance=''):
        config = self.config(system, role, instance)
        self.prefix.mkdir(parents=True, exist_ok=True)
        for kind in ins.components(config):
            (self.state / kind).mkdir(parents=True, exist_ok=True)
        (self.state / 'logs').mkdir(exist_ok=True)
        if 'worker' in ins.components(config):
            ins.write_json(self.state / 'worker/agent.json', dict(node_id=config['node_id'],
                controller=config['controller_url'], node_token_sha256='private-hash',
                workspace_root=str(self.temp / 'workspace')))
            (self.state / 'worker/node.token').write_text('private-token')
        manifest, files = ins.read_release(self.v1)
        directory = ins.install_version(self.prefix, manifest, files)
        ins.switch_current(self.prefix, manifest['version'], system)
        record = dict(version=manifest['version'], previous=None, config=config)
        if desired is not None:
            record['desired'] = desired
        ins.write_json(self.prefix / 'installation.json', record)
        return config, directory

    def invoke(self, args, expected=0):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = ins.main(args)
        self.assertEqual(result, expected, out.getvalue() + err.getvalue())
        return out.getvalue(), err.getvalue()

    def managers(self, system, autostart='enabled'):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.calls = []
        def runner(argv, **kwargs):
            argv = [str(x) for x in argv]
            self.calls.append(argv)
            if argv[0] == 'whoami':
                text = '"user","S-1-5-21-1-2-3-4"'
            elif '/Query' in argv:  # 按名字查询失败、列出全部任务成功且其中没有它：确实不存在
                return subprocess.CompletedProcess(argv, 1 if '/TN' in argv else 0, '', '')
            elif 'is-enabled' in argv:
                text = autostart
            elif 'print-disabled' in argv:
                text = 'disabled services = { "org.agent-collab.worker" => ' + autostart + ' }'
            elif system == 'Windows' and autostart == 'disabled' and any('Get-ScheduledTask' in x for x in argv):
                text = 'Disabled'  # 计划任务的自启动状态与运行状态来自同一个查询
            else:
                text = 'state = running' if system == 'Darwin' else 'Running' if system == 'Windows' else 'active'
            return subprocess.CompletedProcess(argv, 0, text, '')
        stack.enter_context(patch.object(ins, 'run', side_effect=runner))
        stack.enter_context(patch.object(ins, 'release_compatibility', return_value={'decision': 'accept'}))
        stack.enter_context(patch.object(ins.platform, 'system', return_value=system))
        stack.enter_context(patch.object(ins.os, 'getuid', return_value=501, create=True))
        stack.enter_context(patch.object(ins.Path, 'home', return_value=self.temp / 'home'))
        return stack

    def assert_not_started(self):
        for command in self.calls:
            self.assertFalse(set(command) & {'enable', 'start', 'bootstrap', 'kickstart', '/ENABLE', '/Run'}, command)

    def test_stopped_worker_upgrade_reinstall_and_rollback_all_platforms(self):
        for system in ('Linux', 'Darwin', 'Windows') if os.name != 'nt' else ('Windows',):
            with self.subTest(system=system):
                self.prefix, self.state = self.temp / system, self.temp / (system + '-state')
                config, directory = self.installation(system, desired={'worker': 'stopped'})
                identity = (self.state / 'worker/agent.json').read_bytes()
                with self.managers(system), \
                        patch.object(ins, 'wait_worker_status', side_effect=AssertionError('stopped heartbeat')), \
                        patch.object(ins, 'drain_client', side_effect=AssertionError('stopped drain')), \
                        patch.object(ins, 'package_command', side_effect=AssertionError('stopped online doctor')):
                    for options in (['--release', str(self.v2)], ['--release', str(self.v2)], ['--rollback']):
                        out, err = self.invoke(['--prefix', str(self.prefix)] + options)
                        self.assertIn('kept stopped: worker', out)
                        self.assert_not_started()
                        self.assertEqual(ins.read_json(self.prefix / 'installation.json')['desired'], {'worker': 'stopped'})
                    self.assertEqual(ins.current_version(self.prefix, system), '1.0.0')
                    if system == 'Windows':
                        xml = ET.fromstring((self.prefix / 'services/agent-collab-worker.xml').read_bytes())
                        self.assertEqual(xml.find('{*}Settings/{*}Enabled').text, 'false')
                self.assertEqual((self.state / 'worker/agent.json').read_bytes(), identity)
                self.assertEqual((self.state / 'worker/node.token').read_text(), 'private-token')

    def test_instance_names_render_and_commands_all_platforms(self):
        for system in ('Linux', 'Darwin', 'Windows'):
            config = self.config(system, role='all-in-one', instance='candidate-1')
            directory = legacy.ROOT
            rendered = ins.render_services(config, directory, 'S-1-5-21-1-2-3-4')
            for kind in ('controller', 'worker'):
                name = ('org.agent-collab.' + kind + '.candidate-1.plist' if system == 'Darwin' else
                        'agent-collab-' + kind + '-candidate-1' + ('.service' if system == 'Linux' else '.xml'))
                self.assertIn(name, rendered)
                if system == 'Darwin':
                    self.assertEqual(plistlib.loads(rendered[name])['Label'], name[:-6])
            with self.managers(system):
                manager = ins.Services(config, directory)
                manager.start()
                manager.stop(disable_autostart=True)
                manager.status()
                manager.autostart()
                for command in self.calls:
                    if command[:2] != ['launchctl', 'print-disabled']:
                        self.assertIn('candidate-1', ' '.join(command))

    def test_instance_validation_and_existing_installation_cannot_change(self):
        for name in ('A', '-a', 'a_b', 'a' * 32, '../other'):
            with self.assertRaises(ValueError):
                self.config(instance=name)
        config, directory = self.installation(HOST, desired={'worker': 'running'}, instance='candidate')
        out, err = self.invoke(['--prefix', str(self.prefix), '--instance', 'other', '--release', str(self.v2)], 1)
        self.assertIn('instance', err)
        self.assertEqual(ins.current_version(self.prefix, HOST), '1.0.0')

    def runtime_source(self, name, form='directory', executable='bin/python3'):
        if os.name == 'nt':
            self.skipTest('runtime fixture uses a POSIX wrapper around the local interpreter')
        root = self.temp / name
        python = root / executable
        python.parent.mkdir(parents=True)
        # 只借用本机解释器执行探测/发行代码，不复制平台相关的完整标准库。
        python.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
        python.chmod(0o755)
        (root / 'marker').write_text(name)
        if form == 'directory':
            return root
        archive = self.temp / (name + ('.tar.gz' if form == 'tar' else '.zip'))
        if form == 'tar':
            with tarfile.open(str(archive), 'w:gz') as output:
                for path in root.rglob('*'):
                    output.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
        else:
            with zipfile.ZipFile(str(archive), 'w') as output:
                for path in root.rglob('*'):
                    output.write(path, path.relative_to(root).as_posix())
        return archive

    def offline_args(self, *options):
        return ['--prefix', str(self.prefix), '--state-dir', str(self.state), '--role', 'worker',
                '--no-service'] + list(options)

    def snapshot(self, directory):
        return {path.relative_to(directory).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
                for path in directory.rglob('*') if path.is_file()}

    # 断言的是 launchd plist：在 Linux 主机上也按 macOS 渲染服务。
    @patch.object(ins.platform, 'system', return_value='Darwin')
    def test_runtime_directory_tar_zip_installs_and_reuses_without_writes(self, _system):
        for form in ('directory', 'tar', 'zip'):
            with self.subTest(form=form):
                self.prefix, self.state = self.temp / (form + '-code'), self.temp / (form + '-state')
                source = self.runtime_source('runtime-' + form, form)
                args = self.offline_args('--release', str(self.v1), '--runtime', str(source))
                self.invoke(args)
                config = ins.read_json(self.prefix / 'installation.json')['config']
                runtime = config['runtime']
                self.assertTrue(runtime['managed'])
                self.assertRegex(runtime['id'], r'^[0-9a-f]{16}$')
                if form != 'directory':
                    self.assertEqual(runtime['id'], hashlib.sha256(source.read_bytes()).hexdigest()[:16])
                root = self.prefix / 'runtime' / runtime['id']
                self.assertEqual(config['python'], str(root / 'bin/python3'))
                self.assertEqual(runtime['python'], config['python'])
                rendered = ins.render_services(config, self.prefix / 'versions/1.0.0')
                self.assertIn(config['python'], plistlib.loads(next(iter(rendered.values())))['ProgramArguments'])
                before, contents = self.snapshot(root), legacy.hashes(root)
                self.invoke(args)
                self.assertEqual(self.snapshot(root), before)
                self.assertEqual(legacy.hashes(root), contents)

    # 断言的是 launchd plist：在 Linux 主机上也按 macOS 渲染服务。
    @patch.object(ins.platform, 'system', return_value='Darwin')
    def test_runtime_upgrade_rollback_cleanup_and_uninstall(self, _system):
        first, second = self.runtime_source('first'), self.runtime_source('second', 'zip')
        self.invoke(self.offline_args('--release', str(self.v1), '--runtime', str(first)))
        old = ins.read_json(self.prefix / 'installation.json')['config']
        root = Path(old['python']).parents[1]
        orphan = self.prefix / 'runtime' / ('a' * 16)
        orphan.mkdir()
        (orphan / 'old').write_text('unreferenced')
        self.invoke(self.offline_args('--release', str(self.v2), '--runtime', str(second)))
        new = ins.read_json(self.prefix / 'installation.json')['config']
        self.assertTrue(root.is_dir())
        self.assertFalse(orphan.exists())
        self.invoke(self.offline_args('--rollback'))
        config = ins.read_json(self.prefix / 'installation.json')['config']
        self.assertEqual(config['python'], old['python'])
        self.assertTrue(Path(new['python']).is_file())
        self.assertIn(old['python'], (self.prefix / 'bin/agent-collab').read_text())
        rendered = plistlib.loads((self.prefix / 'services/agent-collab-worker.plist').read_bytes())
        self.assertEqual(rendered['ProgramArguments'][0], old['python'])
        self.invoke(self.offline_args('--uninstall'))
        self.assertTrue((self.prefix / 'runtime').is_dir())
        self.invoke(self.offline_args('--uninstall', '--purge-code'))
        self.assertFalse((self.prefix / 'runtime').exists())

    @patch.object(ins.platform, 'system', return_value='Darwin')
    def test_purge_keeps_the_runtime_this_uninstaller_runs_from(self, _system):
        # Windows 上经已安装的启动器卸载时，进程就是 runtime 里的 Python，删不掉它的 DLL；其余照常卸载，再次卸载删掉运行时。
        self.invoke(self.offline_args('--release', str(self.v1), '--runtime', str(self.runtime_source('first'))))
        with patch.object(ins, 'runtime_in_use', return_value=True):
            out, _ = self.invoke(self.offline_args('--uninstall', '--purge-code'))
        self.assertIn('was kept', out)
        self.assertTrue((self.prefix / 'runtime').is_dir())
        self.assertFalse((self.prefix / 'versions').exists())
        self.assertFalse((self.prefix / 'bin').exists())
        self.assertIsNone(ins.read_json(self.prefix / 'installation.json')['version'])
        self.invoke(self.offline_args('--uninstall', '--purge-code'))
        self.assertFalse((self.prefix / 'runtime').exists())

    @patch.object(ins.platform, 'system', return_value='Darwin')
    def test_uninstall_finishes_a_purge_whose_runtime_is_already_broken(self, _system):
        # 上一次 --purge-code 删运行时删到一半（Windows 上被别的进程占着文件）：解释器已经残缺，再次卸载不能先去运行它。
        self.invoke(self.offline_args('--release', str(self.v1), '--runtime', str(self.runtime_source('first'))))
        with patch.object(ins, 'runtime_in_use', return_value=True):
            self.invoke(self.offline_args('--uninstall', '--purge-code'))
        for python in (self.prefix / 'runtime').rglob('python3'):
            python.write_text('#!/bin/sh\nexit 1\n')
        self.invoke(self.offline_args('--uninstall', '--purge-code'))
        self.assertFalse((self.prefix / 'runtime').exists())

    def test_runtime_in_use_only_for_the_running_interpreter_on_windows(self):
        here = Path(sys.executable).resolve().parent
        self.assertEqual(ins.runtime_in_use(here), os.name == 'nt')
        self.assertFalse(ins.runtime_in_use(self.temp))

    def test_runtime_unsafe_archives_rejected_without_partial_directories(self):
        for form, name, link in (('tar', '../escape', None), ('zip', '/absolute', None),
                                 ('tar', 'bin/python3', '../../../outside'),
                                 ('zip', 'bin/python3', '/outside')):
            archive = self.temp / ('bad.' + ('tar.gz' if form == 'tar' else 'zip'))
            if form == 'tar':
                with tarfile.open(str(archive), 'w:gz') as output:
                    info = tarfile.TarInfo(name)
                    if link:
                        info.type, info.linkname = tarfile.SYMTYPE, link
                        output.addfile(info)
                    else:
                        info.size = 1
                        output.addfile(info, io.BytesIO(b'x'))
            else:
                with zipfile.ZipFile(str(archive), 'w') as output:
                    info = zipfile.ZipInfo(name)
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16 if link else 0
                    output.writestr(info, link or 'x')
            self.invoke(self.offline_args('--release', str(self.v1), '--runtime', str(archive)), 1)
            self.assertFalse(list((self.prefix / 'runtime').glob('*')))
            self.assertIsNone(ins.current_version(self.prefix))

    def test_login_name_falls_back_to_the_uid_only_on_posix(self):
        with patch.object(ins.getpass, 'getuser', side_effect=OSError('no passwd entry')):
            with patch.object(ins.os, 'name', 'nt'):
                with self.assertRaises(ValueError):
                    ins.login_name()
            if os.name != 'nt':
                self.assertEqual(ins.login_name(), str(os.getuid()))

    def test_scheduled_task_that_cannot_be_queried_is_not_treated_as_missing(self):
        config, directory = self.installation('Windows')
        with self.managers('Windows'), patch.object(ins, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'access denied')):
            services = ins.Services(config, directory)
            self.assertEqual(set(services.ownership().values()), {'unknown'})
            with self.assertRaises(ValueError):
                services.check_ownership()

    def test_runtime_invalid_probe_preserves_pointer_and_record_offline(self):
        self.invoke(self.offline_args('--release', str(self.v1)))
        before = (self.prefix / 'installation.json').read_bytes()
        source = self.runtime_source('broken')
        (source / 'bin/python3').write_text('#!/bin/sh\nexit 1\n')
        self.invoke(self.offline_args('--release', str(self.v2), '--runtime', str(source)), 1)
        self.assertEqual((self.prefix / 'installation.json').read_bytes(), before)
        self.assertEqual(ins.current_version(self.prefix), '1.0.0')
        self.assertFalse(list((self.prefix / 'runtime').glob('*')))

    def test_runtime_and_python_are_mutually_exclusive(self):
        out, err = self.invoke(self.offline_args('--release', str(self.v1), '--runtime', 'unused', '--python', sys.executable), 1)
        self.assertIn('--runtime', err)
        self.assertIn('--python', err)
        self.assertFalse(self.prefix.exists())

    def test_discover_read_only_complete_and_never_reads_token(self):
        config, directory = self.installation(desired={'worker': 'stopped'}, instance='candidate')
        before = self.snapshot(self.temp)
        read_text = Path.read_text
        def guarded(path, *args, **kwargs):
            self.assertNotEqual(path.name, 'node.token')
            return read_text(path, *args, **kwargs)
        with self.managers('Darwin'), patch.object(Path, 'read_text', new=guarded), \
                patch.object(ins, 'atomic_write', side_effect=AssertionError('read only')):
            out, err = self.invoke(['--discover', '--prefix', str(self.prefix), '--instance', 'candidate'])
        report = json.loads(out)
        self.assertEqual(set(report), {'prefix', 'installed', 'version', 'previous_version', 'role', 'instance',
            'service', 'desired', 'desired_source', 'services', 'autostart', 'service_owner', 'runtime', 'python',
            'state_dir', 'identity', 'controller_data', 'context', 'interrupted'})
        self.assertEqual(report['identity'], {'node_id': config['node_id'], 'controller': config['controller_url'], 'paired': True})
        self.assertNotIn('private', out)
        self.assertEqual(report['desired_source'], 'recorded')
        self.assertEqual(report['context']['worker'], dict(manager='launchd', scope='user-session', runs_without_login=False))
        self.assertEqual(self.snapshot(self.temp), before)

    def test_legacy_desired_inferred_on_status_upgrade_and_rollback(self):
        config, directory = self.installation(HOST)
        with self.managers(HOST, autostart='disabled'):
            before = self.snapshot(self.prefix)
            # 计划任务的运行状态与自启动状态来自同一个查询：被停用的任务不是 active，status 以 1 结束；
            # launchd 的替身只把自启动报成停用，进程仍算在运行。
            out, err = self.invoke(['service', 'status', '--prefix', str(self.prefix)], 1 if HOST == 'Windows' else 0)
            self.assertEqual(json.loads(out)['desired'], {'worker': 'stopped'})
            self.assertEqual(json.loads(out)['desired_source'], 'inferred')
            self.assertEqual(self.snapshot(self.prefix), before)
            self.invoke(['--prefix', str(self.prefix), '--release', str(self.v2)])
            record = ins.read_json(self.prefix / 'installation.json')
            self.assertEqual(record['desired'], {'worker': 'stopped'})
            self.assertEqual(record['previous']['desired'], {'worker': 'stopped'})
            del record['desired']
            ins.write_json(self.prefix / 'installation.json', record)
            self.invoke(['--prefix', str(self.prefix), '--rollback'])
            self.assertEqual(ins.read_json(self.prefix / 'installation.json')['desired'], {'worker': 'stopped'})
            self.assert_not_started()
        for state, expected in (('disabled', 'stopped'), ('enabled', 'running'), ('unknown', 'running')):
            self.assertEqual(ins.desired_state(None, config, {'worker': state}), ({'worker': expected}, 'inferred'))

    def test_temporary_stop_keeps_running_desired_during_and_after_upgrade(self):
        config, directory = self.installation('Windows', desired={'worker': 'running'})
        with self.managers('Windows'), patch.object(ins, 'drain_client', return_value=(object(), 'node')), \
                patch.object(ins, 'drain_and_wait', return_value=False), patch.object(ins, 'set_drain'), \
                patch.object(ins, 'wait_worker_status'), patch.object(ins, 'wait_doctor', return_value={'ok': True}):
            stop = ins.Services.stop
            def observe(manager, *args, **kwargs):
                stop(manager, *args, **kwargs)
                self.assertEqual(ins.read_json(self.prefix / 'installation.json')['desired'], {'worker': 'running'})
            with patch.object(ins.Services, 'stop', new=observe):
                self.invoke(['--prefix', str(self.prefix), '--release', str(self.v2)])
            self.assertIn(['schtasks', '/Run', '/TN', 'agent-collab-worker'], self.calls)
            self.assertEqual(ins.read_json(self.prefix / 'installation.json')['desired'], {'worker': 'running'})

    def test_service_component_updates_only_after_success_and_keeps_instance(self):
        config, directory = self.installation('Darwin', 'all-in-one',
            desired={'controller': 'running', 'worker': 'running'}, instance='preview')
        base = ['--prefix', str(self.prefix), '--component', 'worker', '--timeout', '0']
        with self.managers('Darwin'):
            out, err = self.invoke(['service', 'stop'] + base)
            expected = {'controller': 'running', 'worker': 'stopped'}
            self.assertEqual(json.loads(out)['desired'], expected)
            self.assertFalse(any('org.agent-collab.controller' in ' '.join(c) for c in self.calls))
            self.assertIn(['launchctl', 'disable', 'gui/501/org.agent-collab.worker.preview'], self.calls)
            before = (self.prefix / 'installation.json').read_bytes()
            with patch.object(ins, 'wait_worker_status', side_effect=RuntimeError('no heartbeat')):
                self.invoke(['service', 'start'] + base, 1)
            self.assertEqual((self.prefix / 'installation.json').read_bytes(), before)
            with patch.object(ins, 'wait_worker_status', return_value={'pid': 42}):
                out, err = self.invoke(['service', 'start'] + base)
            self.assertEqual(json.loads(out)['desired'], {'controller': 'running', 'worker': 'running'})
            before = (self.prefix / 'installation.json').read_bytes()
            with patch.object(ins.Services, 'stop', side_effect=RuntimeError('stop failed')):
                self.invoke(['service', 'stop'] + base, 1)
            self.assertEqual((self.prefix / 'installation.json').read_bytes(), before)
            out, err = self.invoke(['service', 'stop', '--prefix', str(self.prefix), '--timeout', '0'])
            self.assertEqual(json.loads(out)['desired'], {'controller': 'stopped', 'worker': 'stopped'})

    def test_stopped_worker_automatic_rollback_and_interrupted_stop_never_start(self):
        for system in ('Linux', 'Darwin', 'Windows') if os.name != 'nt' else ('Windows',):
            with self.subTest(system=system):
                self.prefix, self.state = self.temp / system, self.temp / (system + '-state')
                self.installation(system, desired={'worker': 'stopped'})
                with self.managers(system):
                    with patch.object(ins, 'wait_doctor', side_effect=RuntimeError('health failed')):
                        self.invoke(['--prefix', str(self.prefix), '--release', str(self.v2)], 1)
                    self.assertEqual(ins.current_version(self.prefix, system), '1.0.0')
                    with patch.object(ins.Services, 'stop', side_effect=RuntimeError('partial stop')):
                        self.invoke(['--prefix', str(self.prefix), '--release', str(self.v2)], 1)
                    self.assert_not_started()
                    self.assertEqual(ins.read_json(self.prefix / 'installation.json')['desired'], {'worker': 'stopped'})

    @unittest.skipIf(os.name == 'nt', 'launchd 的命令断言；在 Windows 主机上无法替换模拟平台的 current 符号链接')
    def test_all_in_one_skips_online_checks_for_stopped_components(self):
        config, directory = self.installation('Darwin', 'all-in-one',
            desired={'controller': 'running', 'worker': 'stopped'})
        with self.managers('Darwin'), patch.object(ins, 'backup_controller'), \
                patch('agent_collab.client.Client.json', return_value={'database': {'writable': True}, 'blob_writable': True}), \
                patch.object(ins, 'admin_token', return_value='test'), \
                patch.object(ins, 'wait_worker_status', side_effect=AssertionError('stopped heartbeat')), \
                patch.object(ins, 'package_command', side_effect=AssertionError('stopped doctor')), \
                patch.object(ins, 'drain_client', side_effect=AssertionError('stopped drain')):
            out, err = self.invoke(['--prefix', str(self.prefix), '--release', str(self.v2)])
            self.assertIn('kept stopped: worker', out)
            self.assertIn(['launchctl', 'enable', 'gui/501/org.agent-collab.controller'], self.calls)
            self.assertNotIn(['launchctl', 'enable', 'gui/501/org.agent-collab.worker'], self.calls)

    def test_discover_missing_installation_reports_other_prefix_without_writes(self):
        with self.managers('Darwin'):
            config = self.config(instance='candidate')
            target = dict(ins.Services(config, legacy.ROOT).destinations())['worker']
            target.parent.mkdir(parents=True)
            target.write_bytes(plistlib.dumps({'WorkingDirectory': '/other/prefix/versions/1'}))
            before = self.snapshot(self.temp)
            out, err = self.invoke(['--discover', '--prefix', str(self.prefix), '--instance', 'candidate'])
            report = json.loads(out)
            self.assertFalse(report['installed'])
            self.assertEqual(report['service_owner']['worker'], 'other_prefix')
            self.assertIsNone(report['version'])
            self.assertIsNone(report['identity'])
            self.assertEqual(report['desired'], {})
            self.assertFalse(self.prefix.exists())
            self.assertEqual(self.snapshot(self.temp), before)
            with self.assertRaisesRegex(ValueError, 'another installation'):
                ins.Services(config, legacy.ROOT).check_ownership()

    def test_context_all_platforms_and_linux_linger_unknown(self):
        for system in ('Windows', 'Darwin', 'Linux'):
            config = self.config(system, 'all-in-one')
            for linger in ('yes', 'no', ''):
                with patch.object(ins, 'run', return_value=subprocess.CompletedProcess([], 0, linger, '')):
                    context = ins.service_context(config)
                self.assertEqual(set(context), {'controller', 'worker'})
                self.assertEqual(context['controller']['runs_without_login'], system == 'Linux')
                self.assertEqual(context['worker']['runs_without_login'], system == 'Linux' and linger == 'yes')
                self.assertEqual(context['worker']['scope'], 'user-session')
        with patch.object(ins, 'run', side_effect=FileNotFoundError):
            self.assertFalse(ins.service_context(self.config('Linux'))['worker']['runs_without_login'])

    def test_default_service_definitions_match_f7ebb3e_bytes(self):
        if os.name == 'nt':
            self.skipTest('baseline byte fixtures use POSIX paths')
        # 固定路径下由基线 f7ebb3e 生成的完整模板 SHA-256，覆盖文件名、argv 与序列化格式。
        # 有意的变化：controller 单元的 ReadWritePaths 加上 <state>/backups（维护 API 的在线备份写在那里）。
        expected = {
            'Linux': {'agent-collab-controller.service': '2e3408734f7055a5623cce5ccf39315a0736f5b42aa7c18b3a2a3d6442b336ce',
                      'agent-collab-worker.service': 'f152907307b89aaab62067e83ff0d9642c6dda4b612cf3098d358d49d748ecd5'},
            'Darwin': {'agent-collab-controller.plist': 'e058cc8cedee7f3fe64976ccba0f12f21c89a3bea8a2b55e1fd19f6c833c0dab',
                       'agent-collab-worker.plist': 'ed2c1bda5d5829e6db6d1a28ad07a1c61fe39937038e7b767c1035c6af6a65e3'},
            'Windows': {'agent-collab-controller.xml': '5f8c27472b8d7a810ac419a7701b3b75555b4be74c1d9a94ed4529a08716c3b9',
                        'agent-collab-worker.xml': '960b84d8ff1ca98999b9cfdb5265e019cb2ec46d1e552290b5454bc0c86a3026'}}
        templates = {kind: (legacy.ROOT / 'installer/services' / ('agent-collab-' + kind + '.service')).read_text()
                     for kind in ('controller', 'worker')}
        config = dict(prefix='/fixture/code', state_dir='/fixture/state', python='/fixture/python', role='all-in-one',
                      service=True, bind='127.0.0.1:18669', controller_url='http://127.0.0.1:18669')
        for system in expected:
            for extra in ({}, {'instance': ''}):
                with patch.object(Path, 'read_text', new=lambda path, **kw: templates['worker' if 'worker' in path.name else 'controller']):
                    rendered = ins.render_services(dict(config, system=system, **extra), Path('/fixture/versions/1'), 'S-1-5-21-1-2-3-4')
                self.assertEqual({name: hashlib.sha256(data).hexdigest() for name, data in rendered.items()}, expected[system])

    def test_instance_registration_ownership_and_removal_all_platforms(self):
        for system in ('Linux', 'Darwin', 'Windows'):
            self.prefix = self.temp / system
            self.prefix.mkdir()
            config = self.config(system, instance='candidate')
            manifest, files = ins.read_release(self.v1)
            directory = ins.install_version(self.prefix, manifest, files)
            with self.managers(system):
                manager = ins.Services(config, directory)
                manager.register()
                if system != 'Windows':
                    self.assertEqual(manager.ownership(), {'worker': 'this_prefix'})
                    config_other = dict(config, prefix=str(self.temp / 'other'))
                    with self.assertRaisesRegex(ValueError, 'another installation'):
                        ins.Services(config_other, legacy.ROOT).check_ownership()
                else:
                    xml = (self.prefix / 'services/agent-collab-worker-candidate.xml').read_text(encoding='utf-16')
                    with patch.object(ins, 'run', return_value=subprocess.CompletedProcess([], 0, xml, '')):
                        self.assertEqual(manager.ownership(), {'worker': 'this_prefix'})
                        with self.assertRaisesRegex(ValueError, 'another installation'):
                            ins.Services(dict(config, prefix='/other'), legacy.ROOT).check_ownership()
                manager.remove()
                if system != 'Windows':
                    self.assertFalse(dict(manager.destinations())['worker'].exists())
                for command in self.calls:
                    if '/TN' in command:
                        self.assertEqual(command[command.index('/TN') + 1], 'agent-collab-worker-candidate')
                self.assertFalse(any('agent-collab-worker.service' in c for c in self.calls))

    def test_new_install_default_desired_and_external_python_has_no_runtime(self):
        self.invoke(self.offline_args('--release', str(self.v1), '--python', sys.executable))
        record = ins.read_json(self.prefix / 'installation.json')
        self.assertEqual(record['desired'], {'worker': 'running'})
        self.assertNotIn('runtime', record['config'])

    def test_runtime_same_version_replacement_retains_previous_interpreter(self):
        first, second = self.runtime_source('first'), self.runtime_source('second')
        self.invoke(self.offline_args('--release', str(self.v1), '--runtime', str(first)))
        old = ins.read_json(self.prefix / 'installation.json')['config']
        self.invoke(self.offline_args('--release', str(self.v1), '--runtime', str(second)))
        record = ins.read_json(self.prefix / 'installation.json')
        self.assertEqual(record['previous']['config']['python'], old['python'])
        self.assertTrue(Path(old['python']).exists())
        self.invoke(self.offline_args('--rollback'))
        self.assertEqual(ins.read_json(self.prefix / 'installation.json')['config']['python'], old['python'])

    def test_runtime_dry_run_is_read_only_and_directory_id_tracks_content(self):
        source = self.runtime_source('source')
        first = ins.read_runtime(source)[0]
        inventory = []
        for path in sorted(source.rglob('*')):
            data = path.read_bytes() if path.is_file() else b''
            kind = 'dir' if path.is_dir() else 'file'
            inventory.append([path.relative_to(source).as_posix(), kind, path.lstat().st_mode & 0o777, len(data), hashlib.sha256(data).hexdigest()])
        self.assertEqual(first, hashlib.sha256(json.dumps(inventory, separators=(',', ':')).encode()).hexdigest()[:16])
        copied = self.temp / 'same-content'
        shutil.copytree(source, copied)
        self.assertEqual(ins.read_runtime(copied)[0], first)
        (copied / 'marker').chmod(0o755)  # 内容不变、只改可执行位：ID 也变
        moded = ins.read_runtime(copied)[0]
        self.assertNotEqual(moded, first)
        (copied / 'marker').write_text('new content')
        self.assertNotIn(ins.read_runtime(copied)[0], (first, moded))
        before = self.snapshot(self.temp)
        self.invoke(self.offline_args('--release', str(self.v1), '--runtime', str(source), '--dry-run'))
        self.assertFalse(self.prefix.exists())
        self.assertEqual(self.snapshot(self.temp), before)

    def test_runtime_internal_symlink_hardlink_and_interpreter_priority(self):
        source = self.runtime_source('source', executable='python/bin/python3')
        (source / 'bin').mkdir()
        (source / 'bin/python3').symlink_to('../python/bin/python3')
        archive = self.temp / 'linked.tgz'
        with tarfile.open(str(archive), 'w:gz') as output:
            output.add(source, arcname='.')
            info = tarfile.TarInfo('python-copy')
            info.type, info.linkname = tarfile.LNKTYPE, 'bin/python3'
            output.addfile(info)
        self.invoke(self.offline_args('--release', str(self.v1), '--runtime', str(archive)))
        config = ins.read_json(self.prefix / 'installation.json')['config']
        root = self.prefix / 'runtime' / config['runtime']['id']
        self.assertEqual(config['python'], str(root / 'python/bin/python3'))
        self.assertTrue((root / 'bin/python3').is_symlink())
        self.assertFalse((root / 'python-copy').is_symlink())
        self.assertEqual((root / 'python-copy').read_bytes(), (root / 'python/bin/python3').read_bytes())

    def test_runtime_version_and_required_import_failures_do_not_switch(self):
        self.invoke(self.offline_args('--release', str(self.v1)))
        for name, injected in (('old', 'sys.version_info=(3,8)'), ('missing-ssl', "sys.modules['ssl']=None")):
            source = self.runtime_source(name)
            code = 'import sys; ' + injected + '; exec(sys.argv[1])'
            (source / 'bin/python3').write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -B -c ' + shlex.quote(code) + ' "$3"\n')
            before = (self.prefix / 'installation.json').read_bytes()
            self.invoke(self.offline_args('--release', str(self.v2), '--runtime', str(source)), 1)
            self.assertEqual(ins.current_version(self.prefix), '1.0.0')
            self.assertEqual((self.prefix / 'installation.json').read_bytes(), before)

    def test_runtime_cleanup_warning_does_not_fail_installation(self):
        source = self.runtime_source('source')
        self.invoke(self.offline_args('--release', str(self.v1), '--runtime', str(source)))
        orphan = self.prefix / 'runtime' / ('b' * 16)
        orphan.mkdir()
        remove = shutil.rmtree
        def refuse(path, *args, **kwargs):
            if Path(path) == orphan:
                raise PermissionError('busy')
            return remove(path, *args, **kwargs)
        with patch.object(ins.shutil, 'rmtree', side_effect=refuse):
            out, err = self.invoke(self.offline_args('--release', str(self.v2)))
        self.assertIn('Warning: unused runtime cleanup failed', err)
        self.assertEqual(ins.current_version(self.prefix), '2.0.0')

    def test_runtime_services_reference_managed_interpreter_all_platforms(self):
        source = self.runtime_source('source')
        self.invoke(self.offline_args('--release', str(self.v1), '--runtime', str(source)))
        config = ins.read_json(self.prefix / 'installation.json')['config']
        for system in ('Linux', 'Darwin', 'Windows'):
            rendered = ins.render_services(dict(config, system=system), self.prefix / 'versions/1.0.0', 'S-1-5-21-1-2-3-4')
            data = next(iter(rendered.values()))
            if system == 'Darwin':
                self.assertEqual(plistlib.loads(data)['ProgramArguments'][0], config['python'])
            elif system == 'Windows':
                self.assertEqual(ET.fromstring(data).find('.//{*}Command').text, config['python'])
            else:
                self.assertIn('ExecStart=' + ins.systemd_quote(config['python'], command=True), data.decode())

    def test_runtime_rejects_chained_escape_and_failed_atomic_publish_cleans_stage(self):
        source = self.runtime_source('source')
        (source / 'alias').symlink_to('bin')
        (source / 'bin/outside').symlink_to('../../outside')
        self.invoke(self.offline_args('--release', str(self.v1), '--runtime', str(source)), 1)
        self.assertFalse(self.prefix.exists())
        (source / 'bin/outside').unlink()
        replace = ins.os.replace
        def refuse(src, dst):
            if Path(src).name.startswith('.staging-') and Path(src).parent.name == 'runtime':
                raise OSError('publish failed')
            return replace(src, dst)
        with patch.object(ins.os, 'replace', side_effect=refuse):
            self.invoke(self.offline_args('--release', str(self.v1), '--runtime', str(source)), 1)
        self.assertFalse(list((self.prefix / 'runtime').glob('*')))
        self.assertIsNone(ins.current_version(self.prefix))

    @unittest.skipIf(os.name == 'nt', 'launchd 的命令断言；在 Windows 主机上无法替换模拟平台的 current 符号链接')
    def test_stopped_controller_is_not_started_or_probed_in_all_in_one(self):
        self.installation('Darwin', 'all-in-one', desired={'controller': 'stopped', 'worker': 'running'})
        with self.managers('Darwin'), patch.object(ins, 'backup_controller'), \
                patch.object(ins, 'drain_client', return_value=(object(), 'node')), \
                patch.object(ins, 'drain_and_wait', return_value=False), patch.object(ins, 'set_drain'), \
                patch.object(ins, 'wait_worker_status'), \
                patch.object(ins, 'package_command', return_value=subprocess.CompletedProcess([], 0, '{"ok": true}', '')), \
                patch.object(ins, 'admin_token', side_effect=AssertionError('stopped controller doctor')):
            out, err = self.invoke(['--prefix', str(self.prefix), '--release', str(self.v2)])
            self.assertIn('kept stopped: controller', out)
            self.assertNotIn(['launchctl', 'enable', 'gui/501/org.agent-collab.controller'], self.calls)
            self.assertIn(['launchctl', 'enable', 'gui/501/org.agent-collab.worker'], self.calls)

    def test_runtime_interpreter_locations_and_missing_interpreter(self):
        for index, name in enumerate(ins.RUNTIME_PYTHONS):
            source = self.runtime_source('location-' + str(index), executable=name)
            self.assertEqual(ins.read_runtime(source)[2], name)
        missing = self.temp / 'missing'
        missing.mkdir()
        with self.assertRaisesRegex(ValueError, 'interpreter is missing'):
            ins.read_runtime(missing)

    def test_component_must_belong_to_installation_and_status_exposes_recorded_context(self):
        self.installation(desired={'worker': 'stopped'})
        with self.managers('Darwin'):
            out, err = self.invoke(['service', 'stop', '--prefix', str(self.prefix), '--component', 'controller'], 1)
            self.assertIn('component is not part', err)
            self.assertEqual(self.calls, [])
            out, err = self.invoke(['service', 'status', '--prefix', str(self.prefix)])
            report = json.loads(out)
            self.assertEqual(report['desired_source'], 'recorded')
            self.assertEqual(report['desired'], {'worker': 'stopped'})
            self.assertEqual(report['context']['worker']['manager'], 'launchd')

    def test_discovery_reports_unreadable_controller_data_without_writing(self):
        self.installation('Linux', 'controller', desired={'controller': 'stopped'})
        is_file = Path.is_file
        def inaccessible(path):
            if path.name == 'controller.db':
                raise PermissionError('owned by service account')
            return is_file(path)
        with self.managers('Linux'), patch.object(Path, 'is_file', new=inaccessible):
            out, err = self.invoke(['--discover', '--prefix', str(self.prefix)])
            self.assertFalse(json.loads(out)['controller_data'])
            self.assertEqual(json.loads(out)['context']['controller']['manager'], 'systemd-system')


class ConsoleWindowTests(unittest.TestCase):
    """本机部署向导与本机服务面板经无控制台的助手调用安装器：子进程（schtasks、icacls、whoami、python）不能闪窗口。"""

    def test_installer_subprocesses_do_not_open_console_windows(self):
        with patch('subprocess.run', wraps=subprocess.run) as run:
            ins.run([sys.executable, '-c', 'pass'])
        self.assertEqual(run.call_args[1].get('creationflags'), subprocess.CREATE_NO_WINDOW if os.name == 'nt' else None)


class RemoveTreeTests(unittest.TestCase):
    """卸载删除版本目录：Windows 上刚结束的服务进程还占着目录时稍等重试，不半途失败。"""

    def test_windows_retries_while_the_directory_is_still_in_use(self):
        calls = []

        def busy_twice(path):
            calls.append(path)
            if len(calls) < 3:
                raise PermissionError(32, 'in use')
        with patch.object(ins.shutil, 'rmtree', side_effect=busy_twice), patch.object(ins.os, 'name', 'nt'), \
                patch.object(ins.time, 'sleep'):
            ins.remove_tree('versions/0.0.1')
        self.assertEqual(len(calls), 3)

    def test_other_platforms_and_persistent_failures_still_raise(self):
        with patch.object(ins.shutil, 'rmtree', side_effect=PermissionError(13, 'denied')), patch.object(ins.os, 'name', 'posix'):
            with self.assertRaises(PermissionError):
                ins.remove_tree('versions/0.0.1')
        with patch.object(ins.shutil, 'rmtree', side_effect=PermissionError(32, 'in use')), patch.object(ins.os, 'name', 'nt'), \
                patch.object(ins.time, 'sleep'):
            with self.assertRaises(PermissionError):
                ins.remove_tree('versions/0.0.1', attempts=3)
        with patch.object(ins.shutil, 'rmtree', side_effect=FileNotFoundError()):
            ins.remove_tree('versions/missing')
