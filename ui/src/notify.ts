// 系统通知（仅桌面端）：根据事件流里新到的事件决定是否通知，设置保存在本机 localStorage。
// 规则：同一任务同一类别只通知一次；两条通知至少间隔 30 秒，间隔内到达的合并成一条；
// 连接建立时已有的事件（序号不大于当时的最大序号）不通知；窗口在前台且正在看该任务时不通知。
import { useCallback, useEffect, useRef } from 'react'
import { api } from './api'
import { invoke, isDesktop } from './transport'
import { short } from './ui'

export type Kind = 'problem' | 'acceptance' | 'published'
export type NotifyPrefs = { enabled: boolean } & Record<Kind, boolean>
export const KIND_LABEL: Record<Kind, string> = { problem: '需要处理的问题（任务失败、待对账、等待能力）', acceptance: '等待验收', published: '成果已发布' }
const KEY = 'ac_notify'
const GAP_MS = 30000

export function notifyPrefs(): NotifyPrefs {
  const d: NotifyPrefs = { enabled: true, problem: true, acceptance: true, published: true }
  try { return { ...d, ...JSON.parse(localStorage.getItem(KEY) ?? '{}') } } catch { return d }
}
export function saveNotifyPrefs(v: NotifyPrefs) { try { localStorage.setItem(KEY, JSON.stringify(v)) } catch { /* 存储不可用时只在本次生效 */ } }

const PROBLEM: Record<string, string> = { failed: '失败', waiting_reconcile: '待对账，需要人工处理', waiting_capability: '在等待能力（没有合格设备）' }

// 事件 → [任务, 类别, 说明]；不需要通知时为 null。job.updated 的负载是完整的 job 记录。
export function classify(ev: any): [string, Kind, string] | null {
  if (!ev?.job_id) return null
  const p = ev.payload ?? {}
  if (ev.type === 'job.updated') {
    if (PROBLEM[p.status]) return [ev.job_id, 'problem', PROBLEM[p.status]]
    if (p.status === 'completed' && p.contract?.publish?.requires_acceptance && p.acceptance?.status !== 'passed') return [ev.job_id, 'acceptance', '已完成，等待验收']
  }
  if (ev.type === 'job.published') return [ev.job_id, 'published', '成果已发布' + (p.delivery_version ? '（交付版本 ' + p.delivery_version + '）' : '')]
  return null
}

const MERGED: Record<Kind, string> = { problem: '需要处理', acceptance: '等待验收', published: '成果已发布' }

// 状态挂在 Shell 上：切换连接时 Shell 按 generation 重建，去重表与基准序号随之重置。
export function useNotifier(): (ev: any) => void {
  const st = useRef({ base: null as number | null, maxSeen: 0, seen: new Set<string>(), titles: new Map<string, string>(), queue: [] as [string, Kind, string][], last: 0, timer: 0 })
  useEffect(() => {
    if (!isDesktop) return
    const s = st.current
    // 连接建立时已有的最大序号：之后流从头补读到的旧事件都不大于它。取不到时用已见过的最大序号。
    api.eventsBefore(0, 1).then(d => { s.base = Math.max(d?.events?.at(-1)?.seq ?? 0, s.maxSeen) }, () => { s.base = s.maxSeen })
    return () => window.clearTimeout(s.timer)
  }, [])
  const flush = useCallback(() => {
    const s = st.current
    s.timer = 0
    if (!s.queue.length) return
    const wait = s.last + GAP_MS - Date.now()
    if (wait > 0) { s.timer = window.setTimeout(flush, wait); return }
    const q = s.queue.splice(0)
    s.last = Date.now()
    const name = (job: string) => '「' + (s.titles.get(job) || short(job)) + '」'
    const [title, body] = q.length === 1 ? ['任务' + name(q[0][0]), q[0][2]]
      : ['Agent Collab', (Object.keys(MERGED) as Kind[]).map(k => [k, q.filter(x => x[1] === k).length] as const).filter(([, n]) => n)
        .map(([k, n]) => n + ' 个任务' + MERGED[k]).join('，')]
    void invoke('notify', { title, body }).catch(() => {})
  }, [])
  return useCallback((ev: any) => {
    if (!isDesktop || typeof ev?.seq !== 'number') return
    const s = st.current
    s.maxSeen = Math.max(s.maxSeen, ev.seq)
    if (ev.type === 'job.updated' && ev.payload?.title) s.titles.set(ev.job_id, ev.payload.title)
    if (s.base === null || ev.seq <= s.base) return
    const hit = classify(ev)
    if (!hit) return
    const prefs = notifyPrefs()
    if (!prefs.enabled || !prefs[hit[1]]) return
    const key = hit[0] + ':' + hit[1]
    if (s.seen.has(key)) return
    s.seen.add(key)
    // 正在前台看这个任务：页面上已经能看到，不再弹通知。
    if (document.visibilityState === 'visible' && document.hasFocus() && location.hash === '#/jobs/' + hit[0]) return
    s.queue.push(hit)
    if (!s.timer) flush()
  }, [flush])
}
