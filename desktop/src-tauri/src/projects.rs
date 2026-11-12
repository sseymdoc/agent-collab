//! 项目目录、输入上传与成果应用（`docs/desktop.md`“项目目录、输入与成果”）。
//!
//! 项目对应这台机器上的哪个目录只记在本机的 `projects.json` 里，目录只能经系统的选择文件夹对话框进入这张表：
//! 页面只给 `project_id` 或 `job_id`，外壳自己查表得到目录。读写目录由本机助手完成（规则与 CLI 共用
//! `agent_collab/project.py`）；需要凭据的上传与下载由外壳完成，助手只看到暂存目录里按内容哈希命名的文件。

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::ffi::OsString;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use reqwest::header::{CONTENT_LENGTH, CONTENT_TYPE};
use reqwest::{Method, StatusCode};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Emitter, Manager, State, WebviewWindow};

use super::helper::{self, Helper};
use super::{
    bad_response, bundle, data_dir, device_name, dialog, err, helper_unavailable, hex, http_error, invalid, io, net, now_iso, read_body,
    validate, write_json, Active, BlobSink, BridgeError, Inner, Result, Shell, MAX_BODY,
};

/// 快照只在扫描之后、上传之前有用；超过这个时间的在下次扫描时删除。
const SNAPSHOT_TTL: Duration = Duration::from_secs(7 * 24 * 3600);
/// 应用结果里上报给 controller 的字段，与 CLI 的 `finish_application` 相同。备份目录是本机绝对路径，不上报。
const APPLY_DETAIL: &[&str] = &["changed", "already", "pending", "conflicts", "errors", "restored", "kept", "written", "remaining", "diverged", "cancelled"];
/// 恢复结果里上报的字段，与 CLI 的 `project recover` 相同。
const RECOVER_DETAIL: &[&str] = &["changed", "pending", "conflicts", "errors", "restored", "kept", "written", "remaining", "diverged", "cancelled"];

fn not_bound() -> BridgeError {
    err("not_bound", "这个项目在这台机器上还没有选定目录")
}

fn require_uuid(value: &str, name: &str) -> Result<()> {
    if validate::uuid(value) {
        Ok(())
    } else {
        Err(invalid(format!("{name} 必须是小写 UUID")))
    }
}

fn new_id() -> String {
    uuid::Uuid::new_v4().to_string()
}

/// 助手命令行里的一个参数。
fn os(value: impl Into<OsString>) -> OsString {
    value.into()
}

/// 路径的最后一段；盘符根目录这类没有名称的路径返回空串。
fn dir_name(path: &str) -> String {
    Path::new(path).file_name().map(|name| name.to_string_lossy().into_owned()).unwrap_or_default()
}

/// JSON 数组里的字符串；不是数组时为空。
fn strings(value: &Value) -> Vec<String> {
    value.as_array().into_iter().flatten().filter_map(|item| item.as_str().map(str::to_owned)).collect()
}

fn items(value: &Value) -> &[Value] {
    value.as_array().map(Vec::as_slice).unwrap_or_default()
}

// ---------------------------------------------------------------- 绑定表

/// `projects.json` 里的一项绑定。
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
struct Binding {
    path: String,
    bound_at: String,
}

impl Binding {
    /// 返回给页面的 binding：只有目录名，不给绝对路径（页面只显示名字）；`exists` 是目录此刻是否还在。
    fn view(&self, project_id: &str) -> Value {
        let name = Some(dir_name(&self.path)).filter(|name| !name.is_empty()).unwrap_or_else(|| "root".into());
        json!({ "project_id": project_id, "name": name, "bound_at": self.bound_at, "exists": Path::new(&self.path).is_dir() })
    }
}

/// `projects.json`：`{"<controller_id>": {"<project_id>": {"path", "bound_at"}}}`。不含凭据，不上传。
#[derive(Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(transparent)]
struct Bindings(BTreeMap<String, BTreeMap<String, Binding>>);

impl Bindings {
    fn get(&self, controller_id: &str, project_id: &str) -> Option<&Binding> {
        self.0.get(controller_id)?.get(project_id)
    }

    fn set(&mut self, controller_id: &str, project_id: &str, binding: Binding) {
        self.0.entry(controller_id.to_string()).or_default().insert(project_id.to_string(), binding);
    }

    fn remove(&mut self, controller_id: &str, project_id: &str) {
        if let Some(projects) = self.0.get_mut(controller_id) {
            projects.remove(project_id);
            if projects.is_empty() {
                self.0.remove(controller_id);
            }
        }
    }

    /// 一个 controller 的全部绑定。
    fn views(&self, controller_id: &str) -> Vec<Value> {
        self.0.get(controller_id).into_iter().flatten().map(|(project_id, binding)| binding.view(project_id)).collect()
    }
}

fn bindings_path(app: &AppHandle) -> Result<PathBuf> {
    Ok(app.path().app_config_dir().map_err(io)?.join("projects.json"))
}

/// 读取绑定表。与 `connections.json` 一样，读写都在状态锁内串行（`_held`）。
fn load_bindings(app: &AppHandle, _held: &Inner) -> Result<Bindings> {
    match std::fs::read(bindings_path(app)?) {
        Ok(bytes) => serde_json::from_slice(&bytes).map_err(|e| io(format!("projects.json 无法解析：{e}"))),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Bindings::default()),
        Err(e) => Err(io(format!("读取 projects.json 失败：{e}"))),
    }
}

fn save_bindings(app: &AppHandle, _held: &Inner, bindings: &Bindings) -> Result<()> {
    write_json(&bindings_path(app)?, bindings)
}

// ---------------------------------------------------------------- 同一时间只运行一项操作

/// 正在进行的项目操作。
struct Op {
    epoch: u64,
    /// 操作种类（`ac-project` 的 `op`）。
    name: &'static str,
    /// 最近一次进度的阶段；取消请求的应答据此说明会在哪里停下。
    phase: Mutex<String>,
    cancelled: AtomicBool,
}

impl Op {
    fn set_phase(&self, phase: &str) {
        *self.phase.lock().unwrap_or_else(|e| e.into_inner()) = phase.to_string();
    }

    fn phase(&self) -> String {
        self.phase.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }
}

/// 项目操作的独占位：扫描、上传、下载、应用同一时间只运行一项。
#[derive(Default)]
pub struct Ops(Mutex<Option<Arc<Op>>>);

impl Ops {
    fn slot(&self) -> MutexGuard<'_, Option<Arc<Op>>> {
        self.0.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// 占位；已有操作在进行时返回 `busy`。返回值离开作用域即释放。
    fn begin(&self, epoch: u64, name: &'static str) -> Result<Running<'_>> {
        let mut slot = self.slot();
        if slot.is_some() {
            return Err(err("busy", "已有一项项目操作在进行，等它结束或取消后再试"));
        }
        let op = Arc::new(Op { epoch, name, phase: Mutex::new(String::new()), cancelled: AtomicBool::new(false) });
        *slot = Some(op.clone());
        Ok(Running { ops: self, op })
    }

    /// 给这一代连接上正在进行的操作置取消标志；操作在下一个核对点停下（写入阶段：当前文件写完之后）。
    /// 返回被取消的操作与它当时的阶段；没有操作在进行时返回 None。
    fn cancel(&self, epoch: u64) -> Option<(&'static str, String)> {
        let slot = self.slot();
        let op = slot.as_ref().filter(|op| op.epoch == epoch)?;
        op.cancelled.store(true, Ordering::SeqCst);
        Some((op.name, op.phase()))
    }
}

/// 核对点上对助手做什么。
#[derive(Debug, PartialEq)]
enum Stop {
    Continue,
    /// 经取消文件通知助手，由它在安全的位置停下。
    Signal,
    /// 结束助手进程（只用于只读的动作）。
    Kill,
}

/// 取消请求与连接切换在各种运行方式下的处理（纯逻辑，单元测试覆盖）。
fn stop_action(mode: Mode, cancelled: bool, stale: bool, has_channel: bool) -> Stop {
    let wants = match mode {
        Mode::ReadOnly | Mode::Cancellable => cancelled || stale,
        Mode::Writes => cancelled, // 切换连接不打断写入
        Mode::Finish => false,
    };
    match (wants, has_channel) {
        (false, _) => Stop::Continue,
        (true, true) => Stop::Signal,
        (true, false) if mode == Mode::ReadOnly => Stop::Kill,
        (true, false) => Stop::Continue,
    }
}

/// 助手的运行方式：取消与连接切换时怎样停下。
#[derive(Clone, Copy, PartialEq, Debug)]
enum Mode {
    /// 只读（plan、unfinished、恢复的 status）：停下时可以直接结束助手，没有东西写到一半。
    ReadOnly,
    /// 扫描与暂存：经取消文件通知助手，它在下一个文件处停下并以 `cancelled` 结束；连接切换同样停下。
    Cancellable,
    /// 写入项目目录（apply、recover 的 resume/rollback/abandon）：只有取消请求通知助手，它写完当前文件后停下并如实返回
    /// 部分结果；连接切换不打断写入，结果照常报给发起它的 controller。从不结束助手。
    Writes,
    /// 整包下载的落盘：很快，不打断、不结束助手。
    Finish,
}

struct Running<'a> {
    ops: &'a Ops,
    op: Arc<Op>,
}

impl Drop for Running<'_> {
    fn drop(&mut self) {
        *self.ops.slot() = None;
    }
}

/// 操作该不该在这个核对点停下：被取消，或它所属的连接已经切换、关闭。
fn stop_reason(shell: &Shell, epoch: u64, op: &Op) -> Option<BridgeError> {
    if op.cancelled.load(Ordering::SeqCst) {
        return Some(err("cancelled", "操作已取消"));
    }
    shell.lock().current(epoch).err()
}

// ---------------------------------------------------------------- 进度事件

/// 事件 `ac-project` 的负载。
#[derive(Clone, Debug, PartialEq, Serialize)]
struct Progress {
    epoch: u64,
    op: &'static str,
    phase: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    done: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    total: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    files: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    bytes: Option<u64>,
}

/// 助手的一行进度对应的事件负载：只转发契约里的字段；没有 `phase` 的不转发。
fn progress(epoch: u64, op: &'static str, value: &Value) -> Option<Progress> {
    let number = |key: &str| value[key].as_u64();
    let phase = value["phase"].as_str()?.chars().take(64).collect();
    Some(Progress { epoch, op, phase, done: number("done"), total: number("total"), files: number("files"), bytes: number("bytes") })
}

/// 只在这一代连接仍是当前连接时发事件；检查与发送在同一把锁内。
fn emit(app: &AppHandle, payload: Progress) {
    let shell = app.state::<Shell>();
    let inner = shell.lock();
    if inner.epoch == payload.epoch {
        let _ = app.emit_to("main", "ac-project", payload);
    }
}

// ---------------------------------------------------------------- 暂存目录

/// 一次操作的暂存目录 `<数据目录>/projects/staging/<随机>`：给助手的 JSON 文件，以及 `blobs/` 下按内容哈希命名的文件。
/// 操作结束（成功、失败、取消）时整个删除。
// ponytail: 在异步任务里同步删除；暂存了上 GB 内容时会占住一个工作线程几秒，成问题再挪到阻塞线程。
struct Staging(PathBuf);

impl Staging {
    fn create(state: &Path) -> Result<Self> {
        let dir = state.join("staging").join(new_id());
        std::fs::create_dir_all(dir.join("blobs")).map_err(io)?;
        Ok(Self(dir))
    }

    fn blobs(&self) -> PathBuf {
        self.0.join("blobs")
    }
}

impl Drop for Staging {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// 启动时清掉上次运行被中断（进程被结束、掉电）留下的暂存文件。此时没有操作在进行。
pub fn clear_staging(app: &AppHandle) {
    if let Ok(dir) = app.path().app_local_data_dir() {
        let _ = std::fs::remove_dir_all(dir.join("projects").join("staging"));
    }
}

/// 删掉过期的快照，免得每次扫描留下的文件清单越积越多。
fn prune_snapshots(state: &Path) {
    let Ok(entries) = std::fs::read_dir(state.join("snapshots")) else { return };
    for entry in entries.flatten() {
        let age = entry.metadata().and_then(|meta| meta.modified()).ok().and_then(|at| at.elapsed().ok());
        if age.is_some_and(|age| age > SNAPSHOT_TTL) {
            let _ = std::fs::remove_file(entry.path());
        }
    }
}

// ---------------------------------------------------------------- 纯逻辑：请求体、视图与清单

/// 由内容派生的幂等键，与 `agent_collab/project_cli.py` 的 `stable_request_id` 逐位相同：
/// 各段用换行连接后取 SHA-256 的前 16 字节，按 UUID v4 设置版本位与变体位。
/// 同一份内容从 CLI 或桌面端重复登记得到同一个 request_id，不会产生第二个业务对象。
/// 各段必须覆盖请求体里所有会变的东西：同一个 request_id 配不同的请求体会被 controller 以 409 拒绝。
fn stable_request_id(parts: &[&str]) -> String {
    let digest = Sha256::digest(parts.join("\n").as_bytes());
    let mut bytes = [0u8; 16];
    bytes.copy_from_slice(&digest[..16]);
    uuid::Builder::from_random_bytes(bytes).into_uuid().to_string()
}

/// 与 Python 的 `sys.platform` 相同的取值，基线来源里 CLI 与桌面端写法一致。
fn platform() -> &'static str {
    match std::env::consts::OS {
        "windows" => "win32",
        "macos" => "darwin",
        other => other,
    }
}

/// `POST /baselines` 的请求体。`source` 只带设备名、目录名等说明，不带本机路径。
fn baseline_request(project_id: &str, project_name: &str, snapshot: &Value, device: &str) -> Value {
    let scanned_at = snapshot["scanned_at"].as_str().unwrap_or_default();
    // controller 限制名称不超过 120 个字符：项目名太长时截短，扫描时间保留。
    let room = 120usize.saturating_sub(scanned_at.chars().count() + 3);
    let name = format!("{} @ {scanned_at}", project_name.chars().take(room).collect::<String>());
    json!({
        // 请求号由这次扫描（快照）的身份派生，不由内容或扫描时间派生：内容相同的两次扫描是两次登记。
        "request_id": stable_request_id(&["baseline", project_id, snapshot["snapshot_id"].as_str().unwrap_or_default()]),
        "name": name,
        "manifest": snapshot["manifest"],
        "project_id": project_id,
        "source": {
            "device": device,
            "platform": platform(),
            "root_name": dir_name(snapshot["root"].as_str().unwrap_or_default()),
            "git": snapshot["git"],
            "exclude": snapshot["exclude"],
            "excluded": snapshot["excluded"]["count"],
            "skipped": snapshot["skipped_counts"],
            "scanned_at": snapshot["scanned_at"],
        },
    })
}

/// `project_scan` 的返回值：将上传的范围。文件清单本身留在本机快照里，不给页面；目录只给名称。
fn scan_view(result: &Value) -> Value {
    let mut view = Map::new();
    view.insert("root_name".into(), json!(dir_name(result["root"].as_str().unwrap_or_default())));
    for key in ["snapshot_id", "files", "bytes", "manifest_bytes", "manifest_sha256", "exclude", "excluded", "skipped", "skipped_counts", "git", "scanned_at"] {
        view.insert(key.into(), result[key].clone());
    }
    Value::Object(view)
}

/// 项目的排除规则在前、助手的缺省规则在后，去掉重复项（与 CLI 的 `effective_excludes` 相同）。
fn merge_excludes(own: Vec<String>, defaults: Vec<String>) -> Vec<String> {
    let mut seen = BTreeSet::new();
    own.into_iter().chain(defaults).filter(|pattern| seen.insert(pattern.clone())).collect()
}

/// 一组 manifest 文件条目引用的内容：sha256 → 大小（去重）。哈希会成为请求路径与暂存文件名，形状不对就拒绝。
fn blobs_of<'a>(entries: impl IntoIterator<Item = &'a Value>) -> Result<BTreeMap<String, u64>> {
    let mut blobs = BTreeMap::new();
    for entry in entries {
        match (entry["sha256"].as_str().filter(|sha| validate::sha256_hex(sha)), entry["size"].as_u64()) {
            (Some(sha), Some(size)) => blobs.insert(sha.to_string(), size),
            _ => return Err(io("文件清单里有无效的条目")),
        };
    }
    Ok(blobs)
}

fn manifest_of(files: &[Value]) -> Value {
    json!({ "version": 1, "base": null, "files": files, "deleted": [] })
}

/// 任务的输入基线 manifest（置 `base: null, deleted: []`；任务没有输入时是空 manifest）与契约里的排除规则。
fn base_and_exclude(job: &Value) -> (Value, Option<Value>) {
    let mut base = match &job["contract"]["inputs_manifest"] {
        Value::Object(manifest) => Value::Object(manifest.clone()),
        _ => manifest_of(&[]),
    };
    base["base"] = Value::Null;
    base["deleted"] = json!([]);
    (base, Some(&job["contract"]["workspace"]["exclude"]).filter(|rules| rules.is_array()).cloned())
}

/// 返回给页面的变更计划条目：只留契约里的字段（助手的计划还带着本机文件当前的哈希）。
fn change_view(change: &Value) -> Value {
    let mut view = json!({ "path": change["path"], "action": change["action"], "state": change["state"], "size": change["size"] });
    if !change["reason"].is_null() {
        view["reason"] = change["reason"].clone();
    }
    view
}

/// 不必写入就有结论的两种情况（与 `agent_collab/project.py` 的 `apply` 相同）：
/// 有冲突而用户没有选择跳过；没有可以应用的改动。这两种情况不登记应用。其余情况返回 None。
fn settled_without_writing(changes: &[Value], skip_conflicts: bool) -> Option<Value> {
    let paths = |state: &str| changes.iter().filter(|change| change["state"] == state).map(|change| change["path"].clone()).collect::<Vec<_>>();
    let conflicts: Vec<Value> =
        changes.iter().filter(|change| change["state"] == "conflict").map(|change| json!({ "path": change["path"], "reason": change["reason"] })).collect();
    let todo = paths("clean");
    let status = match (conflicts.is_empty(), todo.is_empty()) {
        (false, _) if !skip_conflicts => "conflict",
        (true, true) => "applied",
        (false, true) => "conflict",
        _ => return None,
    };
    Some(json!({
        "application_id": null, "status": status, "changed": [], "already": paths("already"), "pending": todo,
        "conflicts": conflicts, "errors": [], "reported": null,
    }))
}

/// `result_apply` 的返回值。
fn apply_view(result: &Value, application_id: &str, reported: Option<String>) -> Value {
    json!({
        "application_id": application_id, "status": result["status"], "changed": result["changed"], "already": result["already"],
        "pending": result["pending"], "conflicts": result["conflicts"], "errors": result["errors"],
        "cancelled": result["cancelled"].as_bool().unwrap_or(false), "reported": reported,
    })
}

/// 一个文件都没写、结果也已送达的应用：不留下只有来源记录的目录。还有待上报的回执时保留。
fn clear_if_settled(journal: &Path) {
    if !journal.join("journal.json").exists() && !journal.join("report.json").exists() {
        let _ = std::fs::remove_dir_all(journal);
    }
}

/// controller 只接受 256 KiB 以内的 detail。超出时每个列表只留前 100 项，并在 `truncated` 里记下原来的项数：
/// 结果状态比完整的路径列表重要，不能因为列表太长而报不上去。
fn fit_detail(mut detail: Value) -> Value {
    if detail.to_string().len() <= 200 * 1024 {
        return detail;
    }
    let mut truncated = Map::new();
    if let Some(fields) = detail.as_object_mut() {
        for (key, value) in fields.iter_mut() {
            if let Value::Array(list) = value {
                if list.len() > 100 {
                    truncated.insert(key.clone(), json!(list.len()));
                    list.truncate(100);
                }
            }
        }
        fields.insert("truncated".into(), Value::Object(truncated));
    }
    detail
}

/// 上报给 controller 的最终状态与明细。本机的 `rolled_back` 在 controller 上记为 `failed`（没有留下改动）。
fn application_report(result: &Value, keys: &[&str]) -> (String, Value) {
    let mut detail = Map::new();
    for key in keys {
        // null 与 false 都不上报（cancelled 只在为 true 时出现，与 CLI 相同）。
        if let Some(value) = result.get(key).filter(|value| !value.is_null() && **value != Value::Bool(false)) {
            detail.insert(key.to_string(), value.clone());
        }
    }
    let status = match result["status"].as_str() {
        Some("rolled_back") => {
            detail.insert("rolled_back".into(), json!(true));
            "failed"
        }
        Some(status) => status,
        None => "unknown",
    };
    (status.to_string(), fit_detail(Value::Object(detail)))
}

/// 续写一次被中断的应用还需要的内容：操作日志里尚未写入（`remaining`）且不是删除的文件。
fn remaining_blobs(journal: &Value, status: &Value) -> Result<BTreeMap<String, u64>> {
    let remaining: BTreeSet<&str> = items(&status["remaining"]).iter().filter_map(Value::as_str).collect();
    let paths = items(&journal["changes"]).iter().filter(|change| change["action"] != "delete").filter_map(|change| change["path"].as_str());
    blobs_of(paths.filter(|path| remaining.contains(path)).map(|path| &journal["files"][path]))
}

/// 助手列出的未完成应用 → 返回给页面的条目。只列在当前 controller 的连接下发起的（处理结果要报给登记它的那个
/// controller），以及没有留下来源记录的（只能在本机处理，`project_id`、`job_id` 为 null）。目录只给名称。
fn unfinished_view(entries: &[Value], controller_id: &str, context: impl Fn(&str) -> Option<Value>) -> Vec<Value> {
    let entry_view = |entry: &Value| {
        let application_id = entry["application_id"].as_str().filter(|id| validate::uuid(id))?;
        let context = context(application_id);
        if context.as_ref().is_some_and(|context| context["controller_id"].as_str() != Some(controller_id)) {
            return None;
        }
        let field = |key: &str| context.as_ref().map_or(Value::Null, |context| context[key].clone());
        Some(json!({
            "application_id": application_id, "root_name": dir_name(entry["root"].as_str().unwrap_or_default()),
            "started_at": entry["started_at"], "project_id": field("project_id"), "job_id": field("job_id"),
        }))
    };
    entries.iter().filter_map(entry_view).collect()
}

/// `result_recover` 的返回值：助手的报告，目录只给名称。
fn recover_view(report: &Value) -> Value {
    let mut view = report.clone();
    if let Some(fields) = view.as_object_mut() {
        fields.remove("backup"); // 助手状态目录里的绝对路径，页面不需要
        let root = fields.remove("root");
        fields.insert("root_name".into(), json!(dir_name(root.as_ref().and_then(Value::as_str).unwrap_or_default())));
    }
    view
}

/// 读出一个暂存文件并核对：边读边算 SHA-256，必须等于它的文件名（也就是要上传到的地址）。
/// 核对的与随后上传的是同一份内存里的字节。
// ponytail: 整个文件读进内存再上传，一次一个，上限是 controller 的单文件上限（缺省 64 MiB）；上限调大后改成流式请求体。
fn read_staged(path: &Path, sha256: &str, limit: Option<u64>) -> Result<Vec<u8>> {
    let mut file = std::fs::File::open(path).map_err(io)?;
    let (mut data, mut hasher, mut chunk) = (Vec::new(), Sha256::new(), vec![0u8; 256 * 1024]);
    loop {
        let read = file.read(&mut chunk).map_err(io)?;
        if read == 0 {
            break;
        }
        hasher.update(&chunk[..read]);
        data.extend_from_slice(&chunk[..read]);
        if limit.is_some_and(|max| data.len() as u64 > max) {
            return Err(invalid("文件超过这台 controller 的单文件上限"));
        }
    }
    if hex(&hasher.finalize()) != sha256 {
        return Err(err("blob_mismatch", "暂存内容的哈希与它的名称不符，没有上传"));
    }
    Ok(data)
}

fn read_snapshot(state: &Path, snapshot_id: &str) -> Result<Value> {
    let bytes = std::fs::read(state.join("snapshots").join(format!("{snapshot_id}.json")))
        .map_err(|_| err("no_snapshot", "没有这份本机快照，请重新扫描"))?;
    serde_json::from_slice(&bytes).map_err(|_| io("本机快照记录无效，请重新扫描"))
}

/// 应用开始前记下的来源：`{"project_id", "job_id", "controller_id", "connection_id"}`。
fn read_context(state: &Path, application_id: &str) -> Option<Value> {
    serde_json::from_slice(&std::fs::read(state.join("applies").join(application_id).join("context.json")).ok()?).ok()
}

/// 一份待向 controller 上报的应用结果：本机已有最终结果，controller 还没确认。不含凭据，只记连接的 ID。
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
struct PendingReport {
    application_id: String,
    project_id: String,
    controller_id: String,
    connection_id: String,
    request_id: String,
    status: String,
    detail: Value,
    created_at: String,
}

impl PendingReport {
    fn path(&self) -> String {
        format!("/projects/{}/applications/{}", self.project_id, self.application_id)
    }

    fn body(&self) -> Value {
        json!({ "request_id": self.request_id, "status": self.status, "detail": self.detail })
    }
}

fn report_path(state: &Path, application_id: &str) -> PathBuf {
    state.join("applies").join(application_id).join("report.json")
}

/// 本机所有待上报的结果。
fn pending_reports(state: &Path) -> Vec<PendingReport> {
    let Ok(entries) = std::fs::read_dir(state.join("applies")) else { return Vec::new() };
    let mut found: Vec<PendingReport> = entries
        .flatten()
        .filter_map(|entry| serde_json::from_slice::<PendingReport>(&std::fs::read(entry.path().join("report.json")).ok()?).ok())
        .filter(|report| validate::uuid(&report.application_id) && validate::uuid(&report.project_id))
        .collect();
    found.sort_by(|a, b| a.created_at.cmp(&b.created_at));
    found
}

/// 上报的应答 → controller 记下的状态。2xx 与明确的拒绝（例如记录已有最终结果）都结束这份回执；
/// 授权失败（401、403）与其他情况保留它，等授权恢复后对同一个 controller 补报，不改用别的身份。
fn settle_report(path: &Path, code: StatusCode, bytes: &[u8]) -> Option<String> {
    if code.is_success() {
        let _ = std::fs::remove_file(path);
        let record: Value = serde_json::from_slice(bytes).ok()?;
        return record["status"].as_str().map(str::to_owned);
    }
    if code.is_client_error() && !matches!(code, StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN) {
        let _ = std::fs::remove_file(path);
        return Some(format!("not_recorded:{}", code.as_u16()));
    }
    None
}

fn api_error(what: &str, status: StatusCode, body: &[u8]) -> BridgeError {
    let error = http_error(what, status, body);
    if status == StatusCode::NOT_FOUND {
        err("not_found", error.message)
    } else {
        error
    }
}

// ---------------------------------------------------------------- 一次操作

/// 运行一次 `project local <动作>`，进度经 `ac-project` 发给页面。停下的方式见 `Mode`；`cancel_file` 是助手的取消通道
/// （外壳创建这个文件，助手在下一个核对点看到它）。不用结束助手进程来实现正常的取消。
#[allow(clippy::too_many_arguments)]
async fn run_local(
    app: &AppHandle,
    helper: &Helper,
    cwd: &Path,
    args: Vec<OsString>,
    epoch: u64,
    op: &'static str,
    guard: Option<Arc<Op>>,
    mode: Mode,
    cancel_file: Option<PathBuf>,
) -> Result<Value> {
    let mut full: Vec<OsString> = ["project", "local"].map(Into::into).into();
    full.extend(args);
    if let Some(path) = &cancel_file {
        full.extend([os("--cancel-file"), os(path)]);
    }
    let command = helper.command(cwd, &full);
    let app = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let shell = app.state::<Shell>();
        let stop = || {
            let running = guard.as_ref()?;
            let cancelled = running.cancelled.load(Ordering::SeqCst);
            let stale = shell.lock().current(epoch).is_err();
            match stop_action(mode, cancelled, stale, cancel_file.is_some()) {
                Stop::Continue => None,
                Stop::Signal => {
                    // 助手自己停下：扫描与暂存在下一个文件处，写入在当前文件写完、记进日志之后。
                    if let Some(path) = cancel_file.as_ref().filter(|path| !path.exists()) {
                        let _ = std::fs::write(path, b"");
                    }
                    None
                }
                Stop::Kill => stop_reason(&shell, epoch, running), // 只读：没有东西写到一半，直接结束
            }
        };
        helper::project(command, &stop, &mut |value| {
            if let Some(payload) = progress(epoch, op, value) {
                if let Some(running) = &guard {
                    running.set_phase(&payload.phase);
                }
                emit(&app, payload);
            }
        })
    })
    .await
    .map_err(|_| err("helper_unavailable", "本机助手异常结束"))?
}

/// 一项进行中的项目操作：当前连接、独占位、助手与暂存目录。离开作用域时清理暂存目录并释放独占位。
struct Ctx<'a> {
    app: AppHandle,
    shell: &'a Shell,
    epoch: u64,
    active: Arc<Active>,
    op: &'static str,
    helper: Helper,
    /// 应用本机数据目录：助手的工作目录。
    data: PathBuf,
    /// 助手的状态目录 `<数据目录>/projects`：快照、应用的日志与备份。
    state: PathBuf,
    staging: Staging,
    running: Running<'a>,
}

/// 应用一个任务的已发布成果所需的东西；三份 JSON 已写进暂存目录。
struct Published {
    project_id: String,
    checkpoint_id: String,
    root: PathBuf,
    files: Vec<Value>,
    base: OsString,
    result: OsString,
    exclude: Option<OsString>,
}

impl Published {
    /// 助手 `plan` 与 `apply` 共用的参数。
    fn args(&self) -> Vec<OsString> {
        let mut args: Vec<OsString> =
            vec!["--root".into(), self.root.clone().into(), "--base".into(), self.base.clone(), "--result".into(), self.result.clone()];
        if let Some(exclude) = &self.exclude {
            args.extend([os("--exclude-file"), exclude.clone()]);
        }
        args
    }
}

impl<'a> Ctx<'a> {
    fn begin(app: &AppHandle, shell: &'a Shell, epoch: u64, op: &'static str) -> Result<Self> {
        let active = shell.lock().current(epoch)?;
        let helper = bundle(app).helper.ok_or_else(helper_unavailable)?;
        let running = shell.ops.begin(epoch, op)?;
        let data = data_dir(app)?;
        let state = data.join("projects");
        let staging = Staging::create(&state)?;
        Ok(Self { app: app.clone(), shell, epoch, active, op, helper, data, state, staging, running })
    }

    /// 核对点：操作被取消，或连接已经切换、关闭，就在这里停下。
    fn check(&self) -> Result<()> {
        stop_reason(self.shell, self.epoch, &self.running.op).map_or(Ok(()), Err)
    }

    /// 项目在这台机器上绑定的目录。
    fn root(&self, project_id: &str) -> Result<PathBuf> {
        let bindings = load_bindings(&self.app, &self.shell.lock())?;
        bindings.get(&self.active.controller_id, project_id).map(|binding| PathBuf::from(&binding.path)).ok_or_else(not_bound)
    }

    fn emit(&self, phase: &str, done: u64, total: u64, bytes: u64) {
        self.running.op.set_phase(phase);
        let payload = Progress { epoch: self.epoch, op: self.op, phase: phase.into(), done: Some(done), total: Some(total), files: None, bytes: Some(bytes) };
        emit(&self.app, payload);
    }

    /// 只有阶段、没有计数的进度（登记、上报）。
    fn stage(&self, phase: &str) {
        self.running.op.set_phase(phase);
        emit(&self.app, Progress { epoch: self.epoch, op: self.op, phase: phase.into(), done: None, total: None, files: None, bytes: None });
    }

    /// 把一份 JSON 写进暂存目录交给助手，返回它的路径。
    fn put_json(&self, name: &str, value: &Value) -> Result<OsString> {
        let path = self.staging.0.join(name);
        std::fs::write(&path, serde_json::to_vec(value).map_err(io)?).map_err(io)?;
        Ok(path.into())
    }

    /// 运行助手的一个动作，状态目录是 `state`；怎样停下见 `Mode`。
    async fn local(&self, action: &str, state: &Path, args: Vec<OsString>, mode: Mode) -> Result<Value> {
        let mut full: Vec<OsString> = vec![action.into(), "--state".into(), state.into()];
        full.extend(args);
        let cancel_file = matches!(mode, Mode::Cancellable | Mode::Writes).then(|| self.staging.0.join(format!("cancel-{}", new_id())));
        let outcome = run_local(&self.app, &self.helper, &self.data, full, self.epoch, self.op, Some(self.running.op.clone()), mode, cancel_file).await;
        match mode {
            // 助手因取消文件停下时报的是 cancelled；连接切换引起的停下按切换（stale_connection）如实返回。
            Mode::ReadOnly | Mode::Cancellable => {
                self.check()?;
                outcome
            }
            Mode::Writes | Mode::Finish => outcome,
        }
    }

    /// 以当前连接的凭据调用 controller；应答到达与读完时都核对代次。不看取消标志。
    async fn request(&self, method: Method, path: &str, body: Option<&Value>) -> Result<(StatusCode, Vec<u8>)> {
        let url = validate::api_url(&self.active.base, path).map_err(invalid)?;
        let mut request = self.active.http.api.request(method, url);
        if let Some(body) = body {
            request = request.header(CONTENT_TYPE, "application/json").body(body.to_string());
        }
        let response = self.shell.send(self.epoch, &self.active, request).await?;
        let status = response.status();
        let bytes = read_body(response).await;
        self.shell.lock().current(self.epoch)?; // 读响应体期间代次变了，同样丢弃
        Ok((status, bytes?))
    }

    async fn json(&self, method: Method, path: &str, body: Option<&Value>, what: &str) -> Result<Value> {
        self.check()?;
        let (status, bytes) = self.request(method, path, body).await?;
        if !status.is_success() {
            return Err(api_error(what, status, &bytes));
        }
        serde_json::from_slice(&bytes).map_err(|_| bad_response(what))
    }

    async fn get(&self, path: &str, what: &str) -> Result<Value> {
        self.json(Method::GET, path, None, what).await
    }

    async fn post(&self, path: &str, body: &Value, what: &str) -> Result<Value> {
        self.json(Method::POST, path, Some(body), what).await
    }

    /// `GET /client` 给出的上限：`(max_blob_bytes, max_json_bytes)`。
    async fn limits(&self) -> Result<(Option<u64>, Option<u64>)> {
        let info = self.get("/client", "读取 controller 的上限").await?;
        let limits = &info["controller"]["limits"];
        Ok((limits["max_blob_bytes"].as_u64(), limits["max_json_bytes"].as_u64()))
    }

    /// 把一份内容下载到暂存目录的 `blobs/<sha256>`：写临时文件、核对哈希后改名。
    /// `size` 是清单里记的大小；收到的字节超过它时哈希必然不符，不必收完。
    async fn fetch(&self, sha256: &str, size: u64) -> Result<()> {
        let dir = self.staging.blobs();
        let dest = dir.join(sha256);
        if dest.is_file() {
            return Ok(());
        }
        self.check()?;
        let url = validate::api_url(&self.active.base, &format!("/blobs/{sha256}")).map_err(invalid)?;
        let mut response = self.shell.send(self.epoch, &self.active, self.active.http.api.get(url)).await?;
        match response.status() {
            StatusCode::OK => {}
            StatusCode::NOT_FOUND => return Err(err("not_found", "controller 上没有清单里的这份内容")),
            status => return Err(http_error("下载失败", status, &[])),
        }
        let mut sink = BlobSink::create(&dir)?;
        loop {
            let chunk = response.chunk().await;
            self.check()?;
            match chunk.map_err(net)? {
                Some(chunk) => sink.write(&chunk)?,
                None => break,
            }
            if sink.bytes > size {
                return Err(err("blob_mismatch", "下载的内容比清单里记的大，已中止"));
            }
        }
        if !sink.matches(sha256) {
            // 与助手核对同一件事时用同一个错误码；临时文件随 sink 删除。
            return Err(err("blob_mismatch", "下载内容的 SHA-256 与清单不符，没有使用"));
        }
        sink.finish(sha256, &dest).map(|_| ())
    }

    /// 下载一组内容并逐个发进度。
    async fn fetch_all(&self, wanted: &BTreeMap<String, u64>) -> Result<()> {
        let (total, mut bytes) = (wanted.len() as u64, 0);
        for (index, (sha256, size)) in wanted.iter().enumerate() {
            self.fetch(sha256, *size).await?;
            bytes += size;
            self.emit("download", index as u64 + 1, total, bytes);
        }
        Ok(())
    }

    /// `PUT /blobs/<sha256>`：请求体是原始字节，200（已有）或 201（新建）算成功。
    async fn put_blob(&self, sha256: &str, data: Vec<u8>) -> Result<()> {
        self.check()?;
        let url = validate::api_url(&self.active.base, &format!("/blobs/{sha256}")).map_err(invalid)?;
        // 这个客户端没有读超时，总时限随内容大小放宽：按不低于 16 KiB/s 估算，另加两分钟。
        let limit = Duration::from_secs(120 + data.len() as u64 / 16384);
        // 显式带 Content-Length：内容为空（空文件）时 HTTP 客户端不会自己加，controller 拒绝没有声明长度的 PUT。
        let request = self.active.http.bulk.put(url).header(CONTENT_TYPE, "application/octet-stream").header(CONTENT_LENGTH, data.len()).timeout(limit).body(data);
        let response = self.shell.send(self.epoch, &self.active, request).await?;
        let status = response.status();
        let body = read_body(response).await;
        self.shell.lock().current(self.epoch)?;
        if !matches!(status, StatusCode::OK | StatusCode::CREATED) {
            return Err(api_error("上传文件内容失败", status, &body.unwrap_or_default()));
        }
        Ok(())
    }

    /// 助手内置的缺省排除规则（版本库元数据、依赖与构建缓存、常见凭据文件）。规则只有 `agent_collab/project.py`
    /// 里那一份：让助手扫描一个空目录，从结果里读回它实际使用的规则，不在外壳里另抄一份。
    async fn default_excludes(&self) -> Result<Vec<String>> {
        let (empty, state) = (self.staging.0.join("empty"), self.staging.0.join("probe"));
        std::fs::create_dir_all(&empty).map_err(io)?;
        let result = self.local("scan", &state, vec!["--root".into(), empty.into()], Mode::Cancellable).await?;
        let defaults = strings(&result["exclude"]);
        if defaults.is_empty() {
            // 没有缺省规则就会把 .git、.env 这类内容当作输入，宁可失败。
            return Err(io("读不到本机助手的缺省排除规则"));
        }
        Ok(defaults)
    }

    async fn checkpoint_files(&self, checkpoint_id: &str) -> Result<Vec<Value>> {
        let mut listing = self.get(&format!("/checkpoints/{checkpoint_id}/files"), "读取成果的文件清单").await?;
        match listing["files"].take() {
            Value::Array(files) => Ok(files),
            _ => Err(bad_response("读取成果的文件清单")),
        }
    }

    /// 任务的已发布成果、输入基线与项目绑定的目录（与 CLI 的 `job_result` 相同）。
    /// `project_id` 取自 controller 上的任务记录，不取页面参数。
    async fn published(&self, job_id: &str) -> Result<Published> {
        let job = self.get(&format!("/jobs/{job_id}"), "读取任务").await?;
        let id = |key: &str| job[key].as_str().filter(|id| validate::uuid(id)).map(str::to_owned);
        let project_id = id("project_id").ok_or_else(|| invalid("这个任务不属于任何项目"))?;
        let checkpoint_id = id("delivery_checkpoint_id").ok_or_else(|| invalid("这个任务还没有已发布的成果；只有已发布的成果可以应用到项目"))?;
        let root = self.root(&project_id)?;
        let files = self.checkpoint_files(&checkpoint_id).await?;
        let (base, exclude) = base_and_exclude(&job);
        Ok(Published {
            base: self.put_json("base.json", &base)?,
            result: self.put_json("result.json", &manifest_of(&files))?,
            exclude: exclude.map(|rules| self.put_json("exclude.json", &rules)).transpose()?,
            project_id,
            checkpoint_id,
            root,
            files,
        })
    }

    /// 把应用的最终结果报给 controller，返回它记下的状态。结果连同固定的 request_id 先存进本机回执
    /// `applies/<id>/report.json`，再用发起这次应用的那条连接（凭据、CA、地址都是操作开始时的）发送；送不到时回执保留，
    /// 之后这条连接再次打开时按同一个 request_id 补报（`result_unfinished`），controller 只记一次，本机文件不重写。
    /// 不看取消标志；连接在写入期间切换或关闭时，请求仍发给原 controller，应答不再交给页面。
    async fn report(&self, project_id: &str, application_id: &str, status: &str, detail: Value) -> Option<String> {
        self.stage("report");
        let receipt = PendingReport {
            application_id: application_id.into(),
            project_id: project_id.into(),
            controller_id: self.active.controller_id.clone(),
            connection_id: self.active.id.clone(),
            request_id: new_id(),
            status: status.into(),
            detail,
            created_at: now_iso(),
        };
        let path = report_path(&self.state, application_id);
        if write_json(&path, &receipt).is_err() {
            return None;
        }
        let (code, bytes) = self.request(Method::POST, &receipt.path(), Some(&receipt.body())).await.ok()?;
        settle_report(&path, code, &bytes)
    }
}

// ---------------------------------------------------------------- 命令：绑定

#[tauri::command(rename_all = "snake_case")]
pub async fn project_bind(window: WebviewWindow, app: AppHandle, shell: State<'_, Shell>, epoch: u64, project_id: String) -> Result<Value> {
    let active = shell.lock().current(epoch)?;
    require_uuid(&project_id, "project_id")?;
    // 目录只能来自这个对话框；命令没有任何路径参数。
    let Some(dir) = dialog::pick_folder(&window, "选择项目文件夹").await? else {
        return Ok(json!({ "cancelled": true }));
    };
    if !dir.is_dir() {
        return Err(err("not_a_directory", "所选位置不是目录"));
    }
    let path = dir.to_str().ok_or_else(|| invalid("所选目录的路径含有无法保存的字符"))?.to_string();
    let binding = Binding { path, bound_at: now_iso() };
    let inner = shell.lock();
    inner.current(epoch)?; // 对话框开着的时候连接可能已经切换
    let mut bindings = load_bindings(&app, &inner)?;
    bindings.set(&active.controller_id, &project_id, binding.clone());
    save_bindings(&app, &inner, &bindings)?;
    Ok(json!({ "binding": binding.view(&project_id) }))
}

#[tauri::command]
pub fn project_bindings(app: AppHandle, shell: State<'_, Shell>, epoch: u64) -> Result<Value> {
    let inner = shell.lock();
    let active = inner.current(epoch)?;
    Ok(json!({ "bindings": load_bindings(&app, &inner)?.views(&active.controller_id) }))
}

#[tauri::command(rename_all = "snake_case")]
pub fn project_unbind(app: AppHandle, shell: State<'_, Shell>, epoch: u64, project_id: String) -> Result<Value> {
    require_uuid(&project_id, "project_id")?;
    let inner = shell.lock();
    let active = inner.current(epoch)?;
    let mut bindings = load_bindings(&app, &inner)?;
    bindings.remove(&active.controller_id, &project_id); // 只删除绑定，不动目录
    save_bindings(&app, &inner, &bindings)?;
    Ok(json!({}))
}

#[tauri::command]
pub fn project_cancel(app: AppHandle, shell: State<'_, Shell>, epoch: u64) -> Result<Value> {
    shell.lock().current(epoch)?;
    let Some((op, phase)) = shell.ops.cancel(epoch) else {
        return Ok(json!({ "requested": false }));
    };
    // 取消是请求，不是已停止：写入阶段要把当前文件写完；页面在操作的命令返回之后才显示“已停止”。
    let stops = if phase == "write" { "after_current_file" } else { "next_step" };
    emit(&app, Progress { epoch, op, phase: "cancel_requested".into(), done: None, total: None, files: None, bytes: None });
    Ok(json!({ "requested": true, "op": op, "phase": phase, "stops": stops }))
}

// ---------------------------------------------------------------- 命令：扫描与上传

#[tauri::command(rename_all = "snake_case")]
pub async fn project_scan(app: AppHandle, shell: State<'_, Shell>, epoch: u64, project_id: String, exclude: Option<Vec<String>>) -> Result<Value> {
    require_uuid(&project_id, "project_id")?;
    if exclude.as_ref().is_some_and(|rules| !validate::excludes(rules)) {
        return Err(invalid("exclude 必须是不超过 2000 条、每条不超过 1024 字节的规则列表"));
    }
    let ctx = Ctx::begin(&app, &shell, epoch, "scan")?;
    let root = ctx.root(&project_id)?;
    prune_snapshots(&ctx.state);
    let (max_blob_bytes, _) = ctx.limits().await?;
    let record = ctx.get(&format!("/projects/{project_id}"), "读取项目").await?;
    let mut args: Vec<OsString> = vec!["--root".into(), root.into(), "--project".into(), project_id.into()];
    if let Some(max) = max_blob_bytes {
        args.extend([os("--max-file-bytes"), os(max.to_string())]);
    }
    // 页面给出规则时替换整个列表；否则是项目的排除规则加缺省规则。两者都没有时不传，由助手用它的缺省规则。
    let rules = match exclude {
        Some(rules) => Some(rules),
        None => match strings(&record["defaults"]["exclude"]) {
            own if own.is_empty() => None,
            own => Some(merge_excludes(own, ctx.default_excludes().await?)),
        },
    };
    if let Some(rules) = rules {
        args.extend([os("--exclude-file"), ctx.put_json("exclude.json", &json!(rules))?]);
    }
    if cfg!(windows) {
        // Windows 看不到可执行位：沿用上一份输入里同一路径的标记，否则每次从 Windows 上传都会把它们清掉。
        if let Some(sha256) = record["baseline"]["manifest_sha256"].as_str().filter(|sha| validate::sha256_hex(sha)) {
            ctx.fetch(sha256, MAX_BODY as u64).await?;
            args.extend([os("--previous"), os(ctx.staging.blobs().join(sha256))]);
        }
    }
    let result = ctx.local("scan", &ctx.state, args, Mode::Cancellable).await?;
    Ok(scan_view(&result))
}

#[tauri::command(rename_all = "snake_case")]
pub async fn project_upload(app: AppHandle, shell: State<'_, Shell>, epoch: u64, project_id: String, snapshot_id: String) -> Result<Value> {
    require_uuid(&project_id, "project_id")?;
    require_uuid(&snapshot_id, "snapshot_id")?;
    let ctx = Ctx::begin(&app, &shell, epoch, "upload")?;
    ctx.root(&project_id)?; // 项目必须仍然绑定着目录
    let snapshot = read_snapshot(&ctx.state, &snapshot_id)?;
    if snapshot["project_id"].as_str() != Some(project_id.as_str()) {
        return Err(invalid("这份快照不是这个项目的扫描结果"));
    }
    let manifest_sha256 = snapshot["manifest_sha256"].as_str().filter(|sha| validate::sha256_hex(sha)).ok_or_else(|| io("本机快照记录无效，请重新扫描"))?;
    let (max_blob_bytes, max_json_bytes) = ctx.limits().await?;
    // 文件清单随 POST /baselines 内联提交，受单次请求上限限制；给请求里的其他字段留 64 KiB。
    if let (Some(max), Some(size)) = (max_json_bytes, snapshot["manifest_bytes"].as_u64()) {
        if size > max.saturating_sub(65536) {
            let files = snapshot["files"].as_u64().unwrap_or(0);
            return Err(invalid(format!("文件数太多：文件清单有 {files} 个文件（{size} 字节），超过这台 controller 单次请求的上限；增加排除规则后重新扫描")));
        }
    }
    let digests: Vec<String> = blobs_of(items(&snapshot["manifest"]["files"]))?.into_keys().collect();
    let mut missing = BTreeSet::new();
    for batch in digests.chunks(10000) {
        let reply = ctx.post("/blobs/missing", &json!({ "sha256": batch }), "查询缺少的内容").await?;
        // 只认清单里的哈希：应答里别的东西不会变成暂存文件名或上传地址。
        missing.extend(strings(&reply["missing"]).into_iter().filter(|sha| digests.binary_search(sha).is_ok()));
    }
    let (mut uploaded, mut uploaded_bytes, mut changed) = (0u64, 0u64, Vec::new());
    if !missing.is_empty() {
        let (need, out) = (ctx.put_json("need.json", &json!(missing))?, ctx.staging.blobs());
        let args: Vec<OsString> = vec!["--snapshot".into(), snapshot_id.clone().into(), "--need".into(), need, "--out".into(), out.clone().into()];
        let staged = ctx.local("stage", &ctx.state, args, Mode::Cancellable).await?;
        changed = strings(&staged["changed"]);
        for sha256 in strings(&staged["staged"]) {
            if !validate::sha256_hex(&sha256) || !missing.contains(&sha256) {
                continue;
            }
            let path = out.join(&sha256);
            let data = read_staged(&path, &sha256, max_blob_bytes)?;
            uploaded_bytes += data.len() as u64;
            ctx.put_blob(&sha256, data).await?;
            let _ = std::fs::remove_file(&path);
            uploaded += 1;
            ctx.emit("upload", uploaded, missing.len() as u64, uploaded_bytes);
        }
    }
    if !changed.is_empty() {
        // 扫描之后源文件变了：这份快照已不能代表目录，不登记为输入。已上传的内容留着，重新扫描后只传缺的。
        let listed = changed.iter().take(20).map(String::as_str).collect::<Vec<_>>().join("、");
        return Err(err("source_changed", format!("这些文件在扫描后被修改，没有登记输入：{listed}；重新扫描后再上传")));
    }
    // 这份快照已经发出过登记（应答丢失、进程中断、取消或切换连接）：原样重放存下的请求体，得到同一份基线。
    let body = match snapshot.get("registration").filter(|body| body.is_object()) {
        Some(saved) => saved.clone(),
        None => {
            let record = ctx.get(&format!("/projects/{project_id}"), "读取项目").await?;
            if record["baseline"]["manifest_sha256"].as_str() == Some(manifest_sha256) {
                // 内容与项目最近一份基线相同：不新建。
                return Ok(json!({
                    "baseline_id": record["baseline"]["baseline_id"], "uploaded_blobs": uploaded, "uploaded_bytes": uploaded_bytes,
                    "reused": true, "missing_blobs": [],
                }));
            }
            let body = baseline_request(&project_id, record["name"].as_str().unwrap_or_default(), &snapshot, &device_name());
            // 第一次发出之前存进快照：之后的重试不重新取时间、不重建名称、不换 request_id。
            let mut saved = snapshot.clone();
            saved["registration"] = body.clone();
            write_json(&ctx.state.join("snapshots").join(format!("{snapshot_id}.json")), &saved)?;
            body
        }
    };
    ctx.stage("register");
    let baseline = ctx.post("/baselines", &body, "登记输入").await?;
    Ok(json!({
        "baseline_id": baseline["baseline_id"], "uploaded_blobs": uploaded, "uploaded_bytes": uploaded_bytes,
        "reused": false, "missing_blobs": baseline["missing_blobs"],
    }))
}

// ---------------------------------------------------------------- 命令：成果下载

#[tauri::command(rename_all = "snake_case")]
pub async fn result_download(
    window: WebviewWindow,
    app: AppHandle,
    shell: State<'_, Shell>,
    epoch: u64,
    job_id: String,
    checkpoint_id: String,
) -> Result<Value> {
    require_uuid(&job_id, "job_id")?;
    require_uuid(&checkpoint_id, "checkpoint_id")?;
    let ctx = Ctx::begin(&app, &shell, epoch, "download")?;
    let checkpoint = ctx.get(&format!("/checkpoints/{checkpoint_id}"), "读取检查点").await?;
    if checkpoint["job_id"].as_str() != Some(job_id.as_str()) {
        return Err(invalid("这个检查点不属于该任务"));
    }
    let Some(target) = dialog::pick_folder(&window, "选择保存成果的空文件夹").await? else {
        return Ok(json!({ "cancelled": true }));
    };
    ctx.check()?; // 对话框开着的时候连接可能已经切换
    if std::fs::read_dir(&target).is_ok_and(|mut entries| entries.next().is_some()) {
        return Err(err("target_not_empty", "下载目录必须为空或不存在")); // 助手落盘前还会再查一次
    }
    let files = ctx.checkpoint_files(&checkpoint_id).await?;
    ctx.fetch_all(&blobs_of(&files)?).await?;
    // 落盘由助手完成：每个文件再核对一次哈希，路径逐级拒绝链接与越界。
    // ponytail: 内容先完整下载到暂存目录再落盘，过程中占用两份磁盘空间；嫌多就改成边下载边落盘。
    let args: Vec<OsString> = vec![
        "--out".into(),
        target.clone().into(),
        "--result".into(),
        ctx.put_json("result.json", &manifest_of(&files))?,
        "--blobs".into(),
        ctx.staging.blobs().into(),
    ];
    let done = ctx.local("materialize", &ctx.state, args, Mode::Finish).await?;
    let (count, bytes) = (done["files"].as_u64().unwrap_or(0), done["bytes"].as_u64().unwrap_or(0));
    // 文件已经完整落盘并核对过；登记不上不改变这一事实，如实返回 registered: false。
    let body = json!({ "request_id": new_id(), "checkpoint_id": checkpoint_id, "files": count, "bytes": bytes });
    let registered = match ctx.post(&format!("/jobs/{job_id}/downloads"), &body, "登记下载").await {
        Ok(_) => true,
        Err(error) if error.code == "stale_connection" => return Err(error),
        Err(_) => false,
    };
    Ok(json!({ "path": target.to_string_lossy(), "files": count, "bytes": bytes, "registered": registered }))
}

// ---------------------------------------------------------------- 命令：计划、应用与恢复

#[tauri::command(rename_all = "snake_case")]
pub async fn result_plan(app: AppHandle, shell: State<'_, Shell>, epoch: u64, job_id: String) -> Result<Value> {
    require_uuid(&job_id, "job_id")?;
    let ctx = Ctx::begin(&app, &shell, epoch, "apply")?;
    let published = ctx.published(&job_id).await?;
    let plan = ctx.local("plan", &ctx.state, published.args(), Mode::ReadOnly).await?;
    Ok(json!({
        "project_id": published.project_id,
        "checkpoint_id": published.checkpoint_id,
        "root_name": dir_name(&published.root.to_string_lossy()),
        "changes": items(&plan["changes"]).iter().map(change_view).collect::<Vec<_>>(),
        "summary": plan["summary"],
    }))
}

#[tauri::command(rename_all = "snake_case")]
pub async fn result_apply(
    app: AppHandle,
    shell: State<'_, Shell>,
    epoch: u64,
    job_id: String,
    checkpoint_id: String,
    skip_conflicts: bool,
) -> Result<Value> {
    require_uuid(&job_id, "job_id")?;
    require_uuid(&checkpoint_id, "checkpoint_id")?;
    let ctx = Ctx::begin(&app, &shell, epoch, "apply")?;
    let published = ctx.published(&job_id).await?;
    if published.checkpoint_id != checkpoint_id {
        return Err(invalid("这个检查点已不是该任务已发布的成果，请重新查看变更"));
    }
    // 同一个目录上有一次没有完成的应用时不开始新的。助手写入前也会查；提前查是为了不先登记一条注定不写的记录。
    let pending = ctx.local("unfinished", &ctx.state, vec!["--root".into(), published.root.clone().into()], Mode::ReadOnly).await?;
    if !items(&pending["unfinished"]).is_empty() {
        return Err(err("unfinished_application", "这个目录上有一次没有完成的应用操作，先查看并处理它"));
    }
    let plan = ctx.local("plan", &ctx.state, published.args(), Mode::ReadOnly).await?;
    let changes = items(&plan["changes"]);
    if let Some(settled) = settled_without_writing(changes, skip_conflicts) {
        return Ok(settled);
    }
    // 写入之前把可以应用的改动所需的内容全部取到暂存目录并核对。
    let after: HashMap<&str, &Value> = published.files.iter().filter_map(|file| Some((file["path"].as_str()?, file))).collect();
    let writes = changes.iter().filter(|change| change["state"] == "clean" && change["action"] != "delete");
    ctx.fetch_all(&blobs_of(writes.filter_map(|change| after.get(change["path"].as_str()?).copied()))?).await?;

    // 第一次写入之前登记意图；登记不上就不写。之后即使本进程被打断，controller 上的记录也停在 started。
    // 取消只到助手开始写入之前为止：写入阶段不中途停下，结果按实际情况返回。
    let application_id = new_id();
    let registration = json!({
        "request_id": new_id(), "application_id": application_id, "job_id": job_id, "checkpoint_id": checkpoint_id,
        "device": device_name(), "plan": plan["summary"],
    });
    ctx.stage("register");
    let failed = |error: &BridgeError| json!({ "errors": [{ "reason": error.code, "error": error.message }] });
    if let Err(error) = ctx.post(&format!("/projects/{}/applications", published.project_id), &registration, "登记应用").await {
        // 应答可能丢了而 controller 已经登记：按同一个 application_id 补报“失败、一个文件都没写”（持久回执，送不到下次再报；
        // controller 上没有这条记录时被拒绝，回执随之结束），不让它永远停在 started。
        let journal = ctx.state.join("applies").join(&application_id);
        ctx.report(&published.project_id, &application_id, "failed", failed(&error)).await;
        clear_if_settled(&journal);
        return Err(error);
    }
    if let Err(stopped) = ctx.check() {
        // 登记期间被取消或连接切换：一个文件都还没写，把刚登记的记录结束为 failed。
        let journal = ctx.state.join("applies").join(&application_id);
        ctx.report(&published.project_id, &application_id, "failed", failed(&stopped)).await;
        clear_if_settled(&journal);
        return Err(stopped);
    }
    let journal = ctx.state.join("applies").join(&application_id);
    let context = json!({
        "project_id": published.project_id, "job_id": job_id, "controller_id": ctx.active.controller_id, "connection_id": ctx.active.id,
    });
    let mut args = published.args();
    args.extend([os("--application"), os(&application_id), os("--blobs"), os(ctx.staging.blobs())]);
    if skip_conflicts {
        args.push("--skip-conflicts".into());
    }
    let outcome = match write_json(&journal.join("context.json"), &context) {
        Ok(()) => ctx.local("apply", &ctx.state, args, Mode::Writes).await,
        Err(error) => Err(error),
    };
    let written = journal.join("journal.json").exists();
    let result = match outcome {
        Ok(result) => result,
        Err(error) => {
            // 助手没有给出结果。操作日志不存在说明一个文件都没写，把登记的记录结束为 failed；
            // 日志已经存在则保持 started，由 result_unfinished 与 result_recover 核对现状后处理。
            if !written {
                ctx.report(&published.project_id, &application_id, "failed", failed(&error)).await;
                clear_if_settled(&journal);
            }
            return Err(error);
        }
    };
    let (status, detail) = application_report(&result, APPLY_DETAIL);
    let reported = ctx.report(&published.project_id, &application_id, &status, detail).await;
    // 写入期间连接切换了：文件已按日志里的结果写完、结果已报给（或待补报给）原 controller；这次调用的应答不再交给页面。
    ctx.shell.lock().current(ctx.epoch)?;
    Ok(apply_view(&result, &application_id, reported))
}

#[tauri::command]
pub async fn result_unfinished(app: AppHandle, shell: State<'_, Shell>, epoch: u64) -> Result<Value> {
    let active = shell.lock().current(epoch)?;
    let data = data_dir(&app)?;
    let state = data.join("projects");
    // 本机已有结果、还没得到 controller 确认的应用：属于当前这条连接的当场按原 request_id 补报（controller 只记一次，
    // 文件不重写）；属于别的连接的只列出来，不用当前连接的凭据去报。
    let mut unreported = Vec::new();
    for receipt in pending_reports(&state) {
        let mine = receipt.connection_id == active.id && receipt.controller_id == active.controller_id;
        let mut reported = None;
        if mine {
            let url = validate::api_url(&active.base, &receipt.path()).map_err(invalid)?;
            let request = active.http.api.post(url).header(CONTENT_TYPE, "application/json").body(receipt.body().to_string());
            if let Ok(response) = shell.send(epoch, &active, request).await {
                let code = response.status();
                let bytes = read_body(response).await.unwrap_or_default();
                reported = settle_report(&report_path(&state, &receipt.application_id), code, &bytes);
            }
        }
        unreported.push(unreported_view(&receipt, mine, reported));
    }
    // 页面每次连接后都会查询：没有任何未完成标记时不必启动助手。
    let marked = std::fs::read_dir(state.join("applies").join("roots")).is_ok_and(|mut entries| entries.next().is_some());
    if !marked {
        shell.lock().current(epoch)?;
        return Ok(json!({ "unfinished": [], "unreported": unreported }));
    }
    let helper = bundle(&app).helper.ok_or_else(helper_unavailable)?;
    let args: Vec<OsString> = vec!["unfinished".into(), "--state".into(), state.clone().into()];
    let listed = run_local(&app, &helper, &data, args, epoch, "recover", None, Mode::ReadOnly, None).await?;
    shell.lock().current(epoch)?;
    Ok(json!({
        "unfinished": unfinished_view(items(&listed["unfinished"]), &active.controller_id, |id| read_context(&state, id)),
        "unreported": unreported,
    }))
}

/// 待补报结果的页面视图。`mine` 为 false 表示它属于另一条连接：切换到那条连接后才会补报。
/// `reported` 是这次补报后 controller 记下的状态；仍为 null 时回执保留（不可达、授权被拒）。
fn unreported_view(receipt: &PendingReport, mine: bool, reported: Option<String>) -> Value {
    json!({
        "application_id": receipt.application_id, "project_id": receipt.project_id, "status": receipt.status,
        "created_at": receipt.created_at, "this_connection": mine, "connection_id": receipt.connection_id, "reported": reported,
    })
}

#[tauri::command(rename_all = "snake_case")]
pub async fn result_recover(app: AppHandle, shell: State<'_, Shell>, epoch: u64, application_id: String, action: String) -> Result<Value> {
    require_uuid(&application_id, "application_id")?;
    if !matches!(action.as_str(), "status" | "resume" | "rollback" | "abandon") {
        return Err(invalid("action 只能是 status、resume、rollback 或 abandon"));
    }
    let ctx = Ctx::begin(&app, &shell, epoch, "recover")?;
    let context = read_context(&ctx.state, &application_id);
    let other = |key: &str, current: &str| context.as_ref().is_some_and(|context| context[key].as_str().is_some_and(|id| id != current));
    if other("controller_id", &ctx.active.controller_id) || other("connection_id", &ctx.active.id) {
        // 处理结果要用登记这次应用的那条连接报给那个 controller，不能报给当前这个，也不改用别的身份。
        return Err(err("not_found", "这次应用是在另一条连接下发起的，请打开那条连接后处理"));
    }
    let args = |recover: &str| -> Vec<OsString> { vec!["--application".into(), application_id.clone().into(), "--recover-action".into(), recover.into()] };
    let mut run = args(&action);
    if action == "resume" {
        // 先核对现状找出还没写的文件，把它们需要的内容重新下载到暂存目录，再续写。
        let status = ctx.local("recover", &ctx.state, args("status"), Mode::ReadOnly).await?;
        let journal = std::fs::read(ctx.state.join("applies").join(&application_id).join("journal.json")).map_err(io)?;
        let journal: Value = serde_json::from_slice(&journal).map_err(|_| io("本机的应用日志无效"))?;
        ctx.fetch_all(&remaining_blobs(&journal, &status)?).await?;
        ctx.check()?; // 取消到这里为止
        run.extend([os("--blobs"), os(ctx.staging.blobs())]);
    }
    // 只核对现状的 status 可以随时结束；其余三种会改动目录或日志：取消请求让续写在当前文件写完后停下，切换连接不打断。
    let report = ctx.local("recover", &ctx.state, run, if action == "status" { Mode::ReadOnly } else { Mode::Writes }).await?;
    let mut view = recover_view(&report);
    if action != "status" {
        if let Some(project_id) = context.as_ref().and_then(|context| context["project_id"].as_str()).filter(|id| validate::uuid(id)) {
            let (status, detail) = application_report(&report, RECOVER_DETAIL);
            view["reported"] = json!(ctx.report(project_id, &application_id, &status, detail).await);
        }
    }
    ctx.shell.lock().current(ctx.epoch)?;
    Ok(view)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("agent-collab-desktop-projects-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn code<T>(result: Result<T>) -> String {
        result.err().map_or("ok".into(), |e| e.code.into_owned())
    }

    const PROJECT: &str = "11111111-2222-4333-8444-555555555555";
    const OTHER: &str = "99999999-2222-4333-8444-555555555555";
    const HELLO_SHA256: &str = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824";

    fn binding(path: &str) -> Binding {
        Binding { path: path.into(), bound_at: "2026-11-12T00:00:00.000Z".into() }
    }

    #[test]
    fn bindings_are_kept_per_controller_and_project() {
        let mut bindings = Bindings::default();
        assert!(bindings.get("ctl-a", PROJECT).is_none());
        bindings.set("ctl-a", PROJECT, binding("/work/one"));
        bindings.set("ctl-a", OTHER, binding("/work/two"));
        bindings.set("ctl-b", PROJECT, binding("/work/three"));
        assert_eq!(bindings.get("ctl-a", PROJECT).unwrap().path, "/work/one");
        assert_eq!(bindings.get("ctl-b", PROJECT).unwrap().path, "/work/three");
        assert!(bindings.get("ctl-b", OTHER).is_none());
        assert!(bindings.get("ctl-c", PROJECT).is_none());

        // 重新绑定替换原来的目录。
        bindings.set("ctl-a", PROJECT, binding("/work/moved"));
        assert_eq!(bindings.get("ctl-a", PROJECT).unwrap().path, "/work/moved");
        assert_eq!(bindings.views("ctl-a").len(), 2);
        assert!(bindings.views("ctl-c").is_empty());

        bindings.remove("ctl-a", PROJECT);
        bindings.remove("ctl-a", PROJECT); // 再删一次没有影响
        bindings.remove("ctl-c", PROJECT);
        assert!(bindings.get("ctl-a", PROJECT).is_none());
        assert_eq!(bindings.views("ctl-a").len(), 1);
        // 一个 controller 的最后一项删掉后，它在表里不留空壳。
        bindings.remove("ctl-b", PROJECT);
        assert_eq!(serde_json::to_value(&bindings).unwrap(), json!({ "ctl-a": { OTHER: { "path": "/work/two", "bound_at": "2026-11-12T00:00:00.000Z" } } }));
    }

    #[test]
    fn bindings_file_round_trips_and_views_report_existence() {
        let stored = json!({ "ctl-a": { PROJECT: { "path": "/work/one", "bound_at": "2026-11-12T00:00:00.000Z" } } });
        let bindings: Bindings = serde_json::from_value(stored.clone()).unwrap();
        assert_eq!(serde_json::to_value(&bindings).unwrap(), stored);
        assert_eq!(serde_json::from_str::<Bindings>("{}").unwrap(), Bindings::default());

        let dir = scratch("binding");
        let here = binding(dir.to_str().unwrap()).view(PROJECT);
        assert_eq!(here["project_id"], PROJECT);
        assert_eq!(here["name"], json!(dir.file_name().unwrap().to_str().unwrap()));
        assert_eq!(here["exists"], true);
        assert_eq!(here["bound_at"], "2026-11-12T00:00:00.000Z");
        std::fs::remove_dir_all(&dir).unwrap();
        assert_eq!(binding(dir.to_str().unwrap()).view(PROJECT)["exists"], false);
    }

    #[test]
    fn only_one_project_operation_runs_at_a_time() {
        let ops = Ops::default();
        let first = ops.begin(7, "scan").unwrap();
        assert_eq!(code(ops.begin(7, "scan")), "busy");
        assert_eq!(code(ops.begin(8, "scan")), "busy");

        // 取消只作用于同一代连接上的操作。
        ops.cancel(6);
        assert!(!first.op.cancelled.load(Ordering::SeqCst));
        ops.cancel(7);
        assert!(first.op.cancelled.load(Ordering::SeqCst));

        drop(first); // 操作结束（成功、失败或取消）即释放
        let second = ops.begin(8, "scan").unwrap();
        assert!(!second.op.cancelled.load(Ordering::SeqCst));
        drop(second);
        ops.cancel(8); // 没有操作在进行时取消没有影响
        assert!(ops.begin(9, "scan").is_ok());
    }

    #[test]
    fn cancelling_signals_the_helper_and_only_read_only_work_is_ever_killed() {
        // 写入：取消请求经取消文件通知（写完当前文件再停）；切换连接不打断。从不结束助手。
        assert_eq!(stop_action(Mode::Writes, true, false, true), Stop::Signal);
        assert_eq!(stop_action(Mode::Writes, false, true, true), Stop::Continue);
        assert_eq!(stop_action(Mode::Writes, true, true, false), Stop::Continue);
        // 扫描与暂存：取消与切换都经取消文件通知。
        assert_eq!(stop_action(Mode::Cancellable, true, false, true), Stop::Signal);
        assert_eq!(stop_action(Mode::Cancellable, false, true, true), Stop::Signal);
        // 只读：可以直接结束；整包下载的落盘不打断。
        assert_eq!(stop_action(Mode::ReadOnly, true, false, false), Stop::Kill);
        assert_eq!(stop_action(Mode::ReadOnly, false, true, false), Stop::Kill);
        assert_eq!(stop_action(Mode::Finish, true, true, false), Stop::Continue);
        assert_eq!(stop_action(Mode::ReadOnly, false, false, false), Stop::Continue);
    }

    #[test]
    fn pending_reports_are_kept_until_the_original_controller_answers() {
        let dir = scratch("reports");
        let receipt = |id: &str, connection: &str, at: &str| PendingReport {
            application_id: id.into(), project_id: PROJECT.into(), controller_id: "ctl-a".into(), connection_id: connection.into(),
            request_id: "aaaaaaaa-1111-4111-8111-111111111111".into(), status: "applied".into(), detail: json!({ "changed": ["a"] }),
            created_at: at.into(),
        };
        let (first, second) = ("bbbbbbbb-2222-4222-8222-222222222222", "cccccccc-3333-4333-8333-333333333333");
        write_json(&report_path(&dir, first), &receipt(first, "conn-a", "2026-11-12T01:00:00.000Z")).unwrap();
        write_json(&report_path(&dir, second), &receipt(second, "conn-b", "2026-11-12T02:00:00.000Z")).unwrap();
        let found = pending_reports(&dir);
        assert_eq!(found.iter().map(|r| r.application_id.as_str()).collect::<Vec<_>>(), [first, second]);
        // 请求体只有 request_id、status、detail，同一份回执每次补报都完全相同（controller 按 request_id 只记一次）。
        assert_eq!(found[0].body(), json!({ "request_id": "aaaaaaaa-1111-4111-8111-111111111111", "status": "applied", "detail": { "changed": ["a"] } }));
        assert_eq!(found[0].path(), format!("/projects/{PROJECT}/applications/{first}"));
        // 视图：属于别的连接的只列出，不补报。
        assert_eq!(unreported_view(&found[1], false, None)["this_connection"], false);

        let path = report_path(&dir, first);
        // 不可达、授权被拒、5xx：回执保留，等原 controller 与原身份恢复后补报。
        for code in [StatusCode::UNAUTHORIZED, StatusCode::FORBIDDEN, StatusCode::BAD_GATEWAY] {
            assert_eq!(settle_report(&path, code, b"{}"), None);
            assert!(path.exists());
        }
        // 2xx（第一次记下，或重放得到第一次的结果）：回执结束。
        assert_eq!(settle_report(&path, StatusCode::OK, br#"{"status": "applied"}"#), Some("applied".into()));
        assert!(!path.exists());
        // 明确的拒绝（记录已有最终结果）：重放不会改变它，回执结束并如实说明。
        let path = report_path(&dir, second);
        assert_eq!(settle_report(&path, StatusCode::CONFLICT, b"{}"), Some("not_recorded:409".into()));
        assert!(!path.exists());
        assert!(pending_reports(&dir).is_empty());
    }

    #[test]
    fn stable_request_id_matches_the_cli() {
        // 对照向量来自 agent_collab/project_cli.py 的 stable_request_id。
        assert_eq!(stable_request_id(&["baseline", PROJECT, &"ab".repeat(32)]), "02541494-7297-400a-ab0a-6f9776317584");
        assert_eq!(stable_request_id(&["baseline", PROJECT, &"ab".repeat(32)]), stable_request_id(&["baseline", PROJECT, &"ab".repeat(32)]));
        assert_ne!(stable_request_id(&["baseline", PROJECT, &"ab".repeat(32)]), stable_request_id(&["baseline", OTHER, &"ab".repeat(32)]));
        assert!(validate::uuid(&stable_request_id(&["x"])));
    }

    fn snapshot(root: &Path) -> Value {
        json!({
            "snapshot_id": "aaaaaaaa-2222-4333-8444-555555555555", "project_id": PROJECT, "root": root.to_str().unwrap(),
            "manifest": { "version": 1, "base": null, "files": [{ "path": "a.txt", "sha256": HELLO_SHA256, "size": 5, "executable": false }], "deleted": [] },
            "manifest_sha256": "ab".repeat(32), "manifest_bytes": 120, "files": 1, "bytes": 5,
            "exclude": [".git/**", ".env"], "excluded": { "count": 3, "top": [".git/"] }, "skipped": [{ "path": "x", "reason": "link" }],
            "skipped_counts": { "link": 1 }, "git": { "available": true, "branch": "main", "commit": "c0ffee", "uncommitted": 2 },
            "locations": {}, "stamps": { "a.txt": [5, 1] }, "scanned_at": "2026-11-12T01:02:03Z",
        })
    }

    #[test]
    fn baseline_request_matches_the_cli_and_carries_no_local_path() {
        let root = std::env::temp_dir().join("my-project");
        let snapshot = snapshot(&root);
        let body = baseline_request(PROJECT, "演示项目", &snapshot, "DESK-1");
        // 对照向量：stable_request_id('baseline', PROJECT, <snapshot_id>)（Python 一侧算出）。
        assert_eq!(body["request_id"], "2937df8f-df5f-4274-99a2-76281e167275");
        assert_eq!(body["name"], "演示项目 @ 2026-11-12T01:02:03Z");
        assert_eq!(body["project_id"], PROJECT);
        assert_eq!(body["manifest"], snapshot["manifest"]);
        assert_eq!(body.as_object().unwrap().len(), 5); // controller 拒绝多余的字段
        assert_eq!(
            body["source"],
            json!({
                "device": "DESK-1", "platform": platform(), "root_name": "my-project",
                "git": { "available": true, "branch": "main", "commit": "c0ffee", "uncommitted": 2 },
                "exclude": [".git/**", ".env"], "excluded": 3, "skipped": { "link": 1 }, "scanned_at": "2026-11-12T01:02:03Z",
            })
        );
        // 目录的绝对路径不出现在请求里的任何地方。
        let parent = root.parent().unwrap().to_str().unwrap().to_string();
        let text = body.to_string();
        assert!(!text.contains(&parent) && !text.contains(&parent.replace('\\', "\\\\")), "{text}");
        assert!(["win32", "darwin", "linux"].contains(&platform()));

        // 名称不超过 controller 的 120 个字符上限，扫描时间保留。
        let long = baseline_request(PROJECT, &"长".repeat(300), &snapshot, "DESK-1");
        let name = long["name"].as_str().unwrap();
        assert_eq!(name.chars().count(), 120);
        assert!(name.ends_with(" @ 2026-11-12T01:02:03Z"));
    }

    #[test]
    fn scan_view_describes_the_scope_without_the_manifest_or_the_path() {
        let root = std::env::temp_dir().join("my-project");
        let view = scan_view(&snapshot(&root));
        let mut keys: Vec<&str> = view.as_object().unwrap().keys().map(String::as_str).collect();
        keys.sort();
        assert_eq!(keys, [
            "bytes", "exclude", "excluded", "files", "git", "manifest_bytes", "manifest_sha256", "root_name", "scanned_at", "skipped",
            "skipped_counts", "snapshot_id",
        ]);
        assert_eq!(view["root_name"], "my-project");
        assert_eq!(view["excluded"], json!({ "count": 3, "top": [".git/"] }));
        assert_eq!(view["files"], 1);
    }

    #[test]
    fn project_excludes_come_first_and_duplicates_are_dropped() {
        let list = |items: &[&str]| items.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        assert_eq!(merge_excludes(list(&["dist/**", ".env", "dist/**"]), list(&[".git/**", ".env", "*.pem"])), list(&["dist/**", ".env", ".git/**", "*.pem"]));
        assert_eq!(merge_excludes(vec![], list(&[".git/**"])), list(&[".git/**"]));
    }

    #[test]
    fn progress_lines_become_events_with_contract_fields_only() {
        let event = progress(4, "scan", &json!({ "phase": "scan", "files": 200, "bytes": 4096, "secret": "x" })).unwrap();
        assert_eq!(serde_json::to_value(&event).unwrap(), json!({ "epoch": 4, "op": "scan", "phase": "scan", "files": 200, "bytes": 4096 }));
        let event = progress(4, "upload", &json!({ "phase": "stage", "done": 3, "total": 9 })).unwrap();
        assert_eq!(serde_json::to_value(&event).unwrap(), json!({ "epoch": 4, "op": "upload", "phase": "stage", "done": 3, "total": 9 }));
        // 重扫的进度只有阶段名；没有阶段名的行不转发。
        assert_eq!(serde_json::to_value(progress(4, "scan", &json!({ "phase": "rescan", "attempt": 1, "changed": 2 })).unwrap()).unwrap(), json!({ "epoch": 4, "op": "scan", "phase": "rescan" }));
        assert_eq!(progress(4, "scan", &json!({ "files": 1 })), None);
        assert_eq!(progress(4, "scan", &json!("text")), None);
    }

    #[test]
    fn blob_names_must_be_sha256_hex() {
        let entry = |sha: &str, size: u64| json!({ "path": "p", "sha256": sha, "size": size, "executable": false });
        let ok = [entry(HELLO_SHA256, 5), entry(&"ab".repeat(32), 7), entry(HELLO_SHA256, 5)];
        let blobs = blobs_of(&ok).unwrap();
        assert_eq!(blobs.len(), 2); // 相同内容只取一次
        assert_eq!(blobs[HELLO_SHA256], 5);
        for bad in [
            entry("../../etc/passwd", 1),
            entry(&"AB".repeat(32), 1),
            entry(&"a".repeat(63), 1),
            entry(&format!("{}/x", "a".repeat(62)), 1),
            json!({ "sha256": HELLO_SHA256 }),
            json!({ "sha256": HELLO_SHA256, "size": -1 }),
            json!(null),
        ] {
            assert_eq!(code(blobs_of([&bad])), "io", "{bad}");
        }
        assert!(blobs_of(&[] as &[Value]).unwrap().is_empty());
    }

    #[test]
    fn staged_files_are_uploaded_only_when_their_content_matches_their_name() {
        let dir = scratch("staged");
        let path = dir.join(HELLO_SHA256);
        std::fs::write(&path, b"hello").unwrap();
        assert_eq!(read_staged(&path, HELLO_SHA256, Some(5)).unwrap(), b"hello");
        assert_eq!(read_staged(&path, HELLO_SHA256, None).unwrap(), b"hello");
        // 超过单文件上限、内容与名称不符、文件不存在。
        assert_eq!(code(read_staged(&path, HELLO_SHA256, Some(4))), "invalid");
        std::fs::write(&path, b"tampered").unwrap();
        assert_eq!(code(read_staged(&path, HELLO_SHA256, None)), "blob_mismatch");
        assert_eq!(code(read_staged(&dir.join("missing"), HELLO_SHA256, None)), "io");
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn staging_directory_is_removed_when_the_operation_ends() {
        let state = scratch("staging");
        let staging = Staging::create(&state).unwrap();
        let dir = staging.0.clone();
        assert!(staging.blobs().is_dir());
        assert!(dir.starts_with(state.join("staging")) && validate::uuid(dir.file_name().unwrap().to_str().unwrap()));
        std::fs::write(staging.blobs().join(HELLO_SHA256), b"hello").unwrap();
        std::fs::write(dir.join("need.json"), b"[]").unwrap();
        drop(staging);
        assert!(!dir.exists());
        std::fs::remove_dir_all(state).unwrap();
    }

    #[test]
    fn snapshots_and_contexts_are_read_from_the_state_directory() {
        let state = scratch("state");
        let id = "aaaaaaaa-2222-4333-8444-555555555555";
        assert_eq!(code(read_snapshot(&state, id)), "no_snapshot");
        std::fs::create_dir_all(state.join("snapshots")).unwrap();
        std::fs::write(state.join("snapshots").join(format!("{id}.json")), b"{\"project_id\": \"p\"}").unwrap();
        assert_eq!(read_snapshot(&state, id).unwrap()["project_id"], "p");
        std::fs::write(state.join("snapshots").join(format!("{id}.json")), b"not json").unwrap();
        assert_eq!(code(read_snapshot(&state, id)), "io");

        assert_eq!(read_context(&state, id), None);
        write_json(&state.join("applies").join(id).join("context.json"), &json!({ "project_id": PROJECT, "job_id": OTHER, "controller_id": "ctl-a" })).unwrap();
        assert_eq!(read_context(&state, id).unwrap()["controller_id"], "ctl-a");
        std::fs::remove_dir_all(state).unwrap();
    }

    #[test]
    fn job_inputs_become_the_base_manifest() {
        let job = json!({ "contract": {
            "inputs_manifest": { "version": 1, "base": "ff".repeat(32), "files": [{ "path": "a" }], "deleted": ["b"] },
            "workspace": { "exclude": ["dist/**"] },
        }});
        let (base, exclude) = base_and_exclude(&job);
        assert_eq!(base, json!({ "version": 1, "base": null, "files": [{ "path": "a" }], "deleted": [] }));
        assert_eq!(exclude, Some(json!(["dist/**"])));
        // 没有输入的任务用空 manifest；契约没有排除规则时不传，由助手用缺省规则。空列表是“明确不排除”，照传。
        let (base, exclude) = base_and_exclude(&json!({ "contract": { "inputs_manifest": null } }));
        assert_eq!(base, json!({ "version": 1, "base": null, "files": [], "deleted": [] }));
        assert_eq!(exclude, None);
        assert_eq!(base_and_exclude(&json!({ "contract": { "workspace": { "exclude": [] } } })).1, Some(json!([])));
        assert_eq!(manifest_of(&[json!({ "path": "x" })]), json!({ "version": 1, "base": null, "files": [{ "path": "x" }], "deleted": [] }));
    }

    fn change(path: &str, action: &str, state: &str) -> Value {
        let mut change = json!({ "path": path, "action": action, "state": state, "size": 3 });
        match state {
            "clean" => change["expect"] = json!("ab".repeat(32)),
            "conflict" => change["reason"] = json!("modified_locally"),
            _ => {}
        }
        change
    }

    #[test]
    fn plan_entries_keep_only_contract_fields() {
        assert_eq!(change_view(&change("a", "modify", "clean")), json!({ "path": "a", "action": "modify", "state": "clean", "size": 3 }));
        assert_eq!(
            change_view(&change("b", "add", "conflict")),
            json!({ "path": "b", "action": "add", "state": "conflict", "size": 3, "reason": "modified_locally" })
        );
    }

    #[test]
    fn conflicts_or_nothing_to_do_settle_without_registering() {
        let changes = [change("a", "modify", "clean"), change("b", "add", "conflict"), change("c", "delete", "already")];
        // 有冲突而没有选择跳过：不写入，可应用的文件列为 pending。
        let settled = settled_without_writing(&changes, false).unwrap();
        assert_eq!(settled, json!({
            "application_id": null, "status": "conflict", "changed": [], "already": ["c"], "pending": ["a"],
            "conflicts": [{ "path": "b", "reason": "modified_locally" }], "errors": [], "reported": null,
        }));
        // 选择跳过冲突且有可应用的改动：需要写入。
        assert_eq!(settled_without_writing(&changes, true), None);
        assert_eq!(settled_without_writing(&changes[..1], false), None);
        // 没有可应用的改动：全部已是成果内容为 applied，只剩冲突为 conflict。
        assert_eq!(settled_without_writing(&changes[2..], false).unwrap()["status"], "applied");
        assert_eq!(settled_without_writing(&[], true).unwrap()["status"], "applied");
        for skip in [false, true] {
            let only_conflicts = settled_without_writing(&changes[1..], skip).unwrap();
            assert_eq!((only_conflicts["status"].clone(), only_conflicts["pending"].clone()), (json!("conflict"), json!([])));
        }
    }

    #[test]
    fn application_reports_use_the_cli_fields() {
        let result = json!({
            "application_id": "x", "root": "/work/one", "status": "partial", "changed": ["a"], "pending": [], "already": ["c"],
            "conflicts": [{ "path": "b", "reason": "changed_since_plan" }], "errors": [], "backup": "/state/applies/x/backup",
            "summary": { "clean": 1 }, "restored": null,
        });
        let (status, detail) = application_report(&result, APPLY_DETAIL);
        assert_eq!(status, "partial");
        assert_eq!(detail, json!({
            "changed": ["a"], "pending": [], "already": ["c"], "conflicts": [{ "path": "b", "reason": "changed_since_plan" }],
            "errors": [],
        }));
        // 项目目录（root）与备份目录（backup）都是本机路径，不上报。
        assert!(!detail.to_string().contains("/work/one") && !detail.to_string().contains("/state/applies"));

        // 回滚在 controller 上记为 failed；恢复上报的字段比应用少。
        let rolled = json!({ "status": "rolled_back", "restored": ["a"], "kept": [], "written": ["a"], "remaining": [], "diverged": [], "backup": "/b", "root": "/r" });
        let (status, detail) = application_report(&rolled, RECOVER_DETAIL);
        assert_eq!(status, "failed");
        assert_eq!(detail, json!({ "restored": ["a"], "kept": [], "written": ["a"], "remaining": [], "diverged": [], "rolled_back": true }));
        assert_eq!(application_report(&json!({ "status": "unknown" }), RECOVER_DETAIL), ("unknown".to_string(), json!({})));
        assert_eq!(application_report(&json!({}), RECOVER_DETAIL).0, "unknown");

        assert_eq!(apply_view(&result, "x", Some("partial".into())), json!({
            "application_id": "x", "status": "partial", "changed": ["a"], "already": ["c"], "pending": [],
            "conflicts": [{ "path": "b", "reason": "changed_since_plan" }], "errors": [], "cancelled": false, "reported": "partial",
        }));
        // 被取消而停下的应用：上报的明细与返回给页面的结果都如实带上 cancelled。
        let stopped = json!({ "status": "partial", "changed": ["a"], "pending": ["b"], "conflicts": [], "errors": [], "cancelled": true });
        assert_eq!(application_report(&stopped, APPLY_DETAIL).1["cancelled"], true);
        assert_eq!(apply_view(&stopped, "x", None)["cancelled"], true);
        assert!(application_report(&result, APPLY_DETAIL).1.get("cancelled").is_none());
        assert_eq!(apply_view(&result, "x", None)["reported"], Value::Null);
    }

    #[test]
    fn oversized_report_details_are_truncated_instead_of_rejected() {
        let small = json!({ "changed": ["a", "b"], "errors": [] });
        assert_eq!(fit_detail(small.clone()), small);
        let paths: Vec<String> = (0..20000).map(|n| format!("src/module-{n}/file-{n}.txt")).collect();
        let fitted = fit_detail(json!({ "changed": paths, "errors": [], "kept": ["k"] }));
        assert_eq!(fitted["changed"].as_array().unwrap().len(), 100);
        assert_eq!(fitted["truncated"], json!({ "changed": 20000 }));
        assert_eq!(fitted["kept"], json!(["k"]));
        assert!(fitted.to_string().len() < 256 * 1024);
    }

    #[test]
    fn resume_needs_only_the_content_of_files_not_yet_written() {
        let entry = |sha: &str, size: u64| json!({ "path": "p", "sha256": sha, "size": size, "executable": false });
        let journal = json!({
            "changes": [
                { "path": "done.txt", "action": "modify" }, { "path": "todo.txt", "action": "add" },
                { "path": "gone.txt", "action": "delete" }, { "path": "same.txt", "action": "add" },
            ],
            "files": { "done.txt": entry(&"11".repeat(32), 1), "todo.txt": entry(HELLO_SHA256, 5), "gone.txt": null, "same.txt": entry(HELLO_SHA256, 5) },
        });
        let status = json!({ "written": ["done.txt"], "remaining": ["todo.txt", "gone.txt", "same.txt"], "diverged": [] });
        assert_eq!(remaining_blobs(&journal, &status).unwrap(), BTreeMap::from([(HELLO_SHA256.to_string(), 5)]));
        assert!(remaining_blobs(&journal, &json!({ "remaining": [] })).unwrap().is_empty());
        // 日志里缺了待写文件的条目：不猜，报错。
        let broken = json!({ "changes": [{ "path": "todo.txt", "action": "add" }], "files": {} });
        assert_eq!(code(remaining_blobs(&broken, &status)), "io");
    }

    #[test]
    fn unfinished_applications_are_listed_for_their_own_controller() {
        let mine = "aaaaaaaa-2222-4333-8444-555555555555";
        let theirs = "bbbbbbbb-2222-4333-8444-555555555555";
        let orphan = "cccccccc-2222-4333-8444-555555555555";
        let root = std::env::temp_dir().join("my-project");
        let entry = |id: &str| json!({ "application_id": id, "root": root.to_str().unwrap(), "started_at": "2026-11-12T01:02:03Z", "journal": "/state/x" });
        let entries = [entry(mine), entry(theirs), entry(orphan), entry("../../escape"), json!({})];
        let context = |id: &str| match id {
            id if id == mine => Some(json!({ "project_id": PROJECT, "job_id": OTHER, "controller_id": "ctl-a" })),
            id if id == theirs => Some(json!({ "project_id": PROJECT, "job_id": OTHER, "controller_id": "ctl-b" })),
            _ => None,
        };
        assert_eq!(unfinished_view(&entries, "ctl-a", context), [
            json!({ "application_id": mine, "root_name": "my-project", "started_at": "2026-11-12T01:02:03Z", "project_id": PROJECT, "job_id": OTHER }),
            json!({ "application_id": orphan, "root_name": "my-project", "started_at": "2026-11-12T01:02:03Z", "project_id": null, "job_id": null }),
        ]);
        assert!(unfinished_view(&[], "ctl-a", context).is_empty());

        let report = json!({ "application_id": mine, "root": root.to_str().unwrap(), "status": "writing", "interrupted": true, "written": [], "remaining": ["a"], "diverged": [] });
        let view = recover_view(&report);
        assert_eq!(view["root_name"], "my-project");
        assert!(view.get("root").is_none());
        assert_eq!((view["interrupted"].clone(), view["remaining"].clone()), (json!(true), json!(["a"])));
    }
}
