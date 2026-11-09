# 本地发行包与安装运维（WP3）

Python 3.9+、标准库、系统自带工具；安装器不下载 Python、依赖、模型工具或发行包，不提供发布 URL。
使用可信的本地源码或已核对来源的本地发行包。manifest 校验能发现损坏/篡改文件，**不提供签名或发行者身份认证**。
共享实现为 `agent_collab/installer.py`；`install.sh` 和 `install.ps1` 只查找 Python、禁用字节码写入并转交参数。

## 构建和目录

在源码根目录运行：

```sh
python3 -B scripts/build_release.py --version 0.1.0
# dist/agent-collab-0.1.0.tar.gz 和 dist/agent-collab-0.1.0.zip
```

可用 `--output-dir /absolute/local/output` 改输出目录，用 `--source /absolute/source` 指定源码。
包内只有 `agent_collab/`、`installer/`、`release-manifest.json`，不含 tests、.dev、node_modules、
字节码、任何 `.token`、`.db` 或 SQLite WAL/SHM 文件。若 `agent_collab/static/index.html` 已构建则随包包含；
否则 `ui_built=false`，controller 仍可通过 API 使用。安装器不负责构建 UI。

manifest 记录版本、构建时间、Git HEAD（若可用）、工作区是否脏、Python 要求、schema 兼容范围、
schema 指纹、每个载荷文件的 SHA-256 与大小。manifest 自身不递归计算自己的哈希。
版本目录不可覆盖：同一个版本必须使用同一个包；修改源码后请使用新版本号。
校验在内存中完成；本版单包解压后最多 512 MiB、20,000 个文件，不适合直接打包大型模型或运行数据。

```text
<prefix>/
  versions/<version>/          # 代码和原始 manifest
  current -> versions/<version>  # POSIX 原子符号链接
  current.txt                 # Windows 原子替换的版本指针；启动脚本每次读取
  installation.json           # 安装参数、当前和上一版本；没有令牌内容
  bin/agent-collab            # POSIX 命令入口；Windows 为 agent-collab.ps1
  launcher.py, services/      # 启动器与已渲染的服务定义
<state-dir>/
  controller/                # controller.db、blobs、admin.token、config.json
  worker/                    # agent.json、node.token、attempts、工作区和 agent.log
  logs/                      # LaunchAgent/计划任务输出；Linux 服务另见 journalctl
  backups/                   # 升级前 controller 一致备份，含 blob 清单
```

prefix 与 state-dir 必须互不包含；数据库必须位于执行机器的本地磁盘，不能用 SMB/NFS 或映射的网络盘。
不自动修改 PATH、shell profile、官方工具登录配置或现有全局 Skill。

## 一键安装命令

以下在源码根目录或可信发行包的解包目录执行。POSIX 入口用 `sh installer/install.sh`，Windows 用
`& .\installer\install.ps1`；不要求改变 PowerShell 执行策略。不允许脚本执行时，由用户按组织策略处理，
或直接运行 `python -B installer/entry.py` 加同一组参数。安装前会打印步骤；先加 `--dry-run` 可预览，
此模式校验包但不写文件、不配对、不 drain、不注册/停止服务。

Linux controller（需要 systemd、useradd、chown、install；非 root 账户需要 sudo，root 使用 runuser）：

```sh
sh installer/install.sh --role controller --release /local/agent-collab-0.1.0.tar.gz \
  --prefix /opt/agent-collab --state-dir /var/lib/agent-collab --python /usr/bin/python3 \
  --controller-url https://controller.example:8443
```

安装器只为自己的目录和 system unit 执行必要的系统操作；非 root 用户会收到一次 sudo 提示。
controller 以独立低权限 `agent-collab` 用户运行；`ProtectHome=true`，代码、Python 和状态不能放在 `/home`、
`/root`、`/run/user` 下。unit 固定到 `versions/<version>`，使用 `NoNewPrivileges=true`、
`ProtectSystem=strict`、`ReadWritePaths=<state-dir>/controller`、`Restart=on-failure`。
代码不归 controller 用户所有；controller 状态子目录归该用户所有，权限为 0700。
默认绑定 `127.0.0.1:18669`；`--controller-url` 为公开 origin，不会修改网络监听范围。

Mac worker（当前登录用户，不使用 sudo）：

```sh
sh installer/install.sh --role worker --release /local/agent-collab-0.1.0.tar.gz \
  --prefix "$HOME/.local/share/agent-collab" --state-dir "$HOME/.local/state/agent-collab" \
  --controller-url https://controller.example:8443 --node-id mac-worker --pairing-code ONE_TIME_CODE
```

Linux/WSL worker 使用同一命令，替换 node-id。其服务为 `systemctl --user` unit，需要可用的用户 systemd 会话。
安装器不启用 lingering。WSL 生命周期受 Windows 宿主、WSL 停机和用户会话限制；Windows 关机时不能保持运行。
同一台 Windows 和 WSL 节点应显式传相同 `--physical-host-id desktop-1 --failure-domain desktop-1`，
并按 `docs/agent.md` 设置相同 host_slots；安装器沿用 agent 默认 slots/host_slots=1。

Windows worker（当前用户 PowerShell 5.1+/7）：

```powershell
& .\installer\install.ps1 --role worker --release C:\local\agent-collab-0.1.0.zip `
  --prefix "$env:LOCALAPPDATA\agent-collab\code" --state-dir "$env:LOCALAPPDATA\agent-collab\state" `
  --controller-url https://controller.example:8443 --node-id windows-worker --pairing-code ONE_TIME_CODE `
  --physical-host-id desktop-1 --failure-domain desktop-1
```

Windows 使用当前交互用户 SID、InteractiveToken、LeastPrivilege、“登录时”计划任务，安装后主动运行一次。
这是 **Task Scheduler 任务，不是 SCM 服务**。本版不实现需要服务账户密码的 SCM 模式，不使用 LocalSystem，
不索取账户密码。登出后没有交互令牌，不能保证任务继续运行。worker 优先用 `--python` 同目录的 pythonw.exe，
不存在时沿用指定解释器（通常为 python.exe）。登录触发器每 5 分钟无限重复（无 Duration，StopAtDurationEnd=false），
用于拉起已退出进程；MultipleInstancesPolicy=IgnoreNew 防止重叠，并保留失败重试间隔 1 分钟、最多 3 次。
agent.lock 在 Windows 使用非阻塞字节锁、POSIX 使用 flock，第二个同数据目录进程立即失败。

Windows worker 缺省工作区为 `%SystemDrive%\agent-collab-work\<用户名>`，可用 `--workspace-root` 指定。
已有 agent.json 的 workspace_root 优先，安装与升级不覆盖它或其他自定义键。数据目录仍保留令牌、输出、状态。
安装器对专用共享根及用户目录执行 icacls：重置旧 ACL、移除继承，当前用户、SYSTEM、Administrators 获得
(OI)(CI) 完全控制；若本地 CodexSandboxUsers 组已存在，额外只授予这两个文件夹 RX，不向后代继承。
不创建该组。自定义工作区只调整所选目录，既有父目录的访问能力由运维提供，不改任意共享父目录 ACL。
这些操作可能需要当前用户拥有目录 ACL 管理权限；失败会中止安装事务，保留目录供审查。
macOS 使用 `~/Library/LaunchAgents/org.agent-collab.worker.plist`；只在用户登录后的 GUI 会话运行，
登出后不运行，不提升为 root。Mac/Windows controller 也使用当前用户的 LaunchAgent/计划任务，适合本地开发。

all-in-one（本机开发；会初始化本地 controller、自动生成一次性配对码并配对本机 worker）：

```sh
sh installer/install.sh --role all-in-one --release /local/agent-collab-0.1.0.tar.gz \
  --prefix "$HOME/.local/share/agent-collab" --state-dir "$HOME/.local/state/agent-collab" \
  --node-id local-worker
```

上例用于 Mac。Linux all-in-one 使用 `/opt/agent-collab` 和 `/var/lib/agent-collab`，由普通登录用户调用，
controller 的 system unit 通过 sudo 管理，worker 始终使用该普通用户的 systemd user unit；拒绝以 root 安装 worker。
Windows 将入口换为 `.\installer\install.ps1`、路径换为本地磁盘，`--role all-in-one` 参数相同。
无真实服务的离线演练统一加 `--no-service`；此时不监听、不启动 worker，也不自动配对 all-in-one。

## 首次需要用户完成的步骤

1. 用户在 worker 本机安装和登录所需的官方模型工具。安装器不运行 codex/claude，不复制或代理登录凭据。
2. 在 controller 上生成针对指定 node-id 的一次性配对码，然后交给 worker 安装命令。
   Linux 系统服务数据归专用用户所有，例如：

   ```sh
   cd /opt/agent-collab/current
   sudo -u agent-collab /usr/bin/python3 -B -m agent_collab.controller pairing-code \
     --data-dir /var/lib/agent-collab/controller --node-id mac-worker --ttl 600
   ```

   已有 `agent.json` 与 `node.token` 时忽略新配对码并保留身份。仅存在其中一个时拒绝覆盖，需人工核对。
   配对码属于短期凭据；避免把实际值保存在共享命令记录中。安装器不把码或令牌内容写入安装记录/步骤输出。
3. 审查 `installer/services/Caddyfile.snippet` 的域名、HTTPS 端口与本地端口，将片段合入完整配置后由运维执行
   `caddy validate` 和 reload。安装器仅打印片段和审查步骤，不改 Caddy/Nginx、DNS、安全组、SSH。

   **回退本项目站点**：不要用部署前的旧 Caddyfile 整体覆盖当前文件（部署之后无关站点可能已变化）。只移除本项目站点块：

   ```sh
   cp -p /etc/caddy/Caddyfile /etc/caddy/Caddyfile.before-remove-agent-collab-$(date -u +%Y%m%dT%H%M%SZ)
   python3 /opt/agent-collab/current/installer/caddy_site.py remove --file /etc/caddy/Caddyfile --site https://<DOMAIN>:<HTTPS_PORT> --out /tmp/Caddyfile.new
   diff /etc/caddy/Caddyfile /tmp/Caddyfile.new        # 确认只少了本项目站点块及其上方注释
   caddy validate --config /tmp/Caddyfile.new --adapter caddyfile && \
     install -m 0644 /tmp/Caddyfile.new /etc/caddy/Caddyfile && systemctl reload caddy
   ```

   validate 失败时不替换、不 reload；diff 出现其他站点变化时停止并人工处理。`caddy_site.py` 只按完全相同的站点行定位唯一块，块内出现引号中的大括号时拒绝自动处理。
   公开 origin 必须与实际反代入口一致，HTTPS 验证不会关闭。自建 CA 的 worker 配对可加 `--ca-file`。

## 参数契约（两种脚本相同）

| 参数 | 行为 |
| --- | --- |
| `--role controller\|worker\|all-in-one` | 新安装默认 worker；已有安装沿用角色，不允许原地改角色 |
| `--release <本地包>` | 安装/升级必需；只接受本地产物 tar.gz 或 zip |
| `--version <版本>` | 安装时断言与 manifest 相同；回退时断言为记录的上一版本 |
| `--prefix <目录>` | 专用代码目录；Linux 受管 controller/all-in-one 默认 `/opt/agent-collab`，其余 `~/.local/share/agent-collab` |
| `--state-dir <目录>` | 独立数据目录；Linux 受管 controller/all-in-one 默认 `/var/lib/agent-collab`，其余 `~/.local/state/agent-collab` |
| `--workspace-root <目录>` | 新 worker 工作区根；Windows 默认驱动器根下 agent-collab-work/用户名；已有 agent.json 配置优先 |
| `--controller-url <origin>` | worker 控制端地址 / controller 公开源；默认 `http://127.0.0.1:18669`；已有安装沿用保存值 |
| `--pairing-code <一次性码>` | 只为新 worker 配对；重跑保留旧身份 |
| `--node-id <节点 ID>` | 默认从本机主机名生成小写 ID；建议显式指定 |
| `--python <可执行文件>` | Python 3.9+ 的单个程序路径，不接受额外 shell 参数 |
| `--no-service` | 不注册、不停止、不启动系统服务；渲染模板留在 prefix；安装后 doctor 仅检查离线事实 |
| `--dry-run` | 验证包并打印步骤，不写文件或调用变更 API |
| `--rollback` | 回到记录的上一版本；先检查当前 schema，不能单纯换二进制 |
| `--uninstall` | 移除服务、启动器、current 和当前活动版本；保留数据与其他归档版本 |
| `--purge-code` | 仅与卸载合用，额外删除整个 versions；仍不删除 state-dir |
| `--admin-token-file <文件>` | drain 所需管理员令牌文件；只读取，不复制；all-in-one 默认读取本地 controller/admin.token |
| `--drain` | 只排空并等待，完成后保持 drain=true，供独立维护使用 |
| `--drain-timeout <秒>` | 默认 300；超时中止升级，旧版本不被停止，尝试恢复原 drain 状态 |
| `--health-timeout <秒>` | 启动后检查窗口，默认 60；worker 版本心跳确认最多 60 秒，之后继续 doctor |
| `--allow-active` | 显式跳过本地 preparing/running attempt 拒绝；仍执行 drain 和 OS 锁检查，不绕过进程所有权 |
| `--bind <loopback:port>` | controller 监听地址；默认 `127.0.0.1:18669`，不允许非 loopback |
| `--ca-file <文件>` | 新 worker 的自建 CA；已有身份继续使用原配置 |
| `--physical-host-id` / `--failure-domain` | 转交首次 agent pair；已有身份不改写 |

已有安装建议始终传同一个 `--prefix`；其 state-dir、角色、Python 和连接参数会从安装记录读取。
新建非默认 `--bind` 时也应传正确的本地 `--controller-url`。服务托管模式不允许原地切换：
已注册服务不能用 `--no-service` 跳过停止；离线安装升级继续使用 `--no-service`。
从离线演练迁入托管环境需使用单独审查的 prefix，并核对状态目录所有权。
离线模式要求调用者手工停止所有运行进程；安装器用数据目录锁再次确认，否则拒绝切换。

## Doctor、drain、升级、回退

安装后的 POSIX 命令入口示例（无需修改 PATH）：

```sh
PREFIX="$HOME/.local/share/agent-collab"
"$PREFIX/bin/agent-collab" doctor --prefix "$PREFIX" --role worker
# 离线只检查 current、manifest、schema，运行状态显式 unknown：
"$PREFIX/bin/agent-collab" doctor --prefix "$PREFIX" --role worker --no-service

"$PREFIX/bin/agent-collab" install --prefix "$PREFIX" --drain \
  --admin-token-file /local/authorized-admin.token --drain-timeout 300

"$PREFIX/bin/agent-collab" install --prefix "$PREFIX" --release /local/agent-collab-0.2.0.tar.gz \
  --admin-token-file /local/authorized-admin.token

"$PREFIX/bin/agent-collab" install --prefix "$PREFIX" --rollback \
  --admin-token-file /local/authorized-admin.token
```

Windows 对应入口为 `& "$Prefix\bin\agent-collab.ps1" doctor ...` / `install ...`，参数相同。
controller-only 升级不需要远端管理员令牌；离线安装的上述 install/drain/rollback 命令继续加 `--no-service`。
远端 worker 的 drain API 只接受管理员授权；**节点令牌没有 drain 权限**。本版没有委托式维护凭据接口，
管理员令牌需由有权限的运维通过已有安全渠道在执行机器准备，或由主控扩展协议后再接入。安装器不会自行复制它。

排空使用当前 node version + 新 request_id，等待 `active_attempts=0`，缺失字段不视为空闲。
drain 不强杀活动 attempt；超时或网络错误在停服务之前中止。升级完成会恢复原 drain 值。
独立 `--drain` 后要恢复接单，先查询最新版本，再显式提交：

```sh
"$PREFIX/bin/agent-collab" api GET /nodes --url https://controller.example:8443 --token-file /local/authorized-admin.token
"$PREFIX/bin/agent-collab" api POST /nodes/mac-worker/drain \
  --data '{"expected_version":42,"drain":false}' \
  --url https://controller.example:8443 --token-file /local/authorized-admin.token
```

将 42 换成 GET 返回的该节点当前版本；CLI 会补 request_id。

升级/回退首先检查本地 attempts/*/meta.json；有 preparing/running 时提示先排空并拒绝，除非显式 --allow-active。
停服取得锁后再次检查，防止检查到停服之间新领任务。升级保留 agent.json 自定义键，不重配已有节点身份。
升级顺序：验证所有包文件和本地活动状态 → 写入新 versions 目录 → 排空 worker → 停服务/确认锁 → controller backup →
切换指针和固定版本服务定义 → 启动 → 核对 worker status.json → doctor → 恢复 drain → 写成功记录。失败会停止新版本，
重新验证旧 manifest 与数据库兼容性后切回旧版并启动。失败的新代码和所有备份保留，以便审查。
worker 的 status.json 必须来自新版本目录、匹配 version/git_commit，包含有效 PID 与此次启动后的成功心跳时间；
缺失、旧进程或旧心跳不能通过。60 秒内（或显式更短 health-timeout）不能确认则回退并返回非零。
--no-service 不启动进程，不要求 status.json，运行健康仍标 unknown。目标版本目录中没有 `agent_collab/release.py`
（rc4 及更早，不写 status.json）时，安装器只能用服务管理器与 doctor 确认并打印说明，不能证明实际进程来自该版本目录；
回退到这类版本后须另行核对进程（Windows 任务命令行/启动时间、systemd MainPID 与 WorkingDirectory、launchctl pid）。
管理员令牌不必在 worker 磁盘落盘：POSIX 可用 `--admin-token-file /dev/stdin` 经加密通道传入。
本地备份不是实例外备份，运维仍需安排实例外存储。

当前 WP1 数据库没有 versioned migration：`PRAGMA user_version=0`。本版 release 声明支持 0，
并对版本 0 比较 sqlite_master schema 指纹；未来 DDL 改动不能继续沿用 0。主控需让 controller 的迁移代码维护
`user_version`，同时更新 `installer/release-schema.json` 的最低/最高支持版本；安装器不会伪造 schema 迁移。
当前 schema 超出目标范围，或版本 0 指纹不同，回退拒绝执行；自动失败回退也遵守相同规则。

需要恢复数据时，停止 controller 后使用支持该备份的版本执行：

```sh
python3 -B -m agent_collab.controller restore --data-dir /local/state/controller --from /local/state/backups/upgrade-...
```

restore 会递增 `authority_generation`、撤销旧租约、暂停派单，并要求人工对账；不要手工覆盖数据库或只改 current。
安装过程中断或 schema 不兼容导致自动回退拒绝时，安装器保留不一致的指针/记录并拒绝继续猜测；
先检查备份、current、installation.json 与服务定义，在恢复兼容数据后由运维核对并修复记录。
`.install-lock` 采用独占目录；异常断电后只应在确认没有安装器运行且核对上述状态后清理此锁。

doctor 汇总原有 controller `/api/v1/doctor` 与 `node_agent doctor` 输出、服务状态、current 和 manifest 校验。
controller doctor 需要管理员权限；组件不可达时报告 `unknown` 与错误类型，返回非零。
`--no-service` 的 ok 仅说明离线结构检查通过，不代表服务运行、TLS 连通、模型工具登录或真实任务已验收。

## 卸载与验证边界

```sh
"$PREFIX/bin/agent-collab" install --prefix "$PREFIX" --uninstall
# 若还要删除所有历史代码（入口已卸载时使用保留的可信 installer）：
sh installer/install.sh --prefix "$PREFIX" --uninstall --purge-code
```

离线安装继续加 `--no-service`。默认删除活动版本，归档代码保留；`--purge-code` 才删除全部 versions。
任何卸载模式都不删除 state-dir、备份、节点身份、任务历史、业务工作区或官方工具登录配置，
不删除 Linux 专用账户，不改反向代理。保留最小 installation.json 以支持幂等卸载和重新安装。

测试所有临时目录都在仓库 `.dev/tmp`，不注册真实服务、不写用户全局配置、不调用模型或真实控制端：

```sh
mkdir -p .dev/tmp
TMPDIR="$PWD/.dev/tmp" /usr/bin/python3 -m unittest discover -s tests
sh -n installer/install.sh
```

Mac 上验证 Python 3.9.6 的本地 tar/zip、POSIX 入口、数据保留、升级与失败回退、schema 拒绝和服务模板。
Windows current.txt、任务 XML（pythonw、重复触发）、ACL argv、活动 attempt 拒绝、心跳身份确认失败回退均通过
纯逻辑/注入 runner 测试；没有调用真实 icacls 或注册任务。Mac 无 PowerShell，**install.ps1 尚未经过 PowerShell 解析器或实际执行**。
systemd、launchctl、schtasks 的真实注册、登录/登出、自启/重启、特权账户权限、真实 HTTP/TLS 配对及升级启动
均需目标平台后续验收，不能据模板测试声称这些已通过。
