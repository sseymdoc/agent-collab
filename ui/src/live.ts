import { useEffect, useRef, useState } from 'react'
import { streamUrl } from './api'

// SSE 只触发刷新；事实以 GET 快照为准。EventSource 断线自动重连并带 Last-Event-ID；
// 服务端游标过期时发 snapshot_required，这里清空游标、整体刷新并重连。
export type Live = { state: 'connecting' | 'open' | 'retrying'; lastSeq: number | null; tick: number; lastAt: number | null }

// SSE 没有通配订阅；与 controller 实际发出的事件类型一致（2026-11-09 按 controller.emit 调用核对）。
// 不订阅 attempt.renewed：它随每次心跳出现，不改变页面上的事实。
export const EVENT_TYPES = [
  'controller.started', 'job.created', 'job.updated', 'job.published', 'job.assessed', 'job.integration', 'job.evidence_gap',
  'attempt.assigned', 'attempt.leased', 'attempt.updated', 'attempt.started', 'attempt.progress', 'attempt.log',
  'attempt.side_effect', 'attempt.succeeded', 'attempt.failed', 'attempt.cancelled', 'attempt.lost', 'attempt.superseded',
  'checkpoint.uploading', 'checkpoint.durable', 'node.paired', 'node.registered', 'node.updated',
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
    const bump = () => {
      window.clearTimeout(timer)
      timer = window.setTimeout(() => setLive(l => ({ ...l, tick: l.tick + 1, lastSeq: seq.current, lastAt: Date.now() })), 250)
    }
    const onEvent = (e: MessageEvent) => { const n = Number(e.lastEventId); if (n) seq.current = n; bump() }
    const open = () => {
      es = new EventSource(streamUrl(seq.current), { withCredentials: true })
      es.onopen = () => setLive(l => ({ ...l, state: 'open' }))
      es.onerror = () => setLive(l => ({ ...l, state: 'retrying' }))
      es.onmessage = onEvent
      for (const t of EVENT_TYPES) es.addEventListener(t, onEvent as EventListener)
      es.addEventListener('snapshot_required', () => { seq.current = null; es?.close(); bump(); open() })
    }
    open()
    return () => { window.clearTimeout(timer); es?.close() }
  }, [enabled])
  return live
}
