"""有界、脱敏、只落本机的诊断包；不执行认证探测或访问 controller。"""
import argparse
from collections import Counter
import getpass
import io
import json
import ntpath
import os
from pathlib import Path
import platform
import re
import stat
import sys
import zipfile

from . import installer, protocol, release
from .artifacts import SecretFile
from .project import is_link

FILE_LIMIT = 256 * 1024
JSON_LIMIT = 1024 * 1024
SUMMARY_LIMIT = 2 * JSON_LIMIT
TOTAL_LIMIT = 4 * JSON_LIMIT
MAX_FILES = 128
ERROR_LIMIT = 50
TASK_WARNING = '任务原始日志可能包含项目内容；已按相同规则脱敏，请在分享前检查预览。'
SECRET_KEY = re.compile(r'(?i)(?:^|[_-])(?:token|secret|password|code|key)(?:$|[_-])')
CONTENT_KEYS = {'input', 'inputs', 'prompt', 'task_input', 'source', 'source_code',
                'content', 'files', 'artifacts', 'stdout', 'stderr', 'stdout_tail', 'stderr_tail',
                'instructions', 'messages', 'frozen_input', 'task_spec',
                # 任务契约（controller 事件里的 job 记录会带上）：目标、验收项与排除项是用户写的任务内容。
                'contract', 'goal', 'acceptance', 'exclusions', 'harness_params'}


def normalized_key(name):
    return re.sub(r'([a-z])([A-Z])', r'\1_\2', name).lower()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')


class Redactor:
    def __init__(self, secrets=(), projects=(), home=None, username=None, hostname=None):
        self.counts = Counter()
        self.secrets = sorted(set(s for s in secrets if s), key=len, reverse=True)
        self.projects = sorted(set(projects), key=len, reverse=True)
        self.home = str(Path.home() if home is None else home)
        self.username = getpass.getuser() if username is None else username
        self.hostname = platform.node() if hostname is None else hostname

    def mark(self, category, replacement=None):
        self.counts[category] += 1
        return replacement if replacement is not None else '[REDACTED:%s]' % category

    def text(self, value):
        # JSONL 先解码，避免转义的路径、引号与私钥换行绕过文本规则。
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            return json.dumps(self.value(parsed), ensure_ascii=False)
        value = re.sub(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)',
                       lambda m: self.mark('private_key'), value, flags=re.S)
        value = re.sub(r'\A.*?-----END [A-Z ]*PRIVATE KEY-----',
                       lambda m: self.mark('private_key'), value, flags=re.S)
        for secret in self.secrets:
            value = re.sub(re.escape(secret), lambda m: self.mark('known_token'), value)
        for path in self.projects:
            for spelling in sorted({path, path.replace('\\', '/'), path.replace('\\', '\\\\')}, key=len, reverse=True):
                value = re.sub(re.escape(spelling), lambda m: self.mark('project_path', ntpath.basename(path.rstrip('/\\'))),
                               value, flags=re.I)
        value = re.sub(r'(?im)\b(?:authorization|proxy-authorization|x-coordinator-token|set-cookie|cookie)["\x27]?\s*[:=]\s*[^\r\n]+',
                       lambda m: self.mark('authorization'), value)
        value = re.sub(r'(?i)\b(?:Bearer|Basic)\s+[^\s,;"\x27]+', lambda m: self.mark('authorization'), value)
        # 命令参数里的口令：--password VALUE、--password=VALUE 已由字段规则处理，这里补上空格分隔与 argv 列表的写法。
        value = re.sub(r'(?i)(--?[\w-]*(?:password|passwd|secret|token)["\x27]?(?:\s*,\s*|\s+))("[^"]*"|\x27[^\x27]*\x27|[^\s"\x27,\]]+)',
                       lambda m: m.group(1) + self.mark('argument'), value)
        # URL 里的 userinfo（https://user:password@host）同样是凭据。
        value = re.sub(r'(?i)\b([a-z][a-z0-9+.-]*://)[^\s/"\x27]+@', lambda m: m.group(1) + self.mark('url_userinfo') + '@', value)
        value = re.sub(r'\b(?:(?i:(?:[\w-]*[_-])?(?:token|secret|password|code|key))|'
                       r'[\w-]+(?:Token|Secret|Password|Code|Key))(?:[_-][\w-]+|[A-Z][\w-]*)?["\x27]?\s*[:=]\s*'
                       r'(?:"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27|[^\s,;{}\[\]]+)',
                       lambda m: self.mark('field'), value)
        value = re.sub(r'(?<![\w-])[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{8,}(?![\w-])',
                       lambda m: self.mark('jwt'), value)
        value = re.sub(r'\bsk-[A-Za-z0-9_-]+', lambda m: self.mark('api_key'), value)
        value = re.sub(r'(?<![\w-])[a-fA-F0-9]{32,}(?![\w-])', lambda m: self.mark('hex_token'), value)
        value = re.sub(r'(?<![\w-])[A-Za-z0-9_-]{32,}={0,2}(?![\w-])', lambda m: self.mark('random_token'), value)
        for spelling in sorted({self.home, self.home.replace('\\', '/'), self.home.replace('\\', '\\\\')}, key=len, reverse=True):
            if spelling:
                value = re.sub(re.escape(spelling), lambda m: self.mark('home', '~'), value, flags=re.I)
        # controller 信息也可能来自另一台机器，不能只处理当前用户的路径。
        value = re.sub(r'(?i)(?:[A-Z]:[\\/]+Users[\\/]+|/(?:Users|home)/)[^\\/\s"\x27,;]+',
                       lambda m: self.mark('home', '~'), value)
        for identity, category, replacement in ((self.username, 'username', '<user>'), (self.hostname, 'hostname', '<host>')):
            if identity:
                value = re.sub(r'(?<![\w-])' + re.escape(identity) + r'(?![\w-])',
                               lambda m: self.mark(category, replacement), value, flags=re.I)
        return value

    def value(self, value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                name = str(key)
                normalized = normalized_key(name)
                # auth 与 *_auth_config（例如容器仓库的 auths）里是编码后的凭据，短值也替换。
                if (SECRET_KEY.search(normalized) or re.search(r'(?:^|[_-])cookie(?:$|[_-])', normalized) or
                        normalized == 'auth' or normalized.endswith('_auth_config') or
                        normalized in ('authorization', 'proxy-authorization', 'x-coordinator-token')):
                    item = self.mark('field')
                elif normalized in CONTENT_KEYS:
                    item = self.mark('excluded_content')
                elif normalized in ('username', 'user', 'account', 'owner') and isinstance(item, str):
                    item = self.mark('username', '<user>')
                elif normalized in ('hostname', 'host', 'computername') and isinstance(item, str):
                    item = self.mark('hostname', '<host>')
                elif normalized in ('path', 'directory', 'root', 'project_path', 'workspace_root', 'local_root') and isinstance(item, str):
                    item = self.text(self.mark('project_path', ntpath.basename(item.rstrip('/\\'))))
                else:
                    item = self.value(item)
                result[self.text(name)] = item
            return result
        if isinstance(value, list):
            # argv 数组（例如 JSON 日志里的 ["tool", "--password", "x"]）：口令类选项后面的那一项按凭据替换。
            result, secret_next = [], False
            for item in value:
                if secret_next and isinstance(item, str):
                    result.append(self.mark('argument'))
                else:
                    result.append(self.value(item))
                secret_next = isinstance(item, str) and bool(re.fullmatch(r'(?i)--?[\w-]*(?:password|passwd|secret|token)', item))
            return result
        return self.text(value) if isinstance(value, str) else value


def local_path(path):
    path = Path(path).expanduser().absolute()
    if str(path).startswith(('\\\\', '//')):
        raise ValueError('仅支持本机路径')
    if os.name == 'nt':
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) == 4:
            raise ValueError('不支持网络映射盘')
    if any(part.casefold() in ('.codex', '.claude', '.ssh') for part in path.parts):
        raise ValueError('不读取认证配置目录')
    for parent in reversed((path,) + tuple(path.parents)):
        try:
            if is_link(parent):
                raise ValueError('不跟随符号链接或重解析点')
        except FileNotFoundError:
            pass
    return path


def read_file(path, limit, tail=False, token=False):
    path = local_path(path)
    if path.suffix.lower() in ('.token', '.pem', '.key', '.p12', '.pfx', '.db', '.sqlite', '.sqlite3', '.bak') and not token:
        raise ValueError('不读取凭据、私钥、数据库或备份文件')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
        raise ValueError('仅收集普通单链接文件')
    # 先拒绝 FIFO/设备；POSIX 打开时再禁止跟随链接及阻塞，随后核对实际句柄。
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(str(path), flags), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise ValueError('仅收集普通单链接文件')
        size = info.st_size
        start = max(0, size - limit) if tail else 0
        stream.seek(start)
        data = stream.read(min(size, limit))
    read_bytes = len(data)
    if start:
        # 丢弃切断的首行，防止截出无法识别的凭据片段。
        data = data.partition(b'\n')[2]
    return data, dict(original_bytes=size, read_bytes=read_bytes, truncated=size > len(data))


def read_json(path):
    data, meta = read_file(path, JSON_LIMIT)
    if meta['truncated']:
        raise ValueError('JSON 超过 1 MiB 上限，未收集')
    return json.loads(data), meta


def configuration(args):
    """返回 (config, old, broken)：安装记录损坏时 broken 是那个异常，按没有记录处理以便照常收集其他部分。"""
    name = 'agent-collab' + ('-' + args.instance if args.instance else '')
    prefix = local_path(args.prefix or Path.home() / '.local/share' / name)
    broken = None
    try:
        old, _ = read_json(prefix / 'installation.json')
        if old:
            local_path(old['config']['state_dir'])
            if not isinstance(old['config']['service'], bool) or not isinstance(old.get('version'), (str, type(None))):
                raise ValueError('invalid installation record')
    except FileNotFoundError:
        old = None
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        old, broken = None, exc
    options = ['--prefix', str(prefix)]
    if args.state_dir:
        options += ['--state-dir', str(local_path(args.state_dir))]
    elif args.instance and not old:
        options += ['--state-dir', str(Path.home() / '.local/state' / name)]
    if args.instance:
        options += ['--instance', args.instance]
    config = installer.configuration(installer.parser().parse_args(options), old, platform.system())
    if old:
        config['service'] = old['config']['service']
    config['state_dir'] = str(local_path(config['state_dir']))
    if config['role'] not in installer.ROLES:
        raise ValueError('无效的安装角色')
    return config, old, broken


def attempt_directories(state):
    root = local_path(state / 'worker/attempts')
    if not root.exists():
        return [], False
    found = []
    with os.scandir(root) as entries:
        for entry in entries:
            if re.fullmatch(r'[a-fA-F0-9-]{36}', entry.name) and entry.is_dir(follow_symlinks=False):
                found.append(local_path(entry.path))
                if len(found) == MAX_FILES:
                    return found, True
    return found, False


def task_logs(folder):
    folders = [folder]
    with os.scandir(folder) as entries:
        for entry in entries:
            if re.fullmatch(r'step-\d{1,2}', entry.name) and entry.is_dir(follow_symlinks=False):
                folders.append(local_path(entry.path))
    checks = local_path(folder / 'checks')
    if checks.is_dir():
        with os.scandir(checks) as entries:
            for entry in entries:
                if re.fullmatch(r'\d{1,2}', entry.name) and entry.is_dir(follow_symlinks=False):
                    folders.append(local_path(entry.path))
    return [(directory / filename, '任务原始日志') for directory in folders
            for filename in ('stdout.jsonl', 'stderr.log')]


def collect(args):
    config, old, broken = configuration(args)
    prefix, state = Path(config['prefix']), Path(config['state_dir'])
    projects, secrets = [], []
    notes = []
    try:
        bindings, _ = read_json(state / 'worker/project-bindings.json')
        projects = [v['path'] for v in bindings.values() if isinstance(v, dict) and isinstance(v.get('path'), str)]
    except (OSError, ValueError, AttributeError):
        notes.append('项目绑定缺失或不可读；只使用通用路径规则')
    try:
        attempts, attempts_truncated = attempt_directories(state)
    except (OSError, ValueError):
        # attempt 目录坏了（例如成了普通文件或链接）只影响依赖它的部分。
        attempts, attempts_truncated = [], False
        notes.append('attempt 目录不可读；没有收集 attempt 相关的令牌与任务日志')
    # 仅这些本产品令牌用于内存中的精确替换；不导出、不打印，不读客户端或官方 CLI 凭据。
    token_paths = [state / 'worker/node.token', state / 'controller/admin.token']
    token_paths += [folder / 'lease.token' for folder in attempts]
    for path in token_paths:
        try:
            raw, meta = read_file(path, 4096, token=True)
            if not meta['truncated']:
                secrets.append(raw.decode('utf-8').strip())
        except (OSError, ValueError):
            pass
    redactor = Redactor(secrets, projects)
    sections = {}

    def section(name, source, scope, value, meta=None):
        before = redactor.counts.copy()
        clean = redactor.value(value)
        sections[name] = dict(source=redactor.text(source), scope=scope, data=clean,
                              bytes=len(encoded(clean)), truncated=False)
        sections[name].update(meta or {})
        sections[name]['redactions'] = dict(redactor.counts - before)

    def json_section(name, path, fields=None):
        try:
            raw, meta = read_file(path, JSON_LIMIT)
            if meta['truncated']:
                section(name, str(path), '固定 JSON 元数据', None, dict(meta, status='unavailable', reason='json_limit'))
                return
            value = json.loads(raw)
            if fields is not None:
                value = {k: v for k, v in value.items() if k in fields}
            section(name, str(path), '固定 JSON 元数据', value, meta)
            if fields is not None:
                sections[name]['fields'] = sorted(fields)
        except (OSError, ValueError, AttributeError, RecursionError) as exc:
            section(name, str(path), '固定 JSON 元数据', None,
                    dict(status='unavailable', error_type=type(exc).__name__))

    section('runtime', '运行中的 Python 包', '版本、协议与系统',
            dict(release=release.release_info(), protocol=protocol.PROTOCOL_VERSION,
                 python=platform.python_version(), os=platform.system(), os_version=platform.release(),
                 architecture=platform.machine()))
    try:
        # discover 只读取这个固定 JSON；在调用前拒绝重定向及过大的文件，坏文件让这一段不可用而不是中止导出。
        if broken is not None:
            raise broken
        try:
            read_json(state / 'worker/agent.json')
        except FileNotFoundError:
            pass
        section('deployment', str(prefix / 'installation.json'), '安装器只读发现、服务状态/归属/期望与运行上下文',
                installer.discover(config, old))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        section('deployment', str(prefix), '安装器只读发现', None, dict(status='unavailable', error_type=type(exc).__name__))
    manifest_fields = {'version', 'git_commit', 'git_dirty', 'built_at', 'python_requires', 'protocol',
                       'protocol_version', 'features', 'controller_features', 'controller_schema_min',
                       'controller_schema_max', 'controller_schema_fingerprint', 'build_kind', 'channel',
                       'release_id', 'source_commit', 'source_dirty', 'compatibility', 'components', 'schema'}
    root = release.version_directory()
    if old and old.get('version'):
        root = prefix / 'versions' / installer.validate_version(old['version'])
    for filename in ('release-manifest.json', 'product-manifest.json'):
        json_section(filename[:-5], root / filename, manifest_fields)
    json_section('worker', state / 'worker/status.json',
                 {'version', 'git_commit', 'version_dir', 'pid', 'last_heartbeat_ok_at', 'last_error', 'auth_health', 'auth_checked_at'})
    if args.controller_info:
        json_section('controller', local_path(args.controller_info))
    else:
        section('controller', '未提供 --controller-info', '没有网络请求', None)

    candidates = [(prefix / 'installer.log', '安装器日志'), (state / 'installer.log', '安装器日志'),
                  (state / 'worker/agent.log', '节点日志'), (state / 'controller/controller.log', 'controller 日志')]
    for kind in ('installer', 'worker', 'controller'):
        for suffix in ('.log', '.out.log', '.err.log'):
            candidates.append((state / 'logs' / (kind + suffix), '服务与安装器日志'))
    if args.include_task_logs:
        notes.append(TASK_WARNING)
        for folder in attempts:
            candidates += task_logs(folder)
    logs, records, errors = {}, [], []
    error_count = 0
    remaining = args.max_bytes
    candidates_truncated = False
    for path, scope in candidates:
        if len(records) >= MAX_FILES:
            candidates_truncated = True
            break
        before = redactor.counts.copy()
        try:
            data, meta = read_file(path, min(FILE_LIMIT, remaining), tail=True)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            records.append(dict(source=redactor.text(str(path)), scope=scope, status='unavailable', error_type=type(exc).__name__))
            continue
        remaining -= meta['read_bytes']
        text = data.decode('utf-8', errors='replace')
        # 整体处理私钥块，再逐行处理 JSONL，统计只计算实际发生的替换。
        text = re.sub(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)',
                      lambda m: redactor.mark('private_key'), text, flags=re.S)
        text = re.sub(r'\A.*?-----END [A-Z ]*PRIVATE KEY-----', lambda m: redactor.mark('private_key'), text, flags=re.S)
        clean = '\n'.join(redactor.text(line) for line in text.splitlines())
        content = clean.encode('utf-8')
        # 占位符可能比原文更长；同时限制输入与导出字节，避免短秘密导致膨胀。
        output_limit = min(FILE_LIMIT, args.max_bytes - sum(len(v) for v in logs.values()))
        if len(content) > output_limit:
            content = content[:output_limit].decode('utf-8', errors='ignore').encode('utf-8')
            meta['truncated'] = True
        name = 'logs/%03d.txt' % len(logs)
        logs[name] = content
        records.append(dict(name=name, source=redactor.text(str(path)), scope=scope, bytes=len(content),
                            redactions=dict(redactor.counts - before), **meta))
        for line in content.decode('utf-8').splitlines():
            if re.search(r'(?i)\b(error|exception|failed|fatal|traceback)\b|错误|失败', line):
                error_count += 1
                errors.append(dict(file=name, text=line[:2000]))
        errors = errors[-ERROR_LIMIT:]
    report = dict(format_version=1, sections=sections, logs=records, recent_errors=errors,
                  recent_error_scope=dict(source='已收集的日志尾部，按文件顺序', matched_lines=error_count,
                                          truncated=error_count > ERROR_LIMIT, per_line_characters=2000),
                  limits=dict(per_log_bytes=FILE_LIMIT, total_log_bytes=args.max_bytes, json_bytes=JSON_LIMIT,
                              summary_bytes=SUMMARY_LIMIT, log_files=MAX_FILES, recent_errors=ERROR_LIMIT),
                  scope=dict(task_logs=args.include_task_logs, network=False, attempt_inventory_truncated=attempts_truncated,
                             log_inventory_truncated=candidates_truncated, system_journal='未收集；仅固定位置的本机日志文件'),
                  notes=notes, redactions=dict(redactor.counts))
    if len(encoded(report)) > SUMMARY_LIMIT:
        raise ValueError('诊断摘要超过 2 MiB 上限，未写入导出文件')
    return report, logs


def preview(path, show=None):
    path = local_path(path)
    if path.stat().st_size > 67 * JSON_LIMIT:
        raise ValueError('诊断包超过预览上限')
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if (len(entries) > MAX_FILES + 1 or len(set(names)) != len(names) or 'diagnostics.json' not in names or
                any(name != 'diagnostics.json' and not re.fullmatch(r'logs/\d{3}\.txt', name) for name in names) or
                any(entry.file_size > (SUMMARY_LIMIT if entry.filename == 'diagnostics.json' else FILE_LIMIT)
                    for entry in entries) or
                sum(entry.file_size for entry in entries) > 64 * JSON_LIMIT + SUMMARY_LIMIT):
            raise ValueError('不是支持的有界诊断包')

        def text(name, limit):
            if archive.getinfo(name).file_size > limit:
                raise ValueError('包内文本超过预览上限')
            with archive.open(name) as stream:
                data = stream.read(limit + 1)
            if len(data) > limit:
                raise ValueError('包内文本超过预览上限')
            return data.decode('utf-8')

        if show:
            if show not in names:
                raise ValueError('包内没有该文件')
            return text(show, SUMMARY_LIMIT if show == 'diagnostics.json' else FILE_LIMIT)
        report = json.loads(text('diagnostics.json', SUMMARY_LIMIT))
        records = {item['name']: item for item in report['logs'] if 'name' in item}
        files = [dict(name=entry.filename, bytes=entry.file_size,
                      truncated=records.get(entry.filename, {}).get('truncated', False),
                      redactions=records.get(entry.filename, {}).get('redactions', report['redactions'] if entry.filename == 'diagnostics.json' else {}))
                 for entry in entries]
        return dict(files=files, redactions=report['redactions'], diagnostics=report)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='agent-collab diagnostics')
    commands = parser.add_subparsers(dest='command', required=True)
    export = commands.add_parser('export')
    export.add_argument('--out', required=True)
    for option in ('prefix', 'state-dir', 'instance', 'controller-info', 'profile'):
        export.add_argument('--' + option)
    export.add_argument('--include-task-logs', action='store_true')
    export.add_argument('--max-bytes', type=int, default=TOTAL_LIMIT)
    inspect = commands.add_parser('preview')
    inspect.add_argument('file')
    inspect.add_argument('--show')
    args = parser.parse_args(argv)
    try:
        if args.command == 'preview':
            result = preview(args.file, args.show)
        else:
            if args.profile:
                raise ValueError('离线诊断不读取 profile 凭据或访问网络；请提供 --controller-info JSON 文件')
            if args.instance and not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,30}', args.instance):
                raise ValueError('实例名仅允许 1–31 个小写字母、数字或连字符')
            if not 1 <= args.max_bytes <= 64 * JSON_LIMIT:
                raise ValueError('--max-bytes 必须在 1 至 67108864 之间')
            target = local_path(args.out)
            if target.suffix.lower() != '.zip' or target.exists():
                raise ValueError('--out 必须是尚不存在的本机 .zip 文件')
            report, logs = collect(args)
            if args.include_task_logs:
                print(TASK_WARNING, file=sys.stderr)
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('diagnostics.json', encoded(report))
                for name, content in logs.items():
                    archive.writestr(name, content)
            with SecretFile(target) as pending:
                pending.save(buffer.getvalue())
            result = preview(target)
        print(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, RecursionError, zipfile.BadZipFile) as exc:
        # 底层异常可能带原始路径或内容；只报告类型，不回显未经脱敏的数据。
        print(json.dumps(dict(error='diagnostics_failed', error_type=type(exc).__name__,
                              message=str(exc) if type(exc) is ValueError and str(exc) in (
                                  '离线诊断不读取 profile 凭据或访问网络；请提供 --controller-info JSON 文件',
                                  '--out 必须是尚不存在的本机 .zip 文件', '--max-bytes 必须在 1 至 67108864 之间',
                                  '诊断摘要超过 2 MiB 上限，未写入导出文件') else '无法安全收集或预览诊断；请检查本机路径、文件格式与权限'),
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
