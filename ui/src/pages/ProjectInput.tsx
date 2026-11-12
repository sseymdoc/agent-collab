import { useEffect, useState } from 'react'
import { bridge, isDesktop, onProject, type ProjectProgress } from '../transport'
import { Capped, Section, bytes, errText, when } from '../ui'

// 输入：先把绑定目录扫描成本机快照（不上传），确认将上传的范围后再上传为项目的输入基线。
export const SKIP: Record<string, string> = {
  link: '链接未跟随（符号链接或 Windows 重解析点）', name_not_portable: '名称在 Windows 上无法表示', too_large: '超过上传上限',
  unreadable: '无法读取', special_file: '不是普通文件',
}
export const PHASE: Record<string, string> = {
  scan: '扫描', rescan: '目录有变化，重新扫描', stage: '暂存', upload: '上传', register: '登记', download: '下载', write: '写入项目目录',
  report: '向 controller 报告结果', cancel_requested: '已请求取消',
}

// 取消是请求，不是已停止：写入项目目录时当前文件写完才停（不会留下写了一半的文件），其他阶段在下一步停下。
// 操作的命令返回之后才算已停止，页面那时再显示结果。
export function CancelButton({ op }: { op: string }) {
  const [asked, setAsked] = useState<string | null>(null)
  if (asked) return <small className="muted" role="status">{asked}</small>
  return <button className="danger" onClick={() => void bridge('project_cancel').then((r: any) => {
    if (!r?.requested) return
    setAsked(r.stops === 'after_current_file' ? '已请求取消：当前文件写完后停止，已写入的文件保留（取消不等于回滚）' : '已请求取消：在下一步停止')
  }, () => {})}>{op === 'apply' || op === 'recover' ? '取消（写完当前文件后停止）' : '取消'}</button>
}

// git 为 null 表示不是 Git 项目；available=false 表示是 Git 项目但这台机器上找不到 git。
export const gitText = (g: any) => !g ? null : g.available === false ? 'Git 项目（本机没有 git，未读取分支）'
  : 'Git ' + (g.branch ?? (g.commit ? '游离提交 ' + String(g.commit).slice(0, 7) : '未知分支')) + (typeof g.uncommitted === 'number' ? '，未提交改动 ' + g.uncommitted + ' 个' : '')

export function Progress({ p }: { p: ProjectProgress | null }) {
  if (!p) return null
  if (p.phase === 'cancel_requested') return <p className="muted" role="status">已请求取消，{p.op === 'apply' || p.op === 'recover' ? '正在写的文件写完后停止' : '正在停止'}…</p>
  return <p className="muted" role="status">进行中：{PHASE[p.phase] ?? p.phase}
    {typeof p.done === 'number' ? ` · ${p.done}${typeof p.total === 'number' ? ' / ' + p.total : ''}` : ''}
    {typeof p.files === 'number' ? ` · ${p.files} 个文件` : ''}{typeof p.bytes === 'number' ? ' · ' + bytes(p.bytes) : ''}</p>
}

// 订阅某类项目操作的进度；op 不符的事件忽略。
export function useProgress(ops: string[]) {
  const [p, setP] = useState<ProjectProgress | null>(null)
  useEffect(() => onProject(x => { if (ops.includes(x.op)) setP(x) }), []) // eslint-disable-line
  return [p, setP] as const
}

export function ProjectInput({ p }: { p: any }) {
  const [scan, setScan] = useState<any>(null)
  const [rules, setRules] = useState<string | null>(null)   // 编辑过的排除规则；null 表示用项目规则加缺省规则
  const [busy, setBusy] = useState<'scan' | 'upload' | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [done, setDone] = useState<any>(null)
  const [prog, setProg] = useProgress(['scan', 'upload'])
  if (!isDesktop) return null
  const run = async (kind: 'scan' | 'upload', f: () => Promise<void>) => {
    setBusy(kind); setErr(null); setProg(null)
    try { await f() } catch (e: any) { setErr(e?.code === 'cancelled' ? '已取消。已上传的内容保留，下次只传缺的部分。' : errText(e)) } finally { setBusy(null); setProg(null) }
  }
  const lines = (rules ?? '').split('\n').map(x => x.trim()).filter(Boolean)
  const doScan = () => run('scan', async () => {
    setDone(null)
    setScan(await bridge('project_scan', { project_id: p.project_id, ...(rules !== null ? { exclude: lines } : {}) }))
  })
  const counts = Object.entries<number>(scan?.skipped_counts ?? {})
  return <Section title="输入" extra={<span className="muted">只读选定目录之内的普通文件；不跟随链接</span>}>
    <p className="muted">先扫描，确认将上传的范围（文件、大小、被排除与被跳过的条目），再上传为输入。内容按哈希寻址：controller 已有的文件不重复上传。缺省排除版本库元数据、依赖与构建缓存和常见凭据文件（<code>.env</code>、<code>*.pem</code>、<code>*.key</code> 等）。</p>
    <div className="actions">
      <button disabled={!!busy || !!p.archived_at} onClick={() => void doScan()}>{scan ? '重新扫描' : '扫描'}</button>
      {busy && <CancelButton key={busy} op={busy} />}
      {busy === 'scan' && <small className="muted">扫描中…</small>}
    </div>
    <Progress p={prog} />
    {err && <p className="err" role="alert">{err}</p>}
    {scan && <>
      <h3>将上传的范围（目录 {scan.root_name}，扫描于 {when(scan.scanned_at)}）</h3>
      <dl className="kv">
        <dt>文件</dt><dd>{scan.files} 个，共 {bytes(scan.bytes)}<small className="muted">（文件清单 {bytes(scan.manifest_bytes)}）</small></dd>
        {scan.git && <><dt>Git</dt><dd>{gitText(scan.git)}<small className="muted">（只上传工作目录里的当前内容，包括未提交的改动）</small></dd></>}
        <dt>被排除</dt><dd>{scan.excluded?.count ?? 0} 项{scan.excluded?.top?.length ? <details><summary>查看{scan.excluded.count > scan.excluded.top.length ? `前 ${scan.excluded.top.length} 项` : ''}</summary>
          <ul className="paths"><Capped items={scan.excluded.top as string[]} render={x => <li key={x}><code>{x}</code></li>} /></ul></details> : null}</dd>
        <dt>被跳过</dt><dd>{counts.length ? counts.map(([k, n]) => `${SKIP[k] ?? k} ${n} 个`).join('；') : '无'}
          {scan.skipped?.length ? <details><summary>查看{scan.skipped.length < counts.reduce((a, [, n]) => a + n, 0) ? `前 ${scan.skipped.length} 个` : ''}</summary>
            <ul className="paths"><Capped items={scan.skipped as any[]} render={(x, i) => <li key={i}><code>{x.path}</code> <small className="muted">{SKIP[x.reason] ?? x.reason}</small></li>} /></ul></details> : null}</dd>
      </dl>
      <label>生效的排除规则（每行一个 POSIX glob；修改后重新扫描生效，只用于这次扫描，不改项目默认值）
        <textarea value={rules ?? (scan.exclude ?? []).join('\n')} onChange={e => setRules(e.target.value)} /></label>
      <div className="actions">
        {rules !== null && <button disabled={!!busy} onClick={() => void doScan()}>按修改后的规则重新扫描</button>}
        {rules !== null && <button className="link" onClick={() => setRules(null)}>恢复项目规则</button>}
        <button className="primary" disabled={!!busy || !!p.archived_at} onClick={() => void run('upload', async () => {
          setDone(await bridge('project_upload', { project_id: p.project_id, snapshot_id: scan.snapshot_id }))
        })}>{busy === 'upload' ? '上传中…' : '上传为输入'}</button>
      </div>
    </>}
    {done && <p className="ok">{done.reused ? '内容与项目最近一份输入相同，没有新建基线。' : `已登记为新的输入基线 ${String(done.baseline_id).slice(0, 8)}。`}
      本次上传 {done.uploaded_blobs ?? 0} 个文件（{bytes(done.uploaded_bytes)}）{done.missing_blobs?.length ? `，仍有 ${done.missing_blobs.length} 个文件未上传` : ''}。</p>}
  </Section>
}
