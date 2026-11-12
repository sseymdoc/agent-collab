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

副本的恢复代次加一、派单暂停、原有 attempt 全部撤销，旧 attempt 的迟到结果在副本上被隔离（报告的 `late_results_fenced`）。预演全程不监听端口，副本接不到任何节点；生产数据目录不被接触，备份集的清单、数据库与 blob 内容不变（报告的 `backup_content_unchanged`；恢复过程会在备份集目录里留下空的 `controller.lock` 与数据库旁文件）。预演后删除 `--work` 即可。

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

## 执行权的撤销：普通重启、恢复与切换屏障

三种操作对“切换之前签发的东西”的影响不同，不能互相代替。

| 操作 | 恢复代次 | 进行中的 attempt 与租约 | 协调租约 | 节点凭据、客户端凭据、管理令牌 |
| --- | --- | --- | --- | --- |
| 普通的停止再启动（原位升级就是这样） | 不变 | 保留；启动时把租约顺延一个周期，节点重连后继续 | 未到期的保留 | 不变 |
| 从备份恢复（`controller restore`） | 加一 | 全部撤销：进行中的记为失联，任务停在待对账，派单暂停 | 全部失效 | 节点与客户端凭据按备份里的记录；浏览器会话清除 |
| 切换屏障（`controller barrier`） | 加一 | 同上，但数据不回退 | 全部失效 | 不变 |

- 普通的原位升级不会让升级前签发的请求失效。本文的切换顺序靠第 3 步的前置条件（升级时没有活动 attempt、没有未结束的任务）保证升级后不存在在途的执行权，而不是靠升级本身。升级前已经被取代、撤销或失联的 attempt，其迟到请求继续被 epoch 与撤销标记拒绝（`stale_epoch`、`revoked`），与升级无关。
- 升级前已经结束的任务，它的检查点、验收、发布回执和幂等回执仍然有效：例如对一个已完成、尚未发布的任务，升级后仍可以发布它切换前的成果。这些是合法的历史，不属于“必须失效的旧请求”。
- 需要让切换前签发的执行权**全部**失效时（例如第 3 步的前置条件无法完全满足，或要确保任何残留的旧进程都无法再提交结果），在 controller 停止后运行切换屏障。屏障要求派单已暂停、提交闸门不是 `open`，而旧版本没有提交闸门，所以顺序是：按第 5 步升级并启动新 controller（派单保持暂停），把提交闸门设为 `closed` 或 `acceptance`，停止 controller，运行屏障，再启动并从第 6 步继续：

  ```bash
  agent-collab controller barrier --data-dir <controller 数据目录> --request-id <新 UUID> --reason "<原因>"
  ```

  前置条件：controller 已停止（命令取得数据目录的锁），派单已暂停，提交闸门不是 `open`；不满足时拒绝。效果见上表：恢复代次加一，全部 attempt 的租约令牌作废，协调租约失效，每个节点下次心跳被要求对账：心跳本身照常成功，返回 `reconcile_required: true`，旧代次的 attempt 列在 `revoked` 里；对账完成之前的领取，以及旧代次的检查点、完成与发布请求以 `fenced`（`stale_generation`）被拒绝。它写一条 `controller.barrier` 事件（原因、前后代次、被撤销的活动 attempt 数）；同一 `request_id` 重复执行不再推进代次。
- 屏障（与恢复一样）之后，屏障之前已经结束、但还没发布的成果不能再发布：发布同样按 attempt 的代次核对，旧代次的成果以 `fenced/stale_generation` 被拒绝，客户端发布也一样。前置条件把这类任务算作已结束，所以运行屏障之前先发布（或明确放弃）需要保留的成果；屏障之后只能重跑。旧租约的进度事件不报 409，按 `stale: true` 记录、不改变状态。
- 证明方法：在隔离副本上（预演恢复出的 `--work` 目录，或备份的副本）执行屏障，再用屏障之前的代次与租约发出心跳、检查点、完成与发布请求：心跳应当成功并返回 `reconcile_required: true`、旧 attempt 列在 `revoked` 里，检查点、完成与发布应当以 `fenced/stale_generation` 被拒绝；`tests/test_cutover.py` 对这四类请求以及“普通重启后同样的请求照常被接受”做了对照。恢复预演（`late_results_fenced`）证明的是恢复路径，不能用来说明普通原位升级的效果。

## 回退边界

以第 4 步备份的 `event_seq` 为界，用 `GET /activity?after=<event_seq>` 看备份之后实际产生了什么：受理的任务（区分验收运行与普通任务）、确认的检查点、发布、验收、写回与应用。

- **`disposable: true`**（备份之后只有验收运行，且没有任何写回或应用）：暂停派单，完全停止新版本的 controller 与 worker，用第 4 步的备份恢复，装回旧版本。验收运行的记录与工件随之丢弃；恢复会让恢复代次前进并撤销原有 attempt，这是预期行为。验收运行不属于项目、没有回写目标，所以没有需要另行撤回的外部写入。
- **`disposable: false`**（已经受理普通任务，或已有写回、应用）：不要直接恢复切换前的数据库，那会丢掉这些任务并可能重复执行、重复发布或重复写回。先把提交闸门设为 `closed`，备份当前状态，按 `activity` 列出的任务逐个对账，优先在新版本上向前修复。确需回到旧版本时，用能读取当前数据库的兼容版本，或做受控的状态迁移。
- `disposable` 只看任务、检查点、发布、验收、写回与应用，不看备份之后新建的项目、客户端配对与节点配对（`counts` 里仍会列出 `project.created`、`client.paired`、`node.registered`）。按 `disposable: true` 恢复备份会丢掉这些：验收期间新配对的桌面端与 CLI 要重新配对，新建的项目要重新建。提交闸门只管 `POST /jobs`，`acceptance` 模式下 `POST /projects` 照常受理；切换窗口里不要建正式项目。
- 不想回退、只想清理验收状态时不需要做任何事：验收运行带标记保留在历史里，可以随时用 `GET /jobs?acceptance_run=1` 列出，不计入任何项目。

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
- 是否使用切换屏障的决定。不使用时，第 3 步的前置条件必须全部满足。
- 旧 launcher 状态目录里权限不可读、状态不明的任务：归档旧 launcher 状态目录时一并处理（需要管理员会话）；在那之前不单独重置它的权限。
- controller 主机之外的备份副本若已过期，先更新再切换。
- 已排空且不可达的旧节点记录：升级后用节点退役（`docs/protocol.md` §13.5）处理，保留历史引用；它们不计入“节点都上报模型目录”的条件。
