# AGENTS.md

本文件写给在本仓库工作的编码代理（Codex、Claude Code 等），也是给人类贡献者的开发指南。链接的文档按需阅读；[踩线约束](#踩线约束)列出改动别处时最容易被无声破坏的约束，理由在对应文档里。

## 命令

```sh
python -m unittest discover -s tests                                             # 全量测试（标准库 unittest）
python -m unittest discover -s tests -p 'test_protocol.py'                       # 单个测试文件
python -m unittest discover -s tests -p 'test_controller_core.py' -k idempotency # 再按测试名筛选
cd ui && npm ci && node build.mjs && cd ..                                       # 类型检查并构建控制台到 agent_collab/static/
python -B scripts/build_release.py --version <版本>                               # 本地发行包，见 docs/release-checklist.md
```

CI（`.github/workflows/ci.yml`）在 Ubuntu、Windows、macOS 的 Python 3.13 和 `python:3.9-slim` 容器中跑全量测试，另构建一次 UI。仓库没有配置 lint、formatter 或 Python 类型检查器（UI 的 `tsc --noEmit` 在 `build.mjs` 里），不要顺手引入。

测试在仓库内 `.dev/tmp` 建临时目录并打开 SQLite。仓库位于 SMB/NFS 等网络盘时，先复制到本机磁盘再运行测试和 controller；`node build.mjs` 自己在本机临时目录构建，不受影响。部分用例按平台或环境跳过（仅 POSIX、仅 Windows、需要 Node、需要 loopback socket），报告结果时写明跳过了哪些。

## 改什么先读什么

| 要改的是… | 先读 |
| --- | --- |
| 端点、记录形状、错误码、fencing、幂等、调度原因码、契约字段 | [docs/protocol.md](docs/protocol.md) 与 `agent_collab/protocol.py` |
| controller 的认证、配置、协调租约、受管协调者、备份恢复、事件保留 | [docs/controller.md](docs/controller.md) |
| 节点 agent：领取、进程托管、重启对账、检查点、契约检查、回写、harness 适配器 | [docs/agent.md](docs/agent.md) |
| 安装器、服务模板、升级与回退、发行包 | [docs/install.md](docs/install.md) |
| 运维流程：配对、排空迁移、备份与实例外拉取、隔离恢复 | [docs/operations.md](docs/operations.md) |
| 技术栈、依赖、拓扑、开发隔离 | [docs/adr/](docs/adr/) |
| 许可证、第三方来源、发布前隐私复查 | [docs/release-checklist.md](docs/release-checklist.md)、[THIRD_PARTY.md](THIRD_PARTY.md) |

改运行时行为之前，先找到入口、权威实现和受影响的调用方；实现与文档契约不一致时，先弄清哪一边对再动手。协议变更在同一改动里依次改 `docs/protocol.md`、实现和测试。

读文档不等于要改文档。现有说明因本次改动变得不准确，或引入了不明显的新契约时，在同一改动里更新范围最窄的那份文档；恢复已写明的不变量、不改变行为的重构通常不用改文档。过时的说法直接更正或删除，不在文末追加增补段。

## 踩线约束

每条都是改动别处时容易无声破坏的约束。这里只概括不变量，端点细节与例外以箭头后链接的文档为准；两者不一致时先核对代码，再改错的一方。

- **零运行时依赖，兼容 Python 3.9。** `agent_collab/` 只用标准库，语法和 API 都要在 3.9 上可用（macOS 系统 Python 3.9.6 是真实节点）：不用 `match`、`zip(strict=)`、运行期求值的 `X | None`、`tomllib` 等。Node 只在构建 UI 时需要。→ [ADR 0001](docs/adr/0001-stack-and-boundaries.md)
- **业务逻辑在 `App.handle`。** `http.server` 包装只做 socket、TLS 和 SSE 推送；新端点必须能不开 socket、经 `App.handle` 测试。时间取注入的 `clock()`，节点网络走注入的 `transport`；测试用 `helpers.Clock.advance()` 推进时间，不真实等待。→ protocol §1
- **`domain.py` 只放纯函数。** 状态转换、调度过滤与评分、恢复决策、验收证据判定不访问数据库、文件、网络或时钟。→ protocol §6
- **fencing 优先。** attempt 的事件、检查点、完成与发布请求都校验 `(attempt_id, epoch == job.current_epoch, lease_token, authority_generation)`：旧 epoch 的事件按 `event_id` 去重存档为 `stale=true`，不改状态；检查点、完成与发布返回 409 `fenced`；fencing 先于返回历史幂等回执。只有一个例外：已有完成回执、且 fencing 原因只是 `revoked`（失败或取消的完成本身也会撤销 attempt）时，原样重放该完成请求返回保存的回执，内容不同仍是 409；旧 epoch、旧 generation、租约过期都不在例外之列。客户端在 `publish.mode=manual` 下发起的发布不带 `lease_token`，但仍检查 epoch 与 attempt 状态。新增的 attempt 写端点走同一校验。其他节点写端点不用 attempt 租约，各有自己的认证与校验：注册和心跳按节点令牌认证，心跳带 `authority_generation` 对账；回写结果（`POST /jobs/{id}/integration`）由目标节点认证，`checkpoint_id` 必须等于当前的 `delivery_checkpoint_id`，目标节点不必是执行节点。→ protocol §4 规则 2、7，§5、§11.10
- **幂等回执与状态同事务。** 客户端变更请求都带 `request_id`，相同内容重放原响应，不同内容 409。修改已有记录的请求（cancel、retry、assess、publish、integration、drain、dispatch）还要带 `expected_version`，过期返回 409 `version_mismatch`；创建 job 和获取协调租约没有已有行版本，只用 `request_id`。协调租约另有规则：释放（`DELETE`）凭 `X-Coordinator-Token`，不用 `request_id`，没有有效租约时幂等返回 `{}`；获取回执失效后，即使内容相同，重放也返回 409，不复活旧租约。权威状态、事件与幂等回执在同一个 SQLite 事务里提交；不要拆成多个事务，也不要在事务外补写事件。→ protocol §5、controller.md（数据和配置）
- **blob 齐全才能 durable。** blob 先写同盘临时文件、fsync、校验 SHA-256，再 `os.replace`；checkpoint 引用的 blob 全部存在且大小一致，才与 job 指针在同一事务里置为 `durable`。→ protocol §4 规则 4
- **交付只经发布 CAS。** `delivery_version` 只在 `expected_base_version == delivery_version` 时加 1；`requires_acceptance` 只认 `passed`；`version_mismatch` 不自动重试，交给协调者。→ protocol §4 规则 6
- **验收靠执行证据。** 契约有 `checks` 时，任何来源的 `passed` 都要核对节点执行的检查结果并绑定检查点 manifest，缺证据就降为 `unverified`；模型自述和 stub 步骤结果不能替代。→ protocol §11.7
- **契约受理后不可变。** 契约字段参与 `request_hash`。新增字段要决定是否参与哈希，且不能改变旧契约的哈希；升级时幂等补齐缺省字段，不改写历史契约、请求哈希、幂等回执或版本号。→ controller.md（rc5 检查、能力证据与回写待办）
- **数据库只在本机打开。** SQLite 只在 controller 所在主机的本机文件系统上打开，测试也一样，不经 SMB/NFS 打开。`serve` 持有 `controller.lock`，同一数据目录只运行一个服务；`backup`、`pairing-code`、`prune-events` 等本机命令可以与它并行，其中 `backup` 用 SQLite backup API 取一致快照；`restore` 要求服务已停止。直接构造 `App` 时，由调用方保证同一目录只有一个活动实例。→ [ADR 0001](docs/adr/0001-stack-and-boundaries.md)、controller.md（启动与身份）
- **不凭旧 PID 结束进程。** 节点重启后无法确认原进程身份时，保留 `restart_unknown` 并暂停领取，不根据记录的 PID 结束进程（PID 可能已被重用）。→ agent.md（执行、恢复与回写）
- **模型调用参数是安全边界。** Claude 协调者固定 `--tools ""`、`--permission-mode dontAsk`、每轮新会话，context 作为不可信数据放进提示词；提示词要求它只依据提供的证据判断、不声称没看到的检查，证据不足或没有检查点时选择 `escalate`，改提示词时保留这些要求。Codex 固定 `--ignore-user-config`、`read-only` 或 `workspace-write` 沙箱、`network_access=false`。改 `adapters/claude.py`、`adapters/codex.py` 的 argv 时保持这些，现有测试有断言。→ protocol §10、§11.5
- **控制端凭据只存哈希，节点报告不带令牌。** controller 数据库中的节点令牌、租约、UI 会话、CSRF、配对码、协调令牌只存 SHA-256；校验时比较哈希，并使用 `hmac.compare_digest`。明文凭据文件只有控制端的 `admin.token`，以及节点发起认证必需的 `node.token` 和 `attempts/<id>/lease.token`，POSIX 上均为 0600。节点上报的报告和日志尾部经 agent 的 `clean()` 去除已知令牌；`pairing-code` 打印配对码、获取协调租约时返回令牌属于有意发放凭据的输出，不在此列。新增凭据沿用同样做法。→ controller.md（启动与身份）、agent.md（agent.json 与目录）
- **回写不覆盖人工改动。** `artifacts.integrate()` 只检查本次结果改动的路径，冲突时返回 `conflict` 且不写目标；改写前先备份，再逐文件原子替换；结果未知时上报 `conflict`，不重放。→ agent.md（执行、恢复与回写）
- **兼容面有外部使用者。** `/api/v1` 端点与记录形状、错误码、`agent.json` 与 `config.json` 的键、CLI 与安装器参数、数据库 records、`release-manifest.json` 都有已部署的使用者，controller 与节点不一定同时升级。改动按破坏性变更处理：保持旧字段语义，写明迁移方式。
- **测试不碰真实模型和外部网络。** 测试用 stub harness、`tests/fake_controller.py` 或 `App.handle`；`tests/test_e2e_local.py` 在 loopback 上起本机 controller 是例外，socket 不可用时跳过。真实 Codex/Claude 回合消耗用户额度、依赖登录状态，只在明确要求时运行。`AGENT_COLLAB_FAULT_PARTITION`、`--fault-exit-after-checkpoint` 只用于测试与演练。→ agent.md（测试与故障注入）

## 缺陷、已知限制与部署加固

报告问题或“顺手修”之前，先分清三类：

1. **缺陷**：实现违反了文档写明的契约，例如旧 epoch 的发布被接受、节点令牌能调用客户端端点。应当修复。
2. **已知限制**：文档已经写明的边界，例如契约检查以节点服务身份运行、没有沙箱；stub 没有隔离；协调者的 `--tools ""` 不是操作系统级隔离；单控制服务、没有高可用；macOS 与 Windows 节点依赖用户登录，Linux 节点的 systemd 用户单元在未启用 linger 时也一样（安装器不自动启用）；没有 blob GC。不要当作新发现重复提出；要改变它属于设计变更，先在 issue 里讨论。见 README（保障与边界）与 agent.md（协议缺口与已知限制）。
3. **部署加固**：运维侧可以额外采取的措施，例如独立低权限账户、反向代理、网络访问限制。写进 install.md 或 operations.md，不当作代码缺陷。

## 约定

- **结论要有证据。** 写“已验证”“已通过”时附上实际命令和结果，没运行就写“未运行”。不根据配置、文档或元数据推断“不可用”或“已通过”：实际试一次，引用真实错误。这和本项目对模型输出的要求一样。
- **修 bug 改最窄的地方**，并附一个修复前失败、修复后通过的回归测试；做不到时说明原因。
- **在边界校验，内部信任已建立的契约。** 外部输入（HTTP 请求、配置文件、节点上报、模型输出）在负责它的边界校验与规范化；内部函数不重复同样的检查，也不把非法值悄悄换成空默认值。
- **抽象要挣得位置。** 优先直接的代码，不为假想的复用或没有具体依据的场景添加封装、配置或防御机制。但概率低不是忽略正确性或安全问题的理由。
- **不擅自添加依赖或工具**（包括测试框架、lint、formatter），先在 issue 或 PR 描述里讨论。
- **测试**沿用 `tests/helpers.py`（`ControllerCase`、`FakeNode`、`Clock`、`contract()`、`manifest()`）。能在进程内经 `App.handle` 测的不起子进程或 socket，只有依赖进程边界的行为（进程组、重启、HTTP framing、端到端）才起；平台相关用例用 `skipIf`/`skipUnless`。迭代时跑相关文件，提交前跑全量；不要假定失败原本就存在。
- **注释写为什么**，不复述代码。不要把对话、提示词、评审轮次或内部代号写进代码注释、文档和提交信息，读者没看过这些过程。
- **文档写当前行为**：支持的用法、可观察的行为、限制和例子。不把内部实现细节或修 bug 的历史当作公开契约；少写会过时的数量和手工清单；动笔前对照代码核实。
- **公开仓库不放部署信息。** 不提交真实域名、主机名、用户目录、会话 ID 或令牌，示例用 `controller.example`、`192.0.2.x` 这类保留地址。提交前按[发布检查清单](docs/release-checklist.md)复查暂存区。

### 提交信息

用中文写一行，说清实际改了什么；可加范围前缀，如 `测试：`、`文档：`、`UI：`。只在“为什么”不明显时才写正文：理由、放弃的方案、兼容性说明；正文段落写成连续的长行，不手工折行。

推荐：

```
UI 构建在外层 finally 中释放锁，临时目录创建失败或清理出错时也删除锁；补对应测试
测试：同一时刻、相同 TTL 接管协调权时 expires_at 相同而 version 递增
```

不要写空泛的标题或评审、代理行话：

```
修复若干问题   ❌
处理评审意见   ❌
第二轮加固     ❌
```

### Pull request

- 标题按提交信息的规则写。
- 描述写清改了什么、为什么、关联的 issue，以及跑了哪些检查：哪些平台、结果、跳过项。bug 修复附回归测试，或说明为什么无法提供。
- 按改动的行为补证据：UI 改动附截图；涉及恢复、fencing、回写的改动说明覆盖了哪些故障场景；写明未测试的平台和仍需人工验收的项。纯文档改动只需核对链接以及与代码是否一致。
- 用 `gh` 写 PR 或 issue 正文时，先写进文件再用 `--body-file <路径>` 传入，不用内联 heredoc，否则反引号转义会被破坏。

## 边界

- **先讨论再动手**：新增依赖或工具；改线上协议、数据库记录形状或备份格式；改安装器的服务形态；跨模块的大重构。
- **绝不**：提交凭据、令牌或真实部署信息；为了让测试通过而放宽 fencing、幂等、发布 CAS 或证据门禁；在生产环境开启故障注入开关；未经要求执行破坏性 git 操作（强推、改写已推送的历史）。
