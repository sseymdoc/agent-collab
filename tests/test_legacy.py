import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agent_collab import legacy

ROOT = Path(__file__).resolve().parents[1]


def tree_hash(root):
    h = hashlib.sha256()
    for p in sorted(Path(root).rglob('*')):
        rel = str(p.relative_to(root)).encode()
        # 目录也计入（带类型标记）：删掉空目录、把文件换成目录都会改变摘要。
        if p.is_dir():
            h.update(b'D\0' + rel + b'\0')
        elif p.is_file():
            h.update(b'F\0' + rel + b'\0' + p.read_bytes() + b'\0')
    return h.hexdigest()


class LegacyProjectionTest(unittest.TestCase):
    def setUp(self):
        (ROOT / '.dev' / 'tmp').mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=ROOT / '.dev' / 'tmp'))
        self.addCleanup(shutil.rmtree, self.root)
        t = self.root / 'tasks' / 'mac' / '5cb86daf-bcf1-44c2-9a68-8849447476e1'
        t.mkdir(parents=True)
        (t / 'state.json').write_text(json.dumps({
            'task_id': '5cb86daf-bcf1-44c2-9a68-8849447476e1', 'worker': 'mac', 'status': 'running',
            'session_id': '01a1bdf5-0911-73f1-a732-626d7a4eb698', 'request_hash': 'abc', 'created_at': '2026-11-09T01:00:00+00:00',
            'workflow_version': 2, 'observed': {'model': 'gpt-6-astra', 'effort': 'xhigh'}}), encoding='utf-8')
        (t / 'assessment.json').write_text(json.dumps({'status': 'passed'}), encoding='utf-8')
        (self.root / 'tasks' / 'mac' / 'junk').mkdir()

    def test_projection_is_read_only_and_tagged(self):
        before = tree_hash(self.root)
        rows = legacy.read_tasks(self.root)
        self.assertEqual(tree_hash(self.root), before)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r['source'], r['read_only'], r['authority']), ('codex-collaborate', True, 'legacy_runner'))
        self.assertEqual(r['request_hash'], 'abc')
        self.assertEqual(r['work_package_id'], r['task_id'])
        # 登记为 running 不等于存活，更不是接管依据。
        self.assertEqual(r['recorded_status'], 'running')
        self.assertEqual(r['liveness'], 'check_with_original_launcher')
        self.assertEqual(r['recorded_acceptance'], 'passed')

    def test_non_object_records_do_not_break_listing(self):
        t = self.root / 'tasks' / 'mac' / '5cb86daf-bcf1-44c2-9a68-8849447476e1'
        (t / 'report.json').write_text('[1, 2]', encoding='utf-8')
        (t / 'assessment.json').write_text('"passed"', encoding='utf-8')
        state = json.loads((t / 'state.json').read_text(encoding='utf-8'))
        state['observed'] = ['gpt']
        (t / 'state.json').write_text(json.dumps(state), encoding='utf-8')
        r = legacy.read_tasks(self.root)[0]
        self.assertEqual(r['recorded_acceptance'], 'unverified')
        self.assertIsNone(r['report_outcome'])
        self.assertEqual(r['observed'], {'model': None, 'effort': None, 'reasoning_effort': None})

    def test_missing_state_root_is_an_error_not_an_empty_list(self):
        missing = self.root / 'missing'
        with self.assertRaises(ValueError):
            legacy.read_tasks(missing)
        with self.assertRaises(SystemExit) as raised:
            legacy.main(['list', '--state-root', str(missing)])
        self.assertEqual(raised.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
