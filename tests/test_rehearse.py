"""隔离副本上的升级预演：身份、契约与历史不变，模型设置按规则迁移，旧 attempt 的结果被隔离，备份集与原数据目录不被改动。"""
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid
from unittest.mock import patch

from agent_collab import rehearse
from agent_collab.controller import backup
from helpers import ControllerCase, contract, manifest
import test_coordinator as tc


def digest(root):
    # 恢复流程会在备份集目录里留下空的数据库旁文件（-wal、-shm，是否留下因平台而异）：比较的是清单、数据库与 blob 的内容。
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(Path(root).rglob('*'))
            if path.is_file() and not path.name.endswith(('-wal', '-shm'))}


class RehearseTests(ControllerCase):
    def old_deployment(self):
        """升级前的数据库：有节点、已结束与执行中的任务，没有模型设置记录，根工作包没有冻结策略。"""
        worker = self.node('worker')
        done = self.job(contract(harness='codex', harness_params={}))
        attempt = worker.claim()
        content = b'result'
        worker.upload(content)
        checkpoint = worker.checkpoint(attempt, manifest(content, 'out.txt'), kind='final')
        worker.complete(attempt, checkpoint=checkpoint, report={})
        worker.publish(attempt, checkpoint)
        running = self.job()
        active = worker.claim()
        with self.app.store.transaction():
            self.app.store.delete('settings', 'models')
            for job in self.app.store.all('job'):
                job.pop('model_policy'), job.pop('created_by')
                self.app.store.put('job', job['job_id'], job)
            for (ident,) in self.app.store.db.execute("SELECT id FROM records WHERE kind='root'").fetchall():
                root = self.app.store.get('root', ident)
                root.pop('model_policy')
                self.app.store.put('root', ident, root)
        return done, running, active

    def test_rehearsal_checks_identity_history_settings_and_fencing_without_touching_sources(self):
        done, running, active = self.old_deployment()
        saved = backup(self.temp / 'data', self.temp / 'backup', clock=self.clock)
        config = self.temp / 'config.json'
        config.write_text(json.dumps(tc.CONFIG), encoding='utf-8')
        sources = digest(self.temp / 'backup'), self.current(done), self.current(running)['status']
        report = rehearse.rehearse(self.temp / 'backup', self.temp / 'work', config)
        self.assertTrue(report['ok'], [c for c in report['checks'] if not c['ok']])
        checks = {c['id']: c['detail'] for c in report['checks']}
        self.assertEqual(set(checks), {'controller_identity', 'authority_generation_advanced', 'dispatch_paused', 'node_identities',
                                       'node_credentials', 'job_contracts_immutable', 'finished_jobs_unchanged', 'checkpoint_history',
                                       'publication_history', 'assessment_history', 'late_results_fenced', 'model_settings',
                                       'root_policies_readable', 'durable_artifacts_present', 'event_history_kept', 'backup_content_unchanged'})
        self.assertEqual(checks['controller_identity'], self.app.controller_id)
        self.assertEqual(checks['authority_generation_advanced'], {'before': 1, 'after': 2})
        self.assertEqual(checks['finished_jobs_unchanged'], {'finished': 1, 'held_for_reconcile': 1})
        self.assertEqual(checks['late_results_fenced'], {'attempts': 2, 'reasons': ['stale_generation']})
        settings = checks['model_settings']['settings']
        self.assertEqual((checks['model_settings']['carried_over'], settings['revision']), (False, 1))
        self.assertEqual(settings['codex'], {'default': {'model': 'gpt-6-astra', 'effort': 'xhigh'}, 'allowed': {'gpt-6-astra': ['high', 'xhigh', 'max']}})
        self.assertEqual(settings['claude'], {'default': {'model': 'configured-model', 'effort': 'configured-effort'}})
        self.assertEqual(checks['root_policies_readable'], {'frozen': 0, 'legacy': 2})
        self.assertEqual((report['counts']['job'], report['nodes_without_model_catalog'], report['backup']['db_sha256']), (2, ['worker'], saved['db_sha256']))
        self.assertNotIn(self.admin, json.dumps(report))
        # 备份集与正在使用的数据目录都没有被改动：原 controller 上执行中的任务照常续租。
        self.assertEqual((digest(self.temp / 'backup'), self.current(done), self.current(running)['status']), sources)
        self.node('worker-2')  # 原 controller 仍可正常服务
        self.assertEqual(self.app.meta()['authority_generation'], 1)
        self.assertEqual(self.call('POST', '/nodes/worker/heartbeat', {'authority_generation': 1, 'attempts': [
            {'attempt_id': active['attempt_id'], 'epoch': active['epoch'], 'state': 'running'}]}, token=self.app_token('worker'))['renewed'],
            [active['attempt_id']])

    def app_token(self, node_id):
        return self._tokens[node_id]

    def node(self, node_id='a', **changes):
        created = super().node(node_id, **changes)
        self.__dict__.setdefault('_tokens', {})[node_id] = created.token
        return created

    def test_existing_settings_are_carried_over_and_bad_arguments_are_refused(self):
        self.job()
        expected = self.app.model_settings()
        backup(self.temp / 'data', self.temp / 'backup', clock=self.clock)
        report = rehearse.rehearse(self.temp / 'backup', self.temp / 'work')
        detail = next(c for c in report['checks'] if c['id'] == 'model_settings')
        self.assertTrue(report['ok'])
        self.assertEqual((detail['detail']['carried_over'], detail['detail']['settings']['codex']), (True, expected['codex']))
        with self.assertRaises(ValueError):
            rehearse.rehearse(self.temp / 'backup', self.temp / 'work')  # 目录已非空
        with self.assertRaises(ValueError):
            rehearse.rehearse(self.temp / 'backup', self.temp / 'backup' / 'inside')
        self.assertEqual(rehearse.main(['--backup', str(self.temp / 'missing'), '--work', str(self.temp / 'w2')]), 2)

    def test_legacy_settings_cannot_be_confirmed_without_the_production_config(self):
        self.old_deployment()
        backup(self.temp / 'data', self.temp / 'backup', clock=self.clock)
        report = rehearse.rehearse(self.temp / 'backup', self.temp / 'work')
        detail = next(c for c in report['checks'] if c['id'] == 'model_settings')
        self.assertEqual((detail['ok'], detail['detail']['unconfirmed'], report['ok']), (False, 'config_required', False))

    def test_lost_event_rows_are_detected_even_when_the_count_grows(self):
        self.old_deployment()
        backup(self.temp / 'data', self.temp / 'backup', clock=self.clock)
        config = self.temp / 'config.json'
        config.write_text(json.dumps(tc.CONFIG), encoding='utf-8')
        real = rehearse.restore

        def lossy(data, source):
            generation = real(data, source)
            with sqlite3.connect(str(data / 'controller.db')) as db:
                first = db.execute('SELECT MIN(seq) FROM events').fetchone()[0]
                db.execute('DELETE FROM events WHERE seq=?', (first,))
                for _ in range(3):  # 补上更多新事件：只比较条数时会被掩盖
                    db.execute("INSERT INTO events(event_id, job_id, data) VALUES(?, NULL, '{}')", (str(uuid.uuid4()),))
            db.close()
            return generation
        with patch.object(rehearse, 'restore', lossy):
            report = rehearse.rehearse(self.temp / 'backup', self.temp / 'work', config)
        self.assertFalse(next(c for c in report['checks'] if c['id'] == 'event_history_kept')['ok'])

    def test_report_may_not_overwrite_the_backup_set_or_the_config(self):
        self.job()
        backup(self.temp / 'data', self.temp / 'backup', clock=self.clock)
        config = self.temp / 'config.json'
        config.write_text(json.dumps(tc.CONFIG), encoding='utf-8')
        sources = digest(self.temp / 'backup'), config.read_bytes()
        for n, out in enumerate((self.temp / 'backup' / 'backup-manifest.json', config)):
            with patch('sys.stderr'):
                code = rehearse.main(['--backup', str(self.temp / 'backup'), '--work', str(self.temp / ('w%d' % n)),
                                      '--config', str(config), '--out', str(out)])
            self.assertEqual(code, 2)
        self.assertEqual((digest(self.temp / 'backup'), config.read_bytes()), sources)
