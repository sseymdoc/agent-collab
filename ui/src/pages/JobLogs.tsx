import { useEffect, useRef, useState } from 'react'
import { api, waitOperation } from '../api'
import { Badge, Section, bytes, errText, short } from '../ui'

// 原始输出：harness 与检查进程的完整标准输出/标准错误，只保存在执行节点的 attempt 目录里，按需经节点管理操作取回。
// 每次取回是一项异步操作：POST /jobs/{id}/logs → 轮询 GET /operations/{op_id}。内容按纯文本显示。
const CHUNK = 65536
const KEEP = 2 * 1048576   // 页面里最多保留的文本，超过时丢弃最前面的部分
const STREAM = (s: string) => s === 'harness.stdout' ? 'harness 标准输出' : s === 'harness.stderr' ? 'harness 标准错误'
  : s.replace(/^check\.(\d+)\.(stdout|stderr)$/, '检查 $1 的$2').replace(/^step\.(\d+)\.(stdout|stderr)$/, '步骤 $1 的$2').replace(/stdout$/, '标准输出').replace(/stderr$/, '标准错误')

export function JobLogs({ job, attempts }: { job: any; attempts: any[] }) {
  const ran = attempts.filter(a => a.node_id)
  const [attempt, setAttempt] = useState(job.current_attempt_id && ran.some(a => a.attempt_id === job.current_attempt_id) ? job.current_attempt_id : ran[ran.length - 1]?.attempt_id ?? '')
  const [op, setOp] = useState<any>(null)
  const [index, setIndex] = useState<any>(null)
  const [stream, setStream] = useState('')
  const [text, setText] = useState({ data: '', dropped: 0 })
  const [pos, setPos] = useState<any>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])
  const fetchOp = async (body: any) => {
    setBusy(true); setErr(null); setOp(null)
    try {
      const r = await api.logs(job.job_id, { attempt_id: attempt, ...body })
      const done = await waitOperation(r.op_id, setOp, () => alive.current)
      if (done?.status === 'succeeded') return done.result
      if (done?.status === 'failed') setErr('节点没能取回：' + (typeof done.error === 'string' ? done.error : JSON.stringify(done.error)))
      if (done?.status === 'timed_out') setErr('超时：执行节点不在线，或它的版本不支持取回原始输出。')
      return null
    } catch (e) { setErr(errText(e)); return null } finally { if (alive.current) setBusy(false) }
  }
  const loadIndex = async () => { setIndex(null); setStream(''); setPos(null); setText({ data: '', dropped: 0 }); const r = await fetchOp({ stream: 'index' }); if (r) setIndex(r) }
  const read = async (s: string, offset: number) => {
    const r = await fetchOp({ stream: s, offset, max_bytes: CHUNK })
    if (!r) return
    if (r.retained === false) { setIndex({ retained: false }); return }
    setPos(r)
    setText(t => { const all = (offset ? t.data : '') + (r.data ?? ''); const cut = Math.max(0, all.length - KEEP); return { data: all.slice(cut), dropped: (offset ? t.dropped : 0) + cut } })
  }
  const streams: any[] = index?.streams ?? []
  return <Section title="原始输出" extra={<span className="muted">保存在执行节点上，按需取回；不是产品事件</span>}>
    <p className="muted">CLI 与检查进程的完整标准输出与标准错误只在执行节点的 attempt 目录里，controller 不保存。取回经节点心跳领取，节点在线时通常几秒内完成；内容已由节点脱敏，按纯文本显示。</p>
    {!ran.length ? <p className="muted">还没有在设备上运行过的 attempt。</p> : <div className="actions">
      <select aria-label="选择 attempt" value={attempt} onChange={e => { setAttempt(e.target.value); setIndex(null); setStream(''); setPos(null); setText({ data: '', dropped: 0 }) }}>
        {ran.map(a => <option key={a.attempt_id} value={a.attempt_id}>epoch {a.epoch} · {short(a.attempt_id)} · {a.node_id}</option>)}</select>
      <button disabled={busy || !attempt} onClick={() => void loadIndex()}>取回输出清单</button>
      {op && <span>操作 <Badge v={op.status} /></span>}
    </div>}
    {err && <p className="err">{err}</p>}
    {index?.retained === false && <p className="warn-text">该节点上已没有这次执行的本地文件（已清理或换过数据目录）。</p>}
    {index && index.retained !== false && <>
      {streams.length ? <div className="actions">{streams.map(s => <button key={s.stream} className={stream === s.stream ? 'primary' : ''} disabled={busy}
        onClick={() => { setStream(s.stream); setPos(null); setText({ data: '', dropped: 0 }); void read(s.stream, 0) }}>{STREAM(s.stream)}（{bytes(s.bytes)}）</button>)}</div>
        : <p className="muted">这次执行没有留下可取的输出。</p>}
    </>}
    {stream && pos && <>
      <p className="muted">{STREAM(stream)}：已读 {bytes(pos.next_offset)} / {bytes(pos.total_bytes)}{pos.eof ? '（已到末尾）' : ''}
        {text.dropped ? `；页面只保留最后 ${bytes(KEEP)}，前面 ${text.dropped} 个字符已丢弃` : ''}
        {!pos.eof && <button className="link" disabled={busy} onClick={() => void read(stream, pos.next_offset)}>继续读取</button>}</p>
      <pre className="json log">{text.data || '（空）'}</pre>
    </>}
  </Section>
}
