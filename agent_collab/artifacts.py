"""Byte-preserving workspaces and conservative, backed-up integration (no Git)."""
import hmac
from fnmatch import fnmatchcase
from functools import lru_cache
import os
from pathlib import Path
import shutil
import stat
import uuid

from . import protocol as p


EMPTY = {'version': 1, 'base': None, 'files': [], 'deleted': []}
DEFAULT_EXCLUDES = ['__pycache__/**', '*.pyc', '.pytest_cache/**', '.DS_Store']


def validate_excludes(patterns):
    if not isinstance(patterns, list) or not all(isinstance(x, str) for x in patterns):
        raise ValueError('workspace.exclude must be a string list')
    for pattern in patterns:
        if (not pattern or '\\' in pattern or ':' in pattern or
                any(ord(c) < 32 for c in pattern) or
                any(part in ('', '.', '..') for part in pattern.split('/'))):
            raise ValueError('workspace.exclude requires POSIX relative globs without ..')
    return patterns


def workspace_excluded(path, patterns):
    """Match at any directory depth; only ** crosses path components."""
    parts = path.split('/')
    for pattern in patterns:
        glob = pattern.split('/')

        @lru_cache(None)
        def match(i, j):
            if j == len(glob):
                return i == len(parts)
            if glob[j] == '**':
                return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
            return i < len(parts) and fnmatchcase(parts[i], glob[j]) and match(i + 1, j + 1)

        if any(match(i, 0) for i in range(len(parts))):
            return True
    return False


def atomic_write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temp), str(path))
    finally:
        if temp.exists():
            temp.unlink()


def checked_manifest(manifest):
    result = p.validate_manifest(manifest)
    # Shared validator does not reject case aliases in directory components.
    names = set()
    for item in result['files']:
        parts = item['path'].split('/')
        names.update('/'.join(parts[:i]) for i in range(1, len(parts) + 1))
    if p.case_collisions(sorted(names)):
        raise ValueError('case-conflicting paths')
    return result


def safe_path(root, relative):
    p.check_rel_path(relative)
    root = Path(root).absolute()
    for parent in [root] + list(root.parents):
        if parent.is_symlink():
            raise ValueError('symbolic link in workspace root')
    current = root
    for component in relative.split('/'):
        if current.exists() and not current.is_dir():
            raise ValueError('parent is not a directory: ' + relative)
        # Also refuse aliases on case-insensitive volumes.
        if current.is_dir():
            if any(x.name.casefold() == component.casefold() and x.name != component
                   for x in current.iterdir()):
                raise ValueError('case-conflicting path: ' + relative)
        current = current / component
        if current.is_symlink():
            raise ValueError('symbolic link: ' + relative)
    return current


def verified_blob(entry, fetch_blob):
    data = fetch_blob(entry['sha256'])
    if len(data) != entry['size'] or not hmac.compare_digest(p.sha256_bytes(data), entry['sha256']):
        raise ValueError('blob integrity mismatch: ' + entry['path'])
    return data


def materialize(target_dir, manifest, fetch_blob):
    manifest = checked_manifest(manifest)
    root = Path(target_dir)
    safe_path(root, '.probe')
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError('materialize requires an empty workspace')
    # Validate all bytes before creating any output file.
    content = [(entry, verified_blob(entry, fetch_blob)) for entry in manifest['files']]
    for entry, data in content:
        path = safe_path(root, entry['path'])
        atomic_write(path, data, 0o755 if entry['executable'] else 0o644)
    return manifest


def snapshot(work_dir, base_manifest=None, windows=None, exclude=None):
    """Return (complete manifest, hash -> bytes), including canonical manifest blob."""
    root = Path(work_dir)
    safe_path(root, '.probe')
    if not root.is_dir():
        raise ValueError('snapshot workspace does not exist')
    base = checked_manifest(base_manifest) if base_manifest is not None else None
    old = {f['path']: f for f in (base or EMPTY)['files']}
    patterns = validate_excludes(DEFAULT_EXCLUDES if exclude is None else exclude)
    windows = os.name == 'nt' if windows is None else windows
    files, blobs, names = [], {}, []
    for directory, dirs, filenames in os.walk(str(root), followlinks=False):
        for name in dirs + filenames:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if workspace_excluded(relative, patterns):
                continue
            p.check_rel_path(relative)
            names.append(relative)
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError('unsupported file (including symbolic link): ' + relative)
            if stat.S_ISREG(mode):
                data = path.read_bytes()
                sha = p.sha256_bytes(data)
                executable = old.get(relative, {}).get('executable', False) if windows else bool(mode & 0o111)
                files.append(dict(path=relative, sha256=sha, size=len(data), executable=executable))
                blobs[sha] = data
    if p.case_collisions(names):
        raise ValueError('case-conflicting paths')
    manifest = checked_manifest(dict(version=1, base=p.manifest_sha256(base) if base is not None else None,
                                    files=files, deleted=sorted(path for path in set(old) - {f['path'] for f in files}
                                                               if not workspace_excluded(path, patterns))))
    blobs[p.manifest_sha256(manifest)] = p.canonical_json(manifest)
    return manifest, blobs


def integrate(target_dir, base_manifest, result_manifest, fetch_blob, backup_dir, exclude=None):
    """Check only result changes against the baseline; backup originals first.

    The caller must hold exclusive ownership against concurrent human writers.
    Replacements are atomic per file, not a filesystem-wide transaction.
    """
    base = checked_manifest(base_manifest or EMPTY)
    result = checked_manifest(result_manifest)
    root, backup = Path(target_dir), Path(backup_dir)
    if root.resolve() == backup.resolve() or root.resolve() in backup.resolve().parents:
        raise ValueError('backup must be outside target workspace')
    before = {f['path']: f for f in base['files']}
    after = {f['path']: f for f in result['files']}
    diff = p.manifest_diff(base, result)
    # A snapshot omission must never become a deletion of excluded baseline files.
    patterns = validate_excludes(exclude or [])
    diff = {kind: [path for path in paths if not workspace_excluded(path, patterns)]
            for kind, paths in diff.items()}
    changed = sorted(diff['added'] + diff['modified'] + diff['deleted'])
    conflicts, errors = [], []
    for relative in changed:
        try:
            path = safe_path(root, relative)
            entry = before.get(relative)
            if entry is None:
                if path.exists():
                    conflicts.append(relative)
            elif not path.is_file() or not hmac.compare_digest(p.sha256_bytes(path.read_bytes()), entry['sha256']):
                conflicts.append(relative)
        except (ValueError, OSError) as exc:
            # 无法检查（权限、路径不安全）不等于内容冲突：如实记录原因，同样不写入。
            conflicts.append(relative)
            errors.append({'path': relative, 'error': type(exc).__name__ + ': ' + (exc.strerror or str(exc) if isinstance(exc, OSError) else str(exc))})
    if conflicts:
        return dict(status='conflict', changed=[], conflicts=conflicts, backup=None, errors=errors)
    content = {name: verified_blob(after[name], fetch_blob) for name in changed if name in after}
    if backup.exists() and any(backup.iterdir()):
        raise ValueError('backup directory is not empty')
    safe_path(backup, '.probe')
    backup.mkdir(parents=True, exist_ok=True)
    for name in changed:
        if name in before:
            original = safe_path(root, name)
            destination = safe_path(backup, name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(original), str(destination))
    # All conflicts, blob failures and backups precede the first target mutation.
    for name in diff['deleted']:
        safe_path(root, name).unlink()
    for name in diff['added'] + diff['modified']:
        atomic_write(safe_path(root, name), content[name], 0o755 if after[name]['executable'] else 0o644)
    return dict(status='integrated', changed=changed, conflicts=[], backup=str(backup), errors=[])
