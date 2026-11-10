// 仓库位于 Windows 的 SMB 映射盘时，rolldown 会把实路径解析成 UNC 而无法构建。
// 本脚本把源码与依赖复制到本机临时目录构建，再把产物复制回 agent_collab/static。
// 在本地盘上直接运行 `npx vite build` 同样可用。
import { cpSync, rmSync, mkdtempSync, mkdirSync, existsSync, renameSync } from 'node:fs'
import { spawn } from 'node:child_process'
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

// Ctrl+C / 终止信号：记下中断并结束正在运行的子进程，构建按失败走完 finally（释放锁、还原产物），发布前检查中断标记。
// 子进程用异步 spawn 运行，信号处理函数才能在构建期间执行；只给本进程发 SIGTERM 时，子进程也会被结束。
let child = null, aborted = false
for (const s of ['SIGINT', 'SIGTERM']) process.on(s, () => { aborted = true; process.exitCode = 130; child?.kill() })
function run(args, cwd) {
  return new Promise((resolve, reject) => {
    if (aborted) return reject(new Error('构建已中断，未发布'))
    child = spawn(process.execPath, args, { stdio: 'inherit', cwd })
    child.on('error', reject)
    child.on('exit', (code, signal) => { child = null; code === 0 ? resolve() : reject(new Error('构建步骤失败：' + (signal ?? 'exit ' + code))) })
  })
}
// 同步步骤期间到达的信号要等让出事件循环后才处理：发布前先让出一次，再检查。
async function checkAborted() { await new Promise(r => setImmediate(r)); if (aborted) throw new Error('构建已中断，未发布') }

acquire()
let work = null
// 外层 finally 只负责释放锁：建临时目录失败或下面的清理出错时，锁照样删除。
try { try {
  work = mkdtempSync(join(tmpdir(), 'agent-collab-ui-build-'))
  // 上次发布在两次改名之间中断：static 缺失、旧产物只在备份里。先还原，再开始本次构建。
  if (!existsSync(out) && existsSync(old)) renameSync(old, out)
  for (const d of [stage, old]) rmSync(d, { recursive: true, force: true })
  for (const f of ['src', 'node_modules', 'index.html', 'package.json', 'tsconfig.json', 'vite.config.ts']) cpSync(join(here, f), join(work, f), { recursive: true })
  await checkAborted()
  await run([join(work, 'node_modules', 'typescript', 'bin', 'tsc'), '--noEmit', '-p', work], work)
  await run([join(work, 'node_modules', 'vite', 'bin', 'vite.js'), 'build', '--outDir', join(work, 'dist')], work)
  // 新产物先完整复制到暂存目录，再用改名替换：复制失败（如磁盘不足）时旧产物不受影响，替换失败时还原旧产物。
  cpSync(join(work, 'dist'), stage, { recursive: true })
  await checkAborted()
  if (existsSync(out)) renameSync(out, old)
  try { renameSync(stage, out) } catch (e) { if (!existsSync(out) && existsSync(old)) renameSync(old, out); throw e }
} finally {
  if (work) rmSync(work, { recursive: true, force: true })
  rmSync(stage, { recursive: true, force: true })
  // 旧产物备份只在 static 已就位时删除；static 不在时它是唯一副本，下次构建会先还原它。
  if (existsSync(out)) rmSync(old, { recursive: true, force: true })
} } finally {
  rmSync(lock, { recursive: true, force: true })
}
console.log('UI 已构建到', out)
