"""ui/build.mjs 的构建锁：锁已存在时不接管、不清理，直接失败。"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')


@unittest.skipUnless(NODE, 'node is not installed')
class BuildLockTests(unittest.TestCase):
    def test_existing_lock_makes_concurrent_builds_exit_without_touching_lock_or_static(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'ui').mkdir()
            shutil.copy2(str(ROOT / 'ui' / 'build.mjs'), str(root / 'ui' / 'build.mjs'))
            static = root / 'agent_collab' / 'static'
            static.mkdir(parents=True)
            (static / 'index.html').write_text('<html>old</html>')
            lock = root / 'ui' / 'dist.lock'
            lock.mkdir()
            (lock / 'marker').write_text('stale')
            # 两个构建同时遇到残留锁：都应在获取锁时失败退出（此时还未复制依赖，不需要 node_modules）。
            runs = [subprocess.Popen([NODE, 'build.mjs'], cwd=str(root / 'ui'), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    for _ in range(2)]
            for run in runs:
                out, err = run.communicate(timeout=60)
                self.assertNotEqual(run.returncode, 0)
                self.assertIn('dist.lock', (out + err).decode('utf-8', 'replace'))
            self.assertEqual((lock / 'marker').read_text(), 'stale')
            self.assertEqual((static / 'index.html').read_text(), '<html>old</html>')
            self.assertEqual(sorted(p.name for p in (root / 'ui').iterdir()), ['build.mjs', 'dist.lock'])


if __name__ == '__main__':
    unittest.main()
