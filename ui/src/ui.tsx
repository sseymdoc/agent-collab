import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { ApiError } from './api'
import { can } from './session'

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

// 模型与 effort 的字段格式（docs/protocol.md §12.3）：手工输入时先在页面校验，controller 仍会再校验。
export const MODEL_RE = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,95}$/
export const EFFORT_RE = /^[a-z][a-z0-9_-]{0,31}$/

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
  requested: '已请求', received: '节点已收到', timed_out: '超时', stale: '沿用旧目录', legacy: '旧版本固定组合', unknown: '未知',
}
const TONE: Record<string, string> = {
  running: 'info', leased: 'info', assigned: 'info', recovering: 'warn', waiting_capability: 'warn', waiting_reconcile: 'warn',
  cancel_requested: 'warn', preparing: 'warn', suspect: 'warn', uploading: 'warn', integration_pending: 'warn', rework: 'warn',
  requested: 'info', received: 'info', timed_out: 'bad', stale: 'warn',
  failed: 'bad', lost: 'bad', unreachable: 'bad', conflict: 'bad', blocked: 'bad', expired: 'bad', degraded: 'bad', unproven: 'warn',
  succeeded: 'good', completed: 'good', passed: 'good', durable: 'good', verified: 'good', protected: 'good', online: 'good', integrated: 'good', ok: 'good',
}

export function Badge({ v }: { v?: string | null }) {
  if (!v) return <span className="badge muted">{UNKNOWN}</span>
  // 接口字段是对象时（例如 acceptance）按文本显示，不让整页因渲染对象而卸载。
  if (typeof v !== 'string') return <span className="badge muted">{show(v)}</span>
  return <span className={'badge ' + (TONE[v] ?? 'muted')} title={v}>{LABEL[v] ?? v}</span>
}

export const SCOPE: Record<string, string> = { read: '读取', tasks: '任务', settings: '设置', devices: '设备', maintain: '维护' }

// 原生桥与本机助手的错误码（docs/desktop.md 错误码表）：说明原因与下一步。
export const BRIDGE_ERR: Record<string, string> = {
  invalid: '参数不合要求，或 controller 拒绝了请求', unauthorized: '凭据已失效或被撤销，请重新配对', forbidden: '权限不足：这份凭据没有执行该操作的权限',
  stale_connection: '连接已切换，这次请求已作废', controller_mismatch: '应答不是来自这条连接配对的 controller', secure_storage_unavailable: '系统凭据存储读写失败',
  network: '无法连接 controller（不可达、TLS 校验失败或服务端错误）', not_found: '对象不存在', io: '本机文件读写失败',
  not_bound: '这个项目在这台机器上还没有选定目录，请先在项目页“选择文件夹”',
  busy: '已有一项项目操作在进行（扫描、上传、下载、应用同一时间只运行一项），请等它结束或取消后再试',
  cancelled: '操作已取消', helper_unavailable: '本机助手不可用：这个安装包不含所需的运行时',
  not_a_directory: '绑定的路径已不是目录（可能被移动或删除），请重新选择文件夹',
  unstable: '目录在扫描期间一直在变化，三次重扫仍不稳定。请等正在写入的程序（编译、同步工具等）停下后再扫描，或把变化的目录加入排除规则',
  source_changed: '扫描之后有文件被改过，这份快照没有登记为输入。已上传的内容保留，重新扫描后再上传只会传缺的部分',
  path_conflict: '目录里有仅大小写不同的两个路径，无法放进同一份输入。请改名或排除其中之一后重新扫描',
  unicode_conflict: '目录里有 Unicode 规范化后相同的两个名称，无法放进同一份输入。请改名其中之一后重新扫描',
  unfinished_application: '这个目录上还有一次未完成的应用，请先在页面顶部的提示里处理它',
  insufficient_space: '磁盘空间不足，没有写入', blob_mismatch: '取到的内容与哈希不符，已丢弃，请重试',
  target_not_empty: '所选文件夹不是空的：整包下载需要空文件夹或新建的文件夹', no_application: '本机没有这次应用的记录', no_snapshot: '扫描结果已失效，请重新扫描',
}

const API_ERR: Record<string, string> = {
  invalid: '请求不合要求', not_found: '对象不存在', conflict: '与 controller 当前状态冲突，没有执行', fenced: '执行权已转移（旧 epoch、旧令牌或数据已恢复），没有执行',
  budget_exhausted: '预算已耗尽', too_large: '内容超过上限', missing_blobs: '还有内容没有上传', project_archived: '项目已归档，不接受新的输入、草拟与任务',
  node_exists: '这个 node_id 已登记', node_retired: '这台设备已退役', retire_blocked: '还有未处理的事项，不能退役',
}

export function errText(e: unknown) {
  // 403：这份凭据没有该操作的权限；连接仍然有效，其余已授权的操作不受影响。
  if (e instanceof ApiError && e.status === 403) return '权限不足' + (e.details?.required_scope ? `（需要${SCOPE[e.details.required_scope] ?? e.details.required_scope}权限 ${e.details.required_scope}）` : '') + `：${e.message}`
  if (e instanceof ApiError && e.code === 'version_mismatch') return '记录在你读取之后已被修改（版本冲突），没有执行。页面会刷新为最新状态，请确认后重试。'
  if (e instanceof ApiError && e.details?.reason === 'coordinator_lease') return '这个根工作包的协调权由其他协调者持有：先在“协调权”里取得或接管，再执行该操作。'
  if (e instanceof ApiError) return `${API_ERR[e.code] ? API_ERR[e.code] + '（' + e.code + '）' : e.code}：${e.message}`
  // 原生桥返回 {code, message}，不是 Error。
  const b = e as any
  if (b && typeof b.code === 'string' && !(e instanceof Error)) return (BRIDGE_ERR[b.code] ?? b.code) + (b.message && b.message !== b.code ? `（${b.message}）` : '')
  return e instanceof Error ? e.message : String(e)
}

export function bytes(n?: number | null) {
  if (typeof n !== 'number') return UNKNOWN
  return n < 1024 ? n + ' 字节' : n < 1048576 ? (n / 1024).toFixed(1) + ' KiB' : n < 1073741824 ? (n / 1048576).toFixed(1) + ' MiB' : (n / 1073741824).toFixed(2) + ' GiB'
}

// 长列表只渲染前 n 项，并标明总数。
export function Capped<T>({ items, n = 200, render }: { items: T[]; n?: number; render: (x: T, i: number) => ReactNode }) {
  return <>{items.slice(0, n).map(render)}{items.length > n && <p className="muted">共 {items.length} 项，只显示前 {n} 项。</p>}</>
}

// 执行模型默认值未设置时的引导：新建 codex 任务会被 model_default_unset 拒绝。
export function ModelUnset() {
  return <p className="notice">还没有设置执行任务的默认模型：新建 Codex 任务时若不单独指定模型与 effort，controller 会拒绝受理。
    <a href="#/settings"> 去“设置 → Codex 执行策略”选择默认模型</a></p>
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
// scope：这项操作需要的权限；本连接没有时按钮禁用并说明（真正的检查在 controller）。
export function Action({ label, run, danger, disabled, confirm: ask, done, scope }: { label: string; run: () => Promise<unknown>; danger?: boolean; disabled?: boolean; confirm?: string; done?: (r: any) => string | null; scope?: string }) {
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<[string, string] | null>(null)
  const denied = !!scope && !can(scope)
  return <span className="action">
    <button className={danger ? 'danger' : ''} disabled={busy || disabled || denied} title={denied ? `需要${SCOPE[scope!] ?? scope}权限（${scope}）` : undefined} onClick={async () => {
      if (ask && !window.confirm(ask)) return
      setBusy(true); setMsg(null)
      // 先执行 run 再格式化结果：不能写成 done?.(await run())，done 缺省时可选调用会连参数一起跳过。
      try { const r = await run(); const note = done ? done(r) : null; setMsg(note ? [note, 'warn-text'] : ['已提交', 'ok']) } catch (e) { setMsg([errText(e), 'err']) } finally { setBusy(false) }
    }}>{busy ? '提交中…' : label}</button>
    {denied && <small className="muted">需要{SCOPE[scope!] ?? scope}权限</small>}
    {msg && <small className={msg[1]}>{msg[0]}</small>}
  </span>
}

export function Json({ v }: { v: unknown }) {
  return <pre className="json">{v === undefined ? UNKNOWN : JSON.stringify(v, null, 2)}</pre>
}
