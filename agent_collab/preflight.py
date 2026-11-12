"""只读迁移盘点：python -m agent_collab.preflight --help。"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PureWindowsPath
import platform
import plistlib
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET

from .domain import managed_profile


REDACTED = '[redacted]'
MAX_SCAN_BYTES = 2 * 1024 * 1024
_SENSITIVE_WORDS = r'token|secret|password|authorization|credential|cookie|api[_-]?key|access[_-]?key|private[_-]?key'
SENSITIVE = re.compile(_SENSITIVE_WORDS, re.I)
TERMINAL = {'completed', 'failed', 'cancelled'}
ACTIVE = {'assigned', 'leased', 'running'}


def _object(value):
    return value if isinstance(value, dict) else {}


def _pick(value, names):
    return {name: value.get(name) for name in names.split()}


def _secret(path):
    name = path.name.lower()
    return (name.endswith(('.token', '.pem', '.key')) or name == 'auth.json'
            or name.startswith(('.credentials', 'id_rsa', 'id_ed25519', 'id_ecdsa', 'id_dsa')))


class _Redactor:
    def __init__(self):
        self.values = set()
        self.short = set()  # 环境块里的短值：不做全局替换，只用来判断自由文本行能否安全输出

    def remember(self, value, minimum=1):
        if isinstance(value, str) and value and value != REDACTED:
            (self.values if len(value) >= minimum else self.short).add(value)
        elif isinstance(value, dict):
            for child in value.values():
                self.remember(child, minimum)
        elif isinstance(value, list):
            for child in value:
                self.remember(child, minimum)

    def json(self, value):
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                if SENSITIVE.search(key):
                    self.remember(child)
                    result[key] = REDACTED
                elif key.lower() in ('env', 'environment', 'environmentvariables'):
                    # 记住被遮蔽的值：逐行扫描同一文件时，这些值也不能以原文出现在引用里。
                    # 短值（"1"、"true"，也可能是短口令）若全局替换会改坏报告里的版本号等，单独记下，见 free_text。
                    self.remember(child, minimum=8)
                    result[key] = REDACTED
                else:
                    result[key] = self.json(child)
            return result
        if isinstance(value, list):
            result = []
            hide_next = False
            for child in value:
                if hide_next:
                    self.remember(child)
                    result.append(REDACTED)
                else:
                    result.append(self.json(child))
                hide_next = (isinstance(child, str) and child.startswith('--') and '=' not in child
                             and bool(SENSITIVE.search(child)) and not child.endswith(('-file', '-path')))
            return result
        return value

    def text(self, value):
        for secret in sorted(self.values, key=len, reverse=True):
            value = value.replace(secret, REDACTED)
        value = re.sub(r'(?i)\b(Bearer|Basic)\s+[^\s\"\'<>;,]+', r'\1 ' + REDACTED, value)
        value = re.sub(r'(://)[^/\s:@]+:[^/\s@]+@', r'\1' + REDACTED + '@', value)
        # 不猜测 shell/JSON 的复杂引号边界：含凭据赋值的余行整体遮蔽。
        value = re.sub(
            r'''(?ix)([\w.-]*(?:%s)[\w.-]*["']?\s*[:=]\s*).*''' % _SENSITIVE_WORDS,
            r'\1' + REDACTED, value)
        # --token、--api-key 之后的值遮蔽；--token-file 之类给的是路径，不匹配。
        value = re.sub(r'(?i)(--(?:\w+-)*(?:%s)\s+).*' % _SENSITIVE_WORDS, r'\1' + REDACTED, value)
        if re.search(r'(?i)(?:^|\s)(?:export\s+|Environment(?:Variables)?\s*=)|\b[A-Z_][A-Z0-9_]*=', value):
            return REDACTED
        return value

    def free_text(self, value):
        """从文件里原样取出的行（引用、服务命令）：含有环境块短值作为独立片段时整行遮蔽，无法只遮那一段而不误伤。"""
        value = self.text(value)
        for secret in self.short:
            if re.search(r'(?<![\w.-])' + re.escape(secret) + r'(?![\w.-])', value):
                return REDACTED
        return value

    def strings(self, value):
        # 仅清理字符串，不能遮蔽固定报告里的 node_credentials/count 等统计字段。
        if isinstance(value, dict):
            return {k: self.strings(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.strings(v) for v in value]
        return self.text(value) if isinstance(value, str) else value


class _Reader:
    def __init__(self):
        self.redactor = _Redactor()
        self.risks = []
        self.missing = []

    def risk(self, code, path, error=None):
        detail = str(path) + (': ' + type(error).__name__ if error else '')
        item = dict(code=code, detail=detail)
        if item not in self.risks:
            self.risks.append(item)

    def children(self, path):
        try:
            if path.is_symlink():
                self.risk('symlink_skipped', path)
                return []
            return sorted(path.iterdir()) if path.exists() else []
        except OSError as exc:
            self.risk('directory_unreadable', path, exc)
            return []

    def files(self, path):
        if path.is_symlink():
            self.risk('symlink_skipped', path)
        elif path.is_dir():
            for child in self.children(path):
                yield from self.files(child)
        elif path.is_file():
            yield path

    def read(self, path, limit=None):
        try:
            if _secret(path) or path.is_symlink():
                self.risk('content_skipped', path)
                return None
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                self.risk('non_regular_file', path)
                return None
            if limit is not None and info.st_size > limit:
                self.risk('scan_too_large', path)
                return None
            with path.open('rb') as stream:
                data = stream.read() if limit is None else stream.read(limit + 1)
            if limit is not None and len(data) > limit:
                self.risk('scan_too_large', path)
                return None
            return data
        except OSError as exc:
            self.risk('file_unreadable', path, exc)
            return None

    def json(self, path, optional=False):
        if optional and not path.exists():
            return None
        raw = self.read(path)
        if raw is None:
            return None
        try:
            value = json.loads(raw.decode('utf-8-sig'))
            if not isinstance(value, dict):
                raise ValueError()
            return self.redactor.json(value)
        except (ValueError, UnicodeError, RecursionError) as exc:
            self.risk('json_unreadable', path, exc)
            return None

    def secrets(self, root, required=()):
        paths = {root / name for name in required}
        paths.update(path for path in self.files(root) if _secret(path))
        result = []
        for path in sorted(paths):
            row = dict(path=str(path), present=False, size_bytes=None, mode=None)
            try:
                info = path.lstat()
                row.update(present=True, size_bytes=info.st_size, mode=oct(stat.S_IMODE(info.st_mode)))
            except FileNotFoundError:
                pass
            except OSError as exc:
                self.risk('metadata_unreadable', path, exc)
            result.append(row)
        return result

    def sizes(self, root):
        count, size = 0, 0
        for path in self.files(root):
            try:
                size += path.stat().st_size
                count += 1
            except OSError as exc:
                self.risk('metadata_unreadable', path, exc)
        return dict(count=count, bytes=size)


class _UnsafeJournal(Exception):
    pass


class _ChangedDatabase(Exception):
    pass


def _db_stamp(path):
    result = []
    for entry in (path, Path(str(path) + '-wal'), Path(str(path) + '-journal')):
        try:
            info = entry.lstat()
        except FileNotFoundError:
            result.append(None)
            continue
        if not stat.S_ISREG(info.st_mode):
            raise _UnsafeJournal()
        if entry != path and info.st_size:
            # mode=ro 仍可能创建/改写 SHM；immutable 会忽略 WAL。宁可明确缺失，不能报旧数据。
            raise _UnsafeJournal()
        result.append((info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
    return result


def _database(path, reader):
    report = dict(path=str(path), readable=False, size_bytes=None, journal_mode=None,
                  tables=[], record_counts={}, events=dict(count=None, min_seq=None, max_seq=None), error=None)
    records = {}
    connection = None
    try:
        report['size_bytes'] = path.stat().st_size
        before = _db_stamp(path)
        connection = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=0)
        report['journal_mode'] = connection.execute('PRAGMA journal_mode').fetchone()[0]
        with path.open('rb') as stream:
            header = stream.read(20)
        if header[18:20] == b'\x02\x02':
            report['journal_mode'] = 'wal'
        report['tables'] = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        report['record_counts'] = dict(connection.execute('SELECT kind, count(*) FROM records GROUP BY kind'))
        count, low, high = connection.execute('SELECT count(*), min(seq), max(seq) FROM events').fetchone()
        report['events'] = dict(count=count, min_seq=low, max_seq=high)
        for kind in ('meta', 'job', 'attempt', 'coordinator', 'node', 'client', 'settings'):
            records[kind] = {}
            for ident, raw in connection.execute('SELECT id, data FROM records WHERE kind=? ORDER BY id', (kind,)):
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError()
                record = reader.redactor.json(value)
                if kind == 'coordinator':
                    record['_has_lease_token'] = bool(value.get('token_hash'))
                records[kind][ident] = record
        if _db_stamp(path) != before:
            raise _ChangedDatabase()
        report['readable'] = True
    except (OSError, sqlite3.Error, ValueError, _UnsafeJournal, _ChangedDatabase) as exc:
        report.update(error=type(exc).__name__, record_counts={}, events=dict(count=None, min_seq=None, max_seq=None))
        records = {}
        reader.missing.append(dict(code='database_unreadable', detail=str(path) + ': ' + type(exc).__name__))
        if isinstance(exc, _UnsafeJournal):
            reader.risk('sqlite_sidecar_requires_snapshot', '存在日志旁文件或非普通文件；需要另行提供一致快照，未读取可能过时的数据')
    finally:
        if connection is not None:
            connection.close()
    return report, records


def _path_kind(value):
    if not isinstance(value, str) or not value:
        return 'unknown'
    if value.startswith(('\\\\', '//')):
        return 'unc'
    if value.lower().startswith('smb://') or value.startswith('/Volumes/'):
        return 'smb'
    if value.startswith('/') or PureWindowsPath(value).is_absolute():
        return 'local'
    return 'unknown'


def _controller(root, reader, now, snapshot=None):
    # snapshot：一致的数据库快照（如备份集里的 controller-<哈希>.db）。运行中的 controller 通常有非空 WAL，
    # 严格只读无法读取它的 controller.db；任务、节点等记录改从快照读，配置、blob 与凭据文件的元数据仍取数据目录。
    database, records = _database(snapshot or root / 'controller.db', reader)
    database['source'] = 'snapshot' if snapshot else 'data_dir'
    meta = records.get('meta', {}).get('controller')
    if meta is not None:
        meta = dict(_pick(meta, 'controller_id authority_generation dispatch_paused'),
                    metadata_version=meta.get('version'))
    config = reader.json(root / 'config.json', optional=True)
    jobs = list(records.get('job', {}).values())
    attempts = records.get('attempt', {}).values()
    nodes = []
    for ident, node in records.get('node', {}).items():
        cap = _object(node.get('capabilities'))
        nodes.append(dict(_pick(node, 'agent_version physical_host_id drain last_seen_at'),
                          node_id=node.get('node_id', ident), os=cap.get('os'), arch=cap.get('arch'),
                          runtime_user=_object(node.get('runtime_profile')).get('user'),
                          harness=cap.get('harness'), models_capability='models' in cap,
                          retired=bool(node.get('retired_at'))))
    leases = []
    for ident, lease in records.get('coordinator', {}).items():
        try:
            expires = datetime.fromisoformat(lease['expires_at'].replace('Z', '+00:00'))
            active = bool(expires > now and lease.get('_has_lease_token') and meta
                          and lease.get('authority_generation') == meta['authority_generation'])
        except (KeyError, TypeError, ValueError, AttributeError):
            active = None
        leases.append(dict(work_package_id=ident, **_pick(lease, 'holder_kind expires_at'), active=active))
    bindings = []
    for job in jobs:
        integration = _object(_object(job.get('contract')).get('integration'))
        if integration.get('target_node_id') or integration.get('target_path'):
            bindings.append(dict(_pick(job, 'job_id work_package_id title integration_status'),
                                 **_pick(integration, 'target_node_id target_path'),
                                 path_kind=_path_kind(integration.get('target_path'))))
    backup_root = root.parent / 'backups'
    backups = []
    for path in reader.children(backup_root):
        if path.is_symlink():
            reader.risk('symlink_skipped', path)
            continue
        if path.name in ('status.json', '.lock', '.partial', 'controller.lock'):
            continue  # 定时备份自己的状态与锁文件，不是备份集
        # 两种布局：controller backup 直接写出的目录（backup-manifest.json、数据库、blobs/），
        # 以及定时备份打包的集合（COMPLETE.json + backup.tar，清单与数据库在包里）。
        manifest, complete = path / 'backup-manifest.json', path / 'COMPLETE.json'
        archive = path / 'backup.tar'
        packaged = complete.is_file() and not complete.is_symlink() and archive.is_file() and not archive.is_symlink()
        if not path.is_dir() or not (packaged or (manifest.is_file() and not manifest.is_symlink())):
            reader.risk('backup_set_unrecognized', path)
            continue
        try:
            marker = (reader.json(complete) if packaged else reader.json(manifest)) or {}
            backups.append(dict(name=path.name, kind='packaged' if packaged else 'directory', bytes=reader.sizes(path)['bytes'],
                                mtime=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                                created_at=marker.get('created_at'), db_sha256=marker.get('db_sha256')))
        except OSError as exc:
            reader.risk('metadata_unreadable', path, exc)
    if not backups:
        reader.missing.append(dict(code='backup', detail=str(backup_root)))
    counts = database['record_counts']
    settings = records.get('settings', {}).get('models')
    return dict(data_dir=str(root), database=database, meta=meta,
                config=dict(present=(root / 'config.json').exists(), keys=sorted(config or {}),
                            public_origin=_object(config).get('public_origin'),
                            deployment_profile=_object(config).get('deployment_profile'),
                            managed_coordinator=managed_profile(config or {})),
                jobs=dict(total=len(jobs), by_status=dict(Counter(str(j.get('status', 'unknown')) for j in jobs)),
                          non_terminal=[dict(_pick(j, 'job_id status work_package_id title'),
                                             harness=_object(j.get('contract')).get('harness'))
                                        for j in jobs if j.get('status') not in TERMINAL]),
                attempts=dict(active=[_pick(a, 'attempt_id job_id node_id status epoch authority_generation')
                                      for a in attempts if a.get('status') in ACTIVE]),
                coordinator_leases=leases, nodes=nodes, node_credentials=dict(count=counts.get('node_token', 0)),
                clients=dict(count=counts.get('client', 0), revoked=sum(
                    bool(c.get('revoked_at') or c.get('revoked')) for c in records.get('client', {}).values())),
                sessions=dict(count=counts.get('session', 0)),
                model_settings=dict(present=settings is not None, revision=_object(settings).get('revision')),
                blobs=reader.sizes(root / 'blobs'),
                # status：定时备份记录的本机最近成功与实例外最近确认（没有定时备份时为 null）。
                backups=dict(directory=str(backup_root), sets=backups, status=reader.json(backup_root / 'status.json', optional=True)),
                secret_files=reader.secrets(root, ('admin.token',))), bindings


def _node(root, reader):
    config = reader.json(root / 'agent.json') or {}
    status = reader.json(root / 'status.json', optional=True)
    attempts = []
    unreported = []
    for directory in reader.children(root / 'attempts'):
        if not directory.is_dir() or directory.is_symlink():
            continue
        meta = reader.json(directory / 'meta.json')
        attempts.append(directory.name)
        if _object(meta).get('state') != 'reported':
            unreported.append(directory.name)
    return dict(data_dir=str(root), **_pick(config, 'node_id controller controller_id authority_generation '
                                          'physical_host_id failure_domain slots host_slots labels'),
                commands=_pick(config, 'codex_command claude_command codex_home disabled_harnesses'),
                status=_pick(status, 'version git_commit pid last_heartbeat_ok_at auth_health auth_checked_at')
                if status is not None else None,
                attempts=dict(total=len(attempts), unreported=unreported),
                secret_files=reader.secrets(root, ('node.token',)))


def _service(path, reader, user):
    raw = reader.read(path)
    if raw is None:
        return None
    try:
        if path.suffix == '.service':
            fields = {}
            section = None
            for line in raw.decode('utf-8').splitlines():
                line = line.strip()
                if line.startswith('['):
                    section = line
                elif section == '[Service]' and '=' in line and not line.startswith(('#', ';')):
                    key, value = line.split('=', 1)
                    if key in ('User', 'ExecStart'):
                        fields[key] = value
            kind, name = 'systemd', path.name
            run_user, command = fields.get('User', user), fields.get('ExecStart')
        elif path.suffix == '.plist':
            data = reader.redactor.json(plistlib.loads(raw))
            kind, name = 'launchd', data.get('Label', path.stem)
            run_user, command = data.get('UserName', user), data.get('ProgramArguments', data.get('Program'))
        else:
            tree = ET.fromstring(raw)
            kind, name = 'schtasks', path.stem
            run_user = tree.findtext('.//{*}Principal/{*}UserId')
            command = ' '.join(filter(None, (tree.findtext('.//{*}Exec/{*}Command'),
                                            tree.findtext('.//{*}Exec/{*}Arguments'))))
        if isinstance(command, list):
            # 在拼接后脱敏，防止 --token 与它的值分居不同参数。
            command = subprocess.list2cmdline([str(v) for v in command])
        return dict(kind=kind, name=name, definition_path=str(path), run_user=run_user,
                    command=reader.redactor.text(command) if isinstance(command, str) else None, enabled=None)
    except (ValueError, UnicodeError, ET.ParseError, plistlib.InvalidFileException, AttributeError) as exc:
        reader.risk('service_unreadable', path, exc)
        return None


def _probe(service):
    kind, name = service['kind'], service['name']
    # 名称必须是安装器定义的名称；文件内容不能注入任意管理器参数。
    if kind == 'systemd' and name in ('agent-collab-controller.service', 'agent-collab-worker.service'):
        command = ['systemctl'] + (['--user'] if name.endswith('worker.service') else [])
        command += ['show', name, '--property=UnitFileState', '--value']
    elif kind == 'launchd' and name in ('org.agent-collab.controller', 'org.agent-collab.worker') and hasattr(os, 'getuid'):
        command = ['launchctl', 'print-disabled', 'gui/%s' % os.getuid()]
    elif kind == 'schtasks' and name in ('agent-collab-controller', 'agent-collab-worker'):
        command = ['schtasks', '/query', '/tn', name, '/xml']
    else:
        return None
    try:
        # 同 adapters.commands.NO_WINDOW；这里不导入它：ECS 预检会把本文件配上主机已装的旧版包执行。
        flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=False, **flags)
        if result.returncode != 0:
            return None
        if kind == 'systemd':
            value = result.stdout.decode('utf-8').strip()
            return True if value in ('enabled', 'enabled-runtime') else False if value in ('disabled', 'masked', 'masked-runtime') else None
        if kind == 'schtasks':
            value = ET.fromstring(result.stdout).findtext('.//{*}Settings/{*}Enabled')
            return {'true': True, 'false': False}.get(value)
        text = result.stdout.decode('utf-8')
        if 'disabled services' not in text:
            return None
        match = re.search(r'"' + re.escape(name) + r'"\s*=>\s*(true|false)', text)
        return match.group(1) == 'false' if match else True
    except (OSError, subprocess.SubprocessError, ValueError, ET.ParseError):
        return None


def _installation(root, reader, probe, user):
    record = reader.json(root / 'installation.json')
    config = _object(_object(record).get('config'))
    current = None
    pointer = root / 'current'
    try:
        if pointer.is_symlink():
            target = os.readlink(str(pointer))
            if target.startswith('versions/') and len(Path(target).parts) == 2 and Path(target).name not in ('.', '..'):
                current = Path(target).name
            else:
                reader.risk('invalid_current_pointer', pointer)
        elif (root / 'current.txt').exists():
            raw = reader.read(root / 'current.txt')
            current = raw.decode('utf-8').strip() if raw is not None else None
            if current and (current in ('.', '..') or '/' in current or '\\' in current):
                reader.risk('invalid_current_pointer', root / 'current.txt')
                current = None
    except (OSError, UnicodeError) as exc:
        reader.risk('invalid_current_pointer', pointer, exc)
    services = []
    for path in reader.children(root / 'services'):
        if re.fullmatch(r'agent-collab-(controller|worker)\.(service|plist|xml)', path.name):
            service = _service(path, reader, user)
            if service:
                if probe:
                    service['enabled'] = _probe(service)
                services.append(service)
    if config.get('service') and not services:
        reader.risk('service_definitions_missing', root / 'services')
    return dict(root=str(root), installation=record, current_version=current,
                versions=[p.name for p in reader.children(root / 'versions') if p.is_dir() and not p.is_symlink()],
                services=services)


def _legacy(root, reader):
    statuses = Counter()
    active = []
    risk_count = len(reader.risks)
    if not (root / 'tasks').is_dir():
        reader.risk('legacy_tasks_unknown', root)
    for environment in reader.children(root / 'tasks'):
        if not environment.is_dir() or environment.is_symlink():
            continue
        for directory in reader.children(environment):
            if not directory.is_dir() or directory.is_symlink():
                continue
            state = reader.json(directory / 'state.json') or {}
            status = state.get('status')
            if not isinstance(status, str) or not status:
                status = 'unknown'
            statuses[status] += 1
            if status not in TERMINAL | {'succeeded', 'finished', 'ready_for_review', 'blocked', 'waiting_input'}:
                active.append(dict(task_id=state.get('task_id', directory.name), environment=environment.name, status=status))
    if len(reader.risks) != risk_count:
        reader.risk('legacy_tasks_unknown', root)
    return dict(state_root=str(root), tasks=dict(total=sum(statuses.values()), by_status=dict(statuses)),
                active_or_unknown=active)


def _reference_kinds(line):
    result = []
    for kind, pattern in (
        ('launcher', r'launcher\.py|codex-collaborate'),
        ('skill', r'skills[/\\].*SKILL\.md|\bSkill\b|\$[A-Za-z][\w-]*|codex-collaborate'),
        ('workbench', r'workbench\s+exec'),
        ('smb', r'\\\\[^\s\\]+\\|smb://|/Volumes/|(?<!:)//[^\s/]+/'),
        ('web_console', r'ac_session|/session|控制台.*登录|登录.*控制台'),
        ('admin_token', r'admin\.token|--token-file'),
    ):
        if re.search(pattern, line, re.I):
            result.append(kind)
    return result


def _references(paths, reader):
    references, seen = [], set()
    for root in paths:
        if not root.exists():
            reader.missing.append(dict(code='scan_path', detail=str(root)))
        for path in reader.files(root):
            if path in seen or _secret(path):
                continue
            seen.add(path)
            raw = reader.read(path, MAX_SCAN_BYTES)
            if raw is None:
                continue
            try:
                text = raw.decode('utf-8-sig')
            except UnicodeError:
                continue
            if any(ord(c) < 32 and c not in '\n\r\t' for c in text):
                continue
            try:
                reader.redactor.json(json.loads(text))
            except ValueError:
                pass
            except RecursionError as exc:
                # 嵌套过深、取不全其中的凭据值：不逐行输出这个文件。
                reader.risk('json_unreadable', path, exc)
                continue
            private_block = False
            for number, line in enumerate(text.splitlines(), 1):
                if '-----BEGIN ' in line:
                    private_block = True
                kinds = _reference_kinds(line)
                safe = REDACTED if private_block else reader.redactor.text(line)
                for kind in kinds:
                    references.append(dict(file=str(path), line=number, kind=kind, text=safe))
                if '-----END ' in line:
                    private_block = False
    return references


def _host():
    # platform.platform()/architecture() 在某些系统会调用外部程序；不使用环境变量取用户名。
    user = None
    try:
        if os.name == 'posix':
            import pwd
            user = pwd.getpwuid(os.getuid()).pw_name
        else:
            user = os.getlogin()
    except (OSError, KeyError):
        pass
    return dict(os=platform.system(), arch=platform.machine(), python=platform.python_version(),
                user=user, hostname=socket.gethostname())


def _plan(report):
    plan = {key: [] for key in ('migrate', 'keep', 'archive', 'retire')}

    def add(bucket, what, where, why, entry):
        row = dict(what=what, where=where, why=why, new_entry=entry)
        if row not in plan[bucket]:
            plan[bucket].append(row)

    controller = report['controller']
    if controller:
        root = Path(controller['data_dir'])
        for name in ('controller.db', 'blobs', 'config.json'):
            if (root / name).exists():
                add('keep', name, str(root / name), '原位升级保留数据', 'agent-collab controller')
        if controller['backups']['sets']:
            add('keep', '备份集', controller['backups']['directory'], '保留恢复依据；盘点未验证可恢复性', 'agent-collab controller')
        if controller['node_credentials']['count']:
            add('keep', '已登记节点凭据', str(root / 'controller.db'), '保持节点身份，不重新配对', 'agent-collab agent')
        if controller['sessions']['count']:
            add('retire', '旧网页控制台登录', str(root / 'controller.db'), '改用独立客户端授权，核对后设置 web_console=false',
                '桌面端配对：controller client-code → POST /clients/pair')
    for node in report['node_installs']:
        if Path(node['data_dir']).exists():
            add('keep', '节点数据目录', node['data_dir'], '原位升级保留身份与 attempt 状态', 'agent-collab agent')
        for secret in node['secret_files']:
            if secret['present']:
                add('keep', '节点凭据文件', secret['path'], '原机器原用户保留，不读取凭据', 'agent-collab agent')
        if any(node['commands'].get(k) for k in ('codex_command', 'claude_command', 'codex_home')) or node['status']:
            add('keep', '模型 CLI 登录状态', node['data_dir'], '留在原机器原用户；仅发现配置或状态，未验证登录', '原用户的 codex / claude CLI')
    for installation in report['installations']:
        for service in installation['services']:
            add('keep', '服务定义', service['definition_path'], '安装器管理的服务原位升级', 'agent-collab service')
    for binding in report['project_bindings']:
        if binding['path_kind'] in ('smb', 'unc'):
            add('migrate', '项目目录绑定', binding['target_path'], '共享目录交接需改为明确的节点执行位置', '节点本地路径 / 后续的文件夹导入')
    for launcher in report['legacy']['launchers']:
        if launcher['tasks']['total']:
            add('archive', '旧 launcher 任务历史', launcher['state_root'], '保留历史，不接管旧任务', '只读归档')
    for reference in report['legacy']['references']:
        kind = reference['kind']
        where = '%s:%s' % (reference['file'], reference['line'])
        if kind == 'admin_token':
            add('migrate', '管理令牌脚本引用', where, '为脚本签发可撤销的独立客户端凭据', '使用客户端凭据的 agent-collab CLI')
        elif kind in ('launcher', 'skill', 'workbench', 'web_console'):
            entry = ('桌面端配对：controller client-code → POST /clients/pair' if kind == 'web_console'
                     else 'agent-collab submit / agent-collab job')
            add('retire', kind, where, '停用旧入口，保留历史证据', entry)
    return plan


def inventory(controller_data_dir=None, node_data_dirs=(), install_roots=(), legacy_state_roots=(),
              scan_paths=(), probe_services=False, controller_db=None):
    """所有盘点位置均由调用者传入；不发现远端、不访问模型登录文件。"""
    snapshot = Path(controller_db).expanduser().resolve() if controller_db is not None else None
    if snapshot is not None and controller_data_dir is None:
        controller_data_dir = snapshot.parent  # 只有备份集（隔离副本）时，以快照所在目录为数据目录
    reader = _Reader()
    now = datetime.now(timezone.utc)
    report = dict(schema=1, generated_at=now.isoformat().replace('+00:00', 'Z'), read_only=True,
                  host=_host(), controller=None, node_installs=[], installations=[], project_bindings=[],
                  legacy=dict(launchers=[], references=[]), plan={}, conflicts=[], risks=reader.risks,
                  missing=reader.missing, cutover_preconditions=[])
    if controller_data_dir is not None:
        root = Path(controller_data_dir).expanduser().resolve()
        report['controller'], report['project_bindings'] = _controller(root, reader, now, snapshot)
    else:
        reader.missing.extend([dict(code='controller_data_dir', detail='未提供 controller 数据目录'),
                               dict(code='backup', detail='未提供 controller 数据目录，无法盘点备份')])
    for paths, key, collect in (
        (node_data_dirs, 'node_installs', lambda p: _node(p, reader)),
        (install_roots, 'installations', lambda p: _installation(p, reader, probe_services, report['host']['user'])),
        (legacy_state_roots, 'launchers', lambda p: _legacy(p, reader)),
    ):
        target = report['legacy'][key] if key == 'launchers' else report[key]
        for root in sorted({Path(p).expanduser().resolve() for p in paths}):
            if not root.is_dir():
                reader.missing.append(dict(code=key + '_directory', detail=str(root)))
            target.append(collect(root))
    report['legacy']['references'] = _references([Path(p).expanduser().absolute() for p in scan_paths], reader)
    controller = report['controller']
    meta = _object(controller and controller['meta'])
    seen = set()
    for node in report['node_installs']:
        ident = node['node_id']
        if isinstance(ident, str) and ident and ident in seen:
            report['conflicts'].append(dict(code='duplicate_node_id', detail=node['data_dir']))
        if isinstance(ident, str):
            seen.add(ident)
        if node['controller_id'] and meta.get('controller_id') and node['controller_id'] != meta['controller_id']:
            report['conflicts'].append(dict(code='controller_identity_mismatch', detail=node['data_dir']))
        generation, authority = node['authority_generation'], meta.get('authority_generation')
        if type(generation) is int and type(authority) is int and generation > authority:
            report['conflicts'].append(dict(code='authority_generation_ahead', detail=node['data_dir']))
        if node['attempts']['unreported']:
            reader.risk('unreported_attempts', node['data_dir'])
    readable = bool(controller and controller['database']['readable'])
    conflicts = {item['code'] for item in report['conflicts']}
    node_ids_known = all(n['node_id'] for n in report['node_installs'])
    identities_known = bool(meta.get('controller_id')) and all(n['controller_id'] for n in report['node_installs'])
    legacy_known = not any(r['code'] == 'legacy_tasks_unknown' for r in reader.risks)
    legacy_active = any(l['active_or_unknown'] for l in report['legacy']['launchers'])
    checks = [
        ('no_active_attempts', not controller['attempts']['active'] if readable else None, 'controller 中没有活动 attempt'),
        ('no_non_terminal_jobs', not controller['jobs']['non_terminal'] if readable else None, 'controller 中任务均已终结'),
        ('dispatch_paused', meta.get('dispatch_paused') if type(meta.get('dispatch_paused')) is bool else None, '已暂停派单'),
        ('backup_present', bool(controller['backups']['sets']) if controller else None, '存在备份集；未验证完整性与恢复'),
        ('single_controller_identity', False if 'controller_identity_mismatch' in conflicts else True if identities_known else None,
         '所提供节点与 controller 身份一致'),
        ('no_duplicate_node_ids', False if 'duplicate_node_id' in conflicts else True if node_ids_known else None, '所提供节点 ID 无重复'),
        # 已排空或已退役的节点不会再领任务（如早已停用但记录还在的节点），不让它们卡住这一项。
        ('nodes_declare_models', all(n['models_capability'] for n in controller['nodes'] if not n['drain'] and not n['retired'])
         if readable else None, '仍参与运行（未排空、未退役）的节点全部声明 models 能力'),
        ('legacy_tasks_idle', False if legacy_active else True if legacy_known else None,
         '所提供旧 launcher 无运行或未知任务；不验证进程存活'),
    ]
    report['cutover_preconditions'] = [dict(id=ident, ok=ok, detail=detail) for ident, ok, detail in checks]
    report['plan'] = _plan(report)
    report = reader.redactor.strings(report)
    # 放在全部来源读完之后：环境块可能出现在比引用行更晚读取的文件里。
    for installation in report['installations']:
        for service in installation['services']:
            if isinstance(service.get('command'), str):
                service['command'] = reader.redactor.free_text(service['command'])
    for reference in report['legacy']['references']:
        reference['text'] = reader.redactor.free_text(reference['text'])[:200]
    return report


def _validate_output(output, directories, scans):
    resolved = output.expanduser().resolve()
    roots = [Path(p).expanduser().resolve() for p in directories]
    lexical_roots = [Path(os.path.abspath(Path(p).expanduser())) for p in directories]
    scan_files = []
    for p in scans:
        path = Path(p).expanduser().resolve()
        (roots if path.is_dir() else scan_files).append(path)
        if path.is_dir():
            lexical_roots.append(Path(os.path.abspath(Path(p).expanduser())))
    lexical = Path(os.path.abspath(output.expanduser()))
    if (any(resolved == root or root in resolved.parents for root in roots) or resolved in scan_files
            or any(lexical == root or root in lexical.parents for root in lexical_roots)):
        raise ValueError('报告路径不得位于被检查目录内或覆盖被扫描文件')
    # 已存在的硬链接同样会覆盖被检查对象。
    if resolved.exists():
        reader = _Reader()
        for root in roots + scan_files:
            for path in reader.files(root):
                if os.path.samefile(str(resolved), str(path)):
                    raise ValueError('报告路径与被检查文件指向同一对象')
    return resolved


def main(argv=None):
    parser = argparse.ArgumentParser(description='只读盘点迁移对象；不连接 controller、不读取凭据内容')
    parser.add_argument('--controller-data-dir', metavar='PATH', help='controller 数据目录')
    parser.add_argument('--controller-db', metavar='PATH',
                        help='一致的数据库快照（如 controller backup 生成的备份集里的 .db）；controller 运行中时用它读取记录')
    for flag, help_text in (('node-data-dir', '节点数据目录'), ('install-root', '安装根'),
                            ('legacy-state-root', '旧 launcher 状态根'), ('scan', '扫描文本文件或目录')):
        parser.add_argument('--' + flag, action='append', default=[], metavar='PATH', help=help_text + '（可重复）')
    parser.add_argument('--probe-services', action='store_true', help='允许一次只读服务管理器查询，失败为 null')
    parser.add_argument('--out', metavar='PATH', help='报告文件，缺省输出到标准输出')
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code
    try:
        directories = args.node_data_dir + args.install_root + args.legacy_state_root
        if args.controller_db:
            directories += [Path(args.controller_db).expanduser().resolve().parent]
        if args.controller_data_dir:
            root = Path(args.controller_data_dir).expanduser().resolve()
            directories += [args.controller_data_dir, root.parent / 'backups']
        output = _validate_output(Path(args.out), directories, args.scan) if args.out else None
        report = inventory(args.controller_data_dir, args.node_data_dir, args.install_root,
                           args.legacy_state_root, args.scan, args.probe_services, args.controller_db)
        text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        if output:
            with output.open('w', encoding='utf-8') as stream:
                stream.write(text)
        else:
            sys.stdout.write(text)
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        # 不输出异常原文，路径或 JSON 错误有可能携带凭据。
        parser.print_usage(sys.stderr)
        print('参数或报告路径无效：' + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
