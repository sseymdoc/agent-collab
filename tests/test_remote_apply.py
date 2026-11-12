"""远端项目写回（protocol §13.10）：真实 controller（进程内）+ 真实节点代理，确定性任务，不调用模型。

执行节点用公开节点 API 的夹具完成并发布任务；目标节点是真实的 Agent，目录绑定只在它本机。
"""
import argparse
import contextlib
import io
import json
from pathlib import Path
import threading
import time
import unittest
from unittest import mock
from urllib.parse import urlsplit

from agent_collab import cli, node_agent, node_projects, project, project_cli, protocol as p
from agent_collab.node_agent import Agent, pair
from fake_controller import no_auth
from helpers import ControllerCase, contract, uid

EXEC_CAPS = {'os': 'linux', 'arch': 'x86_64', 'tools': {}, 'gpu': [], 'harness': {'stub': '1'}, 'labels': ['exec']}
AGENT_CAPS = dict(os='linux', arch='x86_64', tools={}, gpu=[], harness={'stub': '1', 'codex': None}, labels=[])
INPUTS = {'src/app.txt': b'version 1\n', 'README.md': b'old readme\n', 'docs/guide.txt': b'guide\n'}
OUTPUTS = {'src/app.txt': b'version 2\n', 'docs/guide.txt': b'guide\n', 'out/result.txt': b'produced\n'}  # 改一个、加一个、删一个


def manifest_of(files):
    return {'version': 1, 'base': None, 'deleted': [], 'files': [
        {'path': k, 'sha256': p.sha256_bytes(v), 'size': len(v), 'executable': False} for k, v in sorted(files.items())]}


def tree(root):
    root = Path(root)
    return {x.relative_to(root).as_posix(): x.read_bytes() for x in sorted(root.rglob('*')) if x.is_file()}


class RemoteCase(ControllerCase):
    legacy_settings = False

    def setUp(self):
        super().setUp()
        self.directory = self.temp / 'agent'
        self.drop = None  # 返回 True 的谓词：模拟这条请求的网络中断
        pair(argparse.Namespace(controller='https://controller.invalid', code=self.app.pairing_code('target'), node_id='target',
                                data_dir=str(self.directory), ca_file=None, slots=1, host_slots=1, physical_host_id='host',
                                failure_domain='domain', label=[]), self.transport)
        self.agent = self.start()
        self.executor = self.node('exec', capabilities=EXEC_CAPS)
        self.folder = self.temp / 'my-project'
        self.write(INPUTS)
        self.project = self.call('POST', '/projects', {'request_id': uid(), 'name': 'demo'}, status=201)
        self.pid = self.project['project_id']

    def tearDown(self):
        thread = self.agent.projects.thread
        if thread:
            thread.join(10)
        self.agent.close()
        super().tearDown()

    def start(self):
        return Agent(self.directory, transport=self.transport, clock=self.clock, capabilities=dict(AGENT_CAPS), auth_runner=no_auth,
                     catalog_probe=lambda argv, env: {'status': 'unknown', 'source': 'test', 'fetched_at': None, 'cli_version': None,
                                                      'reason': 'command_missing', 'models': [], 'provider': None})

    def restart(self):
        thread = self.agent.projects.thread
        if thread:
            thread.join(10)
        self.agent.close()
        self.agent = self.start()

    def transport(self, method, url, headers, body):
        path = urlsplit(url).path
        if self.drop and self.drop(method, path, body):
            raise OSError('injected network failure')
        result = self.app.handle(method, path, headers, body)
        return result.status, result.headers, result.body

    def drive(self, condition, limit=600, step=1.5):
        for _ in range(limit):
            self.agent.cycle()
            self.clock.advance(step)
            if condition():
                return
            time.sleep(.01)
        self.fail('condition not reached')

    def write(self, files, root=None):
        for name, data in files.items():
            path = (root or self.folder) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def bind(self):
        node_projects.bind(self.directory, self.pid, self.folder)
        self.drive(lambda: self.targets())

    def targets(self):
        return self.call('GET', '/projects/' + self.pid)['remote_targets']

    def published(self, outputs=OUTPUTS, inputs=INPUTS):
        """执行节点完成并发布一个属于项目的任务：输入是 inputs，成果是 outputs。"""
        for data in list(inputs.values()) + list(outputs.values()):
            self.call('PUT', '/blobs/' + p.sha256_bytes(data), data, status=None)
        self.executor.heartbeat(assignments=[])
        job = self.job(contract(inputs_manifest=manifest_of(inputs), requirements={'labels': ['exec']},
                                coordination={'policy': 'none'}), project_id=self.pid)
        a = self.executor.claim()
        self.assertEqual(a['job_id'], job['job_id'])
        cp = self.executor.checkpoint(a, manifest_of(outputs), kind='final')
        self.executor.complete(a, checkpoint=cp)
        self.executor.publish(a, cp)
        return self.current(job)

    def call(self, method, path, data=None, token=None, status=200, headers=None):
        if status is None:  # PUT /blobs：新建 201、已有 200
            result = self.app.handle(method, p.API_PREFIX + path, {'authorization': 'Bearer ' + self.admin}, data)
            self.assertIn(result.status, (200, 201))
            return None
        return super().call(method, path, data, token, status, headers)

    def op(self, op_id):
        return self.call('GET', '/operations/' + op_id)

    def plan(self, job, node='target'):
        op = self.call('POST', '/projects/%s/plans' % self.pid, {'request_id': uid(), 'job_id': job['job_id'], 'node_id': node}, status=201)
        self.drive(lambda: self.op(op['op_id'])['status'] in ('succeeded', 'failed', 'timed_out'))
        return self.op(op['op_id'])

    def apply(self, job, status=201, **body):
        data = {'request_id': uid(), 'application_id': uid(), 'job_id': job['job_id'], 'checkpoint_id': job['delivery_checkpoint_id'],
                'node_id': 'target', 'expected_base_manifest_sha256': job['inputs_manifest_sha256']}
        data.update(body)
        return self.call('POST', '/projects/%s/applications' % self.pid, data, status=status)

    def application(self, record):
        return self.call('GET', '/projects/%s/applications/%s' % (self.pid, record['application_id']))

    def settled(self, record):
        self.drive(lambda: self.application(record)['status'] != 'started')
        return self.application(record)

    def assertNoPaths(self, value):
        text = json.dumps(value, ensure_ascii=False)
        for path in (self.temp, self.temp.resolve()):
            for form in (str(path), path.as_posix(), json.dumps(str(path))[1:-1]):
                self.assertNotIn(form, text)


class RemoteApplyTests(RemoteCase):
    def test_bound_directory_is_declared_planned_and_applied_cleanly(self):
        # 绑定只在节点本机；controller 看到的是项目 ID 与目录名，最迟一个心跳周期内出现，不需要重启。
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(node_agent.main(['project-bind', '--data-dir', str(self.directory), self.pid, str(self.folder)]), 0)
        self.assertEqual(json.loads(out.getvalue())['bindings'][0]['root_name'], 'my-project')
        self.drive(lambda: self.targets())
        self.assertEqual(self.targets(), [{'node_id': 'target', 'root_name': 'my-project', 'status': 'online'}])
        node = next(n for n in self.call('GET', '/nodes')['nodes'] if n['node_id'] == 'target')
        self.assertEqual(node['capabilities']['project_bindings'], [{'project_id': self.pid, 'root_name': 'my-project'}])

        job = self.published()
        plan = self.plan(job)
        self.assertEqual(plan['status'], 'succeeded', plan)
        # 参数全部由 controller 按任务记录填写。
        cp = self.call('GET', '/checkpoints/' + job['delivery_checkpoint_id'])
        self.assertEqual((plan['kind'], plan['params']['checkpoint_id'], plan['params']['manifest_sha256'], plan['params']['base_manifest_sha256']),
                         ('project_plan', cp['checkpoint_id'], cp['manifest_sha256'], job['inputs_manifest_sha256']))
        self.assertEqual([(c['path'], c['action'], c['state']) for c in plan['result']['changes']],
                         [('README.md', 'delete', 'clean'), ('out/result.txt', 'add', 'clean'), ('src/app.txt', 'modify', 'clean')])
        self.assertEqual((plan['result']['root_name'], 'root' in plan['result']), ('my-project', False))
        self.assertEqual(tree(self.folder), INPUTS)  # 预览不写入

        body = {'request_id': uid(), 'application_id': uid(), 'job_id': job['job_id'], 'checkpoint_id': plan['params']['checkpoint_id'],
                'node_id': 'target', 'expected_base_manifest_sha256': plan['params']['base_manifest_sha256'], 'plan': plan['result']['summary']}
        started = self.call('POST', '/projects/%s/applications' % self.pid, body, status=201)
        self.assertEqual((started['status'], started['target_node_id'], started['device']), ('started', 'target', 'target'))
        self.assertEqual(self.call('POST', '/projects/%s/applications' % self.pid, body, status=201), started)  # 重放：同一条记录，同一项操作
        done = self.settled(started)
        self.assertEqual((done['status'], sorted(done['detail']['changed'])), ('applied', ['README.md', 'out/result.txt', 'src/app.txt']))
        self.assertEqual(tree(self.folder), OUTPUTS)
        view = self.current(job)
        self.assertEqual((view['integration_status'], [a['status'] for a in view['applications']]), ('integrated', ['applied']))
        self.assertEqual(self.call('GET', '/projects/' + self.pid)['applied']['application_id'], started['application_id'])
        self.assertEqual(self.op(started['operation_id'])['status'], 'succeeded')
        ops = [o for o in self.call('GET', '/nodes/target/operations')['operations'] if o['kind'] == 'project_apply']
        self.assertEqual(len(ops), 1)  # 重放没有产生第二项写回
        # 目标节点不能上报任何绝对路径：应用记录、操作结果、事件里都没有本机路径（备份目录也不上报）。
        self.assertNoPaths([view, self.op(plan['op_id']), self.op(started['operation_id']), self.call('GET', '/events?limit=1000')])

        # 解除绑定同样不需要重启服务。
        node_projects.unbind(self.directory, self.pid)
        self.drive(lambda: not self.targets())
        self.assertEqual(self.call('POST', '/projects/%s/plans' % self.pid, {'request_id': uid(), 'job_id': job['job_id'], 'node_id': 'target'},
                                   status=409)['error']['code'], 'not_bound')

    def test_conflicts_write_nothing_by_default_and_skip_conflicts_is_partial(self):
        self.bind()
        job = self.published()
        self.write({'src/app.txt': b'my own edit\n'})  # 任务期间节点本机改了任务也改的文件
        before = tree(self.folder)
        refused = self.settled(self.apply(job))
        self.assertEqual((refused['status'], refused['detail']['changed'], refused['detail']['conflicts']),
                         ('conflict', [], [{'path': 'src/app.txt', 'reason': 'modified_locally'}]))
        self.assertEqual(tree(self.folder), before)
        self.assertEqual(self.current(job)['integration_status'], 'conflict')

        partial = self.settled(self.apply(job, skip_conflicts=True))
        self.assertEqual((partial['status'], sorted(partial['detail']['changed'])), ('partial', ['README.md', 'out/result.txt']))
        self.assertEqual(tree(self.folder), dict(OUTPUTS, **{'src/app.txt': b'my own edit\n'}))
        view = self.current(job)
        self.assertEqual((view['integration_status'], self.call('GET', '/projects/' + self.pid)['applied']), ('conflict', None))
        self.assertNoPaths(view)

    def test_controller_takes_no_client_paths_and_rejects_stale_expectations(self):
        self.bind()
        job = self.published()
        # 通用操作端点不接受远端写回的种类：参数只能由 controller 填。
        for kind in p.PROJECT_OPERATIONS:
            self.call('POST', '/nodes/target/operations', {'request_id': uid(), 'kind': kind, 'params': {'target_path': str(self.temp)}}, status=400)
        self.call('POST', '/projects/%s/plans' % self.pid, {'request_id': uid(), 'job_id': job['job_id'], 'node_id': 'target',
                                                          'root': str(self.temp)}, status=400)
        for extra in ({'target_path': str(self.temp)}, {'device': 'laptop'}, {'manifest_sha256': '0' * 64}):
            self.apply(job, status=400, **extra)
        stale = self.apply(job, status=409, expected_base_manifest_sha256='0' * 64)['error']
        self.assertEqual(stale['details']['current'], {'checkpoint_id': job['delivery_checkpoint_id'], 'base_manifest_sha256': job['inputs_manifest_sha256']})
        self.apply(job, status=409, checkpoint_id=uid())
        missing = {'request_id': uid(), 'application_id': uid(), 'job_id': job['job_id'], 'checkpoint_id': job['delivery_checkpoint_id'], 'node_id': 'target'}
        self.call('POST', '/projects/%s/applications' % self.pid, missing, status=400)
        self.assertEqual(self.call('GET', '/projects/%s/applications' % self.pid)['applications'], [])

        loose = self.job(contract(requirements={'labels': ['exec']}))  # 不属于项目
        self.call('POST', '/projects/%s/plans' % self.pid, {'request_id': uid(), 'job_id': loose['job_id'], 'node_id': 'target'}, status=409)
        # 远端写回的结果只由目标节点上报，发起的客户端不能代报。
        started = self.apply(job)
        self.assertEqual(self.call('POST', '/projects/%s/applications/%s' % (self.pid, started['application_id']),
                                   {'request_id': uid(), 'status': 'applied', 'detail': {}}, status=403)['error']['code'], 'forbidden')
        viewer_code = self.app.client_code('viewer')
        viewer = self.call('POST', '/clients/pair', {'code': viewer_code, 'name': 'v', 'kind': 'cli'}, headers={})['client_token']
        self.call('POST', '/projects/%s/plans' % self.pid, {'request_id': uid(), 'job_id': job['job_id'], 'node_id': 'target'}, token=viewer, status=403)
        self.call('GET', '/projects/%s/applications/%s' % (self.pid, started['application_id']), token=viewer)
        self.settled(started)

    def test_unbound_and_retired_nodes_are_refused(self):
        job = self.published()
        body = {'request_id': uid(), 'job_id': job['job_id'], 'node_id': 'exec'}
        self.assertEqual(self.call('POST', '/projects/%s/plans' % self.pid, body, status=409)['error']['code'], 'not_bound')
        self.call('POST', '/projects/%s/plans' % self.pid, dict(body, request_id=uid(), node_id='nobody'), status=404)
        self.node('old', capabilities=dict(EXEC_CAPS, labels=[], project_bindings=[{'project_id': self.pid, 'root_name': 'proj'}]))
        self.assertEqual(self.targets(), [{'node_id': 'old', 'root_name': 'proj', 'status': 'online'}])
        version = next(n for n in self.call('GET', '/nodes')['nodes'] if n['node_id'] == 'old')['version']
        self.call('POST', '/nodes/old/retire', {'request_id': uid(), 'expected_version': version, 'retired': True, 'reason': 'gone'})
        self.assertEqual(self.targets(), [])
        self.assertEqual(self.call('POST', '/projects/%s/plans' % self.pid, dict(body, request_id=uid(), node_id='old'), status=409)
                         ['error']['code'], 'node_retired')
        self.assertEqual(self.apply(job, node_id='old', status=409)['error']['code'], 'node_retired')
        # 节点声明的绑定只收项目 ID 与目录名。
        for bad in ([{'project_id': self.pid, 'root_name': 'x', 'path': '/etc'}], [{'project_id': self.pid, 'root_name': 'a/b'}],
                    [{'project_id': 'nope', 'root_name': 'x'}]):
            record = dict(self.executor.record, capabilities=dict(EXEC_CAPS, project_bindings=bad))
            self.call('POST', '/nodes/register', record, token=self.executor.token, status=400)


class RemoteTimeoutTests(RemoteCase):
    def test_node_offline_until_the_deadline_means_failed_and_nothing_written(self):
        self.bind()
        job = self.published()
        started = self.apply(job)  # 节点不再循环：离线
        self.clock.advance(self.app.config['project_operation_timeout_s'] + 1)
        self.app.tick()
        done = self.application(started)
        self.assertEqual((done['status'], done['detail']['reason'], done['detail']['written']), ('failed', 'not_received', []))
        self.assertEqual((self.op(started['operation_id'])['status'], self.current(job)['integration_status']), ('timed_out', 'none'))
        # 节点回来以后不会再领到它，目录不变。
        self.drive(lambda: self.agent.connected and self.agent.reconciled)
        for _ in range(10):
            self.agent.cycle()
            self.clock.advance(1.5)
        self.assertEqual(tree(self.folder), INPUTS)

        # 节点已在心跳里拿到操作、但上报“已领取”时它已到期：controller 回答 timed_out，节点放弃，不执行。
        late = self.apply(job)
        op = self.op(late['operation_id'])
        self.agent.projects.offer(op['op_id'], op['kind'], op['params'])
        self.clock.advance(self.app.config['project_operation_timeout_s'] + 1)
        self.agent.projects.pump()
        self.assertEqual(self.agent.projects.records[op['op_id']]['state'], 'abandoned')
        self.assertEqual((self.application(late)['status'], tree(self.folder)), ('failed', INPUTS))

    def test_received_but_late_result_keeps_started_and_reconciles_when_reported(self):
        self.bind()
        job = self.published()
        gate, original = threading.Event(), project.apply

        def slow(*args, **kwargs):
            gate.wait(30)
            return original(*args, **kwargs)
        with mock.patch.object(project, 'apply', slow):
            started = self.apply(job)
            self.drive(lambda: self.op(started['operation_id'])['status'] == 'received')
            self.clock.advance(self.app.config['project_operation_timeout_s'] + 1)
            self.app.tick()
            # 已领取而没在时限内给出结果：操作记 timed_out，应用记录保持 started（结果待确认），不当作失败。
            self.assertEqual((self.op(started['operation_id'])['status'], self.application(started)['status']), ('timed_out', 'started'))
            self.clock.advance(86400 * 2)
            self.app.tick()
            self.op(started['operation_id'])  # 应用待确认期间这项操作不被清除，节点晚到的结果还要靠它对账
            gate.set()
            done = self.settled(started)
        self.assertEqual((done['status'], tree(self.folder)), ('applied', OUTPUTS))
        late = self.op(started['operation_id'])
        self.assertEqual((late['status'], late['reported_status'], late['result']['application']['status']), ('timed_out', 'succeeded', 'applied'))
        self.assertEqual(self.current(job)['integration_status'], 'integrated')


class RemoteRestartTests(RemoteCase):
    def counting(self):
        calls, original = [], project.apply

        def counted(*args, **kwargs):
            calls.append(args[5])
            return original(*args, **kwargs)
        return calls, mock.patch.object(project, 'apply', counted)

    def test_restart_never_writes_twice_and_reports_the_kept_result(self):
        self.bind()
        job = self.published()
        calls, patch = self.counting()
        with patch:
            started = self.apply(job)
            op_id = started['operation_id']
            # 结果上报的网络中断：结果留在节点本地。
            self.drop = lambda method, path, body: path.endswith(op_id) and b'"succeeded"' in body
            self.drive(lambda: self.agent.projects.records.get(op_id, {}).get('state') == 'finished' and not self.agent.connected)
            self.assertEqual((self.application(started)['status'], tree(self.folder)), ('started', OUTPUTS))
            self.drop = None
            self.restart()
            done = self.settled(started)
            self.assertEqual((done['status'], calls), ('applied', [started['application_id']]))

            # 写完但还没把结果落盘就退出（本地操作停在 running）：重启后不再执行，按本机应用日志补报。
            self.write(INPUTS)
            (self.folder / 'out/result.txt').unlink()
            second = self.published(outputs={'src/app.txt': b'v3\n', 'README.md': b'old readme\n', 'docs/guide.txt': b'guide\n'})
            record = self.apply(second)
            self.drop = lambda method, path, body: path.endswith(record['operation_id']) and b'"succeeded"' in body
            self.drive(lambda: self.agent.projects.records.get(record['operation_id'], {}).get('state') == 'finished')
            self.drop = None
            self.agent.projects.thread.join(10)
            self.agent.close()
            path = self.directory / 'operations' / (record['operation_id'] + '.json')
            saved = json.loads(path.read_text(encoding='utf-8'))
            path.write_bytes(p.canonical_json(dict(saved, state='running', outcome=None)))
            self.agent = self.start()
            done = self.settled(record)
            self.assertEqual((done['status'], done['detail']['changed'], calls[-1], len(calls)), ('applied', ['src/app.txt'], record['application_id'], 2))
            self.assertEqual(tree(self.folder)['src/app.txt'], b'v3\n')

            # 领取后、写入前就退出：没有本机应用日志，说明一个文件都没写，如实记为失败。
            third = self.apply(second)
            op = self.op(third['operation_id'])
            self.call('POST', '/nodes/target/operations/' + op['op_id'], {'status': 'received'}, token=self.agent.client.token)
            node_path = self.directory / 'operations' / (op['op_id'] + '.json')
            self.agent.close()
            node_path.write_bytes(p.canonical_json({'op_id': op['op_id'], 'kind': op['kind'], 'params': op['params'], 'state': 'running',
                                                    'outcome': None, 'at': 0}))
            self.agent = self.start()
            done = self.settled(third)
            self.assertEqual((done['status'], done['detail']['reason'], done['detail']['written'], len(calls)),
                             ('failed', 'interrupted_before_write', [], 2))

    def test_interrupted_write_stays_started_and_is_recovered_on_the_node(self):
        self.bind()
        job = self.published()
        original, wrote = project._write_one, []

        def crash(*args, **kwargs):
            if wrote:
                raise SystemExit('simulated crash')  # 第二个文件写入前进程退出：日志停在 writing
            wrote.append(args[1]['path'])
            return original(*args, **kwargs)
        with mock.patch.object(project, '_write_one', crash):
            started = self.apply(job)
            self.drive(lambda: wrote and not self.agent.projects.thread.is_alive())
        self.restart()
        self.drive(lambda: self.op(started['operation_id'])['status'] in ('succeeded', 'failed'))
        op = self.op(started['operation_id'])
        self.assertEqual((op['status'], op['error'][:11], self.application(started)['status']), ('failed', 'interrupted', 'started'))

        def recover(action):
            op = self.call('POST', '/projects/%s/applications/%s/recover' % (self.pid, started['application_id']),
                           {'request_id': uid(), 'action': action}, status=201)
            self.assertEqual((op['kind'], op['params']['action']), ('project_recover', action))
            self.drive(lambda: self.op(op['op_id'])['status'] in ('succeeded', 'failed', 'timed_out'))
            return self.op(op['op_id'])
        report = recover('status')['result']['report']
        self.assertEqual((report['interrupted'], report['written'], len(report['remaining'])), (True, wrote, 2))
        self.assertEqual(self.application(started)['status'], 'started')
        self.call('POST', '/projects/%s/applications/%s/recover' % (self.pid, started['application_id']),
                  {'request_id': uid(), 'action': 'delete-everything'}, status=400)
        resumed = recover('resume')
        self.assertEqual(resumed['status'], 'succeeded', resumed)
        done = self.application(started)
        self.assertEqual((done['status'], tree(self.folder)), ('applied', OUTPUTS))
        self.assertEqual(self.call('POST', '/projects/%s/applications/%s/recover' % (self.pid, started['application_id']),
                                   {'request_id': uid(), 'action': 'status'}, status=409)['error']['code'], 'conflict')
        self.assertNoPaths([self.current(job), report, resumed])


class RemoteRootsTests(RemoteCase):
    def test_binding_outside_integration_roots_is_refused(self):
        allowed = self.temp / 'allowed'
        allowed.mkdir()
        config = json.loads((self.directory / 'agent.json').read_text(encoding='utf-8'))
        (self.directory / 'agent.json').write_bytes(p.canonical_json(dict(config, integration_roots=[str(allowed)])))
        with self.assertRaises(project.ProjectError) as caught:
            node_projects.bind(self.directory, self.pid, self.folder)
        self.assertEqual(caught.exception.code, 'outside_integration_roots')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(node_agent.main(['project-bind', '--data-dir', str(self.directory), self.pid, str(self.folder)]), 1)
        self.assertEqual(json.loads(out.getvalue())['error']['code'], 'outside_integration_roots')
        inside = allowed / 'proj'
        self.write(INPUTS, inside)
        node_projects.bind(self.directory, self.pid, inside)
        self.assertEqual(node_projects.listing(self.directory)['bindings'][0]['root_name'], 'proj')

        # 绑定之后范围收窄：执行时再核对，计划报错、写回记为失败且一个文件都没写。
        self.drive(lambda: self.targets())
        job = self.published()
        self.agent.config['integration_roots'] = [str(self.temp / 'elsewhere')]
        plan = self.plan(job)
        self.assertEqual((plan['status'], plan['error'].split(':')[0]), ('failed', 'outside_integration_roots'))
        done = self.settled(self.apply(job))
        self.assertEqual((done['status'], done['detail']['reason'], tree(inside)), ('failed', 'outside_integration_roots', INPUTS))
        self.assertNoPaths([plan, done])


class InProcessConn:
    """CLI 的连接换成进程内 controller（不开 socket）；其余与 cli.Conn 相同。"""
    controller_id = url = None

    def __init__(self, case):
        self.case = case

    def send(self, method, path, data=None):
        if method != 'GET' and isinstance(data, dict):
            data.setdefault('request_id', uid())
        result = self.case.app.handle(method, p.API_PREFIX + path, {'authorization': 'Bearer ' + self.case.admin},
                                      p.canonical_json(data) if data is not None else b'')
        return result.status, json.loads(result.body) if result.body else None

    def call(self, method, path, data=None):
        status, body = self.send(method, path, data)
        if not 200 <= status < 300:
            raise SystemExit(cli.rejected(status))
        return body


class RemoteCliTests(RemoteCase):
    def run_cli(self, *argv):
        """在另一个线程里运行 CLI（它会轮询等待），主线程推进节点。"""
        result, out = {}, io.StringIO()

        def target():
            with mock.patch.object(cli, 'Conn', lambda a: InProcessConn(self)), contextlib.redirect_stdout(out):
                try:
                    result['code'] = project_cli.main(list(argv))
                except SystemExit as exc:
                    result['code'] = exc.code
        thread = threading.Thread(target=target)
        thread.start()
        self.drive(lambda: not thread.is_alive(), limit=3000, step=.2)
        return result['code'], json.loads(out.getvalue())

    def test_cli_plans_applies_and_lists_remote_work(self):
        self.bind()
        job = self.published()
        self.write({'src/app.txt': b'my own edit\n'})
        code, plan = self.run_cli('plan', job['job_id'], '--node', 'target')
        self.assertEqual((code, plan['node_id'], plan['summary']['conflict']), (0, 'target', 1))
        code, refused = self.run_cli('apply', job['job_id'], '--node', 'target')
        self.assertEqual((code, refused['status'], refused['application']['detail']['changed']), (5, 'conflict', []))
        code, partial = self.run_cli('apply', job['job_id'], '--node', 'target', '--skip-conflicts')
        self.assertEqual((code, partial['status'], partial['operation']['status']), (5, 'partial', 'succeeded'))
        code, listed = self.run_cli('recover', '--node', 'target')
        self.assertEqual((code, listed), (0, {'unfinished': []}))
        self.assertNoPaths([plan, refused, partial])


if __name__ == '__main__':
    unittest.main()
