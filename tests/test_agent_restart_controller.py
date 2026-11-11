"""Use the owning controller in process; no sockets or duplicated controller logic."""
import argparse
import json
import os
import subprocess
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from agent_collab.node_agent import Agent, pair
from helpers import ControllerCase, contract, rc4_agent_body
from fake_controller import no_auth
from agent_collab import protocol as p


class RestartControllerTests(ControllerCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.drop_after = None
        self.agents = []
        self.agent_dir = self.temp / 'agent'
        args = argparse.Namespace(controller='https://controller.invalid',
            code=self.app.pairing_code('worker'), node_id='worker', data_dir=str(self.agent_dir),
            ca_file=None, slots=1, host_slots=1, physical_host_id='host', failure_domain='domain', label=[])
        pair(args, self.transport)
        self.build_agent()

    def tearDown(self):
        for agent in self.agents:
            agent.close()
        super().tearDown()

    def transport(self, method, url, headers, body):
        path = urlsplit(url).path
        self.calls.append((method, path, body))
        response = self.app.handle(method, path, headers, rc4_agent_body(path, body))
        if self.drop_after and path.endswith(self.drop_after):
            raise ConnectionError('response lost after controller transaction')
        payload = response.body
        if path.endswith('/heartbeat') and getattr(self, 'heartbeat_integrations', None):
            # rc5 heartbeat envelope is supplied by this work package's fixture;
            # controller integration is owned by the separate controller package.
            value = json.loads(payload)
            value['integrations'] = self.heartbeat_integrations()
            payload = p.canonical_json(value)
        return response.status, response.headers, payload

    def build_agent(self):
        capabilities = dict(os='linux', arch='x86_64', tools={}, gpu=[], harness={'stub': '1'}, labels=[])
        self.agent = Agent(self.agent_dir, transport=self.transport, clock=self.clock, capabilities=capabilities, auth_runner=no_auth)
        self.agents.append(self.agent)

    def drive(self, condition, limit=50):
        for _ in range(limit):
            self.agent.cycle()
            self.clock.advance(.25)
            if condition():
                return
        self.fail('controller/agent condition not reached')

    @unittest.skipUnless(os.name == 'nt', 'Windows ACL')
    def test_node_and_admin_tokens_have_no_inherited_access_on_windows(self):
        from agent_collab import artifacts
        for path in (self.agent_dir / 'node.token', self.temp / 'data' / 'admin.token'):
            listing = subprocess.run([artifacts._system_tool('icacls.exe'), str(path)], capture_output=True, text=True, errors='replace', check=True).stdout
            self.assertNotIn('(I)', listing, path)  # 继承来的 ACE 已去掉
            self.assertEqual([n.casefold() for n in artifacts._principals(path)], [artifacts._current_user()[0].casefold()], path)

    def test_pairing_is_not_sent_when_the_token_file_cannot_be_restricted(self):
        # 配对会用掉配对码并撤销同一 node_id 的旧凭据：收紧失败时请求不能发出，目录里也不留下凭据。
        directory = self.temp / 'unrestricted'
        args = argparse.Namespace(controller='https://controller.invalid', code=self.app.pairing_code('second'), node_id='second',
            data_dir=str(directory), ca_file=None, slots=1, host_slots=1, physical_host_id='host', failure_domain='domain', label=[])
        before = len(self.calls)
        with patch('agent_collab.artifacts.owner_only', side_effect=OSError('icacls failed')), self.assertRaises(OSError):
            pair(args, self.transport)
        self.assertEqual(self.calls[before:], [])
        self.assertEqual(list(directory.iterdir()), [])

    def test_admin_token_is_not_left_behind_when_it_cannot_be_restricted(self):
        from agent_collab.controller import App
        data = self.temp / 'restricted'
        with patch('agent_collab.controller.owner_only', side_effect=OSError('icacls failed')):
            with self.assertRaises(OSError):
                App(data, clock=self.clock)
        self.assertFalse((data / 'admin.token').exists())

    def test_restart_after_complete_commit_publishes_once_without_new_attempt(self):
        job = self.job(contract(harness_params={'steps': [{'write': {'path': 'result', 'content': 'one'}}]},
                               publish={'mode': 'auto', 'requires_acceptance': False}))
        self.drop_after = '/complete'
        self.drive(lambda: self.current(job)['status'] == 'completed')
        checkpoint_count = sum(path.endswith('/checkpoints') for _, path, _ in self.calls)
        initial = next(iter(self.agent.attempts.values()))
        self.assertFalse(initial.meta.get('complete_ack'))
        self.drop_after = None
        self.build_agent()
        self.drive(lambda: self.current(job)['delivery_version'] == 1)
        detail = self.current(job)
        self.assertEqual(len(detail['attempts']), 1)
        self.assertEqual(len(detail['publications']), 1)
        self.assertEqual(sum(path.endswith('/checkpoints') for _, path, _ in self.calls), checkpoint_count)
        restored = self.agent.attempts[initial.id]
        self.assertIsNone(restored.last_renewed)
        self.assertIsNone(restored.adapter)
        self.assertEqual((restored.work / 'result').read_bytes(), b'one')
        complete_bodies = [body for _, path, body in self.calls if path.endswith('/complete')]
        self.assertEqual(len(complete_bodies), 2)
        self.assertEqual(complete_bodies[0], complete_bodies[1])

    def test_restart_unknown_releases_controller_slot_and_claims_new_attempt(self):
        job = self.job(contract(harness_params={'steps': [{'sleep': 100}]}))
        self.drive(lambda: any(a.meta['state'] == 'running' for a in self.agent.attempts.values()))
        first = next(iter(self.agent.attempts.values()))
        self.build_agent()
        self.assertEqual(self.agent.attempts[first.id].meta['state'], 'abandoned')
        self.drive(lambda: len(self.agent.attempts) > 1)
        self.assertEqual(self.current(job)['attempts'][0]['status'], 'lost')
        self.assertEqual(self.agent.attempts[first.id].meta['state'], 'abandoned')
        heartbeat = [json.loads(body) for _, path, body in self.calls if path.endswith('/heartbeat')][-1]
        self.assertEqual(heartbeat['attempts'][0]['state'], 'unknown')
        self.assertFalse(self.agent.attempts[first.id].meta['restart_unknown'])

    def test_restart_after_integration_commit_only_replays_receipt(self):
        target = self.temp / 'target'
        target.mkdir()
        job = self.job(contract(harness_params={'steps': [{'write': {'path': 'result', 'content': 'one'}}]},
            publish={'mode': 'auto', 'requires_acceptance': False},
            integration={'target_node_id': 'worker', 'target_path': str(target)}))
        def items():
            current = self.current(job)
            if current['delivery_version'] and current['integration_status'] != 'integrated':
                attempt = next(iter(self.agent.attempts.values()))
                final = attempt.meta['checkpoints'][-1]
                return [dict(job_id=attempt.assignment['job_id'], checkpoint_id=final['checkpoint_id'],
                    manifest_sha256=p.manifest_sha256(final['manifest']), base_manifest_sha256=None,
                    target_path=str(target), exclude=[])]
            return []
        self.heartbeat_integrations = items
        actual_transport = self.agent.client.transport
        def lose_receipt(method, url, headers, body):
            response = actual_transport(method, url, headers, body)
            if url.endswith('/integration') and json.loads(body)['status'] == 'integrated':
                raise ConnectionError('integration committed but response lost')
            return response
        self.agent.client.transport = lose_receipt
        self.drive(lambda: self.current(job)['integration_status'] == 'integrated')
        (target / 'result').write_bytes(b'manual edit after integration')
        self.build_agent()
        self.drive(lambda: all(a.meta['state'] == 'reported' for a in self.agent.attempts.values()))
        self.assertEqual((target / 'result').read_bytes(), b'manual edit after integration')
        self.assertEqual(self.current(job)['delivery_version'], 1)
        integrated = [body for _, path, body in self.calls if path.endswith('/integration') and
                      json.loads(body)['status'] == 'integrated']
        self.assertEqual(len(integrated), 2)
        self.assertEqual(integrated[0], integrated[1])
