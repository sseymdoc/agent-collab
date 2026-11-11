"""rc5 controller contracts through App.handle; no sockets or model processes."""
import copy
import json
from unittest.mock import patch

from agent_collab import domain as d, protocol as p
from agent_collab.controller import backup, restore
from helpers import ControllerCase, contract, manifest, uid
import test_coordinator as co


CHECKS = [{'name': 'unit', 'run': ['{python}', '-m', 'unittest'], 'timeout_s': 600}]


def check_report(cp, **changes):
    evidence = {'manifest_sha256': cp['manifest_sha256'], 'node_id': 'worker',
                'platform': {'os': 'linux', 'release': 'test', 'machine': 'x86_64', 'python': '3.9'},
                'results': [{'name': 'unit', 'argv': ['/usr/bin/python3', '-m', 'unittest'],
                             'exit_code': 0, 'timed_out': False, 'duration_s': 0.1,
                             'stdout_tail': 'OK', 'stderr_tail': ''}]}
    evidence.update(changes)
    return {'checks': evidence}


class Rc5Case(ControllerCase):
    def configured(self):
        self.app.config.update(copy.deepcopy(co.CONFIG))

    def coordinators(self, job=None):
        return [j for j in self.call('GET', '/jobs')['jobs'] if j['contract'].get('coordinates') and
                (job is None or j['contract']['coordinates']['job_id'] == job['job_id'])]

    def finish_decision(self, node, assignment, value):
        raw = p.canonical_json(value)
        node.upload(raw)
        cp = node.checkpoint(assignment, manifest(raw, name='decision.json'), kind='final')
        node.complete(assignment, checkpoint=cp)
        return cp

    def assess(self, job, cp, status='passed', headers=None):
        return self.call('POST', '/jobs/' + job['job_id'] + '/assess', {
            'request_id': uid(), 'expected_version': self.current(job)['version'], 'assessment_id': uid(),
            'checkpoint_id': cp['checkpoint_id'], 'status': status, 'method': '测试',
            'result': '检查结果', 'reviewer': 'admin'}, headers=headers)

    def evidence(self, node, harness='stub'):
        return next(n for n in self.call('GET', '/nodes')['nodes'] if n['node_id'] == node.node_id)['capability_evidence'][harness]


class CheckContractTests(Rc5Case):
    def test_validation_defaults_limits_and_request_hash(self):
        checks = [{'name': 'one', 'run': ['{python}', '-V']}]
        job = self.job(contract(checks=checks))
        self.assertEqual(job['contract']['checks'][0]['timeout_s'], 600)
        self.assertEqual(job['contract']['coordination'], {'policy': 'always'})
        self.assertNotIn('timeout_s', checks[0])
        data = {'request_id': uid(), 'title': 'dedup', 'contract': contract(checks=checks)}
        original = self.call('POST', '/jobs', data, status=201)
        self.assertEqual(self.call('POST', '/jobs', data)['job_id'], original['job_id'])
        data['contract']['checks'][0]['timeout_s'] = 3
        self.call('POST', '/jobs', data, status=409)
        invalid = [None, {}, [None], CHECKS * 2, [{'name': 'a b', 'run': ['x']}],
                   [{'name': 'a' * 65, 'run': ['x']}], [{'name': 'x', 'run': []}],
                   [{'name': 'x', 'run': ['x', 1]}], [{'name': 'x', 'run': ['x'], 'extra': 1}],
                   [{'name': str(i), 'run': ['x']} for i in range(17)]]
        for timeout in (0, 3601, True, '1', float('inf'), float('nan')):
            invalid.append([dict(CHECKS[0], timeout_s=timeout)])
        for value in invalid:
            with self.subTest(checks=value):
                with self.assertRaises(ValueError):
                    d.validate_contract(contract(checks=value))
        for value in (None, [], {'policy': 'sometimes'}, {'extra': True}):
            with self.subTest(coordination=value):
                self.call('POST', '/jobs', {'request_id': uid(), 'contract': contract(coordination=value)}, status=400)
        self.assertEqual(len(d.validate_contract(contract(checks=[{'name': str(i), 'run': ['x']} for i in range(16)]))['checks']), 16)

    def test_admin_gate_failed_timeout_missing_mismatch_and_success(self):
        node = self.node('worker')
        for reason in ('no_report', 'failed', 'timed_out', 'missing', 'manifest_mismatch', None):
            with self.subTest(reason=reason):
                job = self.job(contract(checks=CHECKS, publish={'requires_acceptance': True}))
                assignment = node.claim()
                cp = node.checkpoint(assignment, kind='final')
                report = check_report(cp)
                if reason == 'no_report':
                    report = {'final_message': '全部测试通过'}
                elif reason == 'manifest_mismatch':
                    report['checks']['manifest_sha256'] = '0' * 64
                elif reason == 'missing':
                    report['checks']['results'] = []
                elif reason == 'failed':
                    report['checks']['results'][0]['exit_code'] = 1
                elif reason == 'timed_out':
                    report['checks']['results'][0].update(timed_out=True, exit_code=None)
                node.complete(assignment, checkpoint=cp, report=report)
                record = self.assess(job, cp)
                current = self.current(job)
                if reason:
                    self.assertEqual(record['status'], 'unverified')
                    self.assertEqual(record['requested_status'], 'passed')
                    self.assertEqual(current['acceptance']['evidence_gaps'], [{'check': 'unit', 'reason': reason}])
                    self.assertEqual(current['acceptance']['requested_status'], 'passed')
                    self.assertEqual(current['checkpoints'][0]['state'], 'durable')
                    self.assertIn('job.evidence_gap', [e['type'] for e in current['events']])
                    node.publish(assignment, cp, status=409)
                else:
                    self.assertEqual(record['status'], 'passed')
                    self.assertEqual(record['evidence'], {'attempt_id': assignment['attempt_id'],
                                                         'manifest_sha256': cp['manifest_sha256'], 'checks': ['unit']})
                    self.assertEqual(current['acceptance']['evidence_gaps'], [])
                    self.assertIsNone(current['acceptance']['requested_status'])
                    node.publish(assignment, cp)

    def test_ui_and_managed_accept_cannot_replace_missing_execution_evidence(self):
        self.configured()
        worker = self.node('worker')
        job = self.job(contract(checks=CHECKS, publish={'requires_acceptance': True}))
        assignment = worker.claim()
        cp = worker.checkpoint(assignment, kind='final')
        worker.complete(assignment, checkpoint=cp, report={'final_message': '我已运行全部测试且通过。'})
        mac = self.node('mac', capabilities=co.CAPS)
        ca = mac.claim()
        self.finish_decision(mac, ca, co.decision(cp))
        current = self.current(job)
        self.assertEqual(current['acceptance']['status'], 'unverified')
        self.assertEqual(current['assessments'][0]['evidence_gaps'], [{'check': 'unit', 'reason': 'no_report'}])
        worker.publish(assignment, cp, status=409)
        login = self.app.handle('POST', p.API_PREFIX + '/session', {}, p.canonical_json({'token': self.admin}))
        self.assertEqual(login.status, 200)
        headers = {'cookie': dict(login.headers)['Set-Cookie'].split(';')[0],
                   'x-csrf-token': json.loads(login.body)['csrf_token']}
        self.assertEqual(self.assess(job, cp, headers=headers)['status'], 'unverified')

    def test_managed_accept_with_bound_checks_passes_and_can_publish(self):
        self.configured()
        worker = self.node('worker')
        job = self.job(contract(checks=CHECKS, publish={'requires_acceptance': True}))
        a = worker.claim()
        cp = worker.checkpoint(a, kind='final')
        worker.complete(a, checkpoint=cp, report=check_report(cp))
        mac = self.node('mac', capabilities=co.CAPS)
        ca = mac.claim()
        self.finish_decision(mac, ca, co.decision(cp))
        assessment = self.current(job)['assessments'][0]
        self.assertEqual(assessment['status'], 'passed')
        self.assertEqual(assessment['evidence']['attempt_id'], a['attempt_id'])
        worker.publish(a, cp)

    def test_gate_uses_checkpoint_producer_not_latest_attempt_or_request_evidence(self):
        node = self.node('worker')
        job = self.job(contract(checks=CHECKS))
        first = node.claim()
        old = node.checkpoint(first, kind='final')
        node.complete(first, checkpoint=old, report=check_report(old))
        self.mutate(job, 'retry', reason='rerun')
        second = node.claim()
        cp = node.checkpoint(second, kind='final')
        node.complete(second, checkpoint=cp, report={'final_message': 'passed'})
        self.assertEqual(self.assess(job, old)['evidence']['attempt_id'], first['attempt_id'])
        self.assertEqual(self.assess(job, cp)['status'], 'unverified')
        self.assertEqual(self.assess(job, cp, status='rework')['status'], 'rework')
        self.assertEqual(self.current(job)['acceptance']['evidence_gaps'], [])

    def test_malformed_or_duplicate_results_fail_closed_and_assess_replay_is_once(self):
        for result in ({'exit_code': False, 'timed_out': False}, {'exit_code': 0},
                       {'exit_code': 0, 'timed_out': 0}, {'exit_code': '0', 'timed_out': False}):
            report = {'checks': {'manifest_sha256': 'hash', 'results': [dict(name='unit', **result)]}}
            self.assertEqual(d.evidence_gaps({'checks': CHECKS}, report, 'hash')[0]['reason'], 'failed')
        report = check_report({'manifest_sha256': 'hash'})
        report['checks']['results'] *= 2
        self.assertEqual(d.evidence_gaps({'checks': CHECKS}, report, 'hash')[0]['reason'], 'failed')
        node = self.node('worker')
        job = self.job(contract(checks=CHECKS))
        a = node.claim()
        cp = node.checkpoint(a, kind='final')
        node.complete(a, checkpoint=cp)
        data = dict(request_id=uid(), expected_version=self.current(job)['version'], assessment_id=uid(),
                    checkpoint_id=cp['checkpoint_id'], status='passed', method='manual', result='ok', reviewer='admin')
        path = '/jobs/' + job['job_id'] + '/assess'
        self.assertEqual(self.call('POST', path, data), self.call('POST', path, data))
        self.assertEqual(len([e for e in self.current(job)['events'] if e['type'] == 'job.evidence_gap']), 1)

    def test_legacy_contract_and_node_report_remain_accepted(self):
        node = self.node()
        job = self.job()
        a = node.claim()
        cp = node.checkpoint(a, kind='final')
        node.complete(a, checkpoint=cp, report=None)
        self.assertEqual(self.assess(job, cp)['status'], 'passed')
        self.assertEqual(self.evidence(node)['status'], 'ok')
        node.publish(a, cp)


class CoordinationPolicyTests(Rc5Case):
    def test_none_blocks_completion_failure_and_rework(self):
        self.configured()
        node = self.node('worker')
        job = self.job(contract(coordination={'policy': 'none'}, recovery={'auto': False}))
        a = node.claim()
        cp = node.checkpoint(a)
        node.complete(a, outcome='failed', failure_class='network')
        self.assess(job, cp, status='rework')
        other = self.job(contract(coordination={'policy': 'none'}))
        a = node.claim()
        cp = node.checkpoint(a, kind='final')
        node.complete(a, checkpoint=cp)
        self.assertFalse(self.coordinators())

    def test_on_failure_ignores_plain_completion_and_rework(self):
        self.configured()
        node = self.node('worker')
        job = self.job(contract(coordination={'policy': 'on_failure'}))
        a = node.claim()
        cp = node.checkpoint(a, kind='final')
        node.complete(a, checkpoint=cp)
        self.assess(job, cp, status='rework')
        self.assertFalse(self.coordinators())

    def test_on_failure_triggers_failed_waiting_reconcile_and_waiting_capability(self):
        self.configured()
        node = self.node('worker')
        for failure, status in (('code_failure', 'failed'), ('network', 'waiting_reconcile'),
                                ('capability_missing', 'waiting_capability')):
            job = self.job(contract(coordination={'policy': 'on_failure'}, recovery={'auto': False}))
            a = node.claim()
            node.complete(a, outcome='failed', failure_class=failure)
            self.assertEqual(self.current(job)['status'], status)
            self.assertEqual(len(self.coordinators(job)), 1)
        job = self.job(contract(coordination={'policy': 'on_failure'}, requirements={'tools': ['missing']}))
        self.assertEqual(self.current(job)['status'], 'waiting_capability')
        self.assertEqual(len(self.coordinators(job)), 1)
        self.app.tick()
        self.assertEqual(len(self.coordinators()), 4)

    def test_on_failure_ignores_rework_on_already_failed_target(self):
        node = self.node('worker')
        job = self.job(contract(coordination={'policy': 'on_failure'}, recovery={'auto': False}))
        a = node.claim()
        cp = node.checkpoint(a)
        node.complete(a, outcome='failed', failure_class='code_failure')
        self.assertEqual(self.current(job)['status'], 'failed')
        self.configured()  # 失败时没有配置，未创建协调 job；之后的 rework 验收不应补出一次
        self.assess(job, cp, status='rework')
        self.assertFalse(self.coordinators())

    def test_on_failure_triggers_completed_takeover(self):
        self.configured()
        node = self.node('worker')
        job = self.job(contract(coordination={'policy': 'on_failure'}))
        first = node.claim()
        node.complete(first, outcome='failed', failure_class='infra_retryable')
        self.assertFalse(self.coordinators())
        a = node.claim()
        self.assertEqual(a['recovered_from_attempt'], first['attempt_id'])
        cp = node.checkpoint(a, kind='final')
        node.complete(a, checkpoint=cp)
        self.assertEqual(len(self.coordinators(job)), 1)

    def test_default_always_preserves_old_triggers_and_dedups_versions(self):
        self.configured()
        worker = self.node('worker')
        failed = self.job()
        a = worker.claim()
        worker.complete(a, outcome='failed', failure_class='code_failure')
        self.assertFalse(self.coordinators())
        job = self.job()
        a = worker.claim()
        cp = worker.checkpoint(a, kind='final')
        worker.complete(a, checkpoint=cp)
        worker.complete(a, checkpoint=cp)
        mac = self.node('mac', capabilities=co.CAPS)
        ca = mac.claim()
        self.finish_decision(mac, ca, co.decision(None, 'escalate'))
        before = self.current(job)['version']
        worker.publish(a, cp)
        self.assertGreater(self.current(job)['version'], before)
        # Exercise a repeated state trigger after publication has changed the row version.
        with self.app.store.transaction():
            self.app._trigger_coordinator(self.app.get('job', job['job_id']), 'completed', self.clock())
        self.assertEqual(len(self.coordinators(job)), 1)
        self.assess(job, cp, status='rework')
        self.assess(job, cp, status='rework')
        self.assertEqual(len(self.coordinators(job)), 2)
        ca = mac.claim()
        self.finish_decision(mac, ca, co.decision(None, 'escalate'))
        self.assess(job, cp, status='rework')
        self.assertEqual(len(self.coordinators(job)), 2)

    def test_cancel_after_context_capture_rejects_managed_repair(self):
        self.configured()
        node = self.node('worker')
        job = self.job(contract(recovery={'auto': False}))
        a = node.claim()
        cp = node.checkpoint(a)
        node.complete(a, outcome='failed', failure_class='network')
        mac = self.node('mac', capabilities=co.CAPS)
        ca = mac.claim()
        self.mutate(job, 'cancel')
        self.finish_decision(mac, ca, co.decision(cp, 'rework', co.repair_request()))
        current = self.current(job)
        self.assertEqual(current['status'], 'cancelled')
        self.assertEqual(current['counters']['repairs'], 0)
        self.assertEqual(len([j for j in self.call('GET', '/jobs')['jobs'] if j['work_package_id'] == job['work_package_id']]), 1)
        self.assertIn('coordinator.decision_rejected', [e['type'] for e in current['events']])

    def test_repair_chain_inherits_checks_policy_and_root_attempt_budget(self):
        self.configured()
        caps = copy.deepcopy(self.node('worker').record['capabilities'])
        caps['tools']['codex'] = 'test'
        node = self.node('codex', capabilities=caps)
        job = self.job(contract(checks=CHECKS, coordination={'policy': 'always'}, budgets={'max_attempts': 2}))
        a = node.claim()
        cp = node.checkpoint(a, kind='final')
        node.complete(a, checkpoint=cp)
        mac = self.node('mac', capabilities=co.CAPS)
        ca = mac.claim()
        self.finish_decision(mac, ca, co.decision(cp, 'rework', co.repair_request()))
        repair = next(j for j in self.call('GET', '/jobs')['jobs'] if j['work_package_id'] == job['work_package_id'] and j['job_id'] != job['job_id'])
        self.assertEqual(repair['contract']['checks'], CHECKS)
        self.assertEqual(repair['contract']['coordination'], {'policy': 'always'})
        a = node.claim()
        cp = node.checkpoint(a, kind='final')
        node.complete(a, checkpoint=cp, report=check_report(cp))
        ca = mac.claim()
        self.finish_decision(mac, ca, co.decision(cp, 'rework', co.repair_request()))
        current = self.current(repair)
        self.assertEqual(current['counters']['attempts'], 2)
        self.assertEqual(current['counters']['repairs'], 1)
        self.assertEqual(current['counters'], self.current(job)['counters'])
        self.assertIn('coordinator.escalated', [e['type'] for e in current['events']])
        self.assertEqual(len(self.coordinators()), 2)

    def test_deadline_exhaustion_keeps_assessment_without_repair(self):
        self.configured()
        node = self.node('worker')
        job = self.job(contract(budgets={'deadline_s': 10}))
        a = node.claim()
        cp = node.checkpoint(a, kind='final')
        node.complete(a, checkpoint=cp)
        mac = self.node('mac', capabilities=co.CAPS)
        ca = mac.claim()
        self.clock.advance(11)
        self.finish_decision(mac, ca, co.decision(cp, 'rework', co.repair_request()))
        self.assertEqual(self.current(job)['acceptance']['status'], 'rework')
        self.assertEqual(self.current(job)['counters']['repairs'], 0)
        self.assertTrue(any(e['payload'].get('reason') == 'budget_exhausted:deadline_s' for e in self.current(job)['events']))


class CapabilityEvidenceTests(Rc5Case):
    def test_success_failed_checks_and_failure_update_history_without_filtering(self):
        node = self.node('worker')
        self.assertEqual(self.evidence(node)['status'], 'unproven')
        job = self.job(contract(checks=CHECKS))
        a = node.claim()
        cp = node.checkpoint(a, kind='final')
        node.complete(a, checkpoint=cp, report=check_report(cp))
        success = self.evidence(node)
        self.assertEqual(success['status'], 'ok')
        self.assertEqual(success['last_success']['attempt_id'], a['attempt_id'])
        for report in ({}, {'checks': None}):
            job = self.job(contract(checks=CHECKS))
            a = node.claim()
            cp = node.checkpoint(a, kind='final')
            node.complete(a, checkpoint=cp, report=report)
            failure = self.evidence(node)
            self.assertEqual(failure['status'], 'degraded')
            self.assertEqual(failure['last_failure']['reason'], 'checks_failed')
            self.assertEqual(failure['last_success'], success['last_success'])
        job = self.job()
        a = node.claim()
        node.complete(a, outcome='failed', failure_class='code_failure')
        self.assertEqual(self.evidence(node)['last_failure']['reason'], 'code_failure')
        explanation = self.call('GET', '/jobs/' + job['job_id'] + '/explain')['nodes'][0]
        self.assertTrue(explanation['eligible'])
        self.assertEqual(explanation['capability_evidence']['stub']['status'], 'degraded')
        job = self.job()
        a = node.claim()
        cp = node.checkpoint(a, kind='final')
        node.complete(a, checkpoint=cp)
        self.assertEqual(self.evidence(node)['status'], 'ok')

    def test_succeeded_with_failed_timeout_or_mismatched_checks_is_degraded(self):
        node = self.node('worker')
        for reason in ('failed', 'timed_out', 'manifest_mismatch'):
            job = self.job(contract(checks=CHECKS))
            a = node.claim()
            cp = node.checkpoint(a, kind='final')
            report = check_report(cp)
            if reason == 'manifest_mismatch':
                report['checks']['manifest_sha256'] = '0' * 64
            else:
                report['checks']['results'][0].update(exit_code=1, timed_out=reason == 'timed_out')
            node.complete(a, checkpoint=cp, report=report)
            self.assertEqual(self.current(job)['attempts'][0]['status'], 'succeeded')
            self.assertEqual(self.evidence(node)['status'], 'degraded')
            self.assertEqual(self.evidence(node)['last_failure']['reason'], 'checks_failed')

    def test_rc5_registration_runtime_and_bound_changes_invalidate_evidence(self):
        node = self.node('worker')
        node.record['capabilities'].update(harness_paths={'stub': '/test/stub'}, harness_disabled={},
                                            agent={'version': 'test', 'git_commit': 'abc'})
        self.call('POST', '/nodes/register', node.record, token=node.token)
        for field in ('agent_version', 'harness_version', 'harness_path'):
            job = self.job()
            a = node.claim()
            cp = node.checkpoint(a, kind='final')
            result = node.complete(a, checkpoint=cp, runtime={'agent_commit': 'abc'})
            self.assertEqual(result['runtime']['agent_commit'], 'abc')
            self.assertEqual(self.evidence(node)['status'], 'ok')
            previous = self.evidence(node)['bound']
            if field == 'agent_version':
                node.record['agent_version'] = 'next'
            elif field == 'harness_version':
                node.record['capabilities']['harness']['stub'] = '2'
            else:
                node.record['capabilities']['harness_paths']['stub'] = '/test/new-stub'
            self.call('POST', '/nodes/register', node.record, token=node.token)
            self.assertEqual(self.evidence(node)['status'], 'unproven')
            self.assertEqual(self.evidence(node)['bound'], previous)
        self.restart()
        self.assertEqual(self.evidence(node)['status'], 'unproven')

    def test_running_old_version_completion_does_not_prove_new_registration(self):
        node = self.node('worker')
        job = self.job()
        a = node.claim()
        node.record['agent_version'] = 'new'
        self.call('POST', '/nodes/register', node.record, token=node.token)
        cp = node.checkpoint(a, kind='final')
        node.complete(a, checkpoint=cp)
        self.assertEqual(self.evidence(node)['status'], 'unproven')
        self.assertEqual(self.evidence(node)['bound']['agent_version'], 'test')

    def test_cancel_and_node_loss_do_not_count_as_capability_failures(self):
        node = self.node('worker')
        initial = self.evidence(node)
        job = self.job()
        a = node.claim()
        self.mutate(job, 'cancel')
        node.complete(a, outcome='cancelled')
        self.assertEqual(self.evidence(node), initial)
        job = self.job()
        a = node.claim()
        node.complete(a, outcome='failed', failure_class='user_cancel')
        self.assertEqual(self.evidence(node), initial)
        job = self.job(contract(recovery={'auto': False}))
        a = node.claim()
        self.clock.advance(91)
        self.app.tick()
        self.assertEqual(self.evidence(node), initial)

    def test_coordinator_attempt_also_records_capability(self):
        self.configured()
        node = self.node('worker')
        job = self.job()
        a = node.claim()
        cp = node.checkpoint(a, kind='final')
        node.complete(a, checkpoint=cp)
        mac = self.node('mac', capabilities=co.CAPS)
        ca = mac.claim()
        self.finish_decision(mac, ca, co.decision(cp))
        evidence = self.evidence(mac, 'claude')
        self.assertEqual(evidence['status'], 'ok')
        self.assertEqual(evidence['last_success']['attempt_id'], ca['attempt_id'])


class IntegrationQueueTests(Rc5Case):
    def delivery(self, worker, root=None, content=b'delivery', inputs=None, path='/source', target='target'):
        spec = contract(integration={'target_node_id': target, 'target_path': path}, workspace={'exclude': ['cache/**']},
                        budgets={'max_attempts': 8, 'max_repairs': 7})
        if inputs is not None:
            spec['inputs_manifest'] = inputs
        job = self.job(spec, **({'work_package_id': root['work_package_id']} if root else {}))
        a = worker.claim()
        worker.upload(content)
        cp = worker.checkpoint(a, manifest(content, name='output.txt'), kind='final')
        worker.complete(a, checkpoint=cp)
        worker.publish(a, cp)
        return self.current(job), a, cp

    def integration_result(self, node, job, cp, state='integrated', expected_status=200, request_id=None):
        return self.call('POST', '/jobs/' + job['job_id'] + '/integration', {
            'request_id': request_id or uid(), 'checkpoint_id': cp['checkpoint_id'], 'status': state,
            'detail': {'changed': ['output.txt'], 'conflicts': [], 'backup': None}}, token=node.token, status=expected_status)

    def test_cross_node_offline_target_receives_persistent_queue_only_on_its_heartbeat(self):
        worker = self.node('worker')
        job, a, cp = self.delivery(worker)
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['integration_status'], 'integration_pending')
        self.assertEqual(worker.heartbeat()['integrations'], [])
        self.restart()
        target = self.node('target')
        todo = target.heartbeat()['integrations']
        self.assertEqual(todo, [{'job_id': job['job_id'], 'checkpoint_id': cp['checkpoint_id'],
                                 'manifest_sha256': cp['manifest_sha256'],
                                 'base_manifest_sha256': job['inputs_manifest_sha256'],
                                 'target_path': '/source', 'exclude': ['cache/**']}])
        self.assertEqual(target.heartbeat()['integrations'], todo)
        self.assertEqual(target.heartbeat(generation=0)['integrations'], [])
        target.heartbeat()
        request_id = uid()
        result = self.integration_result(target, job, cp, request_id=request_id)
        self.assertEqual(result['integration_status'], 'integrated')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['delivery_version'], 1)
        self.assertEqual(target.heartbeat()['integrations'], [])
        self.assertEqual(result, self.integration_result(target, job, cp, request_id=request_id))
        self.assertEqual(self.app.store.all('integration_target')[0]['integrated_manifest_sha256'], cp['manifest_sha256'])

    def test_new_delivery_replaces_old_pending_and_keeps_root_input_base(self):
        worker = self.node('a-worker')
        target = self.node('target')
        worker.upload(b'original')
        root, a, old = self.delivery(worker, inputs=manifest(b'original'))
        worker.upload(b'repair input')
        newer, a, cp = self.delivery(worker, root, b'new', inputs=manifest(b'repair input'))
        queue = target.heartbeat()['integrations']
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]['job_id'], newer['job_id'])
        self.assertEqual(queue[0]['base_manifest_sha256'], root['inputs_manifest_sha256'])
        self.assertNotEqual(queue[0]['base_manifest_sha256'], newer['inputs_manifest_sha256'])
        self.integration_result(target, root, old, expected_status=409)
        self.integration_result(target, newer, cp)
        self.assertEqual(target.heartbeat()['integrations'], [])

    def test_base_uses_last_integrated_manifest_and_is_scoped_to_target_path(self):
        worker = self.node('a-worker')
        target = self.node('target')
        root, a, first = self.delivery(worker)
        self.integration_result(target, root, first)
        second, a, cp = self.delivery(worker, root, b'second')
        self.assertEqual(target.heartbeat()['integrations'][0]['base_manifest_sha256'], first['manifest_sha256'])
        self.integration_result(target, second, cp, state='conflict')
        self.assertEqual(self.current(second)['integration_status'], 'conflict')
        self.assertEqual(target.heartbeat()['integrations'], [])
        third, a, last = self.delivery(worker, root, b'third')
        self.assertEqual(target.heartbeat()['integrations'][0]['base_manifest_sha256'], first['manifest_sha256'])
        self.integration_result(target, third, last)
        other, a, cp = self.delivery(worker, root, b'other', path='/other')
        self.assertEqual(target.heartbeat()['integrations'][0]['base_manifest_sha256'], root['inputs_manifest_sha256'])

    def test_integration_authorization_pending_result_and_coordinator_lease(self):
        worker = self.node('a-worker')
        target = self.node('target')
        job, a, cp = self.delivery(worker)
        self.integration_result(worker, job, cp, expected_status=403)
        self.integration_result(target, job, cp, state='integration_pending')
        self.assertEqual(len(target.heartbeat()['integrations']), 1)
        lease_path = '/packages/' + job['work_package_id'] + '/coordinator'
        lease = self.call('POST', lease_path, {'request_id': uid(), 'holder_kind': 'interactive',
                                             'holder_ref': 'admin', 'ttl_s': 90})
        self.integration_result(target, job, cp, expected_status=409)
        self.call('DELETE', lease_path, headers={'authorization': 'Bearer ' + self.admin,
                                               'x-coordinator-token': lease['coordinator_token']})
        self.integration_result(target, job, cp)
        self.assertEqual(target.heartbeat()['integrations'], [])

    def test_publish_replay_does_not_requeue_integrated_delivery(self):
        worker = self.node('a-worker')
        target = self.node('target')
        job, a, cp = self.delivery(worker)
        publication = self.current(job)['publications'][0]
        self.integration_result(target, job, cp)
        worker.publish(a, cp, request_id=publication['request_id'])
        self.assertEqual(self.current(job)['integration_status'], 'integrated')
        self.assertEqual(target.heartbeat()['integrations'], [])

    def test_publish_and_queue_are_atomic_and_backup_preserves_pending(self):
        worker = self.node('a-worker')
        target = self.node('target')
        job = self.job(contract(integration={'target_node_id': 'target', 'target_path': '/source'}))
        a = worker.claim()
        cp = worker.checkpoint(a, kind='final')
        worker.complete(a, checkpoint=cp)
        original = self.app.store.put

        def fail_queue(kind, ident, value):
            if kind == 'integration_target':
                raise ValueError('injected queue failure')
            return original(kind, ident, value)

        with patch.object(self.app.store, 'put', side_effect=fail_queue):
            worker.publish(a, cp, status=400)
        self.assertEqual(self.current(job)['delivery_version'], 0)
        self.assertEqual(self.current(job)['publications'], [])
        self.assertEqual(target.heartbeat()['integrations'], [])
        worker.publish(a, cp)
        pending = target.heartbeat()['integrations']
        backup(self.temp / 'data', self.temp / 'backup', self.clock)
        self.app.close()
        self.app = None
        restore(self.temp / 'data', self.temp / 'backup', self.clock)
        from agent_collab.controller import App
        self.app = App(self.temp / 'data', clock=self.clock)
        self.assertEqual(target.heartbeat()['integrations'], [])
        self.assertEqual(target.heartbeat(generation=self.app.meta()['authority_generation'])['integrations'], pending)

    def test_legacy_integrated_manifest_is_preserved_as_base(self):
        worker = self.node('a-worker')
        target = self.node('target')
        root, a, cp = self.delivery(worker)
        self.integration_result(target, root, cp)
        # A later unsuccessful integration must not erase the earlier integrated baseline.
        self.integration_result(target, root, cp, state='conflict')
        with self.app.store.transaction():
            self.app.store.db.execute("DELETE FROM records WHERE kind='integration_target'")
        self.restart()
        newer, a, last = self.delivery(worker, root, b'new')
        self.assertEqual(target.heartbeat()['integrations'][0]['base_manifest_sha256'], cp['manifest_sha256'])


class MigrationTests(Rc5Case):
    def test_rc4_json_records_and_trigger_keys_upgrade_idempotently(self):
        self.configured()
        node = self.node('worker')
        job = self.job()
        a = node.claim()
        cp = node.checkpoint(a, kind='final')
        node.complete(a, checkpoint=cp)
        coordinator = self.coordinators(job)[0]
        old_job = self.app.get('job', job['job_id'])
        old_job['contract'].pop('checks')
        old_job['contract'].pop('coordination')
        old_job['acceptance'].pop('requested_status')
        old_job['acceptance'].pop('evidence_gaps')
        private = self.app.get('job_private', coordinator['job_id'])
        private['managed']['target_state']['acceptance'] = dict(old_job['acceptance'])
        with self.app.store.transaction():
            self.app.store.put('job', old_job['job_id'], old_job)
            self.app.store.put('job_private', coordinator['job_id'], private)
            old_node = self.app.get('node', node.node_id)
            old_node.pop('capability_evidence')
            self.app.store.put('node', node.node_id, old_node)
            self.app.store.db.execute("DELETE FROM records WHERE kind='coordinator_trigger'")
            self.app.store.put('coordinator_trigger', '%s:completed:%s' % (job['job_id'], old_job['version']),
                               {'job_id': coordinator['job_id']})
        self.restart()
        self.configured()
        current = self.current(job)
        self.assertEqual(current['contract'], old_job['contract'])
        self.assertEqual(current['request_hash'], old_job['request_hash'])
        self.assertEqual(current['acceptance']['evidence_gaps'], [])
        self.assertIsNone(current['acceptance']['requested_status'])
        self.assertEqual(self.evidence(node)['status'], 'unproven')
        mac = self.node('mac', capabilities=co.CAPS)
        ca = mac.claim()
        self.finish_decision(mac, ca, co.decision(None, 'escalate'))
        with self.app.store.transaction():
            self.app._trigger_coordinator(self.app.get('job', job['job_id']), 'completed', self.clock())
        self.assertEqual(len(self.coordinators(job)), 1)
        before = self.current(job)
        self.restart()
        self.assertEqual(self.current(job), before)
        node.publish(a, cp)
