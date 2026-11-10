// 仓库位于 Windows 的 SMB 映射盘时，rolldown 会把实路径解析成 UNC 而无法构建。
// 本脚本把源码与依赖复制到本机临时目录构建，再把产物复制回 agent_collab/static。
// 在本地盘上直接运行 `npx vite build` 同样可用。
import { cpSync, rmSync, mkdtempSync, existsSync, renameSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const out = join(here, '..', 'agent_collab', 'static')
// 每次构建独占一个临时目录（并发构建互不干扰），成功或失败都删除。
const work = mkdtempSync(join(tmpdir(), 'agent-collab-ui-build-'))
// 新产物先完整复制到 static 旁的暂存目录，再用改名替换；复制失败（如磁盘不足）时旧产物不受影响，替换失败时恢复旧产物。
// 暂存目录放在 ui/ 下（.gitignore 忽略 /ui/dist*/），中断残留也不会被发行包收进 agent_collab/；
// 名字带进程号，并发构建各用各的暂存目录，发布只靠改名，不会把半份产物放进 static。
const stage = join(here, 'dist-' + process.pid), old = join(here, 'dist.prev-' + process.pid)
try {
  for (const f of ['src', 'node_modules', 'index.html', 'package.json', 'tsconfig.json', 'vite.config.ts']) cpSync(join(here, f), join(work, f), { recursive: true })
  execFileSync(process.execPath, [join(work, 'node_modules', 'typescript', 'bin', 'tsc'), '--noEmit', '-p', work], { stdio: 'inherit', cwd: work })
  execFileSync(process.execPath, [join(work, 'node_modules', 'vite', 'bin', 'vite.js'), 'build', '--outDir', join(work, 'dist')], { stdio: 'inherit', cwd: work })
  for (const d of [stage, old]) rmSync(d, { recursive: true, force: true })
  cpSync(join(work, 'dist'), stage, { recursive: true })
  if (existsSync(out)) renameSync(out, old)
  // 改名失败时还原旧产物；若并发构建已先发布了新的 static，就保留它。
  try { renameSync(stage, out) } catch (e) { if (!existsSync(out) && existsSync(old)) renameSync(old, out); throw e }
} finally {
  rmSync(work, { recursive: true, force: true })
  rmSync(stage, { recursive: true, force: true })
  // 旧产物备份只在 static 已就位时删除；static 不在时它是唯一副本，留给人工恢复。
  if (existsSync(out)) rmSync(old, { recursive: true, force: true })
}
console.log('UI 已构建到', out)
