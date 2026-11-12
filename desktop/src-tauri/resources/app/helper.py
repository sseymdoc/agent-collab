"""随桌面端分发的本机助手入口。

桌面外壳用随包的 Python 运行时启动它：`<python> -B -s -E -X utf8 helper.py <子命令...>`，子命令只有
`project local <动作>`、`install`、`service`、`controller client-code`（见 docs/desktop.md“权限隔离”）。
`agent_collab` 包在构建时放到本文件旁边；这里只负责让解释器找到它，其余都在 agent_collab.cli 里。
"""
import os
import sys

sys.dont_write_bytecode = True  # 安装目录可能是只读的，也不该被运行时改动
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_collab.cli import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main())
