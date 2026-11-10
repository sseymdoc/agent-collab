import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
import unittest

from agent_collab import domain as d, protocol as p
from helpers import ControllerCase, contract, manifest, uid


class JobsTest(ControllerCase):
    def test_idempotency_conflict_and_version(self):
        data = {'request_id': uid(), 'title': 'one', 'contract': contract()}
        job = self.call('POST', '/jobs', data, status=201)
        self.assertEqual(job, self.call('POST', '/jobs', data))
        error = self.call('POST', '/jobs', dict(data, title='two'), status=409)
        self.assertEqual(error['error']['code'], 'conflict')
        error = self.mutate(job, 'cancel', expected_status=409, expected_version=0)
        self.assertEqual(error['error']['code'], 'version_mismatch')
        self.assertEqual(error['error']['details']['current']['job_id'], job['job_id'])
        cancel = {'request_id': uid(), 'expected_version': self.current(job)['version']}
        first = self.call('POST', '/jobs/' + job['job_id'] + '/cancel', cancel)
        self.assertEqual(first, self.call('POST', '/jobs/' + job['job_id'] + '/cancel', cancel))
        self.assertEqual(first['status'], 'cancelled')

    def test_concurrent_job_deduplication(self):
        data = p.canonical_json({'request_id': uid(), 'contract': contract()})
        def create(_):
            return self.app.handle('POST', '/api/v1/jobs', {'authorization': 'Bearer ' + self.admin}, data)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(create, range(8)))
        self.assertEqual(sum(r.status == 201 for r in results), 1)
        self.assertEqual(len({json.loads(r.body)['job_id'] for r in results}), 1)

    def test_inputs_missing_protection_and_hash(self):
        node = self.node()
        content = b'baseline'
        job = self.job(contract(inputs_manifest=manifest(content)))
        self.assertEqual(job['protection'], 'preparing')
        self.assertIn(p.sha256_bytes(content), job['status_reason'])
        node.claim(status=204)
        node.upload(content)
        current = self.current(job)
        self.assertEqual(current['protection'], 'protected')
        a = node.claim()
        self.assertEqual(a['start']['kind'], 'inputs')
        self.assertEqual(a['start']['manifest_sha256'], current['inputs_manifest_sha256'])
        self.assertEqual(self.app.blobs.read(current['inputs_manifest_sha256']), p.canonical_json(manifest(content)))

    def test_unprotected_can_dispatch_but_cannot_auto_recover(self):
        node = self.node()
        job = self.job(contract(inputs_manifest=None, recovery={'auto': False}))
        self.assertEqual(job['protection'], 'unprotected')
        a = node.claim()
        node.complete(a, outcome='failed', failure_class='network')
        self.app.tick()
        self.assertEqual(self.current(job)['status'], 'waiting_reconcile')
        self.assertEqual(len(self.current(job)['attempts']), 1)

    def test_contract_validation_and_runtime_truth(self):
        for params in ({'effort': 'low'}, {'model': 'wrong'}, {'sandbox': 'danger-full-access'}):
            self.call('POST', '/jobs', {'request_id': uid(), 'contract': contract(harness='codex', harness_params=params)}, status=400)
        node = self.node()
        job = self.job(contract(harness='codex', harness_params={}))
        a = node.claim()
        self.assertEqual(a['contract']['harness_params']['effort'], 'xhigh')
        self.assertIsNone(self.current(job)['attempts'][0]['runtime']['observed'])

    def test_root_budget_and_serial_repairs(self):
        node = self.node()
        c = contract(budgets={'max_attempts': 2, 'max_repairs': 1})
        one = self.job(c)
        self.call('POST', '/jobs', {'request_id': uid(), 'work_package_id': one['work_package_id'], 'contract': c}, status=409)
        a = node.claim()
        node.complete(a, outcome='failed', failure_class='code_failure')
        two = self.job(c, work_package_id=one['work_package_id'])
        self.assertEqual(two['counters'], dict(attempts=2, infra_retries=0, migrations=0, repairs=1))
        self.assertEqual(self.current(one)['counters'], two['counters'])
        b = node.claim()
        node.complete(b, outcome='failed', failure_class='code_failure')
        err = self.call('POST', '/jobs', {'request_id': uid(), 'work_package_id': one['work_package_id'], 'contract': c}, status=409)
        self.assertEqual(err['error']['code'], 'budget_exhausted')

    def test_job_list_cursor_survives_status_change(self):
        a, b, c = self.job(), self.job(), self.job()
        status = self.current(c)['status']
        page = lambda q: [j['job_id'] for j in self.call('GET', '/jobs?limit=1&status=' + status + q)['jobs']]
        self.assertEqual(page(''), [c['job_id']])
        self.mutate(c, 'cancel')
        self.assertNotEqual(self.current(c)['status'], status)
        # 游标所指的 job 已不在筛选结果中，翻页仍按创建顺序继续。
        self.assertEqual(page('&before=' + c['job_id']), [b['job_id']])
        self.assertEqual(page('&before=' + b['job_id']), [a['job_id']])
        self.call('GET', '/jobs?before=' + uid(), status=400)

    def test_queries_diff_doctor_and_shape(self):
        node = self.node()
        job = self.job()
        a = node.claim()
        node.upload(b'new')
        cp = node.checkpoint(a, manifest(b'new', 'new.txt'))
        diff = self.call('GET', '/checkpoints/' + cp['checkpoint_id'] + '/diff?against=inputs')
        self.assertEqual(diff, {'added': ['new.txt'], 'modified': [], 'deleted': []})
        self.assertEqual(self.call('GET', '/checkpoints/' + cp['checkpoint_id']), cp)
        self.assertEqual(len(self.call('GET', '/jobs?limit=1')['jobs']), 1)
        self.assertEqual(self.call('GET', '/nodes')['nodes'][0]['active_attempts'], 1)
        self.assertEqual(self.call('GET', '/overview')['counts']['running'], 1)
        doctor = self.call('GET', '/doctor')
        self.assertEqual(doctor['database'], {'writable': True, 'journal_mode': 'wal'})
        self.assertEqual(doctor['backup_age_s'], 'unknown')
        expected = {'job_id', 'request_id', 'request_hash', 'work_package_id', 'title', 'contract', 'status', 'status_reason',
                    'protection', 'inputs_manifest_sha256', 'current_epoch', 'current_attempt_id', 'durable_checkpoint_id',
                    'durable_checkpoint_at', 'acceptance', 'delivery_version', 'delivery_checkpoint_id', 'integration_status',
                    'counters', 'cancel', 'version', 'created_at', 'updated_at'}
        self.assertEqual(set(job), expected)


class SchedulingTest(ControllerCase):
    def test_all_capability_rejection_reasons(self):
        node = self.node()
        job = self.job(contract(requirements={'os': ['windows'], 'arch': ['arm64'], 'tools': ['xcode'], 'gpu': 'CUDA', 'labels': ['secret']}))
        explanation = self.call('GET', '/jobs/' + job['job_id'] + '/explain')
        reasons = explanation['nodes'][0]['reasons']
        for reason in ('os_mismatch', 'arch_mismatch', 'tool_missing:xcode', 'gpu_missing', 'label_missing:secret'):
            self.assertIn(reason, reasons)
            self.assertIn(reason, job['status_reason'])
        self.assertEqual(job['status'], 'waiting_capability')
        self.assertIsNone(explanation['selected_node_id'])
        self.assertEqual(sum(explanation['nodes'][0]['breakdown'].values()), explanation['nodes'][0]['score'])

    def test_harness_auth_draining_paused_and_excluded(self):
        node = self.node(auth_health={'codex': 'expired'})
        job = self.job(contract(harness='codex', harness_params={}))
        self.assertIn('auth_unhealthy:codex', job['status_reason'])
        node.record['capabilities']['harness'] = {}
        self.call('POST', '/nodes/register', node.record, token=node.token)
        current = self.call('GET', '/nodes')['nodes'][0]
        self.call('POST', '/nodes/a/drain', {'request_id': uid(), 'expected_version': current['version'], 'drain': True})
        self.call('POST', '/dispatch', {'request_id': uid(), 'expected_version': self.app.meta()['version'], 'paused': True})
        self.mutate(job, 'retry', reason='exclude', exclude_node_ids=['a'])
        reasons = self.call('GET', '/jobs/' + job['job_id'] + '/explain')['nodes'][0]['reasons']
        for reason in ('harness_missing:codex', 'draining', 'dispatch_paused', 'excluded_by_request'):
            self.assertIn(reason, reasons)

    def test_suspect_unreachable(self):
        self.node()
        job = self.job(contract(requirements={'tools': ['xcode']}))
        self.clock.advance(31)
        reasons = self.call('GET', '/jobs/' + job['job_id'] + '/explain')['nodes'][0]['reasons']
        self.assertIn('node_suspect', reasons)
        self.clock.advance(60)
        reasons = self.call('GET', '/jobs/' + job['job_id'] + '/explain')['nodes'][0]['reasons']
        self.assertIn('node_unreachable', reasons)

    def test_windows_wsl_share_minimum_host_capacity(self):
        windows = self.node('windows', physical_host_id='pc', resources={'slots': 2, 'host_slots': 2})
        wsl = self.node('wsl', physical_host_id='pc', resources={'slots': 2, 'host_slots': 1})
        first = self.job()
        second = self.job()
        self.assertEqual(len(self.current(first)['attempts']), 1)
        self.assertEqual(len(self.current(second)['attempts']), 0)
        for row in self.call('GET', '/jobs/' + second['job_id'] + '/explain')['nodes']:
            self.assertIn('no_host_slot:pc', row['reasons'])

    def test_node_slots_and_multislot_jobs(self):
        self.node(resources={'slots': 1, 'host_slots': 3})
        self.job()
        second = self.job()
        reasons = self.call('GET', '/jobs/' + second['job_id'] + '/explain')['nodes'][0]['reasons']
        self.assertIn('no_node_slot', reasons)
        self.assertNotIn('no_host_slot:a', reasons)

    def test_new_peer_cannot_lower_capacity_below_live_reservations(self):
        first = self.node('a', physical_host_id='pc')
        self.job(contract(resources={'slots': 2}))
        code = self.app.pairing_code('b')
        credential = self.call('POST', '/nodes/pair', {'code': code, 'node_id': 'b'}, headers={})
        record = copy.deepcopy(first.record)
        record.update(node_id='b', resources={'slots': 1, 'host_slots': 1})
        self.call('POST', '/nodes/register', record, token=credential['node_token'], status=409)
        self.assertEqual(len(self.call('GET', '/nodes')['nodes']), 1)

    def test_quota_blocks_shared_account_across_nodes(self):
        first, second = self.node('a'), self.node('b')
        job = self.job(contract(harness='codex', harness_params={}))
        assignment = first.claim()
        first.complete(assignment, outcome='failed', failure_class='quota')
        current = self.current(job)
        self.assertEqual(current['status'], 'waiting_capability')
        fresh = self.job(contract(harness='codex', harness_params={}))
        for row in self.call('GET', '/jobs/' + fresh['job_id'] + '/explain')['nodes']:
            self.assertIn('account_quota_blocked:account', row['reasons'])
        second.claim(status=204)
        self.clock.advance(901)
        second.heartbeat([])
        second.claim()
        self.assertEqual(len(self.current(job)['attempts']), 1)

    def test_domain_is_pure(self):
        self.node()
        job = self.job()
        records = (job, self.app.store.all('node'), self.app.store.all('attempt'))
        before = copy.deepcopy(records)
        d.explain(job, records[1], records[2], {job['job_id']: job}, self.clock(), self.app.config)
        self.assertEqual(records, before)


class CheckpointTest(ControllerCase):
    def test_half_uploaded_checkpoint_promotes_atomically(self):
        node = self.node()
        job = self.job()
        a = node.claim()
        ident, content = uid(), b'output'
        error = node.checkpoint(a, manifest(content), checkpoint_id=ident, status=409)
        self.assertEqual(error['error']['code'], 'missing_blobs')
        cp = self.call('GET', '/checkpoints/' + ident)
        self.assertEqual(cp['state'], 'uploading')
        self.assertIsNone(self.current(job)['durable_checkpoint_id'])
        digest = p.sha256_bytes(content)
        self.call('PUT', '/blobs/' + digest, b'corrupt', token=node.token, status=400)
        self.assertFalse(self.app.blobs.path(digest).exists())
        self.assertEqual(list(self.app.blobs.root.rglob('.upload-*')), [])
        node.upload(content)
        cp = self.call('GET', '/checkpoints/' + ident)
        self.assertEqual(cp['state'], 'durable')
        self.assertEqual(self.current(job)['durable_checkpoint_id'], ident)
        same = node.checkpoint(a, manifest(content), checkpoint_id=ident)
        self.assertEqual(same, cp)
        self.assertEqual(node.checkpoint(a, manifest(content), checkpoint_id=ident, kind='final', status=409)['error']['code'], 'conflict')

    def test_size_mismatch_and_foreign_base(self):
        node = self.node()
        self.job()
        a = node.claim()
        node.upload(b'one')
        value = manifest(b'one')
        value['files'][0]['size'] = 100
        self.assertEqual(node.checkpoint(a, value, status=409)['error']['code'], 'missing_blobs')
        self.assertEqual(node.checkpoint(a, manifest(base='a' * 64), status=400)['error']['code'], 'invalid')

    def test_older_upload_cannot_replace_newer_durable_checkpoint(self):
        node = self.node()
        job = self.job()
        a = node.claim()
        node.checkpoint(a, manifest(b'late'), status=409)
        newest = node.checkpoint(a)
        node.upload(b'late')
        self.assertEqual(self.current(job)['durable_checkpoint_id'], newest['checkpoint_id'])

    def test_transaction_rolls_back_checkpoint_and_event_together(self):
        node = self.node()
        job = self.job()
        a = node.claim()
        count = len(self.app.store.all('checkpoint'))
        seq = self.app.store.db.execute('SELECT MAX(seq) FROM events').fetchone()[0]
        original = self.app.store.event
        def fail(*args, **kwargs):
            raise ValueError('injected write failure')
        self.app.store.event = fail
        try:
            node.checkpoint(a, status=400)
        finally:
            self.app.store.event = original
        self.assertEqual(len(self.app.store.all('checkpoint')), count)
        self.assertEqual(self.app.store.db.execute('SELECT MAX(seq) FROM events').fetchone()[0], seq)
        self.assertIsNone(self.current(job)['durable_checkpoint_id'])


if __name__ == '__main__':
    unittest.main()
