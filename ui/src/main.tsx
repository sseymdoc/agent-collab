import { StrictMode, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { api, ApiError, bindCoordinators, onUnauthorized } from './api'
import { useLive } from './live'
import { session, setSession } from './session'
import { currentEpoch, invoke, isDesktop, setEpoch } from './transport'
import { applyTheme, errText, short } from './ui'
import { Connect, bridgeText } from './pages/Connect'
import { Overview } from './pages/Overview'
import { Nodes } from './pages/Nodes'
import { Jobs } from './pages/Jobs'
import { JobDetail } from './pages/JobDetail'
import { Settings } from './pages/Settings'
import './styles.css'

try { applyTheme(localStorage.getItem('ac_theme')) } catch { /* 存储不可用时跟随系统 */ }

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
  // 每次建立连接 generation 加一，主界面按它整体重建：上一条连接留下的弹窗、表单和在途请求的结果随之丢弃。
  const [phase, setPhase] = useState<{ at: 'boot' | 'login' | 'ready'; generation: number; reason: string | null; error: string | null }>(
    { at: 'boot', generation: 0, reason: null, error: null })
  const enter = async () => {
    const who = await api.whoami()
    // 换了 controller 才丢弃协调令牌，重连同一个不必重新接管自己的租约。
    bindCoordinators(who.controller.controller_id)
    setSession(who)
    setPhase(p => ({ at: 'ready', generation: p.generation + 1, reason: null, error: null }))
  }
  const leave = (reason: string | null) => {
    setSession(null)
    // 只关闭页面当前使用的那条连接（transport 里的代次）；它已不是外壳的当前连接时外壳答 stale_connection，忽略即可。
    if (isDesktop) void invoke('connection_close', { epoch: currentEpoch() }).catch(() => {})
    setPhase(p => ({ ...p, at: 'login', reason, error: null }))
  }
  const boot = async () => {
    setPhase(p => ({ ...p, at: 'boot', error: null }))
    try {
      if (isDesktop) {
        const { active } = await invoke('connections_list')
        if (!active) return leave(null)
        setEpoch(active.epoch)
      } else await api.resume()
      await enter()
    } catch (e) {
      if (isDesktop) return setPhase(p => ({ ...p, at: 'login', reason: null, error: e instanceof ApiError ? errText(e) : bridgeText(e) }))
      if (e instanceof ApiError && (e.status === 401 || e.status === 403)) leave(null)
      else setPhase(p => ({ ...p, error: errText(e) }))
    }
  }
  useEffect(() => { onUnauthorized(leave); void boot() }, []) // eslint-disable-line
  if (phase.at === 'boot') return <main className="login">{phase.error
    ? <div className="card"><p className="err">无法连接 controller：{phase.error}</p>
      <div className="actions"><button onClick={() => void boot()}>重试</button><button className="link" onClick={() => leave(null)}>改用令牌登录</button></div></div>
    : <p className="muted">连接 controller…</p>}</main>
  if (phase.at === 'login') return isDesktop
    ? <Connect key={phase.reason ?? phase.error ?? ''} reason={phase.reason ?? phase.error} onOpen={async opened => {
      setEpoch(opened.epoch)
      // 只在失败的仍是当前这条连接时离开：期间若已换了连接，旧身份请求的失败不能关掉新连接。
      await enter().catch(e => { if (currentEpoch() === opened.epoch) leave(errText(e)) })
    }} />
    : <Login onDone={() => void enter().catch(e => setPhase(p => ({ ...p, at: 'boot', error: errText(e) })))} />
  // 浏览器退出要等 DELETE /session 答复后再显示登录页：否则马上重新登录时，迟到的旧答复会清掉新会话的 Cookie 和 CSRF 令牌。
  return <Shell key={phase.generation} onLeave={() => isDesktop ? leave(null) : void api.logout().catch(() => {}).finally(() => leave(null))} />
}

// 导航按“总览 → 任务 → 设备 → 设置”排列；项目与活动两页在后续版本加入同一列表。
const NAV = [['overview', '总览'], ['jobs', '任务'], ['nodes', '设备'], ['settings', '设置']]

function Shell({ onLeave }: { onLeave: () => void }) {
  const hash = useHash()
  const live = useLive(true)
  const who = session()
  // 浏览器会话可能在页面空闲时过期：事件流断开期间定时探测，过期由 401 切回登录页。
  useEffect(() => {
    if (isDesktop || live.state !== 'retrying') return
    const probe = () => { void api.resume().catch(() => {}) }
    probe()
    const timer = window.setInterval(probe, 5000)
    return () => window.clearInterval(timer)
  }, [live.state])
  const [, page, id] = hash.split('/')
  const at = live.lastAt ? new Date(live.lastAt).toLocaleTimeString('zh-CN', { hour12: false }) : '尚无'
  const conn = live.state === 'open' ? ['good', '实时'] : live.state === 'retrying' ? ['bad', '连接中断 · 数据截至 ' + at] : ['warn', '连接中']
  return <>
    <header className="top">
      <strong>Agent Collab</strong>
      <nav>{NAV.map(([k, t]) => <a key={k} href={'#/' + k} className={page === k || (!page && k === 'overview') ? 'on' : ''}>{t}</a>)}</nav>
      <span className={'badge ' + conn[0]} title={'最后事件序号 ' + (live.lastSeq ?? '无')}>{conn[1]}</span>
      <small className="muted" title={'controller ' + who?.controller.controller_id}>{who?.identity.name ?? (who?.identity.kind === 'session' ? '浏览器会话' : '管理令牌')} · controller {short(who?.controller.controller_id)}</small>
      <button className="link" onClick={onLeave}>{isDesktop ? '断开' : '退出'}</button>
    </header>
    <main>
      {page === 'nodes' ? <Nodes tick={live.tick} />
        : page === 'jobs' && id ? <JobDetail key={id} id={id} tick={live.tick} />
        : page === 'jobs' ? <Jobs tick={live.tick} />
        : page === 'settings' ? <Settings tick={live.tick} onLeave={onLeave} />
        : <Overview tick={live.tick} />}
    </main>
  </>
}

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>)
