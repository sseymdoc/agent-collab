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

桌面端、CLI 与其他自动化客户端共用本节的端点和授权模型。controller 在 `GET /client`、`GET /overview` 的 `controller.features` 和节点注册响应的 `features` 中列出已支持的能力名：`clients`、`model_settings`、`node_models`、`contract_draft`、`baselines`。客户端和节点只在对端列出相应能力后使用对应字段。

**12.1 客户端凭据**

| 主体 | 凭据 | 用途 |
| --- | --- | --- |
| 客户端（桌面、CLI、Agent） | `Authorization: Bearer <client_token>`；配对时签发一次，controller 只存 SHA-256 | 按授予的权限访问客户端端点；与节点令牌、管理令牌互不通用，可单独撤销 |
| 客户端配对码 | `controller client-code --data-dir D [--role R \| --scopes a,b] [--ttl 600]`（controller 本机），或 `POST /clients/codes`；一次性，只存哈希，缺省 10 分钟、最长 1 小时 | 只能调用 `POST /clients/pair` |

权限（scope）：

| scope | 允许 |
| --- | --- |
| `read` | 除 `GET /clients`、`GET /doctor`（需要 `maintain`）外的全部 GET：任务、事件与事件流、检查点与文件清单、blob、节点、总览、调度解释、协调租约、模型设置与目录、模板、输入基线 |
| `tasks` | 创建任务、契约草拟、取消、重试、验收、发布、回写结果、协调租约取得与释放、上传 blob、登记输入基线 |
| `settings` | 修改模型设置 |
| `devices` | 节点排空与迁移 |
| `maintain` | 暂停与恢复派单、doctor、客户端列表与撤销、签发客户端配对码、接管协调租约 |

角色是 scope 的固定组合：`owner` 为全部五项，`operator` 为 `read`+`tasks`，`viewer` 为 `read`。权限在每个路由上由 controller 检查；缺少权限返回 403 `forbidden`，`details.required_scope` 给出所需权限。管理令牌与浏览器会话等同于全部权限。节点令牌不能调用客户端端点。

- `POST /clients/pair`（无需认证）`{"code", "name", "kind": "desktop|cli|agent"}` → 200 `{"client_id", "client_token", "name", "kind", "scopes", "controller_id", "authority_generation", "controller": {"version", "git_commit"}, "features"}`。配对码用后即废；`client_token` 只在这次响应里出现。
- `GET /client`（任意客户端凭据、管理令牌或会话）→ `{"identity": {"kind": "client|admin|session", "client_id", "name", "scopes"}, "controller": {"controller_id", "authority_generation", "version", "git_commit", "features"}}`。
- `DELETE /client`（客户端凭据）→ 撤销调用者自己。
- `GET /clients`（maintain）→ `{"clients": [client...]}`；client 为 `{"client_id", "name", "kind", "scopes", "created_at", "revoked_at", "version"}`，不含凭据。
- `POST /clients/codes`（maintain）`{"request_id", "role" | "scopes", "ttl_s"}` → 201 `{"code", "scopes", "expires_at"}`；响应不进入幂等回执，重复的 request_id 返回 409。客户端凭据只能签发不超出自身 scopes 的配对码，否则返回 403 `forbidden`，`details.required_scope` 为第一个缺少的 scope；管理令牌与会话不受此限。
- `POST /clients/{client_id}/revoke`（maintain）`{"request_id", "expected_version"}` → 更新后的 client。

撤销从下一次请求起生效，不影响节点凭据和其他客户端。已建立的事件流在下一个轮询周期（缺省 0.25 秒）重新认证，收到一帧 `event: unauthorized` 后被服务端关闭。

所有 `/api/v1` 响应带 `X-Controller-Id: <controller_id>`。客户端把连接绑定到配对时得到的 controller_id；响应里的值不同即停止使用该连接。不带该头的 5xx 视为前置代理代答，按网络错误处理，不算身份不符。

浏览器控制台（`POST /session`、Cookie、CSRF、Origin 检查与静态页面）在配置 `web_console` 为 true（缺省）时保持原行为；设为 false 后 `POST /session` 返回 403，不再提供静态页面，已有会话失效。它是迁移期的兼容入口，退出条件见 `docs/migration.md`。

**12.2 请求来源**：新建的 job 记录带 `created_by: {"kind": "client|admin|session|controller", "client_id", "name"}`（旧记录没有此字段）。每个带 `request_id` 幂等回执的客户端变更请求在实际执行时（重放不计）写一条 `audit.action` 事件：`{"actor": {...同上}, "action": "<METHOD> <路由>", "request_id"}`，与任务有关的记在该任务名下。签发配对码同样写这条事件；模型设置的修改记在 `settings.updated` 与修改历史里，客户端配对与撤销记为 `client.paired`、`client.revoked`。

**12.3 模型设置**（controller 保存，记录 `settings/models`）

```json
{"revision": 0, "updated_at": null, "updated_by": null,
 "codex": {"default": {"model": null, "effort": null}, "allowed": null},
 "claude": {"default": {"model": null, "effort": null}}}
```

- `codex.default`：执行任务的工作区默认值；null 表示未设置。
- `codex.allowed`：部署策略。null 表示部署不作限制；否则为 `{"<model>": ["<effort>", ...]}`，只有列出的组合可用，默认值必须在其中。
- `claude.default`：受管协调回合的模型与 effort；null 时沿用 `config.json` 中 `standby_profiles` 所选 profile 的值。两组设置互不覆盖。
- model 匹配 `[A-Za-z0-9][A-Za-z0-9._:/-]{0,95}`，effort 匹配 `[a-z][a-z0-9_-]{0,31}`。
- 新建的 controller 数据库从上述未设置状态开始。已有任务或节点记录、但没有该记录的数据库（升级前的部署）在启动时写入一次迁移值：`codex.default = gpt-6-astra / xhigh`，`codex.allowed = {"gpt-6-astra": ["high", "xhigh", "max"]}`，`claude.default` 取当时生效的 standby profile；`updated_by.kind` 为 `migration`。之后与其他设置一样可编辑。
- 项目默认层尚未实现；优先级预留为 任务覆盖 > 项目默认 > 工作区默认。

端点：

- `GET /settings/models`（read）→ `{"settings", "catalog", "selectable", "history"}`。
  - `catalog.nodes.<node_id>.<harness>` = `{"status": "ok|unknown|legacy", "source", "fetched_at", "cli_version", "reason", "models": [{"id", "efforts": [...], "default_effort"}]}`，见 12.5。
  - `selectable.codex` = `[{"model", "efforts": [...], "nodes": [node_id...]}]`：部署策略与各节点目录（status 为 ok 或 legacy）的交集；`selectable.unknown_nodes` 列出目录未知的节点。目录条目来自节点上的 CLI，不表示该账号已成功调用过这个模型。
  - `history`：最近 20 次修改 `{"revision", "at", "actor", "before", "after"}`。
- `PUT /settings/models`（settings）`{"expected_revision", "codex"?, "claude"?}` → 更新后的 settings；`expected_revision` 与当前不同返回 409 `version_mismatch`，`details.current` 为当前 settings。写 `settings.updated` 事件。

**12.4 受理时解析与冻结**

- `harness: codex` 的契约可以省略 `harness_params.model` 与 `effort`。受理时逐项解析：请求里显式给出的值 → 工作区默认值；同一根工作包内的后续修订改用根冻结的默认值。解析不出返回 400，`details.reason` 为 `model_default_unset`；解析结果不在许可范围返回 400，`details.reason` 为 `model_not_allowed`。同一根工作包的后续修订若显式给出 model，必须与根冻结的 model 相同（冻结的 model 为 null 时不限），否则同样返回 400 `model_not_allowed`；effort 可在冻结的许可范围内覆盖。不会换成别的模型或 effort。
- 解析结果写入 job 的 `contract.harness_params`（与此前由固定常量填充的位置相同），受理后不变；之后修改设置不影响已受理的 job，包括排队中的。
- job 记录带 `model_policy`：`{"schema": 1, "settings_revision", "requested": {"model", "effort"}, "resolved": {"model", "effort"}, "source": {"model", "effort"}}`。`requested` 是请求显式给出的值（没有则为 null）；`source` 取 `task_override`、`workspace_default`、`root_frozen`、`coordinator_override`、`legacy_compat`（`project_default` 预留）。升级前受理的 job 没有此字段；`request_hash` 与既有契约内容不重算。
- 根工作包记录带冻结策略 `model_policy`：`{"schema": 1, "settings_revision", "codex": {"model", "effort", "allowed": {...}|null}}`，取根 job 受理时的解析结果与当时的 `codex.allowed`；根 job 不是 codex 时取当时的工作区默认值（未设置则 model、effort 为 null）。
- 受管修复只读根的冻结策略：模型固定为冻结的 model；`follow_up.effort` 为 null 时用冻结的 effort，给出值时必须在冻结的许可范围内（`allowed` 为 null 时不限），并在子 job 的 `model_policy.source.effort` 记 `coordinator_override`。不在范围内时拒绝该修复请求（`coordinator.follow_up_rejected`，原因 `effort_not_allowed`），验收结论保留。冻结的 model 为 null 时修复请求以 `model_default_unset` 被拒绝。根工作包的累计预算规则不变。
- 升级前创建的根没有冻结策略，读取时按原行为推导：model 与 effort 取根 job 的 codex 参数（根不是 codex 时为 `gpt-6-astra` / `xhigh`），`allowed` 为 `{"<model>": ["high", "xhigh", "max"]}`，来源记 `legacy_compat`。
- 协调决策 schema 的 `follow_up.effort` 为 `string | null`，不带枚举；`context.json` 增加 `repair_policy: {"model", "effort", "allowed_efforts"}` 供协调者参考。节点同时接受此前的 schema（effort 必填、枚举 high/xhigh/max），以便先升级节点、后升级 controller。
- 受管协调回合在创建时读取当时的 `claude.default`（或 profile 值）并写入自己的契约。

**12.5 节点模型目录**

- 节点注册可带 `capabilities.models`：`{"codex": {"status": "ok|unknown", "source", "fetched_at", "cli_version", "reason", "models": [{"id", "efforts": [...], "default_effort"}]}, "claude": {...}}`。codex 目录由节点在自己的服务进程上下文里启动 `codex app-server`（stdio，不监听网络端口），发送 `initialize` 与 `model/list` 取得，按 `model_catalog_s`（缺省 21600 秒）刷新；取不到时 `status` 为 `unknown` 并给出 `reason`。claude CLI 没有目录接口，`status` 为 `unknown`、`reason` 为 `no_catalog_interface`。读取目录不调用模型。
- 节点第一次注册不带 `models`；注册响应的 `features` 含 `node_models` 时再带上它重新注册。节点与 controller 断线重连后重新注册，以重新取得 `features`（controller 原地升级时恢复代次不变，心跳不会提示能力变化）。
- 没有上报 `models` 的节点（旧版本）按其适配器实际接受的固定集合处理：`status: legacy`，`gpt-6-astra` 配 `high`、`xhigh`、`max`。
- 调度过滤（§6）：codex 任务的 model 不在节点目录里记 `model_unsupported:<model>`，effort 不在该模型的列表里记 `effort_unsupported:<model>/<effort>`；只对 status 为 ok 或 legacy 的目录判断。目录 unknown 的节点不因此被排除，调度解释里该节点带 `model_catalog: "unknown"`。

**12.6 模板、契约草拟与输入基线**

- `GET /templates`（read）→ `{"templates": [{"id", "title", "description", "harness", "diagnostic"}]}`。当前模板：`codex-change`（Codex 修改并运行检查；失败或接管时才触发协调，结果自动发布）、`codex-change-accept`（执行并验收：`publish = {"mode": "manual", "requires_acceptance": true}`、`coordination.policy = always`，验收通过后由客户端发布）、`diagnostic`（确定性步骤，不调用模型，用于检查 controller、节点、检查与工件链路）。
- `POST /contracts/draft`（tasks）`{"template", "title"?, "goal", "acceptance"?: [...], "excludes"?: [...], "baseline_id"? | "inputs_manifest"?, "checks"?: [{"name", "run", "timeout_s"?}], "model"?, "effort"?, "requirements"?}` → `{"title", "contract", "preview", "model_policy", "settings_revision", "warnings"}`。只生成并校验契约，不改变状态。`contract` 是可原样用于 `POST /jobs` 的请求（只含显式选择的 model、effort）；`preview` 是按当前设置解析并补全默认值后的完整契约，`model_policy` 是对应的解析结果。`warnings` 为 `[{"code", "detail"}]`：`empty_inputs`（没有输入基线）、`missing_blobs`（基线有文件未上传）、`no_node_supports_model`（已登记节点的目录都不含该组合）。
- `POST /jobs` 可带 `expected_settings_revision`：与当前模型设置的 revision 不同时返回 409 `version_mismatch`（`details.current` 为当前设置），用于保证受理时的解析结果就是草拟时看到的那一份。
- `GET /jobs` 可带 `work_package_id=<根工作包>`，只列出该根的各次修订（新的在前）。
- `POST /baselines`（tasks）`{"request_id", "name", "manifest"}` → 201 baseline；`GET /baselines`（read）→ `{"baselines": [...]}`。baseline 为 `{"baseline_id", "name", "manifest_sha256", "files", "bytes", "missing_blobs", "created_at", "created_by"}`；manifest 的 `base` 必须为 null，文件内容经 `PUT /blobs/<sha256>` 上传。基线及其 blob 计入备份。

**12.7 读取补充**

- `GET /jobs/{id}/events?before=<seq>&limit=<n>`（read）→ `{"events": [...]}`：该 job 序号小于 `before` 的事件，按序号升序，最多 `limit` 条（1–1000，缺省 200）。
- `GET /nodes` 的每个节点带 `attempts`：该节点上有效（assigned、leased、running）的 attempt，`[{"attempt_id", "job_id", "title", "status", "epoch", "lease_expires_at", "harness", "model", "effort", "coordinates"}]`；`coordinates` 为 true 表示它是受管协调回合。
- `GET /checkpoints/{id}/files`（read）→ `{"checkpoint_id", "manifest_sha256", "files": [{"path", "sha256", "size", "executable"}]}`；文件内容用 `GET /blobs/<sha256>` 读取。

## 13. 不在 v1 承诺范围

活进程迁移、模型内部推理状态、未上传的本地改动、任意 Shell 或模型调用的 exactly-once、控制面高可用。见 README 的保障说明。
