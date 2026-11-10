import { useState } from 'react'
import { api, JOBS_PAGE } from '../api'
import { Badge, Section, show, short, useData, when } from '../ui'

export function Jobs({ tick }: { tick: number }) {
  const [status, setStatus] = useState('')
  // 分页游标栈：每项是对应页的 before（上一页最后一个 job_id）；空栈为最新一页。
  const [pages, setPages] = useState<string[]>([])
  const before = pages[pages.length - 1] ?? ''
  const { data, error } = useData(() => api.jobs(status, before).then(d => ({ ...d, status, before })), [tick, status, before])
  const jobs: any[] = data?.jobs ?? []
  // 新页面到达前仍显示旧数据：这期间不允许翻页，避免把同一游标重复压栈。
  const settled = !!data && data.status === status && data.before === before
  const pager = <p className="actions">
    {pages.length > 0 && <button disabled={!settled && !error} onClick={() => setPages(p => p.slice(0, -1))}>上一页（较新）</button>}
    {(pages.length > 1 || (error && pages.length > 0)) && <button onClick={() => setPages([])}>回到最新</button>}
    {settled && jobs.length >= JOBS_PAGE && <button onClick={() => setPages(p => [...p, jobs[jobs.length - 1].job_id])}>下一页（更早）</button>}
    <small className="muted">第 {pages.length + 1} 页，每页最多 {JOBS_PAGE} 个，按创建时间从新到旧</small>
  </p>
  return <Section title="工作包" extra={<select aria-label="按状态筛选工作包" value={status} onChange={e => { setStatus(e.target.value); setPages([]) }}>
    <option value="">全部状态</option>
    {['queued', 'waiting_capability', 'running', 'recovering', 'waiting_reconcile', 'cancel_requested', 'cancelled', 'completed', 'failed'].map(s => <option key={s} value={s}>{s}</option>)}
  </select>}>
    {error && <p className="err">{error}</p>}
    <table>
      <thead><tr><th>工作包</th><th>运行</th><th>保护</th><th>验收</th><th>交付版本</th><th>回写</th><th>epoch / 尝试</th><th>最近可恢复检查点</th><th>更新</th></tr></thead>
      <tbody>{jobs.map(j => <tr key={j.job_id}>
        <td><a href={'#/jobs/' + j.job_id}>{j.title || short(j.job_id)}</a><br /><small className="muted">{short(j.job_id)} · 根 {short(j.work_package_id)}</small></td>
        <td><Badge v={j.status} />{j.status_reason ? <><br /><small>{show(j.status_reason)}</small></> : null}</td>
        <td><Badge v={j.protection} /></td>
        <td><Badge v={j.acceptance?.status} /></td>
        <td>{show(j.delivery_version)}</td>
        <td><Badge v={j.integration_status} /></td>
        <td>{show(j.current_epoch)} / {show(j.counters?.attempts)}</td>
        <td>{when(j.durable_checkpoint_at)}</td>
        <td>{when(j.updated_at)}</td>
      </tr>)}</tbody>
    </table>
    {data && !jobs.length && <p className="muted">{pages.length ? '没有更早的工作包。' : '没有工作包。'}</p>}
    {pager}
  </Section>
}
