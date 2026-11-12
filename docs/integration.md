# 给 Claude 与其他 Agent 的集成说明

自动化客户端用与桌面端相同的 API 和授权提交工作、观察进展、读取检查与成果。入口是随同一版本分发的 `agent-collab` 命令：命令结果（包括被 controller 拒绝时的应答）以 JSON 写到标准输出，连接与凭据错误以 `{"error": {...}}` 写到标准错误；本地参数错误（缺少凭据文件、参数格式不对等）是标准错误上的一行文本，按退出码判断即可。协议细节见 `docs/protocol.md` §12、§13。

## 一次性准备

拥有者签发配对码（一次性，10 分钟内有效）：在桌面端“设置 → 客户端”里签发，或在 controller 所在机器上运行下面的命令。日常自动化建议用 `operator`（读取 + 任务），不要给它用不到的权限：

```bash
agent-collab controller client-code --data-dir <controller 数据目录> --role operator
```

在运行自动化的机器上换取凭据：

```bash
agent-collab client pair --url https://<controller 地址> --code <配对码> --name "<这个客户端的名字>" --kind agent
```

凭据放在哪里由 `--credential-store` 决定，配对后写进 profile，之后的命令按它读取：

| 后端 | 存放位置 | 适用 |
| --- | --- | --- |
| `system`（缺省） | 系统凭据存储：Windows 凭据管理器、macOS 登录钥匙串、Linux Secret Service（`secret-tool`） | 有图形登录会话的机器 |
| `file` | 当前用户配置目录下的 profile 文件，只有本人可访问（POSIX 为 0600，Windows 的访问控制列表只留当前用户） | 没有图形会话的部署（经 SSH 运行的自动化、无人值守的服务器） |

- 两个后端之间没有自动回退。系统凭据存储在当前会话里不可用时（例如经 SSH 登录的 macOS 读不到登录钥匙串），`client pair` 在发出配对请求之前就报错退出，配对码没有使用；这时显式加上 `--credential-store file`，或设置环境变量 `AGENT_COLLAB_CREDENTIAL_STORE=file`。
- 文件后端每次读取凭据前核对文件此刻的实际访问权限（POSIX 看属主与权限位，Windows 读回访问控制列表）；有当前用户以外的主体能访问时拒绝使用并说明原因。
- 命令输出凭据的存放位置（`credential_store`、`credential_location`），不输出凭据值。
- 这份凭据只有配对码授予的权限，可以单独撤销：拥有者在桌面端“设置 → 客户端”里撤销，或由它自己 `agent-collab client forget --revoke`。

多个 controller 用 `--profile <名字>` 区分。凭据绑定这个 controller 的身份和配对时的地址：命令不会把它发往 `--url` 指定的其他地址（地址变了就重新配对），应答来自别的 controller 时命令以退出码 3 结束。

## 提交

用模板和目标提交，输入取自一个目录的当前内容：

```bash
agent-collab submit --template codex-change-accept \
  --goal "修复 calc.py 里 add 的错误，并新增 mul(a, b)" \
  --acceptance "add(2, 3) 返回 5" --acceptance "mul(4, 5) 返回 20" \
  --exclude "不修改 test_calc.py" \
  --input-dir ./project \
  --check 'unit={python} -m unittest -q'
```

- 模板：`codex-change`（修改并检查，结果自动发布）、`codex-change-accept`（执行并验收，验收通过后由调用方发布）、`diagnostic`（不调用模型的链路自检）。
- `--check 名称=命令` 可重复；命令不经过 shell，`{python}` 换成执行设备上的解释器。检查由执行设备运行并留下证据，验收“通过”要求它们全部通过。
- `--model`、`--effort` 覆盖工作区默认值；不给则用默认值。不被部署许可的组合会被拒绝，不会换成别的模型。
- `--os windows|macos|linux` 限定执行平台。`--baseline <ID>` 使用已登记的输入基线代替 `--input-dir`。
- `--dry-run` 只输出将提交的契约和解析后的模型，不创建任务。
- `--project <项目 ID>` 在项目里提交：用项目的默认值与它最近登记的输入，见下文“项目”。

不带 `--wait` 时，成功输出 `{"http_status": 201, "body": {...}, "request_id": "..."}`，任务标识是 `body.job_id`；带 `--wait` 时等任务有结论后输出等待摘要（与 `job wait` 相同，含 `job_id`、`outcome` 等，不含 `request_id`）。提交现成的契约文件用 `agent-collab submit 任务.json`。

### 提交没有得到答复时

每次提交在得到 controller 的明确答复之前，完整的请求体（含它的 `request_id`）作为回执保存在本机配置目录的 `requests/` 下。连接中断、controller 暂时不可达或进程被打断时，这次提交是否已被受理是未知的；命令以退出码 3 结束，并在标准错误的 JSON 里给出 `request_id` 与恢复命令：

```bash
agent-collab submit --resume <request_id>     # 原样重放这一次提交
agent-collab submit --pending                 # 列出还没有明确答复的提交
```

- `--resume` 可以在之后的任何进程里运行。它重放保存下来的请求体，不重新草拟：controller 已经受理过时返回原来的任务（`http_status` 200、`replayed: true`），包括它受理时冻结的模型策略，即使默认模型此后已经改变；之前没有受理过时这一次才受理（201）。两种情况都不会出现第二个任务。
- 重放照常经过凭据与权限检查；凭据已被撤销时得到 401，回执保留。
- 请求当初根本没有到达、而工作区设置或项目默认值此后已经改变时，重放被 controller 以 `version_mismatch` 拒绝（它不会按没看过的默认值受理），回执随之清除；按当前设置重新提交即可。
- 得到 2xx 或明确的拒绝后回执删除。不带 `--resume` 的提交总是新的 `request_id`、新的任务：重跑整条 `submit` 命令是再提交一次，不是恢复上一次。带着上一次的 `--request-id` 重跑整条命令会被本机拒绝并提示用 `--resume`。

## 等待与读取结果

```bash
agent-collab job wait <job_id> --timeout 3600     # 等到停稳，输出摘要
agent-collab job follow <job_id>                  # 每行一个事件，最后一行是 {"summary": ...}
agent-collab job results <job_id>                 # 只读取当前摘要
```

受管协调者要求返工时，controller 会在同一根工作包里新建修复任务；`wait` 与 `follow` 自动跟到最新一版，摘要里的 `job_id` 是最新修订。

摘要字段：`status`（运行状态）、`checks`（各项检查的退出码，来自执行设备的证据）、`acceptance` 与 `assessments`（验收结论、方法与结果）、`coordination`（各协调回合的结论与所在设备）、`model_policy` 与 `observed`（请求、解析后的模型，以及 CLI 实际观测值）、`delivery_version`、`outcome`。

| `outcome` | 含义 | 下一步 | `wait` 退出码 |
| --- | --- | --- | --- |
| `done` | 成果已发布 | 取成果 | 0 |
| `ready_to_publish` | 执行与验收已满足发布条件，等调用方发布 | `job publish` | 0 |
| `awaiting_acceptance` | 契约要求验收，尚无“通过”的验收 | 自行验收（见下），或交给拥有者 | 0 |
| `needs_attention` | 失败、已取消、待对账，或验收为返工/受阻，且没有进行中的协调回合 | 读 `status_reason`、`assessments`，决定重试或上报 | 5 |
| `running`、`awaiting_coordination`、`awaiting_publication` | 仍在执行、协调或等待执行设备发布 | 继续等待 | 超时为 4 |

运行状态、检查、验收、发布是分开的事实：`status: completed` 只表示执行结束，不表示检查通过或已验收。

## 取成果与发布

```bash
agent-collab job artifacts <job_id>                    # 文件清单
agent-collab job artifacts <job_id> --download ./out   # 写入空目录，逐个校验哈希
agent-collab job publish <job_id>                      # outcome 为 ready_to_publish 时
```

## 项目

项目把一组任务、它们的默认策略和输入归在一起；项目在每台机器上对应哪个目录只保存在那台机器上。

```bash
agent-collab project create --name "<名称>" [--model M --effort E] [--exclude '<glob>' ...]
agent-collab project bind <项目 ID> <目录>          # 这台机器上项目对应的目录
agent-collab project scan --project <项目 ID>       # 将上传的范围：文件数、大小、被排除与被跳过的条目；不上传
agent-collab project push <项目 ID>                 # 扫描、上传缺的内容、登记为项目的输入
agent-collab submit --project <项目 ID> --template codex-change --goal "<目标>" --wait
agent-collab project plan <job_id>                  # 成果相对本机目录的变更与冲突；不写入
agent-collab project apply <job_id> [--skip-conflicts]
agent-collab project recover [<application_id>] [--resume | --rollback | --abandon | --report]   # --report：本机已写完、上报没得到答复时按原 request_id 补报
```

- 默认值的优先级：任务上的 `--model`、`--effort` > 项目默认值 > 工作区默认值。草拟之后项目默认值或工作区设置变了，提交被拒绝（`version_mismatch`），重新运行即可。
- `push` 不跟随符号链接，缺省排除版本库元数据、依赖与构建缓存、常见凭据文件；可以重复运行，已上传的内容不再上传，内容没变不新建输入。扫描期间目录一直在变化时失败并列出变化的路径；扫描之后文件被改动时不登记输入。提供输入的机器不必是执行设备，上传完成后可以离线。
- `apply` 只应用已发布的成果，按任务的输入基线、成果与目录当前内容逐文件比较：只动成果改过的路径；任务期间本机改过的文件算冲突，保留本机内容。有冲突时缺省什么都不写（退出码 5，`status: conflict`）；`--skip-conflicts` 只写没有冲突的文件，结果是 `partial`。只有 `status: applied` 表示全部写入，controller 才把任务记为已回写。
- 应用逐文件进行，被替换的原文件先备份；它不是跨文件的原子操作。应用被打断后，`project recover` 列出未完成的操作，可以续写、回滚，或按现状结束（记为结果未知）。
- 整包下载到别的目录用 `agent-collab job artifacts <job_id> --download <空目录>`；它与应用到项目是两件事，各自登记。

## 自己做验收或协调

要以交互协调者的身份处理某个工作包，先取得它的协调租约，之后的验收、发布、重试带上返回的令牌；同一根工作包同时只有一个协调者，受管协调者在租约期间不会介入。

```bash
agent-collab api POST /packages/<work_package_id>/coordinator \
  --data '{"holder_kind": "interactive", "holder_ref": "<名字>", "ttl_s": 1800, "takeover": false, "reason": ""}'
agent-collab api POST /jobs/<job_id>/assess --coordinator-token <coordinator_token> \
  --data '{"expected_version": <job.version>, "assessment_id": "<新 UUID>", "checkpoint_id": "<final_checkpoint_id>",
           "status": "passed|rework|blocked", "method": "<实际做了什么检查>", "result": "<结果与覆盖范围>", "reviewer": "<名字>"}'
agent-collab api DELETE /packages/<work_package_id>/coordinator --coordinator-token <coordinator_token>
```

只做了局部检查时不要提交整体“通过”，在 `result` 里写明覆盖范围。契约带检查时，没有执行证据的“通过”会被记为未验收。

## 读取原始输出

`job follow` 与任务事件是 controller 记录的状态变化和节点上报的进度；摘要里的检查结果只有退出码和输出尾部。harness 与检查进程的完整输出保存在执行设备上，按需分块取回：

```bash
agent-collab api POST /jobs/<job_id>/logs --data '{"attempt_id": "<attempt_id>"}'                 # 列出可取的输出
agent-collab api POST /jobs/<job_id>/logs --data '{"attempt_id": "<attempt_id>", "stream": "harness.stdout", "offset": 0, "max_bytes": 65536}'
agent-collab api GET /operations/<op_id>                                                          # 读取结果；next_offset 用于下一块
```

操作由执行设备在下一次心跳领取，通常几秒内完成；设备不在线时到期记为 `timed_out`。`retained: false` 表示设备上已没有这次执行的本地文件。

## 停止

```bash
agent-collab job cancel <job_id>
```

取消是请求：摘要里的 `cancel.requested_at` 与 `cancel.confirmed_at` 分别是请求时间和执行设备确认停止的时间。

## 约定

- 每次运行命令都会生成新的幂等键。`job cancel`、`job retry`、`job publish`、`settings set` 和带 `expected_version` 的 `api` 请求重试是安全的：已经生效时重试只会得到 `version_mismatch`（409），表示记录已被改动，重新读取后再决定。`submit` 没有得到答复时用 `submit --resume <request_id>` 恢复（见上文），不要重跑整条命令。`api POST /jobs` 要自己在 `--data` 里带固定的 `request_id` 才能安全重发。`baseline add` 重试可能多登记一份同内容的基线；`project push` 不会。
- 401 与 403 不同：401 表示这份凭据已失效或被撤销（退出码 3），需要重新配对；403 表示凭据有效但没有这项操作的权限（退出码 2，应答的 `details.required_scope` 给出所需权限），凭据不会因此被清除，其余已授权的操作照常可用。
- `job wait` 与 `job follow` 在 controller 暂时不可达（重启、前置代理返回 5xx）时继续等待到超时；其余命令遇到这种情况以退出码 3 结束。
- `client forget --revoke` 用这份客户端凭据自己撤销自己；撤销没有成功时保留凭据文件并以退出码 2 结束。
- 退出码：0 成功；1 本地错误（缺少凭据、凭据存储不可用、地址与配对时不符等，标准错误上是一行文本）；2 请求被拒绝或命令参数格式错误；3 连接、凭据（HTTP 401）或 controller 身份问题；4 等待超时；5 任务以失败、取消或需人工处理结束，或成果没有完整应用到项目。`api` 与提交任务文件也按此区分 401 与其他拒绝。
- 只经这些命令和 API 与系统交互：不读写 controller 的数据库，不在执行设备上自行启动模型进程，不用管理令牌做日常提交。
