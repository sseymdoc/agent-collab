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
from agent_collab.adapters.commands import resolve_commands, probe_auth
from agent_collab.adapters.codex import build_argv
from agent_collab.node_agent import Agent, AgentLock, bounded_report, detect, doctor, save
from agent_collab.release import VERSION, release_info, version_directory
from fake_controller import identity, no_auth
import test_agent_runtime as runtime

ROOT = Path(__file__).resolve().parents[1]


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

    def test_windows_pathext_and_alias_exclusion(self):
        alias = self.temp / 'WindowsApps'
        tool = self.temp / 'tools'
        self.executable(alias / 'codex.BAT')
        real = self.executable(tool / 'codex.BAT')
        env = dict(self.env, PATH=str(alias) + os.pathsep + str(tool), PATHEXT='.BAT;.EXE', USERPROFILE=self.env['HOME'])
        self.assertEqual(resolve_commands(env=env, system='Windows')['codex']['argv'], [real])
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
            self.assertEqual(probe_auth(name, command, runner), expected)
            self.assertEqual(runner.call_args[1]['timeout'], 20)
            self.assertEqual(runner.call_args[1]['env'], self.env)
            self.assertEqual(runner.call_args[0][0][:2], command['argv'])
        runner = Mock(side_effect=subprocess.TimeoutExpired('fixture', 20))
        self.assertEqual(probe_auth('codex', command, runner), 'unknown')

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
            argv = build_argv(['/fixture/codex'], self.temp, self.temp, system='Windows', windows_sandbox=mode)
            self.assertIn('--ignore-user-config', argv)
            self.assertIn('windows.sandbox="' + mode + '"', argv)
        self.assertFalse(any('windows.sandbox' in x for x in build_argv('codex', self.temp, self.temp, system='Linux')))


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
        task['contract'].update(harness='codex', harness_params={})
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
        self.assertEqual(set(status), {'version', 'git_commit', 'pid', 'version_dir', 'last_heartbeat_ok_at'})
        self.assertEqual(status['version'], 'rc5-fixture')
        self.assertEqual(status['git_commit'], 'commit-fixture')
        self.assertEqual(status['pid'], os.getpid())
        self.assertEqual(status['version_dir'], str(ROOT))
        before = (self.data / 'status.json').read_bytes()
        self.fake.offline = True
        with self.assertRaises(Exception):
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

    def test_disabled_harness_assignment_is_rejected_before_any_process(self):
        self.agent.commands = resolve_commands({'disabled_harnesses': {'codex': 'known failure', 'claude': 'fixture'}})
        task = self.fake.add()
        task['contract'].update(harness='codex', harness_params={})
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
        self.assertEqual(len(self.fake.integrations), 3)
        self.assertEqual(self.fake.integrations[-1], record)

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
