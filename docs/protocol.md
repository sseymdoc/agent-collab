# agent-collab 协议契约 v1

状态：首版契约。controller 与 node agent 都按本文实现，不各自改写语义；发现矛盾或缺口时先更正本文，再改实现与测试。共享常量、manifest 规范化与路径校验在 `agent_collab/protocol.py`，由两端共用。

约定：JSON UTF-8；时间为 UTC ISO-8601（`2026-11-09T03:48:38.123Z`）；ID 为小写 UUID v4，`node_id` 为 `[a-z0-9][a-z0-9-]{0,62}`；所有路径前缀 `/api/v1`。时间参数默认值见 `protocol.DEFAULTS`，均为候选值，不是实测保证。

## 1. 进程与可测试边界

- controller 核心是纯函数式入口：`App.handle(method, target, headers, body) -> Response`，`target` 含查询串，`headers` 为小写键字典，`body` 为 bytes。`Response` 有 `status:int`、`headers:list[(str,str)]`、`body:bytes` 或 `stream:iterator[bytes]`（SSE）。`http.server` 包装只做 socket 与 TLS，不含业务逻辑。测试不需要打开 socket 即可覆盖 API。
- controller 的时钟通过可注入的 `clock()` 获取（默认 `time.time`），租约过期、节点疑似失联均可在测试中推进时间。
- node agent 的网络访问经可注入 `transport(method, url, headers, body) -> (status, headers, body)`，默认实现为 `urllib`/`http.client` + `ssl`。
- 只用标准库，兼容 Python 3.9（Mac 系统 Python 3.9.6）。

## 2. 认证

| 主体 | 凭据 | 用途 |
| --- | --- | --- |
| 管理员 / CLI | `Authorization: Bearer <admin_token>`；首次启动生成在状态目录 `admin.token`（0600） | 全部客户端与管理操作 |
| 浏览器 UI | `POST /session {token}` 换取 `ac_session` Cookie（HttpOnly、SameSite=Strict，TLS 下 Secure）与 `csrf_token` | 读取与操作；变更请求必须带 `X-CSRF-Token`，且 `Origin` 为空或等于配置的 `public_origin`（loopback 时为实际监听源） |
| 节点 | 配对后得到 `node_token`；controller 只存其 SHA-256 | 仅节点端点；令牌绑定 node_id |
| 配对码 | `controller pairing-code --node-id X` 生成，一次性，10 分钟有效，只存哈希 | 只能调用 `/nodes/pair` |
| 客户端（桌面、CLI、Agent） | 客户端配对码换取的 `client_token`，见 §12.1 | 按授予的权限访问客户端端点 |

各类凭据互不通用；节点令牌不能调用客户端变更端点，UI 会话不能冒充节点。模型 provider 登录不经过本系统。`GET /healthz` 无需认证，只返回 `{"ok": true}`。

错误统一为 `protocol.error_body`：`{"error": {"code", "message", "details"}}`。`code` 取值：`invalid`(400)、`unauthorized`(401)、`forbidden`(403)、`not_found`(404)、`conflict`(409)、`fenced`(409，旧 epoch/旧令牌/旧 authority_generation)、`missing_blobs`(409)、`version_mismatch`(409，期望版本过期)、`too_large`(413)、`budget_exhausted`(409)。

## 3. 记录模型（API 返回形状）

**job**

```json
{"job_id", "request_id", "request_hash", "work_package_id", "title", "contract",
 "status", "status_reason", "protection", "inputs_manifest_sha256",
 "current_epoch", "current_attempt_id",
 "durable_checkpoint_id", "durable_checkpoint_at",
 "acceptance": {"status", "assessment_id", "checkpoint_id"},
 "delivery_version", "delivery_checkpoint_id", "integration_status",
 "counters": {"attempts", "infra_retries", "migrations", "repairs"},
 "cancel": {"requested_at", "confirmed_at"},
 "version", "created_at", "updated_at"}
```

- `version` 是行版本，每次状态变更 +1，供 `expected_version` 乐观并发。
- `delivery_version` 从 0 开始，每次成功发布 +1。
- `work_package_id` 缺省等于 `job_id`；同根表示同一交付目标的串行修订链，预算沿根累计（见 §6）。

**contract**（提交时给出，受理后不可变，参与 `request_hash`）

```json
{"goal": "str", "acceptance": ["str"], "excludes": ["str"],
 "harness": "stub|codex",
 "harness_params": {},
 "prompt": "str",
 "requirements": {"os": ["windows|linux|macos"], "arch": ["x86_64|arm64"],
                  "tools": ["python3"], "gpu": null, "labels": []},
 "inputs_manifest": <manifest|null>,
 "recovery": {"auto": true, "side_effects": "none|idempotent|manual"},
 "budgets": {"max_attempts": 3, "max_migrations": 2, "max_repairs": 2, "deadline_s": 3600},
 "resources": {"slots": 1},
 "publish": {"mode": "auto|manual", "requires_acceptance": false},
 "integration": {"target_node_id": null, "target_path": null}}
```

- `requirements` 中空列表表示不限；`tools` 为能力名，必须出现在节点 `capabilities.tools` 中。
- `harness_params`：`stub` 为 `{"steps": [...]}`（见 §9）；`codex` 为 `{"model"?, "effort"?, "sandbox": "read-only|workspace-write"}`：model 与 effort 可省略，受理时按 §12.4 解析成确定值写进契约，解析不出或不在许可范围时拒绝；字段约束见 §12.3。
- `recovery.auto=true` 要求 `inputs_manifest` 非空，且所有 blob 已存在于 controller，才能把 `protection` 置为 `protected`；否则为 `preparing`，**不得派单**，`status_reason` 列出缺失 blob。`recovery.auto=false` 时 `protection=unprotected`，可直接派单，但不会自动恢复。
- `side_effects=manual` 时永不自动恢复；`idempotent` 需要 `attempt.side_effect` 事件中的逻辑操作 ID（§7）。

**contract.workspace**：可选 `{"exclude": [glob, ...]}`；省略 workspace 或 exclude 时使用
`["__pycache__/**", "*.pyc", ".pytest_cache/**", ".DS_Store"]`，显式空列表关闭排除，自定义列表替换默认列表。
只接受 POSIX 相对 glob；空模式、绝对路径、盘符、反斜杠、控制字符，以及空、`.`、`..` 路径段在受理时返回 400。
匹配区分大小写，可从任意目录深度开始；`*`、`?`、`[...]` 匹配单个路径段，独立的 `**` 匹配零个或多个路径段。
例如 `build/**` 排除任意层级 build 目录中的文件，`*.pyc` 排除任意层级的 pyc 文件。
workspace 是不可变契约的一部分，提交值参与 request_hash；同 request_id 改变排除规则返回 409。
snapshot 不收录匹配的文件，也不把匹配的基线文件列入 deleted；materialize 仍完整恢复输入 manifest。
agent 回写时同样忽略匹配路径，避免把快照省略误当成对目标缓存文件的删除。

**attempt**

```json
{"attempt_id", "job_id", "epoch", "node_id", "status", "failure_class", "failure_detail",
 "recovered_from_attempt", "start_checkpoint_id", "start_manifest_sha256",
 "lease_expires_at", "authority_generation",
 "runtime": {"agent_version", "harness", "harness_version", "model", "effort", "observed"},
 "native_session_ref": null, "workspace": null, "report": null,
 "created_at", "leased_at", "started_at", "ended_at"}
```

- `epoch` 在同一 job 内严格递增；只有 `job.current_epoch` 的 attempt 拥有执行权与发布权。
- 新 attempt 必须在节点上使用独立工作区 `<agent_data>/attempts/<attempt_id>/work`（配置 `workspace_root` 时见 §11.4）。
- `observed` 只填执行端日志中实际读到的值；读不到时为 `null`，不用请求参数冒充。

**node**

```json
{"node_id", "physical_host_id", "failure_domain",
 "capabilities": {"os", "arch", "tools": {"name": "version|null"}, "gpu": [], "harness": {"codex": "0.167.0"}, "labels": []},
 "runtime_profile": {"user", "workspace_policy"},
 "resources": {"slots": 2, "host_slots": 2, "mem_mb": null},
 "provider_account_refs": {"codex": "不含凭据的账户分组名"},
 "auth_health": {"codex": "ok|expired|unknown"},
 "agent_version", "status", "drain", "last_seen_at", "version"}
```

- `capabilities.tools` / `capabilities.harness`：名称 → 版本字符串；找到但读不到版本时为 `"unknown"`；值为 `null` 或缺键都表示不可用。agent 内置的 `stub` 始终报告。`gpu` 为 `[{"name", "cuda": bool}]`；契约 `requirements.gpu` 为字符串时匹配 `name`，`"cuda"` 匹配任何 `cuda=true` 的设备。
- `auth_health`：只有 `expired` 会被调度排除；`unknown` 不排除。controller 因 `auth_expired` 失败标记的 `expired`，只能由节点报告 `ok` 或管理员处理来解除，节点的 `unknown` 不覆盖它。
- Windows native 与 WSL 节点报告同一 `physical_host_id` 与相同 `host_slots`；调度时同一物理主机上所有节点的活动 attempt 合计不超过该主机的 `host_slots`（多个节点报告不同值时取最小）。
- `provider_account_refs` 相同即视为共享额度：一处出现 `quota` 失败，所有使用该账户的节点对该 harness 均暂停接单（`quota_block_s`），不跨机无限重试。额度未知就是 `unknown`，不伪造剩余量。

**checkpoint**

```json
{"checkpoint_id", "job_id", "attempt_id", "epoch", "kind": "baseline|boundary|final",
 "state": "uploading|durable|verified", "manifest_sha256", "missing_blobs": [],
 "summary": {"completed": [], "pending": [], "failed_tries": [], "last_verification": null,
             "side_effects": [], "restore_hint": null},
 "native_session_ref": null, "created_at", "durable_at"}
```

**event**

```json
{"seq", "event_id", "job_id", "attempt_id", "node_id", "type", "payload", "stale", "created_at"}
```

`seq` 由 controller 全局单调分配，是 SSE 游标。

**publication**

```json
{"job_id", "delivery_version", "attempt_id", "epoch", "checkpoint_id", "manifest_sha256",
 "expected_base_version", "request_id", "created_at"}
```

## 4. 节点端点（Bearer node_token）

| 方法与路径 | 请求 | 响应 |
| --- | --- | --- |
| `POST /nodes/pair`（配对码，无需令牌） | `{"code", "node_id"}` | `{"node_id", "node_token", "controller_id", "authority_generation"}` |
| `POST /nodes/register` | node 记录中除 `status/drain/last_seen_at/version` 外的字段 | `{"node", "authority_generation", "config": {"heartbeat_s", "lease_ttl_s"}}`；幂等 upsert；`node_id` 必须与令牌一致 |
| `POST /nodes/{node_id}/heartbeat` | `{"authority_generation", "attempts": [{"attempt_id", "epoch", "state": "running\|finished_unreported\|unknown"}], "auth_health": {}}` | `{"server_time", "drain", "dispatch_paused", "renewed": [attempt_id], "revoked": [{"attempt_id", "reason"}], "cancel": [attempt_id], "reconcile_required": bool}` |
| `POST /nodes/{node_id}/claim` | `{}` | 200 `{"assignment"}`；无分配时 204 |
| `POST /attempts/{attempt_id}/events` | `{"epoch", "lease_token", "events": [{"event_id", "type", "at", "payload"}]}` | `{"accepted": [], "duplicates": [], "stale": bool}` |
| `HEAD /blobs/{sha256}` | — | 200 存在 / 404 |
| `PUT /blobs/{sha256}` | 原始字节 | 201 新建 / 200 已存在；内容哈希不符 400 且不落盘；超限 413 |
| `GET /blobs/{sha256}` | — | 原始字节（节点与客户端都可读） |
| `POST /attempts/{attempt_id}/checkpoints` | `{"checkpoint_id", "epoch", "lease_token", "kind", "manifest", "summary", "native_session_ref"}` | 200 checkpoint（`durable`）；409 `missing_blobs`（`details.missing`，记录为 `uploading`）；409 `fenced` |
| `POST /attempts/{attempt_id}/complete` | `{"epoch", "lease_token", "outcome": "succeeded\|failed\|cancelled", "failure_class", "failure_detail", "output_checkpoint_id", "report", "native_session_ref", "runtime"}` | attempt；相同请求重放返回同一结果，不同内容 409 |
| `POST /jobs/{job_id}/publish` | `{"request_id", "attempt_id", "epoch", "lease_token", "checkpoint_id", "expected_base_version"}` | publication 回执 |

`assignment`：

```json
{"attempt_id", "job_id", "work_package_id", "epoch", "lease_token", "lease_expires_at",
 "authority_generation", "contract", "delivery_version",
 "start": {"kind": "inputs|checkpoint|empty", "manifest_sha256", "checkpoint_id", "summary"},
 "recovered_from_attempt", "native_session_ref"}
```

`delivery_version` 是领取时 job 的当前交付版本，节点自动发布时作为 `expected_base_version`；不匹配时返回 409 `version_mismatch`，节点不自动重试，交给协调者。

规则：

1. **租约**：`claim` 把 attempt 从 `assigned` 变为 `leased`，签发随机 `lease_token`（controller 只存哈希）和 `lease_expires_at = now + lease_ttl_s`。心跳中列出的、epoch 当前且 token 未撤销的 attempt 被续租。
2. **fencing**：事件、检查点、完成和发布都校验 `(attempt_id, epoch == job.current_epoch, lease_token, authority_generation)`。不满足时：事件仍按 `event_id` 去重存档但标 `stale=true` 且不改变任何状态；检查点、完成、发布返回 409 `fenced`。旧 attempt 可以在本地继续存在，但不能更新权威记录。
3. **事件去重**：按 `event_id` 去重；相同 `event_id` 不同内容返回 409。晚到事件不会让 job/attempt 状态倒退。
4. **检查点提交**：controller 规范化 manifest（`protocol.validate_manifest`），manifest 本身也以 `canonical_json` 存为 blob。所有引用 blob 存在、且大小与 manifest 一致，才在**同一事务**中把 checkpoint 置为 `durable` 并更新 `job.durable_checkpoint_id/at`。缺 blob 时只登记 `uploading`。摘要先到、blob 半途、内容损坏（PUT 时哈希不符被拒）都不能成为 durable。相同 `checkpoint_id` 不同内容 409。`manifest.base` 若非空必须指向该 job 已存在的 manifest。
5. **完成**：`outcome=succeeded` 必须带 `kind=final` 的 durable `output_checkpoint_id`；job 进入 `completed`。`failed` 按 `failure_class` 进入恢复决策（§6）。`cancelled` 只在该 attempt 已有取消请求时接受，job 进入 `cancelled` 并记录 `cancel.confirmed_at`。
6. **发布 CAS**（唯一权威入口）：在一个事务中检查 attempt 为当前 epoch、未被撤销、状态为 `succeeded`；checkpoint 属于该 attempt 且 durable、`kind=final`；`expected_base_version == job.delivery_version`；若 `contract.publish.requires_acceptance`，该 checkpoint 必须有 `passed` 验收。全部满足才写 publication、`delivery_version += 1`、`delivery_checkpoint_id` 更新。相同 `request_id` 相同内容返回原回执；不同内容 409。`publish.mode=auto` 时节点在完成后立即调用；`manual` 时由客户端调用（客户端调用不需要 `lease_token`，但仍检查 epoch 与 attempt 状态）。
7. **authority_generation**：controller 元数据中的整数。恢复旧数据库备份时必须递增；此前签发的租约与 assignment 全部失效（返回 `fenced`，`details.reason="stale_generation"`）。心跳携带旧 generation 时返回 `reconcile_required=true`，节点先上报本地 attempt 状态，controller 对账后才发新租约。普通进程重启**不**递增 generation。
8. **控制服务重启**：启动时把所有活动租约的到期时间延长到 `max(原到期, now + lease_ttl_s)`，写 `controller.started` 事件，不重新派发已有 attempt。

**complete.report**：agent 在每次 complete 中提供 JSON 对象，经 `agent.clean` 移除已知节点/租约令牌，
以 canonical JSON 的 UTF-8 字节计不超过 65536 bytes。controller 原样保存 report（包括 usage 的 token 计数字段）；
显式非对象（null 除外）返回 400，超过上限返回 413；为兼容旧调用方，省略 report 字段或显式 `report=null` 仍允许。历史 report=null 不改写。

- stub：`{"harness":"stub","steps":[{"index":0,"op":"run","status":"done|skipped_from_checkpoint|failed","exit_code":0,"stdout_tail":"...","stderr_tail":"..."}]}`。
  steps 按 index 排序，记录实际执行/尝试及检查点跳过的步骤；未触及的步骤不伪造结果。非 run 或无退出码时 exit_code=null；run 两个输出尾部各 ≤4096 UTF-8 bytes。
- codex：`{"harness":"codex","final_message":"...","session_id":null,"usage":null,"cli_error":null}`。
  final_message 来自 result.txt 尾部，≤16384 UTF-8 bytes；其他字段取实际 CLI 事件，未知为 null。
- claude：`{"harness":"claude","decision":"accept|rework|escalate","notes":"..."}`。
  成功时取通过 schema 复核的 structured_output；无有效决策时 decision=null，notes 说明失败。

任何字段尾部或整体截断均加 `truncated=true`。整体仍超限时缩减最大的内容字段：steps 保留前段、文本保留尾部，
过大的 usage/cli_error 可变为 null；不把截断后的摘要当作完整证据。report 随 complete outbox 持久化，重放不重新采样日志。

## 5. 客户端端点（管理令牌、UI 会话或客户端凭据）

| 方法与路径 | 说明 |
| --- | --- |
| `POST /session` / `GET /session` / `DELETE /session` | 登录 / 刷新后凭 Cookie 取回 `csrf_token`（仅浏览器会话，令牌由服务端秘密从会话派生，不另存原值）/ 注销 |
| `GET /overview` | 各状态计数、节点摘要、最近 durable 检查点时间、controller 信息（版本、`controller_id`、`authority_generation`、部署 profile、`guarantees` 文本）、`dispatch_paused` |
| `GET /jobs?status=&limit=&before=` | job 列表 |
| `POST /jobs` | `{"request_id", "title", "work_package_id", "contract"}`；相同 `request_id` 相同内容返回原 job（200），不同内容 409；新建 201 |
| `GET /jobs/{id}` | job + `attempts`（谱系）+ `checkpoints` + `publications` + `assessments` + 最近 200 条 `events` |
| `GET /jobs/{id}/explain` | 当前调度解释：`[{"node_id", "eligible", "reasons": [], "score", "breakdown": {}}]` 与选中节点 |
| `POST /jobs/{id}/cancel` | `{"request_id", "expected_version"}`；未开始的直接 `cancelled`，执行中进入 `cancel_requested` |
| `POST /jobs/{id}/assess` | `{"assessment_id", "checkpoint_id", "status": "passed\|rework\|blocked\|unverified", "method", "result", "reviewer"}`；绑定 checkpoint 的 manifest 哈希 |
| `POST /jobs/{id}/retry` | `{"request_id", "expected_version", "reason", "exclude_node_ids": []}`；显式迁移/续接，计入 `migrations` 预算 |
| `POST /jobs/{id}/publish` | 同 §4，客户端发起 |
| `POST /jobs/{id}/integration` | 客户端或 `integration.target_node_id` 对应节点令牌均可调用：`{"request_id", "checkpoint_id", "status": "integration_pending\|integrated\|conflict", "detail": {"changed": [], "conflicts": [], "backup": null}}`；`checkpoint_id` 必须等于 `delivery_checkpoint_id`，否则 409。只记录回写结果，不改变交付版本 |
| `GET /checkpoints/{id}` / `GET /checkpoints/{id}/diff?against=<checkpoint_id\|inputs>` | 检查点与 `added/modified/deleted` |
| `GET /nodes` | 节点列表，含当前活动 attempt 数与最近一次拒绝派单原因 |
| `POST /nodes/{id}/drain` | `{"request_id", "expected_version", "drain": bool, "migrate": bool（可选）}` |
| `POST /dispatch` | `{"request_id", "paused": bool, "expected_version"}` 全局暂停派单；`expected_version` 为控制元数据版本 |
| `GET /events?after=<seq>&limit=` | 轮询事件 |
| `GET /events/stream` | SSE，见 §8 |
| `GET /doctor` | 自检：数据库可写与 WAL、blob 目录、可用空间、时钟、TLS/监听方式、公开源配置、备份年龄（未配置为 `unknown`） |

所有变更请求带 `request_id`（幂等键）与 `expected_version`（过期返回 409 `version_mismatch`，附当前记录）。幂等记录按 `(端点, request_id)` 保存请求哈希与响应：相同内容重放原响应，不同内容 409。重复点击不会重复派单。

**drain.migrate**：migrate 缺省 false，保持仅停止新领取的行为；drain=false 时忽略 migrate。
drain=true 且 migrate=true 时，逐个检查节点的活动 attempt：job 必须 protected、side_effects=none、具有 durable/verified
检查点或 inputs 基线、迁移/attempt/deadline 预算未耗尽、无取消或未对账副作用，且其他节点有符合要求的空闲容量。
沿用 retry 的协调租约门禁（需要时携带 X-Coordinator-Token）、调度能力/共享主机/额度/profile 规则。
满足条件时在同一事务中执行显式 retry：旧租约撤销、旧 attempt superseded，新 attempt epoch+1、recovered_from_attempt 指向旧 attempt，
从最近 durable 检查点（无则 inputs）在其他合格节点分配，attempts/migrations 各 +1，infra_retries 不增加。
旧 attempt 后续检查点/完成/发布被 fenced；节点从心跳获知撤销。这不迁移活进程或未上传的工作区改动。
响应保留原 node 字段，并增加 `not_migrated:[{"attempt_id","reasons":[原因码,...]}]`；未迁移者继续保有租约与运行状态。
常见原因：protection_not_ready、side_effects_not_none、missing_recovery_baseline、cancel_requested、side_effect_unknown、
coordinator_lease、budget_exhausted:migrations/attempts/deadline_s、no_eligible_node（附目标节点调度拒绝原因）。
若事务失败，排空标志、撤销、计数及新 attempt 一起回滚；相同请求重放原响应，不重复迁移，expected_version 仍针对 node。

## 6. 调度、故障分类与恢复

调度器在每次 claim/heartbeat 及后台 tick（1 秒）执行，纯函数在 `domain` 中：

1. **硬过滤**（原因码）：`dispatch_paused`、`draining`、`node_suspect`、`node_unreachable`、`os_mismatch`、`arch_mismatch`、`tool_missing:<name>`、`gpu_missing`、`harness_missing:<name>`、`auth_unhealthy:<harness>`、`account_quota_blocked:<ref>`、`no_node_slot`、`no_host_slot:<physical_host_id>`、`excluded_by_request`、`protection_not_ready`。
2. **软评分**（可解释加权和，权重在配置中）：剩余槽位比例、近期成功率、与源节点同物理主机（首次执行偏好就近）、恢复时偏好不同 `failure_domain`、冷启动/迁移成本。
3. 无合格节点：job 为 `waiting_capability`，`status_reason` 汇总各节点拒绝原因（例如缺 CUDA、缺 Xcode）。
4. 已运行的 attempt 保持粘性；只在故障、显式 drain/retry 时迁移。

attempt 结束或失联后的决策（`domain.recovery_decision`）：

| 情况 | 结果 |
| --- | --- |
| `user_cancel` 或 job 已 `cancel_requested` | 不恢复；确认后 `cancelled` |
| `code_failure` | job `failed`，不换节点 |
| `auth_expired` / `capability_missing` | job `waiting_capability`，原因写明；不重试其他共享账户的节点 |
| `quota` | 标记账户 `quota_block_s` 内阻塞；job `waiting_capability` |
| `node_lost` / `network` / `infra_retryable`，且 `recovery.auto`、`protection=protected`、`side_effects != manual`、预算未耗尽 | job `recovering` → 新 attempt：`epoch+1`，`recovered_from_attempt` 指向旧 attempt，`start` 取最新 durable 检查点，没有则取 inputs 基线；旧 attempt 置 `lost`/`superseded`，令牌撤销 |
| 其余（未受保护、副作用不明、预算耗尽） | job `waiting_reconcile` 或 `failed(budget_exhausted)`，`status_reason` 列出缺少的条件 |

租约过期即 attempt `lost`（`failure_class=node_lost`），节点 `last_seen` 超过 `suspect_after_s` 为 `suspect`，超过 `lease_ttl_s` 为 `unreachable`。长时间无输出但心跳正常不判故障。

预算按根工作包累计：`attempts`（每新建 attempt +1）、`infra_retries`（因可恢复失败新建）、`migrations`（换节点或显式 retry）、`repairs`（逻辑修复，由后续同根 job 计数）。任何一项超过 contract 中上限即不再自动新建 attempt。

旧节点回归：心跳列出的 attempt 若已非当前 epoch，返回 `revoked`；节点隔离该工作区（保留不删、不发布），可补传迟到事件（存档为 `stale`）。

## 7. 外部副作用

`attempt.side_effect` 事件 payload：`{"op_id": "绑定逻辑动作的稳定 ID", "state": "intent|done|unknown", "detail"}`。`op_id` 由逻辑工作包与动作决定，新 attempt 沿用同一 ID。检查点 `summary.side_effects` 复制最近状态。存在 `unknown` 状态的操作时，恢复决策为 `waiting_reconcile`，不盲目重放。

## 8. SSE

`GET /events/stream?cursor=<seq>`（或 `Last-Event-ID` 头，查询参数优先）。响应头 `Content-Type: text/event-stream`、`Cache-Control: no-cache`、`X-Accel-Buffering: no`。

- 每条事件：`id: <seq>`、`event: <type>`、`data: <event JSON>`。
- 每 `sse_ping_s` 秒发送注释行 `: ping`。
- 游标小于 `min_seq - 1`（即游标之后、已保留的第一条之前有事件已被裁剪）时，先发送 `event: snapshot_required` 与 `data: {"min_seq"}`，已被裁剪的事件无法补回，客户端依次：重新读取它依赖的权威状态（如 `GET /jobs/{id}`、`GET /overview`）；`GET /events?after=<min_seq-1>` 补读仍保留的事件；再用补读到的最后序号重新连接，补读为空时用 `min_seq - 1`（流按 `seq > cursor` 续读，直接用 `min_seq` 会漏掉第一条）；补读失败时保持原游标重连。
- 浏览器的 `Last-Event-ID` 只是游标传递方式，持久性来自数据库事件表。
- `App.handle` 的 SSE 响应是有限迭代器（测试可设置最大时长或在没有新事件时结束），HTTP 包装负责持续推送。

## 9. stub harness（确定性测试 worker）

`harness_params.steps` 顺序执行，每步一个对象：

| 步骤 | 行为 |
| --- | --- |
| `{"write": {"path", "content", "executable": false}}` | 在工作区写 UTF-8 文本 |
| `{"delete": {"path"}}` | 删除文件 |
| `{"sleep": 秒}` | 等待；配合故障注入 |
| `{"checkpoint": {"kind": "boundary", "completed": [], "pending": []}}` | 在静止边界提交检查点 |
| `{"progress": {"done": n, "total": m}}` | 发送可计数进度 |
| `{"side_effect": {"op_id", "state"}}` | 发送副作用事件 |
| `{"fail": "<failure_class>"}` | 以该分类失败结束 |
| `{"run": ["argv", ...]}` | 在工作区运行命令（无 shell），非零退出为 `code_failure`；argv 元素 `"{python}"` 替换为 agent 自身的 Python 解释器 |

从检查点恢复时，stub 依据 `start.summary.completed`（已完成步骤序号）跳过已完成的步骤，用于证明“从最近确认检查点继续”。

## 10. 协调权与受管协调者

确定性调度只恢复已受理的工作包；需求解释、验收判断、修复派单等语义决策由协调者完成。协调者分两类：`interactive`（用户当前 Claude 或控制台用户）与 `managed`（在幸存节点按事件启动的真实 Claude 回合）。

**协调租约**（按根工作包独占）：

| 方法与路径 | 说明 |
| --- | --- |
| `GET /packages/{work_package_id}/coordinator` | 当前租约：`{"holder_kind", "holder_ref", "expires_at", "version", "authority_generation"}` 或 `null`；`(authority_generation, version)` 唯一标识一次租约状态（数据恢复后 version 可能重新从 1 开始） |
| `POST /packages/{work_package_id}/coordinator` | `{"request_id", "holder_kind": "interactive\|managed", "holder_ref", "ttl_s", "takeover": false, "reason"}` → `{"coordinator_token", "expires_at", "version", "authority_generation"}`（按 request_id 重放升级前的旧回执时后两项为 `null`，表示未知）；他人持有有效租约时 409 `conflict`，除非管理员 `takeover=true` 且给出 `reason`（写 `coordinator.takeover` 事件）；同一持有者带 `X-Coordinator-Token` 时为续租 |
| `DELETE /packages/{work_package_id}/coordinator` | 带 `X-Coordinator-Token` 释放 |

存在有效租约时，针对该根工作包的 `assess`、`publish`、`retry`、`integration` 与同根新 job 提交必须带匹配的 `X-Coordinator-Token`，否则 409 `conflict`（`details.reason="coordinator_lease"`）。用户取消不受此限制。旧协调者恢复连接后先读租约与交付版本，不能凭旧令牌继续。

节点令牌发起、且 `publish.mode=auto` 的发布不受协调租约限制，仍执行全部 epoch、generation、attempt 和发布 CAS 检查。节点 integration 无此豁免。租约端点接受管理员 Bearer、由管理员登录换取的 UI 会话（仍要求 CSRF/Origin 校验）或带 `tasks` 权限的客户端凭据（§12.1）；节点令牌不能调用。`ttl_s` 必须是正有限数；接管必须给出非空 reason。租约 token 只保存哈希，GET 不返回 token；POST 获取回执支持 request_id 重放，续租重放仍须带原 token，已失效的获取回执返回 409，不复活旧租约。DELETE 在无有效租约时幂等返回 `{}`。普通重启保留租约，恢复备份后的 generation 变更使旧协调租约失效。

**受管协调者**：

- 配置 `managed_coordinator: {"enabled": bool, "profile_id": str}` 与 `standby_profiles: {id: {"harness": "claude", "model", "effort", "node_ids": []}}`，由用户事先填写。显式 `enabled=false` 时 status 为 `disabled`；缺失配置或必填字段、model/effort 为空、node_ids 为空或非法时为 `profile_missing`，不创建协调 job。完整启用配置为 `ready`，只表示配置完整，不代表 CLI 或认证已实测可用。不猜测模型或强度，也不回退到其他档位。
- 触发（缺省策略 always；on_failure 与 none 的条件见 §11.8，两者不合并）：job 进入 `completed`、`waiting_reconcile`，或收到 `rework` 验收，且该根工作包没有有效的 interactive 租约。controller 在该状态/验收事务内创建协调 job（`harness="claude"`，contract 内 `coordinates: {"job_id", "work_package_id"}`，`recovery.auto=false`，`requirements.tools=["claude"]`，节点限于 profile 的 `node_ids`）。按 §11.8 的去重键持久去重；同一目标已有未结束的协调 job 时不创建新的协调 job，被抑制的触发不排队、事后也不补发；领取时再次检查目标根没有有效租约。协调 job 使用独立 job/root 和执行预算，不计入目标根修复预算，publish 为 manual；客户端不能自行提交 claude/coordinates 契约。协调 job 自身的完成或失联不触发新的协调回合；协调回合应用的 rework 验收和 follow_up 属于同一决策，该验收不会再触发协调。
- 协调 attempt 领取时自动获得目标根的 managed 租约（holder_ref 为协调 attempt_id），随正常 attempt 心跳续租，完成、失败、取消确认、失联或显式 retry 撤销时释放。已被接管时旧 attempt 不得释放新持有者的租约，也不得应用决策。取消请求发出后，在确认/失联前仍保留租约。
- inputs_manifest 含 controller 生成的 `context.json`（目标 job 契约与状态、最新 attempt report、最新 durable checkpoint 相对 inputs 的 diff、验收记录）和 `decision-schema.json`；文件及 manifest 均通过 blob 提供。model/effort 与 node_ids 在触发时从配置取得快照。只响应新事件，不因配置启用或 interactive 租约过期而补发历史触发；不使用模型轮询心跳。
- **context.json 文本证据**：除上述字段外，还包含 `files:[{"path","content"}]` 和 `files_omitted:[{"path","reason"}]`。
  选择目标 job 最新的 durable/verified final 检查点，无 final 则选最新 durable/verified 检查点（按受理顺序，uploading 不参与）。
  files 按路径排序，仅包含可严格 UTF-8 解码的完整文件内容；单文件 ≤32768 bytes，累计 ≤262144 bytes，均按原文件字节计，非 JSON 转义后的字节。
  依次按 workspace 排除、单文件上限、UTF-8 解码、总预算判定，省略原因分别为 excluded、too_large、binary、budget；省略列表也按路径排序。
  无检查点时两列表为空。已有 report 仍为目标 job 最新 attempt 的报告，checkpoint_diff 仍对应 job.durable_checkpoint_id；
  文件集合优先 final，因此在 final 后又有 boundary 的情况下可能与 diff 对应检查点不同。验收仍绑定 context 捕获的 job.durable_checkpoint_id。
- 协调回合输出 `decision.json`（作为 final 检查点文件）：`{"decision": "accept|rework|escalate", "assessment": {"status", "method", "result", "checkpoint_id"}, "follow_up": {"title", "goal", "acceptance", "instructions", "effort"} | null, "notes"}`。所有列出的键必填；顶层、assessment、follow_up 对象均为 additionalProperties=false。method/result/notes/title/goal/instructions 为字符串，follow_up.acceptance 为非空字符串数组（minItems=1），follow_up.effort 为字符串或 null（§12.4；节点也接受此前 effort 必填、限 high/xhigh/max 的 schema）；不接受 contract、budgets、scope、base_checkpoint_id 等额外字段。adapter 使用同一 schema 作为 --json-schema 和本地形状复核依据，不做依赖目标状态的判断。controller 再检查 accept/status=passed、rework/status=rework，以及 checkpoint_id 必须指向 context 捕获的目标 durable checkpoint；escalate 可用 null checkpoint_id。非 rework 的 follow_up 应为 null，否则 controller 单独拒绝该修复请求。
- 修复契约由 controller 从请求和被验收检查点派生：harness=codex，harness_params 的 model 取根工作包冻结的 model，sandbox=workspace-write，effort 取 follow_up（为 null 时用冻结的 effort，规则见 §12.4）；goal/acceptance 取 follow_up，prompt 包含 instructions、goal、acceptance 及目标契约 excludes。excludes、requirements、recovery、resources、publish、integration、workspace 继承目标，requirements.tools 补入 codex 且不重复。budgets 继承目标的累计上限并保留取较小值的限制，不把上限改成剩余额度；follow_up 无权覆盖预算。inputs_manifest 必须等于被验收检查点的完整 manifest，包括 base/deleted。domain.validate_contract 验证结构，controller 仅对这种从同根 durable/verified 检查点派生的输入允许非空 base，普通客户端提交的非空输入 base 仍拒绝。修复成功受理后沿同根计 repairs+1。
- controller 在同一事务内确认 managed 租约仍属于该 attempt、目标 epoch/状态/检查点/验收/取消状态未改变且没有后续同根修订，然后应用决策；节点自动发布引起的 delivery/version 更新不使输入失效。合法 accept/rework 的验收先记录并绑定 checkpoint manifest，写 coordinator.decision_applied；follow_up 在独立保存点处理。修复请求形状错误、派生契约无效或派单失败时，仅回滚修复产生的状态/计数，写 `coordinator.follow_up_rejected`（含 reason）；超预算或根副作用未知写 `coordinator.escalated`。这些情况保留验收，协调 attempt 仍为 succeeded、协调 job 为 completed，完成请求重放不重复验收/派单。合法 rework 从 waiting_reconcile 创建修复前，将旧目标标为 failed(managed_rework)；创建失败时该状态变更也回滚，目标保留 waiting_reconcile 和已记录验收。follow_up=null 仅记录验收。显式 escalate 仍只记录事件、保持目标状态等待人工。验收自身非法或持有权/目标版本/检查点失效时，整次应用拒绝，写 `coordinator.decision_rejected`，不改变目标 job 和验收。
- 节点 claude 适配器使用用户本机已安装的官方 Claude Code 与其官方登录：`claude -p --model <profile.model> --effort <profile.effort> --output-format json --json-schema <内联 JSON> --permission-mode dontAsk --tools "" --session-id <新 uuid> --strict-mcp-config --setting-sources "" --safe-mode`，环境变量 `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`（禁用全部内置工具；不加载 MCP 服务，不加载 user、project、local 任何来源的设置、插件、hooks 和各级目录的 CLAUDE.md、CLAUDE.local.md，不加载自动记忆；Claude Code 自身的系统提示、官方登录与管理员下发的托管策略照常）。协调工作区里出现 `.claude` 时适配器不启动 Claude，以 `code_failure` 结束。prompt 经 stdin，内嵌 context.json 全文并要求仅返回结构化决策；follow_up 只需给修复目标、验收项、给 Codex 的修改说明及 effort，系统从被验收检查点自动生成完整契约。仅接受 stdout JSON 的 structured_output，通过 schema 后由 agent 写 decision.json 并建立 final checkpoint，不在 adapter 内生成或校验修复契约。含旧 contract 字段或缺字段的 follow_up 会在 adapter 端失败且不写 decision.json；若错误 follow_up 已通过其他客户端到达 controller，其合法验收仍可部分应用。不把自由文本 result 当作决策。认证失败分类 auth_expired；若此时本进程读不到凭据存储（macOS 钥匙串），改为 capability_missing，不报 auth_expired（§11.2）。Claude 不获写文件或执行命令权限；这是 Claude Code 的命令行限制，不是操作系统级隔离。没有工具时 Claude 只能依据 prompt 内嵌的 context：提示词要求只依据提供的证据判断，不得声称做过或看到未出现在 context 中的检查，证据不足或没有检查点时决策为 `escalate`。路径来自 agent.json 的 claude_command（默认 claude），detect 只在找到可执行文件后探测版本。适配器不读取、复制或代理 Claude.ai OAuth 凭据，不改变计费路径；每轮新建会话，不 resume 原生会话。

## 11. 执行环境、执行证据、协调策略与回写（rc5 起；向后兼容，旧字段语义不变）

**11.1 命令解析（agent）**：agent.json 可选键 `codex_command`（执行与 detect 共用）、`claude_command`、`workspace_root`、`disabled_harnesses`、`codex_windows_sandbox`、`auth_probe_s`。
- `*_command` 可为绝对路径字符串或 argv 列表（首元素为绝对路径）；未配置时按顺序查找：服务进程 PATH，然后 `~/.local/bin`；macOS 再查 `/opt/homebrew/bin`、`/usr/local/bin`。Windows 不排除 `.exe/.cmd` 之外的 PATHEXT 规则，仍排除 WindowsApps 应用执行别名。
- 解析出的绝对 argv 只计算一次，供 detect、doctor、auth 探测和 adapter 执行共用；探测与执行使用同一环境：服务环境，PATH 前置“解析出的可执行文件所在目录”。不读取 shell rc、不依赖交互终端。
- `capabilities.harness_paths: {"codex": "<abs>"|null, "claude": "<abs>"|null}`；`capabilities.tools.codex/claude` 与 `harness.codex/claude` 取自同一解析结果（版本字符串，失败为 null）。
- `disabled_harnesses: {"codex": "原因"}`：该 harness 报告 null，并在 `capabilities.harness_disabled` 中列出原因；用于在修复验证前撤销已知失败的能力。

**11.2 认证探测（agent，不调用模型）**：启动时及每 `auth_probe_s`（缺省 900）秒运行 `codex login status`、`claude auth status`（解析 JSON `loggedIn`），超时 20 秒。结果映射为 `auth_health[h] = ok|expired|unknown`（明确未登录为 expired，命令失败或超时为 unknown），随心跳上报。macOS 上 `claude auth status` 报未登录而当前进程读不到登录钥匙串时（如 SSH 会话），记为 unknown，不判定为 expired。输出只取状态，不记录账户标识或凭据。controller 合并规则不变（§3）。

**11.3 版本身份（agent 与 controller 共用）**：运行包版本从运行代码所在版本目录的 `release-manifest.json` 读取 `version` 与 `git_commit`，读不到时回退包内常量且 git_commit 为 null。node 的 `agent_version` 为该版本；`capabilities.agent = {"version", "git_commit"}`；attempt.runtime 含 `agent_commit`。

**11.4 工作区位置**：配置 `workspace_root` 时，attempt 工作区为 `<workspace_root>/<attempt_id>`（而不是 §3 的 `<agent_data>/attempts/<id>/work`），元数据、令牌、输出仍在 agent 数据目录。Windows 上若工作区位于用户配置文件目录（`%USERPROFILE%` 之下）且 codex 可用，doctor 报告 `workspace_in_profile` 警告：Codex Windows 沙箱身份无权列出用户目录，PowerShell 起始位置会退回驱动器根。

**11.5 Codex 执行参数**：argv 包含 `--ignore-user-config`（不加载用户 config.toml 中的 MCP 服务、插件、notify 钩子与默认档位；认证仍用 CODEX_HOME，不复制凭据），Windows 上再加 `-c windows.sandbox="<codex_windows_sandbox，缺省 elevated>"`。model、effort、sandbox 与 sandbox_workspace_write.* 也都显式传入。

**11.6 契约检查与执行证据**：contract 可选 `checks: [{"name", "run": [argv...], "timeout_s": 600}]`（≤16 项；name 唯一，匹配 `[A-Za-z0-9_.-]{1,64}`；run 为非空字符串数组，`{python}` 占位同 §9；timeout_s 为 1–3600 的数字）。参与 request_hash；修复契约继承目标的 checks。
- agent 在 harness 成功结束后（stub 全部步骤完成、codex turn completed 且退出码 0），在工作区内按序运行每项检查（无 shell、stdin 为空、服务环境），之后才建立 final 检查点。检查失败不改变 attempt outcome。
- `report.checks = {"manifest_sha256": <final 检查点 manifest 哈希>, "node_id", "platform": {"os", "release", "machine", "python"}, "results": [{"name", "argv", "exit_code": int|null, "timed_out": bool, "duration_s", "stdout_tail", "stderr_tail"}]}`（尾部各 ≤4KB，经脱敏）。harness 未成功时不运行检查，`report.checks` 为 null。

**11.7 验收证据门禁（controller）**：contract.checks 非空时，任何来源（受管协调者、管理员、UI 会话）的 `passed` 验收，都要求产生被验收检查点的 attempt 的 `report.checks.manifest_sha256` 等于该检查点 manifest 哈希，且每项必需检查都存在、`exit_code == 0`、`timed_out == false`。不满足时验收记录仍保存，但 `status` 改为 `unverified`，并附 `requested_status: "passed"` 与 `evidence_gaps: [{"check", "reason": "missing|failed|timed_out|manifest_mismatch|no_report"}]`；写 `job.evidence_gap` 事件。满足时验收记录附 `evidence: {"attempt_id", "manifest_sha256", "checks": [name...]}`。job.acceptance 透出 `requested_status`、`evidence_gaps`。`publish.requires_acceptance` 仍只认 passed。

**11.8 任务级协调策略（controller）**：contract 可选 `coordination: {"policy": "always|on_failure|none"}`，缺省 always（即 §10 所述的触发行为）。on_failure 只在目标进入 `waiting_reconcile`、`failed`、`waiting_capability`，或以 completed 结束但交付 attempt 带 `recovered_from_attempt`（发生过接管）时触发，`rework` 验收不触发（即使目标当时处于上述状态）；none 从不触发受管协调。修复契约继承目标的 coordination。
- 触发去重键为 `目标 job_id + 触发类型 + durable_checkpoint_id + acceptance.status`；同一目标已有未结束的协调 job 时不再创建新的。取消请求或根预算耗尽后不派生修复（已有 budget_reason 与 cancel 检查须覆盖受管修复路径）。

**11.9 能力证据（controller）**：node 记录包含 `capability_evidence: {"<harness>": {"status": "ok|degraded|unproven", "bound": {"agent_version", "harness_version", "harness_path"}, "last_success": {"attempt_id", "at"}|null, "last_failure": {"attempt_id", "at", "reason"}|null}}`。
- 每个 attempt（含 stub 与协调 job）结束时更新该 attempt 所用 harness：succeeded 且（无 checks 或检查全过）记为 success；succeeded 但检查失败/缺失记为 failure(`checks_failed`)；其他失败记为 failure(failure_class)，`user_cancel`、`node_lost` 不计。
- bound 与节点当前上报值不同则视为 `unproven`（旧证据不沿用）；否则最新一条为 success 时 `ok`，为 failure 时 `degraded`。仅用于展示与解释，不改变调度过滤（撤销能力用 11.1 的 disabled_harnesses）。

**11.10 源目录回写与执行节点解耦**：contract.integration.target_node_id 非空时，回写由该目标节点完成，不要求它是执行节点。
- 交付发布成功后，controller 为该根工作包登记待回写项（每个根工作包同一目标只保留最新一项，旧的待办被新交付替代）：`{"job_id", "checkpoint_id", "manifest_sha256", "base_manifest_sha256", "target_path", "exclude"}`；base 取同根同目标最近一次 `integrated` 的 manifest，没有则取根 job 的 inputs_manifest。job.integration_status 置 `integration_pending`。
- 心跳响应包含 `integrations: [...]`，只发给 target_node_id 对应节点；节点用既有 `integrate()` 回写（只检查本次改动路径、先备份、冲突不覆盖），在本地数据目录 `integrations/<job_id>/<checkpoint_id>/` 持久化进度，结果经既有 `POST /jobs/{id}/integration` 上报。已开始但结果未知的回写不重复执行，上报 `conflict` 并要求人工复核。
- 目标节点离线时 job 仍为 completed、交付与工件保存在 controller，integration_status 保持 `integration_pending`；节点恢复后按上述基线检查再回写。

**11.11 Claude 协调说明语言**：claude 适配器提示词要求 method、result、notes、follow_up 的 title/goal/acceptance/instructions 使用简体中文；结构化枚举值与字段名不变。

## 12. 客户端授权、模型策略与统一入口（向后兼容；旧字段语义不变）

桌面端、CLI 与其他自动化客户端共用本节的端点和授权模型。controller 在 `GET /client`、`GET /overview` 的 `controller.features` 和节点注册响应的 `features` 中列出已支持的能力名：`clients`、`model_settings`、`node_models`、`contract_draft`、`baselines`、`catalog_provenance`，以及 §13 的 `projects`、`applications`、`node_admin`、`coordination_settings`、`raw_logs`、`remote_apply`。客户端和节点只在对端列出相应能力后使用对应字段。

**12.1 客户端凭据**

| 主体 | 凭据 | 用途 |
| --- | --- | --- |
| 客户端（桌面、CLI、Agent） | `Authorization: Bearer <client_token>`；配对时签发一次，controller 只存 SHA-256 | 按授予的权限访问客户端端点；与节点令牌、管理令牌互不通用，可单独撤销 |
| 客户端配对码 | `controller client-code --data-dir D [--role R \| --scopes a,b] [--ttl 600]`（controller 本机），或 `POST /clients/codes`；一次性，只存哈希，缺省 10 分钟、最长 1 小时 | 只能调用 `POST /clients/pair` |

权限（scope）：

| scope | 允许 |
| --- | --- |
| `read` | 除 `GET /clients`、`GET /doctor`、`/maintenance/*`、`GET /settings/backups`（需要 `maintain`）外的全部 GET：任务、事件与事件流、检查点与文件清单、blob、节点、总览、调度解释、协调租约、模型设置与目录、模板、输入基线 |
| `tasks` | 创建任务、契约草拟、取消、重试、验收、发布、回写结果、协调租约取得与释放、上传 blob、登记输入基线 |
| `settings` | 修改模型设置 |
| `devices` | 节点排空与迁移 |
| `maintain` | 暂停与恢复派单、doctor、客户端列表与撤销、签发客户端配对码、接管协调租约、§14 备份维护与预检、备份保留设置 |

角色是 scope 的固定组合：`owner` 为全部五项，`operator` 为 `read`+`tasks`，`viewer` 为 `read`。权限在每个路由上由 controller 检查；缺少权限返回 403 `forbidden`，`details.required_scope` 给出所需权限。管理令牌与浏览器会话等同于全部权限。节点令牌不能调用客户端端点。

- `POST /clients/pair`（无需认证）`{"code", "name", "kind": "desktop|cli|agent"}` → 200 `{"client_id", "client_token", "name", "kind", "scopes", "controller_id", "authority_generation", "controller": {"version", "git_commit"}, "features"}`。配对码用后即废；`client_token` 只在这次响应里出现。
- `GET /client`（任意客户端凭据、管理令牌或会话）→ `{"identity": {"kind": "client|admin|session", "client_id", "name", "scopes"}, "controller": {"controller_id", "authority_generation", "version", "git_commit", "features"}}`。
- `DELETE /client`（客户端凭据）→ 撤销调用者自己。
- `GET /clients`（maintain）→ `{"clients": [client...]}`；client 为 `{"client_id", "name", "kind", "scopes", "created_at", "revoked_at", "version"}`，不含凭据。
- `POST /clients/codes`（maintain）`{"request_id", "role" | "scopes", "ttl_s"}` → 201 `{"code", "scopes", "expires_at"}`；响应不进入幂等回执，重复的 request_id 返回 409。客户端凭据只能签发不超出自身 scopes 的配对码，否则返回 403 `forbidden`，`details.required_scope` 为第一个缺少的 scope；管理令牌与会话不受此限。
- `POST /clients/{client_id}/revoke`（maintain）`{"request_id", "expected_version"}` → 更新后的 client。

撤销从下一次请求起生效，不影响节点凭据和其他客户端。已建立的事件流在下一个轮询周期（缺省 0.25 秒）重新认证：凭据已撤销或失效时收到一帧 `event: unauthorized`，凭据有效但失去 `read` 权限时收到一帧 `event: forbidden`，随后被服务端关闭。

401 与 403 是两种情况，客户端分开处理。401（`unauthorized`）表示凭据失效或已撤销，客户端停止使用这条连接并提示重新配对。403（`forbidden`）表示凭据有效但没有这项操作的权限：连接与本机凭据保留，已授权的读取照常可用，界面说明缺少的权限（`details.required_scope`）。

所有 `/api/v1` 响应带 `X-Controller-Id: <controller_id>`。客户端把连接绑定到配对时得到的 controller_id；响应里的值不同即停止使用该连接。不带该头的 5xx 视为前置代理代答，按网络错误处理，不算身份不符。

浏览器控制台（`POST /session`、Cookie、CSRF、Origin 检查与静态页面）在配置 `web_console` 为 true（缺省）时保持原行为；设为 false 后 `POST /session` 返回 403，不再提供静态页面，已有会话失效。它是迁移期的兼容入口，退出条件见 `docs/migration.md`。

**12.2 请求来源**：新建的 job 记录带 `created_by: {"kind": "client|admin|session|controller", "client_id", "name"}`（旧记录没有此字段）。每个带 `request_id` 幂等回执的客户端变更请求在实际执行时（重放不计）写一条 `audit.action` 事件：`{"actor": {...同上}, "action": "<METHOD> <路由>", "request_id"}`，与任务有关的记在该任务名下。签发配对码同样写这条事件；模型设置的修改记在 `settings.updated` 与修改历史里，客户端配对与撤销记为 `client.paired`、`client.revoked`。

**12.3 模型设置**（controller 保存，记录 `settings/models`）

```json
{"revision": 0, "updated_at": null, "updated_by": null,
 "codex": {"default": {"model": null, "effort": null}, "allowed": null, "unverified_nodes": "wait"},
 "claude": {"default": {"model": null, "effort": null}}}
```

- `codex.default`：执行任务的工作区默认值；null 表示未设置。
- `codex.allowed`：部署策略。null 表示部署不作限制；否则为 `{"<model>": ["<effort>", ...]}`，只有列出的组合可用，默认值必须在其中。
- `codex.unverified_nodes`：模型目录未核实（未知或已过期，见 12.5）的节点能否承接 codex 任务。`wait`（缺省）只派给目录已核实含该组合的节点；`allow` 是显式开启的“未核实能力尝试”，仍优先已核实的节点。`PUT` 的 `codex` 里省略这一项时保持原值。
- `claude.default`：受管协调回合的模型与 effort；null 时沿用 `config.json` 中 `standby_profiles` 所选 profile 的值。两组设置互不覆盖。协调是否启用与候选节点见 §13.7。
- model 匹配 `[A-Za-z0-9][A-Za-z0-9._:/-]{0,95}`，effort 匹配 `[a-z][a-z0-9_-]{0,31}`。
- 新建的 controller 数据库从上述未设置状态开始。已有任务或节点记录、但没有该记录的数据库（升级前的部署）在启动时写入一次迁移值：`codex.default = gpt-6-astra / xhigh`，`codex.allowed = {"gpt-6-astra": ["high", "xhigh", "max"]}`，`claude.default` 取当时生效的 standby profile；`updated_by.kind` 为 `migration`。之后与其他设置一样可编辑。
- 优先级为 任务覆盖 > 项目默认 > 工作区默认；项目默认值见 §13.1。部署许可范围（`codex.allowed`）只有工作区一层。

端点：

- `GET /settings/models`（read）→ `{"settings", "catalog", "selectable", "history"}`。
  - `catalog.nodes.<node_id>.<harness>` = `{"status": "ok|stale|unknown|legacy", "state", "source", "fetched_at", "cli_version", "provider", "reason", "models": [{"id", "efforts": [...], "default_effort"}]}`，见 12.5。`status` 是节点上报的值，`state` 是按有效期算出的当前状态（多一个取值 `expired`）；`catalog.max_age_s` 是有效期。已退役的节点不在其中。
  - `selectable.codex` = `[{"model", "efforts": [...], "nodes": [node_id...]}]`：部署策略与已核实的节点目录（state 为 ok、stale 或 legacy）的交集；`selectable.unknown_nodes` 列出目录未核实（unknown 或 expired）的节点。目录条目来自节点上的 CLI，不表示该账号已成功调用过这个模型。
  - `history`：最近 20 次修改 `{"revision", "at", "actor", "before", "after"}`。
- `PUT /settings/models`（settings）`{"request_id", "expected_revision", "codex"?, "claude"?}` → 更新后的 settings；`request_id` 必填，相同内容重放返回第一次的应答（不再递增 revision），同一 `request_id` 换了内容返回 409 `conflict`；`expected_revision` 与当前不同返回 409 `version_mismatch`，`details.current` 为当前 settings。写 `settings.updated` 事件。

**12.4 受理时解析与冻结**

- `harness: codex` 的契约可以省略 `harness_params.model` 与 `effort`。受理时逐项解析：请求里显式给出的值 → 工作区默认值；同一根工作包内的后续修订改用根冻结的默认值。解析不出返回 400，`details.reason` 为 `model_default_unset`；解析结果不在许可范围返回 400，`details.reason` 为 `model_not_allowed`。同一根工作包的后续修订若显式给出 model，必须与根冻结的 model 相同（冻结的 model 为 null 时不限），否则同样返回 400 `model_not_allowed`；effort 可在冻结的许可范围内覆盖。不会换成别的模型或 effort。
- 解析结果写入 job 的 `contract.harness_params`（与此前由固定常量填充的位置相同），受理后不变；之后修改设置不影响已受理的 job，包括排队中的。
- job 记录带 `model_policy`：`{"schema": 1, "settings_revision", "requested": {"model", "effort"}, "resolved": {"model", "effort"}, "source": {"model", "effort"}, "unverified_nodes"}`，属于项目的任务再带 `project_revision`。`requested` 是请求显式给出的值（没有则为 null）；`source` 逐项取 `task_override`、`project_default`、`workspace_default`、`root_frozen`、`coordinator_override`、`legacy_compat`。`unverified_nodes` 是受理时生效的派单策略（12.5），与模型一起冻结。升级前受理的 job 没有此字段；`request_hash` 与既有契约内容不重算。
- 根工作包记录带冻结策略 `model_policy`：`{"schema": 1, "settings_revision", "codex": {"model", "effort", "allowed": {...}|null, "unverified_nodes"}}`，属于项目的根再带 `project_id`、`project_revision`。取根 job 受理时的解析结果与当时的 `codex.allowed`；根 job 不是 codex 时取当时的默认值（项目默认优先于工作区默认，未设置则 model、effort 为 null）。
- 受管修复只读根的冻结策略：模型固定为冻结的 model；`follow_up.effort` 为 null 时用冻结的 effort，给出值时必须在冻结的许可范围内（`allowed` 为 null 时不限），并在子 job 的 `model_policy.source.effort` 记 `coordinator_override`。不在范围内时拒绝该修复请求（`coordinator.follow_up_rejected`，原因 `effort_not_allowed`），验收结论保留。冻结的 model 为 null 时修复请求以 `model_default_unset` 被拒绝。根工作包的累计预算规则不变。
- 升级前创建的根没有冻结策略，读取时按原行为推导：model 与 effort 取根 job 的 codex 参数（根不是 codex 时为 `gpt-6-astra` / `xhigh`），`allowed` 为 `{"<model>": ["high", "xhigh", "max"]}`，来源记 `legacy_compat`。
- 协调决策 schema 的 `follow_up.effort` 为 `string | null`，不带枚举；`context.json` 增加 `repair_policy: {"model", "effort", "allowed_efforts"}` 供协调者参考。节点同时接受此前的 schema（effort 必填、枚举 high/xhigh/max），以便先升级节点、后升级 controller。
- 受管协调回合在创建时读取当时的 `claude.default`（或 profile 值）并写入自己的契约。

**12.5 节点模型目录**

- 节点注册可带 `capabilities.models`：`{"codex": {"status": "ok|stale|unknown", "source", "fetched_at", "cli_version", "provider", "reason", "models": [{"id", "efforts": [...], "default_effort"}]}, "claude": {...}}`。codex 目录由节点在自己的服务进程上下文里启动 `codex app-server`（stdio，不监听网络端口），发送 `initialize`、`config/read` 与 `model/list` 取得，按 `model_catalog_s`（缺省 21600 秒）刷新，每次探测后重新注册；取不到时 `status` 为 `unknown` 并给出 `reason`。claude CLI 没有目录接口，`status` 为 `unknown`、`reason` 为 `no_catalog_interface`。读取目录不调用模型。
- 探测与执行使用同一个模型 provider。执行用 `codex exec --ignore-user-config`，不读 `$CODEX_HOME/config.toml`；`app-server` 没有这个选项，会读用户配置。因此两边的命令行都显式带 `-c model_provider="openai"`，探测再用 `config/read` 读回生效的 `model_provider`：读不回记 `provider_unverified`，与执行用的不一致记 `provider_mismatch`，两种情况都不采用这份目录。认证两边都取自同一个 `CODEX_HOME`。`provider` 字段记下目录对应的 provider。
- 刷新失败时，节点沿用最近一次成功的目录并标为 `stale`：`fetched_at` 保持那次成功的时间，`reason` 是这次失败的原因；前提是 CLI 版本与 provider 都没有变，否则上报 `unknown`。最近一次成功的目录保存在节点数据目录的 `model-catalog.json`，重启后仍可沿用。
- 有效期：`fetched_at` 早于 `catalog_max_age_s`（controller 配置，缺省 172800 秒）的 `ok` 或 `stale` 目录按 `expired` 处理。`ok`、`stale`、`legacy` 是“已核实”，`unknown`、`expired` 是“未核实”。
- `provider` 字段与 `stale` 状态只在注册响应的 `features` 含 `catalog_provenance` 时发送；否则节点去掉 `provider`，把沿用的旧目录按 `unknown` 上报。
- 节点第一次注册不带 `models`；注册响应的 `features` 含 `node_models` 时再带上它重新注册。节点与 controller 断线重连后重新注册，以重新取得 `features`（controller 原地升级时恢复代次不变，心跳不会提示能力变化）。
- 没有上报 `models` 的节点（旧版本）按其适配器实际接受的固定集合处理：`status: legacy`，`gpt-6-astra` 配 `high`、`xhigh`、`max`。
- 调度过滤（§6）：对已核实的目录，codex 任务的 model 不在其中记 `model_unsupported:<model>`，effort 不在该模型的列表里记 `effort_unsupported:<model>/<effort>`。目录未核实不等于不支持，也不等于支持：任务的 `model_policy.unverified_nodes` 为 `wait`（缺省）时该节点记 `model_catalog_unverified:<unknown|expired>`、不派单，任务没有别的合格节点就停在 `waiting_capability`；为 `allow` 时该节点可以承接，但排在所有目录已核实含该组合的节点之后。调度解释里每个节点带 `model_catalog`（当前状态）与 `unverified_attempt`（这次派单是否属于未核实能力尝试），顶层带 `policy.unverified_nodes`。

**12.6 模板、契约草拟与输入基线**

- `GET /templates`（read）→ `{"templates": [{"id", "title", "description", "harness", "diagnostic"}]}`。当前模板：`codex-change`（Codex 修改并运行检查；失败或接管时才触发协调，结果自动发布）、`codex-change-accept`（执行并验收：`publish = {"mode": "manual", "requires_acceptance": true}`、`coordination.policy = always`，验收通过后由客户端发布）、`diagnostic`（确定性步骤，不调用模型，用于检查 controller、节点、检查与工件链路）。
- `POST /contracts/draft`（tasks）`{"template", "title"?, "goal", "acceptance"?: [...], "excludes"?: [...], "baseline_id"? | "inputs_manifest"?, "checks"?: [{"name", "run", "timeout_s"?}], "model"?, "effort"?, "requirements"?}` → `{"title", "contract", "preview", "model_policy", "settings_revision", "warnings"}`。只生成并校验契约，不改变状态。`contract` 是可原样用于 `POST /jobs` 的请求（只含显式选择的 model、effort）；`preview` 是按当前设置解析并补全默认值后的完整契约，`model_policy` 是对应的解析结果。`warnings` 为 `[{"code", "detail"}]`：`empty_inputs`（没有输入基线）、`missing_blobs`（基线有文件未上传）、`no_node_supports_model`（所有节点的目录都已核实且都不含该组合）、`no_verified_node_supports_model`（没有已核实的目录含该组合，另有目录未核实的节点，策略为 `wait`）、`unverified_node_attempt`（同上但策略为 `allow`，任务可能派给目录未核实的节点）。请求可带 `project_id`（§13.1），响应相应带 `project_id`、`project_revision`、`baseline_id`。
- `POST /jobs` 可带 `expected_settings_revision`：与当前模型设置的 revision 不同时返回 409 `version_mismatch`（`details.current` 为当前设置），用于保证受理时的解析结果就是草拟时看到的那一份。属于项目的任务再带 `expected_project_revision`（§13.1）。
- `POST /jobs` 的幂等回执先于上述版本核对：同一 `request_id`、同一内容的重放返回第一次受理的 job（当时冻结的 `model_policy` 不变），即使之后设置已经改变；重放照常先通过凭据与权限检查。
- `GET /jobs` 可带 `work_package_id=<根工作包>`，只列出该根的各次修订（新的在前）。
- `POST /baselines`（tasks）`{"request_id", "name", "manifest"}` → 201 baseline；`GET /baselines`（read）→ `{"baselines": [...]}`。baseline 为 `{"baseline_id", "name", "manifest_sha256", "files", "bytes", "missing_blobs", "created_at", "created_by"}`；manifest 的 `base` 必须为 null，文件内容经 `PUT /blobs/<sha256>` 上传。基线及其 blob 计入备份。

**12.7 读取补充**

- `GET /events?before=<seq>&limit=<n>`（read）→ `{"events": [...], "min_seq"}`：序号小于 `before` 的最近 `limit` 条全局事件，按序号升序；`before=0` 表示从最新一条开始。用于从新到旧翻看活动。不带 `before` 时仍是 `after=<seq>` 的向后续读。
- `GET /jobs/{id}/events?before=<seq>&limit=<n>`（read）→ `{"events": [...]}`：该 job 序号小于 `before` 的事件，按序号升序，最多 `limit` 条（1–1000，缺省 200）。
- `GET /nodes` 的每个节点带 `attempts`：该节点上有效（assigned、leased、running）的 attempt，`[{"attempt_id", "job_id", "title", "status", "epoch", "lease_expires_at", "harness", "model", "effort", "coordinates"}]`；`coordinates` 为 true 表示它是受管协调回合。
- `GET /nodes`（包括 `retired=1`）的每个节点带整数 `pending_writebacks`：该节点状态为 `requested/received` 的 `project_apply/project_recover` 操作，加上 `target_node_id` 为该节点且状态为 `started` 的应用，按 `application_id` 去重；再加上 `target_node_id` 为该节点、仍有待写入条目（`pending`）的契约自动回写，每个任务算一项。同一应用的写入与恢复操作只算一项；没有应用 ID 的操作各算一项。除上述两类以外的 `node_op` 种类、已结束操作、本机应用不计入。
- `GET /checkpoints/{id}/files`（read）→ `{"checkpoint_id", "manifest_sha256", "files": [{"path", "sha256", "size", "executable"}]}`；文件内容用 `GET /blobs/<sha256>` 读取。

## 13. 项目、输入与成果应用、节点管理（向后兼容；旧字段语义不变）

**13.1 项目**

```json
{"project_id", "name", "revision",
 "defaults": {"codex": {"model": null, "effort": null, "unverified_nodes": null}, "exclude": null},
 "baseline": {"baseline_id", "manifest_sha256", "files", "bytes", "at"} | null,
 "applied": {"application_id", "job_id", "checkpoint_id", "manifest_sha256", "device", "finished_at"} | null,
 "archived_at", "created_at", "created_by", "updated_at", "updated_by", "jobs", "active_jobs"}
```

- `defaults` 的每一项为 null 表示沿用工作区的值。`codex.model` 与 `effort` 成对给出或都不给，且必须在部署许可范围内；`exclude` 是项目的排除规则（POSIX 相对 glob），用于执行节点的检查点与成果应用，草拟时写进 `contract.workspace.exclude`。不论排除规则如何，应用成果时都不写 `.git/`、`.hg/`、`.svn/` 下的路径（版本库元数据里有会被执行的钩子）。
- `revision` 是配置版本：名称、默认策略或归档状态每改一次加一，并留下历史（`GET /projects/{id}` 的 `history`，最近 20 次）。登记输入基线与应用成果不改变 `revision`。
- 本机目录不在 controller 上。项目对应哪台设备上的哪个目录，是那台设备上的绑定（桌面端的 `projects.json`、CLI 的 `<配置目录>/projects.json`），每台设备各有各的；controller 只在基线的 `source` 里保存目录名等说明。
- 优先级：任务显式给出的值 > 项目默认值 > 工作区默认值，model 与 effort 逐项判断来源（`model_policy.source`）。受理时冻结：job 带 `project_id`、`project_revision`，`model_policy` 带 `settings_revision`、`project_revision`、解析结果与来源；根工作包的冻结策略带 `project_id`、`project_revision`。之后修改项目默认值或工作区设置不影响已受理的任务（包括排队中的）和它们的修复。同一根工作包的修订属于根所在的项目，换项目返回 400。
- 归档的项目不接受新的基线、草拟与任务（409 `project_archived`）；它的任务、基线、应用记录都保留并可读，可以取消归档。没有删除项目的端点。

端点（`projects` 能力）：

- `GET /projects`（read）→ `{"projects": [...]}`，含已归档的。`GET /projects/{id}`（read）→ 项目加 `history`。
- `POST /projects`（tasks）`{"request_id", "name", "defaults"?}` → 201。`PUT /projects/{id}`（tasks）`{"request_id", "expected_revision", "name"?, "defaults"?, "archived"?}` → 更新后的项目；`defaults` 整体替换。`expected_revision` 与当前不同返回 409 `version_mismatch`（`details.current`）。改动 `defaults` 另需 `settings` 权限。
- `POST /contracts/draft` 与 `POST /jobs` 可带 `project_id`。草拟返回 `settings_revision` 与 `project_revision`；提交带 `expected_settings_revision` 与 `expected_project_revision`，任一与当前不同都返回 409 `version_mismatch`。草拟没有另给输入时用项目最近登记的基线，响应的 `baseline_id` 给出实际所用的那一份。
- `GET /jobs?project_id=<id>`、`GET /baselines?project_id=<id>` 按项目筛选。

**13.2 输入基线与本机快照**

- `POST /baselines` 可带 `project_id` 与 `source`（≤16 KiB 的说明对象：设备名、目录名、Git 分支与未提交改动数、排除规则、被排除与被跳过的条目数、扫描时间）。baseline 记录相应带这两项；项目的 `baseline` 指向最近登记的一份。重复的 `request_id`（请求体相同）返回第一次登记的那份基线（状态与第一次相同）；请求体不同返回 409。
- 客户端把一次扫描当作一次登记操作：`request_id` 由这次扫描的身份派生（CLI 的扫描 ID、桌面端的快照 ID），不由内容或扫描时间派生；请求体在第一次发出之前存在本机（CLI 的 `requests/<request_id>.json`，桌面端的快照），恢复时原样重放。内容相同的两次扫描是两次登记，文件内容按哈希复用。
- `POST /blobs/missing`（tasks）`{"sha256": [...]}`（≤20000 项）→ `{"missing": [...]}`：controller 还没有的内容。客户端只上传缺的（`PUT /blobs/<sha256>`），上传可以中断后重来；重复上传已有的内容返回 200，不产生新对象。
- `GET /client` 的 `controller.limits` = `{"max_blob_bytes", "max_json_bytes"}`：单个文件与单次请求的上限。文件清单随 `POST /baselines` 内联提交，受 `max_json_bytes` 限制（缺省 2 MiB，约一万余个文件）。
- 输入清单与它引用的全部内容到齐之前，启用自动恢复的任务处于 `protection: preparing`，不派单，`status_reason` 带 `protection_not_ready;missing_blobs:<sha256,...>`；到齐后变为 `protected` 并可被领取。提供输入的设备在内容到齐后不需要保持在线，也不必是执行节点。
- 本机快照由 `agent_collab/project.py` 生成，CLI 与桌面端共用：
  - 只读选定目录之内的普通文件。符号链接与 Windows 重解析点（含目录联接）不跟随、不进入，记为跳过（`link`）；选定的目录本身可以是链接，解析成真实路径后使用。
  - 缺省排除版本库元数据（`.git/**` 等）、依赖与构建缓存（`node_modules/**`、`.venv/**`、`__pycache__/**` 等）和常见凭据文件（`.env`、`.env.*`、`*.pem`、`*.key`、`id_rsa`、`*.token` 等）；规则可以调整。扫描结果给出文件数、总字节数、被排除的条目与被跳过的文件及原因（`link`、`name_not_portable`、`too_large`、`unreadable`、`special_file`）。
  - manifest 路径为 NFC 形式；本机文件名不是 NFC 时快照记下它的实际名称。Windows 无法表示的名称（保留设备名、以点或空格结尾、含 `:` 等）记为 `name_not_portable` 并跳过。仅大小写不同的两个路径无法放进同一份输入，扫描失败（`path_conflict`）。可执行位在 POSIX 上取自文件权限；Windows 上沿用上一份输入里同一路径的标记。
  - 每个文件读取前后核对大小与修改时间，读完后再遍历一次目录比对名称、大小与修改时间；发现变化就整体重扫，三次仍不稳定则失败（`unstable`），不产生混合了新旧内容的快照。
  - 上传时逐个文件再核对内容哈希；与快照不符说明源文件在扫描后被改过（`source_changed`），这份快照不登记为输入。内容寻址保证改过的文件不会以旧哈希的名义进入基线。

**13.3 成果状态**

四个分开的事实：有检查点（final 检查点已 durable）、已发布（`delivery_version > 0`）、已下载、已应用到项目。

- `POST /jobs/{id}/downloads`（read）`{"request_id", "checkpoint_id", "files", "bytes"}` → 201 `{"download_id", "job_id", "checkpoint_id", "manifest_sha256", "files", "bytes", "by", "at"}`，写 `job.downloaded` 事件。客户端在整包下载完成、每个文件的哈希都核对过之后调用；查看文件清单、文本预览或另存单个文件不登记。
- `GET /jobs/{id}` 增加 `downloads` 与 `applications` 两个列表。
- 下载到本机的文件路径只能落在用户选定的目录之内：manifest 路径经 §3 的校验（禁止绝对路径、`..`、盘符），写入时逐级拒绝链接。

**13.4 应用到项目**

应用操作（`applications` 能力）是一条记录：

```json
{"application_id", "project_id", "job_id", "checkpoint_id", "manifest_sha256", "base_manifest_sha256",
 "device", "by", "status": "started|applied|partial|conflict|failed|unknown",
 "plan", "detail", "started_at", "finished_at"}
```

- `POST /projects/{id}/applications`（tasks）`{"request_id", "application_id", "job_id", "checkpoint_id", "device", "plan"}` → 201，`status: started`。任务必须属于该项目，`checkpoint_id` 必须是它已发布的检查点。客户端在第一次写入之前调用；登记不上就不写。
- `POST /projects/{id}/applications/{application_id}`（tasks）`{"request_id", "status", "detail"}` → 更新后的记录。只有发起它的客户端可以上报，结果一经确定不能改写。`GET /projects/{id}/applications`（read）列出记录。写 `project.application` 事件。
- `status` 的含义：`applied` 计划里的改动全部写入；`partial` 写入了一部分（有冲突、出错或被中断后未续完）；`conflict` 因冲突没有写入；`failed` 没有写入或已回滚；`unknown` 被中断且用户选择不再处理。中断的操作停在 `started`。
- job 的 `integration_status`：只有 `applied` 才置为 `integrated`；`partial` 与 `conflict` 置为 `conflict`；`failed`、`unknown` 与停在 `started` 的不改变它。契约指定了回写目标节点（§11.10）的任务由那条路径决定回写状态。项目的 `applied` 记下最近一次完整应用。

本机的应用规则在 `agent_collab/project.py`，CLI 与桌面端共用：

- 比较三份内容：任务的输入基线、成果、目录当前内容。计划只包含成果相对基线新增、修改、删除的路径；目录里的其他文件（任务期间新建的、未跟踪的、被排除的）不在计划里，不会被写或删。
- 每个路径的状态：目录内容等于基线为 `clean`，可以应用；等于成果为 `already`；两者都不是为 `conflict`（`modified_locally`、`deleted_locally`、`created_locally`），保留本机内容。路径经过链接或与现有条目仅大小写不同时为 `conflict`（`unsafe_path`）。
- 并发任务各自在独立工作区执行；它们的成果先后应用到同一个目录时，后一次按自己的基线重新核对：前一次已经改过的文件不再等于后者的基线，记为冲突，不被覆盖。改动互不重叠的成果可以先后应用。同一目录上有一次未完成的应用时，新的应用被拒绝（`unfinished_application`）。
- 有冲突时缺省什么都不写。用户明确选择“只应用没有冲突的文件”时写入其余文件，结果为 `partial`。
- 取消只在两个文件之间生效：正在写的文件写完（备份、原子替换、记日志）后不再开始下一个；结果带 `cancelled: true`，未写的文件列在 `pending`，有已写入的文件时为 `partial`，一个都没写时为 `failed`。最后一个文件写完之后才到的取消不改变结果；取消不等于回滚。上报的 `detail` 相应带 `cancelled: true`。
- 客户端在写入完成后先把要上报的结果连同固定的 `request_id` 存在本机，再发 `POST /projects/{id}/applications/{application_id}`；送不到时按同一个 `request_id` 补报（幂等，controller 只记一次），只向发起这次应用的 controller、用原来的客户端凭据补报。
- 写入之前：成果内容全部取到并核对哈希，检查磁盘空间。写入时先把将被替换或删除的原文件备份到本机状态目录，再逐个文件原子替换并追加日志；每个文件写入前再核对一次它仍是计划时看到的内容，变了就不动它（`changed_since_plan`）。
- 整批写入不是跨文件的原子事务。写入中途进程退出、磁盘写满或权限不足时：出错的文件与未写的文件如实列出；被中断的操作留下状态为 `writing` 的日志，下次由 `recover` 逐文件核对目录现状（已写入、未写入、之后又被改动），可以续写、把已写入且之后未再改动的文件恢复成备份里的原样，或按现状结束并记为 `unknown`。

**13.5 节点配对码、维护与退役**（`node_admin` 能力）

- `POST /nodes/codes`（devices）`{"request_id", "node_id", "ttl_s"?, "replace"?}` → 201 `{"code", "node_id", "expires_at"}`。配对码只在这次响应里出现，重复的 `request_id` 返回 409。`node_id` 已登记时必须带 `replace: true`（配对会替换它原有的凭据），否则返回 409 `node_exists`；已退役的节点返回 409 `node_retired`。写 `audit.action` 事件。
- `POST /nodes/{id}/maintenance`（devices）`{"request_id", "expected_version", "active", "reason"?}` → node。`active` 为 true 时 `reason` 必填（非空，至多 300 字符），为 false 时不需要。进入维护时停止向该节点派单（调度原因 `maintenance`），已在运行的任务不受影响；结束维护时恢复进入维护之前的接单状态（此前已暂停接单的保持暂停）。`GET /nodes` 的每个节点带 `maintenance`（`{"reason", "by", "at", "drain_before"}` 或 null）与 `maintenance_state`：`none`、`draining`（还有任务在运行）、`active`。
- `POST /nodes/{id}/retire`（devices）`{"request_id", "expected_version", "retired", "reason"?}` → node。`retired` 为 true 时 `reason` 必填（非空，至多 300 字符），为 false 时不需要。退役前必须先处理：该节点上有效的 attempt、当前 attempt 在该节点且处于待对账、恢复中或已请求取消的任务、以该节点为目标且尚未完成的回写、该节点上尚未完成的管理操作、结果待确认的远端写回、把它列为候选的协调设置；否则返回 409 `retire_blocked`，`details.blockers` 是一个对象，只含存在的项：`active_attempts`（attempt_id 列表）、`unsettled_jobs`（job_id 列表）、`pending_integrations`（job_id 列表）、`pending_operations`（状态为 `requested` 或 `received` 的 op_id 列表）、`unsettled_applications`（以该节点为目标、仍为 `started` 的 application_id 列表）、`coordination_profile`（协调 profile 的 id）。
  - 退役后：节点记录保留（带 `retired_at`、`retired_by`、`retire_reason`），历史 attempt 仍引用它；它的节点凭据与未用的配对码作废；不再出现在 `GET /nodes`、总览的节点列表、调度与调度解释、模型目录与可选组合里，也不再产生在线状态事件。`GET /nodes?retired=1` 列出已退役的节点，总览带 `retired_nodes` 计数。
  - `retired: false` 恢复身份：记录不变，保持不接单，需要重新配对才有凭据。没有删除节点的端点。

**13.6 节点管理操作与原始日志**（`node_admin`、`raw_logs` 能力）

管理操作是结构化的异步记录，种类是封闭集合，不是远程命令通道：

```json
{"op_id", "node_id", "kind": "probe|log_tail|project_plan|project_apply|project_recover", "params", "status": "requested|received|succeeded|failed|timed_out",
 "requested_by", "requested_at", "received_at", "finished_at", "deadline_at", "result", "error"}
```

- `POST /nodes/{id}/operations`（devices）`{"request_id", "kind", "params"?}` → 201 op。这里只接受 `probe`；`log_tail` 经 `POST /jobs/{id}/logs`，三种项目操作经项目端点，在这里返回 400。`GET /nodes/{id}/operations`（read）列出最近 50 项（不带 `result`），`GET /operations/{op_id}`（read）读取单项。写 `node.operation` 事件。
- 节点在心跳响应的 `operations` 里领取状态为 `requested` 的操作，`POST /nodes/{id}/operations/{op_id}`（节点凭据）上报 `received`，执行后上报 `succeeded` 或 `failed`（带 `result` 或 `error`，结果 ≤768 KiB）。超过 `node_operation_timeout_s`（缺省 120 秒；远端项目写回的三种操作用 `project_operation_timeout_s`，见 §13.10）没有完成记 `timed_out`，不重发；结果已定后的上报不改变它（远端写回的对账例外，见 §13.10）。不认识 `operations` 的旧节点不会领取，操作到期后记 `timed_out`。记录在完成一天后清除。
- `probe`：节点在自己的服务进程上下文里重新运行官方 CLI 的登录状态探测与模型目录探测（不调用模型），返回 `{"at", "auth": {"codex": {"status", "reason", "command", "disabled"}, "claude": {...}}, "harness", "catalog": {"codex": {"status", "reason", "provider", "models"}}, "context": {"user", "pid", "ssh", "tty", "launchd_label", "windows_session", "systemd_unit", "version", "version_dir", "platform"}}`。用户在节点本机按官方方式登录后，用它确认后台服务读得到凭据；探测之后节点刷新并重新上报认证状态与目录。
- 远程提供的节点管理是暂停与恢复接单（`POST /nodes/{id}/drain`）、排空并迁移、维护状态、退役与上述操作，它们都不中断节点与 controller 之间的连接。停止或启动整个节点服务不经 controller 下发：服务停下后心跳通道随之断开，无法再经它启动；这两个动作在节点本机进行（`agent-collab service stop|start`，或该设备上桌面端的本机服务面板）。

原始日志与事件是三种不同的东西：

- 产品事件（`GET /events`、`GET /jobs/{id}/events`）：controller 记录的状态变化与节点上报的进度，不是 CLI 的完整输出。
- 检查摘要（attempt 的 `report.checks`）：每项检查的退出码与标准输出、标准错误的尾部，各 ≤4 KiB。
- 原始输出：harness 与检查进程的完整标准输出与标准错误，只保存在执行节点的 attempt 目录里。
- `POST /jobs/{id}/logs`（read）`{"request_id", "attempt_id", "stream"?, "offset"?, "max_bytes"?}` → 201 op（`kind: log_tail`），客户端随后读取 `GET /operations/{op_id}`。`attempt_id` 必须属于该任务；`stream` 为 `index`（缺省，列出可取的输出）、`harness.stdout`、`harness.stderr`、`check.<n>.stdout|stderr`、`step.<n>.stdout|stderr`（n 为 0–99）；`max_bytes` 为 1–262144。节点只按 attempt 目录的固定布局取这些文件，没有按路径读取的接口。`stream` 为 `index` 时结果为 `{"retained", "streams": [{"stream", "bytes"}]}`（取不存在的流另带 `missing: true`）；取具体的流时结果为 `{"retained", "streams", "stream", "offset", "next_offset", "total_bytes", "eof", "data"}`：`retained: false` 表示该节点上已没有这个 attempt 的本地文件；`data` 是经节点脱敏的文本，按 `next_offset` 继续读取下一块。

**13.7 协调设置**（`coordination_settings` 能力）

- `GET /settings/coordination`（read）→ `{"settings": {"revision", "updated_at", "updated_by", "enabled", "node_ids"}, "effective", "unknown_nodes", "history"}`。`effective` 是受管协调回合实际使用的配置（同 `GET /overview` 的 `managed_coordinator`）；`unknown_nodes` 列出不在已登记节点里的 ID。还没有经 API 保存过时 `revision` 为 0，`enabled` 与 `node_ids` 取自 `config.json` 当前生效的 profile。
- `PUT /settings/coordination`（settings）`{"request_id", "expected_revision", "enabled", "node_ids"}` → 更新后的 settings；版本冲突返回 409 `version_mismatch`。已退役的节点不能列入。保存之后（`revision > 0`），是否启用与候选节点由这份设置决定；协调模型与 effort 取 §12.3 的 `claude.default`，未设置时取 `config.json` 的 profile。没有模型或没有候选节点时 `effective.status` 为 `profile_missing`，协调回合不创建。
- Codex 执行策略（`settings/models` 的 `codex`、项目默认值）与 Claude 协调策略（`claude.default` 与本节）是两组设置，互不覆盖。

**13.8 节点回写目标的范围**

节点配置 `agent.json` 的可选键 `integration_roots`（绝对路径列表）：给出时，§11.10 的回写目标必须在其中某个目录之内（解析链接后比较），否则该回写上报 `conflict` 且不写入。未配置时行为不变。

**13.9 提交闸门、备份后的活动与切换屏障**

- 提交闸门：`POST /intake`（maintain）`{"request_id", "expected_version", "mode": "open|acceptance|closed", "reason"?}` → meta；`expected_version` 是 `GET /overview` 的 `controller.metadata_version`，`mode` 不是 `open` 时必须给原因。`GET /overview` 带 `intake: {"mode", "reason", "by", "at"}`。写 `controller.intake` 事件。
  - `closed`：`POST /jobs` 对任何凭据（含管理令牌）返回 409 `intake_closed`。读取、对已有任务的操作、节点上报与 controller 自己创建的协调回合和修复不受影响。
  - `acceptance`：只接受验收运行，即请求带 `acceptance_run: true`、提交者具备 `maintain` 权限、不属于项目（既不带 `project_id`，`work_package_id` 也不指向属于项目的根：项目任务的修订同样被拒绝）、契约没有回写目标节点、`recovery.side_effects` 为 `none`；不满足形状的返回 400，没带标记的返回 409 `intake_closed`。
  - 任意模式下都可以提交验收运行；job 带 `acceptance_run`，`GET /jobs?acceptance_run=1` 列出它们。
- 备份清单（`backup-manifest.json`）带 `event_seq`：快照里最后一条事件的序号。
- `GET /activity?after=<seq>`（read）→ `{"after", "last_seq", "jobs": [{"job_id", "title", "acceptance_run", "coordinates", "project_id"}], "ordinary_jobs", "acceptance_jobs", "counts", "disposable"}`：序号大于 `after` 的事件里受理的任务与各类事实的数量（确认的检查点、发布、验收、下载、基线、项目、节点写回 `integrated`、已写入或结果未知的应用 `applications_written`、其间开始且仍没有最终结果的应用 `applications_unsettled` 等）。协调回合随它协调的任务归类。`disposable` 为 true 表示其间没有普通任务，也没有任何写回、已写入或结果未定的应用。
- 切换屏障：`agent-collab controller barrier --data-dir D --request-id <UUID> --reason R`，在已停止的 controller 上运行。要求派单已暂停、提交闸门不是 `open`。它把恢复代次加一并撤销此前签发的全部执行权（与 `restore` 相同的处理，但数据不回退）：attempt 的租约令牌作废、进行中的 attempt 记为失联、未结束的任务停在 `waiting_reconcile`（`stale_generation`）、协调租约失效、节点下次心跳被要求对账；尚未被节点领取（`requested`）的管理操作记 `failed`（`error: stale_generation`；从备份恢复的处理不同：快照里未结束的节点操作，包括项目写回与恢复，都记为 `timed_out` 而不重新下发，写回与恢复的应用记录保留为待确认，见 §14.2）；其中未领取的 `project_apply` 对应的远端写回记 `failed`（`reason: stale_generation`，`written` 为空），未领取的 `project_recover` 不改变应用记录（仍为 `started`，可以再次 recover）；已领取的照常对账。节点凭据、客户端凭据、管理令牌与已结束任务的历史不变。写 `controller.barrier` 事件；同一 `request_id` 重复执行返回第一次的结果。普通的停止再启动保留恢复代次、租约与协调租约。

**13.10 远端项目写回**（`remote_apply` 能力）

成果经目标节点的受限操作写回该节点本机绑定的项目目录。它与 §11.10 是两条路径：§11.10 在受理时由契约指定目标节点与绝对路径，controller 保存并下发这个路径，发布后自动回写；本节的目录只在节点本机授权，controller 从不保存、下发或接收路径，写回由客户端在审阅之后显式发起，每次写回是一条 §13.4 的应用记录。§11.10 不变。

- 目录授权在节点本机：`agent-collab agent project-bind --data-dir <节点数据目录> <project_id> <目录>`、`project-unbind --data-dir D <project_id>`、`project-bindings --data-dir D`。绑定保存在节点数据目录的 `project-bindings.json`，目录解析链接后保存；节点配置了 `integration_roots`（§13.8）时必须在其中，否则拒绝（`outside_integration_roots`）。每次执行前再核对一次：目录仍存在（`not_a_directory`）、仍解析到绑定时的那个真实目录（被换成链接时 `binding_changed`）、仍在 `integration_roots` 之内。
- 声明：注册响应的 `features` 含 `remote_apply` 时，节点在注册的 `capabilities.project_bindings` 里上报 `[{"project_id", "root_name"}]`（只有目录名，至多 256 项；形状不符的注册返回 400）。节点得知该能力后立即带上绑定再注册一次；绑定改动后不需要重启服务，下一次心跳之后重新注册。`GET /nodes` 的节点 `capabilities` 与 `GET /projects`、`GET /projects/{id}` 的 `remote_targets: [{"node_id", "root_name", "status"}]` 给出这些声明；已退役的节点不算。
- 端点（读取需要 `read`，其余需要 `tasks`）：
  - `POST /projects/{id}/plans` `{"request_id", "job_id", "node_id"}` → 201 op（`kind: project_plan`），结果经 `GET /operations/{op_id}` 读取。
  - `POST /projects/{id}/applications` 带 `node_id` 时是远端写回：`{"request_id", "application_id", "job_id", "checkpoint_id", "node_id", "expected_base_manifest_sha256", "skip_conflicts"?, "plan"?}` → 201 应用记录，`status: started`，另带 `target_node_id`、`operation_id`（这次写回的 `project_apply` 操作）与 `skip_conflicts`，`device` 为节点名。`expected_base_manifest_sha256` 必填（任务没有输入时为 null）；它与 `checkpoint_id` 是客户端审阅时看到的值，与任务当前的输入基线、已发布检查点不一致返回 409 `conflict`（`details.current`）。不接受 `device` 与其他字段。应用 ID 由客户端给出，不能复用；同一 `request_id` 按 §5 的幂等规则重放原记录，不产生第二项操作。
  - `GET /projects/{id}/applications/{application_id}`（read）→ 应用记录（本机应用与远端写回都适用）。
  - `POST /projects/{id}/applications/{application_id}/recover` `{"request_id", "action": "status|resume|rollback|abandon"}` → 201 op（`kind: project_recover`）。只用于远端写回、且记录仍为 `started`，否则 409。`status` 以外的动作在这次应用的 `project_apply` 或另一个非 status 的 `project_recover` 仍为 `requested` 或 `received` 时返回 409（`details.operation_id` 是那项操作）；受理后应用记录带 `recovery_operation_id`，此后只认这项操作的结果，超时后晚到的原应用结果不再结束记录。
  - 校验：任务属于该项目且有已发布检查点（409）；节点在册（404）、未退役（409 `node_retired`）、声明了该项目的绑定（409 `not_bound`）；项目未归档（写回，409 `project_archived`）。
  - 操作参数全部由 controller 按任务记录填写：`project_id`、`job_id`、`checkpoint_id` 与 `manifest_sha256`（已发布检查点）、`base_manifest_sha256`（任务的输入基线，可为 null）、`exclude`（契约的 `workspace.exclude`）；写回另有 `application_id`、`skip_conflicts`，恢复另有 `application_id`、`action`。通用的 `POST /nodes/{id}/operations` 不接受这三种操作（400）。远端写回的结果只由目标节点上报：经 `POST /projects/{id}/applications/{application_id}` 上报远端写回返回 403。
- 已上报过结果的 `project_apply` 或 `action` 不是 `status` 的 `project_recover` 又被下发（controller 没有它的结果）时，节点不重新执行，确认领取后按本机应用日志补报当时的结果；`status` 是只读的现状核对，重放时重新核对一次并返回当前的 `result.report`。
- 节点执行：只在绑定目录上调用 `agent_collab/project.py` 的 plan、apply、recover（§13.4 的规则：三方比较、冲突缺省不写、`skip_conflicts` 时部分写入、逐文件备份与日志、中断后续写、回滚或按现状结束）；应用日志与备份在节点数据目录的 `projects/` 里。manifest 与成果内容用节点令牌经 `GET /blobs/<sha256>` 取得并核对哈希（节点令牌此前就可读 blob，没有新增权限）。节点同一时间只执行一项这类操作。
- 结果：
  - `project_plan`：`result` 为计划（`changes`、`summary`、`base_manifest_sha256`、`manifest_sha256`、`root_name`），没有 `root`。过长时截断：冲突排在前面保留，带 `truncated` 与原条数 `counts`。
  - `project_apply` 与非 status 的 `project_recover`：`result.application = {"status", "detail"}`，controller 用它结束应用记录，规则同 §13.4：`integration_status` 只有 `applied` 置 `integrated`，`partial`、`conflict` 置 `conflict`，`applied` 更新项目的 `applied`。回滚全部完成记 `failed` 并带 `rolled_back: true`。
  - `detail` 只含相对路径与原因：`changed`、`already`、`pending`、`conflicts`、`errors`、`restored`、`kept`、`written`、`remaining`、`diverged`、`summary`（节点上报到哪些就有哪些），没写入就结束的带 `reason`、`message` 与 `written: []`，另加 `operation_id`；过长时同样截断。不含任何绝对路径：备份目录不上报，错误文本里的本机目录换成目录名、节点数据目录换成 `<node-data>`。
  - `project_recover` 的 `status`：`result.report` 是目录现状（`interrupted`、`written`、`remaining`、`diverged`），不改变应用记录。`abandon` 而节点上没有这次写回的本机记录时记 `unknown`（`reason: no_local_record`）。
  - 第一次写入之前出错（未绑定、超出 `integration_roots`、目录上有未完成的应用、取内容失败、空间不足等）：操作 `failed`，应用 `failed`，`detail.written` 为空。写入开始后出错且本机日志没有最终结果：操作 `failed`，应用保持 `started`（结果待确认），用 recover 处理。
- 时限与对账：三种操作的时限都是 `project_operation_timeout_s`（controller 配置，缺省 1800 秒）。
  - 节点从未上报 `received` 就到期：操作 `timed_out`，写回的应用记录结束为 `failed`（`detail.reason: not_received`，`written: []`）。节点之后上报 `received` 得到 `timed_out`，按约定放弃，不再执行。
  - 已上报 `received` 但到期没有结果：操作 `timed_out`，应用保持 `started`（结果待确认），不当作失败。节点之后上报的结果仍更新应用记录（这是对账，不是重复执行）；操作状态仍为 `timed_out`，另带 `reported_status`、`reported_late_at` 与结果。应用待确认期间这项操作不被清除，对账之后再保留一天。
  - 节点离线时不会有任何一项显示为成功。
- 不重复执行：节点在确认领取之前把操作落盘到 `<节点数据目录>/operations/<op_id>.json`，开始执行之前记为 `running`。进程重启后见到 `running` 的操作不再执行：写回类按本机应用日志补报能确定的结果（没有日志说明一个文件都没写，应用记 `failed`、`reason: interrupted_before_write`；日志已有最终结果就补报它；日志停在写入中就报告操作失败，应用保持 `started`）。结果上报失败时留在本地，之后补报；见过的 `op_id` 不再处理。管理请求超时、客户端重试与节点重启都不会让同一次写回写第二次。
- 已知限制：节点重启后第一次注册不带绑定，紧接着的那次注册带上，其间指向该节点的请求得到 `not_bound`。

## 14. 备份维护、恢复预览与切换预检

所有 `/maintenance/*` 与 `GET/PUT /settings/backups` 都需要 `maintain`；节点凭据不能调用。API 只接受实例内备份 ID，不接受服务器路径；未知请求字段与查询参数拒绝。备份 ID 为 `managed-*` 或安装器的 `upgrade-*`，范围固定为 `<data_dir 的父目录>/backups/`，拒绝链接目录。变更使用 §5 的 `request_id` 幂等机制：原样重放返回同一操作与结果，换内容返回 409；重放仍先验证权限。

**14.1 维护操作与备份**

维护操作同步完成，响应为 `{"maintenance_op": {...}}`，记录保存于数据库：

```json
{"op_id", "kind": "backup|verify|retention|restore", "status": "running|succeeded|failed",
 "steps": [{"name", "status"}], "result", "error", "requested_by", "requested_at", "started_at", "finished_at"}
```

同步请求返回时已为 `succeeded` 或 `failed`；`running` 是执行中的内部状态。业务失败也返回 HTTP 200 的已完成操作并保存幂等回执，调用方必须检查 `status`；参数/权限/版本错误仍返回 4xx。失败操作的重放不再执行，重试须换 request_id。备份的文件载荷与数据库不是跨文件事务；进程被强制终止可能留下没有操作回执的目录，列表会核对它，不能把目录存在视为成功。

| 端点 | 请求 | 结果 |
| --- | --- | --- |
| `POST /maintenance/backups` | `{"request_id", "label"?}`；label 至多 200 字符，仅作说明 | 创建 `managed-<UTC时间>-<短ID>` 一致备份，核对后执行保留策略；result 含 `backup_id`、label、`location: instance`、removed |
| `GET /maintenance/operations/{op_id}` | 无 | 持久保存的 maintenance_op |
| `GET /maintenance/backups` | 无 | `{"backups": [...]}`：id、kind（managed/upgrade）、created_at、schema（user_version、fingerprint）、db_sha256、blob_count、blob_bytes、authority_generation、event_seq、last_verification、location、valid；损坏备份另带 error |
| `POST /maintenance/backups/{id}/verify` | `{"request_id"}` | 在临时副本核对数据库 SHA-256、integrity_check、schema、blob 清单与哈希、引用和代次/事件序号；保存最近核对结果 |
| `GET /maintenance/backups/{id}/archive` | 无 | 确定性 tar 流；`Content-Length` / `X-Archive-Bytes` 为字节数，`X-Content-SHA256` 为归档 SHA-256；只有 manifest、数据库、引用 blobs |
| `GET /settings/backups` | 无 | `{"settings": {"revision", "keep", "updated_at", "updated_by"}}`；未设置时 revision=0、keep=8 |
| `PUT /settings/backups` | `{"request_id", "expected_revision", "keep"}`；keep 为正整数 | 保存 revision+1 并执行清理，返回 retention 操作；result 含 settings、removed；版本不符为 409 version_mismatch |

列表实时核对但不写核对历史；只有变更操作记录核对结果。保留清理仅作用于 managed 目录，按创建时间、事件序号排序，绝不删除最新的有效备份（它也是唯一有效备份时仍保护）；不清理 upgrade 或其他目录。keep 是目标数量，保护恢复点优先于份数。核对失败不把损坏目录计为有效恢复点。

位置始终标为 `instance`；HTTP 导出成功不等于文件已保存到实例之外。客户端“另存为”或 CLI `export --to` 成功保存并核对哈希后，才能说明选定位置有副本；是否真正离开实例取决于实际保存位置。归档不含加密层。

**14.2 恢复预览与离线恢复**

`GET /maintenance/restore-preview?backup_id=<id>` 返回 `verdict`、backup、changes、identity、unresolved_applications、history_incomplete、disposable、disposable_scope、file_effects。changes 逐项列出记录类型、ID、新增/改变/删除、变动字段、可公开的前后摘要及恢复影响；包含任务、attempt、检查点、验收、发布、本机与远端应用、项目、模型/协调/备份设置、身份和凭据变化，不输出凭据哈希。

- `safe`：快照核对通过、历史足够完整、只有可丢弃的验收状态；`needs_reconcile`：存在其他事实、待确认应用或信息不足；`blocked`：备份载荷损坏或 schema 不兼容，带具体 error。
- `disposable` 沿用 `/activity` 的任务与写回统计，不覆盖项目、设置与配对，不能单独当作安全结论。节点和本机项目文件写入不会被撤回；`started/unknown` 应用必须与实际目录和本机 journal 对账。
- 预览、列表、导出、inspect 都不调用 `backup()`，不处理请求到期、不写 controller 数据库。它们会在临时目录复制并核对备份；原始备份的文件、哈希及旁文件保持不变。
- 离线恢复须在 controller 停止后、目标主机执行。当前库可读时，默认 `carry-forward`：备份之后撤销或删除的客户端、退役/删除的节点及消失或替换的凭据按拒绝优先结转；缩减过的客户端 scopes 不会扩大。即使给 `keep`，也不能覆盖已知撤销。备份之后新增的客户端、节点及换过的凭据逐项列在 `identity.re_pair`。
- 当前库不可读或不存在时，必须显式选择 `keep`（保留备份授权，结果警告无法确认备份之后撤销）或 `revoke-all`（撤销全部客户端/节点凭据，重新配对）。不选或选 carry-forward 返回 `identity_required`。所有恢复清除旧浏览器会话及未用配对码；管理令牌仍按目标主机现有文件处理，不随备份迁移。
- generation 更新为 `max(可读当前库,备份)+1`，执行权全部撤销、派单暂停，提交闸门取备份原值；这是执行 fencing，与授权撤销分别记录。快照中未结束的节点操作（通用操作与项目写回、恢复操作都包括在内）记为 timed_out（`error.code: restore_requires_reconcile`），不重新下发：备份之后节点可能已经执行过它们。项目写回与恢复的应用记录保留为待确认，节点之后上报的结果按 §13.10 的对账规则更新应用；也可以先对账再显式发起 recover。
- 核对失败返回如 `backup_object_missing`（具体对象）、`backup_database_hash`、`backup_database_corrupt`、`backup_blob_hash`、`backup_references_invalid`、`backup_schema_incompatible`，不替换当前数据库。恢复结果写 `controller.restored` 事件、maintenance_op 记录以及 `<data_dir 的父目录>/restore-results/<op_id>.json`。

公共 CLI：

```sh
agent-collab maintenance backup [--label 说明] [--request-id UUID]
agent-collab maintenance backups
agent-collab maintenance verify <id> [--request-id UUID]
agent-collab maintenance export <id> --to <本机文件>
agent-collab maintenance restore-preview <id>
agent-collab maintenance barrier-preview
agent-collab maintenance retention [连接参数] set --keep N [--expected-revision N] [--request-id UUID]
agent-collab maintenance inspect --from <目录或tar>
agent-collab maintenance restore --data-dir D --from <目录或tar> [--identity carry-forward|keep|revoke-all] [--dry-run]
```

在线命令沿用 `--profile`、`--url`、`--token-file`、`--ca-file`，输出 JSON。export 在本机临时文件完成下载并核对大小与哈希后再替换目标；核对失败保留原目标。离线命令不用连接参数；dry-run 只给身份与代次计划，不写库、不生成恢复回执，controller 停止与锁由真正恢复时再次核对。它不能替代 restore-preview 的业务影响清点。CLI 成功返回 0、已完成失败操作返回 1、离线参数/核对拒绝返回 2、连接不可用返回 3。

**14.3 屏障待发布成果与弃置**

`GET /maintenance/barrier-preview` 返回 `{"ready", "pending_results", "dispatch_paused", "intake"}`。pending_results 是最新 attempt 成功、有 durable/verified 检查点、最新 durable 检查点尚未发布且未明确弃置的任务；每项包含 job_id、title、attempt_id、checkpoint_id、acceptance、delivery_checkpoint_id、reason。只检查当前成果，不要求历史检查点全部发布。

`POST /jobs/{id}/dismiss`（tasks）接受 `{"request_id", "expected_version", "reason", "checkpoint_id"?}`；reason 非空、至多 1000 字符，沿用 job CAS、幂等及协调租约门禁。只记录最新待发布检查点的弃置；带 `checkpoint_id` 时它必须就是这个检查点，否则 409 `conflict`（确认之后又出现了新的待发布检查点），不删除成果；job 及详情增加 `dismissed_checkpoint_id`、`dismissal`，写 `job.dismissed`。该检查点后续发布返回 409 `checkpoint_dismissed`。新的 attempt/checkpoint 仍需单独处理。

离线 `barrier()` 在持有 controller 锁后，于同一个 `BEGIN IMMEDIATE` 事务里重新检查暂停、提交闸门、pending_results 与代次推进。有未处理成果即拒绝，错误 `barrier_pending_results` 带任务清单；预览不能代替这次权威核对。先发布或明确弃置，之后才可执行屏障。普通更新不调用屏障。

## 15. 不在 v1 承诺范围

活进程迁移、模型内部推理状态、未上传的本地改动、任意 Shell 或模型调用的 exactly-once、控制面高可用。见 README 的保障说明。
