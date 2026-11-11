"""Socket-free worker fixture; all interactions use the public App API."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import uuid

from agent_collab.controller import App
from agent_collab import domain as d
from agent_collab import protocol as p


def uid():
    return str(uuid.uuid4())


def rc4_agent_body(path, body):
    """Bridge optional rc5 metadata in legacy App.handle regression tests only.

    The controller work package owns accepting these new fields. rc5 wire
    identity and heartbeat integrations are asserted separately using fixtures.
    """
    if path.endswith('/register') or path.endswith('/complete'):
        value = json.loads(body)
        if path.endswith('/register'):
            for key in ('agent', 'harness_paths', 'harness_disabled'):
                value.get('capabilities', {}).pop(key, None)
        else:
            value.get('runtime', {}).pop('agent_commit', None)
        return p.canonical_json(value)
    return body


def manifest(content=b'input', name='input.txt', base=None):
    return {'version': 1, 'base': base, 'files': [
        {'path': name, 'sha256': p.sha256_bytes(content), 'size': len(content), 'executable': False}], 'deleted': []}


def contract(**changes):
    value = {'goal': 'test', 'harness': 'stub', 'harness_params': {'steps': []},
             'inputs_manifest': {'version': 1, 'base': None, 'files': [], 'deleted': []}}
    value.update(changes)
    return value


class Clock:
    def __init__(self):
        self.now = 1804800000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ControllerCase(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / '.dev' / 'tmp'
        root.mkdir(parents=True, exist_ok=True)
        self.temp = Path(tempfile.mkdtemp(dir=str(root)))
        self.clock = Clock()
        self.app = App(self.temp / 'data', clock=self.clock)
        self.admin = (self.temp / 'data' / 'admin.token').read_text().strip()
        if self.legacy_settings:
            self.settings(d.migrated_settings(None))

    # 既有用例描述的是升级前部署的行为，按那类部署启动时迁移得到的模型设置运行（protocol §12.3）；
    # 新部署从未设置开始，相关用例把它设为 False。
    legacy_settings = True

    def settings(self, value):
        with self.app.store.transaction():
            self.app.store.put('settings', 'models', value)

    def tearDown(self):
        if self.app:
            self.app.close()
        shutil.rmtree(str(self.temp))

    def call(self, method, path, data=None, token=None, status=200, headers=None):
        hdr = {'authorization': 'Bearer ' + (token or self.admin)} if headers is None else headers
        body = data if isinstance(data, bytes) else p.canonical_json(data) if data is not None else b''
        result = self.app.handle(method, p.API_PREFIX + path, hdr, body)
        self.assertEqual(result.status, status, result.body.decode(errors='replace'))
        return json.loads(result.body) if result.body else None

    def job(self, value=None, **changes):
        data = dict(request_id=uid(), title='test', contract=value or contract())
        data.update(changes)
        return self.call('POST', '/jobs', data, status=201)

    def current(self, job):
        return self.call('GET', '/jobs/' + job['job_id'])

    def mutate(self, job, action, expected_status=200, **changes):
        data = dict(request_id=uid(), expected_version=self.current(job)['version'])
        data.update(changes)
        return self.call('POST', '/jobs/' + job['job_id'] + '/' + action, data, status=expected_status)

    def node(self, node_id='a', **changes):
        return FakeNode(self, node_id, **changes)

    def restart(self):
        self.app.close()
        self.app = App(self.temp / 'data', clock=self.clock)


class FakeNode:
    def __init__(self, case, node_id, **changes):
        self.case, self.node_id = case, node_id
        code = case.app.pairing_code(node_id)
        paired = case.call('POST', '/nodes/pair', {'node_id': node_id, 'code': code}, headers={})
        self.token, self.generation = paired['node_token'], paired['authority_generation']
        self.record = {'node_id': node_id, 'physical_host_id': node_id, 'failure_domain': node_id,
                       'capabilities': {'os': 'linux', 'arch': 'x86_64', 'tools': {'python3': '3.9'}, 'gpu': [],
                                        'harness': {'stub': '1', 'codex': '0.167.0'}, 'labels': []},
                       'resources': {'slots': 2, 'host_slots': 2, 'mem_mb': None},
                       'runtime_profile': {'user': 'test', 'workspace_policy': 'isolated'},
                       'provider_account_refs': {'codex': 'account'}, 'auth_health': {'codex': 'ok'}, 'agent_version': 'test'}
        self.record.update(copy.deepcopy(changes))
        case.call('POST', '/nodes/register', self.record, token=self.token)
        self.assignments = []

    def claim(self, status=200):
        result = self.case.call('POST', '/nodes/' + self.node_id + '/claim', {}, token=self.token, status=status)
        if result and 'assignment' in result:
            self.assignments.append(result['assignment'])
            return result['assignment']
        return result

    def heartbeat(self, assignments=None, generation=None, health=None):
        return self.case.call('POST', '/nodes/' + self.node_id + '/heartbeat', {
            'authority_generation': self.generation if generation is None else generation,
            'attempts': [{'attempt_id': a['attempt_id'], 'epoch': a['epoch'], 'state': 'running'} for a in
                         (self.assignments if assignments is None else assignments)], 'auth_health': health or {}}, token=self.token)

    def payload(self, assignment, **changes):
        data = {k: assignment[k] for k in ('epoch', 'lease_token')}
        data.update(changes)
        return data

    def event(self, assignment, event_type='attempt.started', payload=None, event_id=None, status=200):
        data = self.payload(assignment, events=[{'event_id': event_id or uid(), 'type': event_type,
                                               'at': '2026-11-09T00:00:00.000Z', 'payload': payload or {}}])
        return self.case.call('POST', '/attempts/' + assignment['attempt_id'] + '/events', data, token=self.token, status=status)

    def upload(self, content):
        digest = p.sha256_bytes(content)
        exists = self.case.app.blobs.exists(digest)
        self.case.call('PUT', '/blobs/' + digest, content, token=self.token, status=200 if exists else 201)
        return digest

    def checkpoint(self, assignment, value=None, kind='boundary', checkpoint_id=None, status=200, **changes):
        data = self.payload(assignment, checkpoint_id=checkpoint_id or uid(), kind=kind,
                            manifest=value or contract()['inputs_manifest'], summary={}, native_session_ref=None)
        data.update(changes)
        return self.case.call('POST', '/attempts/' + assignment['attempt_id'] + '/checkpoints', data, token=self.token, status=status)

    def complete(self, assignment, outcome='succeeded', checkpoint=None, status=200, **changes):
        data = self.payload(assignment, outcome=outcome, output_checkpoint_id=checkpoint and checkpoint['checkpoint_id'],
                            failure_class=None, failure_detail=None, report={}, native_session_ref=None)
        data.update(changes)
        return self.case.call('POST', '/attempts/' + assignment['attempt_id'] + '/complete', data, token=self.token, status=status)

    def publish(self, assignment, checkpoint, request_id=None, status=200, **changes):
        data = self.payload(assignment, request_id=request_id or uid(), attempt_id=assignment['attempt_id'],
                            checkpoint_id=checkpoint['checkpoint_id'], expected_base_version=assignment['delivery_version'])
        data.update(changes)
        return self.case.call('POST', '/jobs/' + assignment['job_id'] + '/publish', data, token=self.token, status=status)
