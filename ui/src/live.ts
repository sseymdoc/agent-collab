import { useEffect, useRef, useState } from 'react'
import { streamUrl } from './api'

// SSE 只触发刷新；事实以 GET 快照为准。EventSource 断线自动重连并带 Last-Event-ID；
// 服务端游标过期时发 snapshot_required，这里整体刷新，再从它给出的 min_seq 续读。
export type Live = { state: 'connecting' | 'open' | 'retrying'; lastSeq: number | null; tick: number; lastAt: number | null }

// SSE 没有通配订阅；与 controller 实际发出的事件类型一致（2026-11-10 按 controller.emit 与 store.event 调用核对）。
// 不订阅 attempt.renewed：它随每次心跳出现，不改变页面上的事实。
export const EVENT_TYPES = [
  'controller.started', 'controller.backup', 'controller.restored', 'job.created', 'job.updated', 'job.published', 'job.assessed', 'job.integration', 'job.evidence_gap',
  'attempt.assigned', 'attempt.leased', 'attempt.updated', 'attempt.started', 'attempt.progress', 'attempt.log',
  'attempt.side_effect', 'attempt.succeeded', 'attempt.failed', 'attempt.cancelled', 'attempt.lost', 'attempt.superseded',
  'checkpoint.uploading', 'checkpoint.durable', 'node.paired', 'node.registered', 'node.updated', 'meta.updated',
  'coordinator.triggered', 'coordinator.takeover', 'coordinator.released', 'coordinator.escalated',
  'coordinator.decision_applied', 'coordinator.decision_rejected',
]

export function useLive(enabled: boolean): Live {
  const [live, setLive] = useState<Live>({ state: 'connecting', lastSeq: null, tick: 0, lastAt: null })
  const seq = useRef<number | null>(null)
  useEffect(() => {
    if (!enabled) return
    let es: EventSource | null = null
    let timer: number | undefined
    let retry: number | undefined
    const bump = () => {
      window.clearTimeout(timer)
      timer = window.setTimeout(() => setLive(l => ({ ...l, tick: l.tick + 1, lastSeq: seq.current, lastAt: Date.now() })), 250)
    }
    const onEvent = (e: MessageEvent) => { const n = Number(e.lastEventId); if (n) seq.current = n; bump() }
    const reopen = (ms: number) => { es?.close(); window.clearTimeout(retry); retry = window.setTimeout(open, ms) }
    const open = () => {
      es = new EventSource(streamUrl(seq.current), { withCredentials: true })
      es.onopen = () => setLive(l => ({ ...l, state: 'open' }))
      // 网络中断时浏览器自行重连；收到非 200 响应（会话过期 401、代理 502 等）时浏览器关闭连接不再重试，这里延时重开。
      es.onerror = () => { setLive(l => ({ ...l, state: 'retrying' })); if (es?.readyState === EventSource.CLOSED) reopen(5000) }
      es.onmessage = onEvent
      for (const t of EVENT_TYPES) es.addEventListener(t, onEvent as EventListener)
      // controller 要求 cursor ≥ min_seq，省略游标按 0 处理会再次收到本事件；页面以快照刷新，之后从 min_seq 续读。
      es.addEventListener('snapshot_required', e => {
        let m = NaN
        try { m = Number(JSON.parse((e as MessageEvent).data).min_seq) } catch { /* 数据异常时延时重试 */ }
        bump()
        if (Number.isInteger(m) && m > 0) { seq.current = m; reopen(0) } else reopen(5000)
      })
    }
    open()
    return () => { window.clearTimeout(timer); window.clearTimeout(retry); es?.close() }
  }, [enabled])
  return live
}
