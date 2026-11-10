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
// 暂存目录放在 ui/ 下（已被 .gitignore 忽略），中断残留也不会被发行包收进 agent_collab/。
const stage = join(here, 'dist'), old = join(here, 'dist.prev')
try {
  for (const f of ['src', 'node_modules', 'index.html', 'package.json', 'tsconfig.json', 'vite.config.ts']) cpSync(join(here, f), join(work, f), { recursive: true })
  execFileSync(process.execPath, [join(work, 'node_modules', 'typescript', 'bin', 'tsc'), '--noEmit', '-p', work], { stdio: 'inherit', cwd: work })
  execFileSync(process.execPath, [join(work, 'node_modules', 'vite', 'bin', 'vite.js'), 'build', '--outDir', join(work, 'dist')], { stdio: 'inherit', cwd: work })
  for (const d of [stage, old]) rmSync(d, { recursive: true, force: true })
  cpSync(join(work, 'dist'), stage, { recursive: true })
  if (existsSync(out)) renameSync(out, old)
  try { renameSync(stage, out) } catch (e) { if (existsSync(old)) renameSync(old, out); throw e }
  rmSync(old, { recursive: true, force: true })
} finally {
  rmSync(work, { recursive: true, force: true })
  rmSync(stage, { recursive: true, force: true })
}
console.log('UI 已构建到', out)
