import { StrictMode, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { api, ApiError, onUnauthorized } from './api'
import { useLive } from './live'
import { errText } from './ui'
import { Overview } from './pages/Overview'
import { Nodes } from './pages/Nodes'
import { Jobs } from './pages/Jobs'
import { JobDetail } from './pages/JobDetail'
import { Settings } from './pages/Settings'
import './styles.css'

function useHash() {
  const [h, setH] = useState(location.hash || '#/overview')
  useEffect(() => { const f = () => setH(location.hash || '#/overview'); addEventListener('hashchange', f); return () => removeEventListener('hashchange', f) }, [])
  return h
}

function Login({ onDone }: { onDone: () => void }) {
  const [token, setToken] = useState('')
  const [err, setErr] = useState<string | null>(null)
  return <main className="login">
    <form className="card" onSubmit={async e => {
      e.preventDefault(); setErr(null)
      try { await api.login(token.trim()); setToken(''); onDone() } catch (x) { setErr(errText(x)) }
    }}>
      <h1>agent-collab 控制台</h1>
      <p className="muted">输入 controller 数据目录中的管理令牌（admin.token）。令牌换取本地会话 Cookie，不保存在浏览器存储中。</p>
      <label>管理令牌<input type="password" autoComplete="off" value={token} onChange={e => setToken(e.target.value)} autoFocus /></label>
      <button type="submit" disabled={!token.trim()}>登录</button>
      {err && <p className="err">{err}</p>}
    </form>
  </main>
}

function App() {
  const [authed, setAuthed] = useState<boolean | null>(null)
  const hash = useHash()
  const live = useLive(authed === true)
  const [bootErr, setBootErr] = useState<string | null>(null)
  useEffect(() => {
    onUnauthorized(() => setAuthed(false))
    api.resume().then(() => setAuthed(true), e => {
      if (e instanceof ApiError && (e.status === 401 || e.status === 403)) setAuthed(false)
      else setBootErr(errText(e))
    })
  }, [])
  // 事件流断开时探测会话；页面空闲时会话过期也能由 401 切回登录页。
  useEffect(() => { if (authed && live.state === 'retrying') api.resume().catch(() => {}) }, [authed, live.state])
  if (bootErr) return <main className="login"><p className="err">无法连接 controller：{bootErr}</p></main>
  if (authed === null) return <main className="login"><p className="muted">连接 controller…</p></main>
  if (!authed) return <Login onDone={() => setAuthed(true)} />
  const [, page, id] = hash.split('/')
  const nav = [['overview', '总览'], ['nodes', '节点'], ['jobs', '工作包'], ['settings', '设置与诊断']]
  const conn = live.state === 'open' ? ['good', '事件流已连接'] : live.state === 'retrying' ? ['bad', '事件流重连中'] : ['warn', '事件流连接中']
  return <>
    <header className="top">
      <strong>agent-collab</strong>
      <nav>{nav.map(([k, t]) => <a key={k} href={'#/' + k} className={page === k ? 'on' : ''}>{t}</a>)}</nav>
      <span className={'badge ' + conn[0]} title={'最后事件序号 ' + (live.lastSeq ?? '无')}>{conn[1]}</span>
      {/* 服务端会话可能已失效（DELETE 返回 401），无论结果都回到登录页。 */}
      <button className="link" onClick={() => { void api.logout().catch(() => {}).finally(() => setAuthed(false)) }}>退出</button>
    </header>
    <main>
      {page === 'nodes' ? <Nodes tick={live.tick} />
        : page === 'jobs' && id ? <JobDetail id={id} tick={live.tick} />
        : page === 'jobs' ? <Jobs tick={live.tick} />
        : page === 'settings' ? <Settings tick={live.tick} />
        : <Overview tick={live.tick} />}
    </main>
  </>
}

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>)
