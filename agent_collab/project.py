"""本机项目目录的输入快照与成果应用（docs/protocol.md §13.2–§13.4）。

只做本机文件工作，不访问网络。CLI 直接调用这里的函数；桌面端的外壳把同一份代码作为随包的本机助手运行
（`agent-collab project local ...`），上传与下载仍由持有凭据的一方完成。扫描、排除、冲突判断与写入规则只有这一份。

- scan：把一个目录读成 manifest。不跟随符号链接与重解析点，不读选定目录之外的任何东西；读取期间变化的文件会被发现，
  不稳定的目录不产生快照。
- stage：把快照里指定的文件按内容哈希复制到暂存目录，内容与快照不符的如实报告（源文件已变）。
- plan / apply：按任务的输入基线、成果和目录当前内容逐文件比较。只动成果相对基线改过的路径；本机改过的文件算冲突，不覆盖。
  写入前备份，逐文件原子替换并记日志；整批写入不是跨文件的原子事务，中断后由 recover 报告并恢复到明确状态。
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time
import unicodedata
import uuid

from . import protocol as p
from .adapters.commands import NO_WINDOW
from .artifacts import DEFAULT_EXCLUDES, atomic_write, validate_excludes, workspace_excluded

# 项目输入的缺省排除：版本库元数据、依赖与构建缓存、常见的凭据文件。用户可以在界面或命令行调整。
PROJECT_EXCLUDES = DEFAULT_EXCLUDES + [
    '.git/**', '.hg/**', '.svn/**',
    'node_modules/**', '.venv/**', 'venv/**', '.tox/**', '.mypy_cache/**', '.ruff_cache/**', '.gradle/**', '.next/**', '.turbo/**',
    '.env', '.env.*', '*.pem', '*.key', '*.p12', '*.pfx', 'id_rsa', 'id_ed25519', '.netrc', '.npmrc', '.pypirc', '*.token',
    'Thumbs.db']
CHUNK = 1024 * 1024
REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT：Windows 的目录联接、符号链接与其他重解析点


class ProjectError(Exception):
    def __init__(self, code, message, **detail):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail

    def body(self):
        return {'code': self.code, 'message': self.message, 'detail': self.detail}


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def is_link(path, status=None):
    """符号链接或 Windows 重解析点（含目录联接）。它们可能指向选定目录之外，一律不跟随。"""
    status = status or os.lstat(str(path))
    return stat.S_ISLNK(status.st_mode) or bool(getattr(status, 'st_file_attributes', 0) & REPARSE_POINT)


def resolve_root(root):
    """用户选定的目录。它本身可以是链接（用户明确选了它），解析成真实路径后，其内部不再跟随任何链接。"""
    path = Path(root).expanduser()
    try:
        path = path.resolve(strict=True)
    except OSError:
        raise ProjectError('not_a_directory', '目录不存在或无法访问：%s' % root) from None
    if not path.is_dir():
        raise ProjectError('not_a_directory', '不是目录：%s' % root)
    return path


def _walk(root, patterns):
    """按名称顺序遍历，产出 (kind, 本机相对路径, manifest 路径, lstat)。kind 为 file、excluded 或 skip:<原因>。

    manifest 路径是 NFC 形式的 POSIX 相对路径；本机文件名不是 NFC 时两者不同。被排除或跳过的目录不再进入。
    """
    pending = ['']
    while pending:
        folder = pending.pop()
        try:
            entries = sorted(os.scandir(str(root / folder) if folder else str(root)), key=lambda e: e.name)
        except OSError:
            yield 'skip:unreadable', folder, unicodedata.normalize('NFC', folder), None
            continue
        folders = []
        for entry in entries:
            local = folder + '/' + entry.name if folder else entry.name
            name = unicodedata.normalize('NFC', local)
            try:
                status = entry.stat(follow_symlinks=False)
            except OSError:
                yield 'skip:unreadable', local, name, None
                continue
            if is_link(entry.path, status):
                yield 'skip:link', local, name, status
                continue
            if workspace_excluded(name, patterns):
                yield 'excluded', local, name, status
                continue
            try:
                p.check_rel_path(name)
            except ValueError:
                # Windows 无法表示的名称（保留设备名、以点或空格结尾、含 : 等）：执行节点可能是 Windows，带不过去。
                yield 'skip:name_not_portable', local, name, status
                continue
            if stat.S_ISDIR(status.st_mode):
                folders.append(local)
            elif stat.S_ISREG(status.st_mode):
                yield 'file', local, name, status
            else:
                yield 'skip:special_file', local, name, status
        pending.extend(reversed(folders))


def _hash_stable(path, status):
    """读取并计算哈希；读取前后文件的大小或修改时间变了就重读，三次仍不稳定返回 None。

    返回 (哈希, 大小, 修改时间)：后两项是读完那一刻看到的，之后的复核拿它们比对，读完之后再发生的改动不会被漏掉。
    """
    for _ in range(3):
        digest, size = hashlib.sha256(), 0
        with open(str(path), 'rb') as source:
            while True:
                chunk = source.read(CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        after = os.lstat(str(path))
        if (after.st_size, after.st_mtime_ns) == (status.st_size, status.st_mtime_ns) and size == after.st_size:
            return digest.hexdigest(), size, after.st_mtime_ns
        status = after
    return None


def git_info(root):
    """Git 项目的分支、提交与未提交改动数；不是 Git 项目或取不到时返回 None。只读，不改动仓库。"""
    if not (root / '.git').exists():
        return None
    git = shutil.which('git')
    if not git:
        return {'available': False}
    # 不运行仓库配置里的 fsmonitor 钩子，不写索引锁：选中的目录不一定可信，查询不应该执行其中配置的程序。
    base = [git, '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false', '-C', str(root)]
    env = dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')

    def run(*args):
        try:
            # 桌面端的本机助手由外壳以无控制台方式启动：git 是控制台程序，不带 CREATE_NO_WINDOW 会各自闪出一个窗口。
            done = subprocess.run(base + list(args), capture_output=True, text=True, encoding='utf-8', errors='replace',
                                  timeout=30, env=env, stdin=subprocess.DEVNULL, **NO_WINDOW)
            return done.stdout.strip() if done.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    status = run('status', '--porcelain')
    # symbolic-ref 对还没有提交的新仓库也给得出分支名；游离 HEAD 时没有分支，只有提交。
    return {'available': True, 'branch': run('symbolic-ref', '--short', '-q', 'HEAD'), 'commit': run('rev-parse', '-q', '--verify', 'HEAD'),
            'uncommitted': None if status is None else len([line for line in status.splitlines() if line.strip()])}


def scan(root, exclude=None, max_file_bytes=None, previous=None, progress=None, cancelled=None):
    """把目录读成输入快照。返回 dict；目录读不成稳定快照时抛 ProjectError。

    previous：上一份 manifest（可缺省）。Windows 上看不到可执行位，沿用其中同路径文件的标记。
    progress(dict) 与 cancelled() -> bool 可缺省。
    """
    root = resolve_root(root)
    patterns = validate_excludes(list(PROJECT_EXCLUDES if exclude is None else exclude))
    inherited = {f['path']: f['executable'] for f in (previous or {}).get('files', [])}
    for attempt in range(3):
        result = _scan_once(root, patterns, max_file_bytes, inherited, progress, cancelled)
        # 再走一遍目录，只比对名称、大小与修改时间：扫描期间新增、删除或改动的文件在这里被发现。
        seen = {name: (status.st_size, status.st_mtime_ns) for kind, _, name, status in _walk(root, patterns) if kind == 'file'}
        expected = dict(result['stamps'], **{name: tuple(stamp) for name, stamp in result['unread'].items()})
        changed = sorted(name for name in set(seen) | set(expected) if seen.get(name) != tuple(expected.get(name, ())))
        changed = sorted(set(changed) | set(result['unstable']))
        if not changed:
            del result['unread'], result['unstable']
            return result
        if progress:
            progress({'phase': 'rescan', 'attempt': attempt + 1, 'changed': len(changed)})
    raise ProjectError('unstable', '目录在扫描期间一直在变化，没有生成快照；停止正在写入它的程序，或把这些路径加入排除规则后重试',
                       paths=changed[:50], count=len(changed))


def _scan_once(root, patterns, max_file_bytes, inherited, progress, cancelled):
    files, stamps, locations, unread, unstable = [], {}, {}, {}, []
    skipped, counts, excluded, total = [], {}, {'count': 0, 'top': []}, 0
    for kind, local, name, status in _walk(root, patterns):
        if cancelled and cancelled():
            raise ProjectError('cancelled', '扫描已取消')
        if kind == 'excluded':
            excluded['count'] += 1
            if len(excluded['top']) < 200:
                excluded['top'].append(name + ('/' if stat.S_ISDIR(status.st_mode) else ''))
            continue
        reason = kind[5:] if kind.startswith('skip:') else None
        if reason is None and max_file_bytes is not None and status.st_size > max_file_bytes:
            reason = 'too_large'
        if reason is None:
            try:
                hashed = _hash_stable(root / local, status)
            except OSError:
                reason = 'unreadable'
            else:
                if hashed is None:
                    unstable.append(name)
                    continue
                if name in stamps:
                    raise ProjectError('unicode_conflict', '两个文件名规范化后相同，无法同时放进一份输入：%s' % name, path=name)
                stamps[name] = (hashed[1], hashed[2])
                executable = inherited.get(name, False) if os.name == 'nt' else bool(status.st_mode & 0o111)
                files.append({'path': name, 'sha256': hashed[0], 'size': hashed[1], 'executable': executable})
                if local != name:
                    locations[name] = local
                total += hashed[1]
                if progress and len(files) % 200 == 0:
                    progress({'phase': 'scan', 'files': len(files), 'bytes': total})
                continue
        if kind == 'file':
            unread[name] = (status.st_size, status.st_mtime_ns)  # 没读内容的普通文件：复核时只比对它没有再变
        counts[reason] = counts.get(reason, 0) + 1
        if len(skipped) < 500:
            skipped.append({'path': name, 'reason': reason})
    try:
        manifest = p.validate_manifest({'version': 1, 'base': None, 'files': files, 'deleted': []})
    except ValueError as exc:
        # 仅大小写不同的两个名称（区分大小写的卷上才会出现）：Windows 与 macOS 的缺省文件系统放不下，需要排除其一。
        raise ProjectError('path_conflict', '目录里有无法同时表示的路径：%s' % exc) from None
    return {'root': str(root), 'manifest': manifest, 'manifest_sha256': p.manifest_sha256(manifest), 'files': len(files), 'bytes': total,
            'manifest_bytes': len(p.canonical_json(manifest)), 'exclude': patterns, 'excluded': excluded,
            'skipped': skipped, 'skipped_counts': counts, 'git': git_info(root), 'locations': locations,
            'stamps': {name: list(stamp) for name, stamp in stamps.items()}, 'scanned_at': now(),
            'unread': unread, 'unstable': unstable}


# ---------------------------------------------------------------- 快照的保存与取用

def save_snapshot(state_dir, result, project_id=None):
    """把扫描结果存成本机快照记录，返回 snapshot_id。记录里有本机路径，只留在这台机器上。"""
    snapshot_id = str(uuid.uuid4())
    atomic_write(Path(state_dir) / 'snapshots' / (snapshot_id + '.json'),
                 json.dumps(dict(result, snapshot_id=snapshot_id, project_id=project_id), ensure_ascii=False).encode('utf-8'))
    return snapshot_id


def load_snapshot(state_dir, snapshot_id):
    p.check_uuid(snapshot_id, 'snapshot_id')
    try:
        return json.loads((Path(state_dir) / 'snapshots' / (snapshot_id + '.json')).read_text(encoding='utf-8'))
    except OSError:
        raise ProjectError('no_snapshot', '没有这份本机快照：%s' % snapshot_id) from None


def local_path(root, name, locations=None, must_exist=False):
    """manifest 路径在选定目录里的位置。任何一级是链接、不是目录，或名称只在大小写或规范化形式上与现有条目不同，都拒绝。"""
    p.check_rel_path(name)
    root = Path(root)
    mapped = (locations or {}).get(name)
    parts, current = (mapped or name).split('/'), root
    nfc = lambda value: unicodedata.normalize('NFC', value)
    for index, part in enumerate(parts):
        try:
            names = os.listdir(str(current))
        except NotADirectoryError:
            raise ProjectError('unsafe_path', '上级不是目录：%s' % name, path=name) from None
        except FileNotFoundError:
            names = []
        if part not in names:
            same_form = [n for n in names if nfc(n) == nfc(part)]
            if len(same_form) == 1:
                part = same_form[0]  # 本机文件名不是 NFC 形式：按它实际的名称访问
            elif same_form or any(nfc(n).casefold() == nfc(part).casefold() for n in names):
                # 现有条目与目标只差大小写：在大小写不敏感的卷上它们是同一个文件，不能当成新文件写。
                raise ProjectError('unsafe_path', '已有仅大小写或写法不同的同名条目：%s' % name, path=name)
        current = current / part
        try:
            status = os.lstat(str(current))
        except FileNotFoundError:
            status = None
        if status is not None:
            if is_link(current, status):
                raise ProjectError('unsafe_path', '路径经过链接：%s' % name, path=name)
            if index < len(parts) - 1 and not stat.S_ISDIR(status.st_mode):
                raise ProjectError('unsafe_path', '上级不是目录：%s' % name, path=name)
    if must_exist and not current.is_file():
        raise ProjectError('source_changed', '文件已不存在：%s' % name, path=name)
    return current


def file_hash(path):
    digest = hashlib.sha256()
    with open(str(path), 'rb') as source:
        for chunk in iter(lambda: source.read(CHUNK), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read_verified(snapshot, entry):
    """读出快照里的一个文件；内容与快照记录的哈希不符说明源文件在扫描后变了，这份快照不能再用它。"""
    path = local_path(snapshot['root'], entry['path'], snapshot.get('locations'), must_exist=True)
    data = path.read_bytes()
    if len(data) != entry['size'] or p.sha256_bytes(data) != entry['sha256']:
        raise ProjectError('source_changed', '文件在扫描后被修改，这份快照已不能代表它：%s' % entry['path'], path=entry['path'])
    return data


def stage(snapshot, wanted, out_dir, progress=None, cancelled=None):
    """把快照里哈希在 wanted 中的文件复制到 out_dir/<sha256>。返回 {'staged': [...], 'changed': [路径...]}。

    暂存的内容经过哈希核对，之后源文件再变也不影响已暂存的副本。
    """
    # ponytail: 暂存要占用与待上传内容同样大小的磁盘；首次上传整个项目时最明显。要省空间可改成由调用方流式读取并核对。
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    wanted, staged, changed, done = set(wanted), [], [], set()
    for entry in snapshot['manifest']['files']:
        if entry['sha256'] not in wanted or entry['sha256'] in done:
            continue
        if cancelled and cancelled():
            raise ProjectError('cancelled', '已取消')
        target = out / p.check_sha256(entry['sha256'])
        try:
            if not (target.is_file() and file_hash(target) == entry['sha256']):
                atomic_write(target, read_verified(snapshot, entry))
            staged.append(entry['sha256'])
            done.add(entry['sha256'])
        except ProjectError as exc:
            if exc.code not in ('source_changed', 'unsafe_path'):
                raise
            changed.append(entry['path'])
        except OSError:
            changed.append(entry['path'])
        if progress:
            progress({'phase': 'stage', 'done': len(staged), 'total': len(wanted)})
    return {'staged': staged, 'changed': changed}


# ---------------------------------------------------------------- 成果应用

def _state(path, entry_before, entry_after):
    """目录里这个路径当前的内容，相对任务输入（before）与成果（after）是什么关系。"""
    try:
        status = os.lstat(str(path))
    except FileNotFoundError:
        status = None
    if status is not None and not stat.S_ISREG(status.st_mode):
        return 'other', None
    digest = file_hash(path) if status is not None else None
    mode = None if status is None or os.name == 'nt' else bool(status.st_mode & 0o111)

    def same(entry):
        if entry is None:
            return digest is None
        return digest == entry['sha256'] and (mode is None or mode == entry['executable'])
    return ('result' if same(entry_after) else 'base' if same(entry_before) else 'absent' if digest is None else 'other'), digest


def plan(root, base_manifest, result_manifest, exclude=None, locations=None):
    """比较任务输入基线、成果与目录当前内容，给出逐文件的应用计划。不写任何东西。

    只列出成果相对基线新增、修改、删除的路径；目录里别的文件（包括任务期间新建的、被排除的）不在计划里，也就不会被动到。
    state：clean 可以应用；already 目录里已经是成果的内容；conflict 本机内容既不是基线也不是成果，保留本机内容。
    """
    root = resolve_root(root)
    base = p.validate_manifest(base_manifest or {'version': 1, 'base': None, 'files': [], 'deleted': []})
    result = p.validate_manifest(result_manifest)
    patterns = validate_excludes(list(DEFAULT_EXCLUDES if exclude is None else exclude))
    before, after = {f['path']: f for f in base['files']}, {f['path']: f for f in result['files']}
    diff = p.manifest_diff(base, result)
    changes = []
    for action, names in (('add', diff['added']), ('modify', diff['modified']), ('delete', diff['deleted'])):
        for name in names:
            if workspace_excluded(name, patterns):
                continue  # 被排除的路径不属于任务的输入输出范围：既不写也不删
            change = {'path': name, 'action': action, 'size': after[name]['size'] if name in after else 0}
            try:
                state, digest = _state(local_path(root, name, locations), before.get(name), after.get(name))
            except ProjectError as exc:
                change.update(state='conflict', reason=exc.code)
            except OSError as exc:
                change.update(state='conflict', reason='unreadable', error=type(exc).__name__)
            else:
                if state == 'result':
                    change['state'] = 'already'
                elif state == 'base':
                    change.update(state='clean', expect=digest)
                else:
                    reason = {'add': 'created_locally', 'modify': 'deleted_locally' if state == 'absent' else 'modified_locally',
                              'delete': 'modified_locally'}[action]
                    change.update(state='conflict', reason=reason)
            changes.append(change)
    changes.sort(key=lambda c: c['path'])
    summary = {key: sum(1 for c in changes if c['action'] == key) for key in ('add', 'modify', 'delete')}
    summary.update({key: sum(1 for c in changes if c['state'] == key) for key in ('clean', 'already', 'conflict')})
    summary['bytes'] = sum(c['size'] for c in changes if c['state'] == 'clean' and c['action'] != 'delete')
    return {'root': str(root), 'changes': changes, 'summary': summary,
            'base_manifest_sha256': p.manifest_sha256(base), 'manifest_sha256': p.manifest_sha256(result)}


def _journal_dir(state_dir, application_id):
    return Path(state_dir) / 'applies' / p.check_uuid(application_id, 'application_id')


def _root_marker(state_dir, root):
    return Path(state_dir) / 'applies' / 'roots' / (hashlib.sha256(str(root).encode('utf-8')).hexdigest() + '.json')


def _save(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False).encode('utf-8'))


def unfinished(state_dir, root=None):
    """写入开始后还没有得到最终结果的应用操作（进程被中断、窗口被关掉、机器掉电）。root 给出时只看这个目录的。"""
    found = []
    for marker in sorted((Path(state_dir) / 'applies' / 'roots').glob('*.json')):
        try:
            entry = json.loads(marker.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if root is None or entry['root'] == str(resolve_root(root)):
            found.append(entry)
    return found


def _write_one(root, change, after, fetch, journal, backup, locations):
    """写一个文件（或删除）。写前再核对一次内容仍是计划时看到的；变了就不动它。返回 None 或原因。"""
    name = change['path']
    path = local_path(root, name, locations)
    current = file_hash(path) if path.is_file() else None
    if current != change.get('expect'):
        return 'changed_since_plan'
    if current is not None:
        saved = backup / name
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(path), str(saved))
    if change['action'] == 'delete':
        path.unlink()
    else:
        entry = after[name]
        data = fetch(entry['sha256'])
        if len(data) != entry['size'] or p.sha256_bytes(data) != entry['sha256']:
            raise ProjectError('blob_mismatch', '成果内容的哈希不符：%s' % name, path=name)
        atomic_write(path, data, 0o755 if entry['executable'] else 0o644)
    with open(str(journal / 'done.log'), 'a', encoding='utf-8') as log:
        log.write(json.dumps({'path': name, 'action': change['action']}, ensure_ascii=False) + '\n')
        log.flush()
        os.fsync(log.fileno())
    return None


def apply(root, base_manifest, result_manifest, fetch, state_dir, application_id, exclude=None, locations=None,
          skip_conflicts=False, progress=None, cancelled=None, before_write=None, before_file=None):
    """把成果写进项目目录。返回结果 dict，status 为 applied、partial、conflict 或 failed。

    cancelled() -> bool：取消请求。写入开始之前被取消就什么都不写（抛 ProjectError cancelled）；写入期间被取消，
    当前文件照常写完（备份、原子替换、记日志），不再开始下一个文件，结果的 cancelled 为 true、未写的文件列在 pending，
    有已写入的文件时为 partial，一个都没写时为 failed。最后一个文件写完之后才到的取消不改变结果。取消不等于回滚。
    before_file(index)：开始写第 index 个文件（从 0 计）之前调用，给测试与故障注入设屏障；缺省不调用。

    fetch(sha256) -> bytes：取成果内容（调用方已下载并可核对）。写入前先确认所有内容都取得到、磁盘空间够用；
    有冲突时缺省什么都不写（conflict），skip_conflicts 时只写没有冲突的文件并如实返回 partial。
    before_write(plan)：在第一次写入之前调用（例如向 controller 登记应用意图）；它抛异常则不写。
    """
    root = resolve_root(root)
    pending = unfinished(state_dir, root)
    if pending:
        raise ProjectError('unfinished_application', '这个目录上有一次没有完成的应用操作，先用 recover 查看并处理它',
                           application_id=pending[0]['application_id'])
    outline = plan(root, base_manifest, result_manifest, exclude, locations)
    after = {f['path']: f for f in p.validate_manifest(result_manifest)['files']}
    conflicts = [c for c in outline['changes'] if c['state'] == 'conflict']
    todo = [c for c in outline['changes'] if c['state'] == 'clean']
    result = {'application_id': application_id, 'root': str(root), 'status': None, 'changed': [], 'pending': [],
              'already': [c['path'] for c in outline['changes'] if c['state'] == 'already'],
              'conflicts': [{'path': c['path'], 'reason': c['reason']} for c in conflicts], 'errors': [], 'backup': None,
              'summary': outline['summary']}
    if conflicts and not skip_conflicts:
        return dict(result, status='conflict', pending=[c['path'] for c in todo])
    if not todo:
        return dict(result, status='applied' if not conflicts else 'conflict')
    # 任何写入之前：成果内容全部取得到并核对过，磁盘空间足够（新内容 + 被替换文件的备份）。
    needed = 0
    for change in todo:
        if cancelled and cancelled():
            raise ProjectError('cancelled', '已取消，没有写入任何文件')
        if change['action'] != 'delete':
            entry = after[change['path']]
            data = fetch(entry['sha256'])
            if len(data) != entry['size'] or p.sha256_bytes(data) != entry['sha256']:
                raise ProjectError('blob_mismatch', '成果内容的哈希不符：%s' % change['path'], path=change['path'])
            needed += entry['size']
        if change.get('expect'):
            needed += os.path.getsize(str(local_path(root, change['path'], locations)))
    free = shutil.disk_usage(str(root)).free
    if free < needed + 16 * 1024 * 1024:
        raise ProjectError('insufficient_space', '磁盘空间不足：需要约 %d 字节，可用 %d 字节' % (needed, free), needed=needed, free=free)
    if cancelled and cancelled():
        raise ProjectError('cancelled', '已取消，没有写入任何文件')
    if before_write:
        before_write(outline)
    journal = _journal_dir(state_dir, application_id)
    backup = journal / 'backup'
    backup.mkdir(parents=True, exist_ok=False)
    record = {'application_id': application_id, 'root': str(root), 'started_at': now(), 'status': 'writing',
              'changes': todo, 'conflicts': result['conflicts'], 'locations': locations or {},
              'base_manifest_sha256': outline['base_manifest_sha256'], 'manifest_sha256': outline['manifest_sha256'],
              'files': {c['path']: after.get(c['path']) for c in todo}}
    _save(journal / 'journal.json', record)
    _save(_root_marker(state_dir, root), {'application_id': application_id, 'root': str(root), 'started_at': record['started_at'],
                                         'journal': str(journal)})
    result['backup'] = str(backup)
    return _run(root, record, todo, after, fetch, journal, state_dir, result, progress, cancelled, before_file)


def _run(root, record, todo, after, fetch, journal, state_dir, result, progress, cancelled, before_file=None):
    """逐个文件写入。取消只在两个文件之间生效：正在写的文件总是完整地写完并记进日志。"""
    backup, stop = journal / 'backup', None
    for index, change in enumerate(todo):
        if before_file and not stop:
            before_file(index)
        if stop or (cancelled and cancelled()):
            stop = stop or 'cancelled'
            result['pending'].append(change['path'])
            continue
        try:
            reason = _write_one(root, change, after, fetch, journal, backup, record.get('locations'))
        except ProjectError as exc:
            reason, error = exc.code, exc.message
        except OSError as exc:
            reason, error = 'io_error', '%s: %s' % (type(exc).__name__, exc.strerror or exc)
            if exc.errno == 28:  # ENOSPC：磁盘写满，后面的也写不了
                stop = 'insufficient_space'
        else:
            error = None
        if reason is None:
            result['changed'].append(change['path'])
        elif reason == 'changed_since_plan':
            result['conflicts'].append({'path': change['path'], 'reason': reason})
        else:
            result['errors'].append({'path': change['path'], 'reason': reason, 'error': error})
        if progress:
            progress({'phase': 'write', 'done': index + 1, 'total': len(todo)})
    clean = not result['conflicts'] and not result['errors'] and not result['pending']
    result['status'] = 'applied' if clean else 'partial' if result['changed'] else 'conflict' if result['conflicts'] else 'failed'
    result['cancelled'] = stop == 'cancelled'
    record.update(status=result['status'], finished_at=now(),
                  result={k: result[k] for k in ('changed', 'pending', 'conflicts', 'errors', 'cancelled')})
    _save(journal / 'journal.json', record)
    _root_marker(state_dir, root).unlink()  # 有了最终结果：这个目录可以开始下一次应用
    return result


def recover(state_dir, application_id, action='status', fetch=None, progress=None, cancelled=None):
    """查看或处理一次应用操作，主要用于被中断的那些。

    status：逐文件核对目录现状，报告哪些已写入、哪些还没写、哪些在这之后又被改了；不改变任何东西。
    resume：把还没写的文件写完（需要 fetch）。rollback：把已写入且之后没有再被改动的文件恢复成备份里的原样。
    abandon：不再处理，按现状结束，结果记为 unknown。
    """
    journal = _journal_dir(state_dir, application_id)
    try:
        record = json.loads((journal / 'journal.json').read_text(encoding='utf-8'))
    except OSError:
        raise ProjectError('no_application', '本机没有这次应用操作的记录：%s' % application_id) from None
    root, backup, locations = Path(record['root']), journal / 'backup', record.get('locations')
    files, written, remaining, diverged = record['files'], [], [], []
    for change in record['changes']:
        name, entry = change['path'], files.get(change['path'])
        try:
            path = local_path(root, name, locations)
            current = file_hash(path) if path.is_file() else None
        except (ProjectError, OSError):
            diverged.append(name)
            continue
        target = entry['sha256'] if entry and change['action'] != 'delete' else None
        if current == target:
            written.append(name)
        elif current == change.get('expect'):
            remaining.append(name)
        else:
            diverged.append(name)
    report = {'application_id': application_id, 'root': str(root), 'status': record['status'], 'interrupted': record['status'] == 'writing',
              'written': written, 'remaining': remaining, 'diverged': diverged, 'backup': str(backup)}
    if action == 'status':
        return report
    marker = _root_marker(state_dir, root)
    if action == 'resume':
        if fetch is None:
            raise ProjectError('invalid', 'resume 需要成果内容')
        todo = [c for c in record['changes'] if c['path'] in remaining]
        after = {name: entry for name, entry in files.items() if entry}
        result = {'application_id': application_id, 'root': str(root), 'status': None, 'changed': list(written), 'pending': [],
                  'already': [], 'conflicts': [{'path': name, 'reason': 'changed_since_plan'} for name in diverged] + record.get('conflicts', []),
                  'errors': [], 'backup': str(backup)}
        if not marker.exists():
            _save(marker, {'application_id': application_id, 'root': str(root), 'started_at': record['started_at'], 'journal': str(journal)})
        return _run(root, record, todo, after, fetch, journal, state_dir, result, progress, cancelled)
    if action == 'rollback':
        restored, kept = [], list(diverged)
        for change in record['changes']:
            name = change['path']
            if name not in written:
                continue
            path, saved = local_path(root, name, locations), backup / name
            try:
                if saved.is_file():
                    atomic_write(path, saved.read_bytes(), stat.S_IMODE(os.lstat(str(saved)).st_mode) if os.name != 'nt' else 0o644)
                elif path.is_file():
                    path.unlink()  # 这次应用新建的文件
                restored.append(name)
            except OSError:
                kept.append(name)
        status = 'rolled_back' if not kept else 'partial'
        record.update(status=status, finished_at=now(), result={'restored': restored, 'kept': kept})
    elif action == 'abandon':
        status = 'unknown'
        record.update(status=status, finished_at=now(), result={'written': written, 'remaining': remaining, 'diverged': diverged})
    else:
        raise ProjectError('invalid', 'action 只能是 status、resume、rollback 或 abandon')
    _save(journal / 'journal.json', record)
    if marker.exists():
        marker.unlink()
    return dict(report, status=status, **record['result'])
