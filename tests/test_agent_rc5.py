"""rc5 wire contracts with local Python children and injected controller replies."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from agent_collab import artifacts, protocol as p
from agent_collab.adapters.commands import resolve_commands, probe_auth, probe_auth_detail
from agent_collab.client import ConnectionFailure
from agent_collab.adapters.codex import build_argv
from agent_collab.node_agent import Agent, AgentLock, bounded_report, detect, doctor, save
from agent_collab.release import VERSION, release_info, version_directory
from fake_controller import identity, no_auth
import test_agent_runtime as runtime

ROOT = Path(__file__).resolve().parents[1]


PARAMS = {'model': 'gpt-6-astra', 'effort': 'xhigh'}  # controller 受理时已解析进契约

class ResolutionTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.dev/tmp').mkdir(parents=True, exist_ok=True)
        self.temp = Path(tempfile.mkdtemp(dir=str(ROOT / '.dev/tmp')))
        self.addCleanup(shutil.rmtree, str(self.temp))
        self.env = dict(HOME=str(self.temp / 'home'), PATH=str(self.temp / 'service'), SECRET_TEST='preserved')

    def executable(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('test fixture')
        path.chmod(0o755)
        return str(path)

    def test_service_path_then_home_fallback_then_macos_directories(self):
        local = self.executable(Path(self.env['HOME']) / '.local/bin/codex')
        brew = self.executable(self.temp / 'brew/claude')
        args = dict(env=self.env, system='Darwin', mac_dirs=[str(self.temp / 'brew'), str(self.temp / 'usr')])
        commands = resolve_commands(**args)
        self.assertEqual(commands['codex']['argv'], [local])
        self.assertEqual(commands['claude']['argv'], [brew])
        service = self.executable(Path(self.env['PATH']) / 'codex')
        self.assertEqual(resolve_commands(**args)['codex']['argv'], [service])
        self.assertIsNone(resolve_commands(env=self.env, system='Linux')['claude']['argv'])

    def test_explicit_string_and_argv_missing_and_disabled(self):
        executable = self.executable(self.temp / 'explicit/tool')
        commands = resolve_commands(dict(codex_command=[executable, 'fixed arg'], claude_command=executable), env=self.env, system='Linux')
        self.assertEqual(commands['codex']['argv'], [executable, 'fixed arg'])
        self.assertEqual(commands['claude']['argv'], [executable])
        self.assertEqual(commands['codex']['env']['PATH'], str(Path(executable).parent) + os.pathsep + self.env['PATH'])
        self.assertEqual(commands['codex']['env']['SECRET_TEST'], 'preserved')
        self.assertIsNone(resolve_commands({'codex_command': str(self.temp / 'missing')}, env=self.env, system='Linux')['codex']['argv'])
        config = dict(codex_command=executable, disabled_harnesses={'codex': 'under repair'})
        commands = resolve_commands(config, self.env, 'Linux')
        with patch('agent_collab.node_agent.shutil.which', return_value=None):
            caps = detect(config, commands, no_auth)
        self.assertIsNone(caps['tools']['codex'])
        self.assertIsNone(caps['harness']['codex'])
        self.assertIsNone(caps['harness_paths']['codex'])
        self.assertEqual(caps['harness_disabled'], {'codex': 'under repair'})
        for command in ([], ['relative'], 1):
            with self.assertRaises(ValueError):
                resolve_commands({'codex_command': command}, self.env, 'Linux')
        # 原因值会上报给 controller：非字符串必须在本地就被拒绝；codex_home 必须是路径字符串。
        for bad in ({'disabled_harnesses': {'codex': 1}}, {'disabled_harnesses': ['codex']},
                    {'codex_home': 5}, {'codex_home': ''}):
            with self.assertRaises(ValueError):
                resolve_commands(bad, self.env, 'Linux')
        self.assertIsNone(resolve_commands({'disabled_harnesses': {'codex': None}}, self.env, 'Linux')['codex']['argv'])

    def test_windows_pathext_and_alias_exclusion(self):
        alias = self.temp / 'WindowsApps'
        tool = self.temp / 'tools'
        self.executable(alias / 'codex.BAT')
        real = self.executable(tool / 'codex.BAT')
        env = dict(self.env, PATH=str(alias) + os.pathsep + str(tool), PATHEXT='.BAT;.EXE', USERPROFILE=self.env['HOME'])
        self.assertEqual(resolve_commands(env=env, system='Windows')['codex']['argv'], [real])
        # 解释器类扩展名与无扩展名脚本不能被 CreateProcess 直接启动：即使在 PATH 更前面也跳过。
        early = self.temp / 'early'
        self.executable(early / 'codex')
        self.executable(early / 'codex.VBS')
        env = dict(env, PATH=str(early) + os.pathsep + str(tool), PATHEXT='.VBS;.BAT')
        self.assertEqual(resolve_commands(env=env, system='Windows')['codex']['argv'], [real])
        self.assertIsNone(resolve_commands({'codex_command': str(early / 'codex.VBS')}, env, 'Windows')['codex']['argv'])
        self.assertIsNone(resolve_commands({'codex_command': str(alias / 'codex.BAT')}, env, 'Windows')['codex']['argv'])

    def test_auth_status_mapping_never_returns_account_output(self):
        command = dict(argv=['/fixture', 'wrapper'], env=self.env)
        for name, code, stdout, stderr, expected in (
            ('codex', 0, '', 'Logged in using ChatGPT secret@example.test', 'ok'),
            ('codex', 1, '', 'Not logged in', 'expired'),
            ('codex', 1, 'Logged in', 'command failed', 'unknown'),
            ('codex', 0, 'unrecognized output', '', 'unknown'),
            ('claude', 0, '{"loggedIn":true,"email":"private"}', '', 'ok'),
            ('claude', 1, '{"loggedIn":false}', '', 'expired'),
            ('claude', 1, '{"loggedIn":true}', '', 'unknown'),
            ('claude', 0, '[]', '', 'unknown'),
            ('claude', 0, 'bad JSON', '', 'unknown')):
            runner = Mock(return_value=subprocess.CompletedProcess([], code, stdout, stderr))
            self.assertEqual(probe_auth(name, command, runner, 'Linux'), expected)
            self.assertEqual(runner.call_args[1]['timeout'], 20)
            self.assertEqual(runner.call_args[1]['env'], self.env)
            self.assertEqual(runner.call_args[0][0][:2], command['argv'])
        runner = Mock(side_effect=subprocess.TimeoutExpired('fixture', 20))
        self.assertEqual(probe_auth('codex', command, runner, 'Linux'), 'unknown')

    def test_unreadable_credential_store_is_not_reported_as_expired(self):
        # Mac 的 SSH 会话实测：claude auth status 报 loggedIn=false、authMethod=none，security show-keychain-info exit 36。
        command = dict(argv=['/fixture'], env=self.env)
        def runner(keychain_code, logged_in='false'):
            def run(argv, **kwargs):
                self.assertEqual((kwargs['timeout'], kwargs['env']), (20, self.env))
                if argv[0] == '/usr/bin/security':
                    if keychain_code is None:
                        raise subprocess.TimeoutExpired(argv, 20)
                    return subprocess.CompletedProcess(argv, keychain_code, '', 'User interaction is not allowed.')
                return subprocess.CompletedProcess(argv, 1, '{"loggedIn":%s,"authMethod":"none"}' % logged_in, '')
            return Mock(side_effect=run)
        for system, keychain_code, expected in (
                ('Darwin', 36, ('unknown', 'credential_store_unreadable')),    # 当前上下文读不到认证材料
                ('Darwin', None, ('unknown', 'credential_store_unreadable')),  # 钥匙串检查无响应
                ('Darwin', 0, ('expired', 'not_logged_in')),                   # 钥匙串可读而 CLI 明确未登录
                ('Windows', 36, ('expired', 'not_logged_in')),                 # 凭据是文件的平台不做钥匙串判断
                ('Linux', 36, ('expired', 'not_logged_in'))):
            run = runner(keychain_code)
            self.assertEqual(probe_auth_detail('claude', command, run, system), expected)
            self.assertEqual(probe_auth('claude', command, run, system), expected[0])
            self.assertEqual(any(call[0][0][0] == '/usr/bin/security' for call in run.call_args_list), system == 'Darwin')
        # 钥匙串检查可能刚解锁了被锁定的钥匙串：以重探结果为准。
        answers = iter(['false', 'true'])
        def unlocked(argv, **kwargs):
            if argv[0] == '/usr/bin/security':
                return subprocess.CompletedProcess(argv, 0, '', '')
            return subprocess.CompletedProcess(argv, 0, '{"loggedIn":%s}' % next(answers), '')
        self.assertEqual(probe_auth_detail('claude', command, Mock(side_effect=unlocked), 'Darwin'), ('ok', 'logged_in'))
        # 已登录或 codex 时不碰钥匙串。
        run = runner(36, 'true')
        self.assertEqual(probe_auth_detail('claude', command, run, 'Darwin'), ('unknown', 'probe_inconclusive'))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(probe_auth_detail('codex', dict(argv=None, env=self.env), run, 'Darwin'), ('unknown', 'command_missing'))

    def test_release_identity_reads_only_requested_code_root_and_falls_back(self):
        self.assertEqual(release_info(self.temp), {'version': VERSION, 'git_commit': None})
        manifest = self.temp / 'release-manifest.json'
        manifest.write_text('{"version":"0.1.0rc5","git_commit":"abc"}')
        self.assertEqual(release_info(self.temp), {'version': '0.1.0rc5', 'git_commit': 'abc'})
        manifest.write_text('{broken')
        self.assertEqual(release_info(self.temp)['version'], VERSION)
        self.assertEqual(version_directory(), ROOT)

    def test_codex_windows_isolation_parameters_are_injectable(self):
        for mode in ('elevated', 'unelevated'):
            argv = build_argv(['/fixture/codex'], self.temp, self.temp, PARAMS, system='Windows', windows_sandbox=mode)
            self.assertIn('--ignore-user-config', argv)
            self.assertIn('windows.sandbox="' + mode + '"', argv)
        self.assertFalse(any('windows.sandbox' in x for x in build_argv('codex', self.temp, self.temp, PARAMS, system='Linux')))


class ClientAndAdapterTests(unittest.TestCase):
    def test_transport_keeps_query_and_tolerates_malformed_error_body(self):
        from agent_collab.client import APIError, Client
        seen = []
        class Connection:
            def __init__(self, *args, **kwargs):
                pass
            def request(self, method, target, body=None, headers=None):
                seen.append(target)
            def getresponse(self):
                return Mock(status=200, getheaders=lambda: [], read=lambda: b'{}')
            def close(self):
                pass
        with patch('agent_collab.client.http.client.HTTPConnection', Connection):
            Client('http://127.0.0.1:1')._transport('GET', 'http://127.0.0.1:1/api/v1/events?after=5', {}, b'')
        self.assertEqual(seen, ['/api/v1/events?after=5'])
        for body in ({'error': 'text'}, {'error': {'code': 3, 'details': ['x']}}, ['list'], None):
            error = APIError(409, body)
            self.assertEqual((error.code, error.details), ('invalid', {}))

    def test_stub_run_with_unverified_stop_quarantines(self):
        from agent_collab.adapters.stub import StubAdapter
        temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(temp))
        context = Mock(work=temp, directory=temp, summary={}, contract={'harness_params': {'steps': [{'run': ['x']}]}})
        context.can_execute.return_value = True
        process = Mock(stdout_path=temp / 'out', stderr_path=temp / 'err')
        process.poll.return_value = 0
        process.stop.return_value = False
        context.start_process.return_value = process
        adapter = StubAdapter(context)
        adapter.tick()
        context.process_stopped.assert_called_with(False)
        context.quarantine.assert_called_once()
        context.finish.assert_not_called()


    def test_check_spawn_failure_with_unverified_stop_stays_unverified(self):
        from agent_collab.adapters.checks import ContractChecks
        process = Mock()
        process.stop.return_value = False
        context = Mock(work=Path('.'), directory=Path('.'), contract={'checks': [{'name': 'c', 'run': ['x']}]})
        context.can_execute.return_value = True
        context.clock.return_value = 0
        verdicts = []
        context.process_stopped.side_effect = verdicts.append
        runner = ContractChecks(context)
        def start(*args):
            runner.process = process  # 与 Attempt.start_process 一致：先登记句柄，再落盘失败
            raise OSError('persist failed')
        context.start_process.side_effect = start
        context.quarantine.side_effect = lambda reason: runner.stop()  # 真实 quarantine 会再停一次
        runner.tick()
        context.quarantine.assert_called_once()
        self.assertEqual(verdicts, [False, False])  # 第二次停止也如实记为未确认，不会被覆盖成 True


class RuntimeRC5Tests(unittest.TestCase):
    setUp = runtime.RuntimeTests.setUp
    cycles = runtime.RuntimeTests.cycles
    until = runtime.RuntimeTests.until
    attempt = runtime.RuntimeTests.attempt

    def run_to_completion(self, assignment):
        for _ in range(500):
            self.cycles()
            if assignment['attempt_id'] in self.fake.completions:
                return self.fake.completions[assignment['attempt_id']]
            time.sleep(.005)
        self.fail('check process did not complete')

    def test_detect_auth_and_real_adapter_use_same_resolved_argv_and_environment(self):
        script = self.temp / 'harness.py'
        script.write_text('import json, os, pathlib, sys\n'
            'if "--version" in sys.argv: print("fixture 1"); sys.exit()\n'
            'if "login" in sys.argv: print("Logged in"); sys.exit()\n'
            'pathlib.Path("observed.json").write_text(json.dumps({"argv":sys.argv,"path":os.environ["PATH"]}))\n'
            'print(json.dumps({"type":"turn.completed"}))\n', encoding='utf-8')
        config = dict(self.agent.config, codex_command=[sys.executable, str(script)],
                      disabled_harnesses={'claude': 'fixture'}, codex_home=str(self.temp / 'auth'))
        save(self.data / 'agent.json', config)
        commands = resolve_commands(config)
        with patch('agent_collab.node_agent.shutil.which', return_value=None):
            caps = detect(config, commands)
        self.assertEqual(caps['harness']['codex'], 'fixture 1')
        self.assertEqual(caps['tools']['codex'], caps['harness']['codex'])
        self.assertEqual(caps['harness_paths']['codex'], commands['codex']['argv'][0])
        self.agent.close()
        self.agent = Agent(self.data, transport=self.fake, clock=self.clock, capabilities=caps, commands=commands)
        self.addCleanup(self.agent.close)
        self.assertEqual(self.agent.auth_health['codex'], 'ok')
        task = self.fake.add()
        task['contract'].update(harness='codex', harness_params=dict(PARAMS))
        self.run_to_completion(task)
        observed = json.loads((self.attempt(task).work / 'observed.json').read_text())
        self.assertEqual(observed['argv'][0], str(script))
        self.assertEqual(observed['path'], commands['codex']['env']['PATH'])
        self.assertIn('--ignore-user-config', observed['argv'])
        with patch('agent_collab.node_agent.resolve_commands', side_effect=AssertionError('resolved twice')):
            self.cycles(5)

    def test_auth_startup_periodic_cache_and_status_identity(self):
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, 'Logged in' if 'login' in argv else '{"loggedIn":true}', '')
        command = dict(argv=[sys.executable, 'fixture'], env={})
        commands = {'codex': command, 'claude': copy.deepcopy(command)}
        with patch('agent_collab.node_agent.release_info', return_value=dict(version='rc5-fixture', git_commit='commit-fixture')):
            agent = Agent(self.data, transport=self.fake, clock=self.clock, capabilities=runtime.CAPS,
                          commands=commands, auth_runner=runner)
        self.addCleanup(agent.close)
        self.assertEqual(len(calls), 2)
        agent.register()
        for _ in range(5):
            agent.heartbeat()
            agent.refresh_auth()
        self.assertEqual(len(calls), 2)
        self.clock.advance(901)
        agent.refresh_auth()
        self.assertEqual(len(calls), 4)
        self.assertEqual(self.fake.last_heartbeat['auth_health'], {'codex': 'ok', 'claude': 'ok'})
        status = json.loads((self.data / 'status.json').read_text())
        self.assertEqual(set(status), {'version', 'git_commit', 'pid', 'version_dir', 'last_heartbeat_ok_at',
                                       'auth_health', 'auth_checked_at'})
        self.assertEqual(status['auth_health'], {'codex': 'ok', 'claude': 'ok'})
        # 状态文件写于最后一次心跳，早于其后的那次探测。
        self.assertLessEqual(status['auth_checked_at'], agent.auth_checked_at)
        self.assertTrue(status['auth_checked_at'].endswith('Z'))
        self.assertEqual(status['version'], 'rc5-fixture')
        self.assertEqual(status['git_commit'], 'commit-fixture')
        self.assertEqual(status['pid'], os.getpid())
        self.assertEqual(status['version_dir'], str(ROOT))
        before = (self.data / 'status.json').read_bytes()
        self.fake.offline = True
        with self.assertRaises(ConnectionFailure):
            agent.heartbeat()
        self.assertEqual((self.data / 'status.json').read_bytes(), before)

    def test_external_workspaces_snapshot_restore_isolation_and_restart(self):
        root = self.temp / 'workspaces'
        self.agent.config['workspace_root'] = str(root)
        first = self.fake.add([{'write': {'path': 'keep', 'content': 'one'}}])
        self.run_to_completion(first)
        attempt = self.attempt(first)
        self.assertEqual(attempt.work, root / attempt.id)
        self.assertFalse((attempt.directory / 'work').exists())
        final = attempt.meta['checkpoints'][-1]
        second = self.fake.add([], manifest=final['manifest'])
        self.run_to_completion(second)
        restored = self.attempt(second)
        self.assertNotEqual(restored.work, attempt.work)
        self.assertEqual((restored.work / 'keep').read_text(), 'one')
        restored.quarantine('test retention')
        (restored.work / 'keep').write_text('two')
        self.assertEqual((attempt.work / 'keep').read_text(), 'one')
        replacement = Agent(self.data, transport=self.fake, clock=self.clock, capabilities=runtime.CAPS, auth_runner=no_auth)
        self.addCleanup(replacement.close)
        self.assertEqual(replacement.attempts[attempt.id].work, attempt.work)
        self.assertTrue((restored.work / 'keep').exists())

    def test_checks_bind_final_manifest_keep_success_and_redact_bounded_tails(self):
        task = self.fake.add([{'write': {'path': 'result', 'content': 'model'}}])
        secret = task['lease_token']
        task['contract']['checks'] = [dict(name='pass', run=['{python}', '-c',
            'import pathlib; pathlib.Path("checked").write_text("yes"); print("ok")']),
            dict(name='fail', run=['{python}', '-c', 'import sys; print("x"*6000); print(' + repr(secret) +
                 '); print("test-node-secret", file=sys.stderr); sys.exit(7)'])]
        complete = self.run_to_completion(task)
        self.assertEqual(complete['outcome'], 'succeeded')
        checks = complete['report']['checks']
        self.assertEqual(checks['node_id'], 'node-a')
        self.assertEqual(set(checks['platform']), {'os', 'release', 'machine', 'python'})
        self.assertEqual([r['exit_code'] for r in checks['results']], [0, 7])
        self.assertTrue(all(not r['timed_out'] for r in checks['results']))
        final = self.fake.checkpoints[complete['output_checkpoint_id']]
        self.assertEqual(checks['manifest_sha256'], p.manifest_sha256(final['manifest']))
        self.assertIn('checked', [f['path'] for f in final['manifest']['files']])
        self.assertEqual(checks['results'][0]['argv'][0], sys.executable)
        for r in checks['results']:
            self.assertGreaterEqual(r['duration_s'], 0)
            self.assertLessEqual(len(r['stdout_tail'].encode()), 4096)
            self.assertLessEqual(len(r['stderr_tail'].encode()), 4096)
        self.assertNotIn(secret, json.dumps(complete['report']))
        self.assertNotIn('test-node-secret', json.dumps(complete['report']))
        self.assertIsNone(complete['runtime']['agent_commit'])

    def test_failed_harness_never_runs_checks(self):
        task = self.fake.add([{'fail': 'code_failure'}])
        task['contract']['checks'] = [dict(name='never', run=['{python}', '-c', 'raise Exception()'])]
        complete = self.run_to_completion(task)
        self.assertIsNone(complete['report']['checks'])
        self.assertFalse((self.attempt(task).directory / 'checks').exists())

    def test_timeout_and_spawn_failure_are_evidence_not_harness_failure(self):
        task = self.fake.add()
        task['contract']['checks'] = [dict(name='timeout', run=['{python}', '-c', 'import time; time.sleep(60)'], timeout_s=1),
                                     dict(name='missing', run=[str(self.temp / 'missing')])]
        complete = self.run_to_completion(task)
        self.assertEqual(complete['outcome'], 'succeeded')
        results = complete['report']['checks']['results']
        self.assertTrue(results[0]['timed_out'])
        self.assertIsNone(results[0]['exit_code'])
        self.assertIsNone(results[1]['exit_code'])
        self.assertFalse(results[1]['timed_out'])

    def test_checks_do_not_block_heartbeat_and_cancel_stops_their_process(self):
        task = self.fake.add()
        task['contract']['checks'] = [dict(name='long', run=['{python}', '-c', 'import time; time.sleep(60)'])]
        self.until(lambda: self.attempt(task).check_runner is not None and self.attempt(task).check_runner.process is not None)
        process = self.attempt(task).check_runner.process
        before = sum(path.endswith('/heartbeat') for _, path in self.fake.calls)
        self.cycles(5)
        self.assertGreater(sum(path.endswith('/heartbeat') for _, path in self.fake.calls), before)
        self.fake.cancel.add(task['attempt_id'])
        self.until(lambda: task['attempt_id'] in self.fake.completions)
        self.assertEqual(self.fake.completions[task['attempt_id']]['outcome'], 'cancelled')
        self.assertIsNone(self.fake.completions[task['attempt_id']]['report']['checks'])
        self.assertIsNotNone(process.poll())

    def test_unverified_check_stop_survives_restart_check_and_blocks_claims(self):
        task = self.fake.add()
        task['contract']['checks'] = [dict(name='long', run=['{python}', '-c', 'import time; time.sleep(60)'])]
        self.until(lambda: self.attempt(task).check_runner is not None and self.attempt(task).check_runner.process is not None)
        self.assertTrue(self.agent.connected and self.agent.reconciled)
        attempt = self.attempt(task)
        process = attempt.check_runner.process
        self.addCleanup(process.stop)  # 测试结束后真正清理检查进程
        with patch.object(process, 'stop', return_value=False):
            # 不 mock quarantine：取消先停已结束的 harness（报告已停止），再停检查进程（未确认）
            attempt.cancel()
            self.assertEqual(attempt.meta['state'], 'quarantined')
            attempt.refresh_restart()
        self.assertIsNone(process.poll())  # 检查进程确实还在运行
        self.assertEqual(attempt.meta['state'], 'quarantined')  # 不能被判为 gone 而 abandoned
        self.assertTrue(attempt.meta['restart_unknown'])
        waiting = self.fake.add()
        self.agent.claim()
        self.assertIn(waiting, self.fake.assignments)  # 领取仍被阻塞

    def test_disabled_harness_assignment_is_rejected_before_any_process(self):
        self.agent.commands = resolve_commands({'disabled_harnesses': {'codex': 'known failure', 'claude': 'fixture'}})
        task = self.fake.add()
        task['contract'].update(harness='codex', harness_params=dict(PARAMS))
        complete = self.run_to_completion(task)
        self.assertEqual(complete['failure_class'], 'capability_missing')
        self.assertIsNone(self.attempt(task).adapter)

    def test_large_check_evidence_keeps_identity_and_results_under_wire_limit(self):
        report = dict(harness='stub', checks=dict(manifest_sha256='a'*64, node_id='node-a',
            results=[dict(name=str(i), argv=['python'], exit_code=0, timed_out=False, duration_s=1,
                          stdout_tail='\x00'*4096, stderr_tail='文'*4096) for i in range(16)]))
        bounded_report(report)
        self.assertLessEqual(len(p.canonical_json(report)), 65536)
        self.assertEqual(len(report['checks']['results']), 16)
        self.assertEqual(report['checks']['manifest_sha256'], 'a'*64)

    def integration_item(self, baseline=b'old', result=b'new'):
        target = self.temp / ('target-' + identity())
        target.mkdir()
        (target / 'changed').write_bytes(baseline)
        base, base_blobs = artifacts.snapshot(target)
        work = self.temp / ('result-' + identity())
        work.mkdir()
        (work / 'changed').write_bytes(result)
        final, final_blobs = artifacts.snapshot(work, base)
        self.fake.blobs.update(base_blobs)
        self.fake.blobs.update(final_blobs)
        return dict(job_id=identity(), checkpoint_id=identity(), target_path=str(target),
                    base_manifest_sha256=p.manifest_sha256(base), manifest_sha256=p.manifest_sha256(final), exclude=[])

    def heartbeat_item(self, item):
        self.fake.integration_items[item['job_id']] = dict(item, target_node_id='node-a')
        self.agent.register()
        self.agent.heartbeat()

    def test_heartbeat_integrates_on_target_without_any_execution_attempt(self):
        item = self.integration_item()
        target = Path(item['target_path'])
        (target / 'unrelated').write_text('human addition')
        self.heartbeat_item(item)
        self.assertFalse(self.agent.attempts)
        self.assertEqual((target / 'changed').read_bytes(), b'new')
        self.assertEqual((target / 'unrelated').read_text(), 'human addition')
        record = self.fake.integrations[-1]
        self.assertEqual(record['status'], 'integrated')
        self.assertEqual((Path(record['detail']['backup']) / 'changed').read_bytes(), b'old')
        (target / 'changed').write_bytes(b'human later')
        self.agent.integrations([dict(item, target_node_id='node-a')])
        self.assertEqual((target / 'changed').read_bytes(), b'human later')
        # 待写确认两次（取 blob 前后各一次）+ integrated + 重放同一回执
        self.assertEqual([r['status'] for r in self.fake.integrations],
                         ['integration_pending', 'integration_pending', 'integrated', 'integrated'])
        self.assertEqual(self.fake.integrations[-1], record)

    def test_error_with_unverified_stop_quarantines_instead_of_failing(self):
        task = self.fake.add([{'sleep': 100}])
        self.until(lambda: task['attempt_id'] in self.agent.attempts and self.attempt(task).adapter is not None
                   and self.attempt(task).meta['state'] == 'running')
        attempt = self.attempt(task)
        with patch.object(attempt.adapter, 'tick', side_effect=OSError('read failed')), \
                patch.object(attempt.adapter, 'stop', return_value=False):
            attempt.tick()
        self.assertEqual(attempt.meta['state'], 'quarantined')
        self.assertTrue(attempt.meta['restart_unknown'])
        self.assertIsNone(attempt.meta.get('completion'))

    def test_malformed_events_reply_disconnects_without_failing_attempt(self):
        task = self.fake.add([{'progress': {'n': 1}}])
        bad = {'on': True, 'hits': 0}
        def transport(method, url, headers, body):
            if bad['on'] and url.endswith('/events'):
                bad['hits'] += 1
                return 200, {}, b'<html>proxy error</html>'
            return self.fake(method, url, headers, body)
        self.agent.client.transport = transport
        self.cycles(30)
        self.assertGreater(bad['hits'], 0)
        self.assertNotIn(task['attempt_id'], self.fake.completions)
        bad['on'] = False
        complete = self.run_to_completion(task)
        self.assertEqual(complete['outcome'], 'succeeded')  # 不是 code_failure

    def test_events_are_sent_in_bounded_batches(self):
        task = self.fake.add([{'sleep': 100}])
        self.until(lambda: task['attempt_id'] in self.agent.attempts and self.attempt(task).meta['state'] == 'running')
        attempt = self.attempt(task)
        self.until(lambda: not attempt.events)
        for i in range(12):  # 一次积压多个事件（约 3.6 KB），按 1 KiB 上限必须拆成多批
            attempt.event('attempt.progress', {'n': i, 'pad': 'x' * 200})
        before = sum(path.endswith('/events') for _, path in self.fake.calls)
        with patch('agent_collab.node_agent.EVENT_BATCH_BYTES', 1024):
            attempt.flush_events()
        batches = sum(path.endswith('/events') for _, path in self.fake.calls) - before
        self.assertGreater(batches, 1)
        self.assertLess(batches, 12)
        progress = [e for e in self.fake.events.values() if e['event']['type'] == 'attempt.progress']
        self.assertEqual(len(progress), 12)
        self.assertFalse(attempt.events)

    def test_event_batch_halves_on_controller_size_limit(self):
        # controller 的 max_json_bytes 比本地 512 KiB 分批上限还小：收到 413 后减半重试直到送达。
        task = self.fake.add([{'sleep': 100}])
        self.until(lambda: task['attempt_id'] in self.agent.attempts and self.attempt(task).meta['state'] == 'running')
        attempt = self.attempt(task)
        self.until(lambda: not attempt.events)
        for i in range(12):  # 一次积压多个事件，整批必然超过上限
            attempt.event('attempt.progress', {'n': i, 'pad': 'x' * 200})
        self.fake.max_json_bytes = 3000
        self.until(lambda: not attempt.events)
        self.assertGreater(self.fake.too_large_hits, 0)
        progress = [e for e in self.fake.events.values() if e['event']['type'] == 'attempt.progress']
        self.assertEqual(len(progress), 12)

    def test_unverified_stop_keeps_process_record_open_for_restart_check(self):
        task = self.fake.add([{'sleep': 100}])
        self.until(lambda: task['attempt_id'] in self.agent.attempts and self.attempt(task).meta['state'] == 'running')
        attempt = self.attempt(task)
        attempt.meta['process'] = dict(state='running', pid=12345, stamp='x')
        attempt.process_stopped(True)   # 已结束的 harness 先报告“已停止”（它的旧句柄已关闭）
        attempt.process_stopped(False)  # 契约检查进程停止未确认
        attempt.process_stopped(True)
        self.assertNotEqual(attempt.meta['process']['state'], 'stopped')
        self.assertTrue(attempt.meta['process']['unverified'])

    def test_malformed_controller_reply_disconnects_instead_of_crashing(self):
        self.cycles()
        with patch.object(self.agent, 'heartbeat', side_effect=KeyError('drain')):
            self.agent.next_network = self.agent.next_heartbeat = 0  # 本周期必然发心跳
            self.agent.cycle()
        self.assertFalse(self.agent.connected)
        self.assertEqual(self.agent.last_error['error']['details']['reason'], 'protocol')

    def test_bad_integration_item_does_not_block_other_items(self):
        good = self.integration_item()
        bad = dict(self.integration_item(), checkpoint_id='not-a-uuid')
        self.agent.integrations(['not-an-object', bad, good])
        self.assertEqual((Path(good['target_path']) / 'changed').read_bytes(), b'new')
        self.assertEqual((Path(bad['target_path']) / 'changed').read_bytes(), b'old')

    def test_superseded_integration_is_not_written_and_lease_is_retried(self):
        item = self.integration_item()
        changed = Path(item['target_path']) / 'changed'
        self.fake.integration_error = dict(reason='coordinator_lease')
        self.heartbeat_item(item)
        self.assertEqual(changed.read_bytes(), b'old')
        progress = json.loads(next((self.data / 'integrations').glob('*/*/progress.json')).read_text(encoding='utf-8'))
        self.assertFalse(progress['reported'])
        # 交付已被替代：409 不再让心跳失败，也绝不写目标目录。
        self.fake.integration_error = dict(reason='superseded')
        self.agent.heartbeat()
        self.assertEqual(changed.read_bytes(), b'old')
        progress = json.loads(next((self.data / 'integrations').glob('*/*/progress.json')).read_text(encoding='utf-8'))
        self.assertTrue(progress['reported'] and progress['superseded'])
        self.assertFalse(self.fake.integrations)

    def test_delivery_superseded_while_fetching_blobs_is_not_written(self):
        item = self.integration_item()
        fetch = self.agent.fetch_blob
        def fetch_then_supersede(sha):
            self.fake.integration_error = dict(reason='superseded')  # 取 blob 期间有新发布
            return fetch(sha)
        with patch.object(self.agent, 'fetch_blob', side_effect=fetch_then_supersede):
            self.agent.integrations([item])
        self.assertEqual((Path(item['target_path']) / 'changed').read_bytes(), b'old')
        self.assertEqual([r['status'] for r in self.fake.integrations], ['integration_pending'])

    def test_check_named_dot_dot_stays_inside_attempt_directory(self):
        task = self.fake.add()
        task['contract']['checks'] = [dict(name='..', run=['{python}', '-c', 'print("ok")'])]
        complete = self.run_to_completion(task)
        self.assertEqual(complete['report']['checks']['results'][0]['exit_code'], 0)
        self.assertTrue((self.attempt(task).directory / 'checks' / '0').is_dir())

    def test_conflict_does_not_overwrite_and_non_target_ignores(self):
        item = self.integration_item()
        (Path(item['target_path']) / 'changed').write_bytes(b'human')
        self.agent.integrations([dict(item, target_node_id='somewhere-else')])
        self.assertFalse(self.fake.integrations)
        self.assertFalse((self.data / 'integrations').exists())
        self.heartbeat_item(item)
        self.assertEqual(self.fake.integrations[-1]['status'], 'conflict')
        self.assertEqual((Path(item['target_path']) / 'changed').read_bytes(), b'human')

    def test_unknown_started_integration_is_reported_conflict_without_retry(self):
        item = self.integration_item()
        with patch('agent_collab.node_agent.integrate', side_effect=KeyboardInterrupt('crash')):
            with self.assertRaises(KeyboardInterrupt):
                self.agent.integrations([item])
        replacement = Agent(self.data, transport=self.fake, clock=self.clock, capabilities=runtime.CAPS, auth_runner=no_auth)
        self.addCleanup(replacement.close)
        with patch('agent_collab.node_agent.integrate', side_effect=AssertionError('second write')):
            replacement.integrations([item])
        self.assertEqual(self.fake.integrations[-1]['status'], 'conflict')
        self.assertEqual((Path(item['target_path']) / 'changed').read_bytes(), b'old')

    def test_rc4_completed_integration_receipt_is_imported_without_a_second_write(self):
        task = self.fake.add([{'write': {'path': 'result', 'content': 'new'}}])
        complete = self.run_to_completion(task)
        attempt = self.attempt(task)
        final = attempt.meta['checkpoints'][-1]
        target = self.temp / 'legacy-target'
        target.mkdir()
        (target / 'result').write_text('human edit after rc4')
        record = dict(request_id=identity(), checkpoint_id=complete['output_checkpoint_id'], status='integrated',
                      detail=dict(changed=['result'], conflicts=[], backup=str(attempt.directory / 'integration-backup')))
        attempt.meta.update(integration=record, integration_started=True)
        attempt.persist()
        item = dict(job_id=task['job_id'], checkpoint_id=record['checkpoint_id'],
                    base_manifest_sha256=None, manifest_sha256=p.manifest_sha256(final['manifest']),
                    target_path=str(target), exclude=[])
        with patch('agent_collab.node_agent.integrate', side_effect=AssertionError('duplicate write')):
            self.agent.integrations([item])
        self.assertEqual(self.fake.integrations[-1], record)
        self.assertEqual((target / 'result').read_text(), 'human edit after rc4')

    def test_os_lock_rejects_second_process_immediately_and_releases(self):
        code = ('import sys; from agent_collab.node_agent import AgentLock; '
                'lock=AgentLock(sys.argv[1]); lock.__enter__(); lock.__exit__()')
        with AgentLock(self.data):
            result = subprocess.run([sys.executable, '-B', '-c', code, str(self.data)],
                                    capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('another agent owns', result.stderr)
        result = subprocess.run([sys.executable, '-B', '-c', code, str(self.data)], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)

    def test_doctor_warns_for_windows_profile_workspace(self):
        config = dict(self.agent.config, workspace_root=str(self.temp / 'profile/work'))
        save(self.data / 'agent.json', config)
        commands = resolve_commands({'codex_command': sys.executable, 'disabled_harnesses': {'claude': 'test'}})
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, 'fixture 1', ''))
        with patch.dict(os.environ, USERPROFILE=str(self.temp / 'profile')):
            result = doctor(self.data, self.fake, commands=commands, runner=runner, system='Windows')
        self.assertIn('workspace_in_profile', result['warnings'])
        self.assertTrue(result['ok'])
