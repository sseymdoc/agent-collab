import { useEffect, useState } from 'react'
import { api } from '../api'
import { session } from '../session'
import { invoke } from '../transport'
import { Badge, Json, Section, errText, show, when } from '../ui'

// 首次使用与本机部署（仅桌面端，docs/desktop.md “本机部署”）。本机服务由操作系统托管，与界面进程无关。
// local_discover 的 install 是安装器 `install --discover` 的输出；下面只按常见字段展示，完整内容在“原始信息”里。

export const CLI_NOTE = '官方模型 CLI（Codex、Claude Code）需要你按官方方式自行安装并登录，本产品不代管它们的登录。'
export const SESSION_NOTE = '本机服务在你登录系统后运行，注销后停止；重启后要有人登录才会再运行。'

// 是否已有安装：discover 输出没有明确字段时按有无版本判断。
export const installed = (i: any) => !!i && (typeof i.installed === 'boolean' ? i.installed : !!i.version)

// 构建身份（ADR 0004）：formal 正式版、candidate 开发候选版、internal-test 内部测试版、dev 开发构建。
// 正式身份带预发布版本号（X.Y.Z-rc.N）的是正式根下的发行候选，不是稳定版；平台签名另由安装包本身体现。
const KIND_MARK: Record<string, string> = { candidate: ' · 开发候选版', dev: ' · 开发构建（不对外分发）' }

export function BuildMark({ info }: { info: any }) {
  if (!info) return null
  return <><p className="muted"><small>桌面端 {info.version}{info.commit ? ' · ' + String(info.commit).slice(0, 7) : ''} · {info.platform}/{info.arch}
    {info.instance ? ' · 实例 ' + info.instance : ''}
    {info.automation || info.kind === 'internal-test' ? <b className="warn-text"> · 内部测试版（含自动化钩子，不对外分发）</b> : info.kind === 'formal' && String(info.version).includes('-') ? ' · 发行候选版（预发布）' : KIND_MARK[info.kind] ?? (info.candidate ? ' · 开发候选版' : '')}</small></p>
    {info.payload === false && <p className="warn-text"><small>随包组件核对没有通过：{info.payload_error}。项目目录与本机部署功能已停用，请重新运行安装程序修复。</small></p>}</>
}

const KIND: Record<string, string> = { controller: 'controller', worker: '节点' }
// 同名服务属于另一处安装时，本实例不会接管它（安装器会拒绝）。
const foreign = (install: any) => Object.entries<any>(install?.service_owner ?? {}).filter(([, v]) => v === 'other_prefix').map(([k]) => KIND[k] ?? k)

// install 是安装器 `install --discover` 的输出（docs/install.md）：desired、services、autostart、context 都按组件给出。
function InstallInfo({ install }: { install: any }) {
  const other = foreign(install)
  const warn = other.length > 0 && <p className="warn-text">这台机器上已有同名的 {other.join('、')} 服务，属于另一处安装；本实例不会接管或改动它。</p>
  if (!installed(install)) return <><p>这台机器上还没有本实例的本机部署。</p>{warn}</>
  const id = install.identity
  return <><dl className="kv">
    <dt>角色</dt><dd>{show(install.role)}{install.instance ? ` · 实例 ${install.instance}` : ''}</dd>
    <dt>版本</dt><dd>{show(install.version)}{install.previous_version ? `（上一版本 ${install.previous_version}）` : ''}</dd>
    <dt>服务状态</dt><dd>{Object.entries<any>(install.services ?? {}).map(([k, v]) => <span key={k}>{KIND[k] ?? k} <Badge v={String(v === 'active' ? 'online' : v)} /> </span>)}
      {!install.service && '未注册为系统服务'}</dd>
    <dt>期望状态</dt><dd>{Object.entries<any>(install.desired ?? {}).map(([k, v]) => (KIND[k] ?? k) + '：' + (v === 'stopped' ? '有意停用（升级与回退后也保持停用，直到再次启动）' : '运行')).join('；') || show(null)}
      {install.desired_source === 'inferred' && <small className="muted">（安装记录里没有，按当前自启动状态推断）</small>}</dd>
    <dt>节点身份</dt><dd>{id ? <><code>{id.node_id}</code> → <code>{id.controller}</code>{id.paired ? '' : '（尚未配对）'}</> : '无'}</dd>
    <dt>运行上下文</dt><dd><small>{Object.entries<any>(install.context ?? {}).map(([k, v]) =>
      `${KIND[k] ?? k}：${show(v?.manager)}，${v?.scope === 'system' ? '系统服务' : '用户会话级'}，${v?.runs_without_login ? '无人登录时也运行' : '用户登录后运行、注销后停止'}`).join('；') || show(null)}</small></dd>
    <dt>运行时</dt><dd><small>{install.runtime?.managed ? `随包的受管运行时 ${install.runtime.id}` : '系统上的 Python'} <code>{show(install.python)}</code></small></dd>
  </dl>{warn}<details><summary>原始信息</summary><Json v={install} /></details></>
}

function Discovery({ d, info }: { d: any; info: any }) {
  return <>
    {info && !info.helper && <p className="warn-text">这个构建不含本机部署所需的运行时（本机助手不可用），只能连接已有的 controller。</p>}
    <dl className="kv">
      <dt>本机助手</dt><dd>{d.helper ? '可用' : '不可用'}{d.bundle ? ` · 随包版本 ${show(d.bundle.version)}（运行时 ${show(d.bundle.runtime)}）` : ''}</dd>
      <dt>找到的官方 CLI</dt><dd>Codex：{d.cli?.codex ? <code>{d.cli.codex}</code> : '未找到'}；Claude Code：{d.cli?.claude ? <code>{d.cli.claude}</code> : '未找到'}
        <br /><small className="muted">找到不等于后台服务能用：以节点“重新探测登录与模型目录”的结果为准。</small></dd>
    </dl>
    <InstallInfo install={d.install} />
  </>
}

// 本机部署与“作为节点加入”。打开连接经连接页的 openFor：与连接页其他入口共用一个连接流程代次。
// begin/openFor 来自连接页：领取连接流程的代次，之后由 openFor 在每次等待后核对，被别的流程取代就不再打开。
export function Wizard({ info, begin, openFor, onConnect }: { info: any; begin: () => number; openFor: (g: number, c: any) => Promise<void>; onConnect: () => void }) {
  const [mode, setMode] = useState<'local' | 'worker' | null>(null)
  const [d, setD] = useState<any>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [out, setOut] = useState<string | null>(null)
  const [ok, setOk] = useState<string | null>(null)
  const [url, setUrl] = useState('')
  const [code, setCode] = useState('')
  const [node, setNode] = useState('')
  const discover = () => invoke('local_discover').then(setD, e => setErr(errText(e)))
  useEffect(() => { if (mode && !d) void discover() }, [mode]) // eslint-disable-line
  const run = async (label: string, f: () => Promise<void>) => {
    setBusy(label); setErr(null); setOut(null); setOk(null)
    try { await f() } catch (e: any) { setErr(errText(e)); if (typeof e?.output === 'string') setOut(e.output) } finally { setBusy(null); void discover() }
  }
  const setup = (args: Record<string, unknown>) => run('安装中，可能需要几分钟…', async () => {
    // 开始时领取代次：安装期间改为连接已有 controller、或从连接页打开了别的连接，安装照常完成，但不再自动打开本机连接。
    const g = args.role === 'all-in-one' ? begin() : 0
    const r = await invoke('local_setup', args)
    setOut(r.output ?? null)
    if (!r.ok) throw new Error('安装没有完成，安装器输出见下方')
    if (args.role === 'worker') setOk('这台机器已作为节点安装并加入 controller。它出现在该 controller 的“设备”页后，可在那里重新探测登录与模型目录。')
    if (args.role === 'all-in-one') await openFor(g, () => invoke('local_pair'))
  })
  const has = installed(d?.install)
  const helper = d ? d.helper : info?.helper
  return <Section title="开始使用">
    <p className="muted">{CLI_NOTE}{SESSION_NOTE}</p>
    <div className="choices">
      <button className={mode === 'local' ? 'primary' : ''} onClick={() => setMode('local')}>在这台机器上创建本机部署<br /><small>本机运行 controller 与节点</small></button>
      <button onClick={onConnect}>只连接已有的 controller<br /><small>用配对码连接</small></button>
      <button className={mode === 'worker' ? 'primary' : ''} onClick={() => setMode('worker')}>把这台机器作为节点加入<br /><small>连接已有的 controller 执行任务</small></button>
    </div>
    {err && <p className="err" role="alert">{err}</p>}
    {mode && !d && !err && <p className="muted">读取本机现状…</p>}
    {mode && d && <Discovery d={d} info={info} />}
    {mode === 'local' && d && (has
      ? <p className="muted">已有安装时只报告现状，不提供替换它的选项。{/all-in-one|controller/.test(String(d.install?.role)) && '它包含 controller 时，可以把桌面端连接到它：'}
        {/all-in-one|controller/.test(String(d.install?.role)) && <button disabled={!!busy} onClick={() => void run('配对中…', async () => {
          const g = begin(); await openFor(g, () => invoke('local_pair')) })}>连接本机 controller</button>}</p>
      : <p className="actions"><button className="primary" disabled={!!busy || !helper} onClick={() => void setup({ role: 'all-in-one' })}>创建本机部署（controller + 节点）</button>
        <small className="muted">完成后自动把桌面端配对到本机 controller。</small></p>)}
    {mode === 'worker' && d && (has ? <p className="muted">这台机器已有安装，不会被替换；如需改为加入别的 controller，请先在命令行处理现有安装。</p>
      : <form onSubmit={e => { e.preventDefault(); void setup({ role: 'worker', controller_url: url.trim(), pairing_code: code.trim(), node_id: node.trim() }) }}>
        <p className="muted">节点配对码在已连接该 controller 的桌面端“设备 → 签发节点配对码”里取得（一次性，10 分钟内有效）。</p>
        <label>controller 地址<input value={url} onChange={e => setUrl(e.target.value)} placeholder="https://controller.example.com:8443" required /></label>
        <label>节点配对码<input value={code} onChange={e => setCode(e.target.value)} autoComplete="off" required /></label>
        <label>节点名（与签发配对码时填的 node_id 一致：配对码只对那个名字有效）<input value={node} onChange={e => setNode(e.target.value)} pattern="[a-z0-9][a-z0-9-]{0,62}" required /></label>
        <button type="submit" className="primary" disabled={!!busy || !helper || !url.trim() || !code.trim() || !node.trim()}>安装并加入</button>
      </form>)}
    {busy && <p className="muted" role="status">{busy}</p>}
    {ok && <p className="ok">{ok}</p>}
    {out && <><p className="muted">安装器输出（末尾）</p><pre className="json">{out}</pre></>}
  </Section>
}

// 本机服务面板：只管理本实例的安装。stop 同时关闭自启动并记为有意停用。
export function LocalService() {
  const [d, setD] = useState<any>(null)
  const [st, setSt] = useState<any>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // 卸载成功后本机部署不再存在、面板随之隐藏：卸载结果（状态目录保留在哪里）单独留在原处，不随面板消失。
  const [removed, setRemoved] = useState<any>(null)
  const act = async (action: string) => {
    setBusy(true); setErr(null)
    try { setSt(await invoke('local_service', { action })) } catch (e) { setErr(errText(e)) } finally { setBusy(false) }
    if (action !== 'status') invoke('local_discover').then(setD, () => {})
  }
  useEffect(() => { invoke('local_discover').then(x => { setD(x); if (installed(x?.install)) void act('status') }, () => {}) }, []) // eslint-disable-line
  if (!installed(d?.install)) return removed ? <Section title="本机服务"><UninstallResult res={removed} /></Section> : null
  return <Section title="本机服务" extra={<span className="muted">这台机器上本实例的后台服务；关闭窗口不影响它们</span>}>
    <InstallInfo install={d.install} />
    {st && <dl className="kv">
      <dt>各组件</dt><dd>{Object.entries<any>(st.services ?? {}).map(([k, v]) => <span key={k}>{k} <Badge v={v === 'active' ? 'online' : String(v)} /> </span>)}{!st.services && '未知'}</dd>
      <dt>自启动</dt><dd>{Object.entries<any>(st.autostart ?? {}).map(([k, v]) => k + '：' + (v === 'enabled' ? '开启' : v === 'disabled' ? '关闭' : '未知')).join('；') || '未知'}</dd>
      {st.worker && <><dt>节点心跳</dt><dd>{typeof st.worker.heartbeat_age_s === 'number' ? Math.round(st.worker.heartbeat_age_s) + ' 秒前' : '未知'}</dd></>}
      {st.error && <><dt>错误</dt><dd className="err">{st.error}</dd></>}
    </dl>}
    <p className="muted">{SESSION_NOTE}</p>
    {err && <p className="err">{err}</p>}
    <div className="actions">
      <button disabled={busy} onClick={() => void act('status')}>刷新状态</button>
      <button disabled={busy} onClick={() => void act('start')}>启动</button>
      <button className="danger" disabled={busy} onClick={() => {
        if (window.confirm('停止会关闭自启动，并记为有意停用：升级后也保持停用，直到你再次启动。节点上正在执行的任务需要先排空。确定停止？')) void act('stop')
      }}>停止</button>
      {busy && <small className="muted">处理中…</small>}
    </div>
    {/worker|all-in-one/.test(String(d.install.role)) && d.install.identity?.paired && <NodeBindings />}
    <Uninstall onDone={res => { setRemoved(res); invoke('local_discover').then(setD, () => {}) }} />
  </Section>
}

// 节点本机的项目目录授权（docs/protocol.md §13.10）：只在这台机器上、经系统对话框选目录。controller 只看到项目 ID 与目录名，
// 之后可以请这台节点把该项目已发布的成果写回这个目录（三方比较，冲突缺省不写）。没有图形界面的节点用
// `agent-collab agent project-bind`。
export function NodeBindings() {
  const [b, setB] = useState<any[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [pick, setPick] = useState('')
  const [projects, setProjects] = useState<any[]>([])
  // 读取中不授权：否则迟到的旧列表会覆盖授权后的列表。读取失败可以重读。
  const [loading, setLoading] = useState(true)
  const load = () => { setLoading(true); setErr(null); return invoke('node_bindings').then(r => setB(r.bindings ?? []), e => setErr(errText(e))).finally(() => setLoading(false)) }
  useEffect(() => {
    void load()
    // 已连接 controller 时列出它的项目供选择；节点所属的 controller 可能不是当前连接的那个，所以也可以直接填项目 ID。
    if (session()) api.projects().then(r => setProjects((r.projects ?? []).filter((p: any) => !p.archived_at)), () => {})
  }, [])
  const act = async (f: () => Promise<any>) => {
    setBusy(true); setErr(null)
    try { const r = await f(); if (r && !r.cancelled) setB(r.bindings ?? []) } catch (e) { setErr(errText(e)) } finally { setBusy(false) }
  }
  const valid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(pick)
  return <div className="card inner">
    <h3>项目目录授权（本机节点）</h3>
    <p className="muted">授权后，controller 可以请这台节点把该项目已发布的成果写回所选目录；有冲突的文件缺省不写。撤销只删除授权，不动目录。</p>
    {b && !b.length && <p className="muted">还没有授权任何目录。</p>}
    {b && b.length > 0 && <table className="grid"><thead><tr><th>项目</th><th>目录</th><th>授权时间</th><th></th></tr></thead><tbody>
      {b.map(x => <tr key={x.project_id}>
        <td>{projects.find(p => p.project_id === x.project_id)?.name ?? <code>{x.project_id}</code>}</td>
        <td><code>{x.path}</code></td><td>{when(x.bound_at)}</td>
        <td><button className="danger" disabled={busy} onClick={() => {
          if (window.confirm('撤销这个项目在本机节点上的目录授权？目录里的文件不会被改动。')) void act(() => invoke('node_unbind', { project_id: x.project_id }))
        }}>撤销</button></td>
      </tr>)}
    </tbody></table>}
    <div className="actions">
      {projects.length > 0 && <select value={projects.some(p => p.project_id === pick) ? pick : ''} onChange={e => setPick(e.target.value)}>
        <option value="">选择项目…</option>{projects.map(p => <option key={p.project_id} value={p.project_id}>{p.name}</option>)}</select>}
      <input placeholder="或填写项目 ID" value={pick} onChange={e => setPick(e.target.value.trim().toLowerCase())} size={38} />
      {b === null && !loading && <button disabled={busy} onClick={() => void load()}>重新读取</button>}
      <button disabled={busy || !valid || loading} onClick={() => void act(() => invoke('node_bind', { project_id: pick }))}>选择文件夹并授权</button>
    </div>
    {err && <p className="err">{err}</p>}
  </div>
}

// 卸载本机后台：停用并移除本实例的服务与启动项、启动器与代码；状态目录（数据库、节点身份、备份、日志）保留。
function Uninstall({ onDone }: { onDone: (res: any) => void }) {
  const [res, setRes] = useState<any>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  return <details><summary>卸载本机后台</summary>
    <p className="muted">卸载桌面端不会自动移除本机后台服务；要移除它们在这里操作（Windows 卸载程序也会询问）。只移除本实例的服务、启动项与代码，
      不动用户项目、官方 CLI 的配置与登录、共享缓存和其他实例。状态目录保留，之后重新安装时身份与数据沿用；要彻底删除，卸载后自行删除状态目录。</p>
    <div className="actions"><button className="danger" disabled={busy} onClick={async () => {
      if (!window.confirm('卸载本机后台服务？正在执行的任务会中断，节点会离线。状态目录保留。')) return
      setBusy(true); setErr(null)
      let r: any = null
      try { r = await invoke('local_uninstall', { purge_code: true }); setRes(r) } catch (e) { setErr(errText(e)) } finally { setBusy(false); onDone(r) }
    }}>卸载本机后台（保留数据）</button>{busy && <small className="muted">处理中…</small>}</div>
    {err && <p className="err">{err}</p>}
    {res && <UninstallResult res={res} />}
  </details>
}

function UninstallResult({ res }: { res: any }) {
  return <p className={res.ok ? 'ok' : 'err'}>{res.ok ? '已卸载本机后台服务、启动项与代码。状态目录（数据库、节点身份、备份与日志）保留在 ' : '没有卸载成功，状态目录 '}<code>{res.kept_state_dir}</code>
    {res.ok ? '；要彻底删除，自行删除这个目录。' : '；安装器输出：' + res.output}</p>
}
