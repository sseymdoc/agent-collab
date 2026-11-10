import { useState } from 'react'
import { api } from '../api'
import { Badge, Section, show, short, useData, when } from '../ui'

export function Jobs({ tick }: { tick: number }) {
  const [status, setStatus] = useState('')
  const { data, error } = useData(() => api.jobs(status), [tick, status])
  const jobs: any[] = data ? (data.jobs ?? data) : []
  return <Section title="工作包" extra={<select value={status} onChange={e => setStatus(e.target.value)}>
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
    {data && !jobs.length && <p className="muted">没有工作包。</p>}
  </Section>
}
