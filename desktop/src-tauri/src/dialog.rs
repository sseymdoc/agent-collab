//! 系统对话框：另存为、选择文件夹、打开文件。
//!
//! 外壳读写的本机位置都从这里的返回值来（见 `docs/desktop.md`“权限隔离”），页面不能提供路径。

use std::future::Future;
use std::path::PathBuf;

use tauri::WebviewWindow;

use super::{io, Result};

/// 仅 `automation` feature：环境变量存在时用它的值代替对话框的返回值，空字符串表示用户取消。
/// 用替身做的验证不算系统对话框的验证。
#[cfg(feature = "automation")]
fn stub(var: &str) -> Option<Option<PathBuf>> {
    std::env::var_os(var).map(|value| (!value.is_empty()).then(|| PathBuf::from(value)))
}

/// 弹出一个对话框；用户取消时返回 None。
async fn show<F>(window: &WebviewWindow, open: impl FnOnce(rfd::AsyncFileDialog) -> F + Send + 'static) -> Result<Option<PathBuf>>
where
    F: Future<Output = Option<rfd::FileHandle>> + Send + 'static,
{
    let (tx, rx) = tokio::sync::oneshot::channel();
    let parent = window.clone();
    // 对话框要在主线程上创建（macOS 的要求）；创建后得到的 future 可以在任意线程等待。
    window
        .run_on_main_thread(move || {
            let _ = tx.send(open(rfd::AsyncFileDialog::new().set_parent(&parent)));
        })
        .map_err(io)?;
    let picked = rx.await.map_err(|_| io("无法打开系统对话框"))?.await;
    Ok(picked.map(|file| file.path().to_path_buf()))
}

/// “另存为”对话框。
pub async fn save(window: &WebviewWindow, file_name: String) -> Result<Option<PathBuf>> {
    #[cfg(feature = "automation")]
    if let Some(answer) = stub("AGENT_COLLAB_AUTOMATION_SAVE_PATH") {
        return Ok(answer);
    }
    show(window, move |dialog| {
        let dialog = if file_name.is_empty() { dialog } else { dialog.set_file_name(file_name) };
        dialog.save_file()
    })
    .await
}

/// 选择文件夹对话框。
pub async fn pick_folder(window: &WebviewWindow, title: &'static str) -> Result<Option<PathBuf>> {
    #[cfg(feature = "automation")]
    if let Some(answer) = stub("AGENT_COLLAB_AUTOMATION_PICK_FOLDER") {
        return Ok(answer);
    }
    show(window, move |dialog| dialog.set_title(title).pick_folder()).await
}

/// 打开文件对话框；`extensions` 是可选的文件类型（另有“所有文件”）。
pub async fn pick_file(window: &WebviewWindow, title: &'static str, kind: &'static str, extensions: &'static [&'static str]) -> Result<Option<PathBuf>> {
    #[cfg(feature = "automation")]
    if let Some(answer) = stub("AGENT_COLLAB_AUTOMATION_PICK_FILE") {
        return Ok(answer);
    }
    show(window, move |dialog| dialog.set_title(title).add_filter(kind, extensions).add_filter("*", &["*"]).pick_file()).await
}
