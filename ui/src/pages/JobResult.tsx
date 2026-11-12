import { useEffect, useRef, useState } from 'react'
import { bridge, isDesktop } from '../transport'
import { Capped, Section, bytes, errText, short, when } from '../ui'
import { ApplyBadge } from './ProjectDetail'
import { CancelButton, Progress, useProgress } from './ProjectInput'
import { RemoteApply } from './RemoteApply'

// 成果：整包下载到空文件夹、审阅变更后应用到项目目录（都在桌面端，由外壳与本机助手完成）。
const ACTION: Record<string, string> = { add: '新增', modify: '修改', delete: '删除' }
export const CONFLICT: Record<string, string> = {
  modified_locally: '本机改过这个文件', deleted_locally: '本机删了这个文件', created_locally: '本机新建了同名文件',
  unsafe_path: '路径经过链接，或与现有条目仅大小写不同', changed_since_plan: '审阅之后这个文件又被改过',
}
const reason = (r?: string) => r ? CONFLICT[r] ?? r : ''

// 成果状态的四件事：有检查点、已发布、已下载、已应用到项目。只看文本预览不算下载。
export function ResultFacts({ job, d, finals }: { job: any; d: any; finals: any[] }) {
  const downloads: any[] = d.downloads ?? [], apps: any[] = d.applications ?? []
  const lastDl = downloads[downloads.length - 1], lastApp = apps[apps.length - 1]
  return <div className="badges">
    <span>检查点 {finals.length || job.durable_checkpoint_id ? <span className="badge good">有{finals.length ? '最终检查点' : '检查点'}</span> : <span className="badge muted">没有</span>}</span>
    <span>发布 {job.delivery_version ? <span className="badge good">已发布（交付版本 {job.delivery_version}）</span> : <span className="badge muted">未发布</span>}</span>
    <span>下载 {downloads.length ? <span className="badge good" title={when(lastDl?.at)}>已下载 {downloads.length} 次 · 最近 {lastDl?.by?.name ?? lastDl?.by?.kind ?? '未知客户端'}</span> : <span className="badge muted">未下载</span>}</span>
    <span>应用到项目 {!job.project_id ? <span className="badge muted">不属于项目</span> : lastApp ? <ApplyBadge s={lastApp.status} /> : <span className="badge muted">未应用</span>}</span>
  </div>
}

export function JobResult({ job, finals, tick }: { job: any; finals: any[]; tick?: number }) {
  const cp = job.delivery_checkpoint_id ?? finals[finals.length - 1]?.checkpoint_id
  return <Section title="成果下载与应用" extra={<span className="muted">逐文件核对哈希，路径不会落到所选目录之外</span>}>
    {!isDesktop ? <p className="muted">整包下载与应用到项目在桌面端进行；也可以用命令行 <code>agent-collab job artifacts {job.job_id} --download &lt;目录&gt;</code>。下面“成果文件”里可以逐个查看与另存。</p>
      : !cp ? <p className="muted">还没有可下载的成果（需要最终检查点）。</p>
      : <><Download job={job} cp={cp} />{job.project_id && (job.delivery_checkpoint_id ? <Apply job={job} />
        : <p className="muted">成果发布之后才能应用到项目目录。</p>)}</>}
    {job.project_id && job.delivery_checkpoint_id && <RemoteApply job={job} tick={tick} />}
  </Section>
}

function Download({ job, cp }: { job: any; cp: string }) {
  const [prog, setProg] = useProgress(['download'])
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<[string, string] | null>(null)
  return <div>
    <p className="actions">
      <button disabled={busy} onClick={async () => {
        setBusy(true); setMsg(null); setProg(null)
        try {
          const r = await bridge('result_download', { job_id: job.job_id, checkpoint_id: cp })
          setMsg(r.cancelled ? ['已取消', 'muted'] : [`已下载 ${r.files} 个文件（${bytes(r.bytes)}）到 ${r.path}`, 'ok'])
        } catch (e) { setMsg([errText(e), 'err']) } finally { setBusy(false); setProg(null) }
      }}>{busy ? '下载中…' : '整包下载到文件夹'}</button>
      {busy && <CancelButton op="download" />}
      <small className="muted">{cp === job.delivery_checkpoint_id ? `已发布的交付（版本 ${job.delivery_version}）` : `最终检查点 ${short(cp)}（未发布）`}；需要选择空文件夹或新建一个。</small>
    </p>
    <Progress p={prog} />
    {msg && <p className={msg[1]}>{msg[0]}</p>}
  </div>
}

function Paths({ title, list }: { title: string; list?: any[] }) {
  if (!list?.length) return null
  return <details><summary>{title}（{list.length}）</summary><ul className="paths"><Capped items={list} render={(x, i) => typeof x === 'string'
    ? <li key={i}><code>{x}</code></li> : <li key={i}><code>{x.path}</code> <small className="muted">{reason(x.reason)}{x.error ? ' · ' + x.error : ''}</small></li>} /></ul></details>
}

function Apply({ job }: { job: any }) {
  const [plan, setPlan] = useState<any>(null)
  const [res, setRes] = useState<any>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [prog, setProg] = useProgress(['apply'])
  const run = async (label: string, f: () => Promise<void>) => {
    setBusy(label); setErr(null); setProg(null)
    try { await f() } catch (e) { setErr(errText(e)) } finally { setBusy(null); setProg(null) }
  }
  const changes: any[] = plan?.changes ?? []
  const conflicts = changes.filter(c => c.state === 'conflict')
  const n = (k: string, v: string) => changes.filter(c => c[k] === v).length
  const apply = (skip: boolean) => run('应用中…', async () => {
    try { setRes(await bridge('result_apply', { job_id: job.job_id, checkpoint_id: plan.checkpoint_id, skip_conflicts: skip })) }
    finally { window.dispatchEvent(new Event(UNFINISHED_CHANGED)) }  // 写入中途出错会留下未完成的应用：页面顶部重新查询
    setPlan(null)
  })
  return <div>
    <h3>应用到项目目录</h3>
    <p className="muted">只写成果相对任务输入新增、修改、删除的路径；目录里的其他文件不动。应用逐文件进行，不是整体原子操作；被替换或删除的原文件先备份到这台机器的应用数据目录。</p>
    <p className="actions"><button disabled={!!busy} onClick={() => void run('比对中…', async () => { setRes(null); setPlan(await bridge('result_plan', { job_id: job.job_id })) })}>{plan ? '重新审阅' : '审阅变更'}</button>
      {busy && <small className="muted">{busy}</small>}{busy === '应用中…' && <CancelButton op="apply" />}</p>
    <Progress p={prog} />
    {err && <p className="err" role="alert">{err}</p>}
    {plan && <>
      <p>目录 <b>{plan.root_name}</b> · 检查点 <code>{short(plan.checkpoint_id)}</code>：新增 {n('action', 'add')}、修改 {n('action', 'modify')}、删除 {n('action', 'delete')}；
        可以应用 {n('state', 'clean')}，目录里已是该内容 {n('state', 'already')}，冲突 <b className={conflicts.length ? 'err' : ''}>{conflicts.length}</b>
        {typeof plan.summary?.bytes === 'number' ? `；将写入 ${bytes(plan.summary.bytes)}` : ''}。</p>
      <table><thead><tr><th>路径</th><th>变更</th><th>状态</th><th>大小</th></tr></thead>
        <tbody><Capped items={[...conflicts, ...changes.filter(c => c.state !== 'conflict')]} n={300} render={c => <tr key={c.path}>
          <td><code>{c.path}</code></td><td>{ACTION[c.action] ?? c.action}</td>
          <td>{c.state === 'clean' ? <span className="badge good">可以应用</span> : c.state === 'already' ? <span className="badge muted">目录里已是该内容</span>
            : <><span className="badge bad">冲突</span> <small>{reason(c.reason)}</small></>}</td>
          <td>{c.action === 'delete' ? '—' : bytes(c.size)}</td></tr>} /></tbody></table>
      {!changes.length && <p className="muted">成果相对输入没有改动。</p>}
      {conflicts.length ? <>
        <p className="notice">有 {conflicts.length} 个冲突：缺省不会写入任何文件，冲突文件保留本机内容。处理冲突后可以重新审阅；也可以只应用没有冲突的文件，结果会记为部分应用。</p>
        <p className="actions"><button className="danger" disabled={!!busy} onClick={() => {
          if (window.confirm(`只写入 ${n('state', 'clean')} 个没有冲突的文件，${conflicts.length} 个冲突文件保留本机内容；结果记为部分应用。确定？`)) void apply(true)
        }}>只应用没有冲突的文件</button></p>
      </> : changes.some(c => c.state === 'clean') && <p className="actions"><button className="primary" disabled={!!busy} onClick={() => void apply(false)}>应用到项目</button></p>}
    </>}
    {res && <div className="draft">
      <p>结果 <ApplyBadge s={res.status} />{res.application_id ? <small className="muted"> · 应用 <code>{short(res.application_id)}</code></small> : null}
        {res.status === 'conflict' && ' 因冲突没有写入任何文件。'}</p>
      {res.cancelled && <p className="notice">已按取消请求停止：写完了正在写的文件，之后的文件没有开始。已写入 {res.changed?.length ?? 0} 个、未写 {res.pending?.length ?? 0} 个；
        已写入的文件保留（取消不等于回滚），这次应用记为{res.changed?.length ? '部分完成' : '未写入'}，任务不会显示为已回写。</p>}
      {res.reported === null && res.status !== 'conflict' && <p className="warn-text">本机已按上面的结果写完，还没得到 controller 的确认；结果存在这台机器上，这条连接下次打开时自动补报（不会重写文件）。在那之前 controller 上的应用记录停在“已开始”。</p>}
      {res.reported && res.reported !== res.status && <p className="muted">controller 记下的状态：<ApplyBadge s={res.reported} /></p>}
      <Paths title="已写入" list={res.changed} /><Paths title="目录里已是该内容" list={res.already} /><Paths title="未写入（被中断或跳过）" list={res.pending} />
      <Paths title="冲突（保留本机内容）" list={res.conflicts} /><Paths title="出错" list={res.errors} />
    </div>}
  </div>
}

// 写入开始后没有得到最终结果的应用：连接后查询一次，在页面顶部提示处理。随连接切换（Shell 重建）重置。
const RECOVER: Record<string, [string, string]> = {
  status: ['查看现状', '只核对目录现状：哪些文件已写入、哪些还没写、哪些之后又被改过。不改动任何文件。'],
  resume: ['续写', '把还没写的文件写完；之后又被改过的文件不动，记为冲突。结束后向 controller 上报最终状态。'],
  rollback: ['回滚', '把已写入且之后未再改动的文件恢复成备份里的原样；之后又被改过的文件保留现状。结束后上报最终状态。'],
  abandon: ['按现状结束', '不再写入也不回滚，按目录现状结束，在 controller 上记为“结果未知”。'],
}
const UNFINISHED_CHANGED = 'agent-collab:unfinished-changed'
export function Unfinished() {
  const [list, setList] = useState<any[]>([])
  const [reports, setReports] = useState<any[]>([])
  const [failed, setFailed] = useState<string | null>(null)
  // 查询可能并发（挂载时一次、应用结束后又一次）：只采用最新一次的应答，卸载后的应答都丢弃。
  const gen = useRef(0)
  const load = () => {
    const g = ++gen.current
    return bridge('result_unfinished').then(r => { if (g !== gen.current) return; setFailed(null); setList(r.unfinished ?? []); setReports(r.unreported ?? []) },
      e => { if (g === gen.current) setFailed(errText(e)) })
  }
  useEffect(() => {
    if (!isDesktop) return
    void load()
    const again = () => void load()
    window.addEventListener(UNFINISHED_CHANGED, again)
    return () => { window.removeEventListener(UNFINISHED_CHANGED, again); gen.current++ }
  }, [])
  // 本机已有结果、等 controller 确认的应用：属于这条连接的在查询时已按原 request_id 补报（reported 为补报后的状态）。
  const waiting = reports.filter(r => !r.reported)
  return <>
    {failed && <section className="card notice" role="alert"><b>没能查询这台机器上未完成的应用：</b>{failed}
      <p className="muted">查询不到时，可能有写入中途中断的应用没有显示出来。</p><p className="actions"><button onClick={() => void load()}>重新查询</button></p></section>}
    {reports.some(r => r.reported) && <section className="card"><p className="ok">已向 controller 补报 {reports.filter(r => r.reported).length} 次应用的结果（文件没有重写）：
      {reports.filter(r => r.reported).map(r => <span key={r.application_id}> <code>{short(r.application_id)}</code> <ApplyBadge s={r.reported.startsWith('not_recorded') ? 'unknown' : r.reported} /></span>)}</p></section>}
    {waiting.length > 0 && <section className="card notice" role="status">
      <b>有 {waiting.length} 次应用在本机已写完，还没得到 controller 的确认。</b>
      <ul>{waiting.map(r => <li key={r.application_id}><code>{short(r.application_id)}</code> 本机结果 <ApplyBadge s={r.status} /> · {when(r.created_at)} · {r.this_connection
        ? '这条连接暂时没能送达（controller 不可达，或这台设备的授权被撤销/不足）；恢复后重新打开这条连接会自动补报'
        : '是在另一条连接下发起的：只会用那条连接报给原 controller，切换到它之后自动补报'}</li>)}</ul>
    </section>}
    {list.length > 0 && <section className="card notice" role="alert">
      <b>有 {list.length} 次应用在写入中途中断（窗口被关闭、进程被结束或掉电），目录可能处于部分写入的状态。</b>
      <p className="muted">同一目录上有未完成的应用时，新的应用会被拒绝。每项可以先“查看现状”，再决定续写、回滚或按现状结束。</p>
      {list.map(a => <Recover key={a.application_id} a={a} onDone={load} />)}
    </section>}
  </>
}

function Recover({ a, onDone }: { a: any; onDone: () => void }) {
  const [res, setRes] = useState<any>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [prog, setProg] = useProgress(['recover'])
  const act = async (action: string) => {
    if (action !== 'status' && !window.confirm(RECOVER[action][1] + ' 确定？')) return
    setBusy(true); setErr(null); setProg(null)
    try { setRes(await bridge('result_recover', { application_id: a.application_id, action })); if (action !== 'status') onDone() }
    catch (e) { setErr(errText(e)) } finally { setBusy(false); setProg(null) }
  }
  return <div className="draft">
    <p>目录 <b>{a.root_name}</b> · <a href={'#/jobs/' + a.job_id}>任务 {short(a.job_id)}</a> · <a href={'#/projects/' + a.project_id}>项目</a> · 开始于 {when(a.started_at)}</p>
    <div className="actions">{Object.entries(RECOVER).map(([k, [label, tip]]) => <button key={k} title={tip} disabled={busy} className={k === 'status' ? '' : 'danger'} onClick={() => void act(k)}>{label}</button>)}
      {busy && <CancelButton op="recover" />}</div>
    <small className="muted">{Object.values(RECOVER).map(([l, t]) => l + '：' + t).join(' ')}</small>
    <Progress p={prog} />
    {err && <p className="err">{err}</p>}
    {res && <><p>现状：{res.interrupted ? '写入被中断' : String(res.status)}；已写入 {res.written?.length ?? 0}，未写 {res.remaining?.length ?? 0}，之后又被改过 {res.diverged?.length ?? 0}
      {res.reported !== undefined ? <>；controller 记下 {res.reported ? <ApplyBadge s={res.reported} /> : '（上报失败）'}</> : null}</p>
      <Paths title="已写入" list={res.written} /><Paths title="未写" list={res.remaining} /><Paths title="之后又被改过" list={res.diverged} /></>}
  </div>
}
