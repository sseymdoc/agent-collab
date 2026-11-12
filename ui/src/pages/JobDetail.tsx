import { useRef, useState } from 'react'
import { api, coordHeld, pendingTakeOf } from '../api'
import { can, session } from '../session'
import { saveBlob } from '../transport'
import { Action, Badge, Json, MODEL_SOURCE, ORIGIN, Section, errText, show, short, useData, when } from '../ui'
import { JobLogs } from './JobLogs'
import { JobResult, ResultFacts } from './JobResult'

export function JobDetail({ id, tick }: { id: string; tick: number }) {
  const { data: d, error } = useData(() => api.job(id), [id, tick])
  const explain = useData(() => api.explain(id).catch(() => null), [id, tick])
  // 只为显示项目名；读不到时显示 ID。
  const project = useData(() => d?.job?.project_id ?? d?.project_id ? api.project(d.job?.project_id ?? d.project_id).catch(() => null) : Promise.resolve(null), [d?.job?.project_id ?? d?.project_id])
  const [older, setOlder] = useState<any[]>([])
  const [olderErr, setOlderErr] = useState<string | null>(null)
  const [olderDone, setOlderDone] = useState(false)
  // 打开页面后见过的事件都留在这里（按序号去重）：快照只带最近 200 条，窗口随新事件前移时，
  // 滑出窗口的事件不在快照里，也取不到（向前翻页从已载入的最早一条往前取），不留下就丢了。
  const seen = useRef(new Map<number, any>())
  // 刷新失败时保留已加载的页面（含表单输入），只在顶部提示。
  if (!d) return error ? <p className="err">{error}</p> : <p className="muted">加载中…</p>
  const job = d.job ?? d
  const attempts: any[] = d.attempts ?? []
  const checkpoints: any[] = d.checkpoints ?? []
  // 事件：快照带最近 200 条；更早的按需向前翻页。
  for (const e of [...older, ...(d.events ?? [])]) seen.current.set(e.seq, e)
  const events = [...seen.current.values()].sort((a, b) => a.seq - b.seq)
  const c = job.contract ?? {}
  const mp = job.model_policy
  const current = attempts.find(a => a.attempt_id === job.current_attempt_id)
  const progress = [...events].reverse().find(e => e.type === 'attempt.progress' && !e.stale && e.attempt_id === job.current_attempt_id)
  const finals = checkpoints.filter(k => k.kind === 'final' && k.state !== 'uploading' && k.attempt_id === job.current_attempt_id)
  const results: any[] | null = current?.report?.checks?.results ?? null
  // 与 controller 认定执行证据的规则一致：每个契约检查恰好一条同名结果，退出码 0 且明确未超时。
  const checks = !(c.checks ?? []).length ? ['muted', '契约未要求'] : !results ? ['muted', '尚未运行']
    : c.checks.every((k: any) => { const m = results.filter(r => r?.name === k.name); return m.length === 1 && m[0].exit_code === 0 && m[0].timed_out === false })
      ? ['good', '全部通过'] : ['bad', '未通过']
  const rounds = events.filter(e => e.type === 'coordinator.triggered').map(e => e.payload?.coordinator_job_id).filter(Boolean)
  const tasks = can('tasks')
  const projectName = project.data?.name
  return <>
    {error && <p className="err">刷新失败，以下为上次成功加载的内容：{error}</p>}
    <Section title={job.title || '任务 ' + short(job.job_id)} extra={<a href="#/jobs">返回列表</a>}>
      {/* 执行结束、检查通过、验收通过、成果发布、写回完成是五件事，各有各的状态。 */}
      <div className="badges">
        <span>运行 <Badge v={job.status} /></span>
        <span>检查 <span className={'badge ' + checks[0]}>{checks[1]}</span></span>
        <span>验收 <Badge v={job.acceptance?.status} /></span>
        <span>回写 <Badge v={job.integration_status} /></span>
        <span>恢复保护 <Badge v={job.protection} /></span>
      </div>
      <ResultFacts job={job} d={d} finals={finals} />
      {job.status_reason && <p className="warn-text">原因：{show(job.status_reason)}</p>}
      {job.acceptance?.requested_status && job.acceptance.requested_status !== job.acceptance.status &&
        <p className="warn-text">审核者请求“{show(job.acceptance.requested_status)}”，但缺少契约要求的执行证据，验收记为未验收：
          {(job.acceptance.evidence_gaps ?? []).map((g: any) => g.check + '（' + (GAP[g.reason] ?? g.reason) + '）').join('，')}</p>}
      {job.integration_status === 'integration_pending' && job.status === 'completed' &&
        <p className="warn-text">工件已完成并保存在控制服务（交付版本 {show(job.delivery_version)}）；回写源目录待目标设备 {show(c.integration?.target_node_id)} 处理，不影响已保存的产物。</p>}
      <dl className="kv">
        <dt>执行位置</dt><dd>{current ? <><a href="#/nodes"><b>{current.node_id}</b></a> <Badge v={current.status} /> <small className="muted">attempt {short(current.attempt_id)} · epoch {current.epoch}{['leased', 'running'].includes(current.status) ? ' · 租约至 ' + when(current.lease_expires_at) : ''}</small></>
          : '尚未分配设备'}</dd>
        <dt>协调位置</dt><dd>{c.coordinates ? '本任务就是协调回合，执行位置即协调位置' : rounds.length ? <Rounds ids={rounds} tick={tick} /> : '没有触发受管协调回合'}</dd>
        <dt>模型 / effort</dt><dd>{c.harness === 'stub' ? '不调用模型（确定性步骤）' : <>
          解析后 <b>{show(c.harness_params?.model)} / {show(c.harness_params?.effort)}</b>
          {mp ? <small className="muted">（模型：{MODEL_SOURCE[mp.source?.model] ?? show(mp.source?.model)}；effort：{MODEL_SOURCE[mp.source?.effort] ?? show(mp.source?.effort)}）</small>
            : <small className="muted">（升级前受理，未记录来源）</small>}
          <br /><small>请求 {mp ? `${mp.requested?.model ?? '未指定'} / ${mp.requested?.effort ?? '未指定'}` : '未知'} ·
            CLI 实际观测 {show(current?.runtime?.observed?.model)} / {show(current?.runtime?.observed?.effort ?? current?.runtime?.observed?.reasoning_effort)}</small></>}</dd>
        {job.project_id && <><dt>项目</dt><dd><a href={'#/projects/' + job.project_id}>{projectName ?? short(job.project_id)}</a>
          <small className="muted"> · 受理时项目配置第 {show(job.project_revision ?? mp?.project_revision)} 版</small></dd></>}
        <dt>来源</dt><dd>{job.created_by ? (job.created_by.name ?? ORIGIN[job.created_by.kind] ?? job.created_by.kind) : '未知（升级前受理）'} · 创建 {when(job.created_at)}</dd>
        <dt>job_id / 根工作包</dt><dd><code>{job.job_id}</code> / <code>{job.work_package_id}</code></dd>
        <dt>可计数进度</dt><dd>{progress?.payload?.total ? `${progress.payload.done} / ${progress.payload.total}` : '无可计数阶段（不根据模型文本推测）'}</dd>
        <dt>最后可恢复时间</dt><dd>{when(job.durable_checkpoint_at)}{job.durable_checkpoint_id ? <> · <code>{short(job.durable_checkpoint_id)}</code></> : null}</dd>
        <dt>按最后已复制检查点的 RPO</dt><dd>{!ACTIVE.includes(job.status) ? '不适用（当前无执行中的 attempt）'
          : job.durable_checkpoint_at ? Math.round((Date.now() - Date.parse(job.durable_checkpoint_at)) / 1000) + ' 秒（故障时可能需重做此后的工作）'
          : '尚无已复制检查点；恢复需从输入基线重做或无法自动恢复'}</dd>
        <dt>取消</dt><dd>{job.cancel?.requested_at ? <>已请求 {when(job.cancel.requested_at)}；{job.cancel.confirmed_at ? <>已确认停止 {when(job.cancel.confirmed_at)}</> : <b>停止尚未确认</b>}</> : '无'}</dd>
      </dl>
    </Section>

    <Section title="操作" extra={!tasks && <span className="muted">这条连接没有任务权限（tasks），以下操作不可用</span>}>
      <div className="actions">
        <Action label="请求取消" scope="tasks" danger disabled={['cancelled', 'failed', 'completed', 'cancel_requested'].includes(job.status)} confirm="取消不会触发自动恢复。确定请求取消？" run={() => api.cancel(job.job_id, job.version)} />
        <Retry job={job} />
        {finals.map(k => <Action key={k.checkpoint_id} label={'发布检查点 ' + short(k.checkpoint_id) + '（基于交付版本 ' + job.delivery_version + '）'}
          scope="tasks" disabled={!current || job.delivery_checkpoint_id === k.checkpoint_id} run={() => api.publish(job.job_id, job.work_package_id, job.version, current, k.checkpoint_id, job.delivery_version)} />)}
      </div>
      {c.publish?.requires_acceptance && !job.delivery_version && <p className="muted">本任务要求验收通过后才能发布；验收通过不会自动发布。</p>}
      {tasks && <Assess job={job} checkpoints={checkpoints} />}
    </Section>

    <Coordinator job={job} tick={tick} />

    <Section title="成果文件" extra={<span className="muted">来自已确认的检查点；内容按文本显示，不会被执行</span>}>
      {finals.length || job.delivery_checkpoint_id ? [...new Set([job.delivery_checkpoint_id, ...finals.map(k => k.checkpoint_id)].filter(Boolean))].map(cp =>
        <Files key={cp} cp={cp} label={cp === job.delivery_checkpoint_id ? '已发布的交付（版本 ' + job.delivery_version + '）' : '最终检查点 ' + short(cp) + '（未发布）'} />)
        : <p className="muted">还没有最终检查点。</p>}
    </Section>

    <JobResult job={job} finals={finals} />

    <Section title="契约（受理后不可变）">
      <dl className="kv">
        <dt>目标</dt><dd className="pre">{show(c.goal)}</dd>
        <dt>验收项</dt><dd><ul>{(c.acceptance ?? []).map((x: string, i: number) => <li key={i}>{x}</li>)}</ul></dd>
        <dt>不包含</dt><dd>{(c.excludes ?? []).join('；') || '无'}</dd>
        <dt>harness / 请求参数</dt><dd>{show(c.harness)} · <code>{JSON.stringify(c.harness_params ?? {})}</code></dd>
        <dt>硬性要求</dt><dd><code>{JSON.stringify(c.requirements ?? {})}</code></dd>
        <dt>恢复策略</dt><dd>自动恢复 {show(c.recovery?.auto)} · 副作用 {show(c.recovery?.side_effects)}</dd>
        <dt>预算（已用 / 上限）</dt><dd>尝试 {show(job.counters?.attempts)}/{show(c.budgets?.max_attempts)} · 基础设施重试 {show(job.counters?.infra_retries)} · 迁移 {show(job.counters?.migrations)}/{show(c.budgets?.max_migrations)} · 修复 {show(job.counters?.repairs)}/{show(c.budgets?.max_repairs)}<small className="muted">（同一根工作包的各次修订累计）</small></dd>
        <dt>发布</dt><dd>{show(c.publish?.mode)} · 需验收 {show(c.publish?.requires_acceptance)}</dd>
        <dt>快照排除</dt><dd>{(c.workspace?.exclude ?? ['__pycache__/**', '*.pyc', '.pytest_cache/**', '.DS_Store（默认）']).join('，')}</dd>
        <dt>必需检查（由设备执行并留证据）</dt><dd>{(c.checks ?? []).length ? (c.checks as any[]).map(k => <div key={k.name}><code>{k.name}</code>：<code>{(k.run ?? []).join(' ')}</code></div>) : '无（验收不要求执行证据）'}</dd>
        <dt>协调策略</dt><dd>{POLICY[c.coordination?.policy ?? 'always'] ?? show(c.coordination?.policy)}</dd>
      </dl>
    </Section>

    <Section title="attempt 谱系">
      <table><thead><tr><th>epoch</th><th>attempt</th><th>设备</th><th>状态</th><th>失败分类</th><th>恢复自</th><th>起点</th><th>会话</th><th>请求 / 实际模型与 effort</th><th>时间</th></tr></thead>
        <tbody>{attempts.map(a => <tr key={a.attempt_id} className={a.attempt_id === job.current_attempt_id ? 'current' : ''}>
          <td>{a.epoch}</td><td><code>{short(a.attempt_id)}</code></td><td>{a.node_id ?? '未分配'}</td><td><Badge v={a.status} /></td>
          <td>{a.failure_class ?? (a.status === 'succeeded' ? '—' : show(a.failure_class))}{a.failure_detail ? <><br /><small>{show(a.failure_detail)}</small></> : null}</td>
          <td>{a.recovered_from_attempt ? <code>{short(a.recovered_from_attempt)}</code> : '—'}</td>
          <td>{a.start_checkpoint_id ? <>从检查点 <code>{short(a.start_checkpoint_id)}</code> 新建</> : a.start_manifest_sha256 ? '输入基线' : '空工作区'}</td>
          <td>{sessionMode(a)}</td>
          <td><small>{c.harness === 'stub' ? '— （确定性步骤，无模型）' : <>请求 {show(a.runtime?.model)} / {show(a.runtime?.effort)}<br />实际 {show(a.runtime?.observed?.model)} / {show(a.runtime?.observed?.effort ?? a.runtime?.observed?.reasoning_effort)}</>}</small></td>
          <td><small>创建 {when(a.created_at)}<br />租约至 {when(a.lease_expires_at)}{a.ended_at ? <><br />结束 {when(a.ended_at)}</> : null}</small></td>
        </tr>)}</tbody></table>
    </Section>

    <Section title="执行报告与检查摘要" extra={<span className="muted">设备上报的事实（已脱敏、限长）；不是验收结论</span>}>
      <p className="muted">检查摘要是每项检查的退出码与标准输出、标准错误的尾部（各不超过 4 KiB，可能被截断）；完整输出见下面“原始输出”。</p>
      {attempts.filter(a => a.report).length ? attempts.filter(a => a.report).map(a => <Report key={a.attempt_id} a={a} />)
        : <p className="muted">尚无执行报告。</p>}
    </Section>

    <JobLogs job={job} attempts={attempts} />

    <Section title="检查点">
      <table><thead><tr><th>检查点</th><th>attempt / epoch</th><th>类型</th><th>状态</th><th>缺失 blob</th><th>已完成 / 未完成</th><th>时间</th><th></th></tr></thead>
        <tbody>{checkpoints.map(k => <tr key={k.checkpoint_id}>
          <td><code>{short(k.checkpoint_id)}</code></td><td><code>{short(k.attempt_id)}</code> / {k.epoch}</td><td>{k.kind}</td><td><Badge v={k.state} /></td>
          <td>{(k.missing_blobs ?? []).length}</td>
          <td><small>{(k.summary?.completed ?? []).length} / {(k.summary?.pending ?? []).length}</small></td>
          <td><small>{when(k.created_at)}{k.durable_at ? <><br />确认 {when(k.durable_at)}</> : null}</small></td>
          <td><Diff cp={k.checkpoint_id} /></td>
        </tr>)}</tbody></table>
      {!checkpoints.length && <p className="muted">尚无检查点。</p>}
    </Section>

    <Section title="发布与验收">
      <table><thead><tr><th>交付版本</th><th>attempt / epoch</th><th>检查点</th><th>基于版本</th><th>时间</th></tr></thead>
        <tbody>{(d.publications ?? []).map((p: any) => <tr key={p.delivery_version}><td>{p.delivery_version}</td><td><code>{short(p.attempt_id)}</code> / {p.epoch}</td><td><code>{short(p.checkpoint_id)}</code></td><td>{p.expected_base_version}</td><td>{when(p.created_at)}</td></tr>)}</tbody></table>
      <table><thead><tr><th>验收</th><th>检查点</th><th>状态</th><th>执行证据</th><th>方法</th><th>结果</th><th>审核者</th><th>时间</th></tr></thead>
        <tbody>{(d.assessments ?? []).map((x: any) => <tr key={x.assessment_id}><td><code>{short(x.assessment_id)}</code></td><td><code>{short(x.checkpoint_id)}</code></td>
          <td><Badge v={x.status} />{x.requested_status && x.requested_status !== x.status ? <><br /><small>请求 {x.requested_status}</small></> : null}</td>
          <td><small>{x.evidence ? <>attempt <code>{short(x.evidence.attempt_id)}</code> 的检查 {(x.evidence.checks ?? []).join('，')} 全部通过</>
            : (x.evidence_gaps ?? []).length ? (x.evidence_gaps as any[]).map(g => g.check + '：' + (GAP[g.reason] ?? g.reason)).join('，') : '契约未要求'}</small></td>
          <td>{x.method}</td><td>{x.result}</td><td>{x.reviewer}</td><td>{when(x.created_at ?? x.recorded_at)}</td></tr>)}</tbody></table>
    </Section>

    <Section title="调度解释" extra={<span className="muted">按当前设备状态重新计算，不是历史派单记录</span>}>
      {!SCHEDULABLE.includes(job.status) ? <p className="muted">当前状态不需要调度（{show(job.status)}）；历史派单见 attempt 谱系。</p>
        : explain.data ? <table><thead><tr><th>设备</th><th>合格</th><th>原因</th><th>模型目录</th><th>评分</th></tr></thead>
        <tbody>{(explain.data.nodes ?? explain.data.candidates ?? []).map((n: any) => <tr key={n.node_id}>
          <td>{n.node_id}{(explain.data.selected === n.node_id || explain.data.selected_node_id === n.node_id) ? ' ✓ 选中' : ''}</td><td>{show(n.eligible)}</td>
          <td><small>{(n.reasons ?? []).map((r: string) => REASON(r)).join('，') || '—'}</small></td>
          <td><small>{n.model_catalog ? CATALOG[n.model_catalog] ?? n.model_catalog : '—'}</small></td>
          <td><small>{show(n.score)} {n.breakdown ? JSON.stringify(n.breakdown) : ''}</small></td>
        </tr>)}</tbody></table> : <p className="muted">无调度解释。</p>}
    </Section>

    <Section title="产品事件" extra={<span className="muted">已载入 {events.length} 条；stale 为旧 epoch 迟到事件，仅存档</span>}>
      <p className="muted">controller 记录的状态变化与节点上报的进度，不是 CLI 的完整输出。</p>
      <ol className="timeline">{[...events].reverse().map(e => <li key={e.seq} className={e.stale ? 'stale' : ''}>
        <span className="muted">#{e.seq} {when(e.created_at)}</span> <b>{e.type}</b> {e.attempt_id ? <code>{short(e.attempt_id)}</code> : null} {e.stale ? <span className="badge warn">stale</span> : null}
        {e.payload && Object.keys(e.payload).length ? <details><summary>payload</summary><Json v={e.payload} /></details> : null}
      </li>)}</ol>
      {/* 长日志不一次性载入：每次向前取 200 条。 */}
      {events.length >= 200 && <p className="actions">{olderDone ? <small className="muted">没有更早的事件了</small> : <><button onClick={() => api.jobEvents(id, events[0].seq).then(
        r => { setOlderErr(null); if (r.events.length) setOlder(o => [...r.events, ...o]); else setOlderDone(true) },
        e => setOlderErr(errText(e)))}>加载更早的事件</button>{olderErr && <small className="muted">{olderErr}</small>}</>}</p>}
    </Section>
  </>
}

// 受管协调回合各自在哪台设备上、结论是什么：位置取自回合任务的有效 attempt，不由界面推测。
function Rounds({ ids, tick }: { ids: string[]; tick: number }) {
  const rounds = useData(() => Promise.all(ids.map(i => api.job(i).catch(() => null))), [ids.join(','), tick])
  if (!rounds.data) return <span className="muted">读取协调回合…</span>
  return <>{rounds.data.map((r: any, i: number) => {
    if (!r) return <div key={ids[i]}>回合 <code>{short(ids[i])}</code>：读取失败</div>
    const a = (r.attempts ?? []).find((x: any) => x.attempt_id === r.current_attempt_id)
    return <div key={r.job_id}><a href={'#/jobs/' + r.job_id}>回合 {short(r.job_id)}</a> <Badge v={r.status} /> {a ? <>在 <b>{a.node_id}</b></> : '尚未分配设备'}
      <small className="muted"> · {show(r.contract?.harness_params?.model)} / {show(r.contract?.harness_params?.effort)}</small>
      {a?.report?.decision ? <> · 结论 <b>{a.report.decision}</b>{a.report.notes ? '：' + a.report.notes : ''}</> : null}</div>
  })}</>
}

// 检查点里的文件清单；下载由传输层完成（桌面端经系统“另存为”，内容校验哈希后落盘）。
function Files({ cp, label }: { cp: string; label: string }) {
  const [open, setOpen] = useState(false)
  const files = useData(() => open ? api.files(cp) : Promise.resolve(null), [cp, open])
  const [preview, setPreview] = useState<{ path: string; text: string } | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  return <details onToggle={e => setOpen((e.target as HTMLDetailsElement).open)}>
    <summary>{label}</summary>
    {files.error && <p className="err">{files.error}</p>}
    {msg && <p className="muted">{msg}</p>}
    {files.data && <table><thead><tr><th>路径</th><th>大小</th><th></th></tr></thead>
      <tbody>{(files.data.files as any[]).map(f => <tr key={f.path}>
        <td><code>{f.path}</code></td><td>{f.size} 字节</td>
        <td className="actions">
          <button className="link" onClick={() => saveBlob(f.sha256, f.path.split('/').pop()).then(p => setMsg(p ? '已保存：' + p : '已取消保存'), e => setMsg(e?.message ?? String(e)))}>下载</button>
          {f.size <= 65536 && <button className="link" onClick={() => api.blobText(f.sha256).then(text => setPreview({ path: f.path, text }), e => setMsg(errText(e)))}>查看文本</button>}
        </td></tr>)}</tbody></table>}
    {files.data && !files.data.files.length && <p className="muted">这个检查点没有文件。</p>}
    {preview && <><p className="muted">{preview.path}（按纯文本显示）<button className="link" onClick={() => setPreview(null)}>关闭</button></p><pre className="json">{preview.text}</pre></>}
  </details>
}

// 协调权：根工作包级独占。持有时本页的验收、发布、迁移请求自动附带令牌；接管需写明原因并留事件。
function Coordinator({ job, tick }: { job: any; tick: number }) {
  const wp = job.work_package_id
  const lease = useData(() => api.coordinator(wp), [wp, tick])
  const [reason, setReason] = useState('')
  const l = lease.data
  const held = coordHeld(wp)
  // 只凭本地令牌不够：别人接管后旧令牌仍在。租约的（恢复代次, 版本）与取得时一致才算本页持有；旧记录缺代次时不算。
  const mine = !!(held && l && held.version === l.version && held.generation === l.authority_generation)
  const pending = pendingTakeOf(wp)
  const target = job.contract?.coordinates?.job_id
  const holder = session()?.identity.name ?? 'web-console'
  return <Section title="协调权" extra={<span className="muted">同一根工作包同时只允许一个协调者（交互或受管 Claude）</span>}>
    {target && <p>本任务是受管协调回合，目标任务：<a href={'#/jobs/' + target}>{short(target)}</a></p>}
    {lease.error && <p className="err">{lease.error}</p>}
    <p>{l ? <>当前持有：<b>{l.holder_kind === 'managed' ? '受管协调者' : '交互协调者'}</b>（{show(l.holder_ref)}），到期 {when(l.expires_at)}{mine ? '；由本窗口持有' : ''}</>
      : '当前无人持有协调权。'}</p>
    {held && l && !mine && <p className="warn-text">本窗口之前取得的协调令牌已失效（已被接管或已过期）。</p>}
    {pending && !mine && <p className="warn-text">上次{pending.takeover ? '接管' : '取得'}请求没有收到响应，可能已在服务端生效。
      “恢复”会原样重发那次请求：若已生效，本窗口取回同一令牌，不会再记一次接管；若未生效，按原请求重新处理。</p>}
    {can('tasks') && <div className="actions">
      {/* 取得租约不发事件：完成后主动重读（失败也重读，以便显示“恢复”入口）。 */}
      {pending && !mine && <Action label="恢复上次未确认的请求" run={() => api.resumeTake(wp).finally(lease.reload)} />}
      {!l && <Action label="取得协调权" run={() => api.takeCoordinator(wp, false, '', holder).finally(lease.reload)} />}
      {l && !mine && can('maintain') && <span className="inline-form">
        <input placeholder="接管原因（必填，写入事件）" value={reason} onChange={e => setReason(e.target.value)} />
        <Action label="接管协调权" danger disabled={!reason.trim()} confirm="接管会使当前协调者的令牌失效，其后续验收/发布将被拒绝。确定？"
          run={() => api.takeCoordinator(wp, true, reason.trim(), holder).finally(lease.reload)} />
      </span>}
      {mine && <Action label="释放协调权" run={() => api.releaseCoordinator(wp).finally(lease.reload)} />}
    </div>}
  </Section>
}

const GAP: Record<string, string> = { missing: '未执行', failed: '未通过', timed_out: '超时', manifest_mismatch: '证据不属于被验收版本', no_report: '没有执行报告' }
const POLICY: Record<string, string> = { always: '每次结束都做语义验收', on_failure: '仅失败或发生接管时语义介入', none: '只用确定性检查，不触发受管协调' }
const CATALOG: Record<string, string> = { ok: '已上报', legacy: '旧版本设备（固定组合）', unknown: '未知（未据此排除）' }
// 调度原因由 controller 给出；这里只把模型相关的两类换成说明文字，其余原样显示。
const REASON = (r: string) => r.startsWith('model_unsupported:') ? '设备的模型目录里没有 ' + r.slice(18)
  : r.startsWith('effort_unsupported:') ? '设备上该模型不支持这个 effort（' + r.slice(19) + '）' : r
const ACTIVE = ['running', 'recovering', 'cancel_requested']
const SCHEDULABLE = ['queued', 'waiting_capability', 'recovering']

function Report({ a }: { a: any }) {
  const r = a.report
  return <details open>
    <summary>attempt <code>{short(a.attempt_id)}</code>（epoch {a.epoch}，{a.node_id}）· {show(r.harness)}{r.truncated ? ' · 已截断' : ''}</summary>
    {r.steps && <table><thead><tr><th>#</th><th>步骤</th><th>状态</th><th>退出码</th><th>输出尾部</th></tr></thead>
      <tbody>{r.steps.map((s: any) => <tr key={s.index}><td>{s.index}</td><td>{s.op}</td><td>{s.status === 'skipped_from_checkpoint' ? '从检查点跳过' : s.status}</td>
        <td>{s.exit_code ?? '—'}</td><td>{(s.stdout_tail || s.stderr_tail) ? <pre className="json">{[s.stdout_tail, s.stderr_tail].filter(Boolean).join('\n--- stderr ---\n')}</pre> : '—'}</td></tr>)}</tbody></table>}
    {r.checks && <><p className="muted">设备执行的契约检查（{show(r.checks.node_id)} · {show(r.checks.platform?.os)} {show(r.checks.platform?.release)} · Python {show(r.checks.platform?.python)}；绑定 manifest <code>{short(r.checks.manifest_sha256)}</code>）</p>
      <table><thead><tr><th>检查</th><th>命令</th><th>退出码</th><th>耗时</th><th>输出尾部</th></tr></thead>
        <tbody>{(r.checks.results ?? []).map((k: any) => <tr key={k.name}><td>{k.name}</td><td><code>{(k.argv ?? []).join(' ')}</code></td>
          <td>{k.timed_out ? '超时' : show(k.exit_code)}</td><td>{show(k.duration_s)} 秒</td>
          <td>{(k.stdout_tail || k.stderr_tail) ? <pre className="json">{[k.stdout_tail, k.stderr_tail].filter(Boolean).join('\n--- stderr ---\n')}</pre> : '—'}</td></tr>)}</tbody></table></>}
    {r.final_message && <><p className="muted">Codex 最终回复（尾部，模型自述，不等于检查证据）</p><pre className="json">{r.final_message}</pre></>}
    {r.decision && <p>协调决策：<b>{r.decision}</b>{r.notes ? ' — ' + r.notes : ''}</p>}
  </details>
}

function sessionMode(a: any) {
  const r = a.native_session_ref
  if (!r) return '未记录'
  if (r.mode === 'resume') return '原线程恢复'
  if (r.mode === 'new') return '新线程'
  return show(r.session_id ?? r)
}

function Retry({ job }: { job: any }) {
  const [reason, setReason] = useState('')
  const [exclude, setExclude] = useState('')
  return <span className="inline-form">
    <input placeholder="迁移/续接原因（必填）" value={reason} onChange={e => setReason(e.target.value)} />
    <input placeholder="排除设备，逗号分隔" value={exclude} onChange={e => setExclude(e.target.value)} />
    <Action label="明确迁移/续接" scope="tasks" disabled={!reason.trim()} confirm="将计入迁移预算，从最近确认检查点在新 attempt 中继续。确定？"
      run={() => api.retry(job.job_id, job.work_package_id, job.version, reason.trim(), exclude.split(',').map(s => s.trim()).filter(Boolean))} />
  </span>
}

function Assess({ job, checkpoints }: { job: any; checkpoints: any[] }) {
  const usable = checkpoints.filter(k => k.state !== 'uploading')
  const [cp, setCp] = useState('')
  const [status, setStatus] = useState('passed')
  const [method, setMethod] = useState('')
  const [result, setResult] = useState('')
  return <details className="assess"><summary>记录验收</summary>
    <p className="muted">验收绑定检查点的 manifest 版本；只做了局部检查时不要提交整体通过，在结果中写明覆盖范围。</p>
    <label>检查点<select value={cp} onChange={e => setCp(e.target.value)}><option value="">选择</option>
      {usable.map(k => <option key={k.checkpoint_id} value={k.checkpoint_id}>{short(k.checkpoint_id)} · {k.kind} · {k.state}</option>)}</select></label>
    <label>结论<select value={status} onChange={e => setStatus(e.target.value)}>{['passed', 'rework', 'blocked', 'unverified'].map(s => <option key={s}>{s}</option>)}</select></label>
    <label>方法<input value={method} onChange={e => setMethod(e.target.value)} placeholder="实际执行的测试或比对" /></label>
    <label>结果与覆盖范围<textarea value={result} onChange={e => setResult(e.target.value)} /></label>
    <Action label="提交验收" disabled={!cp || !method.trim() || !result.trim()}
      run={() => api.assess(job.job_id, job.work_package_id, job.version, { checkpoint_id: cp, status, method: method.trim(), result: result.trim(), reviewer: session()?.identity.name ?? '控制台用户' })} />
  </details>
}

function Diff({ cp }: { cp: string }) {
  const [d, setD] = useState<any>(null)
  const [err, setErr] = useState<string | null>(null)
  if (d) return <details open><summary>diff</summary><small>新增 {d.added?.length ?? 0}：{(d.added ?? []).join(', ')}<br />修改 {d.modified?.length ?? 0}：{(d.modified ?? []).join(', ')}<br />删除 {d.deleted?.length ?? 0}：{(d.deleted ?? []).join(', ')}</small></details>
  return <><button className="link" onClick={() => api.diff(cp, 'inputs').then(setD, e => setErr(errText(e)))}>对比输入</button>{err && <small className="err">{err}</small>}</>
}
