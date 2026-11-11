"""客户端授权、模型设置、受理时冻结与修复继承（protocol §12）；只经公开 API，不调用模型。"""
import argparse
import copy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
import urllib.request

from agent_collab import domain as d, protocol as p
from agent_collab.controller import HTTPHandler
from agent_collab.node_agent import Agent, pair
from fake_controller import FakeController, no_auth
from helpers import ControllerCase, contract, manifest, uid
import test_coordinator as tc
from test_e2e_local import sockets_ok

ROOT = Path(__file__).resolve().parents[1]
A, B = 'model-a', 'model-b'
EMPTY_CLAUDE = {'default': {'model': None, 'effort': None}}


def caps(models=None, status='ok'):
    """带 codex 的节点能力；models 为 {模型: [effort...]}，None 表示旧节点（不上报目录）。"""
    value = {'os': 'linux', 'arch': 'x86_64', 'tools': {'python3': '3.9'}, 'gpu': [], 'labels': [],
             'harness': {'stub': '1', 'codex': '1'}}
    if models is not None:
        value['models'] = {'codex': {'status': status, 'source': 'test', 'fetched_at': None, 'cli_version': '1', 'reason': None,
                                     'models': [{'id': m, 'efforts': list(e), 'default_effort': None} for m, e in models.items()]}}
    return value


class Rounds:
    """复用协调者用例的夹具：完成一个目标任务、领取并结束它的协调回合。"""
    configured, completed, round, finish_decision, coordinators = (
        tc.CoordinatorTests.configured, tc.CoordinatorTests.completed, tc.CoordinatorTests.round,
        tc.CoordinatorTests.finish_decision, tc.CoordinatorTests.coordinators)


class ClientCase(Rounds, ControllerCase):
    legacy_settings = False  # 新部署：模型设置从未设置开始

    def pair(self, role='owner', name='desk', kind='desktop', scopes=None):
        code = self.app.client_code(role, scopes)
        return self.call('POST', '/clients/pair', {'code': code, 'name': name, 'kind': kind}, headers={})

    def put(self, token=None, status=200, **body):
        body.setdefault('request_id', uid())
        return self.call('PUT', '/settings/models', body, token=token, status=status)

    def models(self):
        return self.call('GET', '/settings/models')

    def codex_job(self, token=None, status=201, **params):
        return self.call('POST', '/jobs', {'request_id': uid(), 'title': 'codex',
                                           'contract': contract(harness='codex', harness_params=params)}, token=token, status=status)


class ClientAuthTests(ClientCase):
    def test_pairing_code_is_single_use_expiring_and_binds_the_controller(self):
        code = self.app.client_code('viewer', ttl=60)
        self.call('POST', '/clients/pair', {'code': code, 'name': '', 'kind': 'desktop'}, headers={}, status=400)
        self.call('POST', '/clients/pair', {'code': code, 'name': 'desk', 'kind': 'browser'}, headers={}, status=400)
        paired = self.call('POST', '/clients/pair', {'code': code, 'name': 'desk', 'kind': 'desktop'}, headers={})
        self.assertEqual((paired['scopes'], paired['name'], paired['kind']), (['read'], 'desk', 'desktop'))
        self.assertEqual(paired['controller_id'], self.app.controller_id)
        self.assertEqual(paired['features'], list(p.FEATURES))
        self.call('POST', '/clients/pair', {'code': code, 'name': 'again', 'kind': 'cli'}, headers={}, status=401)
        expired = self.app.client_code('owner', ttl=60)
        self.clock.advance(61)
        self.call('POST', '/clients/pair', {'code': expired, 'name': 'late', 'kind': 'cli'}, headers={}, status=401)
        for ttl in (0, 3601, 1.5):
            with self.assertRaises(ValueError):
                self.app.client_code('owner', ttl=ttl)
        who = self.call('GET', '/client', token=paired['client_token'])
        self.assertEqual(who['identity'], {'kind': 'client', 'client_id': paired['client_id'], 'name': 'desk', 'scopes': ['read']})
        self.assertEqual(who['controller']['controller_id'], self.app.controller_id)
        self.assertEqual(self.call('GET', '/client')['identity']['scopes'], list(p.SCOPES))  # 管理令牌
        # controller 只保存哈希：凭据与配对码都不在数据文件里。
        self.app.store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        stored = b''.join(f.read_bytes() for f in (self.temp / 'data').glob('controller.db*'))
        for secret in (paired['client_token'], code, expired):
            self.assertNotIn(secret.encode(), stored)
        listed = self.call('GET', '/clients')['clients']
        self.assertEqual([set(c) for c in listed], [{'client_id', 'name', 'kind', 'scopes', 'created_at', 'revoked_at', 'version'}])

    def test_codes_issued_over_the_api_need_maintain_and_are_not_replayable(self):
        owner, operator = self.pair('owner')['client_token'], self.pair('operator')['client_token']
        body = {'request_id': uid(), 'scopes': ['tasks', 'read'], 'ttl_s': 120}
        self.assertEqual(self.call('POST', '/clients/codes', body, token=operator, status=403)['error']['details'],
                         {'required_scope': 'maintain'})
        issued = self.call('POST', '/clients/codes', body, token=owner, status=201)
        self.assertEqual(issued['scopes'], ['read', 'tasks'])
        self.assertEqual(self.call('POST', '/clients/codes', body, token=owner, status=409)['error']['code'], 'conflict')
        paired = self.call('POST', '/clients/pair', {'code': issued['code'], 'name': 'bot', 'kind': 'agent'}, headers={})
        self.assertEqual(paired['scopes'], ['read', 'tasks'])
        for bad in ({'role': 'root'}, {'scopes': []}, {'scopes': ['read', 'everything']}, {'role': 'owner', 'scopes': ['read']}, {}):
            self.call('POST', '/clients/codes', dict(bad, request_id=uid()), token=owner, status=400)
        # maintain 只能签发不超过自己权限的配对码，不能借配对码换取 owner。
        maintainer = self.pair(role=None, scopes=['maintain'])['client_token']
        self.assertEqual(self.call('POST', '/clients/codes', {'request_id': uid(), 'role': 'owner'}, token=maintainer, status=403)['error']['details'],
                         {'required_scope': 'read'})
        self.assertEqual(self.call('POST', '/clients/codes', {'request_id': uid(), 'scopes': ['maintain']}, token=maintainer, status=201)['scopes'],
                         ['maintain'])

    def test_every_route_checks_the_granted_scope(self):
        worker = self.node('worker')
        job = self.job()
        attempt = worker.claim()
        wp, jid = job['work_package_id'], job['job_id']
        viewer, operator = self.pair('viewer')['client_token'], self.pair('operator')['client_token']
        for path in ('/overview', '/jobs', '/jobs/' + jid, '/jobs/' + jid + '/explain', '/jobs/' + jid + '/events', '/nodes',
                     '/events', '/events/stream?cursor=0', '/settings/models', '/templates', '/baselines',
                     '/packages/' + wp + '/coordinator', '/client'):
            self.call('GET', path, token=viewer)
        version = self.current(job)['version']
        lease = {'request_id': uid(), 'holder_kind': 'interactive', 'holder_ref': 'x', 'ttl_s': 60, 'takeover': False, 'reason': ''}
        mutations = [
            ('POST', '/jobs', {'request_id': uid(), 'contract': contract()}, 'tasks'),
            ('POST', '/jobs/' + jid + '/cancel', {'request_id': uid(), 'expected_version': version}, 'tasks'),
            ('POST', '/jobs/' + jid + '/retry', {'request_id': uid(), 'expected_version': version, 'reason': 'x'}, 'tasks'),
            ('POST', '/jobs/' + jid + '/assess', {'request_id': uid(), 'expected_version': version}, 'tasks'),
            ('POST', '/jobs/' + jid + '/publish', {'request_id': uid(), 'attempt_id': attempt['attempt_id']}, 'tasks'),
            ('POST', '/jobs/' + jid + '/integration', {'request_id': uid()}, 'tasks'),
            ('POST', '/contracts/draft', {'template': 'diagnostic', 'goal': 'x'}, 'tasks'),
            ('POST', '/baselines', {'request_id': uid(), 'name': 'b', 'manifest': manifest()}, 'tasks'),
            ('PUT', '/blobs/' + p.sha256_bytes(b'x'), b'x', 'tasks'),
            ('POST', '/packages/' + wp + '/coordinator', lease, 'tasks'),
            ('DELETE', '/packages/' + wp + '/coordinator', None, 'tasks'),
            ('PUT', '/settings/models', {'request_id': uid(), 'expected_revision': 0}, 'settings'),
            ('POST', '/nodes/worker/drain', {'request_id': uid(), 'expected_version': 1, 'drain': True}, 'devices'),
            ('POST', '/dispatch', {'request_id': uid(), 'expected_version': 1, 'paused': True}, 'maintain'),
            ('GET', '/doctor', None, 'maintain'),
            ('GET', '/clients', None, 'maintain'),
            ('POST', '/clients/codes', {'request_id': uid(), 'role': 'owner'}, 'maintain'),
            ('POST', '/clients/' + uid() + '/revoke', {'request_id': uid(), 'expected_version': 1}, 'maintain'),
        ]
        before = self.app.store.db.execute('SELECT COUNT(*) FROM records').fetchone()[0]
        for method, path, body, scope in mutations:
            with self.subTest(viewer=method + ' ' + path):
                self.assertEqual(self.call(method, path, body, token=viewer, status=403)['error']['details'], {'required_scope': scope})
        for method, path, body, scope in mutations:
            if scope != 'tasks':
                with self.subTest(operator=method + ' ' + path):
                    self.assertEqual(self.call(method, path, body, token=operator, status=403)['error']['details'],
                                     {'required_scope': scope})
        # 被拒绝的请求没有留下任何记录变化。
        self.assertEqual(self.app.store.db.execute('SELECT COUNT(*) FROM records').fetchone()[0], before)
        self.assertEqual(len(self.call('GET', '/jobs')['jobs']), 1)
        self.assertFalse(self.call('GET', '/nodes')['nodes'][0]['drain'])
        self.assertFalse(self.call('GET', '/overview')['dispatch_paused'])
        # operator 具备 tasks：能取得协调租约，但接管需要 maintain。
        self.call('POST', '/packages/' + wp + '/coordinator', lease, token=operator)
        takeover = dict(lease, request_id=uid(), holder_ref='y', takeover=True, reason='test')
        self.assertEqual(self.call('POST', '/packages/' + wp + '/coordinator', takeover, token=operator, status=403)['error']['details'],
                         {'required_scope': 'maintain'})
        self.call('POST', '/packages/' + wp + '/coordinator', takeover, token=self.pair('owner')['client_token'])

    def test_revocation_is_immediate_and_leaves_nodes_and_other_clients_alone(self):
        worker = self.node('worker')
        first, second = self.pair('owner', 'one'), self.pair('owner', 'two')
        one, two = first['client_token'], second['client_token']
        record = next(c for c in self.call('GET', '/clients', token=two)['clients'] if c['client_id'] == first['client_id'])
        path = '/clients/' + first['client_id'] + '/revoke'
        stale = self.call('POST', path, {'request_id': uid(), 'expected_version': 9}, token=two, status=409)
        self.assertEqual(stale['error']['code'], 'version_mismatch')
        self.call('GET', '/client', token=one)
        body = {'request_id': uid(), 'expected_version': record['version']}
        revoked = self.call('POST', path, body, token=two)
        self.assertIsNotNone(revoked['revoked_at'])
        self.assertEqual(self.call('POST', path, body, token=two), revoked)  # 同一请求重放
        for target in ('/client', '/overview', '/events/stream?cursor=0'):
            self.call('GET', target, token=one, status=401)
        self.call('GET', '/overview', token=two)
        worker.heartbeat()
        self.assertEqual(self.call('GET', '/nodes')['nodes'][0]['status'], 'online')
        self.call('DELETE', '/client', token=two)  # 自我撤销不需要 maintain
        self.call('GET', '/client', token=two, status=401)
        self.assertEqual([bool(c['revoked_at']) for c in self.call('GET', '/clients')['clients']], [True, True])
        self.assertEqual([e['type'] for e in self.call('GET', '/events')['events'] if e['type'].startswith('client.')],
                         ['client.paired', 'client.paired', 'client.revoked', 'client.revoked'])

    def test_client_and_node_credentials_do_not_cross(self):
        worker = self.node('worker')
        client = self.pair('owner')['client_token']
        self.call('POST', '/nodes/worker/claim', {}, token=client, status=403)
        self.call('POST', '/nodes/worker/heartbeat', {'authority_generation': 1, 'attempts': []}, token=client, status=403)
        self.call('POST', '/nodes/register', worker.record, token=client, status=403)
        for path in ('/jobs', '/client', '/settings/models', '/clients'):
            self.call('GET', path, token=worker.token, status=403)
        self.call('PUT', '/settings/models', {'request_id': uid(), 'expected_revision': 0}, token=worker.token, status=403)

    def test_request_origin_is_recorded_once_and_stale_versions_do_not_overwrite(self):
        client = self.pair('owner', 'laptop')
        token = client['client_token']
        body = {'request_id': uid(), 'title': 'from client', 'contract': contract()}
        job = self.call('POST', '/jobs', body, token=token, status=201)
        actor = {'kind': 'client', 'client_id': client['client_id'], 'name': 'laptop'}
        self.assertEqual(job['created_by'], actor)
        self.assertEqual(self.call('POST', '/jobs', body, token=token)['job_id'], job['job_id'])  # request_id 重试不重复创建
        self.assertEqual(self.call('POST', '/jobs', dict(body, title='changed'), token=token, status=409)['error']['code'], 'conflict')
        self.assertEqual(len(self.call('GET', '/jobs')['jobs']), 1)
        audits = [e for e in self.current(job)['events'] if e['type'] == 'audit.action']
        self.assertEqual([a['payload'] for a in audits], [{'actor': actor, 'action': 'POST /jobs', 'request_id': body['request_id']}])
        stale = self.call('POST', '/jobs/' + job['job_id'] + '/cancel',
                          {'request_id': uid(), 'expected_version': job['version'] + 5}, token=token, status=409)
        self.assertEqual(stale['error']['code'], 'version_mismatch')
        self.assertEqual(self.current(job)['status'], job['status'])
        self.assertEqual(self.job()['created_by'], {'kind': 'admin', 'client_id': None, 'name': None})

    def test_every_api_response_names_the_controller(self):
        admin = {'authorization': 'Bearer ' + self.admin}
        for method, path, headers in (('GET', '/overview', admin), ('GET', '/missing', admin), ('GET', '/overview', {}),
                                      ('POST', '/clients/pair', {}), ('HEAD', '/jobs', admin)):
            result = self.app.handle(method, p.API_PREFIX + path, headers, b'')
            self.assertIn(('X-Controller-Id', self.app.controller_id), result.headers, path)
        self.assertNotIn('X-Controller-Id', dict(self.app.handle('GET', '/healthz', {}, b'').headers))

    def test_web_console_can_be_retired_without_touching_client_or_admin_access(self):
        login = self.app.handle('POST', p.API_PREFIX + '/session', {}, p.canonical_json({'token': self.admin}))
        cookie = {'cookie': dict(login.headers)['Set-Cookie'].split(';')[0]}
        self.assertEqual(self.app.handle('GET', p.API_PREFIX + '/overview', cookie, b'').status, 200)
        token = self.pair('owner')['client_token']
        self.app.config['web_console'] = False
        self.assertEqual(self.app.handle('GET', p.API_PREFIX + '/overview', cookie, b'').status, 401)
        self.assertEqual(self.app.handle('POST', p.API_PREFIX + '/session', {}, p.canonical_json({'token': self.admin})).status, 403)
        self.assertEqual(self.app.handle('GET', '/index.html', {}, b'').status, 404)
        self.call('GET', '/overview', token=token)
        self.call('GET', '/overview')


@unittest.skipUnless(sockets_ok(), 'loopback socket 不可用')
class StreamRevocationTests(ClientCase):
    def test_open_event_stream_is_told_and_closed_when_its_client_is_revoked(self):
        class Server(ThreadingHTTPServer):
            daemon_threads = True
        server = Server(('127.0.0.1', 0), HTTPHandler)
        server.app, server.stopping = self.app, threading.Event()
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        thread.start()
        try:
            client = self.pair('viewer')
            request = urllib.request.Request('http://127.0.0.1:%d%s/events/stream?cursor=0' % (server.server_address[1], p.API_PREFIX),
                                             headers={'Authorization': 'Bearer ' + client['client_token']})
            with urllib.request.urlopen(request, timeout=20) as stream:
                self.assertEqual(stream.headers['X-Controller-Id'], self.app.controller_id)
                self.assertTrue(stream.readline().startswith(b'id: '))  # 已在正常收事件
                record = self.call('GET', '/clients')['clients'][0]
                self.call('POST', '/clients/' + record['client_id'] + '/revoke',
                          {'request_id': uid(), 'expected_version': record['version']})
                rest = stream.read()  # 服务端在下一轮重新认证后关闭连接
            self.assertTrue(rest.endswith(b'event: unauthorized\ndata: {}\n\n'), rest[-200:])
        finally:
            server.stopping.set()
            server.shutdown()
            server.server_close()
            thread.join(5)


class ModelSettingsTests(ClientCase):
    def test_new_database_starts_unset_and_rejects_instead_of_guessing(self):
        self.assertEqual(self.models()['settings'], d.EMPTY_SETTINGS)
        error = self.codex_job(status=400)['error']
        self.assertEqual((error['code'], error['details']), ('invalid', {'reason': 'model_default_unset'}))
        self.assertEqual(self.codex_job(status=400, model=A)['error']['details'], {'reason': 'model_default_unset'})
        job = self.codex_job(model=A, effort='low')  # 部署未作限制时，显式给全即可
        self.assertEqual(job['model_policy'], {'schema': 1, 'settings_revision': 0, 'requested': {'model': A, 'effort': 'low'},
                                               'resolved': {'model': A, 'effort': 'low'},
                                               'source': {'model': 'task_override', 'effort': 'task_override'}})
        self.assertIsNone(self.job()['model_policy'])  # 不调用模型的任务没有模型策略
        for bad in ({'model': 'bad name', 'effort': 'low'}, {'model': A, 'effort': 'Very High'}, {'model': 5, 'effort': 'low'}):
            self.codex_job(status=400, **bad)
        self.job()
        self.restart()
        self.assertEqual(self.models()['settings'], d.EMPTY_SETTINGS)  # 有了任务也不会被写成迁移值

    def test_settings_persist_with_revision_conflicts_history_and_cross_client_reads(self):
        first, second = self.pair('owner', 'one'), self.pair('owner', 'two')
        one, two = first['client_token'], second['client_token']
        codex = {'default': {'model': A, 'effort': 'high'}, 'allowed': {A: ['high', 'max'], B: ['low']}}
        saved = self.put(one, expected_revision=0, codex=codex)
        self.assertEqual((saved['revision'], saved['codex'], saved['claude']), (1, codex, EMPTY_CLAUDE))
        self.assertEqual(saved['updated_by'], {'kind': 'client', 'client_id': first['client_id'], 'name': 'one'})
        self.assertEqual(self.call('GET', '/settings/models', token=two)['settings'], saved)
        conflict = self.put(two, status=409, expected_revision=0, codex=dict(codex, default={'model': B, 'effort': 'low'}))['error']
        self.assertEqual((conflict['code'], conflict['details']['current']), ('version_mismatch', saved))
        self.assertEqual(self.models()['settings'], saved)  # 过期的写入没有覆盖
        claude = {'default': {'model': 'coordinator-model', 'effort': 'xhigh'}}
        updated = self.put(two, expected_revision=1, claude=claude)
        self.assertEqual((updated['revision'], updated['codex'], updated['claude']), (2, codex, claude))  # 两组互不覆盖
        for bad in (dict(codex, default={'model': B, 'effort': 'high'}), dict(codex, allowed={}), dict(codex, allowed={A: []}),
                    dict(codex, allowed={A: ['high', 'high']}), dict(codex, default={'model': A, 'effort': None}),
                    dict(codex, extra=1), {'default': codex['default']}):
            with self.subTest(bad=bad):
                self.put(status=400, expected_revision=2, codex=bad)
        self.put(status=400, expected_revision=2, claude={'default': {'model': 'x'}})
        self.put(status=400, expected_revision=2, unknown=1)
        self.put(status=400, codex=codex)
        self.assertEqual(self.put(self.pair('viewer')['client_token'], status=403, expected_revision=2)['error']['details'],
                         {'required_scope': 'settings'})
        self.restart()
        view = self.models()
        self.assertEqual(view['settings'], updated)
        self.assertEqual([(h['revision'], h['actor']['name'], h['before']['codex'], h['after']['codex']) for h in view['history']],
                         [(1, 'one', d.EMPTY_SETTINGS['codex'], codex), (2, 'two', codex, codex)])
        self.assertEqual(len([e for e in self.call('GET', '/events')['events'] if e['type'] == 'settings.updated']), 2)
        # 应答丢失后原样重放：返回第一次的结果，不当作版本冲突，也不再递增 revision；同一 request_id 换了内容则拒绝。
        ident = uid()
        third = self.put(one, request_id=ident, expected_revision=2, claude=EMPTY_CLAUDE)
        self.assertEqual(self.put(one, request_id=ident, expected_revision=2, claude=EMPTY_CLAUDE), third)
        self.assertEqual(self.models()['settings']['revision'], 3)
        self.assertEqual(self.put(one, status=409, request_id=ident, expected_revision=3, claude=EMPTY_CLAUDE)['error']['code'], 'conflict')
        self.call('PUT', '/settings/models', {'expected_revision': 3}, status=400)  # request_id 必填

    def test_existing_deployment_keeps_its_previous_fixed_combination_once(self):
        self.job()
        with self.app.store.transaction():
            self.app.store.delete('settings', 'models')  # 升级前的库：有任务，没有设置记录
        (self.temp / 'data' / 'config.json').write_text(json.dumps(tc.CONFIG), encoding='utf-8')
        self.restart()
        settings = self.models()['settings']
        self.assertEqual(settings['codex'], {'default': {'model': 'gpt-6-astra', 'effort': 'xhigh'},
                                             'allowed': {'gpt-6-astra': ['high', 'xhigh', 'max']}})
        self.assertEqual(settings['claude'], {'default': {'model': 'configured-model', 'effort': 'configured-effort'}})
        self.assertEqual((settings['revision'], settings['updated_by']['kind']), (1, 'migration'))
        self.assertEqual(self.codex_job()['contract']['harness_params'],
                         {'model': 'gpt-6-astra', 'effort': 'xhigh', 'sandbox': 'workspace-write'})
        self.assertEqual(self.codex_job(status=400, effort='low')['error']['details'], {'reason': 'model_not_allowed'})
        self.put(expected_revision=1, codex={'default': {'model': A, 'effort': 'low'}, 'allowed': None})  # 迁移值可编辑
        self.restart()
        self.assertEqual(self.models()['settings']['codex']['default'], {'model': A, 'effort': 'low'})
        # profile 的取值不符合设置字段约束时不迁入，协调回合仍直接用 profile。
        odd = d.migrated_settings({'model': 'Model With Spaces', 'effort': 'xhigh'})
        self.assertEqual(odd['claude'], EMPTY_CLAUDE)

    def test_default_change_only_affects_new_jobs_including_queued_ones(self):
        self.put(expected_revision=0, codex={'default': {'model': A, 'effort': 'high'}, 'allowed': None})
        queued = self.codex_job()
        override = self.codex_job(effort='max')
        self.put(expected_revision=1, codex={'default': {'model': B, 'effort': 'low'}, 'allowed': {B: ['low']}})
        fresh = self.codex_job()
        default, task = 'workspace_default', 'task_override'
        for job, model, effort, source, revision in ((queued, A, 'high', {'model': default, 'effort': default}, 1),
                                                     (override, A, 'max', {'model': default, 'effort': task}, 1),
                                                     (fresh, B, 'low', {'model': default, 'effort': default}, 2)):
            current = self.current(job)
            self.assertEqual(current['contract']['harness_params'], {'model': model, 'effort': effort, 'sandbox': 'workspace-write'})
            self.assertEqual((current['model_policy']['source'], current['model_policy']['settings_revision']), (source, revision))
            self.assertEqual((current['request_hash'], current['contract']), (job['request_hash'], job['contract']))
        self.assertEqual(self.codex_job(status=400, model=A, effort='high')['error']['details'], {'reason': 'model_not_allowed'})
        worker = self.node('w', capabilities=caps({A: ['high', 'max'], B: ['low']}))
        assignment = worker.claim()  # 排队期间设置已变；派单用的仍是受理时冻结的值
        self.assertEqual((assignment['job_id'], assignment['contract']['harness_params']['model']), (queued['job_id'], A))
        runtime = self.current(queued)['attempts'][0]['runtime']
        self.assertEqual((runtime['model'], runtime['effort'], runtime['observed']), (A, 'high', None))

    def test_coordination_round_takes_the_claude_setting_without_touching_codex(self):
        self.app.config.update(copy.deepcopy(tc.CONFIG))
        claude = {'default': {'model': 'coordinator-model', 'effort': 'max'}}
        self.put(expected_revision=0, claude=claude)
        self.completed()
        coordinator = self.coordinators()[0]
        self.assertEqual(coordinator['contract']['harness_params'], claude['default'])
        self.assertEqual(coordinator['model_policy']['source'], {'model': 'workspace_default', 'effort': 'workspace_default'})
        self.assertEqual(coordinator['created_by']['kind'], 'controller')
        profile = self.call('GET', '/overview')['managed_coordinator']['profile']
        self.assertEqual((profile['model'], profile['effort'], profile['node_ids']), ('coordinator-model', 'max', ['mac']))
        files = {f['path']: json.loads(self.app.blobs.read(f['sha256'])) for f in coordinator['contract']['inputs_manifest']['files']}
        self.assertEqual(files['context.json']['repair_policy'], {'model': None, 'effort': None, 'allowed_efforts': None})
        self.assertEqual(self.models()['settings']['codex'], d.EMPTY_SETTINGS['codex'])


class RepairInheritanceTests(ClientCase):
    """任务书指定的回归：根任务 A/E 受理后把默认值改成 B/F，再触发修复。"""

    def rework(self, effort):
        self.app.config.update(copy.deepcopy(tc.CONFIG))
        self.put(expected_revision=0, codex={'default': {'model': A, 'effort': 'high'}, 'allowed': {A: ['high', 'max'], B: ['low']}})
        worker = self.node('worker', capabilities=caps({A: ['high', 'max', 'low'], B: ['low']}))
        root = self.codex_job()
        attempt = worker.claim()
        checkpoint = worker.checkpoint(attempt, kind='final')
        worker.complete(attempt, checkpoint=checkpoint, report={})
        self.put(expected_revision=1, codex={'default': {'model': B, 'effort': 'low'}, 'allowed': {B: ['low']}})
        mac = self.node('mac', capabilities=tc.CAPS)
        self.finish_decision(mac, mac.claim(), tc.decision(checkpoint, 'rework', tc.repair_request(effort=effort)))
        jobs = self.call('GET', '/jobs')['jobs']
        self.assertFalse([j for j in jobs if j['contract']['harness_params'].get('model') == B])  # 模型从不自动变成 B
        return self.current(root), [j for j in jobs if j['work_package_id'] == root['work_package_id'] and j['job_id'] != root['job_id']]

    def test_repair_without_override_keeps_the_frozen_model_and_effort(self):
        root, repairs = self.rework(None)
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]['contract']['harness_params'], {'model': A, 'effort': 'high', 'sandbox': 'workspace-write'})
        self.assertEqual(repairs[0]['model_policy'], {'schema': 1, 'settings_revision': 1, 'requested': {'model': None, 'effort': None},
                                                     'resolved': {'model': A, 'effort': 'high'},
                                                     'source': {'model': 'root_frozen', 'effort': 'root_frozen'}})
        # 累计预算属于根工作包：修复计入 repairs，已用的 attempts 不清零，上限不超过根任务。
        self.assertEqual((root['counters'], repairs[0]['counters']), ({'attempts': 1, 'infra_retries': 0, 'migrations': 0, 'repairs': 1},) * 2)
        self.assertEqual(repairs[0]['contract']['budgets'], root['contract']['budgets'])
        frozen = self.app.get('root', root['work_package_id'])['model_policy']
        self.assertEqual(frozen, {'schema': 1, 'settings_revision': 1,
                                  'codex': {'model': A, 'effort': 'high', 'allowed': {A: ['high', 'max'], B: ['low']}}})

    def test_legal_effort_override_changes_only_the_effort_and_records_its_source(self):
        root, repairs = self.rework('max')
        self.assertEqual(repairs[0]['contract']['harness_params'], {'model': A, 'effort': 'max', 'sandbox': 'workspace-write'})
        self.assertEqual(repairs[0]['model_policy']['requested'], {'model': None, 'effort': 'max'})
        self.assertEqual(repairs[0]['model_policy']['source'], {'model': 'root_frozen', 'effort': 'coordinator_override'})

    def test_illegal_effort_override_is_rejected_and_the_assessment_is_kept(self):
        root, repairs = self.rework('low')  # 当前设置与节点都允许 low，但不在根冻结的许可范围内
        self.assertEqual(repairs, [])
        rejected = [e for e in root['events'] if e['type'] == 'coordinator.follow_up_rejected']
        self.assertEqual([e['payload']['reason'] for e in rejected], ['effort_not_allowed'])
        self.assertEqual((root['acceptance']['status'], root['counters']['repairs']), ('rework', 0))

    def test_later_revision_in_the_same_root_resolves_against_the_frozen_policy(self):
        root, repairs = self.rework(None)
        worker_done = self.call('POST', '/jobs/' + repairs[0]['job_id'] + '/cancel',
                                {'request_id': uid(), 'expected_version': repairs[0]['version']})
        self.assertEqual(worker_done['status'], 'cancelled')
        revision = {'request_id': uid(), 'work_package_id': root['work_package_id'],
                    'contract': contract(harness='codex', harness_params={'effort': 'max'})}
        job = self.call('POST', '/jobs', revision, status=201)
        self.assertEqual(job['contract']['harness_params'], {'model': A, 'effort': 'max', 'sandbox': 'workspace-write'})
        self.assertEqual(job['model_policy']['source'], {'model': 'root_frozen', 'effort': 'task_override'})
        outside = dict(revision, request_id=uid(), contract=contract(harness='codex', harness_params={'model': B, 'effort': 'high'}))
        self.call('POST', '/jobs/' + job['job_id'] + '/cancel', {'request_id': uid(), 'expected_version': job['version']})
        self.assertEqual(self.call('POST', '/jobs', outside, status=400)['error']['details'], {'reason': 'model_not_allowed'})
        # 另一模型即使也在冻结的许可范围内（B/low），修订也不能借此换模型。
        switch = dict(revision, request_id=uid(), contract=contract(harness='codex', harness_params={'model': B, 'effort': 'low'}))
        self.assertEqual(self.call('POST', '/jobs', switch, status=400)['error']['details'], {'reason': 'model_not_allowed'})

    def test_repair_is_refused_when_the_root_froze_no_model(self):
        job, mac, attempt, checkpoint = self.round()  # 根是不调用模型的任务，受理时没有默认模型
        self.put(expected_revision=0, codex={'default': {'model': B, 'effort': 'low'}, 'allowed': None})
        self.finish_decision(mac, attempt, tc.decision(checkpoint, 'rework', tc.repair_request(effort=None)))
        current = self.current(job)
        self.assertEqual([e['payload']['reason'] for e in current['events'] if e['type'] == 'coordinator.follow_up_rejected'],
                         ['model_default_unset'])
        self.assertEqual(len(self.call('GET', '/jobs')['jobs']), 2)


class LegacyRootTests(Rounds, ControllerCase):
    """升级前创建的根没有冻结策略：按原来写死的行为推导，不受新设置影响。"""

    def rework(self, effort):
        job, mac, attempt, checkpoint = self.round()
        with self.app.store.transaction():
            root = self.app.store.get('root', job['work_package_id'])
            del root['model_policy']
            self.app.store.put('root', job['work_package_id'], root)
        self.call('PUT', '/settings/models', {'request_id': uid(), 'expected_revision': 0, 'codex': {'default': {'model': B, 'effort': 'low'}, 'allowed': None}})
        self.finish_decision(mac, attempt, tc.decision(checkpoint, 'rework', tc.repair_request(effort=effort)))
        return self.current(job), [j for j in self.call('GET', '/jobs')['jobs']
                                   if j['work_package_id'] == job['work_package_id'] and j['job_id'] != job['job_id']]

    def test_previous_fixed_model_and_effort_range_still_apply(self):
        root, repairs = self.rework('max')
        self.assertEqual(repairs[0]['contract']['harness_params'], {'model': 'gpt-6-astra', 'effort': 'max', 'sandbox': 'workspace-write'})
        self.assertEqual(repairs[0]['model_policy']['source'], {'model': 'legacy_compat', 'effort': 'coordinator_override'})
        self.assertIsNone(repairs[0]['model_policy']['settings_revision'])

    def test_effort_outside_the_previous_range_is_rejected(self):
        root, repairs = self.rework('low')
        self.assertEqual(repairs, [])
        self.assertEqual([e['payload']['reason'] for e in root['events'] if e['type'] == 'coordinator.follow_up_rejected'],
                         ['effort_not_allowed'])


class ModelCatalogTests(ClientCase):
    def reasons(self, job):
        return {row['node_id']: (row['reasons'], row.get('model_catalog')) for row in
                self.call('GET', '/jobs/' + job['job_id'] + '/explain')['nodes']}

    def test_scheduling_uses_reported_catalogs_and_never_guesses_for_old_nodes(self):
        self.put(expected_revision=0, codex={'default': {'model': A, 'effort': 'high'}, 'allowed': None})
        self.node('old')  # 旧节点不上报目录：只认它的适配器实际接受的固定组合
        job = self.codex_job()
        self.assertEqual(self.reasons(job), {'old': (['model_unsupported:' + A], 'legacy')})
        current = self.current(job)
        self.assertEqual(current['status'], 'waiting_capability')
        self.assertIn('model_unsupported:' + A, current['status_reason'])
        self.node('partial', capabilities=caps({A: ['low']}))
        self.assertEqual(self.reasons(job)['partial'], (['effort_unsupported:%s/high' % A], 'ok'))
        legacy = self.codex_job(model='gpt-6-astra', effort='xhigh')
        self.assertEqual(self.reasons(legacy)['old'], ([], 'legacy'))
        self.assertEqual(self.reasons(legacy)['partial'], (['model_unsupported:gpt-6-astra'], 'ok'))
        unknown = self.node('unknown', capabilities=caps({}, status='unknown'))  # 目录未知：不排除，也不声称支持
        self.assertEqual(self.reasons(job)['unknown'], ([], 'unknown'))
        self.assertEqual(unknown.claim()['job_id'], job['job_id'])
        self.assertIsNone(self.reasons(self.job())['old'][1])  # 不调用模型的任务不看目录

    def test_selectable_is_the_intersection_of_policy_and_catalogs(self):
        self.node('old')
        self.node('new', capabilities=caps({A: ['low', 'high'], B: ['low']}))
        self.node('also', capabilities=caps({A: ['high', 'max']}))
        self.node('unknown', capabilities=caps({}, status='unknown'))
        self.node('nocodex', capabilities=dict(caps(), harness={'stub': '1', 'codex': None}))
        view = self.models()
        self.assertEqual(view['selectable'], {'unknown_nodes': ['unknown'], 'codex': [
            {'model': 'gpt-6-astra', 'efforts': ['high', 'xhigh', 'max'], 'nodes': ['old']},
            {'model': A, 'efforts': ['low', 'high', 'max'], 'nodes': ['new', 'also']},
            {'model': B, 'efforts': ['low'], 'nodes': ['new']}]})
        nodes = view['catalog']['nodes']
        self.assertEqual({k: v['codex']['status'] for k, v in nodes.items()},
                         {'old': 'legacy', 'new': 'ok', 'also': 'ok', 'unknown': 'unknown', 'nocodex': 'legacy'})
        self.assertEqual((nodes['new']['claude']['status'], nodes['new']['claude']['reason']), ('unknown', 'no_catalog_interface'))
        self.put(expected_revision=0, codex={'default': {'model': None, 'effort': None}, 'allowed': {A: ['high', 'ultra'], 'other': ['low']}})
        self.assertEqual(self.models()['selectable']['codex'], [{'model': A, 'efforts': ['high'], 'nodes': ['new', 'also']}])

    def test_registration_declares_features_and_rejects_malformed_catalogs(self):
        node = self.node('n', capabilities=caps({A: ['low']}))
        ok = self.call('POST', '/nodes/register', node.record, token=node.token)
        self.assertEqual(ok['features'], list(p.FEATURES))
        entry = node.record['capabilities']['models']['codex']
        for bad in ({'other': entry}, {'codex': dict(entry, status='legacy')}, {'codex': dict(entry, models=[{'id': A}])},
                    {'codex': dict(entry, models=[{'id': 'bad id', 'efforts': [], 'default_effort': None}])},
                    {'codex': dict(entry, models=[{'id': A, 'efforts': ['Bad'], 'default_effort': None}])},
                    {'codex': {'status': 'ok', 'models': []}}, []):
            with self.subTest(bad=bad):
                record = dict(node.record, capabilities=dict(node.record['capabilities'], models=bad))
                self.call('POST', '/nodes/register', record, token=node.token, status=400)


class DraftTests(ClientCase):
    def test_templates_baselines_and_draft_form_an_explicit_contract(self):
        self.put(expected_revision=0, codex={'default': {'model': A, 'effort': 'high'}, 'allowed': None})
        token = self.pair('operator')['client_token']
        self.assertEqual([t['id'] for t in self.call('GET', '/templates', token=token)['templates']],
                         ['codex-change', 'codex-change-accept', 'diagnostic'])
        content = b'print(1)\n'
        inputs = manifest(content, 'app.py')
        body = {'request_id': uid(), 'name': 'demo project', 'manifest': inputs}
        baseline = self.call('POST', '/baselines', body, token=token, status=201)
        self.assertEqual((baseline['files'], baseline['bytes'], baseline['missing_blobs']), (1, len(content), [p.sha256_bytes(content)]))
        self.assertEqual(self.call('POST', '/baselines', body, token=token, status=201)['baseline_id'], baseline['baseline_id'])
        request = {'template': 'codex-change-accept', 'goal': '修复 app.py 的输出\n细节见验收项', 'acceptance': ['输出 2'],
                   'excludes': ['不改测试'], 'baseline_id': baseline['baseline_id'], 'effort': 'max',
                   'checks': [{'name': 'unit', 'run': ['{python}', 'app.py']}]}
        draft = self.call('POST', '/contracts/draft', request, token=token)
        self.assertEqual([w['code'] for w in draft['warnings']], ['missing_blobs', 'no_node_supports_model'])
        self.call('PUT', '/blobs/' + p.sha256_bytes(content), content, token=token, status=201)
        self.node('w', capabilities=caps({A: ['high', 'max'], B: ['low']}))
        draft = self.call('POST', '/contracts/draft', request, token=token)
        self.assertEqual((draft['warnings'], draft['title'], draft['settings_revision']), ([], '修复 app.py 的输出', 1))
        self.assertEqual(draft['contract']['harness_params'], {'effort': 'max', 'sandbox': 'workspace-write'})  # 只带显式选择
        preview = draft['preview']
        self.assertEqual(preview['harness_params'], {'model': A, 'effort': 'max', 'sandbox': 'workspace-write'})
        self.assertEqual((preview['publish'], preview['coordination']), ({'mode': 'manual', 'requires_acceptance': True}, {'policy': 'always'}))
        self.assertEqual((preview['inputs_manifest'], preview['requirements']['tools']), (p.validate_manifest(inputs), ['codex']))
        for text in ('修复 app.py 的输出', '- 输出 2', '- 不改测试', '- unit: {python} app.py'):
            self.assertIn(text, preview['prompt'])
        self.assertEqual(draft['model_policy']['source'], {'model': 'workspace_default', 'effort': 'task_override'})
        self.assertEqual(self.call('GET', '/jobs')['jobs'], [])  # 草拟不改变状态
        self.put(expected_revision=1, codex={'default': {'model': B, 'effort': 'low'}, 'allowed': None})
        submit = {'request_id': uid(), 'title': draft['title'], 'contract': draft['contract'],
                  'expected_settings_revision': draft['settings_revision']}
        stale = self.call('POST', '/jobs', submit, token=token, status=409)['error']
        self.assertEqual((stale['code'], stale['details']['current']['revision']), ('version_mismatch', 2))
        draft = self.call('POST', '/contracts/draft', dict(request, effort='low'), token=token)
        job = self.call('POST', '/jobs', dict(submit, request_id=uid(), contract=draft['contract'],
                                              expected_settings_revision=draft['settings_revision']), token=token, status=201)
        self.assertEqual((job['contract'], job['protection']), (draft['preview'], 'protected'))
        self.assertEqual(job['contract']['harness_params']['model'], B)
        change = self.call('POST', '/contracts/draft', {'template': 'codex-change', 'goal': 'x', 'model': A, 'effort': 'high'}, token=token)
        self.assertEqual((change['preview']['publish'], change['preview']['coordination'], [w['code'] for w in change['warnings']]),
                         ({'mode': 'auto', 'requires_acceptance': False}, {'policy': 'on_failure'}, ['empty_inputs']))
        check = self.call('POST', '/contracts/draft', {'template': 'diagnostic', 'goal': '自检'}, token=token)
        self.assertEqual((check['preview']['harness'], check['model_policy'], check['preview']['coordination']), ('stub', None, {'policy': 'none'}))
        self.assertEqual(check['preview']['harness_params']['steps'], [{'write': {'path': 'agent-collab-diagnostic.txt', 'content': '自检\n'}}])
        for bad in ({'template': 'unknown', 'goal': 'x'}, {'template': 'diagnostic', 'goal': ' '}, {'template': 'diagnostic', 'goal': 'x', 'model': A},
                    {'template': 'codex-change', 'goal': 'x', 'baseline_id': uid()}, {'template': 'codex-change', 'goal': 'x', 'surprise': 1},
                    {'template': 'codex-change', 'goal': 'x', 'baseline_id': baseline['baseline_id'], 'inputs_manifest': inputs},
                    {'template': 'codex-change', 'goal': 'x', 'checks': [{'name': 'bad name', 'run': ['x']}]}):
            with self.subTest(bad=bad):
                self.assertIn(self.app.handle('POST', p.API_PREFIX + '/contracts/draft', {'authorization': 'Bearer ' + token},
                                              p.canonical_json(bad)).status, (400, 404))
        self.assertEqual([b['missing_blobs'] for b in self.call('GET', '/baselines', token=token)['baselines']], [[]])
        for bad in ({'name': '', 'manifest': inputs}, {'name': 'n', 'manifest': dict(inputs, base='0' * 64)}):
            self.call('POST', '/baselines', dict(bad, request_id=uid()), token=token, status=400)

    def test_baseline_survives_backup_and_job_event_pages_and_checkpoint_files_are_readable(self):
        from agent_collab.controller import backup
        content = b'payload'
        node = self.node('w')
        node.upload(content)
        self.call('POST', '/baselines', {'request_id': uid(), 'name': 'kept', 'manifest': manifest(content, 'kept.txt')}, status=201)
        job = self.job()
        attempt = node.claim()
        for done in range(5):
            node.event(attempt, 'attempt.progress', {'done': done, 'total': 5})
        checkpoint = node.checkpoint(attempt, manifest(content, 'out.txt'), kind='final')
        node.complete(attempt, checkpoint=checkpoint)
        events = self.current(job)['events']
        page = self.call('GET', '/jobs/%s/events?before=%d&limit=2' % (job['job_id'], events[3]['seq']))['events']
        self.assertEqual([e['seq'] for e in page], [events[1]['seq'], events[2]['seq']])
        self.assertEqual(self.call('GET', '/jobs/%s/events?limit=1000' % job['job_id'])['events'], events)
        viewer = self.pair('viewer')['client_token']
        files = self.call('GET', '/checkpoints/' + checkpoint['checkpoint_id'] + '/files', token=viewer)
        self.assertEqual((files['manifest_sha256'], files['files']), (checkpoint['manifest_sha256'], manifest(content, 'out.txt')['files']))
        blob = self.app.handle('GET', p.API_PREFIX + '/blobs/' + files['files'][0]['sha256'], {'authorization': 'Bearer ' + viewer}, b'')
        self.assertEqual((blob.status, blob.body), (200, content))
        saved = backup(self.temp / 'data', self.temp / 'backup', clock=self.clock)
        self.assertIn(p.sha256_bytes(content), {b['sha256'] for b in saved['blobs']})


class Tick:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class AgentCatalogTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.dev/tmp').mkdir(parents=True, exist_ok=True)
        self.temp = Path(tempfile.mkdtemp(dir=str(ROOT / '.dev/tmp')))
        self.addCleanup(shutil.rmtree, str(self.temp))
        self.fake, self.clock, self.calls = FakeController(), Tick(), []
        data = self.temp / 'agent'
        pair(argparse.Namespace(controller='https://controller.invalid', code='one-time', node_id='node-a', data_dir=str(data),
                                ca_file=None, slots=1, host_slots=1, physical_host_id='host-a', failure_domain='domain-a', label=[]), self.fake)
        capabilities = {'os': 'linux', 'arch': 'x86_64', 'tools': {}, 'gpu': [], 'harness': {'stub': '1', 'codex': '9.9'}, 'labels': []}

        def probe(argv, env):
            self.calls.append(argv)
            return {'status': 'ok', 'source': 'test', 'fetched_at': None, 'cli_version': None, 'reason': None,
                    'models': [{'id': 'm-one', 'efforts': ['low'], 'default_effort': None}]}
        self.agent = Agent(data, transport=self.fake, clock=self.clock, capabilities=capabilities, auth_runner=no_auth, catalog_probe=probe)
        self.addCleanup(self.agent.close)

    def cycle(self, until=lambda: True):
        for _ in range(200):
            self.clock.now += 1
            self.agent.cycle()
            if until():
                return
            time.sleep(.01)
        self.fail('condition not reached')

    def test_models_are_probed_and_reported_only_after_the_controller_declares_support(self):
        self.cycle()
        self.cycle()
        self.assertNotIn('models', self.fake.registrations[-1]['capabilities'])  # 旧 controller：不探测、不上报
        self.assertEqual(self.calls, [])
        self.fake.features = list(p.FEATURES)
        self.agent.registered = False
        self.cycle(lambda: 'models' in self.fake.registrations[-1]['capabilities'])
        models = self.fake.registrations[-1]['capabilities']['models']
        self.assertEqual((models['codex']['status'], models['codex']['models'], models['codex']['cli_version']),
                         ('ok', [{'id': 'm-one', 'efforts': ['low'], 'default_effort': None}], '9.9'))
        self.assertEqual((models['claude']['status'], models['claude']['reason']), ('unknown', 'no_catalog_interface'))
        d.validate_catalogs(models)
        count = len(self.fake.registrations)
        self.cycle()
        self.cycle()
        self.assertEqual((len(self.calls), len(self.fake.registrations)), (1, count))  # 周期内不重复探测，目录没变不重新注册

    def test_controller_upgraded_during_an_outage_is_renegotiated_on_reconnect(self):
        self.cycle()
        self.cycle()
        self.assertNotIn('models', self.fake.registrations[-1]['capabilities'])
        self.fake.offline = True  # 旧 controller 停机，原地升级；恢复代次不变
        self.cycle()
        self.fake.offline, self.fake.features = False, list(p.FEATURES)
        self.cycle(lambda: 'models' in self.fake.registrations[-1]['capabilities'])
        self.assertEqual(len(self.calls), 1)

    def test_catalog_refreshed_while_registering_is_sent_on_the_next_registration(self):
        self.fake.features = list(p.FEATURES)
        self.cycle(lambda: 'models' in self.fake.registrations[-1]['capabilities'])
        newer = {'status': 'ok', 'source': 'test', 'fetched_at': None, 'cli_version': None, 'reason': None,
                 'models': [{'id': 'm-two', 'efforts': ['high'], 'default_effort': None}]}

        def refreshed_meanwhile():
            # 后台探测恰在注册请求途中完成：它置的 registered = False 会被注册结束时覆盖。
            self.fake.on_register = None
            self.agent.models = dict(self.agent.models, codex=newer)
            self.agent.catalog_revision = getattr(self.agent, 'catalog_revision', 0) + 1  # 后台线程在目录变化时做的事
            self.agent.registered = False
        self.fake.on_register = refreshed_meanwhile
        self.agent.registered = False
        self.cycle(lambda: self.fake.registrations[-1]['capabilities']['models']['codex']['models'] == newer['models'])
