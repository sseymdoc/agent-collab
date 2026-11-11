# 现有部署迁入：预检、预演、切换与回退

现有部署原位升级到带客户端授权与模型设置的版本（`docs/protocol.md` §12），并退役旧的日常入口。controller 留在原位置升级，各节点原位置升级 worker，桌面端连接这个 controller；不另起第二个 controller 管理同一批设备。

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

报告的 `nodes_without_model_catalog` 列出尚未上报模型目录的节点：controller 升级后只会按旧适配器的固定组合给它们派单。

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

任何时刻只有一个 controller 在写这份数据库，每个节点只有一套监督机制。

1. 用当天的备份集完成预演，报告全部通过；各机器预检的冲突已处理。
2. 停止旧入口的新提交。
3. 让在途任务完成，或有序取消；确认没有活动 attempt（预检的 `no_active_attempts`、`no_non_terminal_jobs`）。旧 launcher 没有运行中或状态不明的任务（`legacy_tasks_idle`）。
4. 暂停派单，做一致备份，并复制到 controller 主机之外。
5. 停止旧版本的 worker 服务，升级各节点；节点兼容旧 controller（不上报模型目录、接受旧的协调决策 schema），升级后先在旧 controller 上确认心跳与认证探测正常。
6. 停止 controller，升级，启动。首次启动写入模型设置迁移值；不迁移活进程、内存中的会话或未确认的状态。
7. 恢复派单（总览页，或 `POST /dispatch` 设 `paused: false`），以便执行验收任务。新提交此时仍然受限：旧入口在第 2 步已停，新入口只交给执行验收的人使用。验证：controller 身份与恢复代次、节点全部在线并上报模型目录、设置为迁移值；各节点在服务进程上下文里完成一次真实的执行与协调（验收任务只用不回写真实项目的测试任务）；桌面端完成“提交 → 执行 → 检查 → 成果”的闭环；自动化经新入口提交并取回结果。
8. 开放新提交：通知使用者改用桌面端与 `agent-collab` 入口。
9. 退役旧的日常入口：关闭浏览器控制台，停用旧 launcher 及其自启动项，更新引用它们的说明。保留的旧安装包与备份只作被动恢复材料。

## 回退边界

- **新版本只跑过第 7 步的验收任务**（第 8 步之前，且验收任务没有回写真实项目）：先暂停派单，完全停止新版本的 controller 与 worker 后，用第 4 步的备份恢复，装回旧版本。验收任务的记录与工件随之丢弃；恢复会让恢复代次前进并撤销原有 attempt，这是预期行为。验收任务若回写过真实目标，按下一条处理。
- **新版本已经受理新任务、产生工件或完成写回**：不要直接恢复切换前的数据库，那会丢掉这些任务并可能重复执行、重复发布或重复写回。先停止新提交，备份当前状态并对账，优先在新版本上向前修复。确需回到旧版本时，用能读取当前数据库的兼容版本，或做受控的状态迁移。
- 无论哪种回退，旧 attempt 的迟到结果都继续被代次与 epoch 隔离，不会发布。

## 最终验收

- 只有一个 controller 持有这份数据库的写入权，每个节点一套服务定义。
- 任务历史、节点身份与 controller 身份和迁移前一致。
- 每个节点在真实的服务进程上下文里完成执行与协调。
- 桌面端闭环可用；自动化经 `agent-collab` 入口可以继续协作。
- 本项目的执行与成果交接不依赖共享目录。
- 迁移前签发的请求（旧代次、旧 epoch）无法发布。
- 浏览器控制台、旧 launcher、旧提交包装、相关自启动项和 Skill 引用已退役。
