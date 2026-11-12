import { useEffect, useState } from 'react'
import { api, ApiError } from '../api'
import { can } from '../session'
import { bridge, isDesktop } from '../transport'
import { Action, Badge, Capped, EFFORT_RE, MODEL_RE, ORIGIN, Section, bytes, errText, show, short, useData, when } from '../ui'
import { NewJob } from './NewJob'
import { ProjectInput, gitText } from './ProjectInput'

const actor = (a: any) => a ? (a.name ?? ORIGIN[a.kind] ?? a.kind) : '未知'
const UNVERIFIED: Record<string, string> = { '': '沿用工作区', wait: '等待已核实的节点', allow: '允许未核实能力尝试' }
export const APPLY_STATUS: Record<string, [string, string]> = {
  started: ['已开始，未确认完成', 'warn'], applied: ['已全部写入', 'good'], partial: ['部分写入', 'warn'],
  conflict: ['因冲突未写入', 'bad'], failed: ['未写入或已回滚', 'bad'], unknown: ['结果未知', 'muted'],
}
export const ApplyBadge = ({ s }: { s?: string }) => <span className={'badge ' + (APPLY_STATUS[s ?? '']?.[1] ?? 'muted')}>{APPLY_STATUS[s ?? '']?.[0] ?? show(s)}</span>

export function ProjectDetail({ id, tick }: { id: string; tick: number }) {
  const { data: p, error, reload } = useData(() => api.project(id), [id, tick])
  const [creating, setCreating] = useState(false)
  if (!p) return error ? <p className="err">{error}</p> : <p className="muted">加载中…</p>
  return <>
    {error && <p className="err">刷新失败，以下为上次成功加载的内容：{error}</p>}
    <Basics p={p} reload={reload} />
    <Defaults p={p} tick={tick} reload={reload} />
    <Binding p={p} />
    <ProjectInput p={p} />
    <Baselines p={p} tick={tick} />
    {creating && <NewJob project={p.project_id} onCreated={j => { setCreating(false); location.hash = '#/jobs/' + j }} />}
    <ProjectJobs p={p} tick={tick} extra={can('tasks') && !p.archived_at && <button onClick={() => setCreating(c => !c)}>{creating ? '收起新建' : '在此项目中新建任务'}</button>} />
    <Applications p={p} tick={tick} />
  </>
}

// 版本冲突：别人先改了项目，提示后刷新到最新值，不覆盖。
const conflictText = (e: unknown) => e instanceof ApiError && e.code === 'version_mismatch'
  ? `项目已被修改为第 ${e.details?.current?.revision ?? '新'} 版，你的修改没有保存。页面已刷新为最新值，请确认后重试。` : errText(e)

function Basics({ p, reload }: { p: any; reload: () => void }) {
  const [name, setName] = useState(p.name)
  const [msg, setMsg] = useState<[string, string] | null>(null)
  const [busy, setBusy] = useState(false)
  const put = async (patch: any) => {
    setBusy(true); setMsg(null)
    try { await api.updateProject(p.project_id, p.revision, patch); setMsg(['已保存', 'ok']) } catch (e) { setMsg([conflictText(e), 'err']) } finally { setBusy(false); reload() }
  }
  const tasks = can('tasks')
  return <Section title={'项目 ' + p.name} extra={<a href="#/projects">返回列表</a>}>
    {p.archived_at && <p className="notice">项目已于 {when(p.archived_at)} 归档：不接受新的输入、草拟与任务；已有的任务、输入与应用记录保留可读，可以取消归档。</p>}
    <dl className="kv">
      <dt>project_id</dt><dd><code>{p.project_id}</code></dd>
      <dt>配置版本</dt><dd>第 {p.revision} 版{p.updated_at ? ' · ' + actor(p.updated_by) + ' 修改于 ' + when(p.updated_at) : ''}<small className="muted">（名称、默认策略、归档每改一次加一；登记输入与应用成果不改变它）</small></dd>
      <dt>创建</dt><dd>{actor(p.created_by)} · {when(p.created_at)}</dd>
      <dt>任务</dt><dd>{p.jobs ?? 0} 个，进行中 {p.active_jobs ?? 0} 个</dd>
    </dl>
    <div className="actions">
      <input aria-label="项目名称" value={name} maxLength={200} onChange={e => setName(e.target.value)} disabled={!tasks} />
      <button disabled={busy || !tasks || !name.trim() || name.trim() === p.name} onClick={() => void put({ name: name.trim() })}>改名</button>
      <button className={p.archived_at ? '' : 'danger'} disabled={busy || !tasks} onClick={() => {
        if (p.archived_at || window.confirm('归档后这个项目不再接受新的输入、草拟与任务；已有记录保留，可以随时取消归档。确定？')) void put({ archived: !p.archived_at })
      }}>{p.archived_at ? '取消归档' : '归档'}</button>
      {!tasks && <small className="muted">需要任务权限</small>}
      {msg && <small className={msg[1]}>{msg[0]}</small>}
    </div>
  </Section>
}

// 项目默认值：每项为 null 表示沿用工作区；model 与 effort 成对给出。保存时 defaults 整体替换，需要 settings 权限。
function Defaults({ p, tick, reload }: { p: any; tick: number; reload: () => void }) {
  const models = useData(() => api.models(), [tick])
  const d = p.defaults ?? {}
  const init = (): Record<'model' | 'effort' | 'unverified' | 'exclude', string> => ({ model: d.codex?.model ?? '', effort: d.codex?.effort ?? '', unverified: d.codex?.unverified_nodes ?? '', exclude: (d.exclude ?? []).join('\n') })
  // 编辑副本；null 表示没有在编辑，跟随 controller 的最新值。开始编辑时记下所基于的版本。
  const [edit, setEdit] = useState<ReturnType<typeof init> | null>(null)
  const [base, setBase] = useState(p.revision)
  const [msg, setMsg] = useState<[string, string] | null>(null)
  const [busy, setBusy] = useState(false)
  const f = edit ?? init()
  const dirty = !!edit && JSON.stringify(edit) !== JSON.stringify(init())
  const selectable: any[] = models.data?.selectable?.codex ?? []
  const ws = models.data?.settings?.codex
  const lines = f.exclude.split('\n').map(x => x.trim()).filter(Boolean)
  const badGlob = lines.filter(g => g.startsWith('/') || g.includes('\\') || /^[A-Za-z]:/.test(g) || g.split('/').some(s => s === '' || s === '.' || s === '..'))
  const bad = (f.model && !MODEL_RE.test(f.model)) || (f.effort && !EFFORT_RE.test(f.effort)) || (!f.model !== !f.effort) || badGlob.length > 0
  const editable = can('settings') && can('tasks') && !p.archived_at
  const save = async () => {
    setBusy(true); setMsg(null)
    try {
      await api.updateProject(p.project_id, base, { defaults: {
        codex: { model: f.model || null, effort: f.effort || null, unverified_nodes: f.unverified || null },
        exclude: lines.length ? lines : null } })
      setEdit(null); setMsg(['已保存', 'ok'])
    } catch (e) {
      if (e instanceof ApiError && e.code === 'version_mismatch') { setEdit(null); setMsg([conflictText(e), 'err']) }
      else setMsg([e instanceof ApiError && e.details?.reason === 'model_not_allowed' ? '这个组合不在部署许可范围内，请换一个或先调整“设置”里的许可范围' : errText(e), 'err'])
    } finally { setBusy(false); reload() }
  }
  const set = (k: string, v: string) => { if (!edit) setBase(p.revision); setEdit({ ...f, [k]: v }); setMsg(null) }
  return <Section title="默认策略" extra={<span className="muted">优先级：任务 &gt; 项目 &gt; 工作区；改动只影响之后受理的任务</span>}>
    <p className="muted">留空表示沿用工作区的值（当前工作区默认 {ws?.default?.model ? `${ws.default.model} / ${ws.default.effort}` : '尚未设置'}，未核实能力尝试 {ws?.unverified_nodes === 'allow' ? '允许' : '等待'}）。已受理的任务（包括排队中的）和它们的修复沿用受理时冻结的值。</p>
    <div className="form-grid">
      <label>Codex 默认模型<input list="project-models" disabled={!editable} value={f.model} placeholder="沿用工作区" onChange={e => set('model', e.target.value.trim())} />
        <datalist id="project-models">{selectable.map(s => <option key={s.model} value={s.model} />)}</datalist></label>
      <label>推理强度（effort）<input list="project-efforts" disabled={!editable} value={f.effort} placeholder="沿用工作区" onChange={e => set('effort', e.target.value.trim())} />
        <datalist id="project-efforts">{(selectable.find(s => s.model === f.model)?.efforts ?? []).map((x: string) => <option key={x} value={x} />)}</datalist></label>
      <label>未核实能力尝试<select disabled={!editable} value={f.unverified} onChange={e => set('unverified', e.target.value)}>
        {Object.entries(UNVERIFIED).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
        <small className="muted">模型目录未核实的设备缺省不接单；“允许”时可作为未核实能力尝试，仍优先已核实的设备。</small></label>
    </div>
    <label>排除规则（每行一个 POSIX 相对 glob，如 <code>build/**</code>、<code>*.log</code>；留空沿用缺省规则）
      <textarea disabled={!editable} value={f.exclude} onChange={e => set('exclude', e.target.value)} /></label>
    <p className="muted">排除规则用于扫描输入、执行节点的检查点与成果应用，草拟时写进契约。匹配区分大小写；<code>*</code> 不跨目录，单独的 <code>**</code> 匹配任意层目录。</p>
    {(f.model || f.effort) && !f.model !== !f.effort && <p className="err">模型与 effort 要成对填写，或都留空。</p>}
    {((f.model && !MODEL_RE.test(f.model)) || (f.effort && !EFFORT_RE.test(f.effort))) && <p className="err">格式不对：模型以字母或数字开头，可含 . _ : / -；effort 为小写字母开头，可含数字、_ 与 -。</p>}
    {badGlob.length > 0 && <p className="err">这些规则不是相对 POSIX glob（不能以 / 开头、不能含反斜杠或盘符、不能有空段、. 或 ..）：{badGlob.join('，')}</p>}
    {base !== p.revision && dirty && <p className="notice">项目在你编辑期间被修改为第 {p.revision} 版。保存会因版本冲突被拒绝；<button className="link" onClick={() => setEdit(null)}>放弃我的修改，载入最新值</button></p>}
    <p className="actions">
      <button className="primary" disabled={!editable || busy || !dirty || !!bad} onClick={() => void save()}>保存默认策略</button>
      {dirty && <button onClick={() => { setEdit(null); setMsg(null) }}>撤销修改</button>}
      {!editable && <small className="muted">{p.archived_at ? '已归档的项目不能修改' : '修改默认策略需要设置权限（settings）与任务权限（tasks）'}</small>}
      {msg && <small className={msg[1]}>{msg[0]}</small>}
    </p>
    <details><summary>修改历史（最近 {(p.history ?? []).length} 次）</summary>
      <table><thead><tr><th>版本</th><th>时间</th><th>修改者</th><th>名称</th><th>模型 / effort</th><th>未核实尝试</th><th>排除规则</th><th>归档</th></tr></thead>
        <tbody>{[...(p.history ?? [])].reverse().map((h: any) => { const a = h.after ?? {}, c = a.defaults?.codex ?? {}
          return <tr key={h.revision}><td>{h.revision}</td><td>{when(h.at)}</td><td>{actor(h.actor)}</td><td>{a.name}</td>
            <td>{c.model ? `${c.model} / ${c.effort}` : '沿用工作区'}</td><td>{UNVERIFIED[c.unverified_nodes ?? ''] ?? show(c.unverified_nodes)}</td>
            <td><small>{a.defaults?.exclude ? a.defaults.exclude.join('，') : '缺省'}</small></td><td>{a.archived_at ? '已归档' : '—'}</td></tr> })}</tbody></table>
    </details>
  </Section>
}

// 这台机器上的目录：只由桌面端外壳记录，页面经系统对话框选择，拿不到也不提供路径。
function Binding({ p }: { p: any }) {
  const [b, setB] = useState<any>(undefined)
  const [err, setErr] = useState<string | null>(null)
  const load = () => bridge('project_bindings').then(r => setB((r.bindings ?? []).find((x: any) => x.project_id === p.project_id) ?? null), e => setErr(errText(e)))
  useEffect(() => { if (isDesktop) void load() }, [p.project_id]) // eslint-disable-line
  if (!isDesktop) return <Section title="这台机器上的目录"><p className="muted">选择项目文件夹、扫描与上传输入、把成果应用到目录只在桌面端可用（浏览器不能读写本机目录）。也可以用命令行 <code>agent-collab project</code> 完成。</p></Section>
  return <Section title="这台机器上的目录" extra={<span className="muted">绑定只保存在这台机器上，不上传；每台设备各有各的</span>}>
    {err && <p className="err">{err}</p>}
    {b === undefined ? <p className="muted">读取中…</p> : b ? <p>已绑定目录 <b>{b.name}</b>（{when(b.bound_at)}）{b.exists ? <span className="badge good">存在</span> : <span className="badge bad">目录已不存在</span>}</p>
      : <p className="muted">还没有为这个项目选择文件夹。</p>}
    <div className="actions">
      <Action label={b ? '重新选择文件夹' : '选择文件夹'} disabled={!!p.archived_at} run={async () => {
        const r = await bridge('project_bind', { project_id: p.project_id })
        await load()
        return r
      }} done={r => r?.cancelled ? '已取消选择' : null} />
      {b && <Action label="解除绑定" danger confirm="只删除这台机器上的绑定，不动目录里的文件。确定？" run={async () => { await bridge('project_unbind', { project_id: p.project_id }); await load() }} />}
    </div>
  </Section>
}

function Baselines({ p, tick }: { p: any; tick: number }) {
  const { data, error } = useData(() => api.baselines(p.project_id), [p.project_id, tick])
  const list: any[] = [...(data?.baselines ?? [])].reverse()
  return <Section title="输入基线" extra={<span className="muted">每次上传登记一份；草拟任务缺省用最近登记的那一份</span>}>
    {error && <p className="err">{error}</p>}
    <table><thead><tr><th>名称</th><th>文件</th><th>大小</th><th>来源</th><th>内容</th><th>时间</th></tr></thead>
      <tbody><Capped items={list} n={50} render={b => { const s = b.source ?? {}
        return <tr key={b.baseline_id}><td>{b.name}{p.baseline?.baseline_id === b.baseline_id ? <> <span className="badge info">最近登记</span></> : null}<br /><small className="muted"><code>{short(b.baseline_id)}</code></small></td>
          <td>{b.files}</td><td>{bytes(b.bytes)}</td>
          <td><small>{[s.device && '设备 ' + s.device, s.root_name && '目录 ' + s.root_name, gitText(s.git),
            s.excluded != null && '排除 ' + (s.excluded?.count ?? s.excluded) + ' 项',
            s.skipped != null && '跳过 ' + (typeof s.skipped === 'object' ? Object.values<number>(s.skipped).reduce((a, n) => a + n, 0) : s.skipped) + ' 个',
            s.scanned_at && '扫描于 ' + new Date(s.scanned_at).toLocaleString('zh-CN', { hour12: false })].filter(Boolean).join(' · ') || '未记录'}</small></td>
          <td>{b.missing_blobs?.length ? <span className="badge warn">{b.missing_blobs.length} 个文件未上传</span> : <span className="badge good">已全部上传</span>}</td>
          <td>{when(b.created_at)}</td></tr> }} /></tbody></table>
    {data && !list.length && <p className="muted">还没有输入。{isDesktop ? '在上面“输入”里扫描并上传。' : '在桌面端扫描并上传，或用命令行登记。'}</p>}
  </Section>
}

function ProjectJobs({ p, tick, extra }: { p: any; tick: number; extra: any }) {
  const { data, error } = useData(() => api.jobs('', '', p.project_id), [p.project_id, tick])
  const jobs: any[] = data?.jobs ?? []
  return <Section title="任务" extra={extra}>
    {error && <p className="err">{error}</p>}
    <table><thead><tr><th>任务</th><th>运行</th><th>验收</th><th>交付版本</th><th>受理时项目版本</th><th>更新</th></tr></thead>
      <tbody>{jobs.map(j => <tr key={j.job_id}><td><a href={'#/jobs/' + j.job_id}>{j.title || short(j.job_id)}</a></td><td><Badge v={j.status} /></td>
        <td><Badge v={j.acceptance?.status} /></td><td>{show(j.delivery_version)}</td><td>{show(j.project_revision ?? j.model_policy?.project_revision)}</td><td>{when(j.updated_at)}</td></tr>)}</tbody></table>
    {data && !jobs.length && <p className="muted">这个项目还没有任务。</p>}
  </Section>
}

function Applications({ p, tick }: { p: any; tick: number }) {
  const { data, error } = useData(() => api.applications(p.project_id), [p.project_id, tick])
  const list: any[] = [...(data?.applications ?? [])].reverse()
  return <Section title="应用记录" extra={<span className="muted">把任务成果写进某台设备上的项目目录的记录</span>}>
    {error && <p className="err">{error}</p>}
    <table><thead><tr><th>开始</th><th>结束</th><th>任务</th><th>设备</th><th>状态</th><th>详情</th></tr></thead>
      <tbody><Capped items={list} n={100} render={a => <tr key={a.application_id}><td>{when(a.started_at)}</td><td>{a.finished_at ? when(a.finished_at) : '—'}</td>
        <td><a href={'#/jobs/' + a.job_id}>{short(a.job_id)}</a><br /><small className="muted">检查点 <code>{short(a.checkpoint_id)}</code></small></td>
        <td>{show(a.device)}</td><td><ApplyBadge s={a.status} /></td>
        <td>{a.detail ? <details><summary>展开</summary><pre className="json">{JSON.stringify(a.detail, null, 2)}</pre></details> : '—'}</td></tr>} /></tbody></table>
    {data && !list.length && <p className="muted">还没有应用记录。</p>}
  </Section>
}
