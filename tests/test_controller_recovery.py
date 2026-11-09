import json

from agent_collab import domain as d, protocol as p
from helpers import ControllerCase, contract, manifest, uid


class RecoveryTest(ControllerCase):
    def test_claim_heartbeat_and_no_duplicate_dispatch_on_restart(self):
        node = self.node()
        job = self.job()
        assignment = node.claim()
        self.assertEqual(self.current(job)['attempts'][0]['status'], 'leased')
        self.clock.advance(20)
        heartbeat = node.heartbeat()
        self.assertEqual(heartbeat['renewed'], [assignment['attempt_id']])
        lease = self.current(job)['attempts'][0]['lease_expires_at']
        self.assertEqual(d.seconds(lease), self.clock() + 90)
        self.clock.advance(80)
        self.restart()
        current = self.current(job)
        self.assertEqual(current['current_epoch'], 1)
        self.assertEqual(len(current['attempts']), 1)
        self.assertEqual(d.seconds(current['attempts'][0]['lease_expires_at']), self.clock() + 90)
        self.assertEqual(self.app.meta()['authority_generation'], assignment['authority_generation'])
        node.heartbeat()
        node.claim(status=204)

    def test_lost_lease_recovers_elsewhere_from_durable_and_fences_old_writer(self):
        old, other = self.node('a'), self.node('b')
        job = self.job()
        first = old.claim()
        old.event(first)
        old.upload(b'confirmed')
        checkpoint = old.checkpoint(first, manifest(b'confirmed'), summary={'completed': [1], 'pending': [2]})
        self.clock.advance(91)
        other.heartbeat([])
        second = other.claim()
        self.assertEqual(second['epoch'], first['epoch'] + 1)
        self.assertEqual(second['recovered_from_attempt'], first['attempt_id'])
        self.assertEqual(second['start']['kind'], 'checkpoint')
        self.assertEqual(second['start']['checkpoint_id'], checkpoint['checkpoint_id'])
        self.assertEqual(second['start']['summary']['completed'], [1])
        self.assertIsNone(second['native_session_ref'])
        current = self.current(job)
        self.assertEqual(current['counters'], dict(attempts=2, infra_retries=1, migrations=1, repairs=0))
        self.assertEqual(current['attempts'][0]['status'], 'lost')
        for result in (old.checkpoint(first, status=409), old.complete(first, checkpoint=checkpoint, status=409),
                       old.publish(first, checkpoint, status=409)):
            self.assertEqual(result['error']['code'], 'fenced')
        returned = old.heartbeat()
        self.assertEqual(returned['renewed'], [])
        self.assertEqual(returned['revoked'][0]['attempt_id'], first['attempt_id'])
        late = old.event(first, 'attempt.progress', {'done': 99})
        self.assertTrue(late['stale'])
        self.assertEqual(self.current(job)['current_attempt_id'], second['attempt_id'])
        self.assertTrue(self.current(job)['events'][-1]['stale'])

    def test_owner_reported_unknown_recovers_without_waiting_for_lease(self):
        old, other = self.node('a'), self.node('b')
        job = self.job()
        first = old.claim()
        old.upload(b'confirmed')
        checkpoint = old.checkpoint(first, manifest(b'confirmed'))
        # agent 重启后确认旧进程已不在，在心跳中报 unknown：租约未过期也立即进入恢复。
        result = self.call('POST', '/nodes/a/heartbeat', {'authority_generation': old.generation, 'auth_health': {},
                           'attempts': [{'attempt_id': first['attempt_id'], 'epoch': first['epoch'], 'state': 'unknown'}]}, token=old.token)
        self.assertEqual(result['revoked'], [{'attempt_id': first['attempt_id'], 'reason': 'unknown_state'}])
        self.assertEqual(self.current(job)['attempts'][0]['status'], 'lost')
        other.heartbeat([])
        second = other.claim()
        self.assertEqual((second['epoch'], second['start']['checkpoint_id']), (first['epoch'] + 1, checkpoint['checkpoint_id']))
        self.assertEqual(old.publish(first, checkpoint, status=409)['error']['code'], 'fenced')

    def test_lease_renewal_does_not_append_events(self):
        node = self.node()
        job = self.job()
        node.claim()
        before = len(self.current(job)['events'])
        for _ in range(5):
            self.clock.advance(5)
            node.heartbeat()
        self.assertEqual(len(self.current(job)['events']), before)

    def test_recovery_prefers_different_failure_domain(self):
        first = self.node('a', failure_domain='power-a')
        self.node('b', failure_domain='power-a')
        last = self.node('c', failure_domain='power-b')
        job = self.job()
        assignment = first.claim()
        first.complete(assignment, outcome='failed', failure_class='network')
        self.app.tick()
        self.assertEqual(last.claim()['recovered_from_attempt'], assignment['attempt_id'])

    def test_no_checkpoint_recovery_uses_baseline(self):
        first, other = self.node('a'), self.node('b')
        job = self.job()
        a = first.claim()
        first.complete(a, outcome='failed', failure_class='infra_retryable')
        b = other.claim()
        self.assertEqual(b['start']['kind'], 'inputs')
        self.assertEqual(b['start']['manifest_sha256'], job['inputs_manifest_sha256'])

    def test_event_dedup_conflict_and_atomic_batch(self):
        node = self.node()
        job = self.job()
        a = node.claim()
        event_id = uid()
        first = node.event(a, event_id=event_id)
        second = node.event(a, event_id=event_id)
        self.assertEqual(first['accepted'], [event_id])
        self.assertEqual(second['duplicates'], [event_id])
        self.assertEqual(node.event(a, payload={'different': True}, event_id=event_id, status=409)['error']['code'], 'conflict')
        novel = uid()
        batch = node.payload(a, events=[{'event_id': novel, 'type': 'attempt.progress', 'payload': {}},
                                        {'event_id': event_id, 'type': 'attempt.progress', 'payload': {}}])
        self.call('POST', '/attempts/' + a['attempt_id'] + '/events', batch, token=node.token, status=409)
        self.assertIsNone(self.app.store.db.execute('SELECT 1 FROM event_keys WHERE event_id=?', (novel,)).fetchone())
        self.app.store.prune_events(0)
        self.assertEqual(node.event(a, event_id=event_id)['duplicates'], [event_id])

    def test_cancel_requires_confirmation_and_never_recovers(self):
        node = self.node()
        job = self.job()
        a = node.claim()
        node.complete(a, outcome='cancelled', status=409)
        current = self.mutate(job, 'cancel')
        self.assertEqual(current['status'], 'cancel_requested')
        self.assertEqual(node.heartbeat()['cancel'], [a['attempt_id']])
        node.complete(a, outcome='cancelled')
        self.app.tick()
        self.assertEqual(self.current(job)['status'], 'cancelled')
        self.assertIsNotNone(self.current(job)['cancel']['confirmed_at'])
        self.assertEqual(len(self.current(job)['attempts']), 1)

    def test_cancel_then_lost_stays_unconfirmed_and_does_not_recover(self):
        node, other = self.node('a'), self.node('b')
        job = self.job()
        node.claim()
        self.mutate(job, 'cancel')
        self.clock.advance(91)
        other.heartbeat([])
        current = self.current(job)
        self.assertEqual(current['status'], 'cancel_requested')
        self.assertIsNone(current['cancel']['confirmed_at'])
        self.assertEqual(len(current['attempts']), 1)
        other.claim(status=204)

    def test_code_failure_never_automatically_retries(self):
        first, other = self.node('a'), self.node('b')
        job = self.job()
        a = first.claim()
        first.complete(a, outcome='failed', failure_class='code_failure')
        self.app.tick()
        other.claim(status=204)
        self.assertEqual(self.current(job)['status'], 'failed')
        self.assertEqual(len(self.current(job)['attempts']), 1)

    def test_side_effect_unknown_intent_and_manual_block_recovery(self):
        for state in ('unknown', 'intent'):
            node = self.node('node-' + state)
            job = self.job()
            # Tie-breaks can select a previously registered node; drain previous nodes.
            a = node.claim()
            node.event(a, 'attempt.side_effect', {'op_id': 'stable-operation', 'state': state, 'detail': 'uncertain'})
            node.complete(a, outcome='failed', failure_class='network')
            self.app.tick()
            self.assertEqual(self.current(job)['status'], 'waiting_reconcile')
            self.assertEqual(len(self.current(job)['attempts']), 1)
            n = next(n for n in self.call('GET', '/nodes')['nodes'] if n['node_id'] == node.node_id)
            self.call('POST', '/nodes/' + node.node_id + '/drain', {'request_id': uid(), 'expected_version': n['version'], 'drain': True})
        node = self.node('z')
        job = self.job(contract(recovery={'side_effects': 'manual'}))
        a = node.claim()
        node.complete(a, outcome='failed', failure_class='network')
        self.assertEqual(self.current(job)['status'], 'waiting_reconcile')

    def test_done_operation_does_not_regress_on_late_intent(self):
        node = self.node()
        job = self.job(contract(recovery={'side_effects': 'idempotent'}))
        a = node.claim()
        for state in ('intent', 'done', 'intent'):
            node.event(a, 'attempt.side_effect', {'op_id': 'stable', 'state': state})
        node.complete(a, outcome='failed', failure_class='network')
        self.assertEqual(self.current(job)['status'], 'recovering')

    def test_attempt_deadline_and_migration_budgets(self):
        node = self.node()
        job = self.job(contract(budgets={'max_attempts': 1}))
        a = node.claim()
        node.complete(a, outcome='failed', failure_class='network')
        self.app.tick()
        current = self.current(job)
        self.assertEqual(current['status'], 'failed')
        self.assertIn('budget_exhausted:attempts', current['status_reason'])
        self.assertEqual(len(current['attempts']), 1)
        second = self.job(contract(budgets={'deadline_s': 1}))
        a = node.claim()
        self.clock.advance(2)
        node.complete(a, outcome='failed', failure_class='network')
        self.assertIn('budget_exhausted:deadline_s', self.current(second)['status_reason'])
        third = self.job(contract(budgets={'max_migrations': 0}))
        node.claim()
        error = self.mutate(third, 'retry', reason='move', expected_status=409)
        self.assertEqual(error['error']['code'], 'budget_exhausted')

    def test_explicit_retry_fences_current_and_counts_migration_once(self):
        old, other = self.node('a'), self.node('b')
        job = self.job()
        first = old.claim()
        self.mutate(job, 'retry', reason='maintenance', exclude_node_ids=['a'])
        second = other.claim()
        self.assertEqual(second['recovered_from_attempt'], first['attempt_id'])
        self.assertEqual(self.current(job)['counters']['migrations'], 1)
        self.assertEqual(self.current(job)['counters']['infra_retries'], 0)
        self.assertEqual(old.checkpoint(first, status=409)['error']['code'], 'fenced')

    def test_auth_expired_marks_shared_nodes_and_holds_job(self):
        node = self.node('a')
        self.node('b')
        job = self.job(contract(harness='codex', harness_params={}))
        a = node.claim()
        node.complete(a, outcome='failed', failure_class='auth_expired')
        self.app.tick()
        self.assertEqual(self.current(job)['status'], 'waiting_capability')
        self.assertTrue(all(n['auth_health']['codex'] == 'expired' for n in self.call('GET', '/nodes')['nodes']))

    def test_unknown_effect_takes_precedence_and_late_upload_cannot_promote(self):
        node, other = self.node('a'), self.node('b')
        job = self.job()
        assignment = node.claim()
        checkpoint_id = uid()
        node.checkpoint(assignment, manifest(b'late'), checkpoint_id=checkpoint_id, status=409)
        node.event(assignment, 'attempt.side_effect', {'op_id': 'unknown', 'state': 'unknown'})
        node.complete(assignment, outcome='failed', failure_class='code_failure')
        self.assertEqual(self.current(job)['status'], 'waiting_reconcile')
        node.upload(b'late')
        self.assertEqual(self.call('GET', '/checkpoints/' + checkpoint_id)['state'], 'uploading')
        self.assertIsNone(self.current(job)['durable_checkpoint_id'])


class PublicationTest(ControllerCase):
    def test_complete_and_publish_cas_acceptance_idempotency_and_integration(self):
        node = self.node()
        job = self.job(contract(publish={'requires_acceptance': True}, integration={'target_node_id': 'a'}))
        a = node.claim()
        boundary = node.checkpoint(a)
        self.assertEqual(node.complete(a, checkpoint=boundary, status=409)['error']['code'], 'conflict')
        final = node.checkpoint(a, kind='final')
        complete = node.complete(a, checkpoint=final)
        self.assertEqual(complete, node.complete(a, checkpoint=final))
        self.assertEqual(node.complete(a, checkpoint=final, report={'changed': True}, status=409)['error']['code'], 'conflict')
        self.assertEqual(node.publish(a, final, expected_base_version=99, status=409)['error']['code'], 'version_mismatch')
        self.assertEqual(node.publish(a, final, status=409)['error']['code'], 'conflict')
        assessment = self.mutate(job, 'assess', assessment_id=uid(), checkpoint_id=final['checkpoint_id'],
                                 status='passed', method='unit tests', result='passed', reviewer='test')
        self.assertEqual(assessment['manifest_sha256'], final['manifest_sha256'])
        request_id = uid()
        receipt = node.publish(a, final, request_id=request_id)
        self.assertEqual(receipt, node.publish(a, final, request_id=request_id))
        self.assertEqual(receipt['delivery_version'], 1)
        self.assertEqual(node.publish(a, final, request_id=request_id, expected_base_version=1, status=409)['error']['code'], 'conflict')
        self.assertEqual(node.publish(a, final, status=409)['error']['code'], 'version_mismatch')
        self.assertEqual(self.mutate(job, 'integration', expected_status=409, checkpoint_id=boundary['checkpoint_id'],
                                     **{'status': 'integrated', 'detail': {}})['error']['code'], 'conflict')
        integration = self.call('POST', '/jobs/' + job['job_id'] + '/integration', {
            'request_id': uid(), 'checkpoint_id': final['checkpoint_id'], 'status': 'integrated',
            'detail': {'changed': ['file'], 'conflicts': [], 'backup': None}}, token=node.token)
        self.assertEqual(integration['integration_status'], 'integrated')
        self.assertEqual(integration['delivery_version'], 1)

    def test_manual_publish_has_no_lease_token_but_checks_epoch(self):
        node = self.node()
        job = self.job(contract(publish={'mode': 'manual'}))
        a = node.claim()
        cp = node.checkpoint(a, kind='final')
        node.complete(a, checkpoint=cp)
        node.publish(a, cp, status=403)
        receipt = self.mutate(job, 'publish', attempt_id=a['attempt_id'], epoch=a['epoch'],
                              checkpoint_id=cp['checkpoint_id'], expected_base_version=0)
        self.assertEqual(receipt['delivery_version'], 1)
        self.mutate(job, 'retry', reason='revision')
        error = self.mutate(job, 'publish', expected_status=409, attempt_id=a['attempt_id'], epoch=a['epoch'],
                            checkpoint_id=cp['checkpoint_id'], expected_base_version=1)
        self.assertEqual(error['error']['code'], 'fenced')
