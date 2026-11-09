"""agent-collab 统一入口。

    agent-collab controller ...   # 转交 agent_collab.controller
    agent-collab agent ...        # 转交 agent_collab.node_agent
    agent-collab legacy ...       # 旧任务只读投影
    agent-collab api METHOD PATH [--data JSON|@文件] [--url U] [--token-file F] [--ca-file F]
    agent-collab submit 任务.json  [--url U] [--token-file F] [--ca-file F]
    agent-collab install ...      # 本地 release 安装、升级、回退、卸载
    agent-collab doctor ...       # 安装完整性、服务及 controller/agent 检查

api/submit 使用管理令牌；变更请求缺 request_id 时自动生成。证书始终校验，http:// 仅限 loopback。
"""
import argparse
import importlib
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_URL = os.environ.get('AGENT_COLLAB_URL', 'http://127.0.0.1:18669')
LOOPBACK = ('127.0.0.1', 'localhost', '::1')


def request(url, token, method, path, data=None, ca_file=None):
    u = urllib.parse.urlsplit(url)
    if u.scheme == 'http' and u.hostname not in LOOPBACK:
        raise SystemExit('非 loopback 地址必须使用 https://')
    body = None if data is None else json.dumps(data, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url.rstrip('/') + '/api/v1' + path, data=body, method=method,
                                 headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json',
                                          **({'Content-Type': 'application/json'} if body else {})})
    ctx = ssl.create_default_context(cafile=ca_file) if u.scheme == 'https' else None
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {'raw': raw.decode('utf-8', 'replace')[:2000]}


def _client(argv, submit):
    ap = argparse.ArgumentParser(prog='agent-collab ' + ('submit' if submit else 'api'))
    if submit:
        ap.add_argument('job_file')
    else:
        ap.add_argument('method', type=str.upper)
        ap.add_argument('path')
        ap.add_argument('--data')
    ap.add_argument('--url', default=DEFAULT_URL)
    ap.add_argument('--token-file', default=os.environ.get('AGENT_COLLAB_TOKEN_FILE'))
    ap.add_argument('--ca-file')
    a = ap.parse_args(argv)
    if not a.token_file:
        raise SystemExit('需要 --token-file（controller 数据目录中的 admin.token）或 AGENT_COLLAB_TOKEN_FILE')
    token = open(a.token_file, encoding='utf-8').read().strip()
    if submit:
        method, path, data = 'POST', '/jobs', json.load(open(a.job_file, encoding='utf-8-sig'))
    else:
        method, path = a.method, a.path
        data = None
        if a.data:
            data = json.load(open(a.data[1:], encoding='utf-8-sig')) if a.data.startswith('@') else json.loads(a.data)
    if method != 'GET' and isinstance(data, dict):
        data.setdefault('request_id', str(uuid.uuid4()))
    status, out = request(a.url, token, method, path, data, a.ca_file)
    print(json.dumps({'http_status': status, 'body': out}, ensure_ascii=False, indent=2))
    return 0 if status < 400 else 2


MODULES = {'controller': 'agent_collab.controller', 'agent': 'agent_collab.node_agent', 'legacy': 'agent_collab.legacy'}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd in MODULES:
        return importlib.import_module(MODULES[cmd]).main(rest) or 0
    if cmd in ('api', 'submit'):
        return _client(rest, cmd == 'submit')
    if cmd in ('install', 'doctor'):
        from . import installer
        return installer.main((['doctor'] if cmd == 'doctor' else []) + rest)
    raise SystemExit('未知子命令：' + cmd)


if __name__ == '__main__':
    sys.exit(main())
