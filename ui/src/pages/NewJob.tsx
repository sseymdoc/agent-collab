import { useRef, useState } from 'react'
import { api, ApiError } from '../api'
import { EFFORT_RE, Json, MODEL_RE, MODEL_SOURCE, ModelUnset, Section, bytes, errText, show, short, useData } from '../ui'

// 新建任务：模板 + 自然语言目标 → controller 草拟出完整契约 → 确认后提交。
// 契约由 controller 生成（POST /contracts/draft），界面不拼装也不改写它；CLI 与其他客户端走同一个端点。
const WARN: Record<string, string> = {
  empty_inputs: '没有选择输入基线，任务将从空工作区开始。',
  missing_blobs: '所选输入基线还有文件没有上传到 controller，任务会等到上传完成才开始。',
  no_node_supports_model: '目前没有已登记的设备声明支持这个模型与 effort，任务会一直等待。',
  no_verified_node_supports_model: '没有模型目录已核实、且含这个组合的设备；另有目录未核实的设备，但当前策略是“等待已核实的节点”，任务会停在“等待能力”，直到有设备的目录核实含这个组合。',
  unverified_node_attempt: '没有模型目录已核实、且含这个组合的设备；当前策略允许“未核实能力尝试”，任务可能派给目录未核实的设备，在那里才知道是否真的支持。',
}
const DISPATCH: Record<string, string> = { wait: '等待已核实的节点（目录未核实的设备不接这个任务）', allow: '允许未核实能力尝试（仍优先目录已核实的设备）' }
const PUBLISH = (c: any) => c.publish?.mode === 'manual'
  ? (c.publish?.requires_acceptance ? '验收通过后，由你手动发布成果' : '执行结束后，由你手动发布成果')
  : (c.publish?.requires_acceptance ? '验收通过后自动发布' : '执行结束后自动发布，不经过验收')
const COORD: Record<string, string> = { always: '每次结束都做语义验收', on_failure: '仅失败或发生接管时语义介入', none: '只用确定性检查，不触发协调' }

export function NewJob({ onCreated, project: initial = '' }: { onCreated: (jobId: string) => void; project?: string }) {
  const templates = useData(() => api.templates(), [])
  const baselines = useData(() => api.baselines(), [])
  const models = useData(() => api.models(), [])
  const projects = useData(() => api.projects(), [])
  const [project, setProject] = useState(initial)
  const [template, setTemplate] = useState('codex-change-accept')
  const [goal, setGoal] = useState('')
  const [acceptance, setAcceptance] = useState('')
  const [excludes, setExcludes] = useState('')
  const [baseline, setBaseline] = useState('')
  const [checks, setChecks] = useState<{ name: string; run: string }[]>([])
  const [model, setModel] = useState('')
  const [effort, setEffort] = useState('')
  const [os, setOs] = useState('')
  const [draft, setDraft] = useState<any>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const chosen = (templates.data?.templates ?? []).find((t: any) => t.id === template)
  const usesModel = chosen?.harness === 'codex'
  const selectable: any[] = models.data?.selectable?.codex ?? []
  const def = models.data?.settings?.codex?.default ?? {}
  const proj = (projects.data?.projects ?? []).find((x: any) => x.project_id === project)
  const unset = usesModel && models.data && !def.model && !(proj?.defaults?.codex?.model)
  // 改动任何输入都会让已生成的契约失效：必须重新生成后才能提交。
  // 草拟请求在途时改了输入，迟到的结果对应旧输入，按 inputs 计数丢弃。
  const inputs = useRef(0)
  const edit = <T,>(set: (v: T) => void) => (v: T) => { set(v); setDraft(null); inputs.current++ }
  const lines = (s: string) => s.split('\n').map(x => x.trim()).filter(Boolean)
  const argv = (s: string) => s.trim().startsWith('[') ? JSON.parse(s) : s.trim().split(/\s+/)
  const body = () => ({
    template, goal, acceptance: lines(acceptance), excludes: lines(excludes),
    checks: checks.filter(c => c.name.trim() && c.run.trim()).map(c => ({ name: c.name.trim(), run: argv(c.run) })),
    // 选了项目时不另给输入：controller 用项目最近登记的那一份，并在响应的 baseline_id 里给出。
    ...(project ? { project_id: project } : baseline ? { baseline_id: baseline } : {}), ...(usesModel && model ? { model } : {}), ...(usesModel && effort ? { effort } : {}),
    ...(os ? { requirements: { os: [os] } } : {}),
  })
  const run = async (f: () => Promise<void>) => {
    setBusy(true); setErr(null)
    try { await f() } catch (e) {
      // 草拟之后模型设置被改过：解析结果已过期，要求重新生成，而不是按新默认值提交。
      if (e instanceof ApiError && e.code === 'version_mismatch') {
        setDraft(null); void models.reload(); void projects.reload()
        setErr(project ? '工作区设置或项目默认值在草拟后变了，请重新生成契约并确认。' : '模型设置在你生成契约后被修改了，请重新生成契约并确认。')
      } else if (e instanceof ApiError && e.details?.reason === 'model_default_unset') setErr('unset')
      else if (e instanceof ApiError && e.code === 'project_archived') setErr('这个项目已归档，不接受新的任务。')
      else setErr(e instanceof SyntaxError ? '检查命令的 JSON 数组写法有误' : errText(e))
    } finally { setBusy(false) }
  }
  const p = draft?.preview, mp = draft?.model_policy
  const usedBaseline = draft?.baseline_id ? (baselines.data?.baselines ?? []).find((b: any) => b.baseline_id === draft.baseline_id) : null
  const badModel = usesModel && ((model && !MODEL_RE.test(model)) || (effort && !EFFORT_RE.test(effort)))
  return <Section title="新建任务" extra={<span className="muted">填写目标，选择模板；提交前会显示完整契约供你确认</span>}>
    {(templates.error || baselines.error) && <p className="err">{templates.error ?? baselines.error}</p>}
    {unset && <ModelUnset />}
    <div className="form-grid">
      <label>项目<select value={project} onChange={e => edit(setProject)(e.target.value)}>
        <option value="">不属于项目</option>
        {(projects.data?.projects ?? []).filter((x: any) => !x.archived_at || x.project_id === project).map((x: any) => <option key={x.project_id} value={x.project_id}>{x.name}</option>)}</select>
        <small className="muted">属于项目的任务使用项目的输入、默认模型策略与排除规则（任务 &gt; 项目 &gt; 工作区）。</small></label>
      <label>模板<select value={template} onChange={e => edit(setTemplate)(e.target.value)}>
        {(templates.data?.templates ?? []).map((t: any) => <option key={t.id} value={t.id}>{t.title}{t.diagnostic ? '（不调用模型）' : ''}</option>)}</select>
        <small className="muted">{chosen?.description}</small></label>
      {project ? <label>输入<span>{proj?.baseline ? <>项目最近登记的输入：{proj.baseline.files} 个文件，{bytes(proj.baseline.bytes)}</>
        : <span className="warn-text">这个项目还没有输入。<a href={'#/projects/' + project}>先去项目页扫描并上传</a>，或不使用输入从空工作区开始。</span>}</span></label>
      : <label>输入基线<select value={baseline} onChange={e => edit(setBaseline)(e.target.value)}>
        <option value="">不使用（空工作区）</option>
        {(baselines.data?.baselines ?? []).map((b: any) => <option key={b.baseline_id} value={b.baseline_id}>{b.name}（{b.files} 个文件{b.missing_blobs?.length ? '，未传完' : ''}）</option>)}</select>
        <small className="muted">从本机文件夹导入请先建项目（桌面端“项目”页），或用命令行 <code>agent-collab baseline add &lt;目录&gt; --name &lt;名称&gt;</code>。</small></label>}
    </div>
    <label>目标<textarea value={goal} onChange={e => edit(setGoal)(e.target.value)} placeholder="用自己的话写清楚要完成什么。第一行会作为任务标题。" /></label>
    <div className="form-grid">
      <label>验收项（每行一条）<textarea value={acceptance} onChange={e => edit(setAcceptance)(e.target.value)} /></label>
      <label>明确不做的事（每行一条）<textarea value={excludes} onChange={e => edit(setExcludes)(e.target.value)} /></label>
    </div>
    <fieldset><legend>必需检查（由执行设备运行并留下证据）</legend>
      {checks.map((c, i) => <div className="inline-form" key={i}>
        <input aria-label="检查名称" placeholder="名称，如 unit" value={c.name} onChange={e => edit(setChecks)(checks.map((x, j) => j === i ? { ...x, name: e.target.value } : x))} />
        <input aria-label="检查命令" className="wide" placeholder="命令，如 {python} -m unittest" value={c.run} onChange={e => edit(setChecks)(checks.map((x, j) => j === i ? { ...x, run: e.target.value } : x))} />
        <button type="button" className="link" onClick={() => edit(setChecks)(checks.filter((_, j) => j !== i))}>移除</button>
      </div>)}
      <button type="button" onClick={() => edit(setChecks)([...checks, { name: '', run: '' }])}>添加检查</button>
      <small className="muted"> 命令按空格分词，不经过 shell；<code>{'{python}'}</code> 会换成执行设备上的 Python。参数里有空格时写成 JSON 数组。</small>
    </fieldset>
    <div className="form-grid">
      {usesModel && <label>模型（留空用{proj?.defaults?.codex?.model ? `项目默认值 ${proj.defaults.codex.model} / ${proj.defaults.codex.effort}` : '工作区默认值 ' + (def.model ? `${def.model} / ${def.effort}` : '——尚未设置')}）
        {/* 可选组合作为候选，也可以手工填写（设备目录未知时）；是否可用由 controller 受理时判定。 */}
        <input list="newjob-models" value={model} placeholder="工作区默认" onChange={e => edit(setModel)(e.target.value.trim())} />
        <datalist id="newjob-models">{selectable.map(s => <option key={s.model} value={s.model} />)}</datalist></label>}
      {usesModel && <label>推理强度（effort）
        <input list="newjob-efforts" value={effort} placeholder="工作区默认" onChange={e => edit(setEffort)(e.target.value.trim())} />
        <datalist id="newjob-efforts">{(selectable.find(s => s.model === (model || def.model))?.efforts ?? []).map((x: string) => <option key={x} value={x} />)}</datalist></label>}
      <label>执行平台<select value={os} onChange={e => edit(setOs)(e.target.value)}>
        <option value="">任意合格设备</option><option value="windows">Windows</option><option value="macos">macOS</option><option value="linux">Linux</option></select></label>
    </div>
    {usesModel && !!models.data?.selectable?.unknown_nodes?.length && <p className="muted">设备 {models.data.selectable.unknown_nodes.join('、')} 的模型目录未知，可选项里不包含它们可能支持的组合。</p>}
    {badModel && <p className="err">模型或 effort 格式不对：模型以字母或数字开头，可含 . _ : / -；effort 为小写字母开头，可含数字、_ 与 -。</p>}
    {err === 'unset' ? <><p className="err" role="alert">controller 拒绝受理：没有指定模型，项目与工作区也都没有默认模型（model_default_unset）。</p><ModelUnset /></>
      : err && <p className="err" role="alert">{err}</p>}
    <div className="actions">
      <button disabled={busy || !goal.trim() || !!badModel} onClick={() => void run(async () => { const n = inputs.current; const d = await api.draft(body()); if (n === inputs.current) setDraft(d) })}>{draft ? '重新生成契约' : '生成契约'}</button>
    </div>
    {p && <div className="draft">
      <h3>将提交的契约</h3>
      {(draft.warnings ?? []).map((w: any) => <p key={w.code} className="warn-text">{WARN[w.code] ?? w.detail}</p>)}
      <dl className="kv">
        <dt>标题</dt><dd>{draft.title}</dd>
        <dt>目标</dt><dd className="pre">{p.goal}</dd>
        <dt>验收项</dt><dd>{p.acceptance?.length ? <ul>{p.acceptance.map((x: string, i: number) => <li key={i}>{x}</li>)}</ul> : '无'}</dd>
        <dt>执行方式</dt><dd>{p.harness === 'codex' ? 'Codex 在隔离工作区内修改文件' : '确定性步骤（不调用模型）'}</dd>
        {mp && <><dt>模型 / effort</dt><dd><b>{mp.resolved.model} / {mp.resolved.effort}</b>
          <small className="muted">（模型：{MODEL_SOURCE[mp.source.model] ?? mp.source.model}；effort：{MODEL_SOURCE[mp.source.effort] ?? mp.source.effort}；受理后不随默认值变化）</small></dd>
          <dt>派单策略</dt><dd>{DISPATCH[mp.unverified_nodes] ?? show(mp.unverified_nodes)}</dd></>}
        {draft.project_id && <><dt>项目</dt><dd>{proj?.name ?? short(draft.project_id)}（配置第 {show(draft.project_revision)} 版）</dd></>}
        <dt>输入</dt><dd>{p.inputs_manifest?.files?.length ?? 0} 个文件{draft.baseline_id ? <>，来自输入基线 {usedBaseline ? `${usedBaseline.name}（${bytes(usedBaseline.bytes)}）` : <code>{short(draft.baseline_id)}</code>}</> : ''}</dd>
        {p.workspace?.exclude && <><dt>排除规则</dt><dd><small>{p.workspace.exclude.join('，')}</small></dd></>}
        <dt>必需检查</dt><dd>{p.checks?.length ? p.checks.map((c: any) => <div key={c.name}><code>{c.name}</code>：<code>{c.run.join(' ')}</code></div>) : '无（验收不要求执行证据）'}</dd>
        <dt>协调</dt><dd>{COORD[p.coordination?.policy] ?? show(p.coordination?.policy)}</dd>
        <dt>发布</dt><dd>{PUBLISH(p)}</dd>
        <dt>执行平台</dt><dd>{p.requirements?.os?.length ? p.requirements.os.join('、') : '任意合格设备'}</dd>
        <dt>预算</dt><dd>最多 {p.budgets?.max_attempts} 次尝试、{p.budgets?.max_repairs} 次修复，{Math.round((p.budgets?.deadline_s ?? 0) / 60)} 分钟内完成</dd>
      </dl>
      <details><summary>完整契约（JSON）</summary><Json v={p} /></details>
      <div className="actions">
        <button className="primary" disabled={busy} onClick={() => void run(async () => {
          const job = await api.submit(draft.title, draft.contract, draft.settings_revision,
            draft.project_id ? { id: draft.project_id, revision: draft.project_revision } : undefined)
          onCreated(job.job_id)
        })}>{busy ? '提交中…' : '确认并提交'}</button>
      </div>
    </div>}
  </Section>
}
