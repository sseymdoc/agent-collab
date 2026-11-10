# agent-collab 协议契约 v1

状态：首版契约，由 Claude 主控维护。controller（WP1）与 node agent（WP2）都按本文实现；发现矛盾或缺口时在报告中提出，不各自改写语义。共享常量、manifest 规范化与路径校验在 `agent_collab/protocol.py`，worker 不修改该文件。

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

三类凭据互不通用；节点令牌不能调用客户端变更端点，UI 会话不能冒充节点。模型 provider 登录不经过本系统。`GET /healthz` 无需认证，只返回 `{"ok": true}`。

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
- `harness_params`：`stub` 为 `{"steps": [...]}`（见 §9）；`codex` 为 `{"model": "gpt-6-astra", "effort": "high|xhigh|max", "sandbox": "read-only|workspace-write"}`，模型固定，effort 只允许三档，缺省 `xhigh`，非法值在受理时拒绝。
- `recovery.auto=true` 要求 `inputs_manifest` 非空，且所有 blob 已存在于 controller，才能把 `protection` 置为 `protected`；否则为 `preparing`，**不得派单**，`status_reason` 列出缺失 blob。`recovery.auto=false` 时 `protection=unprotected`，可直接派单，但不会自动恢复。
- `side_effects=manual` 时永不自动恢复；`idempotent` 需要 `attempt.side_effect` 事件中的逻辑操作 ID（§7）。

**contract.workspace（WP5）**：可选 `{"exclude": [glob, ...]}`；省略 workspace 或 exclude 时使用
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
- 新 attempt 必须在节点上使用独立工作区 `<agent_data>/attempts/<attempt_id>/work`。
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

**complete.report（WP5）**：agent 在每次 complete 中提供 JSON 对象，经 `agent.clean` 移除已知节点/租约令牌，
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

## 5. 客户端端点（admin token 或 UI 会话）

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

**drain.migrate（WP5）**：migrate 缺省 false，保持仅停止新领取的行为；drain=false 时忽略 migrate。
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
- 游标早于已保留的最小 `seq` 时，先发送 `event: snapshot_required` 与 `data: {"min_seq"}`，客户端应重新 `GET` 快照后用新游标连接。
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

## 10. 协调权与受管协调者（阶段 C，定稿）

确定性调度只恢复已受理的工作包；需求解释、验收判断、修复派单等语义决策由协调者完成。协调者分两类：`interactive`（用户当前 Claude 或控制台用户）与 `managed`（在幸存节点按事件启动的真实 Claude 回合）。

**协调租约**（按根工作包独占）：

| 方法与路径 | 说明 |
| --- | --- |
| `GET /packages/{work_package_id}/coordinator` | 当前租约：`{"holder_kind", "holder_ref", "expires_at", "version", "authority_generation"}` 或 `null`；`(authority_generation, version)` 唯一标识一次租约状态（数据恢复后 version 可能重新从 1 开始） |
| `POST /packages/{work_package_id}/coordinator` | `{"request_id", "holder_kind": "interactive\|managed", "holder_ref", "ttl_s", "takeover": false, "reason"}` → `{"coordinator_token", "expires_at", "version", "authority_generation"}`（按 request_id 重放升级前的旧回执时后两项为 `null`，表示未知）；他人持有有效租约时 409 `conflict`，除非管理员 `takeover=true` 且给出 `reason`（写 `coordinator.takeover` 事件）；同一持有者带 `X-Coordinator-Token` 时为续租 |
| `DELETE /packages/{work_package_id}/coordinator` | 带 `X-Coordinator-Token` 释放 |

存在有效租约时，针对该根工作包的 `assess`、`publish`、`retry`、`integration` 与同根新 job 提交必须带匹配的 `X-Coordinator-Token`，否则 409 `conflict`（`details.reason="coordinator_lease"`）。用户取消不受此限制。旧协调者恢复连接后先读租约与交付版本，不能凭旧令牌继续。

节点令牌发起、且 `publish.mode=auto` 的发布不受协调租约限制，仍执行全部 epoch、generation、attempt 和发布 CAS 检查。节点 integration 无此豁免。租约端点只接受管理员 Bearer 或由管理员登录换取的 UI 会话；后者仍要求 CSRF/Origin 校验。`ttl_s` 必须是正有限数；接管必须给出非空 reason。租约 token 只保存哈希，GET 不返回 token；POST 获取回执支持 request_id 重放，续租重放仍须带原 token，已失效的获取回执返回 409，不复活旧租约。DELETE 在无有效租约时幂等返回 `{}`。普通重启保留租约，恢复备份后的 generation 变更使旧协调租约失效。

**受管协调者**：

- 配置 `managed_coordinator: {"enabled": bool, "profile_id": str}` 与 `standby_profiles: {id: {"harness": "claude", "model", "effort", "node_ids": []}}`，由用户事先填写。显式 `enabled=false` 时 status 为 `disabled`；缺失配置或必填字段、model/effort 为空、node_ids 为空或非法时为 `profile_missing`，不创建协调 job。完整启用配置为 `ready`，只表示配置完整，不代表 CLI 或认证已实测可用。不猜测模型或强度，也不回退到其他档位。
- 触发：job 进入 `completed`、`waiting_reconcile`，或收到 `rework` 验收，且该根工作包没有有效的 interactive 租约。controller 在该状态/验收事务内创建协调 job（`harness="claude"`，contract 内 `coordinates: {"job_id", "work_package_id"}`，`recovery.auto=false`，`requirements.tools=["claude"]`，节点限于 profile 的 `node_ids`）。以目标 `job_id + 触发类型 + job.version` 持久去重；不同触发可排队，领取时再次检查目标根没有有效租约。协调 job 使用独立 job/root 和执行预算，不计入目标根修复预算，publish 为 manual；客户端不能自行提交 claude/coordinates 契约。协调 job 自身的完成或失联不触发下一轮；本轮应用的 rework 验收和 follow_up 属于同一决策，不递归触发本轮自身的验收。
- 协调 attempt 领取时自动获得目标根的 managed 租约（holder_ref 为协调 attempt_id），随正常 attempt 心跳续租，完成、失败、取消确认、失联或显式 retry 撤销时释放。已被接管时旧 attempt 不得释放新持有者的租约，也不得应用决策。取消请求发出后，在确认/失联前仍保留租约。
- inputs_manifest 含 controller 生成的 `context.json`（目标 job 契约与状态、最新 attempt report、最新 durable checkpoint 相对 inputs 的 diff、验收记录）和 `decision-schema.json`；文件及 manifest 均通过 blob 提供。model/effort 与 node_ids 在触发时从配置取得快照。只响应新事件，不因配置启用或 interactive 租约过期而补发历史触发；不使用模型轮询心跳。
- **context.json 文本证据（WP5）**：在既有字段之外增加 `files:[{"path","content"}]` 和 `files_omitted:[{"path","reason"}]`。
  选择目标 job 最新的 durable/verified final 检查点，无 final 则选最新 durable/verified 检查点（按受理顺序，uploading 不参与）。
  files 按路径排序，仅包含可严格 UTF-8 解码的完整文件内容；单文件 ≤32768 bytes，累计 ≤262144 bytes，均按原文件字节计，非 JSON 转义后的字节。
  依次按 workspace 排除、单文件上限、UTF-8 解码、总预算判定，省略原因分别为 excluded、too_large、binary、budget；省略列表也按路径排序。
  无检查点时两列表为空。已有 report 仍为目标 job 最新 attempt 的报告，checkpoint_diff 仍对应 job.durable_checkpoint_id；
  文件集合优先 final，因此在 final 后又有 boundary 的情况下可能与 diff 对应检查点不同。验收仍绑定 context 捕获的 job.durable_checkpoint_id。
- 协调回合输出 `decision.json`（作为 final 检查点文件）：`{"decision": "accept|rework|escalate", "assessment": {"status", "method", "result", "checkpoint_id"}, "follow_up": {"title", "goal", "acceptance", "instructions", "effort"} | null, "notes"}`。所有列出的键必填；顶层、assessment、follow_up 对象均为 additionalProperties=false。method/result/notes/title/goal/instructions 为字符串，follow_up.acceptance 为非空字符串数组（minItems=1），effort 仅 high/xhigh/max；不接受 contract、budgets、scope、base_checkpoint_id 等额外字段。adapter 使用同一 schema 作为 --json-schema 和本地形状复核依据，不做依赖目标状态的判断。controller 再检查 accept/status=passed、rework/status=rework，以及 checkpoint_id 必须指向 context 捕获的目标 durable checkpoint；escalate 可用 null checkpoint_id。非 rework 的 follow_up 应为 null，否则 controller 单独拒绝该修复请求。
- 修复契约由 controller 从请求和被验收检查点派生：harness=codex，harness_params 固定 model=gpt-6-astra、sandbox=workspace-write，effort 取 follow_up；goal/acceptance 取 follow_up，prompt 包含 instructions、goal、acceptance 及目标契约 excludes。excludes、requirements、recovery、resources、publish、integration、workspace 继承目标，requirements.tools 补入 codex 且不重复。budgets 继承目标的累计上限并保留取较小值的限制，不把上限改成剩余额度；follow_up 无权覆盖预算。inputs_manifest 必须等于被验收检查点的完整 manifest，包括 base/deleted。domain.validate_contract 验证结构，controller 仅对这种从同根 durable/verified 检查点派生的输入允许非空 base，普通客户端提交的非空输入 base 仍拒绝。修复成功受理后沿同根计 repairs+1。
- controller 在同一事务内确认 managed 租约仍属于该 attempt、目标 epoch/状态/检查点/验收/取消状态未改变且没有后续同根修订，然后应用决策；节点自动发布引起的 delivery/version 更新不使输入失效。合法 accept/rework 的验收先记录并绑定 checkpoint manifest，写 coordinator.decision_applied；follow_up 在独立保存点处理。修复请求形状错误、派生契约无效或派单失败时，仅回滚修复产生的状态/计数，写 `coordinator.follow_up_rejected`（含 reason）；超预算或根副作用未知写 `coordinator.escalated`。这些情况保留验收，协调 attempt 仍为 succeeded、协调 job 为 completed，完成请求重放不重复验收/派单。合法 rework 从 waiting_reconcile 创建修复前，将旧目标标为 failed(managed_rework)；创建失败时该状态变更也回滚，目标保留 waiting_reconcile 和已记录验收。follow_up=null 仅记录验收。显式 escalate 仍只记录事件、保持目标状态等待人工。验收自身非法或持有权/目标版本/检查点失效时，整次应用拒绝，写 `coordinator.decision_rejected`，不改变目标 job 和验收。
- 节点 claude 适配器使用用户本机已安装的官方 Claude Code 与其官方登录：`claude -p --model <profile.model> --effort <profile.effort> --output-format json --json-schema <内联 JSON> --permission-mode dontAsk --tools "" --session-id <新 uuid>`（禁用全部工具）。prompt 经 stdin，内嵌 context.json 全文并要求仅返回结构化决策；follow_up 只需给修复目标、验收项、给 Codex 的修改说明及 effort，系统从被验收检查点自动生成完整契约。仅接受 stdout JSON 的 structured_output，通过 schema 后由 agent 写 decision.json 并建立 final checkpoint，不在 adapter 内生成或校验修复契约。含旧 contract 字段或缺字段的 follow_up 会在 adapter 端失败且不写 decision.json；若错误 follow_up 已通过其他客户端到达 controller，其合法验收仍可部分应用。不把自由文本 result 当作决策。Claude 不获写文件或执行命令权限，认证失败分类 auth_expired。路径来自 agent.json 的 claude_command（默认 claude），detect 只在找到可执行文件后探测版本。适配器不读取、复制或代理 Claude.ai OAuth 凭据，不改变计费路径；每轮新建会话，不 resume 原生会话。

## 11. rc5 增补（主控定稿；向后兼容，旧字段语义不变）

**11.1 命令解析（agent）**：agent.json 新增可选键 `codex_command`（已有，rc5 起 detect 也使用）、`claude_command`、`workspace_root`、`disabled_harnesses`、`codex_windows_sandbox`、`auth_probe_s`。
- `*_command` 可为绝对路径字符串或 argv 列表（首元素为绝对路径）；未配置时按顺序查找：服务进程 PATH，然后 `~/.local/bin`；macOS 再查 `/opt/homebrew/bin`、`/usr/local/bin`。Windows 不排除 `.exe/.cmd` 之外的 PATHEXT 规则，仍排除 WindowsApps 应用执行别名。
- 解析出的绝对 argv 只计算一次，供 detect、doctor、auth 探测和 adapter 执行共用；探测与执行使用同一环境：服务环境，PATH 前置“解析出的可执行文件所在目录”。不读取 shell rc、不依赖交互终端。
- `capabilities.harness_paths: {"codex": "<abs>"|null, "claude": "<abs>"|null}`；`capabilities.tools.codex/claude` 与 `harness.codex/claude` 取自同一解析结果（版本字符串，失败为 null）。
- `disabled_harnesses: {"codex": "原因"}`：该 harness 报告 null，并在 `capabilities.harness_disabled` 中列出原因；用于在修复验证前撤销已知失败的能力。

**11.2 认证探测（agent，不调用模型）**：启动时及每 `auth_probe_s`（缺省 900）秒运行 `codex login status`、`claude auth status`（解析 JSON `loggedIn`），超时 20 秒。结果映射为 `auth_health[h] = ok|expired|unknown`（明确未登录为 expired，命令失败或超时为 unknown），随心跳上报。输出只取状态，不记录账户标识或凭据。controller 合并规则不变（§3）。

**11.3 版本身份（agent 与 controller 共用）**：运行包版本从运行代码所在版本目录的 `release-manifest.json` 读取 `version` 与 `git_commit`，读不到时回退包内常量且 git_commit 为 null。node 的 `agent_version` 为该版本；`capabilities.agent = {"version", "git_commit"}`；attempt.runtime 增加 `agent_commit`。

**11.4 工作区位置**：配置 `workspace_root` 时，attempt 工作区为 `<workspace_root>/<attempt_id>`（不再位于 `<agent_data>/attempts/<id>/work`），元数据、令牌、输出仍在 agent 数据目录。Windows 上若工作区位于用户配置文件目录（`%USERPROFILE%` 之下）且 codex 可用，doctor 报告 `workspace_in_profile` 警告：Codex Windows 沙箱身份无权列出用户目录，PowerShell 起始位置会退回驱动器根。

**11.5 Codex 执行参数**：在既有 argv 上增加 `--ignore-user-config`（不加载用户 config.toml 中的 MCP 服务、插件、notify 钩子与默认档位；认证仍用 CODEX_HOME，不复制凭据），Windows 上再加 `-c windows.sandbox="<codex_windows_sandbox，缺省 elevated>"`。model、effort、sandbox 与 sandbox_workspace_write.* 继续显式传入。

**11.6 契约检查与执行证据**：contract 可选 `checks: [{"name", "run": [argv...], "timeout_s": 600}]`（≤16 项；name 唯一，匹配 `[A-Za-z0-9_.-]{1,64}`；run 为非空字符串数组，`{python}` 占位同 §9；timeout_s 为 1–3600 的数字）。参与 request_hash；修复契约继承目标的 checks。
- agent 在 harness 成功结束后（stub 全部步骤完成、codex turn completed 且退出码 0），在工作区内按序运行每项检查（无 shell、stdin 为空、服务环境），之后才建立 final 检查点。检查失败不改变 attempt outcome。
- `report.checks = {"manifest_sha256": <final 检查点 manifest 哈希>, "node_id", "platform": {"os", "release", "machine", "python"}, "results": [{"name", "argv", "exit_code": int|null, "timed_out": bool, "duration_s", "stdout_tail", "stderr_tail"}]}`（尾部各 ≤4KB，经脱敏）。harness 未成功时不运行检查，`report.checks` 为 null。

**11.7 验收证据门禁（controller）**：contract.checks 非空时，任何来源（受管协调者、管理员、UI 会话）的 `passed` 验收，都要求产生被验收检查点的 attempt 的 `report.checks.manifest_sha256` 等于该检查点 manifest 哈希，且每项必需检查都存在、`exit_code == 0`、`timed_out == false`。不满足时验收记录仍保存，但 `status` 改为 `unverified`，并附 `requested_status: "passed"` 与 `evidence_gaps: [{"check", "reason": "missing|failed|timed_out|manifest_mismatch|no_report"}]`；写 `job.evidence_gap` 事件。满足时验收记录附 `evidence: {"attempt_id", "manifest_sha256", "checks": [name...]}`。job.acceptance 透出 `requested_status`、`evidence_gaps`。`publish.requires_acceptance` 仍只认 passed。

**11.8 任务级协调策略（controller）**：contract 可选 `coordination: {"policy": "always|on_failure|none"}`，缺省 always（即 §10 现有行为）。on_failure 只在目标进入 `waiting_reconcile`、`failed`、`waiting_capability`，或以 completed 结束但交付 attempt 带 `recovered_from_attempt`（发生过接管）时触发；none 从不触发受管协调。修复契约继承目标的 coordination。
- 触发去重键改为 `目标 job_id + 触发类型 + durable_checkpoint_id + acceptance.status`；同一目标已有未结束的协调 job 时不再创建新的。取消请求或根预算耗尽后不派生修复（已有 budget_reason 与 cancel 检查须覆盖受管修复路径）。

**11.9 能力证据（controller）**：node 记录增加 `capability_evidence: {"<harness>": {"status": "ok|degraded|unproven", "bound": {"agent_version", "harness_version", "harness_path"}, "last_success": {"attempt_id", "at"}|null, "last_failure": {"attempt_id", "at", "reason"}|null}}`。
- 每个 attempt（含 stub 与协调 job）结束时更新该 attempt 所用 harness：succeeded 且（无 checks 或检查全过）记为 success；succeeded 但检查失败/缺失记为 failure(`checks_failed`)；其他失败记为 failure(failure_class)，`user_cancel`、`node_lost` 不计。
- bound 与节点当前上报值不同则视为 `unproven`（旧证据不沿用）；否则最新一条为 success 时 `ok`，为 failure 时 `degraded`。仅用于展示与解释，不改变调度过滤（撤销能力用 11.1 的 disabled_harnesses）。

**11.10 源目录回写与执行节点解耦**：contract.integration.target_node_id 非空时，回写不再要求由执行节点完成。
- 交付发布成功后，controller 为该根工作包登记待回写项（每个根工作包同一目标只保留最新一项，旧的待办被新交付替代）：`{"job_id", "checkpoint_id", "manifest_sha256", "base_manifest_sha256", "target_path", "exclude"}`；base 取同根同目标最近一次 `integrated` 的 manifest，没有则取根 job 的 inputs_manifest。job.integration_status 置 `integration_pending`。
- 心跳响应增加 `integrations: [...]`，只发给 target_node_id 对应节点；节点用既有 `integrate()` 回写（只检查本次改动路径、先备份、冲突不覆盖），在本地数据目录 `integrations/<job_id>/<checkpoint_id>/` 持久化进度，结果经既有 `POST /jobs/{id}/integration` 上报。已开始但结果未知的回写不重复执行，上报 `conflict` 并要求人工复核。
- 目标节点离线时 job 仍为 completed、交付与工件保存在 controller，integration_status 保持 `integration_pending`；节点恢复后按上述基线检查再回写。

**11.11 Claude 协调说明语言**：claude 适配器提示词要求 method、result、notes、follow_up 的 title/goal/acceptance/instructions 使用简体中文；结构化枚举值与字段名不变。

## 12. 不在 v1 承诺范围

活进程迁移、模型内部推理状态、未上传的本地改动、任意 Shell 或模型调用的 exactly-once、控制面高可用。见 README 的保障说明。
