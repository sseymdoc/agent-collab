"""TEST ONLY: in-process endpoint fixture, not a scheduler or second controller."""
import copy
import hmac
import json
import uuid
from urllib.parse import urlsplit

from agent_collab import protocol as p


def identity():
    return str(uuid.uuid4())


def no_auth(*args, **kwargs):
    from subprocess import CompletedProcess
    return CompletedProcess(args[0], 1, '', '')


class FakeController:
    def __init__(self):
        self.token_hash = p.sha256_bytes(b'test-node-secret')
        self.blobs = {}
        self.calls = []
        self.assignments = []
        self.leases = {}
        self.events = {}
        self.checkpoints = {}
        self.checkpoint_owner = {}
        self.completions = {}
        self.publications = {}
        self.integrations = []
        self.integration_items = {}
        self.issued = {}
        self.revoked = set()
        self.cancel = set()
        self.generation = 1
        self.offline = False
        self.drain = False
        self.paused = False
        self.reconcile_required = False
        self.lose_event_response = False
        self.lose_publish_response = False
        self.missing_once = False
        self.version_mismatch = False
        self.integration_error = None
        self.max_json_bytes = None
        self.too_large_hits = 0
        self.fail_put_after = None
        self.puts = []
        self.checkpoint_requests = []
        self.heartbeat_s = 1
        self.lease_ttl_s = 5

    def add(self, steps=None, manifest=None, summary=None, side_effects='none', publish='auto', integration=None):
        sha = p.manifest_sha256(manifest) if manifest else None
        if manifest:
            self.blobs[sha] = p.canonical_json(manifest)
        assignment = dict(attempt_id=identity(), job_id=identity(), work_package_id=identity(),
            epoch=1, lease_token='test-lease-' + identity(), lease_expires_at='2099-01-01T00:00:00Z',
            authority_generation=self.generation, delivery_version=0, recovered_from_attempt=None,
            native_session_ref=None, start=dict(kind='checkpoint' if summary else 'inputs' if manifest else 'empty',
                manifest_sha256=sha, checkpoint_id=identity() if summary else None, summary=summary),
            contract=dict(harness='stub', harness_params={'steps': steps or []},
                recovery=dict(auto=True, side_effects=side_effects), resources={'slots': 1},
                publish=dict(mode=publish, requires_acceptance=False), integration=integration or {}))
        self.assignments.append(assignment)
        self.issued[assignment['job_id']] = assignment
        self.leases[assignment['attempt_id']] = dict(epoch=1, generation=self.generation,
            token_hash=p.sha256_bytes(assignment['lease_token'].encode()))
        return assignment

    def response(self, status, body=None):
        return status, {}, p.canonical_json(body) if body is not None else b''

    def error(self, code, **details):
        return self.response(409, p.error_body(code, code, **details))

    def fenced(self, identity, body):
        lease = self.leases.get(identity)
        return not lease or identity in self.revoked or body.get('epoch') != lease['epoch'] or (
            lease['generation'] != self.generation or body.get('authority_generation') != self.generation or
            not hmac.compare_digest(p.sha256_bytes((body.get('lease_token') or '').encode()), lease['token_hash']))

    def __call__(self, method, url, headers, body):
        path = urlsplit(url).path[len(p.API_PREFIX):]
        if self.offline:
            raise ConnectionError('partition')
        self.calls.append((method, path))
        if path == '/nodes/pair':
            value = json.loads(body)
            return self.response(200, dict(node_id=value['node_id'], node_token='test-node-secret',
                                            controller_id=str(uuid.uuid4()), authority_generation=self.generation))
        if path == '/healthz':
            return self.response(200, {'ok': True})
        supplied = headers.get('authorization', '').removeprefix('Bearer ')
        if not hmac.compare_digest(p.sha256_bytes(supplied.encode()), self.token_hash):
            return self.response(401, p.error_body('unauthorized', 'test auth failed'))
        if path.startswith('/blobs/'):
            sha = path.split('/')[-1]
            if method == 'HEAD':
                return self.response(200 if sha in self.blobs else 404)
            if method == 'GET':
                return (200, {}, self.blobs[sha]) if sha in self.blobs else self.response(404, p.error_body('not_found', 'blob'))
            if method == 'PUT':
                if self.fail_put_after is not None and len(self.puts) >= self.fail_put_after:
                    self.fail_put_after = None
                    raise ConnectionError('interrupted upload')
                if not hmac.compare_digest(p.sha256_bytes(body), sha):
                    return self.response(400, p.error_body('invalid', 'hash'))
                self.puts.append(sha)
                self.blobs[sha] = body
                return self.response(201)
        value = json.loads(body or b'{}')
        if path == '/nodes/register':
            return self.response(200, dict(node=value, authority_generation=self.generation,
                config=dict(heartbeat_s=self.heartbeat_s, lease_ttl_s=self.lease_ttl_s)))
        if path.endswith('/heartbeat'):
            self.last_heartbeat = copy.deepcopy(value)
            for entry in value['attempts']:
                if entry['state'] == 'unknown' and entry['attempt_id'] not in self.completions:
                    self.revoked.add(entry['attempt_id'])
            identities = [item['attempt_id'] for item in value['attempts']]
            stale = [a for a in identities if a in self.revoked or self.leases.get(a, {}).get('generation') != self.generation]
            return self.response(200, dict(server_time='2026-11-09T00:00:00Z', drain=self.drain,
                integrations=[item for item in self.integration_items.values()
                              if item.get('target_node_id') == path.split('/')[2]],
                dispatch_paused=self.paused, renewed=[a for a in identities if a not in stale and a not in self.completions],
                revoked=[dict(attempt_id=a, reason='superseded') for a in stale], cancel=list(self.cancel),
                reconcile_required=self.reconcile_required or value['authority_generation'] != self.generation))
        if path.endswith('/claim'):
            return self.response(200, {'assignment': self.assignments.pop(0)}) if self.assignments else self.response(204)
        parts = path.strip('/').split('/')
        if len(parts) == 3 and parts[0] == 'attempts':
            identity, operation = parts[1:]
            stale = self.fenced(identity, value)
            if operation == 'events':
                if self.max_json_bytes is not None and len(body) > self.max_json_bytes:
                    self.too_large_hits += 1
                    return self.response(413, p.error_body('too_large', 'request body too large', limit=self.max_json_bytes))
                accepted, duplicates = [], []
                for event in value['events']:
                    key = event['event_id']
                    if key in self.events:
                        if self.events[key]['event'] != event:
                            return self.error('conflict')
                        duplicates.append(key)
                    else:
                        self.events[key] = dict(event=event, stale=stale)
                        accepted.append(key)
                if self.lose_event_response:
                    self.lose_event_response = False
                    raise ConnectionError('response lost after commit')
                return self.response(200, dict(accepted=accepted, duplicates=duplicates, stale=stale))
            if stale:
                return self.error('fenced')
            if operation == 'checkpoints':
                self.checkpoint_requests.append(copy.deepcopy(value))
                key = value['checkpoint_id']
                manifest = p.validate_manifest(value['manifest'])
                if self.missing_once and manifest['files']:
                    self.missing_once = False
                    self.blobs.pop(manifest['files'][0]['sha256'], None)
                missing = [f['sha256'] for f in manifest['files'] if f['sha256'] not in self.blobs]
                if missing:
                    return self.error('missing_blobs', missing=missing)
                if p.manifest_sha256(manifest) not in self.blobs:
                    return self.error('missing_blobs', missing=[p.manifest_sha256(manifest)])
                if key in self.checkpoints and self.checkpoints[key] != value:
                    return self.error('conflict')
                self.checkpoints[key] = copy.deepcopy(value)
                self.checkpoint_owner[key] = identity
                return self.response(200, dict(checkpoint_id=key, state='durable'))
            if operation == 'complete':
                if value['outcome'] == 'succeeded':
                    checkpoint = self.checkpoints.get(value['output_checkpoint_id'])
                    # 与真实 controller 一致：成功完成只能引用本 attempt 自己的 final 检查点。
                    if (not checkpoint or checkpoint['kind'] != 'final' or
                            self.checkpoint_owner.get(value['output_checkpoint_id']) != identity):
                        return self.error('conflict')
                if identity in self.completions and self.completions[identity] != value:
                    return self.error('conflict')
                self.completions[identity] = copy.deepcopy(value)
                return self.response(200, {'attempt_id': identity})
        if len(parts) == 3 and parts[0] == 'jobs':
            job, operation = parts[1:]
            if operation == 'publish':
                if self.fenced(value['attempt_id'], value):
                    return self.error('fenced')
                assignment = self.issued.get(job)
                # 发布必须来自该 job 的 attempt、引用本 attempt 的检查点，且基于当前交付版本。
                if (not assignment or assignment['attempt_id'] != value['attempt_id'] or
                        self.checkpoint_owner.get(value['checkpoint_id']) != value['attempt_id'] or
                        self.checkpoints[value['checkpoint_id']]['kind'] != 'final'):
                    return self.error('conflict')
                if self.version_mismatch or value.get('expected_base_version') != assignment['delivery_version']:
                    return self.error('version_mismatch')
                assert self.completions[value['attempt_id']]['outcome'] == 'succeeded'
                key = value['request_id']
                if key in self.publications and self.publications[key] != value:
                    return self.error('conflict')
                self.publications[key] = copy.deepcopy(value)
                spec = assignment['contract'].get('integration', {})
                if spec.get('target_node_id') and spec.get('target_path') and job not in self.integration_items:
                    checkpoint = self.checkpoints[value['checkpoint_id']]
                    self.integration_items[job] = dict(job_id=job, checkpoint_id=value['checkpoint_id'],
                        manifest_sha256=p.manifest_sha256(checkpoint['manifest']),
                        base_manifest_sha256=assignment['start']['manifest_sha256'],
                        target_path=spec['target_path'], target_node_id=spec['target_node_id'],
                        exclude=assignment['contract'].get('workspace', {}).get('exclude', []))
                if self.lose_publish_response:
                    self.lose_publish_response = False
                    raise ConnectionError('publication response lost')
                return self.response(200, dict(job_id=job, delivery_version=1, checkpoint_id=value['checkpoint_id']))
            if operation == 'integration':
                if self.integration_error is not None:
                    return self.error('conflict', **self.integration_error)
                self.integrations.append(value)
                if value['status'] != 'integration_pending':
                    self.integration_items.pop(job, None)
                return self.response(200, value)
        raise AssertionError('fixture endpoint not implemented: ' + method + ' ' + path)
