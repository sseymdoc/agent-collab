"""本机多进程端到端：真实 HTTP controller + 子进程 node agent（stub harness）。

需要 loopback socket；不可用时跳过。运行数据放在 AGENT_COLLAB_TEST_TMP 或仓库 .dev/tmp（须为本机盘）。
场景：A 单节点闭环；B 源节点崩溃后另一节点从确认检查点承接；C 网络分区时旧 epoch 迟到发布被拒绝。
"""
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def sockets_ok():
    try:
        free_port()
        return True
    except OSError:
        return False


def sha(data):
    return hashlib.sha256(data).hexdigest()


@unittest.skipUnless(sockets_ok(), 'loopback socket 不可用')
class E2E(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get('AGENT_COLLAB_TEST_TMP') or ROOT / '.dev' / 'tmp')
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=str(base)))
        self.procs, self.logs = [], []
        self.addCleanup(self.cleanup)
        self.data = self.tmp / 'controller'
        self.data.mkdir()
        (self.data / 'config.json').write_text(json.dumps({
            'heartbeat_s': 1, 'suspect_after_s': 3, 'lease_ttl_s': 6, 'assign_claim_timeout_s': 5}), encoding='utf-8')
        self.port = free_port()
        self.url = 'http://127.0.0.1:%d' % self.port
        self.spawn('controller', [PY, '-m', 'agent_collab.controller', 'serve', '--data-dir', str(self.data),
                                  '--bind', '127.0.0.1:%d' % self.port])
        self.wait(lambda: self.raw('GET', '/healthz', auth=False)[0] == 200, 20, 'controller 未就绪')
        self.token = (self.data / 'admin.token').read_text(encoding='utf-8').strip()

    def cleanup(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait(10)
        for log in self.logs:
            log.close()
        if os.environ.get('KEEP_E2E'):
            print('\n[kept]', self.tmp)
        else:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def spawn(self, name, argv, env=None):
        log = open(self.tmp / (name + '.log'), 'ab')
        self.logs.append(log)
        p = subprocess.Popen(argv, cwd=str(ROOT), stdout=log, stderr=log, env=dict(os.environ, **(env or {})))
        self.procs.append(p)
        return p

    def raw(self, method, path, body=None, auth=True, data=None, token=None):
        headers = {'Accept': 'application/json'}
        if auth:
            headers['Authorization'] = 'Bearer ' + (token or self.token)
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(self.url + ('' if path == '/healthz' else '/api/v1') + path, data=data,
                                     method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw and r.headers.get_content_type() == 'application/json' else raw)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw
        except (urllib.error.URLError, OSError):
            return 0, None

    def api(self, method, path, body=None, expect=(200, 201)):
        status, out = self.raw(method, path, body)
        self.assertIn(status, expect, (method, path, out))
        return out

    def wait(self, cond, timeout, message):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = cond()
            if value:
                return value
            time.sleep(.2)
        self.fail(message)

    def agent(self, node_id, host, domain, extra=(), env=None):
        d = self.tmp / ('agent-' + node_id)
        out = subprocess.run([PY, '-m', 'agent_collab.controller', 'pairing-code', '--data-dir', str(self.data),
                              '--node-id', node_id], cwd=str(ROOT), capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        text = out.stdout.strip()
        try:
            code = json.loads(text)['code']
        except (ValueError, KeyError, TypeError):
            code = text.split()[-1]
        r = subprocess.run([PY, '-m', 'agent_collab.node_agent', 'pair', '--controller', self.url, '--code', code,
                            '--node-id', node_id, '--data-dir', str(d), '--physical-host-id', host,
                            '--failure-domain', domain, '--slots', '1', '--host-slots', '1'],
                           cwd=str(ROOT), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return d, self.start_agent(node_id, d, extra, env)

    def start_agent(self, node_id, d, extra=(), env=None):
        config_path = d / 'agent.json'
        config = json.loads(config_path.read_text(encoding='utf-8'))
        config['disabled_harnesses'] = {'codex': 'stub-only e2e', 'claude': 'stub-only e2e'}
        config_path.write_text(json.dumps(config), encoding='utf-8')
        return self.spawn('agent-' + node_id, [PY, '-m', 'agent_collab.node_agent', 'run', '--data-dir', str(d),
                                               '--foreground'] + list(extra), env)

    def put_inputs(self, files):
        entries = []
        for path, content in files.items():
            data = content.encode()
            status, out = self.raw('PUT', '/blobs/' + sha(data), data=data)
            self.assertIn(status, (200, 201), out)
            entries.append({'path': path, 'sha256': sha(data), 'size': len(data), 'executable': False})
        return {'version': 1, 'base': None, 'files': entries, 'deleted': []}

    def submit(self, steps, inputs=None, auto=True, title='e2e'):
        contract = {
            'goal': title, 'acceptance': ['stub 步骤完成'], 'excludes': [], 'harness': 'stub',
            'harness_params': {'steps': steps}, 'prompt': '',
            'requirements': {'os': [], 'arch': [], 'tools': [], 'gpu': None, 'labels': []},
            'inputs_manifest': inputs, 'recovery': {'auto': auto, 'side_effects': 'none'},
            'budgets': {'max_attempts': 3, 'max_migrations': 2, 'max_repairs': 2, 'deadline_s': 600},
            'resources': {'slots': 1}, 'publish': {'mode': 'auto', 'requires_acceptance': False},
            'integration': {'target_node_id': None, 'target_path': None}}
        out = self.api('POST', '/jobs', {'request_id': str(uuid.uuid4()), 'title': title, 'contract': contract})
        return (out.get('job') or out)['job_id']

    def job(self, job_id):
        return self.api('GET', '/jobs/' + job_id)

    def delivered_files(self, detail):
        job = detail.get('job') or detail
        cp = next(c for c in detail['checkpoints'] if c['checkpoint_id'] == job['delivery_checkpoint_id'])
        status, manifest = self.raw('GET', '/blobs/' + cp['manifest_sha256'])
        manifest = json.loads(manifest) if isinstance(manifest, bytes) else manifest
        out = {}
        for f in manifest['files']:
            status, data = self.raw('GET', '/blobs/' + f['sha256'])
            out[f['path']] = data if isinstance(data, bytes) else json.dumps(data).encode()
        return out

    def test_a_single_node_closed_loop(self):
        self.agent('n1', 'h1', 'd1')
        inputs = self.put_inputs({'input.txt': 'hello\n'})
        job_id = self.submit([
            {'run': [PY, '-c', "open('upper.txt','wb').write(open('input.txt','rb').read().upper())"]},
            {'checkpoint': {'kind': 'boundary'}},
            {'write': {'path': 'done.txt', 'content': 'ok\n'}},
        ], inputs)
        detail = self.wait(lambda: (lambda d: d if (d.get('job') or d)['delivery_version'] == 1 else None)(self.job(job_id)),
                           60, '单节点闭环未发布')
        files = self.delivered_files(detail)
        self.assertEqual(files['upper.txt'], b'HELLO\n')
        self.assertEqual(files['done.txt'], b'ok\n')
        self.assertEqual(files['input.txt'], b'hello\n')
        types = [e['type'] for e in detail['events']]
        self.assertIn('checkpoint.durable', types)

    def test_b_failover_from_durable_checkpoint(self):
        d1, p1 = self.agent('n1', 'h1', 'd1', ['--fault-exit-after-checkpoint', '1'])
        inputs = self.put_inputs({'input.txt': 'x'})
        job_id = self.submit([
            {'run': ['{python}', '-c', "open('count.txt','a').write('1')"]},
            {'checkpoint': {'kind': 'boundary'}},
            {'sleep': 1},
            {'write': {'path': 'second.txt', 'content': 'after recovery\n'}},
        ], inputs)
        self.wait(lambda: p1.poll() is not None, 60, 'n1 未在第一个 durable 检查点后退出')
        self.assertEqual(p1.returncode, 137)
        crashed_at = time.time()
        self.agent('n2', 'h2', 'd2')
        detail = self.wait(lambda: (lambda d: d if (d.get('job') or d)['delivery_version'] == 1 else None)(self.job(job_id)),
                           90, '未在另一节点承接并发布')
        recovered_s = time.time() - crashed_at
        job = detail.get('job') or detail
        attempts = sorted(detail['attempts'], key=lambda a: a['epoch'])
        self.assertEqual([a['node_id'] for a in attempts], ['n1', 'n2'])
        self.assertEqual(attempts[1]['recovered_from_attempt'], attempts[0]['attempt_id'])
        first_cp = [c for c in detail['checkpoints'] if c['attempt_id'] == attempts[0]['attempt_id'] and c['state'] == 'durable']
        self.assertEqual(attempts[1]['start_checkpoint_id'], first_cp[0]['checkpoint_id'])
        self.assertEqual(job['work_package_id'], job['job_id'])
        files = self.delivered_files(detail)
        # 第 0 步只执行过一次：n2 从检查点继续而不是重做。
        self.assertEqual(files['count.txt'], b'1')
        self.assertEqual(files['second.txt'], b'after recovery\n')
        print('\n[e2e] 崩溃到新节点发布耗时 %.1fs（lease_ttl_s=6）' % recovered_s)

    def test_c_partition_old_epoch_publish_rejected(self):
        part = self.tmp / 'partition-n1'
        d1, p1 = self.agent('n1', 'h1', 'd1', env={'AGENT_COLLAB_FAULT_PARTITION': str(part)})
        inputs = self.put_inputs({'input.txt': 'x'})
        job_id = self.submit([
            {'write': {'path': 'a.txt', 'content': 'a\n'}},
            {'checkpoint': {'kind': 'boundary'}},
            {'sleep': 12},
            {'write': {'path': 'b.txt', 'content': 'from-old\n'}},
        ], inputs)
        self.wait(lambda: (self.job(job_id).get('job') or self.job(job_id))['durable_checkpoint_id'], 60, '无 durable 检查点')
        part.write_text('on')
        self.agent('n2', 'h2', 'd2')
        detail = self.wait(lambda: (lambda d: d if (d.get('job') or d)['delivery_version'] == 1 else None)(self.job(job_id)),
                           90, '分区后未在 n2 承接发布')
        attempts = sorted(detail['attempts'], key=lambda a: a['epoch'])
        self.assertEqual(attempts[-1]['node_id'], 'n2')
        old = attempts[0]
        # 旧节点仍活着并在自己的独立目录继续。
        self.assertIsNone(p1.poll())
        self.assertTrue((d1 / 'attempts' / old['attempt_id'] / 'work').is_dir())
        part.unlink()
        # 旧 attempt 用其仍持有的执行权尝试迟到发布：必须被拒绝。
        lease = (d1 / 'attempts' / old['attempt_id'] / 'lease.token')
        self.assertTrue(lease.exists(), '旧 attempt 未持久化租约令牌')
        old_cp = next(c for c in detail['checkpoints'] if c['attempt_id'] == old['attempt_id'])
        # 以旧节点自己的节点令牌 + 旧租约令牌发起发布，等同旧 worker 在分区恢复后迟到发布。
        status, out = self.raw('POST', '/jobs/' + job_id + '/publish', {
            'request_id': str(uuid.uuid4()), 'attempt_id': old['attempt_id'], 'epoch': old['epoch'],
            'lease_token': lease.read_text().strip(), 'checkpoint_id': old_cp['checkpoint_id'],
            'expected_base_version': 0, 'authority_generation': old['authority_generation']},
            token=(d1 / 'node.token').read_text().strip())
        self.assertEqual(status, 409, out)
        self.assertEqual(out['error']['code'], 'fenced')
        self.wait(lambda: any(e.get('stale') for e in self.job(job_id)['events']) or
                  (d1 / 'attempts' / old['attempt_id'] / 'quarantined.json').exists(), 60, '旧节点回归后未隔离')
        final = self.job(job_id)
        self.assertEqual((final.get('job') or final)['delivery_version'], 1)
        self.assertEqual([p['attempt_id'] for p in final['publications']], [attempts[-1]['attempt_id']])


if __name__ == '__main__':
    unittest.main()
