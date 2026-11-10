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
const coordKey = (wp: string) => 'ac_coord_' + wp
export function coordToken(wp?: string | null): string | null {
  if (!wp) return null
  try { return sessionStorage.getItem(coordKey(wp)) } catch { return null }
}
function setCoordToken(wp: string, v: string | null) {
  try { v ? sessionStorage.setItem(coordKey(wp), v) : sessionStorage.removeItem(coordKey(wp)) } catch { /* 忽略 */ }
}

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
    const e = data?.error ?? {}
    throw new ApiError(r.status, e.code ?? 'http_' + r.status, e.message ?? text.slice(0, 200), e.details ?? {})
  }
  return data
}

export const uuid = () => crypto.randomUUID()

export const api = {
  login: async (token: string) => { const d = await call('POST', '/session', { token }); setCsrf(d?.csrf_token ?? null); return d },
  logout: async () => { try { await call('DELETE', '/session') } finally { setCsrf(null) } },
  // 刷新页面后凭 HttpOnly Cookie 取回 CSRF 令牌；401 表示需要重新登录。
  resume: async () => { const d = await call('GET', '/session'); setCsrf(d?.csrf_token ?? null); return d },
  overview: () => call('GET', '/overview'),
  jobs: (status = '') => call('GET', '/jobs?limit=200' + (status ? '&status=' + encodeURIComponent(status) : '')),
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
  takeCoordinator: async (wp: string, takeover: boolean, reason: string) => {
    const d = await call('POST', '/packages/' + wp + '/coordinator', {
      request_id: uuid(), holder_kind: 'interactive', holder_ref: 'web-console', ttl_s: 1800, takeover, reason,
    }, coordHeader(wp))
    setCoordToken(wp, d?.coordinator_token ?? null)
    return d
  },
  releaseCoordinator: async (wp: string) => {
    try { return await call('DELETE', '/packages/' + wp + '/coordinator', undefined, coordHeader(wp)) } finally { setCoordToken(wp, null) }
  },
  drain: (node: string, v: number, drain: boolean, migrate = false) =>
    call('POST', '/nodes/' + node + '/drain', { request_id: uuid(), expected_version: v, drain, ...(drain && migrate ? { migrate: true } : {}) }),
  dispatch: (paused: boolean, v: number) => call('POST', '/dispatch', { request_id: uuid(), paused, expected_version: v }),
}

function coordHeader(wp: string): Record<string, string> {
  const t = coordToken(wp)
  return t ? { 'X-Coordinator-Token': t } : {}
}

export const blobUrl = (sha: string) => P + '/blobs/' + sha
export const streamUrl = (cursor: number | null) => P + '/events/stream' + (cursor != null ? '?cursor=' + cursor : '')
