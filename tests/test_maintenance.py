"""维护 API、只读预检、拒绝优先恢复和屏障的隔离验收。"""
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import threading
import unittest
from unittest import mock

from agent_collab import cli, maintenance as m, protocol as p
from agent_collab.backup import BackupError, deterministic_tar, inspect_backup
from agent_collab.controller import App, HTTPHandler, backup, barrier, digest_token, file_hash
from helpers import contract, manifest, uid
from test_clients_models import ClientCase
from test_e2e_local import sockets_ok


def contents(root):
    return {path.relative_to(root).as_posix(): file_hash(path) for path in root.rglob('*') if path.is_file()}


class MaintenanceTests(ClientCase):
    def create_backup(self):
        result = self.call('POST', '/maintenance/backups', {'request_id': uid()})['maintenance_op']
        self.assertEqual(result['status'], 'succeeded', result)
        ident = result['result']['backup_id']
        return ident, self.temp / 'backups' / ident

    def reopen(self):
        self.app = App(self.temp / 'data', clock=self.clock)

    def close_app(self):
        self.app.close()
        self.app = None

    def test_maintenance_permissions_strict_parameters_and_request_replay(self):
        owner = self.pair('owner', name='owner')['client_token']
        viewer = self.pair('viewer', name='viewer')['client_token']
        body = dict(request_id=uid(), label='before upgrade')
        first = self.call('POST', '/maintenance/backups', body, token=owner)
        self.assertEqual(first['maintenance_op']['status'], 'succeeded', first)
        self.assertEqual(first, self.call('POST', '/maintenance/backups', body, token=owner))
        self.assertEqual(first['maintenance_op']['requested_by']['name'], 'owner')
        ident = first['maintenance_op']['result']['backup_id']
        routes = [('GET', '/maintenance/backups', None), ('POST', '/maintenance/backups', body),
                  ('GET', '/maintenance/operations/' + first['maintenance_op']['op_id'], None),
                  ('POST', '/maintenance/backups/' + ident + '/verify', {'request_id': uid()}),
                  ('GET', '/maintenance/backups/' + ident + '/archive', None),
                  ('GET', '/maintenance/restore-preview?backup_id=' + ident, None),
                  ('GET', '/maintenance/barrier-preview', None), ('GET', '/settings/backups', None),
                  ('PUT', '/settings/backups', {'request_id': uid(), 'expected_revision': 0, 'keep': 1})]
        for method, path, data in routes:
            with self.subTest(path=path):
                self.assertEqual(self.call(method, path, data, token=viewer, status=403)['error']['details']['required_scope'], 'maintain')
        for extra in ('path', 'dest', 'source', 'data_dir', 'backup_dir'):
            self.call('POST', '/maintenance/backups', dict(request_id=uid(), **{extra: '/tmp/x'}), status=400)
        self.call('POST', '/maintenance/backups?dest=/tmp/x', {'request_id': uid()}, status=400)
        self.call('GET', '/maintenance/restore-preview?backup_id=../../x', status=400)
        self.call('POST', '/maintenance/backups', dict(body, label='changed'), status=409)
        self.assertEqual(len(self.call('GET', '/maintenance/backups')['backups']), 1)
        self.assertEqual(first, self.call('GET', '/maintenance/operations/' + first['maintenance_op']['op_id']))

    def test_preview_project_pairing_and_revocation_even_when_disposable(self):
        old = self.pair('owner', name='old')
        ident, source = self.create_backup()
        project = self.call('POST', '/projects', dict(request_id=uid(), name='after'), status=201)
        new = self.pair('owner', name='new')
        self.call('POST', '/clients/' + old['client_id'] + '/revoke', dict(request_id=uid(), expected_version=1))
        preview = self.call('GET', '/maintenance/restore-preview?backup_id=' + ident)
        self.assertTrue(preview['disposable'])
        self.assertEqual(preview['verdict'], 'needs_reconcile')
        changes = {(v['kind'], v['id'], v['change']) for v in preview['changes']}
        self.assertIn(('project', project['project_id'], 'created'), changes)
        self.assertIn(('client', new['client_id'], 'created'), changes)
        self.assertIn(('client', old['client_id'], 'changed'), changes)
        self.assertIn('不会被撤回', preview['file_effects'])
        original = contents(source)
        self.close_app()
        result = m.restore_offline(self.temp / 'data', source, clock=self.clock)
        self.assertEqual(original, contents(source))
        self.assertIn(new['client_id'], [r['id'] for r in result['identity']['re_pair']])
        self.reopen()
        self.call('GET', '/client', token=old['client_token'], status=401)
        self.call('GET', '/client', token=new['client_token'], status=401)
        self.assertIsNone(self.app.store.get('client', new['client_id']))
        self.assertTrue(self.app.meta()['dispatch_paused'])
        self.assertEqual(self.app.meta()['authority_generation'], 2)
        receipt = json.loads((self.temp / 'restore-results' / (result['op_id'] + '.json')).read_text(encoding='utf-8'))
        event = next(e for e in self.app.store.events() if e['type'] == 'controller.restored')
        self.assertEqual(receipt, event['payload'])

    def test_deleted_rotated_retired_and_new_node_authorizations(self):
        old = self.pair('owner', name='deleted')
        retired, rotated = self.node('retired'), self.node('rotated')
        _, source = self.create_backup()
        with self.app.store.transaction():
            self.app.store.delete('client', old['client_id'])
            self.app.store.delete('client_token', digest_token(old['client_token']))
        n = self.app.store.get('node', retired.node_id)
        self.call('POST', '/nodes/retired/retire', dict(request_id=uid(), expected_version=n['version'], retired=True, reason='retire'))
        replaced = self.node('rotated')
        new = self.node('new')
        self.close_app()
        result = m.restore_offline(self.temp / 'data', source, identity='keep', clock=self.clock)
        self.assertEqual(result['identity']['policy'], 'carry-forward')
        self.reopen()
        for token in (old['client_token'], retired.token, rotated.token, replaced.token, new.token):
            self.call('GET', '/client', token=token, status=401)
        self.assertTrue(self.app.store.get('node', 'retired')['retired_at'])
        self.assertIn('new', [r['id'] for r in result['identity']['re_pair']])
        self.assertIn('rotated', [r['id'] for r in result['identity']['re_pair']])

    def test_unreadable_database_requires_identity_and_revoke_all_denies_credentials(self):
        client, node = self.pair('owner', name='old'), self.node()
        _, source = self.create_backup()
        self.close_app()
        (self.temp / 'data' / 'controller.db').write_bytes(b'not sqlite')
        for policy in (None, 'carry-forward'):
            with self.assertRaises(BackupError) as caught:
                m.restore_offline(self.temp / 'data', source, identity=policy, clock=self.clock)
            self.assertEqual(caught.exception.code, 'identity_required')
        result = m.restore_offline(self.temp / 'data', source, identity='revoke-all', clock=self.clock)
        self.assertEqual(result['identity']['policy'], 'revoke-all')
        self.reopen()
        for token in (client['client_token'], node.token):
            self.call('GET', '/client', token=token, status=401)
        self.assertFalse(self.app.store.all('node_token'))
        self.assertFalse(self.app.store.all('client_token'))

    def test_inspect_verify_restore_archive_preserve_every_source_file(self):
        node = self.node()
        self.job()
        attempt = node.claim()
        node.upload(b'payload')
        node.checkpoint(attempt, manifest(b'payload'))
        ident, source = self.create_backup()
        (source / 'unrelated.lock').write_text('untouched')
        original = contents(source)
        request = dict(request_id=uid())
        first = self.call('POST', '/maintenance/backups/' + ident + '/verify', request)
        self.assertEqual(first, self.call('POST', '/maintenance/backups/' + ident + '/verify', request))
        archives = []
        for _ in range(2):
            response = self.app.handle('GET', p.API_PREFIX + '/maintenance/backups/' + ident + '/archive',
                                       {'authorization': 'Bearer ' + self.admin}, b'')
            content = b''.join(response.stream)
            self.assertEqual(dict(response.headers)['X-Content-SHA256'], p.sha256_bytes(content))
            self.assertEqual(int(dict(response.headers)['Content-Length']), len(content))
            archives.append(content)
        self.assertEqual(*archives)
        archive = self.temp / 'export.tar'
        archive.write_bytes(archives[0])
        with tarfile.open(str(archive)) as tar:
            self.assertTrue(all(not n.endswith(('.lock', '-wal', '-shm')) for n in tar.getnames()))
        info = inspect_backup(archive, self.temp)
        self.assertTrue(info['ok'])
        self.close_app()
        m.restore_offline(self.temp / 'data', archive, clock=self.clock)
        self.assertEqual(original, contents(source))
        self.reopen()

    def test_corrupt_database_missing_blob_and_schema_diagnostics_are_atomic(self):
        node = self.node()
        self.job()
        a = node.claim()
        node.upload(b'content')
        node.checkpoint(a, manifest(b'content'))
        ident, source = self.create_backup()
        saved = json.loads((source / 'backup-manifest.json').read_text(encoding='utf-8'))
        digest = p.sha256_bytes(b'content')
        blob = source / 'blobs' / digest[:2] / digest[2:4] / digest
        blob.unlink()
        self.close_app()
        live = contents(self.temp / 'data')
        with self.assertRaises(BackupError) as caught:
            m.restore_offline(self.temp / 'data', source, clock=self.clock)
        self.assertEqual(caught.exception.code, 'backup_object_missing')
        self.assertIn(digest, str(caught.exception))
        self.assertEqual(live, contents(self.temp / 'data'))
        blob.write_bytes(b'content')
        database = source / saved['database']
        database.write_bytes(b'corrupt')
        with self.assertRaises(BackupError) as caught:
            inspect_backup(source, self.temp)
        self.assertEqual(caught.exception.code, 'backup_database_hash')
        changed = file_hash(database)
        database.rename(source / ('controller-' + changed + '.db'))
        saved.update(db_sha256=changed, database='controller-' + changed + '.db')
        (source / 'backup-manifest.json').write_text(json.dumps(saved))
        with self.assertRaises(BackupError) as caught:
            m.restore_offline(self.temp / 'data', source, clock=self.clock)
        self.assertEqual(caught.exception.code, 'backup_database_corrupt')
        self.assertEqual(live, contents(self.temp / 'data'))
        self.reopen()
        preview = self.call('GET', '/maintenance/restore-preview?backup_id=' + ident)
        self.assertEqual(preview['verdict'], 'blocked')
        request = dict(request_id=uid())
        failure = self.call('POST', '/maintenance/backups/' + ident + '/verify', request)
        self.assertEqual(failure['maintenance_op']['status'], 'failed')
        self.assertEqual(failure, self.call('POST', '/maintenance/backups/' + ident + '/verify', request))

    def test_readonly_previews_do_not_backup_expire_or_write_database(self):
        ident, source = self.create_backup()
        self.node()
        self.job()
        self.clock.advance(10000)
        before = self.app.store.db.total_changes
        snapshot = m.records(self.app.store.db)
        with mock.patch('agent_collab.controller.backup', side_effect=AssertionError('not read only')), \
                mock.patch.object(self.app, '_expire', side_effect=AssertionError('expiry writes')):
            for path in ('/maintenance/restore-preview?backup_id=' + ident, '/maintenance/barrier-preview',
                         '/maintenance/backups', '/settings/backups'):
                self.call('GET', path)
        self.assertEqual(before, self.app.store.db.total_changes)
        self.assertEqual(snapshot, m.records(self.app.store.db))
        self.close_app()
        before_files = contents(self.temp / 'data')
        m.restore_offline(self.temp / 'data', source, dry_run=True, clock=self.clock)
        self.assertEqual(before_files, contents(self.temp / 'data'))
        self.reopen()

    def test_preview_covers_advanced_jobs_settings_and_local_remote_applications(self):
        node = self.node()
        job = self.job()
        ident, _ = self.create_backup()
        a = node.claim()
        cp = node.checkpoint(a, kind='final')
        node.complete(a, checkpoint=cp)
        node.publish(a, cp)
        with self.app.store.transaction():
            self.app.store.put('settings', 'coordination', dict(revision=2, enabled=False, node_ids=[]))
            for status in ('started', 'unknown', 'applied', 'partial'):
                self.app.store.put('application', status, dict(application_id=status, job_id=job['job_id'], status=status,
                                                              target_node_id='a' if status in ('partial', 'started') else None))
        preview = self.call('GET', '/maintenance/restore-preview?backup_id=' + ident)
        self.assertEqual(preview['verdict'], 'needs_reconcile')
        self.assertTrue({'job', 'attempt', 'checkpoint', 'publication', 'application', 'settings'} <= {x['kind'] for x in preview['changes']})
        self.assertEqual({a['status'] for a in preview['unresolved_applications']}, {'started', 'unknown'})

    def test_retention_keeps_latest_valid_and_unique_valid_and_upgrade(self):
        first, source = self.create_backup()
        self.clock.advance(1)
        latest, _ = self.create_backup()
        upgrade = self.temp / 'backups' / 'upgrade-test'
        backup(self.temp / 'data', upgrade, self.clock)
        broken = self.temp / 'backups' / 'managed-broken'
        broken.mkdir()
        body = dict(request_id=uid(), expected_revision=0, keep=1)
        result = self.call('PUT', '/settings/backups', body)
        self.assertEqual(result['maintenance_op']['status'], 'succeeded')
        self.assertEqual(result, self.call('PUT', '/settings/backups', body))
        self.assertFalse(source.exists())
        self.assertTrue((self.temp / 'backups' / latest).exists())
        self.assertTrue(upgrade.exists())
        self.assertFalse(broken.exists())
        broken.mkdir()
        self.call('PUT', '/settings/backups', dict(request_id=uid(), expected_revision=1, keep=1))
        self.assertTrue((self.temp / 'backups' / latest).exists())
        self.call('PUT', '/settings/backups', dict(request_id=uid(), expected_revision=1, keep=2), status=409)
        self.call('PUT', '/settings/backups', dict(request_id=uid(), expected_revision=2, keep=0), status=400)
        listed = self.call('GET', '/maintenance/backups')['backups']
        self.assertEqual({x['kind'] for x in listed}, {'managed', 'upgrade'})
        self.assertTrue(all(x['location'] == 'instance' and x['schema']['fingerprint'] and x['valid'] for x in listed))

    def test_pending_writebacks_deduplicates_application_and_recovery_operations(self):
        self.node('a')
        self.node('b')
        with self.app.store.transaction():
            for ident, target, status in [('shared', 'a', 'started'), ('orphan', 'a', 'started'), ('done', 'a', 'applied'),
                                          ('local', None, 'started'), ('other', 'b', 'started')]:
                self.app.store.put('application', ident, dict(application_id=ident, target_node_id=target, status=status))
            for i, (kind, status, application) in enumerate([('project_apply', 'requested', 'shared'), ('project_recover', 'received', 'shared'),
                    ('project_apply', 'requested', 'pending'), ('project_recover', 'received', 'pending'),
                    ('project_plan', 'requested', 'plan'), ('probe', 'received', 'probe'), ('project_apply', 'failed', 'done')]):
                self.app.store.put('node_op', str(i), dict(op_id=str(i), node_id='a', kind=kind, status=status,
                    params=dict(application_id=application), deadline_at='2099-01-01T00:00:00.000Z', finished_at=None))
            self.app.store.put('integration_target', 'it', dict(target_node_id='b', pending=dict(job_id='job-b')))
            self.app.store.put('integration_target', 'idle', dict(target_node_id='a', pending=None))
        nodes = {n['node_id']: n for n in self.call('GET', '/nodes')['nodes']}
        self.assertEqual(nodes['a']['pending_writebacks'], 3)
        self.assertEqual(nodes['b']['pending_writebacks'], 2)

    def successful(self):
        node = self.node()
        job = self.job(contract(publish={'mode': 'manual', 'requires_acceptance': False}))
        a = node.claim()
        cp = node.checkpoint(a, kind='final')
        node.complete(a, checkpoint=cp)
        return node, job, a, cp

    def pause_close(self):
        meta = self.app.meta()
        self.call('POST', '/dispatch', dict(request_id=uid(), expected_version=meta['version'], paused=True))
        self.call('POST', '/intake', dict(request_id=uid(), expected_version=self.app.meta()['version'], mode='closed', reason='cutover'))

    def test_barrier_blocks_and_dismissal_is_visible_idempotent_and_unpublishable(self):
        node, job, a, cp = self.successful()
        preview = self.call('GET', '/maintenance/barrier-preview')
        self.assertEqual(preview['pending_results'][0]['checkpoint_id'], cp['checkpoint_id'])
        self.pause_close()
        self.close_app()
        with self.assertRaises(BackupError) as caught:
            barrier(self.temp / 'data', uid(), 'cutover', self.clock)
        self.assertEqual(caught.exception.details['pending_results'][0]['job_id'], job['job_id'])
        self.reopen()
        stale = dict(request_id=uid(), expected_version=self.current(job)['version'], reason='不要这个成果', checkpoint_id=uid())
        self.assertEqual(self.call('POST', '/jobs/' + job['job_id'] + '/dismiss', stale, status=409)['error']['code'], 'conflict')
        body = dict(request_id=uid(), expected_version=self.current(job)['version'], reason='不要这个成果', checkpoint_id=cp['checkpoint_id'])
        result = self.call('POST', '/jobs/' + job['job_id'] + '/dismiss', body)
        self.assertEqual(result, self.call('POST', '/jobs/' + job['job_id'] + '/dismiss', body))
        self.assertEqual(self.current(job)['dismissed_checkpoint_id'], cp['checkpoint_id'])
        publish = dict(request_id=uid(), attempt_id=a['attempt_id'], checkpoint_id=cp['checkpoint_id'],
                       expected_base_version=0, expected_version=result['version'], epoch=a['epoch'])
        error = self.call('POST', '/jobs/' + job['job_id'] + '/publish', publish, status=409)
        self.assertEqual(error['error']['code'], 'checkpoint_dismissed')
        self.assertTrue(self.call('GET', '/maintenance/barrier-preview')['ready'])
        self.close_app()
        self.assertEqual(barrier(self.temp / 'data', uid(), 'cutover', self.clock)['authority_generation'], 2)
        self.reopen()

    def test_barrier_rechecks_after_preview_under_the_same_write_transaction(self):
        self.assertEqual(self.call('GET', '/maintenance/barrier-preview')['pending_results'], [])
        node, job, a, cp = self.successful()  # created after the preview
        self.pause_close()
        self.close_app()
        original = m.pending_results
        blocked_writer = []
        def check(store):
            self.assertTrue(store.db.in_transaction)
            db = sqlite3.connect(str(self.temp / 'data' / 'controller.db'), timeout=0)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute('BEGIN IMMEDIATE')
                blocked_writer.append(True)
            finally:
                db.close()
            return original(store)
        with mock.patch.object(m, 'pending_results', side_effect=check):
            with self.assertRaises(BackupError):
                barrier(self.temp / 'data', uid(), 'cutover', self.clock)
        self.assertEqual(blocked_writer, [True])
        self.reopen()
        current = self.current(job)
        self.call('POST', '/jobs/' + job['job_id'] + '/publish', dict(request_id=uid(), attempt_id=a['attempt_id'],
            checkpoint_id=cp['checkpoint_id'], expected_base_version=0, expected_version=current['version'], epoch=a['epoch']))
        self.assertEqual(self.call('GET', '/maintenance/barrier-preview')['pending_results'], [])
        self.close_app()
        barrier(self.temp / 'data', uid(), 'cutover', self.clock)
        self.reopen()

    def test_offline_cli_inspect_dry_run_explicit_identity_and_tar_traversal(self):
        _, source = self.create_backup()
        self.close_app()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli.main(['maintenance', 'inspect', '--from', str(source)]), 0)
        self.assertTrue(json.loads(output.getvalue())['ok'])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(m.main(['restore', '--data-dir', str(self.temp / 'new'), '--from', str(source), '--dry-run']), 2)
        self.assertEqual(json.loads(output.getvalue())['error']['code'], 'identity_required')
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(m.main(['restore', '--data-dir', str(self.temp / 'new'), '--from', str(source), '--identity', 'keep', '--dry-run']), 0)
        self.assertFalse((self.temp / 'new').exists())
        bad = self.temp / 'bad.tar'
        with tarfile.open(str(bad), 'w') as archive:
            entry = tarfile.TarInfo('../escape')
            entry.size = 1
            archive.addfile(entry, io.BytesIO(b'x'))
        with self.assertRaises(BackupError) as caught:
            inspect_backup(bad, self.temp)
        self.assertEqual(caught.exception.code, 'backup_unsafe_archive')
        self.assertFalse((self.temp / 'escape').exists())
        self.reopen()

    def test_online_cli_backup_verify_retention_and_hash_checked_export(self):
        def request(url, token, method, path, data=None, ca_file=None, controller_id=None, headers=None):
            reply = self.app.handle(method, p.API_PREFIX + path, {'authorization': 'Bearer ' + token},
                                    p.canonical_json(data) if data is not None else b'')
            return reply.status, json.loads(reply.body)
        def run(args):
            output = io.StringIO()
            with redirect_stdout(output):
                code = m.main(args[:1] + ['--token-file', str(self.temp / 'data' / 'admin.token')] + args[1:])
            self.assertEqual(code, 0, output.getvalue())
            return json.loads(output.getvalue())
        with mock.patch.object(cli, 'request', side_effect=request):
            made = run(['backup', '--request-id', uid()])['maintenance_op']
            ident = made['result']['backup_id']
            self.assertEqual(run(['backups'])['backups'][0]['id'], ident)
            self.assertEqual(run(['verify', ident])['maintenance_op']['status'], 'succeeded')
            self.assertEqual(run(['restore-preview', ident])['verdict'], 'safe')
            self.assertFalse(run(['barrier-preview'])['ready'])
            self.assertEqual(run(['retention', 'set', '--keep', '2'])['maintenance_op']['result']['settings']['keep'], 2)
        class Download(io.BytesIO):
            def getcode(self):
                return 200
        reply = self.app.handle('GET', p.API_PREFIX + '/maintenance/backups/' + ident + '/archive',
                                {'authorization': 'Bearer ' + self.admin}, b'')
        raw = b''.join(reply.stream)
        def download(*args, **kwargs):
            stream = Download(raw)
            stream.headers = dict(reply.headers)
            return stream
        destination = self.temp / 'download.tar'
        with mock.patch.object(cli, '_open', side_effect=download):
            result = run(['export', ident, '--to', str(destination)])
        self.assertEqual(result['sha256'], file_hash(destination))
        self.assertTrue(inspect_backup(destination, self.temp)['ok'])
        def corrupt(*args, **kwargs):
            stream = download()
            stream.headers['X-Content-SHA256'] = '0' * 64
            return stream
        with mock.patch.object(cli, '_open', side_effect=corrupt), redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(['export', '--token-file', str(self.temp / 'data' / 'admin.token'),
                                     ident, '--to', str(destination)]), 2)
        self.assertEqual(file_hash(destination), result['sha256'])

    @unittest.skipUnless(sockets_ok(), 'loopback socket unavailable')
    def test_http_archive_finishes_as_tar_without_event_stream_suffix(self):
        ident, _ = self.create_backup()
        server = ThreadingHTTPServer(('127.0.0.1', 0), HTTPHandler)
        server.app, server.stopping = self.app, threading.Event()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with cli._open('http://127.0.0.1:%d' % server.server_port, self.admin, 'GET',
                           '/maintenance/backups/' + ident + '/archive', controller_id=self.app.controller_id) as result:
                raw = result.read()
                self.assertEqual(len(raw), int(result.headers['Content-Length']))
                self.assertEqual(p.sha256_bytes(raw), result.headers['X-Content-SHA256'])
        finally:
            server.stopping.set()
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_incompatible_schema_is_blocked_and_source_unchanged(self):
        ident, source = self.create_backup()
        saved = json.loads((source / 'backup-manifest.json').read_text(encoding='utf-8'))
        path = source / saved['database']
        db = sqlite3.connect(str(path))
        db.execute('PRAGMA user_version=999')
        db.close()
        digest = file_hash(path)
        saved.update(db_sha256=digest, database='controller-' + digest + '.db')
        path.rename(source / saved['database'])
        (source / 'backup-manifest.json').write_text(json.dumps(saved))
        before = contents(source)
        preview = self.call('GET', '/maintenance/restore-preview?backup_id=' + ident)
        self.assertEqual(preview['verdict'], 'blocked')
        self.assertEqual(preview['error']['code'], 'backup_schema_incompatible')
        self.assertEqual(before, contents(source))

    def test_acceptance_only_preview_safe_but_history_gap_needs_reconcile(self):
        ident, _ = self.create_backup()
        self.job(acceptance_run=True)
        preview = self.call('GET', '/maintenance/restore-preview?backup_id=' + ident)
        self.assertEqual(preview['verdict'], 'safe', preview)
        self.app.store.prune_events(1)
        self.assertEqual(self.call('GET', '/maintenance/restore-preview?backup_id=' + ident)['verdict'], 'needs_reconcile')

    def test_download_of_a_published_job_registered_after_backup_needs_reconcile(self):
        node = self.node()
        job = self.job()
        a = node.claim()
        final = node.checkpoint(a, kind='final')
        node.complete(a, checkpoint=final)
        self.call('POST', '/jobs/' + job['job_id'] + '/publish', dict(request_id=uid(), attempt_id=a['attempt_id'],
            checkpoint_id=final['checkpoint_id'], expected_base_version=0, expected_version=self.current(job)['version'], epoch=a['epoch']))
        ident, source = self.create_backup()
        self.assertEqual(m.restore_preview(self.app.store, source, activity={'disposable': True})['verdict'], 'safe')
        self.call('POST', '/jobs/%s/downloads' % job['job_id'],
                  dict(request_id=uid(), checkpoint_id=final['checkpoint_id'], files=1, bytes=1), status=201)
        preview = m.restore_preview(self.app.store, source, activity={'disposable': True})
        self.assertEqual(preview['verdict'], 'needs_reconcile')
        self.assertEqual([c['kind'] for c in preview['changes']], ['download'])

    def test_download_registered_after_backup_needs_reconcile(self):
        job = self.job()
        ident, source = self.create_backup()
        self.assertEqual(m.restore_preview(self.app.store, source, activity={'disposable': True})['verdict'], 'safe')
        with self.app.store.transaction():
            self.app.store.put('download', 'd1', dict(download_id='d1', job_id=job['job_id'], checkpoint_id='cp'))
        preview = m.restore_preview(self.app.store, source, activity={'disposable': True})
        self.assertEqual(preview['verdict'], 'needs_reconcile')
        self.assertIn('download', [c['kind'] for c in preview['changes']])

    def test_restore_does_not_redispatch_snapshot_writebacks_and_carries_scope_reduction(self):
        client = self.pair('owner', name='reduced')
        node = self.node()
        with self.app.store.transaction():
            self.app.store.put('node_op', 'pending', dict(op_id='pending', node_id=node.node_id,
                kind='project_apply', status='received', params=dict(application_id='app'),
                deadline_at='2099-01-01T00:00:00.000Z', finished_at=None))
            self.app.store.put('application', 'app', dict(application_id='app', target_node_id=node.node_id, status='started'))
        _, source = self.create_backup()
        with self.app.store.transaction():
            value = self.app.store.get('client', client['client_id'])
            value['scopes'] = ['read']
            self.app.store.put('client', client['client_id'], value)
        self.close_app()
        result = m.restore_offline(self.temp / 'data', source, clock=self.clock)
        self.assertEqual(result['result']['unsettled_operations'][0]['op_id'], 'pending')
        self.reopen()
        self.assertEqual(self.app.store.get('node_op', 'pending')['status'], 'timed_out')
        self.assertEqual(self.app.store.get('application', 'app')['status'], 'started')
        self.assertEqual(self.call('GET', '/client', token=client['client_token'])['identity']['scopes'], ['read'])
        self.call('GET', '/maintenance/backups', token=client['client_token'], status=403)

    def test_coordination_rounds_are_not_pending_results(self):
        # 受管协调回合的检查点只是它的决定，不是用户会采用的成果；屏障不因它阻断。
        node = self.node()
        job = self.job()
        a = node.claim()
        final = node.checkpoint(a, kind='final')
        node.complete(a, checkpoint=final)
        self.assertEqual(len(self.call('GET', '/maintenance/barrier-preview')['pending_results']), 1)
        with self.app.store.transaction():
            record = self.app.store.get('job', job['job_id'])
            record['contract']['coordinates'] = {'job_id': uid()}
            self.app.store.put('job', job['job_id'], record)
        self.assertEqual(self.call('GET', '/maintenance/barrier-preview')['pending_results'], [])

    def test_barrier_ignores_older_durable_checkpoints_after_latest_is_published(self):
        node = self.node()
        job = self.job()
        a = node.claim()
        node.checkpoint(a, kind='boundary')
        final = node.checkpoint(a, kind='final')
        node.complete(a, checkpoint=final)
        node.publish(a, final)
        self.assertEqual(self.call('GET', '/maintenance/barrier-preview')['pending_results'], [])
        self.pause_close()
        self.close_app()
        barrier(self.temp / 'data', uid(), 'cutover', self.clock)
        self.reopen()

    def test_directory_verification_rejects_outside_blob_symlink(self):
        node = self.node()
        self.job()
        a = node.claim()
        node.upload(b'content')
        node.checkpoint(a, manifest(b'content'))
        _, source = self.create_backup()
        digest = p.sha256_bytes(b'content')
        blob = source / 'blobs' / digest[:2] / digest[2:4] / digest
        external = self.temp / 'external'
        external.write_bytes(b'content')
        blob.unlink()
        try:
            blob.symlink_to(external)
        except OSError:
            self.skipTest('symlink unavailable')
        with self.assertRaises(BackupError) as caught:
            inspect_backup(source, self.temp)
        self.assertEqual(caught.exception.code, 'backup_unsafe_path')
