import { api } from '../api'
import { Action, Badge, Section, show, useData, when } from '../ui'

const GROUPS: [string, string[]][] = [
  ['排队', ['queued']], ['执行', ['running']], ['恢复', ['recovering']],
  ['等待能力', ['waiting_capability']], ['待对账（需人工）', ['waiting_reconcile']], ['取消中', ['cancel_requested']],
  ['失败', ['failed']], ['待验收', ['completed']], ['已取消', ['cancelled']],
]

export function Overview({ tick }: { tick: number }) {
  const { data: o, error, reload } = useData(() => api.overview(), [tick])
  if (!o) return error ? <p className="err">{error}</p> : <p className="muted">加载中…</p>
  const counts = o.job_counts ?? o.counts ?? {}
  const ctl = o.controller ?? {}
  return <>
    {error && <p className="err">刷新失败，以下为上次成功加载的内容：{error}</p>}
    <div className="tiles">
      {GROUPS.map(([label, keys]) => {
        const n = keys.reduce((s, k) => s + (Number(counts[k]) || 0), 0)
        return <a className="tile" key={label} href={'#/jobs'}><span>{label}</span><b>{n}</b></a>
      })}
    </div>
    <Section title="恢复保障" extra={<span className="muted">只展示 controller 已确认的事实</span>}>
      <dl className="kv">
        <dt>最近已确认可恢复检查点</dt><dd>{when(o.durable_checkpoint_at)}</dd>
        <dt>部署 profile</dt><dd>{show(ctl.deployment_profile)}</dd>
        <dt>保障说明</dt><dd>{show(ctl.guarantees)}</dd>
        <dt>authority_generation</dt><dd>{show(ctl.authority_generation)}</dd>
      </dl>
    </Section>
    <Section title="控制面备份" extra={<span className="muted">worker 故障看上面的云端检查点；控制服务数据恢复看这里的实例外备份</span>}>
      {(() => {
        const b = o.backup ?? {}
        const age = (s: unknown) => typeof s === 'number' ? (s < 3600 ? Math.round(s / 60) + ' 分钟' : Math.round(s / 3600) + ' 小时') : '未知'
        return <dl className="kv">
          <dt>ECS 本地最近成功</dt><dd>{when(b.local_last_success_at)}（年龄 {age(b.local_age_s)}）</dd>
          <dt>实例外最近确认</dt><dd>{when(b.offsite_last_confirmed_at)}（年龄 {age(b.offsite_age_s)}）</dd>
          <dt>最近错误</dt><dd>{b.last_error ? <span className="warn-text">{show(b.last_error)}</span> : '无'}</dd>
          <dt>说明</dt><dd>只显示实际完成并校验的时间；定时周期不等于保证的 RPO。</dd>
        </dl>
      })()}
    </Section>
    <Section title="受管协调者" extra={<span className="muted">主控会话中断后，由幸存节点上的 Claude 按事件续作</span>}>
      {(() => {
        const m = o.managed_coordinator ?? {}
        const label: Record<string, string> = { ready: '已配置', profile_missing: 'profile 缺失（不会猜测模型或强度）', disabled: '已关闭' }
        return <dl className="kv">
          <dt>状态</dt><dd>{label[m.status] ?? show(m.status)}</dd>
          <dt>profile</dt><dd>{m.profile ? `${show(m.profile_id)}：${m.profile.model} / ${m.profile.effort}，节点 ${(m.profile.node_ids ?? []).join(', ')}` : '未配置'}</dd>
          <dt>说明</dt><dd>“已配置”只表示字段完整；节点上的 CLI 是否已登录、是否在线，以协调回合的实际执行结果为准。</dd>
        </dl>
      })()}
    </Section>
    <Section title="派单" extra={<Badge v={o.dispatch_paused ? 'blocked' : 'ok'} />}>
      <p>{o.dispatch_paused ? '全局派单已暂停：新任务受理后排队，不分配节点；执行中的任务不受影响。' : '派单正常。'}</p>
      <Action label={o.dispatch_paused ? '恢复派单' : '暂停派单'} danger={!o.dispatch_paused}
        run={() => api.dispatch(!o.dispatch_paused, ctl.metadata_version).then(reload)} />
    </Section>
    <Section title="节点摘要">
      <table><thead><tr><th>节点</th><th>物理主机</th><th>状态</th><th>排空</th><th>最近联系</th></tr></thead>
        <tbody>{(o.nodes ?? []).map((n: any) => <tr key={n.node_id}>
          <td>{n.node_id}</td><td>{show(n.physical_host_id)}</td><td><Badge v={n.status} /></td><td>{show(n.drain)}</td><td>{when(n.last_seen_at)}</td>
        </tr>)}</tbody></table>
    </Section>
    <Section title="控制服务">
      <dl className="kv">
        <dt>版本</dt><dd>{show(ctl.version)}{ctl.git_commit ? ' · ' + String(ctl.git_commit).slice(0, 7) : ''}</dd><dt>controller_id</dt><dd>{show(ctl.controller_id)}</dd>
        <dt>说明</dt><dd>TCP 连通不代表任务可接管；可接管性取决于检查点复制状态和合格节点，见各工作包详情。</dd>
      </dl>
    </Section>
  </>
}
