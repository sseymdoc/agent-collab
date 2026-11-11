# 给 Claude 与其他 Agent 的集成说明

自动化客户端用与桌面端相同的 API 和授权提交工作、观察进展、读取检查与成果。入口是随同一版本分发的 `agent-collab` 命令：命令结果（包括被 controller 拒绝时的应答）以 JSON 写到标准输出，连接与凭据错误以 `{"error": {...}}` 写到标准错误；本地参数错误（缺少凭据文件、参数格式不对等）是标准错误上的一行文本，按退出码判断即可。协议细节见 `docs/protocol.md` §12。

## 一次性准备

拥有者在 controller 所在机器上签发配对码（一次性，10 分钟内有效）。日常自动化建议用 `operator`（读取 + 任务）：

```bash
agent-collab controller client-code --data-dir <controller 数据目录> --role operator
```

在运行自动化的机器上换取凭据：

```bash
agent-collab client pair --url https://<controller 地址> --code <配对码> --name "<这个客户端的名字>" --kind agent
```

凭据写入当前用户配置目录下仅本人可读写的文件（POSIX 为 0600；Windows 上 DACL 只留当前用户；这一步在发出配对请求之前完成，做不到时命令直接退出，配对码没有使用），之后的命令自动使用它；多个 controller 用 `--profile <名字>` 区分。凭据绑定这个 controller 的身份和配对时的地址：命令不会把它发往 `--url` 指定的其他地址（地址变了就重新配对），应答来自别的 controller 时命令以退出码 3 结束。拥有者可以随时在桌面端“设置 → 客户端”里撤销它。

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
- `--request-id <UUID>` 指定幂等键：提交因网络问题没有拿到应答时，用同一个值重试不会重复创建任务。

成功时输出 `{"http_status": 201, "body": {...}}`，任务标识是 `body.job_id`。提交现成的契约文件用 `agent-collab submit 任务.json`。

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

## 停止

```bash
agent-collab job cancel <job_id>
```

取消是请求：摘要里的 `cancel.requested_at` 与 `cancel.confirmed_at` 分别是请求时间和执行设备确认停止的时间。

## 约定

- 每次运行命令都会生成新的幂等键。`job cancel`、`job retry`、`job publish`、`settings set` 和带 `expected_version` 的 `api` 请求重试是安全的：已经生效时重试只会得到 `version_mismatch`（409），表示记录已被改动，重新读取后再决定。`submit` 重试前要么带上同一个 `--request-id`（任务文件里写 `request_id`、`api` 的 `--data` 里带 `request_id` 同理），要么先用 `job list` 确认上一次是否已受理；`baseline add` 重试可能多登记一份同内容的基线。
- `job wait` 与 `job follow` 在 controller 暂时不可达（重启、前置代理返回 5xx）时继续等待到超时；其余命令遇到这种情况以退出码 3 结束。
- `client forget --revoke` 用这份客户端凭据自己撤销自己；撤销没有成功时保留凭据文件并以退出码 2 结束。
- 退出码：0 成功；1 本地错误（缺少凭据文件、地址与配对时不符等，标准错误上是一行文本）；2 请求被拒绝或命令参数格式错误；3 连接、凭据（HTTP 401）或 controller 身份问题；4 等待超时；5 任务以失败、取消或需人工处理结束。`api` 与提交任务文件也按此区分 401 与其他拒绝。
- 只经这些命令和 API 与系统交互：不读写 controller 的数据库，不在执行设备上自行启动模型进程，不用管理令牌做日常提交。
