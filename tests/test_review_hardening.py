"""公开仓库首轮代码评审发现的问题的回归测试（协议路径、manifest、快照、回写、blob、stub 契约）。"""
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_collab import artifacts as a, domain as d, protocol as p
from agent_collab.blobstore import BlobStore

ROOT = Path(__file__).resolve().parents[1]


def entry(path, data=b'x'):
    return {'path': path, 'sha256': p.sha256_bytes(data), 'size': len(data), 'executable': False}


class PathAndManifestTests(unittest.TestCase):
    def test_windows_unrepresentable_or_aliasing_names_rejected(self):
        for bad in ('foo.', 'foo ', 'dir./x', 'a:b', 'x?y', 'q"z', 'p|q', 'star*', 'CON', 'nul.txt', 'a/LPT1', 'com3.log', 'tab\there'):
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
                     {'side_effect': {'state': 'done'}}, {'write': {}, 'run': ['x']}, 'write'):
            with self.subTest(step=step), self.assertRaises(ValueError):
                d.validate_contract(self.contract([step]))


if __name__ == '__main__':
    unittest.main()
