"""ui/build.mjs 的构建锁：锁已存在时不接管、不清理，直接失败。"""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
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

    @unittest.skipIf(os.name == 'nt', 'SIGTERM cannot be handled on Windows')
    def test_sigterm_to_build_process_stops_child_and_does_not_publish(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ui = root / 'ui'
            (ui / 'src').mkdir(parents=True)
            shutil.copy2(str(ROOT / 'ui' / 'build.mjs'), str(ui / 'build.mjs'))
            for name in ('index.html', 'tsconfig.json', 'vite.config.ts'):
                (ui / name).write_text('')
            (ui / 'package.json').write_text('{}')  # 假脚本按 CommonJS 运行
            # 假的 tsc：记下进程号后长时间运行；假的 vite：若被执行就产出新 static。
            marker = root / 'tsc.pid'
            tsc = ui / 'node_modules' / 'typescript' / 'bin' / 'tsc'
            tsc.parent.mkdir(parents=True)
            (tsc.parent.parent / 'package.json').write_text('{}')
            tsc.write_text("require('fs').writeFileSync(%r, String(process.pid)); setTimeout(() => {}, 60000)\n" % str(marker))
            vite = ui / 'node_modules' / 'vite' / 'bin' / 'vite.js'
            vite.parent.mkdir(parents=True)
            (vite.parent.parent / 'package.json').write_text('{}')
            vite.write_text("const fs = require('fs'), p = require('path'); const d = process.argv[process.argv.indexOf('--outDir') + 1];"
                            " fs.mkdirSync(d, { recursive: true }); fs.writeFileSync(p.join(d, 'index.html'), 'new')\n")
            static = root / 'agent_collab' / 'static'
            static.mkdir(parents=True)
            (static / 'index.html').write_text('old')
            build = subprocess.Popen([NODE, 'build.mjs'], cwd=str(ui), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                for _ in range(300):
                    if marker.exists() and marker.read_text():
                        break
                    time.sleep(0.05)
                else:
                    self.fail('fake tsc did not start')
                child = int(marker.read_text())
                build.send_signal(signal.SIGTERM)  # 只发给构建进程本身
                build.communicate(timeout=30)
            finally:
                if build.poll() is None:
                    build.kill()
            self.assertNotEqual(build.returncode, 0)
            self.assertEqual((static / 'index.html').read_text(), 'old')
            self.assertFalse((ui / 'dist.lock').exists())
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)

    def test_lock_is_released_when_temporary_directory_cannot_be_created(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'ui').mkdir()
            shutil.copy2(str(ROOT / 'ui' / 'build.mjs'), str(root / 'ui' / 'build.mjs'))
            missing = str(root / 'no-such-tmp')
            env = dict(os.environ, TMPDIR=missing, TMP=missing, TEMP=missing)
            run = subprocess.run([NODE, 'build.mjs'], cwd=str(root / 'ui'), env=env, capture_output=True, timeout=60)
            self.assertNotEqual(run.returncode, 0)
            self.assertIn('mkdtemp', (run.stdout + run.stderr).decode('utf-8', 'replace'))
            self.assertFalse((root / 'ui' / 'dist.lock').exists())


if __name__ == '__main__':
    unittest.main()
