//! 本机部署：发现已有安装、安装或升级、服务启停、把桌面端配对到本机 controller（`docs/desktop.md`“本机部署”）。
//!
//! 都通过本机助手运行安装器（`install`、`service`）与 `controller client-code`。桌面端只管理它自己这个实例的
//! 安装：实例名与本机 controller 的端口在构建时定下，页面不能指定安装位置。

use std::ffi::{OsStr, OsString};
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde_json::{json, Value};
use tauri::{AppHandle, Manager, State};

use super::helper::{self, Captured, Helper};
use super::{bundle, data_dir, device_name, err, helper_unavailable, invalid, io, pair, validate, BridgeError, Result, Shell};

/// 按实例区分的名字：安装目录与系统凭据存储的服务名都用它，两边不会对不上。
pub fn instance_name(instance: &str) -> String {
    if instance.is_empty() {
        "agent-collab".to_string()
    } else {
        format!("agent-collab-{instance}")
    }
}

/// 本机部署的实例名：正式版为空串；开发候选版与内部测试版在构建时给出（环境变量 `AGENT_COLLAB_INSTANCE`），
/// 安装到独立的前缀与状态目录，服务名带实例后缀，与同一台机器上的正式安装互不触碰。
pub const INSTANCE: &str = match option_env!("AGENT_COLLAB_INSTANCE") {
    Some(name) => name,
    None => "",
};
/// 本机 controller 监听的 loopback 端口（构建时的 `AGENT_COLLAB_LOCAL_PORT`）；并存的实例要用不同的端口。
const LOCAL_PORT: &str = match option_env!("AGENT_COLLAB_LOCAL_PORT") {
    Some(port) => port,
    None => "18669",
};

const DISCOVER_TIMEOUT: Duration = Duration::from_secs(60);
/// 安装包括解包运行时、注册并启动服务、等待健康检查。
const SETUP_TIMEOUT: Duration = Duration::from_secs(600);
/// 安装器自己等服务就绪最多 60 秒，停止时还要等数据目录的锁释放。
const SERVICE_TIMEOUT: Duration = Duration::from_secs(180);

/// 这个构建管理的那一份本机部署：家目录、实例名与端口决定它的前缀、状态目录和 controller 地址。
struct Deployment<'a> {
    home: &'a Path,
    instance: &'a str,
    port: &'a str,
}

/// `local_setup` 的参数；空字符串按没给处理。
struct Setup<'a> {
    role: &'a str,
    controller_url: Option<&'a str>,
    pairing_code: Option<&'a str>,
    node_id: Option<&'a str>,
}

fn args<const N: usize>(items: [&str; N]) -> Vec<OsString> {
    items.map(OsString::from).into()
}

impl Deployment<'_> {
    /// 构建时给的实例名与端口是否可用：实例名与安装器的规则相同（1–31 个小写字母、数字或连字符），端口是 1–65535。
    fn check(&self) -> std::result::Result<(), String> {
        let name = self.instance.as_bytes();
        let named = name.is_empty()
            || (name.len() <= 31 && name[0] != b'-' && name.iter().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'-'));
        if !named {
            return Err("这个构建的实例名无效".into());
        }
        if !self.port.bytes().all(|b| b.is_ascii_digit()) || !self.port.parse::<u16>().is_ok_and(|port| port != 0) {
            return Err("这个构建的本机 controller 端口无效".into());
        }
        Ok(())
    }

    fn directory(&self, kind: &str) -> PathBuf {
        self.home.join(".local").join(kind).join(instance_name(self.instance))
    }

    /// 安装前缀（代码）。实例为空时与安装器的缺省值相同。
    fn prefix(&self) -> PathBuf {
        self.directory("share")
    }

    /// 状态目录（身份与数据）。
    fn state_dir(&self) -> PathBuf {
        self.directory("state")
    }

    fn controller_url(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }

    /// 指明是哪一份安装的参数。实例为空时不传，由安装器取缺省值：正式版因此与命令行安装的是同一份。
    fn location(&self) -> Vec<OsString> {
        if self.instance.is_empty() {
            return Vec::new();
        }
        vec!["--prefix".into(), self.prefix().into(), "--state-dir".into(), self.state_dir().into(), "--instance".into(), self.instance.into()]
    }

    /// `install --discover`：只读。
    fn discover(&self) -> Vec<OsString> {
        let mut command = args(["install", "--discover"]);
        command.extend(self.location());
        command
    }

    /// `install --release ... --runtime ... --role ...`：用随包的发行包与运行时安装或升级。
    /// 页面给的值都以 `--选项=值` 的形式传入，不会被安装器当成别的选项。
    fn setup(&self, release: &Path, runtime: &Path, request: &Setup) -> std::result::Result<Vec<OsString>, String> {
        self.check()?;
        let hosts_controller = match request.role {
            "all-in-one" | "controller" => true,
            "worker" => false,
            _ => return Err("role 只能是 all-in-one、worker 或 controller".into()),
        };
        let mut command = args(["install", "--release"]);
        command.extend([release.into(), "--runtime".into(), runtime.into(), "--role".into(), OsString::from(request.role)]);
        if hosts_controller {
            // 本机 controller 只监听 loopback，节点与桌面端都经这个地址连接它；页面给的地址对这两种角色没有意义。
            command.extend(["--bind".into(), OsString::from(format!("127.0.0.1:{}", self.port)), "--controller-url".into(), self.controller_url().into()]);
        } else if let Some(url) = request.controller_url {
            command.push(format!("--controller-url={}", validate::base_url(url)?).into());
        }
        if let Some(code) = request.pairing_code {
            if !validate::pairing_code(code) {
                return Err("配对码的格式无效".into());
            }
            command.push(format!("--pairing-code={code}").into());
        }
        if let Some(node_id) = request.node_id {
            if !validate::node_id(node_id) {
                return Err("node_id 只允许小写字母、数字和连字符，最长 63".into());
            }
            command.push(format!("--node-id={node_id}").into());
        }
        command.extend(self.location());
        Ok(command)
    }

    /// `service <动作>`。安装记录里有状态目录与实例名，只需要指明前缀。
    fn service(&self, action: &str) -> std::result::Result<Vec<OsString>, String> {
        self.check()?;
        if !matches!(action, "status" | "start" | "stop") {
            return Err("action 只能是 status、start 或 stop".into());
        }
        let mut command = args(["service", action]);
        if !self.instance.is_empty() {
            command.extend(["--prefix".into(), OsString::from(self.prefix())]);
        }
        Ok(command)
    }

    /// 本机 controller 的数据目录。
    fn controller_data(&self) -> PathBuf {
        self.state_dir().join("controller")
    }

    /// `controller client-code`：在本机生成一次性的拥有者配对码。
    fn client_code(&self) -> Vec<OsString> {
        let mut command = args(["controller", "client-code", "--data-dir"]);
        command.extend([OsString::from(self.controller_data()), "--role".into(), "owner".into()]);
        command
    }
}

/// 安装器的 JSON 输出：整个标准输出是一个对象，或最后一行是（前面可能有说明文字）。
fn json_output(stdout: &str) -> Option<Value> {
    serde_json::from_str::<Value>(stdout.trim()).ok().filter(Value::is_object).or_else(|| helper::last_json(stdout))
}

/// 找官方 CLI 时查看的目录：PATH，外加 `~/.local/bin`；macOS 再加 Homebrew 的两个位置
/// （从访达启动的应用拿不到登录 shell 里设置的 PATH）。
fn search_dirs(path: Option<&OsStr>, home: &Path, macos: bool) -> Vec<PathBuf> {
    let mut dirs: Vec<PathBuf> = path.map(|path| std::env::split_paths(path).collect()).unwrap_or_default();
    dirs.push(home.join(".local").join("bin"));
    if macos {
        dirs.extend(["/opt/homebrew/bin", "/usr/local/bin"].map(PathBuf::from));
    }
    dirs
}

/// 在这些目录里找一个命令，只看文件在不在，不执行它。后台服务里能否使用以节点的 probe 结果为准。
fn find_cli(dirs: &[PathBuf], name: &str, windows: bool) -> Option<PathBuf> {
    let extensions: &[&str] = if windows { &[".exe", ".cmd", ".bat", ""] } else { &[""] };
    dirs.iter()
        .filter(|dir| dir.is_absolute())
        .flat_map(|dir| extensions.iter().map(move |extension| dir.join(format!("{name}{extension}"))))
        .find(|path| path.is_file())
}

/// 从要返回给页面的输出里去掉一个不该出现的值（配对码）。安装器不回显它，这是最后一道保险。
fn redact(output: String, secret: Option<&str>) -> String {
    match secret {
        Some(secret) if !secret.is_empty() => output.replace(secret, "***"),
        _ => output,
    }
}

/// 随包发行包的来源签名（流水线放在发行包旁边：`<发行包>.sig` 与 `allowed_signers`）。有就交给安装器核对：
/// 第一次安装时固定这份信任，之后的升级都要由同一把发行密钥签名（ADR 0004 第 5 条）。
fn release_signature(release: &Path) -> Vec<OsString> {
    let signature = PathBuf::from(format!("{}.sig", release.display()));
    let signers = release.with_file_name("allowed_signers");
    if signature.is_file() && signers.is_file() {
        vec!["--signature".into(), signature.into(), "--allowed-signers".into(), signers.into()]
    } else {
        Vec::new()
    }
}

/// 随包后台版本相对本机已安装版本：`newer`、`same`、`older`；任一方缺失为 null，写法不认识为 `unknown`。
fn upgrade(bundled: Option<&str>, installed: Option<&str>) -> Value {
    let (Some(bundled), Some(installed)) = (bundled, installed) else { return Value::Null };
    match (super::update::pep440_key(bundled), super::update::pep440_key(installed)) {
        (Some(a), Some(b)) => json!(match a.cmp(&b) {
            std::cmp::Ordering::Greater => "newer",
            std::cmp::Ordering::Equal => "same",
            std::cmp::Ordering::Less => "older",
        }),
        _ => json!("unknown"),
    }
}

fn busy() -> BridgeError {
    err("busy", "本机部署的另一项操作还在进行")
}

fn home_dir(app: &AppHandle) -> Result<PathBuf> {
    app.path().home_dir().map_err(io)
}

fn deployment(home: &Path) -> Deployment<'_> {
    Deployment { home, instance: INSTANCE, port: LOCAL_PORT }
}

/// 运行助手的一个部署子命令并收集输出。
async fn run(app: &AppHandle, helper: Helper, command: Vec<OsString>, timeout: Duration) -> Result<Captured> {
    run_up_to(app, helper, command, timeout, 1024 * 1024).await
}

/// 诊断导出与预览的输出：摘要上限 2 MiB，按缩进 JSON 打印后会更大。
const DIAGNOSTICS_OUTPUT: usize = 8 * 1024 * 1024;

async fn run_up_to(app: &AppHandle, helper: Helper, command: Vec<OsString>, timeout: Duration, limit: usize) -> Result<Captured> {
    let cwd = data_dir(app)?;
    tauri::async_runtime::spawn_blocking(move || helper::capture_up_to(helper.command(&cwd, &command), timeout, limit))
        .await
        .map_err(|_| err("helper_unavailable", "本机助手异常结束"))?
}

/// 安装器 `install --discover` 的输出；助手不可用、运行失败或输出无法解析时为 null。
async fn discover(app: &AppHandle, helper: Option<Helper>, home: &Path) -> Value {
    let deployment = deployment(home);
    let (Some(helper), Ok(())) = (helper, deployment.check()) else {
        return Value::Null;
    };
    match run(app, helper, deployment.discover(), DISCOVER_TIMEOUT).await {
        Ok(captured) => json_output(&captured.stdout).unwrap_or(Value::Null),
        Err(_) => Value::Null,
    }
}

#[tauri::command]
pub async fn local_discover(app: AppHandle) -> Result<Value> {
    let home = home_dir(&app)?;
    let bundle = bundle(&app);
    let dirs = search_dirs(std::env::var_os("PATH").as_deref(), &home, cfg!(target_os = "macos"));
    let cli = |name: &str| find_cli(&dirs, name, cfg!(windows)).map(|path| path.to_string_lossy().into_owned());
    let (codex, claude) = (cli("codex"), cli("claude"));
    let version = bundle.release.as_deref().and_then(helper::release_version);
    let helper_available = bundle.helper.is_some();
    let install = discover(&app, bundle.helper, &home).await;
    Ok(json!({
        "helper": helper_available,
        "bundle": { "version": version, "runtime": bundle.runtime.is_some() },
        // 随包后台版本与本机已安装版本的比较（PEP 440 语义）：newer 时页面提示“本机后台可升级”。
        "upgrade": upgrade(version.as_deref(), install["version"].as_str()),
        "install": install,
        "cli": { "codex": codex, "claude": claude },
    }))
}

#[tauri::command(rename_all = "snake_case")]
pub async fn local_setup(
    app: AppHandle,
    shell: State<'_, Shell>,
    role: String,
    controller_url: Option<String>,
    pairing_code: Option<String>,
    node_id: Option<String>,
) -> Result<Value> {
    let home = home_dir(&app)?;
    let bundle = bundle(&app);
    let (Some(helper), Some(release), Some(runtime)) = (bundle.helper, bundle.release, bundle.runtime) else {
        return Err(helper_unavailable());
    };
    let given = |value: &Option<String>| value.as_deref().map(str::trim).filter(|text| !text.is_empty()).map(str::to_owned);
    let (controller_url, pairing_code, node_id) = (given(&controller_url), given(&pairing_code), given(&node_id));
    let request = Setup { role: &role, controller_url: controller_url.as_deref(), pairing_code: pairing_code.as_deref(), node_id: node_id.as_deref() };
    let mut command = deployment(&home).setup(&release, &runtime, &request).map_err(invalid)?;
    command.extend(release_signature(&release));
    let _serial = shell.deploying.try_lock().map_err(|_| busy())?;
    let captured = run(&app, helper.clone(), command, SETUP_TIMEOUT).await?;
    Ok(json!({
        "ok": captured.code == Some(0),
        // 安装器输出的末尾，只返回给页面，不写日志。
        "output": redact(captured.tail, pairing_code.as_deref()),
        "install": discover(&app, Some(helper), &home).await,
    }))
}

#[tauri::command]
pub async fn local_service(app: AppHandle, shell: State<'_, Shell>, action: String) -> Result<Value> {
    let home = home_dir(&app)?;
    let helper = bundle(&app).helper.ok_or_else(helper_unavailable)?;
    let command = deployment(&home).service(&action).map_err(invalid)?;
    // 查询状态不改变任何东西，可以与安装、启停同时进行。
    let _serial = if action == "status" { None } else { Some(shell.deploying.try_lock().map_err(|_| busy())?) };
    let captured = run(&app, helper, command, SERVICE_TIMEOUT).await?;
    // 退出码非 0 时安装器同样在最后一行打印这份 JSON（带 error 字段），照样返回。
    helper::last_json(&captured.stdout).ok_or_else(|| {
        let code = captured.code.map_or("被终止".to_string(), |code| format!("退出码 {code}"));
        io(format!("安装器没有给出结果（{code}）：{}", helper::tail_chars(&captured.tail, 600)))
    })
}

#[tauri::command]
pub async fn local_pair(app: AppHandle, shell: State<'_, Shell>) -> Result<Value> {
    let home = home_dir(&app)?;
    let helper = bundle(&app).helper.ok_or_else(helper_unavailable)?;
    let deployment = deployment(&home);
    deployment.check().map_err(invalid)?;
    // 只在本机部署确实含 controller 时生成配对码：对一个没有数据库的目录运行 client-code 会就地新建一个。
    if !deployment.controller_data().join("controller.db").is_file() {
        return Err(err("not_found", "本机部署不含 controller，或还没有安装"));
    }
    let captured = run(&app, helper, deployment.client_code(), DISCOVER_TIMEOUT).await?;
    // 标准输出里有配对码：只在这里取用，不进入错误信息、日志和返回值，也不经过页面。
    let code = json_output(&captured.stdout).and_then(|output| output["code"].as_str().filter(|code| validate::pairing_code(code)).map(str::to_owned));
    let exit = captured.code;
    drop(captured);
    let code = code.ok_or_else(|| io(format!("本机 controller 没有给出配对码（退出码 {}）", exit.map_or("无".to_string(), |code| code.to_string()))))?;
    pair(&app, &shell, deployment.controller_url(), &code, &device_name(), None).await.map(|conn| conn.view())
}

// ---------------------------------------------------------------- 卸载本机后台

/// 卸载这个构建管理的本机部署：停止并移除它的服务与启动项、当前指针、启动器与当前代码；`purge_code` 时连同已归档的版本。
/// 状态目录（数据库、节点身份、备份与日志）始终保留：要删除它们由用户在确认后自己删除状态目录（界面给出位置）。
/// 不碰用户项目、官方 CLI 的配置与登录、共享缓存，也不碰别的实例。
#[tauri::command(rename_all = "snake_case")]
pub async fn local_uninstall(app: AppHandle, shell: State<'_, Shell>, purge_code: bool) -> Result<Value> {
    let home = home_dir(&app)?;
    let helper = bundle(&app).helper.ok_or_else(helper_unavailable)?;
    let deployment = deployment(&home);
    deployment.check().map_err(invalid)?;
    let mut command = args(["install", "--uninstall"]);
    if purge_code {
        command.push("--purge-code".into());
    }
    command.extend(deployment.location());
    let _serial = shell.deploying.try_lock().map_err(|_| busy())?;
    let captured = run(&app, helper.clone(), command, SERVICE_TIMEOUT).await?;
    Ok(json!({
        "ok": captured.code == Some(0),
        "output": captured.tail,
        "kept_state_dir": deployment.state_dir().to_string_lossy(),
        "install": discover(&app, Some(helper), &home).await,
    }))
}

// ---------------------------------------------------------------- 节点本机的项目目录授权（§13.10）

/// 这台机器上的节点数据目录。只在本机部署含节点、且节点已配对时可用。
fn worker_dir(deployment: &Deployment) -> Result<PathBuf> {
    deployment.check().map_err(invalid)?;
    let dir = deployment.state_dir().join("worker");
    if !dir.join("agent.json").is_file() {
        return Err(err("not_found", "这台机器上没有已配对的本机节点"));
    }
    Ok(dir)
}

fn project_id(value: &str) -> Result<String> {
    match uuid::Uuid::parse_str(value) {
        Ok(id) if id.hyphenated().to_string() == value => Ok(value.to_string()),
        _ => Err(invalid("project_id 必须是小写 UUID")),
    }
}

/// 运行节点的绑定子命令（`agent project-bind / project-unbind / project-bindings --data-dir <节点数据目录>`）。
async fn binding_command(app: &AppHandle, shell: &Shell, action: &str, extra: Vec<OsString>) -> Result<Value> {
    let home = home_dir(app)?;
    let helper = bundle(app).helper.ok_or_else(helper_unavailable)?;
    let dir = worker_dir(&deployment(&home))?;
    let mut command = args(["agent", action, "--data-dir"]);
    command.push(dir.into());
    command.extend(extra);
    let _serial = if action == "project-bindings" { None } else { Some(shell.deploying.try_lock().map_err(|_| busy())?) };
    let captured = run(app, helper, command, DISCOVER_TIMEOUT).await?;
    let output = json_output(&captured.stdout).ok_or_else(|| io(format!("节点没有给出结果：{}", helper::tail_chars(&captured.tail, 600))))?;
    if captured.code != Some(0) || output.get("error").is_some() {
        let message = output["error"]["message"].as_str().or(output["error"].as_str()).unwrap_or("节点拒绝了这次操作").to_string();
        return Err(invalid(message));
    }
    Ok(output)
}

/// 本机节点为哪些项目授权了哪个目录（含本机路径：这是节点主人在自己机器上看）。controller 只看到项目 ID 与目录名。
#[tauri::command]
pub async fn node_bindings(app: AppHandle, shell: State<'_, Shell>) -> Result<Value> {
    binding_command(&app, &shell, "project-bindings", Vec::new()).await
}

/// 在这台机器上为一个项目授权一个目录：目录只能经系统的选择文件夹对话框选取；之后 controller 可以请这台节点把
/// 该项目已发布的成果写回这个目录（仍按三方比较，冲突缺省不写）。
#[tauri::command(rename_all = "snake_case")]
pub async fn node_bind(window: tauri::WebviewWindow, app: AppHandle, shell: State<'_, Shell>, project_id: String) -> Result<Value> {
    let project_id = self::project_id(&project_id)?;
    worker_dir(&deployment(&home_dir(&app)?))?;
    let Some(folder) = super::dialog::pick_folder(&window, "选择授权给这个项目写回的文件夹").await? else {
        return Ok(json!({ "cancelled": true }));
    };
    binding_command(&app, &shell, "project-bind", vec![project_id.into(), folder.into()]).await
}

/// 撤销授权：只删除绑定，不动目录。正在进行的写回不受影响（节点执行时会再次核对绑定）。
#[tauri::command(rename_all = "snake_case")]
pub async fn node_unbind(app: AppHandle, shell: State<'_, Shell>, project_id: String) -> Result<Value> {
    let project_id = self::project_id(&project_id)?;
    binding_command(&app, &shell, "project-unbind", vec![project_id.into()]).await
}

// ---------------------------------------------------------------- 诊断导出

/// 诊断包暂存在应用本机数据目录的 `diagnostics/`，预览之后由用户另存；只落本机，不上传。
fn diagnostics_dir(app: &AppHandle) -> Result<PathBuf> {
    let dir = data_dir(app)?.join("diagnostics");
    std::fs::create_dir_all(&dir).map_err(io)?;
    Ok(dir)
}

fn diagnostics_file(app: &AppHandle, id: &str) -> Result<PathBuf> {
    match uuid::Uuid::parse_str(id) {
        Ok(parsed) if parsed.hyphenated().to_string() == id => Ok(diagnostics_dir(app)?.join(format!("{id}.zip"))),
        _ => Err(invalid("诊断包 ID 无效")),
    }
}

/// 用当前连接取几项 controller 信息交给助手（助手不访问网络、拿不到凭据）；拿不到的项如实记下原因。
async fn controller_info(shell: &Shell, epoch: u64) -> Result<Value> {
    let active = shell.lock().current(epoch)?;
    let mut info = serde_json::Map::new();
    for (key, path) in [("client", "/client"), ("doctor", "/doctor"), ("nodes", "/nodes"), ("events", "/events?before=0&limit=200")] {
        let url = super::validate::api_url(&active.base, path).map_err(invalid)?;
        let value = match shell.send(epoch, &active, active.http.api.get(url)).await {
            Ok(response) => {
                let status = response.status();
                match super::read_body(response).await {
                    Ok(body) if status.is_success() => serde_json::from_slice::<Value>(&body).unwrap_or(json!({ "error": "响应不是 JSON" })),
                    Ok(_) => json!({ "error": format!("HTTP {}", status.as_u16()) }),
                    Err(e) => json!({ "error": e.message }),
                }
            }
            Err(e) => json!({ "error": e.message }),
        };
        info.insert(key.to_string(), value);
    }
    Ok(Value::Object(info))
}

/// 生成一份诊断包并返回预览。`epoch` 给出时附带当前连接的 controller 信息；`include_task_logs` 时收集任务原始日志
/// （可能包含项目内容，照样脱敏）。
#[tauri::command(rename_all = "snake_case")]
pub async fn diagnostics_export(app: AppHandle, shell: State<'_, Shell>, epoch: Option<u64>, include_task_logs: bool) -> Result<Value> {
    let home = home_dir(&app)?;
    let helper = bundle(&app).helper.ok_or_else(helper_unavailable)?;
    let deployment = deployment(&home);
    deployment.check().map_err(invalid)?;
    let id = uuid::Uuid::new_v4().to_string();
    let out = diagnostics_file(&app, &id)?;
    let mut command = args(["diagnostics", "export", "--out"]);
    command.extend([out.clone().into(), "--prefix".into(), deployment.prefix().into(), "--state-dir".into(), deployment.state_dir().into()]);
    let info_file = match epoch {
        Some(epoch) => {
            let info = controller_info(&shell, epoch).await?;
            let path = diagnostics_dir(&app)?.join(format!("{id}-controller.json"));
            super::write_json(&path, &info)?;
            command.extend(["--controller-info".into(), path.clone().into()]);
            Some(path)
        }
        None => None,
    };
    if include_task_logs {
        command.push("--include-task-logs".into());
    }
    let captured = run_up_to(&app, helper, command, SETUP_TIMEOUT, DIAGNOSTICS_OUTPUT).await;
    if let Some(path) = info_file {
        let _ = std::fs::remove_file(path); // 原始应答只给助手读一次
    }
    let captured = captured?;
    let preview = json_output(&captured.stdout).filter(|_| captured.code == Some(0) && out.is_file());
    let preview = preview.ok_or_else(|| io(format!("诊断导出失败：{}", helper::tail_chars(&captured.tail, 600))))?;
    Ok(json!({ "id": id, "preview": preview }))
}

/// 查看已生成的诊断包里某个文件的文本（有上限），用于导出后的预览。
#[tauri::command]
pub async fn diagnostics_preview(app: AppHandle, id: String, name: String) -> Result<Value> {
    let helper = bundle(&app).helper.ok_or_else(helper_unavailable)?;
    let file = diagnostics_file(&app, &id)?;
    if name.is_empty() || name.len() > 200 || name.contains("..") || name.starts_with(['/', '\\']) {
        return Err(invalid("文件名无效"));
    }
    let mut command = args(["diagnostics", "preview"]);
    command.extend([file.into(), "--show".into(), OsString::from(name)]);
    let captured = run_up_to(&app, helper, command, DISCOVER_TIMEOUT, DIAGNOSTICS_OUTPUT).await?;
    Ok(json!({ "ok": captured.code == Some(0), "text": helper::tail_chars(&captured.stdout, 256 * 1024) }))
}

/// 把预览过的诊断包另存到用户选定的位置；之后删除暂存的那一份。
#[tauri::command]
pub async fn diagnostics_save(window: tauri::WebviewWindow, app: AppHandle, id: String) -> Result<Value> {
    let file = diagnostics_file(&app, &id)?;
    if !file.is_file() {
        return Err(err("not_found", "这份诊断包已不存在，重新导出"));
    }
    let Some(dest) = super::dialog::save(&window, format!("agent-collab-diagnostics-{}.zip", &id[..8])).await? else {
        return Ok(json!({ "cancelled": true }));
    };
    let bytes = std::fs::copy(&file, &dest).map_err(io)?;
    let _ = std::fs::remove_file(&file);
    Ok(json!({ "path": dest.to_string_lossy(), "bytes": bytes }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn strings(command: &[OsString]) -> Vec<String> {
        command.iter().map(|arg| arg.to_string_lossy().replace('\\', "/")).collect()
    }

    fn deployed<'a>(instance: &'a str, port: &'a str) -> Deployment<'a> {
        Deployment { home: Path::new("/home/u"), instance, port }
    }

    const RELEASE: &str = "/res/release/agent-collab-1.0.0.tar.gz";
    const RUNTIME: &str = "/res/runtime";

    fn setup(deployment: &Deployment, role: &str, controller_url: Option<&str>, pairing_code: Option<&str>, node_id: Option<&str>) -> std::result::Result<Vec<String>, String> {
        let request = Setup { role, controller_url, pairing_code, node_id };
        deployment.setup(Path::new(RELEASE), Path::new(RUNTIME), &request).map(|command| strings(&command))
    }

    #[test]
    fn default_instance_leaves_locations_to_the_installer() {
        let release = deployed("", "18669");
        assert!(release.location().is_empty());
        assert_eq!(strings(&release.discover()), ["install", "--discover"]);
        assert_eq!(strings(&release.service("status").unwrap()), ["service", "status"]);
        assert_eq!(strings(&release.client_code()), ["controller", "client-code", "--data-dir", "/home/u/.local/state/agent-collab/controller", "--role", "owner"]);
        assert_eq!(release.controller_url(), "http://127.0.0.1:18669");
    }

    #[test]
    fn named_instance_gets_its_own_prefix_state_and_service_names() {
        let candidate = deployed("dev", "18769");
        let location = ["--prefix", "/home/u/.local/share/agent-collab-dev", "--state-dir", "/home/u/.local/state/agent-collab-dev", "--instance", "dev"];
        assert_eq!(strings(&candidate.location()), location);
        assert_eq!(strings(&candidate.discover()), [&["install", "--discover"][..], &location[..]].concat());
        assert_eq!(strings(&candidate.service("stop").unwrap()), ["service", "stop", "--prefix", "/home/u/.local/share/agent-collab-dev"]);
        assert_eq!(strings(&candidate.client_code())[3], "/home/u/.local/state/agent-collab-dev/controller");
        assert_eq!(candidate.controller_url(), "http://127.0.0.1:18769");
    }

    #[test]
    fn setup_command_for_each_role() {
        let release = deployed("", "18669");
        let head = ["install", "--release", RELEASE, "--runtime", RUNTIME, "--role"];
        let local = ["--bind", "127.0.0.1:18669", "--controller-url", "http://127.0.0.1:18669"];

        // 本机 controller 加节点：页面不必给地址，给了也不采用。
        let all = setup(&release, "all-in-one", None, None, None).unwrap();
        assert_eq!(all, [&head[..], &["all-in-one"], &local[..]].concat());
        assert_eq!(setup(&release, "all-in-one", Some("https://elsewhere.example"), None, None).unwrap(), all);
        assert_eq!(setup(&release, "controller", None, None, None).unwrap(), [&head[..], &["controller"], &local[..]].concat());

        // 节点：连接已有的 controller。地址经过与配对相同的校验并规范化。
        let worker = setup(&release, "worker", Some(" https://ctl.example:8443/api/v1 "), Some("3f9a0c1d2e4b"), Some("node-desk")).unwrap();
        assert_eq!(worker, [&head[..], &["worker", "--controller-url=https://ctl.example:8443", "--pairing-code=3f9a0c1d2e4b", "--node-id=node-desk"]].concat());
        // 升级已有的节点：身份与地址沿用安装记录，可以都不给。
        assert_eq!(setup(&release, "worker", None, None, None).unwrap(), [&head[..], &["worker"]].concat());

        // 带实例名的构建在末尾指明自己的那一份安装。
        let candidate = deployed("dev", "18769");
        let named = setup(&candidate, "all-in-one", None, None, Some("node-dev")).unwrap();
        assert_eq!(named[6..], [
            "all-in-one", "--bind", "127.0.0.1:18769", "--controller-url", "http://127.0.0.1:18769", "--node-id=node-dev",
            "--prefix", "/home/u/.local/share/agent-collab-dev", "--state-dir", "/home/u/.local/state/agent-collab-dev", "--instance", "dev",
        ]);
    }

    #[test]
    fn page_values_cannot_become_installer_options() {
        let release = deployed("", "18669");
        for role in ["", "owner", "all-in-one --uninstall", "--uninstall", "Worker"] {
            assert!(setup(&release, role, None, None, None).is_err(), "{role}");
        }
        for url in ["http://ctl.example", "--uninstall", "ftp://x", "https://u:p@ctl.example", "https://ctl.example/?x=--uninstall"] {
            assert!(setup(&release, "worker", Some(url), None, None).is_err(), "{url}");
        }
        for code in ["--uninstall", "-x", "a b", "a\nb"] {
            assert!(setup(&release, "worker", None, Some(code), None).is_err(), "{code}");
        }
        for node_id in ["--purge-code", "Node", "a b", "-n"] {
            assert!(setup(&release, "worker", None, None, Some(node_id)).is_err(), "{node_id}");
        }
        for action in ["", "restart", "stop --allow-active", "--prefix"] {
            assert!(release.service(action).is_err(), "{action}");
        }
        // 每个页面给的值都和它的选项连在同一个参数里。
        let worker = setup(&release, "worker", Some("https://ctl.example"), Some("abc"), Some("n1")).unwrap();
        assert!(worker[7..].iter().all(|arg| arg.starts_with("--") && arg.contains('=')), "{worker:?}");
    }

    #[test]
    fn build_time_instance_and_port_are_checked() {
        for (instance, port) in [("", "18669"), ("dev", "1"), ("internal-test", "65535"), ("a1", "8080")] {
            assert!(deployed(instance, port).check().is_ok(), "{instance} {port}");
        }
        for (instance, port) in [("Dev", "18669"), ("-dev", "18669"), ("a b", "18669"), ("../x", "18669"), ("", "0"), ("", "65536"), ("", "80a"), ("", ""), ("", "+80")] {
            assert!(deployed(instance, port).check().is_err(), "{instance} {port}");
            assert!(setup(&deployed(instance, port), "all-in-one", None, None, None).is_err());
            assert!(deployed(instance, port).service("status").is_err());
        }
        assert!(deployed(&"a".repeat(31), "1").check().is_ok());
        assert!(deployed(&"a".repeat(32), "1").check().is_err());
        // 这个构建自己的取值必须可用。
        assert!(Deployment { home: Path::new("/home/u"), instance: INSTANCE, port: LOCAL_PORT }.check().is_ok());
    }

    #[test]
    fn installer_output_is_one_object_or_the_last_line() {
        assert_eq!(json_output("{\"installed\": true, \"role\": \"all-in-one\"}\n"), Some(json!({ "installed": true, "role": "all-in-one" })));
        assert_eq!(json_output("{\n  \"installed\": false\n}\n"), Some(json!({ "installed": false })));
        assert_eq!(json_output("1. Validate\n2. Stage\n{\"action\": \"start\", \"ready\": true}\n"), Some(json!({ "action": "start", "ready": true })));
        assert_eq!(json_output("{\"code\": \"abc\", \"scopes\": [\"read\"], \"ttl_s\": 600}\n").unwrap()["code"], "abc");
        for bad in ["", "Installer: failed\n", "[1, 2]\n", "null\n"] {
            assert_eq!(json_output(bad), None, "{bad}");
        }
    }

    #[test]
    fn official_clis_are_looked_up_without_running_them() {
        let root = std::env::temp_dir().join(format!("agent-collab-desktop-local-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let (first, second) = (root.join("first"), root.join("second"));
        for dir in [&first, &second] {
            std::fs::create_dir_all(dir).unwrap();
        }
        std::fs::write(second.join("codex"), b"").unwrap();
        std::fs::write(second.join("codex.cmd"), b"").unwrap();
        std::fs::create_dir_all(first.join("claude")).unwrap(); // 同名目录不算
        let dirs = [PathBuf::from("relative"), first.clone(), second.clone(), root.join("missing")];
        assert_eq!(find_cli(&dirs, "codex", false), Some(second.join("codex")));
        assert_eq!(find_cli(&dirs, "codex", true), Some(second.join("codex.cmd")));
        assert_eq!(find_cli(&dirs, "claude", false), None);
        assert_eq!(find_cli(&[], "codex", true), None);
        // 排在前面的目录优先。
        std::fs::write(first.join("codex.exe"), b"").unwrap();
        assert_eq!(find_cli(&dirs, "codex", true), Some(first.join("codex.exe")));
        std::fs::remove_dir_all(&root).unwrap();

        let path = std::env::join_paths([first.clone(), second.clone()]).unwrap();
        let home = Path::new("/home/u");
        assert_eq!(search_dirs(Some(path.as_os_str()), home, false), [first.clone(), second.clone(), home.join(".local").join("bin")]);
        assert_eq!(search_dirs(None, home, true), [home.join(".local").join("bin"), PathBuf::from("/opt/homebrew/bin"), PathBuf::from("/usr/local/bin")]);
    }

    #[test]
    fn bundled_backend_is_compared_semantically() {
        assert_eq!(upgrade(Some("0.3.0.dev10"), Some("0.3.0.dev9")), json!("newer"));
        assert_eq!(upgrade(Some("0.3.0.dev1"), Some("0.3.0.dev1")), json!("same"));
        assert_eq!(upgrade(Some("0.2.0.dev7"), Some("0.2.0")), json!("older"));
        assert_eq!(upgrade(Some("0.3.0.dev1"), None), Value::Null);
        assert_eq!(upgrade(Some("weird"), Some("0.2.0")), json!("unknown"));
    }

    #[test]
    fn pairing_code_never_reaches_the_returned_output() {
        assert_eq!(redact("paired with 3f9a0c and 3f9a0c again".into(), Some("3f9a0c")), "paired with *** and *** again");
        assert_eq!(redact("nothing here".into(), Some("3f9a0c")), "nothing here");
        assert_eq!(redact("text".into(), None), "text");
        assert_eq!(redact("text".into(), Some("")), "text");
    }
}
