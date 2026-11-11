import { useEffect, useState } from 'react'
import { lost } from './api'
import { currentEpoch, openStream } from './transport'

// 事件流只触发刷新；事实以 GET 快照为准。这里负责：按序号续读与去重、断线退避重连、
// 游标过期（snapshot_required）后整体刷新并从 controller 给出的最小序号继续、凭据失效时交回连接页。
// lastAt 是最近一次收到事件或成功连上的时间：断线期间页面显示它，不把旧数据当作实时。
export type Live = { state: 'connecting' | 'open' | 'retrying'; lastSeq: number | null; tick: number; lastAt: number | null }

export function useLive(enabled: boolean): Live {
  const [live, setLive] = useState<Live>({ state: 'connecting', lastSeq: null, tick: 0, lastAt: null })
  useEffect(() => {
    if (!enabled) return
    let cursor = 0, close = () => {}, timer: number | undefined, retry: number | undefined, healthy: number | undefined, delay = 1000, stopped = false
    // 250 毫秒内的多条事件合并成一次刷新：长日志或批量状态变化不会让页面逐条重绘。
    const bump = () => {
      window.clearTimeout(timer)
      timer = window.setTimeout(() => setLive(l => ({ ...l, tick: l.tick + 1, lastSeq: cursor || null, lastAt: Date.now() })), 250)
    }
    const reopen = (ms: number) => { close(); window.clearTimeout(healthy); window.clearTimeout(retry); retry = window.setTimeout(open, ms) }
    const open = () => {
      if (stopped) return
      const mine = currentEpoch()
      close = openStream(cursor, e => {
        if (stopped) return
        if (e.kind === 'open') {
          // 连上后稳定一段时间才把退避重置回 1 秒：连上就断的情况仍按指数退避，不会每秒重连一次。
          window.clearTimeout(healthy)
          healthy = window.setTimeout(() => { delay = 1000 }, 30000)
          // 重连成功后补一次刷新：断线期间的变化可能已被服务端裁剪，不能只靠续读。
          setLive(l => ({ ...l, state: 'open', lastAt: Date.now(), tick: l.state === 'retrying' ? l.tick + 1 : l.tick }))
        } else if (e.kind === 'event') {
          const n = Number(e.id)
          if (!n || n <= cursor) return // 重连后服务端可能重发已读过的事件
          cursor = n
          bump()
        } else if (e.kind === 'snapshot_required') {
          // controller 已裁剪到 min_seq 之后（或恢复过数据）：以快照为准，再从它给出的位置续读。
          let m = NaN
          try { m = Number(JSON.parse(e.data ?? '').min_seq) } catch { /* 数据异常时按断线重试 */ }
          bump()
          if (Number.isInteger(m) && m > 0) { cursor = m; reopen(0) } else reopen(5000)
        } else if (e.kind === 'unauthorized') {
          stopped = true
          lost('unauthorized')
        } else if ((e.data === 'controller_mismatch' || e.data === 'stale_connection') && mine === currentEpoch()) {
          // 桌面端：外壳已关闭这条连接，用同一代次重连只会一直得到 stale_connection，交回连接页。
          stopped = true
          lost(e.data)
        } else {
          setLive(l => ({ ...l, state: 'retrying' }))
          reopen(delay)
          delay = Math.min(delay * 2, 10000)
        }
      })
    }
    open()
    return () => { stopped = true; window.clearTimeout(timer); window.clearTimeout(retry); window.clearTimeout(healthy); close() }
  }, [enabled])
  return live
}
