"""Scheduled consistent snapshots and verified, restartable off-instance pulls."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

from . import domain as d, protocol as p
from .blobstore import BlobStore
from .controller import ControllerLock, backup, database_references, file_hash, sync_directory
from .store import SCHEMA


LOCKED_EXIT = 75
COMPLETE_KEYS = ('id', 'created_at', 'tar_name', 'tar_sha256', 'tar_bytes',
                 'db_sha256', 'authority_generation')


class BackupBusy(RuntimeError):
    pass


class BackupError(ValueError):
    """Diagnostic rejection shared by maintenance HTTP and offline commands."""
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details

    def result(self):
        return dict(code=self.code, message=str(self), details=self.details)


def schema_info(db):
    rows = [tuple(row) for row in db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]
    return dict(user_version=db.execute('PRAGMA user_version').fetchone()[0],
                fingerprint=p.sha256_bytes(json.dumps(rows, separators=(',', ':')).encode()))


def payload_manifest(source):
    manifest = read_json(Path(source) / 'backup-manifest.json')
    if (manifest.get('version') != 1 or type(manifest.get('authority_generation')) is not int or
            manifest['authority_generation'] < 1 or not isinstance(manifest.get('blobs'), list)):
        raise BackupError('backup_manifest_invalid', 'invalid backup manifest')
    digest = p.check_sha256(manifest['db_sha256'])
    if manifest.get('database') not in ('controller.db', 'controller-' + digest + '.db'):
        raise BackupError('backup_manifest_invalid', 'unsafe snapshot database name')
    d.seconds(manifest['created_at'])
    return manifest


def payload_paths(manifest):
    paths = ['backup-manifest.json', manifest['database']]
    seen = set()
    for entry in manifest['blobs']:
        digest, size = p.check_sha256(entry['sha256']), entry['size']
        if type(size) is not int or size < 0 or digest in seen:
            raise BackupError('backup_manifest_invalid', 'invalid blob inventory', sha256=digest)
        seen.add(digest)
        paths.append('blobs/{}/{}/{}'.format(digest[:2], digest[2:4], digest))
    return paths


def _copy_payload(source, stage):
    manifest = payload_manifest(source)
    for name in payload_paths(manifest):
        path, target = source / name, stage / name
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise BackupError('backup_unsafe_path', 'link outside backup payload', object=name)
        if not path.is_file():
            raise BackupError('backup_object_missing', 'missing backup object: ' + name, object=name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(path), str(target))


def _extract_payload(source, stage):
    names = set()
    # No extractall: reject links, devices, duplicate entries and arbitrary paths on both platforms.
    with tarfile.open(str(source), 'r:') as archive:
        for member in archive:
            name = member.name
            allowed = (name in ('backup-manifest.json', 'controller.db') or
                       re.fullmatch(r'controller-[0-9a-f]{64}\.db', name) or
                       re.fullmatch(r'blobs/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}', name))
            if not allowed or not member.isfile() or name in names:
                raise BackupError('backup_unsafe_archive', 'unsafe or duplicate archive member', object=name)
            names.add(name)
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as stream, target.open('xb') as output:
                shutil.copyfileobj(stream, output)
    expected = set(payload_paths(payload_manifest(stage)))
    if names != expected:
        raise BackupError('backup_inventory_mismatch', 'archive inventory differs from manifest',
                          missing=sorted(expected - names), unexpected=sorted(names - expected))


def _verify_payload(stage):
    manifest = payload_manifest(stage)
    database = stage / manifest['database']
    if file_hash(database) != manifest['db_sha256']:
        raise BackupError('backup_database_hash', 'backup database hash mismatch', object=database.name)
    blobs, listed = BlobStore(stage), {}
    for entry in manifest['blobs']:
        digest, size = entry['sha256'], entry['size']
        path = blobs.path(digest)
        if not path.is_file():
            raise BackupError('backup_object_missing', 'missing backup blob: ' + digest, sha256=digest)
        if not blobs.exists(digest, size) or file_hash(path) != digest:
            raise BackupError('backup_blob_hash', 'backup blob hash/size mismatch: ' + digest, sha256=digest)
        listed[digest] = size
    db = sqlite3.connect(str(database))
    try:
        integrity = [row[0] for row in db.execute('PRAGMA integrity_check')]
        if integrity != ['ok']:
            raise BackupError('backup_database_corrupt', 'backup database integrity check failed', integrity=integrity)
        schema = schema_info(db)
        expected = sqlite3.connect(':memory:')
        try:
            expected.executescript(SCHEMA)
            if schema != schema_info(expected):
                raise BackupError('backup_schema_incompatible', 'unsupported backup database schema', schema=schema)
        finally:
            expected.close()
        meta = json.loads(db.execute("SELECT data FROM records WHERE kind='meta' AND id='controller'").fetchone()[0])
        if meta['authority_generation'] != manifest['authority_generation']:
            raise BackupError('backup_inventory_mismatch', 'backup authority generation mismatch')
        try:
            references = database_references(db, blobs)
        except (ValueError, OSError) as exc:
            raise BackupError('backup_references_invalid', str(exc)) from exc
        if references != listed:
            raise BackupError('backup_inventory_mismatch', 'backup references differ from blob inventory',
                              missing=sorted(set(references) - set(listed)), extra=sorted(set(listed) - set(references)))
        event_seq = db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
        if 'event_seq' in manifest and manifest['event_seq'] != event_seq:
            raise BackupError('backup_inventory_mismatch', 'backup event sequence mismatch')
        return dict(manifest=manifest, schema=schema, db_sha256=manifest['db_sha256'],
                    authority_generation=meta['authority_generation'], controller_id=meta['controller_id'],
                    event_seq=event_seq, blob_count=len(listed), blob_bytes=sum(listed.values()),
                    created_at=manifest['created_at'], ok=True)
    except sqlite3.DatabaseError as exc:
        raise BackupError('backup_database_corrupt', 'backup database is unreadable: ' + str(exc)) from exc
    finally:
        db.close()


@contextmanager
def verified_snapshot(source, work_dir=None):
    """Never open SQLite or create a lock in the original backup directory."""
    source = Path(source).resolve()
    with tempfile.TemporaryDirectory(prefix='.verify-', dir=str(work_dir) if work_dir else None) as temporary:
        stage = Path(temporary)
        try:
            if source.is_dir():
                _copy_payload(source, stage)
            else:
                _extract_payload(source, stage)
            info = _verify_payload(stage)
        except BackupError:
            raise
        except (OSError, ValueError, KeyError, TypeError, IndexError, tarfile.TarError) as exc:
            raise BackupError('backup_payload_invalid', 'invalid backup payload: ' + str(exc)) from exc
        yield stage, info


def inspect_backup(source, work_dir=None):
    with verified_snapshot(source, work_dir) as (_, info):
        return info


class BackupLock(ControllerLock):
    """Reuse the controller's nonblocking flock / Windows byte-range lock."""
    def __init__(self, directory):
        super().__init__(directory)
        self.path = Path(directory) / '.lock'

    def __enter__(self):
        try:
            return super().__enter__()
        except RuntimeError as exc:
            raise BackupBusy('backup .lock is held') from exc


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    try:
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(p.canonical_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
        sync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_json(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('expected JSON object: ' + str(path))
    return value


def status(directory):
    try:
        return read_json(Path(directory) / 'status.json')
    except FileNotFoundError:
        return {}


def check_id(ident):
    if (not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', ident) or
            ident.lower().startswith('upgrade-') or
            re.fullmatch(r'(status|con|prn|aux|nul|com[0-9]|lpt[0-9])', ident, re.I)):
        raise ValueError('unsafe or unmanaged backup id')
    return ident


def check_complete(value):
    if not isinstance(value, dict):
        raise ValueError('invalid COMPLETE metadata')
    check_id(value['id'])
    d.seconds(value['created_at'])
    if value['tar_name'] != 'backup.tar':
        raise ValueError('unsafe tar name')
    p.check_sha256(value['tar_sha256'])
    p.check_sha256(value['db_sha256'])
    if type(value['tar_bytes']) is not int or value['tar_bytes'] <= 0:
        raise ValueError('invalid tar size')
    if type(value['authority_generation']) is not int or value['authority_generation'] < 1:
        raise ValueError('invalid authority generation')
    return {key: value[key] for key in COMPLETE_KEYS}


def order(value):
    return d.seconds(value['created_at']), value['id']


def list_complete(backups_dir):
    root = Path(backups_dir)
    result = []
    for directory in root.iterdir() if root.exists() else ():
        if directory.name.startswith(('.', 'upgrade-')) or directory.is_symlink() or not directory.is_dir():
            continue
        marker = directory / 'COMPLETE.json'
        if not marker.is_file():
            continue
        value = check_complete(read_json(marker))
        if value['id'] != directory.name:
            raise ValueError('COMPLETE id does not match directory')
        archive = directory / value['tar_name']
        if archive.is_symlink() or not archive.is_file() or archive.stat().st_size != value['tar_bytes']:
            raise ValueError('complete backup archive missing or wrong size')
        result.append(value)
    return sorted(result, key=order)


def confirmed(backups_dir, complete):
    try:
        marker = read_json(Path(backups_dir) / complete['id'] / 'OFFSITE.json')
    except FileNotFoundError:
        return None
    if marker.get('id') != complete['id'] or marker.get('tar_sha256') != complete['tar_sha256']:
        raise ValueError('OFFSITE metadata mismatch')
    d.seconds(marker['confirmed_at'])
    return marker


def positive(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(name + ' must be a positive integer')


def prune_local(root, keep, max_bytes):
    sets = list_complete(root)
    protected = {sets[-1]['id']} if sets else set()
    copied = [item for item in sets if confirmed(root, item)]
    if copied:
        protected.add(copied[-1]['id'])
    total, count = sum(item['tar_bytes'] for item in sets), len(sets)
    for item in sets:
        if count <= keep and total <= max_bytes:
            break
        if item['id'] not in protected:
            # 先原子改名为 list_complete 不可见的临时名再删除：中途中断留下的残留由下一轮 .tmp-* 清理回收，
            # 不会变成“COMPLETE.json 仍在但归档缺失”而让之后每一轮都失败。
            doomed = Path(root) / ('.tmp-prune-' + item['id'])
            os.replace(str(Path(root) / item['id']), str(doomed))
            sync_directory(root)
            shutil.rmtree(str(doomed))
            count -= 1
            total -= item['tar_bytes']
    sync_directory(root)


def deterministic_tar(source, destination):
    source = Path(source)
    manifest = read_json(source / 'backup-manifest.json')
    digest = p.check_sha256(manifest['db_sha256'])
    if manifest['database'] not in ('controller.db', 'controller-' + digest + '.db'):
        raise ValueError('unsafe snapshot database name')
    paths = [source / 'backup-manifest.json', source / manifest['database']]
    for entry in manifest['blobs']:
        digest = p.check_sha256(entry['sha256'])
        paths.append(source / 'blobs' / digest[:2] / digest[2:4] / digest)
    # Only the manifest inventory belongs in the archive, never SQLite scratch/WAL files.
    with tarfile.open(str(destination), 'w', format=tarfile.GNU_FORMAT) as archive:
        for path in sorted(paths):
            if path.is_symlink():
                raise ValueError('symlink in backup snapshot')
            info = archive.gettarinfo(str(path), arcname=path.relative_to(source).as_posix())
            info.mtime = info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.mode = 0o600
            with path.open('rb') as stream:
                archive.addfile(info, stream)
    with Path(destination).open('r+b') as stream:
        os.fsync(stream.fileno())


def run_local(data_dir, backups_dir, keep=8, max_bytes=500000000):
    positive(keep, 'keep')
    positive(max_bytes, 'max_bytes')
    root, data = Path(backups_dir).resolve(), Path(data_dir).resolve()
    if root == data or root in data.parents or data in root.parents:
        raise ValueError('backups directory must be separate from controller data')
    root.mkdir(parents=True, exist_ok=True)
    with BackupLock(root):
        state = status(root)
        local = state.setdefault('local', dict(last_success_at=None, last_success_id=None,
                                               last_failure_at=None, last_error=None))
        state.setdefault('offsite', dict(last_confirmed_at=None, last_confirmed_id=None, location=None))
        try:
            for abandoned in root.glob('.tmp-*'):
                if abandoned.is_dir() and not abandoned.is_symlink():
                    shutil.rmtree(str(abandoned))
                else:
                    abandoned.unlink()
            ident = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ-') + uuid.uuid4().hex[:12]
            stage = root / ('.tmp-' + ident)
            stage.mkdir(mode=0o700)
            snapshot = stage / 'snapshot'
            manifest = backup(data, snapshot)
            archive = stage / 'backup.tar'
            deterministic_tar(snapshot, archive)
            complete = dict(id=ident, created_at=manifest['created_at'], tar_name=archive.name,
                            tar_sha256=file_hash(archive), tar_bytes=archive.stat().st_size,
                            db_sha256=manifest['db_sha256'], authority_generation=manifest['authority_generation'])
            shutil.rmtree(str(snapshot))
            atomic_json(stage / 'COMPLETE.json', complete)
            os.replace(str(stage), str(root / ident))
            sync_directory(root)
            prune_local(root, keep, max_bytes)
            success = dict(local, last_success_at=d.timestamp(time.time()), last_success_id=ident, last_error=None)
            atomic_json(root / 'status.json', dict(state, local=success))
            return complete
        except Exception as exc:
            local.update(last_failure_at=d.timestamp(time.time()), last_error=str(exc))
            atomic_json(root / 'status.json', state)
            raise


def confirm_offsite(backups_dir, ident, tar_sha256, location):
    check_id(ident)
    p.check_sha256(tar_sha256)
    if not isinstance(location, str) or not location.strip() or len(location) > 4096 or '\x00' in location:
        raise ValueError('invalid offsite location')
    root = Path(backups_dir)
    with BackupLock(root):
        sets = list_complete(root)
        complete = next((item for item in sets if item['id'] == ident), None)
        if complete is None or complete['tar_sha256'] != tar_sha256:
            raise ValueError('offsite confirmation hash/id mismatch')
        marker = confirmed(root, complete)
        if marker is None:
            marker = dict(id=ident, tar_sha256=tar_sha256, location=location,
                          confirmed_at=d.timestamp(time.time()))
            atomic_json(root / ident / 'OFFSITE.json', marker)
        # Recover a crash between OFFSITE and status, without moving backwards on a late ACK.
        newest = next(confirmed(root, item) for item in reversed(sets) if confirmed(root, item))
        state = status(root)
        state['offsite'] = dict(last_confirmed_at=newest['confirmed_at'], last_confirmed_id=newest['id'],
                                location=newest['location'])
        atomic_json(root / 'status.json', state)
        return marker


def archive_matches(path, complete):
    return (path.is_file() and not path.is_symlink() and path.stat().st_size == complete['tar_bytes']
            and file_hash(path) == complete['tar_sha256'])


def verify_archive(path, complete, extracted):
    """Extract only our regular-file layout, then check the same inventory as restore."""
    if not archive_matches(Path(path), complete):
        raise ValueError('tar hash/size mismatch')
    extracted = Path(extracted)
    extracted.mkdir(mode=0o700)
    names, total = set(), 0
    with tarfile.open(str(path), 'r:') as archive:
        for member in archive:
            name = member.name
            allowed = (name == 'backup-manifest.json' or
                       re.fullmatch(r'controller-[0-9a-f]{64}\.db', name) or
                       re.fullmatch(r'blobs/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}', name))
            total += member.size
            if not allowed or not member.isfile() or name in names or member.size < 0 or total > complete['tar_bytes']:
                raise ValueError('unsafe or unexpected tar member')
            names.add(name)
            target = extracted / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, target.open('xb') as output:
                shutil.copyfileobj(source, output)
    manifest = read_json(extracted / 'backup-manifest.json')
    if (manifest.get('version') != 1 or manifest.get('db_sha256') != complete['db_sha256'] or
            manifest.get('authority_generation') != complete['authority_generation'] or
            manifest.get('created_at') != complete['created_at']):
        raise ValueError('backup manifest/COMPLETE mismatch')
    database_name = 'controller-' + complete['db_sha256'] + '.db'
    if manifest.get('database') != database_name or file_hash(extracted / database_name) != complete['db_sha256']:
        raise ValueError('backup database hash/name mismatch')
    blobs, listed = BlobStore(extracted), {}
    expected = {'backup-manifest.json', database_name}
    for entry in manifest['blobs']:
        digest, size = p.check_sha256(entry['sha256']), entry['size']
        if type(size) is not int or size < 0 or digest in listed:
            raise ValueError('invalid blob inventory')
        if not blobs.exists(digest, size) or file_hash(blobs.path(digest)) != digest:
            raise ValueError('backup blob hash/size mismatch')
        listed[digest] = size
        expected.add(blobs.path(digest).relative_to(extracted).as_posix())
    if names != expected:
        raise ValueError('unexpected backup inventory')
    db = sqlite3.connect(str(extracted / database_name))
    try:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('backup database integrity check failed')
        meta = json.loads(db.execute("SELECT data FROM records WHERE kind='meta' AND id='controller'").fetchone()[0])
        if meta['authority_generation'] != complete['authority_generation'] or database_references(db, blobs) != listed:
            raise ValueError('backup inventory/generation mismatch')
    finally:
        db.close()
    return manifest


def verified_copies(dest):
    result = []
    for path in Path(dest).glob('*.json'):
        if path.name == 'status.json':
            continue
        value = read_json(path)
        complete = check_complete(value)
        if path.stem != complete['id']:
            raise ValueError('local copy id mismatch')
        if value.get('verified_at') and archive_matches(Path(dest) / (complete['id'] + '.tar'), complete):
            result.append(value)
    return sorted(result, key=order)


def prune_offsite(dest, keep):
    copies = verified_copies(dest)
    confirmed_copies = [item for item in copies if item.get('confirmed_at')]
    protected = {copies[-1]['id']} if copies else set()
    if confirmed_copies:
        protected.add(confirmed_copies[-1]['id'])
    count = len(copies)
    for item in copies:
        if count <= keep:
            break
        if item['id'] in protected:
            continue
        # The JSON is the commit marker. An interrupted deletion can only leave an orphan tar.
        (dest / (item['id'] + '.json')).unlink()
        (dest / (item['id'] + '.tar')).unlink()
        count -= 1
    sync_directory(dest)


def pull(dest, remote, keep_offsite=28):
    positive(keep_offsite, 'keep_offsite')
    dest = Path(dest).resolve()
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    with BackupLock(dest):
        state = status(dest)
        try:
            partial = dest / '.partial'
            partial.mkdir(mode=0o700, exist_ok=True)
            for abandoned in partial.glob('verify-*'):
                shutil.rmtree(str(abandoned))
            available = sorted((check_complete(item) for item in remote.list()), key=order, reverse=True)
            if len({item['id'].lower() for item in available}) != len(available):
                raise ValueError('duplicate remote backup id')
            copies = verified_copies(dest)
            pending = [item for item in copies if not item.get('confirmed_at') and
                       any(remote_item['id'] == item['id'] for remote_item in available)]
            by_id = {item['id']: item for item in copies}
            for item in available:
                if item['id'] in by_id and check_complete(by_id[item['id']]) != item:
                    raise ValueError('remote metadata changed for a verified backup id')
            # A lost ACK must be retried even when a newer remote snapshot now exists.
            complete = check_complete(pending[-1]) if pending else next(
                (item for item in available if item['id'] not in by_id and
                 (not copies or order(item) > order(copies[-1]))), None)
            if complete is None:
                if copies and not copies[-1].get('confirmed_at'):
                    raise ValueError('unconfirmed copy no longer on remote; waiting for a newer backup')
                # Recover interruption after ACK/JSON but before status publication; no fresh success on a no-op.
                if copies:
                    latest = max((item for item in copies if item.get('confirmed_at')),
                                 key=lambda item: d.seconds(item['confirmed_at']))
                    prune_offsite(dest, keep_offsite)
                    atomic_json(dest / 'status.json', dict(state, last_success_at=latest['confirmed_at'],
                                                          last_success_id=latest['id'], last_error=None))
                return None
            ident = complete['id']
            local_tar, local_json = dest / (ident + '.tar'), dest / (ident + '.json')
            if ident not in by_id:
                download = partial / (ident + '.tar')
                for abandoned in partial.glob('*.tar'):
                    if abandoned != download:
                        abandoned.unlink()
                # Also recover a crash between the tar rename and the JSON commit marker.
                source = local_tar if archive_matches(local_tar, complete) else download
                if not archive_matches(source, complete):
                    remote.download(ident, download)
                    source = download
                extracted = partial / ('verify-' + uuid.uuid4().hex)
                try:
                    verify_archive(source, complete, extracted)
                except Exception:
                    if source == download and download.exists():
                        download.unlink()
                    raise
                finally:
                    if extracted.exists():
                        shutil.rmtree(str(extracted))
                with source.open('r+b') as stream:
                    os.fsync(stream.fileno())
                if source != local_tar:
                    os.replace(str(source), str(local_tar))
                sync_directory(dest)
                receipt = dict(complete, verified_at=d.timestamp(time.time()), confirmed_at=None)
                atomic_json(local_json, receipt)
            else:
                receipt = by_id[ident]
            remote.confirm(ident, complete['tar_sha256'], str(local_tar))
            receipt['confirmed_at'] = d.timestamp(time.time())
            atomic_json(local_json, receipt)
            prune_offsite(dest, keep_offsite)
            atomic_json(dest / 'status.json', dict(state, last_success_at=receipt['confirmed_at'],
                                                  last_success_id=ident, last_error=None))
            return complete
        except Exception as exc:
            state.update(last_failure_at=d.timestamp(time.time()), last_error=str(exc))
            atomic_json(dest / 'status.json', state)
            raise


def remote_root(value):
    if not isinstance(value, str) or not re.fullmatch(r'/[A-Za-z0-9_./-]+', value):
        raise ValueError('unsafe remote backups directory')
    parts = value.split('/')[1:]
    if any(part in ('', '.', '..') for part in parts) or str(PurePosixPath(value)) != value:
        raise ValueError('remote backups directory must be an absolute normalized path')
    return value


def remote_command(backups_dir, python_cmd, action, ident=None, sha256=None, location=None):
    """Pure shell construction: only an argv-quoted Python executable, never shell fragments."""
    root = remote_root(backups_dir)
    if not isinstance(python_cmd, str) or not re.fullmatch(r'[A-Za-z0-9_./-]+', python_cmd) or python_cmd.startswith('-'):
        raise ValueError('python_cmd must name one executable')
    args = ['runuser', '-u', 'agent-collab', '--', python_cmd, '-B', '-m', 'agent_collab.backup', action,
            '--backups-dir', root]
    if action == 'confirm':
        check_id(ident)
        p.check_sha256(sha256)
        if not isinstance(location, str) or not location or '\x00' in location:
            raise ValueError('invalid offsite location')
        args += ['--id', ident, '--sha256', sha256, '--location', location]
    elif action != 'list':
        raise ValueError('unsupported remote action')
    return 'cd /opt/agent-collab/current && ' + shlex.join(args)


def download_command(instance_id, workbench_path, backups_dir, ident, local_path):
    check_id(ident)
    root = remote_root(backups_dir)
    return [str(workbench_path), 'download', root + '/' + ident + '/backup.tar', str(local_path),
            '--instance-id', instance_id, '-f']


class WorkbenchRemote:
    def __init__(self, instance_id, workbench_path='/usr/local/bin/workbench',
                 remote_backups_dir='/var/lib/agent-collab/backups', python_cmd='/usr/bin/python3'):
        if not isinstance(instance_id, str) or not re.fullmatch(r'i-[A-Za-z0-9]+', instance_id):
            raise ValueError('invalid instance id')
        self.instance_id, self.workbench = instance_id, str(workbench_path)
        self.root, self.python = remote_root(remote_backups_dir), python_cmd
        remote_command(self.root, self.python, 'list')

    def _exec(self, action, **kwargs):
        command = remote_command(self.root, self.python, action, **kwargs)
        result = subprocess.run([self.workbench, 'exec', '--instance-id', self.instance_id,
                                 '--command', command, '--output', 'json'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True, timeout=300)
        value = json.loads(result.stdout)
        if type(value.get('exit_code')) is not int or value['exit_code'] != 0:
            raise RuntimeError('workbench exec failed (exit_code=%s): %s' %
                               (value.get('exit_code'), str(value.get('stderr', ''))[:2000]))
        return json.loads(value['stdout'])

    def list(self):
        value = self._exec('list')
        if not isinstance(value, list):
            raise ValueError('workbench list must return a JSON array')
        return value

    def download(self, ident, local_path):
        subprocess.run(download_command(self.instance_id, self.workbench, self.root, ident, local_path),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True, timeout=3600)

    def confirm(self, ident, sha, location):
        return self._exec('confirm', ident=ident, sha256=sha, location=location)


def summary(backups_dir, now):
    result = dict(local_last_success_at='unknown', local_age_s='unknown',
                  offsite_last_confirmed_at='unknown', offsite_age_s='unknown', last_error='unknown')
    try:
        state = status(backups_dir)
        if not state:
            return result
        for section, field, output, age in (
                ('local', 'last_success_at', 'local_last_success_at', 'local_age_s'),
                ('offsite', 'last_confirmed_at', 'offsite_last_confirmed_at', 'offsite_age_s')):
            value = state.get(section, {}).get(field)
            if value:
                result[age] = max(0, now - d.seconds(value))
                result[output] = value
        result['last_error'] = state.get('local', {}).get('last_error') or state.get('offsite', {}).get('last_error')
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        result['last_error'] = 'backup status unreadable: ' + str(exc)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    local = commands.add_parser('run-local')
    local.add_argument('--data-dir', required=True)
    local.add_argument('--backups-dir', required=True)
    local.add_argument('--keep', type=int, default=8)
    local.add_argument('--max-bytes', type=int, default=500000000)
    listing = commands.add_parser('list')
    listing.add_argument('--backups-dir', required=True)
    confirm = commands.add_parser('confirm')
    confirm.add_argument('--backups-dir', required=True)
    confirm.add_argument('--id', required=True)
    confirm.add_argument('--sha256', required=True)
    confirm.add_argument('--location', required=True)
    pulling = commands.add_parser('pull')
    pulling.add_argument('--dest', required=True)
    pulling.add_argument('--instance-id', required=True)
    pulling.add_argument('--workbench', default='/usr/local/bin/workbench')
    pulling.add_argument('--remote-backups-dir', default='/var/lib/agent-collab/backups')
    pulling.add_argument('--python-cmd', default='/usr/bin/python3')
    pulling.add_argument('--keep-offsite', type=int, default=28)
    checking = commands.add_parser('status')
    paths = checking.add_mutually_exclusive_group(required=True)
    paths.add_argument('--backups-dir')
    paths.add_argument('--dest')
    args = parser.parse_args(argv)
    try:
        if args.command == 'run-local':
            value = run_local(args.data_dir, args.backups_dir, args.keep, args.max_bytes)
        elif args.command == 'list':
            value = list_complete(args.backups_dir)
        elif args.command == 'confirm':
            value = confirm_offsite(args.backups_dir, args.id, args.sha256, args.location)
        elif args.command == 'pull':
            value = pull(args.dest, WorkbenchRemote(args.instance_id, args.workbench,
                                                   args.remote_backups_dir, args.python_cmd), args.keep_offsite)
        else:
            value = status(args.backups_dir or args.dest)
        print(json.dumps(value, ensure_ascii=False))
        return 0
    except BackupBusy as exc:
        print(str(exc), file=sys.stderr)
        return LOCKED_EXIT
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
