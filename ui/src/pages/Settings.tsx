import { api } from '../api'
import { Badge, Json, Section, show, useData } from '../ui'

export function Settings({ tick }: { tick: number }) {
  const doctor = useData(() => api.doctor(), [tick])
  const ov = useData(() => api.overview(), [tick])
  const ctl = ov.data?.controller ?? {}
  const doc = doctor.data ?? {}
  const checks: any[] = doc.checks ?? Object.entries(doc).map(([name, v]) => ({
    name, ok: typeof v === 'boolean' ? v : v === 'unknown' ? null : undefined, detail: v,
  }))
  return <>
    <Section title="运行 profile 与服务身份">
      <dl className="kv">
        <dt>controller 版本</dt><dd>{show(ctl.version)}{ctl.git_commit ? <> · 提交 <code>{String(ctl.git_commit).slice(0, 10)}</code></> : '（提交信息不可用）'}</dd>
        <dt>controller_id</dt><dd><code>{show(ctl.controller_id)}</code></dd>
        <dt>authority_generation</dt><dd>{show(ctl.authority_generation)}</dd>
        <dt>部署 profile</dt><dd>{show(ctl.deployment_profile)}</dd>
        <dt>公开入口</dt><dd>{show(doc.public_origin)}{doc.tls === false ? '（controller 直接监听未启用 TLS；生产应在反向代理终止 TLS）' : ''}</dd>
        <dt>最近备份</dt><dd>{doc.backup_age_s === 'unknown' || doc.backup_age_s == null ? '未知（尚未配置或未完成实例外备份）' : show(doc.backup_age_s) + ' 秒前'}</dd>
        <dt>当前页面来源</dt><dd><code>{location.origin}</code>{location.protocol === 'https:' ? '（TLS，浏览器已校验证书）' : '（非 TLS，连接未加密）'}</dd>
        <dt>部署保障</dt><dd>{show(ctl.guarantees)}</dd>
      </dl>
    </Section>
    <Section title="节点配对">
      <p>在 controller 主机执行 <code>python -m agent_collab.controller pairing-code --data-dir &lt;数据目录&gt; --node-id &lt;节点名&gt;</code> 获取一次性配对码，再在节点执行 <code>python -m agent_collab.node_agent pair --controller &lt;地址&gt; --code &lt;配对码&gt; --node-id &lt;节点名&gt; --data-dir &lt;目录&gt;</code>。配对令牌、UI 会话与模型登录彼此独立。</p>
    </Section>
    <Section title="doctor 自检">
      {doctor.error && <p className="err">{doctor.error}</p>}
      <table><thead><tr><th>检查</th><th>结果</th><th>说明</th></tr></thead>
        <tbody>{checks.map((c, i) => <tr key={i}><td>{c.name}</td><td>{c.ok === undefined ? '—' : <Badge v={c.ok === true ? 'passed' : c.ok === false ? 'failed' : null} />}</td><td><small>{show(c.detail ?? c.message)}</small></td></tr>)}</tbody></table>
      {!checks.length && doctor.data && <Json v={doctor.data} />}
    </Section>
  </>
}
