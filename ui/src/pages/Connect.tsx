import { useEffect, useState } from 'react'
import { invoke } from '../transport'
import { short } from '../ui'

// 桌面端的连接页：配对、选择、忘记连接。凭据由外壳写入系统凭据存储，页面只看到连接的非机密信息。
const ERR: Record<string, string> = {
  secure_storage_unavailable: '系统凭据存储不可用，凭据没有保存。请确认已登录系统的图形会话后重试。',
  controller_mismatch: '应答不是来自这条连接配对的 controller，已停止使用该连接。请检查地址，或重新配对。',
  stale_connection: '连接已切换，之前的请求已作废。',
  unauthorized: '凭据已失效或被撤销，请重新配对。',
}
// 原生桥的错误带 controller 给出的具体原因（如配对码过期）时优先显示它。
export const bridgeText = (e: any) => e?.message ? (ERR[e.code] && e.code !== 'unauthorized' ? ERR[e.code] : e.message) : ERR[e?.code] ?? String(e)

export function Connect({ reason, onOpen }: { reason: string | null; onOpen: (opened: any, connection: any) => Promise<void> }) {
  const [list, setList] = useState<any[] | null>(null)
  const [info, setInfo] = useState<any>(null)
  const [err, setErr] = useState<string | null>(reason ? ERR[reason] ?? reason : null)
  const [busy, setBusy] = useState(false)
  const [url, setUrl] = useState('')
  const [code, setCode] = useState('')
  const [name, setName] = useState('桌面端')
  const reload = () => invoke('connections_list').then(d => setList(d.connections), e => setErr(bridgeText(e)))
  useEffect(() => { void reload(); invoke('app_info').then(setInfo, () => {}) }, [])
  const run = async (f: () => Promise<void>) => {
    setBusy(true); setErr(null)
    try { await f() } catch (e) { setErr(bridgeText(e)) } finally { setBusy(false) }
  }
  // 等身份确认（GET /client）完成再解除 busy：期间再点一次会换掉连接代次，先发的确认随后失败会把新连接关掉。
  const open = (c: any) => run(async () => { await onOpen(await invoke('connection_open', { id: c.id }), c) })
  return <main className="connect">
    <h1>Agent Collab</h1>
    {err && <p className="err" role="alert">{err}</p>}
    {!!list?.length && <section className="card">
      <header><h2>已配对的 controller</h2></header>
      <table><thead><tr><th>名称</th><th>地址</th><th>controller</th><th>权限</th><th></th></tr></thead>
        <tbody>{list.map(c => <tr key={c.id}>
          <td>{c.name}</td><td><code>{c.url}</code></td><td><code title={c.controller_id}>{short(c.controller_id)}</code></td>
          <td><small>{(c.scopes ?? []).join('、')}</small></td>
          <td className="actions">
            <button disabled={busy} onClick={() => open(c)}>连接</button>
            <button className="link" disabled={busy} onClick={() => {
              if (!window.confirm('忘记这条连接会删除本机保存的凭据，并在 controller 上撤销它。确定？')) return
              void run(async () => {
                const r = await invoke('connection_forget', { id: c.id, revoke: true })
                if (!r.revoked) setErr('本机的连接与凭据已删除，但没能确认 controller 已撤销这个客户端（controller 不可达、拒绝了请求、应答身份不符或本机读不到凭据）。请在另一客户端的“设置 → 客户端”里确认并撤销它。')
                await reload()
              })
            }}>忘记</button>
          </td>
        </tr>)}</tbody></table>
    </section>}
    <form className="card" onSubmit={e => {
      e.preventDefault()
      void run(async () => {
        const c = await invoke('connection_pair', { url: url.trim(), code: code.trim(), name: name.trim() })
        setCode('')
        // 配对码已用掉：先刷新列表，接下来打开失败时这条连接仍在列表里可以重试。
        await reload()
        await onOpen(await invoke('connection_open', { id: c.id }), c)
      })
    }}>
      <header><h2>配对新的 controller</h2></header>
      <p className="muted">在 controller 所在的机器上运行 <code>agent-collab controller client-code --data-dir &lt;数据目录&gt;</code> 取得配对码。
        配对码 10 分钟内有效，只能用一次；换来的凭据保存在本机的系统凭据存储里，可以随时单独撤销。</p>
      <label>controller 地址<input value={url} onChange={e => setUrl(e.target.value)} placeholder="https://controller.example.com:8443" required /></label>
      <label>配对码<input value={code} onChange={e => setCode(e.target.value)} autoComplete="off" required /></label>
      <label>这台设备的名称<input value={name} onChange={e => setName(e.target.value)} maxLength={80} required /></label>
      <button type="submit" disabled={busy || !url.trim() || !code.trim() || !name.trim()}>{busy ? '处理中…' : '配对并连接'}</button>
    </form>
    {info && <p className="muted"><small>桌面端 {info.version}{info.commit ? ' · ' + String(info.commit).slice(0, 7) : ''} · {info.platform}/{info.arch}
      {info.candidate ? ' · 开发候选版（未签名，不是正式发行版）' : ''}</small></p>}
  </main>
}
