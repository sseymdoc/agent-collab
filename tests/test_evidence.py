"""WP5 evidence and safe drain migration, through the socket-free public API."""
import copy
import json
from unittest.mock import patch

from agent_collab import artifacts, protocol as p
from helpers import ControllerCase, contract, manifest, uid
from test_coordinator import CONFIG


class EvidenceTests(ControllerCase):
    def test_workspace_defaults_validation_and_request_hash(self):
        job = self.job()
        self.assertEqual(job['contract']['workspace'], {'exclude': artifacts.DEFAULT_EXCLUDES})
        for workspace in (None, [], {'other': []}, {'exclude': None}, {'exclude': ['']},
                          {'exclude': ['../x']}, {'exclude': ['/x']}, {'exclude': ['x/../y']},
                          {'exclude': ['C:/x']}, {'exclude': ['a\\b']}, {'exclude': ['a//b']},
                          {'exclude': ['a/./b']}, {'exclude': ['a\x00b']}, {'exclude': [1]}):
            with self.subTest(workspace=workspace):
                self.call('POST', '/jobs', dict(request_id=uid(), contract=contract(workspace=workspace)), status=400)
        request = dict(request_id=uid(), contract=contract(workspace={'exclude': ['build/**']}))
        first = self.call('POST', '/jobs', request, status=201)
        self.assertEqual(first['request_hash'], p.value_hash(request))
        self.assertEqual(self.call('POST', '/jobs', request), first)
        request['contract']['workspace']['exclude'] = []
        self.call('POST', '/jobs', request, status=409)

    def test_complete_report_object_and_canonical_utf8_size_boundary(self):
        node = self.node()
        job = self.job()
        attempt = node.claim()
        cp = node.checkpoint(attempt, kind='final')
        # null 保持兼容（旧 agent 发送 report=null，已在线上节点运行），其余非对象值拒绝。
        for value in ([], 'text', 0, False):
            with self.subTest(value=value):
                node.complete(attempt, checkpoint=cp, report=value, status=400)
        report = {'harness': 'codex', 'usage': {'input_tokens': 7, 'output_tokens': 3}, 'text': ''}
        room = 65536 - len(p.canonical_json(report))
        report['text'] = '文' * (room // 3) + 'x' * (room % 3)
        self.assertEqual(len(p.canonical_json(report)), 65536)
        oversized = dict(report, text=report['text'] + 'x')
        node.complete(attempt, checkpoint=cp, report=oversized, status=413)
        self.assertIsNone(self.current(job)['attempts'][0]['report'])
        completed = node.complete(attempt, checkpoint=cp, report=report)
        self.assertEqual(completed['report'], report)
        self.assertEqual(node.complete(attempt, checkpoint=cp, report=report), completed)
        self.restart()
        self.assertEqual(self.current(job)['attempts'][0]['report'], report)

    def context(self, job):
        coordinator = next(j for j in reversed(self.app.store.all('job'))
                           if j['contract'].get('coordinates', {}).get('job_id') == job['job_id'])
        entry = next(f for f in coordinator['contract']['inputs_manifest']['files'] if f['path'] == 'context.json')
        return json.loads(self.app.blobs.read(entry['sha256']))

    def test_context_text_sorted_byte_limits_omission_reasons_and_report(self):
        self.app.config.update(copy.deepcopy(CONFIG))
        node = self.node()
        job = self.job(contract(workspace={'exclude': ['private/**']}))
        attempt = node.claim()
        contents = {'a-binary': b'\xff', 'b-large': b'x' * 32769, 'private/secret': b'not for context'}
        for i in range(8):
            contents['text-%d' % i] = ('文' * 10922 + 'xx').encode('utf-8')
        contents['z-budget'] = b'one byte beyond total'
        files = []
        for name, raw in reversed(list(contents.items())):
            files.append(dict(path=name, size=len(raw), sha256=node.upload(raw), executable=False))
        cp = node.checkpoint(attempt, dict(version=1, base=None, files=files, deleted=[]), kind='final')
        report = {'harness': 'stub', 'steps': [{'index': 0, 'op': 'run', 'status': 'done',
                  'exit_code': 0, 'stdout_tail': '2 tests OK', 'stderr_tail': ''}]}
        node.complete(attempt, checkpoint=cp, report=report)
        context = self.context(job)
        self.assertEqual(context['report'], report)
        self.assertEqual(context['files'], [{'path': 'text-%d' % i, 'content': contents['text-%d' % i].decode()} for i in range(8)])
        self.assertEqual(sum(len(f['content'].encode()) for f in context['files']), 262144)
        self.assertEqual(context['files_omitted'], [
            {'path': 'a-binary', 'reason': 'binary'}, {'path': 'b-large', 'reason': 'too_large'},
            {'path': 'private/secret', 'reason': 'excluded'}, {'path': 'z-budget', 'reason': 'budget'}])
        self.assertIn('checkpoint_diff', context)
        self.assertIn('assessments', context)

    def test_context_prefers_latest_final_and_falls_back_to_latest_durable(self):
        node = self.node()
        job = self.job()
        attempt = node.claim()
        for kind, content in [('boundary', b'first'), ('boundary', b'second'), ('final', b'final-one'),
                              ('final', b'final-two'), ('boundary', b'newest-boundary')]:
            node.upload(content)
            node.checkpoint(attempt, manifest(content), kind=kind)
            expected = b'final-two' if content == b'newest-boundary' else content
            self.assertEqual(self.app._context_files(job)['files'], [{'path': 'input.txt', 'content': expected.decode()}])
        # Uploading final is never evidence, even if it is the newest checkpoint record.
        node.checkpoint(attempt, manifest(b'missing'), kind='final', status=409)
        self.assertEqual(self.app._context_files(job)['files'][0]['content'], 'final-two')


class DrainMigrationTests(ControllerCase):
    def drain_request(self, node, **changes):
        value = self.app.get('node', node.node_id)
        body = dict(request_id=uid(), expected_version=value['version'], drain=True, migrate=True)
        body.update(changes)
        return body

    def drain(self, node, body=None, status=200, headers=None):
        return self.call('POST', '/nodes/' + node.node_id + '/drain',
                         body or self.drain_request(node), status=status, headers=headers)

    def test_migrate_durable_epoch_fencing_counters_and_idempotency(self):
        source = self.node('a')
        job = self.job()
        old = source.claim()
        source.event(old)
        source.upload(b'checkpoint bytes')
        cp = source.checkpoint(old, manifest(b'checkpoint bytes'), summary={'completed': [0]})
        destination = self.node('b')
        request = self.drain_request(source)
        result = self.drain(source, request)
        self.assertEqual(result['not_migrated'], [])
        self.assertTrue(result['drain'])
        self.assertEqual(self.drain(source, request), result)
        self.drain(source, dict(request, migrate=False), status=409)
        self.drain(source, dict(request, request_id=uid()), status=409)
        new = destination.claim()
        self.assertEqual(new['epoch'], old['epoch'] + 1)
        self.assertEqual(new['recovered_from_attempt'], old['attempt_id'])
        self.assertEqual(new['start']['checkpoint_id'], cp['checkpoint_id'])
        self.assertEqual(new['start']['manifest_sha256'], cp['manifest_sha256'])
        self.assertEqual(new['start']['summary']['completed'], [0])
        materialized = self.temp / 'new-work'
        artifacts.materialize(materialized, self.app.manifest(new['start']['manifest_sha256']), self.app.blobs.read)
        self.assertEqual((materialized / 'input.txt').read_bytes(), b'checkpoint bytes')
        record = self.current(job)
        self.assertEqual(record['counters'], dict(attempts=2, migrations=1, infra_retries=0, repairs=0))
        self.assertEqual(record['attempts'][0]['status'], 'superseded')
        self.assertEqual(source.publish(old, cp, status=409)['error']['code'], 'fenced')
        self.assertEqual(source.checkpoint(old, status=409)['error']['code'], 'fenced')
        self.assertEqual(source.complete(old, checkpoint=cp, status=409)['error']['code'], 'fenced')
        self.assertTrue(source.event(old)['stale'])
        self.assertEqual(source.heartbeat()['revoked'][0]['attempt_id'], old['attempt_id'])
        # A new request while already draining must not migrate the new node's attempt again.
        self.assertEqual(self.drain(source)['not_migrated'], [])
        self.assertEqual(self.current(job)['counters']['migrations'], 1)

    def test_inputs_baseline_migration_reserves_slot_before_other_pending_jobs(self):
        source = self.node('a', resources=dict(slots=1, host_slots=1, mem_mb=None))
        node_input = b'inputs baseline'
        source.upload(node_input)
        job = self.job(contract(inputs_manifest=manifest(node_input)))
        old = source.claim()
        other = self.job()
        destination = self.node('b', resources=dict(slots=1, host_slots=1, mem_mb=None))
        self.assertEqual(self.drain(source)['not_migrated'], [])
        new = destination.claim()
        self.assertEqual(new['job_id'], job['job_id'])
        self.assertEqual(new['start']['kind'], 'inputs')
        self.assertEqual(new['start']['manifest_sha256'], job['inputs_manifest_sha256'])
        self.assertEqual(new['recovered_from_attempt'], old['attempt_id'])
        self.assertIsNone(self.current(other)['current_attempt_id'])

    def test_unsafe_attempts_continue_and_reasons_are_individual(self):
        source = self.node('a', resources=dict(slots=10, host_slots=10, mem_mb=None))
        cases = [
            (contract(recovery={'auto': True, 'side_effects': 'manual'}), 'side_effects_not_none'),
            (contract(recovery={'auto': True, 'side_effects': 'idempotent'}), 'side_effects_not_none'),
            (contract(budgets={'max_migrations': 0}), 'budget_exhausted:migrations'),
            (contract(budgets={'max_attempts': 1}), 'budget_exhausted:attempts'),
            (contract(recovery={'auto': False}, inputs_manifest=None), 'missing_recovery_baseline')]
        active = []
        for value, reason in cases:
            job = self.job(value)
            attempt = source.claim()
            source.event(attempt)
            active.append((job, attempt, reason))
        self.node('b', resources=dict(slots=10, host_slots=10, mem_mb=None))
        result = self.drain(source)
        omitted = {item['attempt_id']: item['reasons'] for item in result['not_migrated']}
        self.assertEqual(len(omitted), len(active))
        for job, attempt, reason in active:
            self.assertIn(reason, omitted[attempt['attempt_id']])
            record = self.current(job)
            self.assertEqual(record['attempts'][0]['status'], 'running')
            self.assertEqual(record['counters']['migrations'], 0)
            source.checkpoint(attempt)
        self.assertEqual(len(source.heartbeat()['renewed']), len(active))

    def test_no_destination_leaves_lease_usable_and_undrain_ignores_migrate(self):
        source = self.node('a')
        job = self.job()
        old = source.claim()
        source.event(old)
        result = self.drain(source)
        self.assertEqual(result['not_migrated'], [{'attempt_id': old['attempt_id'], 'reasons': ['no_eligible_node']}])
        cp = source.checkpoint(old, kind='final')
        source.complete(old, checkpoint=cp)
        source.publish(old, cp)
        self.assertEqual(self.current(job)['delivery_version'], 1)
        self.drain(source, self.drain_request(source, drain=False, migrate='ignored'))
        self.assertFalse(self.app.get('node', 'a')['drain'])
        self.drain(source, self.drain_request(source, migrate='invalid'), status=400)
        self.assertFalse(self.app.get('node', 'a')['drain'])

    def test_destination_filters_and_remaining_capacity(self):
        source = self.node('a')
        first, second = self.job(), self.job()
        a1, a2 = source.claim(), source.claim()
        self.node('b', resources=dict(slots=1, host_slots=1, mem_mb=None))
        result = self.drain(source)
        self.assertEqual(result['not_migrated'][0]['attempt_id'], a2['attempt_id'])
        self.assertIn('no_node_slot', result['not_migrated'][0]['reasons'])
        self.assertEqual(self.current(first)['current_epoch'], 2)
        self.assertEqual(self.current(second)['current_epoch'], 1)
        source.checkpoint(a2)

    def test_coordinator_gate_preserved(self):
        source = self.node('a')
        job = self.job()
        old = source.claim()
        self.node('b')
        path = '/packages/' + job['work_package_id'] + '/coordinator'
        lease = self.call('POST', path, dict(request_id=uid(), holder_kind='interactive', holder_ref='test', ttl_s=90))
        self.assertIn('coordinator_lease', self.drain(source)['not_migrated'][0]['reasons'])
        source.checkpoint(old)
        headers = {'authorization': 'Bearer ' + self.admin, 'x-coordinator-token': lease['coordinator_token']}
        self.assertEqual(self.drain(source, headers=headers)['not_migrated'], [])
        new = self.app.get('attempt', self.current(job)['current_attempt_id'])
        self.assertEqual(new['node_id'], 'b')

    def test_cancel_and_unknown_side_effect_attempts_are_not_migrated(self):
        source = self.node('a')
        cancelled = self.job()
        first = source.claim()
        self.mutate(cancelled, 'cancel')
        unknown = self.job()
        second = source.claim()
        source.event(second, 'attempt.side_effect', {'op_id': 'external-write', 'state': 'intent'})
        self.node('b')
        result = self.drain(source)
        reasons = {item['attempt_id']: item['reasons'] for item in result['not_migrated']}
        self.assertIn('cancel_requested', reasons[first['attempt_id']])
        self.assertIn('side_effect_unknown', reasons[second['attempt_id']])
        self.assertEqual(self.current(cancelled)['status'], 'cancel_requested')
        self.assertEqual(self.current(unknown)['current_epoch'], 1)
        source.checkpoint(second)
        source.complete(first, outcome='cancelled')

    def test_dispatch_pause_deadline_and_shared_host_capacity_gate_migration(self):
        source = self.node('a', physical_host_id='shared', resources=dict(slots=2, host_slots=2, mem_mb=None))
        job = self.job(contract(budgets={'deadline_s': 10}))
        old = source.claim()
        other = self.job()
        source.claim()
        self.node('b', physical_host_id='shared', resources=dict(slots=1, host_slots=2, mem_mb=None))
        self.call('POST', '/dispatch', dict(request_id=uid(), expected_version=self.app.meta()['version'], paused=True))
        result = self.drain(source)
        self.assertTrue(all('dispatch_paused' in item['reasons'] for item in result['not_migrated']))
        # A lease can remain valid after the job's migration deadline.
        self.clock.advance(11)
        source.heartbeat()
        self.node('b', physical_host_id='shared', resources=dict(slots=1, host_slots=2, mem_mb=None))
        self.call('POST', '/dispatch', dict(request_id=uid(), expected_version=self.app.meta()['version'], paused=False))
        result = self.drain(source)
        reason = next(item for item in result['not_migrated'] if item['attempt_id'] == old['attempt_id'])
        self.assertIn('budget_exhausted:deadline_s', reason['reasons'])
        self.assertEqual(self.current(job)['current_epoch'], 1)
        # The other attempt can move within the shared host after revocation frees its slot.
        self.assertEqual(self.current(other)['current_epoch'], 2)

    def test_migration_failure_rolls_back_drain_and_old_lease(self):
        source = self.node('a')
        job = self.job()
        old = source.claim()
        self.node('b')
        original = self.app._schedule
        def fail_after_schedule(*args, **kwargs):
            original(*args, **kwargs)
            raise ValueError('injected transaction failure')
        with patch.object(self.app, '_schedule', side_effect=fail_after_schedule):
            self.drain(source, status=400)
        self.assertFalse(self.app.get('node', 'a')['drain'])
        self.assertEqual(self.current(job)['current_attempt_id'], old['attempt_id'])
        self.assertEqual(self.current(job)['counters']['migrations'], 0)
        source.checkpoint(old)

    def test_destination_unavailable_after_preflight_keeps_old_attempt_running(self):
        source = self.node('a')
        job = self.job()
        old = source.claim()
        source.event(old)
        self.node('b')
        # Simulate a destination crossing its heartbeat deadline between checks.
        with patch.object(self.app, 'explanation', return_value={'selected_node_id': None,
                'nodes': [{'node_id': 'b', 'reasons': ['node_suspect']}]}):
            result = self.drain(source)
        self.assertEqual(result['not_migrated'], [{'attempt_id': old['attempt_id'],
                         'reasons': ['migration_not_scheduled', 'node_suspect']}])
        record = self.current(job)
        self.assertEqual(record['attempts'][0]['status'], 'running')
        self.assertEqual(record['current_epoch'], 1)
        self.assertEqual(record['counters']['migrations'], 0)
        source.checkpoint(old)
