# Controller 运维与集成（WP1）

实现：Python 标准库，兼容 Python 3.9；数据库只在 controller 所在机器的本地文件系统打开。不要从 Windows 经 SMB 打开 Mac 数据库。HTTP 包装使用 `ThreadingHTTPServer`，核心入口为 `App(data_dir, config=None, clock=time.time).handle(method, target, headers, body)`，无需 socket 即可测试。调用方结束时执行 `app.close()`。

## 启动与身份

```sh
python -m agent_collab.controller serve --data-dir /local/controller-data --bind 127.0.0.1:18669
python -m agent_collab.controller admin-token --data-dir /local/controller-data
python -m agent_collab.controller pairing-code --data-dir /local/controller-data --node-id mac-worker --ttl 600
```

`admin-token` 只打印文件路径。管理员令牌首次生成于 `admin.token`；POSIX 权限为 0600。此文件是协议要求的唯一明文凭据文件；数据库中的节点令牌、租约、UI 会话、CSRF 和配对码均只保存 SHA-256，比较使用 `hmac.compare_digest`。配对码过期或使用一次后失效，再次配对同一节点会撤销旧 node token。节点凭据不能访问客户端查询或控制端点；管理员/UI 凭据不能冒充节点。

非 loopback 地址必须同时提供 `--tls-cert` 和 `--tls-key`，否则在创建监听器之前拒绝启动。例如：

```sh
python -m agent_collab.controller serve --data-dir /local/controller-data --bind 192.0.2.10:18669 --tls-cert server.pem --tls-key server.key --public-origin https://controller.example:18669
```

已有 TLS 反向代理时，让 controller 保持 loopback 监听，配置实际外部 `public_origin`。不信任请求中的 Host/X-Forwarded-* 来推导登录源。未指定公开源时使用实际监听源；配置文件中的公开源会保留，命令行参数优先。UI 登录返回 HttpOnly、SameSite=Strict cookie；公开源为 HTTPS 时附加 Secure。UI 变更要求 CSRF，Origin 只能为空或精确匹配公开源。

serve 全生命周期持有 OS 文件锁 `controller.lock`；第二个 serve 或 restore 会拒绝执行。锁文件保留，退出/崩溃由 OS 释放锁。直接构造 App 是进程内测试/嵌入边界，**调用方负责保证同一目录只有一个活动 App**；它不替代 serve 的进程锁。

## 数据和配置

目录包含 `controller.db`（SQLite WAL/FULL）、`controller.db-wal/-shm`、`blobs/ab/cd/<sha256>`、`admin.token`、`controller.lock`、可选 `config.json`。运行时临时文件放在目标数据目录内，不依赖系统临时目录。blob 先写同盘临时文件，fsync 后验证 SHA-256，再 `os.replace`；manifest 本身也是 blob。

可选配置示例：

```json
{
  "public_origin": "http://127.0.0.1:18669",
  "deployment_profile": "local",
  "guarantees": "Single controller; durable checkpoints only.",
  "heartbeat_s": 5,
  "suspect_after_s": 30,
  "lease_ttl_s": 90,
  "assign_claim_timeout_s": 60,
  "quota_block_s": 900,
  "max_blob_bytes": 67108864,
  "max_json_bytes": 2097152,
  "sse_ping_s": 15,
  "score_weights": {
    "free_slots": 10,
    "success_rate": 2,
    "same_host": 1,
    "different_failure_domain": 20,
    "warm_start": 1
  }
}
```

顶层未知配置键、非正/非有限时间或尺寸、错误公开源均拒绝。`managed_coordinator` 与 `standby_profiles` 内部只读取已定义字段：未知子键被忽略，字段缺失或非法时 overview 显示 `profile_missing`（不报配置错误），请据此核对拼写。时间由 controller 的 `clock()` 决定；生产默认 wall clock，需保持主机时钟正常。以上间隔是协议默认值，不是实测 RTO/RPO。重启不会增加 generation；会延长已有活动租约并记录 `controller.started`，不重复建立 attempt。后台每秒 tick；claim/heartbeat 也调度。心跳正常而没有输出不会判为失联。

SQLite 事务同时提交权威状态、事件和幂等回执。请求错误回滚该请求；已发生的到期处理是独立事实。blob 是不可变外部文件，数据库回滚可能留下未引用 blob，不会产生引用未写完 blob 的 durable 状态。上传中的 checkpoint 只有文件哈希已验证且长度匹配后才能 durable，并与 job 指针同事务提交；晚完成的旧 checkpoint 不会覆盖较新的指针。

## 集成约定与协议缺口的处理

API 路径统一前缀 `/api/v1`，另支持无认证 `/healthz`。`GET /healthz` 仅返回 `{"ok":true}`。请求 ID 使用小写 UUID v4。错误统一 `protocol.error_body`。

- §5 的通用规则优先于表格省略字段：客户端 cancel/retry/assess/publish/integration/drain/dispatch 均须携带 `request_id`、`expected_version`；assess 还须独立 `assessment_id`。创建 jobs 没有已有行版本，只要求 `request_id`。session 登录/注销是凭据生命周期，不使用通用幂等缓存，避免持久化明文 session/CSRF 响应。节点端点按 §4 的专用规则执行；节点 integration 无 expected_version，仍检查当前交付指针。
- 客户端和节点均可 PUT/HEAD/GET blob。协议只在节点表列出了上传接口，但客户端需要上传初始输入；此处允许已认证管理员/UI 上传，UI PUT 仍受 CSRF/Origin 约束。blob 不执行、不物化。
- 集合返回 `{"jobs":[...]}`、`{"nodes":[...]}`、`{"events":[...],"min_seq":N}`。job 详情为 job 字段加 `attempts/checkpoints/publications/assessments/events`；explain 返回 `{"nodes":[...],"selected_node_id":...}`。job `status_reason` 为原因码及缺失 hash 组成的字符串。`/overview.controller.version` 为软件版本，`metadata_version` 用作 `/dispatch.expected_version`。
- GPU 要求可为名称字符串或属性对象，按名称/属性精确匹配；没有自动推断 CUDA 版本兼容性。labels 也硬过滤，协议未列出的原因码使用 `label_missing:<label>`。协议没有初次执行的源节点字段，因此首次 source locality 分数中立；恢复时用上一 attempt 的节点计算不同故障域、同节点缓存等分数。近期成功率取该节点最近 20 个结束 attempt，未知为 0.5；没有伪造硬件或迁移耗时测量值。
- root 工作包只允许串行修订；新修订累计 repairs，各 job 暴露同根累计 counters。协议没有 `max_infra_retries`，由 `max_attempts` 同时限制基础设施重试。slots 按 contract.resources.slots 预留；同主机取所有节点 host_slots 的最小值，注册也不能把容量降到已有预留以下。
- quota 对同 harness/账户分组封锁 `quota_block_s`，新提交任务在封锁结束后可调度。失败的 quota/auth_expired/capability_missing job 保持 waiting_capability，外部条件修复后显式 retry；不会自动跨账户或主机探路。auth_expired 同时将已知同账户节点的 auth_health 标为 expired。
- drain 默认停止新 claim；可选 migrate=true 在同一事务中对受保护、无副作用、有恢复基线且预算/目标容量允许的活动 attempt 执行 retry 迁移。响应在 node 字段外增加 not_migrated，逐条解释保留运行的 attempt。旧租约立即撤销，新节点从最近 durable 检查点或 inputs 开始；旧节点下次心跳获知撤销。没有活进程迁移或强杀节点的额外协议。retry 计一次 migrations，即使仍在同一节点。
- side-effect `intent` 未收到 done 时，与 unknown 一样保守等待对账；晚到 intent 不覆盖 done。checkpoint 摘要不能清除已观察到的 unknown。unknown/intent 阻止自动和显式 retry；协议没有人工副作用对账端点，因此此状态需要主控补充协议再解除，不提供绕过门禁。没有取消请求的 `failed/user_cancel` 等待对账；不会伪造取消确认。
- 节点在 manual publish 模式调用 publish 得到 403；客户端可发布。passed 验收按被发布 checkpoint 的最近一次 assessment 检查。成功完成后保留该 epoch 的发布凭据，直到被 retry/revoke/restore 失效；执行租约超时只判定活动 attempt。旧 epoch/generation 的 fencing 优先于返回历史幂等回执。租约是含 generation 前缀的随机不透明字符串，仍只存整体哈希；restore 后即使旧 attempt 行已不在备份中，也能返回 stale_generation。此类迟到事件只存 stale，job_id 为 null。
- `GET /jobs?before=` 接受上页最后一个 job_id；limit 默认 200，上限 1000。SSE 的 App 迭代器每批至多 1000 条，空批返回 ping；HTTP 持续轮询并按 sse_ping_s 发 ping，断开后停止。cursor 查询参数优先于 Last-Event-ID；从初始、未裁剪日志的 cursor=0 可读全量。

## 协调租约与受管协调者（WP4）

`config.json` 可增加以下配置；model、effort 与节点 ID 均由用户填写，示例占位符不是默认模型：

```json
{
  "managed_coordinator": {"enabled": true, "profile_id": "standby-mac"},
  "standby_profiles": {
    "standby-mac": {
      "harness": "claude",
      "model": "<用户指定模型>",
      "effort": "<用户指定强度>",
      "node_ids": ["mac-worker"]
    }
  }
}
```

`GET /api/v1/overview` 新增如下字段，其余字段保持原形状：

```json
{
  "managed_coordinator": {
    "status": "ready",
    "profile_id": "standby-mac",
    "profile": {"harness": "claude", "model": "<配置值>", "effort": "<配置值>", "node_ids": ["mac-worker"]}
  }
}
```

缺失或不完整时为 `{"status":"profile_missing","profile_id":null,"profile":null}`；如果已给出 profile_id，保留其配置值。显式关闭为 `{"status":"disabled","profile_id":null,"profile":null}`，同样保留已配置的 profile_id。ready 只说明字段完整；CLI 缺失、节点不可用或认证过期通过协调 job 的 waiting_capability 和 explain 表达。profile 不完整不会阻止普通 job 完成，但不生成协调 job。

`GET /api/v1/packages/{work_package_id}/coordinator` 返回 JSON `null`，或以下对象（没有 token/hash）：

```json
{"holder_kind":"interactive","holder_ref":"desktop-session","expires_at":"2026-11-09T03:50:00.000Z","version":1}
```

POST 同路径提交 `{"request_id":"<UUID v4>","holder_kind":"interactive","holder_ref":"desktop-session","ttl_s":90,"takeover":false,"reason":""}`，返回 `{"coordinator_token":"<不透明凭据>","expires_at":"<UTC时间>"}`。续租使用新 request_id、相同 holder_kind/ref，并带 `X-Coordinator-Token`；DELETE 带该头释放，返回 `{}`。其他持有者占用时为 409 conflict；管理员 Bearer 或经管理员登录建立的 UI 会话可用 takeover=true 和非空 reason 接管，并写 coordinator.takeover 事件。节点身份不能获取/接管。UI 仍须提供 CSRF 和合法 Origin。

协调租约按目标根工作包独占。客户端 assess/publish/retry/integration 及同根新 job 提交缺少匹配 token 时返回 `409 {"error":{"code":"conflict","message":"…","details":{"reason":"coordinator_lease"}}}`。cancel 不受门禁影响；节点 auto publish 豁免协调 token，但仍受 epoch/generation/CAS 约束；节点 integration 不豁免。检查门禁后才读写普通请求幂等回执，旧协调者不能凭旧请求重放绕过租约。

协调 POST 的专用幂等记录只含请求哈希、凭据哈希和到期时间；获取凭据由服务端秘密与 request_id/generation 派生以重放丢失的获取回执，不将 token 明文写入 SQLite。已释放、过期或接管的回执返回 409。续租回执重放仍须原 token。普通重启保留租约；restore 的 generation 变更会使旧协调凭据无效。

缺省策略下 completed/waiting_reconcile/rework 事件与协调 job 创建同事务提交。rc5 以目标 job_id、触发类型、durable_checkpoint_id、acceptance.status 持久去重；同一目标已有未结束协调 job 时不再新建。每个协调 job 有独立根及执行预算，contract.coordinates 指向被协调的目标根；执行不增加目标根 attempts/repairs。model/effort 与 node_ids 在触发时冻结；仅允许名单节点领取，领取时原子取得 managed 租约，holder_ref 为协调 attempt_id。心跳续租，结束/失联释放；被接管后旧 attempt 不能应用结果或释放新租约。协调 job 不自动恢复，也不递归触发协调。正常客户端不能伪造 claude/coordinates job。

context.json 提供目标 job（含契约与状态）、最新 attempt report、最新 durable checkpoint 对 inputs 的 diff 和验收记录。decision-schema.json 为 controller/adapter 共用的固定 schema。两文件通过 inputs_manifest 和 blob 提供；不复制原生 transcript 或 provider 凭据。

WP5 另附 files 与 files_omitted：优先最新 final，无则最新 durable；每个 UTF-8 文本 ≤32 KiB、总计 ≤256 KiB，按路径排序，
明确记录 excluded/too_large/binary/budget。report 保留 agent 传入的对象与 usage 计数；显式非对象返回 400、canonical JSON
超过 64 KiB 返回 413。报告的脱敏由 agent.clean 完成，缺省旧 report 不补造；report=null 的旧 agent 仍兼容。当前 decision schema 见下文。
若最新 final 早于最新 boundary，files 来自 final，既有 diff 与 report 仍分别来自最新 durable 与最新 attempt；详见协议 §10。

工作区 contract.workspace.exclude 在受理时校验，随 request_hash 保存；缺省排除解释器与测试缓存。显式 [] 关闭排除，
自定义列表替换默认列表。该规则不修改输入物化行为；详见协议 §3。

排空迁移示例：`POST /api/v1/nodes/mac-worker/drain {"request_id":"<UUID v4>","expected_version":<node.version>,"drain":true,"migrate":true}`。
协调租约仍生效，需要时携带 X-Coordinator-Token；无 token 的相关 attempt 保持运行并返回 coordinator_lease。
停止迁移条件也包括取消请求、未对账副作用、全局暂停、共享主机容量、预算和目标能力不足。drain=false 忽略 migrate。

成功 attempt 的 final checkpoint 必须含 decision.json。follow_up 不再允许模型手写完整契约，其形状严格为 null 或以下对象，所有字段必填、额外字段拒绝：

```json
{
  "title": "补齐空名字分支和单元测试",
  "goal": "空名字返回默认问候，保留非空名字行为",
  "acceptance": ["非空名字用例通过", "空名字用例通过"],
  "instructions": "修改目标函数并补充测试，运行对应 unittest。",
  "effort": "xhigh"
}
```

acceptance 必须是非空字符串数组，effort 仅 high/xhigh/max。schema 的 follow_up 对象为 additionalProperties=false；旧 contract、scope、base_checkpoint_id 或 budgets 字段都不接受。adapter 仅作 schema 形状复核，schema 合法就写 decision.json；assessment 的 checkpoint/状态语义和修复准入条件由 controller 判断。缺字段、旧形状等在 adapter 端仍会失败，controller 的部分应用不绕过这一节点边界。

此修订需要 controller 与 Claude adapter 同步升级；已创建协调 job 的 inputs 中保存的是当时的固定 schema，不自动改写。旧 schema 不匹配的新 adapter 会拒绝启动该回合，应由主控处置旧待执行回合并重新触发新 schema 的协调 job，不在 worker 内迁移历史数据。

controller 生成的修复契约使用 codex、gpt-6-astra、workspace-write，effort 取请求；goal/acceptance 取请求，prompt 由 instructions、目标、验收项和目标契约 excludes 组成。excludes、requirements、recovery、resources、publish、integration、workspace、checks、coordination 继承目标；requirements.tools 增加 codex，不替换既有工具。budgets 继承目标的累计上限并保留取较小值约束，沿同根计数，不让模型填写预算或将上限误写为剩余额度。inputs_manifest 原样取被验收检查点（含 base/deleted），由 domain.validate_contract 验证结构，再由 controller 校验同根检查点来源；普通客户端仍不能提交带未知 base 的初始输入。

应用前仍检查持有权、目标 epoch/状态/检查点/验收/取消状态和串行修订关系；自动 publish 的版本更新不使决策失效。合法 accept/rework 的验收绑定 checkpoint manifest，先记录并写 coordinator.decision_applied，再在单独保存点处理 follow_up。形状错误、生成契约无效或派单失败只回滚修复步骤，写 coordinator.follow_up_rejected（含原因）；超预算或副作用未知写 coordinator.escalated。验收保留、repairs 不增加，协调 attempt 为 succeeded、协调 job 为 completed。follow_up=null 时仅记录验收；成功修复派单沿同根增加 repairs。waiting_reconcile 的旧目标在修复创建前置为 failed(managed_rework)，创建失败则该状态变更回滚，但保留验收。完成请求重放既不重复验收，也不重复派单或拒绝事件。

只有验收非法或租约/目标/检查点前提失效时，才整体拒绝并写 coordinator.decision_rejected。显式 escalate 保持原行为：只写事件，目标继续等待人工，不凭 null checkpoint_id 创造验收记录。测试夹具 tests/fixtures/claude_rework_request.json 保留主控真实样本的 assessment，仅将 follow_up 转为新请求形状；测试证明样本可记录 rework 并生成合法 Codex 修复 job，未在 worker 内重新调用真实模型。

本实现只处理新触发：因 interactive 租约或配置缺失被跳过的历史事件，不在租约过期/配置启用时自动补发。managed 应用中的 rework 验收属于该轮决策，不再触发自身的下一轮；修复 job 后续完成可正常触发。以上行为及真实 CLI/认证可用性应由集成验收分别核对；没有真实模型或跨机接管证明。

## rc5 检查、能力证据与回写待办

`contract.checks` 缺省为空，最多 16 项；每项包含唯一 name、非空字符串 argv 的 run 与 1–3600 秒的 timeout_s（缺省 600）。检查契约和 coordination 都参与请求哈希并由修复契约继承。controller 不执行检查命令，执行与日志采集由节点负责。

管理员 Bearer、UI 会话和受管决策共用 assess 实现。请求 passed 且有 checks 时，只读取被验收检查点所属 attempt 的 report.checks：manifest 必须匹配，每项检查必须存在且退出码为整数 0、timed_out 为布尔 false。缺失报告、manifest 不匹配、缺项、失败和超时分别记录 no_report、manifest_mismatch、missing、failed、timed_out。畸形或重复同名结果保守视为缺项/失败。缺证据时保留验收记录，改为 unverified，附 requested_status=passed 和 evidence_gaps，写 job.evidence_gap；不会把检查点升级为 verified，也不能通过 requires_acceptance 发布。全部满足时附 evidence（attempt_id、manifest_sha256、checks）。job.acceptance 始终提供 requested_status（无降级时 null）与 evidence_gaps（无缺项时 []）。没有 checks 的旧契约保持既有验收行为；文字自述、stub steps 和模型 accept 不能替代 checks。

`coordination.policy` 缺省 always，保留原触发场景；none 禁用受管触发；on_failure 只在进入 waiting_reconcile、failed、waiting_capability，或完成的 attempt 有 recovered_from_attempt 时触发。未结束协调 job 指状态不属于 completed/failed/cancelled；waiting_reconcile 的协调 job 仍需人工处置。因已有未结束协调 job 被抑制的事件不自动补发，也不更新已有不可变 context；目标变化后旧决策仍会被拒绝。取消请求阻止修复，根 attempts/repairs/migrations/deadline 预算继续约束修复链，协调 job 自己的执行不消耗目标根预算。

node.capability_evidence 与 explain 的各节点条目展示同一证据。领取时捕获 agent_version、harness_version、harness_path，完成时使用实际报告的 runtime 版本；注册变更导致绑定不匹配时标为 unproven，须有新执行结果才恢复。成功但必需 checks 不全通过记 checks_failed；其他失败使用 failure_class；user_cancel、node_lost 不计，管理员 retry 撤销也不制造执行失败。新绑定下的执行记录重新开始累计，不混用旧绑定的成功记录。旧节点未上报路径时 bound.harness_path 为 null。证据不加入调度硬过滤或评分。注册接受 §11 的 harness_paths、harness_disabled、agent，attempt.runtime 接受 agent_commit。

发布成功且配置了 target_node_id 后，在同一事务写 integration_pending 和持久待办；待办按根工作包、target_node_id、target_path 唯一，新发布替代旧待办。base 取该目标最近已确认 integrated 的 manifest，无则取根链第一条 job 的 inputs（不是当前修复 job 的 inputs）。心跳只向匹配目标节点发 integrations，generation 未对账时不下发；节点离线不影响 completed 与交付持久性。目标节点或管理员通过既有 integration 接口报告结果：integration_pending 保留待办，integrated 记录基线并清除待办，conflict 清除自动待办但保留 job 的冲突状态，交人工处理；均不改变 delivery_version。协调租约门禁保持有效。

§11 未规定已被新交付替代的旧结果处理。本实现拒绝会覆盖同根同目标新交付状态的旧结果（409 conflict），避免旧结果清除新待办或倒退基线。被替代旧 job 的历史 integration_status 保留，协议没有新增 superseded 状态。目标以 node_id 与原样 target_path 共同标识，不跨操作系统规范化路径。这里不承担节点的回写、备份、冲突检测和本地进度恢复，需与节点工作包联验。

SQLite 沿用通用 records 表，不改 SQL 表结构。启动时事务性补齐既有 job.acceptance 和 node.capability_evidence 缺省字段，同时补齐在途协调 context 的比较状态，并从旧触发记录及其捕获状态建立 rc5 去重键；重复启动幂等。不改历史不可变契约、请求哈希、幂等回执或版本号，不把历史 attempt 追认为能力证明。已有 integrated job/保留的 job.integration 事件可恢复旧回写基线；若旧基线及事件均已不可恢复，只能退回根 inputs。升级不自动补发 rc4 历史交付的回写待办，新发布后启用待办机制。备份/恢复自然包含新增 records，恢复后仍需 generation 对账。

## 备份、恢复与事件保留

```sh
python -m agent_collab.controller backup --data-dir /local/controller-data --dest /local/controller-backup
python -m agent_collab.controller restore --data-dir /local/controller-data --from /local/controller-backup
python -m agent_collab.controller prune-events --data-dir /local/controller-data --keep 10000
```

backup 可与 serve 并行，使用 SQLite backup API 取得一致快照，仅复制该快照引用且已存在的 blobs。缺失的 uploading 数据可以保留为缺失；durable/protected 数据缺失则备份失败。目的目录包含不可变 `controller-<db_sha256>.db`、blob 子目录、`backup-manifest.json`（database、db_sha256、blob hash/size 清单、authority_generation、created_at）。blob 增量复制，先完成新数据库，再原子切换清单；切换中断仍可使用上次清单。历史数据库和多余 blob 不自动删除。

restore 必须停止同目录 controller，校验清单、数据库和每个 blob 后，在数据目录暂存并原子替换数据库。generation 更新为 `max(当前,备份)+1`，旧租约全部撤销，旧活动任务变为 waiting_reconcile，取消中的任务保留 cancel_requested；所有派单暂停，旧 UI sessions 清除，节点先完成新 generation 心跳对账。主控核对工作区和副作用后，对可继续的 job 显式 retry，再解除 dispatch 暂停。当前目录的 admin.token 保留；恢复到新目录时下次 App 启动生成新管理员令牌。

恢复到原目录保留事件序号的高水位，新事件不会重用备份之后曾分配的 seq。恢复到全新目录无法知道备份之外的旧 seq，客户端应重新登录并获取快照/游标。备份数据库包含节点身份哈希等控制状态，备份目录须按状态目录保护；同机器目录备份不等于实例外备份。

prune-events 只裁剪事件内容，保留 event_id/hash 去重账本，防止迟到重发重复改变状态。旧游标收到 snapshot_required 后重新请求快照。没有后台 blob GC 或幂等/去重账本淘汰策略。

## 验证与限制

```sh
/usr/bin/python3 -m unittest discover -s tests
```

Mac 当前沙箱的 Xcode Python 启动器不能写系统临时目录，可运行：

```sh
mkdir -p .dev/tmp
TMPDIR="$PWD/.dev/tmp" /usr/bin/python3 -m unittest discover -s tests
```

所有测试临时目录明确使用仓库 `.dev/tmp` 并在结束清理。`tests/helpers.py` 的 FakeNode 仅通过 App.handle 配对、注册、心跳、领取、上传、检查点、完成和发布。测试覆盖事务故障注入、并发幂等、恢复与 fencing、共享主机/账户约束、发布 CAS、凭据隔离、备份中断/恢复、SSE 补发/HTTP 持续推送和断开。HTTP 使用内存文件对象验证 framing、上限、HEAD 与断开，没有打开 socket。

当前设计面向少量节点、低 QPS：调度器扫描内存中的记录，单进程 RLock 串行数据库事务；JSON 和单 blob 请求有大小上限，但按整个请求载入内存。SSE 不持有长数据库事务。未实现节点 agent、适配器、输入物化、UI 构建、installer、系统服务或实际部署（其他工作包负责）。真实 socket/TLS、Windows/WSL、整机重启、长时间运行及实例外恢复尚未验证。

静态 UI 若安装在 `agent_collab/static/index.html`，非 `/api/` GET 路径提供静态文件/SPA fallback；目录穿越被拒绝。没有 UI 时 `/` 返回简短说明。`doctor` 检查本地数据库/WAL、blob 目录可写标志、空间、时钟、公开源及最近备份年龄；它不是公网连通性或证书可信链测试。
