import { useEffect, useRef, useState } from 'react'
import { api, ApiError, uuid } from '../api'
import { can, session } from '../session'
import { isDesktop } from '../transport'
import { type Kind, KIND_LABEL, notifyPrefs, saveNotifyPrefs } from '../notify'
import { Action, Badge, EFFORT_RE, Json, MODEL_RE, ORIGIN, SCOPE, Section, applyTheme, errText, show, short, useData, when } from '../ui'
import { CATALOG } from './Nodes'
import { LocalService } from './Wizard'
const ROLE: Record<string, string> = { owner: '拥有者（全部权限）', operator: '操作者（读取 + 任务）', viewer: '只读' }
// 与 controller 的 protocol.ROLES 一致：角色只是常用 scope 组合的预设。
const ROLE_SCOPES: Record<string, string[]> = { owner: Object.keys(SCOPE), operator: ['read', 'tasks'], viewer: ['read'] }
const actor = (a: any) => a ? (a.name ?? ORIGIN[a.kind] ?? a.kind) : '未知'
// 比较设置时不计键与 effort 列表的顺序：勾掉再勾回同一项算没改。
const canon = (v: any): any => Array.isArray(v) ? v.map(canon).sort() : v && typeof v === 'object' ? Object.fromEntries(Object.keys(v).sort().map(k => [k, canon(v[k])])) : v
const same = (a: any, b: any) => JSON.stringify(canon(a)) === JSON.stringify(canon(b))

export function Settings({ tick, onLeave }: { tick: number; onLeave: () => void }) {
  const who = session()
  return <>
    <Section title="当前连接">
      <dl className="kv">
        <dt>身份</dt><dd>{who?.identity.name ?? ORIGIN[who?.identity.kind] ?? show(who?.identity.kind)}
          {who?.identity.client_id ? <small className="muted"> · 客户端 <code>{short(who.identity.client_id)}</code></small> : null}</dd>
        <dt>权限</dt><dd>{(who?.identity.scopes ?? []).map((s: string) => SCOPE[s] ?? s).join('、')}</dd>
        <dt>controller</dt><dd><code>{show(who?.controller.controller_id)}</code> · 版本 {show(who?.controller.version)}{who?.controller.git_commit ? ' · ' + String(who.controller.git_commit).slice(0, 7) : ''} · authority_generation {show(who?.controller.authority_generation)}</dd>
        <dt>controller 支持的能力</dt><dd>{(who?.controller.features ?? []).join('、') || '未声明（旧版本 controller）'}</dd>
        <dt>传输</dt><dd>{isDesktop ? '桌面端原生桥；凭据在系统凭据存储里，页面拿不到' : '浏览器会话（Cookie + CSRF）· ' + location.origin + (location.protocol === 'https:' ? '（TLS，浏览器已校验证书）' : '（非 TLS，连接未加密：登录令牌和会话 Cookie 以明文传输）')}</dd>
      </dl>
      <p className="actions"><button onClick={onLeave}>{isDesktop ? '断开连接' : '退出登录'}</button>
        {isDesktop && <small className="muted">断开只关闭这个窗口的连接，不影响 controller 和设备上的任务；连接仍保留在本机，可在连接页重新进入或忘记。</small>}</p>
    </Section>
    <ModelGroup tick={tick} group="codex" />
    <ModelGroup tick={tick} group="claude" />
    <Catalogs tick={tick} />
    {isDesktop && <LocalService />}
    {can('maintain') && <Clients tick={tick} />}
    {isDesktop && <Notifications />}
    <Preferences />
    {can('maintain') && <Doctor tick={tick} />}
  </>
}

// 模型设置分两组（Codex 执行策略、Claude 协调策略），各自编辑、各自保存：只提交改动的那一组，
// 没动的那组跟随 controller 的最新值，不会把别的客户端刚改的另一组盖回去。
// 保存带读到的 revision，别人先改过就被拒绝并给出当前值；只有另一组被改时自动基于新版本继续，不打扰。
function ModelGroup({ tick, group }: { tick: number; group: 'codex' | 'claude' }) {
  const view = useData(() => api.models(), [tick])
  const [form, setForm] = useState<any>(null)   // 本组的编辑副本；null 表示没有未保存的修改
  const [base, setBase] = useState<{ revision: number; value: any } | null>(null)   // 编辑副本所基于的版本与当时本组的值
  const pending = useRef<{ form: any; revision: number; id: string } | null>(null)
  // 同一时刻只有一个保存请求：并行的两次保存基于同一 revision，先返回的冲突会盖掉后一次成功的提示。
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState<[string, string] | null>(null)
  const [conflict, setConflict] = useState<any>(null)
  const settings = view.data?.settings
  useEffect(() => {
    if (!settings) return
    // 没有未保存的修改时跟随最新值；有修改但本组在服务端没变（只是另一组被改）时，静默改为基于新版本。
    if (!form || (base && base.revision !== settings.revision && same(base.value, settings[group]))) setBase({ revision: settings.revision, value: settings[group] })
  }, [settings?.revision, form]) // eslint-disable-line
  const title = group === 'codex' ? 'Codex 执行策略' : 'Claude 协调策略'
  if (!settings) return <Section title={title}>{view.error ? <p className="err">{view.error}</p> : <p className="muted">加载中…</p>}</Section>
  const editable = can('settings')
  const cur = form ?? settings[group]
  const set = (f: (v: any) => void) => {
    const next = structuredClone(cur)
    f(next)
    // 改回原值不再算修改，不会提交一次相同的设置让 revision 白白递增。
    setForm(same(next, settings[group]) ? null : next)
    setMsg(null)
  }
  const stale = form && base && base.revision !== settings.revision
  const save = async () => {
    setMsg(null); setConflict(null); setSaving(true)
    const sent = form, revision = base?.revision ?? settings.revision
    // 应答丢失后再点“保存”：内容与基准版本都没变就复用 request_id，controller 原样返回第一次的结果。
    if (!pending.current || pending.current.form !== sent || pending.current.revision !== revision) pending.current = { form: sent, revision, id: uuid() }
    try {
      await api.saveModels(revision, group === 'codex' ? sent : undefined, group === 'claude' ? sent : undefined, pending.current.id)
      pending.current = null
      // 保存期间又改了的话保留这些修改。
      setForm((f: any) => f === sent ? null : f); setMsg(['已保存', 'ok']); void view.reload()
    } catch (e) {
      if (e instanceof ApiError && e.code === 'version_mismatch') { setConflict(e.details.current); void view.reload() }
      else setMsg([e instanceof ApiError && e.details?.reason === 'model_not_allowed' ? '默认值不在许可范围内，请先调整许可范围或默认值' : errText(e), 'err'])
    } finally {
      setSaving(false)
    }
  }
  const latest = conflict ?? settings
  const bad = [[cur.default.model, MODEL_RE, '模型'], [cur.default.effort, EFFORT_RE, 'effort']]
    .filter(([v, re]) => v != null && !(re as RegExp).test(v)).map(([, , name]) => name)
  const extraBad = group === 'codex' && !!cur.allowed && !Object.keys(cur.allowed).length
  return <Section title={title} extra={<span className="muted">模型设置第 {settings.revision} 版{settings.updated_at ? ' · ' + actor(settings.updated_by) + ' 修改于 ' + when(settings.updated_at) : ' · 尚未设置'}</span>}>
    {(conflict || stale) && <div className="notice" role="alert">
      <b>{title}已被{actor(latest.updated_by)}修改（模型设置第 {latest.revision} 版），你的修改没有保存。</b>
      <br />当前值：{show(latest[group].default.model)} / {show(latest[group].default.effort)}{group === 'codex' ? '，未核实能力尝试 ' + (latest.codex.unverified_nodes === 'allow' ? '允许' : '等待') : ''}。
      <div className="actions"><button onClick={() => { setForm(null); setConflict(null); setMsg(null) }}>放弃我的修改，载入最新值</button>
        <button onClick={() => { setBase({ revision: settings.revision, value: settings[group] }); setConflict(null) }}>保留我的修改，基于最新版重新保存</button></div>
    </div>}
    {group === 'codex' ? <CodexFields view={view.data} cur={cur} set={set} editable={editable} /> : <>
      <p className="muted">受管协调回合（任务结束或需要处理时，由指定设备上的 Claude 做语义验收）使用的模型与 effort，与执行模型分开设置。Claude CLI 不提供模型目录，这里填写的值在协调回合实际运行时才得到验证。留空则沿用部署配置里的协调 profile。</p>
      <div className="form-grid">
        <label>协调模型<input disabled={!editable} value={cur.default.model ?? ''} placeholder="沿用部署配置" onChange={e => set(v => { v.default.model = e.target.value.trim() || null; if (!v.default.model) v.default.effort = null })} /></label>
        <label>协调推理强度（effort）<input disabled={!editable || !cur.default.model} value={cur.default.effort ?? ''} onChange={e => set(v => { v.default.effort = e.target.value.trim() || null })} /></label>
      </div></>}
    {editable ? <p className="actions">
      <button className="primary" disabled={saving || !form || !!bad.length || extraBad} onClick={() => void save()}>保存{group === 'codex' ? '执行策略' : '协调模型'}</button>
      {!!bad.length && <small className="err">{bad.join('、')}格式不对：模型以字母或数字开头，可含 . _ : / -；effort 为小写字母开头，可含数字、_ 与 -。</small>}
      {form && <button onClick={() => { setForm(null); setMsg(null); setConflict(null) }}>撤销修改</button>}
      {msg && <small className={msg[1]}>{msg[0]}</small>}
    </p> : <p className="muted">这条连接没有“设置”权限（settings），只能查看。</p>}
    <details><summary>修改记录（最近 {(view.data.history ?? []).length} 次）</summary>
      <table><thead><tr><th>版本</th><th>时间</th><th>修改者</th><th>默认值</th>{group === 'codex' && <><th>许可范围</th><th>未核实尝试</th></>}</tr></thead>
        <tbody>{[...(view.data.history ?? [])].reverse().map((h: any) => <tr key={h.revision}><td>{h.revision}</td><td>{when(h.at)}</td><td>{actor(h.actor)}</td>
          <td>{show(h.after[group].default.model)} / {show(h.after[group].default.effort)}</td>
          {group === 'codex' && <><td><small>{h.after.codex.allowed ? Object.entries<string[]>(h.after.codex.allowed).map(([m, e]) => m + '：' + e.join('、')).join('；') : '不限制'}</small></td>
            <td>{h.after.codex.unverified_nodes === 'allow' ? '允许' : '等待'}</td></>}</tr>)}</tbody></table>
    </details>
    {group === 'claude' && <Coordination tick={tick} />}
  </Section>
}

function CodexFields({ view, cur, set, editable }: { view: any; cur: any; set: (f: (v: any) => void) => void; editable: boolean }) {
  const selectable: any[] = view.selectable?.codex ?? []
  // 目录里的全部模型（不受当前许可范围过滤），用来编辑许可范围。
  const catalog = new Map<string, Set<string>>()
  const defaults = new Map<string, string>()   // 设备上报的 default_effort，选模型时优先预选它
  for (const node of Object.values<any>(view.catalog?.nodes ?? {}))
    if (node.codex?.status !== 'unknown') for (const m of node.codex?.models ?? []) {
      catalog.set(m.id, new Set([...(catalog.get(m.id) ?? []), ...m.efforts]))
      if (m.default_effort && !defaults.has(m.id)) defaults.set(m.id, m.default_effort)
    }
  for (const [m, efforts] of Object.entries<string[]>(cur.allowed ?? {})) catalog.set(m, new Set([...(catalog.get(m) ?? []), ...efforts]))
  const allowed = cur.allowed
  const ok = (m: string, e: string) => !allowed || (allowed[m] ?? []).includes(e)
  // 只列至少有一个可用 effort 的模型：没有 effort 的模型选了也保存不了。
  const models = [...catalog].filter(([m, es]) => [...es].some(e => ok(m, e))).map(([m]) => m)
  const firstEffort = (m: string) => { const es = [...(catalog.get(m) ?? [])].filter(e => ok(m, e)), d = defaults.get(m); return d && es.includes(d) ? d : es[0] ?? null }
  const efforts = [...(catalog.get(cur.default.model) ?? [])].filter(e => ok(cur.default.model, e))
  return <>
    <p className="muted">执行任务的工作区默认值，只影响之后受理的任务；已受理的任务（包括排队中的）和它们的修复沿用受理时冻结的值。优先级：任务 &gt; 项目 &gt; 工作区，项目默认值在各项目页设置。</p>
    <div className="form-grid">
      {/* 目录里的组合作为候选，也可以手工填写：设备还没上报目录时同样能设置默认值。 */}
      <label>默认模型<input list="settings-codex-models" disabled={!editable} value={cur.default.model ?? ''} placeholder="未设置（提交任务时必须指定）"
        onChange={e => set(v => { const m = e.target.value.trim(); v.default = m ? { model: m, effort: catalog.has(m) ? firstEffort(m) : v.default.effort } : { model: null, effort: null } })} />
        <datalist id="settings-codex-models">{models.map(m => <option key={m} value={m} />)}</datalist></label>
      <label>默认推理强度（effort）<input list="settings-codex-efforts" disabled={!editable || !cur.default.model} value={cur.default.effort ?? ''}
        onChange={e => set(v => { v.default.effort = e.target.value.trim() || null })} />
        <datalist id="settings-codex-efforts">{efforts.map(x => <option key={x} value={x} />)}</datalist></label>
    </div>
    <fieldset disabled={!editable}><legend>未核实能力尝试</legend>
      <label className="row"><input type="checkbox" checked={cur.unverified_nodes === 'allow'} onChange={e => set(v => { v.unverified_nodes = e.target.checked ? 'allow' : 'wait' })} /> 允许把任务派给模型目录未核实的设备</label>
      <p className="muted">目录未核实（取不到或已过期）的设备缺省不派单，任务停在“等待能力”。开启后它们才会作为未核实能力尝试承接任务，且仍优先目录已核实的设备。只影响之后受理的任务；各项目可以单独覆盖。</p>
    </fieldset>
    <fieldset disabled={!editable}><legend>部署许可范围</legend>
      <label className="row"><input type="radio" checked={!allowed} onChange={() => set(v => { v.allowed = null })} /> 不限制：设备目录里的任何模型与 effort 都可以用</label>
      <label className="row"><input type="radio" checked={!!allowed} onChange={() => set(v => {
        v.allowed = v.default.model ? { [v.default.model]: [v.default.effort] } : Object.fromEntries(selectable.slice(0, 1).map(s => [s.model, s.efforts])) })} /> 只允许下面勾选的组合</label>
      {allowed && <table><thead><tr><th>模型</th><th>允许的 effort</th></tr></thead>
        <tbody>{[...catalog].map(([m, all]) => <tr key={m}><td><code>{m}</code></td>
          <td>{[...all].map(x => <label key={x} className="row inline"><input type="checkbox" checked={(allowed[m] ?? []).includes(x)} onChange={e => set(v => {
            const list = (v.allowed[m] ?? []).filter((y: string) => y !== x)
            if (e.target.checked) list.push(x)
            if (list.length) v.allowed[m] = list; else delete v.allowed[m]
          })} /> {x}</label>)}</td></tr>)}</tbody></table>}
      {allowed && !Object.keys(allowed).length && <p className="warn-text">至少勾选一个组合，或改为“不限制”。</p>}
    </fieldset>
    {!!view.selectable?.unknown_nodes?.length && <p className="muted">设备 {view.selectable.unknown_nodes.join('、')} 的模型目录未核实，上面的可选项不包含它们。</p>}
    {!catalog.size && <p className="muted">还没有设备上报模型目录；设备登记并读取到目录后，这里会出现可选的模型。</p>}
  </>
}

// 协调是否启用与候选节点（§13.7）：与模型设置是两条记录，各自带 revision。
const COORD_STATUS: Record<string, [string, string]> = { ready: ['就绪', 'good'], profile_missing: ['未就绪：缺少协调模型或候选节点', 'warn'], disabled: ['已停用', 'muted'] }
function Coordination({ tick }: { tick: number }) {
  const view = useData(() => api.coordination(), [tick])
  const nodes = useData(() => api.nodes(), [tick])
  const [form, setForm] = useState<{ enabled: boolean; node_ids: string[] } | null>(null)
  const [msg, setMsg] = useState<[string, string] | null>(null)
  const [saving, setSaving] = useState(false)
  const pending = useRef<{ form: any; id: string } | null>(null)
  const [base, setBase] = useState(0)
  const s = view.data?.settings
  if (!s) return view.error ? <p className="err">{view.error}</p> : <p className="muted">读取协调设置…</p>
  const cur: { enabled: boolean; node_ids: string[] } = form ?? { enabled: s.enabled, node_ids: s.node_ids }
  const st = COORD_STATUS[view.data.effective?.status] ?? [show(view.data.effective?.status), 'muted']
  const known: string[] = (nodes.data?.nodes ?? []).map((n: any) => n.node_id)
  const editable = can('settings')
  // 开始编辑时记下所基于的版本：期间别人保存过，提交时按旧版本被拒绝，不会悄悄覆盖。
  const set = (v: { enabled: boolean; node_ids: string[] }) => { if (!form) setBase(s.revision); setForm(same(v, { enabled: s.enabled, node_ids: s.node_ids }) ? null : v); setMsg(null) }
  return <>
    <h3>是否启用与候选节点 <small className="muted">（协调设置第 {s.revision} 版{s.revision === 0 ? '，尚未经界面保存，显示部署配置的当前值' : s.updated_at ? ' · ' + actor(s.updated_by) + ' 修改于 ' + when(s.updated_at) : ''}）</small></h3>
    <p>实际状态 <span className={'badge ' + st[1]}>{st[0]}</span>
      {view.data.effective?.profile && <small className="muted"> · 实际使用 {show(view.data.effective.profile.model)} / {show(view.data.effective.profile.effort)}，设备 {(view.data.effective.profile.node_ids ?? []).join('、') || '无'}</small>}</p>
    <p className="muted">“就绪”只表示配置完整，不代表设备上的 Claude Code 已登录或在线。</p>
    <fieldset disabled={!editable}>
      <label className="row"><input type="checkbox" checked={cur.enabled} onChange={e => set({ ...cur, enabled: e.target.checked })} /> 启用受管协调</label>
      <div>候选节点：{[...new Set([...known, ...cur.node_ids])].map(id => <label key={id} className="row inline"><input type="checkbox" checked={cur.node_ids.includes(id)}
        onChange={e => set({ ...cur, node_ids: e.target.checked ? [...cur.node_ids, id] : cur.node_ids.filter(x => x !== id) })} /> {id}{known.includes(id) ? '' : '（未登记）'}</label>)}
        {!known.length && !cur.node_ids.length && <span className="muted">还没有已登记的设备</span>}</div>
    </fieldset>
    {form && base !== s.revision && <p className="warn-text">协调设置在你编辑期间被修改为第 {s.revision} 版；保存会因版本冲突被拒绝，请撤销修改后重新编辑。</p>}
    {!!view.data.unknown_nodes?.length && <p className="warn-text">候选列表里有不在已登记设备中的 ID：{view.data.unknown_nodes.join('、')}。</p>}
    {editable && <p className="actions">
      <button className="primary" disabled={saving || !form} onClick={async () => {
        setSaving(true); setMsg(null)
        if (!pending.current || pending.current.form !== form) pending.current = { form, id: uuid() }
        try { await api.saveCoordination(base, form!.enabled, form!.node_ids, pending.current.id); pending.current = null; setForm(null); setMsg(['已保存', 'ok']) }
        catch (e) {
          if (e instanceof ApiError && e.code === 'version_mismatch') { setForm(null); setMsg(['协调设置已被别人修改，已载入最新值，你的修改没有保存', 'err']) }
          else setMsg([e instanceof ApiError && e.code === 'node_retired' ? '已退役的设备不能列为候选节点' : errText(e), 'err'])
        } finally { setSaving(false); void view.reload() }
      }}>保存协调设置</button>
      {form && <button onClick={() => { setForm(null); setMsg(null) }}>撤销修改</button>}
      {msg && <small className={msg[1]}>{msg[0]}</small>}
    </p>}
  </>
}

// 各节点上报的模型目录：state 是按有效期算出的当前状态。
function Catalogs({ tick }: { tick: number }) {
  const view = useData(() => api.models(), [tick])
  const nodes = Object.entries<any>(view.data?.catalog?.nodes ?? {})
  const age = view.data?.catalog?.max_age_s
  return <Section title="模型目录" extra={<span className="muted">来自各设备上的 CLI；有效期 {typeof age === 'number' ? Math.round(age / 3600) + ' 小时' : '未知'}</span>}>
    {view.error && <p className="err">{view.error}</p>}
    <table><thead><tr><th>设备</th><th>harness</th><th>当前状态</th><th>上报状态</th><th>取得时间</th><th>到期</th><th>provider</th><th>模型数</th><th>原因</th></tr></thead>
      <tbody>{nodes.flatMap(([id, hs]) => Object.entries<any>(hs).map(([h, c]) => <tr key={id + h}><td>{id}</td><td>{h}</td>
        <td><small>{CATALOG[c.state] ?? show(c.state)}</small></td><td><Badge v={c.status} /></td><td>{when(c.fetched_at)}</td>
        <td>{c.fetched_at && typeof age === 'number' ? when(new Date(Date.parse(c.fetched_at) + age * 1000).toISOString()) : '—'}</td>
        <td>{show(c.provider)}</td><td>{c.models?.length ?? 0}</td><td><small>{c.reason ?? '—'}</small></td></tr>))}</tbody></table>
    {view.data && !nodes.length && <p className="muted">还没有设备上报模型目录。</p>}
  </Section>
}

// 系统通知：本机设置，只在桌面端。
function Notifications() {
  const [v, setV] = useState(notifyPrefs)
  const put = (n: typeof v) => { setV(n); saveNotifyPrefs(n) }
  return <Section title="通知" extra={<span className="muted">只保存在这台设备上</span>}>
    <label className="row"><input type="checkbox" checked={v.enabled} onChange={e => put({ ...v, enabled: e.target.checked })} /> 发送系统通知</label>
    <fieldset disabled={!v.enabled}>{(Object.keys(KIND_LABEL) as Kind[]).map(k => <label key={k} className="row"><input type="checkbox" checked={v[k]} onChange={e => put({ ...v, [k]: e.target.checked })} /> {KIND_LABEL[k]}</label>)}</fieldset>
    <p className="muted">限流：同一任务的同一类事情只通知一次；两条通知之间至少间隔 30 秒，期间到达的合并成一条（例如“3 个任务需要处理”）；连接建立前已有的事件不通知；窗口在前台且正在看该任务时不通知。</p>
  </Section>
}

function Clients({ tick }: { tick: number }) {
  const list = useData(() => api.clients(), [tick])
  const [issued, setIssued] = useState<any>(null)
  const me = session()?.identity.client_id
  // 只能签发不超过本连接权限的配对码（controller 会再检查一次）：只列出本连接持有的 scope。
  const held: string[] = session()?.identity.scopes ?? []
  const roles = Object.keys(ROLE).filter(r => ROLE_SCOPES[r].every(s => held.includes(s)))
  const [scopes, setScopes] = useState<string[]>(() => held.filter(s => ROLE_SCOPES[roles[0] ?? 'viewer'].includes(s)))
  const preset = roles.find(r => ROLE_SCOPES[r].length === scopes.length && ROLE_SCOPES[r].every(s => scopes.includes(s))) ?? ''
  return <Section title="客户端" extra={<span className="muted">桌面端、命令行与自动化各用自己的凭据；撤销一个不影响其他客户端和设备</span>}>
    {list.error && <p className="err">{list.error}</p>}
    <table><thead><tr><th>名称</th><th>类型</th><th>权限</th><th>配对时间</th><th>状态</th><th></th></tr></thead>
      <tbody>{(list.data?.clients ?? []).map((c: any) => <tr key={c.client_id}>
        <td>{c.name}{c.client_id === me ? <small className="muted">（本连接）</small> : null}<br /><small className="muted"><code>{short(c.client_id)}</code></small></td>
        <td>{c.kind}</td><td><small>{c.scopes.map((s: string) => SCOPE[s] ?? s).join('、')}</small></td><td>{when(c.created_at)}</td>
        <td>{c.revoked_at ? <span className="badge muted">已撤销 {when(c.revoked_at)}</span> : <span className="badge good">有效</span>}</td>
        <td>{!c.revoked_at && <Action label="撤销" danger confirm={c.client_id === me ? '撤销本连接后，这个窗口会立刻断开，需要重新配对才能再连接。确定？' : '撤销后该客户端立刻无法访问，已打开的事件流会被关闭。确定？'}
          run={() => api.revokeClient(c.client_id, c.version).then(list.reload)} />}</td></tr>)}</tbody></table>
    <div className="actions">
      <select aria-label="新客户端的权限预设" value={preset} onChange={e => e.target.value && setScopes(ROLE_SCOPES[e.target.value])}>
        <option value="">自定义</option>{roles.map(k => <option key={k} value={k}>{ROLE[k]}</option>)}</select>
      {held.map(s => <label key={s} className="row inline"><input type="checkbox" checked={scopes.includes(s)}
        onChange={e => setScopes(v => e.target.checked ? held.filter(x => x === s || v.includes(x)) : v.filter(x => x !== s))} /> {SCOPE[s] ?? s}</label>)}
      <Action label="生成配对码" disabled={!scopes.length} run={() => api.issueCode(scopes).then(setIssued)} />
    </div>
    {issued && <p className="notice">配对码（只显示这一次，{when(issued.expires_at)}过期，只能用一次）：<code className="code">{issued.code}</code>
      <br /><small className="muted">权限：{issued.scopes.map((s: string) => SCOPE[s] ?? s).join('、')}。在另一台设备的桌面端“配对新的 controller”里输入，或用于 <code>agent-collab client pair</code>。</small>
      <button className="link" onClick={() => setIssued(null)}>关闭</button></p>}
  </Section>
}

// 界面偏好只存在本机，不进 controller 的设置。
function Preferences() {
  const [theme, setTheme] = useState(() => { try { return localStorage.getItem('ac_theme') ?? '' } catch { return '' } })
  return <Section title="本机偏好" extra={<span className="muted">只保存在这台设备上</span>}>
    <label className="narrow">主题<select value={theme} onChange={e => {
      setTheme(e.target.value); applyTheme(e.target.value)
      try { e.target.value ? localStorage.setItem('ac_theme', e.target.value) : localStorage.removeItem('ac_theme') } catch { /* 存储不可用时只在本次生效 */ }
    }}><option value="">跟随系统</option><option value="light">浅色</option><option value="dark">深色</option></select></label>
  </Section>
}

function Doctor({ tick }: { tick: number }) {
  const doctor = useData(() => api.doctor(), [tick])
  const doc = doctor.data ?? {}
  return <Section title="controller 自检">
    {doctor.error && <p className="err">{doctor.error}</p>}
    <dl className="kv">
      <dt>数据库</dt><dd>{doc.database ? <><Badge v={doc.database.writable ? 'ok' : 'failed'} /> {show(doc.database.journal_mode)}</> : '未知'}</dd>
      <dt>公开入口</dt><dd>{show(doc.public_origin)}{doc.tls === false ? '（controller 直接监听未启用 TLS；生产应在反向代理终止 TLS）' : ''}</dd>
      <dt>最近备份</dt><dd>{doc.backup_age_s === 'unknown' || doc.backup_age_s == null ? '未知（尚未配置或未完成实例外备份）' : show(doc.backup_age_s) + ' 秒前'}</dd>
      <dt>可用空间</dt><dd>{typeof doc.free_bytes === 'number' ? Math.round(doc.free_bytes / 1048576) + ' MiB' : '未知'}</dd>
    </dl>
    <details><summary>完整自检结果</summary><Json v={doctor.data} /></details>
  </Section>
}
