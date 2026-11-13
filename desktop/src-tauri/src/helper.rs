//! 本机助手：随包的 Python 运行时加 `agent_collab` 代码，由外壳以固定的子命令启动
//! （`project local <动作>`、`install`、`service`、`controller client-code`）。
//!
//! 助手只做本机文件工作与本机部署。它不访问网络，也拿不到客户端凭据：凭据不进入它的参数、环境与文件，
//! 上传与下载由外壳完成。这里负责找到它、拼出命令行、运行并读取输出，不依赖 Tauri 运行时。

use std::borrow::Cow;
use std::ffi::OsString;
use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Stdio};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde_json::Value;

use super::{err, io, BridgeError, Result};

/// 运行时目录里解释器的候选位置，按顺序取第一个存在的。
const INTERPRETERS: [&str; 4] = ["python/bin/python3", "bin/python3", "python/python.exe", "python.exe"];
/// 助手单行输出的上限。扫描结果那一行带着整份文件清单。
// ponytail: 整行读进内存再解析；清单大到这个量级（约四十万个文件）时扫描直接失败，要支持就让助手把清单只写进快照文件。
const MAX_LINE: u64 = 64 * 1024 * 1024;
const TICK: Duration = Duration::from_millis(100);
/// 助手退出后再等这么久收尾它的输出：它启动的后台进程可能还握着输出管道，不能等到管道关闭。
const GRACE: Duration = Duration::from_secs(2);

/// 一个可以启动的助手：解释器加入口。
#[derive(Clone, Debug, PartialEq)]
pub struct Helper {
    python: PathBuf,
    /// 含 `helper.py` 的目录；`module` 为 true 时是含 `agent_collab` 包的目录。
    app: PathBuf,
    /// 以 `-m agent_collab.cli` 方式运行（仅 `automation` feature 下由环境变量指定位置时）。
    module: bool,
}

/// 随包资源里与助手有关的三样东西；缺了哪样，哪样就是 None。
pub struct Bundle {
    pub helper: Option<Helper>,
    /// Python 运行时目录（安装本机部署时交给安装器）。
    pub runtime: Option<PathBuf>,
    /// 发行包 `agent-collab-<版本>.tar.gz`。
    pub release: Option<PathBuf>,
    /// 随包载荷核对的结果（见 [`verify_payload`]）。没通过时上面三项都是 None：不启动内容不确定的助手。
    pub payload: std::result::Result<(), String>,
}

/// 构建时编进来的随包载荷清单 `resources/payload.json` 的 SHA-256（ADR 0004 第 7 条）。
/// 开发构建没有清单，为 None，不做核对。
const PAYLOAD_SHA256: Option<&str> = option_env!("AGENT_COLLAB_PAYLOAD_SHA256");

fn sha256_file(path: &Path) -> std::io::Result<String> {
    use sha2::{Digest, Sha256};
    let mut file = std::fs::File::open(path)?;
    let mut hasher = Sha256::new();
    std::io::copy(&mut file, &mut hasher)?;
    Ok(hasher.finalize().iter().map(|b| format!("{b:02x}")).collect())
}

/// 核对随包载荷：`payload.json` 的摘要等于构建时编进来的值；`resources/` 下除它之外的每个文件都在清单里、
/// 大小与 SHA-256 相符；清单里的每个文件都存在。清单外的文件（例如上一版留下、新版已删除或改名的模块）、
/// 缺文件与内容不符都拒绝：激活的助手、运行时与发行包只能是这次构建的那一份。
pub fn verify_payload(resources: &Path, expected: Option<&str>) -> std::result::Result<(), String> {
    let Some(expected) = expected else { return Ok(()) };
    let manifest = std::fs::read(resources.join("payload.json")).map_err(|e| format!("缺少随包载荷清单 payload.json（{e}）"))?;
    let digest = {
        use sha2::{Digest, Sha256};
        Sha256::digest(&manifest).iter().map(|b| format!("{b:02x}")).collect::<String>()
    };
    if digest != expected {
        return Err("随包载荷清单与这个程序不对应（安装不完整，或混入了别的版本）".into());
    }
    let manifest: Value = serde_json::from_slice(&manifest).map_err(|e| format!("随包载荷清单格式无效：{e}"))?;
    let listed = manifest["files"].as_object().ok_or("随包载荷清单没有 files")?;
    let mut seen = 0usize;
    let mut pending = vec![resources.to_path_buf()];
    while let Some(dir) = pending.pop() {
        for entry in std::fs::read_dir(&dir).map_err(|e| format!("读取 {} 失败：{e}", dir.display()))?.flatten() {
            let path = entry.path();
            let kind = entry.file_type().map_err(|e| e.to_string())?;
            let relative = path.strip_prefix(resources).map_err(|e| e.to_string())?.to_string_lossy().replace('\\', "/");
            if kind.is_symlink() {
                return Err(format!("载荷里不应有链接：{relative}"));
            }
            if kind.is_dir() {
                pending.push(path);
                continue;
            }
            if relative == "payload.json" {
                continue;
            }
            let entry = listed.get(&relative).ok_or_else(|| format!("清单外的文件：{relative}（可能是上一版残留，重新运行安装程序修复）"))?;
            let size = std::fs::metadata(&path).map_err(|e| e.to_string())?.len();
            if Some(size) != entry["size"].as_u64() || sha256_file(&path).map_err(|e| e.to_string())?.as_str() != entry["sha256"].as_str().unwrap_or("") {
                return Err(format!("文件内容与清单不符：{relative}"));
            }
            seen += 1;
        }
    }
    if seen != listed.len() {
        return Err(format!("缺少 {} 个清单里的文件（安装不完整）", listed.len() - seen));
    }
    Ok(())
}

/// 每个进程只核对一次（运行时几百个文件，约百毫秒）。
fn payload_once(resources: &Path) -> std::result::Result<(), String> {
    static CHECKED: std::sync::OnceLock<std::result::Result<(), String>> = std::sync::OnceLock::new();
    CHECKED.get_or_init(|| verify_payload(resources, PAYLOAD_SHA256)).clone()
}

pub fn interpreter(runtime: &Path) -> Option<PathBuf> {
    INTERPRETERS.iter().map(|candidate| runtime.join(candidate)).find(|path| path.is_file())
}

/// 发行包文件名里的版本：`agent-collab-<版本>.tar.gz`。
pub fn release_version(path: &Path) -> Option<String> {
    let version = path.file_name()?.to_str()?.strip_prefix("agent-collab-")?.strip_suffix(".tar.gz")?;
    (!version.is_empty()).then(|| version.to_string())
}

/// 随包的发行包。构建时定下了文件名（`AGENT_COLLAB_BUNDLED_RELEASE`）就只认那一个：安装包升级不会删掉上一版
/// 留下的发行包，目录里可能不止一个。没有定下时取目录里唯一的那个；没有或不止一个都返回 None（不猜用哪个）。
pub fn release(dir: &Path) -> Option<PathBuf> {
    release_named(dir, option_env!("AGENT_COLLAB_BUNDLED_RELEASE"))
}

fn release_named(dir: &Path, name: Option<&str>) -> Option<PathBuf> {
    if let Some(name) = name {
        let path = dir.join(name);
        return (path.is_file() && release_version(&path).is_some()).then_some(path);
    }
    let mut found = std::fs::read_dir(dir).ok()?.flatten().map(|entry| entry.path()).filter(|path| path.is_file() && release_version(path).is_some());
    let first = found.next()?;
    found.next().is_none().then_some(first)
}

/// 在应用资源目录的 `resources/` 下找助手：`runtime/`、`app/`（`helper.py` 与 `agent_collab` 包）、`release/`。
pub fn locate(resources: Option<&Path>) -> Bundle {
    let Some(resources) = resources else {
        return Bundle { helper: overridden(None, None), runtime: None, release: None, payload: Ok(()) };
    };
    if let Err(reason) = payload_once(resources) {
        return Bundle { helper: overridden(None, None), runtime: None, release: None, payload: Err(reason) };
    }
    let runtime = Some(resources.join("runtime")).filter(|dir| interpreter(dir).is_some());
    let app = resources.join("app");
    let bundled = runtime
        .as_deref()
        .and_then(interpreter)
        .filter(|_| app.join("helper.py").is_file() && app.join("agent_collab").is_dir())
        .map(|python| Helper { python, app: app.clone(), module: false });
    Bundle { helper: overridden(bundled, Some(&app)), runtime, release: release(&resources.join("release")), payload: Ok(()) }
}

/// 仅 `automation` feature：`AGENT_COLLAB_HELPER_PYTHON` 指定解释器，`AGENT_COLLAB_HELPER_APP` 指定含
/// `agent_collab` 包的目录（此时不要求 `helper.py`）。未启用该 feature 的二进制只用随包的助手。
#[cfg(feature = "automation")]
fn overridden(bundled: Option<Helper>, app: Option<&Path>) -> Option<Helper> {
    let python = std::env::var_os("AGENT_COLLAB_HELPER_PYTHON").map(PathBuf::from);
    let package = std::env::var_os("AGENT_COLLAB_HELPER_APP").map(PathBuf::from);
    override_with(bundled, app, python, package)
}

/// 覆盖规则本身：只给解释器时仍用随包的 `helper.py`；给了包目录就以模块方式运行，解释器没给则用随包的。
#[cfg(any(feature = "automation", test))]
fn override_with(bundled: Option<Helper>, app: Option<&Path>, python: Option<PathBuf>, package: Option<PathBuf>) -> Option<Helper> {
    if python.is_none() && package.is_none() {
        return bundled;
    }
    let python = python.or_else(|| bundled.map(|helper| helper.python))?;
    match package {
        Some(app) => Some(Helper { python, app, module: true }),
        None => app.filter(|dir| dir.join("helper.py").is_file()).map(|dir| Helper { python, app: dir.to_path_buf(), module: false }),
    }
}

#[cfg(not(feature = "automation"))]
fn overridden(bundled: Option<Helper>, _app: Option<&Path>) -> Option<Helper> {
    bundled
}

impl Helper {
    /// 解释器之后的全部参数。`-B` 不写字节码，`-s` 不看用户 site-packages，`-E` 不看 `PYTHON*` 环境变量，
    /// `-X utf8` 让标准输出与文件名在 Windows 上也按 UTF-8 编码（否则非 ASCII 路径会按本机代码页输出）。
    fn argv(&self, args: &[OsString]) -> Vec<OsString> {
        let mut argv: Vec<OsString> = ["-B", "-s", "-X", "utf8"].map(Into::into).into();
        if self.module {
            // 包的位置经 PYTHONPATH 给出，所以这种方式不能带 -E。
            argv.extend(["-m", "agent_collab.cli"].map(OsString::from));
        } else {
            argv.push("-E".into());
            argv.push(self.app.join("helper.py").into());
        }
        argv.extend_from_slice(args);
        argv
    }

    /// 启动助手的命令：标准输入为空，工作目录是应用本机数据目录，Windows 上不弹控制台窗口。
    pub fn command(&self, cwd: &Path, args: &[OsString]) -> Command {
        let mut command = Command::new(&self.python);
        command.args(self.argv(args)).current_dir(cwd).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
        if self.module {
            command.env("PYTHONPATH", &self.app);
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
        }
        command
    }
}

// ---------------------------------------------------------------- 运行

enum Event {
    Line { stderr: bool, text: String },
    TooLong,
}

/// 把一条管道按行送进通道。行尾的换行去掉；非 UTF-8 字节按替换字符处理。
fn pump(pipe: impl Read + Send + 'static, stderr: bool, tx: mpsc::Sender<Event>) {
    std::thread::spawn(move || {
        let mut reader = BufReader::new(pipe);
        let mut line = Vec::new();
        loop {
            line.clear();
            match reader.by_ref().take(MAX_LINE).read_until(b'\n', &mut line) {
                Ok(0) | Err(_) => return,
                Ok(_) if line.last() != Some(&b'\n') && line.len() as u64 >= MAX_LINE => {
                    let _ = tx.send(Event::TooLong);
                    return;
                }
                Ok(_) => {
                    let text = String::from_utf8_lossy(&line).trim_end_matches(['\n', '\r']).to_string();
                    if tx.send(Event::Line { stderr, text }).is_err() {
                        return;
                    }
                }
            }
        }
    });
}

/// 运行到结束，返回退出码。输出逐行交给 `on_line(来自标准错误, 文本)`。
///
/// `stop` 每 100 毫秒问一次：返回错误时结束子进程并以该错误返回（取消、连接已切换）。
/// 超过 `timeout` 同样结束子进程。阻塞调用，放在阻塞线程里。
pub fn run(
    mut command: Command,
    timeout: Option<Duration>,
    stop: &dyn Fn() -> Option<BridgeError>,
    on_line: &mut dyn FnMut(bool, &str),
) -> Result<Option<i32>> {
    let _pass = GATE.enter(&label(&command)).ok_or_else(|| err("busy", "正在安装桌面端更新，本机助手暂停使用"))?;
    let mut child = command.spawn().map_err(|e| err("helper_unavailable", format!("无法启动本机助手：{e}")))?;
    let (tx, rx) = mpsc::channel();
    if let Some(pipe) = child.stdout.take() {
        pump(pipe, false, tx.clone());
    }
    if let Some(pipe) = child.stderr.take() {
        pump(pipe, true, tx);
    }
    let (started, mut checked) = (Instant::now(), Instant::now());
    let mut open = true;
    let mut exited: Option<(ExitStatus, Instant)> = None;
    let failure = loop {
        if open {
            match rx.recv_timeout(TICK) {
                Ok(Event::Line { stderr, text }) => on_line(stderr, &text),
                Ok(Event::TooLong) => break io("本机助手的输出超过上限"),
                Err(RecvTimeoutError::Timeout) => {}
                Err(RecvTimeoutError::Disconnected) => open = false,
            }
        } else if exited.is_none() {
            std::thread::sleep(TICK);
        }
        if let Some((status, at)) = exited {
            if !open || at.elapsed() > GRACE {
                return Ok(status.code());
            }
            continue;
        }
        if checked.elapsed() < TICK {
            continue;
        }
        checked = Instant::now();
        if let Some(reason) = stop() {
            break reason;
        }
        if timeout.is_some_and(|limit| started.elapsed() > limit) {
            break io("本机助手运行超时，已结束");
        }
        match child.try_wait() {
            Ok(Some(status)) => exited = Some((status, Instant::now())),
            Ok(None) => {}
            Err(e) => break io(e),
        }
    };
    let _ = child.kill();
    let _ = child.wait();
    Err(failure)
}

/// 助手进程的闸门：记录正在运行的助手（及其子命令）；关闸后不再启动新的。
///
/// 桌面端更新在 Windows 上由安装程序替换随包运行时，正在运行的助手占着它的 DLL（例如 `zipfile` 导入时加载的
/// `_bz2.pyd`），安装程序写不进去，只能停在“中止/重试/忽略”（M3 干净虚拟机实测）。所以启动安装程序之前先关闸、
/// 等正在运行的助手结束（见 [`Gate::exclusive`]）。只管本机助手进程：远端 controller 的请求与本机后台服务不经过这里。
pub struct Gate {
    state: Mutex<GateState>,
}

struct GateState {
    closed: bool,
    next: u64,
    running: Vec<(u64, String)>,
}

pub static GATE: Gate = Gate::new();

/// 一个助手进程的通行证；放下时（正常结束、出错或 panic 展开）从运行列表里去掉。
pub struct Pass<'a>(&'a Gate, u64);

impl Drop for Pass<'_> {
    fn drop(&mut self) {
        self.0.lock().running.retain(|(id, _)| *id != self.1);
    }
}

impl Gate {
    pub const fn new() -> Self {
        Gate { state: Mutex::new(GateState { closed: false, next: 0, running: Vec::new() }) }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, GateState> {
        self.state.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// 关闸与进入在同一把锁下判断：要么关闸方等到这个助手结束，要么这个助手被拒，没有“刚判断空闲又启动”的窗口。
    pub fn enter(&self, label: &str) -> Option<Pass<'_>> {
        let mut state = self.lock();
        if state.closed {
            return None;
        }
        state.next += 1;
        let id = state.next;
        state.running.push((id, label.to_string()));
        Some(Pass(self, id))
    }

    /// 关闸并等正在运行的助手结束；超时则重新开闸，返回还在运行的助手（子命令）。不结束它们：
    /// 正在应用文件或安装本机部署的助手被强行结束会留下半完成的状态。
    pub fn quiesce(&self, timeout: Duration) -> std::result::Result<(), Vec<String>> {
        self.lock().closed = true;
        let deadline = Instant::now() + timeout;
        loop {
            {
                let mut state = self.lock();
                if state.running.is_empty() {
                    return Ok(());
                }
                if Instant::now() >= deadline {
                    state.closed = false;
                    return Err(state.running.iter().map(|(_, label)| label.clone()).collect());
                }
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }

    pub fn reopen(&self) {
        self.lock().closed = false;
    }

    /// 关闸、等助手结束，再执行 `install`（启动安装程序）。等不到就重新开闸并拒绝（更新延后，原来的操作不被打断）；
    /// `install` 失败也重新开闸，旧界面照常使用助手。只有 `install` 成功（安装程序已接管、本进程随后退出或重启）时保持关闸。
    pub fn exclusive<T>(&self, timeout: Duration, install: impl FnOnce() -> Result<T>) -> Result<T> {
        if let Err(running) = self.quiesce(timeout) {
            return Err(err("busy", format!("本机助手还在运行（{}），这次没有安装；等它们结束后再安装", running.join("、"))));
        }
        let result = install();
        if result.is_err() {
            self.reopen();
        }
        result
    }
}

/// 助手命令行里入口之后的子命令（`install --discover`、`service status`、`project local scan`），用来说明正在等哪个操作。
/// 不含路径与选项的值。
pub fn label(command: &Command) -> String {
    let args: Vec<String> = command.get_args().map(|arg| arg.to_string_lossy().into_owned()).collect();
    let start = args.iter().position(|arg| arg.ends_with("helper.py") || arg == "agent_collab.cli").map_or(0, |at| at + 1);
    let rest = &args[start..];
    let mut words: Vec<&str> = rest.iter().map(String::as_str).take_while(|arg| !arg.starts_with('-')).take(3).collect();
    let operation = rest.get(words.len()).filter(|arg| arg.starts_with("--") && !arg.contains('=') && !["--prefix", "--state-dir", "--instance"].contains(&arg.as_str()));
    words.extend(operation.map(String::as_str));
    if words.is_empty() { "本机助手".into() } else { words.join(" ") }
}

/// 只保留输出末尾的若干字符。
pub struct Tail {
    text: String,
    max: usize,
}

impl Tail {
    pub fn new(max: usize) -> Self {
        Self { text: String::new(), max }
    }

    pub fn push(&mut self, line: &str) {
        self.text.push_str(line);
        self.text.push('\n');
        if self.text.len() > self.max * 8 {
            self.text = tail_chars(&self.text, self.max);
        }
    }

    pub fn finish(self) -> String {
        tail_chars(self.text.trim_end(), self.max)
    }
}

/// 最后 `max` 个字符。
pub fn tail_chars(text: &str, max: usize) -> String {
    let skip = text.chars().count().saturating_sub(max);
    text.chars().skip(skip).collect()
}

// ---------------------------------------------------------------- project local 的输出

/// `project local` 标准输出的一行。
#[derive(Debug, PartialEq)]
pub enum Message {
    Progress(Value),
    Result(Value),
    Error(BridgeError),
    /// 不是这三种 JSON 行（助手不应该输出别的；忽略而不是失败）。
    Other,
}

pub fn parse_line(line: &str) -> Message {
    let Ok(Value::Object(mut map)) = serde_json::from_str::<Value>(line) else {
        return Message::Other;
    };
    if let Some(value) = map.remove("result") {
        return Message::Result(value);
    }
    if let Some(error) = map.remove("error") {
        return Message::Error(helper_error(&error));
    }
    map.remove("progress").map_or(Message::Other, Message::Progress)
}

/// 助手的错误码原样作为桥错误码透出（`unstable`、`source_changed`、`unfinished_application` 等）；
/// 形状不像错误码的按 `io` 处理。
fn helper_error(error: &Value) -> BridgeError {
    let code = error["code"].as_str().filter(|code| {
        (1..=64).contains(&code.len()) && code.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
    });
    let message: String = error["message"].as_str().unwrap_or("本机助手报告了错误").chars().take(2000).collect();
    BridgeError { code: code.map_or(Cow::Borrowed("io"), |code| Cow::Owned(code.to_string())), message }
}

/// 运行一次 `project local <动作>`：进度行交给 `progress`，返回结果行里的值或错误行对应的错误。
pub fn project(command: Command, stop: &dyn Fn() -> Option<BridgeError>, progress: &mut dyn FnMut(&Value)) -> Result<Value> {
    let mut outcome = None;
    let mut stderr = Tail::new(600);
    let code = run(command, None, stop, &mut |from_stderr, line| {
        if from_stderr {
            return stderr.push(line);
        }
        match parse_line(line) {
            Message::Progress(value) => progress(&value),
            Message::Result(value) => outcome = Some(Ok(value)),
            Message::Error(error) => outcome = Some(Err(error)),
            Message::Other => {}
        }
    })?;
    outcome.unwrap_or_else(|| {
        // 没有结果行就退出：助手本身坏了（缺模块、解释器不对）。标准错误的末尾有助于定位，其中没有凭据。
        let code = code.map_or("被终止".to_string(), |code| format!("退出码 {code}"));
        Err(err("helper_unavailable", format!("本机助手没有给出结果（{code}）：{}", stderr.finish())))
    })
}

/// `install`、`service`、`controller client-code` 的输出。
pub struct Captured {
    pub code: Option<i32>,
    /// 标准输出（上限 调用方给的上限（`capture_up_to` 的 `limit`））。
    pub stdout: String,
    /// 标准输出与标准错误合并后的末尾 4000 个字符。
    pub tail: String,
}

/// 运行到结束并收集输出，标准输出最多保留 `limit` 字节；超时即结束子进程并返回 `io` 错误。
pub fn capture_up_to(command: Command, timeout: Duration, limit: usize) -> Result<Captured> {
    let (mut stdout, mut tail) = (String::new(), Tail::new(4000));
    let code = run(command, Some(timeout), &|| None, &mut |from_stderr, line| {
        tail.push(line);
        if !from_stderr && stdout.len() + line.len() < limit {
            stdout.push_str(line);
            stdout.push('\n');
        }
    })?;
    Ok(Captured { code, stdout, tail: tail.finish() })
}

/// 输出里最后一行 JSON 对象（安装器先打印说明文字，最后一行才是结果）。
pub fn last_json(output: &str) -> Option<Value> {
    output.lines().rev().find_map(|line| serde_json::from_str::<Value>(line.trim()).ok().filter(Value::is_object))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_release_named_at_build_time_wins_over_leftovers() {
        let dir = std::env::temp_dir().join(format!("agent-collab-release-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        for name in ["agent-collab-0.2.0.dev3.tar.gz", "agent-collab-0.2.0.dev4.tar.gz"] {
            std::fs::write(dir.join(name), b"x").unwrap();
        }
        // 升级安装后目录里有上一版留下的发行包：按构建时的文件名取，不猜。
        assert_eq!(release_named(&dir, Some("agent-collab-0.2.0.dev4.tar.gz")), Some(dir.join("agent-collab-0.2.0.dev4.tar.gz")));
        assert_eq!(release_named(&dir, Some("agent-collab-0.2.0.dev5.tar.gz")), None);
        assert_eq!(release_named(&dir, None), None); // 没有定下文件名时，不止一个就不选
        std::fs::remove_file(dir.join("agent-collab-0.2.0.dev3.tar.gz")).unwrap();
        assert_eq!(release_named(&dir, None), Some(dir.join("agent-collab-0.2.0.dev4.tar.gz")));
        let _ = std::fs::remove_dir_all(&dir);
    }
    use serde_json::json;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("agent-collab-desktop-helper-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn touch(path: &Path) {
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, b"").unwrap();
    }

    /// 按目录现状写出 payload.json，返回它的摘要（流水线做的事，这里给测试用）。
    fn seal(resources: &Path) -> String {
        use sha2::{Digest, Sha256};
        let mut files = serde_json::Map::new();
        let mut pending = vec![resources.to_path_buf()];
        while let Some(dir) = pending.pop() {
            for entry in std::fs::read_dir(&dir).unwrap().flatten() {
                let path = entry.path();
                if path.is_dir() {
                    pending.push(path);
                } else if path.file_name().unwrap() != "payload.json" {
                    let relative = path.strip_prefix(resources).unwrap().to_string_lossy().replace('\\', "/");
                    let data = std::fs::read(&path).unwrap();
                    let digest: String = Sha256::digest(&data).iter().map(|b| format!("{b:02x}")).collect();
                    files.insert(relative, serde_json::json!({ "sha256": digest, "size": data.len() }));
                }
            }
        }
        let bytes = serde_json::to_vec(&serde_json::json!({ "schema": 1, "files": files })).unwrap();
        std::fs::write(resources.join("payload.json"), &bytes).unwrap();
        Sha256::digest(&bytes).iter().map(|b| format!("{b:02x}")).collect()
    }

    #[test]
    fn closing_the_gate_waits_for_running_helpers_and_refuses_new_ones() {
        let gate = Gate::new();
        let pass = gate.enter("install --discover").unwrap();
        let waited = std::thread::scope(|s| {
            let waiter = s.spawn(|| gate.quiesce(Duration::from_secs(5)));
            std::thread::sleep(Duration::from_millis(200));
            assert!(gate.enter("project local").is_none(), "关闸后不再放行新的助手");
            drop(pass);
            waiter.join().unwrap()
        });
        assert_eq!(waited, Ok(()));
        // 等不到：报告还在运行的子命令，并重新开闸（安装放弃，助手照常可用）
        let gate = Gate::new();
        let _busy = gate.enter("install --discover").unwrap();
        assert_eq!(gate.quiesce(Duration::from_millis(100)), Err(vec!["install --discover".to_string()]));
        assert!(gate.enter("project local").is_some());
    }

    #[test]
    fn helpers_that_fail_or_panic_leave_the_gate() {
        let gate = Gate::new();
        let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _pass = gate.enter("project local scan").unwrap();
            panic!("助手线程出错");
        }));
        assert!(outcome.is_err());
        drop(gate.enter("install --uninstall").unwrap()); // 正常结束（或返回错误）同样放下通行证
        assert_eq!(gate.quiesce(Duration::from_millis(100)), Ok(()), "计数收敛到 0，关闸不用等");
    }

    #[test]
    fn update_install_reopens_the_gate_unless_the_installer_took_over() {
        // 安装程序没能启动（或更新没有真正接管）：重新开闸，旧界面照常使用助手。
        let gate = Gate::new();
        let failed: Result<()> = gate.exclusive(Duration::from_secs(1), || Err(io("安装程序启动失败")));
        assert_eq!(failed.unwrap_err().code, "io");
        assert!(gate.enter("install --discover").is_some(), "安装失败后助手照常可用");
        // 等助手时超时：这次不安装、不结束助手，说明在等哪个操作，并重新开闸。
        let gate = Gate::new();
        let busy = gate.enter("install --status").unwrap();
        let mut ran = false;
        let waited = gate.exclusive(Duration::from_millis(100), || {
            ran = true;
            Ok(())
        });
        let error = waited.unwrap_err();
        assert_eq!(error.code, "busy");
        assert!(error.message.contains("install --status"), "{}", error.message);
        assert!(!ran, "等不到时不启动安装程序");
        drop(busy);
        assert!(gate.enter("project local").is_some(), "延后之后助手照常可用");
        // 安装程序已接管：保持关闸，直到本进程退出或重启。
        let gate = Gate::new();
        assert!(gate.exclusive(Duration::from_secs(1), || Ok(())).is_ok());
        assert!(gate.enter("project local").is_none());
    }

    #[test]
    fn labels_name_the_subcommand_without_paths() {
        let helper = Helper { python: PathBuf::from("/r/python3"), app: PathBuf::from("/a"), module: false };
        let command = helper.command(Path::new("/tmp"), &["install", "--discover", "--prefix", "/home/u/.local/share/x", "--instance", "itest"].map(OsString::from));
        assert_eq!(label(&command), "install --discover");
        let command = helper.command(Path::new("/tmp"), &["project", "local", "scan", "--root=/p"].map(OsString::from));
        assert_eq!(label(&command), "project local scan");
        let command = helper.command(Path::new("/tmp"), &["service", "status", "--prefix", "/x"].map(OsString::from));
        assert_eq!(label(&command), "service status");
    }

    #[test]
    fn payload_rejects_leftover_missing_and_changed_files() {
        let resources = scratch("payload");
        std::fs::create_dir_all(resources.join("app/agent_collab")).unwrap();
        std::fs::write(resources.join("app/agent_collab/a.py"), b"A = 1\n").unwrap();
        std::fs::write(resources.join("app/helper.py"), b"print()\n").unwrap();
        let digest = seal(&resources);
        assert_eq!(verify_payload(&resources, Some(&digest)), Ok(()));
        assert_eq!(verify_payload(&resources, None), Ok(()), "没有编进清单摘要的开发构建不核对");
        assert!(verify_payload(&resources, Some(&"0".repeat(64))).unwrap_err().contains("不对应"));
        // 上一版留下、新版已删除的模块：清单外的文件。
        std::fs::write(resources.join("app/agent_collab/old.py"), b"OLD = 1\n").unwrap();
        assert!(verify_payload(&resources, Some(&digest)).unwrap_err().contains("清单外的文件：app/agent_collab/old.py"));
        std::fs::remove_file(resources.join("app/agent_collab/old.py")).unwrap();
        std::fs::write(resources.join("app/agent_collab/a.py"), b"A = 2\n").unwrap();
        assert!(verify_payload(&resources, Some(&digest)).unwrap_err().contains("不符"));
        std::fs::remove_file(resources.join("app/agent_collab/a.py")).unwrap();
        assert!(verify_payload(&resources, Some(&digest)).unwrap_err().contains("缺少 1 个"));
        std::fs::remove_dir_all(resources).unwrap();
    }

    fn strings(args: &[OsString]) -> Vec<String> {
        args.iter().map(|arg| arg.to_string_lossy().replace('\\', "/")).collect()
    }

    #[test]
    fn interpreter_candidates_are_tried_in_order() {
        let runtime = scratch("interp");
        assert_eq!(interpreter(&runtime), None);
        touch(&runtime.join("python.exe"));
        assert_eq!(interpreter(&runtime), Some(runtime.join("python.exe")));
        touch(&runtime.join("bin/python3"));
        assert_eq!(interpreter(&runtime), Some(runtime.join("bin/python3")));
        touch(&runtime.join("python/bin/python3"));
        assert_eq!(interpreter(&runtime), Some(runtime.join("python/bin/python3")));
        // 目录不算解释器。
        let other = scratch("interp-dir");
        std::fs::create_dir_all(other.join("python.exe")).unwrap();
        assert_eq!(interpreter(&other), None);
        for dir in [runtime, other] {
            std::fs::remove_dir_all(dir).unwrap();
        }
    }

    #[test]
    fn release_is_the_single_tarball_and_carries_its_version() {
        assert_eq!(release_version(Path::new("x/agent-collab-0.9.0-rc.10.tar.gz")).as_deref(), Some("0.9.0-rc.10"));
        for bad in ["agent-collab-.tar.gz", "agent-collab-1.0.zip", "other-1.0.tar.gz", "agent-collab-1.0.tar.gz.part"] {
            assert_eq!(release_version(Path::new(bad)), None, "{bad}");
        }
        let dir = scratch("release");
        assert_eq!(release(&dir), None);
        assert_eq!(release(&dir.join("missing")), None);
        touch(&dir.join("README.txt"));
        touch(&dir.join("agent-collab-1.2.3.tar.gz"));
        assert_eq!(release(&dir), Some(dir.join("agent-collab-1.2.3.tar.gz")));
        touch(&dir.join("agent-collab-1.2.4.tar.gz"));
        assert_eq!(release(&dir), None); // 两个发行包：不猜
        std::fs::remove_dir_all(dir).unwrap();
    }

    // 启用 automation 时环境变量可以覆盖助手位置；这一条只描述随包助手的规则。
    #[cfg(not(feature = "automation"))]
    #[test]
    fn helper_needs_the_runtime_the_entry_script_and_the_package() {
        let resources = scratch("locate");
        assert!(locate(None).helper.is_none());
        assert!(locate(Some(&resources)).helper.is_none());
        // 入库的只有 helper.py：没有放入运行时与后台程序的构建，助手不可用。
        touch(&resources.join("app/helper.py"));
        let bundle = locate(Some(&resources));
        assert!(bundle.helper.is_none() && bundle.runtime.is_none() && bundle.release.is_none());
        touch(&resources.join("runtime/python/python.exe"));
        touch(&resources.join("release/agent-collab-1.0.0.tar.gz"));
        let bundle = locate(Some(&resources));
        assert_eq!(bundle.runtime, Some(resources.join("runtime")));
        assert_eq!(bundle.release, Some(resources.join("release/agent-collab-1.0.0.tar.gz")));
        assert!(bundle.helper.is_none());
        touch(&resources.join("app/agent_collab/__init__.py"));
        let helper = locate(Some(&resources)).helper.unwrap();
        assert_eq!(helper, Helper { python: resources.join("runtime/python/python.exe"), app: resources.join("app"), module: false });
        std::fs::remove_file(resources.join("app/helper.py")).unwrap();
        let bundle = locate(Some(&resources));
        assert!(bundle.helper.is_none() && bundle.runtime.is_some());
        std::fs::remove_dir_all(resources).unwrap();
    }

    #[test]
    fn automation_overrides_replace_the_interpreter_or_the_package() {
        let resources = scratch("override");
        let app = resources.join("app");
        let bundled = Helper { python: "/rt/python3".into(), app: app.clone(), module: false };
        let (python, package) = (PathBuf::from("/usr/bin/python3"), PathBuf::from("/src/repo"));
        // 两个变量都没给：用随包的助手，没有就是没有。
        assert_eq!(override_with(Some(bundled.clone()), Some(&app), None, None), Some(bundled.clone()));
        assert_eq!(override_with(None, Some(&app), None, None), None);
        // 给了包目录：以模块方式运行，不要求 helper.py；解释器没给时用随包的，随包的也没有则不可用。
        let module = |python: &str| Some(Helper { python: python.into(), app: package.clone(), module: true });
        assert_eq!(override_with(None, None, Some(python.clone()), Some(package.clone())), module("/usr/bin/python3"));
        assert_eq!(override_with(Some(bundled.clone()), Some(&app), None, Some(package.clone())), module("/rt/python3"));
        assert_eq!(override_with(None, Some(&app), None, Some(package.clone())), None);
        // 只给解释器：仍然需要随包的 helper.py。
        assert_eq!(override_with(None, Some(&app), Some(python.clone()), None), None);
        touch(&app.join("helper.py"));
        assert_eq!(override_with(None, Some(&app), Some(python.clone()), None), Some(Helper { python, app: app.clone(), module: false }));
        std::fs::remove_dir_all(resources).unwrap();
    }

    #[test]
    fn command_line_is_fixed_around_the_subcommand() {
        let args: Vec<OsString> = ["project", "local", "scan"].map(Into::into).into();
        let bundled = Helper { python: "/rt/bin/python3".into(), app: "/res/app".into(), module: false };
        assert_eq!(strings(&bundled.argv(&args)), ["-B", "-s", "-X", "utf8", "-E", "/res/app/helper.py", "project", "local", "scan"]);
        let command = bundled.command(Path::new("/data"), &args);
        assert_eq!(command.get_program(), "/rt/bin/python3");
        assert_eq!(command.get_current_dir(), Some(Path::new("/data")));
        assert_eq!(command.get_envs().count(), 0); // 不向助手的环境里加任何东西

        let module = Helper { python: "python".into(), app: "/src".into(), module: true };
        assert_eq!(strings(&module.argv(&args)), ["-B", "-s", "-X", "utf8", "-m", "agent_collab.cli", "project", "local", "scan"]);
        let command = module.command(Path::new("/data"), &args);
        let envs: Vec<_> = command.get_envs().map(|(key, value)| (key.to_os_string(), value.map(|v| v.to_os_string()))).collect();
        assert_eq!(envs, [(OsString::from("PYTHONPATH"), Some(OsString::from("/src")))]);
    }

    #[test]
    fn project_lines_are_progress_result_or_error() {
        assert_eq!(parse_line(r#"{"progress": {"phase": "scan", "files": 200}}"#), Message::Progress(json!({"phase": "scan", "files": 200})));
        assert_eq!(parse_line(r#"{"result": {"files": 3}}"#), Message::Result(json!({"files": 3})));
        assert_eq!(parse_line(r#"{"result": null}"#), Message::Result(Value::Null));
        let Message::Error(error) = parse_line(r#"{"error": {"code": "unstable", "message": "目录一直在变化", "detail": {"count": 2}}}"#) else {
            panic!("应当解析为错误");
        };
        assert_eq!((&*error.code, error.message.as_str()), ("unstable", "目录一直在变化"));
        for other in ["", "not json", "[1]", "42", r#"{"unknown": 1}"#, r#"{"progress""#] {
            assert_eq!(parse_line(other), Message::Other, "{other}");
        }
    }

    #[test]
    fn helper_error_codes_pass_through_only_when_they_look_like_codes() {
        let code = |value: Value| helper_error(&value).code.into_owned();
        for known in ["not_a_directory", "source_changed", "unfinished_application", "blob_mismatch", "no_snapshot", "io", "invalid"] {
            assert_eq!(code(json!({"code": known, "message": "m"})), known);
        }
        for odd in [json!({"code": "Bad Code"}), json!({"code": ""}), json!({"code": 7}), json!({}), json!("text"), json!({"code": "a".repeat(65)})] {
            assert_eq!(code(odd), "io");
        }
        assert_eq!(helper_error(&json!({"code": "io"})).message, "本机助手报告了错误");
        assert_eq!(helper_error(&json!({"code": "io", "message": "长".repeat(5000)})).message.chars().count(), 2000);
    }

    #[test]
    fn tail_keeps_the_last_characters() {
        assert_eq!(tail_chars("héllo", 3), "llo");
        assert_eq!(tail_chars("短", 10), "短");
        let mut tail = Tail::new(10);
        for n in 0..1000 {
            tail.push(&format!("line {n}"));
        }
        assert_eq!(tail.finish(), "8\nline 999");
        assert_eq!(Tail::new(5).finish(), "");
    }

    #[test]
    fn last_json_object_wins_over_preceding_text() {
        let output = "1. Validate\n{\"action\": \"status\"}\nInstaller: note\n{\"action\": \"stop\", \"stopped\": false, \"error\": \"x\"}\n\n";
        assert_eq!(last_json(output), Some(json!({"action": "stop", "stopped": false, "error": "x"})));
        assert_eq!(last_json("no json here\n[1, 2]\n\"text\"\n"), None);
        assert_eq!(last_json(""), None);
    }

    /// 用系统自带的命令行解释器跑一段脚本来代替助手：`lines` 是 (写到标准错误, 文本)，`tail` 是之后的原始命令。
    /// 文本只用两种解释器都不需要转义的字符。
    fn script(dir: &Path, lines: &[(bool, &str)], tail: (&str, &str)) -> Command {
        let mut command;
        if cfg!(windows) {
            let body: String = lines.iter().map(|(stderr, text)| format!("{}echo {text}\r\n", if *stderr { ">&2 " } else { "" })).collect();
            std::fs::write(dir.join("run.cmd"), format!("@echo off\r\n{body}{}\r\n", tail.0)).unwrap();
            command = Command::new("cmd");
            command.arg("/C").arg(dir.join("run.cmd"));
        } else {
            let body: String = lines.iter().map(|(stderr, text)| format!("echo '{text}'{}\n", if *stderr { " >&2" } else { "" })).collect();
            std::fs::write(dir.join("run.sh"), format!("{body}{}\n", tail.1)).unwrap();
            command = Command::new("sh");
            command.arg(dir.join("run.sh"));
        }
        command.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
        command
    }

    const EXIT_3: (&str, &str) = ("exit /b 3", "exit 3");
    const EXIT_0: (&str, &str) = ("exit /b 0", "exit 0");
    const SLEEP: (&str, &str) = ("ping -n 30 127.0.0.1 >NUL", "sleep 30");

    #[test]
    fn run_delivers_lines_from_both_streams_and_the_exit_code() {
        let dir = scratch("run");
        let mut lines = Vec::new();
        let code = run(script(&dir, &[(false, "one"), (true, "two")], EXIT_3), None, &|| None, &mut |stderr, line| {
            lines.push((stderr, line.to_string()));
        });
        assert_eq!(code, Ok(Some(3)));
        lines.sort();
        assert_eq!(lines, [(false, "one".to_string()), (true, "two".to_string())]);

        let captured = capture_up_to(script(&dir, &[(false, "1. step"), (false, r#"{"ok": true}"#), (true, "oops")], EXIT_0), Duration::from_secs(30), 1024 * 1024).unwrap();
        assert_eq!(captured.code, Some(0));
        assert_eq!(last_json(&captured.stdout), Some(json!({"ok": true})));
        assert!(!captured.stdout.contains("oops"));
        assert!(captured.tail.contains("oops") && captured.tail.contains("1. step"));
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn run_stops_the_child_on_request_and_on_timeout() {
        let dir = scratch("stop");
        let started = Instant::now();
        let stopped = run(script(&dir, &[], SLEEP), None, &|| Some(err("cancelled", "操作已取消")), &mut |_, _| {});
        assert_eq!(stopped.unwrap_err().code, "cancelled");
        let timed_out = run(script(&dir, &[], SLEEP), Some(Duration::from_millis(300)), &|| None, &mut |_, _| {});
        assert_eq!(timed_out.unwrap_err().code, "io");
        assert!(started.elapsed() < Duration::from_secs(20));

        let missing = run(Command::new("agent-collab-no-such-interpreter"), None, &|| None, &mut |_, _| {});
        assert_eq!(missing.unwrap_err().code, "helper_unavailable");
        let _ = std::fs::remove_dir_all(dir); // 被结束的脚本解释器可能还占着脚本文件
    }

    /// 用真实的解释器与 `agent_collab` 包走一遍助手协议。只在给出这两个环境变量时运行：
    /// 缺省的 `cargo test` 不依赖机器上有 Python。
    #[test]
    fn real_helper_speaks_the_expected_protocol() {
        let (Some(python), Some(app)) = (std::env::var_os("AGENT_COLLAB_TEST_HELPER_PYTHON"), std::env::var_os("AGENT_COLLAB_TEST_HELPER_APP")) else {
            return;
        };
        let helper = Helper { python: python.into(), app: app.into(), module: true };
        let dir = scratch("real");
        let (root, state) = (dir.join("项目 目录"), dir.join("state"));
        std::fs::create_dir_all(root.join("源码")).unwrap();
        std::fs::write(root.join("源码").join("主程序.py"), b"print(1)\n").unwrap();
        std::fs::write(root.join(".env"), b"SECRET=1").unwrap();
        let local = |action: &str, extra: &[&Path]| {
            let mut args: Vec<OsString> = ["project", "local", action, "--state"].map(Into::into).into();
            args.push(state.clone().into());
            args.extend(extra.iter().map(|arg| OsString::from(*arg)));
            project(helper.command(&dir, &args), &|| None, &mut |_| {})
        };

        let scanned = local("scan", &[Path::new("--root"), &root, Path::new("--project"), Path::new("11111111-2222-4333-8444-555555555555")]).unwrap();
        assert_eq!((scanned["files"].clone(), scanned["excluded"]["count"].clone()), (json!(1), json!(1)));
        assert_eq!(scanned["manifest"]["files"][0]["path"], "源码/主程序.py"); // 非 ASCII 路径按 UTF-8 读回
        assert!(scanned["root"].as_str().unwrap().ends_with("项目 目录"));
        assert!(scanned["exclude"].as_array().unwrap().iter().any(|rule| rule == ".env"));
        let snapshot = state.join("snapshots").join(format!("{}.json", scanned["snapshot_id"].as_str().unwrap()));
        let saved: Value = serde_json::from_slice(&std::fs::read(snapshot).unwrap()).unwrap();
        assert_eq!(saved["project_id"], "11111111-2222-4333-8444-555555555555");
        assert!(saved["manifest_bytes"].as_u64().unwrap() > 0 && saved["scanned_at"].is_string());

        assert_eq!(local("unfinished", &[Path::new("--root"), &root]), Ok(json!({ "unfinished": [] })));
        // 助手的错误码原样透出。
        assert_eq!(local("scan", &[Path::new("--root"), &dir.join("missing")]).unwrap_err().code, "not_a_directory");
        assert_eq!(local("recover", &[Path::new("--application"), Path::new("11111111-2222-4333-8444-555555555555")]).unwrap_err().code, "no_application");
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn project_run_returns_the_result_line_or_the_error_line() {
        let dir = scratch("project");
        let mut phases = Vec::new();
        let lines = [(false, r#"{"progress": {"phase": "scan"}}"#), (true, "warning"), (false, r#"{"result": {"files": 1}}"#)];
        let ok = project(script(&dir, &lines, EXIT_0), &|| None, &mut |value| phases.push(value["phase"].clone()));
        assert_eq!(ok, Ok(json!({"files": 1})));
        assert_eq!(phases, [json!("scan")]);
        let failed = project(script(&dir, &[(false, r#"{"error": {"code": "unstable", "message": "m"}}"#)], EXIT_3), &|| None, &mut |_| {});
        assert_eq!(failed.unwrap_err().code, "unstable");
        // 没有结果行：助手本身坏了。
        let broken = project(script(&dir, &[(true, "Traceback")], EXIT_3), &|| None, &mut |_| {}).unwrap_err();
        assert_eq!(broken.code, "helper_unavailable");
        assert!(broken.message.contains("退出码 3") && broken.message.contains("Traceback"));
        std::fs::remove_dir_all(dir).unwrap();
    }
}
