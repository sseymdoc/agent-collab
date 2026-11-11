"""CLI 的本地逻辑：结果判定、断线重连、凭据只发往配对地址、撤销与退出码。不启动 controller。"""
import argparse
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_collab import cli

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = '11111111-1111-4111-8111-111111111111'


def job(status, coordinator=None):
    return {'job_id': 'j1', 'work_package_id': 'wp', 'title': 't', 'status': status, 'status_reason': None,
            'attempts': [], 'current_attempt_id': None, 'checkpoints': [], 'acceptance': {'status': 'none'},
            'contract': {'publish': {'mode': 'auto', 'requires_acceptance': False}}, 'delivery_version': 0,
            'delivery_checkpoint_id': None, 'integration_status': None, 'cancel': None, 'counters': {}}


class FakeConn:
    def __init__(self, routes):
        self.routes = routes

    def call(self, method, path, data=None):
        return self.routes[path]


class Response:
    def __init__(self, lines, code=200):
        self.lines, self.code = lines, code

    def __iter__(self):
        return iter(self.lines)

    def getcode(self):
        return self.code

    def close(self):
        pass


class CliTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / '.dev' / 'tmp'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = Path(tempfile.mkdtemp(dir=str(parent)))
        self.addCleanup(shutil.rmtree, str(self.temp))

    def test_failed_job_with_an_active_coordination_round_is_still_awaited(self):
        def routes(round_status):
            return {'/jobs/j1/events?limit=1000': {'events': [{'type': 'coordinator.triggered', 'payload': {'coordinator_job_id': 'c1'}}]},
                    '/jobs/c1': {'job_id': 'c1', 'status': round_status, 'attempts': []}}
        for status in ('failed', 'waiting_reconcile'):
            self.assertEqual(cli.summarize(FakeConn(routes('queued')), job(status))['outcome'], 'awaiting_coordination')
            self.assertEqual(cli.summarize(FakeConn(routes('completed')), job(status))['outcome'], 'needs_attention')

    def test_stream_retries_unreachable_controller_and_fills_the_gap_after_snapshot_required(self):
        opened = []
        responses = [cli.Unreachable(),
                     Response([b'event: snapshot_required\n', b'data: {"min_seq": 5}\n', b'\n']),
                     Response([b'id: 7\n', b'event: x\n', b'data: {"seq": 7}\n', b'\n'])]

        def fake_open(url, token, method, path, *args, **kwargs):
            opened.append(path)
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        conn = FakeConn({'/events?after=4&limit=1000': {'events': [{'seq': 5}, {'seq': 6}]}})
        conn.url = conn.token = conn.ca_file = conn.controller_id = None
        with patch.object(cli, '_open', fake_open), patch.object(cli.time, 'sleep'):
            events = cli.stream(conn, 3)
            seen = [next(events) for _ in range(5)]
        self.assertEqual(seen, [None, {'seq': 5}, {'seq': 6}, None, {'seq': 7}])  # 第 5 条仍保留的事件没有丢
        self.assertEqual(opened, ['/events/stream?cursor=3'] * 2 + ['/events/stream?cursor=6'])

    def test_wait_keeps_waiting_through_a_controller_restart(self):
        done = dict(cli.summarize(FakeConn({'/jobs/j1/events?limit=1000': {'events': []}}), dict(job('completed'), delivery_version=1)),
                    settled=True)
        with patch.object(cli, 'settled_state', side_effect=[cli.Unreachable(), done]), patch.object(cli.time, 'sleep'), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.wait(None, 'j1', 60), 0)

    def test_proxy_5xx_without_controller_identity_is_a_network_problem(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                code, identity = {'/api/v1/proxy': (502, None), '/api/v1/own': (503, CONTROLLER), '/api/v1/other': (200, 'x')}[self.path]
                self.send_response(code)
                if identity:
                    self.send_header('X-Controller-Id', identity)
                self.send_header('Content-Length', '0')
                self.end_headers()

            def log_message(self, *args):
                pass
        server = HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = 'http://127.0.0.1:%d' % server.server_port
        with self.assertRaises(cli.Unreachable):
            cli.request(url, 't', 'GET', '/proxy', controller_id=CONTROLLER)
        self.assertEqual(cli.request(url, 't', 'GET', '/own', controller_id=CONTROLLER)[0], 503)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.request(url, 't', 'GET', '/other', controller_id=CONTROLLER)
        self.assertEqual(caught.exception.code, 3)
        with redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as caught:
            cli.main(['api', 'GET', '/x', '--url', 'http://127.0.0.1:9', '--token-file', str(self.token())])
        self.assertEqual((caught.exception.code, json.loads(err.getvalue())['error']['code']), (3, 'network'))

    def token(self):
        path = self.temp / 'admin.token'
        path.write_text('admin-secret', encoding='utf-8')
        return path

    def profile(self):
        home = self.temp / 'home'
        (home / 'clients').mkdir(parents=True)
        (home / 'clients' / 'default.json').write_text(json.dumps({'url': 'https://paired.example', 'token': 'client-secret',
                                                                    'controller_id': CONTROLLER, 'ca_file': None}), encoding='utf-8')
        return home

    def test_profile_credentials_are_only_sent_to_the_paired_address(self):
        home = self.profile()
        args = argparse.Namespace(profile='default', url='https://elsewhere.example', token_file=None, ca_file=None)
        with patch.dict(os.environ, {'AGENT_COLLAB_HOME': str(home)}), patch.object(cli, 'request') as sent:
            with self.assertRaises(SystemExit):
                cli.Conn(args)
            self.assertEqual(cli.Conn(argparse.Namespace(profile='default', url='https://paired.example/', token_file=None,
                                                         ca_file=None)).url, 'https://paired.example')
        sent.assert_not_called()

    def test_forget_revoke_uses_the_profile_credential_and_keeps_it_until_revoked(self):
        home = self.profile()
        saved = home / 'clients' / 'default.json'
        env = {'AGENT_COLLAB_HOME': str(home), 'AGENT_COLLAB_TOKEN_FILE': str(self.token())}
        for status, code, kept in ((401, 2, True), (200, 0, False)):
            with patch.dict(os.environ, env), patch.object(cli, 'request', return_value=(status, {})) as sent, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['client', 'forget', '--revoke']), code)
            self.assertEqual(sent.call_args[0][1], 'client-secret')  # 不是环境里的管理令牌
            self.assertEqual(saved.exists(), kept)

    def test_unauthorized_responses_exit_3_everywhere(self):
        with patch.object(cli, 'request', return_value=(401, {})), redirect_stdout(io.StringIO()):
            self.assertEqual(cli._client(['GET', '/overview', '--token-file', str(self.token())], False), 3)
            job_file = self.temp / 'job.json'
            job_file.write_text('{"contract": {}}', encoding='utf-8')
            self.assertEqual(cli._client([str(job_file), '--token-file', str(self.token())], True), 3)
        with patch.object(cli, 'request', return_value=(403, {})), redirect_stdout(io.StringIO()):
            self.assertEqual(cli._client(['GET', '/overview', '--token-file', str(self.token())], False), 2)


if __name__ == '__main__':
    unittest.main()
