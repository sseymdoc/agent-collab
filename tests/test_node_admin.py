"""节点管理（protocol §13.5–§13.7）：配对码 API、维护、退役（保留历史）、结构化管理操作、原始日志取回、协调设置。

controller 一侧只经公开 API；节点一侧用真实的 Agent 对着同一个进程内的 controller。不调用模型。
"""
import argparse
import copy
import json
from pathlib import Path
import sys
import unittest
from urllib.parse import urlsplit

from agent_collab import domain as d, protocol as p
from agent_collab.node_agent import Agent, log_streams, pair
from fake_controller import no_auth
from helpers import ControllerCase, contract, uid
import test_coordinator as tc
from test_clients_models import A, ClientCase, caps


class NodeAdminCase(ClientCase):
    def node_version(self, node_id):
        return next(n for n in self.call('GET', '/nodes')['nodes'] if n['node_id'] == node_id)['version']

    def retire(self, node_id, status=200, token=None, **body):
        body.setdefault('request_id', uid())
        body.setdefault('retired', True)
        body.setdefault('reason', 'decommissioned')
        if 'expected_version' not in body:
            listed = self.call('GET', '/nodes')['nodes'] + self.call('GET', '/nodes?retired=1')['nodes']
            body['expected_version'] = next(n for n in listed if n['node_id'] == node_id)['version']
        return self.call('POST', '/nodes/%s/retire' % node_id, body, token=token, status=status)


class PairingCodeTests(NodeAdminCase):
    def test_node_codes_are_issued_over_the_api_and_never_replace_a_node_by_accident(self):
        devices = self.pair(scopes=['read', 'devices'], name='ops')['client_token']
        operator = self.pair('operator', name='op')['client_token']
        self.assertEqual(self.call('POST', '/nodes/codes', {'request_id': uid(), 'node_id': 'fresh'}, token=operator, status=403)
                         ['error']['details'], {'required_scope': 'devices'})
        request = {'request_id': uid(), 'node_id': 'fresh', 'ttl_s': 120}
        issued = self.call('POST', '/nodes/codes', request, token=devices, status=201)
        self.assertEqual((issued['node_id'], set(issued)), ('fresh', {'code', 'node_id', 'expires_at'}))
        self.assertEqual(self.call('POST', '/nodes/codes', request, token=devices, status=409)['error']['code'], 'conflict')  # 配对码不重放
        paired = self.call('POST', '/nodes/pair', {'node_id': 'fresh', 'code': issued['code']}, headers={})
        self.assertTrue(paired['node_token'])
        self.call('POST', '/nodes/pair', {'node_id': 'fresh', 'code': issued['code']}, headers={}, status=401)  # 一次性

        # 已登记的节点：配对会替换它的凭据，必须明确要求。
        existing = self.node('worker')
        self.assertEqual(self.call('POST', '/nodes/codes', {'request_id': uid(), 'node_id': 'worker'}, token=devices, status=409)
                         ['error']['code'], 'node_exists')
        code = self.call('POST', '/nodes/codes', {'request_id': uid(), 'node_id': 'worker', 'replace': True}, token=devices, status=201)['code']
        self.call('POST', '/nodes/pair', {'node_id': 'worker', 'code': code}, headers={})
        self.call('POST', '/nodes/worker/claim', {}, token=existing.token, status=401)  # 旧凭据随之作废
        for bad in ({'node_id': 'Bad Id'}, {'node_id': 'x', 'ttl_s': 0}, {'node_id': 'x', 'ttl_s': 99999}):
            self.call('POST', '/nodes/codes', dict(bad, request_id=uid()), token=devices, status=400)
        actions = [e['payload'] for e in self.call('GET', '/events?limit=1000')['events'] if e['type'] == 'audit.action'
                   and e['payload']['action'] == 'POST /nodes/codes']
        self.assertEqual([(a['node_id'], a['replace'], a['actor']['name']) for a in actions], [('fresh', False, 'ops'), ('worker', True, 'ops')])
        self.assertNotIn(issued['code'], json.dumps(self.call('GET', '/events?limit=1000')))


class MaintenanceTests(NodeAdminCase):
    def maintenance(self, node_id, active, status=200, **body):
        body.update(request_id=uid(), active=active, expected_version=self.node_version(node_id))
        return self.call('POST', '/nodes/%s/maintenance' % node_id, body, status=status)

    def state(self, node_id):
        node = next(n for n in self.call('GET', '/nodes')['nodes'] if n['node_id'] == node_id)
        return node['maintenance_state'], node['drain'], (node.get('maintenance') or {}).get('reason')

    def test_maintenance_stops_dispatch_keeps_running_work_and_restores_the_previous_state(self):
        node = self.node('n')
        running = self.job(contract(harness_params={'steps': [{'sleep': 60}]}))
        assignment = node.claim()
        self.maintenance('n', True, status=400)  # 必须说明原因
        entered = self.maintenance('n', True, reason='更换磁盘')
        self.assertEqual((entered['drain'], entered['maintenance']['reason'], entered['maintenance']['drain_before']), (True, '更换磁盘', False))
        self.assertEqual(self.state('n'), ('draining', True, '更换磁盘'))  # 还有任务在跑
        self.assertEqual(node.heartbeat()['renewed'], [assignment['attempt_id']])  # 已在运行的任务不受影响

        waiting = self.job()
        reasons = next(r['reasons'] for r in self.call('GET', '/jobs/%s/explain' % waiting['job_id'])['nodes'] if r['node_id'] == 'n')
        self.assertIn('maintenance', reasons)
        self.assertIsNone(node.claim(status=204))
        node.complete(assignment, checkpoint=node.checkpoint(assignment, kind='final'))
        self.assertEqual(self.state('n'), ('active', True, '更换磁盘'))

        left = self.maintenance('n', False)
        self.assertEqual((left['drain'], left['maintenance']), (False, None))
        self.assertEqual(self.state('n'), ('none', False, None))
        self.assertEqual(node.claim()['job_id'], waiting['job_id'])

    def test_a_node_drained_before_maintenance_stays_drained_afterwards(self):
        self.node('n')
        self.call('POST', '/nodes/n/drain', {'request_id': uid(), 'expected_version': self.node_version('n'), 'drain': True})
        self.maintenance('n', True, reason='升级')
        self.assertEqual(self.maintenance('n', False)['drain'], True)  # 用户此前有意暂停接单：维护结束后不替他恢复


class RetirementTests(NodeAdminCase):
    def test_retirement_waits_for_work_and_dependants_to_be_settled(self):
        self.app.config.update(copy.deepcopy(tc.CONFIG))  # 协调 profile 的节点列表里有 mac
        worker = self.node('worker')
        # 不自动恢复：节点失联后任务停在待对账，而不是接管到别的节点。
        job = self.job(contract(coordination={'policy': 'none'}, recovery={'auto': False, 'side_effects': 'none'},
                                harness_params={'steps': [{'sleep': 60}]}))
        assignment = worker.claim()
        self.node('mac', capabilities=tc.CAPS)
        blocked = self.retire('worker', status=409)['error']
        self.assertEqual((blocked['code'], blocked['details']['blockers']), ('retire_blocked', {'active_attempts': [assignment['attempt_id']]}))
        self.assertEqual(self.retire('mac', status=409)['error']['details']['blockers'], {'coordination_profile': 'standby'})
        self.retire('worker', status=400, reason='')

        # 节点失联、任务停在待对账：仍不能退役，先把任务处理掉。
        self.clock.advance(self.app.config['lease_ttl_s'] + 1)
        self.app.tick()
        self.assertEqual(self.current(job)['status'], 'waiting_reconcile')
        self.assertEqual(self.retire('worker', status=409)['error']['details']['blockers'], {'unsettled_jobs': [job['job_id']]})
        self.mutate(job, 'cancel')
        self.assertTrue(self.retire('worker')['retired_at'])

    def test_retired_node_keeps_history_and_leaves_scheduling_catalogs_and_statistics(self):
        self.put(expected_revision=0, codex={'default': {'model': A, 'effort': 'high'}, 'allowed': None})
        old = self.node('old', capabilities=caps({A: ['high']}))
        done = self.codex_job()
        assignment = old.claim()
        old.complete(assignment, checkpoint=old.checkpoint(assignment, kind='final'))
        self.assertEqual(self.models()['selectable']['codex'], [{'model': A, 'efforts': ['high'], 'nodes': ['old']}])

        retired = self.retire('old', reason='替换为新设备')
        self.assertEqual((retired['retire_reason'], retired['retired_by']['kind'], retired['drain']), ('替换为新设备', 'admin', True))
        self.assertEqual(self.retire('old')['retired_at'], retired['retired_at'])  # 再次退役不改变什么

        # 记录还在，历史任务仍引用它；它不再出现在调度、目录、可选组合与总览的节点列表里。
        self.assertEqual(self.call('GET', '/nodes')['nodes'], [])
        self.assertEqual([n['node_id'] for n in self.call('GET', '/nodes?retired=1')['nodes']], ['old'])
        history = self.current(done)
        self.assertEqual((history['attempts'][0]['node_id'], history['status']), ('old', 'completed'))
        view = self.models()
        self.assertEqual((view['catalog']['nodes'], view['selectable']), ({}, {'codex': [], 'unknown_nodes': []}))
        overview = self.call('GET', '/overview')
        self.assertEqual((overview['nodes'], overview['retired_nodes']), ([], 1))
        waiting = self.codex_job()
        self.assertEqual((self.call('GET', '/jobs/%s/explain' % waiting['job_id'])['nodes'], self.current(waiting)['status']),
                         ([], 'waiting_capability'))
        draft = self.call('POST', '/contracts/draft', {'template': 'codex-change', 'goal': 'g'})
        self.assertIn('no_node_supports_model', [w['code'] for w in draft['warnings']])

        # 凭据已作废；退役的身份不能直接再配对，也不能进协调节点列表。
        self.call('POST', '/nodes/old/heartbeat', {'authority_generation': 1, 'attempts': []}, token=old.token, status=401)
        self.assertEqual(self.call('POST', '/nodes/codes', {'request_id': uid(), 'node_id': 'old', 'replace': True}, status=409)['error']['code'], 'node_retired')
        self.assertEqual(self.call('PUT', '/settings/coordination', {'request_id': uid(), 'expected_revision': 0, 'enabled': True,
                                                                     'node_ids': ['old']}, status=409)['error']['code'], 'node_retired')
        self.assertEqual(self.call('POST', '/nodes/old/operations', {'request_id': uid(), 'kind': 'probe'}, status=409)['error']['code'], 'node_retired')
        events = [e['type'] for e in self.call('GET', '/events?limit=1000')['events']]
        self.assertIn('node.retired', events)
        self.clock.advance(self.app.config['lease_ttl_s'] * 3)
        self.app.tick()  # 退役的节点不再产生“不可达”之类的状态事件
        self.assertEqual(self.call('GET', '/nodes?retired=1')['nodes'][0]['version'], retired['version'])

        # 恢复身份：记录与历史不变，需要重新配对才有凭据，且保持不接单。
        restored = self.retire('old', retired=False)
        self.assertEqual((restored['retired_at'], restored['drain']), (None, True))
        code = self.call('POST', '/nodes/codes', {'request_id': uid(), 'node_id': 'old', 'replace': True}, status=201)['code']
        self.call('POST', '/nodes/pair', {'node_id': 'old', 'code': code}, headers={})
        self.assertEqual(self.current(done)['attempts'][0]['node_id'], 'old')


class OperationTests(NodeAdminCase):
    def report(self, node, op_id, status_value, status=200, **body):
        return self.call('POST', '/nodes/%s/operations/%s' % (node.node_id, op_id), dict(body, status=status_value), token=node.token, status=status)

    def test_operation_is_requested_received_and_completed_once(self):
        node = self.node('n')
        viewer = self.pair('viewer', name='v')['client_token']
        ops = self.pair(scopes=['read', 'devices'], name='ops')['client_token']
        self.assertEqual(self.call('POST', '/nodes/n/operations', {'request_id': uid(), 'kind': 'probe'}, token=viewer, status=403)
                         ['error']['details'], {'required_scope': 'devices'})
        for bad in ({'kind': 'shell', 'params': {'cmd': 'whoami'}}, {'kind': 'probe', 'params': {'x': 1}}, {'kind': 'probe', 'argv': ['x']}):
            self.call('POST', '/nodes/n/operations', dict(bad, request_id=uid()), token=ops, status=400)  # 没有任意命令
        request = {'request_id': uid(), 'kind': 'probe'}
        op = self.call('POST', '/nodes/n/operations', request, token=ops, status=201)
        self.assertEqual((op['status'], op['requested_by']['name'], op['kind'], op['params']), ('requested', 'ops', 'probe', {}))
        self.assertEqual(self.call('POST', '/nodes/n/operations', request, token=ops, status=201)['op_id'], op['op_id'])  # 重放不产生第二项

        self.assertEqual(node.heartbeat()['operations'], [{'op_id': op['op_id'], 'kind': 'probe', 'params': {}}])
        self.assertEqual(self.report(node, op['op_id'], 'received')['status'], 'received')
        self.assertEqual(node.heartbeat()['operations'], [])  # 已收到的不再下发
        self.assertEqual(self.call('GET', '/operations/' + op['op_id'], token=viewer)['status'], 'received')
        other = self.node('other')
        self.report(other, op['op_id'], 'succeeded', status=404, result={})  # 别的节点不能代报
        self.report(node, op['op_id'], 'succeeded', result={'auth': {'codex': {'status': 'ok'}}, 'token': 'secret-value'})
        done = self.call('GET', '/operations/' + op['op_id'], token=viewer)
        self.assertEqual((done['status'], done['result']['auth'], done['result']['token']), ('succeeded', {'codex': {'status': 'ok'}}, '[redacted]'))
        self.assertEqual(self.report(node, op['op_id'], 'failed', error='late')['status'], 'succeeded')  # 迟到的上报不改结果
        listed = self.call('GET', '/nodes/n/operations', token=viewer)['operations']
        self.assertEqual([(o['op_id'], o['status'], 'result' in o) for o in listed], [(op['op_id'], 'succeeded', False)])
        states = [e['payload']['status'] for e in self.call('GET', '/events?limit=1000')['events'] if e['type'] == 'node.operation']
        self.assertEqual(states, ['requested', 'received', 'succeeded'])
        # 活动页从新到旧翻页：before=0 取最新的几条，再用其中最小的序号继续往前。
        everything = [e['seq'] for e in self.call('GET', '/events?limit=1000')['events']]
        latest = [e['seq'] for e in self.call('GET', '/events?before=0&limit=3')['events']]
        earlier = [e['seq'] for e in self.call('GET', '/events?before=%d&limit=3' % latest[0])['events']]
        self.assertEqual((latest, earlier), (everything[-3:], everything[-6:-3]))

    def test_operation_that_is_not_completed_in_time_is_reported_as_timed_out(self):
        node = self.node('n')
        op = self.call('POST', '/nodes/n/operations', {'request_id': uid(), 'kind': 'probe'}, status=201)
        self.clock.advance(self.app.config['node_operation_timeout_s'] + 1)
        self.app.tick()
        self.assertEqual(self.call('GET', '/operations/' + op['op_id'])['status'], 'timed_out')
        self.assertEqual(node.heartbeat()['operations'], [])
        self.assertEqual(self.report(node, op['op_id'], 'succeeded', result={})['status'], 'timed_out')
        self.clock.advance(86401)
        self.app.tick()
        self.call('GET', '/operations/' + op['op_id'], status=404)  # 结果只保留一天

    def test_raw_log_requests_are_bound_to_the_job_attempt_and_bounded(self):
        node = self.node('n')
        job, other = self.job(), self.job()
        assignment = node.claim()
        viewer = self.pair('viewer', name='v')['client_token']
        path = '/jobs/%s/logs' % job['job_id']
        op = self.call('POST', path, {'request_id': uid(), 'attempt_id': assignment['attempt_id'], 'stream': 'harness.stdout',
                                      'offset': 10, 'max_bytes': 4096}, token=viewer, status=201)  # 能读成果就能读它的原始输出
        self.assertEqual((op['kind'], op['node_id'], op['params']), ('log_tail', 'n', {
            'attempt_id': assignment['attempt_id'], 'stream': 'harness.stdout', 'offset': 10, 'max_bytes': 4096}))
        self.assertEqual(self.call('POST', path, {'request_id': uid(), 'attempt_id': assignment['attempt_id']}, status=201)['params']['stream'], 'index')
        self.call('POST', '/jobs/%s/logs' % other['job_id'], {'request_id': uid(), 'attempt_id': assignment['attempt_id']}, status=409)
        for bad in ({'stream': '../../node.token'}, {'stream': 'C:/Windows/win.ini'}, {'stream': 'harness.stdout', 'max_bytes': 262145},
                    {'stream': 'harness.stdout', 'offset': -1}, {'stream': 'check.100.stdout'}, {'path': '/etc/passwd'}):
            self.call('POST', path, dict(bad, request_id=uid(), attempt_id=assignment['attempt_id']), status=400)
        self.call('POST', '/nodes/n/operations', {'request_id': uid(), 'kind': 'log_tail', 'params': {'attempt_id': uid()}},
                  token=viewer, status=403)  # 直接对节点下发操作需要 devices
        # 有 devices 权限也不行：原始输出只能经按任务绑定的接口取，绕不过 attempt 归属与流名、大小的检查。
        self.call('POST', '/nodes/n/operations', {'request_id': uid(), 'kind': 'log_tail', 'params': {'attempt_id': uid()}}, status=400)


class CoordinationSettingsTests(NodeAdminCase):
    def test_coordination_is_configured_through_versioned_settings_separately_from_codex(self):
        view = self.call('GET', '/settings/coordination')
        self.assertEqual((view['settings'], view['effective']['status']),
                         ({'revision': 0, 'updated_at': None, 'updated_by': None, 'enabled': False, 'node_ids': []}, 'profile_missing'))
        settings_only = self.pair(scopes=['read', 'settings'], name='s')['client_token']
        operator = self.pair('operator', name='op')['client_token']
        body = {'request_id': uid(), 'expected_revision': 0, 'enabled': True, 'node_ids': ['mac']}
        self.assertEqual(self.call('PUT', '/settings/coordination', body, token=operator, status=403)['error']['details'], {'required_scope': 'settings'})
        saved = self.call('PUT', '/settings/coordination', body, token=settings_only)
        self.assertEqual((saved['revision'], saved['enabled'], saved['node_ids'], saved['updated_by']['name']), (1, True, ['mac'], 's'))
        self.assertEqual(self.call('PUT', '/settings/coordination', body, token=settings_only)['revision'], 1)  # 重放
        # 启用了但还没有协调模型：如实显示未就绪；模型在模型设置的 claude 一组里，与 codex 的设置互不影响。
        self.assertEqual(self.call('GET', '/settings/coordination')['effective']['status'], 'profile_missing')
        self.put(expected_revision=0, claude={'default': {'model': 'c-model', 'effort': 'max'}})
        view = self.call('GET', '/settings/coordination')
        self.assertEqual((view['effective'], view['unknown_nodes'], self.models()['settings']['codex']['default']),
                         ({'status': 'ready', 'profile_id': 'settings', 'source': 'settings',
                           'profile': {'harness': 'claude', 'model': 'c-model', 'effort': 'max', 'node_ids': ['mac']}}, ['mac'],
                          {'model': None, 'effort': None}))
        self.assertEqual(self.call('GET', '/overview')['managed_coordinator']['status'], 'ready')

        # 协调回合按设置里的节点列表与模型创建。
        worker = self.node('worker')
        self.job()
        a = worker.claim()
        mac = self.node('mac', capabilities=tc.CAPS)
        worker.complete(a, checkpoint=worker.checkpoint(a, kind='final'))
        rounds = [j for j in self.call('GET', '/jobs')['jobs'] if 'coordinates' in j['contract']]
        self.assertEqual((len(rounds), rounds[0]['contract']['harness_params']), (1, {'model': 'c-model', 'effort': 'max'}))
        self.assertIsNone(worker.claim(status=204))
        self.assertEqual(mac.claim()['job_id'], rounds[0]['job_id'])

        stale = self.call('PUT', '/settings/coordination', dict(body, request_id=uid(), enabled=False), status=409)['error']
        self.assertEqual((stale['code'], stale['details']['current']['revision']), ('version_mismatch', 1))
        off = self.call('PUT', '/settings/coordination', {'request_id': uid(), 'expected_revision': 1, 'enabled': False, 'node_ids': ['mac']})
        self.assertEqual((off['revision'], self.call('GET', '/settings/coordination')['effective']['status']), (2, 'disabled'))
        self.assertEqual([(h['revision'], h['before']['enabled'], h['after']['enabled']) for h in self.call('GET', '/settings/coordination')['history']],
                         [(1, False, True), (2, True, False)])
        for bad in ({'enabled': 'yes', 'node_ids': []}, {'enabled': True, 'node_ids': ['a', 'a']}, {'enabled': True, 'node_ids': ['Bad Id']},
                    {'enabled': True, 'node_ids': [], 'model': 'x'}):
            self.call('PUT', '/settings/coordination', dict(bad, request_id=uid(), expected_revision=2), status=400)

    def test_deployments_configured_in_config_json_keep_working_until_settings_are_saved(self):
        self.app.config.update(copy.deepcopy(tc.CONFIG))
        view = self.call('GET', '/settings/coordination')
        self.assertEqual((view['settings']['revision'], view['settings']['enabled'], view['settings']['node_ids'], view['effective']['profile_id']),
                         (0, True, ['mac'], 'standby'))
        saved = self.call('PUT', '/settings/coordination', {'request_id': uid(), 'expected_revision': 0, 'enabled': True, 'node_ids': ['mac', 'win']})
        effective = self.call('GET', '/settings/coordination')['effective']
        # 节点列表改由设置决定；没有另设协调模型时，模型与 effort 仍取 config.json 的 profile。
        self.assertEqual((saved['revision'], effective['profile']['node_ids'], effective['profile']['model'], effective['source']),
                         (1, ['mac', 'win'], tc.PROFILE['model'], 'settings'))


class AgentOperationTests(ControllerCase):
    """真实的节点代理对着进程内的 controller：领取操作、在自己的进程上下文里执行、上报结果。"""
    legacy_settings = False

    def setUp(self):
        super().setUp()
        self.directory = self.temp / 'agent'
        pair(argparse.Namespace(controller='https://controller.invalid', code=self.app.pairing_code('worker'), node_id='worker',
                                data_dir=str(self.directory), ca_file=None, slots=1, host_slots=1, physical_host_id='host',
                                failure_domain='domain', label=[]), self.transport)
        capabilities = dict(os='linux', arch='x86_64', tools={}, gpu=[], harness={'stub': '1', 'codex': None}, labels=[])
        self.agent = Agent(self.directory, transport=self.transport, clock=self.clock, capabilities=capabilities, auth_runner=no_auth,
                           catalog_probe=lambda argv, env: {'status': 'unknown', 'source': 'test', 'fetched_at': None, 'cli_version': None,
                                                            'reason': 'command_missing', 'models': [], 'provider': None})
        self.addCleanup(self.agent.close)

    drop_received = 0

    def transport(self, method, url, headers, body):
        if self.drop_received and '/operations/' in url and b'"received"' in (body or b''):
            self.drop_received -= 1
            return 503, {}, b'{}'  # 代理临时故障：确认没有送到
        result = self.app.handle(method, urlsplit(url).path, headers, body)
        return result.status, result.headers, result.body

    def drive(self, condition, limit=400):
        import time
        for _ in range(limit):
            self.agent.cycle()
            self.clock.advance(1.5)
            if condition():
                return
            time.sleep(.01)
        self.fail('condition not reached')

    def operation(self, op_id):
        return self.call('GET', '/operations/' + op_id)

    def test_probe_runs_in_the_worker_process_context_and_reports_login_state(self):
        self.drive(lambda: self.call('GET', '/nodes')['nodes'])
        op = self.call('POST', '/nodes/worker/operations', {'request_id': uid(), 'kind': 'probe'}, status=201)
        self.drive(lambda: self.operation(op['op_id'])['status'] in ('succeeded', 'failed'))
        done = self.operation(op['op_id'])
        self.assertEqual(done['status'], 'succeeded', done)
        result = done['result']
        import os
        self.assertEqual((result['context']['pid'], result['auth']['codex']['status'], result['auth']['claude']['status'],
                          result['catalog']['codex']['status'], result['catalog']['codex']['reason']),
                         (os.getpid(), 'unknown', 'unknown', 'unknown', 'command_missing'))
        self.assertEqual(set(result['context']) >= {'user', 'ssh', 'tty', 'launchd_label', 'windows_session', 'version', 'version_dir', 'platform'}, True)
        self.assertNotIn(self.agent.client.token, json.dumps(done))
        self.assertTrue(done['received_at'] and done['finished_at'])

    def test_operation_whose_receipt_was_not_delivered_is_acknowledged_again_after_reconnecting(self):
        self.drive(lambda: self.call('GET', '/nodes')['nodes'])
        self.drop_received = 1
        op = self.call('POST', '/nodes/worker/operations', {'request_id': uid(), 'kind': 'probe'}, status=201)
        self.drive(lambda: self.operation(op['op_id'])['status'] in ('succeeded', 'failed'))
        self.assertEqual((self.drop_received, self.operation(op['op_id'])['status']), (0, 'succeeded'))

    def test_raw_output_is_read_in_chunks_from_the_attempt_only_and_redacted(self):
        self.drive(lambda: self.call('GET', '/nodes')['nodes'])
        marker = 'hello-from-the-step'
        job = self.job(contract(coordination={'policy': 'none'}, harness_params={'steps': [
            {'run': [sys.executable, '-c', 'import sys; print(%r); print("x" * 20000); sys.stderr.write("warn\\n")' % marker]}]}))
        self.drive(lambda: self.current(job)['status'] == 'completed')
        attempt_id = self.current(job)['attempts'][0]['attempt_id']

        def read(**params):
            op = self.call('POST', '/jobs/%s/logs' % job['job_id'], dict(params, request_id=uid(), attempt_id=attempt_id), status=201)
            self.drive(lambda: self.operation(op['op_id'])['status'] in ('succeeded', 'failed'))
            done = self.operation(op['op_id'])
            self.assertEqual(done['status'], 'succeeded', done)
            return done['result']

        index = read()
        self.assertEqual((index['retained'], [s['stream'] for s in index['streams']]), (True, ['step.0.stderr', 'step.0.stdout']))
        first = read(stream='step.0.stdout', max_bytes=1024)
        self.assertEqual((first['offset'], first['next_offset'], first['eof'], first['total_bytes'] > 20000), (0, 1024, False, True))
        self.assertIn(marker, first['data'])
        rest = read(stream='step.0.stdout', offset=first['next_offset'], max_bytes=262144)
        self.assertEqual((rest['eof'], rest['next_offset'], len(first['data']) + len(rest['data']) >= 20000), (True, rest['total_bytes'], True))
        self.assertEqual(read(stream='step.0.stderr')['data'].strip(), 'warn')
        # 事件里只有截断的摘要；完整输出在节点上，经这条按任务绑定的接口才取得到。
        events = json.dumps(self.current(job)['events'])
        self.assertNotIn('x' * 6000, events)
        missing = read(stream='harness.stdout')  # 这个 attempt 没有这一路输出
        self.assertEqual((missing['missing'], 'data' in missing), (True, False))

        # 输出里出现的凭据在节点上脱敏后才离开；别的 attempt（或已清理的）如实报告没有保留。
        attempt_dir = self.directory / 'attempts' / attempt_id
        (attempt_dir / 'stdout.jsonl').write_text('token=' + self.agent.client.token + '\n', encoding='utf-8')
        leaked = self.agent.guarded(lambda: self.agent.log_tail({'attempt_id': attempt_id, 'stream': 'harness.stdout'}))[1]
        self.assertNotIn(self.agent.client.token, leaked['data'])
        # 分块读取：无论块边界落在凭据的哪个位置，拼起来都拿不到它，也拿不到它的任何一段。
        token = self.agent.client.token
        content = ('before ' + token + ' after\n').encode('utf-8')
        (attempt_dir / 'stdout.jsonl').write_bytes(content)
        for cut in range(1, len(content)):
            parts = [self.agent.log_tail({'attempt_id': attempt_id, 'stream': 'harness.stdout', 'offset': start, 'max_bytes': length})
                     for start, length in ((0, cut), (cut, len(content)))]
            joined = ''.join(part['data'] for part in parts)
            self.assertEqual((parts[0]['next_offset'], parts[1]['eof']), (cut, True))
            self.assertNotIn(token, joined)
            for start in range(len(token) - 7):  # 凭据的任何连续 8 字节都不出现
                self.assertNotIn(token[start:start + 8], joined, cut)
            self.assertTrue(joined.startswith('before ') and joined.endswith(' after\n'), (cut, joined))
        self.assertEqual(self.agent.log_tail({'attempt_id': uid()}), {'retained': False, 'streams': []})
        # 固定布局之外的文件、符号链接都不在可取范围里。
        (attempt_dir / 'lease.copy').write_text('secret', encoding='utf-8')
        linked = attempt_dir / 'step-1'
        linked.mkdir()
        try:  # 布局合法、但文件是指向别处的链接：不算可取的输出（Windows 普通用户可能不能建链接）
            (linked / 'stdout.jsonl').symlink_to(self.directory / 'agent.json')
        except OSError:
            linked.rmdir()
        self.assertEqual(set(log_streams(attempt_dir)), {'harness.stdout', 'step.0.stdout', 'step.0.stderr'})


if __name__ == '__main__':
    unittest.main()
