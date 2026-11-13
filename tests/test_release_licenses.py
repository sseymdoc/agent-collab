"""发行许可材料的完整性核对（scripts/rust_licenses.py）：缺一项就不通过。"""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rust_licenses', ROOT / 'scripts' / 'rust_licenses.py')
rl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rl)


class CompletenessTests(unittest.TestCase):
    def setUp(self):
        self.listed = {'a 1.0.0': 'MIT', 'b 2.0.0': 'Apache-2.0 OR MIT'}
        self.deps = {'a 1.0.0': {}, 'b 2.0.0': {}}
        self.texts = {'a 1.0.0': [('LICENSE', 'MIT text')], 'b 2.0.0': []}
        self.extra = {'b 2.0.0': {'files': [('LICENSE-MIT', 'MIT text b')]}}

    def test_complete_when_every_crate_has_a_text(self):
        self.assertEqual(rl.check(self.listed, self.deps, self.texts, self.extra), [])

    def test_missing_text_is_reported(self):
        problems = rl.check(self.listed, self.deps, self.texts, {})
        self.assertEqual(len(problems), 1)
        self.assertIn('b 2.0.0', problems[0])

    def test_explained_not_applicable_counts(self):
        self.assertEqual(rl.check(self.listed, self.deps, self.texts, {'b 2.0.0': {'files': [], 'not_applicable': '只在测试中使用'}}), [])

    def test_dependency_graph_and_table_must_agree(self):
        deps = dict(self.deps, **{'c 0.1.0': {}})
        del deps['a 1.0.0']
        problems = rl.check(self.listed, deps, self.texts, self.extra)
        self.assertTrue(any('c 0.1.0' in p and '表里没有' in p for p in problems), problems)
        self.assertTrue(any('a 1.0.0' in p and '依赖图里没有' in p for p in problems), problems)

    def test_supplement_file_must_match_recorded_digest(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        (folder / 'LICENSE-X').write_bytes(b'original\r\n')  # CRLF 检出与按 LF 记录的摘要一致
        manifest = {'files': {'LICENSE-X': {'sha256': hashlib.sha256(b'original\n').hexdigest(), 'source': 'x'}},
                    'crates': {'x 1.0.0': {'files': ['LICENSE-X']}, 'y 1.0.0': {'files': ['LICENSE-Y']}}}
        (folder / 'sources.json').write_text(json.dumps(manifest), encoding='utf-8')
        extra, problems = rl.supplement(folder)
        self.assertEqual(extra['x 1.0.0']['files'][0][0], 'LICENSE-X')
        self.assertTrue(any('y 1.0.0' in p for p in problems), problems)
        (folder / 'LICENSE-X').write_bytes(b'changed\n')
        _, problems = rl.supplement(folder)
        self.assertTrue(any('x 1.0.0' in p and 'SHA-256' in p for p in problems), problems)

    def test_repository_supplement_is_consistent(self):
        # 仓库里的补充文本都有出处记录且摘要相符。
        _, problems = rl.supplement()
        self.assertEqual(problems, [])


if __name__ == '__main__':
    unittest.main()
