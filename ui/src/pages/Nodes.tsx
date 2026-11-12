import { Fragment, useEffect, useRef, useState } from 'react'
import { api, ApiError, waitOperation } from '../api'
import { can } from '../session'
import { Action, Badge, Section, errText, short, show, useData, when } from '../ui'
// 能力分层显示：在线（心跳）≠ CLI 已发现（版本）≠ 认证可用（探测）≠ 近期执行成功（绑定 agent/CLI 版本的证据）。
// “当前任务”取自 controller 记录的有效 attempt 与租约，不由界面推断。

// 模型目录的当前状态（catalog.state，按有效期算出）。
export const CATALOG: Record<string, string> = {
  ok: '已核实', stale: '已核实（本次刷新失败，沿用上次成功的目录）', expired: '未核实：目录已过期', unknown: '未核实：取不到目录', legacy: '旧版本设备：按其固定组合派单',
}
const MAINT: Record<string, [string, string]> = { draining: ['维护：排空中（还有任务在运行）', 'warn'], active: ['维护中', 'warn'] }
const BLOCKER: Record<string, string> = {
  active_attempts: '仍有执行中的任务', unsettled_jobs: '有待对账、恢复中或已请求取消的任务（当前 attempt 在这台设备上）',
  pending_integrations: '有以它为目标、尚未完成的回写', coordination_profile: '仍在协调节点列表里（先在“设置 → Claude 协调策略”里移除）',
}
const SERVICE_NOTE = '停止或启动整个节点服务只能在该设备本机进行（桌面端连接页的“本机服务”面板，或 agent-collab service stop|start）：服务停下后它与 controller 的连接也断开，无法再经这里启动。'

export function Nodes({ tick }: { tick: number }) {
  const { data, error } = useData(() => api.nodes(), [tick])
  const models = useData(() => api.models(), [tick])
  if (!data) return error ? <p className="err">{error}</p> : <p className="muted">加载中…</p>
  const nodes: any[] = data.nodes ?? data
  const hosts = new Map<string, any[]>()
  for (const n of nodes) hosts.set(n.physical_host_id ?? '未知主机', [...(hosts.get(n.physical_host_id ?? '未知主机') ?? []), n])
  const cols = 11
  return <>
    {error && <p className="err">刷新失败，以下为上次成功加载的内容：{error}</p>}
    {/* 模型目录读取失败与设备没上报目录是两回事：失败时单独提示，不把它显示成“未知”。 */}
    {models.error && <p className="err">读取模型目录失败{models.data ? '，模型目录列为上次成功读取的内容' : ''}：{models.error}</p>}
    <PairCode nodes={nodes} />
    <p className="muted">{SERVICE_NOTE}</p>
    {[...hosts].map(([host, list]) => <Section key={host} title={'物理主机 ' + host}
      extra={<span className="muted">同一主机的设备共享 host_slots，不能互为断电备份；“故障域”是各设备自行上报的标签，恢复评分按它区分</span>}>
      <table>
        <thead><tr><th>设备</th><th>环境</th><th>状态</th><th>当前任务（有效 attempt）</th><th>槽位（设备/主机）</th><th>工具与 harness</th><th>模型目录</th><th>认证（不调用模型的探测）</th><th>执行证据（绑定当前版本）</th><th>拒绝派单原因</th><th>接单</th></tr></thead>
        <tbody>{list.map(n => {
          const c = n.capabilities ?? {}
          const catalog = models.data?.catalog?.nodes?.[n.node_id]?.codex
          const st = catalog?.state ?? catalog?.status
          const m = MAINT[n.maintenance_state]
          return [<tr key={n.node_id}>
            <td><b>{n.node_id}</b><br /><small className="muted">agent {show(c.agent?.version ?? n.agent_version)}{c.agent?.git_commit ? ' · ' + c.agent.git_commit.slice(0, 7) : ''}<br />故障域 {show(n.failure_domain)}</small></td>
            <td>{show(c.os)} / {show(c.arch)}{(c.labels ?? []).length ? <><br /><small>{c.labels.join(', ')}</small></> : null}</td>
            <td><Badge v={n.status} />{m ? <> <span className={'badge ' + m[1]} title={n.maintenance?.reason}>{m[0]}</span></> : n.drain ? <> <span className="badge warn">暂停接单</span></> : null}
              {n.maintenance?.reason && <><br /><small>维护原因：{n.maintenance.reason}（{n.maintenance.by?.name ?? n.maintenance.by?.kind ?? '未知'}，{when(n.maintenance.at)}）</small></>}
              <br /><small className="muted">最近联系 {when(n.last_seen_at)}</small></td>
            <td>{(n.attempts ?? []).length ? (n.attempts as any[]).map(a => <div key={a.attempt_id}>
              <a href={'#/jobs/' + a.job_id}>{a.title || short(a.job_id)}</a> <Badge v={a.status} />
              <br /><small className="muted">{a.coordinates ? '协调回合' : '执行'} · {a.harness}{a.model ? ' · ' + a.model + ' / ' + a.effort : ''}{a.lease_expires_at ? ' · 租约至 ' + when(a.lease_expires_at) : ' · 等待领取'}</small></div>)
              : <span className="muted">空闲{n.active_attempts ? '（' + n.active_attempts + ' 个 attempt）' : ''}</span>}</td>
            <td>{show(n.resources?.slots)} / {show(n.resources?.host_slots)}</td>
            <td><small>{Object.entries(c.tools ?? {}).map(([k, v]) => k + (v ? ' ' + v : '')).join('，') || '未知'}<br />{Object.entries(c.harness ?? {}).map(([k, v]) => k + ' ' + (v ?? '不可用')).join('，')}
              {Object.entries(c.harness_paths ?? {}).filter(([, v]) => v).map(([k, v]) => <span key={k}><br />{k} 路径 <code>{String(v)}</code></span>)}
              {Object.entries(c.harness_disabled ?? {}).map(([k, v]) => <span key={k} className="warn-text"><br />{k} 已停用：{String(v)}</span>)}
              {(c.gpu ?? []).length ? <><br />GPU {c.gpu.map((g: any) => typeof g === 'string' ? g : g.name + (g.cuda ? '（CUDA）' : '')).join(', ')}</> : null}</small></td>
            <td><small>{!catalog ? (models.data ? '未知' : models.error ? '读取失败' : '读取中…') : <>{CATALOG[st] ?? st}{catalog.reason ? '（' + catalog.reason + '）' : ''}
              <br /><span className="muted">取得于 {when(catalog.fetched_at)}{catalog.provider ? ' · provider ' + catalog.provider : ''}</span>
              {catalog.models?.length ? <details><summary>{catalog.models.length} 个模型</summary>
                {catalog.models.map((m: any) => <div key={m.id}><code>{m.id}</code>：{m.efforts.join('、')}</div>)}
                <span className="muted">目录来自该设备上的 CLI，不表示这个账号已成功调用过。</span></details> : null}</>}</small></td>
            <td><small>{Object.entries(n.auth_health ?? {}).map(([k, v]) => <span key={k}>{k} <Badge v={String(v)} /> </span>)}{!Object.keys(n.auth_health ?? {}).length && '未知'}
              <br />账户分组 {Object.entries(n.provider_account_refs ?? {}).map(([k, v]) => k + '：' + show(v)).join('，') || '未知'}</small></td>
            <td><small>{Object.entries(n.capability_evidence ?? {}).map(([k, e]: [string, any]) => <span key={k}>{k} <Badge v={e.status} />
              {e.last_success ? <> 成功 {when(e.last_success.at)}</> : null}{e.last_failure ? <> · 失败 {when(e.last_failure.at)}（{show(e.last_failure.reason)}）</> : null}<br /></span>)}
              {!Object.keys(n.capability_evidence ?? {}).length && '暂无（在线或 CLI 版本不等于可执行）'}</small></td>
            <td><small>{(n.last_rejections ?? n.last_rejection_reasons ?? []).join('，') || '无'}</small></td>
            <td>{n.drain
              ? <Action label="恢复接单" scope="devices" disabled={!!n.maintenance} run={() => api.drain(n.node_id, n.version, false)} />
              : <Action label="暂停接单" scope="devices" danger confirm="暂停后该设备不再领取新任务，已运行的任务继续到结束。确定？"
                  run={() => api.drain(n.node_id, n.version, true)} />}
              {/* 已暂停的设备也可再迁移剩余任务；响应里的 not_migrated 列出留在本设备的 attempt 及原因。 */}
              <Action label={n.drain ? '迁移剩余任务' : '排空并迁移'} scope="devices" danger
                confirm="排空，并把可安全迁移的任务（已受保护、无外部副作用、有确认检查点）从最近检查点迁到其他设备；不满足条件的继续在本设备运行。确定？"
                run={() => api.drain(n.node_id, n.version, true, true)}
                done={r => (r?.not_migrated ?? []).length ? '未迁移：' + r.not_migrated.map((x: any) => short(x.attempt_id) + '（' + (x.reasons ?? []).filter(Boolean).join('、') + '）').join('；') : null} />
              {n.maintenance && <small className="muted">维护期间由“结束维护”恢复接单</small>}</td>
          </tr>, <tr key={n.node_id + ':admin'} className="sub"><td colSpan={cols}><NodeAdmin n={n} /></td></tr>]
        })}</tbody>
      </table>
    </Section>)}
    {!nodes.length && <p className="muted">还没有已登记的设备。用上面的“签发节点配对码”为新设备生成配对码。</p>}
    <Retired tick={tick} />
  </>
}

// 节点配对码：只显示这一次。对已登记的节点签发会替换它现有的凭据，必须单独确认。
function PairCode({ nodes }: { nodes: any[] }) {
  const [id, setId] = useState('')
  const [replace, setReplace] = useState(false)
  const [issued, setIssued] = useState<any>(null)
  const exists = nodes.some(n => n.node_id === id.trim())
  const valid = /^[a-z0-9][a-z0-9-]{0,62}$/.test(id.trim())
  return <Section title="签发节点配对码" extra={<span className="muted">一次性，10 分钟内有效</span>}>
    <div className="actions">
      <input aria-label="节点 ID" placeholder="node_id，如 mac-worker" value={id} onChange={e => { setId(e.target.value); setReplace(false) }} />
      {exists && <label className="row"><input type="checkbox" checked={replace} onChange={e => setReplace(e.target.checked)} /> 这台设备已登记：签发会替换它现有的凭据，原设备将无法再连接，直到用新配对码重新配对</label>}
      <Action label="签发配对码" scope="devices" disabled={!valid || (exists && !replace)} run={() => api.nodeCode(id.trim(), exists && replace).then(setIssued).catch(e => {
        if (e instanceof ApiError && e.code === 'node_exists') throw new Error('这个 node_id 已登记：勾选“替换它现有的凭据”后再签发。')
        if (e instanceof ApiError && e.code === 'node_retired') throw new Error('这个 node_id 已退役：先在下面“已退役的设备”里恢复身份。')
        throw e
      })} />
    </div>
    {id && !valid && <p className="err">node_id 只能是小写字母、数字与 -，以字母或数字开头，最长 63 个字符。</p>}
    {issued && <div className="notice">配对码（只显示这一次，{when(issued.expires_at)}过期，只能用一次）：<code className="code">{issued.code}</code>（节点 <b>{issued.node_id}</b>）
      <p className="muted">在目标机器上使用：桌面端连接页选“把这台机器作为节点加入”，填 controller 地址、这个配对码与节点名 <code>{issued.node_id}</code>；
        或用命令行安装器：<code>install.sh --role worker --controller-url &lt;地址&gt; --node-id {issued.node_id} --pairing-code &lt;配对码&gt;</code>（Windows 用 <code>install.ps1</code>，参数相同）。</p>
      <button className="link" onClick={() => setIssued(null)}>关闭</button></div>}
  </Section>
}

const CONTEXT: [string, string][] = [['user', '用户'], ['ssh', '经 SSH 启动'], ['tty', '终端'], ['launchd_label', 'launchd 标签'], ['windows_session', 'Windows 会话号'],
  ['systemd_unit', 'systemd 单元'], ['version', '版本'], ['version_dir', '版本目录'], ['platform', '平台'], ['pid', '进程号']]
const LOGIN: Record<string, string> = { codex: 'codex login', claude: 'claude auth login' }

// 维护、退役与“重新探测”。探测在节点自己的服务进程上下文里运行，结果代表后台服务实际读得到的登录与目录。
function NodeAdmin({ n }: { n: any }) {
  const [reason, setReason] = useState('')
  const [retireReason, setRetireReason] = useState('')
  const [op, setOp] = useState<any>(null)
  const [probeErr, setProbeErr] = useState<string | null>(null)
  const [blockers, setBlockers] = useState<any>(null)
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])
  const probing = !!op && !['succeeded', 'failed', 'timed_out'].includes(op.status)
  const r = op?.status === 'succeeded' ? op.result : null
  return <details>
    <summary>维护、退役与重新探测{r ? ' · 已有探测结果' : ''}</summary>
    <div className="form-grid">
      <div>
        <h3>维护</h3>
        {n.maintenance ? <Action label="结束维护" scope="devices" run={() => api.maintenance(n.node_id, n.version, false, '')} />
          : <span className="inline-form"><input placeholder="维护原因（必填）" value={reason} maxLength={300} onChange={e => setReason(e.target.value)} />
            <Action label="进入维护" scope="devices" disabled={!reason.trim()} confirm="进入维护后不再向该设备派单，已在运行的任务不受影响。确定？"
              run={() => api.maintenance(n.node_id, n.version, true, reason.trim()).then(() => setReason(''))} /></span>}
        <p className="muted"><small>结束维护时恢复进入维护之前的接单状态（此前已暂停接单的保持暂停）。</small></p>
      </div>
      <div>
        <h3>退役</h3>
        <span className="inline-form"><input placeholder="退役原因（必填）" value={retireReason} maxLength={300} onChange={e => setRetireReason(e.target.value)} />
          <Action label="退役" scope="devices" danger disabled={!retireReason.trim()} confirm="退役后这台设备的凭据与未用的配对码作废，不再参与调度；记录与历史保留，可以恢复身份（之后需重新配对）。确定？"
            run={() => api.retire(n.node_id, n.version, true, retireReason.trim()).then(() => setBlockers(null), e => {
              if (e instanceof ApiError && e.code === 'retire_blocked') { setBlockers(e.details?.blockers ?? {}); throw new Error('还不能退役，先处理下面列出的各项') }
              throw e
            })} /></span>
        {blockers && <ul className="warn-text">{Object.entries<any>(blockers).map(([k, v]) => <li key={k}>{BLOCKER[k] ?? k}
          {Array.isArray(v) ? <>：{v.slice(0, 20).map((x: string, i: number) => <span key={x}>{i ? '、' : ''}{k === 'active_attempts' ? <code>{short(x)}</code> : <a href={'#/jobs/' + x}>{short(x)}</a>}</span>)}{v.length > 20 ? ` 等 ${v.length} 项` : ''}</>
            : typeof v === 'string' ? `（${v}）` : null}</li>)}</ul>}
      </div>
    </div>
    <h3>重新探测登录与模型目录</h3>
    <p className="actions"><button disabled={probing || !can('devices')} title={can('devices') ? undefined : '需要设备权限（devices）'} onClick={async () => {
      setProbeErr(null); setOp(null)
      try { const x = await api.nodeOperation(n.node_id, 'probe'); setOp({ status: x.status ?? 'requested' }); await waitOperation(x.op_id, setOp, () => alive.current) }
      catch (e) { setProbeErr(errText(e)) }
    }}>{probing ? '探测中…' : '重新探测'}</button>
      {!can('devices') && <small className="muted">需要设备权限</small>}
      {op && <span>已请求 → {op.status === 'requested' ? '等待节点领取' : <>节点已收到{op.status !== 'received' && <> → <Badge v={op.status} /></>}</>}</span>}</p>
    {probeErr && <p className="err">{probeErr}</p>}
    {op?.status === 'failed' && <p className="err">探测失败：{typeof op.error === 'string' ? op.error : JSON.stringify(op.error)}</p>}
    {op?.status === 'timed_out' && <p className="err">超时：节点不在线，或它的版本不支持管理操作。</p>}
    {r && <dl className="kv">
      <dt>探测时间</dt><dd>{when(r.at)}</dd>
      {Object.entries<any>(r.auth ?? {}).map(([h, a]) => <Fragment key={h}><dt>{h} 登录</dt><dd><Badge v={a.status} />{a.reason ? ' ' + a.reason : ''}
        {a.disabled ? <span className="warn-text">（已停用：{show(a.disabled)}）</span> : null}{a.command ? <small className="muted"> · <code>{Array.isArray(a.command) ? a.command.join(' ') : String(a.command)}</code></small> : null}</dd></Fragment>)}
      {Object.entries<any>(r.catalog ?? {}).map(([h, c]) => <Fragment key={h}><dt>{h} 模型目录</dt><dd><Badge v={c.status} />{c.reason ? ' ' + c.reason : ''}{c.provider ? ' · provider ' + c.provider : ''}
        {Array.isArray(c.models) ? ` · ${c.models.length} 个模型` : ''}</dd></Fragment>)}
      <dt>服务进程的运行上下文</dt><dd><small>{CONTEXT.filter(([k]) => r.context?.[k] != null).map(([k, l]) => l + '：' + show(r.context[k])).join('；') || '未知'}</small></dd>
    </dl>}
    <p className="muted"><small>登录引导：在该设备上、以运行服务的那个用户、按官方方式登录相应 CLI（Codex：<code>{LOGIN.codex}</code>；Claude Code：<code>{LOGIN.claude}</code>），然后重新探测。
      在终端里已登录不等于后台服务读得到凭据（例如 macOS 钥匙串在 SSH 会话里不可用），以这里的探测结果为准。</small></p>
  </details>
}

function Retired({ tick }: { tick: number }) {
  const { data, error } = useData(() => api.retiredNodes(), [tick])
  const list: any[] = data?.nodes ?? []
  if (!list.length && !error) return null
  return <Section title="已退役的设备" extra={<span className="muted">记录与历史保留；没有删除</span>}>
    {error && <p className="err">{error}</p>}
    <table><thead><tr><th>设备</th><th>退役时间</th><th>原因</th><th>执行者</th><th></th></tr></thead>
      <tbody>{list.map(n => <tr key={n.node_id}><td><b>{n.node_id}</b></td><td>{when(n.retired_at)}</td><td>{show(n.retire_reason)}</td><td>{n.retired_by?.name ?? n.retired_by?.kind ?? '未知'}</td>
        <td><Action label="恢复身份" scope="devices" confirm="恢复后记录不变、保持不接单；需要重新签发配对码并在设备上重新配对才有凭据。确定？" run={() => api.retire(n.node_id, n.version, false, '')} /></td></tr>)}</tbody></table>
    <p className="muted">恢复身份后仍需重新配对：在上面“签发节点配对码”里为它签发（勾选替换凭据），再在设备上配对。</p>
  </Section>
}
