# agent-collab

**Claude–Codex 协作调度器**：把开发工作包交给多台机器上的 Codex / Claude 执行，按能力调度、靠检查点恢复，在一台主机失效后由另一台接着完成修复与验收。

*A small, dependency-free scheduler that runs Claude Code and Codex CLI work packages across your own machines, with checkpoint recovery, fenced publishing and evidence-gated acceptance.*

> 状态：候选版本 `0.1.0rc7`。下文“已验证”与“未验证”分开写；不要把它当作生产级高可用系统。

## 它解决什么问题

一个人在 Windows、WSL、Mac 等几台机器上同时使用 Claude Code 和 Codex 时，常见的问题是：任务跑在哪台机器、机器掉线后工作丢了没有、模型说“测试通过”是否可信、谁有权决定返工。agent-collab 用一个权威控制服务和每台机器上的节点 agent 回答这些问题：

- **受理与调度**：任务契约（目标、验收项、排除项、harness、资源与预算）受理后不可变；按操作系统、CPU 架构、工具、GPU、认证状态、主机槽位等硬条件过滤，再做可解释的评分。
- **检查点恢复**：节点在静止边界提交内容寻址的检查点；节点失联后，从最近一个已确认的检查点在另一台合格机器上新建 attempt（epoch +1），旧 attempt 的迟到发布被 fencing 拒绝。
- **受管协调**：任务结束或需要对账时，可由幸存节点上的 Claude Code 读取上下文、给出结构化决定（accept / rework / escalate），必要时派生 Codex 修复任务；协调权按工作包独占，迟到或重复的决定不会重复生效。
- **证据门禁**：契约可声明必需检查（如单元测试命令），由节点在 harness 完成后亲自执行并记录退出码、平台和输出尾部，绑定到检查点 manifest。有人请求 `passed` 验收时（无论来自模型还是管理员）才核对这些证据：缺少或失败就只记为 `unverified` 并列出缺口；没有验收请求时不会生成验收记录，结果要看 attempt 报告。要让未通过验收的结果不能发布，契约需设 `publish.requires_acceptance=true`；默认值为 `false`：检查结果照样由节点写入 attempt 报告（`attempts[].report.checks.results`），只是发布不受它阻止。
- **源目录回写**：交付保存在控制服务；回写到源目录由目标节点按基线检查执行，源机暂时不可达时显示为“待回写”，冲突时不覆盖。
- **运维**：安装器（systemd、LaunchAgent、Windows 计划任务）、不可变版本目录、升级/回退、定时一致备份与实例外拉取、Web 控制台。

## 架构

```text
        ┌──────────────────────────── 控制服务（单实例，SQLite WAL + 内容寻址 blob）────────────┐
        │  HTTP API · SSE 事件流 · 调度与恢复决策 · 协调租约 · 发布 CAS · 验收证据门禁 · Web UI │
        └───────────▲───────────────────────▲───────────────────────────▲────────────────────────┘
                    │ HTTPS（节点主动出站）  │                           │
           ┌────────┴───────┐      ┌───────┴────────┐          ┌───────┴────────┐
           │ 节点 agent      │      │ 节点 agent      │          │ 节点 agent      │
           │ Windows 原生    │      │ WSL / Linux     │          │ macOS           │
           │ codex · claude  │      │ codex           │          │ codex · claude  │
           └────────────────┘      └────────────────┘          └────────────────┘
```

- **零运行时依赖**：控制服务、节点 agent 与适配器只用 Python 标准库，兼容 Python 3.9+。
- **节点只出站**：不需要在工作机上开放端口；控制服务建议放在 loopback 后面，由反向代理（如 Caddy）终止 TLS。
- **模型工具来自你本机**：节点调用你自己安装并登录的 Codex CLI 与 Claude Code；本项目不分发、不代理、不复制它们的认证。

## 目录

| 路径 | 内容 |
| --- | --- |
| `agent_collab/protocol.py` | 共享常量、manifest 规范化与路径校验 |
| `agent_collab/domain.py` | 状态转换、调度过滤/评分、恢复决策、验收证据判定（纯函数） |
| `agent_collab/store.py`、`blobstore.py` | SQLite 权威状态与内容寻址 blob |
| `agent_collab/controller.py` | HTTP API、SSE、认证、协调者、发布与回写 |
| `agent_collab/node_agent.py` | 节点 agent：注册、心跳、领取、进程托管、检查点、契约检查、回写 |
| `agent_collab/adapters/` | stub（确定性测试）、codex、claude 适配器与命令解析 |
| `agent_collab/artifacts.py` | 快照、物化、回写 |
| `agent_collab/backup.py` | 互斥原子的本地定时备份与实例外拉取 |
| `agent_collab/installer.py`、`installer/` | 安装、升级、回退、卸载与服务模板 |
| `ui/` | React + TypeScript 控制台源码，构建到 `agent_collab/static/` |
| `docs/` | 协议契约、组件说明、安装与运维、架构决定 |
| `tests/` | 契约、恢复、并发、故障注入与本机多进程端到端测试 |

## 快速试用（本机 loopback）

需要 Python 3.9+。以下命令在源码根目录执行，只使用确定性的 `stub` harness，不调用任何模型：

```sh
# 1. 启动控制服务（只监听 127.0.0.1；首次启动在数据目录生成 admin.token）
python -m agent_collab.controller serve --data-dir ./.dev/ctl --bind 127.0.0.1:18669

# 2. 另一个终端：为节点生成一次性配对码，然后配对并运行节点
python -m agent_collab.controller pairing-code --data-dir ./.dev/ctl --node-id dev-node
python -m agent_collab.node_agent pair --controller http://127.0.0.1:18669 --code <配对码> \
  --node-id dev-node --data-dir ./.dev/node
python -m agent_collab.node_agent run --data-dir ./.dev/node --foreground
```

提交一个 stub 任务：在工作区写一个文件，再由节点执行一项检查。把下面的内容保存为 `job.json`：

```json
{"title": "stub 试用",
 "contract": {
   "goal": "在工作区写一个文件，并由节点检查它",
   "harness": "stub",
   "harness_params": {"steps": [{"write": {"path": "hello.txt", "content": "hello\n"}}]},
   "checks": [{"name": "hello", "run": ["{python}", "-c", "assert open('hello.txt').read() == 'hello\\n'"]}],
   "recovery": {"auto": false},
   "coordination": {"policy": "none"}}}
```

`recovery.auto=false` 表示不做自动恢复，因此不要求提供输入基线（`inputs_manifest`）。本例没有提供基线，任务受理后可以直接派单。`coordination.policy=none` 表示结束后不调用 Claude 协调者。保持前两个终端中的 controller 和节点继续运行，在第三个终端提交并查看结果：

```sh
python -m agent_collab.cli submit job.json --token-file ./.dev/ctl/admin.token
python -m agent_collab.cli api GET /jobs/<job_id> --token-file ./.dev/ctl/admin.token
```

几秒后 `status` 变为 `completed`，`delivery_version` 变为 1；`attempts[-1].report.checks.results` 中 `hello` 检查的 `exit_code` 为 0。也可以在浏览器打开 `http://127.0.0.1:18669/`，用 `./.dev/ctl/admin.token` 中的令牌登录查看（需先构建 UI，见下）。契约字段与全部 stub 步骤见 [docs/protocol.md](docs/protocol.md)。

构建 Web 控制台（仅构建期需要 Node）：

```sh
cd ui && npm ci && node build.mjs   # 产物写入 agent_collab/static/
```

## 正式安装

用 `scripts/build_release.py` 构建带逐文件 SHA-256 manifest 的本地发行包，再用 `installer/install.sh` 或 `installer/install.ps1` 安装：

| 角色 | 服务形态 |
| --- | --- |
| controller（Linux） | systemd system unit，独立低权限用户，只监听 loopback |
| worker（Linux/WSL） | systemd 用户单元（建议开启 linger） |
| worker（macOS） | LaunchAgent（模型登录在钥匙串中，需要图形登录会话） |
| worker（Windows） | 当前用户登录触发的计划任务（pythonw，5 分钟重复触发以拉起退出的进程） |

在 controller 所在主机上升级时，先对控制服务做一次本地一致备份；只装 worker 的主机升级时不备份远端 controller。升级后核对新进程确实来自新版本目录并已成功心跳，否则自动回到旧版本。回退前会重新检查旧版本能否支持当前数据库 schema，不支持时拒绝回退，并保留指针、记录与备份，由运维核对后处理。完整参数、回退与卸载见 [docs/install.md](docs/install.md)，日常运维见 [docs/operations.md](docs/operations.md)。

## 保障与边界

**会做到的：**

- 只对已受理、输入已复制到控制服务、存在已确认检查点或完整初始基线的任务做自动接管；旧 epoch 的检查点与发布会被拒绝（409 `fenced`）；旧 epoch 的事件仍会去重存档，但标记 `stale=true`，不改变任何状态，事件消费者应据此区分迟到事件。
- 修复任务继承根工作包的累计预算；取消或预算耗尽后不再派生修复或恢复。
- Codex 以 `workspace-write` 沙箱、关闭网络、`--ignore-user-config`（不加载你的 MCP 服务与钩子）运行，写权限限于 attempt 工作区。

**不会做到的：**

- 不迁移活进程，不恢复模型内部推理状态，不承诺任意命令 exactly-once。
- 控制服务是单实例：它不可用期间不派新单、不发布，节点不会自行另起主控（没有控制面高可用）。
- Claude 协调者不启用任何工具（`--tools ''`），只根据控制服务在提示中提供的上下文给出决定，不能自行读取文件；这是 Claude Code 自身的命令行限制，**不是操作系统级隔离**；stub harness 仅用于测试，没有隔离。
- macOS 和 Windows 的节点服务依赖用户登录（macOS 钥匙串、Windows 登录触发），开机后无人登录时不会运行。Linux worker 的 systemd 用户单元默认也随用户会话启停；启用 linger 后可在无人登录时运行，但安装器不会自动启用。WSL 还受 Windows 宿主与 WSL 生命周期限制。

## 验证情况（0.1.0rc6 / rc7）

- **自动化测试**：Windows（Python 3.15）、WSL（3.14）、macOS（系统 3.9.6）全量运行无失败。部分用例按平台条件跳过（如仅适用于 POSIX、或需要 Node 的用例），例如公开候选在 Windows 上 393 项中 382 项通过、11 项跳过。测试含本机多进程 HTTP 端到端场景（崩溃承接、分区下旧 epoch 迟到发布被拒）。CI 覆盖 Ubuntu（3.9、3.13）、Windows、macOS。
- **真实环境演练**（云主机控制服务 + 两台物理主机上的三个常驻节点）：
  - 两个方向的完整续作：一侧节点失效 → 另一侧从检查点接管 → Claude 判定 rework → Codex 修复 → 节点执行检查 → Claude accept，验收绑定执行证据；
  - 源机不可达时工件照常保存、回写待处理，恢复后按基线回写，人工改动判冲突不覆盖；
  - 三种服务形态的 worker 升级 → 回退 → 再升级；一份实例外备份的隔离恢复中旧令牌不能复活；
  - 证据门禁负例：契约为 `publish.mode=manual`、`requires_acceptance=true`；worker 自称测试通过而检查实际失败时，`passed` 请求被降为 `unverified`，发布被拒绝。默认 `requires_acceptance=false` 时不会阻止发布。
- **未验证**：真实重启/注销/断网后的自动恢复、开机无人登录、公网控制台的完整浏览器流程、长期运行。

## 开发

```sh
python -m unittest discover -s tests
```

测试只用标准库；临时目录放在仓库内 `.dev/tmp`。Windows 上若仓库位于 SMB 映射盘，请复制到本机磁盘再运行测试（SQLite 不能经网络盘打开）；`node build.mjs` 会自行在本机临时目录构建，可以直接在映射盘上运行。

协议变更先改 [docs/protocol.md](docs/protocol.md)，再改实现与测试。

## 许可证

[MIT](LICENSE)。第三方来源见 [THIRD_PARTY.md](THIRD_PARTY.md)。Codex CLI、Claude Code 及相关模型服务遵循其各自的条款，不随本项目分发。
