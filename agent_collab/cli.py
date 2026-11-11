"""agent-collab 统一入口。

    agent-collab controller ...   # 转交 agent_collab.controller
    agent-collab agent ...        # 转交 agent_collab.node_agent
    agent-collab legacy ...       # 旧任务只读投影
    agent-collab install ...      # 本地 release 安装、升级、回退、卸载
    agent-collab doctor ...       # 安装完整性、服务及 controller/agent 检查
    agent-collab service ...      # 受管服务 start、stop、status
    agent-collab migrate ...      # 迁移预检（只读盘点）与隔离副本上的升级预演

客户端子命令与桌面端共用同一套 API 和授权（docs/protocol.md §12），输出都是 JSON：

    agent-collab client pair --url U --code C [--name N]    # 用配对码换取本机的客户端凭据
    agent-collab client status | forget [--revoke]
    agent-collab submit 任务.json
    agent-collab submit --template T --goal 目标 [--input-dir 目录 | --baseline ID] [--check 名称=命令] [--wait]
    agent-collab job list | show ID | wait ID | follow ID | results ID | artifacts ID [--download 目录]
    agent-collab job publish ID | cancel ID | retry ID --reason 原因
    agent-collab baseline add 目录 --name 名称 | list
    agent-collab settings get | set [--codex-model M --codex-effort E] [--allow M=e1,e2 | --allow-any] ...
    agent-collab api METHOD PATH [--data JSON|@文件]

凭据：--profile（缺省 default；client pair 写入的客户端凭据），或 --token-file（controller 本机的管理令牌）。
变更请求缺 request_id 时自动生成。证书始终校验，http:// 仅限 loopback。
退出码：0 成功；1 本地错误；2 请求被拒绝或参数格式错误；3 连接、凭据（HTTP 401）或 controller 身份问题；4 等待超时；5 任务以失败、取消或需人工处理结束。
"""
import argparse
import http.client
import importlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_URL = os.environ.get('AGENT_COLLAB_URL', 'http://127.0.0.1:18669')
LOOPBACK = ('127.0.0.1', 'localhost', '::1')
RESTING = ('completed', 'failed', 'cancelled', 'waiting_reconcile')  # 不再自行变化、等人或等下一步的状态
ENDED = ('completed', 'failed', 'cancelled')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向：urllib 会把 Authorization 头带到新地址，凭据可能被转发到别的主机。"""
    def redirect_request(self, *args, **kwargs):
        return None  # 3xx 原样作为 HTTPError 返回，由调用方当作失败


def out(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def fail(code, message, exit_code=3):
    print(json.dumps({'error': {'code': code, 'message': message}}, ensure_ascii=False), file=sys.stderr)
    raise SystemExit(exit_code)


class Unreachable(Exception):
    """controller 暂时不可达（连接失败，或前置代理代答的 5xx）。等待类命令据此重试，其余命令在 main 里以退出码 3 结束。"""


def rejected(status):
    return 3 if status == 401 else 2


def _open(url, token, method, path, body=None, ca_file=None, controller_id=None, content_type='application/json', timeout=30, extra=None):
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ('http', 'https') or (u.scheme == 'http' and u.hostname not in LOOPBACK):
        raise SystemExit('非 loopback 地址必须使用 https://')
    headers = dict(extra or {}, Accept='application/json')
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if body is not None:
        headers['Content-Type'] = content_type
    req = urllib.request.Request(url.rstrip('/') + '/api/v1' + path, data=body, method=method, headers=headers)
    handlers = [NoRedirect()] + ([urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_file))]
                                 if u.scheme == 'https' else [])
    try:
        response = urllib.request.build_opener(*handlers).open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        response = e
    except http.client.InvalidURL:
        fail('invalid', '请求路径含有空格或控制字符：' + path, 2)
    except (urllib.error.URLError, OSError) as exc:
        if any(isinstance(e, ssl.SSLCertVerificationError) for e in (exc, getattr(exc, 'reason', None))):
            fail('network', 'TLS 校验失败')  # 证书不对是身份问题，重试不会变好
        raise Unreachable() from exc
    seen = response.headers.get('X-Controller-Id')
    if seen is None and response.getcode() >= 500:
        # 不带 controller 身份的 5xx 来自前置代理（例如 controller 重启期间）：按网络问题处理，不当作身份不符。
        response.close()
        raise Unreachable()
    # 配对得到的凭据绑定了 controller 身份：应答必须来自同一个 controller（protocol §12.1）。
    if controller_id and seen != controller_id:
        response.close()
        fail('controller_mismatch', '应答不是来自配对时的 controller；凭据没有再发送，检查地址或重新配对')
    return response


def request(url, token, method, path, data=None, ca_file=None, controller_id=None, headers=None):
    body = None if data is None else json.dumps(data, ensure_ascii=False).encode('utf-8')
    with _open(url, token, method, path, body, ca_file, controller_id, extra=headers) as r:
        raw = r.read()
        try:
            return r.getcode(), json.loads(raw) if raw else None
        except ValueError:
            return r.getcode(), {'raw': raw.decode('utf-8', 'replace')[:2000]}


def client_home():
    if os.environ.get('AGENT_COLLAB_HOME'):
        return Path(os.environ['AGENT_COLLAB_HOME'])
    if os.name == 'nt':
        return Path(os.environ.get('APPDATA') or Path.home() / 'AppData' / 'Roaming') / 'agent-collab'
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'agent-collab'


def profile_path(name):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', name):
        raise SystemExit('profile 名只允许字母、数字、点、下划线和连字符')
    return client_home() / 'clients' / (name + '.json')


class Conn:
    """一条已认证的 controller 连接：客户端凭据（绑定 controller 身份）或本机管理令牌。"""
    def __init__(self, a, profile_only=False):
        token_file = None if profile_only else a.token_file or os.environ.get('AGENT_COLLAB_TOKEN_FILE')
        if token_file:
            self.url, self.ca_file, self.controller_id = a.url or DEFAULT_URL, a.ca_file, None
            self.token = Path(token_file).read_text(encoding='utf-8').strip()
            return
        path = profile_path(a.profile)
        if not path.is_file():
            raise SystemExit('没有客户端凭据（%s）。先运行 agent-collab client pair --url <地址> --code <配对码>，'
                             '或在 controller 本机用 --token-file 指定管理令牌' % path)
        saved = json.loads(path.read_text(encoding='utf-8'))
        # 凭据只发往配对时的地址：X-Controller-Id 要等应答回来才能核对，那时凭据已经送出。地址变了就重新配对。
        if a.url and a.url.rstrip('/') != saved['url']:
            raise SystemExit('profile %s 配对的地址是 %s；凭据不会发往 --url 指定的其他地址，地址变了请重新配对' % (a.profile, saved['url']))
        self.url, self.token, self.controller_id = saved['url'], saved['token'], saved['controller_id']
        self.ca_file = a.ca_file or saved.get('ca_file')

    def send(self, method, path, data=None):
        if method != 'GET' and isinstance(data, dict):
            data.setdefault('request_id', str(uuid.uuid4()))
        return request(self.url, self.token, method, path, data, self.ca_file, self.controller_id)

    def call(self, method, path, data=None):
        """成功返回应答体；被拒绝时打印应答并退出。"""
        status, body = self.send(method, path, data)
        if not 200 <= status < 300:
            out({'http_status': status, 'body': body})
            raise SystemExit(rejected(status))
        return body

    def raw(self, method, path, body=None, timeout=120):
        with _open(self.url, self.token, method, path, body, self.ca_file, self.controller_id,
                   'application/octet-stream', timeout) as r:
            return r.getcode(), r.read()

    def blob(self, sha):
        status, data = self.raw('GET', '/blobs/' + sha)
        if status != 200:
            fail('blob_unavailable', 'blob %s 读取失败（HTTP %d）' % (sha, status), 2)
        return data


def connection_args(parser):
    parser.add_argument('--profile', default=os.environ.get('AGENT_COLLAB_PROFILE', 'default'))
    parser.add_argument('--url')
    parser.add_argument('--token-file')
    parser.add_argument('--ca-file')


def _client(argv, submit):
    """api 与“提交任务文件”：输出 {"http_status", "body"}，只有 2xx 算成功。"""
    ap = argparse.ArgumentParser(prog='agent-collab ' + ('submit' if submit else 'api'))
    if submit:
        ap.add_argument('job_file')
    else:
        ap.add_argument('method', type=str.upper)
        ap.add_argument('path')
        ap.add_argument('--data')
        ap.add_argument('--coordinator-token', help='持有该根工作包协调租约时的令牌（X-Coordinator-Token）')
    connection_args(ap)
    a = ap.parse_args(argv)
    conn = Conn(a)
    if submit:
        method, path, data = 'POST', '/jobs', json.loads(Path(a.job_file).read_text(encoding='utf-8-sig'))
    else:
        method, path = a.method, a.path
        data = None
        if a.data:
            data = json.loads(Path(a.data[1:]).read_text(encoding='utf-8-sig')) if a.data.startswith('@') else json.loads(a.data)
    if method != 'GET' and isinstance(data, dict):
        data.setdefault('request_id', str(uuid.uuid4()))
    extra = {'headers': {'X-Coordinator-Token': a.coordinator_token}} if not submit and a.coordinator_token else {}
    status, body = request(conn.url, conn.token, method, path, data, conn.ca_file, conn.controller_id, **extra)
    out({'http_status': status, 'body': body})
    return 0 if 200 <= status < 300 else rejected(status)  # 3xx 不算送达


def _client_command(argv):
    ap = argparse.ArgumentParser(prog='agent-collab client')
    sub = ap.add_subparsers(dest='action', required=True)
    pairing = sub.add_parser('pair', help='用 controller 签发的一次性配对码换取本机的客户端凭据')
    pairing.add_argument('--url', required=True)
    pairing.add_argument('--code', required=True)
    pairing.add_argument('--name', default='cli@' + platform.node())
    pairing.add_argument('--kind', choices=('cli', 'agent'), default='cli')
    pairing.add_argument('--profile', default=os.environ.get('AGENT_COLLAB_PROFILE', 'default'))
    pairing.add_argument('--ca-file')
    for name in ('status', 'forget'):
        connection_args(sub.add_parser(name))
    sub.choices['forget'].add_argument('--revoke', action='store_true', help='同时在 controller 上撤销这份凭据')
    a = ap.parse_args(argv)
    path = profile_path(a.profile)
    if a.action == 'pair':
        if path.exists():
            raise SystemExit('profile %s 已有凭据；先 agent-collab client forget，或换一个 --profile' % a.profile)
        from .artifacts import SecretFile
        # 自动化客户端的凭据放在当前用户的配置目录里，文件仅本人可访问（POSIX 用 0600，Windows 的 DACL 只留当前用户）；
        # 它可单独撤销、权限按配对码授予。凭据值不打印。先收紧再配对：配对码只能用一次。
        try:
            secret = SecretFile(path)
        except OSError:
            raise SystemExit('无法把凭据文件限制为仅当前用户可访问；配对码没有使用，凭据没有保存')
        with secret:
            status, body = request(a.url, None, 'POST', '/clients/pair', {'code': a.code, 'name': a.name, 'kind': a.kind}, a.ca_file)
            if status != 200:
                out({'http_status': status, 'body': body})
                return 2
            saved = {k: body[k] for k in ('client_id', 'name', 'kind', 'scopes', 'controller_id')}
            saved.update(url=a.url.rstrip('/'), ca_file=str(Path(a.ca_file).absolute()) if a.ca_file else None, token=body['client_token'])
            secret.save(json.dumps(saved, ensure_ascii=False, indent=2).encode('utf-8'))
        out(dict({k: saved[k] for k in ('client_id', 'name', 'kind', 'scopes', 'controller_id', 'url')},
                 profile=a.profile, credential_file=str(path)))
        return 0
    if a.action == 'status':
        out(Conn(a).call('GET', '/client'))
        return 0
    revoked = None
    if a.revoke:
        # 撤销的是这份客户端凭据自己：只用 profile 里的凭据，不用 --token-file 或环境里的管理令牌。
        revoked = Conn(a, profile_only=True).send('DELETE', '/client')[0] == 200
        if not revoked:
            out({'profile': a.profile, 'forgotten': False, 'revoked': False})
            return 2  # 撤销没成功就保留凭据文件，以便重试
    if path.exists():
        path.unlink()
    out({'profile': a.profile, 'forgotten': True, 'revoked': revoked})
    return 0


def upload_directory(conn, directory):
    """把目录快照成 manifest，并上传 controller 还没有的文件内容。"""
    from .artifacts import snapshot
    manifest, blobs = snapshot(directory)
    for entry in manifest['files']:
        if conn.raw('HEAD', '/blobs/' + entry['sha256'])[0] != 200:
            status, _ = conn.raw('PUT', '/blobs/' + entry['sha256'], blobs[entry['sha256']])
            if status not in (200, 201):
                fail('upload_failed', '上传 %s 失败（HTTP %d）' % (entry['path'], status), 2)
    return manifest


def parse_check(text):
    """名称=命令：命令按 shell 规则分词，或直接给 JSON 数组；{python} 由执行节点替换成它的解释器。"""
    name, separator, command = text.partition('=')
    if not separator or not command.strip():
        raise SystemExit('--check 的格式是 名称=命令')
    return {'name': name.strip(), 'run': json.loads(command) if command.lstrip().startswith('[') else shlex.split(command)}


def summarize(conn, job):
    """任务的检查、验收、协调与交付状态；模型自述不在其中，只取 controller 记录的事实。"""
    attempts = job.get('attempts', [])
    current = next((x for x in attempts if x['attempt_id'] == job['current_attempt_id']), None) or {}
    checks = ((current.get('report') or {}).get('checks') or {}).get('results')
    finals = [c['checkpoint_id'] for c in job.get('checkpoints', [])
              if c['kind'] == 'final' and c['state'] != 'uploading' and c['attempt_id'] == job['current_attempt_id']]
    rounds = []
    for event in conn.call('GET', '/jobs/%s/events?limit=1000' % job['job_id'])['events']:
        if event['type'] == 'coordinator.triggered':
            c = conn.call('GET', '/jobs/' + event['payload']['coordinator_job_id'])
            report = next((x.get('report') for x in reversed(c.get('attempts', [])) if x.get('report')), None) or {}
            rounds.append({'job_id': c['job_id'], 'status': c['status'], 'decision': report.get('decision'), 'notes': report.get('notes'),
                           'node_id': next((x['node_id'] for x in reversed(c.get('attempts', []))), None)})
    acceptance = job['acceptance']['status']
    if job['status'] not in RESTING:
        outcome = 'running'
    elif any(r['status'] not in ENDED for r in rounds):
        # 失败或待对账也可能触发协调回合；回合结束前它可能新建修复修订，不能先判为需人工处理。
        outcome = 'awaiting_coordination'
    elif job['status'] != 'completed':
        outcome = 'needs_attention'
    elif acceptance in ('rework', 'blocked'):
        outcome = 'needs_attention'
    elif job['contract']['publish']['requires_acceptance'] and acceptance != 'passed':
        outcome = 'awaiting_acceptance'
    elif not job['delivery_version']:
        # 执行结束不等于成果已发布：手动发布等调用方，自动发布等执行节点完成发布。
        outcome = 'ready_to_publish' if job['contract']['publish']['mode'] == 'manual' else 'awaiting_publication'
    else:
        outcome = 'done'
    return {'job_id': job['job_id'], 'work_package_id': job['work_package_id'], 'title': job['title'],
            'status': job['status'], 'status_reason': job['status_reason'], 'outcome': outcome,
            'node_id': current.get('node_id'), 'model_policy': job.get('model_policy'),
            'observed': (current.get('runtime') or {}).get('observed'),
            'checks': None if checks is None else {
                'passed': all(c.get('exit_code') == 0 and not c.get('timed_out') for c in checks),
                'results': [{k: c.get(k) for k in ('name', 'exit_code', 'timed_out', 'duration_s')} for c in checks]},
            'acceptance': job['acceptance'],
            'assessments': [{k: x.get(k) for k in ('status', 'method', 'result', 'reviewer', 'checkpoint_id')} for x in job.get('assessments', [])],
            'coordination': rounds, 'final_checkpoint_id': finals[-1] if finals else None,
            'delivery_version': job['delivery_version'], 'delivery_checkpoint_id': job['delivery_checkpoint_id'],
            'integration_status': job['integration_status'], 'cancel': job['cancel'], 'counters': job['counters']}


def settled_state(conn, job_id):
    """跟到同一根工作包的最新修订（受管修复会新建修订），返回它的摘要与是否已停稳。"""
    summary = summarize(conn, conn.call('GET', '/jobs/' + job_id))
    # 先取摘要、后查最新修订：协调回合结束与修复任务的创建在同一事务里，这个顺序不会漏掉刚创建的修复。
    newest = conn.call('GET', '/jobs?limit=1&work_package_id=' + summary['work_package_id'])['jobs'][0]['job_id']
    if newest != job_id:
        return settled_state(conn, newest)
    summary['settled'] = summary['outcome'] not in ('running', 'awaiting_coordination', 'awaiting_publication')
    return summary


def finish(summary, line=False):
    # outcome 说明下一步：done 已发布；ready_to_publish、awaiting_acceptance 轮到调用方；needs_attention 需人工处理。
    print(json.dumps({'summary': summary}, ensure_ascii=False), flush=True) if line else out(summary)
    return 0 if summary['outcome'] in ('done', 'ready_to_publish', 'awaiting_acceptance') else 5


def wait(conn, job_id, timeout, interval=2.0):
    deadline, summary = time.monotonic() + timeout, None
    while True:
        try:
            summary = settled_state(conn, job_id)
        except Unreachable:
            # controller 重启或代理暂时 5xx：到期前继续等；到期时还从未取到摘要则按不可达结束。
            if summary is None and time.monotonic() >= deadline:
                raise
        else:
            if summary['settled']:
                return finish(summary)
        if time.monotonic() >= deadline:
            out(dict(summary, timed_out=True))
            return 4
        time.sleep(interval)


SNAPSHOT = object()  # stream 产出它表示游标已过期：调用方先刷新权威状态，再处理随后补读的事件


def stream(conn, cursor):
    """事件流：产出事件（或 None 表示心跳/重连）。断线从上次序号续读。

    游标早于 controller 保留的最早事件时（中间的事件已被清理），先产出 SNAPSHOT 让调用方刷新状态，
    再补读仍保留的事件，最后从补读到的位置续接。
    """
    while True:
        try:
            response = _open(conn.url, conn.token, 'GET', '/events/stream?cursor=%d' % cursor, None, conn.ca_file, conn.controller_id, timeout=60)
        except Unreachable:  # controller 重启或代理暂时 5xx：稍后从同一游标重连
            time.sleep(2)
            yield None
            continue
        if response.getcode() != 200:
            response.close()
            if response.getcode() in (401, 403):
                fail('unauthorized', '凭据已失效或没有读取权限')
            time.sleep(2)
            yield None
            continue
        kind = data = None
        try:
            for raw in response:
                line = raw.decode('utf-8', 'replace').rstrip('\r\n')
                if line.startswith('id: '):
                    cursor = int(line[4:])
                elif line.startswith('event: '):
                    kind = line[7:]
                elif line.startswith('data: '):
                    data = line[6:]
                elif not line:
                    if kind == 'unauthorized':
                        fail('unauthorized', '凭据已被撤销')
                    if kind == 'snapshot_required':
                        yield SNAPSHOT
                        # min_seq 是仍保留的第一条，而流按 seq > cursor 续读：先补读从它开始的一页，再从补读到的位置续接。
                        minimum = json.loads(data)['min_seq']
                        try:
                            backlog = conn.call('GET', '/events?after=%d&limit=1000' % (minimum - 1))['events']
                        except Unreachable:
                            break  # 游标不变：重连后会再次收到 snapshot_required
                        for event in backlog:
                            yield event
                        cursor = backlog[-1]['seq'] if backlog else minimum - 1
                        break
                    yield json.loads(data) if kind and data else None
                    kind = data = None
        except OSError:  # 含读超时：按断线处理，重新连接
            pass
        finally:
            response.close()
        yield None


def retrying(deadline, call):
    """controller 暂时不可达时每 2 秒重试，直到 deadline；到期仍不可达就抛出 Unreachable。"""
    while True:
        try:
            return call()
        except Unreachable:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


def follow(conn, job_id, timeout):
    """每行一个 JSON：该工作包相关的事件，停稳后最后一行是 {"summary": ...}。"""
    deadline = time.monotonic() + timeout
    job = retrying(deadline, lambda: conn.call('GET', '/jobs/' + job_id))
    root, tracked, cursor = job['work_package_id'], {job_id}, 0
    for event in retrying(deadline, lambda: conn.call('GET', '/jobs/%s/events?limit=1000' % job_id))['events']:
        print(json.dumps(event, ensure_ascii=False), flush=True)
        cursor = event['seq']
    # 先取一次摘要：已停稳就直接结束；到期时 controller 恰好不可达，也有最近一次摘要可以输出。
    last, checked = retrying(deadline, lambda: settled_state(conn, job_id)), time.monotonic()
    if last['settled']:
        return finish(last, line=True)
    for event in stream(conn, cursor):
        relevant = False
        if event is SNAPSHOT:
            # 游标过期期间的事件（可能含修复修订的 job.created）已被清理：先按权威状态补齐要跟的任务，再读补读的事件。
            try:
                tracked.update(j['job_id'] for j in retrying(deadline, lambda: conn.call('GET', '/jobs?limit=1000&work_package_id=' + root))['jobs'])
            except Unreachable:
                pass  # 已到期：下面的到期分支输出最近摘要
            relevant = True
        elif event:
            payload = event.get('payload') or {}
            if event['type'] == 'job.created' and root in (payload.get('work_package_id'),
                                                           ((payload.get('contract') or {}).get('coordinates') or {}).get('work_package_id')):
                tracked.add(event['job_id'])
            relevant = event.get('job_id') in tracked
            if relevant:
                print(json.dumps(event, ensure_ascii=False), flush=True)
        # 相关事件到达时立即核对；此外每 10 秒核对一次，事件丢失或游标跳过时也能结束。
        if relevant or time.monotonic() - checked > 10:
            checked = time.monotonic()
            try:
                last = settled_state(conn, job_id)
            except Unreachable:
                pass  # 暂时不可达：事件流在重连，到期前继续等
            else:
                tracked.update(r['job_id'] for r in last['coordination'])
                if last['settled']:
                    return finish(last, line=True)
        if time.monotonic() >= deadline:
            try:
                last = settled_state(conn, job_id)
            except Unreachable:
                pass  # 到期时仍不可达：按超时结束，输出最近一次摘要
            print(json.dumps({'summary': dict(last, timed_out=True)}, ensure_ascii=False), flush=True)
            return 4


def _submit(argv):
    if argv and not argv[0].startswith('-') and '--template' not in argv:
        return _client(argv, True)  # 提交现成的任务文件
    ap = argparse.ArgumentParser(prog='agent-collab submit')
    ap.add_argument('--template', required=True, help='模板 ID，见 agent-collab api GET /templates')
    ap.add_argument('--goal', required=True)
    ap.add_argument('--title')
    ap.add_argument('--acceptance', action='append', default=[])
    ap.add_argument('--exclude', action='append', default=[], help='明确不包含的范围')
    inputs = ap.add_mutually_exclusive_group()
    inputs.add_argument('--input-dir', help='以该目录的当前内容作为输入')
    inputs.add_argument('--baseline', help='已登记的输入基线 ID')
    ap.add_argument('--check', action='append', default=[], metavar='名称=命令')
    ap.add_argument('--model')
    ap.add_argument('--effort')
    ap.add_argument('--os', action='append', default=[], choices=('windows', 'linux', 'macos'))
    ap.add_argument('--dry-run', action='store_true', help='只输出将提交的契约与解析后的模型，不创建任务')
    ap.add_argument('--request-id', help='幂等键（UUID）；网络失败后用同一个值重试不会重复创建任务')
    ap.add_argument('--wait', action='store_true')
    ap.add_argument('--timeout', type=float, default=3600)
    connection_args(ap)
    a = ap.parse_args(argv)
    conn = Conn(a)
    draft = {'template': a.template, 'goal': a.goal, 'acceptance': a.acceptance, 'excludes': a.exclude,
             'checks': [parse_check(c) for c in a.check]}
    for key in ('title', 'model', 'effort'):
        if getattr(a, key):
            draft[key] = getattr(a, key)
    if a.os:
        draft['requirements'] = {'os': a.os}
    if a.input_dir:
        draft['inputs_manifest'] = upload_directory(conn, a.input_dir)
    elif a.baseline:
        draft['baseline_id'] = a.baseline
    drafted = conn.call('POST', '/contracts/draft', draft)
    if a.dry_run:
        out(drafted)
        return 0
    # 带上草拟时的设置版本：期间默认模型被改动则被拒绝，不会按没看过的解析结果受理。
    request = {'title': drafted['title'], 'contract': drafted['contract'], 'expected_settings_revision': drafted['settings_revision']}
    if a.request_id:
        request['request_id'] = a.request_id
    status, body = conn.send('POST', '/jobs', request)
    if not 200 <= status < 300 or not a.wait:
        out({'http_status': status, 'body': body, 'warnings': drafted['warnings']})
        return 0 if 200 <= status < 300 else rejected(status)
    return wait(conn, body['job_id'], a.timeout)


def _job(argv):
    ap = argparse.ArgumentParser(prog='agent-collab job')
    sub = ap.add_subparsers(dest='action', required=True)
    listing = sub.add_parser('list')
    listing.add_argument('--status')
    listing.add_argument('--limit', type=int, default=50)
    connection_args(listing)
    for name in ('show', 'wait', 'follow', 'results', 'artifacts', 'publish', 'cancel', 'retry'):
        command = sub.add_parser(name)
        command.add_argument('job_id')
        connection_args(command)
        if name in ('wait', 'follow'):
            command.add_argument('--timeout', type=float, default=3600)
        elif name == 'artifacts':
            command.add_argument('--download', help='把文件写入该目录（必须为空或不存在）')
        elif name == 'retry':
            command.add_argument('--reason', required=True)
            command.add_argument('--exclude-node', action='append', default=[])
    a = ap.parse_args(argv)
    conn = Conn(a)
    if a.action == 'list':
        jobs = conn.call('GET', '/jobs?limit=%d%s' % (a.limit, '&status=' + a.status if a.status else ''))['jobs']
        out({'jobs': [{k: j.get(k) for k in ('job_id', 'work_package_id', 'title', 'status', 'status_reason', 'acceptance',
                                              'delivery_version', 'created_by', 'created_at', 'updated_at')} for j in jobs]})
        return 0
    if a.action == 'wait':
        return wait(conn, a.job_id, a.timeout)
    if a.action == 'follow':
        return follow(conn, a.job_id, a.timeout)
    job = conn.call('GET', '/jobs/' + a.job_id)
    if a.action == 'show':
        out(job)
    elif a.action == 'results':
        out(summarize(conn, job))
    elif a.action == 'cancel':
        out(conn.call('POST', '/jobs/%s/cancel' % a.job_id, {'expected_version': job['version']}))
    elif a.action == 'retry':
        out(conn.call('POST', '/jobs/%s/retry' % a.job_id, {'expected_version': job['version'], 'reason': a.reason,
                                                             'exclude_node_ids': a.exclude_node}))
    else:
        summary = summarize(conn, job)
        checkpoint = job['delivery_checkpoint_id'] or summary['final_checkpoint_id']
        if not checkpoint:
            fail('no_result', '该任务还没有可读取的最终检查点', 2)
        if a.action == 'publish':
            attempt = next(x for x in job['attempts'] if x['attempt_id'] == job['current_attempt_id'])
            out(conn.call('POST', '/jobs/%s/publish' % a.job_id, {
                'expected_version': job['version'], 'attempt_id': attempt['attempt_id'], 'epoch': attempt['epoch'],
                'checkpoint_id': summary['final_checkpoint_id'], 'expected_base_version': job['delivery_version']}))
            return 0
        files = conn.call('GET', '/checkpoints/%s/files' % checkpoint)
        target = None
        if a.download:
            from .artifacts import materialize
            target = Path(a.download)
            if target.exists() and any(target.iterdir()):
                raise SystemExit('下载目录必须为空或不存在')
            materialize(target, {'version': 1, 'base': None, 'files': files['files'], 'deleted': []}, conn.blob)
        out(dict(files, job_id=a.job_id, published=bool(job['delivery_checkpoint_id']), downloaded_to=str(target) if target else None))
    return 0


def _baseline(argv):
    ap = argparse.ArgumentParser(prog='agent-collab baseline')
    sub = ap.add_subparsers(dest='action', required=True)
    add = sub.add_parser('add', help='把一个目录登记为可选的输入基线')
    add.add_argument('directory')
    add.add_argument('--name', required=True)
    connection_args(add)
    connection_args(sub.add_parser('list'))
    a = ap.parse_args(argv)
    conn = Conn(a)
    if a.action == 'list':
        out(conn.call('GET', '/baselines'))
    else:
        out(conn.call('POST', '/baselines', {'name': a.name, 'manifest': upload_directory(conn, a.directory)}))
    return 0


def _settings(argv):
    ap = argparse.ArgumentParser(prog='agent-collab settings')
    sub = ap.add_subparsers(dest='action', required=True)
    connection_args(sub.add_parser('get'))
    change = sub.add_parser('set', help='修改模型设置；没给的项保持不变')
    for name in ('codex-model', 'codex-effort', 'claude-model', 'claude-effort'):
        change.add_argument('--' + name)
    limits = change.add_mutually_exclusive_group()
    limits.add_argument('--allow', action='append', metavar='模型=effort1,effort2', help='部署允许的组合，可重复；给出后替换原有许可范围')
    limits.add_argument('--allow-any', action='store_true', help='部署不限制模型与 effort')
    connection_args(change)
    a = ap.parse_args(argv)
    conn = Conn(a)
    view = conn.call('GET', '/settings/models')
    if a.action == 'get':
        out(view)
        return 0
    settings = view['settings']
    codex, claude = settings['codex'], settings['claude']
    for target, prefix in ((codex['default'], 'codex'), (claude['default'], 'claude')):
        for key in ('model', 'effort'):
            if getattr(a, prefix + '_' + key) is not None:
                target[key] = getattr(a, prefix + '_' + key)
    if a.allow_any:
        codex['allowed'] = None
    elif a.allow:
        codex['allowed'] = {model: efforts.split(',') for model, _, efforts in (item.partition('=') for item in a.allow)}
    # expected_revision 取读到的版本：期间别人改过则被拒绝并返回当前值，不覆盖。
    out(conn.call('PUT', '/settings/models', {'expected_revision': settings['revision'], 'codex': codex, 'claude': claude}))
    return 0


def _migrate(argv):
    if argv and argv[0] == 'preflight':
        from . import preflight
        return preflight.main(argv[1:])
    if argv and argv[0] == 'rehearse':
        from . import rehearse
        return rehearse.main(argv[1:])
    raise SystemExit('用法：agent-collab migrate preflight ... | agent-collab migrate rehearse ...')


MODULES = {'controller': 'agent_collab.controller', 'agent': 'agent_collab.node_agent', 'legacy': 'agent_collab.legacy'}
COMMANDS = {'client': _client_command, 'submit': _submit, 'job': _job, 'baseline': _baseline, 'settings': _settings,
            'migrate': _migrate, 'api': lambda argv: _client(argv, False)}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd in MODULES:
        return importlib.import_module(MODULES[cmd]).main(rest) or 0
    if cmd in COMMANDS:
        try:
            return COMMANDS[cmd](rest) or 0
        except Unreachable:
            fail('network', 'controller 不可达')
    if cmd in ('install', 'doctor', 'service'):
        from . import installer
        return installer.main(([cmd] if cmd != 'install' else []) + rest)
    raise SystemExit('未知子命令：' + cmd)


if __name__ == '__main__':
    sys.exit(main())
