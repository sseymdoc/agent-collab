//! 发行身份与桌面更新（ADR 0004）。
//!
//! - 构建身份（正式版、开发候选版、内部测试版）、更新通道与更新地址在构建时定下（环境变量见 `docs/desktop.md`“构建”）；
//!   验签公钥在 Tauri 配置的 `plugins.updater.pubkey`。页面只能在本身份允许的通道之间切换。
//! - 检查、下载、验签与安装由 Tauri 官方 updater 完成；这里补上它不管的三件事：随更新发布的产品发行清单
//!   （`product-manifest.json`，同一把公钥签名）的核对与兼容判断、安装前的安全空闲条件、可恢复的持久记录
//!   （应用本机数据目录的 `updates/<op_id>.json`）。
//! - 桌面更新只换这台机器上的桌面外壳与随包载荷。本机后台服务从安装前缀运行，不受影响；随包后台版本变新之后，
//!   页面单独提示“本机后台可升级”，由用户经安装器的事务升级（`local_setup`）。

use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;

use base64::Engine;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, Manager, State};
use tauri_plugin_updater::{Update, UpdaterExt};

use super::{bundle, err, helper, invalid, io, now_iso, write_json, BridgeError, Result, Shell};

/// 构建身份：`formal`、`candidate`、`internal-test`；没有给出的开发构建为 `dev`（不对外分发，不检查更新）。
pub const KIND: &str = match option_env!("AGENT_COLLAB_BUILD_KIND") {
    Some(kind) => kind,
    None => "dev",
};
/// 更新地址的根：`<根>/<通道>/latest.json` 与 `<根>/<通道>/product-manifest.json`。没有给出时不检查更新。
const UPDATE_BASE: Option<&str> = option_env!("AGENT_COLLAB_UPDATE_BASE");
/// 本身份允许的通道，逗号分隔，第一个是缺省通道。
const UPDATE_CHANNELS: &str = match option_env!("AGENT_COLLAB_UPDATE_CHANNELS") {
    Some(channels) => channels,
    None => "",
};
/// 更新记录的步骤，名称与顺序固定（ADR 0004 第 6 条）。桌面更新没有数据变化，`backup` 记为 skipped。
const STEPS: [&str; 7] = ["compat", "download", "verify", "prepare", "backup", "switch", "health"];
const CHECK_TIMEOUT: Duration = Duration::from_secs(30);
const MANIFEST_LIMIT: usize = 1024 * 1024;

pub fn channels() -> Vec<&'static str> {
    UPDATE_CHANNELS.split(',').map(str::trim).filter(|c| !c.is_empty()).collect()
}

/// 这个构建是否能检查更新：有更新地址、有通道、配置了公钥，而且不是开发构建。
fn configured(pubkey: &str) -> bool {
    KIND != "dev" && UPDATE_BASE.is_some_and(|base| !base.is_empty()) && !channels().is_empty() && !pubkey.trim().is_empty()
}

/// 通道目录下的一个文件。正式身份只接受 https；测试身份另外允许 loopback 上的 http（隔离联调用）。
fn channel_url(base: &str, channel: &str, file: &str) -> std::result::Result<url::Url, String> {
    if !channels().contains(&channel) {
        return Err(format!("这个构建没有 {channel} 通道"));
    }
    let url = url::Url::parse(&format!("{}/{channel}/{file}", base.trim_end_matches('/'))).map_err(|e| e.to_string())?;
    let loopback = matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "[::1]"));
    match url.scheme() {
        "https" => Ok(url),
        "http" if loopback && KIND != "formal" => Ok(url),
        _ => Err("更新地址必须是 https（测试身份只另外允许 loopback 上的 http）".into()),
    }
}

// ---------------------------------------------------------------- 设置

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Settings {
    pub channel: String,
    /// 启动时与每天检查一次。
    pub auto_check: bool,
    /// 检查到新版本后自动下载（不安装）。
    pub auto_download: bool,
    /// 下载完成后，满足安全空闲条件时自动安装。
    pub auto_install: bool,
}

impl Settings {
    /// 设置文件存在却读不出或损坏：不按缺省值自动检查、下载或安装（用户可能关掉过），等用户在维护页重新保存。
    fn cautious() -> Self {
        Self { auto_check: false, auto_download: false, auto_install: false, ..Self::defaults() }
    }
    fn defaults() -> Self {
        Self { channel: channels().first().copied().unwrap_or("").to_string(), auto_check: true, auto_download: false, auto_install: false }
    }

    /// 读到的通道不在本身份允许的范围内（例如换了构建身份）时退回缺省通道。
    fn sanitized(mut self) -> Self {
        if !channels().contains(&self.channel.as_str()) {
            self.channel = Self::defaults().channel;
        }
        self
    }
}

fn settings_path(app: &AppHandle) -> Result<PathBuf> {
    Ok(app.path().app_config_dir().map_err(io)?.join("update-settings.json"))
}

fn load_settings(app: &AppHandle) -> Settings {
    let Ok(path) = settings_path(app) else {
        return Settings::cautious();
    };
    match std::fs::read(&path) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Settings::defaults(),
        Ok(bytes) => serde_json::from_slice::<Settings>(&bytes).map_or_else(|_| Settings::cautious(), Settings::sanitized),
        Err(_) => Settings::cautious(),
    }
}

// ---------------------------------------------------------------- 持久记录

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Step {
    pub name: String,
    /// `pending`、`running`、`done`、`skipped`、`failed`。
    pub status: String,
    pub at: Option<String>,
    pub detail: Option<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Journal {
    pub op_id: String,
    pub kind: String,
    pub from: String,
    pub to: String,
    pub channel: String,
    pub started_at: String,
    pub updated_at: String,
    /// `running`、`done`、`failed`（切换前失败，旧版本继续可用）、`pending`（需要处理）。
    pub status: String,
    pub reason: Option<String>,
    pub steps: Vec<Step>,
}

impl Journal {
    fn new(from: &str, to: &str, channel: &str) -> Self {
        let now = now_iso();
        Self {
            op_id: uuid::Uuid::new_v4().to_string(),
            kind: "desktop_update".into(),
            from: from.into(),
            to: to.into(),
            channel: channel.into(),
            started_at: now.clone(),
            updated_at: now,
            status: "running".into(),
            reason: None,
            steps: STEPS.iter().map(|name| Step { name: (*name).into(), status: "pending".into(), at: None, detail: None }).collect(),
        }
    }

    fn mark(&mut self, name: &str, status: &str, detail: Option<String>) {
        let now = now_iso();
        if let Some(step) = self.steps.iter_mut().find(|step| step.name == name) {
            step.status = status.into();
            step.at = Some(now.clone());
            step.detail = detail;
        }
        self.updated_at = now;
    }

    fn finish(&mut self, status: &str, reason: Option<String>) {
        self.status = status.into();
        self.reason = reason;
        self.updated_at = now_iso();
    }

    fn step(&self, name: &str) -> Option<&Step> {
        self.steps.iter().find(|step| step.name == name)
    }

    /// 启动时对账：记录停在 running 的更新，按现在实际运行的版本给出结论，不靠猜。
    /// - 已是目标版本：切换完成；载荷核对通过即健康检查通过（`done`），否则 `pending`。
    /// - 仍是原版本：安装器已经启动过（`switch` 为 running）说明安装没有完成或被取消，旧版本继续可用（`failed`）；
    ///   安装器还没启动就退出（例如下载中关闭窗口），同样 `failed`。
    /// - 两者都不是（又装了别的版本）：`pending`，如实说明。
    fn reconcile(&mut self, current: &str, payload_ok: std::result::Result<(), String>) {
        // 切换已完成、只是健康检查没过的记录（例如安装程序中途被结束、载荷不完整）：用户重新运行安装程序修复之后，
        // 下次启动再核对一次，通过就记为完成；更新检查不会再提供同一个版本，修复只能经安装程序。
        if self.status == "pending" && current == self.to && self.step("health").is_some_and(|step| step.status == "failed") {
            if payload_ok.is_ok() {
                self.mark("health", "done", Some("重新运行安装程序之后，随包载荷核对通过".into()));
                self.finish("done", None);
            }
            return;
        }
        if self.status != "running" {
            return;
        }
        if current == self.to {
            self.mark("switch", "done", None);
            match payload_ok {
                Ok(()) => {
                    self.mark("health", "done", Some("新版本已启动，随包载荷核对通过".into()));
                    self.finish("done", None);
                }
                Err(reason) => {
                    self.mark("health", "failed", Some(reason.clone()));
                    self.finish("pending", Some(format!("新版本已启动，但随包载荷核对失败：{reason}；重新运行安装程序修复")));
                }
            }
        } else if current == self.from {
            let launched = self.step("switch").is_some_and(|step| step.status == "running");
            if launched {
                self.mark("switch", "failed", Some("安装程序没有完成".into()));
            }
            self.finish(
                "failed",
                Some(if launched { "安装没有完成或被取消，仍在使用原版本" } else { "更新在安装前中断，仍在使用原版本" }.into()),
            );
        } else {
            self.finish("pending", Some(format!("现在运行的是 {current}，既不是原版本也不是目标版本")));
        }
    }
}

fn journal_dir(app: &AppHandle) -> Result<PathBuf> {
    let dir = super::data_dir(app)?.join("updates");
    std::fs::create_dir_all(&dir).map_err(io)?;
    Ok(dir)
}

fn save_journal(app: &AppHandle, journal: &Journal) -> Result<()> {
    write_json(&journal_dir(app)?.join(format!("{}.json", journal.op_id)), journal)
}

/// 最近的更新记录，新的在前。
fn journals(dir: &Path) -> Vec<Journal> {
    let mut found: Vec<Journal> = std::fs::read_dir(dir)
        .map(|entries| {
            entries
                .flatten()
                .filter(|entry| entry.path().extension().is_some_and(|ext| ext == "json"))
                .filter_map(|entry| std::fs::read(entry.path()).ok())
                .filter_map(|bytes| serde_json::from_slice::<Journal>(&bytes).ok())
                .collect()
        })
        .unwrap_or_default();
    found.sort_by(|a, b| b.started_at.cmp(&a.started_at));
    found
}

// ---------------------------------------------------------------- 发行清单与兼容

/// 用 updater 的同一把公钥核对 minisign 签名。公钥与签名都是 Tauri 的写法：base64 包着 minisign 的文本格式。
fn verify_signature(data: &[u8], signature: &str, pubkey: &str) -> std::result::Result<(), String> {
    let text = |value: &str| -> std::result::Result<String, String> {
        let bytes = base64::engine::general_purpose::STANDARD.decode(value.trim()).map_err(|e| e.to_string())?;
        String::from_utf8(bytes).map_err(|e| e.to_string())
    };
    let key = minisign_verify::PublicKey::decode(&text(pubkey)?).map_err(|e| e.to_string())?;
    let signature = minisign_verify::Signature::decode(&text(signature)?).map_err(|e| e.to_string())?;
    key.verify(data, &signature, false).map_err(|e| e.to_string())
}

/// 这个桌面端说的 controller API 版本（`/api/v1`）。
const API_VERSION: u64 = 1;

/// 新版本桌面端对当前 controller 的兼容判断（ADR 0004 第 4 条）。`features` 是当前连接的 controller 提供的 features；
/// 没有打开的连接时为 None，结论是 unknown（连接时再核对）。
fn compat(manifest: &Value, features: Option<&[String]>) -> Value {
    let list = |key: &str| -> Vec<String> {
        manifest["compat"][key].as_array().map(|items| items.iter().filter_map(|v| v.as_str().map(str::to_owned)).collect()).unwrap_or_default()
    };
    let api = manifest["protocol"]["api_version"].as_u64();
    if api.is_some_and(|version| version != API_VERSION) {
        return json!({ "verdict": "reject", "reason": format!("新版本使用 controller API v{}，与现在的 v{API_VERSION} 不兼容", api.unwrap_or(0)) });
    }
    let Some(features) = features else {
        return json!({ "verdict": "unknown", "reason": "没有打开的连接；连接到 controller 时再核对" });
    };
    let missing = |names: Vec<String>| names.into_iter().filter(|name| !features.contains(name)).collect::<Vec<_>>();
    let (required, optional) = (missing(list("client_requires")), missing(list("client_optional")));
    if !required.is_empty() {
        json!({ "verdict": "reject", "missing_required": required, "missing_optional": optional,
                "reason": "当前 controller 缺少新版本桌面端必需的功能；先在 controller 所在机器上升级它" })
    } else if !optional.is_empty() {
        json!({ "verdict": "degraded", "missing_optional": optional, "reason": "当前 controller 较旧，新版本里依赖这些功能的操作不可用" })
    } else {
        json!({ "verdict": "accept" })
    }
}

const DOWNLOAD_TIMEOUT: Duration = Duration::from_secs(30 * 60);

fn this_platform() -> String {
    format!("{}-{}", if cfg!(windows) { "windows" } else if cfg!(target_os = "macos") { "darwin" } else { "linux" }, std::env::consts::ARCH)
}

/// 下载的字节必须是产品发行清单里本平台的更新包（大小与 SHA-256 一致）。
fn matches_manifest(manifest: &Value, bytes: &[u8]) -> std::result::Result<(), String> {
    use sha2::Digest;
    let artifacts = manifest["platforms"][this_platform()]["artifacts"].as_array().cloned().unwrap_or_default();
    let item = artifacts
        .iter()
        .find(|a| matches!(a["role"].as_str(), Some("updater" | "installer+updater")))
        .ok_or_else(|| "产品发行清单里没有本平台的更新包".to_string())?;
    let digest = super::hex(&sha2::Sha256::digest(bytes));
    if item["sha256"].as_str() != Some(digest.as_str()) || item["bytes"].as_u64() != Some(bytes.len() as u64) {
        return Err("下载的更新包与产品发行清单记录的大小或 SHA-256 不符".into());
    }
    Ok(())
}

/// 页面需要的发行清单摘要：组件版本、本平台的最低系统与产物、签名与公证状态。
fn summary(manifest: &Value) -> Value {
    let platform = this_platform();
    json!({
        "release_id": manifest["release_id"],
        "kind": manifest["identity"]["kind"],
        "channel": manifest["channel"],
        "source": manifest["source"],
        "components": manifest["components"],
        "platform": manifest["platforms"][&platform],
        "compat": manifest["compat"],
    })
}

// ---------------------------------------------------------------- 状态与命令

struct Offer {
    update: Update,
    manifest: Value,
    /// 检查时对当前 controller 的兼容结论；reject 时不下载也不安装（不只靠页面的按钮状态）。
    compat: Value,
    /// 检查时的通道；之后改了通道，这次的结果与已下载的包都不再有效。
    channel: String,
    /// 兼容结论所依据的 controller features（没有连接时为 None）；连接换了，结论就要重新得出。
    features: Option<Vec<String>>,
}

/// 下载与安装前核对：兼容结论针对的仍是现在连接的 controller。
fn same_controller(offer: &Offer, features: &Option<Vec<String>>) -> Result<()> {
    if &offer.features != features {
        return Err(invalid("连接的 controller 已经改变，先重新检查更新"));
    }
    Ok(())
}

/// 下载与安装前核对：这次检查的通道仍是当前设置的通道。
fn same_channel(app: &AppHandle, offer: &Offer) -> Result<()> {
    if offer.channel != load_settings(app).channel {
        return Err(invalid("更新通道已经改变，先重新检查更新"));
    }
    Ok(())
}

fn refused(offer: &Offer) -> Option<BridgeError> {
    (offer.compat["verdict"] == "reject").then(|| {
        err("incompatible", format!("新版本与当前 controller 不兼容：{}", offer.compat["reason"].as_str().unwrap_or("缺少必需的功能")))
    })
}

#[derive(Default)]
pub struct Updates {
    offer: Mutex<Option<Offer>>,
    /// 已下载并验签的更新包：版本、所属通道与字节。
    bytes: Mutex<Option<(String, String, Vec<u8>)>>,
    last_check: Mutex<Option<Value>>,
    /// 每次换通道加一：下载开始时记下，完成时不同就作废（A→B→A 来回切换也算换过）。
    generation: std::sync::atomic::AtomicU64,
    /// 下载或安装进行中。
    running: tokio::sync::Mutex<()>,
}

fn lock<T>(mutex: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|e| e.into_inner())
}

fn pubkey(app: &AppHandle) -> String {
    app.config().plugins.0.get("updater").and_then(|c| c["pubkey"].as_str()).unwrap_or("").to_string()
}

fn current_version(app: &AppHandle) -> String {
    app.package_info().version.to_string()
}

fn identity(app: &AppHandle) -> Value {
    json!({
        "kind": KIND,
        "identifier": app.config().identifier,
        "channels": channels(),
        "configured": configured(&pubkey(app)),
    })
}

/// 安装前的安全空闲条件：没有进行中的项目操作（扫描、上传、下载、写入）与本机部署操作。
fn blockers(shell: &Shell) -> Vec<String> {
    let mut found = Vec::new();
    if let Some(name) = shell.ops.active() {
        found.push(format!("项目操作“{name}”正在进行"));
    }
    if shell.deploying.try_lock().is_err() {
        found.push("本机部署的安装或服务操作正在进行".into());
    }
    if shell.exports.load(std::sync::atomic::Ordering::SeqCst) > 0 {
        found.push("正在把备份导出到这台机器".into());
    }
    found
}

#[tauri::command]
pub fn update_status(app: AppHandle, shell: State<'_, Shell>, updates: State<'_, Updates>) -> Result<Value> {
    let offer = lock(&updates.offer).as_ref().map(|offer| {
        json!({ "version": offer.update.version, "summary": summary(&offer.manifest), "compat": offer.compat,
                "notes": offer.update.body, "channel": offer.channel })
    });
    let downloaded = lock(&updates.bytes).as_ref().map(|(version, _, bytes)| json!({ "version": version, "bytes": bytes.len() }));
    Ok(json!({
        "identity": identity(&app),
        "current": current_version(&app),
        "settings": load_settings(&app),
        "last_check": *lock(&updates.last_check),
        "offer": offer,
        "downloaded": downloaded,
        "blockers": blockers(&shell),
        "history": journals(&journal_dir(&app)?).into_iter().take(10).collect::<Vec<_>>(),
    }))
}

#[tauri::command(rename_all = "snake_case")]
pub fn update_settings(
    app: AppHandle,
    updates: State<'_, Updates>,
    channel: String,
    auto_check: bool,
    auto_download: bool,
    auto_install: bool,
) -> Result<Value> {
    if !channels().contains(&channel.as_str()) {
        return Err(invalid(format!("这个构建只有这些通道：{}", channels().join("、"))));
    }
    let changed = load_settings(&app).channel != channel;
    let settings = Settings { channel, auto_check, auto_download, auto_install };
    // 先写入新通道再作废缓存，并在持有下载缓存的锁时写入：完成的下载在同一把锁下核对通道，
    // 两边怎样交错都不会把旧通道的字节留在缓存里。
    let mut bytes = lock(&updates.bytes);
    write_json(&settings_path(&app)?, &settings)?;
    if changed {
        // 换了通道：上一个通道检查到的版本与下载好的包都作废。
        updates.generation.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        *bytes = None;
        *lock(&updates.offer) = None;
        *lock(&updates.last_check) = None;
    }
    Ok(json!(settings))
}

async fn fetch(url: url::Url) -> std::result::Result<Vec<u8>, String> {
    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(CHECK_TIMEOUT)
        .build()
        .map_err(|e| e.to_string())?;
    let response = client.get(url.clone()).send().await.map_err(|e| e.to_string())?;
    if !response.status().is_success() {
        return Err(format!("{url} 返回 HTTP {}", response.status().as_u16()));
    }
    // 边读边数，超过上限就停：不把过大的响应整个读进内存。
    let mut response = response;
    let mut body = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(|e| e.to_string())? {
        if body.len() + chunk.len() > MANIFEST_LIMIT {
            return Err(format!("{url} 超过 1 MiB"));
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

/// 取回并核对新版本的产品发行清单：签名有效、发行 ID 等于 updater 宣布的版本、身份与本构建一致。
async fn manifest(base: &str, channel: &str, version: &str, pubkey: &str, identifier: &str) -> std::result::Result<Value, String> {
    let data = fetch(channel_url(base, channel, "product-manifest.json")?).await?;
    let signature = fetch(channel_url(base, channel, "product-manifest.json.sig")?).await?;
    verify_signature(&data, &String::from_utf8_lossy(&signature), pubkey).map_err(|e| format!("产品发行清单的签名无效：{e}"))?;
    let value: Value = serde_json::from_slice(&data).map_err(|e| format!("产品发行清单格式无效：{e}"))?;
    if value["release_id"].as_str() != Some(version) {
        return Err(format!("产品发行清单的版本（{}）与宣布的版本（{version}）不一致", value["release_id"]));
    }
    // 同一身份的各通道用同一把公钥：清单里签进去的通道也要是正在检查的这个。
    if value["channel"].as_str() != Some(channel) {
        return Err(format!("产品发行清单属于 {} 通道，不是现在检查的 {channel}", value["channel"]));
    }
    if value["identity"]["kind"].as_str() != Some(KIND) || value["identity"]["identifier"].as_str() != Some(identifier) {
        return Err("产品发行清单的构建身份与这个应用不一致".into());
    }
    Ok(value)
}

fn update_error(e: tauri_plugin_updater::Error) -> BridgeError {
    err("network", format!("更新失败：{e}"))
}

#[tauri::command]
pub async fn update_check(app: AppHandle, updates: State<'_, Updates>, features: Option<Vec<String>>) -> Result<Value> {
    let key = pubkey(&app);
    if !configured(&key) {
        return Err(err("not_configured", "这个构建没有配置更新通道"));
    }
    let settings = load_settings(&app);
    let base = UPDATE_BASE.unwrap_or_default();
    let endpoint = channel_url(base, &settings.channel, "latest.json").map_err(invalid)?;
    let updater = app.updater_builder().endpoints(vec![endpoint]).map_err(update_error)?.timeout(CHECK_TIMEOUT).build().map_err(update_error)?;
    let checked_at = now_iso();
    let found = updater.check().await.map_err(update_error)?;
    let result = match found {
        None => {
            *lock(&updates.offer) = None;
            json!({ "available": false, "current": current_version(&app), "channel": settings.channel, "checked_at": checked_at })
        }
        Some(update) => {
            let manifest = manifest(base, &settings.channel, &update.version, &key, &app.config().identifier).await.map_err(|e| err("invalid", e))?;
            let verdict = compat(&manifest, features.as_deref());
            let result = json!({
                "available": true,
                "current": current_version(&app),
                "version": update.version,
                "notes": update.body,
                "date": update.date.map(|d| d.to_string()),
                "channel": settings.channel,
                "checked_at": checked_at,
                "summary": summary(&manifest),
                "compat": verdict,
            });
            *lock(&updates.offer) = Some(Offer { update, manifest, compat: verdict, channel: settings.channel.clone(), features });
            result
        }
    };
    *lock(&updates.last_check) = Some(result.clone());
    Ok(result)
}

#[tauri::command]
pub async fn update_download(app: AppHandle, updates: State<'_, Updates>, features: Option<Vec<String>>) -> Result<Value> {
    let _running = updates.running.try_lock().map_err(|_| err("busy", "更新的下载或安装正在进行"))?;
    let update = {
        let offer = lock(&updates.offer);
        let offer = offer.as_ref().ok_or_else(|| invalid("没有可下载的更新，先检查更新"))?;
        if let Some(e) = refused(offer) {
            return Err(e);
        }
        same_channel(&app, offer)?;
        same_controller(offer, &features)?;
        (offer.update.clone(), offer.channel.clone(), offer.manifest.clone(), updates.generation.load(std::sync::atomic::Ordering::SeqCst))
    };
    let (update, channel, offer_manifest, generation) = update;
    let mut journal = Journal::new(&current_version(&app), &update.version, &load_settings(&app).channel);
    journal.mark("compat", "done", None);
    journal.mark("download", "running", None);
    save_journal(&app, &journal)?;
    let (mut done, events) = (0u64, app.clone());
    // 插件建 Update 时不带超时：服务器停住不发数据时下载会一直挂着并占着 running。
    let mut update = update;
    update.timeout = Some(DOWNLOAD_TIMEOUT);
    let downloaded = update
        .download(
            |chunk, total| {
                done += chunk as u64;
                let _ = events.emit("ac-update", json!({ "phase": "download", "done": done, "total": total }));
            },
            || {},
        )
        .await;
    match downloaded {
        Ok(bytes) => {
            // download() 已用公钥核对过签名与签名里的版本；再核对字节就是已验签的产品清单里本平台的那个更新包。
            if let Err(e) = matches_manifest(&offer_manifest, &bytes) {
                journal.mark("verify", "failed", Some(e.clone()));
                journal.finish("failed", Some("下载的更新包与产品发行清单不符".into()));
                save_journal(&app, &journal)?;
                return Err(err("invalid", e));
            }
            journal.mark("download", "done", Some(format!("{} 字节", bytes.len())));
            journal.mark("verify", "done", Some("签名有效".into()));
            journal.finish("running", None);
            save_journal(&app, &journal)?;
            let size = bytes.len();
            let mut slot = lock(&updates.bytes);
            if load_settings(&app).channel != channel || updates.generation.load(std::sync::atomic::Ordering::SeqCst) != generation {
                drop(slot);
                journal.finish("failed", Some("下载期间更新通道已经改变，本次下载作废".into()));
                save_journal(&app, &journal)?;
                return Err(invalid("下载期间更新通道已经改变，这次下载不再使用；先重新检查更新"));
            }
            *slot = Some((update.version.clone(), channel, bytes));
            Ok(json!({ "version": update.version, "bytes": size, "op_id": journal.op_id }))
        }
        Err(e) => {
            let failed = if matches!(e, tauri_plugin_updater::Error::Minisign(_) | tauri_plugin_updater::Error::SignedVersionMismatch { .. } | tauri_plugin_updater::Error::MissingSignedVersion) { "verify" } else { "download" };
            if failed == "verify" {
                journal.mark("download", "done", None); // 内容已下载完，失败在验签
            }
            journal.mark(failed, "failed", Some(e.to_string()));
            journal.finish("failed", Some("下载或验签失败，没有安装，仍在使用原版本".into()));
            save_journal(&app, &journal)?;
            Err(update_error(e))
        }
    }
}

#[tauri::command]
pub async fn update_install(app: AppHandle, shell: State<'_, Shell>, updates: State<'_, Updates>, features: Option<Vec<String>>) -> Result<Value> {
    let _running = updates.running.try_lock().map_err(|_| err("busy", "更新的下载或安装正在进行"))?;
    // 先关上备份导出的准入，再看空闲条件：之后开始的导出会被拒绝，已经开始的会让这次安装以 busy 结束。
    let _installing = super::Installing::start(&shell);
    let blocked = blockers(&shell);
    if !blocked.is_empty() {
        return Err(err("busy", format!("现在不能安装：{}", blocked.join("；"))));
    }
    let update = {
        let offer = lock(&updates.offer);
        let offer = offer.as_ref().ok_or_else(|| invalid("没有可安装的更新"))?;
        if let Some(e) = refused(offer) {
            return Err(e);
        }
        same_channel(&app, offer)?;
        same_controller(offer, &features)?;
        (offer.update.clone(), offer.channel.clone(), updates.generation.load(std::sync::atomic::Ordering::SeqCst))
    };
    let (update, channel, generation) = update;
    // 缓存的字节必须来自同一个通道的同一个版本。
    let bytes = lock(&updates.bytes).take().filter(|(version, from, _)| *version == update.version && *from == channel).map(|(_, _, bytes)| bytes);
    let Some(bytes) = bytes else {
        return Err(invalid("这个版本还没有下载"));
    };
    // 安装程序要替换随包运行时，正在运行的本机助手占着它的文件（Windows 上写不进去）：关闸，等它们结束（最多 60 秒，
    // 等不到就这次不安装、不结束它们）；安装程序没能接管时重新开闸（helper::Gate::exclusive）。
    let version = update.version.clone();
    let installing = tauri::async_runtime::spawn_blocking(move || {
        let result = helper::GATE.exclusive(Duration::from_secs(60), || {
            // 等助手的这段时间里可能开始了新的操作（例如备份导出）：关闸之后再核对一次空闲条件。
            let blocked = blockers(&app.state::<Shell>());
            if !blocked.is_empty() {
                return Err(err("busy", format!("现在不能安装：{}", blocked.join("；"))));
            }
            let mut journal = journals(&journal_dir(&app)?)
                .into_iter()
                .find(|journal| journal.status == "running" && journal.to == update.version)
                .unwrap_or_else(|| Journal::new(&current_version(&app), &update.version, &load_settings(&app).channel));
            journal.mark("prepare", "done", Some("没有进行中的项目操作与本机部署操作，本机助手已全部结束".into()));
            journal.mark("backup", "skipped", Some("桌面更新不改动数据".into()));
            journal.mark("switch", "running", None);
            save_journal(&app, &journal)?;
            // Windows：install() 启动安装程序后结束本进程，安装完成后由安装程序重新启动应用，启动时对账。
            // macOS：就地替换应用包，然后重新启动。
            if let Err(e) = update.install(&bytes) {
                journal.mark("switch", "failed", Some(e.to_string()));
                journal.finish("failed", Some("安装失败，仍在使用原版本".into()));
                save_journal(&app, &journal)?;
                return Err(update_error(e));
            }
            Ok(app)
        });
        (result, bytes)
    })
    .await;
    match installing {
        Ok((Ok(app), _)) => app.restart(),
        Ok((Err(e), bytes)) => {
            // 没有安装（等助手超时，或安装程序没能接管）：已下载并验签的更新留着，稍后可以直接再安装。
            // 等待期间换过通道（包括来回切换）就不放回：缓存只属于当时的那个通道代次。
            let mut slot = lock(&updates.bytes);
            if updates.generation.load(std::sync::atomic::Ordering::SeqCst) == generation {
                *slot = Some((version, channel, bytes));
            }
            Err(e)
        }
        Err(e) => {
            helper::GATE.reopen();
            Err(io(format!("安装没有完成：{e}")))
        }
    }
}

/// 启动时：对账停在 running 的更新记录；按设置安排自动检查。
pub fn startup(app: &AppHandle) {
    if let Ok(dir) = journal_dir(app) {
        let current = current_version(app);
        let payload = bundle(app).payload;
        for mut journal in journals(&dir).into_iter().filter(|journal| journal.status == "running" || journal.status == "pending") {
            journal.reconcile(&current, payload.clone());
            let _ = save_journal(app, &journal);
        }
    }
    if !configured(&pubkey(app)) {
        return;
    }
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(Duration::from_secs(20)).await;
        loop {
            if load_settings(&app).auto_check {
                let _ = app.emit("ac-update", json!({ "phase": "auto_check" }));
            }
            tokio::time::sleep(Duration::from_secs(24 * 3600)).await;
        }
    });
}

// ---------------------------------------------------------------- 后台版本比较

/// 本项目接受的 PEP 440 子集：`X.Y.Z[{a|b|rc}N][.devN]`。返回可比较的键；不认识的写法返回 None（不按字符串比较）。
pub fn pep440_key(version: &str) -> Option<(Vec<u64>, u8, u64, u8, u64)> {
    let (rest, dev) = match version.split_once(".dev") {
        Some((rest, dev)) => (rest, Some(dev.parse::<u64>().ok()?)),
        None => (version, None),
    };
    let split = rest.find(|c: char| c.is_ascii_alphabetic()).unwrap_or(rest.len());
    let (release, pre) = rest.split_at(split);
    let numbers: Vec<u64> = release.split('.').map(|n| n.parse::<u64>().ok()).collect::<Option<_>>()?;
    if numbers.len() != 3 {
        return None;
    }
    // 预发布阶段：a < b < rc < 正式版；只有 .devN（没有 a/b/rc）的版本排在该版本全部预发布之前。
    let (phase, pre_n) = match pre {
        "" => (if dev.is_some() { 0 } else { 4 }, 0),
        _ => {
            let digits = pre.find(|c: char| c.is_ascii_digit())?;
            let (tag, n) = pre.split_at(digits);
            let phase = match tag {
                "a" => 1,
                "b" => 2,
                "rc" => 3,
                _ => return None,
            };
            (phase, n.parse::<u64>().ok()?)
        }
    };
    // 同一预发布内：.devN 早于不带 dev 的那一个。
    let (dev_flag, dev_n) = match dev {
        Some(n) => (0, n),
        None => (1, 0),
    };
    Some((numbers, phase, pre_n, dev_flag, dev_n))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn downloaded_bytes_must_match_the_manifest_entry_for_this_platform() {
        use sha2::Digest;
        let bytes = b"update-bytes";
        let manifest = json!({ "platforms": { this_platform(): { "artifacts": [
            { "role": "updater", "bytes": bytes.len(), "sha256": crate::hex(&sha2::Sha256::digest(bytes)) } ] } } });
        assert!(matches_manifest(&manifest, bytes).is_ok());
        assert!(matches_manifest(&manifest, b"other-bytes!").is_err());
        assert!(matches_manifest(&json!({ "platforms": {} }), bytes).is_err());
    }

    #[test]
    fn pep440_order_is_semantic_not_lexical() {
        let order = ["0.1.0rc9", "0.1.0rc10", "0.2.0.dev2", "0.2.0.dev10", "0.2.0a1", "0.2.0b1.dev1", "0.2.0b1", "0.2.0rc1", "0.2.0", "0.10.0"];
        for pair in order.windows(2) {
            assert!(pep440_key(pair[0]).unwrap() < pep440_key(pair[1]).unwrap(), "{} < {}", pair[0], pair[1]);
        }
        for bad in ["", "1.2", "1.2.3.4", "1.2.3x1", "1.2.3rc", "1.2.3.devx", "v1.2.3"] {
            assert!(pep440_key(bad).is_none(), "{bad}");
        }
    }

    fn manifest(required: &[&str], optional: &[&str], api: u64) -> Value {
        json!({ "protocol": { "api_version": api }, "compat": { "client_requires": required, "client_optional": optional } })
    }

    #[test]
    fn compat_rejects_missing_required_and_degrades_on_optional() {
        let have: Vec<String> = ["clients", "projects"].map(String::from).into();
        assert_eq!(compat(&manifest(&["clients"], &["projects"], 1), Some(&have))["verdict"], "accept");
        assert_eq!(compat(&manifest(&["clients"], &["maintenance"], 1), Some(&have))["verdict"], "degraded");
        let rejected = compat(&manifest(&["clients", "model_settings"], &[], 1), Some(&have));
        assert_eq!(rejected["verdict"], "reject");
        assert_eq!(rejected["missing_required"], json!(["model_settings"]));
        assert_eq!(compat(&manifest(&[], &[], 2), Some(&have))["verdict"], "reject");
        assert_eq!(compat(&manifest(&["clients"], &[], 1), None)["verdict"], "unknown");
    }

    #[test]
    fn reconcile_reads_the_running_version_instead_of_guessing() {
        let started = || {
            let mut journal = Journal::new("0.3.0-dev.1", "0.3.0-dev.2", "stable");
            journal.mark("switch", "running", None);
            journal
        };
        let mut done = started();
        done.reconcile("0.3.0-dev.2", Ok(()));
        assert_eq!((done.status.as_str(), done.step("health").unwrap().status.as_str()), ("done", "done"));

        let mut broken = started();
        broken.reconcile("0.3.0-dev.2", Err("缺少文件 app/x.py".into()));
        assert_eq!(broken.status, "pending");

        let mut old = started();
        old.reconcile("0.3.0-dev.1", Ok(()));
        assert_eq!((old.status.as_str(), old.step("switch").unwrap().status.as_str()), ("failed", "failed"));

        let mut never = Journal::new("0.3.0-dev.1", "0.3.0-dev.2", "stable");
        never.reconcile("0.3.0-dev.1", Ok(()));
        assert_eq!((never.status.as_str(), never.step("switch").unwrap().status.as_str()), ("failed", "pending"));

        let mut other = started();
        other.reconcile("0.3.0-dev.9", Ok(()));
        assert_eq!(other.status, "pending");

        let mut settled = started();
        settled.finish("failed", None);
        settled.reconcile("0.3.0-dev.2", Ok(()));
        assert_eq!(settled.status, "failed");

        // 载荷不完整的切换：修复之前保持 pending，重新安装修复之后下次启动记为完成。
        let mut repaired = started();
        repaired.reconcile("0.3.0-dev.2", Err("缺少 546 个清单里的文件".into()));
        repaired.reconcile("0.3.0-dev.2", Err("缺少 546 个清单里的文件".into()));
        assert_eq!(repaired.status, "pending");
        repaired.reconcile("0.3.0-dev.2", Ok(()));
        assert_eq!((repaired.status.as_str(), repaired.step("health").unwrap().status.as_str()), ("done", "done"));
    }
}
