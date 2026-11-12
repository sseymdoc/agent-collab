import { useEffect, useRef, useState } from 'react'
import { ApiError, api, uuid, waitOperation, OP_DONE } from '../api'
import { can } from '../session'
import { Capped, bytes, errText, short, useData, when } from '../ui'
import { CONFLICT } from './JobResult'
import { ApplyBadge } from './ProjectDetail'

// 远端项目写回（docs/protocol.md §13.10）：成果经目标节点的受限操作写回该节点本机绑定的目录。
// 这台设备不读写任何项目文件，只经 controller 登记请求、读取节点的结果；浏览器控制台与桌面端相同。
const ACTION: Record<string, string> = { add: '新增', modify: '修改', delete: '删除' }
const REASON: Record<string, string> = {
  ...CONFLICT, not_received: '节点在时限内没有领取，一个文件都没写', not_bound: '节点上已没有这个项目的目录绑定',
  outside_integration_roots: '绑定的目录不在节点允许写回的范围内', binding_changed: '绑定的路径现在指向别的目录', not_a_directory: '绑定的目录已不存在',
  unfinished_application: '这个目录上还有一次没完成的写回，先处理它', interrupted_before_write: '节点在写入之前重启，一个文件都没写',
  no_local_record: '节点上没有这次写回的本机记录', insufficient_space: '节点磁盘空间不足，没有写入',
}
const why = (r?: string) => r ? REASON[r] ?? r : ''

// 应用记录是权威状态；操作只说明节点有没有收到、有没有给出结果。
function State({ app, op }: { app: any; op: any }) {
  if (app && app.status !== 'started') return <ApplyBadge s={app.status} />
  if (!op || op.status === 'requested') return <span className="badge info">已登记，等待节点领取</span>
  if (op.status === 'received') return <span className="badge info">节点已收到，执行中</span>
  return <span className="badge warn" title="节点收到了但没在时限内给出结果，或执行中出错且无法确定写到了哪里">结果待确认</span>
}

function Paths({ title, list }: { title: string; list?: any[] }) {
  if (!list?.length) return null
  return <details><summary>{title}（{list.length}）</summary><ul className="paths"><Capped items={list} render={(x, i) => typeof x === 'string'
    ? <li key={i}><code>{x}</code></li> : <li key={i}><code>{x.path}</code> <small className="muted">{why(x.reason)}{x.error ? ' · ' + x.error : ''}</small></li>} /></ul></details>
}

function Detail({ app }: { app: any }) {
  const d = app?.detail
  if (!d) return null
  return <>
    {d.reason && <p className="muted">原因：{why(d.reason)}{d.message ? '（' + d.message + '）' : ''}</p>}
    {d.truncated && <p className="warn-text">明细过长，节点只上报了一部分：{JSON.stringify(d.counts ?? {})}</p>}
    <Paths title="已写入" list={d.changed} /><Paths title="目录里已是该内容" list={d.already} /><Paths title="未写入（被中断或跳过）" list={d.pending} />
    <Paths title="冲突（保留节点上的内容）" list={d.conflicts} /><Paths title="出错" list={d.errors} />
    <Paths title="已恢复成原样" list={d.restored} /><Paths title="保留现状" list={d.kept} />
  </>
}

// 页面离开后不再更新状态、停止轮询。
function useAlive() { const r = useRef(true); useEffect(() => { r.current = true; return () => { r.current = false } }, []); return r }

export function RemoteApply({ job }: { job: any }) {
  const { data: p } = useData(() => api.project(job.project_id), [job.project_id])
  const targets: any[] = p?.remote_targets ?? []
  const pending: any[] = (job.applications ?? []).filter((a: any) => a.target_node_id && a.status === 'started')
  const [node, setNode] = useState('')
  const chosen = targets.find(t => t.node_id === node) ?? targets[0]
  if (!p) return null
  return <div>
    <h3>写回到节点上的目录</h3>
    {!targets.length ? <p className="muted">没有节点为这个项目绑定目录。要把成果写回某台节点上的目录，在那台节点本机运行
      <code> agent-collab agent project-bind --data-dir &lt;节点数据目录&gt; {job.project_id} &lt;目录&gt;</code>，绑定会在一个心跳周期内出现在这里。</p>
      : <>
        <p className="muted">由那台节点在自己绑定的目录上比对与写入，规则与本机应用相同：只写成果改过的路径，节点上改过的文件算冲突、缺省不写；写入前备份到节点的数据目录。controller 只知道目录名。</p>
        <p className="actions"><label>目标 <select aria-label="选择目标节点" value={chosen?.node_id ?? ''} onChange={e => setNode(e.target.value)}>
          {targets.map(t => <option key={t.node_id} value={t.node_id}>应用到节点 {t.node_id} 上的目录（{t.root_name}）{t.status !== 'online' ? ' · ' + t.status : ''}</option>)}
        </select></label></p>
        {chosen && <Target key={chosen.node_id} job={job} target={chosen} />}
      </>}
    {pending.map(a => <Pending key={a.application_id} job={job} app={a} />)}
  </div>
}

function Target({ job, target }: { job: any; target: any }) {
  const live = useAlive()
  const [planOp, setPlanOp] = useState<any>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [run, setRun] = useState<{ app: any; op: any } | null>(null)
  const tasks = can('tasks')
  const review = async () => {
    setBusy('review'); setErr(null); setRun(null); setPlanOp(null)
    try {
      const op = await api.remotePlan(job.project_id, job.job_id, target.node_id)
      const done = await waitOperation(op.op_id, setPlanOp, () => live.current)
      if (done?.status === 'failed') setErr('节点没能比对：' + done.error)
      if (done?.status === 'timed_out') setErr('超时：节点不在线，或没在时限内完成比对。')
    } catch (e) { setErr(errText(e)) } finally { if (live.current) setBusy(null) }
  }
  const apply = async (skip: boolean) => {
    setBusy('apply'); setErr(null)
    try {
      // 带上审阅时看到的检查点与输入基线：期间成果被重新发布，controller 拒绝而不是写入没审阅过的内容。
      const app = await api.remoteApply(job.project_id, { application_id: uuid(), job_id: job.job_id, checkpoint_id: planOp.params.checkpoint_id,
        node_id: target.node_id, expected_base_manifest_sha256: planOp.params.base_manifest_sha256, skip_conflicts: skip, plan: planOp.result?.summary ?? {} })
      setRun({ app, op: null }); setPlanOp(null)
      let current = app
      for (let delay = 500; live.current; delay = Math.min(delay * 1.5, 3000)) {
        const op = await api.operation(app.operation_id).catch(() => null)
        current = await api.application(job.project_id, app.application_id)
        if (!live.current) return
        setRun({ app: current, op })
        if (current.status !== 'started' || (op && OP_DONE.includes(op.status))) break
        await new Promise(r => setTimeout(r, delay))
      }
    } catch (e) { setErr(errText(e)) } finally { if (live.current) setBusy(null) }
  }
  const plan = planOp?.status === 'succeeded' ? planOp.result : null
  const changes: any[] = plan?.changes ?? []
  const conflicts = changes.filter(c => c.state === 'conflict')
  const n = (k: string, v: string) => changes.filter(c => c[k] === v).length
  return <div>
    <p className="actions"><button disabled={!!busy || !tasks} title={tasks ? undefined : '需要任务权限（tasks）'} onClick={() => void review()}>{plan ? '重新审阅' : '审阅节点上的变更'}</button>
      {busy === 'review' && planOp && <small className="muted">{planOp.status === 'requested' ? '等待节点领取…' : '节点已收到，正在比对…'}</small>}
      {target.status !== 'online' && <small className="warn-text">节点当前{target.status === 'suspect' ? '疑似失联' : '不可达'}：请求会等它上线，到期仍未领取则不写入任何文件。</small>}</p>
    {err && <p className="err" role="alert">{err}</p>}
    {plan && <>
      <p>节点 <b>{target.node_id}</b> 上的目录 <b>{plan.root_name}</b> · 检查点 <code>{short(planOp.params.checkpoint_id)}</code>：新增 {n('action', 'add')}、修改 {n('action', 'modify')}、删除 {n('action', 'delete')}；
        可以应用 {n('state', 'clean')}，目录里已是该内容 {n('state', 'already')}，冲突 <b className={conflicts.length ? 'err' : ''}>{conflicts.length}</b>
        {typeof plan.summary?.bytes === 'number' ? `；将写入 ${bytes(plan.summary.bytes)}` : ''}{plan.truncated ? '（清单过长，只显示了一部分）' : ''}。</p>
      <table><thead><tr><th>路径</th><th>变更</th><th>状态</th><th>大小</th></tr></thead>
        <tbody><Capped items={[...conflicts, ...changes.filter(c => c.state !== 'conflict')]} n={300} render={c => <tr key={c.path}>
          <td><code>{c.path}</code></td><td>{ACTION[c.action] ?? c.action}</td>
          <td>{c.state === 'clean' ? <span className="badge good">可以应用</span> : c.state === 'already' ? <span className="badge muted">目录里已是该内容</span>
            : <><span className="badge bad">冲突</span> <small>{why(c.reason)}</small></>}</td>
          <td>{c.action === 'delete' ? '—' : bytes(c.size)}</td></tr>} /></tbody></table>
      {!changes.length && <p className="muted">成果相对输入没有改动。</p>}
      {conflicts.length ? <>
        <p className="notice">有 {conflicts.length} 个冲突：缺省不会写入任何文件，冲突文件保留节点上的内容。也可以只写入没有冲突的文件，结果会记为部分应用。</p>
        <p className="actions"><button className="danger" disabled={!!busy || !tasks} onClick={() => {
          if (window.confirm(`在节点 ${target.node_id} 上只写入 ${n('state', 'clean')} 个没有冲突的文件，${conflicts.length} 个冲突文件保留原样；结果记为部分应用。确定？`)) void apply(true)
        }}>只应用没有冲突的文件</button></p>
      </> : changes.some(c => c.state === 'clean') && <p className="actions"><button className="primary" disabled={!!busy || !tasks} onClick={() => void apply(false)}>写回到节点上的目录</button></p>}
    </>}
    {run && <div className="draft">
      <p>写回 <code>{short(run.app.application_id)}</code>：<State app={run.app} op={run.op} />
        {run.op?.received_at && <small className="muted"> · 节点收到于 {when(run.op.received_at)}</small>}</p>
      {run.app.status === 'started' && run.op && OP_DONE.includes(run.op.status) &&
        <p className="warn-text">节点{run.op.status === 'timed_out' ? '收到了请求，但没在时限内给出结果' : '报告执行中出错：' + (run.op.error ?? '')}。记录保持“已开始”，节点之后报来的结果仍会更新它；也可以在下面“未完成的写回”里查看节点上的现状并处理。</p>}
      <Detail app={run.app} />
    </div>}
  </div>
}

const RECOVER: Record<string, [string, string]> = {
  status: ['查看现状', '由节点核对目录现状：哪些已写入、哪些还没写、哪些之后又被改过。不改动任何文件。'],
  resume: ['续写', '由节点把还没写的文件写完；之后又被改过的文件不动，记为冲突。'],
  rollback: ['回滚', '由节点把已写入且之后未再改动的文件恢复成备份里的原样。'],
  abandon: ['按现状结束', '不再写入也不回滚，按现状结束，记为“结果未知”。'],
}

function Pending({ job, app }: { job: any; app: any }) {
  const live = useAlive()
  const [op, setOp] = useState<any>(null)
  const [res, setRes] = useState<any>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => { api.operation(app.operation_id).then(o => live.current && setOp(o), e => live.current && e instanceof ApiError && e.status === 404 && setOp({ status: 'gone' })) }, [app.operation_id]) // eslint-disable-line
  const act = async (action: string) => {
    if (action !== 'status' && !window.confirm(RECOVER[action][1] + ' 确定？')) return
    setBusy(true); setErr(null); setRes(null)
    try {
      const r = await api.remoteRecover(job.project_id, app.application_id, action)
      const done = await waitOperation(r.op_id, setRes, () => live.current)
      if (done?.status === 'failed') setErr('节点没能处理：' + done.error)
      if (done?.status === 'timed_out') setErr('超时：节点不在线，或没在时限内处理。')
    } catch (e) { setErr(errText(e)) } finally { if (live.current) setBusy(false) }
  }
  const report = res?.result?.report
  return <div className="draft">
    <p>未完成的写回 <code>{short(app.application_id)}</code> · 节点 <b>{app.target_node_id}</b> · 开始于 {when(app.started_at)}：
      <State app={app} op={op?.status === 'gone' ? { status: 'timed_out' } : op} /></p>
    <div className="actions">{Object.entries(RECOVER).map(([k, [label, tip]]) => <button key={k} title={tip} disabled={busy || !can('tasks')}
      className={k === 'status' ? '' : 'danger'} onClick={() => void act(k)}>{label}</button>)}</div>
    <small className="muted">{Object.values(RECOVER).map(([l, t]) => l + '：' + t).join(' ')}</small>
    {busy && res && <p className="muted">{res.status === 'requested' ? '等待节点领取…' : res.status === 'received' ? '节点已收到，处理中…' : ''}</p>}
    {err && <p className="err">{err}</p>}
    {report && <><p>节点上的现状：{report.interrupted ? '写入被中断' : String(report.status)}
      {report.written ? `；已写入 ${report.written.length}，未写 ${report.remaining?.length ?? 0}，之后又被改过 ${report.diverged?.length ?? 0}` : ''}</p>
      <Paths title="已写入" list={report.written} /><Paths title="未写" list={report.remaining} /><Paths title="之后又被改过" list={report.diverged} /></>}
    {res?.result?.application && <p>结果 <ApplyBadge s={res.result.application.status} /></p>}
  </div>
}
