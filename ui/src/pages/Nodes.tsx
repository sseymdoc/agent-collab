import { api } from '../api'
import { Action, Badge, Section, show, useData, when } from '../ui'
// 能力分层显示：在线（心跳）≠ CLI 已发现（版本）≠ 认证可用（探测）≠ 近期执行成功（绑定 agent/CLI 版本的证据）。

export function Nodes({ tick }: { tick: number }) {
  const { data, error } = useData(() => api.nodes(), [tick])
  if (error) return <p className="err">{error}</p>
  if (!data) return <p className="muted">加载中…</p>
  const nodes: any[] = data.nodes ?? data
  const hosts = new Map<string, any[]>()
  for (const n of nodes) hosts.set(n.physical_host_id ?? '未知主机', [...(hosts.get(n.physical_host_id ?? '未知主机') ?? []), n])
  return <>
    {[...hosts].map(([host, list]) => <Section key={host} title={'物理主机 ' + host}
      extra={<span className="muted">同一主机的节点共享 host_slots 与故障域；不能互为断电备份</span>}>
      <table>
        <thead><tr><th>节点</th><th>环境</th><th>故障域</th><th>状态</th><th>最近联系</th><th>槽位（节点/主机）</th><th>活动</th><th>工具与 harness</th><th>认证（不调用模型的探测）</th><th>执行证据（绑定当前版本）</th><th>账户分组</th><th>拒绝派单原因</th><th>排空</th></tr></thead>
        <tbody>{list.map(n => {
          const c = n.capabilities ?? {}
          return <tr key={n.node_id}>
            <td><b>{n.node_id}</b><br /><small className="muted">agent {show(c.agent?.version ?? n.agent_version)}{c.agent?.git_commit ? ' · ' + c.agent.git_commit.slice(0, 7) : ''}</small></td>
            <td>{show(c.os)} / {show(c.arch)}{(c.labels ?? []).length ? <><br /><small>{c.labels.join(', ')}</small></> : null}</td>
            <td>{show(n.failure_domain)}</td>
            <td><Badge v={n.status} /></td>
            <td>{when(n.last_seen_at)}</td>
            <td>{show(n.resources?.slots)} / {show(n.resources?.host_slots)}</td>
            <td>{show(n.active_attempts)}</td>
            <td><small>{Object.entries(c.tools ?? {}).map(([k, v]) => k + (v ? ' ' + v : '')).join('，') || '未知'}<br />{Object.entries(c.harness ?? {}).map(([k, v]) => k + ' ' + (v ?? '不可用')).join('，')}
              {Object.entries(c.harness_paths ?? {}).filter(([, v]) => v).map(([k, v]) => <span key={k}><br />{k} 路径 <code>{String(v)}</code></span>)}
              {Object.entries(c.harness_disabled ?? {}).map(([k, v]) => <span key={k} className="warn-text"><br />{k} 已停用：{String(v)}</span>)}
              {(c.gpu ?? []).length ? <><br />GPU {c.gpu.map((g: any) => typeof g === 'string' ? g : g.name + (g.cuda ? '（CUDA）' : '')).join(', ')}</> : null}</small></td>
            <td><small>{Object.entries(n.auth_health ?? {}).map(([k, v]) => <span key={k}>{k} <Badge v={String(v)} /> </span>)}{!Object.keys(n.auth_health ?? {}).length && '未知'}</small></td>
            <td><small>{Object.entries(n.capability_evidence ?? {}).map(([k, e]: [string, any]) => <span key={k}>{k} <Badge v={e.status} />
              {e.last_success ? <> 成功 {when(e.last_success.at)}</> : null}{e.last_failure ? <> · 失败 {when(e.last_failure.at)}（{show(e.last_failure.reason)}）</> : null}<br /></span>)}
              {!Object.keys(n.capability_evidence ?? {}).length && '暂无（在线或 CLI 版本不等于可执行）'}</small></td>
            <td><small>{Object.entries(n.provider_account_refs ?? {}).map(([k, v]) => k + '：' + show(v)).join('，') || '未知'}<br />额度：未知（不估算）</small></td>
            <td><small>{(n.last_rejections ?? n.last_rejection_reasons ?? []).join('，') || '无'}</small></td>
            <td>{n.drain
              ? <Action label="取消排空" run={() => api.drain(n.node_id, n.version, false)} />
              : <>
                <Action label="排空" danger confirm="排空后该节点不再领取新任务，已运行的任务继续到结束。确定？"
                  run={() => api.drain(n.node_id, n.version, true)} />
                <Action label="排空并迁移" danger
                  confirm="排空，并把可安全迁移的任务（已受保护、无外部副作用、有确认检查点）从最近检查点迁到其他节点；不满足条件的继续在本节点运行。确定？"
                  run={() => api.drain(n.node_id, n.version, true, true)} />
              </>}</td>
          </tr>
        })}</tbody>
      </table>
    </Section>)}
    {!nodes.length && <p className="muted">尚无已注册节点。用 controller pairing-code 生成配对码后在节点上执行 agent pair。</p>}
  </>
}
