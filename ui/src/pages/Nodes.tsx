import { api } from '../api'
import { can } from '../session'
import { Action, Badge, Section, short, show, useData, when } from '../ui'
// 能力分层显示：在线（心跳）≠ CLI 已发现（版本）≠ 认证可用（探测）≠ 近期执行成功（绑定 agent/CLI 版本的证据）。
// “当前任务”取自 controller 记录的有效 attempt 与租约，不由界面推断。

const CATALOG: Record<string, string> = { ok: '已上报', legacy: '旧版本设备：只按其固定组合派单', unknown: '未知' }

export function Nodes({ tick }: { tick: number }) {
  const { data, error } = useData(() => api.nodes(), [tick])
  const models = useData(() => api.models(), [tick])
  if (!data) return error ? <p className="err">{error}</p> : <p className="muted">加载中…</p>
  const nodes: any[] = data.nodes ?? data
  const hosts = new Map<string, any[]>()
  for (const n of nodes) hosts.set(n.physical_host_id ?? '未知主机', [...(hosts.get(n.physical_host_id ?? '未知主机') ?? []), n])
  const devices = can('devices')
  return <>
    {error && <p className="err">刷新失败，以下为上次成功加载的内容：{error}</p>}
    {/* 模型目录读取失败与设备没上报目录是两回事：失败时单独提示，不把它显示成“未知”。 */}
    {models.error && <p className="err">读取模型目录失败{models.data ? '，模型目录列为上次成功读取的内容' : ''}：{models.error}</p>}
    {[...hosts].map(([host, list]) => <Section key={host} title={'物理主机 ' + host}
      extra={<span className="muted">同一主机的设备共享 host_slots，不能互为断电备份；“故障域”是各设备自行上报的标签，恢复评分按它区分</span>}>
      <table>
        <thead><tr><th>设备</th><th>环境</th><th>状态</th><th>当前任务（有效 attempt）</th><th>槽位（设备/主机）</th><th>工具与 harness</th><th>模型目录</th><th>认证（不调用模型的探测）</th><th>执行证据（绑定当前版本）</th><th>拒绝派单原因</th>{devices && <th>排空</th>}</tr></thead>
        <tbody>{list.map(n => {
          const c = n.capabilities ?? {}
          const catalog = models.data?.catalog?.nodes?.[n.node_id]?.codex
          return <tr key={n.node_id}>
            <td><b>{n.node_id}</b><br /><small className="muted">agent {show(c.agent?.version ?? n.agent_version)}{c.agent?.git_commit ? ' · ' + c.agent.git_commit.slice(0, 7) : ''}<br />故障域 {show(n.failure_domain)}</small></td>
            <td>{show(c.os)} / {show(c.arch)}{(c.labels ?? []).length ? <><br /><small>{c.labels.join(', ')}</small></> : null}</td>
            <td><Badge v={n.status} />{n.drain ? <> <span className="badge warn">排空中</span></> : null}<br /><small className="muted">最近联系 {when(n.last_seen_at)}</small></td>
            <td>{(n.attempts ?? []).length ? (n.attempts as any[]).map(a => <div key={a.attempt_id}>
              <a href={'#/jobs/' + a.job_id}>{a.title || short(a.job_id)}</a> <Badge v={a.status} />
              <br /><small className="muted">{a.coordinates ? '协调回合' : '执行'} · {a.harness}{a.model ? ' · ' + a.model + ' / ' + a.effort : ''}{a.lease_expires_at ? ' · 租约至 ' + when(a.lease_expires_at) : ' · 等待领取'}</small></div>)
              : <span className="muted">空闲{n.active_attempts ? '（' + n.active_attempts + ' 个 attempt）' : ''}</span>}</td>
            <td>{show(n.resources?.slots)} / {show(n.resources?.host_slots)}</td>
            <td><small>{Object.entries(c.tools ?? {}).map(([k, v]) => k + (v ? ' ' + v : '')).join('，') || '未知'}<br />{Object.entries(c.harness ?? {}).map(([k, v]) => k + ' ' + (v ?? '不可用')).join('，')}
              {Object.entries(c.harness_paths ?? {}).filter(([, v]) => v).map(([k, v]) => <span key={k}><br />{k} 路径 <code>{String(v)}</code></span>)}
              {Object.entries(c.harness_disabled ?? {}).map(([k, v]) => <span key={k} className="warn-text"><br />{k} 已停用：{String(v)}</span>)}
              {(c.gpu ?? []).length ? <><br />GPU {c.gpu.map((g: any) => typeof g === 'string' ? g : g.name + (g.cuda ? '（CUDA）' : '')).join(', ')}</> : null}</small></td>
            <td><small>{!catalog ? (models.data ? '未知' : models.error ? '读取失败' : '读取中…') : <>{CATALOG[catalog.status] ?? catalog.status}{catalog.reason ? '（' + catalog.reason + '）' : ''}
              {catalog.models?.length ? <details><summary>{catalog.models.length} 个模型{catalog.fetched_at ? ' · 读取于 ' + when(catalog.fetched_at) : ''}</summary>
                {catalog.models.map((m: any) => <div key={m.id}><code>{m.id}</code>：{m.efforts.join('、')}</div>)}
                <span className="muted">目录来自该设备上的 CLI，不表示这个账号已成功调用过。</span></details> : null}</>}</small></td>
            <td><small>{Object.entries(n.auth_health ?? {}).map(([k, v]) => <span key={k}>{k} <Badge v={String(v)} /> </span>)}{!Object.keys(n.auth_health ?? {}).length && '未知'}
              <br />账户分组 {Object.entries(n.provider_account_refs ?? {}).map(([k, v]) => k + '：' + show(v)).join('，') || '未知'}</small></td>
            <td><small>{Object.entries(n.capability_evidence ?? {}).map(([k, e]: [string, any]) => <span key={k}>{k} <Badge v={e.status} />
              {e.last_success ? <> 成功 {when(e.last_success.at)}</> : null}{e.last_failure ? <> · 失败 {when(e.last_failure.at)}（{show(e.last_failure.reason)}）</> : null}<br /></span>)}
              {!Object.keys(n.capability_evidence ?? {}).length && '暂无（在线或 CLI 版本不等于可执行）'}</small></td>
            <td><small>{(n.last_rejections ?? n.last_rejection_reasons ?? []).join('，') || '无'}</small></td>
            {devices && <td>{n.drain
              ? <Action label="取消排空" run={() => api.drain(n.node_id, n.version, false)} />
              : <Action label="排空" danger confirm="排空后该设备不再领取新任务，已运行的任务继续到结束。确定？"
                  run={() => api.drain(n.node_id, n.version, true)} />}
              {/* 已排空的设备也可再迁移剩余任务；响应里的 not_migrated 列出留在本设备的 attempt 及原因。 */}
              <Action label={n.drain ? '迁移剩余任务' : '排空并迁移'} danger
                confirm="排空，并把可安全迁移的任务（已受保护、无外部副作用、有确认检查点）从最近检查点迁到其他设备；不满足条件的继续在本设备运行。确定？"
                run={() => api.drain(n.node_id, n.version, true, true)}
                done={r => (r?.not_migrated ?? []).length ? '未迁移：' + r.not_migrated.map((x: any) => short(x.attempt_id) + '（' + (x.reasons ?? []).filter(Boolean).join('、') + '）').join('；') : null} /></td>}
          </tr>
        })}</tbody>
      </table>
    </Section>)}
    {!nodes.length && <p className="muted">还没有已登记的设备。在 controller 上用 <code>agent-collab controller pairing-code</code> 生成配对码，再在设备上执行 <code>agent-collab agent pair</code>。</p>}
  </>
}
