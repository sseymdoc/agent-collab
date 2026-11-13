// 只对接真实 controller API（docs/protocol.md §5、§8、§12、§13）；没有 mock。传输见 transport.ts。
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
    // 与上面相同：只有当前连接代次的请求得到 401 才回到登录或连接页，旧流程迟到的 401 不关掉新连接。
    if (r.status === 401 && mine === currentEpoch()) { setCsrf(null); unauthorized('unauthorized') }
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
  jobs: (status = '', before = '', project = '') => call('GET', '/jobs?limit=' + JOBS_PAGE + (status ? '&status=' + encodeURIComponent(status) : '') + (before ? '&before=' + encodeURIComponent(before) : '')
    + (project ? '&project_id=' + encodeURIComponent(project) : '')),
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
  baselines: (project = '') => call('GET', '/baselines' + (project ? '?project_id=' + encodeURIComponent(project) : '')),
  draft: (body: J) => call('POST', '/contracts/draft', body),
  // 提交带上草拟时的设置版本（属于项目时再带项目配置版本）：期间默认值被改动则被拒绝，不会按没看过的解析结果受理。
  submit: (title: string, contract: J, revision: number, project?: { id: string; revision: number }) => call('POST', '/jobs', {
    request_id: uuid(), title, contract, expected_settings_revision: revision,
    ...(project ? { project_id: project.id, expected_project_revision: project.revision } : {}) }),
  // 全局事件从新到旧翻看：序号小于 before 的最近 limit 条（升序）；before=0 从最新一条开始。
  eventsBefore: (before: number, limit = 200) => call('GET', '/events?before=' + before + '&limit=' + limit),
  // 项目（§13.1）：配置改动带读到的 revision；登记基线与应用成果不改变 revision。
  projects: () => call('GET', '/projects'),
  project: (id: string) => call('GET', '/projects/' + id),
  createProject: (name: string) => call('POST', '/projects', { request_id: uuid(), name }),
  updateProject: (id: string, revision: number, patch: J) => call('PUT', '/projects/' + id, { request_id: uuid(), expected_revision: revision, ...patch }),
  applications: (id: string) => call('GET', '/projects/' + id + '/applications'),
  // 远端项目写回（§13.10）：controller 按任务记录填写参数，这里只指名任务与节点；结果在应用记录里。
  application: (project: string, id: string) => call('GET', '/projects/' + project + '/applications/' + id),
  remotePlan: (project: string, job: string, node: string) => call('POST', '/projects/' + project + '/plans', { request_id: uuid(), job_id: job, node_id: node }),
  remoteApply: (project: string, body: J) => call('POST', '/projects/' + project + '/applications', { request_id: uuid(), ...body }),
  remoteRecover: (project: string, id: string, action: string) =>
    call('POST', '/projects/' + project + '/applications/' + id + '/recover', { request_id: uuid(), action }),
  // 协调设置（§13.7），与模型设置是两条记录，各自带 revision。
  coordination: () => call('GET', '/settings/coordination'),
  saveCoordination: (revision: number, enabled: boolean, nodeIds: string[], requestId: string) =>
    call('PUT', '/settings/coordination', { request_id: requestId, expected_revision: revision, enabled, node_ids: nodeIds }),
  // 节点管理（§13.5、§13.6）。
  retiredNodes: () => call('GET', '/nodes?retired=1'),
  nodeCode: (nodeId: string, replace: boolean) => call('POST', '/nodes/codes', { request_id: uuid(), node_id: nodeId, ...(replace ? { replace: true } : {}) }),
  maintenance: (id: string, v: number, active: boolean, reason: string) =>
    call('POST', '/nodes/' + id + '/maintenance', { request_id: uuid(), expected_version: v, active, ...(reason ? { reason } : {}) }),
  retire: (id: string, v: number, retired: boolean, reason: string) =>
    call('POST', '/nodes/' + id + '/retire', { request_id: uuid(), expected_version: v, retired, ...(reason ? { reason } : {}) }),
  nodeOperation: (id: string, kind: string, params: J = {}) => call('POST', '/nodes/' + id + '/operations', { request_id: uuid(), kind, params }),
  operation: (op: string) => call('GET', '/operations/' + op),
  // 原始输出取回：节点在心跳里领取，结果经 GET /operations/{op_id} 读取。
  logs: (job: string, body: J) => call('POST', '/jobs/' + job + '/logs', { request_id: uuid(), ...body }),
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
  // 维护（§14，maintain 权限）：备份只写到 controller 预授权的备份位置，请求里没有服务器路径。
  backups: () => call('GET', '/maintenance/backups'),
  createBackup: (label: string) => call('POST', '/maintenance/backups', { request_id: uuid(), ...(label ? { label } : {}) }),
  verifyBackup: (id: string) => call('POST', '/maintenance/backups/' + encodeURIComponent(id) + '/verify', { request_id: uuid() }),
  restorePreview: (id: string) => call('GET', '/maintenance/restore-preview?backup_id=' + encodeURIComponent(id)),
  barrierPreview: () => call('GET', '/maintenance/barrier-preview'),
  maintenanceOperation: (op: string) => call('GET', '/maintenance/operations/' + op),
  backupPolicy: () => call('GET', '/settings/backups'),
  saveBackupPolicy: (revision: number, keep: number) => call('PUT', '/settings/backups', { request_id: uuid(), expected_revision: revision, keep }),
  // 明确弃置一个待发布的成果：只记录弃置，不删除任何东西；之后切换屏障不再因它阻断。
  dismiss: (id: string, v: number, reason: string) => call('POST', '/jobs/' + id + '/dismiss', { request_id: uuid(), expected_version: v, reason }),
}

// 管理操作是异步记录：轮询到结果确定（succeeded / failed / timed_out）。alive() 为 false 时（页面已离开）停止。
export const OP_DONE = ['succeeded', 'failed', 'timed_out']
export async function waitOperation(opId: string, on: (op: J) => void, alive: () => boolean): Promise<J | null> {
  for (let delay = 500; alive(); delay = Math.min(delay * 1.5, 3000)) {
    const op = await api.operation(opId)
    if (!alive()) return null
    on(op)
    if (OP_DONE.includes(op.status)) return op
    await new Promise(r => setTimeout(r, delay))
  }
  return null
}

function coordHeader(wp: string): Record<string, string> {
  const t = coordHeld(wp)
  return t ? { 'X-Coordinator-Token': t.token } : {}
}
