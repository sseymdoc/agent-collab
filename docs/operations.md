# 运维速查

安装、升级、回退、卸载的完整参数见 [install.md](install.md)。本页只列日常操作；`<CONTROLLER>` 为公开入口（如 `https://controller.example:8443`），`<DATA>` 为 controller 状态目录（Linux 默认 `/var/lib/agent-collab/controller`）。下述定时备份安装命令是待部署步骤，单元测试通过不代表已注册或已验证真实云端任务。

本页的 `"$PREFIX/bin/agent-collab"` 是安装器生成的入口（使用安装记录的解释器并从 `current` 运行）；
默认安装不会把 `agent_collab` 加入全局 Python 路径，在普通目录下 `python -m agent_collab` 无法导入。
运行前先把 `PREFIX` 设为安装时的 `--prefix`：Linux 受管 controller/all-in-one 默认 `PREFIX=/opt/agent-collab`，
其余 POSIX 安装默认 `PREFIX="$HOME/.local/share/agent-collab"`。Windows 的入口是 PowerShell 脚本
`<前缀>\bin\agent-collab.ps1`（未指定 `--prefix` 时为 `~\.local\share\agent-collab`；install.md 的示例用的是
`$env:LOCALAPPDATA\agent-collab\code`），写法见下方节点配对。

## 访问与身份

| 操作 | 命令 |
| --- | --- |
| 健康检查（无需认证） | `curl -fsS <CONTROLLER>/healthz` |
| 管理令牌位置 | `<DATA>/admin.token`（0600，属服务用户）；UI 登录与 CLI 管理调用使用；恢复到新目录后首次启动生成新令牌，原目录恢复保留原令牌 |
| UI | 浏览器打开 `<CONTROLLER>/`，输入管理令牌；会话为 HttpOnly Cookie，刷新可自动取回 CSRF |
| CLI 管理调用 | `"$PREFIX/bin/agent-collab" api GET /overview --url <CONTROLLER> --token-file <DATA>/admin.token`（证书始终校验；非 loopback 必须 https）。Linux 受管安装中令牌文件仅 `agent-collab` 可读：在 controller 主机上以 `runuser -u agent-collab --` 执行，或改用已安全交给执行用户、仅其可读的令牌副本 |
| doctor | `"$PREFIX/bin/agent-collab" doctor --prefix "$PREFIX" --role controller` |

## 提交任务与平台约束

任务经 controller 的 `POST /jobs` 提交，`agent-collab submit <任务.json>` 与 `agent-collab api POST /jobs --data <JSON|@文件>` 是同一个接口。
提交方不需要是节点：任何能以管理令牌访问 controller 的机器都可以提交，由 controller 按契约的 `requirements` 选择节点。
要把任务限定在某个平台或硬件上，写 `requirements`，不要绕过 controller 直接登录目标机器启动模型：

```json
{"title": "只在 Windows 节点执行",
 "contract": {"goal": "…", "harness": "stub", "harness_params": {"steps": [{"run": ["{python}", "-c", "print('ok')"]}]},
              "requirements": {"os": ["windows"], "arch": [], "tools": [], "gpu": null, "labels": []}}}
```

`os` 取 `windows`、`macos`、`linux`；`gpu` 可写 `"cuda"`；`tools`、`labels` 要求节点上报了对应工具或标签。没有节点满足时任务停在
`waiting_capability`，不会派到不满足条件的节点。管理令牌只在 controller 主机上时，从其他机器提交有两种做法：
经云厂商的远程执行通道在 controller 主机上以服务用户运行同一条命令（令牌不离开该主机），
或使用已安全交给执行用户、仅其可读的令牌副本直接访问 `<CONTROLLER>`：

```sh
# 在 controller 主机上（或经远程执行通道），任务内容以 base64 传入，不落盘
runuser -u agent-collab -- "$PREFIX/bin/agent-collab" api POST /jobs --data "$(echo <任务.json 的 base64> | base64 -d)" \
  --url http://127.0.0.1:18669 --token-file <DATA>/admin.token
runuser -u agent-collab -- "$PREFIX/bin/agent-collab" api GET /jobs/<job_id> --url http://127.0.0.1:18669 --token-file <DATA>/admin.token
```

## 节点配对

在 controller 主机以服务用户生成一次性配对码（10 分钟有效），不要以 root 直接写数据库：

```sh
cd /opt/agent-collab/current && runuser -u agent-collab -- python3 -B -m agent_collab.controller pairing-code --data-dir <DATA> --node-id <节点名>
```

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

```sh
# 排空：不再领取新任务；migrate=true 时把可安全迁移的 attempt（已受保护、无外部副作用、有确认检查点或基线）迁到其他合格节点
"$PREFIX/bin/agent-collab" api POST /nodes/<节点名>/drain --data '{"expected_version":<版本>,"drain":true,"migrate":true}' --url <CONTROLLER> --token-file <DATA>/admin.token
# 解除排空
"$PREFIX/bin/agent-collab" api POST /nodes/<节点名>/drain --data '{"expected_version":<版本>,"drain":false}' --url <CONTROLLER> --token-file <DATA>/admin.token
# 明确迁移/续接某个工作包（计入迁移预算，从最近确认检查点新建 attempt）。
# 要离开当前节点就把它列入 exclude_node_ids；留空表示只续接，调度器仍可能选回原节点。
"$PREFIX/bin/agent-collab" api POST /jobs/<job_id>/retry --data '{"expected_version":<版本>,"reason":"<原因>","exclude_node_ids":["<源节点名>"]}' --url <CONTROLLER> --token-file <DATA>/admin.token
# 请求取消（不会触发自动恢复；节点确认后为 cancelled）
"$PREFIX/bin/agent-collab" api POST /jobs/<job_id>/cancel --data '{"expected_version":<版本>}' --url <CONTROLLER> --token-file <DATA>/admin.token
```

`<版本>` 取自 `GET /nodes` 或 `GET /jobs/<id>` 的 `version`；过期返回 `409 version_mismatch`，重新读取后再提交。节点失联后的自动恢复无需操作：租约到期（默认 90s）即判失联，满足条件时自动在其他合格节点从确认检查点继续。合格指该节点在线（用户已登录、服务在运行）、认证正常、能取得恢复所需的检查点与输入，并具备契约 `requirements` 要求的平台、工具和硬件；只有某个平台或某块硬件能做的任务，等待符合条件的节点，不会换到别的平台上执行。

## 停用、启动与退役节点

在节点本机，或经已配置的 SSH 管理入口，使用固定命令（参数与输出见 [install.md](install.md) 的“服务管理与 SSH 管理入口”）：

```sh
"$PREFIX/bin/agent-collab" service status --prefix "$PREFIX"
"$PREFIX/bin/agent-collab" service stop   --prefix "$PREFIX"   # 有意停用，保持停用
"$PREFIX/bin/agent-collab" service start  --prefix "$PREFIX"   # 恢复自启动并启动，新心跳到达才算成功
```

让一个节点退出常驻调度（例如不再需要的 WSL 节点）：

1. 排空（`drain:true, migrate:true`），等 `active_attempts` 为 0。未结束的任务按检查点迁移；原生会话不能跨平台续接。
2. 在该节点执行 `service stop`。节点随后显示为 unreachable 且保持排空，不会再被派单。
3. 保留它的 node_id、执行证据和数据目录，不要删除节点记录或卸载。要重新启用：`service start`，再解除排空。

对已停用的节点运行安装器升级或回退会重新启用并启动它的服务，并恢复升级前的排空设置：只有升级前已排空的节点才不会被派单。要继续停用，之后再执行一次 `service stop`。

只有被排空节点才具备的能力不会被别的节点替代：仅 Linux 可满足的任务进入 `waiting_capability`，`status_reason` 列出各节点的拒绝原因
（如 `os_mismatch`、`gpu_missing`、`tool_missing:<名称>`）。macOS 的 POSIX shell 不等于 Linux，controller 主机也不是 worker。

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

`Persistent=true` 会补触发停机期间错过的日历任务，不补造每个历史时点的备份。若改变 `--backups-dir`，controller 的默认状态展示将读不到它；本工作包选择固定默认路径，未扩展 controller 配置 schema。保持安装布局即可自动展示。

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

在拥有已验证 Workbench 配置的 Mac 登录用户下执行。`CODE_DIR` 必须是本机已安装、包含本模块的代码绝对路径；示例默认用独立安装的 `current`，不要指向临时工作包 clone。`STATE_DIR` 是实例外持久目录，放在**内置盘**的用户目录下，实例 ID 用现场值替换。模板不含令牌，也不会复制 Workbench 登录资料；需确保该登录用户无需交互即可执行 Workbench。

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

LaunchAgent 依赖用户登录和 Workbench 当前可用；`StartInterval=21600` 不是未登录启动保证，不声称补齐休眠/离线期间每轮任务。日志只追加，可在停用时按实际容量归档；本工作包未增加日志轮转服务。Workbench exec 以 root 运行，但 list/confirm 命令均经 `runuser -u agent-collab`；download 读取的路径固定为严格验证目录内 `<id>/backup.tar`。exec 超时为 300 秒，download 为 3600 秒，超时记录失败并释放本地锁；远端 exec 超时后可能仍在完成短命令，确认重试保持幂等。Windows 主控可复跑单元测试，Linux/Mac 模板不能用于 Windows 服务注册。

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

## 受管协调者

controller `config.json`：

```json
{"managed_coordinator": {"enabled": true, "profile_id": "standby"},
 "standby_profiles": {"standby": {"harness": "claude", "model": "<用户选定模型>", "effort": "<用户选定强度>", "node_ids": ["<备用节点>"]}}}
```

未配置或字段不全时 UI 显示“profile 缺失”，系统不猜测模型或强度。协调权在 UI 工作包详情中取得、接管（须写原因，留事件）与释放。

## 旧任务

codex-collaborate 旧任务继续由原 launcher 管理；只读查看：`"$PREFIX/bin/agent-collab" legacy list --state-root <旧状态目录>`。
