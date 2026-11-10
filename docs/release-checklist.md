# 发布检查清单

## 许可证

- 自有代码以 MIT 许可证发布（见 [LICENSE](../LICENSE)）。
- 第三方：见 [THIRD_PARTY.md](../THIRD_PARTY.md)。Vite/TypeScript 仅构建期使用；发行包内的 `agent_collab/static` 含打包后的 React（MIT），构建时其许可证文本写入 `agent_collab/static/third-party-licenses.md`，仓库根目录的 `LICENSE` 与 `THIRD_PARTY.md` 也随发行包放在 `agent_collab/` 下。
- 不随包分发、不代理、不转售 Codex CLI、Claude Code 或任何模型服务及其认证；节点使用用户本机官方工具与官方登录。

## 隐私与环境信息

- 部署实测证据（真实域名、实例、主机名、用户目录、会话 ID）只保存在私有资料中，不进入公开仓库。
- 提交前复查：`git grep -nIiE "<你的域名>|<实例 ID 前缀>|<主机名>|<用户名>"`；确认无 `*.token`、`*.db`、`admin.token`、`node.token`、`lease.token` 被跟踪（`.gitignore` 已排除）。

## 发布产物

- `python3 -B scripts/build_release.py --version <版本>`：manifest 记录 git_commit/git_dirty、逐文件 sha256；当前**没有签名**，正式发行前需增加可信签名或来源证明（如 Sigstore / minisign）并在安装器中校验。
- 发行前在 Windows、Linux（含 WSL）、macOS 系统 Python 3.9 各跑一次 `python -m unittest discover -s tests`（含 `tests/test_e2e_local.py`）；CI 覆盖 Ubuntu（3.9、3.13）、Windows 与 macOS。
