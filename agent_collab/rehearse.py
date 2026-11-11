"""在隔离副本上预演原位升级：python -m agent_collab.rehearse --backup <备份集目录> --work <空目录>。

把备份集恢复到 work 目录，用当前版本的代码打开它，核对升级前后的身份、契约、历史与模型设置迁移。
备份集的清单、数据库与 blob 内容不变（恢复过程会在备份集目录里留下空的锁文件与数据库旁文件），
也不接触正在运行的 controller 的数据目录。恢复出来的副本派单暂停、恢复代次加一、已有 attempt 全部撤销，
并且全程不监听端口：它接不到任何节点，节点也不会把它当成原来的 controller。
"""
import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import sys

from . import protocol as p
from .controller import App, database_references, file_hash, restore


def _records(db):
    return {(kind, ident): json.loads(data) for kind, ident, data in db.execute('SELECT kind,id,data FROM records')}


def _of(records, kind):
    return {ident: value for (k, ident), value in records.items() if k == kind}


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
        events_before = source.execute('SELECT COUNT(*) FROM events').fetchone()[0]
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
        check('model_settings', settings == before[('settings', 'models')] if had else
              ((settings['updated_by'] or {}).get('kind') == 'migration') == bool(jobs_before or _of(before, 'node')),
              {'carried_over': had, 'settings': {k: settings[k] for k in ('revision', 'codex', 'claude')}})
        policies = {'frozen': 0, 'legacy': 0}
        for root_id in _of(after, 'root'):
            policies['legacy' if app.root_policy(root_id).get('legacy') else 'frozen'] += 1
        check('root_policies_readable', True, policies)
        try:
            references = database_references(app.store.db, app.blobs)
            check('durable_artifacts_present', True, {'blobs': len(references)})
        except ValueError as exc:
            check('durable_artifacts_present', False, str(exc))
        events_after = app.store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        check('event_history_kept', events_after >= events_before, {'before': events_before, 'after': events_after})
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
        report = rehearse(args.backup, args.work, args.config)
    except (OSError, ValueError, KeyError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps(p.error_body('invalid', type(exc).__name__ + ': ' + str(exc)), ensure_ascii=False), file=sys.stderr)
        return 2
    text = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.out:
        Path(args.out).write_text(text, encoding='utf-8')
    else:
        sys.stdout.write(text)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
