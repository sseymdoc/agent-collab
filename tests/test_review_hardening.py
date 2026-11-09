"""公开仓库首轮代码评审发现的问题的回归测试（协议路径、manifest、快照、回写、blob、stub 契约）。"""
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_collab import artifacts as a, backup as b, controller as c, domain as d, protocol as p
from agent_collab.blobstore import BlobStore
from helpers import ControllerCase, contract, manifest, uid

ROOT = Path(__file__).resolve().parents[1]


def entry(path, data=b'x'):
    return {'path': path, 'sha256': p.sha256_bytes(data), 'size': len(data), 'executable': False}


class PathAndManifestTests(unittest.TestCase):
    def test_windows_unrepresentable_or_aliasing_names_rejected(self):
        for bad in ('foo.', 'foo ', 'dir./x', 'a:b', 'x?y', 'q"z', 'p|q', 'star*', 'CON', 'nul.txt', 'nul.tar.gz', 'con.a.b', 'a/LPT1', 'com3.log', 'tab\there'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                p.check_rel_path(bad)
        # Windows 工作区中，带盘符的路径段会被解析成盘符相对路径而逃出工作区。
        for bad in ('a/C:../C:../outside.txt', 'x/D:evil', 'C:evil'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                p.check_rel_path(bad)
        for ok in ('normal.txt', 'a.b/c', 'CONFIG.md', 'console', '.gitignore', 'com10', '__pycache__/x.pyc'):
            self.assertEqual(p.check_rel_path(ok), ok)

    def test_manifest_version_must_be_integer_one(self):
        for version in (True, 1.0, '1', 2):
            with self.subTest(version=version), self.assertRaises(ValueError):
                p.validate_manifest({'version': version, 'base': None, 'files': [], 'deleted': []})

    def test_file_directory_overlap_still_rejected_and_large_manifest_is_linear(self):
        with self.assertRaises(ValueError):
            p.validate_manifest({'version': 1, 'base': None, 'files': [entry('a'), entry('a/b')], 'deleted': []})
        files = [entry('dir%03d/file%04d.txt' % (i % 100, i)) for i in range(10000)]
        started = time.monotonic()
        p.validate_manifest({'version': 1, 'base': None, 'files': files, 'deleted': []})
        self.assertLess(time.monotonic() - started, 3.0)

    def test_manifest_diff_accepts_missing_new_manifest(self):
        old = {'version': 1, 'base': None, 'files': [entry('a')], 'deleted': []}
        self.assertEqual(p.manifest_diff(old, None), {'added': [], 'modified': [], 'deleted': ['a']})


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.dev/tmp').mkdir(parents=True, exist_ok=True)
        self.temp = Path(tempfile.mkdtemp(dir=str(ROOT / '.dev/tmp')))
        self.addCleanup(shutil.rmtree, str(self.temp), True)

    def test_snapshot_fails_instead_of_dropping_unreadable_subtree(self):
        work = self.temp / 'work'
        (work / 'sub').mkdir(parents=True)
        (work / 'sub/keep.txt').write_text('base')
        real_walk = os.walk

        def failing_walk(top, **kwargs):
            for item in real_walk(top, **kwargs):
                yield item
                kwargs['onerror'](PermissionError(13, 'Permission denied', str(work / 'sub')))

        with patch.object(a.os, 'walk', failing_walk), self.assertRaises(PermissionError):
            a.snapshot(work)

    def test_integrate_without_exclude_keeps_default_excluded_baseline_files(self):
        target, work = self.temp / 'target', self.temp / 'work'
        for root in (target, work):
            (root / '__pycache__').mkdir(parents=True)
            (root / 'main.py').write_text('v1')
            (root / '__pycache__/main.pyc').write_bytes(b'cache')
        base, blobs = a.snapshot(work, exclude=[])  # 基线含缓存文件
        (work / 'main.py').write_text('v2')
        result, new_blobs = a.snapshot(work, base)  # 缺省排除：结果不含缓存文件
        blobs.update(new_blobs)
        output = a.integrate(target, base, result, blobs.__getitem__, self.temp / 'backup')
        self.assertEqual(output['status'], 'integrated')
        self.assertEqual(output['changed'], ['main.py'])
        self.assertTrue((target / '__pycache__/main.pyc').exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink semantics')
    def test_workspace_under_symlinked_system_ancestor_is_allowed(self):
        real = self.temp / 'real'
        (real / 'ws').mkdir(parents=True)
        link = self.temp / 'link'
        os.symlink(str(real), str(link))
        self.assertEqual(a.safe_path(link / 'ws', 'f.txt'), real.resolve() / 'ws' / 'f.txt')
        os.symlink(str(real / 'ws'), str(self.temp / 'wslink'))
        with self.assertRaises(ValueError):
            a.safe_path(self.temp / 'wslink', 'f.txt')  # 工作区根本身是符号链接仍拒绝

    def test_blob_read_detects_at_rest_corruption(self):
        store = BlobStore(self.temp)
        data = b'payload'
        digest = p.sha256_bytes(data)
        store.put(digest, data)
        self.assertEqual(store.read(digest), data)
        store.path(digest).write_bytes(b'PAYLOAD')  # 同长度损坏
        with self.assertRaises(ValueError):
            store.read(digest)


    @unittest.skipIf(os.name == 'nt', 'POSIX executable bit')
    def test_local_executable_bit_change_is_a_conflict(self):
        target, work = self.temp / 'target', self.temp / 'work'
        for root in (target, work):
            root.mkdir()
            (root / 'run.sh').write_text('echo 1')
            os.chmod(str(root / 'run.sh'), 0o644)
        base, blobs = a.snapshot(work)
        (work / 'run.sh').write_text('echo 2')
        result, new_blobs = a.snapshot(work, base)
        blobs.update(new_blobs)
        os.chmod(str(target / 'run.sh'), 0o755)  # 用户在本地改了可执行位
        output = a.integrate(target, base, result, blobs.__getitem__, self.temp / 'backup')
        self.assertEqual((output['status'], output['conflicts']), ('conflict', ['run.sh']))
        self.assertEqual((target / 'run.sh').read_text(), 'echo 1')

    def test_interrupted_prune_never_leaves_a_visible_broken_backup_set(self):
        data, root = self.temp / 'ctl', self.temp / 'backups'
        c.App(data).close()
        for _ in range(2):
            b.run_local(data, root, keep=10, max_bytes=10 ** 9)
            time.sleep(0.01)
        oldest = b.list_complete(root)[0]['id']
        with patch.object(b.shutil, 'rmtree', side_effect=OSError('interrupted')), self.assertRaises(OSError):
            b.prune_local(root, keep=1, max_bytes=10 ** 9)
        self.assertNotIn(oldest, [item['id'] for item in b.list_complete(root)])  # 不抛错、残留不可见
        self.assertTrue((root / ('.tmp-prune-' + oldest)).exists())
        b.run_local(data, root, keep=1, max_bytes=10 ** 9)  # 下一轮回收残留
        self.assertFalse((root / ('.tmp-prune-' + oldest)).exists())


class ControllerHardeningTests(ControllerCase):
    def integration_contract(self):
        return contract(integration={'target_node_id': 'target', 'target_path': '/source'},
                        budgets={'max_attempts': 8, 'max_repairs': 7})

    def test_late_publication_of_older_revision_keeps_newer_pending(self):
        worker, target = self.node('a-worker'), self.node('target')  # 评分相同按节点 ID 排序，先派给 a-worker
        older = self.job(self.integration_contract())
        a1 = worker.claim()
        worker.upload(b'old')
        cp_old = worker.checkpoint(a1, manifest(b'old', name='output.txt'), kind='final')
        worker.complete(a1, checkpoint=cp_old)
        newer = self.job(self.integration_contract(), work_package_id=older['work_package_id'])
        a2 = worker.claim()
        worker.upload(b'new')
        cp_new = worker.checkpoint(a2, manifest(b'new', name='output.txt'), kind='final')
        worker.complete(a2, checkpoint=cp_new)
        worker.publish(a2, cp_new)
        worker.publish(a1, cp_old)  # 旧修订迟到的发布
        self.assertEqual([i['job_id'] for i in target.heartbeat()['integrations']], [newer['job_id']])
        self.assertEqual(self.current(older)['integration_status'], 'none')

    def test_retry_of_older_revision_rejected_while_newer_revision_is_active(self):
        worker = self.node('worker')
        older = self.job(contract(budgets={'max_attempts': 8, 'max_repairs': 7}))
        a1 = worker.claim()
        worker.upload(b'old')
        worker.complete(a1, checkpoint=worker.checkpoint(a1, manifest(b'old', name='output.txt'), kind='final'))
        self.job(contract(budgets={'max_attempts': 8, 'max_repairs': 7}), work_package_id=older['work_package_id'])
        result = self.mutate(older, 'retry', expected_status=409, reason='retry the older revision')
        self.assertIn('another revision', result['error']['message'])

    def events(self, count):
        return {'epoch': 1, 'lease_token': '', 'events': [
            {'event_id': uid(), 'type': 'attempt.progress', 'payload': {'done': 1, 'total': 2}} for _ in range(count)]}

    def test_events_for_unknown_attempts_need_a_restore_and_are_bounded(self):
        node = self.node('a')
        self.call('POST', '/attempts/' + uid() + '/events', self.events(1), token=node.token, status=404)
        meta = self.app.store.get('meta', 'controller')
        meta['authority_generation'] = 2  # 等价于恢复过备份
        self.app.store.put('meta', 'controller', meta)
        with patch.object(c, 'ARCHIVE_EVENT_LIMIT', 3):
            first, attempt = self.events(2), uid()
            self.call('POST', '/attempts/' + attempt + '/events', first, token=node.token)
            replay = self.call('POST', '/attempts/' + attempt + '/events', first, token=node.token)  # 响应丢失后的重发
            self.assertEqual(len(replay['duplicates']), 2)
            self.call('POST', '/attempts/' + uid() + '/events', self.events(1), token=node.token)  # 重发不占配额
            self.call('POST', '/attempts/' + uid() + '/events', self.events(1), token=node.token, status=413)


class StubAdmissionTests(unittest.TestCase):
    def contract(self, steps):
        return {'goal': 'g', 'harness': 'stub', 'harness_params': {'steps': steps}}

    def test_known_steps_admitted(self):
        d.validate_contract(self.contract([
            {'write': {'path': 'a.txt', 'content': 'x'}}, {'delete': {'path': 'a.txt'}}, {'sleep': 0.5},
            {'checkpoint': {'kind': 'boundary'}}, {'progress': {'done': 1, 'total': 2}},
            {'side_effect': {'op_id': 'op-1', 'state': 'intent'}}, {'run': ['{python}', '-c', 'pass']},
            {'fail': sorted(p.FAILURE_CLASSES)[0]}]))

    def test_malformed_or_unknown_steps_rejected(self):
        for step in ({'bogus': {}}, {'write': {'path': 'a.txt'}}, {'write': {'path': '../x', 'content': ''}},
                     {'sleep': -1}, {'sleep': True}, {'run': []}, {'run': 'echo hi'}, {'fail': 'not-a-class'},
                     {'side_effect': {'state': 'done'}}, {'side_effect': {'op_id': '', 'state': 'done'}},
                     {'side_effect': {'op_id': 'op', 'state': 'maybe'}}, {'write': {}, 'run': ['x']}, 'write'):
            with self.subTest(step=step), self.assertRaises(ValueError):
                d.validate_contract(self.contract([step]))



@unittest.skipIf(shutil.which('openssl') is None, 'needs openssl to create a throwaway certificate')
class DirectTlsTests(unittest.TestCase):
    """直连 TLS：一个只连不握手的客户端不能阻塞其他请求（握手在处理线程中进行并受超时约束）。"""

    def setUp(self):
        (ROOT / '.dev/tmp').mkdir(parents=True, exist_ok=True)
        self.temp = Path(tempfile.mkdtemp(dir=str(ROOT / '.dev/tmp')))
        self.addCleanup(shutil.rmtree, str(self.temp), True)

    def test_stalled_tls_client_does_not_block_other_requests(self):
        import socket, ssl, subprocess, sys, urllib.request
        key, cert = self.temp / 'key.pem', self.temp / 'cert.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert),
                        '-days', '1', '-subj', '/CN=localhost'], check=True, capture_output=True, timeout=60)
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        log = open(str(self.temp / 'server.log'), 'wb')
        self.addCleanup(log.close)
        server = subprocess.Popen([sys.executable, '-B', '-m', 'agent_collab.controller', 'serve', '--data-dir', str(self.temp / 'data'),
                                   '--bind', '127.0.0.1:%d' % port, '--tls-cert', str(cert), '--tls-key', str(key)],
                                  cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT)
        self.addCleanup(server.wait, 10)
        self.addCleanup(server.kill)
        context = ssl.create_default_context(cafile=str(cert))
        context.check_hostname = False
        url = 'https://127.0.0.1:%d/healthz' % port

        def health():
            with urllib.request.urlopen(url, context=context, timeout=5) as r:
                return r.status

        deadline = time.monotonic() + 60
        while True:
            try:
                self.assertEqual(health(), 200)
                break
            except OSError as exc:
                if server.poll() is not None or time.monotonic() > deadline:
                    self.fail('controller did not become healthy: %r exit=%s log=%s' % (
                        exc, server.poll(), (self.temp / 'server.log').read_text(errors='replace')[-3000:]))
                time.sleep(0.2)
        stalled = socket.create_connection(('127.0.0.1', port))  # 只建 TCP 连接，从不发送 ClientHello
        self.addCleanup(stalled.close)
        time.sleep(0.5)
        self.assertEqual(health(), 200)


if __name__ == '__main__':
    unittest.main()
