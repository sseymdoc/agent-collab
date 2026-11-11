"""在隔离副本上预演原位升级：python -m agent_collab.rehearse --backup <备份集目录> --work <空目录>。

把备份集恢复到 work 目录，用当前版本的代码打开它，核对升级前后的身份、契约、历史与模型设置迁移。
备份集的清单、数据库与 blob 内容不变（恢复过程会在备份集目录里留下空的锁文件与数据库旁文件），
也不接触正在运行的 controller 的数据目录。恢复出来的副本派单暂停、恢复代次加一、已有 attempt 全部撤销，
并且全程不监听端口：它接不到任何节点，节点也不会把它当成原来的 controller。
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys

from . import domain as d, protocol as p
from .blobstore import BlobStore
from .controller import App, database_references, file_hash, restore
from .preflight import _validate_output


def _records(db):
    return {(kind, ident): json.loads(data) for kind, ident, data in db.execute('SELECT kind,id,data FROM records')}


def _of(records, kind):
    return {ident: value for (k, ident), value in records.items() if k == kind}


def _events(db, upto=None):
    """事件表（序号不大于 upto 的部分）的条数、最大序号与内容摘要：恢复只追加事件，原有的行必须逐条不变。"""
    digest, count, last = hashlib.sha256(), 0, None
    rows = db.execute('SELECT seq, event_id, job_id, data FROM events WHERE seq <= ? ORDER BY seq', (1 << 62 if upto is None else upto,))
    for row in rows:
        # 归属列（event_id、job_id）与内容一起比较：只改归属、不改 data 也算历史被改动。
        digest.update(json.dumps([v.decode('utf-8', 'replace') if isinstance(v, bytes) else v for v in row]).encode('utf-8') + b'\n')
        count, last = count + 1, row[0]
    return count, last, digest.hexdigest()


def _inputs(backup_dir):
    """预演会读取的备份集文件（清单、数据库、blob），按跟随符号链接后的真实路径给出。

    输出路径校验遍历目录时不跟随符号链接；备份集里的符号链接可能指向目录外的文件，单独列出才能防住它及其硬链接。
    """
    backup = Path(backup_dir)
    paths = [backup / 'backup-manifest.json']
    try:
        manifest = json.loads(paths[0].read_text(encoding='utf-8'))
        paths.append(backup / manifest.get('database', 'controller.db'))
        blobs = BlobStore(backup)
        paths += [blobs.path(entry['sha256']) for entry in manifest.get('blobs', [])]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass  # 清单读不出来时预演本身会失败；这里只保护能确定的文件
    return [str(path.resolve()) for path in paths if path.exists()]


def rehearse(backup_dir, work_dir, config=None):
    backup_dir, work = Path(backup_dir).resolve(), Path(work_dir).resolve()
    if work.exists() and any(work.iterdir()):
        raise ValueError('work directory must be empty or absent')
    if work == backup_dir or backup_dir in work.parents or work in backup_dir.parents:
        raise ValueError('work directory must be separate from the backup set')
    manifest_bytes = (backup_dir / 'backup-manifest.json').read_bytes()
    manifest = json.loads(manifest_bytes.decode('utf-8'))
    source = sqlite3.connect((backup_dir / manifest.get('database', 'controller.db')).as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        before = _records(source)
        events_before = _events(source)
    finally:
        source.close()
    data = work / 'controller'
    generation = restore(data, backup_dir)
    if config:
        # 配置文件不在备份集里；给出时复制进副本，模型设置迁移据此取协调 profile。
        shutil.copyfile(str(config), str(data / 'config.json'))
    checks = []

    def check(ident, ok, detail):
        checks.append({'id': ident, 'ok': bool(ok), 'detail': detail})

    app = App(data)
    try:
        after = _records(app.store.db)
        meta_before, meta_after = before[('meta', 'controller')], after[('meta', 'controller')]
        check('controller_identity', meta_before['controller_id'] == meta_after['controller_id'], meta_after['controller_id'])
        check('authority_generation_advanced', meta_after['authority_generation'] == generation > meta_before['authority_generation'],
              {'before': meta_before['authority_generation'], 'after': generation})
        check('dispatch_paused', meta_after['dispatch_paused'] is True, '副本不派单')
        identity = lambda nodes: {k: {f: v.get(f) for f in ('node_id', 'physical_host_id', 'failure_domain')} for k, v in nodes.items()}
        check('node_identities', identity(_of(before, 'node')) == identity(_of(after, 'node')), sorted(_of(after, 'node')))
        check('node_credentials', set(_of(before, 'node_token')) == set(_of(after, 'node_token')),
              {'count': len(_of(after, 'node_token'))})  # 只比较哈希集合，不输出
        jobs_before, jobs_after = _of(before, 'job'), _of(after, 'job')
        frozen = ('contract', 'request_id', 'request_hash', 'work_package_id', 'inputs_manifest_sha256', 'created_at')
        changed = [k for k in jobs_before if k not in jobs_after or any(jobs_before[k].get(f) != jobs_after[k].get(f) for f in frozen)]
        check('job_contracts_immutable', not changed and set(jobs_before) == set(jobs_after), {'jobs': len(jobs_after), 'changed': changed[:20]})
        ended = [k for k, v in jobs_before.items() if v['status'] in ('completed', 'failed', 'cancelled')]
        check('finished_jobs_unchanged', all(jobs_before[k]['status'] == jobs_after[k]['status'] and
                                             jobs_before[k]['delivery_version'] == jobs_after[k]['delivery_version'] for k in ended),
              {'finished': len(ended), 'held_for_reconcile': len(jobs_before) - len(ended)})
        for kind in ('checkpoint', 'publication', 'assessment'):
            check(kind + '_history', _of(before, kind) == _of(after, kind), {'count': len(_of(after, kind))})
        attempts = _of(after, 'attempt')
        # 旧 attempt 的迟到结果：副本的恢复代次已经前进，它们的心跳、检查点、完成与发布都会被隔离。
        reasons = {app.fence_reason(a, {'epoch': a['epoch'], 'authority_generation': a['authority_generation'], 'lease_token': ''})
                   for a in attempts.values()}
        check('late_results_fenced', None not in reasons and all(v['revoked'] for v in _of(after, 'attempt_private').values()),
              {'attempts': len(attempts), 'reasons': sorted(r for r in reasons if r)})
        settings = app.model_settings()
        had = ('settings', 'models') in before
        detail = {'carried_over': had, 'settings': {k: settings[k] for k in ('revision', 'codex', 'claude')}}
        if had:
            ok = settings == before[('settings', 'models')]
        elif jobs_before or _of(before, 'node'):
            # 升级前的部署：迁移值必须就是 §12.3 规定的组合与当时生效的协调 profile。profile 在 config.json 里，
            # 而它不在备份集中；没给 --config 时无法确认，不算通过。
            expected = d.migrated_settings(d.managed_profile(app.config)['profile'])
            ok = (config is not None and (settings['updated_by'] or {}).get('kind') == 'migration'
                  and all(settings[k] == expected[k] for k in ('codex', 'claude')))
            if config is None:
                detail['unconfirmed'] = 'config_required'
        else:
            ok = settings['updated_by'] is None and all(settings[k] == d.EMPTY_SETTINGS[k] for k in ('codex', 'claude'))
        check('model_settings', ok, detail)
        policies = {'frozen': 0, 'legacy': 0}
        for root_id in _of(after, 'root'):
            policies['legacy' if app.root_policy(root_id).get('legacy') else 'frozen'] += 1
        check('root_policies_readable', True, policies)
        try:
            references = database_references(app.store.db, app.blobs)
            check('durable_artifacts_present', True, {'blobs': len(references)})
        except ValueError as exc:
            check('durable_artifacts_present', False, str(exc))
        kept = _events(app.store.db, events_before[1] if events_before[1] is not None else 0)
        check('event_history_kept', kept == events_before,
              {'before': events_before[0], 'after': app.store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]})
        check('backup_content_unchanged', (backup_dir / 'backup-manifest.json').read_bytes() == manifest_bytes and
              file_hash(backup_dir / manifest.get('database', 'controller.db')) == manifest['db_sha256'], manifest['db_sha256'])
        nodes = _of(after, 'node').values()
        without = sorted(n['node_id'] for n in nodes if 'models' not in n['capabilities'])
        report = {'schema': 1, 'backup': {k: manifest.get(k) for k in ('db_sha256', 'created_at', 'authority_generation')},
                  'work': str(work), 'counts': {k: len(_of(after, k)) for k in ('job', 'attempt', 'node', 'checkpoint', 'client', 'baseline')},
                  'checks': checks, 'ok': all(c['ok'] for c in checks),
                  # 这些节点还没有上报模型目录：升级后只按旧适配器的固定组合给它们派单，先升级节点再升级 controller。
                  'nodes_without_model_catalog': without}
    finally:
        app.close()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', required=True, help='controller backup 生成的备份集目录')
    parser.add_argument('--work', required=True, help='空目录；副本恢复到这里，预演后可整个删除')
    parser.add_argument('--config', help='生产 controller 的 config.json（不在备份集里）；给出时复制进副本')
    parser.add_argument('--out', help='报告文件，缺省输出到标准输出')
    args = parser.parse_args(argv)
    try:
        # 报告不能覆盖备份集（含硬链接别名）或输入的配置文件。
        # 预演生成的副本数据目录（work/controller）同样不能被报告覆盖；work 下的其他位置可以。
        protected = [args.backup, str(Path(args.work) / 'controller')]
        inputs = _inputs(args.backup) + ([args.config] if args.config else [])
        output = _validate_output(Path(args.out), protected, inputs) if args.out else None
        report = rehearse(args.backup, args.work, args.config)
    except (OSError, ValueError, KeyError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps(p.error_body('invalid', type(exc).__name__ + ': ' + str(exc)), ensure_ascii=False), file=sys.stderr)
        return 2
    text = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if output:
        output.write_text(text, encoding='utf-8')
    else:
        sys.stdout.write(text)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
