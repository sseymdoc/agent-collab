import { useState } from 'react'
import { api, blobUrl, coordHeld } from '../api'
import { Action, Badge, Json, Section, errText, show, short, useData, when } from '../ui'

export function JobDetail({ id, tick }: { id: string; tick: number }) {
  const { data: d, error } = useData(() => api.job(id), [id, tick])
  const explain = useData(() => api.explain(id).catch(() => null), [id, tick])
  // 刷新失败时保留已加载的页面（含表单输入），只在顶部提示。
  if (!d) return error ? <p className="err">{error}</p> : <p className="muted">加载中…</p>
  const job = d.job ?? d
  const attempts: any[] = d.attempts ?? []
  const checkpoints: any[] = d.checkpoints ?? []
  const events: any[] = d.events ?? []
  const c = job.contract ?? {}
  const current = attempts.find(a => a.attempt_id === job.current_attempt_id)
  const progress = [...events].reverse().find(e => e.type === 'attempt.progress' && !e.stale && e.attempt_id === job.current_attempt_id)
  const finals = checkpoints.filter(k => k.kind === 'final' && k.state !== 'uploading' && k.attempt_id === job.current_attempt_id)
  return <>
    {error && <p className="err">刷新失败，以下为上次成功加载的内容：{error}</p>}
    <Section title={job.title || '工作包 ' + short(job.job_id)} extra={<a href="#/jobs">返回列表</a>}>
      <div className="badges">
        <span>运行 <Badge v={job.status} /></span>
        <span>恢复保护 <Badge v={job.protection} /></span>
        <span>验收 <Badge v={job.acceptance?.status} /></span>
        <span>回写 <Badge v={job.integration_status} /></span>
        <span>交付版本 <b>{show(job.delivery_version)}</b></span>
      </div>
      {job.status_reason && <p className="warn-text">原因：{show(job.status_reason)}</p>}
      {job.acceptance?.requested_status && job.acceptance.requested_status !== job.acceptance.status &&
        <p className="warn-text">审核者请求“{show(job.acceptance.requested_status)}”，但缺少契约要求的执行证据，验收记为未验收：
          {(job.acceptance.evidence_gaps ?? []).map((g: any) => g.check + '（' + (GAP[g.reason] ?? g.reason) + '）').join('，')}</p>}
      {job.integration_status === 'integration_pending' && job.status === 'completed' &&
        <p className="warn-text">工件已完成并保存在控制服务（交付版本 {show(job.delivery_version)}）；回写源目录待目标节点 {show(c.integration?.target_node_id)} 处理，不影响已保存的产物。</p>}
      <dl className="kv">
        <dt>job_id / 根工作包</dt><dd><code>{job.job_id}</code> / <code>{job.work_package_id}</code></dd>
        <dt>当前 epoch / attempt</dt><dd>{show(job.current_epoch)} / <code>{show(job.current_attempt_id)}</code>{current ? <> 在 <b>{current.node_id}</b> <Badge v={current.status} /></> : null}</dd>
        <dt>可计数进度</dt><dd>{progress?.payload?.total ? `${progress.payload.done} / ${progress.payload.total}` : '无可计数阶段（不根据模型文本推测）'}</dd>
        <dt>最后可恢复时间</dt><dd>{when(job.durable_checkpoint_at)}{job.durable_checkpoint_id ? <> · <code>{short(job.durable_checkpoint_id)}</code></> : null}</dd>
        <dt>按最后已复制检查点的 RPO</dt><dd>{!ACTIVE.includes(job.status) ? '不适用（当前无执行中的 attempt）'
          : job.durable_checkpoint_at ? Math.round((Date.now() - Date.parse(job.durable_checkpoint_at)) / 1000) + ' 秒（故障时可能需重做此后的工作）'
          : '尚无已复制检查点；恢复需从输入基线重做或无法自动恢复'}</dd>
        <dt>取消</dt><dd>{job.cancel?.requested_at ? <>已请求 {when(job.cancel.requested_at)}；{job.cancel.confirmed_at ? <>已确认停止 {when(job.cancel.confirmed_at)}</> : <b>停止尚未确认</b>}</> : '无'}</dd>
      </dl>
    </Section>

    <Section title="操作">
      <div className="actions">
        <Action label="请求取消" danger disabled={['cancelled', 'failed', 'completed', 'cancel_requested'].includes(job.status)} confirm="取消不会触发自动恢复。确定请求取消？" run={() => api.cancel(job.job_id, job.version)} />
        <Retry job={job} />
        {finals.map(k => <Action key={k.checkpoint_id} label={'发布检查点 ' + short(k.checkpoint_id) + '（基于交付版本 ' + job.delivery_version + '）'}
          disabled={!current || job.delivery_checkpoint_id === k.checkpoint_id} run={() => api.publish(job.job_id, job.work_package_id, job.version, current, k.checkpoint_id, job.delivery_version)} />)}
      </div>
      <Assess job={job} checkpoints={checkpoints} />
    </Section>

    <Coordinator job={job} tick={tick} />

    <Section title="契约（受理后不可变）">
      <dl className="kv">
        <dt>目标</dt><dd>{show(c.goal)}</dd>
        <dt>验收项</dt><dd><ul>{(c.acceptance ?? []).map((x: string, i: number) => <li key={i}>{x}</li>)}</ul></dd>
        <dt>不包含</dt><dd>{(c.excludes ?? []).join('；') || '无'}</dd>
        <dt>harness / 请求参数</dt><dd>{show(c.harness)} · <code>{JSON.stringify(c.harness_params ?? {})}</code></dd>
        <dt>硬性要求</dt><dd><code>{JSON.stringify(c.requirements ?? {})}</code></dd>
        <dt>恢复策略</dt><dd>自动恢复 {show(c.recovery?.auto)} · 副作用 {show(c.recovery?.side_effects)}</dd>
        <dt>预算（已用 / 上限）</dt><dd>尝试 {show(job.counters?.attempts)}/{show(c.budgets?.max_attempts)} · 基础设施重试 {show(job.counters?.infra_retries)} · 迁移 {show(job.counters?.migrations)}/{show(c.budgets?.max_migrations)} · 修复 {show(job.counters?.repairs)}/{show(c.budgets?.max_repairs)}</dd>
        <dt>发布</dt><dd>{show(c.publish?.mode)} · 需验收 {show(c.publish?.requires_acceptance)}</dd>
        <dt>快照排除</dt><dd>{(c.workspace?.exclude ?? ['__pycache__/**', '*.pyc', '.pytest_cache/**', '.DS_Store（默认）']).join('，')}</dd>
        <dt>必需检查（由节点执行并留证据）</dt><dd>{(c.checks ?? []).length ? (c.checks as any[]).map(k => <div key={k.name}><code>{k.name}</code>：<code>{(k.run ?? []).join(' ')}</code></div>) : '无（验收不要求执行证据）'}</dd>
        <dt>协调策略</dt><dd>{POLICY[c.coordination?.policy ?? 'always'] ?? show(c.coordination?.policy)}</dd>
      </dl>
    </Section>

    <Section title="attempt 谱系">
      <table><thead><tr><th>epoch</th><th>attempt</th><th>节点</th><th>状态</th><th>失败分类</th><th>恢复自</th><th>起点</th><th>会话</th><th>请求 / 实际模型与 effort</th><th>时间</th></tr></thead>
        <tbody>{attempts.map(a => <tr key={a.attempt_id} className={a.attempt_id === job.current_attempt_id ? 'current' : ''}>
          <td>{a.epoch}</td><td><code>{short(a.attempt_id)}</code></td><td>{a.node_id ?? '未分配'}</td><td><Badge v={a.status} /></td>
          <td>{a.failure_class ?? (a.status === 'succeeded' ? '—' : show(a.failure_class))}{a.failure_detail ? <><br /><small>{show(a.failure_detail)}</small></> : null}</td>
          <td>{a.recovered_from_attempt ? <code>{short(a.recovered_from_attempt)}</code> : '—'}</td>
          <td>{a.start_checkpoint_id ? <>从检查点 <code>{short(a.start_checkpoint_id)}</code> 新建</> : a.start_manifest_sha256 ? '输入基线' : '空工作区'}</td>
          <td>{sessionMode(a)}</td>
          <td><small>{c.harness === 'stub' ? '— （确定性桩，无模型）' : <>请求 {show(c.harness_params?.model)} / {show(c.harness_params?.effort)}<br />实际 {show(a.runtime?.observed?.model)} / {show(a.runtime?.observed?.effort ?? a.runtime?.observed?.reasoning_effort)}</>}</small></td>
          <td><small>创建 {when(a.created_at)}<br />租约至 {when(a.lease_expires_at)}{a.ended_at ? <><br />结束 {when(a.ended_at)}</> : null}</small></td>
        </tr>)}</tbody></table>
    </Section>

    <Section title="执行报告" extra={<span className="muted">节点上报的事实（已脱敏、限长）；不是验收结论</span>}>
      {attempts.filter(a => a.report).length ? attempts.filter(a => a.report).map(a => <Report key={a.attempt_id} a={a} />)
        : <p className="muted">尚无执行报告。</p>}
    </Section>

    <Section title="检查点">
      <table><thead><tr><th>检查点</th><th>attempt / epoch</th><th>类型</th><th>状态</th><th>缺失 blob</th><th>已完成 / 未完成</th><th>时间</th><th></th></tr></thead>
        <tbody>{checkpoints.map(k => <tr key={k.checkpoint_id}>
          <td><code>{short(k.checkpoint_id)}</code></td><td><code>{short(k.attempt_id)}</code> / {k.epoch}</td><td>{k.kind}</td><td><Badge v={k.state} /></td>
          <td>{(k.missing_blobs ?? []).length}</td>
          <td><small>{(k.summary?.completed ?? []).length} / {(k.summary?.pending ?? []).length}</small></td>
          <td><small>{when(k.created_at)}{k.durable_at ? <><br />确认 {when(k.durable_at)}</> : null}</small></td>
          <td>{k.manifest_sha256 && <a href={blobUrl(k.manifest_sha256)} target="_blank" rel="noreferrer">manifest</a>} <Diff cp={k.checkpoint_id} /></td>
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

    <Section title="调度解释" extra={<span className="muted">按当前节点状态重新计算，不是历史派单记录</span>}>
      {!SCHEDULABLE.includes(job.status) ? <p className="muted">当前状态不需要调度（{show(job.status)}）；历史派单见 attempt 谱系。</p>
        : explain.data ? <table><thead><tr><th>节点</th><th>合格</th><th>原因</th><th>评分</th></tr></thead>
        <tbody>{(explain.data.nodes ?? explain.data.candidates ?? []).map((n: any) => <tr key={n.node_id}>
          <td>{n.node_id}{(explain.data.selected === n.node_id || explain.data.selected_node_id === n.node_id) ? ' ✓ 选中' : ''}</td><td>{show(n.eligible)}</td>
          <td><small>{(n.reasons ?? []).join('，') || '—'}</small></td><td><small>{show(n.score)} {n.breakdown ? JSON.stringify(n.breakdown) : ''}</small></td>
        </tr>)}</tbody></table> : <p className="muted">无调度解释。</p>}
    </Section>

    <Section title="事件时间线" extra={<span className="muted">最近 {events.length} 条；stale 为旧 epoch 迟到事件，仅存档</span>}>
      <ol className="timeline">{[...events].reverse().map(e => <li key={e.seq} className={e.stale ? 'stale' : ''}>
        <span className="muted">#{e.seq} {when(e.created_at)}</span> <b>{e.type}</b> {e.attempt_id ? <code>{short(e.attempt_id)}</code> : null} {e.stale ? <span className="badge warn">stale</span> : null}
        {e.payload && Object.keys(e.payload).length ? <details><summary>payload</summary><Json v={e.payload} /></details> : null}
      </li>)}</ol>
    </Section>
  </>
}

// 协调权：根工作包级独占。持有时本页的验收、发布、迁移请求自动附带令牌；接管需写明原因并留事件。
function Coordinator({ job, tick }: { job: any; tick: number }) {
  const wp = job.work_package_id
  const lease = useData(() => api.coordinator(wp), [wp, tick])
  const [reason, setReason] = useState('')
  const l = lease.data
  const held = coordHeld(wp)
  // 只凭本地令牌不够：别人接管后旧令牌仍在。租约的 expires_at 与取得时一致才算本页持有。
  const mine = !!(held && l && held.expires_at === l.expires_at)
  const target = job.contract?.coordinates?.job_id
  return <Section title="协调权" extra={<span className="muted">同一根工作包同时只允许一个协调者（交互或受管 Claude）</span>}>
    {target && <p>本任务是受管协调回合，目标任务：<a href={'#/jobs/' + target}>{short(target)}</a></p>}
    {lease.error && <p className="err">{lease.error}</p>}
    <p>{l ? <>当前持有：<b>{l.holder_kind === 'managed' ? '受管协调者' : '交互协调者'}</b>（{show(l.holder_ref)}），到期 {when(l.expires_at)}{mine ? '；由本页持有' : ''}</>
      : '当前无人持有协调权。'}</p>
    {held && l && !mine && <p className="warn-text">本页之前取得的协调令牌已失效（已被接管或已过期）。</p>}
    <div className="actions">
      {/* 取得租约不发事件：成功后主动重读，令牌在 sessionStorage 中也不会触发重渲染。 */}
      {!l && <Action label="取得协调权" run={() => api.takeCoordinator(wp, false, '').then(lease.reload)} />}
      {l && !mine && <span className="inline-form">
        <input placeholder="接管原因（必填，写入事件）" value={reason} onChange={e => setReason(e.target.value)} />
        <Action label="接管协调权" danger disabled={!reason.trim()} confirm="接管会使当前协调者的令牌失效，其后续验收/发布将被拒绝。确定？"
          run={() => api.takeCoordinator(wp, true, reason.trim()).then(lease.reload)} />
      </span>}
      {mine && <Action label="释放协调权" run={() => api.releaseCoordinator(wp).finally(lease.reload)} />}
    </div>
  </Section>
}

const GAP: Record<string, string> = { missing: '未执行', failed: '未通过', timed_out: '超时', manifest_mismatch: '证据不属于被验收版本', no_report: '没有执行报告' }
const POLICY: Record<string, string> = { always: '每次结束都做语义验收', on_failure: '仅失败或发生接管时语义介入', none: '只用确定性检查，不触发受管协调' }
const ACTIVE = ['running', 'recovering', 'cancel_requested']
const SCHEDULABLE = ['queued', 'waiting_capability', 'recovering']

function Report({ a }: { a: any }) {
  const r = a.report
  return <details open={a.attempt_id === undefined ? false : true}>
    <summary>attempt <code>{short(a.attempt_id)}</code>（epoch {a.epoch}，{a.node_id}）· {show(r.harness)}{r.truncated ? ' · 已截断' : ''}</summary>
    {r.steps && <table><thead><tr><th>#</th><th>步骤</th><th>状态</th><th>退出码</th><th>输出尾部</th></tr></thead>
      <tbody>{r.steps.map((s: any) => <tr key={s.index}><td>{s.index}</td><td>{s.op}</td><td>{s.status === 'skipped_from_checkpoint' ? '从检查点跳过' : s.status}</td>
        <td>{s.exit_code ?? '—'}</td><td>{(s.stdout_tail || s.stderr_tail) ? <pre className="json">{[s.stdout_tail, s.stderr_tail].filter(Boolean).join('\n--- stderr ---\n')}</pre> : '—'}</td></tr>)}</tbody></table>}
    {r.checks && <><p className="muted">节点执行的契约检查（{show(r.checks.node_id)} · {show(r.checks.platform?.os)} {show(r.checks.platform?.release)} · Python {show(r.checks.platform?.python)}；绑定 manifest <code>{short(r.checks.manifest_sha256)}</code>）</p>
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
    <input placeholder="排除节点，逗号分隔" value={exclude} onChange={e => setExclude(e.target.value)} />
    <Action label="明确迁移/续接" disabled={!reason.trim()} confirm="将计入迁移预算，从最近确认检查点在新 attempt 中继续。确定？"
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
      run={() => api.assess(job.job_id, job.work_package_id, job.version, { checkpoint_id: cp, status, method: method.trim(), result: result.trim(), reviewer: '控制台用户' })} />
  </details>
}

function Diff({ cp }: { cp: string }) {
  const [d, setD] = useState<any>(null)
  const [err, setErr] = useState<string | null>(null)
  if (d) return <details open><summary>diff</summary><small>新增 {d.added?.length ?? 0}：{(d.added ?? []).join(', ')}<br />修改 {d.modified?.length ?? 0}：{(d.modified ?? []).join(', ')}<br />删除 {d.deleted?.length ?? 0}：{(d.deleted ?? []).join(', ')}</small></details>
  return <><button className="link" onClick={() => api.diff(cp, 'inputs').then(setD, e => setErr(errText(e)))}>对比输入</button>{err && <small className="err">{err}</small>}</>
}
