# 功能覆盖矩阵

桌面端与自动化入口（CLI、Agent）要覆盖的操作，以及每项落在哪个里程碑。作用目标分三类：**controller**（当前连接的控制服务）、**本机**（运行桌面端或 CLI 的这台机器）、**远端节点**（指定的执行设备）。需要远端节点管理 API 而目前没有的，列为后端工作，不由桌面端执行远程命令代替。

里程碑：M1 远程候选闭环、M2 本机完整模式、项目与文件夹输入、完整管理操作，两者都已实现；M3 安装（签名与公证、干净机器上的安装验证）、自动更新、备份恢复与诊断的界面，M4 生产迁移，两者都未开始。标“M1 收尾”的几项（401 与 403 区分、提交回执、模型目录派单策略、凭据存储）属于 M1 范围，随 M2 一起交付。

验收方法里的测试名指 `tests/` 下的用例，“Rust 用例”指 `desktop/src-tauri/src` 下 `cargo test` 的用例。“桌面联调”指在真实桌面窗口中对 controller 做的端到端联调；“M2 桌面联调 wN / mN”是 M2 验证中的步骤名（w 为 Windows 桌面端，m 为 macOS 桌面端），连接 HTTPS 候选 controller 与两台真实节点。“M2 CLI HTTPS 实测”“M2 服务生命周期实测”“切换机制隔离预演”是同一轮验证中的实测项目。“仅自动化测试”表示有自动化测试、没有在真实窗口或真实设备上跑过；“未验证”表示两者都没有。

## 连接与授权

| 操作 | 目标 | API / 入口 | 需要的扩展 | 权限 | 幂等与版本 | 里程碑 | 验收方法 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 配对客户端 | controller | `POST /clients/pair`；桌面“配对新的 controller”；`client pair` | 无 | 配对码 | 配对码一次性 | M1 | `test_clients_models` 配对用例；桌面联调；M2 桌面联调 w1、m1 |
| 查看身份、权限与 controller 能力 | controller | `GET /client` | 无 | 任意客户端凭据 | 只读 | M1 | 同上 |
| 经 HTTPS 连接：系统根证书或自建 CA，证书不符一律拒绝 | 本机、controller | 原生桥 `connection_pair` 带 CA 证书；`client pair --ca-file` | 无 | 配对码 | 被拒的尝试不消耗配对码；用自建 CA 的连接只信任该 CA，不回退到系统根证书 | M2 | Rust `tls.rs`、`lib.rs` 用例；`test_controller_operations` 的 `test_bind_tls_policy`；M2 桌面联调 w1（系统信任、损坏的 CA、自签证书、主机名不符都被拒，自建 CA 配对成功）、m1；M2 CLI HTTPS 实测；系统信任模式下的成功路径未验证（候选环境没有公开证书） |
| 断开、忘记连接，在多个 controller 之间切换 | 本机、controller | 原生桥 `connection_close` / `connection_forget`，`DELETE /client`；`client forget --revoke` | 无 | 自身凭据 | 可重复；切换后旧连接的迟到响应被丢弃 | M1 | 桌面联调；`test_cli_e2e`；Rust `lib.rs` epoch 用例；M2 桌面联调 w5 |
| 凭据存储：缺省用系统存储，file 后端须显式选择 | 本机 | 桌面端只用系统存储；`client pair --credential-store system` 或 `file` | 无 | — | 系统存储不可用时不回退到文件；file 后端每次读取都核对文件权限 | M1 收尾 | `test_credstore`（含真实系统存储往返）；M2 CLI HTTPS 实测（凭据存入 Windows 凭据管理器） |
| 401 与 403 区分：权限不足不登出 | 本机、controller | 全部 API；403 应答带 `details.required_scope` | 无 | — | — | M1 收尾 | `test_clients_models`、`test_node_admin`、`test_cli_e2e` 的权限用例；M2 桌面联调 w7（只读客户端的设备操作按钮置灰，收到 403 后仍可浏览任务） |
| 签发客户端配对码 | controller | `controller client-code`（本机）；`POST /clients/codes` | 无 | 本机，或 maintain | request_id 不可重放 | M1 | `test_clients_models` |
| 客户端列表与撤销 | controller | `GET /clients`、`POST /clients/{id}/revoke` | 无 | maintain | request_id + expected_version | M1 | `test_clients_models` 撤销与事件流用例；桌面联调；M2 桌面联调 w11（窗口打开时撤销，页面经事件流得知并回到连接页） |

## 任务

| 操作 | 目标 | API / 入口 | 需要的扩展 | 权限 | 幂等与版本 | 里程碑 | 验收方法 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 用模板和目标新建任务 | controller | `POST /contracts/draft` → `POST /jobs`；桌面“新建任务”；`submit --template` | 无 | tasks | request_id；`expected_settings_revision` | M1 | `test_clients_models` 草拟用例；桌面联调；`test_cli_e2e`；M2 桌面联调 w2、m2 |
| 提交现成的任务文件 | controller | `POST /jobs`；`submit 任务.json` | 无 | tasks | request_id | M1 | `test_cli_e2e` |
| 提交没有得到答复时恢复 | 本机 → controller | `submit --resume <request_id>`、`submit --pending`（回执存在本机） | 无 | tasks | 原样重放原来的 request_id 与请求体 | M1 收尾 | `test_submit_receipts`；仅自动化测试 |
| 登记输入基线 | controller | `PUT /blobs`、`POST /baselines`；`baseline add` | 无 | tasks | request_id；内容寻址 | M1 | `test_clients_models`；桌面联调 |
| 任务列表、筛选、分页 | controller | `GET /jobs` | 无 | read | 只读 | M1 | 既有用例；桌面联调 |
| 任务详情、事件与日志、向前翻页 | controller | `GET /jobs/{id}`、`GET /jobs/{id}/events` | 无 | read | 只读 | M1 | `test_clients_models` |
| 调度解释 | controller | `GET /jobs/{id}/explain` | 无 | read | 只读 | M1 | `test_clients_models` 目录用例 |
| 请求取消 | controller | `POST /jobs/{id}/cancel`；`job cancel` | 无 | tasks | request_id + expected_version | M1 | 桌面联调；`test_cli_e2e` |
| 重试、迁移到别的设备 | controller | `POST /jobs/{id}/retry`；`job retry` | 无 | tasks | 同上 | M1 | 既有用例；M2 桌面联调 w8（重试） |
| 记录验收 | controller | `POST /jobs/{id}/assess` | 无 | tasks | 同上 | M1 | 既有用例；M2 桌面联调 w8 |
| 发布成果 | controller | `POST /jobs/{id}/publish`；`job publish` | 无 | tasks | request_id + expected_version + expected_base_version | M1 | 桌面联调 |
| 成果文件清单、单文件下载、文本预览 | controller → 本机 | `GET /checkpoints/{id}/files`、`GET /blobs/{sha}`；原生桥 `blob_save`；`job artifacts --download` | 无 | read | 只读；下载校验哈希 | M1 | `test_clients_models`；`test_cli_e2e`；桌面联调；M2 桌面联调 w3（文本预览）、w10（真实另存为） |
| 协调租约：取得、释放、接管 | controller | `/packages/{id}/coordinator` | 无 | tasks；接管需 maintain | request_id；租约（代次, 版本） | M1 | `test_coordinator`、`test_clients_models`；M2 桌面联调 w8（取得协调权） |
| 等待到停稳、跟随事件 | controller | `job wait`、`job follow`（事件流） | 无 | read | 只读 | M1 | `test_cli_e2e`；桌面联调 |

## 项目与成果

| 操作 | 目标 | API / 入口 | 需要的扩展 | 权限 | 幂等与版本 | 里程碑 | 验收方法 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 项目：新建、改名、归档，配置版本与修改历史 | controller | `GET` / `POST /projects`、`PUT /projects/{id}`；桌面“项目”；`project create` | 无 | read / tasks | request_id + expected_revision | M2 | `test_projects`（归档的项目保留历史、不接新任务）；M2 桌面联调 w2、m2（新建项目）；改名、归档仅自动化测试 |
| 项目默认值：优先级 任务 > 项目 > 工作区，受理时冻结 | controller | `PUT /projects/{id}` 的 `defaults`；草拟与提交带 `expected_project_revision`；桌面项目页“默认策略”；`project create --model --effort` | 无 | settings | expected_revision；草拟之后默认值变了，提交返回 409 | M2 | `test_projects` ProjectDefaultsTests；M2 桌面联调 w2（任务采用项目默认值并冻结）、m2（项目没有默认值时采用工作区默认值） |
| 项目文件夹：本机绑定、扫描、排除规则 | 本机 | 原生桥 `project_bind` / `project_bindings` / `project_scan`；`project bind`、`project scan` | 无 | — | 绑定只存在这台机器上，controller 不知道路径；Git 工作区按文件夹扫描，另记分支与未提交改动数 | M2 | `test_project_local` ScanTests（不跟随链接、缺省排除版本库元数据与凭据文件、扫描期间的变化被发现等）；Rust `projects.rs` 用例；M2 桌面联调 w2（绑定、扫描、被排除的条目可见）、w10（真实的选择文件夹对话框） |
| 上传输入并登记基线：只传缺的内容 | 本机 → controller | 原生桥 `project_upload` / `project_cancel`；`project push`；`POST /blobs/missing`、`PUT /blobs`、`POST /baselines`（带 `project_id` 与 `source`） | 无 | tasks | request_id 由项目、清单哈希与扫描时间派生；内容没变不新建基线 | M2 | `test_project_local` StageTests、`test_projects` ProjectInputTests、`test_project_e2e`；M2 桌面联调 w2（首次上传登记基线；内容不变再上传，没有新建基线）、m2 |
| 基线登记没有得到答复时重放 | 本机 → controller | `project push --resume <request_id>` | 无 | tasks | 同一 request_id 返回第一次登记的那份基线 | M2 | `test_project_replay`（`test_lost_baseline_answer_is_replayed_to_the_same_baseline`）；Rust `projects.rs` 的 request_id 对照用例；仅自动化测试 |
| 成果整包下载到文件夹并登记 | controller → 本机 | 原生桥 `result_download`；`job artifacts --download`；`POST /jobs/{id}/downloads` | 无 | read | request_id；逐个校验哈希；已下载不等于已应用 | M2 | `test_projects` ResultStateTests、`test_project_e2e`、`test_cli_e2e`；仅自动化测试（联调中没有整包下载） |
| 审阅成果并应用到本机项目目录 | 本机 | 原生桥 `result_plan` / `result_apply`；`project plan`、`project apply [--skip-conflicts]`；`POST /projects/{id}/applications` | 无 | tasks | request_id；三方比较，冲突缺省不写；只有 `applied` 记为已回写 | M2 | `test_project_local` ApplyTests、`test_projects` ResultStateTests、`test_project_e2e`；Rust `projects.rs` 用例；M2 桌面联调 w3（Windows 应用 macOS 节点上真实 Codex 回合的成果）、m2（反向：macOS 应用 Windows 节点的成果）；本机应用的冲突路径仅自动化测试 |
| 取消正在进行的应用 | 本机 | 原生桥 `project_cancel` | 无 | — | 当前文件写完后停止；已写入的保留（取消不等于回滚），记为 partial | M2 | `test_project_local` CancelTests、HelperCancelTests；Rust `projects.rs` 取消用例；M2 桌面联调 w4 |
| 恢复被打断的应用：续写、回滚、按现状结束 | 本机 | 原生桥 `result_unfinished` / `result_recover`；`project recover`（`--resume`、`--rollback`、`--abandon`） | 无 | tasks | 逐文件备份与日志；按现状结束记为结果未知 | M2 | `test_project_local` InterruptionTests；Rust `projects.rs` 续写用例；仅自动化测试 |
| 应用结果补报 | 本机 → controller | 本机先存回执，再以同一 request_id 上报 `POST /projects/{id}/applications/{application_id}` | 无 | tasks | 只向发起应用的 controller、用原来的客户端凭据补报；controller 只记一次 | M2 | `test_project_replay`（重报不重写文件、从未送达的结果在重试时记下）、`test_projects`（只有发起的客户端能上报）；Rust `projects.rs` 用例；M2 桌面联调 w5（应用期间切换连接，回到原连接后补报 1 次，文件没有重写） |
| 远端项目写回：写进节点本机绑定的目录 | 远端节点 | 节点本机 `agent-collab agent project-bind`；`POST /projects/{id}/plans`、`POST /projects/{id}/applications`（带 `node_id`）、`.../recover`；桌面“写回到节点上的目录” | 无 | tasks | request_id + `expected_base_manifest_sha256`；controller 只知道目录名；冲突缺省不写 | M2 | `test_remote_apply`；M2 桌面联调 w6（节点本机绑定、目标只显示目录名、无冲突写回、节点上改过的文件算冲突且不写）；远端恢复与节点离线超时仅自动化测试 |

## 设备

| 操作 | 目标 | API / 入口 | 需要的扩展 | 权限 | 幂等与版本 | 里程碑 | 验收方法 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 设备列表、能力、认证探测、执行证据 | controller | `GET /nodes` | 无 | read | 只读 | M1 | 既有用例；桌面联调；M2 桌面联调 w1、m1 |
| 当前任务位置（执行与协调各在哪台设备） | controller | `GET /nodes` 的 `attempts`；任务详情 | 无 | read | 只读 | M1 | 桌面联调 |
| 模型目录与拒绝派单原因 | controller | `GET /settings/models`、`GET /jobs/{id}/explain` | 无 | read | 只读 | M1 | `test_clients_models` |
| 模型目录未核实时的派单策略、目录过期 | controller | `PUT /settings/models` 的 `codex.unverified_nodes`（`wait` / `allow`）；controller 配置 `catalog_max_age_s` | 无 | settings | 受理时随模型一起冻结 | M1 收尾 | `test_catalog_policy`；M2 桌面联调 w2（草拟显示并冻结 `unverified_nodes: wait`）；目录过期仅自动化测试 |
| 排空、暂停与恢复接单、排空并迁移 | controller | `POST /nodes/{id}/drain` | 无 | devices | request_id + expected_version | M1 | 既有用例；M2 桌面联调 w7（暂停接单、恢复） |
| 暂停、恢复派单（全局） | controller | `POST /dispatch` | 无 | maintain | 同上 | M1 | 既有用例 |
| 签发节点配对码 | controller | `POST /nodes/codes`；桌面“设备 → 签发节点配对码”；`controller pairing-code`（本机） | 无 | devices | request_id 不可重放；node_id 已登记时须显式 `replace: true` | M2 | `test_node_admin` PairingCodeTests；节点用配对码加入见 M2 桌面联调 w7、w9，从桌面端签发这一步联调中没有单独记录 |
| 维护状态 | controller | `POST /nodes/{id}/maintenance`；设备页“维护” | 无 | devices | request_id + expected_version；结束维护时恢复进入前的接单状态 | M2 | `test_node_admin` MaintenanceTests；M2 桌面联调 w7（带原因进入维护） |
| 节点服务启停与状态 | 本机（节点所在设备） | `agent-collab service status` / `start` / `stop`；原生桥 `local_service`（桌面连接页“本机服务”面板） | 无：按 ADR 0003 不经 controller 远程启停（服务停下后心跳通道随之断开） | 本机 | `stop` 同时关闭自启动并记为有意停用 | M2 | `test_native_rc8` ServiceTests、`test_installer_m2`；M2 服务生命周期实测（两平台）；“本机服务”面板识别本机安装与状态见 M2 桌面联调 w9，经面板启停联调中没有操作 |
| 重新探测登录与模型目录（probe）、登录引导 | 远端节点 | `POST /nodes/{id}/operations`（`kind: probe`，节点在服务进程里执行）；设备页“重新探测”与登录引导；登录在节点本机的官方 CLI 里完成 | 无 | devices | request_id；随心跳领取，到期记 `timed_out` | M2 | `test_node_admin` OperationTests、AgentOperationTests（`test_probe_runs_in_the_worker_process_context_and_reports_login_state`）；M2 桌面联调 w7（已请求 → 节点已收到 → 成功，显示两种 CLI 的登录状态） |
| 读取原始输出（log_tail） | 远端节点 | `POST /jobs/{id}/logs`、`GET /operations/{op_id}`；任务详情“原始输出” | 无 | read | request_id；只限该任务的 attempt，分块、有上限，内容由节点脱敏 | M2 | `test_node_admin`（`test_raw_log_requests_are_bound_to_the_job_attempt_and_bounded`、`test_raw_output_is_read_in_chunks_from_the_attempt_only_and_redacted`）；M2 桌面联调 w7（取回输出清单并读取 harness 标准输出） |
| 退役节点 | controller | `POST /nodes/{id}/retire`、`GET /nodes?retired=1`；设备页“退役” | 无 | devices | request_id + expected_version；不做物理删除，记录保留为墓碑 | M2 | `test_node_admin` RetirementTests；`test_remote_apply`（已退役的节点不能写回）；M2 桌面联调 w7（有执行中的任务时被阻止并列出原因；退役后历史 attempt 仍引用它，节点离开设备列表与模型目录） |
| 节点升级与回退（更新通道） | 远端节点 | 安装器（节点本机） | 更新通道与兼容协商 | maintain | — | M3 | 未开始（本机安装器的升级与回退见“服务期望状态”一行） |

## 设置

| 操作 | 目标 | API / 入口 | 需要的扩展 | 权限 | 幂等与版本 | 里程碑 | 验收方法 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 读取、修改模型默认值与部署许可范围 | controller | `GET` / `PUT /settings/models`；桌面“设置”；`settings get/set` | 无 | read / settings | expected_revision | M1 | `test_clients_models`；桌面联调（含冲突）；M2 桌面联调 w2（保存工作区默认值） |
| 任务级模型与 effort 覆盖 | controller | 契约 `harness_params`；桌面“新建任务” | 无 | tasks | 受理时冻结 | M1 | `test_clients_models`；桌面联调 |
| 协调设置：受管协调是否启用与候选节点 | controller | `GET` / `PUT /settings/coordination`；桌面“设置 → Claude 协调策略”（协调模型与 effort 在模型设置里） | 无 | read / settings | request_id + expected_revision | M2 | `test_node_admin` CoordinationSettingsTests；M2 桌面联调 w8（保存后恢复，版本递增并留下修改记录）；受管协调回合（Claude）的实际执行未验证 |
| 界面主题等本机偏好 | 本机 | 页面本地存储 | 无 | — | — | M1 | 桌面联调 |
| 关闭浏览器控制台 | controller | `config.json` 的 `web_console` | 无 | 本机 | — | M4 | `test_clients_models` |

## 维护与首次配置

| 操作 | 目标 | API / 入口 | 需要的扩展 | 权限 | 幂等与版本 | 里程碑 | 验收方法 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| controller 自检 | controller | `GET /doctor` | 无 | maintain | 只读 | M1 | 既有用例 |
| 备份状态 | controller | `GET /overview` | 无 | read | 只读 | M1 | 既有用例 |
| 触发备份、恢复、事件裁剪 | controller | `controller backup / restore / prune-events`（本机） | 维护 API 与界面 | maintain | — | M3 | 界面未开始；命令行入口见 `test_controller_operations` BackupTest |
| 迁移预检与隔离副本上的升级预演 | 本机 | `migrate preflight`、`migrate rehearse` | 无 | 本机只读 | 只读 | M1 | `test_preflight`、`test_rehearse` |
| 提交闸门与切换屏障 | controller | `POST /intake`（`open` / `acceptance` / `closed`）、`GET /activity?after=`；`controller barrier`（本机，controller 停止时） | 无 | maintain；屏障在本机 | request_id + expected_version；屏障用同一 request_id 重放不再推进代次 | M2 | `test_cutover`；切换机制隔离预演（隔离副本上逐项通过，生产上没有执行） |
| 本机完整模式：随包运行时、安装或接管已有安装 | 本机 | 安装器 `--instance`、`--runtime`、只读发现 `install --discover`；原生桥 `local_discover` / `local_setup` | 无 | 本机 | 在同一前缀上升级，身份与数据保留；发现只读、不读令牌 | M2 | `test_installer_m2`；Rust `local.rs` 用例；M2 桌面联调 w9（只读发现；Windows 候选节点卸载后保留状态与身份，经向导用随包版本重新安装并加入同一 controller：只有一个计划任务与一个 worker 进程，节点 ID 不变） |
| 服务期望状态：有意停用经升级、回退保持 | 本机 | `agent-collab service stop` / `start`；安装器升级与回退 | 无 | 本机 | 期望状态记在安装记录里；临时停止不改变它 | M2 | `test_installer_m2`（`test_stopped_worker_upgrade_reinstall_and_rollback_all_platforms` 等）、`test_native_rc8`（`test_upgrade_and_rollback_keep_a_previously_disabled_service_stopped`）；M2 服务生命周期实测（Windows 计划任务与 macOS LaunchAgent：停用 → 升级 → 回退，全程保持停用，再启动，只有一套进程与启动项；之后运行中连续升级到最终版本）；锁屏、睡眠、注销与重新登录对用户级服务的影响未验证（只验证了关闭窗口） |
| 首次配置向导 | 本机、controller | 桌面首次使用引导：本机部署（controller 加节点）、只连接已有 controller、作为节点加入 | 无 | — | — | M2 | Rust `local.rs`（各角色的安装命令、配对码不进入返回输出）；M2 桌面联调 w9（作为节点加入，页面提示官方 CLI 须自行安装并登录）；“本机运行 controller 与节点”一项仅自动化测试 |
| 活动页 | controller | `GET /events?before=`（从新到旧翻页）；桌面“活动” | 无 | read | 只读 | M2 | `test_node_admin` 活动翻页断言；活动页联调中没有单独记录 |
| 系统通知 | 本机 | 原生桥 `notify`（`tauri-plugin-notification` 的 Rust API，不授予页面权限）；页面按本机设置决定通知哪些事件 | 无 | — | — | M2 | M2 桌面联调 w8（Windows 上调用返回成功）；macOS 未验证 |
| 系统对话框：打开证书、选择项目文件夹、另存为 | 本机 | 原生桥（`rfd`，不注册对话框插件） | 无 | — | 另存为的内容先校验哈希再落盘 | M2 | Rust `lib.rs` 落盘用例；M2 桌面联调 w10（Windows 普通候选包、真实系统对话框：打开证书、选择文件夹、另存为落盘内容与交付文件哈希一致、取消另存为不留文件）；M2 macOS 人工验证（普通候选包：真实“打开”对话框选 CA 配对、真实“选择文件夹”对话框绑定项目并扫描、“存储为”落盘内容与交付文件哈希一致、取消不留文件） |
| 诊断导出（不含凭据） | 本机、controller | — | 导出与脱敏 | maintain | — | M3 | 未开始 |
| 安装包签名与公证 | 本机 | 桌面安装包 | Windows 代码签名、macOS 签名与公证 | — | — | M3 | 未开始 |
| 干净机器上的安装验证 | 本机 | 桌面安装包 | 干净的测试机器 | — | — | M3 | 未开始（M2 候选只装在已有开发环境的两台机器上） |
| 自动更新：桌面端、controller、节点 | 本机、远端节点 | — | 签名、更新通道、兼容协商 | maintain | — | M3 | 未开始 |
| 生产迁移 | controller、远端节点 | `docs/migration.md` 的切换顺序 | 无 | maintain；本机 | — | M4 | 未开始（切换机制只在隔离副本上预演过） |
