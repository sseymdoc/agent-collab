// 只对接真实 controller API（docs/protocol.md §5、§8）；没有 mock。
export type J = Record<string, any>

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public details: J = {}) {
    super(message)
  }
}

const P = '/api/v1'
let csrf: string | null = null
try { csrf = sessionStorage.getItem('ac_csrf') } catch { /* 隐私模式 */ }

function setCsrf(v: string | null) {
  csrf = v
  try { v ? sessionStorage.setItem('ac_csrf', v) : sessionStorage.removeItem('ac_csrf') } catch { /* 忽略 */ }
}

// 协调令牌按根工作包保存在当前标签页；持有租约时，变更请求附带 X-Coordinator-Token。
// 内存为准，sessionStorage 只用于刷新页面后恢复（隐私模式下不可用时仍能用内存中的令牌）。
// 同时记下取得时的租约版本：版本变了（被接管、续租或重建），本页不再算持有者。版本单调递增，不会像时间戳那样在同一毫秒重复。
export type CoordHeld = { token: string; version: number }
const coordMem = new Map<string, CoordHeld | null>()
const coordKey = (wp: string) => 'ac_coord_' + wp
export function coordHeld(wp?: string | null): CoordHeld | null {
  if (!wp) return null
  if (!coordMem.has(wp)) {
    let v: CoordHeld | null = null
    try { const s = JSON.parse(sessionStorage.getItem(coordKey(wp)) ?? 'null'); if (typeof s?.token === 'string') v = s } catch { /* 不可用或格式不符 */ }
    coordMem.set(wp, v)
  }
  return coordMem.get(wp) ?? null
}
function setCoord(wp: string, v: CoordHeld | null) {
  coordMem.set(wp, v)
  try { v ? sessionStorage.setItem(coordKey(wp), JSON.stringify(v)) : sessionStorage.removeItem(coordKey(wp)) } catch { /* 忽略 */ }
}
// 取得请求的响应丢失（网络错误）时保留请求体，由用户显式“恢复”时原样重放：controller 按 request_id 返回同一令牌，
// 而不是因租约已被（自己）占用而拒绝。新的取得/接管总是新请求，不会悄悄重放旧请求。收到 controller 的明确答复后丢弃。
const pendingTake = new Map<string, J>()
export const pendingTakeOf = (wp: string) => pendingTake.get(wp) ?? null

async function sendTake(wp: string, body: J) {
  pendingTake.set(wp, body)
  let d: any
  try { d = await call('POST', '/packages/' + wp + '/coordinator', body, coordHeader(wp)) } catch (e) {
    if (e instanceof ApiError) pendingTake.delete(wp)
    throw e
  }
  pendingTake.delete(wp)
  setCoord(wp, typeof d?.coordinator_token === 'string' ? { token: d.coordinator_token, version: d.version } : null)
  return d
}

// 任何请求得到 401 都表示会话已失效（如超过会话有效期），由 App 切回登录页。
let unauthorized = () => {}
export function onUnauthorized(f: () => void) { unauthorized = f }

async function call(method: string, path: string, body?: unknown, extra: Record<string, string> = {}): Promise<any> {
  const headers: Record<string, string> = { Accept: 'application/json', ...extra }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (method !== 'GET' && csrf) headers['X-CSRF-Token'] = csrf
  const r = await fetch(P + path, {
    method, headers, credentials: 'same-origin',
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (r.status === 204) return null
  const text = await r.text()
  let data: any = null
  try { data = text ? JSON.parse(text) : null } catch { data = { raw: text } }
  if (!r.ok) {
    if (r.status === 401) { setCsrf(null); unauthorized() }
    const e = data?.error ?? {}
    throw new ApiError(r.status, e.code ?? 'http_' + r.status, e.message ?? text.slice(0, 200), e.details ?? {})
  }
  return data
}

export const uuid = () => crypto.randomUUID()
export const JOBS_PAGE = 200

export const api = {
  login: async (token: string) => { const d = await call('POST', '/session', { token }); setCsrf(d?.csrf_token ?? null); return d },
  logout: async () => { try { await call('DELETE', '/session') } finally { setCsrf(null) } },
  // 刷新页面后凭 HttpOnly Cookie 取回 CSRF 令牌；401 表示需要重新登录。
  resume: async () => { const d = await call('GET', '/session'); setCsrf(d?.csrf_token ?? null); return d },
  overview: () => call('GET', '/overview'),
  // 按创建时间倒序分页：before 为上一页最后一个 job_id。
  jobs: (status = '', before = '') => call('GET', '/jobs?limit=' + JOBS_PAGE + (status ? '&status=' + encodeURIComponent(status) : '') + (before ? '&before=' + encodeURIComponent(before) : '')),
  job: (id: string) => call('GET', '/jobs/' + id),
  explain: (id: string) => call('GET', '/jobs/' + id + '/explain'),
  nodes: () => call('GET', '/nodes'),
  doctor: () => call('GET', '/doctor'),
  diff: (cp: string, against: string) => call('GET', '/checkpoints/' + cp + '/diff?against=' + encodeURIComponent(against)),
  // 变更：每次都带新的幂等键与当前记录版本，陈旧页面得到 version_mismatch。
  cancel: (id: string, v: number) => call('POST', '/jobs/' + id + '/cancel', { request_id: uuid(), expected_version: v }),
  retry: (id: string, wp: string, v: number, reason: string, exclude: string[]) =>
    call('POST', '/jobs/' + id + '/retry', { request_id: uuid(), expected_version: v, reason, exclude_node_ids: exclude }, coordHeader(wp)),
  publish: (id: string, wp: string, v: number, a: J, cp: string, base: number) =>
    call('POST', '/jobs/' + id + '/publish', { request_id: uuid(), expected_version: v, attempt_id: a.attempt_id, epoch: a.epoch, checkpoint_id: cp, expected_base_version: base }, coordHeader(wp)),
  assess: (id: string, wp: string, v: number, body: J) =>
    call('POST', '/jobs/' + id + '/assess', { request_id: uuid(), expected_version: v, assessment_id: uuid(), ...body }, coordHeader(wp)),
  coordinator: (wp: string) => call('GET', '/packages/' + wp + '/coordinator'),
  takeCoordinator: (wp: string, takeover: boolean, reason: string) =>
    sendTake(wp, { request_id: uuid(), holder_kind: 'interactive', holder_ref: 'web-console', ttl_s: 1800, takeover, reason }),
  resumeTake: (wp: string) => { const body = pendingTake.get(wp); return body ? sendTake(wp, body) : Promise.reject(new Error('没有待恢复的请求')) },
  // 成功后才清除令牌：失败或响应丢失时保留，便于重试释放。
  releaseCoordinator: async (wp: string) => {
    const d = await call('DELETE', '/packages/' + wp + '/coordinator', undefined, coordHeader(wp))
    setCoord(wp, null)
    return d
  },
  drain: (node: string, v: number, drain: boolean, migrate = false) =>
    call('POST', '/nodes/' + node + '/drain', { request_id: uuid(), expected_version: v, drain, ...(drain && migrate ? { migrate: true } : {}) }),
  dispatch: (paused: boolean, v: number) => call('POST', '/dispatch', { request_id: uuid(), paused, expected_version: v }),
}

function coordHeader(wp: string): Record<string, string> {
  const t = coordHeld(wp)
  return t ? { 'X-Coordinator-Token': t.token } : {}
}

export const blobUrl = (sha: string) => P + '/blobs/' + sha
export const streamUrl = (cursor: number | null) => P + '/events/stream' + (cursor != null ? '?cursor=' + cursor : '')
