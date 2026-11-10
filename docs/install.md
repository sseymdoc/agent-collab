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

默认生产拓扑是一个 Linux controller 加每台物理主机一个原生 worker（Windows 用计划任务，macOS 用 LaunchAgent）。
“一个 worker”指一个节点监督进程，模型子进程仍按 slots 并发。Linux/WSL worker 是可选环境，只在确有 Linux 专属任务时安装；
Windows 上的任务不需要 WSL。节点主动出站连接 controller；SSH 只是可选的管理入口，见“服务管理与 SSH 管理入口”。

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

Linux worker（可选，含按需启用的 WSL）使用同一命令，替换 node-id。其服务为 `systemctl --user` unit，需要可用的用户 systemd 会话。
安装器不启用 lingering。WSL 生命周期受 Windows 宿主、WSL 停机和用户会话限制，没有 Windows 侧保活时发行版会在空闲后停止；
它不属于默认拓扑，没有它时只要求 Linux 的任务会停在 `waiting_capability`，不会被派到 macOS 或 Windows。
与 Windows 节点同机时应显式传相同的 `--physical-host-id desktop-1`，并按 `docs/agent.md` 设置相同 host_slots；
`--failure-domain` 按节点实际所在的故障域填写（同一台物理主机上的节点通常相同）。安装器沿用 agent 默认 slots/host_slots=1。

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
不存在时沿用指定解释器（通常为 python.exe）。任务有两个触发器：登录触发器在登录时立即启动；
时间触发器以过去时刻为起点、每 1 分钟无限重复（无 Duration，StopAtDurationEnd=false），拉起已退出的进程。
MultipleInstancesPolicy=IgnoreNew 防止重叠，已有实例运行时重复触发被忽略。实测依据：进程被强制结束后
RestartOnFailure（保留，间隔 1 分钟、最多 3 次）不会重启它；挂在登录触发器上的重复要到下一次登录才生效，
而安装和升级都是在会话中途重新注册任务。安装器停止任务前先禁用它，启动时重新启用，避免触发器在切换版本期间拉起进程。
安装器在停止与启动之间被强行终止时，任务停留在禁用状态、不会自行恢复：按“Doctor、drain、升级、回退”一节核对安装状态后运行 `service start`。
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

## 服务管理与 SSH 管理入口

`service` 是固定的管理命令，本机直接运行和经 SSH 调用是同一条命令；不需要管理员令牌，也不调用 controller 的 API。
stdout 是一个 JSON 对象，退出码 0 表示其中所述状态已被证实。非默认前缀加 `--prefix`，等待时间用 `--timeout <秒>`（缺省 60，最长 600）。
服务名在整台机器上共用：服务定义属于另一个前缀的安装，或无法确认归属时，三个动作都拒绝执行。

| 命令 | 行为 |
| --- | --- |
| `service status` | 只读：当前版本、服务状态、是否自启动（enabled/disabled/unknown）、worker 的 status.json 与心跳年龄。组件都在运行时返回 0 |
| `service stop` | 有意停用：先关闭自启动（Windows 禁用计划任务，macOS `launchctl disable`，Linux `disable --now`），再停止进程，状态锁释放后才返回 0。本地有 preparing/running attempt 时拒绝，先排空或加 `--allow-active`。停用后重复触发和重新登录都不会拉起进程，直到 `service start` 或下一次升级、回退 |
| `service start` | 恢复自启动并启动。worker 以 status.json 中来自当前版本目录、时间不早于本次请求的成功心跳判定就绪；controller 以本机无认证的 `/healthz` 有应答判定就绪；否则返回非零。已在运行时重复执行不会产生第二个实例 |

```sh
"$PREFIX/bin/agent-collab" service status --prefix "$PREFIX"
```

```powershell
& "$Prefix\bin\agent-collab.ps1" service stop --prefix $Prefix
```

`service stop` 不通知 controller：要立刻停止派单，先用 API 或 `install --drain` 排空。它对活动 attempt 的检查只是停止前的一次快照，
与 worker 之间没有同步：未排空的节点在检查之后、停止之前领到的 attempt 会被打断，由 controller 按节点失效处理（满足恢复条件时从最新的 durable 检查点继续；还没有检查点时从 inputs 基线重新开始）。
要避免这种情况，先排空并等活动 attempt 归零再停。受管服务安装的升级和回退会重新启用并启动服务，
被 `service stop` 停用的节点也一样：要让它继续停用，升级或回退之后再执行一次 `service stop`。
进程异常退出由服务管理器恢复（Windows 每分钟的时间触发器、launchd KeepAlive、systemd `Restart=on-failure`），
被 `service stop` 停用的节点不受这些机制影响。

### SSH 管理入口（可选）

SSH 只用于安装、启动、诊断和恢复。任务的领取、续租、事件与工件上传都走 worker 自己的出站 HTTPS 连接：
经 SSH 发出 `service start` 后可以断开，worker 由计划任务或 LaunchAgent 托管并继续运行。任务结果从 controller 查询，
worker 是否在运行、心跳是否新鲜从 `service status` 查询。不要用 `Start-Job`、`nohup` 或 `ssh host claude ...` 代替受管服务：
直接经 SSH 打开的模型会话没有检查点，也不会被接管。固定的 `service` 命令是运维约定，SSH 本身不限制命令。

Windows 使用 Microsoft OpenSSH Server。`installer/windows-ssh-management.ps1` 在管理员 PowerShell 中运行，
只接受公钥认证、一个账户和列出的来源地址：

```powershell
& .\installer\windows-ssh-management.ps1 -AllowFrom 192.0.2.10 -PublicKey 'ssh-ed25519 AAAA... name'
& .\installer\windows-ssh-management.ps1 -Remove     # 停用入口：删规则、停并禁用 sshd、移除受管配置段
```

脚本安装 OpenSSH Server 能力（若缺少），在 `sshd_config` 顶部写入受管段（关闭口令与转发，`AllowUsers <账户>@<来源>`），
把公钥连同 `from="<来源>",restrict,pty` 写入授权文件，新增或就地更新只放行这些来源的防火墙规则，最后停用能力自带的全放行规则
（全新安装时在 sshd 第一次启动之前就停用）。端口已被其他进程占用时中止。配置写入之后任何一步失败（包括 `sshd -t` 不通过），
脚本把配置、授权文件、受管的防火墙规则和服务状态恢复到写入之前的样子：原授权文件先改名留存，失败时原样放回，本次新增的规则撤掉。
全新安装时，已安装的 OpenSSH 能力、首次启动生成的默认配置与主机密钥会保留，能力自带的全放行规则保持停用。

授权文件先在 `%ProgramData%\ssh` 里准备好并设好权限，再移到位。这个目录由 OpenSSH 创建，只有 Administrators 与 SYSTEM 可写；
它若属于其他账户或能被其他账户写入，脚本在改写配置之前中止：目录已存在时不做任何改动，全新安装时在 sshd 首次启动创建它之后再检查一次。
管理员账户在默认配置下用共享的
`administrators_authorized_keys`，只留 Administrators 与 SYSTEM；默认的 `Match Group administrators` 段被去掉时的管理员账户，以及普通账户，
用账户自己的 `.ssh\authorized_keys`，只留该账户、Administrators 与 SYSTEM，属主是该账户。已有授权文件里的密钥会保留，
所以它若属于其他账户或能被其他账户写入，脚本不做任何改动就中止，要求先检查文件内容。

受管段只在 sshd 对同一关键字“取第一个值”时才压得住后面的配置。原有配置在受管段之外含有下列指令时，脚本不做任何改动就中止，
需要先手工清理或合并：`AllowUsers`、`Port`（多条是累加的）、`Include`（被包含的文件脚本不检查）、`AuthorizedKeysCommand`、
`TrustedUserCAKeys`（会让别的密钥通过认证）、`ForceCommand`（会替换管理命令）、默认配置那两处之外的 `AuthorizedKeysFile`
（只接受 `Match` 块之外的 `.ssh/authorized_keys`，和 `Match Group administrators` 段里的共享文件），
以及 `Match` 块里对受管段所设认证与转发项的改写（对匹配的连接生效）。系统自带的默认配置不含这些指令。
它不修改 DefaultShell，调用时显式写出解释器和安装前缀（下例对应上文 Windows 安装示例的前缀）：

```sh
ssh win-host "C:\Python312\python.exe -B C:\Users\me\AppData\Local\agent-collab\code\launcher.py service status --prefix C:\Users\me\AppData\Local\agent-collab\code"
```

管理员组账户经 SSH 登录得到的是完整管理员令牌，没有 UAC 降权：持有私钥就能控制这台机器。不要经这个入口直接启动模型进程，
worker 仍由最低权限的计划任务运行。SSH 登录会话与图形登录会话也不等价：macOS 的 SSH 会话读不到登录钥匙串，
`service start` 可用是因为它只让 launchd 在已登录用户的 GUI 域里启动 LaunchAgent。
macOS 与 Linux 使用系统自带 sshd，按同样原则限制来源与账户。

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

需要恢复数据时，停止 controller 后，在支持该备份的版本目录中以 controller 服务账户执行
（`agent_collab` 只在版本目录里可导入；Linux 受管安装的 controller 数据目录属 `agent-collab` 账户、权限 0700）：

```sh
cd /opt/agent-collab/versions/<支持该备份的版本>
runuser -u agent-collab -- "<PYTHON>" -B -m agent_collab.controller restore \
  --data-dir /var/lib/agent-collab/controller --from /var/lib/agent-collab/backups/upgrade-...
```

`<PYTHON>` 用本次安装记录的解释器（安装时的 `--python`，见 `/opt/agent-collab/installation.json` 的
`config.python`），不要换成系统里随手找到的 `python3`：它可能不存在或低于 3.9。
替换时保留两侧引号：解释器路径可以含空格（如 `/opt/Python 3.12/bin/python3`）。
非 Linux 受管安装换成对应的 `--prefix`/`--state-dir`，并以安装时的用户执行。

restore 会递增 `authority_generation`、撤销旧租约、暂停派单，并要求人工对账；不要手工覆盖数据库或只改 current。
安装过程中断或 schema 不兼容导致自动回退拒绝时，安装器保留不一致的指针/记录并拒绝继续猜测；
先检查备份、current、installation.json 与服务定义，在恢复兼容数据后由运维核对并修复记录。
`.install-lock` 采用独占目录；异常断电后只应在确认没有安装器运行且核对上述状态后清理此锁。

doctor 汇总原有 controller `/api/v1/doctor` 与 `node_agent doctor` 输出、服务状态、current 和 manifest 校验。
controller doctor 需要管理员权限；组件不可达时报告 `unknown` 与错误类型，返回非零。
worker 部分含 `effective`：不含密钥的最终生效配置，用来在一处核对节点身份与并发、工作区、解释器，
以及每个 harness 的可执行文件、PATH 前置目录和 `managed_argv`（由适配器实际使用的参数构造函数生成，运行期才确定的值显示为占位符）。
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
Windows current.txt、任务 XML（pythonw、重复触发）、ACL argv、活动 attempt 拒绝、心跳身份确认失败回退、`service` 命令序列均通过
纯逻辑/注入 runner 测试；没有调用真实 icacls 或注册任务。Mac 无 PowerShell，**install.ps1 尚未经过 PowerShell 解析器或实际执行**。
systemd、launchctl、schtasks 的真实注册、登录/登出、自启/重启、特权账户权限、真实 HTTP/TLS 配对及升级启动
均需目标平台后续验收，不能据模板测试声称这些已通过。
