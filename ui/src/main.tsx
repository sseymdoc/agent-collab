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
import { Projects } from './pages/Projects'
import { ProjectDetail } from './pages/ProjectDetail'
import { Activity } from './pages/Activity'
import { Unfinished } from './pages/JobResult'
import { BuildMark } from './pages/Wizard'
import { Maintenance, useAutoUpdate } from './pages/Maintenance'
import { notifyPrefs, useNotifier } from './notify'
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
  // 自动更新的提示与事件通知共用“通知”设置里的总开关。
  useAutoUpdate((title, body) => { if (notifyPrefs().enabled) void invoke('notify', { title, body }).catch(() => {}) })
  // 每次建立连接 generation 加一，主界面按它整体重建：上一条连接留下的弹窗、表单和在途请求的结果随之丢弃。
  const [phase, setPhase] = useState<{ at: 'boot' | 'login' | 'ready'; generation: number; reason: string | null; error: string | null }>(
    { at: 'boot', generation: 0, reason: null, error: null })
  const enter = async (live?: () => boolean, epoch?: number) => {
    const who = await api.whoami()
    if (live && !live()) {
      // 等身份请求期间用户换了流程：不进入这条连接，也不让它在外壳里留着。
      if (epoch !== undefined) void invoke('connection_close', { epoch }).catch(() => {})
      return
    }
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
      // 只有 401 表示需要重新登录；403 是权限不足，会话仍然有效，按错误显示。
      if (e instanceof ApiError && e.status === 401) leave(null)
      else setPhase(p => ({ ...p, error: errText(e) }))
    }
  }
  useEffect(() => { onUnauthorized(leave); void boot() }, []) // eslint-disable-line
  if (phase.at === 'boot') return <main className="login">{phase.error
    ? <div className="card"><p className="err">无法连接 controller：{phase.error}</p>
      <div className="actions"><button onClick={() => void boot()}>重试</button><button className="link" onClick={() => leave(null)}>改用令牌登录</button></div></div>
    : <p className="muted">连接 controller…</p>}</main>
  if (phase.at === 'login') return isDesktop
    ? <Connect key={phase.reason ?? phase.error ?? ''} reason={phase.reason ?? phase.error} onOpen={async (opened, _c, live) => {
      setEpoch(opened.epoch)
      // 只在失败的仍是当前这条连接时离开：期间若已换了连接，旧身份请求的失败不能关掉新连接。
      await enter(live, opened.epoch).catch(e => {
        // 流程已被取代：无论身份请求成功与否，都按这条连接的代次关闭它（外壳拒绝过期的代次，不会关掉更新的连接）。
        if (!live()) return void invoke('connection_close', { epoch: opened.epoch }).catch(() => {})
        if (currentEpoch() === opened.epoch) leave(errText(e))
      })
    }} />
    : <Login onDone={() => void enter().catch(e => setPhase(p => ({ ...p, at: 'boot', error: errText(e) })))} />
  // 浏览器退出要等 DELETE /session 答复后再显示登录页：否则马上重新登录时，迟到的旧答复会清掉新会话的 Cookie 和 CSRF 令牌。
  return <Shell key={phase.generation} onLeave={() => isDesktop ? leave(null) : void api.logout().catch(() => {}).finally(() => leave(null))} />
}

const NAV = [['overview', '总览'], ['projects', '项目'], ['jobs', '任务'], ['activity', '活动'], ['nodes', '设备'], ['maintenance', '维护'], ['settings', '设置']]

function Shell({ onLeave }: { onLeave: () => void }) {
  const hash = useHash()
  // 通知去重表与“连接建立时的最大序号”挂在 Shell 上，随连接切换（generation）重建。
  const notify = useNotifier()
  const live = useLive(true, notify)
  const who = session()
  const [info, setInfo] = useState<any>(null)
  useEffect(() => { if (isDesktop) invoke('app_info').then(setInfo, () => {}) }, [])
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
  const conn = live.state === 'open' ? ['good', '实时'] : live.state === 'retrying' ? ['bad', '连接中断 · 数据截至 ' + at]
    : live.state === 'forbidden' ? ['warn', '无实时事件权限 · 手动刷新'] : ['warn', '连接中']
  return <>
    <header className="top">
      <strong>Agent Collab</strong>
      <nav>{NAV.map(([k, t]) => <a key={k} href={'#/' + k} className={page === k || (!page && k === 'overview') ? 'on' : ''}>{t}</a>)}</nav>
      <span className={'badge ' + conn[0]} title={'最后事件序号 ' + (live.lastSeq ?? '无')}>{conn[1]}</span>
      <small className="muted" title={'controller ' + who?.controller.controller_id}>{who?.identity.name ?? (who?.identity.kind === 'session' ? '浏览器会话' : '管理令牌')} · controller {short(who?.controller.controller_id)}</small>
      <button className="link" onClick={onLeave}>{isDesktop ? '断开' : '退出'}</button>
    </header>
    <main>
      <Unfinished />
      {page === 'nodes' ? <Nodes tick={live.tick} />
        : page === 'projects' && id ? <ProjectDetail key={id} id={id} tick={live.tick} />
        : page === 'projects' ? <Projects tick={live.tick} />
        : page === 'activity' ? <Activity tick={live.tick} />
        : page === 'jobs' && id ? <JobDetail key={id} id={id} tick={live.tick} />
        : page === 'jobs' ? <Jobs tick={live.tick} />
        : page === 'settings' ? <Settings tick={live.tick} onLeave={onLeave} />
        : page === 'maintenance' ? <Maintenance tick={live.tick} />
        : <Overview tick={live.tick} />}
      <footer><BuildMark info={info} /></footer>
    </main>
  </>
}

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>)
