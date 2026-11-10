"""Single-authority controller; App is socket-free, HTTP only transports Responses."""
import argparse
from dataclasses import dataclass
import hashlib
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import threading
import time
from typing import Iterator, Optional
from urllib.parse import parse_qs, unquote, urlsplit
import uuid

from . import domain as d
from .release import release_info
from . import protocol as p
from .blobstore import BlobStore
from .artifacts import DEFAULT_EXCLUDES, workspace_excluded
from .store import Store


@dataclass
class Response:
    status: int
    headers: list
    body: bytes = b''
    stream: Optional[Iterator[bytes]] = None


def response(value=None, status=200, headers=None):
    return Response(status, [('Content-Type', 'application/json; charset=utf-8')] + (headers or []),
                    p.canonical_json(value) if value is not None else b'')


class APIError(Exception):
    def __init__(self, code, message, **details):
        self.code, self.message, self.details = code, message, details

    def response(self):
        status = {'invalid': 400, 'unauthorized': 401, 'forbidden': 403, 'not_found': 404,
                  'too_large': 413}.get(self.code, 409)
        return response(p.error_body(self.code, self.message, **self.details), status)


def digest_token(token):
    if not isinstance(token, str):
        raise ValueError('credential must be a string')
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


# 与节点共用同一版本身份读取（§11.3）：versions/<v>/release-manifest.json，开发运行时回退到包版本。
RELEASE = release_info()
ARCHIVE_EVENT_LIMIT = 1000  # 每个节点可写入“恢复前 attempt”归档事件的总数上限


def merge_health(old, new):
    """节点上报的 unknown 不能抹掉 controller 已确认的 expired；只有 ok 才解除。"""
    merged = dict(old)
    for harness, value in new.items():
        if not (value == 'unknown' and old.get(harness) == 'expired'):
            merged[harness] = value
    return merged


def new_id():
    return str(uuid.uuid4())


def require_uuid(value, name='id'):
    p.check_uuid(value, name)
    if uuid.UUID(value).version != 4:
        raise ValueError(name + ' must be UUID v4')
    return value


def clean(value, tokens=()):
    """Never persist supplied controller credentials in logs, reports or events."""
    if isinstance(value, dict):
        return {k: ('[redacted]' if any(word in k.lower() for word in ('token', 'password', 'secret', 'authorization'))
                    else clean(v, tokens)) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v, tokens) for v in value]
    if isinstance(value, str):
        for token in tokens:
            if token:
                value = value.replace(token, '[redacted]')
        return re.sub(r'Bearer\s+\S+', 'Bearer [redacted]', value, flags=re.I)
    return value


class App:
    def __init__(self, data_dir, config=None, clock=time.time):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.config = dict(p.DEFAULTS, public_origin='http://127.0.0.1:18669',
                           deployment_profile='local', guarantees='Single controller; durable checkpoints only.',
                           score_weights={})
        if (self.data_dir / 'config.json').exists():
            self.config.update(json.loads((self.data_dir / 'config.json').read_text(encoding='utf-8')))
        self.config.update(config or {})
        self._validate_config()
        self.blobs = BlobStore(self.data_dir, self.config['max_blob_bytes'])
        self.store = Store(self.data_dir)
        token_file = self.data_dir / 'admin.token'
        try:
            fd = os.open(str(token_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(secrets.token_urlsafe(32) + '\n')
                f.flush()
                os.fsync(f.fileno())
        if os.name == 'posix':
            token_file.chmod(0o600)
        self.admin_hash = digest_token(token_file.read_text(encoding='utf-8').strip())
        now = self.clock()
        with self.store.transaction():
            if self.store.get('meta', 'controller') is None:
                self.store.put('meta', 'controller', {'controller_id': new_id(), 'authority_generation': 1,
                               'version': 1, 'dispatch_paused': False, 'created_at': d.timestamp(now)})
            for node in self.store.all('node'):
                node['capability_evidence'] = d.capability_evidence(node)
                self.store.put('node', node['node_id'], node)
            for attempt in self.store.all('attempt'):
                if attempt['status'] in ('leased', 'running') and attempt['authority_generation'] == self.meta()['authority_generation']:
                    attempt['lease_expires_at'] = d.timestamp(max(d.seconds(attempt['lease_expires_at']), now + self.config['lease_ttl_s']))
                    self.store.put('attempt', attempt['attempt_id'], attempt)
                    self._managed_lease(attempt, renew=True)
                elif attempt['status'] == 'assigned':
                    private = self.private(attempt)
                    private['claim_until'] = max(private['claim_until'], now + self.config['assign_claim_timeout_s'])
                    self.store.put('attempt_private', attempt['attempt_id'], private)
            self.emit('controller.started', {'authority_generation': self.meta()['authority_generation']}, now)

    def _validate_config(self):
        if set(self.config) - (set(p.DEFAULTS) | {'public_origin', 'deployment_profile', 'guarantees', 'score_weights',
                                               'managed_coordinator', 'standby_profiles'}):
            raise ValueError('unknown controller config key')
        for key in p.DEFAULTS:
            value = self.config[key]
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError('invalid config ' + key)
        for key in ('max_blob_bytes', 'max_json_bytes'):
            if type(self.config[key]) is not int:
                raise ValueError(key + ' must be integer')
        if self.config['suspect_after_s'] > self.config['lease_ttl_s']:
            raise ValueError('suspect_after_s must not exceed lease_ttl_s')
        origin = urlsplit(self.config['public_origin'])
        if origin.scheme not in ('http', 'https') or not origin.netloc or origin.path or origin.query or origin.fragment or origin.username:
            raise ValueError('public_origin must be an HTTP(S) origin without a path')
        allowed = {'free_slots', 'success_rate', 'same_host', 'different_failure_domain', 'warm_start'}
        weights = self.config['score_weights']
        if not isinstance(weights, dict) or set(weights) - allowed or any(type(v) not in (int, float) or not math.isfinite(v) for v in weights.values()):
            raise ValueError('invalid score_weights')

    def close(self):
        self.store.close()

    def meta(self):
        return self.store.get('meta', 'controller')

    def get(self, kind, ident):
        value = self.store.get(kind, ident)
        if value is None:
            raise APIError('not_found', kind + ' not found')
        return value

    def get_attempt(self, ident, data):
        attempt = self.store.get('attempt', ident)
        if attempt is None:
            token = data.get('lease_token', '')
            issued = token.split('.', 1)[0] if isinstance(token, str) else ''
            if issued.isdigit() and int(issued) != self.meta()['authority_generation']:
                raise APIError('fenced', 'assignment predates this authority', reason='stale_generation')
            raise APIError('not_found', 'attempt not found')
        return attempt

    def private(self, attempt):
        return self.get('attempt_private', attempt['attempt_id'])

    def emit(self, event_type, payload, now, job=None, attempt=None, node=None):
        return self.store.event(event_type, clean(payload), d.timestamp(now),
                                job_id=job and job['job_id'], attempt_id=attempt and attempt['attempt_id'],
                                node_id=node or (attempt and attempt['node_id']))

    def change(self, kind, record, now, **changes):
        updated = d.transition(record, now, **changes)
        if updated != record:
            key = {'job': 'job_id', 'node': 'node_id'}.get(kind)
            self.store.put(kind, record[key] if key else 'controller', updated)
            self.emit(kind + '.updated', updated, now, job=updated if kind == 'job' else None,
                      node=updated.get('node_id'))
        return updated

    def version(self, data, record):
        if type(data.get('expected_version')) is not int:
            raise ValueError('expected_version is required')
        if data['expected_version'] != record['version']:
            raise APIError('version_mismatch', 'record changed', current=record)

    def pairing_code(self, node_id, ttl=600):
        p.check_node_id(node_id)
        if type(ttl) is not int or ttl <= 0:
            raise ValueError('ttl must be positive')
        code = secrets.token_hex(18)  # 不以 '-' 开头，避免命令行参数解析把配对码当成选项
        with self.store.transaction():
            self.store.put('pairing', digest_token(code), {'hash': digest_token(code), 'node_id': node_id, 'expires': self.clock() + ttl})
        return code

    def _authenticate(self, headers, method):
        authorization = headers.get('authorization', '')
        if authorization:
            if not authorization.startswith('Bearer '):
                raise APIError('unauthorized', 'Bearer credential required')
            token_hash = digest_token(authorization[7:])
            if hmac.compare_digest(token_hash, self.admin_hash):
                return ('admin', None)
            credential = self.store.get('node_token', token_hash)
            if credential and hmac.compare_digest(token_hash, credential['hash']):
                return ('node', credential['node_id'])
            raise APIError('unauthorized', 'invalid credential')
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get('cookie', ''))
            value = cookie['ac_session'].value if 'ac_session' in cookie else ''
        except Exception:
            value = ''
        hashed = digest_token(value)
        session = self.store.get('session', hashed)
        if not session or not hmac.compare_digest(hashed, session['hash']) or session['expires'] <= self.clock():
            raise APIError('unauthorized', 'authentication required')
        if method not in ('GET', 'HEAD'):
            csrf = digest_token(headers.get('x-csrf-token', ''))
            if not hmac.compare_digest(csrf, session['csrf_hash']) or headers.get('origin', '') not in ('', self.config['public_origin']):
                raise APIError('forbidden', 'CSRF or Origin check failed')
        return ('session', hashed)

    @staticmethod
    def client(identity):
        if identity[0] not in ('admin', 'session'):
            raise APIError('forbidden', 'client credential required')

    @staticmethod
    def node(identity, node_id=None):
        if identity[0] != 'node' or (node_id is not None and node_id != identity[1]):
            raise APIError('forbidden', 'matching node credential required')

    def handle(self, method, target, headers, body):
        method = method.upper()
        is_head = method == 'HEAD'
        try:
            parts = urlsplit(target)
            path = parts.path
            if parts.scheme or parts.netloc:
                raise ValueError('target must be relative')
            if method in ('GET', 'HEAD') and path in ('/healthz', p.API_PREFIX + '/healthz'):
                result = response({'ok': True})
            elif not path.startswith(p.API_PREFIX + '/'):
                result = self.static(path) if method in ('GET', 'HEAD') and not path.startswith('/api/') else APIError('not_found', 'route not found').response()
            else:
                route = path[len(p.API_PREFIX):]
                raw_blob = method == 'PUT' and route.startswith('/blobs/')
                limit = self.config['max_blob_bytes' if raw_blob else 'max_json_bytes']
                if len(body) > limit:
                    raise APIError('too_large', 'request body too large', limit=limit)
                data = {} if raw_blob or not body else json.loads(body.decode('utf-8'), parse_constant=lambda x: (_ for _ in ()).throw(ValueError('non-finite JSON')))
                if not isinstance(data, dict):
                    raise ValueError('request JSON must be an object')
                query = parse_qs(parts.query, keep_blank_values=True)
                with self.store.transaction():
                    if method == 'POST' and route == '/session':
                        result = self.login(data, headers)
                    elif method == 'POST' and route == '/nodes/pair':
                        result = self.pair(data)
                    else:
                        identity = self._authenticate(headers, method)
                        # Expiry is an independent authoritative fact, even when a request is rejected.
                        self._expire(self.clock())
                        self.store.db.execute('SAVEPOINT request')
                        try:
                            result = self.route('GET' if is_head else method, route, query, headers, body, data, identity)
                        except (APIError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
                            self.store.db.execute('ROLLBACK TO request')
                            result = self.error(exc)
                        finally:
                            self.store.db.execute('RELEASE request')
        except (APIError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
            result = self.error(exc)
        if is_head:
            result.headers.append(('Content-Length', str(len(result.body))))
            result.body, result.stream = b'', None
        return result

    @staticmethod
    def error(exc):
        if isinstance(exc, APIError):
            return exc.response()
        if isinstance(exc, OverflowError):
            return APIError('too_large', str(exc)).response()
        # Do not reflect raw malformed request values (which can contain credentials).
        return APIError('invalid', 'invalid request: ' + (str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else 'shape or JSON')).response()

    def static(self, path):
        relative = unquote(path).lstrip('/')
        if relative and (any(x in ('.', '..') for x in relative.split('/')) or '\\' in relative or '\x00' in relative or ':' in relative):
            raise ValueError('unsafe static path')
        root = Path(__file__).parent / 'static'
        index = root / 'index.html'
        if not index.is_file():
            if path == '/':
                return Response(200, [('Content-Type', 'text/plain; charset=utf-8')], b'agent-collab controller; UI is not installed.\n')
            raise APIError('not_found', 'static UI not installed')
        candidate = (root / relative).resolve()
        if root.resolve() not in candidate.parents and candidate != root.resolve():
            raise APIError('forbidden', 'unsafe static path')
        if not candidate.is_file():
            candidate = index.resolve()
            if root.resolve() not in candidate.parents:
                raise APIError('forbidden', 'unsafe static index')
        # index.html 每次校验，升级后立即生效；带内容哈希的 assets 可长期缓存。
        cache = 'public, max-age=31536000, immutable' if relative.startswith('assets/') and candidate != index.resolve() else 'no-cache'
        return Response(200, [('Content-Type', mimetypes.guess_type(str(candidate))[0] or 'application/octet-stream'),
                              ('X-Content-Type-Options', 'nosniff'), ('Cache-Control', cache)], candidate.read_bytes())

    def login(self, data, headers):
        if headers.get('origin', '') not in ('', self.config['public_origin']):
            raise APIError('forbidden', 'Origin check failed')
        token = data.get('token', '')
        if not isinstance(token, str) or not hmac.compare_digest(digest_token(token), self.admin_hash):
            raise APIError('unauthorized', 'invalid credential')
        session = secrets.token_urlsafe(32)
        hashed = digest_token(session)
        csrf = self.csrf_for(hashed)
        self.store.put('session', hashed, {'hash': hashed, 'csrf_hash': digest_token(csrf), 'expires': self.clock() + 86400})
        secure = '; Secure' if self.config['public_origin'].startswith('https:') else ''
        return response({'csrf_token': csrf}, headers=[('Set-Cookie', 'ac_session=' + session + '; Path=/; HttpOnly; SameSite=Strict' + secure)])

    def csrf_for(self, session_hash):
        # 由服务端秘密派生，GET /session 可为同一会话重算而不保存原值；多个标签页共用同一令牌。
        return hmac.new(self.admin_hash.encode(), b'csrf:' + session_hash.encode(), 'sha256').hexdigest()

    def pair(self, data):
        p.check_node_id(data['node_id'])
        hashed = digest_token(data['code'])
        record = self.store.get('pairing', hashed)
        if not record or not hmac.compare_digest(record['hash'], hashed) or record['expires'] <= self.clock() or record['node_id'] != data['node_id']:
            raise APIError('unauthorized', 'invalid or expired pairing code')
        self.store.delete('pairing', hashed)
        for credential in self.store.all('node_token'):
            if credential['node_id'] == data['node_id']:
                self.store.delete('node_token', credential['hash'])
        token = secrets.token_urlsafe(32)
        self.store.put('node_token', digest_token(token), {'hash': digest_token(token), 'node_id': data['node_id']})
        self.emit('node.paired', {'node_id': data['node_id']}, self.clock(), node=data['node_id'])
        return response(dict(node_id=data['node_id'], node_token=token,
                             controller_id=self.meta()['controller_id'], authority_generation=self.meta()['authority_generation']))

    def idempotent(self, endpoint, data, operation, creation=False):
        ident = require_uuid(data.get('request_id'), 'request_id')
        hashed = p.value_hash(data)
        row = self.store.db.execute('SELECT * FROM requests WHERE endpoint=? AND request_id=?', (endpoint, ident)).fetchone()
        if row:
            if not hmac.compare_digest(row['request_hash'], hashed):
                raise APIError('conflict', 'request_id reused with different content')
            return Response(200 if creation else row['status'], json.loads(row['headers']), bytes(row['body']))
        result = operation()
        if 200 <= result.status < 300:
            self.store.db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?)',
                                  (endpoint, ident, hashed, result.status, json.dumps(result.headers), result.body))
        return result

    def remember_manifest(self, job_id, manifest):
        digest = self.blobs.manifest(manifest)
        self.store.db.execute('INSERT OR IGNORE INTO manifest_refs VALUES(?,?)', (job_id, digest))
        return digest

    def manifest(self, digest):
        return json.loads(self.blobs.read(digest)) if digest else None

    def root(self, job):
        return self.get('root', job['work_package_id'])

    def coordinator(self, root_id):
        lease = self.store.get('coordinator', root_id)
        if lease and (lease['authority_generation'] == self.meta()['authority_generation'] and
                      d.seconds(lease['expires_at']) > self.clock() and lease.get('token_hash')):
            return lease
        return None

    def coordinator_guard(self, root_id, headers):
        lease = self.coordinator(root_id)
        token = headers.get('x-coordinator-token', '')
        if lease and not hmac.compare_digest(lease['token_hash'], digest_token(token)):
            raise APIError('conflict', 'work package has an exclusive coordinator', reason='coordinator_lease')

    def _coordinator_api(self, method, root_id, data, headers, identity):
        self.get('root', root_id)
        lease = self.coordinator(root_id)
        if method == 'GET':
            return response({k: lease[k] for k in ('holder_kind', 'holder_ref', 'expires_at', 'version')}) if lease else Response(200, [('Content-Type', 'application/json')], b'null')
        token = headers.get('x-coordinator-token', '')
        if method == 'DELETE':
            # Expired/released credentials never release a later holder's lease.
            if lease:
                self.coordinator_guard(root_id, headers)
                self._release_coordinator(root_id, lease, self.clock())
            return response({})
        if method != 'POST':
            raise APIError('not_found', 'route not found')
        request_id = require_uuid(data.get('request_id'), 'request_id')
        kind, ref, ttl = data.get('holder_kind'), data.get('holder_ref'), data.get('ttl_s')
        if kind not in ('interactive', 'managed') or not isinstance(ref, str) or not ref.strip():
            raise ValueError('holder_kind and holder_ref are required')
        if type(ttl) not in (int, float) or not math.isfinite(ttl) or ttl <= 0:
            raise ValueError('ttl_s must be finite and positive')
        takeover = data.get('takeover', False)
        if type(takeover) is not bool or (takeover and (not isinstance(data.get('reason'), str) or not data['reason'].strip())):
            raise ValueError('takeover requires a reason')
        key = root_id + ':' + request_id
        hashed = p.value_hash(data)
        receipt = self.store.get('coordinator_request', key)
        # Reconstruct acquisition receipts with the server secret, never store bearer plaintext.
        issued = hmac.new(self.admin_hash.encode(), ('coordinator:' + key + ':' + str(self.meta()['authority_generation'])).encode(), hashlib.sha256).hexdigest()
        if receipt:
            if receipt['request_hash'] != hashed:
                raise APIError('conflict', 'request_id reused with different content')
            if not lease or lease['token_hash'] != receipt['token_hash']:
                raise APIError('conflict', 'coordinator receipt is no longer current', reason='coordinator_lease')
            credential = token if receipt['renewal'] else issued
            if digest_token(credential) != receipt['token_hash']:
                raise APIError('conflict', 'coordinator credential required', reason='coordinator_lease')
            return response({'coordinator_token': credential, 'expires_at': receipt['expires_at']})
        owns = bool(lease and hmac.compare_digest(lease['token_hash'], digest_token(token)) and
                    lease['holder_kind'] == kind and lease['holder_ref'] == ref)
        if lease and not owns and not takeover:
            raise APIError('conflict', 'work package has an exclusive coordinator', reason='coordinator_lease')
        if takeover and identity[0] not in ('admin', 'session'):
            raise APIError('forbidden', 'administrator required for takeover')
        if lease and not owns:
            self.emit('coordinator.takeover', {'work_package_id': root_id, 'previous_holder': lease['holder_ref'],
                      'holder_ref': ref, 'reason': data['reason']}, self.clock())
        previous = self.store.get('coordinator', root_id, {})
        credential = token if owns else issued
        record = {'holder_kind': kind, 'holder_ref': ref, 'expires_at': d.timestamp(self.clock() + ttl),
                  'version': previous.get('version', 0) + 1, 'token_hash': digest_token(credential),
                  'authority_generation': self.meta()['authority_generation']}
        self.store.put('coordinator', root_id, record)
        self.store.put('coordinator_request', key, {'request_hash': hashed, 'token_hash': record['token_hash'],
                       'expires_at': record['expires_at'], 'renewal': owns})
        return response({'coordinator_token': credential, 'expires_at': record['expires_at']})

    def _release_coordinator(self, root_id, lease, now):
        self.store.put('coordinator', root_id, dict(lease, token_hash=None, expires_at=d.timestamp(now), version=lease['version'] + 1))
        self.emit('coordinator.released', {'work_package_id': root_id, 'holder_ref': lease['holder_ref']}, now)

    def _managed_lease(self, attempt, release=False, renew=False):
        coordinates = self.get('job', attempt['job_id'])['contract'].get('coordinates')
        if not coordinates:
            return
        root_id = coordinates['work_package_id']
        lease = self.store.get('coordinator', root_id)
        if (lease and lease['holder_kind'] == 'managed' and lease['holder_ref'] == attempt['attempt_id'] and
                lease.get('token_hash') == self.private(attempt).get('coordinator_hash')):
            if release:
                self._release_coordinator(root_id, lease, self.clock())
            elif renew and self.coordinator(root_id):
                lease['expires_at'] = attempt['lease_expires_at']
                lease['version'] += 1
                self.store.put('coordinator', root_id, lease)

    def _trigger_coordinator(self, job, trigger, now):
        if job['contract'].get('coordinates') or job['cancel']['requested_at']:
            return
        policy = job['contract'].get('coordination', {}).get('policy', 'always')
        attempt = self.store.get('attempt', job['current_attempt_id'])
        if policy == 'none':
            return
        if policy == 'on_failure':
            if not (job['status'] in ('waiting_reconcile', 'failed', 'waiting_capability') or
                    (job['status'] == 'completed' and attempt and attempt['recovered_from_attempt'])):
                return
        elif trigger not in ('completed', 'waiting_reconcile', 'rework'):
            return
        profile = d.managed_profile(self.config)
        lease = self.coordinator(job['work_package_id'])
        if profile['status'] != 'ready' or (lease and lease['holder_kind'] == 'interactive'):
            return
        key = '%s:%s:%s:%s' % (job['job_id'], trigger, job['durable_checkpoint_id'], job['acceptance']['status'])
        if self.store.get('coordinator_trigger', key):
            return
        if any(j['contract'].get('coordinates', {}).get('job_id') == job['job_id'] and
               j['status'] not in ('completed', 'failed', 'cancelled') for j in self.store.all('job')):
            return
        cp = self.store.get('checkpoint', job['durable_checkpoint_id'])
        context = {'job': job, 'report': attempt.get('report') if attempt else None,
                   'checkpoint_diff': p.manifest_diff(job['contract']['inputs_manifest'], self.manifest(cp['manifest_sha256'])) if cp else None,
                   'assessments': [a for a in self.store.all('assessment') if a['job_id'] == job['job_id']]}
        context.update(self._context_files(job))
        files = []
        for name, value in (('context.json', context), ('decision-schema.json', d.DECISION_SCHEMA)):
            raw = p.canonical_json(value)
            digest = p.sha256_bytes(raw)
            self.blobs.put(digest, raw)
            files.append({'path': name, 'sha256': digest, 'size': len(raw), 'executable': False})
        selected = profile['profile']
        contract = {'goal': 'Assess the target job and return a structured coordination decision.',
                    'harness': 'claude', 'harness_params': {k: selected[k] for k in ('model', 'effort')},
                    'coordinates': {'job_id': job['job_id'], 'work_package_id': job['work_package_id']},
                    'requirements': {'tools': ['claude']}, 'recovery': {'auto': False, 'side_effects': 'none'},
                    'inputs_manifest': {'version': 1, 'base': None, 'files': files, 'deleted': []},
                    'publish': {'mode': 'manual', 'requires_acceptance': False}}
        created = self._create_job({'request_id': new_id(), 'title': 'Coordinate: ' + job['title'], 'contract': contract},
                                   managed={'node_ids': selected['node_ids'], 'target_state': self._decision_state(job)}, schedule=False)
        ident = json.loads(created.body)['job_id']
        self.store.put('coordinator_trigger', key, {'job_id': ident})
        self.emit('coordinator.triggered', {'coordinator_job_id': ident, 'trigger': trigger, 'target_version': job['version']}, now, job=job)

    def _context_files(self, job):
        checkpoints = [cp for cp in self.store.all('checkpoint')
                       if cp['job_id'] == job['job_id'] and cp['state'] in ('durable', 'verified')]
        finals = [cp for cp in checkpoints if cp['kind'] == 'final']
        selected = max(finals or checkpoints,
                       key=lambda cp: self.get('checkpoint_private', cp['checkpoint_id'])['order'], default=None)
        files, omitted, total = [], [], 0
        patterns = job['contract'].get('workspace', {}).get('exclude', DEFAULT_EXCLUDES)
        for entry in sorted(self.manifest(selected['manifest_sha256'])['files'] if selected else [],
                            key=lambda entry: entry['path']):
            path, reason = entry['path'], None
            if workspace_excluded(path, patterns):
                reason = 'excluded'
            elif entry['size'] > 32768:
                reason = 'too_large'
            else:
                try:
                    content = self.blobs.read(entry['sha256']).decode('utf-8')
                except UnicodeDecodeError:
                    reason = 'binary'
                else:
                    if total + entry['size'] > 262144:
                        reason = 'budget'
                    else:
                        files.append({'path': path, 'content': content})
                        total += entry['size']
            if reason:
                omitted.append({'path': path, 'reason': reason})
        return {'files': files, 'files_omitted': omitted}

    @staticmethod
    def _decision_state(job):
        # Automatic publication can change delivery/version while Claude reads its immutable inputs.
        return {k: job[k] for k in ('status', 'current_epoch', 'durable_checkpoint_id', 'acceptance', 'cancel')}

    def _apply_coordinator(self, attempt, cp, now):
        coordinator_job = self.get('job', attempt['job_id'])
        coordinates = coordinator_job['contract']['coordinates']
        target = self.get('job', coordinates['job_id'])
        self.store.db.execute('SAVEPOINT decision')
        try:
            lease = self.coordinator(coordinates['work_package_id'])
            if not lease or lease['holder_kind'] != 'managed' or lease['holder_ref'] != attempt['attempt_id'] or lease['token_hash'] != self.private(attempt).get('coordinator_hash'):
                raise ValueError('managed coordination lease lost')
            state = self.get('job_private', coordinator_job['job_id'])['managed']['target_state']
            if self._decision_state(target) != state:
                raise ValueError('target changed since coordination inputs were captured')
            relatives = [j for j in self.store.all('job') if j['work_package_id'] == target['work_package_id']]
            if relatives[-1]['job_id'] != target['job_id']:
                raise ValueError('target has a newer revision')
            entry = next((f for f in self.manifest(cp['manifest_sha256'])['files'] if f['path'] == 'decision.json'), None)
            if entry is None:
                raise ValueError('final checkpoint lacks decision.json')
            decision = d.validate_decision(json.loads(self.blobs.read(entry['sha256'])), check_follow_up=False)
            assessment = decision['assessment']
            target_cp = None
            if decision['decision'] != 'escalate':
                if assessment['checkpoint_id'] != target['durable_checkpoint_id']:
                    raise ValueError('assessment must bind the captured target checkpoint')
                target_cp = self.get('checkpoint', assessment['checkpoint_id'])
                if target_cp['job_id'] != target['job_id'] or target_cp['state'] not in ('durable', 'verified'):
                    raise ValueError('assessment checkpoint is not durable for target')
            if decision['decision'] == 'escalate':
                self.emit('coordinator.escalated', {'notes': decision['notes'], 'reason': 'decision',
                          'coordinator_attempt_id': attempt['attempt_id']}, now, job=target)
            else:
                self._job_action(target, 'assess', dict(assessment, expected_version=target['version'],
                    assessment_id=new_id(), reviewer='managed:' + attempt['attempt_id']), trigger_coordinator=False)
                self.emit('coordinator.decision_applied', {'decision': decision['decision'], 'notes': decision['notes'],
                          'coordinator_attempt_id': attempt['attempt_id']}, now, job=target)
            # Assessment is authoritative even if a proposed repair cannot be admitted.
            self._apply_follow_up(self.get('job', target['job_id']), target_cp, decision, attempt, now)
        except (APIError, ValueError, TypeError, KeyError, OverflowError) as exc:
            self.store.db.execute('ROLLBACK TO decision')
            self.emit('coordinator.decision_rejected', {'reason': str(exc), 'coordinator_attempt_id': attempt['attempt_id']}, now, job=target)
        finally:
            self.store.db.execute('RELEASE decision')

    def _apply_follow_up(self, target, checkpoint, decision, attempt, now):
        self.store.db.execute('SAVEPOINT follow_up')
        event_type = 'coordinator.follow_up_rejected'
        try:
            follow = decision['follow_up']
            if follow is None:
                return
            if decision['decision'] != 'rework':
                raise ValueError('follow_up requires rework')
            if target['cancel']['requested_at']:
                raise ValueError('cancel_requested')
            contract = d.repair_contract(target['contract'], follow, self.manifest(checkpoint['manifest_sha256']))
            root = self.root(target)
            reason = d.budget_reason(dict(target, contract=contract), root['counters'], now, root['created_at'], repair=True)
            if any(v in ('unknown', 'intent') for v in self.store.get('effects', target['work_package_id'], {}).values()):
                reason = 'side_effect_unknown'
            if reason:
                event_type = 'coordinator.escalated'
                raise ValueError(reason)
            if target['status'] == 'waiting_reconcile':
                self.change('job', target, now, status='failed', status_reason='managed_rework')
            self._create_job({'request_id': new_id(), 'title': follow['title'],
                              'work_package_id': target['work_package_id'], 'contract': contract},
                             input_checkpoint=checkpoint)
        except (APIError, ValueError, TypeError, KeyError, OverflowError) as exc:
            self.store.db.execute('ROLLBACK TO follow_up')
            if isinstance(exc, APIError) and exc.code == 'budget_exhausted':
                event_type = 'coordinator.escalated'
            self.emit(event_type, {'reason': str(exc), 'notes': decision['notes'],
                      'coordinator_attempt_id': attempt['attempt_id']}, now, job=target)
        finally:
            self.store.db.execute('RELEASE follow_up')

    def _counters(self, job, now, **deltas):
        root = self.root(job)
        for key, value in deltas.items():
            root['counters'][key] += value
        self.store.put('root', job['work_package_id'], root)
        for relative in self.store.all('job'):
            if relative['work_package_id'] == job['work_package_id']:
                self.change('job', relative, now, counters=dict(root['counters']))

    def _refresh_blobs(self, now):
        for job in self.store.all('job'):
            if job['protection'] == 'preparing':
                manifest = job['contract']['inputs_manifest']
                missing = self.blobs.missing(manifest) if manifest is not None else ['inputs_manifest']
                if not missing:
                    self.change('job', job, now, protection='protected', status_reason='')
        for cp in self.store.all('checkpoint'):
            if cp['state'] != 'uploading':
                continue
            missing = self.blobs.missing(self.manifest(cp['manifest_sha256']))
            if missing != cp['missing_blobs']:
                cp['missing_blobs'] = missing
                self.store.put('checkpoint', cp['checkpoint_id'], cp)
                self.emit('checkpoint.uploading', cp, now, job=self.get('job', cp['job_id']))
            attempt = self.get('attempt', cp['attempt_id'])
            if not missing and self.fence_reason(attempt, {'epoch': cp['epoch']}, client=True) is None:
                self._make_durable(cp, now)

    def _make_durable(self, cp, now):
        cp.update(state='durable', missing_blobs=[], durable_at=d.timestamp(now))
        self.store.put('checkpoint', cp['checkpoint_id'], cp)
        job = self.get('job', cp['job_id'])
        # Uploads may finish out of order: an older checkpoint must not replace a newer one.
        order = self.get('checkpoint_private', cp['checkpoint_id'])['order']
        previous = self.store.get('checkpoint_private', job['durable_checkpoint_id']) if job['durable_checkpoint_id'] else None
        if previous is None or order > previous['order']:
            self.change('job', job, now, durable_checkpoint_id=cp['checkpoint_id'], durable_checkpoint_at=cp['durable_at'])
        self.emit('checkpoint.durable', cp, now, job=job, attempt=self.get('attempt', cp['attempt_id']))

    def _expire(self, now):
        for node in self.store.all('node'):
            self.change('node', node, now, status=d.node_status(node, now, self.config))
        for attempt in self.store.all('attempt'):
            if attempt['status'] not in p.ATTEMPT_ACTIVE:
                continue
            private = self.private(attempt)
            expires = private['claim_until'] if attempt['status'] == 'assigned' else d.seconds(attempt['lease_expires_at'])
            if expires <= now:
                self._failure(attempt, 'node_lost', 'lease or claim deadline expired', now, 'lost')

    def _failure(self, attempt, failure, detail, now, status='failed'):
        self._managed_lease(attempt, release=True)
        attempt.update(status=status, failure_class=failure, failure_detail=detail, ended_at=d.timestamp(now))
        self.store.put('attempt', attempt['attempt_id'], attempt)
        private = self.private(attempt)
        private['revoked'] = True
        self.store.put('attempt_private', attempt['attempt_id'], private)
        job = self.get('job', attempt['job_id'])
        self._record_capability(attempt, job, now)
        node = self.get('node', attempt['node_id'])
        harness = job['contract']['harness']
        ref = node['provider_account_refs'].get(harness)
        if failure == 'quota':
            if ref:
                self.store.put('quota', harness + ':' + ref, {'key': harness + ':' + ref, 'until': now + self.config['quota_block_s']})
            else:
                # No account identity: hold the failed job for explicit reconciliation.
                detail = 'quota_account_unknown'
        elif failure == 'auth_expired':
            for peer in self.store.all('node'):
                if peer['node_id'] == node['node_id'] or (ref and peer['provider_account_refs'].get(harness) == ref):
                    health = dict(peer['auth_health'], **{harness: 'expired'})
                    self.change('node', peer, now, auth_health=health)
        root = self.root(job)
        effects = self.store.get('effects', job['work_package_id'], {})
        decision = d.recovery_decision(job, attempt, root['counters'], now, root['created_at'], effects)
        job = self.change('job', job, now, status=decision['status'], status_reason=decision['reason'])
        jp = self.get('job_private', job['job_id'])
        jp.update(recovery_from=attempt['attempt_id'], pending=decision['recover'], explicit=False)
        # v1 has no reconciliation endpoint for auth/capability/quota; explicit retry
        # after the external condition is fixed avoids silently moving accounts.
        self.store.put('job_private', job['job_id'], jp)
        self.emit('attempt.' + status, attempt, now, job=job, attempt=attempt)
        self._trigger_coordinator(job, job['status'], now)

    def _record_capability(self, attempt, job, now, checkpoint=None):
        failure = attempt.get('failure_class')
        if attempt['status'] == 'cancelled' or failure in ('user_cancel', 'node_lost'):
            return
        if attempt['status'] == 'succeeded':
            failure = 'checks_failed' if d.evidence_gaps(job['contract'], attempt.get('report'),
                                                        checkpoint['manifest_sha256']) else None
        node = self.get('node', attempt['node_id'])
        harness = job['contract']['harness']
        bound = self.private(attempt).get('capability_bound')
        if bound is None:
            # Legacy in-flight attempts did not capture the executable path.
            bound = dict(d.capability_bound(node, harness), harness_path=None)
        bound = dict(bound, **{key: attempt['runtime'].get(key) for key in ('agent_version', 'harness_version')})
        evidence = d.capability_evidence(node)
        entry = evidence.get(harness)
        if entry is None or entry['bound'] != bound:
            entry = {'bound': bound, 'last_success': None, 'last_failure': None}
        entry['status'] = 'degraded' if failure else 'ok'
        entry['last_failure' if failure else 'last_success'] = dict(
            {'attempt_id': attempt['attempt_id'], 'at': attempt['ended_at']}, **({'reason': failure} if failure else {}))
        if bound != d.capability_bound(node, harness):
            entry['status'] = 'unproven'
        evidence[harness] = entry
        self.change('node', node, now, capability_evidence=evidence)

    def explanation(self, job, now):
        private = self.get('job_private', job['job_id'])
        excluded = list(private['excluded'])
        if private.get('managed'):
            excluded.extend(n['node_id'] for n in self.store.all('node') if n['node_id'] not in private['managed']['node_ids'])
        old = self.store.get('attempt', private['recovery_from']) if private['recovery_from'] else None
        source = self.store.get('node', old['node_id']) if old else None
        return d.explain(job, self.store.all('node'), self.store.all('attempt'),
                         {j['job_id']: j for j in self.store.all('job')}, now, self.config,
                         paused=self.meta()['dispatch_paused'],
                         quota={q['key']: q['until'] for q in self.store.all('quota')},
                         excluded=excluded, source_node=source, recovering=bool(old))

    def _schedule(self, now, only_job_id=None):
        self._refresh_blobs(now)
        for job in self.store.all('job'):
            if only_job_id is not None and job['job_id'] != only_job_id:
                continue
            private = self.get('job_private', job['job_id'])
            if not private['pending'] or job['status'] not in ('queued', 'waiting_capability', 'recovering'):
                continue
            coordinates = job['contract'].get('coordinates')
            if coordinates and self.coordinator(coordinates['work_package_id']):
                continue
            current = self.store.get('attempt', job['current_attempt_id']) if job['current_attempt_id'] else None
            if current and current['status'] in p.ATTEMPT_ACTIVE:
                continue
            root = self.root(job)
            exhausted = d.budget_reason(job, root['counters'], now, root['created_at'])
            if exhausted:
                job = self.change('job', job, now, status='failed', status_reason=exhausted)
                private['pending'] = False
                self.store.put('job_private', job['job_id'], private)
                self._trigger_coordinator(job, 'failed', now)
                continue
            explanation = self.explanation(job, now)
            selected = explanation['selected_node_id']
            if selected is None:
                reasons = sorted({r for n in explanation['nodes'] for r in n['reasons']})
                reason = ','.join(reasons) or 'no_registered_nodes'
                if job['protection'] == 'preparing':
                    manifest = job['contract']['inputs_manifest']
                    missing = self.blobs.missing(manifest) if manifest is not None else ['inputs_manifest']
                    reason += ';missing_blobs:' + ','.join(missing)
                changed = job['status'] != 'waiting_capability'
                job = self.change('job', job, now, status='waiting_capability', status_reason=reason)
                if changed:
                    self._trigger_coordinator(job, 'waiting_capability', now)
                continue
            old = self.store.get('attempt', private['recovery_from']) if private['recovery_from'] else None
            migration = bool(private['explicit'] or (old and old['node_id'] != selected))
            root = self.root(job)
            exhausted = d.budget_reason(job, root['counters'], now, root['created_at'], migration=migration)
            if exhausted:
                job = self.change('job', job, now, status='failed', status_reason=exhausted)
                private['pending'] = False
                self.store.put('job_private', job['job_id'], private)
                self._trigger_coordinator(job, 'failed', now)
                continue
            self._counters(job, now, attempts=1, infra_retries=int(bool(old and not private['explicit'])), migrations=int(migration))
            job = self.get('job', job['job_id'])
            cp = self.store.get('checkpoint', job['durable_checkpoint_id']) if job['durable_checkpoint_id'] else None
            node = self.get('node', selected)
            ident = new_id()
            attempt = {
                'attempt_id': ident, 'job_id': job['job_id'], 'epoch': job['current_epoch'] + 1,
                'node_id': selected, 'status': 'assigned', 'failure_class': None, 'failure_detail': None,
                'recovered_from_attempt': old['attempt_id'] if old else None,
                'start_checkpoint_id': cp['checkpoint_id'] if cp else None,
                'start_manifest_sha256': cp['manifest_sha256'] if cp else job['inputs_manifest_sha256'],
                'lease_expires_at': None, 'authority_generation': self.meta()['authority_generation'],
                'runtime': {'agent_version': node['agent_version'], 'agent_commit': node['capabilities'].get('agent', {}).get('git_commit'),
                            'harness': job['contract']['harness'],
                            'harness_version': node['capabilities']['harness'].get(job['contract']['harness']),
                            'model': job['contract']['harness_params'].get('model'),
                            'effort': job['contract']['harness_params'].get('effort'), 'observed': None},
                'native_session_ref': None, 'workspace': None, 'report': None,
                'created_at': d.timestamp(now), 'leased_at': None, 'started_at': None, 'ended_at': None,
            }
            self.store.put('attempt', ident, attempt)
            self.store.put('attempt_private', ident, {'token_hash': None, 'revoked': False,
                           'claim_until': now + self.config['assign_claim_timeout_s'], 'complete_hash': None, 'complete_response': None})
            private['pending'] = False
            self.store.put('job_private', job['job_id'], private)
            self.change('job', job, now, current_attempt_id=ident, current_epoch=attempt['epoch'],
                        status='recovering' if old else 'queued', status_reason='assigned:' + selected)
            self.emit('attempt.assigned', attempt, now, job=job, attempt=attempt)

    def tick(self):
        with self.store.transaction():
            now = self.clock()
            self._expire(now)
            self._schedule(now)

    def fence_reason(self, attempt, data, client=False):
        meta = self.meta()
        if (attempt['authority_generation'] != meta['authority_generation'] or
                type(data.get('authority_generation', meta['authority_generation'])) is not int or
                data.get('authority_generation', meta['authority_generation']) != meta['authority_generation']):
            return 'stale_generation'
        job = self.get('job', attempt['job_id'])
        if type(data.get('epoch')) is not int or data.get('epoch') != job['current_epoch'] or attempt['attempt_id'] != job['current_attempt_id']:
            return 'stale_epoch'
        private = self.private(attempt)
        if private['revoked']:
            return 'revoked'
        if attempt['status'] == 'assigned' or (attempt['status'] in ('leased', 'running') and d.seconds(attempt['lease_expires_at']) <= self.clock()):
            return 'lease_expired'
        if not client:
            token = data.get('lease_token')
            if not isinstance(token, str) or not private['token_hash'] or not hmac.compare_digest(digest_token(token), private['token_hash']):
                return 'invalid_lease_token'
        return None

    def fence(self, attempt, data, client=False):
        reason = self.fence_reason(attempt, data, client)
        if reason:
            raise APIError('fenced', 'attempt no longer owns execution', reason=reason)

    def _register(self, data, identity):
        self.node(identity, data['node_id'])
        p.check_node_id(data['node_id'])
        required = {'node_id', 'physical_host_id', 'failure_domain', 'capabilities', 'runtime_profile',
                    'resources', 'provider_account_refs', 'auth_health', 'agent_version'}
        if set(data) != required:
            raise ValueError('register fields must match node contract')
        for key in ('physical_host_id', 'failure_domain', 'agent_version'):
            if not isinstance(data[key], str) or not data[key]:
                raise ValueError('invalid node identity')
        cap = data['capabilities']
        required_cap = {'os', 'arch', 'tools', 'gpu', 'harness', 'labels'}
        if (not isinstance(cap, dict) or not required_cap <= set(cap) or
                set(cap) - required_cap - {'harness_paths', 'harness_disabled', 'agent'} or
                cap['os'] not in p.OS_NAMES or cap['arch'] not in ('x86_64', 'arm64')):
            raise ValueError('invalid capabilities')
        for key in ('tools', 'harness', 'harness_paths', 'harness_disabled'):
            if key not in cap:
                continue
            if not isinstance(cap[key], dict) or any(not isinstance(k, str) or (v is not None and not isinstance(v, str)) for k, v in cap[key].items()):
                raise ValueError('invalid tool/harness map')
        if 'agent' in cap and (not isinstance(cap['agent'], dict) or set(cap['agent']) != {'version', 'git_commit'} or
                not isinstance(cap['agent']['version'], str) or
                (cap['agent']['git_commit'] is not None and not isinstance(cap['agent']['git_commit'], str))):
            raise ValueError('invalid agent version identity')
        if not isinstance(cap['gpu'], list) or any(not isinstance(g, (str, dict)) for g in cap['gpu']):
            raise ValueError('gpu must be a list')
        d.string_list(cap['labels'], 'labels')
        resources = data['resources']
        if set(resources) - {'slots', 'host_slots', 'mem_mb'} or any(type(resources.get(k)) is not int or resources[k] < 1 for k in ('slots', 'host_slots')):
            raise ValueError('invalid resources')
        resources.setdefault('mem_mb', None)
        if resources['mem_mb'] is not None and (type(resources['mem_mb']) is not int or resources['mem_mb'] < 0):
            raise ValueError('invalid mem_mb')
        if not isinstance(data['runtime_profile'], dict):
            raise ValueError('invalid runtime_profile')
        self._health(data['auth_health'])
        if not isinstance(data['provider_account_refs'], dict) or any(not isinstance(v, str) or not v for v in data['provider_account_refs'].values()):
            raise ValueError('invalid provider_account_refs')
        old = self.store.get('node', data['node_id'])
        now = self.clock()
        peers = [n for n in self.store.all('node') if n['physical_host_id'] == data['physical_host_id'] and n['node_id'] != data['node_id']]
        host_ids = {n['node_id'] for n in peers} | {data['node_id']}
        used = sum(self.get('job', a['job_id'])['contract']['resources']['slots'] for a in self.store.all('attempt')
                   if a['node_id'] in host_ids and a['status'] in p.ATTEMPT_ACTIVE)
        if used > min([resources['host_slots']] + [n['resources']['host_slots'] for n in peers]):
            raise APIError('conflict', 'host capacity cannot drop below active reservations')
        if old:
            active = [a for a in self.store.all('attempt') if a['node_id'] == old['node_id'] and a['status'] in p.ATTEMPT_ACTIVE]
            if active and any(old[k] != data[k] for k in ('physical_host_id', 'failure_domain', 'resources')):
                raise APIError('conflict', 'cannot change host/resources with active attempts')
            data = dict(data, auth_health=merge_health(old['auth_health'], data['auth_health']))
            data['capability_evidence'] = d.capability_evidence(dict(old, **data))
            node = self.change('node', old, now, **dict(data, last_seen_at=d.timestamp(now), status='online'))
        else:
            node = dict(data, status='online', drain=False, last_seen_at=d.timestamp(now), version=1)
            node['capability_evidence'] = d.capability_evidence(node)
            self.store.put('node', node['node_id'], node)
            self.emit('node.registered', node, now, node=node['node_id'])
        return response({'node': node, 'authority_generation': self.meta()['authority_generation'],
                         'config': {k: self.config[k] for k in ('heartbeat_s', 'lease_ttl_s')}})

    @staticmethod
    def _health(value):
        if not isinstance(value, dict) or any(x not in ('ok', 'expired', 'unknown') for x in value.values()):
            raise ValueError('invalid auth_health')

    def _heartbeat(self, node_id, data, identity):
        self.node(identity, node_id)
        node = self.get('node', node_id)
        now = self.clock()
        self._health(data.get('auth_health', {}))
        if type(data.get('authority_generation')) is not int or not isinstance(data.get('attempts'), list):
            raise ValueError('invalid heartbeat')
        reconcile = data['authority_generation'] != self.meta()['authority_generation']
        node = self.change('node', node, now, status='online', last_seen_at=d.timestamp(now),
                           auth_health=merge_health(node['auth_health'], data.get('auth_health', {})))
        renewed, revoked, cancel = [], [], []
        seen = set()
        for entry in data['attempts']:
            require_uuid(entry['attempt_id'], 'attempt_id')
            if type(entry.get('epoch')) is not int or entry.get('state') not in ('running', 'finished_unreported', 'unknown') or entry['attempt_id'] in seen:
                raise ValueError('invalid heartbeat attempt')
            seen.add(entry['attempt_id'])
            attempt = self.store.get('attempt', entry['attempt_id'])
            reason = 'stale_generation' if reconcile else 'unknown_attempt'
            if attempt and attempt['node_id'] == node_id and not reconcile:
                reason = self.fence_reason(attempt, entry, client=True)
                if not reason and attempt['status'] in ('leased', 'running'):
                    if entry['state'] == 'unknown':
                        # 持有节点自己声明无法继续（如 agent 重启后旧进程已不在）：立即判失联并走恢复决策，
                        # 不必等租约过期。旧 attempt 的令牌随之失效，发布仍受 epoch 门禁保护。
                        reason = 'unknown_state'
                        self._failure(attempt, 'node_lost', 'owner reported unknown state', now, 'lost')
                    else:
                        # 续租只更新租约字段，不写事件：每次心跳一条事件会让事件表与 SSE 持续膨胀。
                        attempt['lease_expires_at'] = d.timestamp(now + self.config['lease_ttl_s'])
                        self.store.put('attempt', attempt['attempt_id'], attempt)
                        self._managed_lease(attempt, renew=True)
                        renewed.append(attempt['attempt_id'])
                        if self.get('job', attempt['job_id'])['cancel']['requested_at']:
                            cancel.append(attempt['attempt_id'])
            if reason:
                revoked.append({'attempt_id': entry['attempt_id'], 'reason': reason})
        self.store.put('node_private', node_id, {'reconcile_required': reconcile})
        if not reconcile:
            self._schedule(now)
        return response({'server_time': d.timestamp(now), 'drain': node['drain'],
                         'dispatch_paused': self.meta()['dispatch_paused'], 'renewed': renewed,
                         'revoked': revoked, 'cancel': cancel, 'reconcile_required': reconcile,
                         'integrations': [r['pending'] for r in self.store.all('integration_target')
                                          if r['target_node_id'] == node_id and r.get('pending')] if not reconcile else []})

    def _claim(self, node_id, identity):
        self.node(identity, node_id)
        node = self.get('node', node_id)
        if self.store.get('node_private', node_id, {}).get('reconcile_required'):
            raise APIError('fenced', 'heartbeat reconciliation required', reason='stale_generation')
        now = self.clock()
        self._schedule(now)
        if self.meta()['dispatch_paused'] or node['drain'] or d.node_status(node, now, self.config) != 'online':
            return response(status=204)
        assigned = []
        for candidate in self.store.all('attempt'):
            if candidate['node_id'] != node_id or candidate['status'] != 'assigned':
                continue
            cj = self.get('job', candidate['job_id'])
            managed = self.get('job_private', cj['job_id']).get('managed')
            coordinates = cj['contract'].get('coordinates')
            if coordinates and (not managed or node_id not in managed['node_ids'] or self.coordinator(coordinates['work_package_id'])):
                continue
            assigned.append(candidate)
        if not assigned:
            return response(status=204)
        attempt = assigned[0]
        job = self.get('job', attempt['job_id'])
        # Recheck hard capabilities after assignment (registration or quota may have changed).
        others = [a for a in self.store.all('attempt') if a['attempt_id'] != attempt['attempt_id']]
        check = d.explain(job, self.store.all('node'), others, {j['job_id']: j for j in self.store.all('job')},
                          now, self.config, quota={q['key']: q['until'] for q in self.store.all('quota')})
        if not next(row for row in check['nodes'] if row['node_id'] == node_id)['eligible']:
            return response(status=204)
        # Prefix survives rollback of the attempt row; the entire opaque token is hashed.
        token = str(self.meta()['authority_generation']) + '.' + secrets.token_urlsafe(32)
        private = self.private(attempt)
        private['token_hash'] = digest_token(token)
        private['capability_bound'] = d.capability_bound(node, job['contract']['harness'])
        self.store.put('attempt_private', attempt['attempt_id'], private)
        attempt.update(status='leased', leased_at=d.timestamp(now), lease_expires_at=d.timestamp(now + self.config['lease_ttl_s']))
        attempt['runtime'].update(agent_version=node['agent_version'],
                                  agent_commit=node['capabilities'].get('agent', {}).get('git_commit'),
                                  harness_version=node['capabilities']['harness'].get(job['contract']['harness']))
        self.store.put('attempt', attempt['attempt_id'], attempt)
        coordinates = job['contract'].get('coordinates')
        if coordinates:
            root_id = coordinates['work_package_id']
            old = self.store.get('coordinator', root_id, {})
            hashed = digest_token(secrets.token_urlsafe(32))
            private['coordinator_hash'] = hashed
            self.store.put('attempt_private', attempt['attempt_id'], private)
            self.store.put('coordinator', root_id, {'holder_kind': 'managed', 'holder_ref': attempt['attempt_id'],
                           'expires_at': attempt['lease_expires_at'], 'version': old.get('version', 0) + 1,
                           'token_hash': hashed, 'authority_generation': self.meta()['authority_generation']})
        self.change('job', job, now, status='running', status_reason='')
        self.emit('attempt.leased', attempt, now, job=job, attempt=attempt)
        cp = self.store.get('checkpoint', attempt['start_checkpoint_id']) if attempt['start_checkpoint_id'] else None
        assignment = {k: attempt[k] for k in ('attempt_id', 'job_id', 'epoch', 'lease_expires_at', 'authority_generation', 'recovered_from_attempt')}
        assignment.update(lease_token=token, work_package_id=job['work_package_id'], contract=job['contract'],
                          delivery_version=job['delivery_version'], native_session_ref=None,
                          start={'kind': 'checkpoint' if cp else ('inputs' if job['inputs_manifest_sha256'] else 'empty'),
                                 'manifest_sha256': attempt['start_manifest_sha256'],
                                 'checkpoint_id': attempt['start_checkpoint_id'], 'summary': cp['summary'] if cp else None})
        return response({'assignment': assignment})

    def route(self, method, route, query, headers, body, data, identity):
        pieces = route.strip('/').split('/')
        now = self.clock()
        if route == '/session' and method == 'GET':
            # 页面刷新后取回 CSRF；只对浏览器会话开放，跨源页面无法读取响应（未开启 CORS）。
            if identity[0] != 'session':
                raise APIError('forbidden', 'browser session required')
            session = self.store.get('session', identity[1])
            return response({'csrf_token': self.csrf_for(identity[1]), 'expires_at': d.timestamp(session['expires'])})
        if route == '/session' and method == 'DELETE':
            self.client(identity)
            if identity[0] == 'session':
                self.store.delete('session', identity[1])
            return response({}, headers=[('Set-Cookie', 'ac_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict')])
        if pieces[0] == 'blobs' and len(pieces) == 2:
            digest = p.check_sha256(pieces[1])
            if method == 'PUT':
                created = self.blobs.put(digest, body)
                self._refresh_blobs(now)
                self._schedule(now)
                return response({'sha256': digest}, 201 if created else 200)
            if method == 'GET':
                if not self.blobs.exists(digest):
                    raise APIError('not_found', 'blob not found')
                return Response(200, [('Content-Type', 'application/octet-stream')], self.blobs.read(digest))
        if route == '/nodes/register' and method == 'POST':
            return self._register(data, identity)
        if pieces[0] == 'nodes' and len(pieces) == 3 and method == 'POST':
            if pieces[2] == 'heartbeat':
                return self._heartbeat(pieces[1], data, identity)
            if pieces[2] == 'claim':
                return self._claim(pieces[1], identity)
        if pieces[0] == 'attempts' and len(pieces) == 3 and method == 'POST':
            if pieces[2] == 'events' and self.store.get('attempt', pieces[1]) is None:
                require_uuid(pieces[1], 'attempt_id')
                # A restored snapshot may predate this entire attempt. Archive only;
                # there is no authority row whose state this node can change.
                # 只有恢复过备份（generation > 1）才可能出现这种 attempt；且每个节点的归档事件有上限，
                # 防止持有节点令牌者向任意不存在的 attempt 无限写入。
                if identity[0] != 'node' or self.meta()['authority_generation'] <= 1:
                    raise APIError('not_found', 'attempt not found')
                attempt = {'attempt_id': pieces[1], 'job_id': None, 'node_id': identity[1]}
            else:
                attempt = self.get_attempt(pieces[1], data)
            self.node(identity, attempt['node_id'])
            tokens = (headers.get('authorization', '')[7:], data.get('lease_token', ''))
            if pieces[2] == 'events':
                return self._events(attempt, data, tokens)
            if pieces[2] == 'checkpoints':
                return self._checkpoint(attempt, data, tokens)
            if pieces[2] == 'complete':
                return self._complete(attempt, data, tokens)
        if pieces[0] == 'jobs' and len(pieces) == 3 and method == 'POST' and pieces[2] in ('publish', 'integration'):
            if pieces[2] == 'publish':
                attempt = self.get_attempt(data['attempt_id'], data)
                job = self.get('job', pieces[1])
                if attempt['job_id'] != job['job_id']:
                    raise APIError('conflict', 'attempt belongs to another job')
                if identity[0] == 'node':
                    self.node(identity, attempt['node_id'])
                else:
                    self.client(identity)
                if identity[0] != 'node' or job['contract']['publish']['mode'] != 'auto':
                    self.coordinator_guard(job['work_package_id'], headers)
                self.fence(attempt, data, client=identity[0] != 'node')
                return self.idempotent(route, data, lambda: self._publish(job, attempt, data, identity))
            job = self.get('job', pieces[1])
            if identity[0] == 'node':
                self.node(identity, job['contract']['integration']['target_node_id'])
                if job['contract']['integration']['target_node_id'] is None:
                    raise APIError('forbidden', 'no integration target node configured')
            else:
                self.client(identity)
            self.coordinator_guard(job['work_package_id'], headers)
            return self.idempotent(route, data, lambda: self._integration(job, data, identity))
        self.client(identity)
        if pieces[0] == 'packages' and len(pieces) == 3 and pieces[2] == 'coordinator':
            return self._coordinator_api(method, require_uuid(pieces[1], 'work_package_id'), data, headers, identity)
        if route == '/jobs' and method == 'POST':
            if data.get('work_package_id'):
                self.coordinator_guard(data['work_package_id'], headers)
            return self.idempotent(route, data, lambda: self._create_job(data), creation=True)
        if route == '/jobs' and method == 'GET':
            jobs = self.store.all('job')
            status = query.get('status', [''])[0]
            before = query.get('before', [''])[0]
            if status and status not in p.JOB_STATUSES:
                raise ValueError('invalid status')
            jobs.reverse()
            # 先在全部 job 中定位游标再按状态筛选：游标所指的 job 换了状态，分页仍然有效。
            if before:
                ids = [j['job_id'] for j in jobs]
                if before not in ids:
                    raise ValueError('before must be an existing job_id')
                jobs = jobs[ids.index(before) + 1:]
            if status:
                jobs = [j for j in jobs if j['status'] == status]
            return response({'jobs': jobs[:self.limit(query)]})
        if pieces[0] == 'jobs' and len(pieces) in (2, 3):
            job = self.get('job', pieces[1])
            if len(pieces) == 2 and method == 'GET':
                result = dict(job)
                for kind in ('attempt', 'checkpoint', 'publication', 'assessment'):
                    result[kind + 's'] = [v for v in self.store.all(kind) if v['job_id'] == job['job_id']]
                result['events'] = self.store.events(job_id=job['job_id'])
                return response(result)
            if len(pieces) == 3 and pieces[2] == 'explain' and method == 'GET':
                return response(self.explanation(job, now))
            if len(pieces) == 3 and method == 'POST' and pieces[2] in ('cancel', 'retry', 'assess'):
                if pieces[2] != 'cancel':
                    self.coordinator_guard(job['work_package_id'], headers)
                return self.idempotent(route, data, lambda: self._job_action(job, pieces[2], data))
        if pieces[0] == 'checkpoints' and len(pieces) in (2, 3) and method == 'GET':
            cp = self.get('checkpoint', pieces[1])
            if len(pieces) == 2:
                return response(cp)
            if pieces[2] == 'diff':
                against = query.get('against', ['inputs'])[0]
                job = self.get('job', cp['job_id'])
                old = job['contract']['inputs_manifest']
                if against != 'inputs':
                    other = self.get('checkpoint', against)
                    if other['job_id'] != cp['job_id']:
                        raise APIError('conflict', 'diff requires checkpoints of the same job')
                    old = self.manifest(other['manifest_sha256'])
                return response(p.manifest_diff(old, self.manifest(cp['manifest_sha256'])))
        if route == '/nodes' and method == 'GET':
            nodes = []
            jobs = [j for j in self.store.all('job') if j['status'] in ('queued', 'recovering', 'waiting_capability')]
            for node in self.store.all('node'):
                attempts = [a for a in self.store.all('attempt') if a['node_id'] == node['node_id'] and a['status'] in p.ATTEMPT_ACTIVE]
                reasons = []
                for job in jobs:
                    reasons = next(row['reasons'] for row in self.explanation(job, now)['nodes'] if row['node_id'] == node['node_id'])
                nodes.append(dict(node, active_attempts=len(attempts), last_rejection_reasons=reasons))
            return response({'nodes': nodes})
        if pieces[0] == 'nodes' and len(pieces) == 3 and pieces[2] == 'drain' and method == 'POST':
            return self.idempotent(route, data, lambda: self._drain(pieces[1], data, headers))
        if route == '/dispatch' and method == 'POST':
            return self.idempotent(route, data, lambda: self._dispatch(data))
        if route == '/overview' and method == 'GET':
            from .backup import summary as backup_summary
            jobs = self.store.all('job')
            return response({'counts': {s: sum(j['status'] == s for j in jobs) for s in p.JOB_STATUSES},
                             'nodes': self.store.all('node'), 'durable_checkpoint_at': max((j['durable_checkpoint_at'] for j in jobs if j['durable_checkpoint_at']), default=None),
                             'controller': dict(self.meta(), version=RELEASE['version'], git_commit=RELEASE['git_commit'],
                                                metadata_version=self.meta()['version'],
                                                deployment_profile=self.config['deployment_profile'], guarantees=self.config['guarantees']),
                             'dispatch_paused': self.meta()['dispatch_paused'],
                             'backup': backup_summary(self.data_dir.parent / 'backups', now),
                             'managed_coordinator': d.managed_profile(self.config)})
        if route == '/events' and method == 'GET':
            after = self.cursor(query.get('after', ['0'])[0])
            return response({'events': self.store.events(after, self.limit(query)), 'min_seq': self.min_seq()})
        if route == '/events/stream' and method == 'GET':
            cursor = self.cursor(query.get('cursor', [headers.get('last-event-id', '0')])[0])
            return Response(200, [('Content-Type', 'text/event-stream'), ('Cache-Control', 'no-cache'),
                                  ('X-Accel-Buffering', 'no')], stream=iter(self.sse_batch(cursor)))
        if route == '/doctor' and method == 'GET':
            from .backup import summary as backup_summary
            self.store.put('doctor', 'probe', {'at': d.timestamp(now)})
            self.store.delete('doctor', 'probe')
            backup = self.store.get('backup', 'latest')
            backup_state = backup_summary(self.data_dir.parent / 'backups', now)
            return response({'database': {'writable': True, 'journal_mode': self.store.db.execute('PRAGMA journal_mode').fetchone()[0]},
                             'blob_directory': str(self.blobs.root), 'blob_writable': os.access(str(self.blobs.root), os.W_OK),
                             'free_bytes': shutil.disk_usage(str(self.data_dir)).free, 'clock': d.timestamp(now),
                             'public_origin': self.config['public_origin'], 'tls': self.config['public_origin'].startswith('https:'),
                             'listener': getattr(self, 'listener', 'in_process'),
                             'backup': backup_state,
                             'backup_age_s': (backup_state['local_age_s'] if (self.data_dir.parent / 'backups' / 'status.json').exists()
                                              else max(0, now - backup['time']) if backup else 'unknown')})
        raise APIError('not_found', 'route not found')

    @staticmethod
    def cursor(value):
        result = int(value)
        if result < 0:
            raise ValueError('cursor must be nonnegative')
        return result

    def limit(self, query):
        value = int(query.get('limit', ['200'])[0])
        if not 1 <= value <= 1000:
            raise ValueError('limit must be between 1 and 1000')
        return value

    def min_seq(self):
        minimum = self.store.db.execute('SELECT MIN(seq) FROM events').fetchone()[0]
        if minimum is not None:
            return minimum
        last = self.store.db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
        return (last[0] + 1) if last else 1

    def sse_batch(self, cursor):
        minimum = self.min_seq()
        # Cursor zero on an unpruned stream means 'from the beginning'.
        if minimum > 1 and cursor < minimum:
            return [b'event: snapshot_required\ndata: ' + p.canonical_json({'min_seq': minimum}) + b'\n\n']
        events = self.store.events(cursor, 1000)
        return [('id: %d\nevent: %s\ndata: ' % (e['seq'], e['type'])).encode() + p.canonical_json(e) + b'\n\n' for e in events] or [b': ping\n\n']

    def _create_job(self, data, managed=None, schedule=True, input_checkpoint=None):
        if not isinstance(data.get('title', ''), str):
            raise ValueError('title must be a string')
        contract = d.validate_contract(data['contract'])
        inputs = contract['inputs_manifest']
        if input_checkpoint is not None:
            source_job = self.get('job', input_checkpoint['job_id'])
            if (input_checkpoint['state'] not in ('durable', 'verified') or
                    source_job['work_package_id'] != data.get('work_package_id') or
                    inputs != self.manifest(input_checkpoint['manifest_sha256'])):
                raise ValueError('repair inputs must equal the assessed same-root checkpoint manifest')
        elif inputs is not None and inputs['base'] is not None:
            raise ValueError('initial inputs cannot refer to an unknown job manifest')
        if not managed and (contract.get('coordinates') or contract['harness'] == 'claude'):
            raise ValueError('claude coordinator contracts are controller-generated')
        ident, now = new_id(), self.clock()
        root_id = data.get('work_package_id') or ident
        require_uuid(root_id, 'work_package_id')
        root = self.store.get('root', root_id)
        if root:
            if any(j['work_package_id'] == root_id and j['status'] not in ('completed', 'failed', 'cancelled') for j in self.store.all('job')):
                raise APIError('conflict', 'work_package revisions must be serial')
        else:
            root = {'counters': dict(attempts=0, infra_retries=0, migrations=0, repairs=0), 'created_at': d.timestamp(now)}
        job = {
            'job_id': ident, 'request_id': data['request_id'], 'request_hash': p.value_hash(data),
            'work_package_id': root_id, 'title': data.get('title', ''), 'contract': contract,
            'status': 'queued', 'status_reason': '', 'protection': 'preparing' if contract['recovery']['auto'] else 'unprotected',
            'inputs_manifest_sha256': None, 'current_epoch': 0, 'current_attempt_id': None,
            'durable_checkpoint_id': None, 'durable_checkpoint_at': None,
            'acceptance': {'status': 'unverified', 'assessment_id': None, 'checkpoint_id': None,
                           'requested_status': None, 'evidence_gaps': []},
            'delivery_version': 0, 'delivery_checkpoint_id': None, 'integration_status': 'none',
            'counters': dict(root['counters']), 'cancel': {'requested_at': None, 'confirmed_at': None},
            'version': 1, 'created_at': d.timestamp(now), 'updated_at': d.timestamp(now),
        }
        repair = self.store.get('root', root_id) is not None
        exhausted = d.budget_reason(job, root['counters'], now, root['created_at'], repair=repair)
        if exhausted:
            raise APIError('budget_exhausted', exhausted)
        self.store.put('root', root_id, root)
        if contract['inputs_manifest'] is not None:
            job['inputs_manifest_sha256'] = self.remember_manifest(ident, contract['inputs_manifest'])
            if contract['recovery']['auto'] and not self.blobs.missing(contract['inputs_manifest']):
                job['protection'] = 'protected'
        self.store.put('job', ident, job)
        self.store.put('job_private', ident, {'pending': True, 'recovery_from': None, 'excluded': [], 'explicit': False,
                                            'managed': managed})
        if repair:
            self._counters(job, now, repairs=1)
        self.emit('job.created', job, now, job=job)
        if schedule:
            self._schedule(now)
        return response(self.get('job', ident), 201)

    def _events(self, attempt, data, tokens):
        stale = attempt['job_id'] is None or self.fence_reason(attempt, data) is not None or attempt['status'] not in ('leased', 'running')
        if not isinstance(data.get('events'), list):
            raise ValueError('events must be a list')
        accepted, duplicates = [], []
        now = self.clock()
        job = self.get('job', attempt['job_id']) if attempt['job_id'] else None
        if job is None:
            # 只计新事件：响应丢失后的重发会作为 duplicates 成功返回，不能重复消耗配额。
            # 同一批内重复的 event_id 只会写入一次，先去重再计数。
            ids = {str(event.get('event_id')) for event in data['events'] if isinstance(event, dict)}
            fresh = sum(1 for ident in ids if not self.store.db.execute(
                'SELECT 1 FROM event_keys WHERE event_id=?', (ident,)).fetchone())
            used = self.store.get('archive_quota', attempt['node_id'], 0)
            if used + fresh > ARCHIVE_EVENT_LIMIT:
                raise APIError('too_large', 'archive-only event quota exceeded for node', limit=ARCHIVE_EVENT_LIMIT)
            self.store.put('archive_quota', attempt['node_id'], used + fresh)
        for event in data['events']:
            ident = require_uuid(event['event_id'], 'event_id')
            if event['type'] not in p.NODE_EVENT_TYPES or not isinstance(event.get('payload'), dict):
                raise ValueError('invalid node event')
            if 'at' in event:
                d.seconds(event['at'])
            hashed = p.value_hash({'attempt_id': attempt['attempt_id'], 'epoch': data.get('epoch'), 'event': event})
            previous = self.store.db.execute('SELECT request_hash FROM event_keys WHERE event_id=?', (ident,)).fetchone()
            if previous:
                if not hmac.compare_digest(previous[0], hashed):
                    raise APIError('conflict', 'event_id reused with different content')
                duplicates.append(ident)
                continue
            payload = clean(event['payload'], tokens)
            if event['type'] == 'attempt.side_effect':
                if not isinstance(payload.get('op_id'), str) or not payload['op_id'] or payload.get('state') not in ('intent', 'done', 'unknown'):
                    raise ValueError('invalid side effect')
                if not stale:
                    effects = self.store.get('effects', job['work_package_id'], {})
                    # Late intent must not regress an acknowledged operation.
                    if effects.get(payload['op_id']) != 'done':
                        effects[payload['op_id']] = payload['state']
                    self.store.put('effects', job['work_package_id'], effects)
            if event['type'] == 'attempt.started' and not stale and attempt['status'] == 'leased':
                attempt.update(status='running', started_at=d.timestamp(now), workspace=payload.get('workspace'),
                               native_session_ref=payload.get('native_session_ref'))
                self.store.put('attempt', attempt['attempt_id'], attempt)
            self.store.event(event['type'], payload, d.timestamp(now), job_id=job['job_id'] if job else None,
                             attempt_id=attempt['attempt_id'], node_id=attempt['node_id'], event_id=ident,
                             stale=stale, request_hash=hashed)
            accepted.append(ident)
        return response({'accepted': accepted, 'duplicates': duplicates, 'stale': stale})

    def _checkpoint(self, attempt, data, tokens):
        self.fence(attempt, data)
        ident = require_uuid(data['checkpoint_id'], 'checkpoint_id')
        if data.get('kind') not in ('baseline', 'boundary', 'final'):
            raise ValueError('invalid checkpoint kind')
        manifest = p.validate_manifest(data['manifest'])
        summary = data.get('summary', {})
        if not isinstance(summary, dict) or set(summary) - {'completed', 'pending', 'failed_tries', 'last_verification', 'side_effects', 'restore_hint'}:
            raise ValueError('invalid summary')
        summary = dict(summary)
        for key in ('completed', 'pending', 'failed_tries', 'side_effects'):
            summary.setdefault(key, [])
            if not isinstance(summary[key], list):
                raise ValueError('invalid summary list')
        summary.setdefault('last_verification', None)
        summary.setdefault('restore_hint', None)
        if manifest['base'] and not self.store.db.execute('SELECT 1 FROM manifest_refs WHERE job_id=? AND sha256=?', (attempt['job_id'], manifest['base'])).fetchone():
            raise ValueError('manifest.base must belong to this job')
        content = {k: v for k, v in data.items() if k not in ('lease_token', 'authority_generation')}
        content.update(manifest=manifest, summary=summary, attempt_id=attempt['attempt_id'])
        hashed = p.value_hash(content)
        old = self.store.get('checkpoint', ident)
        if old:
            if self.get('checkpoint_private', ident)['hash'] != hashed:
                raise APIError('conflict', 'checkpoint_id reused with different content')
            if old['state'] in ('durable', 'verified'):
                return response(old)
        elif attempt['status'] not in ('leased', 'running'):
            raise APIError('conflict', 'attempt has already completed')
        now = self.clock()
        if old is None:
            digest = self.remember_manifest(attempt['job_id'], manifest)
            old = {'checkpoint_id': ident, 'job_id': attempt['job_id'], 'attempt_id': attempt['attempt_id'],
                   'epoch': attempt['epoch'], 'kind': data['kind'], 'state': 'uploading', 'manifest_sha256': digest,
                   'missing_blobs': self.blobs.missing(manifest), 'summary': clean(summary, tokens),
                   'native_session_ref': clean(data.get('native_session_ref'), tokens),
                   'created_at': d.timestamp(now), 'durable_at': None}
            self.store.put('checkpoint', ident, old)
            order = self.emit('checkpoint.uploading', old, now, job=self.get('job', attempt['job_id']), attempt=attempt)['seq']
            self.store.put('checkpoint_private', ident, {'hash': hashed, 'order': order})
            # Checkpoint evidence may add uncertainty but cannot erase observed unknown effects.
            effects = self.store.get('effects', self.get('job', attempt['job_id'])['work_package_id'], {})
            for effect in summary['side_effects']:
                if not isinstance(effect, dict) or not isinstance(effect.get('op_id'), str) or effect.get('state') not in ('intent', 'done', 'unknown'):
                    raise ValueError('invalid summary side_effects')
                if effect['op_id'] not in effects or (effects[effect['op_id']] != 'done' and effect['state'] != 'done'):
                    effects[effect['op_id']] = effect['state']
            self.store.put('effects', self.get('job', attempt['job_id'])['work_package_id'], effects)
        self._refresh_blobs(now)
        cp = self.get('checkpoint', ident)
        if cp['state'] == 'uploading':
            return APIError('missing_blobs', 'checkpoint upload incomplete', missing=cp['missing_blobs'], checkpoint=cp).response()
        return response(cp)

    def _complete(self, attempt, data, tokens):
        private = self.private(attempt)
        reason = self.fence_reason(attempt, data)
        hashed = p.value_hash(data)
        if reason and not (reason == 'revoked' and private['complete_hash']):
            raise APIError('fenced', 'attempt no longer owns execution', reason=reason)
        if private['complete_hash']:
            if private['complete_hash'] != hashed:
                raise APIError('conflict', 'completion content changed')
            return response(private['complete_response'])
        self.fence(attempt, data)
        job, now = self.get('job', attempt['job_id']), self.clock()
        outcome = data.get('outcome')
        if outcome not in ('succeeded', 'failed', 'cancelled'):
            raise ValueError('invalid completion outcome')
        if job['cancel']['requested_at'] and outcome != 'cancelled':
            raise APIError('conflict', 'cancellation must be acknowledged')
        if outcome == 'succeeded':
            cp = self.get('checkpoint', data.get('output_checkpoint_id'))
            if cp['attempt_id'] != attempt['attempt_id'] or cp['kind'] != 'final' or cp['state'] not in ('durable', 'verified'):
                raise APIError('conflict', 'success requires a durable final checkpoint from this attempt')
        elif outcome == 'cancelled' and not job['cancel']['requested_at']:
            raise APIError('conflict', 'cancel was not requested')
        elif outcome == 'failed' and data.get('failure_class') not in p.FAILURE_CLASSES:
            raise ValueError('invalid failure_class')
        runtime = data.get('runtime', attempt['runtime'])
        if not isinstance(runtime, dict) or set(runtime) - (set(attempt['runtime']) | {'agent_commit'}):
            raise ValueError('invalid runtime')
        runtime = dict(attempt['runtime'], **runtime)
        report = data.get('report')
        # 兼容 WP5 之前的 agent：它们在 complete 中发送 report=null。只拒绝非对象的非空值。
        if report is not None and not isinstance(report, dict):
            raise ValueError('report must be an object or null')
        if report is not None and len(p.canonical_json(report)) > 65536:
            raise APIError('too_large', 'report exceeds 64 KiB', limit=65536)
        attempt.update(report=report, native_session_ref=clean(data.get('native_session_ref'), tokens),
                       runtime=clean(runtime, tokens), ended_at=d.timestamp(now), status=outcome)
        self.store.put('attempt', attempt['attempt_id'], attempt)
        if outcome == 'failed':
            self._failure(attempt, data['failure_class'], clean(data.get('failure_detail'), tokens), now)
        else:
            cancel = dict(job['cancel'])
            if outcome == 'cancelled':
                cancel['confirmed_at'] = d.timestamp(now)
                private['revoked'] = True
            job = self.change('job', job, now, status='completed' if outcome == 'succeeded' else 'cancelled', status_reason='', cancel=cancel)
            self.emit('attempt.' + outcome, attempt, now, job=job, attempt=attempt)
            if outcome == 'succeeded':
                self._record_capability(attempt, job, now, cp)
                if job['contract'].get('coordinates'):
                    self._apply_coordinator(attempt, cp, now)
                else:
                    self._trigger_coordinator(job, 'completed', now)
            self._managed_lease(attempt, release=True)
        final = self.get('attempt', attempt['attempt_id'])
        private = dict(self.private(attempt), revoked=private['revoked'] or outcome in ('failed', 'cancelled'),
                       complete_hash=hashed, complete_response=final)
        self.store.put('attempt_private', attempt['attempt_id'], private)
        return response(final)

    def _publish(self, job, attempt, data, identity):
        if identity[0] != 'node':
            self.version(data, job)
        elif job['contract']['publish']['mode'] != 'auto':
            raise APIError('forbidden', 'manual publication requires a client credential')
        if attempt['status'] != 'succeeded' or job['cancel']['requested_at']:
            raise APIError('conflict', 'publication requires a successful current attempt')
        cp = self.get('checkpoint', data['checkpoint_id'])
        if cp['attempt_id'] != attempt['attempt_id'] or cp['state'] not in ('durable', 'verified') or cp['kind'] != 'final':
            raise APIError('conflict', 'publication requires the attempt final durable checkpoint')
        if type(data.get('expected_base_version')) is not int or data['expected_base_version'] != job['delivery_version']:
            raise APIError('version_mismatch', 'delivery version changed', current=job)
        assessments = [a for a in self.store.all('assessment') if a['checkpoint_id'] == cp['checkpoint_id']]
        if job['contract']['publish']['requires_acceptance'] and (not assessments or assessments[-1]['status'] != 'passed'):
            raise APIError('conflict', 'checkpoint has not passed acceptance')
        now = self.clock()
        publication = {'job_id': job['job_id'], 'delivery_version': job['delivery_version'] + 1,
                       'attempt_id': attempt['attempt_id'], 'epoch': attempt['epoch'], 'checkpoint_id': cp['checkpoint_id'],
                       'manifest_sha256': cp['manifest_sha256'], 'expected_base_version': data['expected_base_version'],
                       'request_id': data['request_id'], 'created_at': d.timestamp(now)}
        self.store.put('publication', job['job_id'] + ':' + str(publication['delivery_version']), publication)
        target = job['contract']['integration']
        superseded = False
        if target['target_node_id']:
            key, record = self._integration_target(job)
            revisions = [j['job_id'] for j in self.store.all('job') if j['work_package_id'] == job['work_package_id']]
            latest = (record.get('latest') or {}).get('job_id')
            # 旧修订迟到的发布不能顶替同目标上较新修订的回写待办。
            superseded = latest in revisions and revisions.index(latest) > revisions.index(job['job_id'])
        if target['target_node_id'] and not superseded:
            root_job = next(j for j in self.store.all('job') if j['work_package_id'] == job['work_package_id'])
            record['latest'] = {'job_id': job['job_id'], 'checkpoint_id': cp['checkpoint_id']}
            record['pending'] = dict(record['latest'], manifest_sha256=cp['manifest_sha256'],
                                     base_manifest_sha256=record.get('integrated_manifest_sha256') or root_job['inputs_manifest_sha256'],
                                     target_path=target['target_path'],
                                     exclude=job['contract'].get('workspace', {}).get('exclude', list(DEFAULT_EXCLUDES)))
            self.store.put('integration_target', key, record)
        self.change('job', job, now, delivery_version=publication['delivery_version'],
                    delivery_checkpoint_id=cp['checkpoint_id'],
                    integration_status='integration_pending' if target['target_node_id'] and not superseded else 'none')
        self.emit('job.published', publication, now, job=job, attempt=attempt)
        return response(publication)

    def _integration_target(self, job):
        target = job['contract']['integration']
        identity = dict(target, work_package_id=job['work_package_id'])
        key = p.value_hash(identity)
        record = self.store.get('integration_target', key)
        if record is None:
            # Existing rc4 integrations remain valid baselines after upgrade.
            relatives = [j for j in self.store.all('job') if j['work_package_id'] == job['work_package_id'] and
                         j['contract']['integration'] == target]
            integrated = [j for j in relatives if j['integration_status'] == 'integrated' and j['delivery_checkpoint_id']]
            cp = self.get('checkpoint', integrated[-1]['delivery_checkpoint_id']) if integrated else None
            latest_seq = -1
            for relative in relatives:
                for row in self.store.db.execute('SELECT data FROM events WHERE job_id=? ORDER BY seq DESC', (relative['job_id'],)):
                    event = json.loads(row['data'])
                    if event['type'] == 'job.integration' and event['payload'].get('status') == 'integrated':
                        if event['seq'] > latest_seq:
                            latest_seq = event['seq']
                            cp = self.get('checkpoint', event['payload']['checkpoint_id'])
                        break
            record = dict(identity, pending=None, integrated_manifest_sha256=cp['manifest_sha256'] if cp else None)
        return key, record

    def _integration(self, job, data, identity):
        if identity[0] != 'node':
            self.version(data, job)
        if not job['delivery_checkpoint_id'] or data.get('checkpoint_id') != job['delivery_checkpoint_id']:
            raise APIError('conflict', 'integration must refer to the current delivery checkpoint')
        if data.get('status') not in ('integration_pending', 'integrated', 'conflict') or not isinstance(data.get('detail'), dict):
            raise ValueError('invalid integration result')
        if job['contract']['integration']['target_node_id']:
            key, record = self._integration_target(job)
            delivery = {'job_id': job['job_id'], 'checkpoint_id': data['checkpoint_id']}
            if record.get('latest', delivery) != delivery:
                raise APIError('conflict', 'integration delivery was superseded for this target')
            record['latest'] = delivery
            if data['status'] == 'integrated':
                record['integrated_manifest_sha256'] = self.get('checkpoint', data['checkpoint_id'])['manifest_sha256']
            if data['status'] != 'integration_pending':
                record['pending'] = None
            self.store.put('integration_target', key, record)
        now = self.clock()
        job = self.change('job', job, now, integration_status=data['status'])
        self.emit('job.integration', {'checkpoint_id': data['checkpoint_id'], 'status': data['status'], 'detail': clean(data['detail'])}, now, job=job)
        return response(job)

    def _job_action(self, job, action, data, trigger_coordinator=True, schedule=True):
        self.version(data, job)
        now = self.clock()
        attempt = self.store.get('attempt', job['current_attempt_id']) if job['current_attempt_id'] else None
        if action == 'cancel':
            if job['status'] in ('completed', 'failed', 'cancelled'):
                raise APIError('conflict', 'job is already terminal')
            running = attempt and attempt['status'] in ('leased', 'running')
            cancel = {'requested_at': job['cancel']['requested_at'] or d.timestamp(now), 'confirmed_at': None if running else d.timestamp(now)}
            job = self.change('job', job, now, status='cancel_requested' if running else 'cancelled', cancel=cancel, status_reason='user_cancel')
            jp = self.get('job_private', job['job_id'])
            jp['pending'] = False
            self.store.put('job_private', job['job_id'], jp)
            if attempt and attempt['status'] == 'assigned':
                attempt.update(status='cancelled', ended_at=d.timestamp(now), failure_class='user_cancel')
                self.store.put('attempt', attempt['attempt_id'], attempt)
                private = self.private(attempt)
                private['revoked'] = True
                self.store.put('attempt_private', attempt['attempt_id'], private)
                self.emit('attempt.cancelled', attempt, now, job=job, attempt=attempt)
            return response(job)
        if action == 'assess':
            ident = require_uuid(data['assessment_id'], 'assessment_id')
            cp = self.get('checkpoint', data['checkpoint_id'])
            if cp['job_id'] != job['job_id'] or cp['state'] not in ('durable', 'verified') or data.get('status') not in p.ACCEPTANCE:
                raise APIError('conflict', 'assessment requires this job durable checkpoint and valid status')
            for key in ('method', 'result', 'reviewer'):
                if not isinstance(data.get(key), str):
                    raise ValueError('assessment requires method, result and reviewer strings')
            record = {k: data[k] for k in ('assessment_id', 'checkpoint_id', 'status', 'method', 'result', 'reviewer')}
            record.update(job_id=job['job_id'], manifest_sha256=cp['manifest_sha256'], created_at=d.timestamp(now))
            if record['status'] == 'passed' and job['contract'].get('checks'):
                producer = self.get('attempt', cp['attempt_id'])
                gaps = d.evidence_gaps(job['contract'], producer.get('report'), cp['manifest_sha256'])
                if gaps:
                    record.update(status='unverified', requested_status='passed', evidence_gaps=gaps)
                else:
                    record['evidence'] = {'attempt_id': producer['attempt_id'], 'manifest_sha256': cp['manifest_sha256'],
                                          'checks': [c['name'] for c in job['contract']['checks']]}
            if self.store.get('assessment', ident):
                raise APIError('conflict', 'assessment_id already exists')
            self.store.put('assessment', ident, clean(record))
            if record['status'] == 'passed':
                cp['state'] = 'verified'
                self.store.put('checkpoint', cp['checkpoint_id'], cp)
            acceptance = {k: record[k] for k in ('status', 'assessment_id', 'checkpoint_id')}
            acceptance.update(requested_status=record.get('requested_status'), evidence_gaps=record.get('evidence_gaps', []))
            job = self.change('job', job, now, acceptance=acceptance)
            if record.get('evidence_gaps'):
                self.emit('job.evidence_gap', record, now, job=job)
            self.emit('job.assessed', record, now, job=job)
            if data['status'] == 'rework' and trigger_coordinator:
                self._trigger_coordinator(job, 'rework', now)
            return response(record)
        if job['cancel']['requested_at']:
            raise APIError('conflict', 'cancelled jobs cannot be retried')
        if not isinstance(data.get('reason'), str) or not data['reason']:
            raise ValueError('retry reason required')
        excluded = data.get('exclude_node_ids', [])
        d.string_list(excluded, 'exclude_node_ids')
        for node_id in excluded:
            p.check_node_id(node_id)
        effects = self.store.get('effects', job['work_package_id'], {})
        if any(v in ('unknown', 'intent') for v in effects.values()):
            raise APIError('conflict', 'side effects require reconciliation before retry')
        # 同根修订串行执行（与 _create_job 一致）：另一修订未结束时不能重试旧修订，否则两者会同时持有租约。
        if any(j['job_id'] != job['job_id'] and j['work_package_id'] == job['work_package_id'] and
               j['status'] not in ('completed', 'failed', 'cancelled') for j in self.store.all('job')):
            raise APIError('conflict', 'another revision of this work package is not terminal')
        root = self.root(job)
        reason = d.budget_reason(job, root['counters'], now, root['created_at'], migration=True)
        if reason:
            raise APIError('budget_exhausted', reason)
        if attempt:
            self._managed_lease(attempt, release=True)
            private = self.private(attempt)
            private['revoked'] = True
            self.store.put('attempt_private', attempt['attempt_id'], private)
            if attempt['status'] in p.ATTEMPT_ACTIVE or attempt['status'] == 'succeeded':
                attempt.update(status='superseded', ended_at=d.timestamp(now))
                self.store.put('attempt', attempt['attempt_id'], attempt)
                self.emit('attempt.superseded', attempt, now, job=job, attempt=attempt)
        jp = self.get('job_private', job['job_id'])
        jp.update(pending=True, recovery_from=attempt['attempt_id'] if attempt else None, excluded=excluded, explicit=True)
        self.store.put('job_private', job['job_id'], jp)
        job = self.change('job', job, now, status='recovering', status_reason=data['reason'])
        if schedule:
            self._schedule(now)
        return response(self.get('job', job['job_id']))

    def _drain(self, node_id, data, headers=None):
        node = self.get('node', node_id)
        self.version(data, node)
        if type(data.get('drain')) is not bool:
            raise ValueError('drain must be boolean')
        if data['drain'] and type(data.get('migrate', False)) is not bool:
            raise ValueError('migrate must be boolean')
        node = self.change('node', node, self.clock(), drain=data['drain'])
        if not data['drain'] or not data.get('migrate'):
            return response(node)
        not_migrated = []
        for attempt in self.store.all('attempt'):
            if attempt['node_id'] != node_id or attempt['status'] not in p.ATTEMPT_ACTIVE:
                continue
            job = self.get('job', attempt['job_id'])
            reasons = self._drain_migration_reasons(job, attempt, headers or {})
            if reasons:
                not_migrated.append({'attempt_id': attempt['attempt_id'], 'reasons': reasons})
                continue
            self.store.db.execute('SAVEPOINT drain_migration')
            try:
                self._job_action(job, 'retry', {'expected_version': job['version'], 'reason': 'node_draining',
                                               'exclude_node_ids': [node_id]}, schedule=False)
                # Reserve the destination before unrelated pending work can consume it.
                self._schedule(self.clock(), only_job_id=job['job_id'])
                updated = self.get('job', job['job_id'])
                if updated['current_attempt_id'] == attempt['attempt_id']:
                    reasons = ['migration_not_scheduled', updated['status_reason']]
            except APIError as exc:
                reasons = [exc.code, exc.message]
            finally:
                if reasons:
                    self.store.db.execute('ROLLBACK TO drain_migration')
                self.store.db.execute('RELEASE drain_migration')
            if reasons:
                not_migrated.append({'attempt_id': attempt['attempt_id'], 'reasons': reasons})
        return response(dict(node, not_migrated=not_migrated))

    def _drain_migration_reasons(self, job, attempt, headers):
        reasons = []
        if job['protection'] != 'protected':
            reasons.append('protection_not_ready')
        if job['contract']['recovery']['side_effects'] != 'none':
            reasons.append('side_effects_not_none')
        cp = self.store.get('checkpoint', job['durable_checkpoint_id'])
        if not (cp and cp['state'] in ('durable', 'verified')) and not job['inputs_manifest_sha256']:
            reasons.append('missing_recovery_baseline')
        if job['cancel']['requested_at']:
            reasons.append('cancel_requested')
        effects = self.store.get('effects', job['work_package_id'], {})
        if any(v in ('unknown', 'intent') for v in effects.values()):
            reasons.append('side_effect_unknown')
        try:
            self.coordinator_guard(job['work_package_id'], headers)
        except APIError:
            reasons.append('coordinator_lease')
        root = self.root(job)
        budget = d.budget_reason(job, root['counters'], self.clock(), root['created_at'], migration=True)
        if budget:
            reasons.append(budget)
        private = self.get('job_private', job['job_id'])
        excluded = [attempt['node_id']]
        if private.get('managed'):
            excluded.extend(n['node_id'] for n in self.store.all('node') if n['node_id'] not in private['managed']['node_ids'])
        explanation = d.explain(job, self.store.all('node'),
            [a for a in self.store.all('attempt') if a['attempt_id'] != attempt['attempt_id']],
            {j['job_id']: j for j in self.store.all('job')}, self.clock(), self.config,
            paused=self.meta()['dispatch_paused'], quota={q['key']: q['until'] for q in self.store.all('quota')},
            excluded=excluded, source_node=self.get('node', attempt['node_id']), recovering=True)
        if explanation['selected_node_id'] is None:
            reasons.append('no_eligible_node')
            reasons.extend(sorted({r for n in explanation['nodes'] if n['node_id'] != attempt['node_id'] for r in n['reasons']}))
        return reasons

    def _dispatch(self, data):
        meta = self.meta()
        self.version(data, meta)
        if type(data.get('paused')) is not bool:
            raise ValueError('paused must be boolean')
        meta = self.change('meta', meta, self.clock(), dispatch_paused=data['paused'])
        if not data['paused']:
            self._schedule(self.clock())
        return response(meta)


class ControllerLock:
    """OS-owned lock, released on process death; never unlink the lock inode."""
    def __init__(self, data_dir):
        self.path = Path(data_dir) / 'controller.lock'
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(str(self.path), 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.file.write(b'\0')
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise RuntimeError('controller.lock is held; stop the controller before restore/another serve')
        return self

    def __exit__(self, *args):
        if self.file:
            if os.name == 'nt':
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()
            self.file = None


def file_hash(path):
    digest = hashlib.sha256()
    with open(str(path), 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def sync_directory(path):
    if os.name == 'posix':
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def database_references(db, blobs):
    """Return expected present blobs; missing uploads are legal, missing durable data is not."""
    records = [(row[0], json.loads(row[1])) for row in db.execute('SELECT kind,data FROM records')]
    durable = {r['manifest_sha256'] for k, r in records if k == 'checkpoint' and r['state'] in ('durable', 'verified')}
    durable.update(r['inputs_manifest_sha256'] for k, r in records if k == 'job' and r['protection'] == 'protected')
    references = {}
    for row in db.execute('SELECT DISTINCT sha256 FROM manifest_refs'):
        digest = row[0]
        raw = blobs.read(digest)
        if p.sha256_bytes(raw) != digest:
            raise ValueError('corrupt manifest blob')
        manifest = p.validate_manifest(json.loads(raw))
        references[digest] = len(raw)
        for entry in manifest['files']:
            if blobs.exists(entry['sha256'], entry['size']):
                references[entry['sha256']] = entry['size']
            elif digest in durable:
                raise ValueError('missing durable blob: ' + entry['sha256'])
    return references


def backup(data_dir, dest, clock=time.time):
    data_dir, dest = Path(data_dir).resolve(), Path(dest).resolve()
    if dest == data_dir or data_dir in dest.parents or dest in data_dir.parents:
        raise ValueError('backup destination must be separate from the data directory')
    if not (data_dir / 'controller.db').is_file():
        raise ValueError('controller database does not exist')
    dest.mkdir(parents=True, exist_ok=True)
    # Serialize writers to this backup destination independently of a running server.
    with ControllerLock(dest):
        stage = dest / ('.backup-' + new_id() + '.db')
        manifest_stage = None
        source = sqlite3.connect(str(data_dir / 'controller.db'))
        target = sqlite3.connect(str(stage))
        try:
            source.backup(target)
            generation = json.loads(target.execute("SELECT data FROM records WHERE kind='meta' AND id='controller'").fetchone()[0])['authority_generation']
            blobs = BlobStore(data_dir)
            references = database_references(target, blobs)
            output = BlobStore(dest)
            for digest, size in references.items():
                if not output.exists(digest, size) or file_hash(output.path(digest)) != digest:
                    # Content is immutable, so references from the backup snapshot stay valid.
                    output.max_bytes = max(output.max_bytes, size)
                    output.put(digest, blobs.read(digest))
            target.close()
            target = None
            database_hash = file_hash(stage)
            database_name = 'controller-' + database_hash + '.db'
            manifest = {'version': 1, 'db_sha256': database_hash, 'database': database_name, 'blobs': [
                {'sha256': digest, 'size': size} for digest, size in sorted(references.items())],
                        'authority_generation': generation, 'created_at': d.timestamp(clock())}
            manifest_stage = dest / ('.manifest-' + new_id())
            with open(str(manifest_stage), 'wb') as f:
                f.write(p.canonical_json(manifest))
                f.flush()
                os.fsync(f.fileno())
            # Publish the immutable database first, then atomically switch the manifest.
            # A crash here leaves the previous manifest and its snapshot valid.
            os.replace(str(stage), str(dest / database_name))
            sync_directory(dest)
            os.replace(str(manifest_stage), str(dest / 'backup-manifest.json'))
            sync_directory(dest)
        finally:
            source.close()
            if target is not None:
                target.close()
            if stage.exists():
                stage.unlink()
            if manifest_stage is not None and manifest_stage.exists():
                manifest_stage.unlink()
    store = Store(data_dir)
    try:
        with store.transaction():
            store.put('backup', 'latest', {'time': clock(), 'destination': str(dest)})
            store.event('controller.backup', {'db_sha256': manifest['db_sha256']}, d.timestamp(clock()))
    finally:
        store.close()
    return manifest


def restore(data_dir, source_dir, clock=time.time):
    data_dir, source_dir = Path(data_dir).resolve(), Path(source_dir).resolve()
    if data_dir == source_dir or data_dir in source_dir.parents or source_dir in data_dir.parents:
        raise ValueError('restore source must be separate from the data directory')
    with ControllerLock(data_dir), ControllerLock(source_dir):
        manifest = json.loads((source_dir / 'backup-manifest.json').read_text(encoding='utf-8'))
        if manifest.get('version') != 1 or type(manifest.get('authority_generation')) is not int:
            raise ValueError('invalid backup manifest')
        p.check_sha256(manifest['db_sha256'])
        database_name = manifest.get('database', 'controller.db')
        if database_name not in ('controller.db', 'controller-' + manifest['db_sha256'] + '.db'):
            raise ValueError('unsafe backup database name')
        source_database = source_dir / database_name
        if file_hash(source_database) != manifest['db_sha256']:
            raise ValueError('backup database hash mismatch')
        source_blobs, target_blobs = BlobStore(source_dir), BlobStore(data_dir)
        listed = {}
        for entry in manifest['blobs']:
            digest, size = p.check_sha256(entry['sha256']), entry['size']
            if type(size) is not int or size < 0 or digest in listed:
                raise ValueError('invalid blob inventory')
            if not source_blobs.exists(digest, size) or file_hash(source_blobs.path(digest)) != digest:
                raise ValueError('backup blob hash/size mismatch')
            listed[digest] = size
        source = sqlite3.connect(str(source_database))
        try:
            if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('backup database integrity check failed')
            saved_meta = json.loads(source.execute("SELECT data FROM records WHERE kind='meta' AND id='controller'").fetchone()[0])
            if saved_meta['authority_generation'] != manifest['authority_generation'] or database_references(source, source_blobs) != listed:
                raise ValueError('backup inventory/generation mismatch')
        finally:
            source.close()
        current_generation, current_seq = 0, 0
        database = data_dir / 'controller.db'
        if database.exists():
            current = Store(data_dir)
            try:
                current_generation = current.get('meta', 'controller')['authority_generation']
                sequence = current.db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
                current_seq = sequence[0] if sequence else 0
                current.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            finally:
                current.close()
        for digest, size in listed.items():
            if not target_blobs.exists(digest, size) or file_hash(target_blobs.path(digest)) != digest:
                target_blobs.max_bytes = max(target_blobs.max_bytes, size)
                target_blobs.put(digest, source_blobs.read(digest))
        stage_dir = data_dir / ('.restore-' + new_id())
        stage_dir.mkdir()
        try:
            shutil.copyfile(str(source_database), str(stage_dir / 'controller.db'))
            stage = Store(stage_dir)
            try:
                with stage.transaction():
                    stage.db.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='events'", (current_seq,))
                    meta = stage.get('meta', 'controller')
                    meta.update(authority_generation=max(current_generation, manifest['authority_generation']) + 1,
                                dispatch_paused=True, version=meta['version'] + 1)
                    stage.put('meta', 'controller', meta)
                    # Backup rollback loses live facts; freeze execution until explicit reconciliation.
                    for attempt in stage.all('attempt'):
                        private = stage.get('attempt_private', attempt['attempt_id'])
                        private['revoked'] = True
                        stage.put('attempt_private', attempt['attempt_id'], private)
                        if attempt['status'] in p.ATTEMPT_ACTIVE:
                            attempt.update(status='lost', failure_class='node_lost', failure_detail='stale_generation', ended_at=d.timestamp(clock()))
                            stage.put('attempt', attempt['attempt_id'], attempt)
                    for job in stage.all('job'):
                        jp = stage.get('job_private', job['job_id'])
                        jp['pending'] = False
                        stage.put('job_private', job['job_id'], jp)
                        if job['status'] not in ('cancelled', 'failed', 'completed'):
                            job = d.transition(job, clock(), status='cancel_requested' if job['cancel']['requested_at'] else 'waiting_reconcile', status_reason='stale_generation')
                            stage.put('job', job['job_id'], job)
                            stage.event('job.updated', job, d.timestamp(clock()), job_id=job['job_id'])
                    for node in stage.all('node'):
                        stage.put('node_private', node['node_id'], {'reconcile_required': True})
                    stage.db.execute("DELETE FROM records WHERE kind='session'")
                    stage.event('controller.restored', {'authority_generation': meta['authority_generation']}, d.timestamp(clock()))
                stage.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                stage.db.execute('PRAGMA journal_mode=DELETE')
            finally:
                stage.close()
            for suffix in ('-wal', '-shm'):
                path = Path(str(database) + suffix)
                if path.exists():
                    path.unlink()
            os.replace(str(stage_dir / 'controller.db'), str(database))
            sync_directory(data_dir)
        finally:
            shutil.rmtree(str(stage_dir))
        return meta['authority_generation']


def parse_bind(value, tls_cert=None, tls_key=None):
    if value.startswith('['):
        host, separator, port = value[1:].partition(']:')
    else:
        host, separator, port = value.rpartition(':')
    if not separator or not host or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError('bind must be host:port or [IPv6]:port')
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        if host != 'localhost':
            raise ValueError('bind must be a literal IP or localhost')
        loopback = True
        host = '127.0.0.1'
    if bool(tls_cert) != bool(tls_key) or (not loopback and not tls_cert):
        raise ValueError('non-loopback bind requires both --tls-cert and --tls-key')
    return host, int(port)


class HTTPHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    # 连接建立后读取请求行与头部的上限；直连 TLS 时握手也在处理线程中受此限制，
    # 不在主线程 accept() 里进行，避免一个不发 ClientHello 的连接阻塞所有请求。
    timeout = 30

    def setup(self):
        super().setup()
        if isinstance(self.connection, ssl.SSLSocket):
            self.connection.do_handshake()

    def log_message(self, format, *args):
        # Paths, query strings and credentials never enter default stderr request logs.
        pass

    def send_error(self, code, message=None, explain=None):
        result = APIError('invalid', 'invalid HTTP request').response()
        result.status = code
        self.close_connection = True
        self._write(result)

    def _write(self, result):
        self.send_response(result.status)
        for key, value in result.headers:
            self.send_header(key, value)
        if not any(key.lower() == 'content-length' for key, _ in result.headers):
            self.send_header('Content-Length', str(len(result.body)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(result.body)

    def handle_expect_100(self):
        try:
            self._length()
        except APIError as exc:
            self.close_connection = True
            self._write(exc.response())
            return False
        self.send_response_only(100)
        self.end_headers()
        return True

    def _length(self):
        values = self.headers.get_all('Content-Length', [])
        if self.headers.get('Transfer-Encoding') or len(values) > 1:
            raise APIError('invalid', 'ambiguous or unsupported body framing')
        if not values:
            # 无请求体的 DELETE（浏览器 fetch 不发送 Content-Length）按空体处理；
            # 曾导致 UI 退出登录与释放协调权失败。POST/PUT 仍须显式声明长度。
            if self.command in ('POST', 'PUT'):
                raise APIError('invalid', 'Content-Length required')
            return 0
        if not values[0].isdigit():
            raise APIError('invalid', 'invalid Content-Length')
        length = int(values[0])
        is_blob = self.command == 'PUT' and urlsplit(self.path).path.startswith(p.API_PREFIX + '/blobs/')
        maximum = self.server.app.config['max_blob_bytes' if is_blob else 'max_json_bytes']
        if length > maximum:
            raise APIError('too_large', 'request body too large', limit=maximum)
        return length

    def _request(self):
        try:
            self.connection.settimeout(30)
            length = self._length()
            body = self.rfile.read(length)
            if len(body) != length:
                raise APIError('invalid', 'truncated request body')
            headers = {k.lower(): v for k, v in self.headers.items()}
            result = self.server.app.handle(self.command, self.path, headers, body)
            if result.stream is None:
                self._write(result)
                return
            self.send_response(result.status)
            for key, value in result.headers:
                self.send_header(key, value)
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            cursor = int(query.get('cursor', [headers.get('last-event-id', '0')])[0])
            last_ping = time.monotonic()
            while not self.server.stopping.is_set():
                snapshot = False
                sent = False
                for chunk in result.stream:
                    if chunk.startswith(b': ping') and time.monotonic() - last_ping < self.server.app.config['sse_ping_s']:
                        continue
                    self.wfile.write(chunk)
                    sent = True
                    last_ping = time.monotonic()
                    if chunk.startswith(b'id: '):
                        cursor = int(chunk.split(b'\n', 1)[0][4:])
                    snapshot = snapshot or chunk.startswith(b'event: snapshot_required')
                if sent:
                    self.wfile.flush()
                if snapshot:
                    return
                self.server.stopping.wait(0.25)
                result = self.server.app.handle('GET', p.API_PREFIX + '/events/stream?cursor=' + str(cursor), headers, b'')
                if result.status != 200 or result.stream is None:
                    return
        except APIError as exc:
            self.close_connection = True
            self._write(exc.response())
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
            self.close_connection = True

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = _request


def serve(data_dir, bind='127.0.0.1:18669', public_origin=None, tls_cert=None, tls_key=None):
    address = parse_bind(bind, tls_cert, tls_key)
    context = None
    if tls_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(tls_cert, tls_key)
    host = '[' + address[0] + ']' if ':' in address[0] else address[0]
    configured = Path(data_dir) / 'config.json'
    saved = json.loads(configured.read_text(encoding='utf-8')) if configured.is_file() else {}
    config = {'public_origin': public_origin or saved.get('public_origin') or
              ('https' if context else 'http') + '://' + host + ':' + str(address[1])}
    with ControllerLock(data_dir):
        app = App(data_dir, config)
        app.listener = bind
        class Server(ThreadingHTTPServer):
            daemon_threads = True
            address_family = socket.AF_INET6 if ':' in address[0] else socket.AF_INET
        try:
            server = Server(address, HTTPHandler)
            server.app, server.stopping = app, threading.Event()
            if context:
                server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
            def ticking():
                while not server.stopping.wait(1):
                    try:
                        app.tick()
                    except Exception:
                        # Fail closed on authoritative-store failure, without printing request data.
                        server.stopping.set()
                        server.shutdown()
                        return
            thread = threading.Thread(target=ticking, daemon=True)
            thread.start()
            try:
                server.serve_forever(poll_interval=0.25)
            except KeyboardInterrupt:
                pass
            finally:
                server.stopping.set()
                server.server_close()
                thread.join(timeout=5)
        finally:
            app.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('serve', 'pairing-code', 'admin-token', 'backup', 'restore', 'prune-events'):
        command = sub.add_parser(name)
        command.add_argument('--data-dir', required=True)
        if name == 'serve':
            command.add_argument('--bind', default='127.0.0.1:18669')
            command.add_argument('--public-origin')
            command.add_argument('--tls-cert')
            command.add_argument('--tls-key')
        elif name == 'pairing-code':
            command.add_argument('--node-id', required=True)
            command.add_argument('--ttl', type=int, default=600)
        elif name == 'backup':
            command.add_argument('--dest', required=True)
        elif name == 'restore':
            command.add_argument('--from', dest='source', required=True)
        elif name == 'prune-events':
            command.add_argument('--keep', type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'serve':
            serve(args.data_dir, args.bind, args.public_origin, args.tls_cert, args.tls_key)
        elif args.command == 'backup':
            print(json.dumps(backup(args.data_dir, args.dest)))
        elif args.command == 'restore':
            print(json.dumps({'authority_generation': restore(args.data_dir, args.source)}))
        else:
            data_dir = Path(args.data_dir).resolve()
            if not (data_dir / 'controller.db').exists() or not (data_dir / 'admin.token').exists():
                with ControllerLock(data_dir):
                    app = App(data_dir)
                    app.close()
            if args.command == 'admin-token':
                print(str(data_dir / 'admin.token'))
            elif args.command == 'prune-events':
                store = Store(data_dir)
                try:
                    store.prune_events(args.keep)
                finally:
                    store.close()
            else:
                p.check_node_id(args.node_id)
                if args.ttl <= 0:
                    raise ValueError('ttl must be positive')
                code = secrets.token_hex(18)  # 同上：不以 '-' 开头
                store = Store(data_dir)
                try:
                    with store.transaction():
                        store.put('pairing', digest_token(code), {'hash': digest_token(code), 'node_id': args.node_id, 'expires': time.time() + args.ttl})
                finally:
                    store.close()
                print(code)
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        parser.exit(1, type(exc).__name__ + ': ' + str(exc) + '\n')
    return 0


if __name__ == '__main__':
    main()
