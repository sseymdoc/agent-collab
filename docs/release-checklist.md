# 发布前清单

每次对外发行（导出公开仓库、发行包、桌面安装包）之前逐项核对。本清单列要求、核对方法与已经定下的发行配置（长期密钥的指纹、托管与发布位置）；每次发行的核对结果与证据记在发行记录里，不写进本文。

## 1. 许可证

- [ ] 本项目的许可证是 MIT（[LICENSE](../LICENSE)）。`pyproject.toml` 的 `license` 字段与 README 的“许可证”一节与之一致。
- [ ] 许可材料随包（门禁对所有构建身份核对，不依赖先发布源码）：`scripts/build_release.py` 把 `LICENSE` 与 `THIRD_PARTY.md` 放进后台发行包的 `agent_collab/`；`scripts/build_desktop.py` 把 `LICENSE`、`THIRD_PARTY.md`、`desktop/THIRD_PARTY_RUST.md`、`desktop/THIRD_PARTY_RUST_LICENSES.txt` 与页面的 `third-party-licenses.md` 放进桌面安装包的 `resources/licenses/`（在载荷清单里），Python 运行时的许可证随运行时目录。
- [ ] 发布时另行核对“源码与产物对应”（不属于构建门禁）：要发行的提交已经导出到公开仓库并打上与版本号一致的标签；产品清单里的 `source.commit` 是这个提交；下载位置上的文件与清单里的名称、大小和 SHA-256 一致。

## 2. 第三方依赖与许可证文本

- [ ] Python 部分只用标准库：`pyproject.toml` 的 `dependencies` 为空，没有新增第三方 Python 依赖。
- [ ] 页面：产物 `agent_collab/static/` 里打包进了 React 与 React DOM（MIT）。构建时把打包依赖的许可证文本写入 `agent_collab/static/third-party-licenses.md`；核对这个文件在页面产物、发行包与桌面安装包里都存在。Vite、TypeScript 等只在构建期使用，不随包分发。
- [ ] 桌面外壳的 Rust 依赖：tauri、wry、reqwest 与 rustls、keyring、rfd、sha2、tokio、serde 等，直接依赖与间接依赖以 `desktop/src-tauri/Cargo.lock` 为准。许可证清单在 `desktop/THIRD_PARTY_RUST.md`（按 `Cargo.lock` 生成），许可证文本在 `desktop/THIRD_PARTY_RUST_LICENSES.txt`（`python scripts/rust_licenses.py` 从本机 cargo 缓存里的 crate 包收集）；依赖变化后两份一起重新生成。包里没有许可证文件的 crate，用 `desktop/third_party_rust/` 里的补充文本：上游发布该版本时提交上的原始许可文件，或可追溯的标准文本（SPDX 的 MIT、Mozilla 的 MPL-2.0），出处、上游提交与 SHA-256 记在 `sources.json`；有多种许可可选（OR）时写明选用哪一种；确实不适用的 crate 只能在那里写明理由。`python scripts/rust_licenses.py --check` 用 `cargo metadata` 按两个发行目标解析 `Cargo.lock`，依赖不在清单里、没有文本也没有理由、补充文件摘要不符、生成文件过期，都算不完整；门禁运行同一项核对。
- [ ] 随包的 Python 运行时：CPython，PSF 许可证，许可证文本随运行时一起分发。若使用 python-build-standalone 的构建，还要带上它附带的各组件许可证文件。
- [ ] 核对并更新 [THIRD_PARTY.md](../THIRD_PARTY.md)：页面依赖、桌面外壳的 Rust 依赖与随包运行时都在清单里，来源、用途与许可证写明。

## 3. 模型 CLI 与认证

- [ ] 不随包分发、不代理、不转售 Codex CLI、Claude Code 或任何模型服务及其认证。节点使用用户本机按官方方式安装的工具与官方登录，本产品不复制、不中转凭据。核对发行包与桌面安装包里没有这些工具的二进制、登录资料或凭据，`THIRD_PARTY.md` 里的相应条目保留。

## 4. 桌面安装包

构建身份有三种，定义与取值见 [ADR 0004](adr/0004-release-identity-updates-and-compatibility.md) 第 1 条，界面与 `app_info.kind` 如实标明：

| 身份 | 版本号 | `automation` feature | 实例名 / 端口 | 凭据服务名 | 更新通道 | 对外分发 |
| --- | --- | --- | --- | --- | --- | --- |
| `formal` 正式版 | 不带预发布后缀 | 不带 | 空 / 18669 | `agent-collab` | `stable`（缺省）、`preview` | 是 |
| `candidate` 开发候选版 | 带预发布后缀 | 不带 | `cand` / 18769 | `agent-collab-cand` | `stable`、`preview`（测试地址） | 可对外试用 |
| `internal-test` 内部测试版 | 带预发布后缀 | 编译进 | `itest` / 18779 | `agent-collab-itest` | `internal-test`（测试地址） | 否；文件名带 `internal-test` |

发行构建一律经 `scripts/build_desktop.py build`，结束前运行 `scripts/build_desktop.py gate --dist <dist> --test-pubkey <测试公钥> --require-platforms windows-x86_64,darwin-aarch64`。门禁逐项输出并写 `gate-report.json`，任何一项失败即整体失败，下面各项都在其中：

- [ ] 身份、应用标识、实例与通道与上表一致；版本号与身份相符。
- [ ] `automation` 只出现在内部测试版；文件名的 `internal-test` 标记与身份相符；内部测试包不放进任何对外的下载位置与发行说明。
- [ ] 源检出干净；随包后台发行包来自同一提交、`git_dirty` 为 false。
- [ ] 随包载荷有 `payload.json`，安装后 `app_info.payload` 为 true、`helper` 为 true；正式版不带测试夹具模块。
- [ ] 每个产物的 SHA-256 与大小和平台片段一致；更新签名绑定的版本与文件名等于最终分发的那个文件。
- [ ] 所有身份：后台发行包带 `LICENSE` 与 `THIRD_PARTY.md`；桌面安装包的 `resources/licenses/` 有全部许可材料，运行时许可证在。
- [ ] 第三方许可材料完整（`scripts/rust_licenses.py` 的核对通过），每个平台包里的 Rust 依赖清单与许可证文本就是核对过的那一份。门禁在有两个平台全部 crate 的构建机上运行。
- [ ] 正式版：不带测试验签公钥；更新地址是 https、没有不安全传输开关；有平台代码签名并验证通过；macOS 公证、stapling 完成且 Gatekeeper 接受；后台发行包带来源签名。测试身份：不带正式公钥。
- [ ] 至少一个通道有 `latest.json`（先运行 `manifest`）；`latest.json` 的版本等于发行 ID 且各平台条目完整；`product-manifest.json` 有签名。

## 5. 签名、公证与更新

流水线已接入（`scripts/build_desktop.py`），顺序见 [desktop.md](desktop.md)“构建”：嵌套代码先签名再生成载荷清单，应用与安装包按身份签名，macOS 公证与 stapling 之后才打更新归档，更新签名与摘要都从最终产物生成。三把信任根分开（ADR 0004 第 8 条）。正式发行需要下列资产，缺任何一项门禁都不通过：

| 资产 | 用途 | 配置入口 | 缺失时阻断的步骤 |
| --- | --- | --- | --- |
| Windows 代码签名证书（Authenticode，带时间戳服务） | 签随包运行时二进制、主程序与 NSIS 安装包：让系统验证发布者与完整性、满足要求签名的平台策略 | 构建环境 `AGENT_COLLAB_SIGNTOOL`、`AGENT_COLLAB_SIGN_CERT_SHA1`、`AGENT_COLLAB_TIMESTAMP_URL`（证书在本机证书存储或硬件令牌里，不经聊天传递） | 门禁“平台代码签名”。签名有效与 SmartScreen 的实际表现分开记录：有效签名的新程序仍可能因信誉不足出现提示，不以“没有提示”为验收条件 |
| Apple Developer ID Application 证书 | 签嵌套代码与应用（hardened runtime、时间戳） | `AGENT_COLLAB_CODESIGN_IDENTITY`（登录钥匙串里的身份名） | 门禁“平台代码签名”“Gatekeeper 接受”。签名、公证、stapling、Gatekeeper 评估与真实下载后的首次启动分别验证，持有证书不等于正式分发通过 |
| App Store Connect 公证凭据 | `notarytool submit --wait` 与 `stapler staple` | `xcrun notarytool store-credentials <名称>` 存进钥匙串，构建时 `AGENT_COLLAB_NOTARY_PROFILE=<名称>` | 门禁“公证与 stapling” |
| 正式更新签名密钥（minisign，`tauri signer generate`） | 更新包与产品发行清单的签名；公钥编进正式版 | 私钥在托管与备份方案确定后生成，构建时 `--updater-key`；公钥文件 `--updater-pubkey` | 正式版的更新签名；生成后不能丢弃，否则已安装的正式版无法再更新 |
| 正式发行签名密钥（OpenSSH ed25519，命名空间 `agent-collab-release`） | 后台发行包的来源签名（无图形部署与本机部署向导核对） | `ssh-keygen -Y sign -f <私钥> -n agent-collab-release <发行包>`；随包与发布位置都放 `allowed_signers` | 门禁“后台发行包带来源签名”；首次安装会固定这份信任 |
| 正式 HTTPS 发布位置 | 放 `<通道>/latest.json`、`product-manifest.json(.sig)` 与更新包 | 构建时 `--update-base https://…` | 正式版的更新检查；不能用测试地址或 http |

### 正式资产接入步骤

首版采用人工触发的受控发行构建：Mac 构建机是 updater 签名与发行清单签名的唯一位置，Windows 构建机只持有自己的平台签名身份。每一步的产物在下一步之前冻结，签名之后不再重建或替换已嵌入的资源。

1. **长期密钥（已生成）。** 用 `scripts/release_keys.sh` 在 Mac 签名机上生成两把口令加密的密钥：updater 密钥（Tauri minisign）与发行签名密钥（OpenSSH ed25519，signer 与 namespace 都是 `agent-collab-release`）。公钥、`allowed_signers` 与指纹在仓库的 `release-keys/`：updater 公钥文件 SHA-256 `44245fda4a5a02d702f0488eaffe5b4c4c970b58cf733636f3358a2244fdc228`，发行签名公钥 `SHA256:LkClpWxIMI1GwvQkc4bG6nREDBw4DjGNGGkE9u3uY50`。
   - 主托管：签名机当前账户下只有该账户可读写的目录；两份随机口令在该账户的系统钥匙串里，签名在已登录的图形会话里进行，口令不经过命令行输出或聊天。
   - 异机备份：另一台机器上只授权当前用户的目录，内容是加密私钥、公钥与指纹；两份口令另以该用户的系统级加密（Windows DPAPI）存在同一目录，所以恢复不依赖原签名机。可选再加一份离线副本。
   - 恢复办法：在备份所在机器上解出口令，经管道交给 `release_keys.sh check <备份目录>`；它用备份解开两把私钥，updater 密钥签小文件并核对密钥 ID，发行签名密钥签小文件并按 `allowed_signers` 验签。
   - 两把密钥不复用任何人的 SSH 登录密钥，不在每次构建时重新生成；测试密钥不能改名充当正式根。轮换：updater 公钥随新版本发布（用旧私钥签的最后一个版本把新公钥带给用户），发行签名轮换用 `install --replace-trust` 并在发行说明里写明；丢失 updater 私钥意味着已安装的正式版无法再更新。
2. **平台签名身份（只需要引用，不经聊天传递证书、私钥或口令）。** 工具已安装不等于身份具备。Windows：Authenticode 证书在构建机证书存储或硬件令牌里（或既有的签名服务），构建环境给 `AGENT_COLLAB_SIGNTOOL`、`AGENT_COLLAB_SIGN_CERT_SHA1`（证书指纹）、`AGENT_COLLAB_TIMESTAMP_URL`；先用 `signtool sign /sha1 <指纹> /fd sha256 /tr <时间戳服务> /td sha256` 对样例程序签名、`signtool verify /pa` 通过。macOS：`security find-identity -v -p codesigning` 能看到 `Developer ID Application: … (<Team ID>)`，构建环境给 `AGENT_COLLAB_CODESIGN_IDENTITY`；公证凭据用 `xcrun notarytool store-credentials <名称>` 存进钥匙串（App Store Connect API 密钥，或 Apple ID 应用专用密码加 Team ID），构建环境给 `AGENT_COLLAB_NOTARY_PROFILE=<名称>`，先 `xcrun notarytool history --keychain-profile <名称>` 能列出记录。
3. **固定发行。** 定下发行提交、版本号（不带预发布后缀）、身份 `formal`、应用标识 `org.agent-collab.desktop`、通道 `stable`、最低系统（Windows 10.0.19045、macOS 11.0）、正式 updater 公钥与指纹、发行 signer 与 namespace、发布地址。
4. **构建。**
   1. Mac：`scripts/build_release.py --version <版本>` 构建后台发行包，`ssh-keygen -Y sign -f release.key -n agent-collab-release <后台发行包>` 签名（输入口令）。
   2. Windows：`scripts/build_desktop.py build --kind formal --release … --release-signature … --allowed-signers … --update-base <通道根> --updater-pubkey updater.key.pub`（不带 `--updater-key`），构建环境带上 Authenticode 配置：随包二进制先签名再写载荷清单，安装包由 Tauri 签名。把 `dist/` 原样送到 Mac：经两台机器之间认证的通道传送（例如 SSH），构建结束时在 Windows 上记下各产物的 SHA-256，Mac 补签前逐个对照，不一致就不签。`sign` 只核对产物与同一份 `dist/` 里的平台片段一致，片段本身不能证明来源；Windows 构建机持有 Authenticode 身份，本来就在信任范围内。
   3. Mac：`build --kind formal … --updater-key updater.key`（Developer ID、hardened runtime、公证与 stapling 之后才打更新归档，再生成更新签名）；把 Windows 的平台片段与产物放进同一个 `dist/`，`build_desktop.py sign --dist <dist> --updater-key updater.key` 给 Windows 更新包补签（字节与片段记录不符、或私钥与程序里的公钥不对应都拒绝）；`manifest --channel stable --base-url <通道根> --download-base <版本下载地址>`。
   4. Windows（有两个平台全部 crate 的 cargo 缓存）：`gate --require-platforms windows-x86_64,darwin-aarch64`。门禁会用 `cargo metadata` 核对第三方许可材料，Mac 上缺 Windows 专用的 crate 时无法离线解析依赖图。
5. **验证。** 在两台干净虚拟机里从真实下载（带下载来源标记）安装：Windows 记录 `Get-AuthenticodeSignature` 结果与 SmartScreen 的实际表现；macOS 记录 `codesign --verify --deep --strict`、`spctl -a -t exec -vv`、`stapler validate` 与首次打开的表现。再做一次正式根下的更新：第一次正式发行没有上一正式版，就用同一正式根再构建一个只在隔离环境使用、不发布的下一个版本（例如 0.3.0 → 0.3.1）；测试身份不能带正式公钥，不能拿候选代替。不以生产部署作为升级目标。
6. **发布（需要单独的发布决定）。** 见下面的托管与发布动作。

### 托管：GitHub Releases 与通道文件

首选公开仓库 `<owner>/agent-collab` 的 Releases，不默认新增下载站、域名或对象存储。

- **版本化产物放 Release 附件**：tag `desktop-v<版本>`（例如 `desktop-v0.3.0`）。附件：`agent-collab-desktop-<版本>-windows-x64-setup.exe` 与 `.sig`、`agent-collab-desktop-<版本>-macos-arm64.dmg`、`agent-collab-desktop-<版本>-macos-arm64.app.tar.gz` 与 `.sig`、`agent-collab-<后台版本>.tar.gz` 与 `.sig`、`allowed_signers`、`product-manifest.json` 与 `.sig`（存档）、`SHA256SUMS`。下载地址模板：`https://github.com/<owner>/agent-collab/releases/download/desktop-v<版本>/<文件名>`，即 `manifest --download-base` 的值；更新包下载会重定向到 GitHub 的文件服务器，updater 跟随重定向。内部测试身份的文件不上传。
- **通道文件放不重定向的 https 地址**：桌面端按 `<通道根>/<通道>/latest.json`、`<通道根>/<通道>/product-manifest.json(.sig)` 取，取产品发行清单时**不跟随重定向**（`update.rs` 的 `fetch`），而 Release 附件的下载地址都会重定向，所以通道文件不能放 Release 附件。通道文件放同一仓库的 GitHub Pages：通道根 `https://<owner>.github.io/agent-collab/desktop`（项目 Pages 没有自定义域名时直接返回 200，不跳转）。通道根编进正式版程序，以后给 Pages 配自定义域名会变成 301，取清单会失败，所以不要配；确需迁移时先发一个带新通道根的版本。Pages 的内容有缓存，推送后要等几分钟才对所有用户生效，回退同样。
- **通道与“最新 Release”分开**：桌面 stable 只由 `stable/latest.json` 决定，不使用 GitHub 的 `releases/latest`（仓库里别的发布也可能成为“最新”）。
- **动作与条件**（各自独立，前一步不授权后一步）：
  1. 本地草稿：在私有位置组装全部文件与 `SHA256SUMS`，门禁通过。
  2. 上传候选：建立 GitHub 草稿 Release（未公开）并上传附件，下载后逐个核对摘要；需要使用该仓库的账户授权。
  3. 公开发布：发布这个 Release（创建 tag），核对标签指向发行提交、公开下载的字节与清单一致。
  4. 更新 stable：把 `stable/latest.json` 与产品清单推到 Pages。这一步让所有正式版用户看到新版本，最后做，单独决定；回退时把它恢复成上一版的内容。
- **Pages 的最小配置**：来源用 `gh-pages` 分支的根目录，内容只有 `.nojekyll` 与 `desktop/<通道>/{latest.json, product-manifest.json, product-manifest.json.sig}`；`scripts/stage_pages.sh <dist> <通道> <工作目录>` 在本机组装这棵树并核对 `latest.json` 里的更新包地址是 https 的版本化下载地址。
- **获准后的发布与核对**：
  1. 推送 `gh-pages`；首次启用 Pages：`gh api -X POST repos/<owner>/agent-collab/pages -f 'source[branch]=gh-pages' -f 'source[path]=/'`。
  2. 用未登录的客户端核对：`<通道根>/stable/latest.json` 与 `product-manifest.json(.sig)` 直接返回 200（不重定向）且字节与本机组装的一致；产品清单的签名用正式 updater 公钥核对通过。
  3. `latest.json` 里每个平台的地址能取到 Release 附件的真实字节，SHA-256 与产品清单一致、更新签名有效。
  4. 草稿 Release 上传成功不算通过；附件公开可下载之前，不把它挂进 stable。
- GitHub 静态下载成功不能代替 controller API/SSE 的公开 TLS 实测。

- [ ] 测试密钥与 loopback 测试地址只用于候选与内部测试身份的联调，不进入正式身份的构建。
- [ ] 候选包的发行说明写明：未签名（或只有临时签名）、未公证、使用测试更新地址，不是正式发行版；Windows 安装时会出现 SmartScreen 提示，macOS 包从别的机器拷入或下载后需要用户手动放行。
- [ ] 普通检查只提供更高的版本；需要回退时走明确流程（桌面端重新安装旧安装包，本机后台 `install --rollback`）。

## 6. 验证范围

- [ ] 全量测试在 Windows 与 macOS 上各跑一次：`python -m unittest discover -s tests`；覆盖受支持的最低版本 Python 3.9。确认 `tests/test_e2e_local.py` 没有被跳过（它需要 loopback socket）。
- [ ] controller 运行在 Linux：在任一 Linux 环境补跑一次全量测试（按需启动的 WSL 即可，不需要常驻节点），并在隔离前缀上走一次签名安装、升级、中断恢复与回退。
- [ ] 桌面外壳：在 `desktop/src-tauri` 下运行 `cargo test` 与 `cargo clippy`。
- [ ] 页面：`tsc --noEmit` 通过并完成构建（`cd ui && npm ci && node build.mjs`）。
- [ ] 发行包：`python3 -B scripts/build_release.py --version <版本>`；核对 manifest 的 `git_commit` 是要发行的提交，`git_dirty` 为 false，并带 `protocol_version`、`features`、`compat`。
- [ ] 安装包在没有开发环境的隔离环境（虚拟机）里完成首次安装、首次启动、覆盖升级、真实更新、卸载产品与卸载后台的验证。
- [ ] 本机服务在真实的服务管理器下运行的验证：Windows 的计划任务（两个组件都用 `pythonw.exe`，启动时不弹控制台窗口）、macOS 的 LaunchAgent。安装后由服务管理器拉起并保持心跳，`service stop`、`service start` 的行为与 [install.md](install.md) 一致。
- [ ] 更新的失败路径：损坏的包、错误签名、缺目标平台、与当前 controller 不兼容、下载中断、宣布版本与清单不符、安装中断后修复，各自如实记录且旧版本可用。
- [ ] 卸载：Windows 交互卸载时选“一并卸载本机后台”，服务、启动项与程序文件（`versions`、`runtime`）都移除，状态目录保留；没有完全清除时如实提示，可以取消这次卸载，再次运行卸载程序接着清除；静默卸载保留后台并在前缀里写 `desktop-uninstalled.txt`。macOS 删除应用包后本机后台照常运行、不引用应用包，之后要移除用前缀里的 `bin/agent-collab install --uninstall --purge-code`。
- [ ] 后台发行包的来源验签用 OpenSSH 8.1 以上的 `ssh-keygen -Y verify`，核对固定的 signer `agent-collab-release`、namespace 与真实字节。安装器依次尝试系统自带的 OpenSSH 客户端（`%SystemRoot%\System32\OpenSSH`）、Microsoft Win32-OpenSSH 安装包的位置（`%ProgramFiles%\OpenSSH`）与 PATH；区分工具缺失、不支持 `-Y verify`、无法运行与签名不符，前三种换下一个工具或拒绝并说明办法，签名不符立即拒绝；都在改动安装、服务与信任之前。macOS 用 `/usr/bin/ssh-keygen`。
- [ ] Windows 10 22H2（最低支持版本）的验签路径：在真实的 Windows 10 22H2 系统环境（虚拟机即可）上用 `C:\Windows\System32\OpenSSH\ssh.exe -V` 看系统自带客户端的版本；没有就按“可选功能 → OpenSSH 客户端”（或管理员 PowerShell `Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0`）安装；低于 8.1 时安装 Microsoft Win32-OpenSSH 的 MSI 客户端（`msiexec /i OpenSSH-Win64-<版本>.msi ADDLOCAL=Client`，装到 `%ProgramFiles%\OpenSSH`，不需要启用 sshd 或开放 22 端口）。然后装候选桌面端，用随包的安装器做一次真实验签与安装前检查，记录它输出的 `Release source verified … (OpenSSH, <所用的 ssh-keygen>)`。`ssh -V` 只作辅助诊断：PATH 上的 ssh 可能与安装器选中的不是同一个。Windows 10 22H2 默认带的 OpenSSH 客户端是 8.1p1，满足要求。
- [ ] 默认系统信任的公开 TLS 正例：在使用公开有效证书的独立候选 controller 上，两个平台的普通客户端不加自定义 CA、不关闭校验，完成配对、API、事件流（SSE 长连接）、本机助手与小工件读写。不用生产入口、静态下载页或测试 CA 代替；免账户的 Cloudflare Quick Tunnel 官方不支持 SSE，不能用。可行的做法：自有域名下的测试子域名、Let's Encrypt DNS-01 证书、候选 controller 自己提供 TLS 并只监听局域网地址，验证后撤销测试客户端、删除测试证书与数据目录。
- [ ] 真实睡眠/唤醒：Windows x64 与 macOS Apple Silicon 各验证一次（记录实际进入的电源状态、节点离线与恢复、任务与操作如何收敛、旧 attempt 迟到的结果如何处理）。用借来的非生产设备，或经批准的现有宿主维护窗口（窗口内暂停这台节点接单，候选 controller 放在不随被测宿主睡眠的另一台机器上）；虚拟机、暂停虚拟机、断网、锁屏、注销与重启都不能代替。

## 7. 隐私与环境信息

- [ ] 部署实测证据（真实域名、实例、主机名、用户目录、会话 ID）只保存在私有资料中，不进入公开仓库。
- [ ] 提交前在仓库根目录复查暂存区（`git add` 之后），检查将要提交的内容而不是工作区：`git grep --cached -nIiE "<你的域名>|<实例 ID 前缀>|<主机名>|<用户名>"` 应无输出。
- [ ] `git ls-files -- "*.token" "*.db" "*.db-wal" "*.db-shm"` 应无输出（`.gitignore` 已忽略这些文件，但忽略规则不影响已被跟踪的文件）。

## 8. 名称与商标

- [ ] 内部目录名为 `agent-collab`；发布前检查同名项目与商标冲突。
