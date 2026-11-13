import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { can, session } from '../session'
import { bridge, currentEpoch, invoke, isDesktop } from '../transport'
import { Action, Badge, Json, Section, bytes, errText, show, useData, when } from '../ui'
import { NodeBindings, installed } from './Wizard'

// 维护（ADR 0004、docs/protocol.md §14）：桌面更新与本机后台升级（仅桌面端），备份与恢复预览、切换屏障预检（maintain 权限），
// 诊断导出（仅桌面端，只落本机）。每个角色更新什么见 ADR 0004 第 5 条：桌面更新不升级远端 controller 或节点。

const STEP: Record<string, string> = { compat: '兼容检查', download: '下载', verify: '验签与完整性', prepare: '准备（空闲检查与排空）', backup: '备份', switch: '切换版本', health: '健康检查' }
const STATUS: Record<string, string> = { running: '进行中', done: '完成', failed: '失败（旧版本继续可用）', pending: '需要处理', skipped: '不需要' }
const SIGNED: Record<string, string> = { authenticode: 'Authenticode', developer_id: 'Developer ID', adhoc: '临时签名（未经平台认证）' }
const VERDICT: Record<string, [string, string]> = {
  accept: ['兼容', 'ok'], degraded: ['可以安装，部分功能不可用', 'warn-text'], reject: ['不兼容', 'err'], unknown: ['未核对', 'muted'],
  safe: ['只丢弃可丢弃的验收状态', 'ok'], needs_reconcile: ['需要对账：恢复会丢掉下列事实', 'warn-text'], blocked: ['不能恢复', 'err'],
}

export function Maintenance({ tick }: { tick: number }) {
  return <>
    {isDesktop && <DesktopUpdate />}
    {isDesktop && <LocalBackend />}
    {can('maintain') ? <><Backups tick={tick} /><Barrier tick={tick} /></>
      : <Section title="备份与恢复"><p className="muted">需要维护权限（maintain）才能查看与创建备份。</p></Section>}
    {isDesktop && <Diagnostics />}
  </>
}

function Steps({ j }: { j: any }) {
  return <ol className="steps">{(j.steps ?? []).map((s: any) => <li key={s.name}>{STEP[s.name] ?? s.name}：{STATUS[s.status] ?? s.status}
    {s.detail ? <small className="muted"> · {s.detail}</small> : null}</li>)}</ol>
}

// ---------------------------------------------------------------- 桌面更新

function DesktopUpdate() {
  const [st, setSt] = useState<any>(null)
  const [check, setCheck] = useState<any>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [progress, setProgress] = useState<{ done: number; total?: number } | null>(null)
  const load = () => invoke('update_status').then(setSt, e => setErr(errText(e)))
  // 有阻塞（扫描、写回、本机部署）时每 5 秒重读一次，结束后安装按钮随之可用。
  useEffect(() => {
    if (!st?.blockers?.length) return
    const timer = window.setInterval(() => { void load() }, 5000)
    return () => window.clearInterval(timer)
  }, [st?.blockers?.length]) // eslint-disable-line
  useEffect(() => {
    void load()
    const tauri = (window as any).__TAURI__
    const off = tauri.event.listen('ac-update', (e: any) => { if (e.payload?.phase === 'download') setProgress({ done: e.payload.done, total: e.payload.total ?? undefined }) })
    return () => { void off.then((f: () => void) => f()) }
  }, [])
  const run = async (label: string, f: () => Promise<void>) => {
    setBusy(label); setErr(null)
    try { await f() } catch (e) { setErr(errText(e)) } finally { setBusy(null); void load() }
  }
  if (!st) return <Section title="桌面端更新">{err ? <p className="err">{err}</p> : <p className="muted">读取中…</p>}</Section>
  const id = st.identity
  const settings = st.settings
  // 换通道时上一个通道的检查结果作废（外壳同时清掉已检查的版本与下载）。
  const save = (patch: any) => run('保存中…', async () => { await invoke('update_settings', { ...settings, ...patch }); if (patch.channel !== undefined) setCheck(null) })
  const features: string[] = session()?.controller?.features ?? []
  const shared = st.offer ? { available: true, ...st.offer } : null
  const offer = check?.available && (!shared || shared.version === check.version) ? check : shared
  const verdict = offer?.compat?.verdict
  return <Section title="桌面端更新" extra={<span className="muted">只更新这台机器上的桌面端与随包组件，不升级远端 controller 或节点</span>}>
    <dl className="kv">
      <dt>当前版本</dt><dd>{st.current} · 构建身份 {id.kind}{id.kind === 'formal' ? '（正式版）' : id.kind === 'candidate' ? '（开发候选版）' : id.kind === 'internal-test' ? '（内部测试版）' : '（开发构建，不检查更新）'}</dd>
      <dt>更新通道</dt><dd>{id.configured
        ? <select value={settings.channel} disabled={!!busy} onChange={e => void save({ channel: e.target.value })}>{id.channels.map((c: string) =>
          <option key={c} value={c}>{c === 'stable' ? 'stable（稳定版，缺省）' : c === 'preview' ? 'preview（预览版）' : c}</option>)}</select>
        : '这个构建没有配置更新通道'}</dd>
      {id.configured && <><dt>策略</dt><dd>
        <label className="inline"><input type="checkbox" checked={settings.auto_check} disabled={!!busy} onChange={e => void save({ auto_check: e.target.checked })} /> 自动检查（启动时与每天一次）</label>
        <label className="inline"><input type="checkbox" checked={settings.auto_download} disabled={!!busy} onChange={e => void save({ auto_download: e.target.checked })} /> 自动下载</label>
        <label className="inline"><input type="checkbox" checked={settings.auto_install} disabled={!!busy} onChange={e => void save({ auto_install: e.target.checked })} /> 下载后在空闲时自动安装</label>
        <br /><small className="muted">自动安装也要满足空闲条件：没有进行中的扫描、上传、下载、写回与本机部署操作。</small></dd></>}
    </dl>
    {st.blockers?.length > 0 && <p className="warn-text">现在不能安装：{st.blockers.join('；')}</p>}
    {err && <p className="err" role="alert">{err}</p>}
    <div className="actions">
      <button disabled={!!busy || !id.configured} onClick={() => void run('检查中…', async () => { setCheck(await invoke('update_check', { features })) })}>检查更新</button>
      {check && !check.available && <small className="ok">已是最新（{check.channel} 通道，{when(check.checked_at)}）</small>}
      {busy && <small className="muted" role="status">{busy}</small>}
    </div>
    {offer && <div className="card inner">
      <h3>新版本 {offer.version}</h3>
      {offer.notes && <pre className="notes">{offer.notes}</pre>}
      <dl className="kv">
        <dt>影响范围</dt><dd>只替换桌面端与随包组件（运行时、本机助手、后台发行包）；需要重启桌面端{navigator.userAgent.includes('Windows') ? '（安装时窗口会自动关闭，安装完成后重新打开）' : ''}。
          本机后台服务继续运行、不被打断；随包的后台版本变新时，安装后在“本机后台”里另行升级。</dd>
        <dt>组件</dt><dd>后台 {show(offer.summary?.components?.backend?.version)} · 运行时 Python {show(offer.summary?.platform?.runtime?.python ?? offer.summary?.components?.runtime?.python)} · 源提交 {String(offer.summary?.source?.commit ?? '').slice(0, 7) || show(null)}</dd>
        <dt>本平台</dt><dd>最低系统 {show(offer.summary?.platform?.min_os)} · 平台签名 {SIGNED[offer.summary?.platform?.signed?.platform] ?? '未签名'}
          {offer.summary?.platform?.signed?.notarized !== null && offer.summary?.platform?.signed?.notarized !== undefined ? ' · 公证 ' + (offer.summary.platform.signed.notarized ? '已公证' : '未公证') : ''}</dd>
        <dt>与当前 controller</dt><dd className={VERDICT[verdict]?.[1]}>{VERDICT[verdict]?.[0] ?? show(verdict)}{offer.compat?.reason ? '：' + offer.compat.reason : ''}
          {offer.compat?.missing_required?.length ? <><br />缺少必需功能：{offer.compat.missing_required.join('、')}</> : null}
          {offer.compat?.missing_optional?.length ? <><br />不可用的功能：{offer.compat.missing_optional.join('、')}</> : null}</dd>
      </dl>
      <div className="actions">
        <button disabled={!!busy || verdict === 'reject' || (st.downloaded?.version === offer.version)} onClick={() => void run('下载中…', async () => { setProgress(null); await invoke('update_download', { features }) })}>下载并验签</button>
        {progress && <small className="muted">{bytes(progress.done)}{progress.total ? ' / ' + bytes(progress.total) : ''}</small>}
        <button className="primary" disabled={!!busy || st.downloaded?.version !== offer.version || st.blockers?.length > 0} onClick={() => {
          if (window.confirm(`安装 ${offer.version}：桌面端会关闭并重新启动，本机后台服务不受影响。确定安装？`)) void run('准备安装：等正在运行的本机助手结束（最多一分钟，等不到就这次不安装）…', async () => { await invoke('update_install', { features }) })
        }}>安装并重启</button>
        {verdict === 'reject' && <small className="err">先在 controller 所在机器上升级它，再更新桌面端。</small>}
      </div>
    </div>}
    {st.history?.length > 0 && <details><summary>更新记录（{st.history.length}）</summary>
      {st.history.map((j: any) => <div key={j.op_id}><p>{j.from} → {j.to}（{j.channel}）：<b>{STATUS[j.status] ?? j.status}</b>{j.reason ? ' · ' + j.reason : ''} <small className="muted">{when(j.started_at)}</small></p><Steps j={j} /></div>)}
    </details>}
  </Section>
}

// ---------------------------------------------------------------- 本机后台

function LocalBackend() {
  const [d, setD] = useState<any>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<[string, string] | null>(null)
  const [out, setOut] = useState<string | null>(null)
  const load = () => invoke('local_discover').then(setD, e => setMsg([errText(e), 'err']))
  useEffect(() => { void load() }, [])
  // 第一次读取就失败时也要说明原因，不能整块消失。
  if (!d) return msg ? <Section title="本机后台"><p className={msg[1]}>{msg[0]}</p></Section> : null
  if (!installed(d.install)) return null
  const i = d.install
  // 安装器 discover 的 interrupted：上一次更新的 journal 摘要与当前指针（docs/install.md“中断与恢复”）。
  const stuck = i.interrupted && i.interrupted.status !== 'running' ? i.interrupted : null
  const journal = stuck ? { ...(stuck.journal ?? {}), step: (stuck.journal?.steps ?? []).find((s: any) => s.status !== 'done' && s.status !== 'skipped')?.name } : null
  const upgrade = async () => {
    setBusy(true); setMsg(null); setOut(null)
    try {
      const r = await invoke('local_setup', { role: i.role })
      setOut(r.output ?? null)
      setMsg(r.ok ? ['本机后台已升级到随包版本，期望状态保持不变', 'ok'] : ['升级没有完成：切换前失败时旧版本继续运行；安装器输出见下方', 'err'])
    } catch (e) { setMsg([errText(e), 'err']) } finally { setBusy(false); void load() }
  }
  return <Section title="本机后台" extra={<span className="muted">这台机器上的 controller / 节点，由安装器管理</span>}>
    <dl className="kv">
      <dt>已安装</dt><dd>{show(i.version)}（{show(i.role)}）{i.previous_version ? ' · 可回退到 ' + i.previous_version : ''}</dd>
      <dt>随包版本</dt><dd>{show(d.bundle?.version)}{d.upgrade === 'newer' ? <b className="warn-text"> · 可升级</b> : d.upgrade === 'same' ? ' · 已是随包版本' : d.upgrade === 'older' ? ' · 比已安装的旧（不会降级）' : ''}</dd>
      <dt>期望状态</dt><dd>{Object.entries<any>(i.desired ?? {}).map(([k, v]) => k + '：' + (v === 'stopped' ? '有意停用（升级后保持停用）' : '运行')).join('；') || show(null)}</dd>
    </dl>
    {journal && <p className="warn-text">上一次更新没有正常结束（{show(journal.operation)} {show(journal.from)} → {show(journal.to)}，停在 {STEP[journal.step] ?? show(journal.step)}）。
      在这台机器上运行 <code>agent-collab install --recover</code> 按记录对账，或联系维护者；不要直接重新安装。</p>}
    {d.upgrade === 'newer' && !journal && <>
      <p className="muted">升级在安装器的事务里进行：兼容检查 → 排空本机节点（等正在执行的任务与写回结束）→ 停服务 → 备份本机 controller → 切换版本 → 按期望状态启动并健康检查；
        失败时自动回到旧版本。原本停用的组件保持停用。正在执行的任务会等它结束，排空超时则放弃升级、旧版本继续运行。</p>
      <p className="actions"><button className="primary" disabled={busy} onClick={() => {
        if (window.confirm(`把本机后台从 ${i.version} 升级到 ${d.bundle?.version}？期间本机节点暂停接单，controller 短暂重启。`)) void upgrade()
      }}>升级本机后台</button>{busy && <small className="muted">升级中，可能需要几分钟…</small>}</p></>}
    {msg && <p className={msg[1]}>{msg[0]}</p>}
    {out && <><p className="muted">安装器输出（末尾）</p><pre className="json">{out}</pre></>}
    {/* 连接页上的同一个面板在连上 controller 之后就看不到了：这里也提供，项目可以从当前连接里选。 */}
    {/worker|all-in-one/.test(String(i.role)) && i.identity?.paired && <NodeBindings />}
  </Section>
}

// ---------------------------------------------------------------- 备份与恢复

// 维护操作失败（例如磁盘写满）时接口仍是 200，结果在 maintenance_op 里：按失败显示，不显示“已提交”。
function opFailed(r: any, what: string) {
  const op = r?.maintenance_op
  if (op?.status === 'failed') throw new Error(what + '没有完成：' + (op.error?.message ?? op.error?.code ?? '未知原因'))
}

function Backups({ tick }: { tick: number }) {
  const list = useData(() => api.backups(), [tick])
  const policy = useData(() => api.backupPolicy(), [tick])
  const [label, setLabel] = useState('')
  const [preview, setPreview] = useState<{ id: string; v?: any; err?: string } | null>(null)
  const previewTicket = useRef(0)
  const [saved, setSaved] = useState<string | null>(null)
  const [keep, setKeep] = useState<number | null>(null)
  const items: any[] = list.data?.backups ?? []
  const pol = policy.data?.settings ?? {}
  return <Section title="备份与恢复" extra={<span className="muted">备份含任务、项目与设置等敏感数据；不会进入诊断导出</span>}>
    {list.error && <p className="err">{list.error}</p>}
    <p className="muted">“立即备份”写到 controller 管理的备份位置（与数据库同一实例内）；要留一份副本，在列表里“导出到这台机器”（与 controller 同机时还要复制到别的存储，才算实例外的备份）。
      恢复在 controller 停止后于 controller 所在机器上进行（见下方说明），这里只提供恢复前的影响预览。</p>
    <div className="actions">
      <input placeholder="备注（可选）" value={label} onChange={e => setLabel(e.target.value)} maxLength={80} />
      <Action label="立即备份" scope="maintain" run={async () => {
        const r: any = await api.createBackup(label.trim())
        await list.reload()
        opFailed(r, '备份')
        setLabel('')
      }} />
    </div>
    <table className="grid"><thead><tr><th>时间</th><th>类型</th><th>位置</th><th>schema</th><th>内容</th><th>核对</th><th></th></tr></thead><tbody>
      {items.map(b => <tr key={b.id}>
        <td>{when(b.created_at)}{b.label ? <><br /><small>{b.label}</small></> : null}</td>
        <td>{b.kind === 'upgrade' ? '升级前自动备份' : b.kind === 'managed' ? '受管备份' : show(b.kind)}</td>
        <td>{b.location === 'instance' ? '实例内' : show(b.location)}</td>
        <td><small>{show(b.schema?.user_version ?? b.schema)}{b.schema?.fingerprint ? ' · ' + String(b.schema.fingerprint).slice(0, 8) : ''}</small></td>
        <td><small>{b.valid === false ? <span className="err">无法读取：{show(b.error?.message ?? b.error?.code)}</span>
          : <>{show(b.blob_count)} 个对象 · {bytes(b.blob_bytes)} · 代次 {show(b.authority_generation)} · 事件 {show(b.event_seq)}</>}</small></td>
        <td>{b.last_verification ? <><Badge v={b.last_verification.ok ? 'passed' : 'failed'} /> <small className="muted">{when(b.last_verification.at)}</small></> : <small className="muted">未核对</small>}</td>
        <td className="actions">
          <Action label="核对" scope="maintain" run={async () => { const r: any = await api.verifyBackup(b.id); await list.reload(); opFailed(r, '核对') }} />
          <button onClick={() => {
            const ticket = ++previewTicket.current
            setPreview({ id: b.id })
            const settle = (next: { id: string; v?: any; err?: string }) => { if (ticket === previewTicket.current) setPreview(next) }
            api.restorePreview(b.id).then(v => settle({ id: b.id, v }), e => settle({ id: b.id, err: errText(e) }))
          }}>恢复预览</button>
          {isDesktop && <Action label="导出到这台机器" scope="maintain" run={async () => {
            const r = await bridge('backup_save', { backup_id: b.id })
            if (!r.cancelled) setSaved(`已导出到 ${r.path}（${bytes(r.bytes)}，SHA-256 ${String(r.sha256).slice(0, 12)}…）：这是一份副本，是否在实例之外取决于保存位置（与 controller 在同一台机器时请再复制到别的存储），请存放在只有你能访问的位置`)
          }} />}
        </td>
      </tr>)}
      {!list.data && !list.error && <tr><td colSpan={7} className="muted">读取中…</td></tr>}
      {list.data && !items.length && <tr><td colSpan={7} className="muted">还没有备份</td></tr>}
    </tbody></table>
    {saved && <p className="ok">{saved}</p>}
    {preview && <RestorePreview p={preview} />}
    <details><summary>保留策略</summary>
      <p className="muted">只清理受管备份；不会删除唯一一份、也不会删除最新一份核对通过的备份。升级前的自动备份不在清理范围内。</p>
      <div className="actions"><label>保留份数 <input type="number" min={1} max={100} value={keep ?? pol.keep ?? 8} onChange={e => setKeep(Number(e.target.value))} /></label>
        <Action label="保存" scope="maintain" disabled={keep === null} run={async () => { const r: any = await api.saveBackupPolicy(pol.revision ?? 0, keep!); await policy.reload(); opFailed(r, '保存保留策略'); setKeep(null) }} /></div>
    </details>
    <details><summary>怎样恢复</summary>
      <p>恢复会把 controller 的数据换成备份时的样子：之后产生的任务、发布、写回记录、项目与设置修改、配对都会丢失；节点上已经写入项目目录的文件不会被撤回。
        已撤销的客户端与节点授权保持撤销（拒绝优先），备份之后才配对的要重新配对。旧的执行权按代次失效，派单保持暂停直到你确认。</p>
      <p>在 controller 所在机器上，停止 controller 服务后运行（可先加 <code>--dry-run</code> 只看结果）：</p>
      <pre className="json">agent-collab service stop{'\n'}agent-collab maintenance restore --data-dir &lt;状态目录&gt;/controller --from &lt;备份目录或导出的 tar&gt;{'\n'}agent-collab service start</pre>
      <p className="muted">数据库不可读（换机器、灾难恢复）时必须显式选择 <code>--identity keep</code>（保留备份里的凭据）或 <code>--identity revoke-all</code>（全部重新配对）。</p>
    </details>
  </Section>
}

function RestorePreview({ p }: { p: { id: string; v?: any; err?: string } }) {
  if (p.err) return <p className="err">恢复预览失败：{p.err}</p>
  if (!p.v) return <p className="muted">计算恢复影响…</p>
  const v = p.v
  const [text, tone] = VERDICT[v.verdict] ?? [show(v.verdict), '']
  return <div className="card inner">
    <h3>恢复到这份备份的影响</h3>
    <p className={tone}><b>{text}</b></p>
    {v.error && <p className="err">{show(v.error.code)}：{show(v.error.message)}</p>}
    {v.history_incomplete && <p className="warn-text">备份之后的事件历史不完整（已裁剪或来自别的 controller），无法列全变化，按需要对账处理。</p>}
    {(v.unresolved_applications ?? []).length > 0 && <p className="warn-text">有 {v.unresolved_applications.length} 次写回结果待确认（started / unknown）：先在项目页处理，恢复不会撤回已写入的文件。</p>}
    {(v.identity?.revoked ?? []).length > 0 && <p>恢复后保持撤销：{v.identity.revoked.map((r: any) => (r.kind === 'client' ? '客户端 ' : '节点 ') + String(r.id).slice(0, 8)).join('、')}</p>}
    {(v.identity?.re_pair ?? []).length > 0 && <p>恢复后需要重新配对：{v.identity.re_pair.map((r: any) => (r.kind === 'client' ? '客户端 ' : '节点 ') + String(r.id).slice(0, 8)).join('、')}</p>}
    {(v.identity?.warnings ?? []).map((w: string, i: number) => <p key={i} className="warn-text">{w}</p>)}
    {(v.changes ?? []).length > 0 && <details open={v.changes.length <= 20}><summary>备份之后的变化（{v.changes.length} 项，恢复后丢失）</summary><ul>
      {v.changes.slice(0, 200).map((c: any, i: number) => <li key={i}>{CHANGE_KIND[c.kind] ?? c.kind} <code>{String(c.id).slice(0, 8)}</code>{c.after?.title || c.before?.title ? ' ' + (c.after?.title ?? c.before?.title) : ''}：
        {c.change === 'created' ? '备份之后新建' : c.change === 'deleted' ? '备份之后删除' : '备份之后改过'}{c.consequence ? <small className="muted"> · {c.consequence}</small> : null}</li>)}</ul></details>}
    {v.file_effects && <p className="muted"><small>{v.file_effects}</small></p>}
    {'disposable' in v && <p className="muted"><small>disposable：{show(v.disposable)}（{v.disposable_scope ?? '只统计任务与写回，不看项目、设置与配对'}）。它为 true 不代表可以无损恢复。</small></p>}
    <details><summary>完整预览</summary><Json v={v} /></details>
  </div>
}

const CHANGE_KIND: Record<string, string> = {
  job: '任务', attempt: '执行', checkpoint: '检查点', assessment: '验收', publication: '发布', application: '写回', integration_target: '回写目标',
  project: '项目', settings: '设置', client: '客户端', node: '节点', client_token: '客户端凭据', node_token: '节点凭据', baseline: '输入基线',
  dismissal: '弃置记录', effects: '副作用记录', node_op: '节点操作',
}

// ---------------------------------------------------------------- 切换屏障预检

function Barrier({ tick }: { tick: number }) {
  const preview = useData(() => api.barrierPreview(), [tick])
  const pending: any[] = preview.data?.pending_results ?? []
  return <Section title="切换屏障预检" extra={<span className="muted">只在迁移切换前使用；常规更新不需要屏障</span>}>
    {preview.error && <p className="err">{preview.error}</p>}
    <p className="muted">切换屏障会让屏障之前签发的执行权全部失效，屏障之后这些成果不能再发布。执行屏障前，下列仍待验收或发布的成果要先发布，或明确弃置。
      这里只是展示；controller 在执行屏障时会在同一事务里再核对一次。</p>
    {preview.data && <p className="muted">派单{preview.data.dispatch_paused ? '已暂停' : '未暂停'} · 普通提交 {show(preview.data.intake)} ·
      {preview.data.ready ? ' 满足执行屏障的前置条件' : ' 还不满足执行屏障的前置条件（要先暂停派单、关闭或限制普通提交、处理下列成果）'}</p>}
    {preview.data && !pending.length && <p className="ok">没有待处理的成果。</p>}
    {pending.length > 0 && <table className="grid"><thead><tr><th>任务</th><th>状态</th><th>检查点</th><th></th></tr></thead><tbody>
      {pending.map(j => <tr key={j.job_id}>
        <td><a href={'#/jobs/' + j.job_id}>{j.title ?? j.job_id}</a></td><td>{j.acceptance?.status ? <Badge v={j.acceptance.status} /> : null} <small>{j.delivery_checkpoint_id ? '已发布的不是最新检查点' : '未发布'}</small></td>
        <td><small><code>{String(j.checkpoint_id).slice(0, 8)}</code></small></td>
        <td><Action label="弃置" danger scope="tasks" confirm="弃置只记录“这份成果不采用”，不删除任何东西；弃置后这个检查点不能再发布。确定？"
          run={async () => { const reason = window.prompt('弃置原因') ?? ''; if (!reason.trim()) throw new Error('需要填写原因'); const job = await api.job(j.job_id); await api.dismiss(j.job_id, job.work_package_id, job.version, reason.trim(), j.checkpoint_id); await preview.reload() }} /></td>
      </tr>)}
    </tbody></table>}
  </Section>
}

// ---------------------------------------------------------------- 诊断导出

function Diagnostics() {
  const [taskLogs, setTaskLogs] = useState(false)
  const [withController, setWithController] = useState(true)
  const [res, setRes] = useState<any>(null)
  const [file, setFile] = useState<{ name: string; text: string } | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [saved, setSaved] = useState<string | null>(null)
  const run = async (label: string, f: () => Promise<void>) => {
    setBusy(label); setErr(null)
    try { await f() } catch (e) { setErr(errText(e)) } finally { setBusy(null) }
  }
  // 读取结果只写回仍是当前这份诊断包的预览：换了新包，旧包迟到的文本作废。
  const latest = useRef<string | null>(null)
  latest.current = res?.id ?? null
  const files: any[] = res?.preview?.files ?? []
  const summary = res?.preview?.diagnostics ?? res?.preview?.summary ?? null
  return <Section title="诊断导出" extra={<span className="muted">只生成在这台机器上，不会自动上传或发送</span>}>
    <p className="muted">包含：版本与构建、兼容关系、本机部署与服务状态、最近错误与有界的日志末尾{withController ? '，以及当前连接的 controller 状态' : ''}。
      不包含：令牌、配对码、私钥、官方 CLI 的登录材料、任务输入与项目源码；家目录、用户名与主机名替换为占位符。</p>
    <label className="inline"><input type="checkbox" checked={withController} onChange={e => setWithController(e.target.checked)} /> 附带当前 controller 的状态</label>
    <label className="inline"><input type="checkbox" checked={taskLogs} onChange={e => setTaskLogs(e.target.checked)} /> 附带任务原始日志</label>
    {taskLogs && <p className="warn-text">任务原始日志可能包含项目内容（代码片段、文件名、模型输出）；仍会脱敏，但请在发送前检查。</p>}
    <div className="actions">
      <button disabled={!!busy} onClick={() => void run('收集中…', async () => {
        setRes(null); setFile(null); setSaved(null)
        setRes(await invoke('diagnostics_export', { epoch: withController ? currentEpoch() : null, include_task_logs: taskLogs }))
      })}>生成诊断包</button>
      {busy && <small className="muted">{busy}</small>}
    </div>
    {err && <p className="err">{err}</p>}
    {res && <div className="card inner">
      <h3>预览（还没有保存）</h3>
      <table className="grid"><thead><tr><th>文件</th><th>大小</th><th>截断</th><th></th></tr></thead><tbody>
        {files.map(f => <tr key={f.name}><td><code>{f.name}</code></td><td>{bytes(f.bytes ?? f.size)}</td><td>{f.truncated ? '是（原始 ' + bytes(f.original_bytes) + '）' : '否'}</td>
          <td><button disabled={!!busy} onClick={() => void run('读取中…', async () => { const id = res.id; const r = await invoke('diagnostics_preview', { id, name: f.name }); setFile(cur => latest.current === id ? { name: f.name, text: r.text } : cur) })}>查看</button></td></tr>)}
      </tbody></table>
      {res.preview?.redactions && <p className="muted"><small>脱敏：{Object.entries<any>(res.preview.redactions).map(([k, v]) => k + ' ' + v + ' 处').join('；')}</small></p>}
      {file && <><p className="muted">{file.name}</p><pre className="json">{file.text}</pre></>}
      {summary && <details><summary>diagnostics.json</summary><Json v={summary} /></details>}
      <div className="actions"><button className="primary" disabled={!!busy} onClick={() => void run('保存中…', async () => {
        const r = await invoke('diagnostics_save', { id: res.id })
        if (!r.cancelled) { setSaved(`已保存到 ${r.path}（${bytes(r.bytes)}）`); setRes(null); setFile(null) }
      })}>另存为…</button></div>
    </div>}
    {saved && <p className="ok">{saved}</p>}
  </Section>
}

// 自动检查（桌面端，启动后约 20 秒与每天一次由外壳发出 auto_check）：按本机的更新策略检查、下载，空闲时安装。
// 挂在 App 上（不随连接页与主界面切换卸载），否则启动时还在连接页就会错过这一次检查。
// 已连接时带上 controller 的 features 做兼容判断（旧 controller 没有这个字段按空表，不当作未知）；不兼容（reject）时不下载、不安装，只提示。
// 检查失败不打扰（“维护”页手动检查时显示原因）；下载或安装失败要提示。
export function useAutoUpdate(notify: (title: string, body: string) => void) {
  useEffect(() => {
    if (!isDesktop) return
    const tauri = (window as any).__TAURI__
    let busy = false, stopped = false, retry = 0
    const off = tauri.event.listen('ac-update', async (e: any) => {
      if (e.payload?.phase !== 'auto_check' || busy) return
      busy = true
      try {
        let st: any, r: any
        // 每一步都用当前连接的 features：期间换了 controller，外壳会发现与检查时不同而要求重新检查，不会沿用旧连接的结论。
        const features = () => { const who = session(); return who ? (who.controller?.features ?? []) : null }
        try {
          st = await invoke('update_status')
          if (!st.identity?.configured || !st.settings?.auto_check) return
          const who = session()
          r = await invoke('update_check', { features: features() })
        } catch { return }
        if (!r.available) return
        notify('桌面端有新版本 ' + r.version, r.compat?.verdict === 'reject' ? '与当前 controller 不兼容，先升级 controller；详见“维护”页' : '在“维护”页查看并安装')
        if (r.compat?.verdict === 'reject' || !st.settings.auto_download) return
        try {
          await invoke('update_download', { features: features() })
          // 下载期间用户可能改了设置；有扫描、写回或本机部署在进行时等它们结束（每分钟再看一次）。
          for (;;) {
            const now = await invoke('update_status')
            if (stopped || !now.settings?.auto_install || now.downloaded?.version !== r.version) return
            if (!now.blockers?.length) {
              // 读状态之后又有操作开始时，安装会以 busy 拒绝：当作阻塞，继续等。
              try { await invoke('update_install', { features: features() }); return } catch (err: any) { if (err?.code !== 'busy') throw err }
            }
            await new Promise(done => { retry = window.setTimeout(done, 60000) })
          }
        } catch (err) { notify('桌面端更新 ' + r.version + ' 没有完成', errText(err) + '；在“维护”页查看') }
      } finally { busy = false }
    })
    return () => { stopped = true; window.clearTimeout(retry); void off.then((f: () => void) => f()) }
  }, []) // eslint-disable-line
}
