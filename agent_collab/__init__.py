import sys

__version__ = '0.1.0.dev0'

# 输出里有中文说明；控制台或重定向用的编码（例如 Windows 的 cp1252）写不了时转义输出，不让说明文字中断安装或维护操作。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(errors='backslashreplace')
