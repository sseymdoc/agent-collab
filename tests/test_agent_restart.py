"""Restart regressions: durable credentials, outbox replay and process ownership."""
import json
import os
import sys
import unittest
from unittest.mock import patch

from agent_collab import protocol as p
from agent_collab.adapters.codex import process_status
from agent_collab.client import APIError
from agent_collab.node_agent import Agent, doctor, save
import test_agent_runtime as runtime_tests


class RestartTests(unittest.TestCase):
    setUp = runtime_tests.RuntimeTests.setUp
    cycles = runtime_tests.RuntimeTests.cycles
    until = runtime_tests.RuntimeTests.until
    attempt = runtime_tests.RuntimeTests.attempt

    def restart(self):
        old = self.agent
        self.agent = Agent(self.data, transport=self.fake, clock=self.clock, capabilities=dict(runtime_tests.CAPS), auth_runner=runtime_tests.no_auth)
        self.addCleanup(self.agent.close)
        return old

    def finished_before_complete(self, integration=None):
        assignment = self.fake.add([{'write': {'path': 'result', 'content': 'once'}}], integration=integration)
        original = self.fake
        def disconnect(method, url, headers, body):
            if url.endswith('/complete'):
                raise ConnectionError('before completion')
            return original(method, url, headers, body)
        self.agent.client.transport = disconnect
        self.until(lambda: bool(self.fake.checkpoints))
        attempt = self.attempt(assignment)
        self.assertEqual(attempt.meta['state'], 'finished_unreported')
        self.assertTrue(attempt.meta['checkpoints'][0]['durable'])
        self.assertFalse(self.fake.completions)
        return assignment

    def test_finished_restart_reports_without_reexecution_or_checkpoint_reupload(self):
        assignment = self.finished_before_complete()
        uploaded = list(self.fake.puts)
        self.restart()
        self.assertEqual(self.attempt(assignment).meta['state'], 'finished_unreported')
        self.until(lambda: bool(self.fake.publications))
        attempt = self.attempt(assignment)
        self.assertIsNone(attempt.adapter)
        self.assertEqual(attempt.meta['state'], 'reported')
        self.assertEqual((attempt.work / 'result').read_bytes(), b'once')
        self.assertEqual(self.fake.puts, uploaded)
        self.assertEqual(len(self.fake.checkpoint_requests), 1)
        self.assertEqual(len(self.fake.completions), 1)

    def test_completed_restart_replays_publication_even_without_lease_renewal(self):
        assignment = self.fake.add()
        self.fake.lose_publish_response = True
        self.until(lambda: bool(self.fake.publications))
        before = dict(self.attempt(assignment).meta['publication'])
        self.assertNotIn('receipt', before)
        self.assertTrue(self.attempt(assignment).meta['complete_ack'])
        self.restart()
        self.until(lambda: self.attempt(assignment).meta['state'] == 'reported')
        self.assertIsNone(self.attempt(assignment).last_renewed)
        self.assertIsNotNone(self.attempt(assignment).last_reconciled)
        self.assertEqual(len(self.fake.publications), 1)
        self.assertEqual(self.attempt(assignment).meta['publication']['request_id'], before['request_id'])
        self.assertEqual(len(self.fake.checkpoint_requests), 1)

    def test_restart_replays_lost_complete_response_with_identical_body(self):
        assignment = self.fake.add()
        requests = []
        def transport(method, url, headers, body):
            response = self.fake(method, url, headers, body)
            if url.endswith('/complete'):
                requests.append(body)
                raise ConnectionError('complete committed but response lost')
            return response
        self.agent.client.transport = transport
        self.until(lambda: bool(self.fake.completions))
        old_body = requests[0]
        self.restart()
        self.until(lambda: bool(self.fake.publications))
        self.assertEqual(p.canonical_json(self.fake.completions[assignment['attempt_id']]), old_body)
        self.assertEqual(sum(path.endswith('/complete') for _, path in self.fake.calls), 2)

    def test_finished_restart_is_still_fenced(self):
        assignment = self.finished_before_complete()
        self.fake.revoked.add(assignment['attempt_id'])
        self.restart()
        self.cycles(2)
        self.assertEqual(self.attempt(assignment).meta['state'], 'quarantined')
        self.assertFalse(self.fake.completions)
        self.assertFalse(self.fake.publications)

    def test_finished_restart_replays_integration_receipt_without_writing_twice(self):
        target = self.temp / 'target'
        target.mkdir()
        assignment = self.fake.add([{'write': {'path': 'result', 'content': 'once'}}],
            integration={'target_node_id': 'node-a', 'target_path': str(target)})
        def transport(method, url, headers, body):
            response = self.fake(method, url, headers, body)
            if url.endswith('/integration') and json.loads(body)['status'] == 'integrated':
                raise ConnectionError('integration response lost')
            return response
        self.agent.client.transport = transport
        self.until(lambda: len(self.fake.integrations) == 2)
        record = self.fake.integrations[-1]
        (target / 'result').write_bytes(b'manual after integration')
        self.restart()
        self.until(lambda: self.attempt(assignment).meta['state'] == 'reported')
        self.assertEqual(self.fake.integrations[-1], record)
        self.assertEqual((target / 'result').read_bytes(), b'manual after integration')
        self.assertEqual(len(self.fake.checkpoint_requests), 1)

    def test_running_stub_without_child_is_abandoned_and_next_assignment_claimed(self):
        first = self.fake.add([{'sleep': 100}])
        self.cycles(2)
        second = self.fake.add([{'sleep': 100}])
        self.restart()
        self.assertEqual(self.attempt(first).meta['state'], 'abandoned')
        self.assertTrue(self.attempt(first).work.exists())
        self.cycles()
        self.assertEqual(self.fake.last_heartbeat['attempts'][0]['state'], 'unknown')
        self.assertIn(second['attempt_id'], self.agent.attempts)
        self.assertFalse(self.attempt(first).meta['restart_unknown'])

    def test_preparing_without_spawn_is_abandoned(self):
        first = self.fake.add()
        self.cycles()
        self.assertEqual(self.attempt(first).meta['state'], 'preparing')
        second = self.fake.add()
        self.restart()
        self.cycles()
        self.assertEqual(self.attempt(first).meta['state'], 'abandoned')
        self.assertIn(second['attempt_id'], self.agent.attempts)

    def test_live_recorded_child_blocks_claim_until_exit(self):
        first = self.fake.add([{'run': [sys.executable, '-c', 'import time; time.sleep(30)']}])
        with patch('agent_collab.node_agent.process_stamp', return_value='recorded-creation-time'):
            self.cycles(2)
        original = self.attempt(first)
        child = original.adapter.process
        self.addCleanup(child.stop)
        self.assertEqual(original.meta['process']['pid'], child.child.pid)
        self.assertEqual(original.meta['process']['stamp'], 'recorded-creation-time')
        second = self.fake.add()
        with patch('agent_collab.adapters.codex.process_stamp', return_value='recorded-creation-time'):
            with self.assertLogs('agent_collab.node_agent', level='WARNING') as log:
                self.restart()
            self.cycles(2)
            restored = self.attempt(first)
            self.assertTrue(restored.meta['restart_unknown'])
            self.assertEqual(restored.meta['restart_process_status'], 'alive')
            self.assertNotIn(second['attempt_id'], self.agent.attempts)
            result = doctor(self.data, self.fake, runner=runtime_tests.no_auth)
            self.assertFalse(result['checks']['restart_clear'])
            self.assertIn(str(child.child.pid), result['restart_blockers'][0]['inspect_command'])
            self.assertNotIn(first['lease_token'], json.dumps(result) + '\n'.join(log.output))
        self.assertTrue(child.stop())
        self.cycles(2)
        self.assertEqual(restored.meta['state'], 'abandoned')
        self.assertFalse(restored.meta['restart_unknown'])
        self.assertTrue(restored.meta['stop_verified'])
        self.assertIn(second['attempt_id'], self.agent.attempts)

    def test_uninspectable_record_stays_blocked_and_is_rechecked(self):
        first = self.fake.add([{'sleep': 100}])
        self.cycles(2)
        attempt = self.attempt(first)
        attempt.meta['process'] = dict(state='running', pid=os.getpid(), stamp='saved')
        attempt.persist()
        second = self.fake.add()
        with patch('agent_collab.adapters.codex.process_stamp', side_effect=PermissionError('unavailable')):
            self.restart()
            self.cycles()
            self.assertTrue(self.attempt(first).meta['restart_unknown'])
            self.assertNotIn(second['attempt_id'], self.agent.attempts)
        with patch('agent_collab.node_agent.process_status', return_value='gone'):
            self.cycles(2)
        self.assertIn(second['attempt_id'], self.agent.attempts)

    def test_recorded_child_already_exited_is_abandoned_on_restart(self):
        first = self.fake.add([{'run': [sys.executable, '-c', 'import time; time.sleep(30)']}])
        self.cycles(2)
        original = self.attempt(first)
        self.assertEqual(original.meta['process']['state'], 'running')
        # Simulate process exit before the old agent records the exit.
        self.assertTrue(original.adapter.process.stop())
        second = self.fake.add()
        self.restart()
        self.assertEqual(self.attempt(first).meta['state'], 'abandoned')
        self.cycles()
        self.assertIn(second['attempt_id'], self.agent.attempts)

    def test_spawn_window_without_pid_remains_unknown(self):
        first = self.fake.add()
        self.cycles()
        attempt = self.attempt(first)
        attempt.meta['process'] = dict(state='starting', pid=None, stamp=None)
        attempt.persist()
        self.fake.add()
        self.restart()
        self.cycles()
        self.assertTrue(self.attempt(first).meta['restart_unknown'])
        self.assertEqual(len(self.agent.attempts), 1)
        result = doctor(self.data, self.fake, runner=runtime_tests.no_auth)
        self.assertTrue(result['restart_blockers'][0]['inspect_command'])

    def test_lease_is_private_and_redacted_across_restart_and_http_errors(self):
        assignment = self.fake.add([{'sleep': 100}])
        self.cycles(2)
        attempt = self.attempt(assignment)
        lease = assignment['lease_token']
        path = attempt.directory / 'lease.token'
        self.assertEqual(path.read_text(), lease)
        if os.name != 'nt':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        attempt.event('attempt.log', {'text': lease, lease: ['test-node-secret']})
        self.restart()
        self.assertNotIn(lease, (attempt.directory / 'meta.json').read_text())
        self.assertNotIn(lease, attempt.events_path.read_text())
        self.assertNotIn(lease, json.dumps(self.agent.clean({lease: [lease]})))
        def error(*args):
            return 409, {}, p.canonical_json(p.error_body('conflict', lease,
                echoed={'lease': lease, 'node': 'test-node-secret'}))
        self.agent.client.transport = error
        with self.assertRaises(APIError) as caught:
            self.agent.client.json('POST', '/test', {'lease_token': lease})
        self.assertNotIn(lease, json.dumps(caught.exception.body) + str(caught.exception))
        self.assertNotIn('test-node-secret', json.dumps(caught.exception.body))

    def test_missing_or_corrupt_lease_quarantines_completed_attempt(self):
        assignment = self.finished_before_complete()
        path = self.attempt(assignment).directory / 'lease.token'
        path.write_text('wrong credential', encoding='utf-8')
        self.restart()
        self.cycles()
        self.assertEqual(self.attempt(assignment).meta['state'], 'quarantined')
        self.assertFalse(self.fake.publications)

    def test_process_identity_handles_pid_reuse_and_unknown_conservatively(self):
        record = {'state': 'running', 'pid': 1234, 'stamp': 'old'}
        with patch('agent_collab.adapters.codex.process_stamp', return_value='new'):
            self.assertEqual(process_status(record), 'gone')
        with patch('agent_collab.adapters.codex.process_stamp', return_value='old'):
            self.assertEqual(process_status(record), 'alive')
            self.assertEqual(process_status(dict(record, stamp=None)), 'unknown')
        with patch('agent_collab.adapters.codex.process_stamp', side_effect=PermissionError):
            self.assertEqual(process_status(record), 'unknown')
        with patch('agent_collab.adapters.codex.process_stamp', return_value=None), \
                patch('agent_collab.adapters.codex.group_empty', return_value=True):
            self.assertEqual(process_status(record), 'gone')
