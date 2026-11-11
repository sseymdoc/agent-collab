# 桌面端

桌面端是 Tauri v2 外壳加 `ui/` 的同一套 React 页面。外壳只做四件事：保管客户端凭据、代发 controller 请求、转发事件流、把工件存到用户选定的位置。调度、状态机、发布与写回规则都在 controller，桌面端不直接读写数据库，也不向节点发命令。

源码在 `desktop/`（Rust）与 `ui/`（页面）。页面在浏览器里经 Cookie 与 CSRF 访问 controller，在桌面端里经本文的原生桥访问；两种传输共用 `ui/src/api.ts` 之上的全部页面代码。

## 连接与凭据

- 一条连接 = controller 地址 + 配对时得到的 `controller_id` + 本客户端的 `client_id`。连接的非机密信息保存在应用配置目录的 `connections.json`；`client_token` 只保存在系统凭据存储（macOS 钥匙串、Windows 凭据管理器），服务名 `agent-collab`，账户名 `<controller_id>/<client_id>`。
- 凭据存储读写失败时命令返回 `secure_storage_unavailable`，不写入任何明文替代位置。
- 页面拿不到 `client_token`：它不出现在命令返回值、事件、日志和 `connections.json` 里。
- 地址必须是 `https://`；`http://` 只接受 loopback 主机。不跟随重定向。
- 每次打开或关闭连接，外壳把连接代次 `epoch` 加一。页面发出的每个请求都带它取得的 `epoch`；与当前值不同的请求被拒绝（`stale_connection`），旧代次的事件流被关闭。切换连接后，旧页面状态里的请求和弹窗因此不会作用到新的 controller。
- 每个响应的 `X-Controller-Id` 必须等于连接绑定的 `controller_id`，否则返回 `controller_mismatch` 并关闭该连接。唯一的例外是不带这个头的 5xx：它来自 controller 前面的反向代理（例如 controller 重启期间的 502），按 `network` 处理，连接保留。

## 原生桥命令

页面经 `window.__TAURI__.core.invoke(<命令>, <参数>)` 调用。失败时返回 `{"code", "message"}`；`code` 取 `invalid`、`unauthorized`（配对码无效或过期，或这条连接的凭据已被撤销）、`stale_connection`、`controller_mismatch`、`secure_storage_unavailable`、`network`（不可达、TLS 校验失败、5xx 或响应格式不对）、`not_found`、`io`。

| 命令 | 参数 | 返回 | 说明 |
| --- | --- | --- | --- |
| `app_info` | 无 | `{"version", "commit", "platform", "arch", "candidate"}` | 外壳自身的版本身份 |
| `connections_list` | 无 | `{"connections": [connection...], "active": {"id", "epoch"} \| null}` | connection 为 `{"id", "name", "url", "controller_id", "client_id", "kind", "scopes", "paired_at"}` |
| `connection_pair` | `{"url", "code", "name"}` | connection | `POST /clients/pair`（`kind: "desktop"`），凭据写入系统凭据存储后才保存连接 |
| `connection_open` | `{"id"}` | `{"epoch", "identity", "controller"}` | 读取凭据，调用 `GET /client` 并核对 `controller_id`，成功后成为当前连接 |
| `connection_close` | 无 | `{}` | 关闭当前连接与事件流，不删除连接 |
| `connection_forget` | `{"id", "revoke"}` | `{"revoked"}` | `revoke` 为 true 时先调用 `DELETE /client`（失败不阻止本地删除，返回值如实给出）；删除凭据与连接记录 |
| `api_request` | `{"epoch", "method", "path", "body"?, "headers"?}` | `{"status", "body"}` | 请求 `<url>/api/v1<path>`；`path` 以 `/` 开头且不含 `..`；`headers` 只转发 `X-Coordinator-Token`；`body` 为 JSON 文本；响应体上限 16 MiB |
| `stream_open` | `{"epoch", "cursor"}` | `{}` | 打开 `/events/stream?cursor=<cursor>`，经事件 `ac-sse` 转发 |
| `stream_close` | `{"epoch"}` | `{}` | 关闭事件流 |
| `blob_save` | `{"epoch", "sha256", "name"}` | `{"path", "bytes"}` 或 `{"cancelled": true}` | 弹出系统“另存为”对话框；下载到同目录临时文件，SHA-256 相符后改名到所选路径，不符则删除并报错 |

事件 `ac-sse` 的负载：`{"epoch", "kind", "id", "type", "data"}`。`kind` 为 `open`（已连接）、`event`（一条 SSE 事件，`id` 为序号、`type` 为事件类型、`data` 为 JSON 文本）、`snapshot_required`（`data` 含 `min_seq`）、`unauthorized`（凭据已撤销）、`closed`（连接结束，`data` 为原因）。重连、游标续读与去重在页面一侧处理，与浏览器传输共用同一段逻辑。

## 权限隔离

- 主窗口只加载随包分发的页面，不导航到外部地址；内容安全策略只允许自身资源与原生桥。
- 原生桥命令只对主窗口开放。页面里展示的模型输出、日志、Markdown 与工件一律按文本渲染，不执行其中的脚本或 HTML，因此拿不到原生桥。
- 外壳不提供任意文件读写、命令执行或 Shell 能力；唯一的文件写入是 `blob_save`，路径由系统对话框返回。
- 关闭窗口只结束界面进程。controller 与节点的任务不受影响；重新打开后页面从 controller 读取当前状态。

## 构建

需要 Rust 工具链与 Tauri CLI 2.x（已在 Windows 上用 Rust 1.95、macOS 上用 Rust 1.99 配合 Tauri CLI 2.12 构建）；Windows 另需 WebView2 运行时与 MSVC 构建工具，macOS 需 Xcode 命令行工具。源码要放在本机磁盘上构建。

```bash
cd ui && npm ci && node build.mjs          # 页面构建到 agent_collab/static/（外壳的 frontendDist）
cd ../desktop && cargo tauri build         # Windows：NSIS 安装包；macOS：.app 与 .dmg
```

- 没有 Node 的机器可以直接使用另一台机器构建好的 `agent_collab/static/`：页面产物与平台无关。
- `AGENT_COLLAB_COMMIT=<提交>` 写入 `app_info.commit`。
- 开发候选包用独立的应用标识与名称构建，数据目录与正式版分开，可并存安装：`cargo tauri build --config '{"identifier":"org.agent-collab.desktop.dev","productName":"Agent Collab Dev"}'`。版本号带预发布后缀的构建在界面上标明“开发候选版”。
- 当前产物没有代码签名与公证：Windows 安装时会出现 SmartScreen 提示，macOS 包只有临时签名，从别的机器拷入后需要用户手动放行。签名、公证与自动更新属于后续里程碑。
- `cargo test`（在 `desktop/src-tauri`）覆盖事件流帧解析、地址与路径校验、连接代次、controller 身份核对和下载落盘。

## 自动化钩子

cargo feature `automation`（缺省关闭）用于在没有界面自动化驱动的平台上验证真实窗口：启用后，若环境变量 `AGENT_COLLAB_DESKTOP_AUTOMATION` 指向一个本机 JS 文件，主窗口加载完成后执行它一次；脚本可调用 `automation_report`（参数 `{"data": ...}`），把 JSON 逐行追加到 `AGENT_COLLAB_DESKTOP_AUTOMATION_OUT` 指向的文件。未启用该 feature 的二进制里没有这两样。它只用于候选版验收，正式发行的构建不启用。

## 使用

1. 拥有者在 controller 所在机器上生成配对码：`agent-collab controller client-code --data-dir <数据目录>`。
2. 打开桌面端，在“配对新的 controller”里填 controller 地址、配对码和这台设备的名称。之后再打开时在“已配对的 controller”里点“连接”，不需要再配对。
3. “设置”里选择执行任务的默认模型与 effort；可选项来自各设备上报的模型目录。
4. “任务 → 新建任务”：选模板、写目标与验收项、选输入基线、添加检查，点“生成契约”查看将提交的完整契约，确认后提交。
5. 任务详情分别显示运行、检查、验收、发布、回写的状态，执行与协调各在哪台设备，以及成果文件（可下载或按文本查看）。
6. “断开”只关闭本窗口的连接；“忘记”删除本机保存的凭据并在 controller 上撤销它。
