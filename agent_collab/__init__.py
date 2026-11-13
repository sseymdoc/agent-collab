import sys

__version__ = '0.1.0.dev0'

# 输出里有中文说明与 JSON 结果：重定向到按区域编码（例如 Windows 的 cp1252）打开的流时，改为 UTF-8 输出，
# 不因说明文字中断操作，JSON 也保持有效。Windows 控制台本身已经是 Unicode，不受影响。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure') and (getattr(_stream, 'encoding', None) or '').lower().replace('-', '') != 'utf8':
        _stream.reconfigure(encoding='utf-8')
del _stream
