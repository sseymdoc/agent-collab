# 功能覆盖矩阵

桌面端与自动化入口（CLI、Agent）要覆盖的操作，以及每项落在哪个里程碑。作用目标分三类：**controller**（当前连接的控制服务）、**本机**（运行桌面端或 CLI 的这台机器）、**远端节点**（指定的执行设备）。需要远端节点管理 API 而目前没有的，列为后端工作，不由桌面端执行远程命令代替。

里程碑：M1 远程候选闭环（已实现）；M2 本机完整模式、项目与文件夹输入、完整管理操作；M3 安装、签名、更新、备份恢复与诊断；M4 生产迁移。

验收方法里的测试名指 `tests/` 下的用例；“桌面联调”指在真实桌面窗口中对 controller 做的端到端联调。

## 连接与授权

| 操作 | 目标 | API / 入口 | 需要的扩展 | 权限 | 幂等与版本 | 里程碑 | 验收方法 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 配对客户端 | controller | `POST /clients/pair`；桌面“配对新的 controller”；`client pair` | 无 | 配对码 | 配对码一次性 | M1 | `test_clients_models` 配对用例；桌面联调 |
| 查看身份、权限与 controller 能力 | controller | `GET /client` | 无 | 任意客户端凭据 | 只读 | M1 | 同上 |
| 断开、忘记连接 | 本机、controller | 原生桥 `connection_close` / `connection_forget`，`DELETE /client`；`client forget --revoke` | 无 | 自身凭据 | 可重复 | M1 | 桌面联调；`test_cli_e2e` |
| 签发客户端配对码 | controller | `controller client-code`（本机）；`POST /clients/codes` | 无 | 本机，或 maintain | request_id 不可重放 | M1 | `test_clients_models` |
| 客户端列表与撤销 | controller | `GET /clients`、`POST /clients/{id}/revoke` | 无 | maintain | request_id + expected_version | M1 | `test_clients_models` 撤销与事件流用例；桌面联调 |
| 签发节点配对码 | controller | `controller pairing-code`（本机） | API 与桌面入口 | maintain | request_id | M2 | — |

## 任务

| 操作 | 目标 | API / 入口 | 需要的扩展 | 权限 | 幂等与版本 | 里程碑 | 验收方法 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 用模板和目标新建任务 | controller | `POST /contracts/draft` → `POST /jobs`；桌面“新建任务”；`submit --template` | 无 | tasks | request_id；`expected_settings_revision` | M1 | `test_clients_models` 草拟用例；桌面联调；`test_cli_e2e` |
| 提交现成的任务文件 | controller | `POST /jobs`；`submit 任务.json` | 无 | tasks | request_id | M1 | `test_cli_e2e` |
| 登记输入基线 | controller | `PUT /blobs`、`POST /baselines`；`baseline add` | 无 | tasks | request_id；内容寻址 | M1 | `test_clients_models`；桌面联调 |
| 从本机文件夹或 Git 导入输入、排除规则 | 本机 → controller | — | 原生桥的受限文件读取；项目记录 | tasks | 内容寻址 | M2 | — |
| 任务列表、筛选、分页 | controller | `GET /jobs` | 无 | read | 只读 | M1 | 既有用例；桌面联调 |
| 任务详情、事件与日志、向前翻页 | controller | `GET /jobs/{id}`、`GET /jobs/{id}/events` | 无 | read | 只读 | M1 | `test_clients_models` |
| 调度解释 | controller | `GET /jobs/{id}/explain` | 无 | read | 只读 | M1 | `test_clients_models` 目录用例 |
| 请求取消 | controller | `POST /jobs/{id}/cancel`；`job cancel` | 无 | tasks | request_id + expected_version | M1 | 桌面联调；`test_cli_e2e` |
| 重试、迁移到别的设备 | controller | `POST /jobs/{id}/retry`；`job retry` | 无 | tasks | 同上 | M1 | 既有用例 |
| 记录验收 | controller | `POST /jobs/{id}/assess` | 无 | tasks | 同上 | M1 | 既有用例 |
| 发布成果 | controller | `POST /jobs/{id}/publish`；`job publish` | 无 | tasks | request_id + expected_version + expected_base_version | M1 | 桌面联调 |
| 成果文件清单、单文件下载、文本预览 | controller → 本机 | `GET /checkpoints/{id}/files`、`GET /blobs/{sha}`；原生桥 `blob_save`；`job artifacts --download` | 无 | read | 只读；下载校验哈希 | M1 | `test_clients_models`；`test_cli_e2e`；桌面联调 |
| 整包下载到文件夹、写回冲突处理 | 本机、远端节点 | — | 原生桥的目录写入；冲突视图 | tasks | 基线比对 | M2 | — |
| 协调租约：取得、释放、接管 | controller | `/packages/{id}/coordinator` | 无 | tasks；接管需 maintain | request_id；租约（代次, 版本） | M1 | `test_coordinator`、`test_clients_models` |
| 等待到停稳、跟随事件 | controller | `job wait`、`job follow`（事件流） | 无 | read | 只读 | M1 | `test_cli_e2e`；桌面联调 |

## 设备

| 操作 | 目标 | API / 入口 | 需要的扩展 | 权限 | 幂等与版本 | 里程碑 | 验收方法 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 设备列表、能力、认证探测、执行证据 | controller | `GET /nodes` | 无 | read | 只读 | M1 | 既有用例；桌面联调 |
| 当前任务位置（执行与协调各在哪台设备） | controller | `GET /nodes` 的 `attempts`；任务详情 | 无 | read | 只读 | M1 | 桌面联调 |
| 模型目录与拒绝派单原因 | controller | `GET /settings/models`、`GET /jobs/{id}/explain` | 无 | read | 只读 | M1 | `test_clients_models` |
| 排空、排空并迁移 | controller | `POST /nodes/{id}/drain` | 无 | devices | request_id + expected_version | M1 | 既有用例 |
| 暂停、恢复派单 | controller | `POST /dispatch` | 无 | maintain | 同上 | M1 | 既有用例 |
| 节点服务启停与状态 | 远端节点 | `agent-collab service`（节点本机） | 节点管理 API（经 controller 下发、节点在心跳中领取） | devices | 待定 | M2 | — |
| 模型 CLI 登录引导与“后台服务真正可用”的确认 | 远端节点 | 认证探测只读；登录在节点本机的官方 CLI 里完成 | 本机节点上的登录引导 | devices | — | M2 | — |
| 退役、删除节点 | controller | — | 节点退役 API | devices | expected_version | M2 | — |
| 节点升级与回退 | 远端节点 | 安装器（节点本机） | 更新通道与兼容协商 | maintain | — | M3 | — |

## 设置

| 操作 | 目标 | API / 入口 | 需要的扩展 | 权限 | 幂等与版本 | 里程碑 | 验收方法 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 读取、修改模型默认值与部署许可范围 | controller | `GET` / `PUT /settings/models`；桌面“设置”；`settings get/set` | 无 | read / settings | expected_revision | M1 | `test_clients_models`；桌面联调（含冲突） |
| 任务级模型与 effort 覆盖 | controller | 契约 `harness_params`；桌面“新建任务” | 无 | tasks | 受理时冻结 | M1 | `test_clients_models`；桌面联调 |
| 项目默认值 | controller | — | 项目记录与优先级解析 | settings | revision | M2 | — |
| 协调 profile 的节点列表与启用 | controller | `config.json`（本机） | 设置 API | settings | revision | M2 | — |
| 界面主题等本机偏好 | 本机 | 页面本地存储 | 无 | — | — | M1 | 桌面联调 |
| 关闭浏览器控制台 | controller | `config.json` 的 `web_console` | 无 | 本机 | — | M4 | `test_clients_models` |

## 维护与首次配置

| 操作 | 目标 | API / 入口 | 需要的扩展 | 权限 | 幂等与版本 | 里程碑 | 验收方法 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| controller 自检 | controller | `GET /doctor` | 无 | maintain | 只读 | M1 | 既有用例 |
| 备份状态 | controller | `GET /overview` | 无 | read | 只读 | M1 | 既有用例 |
| 触发备份、恢复、事件裁剪 | controller | `controller backup / restore / prune-events`（本机） | 维护 API 与界面 | maintain | — | M3 | — |
| 迁移预检与隔离副本上的升级预演 | 本机 | `migrate preflight`、`migrate rehearse` | 无 | 本机只读 | 只读 | M1 | `test_preflight`、`test_rehearse` |
| 本机完整模式：随包运行时、服务安装或接管已有安装 | 本机 | 安装器（命令行） | 随包运行时、安装向导 | 本机 | — | M2 | — |
| 首次配置向导 | 本机、controller | — | 向导页面 | — | — | M2 | — |
| 项目页、活动页 | controller | 事件与 `audit.action` 已有 | 页面与项目记录 | read | — | M2 | — |
| 系统通知 | 本机 | — | 原生桥通知 | — | — | M2 | — |
| 诊断导出（不含凭据） | 本机、controller | — | 导出与脱敏 | maintain | — | M3 | — |
| 桌面端、controller、节点的更新 | 本机、远端节点 | — | 签名、更新通道、兼容协商 | maintain | — | M3 | — |
