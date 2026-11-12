"""任务提交的跨进程幂等：controller 已受理但应答丢失 → CLI 进程退出 → 默认模型变化 → 另一个进程恢复同一次提交。

真实 HTTP controller，每条 CLI 命令是一个新进程；进程之间只共享配置目录里的请求回执。
应答丢失由 loopback 上的转发代理制造：请求照常送到 controller，应答不回给客户端。
"""
import json
import os
import socket
import subprocess
import threading
import unittest
import uuid

import test_e2e_local as e2e

PY, ROOT = e2e.PY, e2e.ROOT


class LossyProxy:
    """开启 drop 后，请求行以 match 开头的请求（缺省 POST /jobs）照常转发，但应答不回给客户端。"""
    def __init__(self, upstream_port, match=b'POST /api/v1/jobs '):
        self.upstream, self.drop, self.dropped, self.match = upstream_port, False, 0, match
        self.forward = True  # False：匹配的请求根本不转发（controller 不可达），而不只是丢应答
        self.server = socket.socket()
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(16)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.relay, args=(client,), daemon=True).start()

    def relay(self, client):
        try:
            client.settimeout(20)
            data = b''
            while b'\r\n\r\n' not in data:
                chunk = client.recv(65536)
                if not chunk:
                    return
                data += chunk
            head, _, body = data.partition(b'\r\n\r\n')
            length = next((int(line.split(b':')[1]) for line in head.split(b'\r\n') if line.lower().startswith(b'content-length:')), 0)
            while len(body) < length:
                body += client.recv(65536)
            if self.drop and not self.forward and head.startswith(self.match):
                self.dropped += 1
                return
            with socket.create_connection(('127.0.0.1', self.upstream), timeout=20) as upstream:
                upstream.sendall(head + b'\r\n\r\n' + body)
                answer = b''
                while True:
                    chunk = upstream.recv(65536)
                    if not chunk:
                        break
                    answer += chunk
            if self.drop and head.startswith(self.match):
                self.dropped += 1  # controller 已处理完，客户端收不到应答
            else:
                client.sendall(answer)
        except OSError:
            pass
        finally:
            client.close()

    def close(self):
        self.server.close()


@unittest.skipUnless(e2e.sockets_ok(), 'loopback socket 不可用')
class SubmitReceiptTests(unittest.TestCase):
    setUp, cleanup, spawn, raw, api, wait = e2e.E2E.setUp, e2e.E2E.cleanup, e2e.E2E.spawn, e2e.E2E.raw, e2e.E2E.api, e2e.E2E.wait

    def run_cli(self, *argv):
        env = {k: v for k, v in os.environ.items() if not k.startswith('AGENT_COLLAB_')}
        env.update(AGENT_COLLAB_HOME=str(self.tmp / 'cli-home'), PYTHONIOENCODING='utf-8')
        done = subprocess.run([PY, '-B', '-m', 'agent_collab.cli'] + list(argv), cwd=str(ROOT), env=env,
                              capture_output=True, text=True, encoding='utf-8', timeout=120)
        text = done.stdout.strip() or done.stderr.strip()
        try:
            return done.returncode, json.loads(text)
        except ValueError:
            return done.returncode, text

    def client_code(self):
        result = subprocess.run([PY, '-m', 'agent_collab.controller', 'client-code', '--data-dir', str(self.data), '--role', 'owner'],
                                cwd=str(ROOT), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)['code']

    def set_default(self, model):
        current = self.api('GET', '/settings/models')['settings']
        self.api('PUT', '/settings/models', {'request_id': str(uuid.uuid4()), 'expected_revision': current['revision'],
                                             'codex': {'default': {'model': model, 'effort': 'high'}, 'allowed': None}})

    def pending(self):
        return [r['request_id'] for r in self.run_cli('submit', '--pending')[1]['pending']]

    def test_lost_response_is_resumed_by_another_process_with_the_original_job_and_snapshot(self):
        proxy = LossyProxy(self.port)
        self.addCleanup(proxy.close)
        via = 'http://127.0.0.1:%d' % proxy.port
        self.set_default('model-a')
        self.assertEqual(self.run_cli('client', 'pair', '--url', via, '--code', self.client_code(), '--name', 'automation',
                                      '--credential-store', 'file')[0], 0)
        submit = ('submit', '--template', 'codex-change', '--goal', '同一次提交')

        # 受理了，但应答没有回来：进程以连接错误结束，输出恢复方法，回执留在本机。
        proxy.drop = True
        code, lost = self.run_cli(*submit)
        proxy.drop = False
        self.assertEqual((code, proxy.dropped, lost['error']['code']), (3, 1, 'network'), lost)
        request_id = lost['request_id']
        self.assertEqual(lost['resume'], 'agent-collab submit --resume ' + request_id)
        jobs = self.api('GET', '/jobs')['jobs']
        self.assertEqual((len(jobs), jobs[0]['request_id'], jobs[0]['model_policy']['resolved']['model']), (1, request_id, 'model-a'))
        self.assertEqual(self.pending(), [request_id])

        # 期间默认模型变了。带着同一个 request_id 重跑整条命令不是恢复：被本机拒绝，也没有新任务。
        self.set_default('model-b')
        code, refused = self.run_cli(*submit, '--request-id', request_id)
        self.assertEqual(code, 1, refused)
        self.assertEqual(len(self.api('GET', '/jobs')['jobs']), 1)

        # 另一个进程恢复：得到原来的任务和它受理时冻结的快照，没有第二个任务。
        code, resumed = self.run_cli('submit', '--resume', request_id)
        self.assertEqual((code, resumed['http_status'], resumed['replayed']), (0, 200, True), resumed)
        self.assertEqual((resumed['body']['job_id'], resumed['body']['model_policy']['resolved']['model'],
                          resumed['body']['model_policy']['source']['model']), (jobs[0]['job_id'], 'model-a', 'workspace_default'))
        self.assertEqual(len(self.api('GET', '/jobs')['jobs']), 1)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.run_cli('submit', '--resume', request_id)[0], 2)  # 回执已清除，不能再重放

        # 用户有意再提交一次同样的命令：新的 request_id、新的任务，按当前默认值解析。
        code, again = self.run_cli(*submit)
        self.assertEqual((code, again['http_status']), (0, 201), again)
        self.assertNotEqual(again['body']['job_id'], jobs[0]['job_id'])
        self.assertNotEqual(again['request_id'], request_id)
        self.assertEqual(again['body']['model_policy']['resolved']['model'], 'model-b')
        self.assertEqual(len(self.api('GET', '/jobs')['jobs']), 2)

        # 重放同样先过当前授权：凭据被撤销后恢复得到 401，回执保留，没有借旧回执绕过撤销。
        proxy.drop = True
        code, lost = self.run_cli(*submit)
        proxy.drop = False
        self.assertEqual(code, 3)
        client = next(c for c in self.api('GET', '/clients')['clients'] if c['name'] == 'automation')
        self.api('POST', '/clients/%s/revoke' % client['client_id'], {'request_id': str(uuid.uuid4()), 'expected_version': client['version']})
        code, denied = self.run_cli('submit', '--resume', lost['request_id'])
        self.assertEqual((code, denied['http_status']), (3, 401), denied)
        self.assertEqual(self.pending(), [lost['request_id']])

    def test_never_accepted_submission_is_refused_on_resume_once_settings_moved(self):
        """请求根本没有到达 controller 时，恢复才是第一次受理；设置已变则被明确拒绝，不按没看过的默认值悄悄受理。"""
        self.set_default('model-a')
        self.assertEqual(self.run_cli('client', 'pair', '--url', self.url, '--code', self.client_code(), '--credential-store', 'file')[0], 0)
        drafted = self.run_cli('submit', '--template', 'codex-change', '--goal', '未送达', '--dry-run')[1]
        request_id = str(uuid.uuid4())
        receipt = self.tmp / 'cli-home' / 'requests' / (request_id + '.json')
        receipt.parent.mkdir(parents=True)
        profile = json.loads((self.tmp / 'cli-home' / 'clients' / 'default.json').read_text(encoding='utf-8'))
        receipt.write_text(json.dumps({'request_id': request_id, 'controller_id': profile['controller_id'], 'url': self.url,
                                       'created_at': '2026-01-01T00:00:00Z',
                                       'body': {'request_id': request_id, 'title': drafted['title'], 'contract': drafted['contract'],
                                                'expected_settings_revision': drafted['settings_revision']}}), encoding='utf-8')
        self.set_default('model-b')
        code, answer = self.run_cli('submit', '--resume', request_id)
        self.assertEqual((code, answer['http_status'], answer['body']['error']['code']), (2, 409, 'version_mismatch'), answer)
        self.assertEqual((self.api('GET', '/jobs')['jobs'], self.pending()), ([], []))


if __name__ == '__main__':
    unittest.main()
