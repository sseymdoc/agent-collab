// 仓库位于 Windows 的 SMB 映射盘时，rolldown 会把实路径解析成 UNC 而无法构建。
// 本脚本把源码与依赖复制到本机临时目录构建，再把产物复制回 agent_collab/static。
// 在本地盘上直接运行 `npx vite build` 同样可用。
import { cpSync, rmSync, mkdtempSync, mkdirSync, existsSync, renameSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const out = join(here, '..', 'agent_collab', 'static')
// 暂存、旧产物备份和锁都在 ui/ 下（.gitignore 忽略 /ui/dist*/），残留不会被发行包收进 agent_collab/。
const stage = join(here, 'dist'), old = join(here, 'dist.prev'), lock = join(here, 'dist.lock')

// 同一 checkout 的构建互斥：锁就是一个目录，mkdir 原子创建，只在本进程的 finally 中删除。
// 不判断持有者是否存活、不自动接管：进程号跨主机或 PID 命名空间不可靠，接管本身也会与新锁竞争。
// 异常退出（强制结束）留下的锁由人确认后删除。
function acquire() {
  try { mkdirSync(lock) } catch (e) {
    if (e.code !== 'EEXIST') throw e
    throw new Error('构建锁 ' + lock + ' 已存在：可能有另一个 UI 构建正在进行，或上次构建被强制结束留下了残留。确认没有构建在运行后删除该目录再试。')
  }
}

// Ctrl+C / 终止信号不立即退出：正在运行的 tsc、vite 子进程随信号结束，构建按失败走完 finally，释放锁并还原产物，
// 不留下残留锁。
for (const s of ['SIGINT', 'SIGTERM']) process.on(s, () => { process.exitCode = 130 })

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
