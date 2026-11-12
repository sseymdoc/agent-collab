import { useEffect, useRef, useState } from 'react'
import { invoke } from '../transport'
import { short } from '../ui'
import { BuildMark, LocalService, Wizard } from './Wizard'

// 桌面端的连接页：配对、选择、忘记连接。凭据由外壳写入系统凭据存储，页面只看到连接的非机密信息。
const ERR: Record<string, string> = {
  secure_storage_unavailable: '系统凭据存储不可用，凭据没有保存。请确认已登录系统的图形会话后重试。',
  controller_mismatch: '应答不是来自这条连接配对的 controller，已停止使用该连接。请检查地址，或重新配对。',
  stale_connection: '连接已切换，之前的请求已作废。',
  unauthorized: '凭据已失效或被撤销，请重新配对。',
  forbidden: '这份凭据没有执行该操作的权限；连接本身仍然有效。',
}
// 原生桥的错误带 controller 给出的具体原因（如配对码过期）时优先显示它。
export const bridgeText = (e: any) => e?.message ? (ERR[e.code] && e.code !== 'unauthorized' ? ERR[e.code] : e.message) : ERR[e?.code] ?? String(e)

// onOpen 的第三个参数报告这个流程是否仍是最新的：进入主界面之前（身份请求返回之后）还要再核对一次。
export function Connect({ reason, onOpen }: { reason: string | null; onOpen: (opened: any, connection: any, live: () => boolean) => Promise<void> }) {
  const [list, setList] = useState<any[] | null>(null)
  const [info, setInfo] = useState<any>(null)
  const [err, setErr] = useState<string | null>(reason ? ERR[reason] ?? reason : null)
  const [busy, setBusy] = useState(false)
  const [url, setUrl] = useState('')
  const [code, setCode] = useState('')
  const [name, setName] = useState('桌面端')
  const [customCa, setCustomCa] = useState(false)
  // 没有任何已配对连接时先显示首次使用引导；选择“只连接已有的 controller”后显示配对表单。
  const [guide, setGuide] = useState<boolean | null>(null)
  const reload = () => invoke('connections_list').then(d => { setList(d.connections); setGuide(g => g ?? !d.connections.length) }, e => setErr(bridgeText(e)))
  useEffect(() => { void reload(); invoke('app_info').then(setInfo, () => {}) }, [])
  const run = async (f: () => Promise<void>) => {
    setBusy(true); setErr(null)
    try { await f() } catch (e) { setErr(bridgeText(e)) } finally { setBusy(false) }
  }
  // 连接流程的代次：这里打开已有连接、配对新连接、向导打开本机连接，谁后开始谁有效。先开始的流程在每次等待之后
  // 核对代次，已被取代就停下，不再调用 connection_open 或 onOpen，不会把用户刚选的连接换回去。
  const flow = useRef(0)
  const begin = () => ++flow.current
  const current = (g: number) => g === flow.current
  // c 可以是连接，也可以是取得连接的函数（例如向导的 local_pair）：被取代的流程连这一步也不做。
  const openFor = async (g: number, c: any) => {
    if (!current(g)) return
    if (typeof c === 'function') {
      c = await c()
      if (!current(g)) return
    }
    const opened = await invoke('connection_open', { id: c.id })
    if (current(g)) return onOpen(opened, c, () => current(g))
    // 打开期间被别的流程取代：页面不进入它，原生侧也不留着这条活动连接。
    await invoke('connection_close', { epoch: opened.epoch }).catch(() => {})
  }
  useEffect(() => () => { flow.current++ }, [])
  // 等身份确认（GET /client）完成再解除 busy：期间再点一次会换掉连接代次，先发的确认随后失败会把新连接关掉。
  const open = (c: any) => { const g = begin(); return run(() => openFor(g, c)) }
  return <main className="connect">
    <h1>Agent Collab</h1>
    {err && <p className="err" role="alert">{err}</p>}
    {!!list?.length && <section className="card">
      <header><h2>已配对的 controller</h2></header>
      <table><thead><tr><th>名称</th><th>地址</th><th>controller</th><th>权限</th><th></th></tr></thead>
        <tbody>{list.map(c => <tr key={c.id}>
          <td>{c.name}{c.custom_ca ? <><br /><small className="muted" title={c.ca_fingerprint}>自建 CA{c.ca_fingerprint ? ' · ' + String(c.ca_fingerprint).slice(0, 16) + '…' : ''}</small></> : null}{c.revoked_at ? <> <span className="badge muted" title={c.revoked_at}>已撤销，待清理</span></> : null}</td><td><code>{c.url}</code></td><td><code title={c.controller_id}>{short(c.controller_id)}</code></td>
          <td><small>{(c.scopes ?? []).join('、')}</small></td>
          <td className="actions">
            {/* controller 已确认撤销、本机凭据还没删：连不上了，只留“忘记”来清理。 */}
            {!c.revoked_at && <button disabled={busy} onClick={() => open(c)}>连接</button>}
            <button className="link" disabled={busy} onClick={() => {
              if (!window.confirm(c.revoked_at ? 'controller 已撤销这条连接，忘记只删除本机保存的凭据。确定？' : '忘记这条连接会删除本机保存的凭据，并在 controller 上撤销它。确定？')) return
              void run(async () => {
                // 删除本机凭据失败时外壳会抛错，但记录可能已变为“已撤销，待清理”：无论成败都刷新列表。
                try {
                  const r = await invoke('connection_forget', { id: c.id, revoke: true })
                  if (!r.revoked) setErr('本机的连接与凭据已删除，但没能确认 controller 已撤销这个客户端（controller 不可达、拒绝了请求、应答身份不符或本机读不到凭据）。请在另一客户端的“设置 → 客户端”里确认并撤销它。')
                } finally {
                  await reload()
                }
              })
            }}>忘记</button>
          </td>
        </tr>)}</tbody></table>
    </section>}
    {guide && <Wizard info={info} begin={begin} openFor={openFor} onConnect={() => { begin(); setGuide(false) }} />}
    {guide === false && <form className="card" onSubmit={e => {
      e.preventDefault()
      const g = begin()
      void run(async () => {
        const c = await invoke('connection_pair', { url: url.trim(), code: code.trim(), name: name.trim(), custom_ca: customCa })
        // 取消选取 CA 证书：没有配对，配对码也没有消耗。
        if (c.cancelled) return setErr('已取消选取 CA 证书，没有配对；配对码仍可使用。')
        setCode('')
        // 配对码已用掉：先刷新列表，接下来打开失败时这条连接仍在列表里可以重试。
        await reload()
        await openFor(g, c)
      })
    }}>
      <header><h2>配对新的 controller</h2></header>
      <p className="muted">在 controller 所在的机器上运行 <code>agent-collab controller client-code --data-dir &lt;数据目录&gt;</code> 取得配对码。
        配对码 10 分钟内有效，只能用一次；换来的凭据保存在本机的系统凭据存储里，可以随时单独撤销。</p>
      <label>controller 地址<input value={url} onChange={e => setUrl(e.target.value)} placeholder="https://controller.example.com:8443" required /></label>
      <label>配对码<input value={code} onChange={e => setCode(e.target.value)} autoComplete="off" required /></label>
      <label>这台设备的名称<input value={name} onChange={e => setName(e.target.value)} maxLength={80} required /></label>
      <label className="row"><input type="checkbox" checked={customCa} onChange={e => setCustomCa(e.target.checked)} /> 使用自建 CA 证书</label>
      {customCa && <p className="muted">配对时会弹出打开文件对话框，选取签发 controller 证书的 CA（PEM）。之后这条连接只信任这份 CA，系统根证书对它不再生效；主机名照常核对。用于局域网里的本机 controller 等自建 CA 部署。</p>}
      <button type="submit" disabled={busy || !url.trim() || !code.trim() || !name.trim()}>{busy ? '处理中…' : '配对并连接'}</button>
    </form>}
    {guide === false && <p><button className="link" onClick={() => setGuide(true)}>{list?.length ? '本机部署，或把这台机器作为节点加入' : '返回首次使用引导'}</button></p>}
    <LocalService />
    <BuildMark info={info} />
  </main>
}
