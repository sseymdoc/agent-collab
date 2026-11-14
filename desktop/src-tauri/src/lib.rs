//! Agent Collab 桌面外壳。契约见 `docs/desktop.md`。
//!
//! 外壳保管客户端凭据、代发 controller 请求、转发事件流、把工件存到用户选定的位置、发系统通知；
//! 项目目录的读写（`projects`）与本机部署（`local`）交给随包的本机助手（`helper`）。
//! 凭据只存在于系统凭据存储和本进程内存里的 [`Token`]，不进入返回值、事件、日志、`connections.json`，
//! 也不进入助手的参数、环境与文件。

mod dialog;
mod helper;
mod local;
mod projects;
mod sse;
mod tls;
mod update;
mod validate;

use std::borrow::Cow;
use std::collections::HashMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use reqwest::header::{HeaderValue, ACCEPT, CONTENT_LENGTH, CONTENT_TYPE};
use reqwest::StatusCode;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Emitter, Manager, State, WebviewWindow};
use validate::API_PREFIX as API;

/// 系统凭据存储里的服务名，按构建身份的实例分开（ADR 0004 第 1 条）：正式版 `agent-collab`，
/// 候选版 `agent-collab-cand`，内部测试版 `agent-collab-itest`。不同身份互相读不到对方的凭据。
fn service_name() -> String {
    local::instance_name(local::INSTANCE)
}
const MAX_BODY: usize = 16 * 1024 * 1024;
/// 60 秒没有收到任何字节（含 SSE 心跳）即认为连接停滞。
const IDLE: Duration = Duration::from_secs(60);

// ---------------------------------------------------------------- 错误

/// 命令失败时返回给页面的 `{"code", "message"}`。
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct BridgeError {
    /// 外壳自己的错误码都是固定的；本机助手返回的错误码原样透出，所以不是 `&'static str`。
    code: Cow<'static, str>,
    message: String,
}

type Result<T> = std::result::Result<T, BridgeError>;

fn err(code: &'static str, message: impl Into<String>) -> BridgeError {
    BridgeError { code: Cow::Borrowed(code), message: message.into() }
}

fn invalid(message: impl Into<String>) -> BridgeError {
    err("invalid", message)
}

fn io(e: impl std::fmt::Display) -> BridgeError {
    err("io", e.to_string())
}

fn stale() -> BridgeError {
    err("stale_connection", "连接已切换或关闭，这次请求被丢弃")
}

fn mismatch() -> BridgeError {
    err("controller_mismatch", "响应的 X-Controller-Id 与连接绑定的 controller 不一致，连接已关闭")
}

fn bad_response(what: &str) -> BridgeError {
    err("network", format!("controller 的{what}响应格式无效"))
}

fn net(e: reqwest::Error) -> BridgeError {
    // reqwest 的错误链只有 URL 与底层原因，不含请求头。
    let mut message = e.to_string();
    let mut source = std::error::Error::source(&e);
    while let Some(s) = source {
        message.push_str(": ");
        message.push_str(&s.to_string());
        source = s.source();
    }
    err("network", message)
}

/// controller 返回非预期状态码时的错误；只引用它的 `error.message`，不回显响应体。
fn http_error(what: &str, status: StatusCode, body: &[u8]) -> BridgeError {
    let detail = serde_json::from_slice::<Value>(body)
        .ok()
        .and_then(|v| v["error"]["message"].as_str().map(|m| format!("：{}", m.chars().take(300).collect::<String>())))
        .unwrap_or_default();
    // 401：配对码无效或过期，或这条连接的凭据已被撤销；页面据此提示重新配对。
    // 403：凭据有效但没有这项操作的权限；连接与本机凭据都保留，不能当作认证失效处理。
    let code = if status == StatusCode::UNAUTHORIZED {
        "unauthorized"
    } else if status == StatusCode::FORBIDDEN {
        "forbidden"
    } else if status.is_client_error() {
        "invalid"
    } else {
        "network"
    };
    err(code, format!("{what}（HTTP {}）{detail}", status.as_u16()))
}

fn helper_unavailable() -> BridgeError {
    err("helper_unavailable", "本机助手不可用：安装包不完整，或随包载荷核对没有通过（窗口底部的版本信息里有原因）；重新运行安装程序修复")
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn storage_err(e: keyring::Error) -> BridgeError {
    // 不直接格式化 keyring::Error：BadEncoding、Ambiguous 变体里带着凭据内容。
    let detail = match e {
        keyring::Error::NoEntry => "没有找到这条连接的凭据".to_string(),
        keyring::Error::PlatformFailure(e) | keyring::Error::NoStorageAccess(e) => e.to_string(),
        keyring::Error::TooLong(name, limit) => format!("{name} 超过长度上限 {limit}"),
        _ => "读写失败".to_string(),
    };
    err("secure_storage_unavailable", format!("系统凭据存储不可用：{detail}"))
}

// ---------------------------------------------------------------- 状态

/// 客户端凭据。故意不实现 Debug / Serialize / Clone。
struct Token(String);

/// 当前连接：地址、绑定的 controller、内存中的凭据，以及按它的信任来源构造的 HTTP 客户端。
struct Active {
    id: String,
    base: String,
    controller_id: String,
    token: Token,
    http: tls::Http,
}

/// 连接代次与事件流的状态机。不含 I/O，由单元测试覆盖。
#[derive(Default)]
struct Inner {
    /// 连接代次，单调递增；每次打开或关闭连接加一。
    epoch: u64,
    active: Option<Arc<Active>>,
    /// 事件流代次；每次替换或关闭事件流加一，旧流据此停止发事件。
    stream_gen: u64,
    stream: Option<tauri::async_runtime::JoinHandle<()>>,
}

impl Inner {
    /// 关闭当前连接：代次加一，丢弃内存中的凭据，中止事件流。返回新的代次。
    fn bump(&mut self) -> u64 {
        self.epoch += 1;
        self.active = None;
        self.stop_stream();
        self.epoch
    }

    fn stop_stream(&mut self) {
        self.stream_gen += 1;
        if let Some(task) = self.stream.take() {
            task.abort();
        }
    }

    /// 取 `epoch` 对应的连接；它不是当前代次（或已没有连接）时返回 `stale_connection`。
    fn current(&self, epoch: u64) -> Result<Arc<Active>> {
        match &self.active {
            Some(active) if self.epoch == epoch => Ok(active.clone()),
            _ => Err(stale()),
        }
    }

    /// 响应到达后的核对：代次未变，且 `X-Controller-Id` 等于绑定值；不等则关闭连接。
    fn verify(&mut self, epoch: u64, controller_header: Option<&str>) -> Result<()> {
        let active = self.current(epoch)?;
        if controller_header != Some(active.controller_id.as_str()) {
            self.bump();
            return Err(mismatch());
        }
        Ok(())
    }

    /// 页面关闭它持有的那一代连接（包括还在核对、尚未成为当前连接的那次打开）。
    /// 旧页面状态晚到的关闭不会关掉已经切换过去的新连接。
    fn close(&mut self, epoch: u64) -> Result<()> {
        if self.epoch != epoch {
            return Err(stale());
        }
        self.bump();
        Ok(())
    }

    fn close_if_active(&mut self, id: &str) {
        if self.active.as_ref().is_some_and(|a| a.id == id) {
            self.bump();
        }
    }
}

struct Shell {
    inner: Mutex<Inner>,
    /// 配对逐个进行：查重、写凭据、保存连接三步之间不能插入另一次配对。
    pairing: tokio::sync::Mutex<()>,
    /// 信任系统根证书的客户端：没有指定 CA 的连接都用它。
    http: tls::Http,
    /// 同一时间只运行一项项目操作。
    ops: projects::Ops,
    /// 本机部署的安装与服务启停逐个进行。
    deploying: tokio::sync::Mutex<()>,
    /// 进行中的备份导出数：导出期间不安装桌面端更新（安装会重启应用，导出随之中断）。
    exports: std::sync::atomic::AtomicUsize,
    /// 桌面端更新的安装已经开始：不再接受新的备份导出。
    installing: std::sync::atomic::AtomicBool,
}

/// 备份导出期间计数，离开作用域即减一。安装已开始时拒绝。
/// 与 `Installing` 都先登记自己、再看对方（SeqCst），两边同时开始时至少一边会看到另一边。
struct Exporting<'a>(&'a std::sync::atomic::AtomicUsize);

impl<'a> Exporting<'a> {
    fn start(shell: &'a Shell) -> Result<Self> {
        Self::admit(&shell.exports, &shell.installing)
    }

    fn admit(exports: &'a std::sync::atomic::AtomicUsize, installing: &std::sync::atomic::AtomicBool) -> Result<Self> {
        exports.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        let guard = Self(exports);
        if installing.load(std::sync::atomic::Ordering::SeqCst) {
            return Err(err("busy", "桌面端更新正在安装，稍后再导出备份"));
        }
        Ok(guard)
    }
}

impl Drop for Exporting<'_> {
    fn drop(&mut self) {
        self.0.fetch_sub(1, std::sync::atomic::Ordering::SeqCst);
    }
}

/// 安装更新期间关闭备份导出的准入，离开作用域（安装没有进行下去）即重新打开。
pub(crate) struct Installing<'a>(&'a std::sync::atomic::AtomicBool);

impl<'a> Installing<'a> {
    pub(crate) fn start(shell: &'a Shell) -> Self {
        Self::close(&shell.installing)
    }

    fn close(installing: &'a std::sync::atomic::AtomicBool) -> Self {
        installing.store(true, std::sync::atomic::Ordering::SeqCst);
        Self(installing)
    }
}

impl Drop for Installing<'_> {
    fn drop(&mut self) {
        self.0.store(false, std::sync::atomic::Ordering::SeqCst);
    }
}

impl Shell {
    fn new() -> std::result::Result<Self, String> {
        Ok(Self {
            inner: Mutex::default(),
            pairing: tokio::sync::Mutex::new(()),
            http: tls::http(None)?,
            ops: projects::Ops::default(),
            deploying: tokio::sync::Mutex::new(()),
            exports: std::sync::atomic::AtomicUsize::new(0),
            installing: std::sync::atomic::AtomicBool::new(false),
        })
    }

    /// 状态锁。只在同步代码段里持有，不跨 await。
    fn lock(&self) -> MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// 这条连接的 HTTP 客户端。指定了 CA 的连接只信任记录里的那份证书；
    /// 记录里的证书缺失或损坏时报错，不退回系统根证书。
    fn http_for(&self, conn: &Connection) -> Result<tls::Http> {
        if !conn.custom_ca {
            return Ok(self.http.clone());
        }
        let pem = conn.ca_pem.as_deref().ok_or_else(|| io("连接记录里没有它的 CA 证书，请忘记这条连接后重新配对"))?;
        tls::http(Some(pem)).map_err(io)
    }

    /// 以当前连接的凭据发出请求，并在响应返回时核对代次与 `X-Controller-Id`。
    /// 请求期间代次变了，结果（包括错误）一律丢弃。
    async fn send(&self, epoch: u64, active: &Active, request: reqwest::RequestBuilder) -> Result<reqwest::Response> {
        let sent = request.bearer_auth(&active.token.0).send().await;
        let mut inner = self.lock();
        inner.current(epoch)?;
        let response = sent.map_err(net)?;
        let header = controller_header(&response);
        if from_proxy(header.as_deref(), response.status()) {
            return Err(proxy_error(response.status()));
        }
        inner.verify(epoch, header.as_deref())?;
        Ok(response)
    }

    /// 用凭据调用 `DELETE /client` 自我撤销，返回是否确认成功。
    async fn revoke(&self, http: &reqwest::Client, base: &str, controller_id: Option<&str>, token: &Token) -> bool {
        match http.delete(format!("{base}{API}/client")).bearer_auth(&token.0).send().await {
            Ok(r) => r.status().is_success() && controller_id.is_none_or(|id| controller_header(&r).as_deref() == Some(id)),
            Err(_) => false,
        }
    }

    /// 配对成功后接管凭据：先写入系统凭据存储，再保存连接记录。
    async fn adopt(&self, app: &AppHandle, conn: &Connection, controller_header: Option<&str>, token: &Token) -> Result<()> {
        if !validate::ident(&conn.controller_id) || !validate::ident(&conn.client_id) || !validate::token(&token.0) {
            return Err(bad_response("配对"));
        }
        if controller_header != Some(conn.controller_id.as_str()) {
            return Err(err("controller_mismatch", "响应的 X-Controller-Id 与配对结果里的 controller_id 不一致"));
        }
        let _serial = self.pairing.lock().await;
        if load(app, &self.lock())?.iter().any(|c| c.id == conn.id) {
            return Err(invalid("已经存在 client_id 相同的连接"));
        }
        let secret = Token(token.0.clone());
        credential(conn, move |entry| entry.set_password(&secret.0)).await?;
        let saved = {
            let inner = self.lock();
            load(app, &inner).and_then(|mut list| {
                list.push(conn.clone());
                save(app, &inner, &list)
            })
        };
        if saved.is_err() {
            let _ = credential(conn, |entry| entry.delete_credential()).await;
        }
        saved
    }
}

fn controller_header(response: &reqwest::Response) -> Option<String> {
    response.headers().get("x-controller-id")?.to_str().ok().map(str::to_owned)
}

/// 没带身份头的 5xx 来自 controller 前面的反向代理（controller 重启期间的 502/503/504）：
/// 按暂时不可达处理，不关闭连接。带了头但不一致，或其他状态码缺头，仍判为不是同一个 controller。
/// 所有核对 `X-Controller-Id` 的地方都先过这一关，行为才一致。
fn from_proxy(header: Option<&str>, status: StatusCode) -> bool {
    header.is_none() && status.is_server_error()
}

fn proxy_error(status: StatusCode) -> BridgeError {
    err("network", format!("controller 暂时不可达（HTTP {}）", status.as_u16()))
}

/// 读完整个响应体，上限 16 MiB。
async fn read_body(mut response: reqwest::Response) -> Result<Vec<u8>> {
    let mut body = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(net)? {
        if body.len() + chunk.len() > MAX_BODY {
            return Err(err("network", "响应体超过 16 MiB 上限"));
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

// ---------------------------------------------------------------- 连接记录与凭据

/// `connections.json` 里的一条连接。只有非机密信息。
#[derive(Clone, Serialize, Deserialize)]
struct Connection {
    id: String,
    name: String,
    url: String,
    controller_id: String,
    client_id: String,
    kind: String,
    scopes: Vec<String>,
    paired_at: String,
    /// 已在 controller 上确认撤销、但本机凭据还没删掉的连接（等待再次“忘记”来清理）。旧文件里没有这个字段。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    revoked_at: Option<String>,
    /// 这条连接只信任配对时指定的 CA，系统根证书对它不生效。旧文件里没有这三个字段。
    #[serde(default)]
    custom_ca: bool,
    /// 首张 CA 证书 DER 的 SHA-256。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    ca_fingerprint: Option<String>,
    /// CA 证书（PEM，公开信息）。只保存在连接记录里，不返回给页面。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    ca_pem: Option<String>,
}

impl Connection {
    /// 返回给页面的形状：去掉证书正文，页面只需要指纹。
    fn view(&self) -> Value {
        let mut value = json!(self);
        if let Some(fields) = value.as_object_mut() {
            fields.remove("ca_pem");
        }
        value
    }
}

#[derive(Deserialize)]
struct StoreFile {
    connections: Vec<Connection>,
}

fn store_path(app: &AppHandle) -> Result<PathBuf> {
    Ok(app.path().app_config_dir().map_err(io)?.join("connections.json"))
}

/// 读取连接记录。`_held` 表示调用方持有状态锁：对文件的读写都在这把锁内串行。
fn load(app: &AppHandle, _held: &Inner) -> Result<Vec<Connection>> {
    match std::fs::read(store_path(app)?) {
        Ok(bytes) => serde_json::from_slice::<StoreFile>(&bytes)
            .map(|file| file.connections)
            .map_err(|e| io(format!("connections.json 无法解析：{e}"))),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Vec::new()),
        Err(e) => Err(io(format!("读取 connections.json 失败：{e}"))),
    }
}

fn save(app: &AppHandle, _held: &Inner, connections: &[Connection]) -> Result<()> {
    write_json(&store_path(app)?, &json!({ "connections": connections }))
}

/// 写一份 JSON 记录：先写同目录的临时文件再改名，读的一方看不到写了一半的文件。
fn write_json(path: &Path, value: &impl Serialize) -> Result<()> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(io)?;
    }
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, serde_json::to_vec_pretty(value).map_err(io)?).map_err(io)?;
    std::fs::rename(&tmp, path).map_err(io)
}

/// 应用本机数据目录：助手的工作目录，项目操作的状态与暂存文件在它下面的 `projects/`。
fn data_dir(app: &AppHandle) -> Result<PathBuf> {
    let dir = app.path().app_local_data_dir().map_err(io)?;
    std::fs::create_dir_all(&dir).map_err(io)?;
    Ok(dir)
}

/// 随包的本机助手，在应用资源目录的 `resources/` 下（见 `docs/desktop.md`“构建”）。
fn bundle(app: &AppHandle) -> helper::Bundle {
    helper::locate(app.path().resource_dir().ok().map(|dir| dir.join("resources")).as_deref())
}

/// 这台设备的名称：写进基线来源与应用记录，也用作本机配对时的客户端名称。
fn device_name() -> String {
    static NAME: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    NAME.get_or_init(|| {
        let name = if cfg!(windows) {
            std::env::var("COMPUTERNAME").ok()
        } else {
            let output = std::process::Command::new("/bin/hostname").stdin(std::process::Stdio::null()).output().ok();
            output.filter(|o| o.status.success()).and_then(|o| String::from_utf8(o.stdout).ok())
        };
        // controller 要求设备名非空且不超过 120 个字符。
        let name: String = name.unwrap_or_default().trim().chars().filter(|c| !c.is_control()).take(120).collect();
        if name.is_empty() { "desktop".into() } else { name }
    })
    .clone()
}

/// 同一配置目录只允许一个实例：`connections.json` 的读-改-写、它的临时文件和凭据增删只在进程内串行，
/// 两个实例同时配对或忘记会互相覆盖记录。锁在进程退出时由系统释放，崩溃也不会留下死锁。
/// 已被别的实例持有时返回 None。
fn instance_lock(dir: &Path) -> std::io::Result<Option<std::fs::File>> {
    std::fs::create_dir_all(dir)?;
    let file = std::fs::OpenOptions::new().create(true).write(true).truncate(false).open(dir.join("instance.lock"))?;
    match file.try_lock() {
        Ok(()) => Ok(Some(file)),
        Err(std::fs::TryLockError::WouldBlock) => Ok(None),
        Err(std::fs::TryLockError::Error(e)) => Err(e),
    }
}

fn find(app: &AppHandle, held: &Inner, id: &str) -> Result<Connection> {
    load(app, held)?.into_iter().find(|c| c.id == id).ok_or_else(|| err("not_found", "没有这条连接"))
}

/// 在系统凭据存储里对这条连接的凭据做一次操作（服务名 `agent-collab`，账户名 `<controller_id>/<client_id>`）。
/// 钥匙串可能弹窗等待用户，所以放到阻塞线程里。
async fn credential<T: Send + 'static>(
    conn: &Connection,
    op: impl FnOnce(&keyring::Entry) -> keyring::Result<T> + Send + 'static,
) -> Result<T> {
    let account = format!("{}/{}", conn.controller_id, conn.client_id);
    tauri::async_runtime::spawn_blocking(move || keyring::Entry::new(&service_name(), &account).and_then(|entry| op(&entry)))
        .await
        .map_err(|_| err("secure_storage_unavailable", "系统凭据存储不可用：操作异常结束"))?
        .map_err(storage_err)
}

fn now_iso() -> String {
    let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default();
    iso8601(now.as_secs(), now.subsec_millis())
}

/// UTC ISO-8601（`2026-11-09T03:48:38.123Z`）。日期换算用 Howard Hinnant 的 civil_from_days。
fn iso8601(secs: u64, millis: u32) -> String {
    let (days, rem) = ((secs / 86_400) as i64 + 719_468, secs % 86_400);
    let (era, doe) = (days.div_euclid(146_097), days.rem_euclid(146_097));
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!("{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{millis:03}Z", rem / 3600, rem % 3600 / 60, rem % 60)
}

// ---------------------------------------------------------------- 命令：身份与连接

#[tauri::command]
fn app_info(app: AppHandle) -> Value {
    let helper = bundle(&app);
    json!({
        "version": app.package_info().version.to_string(),
        // 构建时由环境变量 AGENT_COLLAB_COMMIT 传入；未设置为 null。
        "commit": option_env!("AGENT_COLLAB_COMMIT"),
        "platform": std::env::consts::OS,
        "arch": match std::env::consts::ARCH { "aarch64" => "arm64", other => other },
        // 构建身份（ADR 0004）：formal、candidate、internal-test；没有指定的开发构建为 dev。只有 formal 是正式发行版。
        "kind": update::KIND,
        "candidate": update::KIND != "formal" || cfg!(feature = "automation") || app.package_info().version.to_string().contains('-'),
        // 内部测试版：编译进了自动化钩子（cargo feature `automation`），不对外分发。
        "automation": cfg!(feature = "automation"),
        "helper": helper.helper.is_some(),
        // 随包载荷核对：通过为 true；没通过时 payload_error 给出原因（缺文件、清单外的残留文件、内容不符）。
        "payload": helper.payload.is_ok(),
        "payload_error": helper.payload.err(),
        "identifier": app.config().identifier,
        "credential_service": service_name(),
        // 本机部署的实例名；正式版为空串。
        "instance": local::INSTANCE,
    })
}

#[tauri::command]
async fn connections_list(app: AppHandle, shell: State<'_, Shell>) -> Result<Value> {
    let inner = shell.lock();
    let connections: Vec<Value> = load(&app, &inner)?.iter().map(Connection::view).collect();
    let active = inner.active.as_ref().map(|a| json!({ "id": a.id, "epoch": inner.epoch }));
    Ok(json!({ "connections": connections, "active": active }))
}

/// `POST /clients/pair` 的响应。不派生 Debug：里面有凭据。
#[derive(Deserialize)]
struct Pairing {
    client_id: String,
    client_token: String,
    controller_id: String,
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    kind: Option<String>,
    #[serde(default)]
    scopes: Vec<String>,
}

/// CA 证书文件的大小上限；一份证书链只有几 KB。
const MAX_CA_FILE: u64 = 1024 * 1024;

#[tauri::command(rename_all = "snake_case")]
async fn connection_pair(
    window: WebviewWindow,
    app: AppHandle,
    shell: State<'_, Shell>,
    url: String,
    code: String,
    name: String,
    custom_ca: Option<bool>,
) -> Result<Value> {
    let base = validate::base_url(&url).map_err(invalid)?;
    if code.trim().is_empty() {
        return Err(invalid("配对码不能为空"));
    }
    let ca = if custom_ca == Some(true) {
        // 取消选取则不配对，配对码不消耗。证书只能来自这个对话框，页面不能提供路径或内容。
        let Some(path) = dialog::pick_file(&window, "选择 CA 证书", "PEM 证书", &["pem", "crt", "cer"]).await? else {
            return Ok(json!({ "cancelled": true }));
        };
        if std::fs::metadata(&path).map_err(io)?.len() > MAX_CA_FILE {
            return Err(invalid("所选文件太大，不是 CA 证书"));
        }
        Some(tls::Ca::parse(&std::fs::read(&path).map_err(io)?).map_err(invalid)?)
    } else {
        None
    };
    pair(&app, &shell, base, &code, &name, ca).await.map(|conn| conn.view())
}

/// 用配对码换取凭据并保存为一条连接。`connection_pair` 与 `local_pair` 共用。
async fn pair(app: &AppHandle, shell: &Shell, base: String, code: &str, name: &str, ca: Option<tls::Ca>) -> Result<Connection> {
    let (code, name) = (code.trim(), name.trim());
    let http = match &ca {
        Some(ca) => tls::http(Some(&ca.pem)).map_err(invalid)?,
        None => shell.http.clone(),
    };
    let response = http
        .api
        .post(format!("{base}{API}/clients/pair"))
        .header(CONTENT_TYPE, "application/json")
        .body(json!({ "code": code, "name": name, "kind": "desktop" }).to_string())
        .send()
        .await
        .map_err(net)?;
    let (status, header) = (response.status(), controller_header(&response));
    let body = read_body(response).await?;
    if status != StatusCode::OK {
        return Err(http_error("配对失败", status, &body));
    }
    // 解析失败时不引用 serde 的错误和响应体：里面可能有凭据。
    let pairing: Pairing = serde_json::from_slice(&body).map_err(|_| bad_response("配对"))?;
    drop(body);
    let token = Token(pairing.client_token);
    let conn = Connection {
        id: pairing.client_id.clone(),
        name: pairing.name.filter(|n| !n.is_empty()).unwrap_or_else(|| name.to_string()),
        url: base.clone(),
        controller_id: pairing.controller_id,
        client_id: pairing.client_id,
        kind: pairing.kind.unwrap_or_else(|| "desktop".into()),
        scopes: pairing.scopes,
        paired_at: now_iso(),
        revoked_at: None,
        custom_ca: ca.is_some(),
        ca_fingerprint: ca.as_ref().map(|ca| ca.fingerprint.clone()),
        ca_pem: ca.map(|ca| ca.pem),
    };
    // 从这里起手里有一份已签发的凭据：之后任何一步失败，都尽力用它自我撤销，不留下无人保管的凭据。
    let adopted = shell.adopt(app, &conn, header.as_deref(), &token).await;
    if adopted.is_err() {
        shell.revoke(&http.api, &base, None, &token).await;
    }
    adopted.map(|()| conn)
}

#[tauri::command]
async fn connection_open(app: AppHandle, shell: State<'_, Shell>, id: String) -> Result<Value> {
    // 打开即先关闭旧连接：核对新连接期间，旧代次的请求和事件流已经失效。
    let (conn, epoch) = {
        let mut inner = shell.lock();
        let conn = find(&app, &inner, &id)?;
        if conn.revoked_at.is_some() {
            // 凭据已在 controller 上失效，打开只会得到 401；不动当前连接，提示用户完成清理。
            return Err(err("unauthorized", "这条连接已在 controller 上撤销，请再次“忘记”以清理本机凭据"));
        }
        (conn, inner.bump())
    };
    let base = validate::base_url(&conn.url).map_err(invalid)?;
    let http = shell.http_for(&conn)?;
    let token = Token(credential(&conn, |entry| entry.get_password()).await?);
    let response = http.api.get(format!("{base}{API}/client")).bearer_auth(&token.0).send().await.map_err(net)?;
    let status = response.status();
    let header = controller_header(&response);
    if from_proxy(header.as_deref(), status) {
        return Err(proxy_error(status));
    }
    if header.as_deref() != Some(conn.controller_id.as_str()) {
        return Err(mismatch());
    }
    let body = read_body(response).await?;
    if status != StatusCode::OK {
        return Err(http_error("打开连接失败", status, &body));
    }
    let mut info: Value = serde_json::from_slice(&body).map_err(|_| bad_response(" /client "))?;
    if info["controller"]["controller_id"].as_str() != Some(conn.controller_id.as_str()) {
        return Err(mismatch());
    }
    let mut inner = shell.lock();
    if inner.epoch != epoch {
        return Err(stale());
    }
    find(&app, &inner, &id)?; // 核对期间这条连接可能已被忘掉
    inner.active = Some(Arc::new(Active { id, base, controller_id: conn.controller_id, token, http }));
    Ok(json!({ "epoch": epoch, "identity": info["identity"].take(), "controller": info["controller"].take() }))
}

#[tauri::command]
fn connection_close(shell: State<'_, Shell>, epoch: u64) -> Result<Value> {
    shell.lock().close(epoch)?;
    Ok(json!({}))
}

#[tauri::command]
async fn connection_forget(app: AppHandle, shell: State<'_, Shell>, id: String, revoke: bool) -> Result<Value> {
    let mut conn = {
        let mut inner = shell.lock();
        let conn = find(&app, &inner, &id)?;
        inner.close_if_active(&id);
        conn
    };
    // 上次已确认撤销、只是本机凭据没删掉：这次不再撤销（凭据已失效，重试只会失败），如实报告已撤销。
    let mut revoked = conn.revoked_at.is_some();
    if revoke && !revoked {
        // 撤销失败（包括读不到凭据）不阻止本地删除，结果如实返回。
        if let (Ok(base), Ok(http), Ok(secret)) =
            (validate::base_url(&conn.url), shell.http_for(&conn), credential(&conn, |entry| entry.get_password()).await)
        {
            revoked = shell.revoke(&http.api, &base, Some(&conn.controller_id), &Token(secret)).await;
        }
    }
    // 先删连接记录、再删凭据，凭据删不掉时把记录放回：任何一步失败，列表里都留着这条连接，可以再次“忘记”。
    // 反过来的顺序会在保存失败时留下一条看得见、却找不到凭据的连接。放回的记录若已撤销，带上 revoked_at。
    {
        let mut inner = shell.lock();
        let remaining: Vec<Connection> = load(&app, &inner)?.into_iter().filter(|c| c.id != id).collect();
        save(&app, &inner, &remaining)?;
        inner.close_if_active(&id); // 删除期间它可能刚被打开
    }
    let deleted = credential(&conn, |entry| match entry.delete_credential() {
        Err(keyring::Error::NoEntry) => Ok(()), // 凭据本来就不在
        other => other,
    })
    .await;
    if deleted.is_err() {
        if revoked && conn.revoked_at.is_none() {
            conn.revoked_at = Some(now_iso());
        }
        let inner = shell.lock();
        let _ = load(&app, &inner).and_then(|mut list| {
            if !list.iter().any(|c| c.id == conn.id) {
                list.push(conn.clone());
            }
            save(&app, &inner, &list)
        });
    }
    deleted?;
    Ok(json!({ "revoked": revoked }))
}

// ---------------------------------------------------------------- 命令：请求代发

#[tauri::command]
async fn api_request(
    shell: State<'_, Shell>,
    epoch: u64,
    method: String,
    path: String,
    body: Option<String>,
    headers: Option<HashMap<String, String>>,
) -> Result<Value> {
    let active = shell.lock().current(epoch)?;
    if !validate::method(&method) {
        return Err(invalid("不支持的请求方法"));
    }
    let method_name = method.to_ascii_uppercase();
    let method = reqwest::Method::from_bytes(method.as_bytes()).map_err(|_| invalid("不支持的请求方法"))?;
    let url = validate::api_url(&active.base, &path).map_err(invalid)?;
    let mut request = active.http.api.request(method, url);
    // 页面给的请求头只转发 X-Coordinator-Token。
    if let Some((_, value)) = headers.iter().flatten().find(|(k, _)| k.eq_ignore_ascii_case("x-coordinator-token")) {
        let mut value = HeaderValue::from_str(value).map_err(|_| invalid("X-Coordinator-Token 的值无效"))?;
        value.set_sensitive(true);
        request = request.header("x-coordinator-token", value);
    }
    match body {
        Some(body) => request = request.header(CONTENT_TYPE, "application/json").body(body),
        // 没有请求体的 POST/PUT 也声明长度 0：HTTP 客户端对空体不加 Content-Length，controller 会拒绝。
        None if matches!(method_name.as_str(), "POST" | "PUT" | "PATCH") => request = request.header(CONTENT_LENGTH, 0).body(Vec::new()),
        None => {}
    }
    let response = shell.send(epoch, &active, request).await?;
    let status = response.status().as_u16();
    let body = read_body(response).await;
    shell.lock().current(epoch)?; // 读响应体期间代次变了，同样丢弃
    let body = body?;
    Ok(json!({ "status": status, "body": String::from_utf8_lossy(&body) }))
}

// ---------------------------------------------------------------- 命令：事件流

/// 事件 `ac-sse` 的负载。
#[derive(Clone, Serialize)]
struct SsePayload {
    epoch: u64,
    kind: &'static str,
    id: Option<u64>,
    #[serde(rename = "type")]
    ty: Option<String>,
    data: Option<String>,
}

struct Stream {
    app: AppHandle,
    epoch: u64,
    gen: u64,
}

impl Stream {
    /// 仅当这条流仍是当前流时才发事件。检查与发送在同一把锁内，
    /// 所以流被替换或关闭之后不会再有它的事件到达页面。返回是否已发出。
    fn emit(&self, kind: &'static str, id: Option<u64>, ty: Option<String>, data: Option<String>) -> bool {
        let shell = self.app.state::<Shell>();
        let inner = shell.lock();
        if inner.epoch != self.epoch || inner.stream_gen != self.gen {
            return false;
        }
        let _ = self.app.emit_to("main", "ac-sse", SsePayload { epoch: self.epoch, kind, id, ty, data });
        true
    }

    fn closed(&self, reason: impl Into<String>) {
        self.emit("closed", None, None, Some(reason.into()));
    }

    async fn run(self, active: Arc<Active>, cursor: Option<u64>) {
        let shell = self.app.state::<Shell>();
        let mut url = match validate::api_url(&active.base, "/events/stream") {
            Ok(url) => url,
            Err(e) => return self.closed(e),
        };
        if let Some(cursor) = cursor {
            url.query_pairs_mut().append_pair("cursor", &cursor.to_string());
        }
        let request = active.http.api.get(url).header(ACCEPT, "text/event-stream").bearer_auth(&active.token.0);
        let mut response = match request.send().await {
            Ok(response) => response,
            Err(e) => return self.closed(net(e).message),
        };
        let header = controller_header(&response);
        if from_proxy(header.as_deref(), response.status()) {
            return self.closed(format!("http_{}", response.status().as_u16()));
        }
        if header.as_deref() != Some(active.controller_id.as_str()) {
            self.closed("controller_mismatch");
            let _ = shell.lock().verify(self.epoch, header.as_deref());
            return;
        }
        match response.status() {
            StatusCode::OK => {}
            StatusCode::UNAUTHORIZED => {
                self.emit("unauthorized", None, None, None);
                return;
            }
            // 没有读取事件的权限：凭据仍然有效，页面停止重连但不离开这条连接。
            StatusCode::FORBIDDEN => {
                self.emit("forbidden", None, None, None);
                return;
            }
            status => return self.closed(format!("http_{}", status.as_u16())),
        }
        if !self.emit("open", None, None, None) {
            return;
        }
        let mut parser = sse::Parser::default();
        loop {
            let chunk = match response.chunk().await {
                Ok(Some(chunk)) => chunk,
                Ok(None) => return self.closed("eof"),
                Err(e) if e.is_timeout() => return self.closed("idle_timeout"),
                Err(e) => return self.closed(net(e).message),
            };
            for frame in parser.push(&chunk) {
                // 一帧可能在同一块里完成并被取走，pending() 看不到它；交付前单独限制。
                if frame.size() > MAX_BODY {
                    return self.closed("frame_too_large");
                }
                let (kind, id, ty, data) = classify(frame);
                if !self.emit(kind, id, Some(ty), Some(data)) || kind == "unauthorized" || kind == "forbidden" {
                    return;
                }
            }
            if parser.pending() > MAX_BODY {
                return self.closed("frame_too_large");
            }
        }
    }
}

/// 一条 SSE 帧对应的 `ac-sse` 负载：`(kind, id, type, data)`。
fn classify(frame: sse::Frame) -> (&'static str, Option<u64>, String, String) {
    let kind = match frame.event.as_deref() {
        Some("unauthorized") => "unauthorized",
        Some("forbidden") => "forbidden",
        Some("snapshot_required") => "snapshot_required",
        _ => "event",
    };
    let id = frame.id.and_then(|id| id.parse().ok());
    (kind, id, frame.event.unwrap_or_else(|| "message".into()), frame.data)
}

fn parse_cursor(cursor: Option<Value>) -> Result<Option<u64>> {
    let bad = || invalid("cursor 必须是非负整数");
    match cursor {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Number(n)) => n.as_u64().map(Some).ok_or_else(bad),
        Some(Value::String(s)) => s.parse().map(Some).map_err(|_| bad()),
        Some(_) => Err(bad()),
    }
}

#[tauri::command]
async fn stream_open(app: AppHandle, shell: State<'_, Shell>, epoch: u64, cursor: Option<Value>) -> Result<Value> {
    let cursor = parse_cursor(cursor)?;
    let mut inner = shell.lock();
    let active = inner.current(epoch)?;
    inner.stop_stream(); // 同一时间最多一条流
    let stream = Stream { app, epoch, gen: inner.stream_gen };
    inner.stream = Some(tauri::async_runtime::spawn(stream.run(active, cursor)));
    Ok(json!({}))
}

#[tauri::command]
fn stream_close(shell: State<'_, Shell>, epoch: u64) -> Result<Value> {
    let mut inner = shell.lock();
    inner.current(epoch)?;
    inner.stop_stream();
    Ok(json!({}))
}

// ---------------------------------------------------------------- 命令：保存工件

/// 下载落盘：写入目标目录里的临时文件并计算 SHA-256，摘要相符才改名到目标路径；
/// 其余情况（出错、摘要不符、中途放弃）离开作用域时删除临时文件。
struct BlobSink {
    path: PathBuf,
    file: Option<std::fs::File>,
    hasher: Sha256,
    bytes: u64,
}

impl BlobSink {
    /// `private`：只有当前用户可读写（Unix；备份导出用），普通工件下载沿用默认权限。
    fn create(dir: &Path, private: bool) -> Result<Self> {
        let nanos = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.subsec_nanos()).unwrap_or(0);
        let path = dir.join(format!(".agent-collab-{}-{nanos:08x}.part", std::process::id()));
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        if private {
            std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
        }
        #[cfg(not(unix))]
        let _ = private;
        let file = options
            .open(&path)
            .map_err(|e| io(format!("无法在目标目录创建临时文件：{e}")))?;
        Ok(Self { path, file: Some(file), hasher: Sha256::new(), bytes: 0 })
    }

    fn write(&mut self, chunk: &[u8]) -> Result<()> {
        // ponytail: 在异步任务里做同步写盘，每块几十 KB；出现卡顿再换成 spawn_blocking。
        self.file.as_mut().ok_or_else(|| io("临时文件已关闭"))?.write_all(chunk).map_err(io)?;
        self.hasher.update(chunk);
        self.bytes += chunk.len() as u64;
        Ok(())
    }

    /// 已写入的内容的摘要是否等于 `sha256`。
    fn matches(&self, sha256: &str) -> bool {
        hex(&self.hasher.clone().finalize()) == sha256
    }

    /// 摘要相符则落盘并原子改名到 `dest`，返回字节数；不符返回 `io` 错误（临时文件随即删除）。
    fn finish(mut self, sha256: &str, dest: &Path) -> Result<u64> {
        if !self.matches(sha256) {
            return Err(io("下载内容的 SHA-256 与预期不符，文件未保存"));
        }
        if let Some(file) = self.file.take() {
            file.sync_all().map_err(io)?;
        }
        std::fs::rename(&self.path, dest).map_err(io)?;
        Ok(self.bytes)
    }
}

impl Drop for BlobSink {
    fn drop(&mut self) {
        self.file.take(); // 先关句柄再删
        let _ = std::fs::remove_file(&self.path);
    }
}

#[tauri::command]
async fn blob_save(window: WebviewWindow, shell: State<'_, Shell>, epoch: u64, sha256: String, name: String) -> Result<Value> {
    let active = shell.lock().current(epoch)?;
    if !validate::sha256_hex(&sha256) {
        return Err(invalid("sha256 必须是 64 位小写十六进制"));
    }
    let url = validate::api_url(&active.base, &format!("/blobs/{sha256}")).map_err(invalid)?;
    let Some(dest) = dialog::save(&window, validate::file_name(&name)).await? else {
        return Ok(json!({ "cancelled": true }));
    };
    shell.lock().current(epoch)?; // 对话框开着的时候连接可能已经切换
    let mut response = shell.send(epoch, &active, active.http.api.get(url)).await?;
    match response.status() {
        StatusCode::OK => {}
        StatusCode::NOT_FOUND => return Err(err("not_found", "controller 上没有这个工件")),
        status => return Err(err("network", format!("下载失败（HTTP {}）", status.as_u16()))),
    }
    let dir = dest.parent().ok_or_else(|| io("所选路径没有上级目录"))?;
    let mut sink = BlobSink::create(dir, false)?;
    loop {
        let chunk = response.chunk().await;
        shell.lock().current(epoch)?;
        match chunk.map_err(net)? {
            Some(chunk) => sink.write(&chunk)?,
            None => break,
        }
    }
    let bytes = sink.finish(&sha256, &dest)?;
    Ok(json!({ "path": dest.to_string_lossy(), "bytes": bytes }))
}

/// 备份导出（生成归档与下载）的总时限。
const BACKUP_EXPORT_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(30 * 60);

/// 把 controller 上的一份备份导出到这台机器（“另存为”选定的位置），之后它才算实例外的备份。
/// 归档的 SHA-256 由 controller 在响应头里给出（`X-Content-SHA256`），落盘前核对，不符则不保存。
/// 备份含任务、项目与设置等敏感数据：只存到用户自己选的位置，不进入诊断导出。
#[tauri::command(rename_all = "snake_case")]
async fn backup_save(window: WebviewWindow, shell: State<'_, Shell>, epoch: u64, backup_id: String) -> Result<Value> {
    let active = shell.lock().current(epoch)?;
    if !backup_id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_') || backup_id.is_empty() || backup_id.len() > 120 {
        return Err(invalid("备份 ID 格式无效"));
    }
    let url = validate::api_url(&active.base, &format!("/maintenance/backups/{backup_id}/archive")).map_err(invalid)?;
    let Some(dest) = dialog::save(&window, validate::file_name(&format!("agent-collab-backup-{backup_id}.tar"))).await? else {
        return Ok(json!({ "cancelled": true }));
    };
    shell.lock().current(epoch)?;
    let _exporting = Exporting::start(&shell)?;
    // controller 先生成并核对归档再回响应头，大备份可能超过常规客户端 60 秒的读超时：用不带读超时的客户端，整体限时。
    let mut response = shell.send(epoch, &active, active.http.bulk.get(url).timeout(BACKUP_EXPORT_TIMEOUT)).await?;
    if !response.status().is_success() {
        let status = response.status();
        let body = read_body(response).await.unwrap_or_default();
        return Err(http_error("导出备份", status, &body));
    }
    let expected = response
        .headers()
        .get("x-content-sha256")
        .and_then(|v| v.to_str().ok())
        .filter(|v| validate::sha256_hex(v))
        .map(str::to_owned)
        .ok_or_else(|| bad_response("备份导出"))?;
    let dir = dest.parent().ok_or_else(|| io("所选路径没有上级目录"))?;
    let mut sink = BlobSink::create(dir, true)?;
    loop {
        let chunk = response.chunk().await;
        shell.lock().current(epoch)?;
        match chunk.map_err(net)? {
            Some(chunk) => sink.write(&chunk)?,
            None => break,
        }
    }
    let bytes = sink.finish(&expected, &dest)?;
    Ok(json!({ "path": dest.to_string_lossy(), "bytes": bytes, "sha256": expected }))
}

// ---------------------------------------------------------------- 命令：系统通知

/// 发一条系统通知。是否通知、通知哪些事件与限流由页面按本机设置决定；这里只去掉控制字符并限制长度。
#[tauri::command]
fn notify(app: AppHandle, title: String, body: String) -> Result<Value> {
    use tauri_plugin_notification::NotificationExt;
    let clip = |text: &str, max: usize| text.chars().filter(|c| !c.is_control() || *c == '\n').take(max).collect::<String>();
    app.notification().builder().title(clip(&title, 120)).body(clip(&body, 500)).show().map_err(io)?;
    Ok(json!({}))
}

// ---------------------------------------------------------------- 自动化钩子（仅内部测试版）

/// 默认不编译。启用 cargo feature `automation` 后：
/// - 环境变量 `AGENT_COLLAB_DESKTOP_AUTOMATION` 指向的本地 JS 文件，在主窗口页面加载完成后执行一次；
/// - 命令 `automation_report {"data"}` 把 JSON 逐行追加到 `AGENT_COLLAB_DESKTOP_AUTOMATION_OUT` 指向的文件。
///
/// 同一个 feature 还编译进对话框替身（`dialog`）与助手位置覆盖（`helper`）。
#[cfg(feature = "automation")]
mod automation {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};
    use tauri::webview::PageLoadEvent;

    static RAN: AtomicBool = AtomicBool::new(false);

    pub fn install(builder: tauri::Builder<tauri::Wry>) -> tauri::Builder<tauri::Wry> {
        builder.on_page_load(|webview, payload| {
            if webview.label() != "main" || payload.event() != PageLoadEvent::Finished || RAN.swap(true, Ordering::SeqCst) {
                return;
            }
            let Some(path) = std::env::var_os("AGENT_COLLAB_DESKTOP_AUTOMATION") else { return };
            match std::fs::read_to_string(&path) {
                Ok(script) => {
                    let _ = webview.eval(script);
                }
                Err(e) => eprintln!("automation: 读取 {} 失败：{e}", Path::new(&path).display()),
            }
        })
    }

    /// `automation_report` 只在这里授权给主窗口；`capabilities/main.json` 里没有它。
    pub fn grant(app: &tauri::App) -> tauri::Result<()> {
        app.add_capability(
            tauri::ipc::CapabilityBuilder::new("automation").window("main").permission("allow-automation-report"),
        )
    }

    #[tauri::command]
    pub fn automation_report(data: Value) -> Result<Value> {
        let path = std::env::var_os("AGENT_COLLAB_DESKTOP_AUTOMATION_OUT")
            .ok_or_else(|| invalid("未设置 AGENT_COLLAB_DESKTOP_AUTOMATION_OUT"))?;
        let mut file = std::fs::OpenOptions::new().create(true).append(true).open(path).map_err(io)?;
        writeln!(file, "{data}").map_err(io)?;
        Ok(json!({}))
    }
}

// ---------------------------------------------------------------- 启动

/// 主窗口不离开随包分发的页面：任何其他地址的导航都被取消。
fn navigation_guard() -> tauri::plugin::TauriPlugin<tauri::Wry> {
    tauri::plugin::Builder::new("navigation-guard").on_navigation(|_, url| validate::is_app_url(url)).build()
}

pub fn run() {
    let shell = Shell::new().expect("初始化 HTTP 客户端失败");
    let builder = tauri::Builder::default()
        .manage(shell)
        .plugin(navigation_guard())
        // 只用它的 Rust API 发通知；capabilities 里没有授予它的页面权限，页面只能经 `notify` 命令。
        .plugin(tauri_plugin_notification::init())
        // 桌面更新：只用它的 Rust API（update_* 命令），capabilities 里没有授予它的页面权限。
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(update::Updates::default())
        .setup(|app| {
            match instance_lock(&app.path().app_config_dir()?)? {
                Some(lock) => {
                    app.manage(lock); // 持有到进程结束
                }
                None => {
                    rfd::MessageDialog::new()
                        .set_title("Agent Collab")
                        .set_description("Agent Collab 已经在运行，请切换到已打开的窗口。")
                        .show();
                    std::process::exit(0);
                }
            }
            projects::clear_staging(app.handle()); // 上次运行被中断时留下的暂存文件
            update::startup(app.handle()); // 对账上次没有结束的更新记录，安排自动检查
            #[cfg(feature = "automation")]
            automation::grant(app)?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            app_info,
            connections_list,
            connection_pair,
            connection_open,
            connection_close,
            connection_forget,
            api_request,
            stream_open,
            stream_close,
            blob_save,
            notify,
            projects::project_bind,
            projects::project_bindings,
            projects::project_unbind,
            projects::project_scan,
            projects::project_upload,
            projects::project_cancel,
            projects::result_download,
            projects::result_plan,
            projects::result_apply,
            projects::result_unfinished,
            projects::result_recover,
            local::local_discover,
            local::local_setup,
            local::local_service,
            local::local_pair,
            local::local_uninstall,
            local::node_bindings,
            local::node_bind,
            local::node_unbind,
            local::diagnostics_export,
            local::diagnostics_preview,
            local::diagnostics_save,
            backup_save,
            update::update_status,
            update::update_settings,
            update::update_check,
            update::update_download,
            update::update_install,
            #[cfg(feature = "automation")]
            automation::automation_report,
        ]);
    #[cfg(feature = "automation")]
    let builder = automation::install(builder);
    builder.run(tauri::generate_context!()).expect("Agent Collab 启动失败");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backup_exports_and_update_installs_exclude_each_other() {
        use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
        let (exports, installing) = (AtomicUsize::new(0), AtomicBool::new(false));
        {
            let _export = Exporting::admit(&exports, &installing).unwrap();
            // 安装看得到进行中的导出（blockers 据此返回 busy）。
            assert_eq!(exports.load(Ordering::SeqCst), 1);
        }
        assert_eq!(exports.load(Ordering::SeqCst), 0);
        {
            let _install = Installing::close(&installing);
            assert!(Exporting::admit(&exports, &installing).is_err());
            assert_eq!(exports.load(Ordering::SeqCst), 0, "a refused export leaves no count behind");
        }
        // 安装没有进行下去（守卫离开作用域）后重新接受导出。
        assert!(Exporting::admit(&exports, &installing).is_ok());
    }

    fn active() -> Arc<Active> {
        Arc::new(Active {
            id: "conn-1".into(),
            base: "https://controller.example".into(),
            controller_id: "ctl-a".into(),
            token: Token("secret".into()),
            http: tls::http(None).unwrap(),
        })
    }

    /// 一个已打开连接的状态，以及它的代次。
    fn connected() -> (Inner, u64) {
        let mut inner = Inner::default();
        let epoch = inner.bump();
        inner.active = Some(active());
        (inner, epoch)
    }

    fn code<T>(result: Result<T>) -> String {
        result.err().map_or("ok".into(), |e| e.code.into_owned())
    }

    #[test]
    fn only_the_current_epoch_is_accepted() {
        let (mut inner, epoch) = connected();
        assert_eq!(code(inner.current(epoch)), "ok");
        assert_eq!(code(inner.current(epoch - 1)), "stale_connection");
        assert_eq!(code(inner.current(epoch + 1)), "stale_connection");

        // 关闭连接：代次加一，旧代次和新代次（没有连接）都不可用。
        let closed = inner.bump();
        assert_eq!(closed, epoch + 1);
        assert_eq!(code(inner.current(epoch)), "stale_connection");
        assert_eq!(code(inner.current(closed)), "stale_connection");

        // 没有任何连接时也一样。
        assert_eq!(code(Inner::default().current(0)), "stale_connection");
    }

    #[test]
    fn epoch_is_monotonic_across_open_close_cycles() {
        let mut inner = Inner::default();
        let mut seen = vec![inner.epoch];
        for _ in 0..3 {
            seen.push(inner.bump()); // open
            inner.active = Some(active());
            seen.push(inner.bump()); // close
        }
        assert!(seen.windows(2).all(|w| w[1] == w[0] + 1), "{seen:?}");
    }

    #[test]
    fn controller_mismatch_closes_the_connection() {
        let (mut inner, epoch) = connected();
        assert_eq!(code(inner.verify(epoch, Some("ctl-a"))), "ok");
        assert_eq!(inner.epoch, epoch);

        assert_eq!(code(inner.verify(epoch, Some("ctl-b"))), "controller_mismatch");
        assert_eq!(inner.epoch, epoch + 1);
        assert!(inner.active.is_none());
        // 连接已关闭：同一代次后续的响应全部过期。
        assert_eq!(code(inner.verify(epoch, Some("ctl-a"))), "stale_connection");

        // 响应没有 X-Controller-Id 也算不一致。
        let (mut inner, epoch) = connected();
        assert_eq!(code(inner.verify(epoch, None)), "controller_mismatch");
    }

    #[test]
    fn revoked_at_is_optional_in_connections_json() {
        let old = json!({ "connections": [{
            "id": "c1", "name": "n", "url": "https://x", "controller_id": "ctl", "client_id": "c1",
            "kind": "desktop", "scopes": [], "paired_at": "2026-01-01T00:00:00.000Z"
        }]});
        let mut conn = serde_json::from_value::<StoreFile>(old).unwrap().connections.remove(0);
        assert!(conn.revoked_at.is_none());
        assert!(serde_json::to_value(&conn).unwrap().get("revoked_at").is_none());
        conn.revoked_at = Some("2026-11-11T00:00:00.000Z".into());
        let back: Connection = serde_json::from_value(serde_json::to_value(&conn).unwrap()).unwrap();
        assert_eq!(back.revoked_at.as_deref(), Some("2026-11-11T00:00:00.000Z"));
        // 旧记录没有 CA 字段：按系统根证书处理，返回给页面时 custom_ca 为 false。
        assert!(!back.custom_ca && back.ca_pem.is_none());
        assert_eq!(back.view()["custom_ca"], json!(false));
        assert!(back.view().get("ca_fingerprint").is_none());
    }

    #[test]
    fn ca_certificate_stays_in_the_record_and_out_of_the_page() {
        let mut conn: Connection = serde_json::from_value(json!({
            "id": "c1", "name": "n", "url": "https://x", "controller_id": "ctl", "client_id": "c1",
            "kind": "desktop", "scopes": [], "paired_at": "2026-01-01T00:00:00.000Z"
        }))
        .unwrap();
        conn.custom_ca = true;
        conn.ca_fingerprint = Some("ab".repeat(32));
        conn.ca_pem = Some("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n".into());
        // 写进 connections.json 的记录带证书，读回来不丢。
        let stored = serde_json::to_value(&conn).unwrap();
        assert!(stored["ca_pem"].as_str().unwrap().contains("BEGIN CERTIFICATE"));
        assert_eq!(serde_json::from_value::<Connection>(stored).unwrap().ca_pem, conn.ca_pem);
        // 页面只看到开关与指纹。
        let view = conn.view();
        assert_eq!((view["custom_ca"].clone(), view["ca_fingerprint"].clone()), (json!(true), json!("ab".repeat(32))));
        assert!(view.get("ca_pem").is_none());
    }

    #[test]
    fn a_connection_with_a_custom_ca_never_falls_back_to_system_roots() {
        let shell = Shell::new().unwrap();
        let mut conn: Connection = serde_json::from_value(json!({
            "id": "c1", "name": "n", "url": "https://x", "controller_id": "ctl", "client_id": "c1",
            "kind": "desktop", "scopes": [], "paired_at": "2026-01-01T00:00:00.000Z"
        }))
        .unwrap();
        assert!(shell.http_for(&conn).is_ok());
        conn.custom_ca = true; // 记录说要用指定的 CA，但证书不在或已损坏
        assert_eq!(code(shell.http_for(&conn)), "io");
        conn.ca_pem = Some("garbage".into());
        assert_eq!(code(shell.http_for(&conn)), "io");
    }

    #[test]
    fn close_only_affects_its_own_epoch() {
        let (mut inner, old) = connected();
        let new = inner.bump();
        inner.active = Some(active());
        // 旧页面状态晚到的关闭被拒绝，新连接不受影响。
        assert_eq!(code(inner.close(old)), "stale_connection");
        assert_eq!(inner.epoch, new);
        assert!(inner.active.is_some());

        assert_eq!(code(inner.close(new)), "ok");
        assert_eq!(inner.epoch, new + 1);
        assert!(inner.active.is_none());
    }

    #[test]
    fn second_instance_cannot_take_the_lock() {
        let dir = scratch_dir("instance");
        let first = instance_lock(&dir).unwrap();
        assert!(first.is_some());
        assert!(instance_lock(&dir).unwrap().is_none());
        drop(first); // 进程退出时同样释放
        assert!(instance_lock(&dir).unwrap().is_some());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn only_headerless_5xx_counts_as_proxy_outage() {
        for status in [StatusCode::BAD_GATEWAY, StatusCode::SERVICE_UNAVAILABLE, StatusCode::GATEWAY_TIMEOUT] {
            assert!(from_proxy(None, status));
            // 带了身份头的 5xx 来自 controller 本身，照常核对身份。
            assert!(!from_proxy(Some("ctl-b"), status));
        }
        for status in [StatusCode::OK, StatusCode::UNAUTHORIZED, StatusCode::NOT_FOUND] {
            assert!(!from_proxy(None, status));
        }
    }

    #[test]
    fn late_response_from_old_epoch_is_dropped_without_touching_the_new_connection() {
        let (mut inner, old) = connected();
        let new = inner.bump();
        inner.active = Some(active());
        // 旧代次的响应即使 controller 不一致，也只是被丢弃，不会关掉新连接。
        assert_eq!(code(inner.verify(old, Some("ctl-b"))), "stale_connection");
        assert_eq!(inner.epoch, new);
        assert!(inner.active.is_some());
    }

    #[test]
    fn forgetting_only_closes_the_active_connection() {
        let (mut inner, epoch) = connected();
        inner.close_if_active("other");
        assert_eq!(inner.epoch, epoch);
        inner.close_if_active("conn-1");
        assert_eq!(inner.epoch, epoch + 1);
        assert!(inner.active.is_none());
    }

    #[test]
    fn stream_generation_changes_on_replace_close_and_epoch_bump() {
        let (mut inner, _) = connected();
        let first = inner.stream_gen;
        inner.stop_stream();
        assert_eq!(inner.stream_gen, first + 1);
        inner.bump();
        assert_eq!(inner.stream_gen, first + 2);
    }

    #[test]
    fn sse_frames_map_to_payload_kinds() {
        let frame = |id: Option<&str>, event: Option<&str>, data: &str| sse::Frame {
            id: id.map(Into::into),
            event: event.map(Into::into),
            data: data.into(),
        };
        assert_eq!(
            classify(frame(Some("42"), Some("job.created"), "{}")),
            ("event", Some(42), "job.created".to_string(), "{}".to_string())
        );
        assert_eq!(
            classify(frame(None, Some("snapshot_required"), "{\"min_seq\":9}")),
            ("snapshot_required", None, "snapshot_required".to_string(), "{\"min_seq\":9}".to_string())
        );
        assert_eq!(classify(frame(None, Some("unauthorized"), "")).0, "unauthorized");
        // 权限不足与凭据失效是两种帧：前者不让页面离开连接。
        assert_eq!(classify(frame(None, Some("forbidden"), "{}")).0, "forbidden");
        // 没有 event 字段按 SSE 缺省类型 message；非数字 id 记为 null。
        assert_eq!(classify(frame(Some("abc"), None, "x")), ("event", None, "message".to_string(), "x".to_string()));
    }

    #[test]
    fn cursor_accepts_integers_and_digit_strings() {
        assert_eq!(parse_cursor(None), Ok(None));
        assert_eq!(parse_cursor(Some(Value::Null)), Ok(None));
        assert_eq!(parse_cursor(Some(json!(0))), Ok(Some(0)));
        assert_eq!(parse_cursor(Some(json!(1234))), Ok(Some(1234)));
        assert_eq!(parse_cursor(Some(json!("77"))), Ok(Some(77)));
        for bad in [json!(-1), json!(1.5), json!("x"), json!(""), json!([1]), json!(true)] {
            assert_eq!(code(parse_cursor(Some(bad))), "invalid");
        }
    }

    #[test]
    fn errors_serialize_as_code_and_message() {
        assert_eq!(
            serde_json::to_value(stale()).unwrap(),
            json!({ "code": "stale_connection", "message": "连接已切换或关闭，这次请求被丢弃" })
        );
        assert_eq!(proxy_error(StatusCode::BAD_GATEWAY).code, "network");
        let e = http_error("配对失败", StatusCode::BAD_REQUEST, br#"{"error":{"code":"invalid","message":"bad code"}}"#);
        assert_eq!((&*e.code, e.message.as_str()), ("invalid", "配对失败（HTTP 400）：bad code"));
        assert_eq!(http_error("x", StatusCode::BAD_GATEWAY, b"<html>").code, "network");
        // 401 与 403 不能合并：403 是权限不足，凭据仍然有效。
        assert_eq!(http_error("x", StatusCode::UNAUTHORIZED, b"").code, "unauthorized");
        assert_eq!(http_error("x", StatusCode::FORBIDDEN, b"").code, "forbidden");
    }

    #[test]
    fn storage_errors_never_carry_secret_bytes() {
        let e = storage_err(keyring::Error::BadEncoding(b"super-secret".to_vec()));
        assert_eq!(e.code, "secure_storage_unavailable");
        assert!(!e.message.contains("super-secret"));
        assert!(storage_err(keyring::Error::NoEntry).message.ends_with("没有找到这条连接的凭据"));
    }

    #[test]
    fn iso8601_formats_utc_with_milliseconds() {
        assert_eq!(iso8601(0, 0), "1970-01-01T00:00:00.000Z");
        assert_eq!(iso8601(1_700_000_000, 123), "2023-11-14T22:13:20.123Z");
        assert_eq!(iso8601(1_709_164_800, 0), "2024-02-29T00:00:00.000Z");
        assert_eq!(iso8601(1_735_689_599, 999), "2024-12-31T23:59:59.999Z");
    }

    const HELLO_SHA256: &str = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824";

    fn scratch_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("agent-collab-desktop-test-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn entries(dir: &Path) -> Vec<String> {
        let mut names: Vec<String> =
            std::fs::read_dir(dir).unwrap().map(|e| e.unwrap().file_name().to_string_lossy().into_owned()).collect();
        names.sort();
        names
    }

    #[test]
    fn blob_is_renamed_into_place_only_when_the_digest_matches() {
        let dir = scratch_dir("match");
        let dest = dir.join("hello.txt");
        std::fs::write(&dest, b"old contents").unwrap(); // 用户已在对话框里确认覆盖

        let mut sink = BlobSink::create(&dir, false).unwrap();
        sink.write(b"he").unwrap();
        sink.write(b"llo").unwrap();
        assert_eq!(sink.finish(HELLO_SHA256, &dest), Ok(5));
        assert_eq!(std::fs::read(&dest).unwrap(), b"hello");
        assert_eq!(entries(&dir), ["hello.txt"]);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn blob_with_wrong_digest_or_abandoned_download_leaves_nothing_behind() {
        let dir = scratch_dir("mismatch");
        let dest = dir.join("out.bin");

        let mut sink = BlobSink::create(&dir, false).unwrap();
        sink.write(b"tampered").unwrap();
        assert_eq!(code(sink.finish(HELLO_SHA256, &dest)), "io");
        assert!(entries(&dir).is_empty());

        let mut sink = BlobSink::create(&dir, false).unwrap();
        sink.write(b"partial").unwrap();
        assert_eq!(entries(&dir).len(), 1);
        drop(sink); // 下载中途出错或代次变化
        assert!(entries(&dir).is_empty());
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
