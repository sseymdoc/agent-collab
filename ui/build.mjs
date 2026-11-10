// 仓库位于 Windows 的 SMB 映射盘时，rolldown 会把实路径解析成 UNC 而无法构建。
// 本脚本把源码与依赖复制到本机临时目录构建，再把产物复制回 agent_collab/static。
// 在本地盘上直接运行 `npx vite build` 同样可用。
import { cpSync, rmSync, mkdtempSync, mkdirSync, existsSync, renameSync, readFileSync, writeFileSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const out = join(here, '..', 'agent_collab', 'static')
// 暂存、旧产物备份和锁都在 ui/ 下（.gitignore 忽略 /ui/dist*/），残留不会被发行包收进 agent_collab/。
const stage = join(here, 'dist'), old = join(here, 'dist.prev'), lock = join(here, 'dist.lock')

// 同一 checkout 的构建互斥（mkdir 是原子操作）。锁里记下进程号：持有者已退出（如 Ctrl+C 未走到清理）时接管残留锁。
function acquire() {
  for (;;) {
    try { mkdirSync(lock); writeFileSync(join(lock, 'pid'), String(process.pid)); return } catch (e) {
      if (e.code !== 'EEXIST') throw e
      let pid = 0
      try { pid = Number(readFileSync(join(lock, 'pid'), 'utf8')) } catch { /* 刚建锁还没写入，按占用处理 */ }
      let alive = !pid
      try { if (pid) { process.kill(pid, 0); alive = true } } catch (k) { alive = k.code === 'EPERM' }
      if (alive) throw new Error('另一个 UI 构建正在进行（' + lock + '）；等它结束再试。确认没有构建在运行时可删除该目录。')
      rmSync(lock, { recursive: true, force: true })
    }
  }
}

acquire()
const work = mkdtempSync(join(tmpdir(), 'agent-collab-ui-build-'))
try {
  // 上次发布在两次改名之间中断：static 缺失、旧产物只在备份里。先还原，再开始本次构建。
  if (!existsSync(out) && existsSync(old)) renameSync(old, out)
  for (const d of [stage, old]) rmSync(d, { recursive: true, force: true })
  for (const f of ['src', 'node_modules', 'index.html', 'package.json', 'tsconfig.json', 'vite.config.ts']) cpSync(join(here, f), join(work, f), { recursive: true })
  execFileSync(process.execPath, [join(work, 'node_modules', 'typescript', 'bin', 'tsc'), '--noEmit', '-p', work], { stdio: 'inherit', cwd: work })
  execFileSync(process.execPath, [join(work, 'node_modules', 'vite', 'bin', 'vite.js'), 'build', '--outDir', join(work, 'dist')], { stdio: 'inherit', cwd: work })
  // 新产物先完整复制到暂存目录，再用改名替换：复制失败（如磁盘不足）时旧产物不受影响，替换失败时还原旧产物。
  cpSync(join(work, 'dist'), stage, { recursive: true })
  if (existsSync(out)) renameSync(out, old)
  try { renameSync(stage, out) } catch (e) { if (!existsSync(out) && existsSync(old)) renameSync(old, out); throw e }
} finally {
  rmSync(work, { recursive: true, force: true })
  rmSync(stage, { recursive: true, force: true })
  // 旧产物备份只在 static 已就位时删除；static 不在时它是唯一副本，下次构建会先还原它。
  if (existsSync(out)) rmSync(old, { recursive: true, force: true })
  rmSync(lock, { recursive: true, force: true })
}
console.log('UI 已构建到', out)
