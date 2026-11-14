"""Maintenance API helpers and public online/offline CLI (stdlib, Python 3.9)."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from urllib.parse import quote

from . import domain as d, protocol as p
from .backup import (BackupError, atomic_json, describe_backup, deterministic_tar, file_hash,
                     inspect_backup, verified_snapshot)
from .blobstore import BlobStore
from .store import Store


def records(db):
    return {(row[0], row[1]): json.loads(row[2]) for row in db.execute('SELECT kind,id,data FROM records ORDER BY rowid')}


def current_state(data_dir):
    """Read the stopped authority in a copy; even SQLite SHM writes stay in the copy."""
    path = Path(data_dir).resolve() / 'controller.db'
    try:
        with tempfile.TemporaryDirectory(prefix='.identity-', dir=str(path.parent.parent)) as temporary:
            copied = Path(temporary) / 'controller.db'
            shutil.copyfile(str(path), str(copied))
            wal = Path(str(path) + '-wal')
            if wal.exists():
                shutil.copyfile(str(wal), str(copied) + '-wal')
            with closing(sqlite3.connect(str(copied))) as db:
                db.execute('BEGIN')
                if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise ValueError('integrity_check failed')
                state = records(db)
                if any(not isinstance(value, dict) for value in state.values()):
                    raise ValueError('invalid authority records')
                for (kind, _), value in state.items():
                    if kind in ('client_token', 'node_token'):
                        p.check_sha256(value['hash'])
                        if not isinstance(value[kind.split('_')[0] + '_id'], str):
                            raise ValueError('invalid credential owner')
                if type(state[('meta', 'controller')]['authority_generation']) is not int:
                    raise ValueError('invalid authority generation')
                sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
                return state, sequence[0] if sequence else 0, None
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        return None, 0, type(exc).__name__ + ': ' + str(exc)


def identities(saved, current, policy=None):
    if policy not in (None, 'carry-forward', 'keep', 'revoke-all'):
        raise ValueError('unknown identity policy')
    if current is None and policy not in ('keep', 'revoke-all'):
        raise BackupError('identity_required', '当前数据库不可读；必须显式选择 --identity keep 或 revoke-all')
    # Explicit keep cannot override known revocations on a readable authority.
    effective = 'revoke-all' if policy == 'revoke-all' else 'carry-forward' if current is not None else 'keep'
    result = dict(policy=effective, revoked=[], re_pair=[], warnings=[])
    if current is None:
        result['warnings'].append('无法确认备份之后的撤销；keep 会保留备份里的授权。' if effective == 'keep'
                                  else '当前身份历史不可读；所有客户端与节点必须重新配对。')
    for kind in ('client', 'node'):
        field = kind + '_id'
        old = {ident: value for (k, ident), value in saved.items() if k == kind}
        live = {ident: value for (k, ident), value in (current or {}).items() if k == kind}
        old_ids = set(old) | {v[field] for (k, _), v in saved.items() if k == kind + '_token'}
        live_ids = set(live) | {v[field] for (k, _), v in (current or {}).items() if k == kind + '_token'}
        for ident in sorted(live_ids - old_ids):
            result['re_pair'].append(dict(kind=kind, id=ident, reason='paired_after_backup'))
        for ident in sorted(old_ids):
            before, now = old.get(ident, {}), live.get(ident, {})
            revoked = (effective == 'revoke-all' or bool(before.get('revoked_at') or before.get('retired_at')) or
                       (current is not None and (ident not in live_ids or (ident in old and ident not in live) or
                                                 now.get('revoked_at') or now.get('retired_at'))))
            old_tokens = {key for key, v in saved.items() if key[0] == kind + '_token' and v[field] == ident}
            removed = {key for key in old_tokens if effective == 'revoke-all' or revoked or
                       (current is not None and current.get(key) != saved[key])}
            if revoked or removed:
                result['revoked'].append(dict(kind=kind, id=ident,
                                              reason='revoke_all' if effective == 'revoke-all' else 'revoked_or_deleted_or_rotated',
                                              credentials=len(removed), identity_revoked=bool(revoked)))
                if effective == 'revoke-all' or (removed and not revoked):
                    result['re_pair'].append(dict(kind=kind, id=ident, reason='credentials_revoked'))
    return result


def carry_identities(store, saved, current, plan, now):
    revoked = {(r['kind'], r['id']) for r in plan['revoked'] if r['identity_revoked']}
    for kind in ('client', 'node'):
        field = kind + '_id'
        for token in store.all(kind + '_token'):
            key = (kind + '_token', token['hash'])
            if ((kind, token[field]) in revoked or plan['policy'] == 'revoke-all' or
                    (current is not None and current.get(key) != saved[key])):
                store.delete(*key)
        for value in store.all(kind):
            ident = value[field]
            live = (current or {}).get((kind, ident), {})
            if (kind, ident) in revoked:
                value['revoked_at' if kind == 'client' else 'retired_at'] = (
                    live.get('revoked_at' if kind == 'client' else 'retired_at') or d.timestamp(now))
                value['version'] = value.get('version', 0) + 1
                if kind == 'node':
                    value.update(drain=True, retire_reason='restore_identity_policy')
            if kind == 'client' and current is not None:
                value['scopes'] = [s for s in value['scopes'] if s in live.get('scopes', [])]
            store.put(kind, ident, value)
    store.db.execute("DELETE FROM records WHERE kind IN ('session','pairing','client_pairing')")


def pending_writebacks(store, node_id):
    pending = {('application', a['application_id']) for a in store.all('application')
               if a.get('target_node_id') == node_id and a['status'] == 'started'}
    for op in store.all('node_op'):
        if op['node_id'] == node_id and op['kind'] in ('project_apply', 'project_recover') and op['status'] in ('requested', 'received'):
            ident = op.get('params', {}).get('application_id')
            pending.add(('application', ident) if ident else ('operation', op['op_id']))
    # 契约自动回写：controller 还在等这个节点写入并上报的条目。
    for record in store.all('integration_target'):
        if record.get('target_node_id') == node_id and record.get('pending'):
            pending.add(('integration', record['pending'].get('job_id')))
    return len(pending)


def pending_results(store):
    latest = {a['job_id']: a for a in store.all('attempt')}
    checkpoints = store.all('checkpoint')
    results = []
    for job in store.all('job'):
        if (job.get('contract') or {}).get('coordinates'):
            continue  # 受管协调回合的检查点是它的决定，不是用户会采用的成果
        attempt = latest.get(job['job_id'])
        if not attempt or attempt['status'] != 'succeeded':
            continue
        durable = [cp for cp in checkpoints if cp['attempt_id'] == attempt['attempt_id'] and cp['state'] in ('durable', 'verified')]
        if not durable:
            continue
        cp = max(durable, key=lambda c: store.get('checkpoint_private', c['checkpoint_id'], {}).get('order', 0))
        if job.get('delivery_checkpoint_id') == cp['checkpoint_id'] or store.get('dismissal', cp['checkpoint_id']):
            continue
        results.append(dict(job_id=job['job_id'], title=job['title'], attempt_id=attempt['attempt_id'],
                            checkpoint_id=cp['checkpoint_id'], acceptance=job.get('acceptance'),
                            delivery_checkpoint_id=job.get('delivery_checkpoint_id'), reason='unpublished_latest_durable'))
    return results


def barrier_preview(store):
    meta = store.get('meta', 'controller')
    pending = pending_results(store)
    return dict(ready=not pending and meta['dispatch_paused'] and (meta.get('intake') or {}).get('mode', 'open') != 'open',
                pending_results=pending, dispatch_paused=meta['dispatch_paused'],
                intake=(meta.get('intake') or {}).get('mode', 'open'))


def dismiss(app, job, data, identity):
    from .controller import APIError, response
    if set(data) - {'request_id', 'expected_version', 'reason', 'checkpoint_id'}:
        raise ValueError('unknown dismissal fields')
    app.version(data, job)
    reason = data.get('reason')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise ValueError('dismiss requires a reason of at most 1000 characters')
    target = next((r for r in pending_results(app.store) if r['job_id'] == job['job_id']), None)
    if target is None:
        raise APIError('conflict', 'no unpublished latest durable checkpoint to dismiss')
    # 带上用户确认时看到的检查点：确认期间出现了更新的待发布检查点，就不能把确认挪到新的那个上。
    if 'checkpoint_id' in data and data['checkpoint_id'] != target['checkpoint_id']:
        raise APIError('conflict', 'the latest unpublished checkpoint changed; review it again before dismissing')
    record = dict(target, reason=reason.strip(), by=app.actor(identity), at=d.timestamp(app.clock()))
    app.store.put('dismissal', target['checkpoint_id'], record)
    changed = app.change('job', job, app.clock(), dismissed_checkpoint_id=target['checkpoint_id'], dismissal=record)
    app.emit('job.dismissed', record, app.clock(), job=job)
    return response(changed)


def restore_preview(store, source, activity=None, work_dir=None):
    try:
        with verified_snapshot(source, work_dir) as (stage, info):
            with closing(sqlite3.connect(str(stage / info['manifest']['database']))) as db:
                saved = records(db)
    except BackupError as exc:
        return dict(verdict='blocked', error=exc.result(), disposable=None, changes=[],
                    disposable_scope='只统计任务与写回，不看项目与配对。',
                    file_effects='节点和本机项目文件写入不会被撤回。')
    current = records(store.db)
    plan = identities(saved, current)
    kinds = {'job', 'attempt', 'checkpoint', 'assessment', 'publication', 'application', 'integration_target',
             'project', 'settings', 'client', 'node', 'client_token', 'node_token', 'baseline', 'dismissal', 'effects', 'node_op', 'download'}
    changes, reconcile = [], False
    jobs = {ident: value for (kind, ident), value in current.items() if kind == 'job'}
    def disposable_job(ident):
        job = jobs.get(ident, {})
        target = (job.get('contract') or {}).get('coordinates') or {}
        return bool(job.get('acceptance_run') or jobs.get(target.get('job_id'), {}).get('acceptance_run'))
    for key in sorted(set(saved) | set(current)):
        kind, ident = key
        before, after = saved.get(key), current.get(key)
        if kind not in kinds or before == after:
            continue
        value = after or before
        entry = dict(kind=kind, id=ident, change='created' if before is None else 'deleted' if after is None else 'changed',
                     consequence='恢复后丢弃当前事实，回到备份状态。')
        if kind in ('client_token', 'node_token'):
            entry['id'] = value[kind.split('_')[0] + '_id']
            entry['consequence'] = '旧凭据按拒绝优先结转撤销；备份后新凭据需重新配对。'
        entry['fields'] = sorted(k for k in set(before or {}) | set(after or {}) if (before or {}).get(k) != (after or {}).get(k))
        visible = ('job_id', 'attempt_id', 'checkpoint_id', 'title', 'name', 'status', 'state', 'revision', 'version',
                   'acceptance', 'delivery_checkpoint_id', 'durable_checkpoint_id', 'integration_status', 'scopes',
                   'revoked_at', 'retired_at')
        entry['before'] = {k: before[k] for k in visible if before and k in before}
        entry['after'] = {k: after[k] for k in visible if after and k in after}
        if kind in ('project', 'settings'):
            entry.update(revision_before=(before or {}).get('revision'), revision_after=(after or {}).get('revision'))
        if kind in ('client', 'node'):
            entry.update(revoked_at=value.get('revoked_at'), retired_at=value.get('retired_at'),
                         consequence='撤销/退役不会复活；备份后新增身份需重新配对。')
        safe = kind in ('job', 'attempt', 'checkpoint', 'assessment', 'publication', 'dismissal', 'download') and disposable_job(
            ident if kind == 'job' else value.get('job_id'))
        if kind in ('application', 'integration_target'):
            entry.update(status=value.get('status'), target_node_id=value.get('target_node_id'),
                         consequence='节点和本机项目文件写入不会被撤回；恢复后需核对应用记录和实际文件。')
        changes.append(entry)
        reconcile |= not safe
    unresolved = [dict(application_id=v['application_id'], status=v['status'], target_node_id=v.get('target_node_id'))
                  for (kind, _), v in current.items() if kind == 'application' and v['status'] in ('started', 'unknown')]
    minimum = store.db.execute('SELECT MIN(seq) FROM events').fetchone()[0]
    max_seq = store.db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
    history_incomplete = bool((minimum and minimum > info['event_seq'] + 1) or max_seq < info['event_seq'] or
                              current[('meta', 'controller')]['controller_id'] != info['controller_id'] or
                              current[('meta', 'controller')]['authority_generation'] != info['authority_generation'])
    disposable = activity.get('disposable') if activity is not None else None
    return dict(verdict='needs_reconcile' if reconcile or unresolved or history_incomplete or disposable is not True else 'safe',
                backup=info, changes=changes, identity=plan, unresolved_applications=unresolved,
                history_incomplete=history_incomplete, disposable=disposable,
                disposable_scope='只统计任务与写回，不看项目与配对；不能单独据此判断恢复安全。',
                file_effects='节点和本机项目文件写入不会被撤回；本机绑定、应用 journal 和配置文件不随数据库回退。')


def backup_path(app, ident):
    from .controller import APIError
    if not isinstance(ident, str) or not re.fullmatch(r'(managed|upgrade)-[A-Za-z0-9_-]{1,120}', ident):
        raise ValueError('invalid backup id')
    root = app.data_dir.parent / 'backups'
    path = root / ident
    if root.is_symlink() or path.is_symlink() or path.resolve().parent != root.resolve():
        raise ValueError('backup path is not instance-managed')
    if not path.is_dir():
        raise APIError('not_found', 'backup not found')
    return path


def backups(app):
    root, result = app.data_dir.parent / 'backups', []
    if root.is_symlink():
        raise ValueError('backup root must not be a symlink')
    for path in sorted(root.iterdir()) if root.exists() else []:
        if not re.fullmatch(r'(managed|upgrade)-[A-Za-z0-9_-]{1,120}', path.name) or path.is_symlink() or not path.is_dir():
            continue
        item = dict(id=path.name, kind=path.name.split('-')[0], location='instance',
                    created_at=None, schema=None, db_sha256=None, blob_count=None, blob_bytes=None,
                    authority_generation=None, event_seq=None, last_verification=app.store.get('backup_verification', path.name))
        try:
            info = describe_backup(path)  # 轻量：不复制、不重算摘要；完整核对见 verify 与 last_verification
            item.update({k: info[k] for k in ('created_at', 'schema', 'db_sha256', 'blob_count', 'blob_bytes', 'authority_generation', 'event_seq')})
            item['valid'] = True
        except BackupError as exc:
            item.update(valid=False, error=exc.result())
        result.append(item)
    return result


def retention_settings(store):
    return store.get('settings', 'backups', dict(revision=0, keep=8, updated_at=None, updated_by=None))


def prune(app):
    items = [item for item in backups(app) if item['kind'] == 'managed']
    items.sort(key=lambda item: (item['created_at'] or '', item['event_seq'] or 0, item['id']))
    protected, removed = None, []
    if len(items) > retention_settings(app.store)['keep']:
        # 列表只做轻量核对；要删别的备份之前，从最新往前完整核对，保护第一份真正可用的恢复点。
        for item in reversed(items):
            if not item['valid']:
                continue
            try:
                inspect_backup(backup_path(app, item['id']), app.data_dir)
            except BackupError:
                continue
            protected = item['id']
            break
    for item in items:
        if len(items) - len(removed) <= retention_settings(app.store)['keep']:
            break
        if item['id'] == protected:
            continue
        path = backup_path(app, item['id'])
        tombstone = path.with_name('.prune-' + uuid.uuid4().hex)
        os.replace(str(path), str(tombstone))
        removed.append(item['id'])
        shutil.rmtree(str(tombstone))
    return removed


def operation(app, kind, identity, action):
    from .controller import response
    record = dict(op_id=str(uuid.uuid4()), kind=kind, status='running', steps=[], result=None, error=None,
                  requested_by=app.actor(identity), requested_at=d.timestamp(app.clock()), started_at=d.timestamp(app.clock()), finished_at=None)
    app.store.put('maintenance_op', record['op_id'], record)
    try:
        record['result'] = action(record)
        record['status'] = 'succeeded'
    except (BackupError, OSError, sqlite3.Error, ValueError) as exc:
        record['status'] = 'failed'
        record['error'] = exc.result() if isinstance(exc, BackupError) else dict(code='maintenance_failed', message=str(exc))
    record['finished_at'] = d.timestamp(app.clock())
    app.store.put('maintenance_op', record['op_id'], record)
    app.emit('maintenance.completed', record, app.clock())
    return response({'maintenance_op': record})


def api(app, method, route, query, data, identity):
    from .controller import APIError, Response, backup, response
    app.allow(identity, 'maintain')
    pieces = route.strip('/').split('/')
    if method not in ('GET', 'HEAD') and query:
        raise ValueError('maintenance mutations do not accept query parameters')
    if method in ('GET', 'HEAD'):
        allowed_query = {'backup_id'} if route == '/maintenance/restore-preview' else set()
        if set(query) - allowed_query or data:
            raise ValueError('unknown maintenance parameters')
    if route == '/settings/backups':
        if method == 'GET':
            return response({'settings': retention_settings(app.store)})
        if method == 'PUT':
            if set(data) - {'request_id', 'expected_revision', 'keep'} or type(data.get('keep')) is not int or data['keep'] < 1:
                raise ValueError('keep must be a positive integer; unknown fields are not accepted')
            def update():
                current = retention_settings(app.store)
                if type(data.get('expected_revision')) is not int or data['expected_revision'] != current['revision']:
                    raise APIError('version_mismatch', 'backup settings changed', current=current)
                value = dict(keep=data['keep'], revision=current['revision'] + 1,
                             updated_at=d.timestamp(app.clock()), updated_by=app.actor(identity))
                app.store.put('settings', 'backups', value)
                app.emit('settings.backups.updated', value, app.clock())
                return operation(app, 'retention', identity, lambda op: dict(settings=value, removed=prune(app)))
            return app.idempotent(route, data, update)
    if route == '/maintenance/backups':
        if method == 'GET':
            return response({'backups': backups(app)})
        if method == 'POST':
            if set(data) - {'request_id', 'label'} or ('label' in data and (not isinstance(data['label'], str) or len(data['label']) > 200)):
                raise ValueError('backup accepts only request_id and an optional label of at most 200 characters')
            def create(op):
                ident = 'managed-' + time.strftime('%Y%m%dT%H%M%S', time.gmtime(app.clock())) + '-' + op['op_id'][:8]
                root = app.data_dir.parent / 'backups'
                if root.is_symlink():
                    raise ValueError('backup root must not be a symlink')
                root.mkdir(mode=0o700, exist_ok=True)
                dest = root / ident
                if dest.exists():
                    raise ValueError('backup id already exists')
                backup(app.data_dir, dest, app.clock, record_store=app.store)
                op['steps'].append(dict(name='snapshot', status='succeeded'))
                info = inspect_backup(dest, app.data_dir)
                verified = dict(ok=True, at=d.timestamp(app.clock()), db_sha256=info['db_sha256'])
                app.store.put('backup_verification', ident, verified)
                op['steps'].append(dict(name='verify', status='succeeded'))
                return dict(backup_id=ident, label=data.get('label'), location='instance', removed=prune(app))
            return app.idempotent(route, data, lambda: operation(app, 'backup', identity, create))
    if len(pieces) == 3 and pieces[:2] == ['maintenance', 'operations'] and method == 'GET':
        return response({'maintenance_op': app.get('maintenance_op', pieces[2])})
    if len(pieces) == 4 and pieces[:2] == ['maintenance', 'backups']:
        if pieces[3] == 'verify' and method == 'POST':
            if set(data) - {'request_id'}:
                raise ValueError('verify accepts only request_id')
            def verify(op):
                path = backup_path(app, pieces[2])
                try:
                    info = inspect_backup(path, app.data_dir)
                except BackupError as exc:
                    app.store.put('backup_verification', pieces[2], dict(ok=False, at=d.timestamp(app.clock()), error=exc.result()))
                    raise
                result = dict(ok=True, at=d.timestamp(app.clock()), db_sha256=info['db_sha256'])
                app.store.put('backup_verification', pieces[2], result)
                op['steps'].append(dict(name='verify', status='succeeded'))
                return dict(backup_id=pieces[2], verification=result)
            return app.idempotent(route, data, lambda: operation(app, 'verify', identity, verify))
        if pieces[3] == 'archive' and method == 'GET':
            with verified_snapshot(backup_path(app, pieces[2]), app.data_dir) as (stage, info):
                archive = tempfile.TemporaryFile(dir=str(app.data_dir))
                try:
                    target = stage / 'export.tar'
                    deterministic_tar(stage, target)
                    digest, size = file_hash(target), target.stat().st_size
                    with target.open('rb') as src:
                        shutil.copyfileobj(src, archive)
                    archive.seek(0)
                except BaseException:
                    archive.close()
                    raise
            def stream():
                try:
                    yield from iter(lambda: archive.read(1024 * 1024), b'')
                finally:
                    archive.close()
            headers = [('Content-Type', 'application/x-tar'), ('Content-Length', str(size)),
                       ('X-Content-SHA256', digest), ('X-Archive-Bytes', str(size))]
            if app.request[1] == 'HEAD':
                archive.close()
                return Response(200, headers)
            return Response(200, headers, stream=stream())
    if route == '/maintenance/restore-preview' and method == 'GET':
        path = backup_path(app, query.get('backup_id', [''])[0])
        try:
            info = inspect_backup(path, app.data_dir)
            activity = json.loads(app._activity(info['event_seq']).body)
        except BackupError:
            activity = None
        return response(restore_preview(app.store, path, activity, app.data_dir))
    if route == '/maintenance/barrier-preview' and method == 'GET':
        return response(barrier_preview(app.store))
    raise APIError('not_found', 'maintenance route not found')


def restore_offline(data_dir, source, identity=None, dry_run=False, clock=time.time, high_water=None):
    """high_water：此前已经在线使用过的最高恢复代次（由运维记录在数据库之外）。恢复后的代次大于它、当前库与备份三者，
    当前库丢失或不可读时也不会复用已经在线出现过的执行权代次。"""
    from .controller import ControllerLock, revoke_execution, sync_directory
    if high_water is not None and (type(high_water) is not int or high_water < 0):
        raise ValueError('high_water must be a non-negative integer')
    data, source = Path(data_dir).resolve(), Path(source).resolve()
    if data == source or data in source.parents or source in data.parents:
        raise ValueError('restore source must be separate from the data directory')
    work = data.parent if data.parent.is_dir() else None
    with verified_snapshot(source, work) as (snapshot, info):
        with closing(sqlite3.connect(str(snapshot / info['manifest']['database']))) as db:
            saved = records(db)
        def plan():
            current, seq, error = current_state(data)
            identity_plan = identities(saved, current, identity)
            generation = max((current or {}).get(('meta', 'controller'), {}).get('authority_generation', 0), info['authority_generation'],
                             high_water or 0) + 1
            return current, seq, dict(op_id=str(uuid.uuid4()), kind='restore', status='succeeded', identity=identity_plan,
                                      authority_generation=generation, high_water=high_water, db_sha256=info['db_sha256'], current_error=error,
                                      requested_by={'kind': 'offline'}, requested_at=d.timestamp(clock()),
                                      started_at=d.timestamp(clock()), finished_at=d.timestamp(clock()), error=None,
                                      steps=[dict(name=name, status='succeeded') for name in ('verify', 'identity', 'fence')],
                                      result=dict(identity=identity_plan, authority_generation=generation, db_sha256=info['db_sha256']),
                                      file_effects='节点和本机项目文件写入不会被撤回。')
        if dry_run:
            _, _, result = plan()
            return dict(result, status='preview', dry_run=True)
        with ControllerLock(data):
            current, seq, result = plan()
            with tempfile.TemporaryDirectory(prefix='.restore-', dir=str(data)) as temporary:
                stage_dir = Path(temporary)
                shutil.copyfile(str(snapshot / info['manifest']['database']), str(stage_dir / 'controller.db'))
                stage = Store(stage_dir)
                try:
                    with stage.transaction():
                        stage.db.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='events'", (seq,))
                        revoke_execution(stage, result['authority_generation'], clock)
                        carry_identities(stage, saved, current, result['identity'], clock())
                        # A snapshot may predate the node consuming a queued writeback.
                        # Never redispatch it after rollback; keep the application for reconciliation.
                        unsettled = []
                        for op in stage.all('node_op'):
                            if op['status'] in ('requested', 'received'):
                                unsettled.append(dict(op_id=op['op_id'], kind=op['kind'], node_id=op['node_id']))
                                op.update(status='timed_out', finished_at=d.timestamp(clock()),
                                          error={'code': 'restore_requires_reconcile'})
                                stage.put('node_op', op['op_id'], op)
                        result['result']['unsettled_operations'] = unsettled
                        stage.put('maintenance_op', result['op_id'], result)
                        stage.event('controller.restored', result, d.timestamp(clock()))
                    stage.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                    stage.db.execute('PRAGMA journal_mode=DELETE')
                finally:
                    stage.close()
                target_blobs, source_blobs = BlobStore(data), BlobStore(snapshot)
                for entry in info['manifest']['blobs']:
                    digest, size = entry['sha256'], entry['size']
                    if not target_blobs.exists(digest, size) or file_hash(target_blobs.path(digest)) != digest:
                        target_blobs.max_bytes = max(target_blobs.max_bytes, size)
                        target_blobs.put(digest, source_blobs.read(digest))
                database = data / 'controller.db'
                if current is not None:
                    with closing(sqlite3.connect(str(database))) as live:
                        if live.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
                            raise BackupError('restore_database_busy', 'current database is busy')
                receipts = data.parent / 'restore-results'
                receipts.mkdir(mode=0o700, exist_ok=True)
                pending = receipts / ('.' + result['op_id'] + '.json')
                atomic_json(pending, result)
                for suffix in ('-wal', '-shm'):
                    sidecar = Path(str(database) + suffix)
                    if sidecar.exists():
                        sidecar.unlink()
                os.replace(str(stage_dir / 'controller.db'), str(database))
                sync_directory(data)
                os.replace(str(pending), str(receipts / (result['op_id'] + '.json')))
                sync_directory(receipts)
            return result


def main(argv=None):
    from . import cli
    parser = argparse.ArgumentParser(prog='agent-collab maintenance')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('backup', 'backups', 'verify', 'export', 'restore-preview', 'barrier-preview', 'retention', 'restore', 'inspect'):
        command = sub.add_parser(name)
        if name in ('restore', 'inspect'):
            command.add_argument('--from', dest='source', required=True)
            if name == 'restore':
                command.add_argument('--data-dir', required=True)
                command.add_argument('--identity', choices=('carry-forward', 'keep', 'revoke-all'))
                command.add_argument('--high-water', type=int, help='已在线使用过的最高恢复代次；恢复后的代次大于它')
                command.add_argument('--dry-run', action='store_true')
        else:
            cli.connection_args(command)
            if name in ('backup', 'verify'):
                command.add_argument('--request-id', default=None)
            if name == 'backup':
                command.add_argument('--label')
            if name in ('verify', 'export', 'restore-preview'):
                command.add_argument('id')
            if name == 'export':
                command.add_argument('--to', required=True)
            if name == 'retention':
                nested = command.add_subparsers(dest='action', required=True).add_parser('set')
                nested.add_argument('--keep', required=True, type=int)
                nested.add_argument('--request-id')
                nested.add_argument('--expected-revision', type=int)
    args = parser.parse_args(argv)
    try:
        if args.command == 'inspect':
            cli.out(inspect_backup(args.source))
            return 0
        if args.command == 'restore':
            cli.out(restore_offline(args.data_dir, args.source, args.identity, args.dry_run, high_water=args.high_water))
            return 0
        conn = cli.Conn(args)
        ident = quote(getattr(args, 'id', ''), safe='')
        if args.command == 'export':
            target = Path(args.to).resolve()
            with cli._open(conn.url, conn.token, 'GET', '/maintenance/backups/' + ident + '/archive',
                           ca_file=conn.ca_file, controller_id=conn.controller_id, timeout=120) as stream:
                if stream.getcode() != 200:
                    cli.out({'http_status': stream.getcode(), 'body': json.loads(stream.read())})
                    return cli.rejected(stream.getcode())
                with tempfile.NamedTemporaryFile(dir=str(target.parent), delete=False) as output:
                    temporary = Path(output.name)
                    try:
                        shutil.copyfileobj(stream, output)
                        output.flush()
                        os.fsync(output.fileno())
                    except BaseException:
                        output.close()
                        temporary.unlink()
                        raise
                try:
                    digest = file_hash(temporary)
                    if digest != stream.headers.get('X-Content-SHA256') or temporary.stat().st_size != int(stream.headers.get('Content-Length', '-1')):
                        raise BackupError('archive_hash_mismatch', 'downloaded archive hash/size mismatch')
                    size = temporary.stat().st_size
                    os.replace(str(temporary), str(target))
                finally:
                    if temporary.exists():
                        temporary.unlink()
            cli.out(dict(path=str(target), sha256=digest, bytes=size, saved=True))
            return 0
        request_id = getattr(args, 'request_id', None) or str(uuid.uuid4())
        if args.command == 'backup':
            body = dict(request_id=request_id)
            if args.label is not None:
                body['label'] = args.label
            result = conn.call('POST', '/maintenance/backups', body)
        elif args.command == 'verify':
            result = conn.call('POST', '/maintenance/backups/' + ident + '/verify', dict(request_id=request_id))
        elif args.command == 'retention':
            revision = args.expected_revision
            if revision is None:
                revision = conn.call('GET', '/settings/backups')['settings']['revision']
            result = conn.call('PUT', '/settings/backups', dict(request_id=request_id, expected_revision=revision, keep=args.keep))
        else:
            path = '/maintenance/' + args.command
            if args.command == 'restore-preview':
                path += '?backup_id=' + ident
            result = conn.call('GET', path)
        cli.out(result)
        return 1 if result.get('maintenance_op', {}).get('status') == 'failed' else 0
    except BackupError as exc:
        cli.out({'error': exc.result()})
        return 2
    except (OSError, ValueError, RuntimeError) as exc:
        cli.out({'error': dict(code='maintenance_failed', message=str(exc))})
        return 2
    except cli.Unreachable:
        cli.out({'error': dict(code='unreachable', message='controller 无法连接；离线恢复须在目标主机执行。')})
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
