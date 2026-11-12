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
        let name = if self.instance.is_empty() { "agent-collab".to_string() } else { format!("agent-collab-{}", self.instance) };
        self.home.join(".local").join(kind).join(name)
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
    let cwd = data_dir(app)?;
    tauri::async_runtime::spawn_blocking(move || helper::capture(helper.command(&cwd, &command), timeout))
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
    Ok(json!({
        "helper": bundle.helper.is_some(),
        "bundle": { "version": version, "runtime": bundle.runtime.is_some() },
        "install": discover(&app, bundle.helper, &home).await,
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
    let command = deployment(&home).setup(&release, &runtime, &request).map_err(invalid)?;
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
    fn pairing_code_never_reaches_the_returned_output() {
        assert_eq!(redact("paired with 3f9a0c and 3f9a0c again".into(), Some("3f9a0c")), "paired with *** and *** again");
        assert_eq!(redact("nothing here".into(), Some("3f9a0c")), "nothing here");
        assert_eq!(redact("text".into(), None), "text");
        assert_eq!(redact("text".into(), Some("")), "text");
    }
}
