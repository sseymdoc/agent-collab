# 运维速查

安装、升级、回退、卸载的完整参数见 [install.md](install.md)。本页只列日常操作；`<CONTROLLER>` 为公开入口（如 `https://controller.example:8443`），`<DATA>` 为 controller 状态目录（Linux 默认 `/var/lib/agent-collab/controller`）。下述定时备份安装命令是待部署步骤，单元测试通过不代表已注册或已验证真实云端任务。

日常操作有两个入口，用的是同一组 API 和同一套授权（`docs/protocol.md` §12、§13）：桌面端（[desktop.md](desktop.md)），以及 `agent-collab` 的客户端命令。客户端命令先用 `client pair` 换取一次本机的客户端凭据，之后的 `submit`、`job`、`project`、`settings`、`api` 都用这份凭据，不需要 `--url` 与 `--token-file`；给自动化用的说明见 [integration.md](integration.md)。依赖管理令牌文件、浏览器控制台或远程执行通道的做法集中在文末“迁移前部署与应急入口”。

本页的 `"$PREFIX/bin/agent-collab"` 是安装器生成的入口（使用安装记录的解释器并从 `current` 运行）；
默认安装不会把 `agent_collab` 加入全局 Python 路径，在普通目录下 `python -m agent_collab` 无法导入。
运行前先把 `PREFIX` 设为安装时的 `--prefix`：Linux 受管 controller/all-in-one 默认 `PREFIX=/opt/agent-collab`，
其余 POSIX 安装默认 `PREFIX="$HOME/.local/share/agent-collab"`。Windows 的入口是 PowerShell 脚本
`<前缀>\bin\agent-collab.ps1`（未指定 `--prefix` 时为 `~\.local\share\agent-collab`；install.md 的示例用的是
`$env:LOCALAPPDATA\agent-collab\code`），写法见下方节点配对。

## 访问与身份

| 操作 | 做法 |
| --- | --- |
| 健康检查（无需认证） | `curl -fsS <CONTROLLER>/healthz` |
| 桌面端 | 打开桌面端，在“配对新的 controller”里填地址与配对码；凭据保存在系统凭据存储，可在“设置 → 客户端”里撤销 |
| CLI 客户端 | `"$PREFIX/bin/agent-collab" client pair --url <CONTROLLER> --code <配对码>`，之后的 `submit`、`job`、`project`、`settings`、`api` 等命令使用这份凭据（证书始终校验；非 loopback 必须 https；多个 controller 用 `--profile <名字>` 区分）；用法见 `docs/integration.md` |
| CLI 凭据后端 | 缺省保存在系统凭据存储（Windows 凭据管理器、macOS 登录钥匙串、Linux Secret Service）。没有图形会话的部署（经 SSH 运行的自动化、无人值守的服务器）在配对时显式加 `--credential-store file`，或设环境变量 `AGENT_COLLAB_CREDENTIAL_STORE=file`，凭据保存在仅当前用户可访问的文件里。两个后端之间不自动回退：系统存储不可用时 `client pair` 在发出配对请求之前报错，配对码没有使用 |
| 客户端配对码（桌面端、CLI、Agent） | 一次性，缺省 10 分钟有效。已有带 `maintain` 权限的连接时：在桌面端“设置 → 客户端”里生成，或调用 `POST /clients/codes`（只能签发不超出自身权限的配对码）。还没有任何客户端时，在 controller 主机上：`"$PREFIX/bin/agent-collab" controller client-code --data-dir <DATA> --role operator`；`--role` 可取 `owner`、`operator`、`viewer`，省略时为 `owner`（全部权限） |
| 权限 | `operator` 只有 `read` 与 `tasks`（提交、取消、重试、验收、发布、项目与输入）。修改模型设置、协调设置与项目默认值需要 `settings`；节点管理（节点配对码、暂停接单、维护、退役、探测）需要 `devices`；暂停派单、提交闸门、签发客户端配对码需要 `maintain`。配对码用 `--role owner`，或 `--scopes read,devices` 这样逐项指定。缺少权限时返回 403，`details.required_scope` 给出所需权限，凭据不受影响 |
| 当前身份 | `"$PREFIX/bin/agent-collab" client status`（身份、权限、controller 版本与能力）。撤销：`client forget --revoke`，或由拥有者在桌面端“设置 → 客户端”里撤销 |
| 总览 | 桌面端“总览”页；或 `"$PREFIX/bin/agent-collab" api GET /overview`（任务计数、节点、`dispatch_paused`、`intake`、`backup`、`managed_coordinator`） |
| doctor | `"$PREFIX/bin/agent-collab" doctor --prefix "$PREFIX" --role controller` |

## 提交任务与平台约束

任务经 controller 的 `POST /jobs` 提交。桌面端在“任务 → 新建任务”里选模板、写目标与验收项、添加检查，查看生成的完整契约后提交。命令行有两种写法：`agent-collab submit --template <模板> --goal <目标> ...` 由 controller 按模板草拟契约后提交（参数见 `docs/integration.md`）；`agent-collab submit <任务.json>` 提交现成的契约文件，与 `agent-collab api POST /jobs --data <JSON|@文件>` 是同一个接口。
提交方不需要是节点：任何配对了带 `tasks` 权限的客户端凭据的机器都可以提交，由 controller 按契约的 `requirements` 选择节点。
要把任务限定在某个平台或硬件上，写 `requirements`（按模板提交时用 `--os`），不要绕过 controller 直接登录目标机器启动模型：

```json
{"title": "只在 Windows 节点执行",
 "contract": {"goal": "…", "harness": "stub", "harness_params": {"steps": [{"run": ["{python}", "-c", "print('ok')"]}]},
              "inputs_manifest": {"version": 1, "base": null, "files": [], "deleted": []},
              "requirements": {"os": ["windows"], "arch": [], "tools": [], "gpu": null, "labels": []}}}
```

`recovery.auto` 缺省为 true，这时契约必须带 `inputs_manifest`（没有输入文件就写上面的空基线），且清单里的 blob 都已上传，
否则任务停在 `waiting_capability`，不会派给任何节点（`status_reason` 中会有 `protection_not_ready`，或在没有注册节点时只有 `no_registered_nodes` 与 `missing_blobs:…`）。清单完整而缺 blob 时补传 blob 即可解除；`missing_blobs` 也包括 blob 已存在但长度与清单 `size` 不符的情况：上传会校验哈希并替换已有文件，存储中的 blob 损坏或被截断时重新上传正确内容即可；若是清单的 `size` 写错（同一哈希的内容长度是确定的），重传无法解除，要取消任务、修正清单后重新提交；提交时没带 `inputs_manifest` 则无法补救（契约受理后不可修改），要取消该任务、补上清单后重新提交。`os` 取 `windows`、`macos`、`linux`；`gpu` 可写 `"cuda"`；`tools`、`labels` 要求节点上报了对应工具或标签。没有节点满足时任务停在
`waiting_capability`，不会派到不满足条件的节点；等待计入 `budgets.deadline_s`（缺省 3600 秒），从首次提交起算，到期后任务转为
`failed`（`budget_exhausted:deadline_s`）。需要等更久时调大该预算，或在失败后重新提交：期限按工作包（`work_package_id`）的首次创建时间计算，沿用旧的 `work_package_id` 不会重新计时，预算已耗尽时还会直接拒绝，所以重新提交时省略 `work_package_id`（新建工作包）；`request_id` 也要用新的，CLI 未提供时会自动生成。

`harness: codex` 的契约可以省略 model 与 effort，受理时按“任务显式给出的值 > 项目默认值 > 工作区默认值”解析并冻结；还没有默认值时返回 400（`details.reason` 为 `model_default_unset`），组合不在部署许可范围内返回 400（`model_not_allowed`），不会换成别的模型。默认值与许可范围见下文“模型设置”。

### 提交没有得到答复

`submit` 在得到 controller 的明确答复之前，把完整的请求体（含 `request_id`）作为回执保存在本机配置目录的 `requests/` 下。连接中断、controller 暂时不可达或进程被打断时，这次提交是否已被受理是未知的；controller 不可达时命令以退出码 3 结束，并在标准错误的 JSON 里给出 `request_id` 与恢复命令：

```sh
"$PREFIX/bin/agent-collab" submit --pending                 # 列出还没有明确答复的提交
"$PREFIX/bin/agent-collab" submit --resume <request_id>     # 原样重放这一次提交，不重新草拟
```

`--resume` 可以在之后的任何进程里运行：controller 已经受理过时返回原来的任务和它受理时冻结的模型策略（`http_status` 200、`replayed: true`），之前没有受理过时这一次才受理（201），两种情况都不会出现第二个任务。得到 2xx 或明确的拒绝后回执删除；凭据已被撤销（401）时回执保留。重跑整条 `submit` 命令是再提交一次，不是恢复上一次。

### 查看进展与结果

桌面端的任务详情分别显示运行、检查、验收、发布、下载与应用到项目的状态。命令行：

```sh
"$PREFIX/bin/agent-collab" job list [--status <状态>]
"$PREFIX/bin/agent-collab" job wait <job_id> --timeout 3600     # 等到停稳，输出摘要；受管修复新建修订时自动跟到最新一版
"$PREFIX/bin/agent-collab" job follow <job_id>                  # 每行一个事件，最后一行是摘要
"$PREFIX/bin/agent-collab" job results <job_id>                 # 只读取当前摘要
"$PREFIX/bin/agent-collab" job artifacts <job_id> [--download <空目录>]
"$PREFIX/bin/agent-collab" job publish <job_id>                 # 摘要的 outcome 为 ready_to_publish 时
```

运行状态、检查、验收、发布是分开的事实：`status: completed` 只表示执行结束。摘要字段与 `outcome` 的含义、退出码见 `docs/integration.md`；执行节点上的完整输出见下文“读取原始输出”。

## 项目与本机目录

项目把一组任务、它们的默认策略和输入归在一起（`docs/protocol.md` §13.1–§13.4）。项目在每台机器上对应哪个目录只保存在那台机器上，controller 不保存本机路径。桌面端在“项目”页新建项目、选定它在这台机器上的文件夹、查看将上传的范围并调整排除规则、上传为输入，成果在任务详情里审阅变更后应用到项目目录（[desktop.md](desktop.md)）。命令行：

```sh
"$PREFIX/bin/agent-collab" project create --name "<名称>" [--model <M> --effort <E>] [--exclude '<glob>' ...]
"$PREFIX/bin/agent-collab" project set <项目 ID> [--name <名称>] [--model <M> --effort <E> | --no-model] [--unverified-nodes wait|allow|inherit] [--exclude '<glob>' ... | --no-exclude]
"$PREFIX/bin/agent-collab" project bind <项目 ID> <目录>      # 这台机器上项目对应的目录；只保存在本机
"$PREFIX/bin/agent-collab" project push <项目 ID>             # 扫描绑定目录，上传 controller 还没有的内容，登记为项目的输入
"$PREFIX/bin/agent-collab" submit --project <项目 ID> --template <模板> --goal "<目标>" --wait
"$PREFIX/bin/agent-collab" project plan <job_id>              # 已发布的成果相对本机目录的变更与冲突；不写入
"$PREFIX/bin/agent-collab" project apply <job_id> [--skip-conflicts]
"$PREFIX/bin/agent-collab" project recover [<应用 ID>] [--resume | --rollback | --abandon | --report]
```

- `project list`、`show`、`archive`、`restore`、`unbind`、`bindings`、`scan` 见 `agent-collab project --help`。建项目、改名、归档与推送输入需要 `tasks` 权限；设置或修改项目默认值（模型、effort、排除规则、`unverified_nodes`）另需 `settings`。
- `push` 不跟随符号链接，缺省排除版本库元数据、依赖与构建缓存、常见凭据文件；可以重复运行，已上传的内容不再上传，内容没变不新建输入。扫描之后文件被改动时不登记输入，重新运行即可。提供输入的机器不必是执行节点，上传完成后可以离线。
- `apply` 只应用已发布的成果，按任务的输入基线、成果与目录当前内容逐文件比较，只动成果改过的路径。有冲突时缺省什么都不写（退出码 5，`status: conflict`）；`--skip-conflicts` 只写没有冲突的文件，结果是 `partial`。只有 `status: applied` 表示全部写入。
- 应用逐文件进行，被替换的原文件先备份，不是跨文件的原子操作。被打断后用 `project recover` 列出未完成的操作，再选择续写（`--resume`）、把已写入且之后未再改动的文件恢复成原样（`--rollback`），或按现状结束并记为结果未知（`--abandon`）。同一目录上有未完成的应用时，新的应用被拒绝。
- 取消：`apply` 进行中按一次 Ctrl+C 是取消请求，正在写的文件写完后停下，结果如实记为部分完成（`cancelled: true`，未写的列在 `pending`），已写入的文件保留，取消不等于回滚；再按一次才直接中断，之后用 `project recover` 处理。
- 结果没能报给 controller（不可达、应答丢失、凭据被撤销）时，结果留在本机回执里，输出带 `report_pending`；`project recover` 的 `unreported` 列出这些应用，`project recover <应用 ID> --report` 按原来的 `request_id` 补报，controller 只记一次，文件不重写。
- `push` 的登记没有得到答复（退出码 3，输出 `request_id` 与 `resume`）时，`project push <项目 ID> --resume <request_id>` 原样重放那一次登记，得到同一份基线，不重新扫描；`submit --pending` 也会列出它。再运行一次不带 `--resume` 的 `push` 是新的扫描与新的登记。

### 写回到节点上的项目目录

成果也可以由某台节点写回它本机绑定的项目目录（`docs/protocol.md` §13.10）：发起写回的机器不读写任何项目文件，比对与写入都由那台节点在自己绑定的目录上进行，规则与上面的本机应用相同。目录只在节点本机授权，controller 只知道目录名：

```sh
# 在节点本机：把项目绑定到这台节点上的目录（配置了 integration_roots 时必须在其中）；改动不需要重启节点服务
"$PREFIX/bin/agent-collab" agent project-bind --data-dir <节点数据目录> <项目 ID> <目录>
"$PREFIX/bin/agent-collab" agent project-bindings --data-dir <节点数据目录>
"$PREFIX/bin/agent-collab" agent project-unbind --data-dir <节点数据目录> <项目 ID>
# 在任意已配对的客户端（需要 tasks 权限）
"$PREFIX/bin/agent-collab" project show <项目 ID>                          # remote_targets：哪些节点为它绑定了目录
"$PREFIX/bin/agent-collab" project plan <job_id> --node <节点名>           # 由节点比对，不写入
"$PREFIX/bin/agent-collab" project apply <job_id> --node <节点名> [--skip-conflicts]
"$PREFIX/bin/agent-collab" project recover --node <节点名>                 # 列出该节点上没有最终结果的写回
"$PREFIX/bin/agent-collab" project recover <应用 ID> --node <节点名> [--resume | --rollback | --abandon]
```

- `apply --node` 登记写回并等节点的结果（`--timeout`，缺省 1860 秒），输出 `status`、应用记录与操作状态。退出码与本机 `apply` 一致：只有 `applied` 为 0；有冲突、部分写入、失败为 5；节点收到了但没在时限内给出结果（`pending_confirmation: true`，记录停在 `started`）也是 5，之后节点报来的结果仍会更新记录；等待超时而节点还在执行为 4。
- 节点不在线时请求一直等它领取；到期（`project_operation_timeout_s`，缺省 1800 秒）仍未领取的写回记为失败，一个文件都没写，节点之后也不会再执行它。
- 节点在写入中途重启时不会自动重写，记录保持 `started`：用 `recover <应用 ID> --node <节点名>` 先看节点上的现状，再续写、回滚或按现状结束。桌面端与浏览器控制台在任务详情的“成果下载与应用”里提供同样的审阅、写回与恢复。

## 节点配对

签发一次性的节点配对码（缺省 10 分钟有效，最长 1 小时）不必登录 controller 主机：在桌面端“设备”页签发，或用带 `devices` 权限的客户端凭据调用 `POST /nodes/codes`：

```sh
"$PREFIX/bin/agent-collab" api POST /nodes/codes --data '{"node_id":"<节点名>"}'
```

配对码在应答的 `body.code` 里，只出现这一次。`node_id` 已登记时必须带 `"replace": true`（配对会替换它原有的凭据），否则返回 409 `node_exists`；已退役的节点返回 409 `node_retired`，要先恢复身份（见下文“退役与恢复身份”）。

在节点上（默认每台物理主机一个原生节点；同机再装可选节点（如 WSL）时使用相同 `--physical-host-id`，`--host-slots` 为该主机总并发；`--failure-domain` 按节点实际所在的故障域填写）：

```sh
"$PREFIX/bin/agent-collab" agent pair --controller <CONTROLLER> --code <配对码> --node-id <节点名> \
  --data-dir <节点数据目录> --physical-host-id <主机> --failure-domain <故障域> --slots 1 --host-slots 2
"$PREFIX/bin/agent-collab" agent doctor --data-dir <节点数据目录>
"$PREFIX/bin/agent-collab" agent run --data-dir <节点数据目录> --foreground   # 由服务托管时不需手动运行
```

Windows 节点（PowerShell；安装器生成的是 `bin\agent-collab.ps1`）：

```powershell
$Prefix = "$env:LOCALAPPDATA\agent-collab\code"   # 安装时的 --prefix
& "$Prefix\bin\agent-collab.ps1" agent pair --controller <CONTROLLER> --code <配对码> --node-id <节点名> `
  --data-dir <节点数据目录> --physical-host-id <主机> --failure-domain <故障域> --slots 1 --host-slots 2
& "$Prefix\bin\agent-collab.ps1" agent doctor --data-dir <节点数据目录>
& "$Prefix\bin\agent-collab.ps1" agent run --data-dir <节点数据目录> --foreground   # 由计划任务托管时不需手动运行
```

上面的 `agent pair` 只用于尚未配对的新数据目录。已经由安装器配对（`--pairing-code`，见 [install.md](install.md)）的节点不要再次配对：
数据目录已有身份，会被拒绝。要调整并发：

1. 先排空该节点（`drain:true`，见下方“排空、迁移与恢复”），等 `api GET /nodes` 中它的 `active_attempts` 变为 0。
   有活动 attempt 时 controller 拒绝资源变化的注册，直接重启也会中断任务。
   要调低 `host_slots` 时，排空所有同机节点（共用 `physical_host_id`），等它们的 `active_attempts` 合计为 0：
   注册时按同机全部节点的活动预留检查新上限，只清空本节点仍会被拒绝（409）。
2. 停止节点服务，编辑节点数据目录中 `agent.json` 的 `slots` / `host_slots`，再启动服务（节点在启动注册时读取这两个值）。
3. 解除排空（`drain:false`；排空了多个同机节点时逐个解除）。

同一物理主机上有多个节点时（如 Windows 原生节点加按需启用的 WSL 节点，共用 `physical_host_id`），按各节点 `host_slots` 的最小值限制主机总并发：
调整主机上限时，所有同机节点都要按上述步骤改成同一个值，只改一个不会生效。

组织策略禁止执行 PowerShell 脚本（如 `Restricted`）时，遵循该策略、不要自行放宽；改为直接用安装记录的解释器运行启动器：

```powershell
$Python = (Get-Content -Raw "$Prefix\installation.json" | ConvertFrom-Json).config.python
& $Python -B "$Prefix\launcher.py" agent doctor --data-dir <节点数据目录>   # pair、run 同样把脚本入口换成这一写法
```

节点数据目录必须在本机磁盘。受管协调节点需在 `agent.json` 设置 `claude_command`（Claude Code 可执行文件路径），并由用户在该机本地完成官方登录；本系统不复制凭据。

## 排空、迁移与恢复

桌面端在“设备”页暂停与恢复接单、排空，在任务详情里请求取消、明确迁移或续接。命令行（节点操作需要 `devices` 权限，任务操作需要 `tasks`）：

```sh
# 暂停接单（排空）：不再领取新任务，已在运行的任务继续；migrate=true 时把可安全迁移的 attempt（已受保护、无外部副作用、有确认检查点或基线）迁到其他合格节点
"$PREFIX/bin/agent-collab" api POST /nodes/<节点名>/drain --data '{"expected_version":<版本>,"drain":true,"migrate":true}'
# 恢复接单（解除排空）
"$PREFIX/bin/agent-collab" api POST /nodes/<节点名>/drain --data '{"expected_version":<版本>,"drain":false}'
# 明确迁移/续接某个工作包（计入迁移预算，从最近确认检查点新建 attempt）。
# 要离开当前节点就用 --exclude-node 排除它；不排除表示只续接，调度器仍可能选回原节点。
"$PREFIX/bin/agent-collab" job retry <job_id> --reason "<原因>" --exclude-node <源节点名>
# 请求取消（不会触发自动恢复；节点确认后为 cancelled）
"$PREFIX/bin/agent-collab" job cancel <job_id>
```

节点的 `<版本>` 取自 `api GET /nodes` 里该节点的 `version`；过期返回 `409 version_mismatch`，重新读取后再提交（`job retry`、`job cancel` 自己读取任务的版本）。带 `migrate` 的排空，应答里的 `not_migrated` 逐条给出没有迁走的 attempt 与原因。节点失联后的自动恢复无需操作：租约到期（默认 90s）即判失联，满足条件时自动在其他合格节点继续：有 durable 检查点时从最新的那个开始，没有时从 inputs 基线开始。合格指该节点在线（心跳新鲜，即服务在运行；macOS、Windows 的服务只在用户登录后运行，Linux 启用 linger 后无人登录也可在线）、没有暂停接单也不在维护、所需 harness 的认证未被确认过期（`unknown` 不排除）、能取得恢复所需的检查点与输入，并具备契约 `requirements` 要求的平台、工具和硬件，codex 任务还要求节点的模型目录已核实并含该任务的 model 与 effort（见下文“模型设置”）；只有某个平台或某块硬件能做的任务，等待符合条件的节点，不会换到别的平台上执行。任务本身也要允许自动恢复：`recovery.auto=false`、`side_effects=manual`、存在状态未知的副作用或预算耗尽时，任务进入 `waiting_reconcile` 或 `failed`，不会自动接管；接管迟迟没有发生时查看任务的 `status` 与 `status_reason`。

## 维护、退役与重新探测

这些操作经 controller 完成，不中断节点与 controller 之间的连接（`docs/protocol.md` §13.5、§13.6）。桌面端在“设备”页进行；命令行需要 `devices` 权限：

```sh
# 维护：停止向该节点派单并记下原因，已在运行的任务不受影响；结束维护时恢复进入维护之前的接单状态
"$PREFIX/bin/agent-collab" api POST /nodes/<节点名>/maintenance --data '{"expected_version":<版本>,"active":true,"reason":"<原因>"}'
"$PREFIX/bin/agent-collab" api POST /nodes/<节点名>/maintenance --data '{"expected_version":<版本>,"active":false}'
# 退役 / 恢复身份
"$PREFIX/bin/agent-collab" api POST /nodes/<节点名>/retire --data '{"expected_version":<版本>,"retired":true,"reason":"<原因>"}'
"$PREFIX/bin/agent-collab" api POST /nodes/<节点名>/retire --data '{"expected_version":<版本>,"retired":false}'
# 重新探测登录状态与模型目录：先登记操作，再按应答里的 op_id 读结果
"$PREFIX/bin/agent-collab" api POST /nodes/<节点名>/operations --data '{"kind":"probe"}'
"$PREFIX/bin/agent-collab" api GET /operations/<op_id>
```

- 维护：`GET /nodes` 的每个节点带 `maintenance`（原因、操作者、时间）与 `maintenance_state`：`none`、`draining`（还有任务在运行）、`active`。维护期间调度解释里该节点带原因 `maintenance`。
- 重新探测（`probe`）：节点在下一次心跳领取操作，在后台服务进程自己的上下文里重新运行官方 CLI 的登录状态探测与模型目录探测（不调用模型）；结果里有每个 harness 的认证状态与原因、目录状态和服务进程的运行上下文，之后节点刷新并重新上报认证状态与目录。用户在节点本机按官方方式登录后，用它确认后台服务读得到凭据。操作的 `status` 依次为 `requested`、`received`，再到 `succeeded` 或 `failed`；超过时限（缺省 120 秒）没有完成记 `timed_out`，不重发，节点不在线时就是这个结果。

### 退役与恢复身份

让一个节点退出调度（例如不再需要的 WSL 节点）。退役保留历史，不删除节点记录：

1. 排空并迁移（`drain:true, migrate:true`），等 `active_attempts` 为 0。未结束的任务按检查点迁移；原生会话不能跨平台续接。
2. 退役（`retired:true`，必须写原因）。还有没处理完的东西时返回 409 `retire_blocked`，`details.blockers` 列出各项，逐项处理后重试：
   - `active_attempts`：该节点上有效的 attempt。等它们结束，或按上一节迁走、取消。
   - `unsettled_jobs`：当前 attempt 在该节点上，且处于待对账（`waiting_reconcile`）、恢复中或已请求取消的任务。先处置这些任务：核对后明确迁移（`job retry <job_id> --reason … --exclude-node <节点名>`），或等取消得到确认。
   - `pending_integrations`：以该节点为目标、尚未完成的回写。让节点完成回写，或经 `POST /jobs/{id}/integration` 报告结果。
   - `coordination_profile`：该节点仍是受管协调的候选节点。先把它从候选列表里去掉（见下文“协调设置”）。
3. 退役之后：节点记录保留（带退役时间、操作者与原因），历史任务仍引用它；它的节点凭据与未用的配对码作废；不再出现在 `GET /nodes`、总览、调度解释与模型目录里。已退役的节点用 `api GET '/nodes?retired=1'` 列出，恢复身份所需的 `<版本>` 也从这里取。没有删除节点的端点。
4. 恢复身份（`retired:false`）：记录不变，保持不接单，也没有凭据。要重新启用，先签发带 `"replace": true` 的节点配对码，在节点上重新配对（`agent pair` 只接受尚未配对的数据目录），确认心跳恢复后再恢复接单。

只有被排空、停用或退役的节点才具备的能力不会被别的节点替代：仅 Linux 可满足的任务进入 `waiting_capability`，`status_reason` 列出各节点的拒绝原因
（如 `os_mismatch`、`gpu_missing`、`tool_missing:<名称>`）。macOS 的 POSIX shell 不等于 Linux，controller 主机也不是 worker。

## 节点服务启停（节点本机）

停止或启动整个节点服务不经 controller 下发：服务停下后，节点与 controller 之间的心跳通道随之断开，controller 无法再经它把服务启动起来。这两个动作只能在节点本机进行：在该设备上运行下面的固定命令（经 SSH 登录该设备后运行的也是这条本机命令），或用该设备上桌面端的本机服务面板。参数与输出见 [install.md](install.md) 的“服务管理与 SSH 管理入口”：

```sh
"$PREFIX/bin/agent-collab" service status --prefix "$PREFIX"
"$PREFIX/bin/agent-collab" service stop   --prefix "$PREFIX"   # 有意停用，保持停用
"$PREFIX/bin/agent-collab" service start  --prefix "$PREFIX"   # 恢复自启动并启动，新心跳到达才算成功
```

- `service stop` 先关闭自启动、再停止进程，并记为有意停用：重新登录、重启机器以及之后的升级与回退都保持停用，直到执行 `service start`。本地有 preparing/running 的 attempt 时拒绝，先排空，或加 `--allow-active`。
- `service stop` 不通知 controller：停下后节点显示为 unreachable，它的排空与维护状态不变。只想让节点暂时不接单而不停服务，用上文的暂停接单或维护。
- `service start` 恢复自启动并启动；之后要接单，再解除排空。
- 临时停用一个节点而不退役：先排空并等 `active_attempts` 为 0，再在该节点执行 `service stop`。保留它的 node_id、执行证据和数据目录，不要删除节点记录或卸载。

## 读取原始输出

事件、检查摘要与原始输出是三种不同的东西（`docs/protocol.md` §13.6）：

- 产品事件（`job follow`、`GET /events`、`GET /jobs/{id}/events`，桌面端任务详情的事件时间线）：controller 记录的状态变化与节点上报的进度，不是 CLI 的完整输出。
- 检查摘要（任务摘要的 `checks`，来自 attempt 的 `report.checks`）：每项检查的退出码与标准输出、标准错误的尾部，尾部各不超过 4 KiB。
- 原始输出：harness 与检查进程的完整标准输出与标准错误，只保存在执行节点的 attempt 目录里，按需分块取回：

```sh
"$PREFIX/bin/agent-collab" api POST /jobs/<job_id>/logs --data '{"attempt_id":"<attempt_id>"}'      # 列出可取的输出
"$PREFIX/bin/agent-collab" api POST /jobs/<job_id>/logs --data '{"attempt_id":"<attempt_id>","stream":"harness.stdout","offset":0,"max_bytes":65536}'
"$PREFIX/bin/agent-collab" api GET /operations/<op_id>                                             # 读取结果；next_offset 用于下一块
```

`POST /jobs/{id}/logs` 只登记一项取回操作并返回 `op_id`（`read` 权限即可），内容在随后 `GET /operations/{op_id}` 的 `result` 里。`attempt_id` 取自 `job show <job_id>` 的 `attempts`；`stream` 为 `index`（缺省，列出可取的输出）、`harness.stdout|stderr`、`check.<n>.stdout|stderr` 或 `step.<n>.stdout|stderr`，`max_bytes` 最大 262144。执行节点在下一次心跳领取操作，通常几秒内完成；节点不在线时到期记为 `timed_out`。`result.retained` 为 false 表示节点上已没有这次执行的本地文件；`result.data` 是经节点脱敏的文本。

## 模型设置

工作区的默认模型、部署许可范围与各节点的模型目录都在 `GET /settings/models` 里（`docs/protocol.md` §12.3–§12.5）。桌面端在“设置”里选择执行任务的默认模型与 effort，可选项来自各节点上报的模型目录。命令行：

```sh
"$PREFIX/bin/agent-collab" settings get        # settings、catalog（各节点目录）、selectable（可选组合）、history（最近 20 次修改）
"$PREFIX/bin/agent-collab" settings set --codex-model <M> --codex-effort <E>           # 执行任务的工作区默认值
"$PREFIX/bin/agent-collab" settings set --allow <M>=<effort1>,<effort2>                # 部署许可范围：只允许列出的组合（可重复）；--allow-any 为不限制
"$PREFIX/bin/agent-collab" settings set --claude-model <M> --claude-effort <E>         # 受管协调回合的模型与 effort
```

`settings set` 需要 `settings` 权限；没给的项保持不变，期间别人改过设置时以 `version_mismatch` 被拒绝，重新运行即可。修改只影响之后受理的任务：已受理的任务（包括排队中的）与它们的修复用受理时冻结的值。

`codex.unverified_nodes` 决定模型目录未核实的节点能否承接 codex 任务，缺省 `wait`：

| 取值 | 含义 |
| --- | --- |
| `wait`（缺省） | 只派给目录已核实并含该 model 与 effort 的节点；没有这样的节点时任务停在 `waiting_capability`，目录未核实的节点在调度解释里的原因是 `model_catalog_unverified:<unknown\|expired>` |
| `allow` | 显式开启的“未核实能力尝试”：目录未核实的节点也可以承接，但排在所有目录已核实含该组合的节点之后 |

这一项与模型一起在任务受理时冻结。`settings set` 没有对应的选项，也不会改动它；要修改时，取 `settings get` 返回的 `settings.codex`，改其中的 `unverified_nodes`，连同 `expected_revision`（当前的 `settings.revision`）用 `api PUT /settings/models` 提交（`codex` 要带齐 `default` 与 `allowed`）。只想对某个项目放开，用 `project set <项目 ID> --unverified-nodes allow`（`inherit` 恢复为沿用工作区的值）。

各节点目录的当前状态看 `catalog.nodes.<节点名>.codex.state`。`status` 是节点上报的值，`state` 是 controller 按有效期（`catalog.max_age_s`，缺省 172800 秒）算出的当前状态：

| `state` | 含义 | 已核实 |
| --- | --- | --- |
| `ok` | 最近一次探测成功 | 是 |
| `stale` | 最近一次探测失败，节点沿用上一次成功的目录（CLI 版本与 provider 未变）；`reason` 是这次失败的原因，`fetched_at` 是那次成功的时间 | 是 |
| `legacy` | 节点没有上报目录（旧版本），按固定组合 `gpt-6-astra` 配 `high`、`xhigh`、`max` 处理 | 是 |
| `unknown` | 没有可用目录，`reason` 给出原因（如 `command_missing`、`probe_failed`、`provider_unverified`、`provider_mismatch`） | 否 |
| `expired` | 目录的取得时间早于有效期，不再当作当前能力 | 否 |

`selectable.codex` 是部署许可范围与已核实目录的交集，`selectable.unknown_nodes` 列出目录未核实的节点。目录条目来自节点上的 CLI，不表示该账号已成功调用过这个模型。节点缺省每 6 小时刷新一次目录（`agent.json` 的 `model_catalog_s`）；状态为 `unknown` 或 `expired` 时先看 `reason`，在节点本机处理好官方 CLI 或登录之后，用上文的重新探测（`probe`）立即刷新。某个任务为什么没有派出去，看 `api GET /jobs/<job_id>/explain` 里各节点的原因：`model_unsupported:<model>`、`effort_unsupported:<model>/<effort>` 表示已核实的目录不含该组合。

## 协调设置

受管协调是否启用、由哪些节点承担，经协调设置读写（`docs/protocol.md` §13.7）；协调回合的模型与 effort 是上一节的 `claude.default`。桌面端“总览”页显示受管协调者当前的状态。命令行（修改需要 `settings` 权限）：

```sh
"$PREFIX/bin/agent-collab" api GET /settings/coordination
"$PREFIX/bin/agent-collab" api PUT /settings/coordination --data '{"expected_revision":<revision>,"enabled":true,"node_ids":["<备用节点>"]}'
```

- `GET` 返回 `settings`（`revision`、`enabled`、`node_ids` 与最近的修改者）、`effective`（协调回合实际使用的配置，与 `GET /overview` 的 `managed_coordinator` 相同）、`unknown_nodes`（列表里不在已登记节点中的 ID）与 `history`。
- `<revision>` 取 `GET` 返回的 `settings.revision`，不一致时返回 409 `version_mismatch`。已退役的节点不能列入。
- 保存过一次（`revision > 0`）之后，是否启用与候选节点由这份设置决定，覆盖 `config.json` 的对应项。还没有保存过时 `revision` 为 0，`enabled` 与 `node_ids` 显示的是 `config.json` 当前生效的值。
- 没有协调模型或没有候选节点时 `effective.status` 为 `profile_missing`，不创建协调回合。`ready` 只表示配置完整，不代表节点上的 Claude CLI 与登录可用；后者看该节点的认证探测结果（上文的 `probe`）。

没有经 API 保存过协调设置的部署沿用 controller `config.json` 里的配置；没有设置 `claude.default` 时，协调回合的模型与强度也取自这里的 profile：

```json
{"managed_coordinator": {"enabled": true, "profile_id": "standby"},
 "standby_profiles": {"standby": {"harness": "claude", "model": "<用户选定模型>", "effort": "<用户选定强度>", "node_ids": ["<备用节点>"]}}}
```

未配置或字段不全时 UI 显示“profile 缺失”，系统不猜测模型或强度。协调权在 UI 工作包详情中取得、接管（须写原因，留事件）与释放。

## 派单、提交闸门与切换屏障

本节只是速查：字段见 `docs/protocol.md` §13.9，完整的切换与回退步骤见 [migration.md](migration.md)。下面的 `POST` 需要 `maintain` 权限，`GET /activity` 需要 `read`；`<元数据版本>` 取 `GET /overview` 的 `controller.metadata_version`：

```sh
# 暂停 / 恢复派单（桌面端在“总览”页的“派单”里操作）
"$PREFIX/bin/agent-collab" api POST /dispatch --data '{"expected_version":<元数据版本>,"paused":true}'
# 提交闸门：open（缺省）、acceptance（只接受验收运行）、closed（不接受新任务）；不是 open 时必须写原因
"$PREFIX/bin/agent-collab" api POST /intake --data '{"expected_version":<元数据版本>,"mode":"closed","reason":"<原因>"}'
# 某个事件序号之后产生了什么：受理的任务、确认的检查点、发布、验收、写回与应用
"$PREFIX/bin/agent-collab" api GET '/activity?after=<event_seq>'
```

- 闸门当前的状态在 `GET /overview` 的 `intake` 里。`closed` 时 `POST /jobs` 对任何凭据（含管理令牌）返回 409 `intake_closed`；读取、对已有任务的操作、节点上报与 controller 自己创建的协调回合和修复不受影响。`acceptance` 只接受验收运行：请求带 `acceptance_run: true`、提交者具备 `maintain` 权限、不属于项目、没有回写目标节点、不声明外部副作用。
- `GET /activity` 的 `disposable` 为 true 表示其间没有普通任务，也没有任何写回或应用。`<event_seq>` 通常取备份清单（`backup-manifest.json`）里的 `event_seq`。
- 切换屏障在 controller 主机上、controller 停止之后运行（Linux 受管安装以服务用户执行，如 `runuser -u agent-collab --`），要求派单已暂停、提交闸门不是 `open`。它把恢复代次加一并撤销此前签发的全部执行权，处理与恢复备份相同，但数据不回退；节点凭据、客户端凭据与管理令牌不变：

```sh
"$PREFIX/bin/agent-collab" controller barrier --data-dir <DATA> --request-id <新 UUID> --reason "<原因>"
```

## 备份与恢复

```sh
# 在线一致备份（服务运行中执行；sqlite backup API + 被引用 blob + manifest）
install -d -o agent-collab -g agent-collab -m 0700 /var/lib/agent-collab/backups
TS=$(date -u +%Y%m%dT%H%M%SZ)  # 备份目录与打包文件共用同一个时间戳
cd /opt/agent-collab/current && runuser -u agent-collab -- python3 -B -m agent_collab.controller backup \
  --data-dir <DATA> --dest /var/lib/agent-collab/backups/$TS
# 打包并复制到实例外（同一实例上的其他目录不算实例外备份）
tar -C /var/lib/agent-collab/backups -czf /root/backup-$TS.tar.gz $TS
```

恢复前必须确认原 controller 已停止或已隔离：

```sh
systemctl stop agent-collab-controller
cd /opt/agent-collab/current && runuser -u agent-collab -- python3 -B -m agent_collab.controller restore --data-dir <DATA> --from <解包的备份目录>
systemctl start agent-collab-controller
```

恢复会把 `authority_generation` 加一并暂停派单：此前签发的租约全部失效（`fenced: stale_generation`），节点心跳得到 `reconcile_required` 并先对账；确认后在 UI“总览”恢复派单。恢复演练应在隔离环境进行（另一目录、loopback 端口），不要让真实节点连上演练实例。

### 持续备份的布局与默认值

`python -m agent_collab.backup` 只依赖标准库，兼容 Python 3.9。安装器尚不自动注册本节任务；需要先将含本模块和模板的发行版本安装到 ECS 与 Mac，再手动启用。远端代码固定从 `/opt/agent-collab/current` 运行；自定义安装前缀需要相应修改 `remote_command()` 的工作目录，不能把 shell 命令塞进 `--python-cmd`。

- ECS：状态根目录 `/var/lib/agent-collab`，controller 数据在其 `controller/`，定时备份在同级 `backups/`。禁止把备份放进 controller 数据目录或其祖先。HTTP `/overview`、`/doctor` 的 `backup` 对象读取 `<controller 数据目录的父目录>/backups/status.json`；缺文件时为 `unknown`，损坏文件报告读取错误。安装器在线 doctor 在 `components.controller.backup` 转发这个对象；离线 doctor 仍为 `unknown`。
- 默认每 6 小时一轮，ECS `keep=8`、`max_bytes=500000000`（十进制 500 MB），Mac `keep_offsite=28`。按时运行时 8 份与 28 份分别相当于约 2 天与 7 天的历史。单份大小等于数据库加全部被引用 blob（确定性 tar 不压缩，以 COMPLETE 为准），随任务工件增长：一次现场测量为 89 MB，此时本地先碰到 500 MB 上限、实际保留约 5 份，实例外 28 份约 2.5 GB。按实际尺寸调整 `max_bytes` 与 `--keep-offsite`。这些是调度与保留配置，不是保证的 RPO。
- `--max-bytes` 是全部定时完整集 tar 字节的总留存预算，不限制单份的生成：单份超过它照样生成并作为最新集保留，只是更早的集合会在下一轮按预算删除。最新集同时是最新已确认实例外集时，上一份不再受保护，下一轮就会被删；需要在实例内多留一份（例如切换前的恢复点）时，先按“单份大小 × 想保留的份数”提高预算。每轮还需要约“完整快照（数据库 + 全部被引用 blob）+ 一份 tar”的临时空间：快照与打包都在备份目录里完成，打包期间两者同时存在。
- 本地 `.lock` 使用非阻塞 `flock`；Windows 使用等效字节锁。锁忙退出码 `75`，不改变状态；其他失败为 `1`。锁文件不删除，崩溃由 OS 解锁。遗留 `.tmp-*` 下一轮自动删除；其他不完整目录不计入保留策略。`upgrade-*` 完全不受定时保留策略管理。
- 完整集是 `<id>/backup.tar` 与最后原子发布的 `COMPLETE.json`，其中含 tar/DB SHA-256、tar 字节数、备份创建时间和 generation。tar 只包含 manifest 声明的数据库和 blob，名称排序、mtime/uid/gid 固定。保留策略按 tar 字节总和删除最旧完整集，永远保留最新集和最新已确认实例外复制的集；保护集可使份数/容量超过配置。没有实例外确认时仍至少保留最新完整集。
- `OFFSITE.json` 是 Mac 校验成功后的回执；同 ID 重复确认幂等，较旧的延迟回执不覆盖较新的实例外确认。ECS 的 `status.json` 分别记录 `local.last_success_* / last_failure_at / last_error` 和 `offsite.last_confirmed_* / location`。
- Mac 每轮复制最新且比现存已验证副本更新的远端集，不补拉历史，不反复拉取已被保留策略淘汰的旧集。`.partial/` 存未完成下载；完整下载重跑可复用，不完整下载会从头下载（Workbench 无字节续传接口）。校验 tar 哈希/长度、内部 manifest/全部 blob、SQLite `PRAGMA integrity_check` 和数据库引用清单后，先原子放置 `<id>.tar`，再原子发布 `<id>.json` 作为已验证标记。跨两个文件没有单个原子事务；中间崩溃的孤立 tar 会在重跑时重新校验。
- 确认失败保留已验证副本，下一轮优先重试确认，再拉更新集。如果远端已按保留策略淘汰待确认集，则保留本地副本直到更新集成功拉取并确认，再应用实例外保留策略，避免永久卡在过期确认上。只有远端确认成功后才更新 Mac `status.json` 的 `last_success_at / last_success_id`；失败只更新 `last_failure_at / last_error`。无新备份的轮次不刷新成功时间。成功后清空 `last_error`，保留历史失败时间。哈希校验不通过不会删除已有已验证副本。
- controller 的 `backup.local_age_s / offsite_age_s` 根据实际成功/确认时间计算，不写入配置周期。`backup.last_error` 显示 ECS 本地状态的错误；Mac 拉取失败保存在 Mac 的 `--dest/status.json`。远端不可达时不能把该失败写回 ECS，需同时检查 Mac 状态和日志。确认时间是回执时间，备份数据时点需查看 `COMPLETE.created_at`，不要把回执年龄当作数据 RPO。

### ECS 手动安装、启用与停用

以下命令在 ECS root 管理终端执行（也可通过已授权的 Workbench exec）；模块始终由 `agent-collab` 用户运行。路径采用上述默认布局。重复启用不会注册第二个任务，手动轮次与定时轮次共用锁。

```sh
cd /opt/agent-collab/current
install -d -o agent-collab -g agent-collab -m 0700 /var/lib/agent-collab/backups
# 如果已有同名单元，第一次覆盖前保留回退副本；再次执行不覆盖回退文件。
for unit in agent-collab-backup.service agent-collab-backup.timer; do
  if test -f "/etc/systemd/system/$unit" && ! test -f "/etc/systemd/system/$unit.before-rc5"; then
    cp -p "/etc/systemd/system/$unit" "/etc/systemd/system/$unit.before-rc5"
  fi
  install -m 0644 "installer/services/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now agent-collab-backup.timer
# 首次立即运行一轮；后续由 UTC 00/06/12/18 点 + 最多 10 分钟随机延迟触发。
systemctl start agent-collab-backup.service
systemctl list-timers agent-collab-backup.timer
journalctl -u agent-collab-backup.service -n 50 --no-pager
runuser -u agent-collab -- /usr/bin/python3 -B -m agent_collab.backup status --backups-dir /var/lib/agent-collab/backups
runuser -u agent-collab -- /usr/bin/python3 -B -m agent_collab.backup list --backups-dir /var/lib/agent-collab/backups

# 停用后不再启动新轮次；stop service 会中断当前轮次，遗留临时集下次清理。
systemctl disable --now agent-collab-backup.timer
systemctl stop agent-collab-backup.service
```

`Persistent=true` 会补触发停机期间错过的日历任务，不补造每个历史时点的备份。若改变 `--backups-dir`，controller 的默认状态展示将读不到它：controller 只读取固定的默认路径，配置 schema 中没有对应选项。保持安装布局即可自动展示。

ECS 回退定时机制（保留所有备份、状态、锁文件和 controller 数据）：

```sh
systemctl disable --now agent-collab-backup.timer
systemctl stop agent-collab-backup.service
for unit in agent-collab-backup.service agent-collab-backup.timer; do
  if test -f "/etc/systemd/system/$unit.before-rc5"; then
    mv "/etc/systemd/system/$unit.before-rc5" "/etc/systemd/system/$unit"
  else
    rm -f "/etc/systemd/system/$unit"
  fi
done
systemctl daemon-reload
systemctl reset-failed agent-collab-backup.service || true
# 仅当恢复了原有 timer 且希望恢复其运行时，再执行：
# systemctl enable --now agent-collab-backup.timer
```

### Mac 实例外拉取 LaunchAgent

在拥有已验证 Workbench 配置的 Mac 登录用户下执行。`CODE_DIR` 必须是本机已安装、包含本模块的代码绝对路径；示例默认用独立安装的 `current`，不要指向临时的源码 clone。`STATE_DIR` 是实例外持久目录，放在**内置盘**的用户目录下，实例 ID 用现场值替换。模板不含令牌，也不会复制 Workbench 登录资料；需确保该登录用户无需交互即可执行 Workbench。

不要把 `STATE_DIR` 放在外置卷：macOS 的隐私控制（TCC）会拒绝 LaunchAgent 里的 `/usr/bin/python3` 访问可移除卷（`PermissionError: Operation not permitted`），
而同一命令在 SSH 会话里手动执行可能成功，这不说明后台任务也能访问。确需外置卷时由用户在“隐私与安全性”里为实际运行的程序授权，
不要为此给通用解释器完全磁盘访问。

```sh
CODE_DIR="$HOME/.local/share/agent-collab/current"
STATE_DIR="$HOME/.local/state/agent-collab"
INSTANCE_ID="i-替换为实际实例ID"
PLIST="$HOME/Library/LaunchAgents/org.agent-collab.backup-pull.plist"
umask 077
mkdir -p "$STATE_DIR/offsite-backups" "$STATE_DIR/logs" "$HOME/Library/LaunchAgents"
if test -f "$PLIST" && ! test -f "$PLIST.before-rc5"; then cp -p "$PLIST" "$PLIST.before-rc5"; fi
/usr/bin/python3 - "$CODE_DIR" "$STATE_DIR" "$INSTANCE_ID" "$PLIST" <<'PY'
import pathlib, plistlib, sys
code, state, instance, target = sys.argv[1:]
assert pathlib.Path(code).is_absolute() and pathlib.Path(state).is_absolute()
with open(str(pathlib.Path(code) / 'installer/services/org.agent-collab.backup-pull.plist'), 'rb') as f:
    template = plistlib.load(f)
def render(value):
    if isinstance(value, str):
        return value.replace('@CODE_DIR@', code).replace('@STATE_DIR@', state).replace('@INSTANCE_ID@', instance)
    if isinstance(value, list):
        return [render(v) for v in value]
    if isinstance(value, dict):
        return {k: render(v) for k, v in value.items()}
    return value
with open(target, 'wb') as f:
    plistlib.dump(render(template), f)
PY
plutil -lint "$PLIST"
# 重载同一 label；第一次不存在时 bootout 的失败可忽略。
launchctl bootout "gui/$(id -u)/org.agent-collab.backup-pull" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
# RunAtLoad=false：注册本身不拉取；首次验证显式触发。
launchctl kickstart "gui/$(id -u)/org.agent-collab.backup-pull"
launchctl print "gui/$(id -u)/org.agent-collab.backup-pull"
cd "$CODE_DIR"
/usr/bin/python3 -B -m agent_collab.backup status --dest "$STATE_DIR/offsite-backups"
tail -n 50 "$STATE_DIR/logs/backup-pull.err.log"

# 手动运行与 LaunchAgent 用同一目标锁：
/usr/bin/python3 -B -m agent_collab.backup pull --dest "$STATE_DIR/offsite-backups" \
  --instance-id "$INSTANCE_ID" --workbench /usr/local/bin/workbench \
  --remote-backups-dir /var/lib/agent-collab/backups --keep-offsite 28
```

LaunchAgent 依赖用户登录和 Workbench 当前可用；`StartInterval=21600` 不是未登录启动保证，不声称补齐休眠/离线期间每轮任务。日志只追加，可在停用时按实际容量归档；没有日志轮转服务。Workbench exec 以 root 运行，但 list/confirm 命令均经 `runuser -u agent-collab`；download 读取的路径固定为严格验证目录内 `<id>/backup.tar`。exec 超时为 300 秒，download 为 3600 秒，超时记录失败并释放本地锁；远端 exec 超时后可能仍在完成短命令，确认重试保持幂等。Windows 上可以运行这些单元测试，但 Linux/Mac 模板不能用于 Windows 服务注册。

停用与回退（保留 Mac 副本、状态与日志；恢复旧模板不自动启用）：

```sh
PLIST="$HOME/Library/LaunchAgents/org.agent-collab.backup-pull.plist"
launchctl bootout "gui/$(id -u)/org.agent-collab.backup-pull" 2>/dev/null || true
if test -f "$PLIST.before-rc5"; then
  mv "$PLIST.before-rc5" "$PLIST"
else
  rm -f "$PLIST"
fi
# 仅当恢复了旧模板且希望重新启用时：
# launchctl bootstrap "gui/$(id -u)" "$PLIST"
```

### 从新拉取的副本隔离恢复

先检查 `<id>.json` 存在且有 `verified_at`、`confirmed_at`，再使用隔离目录（均须不存在），不能把旧 controller 的现场数据作为试验目标。解包前再次校验文件，防止保存后的损坏：

```sh
cd "$CODE_DIR"
BACKUP_ID="替换为已验证副本ID"
RESTORE_ROOT="$STATE_DIR/restore-drill-$BACKUP_ID"
/usr/bin/python3 -B - "$STATE_DIR/offsite-backups" "$BACKUP_ID" "$RESTORE_ROOT" <<'PY'
import pathlib, sys
from agent_collab.backup import check_complete, read_json, verify_archive
from agent_collab.controller import restore
copies, ident, target = pathlib.Path(sys.argv[1]), sys.argv[2], pathlib.Path(sys.argv[3])
receipt = read_json(copies / (ident + '.json'))
complete = check_complete(receipt)
assert complete['id'] == ident and receipt['verified_at'] and receipt['confirmed_at']
target.mkdir(mode=0o700)
verify_archive(copies / (ident + '.tar'), complete, target / 'source')
generation = restore(target / 'controller', target / 'source')
assert generation == complete['authority_generation'] + 1
print({'isolated_data_dir': str(target / 'controller'), 'authority_generation': generation})
PY
```

恢复继续沿用既有语义：旧执行租约/generation 被 fence，旧协调租约不能取得新协调权；节点身份凭据保留用于心跳对账，不代表旧执行权复活。备份不含 `admin.token`，新隔离目录首次启动会创建新管理令牌；覆盖已有数据目录时沿用该目录原有管理令牌，不能宣称所有恢复都会轮换它。CLI 没有启动演练 controller，不会让真实节点连接演练实例。

## 诊断导出

`agent-collab diagnostics` 只落本机，不自动上传或发送。导出不停止服务、不运行认证探测、不访问网络；controller 信息由调用方取得后通过 `--controller-info` 传入。`--profile` 在离线契约下明确拒绝，不读取配对凭据；需要 controller 信息时改用 JSON 文件。

```sh
agent-collab diagnostics export --out /local/diagnostics.zip --prefix /local/code --state-dir /local/state
agent-collab diagnostics export --out /local/diagnostics-cand.zip --instance cand --controller-info /local/controller-info.json
agent-collab diagnostics preview /local/diagnostics.zip
agent-collab diagnostics preview /local/diagnostics.zip --show logs/000.txt
```

- 收集运行包版本、Python、操作系统与架构；安装器只读发现中的服务状态、自启动、期望状态、服务归属与运行上下文；已安装版本的发行清单元数据；节点 `status.json` 中的版本、版本目录、心跳、认证健康摘要与最近错误。缺失、不可读或无效的可选 JSON 如实标记，不把未知状态当作健康。
- 日志只读固定位置的末尾：`<prefix>/installer.log`、`<state-dir>/installer.log`、`worker/agent.log`、`controller/controller.log`，以及 `logs/` 下 installer、worker、controller 的 `.log`、`.out.log`、`.err.log`。不读取系统 journal，也不递归扫描任意文件。源路径经脱敏后保存在清单里；包内文件使用 `logs/000.txt` 这样的编号名称。
- 默认不读取 attempt 的 stdout/stderr。显式加 `--include-task-logs` 才收集 `worker/attempts/<UUID>/`、`checks/<编号>/`、`step-<编号>/` 下的 `stdout.jsonl` 与 `stderr.log`；命令在 stderr 和预览的 notes 中提示“任务原始日志可能包含项目内容”，并继续脱敏。分享前应检查预览。
- 不收集客户端凭据、系统凭据存储、官方 CLI 配置与认证材料、配对码、私钥文件、任务输入、项目源码、成果、数据库或备份；不打开 `.codex`、`.claude`、`.ssh`。仅固定位置的本产品 `worker/node.token`、`controller/admin.token` 与有界 attempt 的 `lease.token` 可读取至内存，用于精确替换日志里的同值，每个最多 4096 字节，令牌文件本身不入包、不打印。其他读取（包括 `--controller-info`）拒绝 `.token`、`.pem`、`.key`、`.p12`、`.pfx`、`.db`、`.sqlite`、`.sqlite3`、`.bak` 文件。项目绑定只读 `worker/project-bindings.json` 中的目录字符串，不打开项目目录。
- 所有收集到的文本与 JSON 值统一处理 Authorization/Bearer/Basic、`X-Coordinator-Token`、Cookie 与 Set-Cookie、URL 里的用户名与密码、token/secret/password/code/key 字段、JWT、`sk-` API key、至少 32 字符的 URL-safe base64/十六进制串与 PEM 私钥块。替换为固定占位符，并按类别统计；长提交哈希也可能被保守脱敏。用户家目录变为 `~`，用户名变为 `<user>`，主机名变为 `<host>`，已知绑定目录只留目录名。JSON 中的任务输入、源码与成果内容字段省略。统计表示本次采集实际执行的替换次数，预览及最近错误的副本不重复计数。
- 每个日志最多读取和导出 256 KiB；`--max-bytes` 控制日志合计读取及导出上限，缺省 4 MiB，允许 1 字节至 64 MiB。最多记录 128 个日志、检查 128 个 attempt；清单标明库存截断、原始大小、读取字节、导出字节与截断情况。截断的首行丢弃，防止残缺凭据绕过规则。最近错误来自已收集的日志尾部，按文件顺序保留最后 50 条，每条最多 2000 字符，不代表跨文件的时间排序。
- 单个 JSON 最多读取 1 MiB，超限时标记未收集和截断，不解析残缺 JSON；`diagnostics.json` 最多 2 MiB，超限则导出失败。controller 信息文件也受此限制。预览不解压文件，只接受本格式的有限文件名与大小，`--show` 日志最多 256 KiB、摘要最多 2 MiB。
- `--out` 必须是尚不存在的本机 `.zip` 文件；拒绝网络路径、网络映射盘、符号链接、重解析点及多硬链接输入文件。日志打开前拒绝 FIFO、设备等非普通文件，打开后再次核对句柄。先创建同目录临时文件并限制权限，再写入、同步并原子改名。POSIX 为 `0600`，Windows 的 DACL 只保留当前用户；权限收紧失败则不写入诊断内容。stdout 输出文件列表、大小、截断与脱敏统计以及完整结构化摘要。

## 旧任务

codex-collaborate 旧任务继续由原 launcher 管理；只读查看：`"$PREFIX/bin/agent-collab" legacy list --state-root <旧状态目录>`。

## 迁移前部署与应急入口

本节的做法适用于尚未迁移到客户端授权的现有部署，以及客户端凭据不可用时在 controller 本机的应急操作。它们仍然有效，本文不要求立即改变现有部署；迁入步骤与这些入口的退出条件见 [migration.md](migration.md)。

| 操作 | 命令 |
| --- | --- |
| 管理令牌位置 | `<DATA>/admin.token`（0600，属服务用户）；UI 登录与 CLI 管理调用使用；恢复到新目录后首次启动生成新令牌，原目录恢复保留原令牌 |
| UI | 浏览器打开 `<CONTROLLER>/`，输入管理令牌；会话为 HttpOnly Cookie，刷新可自动取回 CSRF。controller 配置里 `web_console` 为 false 时这个入口关闭 |
| CLI 管理调用 | `"$PREFIX/bin/agent-collab" api GET /overview --url <CONTROLLER> --token-file <DATA>/admin.token`（证书始终校验；非 loopback 必须 https）。Linux 受管安装中令牌文件仅 `agent-collab` 可读：在 controller 主机上以 `runuser -u agent-collab --` 执行，或改用已安全交给执行用户、仅其可读的令牌副本 |

管理令牌等同于全部权限。本文其余各节的 `api`、`submit`、`job`、`project`、`settings` 命令加上 `--url <CONTROLLER> --token-file <令牌文件>`，就改用管理令牌而不是客户端凭据，例如：

```sh
"$PREFIX/bin/agent-collab" api POST /nodes/<节点名>/drain --data '{"expected_version":<版本>,"drain":true,"migrate":true}' --url <CONTROLLER> --token-file <DATA>/admin.token
"$PREFIX/bin/agent-collab" api POST /jobs/<job_id>/retry --data '{"expected_version":<版本>,"reason":"<原因>","exclude_node_ids":["<源节点名>"]}' --url <CONTROLLER> --token-file <DATA>/admin.token
"$PREFIX/bin/agent-collab" api POST /jobs/<job_id>/cancel --data '{"expected_version":<版本>}' --url <CONTROLLER> --token-file <DATA>/admin.token
```

`<版本>` 取自 `GET /nodes` 或 `GET /jobs/<id>` 的 `version`；过期返回 `409 version_mismatch`，重新读取后再提交。

### 从没有客户端凭据的机器提交任务

管理令牌只在 controller 主机上时，从其他机器提交有两种做法：
经云厂商的远程执行通道在 controller 主机上以服务用户运行同一条命令（令牌不离开该主机），
或使用已安全交给执行用户、仅其可读的令牌副本直接访问 `<CONTROLLER>`：

```sh
# 在 controller 主机上（或经远程执行通道），任务内容以 base64 传入，不落盘
runuser -u agent-collab -- "$PREFIX/bin/agent-collab" api POST /jobs --data "$(echo <任务.json 的 base64> | base64 -d)" \
  --url http://127.0.0.1:18669 --token-file <DATA>/admin.token
runuser -u agent-collab -- "$PREFIX/bin/agent-collab" api GET /jobs/<job_id> --url http://127.0.0.1:18669 --token-file <DATA>/admin.token
```

### 在 controller 主机上生成节点配对码

在 controller 主机以服务用户生成一次性配对码（10 分钟有效），不要以 root 直接写数据库：

```sh
cd /opt/agent-collab/current && runuser -u agent-collab -- python3 -B -m agent_collab.controller pairing-code --data-dir <DATA> --node-id <节点名>
```
