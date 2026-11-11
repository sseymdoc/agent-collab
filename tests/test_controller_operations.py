from email.message import Message
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest import mock

from agent_collab import protocol as p
from agent_collab.controller import App, APIError, ControllerLock, HTTPHandler, backup, restore, parse_bind
from helpers import ControllerCase, contract, manifest, uid


class AuthTest(ControllerCase):
    def test_health_and_auth_roles(self):
        self.assertEqual(self.call('GET', '/healthz', headers={}), {'ok': True})
        self.call('GET', '/jobs', headers={}, status=401)
        node = self.node()
        self.call('POST', '/jobs', {'request_id': uid(), 'contract': contract()}, token=node.token, status=403)
        self.call('GET', '/jobs', token=node.token, status=403)
        self.call('POST', '/nodes/a/claim', {}, status=403)
        other = self.node('other')
        self.call('POST', '/nodes/a/claim', {}, token=other.token, status=403)

    def test_pairing_code_is_bound_expiring_and_one_time(self):
        code = self.app.pairing_code('node')
        self.call('POST', '/nodes/pair', {'code': code, 'node_id': 'wrong'}, headers={}, status=401)
        self.call('POST', '/nodes/pair', {'code': code, 'node_id': 'node'}, headers={})
        self.call('POST', '/nodes/pair', {'code': code, 'node_id': 'node'}, headers={}, status=401)
        code = self.app.pairing_code('node', 1)
        self.clock.advance(2)
        self.call('POST', '/nodes/pair', {'code': code, 'node_id': 'node'}, headers={}, status=401)

    def login(self):
        result = self.app.handle('POST', '/api/v1/session', {}, p.canonical_json({'token': self.admin}))
        self.assertEqual(result.status, 200)
        cookie = dict(result.headers)['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        return {'cookie': cookie.split(';')[0], 'x-csrf-token': json.loads(result.body)['csrf_token']}

    def test_session_csrf_origin_roles_and_logout(self):
        headers = self.login()
        self.call('GET', '/jobs', headers={'cookie': headers['cookie']})
        data = {'request_id': uid(), 'contract': contract()}
        self.call('POST', '/jobs', data, headers={'cookie': headers['cookie']}, status=403)
        self.call('POST', '/jobs', data, headers=dict(headers, origin='https://attacker.invalid'), status=403)
        self.call('POST', '/jobs', data, headers=dict(headers, origin=self.app.config['public_origin']), status=201)
        self.call('POST', '/nodes/a/claim', {}, headers=headers, status=403)
        self.call('DELETE', '/session', headers=headers)
        self.call('GET', '/jobs', headers=headers, status=401)

    def test_session_csrf_recoverable_after_reload(self):
        headers = self.login()
        again = self.call('GET', '/session', headers={'cookie': headers['cookie']})
        self.assertEqual(again['csrf_token'], headers['x-csrf-token'])
        # 取回的令牌可用于变更；管理令牌与节点令牌不能读取会话 CSRF。
        self.call('POST', '/jobs', {'request_id': uid(), 'contract': contract()},
                  headers={'cookie': headers['cookie'], 'x-csrf-token': again['csrf_token']}, status=201)
        self.call('GET', '/session', status=403)
        self.call('GET', '/session', token=self.node().token, status=403)
        self.call('GET', '/session', headers={}, status=401)

    def test_session_https_secure_and_login_origin(self):
        self.app.config['public_origin'] = 'https://controller.example'
        result = self.app.handle('POST', '/api/v1/session', {}, p.canonical_json({'token': self.admin}))
        self.assertIn('; Secure', dict(result.headers)['Set-Cookie'])
        self.call('POST', '/session', {'token': self.admin}, headers={'origin': 'http://attacker.invalid'}, status=403)

    def test_credentials_are_hashed_and_event_content_redacted(self):
        node = self.node()
        job = self.job()
        a = node.claim()
        node.event(a, 'attempt.log', {'text': 'credential ' + node.token + ' ' + a['lease_token'], 'lease_token': a['lease_token']})
        events = self.call('GET', '/events?after=0')['events']
        serialized = json.dumps(events)
        self.assertNotIn(node.token, serialized)
        self.assertNotIn(a['lease_token'], serialized)
        dump = '\n'.join(self.app.store.db.iterdump())
        self.assertNotIn(node.token, dump)
        self.assertNotIn(a['lease_token'], dump)
        self.assertNotIn(self.admin, dump)
        if os.name == 'posix':
            self.assertEqual((self.temp / 'data' / 'admin.token').stat().st_mode & 0o777, 0o600)

    def test_body_limits_validation_and_unknown_api(self):
        self.app.config['max_json_bytes'] = 50
        self.call('POST', '/jobs', b'x' * 51, status=413)
        self.app.config['max_blob_bytes'] = 2
        self.call('PUT', '/blobs/' + p.sha256_bytes(b'abc'), b'abc', status=413)
        for body in (b'{', b'[]', b'{"x":NaN}'):
            self.call('POST', '/jobs', body, status=400)
        self.call('GET', '/missing', status=404)
        response = self.app.handle('HEAD', '/api/v1/healthz', {}, b'')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b'')
        self.assertGreater(int(dict(response.headers)['Content-Length']), 0)

    def test_blobs_head_download_auth_and_path_traversal(self):
        node = self.node()
        digest = node.upload(b'abc')
        result = self.app.handle('HEAD', '/api/v1/blobs/' + digest, {'authorization': 'Bearer ' + node.token}, b'')
        self.assertEqual(result.status, 200)
        self.assertEqual(result.body, b'')
        self.assertEqual(dict(result.headers)['Content-Length'], '3')
        result = self.app.handle('GET', '/api/v1/blobs/' + digest, {'authorization': 'Bearer ' + self.admin}, b'')
        self.assertEqual(result.body, b'abc')
        self.call('GET', '/blobs/' + digest, headers={}, status=401)
        self.call('HEAD', '/blobs/' + ('f' * 64), status=404)
        for path in ('/../admin.token', '/%2e%2e/admin.token', '/a\\b', '/C:/x'):
            self.assertEqual(self.app.handle('GET', path, {}, b'').status, 400)

    def test_static_assets_and_spa_fallback(self):
        root = self.temp / 'package'
        (root / 'static').mkdir(parents=True)
        (root / 'static' / 'index.html').write_bytes(b'<h1>UI</h1>')
        (root / 'static' / 'app.js').write_bytes(b'console.log(1)')
        with mock.patch('agent_collab.controller.__file__', str(root / 'controller.py')):
            for path in ('/', '/jobs/example'):
                self.assertEqual(self.app.handle('GET', path, {}, b'').body, b'<h1>UI</h1>')
            self.assertEqual(self.app.handle('GET', '/app.js', {}, b'').body, b'console.log(1)')
            self.assertEqual(self.app.handle('GET', '/api/v1/unknown', {'authorization': 'Bearer ' + self.admin}, b'').status, 404)
            self.assertEqual(self.app.handle('GET', '/%2e%2e/admin.token', {}, b'').status, 400)

    def test_malformed_credentials_and_epoch_types_are_rejected(self):
        self.call('POST', '/nodes/pair', {'code': [], 'node_id': 'x'}, headers={}, status=400)
        node = self.node()
        self.job()
        assignment = node.claim()
        bad = dict(assignment, epoch=True)
        node.checkpoint(bad, status=409)
        node.event(assignment, payload={'at': []})
        data = node.payload(assignment, events=[{'event_id': uid(), 'type': 'attempt.started', 'at': {}, 'payload': {}}])
        self.call('POST', '/attempts/' + assignment['attempt_id'] + '/events', data, token=node.token, status=400)


class EventsTest(ControllerCase):
    def stream(self, suffix='', headers=None):
        headers = headers or {'authorization': 'Bearer ' + self.admin}
        result = self.app.handle('GET', '/api/v1/events/stream' + suffix, headers, b'')
        self.assertEqual(result.status, 200)
        self.assertEqual(dict(result.headers)['Content-Type'], 'text/event-stream')
        self.assertEqual(dict(result.headers)['X-Accel-Buffering'], 'no')
        return b''.join(result.stream)

    def test_cursor_and_last_event_id_replay(self):
        self.job()
        events = self.call('GET', '/events')['events']
        cursor = events[0]['seq']
        stream = self.stream('?cursor=' + str(cursor))
        self.assertNotIn(('id: %d\n' % cursor).encode(), stream)
        for event in events[1:]:
            self.assertIn(('id: %d\nevent: %s\ndata: ' % (event['seq'], event['type'])).encode(), stream)
        self.assertEqual(stream, self.stream(headers={'authorization': 'Bearer ' + self.admin, 'last-event-id': str(cursor)}))
        override = self.stream('?cursor=' + str(cursor), {'authorization': 'Bearer ' + self.admin, 'last-event-id': str(events[-1]['seq'])})
        self.assertEqual(override, stream)
        self.assertEqual(self.stream('?cursor=' + str(events[-1]['seq'])), b': ping\n\n')

    def test_pruned_cursor_requires_snapshot_including_empty_log(self):
        self.job()
        self.app.store.prune_events(1)
        stream = self.stream('?cursor=1')
        self.assertTrue(stream.startswith(b'event: snapshot_required\n'))
        self.assertIn(b'"min_seq"', stream)
        self.assertEqual(len(self.call('GET', '/events')['events']), 1)
        # 流按 seq > cursor 续读：游标为 min_seq-1 时没有漏掉任何保留的事件，直接续流，不要求快照。
        kept = self.call('GET', '/events')
        self.assertTrue(self.stream('?cursor=%d' % (kept['min_seq'] - 1)).startswith(('id: %d\n' % kept['min_seq']).encode()))
        self.app.store.prune_events(0)
        self.assertTrue(self.stream('?cursor=0').startswith(b'event: snapshot_required'))
        # 事件表清空时 min_seq 是下一个序号：补读为空的客户端以 min_seq-1 续流，不能再次被要求快照。
        empty = self.call('GET', '/events')
        self.assertEqual((empty['events'], self.stream('?cursor=%d' % (empty['min_seq'] - 1))), ([], b': ping\n\n'))


class BackupTest(ControllerCase):
    def fixture(self):
        node = self.node()
        job = self.job()
        a = node.claim()
        node.upload(b'confirmed')
        cp = node.checkpoint(a, manifest(b'confirmed'))
        return node, job, a, cp

    def test_backup_restore_increments_generation_and_fences_all_old_entries(self):
        node, job, assignment, cp = self.fixture()
        dest = self.temp / 'backup'
        result = backup(self.temp / 'data', dest, self.clock)
        self.assertEqual(result['authority_generation'], 1)
        self.assertEqual(len(result['blobs']), 3)
        self.app.close()
        self.app = None
        self.assertEqual(restore(self.temp / 'data', dest, self.clock), 2)
        self.app = App(self.temp / 'data', clock=self.clock)
        self.assertEqual(self.app.meta()['authority_generation'], 2)
        self.assertTrue(self.app.meta()['dispatch_paused'])
        for result in (node.checkpoint(assignment, status=409), node.complete(assignment, checkpoint=cp, status=409), node.publish(assignment, cp, status=409)):
            self.assertEqual(result['error']['code'], 'fenced')
            self.assertEqual(result['error']['details']['reason'], 'stale_generation')
        hb = node.heartbeat()
        self.assertTrue(hb['reconcile_required'])
        self.assertEqual(hb['revoked'][0]['reason'], 'stale_generation')
        node.claim(status=409)
        node.heartbeat(generation=2)
        node.claim(status=204)
        self.assertEqual(self.current(job)['status'], 'waiting_reconcile')
        self.app.close()
        self.app = None
        self.assertEqual(restore(self.temp / 'data', dest, self.clock), 3)
        self.app = App(self.temp / 'data', clock=self.clock)

    def test_backup_is_incremental_and_referenced_only(self):
        node, job, assignment, cp = self.fixture()
        unused = node.upload(b'unreferenced')
        dest = self.temp / 'backup'
        first = backup(self.temp / 'data', dest, self.clock)
        self.assertNotIn(unused, {b['sha256'] for b in first['blobs']})
        paths = list((dest / 'blobs').rglob('*'))
        mtimes = {str(path): path.stat().st_mtime_ns for path in paths if path.is_file()}
        backup(self.temp / 'data', dest, self.clock)
        self.assertEqual(mtimes, {str(path): path.stat().st_mtime_ns for path in paths if path.is_file()})
        self.assertEqual(self.call('GET', '/doctor')['backup_age_s'], 0)

    def test_corrupt_backups_rejected_without_replacing_live_database(self):
        node, job, assignment, cp = self.fixture()
        dest = self.temp / 'backup'
        backup(self.temp / 'data', dest, self.clock)
        self.app.close()
        self.app = None
        live = (self.temp / 'data' / 'controller.db').read_bytes()
        path = dest / 'blobs' / p.sha256_bytes(b'confirmed')[:2] / p.sha256_bytes(b'confirmed')[2:4] / p.sha256_bytes(b'confirmed')
        path.write_bytes(b'bad')
        with self.assertRaises(ValueError):
            restore(self.temp / 'data', dest, self.clock)
        self.assertEqual(live, (self.temp / 'data' / 'controller.db').read_bytes())
        path.write_bytes(b'confirmed')
        saved = json.loads((dest / 'backup-manifest.json').read_text())
        with open(str(dest / saved['database']), 'ab') as f:
            f.write(b'bad')
        with self.assertRaises(ValueError):
            restore(self.temp / 'data', dest, self.clock)
        self.assertEqual(live, (self.temp / 'data' / 'controller.db').read_bytes())
        self.app = App(self.temp / 'data', clock=self.clock)

    def test_restore_and_second_controller_rejected_while_locked(self):
        self.fixture()
        dest = self.temp / 'backup'
        backup(self.temp / 'data', dest, self.clock)
        with ControllerLock(self.temp / 'data'):
            with self.assertRaises(RuntimeError):
                with ControllerLock(self.temp / 'data'):
                    self.fail('second lock unexpectedly acquired')
            with self.assertRaises(RuntimeError):
                restore(self.temp / 'data', dest, self.clock)
        with ControllerLock(self.temp / 'data'):
            pass

    def test_missing_upload_is_backupable_but_missing_durable_blob_is_not(self):
        node, job, a, cp = self.fixture()
        node.checkpoint(a, manifest(b'partial'), status=409)
        backup(self.temp / 'data', self.temp / 'backup', self.clock)
        self.app.blobs.path(p.sha256_bytes(b'confirmed')).unlink()
        with self.assertRaises(ValueError):
            backup(self.temp / 'data', self.temp / 'backup', self.clock)

    def test_restore_preserves_sequence_and_fences_attempt_absent_from_backup(self):
        node = self.node()
        dest = self.temp / 'backup'
        backup(self.temp / 'data', dest, self.clock)
        job = self.job()
        assignment = node.claim()
        cp = node.checkpoint(assignment, kind='final')
        previous_seq = self.call('GET', '/events')['events'][-1]['seq']
        self.app.close()
        self.app = None
        restore(self.temp / 'data', dest, self.clock)
        self.app = App(self.temp / 'data', clock=self.clock)
        events = self.call('GET', '/events?after=' + str(previous_seq))['events']
        self.assertTrue(events)
        self.assertTrue(all(e['seq'] > previous_seq for e in events))
        for result in (node.checkpoint(assignment, status=409), node.complete(assignment, checkpoint=cp, status=409), node.publish(assignment, cp, status=409)):
            self.assertEqual(result['error']['details']['reason'], 'stale_generation')
        self.assertTrue(node.event(assignment)['stale'])

    def test_failed_manifest_switch_keeps_previous_backup_valid(self):
        self.fixture()
        dest = self.temp / 'backup'
        first = backup(self.temp / 'data', dest, self.clock)
        original = os.replace
        def fail_manifest(source, target):
            if str(target).endswith('backup-manifest.json'):
                raise OSError('simulated interruption')
            return original(source, target)
        with mock.patch('agent_collab.controller.os.replace', side_effect=fail_manifest):
            with self.assertRaises(OSError):
                backup(self.temp / 'data', dest, self.clock)
        self.assertEqual(json.loads((dest / 'backup-manifest.json').read_text()), first)
        self.app.close()
        self.app = None
        self.assertEqual(restore(self.temp / 'data', dest, self.clock), 2)
        self.app = App(self.temp / 'data', clock=self.clock)


class HTTPAndCLITest(ControllerCase):
    def test_bind_tls_policy(self):
        self.assertEqual(parse_bind('127.0.0.1:18669'), ('127.0.0.1', 18669))
        self.assertEqual(parse_bind('[::1]:18669'), ('::1', 18669))
        for bind in ('0.0.0.0:18669', '[::]:18669', 'example.com:18669', '127.0.0.1:0', ':18669'):
            with self.assertRaises(ValueError):
                parse_bind(bind)
        self.assertEqual(parse_bind('0.0.0.0:18669', 'cert', 'key'), ('0.0.0.0', 18669))
        with self.assertRaises(ValueError):
            parse_bind('127.0.0.1:18669', 'cert')

    def handler(self, method, path, body=b'', headers=None):
        handler = HTTPHandler.__new__(HTTPHandler)
        handler.command, handler.path = method, path
        handler.headers = Message()
        for key, value in (headers or {}).items():
            handler.headers[key] = value
        handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
        handler.connection = SimpleNamespace(settimeout=lambda _: None)
        handler.server = SimpleNamespace(app=self.app)
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        return handler

    def test_http_framing_and_body_limit_without_socket(self):
        for headers in ({}, {'Content-Length': '-1'}, {'Content-Length': '4', 'Transfer-Encoding': 'chunked'},
                        {'Content-Length': str(self.app.config['max_json_bytes'] + 1)}):
            handler = self.handler('POST', '/api/v1/jobs', headers=headers)
            handler._request()
            self.assertIn(handler.send_response.call_args.args[0], (400, 413))
            self.assertEqual(handler.rfile.tell(), 0)
        handler = self.handler('POST', '/api/v1/jobs', b'{}', {'Content-Length': '5'})
        handler._request()
        handler.send_response.assert_called_once_with(400)
        handler = self.handler('POST', '/api/v1/jobs', headers={'Content-Length': '0'})
        handler.headers['Content-Length'] = '0'
        with self.assertRaises(APIError):
            handler._length()

    def test_overview_reports_release_version_from_install_manifest(self):
        from agent_collab import controller as c
        from agent_collab import __version__
        self.assertEqual(c.release_info()['version'], __version__)  # 源码运行：无 release-manifest.json
        root = Path(self.temp) / 'versions' / '9.9.9'
        (root / 'agent_collab').mkdir(parents=True)
        (root / 'release-manifest.json').write_text(json.dumps({'version': '9.9.9', 'git_commit': 'abc'}), encoding='utf-8')
        self.assertEqual(c.release_info(root), {'version': '9.9.9', 'git_commit': 'abc'})

    def test_http_delete_without_content_length_is_empty_body(self):
        # 浏览器 fetch 的无体 DELETE 不带 Content-Length（UI 退出登录、释放协调权曾因此 400）。
        handler = self.handler('DELETE', '/api/v1/session', headers={'Authorization': 'Bearer ' + self.admin})
        self.assertEqual(handler._length(), 0)
        handler._request()
        handler.send_response.assert_called_once_with(200)
        with self.assertRaises(APIError):
            self.handler('DELETE', '/api/v1/session', headers={'Transfer-Encoding': 'chunked'})._length()

    def test_http_head_and_disconnect_without_socket(self):
        handler = self.handler('HEAD', '/api/v1/healthz')
        handler._request()
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(handler.wfile.getvalue(), b'')
        handler = self.handler('GET', '/api/v1/healthz')
        handler.wfile = mock.Mock()
        handler.wfile.write.side_effect = BrokenPipeError()
        handler._request()
        self.assertTrue(handler.close_connection)

    def test_http_sse_polls_new_events_and_detects_disconnect_without_socket(self):
        handler = self.handler('GET', '/api/v1/events/stream', headers={'Authorization': 'Bearer ' + self.admin})
        case = self
        class Stop:
            count = 0
            def is_set(self):
                return self.count >= 2
            def wait(self, _):
                self.count += 1
                if self.count == 1:
                    case.job()
        handler.server.stopping = Stop()
        handler._request()
        wire = handler.wfile.getvalue()
        self.assertIn(b'event: controller.started', wire)
        self.assertIn(b'event: job.created', wire)
        handler = self.handler('GET', '/api/v1/events/stream', headers={'Authorization': 'Bearer ' + self.admin})
        handler.server.stopping = Stop()
        handler.wfile = mock.Mock()
        handler.wfile.write.side_effect = BrokenPipeError()
        handler._request()
        self.assertTrue(handler.close_connection)

    def test_cli_admin_path_pairing_and_prune(self):
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, TMPDIR=str(root / '.dev' / 'tmp'), PYTHONDONTWRITEBYTECODE='1')
        self.clock.now = time.time()
        def run(*args):
            result = subprocess.run([sys.executable, '-m', 'agent_collab.controller'] + list(args) +
                                    ['--data-dir', str(self.temp / 'data')], cwd=str(root), env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()
        self.assertEqual(run('admin-token'), str(self.temp / 'data' / 'admin.token'))
        code = run('pairing-code', '--node-id', 'cli')
        self.call('POST', '/nodes/pair', {'node_id': 'cli', 'code': code}, headers={})
        self.assertNotIn(self.admin, run('admin-token'))
        run('prune-events', '--keep', '1')
        self.assertEqual(len(self.call('GET', '/events')['events']), 1)
