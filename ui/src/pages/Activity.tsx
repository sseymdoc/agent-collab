import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { Badge, Json, ORIGIN, Section, errText, short, when } from '../ui'

// 活动：全局产品事件从新到旧。首屏取最新 200 条，“更早”从已载入的最小序号往前翻；tick 时从最大序号向后补读新事件。
const CATS: [string, string, (t: string) => boolean][] = [
  ['jobs', '任务', t => /^(job|attempt|coordinator)\./.test(t)], ['nodes', '设备', t => t.startsWith('node.')],
  ['projects', '项目', t => /^(project|baseline)\./.test(t)], ['settings', '设置', t => t.startsWith('settings.')],
  ['clients', '客户端', t => t.startsWith('client.')], ['audit', '操作记录', t => t === 'audit.action'],
]
const TYPE: Record<string, string> = {
  'job.created': '任务受理', 'job.updated': '任务状态变化', 'job.published': '成果发布', 'job.assessed': '记录验收', 'job.evidence_gap': '验收缺少执行证据',
  'job.downloaded': '成果整包下载', 'job.integration': '回写结果', 'attempt.assigned': '分配设备', 'attempt.leased': '设备领取', 'attempt.started': '开始执行',
  'attempt.progress': '执行进度', 'attempt.side_effect': '外部副作用', 'attempt.superseded': 'attempt 被取代', 'attempt.cancelled': 'attempt 已取消',
  'coordinator.triggered': '触发受管协调', 'coordinator.takeover': '接管协调权', 'coordinator.released': '释放协调权', 'coordinator.decision_applied': '应用协调决策',
  'coordinator.decision_rejected': '拒绝协调决策', 'coordinator.follow_up_rejected': '拒绝修复请求', 'coordinator.escalated': '协调升级人工处理',
  'node.registered': '设备登记', 'node.paired': '设备配对', 'node.updated': '设备状态变化', 'node.retired': '设备退役', 'node.restored': '设备恢复身份', 'node.operation': '设备管理操作',
  'project.created': '新建项目', 'project.updated': '修改项目', 'project.application': '成果应用到项目', 'baseline.created': '登记输入基线',
  'settings.updated': '修改设置', 'client.paired': '客户端配对', 'client.revoked': '客户端撤销', 'audit.action': '客户端操作',
  'controller.started': 'controller 启动', 'controller.restored': 'controller 数据恢复', 'controller.backup': 'controller 备份',
}
const actorName = (a: any) => a ? (a.name ?? ORIGIN[a.kind] ?? a.kind) : null
const MAX_RENDER = 500

export function Activity({ tick }: { tick: number }) {
  const [events, setEvents] = useState<Map<number, any>>(new Map())
  const [err, setErr] = useState<string | null>(null)
  const [done, setDone] = useState(false)
  const [busy, setBusy] = useState(false)
  const [cat, setCat] = useState('')
  const max = useRef(0)
  const merge = (list: any[]) => setEvents(m => { const n = new Map(m); for (const e of list) { n.set(e.seq, e); max.current = Math.max(max.current, e.seq) } return n })
  // 首次加载取最新一页；之后每次 tick 从已知最大序号向后补读（after 模式，升序）。
  useEffect(() => {
    const first = !max.current
    ;(first ? api.eventsBefore(0, 200) : api.events(max.current)).then(d => {
      const list: any[] = d?.events ?? []
      merge(list)
      if (first && list.length < 200) setDone(true)
      setErr(null)
    }, e => setErr(errText(e)))
  }, [tick]) // eslint-disable-line
  const all = [...events.values()].sort((a, b) => b.seq - a.seq)
  const test = CATS.find(c => c[0] === cat)?.[2]
  const shown = test ? all.filter(e => test(e.type)) : all
  const older = async () => {
    setBusy(true)
    try { const d = await api.eventsBefore(all[all.length - 1].seq, 200); merge(d.events ?? []); if ((d.events ?? []).length < 200) setDone(true) } catch (e) { setErr(errText(e)) } finally { setBusy(false) }
  }
  return <Section title="活动" extra={<span className="actions"><select aria-label="按类别筛选" value={cat} onChange={e => setCat(e.target.value)}>
    <option value="">全部类别</option>{CATS.map(([k, t]) => <option key={k} value={k}>{t}</option>)}</select>
    <small className="muted">已载入 {all.length} 条</small></span>}>
    <p className="muted">controller 记录的产品事件（状态变化、进度与操作记录），不是 CLI 的原始日志；原始输出在任务详情里按需从执行节点取回。</p>
    {err && <p className="err">{err}</p>}
    <table><thead><tr><th>时间</th><th>事件</th><th>相关对象</th><th>执行者</th><th></th></tr></thead>
      <tbody>{shown.slice(0, MAX_RENDER).map(e => { const p = e.payload ?? {}
        return <tr key={e.seq} className={e.stale ? 'stale' : ''}>
          <td><small>#{e.seq}<br />{when(e.created_at)}</small></td>
          <td>{TYPE[e.type] ?? e.type}{e.type === 'audit.action' && p.action ? <> <code>{p.action}</code></> : null}
            {e.type === 'job.updated' && p.status ? <> <Badge v={p.status} /></> : null}{e.stale ? <> <span className="badge warn">stale</span></> : null}
            <br /><small className="muted">{e.type}</small></td>
          <td><small>{e.job_id && <><a href={'#/jobs/' + e.job_id}>任务 {e.type === 'job.updated' && p.title ? p.title : short(e.job_id)}</a><br /></>}
            {e.node_id && <><a href="#/nodes">设备 {e.node_id}</a><br /></>}
            {p.project_id && <a href={'#/projects/' + p.project_id}>项目 {p.name && e.type.startsWith('project.') ? p.name : short(p.project_id)}</a>}</small></td>
          <td><small>{actorName(p.actor) ?? '—'}</small></td>
          <td>{Object.keys(p).length ? <details><summary>详情</summary><Json v={p} /></details> : null}</td>
        </tr> })}</tbody></table>
    {shown.length > MAX_RENDER && <p className="muted">筛选后共 {shown.length} 条，只显示最新 {MAX_RENDER} 条。</p>}
    {!all.length && !err && <p className="muted">{done ? '还没有事件。' : '加载中…'}</p>}
    <p className="actions">{done ? <small className="muted">没有更早的事件了（更早的可能已被裁剪）</small>
      : all.length > 0 && <button disabled={busy} onClick={() => void older()}>{busy ? '加载中…' : '更早'}</button>}</p>
  </Section>
}
