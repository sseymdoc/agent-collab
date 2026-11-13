# 第三方与来源清单

| 组件 | 来源 | 用途 | 许可证 | 状态 |
| --- | --- | --- | --- | --- |
| codex-collaborate launcher/workflow | 本用户自有，`~/.claude/skills/codex-collaborate/scripts/{launcher,workflow}.py`（sha256 前缀 `7862865F` / `A2D23644`，2026-11-09 版） | 进程托管、Windows Job Object、codex exec 参数与报告 schema 的移植来源 | MIT（随本仓库） | 移植处在代码头注明来源 |
| Python 标准库 | CPython | 运行时 | PSF | — |
| 随包 Python 运行时：CPython 3.14.9 | python-build-standalone 构建；取自构建机上已有的 uv 管理副本，没有新下载 | 桌面安装包与本机部署的运行时（controller、节点 agent、本机助手），随包分发 | PSF；CPython 捆绑的组件（OpenSSL 3.5.9 等）按各自许可证 | 运行时目录内的许可证文本随包分发（Windows `python\LICENSE.txt`，macOS `python/lib/python3.14/LICENSE.txt`），其中含捆绑组件的许可证说明；打包时去掉了 pip、测试、IDLE、tkinter/Tcl/Tk |
| React、React DOM | npm | 控制台运行时，打包进 `agent_collab/static/` 随发行包分发 | MIT | 版本以 `ui/package-lock.json` 为准；构建时把打包依赖的许可证文本写入 `agent_collab/static/third-party-licenses.md` |
| Vite、@vitejs/plugin-react、TypeScript | npm | 仅构建期，不随包分发 | MIT / Apache-2.0 | 版本以 `ui/package-lock.json` 为准 |
| 桌面外壳的 Rust 依赖（含间接依赖） | crates.io | 编入桌面外壳（`desktop/src-tauri`），随桌面安装包分发 | 逐个见 `desktop/THIRD_PARTY_RUST.md`（Windows x64 与 macOS arm64 合计 324 个 crate）；其中 cssparser、cssparser-macros、dtoa-short、option-ext、selectors 为 MPL-2.0 | 版本以 `desktop/src-tauri/Cargo.lock` 为准；直接依赖以 `desktop/src-tauri/Cargo.toml` 为准，见下面几行（括号里是当前锁定的版本）；MPL-2.0 的 crate 未作修改；桌面安装包在 `resources/licenses/` 里随附本清单与各 crate 的许可证文本（`desktop/THIRD_PARTY_RUST_LICENSES.txt`，由 `scripts/rust_licenses.py` 生成；包里没有许可证文件的 crate 用上游原文补充，出处见 `desktop/third_party_rust/sources.json`） |
| tauri（2.12.5）、tauri-build（2.7.4） | crates.io | 桌面外壳框架；tauri-build 仅构建期 | Apache-2.0 OR MIT | — |
| serde（1.0.229）、serde_json（1.0.154） | crates.io | 原生桥参数与 JSON 处理 | MIT OR Apache-2.0 | — |
| reqwest（0.12.28） | crates.io | 代发请求与事件流；不带缺省 feature，只用 rustls 与系统根证书 | MIT OR Apache-2.0 | — |
| keyring（3.6.3） | crates.io | 客户端凭据存入系统存储（macOS 钥匙串、Windows 凭据管理器） | MIT OR Apache-2.0 | — |
| rfd（0.17.2） | crates.io | 系统对话框（另存为、选择文件夹、打开文件） | MIT | 不注册 tauri-plugin-dialog |
| sha2（0.10.9）、url（2.5.8） | crates.io | 内容哈希校验；地址校验 | MIT OR Apache-2.0 | — |
| tokio（1.54.1） | crates.io | 同步原语（只开 `sync` feature） | MIT | — |
| uuid（1.29.2）、base64（0.22.1） | crates.io | 生成 request_id 与应用 ID；解码 PEM 证书 | Apache-2.0 OR MIT / MIT OR Apache-2.0 | 本来就由 tauri、reqwest 带入，改为直接依赖 |
| tauri-plugin-notification（2.8.1） | crates.io | 系统通知 | Apache-2.0 OR MIT | 只用它的 Rust API，不授予它自带的页面权限 |
| mac-notification-sys（0.6.15）、tauri-winrt-notification（0.8.1） | crates.io，由 tauri-plugin-notification 带入 | macOS 与 Windows 的通知后端 | MIT/Apache-2.0 / MIT OR Apache-2.0 | 分别只进入 macOS arm64 与 Windows x64 构建 |
| Codex CLI、Claude Code | OpenAI / Anthropic 官方安装 | 由节点调用，不随包分发 | 各自条款 | 不转售、不再分发、不代理其认证 |

## MPL-2.0 组件的源码

桌面外壳以可执行形式包含下列 MPL-2.0 的 crate（MPL-2.0 第 3.2 节）。它们都未作修改，源码就是 `desktop/src-tauri/Cargo.lock` 锁定版本在 crates.io 上的发布包；括号里是发布该版本时的上游提交（取自包内的 `.cargo_vcs_info.json`）。如果以后修改了其中任何一个，要改为提供修改后的源码。

| crate | 锁定版本 | 源码（发布包） | 上游提交 |
| --- | --- | --- | --- |
| selectors | 0.38.0 | https://static.crates.io/crates/selectors/selectors-0.38.0.crate | https://github.com/servo/stylo/tree/572ecba2d1600e7c3d490586692a209faf703baa/selectors |
| cssparser | 0.37.0 | https://static.crates.io/crates/cssparser/cssparser-0.37.0.crate | https://github.com/servo/rust-cssparser/tree/4c49486494fb24dc01390e3baca9698ef1744c71 |
| cssparser-macros | 0.7.1 | https://static.crates.io/crates/cssparser-macros/cssparser-macros-0.7.1.crate | https://github.com/servo/rust-cssparser/tree/d128a90e1203fc4afb959420d6dc5e24f77a3191/macros |
| dtoa-short | 0.3.5 | https://static.crates.io/crates/dtoa-short/dtoa-short-0.3.5.crate | https://github.com/upsuper/dtoa-short/tree/2d905cdb8b2e08163dc0d015f529877fe657b4ef |
| option-ext | 0.2.0 | https://static.crates.io/crates/option-ext/option-ext-0.2.0.crate | https://github.com/soc/option-ext/tree/272f22fc9ea1ac6b08f01704af52c4ac338df4e2 |
