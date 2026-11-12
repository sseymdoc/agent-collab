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
        value = self.routes[path]
        if isinstance(value, list):  # 依次给出的应答；其中的异常表示这一次不可达
            value = value.pop(0) if len(value) > 1 else value[0]
        if isinstance(value, Exception):
            raise value
        return value


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
                     Response([b'id: 7\n', b'event: x\n', b'data: {"seq": 7}\n', b'\n']),
                     Response([b'event: snapshot_required\n', b'data: {"min_seq": 9}\n', b'\n']),
                     Response([])]

        def fake_open(url, token, method, path, *args, **kwargs):
            opened.append(path)
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        conn = FakeConn({'/events?after=4&limit=1000': {'events': [{'seq': 5}, {'seq': 6}]},
                         '/events?after=8&limit=1000': {'events': []}})
        conn.url = conn.token = conn.ca_file = conn.controller_id = None
        with patch.object(cli, '_open', fake_open), patch.object(cli.time, 'sleep'):
            events = cli.stream(conn, 3)
            seen = [next(events) for _ in range(9)]
            next(events)  # 再连一次，看续流用的游标
        # 先产出 SNAPSHOT 让调用方刷新状态，再补读；第 5 条仍保留的事件没有丢。
        self.assertEqual(seen, [None, cli.SNAPSHOT, {'seq': 5}, {'seq': 6}, None, {'seq': 7}, None, cli.SNAPSHOT, None])
        # 补读为空（事件表已清空）时从 min_seq-1 续流，下一条序号为 min_seq 的新事件不会被跳过。
        self.assertEqual(opened, ['/events/stream?cursor=%d' % c for c in (3, 3, 6, 7, 8)])

    def follow(self, events, states, timeout=3600):
        conn = FakeConn({'/jobs/j1': job('running'), '/jobs/j1/events?limit=1000': {'events': [{'seq': 1, 'type': 'job.created', 'job_id': 'j1'}]},
                         '/jobs?limit=1000&work_package_id=wp': {'jobs': [{'job_id': 'j2'}, {'job_id': 'j1'}]}})
        running = {'job_id': 'j1', 'outcome': 'running', 'settled': False, 'coordination': []}
        states = [running if s == 'running' else s for s in states]
        with patch.object(cli, 'stream', lambda conn, cursor: iter(events)), patch.object(cli, 'settled_state', side_effect=states), \
                redirect_stdout(io.StringIO()) as printed:
            code = cli.follow(conn, 'j1', timeout)
        return code, [json.loads(line) for line in printed.getvalue().splitlines()]

    def test_stream_keeps_its_cursor_until_the_backlog_is_read(self):
        opened = []
        snapshot = [b'event: snapshot_required\n', b'data: {"min_seq": 5}\n', b'\n']
        responses = [Response(snapshot), Response(snapshot), Response([])]

        def fake_open(url, token, method, path, *args, **kwargs):
            opened.append(path)
            return responses.pop(0)
        conn = FakeConn({'/events?after=4&limit=1000': [cli.Unreachable(), {'events': [{'seq': 5}]}]})
        conn.url = conn.token = conn.ca_file = conn.controller_id = None
        with patch.object(cli, '_open', fake_open), patch.object(cli.time, 'sleep'):
            events = cli.stream(conn, 3)
            seen = [next(events) for _ in range(5)]
            next(events)
        self.assertEqual(seen, [cli.SNAPSHOT, None, cli.SNAPSHOT, {'seq': 5}, None])
        self.assertEqual(opened, ['/events/stream?cursor=%d' % c for c in (3, 3, 5)])  # 补读失败时游标不动

    def test_follow_retries_its_first_requests_while_the_controller_restarts(self):
        done = {'job_id': 'j1', 'outcome': 'done', 'settled': True, 'coordination': []}
        conn = FakeConn({'/jobs/j1': [cli.Unreachable(), job('running')],
                         '/jobs/j1/events?limit=1000': [cli.Unreachable(), {'events': [{'seq': 1, 'type': 'job.created', 'job_id': 'j1'}]}]})
        with patch.object(cli, 'settled_state', side_effect=[cli.Unreachable(), done]), patch.object(cli.time, 'sleep'), \
                redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(cli.follow(conn, 'j1', 3600), 0)
        self.assertEqual(json.loads(printed.getvalue().splitlines()[-1])['summary']['outcome'], 'done')

    def test_follow_times_out_with_the_last_summary_while_the_controller_is_unreachable(self):
        code, lines = self.follow([None], ['running', cli.Unreachable()], timeout=0)
        self.assertEqual((code, lines[-1]['summary']['outcome'], lines[-1]['summary']['timed_out']), (4, 'running', True))

    def test_follow_refreshes_tracked_revisions_after_snapshot_required(self):
        # 修复修订 j2 的 job.created 已被清理：刷新快照后仍能认出它的事件。
        done = {'job_id': 'j2', 'outcome': 'done', 'settled': True, 'coordination': []}
        code, lines = self.follow([cli.SNAPSHOT, {'seq': 9, 'type': 'attempt.progress', 'job_id': 'j2'}], ['running', 'running', done])
        self.assertEqual((code, [l.get('job_id') for l in lines[:-1]], lines[-1]['summary']['job_id']), (0, ['j1', 'j2'], 'j2'))

    @unittest.skipUnless(os.name == 'nt', 'Windows ACL')
    def test_client_credential_file_has_no_inherited_access_on_windows(self):
        import subprocess
        from agent_collab import artifacts
        path = self.temp / 'cred.json'
        with artifacts.SecretFile(path) as secret:
            secret.save(b'{}')
        listing = subprocess.run([artifacts._system_tool('icacls.exe'), str(path)], capture_output=True, text=True, errors='replace', check=True).stdout
        self.assertNotIn('(I)', listing)  # 继承来的 ACE 已去掉
        self.assertEqual([n.casefold() for n in artifacts._principals(path)], [artifacts._current_user()[0].casefold()])
        self.assertEqual(path.read_bytes(), b'{}')

    @unittest.skipUnless(os.name == 'nt', 'Windows ACL')
    def test_explicit_access_for_other_principals_is_removed_on_windows(self):
        # 父目录没有可继承 ACE 时，新文件会带上显式 ACE；/inheritance:r 去不掉它们，要逐个移除再核对。
        import subprocess
        from agent_collab import artifacts
        path = self.temp / 'explicit.txt'
        path.write_bytes(b'')
        for grant in (['/grant', '*S-1-5-32-545:R'], ['/deny', '*S-1-1-0:W']):  # Users 显式授权、Everyone 显式拒绝
            subprocess.run([artifacts._system_tool('icacls.exe'), str(path)] + grant, capture_output=True, check=True)
        self.assertGreater(len(artifacts._principals(path)), 1)
        artifacts._current_user.cache_clear()  # 让 whoami 也经过下面的检查
        with patch('subprocess.run', wraps=subprocess.run) as run:
            artifacts.owner_only(path)
        self.assertEqual([n.casefold() for n in artifacts._principals(path)], [artifacts._current_user()[0].casefold()])
        # 凭据文件可能由无控制台的 pythonw 服务或桌面端写入：whoami/icacls 缺 CREATE_NO_WINDOW 会各闪一个控制台窗口。
        self.assertIn('/remove', [a for c in run.call_args_list for a in c[0][0]])
        self.assertEqual({c[1].get('creationflags') for c in run.call_args_list}, {subprocess.CREATE_NO_WINDOW})

    @unittest.skipUnless(os.name == 'nt', 'Windows ACL')
    def test_atomic_credential_write_leaves_only_the_current_user_on_windows(self):
        # 端到端：目录没有可继承 ACE 时，临时文件带着令牌默认 DACL 的显式 ACE（SYSTEM、Administrators 等）创建；
        # 经 atomic_write 写入并改名后，最终文件的完整 DACL 只允许当前用户。
        import subprocess
        from agent_collab import artifacts
        name, sid = artifacts._current_user()
        folder = self.temp / 'no-inherit'
        folder.mkdir()
        subprocess.run([artifacts._system_tool('icacls.exe'), str(folder), '/inheritance:r', '/grant:r', '*%s:F' % sid], capture_output=True, check=True)
        probe = folder / 'probe'
        probe.write_bytes(b'')
        if not any(n.casefold() != name.casefold() for n in artifacts._principals(probe)):
            self.skipTest('这台机器上新文件的默认 DACL 只有当前用户，构造不出要验证的情况')
        path = folder / 'cred.json'
        with artifacts.SecretFile(path) as secret:
            secret.save(b'{"token": "x"}')
        self.assertEqual([n.casefold() for n in artifacts._principals(path)], [name.casefold()])
        self.assertEqual(path.read_bytes(), b'{"token": "x"}')
        self.assertEqual([x.name for x in folder.iterdir() if x.name.endswith('.tmp')], [])

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
        try:
            server = HTTPServer(('127.0.0.1', 0), Handler)
        except OSError as exc:
            self.skipTest('loopback socket 不可用：%s' % exc)
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
        # 与 client pair 相同的写法：文件后端的凭据文件只允许当前用户访问，读取前会核对。
        from agent_collab import artifacts
        home = self.temp / 'home'
        with artifacts.SecretFile(home / 'clients' / 'default.json') as secret:
            secret.save(json.dumps({'url': 'https://paired.example', 'token': 'client-secret',
                                    'controller_id': CONTROLLER, 'ca_file': None}).encode('utf-8'))
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

    def test_pair_restricts_the_credential_file_before_spending_the_code(self):
        home, clients = self.temp / 'pair-home', self.temp / 'pair-home' / 'clients'
        argv = ['client', 'pair', '--url', 'https://paired.example', '--code', 'one-time', '--credential-store', 'file']
        with patch.dict(os.environ, {'AGENT_COLLAB_HOME': str(home)}):
            # 收紧失败：配对请求根本没有发出，配对码还能用，也不留下文件。
            with patch('agent_collab.artifacts.owner_only', side_effect=OSError('icacls failed')),                     patch.object(cli, 'request') as sent, self.assertRaises(SystemExit):
                cli.main(argv)
            sent.assert_not_called()
            self.assertEqual(list(clients.iterdir()) if clients.exists() else [], [])
            # 收紧成功：凭据写进预留的文件，不留临时文件。
            body = {'client_id': 'c1', 'name': 'automation', 'kind': 'cli', 'scopes': ['read'], 'controller_id': CONTROLLER, 'client_token': 'secret'}
            with patch.object(cli, 'request', return_value=(200, body)), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(argv), 0)
        self.assertEqual([x.name for x in clients.iterdir()], ['default.json'])
        self.assertEqual(json.loads((clients / 'default.json').read_text(encoding='utf-8'))['token'], 'secret')

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
        # 提交会在配置目录里留下请求回执：指到临时目录，不写进运行测试的用户的真实配置。
        with patch.dict(os.environ, {'AGENT_COLLAB_HOME': str(self.temp / 'receipts-home')}), \
                patch.object(cli, 'request', return_value=(401, {})), redirect_stdout(io.StringIO()):
            self.assertEqual(cli._client(['GET', '/overview', '--token-file', str(self.token())], False), 3)
            job_file = self.temp / 'job.json'
            job_file.write_text('{"contract": {}}', encoding='utf-8')
            self.assertEqual(cli._client([str(job_file), '--token-file', str(self.token())], True), 3)
        with patch.object(cli, 'request', return_value=(403, {})), redirect_stdout(io.StringIO()):
            self.assertEqual(cli._client(['GET', '/overview', '--token-file', str(self.token())], False), 2)


if __name__ == '__main__':
    unittest.main()
