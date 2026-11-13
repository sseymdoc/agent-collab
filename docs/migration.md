# 现有部署迁入：预检、预演、切换与回退

现有部署原位升级到带客户端授权、模型设置与项目的版本（`docs/protocol.md` §12、§13），并退役旧的日常入口。controller 留在原位置升级，各节点原位置升级 worker，桌面端连接这个 controller；不另起第二个 controller 管理同一批设备。

切换在产品完整交付之后做一次。在那之前，下面的预检和预演可以随时运行：它们只读，不改变任何生产状态。

## 预检（只读盘点）

```bash
agent-collab migrate preflight \
  --controller-data-dir <controller 数据目录> --controller-db <一致快照.db> \
  --node-data-dir <节点数据目录> --install-root <安装根> \
  --legacy-state-root <旧 launcher 状态根> --scan <要查找旧入口引用的文件或目录> \
  --out <报告.json>
```

各参数都可省略或重复；在 controller 主机和每个节点上各运行一次，盘点各自机器上的对象。

- 数据库以只读方式打开，不执行写语句；报告文件不能位于被检查的目录内。运行中的 controller 的 `controller.db` 带有未合并的日志，严格只读读不了它：用 `--controller-db` 指向一致快照（`controller backup` 生成的备份集里的 `controller-<哈希>.db`），配置、blob 和凭据文件的元数据仍取自 `--controller-data-dir`。
- 凭据文件（`*.token`、`auth.json`、私钥等）只取存在性、大小和权限位，不读内容；配置、安装记录和被扫描文本里的凭据值在报告中被遮蔽。
- 不访问网络。只有给出 `--probe-services` 时才向服务管理器做只读查询（`systemctl show`、`launchctl print-disabled`、`schtasks /query`）。

报告内容：

| 部分 | 内容 |
| --- | --- |
| `controller` | 数据库表与各类记录数量、controller 身份与恢复代次、配置项、未结束的任务、活动 attempt、协调租约、已登记节点（版本、运行用户、是否上报模型目录）、客户端与会话数量、模型设置、blob 与备份集 |
| `node_installs`、`installations` | 节点身份与所连 controller、命令路径配置、服务进程写下的认证探测结果、未上报的 attempt；安装版本与服务定义（运行用户、命令、是否启用） |
| `project_bindings` | 任务契约里的回写目标及其路径类型（SMB、UNC、本地） |
| `legacy` | 旧 launcher 的任务数量与仍在运行或状态不明的任务；被扫描文本里对旧 launcher、Skill、远程执行包装、SMB 路径、浏览器控制台登录、管理令牌的引用 |
| `plan` | 按盘点结果列出的迁移、原位保留、归档、退役项，以及各自在新产品里的对应入口 |
| `conflicts`、`risks`、`missing` | 身份不一致、节点 ID 重复、代次超前等冲突；读不了或被跳过的对象；缺少的信息 |
| `cutover_preconditions` | 切换前置条件的当前取值（true、false 或无法判断） |

备份集有两种布局，预检都认得：`controller backup` 直接写出的目录，以及定时备份打包的集合（`COMPLETE.json` 加 `backup.tar`，数据库在包里）。只有打包集合时，先把包里的 `controller-<哈希>.db` 解到被检查目录之外的位置，再用 `--controller-db` 指向它；预检自己不解包、不落盘。

controller 运行期间的当前状态（未结束的任务、活动 attempt、节点版本与是否上报模型目录、是否暂停派单）也可以直接从它的只读接口取得：`agent-collab api GET /overview` 与 `agent-collab api GET /nodes`，与快照里的记录互相印证。

已知限制：备份集只判断是否存在，不验证能否恢复（恢复能力由下面的预演验证）；服务管理器查询只对 systemd 做过实际运行，launchd 与计划任务的解析只有打桩验证。

## 预演（隔离副本上的升级）

```bash
agent-collab migrate rehearse --backup <备份集目录> --work <空目录> --config <生产 config.json 的副本> --out <报告.json>
```

`--backup` 是 `controller backup` 直接写出的目录（根部有 `backup-manifest.json`）。定时备份的打包集合要先核对 `COMPLETE.json` 里的 `tar_sha256`，把 `backup.tar` 完整解到一个独立的空目录，再把该目录传给 `--backup`。

预演把备份集恢复到 `--work`，用新版本代码打开，核对：controller 身份不变；节点身份与节点凭据集合不变；每个任务的契约、`request_id`、`request_hash` 不变；已结束任务的状态与交付版本不变；检查点、发布与验收记录不变；模型设置按规则迁移（升级前的数据库得到 `gpt-6-astra / xhigh` 与 `high、xhigh、max` 的许可范围，协调模型取 `--config` 里的 profile）；旧根工作包的策略可读；已确认检查点引用的 blob 齐全；事件历史保留。

副本的恢复代次加一、派单暂停、原有 attempt 全部撤销，旧 attempt 的迟到结果在副本上被隔离（报告的 `late_results_fenced`）。预演全程不监听端口，副本接不到任何节点；生产数据目录不被接触，备份集的清单、数据库与 blob 内容不变（报告的 `backup_content_unchanged`；预演读取原始备份集时以只读、immutable 方式打开数据库，不加锁也不创建旁文件；恢复只写入独立副本）。预演后删除 `--work` 即可。

报告的 `nodes_without_model_catalog` 列出仍参与运行（未排空、未退役）而尚未上报模型目录的节点：controller 升级后只会按旧适配器的固定组合给它们派单。已排空且不可达、或已退役的节点不在其中，也不计入切换前置条件。

## 迁移对象

| 对象 | 处理 |
| --- | --- |
| controller 数据库、blob、`config.json`、备份集 | 原位保留；升级后首次启动写入模型设置迁移值 |
| 任务历史、检查点、发布与验收记录、恢复包 | 随数据库与 blob 保留，契约与请求哈希不重算 |
| 节点身份、节点凭据、节点数据目录、服务定义与运行用户 | 原位保留，worker 原位升级，不重新配对 |
| 模型 CLI 的登录 | 留在原机器、原用户的官方 CLI 环境；不复制、不经 controller 中转 |
| 回写目标是共享目录路径的项目绑定 | 改为节点本地路径；只处理本项目使用的路径，不动其他共享文件 |
| 使用管理令牌文件的日常脚本 | 改用配对得到的客户端凭据（`agent-collab client pair`） |
| 浏览器控制台的登录 | 改用桌面端配对；切换完成后设 `web_console: false` |
| 旧 launcher、提交包装、相关自启动项与 Skill 引用 | 退役；入口换成 `agent-collab submit / job` 与 `docs/integration.md` |
| 旧 launcher 的任务历史 | 只读归档，不转换成活动任务 |

## 切换顺序

任何时刻只有一个 controller 在写这份数据库，每个节点只有一套监督机制。普通提交是否开放由 controller 的提交闸门决定（`docs/protocol.md` §13.9），不靠“这段时间不要提交”的约定。

1. **准备。** 用切换当天、保存在 controller 主机之外的最新一致备份完成预演，报告全部通过；各机器预检的冲突已处理。更早的备份上做过的预演只说明机制可用，不能代替这一次。
2. **停止普通提交。** 旧版本 controller 没有提交闸门：停掉旧入口本身（旧 launcher 及其自启动项、提交包装、使用管理令牌的定时脚本），并核对 `GET /overview` 与 `GET /jobs` 不再有新任务出现。
3. **排空并暂停。** 让在途任务完成，或有序取消；确认没有活动 attempt、没有未结束的任务（预检的 `no_active_attempts`、`no_non_terminal_jobs`），旧 launcher 没有运行中或状态不明的任务（`legacy_tasks_idle`）。然后暂停派单（`POST /dispatch`，`paused: true`）。
4. **一致备份。** 在暂停状态下做一次备份并复制到 controller 主机之外；记下备份清单里的 `event_seq`（旧版本写出的清单没有这一项时，记下此刻 `GET /events` 的最大序号）。回退边界从这一点算起。
5. **升级并静态核对。** 停止旧版本的 worker 服务，升级各节点（节点兼容旧 controller：不上报模型目录、接受旧的协调决策 schema），在旧 controller 上确认心跳与认证探测正常。停止 controller，升级，启动。首次启动写入模型设置迁移值；不迁移活进程、内存中的会话或未确认的状态。核对 controller 身份与恢复代次未变、派单仍然暂停、仍参与运行的节点（未排空、未退役）全部在线并上报模型目录、设置为迁移值。
6. **只开放验收提交，受控恢复派单。** 把提交闸门设为 `acceptance`（`POST /intake`，带原因），再恢复派单。此时 controller 只接受验收运行：由具备 `maintain` 权限的主体提交、带 `acceptance_run` 标记、不属于任何项目、没有回写目标、不声明外部副作用；其余提交一律以 `intake_closed` 被拒绝，包括用管理令牌的。验收运行的输入用专门准备的小型测试内容，工作区是节点上隔离的 attempt 目录。
7. **验收。** 验收运行经 `agent-collab submit <任务文件>` 提交，任务文件里带 `"acceptance_run": true`（桌面端与浏览器页面的提交入口不发送这个标记，这一阶段会被 `intake_closed` 拒绝）。各节点在服务进程上下文里完成一次真实的执行与协调；自动化经 `agent-collab` 入口提交并取回结果；桌面端连接后查看这些任务的执行、检查与成果并另存。这些任务都带 `acceptance_run` 标记（`GET /jobs?acceptance_run=1`）。桌面端自己的“提交 → 执行 → 检查 → 成果”闭环在第 8 步开放普通提交之后做。
8. **开放普通提交。** 确认 `GET /activity?after=<第 4 步的 event_seq>` 的 `ordinary_jobs` 为 0、`disposable` 为 true（切换以来只有验收状态），把提交闸门设回 `open`，通知使用者改用桌面端与 `agent-collab` 入口。
9. **退役旧的日常入口。** 关闭浏览器控制台（`web_console: false`），更新引用旧 launcher、旧提交包装的说明。保留的旧安装包与备份只作被动恢复材料。

## 后台版本、来源与中断对账

后台发行版按 `X.Y.Z[{a|b|rc}N][.devN]` 的 PEP 440 子集比较，必须有三段数字；普通更新不隐式降级。
降级须使用记录的 `install --rollback` 或明确 `--allow-downgrade`，仍受当前数据库 schema 范围约束，不自动恢复旧数据库。
升级前可用 `agent-collab release compat --release <包> --as worker|controller --controller-info <GET-client.json>` 核对；
在线模式用 `--controller <URL>` 与现有 CLI 连接参数。accept/degraded/reject 退出码为 0/1/2，缺可选功能和未核对事项明确列出。
先升级节点再升级 controller；协议不匹配、缺必需功能或达不到最低版本时拒绝。旧 controller 缺兼容字段时不能把 degraded 当成完整验收。

独立发行包的 OpenSSH 命名空间与身份固定为 `agent-collab-release`。首次 `--allowed-signers` 固定信任，
升级与回退重新验签；换信任需 `--replace-trust`，固定后无签名需显式 `--allow-unsigned`。
逐文件哈希与来源验签分别核对，构建脚本不签名。完整命令见 [安装运维](install.md#发行签名版本与兼容)。

本地更新前拒绝 preparing/running attempts 与 claimed/queued/running/finished 写回操作；finished 的结果尚未报告。
排空同时等待 `active_attempts=0` 与可用的 `pending_writebacks=0`；旧 controller 缺后者时提示本次未核对写回。
安装器将 compat、verify、prepare、backup、switch、health 持久写入 `update-journal.json`。
中断后先 `install --discover` 只读查看；`--recover` 按 journal、current/current.txt 与 installation.json 对账，
切换前恢复旧版本记 failed，切换后完成目标版本记 done，也可 `--recover --rollback` 明确回退；无法确认时保留 pending。
所有路径保持 desired，停用组件仅做目标解释器导入与 manifest/schema 离线检查，不启动服务、不监听端口。
没有 journal 的旧安装可直接升级；兼容更新与恢复不调用数据库 restore 或切换屏障，不改变恢复代次。

## 执行权的撤销：普通重启、恢复与切换屏障

三种操作对“切换之前签发的东西”的影响不同，不能互相代替。

| 操作 | 恢复代次 | 进行中的 attempt 与租约 | 协调租约 | 节点凭据、客户端凭据、管理令牌 |
| --- | --- | --- | --- | --- |
| 普通的停止再启动（原位升级就是这样） | 不变 | 保留；启动时把租约顺延一个周期，节点重连后继续 | 未到期的保留 | 不变 |
| 从备份恢复（`controller restore`） | 加一 | 全部撤销：进行中的记为失联，任务停在待对账，派单暂停 | 全部失效 | 可读当前库的撤销按拒绝优先结转；不可读时须显式 keep 或 revoke-all；会话与配对码清除 |
| 切换屏障（`controller barrier`） | 加一 | 同上，但数据不回退 | 全部失效 | 不变 |

- 普通的原位升级不会让升级前签发的请求失效。本文的切换顺序靠第 3 步的前置条件（升级时没有活动 attempt、没有未结束的任务）保证升级后不存在在途的执行权，而不是靠升级本身。升级前已经被取代、撤销或失联的 attempt，其迟到请求继续被 epoch 与撤销标记拒绝（`stale_epoch`、`revoked`），与升级无关。
- 升级前已经结束的任务，它的检查点、验收、发布回执和幂等回执仍然有效：例如对一个已完成、尚未发布的任务，升级后仍可以发布它切换前的成果。这些是合法的历史，不属于“必须失效的旧请求”。
- 需要让切换前签发的执行权**全部**失效时（例如第 3 步的前置条件无法完全满足，或要确保任何残留的旧进程都无法再提交结果），在 controller 停止后运行切换屏障。屏障要求派单已暂停、提交闸门不是 `open`，而旧版本没有提交闸门，所以顺序是：按第 5 步升级并启动新 controller（派单保持暂停），把提交闸门设为 `closed` 或 `acceptance`，停止 controller，运行屏障，再启动并从第 6 步继续：

  ```bash
  agent-collab controller barrier --data-dir <controller 数据目录> --request-id <新 UUID> --reason "<原因>"
  ```

  前置条件：controller 已停止（命令取得数据目录的锁），派单已暂停，提交闸门不是 `open`，最新成功 attempt 的最新 durable 成果已发布或明确弃置。先用 `agent-collab maintenance barrier-preview` 查看清单；未采用的成果用 `POST /jobs/{id}/dismiss`（request_id、expected_version、reason）记录弃置。barrier 在同一个写事务里重新检查待发布成果与上述门禁，有未处理项即返回 barrier_pending_results 并列出；不满足时拒绝。效果见上表：恢复代次加一，全部 attempt 的租约令牌作废，协调租约失效，每个节点下次心跳被要求对账：心跳本身照常成功，返回 `reconcile_required: true`，旧代次的 attempt 列在 `revoked` 里；对账完成之前的领取，以及旧代次的检查点、完成与发布请求以 `fenced`（`stale_generation`）被拒绝。它写一条 `controller.barrier` 事件（原因、前后代次、被撤销的活动 attempt 数）；同一 `request_id` 重复执行不再推进代次。
- 屏障（与恢复一样）之后，屏障之前已经结束、但还没发布的成果不能再发布：发布同样按 attempt 的代次核对，旧代次的成果以 `fenced/stale_generation` 被拒绝，客户端发布也一样。即使任务已结束，新的屏障前置条件也会阻断未发布且未弃置的最新成果；先发布需要保留的成果，或明确弃置，屏障之后只能重跑。旧租约的进度事件不报 409，按 `stale: true` 记录、不改变状态。
- 证明方法：在隔离副本上（预演恢复出的 `--work` 目录，或备份的副本）先建立一份屏障之前有效的执行权：恢复本身已撤销副本里原有的 attempt 并推进了代次，所以要在副本上启动 controller、恢复派单、提交一个验收运行并让测试节点领取，确认它的心跳与检查点被接受；然后暂停派单、关闭提交闸门、停止 controller、执行屏障，再用这份租约与屏障之前的代次发出心跳、检查点、完成与发布请求：心跳应当成功并返回 `reconcile_required: true`、旧 attempt 列在 `revoked` 里，检查点、完成与发布应当以 `fenced/stale_generation` 被拒绝；`tests/test_cutover.py` 对这四类请求以及“普通重启后同样的请求照常被接受”做了对照。恢复预演（`late_results_fenced`）证明的是恢复路径，不能用来说明普通原位升级的效果。

## 回退边界

使用 `agent-collab maintenance restore-preview <id>` 比较第 4 步备份与当前状态。预览核对载荷、任务/attempt/检查点/验收/发布、已写与待确认应用、项目及模型/协调设置 revision、客户端和节点配对/撤销/退役/凭据变化，逐项列出恢复影响。

- verdict=safe：只有可丢弃的验收状态、没有需要对账的其他事实。暂停派单并停止 controller 与 worker 后恢复；验收状态随之丢弃，执行代次推进。
- verdict=needs_reconcile：先关闭普通提交，备份当前状态，按 changes、unresolved_applications、identity 逐项对账，优先向前修复。节点文件写入不会被数据库回退撤回；信息不足也属于此类。
- verdict=blocked：备份损坏或不兼容，按具体对象/错误码修复恢复材料，不能继续恢复。
- disposable 继续展示，但只统计任务与写回，不覆盖项目、设置与配对，true 不能替代综合 verdict。验收窗口新项目会丢失，新客户端/节点要重新配对，备份之后已撤销授权按拒绝优先结转。恢复时当前库不可读必须显式 keep 或 revoke-all，不允许静默保留旧授权。

离线恢复在目标主机使用 `agent-collab maintenance restore`；原始备份目录/导出 tar 保持不变，结果写数据库事件及 `restore-results/<op_id>.json`。另行核对 config.json、运行账户、TLS 配置、本机绑定和应用 journal；这些不在数据库备份的载荷中。

## 最终验收

- 只有一个 controller 持有这份数据库的写入权，每个节点一套服务定义。
- 任务历史、节点身份与 controller 身份和迁移前一致。
- 每个节点在真实的服务进程上下文里完成执行与协调。
- 桌面端闭环可用；自动化经 `agent-collab` 入口可以继续协作。
- 本项目的执行与成果交接不依赖共享目录。
- 切换时不存在在途的执行权：升级前没有活动 attempt 与未结束的任务；若使用了切换屏障，屏障之前的代次与租约发出的请求被拒绝。
- 浏览器控制台、旧 launcher、旧提交包装、相关自启动项和 Skill 引用已退役。

## 切换前置项

下列各项在切换当天处理，不在此之前改动生产：

- 当天的、保存在 controller 主机之外的一致备份，以及在它上面重跑的预演。此前在较早备份上做过的预演只证明机制。
- 是否使用切换屏障的决定。不使用时，第 3 步的前置条件必须全部满足；使用时须先清点并发布或弃置最新成功 attempt 的待发布 durable 成果，屏障在同一事务里再次核对。
- 旧 launcher 状态目录里权限不可读、状态不明的任务：归档旧 launcher 状态目录时一并处理（需要管理员会话）；在那之前不单独重置它的权限。
- controller 主机之外的备份副本若已过期，先更新再切换。
- 已排空且不可达的旧节点记录：升级后用节点退役（`docs/protocol.md` §13.5）处理，保留历史引用；它们不计入“节点都上报模型目录”的条件。
- 正式身份的发行（ADR 0004）已签名、公证并经门禁：生产机器上只装正式身份的桌面端；候选与内部测试身份（`cand`、`itest`）不用于生产。正式版的实例为空，与命令行安装的生产节点是同一前缀：桌面端发现它时只报告，接管走切换步骤，不经向导替换。
- 升级顺序与兼容：先节点、后 controller；每次升级前用 `agent-collab release compat --release <发行包> --as worker|controller` 核对目标 controller（或现有节点），结论为 reject 时不升级。
- 每个节点升级前 `GET /nodes` 的 `pending_writebacks` 为 0、节点本机没有 claimed/queued/running/finished 的写回操作（安装器会拒绝）；有待确认的写回先在项目页处理。
- 生产节点的发行包带 OpenSSH 来源签名，第一次经新安装器升级时固定 `allowed_signers`（正式发行密钥，不是测试密钥）。
- 当天的实例外备份用 `agent-collab maintenance backup` 创建、`export` 导出到 controller 主机之外并核对哈希；恢复预览的 verdict 与身份结转计划随切换记录保存。
- Windows 生产节点仍是 rc10：它的周期认证探测会弹出控制台窗口（M3 联调里实测到），升级到含 `CREATE_NO_WINDOW` 修复的版本后消失；本机 controller 的计划任务改用 `pythonw.exe` 也只在新版本里。
