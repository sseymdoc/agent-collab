# 第三方与来源清单

| 组件 | 来源 | 用途 | 许可证 | 状态 |
| --- | --- | --- | --- | --- |
| codex-collaborate launcher/workflow | 本用户自有，`~/.claude/skills/codex-collaborate/scripts/{launcher,workflow}.py`（sha256 前缀 `7862865F` / `A2D23644`，2026-11-09 版） | 进程托管、Windows Job Object、codex exec 参数与报告 schema 的移植来源 | 自有，待定 | 移植处在代码头注明来源 |
| Python 标准库 | CPython | 运行时 | PSF | — |
| React、React DOM | npm | 控制台运行时，打包进 `agent_collab/static/` 随发行包分发 | MIT | 版本以 `ui/package-lock.json` 为准；构建时把打包依赖的许可证文本写入 `agent_collab/static/third-party-licenses.md` |
| Vite、@vitejs/plugin-react、TypeScript | npm | 仅构建期，不随包分发 | MIT / Apache-2.0 | 版本以 `ui/package-lock.json` 为准 |
| Codex CLI、Claude Code | OpenAI / Anthropic 官方安装 | 由节点调用，不随包分发 | 各自条款 | 不转售、不再分发、不代理其认证 |
