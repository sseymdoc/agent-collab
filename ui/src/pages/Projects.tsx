import { useState } from 'react'
import { api } from '../api'
import { Action, Section, bytes, short, useData, when } from '../ui'

// 项目列表：项目是 controller 上的记录；它对应哪台机器上的哪个目录，是各台设备自己的绑定，不上传。
export function Projects({ tick }: { tick: number }) {
  const { data, error } = useData(() => api.projects(), [tick])
  const [archived, setArchived] = useState(false)
  const [name, setName] = useState('')
  const all: any[] = data?.projects ?? []
  const list = all.filter(p => archived || !p.archived_at)
  const hidden = all.length - list.length
  return <Section title="项目" extra={<label className="row"><input type="checkbox" checked={archived} onChange={e => setArchived(e.target.checked)} /> 显示已归档{hidden ? `（${hidden} 个）` : ''}</label>}>
    {error && <p className="err">{error}</p>}
    <table><thead><tr><th>名称</th><th>任务（进行中）</th><th>最近登记的输入</th><th>最近一次完整应用</th><th>状态</th></tr></thead>
      <tbody>{list.map(p => <tr key={p.project_id}>
        <td><a href={'#/projects/' + p.project_id}>{p.name}</a><br /><small className="muted"><code>{short(p.project_id)}</code> · 第 {p.revision} 版</small></td>
        <td>{p.jobs ?? 0}（{p.active_jobs ?? 0}）</td>
        <td>{p.baseline ? <>{p.baseline.files} 个文件，{bytes(p.baseline.bytes)}<br /><small className="muted">{when(p.baseline.at)}</small></> : <span className="muted">还没有输入</span>}</td>
        <td>{p.applied ? <><a href={'#/jobs/' + p.applied.job_id}>任务 {short(p.applied.job_id)}</a> · {p.applied.device ?? '未知设备'}<br /><small className="muted">{when(p.applied.finished_at)}</small></> : <span className="muted">无</span>}</td>
        <td>{p.archived_at ? <span className="badge muted" title={p.archived_at}>已归档</span> : <span className="badge good">使用中</span>}</td>
      </tr>)}</tbody></table>
    {data && !list.length && <p className="muted">{all.length ? '没有未归档的项目。' : '还没有项目。项目把一个本机文件夹、它的输入版本、默认模型策略与成果应用记录放在一起。'}</p>}
    <div className="actions">
      <input aria-label="新项目名称" placeholder="新项目名称" value={name} maxLength={200} onChange={e => setName(e.target.value)} />
      <Action label="新建项目" scope="tasks" disabled={!name.trim()} run={async () => {
        const p = await api.createProject(name.trim())
        setName('')
        location.hash = '#/projects/' + p.project_id
      }} />
    </div>
  </Section>
}
