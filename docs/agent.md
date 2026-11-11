# Node agent（WP2）

Python 3.9+，仅标准库；主动连接控制端，不监听入站端口，不需要 SSH。
协议来源为 `docs/protocol.md` 和 `agent_collab/protocol.py`。本文实现未运行真实模型。

## 命令

```sh
python3 -m agent_collab.node_agent pair \
  --controller https://controller.example \
  --code ONE_TIME_CODE --node-id mac-worker --data-dir /absolute/agent-data \
  --physical-host-id desktop-1 --failure-domain home --slots 1 --host-slots 1 --label local
python3 -m agent_collab.node_agent detect
python3 -m agent_collab.node_agent detect --data-dir /absolute/agent-data
python3 -m agent_collab.node_agent doctor --data-dir /absolute/agent-data
python3 -m agent_collab.node_agent run --data-dir /absolute/agent-data
```

自建 CA 使用 `pair --ca-file /absolute/ca.pem`。HTTPS 始终验证证书和主机名；只有
`localhost` 或字面 loopback IP 允许 HTTP。客户端不跟随重定向，不转发认证到其他源。
`detect` 仅调用工具的版本命令和 `nvidia-smi`，不启动模型回合；Codex/Claude 不存在、被禁用或版本探测失败均为 null。
其他通用工具找到但读不到版本时为 unknown。`--data-dir` 可读取与服务相同的 agent.json 配置。
内置 stub 始终上报；GPU 使用 `{name, cuda}` 结构。
`doctor` 检查配置、令牌匹配、POSIX 权限、控制端健康检查、实际工作区根目录写入和 harness 版本。
它还运行无模型的认证状态探测；stub 可用而 Codex 缺失时会单独显示 `harness.codex=false`。
认证结果按执行上下文分开给出：`auth_health` 是本次 doctor 进程自己的探测，`auth_source` 写明它的上下文
（`context=doctor_process`、是否经 SSH 调用的 `via_ssh`、探测时间 `checked_at`、每个 harness 的 `reasons`）；
`worker_auth` 是 worker 服务进程随心跳写入 status.json 的结果（`context=worker_service`、`auth_health`、`checked_at`、`pid`、
`last_heartbeat_ok_at`），没有 status.json 或 worker 版本较旧没写这两项时为 null。doctor 只输出，不向 controller 上报，
它的结果不会改动 controller 上由 worker 上报的 `auth_health`。`reasons` 的取值：`logged_in`、`not_logged_in`、
`credential_store_unreadable`（当前进程读不到凭据存储）、`probe_inconclusive`（输出无法识别）、`probe_failed`（命令失败或超时）、
`command_missing`。
Windows 工作区位于 USERPROFILE 内且 Codex 可用时，在 warnings 中报告 `workspace_in_profile`。
`doctor` 还检查 attempt 租约文件及 POSIX 权限，并在 `restart_blockers` 中列出阻塞领取的
attempt、PID、记录的创建时间、meta.json 路径和可执行的进程查询命令；不输出凭据原值。
`effective` 给出不含密钥的最终生效配置：`node`（身份、并发、标签、workspace_root、data_dir）、`agent`（版本、提交、
版本目录、解释器、用户、平台）和每个 harness 的 `argv`、`version`、`disabled`、`path_prepend`、`managed_argv`
（Codex 另有 `codex_home` 与被清除的环境变量名，Claude 另有适配器固定设置的 `managed_env`）。`managed_argv` 直接调用适配器的 `build_argv` 生成，
`<work>`、`<attempt>`、`<sandbox>`、`<effort>`、`<profile.model>`、`<profile.effort>`、`<decision-schema>`、`<session-id>`
是运行期才确定的值；环境变量只列名称，不列取值。`argv` 与 `managed_argv` 只原样显示可执行文件路径：
`codex_command`、`claude_command` 里附带的其余参数可能含凭据，一律显示为 `<configured-argument>`。
`agent.json` 里的值类型不对、无法生成 `effective` 时，`effective` 只含 `error_type`，`checks.configuration` 记为失败，其余检查照常返回。

`run` 默认派生脱离当前会话的后台 agent，将输出写入 `agent.log`，返回 `spawned` 与 PID；
`spawned` 仅表示进程已创建，后续错误见日志。`--foreground` 用于服务管理器；
`--once` 前台执行一次领取，处理该任务到完成上报或隔离；没有任务时退出。
正常终止可向后台 PID 发送 SIGTERM，agent 会停止其托管进程。
单个数据目录由 OS 文件锁独占。后台模式不依赖发起 shell/SSH，但不提供开机自启或崩溃重启；
installer、系统服务、长时间运行与整机重启验收属于后续工作包。

## agent.json 与目录

配对写入 `agent.json` 与 `node.token`，POSIX 均为 0600，已配对目录拒绝覆盖。
配置字段：

| 字段 | 用途 |
| --- | --- |
| controller / ca_file | 控制端 origin 与可选 CA 文件绝对路径 |
| node_id / controller_id / authority_generation | 配对身份；注册刷新运行中的 generation |
| node_token_sha256 | `node.token` 的 SHA-256，用 `hmac.compare_digest` 校验 |
| physical_host_id / failure_domain | 物理宿主和故障域；默认本机主机名 |
| slots / host_slots / labels | 节点、宿主并发额度与附加能力标签 |
| codex_command / claude_command | 可选，绝对可执行路径字符串或 argv 列表（首项为绝对路径）；不经过 shell |
| codex_home | 可选，传给 Codex 执行和探测的 CODEX_HOME；不复制认证 |
| disabled_harnesses | `{harness: 原因}`，撤销对应能力并禁止执行；透出 harness_disabled |
| workspace_root | 可选，工作区改为 `<workspace_root>/<attempt_id>`；元数据和输出仍在 data 下 |
| codex_windows_sandbox | Windows Codex 的 windows.sandbox，缺省 elevated |
| auth_probe_s | 认证状态探测周期，缺省 900 秒；启动探测，后续后台探测，不阻塞心跳 |
| provider_account_refs | 可选，非凭据账户分组；auth_health 由实际探测产生，旧配置中的静态 auth_health 不再作为探测结果 |

同一物理主机上的多个节点（例如 Windows 原生节点与按需启用的 WSL 节点）应显式使用相同 `physical_host_id` 和 `host_slots`。
默认每台物理主机只有一个原生节点，WSL 节点会带 `wsl` 标签。
Codex 模型固定为 `gpt-6-astra`；effort 仅 `high/xhigh/max`，契约不写 effort 时取 `xhigh`，其他取值在提交时被拒绝。Claude 的 model/effort 必须来自 controller 的已配置 standby profile，不使用默认档位。

每个进程启动时只解析一次 harness argv：服务 PATH → `~/.local/bin` → macOS 的 `/opt/homebrew/bin`、
`/usr/local/bin`。Windows 使用 PATHEXT，排除 WindowsApps 应用执行别名。探测与执行共用绝对 argv 和环境，
PATH 前置该可执行文件目录，不加载 shell rc；Codex 线程来源变量被清除，显式 codex_home 同时用于所有探测。
`capabilities.harness_paths` 给出路径或 null；tools 与 harness 的版本来自同一次探测。
启动时同步调用一次 `codex login status` / `claude auth status`（各 20 秒超时，最坏约 40 秒后才进入主循环），
之后每隔 `auth_probe_s`（缺省 900 秒）在后台调用，只保留 ok/expired/unknown，
不保存账户标识、凭据或命令输出。超时及无法识别的结果为 unknown，明确未登录为 expired。
macOS 上 Claude 的订阅登录存在登录钥匙串里，只有图形登录会话能读：`claude auth status` 报未登录时再运行
`security show-keychain-info`，钥匙串读不到（SSH 会话里 exit 36）就记为 unknown，原因 `credential_store_unreadable`，
不据此判定凭据过期；钥匙串可读而 CLI 仍报未登录才是 expired。其他平台的凭据是文件，不做这一步。
worker 若运行在读不到钥匙串的上下文里，同样上报 unknown；派到它的 Claude 回合会以 `auth_expired` 失败，
controller 再依据这次实际失败标记 expired。
Codex 执行增加 `--ignore-user-config`，Windows 显式传入 `-c windows.sandbox="elevated"`（可配置）。
已有 model、effort、approval_policy、sandbox_workspace_write 隔离参数继续显式传递。
`--ignore-user-config` 跳过整个用户 config.toml，不只是 MCP 与 notify：模型、effort、沙箱模式、审批策略、工作目录、
可写根与网络开关全部来自上述受管参数；provider 用 Codex 缺省值，认证用 CODEX_HOME 中的官方本机登录。
用户配置里的 service_tier、插件和其他键在受管执行中不生效。这个开关不等于隔离全部指引：
工作区内的 AGENTS.md 与 CODEX_HOME 下的全局指引是否加载取决于 Codex 自身，本项目没有验证，也不对此作保证。

`release_info()` 从运行代码根目录的 release-manifest.json 获取 version/git_commit；缺失或无效时回退
0.1.0.dev0/null。注册 agent_version、capabilities.agent、attempt.runtime.agent_version/agent_commit 均采用它。
每次成功心跳原子写入 status.json，包含 version、git_commit、pid、version_dir、last_heartbeat_ok_at，
以及本服务进程最近一次认证探测的 auth_health 与 auth_checked_at。

```text
agent-data/
  agent.json, node.token, agent.lock, agent.log, status.json
  blobs/<sha256>                  # 不可变的本地上传/下载缓存
  attempts/<attempt_id>/
    meta.json                    # assignment、令牌哈希、checkpoint/complete/publish outbox
    lease.token                  # 此 attempt 的 Bearer 租约原值，原子写入，POSIX 0600
    events.jsonl                 # 未确认事件，原 event_id 重发
    work/                        # 未配置 workspace_root 时使用；否则为 <workspace_root>/<id>
    quarantined.json             # 隔离原因与停止确认状态；保留全部工作文件
    stdin.txt, stdout.jsonl, stderr.log, result.txt   # Codex 本地运行文件
    step-<index>/                # stub run 的托管进程日志
    checks/<序号>/               # 契约检查的本地进程输入/输出（按序号建目录，检查名不参与路径）
  integrations/<job_id>/<checkpoint_id>/
    progress.json                # pending、写入意图、固定回执和上报确认
    backup/                      # 回写前原文件备份
```

配置、事件、元数据、agent 日志、doctor 和 HTTP 错误不写节点或租约明文令牌。
agent 使用 `node.token` 和 `attempts/<id>/lease.token` 保存发起认证所需的 Bearer 原值；
控制端和元数据中的校验记录只保存 SHA-256，比较用 `hmac.compare_digest`。模型 provider 登录不复制。
运行日志仅来自本机 harness；本产品不把它们自动上传为事件。

WP5 在 complete.report 上传有限的执行证据：stub 每个已触及步骤的状态、run 退出码及两个各 ≤4 KiB 的输出尾部，
检查点跳过的步骤标为 skipped_from_checkpoint；Codex 的 result.txt 尾部 ≤16 KiB、实际 session/usage/CLI error；
Claude 的有效 decision 和 notes。未知值用 null，未执行步骤不伪造结果。报告先经 agent.clean 脱敏，再限制 canonical JSON
总计 ≤64 KiB；发生局部/整体截断均标 truncated=true，整体过大时可能减少 steps 或清空过大的 usage/cli_error。
报告随 complete outbox 保存，重启重放不重新读取输出。controller 原样保存报告，不把 usage 中的 token 计数当成凭据移除。

rc5 在 harness 成功之后按顺序执行 contract.checks：无 shell、空 stdin、工作区 cwd、服务环境，
`{python}` 替换为 agent 解释器。检查由主循环轮询，心跳与取消继续工作；超时停止整个进程组。
失败/超时/无法启动均记为检查证据，不改变成功的 harness outcome；无法确认检查进程停止时则隔离工作区。
信任边界：检查以节点服务身份运行，**不在** Codex 沙箱内，可联网并读取该账户可读的文件（含 node.token、
lease.token）。检查若导入模型刚写出的工作区代码（如 `{python} -m unittest`），等同以节点身份执行模型产物。
只对可信契约启用会执行工作区代码的检查；隔离执行（独立低权限账户或 OS 沙箱）尚未实现，见已知限制。
全部检查结束后才生成 final，report.checks 绑定该 manifest 的哈希，并记录节点、平台、argv、退出码、
超时、耗时和脱敏输出尾部（各 ≤4 KiB）。harness 失败时不运行检查，checks=null。
成功但未配置检查时 results=[]。64 KiB 总限制优先压缩检查尾部；极端超大 argv 仍可能使检查证据被清空，
此时 controller 应按缺证据处理，不能当作通过。检查原始本地输出与 harness 日志一样只保存在节点。

## Claude 受管协调适配器（WP4）

controller 生成 harness=claude 的协调契约，harness_params 明确给出 model/effort，并在 inputs_manifest 提供 context.json 和 decision-schema.json。节点在独立 attempt/work 中物化后调用：

```text
[claude_command, -p, --model, <profile.model>, --effort, <profile.effort>,
 --output-format, json, --json-schema, <内联 JSON>, --permission-mode, dontAsk,
 --tools, "", --session-id, <新 UUID>, --strict-mcp-config, --setting-sources, "", --safe-mode]
```

环境变量另加 `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`，其余环境沿用节点解析出的命令环境。

各项的作用范围不同，合起来才是“任务相关输入只来自契约”：`--tools ""` 只关闭内置工具，不关 MCP 工具；
`--strict-mcp-config` 不加载任何 MCP 服务（含账户级连接器）；`--setting-sources ""` 不加载 user、project、local 三个来源的设置，
因此用户级 settings、插件、hooks、各级 CLAUDE.md 与 CLAUDE.local.md、工作目录的 `.claude/settings.local.json` 都不进入受管回合
（受管回合用不到任何一个来源，所以列表为空）；自动记忆（`~/.claude/projects/<项目>/memory/`）不受来源列表控制，
由 `--safe-mode` 与上面的环境变量关闭；`--safe-mode` 同时关闭记忆文件、skills、插件、hooks 等定制。
以下内容仍然存在，属于预期：Claude Code 自己的系统提示、内置的插件与斜杠命令清单（没有工具，无法调用）、
`~/.claude.json` 全局配置、官方本机登录，以及管理员下发的托管策略（其中配置的 hooks 仍会运行）。
认证仍用官方本机登录（不使用 `--bare`，它读不到订阅登录）。

实测（Claude Code 2.1.302 与 2.1.321，工作目录与上级目录各放带标记的 CLAUDE.local.md，对应项目的自动记忆目录放带标记的 MEMORY.md，
核对 init 事件与回答）：只有 `--tools ""` 时回合会连接账户的邮件、笔记等连接器，加载用户插件并运行 SessionStart/UserPromptSubmit hooks；
`--setting-sources project` 仍会带入用户 CLAUDE.md；`--strict-mcp-config --setting-sources local` 下工具与 MCP 列表为空、没有用户插件和 hooks，
但两个 CLAUDE.local.md 标记和自动记忆标记都被带入；`--setting-sources ""` 不再带入 CLAUDE.local.md，自动记忆仍在；
加上 `--safe-mode`，或只加环境变量，自动记忆不再加载；现行组合下三个标记都不出现，订阅登录正常。
工作目录自己的 `.claude/settings.local.json` 里的 hooks 在 `--setting-sources local` 且没有 `--safe-mode` 时会运行。
协调工作区里若出现 `.claude`，适配器仍然不启动 Claude 并以 `code_failure` 结束。
本机 Claude Code 必须支持这些参数，不支持的旧版本不能用于协调回合。

这些都是 Claude Code 的命令行参数，不构成操作系统级隔离。提示词要求协调者只依据提供的上下文判断、不声称没看到的检查，
证据不足或没有检查点时选择 `escalate`；结构化决策本身不是独立验证，目标契约声明了 checks 时，`passed` 是否成立由 controller
按节点执行的检查证据核对（protocol §11.7）。

prompt 经 stdin 传入，context.json 全文内嵌在 prompt 末尾，要求仅返回结构化决策；`--tools ""` 不授予任何工具（含读文件）。适配器只解析 stdout JSON 的 structured_output，不从 result 自由文本猜测 JSON。固定 schema 由 controller/adapter 共用并在本地复核：follow_up 只是修复请求 `{title, goal, acceptance, instructions, effort}`，不含完整契约；完整的 codex 修复契约由 controller 基于被验收检查点与目标契约派生。结构化输出通过 schema 即由 agent 原子写入 decision.json，再走已有 final checkpoint 上传和 complete outbox；不合 schema 的结果失败且不写该文件。依赖目标状态的校验（租约归属、检查点、预算、副作用）只在 controller 进行：验收结论合法时一定记录，修复请求无法生成合法契约时写 coordinator.follow_up_rejected 而不是丢弃整个决策。

进程使用现有 OwnedProcess 生命周期管理；每次协调创建新的 session UUID，不 resume 旧 Claude 会话，不读取任何登录文件或 transcript。runtime.model/effort 记录请求的配置值，runtime.observed 为 null，不将请求值冒充模型实际观测。进程输出包含认证失败时分类 auth_expired；controller 可据此标记 claude 的 auth_health 为 expired。认证恢复沿用节点报告 ok/管理员处理的既有机制。

Claude 与 Codex 使用相同命令解析规则。提示词要求 method、result、notes 以及 follow_up 的
title/goal/acceptance/instructions 使用简体中文，字段名与结构化枚举保持不变；未调用真实模型验证语言遵循情况。

`tests/test_agent_claude.py` 使用注入 process_factory 和本地 Python 桩子进程验证 argv、stdin、结构化输出、认证错误、final checkpoint 及 controller 应用；未运行真实 Claude/Codex，未证明真实 CLI 权限约束、登录可用性、Windows/WSL 或跨机接管。


## 执行、恢复与回写

启动先注册、带本地 attempt 列表心跳，之后才能领取。心跳按配置加 ±20% 抖动，网络失败使用
基数 1 秒、上限 60 秒的指数 full jitter。网络等待期间仍轮询本地 harness；恢复后先心跳对账。
一个任务的全部步骤及其模型进程都在分配给它的那一个 attempt 内运行（租约与恢复都以 attempt 为界）；
attempt 被撤销或 fenced 后停止其进程组、隔离工作区、禁止发布。
事件仍可作为 stale 归档。`reconcile_required` 时隔离旧执行，刷新注册后再次心跳，期间不领取。

重启不重新执行旧 harness，按持久化事实处理：

- `finished_unreported`：读取并校验 lease.token，心跳仍报告 finished_unreported；通过对账后
  补传尚未确认的检查点、complete、publish、integration。已确认 durable 的检查点不重复上传，
  已保存的 request_id/请求内容保持不变。已在控制端完成的 attempt 不再续租，允许根据成功对账补报，
  每次请求仍由控制端 fencing 裁决，被拒则隔离。
- `preparing/running`：每次启动子进程前先记录启动意图，启动后保存 `meta.process` 中的 PID 和
  创建时间戳，确认进程组停止后记录 stopped。stub 没有子进程或原进程已确认不存在时，标记
  `abandoned`，保留工作区，心跳报 unknown，让 controller 立即进入恢复决策；该记录不阻止新领取。
- 原进程仍存活或身份无法确认时，保留 `restart_unknown=true` 并暂停领取；agent 周期性重查，
  确认退出后转为 abandoned 并重新心跳，随后恢复领取。不会根据旧 PID 直接杀进程，避免 PID 重用误杀。
  日志和 doctor 提供 `ps` 或 PowerShell 查询命令；先核对创建时间、工作目录及后代进程，再处理原进程。

POSIX 查询权限不足时创建时间可能为 null，这表示 unknown，不视为已退出。若原进程退出且进程组
已消失，仍可自动解除阻塞。若崩溃发生在启动意图与 PID 落盘之间，则没有可靠 PID，保留人工核对；
本版不提供跳过进程证据的自动解除入口。

stub 的 `summary.completed` 使用 **从 0 开始**的步骤序号。checkpoint 步骤记录真实执行序号，
包括该 checkpoint 步骤自身，不信任步骤参数中未经执行的 completed 声明。
`summary.side_effects` 按稳定 `op_id` 保存最近状态。副作用类型非 `none` 时，本地租约过期后
停止推进步骤；POSIX 已运行进程组会 SIGSTOP，获续租后 SIGCONT。Windows 没有移植稳定的
进程树挂起机制，保守终止已有进程，重连后上报 infra_retryable，不盲目重放副作用。

快照保存完整文件列表与相对起始 manifest 的 deleted，二进制字节不转码、不转换换行；
符号链接、特殊文件、文件及目录组件的大小写别名均拒绝，形成 failure_detail。
POSIX 记录可执行位；Windows 沿用 base 值，新文件为 false。
contract.workspace.exclude 缺省为 `__pycache__/**`、`*.pyc`、`.pytest_cache/**`、`.DS_Store`。
自定义列表替换默认，[] 关闭；POSIX 相对 glob 从任意目录深度匹配，只有独立 ** 可以跨目录。
匹配的文件既不进入快照 files，也不进入 deleted；物化仍恢复输入 manifest 的全部文件。
自动回写按同一规则保留被排除的目标文件，避免完整集合 diff 把省略推断为删除。
上传用 HEAD 去重、PUT 校验，manifest 本身以 canonical_json 作为 blob；missing_blobs 重试
保留 checkpoint_id 和请求内容。每个 checkpoint 在本地缓存固定字节，后续工作区修改不会改变待传内容。
成功必须先 durable final checkpoint，再 complete，再按领取时 delivery_version 做一次逻辑发布。
网络响应丢失时重放同一 request_id；version_mismatch 和其他应用层发布错误不自动重试。

`artifacts.integrate(target_dir, base_manifest, result_manifest, fetch_blob, backup_dir)` 在改写前检查
本次结果相对 base 的 added/modified/deleted 路径；这些路径的哈希/缺失/已存在冲突返回 conflict 且不写目标。
未被本次结果修改的基线文件不参与冲突判断，也不备份或覆盖，保留目标中的人工修改或删除。
通过后先验证所有结果 blob，备份将覆盖/删除的原文件，再逐文件原子替换。
rc5 由 controller 在目标节点的心跳响应中下发 integrations，执行节点不再在发布后直接回写。
目标节点不需要持有原 attempt：下载 base/result manifests 与 blobs，上报 integration_pending，持久化写入意图，
执行回写并保存固定 request_id 回执，然后上报 integrated/conflict。丢失回执响应可跨重启重放，已完成项不再写入。
协议条目不含 target_node_id，目标身份由经过认证的心跳路由保证；若条目额外带目标字段，非本节点会忽略。
rc4 已有回执/写入意图会导入同一进度路径，避免升级后再次回写同一交付。
目标目录需由调用者独占；这不是跨文件事务，无法对抗同时修改目标的外部进程。
回写中途 I/O 失败或重启发现已开始但结果未知，保留备份、上报 conflict、不重复执行，要求人工核对。
工作区路径保存于 meta.json；配置变化后已存在的 attempt 保持原路径。外置工作区与旧布局一样，隔离/终止时保留，
没有自动删除 attempt/workspace 的 GC；人工清理时应同时检查 meta.json 的 work 指针。

## 协议缺口与已知限制

- 旧版本没有 lease.token 的 finished_unreported 无法恢复凭据，保守隔离；缺失或哈希不匹配的
  租约文件不会被当成有效凭据。旧元数据没有进程记录时，只有不含 run 的 stub 能确认无子进程；
  其他情况保留 unknown，需人工核对。
- §4 请求表没有列 authority_generation，但 fencing 规则要求该字段；agent 在 attempt 变更请求中
  显式携带 assignment 的 generation。主控集成时需确认 WP1 接受这一字段。
- rc5 节点需要配套 rc5 controller 接受能力/运行版本字段并在心跳提供 integrations；rc4 controller
  严格拒绝新增能力/runtime 字段。旧 App.handle 回归测试只通过测试专用桥移除这些字段；完整 rc5 报文另由桩验证，
  这不代表两工作包已完成实际集成。
- assignment 的 native_session_ref 没有原线程独占证明。Codex adapter 默认从文件检查点新建线程；
  argv 构造器支持稳定 launcher 的 resume 布局，但 agent 不自动 resume 未证明独占的线程。
- 新建/续接 argv、WindowsJob、POSIX 进程组停止、进程创建时间查询、事件解析和 observed 读取移植自
  `~/.claude/skills/codex-collaborate/scripts/launcher.py`，SHA-256
  `7862865fadc8c2a1514744eb33cb2ef93b58d1f6e58823df38ecabd01a774921`。
  `workflow.py`（SHA-256 `a2d2364486800f8491fe853a31b9daed23cc547d89a5a9d888b5d7f0ec8d4e3e`）
  作为只读事实/验收分离参考，未复制工作流、递归派单或依赖它的运行环境。
- 契约检查未沙箱化：以节点服务身份、完整服务环境与网络运行。需要隔离时应由独立低权限账户或 OS 沙箱
  执行检查，并让该账户无法读取节点数据目录；本版未实现。
- blob 与单次快照目前在内存中组装，没有分块传输或缓存 GC；适合受控小工作包。目录创建/删除本身
  不进入 manifest，仅跟踪文件。文件/目录形态切换可能保守报告冲突，跨平台非法文件名可能本地失败。
- 子进程继承本机账户权限，stub run 不是操作系统沙箱；只应用于受信测试命令。Codex 使用固定沙箱参数。
  本轮修订未运行真实 Codex、Windows Job Object、真实 TLS/网络、跨机恢复、全机断电或长期运行。
  主控此前已执行的 HTTP 闭环与故障演练证据由主控维护，不以本轮进程内测试替代。

## 测试与故障注入

```sh
/usr/bin/python3 -m unittest discover -s tests
```

`tests/fake_controller.py` 仅为进程内端点 fixture，不含生产调度逻辑。
本轮测试临时目录均在仓库 `.dev/tmp` 内，结束清理；新增测试不需要 socket，不访问网络，不调用模型。
Windows 专属测试在其他平台 skip；主控应在 Windows 复跑。
`test_agent_restart_controller.py` 使用真实 `App.handle` 做进程内重启集成验证；
主控的 `test_e2e_local.py` 需要 loopback socket，在本 worker 沙箱不可用时跳过。

**以下开关仅限隔离测试/演练，不得用于生产：**

- `AGENT_COLLAB_FAULT_PARTITION=/absolute/test-flag`：文件存在时 transport 抛连接错误；
  本地执行继续受租约/副作用规则限制。删除文件后按退避时机重连。
- `run --fault-exit-after-checkpoint N`：当前进程第 N 个收到 durable 确认并保存本地状态的 checkpoint
  后执行 `os._exit(137)`，不执行正常清理，模拟节点 agent 崩溃；不是整机关机测试。
