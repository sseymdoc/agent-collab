"""WP4 authority invariants, using the real controller without sockets/models."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from unittest.mock import patch

from agent_collab import domain as d, protocol as p
from agent_collab.controller import App, backup, restore
from agent_collab.artifacts import materialize
from helpers import ControllerCase, contract, manifest, uid


PROFILE = {'harness': 'claude', 'model': 'configured-model', 'effort': 'configured-effort', 'node_ids': ['mac']}
CONFIG = {'managed_coordinator': {'enabled': True, 'profile_id': 'standby'}, 'standby_profiles': {'standby': PROFILE}}
CAPS = {'os': 'macos', 'arch': 'arm64', 'tools': {'claude': 'test'}, 'harness': {'stub': '1', 'claude': 'test'}, 'gpu': [], 'labels': []}


def decision(cp, kind='accept', follow=None):
    return {'decision': kind, 'assessment': {'status': {'accept': 'passed', 'rework': 'rework', 'escalate': 'blocked'}[kind],
            'method': 'stub review', 'result': 'checked captured artifacts', 'checkpoint_id': cp['checkpoint_id'] if cp else None},
            'follow_up': follow, 'notes': 'test decision'}


def repair_request(**changes):
    value = dict(title='repair', goal='Fix the unmet acceptance criteria', acceptance=['Regression tests pass'],
                 instructions='Repair the failing branch and add a regression test.', effort='xhigh')
    value.update(changes)
    return value


class CoordinatorTests(ControllerCase):
    def lease_path(self, job):
        return '/packages/' + job['work_package_id'] + '/coordinator'

    def headers(self, token):
        return {'authorization': 'Bearer ' + self.admin, 'x-coordinator-token': token}

    def lease(self, job, token='', status=200, **changes):
        data = dict(request_id=uid(), holder_kind='interactive', holder_ref='desktop', ttl_s=90, takeover=False, reason='')
        data.update(changes)
        return self.call('POST', self.lease_path(job), data, headers=self.headers(token), status=status)

    def configured(self):
        self.app.config.update(copy.deepcopy(CONFIG))

    def completed(self, value=None):
        worker = self.node('worker')
        job = self.job(value)
        a = worker.claim()
        cp = worker.checkpoint(a, kind='final')
        worker.complete(a, checkpoint=cp, report={'summary': 'completed target'})
        return job, worker, a, cp

    def coordinators(self):
        return [j for j in self.call('GET', '/jobs')['jobs'] if 'coordinates' in j['contract']]

    def round(self, value=None):
        self.configured()
        job, worker, assignment, cp = self.completed(value)
        mac = self.node('mac', capabilities=CAPS)
        ca = mac.claim()
        return job, mac, ca, cp

    def finish_decision(self, mac, assignment, value, filename='decision.json'):
        raw = p.canonical_json(value)
        mac.upload(raw)
        cp = mac.checkpoint(assignment, manifest(raw, name=filename), kind='final')
        mac.complete(assignment, checkpoint=cp)
        return cp

    def event_types(self, job):
        return [e['type'] for e in self.current(job)['events']]

    def assert_assessment_applied(self, job, ca, cp, status='rework'):
        current = self.current(job)
        self.assertEqual(current['acceptance']['status'], status)
        self.assertEqual(len(current['assessments']), 1)
        self.assertEqual(current['assessments'][0]['manifest_sha256'], cp['manifest_sha256'])
        self.assertEqual(self.app.get('attempt', ca['attempt_id'])['status'], 'succeeded')
        self.assertEqual(self.app.get('job', ca['job_id'])['status'], 'completed')
        self.assertIsNone(self.call('GET', self.lease_path(job)))
        self.assertNotIn('coordinator.decision_rejected', self.event_types(job))
        return current

    def test_lease_acquire_renew_conflict_takeover_release_and_hash_only(self):
        job = self.job()
        self.assertIsNone(self.call('GET', self.lease_path(job)))
        request = uid()
        first = self.lease(job, request_id=request)
        self.assertEqual(first, self.lease(job, request_id=request))
        token = first['coordinator_token']
        record = self.call('GET', self.lease_path(job))
        self.assertEqual(set(record), {'holder_kind', 'holder_ref', 'expires_at', 'version'})
        # 取得响应带租约版本：UI 据此判断归属（expires_at 可能在同一毫秒的接管中重复）。
        self.assertEqual(first['version'], record['version'])
        self.clock.advance(5)
        renewed = self.lease(job, token, ttl_s=100)
        self.assertEqual(renewed['coordinator_token'], token)
        self.assertGreater(renewed['expires_at'], first['expires_at'])
        # 续租后重放最初的取得请求：返回当次的同一租约状态（expires_at 与 version 都是原值）。
        self.assertEqual(self.lease(job, request_id=request), first)
        self.lease(job, status=409)
        self.lease(job, holder_ref='other', status=409)
        self.lease(job, holder_ref='other', takeover=True, status=400)
        second = self.lease(job, holder_ref='other', takeover=True, reason='operator takeover')
        self.assertNotEqual(second['coordinator_token'], token)
        self.assertGreater(second['version'], renewed['version'])
        self.assertEqual(self.call('GET', self.lease_path(job))['version'], second['version'])
        takeovers = [e for e in self.call('GET', '/events')['events'] if e['type'] == 'coordinator.takeover']
        self.assertEqual(len(takeovers), 1)
        self.assertEqual(takeovers[0]['payload']['reason'], 'operator takeover')
        self.call('DELETE', self.lease_path(job), headers=self.headers(token), status=409)
        dump = '\n'.join(self.app.store.db.iterdump())
        self.assertNotIn(token, dump)
        self.assertNotIn(second['coordinator_token'], dump)
        self.call('DELETE', self.lease_path(job), headers=self.headers(second['coordinator_token']))
        self.assertIsNone(self.call('GET', self.lease_path(job)))
        self.lease(job, request_id=request, status=409)

    def test_expired_token_cannot_release_new_owner_and_lease_survives_restart(self):
        job = self.job()
        first = self.lease(job, ttl_s=2)
        self.restart()
        self.assertEqual(self.call('GET', self.lease_path(job))['holder_ref'], 'desktop')
        self.clock.advance(3)
        self.assertIsNone(self.call('GET', self.lease_path(job)))
        self.lease(job, holder_ref='other')
        self.call('DELETE', self.lease_path(job), headers=self.headers(first['coordinator_token']), status=409)

    def test_node_cannot_acquire_or_takeover(self):
        node = self.node()
        job = self.job()
        self.call('POST', self.lease_path(job), dict(request_id=uid(), holder_kind='managed', holder_ref='x', ttl_s=10,
                  takeover=True, reason='no'), token=node.token, status=403)

    def test_guard_assess_publish_retry_integration_and_same_root_but_node_auto_publish(self):
        job, worker, a, cp = self.completed()
        token = self.lease(job)['coordinator_token']
        actions = {
            'assess': dict(assessment_id=uid(), checkpoint_id=cp['checkpoint_id'], status='passed', method='test', result='ok', reviewer='test'),
            'retry': dict(reason='retry'),
            'publish': dict(attempt_id=a['attempt_id'], epoch=a['epoch'], checkpoint_id=cp['checkpoint_id'], expected_base_version=0),
            'integration': dict(checkpoint_id=cp['checkpoint_id'], status='integrated', detail={})}
        for action, values in actions.items():
            with self.subTest(action=action):
                result = self.mutate(job, action, expected_status=409, **values)
                self.assertEqual(result['error']['details']['reason'], 'coordinator_lease')
        body = dict(request_id=uid(), work_package_id=job['work_package_id'], contract=contract())
        result = self.call('POST', '/jobs', body, status=409)
        self.assertEqual(result['error']['details']['reason'], 'coordinator_lease')
        worker.publish(a, cp)
        values = dict(actions['assess'], request_id=uid(), expected_version=self.current(job)['version'])
        self.call('POST', '/jobs/' + job['job_id'] + '/assess', values, headers=self.headers(token))
        self.assertEqual(self.current(job)['acceptance']['status'], 'passed')
        self.call('POST', '/jobs', body, headers=self.headers(token), status=201)

    def test_cancel_unrestricted(self):
        worker = self.node()
        job = self.job()
        a = worker.claim()
        self.lease(job)
        self.mutate(job, 'cancel')
        worker.complete(a, outcome='cancelled')
        self.assertEqual(self.current(job)['status'], 'cancelled')

    def test_missing_disabled_and_incomplete_profiles_no_jobs(self):
        for changes, status in (({}, 'profile_missing'), ({'managed_coordinator': {'enabled': False}}, 'disabled'),
                ({'managed_coordinator': {'enabled': True, 'profile_id': 'missing'}}, 'profile_missing')):
            self.app.config.pop('managed_coordinator', None)
            self.app.config.update(changes)
            self.assertEqual(self.call('GET', '/overview')['managed_coordinator']['status'], status)
        self.app.config.update(copy.deepcopy(CONFIG))
        for key in ('harness', 'model', 'effort', 'node_ids'):
            profile = copy.deepcopy(PROFILE)
            del profile[key]
            self.app.config['standby_profiles'] = {'standby': profile}
            self.assertEqual(self.call('GET', '/overview')['managed_coordinator']['status'], 'profile_missing')
            worker = self.node('worker-' + key.replace('_', '-'))
            job = self.job(contract(requirements={'labels': ['only-' + key]}))
            worker.record['capabilities']['labels'] = ['only-' + key]
            self.call('POST', '/nodes/register', worker.record, token=worker.token)
            a = worker.claim()
            cp = worker.checkpoint(a, kind='final')
            worker.complete(a, checkpoint=cp)
            self.assertFalse(self.coordinators())

    def test_config_file_and_overview_ready_snapshot(self):
        self.app.close()
        (self.temp / 'data/config.json').write_text(json.dumps(CONFIG), encoding='utf-8')
        self.app = App(self.temp / 'data', clock=self.clock)
        self.assertEqual(self.call('GET', '/overview')['managed_coordinator'],
                         dict(status='ready', profile_id='standby', profile=PROFILE))

    def test_exactly_one_round_inputs_profile_allowlist_and_claim_lease(self):
        self.configured()
        job, worker, a, cp = self.completed()
        other = self.node('not-mac', capabilities=CAPS)
        other.claim(status=204)
        coordinators = self.coordinators()
        self.assertEqual(len(coordinators), 1)
        cj = coordinators[0]
        c = cj['contract']
        self.assertEqual(c['coordinates'], {'job_id': job['job_id'], 'work_package_id': job['work_package_id']})
        self.assertFalse(c['recovery']['auto'])
        self.assertEqual(c['requirements']['tools'], ['claude'])
        self.assertEqual(c['harness_params'], {k: PROFILE[k] for k in ('model', 'effort')})
        files = {f['path']: json.loads(self.app.blobs.read(f['sha256'])) for f in c['inputs_manifest']['files']}
        self.assertEqual(files['decision-schema.json'], d.DECISION_SCHEMA)
        self.assertEqual(files['context.json']['report']['summary'], 'completed target')
        self.assertEqual(files['context.json']['job']['durable_checkpoint_id'], cp['checkpoint_id'])
        target = self.app.get('job', job['job_id'])
        with self.app.store.transaction():
            self.app._trigger_coordinator(target, 'completed', self.clock())
        worker.complete(a, checkpoint=cp, report={'summary': 'completed target'})
        self.assertEqual(len(self.coordinators()), 1)
        mac = self.node('mac', capabilities=CAPS)
        ca = mac.claim()
        self.assertEqual(ca['job_id'], cj['job_id'])
        lease = self.call('GET', self.lease_path(job))
        self.assertEqual((lease['holder_kind'], lease['holder_ref']), ('managed', ca['attempt_id']))
        self.assertEqual(self.current(job)['counters']['attempts'], 1)

    def test_interactive_blocks_trigger_and_pending_claim(self):
        self.configured()
        node = self.node('worker')
        job = self.job()
        token = self.lease(job)['coordinator_token']
        a = node.claim()
        cp = node.checkpoint(a, kind='final')
        node.complete(a, checkpoint=cp)
        self.assertFalse(self.coordinators())
        self.call('DELETE', self.lease_path(job), headers=self.headers(token))
        self.mutate(job, 'assess', assessment_id=uid(), checkpoint_id=cp['checkpoint_id'], status='rework', method='test', result='needs work', reviewer='test')
        self.assertEqual(len(self.coordinators()), 1)
        mac = self.node('mac', capabilities=CAPS)
        self.app.tick()
        token = self.lease(job)['coordinator_token']
        mac.claim(status=204)
        self.call('DELETE', self.lease_path(job), headers=self.headers(token))
        mac.claim()

    def test_waiting_reconcile_triggers_once_and_escalate_has_no_assessment(self):
        self.configured()
        worker = self.node('worker')
        job = self.job(contract(recovery={'auto': False}))
        a = worker.claim()
        worker.complete(a, outcome='failed', failure_class='network')
        self.assertEqual(self.current(job)['status'], 'waiting_reconcile')
        self.assertEqual(len(self.coordinators()), 1)
        mac = self.node('mac', capabilities=CAPS)
        ca = mac.claim()
        before = self.app.get('job', job['job_id'])
        self.finish_decision(mac, ca, decision(None, 'escalate'))
        self.assertEqual(before, self.app.get('job', job['job_id']))
        self.assertIn('coordinator.escalated', self.event_types(job))
        self.assertFalse(self.current(job)['assessments'])
        self.assertEqual(len(self.coordinators()), 1)

    def test_managed_heartbeat_renews_and_loss_releases_no_recursive_round(self):
        job, mac, ca, cp = self.round()
        first = self.call('GET', self.lease_path(job))
        self.clock.advance(10)
        mac.heartbeat()
        self.assertGreater(self.call('GET', self.lease_path(job))['expires_at'], first['expires_at'])
        self.clock.advance(91)
        self.app.tick()
        self.assertIsNone(self.call('GET', self.lease_path(job)))
        self.assertEqual(len(self.coordinators()), 1)
        self.assertEqual(self.current({'job_id': ca['job_id']})['status'], 'waiting_reconcile')

    def test_accept_applies_manifest_bound_assessment_once_and_releases(self):
        job, mac, ca, cp = self.round()
        out = self.finish_decision(mac, ca, decision(cp))
        mac.complete(ca, checkpoint=out)
        current = self.current(job)
        self.assertEqual(current['acceptance']['status'], 'passed')
        self.assertEqual(len(current['assessments']), 1)
        self.assertEqual(current['assessments'][0]['manifest_sha256'], cp['manifest_sha256'])
        self.assertIsNone(self.call('GET', self.lease_path(job)))
        self.assertEqual(len(self.coordinators()), 1)

    def test_rework_creates_same_root_repair_and_preserves_budget(self):
        job, mac, ca, cp = self.round()
        follow = repair_request()
        self.finish_decision(mac, ca, decision(cp, 'rework', follow))
        relatives = [j for j in self.call('GET', '/jobs')['jobs'] if j['work_package_id'] == job['work_package_id']]
        self.assertEqual(len(relatives), 2)
        self.assertEqual({j['counters']['repairs'] for j in relatives}, {1})
        self.assertEqual(relatives[0]['contract']['budgets']['max_repairs'], 2)
        self.assertEqual(relatives[0]['contract']['harness'], 'codex')
        self.assertEqual(relatives[0]['contract']['harness_params'],
                         dict(model='gpt-6-astra', effort='xhigh', sandbox='workspace-write'))
        self.assertEqual(d.validate_contract(relatives[0]['contract']), relatives[0]['contract'])
        self.assertEqual(self.current(job)['acceptance']['status'], 'rework')
        self.assertEqual(len(self.coordinators()), 1)

    def test_rework_continues_from_assessed_checkpoint_and_inherits_recovery(self):
        job, mac, ca, cp = self.round(contract(recovery={'auto': False, 'side_effects': 'none'}))
        follow = repair_request()
        self.finish_decision(mac, ca, decision(cp, 'rework', follow))
        repair = next(j for j in self.call('GET', '/jobs')['jobs']
                      if j['work_package_id'] == job['work_package_id'] and j['job_id'] != job['job_id'])
        target_manifest = json.loads(self.app.blobs.read(cp['manifest_sha256']))
        self.assertEqual(repair['contract']['inputs_manifest'], p.validate_manifest(target_manifest))
        self.assertEqual(repair['contract']['recovery'], job['contract']['recovery'])

    def test_repair_budget_exhaustion_escalates_but_keeps_assessment(self):
        job, mac, ca, cp = self.round(contract(budgets={'max_repairs': 0}))
        before = self.app.get('job', job['job_id'])
        self.finish_decision(mac, ca, decision(cp, 'rework', repair_request()))
        current = self.assert_assessment_applied(job, ca, cp)
        self.assertEqual(before['counters'], current['counters'])
        self.assertEqual(before['status'], current['status'])
        self.assertIn('coordinator.escalated', self.event_types(job))
        self.assertEqual(len(self.call('GET', '/jobs')['jobs']), 2)

    def test_followup_cannot_override_budgets_but_assessment_is_kept(self):
        job, mac, ca, cp = self.round()
        before = self.app.get('job', job['job_id'])
        follow = repair_request(budgets={'max_repairs': 99})
        self.finish_decision(mac, ca, decision(cp, 'rework', follow))
        current = self.assert_assessment_applied(job, ca, cp)
        self.assertEqual(before['counters'], current['counters'])
        self.assertIn('coordinator.follow_up_rejected', self.event_types(job))

    def test_malformed_profile_id_has_stable_overview_shape(self):
        self.app.config['managed_coordinator'] = {'enabled': True, 'profile_id': []}
        self.assertEqual(self.call('GET', '/overview')['managed_coordinator'],
                         dict(status='profile_missing', profile_id=None, profile=None))

    def test_invalid_decision_leaves_target_unchanged(self):
        job, mac, ca, cp = self.round()
        before = self.app.get('job', job['job_id'])
        value = decision(cp)
        value['assessment']['status'] = 'rework'
        self.finish_decision(mac, ca, value)
        self.assertEqual(before, self.app.get('job', job['job_id']))
        self.assertIn('coordinator.decision_rejected', self.event_types(job))
        self.assertIsNone(self.call('GET', self.lease_path(job)))

    def test_missing_decision_file_rejected(self):
        job, mac, ca, cp = self.round()
        self.finish_decision(mac, ca, decision(cp), filename='other.json')
        self.assertEqual(self.current(job)['acceptance']['status'], 'unverified')
        self.assertIn('coordinator.decision_rejected', self.event_types(job))

    def test_only_followup_rolls_back_when_creation_fails_after_writing(self):
        job, mac, ca, cp = self.round()
        before = self.app.get('job', job['job_id'])
        create = self.app._create_job
        def fail_after_creation(*args, **kwargs):
            create(*args, **kwargs)
            raise ValueError('injected failure after repair creation')
        with patch.object(self.app, '_create_job', side_effect=fail_after_creation):
            out = self.finish_decision(mac, ca, decision(cp, 'rework', repair_request()))
        mac.complete(ca, checkpoint=out)
        current = self.assert_assessment_applied(job, ca, cp)
        self.assertEqual(before['counters'], current['counters'])
        self.assertEqual(len(self.call('GET', '/jobs')['jobs']), 2)
        self.assertEqual(self.app.get('checkpoint', cp['checkpoint_id'])['state'], 'durable')
        self.assertIn('coordinator.follow_up_rejected', self.event_types(job))
        self.assertEqual(self.event_types(job).count('coordinator.follow_up_rejected'), 1)

    def test_takeover_fences_inflight_decision_and_preserves_new_lease(self):
        job, mac, ca, cp = self.round()
        self.lease(job, holder_ref='rescuer', takeover=True, reason='manual control')
        before = self.app.get('job', job['job_id'])
        self.finish_decision(mac, ca, decision(cp))
        self.assertEqual(before, self.app.get('job', job['job_id']))
        self.assertIn('coordinator.decision_rejected', self.event_types(job))
        self.assertEqual(self.call('GET', self.lease_path(job))['holder_ref'], 'rescuer')

    def test_automatic_publication_does_not_invalidate_decision(self):
        self.configured()
        job, worker, a, cp = self.completed()
        mac = self.node('mac', capabilities=CAPS)
        ca = mac.claim()
        worker.publish(a, cp)
        self.finish_decision(mac, ca, decision(cp))
        self.assertEqual(self.current(job)['acceptance']['status'], 'passed')

    def test_changed_target_rejects_stale_context(self):
        self.configured()
        job, worker, a, cp = self.completed()
        self.mutate(job, 'assess', assessment_id=uid(), checkpoint_id=cp['checkpoint_id'], status='blocked', method='human', result='changed', reviewer='user')
        mac = self.node('mac', capabilities=CAPS)
        ca = mac.claim()
        before = self.app.get('job', job['job_id'])
        self.finish_decision(mac, ca, decision(cp))
        self.assertEqual(before, self.app.get('job', job['job_id']))
        self.assertIn('coordinator.decision_rejected', self.event_types(job))

    def test_backup_restore_invalidates_coordinator_lease(self):
        job = self.job()
        token = self.lease(job)['coordinator_token']
        dest = self.temp / 'backup'
        backup(self.temp / 'data', dest, clock=self.clock)
        self.app.close()
        self.app = None
        restore(self.temp / 'data', dest, clock=self.clock)
        self.app = App(self.temp / 'data', clock=self.clock)
        self.assertIsNone(self.call('GET', self.lease_path(job)))
        new = self.lease(job, holder_ref='new')
        self.assertNotEqual(new['coordinator_token'], token)

    def test_client_cannot_forge_coordinator_contract(self):
        value = contract(harness='claude', harness_params={'model': 'x', 'effort': 'y'},
                         coordinates={'job_id': uid(), 'work_package_id': uid()})
        self.call('POST', '/jobs', dict(request_id=uid(), contract=value), status=400)

    def test_concurrent_acquisition_has_exactly_one_owner(self):
        job = self.job()
        def acquire(ref):
            raw = p.canonical_json(dict(request_id=uid(), holder_kind='interactive', holder_ref=ref, ttl_s=90))
            return self.app.handle('POST', p.API_PREFIX + self.lease_path(job), self.headers(''), raw).status
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(acquire, ['one', 'two']))
        self.assertEqual(sorted(statuses), [200, 409])

    def test_distinct_rework_trigger_reuses_unfinished_coordinator(self):
        self.configured()
        job, worker, a, cp = self.completed()
        self.mutate(job, 'assess', assessment_id=uid(), checkpoint_id=cp['checkpoint_id'], status='rework', method='human', result='changed', reviewer='user')
        self.assertEqual(len(self.coordinators()), 1)
        mac = self.node('mac', capabilities=CAPS)
        ca = mac.claim()
        mac.claim(status=204)
        self.finish_decision(mac, ca, decision(cp))  # stale first context
        mac.claim(status=204)
        self.assertEqual(self.current(job)['acceptance']['status'], 'rework')
        self.assertIn('coordinator.decision_rejected', self.event_types(job))
        self.assertEqual(len(self.coordinators()), 1)

    def test_trigger_creation_is_atomic_with_completion(self):
        self.configured()
        worker = self.node('worker')
        job = self.job()
        a = worker.claim()
        cp = worker.checkpoint(a, kind='final')
        with patch.object(self.app, '_create_job', side_effect=ValueError('injected trigger failure')):
            worker.complete(a, checkpoint=cp, status=400)
        self.assertEqual(self.current(job)['status'], 'running')
        self.assertFalse(self.coordinators())
        worker.complete(a, checkpoint=cp)
        self.assertEqual(len(self.coordinators()), 1)

    def test_managed_failure_and_cancel_release_lease(self):
        job, mac, ca, cp = self.round()
        mac.complete(ca, outcome='failed', failure_class='auth_expired')
        self.assertIsNone(self.call('GET', self.lease_path(job)))
        self.assertEqual(len(self.coordinators()), 1)
        self.assertEqual(self.app.get('node', 'mac')['auth_health']['claude'], 'expired')

    def test_cancel_managed_attempt_releases_on_acknowledgment(self):
        job, mac, ca, cp = self.round()
        self.mutate({'job_id': ca['job_id']}, 'cancel')
        self.assertIsNotNone(self.call('GET', self.lease_path(job)))
        mac.complete(ca, outcome='cancelled')
        self.assertIsNone(self.call('GET', self.lease_path(job)))

    def test_side_effect_unknown_escalates_instead_of_repair(self):
        self.configured()
        worker = self.node('worker')
        job = self.job(contract(recovery={'auto': False, 'side_effects': 'idempotent'}))
        a = worker.claim()
        worker.event(a, 'attempt.side_effect', {'op_id': 'work:external', 'state': 'unknown'})
        cp = worker.checkpoint(a, kind='boundary')
        worker.complete(a, outcome='failed', failure_class='network')
        mac = self.node('mac', capabilities=CAPS)
        ca = mac.claim()
        before = self.app.get('job', job['job_id'])
        self.finish_decision(mac, ca, decision(cp, 'rework', repair_request()))
        current = self.assert_assessment_applied(job, ca, cp)
        self.assertEqual(before['counters'], current['counters'])
        self.assertEqual(current['status'], 'waiting_reconcile')
        events = [e for e in self.current(job)['events'] if e['type'] == 'coordinator.escalated']
        self.assertEqual(events[-1]['payload']['reason'], 'side_effect_unknown')

    def test_foreign_checkpoint_rejected(self):
        job, mac, ca, cp = self.round()
        self.finish_decision(mac, ca, decision({'checkpoint_id': uid()}))
        self.assertEqual(self.current(job)['acceptance']['status'], 'unverified')
        self.assertIn('coordinator.decision_rejected', self.event_types(job))

    def test_real_sample_assessment_creates_codex_repair_from_exact_checkpoint(self):
        # Assessment is unmodified from the controller's real Claude rework sample.
        sample = json.loads((Path(__file__).parent / 'fixtures/claude_rework_request.json').read_bytes())
        self.configured()
        worker = self.node('worker')
        worker.record['capabilities']['labels'] = ['portable']
        self.call('POST', '/nodes/register', worker.record, token=worker.token)
        worker.upload(b'old input')
        job = self.job(contract(inputs_manifest=manifest(b'old input', 'obsolete.txt'), excludes=['修改其他文件'],
            requirements={'os': ['linux'], 'arch': ['x86_64'], 'tools': ['python3'], 'labels': ['portable']},
            budgets={'max_attempts': 5, 'max_repairs': 1, 'max_migrations': 0, 'deadline_s': 6000},
            resources={'slots': 1}, publish={'mode': 'manual', 'requires_acceptance': True},
            integration={'target_node_id': 'worker', 'target_path': str(self.temp / 'integration')},
            workspace={'exclude': ['.cache/**']}))
        assignment = worker.claim()
        files = []
        for name, raw in [('greeting.py', b'def greet(name):\n    return "hello, " + name\n'),
                          ('test_greeting.py', b'import unittest\nfrom greeting import greet\n'
                           b'class Tests(unittest.TestCase):\n    def test_name(self):\n'
                           b'        self.assertEqual(greet("ada"), "hello, ada")\n')]:
            files.append(dict(path=name, size=len(raw), sha256=worker.upload(raw), executable=False))
        value = dict(version=1, base=job['inputs_manifest_sha256'], files=files, deleted=['obsolete.txt'])
        cp = worker.checkpoint(assignment, value, kind='final', checkpoint_id=sample['assessment']['checkpoint_id'])
        worker.complete(assignment, checkpoint=cp)
        mac = self.node('mac', capabilities=CAPS)
        ca = mac.claim()
        output = self.finish_decision(mac, ca, sample)
        mac.complete(ca, checkpoint=output)  # Lost completion receipts must not duplicate either operation.
        current = self.assert_assessment_applied(job, ca, cp)
        assessment = current['assessments'][0]
        for key, value in sample['assessment'].items():
            self.assertEqual(assessment[key], value)
        repairs = [j for j in self.call('GET', '/jobs')['jobs']
                   if j['work_package_id'] == job['work_package_id'] and j['job_id'] != job['job_id']]
        self.assertEqual(len(repairs), 1)
        repair, target = repairs[0]['contract'], job['contract']
        follow = sample['follow_up']
        self.assertEqual(d.validate_contract(repair), repair)
        self.assertEqual(repair['harness'], 'codex')
        self.assertEqual(repair['harness_params'], dict(model='gpt-6-astra', effort=follow['effort'], sandbox='workspace-write'))
        for key in ('excludes', 'recovery', 'resources', 'publish', 'integration', 'workspace', 'budgets'):
            self.assertEqual(repair[key], target[key])
        for key in ('os', 'arch', 'labels', 'gpu'):
            self.assertEqual(repair['requirements'][key], target['requirements'][key])
        self.assertEqual(repair['requirements']['tools'], ['python3', 'codex'])
        self.assertEqual(repair['goal'], follow['goal'])
        self.assertEqual(repair['acceptance'], follow['acceptance'])
        for text in [follow['instructions'], follow['goal']] + follow['acceptance'] + target['excludes']:
            self.assertIn(text, repair['prompt'])
        self.assertEqual(repair['inputs_manifest'], self.app.manifest(cp['manifest_sha256']))
        self.assertEqual(repairs[0]['inputs_manifest_sha256'], cp['manifest_sha256'])
        self.assertEqual(repairs[0]['counters']['repairs'], 1)
        self.assertEqual(current['counters']['repairs'], 1)
        self.assertEqual(repairs[0]['protection'], 'protected')
        self.assertNotIn('coordinator.follow_up_rejected', self.event_types(job))
        worker.record['capabilities']['tools']['codex'] = 'stub-only'
        self.call('POST', '/nodes/register', worker.record, token=worker.token)
        repair_assignment = worker.claim()
        self.assertEqual(repair_assignment['job_id'], repairs[0]['job_id'])
        self.assertEqual(repair_assignment['start']['manifest_sha256'], cp['manifest_sha256'])
        work = self.temp / 'materialized-repair'
        materialize(work, repair['inputs_manifest'], self.app.blobs.read)
        self.assertTrue((work / 'greeting.py').is_file())
        self.assertFalse((work / 'obsolete.txt').exists())
        next_manifest = dict(repair['inputs_manifest'], base=cp['manifest_sha256'], deleted=[])
        worker.checkpoint(repair_assignment, next_manifest)

    def test_client_supplied_input_base_still_rejected(self):
        self.call('POST', '/jobs', dict(request_id=uid(), contract=contract(
            inputs_manifest=manifest(b'input', base='a' * 64))), status=400)

    def test_invalid_followup_shapes_preserve_assessment_and_succeeded_attempt(self):
        values = [{'contract': contract(), 'title': 'legacy'}, repair_request(effort='low'),
                  repair_request(acceptance=[]), repair_request(acceptance=[1]), {}, False]
        for index, follow in enumerate(values):
            with self.subTest(follow=follow):
                # New independent roots avoid completion idempotency/state masking a later variant.
                self.configured()
                worker = self.node('worker-' + str(index))
                worker.record['capabilities']['labels'] = ['target-' + str(index)]
                self.call('POST', '/nodes/register', worker.record, token=worker.token)
                job = self.job(contract(requirements={'labels': ['target-' + str(index)]}))
                a = worker.claim()
                cp = worker.checkpoint(a, kind='final')
                worker.complete(a, checkpoint=cp)
                mac = self.node('mac', capabilities=CAPS)
                ca = mac.claim()
                self.finish_decision(mac, ca, decision(cp, 'rework', follow))
                self.assert_assessment_applied(job, ca, cp)
                self.assertEqual(self.current(job)['counters']['repairs'], 0)
                events = [e for e in self.current(job)['events'] if e['type'] == 'coordinator.follow_up_rejected']
                self.assertTrue(events[-1]['payload']['reason'])

    def test_generated_contract_validation_failure_keeps_assessment(self):
        job, mac, ca, cp = self.round()
        validate = d.validate_contract
        def reject_generated(value):
            if value.get('harness') == 'codex':
                raise ValueError('injected invalid generated contract')
            return validate(value)
        with patch.object(d, 'validate_contract', side_effect=reject_generated):
            self.finish_decision(mac, ca, decision(cp, 'rework', repair_request()))
        self.assert_assessment_applied(job, ca, cp)
        self.assertIn('coordinator.follow_up_rejected', self.event_types(job))
        self.assertEqual(self.current(job)['counters']['repairs'], 0)

    def test_missing_followup_is_rejected_separately_from_assessment(self):
        job, mac, ca, cp = self.round()
        value = decision(cp, 'rework')
        del value['follow_up']
        self.finish_decision(mac, ca, value)
        self.assert_assessment_applied(job, ca, cp)
        self.assertIn('coordinator.follow_up_rejected', self.event_types(job))

    def test_illegal_assessment_is_rejected_even_with_illegal_followup(self):
        job, mac, ca, cp = self.round()
        before = self.app.get('job', job['job_id'])
        value = decision(cp, 'rework', {'contract': {}})
        value['assessment']['status'] = 'passed'
        self.finish_decision(mac, ca, value)
        self.assertEqual(before, self.app.get('job', job['job_id']))
        self.assertFalse(self.current(job)['assessments'])
        self.assertIn('coordinator.decision_rejected', self.event_types(job))
        self.assertNotIn('coordinator.follow_up_rejected', self.event_types(job))

    def test_failed_followup_does_not_close_waiting_reconcile_target(self):
        self.configured()
        worker = self.node('worker')
        job = self.job(contract(recovery={'auto': False}))
        a = worker.claim()
        cp = worker.checkpoint(a, kind='boundary')
        worker.complete(a, outcome='failed', failure_class='network')
        mac = self.node('mac', capabilities=CAPS)
        ca = mac.claim()
        with patch.object(self.app, '_create_job', side_effect=ValueError('cannot create repair')):
            self.finish_decision(mac, ca, decision(cp, 'rework', repair_request()))
        current = self.assert_assessment_applied(job, ca, cp)
        self.assertEqual(current['status'], 'waiting_reconcile')
        self.assertEqual(current['counters']['repairs'], 0)
