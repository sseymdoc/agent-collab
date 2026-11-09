"""Claude CLI boundary tested with fake output and a local Python child only."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from agent_collab import domain as d, protocol as p
from agent_collab.adapters.claude import ClaudeAdapter, build_argv
from agent_collab.adapters.codex import OwnedProcess
from agent_collab.node_agent import Agent, detect, pair
from fake_controller import no_auth
from agent_collab.adapters.commands import resolve_commands
from helpers import ControllerCase, contract, uid, rc4_agent_body
from test_coordinator import CAPS, CONFIG, PROFILE, decision, repair_request

ROOT = Path(__file__).resolve().parents[1]


class ClaudeTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.dev/tmp').mkdir(parents=True, exist_ok=True)
        self.temp = Path(tempfile.mkdtemp(dir=str(ROOT / '.dev/tmp')))
        self.addCleanup(shutil.rmtree, str(self.temp))
        (self.temp / 'decision-schema.json').write_bytes(p.canonical_json(d.DECISION_SCHEMA))
        (self.temp / 'context.json').write_bytes(p.canonical_json({'job': {}}))  # controller 提供的输入文件

    def test_exact_argv_has_configured_profile_read_only_and_inline_schema(self):
        session = uid()
        params = {k: PROFILE[k] for k in ('model', 'effort')}
        argv = build_argv('/custom/claude', params, d.DECISION_SCHEMA, session)
        self.assertEqual(argv, ['/custom/claude', '-p', '--model', PROFILE['model'], '--effort', PROFILE['effort'],
            '--output-format', 'json', '--json-schema', p.canonical_json(d.DECISION_SCHEMA).decode(),
            '--permission-mode', 'dontAsk', '--tools', '', '--session-id', session])
        for value in ({}, {'model': 'x'}, {'model': 'x', 'effort': ''}):
            with self.assertRaises(ValueError):
                build_argv('claude', value, d.DECISION_SCHEMA, session)

    def run_adapter(self, stdout, stderr='', code=0):
        case = self
        class Context:
            work = case.temp
            directory = case.temp
            contract = {'harness_params': {k: PROFILE[k] for k in ('model', 'effort')}}
            def can_execute(self):
                return True
            def start_process(self, factory, *args):
                return factory(*args)
            def process_stopped(self, verified):
                case.assertTrue(verified)
            def finish(self, *args):
                self.result = args
        class FakeProcess:
            def __init__(self, argv, cwd, directory, stdin, env):
                case.assertEqual(argv[0], '/custom/claude')
                case.assertEqual(cwd, case.temp)
                case.assertIn(b'<context.json>', stdin)
                case.assertIn(b'Do not write files or execute commands', stdin)
                case.assertIn(b'controller generates the complete Codex repair contract', stdin)
                case.assertIn(b'Do not write a contract', stdin)
                self.stdout_path, self.stderr_path = directory / 'out', directory / 'err'
                self.stdout_path.write_text(stdout, encoding='utf-8')
                self.stderr_path.write_text(stderr, encoding='utf-8')
            def resume(self):
                pass
            def poll(self):
                return code
            def stop(self):
                return True
        context = Context()
        adapter = ClaudeAdapter(context, '/custom/claude', process_factory=FakeProcess)
        adapter.tick()
        context.report = adapter.report()
        return context

    def test_structured_output_validated_and_written(self):
        value = decision({'checkpoint_id': uid()})
        context = self.run_adapter(json.dumps({'type': 'result', 'is_error': False, 'structured_output': value}))
        self.assertEqual(context.result, ('succeeded',))
        self.assertEqual(json.loads((self.temp / 'decision.json').read_bytes()), value)
        self.assertIsNone(context.observed)
        p.check_uuid(context.native_session_ref)
        self.assertEqual(context.report, {'harness': 'claude', 'decision': 'accept', 'notes': value['notes']})

    def test_bad_schema_output_never_writes_decision(self):
        for value in ({}, {'decision': 'accept'}, [], None):
            with self.subTest(value=value):
                context = self.run_adapter(json.dumps({'structured_output': value}))
                self.assertEqual(context.result[:2], ('failed', 'code_failure'))
                self.assertFalse((self.temp / 'decision.json').exists())
                self.assertIsNone(context.report['decision'])
                self.assertTrue(context.report['notes'])

    def test_plain_text_result_is_not_trusted_as_structured_output(self):
        context = self.run_adapter(json.dumps({'result': json.dumps(decision({'checkpoint_id': uid()}))}))
        self.assertEqual(context.result[:2], ('failed', 'code_failure'))
        self.assertFalse((self.temp / 'decision.json').exists())

    def test_repair_request_schema_is_shared_and_all_fields_are_required(self):
        schema = d.DECISION_SCHEMA['properties']['follow_up']['anyOf'][1]
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(set(schema['required']), {'title', 'goal', 'acceptance', 'instructions', 'effort'})
        self.assertEqual(schema['properties']['acceptance']['minItems'], 1)
        self.assertEqual(schema['properties']['effort']['enum'], ['high', 'xhigh', 'max'])
        for field in schema['required']:
            value = repair_request()
            del value[field]
            with self.subTest(missing=field):
                context = self.run_adapter(json.dumps({'structured_output': decision({'checkpoint_id': uid()}, 'rework', value)}))
                self.assertEqual(context.result[:2], ('failed', 'code_failure'))
                self.assertFalse((self.temp / 'decision.json').exists())

    def test_legacy_contract_extra_fields_and_invalid_acceptance_or_effort_rejected(self):
        for follow in ({'title': 'legacy', 'contract': contract()}, repair_request(contract=contract()),
                       repair_request(acceptance=[]), repair_request(acceptance=[1]), repair_request(effort='low')):
            with self.subTest(follow=follow):
                context = self.run_adapter(json.dumps({'structured_output': decision({'checkpoint_id': uid()}, 'rework', follow)}))
                self.assertEqual(context.result[:2], ('failed', 'code_failure'))
                self.assertFalse((self.temp / 'decision.json').exists())

    def test_valid_repair_output_written_without_contract_or_target_validation(self):
        for effort in ('high', 'xhigh', 'max'):
            value = decision({'checkpoint_id': uid()}, 'rework', repair_request(effort=effort))
            with self.subTest(effort=effort), patch.object(d, 'validate_contract', side_effect=AssertionError('not adapter responsibility')):
                context = self.run_adapter(json.dumps({'structured_output': value}))
                self.assertEqual(context.result, ('succeeded',))
                self.assertEqual(json.loads((self.temp / 'decision.json').read_bytes()), value)
                (self.temp / 'decision.json').unlink()

    def test_schema_valid_output_defers_assessment_semantics_to_controller(self):
        value = decision({'checkpoint_id': 'controller-must-check-this'})
        value['assessment']['status'] = 'blocked'
        context = self.run_adapter(json.dumps({'structured_output': value}))
        self.assertEqual(context.result, ('succeeded',))
        self.assertEqual(json.loads((self.temp / 'decision.json').read_bytes()), value)
        with self.assertRaises(ValueError):
            d.validate_decision(value)

    def test_auth_failure_text_classified_without_credentials(self):
        for stdout, stderr, code in (('', 'authentication expired; please sign in', 1),
                (json.dumps({'is_error': True, 'result': '401 unauthorized'}), '', 0),
                ('', 'Invalid API key · Please run /login', 1), ('', 'Not logged in', 1)):
            with self.subTest(code=code):
                context = self.run_adapter(stdout, stderr, code)
                self.assertEqual(context.result[:2], ('failed', 'auth_expired'))
                self.assertFalse((self.temp / 'decision.json').exists())

    def test_detect_uses_configured_executable_and_missing_stays_null(self):
        # 隔离 PATH/HOME：宿主上真实安装的 codex/claude 不能影响结果。
        env = dict(PATH=str(self.temp / 'empty-path'), HOME=str(self.temp / 'empty-home'))
        custom = self.temp / 'custom-claude'
        custom.write_text('fixture')
        custom.chmod(0o755)
        with patch('agent_collab.node_agent.probe', return_value='claude-stub-version') as probe:
            caps = detect(str(custom), commands=resolve_commands({'claude_command': str(custom)}, env=env, system='Linux'))
        self.assertIn([str(custom.absolute()), '--version'], [c[0][0] for c in probe.call_args_list])
        self.assertEqual(caps['tools']['claude'], 'claude-stub-version')
        self.assertEqual(caps['harness']['claude'], 'claude-stub-version')
        absent = str(self.temp / 'absent')
        with patch('agent_collab.node_agent.probe') as probe:
            caps = detect(absent, commands=resolve_commands({'claude_command': absent}, env=env, system='Linux'))
        self.assertFalse([c for c in probe.call_args_list if c[0][0][0] == absent])
        self.assertIsNone(caps['harness']['claude'])

    def test_decision_semantic_validation(self):
        valid = decision({'checkpoint_id': uid()})
        bad = []
        value = copy.deepcopy(valid)
        value['extra'] = True
        bad.append(value)
        value = copy.deepcopy(valid)
        value['assessment']['checkpoint_id'] = 'not-uuid'
        bad.append(value)
        value = copy.deepcopy(valid)
        value['assessment']['status'] = 'blocked'
        bad.append(value)
        value = copy.deepcopy(valid)
        value['follow_up'] = {'title': 'no', 'contract': contract()}
        bad.append(value)
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                d.validate_decision(value)


class ClaudeAgentControllerTests(ControllerCase):
    def test_stub_child_structured_decision_enters_final_checkpoint_and_is_applied(self):
        self.app.config.update(copy.deepcopy(CONFIG))
        worker = self.node('worker')
        job = self.job()
        a = worker.claim()
        cp = worker.checkpoint(a, kind='final')
        worker.complete(a, checkpoint=cp)
        def transport(method, url, headers, body):
            path = urlsplit(url).path
            result = self.app.handle(method, path, headers, rc4_agent_body(path, body))
            return result.status, result.headers, result.body
        data_dir = self.temp / 'agent'
        pair(argparse.Namespace(controller='https://controller.invalid', code=self.app.pairing_code('mac'),
            node_id='mac', data_dir=str(data_dir), ca_file=None, slots=1, host_slots=1,
            physical_host_id='mac', failure_domain='mac', label=[]), transport)
        config_path = data_dir / 'agent.json'
        config = json.loads(config_path.read_bytes())
        config['claude_command'] = '/stub-only/claude'
        config_path.write_bytes(p.canonical_json(config))
        script = self.temp / 'claude_stub.py'
        script.write_text('import json, pathlib, sys\n'
            'assert "<context.json>" in sys.stdin.read()\n'
            'c = json.loads(pathlib.Path("context.json").read_text())\n'
            'schema = json.loads(sys.argv[sys.argv.index("--json-schema")+1])\n'
            'assert "assessment" in schema["required"]\n'
            'print(json.dumps({"is_error": False, "structured_output": {"decision": "accept",\n'
            '"assessment": {"status": "passed", "method": "local stub", "result": "ok",\n'
            '"checkpoint_id": c["job"]["durable_checkpoint_id"]}, "follow_up": None,\n'
            '"notes": "stub " + pathlib.Path("../lease.token").read_text()}}))\n', encoding='utf-8')
        calls = []
        def factory(argv, cwd, directory, stdin, env):
            calls.append(argv)
            return OwnedProcess([sys.executable, str(script)] + argv[1:], cwd, directory, stdin, env)
        def adapter(context, command, **kwargs):
            return ClaudeAdapter(context, command, process_factory=factory, **kwargs)
        commands = resolve_commands({'claude_command': __import__('sys').executable})
        commands['claude']['argv'] = ['/stub-only/claude']  # Only the injected process factory consumes this.
        agent = Agent(data_dir, transport=transport, clock=self.clock, capabilities=copy.deepcopy(CAPS),
                      auth_runner=no_auth, commands=commands)
        self.addCleanup(agent.close)
        with patch('agent_collab.node_agent.ClaudeAdapter', side_effect=adapter):
            # 按挂钟时间等待：Windows 运行器上 Python 子进程启动可能超过 1 秒，固定 100 次循环不够。
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                agent.cycle()
                self.clock.advance(.1)
                if self.current(job)['acceptance']['status'] == 'passed':
                    break
                time.sleep(.01)
        self.assertEqual(self.current(job)['acceptance']['status'], 'passed')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], '/stub-only/claude')
        self.assertEqual(calls[0][calls[0].index('--model') + 1], PROFILE['model'])
        self.assertEqual(calls[0][calls[0].index('--effort') + 1], PROFILE['effort'])
        attempt = next(iter(agent.attempts.values()))
        final = next(c for c in attempt.meta['checkpoints'] if c['kind'] == 'final')
        self.assertTrue(final['durable'])
        entry = next(f for f in final['manifest']['files'] if f['path'] == 'decision.json')
        value = json.loads(self.app.blobs.read(entry['sha256']))
        self.assertEqual(value['assessment']['checkpoint_id'], cp['checkpoint_id'])
        self.assertEqual(attempt.runtime()['model'], PROFILE['model'])
        self.assertEqual(attempt.runtime()['effort'], PROFILE['effort'])
        report = attempt.meta['completion']['report']
        self.assertEqual({k: v for k, v in report.items() if k != 'checks'},
                         {'harness': 'claude', 'decision': 'accept', 'notes': 'stub [REDACTED]'})
        self.assertEqual(self.app.get('attempt', attempt.id)['report'], report)
