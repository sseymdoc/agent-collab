// 只对接真实 controller API（docs/protocol.md §5、§8、§12）；没有 mock。传输见 transport.ts。
import { currentEpoch, request, setCsrf, type Rsp } from './transport'

export type J = Record<string, any>

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public details: J = {}) {
    super(message)
  }
}

// 协调令牌按根工作包保存在当前窗口；持有租约时，变更请求附带 X-Coordinator-Token。
// 内存为准，sessionStorage 只用于刷新页面后恢复（不可用时仍能用内存中的令牌）。
// 同时记下取得时的租约身份（恢复代次, 版本）：变了（被接管、续租、重建或数据恢复），本页不再算持有者。
// 版本单调递增，不会像时间戳那样在同一毫秒重复；数据恢复后版本可能重新从 1 开始，所以要连同代次一起比较。
export type CoordHeld = { token: string; version: number; generation: number }
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
  try { v ? sessionStorage.setItem(coordKey(wp), JSON.stringify({ ...v, controller: coordController })) : sessionStorage.removeItem(coordKey(wp)) } catch { /* 忽略 */ }
}
// 协调令牌只对签发它的 controller 有效，保存时记下 controller_id。连上（或刷新后重连）一个 controller 时，
// 内存和 sessionStorage 里不属于它的令牌全部丢弃，包括本页还没读入内存的；重连同一个 controller 时保留。
let coordController: string | null = null
export function bindCoordinators(controllerId: string) {
  if (controllerId === coordController) return
  coordController = controllerId
  coordMem.clear()
  pendingTake.clear()
  try {
    for (const k of Object.keys(sessionStorage)) {
      if (!k.startsWith('ac_coord_')) continue
      let owner: unknown
      try { owner = JSON.parse(sessionStorage.getItem(k) ?? 'null')?.controller } catch { /* 格式不符，按不匹配丢弃 */ }
      if (owner !== controllerId) sessionStorage.removeItem(k)
    }
  } catch { /* 不可用时只有内存 */ }
}
// 取得请求的响应丢失（网络错误）时保留请求体，由用户显式“恢复”时原样重放：controller 按 request_id 返回同一令牌，
// 而不是因租约已被（自己）占用而拒绝。新的取得/接管总是新请求，不会悄悄重放旧请求。收到 controller 的明确答复后丢弃。
const pendingTake = new Map<string, J>()
export const pendingTakeOf = (wp: string) => pendingTake.get(wp) ?? null

async function sendTake(wp: string, body: J) {
  pendingTake.set(wp, body)
  let d: any
  try { d = await call('POST', '/packages/' + wp + '/coordinator', body, coordHeader(wp)) } catch (e) {
    if (e instanceof ApiError && e.status) pendingTake.delete(wp)
    throw e
  }
  pendingTake.delete(wp)
  setCoord(wp, typeof d?.coordinator_token === 'string' ? { token: d.coordinator_token, version: d.version, generation: d.authority_generation } : null)
  return d
}

// 401（会话过期、凭据被撤销）或连接已失效（切换了连接、应答不是来自配对的 controller）：由 App 回到登录或连接页。
let unauthorized = (_reason: string) => {}
export function onUnauthorized(f: (reason: string) => void) { unauthorized = f }
export const lost = (reason: string) => unauthorized(reason)

// 发请求并把非 2xx 变成 ApiError；JSON 接口和原样取内容（blob）都经过这里，401 与连接失效的处理一致。
async function send(method: string, path: string, body?: string, extra: Record<string, string> = {}): Promise<Rsp> {
  // 桌面端：记下发出时的连接代次。旧连接的请求被外壳作废（stale_connection）只说明这次请求被丢弃，
  // 不代表当前连接有问题；只有当前代次的请求失效时才回到连接页，否则会把刚打开的新连接关掉。
  const mine = currentEpoch()
  let r: Rsp
  try { r = await request(method, path, body, extra) } catch (e: any) {
    // 原生桥的错误是 {code, message}；浏览器 fetch 失败是 TypeError。status 0 表示没有得到 controller 的答复。
    const code = typeof e?.code === 'string' ? e.code : 'network'
    if ((code === 'stale_connection' || code === 'controller_mismatch') && mine === currentEpoch()) unauthorized(code)
    throw new ApiError(0, code, typeof e?.message === 'string' ? e.message : '无法连接 controller')
  }
  if (r.status < 200 || r.status >= 300) {
    if (r.status === 401) { setCsrf(null); unauthorized('unauthorized') }
    let data: any = null
    try { data = JSON.parse(r.body) } catch { /* 不是 JSON 错误体 */ }
    const e = data?.error ?? {}
    throw new ApiError(r.status, e.code ?? 'http_' + r.status, e.message ?? r.body.slice(0, 200), e.details ?? {})
  }
  return r
}

async function call(method: string, path: string, body?: unknown, extra: Record<string, string> = {}): Promise<any> {
  const r = await send(method, path, body === undefined ? undefined : JSON.stringify(body), extra)
  if (r.status === 204 || !r.body) return null
  try { return JSON.parse(r.body) } catch { return { raw: r.body } }
}

export const uuid = () => crypto.randomUUID()
export const JOBS_PAGE = 200

export const api = {
  login: async (token: string) => { const d = await call('POST', '/session', { token }); setCsrf(d?.csrf_token ?? null); return d },
  logout: async () => { try { await call('DELETE', '/session') } finally { setCsrf(null) } },
  // 刷新页面后凭 HttpOnly Cookie 取回 CSRF 令牌；401 表示需要重新登录。
  resume: async () => { const d = await call('GET', '/session'); setCsrf(d?.csrf_token ?? null); return d },
  whoami: () => call('GET', '/client'),
  overview: () => call('GET', '/overview'),
  // 按创建时间倒序分页：before 为上一页最后一个 job_id。
  jobs: (status = '', before = '') => call('GET', '/jobs?limit=' + JOBS_PAGE + (status ? '&status=' + encodeURIComponent(status) : '') + (before ? '&before=' + encodeURIComponent(before) : '')),
  job: (id: string) => call('GET', '/jobs/' + id),
  jobEvents: (id: string, before: number) => call('GET', '/jobs/' + id + '/events?limit=200&before=' + before),
  explain: (id: string) => call('GET', '/jobs/' + id + '/explain'),
  // 全局事件按序号补读：after 之后的事件，按序号升序。
  events: (after: number, limit = 1000) => call('GET', '/events?after=' + after + '&limit=' + limit),
  nodes: () => call('GET', '/nodes'),
  doctor: () => call('GET', '/doctor'),
  diff: (cp: string, against: string) => call('GET', '/checkpoints/' + cp + '/diff?against=' + encodeURIComponent(against)),
  files: (cp: string) => call('GET', '/checkpoints/' + cp + '/files'),
  blobText: async (sha: string) => (await send('GET', '/blobs/' + sha)).body,
  // 模型设置：保存时带读到的 revision，别人先改过则得到 version_mismatch 与当前值。
  models: () => call('GET', '/settings/models'),
  // 只带要改的那一组；没给的那组 controller 保持原值。
  saveModels: (revision: number, codex: J | undefined, claude: J | undefined, requestId: string) =>
    call('PUT', '/settings/models', { request_id: requestId, expected_revision: revision, codex, claude }),
  templates: () => call('GET', '/templates'),
  baselines: () => call('GET', '/baselines'),
  draft: (body: J) => call('POST', '/contracts/draft', body),
  // 提交带上草拟时的设置版本：期间默认模型被改动则被拒绝，不会按没看过的解析结果受理。
  submit: (title: string, contract: J, revision: number) => call('POST', '/jobs', { request_id: uuid(), title, contract, expected_settings_revision: revision }),
  clients: () => call('GET', '/clients'),
  revokeClient: (id: string, v: number) => call('POST', '/clients/' + id + '/revoke', { request_id: uuid(), expected_version: v }),
  issueCode: (scopes: string[]) => call('POST', '/clients/codes', { request_id: uuid(), scopes, ttl_s: 600 }),
  // 变更：每次都带新的幂等键与当前记录版本，陈旧页面得到 version_mismatch。
  cancel: (id: string, v: number) => call('POST', '/jobs/' + id + '/cancel', { request_id: uuid(), expected_version: v }),
  retry: (id: string, wp: string, v: number, reason: string, exclude: string[]) =>
    call('POST', '/jobs/' + id + '/retry', { request_id: uuid(), expected_version: v, reason, exclude_node_ids: exclude }, coordHeader(wp)),
  publish: (id: string, wp: string, v: number, a: J, cp: string, base: number) =>
    call('POST', '/jobs/' + id + '/publish', { request_id: uuid(), expected_version: v, attempt_id: a.attempt_id, epoch: a.epoch, checkpoint_id: cp, expected_base_version: base }, coordHeader(wp)),
  assess: (id: string, wp: string, v: number, body: J) =>
    call('POST', '/jobs/' + id + '/assess', { request_id: uuid(), expected_version: v, assessment_id: uuid(), ...body }, coordHeader(wp)),
  coordinator: (wp: string) => call('GET', '/packages/' + wp + '/coordinator'),
  takeCoordinator: (wp: string, takeover: boolean, reason: string, holder: string) =>
    sendTake(wp, { request_id: uuid(), holder_kind: 'interactive', holder_ref: holder, ttl_s: 1800, takeover, reason }),
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
