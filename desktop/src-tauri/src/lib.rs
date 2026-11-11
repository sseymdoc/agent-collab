//! Agent Collab 桌面外壳。契约见 `docs/desktop.md`。
//!
//! 外壳只做四件事：保管客户端凭据、代发 controller 请求、转发事件流、把工件存到用户选定的位置。
//! 凭据只存在于系统凭据存储和本进程内存里的 [`Token`]，不进入返回值、事件、日志和 `connections.json`。

mod sse;
mod validate;

use std::collections::HashMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use reqwest::header::{HeaderValue, ACCEPT, CONTENT_TYPE};
use reqwest::StatusCode;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Emitter, Manager, State, WebviewWindow};
use validate::API_PREFIX as API;

const SERVICE: &str = "agent-collab";
const MAX_BODY: usize = 16 * 1024 * 1024;
/// 60 秒没有收到任何字节（含 SSE 心跳）即认为连接停滞。
const IDLE: Duration = Duration::from_secs(60);

// ---------------------------------------------------------------- 错误

/// 命令失败时返回给页面的 `{"code", "message"}`。
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct BridgeError {
    code: &'static str,
    message: String,
}

type Result<T> = std::result::Result<T, BridgeError>;

fn err(code: &'static str, message: impl Into<String>) -> BridgeError {
    BridgeError { code, message: message.into() }
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
    // 401/403：配对码无效或过期，或这条连接的凭据已被撤销；页面据此提示重新配对。
    let code = if status == StatusCode::UNAUTHORIZED || status == StatusCode::FORBIDDEN {
        "unauthorized"
    } else if status.is_client_error() {
        "invalid"
    } else {
        "network"
    };
    err(code, format!("{what}（HTTP {}）{detail}", status.as_u16()))
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

/// 当前连接：地址、绑定的 controller 与内存中的凭据。
struct Active {
    id: String,
    base: String,
    controller_id: String,
    token: Token,
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
    http: reqwest::Client,
}

impl Shell {
    fn new() -> reqwest::Result<Self> {
        let http = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .no_proxy()
            .connect_timeout(Duration::from_secs(10))
            .read_timeout(IDLE)
            .user_agent(concat!("agent-collab-desktop/", env!("CARGO_PKG_VERSION")))
            .build()?;
        Ok(Self { inner: Mutex::default(), pairing: tokio::sync::Mutex::new(()), http })
    }

    /// 状态锁。只在同步代码段里持有，不跨 await。
    fn lock(&self) -> MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// 以当前连接的凭据发出请求，并在响应返回时核对代次与 `X-Controller-Id`。
    /// 请求期间代次变了，结果（包括错误）一律丢弃。
    async fn send(&self, epoch: u64, active: &Active, request: reqwest::RequestBuilder) -> Result<reqwest::Response> {
        let sent = request.bearer_auth(&active.token.0).send().await;
        let mut inner = self.lock();
        inner.current(epoch)?;
        let response = sent.map_err(net)?;
        let header = controller_header(&response);
        if header.is_none() && response.status().is_server_error() {
            // 没带身份头的 5xx 来自 controller 前面的反向代理（controller 重启期间的 502/503/504）：
            // 按暂时不可达处理，不关闭连接。带了头但不一致，或其他状态码缺头，仍判为不是同一个 controller。
            return Err(err("network", format!("controller 暂时不可达（HTTP {}）", response.status().as_u16())));
        }
        inner.verify(epoch, header.as_deref())?;
        Ok(response)
    }

    /// 用凭据调用 `DELETE /client` 自我撤销，返回是否确认成功。
    async fn revoke(&self, base: &str, controller_id: Option<&str>, token: &Token) -> bool {
        match self.http.delete(format!("{base}{API}/client")).bearer_auth(&token.0).send().await {
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
    let path = store_path(app)?;
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(io)?;
    }
    let tmp = path.with_extension("json.tmp");
    let bytes = serde_json::to_vec_pretty(&json!({ "connections": connections })).map_err(io)?;
    std::fs::write(&tmp, bytes).map_err(io)?;
    std::fs::rename(&tmp, &path).map_err(io)
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
    tauri::async_runtime::spawn_blocking(move || keyring::Entry::new(SERVICE, &account).and_then(|entry| op(&entry)))
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
    json!({
        "version": app.package_info().version.to_string(),
        // 构建时由环境变量 AGENT_COLLAB_COMMIT 传入；未设置为 null。
        "commit": option_env!("AGENT_COLLAB_COMMIT"),
        "platform": std::env::consts::OS,
        "arch": match std::env::consts::ARCH { "aarch64" => "arm64", other => other },
        // 候选验证版：编译进了自动化钩子（cargo feature `automation`）。
        // 预发布版本号或编译进了自动化钩子的构建都不是正式发行版。
        "candidate": cfg!(feature = "automation") || env!("CARGO_PKG_VERSION").contains('-'),
    })
}

#[tauri::command]
async fn connections_list(app: AppHandle, shell: State<'_, Shell>) -> Result<Value> {
    let inner = shell.lock();
    let connections = load(&app, &inner)?;
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

#[tauri::command]
async fn connection_pair(app: AppHandle, shell: State<'_, Shell>, url: String, code: String, name: String) -> Result<Connection> {
    let base = validate::base_url(&url).map_err(invalid)?;
    let (code, name) = (code.trim(), name.trim());
    if code.is_empty() {
        return Err(invalid("配对码不能为空"));
    }
    let response = shell
        .http
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
    };
    // 从这里起手里有一份已签发的凭据：之后任何一步失败，都尽力用它自我撤销，不留下无人保管的凭据。
    let adopted = shell.adopt(&app, &conn, header.as_deref(), &token).await;
    if adopted.is_err() {
        shell.revoke(&base, None, &token).await;
    }
    adopted.map(|()| conn)
}

#[tauri::command]
async fn connection_open(app: AppHandle, shell: State<'_, Shell>, id: String) -> Result<Value> {
    // 打开即先关闭旧连接：核对新连接期间，旧代次的请求和事件流已经失效。
    let (conn, epoch) = {
        let mut inner = shell.lock();
        (find(&app, &inner, &id)?, inner.bump())
    };
    let base = validate::base_url(&conn.url).map_err(invalid)?;
    let token = Token(credential(&conn, |entry| entry.get_password()).await?);
    let response = shell.http.get(format!("{base}{API}/client")).bearer_auth(&token.0).send().await.map_err(net)?;
    if controller_header(&response).as_deref() != Some(conn.controller_id.as_str()) {
        return Err(mismatch());
    }
    let status = response.status();
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
    inner.active = Some(Arc::new(Active { id, base, controller_id: conn.controller_id, token }));
    Ok(json!({ "epoch": epoch, "identity": info["identity"].take(), "controller": info["controller"].take() }))
}

#[tauri::command]
fn connection_close(shell: State<'_, Shell>) -> Value {
    shell.lock().bump();
    json!({})
}

#[tauri::command]
async fn connection_forget(app: AppHandle, shell: State<'_, Shell>, id: String, revoke: bool) -> Result<Value> {
    let conn = {
        let mut inner = shell.lock();
        let conn = find(&app, &inner, &id)?;
        inner.close_if_active(&id);
        conn
    };
    let mut revoked = false;
    if revoke {
        // 撤销失败（包括读不到凭据）不阻止本地删除，结果如实返回。
        if let (Ok(base), Ok(secret)) = (validate::base_url(&conn.url), credential(&conn, |entry| entry.get_password()).await) {
            revoked = shell.revoke(&base, Some(&conn.controller_id), &Token(secret)).await;
        }
    }
    credential(&conn, |entry| match entry.delete_credential() {
        Err(keyring::Error::NoEntry) => Ok(()), // 凭据本来就不在
        other => other,
    })
    .await?;
    let mut inner = shell.lock();
    let remaining: Vec<Connection> = load(&app, &inner)?.into_iter().filter(|c| c.id != id).collect();
    save(&app, &inner, &remaining)?;
    inner.close_if_active(&id); // 删除期间它可能刚被打开
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
    let method = reqwest::Method::from_bytes(method.as_bytes()).map_err(|_| invalid("不支持的请求方法"))?;
    let url = validate::api_url(&active.base, &path).map_err(invalid)?;
    let mut request = shell.http.request(method, url);
    // 页面给的请求头只转发 X-Coordinator-Token。
    if let Some((_, value)) = headers.iter().flatten().find(|(k, _)| k.eq_ignore_ascii_case("x-coordinator-token")) {
        let mut value = HeaderValue::from_str(value).map_err(|_| invalid("X-Coordinator-Token 的值无效"))?;
        value.set_sensitive(true);
        request = request.header("x-coordinator-token", value);
    }
    if let Some(body) = body {
        request = request.header(CONTENT_TYPE, "application/json").body(body);
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
        let request = shell.http.get(url).header(ACCEPT, "text/event-stream").bearer_auth(&active.token.0);
        let mut response = match request.send().await {
            Ok(response) => response,
            Err(e) => return self.closed(net(e).message),
        };
        let header = controller_header(&response);
        if header.is_none() && response.status().is_server_error() {
            return self.closed(format!("http_{}", response.status().as_u16())); // 反向代理的 5xx，同 send()
        }
        if header.as_deref() != Some(active.controller_id.as_str()) {
            self.closed("controller_mismatch");
            let _ = shell.lock().verify(self.epoch, header.as_deref());
            return;
        }
        match response.status() {
            StatusCode::OK => {}
            StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN => {
                self.emit("unauthorized", None, None, None);
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
                let (kind, id, ty, data) = classify(frame);
                if !self.emit(kind, id, Some(ty), Some(data)) || kind == "unauthorized" {
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
    fn create(dir: &Path) -> Result<Self> {
        let nanos = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.subsec_nanos()).unwrap_or(0);
        let path = dir.join(format!(".agent-collab-{}-{nanos:08x}.part", std::process::id()));
        let file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
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

    /// 摘要相符则落盘并原子改名到 `dest`，返回字节数；不符返回 `io` 错误（临时文件随即删除）。
    fn finish(mut self, sha256: &str, dest: &Path) -> Result<u64> {
        let digest: String = std::mem::take(&mut self.hasher).finalize().iter().map(|b| format!("{b:02x}")).collect();
        if digest != sha256 {
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

/// 弹出系统“另存为”对话框；用户取消时返回 None。
async fn save_dialog(window: &WebviewWindow, file_name: String) -> Result<Option<PathBuf>> {
    let (tx, rx) = tokio::sync::oneshot::channel();
    let parent = window.clone();
    // 对话框要在主线程上创建（macOS 的要求）；创建后得到的 future 可以在任意线程等待。
    window
        .run_on_main_thread(move || {
            let mut dialog = rfd::AsyncFileDialog::new().set_parent(&parent);
            if !file_name.is_empty() {
                dialog = dialog.set_file_name(file_name);
            }
            let _ = tx.send(dialog.save_file());
        })
        .map_err(io)?;
    let picked = rx.await.map_err(|_| io("无法打开保存对话框"))?.await;
    Ok(picked.map(|file| file.path().to_path_buf()))
}

#[tauri::command]
async fn blob_save(window: WebviewWindow, shell: State<'_, Shell>, epoch: u64, sha256: String, name: String) -> Result<Value> {
    let active = shell.lock().current(epoch)?;
    if !validate::sha256_hex(&sha256) {
        return Err(invalid("sha256 必须是 64 位小写十六进制"));
    }
    let url = validate::api_url(&active.base, &format!("/blobs/{sha256}")).map_err(invalid)?;
    let Some(dest) = save_dialog(&window, validate::file_name(&name)).await? else {
        return Ok(json!({ "cancelled": true }));
    };
    shell.lock().current(epoch)?; // 对话框开着的时候连接可能已经切换
    let mut response = shell.send(epoch, &active, shell.http.get(url)).await?;
    match response.status() {
        StatusCode::OK => {}
        StatusCode::NOT_FOUND => return Err(err("not_found", "controller 上没有这个工件")),
        status => return Err(err("network", format!("下载失败（HTTP {}）", status.as_u16()))),
    }
    let dir = dest.parent().ok_or_else(|| io("所选路径没有上级目录"))?;
    let mut sink = BlobSink::create(dir)?;
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

// ---------------------------------------------------------------- 自动化钩子（仅候选版验证用）

/// 默认不编译。启用 cargo feature `automation` 后：
/// - 环境变量 `AGENT_COLLAB_DESKTOP_AUTOMATION` 指向的本地 JS 文件，在主窗口页面加载完成后执行一次；
/// - 命令 `automation_report {"data"}` 把 JSON 逐行追加到 `AGENT_COLLAB_DESKTOP_AUTOMATION_OUT` 指向的文件。
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
        .setup(|_app| {
            #[cfg(feature = "automation")]
            automation::grant(_app)?;
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

    fn active() -> Arc<Active> {
        Arc::new(Active {
            id: "conn-1".into(),
            base: "https://controller.example".into(),
            controller_id: "ctl-a".into(),
            token: Token("secret".into()),
        })
    }

    /// 一个已打开连接的状态，以及它的代次。
    fn connected() -> (Inner, u64) {
        let mut inner = Inner::default();
        let epoch = inner.bump();
        inner.active = Some(active());
        (inner, epoch)
    }

    fn code<T>(result: Result<T>) -> &'static str {
        result.err().map(|e| e.code).unwrap_or("ok")
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
        let e = http_error("配对失败", StatusCode::BAD_REQUEST, br#"{"error":{"code":"invalid","message":"bad code"}}"#);
        assert_eq!((e.code, e.message.as_str()), ("invalid", "配对失败（HTTP 400）：bad code"));
        assert_eq!(http_error("x", StatusCode::BAD_GATEWAY, b"<html>").code, "network");
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

        let mut sink = BlobSink::create(&dir).unwrap();
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

        let mut sink = BlobSink::create(&dir).unwrap();
        sink.write(b"tampered").unwrap();
        assert_eq!(code(sink.finish(HELLO_SHA256, &dest)), "io");
        assert!(entries(&dir).is_empty());

        let mut sink = BlobSink::create(&dir).unwrap();
        sink.write(b"partial").unwrap();
        assert_eq!(entries(&dir).len(), 1);
        drop(sink); // 下载中途出错或代次变化
        assert!(entries(&dir).is_empty());
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
