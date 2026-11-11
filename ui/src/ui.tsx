import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { ApiError } from './api'

export const UNKNOWN = '未知'

// 任务与设置的来源（created_by / updated_by 的 kind）。
export const ORIGIN: Record<string, string> = { admin: '管理令牌', session: '浏览器控制台', controller: 'controller 自动创建', migration: '升级迁移' }
// 模型与 effort 的取值来源（job.model_policy.source）。
export const MODEL_SOURCE: Record<string, string> = {
  task_override: '本任务指定', workspace_default: '工作区默认值', project_default: '项目默认值', root_frozen: '沿用根任务受理时冻结的值',
  coordinator_override: '协调者在冻结的许可范围内指定', legacy_compat: '升级前的固定组合', deployment_profile: '部署配置的协调 profile',
}

// 主题是本机偏好，不进 controller 的设置；空值跟随系统。
export function applyTheme(v: string | null) {
  if (v === 'light' || v === 'dark') document.documentElement.dataset.theme = v
  else delete document.documentElement.dataset.theme
}

export function show(v: unknown): string {
  if (v === null || v === undefined || v === '') return UNKNOWN
  if (typeof v === 'boolean') return v ? '是' : '否'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

export function short(id?: string | null) { return id ? id.slice(0, 8) : UNKNOWN }

export function when(iso?: string | null) {
  if (!iso) return UNKNOWN
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  const d = Math.round((Date.now() - t) / 1000)
  const s = Math.abs(d)
  const span = s < 60 ? s + ' 秒' : s < 3600 ? Math.round(s / 60) + ' 分钟' : s < 86400 ? Math.round(s / 3600) + ' 小时' : Math.round(s / 86400) + ' 天'
  const rel = span + (d < 0 ? '后' : '前')
  return rel + '（' + new Date(t).toLocaleString('zh-CN', { hour12: false }) + '）'
}

const LABEL: Record<string, string> = {
  queued: '排队', waiting_capability: '等待能力', running: '执行中', recovering: '恢复中', waiting_reconcile: '待对账',
  cancel_requested: '已请求取消', cancelled: '已取消', completed: '已完成待验收', failed: '失败',
  assigned: '已分配', leased: '已领取', succeeded: '成功', lost: '失联', superseded: '已被取代',
  unprotected: '未启用自动恢复', preparing: '准备中（未受保护）', protected: '已受保护',
  unverified: '未验收', passed: '验收通过', rework: '需返工', blocked: '受阻',
  none: '无', integration_pending: '待回写', integrated: '已回写', conflict: '回写冲突',
  uploading: '上传中（不可恢复）', durable: '已确认可恢复', verified: '已通过业务验证',
  online: '在线', suspect: '疑似失联', unreachable: '不可达',
  ok: '正常', expired: '已过期', degraded: '近期失败（降级）', unproven: '当前版本未实测',
}
const TONE: Record<string, string> = {
  running: 'info', leased: 'info', assigned: 'info', recovering: 'warn', waiting_capability: 'warn', waiting_reconcile: 'warn',
  cancel_requested: 'warn', preparing: 'warn', suspect: 'warn', uploading: 'warn', integration_pending: 'warn', rework: 'warn',
  failed: 'bad', lost: 'bad', unreachable: 'bad', conflict: 'bad', blocked: 'bad', expired: 'bad', degraded: 'bad', unproven: 'warn',
  succeeded: 'good', completed: 'good', passed: 'good', durable: 'good', verified: 'good', protected: 'good', online: 'good', integrated: 'good', ok: 'good',
}

export function Badge({ v }: { v?: string | null }) {
  if (!v) return <span className="badge muted">{UNKNOWN}</span>
  return <span className={'badge ' + (TONE[v] ?? 'muted')} title={v}>{LABEL[v] ?? v}</span>
}

export function errText(e: unknown) {
  if (e instanceof ApiError) return `${e.code}：${e.message}`
  return e instanceof Error ? e.message : String(e)
}

// 取数：tick 变化（SSE 事件）时重新拉取快照。请求重叠时只采用最后一次发起的结果。
export function useData<T>(load: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const gen = useRef(0)
  const reload = useCallback(() => {
    const g = ++gen.current
    return load().then(d => { if (g === gen.current) { setData(d); setError(null) } }, e => { if (g === gen.current) setError(errText(e)) })
  }, deps) // eslint-disable-line
  // 依赖变化或卸载时作废在途请求。
  useEffect(() => { void reload(); return () => { gen.current++ } }, [reload])
  return { data, error, reload }
}

export function Section({ title, children, extra }: { title: string; children: ReactNode; extra?: ReactNode }) {
  return <section className="card"><header><h2>{title}</h2>{extra}</header>{children}</section>
}

// 变更按钮：执行期间禁用，防止重复提交；服务端幂等键兜底。
// done 可把成功响应里的部分结果（如未迁移的 attempt）显示为提醒，返回 null 表示完全成功。
export function Action({ label, run, danger, disabled, confirm: ask, done }: { label: string; run: () => Promise<unknown>; danger?: boolean; disabled?: boolean; confirm?: string; done?: (r: any) => string | null }) {
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<[string, string] | null>(null)
  return <span className="action">
    <button className={danger ? 'danger' : ''} disabled={busy || disabled} onClick={async () => {
      if (ask && !window.confirm(ask)) return
      setBusy(true); setMsg(null)
      // 先执行 run 再格式化结果：不能写成 done?.(await run())，done 缺省时可选调用会连参数一起跳过。
      try { const r = await run(); const note = done ? done(r) : null; setMsg(note ? [note, 'warn-text'] : ['已提交', 'ok']) } catch (e) { setMsg([errText(e), 'err']) } finally { setBusy(false) }
    }}>{busy ? '提交中…' : label}</button>
    {msg && <small className={msg[1]}>{msg[0]}</small>}
  </span>
}

export function Json({ v }: { v: unknown }) {
  return <pre className="json">{v === undefined ? UNKNOWN : JSON.stringify(v, null, 2)}</pre>
}
