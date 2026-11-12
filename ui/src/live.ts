import { useEffect, useRef, useState } from 'react'
import { api, lost } from './api'
import { currentEpoch, openStream } from './transport'

// 事件流只触发刷新；事实以 GET 快照为准。这里负责：按序号续读与去重、断线退避重连、
// 游标过期（snapshot_required）后先刷新快照、再补读仍保留的事件、最后从补读到的位置续流，凭据失效时交回连接页。
// lastAt 是最近一次收到事件或成功连上的时间：断线期间页面显示它，不把旧数据当作实时。
// forbidden：这份凭据没有读取事件的权限（403）。凭据仍然有效，所以不离开连接、也不反复重连；页面改为手动刷新。
export type Live = { state: 'connecting' | 'open' | 'retrying' | 'forbidden'; lastSeq: number | null; tick: number; lastAt: number | null }

// onEvent：每条去重后的新事件（已解析的 event 记录），供系统通知判断；不影响刷新逻辑。
export function useLive(enabled: boolean, onEvent?: (ev: any) => void): Live {
  const [live, setLive] = useState<Live>({ state: 'connecting', lastSeq: null, tick: 0, lastAt: null })
  const listener = useRef(onEvent)
  listener.current = onEvent
  useEffect(() => {
    if (!enabled) return
    let cursor = 0, close = async () => {}, timer: number | undefined, retry: number | undefined, healthy: number | undefined, delay = 1000, stopped = false
    let lastMin: number | null = null
    // 250 毫秒内的多条事件合并成一次刷新：长日志或批量状态变化不会让页面逐条重绘。
    const bump = () => {
      window.clearTimeout(timer)
      timer = window.setTimeout(() => setLive(l => ({ ...l, tick: l.tick + 1, lastSeq: cursor || null, lastAt: Date.now() })), 250)
    }
    // 等旧流关闭完成再开新流（见 openStream）。
    const reopen = (ms: number) => {
      const closed = close()
      window.clearTimeout(healthy); window.clearTimeout(retry)
      retry = window.setTimeout(() => void closed.then(open), ms)
    }
    const fail = () => {
      setLive(l => ({ ...l, state: 'retrying' }))
      reopen(delay)
      delay = Math.min(delay * 2, 10000)
    }
    // 从 after 之后补读到最新，返回读到的最后序号（没有事件时仍是 after）。
    const backfill = async (after: number) => {
      for (;;) {
        const list: any[] = (await api.events(after))?.events ?? []
        if (list.length) after = list[list.length - 1].seq
        if (list.length < 1000 || stopped) return after
      }
    }
    const open = () => {
      if (stopped) return
      const mine = currentEpoch()
      let handedOff = false   // 这条流已转去处理 snapshot_required，它随后的关闭不再触发重连
      close = openStream(cursor, e => {
        if (stopped || handedOff) return
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
          lastMin = null
          if (listener.current) try { listener.current(JSON.parse(e.data ?? 'null')) } catch { /* 数据异常不影响刷新 */ }
          bump()
        } else if (e.kind === 'snapshot_required') {
          // controller 已裁剪到 min_seq 之后（或恢复过数据）：先刷新快照，再从 min_seq-1 补读仍保留的事件，
          // 最后以补读到的最后序号续流。流按 seq > cursor 续读，直接用 min_seq 当游标会漏掉第一条。
          handedOff = true
          let m = NaN
          try { m = Number(JSON.parse(e.data ?? '').min_seq) } catch { /* 数据异常时按断线重试 */ }
          setLive(l => ({ ...l, tick: l.tick + 1, lastAt: Date.now() }))
          if (!Number.isInteger(m) || m <= 0) return fail()
          // 补读之后马上又要求同一个 min_seq：按退避重试，不原地打转。
          const again = m === lastMin
          lastMin = m
          backfill(m - 1).then(last => {
            if (stopped) return
            cursor = last
            if (last >= m) bump()   // 补读到的事件可能晚于刚才的快照，再刷新一次
            if (again) fail(); else reopen(0)
          }, () => { if (!stopped) fail() })
        } else if (e.kind === 'unauthorized') {
          stopped = true
          lost('unauthorized')
        } else if (e.kind === 'forbidden') {
          stopped = true
          setLive(l => ({ ...l, state: 'forbidden' }))
        } else if ((e.data === 'controller_mismatch' || e.data === 'stale_connection') && mine === currentEpoch()) {
          // 桌面端：外壳已关闭这条连接，用同一代次重连只会一直得到 stale_connection，交回连接页。
          stopped = true
          lost(e.data)
        } else fail()
      })
    }
    open()
    return () => { stopped = true; window.clearTimeout(timer); window.clearTimeout(retry); window.clearTimeout(healthy); void close() }
  }, [enabled])
  return live
}
