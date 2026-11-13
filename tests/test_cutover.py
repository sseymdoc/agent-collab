"""切换所需的机制（protocol §13.9）：提交闸门、备份之后的活动汇总、切换屏障；以及普通重启与屏障的区别。"""
import json
import unittest

from agent_collab import protocol as p
from agent_collab.controller import App, backup, barrier
from helpers import contract, uid
from test_clients_models import ClientCase


class CutoverCase(ClientCase):
    def set_intake(self, mode, status=200, token=None, **body):
        body.update(request_id=uid(), mode=mode, expected_version=self.call('GET', '/overview')['controller']['metadata_version'])
        return self.call('POST', '/intake', body, token=token, status=status)

    def pause(self):
        self.call('POST', '/dispatch', {'request_id': uid(), 'paused': True,
                                        'expected_version': self.call('GET', '/overview')['controller']['metadata_version']})

    def submit(self, token=None, status=201, **body):
        body.setdefault('request_id', uid())
        body.setdefault('title', 'job')
        body.setdefault('contract', contract())
        return self.call('POST', '/jobs', body, token=token, status=status)


class IntakeTests(CutoverCase):
    def test_ordinary_submission_is_closed_by_the_controller_not_by_convention(self):
        owner = self.pair('owner', name='owner')['client_token']
        operator = self.pair('operator', name='op')['client_token']
        self.assertEqual(self.call('GET', '/overview')['intake']['mode'], 'open')
        project = self.call('POST', '/projects', {'request_id': uid(), 'name': 'real'}, status=201)
        project_job = self.submit(token=owner, project_id=project['project_id'])
        self.mutate(project_job, 'cancel')
        accepted = self.submit(token=operator)
        self.assertEqual(self.set_intake('closed', token=operator, reason='x', status=403)['error']['details'], {'required_scope': 'maintain'})
        self.set_intake('closed', status=400)  # 关闭必须说明原因
        closed = self.set_intake('closed', token=owner, reason='切换前停止提交')
        self.assertEqual((closed['intake']['mode'], closed['intake']['reason'], closed['intake']['by']['name']), ('closed', '切换前停止提交', 'owner'))
        # 关闭后：任何凭据的普通提交都被拒绝，包括管理令牌；读取与对已有任务的操作不受影响。
        for token in (operator, owner, None):
            refused = self.submit(token=token, status=409)['error']
            self.assertEqual((refused['code'], refused['details']['intake']['mode']), ('intake_closed', 'closed'))
        self.assertEqual(self.submit(token=owner, acceptance_run=True, status=409)['error']['code'], 'intake_closed')
        self.assertEqual(len(self.call('GET', '/jobs', token=operator)['jobs']), 2)
        self.mutate(accepted, 'cancel')

        # 只开放验收运行：要有 maintain 权限、带标记、不产生外部写入；这样的任务带着标记，事后可以整批找出来。
        self.set_intake('acceptance', token=owner, reason='升级后验收')
        self.assertEqual(self.submit(token=owner, status=409)['error']['code'], 'intake_closed')
        self.assertEqual(self.submit(token=operator, acceptance_run=True, status=403)['error']['details'], {'required_scope': 'maintain'})
        for unsafe in ({'project_id': project['project_id']},
                       {'work_package_id': project_job['work_package_id']},  # 修订会继承根的项目
                       {'contract': contract(integration={'target_node_id': 'n', 'target_path': '/srv/project'})},
                       {'contract': contract(recovery={'auto': True, 'side_effects': 'manual'})}):
            self.submit(token=owner, acceptance_run=True, status=400, **unsafe)
        self.submit(token=owner, acceptance_run='yes', status=400)
        run = self.submit(token=owner, acceptance_run=True)
        self.assertEqual((run['acceptance_run'], accepted['acceptance_run']), (True, False))
        self.assertEqual([j['job_id'] for j in self.call('GET', '/jobs?acceptance_run=1')['jobs']], [run['job_id']])

        opened = self.set_intake('open', token=owner)
        self.assertEqual((opened['intake']['mode'], opened['intake']['reason']), ('open', None))
        self.submit(token=operator)
        modes = [e['payload']['mode'] for e in self.call('GET', '/events?limit=1000')['events'] if e['type'] == 'controller.intake']
        self.assertEqual(modes, ['closed', 'acceptance', 'open'])


class ActivityTests(CutoverCase):
    def test_activity_after_a_backup_tells_disposable_acceptance_state_from_real_work(self):
        node = self.node('n')
        before = self.submit()
        a = node.claim()
        node.complete(a, checkpoint=node.checkpoint(a, kind='final'))
        self.app.close()
        manifest = backup(self.temp / 'data', self.temp / 'backup', clock=self.clock)
        self.app = App(self.temp / 'data', clock=self.clock)
        events = self.call('GET', '/events?limit=1000')['events']
        recorded = next(e['seq'] for e in events if e['type'] == 'controller.backup')
        self.assertEqual(manifest['event_seq'], recorded - 1)  # 快照里最后一条事件；紧随其后的是备份自己的记录
        path = '/activity?after=%d' % manifest['event_seq']
        self.assertEqual({k: v for k, v in self.call('GET', path).items() if k in ('jobs', 'ordinary_jobs', 'disposable')},
                         {'jobs': [], 'ordinary_jobs': 0, 'disposable': True})

        # 备份之后只跑了验收运行（含它的检查点）：恢复这份备份只丢掉可丢弃的状态。
        self.set_intake('acceptance', reason='验收')
        run = self.submit(acceptance_run=True)
        a = node.claim()
        node.complete(a, checkpoint=node.checkpoint(a, kind='final'))
        activity = self.call('GET', path)
        self.assertEqual((activity['ordinary_jobs'], activity['acceptance_jobs'], activity['disposable'], activity['counts']['checkpoint.durable']),
                         (0, 1, True, 1))
        self.assertEqual([(j['job_id'], j['acceptance_run']) for j in activity['jobs']], [(run['job_id'], True)])

        # 一旦受理了普通任务，就不再是可丢弃的：回退要按“已有新任务”处理。
        self.set_intake('open')
        real = self.submit()
        activity = self.call('GET', path)
        self.assertEqual((activity['ordinary_jobs'], activity['disposable'], [j['job_id'] for j in activity['jobs'] if not j['acceptance_run']]),
                         (1, False, [real['job_id']]))
        viewer = self.pair('viewer', name='v')['client_token']
        self.call('GET', path, token=viewer)


class BarrierTests(CutoverCase):
    def running(self):
        node = self.node('n')
        job = self.submit(contract=contract(harness_params={'steps': [{'sleep': 60}]}))
        assignment = node.claim()
        node.event(assignment)
        return node, job, assignment

    def reopen(self):
        self.app.close()
        self.app = App(self.temp / 'data', clock=self.clock)

    def test_ordinary_restart_keeps_generation_and_execution_rights(self):
        """普通的停止再启动（原位升级就是这样）不撤销任何东西：这不是让升级前的请求失效的办法。"""
        node, job, assignment = self.running()
        generation = self.call('GET', '/overview')['controller']['authority_generation']
        self.reopen()
        self.assertEqual(self.call('GET', '/overview')['controller']['authority_generation'], generation)
        beat = node.heartbeat()
        self.assertEqual((beat['renewed'], beat['revoked'], beat['reconcile_required']), ([assignment['attempt_id']], [], False))
        cp = node.checkpoint(assignment, kind='final')  # 重启前领取的 attempt 照常提交检查点并完成
        node.complete(assignment, checkpoint=cp)
        self.assertEqual(self.current(job)['status'], 'completed')

    def test_barrier_revokes_everything_issued_before_it_and_keeps_identities_and_history(self):
        finished_node = self.node('done')
        finished = self.submit(contract=contract(publish={'mode': 'manual', 'requires_acceptance': False}))
        fa = finished_node.claim()
        fcp = finished_node.checkpoint(fa, kind='final')
        finished_node.complete(fa, checkpoint=fcp)
        self.call('POST', '/nodes/done/drain', {'request_id': uid(), 'drain': True,
                                                'expected_version': next(n for n in self.call('GET', '/nodes')['nodes'] if n['node_id'] == 'done')['version']})
        node, job, assignment = self.running()
        client = self.pair('owner', name='owner')['client_token']
        lease = self.call('POST', '/packages/%s/coordinator' % job['work_package_id'], {
            'request_id': uid(), 'holder_kind': 'interactive', 'holder_ref': 'me', 'ttl_s': 600, 'takeover': False, 'reason': ''}, token=client)
        before = self.call('GET', '/overview')['controller']['authority_generation']
        data = self.temp / 'data'

        # 前置条件逐项检查：没暂停派单、提交闸门仍为 open，任一项不满足都拒绝。
        self.app.close()
        with self.assertRaises(ValueError):
            barrier(data, uid(), '切换', clock=self.clock)
        self.app = App(data, clock=self.clock)
        self.pause()
        self.app.close()
        with self.assertRaises(ValueError):  # 已暂停、闸门仍为 open
            barrier(data, uid(), '切换', clock=self.clock)
        self.app = App(data, clock=self.clock)
        self.call('POST', '/dispatch', {'request_id': uid(), 'paused': False,
                                        'expected_version': self.call('GET', '/overview')['controller']['metadata_version']})
        self.set_intake('closed', reason='切换')
        # 新屏障要求先明确处置成功但未发布的成果；弃置保留检查点与历史。
        self.mutate(finished, 'dismiss', reason='切换前明确弃置')
        self.app.close()
        with self.assertRaises(ValueError):  # 闸门已关、没暂停派单
            barrier(data, uid(), '切换', clock=self.clock)
        self.app = App(data, clock=self.clock)
        self.pause()
        self.app.close()
        request_id = uid()
        done = barrier(data, request_id, '切换到新版本', clock=self.clock)
        self.assertEqual((done['generation_before'], done['authority_generation'], done['revoked_active_attempts'], done['replayed']),
                         (before, before + 1, 1, False))
        again = barrier(data, request_id, '切换到新版本', clock=self.clock)  # 同一次屏障重复执行不再推进代次
        self.assertEqual((again['authority_generation'], again['replayed']), (before + 1, True))
        self.app = App(data, clock=self.clock)
        overview = self.call('GET', '/overview')
        self.assertEqual((overview['controller']['authority_generation'], overview['dispatch_paused']), (before + 1, True))

        # 屏障之前签发的执行权：带旧代次的心跳被要求对账，旧租约的检查点、完成与发布都以 stale_generation 被拒绝。
        beat = node.heartbeat()
        self.assertEqual((beat['reconcile_required'], beat['revoked'], beat['renewed']),
                         (True, [{'attempt_id': assignment['attempt_id'], 'reason': 'stale_generation'}], []))
        for late in (node.checkpoint(assignment, kind='final', status=409),
                     node.complete(assignment, outcome='failed', failure_class='code_failure', status=409),
                     finished_node.publish(fa, fcp, status=409)):
            self.assertEqual((late['error']['code'], late['error']['details']['reason']), ('fenced', 'stale_generation'))
        stuck = self.current(job)
        self.assertEqual((stuck['status'], stuck['status_reason'], stuck['attempts'][0]['status']), ('waiting_reconcile', 'stale_generation', 'lost'))
        # 屏障之前取得的协调租约不再有效。
        self.assertIsNone(self.call('GET', '/packages/%s/coordinator' % job['work_package_id']))
        self.assertTrue(lease['coordinator_token'])

        # 没有被撤销的东西：节点凭据、客户端凭据、管理令牌照常认证；已结束任务的历史与检查点原样保留。
        node.generation = before + 1
        self.assertEqual(node.heartbeat([])['reconcile_required'], False)
        self.assertEqual(self.call('GET', '/client', token=client)['identity']['name'], 'owner')
        history = self.current(finished)
        self.assertEqual((history['status'], history['attempts'][0]['status'], history['checkpoints'][0]['state'], history['request_hash']),
                         ('completed', 'succeeded', 'durable', finished['request_hash']))
        events = [e['payload'] for e in self.call('GET', '/events?limit=1000')['events'] if e['type'] == 'controller.barrier']
        self.assertEqual([(e['request_id'], e['reason'], e['revoked_active_attempts']) for e in events], [(request_id, '切换到新版本', 1)])


if __name__ == '__main__':
    unittest.main()
