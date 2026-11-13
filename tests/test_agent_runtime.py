import argparse
import json
import os
from pathlib import Path
import random
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_collab import artifacts, protocol as p
from agent_collab.client import Client, ConnectionFailure, validate_url
from agent_collab.node_agent import Agent, AgentLock, Backoff, bounded_report, detect, doctor, pair
from fake_controller import FakeController, no_auth
from agent_collab.adapters.commands import resolve_commands

ROOT = Path(__file__).resolve().parents[1]
CAPS = dict(os='macos', arch='arm64', tools={}, gpu=[], harness={}, labels=[])


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, delta=.5):
        self.value += delta


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.dev/tmp').mkdir(parents=True, exist_ok=True)
        self.temp = Path(tempfile.mkdtemp(dir=str(ROOT / '.dev/tmp')))
        self.addCleanup(shutil.rmtree, str(self.temp))
        self.fake = FakeController()
        self.clock = Clock()
        self.data = self.temp / 'agent'
        self.args = argparse.Namespace(controller='https://controller.invalid', code='one-time', node_id='node-a',
            data_dir=str(self.data), ca_file=None, slots=1, host_slots=1, physical_host_id='host-a',
            failure_domain='domain-a', label=['test'])
        pair(self.args, self.fake)
        self.agent = Agent(self.data, transport=self.fake, clock=self.clock, rng=random.Random(7), capabilities=dict(CAPS), auth_runner=no_auth)
        self.addCleanup(self.agent.close)

    def cycles(self, count=1):
        for _ in range(count):
            self.agent.cycle()
            self.clock.advance()

    def until(self, predicate, count=100):
        for _ in range(count):
            self.cycles()
            if predicate():
                return
        self.fail('condition did not complete')

    def attempt(self, assignment):
        return self.agent.attempts[assignment['attempt_id']]

    def test_pair_permissions_no_token_in_json_and_no_repair_overwrite(self):
        config = json.loads((self.data / 'agent.json').read_text())
        self.assertEqual(config['node_token_sha256'], p.sha256_bytes(b'test-node-secret'))
        self.assertNotIn('test-node-secret', (self.data / 'agent.json').read_text())
        if os.name != 'nt':
            self.assertEqual((self.data / 'node.token').stat().st_mode & 0o777, 0o600)
            self.assertEqual((self.data / 'agent.json').stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            pair(self.args, self.fake)

    def test_register_heartbeat_claim_order_and_isolated_workspaces(self):
        first = self.fake.add([{'write': {'path': 'a', 'content': 'one'}}])
        second = self.fake.add([{'write': {'path': 'a', 'content': 'two'}}])
        self.until(lambda: len(self.fake.publications) == 2)
        calls = [path for method, path in self.fake.calls if method == 'POST']
        self.assertLess(calls.index('/nodes/register'), calls.index('/nodes/node-a/heartbeat'))
        self.assertLess(calls.index('/nodes/node-a/heartbeat'), calls.index('/nodes/node-a/claim'))
        self.assertEqual((self.attempt(first).work / 'a').read_text(), 'one')
        self.assertEqual((self.attempt(second).work / 'a').read_text(), 'two')
        self.assertFalse((self.attempt(first).work / '.git').exists())

    def test_head_dedup_and_partial_upload_resume(self):
        assignment = self.fake.add([{'write': {'path': 'a', 'content': 'a'}}, {'write': {'path': 'b', 'content': 'b'}}])
        self.fake.fail_put_after = 1
        self.until(lambda: bool(self.fake.publications))
        self.assertEqual(len(self.fake.puts), len(set(self.fake.puts)))
        self.assertEqual(len(self.fake.puts), 3)  # two files and canonical manifest
        self.assertTrue(any(method == 'HEAD' for method, _ in self.fake.calls))
        self.assertEqual(self.attempt(assignment).meta['state'], 'reported')

    def test_missing_blobs_retries_same_checkpoint_id_and_payload(self):
        self.fake.add([{'write': {'path': 'a', 'content': 'a'}}])
        self.fake.missing_once = True
        self.until(lambda: bool(self.fake.publications))
        self.assertEqual(len(self.fake.checkpoint_requests), 2)
        self.assertEqual(self.fake.checkpoint_requests[0], self.fake.checkpoint_requests[1])
        self.assertEqual(len(set(self.fake.checkpoints)), 1)

    def test_events_survive_partition_and_lost_ack_with_original_ids(self):
        assignment = self.fake.add([{'sleep': 1}, {'progress': {'done': 1, 'total': 2}},
                                   {'progress': {'done': 2, 'total': 2}}, {'sleep': 50}])
        self.cycles(2)
        attempt = self.attempt(assignment)
        self.fake.offline = True
        self.cycles(10)
        self.assertGreaterEqual(len(attempt.events), 2)
        ids = {event['event_id'] for event in attempt.events}
        self.assertTrue(ids)
        self.assertTrue(attempt.events_path.read_bytes())
        claim_count = self.fake.calls.count(('POST', '/nodes/node-a/claim'))
        self.fake.offline = False
        self.fake.lose_event_response = True
        self.until(lambda: not attempt.events)
        self.assertTrue(ids.issubset(self.fake.events))
        self.assertEqual(len(self.fake.events), len(set(self.fake.events)))
        self.assertEqual(claim_count, self.fake.calls.count(('POST', '/nodes/node-a/claim')))

    def test_reconnect_heartbeat_precedes_outbox_and_claim(self):
        self.fake.offline = True
        self.cycles(3)
        self.assertFalse(self.agent.connected)
        self.assertNotIn(('POST', '/nodes/node-a/claim'), self.fake.calls)
        self.fake.offline = False
        boundary = len(self.fake.calls)
        self.until(lambda: self.agent.connected)
        calls = self.fake.calls[boundary:]
        self.assertLess(calls.index(('POST', '/nodes/node-a/heartbeat')), calls.index(('POST', '/nodes/node-a/claim')))

    def test_revoked_stops_quarantines_never_publishes_and_archives_late_events(self):
        assignment = self.fake.add([{'sleep': 100}])
        self.cycles(2)
        attempt = self.attempt(assignment)
        event_id = attempt.event('attempt.progress', {'done': 1})
        self.fake.revoked.add(attempt.id)
        self.clock.advance(2)
        self.cycles(4)
        self.assertTrue(attempt.adapter.stopped)
        self.assertEqual(attempt.meta['state'], 'quarantined')
        self.assertTrue((attempt.directory / 'quarantined.json').exists())
        self.assertTrue(attempt.work.exists())
        self.assertTrue(self.fake.events[event_id]['stale'])
        self.assertFalse(self.fake.publications)

    def test_lease_expiry_pauses_side_effect_steps_until_reconciled(self):
        assignment = self.fake.add([{'sleep': 8}, {'side_effect': {'op_id': 'work:op', 'state': 'done'}},
                                   {'write': {'path': 'after', 'content': 'after'}}], side_effects='idempotent')
        self.cycles(2)
        attempt = self.attempt(assignment)
        self.fake.offline = True
        self.cycles(30)
        self.assertFalse((attempt.work / 'after').exists())
        self.assertFalse(attempt.can_execute())
        self.fake.offline = False
        self.until(lambda: bool(self.fake.publications))
        self.assertTrue((attempt.work / 'after').exists())

    def test_no_side_effect_attempt_continues_offline_but_cannot_publish(self):
        assignment = self.fake.add([{'sleep': 8}, {'write': {'path': 'after', 'content': 'local'}}])
        self.cycles(2)
        self.fake.offline = True
        self.cycles(30)
        self.assertEqual((self.attempt(assignment).work / 'after').read_text(), 'local')
        self.assertEqual(self.attempt(assignment).meta['state'], 'finished_unreported')
        self.assertFalse(self.fake.publications)

    def test_checkpoint_restore_skips_completed_steps(self):
        workspace = self.temp / 'input'
        workspace.mkdir()
        (workspace / 'count').write_text('already complete')
        manifest, blobs = artifacts.snapshot(workspace)
        self.fake.blobs.update(blobs)
        assignment = self.fake.add([{'fail': 'code_failure'}, {'checkpoint': {}},
            {'write': {'path': 'new', 'content': 'restored'}}], manifest=manifest, summary={'completed': [0]})
        self.until(lambda: bool(self.fake.publications))
        self.assertEqual((self.attempt(assignment).work / 'count').read_text(), 'already complete')
        boundaries = [v for v in self.fake.checkpoints.values() if v['kind'] == 'boundary']
        self.assertEqual(boundaries[0]['summary']['completed'], [0, 1])
        self.assertEqual(boundaries[0]['summary']['pending'], [2])
        report = self.fake.completions[assignment['attempt_id']]['report']
        self.assertEqual(report['harness'], 'stub')
        self.assertEqual([s['status'] for s in report['steps']], ['skipped_from_checkpoint', 'done', 'done'])
        self.assertEqual([s['op'] for s in report['steps']], ['fail', 'checkpoint', 'write'])
        self.assertEqual([s['index'] for s in report['steps']], [0, 1, 2])
        self.assertTrue(all(s['exit_code'] is None for s in report['steps']))

    def test_stub_fail_propagates_class(self):
        assignment = self.fake.add([{'fail': 'quota'}])
        self.until(lambda: bool(self.fake.completions))
        self.assertEqual(self.fake.completions[assignment['attempt_id']]['failure_class'], 'quota')
        self.assertFalse(self.fake.publications)

    def test_report_run_tails_exit_codes_redaction_and_failures(self):
        for code in (0, 3):
            with self.subTest(code=code):
                steps = [{'progress': {'done': 1}}, {'run': ['{python}', '-c', '']}, {'delete': {'path': 'absent'}}]
                assignment = self.fake.add(steps)
                secret = assignment['lease_token']
                # 直接写 UTF-8 字节：Windows 上重定向的标准输出用区域编码（如 cp1252），print 中文会让子进程出错退出。
                steps[1]['run'][-1] = ('import sys; sys.stdout.buffer.write(("文" * 5000 + ' + repr(secret + 'test-node-secret END') +
                                      ' + "\\n").encode("utf-8")); sys.stdout.flush(); print("e" * 5000 + " ERR", file=sys.stderr); sys.exit(' + str(code) + ')')
                for _ in range(150):
                    self.cycles()
                    if assignment['attempt_id'] in self.fake.completions:
                        break
                    time.sleep(.01)
                report = self.fake.completions[assignment['attempt_id']]['report']
                raw = p.canonical_json(report)
                self.assertNotIn(secret.encode(), raw)
                self.assertNotIn(b'test-node-secret', raw)
                self.assertTrue(report['truncated'])
                self.assertLessEqual(len(raw), 65536)
                run = report['steps'][1]
                self.assertEqual(run['exit_code'], code)
                self.assertEqual(run['status'], 'done' if code == 0 else 'failed')
                self.assertEqual([s['status'] for s in report['steps']],
                                 ['done', 'done', 'done'] if code == 0 else ['done', 'failed'])
                for key, ending in (('stdout_tail', ' END\n'), ('stderr_tail', ' ERR\n')):
                    self.assertLessEqual(len(run[key].encode('utf-8')), 4096)
                    # Windows 子进程文本模式输出 CRLF。
                    self.assertTrue(run[key].replace('\r\n', '\n').endswith(ending))

    def test_report_total_bound_counts_json_escaping_and_preserves_schema(self):
        assignment = self.fake.add([])
        self.until(lambda: assignment['attempt_id'] in self.fake.completions)
        attempt = self.attempt(assignment)
        # Exercise the complete outbox with many individually legal step tails.
        steps = [dict(index=i, op='run', status='done', exit_code=0,
                      stdout_tail='\x00' * 4096 + 'test-node-secret', stderr_tail='') for i in range(30)]
        attempt.meta['completion'] = None
        with patch.object(attempt.adapter, 'report', return_value={'harness': 'stub', 'steps': steps}):
            attempt.finish('failed', 'code_failure', 'test')
        report = attempt.meta['completion']['report']
        self.assertTrue(report['truncated'])
        self.assertLessEqual(len(p.canonical_json(report)), 65536)
        self.assertGreater(len(report['steps']), 0)
        self.assertNotIn(b'test-node-secret', p.canonical_json(report))
        huge = bounded_report({'harness': 'codex', 'usage': {'detail': 'x' * 100000},
                               'cli_error': {'detail': 'x' * 100000}, 'final_message': '文' * 20000})
        self.assertTrue(huge['truncated'])
        self.assertLessEqual(len(p.canonical_json(huge)), 65536)
        claude = bounded_report({'harness': 'claude', 'decision': 'accept', 'notes': '\x00' * 100000})
        self.assertEqual(claude['decision'], 'accept')
        self.assertTrue(claude['truncated'])
        self.assertLessEqual(len(p.canonical_json(claude)), 65536)

    def test_agent_checkpoint_honors_custom_workspace_exclusion(self):
        assignment = self.fake.add([{'write': {'path': 'keep.txt', 'content': 'kept'}},
                                    {'write': {'path': 'build/output', 'content': 'ignored'}}])
        assignment['contract']['workspace'] = {'exclude': ['build/**']}
        self.until(lambda: assignment['attempt_id'] in self.fake.completions)
        final = self.attempt(assignment).meta['checkpoints'][-1]
        self.assertEqual([f['path'] for f in final['manifest']['files']], ['keep.txt'])

    def test_codex_report_from_python_stub_is_sanitized_and_bounded(self):
        session = '5cb86daf-bcf1-44c2-9a68-8849447476e1'
        for failure in (False, True):
            with self.subTest(failure=failure):
                assignment = self.fake.add([])
                assignment['contract'].update(harness='codex', harness_params={'model': 'gpt-6-astra', 'effort': 'xhigh'})
                events = [dict(type='thread.started', thread_id=session),
                          dict(type='turn.completed', usage={'input_tokens': 7, 'output_tokens': 9})]
                if failure:
                    events.append(dict(type='error', message='test-node-secret ' + assignment['lease_token']))
                script = self.temp / 'codex_stub.py'
                script.write_text('import pathlib, sys\n'
                    'pathlib.Path(sys.argv[sys.argv.index("-o") + 1]).write_text(' +
                    '"文" * 20000 + ' + repr('test-node-secret ' + assignment['lease_token'] + ' FINAL') + ', encoding="utf-8")\n'
                    'print(' + repr('\n'.join(json.dumps(e) for e in events)) + ')\n', encoding='utf-8')
                self.agent.config.update(codex_command=[sys.executable, str(script)], codex_home=str(self.temp / 'no-sessions'))
                self.agent.commands = resolve_commands(self.agent.config)
                for _ in range(150):
                    self.cycles()
                    if assignment['attempt_id'] in self.fake.completions:
                        break
                    time.sleep(.01)
                report = self.fake.completions[assignment['attempt_id']]['report']
                self.assertEqual(report['harness'], 'codex')
                self.assertEqual(report['session_id'], session,
                                 (self.attempt(assignment).directory / 'stderr.log').read_text(errors='replace'))
                self.assertEqual(report['usage'], {'input_tokens': 7, 'output_tokens': 9})
                self.assertEqual(bool(report['cli_error']), failure)
                self.assertTrue(report['final_message'].endswith(' FINAL'))
                self.assertLessEqual(len(report['final_message'].encode()), 16384)
                self.assertTrue(report['truncated'])
                for secret in ('test-node-secret', assignment['lease_token']):
                    self.assertNotIn(secret.encode(), p.canonical_json(report))

    def test_stub_run_nonzero_code_failure_without_shell(self):
        assignment = self.fake.add([{'run': [sys.executable, '-c', 'raise SystemExit(3)']}])
        for _ in range(150):
            self.cycles()
            if self.fake.completions:
                break
            time.sleep(.01)
        self.assertEqual(self.fake.completions[assignment['attempt_id']]['failure_class'], 'code_failure')

    def test_cancel_stops_and_completes_cancelled(self):
        assignment = self.fake.add([{'sleep': 100}])
        self.cycles(2)
        self.fake.cancel.add(assignment['attempt_id'])
        self.until(lambda: bool(self.fake.completions))
        self.assertEqual(self.fake.completions[assignment['attempt_id']]['outcome'], 'cancelled')
        self.assertTrue(self.attempt(assignment).adapter.stopped)
        self.assertFalse(self.fake.publications)

    def test_generation_reconciliation_prevents_claim_and_quarantines(self):
        assignment = self.fake.add([{'sleep': 100}])
        self.cycles(2)
        self.fake.reconcile_required = True
        self.fake.add()
        self.clock.advance(2)
        previous = self.fake.calls.count(('POST', '/nodes/node-a/claim'))
        self.cycles(4)
        self.assertEqual(self.attempt(assignment).meta['state'], 'quarantined')
        self.assertEqual(self.fake.calls.count(('POST', '/nodes/node-a/claim')), previous)
        self.assertFalse(self.agent.reconciled)

    def test_fenced_checkpoint_quarantines(self):
        assignment = self.fake.add([{'write': {'path': 'a', 'content': 'a'}}])
        self.cycles(3)
        attempt = self.attempt(assignment)
        self.fake.revoked.add(attempt.id)
        # Suppress heartbeat so endpoint fencing, not heartbeat revocation, acts.
        self.agent.next_heartbeat = self.clock() + 100
        self.cycles(3)
        self.assertEqual(attempt.meta['state'], 'quarantined')
        self.assertFalse(self.fake.publications)

    def test_publication_version_mismatch_is_not_retried(self):
        assignment = self.fake.add()
        self.fake.version_mismatch = True
        self.until(lambda: self.attempt(assignment).meta['state'] == 'reported')
        self.cycles(10)
        self.assertEqual(sum(path.endswith('/publish') for _, path in self.fake.calls), 1)
        self.assertEqual(self.attempt(assignment).meta['publication']['error']['error']['code'], 'version_mismatch')

    def test_publication_lost_ack_replays_same_request_once_logically(self):
        self.fake.add()
        self.fake.lose_publish_response = True
        self.until(lambda: any(a.meta['state'] == 'reported' for a in self.agent.attempts.values()))
        self.assertEqual(len(self.fake.publications), 1)
        self.assertEqual(sum(path.endswith('/publish') for _, path in self.fake.calls), 2)

    def test_final_checkpoint_precedes_complete_precedes_publish(self):
        self.fake.add()
        self.until(lambda: bool(self.fake.publications))
        operations = [path.rsplit('/', 1)[-1] for method, path in self.fake.calls if method == 'POST']
        self.assertLess(operations.index('checkpoints'), operations.index('complete'))
        self.assertLess(operations.index('complete'), operations.index('publish'))

    def test_drain_and_dispatch_pause_block_claim(self):
        for field in ('drain', 'paused'):
            setattr(self.fake, field, True)
            self.agent.next_heartbeat = 0
            before = self.fake.calls.count(('POST', '/nodes/node-a/claim'))
            self.cycles(2)
            self.assertEqual(self.fake.calls.count(('POST', '/nodes/node-a/claim')), before)
            setattr(self.fake, field, False)

    def test_restart_abandons_stub_without_reexecution_or_token_in_metadata(self):
        assignment = self.fake.add([{'sleep': 100}])
        self.cycles(2)
        attempt = self.attempt(assignment)
        attempt.event('attempt.log', {'text': assignment['lease_token'] + ' test-node-secret'})
        self.assertNotIn(assignment['lease_token'], (attempt.directory / 'meta.json').read_text())
        self.assertNotIn(assignment['lease_token'], attempt.events_path.read_text())
        self.assertNotIn('test-node-secret', attempt.events_path.read_text())
        replacement = Agent(self.data, transport=self.fake, clock=self.clock, capabilities=dict(CAPS), auth_runner=no_auth)
        self.addCleanup(replacement.close)
        replacement.cycle()
        self.assertEqual(replacement.attempts[attempt.id].meta['state'], 'abandoned')
        self.assertIsNone(replacement.attempts[attempt.id].adapter)
        self.assertEqual(self.fake.last_heartbeat['attempts'][0]['state'], 'unknown')

    def test_integration_local_target_posts_result(self):
        target = self.temp / 'target'
        target.mkdir()
        assignment = self.fake.add([{'write': {'path': 'new', 'content': 'result'}}],
            integration={'target_node_id': 'node-a', 'target_path': str(target)})
        self.until(lambda: bool(self.fake.integrations) and self.fake.integrations[-1]['status'] == 'integrated')
        self.assertEqual((target / 'new').read_text(), 'result')
        self.assertEqual([x['status'] for x in self.fake.integrations], ['integration_pending', 'integration_pending', 'integrated'])

    def test_fault_partition_checks_file_without_network(self):
        fault = self.temp / 'partition'
        fault.touch()
        with patch.dict(os.environ, {'AGENT_COLLAB_FAULT_PARTITION': str(fault)}):
            with self.assertRaises(ConnectionFailure):
                self.agent.client.json('GET', '/healthz')
            fault.unlink()
            self.assertTrue(self.agent.client.json('GET', '/healthz')['ok'])

    def test_certificate_rejection_reports_the_reason_and_nothing_else(self):
        import ssl

        def rejected(method, url, headers, body):
            error = ssl.SSLCertVerificationError(1, 'verify failed for https://secret-host/path')
            error.verify_message = 'self-signed certificate'
            raise error

        client = Client('https://controller.example', 'token-value', transport=rejected)
        with self.assertRaises(ConnectionFailure) as raised:
            client.json('GET', '/healthz')
        self.assertEqual(str(raised.exception), 'TLS certificate verification failed: self-signed certificate')
        # 其他传输错误仍然只给固定文案。
        client = Client('https://controller.example', 'token-value',
                        transport=lambda *a: (_ for _ in ()).throw(OSError('token-value leaked')))
        with self.assertRaises(ConnectionFailure) as raised:
            client.json('GET', '/healthz')
        self.assertEqual(str(raised.exception), 'controller unavailable or TLS verification failed')

    def test_fault_exit_only_after_durable_checkpoint(self):
        self.fake.add([{'checkpoint': {}}])
        self.agent.fault_exit_after_checkpoint = 1
        with patch('agent_collab.node_agent.os._exit', side_effect=SystemExit) as terminate:
            with self.assertRaises(SystemExit):
                self.cycles(10)
            terminate.assert_called_once_with(137)
        self.assertTrue(self.fake.checkpoints)
        self.assertTrue(next(iter(self.agent.attempts.values())).meta['checkpoints'][0]['durable'])

    def test_heartbeat_jitter_and_backoff_bounds(self):
        self.cycles()
        self.assertGreaterEqual(self.agent.next_heartbeat - 100, .8)
        self.assertLessEqual(self.agent.next_heartbeat - 100, 1.2)
        backoff = Backoff(random.Random(3))
        values = [backoff.next() for _ in range(20)]
        self.assertTrue(all(0 <= v <= min(60, 2 ** min(i, 6)) for i, v in enumerate(values)))
        self.assertGreater(len(set(round(v, 2) for v in values[6:])), 5)
        backoff.reset()
        self.assertLessEqual(backoff.next(), 1)

    def test_tls_validation_and_loopback_only_http(self):
        for url in ('http://example.org', 'http://127.0.0.1.evil', 'https://u:p@example.org', 'ftp://localhost'):
            with self.assertRaises(ValueError):
                validate_url(url)
        for url in ('http://127.0.0.1', 'http://[::1]:1234', 'http://localhost'):
            self.assertEqual(validate_url(url), url)
        self.assertTrue(self.agent.client.context.check_hostname)
        import ssl
        self.assertEqual(self.agent.client.context.verify_mode, ssl.CERT_REQUIRED)

    def test_corrupt_download_rejected(self):
        sha = p.sha256_bytes(b'expected')
        self.fake.blobs[sha] = b'wrong'
        with self.assertRaises(ValueError):
            self.agent.client.fetch_blob(sha)

    def test_doctor_uses_injected_transport_no_model(self):
        with patch('agent_collab.node_agent.shutil.which', return_value=None):
            result = doctor(self.data, self.fake, runner=no_auth)
        self.assertTrue(result['ok'])
        self.assertEqual(result['checks']['harness'], {'stub': True, 'codex': False, 'claude': False})

    def test_detect_normalizes_wsl_and_failed_probes(self):
        with patch('agent_collab.node_agent.platform.system', return_value='Linux'), \
             patch('agent_collab.node_agent.platform.machine', return_value='AMD64'), \
             patch('agent_collab.node_agent.platform.release', return_value='6.1-microsoft-WSL2'), \
             patch('agent_collab.node_agent.shutil.which', return_value='/tool'), \
             patch('agent_collab.node_agent.probe', side_effect=['Python 3.9', None, None, 'GPU X', 'codex 1.2', 'claude test']):
            commands = {name: dict(argv=['/tool'], env={}, disabled=None) for name in ('codex', 'claude')}
            result = detect(commands=commands)
        self.assertEqual(result['os'], 'linux')
        self.assertEqual(result['arch'], 'x86_64')
        self.assertEqual(result['labels'], ['wsl'])
        # 找到但版本探测失败：可用、版本未知（协议 §3）；未找到才是 null。
        self.assertEqual(result['tools']['git'], 'unknown')
        self.assertEqual(result['harness']['codex'], 'codex 1.2')
        self.assertEqual(result['harness']['claude'], 'claude test')
        self.assertEqual(result['harness']['stub'], '0.1.0.dev0')
        self.assertEqual(result['gpu'], [{'name': 'GPU X', 'cuda': True}])
        with patch('agent_collab.node_agent.shutil.which', return_value=None):
            commands = {name: dict(argv=None, env={}, disabled=None) for name in ('codex', 'claude')}
            missing = detect(commands=commands)
        self.assertIsNone(missing['tools']['git'])
        self.assertIsNone(missing['harness']['codex'])
        self.assertIsNone(missing['harness']['claude'])

    def test_data_directory_lock_excludes_second_agent(self):
        with AgentLock(self.data):
            with self.assertRaises(ValueError):
                with AgentLock(self.data):
                    self.fail('lock admitted two owners')

    def test_abandoned_events_are_archived_stale_with_persisted_lease(self):
        assignment = self.fake.add([{'sleep': 100}])
        self.cycles(2)
        attempt = self.attempt(assignment)
        event_id = attempt.event('attempt.progress', {'done': 1})
        replacement = Agent(self.data, transport=self.fake, clock=self.clock, capabilities=dict(CAPS), auth_runner=no_auth)
        self.addCleanup(replacement.close)
        replacement.cycle()
        self.assertTrue(self.fake.events[event_id]['stale'])
        self.assertEqual(replacement.attempts[attempt.id].events, [])
        self.assertFalse(self.fake.publications)

    def test_slow_network_wait_keeps_local_steps_progressing(self):
        assignment = self.fake.add([{'progress': {'done': i}} for i in range(20)])
        self.cycles(2)
        attempt = self.attempt(assignment)
        def slow_transport(method, url, headers, body):
            time.sleep(.16)
            raise ConnectionError('delayed failure')
        self.agent.client.transport = slow_transport
        completed = len(attempt.adapter.completed)
        self.cycles()
        self.assertGreater(len(attempt.adapter.completed), completed + 1)
        self.assertFalse(self.agent.connected)

    def test_revocation_during_upload_prevents_complete_or_publish(self):
        assignment = self.fake.add([{'write': {'path': 'a', 'content': 'a'}}])
        self.cycles(3)
        attempt = self.attempt(assignment)
        def slow_upload(method, url, headers, body):
            result = self.fake(method, url, headers, body)
            if method == 'PUT':
                self.fake.revoked.add(attempt.id)
                self.clock.advance(2)
            return result
        self.agent.client.transport = slow_upload
        self.cycles(2)
        self.assertEqual(attempt.meta['state'], 'quarantined')
        self.assertFalse(self.fake.completions)
        self.assertFalse(self.fake.publications)

    def test_integration_pending_lost_response_retries_before_writing(self):
        target = self.temp / 'target'
        target.mkdir()
        assignment = self.fake.add([{'write': {'path': 'new', 'content': 'result'}}],
            integration={'target_node_id': 'node-a', 'target_path': str(target)})
        lost = [False]
        def transport(method, url, headers, body):
            result = self.fake(method, url, headers, body)
            if url.endswith('/integration') and not lost[0]:
                lost[0] = True
                raise ConnectionError('lost pending response')
            return result
        self.agent.client.transport = transport
        self.until(lambda: bool(self.fake.integrations) and self.fake.integrations[-1]['status'] == 'integrated')
        self.assertEqual((target / 'new').read_text(), 'result')
        self.assertEqual(self.fake.integrations[0], self.fake.integrations[1])
        self.assertEqual(self.fake.integrations[-1]['status'], 'integrated')

    def test_revocation_stops_real_stub_child_process(self):
        assignment = self.fake.add([{'run': [sys.executable, '-c', 'import time; time.sleep(30)']}])
        self.cycles(2)
        attempt = self.attempt(assignment)
        process = attempt.adapter.process
        self.assertIsNone(process.poll())
        self.fake.revoked.add(attempt.id)
        self.clock.advance(2)
        self.cycles()
        self.assertIsNotNone(process.poll())
        self.assertTrue(attempt.meta['stop_verified'])
        self.assertFalse(self.fake.publications)

    def test_snapshot_symlink_is_reported_as_failure_detail(self):
        assignment = self.fake.add([{'sleep': 1}])
        self.cycles(2)
        attempt = self.attempt(assignment)
        try:
            (attempt.work / 'link').symlink_to(self.temp, target_is_directory=True)
        except OSError:
            self.skipTest('symlinks unavailable')
        self.until(lambda: bool(self.fake.completions))
        completion = self.fake.completions[attempt.id]
        self.assertEqual(completion['failure_class'], 'code_failure')
        self.assertIn('symbolic link', completion['failure_detail'])
        self.assertFalse(self.fake.publications)

    def test_unacknowledged_checkpoint_payload_stays_immutable_during_later_writes(self):
        assignment = self.fake.add([{'write': {'path': 'a', 'content': 'before'}},
                                   {'checkpoint': {}}, {'write': {'path': 'a', 'content': 'after'}}])
        self.cycles(3)
        attempt = self.attempt(assignment)
        self.fake.offline = True
        self.cycles(5)
        checkpoint = attempt.meta['checkpoints'][0]
        sha = checkpoint['manifest']['files'][0]['sha256']
        self.assertEqual(self.agent.fetch_blob(sha), b'before')
        self.assertEqual((attempt.work / 'a').read_text(), 'after')
        self.fake.offline = False
        self.until(lambda: bool(self.fake.publications))
        self.assertEqual(self.fake.blobs[sha], b'before')

    def test_two_slots_keep_concurrent_attempts_isolated(self):
        self.agent.config['slots'] = 2
        first = self.fake.add([{'sleep': 100}])
        second = self.fake.add([{'sleep': 100}])
        self.cycles(4)
        self.assertEqual(len(self.agent.attempts), 2)
        self.assertNotEqual(self.attempt(first).work, self.attempt(second).work)
        self.assertEqual(self.attempt(first).meta['state'], 'running')
        self.assertEqual(self.attempt(second).meta['state'], 'running')

    def test_slow_heartbeat_does_not_extend_local_lease_by_response_latency(self):
        assignment = self.fake.add([{'sleep': 100}], side_effects='manual')
        self.cycles(2)
        attempt = self.attempt(assignment)
        def delayed(method, url, headers, body):
            result = self.fake(method, url, headers, body)
            if url.endswith('/heartbeat'):
                self.clock.advance(10)
            return result
        self.agent.client.transport = delayed
        sent_at = self.clock()
        self.agent.heartbeat()
        self.assertEqual(attempt.last_renewed, sent_at)
        self.assertFalse(attempt.can_execute())

    def test_integration_conflict_is_reported_without_overwriting_manual_file(self):
        target = self.temp / 'target'
        target.mkdir()
        (target / 'new').write_bytes(b'human')
        assignment = self.fake.add([{'write': {'path': 'new', 'content': 'agent'}}],
            integration={'target_node_id': 'node-a', 'target_path': str(target)})
        self.until(lambda: bool(self.fake.integrations) and self.fake.integrations[-1]['status'] == 'conflict')
        self.assertEqual((target / 'new').read_bytes(), b'human')
        self.assertEqual(self.fake.integrations[-1]['status'], 'conflict')
        self.assertEqual(self.fake.integrations[-1]['detail']['conflicts'], ['new'])

    def test_local_process_launch_error_is_infra_retryable(self):
        assignment = self.fake.add([{'run': ['unstarted-test-command']}])
        with patch('agent_collab.adapters.codex.subprocess.Popen', side_effect=PermissionError('test denial')):
            self.cycles(2)
        self.until(lambda: bool(self.fake.completions))
        self.assertEqual(self.fake.completions[assignment['attempt_id']]['failure_class'], 'infra_retryable')


class XcodeShimTests(unittest.TestCase):
    """干净的 macOS（没装开发者工具）上 /usr/bin/git、/usr/bin/python3 只是占位程序：运行它们会在用户桌面弹出安装提示。"""

    def test_shims_without_developer_tools_count_as_missing(self):
        from agent_collab.adapters import commands
        import subprocess
        calls = []
        missing = lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 2)
        present = lambda argv, **kw: subprocess.CompletedProcess(argv, 0)
        with patch.object(commands.shutil, 'which', side_effect=lambda name, path=None: None if path else '/usr/bin/' + name):
            self.assertIsNone(commands.which('git', system='Darwin', runner=missing))
            self.assertEqual(calls, [['/usr/bin/xcode-select', '-p']])  # 只查询，不运行 git 本身
            self.assertEqual(commands.which('git', system='Darwin', runner=present), '/usr/bin/git')
            self.assertEqual(commands.which('git', system='Linux', runner=missing), '/usr/bin/git')
            self.assertEqual(commands.which('node', system='Darwin', runner=missing), '/usr/bin/node')

    def test_real_tool_later_in_path_is_found_after_a_shim(self):
        from agent_collab.adapters import commands
        import subprocess
        missing = lambda argv, **kw: subprocess.CompletedProcess(argv, 2)
        found = lambda name, path=None: '/usr/bin/' + name if path is None else (
            '/opt/homebrew/bin/' + name if '/opt/homebrew/bin' in path.split(os.pathsep) and '/usr/bin' not in path.split(os.pathsep) else None)
        with patch.object(commands.shutil, 'which', side_effect=found), \
                patch.dict(commands.os.environ, {'PATH': os.pathsep.join(['/usr/bin', '/opt/homebrew/bin'])}):
            self.assertEqual(commands.which('git', system='Darwin', runner=missing), '/opt/homebrew/bin/git')

    def test_detect_and_git_scan_do_not_run_shims_on_a_clean_mac(self):
        import subprocess
        from agent_collab import project
        probed = []
        commands = {name: dict(argv=[], env={}, disabled=None) for name in ('codex', 'claude')}
        with patch('agent_collab.adapters.commands.platform.system', return_value='Darwin'), \
             patch('agent_collab.adapters.commands.shutil.which', side_effect=lambda name, path=None: '/usr/bin/' + name if name in ('git', 'python3') and path is None else None), \
             patch('agent_collab.adapters.commands.subprocess.run', return_value=subprocess.CompletedProcess([], 2)), \
             patch('agent_collab.node_agent.probe', side_effect=lambda argv, **kw: probed.append(argv)):
            tools = detect(commands=commands)['tools']
            with tempfile.TemporaryDirectory() as root:
                (Path(root) / '.git').mkdir()
                self.assertEqual(project.git_info(Path(root)), {'available': False})
        self.assertIsNone(tools['git'])
        self.assertIsNone(tools['python3'])
        self.assertEqual(probed, [])
