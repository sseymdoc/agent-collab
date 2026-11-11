import { useEffect, useState } from 'react'
import { api, ApiError } from '../api'
import { can, session } from '../session'
import { isDesktop } from '../transport'
import { Action, Badge, Json, ORIGIN, Section, applyTheme, errText, show, short, useData, when } from '../ui'

const SCOPE: Record<string, string> = { read: '读取', tasks: '任务', settings: '设置', devices: '设备', maintain: '维护' }
const ROLE: Record<string, string> = { owner: '拥有者（全部权限）', operator: '操作者（读取 + 任务）', viewer: '只读' }
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
    <Models tick={tick} />
    {can('maintain') && <Clients tick={tick} />}
    <Preferences />
    {can('maintain') && <Doctor tick={tick} />}
  </>
}

// 模型默认值：controller 保存，带 revision。保存时带上读到的 revision，别人先改过就被拒绝并给出当前值，不会覆盖。
function Models({ tick }: { tick: number }) {
  const view = useData(() => api.models(), [tick])
  // 只保存改动过的那一组（codex / claude）：没动的那组跟随 controller 的最新值，保存时也不提交，
  // 这样不会把别的客户端刚改的另一组盖回去。null 表示没有未保存的修改。
  const [form, setForm] = useState<{ codex?: any; claude?: any } | null>(null)
  const [base, setBase] = useState<number | null>(null)   // 编辑副本所基于的 revision
  const [msg, setMsg] = useState<[string, string] | null>(null)
  const [conflict, setConflict] = useState<any>(null)
  const settings = view.data?.settings
  // 没有未保存的修改时跟随 controller 的最新值。
  useEffect(() => { if (settings && !form) setBase(settings.revision) }, [settings?.revision, form]) // eslint-disable-line
  if (!settings) return <Section title="模型默认值">{view.error ? <p className="err">{view.error}</p> : <p className="muted">加载中…</p>}</Section>
  const editable = can('settings')
  const cur = { codex: form?.codex ?? settings.codex, claude: form?.claude ?? settings.claude }
  const set = (f: (v: any) => void) => {
    const next = structuredClone(cur)
    f(next)
    // 与 controller 当前值比较：改回原值的那组不再算修改，两组都没改时清空表单，不会提交一次相同的设置让 revision 白白递增。
    const changed = (k: 'codex' | 'claude') => same(next[k], settings[k]) ? {} : { [k]: next[k] }
    const edited = { ...changed('codex'), ...changed('claude') }
    setForm(Object.keys(edited).length ? edited : null)
    setMsg(null)
  }
  const selectable: any[] = view.data.selectable?.codex ?? []
  // 目录里的全部模型（不受当前许可范围过滤），用来编辑许可范围。
  const catalog = new Map<string, Set<string>>()
  const defaults = new Map<string, string>()   // 设备上报的 default_effort，选模型时优先预选它
  for (const node of Object.values<any>(view.data.catalog?.nodes ?? {}))
    if (node.codex?.status !== 'unknown') for (const m of node.codex?.models ?? []) {
      catalog.set(m.id, new Set([...(catalog.get(m.id) ?? []), ...m.efforts]))
      if (m.default_effort && !defaults.has(m.id)) defaults.set(m.id, m.default_effort)
    }
  for (const [m, efforts] of Object.entries<string[]>(cur.codex.allowed ?? {})) catalog.set(m, new Set([...(catalog.get(m) ?? []), ...efforts]))
  const allowed = cur.codex.allowed
  const ok = (m: string, e: string) => !allowed || (allowed[m] ?? []).includes(e)
  // 只列至少有一个可用 effort 的模型：没有 effort 的模型选了也保存不了。
  const models = [...catalog].filter(([m, es]) => [...es].some(e => ok(m, e))).map(([m]) => m)
  const firstEffort = (m: string) => { const es = [...(catalog.get(m) ?? [])].filter(e => ok(m, e)), d = defaults.get(m); return d && es.includes(d) ? d : es[0] ?? null }
  const efforts = [...(catalog.get(cur.codex.default.model) ?? [])].filter(e => ok(cur.codex.default.model, e))
  const stale = form && base !== settings.revision
  const save = async () => {
    setMsg(null); setConflict(null)
    try {
      await api.saveModels(base ?? settings.revision, form?.codex, form?.claude)
      setForm(null); setMsg(['已保存', 'ok']); void view.reload()
    } catch (e) {
      if (e instanceof ApiError && e.code === 'version_mismatch') { setConflict(e.details.current); void view.reload() }
      else setMsg([e instanceof ApiError && e.details?.reason === 'model_not_allowed' ? '默认值不在许可范围内，请先调整许可范围或默认值' : errText(e), 'err'])
    }
  }
  return <Section title="模型默认值" extra={<span className="muted">第 {settings.revision} 版{settings.updated_at ? ' · ' + actor(settings.updated_by) + ' 修改于 ' + when(settings.updated_at) : ' · 尚未设置'}</span>}>
    <p className="muted">默认值只影响之后新建的任务。已受理的任务（包括排队中的）和它们的修复，沿用受理时冻结的模型与 effort。</p>
    {(conflict || stale) && <div className="notice" role="alert">
      <b>设置已被{actor((conflict ?? settings).updated_by)}修改为第 {(conflict ?? settings).revision} 版，你的修改没有保存。</b>
      <br />当前值：执行 {show((conflict ?? settings).codex.default.model)} / {show((conflict ?? settings).codex.default.effort)}，协调 {show((conflict ?? settings).claude.default.model)} / {show((conflict ?? settings).claude.default.effort)}。
      <div className="actions"><button onClick={() => { setForm(null); setConflict(null); setMsg(null) }}>放弃我的修改，载入最新值</button>
        <button onClick={() => { setBase(settings.revision); setConflict(null) }}>保留我的修改，基于最新版重新保存</button></div>
    </div>}
    <h3>执行（Codex）</h3>
    <div className="form-grid">
      <label>默认模型<select disabled={!editable} value={cur.codex.default.model ?? ''} onChange={e => set(v => { v.codex.default = e.target.value ? { model: e.target.value, effort: firstEffort(e.target.value) } : { model: null, effort: null } })}>
        <option value="">未设置（提交任务时必须指定）</option>{models.map(m => <option key={m} value={m}>{m}</option>)}</select></label>
      <label>默认推理强度（effort）<select disabled={!editable || !cur.codex.default.model} value={cur.codex.default.effort ?? ''} onChange={e => set(v => { v.codex.default.effort = e.target.value || null })}>
        {!cur.codex.default.model && <option value="">—</option>}{efforts.map(x => <option key={x} value={x}>{x}</option>)}</select></label>
    </div>
    <fieldset disabled={!editable}><legend>部署许可范围</legend>
      <label className="row"><input type="radio" checked={!allowed} onChange={() => set(v => { v.codex.allowed = null })} /> 不限制：设备目录里的任何模型与 effort 都可以用</label>
      <label className="row"><input type="radio" checked={!!allowed} onChange={() => set(v => {
        v.codex.allowed = v.codex.default.model ? { [v.codex.default.model]: [v.codex.default.effort] } : Object.fromEntries(selectable.slice(0, 1).map(s => [s.model, s.efforts])) })} /> 只允许下面勾选的组合</label>
      {allowed && <table><thead><tr><th>模型</th><th>允许的 effort</th></tr></thead>
        <tbody>{[...catalog].map(([m, all]) => <tr key={m}><td><code>{m}</code></td>
          <td>{[...all].map(x => <label key={x} className="row inline"><input type="checkbox" checked={(allowed[m] ?? []).includes(x)} onChange={e => set(v => {
            const list = (v.codex.allowed[m] ?? []).filter((y: string) => y !== x)
            if (e.target.checked) list.push(x)
            if (list.length) v.codex.allowed[m] = list; else delete v.codex.allowed[m]
          })} /> {x}</label>)}</td></tr>)}</tbody></table>}
      {allowed && !Object.keys(allowed).length && <p className="warn-text">至少勾选一个组合，或改为“不限制”。</p>}
    </fieldset>
    {!!view.data.selectable?.unknown_nodes?.length && <p className="muted">设备 {view.data.selectable.unknown_nodes.join('、')} 的模型目录未知，上面的可选项不包含它们。</p>}
    {!catalog.size && <p className="muted">还没有设备上报模型目录；设备登记并读取到目录后，这里会出现可选的模型。</p>}
    <h3>协调（Claude）</h3>
    <p className="muted">与执行模型分开设置，互不影响。Claude CLI 不提供模型目录，这里填写的值在协调回合实际运行时才得到验证。留空则沿用部署配置里的协调 profile。</p>
    <div className="form-grid">
      <label>协调模型<input disabled={!editable} value={cur.claude.default.model ?? ''} placeholder="沿用部署配置" onChange={e => set(v => { v.claude.default.model = e.target.value.trim() || null; if (!v.claude.default.model) v.claude.default.effort = null })} /></label>
      <label>协调推理强度（effort）<input disabled={!editable || !cur.claude.default.model} value={cur.claude.default.effort ?? ''} onChange={e => set(v => { v.claude.default.effort = e.target.value.trim() || null })} /></label>
    </div>
    {editable ? <p className="actions">
      <button className="primary" disabled={!form || (!!allowed && !Object.keys(allowed).length)} onClick={() => void save()}>保存</button>
      {form && <button onClick={() => { setForm(null); setMsg(null); setConflict(null) }}>撤销修改</button>}
      {msg && <small className={msg[1]}>{msg[0]}</small>}
    </p> : <p className="muted">这条连接没有“设置”权限，只能查看。</p>}
    <details><summary>修改记录（最近 {(view.data.history ?? []).length} 次）</summary>
      <table><thead><tr><th>版本</th><th>时间</th><th>修改者</th><th>执行默认值</th><th>协调默认值</th><th>许可范围</th></tr></thead>
        <tbody>{[...(view.data.history ?? [])].reverse().map((h: any) => <tr key={h.revision}><td>{h.revision}</td><td>{when(h.at)}</td><td>{actor(h.actor)}</td>
          <td>{show(h.after.codex.default.model)} / {show(h.after.codex.default.effort)}</td><td>{show(h.after.claude.default.model)} / {show(h.after.claude.default.effort)}</td>
          <td><small>{h.after.codex.allowed ? Object.entries<string[]>(h.after.codex.allowed).map(([m, e]) => m + '：' + e.join('、')).join('；') : '不限制'}</small></td></tr>)}</tbody></table>
    </details>
    <p className="muted">项目级默认值（任务覆盖 &gt; 项目默认 &gt; 工作区默认）将随项目管理一起提供，目前只有工作区默认值。</p>
  </Section>
}

function Clients({ tick }: { tick: number }) {
  const list = useData(() => api.clients(), [tick])
  const [role, setRole] = useState('owner')
  const [issued, setIssued] = useState<any>(null)
  const me = session()?.identity.client_id
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
      <select aria-label="新客户端的权限" value={role} onChange={e => setRole(e.target.value)}>{Object.entries(ROLE).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
      <Action label="生成配对码" run={() => api.issueCode(role).then(setIssued)} />
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
