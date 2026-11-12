// 两种传输，同一套页面：浏览器经 Cookie + CSRF 直接访问 controller；桌面端经原生桥（docs/desktop.md），
// 页面拿不到客户端凭据。事件流在两种传输下都只负责送达，重连与续读在 live.ts。
export type Rsp = { status: number; body: string }
// unauthorized：凭据失效或被撤销（401）；forbidden：凭据有效但没有读取事件的权限（403），两者处理不同。
export type StreamEvent = { kind: 'open' | 'event' | 'snapshot_required' | 'unauthorized' | 'forbidden' | 'closed'; id?: number | null; type?: string | null; data?: string | null }
export type BridgeError = { code: string; message: string }

const P = '/api/v1'
const tauri = (window as any).__TAURI__
export const isDesktop = !!tauri

// 桌面端：当前连接的代次。每个请求都带它；切换连接后旧代次的请求被外壳拒绝（stale_connection）。
let epoch = 0
export function setEpoch(v: number) { epoch = v }
export const currentEpoch = () => epoch

export function invoke<T = any>(command: string, args: Record<string, unknown> = {}): Promise<T> {
  return tauri.core.invoke(command, args)
}

// 带当前连接代次的原生桥命令（项目目录、输入与成果）；切换连接后旧页面发出的被外壳拒绝。
export const bridge = <T = any>(command: string, args: Record<string, unknown> = {}) => invoke<T>(command, { epoch, ...args })

// 项目操作的进度（事件 ac-project）。只在桌面端有；只接收订阅时这一代连接的事件。
// phase 是封闭集合（docs/desktop.md“项目操作的进度”）：scan、rescan、stage、upload、register、download、write、report、cancel_requested。
export type ProjectProgress = { op: 'scan' | 'upload' | 'download' | 'apply' | 'recover'; phase: string; done?: number; total?: number; files?: number; bytes?: number }
export function onProject(f: (p: ProjectProgress) => void): () => void {
  if (!isDesktop) return () => {}
  const mine = epoch
  let stopped = false
  const unlisten: Promise<() => void> = tauri.event.listen('ac-project', (e: any) => { if (!stopped && e.payload?.epoch === mine) f(e.payload) })
  return () => { stopped = true; void unlisten.then(u => u()) }
}

// 浏览器：CSRF 令牌只在内存和本标签页的 sessionStorage 里。
let csrf: string | null = null
try { csrf = sessionStorage.getItem('ac_csrf') } catch { /* 隐私模式 */ }
export function setCsrf(v: string | null) {
  csrf = v
  try { v ? sessionStorage.setItem('ac_csrf', v) : sessionStorage.removeItem('ac_csrf') } catch { /* 忽略 */ }
}

export async function request(method: string, path: string, body?: string, headers: Record<string, string> = {}): Promise<Rsp> {
  if (isDesktop) return invoke<Rsp>('api_request', { epoch, method, path, body: body ?? null, headers })
  const h: Record<string, string> = { Accept: 'application/json', ...headers }
  if (body !== undefined) h['Content-Type'] = 'application/json'
  if (method !== 'GET' && csrf) h['X-CSRF-Token'] = csrf
  const r = await fetch(P + path, { method, headers: h, credentials: 'same-origin', body })
  return { status: r.status, body: await r.text() }
}

// SSE 帧：id / event / data 三种行，空行结束一帧，冒号开头是心跳注释。
function frames(onFrame: (f: { id: number | null; event: string | null; data: string | null }) => void) {
  let buffer = ''
  return (chunk: string) => {
    buffer += chunk
    for (let i = buffer.indexOf('\n\n'); i >= 0; i = buffer.indexOf('\n\n')) {
      const f = { id: null as number | null, event: null as string | null, data: null as string | null }
      for (const line of buffer.slice(0, i).split('\n')) {
        if (line.startsWith('id: ')) f.id = Number(line.slice(4))
        else if (line.startsWith('event: ')) f.event = line.slice(7)
        else if (line.startsWith('data: ')) f.data = line.slice(6)
      }
      buffer = buffer.slice(i + 2)
      if (f.event) onFrame(f)
    }
  }
}

// 返回的关闭函数在关闭完成后 resolve：桌面端重连要等旧流的 stream_close 执行完再 stream_open，
// 否则迟到的 stream_close 会把新流停掉，而且不会再有 closed 事件。
export function openStream(cursor: number, on: (e: StreamEvent) => void): () => Promise<void> {
  if (isDesktop) {
    const mine = epoch
    let stopped = false
    const unlisten: Promise<() => void> = tauri.event.listen('ac-sse', (e: any) => {
      if (!stopped && e.payload?.epoch === mine) on(e.payload)
    })
    unlisten.then(() => { if (!stopped) invoke('stream_open', { epoch: mine, cursor }).catch((x: BridgeError) => on({ kind: 'closed', data: x?.code ?? 'error' })) })
    return () => { stopped = true; void unlisten.then(f => f()); return invoke('stream_close', { epoch: mine }).then(() => {}, () => {}) }
  }
  // 浏览器用 fetch 读流而不是 EventSource：能区分 401，不需要逐个登记事件类型，重连策略与桌面端一致。
  const abort = new AbortController()
  void (async () => {
    try {
      const r = await fetch(P + '/events/stream?cursor=' + cursor, { credentials: 'same-origin', signal: abort.signal, headers: { Accept: 'text/event-stream' } })
      if (r.status === 401) return on({ kind: 'unauthorized' })
      if (r.status === 403) return on({ kind: 'forbidden' })
      if (!r.ok || !r.body) return on({ kind: 'closed', data: 'http_' + r.status })
      on({ kind: 'open' })
      const feed = frames(f => {
        if (f.event === 'unauthorized') on({ kind: 'unauthorized' })
        else if (f.event === 'forbidden') on({ kind: 'forbidden' })
        else if (f.event === 'snapshot_required') on({ kind: 'snapshot_required', data: f.data })
        else on({ kind: 'event', id: f.id, type: f.event, data: f.data })
      })
      const reader = r.body.pipeThrough(new TextDecoderStream()).getReader()
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        feed(value)
      }
      on({ kind: 'closed', data: 'ended' })
    } catch {
      if (!abort.signal.aborted) on({ kind: 'closed', data: 'network' })
    }
  })()
  return async () => abort.abort()
}

// 保存工件：桌面端由系统“另存为”对话框选位置，外壳校验哈希后落盘；浏览器走普通下载。
export async function saveBlob(sha: string, name: string): Promise<string | null> {
  if (isDesktop) {
    const r = await invoke<{ path?: string; cancelled?: boolean }>('blob_save', { epoch, sha256: sha, name })
    return r.cancelled ? null : r.path ?? null
  }
  const r = await fetch(P + '/blobs/' + sha, { credentials: 'same-origin' })
  if (!r.ok) throw new Error('下载失败（HTTP ' + r.status + '）')
  const url = URL.createObjectURL(await r.blob())
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  // 有的浏览器在 click 之后才异步开始下载，立即释放会让下载失败。
  window.setTimeout(() => URL.revokeObjectURL(url), 10000)
  return name
}
