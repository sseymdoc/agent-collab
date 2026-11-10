# ADR 0001：技术栈、拓扑与开发隔离

日期：2026-11-09　状态：已采纳

## 背景

- 执行环境：Windows 原生（Python 3.15）、macOS（系统 Python 3.9.6，不假定有 uv/node/brew）；同机 Ubuntu WSL2（Python 3.14）是可选的 Linux 环境。
- 控制节点：一台低配云主机（Debian、x86_64、约 2 vCPU / 2GB 内存、systemd，前置 Caddy 终止 TLS）。
- 已有一个只用标准库的 Claude–Codex 协作执行器（按任务目录 `state.json` 保存运行事实）；本项目开发期间它继续作为稳定执行器，本项目移植了其中的进程托管代码。

## 决定

1. **Python 标准库，兼容 3.9**：controller、node agent、适配器均不引入运行时依赖。理由：部分节点只有系统 Python 3.9；控制主机内存紧张；离线/内网安装无需 PyPI；与已有执行器同栈，可直接移植其进程托管代码。HTTP 用 `http.server.ThreadingHTTPServer`，业务在无 socket 的 `App.handle` 中。未选 FastAPI：单控制服务、低 QPS 下收益不抵依赖与安装成本；若将来需要再替换包装层，不影响协议。
2. **单控制服务 + 本地 SQLite（WAL）**：数据库只由 controller 进程在本机文件系统打开，经 API 访问；禁止 SMB/NFS 共享打开。备份用 `sqlite3.Connection.backup` 并附 blob 清单。
3. **内容寻址 blob 目录**：`blobs/ab/cd/<sha256>`，写入先落临时文件、校验哈希后原子改名；manifest 自身也是 blob。
4. **UI**：React + TypeScript + Vite，构建产物随包分发到 `agent_collab/static/`，最终用户无需 Node。
5. **拓扑**：生产 controller 监听 `127.0.0.1:<本地端口>`，由反向代理在 `https://<DOMAIN>:<HTTPS_PORT>` 终止 TLS；节点主动出站连接。默认每台物理主机一个原生 worker（Windows 计划任务、macOS LaunchAgent），Linux/WSL worker 可选、按需启用。任务生命周期只走节点的出站 HTTPS；SSH 是可选的管理入口（安装、启动、诊断、恢复），只调用固定的 `service start|stop|status` 命令，不是第二套节点控制协议。开发/备用使用 loopback all-in-one；非 loopback 监听必须显式配置 TLS 证书，不自动绑定 0.0.0.0。
6. **不做**：Kubernetes、Redis、消息总线、自建共识/双主选举。Temporal/Restate 仅作为未来 durable backend 评估方向。

## 开发隔离

- 稳定执行器不被本项目修改或热替换；候选版本不管理自己的开发任务。
- 开发在共享目录中进行：Windows 侧可能经 SMB 访问。Git 因所有权检查拒绝该 checkout 时，用 `git -c safe.directory=<该 checkout 的绝对路径>` 只信任这一个目录，不改全局配置，也不用通配 `*`；worker 在独立 clone（分支 `wp/<包名>`）中实施，且该 clone 位于执行端本地盘。不用 `git worktree`，因为其元数据含单一 OS 的绝对路径。
- 候选服务：开发端口 18669（loopback）；测试任务使用测试 profile。
- SQLite 只在本机文件系统打开。测试数据（`.dev/tmp`）和快速试用数据（`./.dev/ctl`）默认都在仓库内；仓库经 SMB 访问时，须先复制到本机磁盘，再运行 controller 和测试，不经网络盘打开数据库。UI 构建不受影响：`node build.mjs` 会自行在本机临时目录构建。

## 后果

- 需要自行实现少量 HTTP 路由、SSE 与简单二进制上传（PUT 原始字节），代码量可控。
- Windows SCM 服务需要 ctypes 或外部包装，安装阶段单独决定；不把计划任务冒充为服务。
