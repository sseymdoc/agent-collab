import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from agent_collab.adapters.codex import (MODEL, WindowsJob, OwnedProcess, CodexAdapter,
    build_argv, classify_failure, parse_events, read_observed)

ROOT = Path(__file__).resolve().parents[1]
SESSION = '5cb86daf-bcf1-44c2-9a68-8849447476e1'


class CodexTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.dev/tmp').mkdir(parents=True, exist_ok=True)
        self.temp = Path(tempfile.mkdtemp(dir=str(ROOT / '.dev/tmp')))
        self.addCleanup(shutil.rmtree, str(self.temp))

    def test_argv_new_and_resume_exact_golden_layout(self):
        common = ['codex', 'exec', '--ignore-user-config', '-C', str(self.temp), '--sandbox', 'workspace-write',
            '-c', 'approval_policy="never"', '-c', 'model_reasoning_effort="xhigh"',
            '-c', 'sandbox_workspace_write.writable_roots=[]',
            '-c', 'sandbox_workspace_write.network_access=false',
            '-c', 'sandbox_workspace_write.exclude_tmpdir_env_var=true',
            '-c', 'sandbox_workspace_write.exclude_slash_tmp=true',
            '--model', 'gpt-6-astra', '--skip-git-repo-check', '--json', '-o', str(self.temp / 'result.txt')]
        self.assertEqual(build_argv(['codex'], self.temp, self.temp, {'sandbox': 'workspace-write'}, system='Darwin'), common + ['-'])
        self.assertEqual(build_argv(['codex'], self.temp, self.temp, {'sandbox': 'workspace-write'}, SESSION, system='Darwin'),
                         common + ['resume', SESSION, '-'])

    def test_argv_matches_installed_stable_launcher_function_without_running_it(self):
        path = Path.home() / '.claude/skills/codex-collaborate/scripts/launcher.py'
        if not path.exists():
            self.skipTest('reference launcher unavailable; portable golden argv test still runs')
        source = path.read_bytes()
        self.assertEqual(hashlib.sha256(source).hexdigest(), '7862865fadc8c2a1514744eb33cb2ef93b58d1f6e58823df38ecabd01a774921')
        tree = ast.parse(source)
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'argv')
        namespace = {'MODEL': MODEL}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), 'exec'), namespace)
        for operation in ('start', 'resume'):
            for effort in ('high', 'xhigh', 'max'):
                for sandbox in ('read-only', 'workspace-write'):
                    for structured in (False, True):
                        q = dict(op=operation, cwd=str(self.temp), sandbox=sandbox, effort=effort,
                                 session_id=SESSION, structured_report=structured)
                        expected = namespace['argv']({'codex': ['python', 'wrapper']}, q, self.temp)
                        expected.insert(expected.index('exec') + 1, '--ignore-user-config')
                        actual = build_argv(['python', 'wrapper'], self.temp, self.temp,
                            {'sandbox': sandbox, 'effort': effort}, SESSION if operation == 'resume' else None, structured, system='Darwin')
                        self.assertEqual(actual, expected)

    def test_invalid_model_effort_sandbox_and_session_rejected(self):
        for params in ({'model': 'another'}, {'effort': 'low'}, {'effort': None}, {'sandbox': 'danger-full-access'}):
            with self.assertRaises(ValueError):
                build_argv('codex', self.temp, self.temp, params)
        with self.assertRaises(ValueError):
            build_argv('codex', self.temp, self.temp, {}, 'not-a-uuid')

    def test_failure_classification(self):
        for text, code, abnormal, expected in (
            ('authentication expired', 1, False, 'auth_expired'),
            ('please sign in', 1, False, 'auth_expired'),
            ('usage limit reached', 1, False, 'quota'),
            ('HTTP 429', 1, False, 'quota'),
            ('rate_limit', 1, False, 'quota'),
            ('status 401: invalid credential', 1, False, 'auth_expired'),
            # UUID、路径、行号中的 401/429 子串不是状态码（曾导致测试随机失败）。
            ('session 6a401b29-4013-4429-a401-429000000401 failed', 1, False, 'code_failure'),
            ('File "x.py", line 4012; /tmp/run-429abc/out', 1, False, 'code_failure'),
            ('', -9, False, 'infra_retryable'),
            ('', 3221225477, False, 'infra_retryable'),
            ('', 0, True, 'infra_retryable'),
            ('tests failed', 3, False, 'code_failure')):
            self.assertEqual(classify_failure(text, code, abnormal), expected)

    def test_event_parser_records_actual_thread_usage_and_errors(self):
        events = [dict(type='thread.started', thread_id=SESSION), dict(type='turn.completed', usage={'input_tokens': 7}),
                  dict(type='turn.failed', error={'message': 'failed'})]
        state = parse_events('bad\n[]\n' + '\n'.join(json.dumps(e) for e in events))
        self.assertEqual(state['session_id'], SESSION)
        self.assertTrue(state['turn_completed'])
        self.assertEqual(state['usage'], {'input_tokens': 7})
        self.assertEqual(state['cli_reported_error']['type'], 'turn.failed')
        state = parse_events('{"type":"error","message":"error"}', state)
        self.assertEqual(state['cli_reported_error']['type'], 'error')

    def test_observed_is_unknown_until_read_from_session(self):
        self.assertIsNone(read_observed(self.temp, SESSION))
        sessions = self.temp / 'sessions'
        sessions.mkdir()
        path = sessions / ('rollout-' + SESSION + '.jsonl')
        path.write_text(json.dumps(dict(type='turn_context', payload=dict(model='actual', effort='high'))), encoding='utf-8')
        result = read_observed(self.temp, SESSION)
        self.assertEqual(result['model'], 'actual')
        self.assertEqual(result['effort'], 'high')
        self.assertIsNone(result['sandbox_policy'])
        self.assertEqual(result['source'], str(path))

    def test_owned_process_can_stop_without_ps_permissions(self):
        process = OwnedProcess([sys.executable, '-c', 'import time; time.sleep(30)'], self.temp, self.temp / 'logs')
        self.addCleanup(process.stop)
        self.assertIsNone(process.poll())
        self.assertTrue(process.stop())
        self.assertIsNotNone(process.poll())

    @unittest.skipUnless(os.name == 'nt', 'Windows Job Object needs a Windows host')
    def test_windows_job_terminates_owned_child(self):
        process = OwnedProcess([sys.executable, '-c', 'import time; time.sleep(30)'], self.temp, self.temp / 'logs')
        self.addCleanup(process.stop)
        self.assertIsInstance(process.job, WindowsJob)
        self.assertTrue(process.stop())
        self.assertIsNotNone(process.poll())

    @unittest.skipIf(os.name == 'nt', 'POSIX process-group suspend/resume')
    def test_posix_pause_resume_does_not_end_process(self):
        process = OwnedProcess([sys.executable, '-c', 'import time; time.sleep(30)'], self.temp, self.temp / 'logs')
        self.addCleanup(process.stop)
        self.assertTrue(process.pause())
        self.assertTrue(process.paused)
        self.assertIsNone(process.poll())
        process.resume()
        self.assertFalse(process.paused)
        self.assertTrue(process.stop())

    def test_codex_adapter_uses_injected_process_and_requires_completed_event(self):
        class Context:
            work = self.temp
            directory = self.temp
            contract = {'prompt': 'no real model', 'harness_params': {}}
            def can_execute(self):
                return True
            def start_process(self, factory, *args):
                return factory(*args)
            def process_stopped(self, verified):
                pass
            def finish(self, *args):
                self.result = args
        class FakeProcess:
            def __init__(inner, argv, cwd, directory, stdin, env):
                inner.stdout_path = directory / 'stdout.jsonl'
                inner.stderr_path = directory / 'stderr.log'
                inner.stdout_path.write_text(json.dumps({'type': 'turn.completed', 'usage': {}}), encoding='utf-8')
                self.assertNotIn('resume', argv)
                self.assertEqual(stdin, b'no real model')
            def resume(inner):
                pass
            def poll(inner):
                return 0
            def stop(inner):
                return True
        context = Context()
        adapter = CodexAdapter(context, codex_home=self.temp, process_factory=FakeProcess)
        adapter.tick()
        self.assertEqual(context.result, ('succeeded',))
        self.assertIsNone(context.observed)
