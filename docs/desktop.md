# 桌面端

桌面端是 Tauri v2 外壳加 `ui/` 的同一套 React 页面。调度、状态机、发布与写回规则都在 controller，桌面端不直接读写它的数据库，也不向节点发命令。外壳做这几件事：

- 保管客户端凭据，代发 controller 请求，转发事件流；
- 把工件存到用户选定的位置；
- 在用户经系统对话框选定的项目目录里读取输入、写回成果（由随包的本机助手执行，规则与 CLI 共用 `agent_collab/project.py`）；
- 发系统通知；
- 管理这台机器上的本机部署（安装、发现已有安装、服务启停）。

源码在 `desktop/`（Rust）与 `ui/`（页面）。页面在浏览器里经 Cookie 与 CSRF 访问 controller，在桌面端里经本文的原生桥访问；两种传输共用 `ui/src/api.ts` 之上的全部页面代码。需要本机文件或本机服务的功能只在桌面端出现。

## 连接与凭据

- 一条连接 = controller 地址 + 配对时得到的 `controller_id` + 本客户端的 `client_id`。连接的非机密信息保存在应用配置目录的 `connections.json`；`client_token` 只保存在系统凭据存储（macOS 钥匙串、Windows 凭据管理器），服务名 `agent-collab`，账户名 `<controller_id>/<client_id>`。
- 同一应用配置目录只运行一个实例（目录里的 `instance.lock` 加跨进程排他锁）：连接记录与凭据的修改不会被另一个实例覆盖；第二个实例启动时提示已在运行并退出。
- 凭据存储读写失败时命令返回 `secure_storage_unavailable`，不写入任何明文替代位置。
- 页面拿不到 `client_token`：它不出现在命令返回值、事件、日志和 `connections.json` 里。本机助手也拿不到它：助手不访问网络，上传与下载由外壳完成。
- 地址必须是 `https://`；`http://` 只接受 loopback 主机。不跟随重定向。证书始终校验，没有跳过校验的选项。
- 证书的信任来源：缺省是系统根证书。配对时可以为这条连接指定一份 CA 证书文件（经系统的打开文件对话框选取，PEM）：之后这条连接只信任这份 CA 签发的证书，系统根证书对它不再生效；主机名照常核对。它用于自建 CA 的部署，例如局域网里的本机 controller。连接记录里保存这份证书（公开信息）与它的 SHA-256 指纹。
- 每次打开或关闭连接，外壳把连接代次 `epoch` 加一。页面发出的每个请求都带它取得的 `epoch`；与当前值不同的请求被拒绝（`stale_connection`），旧代次的事件流被关闭。切换连接后，旧页面状态里的请求和弹窗因此不会作用到新的 controller。
- 每个响应的 `X-Controller-Id` 必须等于连接绑定的 `controller_id`，否则返回 `controller_mismatch` 并关闭该连接。唯一的例外是不带这个头的 5xx：它来自 controller 前面的反向代理（例如 controller 重启期间的 502），按 `network` 处理，连接保留。
- 401 与 403 分开处理。401（`unauthorized`）：凭据失效或已撤销，页面回到连接页。403（`forbidden`）：凭据有效但没有这项操作的权限，连接与本机凭据保留，页面在出错的操作旁说明缺少的权限；没有 `read` 权限的连接打不开事件流，页面显示“无实时事件权限”并停止重连，不离开连接。

## 原生桥命令

页面经 `window.__TAURI__.core.invoke(<命令>, <参数>)` 调用。失败时返回 `{"code", "message"}`。`code` 取值：

| code | 含义 |
| --- | --- |
| `invalid` | 参数不合要求，或 controller 以 4xx（401、403 除外）拒绝 |
| `unauthorized` | 配对码无效或过期，或这条连接的凭据已被撤销 |
| `forbidden` | 凭据有效但没有这项操作的权限 |
| `stale_connection` | 请求属于已切换或已关闭的连接 |
| `controller_mismatch` | 应答不是来自这条连接配对的 controller |
| `secure_storage_unavailable` | 系统凭据存储读写失败 |
| `network` | 不可达、TLS 校验失败、5xx 或响应格式不对 |
| `not_found` | controller 上没有这个对象，或本机没有这条连接 |
| `io` | 本机文件读写失败 |
| `not_bound` | 这个项目在这台机器上还没有选定目录 |
| `busy` | 已有一项项目操作在进行（同一时间只运行一项） |
| `cancelled` | 操作被用户取消 |
| `helper_unavailable` | 本机助手不可用（安装包不完整） |
| 其他 | 本机助手返回的错误码原样透出：`not_a_directory`、`unstable`、`path_conflict`、`unicode_conflict`、`source_changed`、`unfinished_application`、`insufficient_space`、`blob_mismatch`、`target_not_empty`、`no_application`、`no_snapshot` |

### 身份、连接、请求与事件流

| 命令 | 参数 | 返回 | 说明 |
| --- | --- | --- | --- |
| `app_info` | 无 | `{"version", "commit", "platform", "arch", "candidate", "automation", "helper", "instance"}` | 外壳自身的版本身份。`automation` 表示编译进了自动化钩子（只有内部测试包为 true）；`helper` 表示本机助手可用；`instance` 是本机部署的实例名（正式版为空串） |
| `connections_list` | 无 | `{"connections": [connection...], "active": {"id", "epoch"} \| null}` | connection 为 `{"id", "name", "url", "controller_id", "client_id", "kind", "scopes", "paired_at", "custom_ca", "ca_fingerprint"?, "revoked_at"?}`；`revoked_at` 只出现在已在 controller 上撤销、本机凭据尚待清理的连接上，这类连接只能再次“忘记” |
| `connection_pair` | `{"url", "code", "name", "custom_ca"?}` | connection，或 `{"cancelled": true}` | `POST /clients/pair`（`kind: "desktop"`），凭据写入系统凭据存储后才保存连接。`custom_ca` 为 true 时先弹出打开文件对话框选取 CA 证书；取消选取则不配对、配对码不消耗 |
| `connection_open` | `{"id"}` | `{"epoch", "identity", "controller"}` | 读取凭据，调用 `GET /client` 并核对 `controller_id`，成功后成为当前连接；带 `revoked_at` 的连接直接返回 `unauthorized`，不影响当前连接 |
| `connection_close` | `{"epoch"}` | `{}` | 关闭这一代次的连接与事件流（包括尚在核对中的打开），不删除连接；代次不是当前值时返回 `stale_connection`，不影响当前连接 |
| `connection_forget` | `{"id", "revoke"}` | `{"revoked"}` | `revoke` 为 true 时先调用 `DELETE /client`（失败不阻止本地删除，返回值如实给出）；先删除连接记录再删除凭据，凭据删除失败时恢复连接记录并返回 `secure_storage_unavailable`，若这次已确认撤销则恢复的记录带 `revoked_at`；对带 `revoked_at` 的连接再次忘记时不再撤销，`revoked` 返回 true |
| `api_request` | `{"epoch", "method", "path", "body"?, "headers"?}` | `{"status", "body"}` | 请求 `<url>/api/v1<path>`；`path` 以 `/` 开头且不含 `..`；`headers` 只转发 `X-Coordinator-Token`；`body` 为 JSON 文本；响应体上限 16 MiB。403 与其他状态码一样原样返回，由页面显示 |
| `stream_open` | `{"epoch", "cursor"}` | `{}` | 打开 `/events/stream?cursor=<cursor>`，经事件 `ac-sse` 转发；单帧保留的 `id`、`event`、`data` 与未成行的缓冲合计上限 16 MiB（重复出现的 `id`、`event` 只保留最后一个值，被覆盖的不占内存；每收到一块网络数据后检查一次，所以内存峰值可能比上限多出一块数据），超出时以 `closed`（`frame_too_large`）结束 |
| `stream_close` | `{"epoch"}` | `{}` | 关闭事件流 |
| `blob_save` | `{"epoch", "sha256", "name"}` | `{"path", "bytes"}` 或 `{"cancelled": true}` | 弹出系统“另存为”对话框；下载到同目录临时文件，SHA-256 相符后改名到所选路径，不符则删除并报错。另存单个文件不登记为“已下载” |
| `notify` | `{"title", "body"}` | `{}` | 发一条系统通知。是否通知、通知哪些事件与限流由页面按本机设置决定 |

事件 `ac-sse` 的负载：`{"epoch", "kind", "id", "type", "data"}`。`kind` 为 `open`（已连接）、`event`（一条 SSE 事件，`id` 为序号、`type` 为事件类型、`data` 为 JSON 文本）、`snapshot_required`（`data` 含 `min_seq`）、`unauthorized`（凭据已撤销）、`forbidden`（没有读取事件的权限）、`closed`（连接结束，`data` 为原因）。重连、游标续读与去重在页面一侧处理，与浏览器传输共用同一段逻辑。

### 项目目录、输入与成果

项目对应这台机器上的哪个目录，只由外壳记录（应用配置目录的 `projects.json`，按 `controller_id` 与 `project_id` 分开），不上传。目录只能经系统的选择文件夹对话框进入这张表：页面不能提供路径，下面的命令只接受 `project_id` 或 `job_id`，外壳自己查表得到目录。读写都由本机助手在这个目录之内完成，不跟随链接，路径逐级核对。

同一时间只运行一项项目操作（扫描、上传、下载、应用），再发起返回 `busy`。进度经事件 `ac-project` 发出：`{"epoch", "op": "scan|upload|download|apply|recover", "phase", "done"?, "total"?, "files"?, "bytes"?}`。`phase` 是封闭集合，助手（`agent_collab/project.py`）与外壳发出的取值相同：

| `phase` | 发出者 | 含义 |
| --- | --- | --- |
| `scan`、`rescan` | 助手 | 读目录；`rescan` 是目录在扫描期间变了、整体重扫 |
| `stage` | 助手 | 把要上传的文件按哈希暂存并核对 |
| `upload` | 外壳 | 上传缺的内容 |
| `register` | 外壳 | 向 controller 登记输入基线或应用意图 |
| `download` | 外壳 | 下载成果内容并核对哈希 |
| `write` | 助手 | 逐文件写入项目目录（含续写） |
| `report` | 外壳 | 把应用结果报给 controller |
| `cancel_requested` | 外壳 | 收到取消请求，操作还没停下 |

**取消**是请求，不是已停止。`project_cancel` 置取消标志，经取消文件通知助手（`project local ... --cancel-file`），不结束助手进程：扫描、暂存、下载、上传在下一个文件处停下，命令以 `cancelled` 返回；写入阶段（`write`）把正在写的文件写完（备份、原子替换、记日志）后不再开始下一个，命令照常返回结果，`cancelled` 为 true，未写的文件列在 `pending`，有已写入的文件时 `status` 为 `partial`、一个都没写时为 `failed`，任务不会被记为已回写。最后一个文件写完之后才到的取消不改变结果。取消不等于回滚：已写入的文件保留，需要时用 `result_recover` 回滚。只读的动作（比对、查询未完成的应用）可以直接结束助手。

**切换连接**不打断写入。每项操作开始时冻结它的连接：controller、连接 ID、地址、信任的 CA 与凭据都取自开始那一刻，目录绑定与输入基线按那条连接的 controller 查；之后切换到别的连接，操作既不改用新连接的凭据、CA、项目或绑定，也不把结果报给新的 controller。扫描与上传在下一个文件处停下；写入照常写完，结果仍用原连接报给原 controller，只是这次命令的应答不再交给页面（返回 `stale_connection`，它只表示当前页面不接收这次应答，不表示文件没写）。结果在页面上通过原连接再次打开后的 `result_unfinished` 与 controller 上的应用记录查看。

**结果上报**先存回执再发送：应用（与恢复）的最终结果连同固定的 `request_id` 写进 `projects/applies/<application_id>/report.json`（只记连接 ID，不含凭据），再用原连接发出；得到 2xx 或明确的拒绝后删除。送不到（controller 不可达、应答丢失、授权被撤销或不足）时保留，状态为“本机已完成、待向原 controller 确认”；原连接下次打开时 `result_unfinished` 按同一个 `request_id` 补报，controller 只记一次，本机文件不重写。属于别的连接的回执只列出，不用当前连接补报；原连接已被忘记或撤销时如实显示，不自动改用另一个身份。

| 命令 | 参数 | 返回 | 说明 |
| --- | --- | --- | --- |
| `project_bind` | `{"epoch", "project_id"}` | `{"binding"}` 或 `{"cancelled": true}` | 弹出选择文件夹对话框，把所选目录记为这个项目在本机的目录。binding 为 `{"project_id", "path", "name", "bound_at", "exists"}`，`name` 是目录名 |
| `project_bindings` | `{"epoch"}` | `{"bindings": [binding...]}` | 当前 controller 的全部绑定；`exists` 表示目录此刻是否还在 |
| `project_unbind` | `{"epoch", "project_id"}` | `{}` | 只删除绑定，不动目录 |
| `project_scan` | `{"epoch", "project_id", "exclude"?}` | `{"snapshot_id", "root_name", "files", "bytes", "manifest_bytes", "manifest_sha256", "exclude", "excluded": {"count", "top"}, "skipped", "skipped_counts", "git", "scanned_at"}` | 把绑定目录读成本机快照，不上传。`exclude` 给出时替换整个排除规则列表，缺省为项目的排除规则加缺省规则。返回将上传的范围：文件数、字节数、被排除的条目（前 200 个顶层条目）、被跳过的文件与原因、Git 分支与未提交改动数（不是 Git 项目为 null）。目录在扫描期间一直变化时返回 `unstable` |
| `project_upload` | `{"epoch", "project_id", "snapshot_id"}` | `{"baseline_id", "uploaded_blobs", "uploaded_bytes", "reused", "missing_blobs"}` | 问清 controller 缺哪些内容（`POST /blobs/missing`），由助手把这些文件按哈希暂存并核对，外壳逐个上传，全部到齐后登记为项目的输入基线（`POST /baselines`，带 `source`）。内容与项目最近一份基线相同时不新建（`reused: true`）。源文件在扫描后变了返回 `source_changed`，不登记；已上传的内容保留，重新扫描后再上传只传缺的。一份快照是一次登记操作：`request_id` 由快照 ID 派生，第一次发出登记之前，完整的请求体（名称与来源带这次的扫描时间）存进快照；之后对同一份快照重试（应答丢失、进程中断、取消、切换连接）原样重放，得到同一份基线，不重新取时间、不重建名称。重新扫描是新的操作 |
| `project_cancel` | `{"epoch"}` | `{"requested": true, "op", "phase", "stops": "after_current_file\|next_step"}`，没有进行中的操作时 `{"requested": false}` | 请求取消进行中的项目操作（见上文“取消”）。`stops` 说明它会在哪里停下：写入阶段是当前文件写完之后。已上传的内容保留 |
| `result_download` | `{"epoch", "job_id", "checkpoint_id"}` | `{"path", "files", "bytes"}` 或 `{"cancelled": true}` | 弹出选择文件夹对话框（目录必须为空或新建），把该检查点的全部文件下载进去：每个文件核对哈希，路径不能落到所选目录之外。完成后向 controller 登记“已下载”（`POST /jobs/{id}/downloads`） |
| `result_plan` | `{"epoch", "job_id"}` | `{"project_id", "checkpoint_id", "root_name", "changes": [{"path", "action", "state", "reason"?, "size"}], "summary"}` | 已发布的成果相对绑定目录当前内容的变更计划，不写入。`action` 为 `add`、`modify`、`delete`；`state` 为 `clean`、`already`、`conflict`。任务没有已发布的成果返回 `invalid`，项目没有绑定目录返回 `not_bound` |
| `result_apply` | `{"epoch", "job_id", "checkpoint_id", "skip_conflicts"}` | `{"application_id", "status", "changed", "already", "pending", "conflicts", "errors", "cancelled", "reported"}` | 把成果写进绑定目录。`checkpoint_id` 必须仍是该任务已发布的检查点。有冲突且 `skip_conflicts` 为 false 时不写入，`status` 为 `conflict`，不登记应用。否则：下载所需内容并核对 → 向 controller 登记应用意图（`started`）→ 助手备份、逐文件写入 → 上报最终状态。`status` 为 `applied`、`partial`、`conflict` 或 `failed`；`cancelled` 为 true 表示按取消请求停在两个文件之间；`reported` 是 controller 记下的状态，还没得到确认时为 null（回执保留待补报，见上文“结果上报”） |
| `result_unfinished` | `{"epoch"}` | `{"unfinished": [{"application_id", "root_name", "started_at", "project_id", "job_id"}], "unreported": [{"application_id", "project_id", "status", "created_at", "this_connection", "connection_id", "reported"}]}` | `unfinished`：写入开始后没有得到最终结果的应用（应用过程中窗口被关掉、进程被结束或机器掉电）。`unreported`：本机已有结果、还没得到 controller 确认的应用；`this_connection` 为 true 的在这次查询里已按原 `request_id` 补报，`reported` 是补报后 controller 记下的状态（仍为 null 表示还没送达）。页面在连接后查询并提示处理 |
| `result_recover` | `{"epoch", "application_id", "action"}` | `{"status", "interrupted", "written", "remaining", "diverged", "reported"?, ...}` | `action` 为 `status`（只核对现状）、`resume`（把没写的写完）、`rollback`（把已写入且之后未再改动的文件恢复成原样）、`abandon`（按现状结束，记为 `unknown`）。除 `status` 外，结束后向 controller 上报最终状态（同样先存回执）。只能在发起这次应用的那条连接下处理，否则返回 `not_found`。续写可以取消，规则同上 |

应用不是跨文件的原子事务：每个文件单独原子替换。备份与操作日志保存在应用本机数据目录的 `projects/applies/<application_id>/`。

### 本机部署

本机的 controller 与节点是操作系统托管的后台服务，与界面进程无关。安装包自带 Python 运行时与本产品的后台程序；模型 CLI（Codex、Claude Code）由用户按官方方式安装和登录。

| 命令 | 参数 | 返回 | 说明 |
| --- | --- | --- | --- |
| `local_discover` | 无 | `{"helper", "bundle": {"version", "runtime"}, "install", "cli": {"codex", "claude"}}` | 只读。`install` 是安装器 `install --discover` 的输出（见 `docs/install.md`）：是否已安装、版本、角色、服务状态与期望状态、节点身份、服务归属、运行上下文。`cli` 给出在这台机器上找到的官方 CLI 的路径（没找到为 null）；能否在后台服务里使用以节点的 `probe` 结果为准 |
| `local_setup` | `{"role", "controller_url"?, "pairing_code"?, "node_id"?}` | `{"ok", "output", "install"}` | 用随包的发行包与运行时安装或升级本机部署。`role` 为 `all-in-one`（本机 controller 加节点）、`worker`（此机作为节点，连接已有 controller，需要配对码）或 `controller`。运行时放进安装前缀下的受管目录，服务定义引用它；已有安装在同一前缀上升级，身份与数据保留，此前有意停用的服务保持停用。`output` 是安装器输出的末尾 |
| `local_service` | `{"action"}` | 安装器 `service` 子命令的 JSON | `action` 为 `status`、`start`、`stop`。`stop` 同时关闭自启动并记为有意停用 |
| `local_pair` | 无 | connection | 仅本机部署含 controller 时：在本机生成一次性配对码并把桌面端配对到本机 controller（`http://127.0.0.1:<端口>`）。配对码不经过页面 |

- 实例：正式版安装到缺省前缀，服务用缺省名称。开发候选版与内部测试版构建时带实例名（`app_info.instance`），安装到独立的前缀与状态目录，服务名带实例后缀，可以与同一台机器上的正式安装并存，互不触碰。
- 桌面端只管理它自己这个实例的安装。在缺省前缀上发现已有安装时（例如连接着远端 controller 的现有节点），只报告，不替换它的 controller 地址或身份。
- 远端节点的服务启停不经桌面端执行；见 `docs/protocol.md` §13.6。

## 权限隔离

- 主窗口只加载随包分发的页面（macOS 为 `tauri://localhost`，Windows 为 `http://tauri.localhost`，协议与端口精确匹配），不导航到其他地址；内容安全策略只允许自身资源与原生桥。
- 原生桥命令只对主窗口开放。页面里展示的模型输出、日志、Markdown 与工件一律按文本渲染，不执行其中的脚本或 HTML，因此拿不到原生桥。
- 外壳不提供任意文件读写、命令执行或 Shell 能力。文件写入只有三处：`blob_save` 与 `result_download` 的目标由系统对话框返回；`result_apply` 与 `result_recover` 只写用户经对话框绑定给项目的目录。读取只有项目绑定目录（`project_scan`、`project_upload`）与用户经对话框选取的 CA 证书。
- 本机助手是随包的 Python 运行时加 `agent_collab` 代码，由外壳以固定的子命令启动（`project local <动作>`、`install`、`service`、`controller client-code`），参数里的路径都来自外壳自己的记录。助手不访问网络，不读写客户端凭据。
- 关闭窗口只结束界面进程。controller 与节点的任务不受影响；重新打开后页面从 controller 读取当前状态。

## 界面关闭、退出与系统会话

| 情况 | 界面 | 本机后台服务（controller、节点） |
| --- | --- | --- |
| 关闭窗口、退出应用 | 结束 | 继续运行；正在执行的任务不受影响 |
| 锁屏 | 不变 | 继续运行 |
| 睡眠 | 暂停 | 暂停；唤醒后节点重新连接，租约已过期的任务按恢复规则处理 |
| 注销 | 结束 | 停止：服务是用户会话级的（Windows 计划任务以交互令牌运行，macOS 是 LaunchAgent） |
| 重启 | 结束 | 用户登录后自动启动；没有人登录时不运行 |
| 用户执行“停止服务” | 不变 | 停止并关闭自启动，升级与回退后保持停用，直到用户执行“启动服务” |

本产品不为实现无人登录运行而提升整个应用的权限；需要无人值守运行的部署用 Linux 上的系统服务承载 controller。

## 构建

需要 Rust 工具链与 Tauri CLI 2.x（已在 Windows 上用 Rust 1.95、macOS 上用 Rust 1.99 配合 Tauri CLI 2.12 构建）；Windows 另需 WebView2 运行时与 MSVC 构建工具，macOS 需 Xcode 命令行工具。源码要放在本机磁盘上构建。

```bash
cd ui && npm ci && node build.mjs          # 页面构建到 agent_collab/static/（外壳的 frontendDist）
cd ../desktop && cargo tauri build         # Windows：NSIS 安装包；macOS：.app 与 .dmg
```

- 没有 Node 的机器可以直接使用另一台机器构建好的 `agent_collab/static/`：页面产物与平台无关。
- `AGENT_COLLAB_COMMIT=<提交>` 写入 `app_info.commit`。
- 本机助手随包分发：构建前把 Python 运行时与 `agent_collab` 代码、发行包放进 `desktop/src-tauri/resources/`（`runtime/`、`app/`、`release/`），它们作为应用资源打进安装包。缺少这些资源的构建 `app_info.helper` 为 false，项目目录与本机部署功能不可用。
- 随包发行包的文件名在构建时写进 `AGENT_COLLAB_BUNDLED_RELEASE`（`build.rs` 读 `resources/release/` 里唯一的那个），运行时只用它：Windows 安装包覆盖升级不会删掉上一版留下的资源文件，`resources/release/` 里可能同时有新旧发行包。`resources/app/agent_collab/` 里同样可能残留上一版删掉的模块文件；它们不会被导入，除非新版本再引用同名模块。干净的做法是先卸载再安装。
- 候选包的实例名与本机端口也在构建时给出（`AGENT_COLLAB_INSTANCE`、`AGENT_COLLAB_LOCAL_PORT`），本机部署的前缀、状态目录与服务名都带实例名，与正式版分开。
- 构建身份有三种，界面与 `app_info` 如实标明：
  - 正式版：版本号不带预发布后缀，不带 `automation`，实例名为空。
  - 开发候选版（可对外试用）：版本号带预发布后缀，不带 `automation`；用独立的应用标识与实例名构建，数据目录、安装前缀与服务名都与正式版分开。
  - 内部测试版：在候选版基础上编译进 `automation` feature，只用于自动化验证真实窗口，不对外分发。文件名带 `internal-test`。
- 当前产物没有代码签名与公证：Windows 安装时会出现 SmartScreen 提示，macOS 包只有临时签名，从别的机器拷入后需要用户手动放行。签名、公证与自动更新属于后续里程碑。
- `cargo test`（在 `desktop/src-tauri`）覆盖事件流帧解析、地址与路径校验、连接代次、controller 身份核对、下载落盘、项目绑定与助手输出解析。

## 自动化钩子

cargo feature `automation`（缺省关闭）用于在没有界面自动化驱动的平台上验证真实窗口：启用后，若环境变量 `AGENT_COLLAB_DESKTOP_AUTOMATION` 指向一个本机 JS 文件，主窗口加载完成后执行它一次；脚本可调用 `automation_report`（参数 `{"data": ...}`），把 JSON 逐行追加到 `AGENT_COLLAB_DESKTOP_AUTOMATION_OUT` 指向的文件。启用时还可以用环境变量替代系统对话框的返回值（`AGENT_COLLAB_AUTOMATION_PICK_FOLDER`、`AGENT_COLLAB_AUTOMATION_PICK_FILE`、`AGENT_COLLAB_AUTOMATION_SAVE_PATH`），并用 `AGENT_COLLAB_HELPER_PYTHON`、`AGENT_COLLAB_HELPER_APP` 指定助手的位置。未启用该 feature 的二进制里没有这些；用替代返回值做的验证不算系统对话框的验证。

## 使用

1. 首次打开时选择用法：在这台机器上创建本机部署、只连接已有的 controller，或把这台机器作为节点加入已有的 controller。
2. 连接已有 controller：拥有者生成配对码（桌面端“设置 → 客户端”，或在 controller 所在机器上运行 `agent-collab controller client-code --data-dir <数据目录>`），在“配对新的 controller”里填地址、配对码和这台设备的名称。之后再打开时在“已配对的 controller”里点“连接”。
3. “设置”里选择执行任务的默认模型与 effort；可选项来自各设备上报的模型目录。还没有默认模型时，首页与新建任务页会提示先完成这一步。
4. “项目”：新建项目，选定它在这台机器上的文件夹，查看将上传的范围并调整排除规则，上传为输入。
5. “任务 → 新建任务”：选项目与模板、写目标与验收项、添加检查，点“生成契约”查看将提交的完整契约，确认后提交。
6. 任务详情分别显示运行、检查、验收、发布、下载、应用到项目的状态，执行与协调各在哪台设备。成果可以整包下载到一个文件夹，或审阅变更后应用到项目目录；有冲突的文件保留本机内容并列出。
7. “设备”：签发节点配对码，暂停与恢复接单、排空、维护、退役，查看认证与模型目录的探测结果。
8. “断开”只关闭本窗口的连接；“忘记”删除本机保存的凭据并在 controller 上撤销它。
