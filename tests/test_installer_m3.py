"""M3 update journal, offline health, writeback draining and OpenSSH trust contracts."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import unittest
from unittest.mock import Mock, patch

from agent_collab import installer as ins
from agent_collab.release_tools import evaluate_compat
import test_installer as legacy


class ProcessKilled(BaseException):
    pass


class InstallerM3Tests(unittest.TestCase):
    setUpClass = classmethod(legacy.InstallerTests.setUpClass.__func__)
    tearDownClass = classmethod(legacy.InstallerTests.tearDownClass.__func__)
    setUp = legacy.InstallerTests.setUp
    args = legacy.InstallerTests.args
    invoke = legacy.InstallerTests.invoke
    build_modified = legacy.InstallerTests.build_modified

    def stopped_install(self):
        self.invoke('--release', str(self.v1), role='worker')
        old = ins.read_json(self.prefix / 'installation.json')
        old['desired'] = {'worker': 'stopped'}
        # Exercise service-enabled recovery without real service commands or socket listeners.
        old['config']['service'] = True
        ins.write_json(self.prefix / 'installation.json', old)
        ins.write_json(self.state / 'worker/agent.json', {'node_id': old['config']['node_id'],
                       'controller': old['config']['controller_url']})
        (self.state / 'worker/node.token').write_text('fixture-node-token')
        return old

    def fakes(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name in ('check_ownership', 'stop', 'register'):
            stack.enter_context(patch.object(ins.Services, name))
        self.started = stack.enter_context(patch.object(ins.Services, 'start', side_effect=AssertionError('stopped service started')))
        stack.enter_context(patch.object(ins.Services, 'status', return_value={'worker': 'inactive'}))
        stack.enter_context(patch.object(ins.Services, 'autostart', return_value={'worker': 'disabled'}))
        stack.enter_context(patch.object(ins.Services, 'ownership', return_value={'worker': 'this_prefix'}))
        stack.enter_context(patch.object(ins, 'release_compatibility', return_value={'decision': 'accept'}))
        stack.enter_context(patch.object(socket.socket, 'bind', side_effect=AssertionError('listener')))
        stack.enter_context(patch.object(ins, 'pid_alive', return_value=False))
        # The installer refuses worker services for root; test runners (CI containers) may run as root.
        stack.enter_context(patch.object(ins.os, 'getuid', return_value=501, create=True))
        return stack

    def main(self, *args, expected=0):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            code = ins.main(['--prefix', str(self.prefix), '--health-timeout', '0'] + list(args))
        self.assertEqual(code, expected, out.getvalue() + err.getvalue())
        return out.getvalue() + err.getvalue()

    def kill_switch(self, after):
        switch = ins.switch_current
        def terminate(prefix, version, system=None):
            if after:
                switch(prefix, version, system)
            raise ProcessKilled()
        with patch.object(ins, 'switch_current', side_effect=terminate), self.assertRaises(ProcessKilled):
            self.main('--release', str(self.v2))
        journal = ins.read_json(self.prefix / 'update-journal.json')
        self.assertEqual(journal['status'], 'running')
        self.assertEqual([s['name'] for s in journal['steps']], list(ins.JOURNAL_STEPS))
        self.assertEqual(journal['steps'][4]['status'], 'running')
        self.assertTrue((self.prefix / '.install-lock').is_dir())
        return journal

    def test_killed_before_switch_discovery_read_only_recovery_restores_old_stopped(self):
        self.stopped_install()
        with self.fakes():
            self.kill_switch(False)
            before = legacy.hashes(self.temp)
            with patch.object(ins, 'atomic_write', side_effect=AssertionError('read-only discovery')):
                info = json.loads(self.main('--discover'))
            self.assertEqual(info['interrupted']['status'], 'interrupted')
            self.assertEqual(info['interrupted']['current'], '1.0.0')
            self.assertEqual(info['interrupted']['recorded'], '1.0.0')
            self.assertEqual(legacy.hashes(self.temp), before)
            self.assertIn('--recover', self.main('--release', str(self.v2), expected=1))
            self.main('--recover')
            self.started.assert_not_called()
        self.assertEqual(ins.current_version(self.prefix), '1.0.0')
        self.assertEqual(ins.read_json(self.prefix / 'update-journal.json')['status'], 'failed')
        self.assertEqual(ins.read_json(self.prefix / 'installation.json')['desired'], {'worker': 'stopped'})
        self.assertFalse((self.prefix / '.install-lock').exists())

    def test_killed_after_switch_recovery_completes_new_stopped_version(self):
        self.stopped_install()
        with self.fakes():
            self.kill_switch(True)
            before = legacy.hashes(self.temp)
            info = json.loads(self.main('--discover'))
            self.assertEqual(info['interrupted']['current'], '2.0.0')
            self.assertEqual(info['interrupted']['recorded'], '1.0.0')
            self.assertEqual(legacy.hashes(self.temp), before)
            self.main('--recover')
            self.started.assert_not_called()
        journal = ins.read_json(self.prefix / 'update-journal.json')
        self.assertEqual(journal['status'], 'done')
        self.assertTrue(all(s['status'] == 'done' for s in journal['steps']))
        self.assertEqual(journal['steps'][-1]['result']['components']['worker']['status'], 'offline')
        record = ins.read_json(self.prefix / 'installation.json')
        self.assertEqual(record['version'], '2.0.0')
        self.assertEqual(record['previous']['version'], '1.0.0')
        self.assertEqual(record['desired'], {'worker': 'stopped'})

    def test_windows_current_txt_interruption_recovers_stopped_version(self):
        self.stopped_install()
        old = ins.read_json(self.prefix / 'installation.json')
        old['config']['system'] = 'Windows'
        ins.write_json(self.prefix / 'installation.json', old)
        ins.switch_current(self.prefix, '1.0.0', 'Windows')
        with self.fakes(), patch.object(ins.platform, 'system', return_value='Windows'), \
                patch.object(ins.Services, 'render'):
            self.kill_switch(True)
            self.main('--recover')
            self.started.assert_not_called()
        self.assertEqual((self.prefix / 'current.txt').read_text().strip(), '2.0.0')
        self.assertEqual(ins.read_json(self.prefix / 'update-journal.json')['status'], 'done')

    def test_first_install_killed_before_switch_recovers_failed_and_can_retry(self):
        with patch.object(ins, 'switch_current', side_effect=ProcessKilled()), self.assertRaises(ProcessKilled):
            self.invoke('--release', str(self.v1), role='worker')
        with patch.object(ins, 'pid_alive', return_value=False):
            self.invoke('--recover', role='worker')
        self.assertEqual(ins.read_json(self.prefix / 'update-journal.json')['status'], 'failed')
        self.assertIsNone(ins.current_version(self.prefix))
        self.invoke('--release', str(self.v1), role='worker')
        self.assertEqual(ins.current_version(self.prefix), '1.0.0')

    def test_recovery_can_resume_after_a_second_process_kill(self):
        self.stopped_install()
        with self.fakes():
            self.kill_switch(True)
            with patch.object(ins, 'write_launchers', side_effect=ProcessKilled()), self.assertRaises(ProcessKilled):
                self.main('--recover')
            self.main('--recover')
            self.started.assert_not_called()
        self.assertEqual(ins.read_json(self.prefix / 'update-journal.json')['status'], 'done')

    def test_unfinished_update_refuses_service_changes(self):
        self.stopped_install()
        with self.fakes():
            self.kill_switch(False)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
                self.assertEqual(ins.main(['service', 'start', '--prefix', str(self.prefix)]), 1)
            self.assertIn('--recover', error.getvalue())
            self.started.assert_not_called()

    def test_killed_after_switch_explicit_rollback_preserves_stopped(self):
        self.stopped_install()
        with self.fakes():
            self.kill_switch(True)
            self.main('--recover', '--rollback')
            self.started.assert_not_called()
        self.assertEqual(ins.current_version(self.prefix), '1.0.0')
        self.assertEqual(ins.read_json(self.prefix / 'update-journal.json')['status'], 'failed')
        self.assertEqual(ins.read_json(self.prefix / 'installation.json')['desired'], {'worker': 'stopped'})

    def test_uncertain_pointer_remains_pending_and_never_starts(self):
        self.stopped_install()
        with self.fakes():
            self.kill_switch(True)
            ins.switch_current(self.prefix, '9.9.9')
            self.main('--recover', expected=1)
            self.started.assert_not_called()
        self.assertEqual(ins.current_version(self.prefix), '9.9.9')
        self.assertEqual(ins.read_json(self.prefix / 'update-journal.json')['status'], 'pending')
        self.assertTrue((self.prefix / '.install-lock').exists())

    def test_live_or_uncheckable_pid_refuses_recovery_without_writes(self):
        self.stopped_install()
        with self.fakes():
            self.kill_switch(False)
            before = legacy.hashes(self.temp)
            for alive in (True, None):
                with patch.object(ins, 'pid_alive', return_value=alive):
                    self.main('--recover', expected=1)
            self.assertEqual(legacy.hashes(self.temp), before)

    def test_recovery_schema_rejection_is_pending(self):
        self.stopped_install()
        with self.fakes():
            self.kill_switch(True)
            with patch.object(ins, 'compatible_schema', side_effect=ValueError('schema incompatible')):
                self.main('--recover', expected=1)
            self.started.assert_not_called()
        self.assertEqual(ins.read_json(self.prefix / 'update-journal.json')['status'], 'pending')
        self.assertEqual(ins.current_version(self.prefix), '2.0.0')

    def test_recovery_restores_persisted_drain_after_kill_during_prepare(self):
        self.invoke('--release', str(self.v1), role='worker')
        old = ins.read_json(self.prefix / 'installation.json')
        worker = self.state / 'worker'
        ins.write_json(worker / 'agent.json', {'controller': old['config']['controller_url'], 'node_id': 'test'})
        (worker / 'node.token').write_text('fixture')
        def drain(*args, before_drain=None, **kwargs):
            before_drain(False)
            raise ProcessKilled()
        client = Mock()
        with patch.object(ins, 'drain_client', return_value=(client, 'test')), \
                patch.object(ins, 'drain_and_wait', side_effect=drain), self.assertRaises(ProcessKilled):
            self.invoke('--release', str(self.v2), role='worker')
        journal = ins.read_json(self.prefix / 'update-journal.json')
        self.assertTrue(journal['drain']['restore_required'])
        with patch.object(ins, 'pid_alive', return_value=False), \
                patch.object(ins, 'drain_client', return_value=(client, 'test')), patch.object(ins, 'set_drain') as restore:
            self.invoke('--recover', role='worker')
        restore.assert_called_once_with(client, 'test', False)
        self.assertFalse(ins.read_json(self.prefix / 'update-journal.json')['drain']['restore_required'])

    def test_failed_automatic_rollback_is_pending(self):
        self.invoke('--release', str(self.v1))
        check = ins.compatible_schema
        def schema(config, manifest, directory):
            if manifest['version'] == '1.0.0':
                raise ValueError('new schema cannot be read by old code')
            return check(config, manifest, directory)
        with patch.object(ins, 'wait_doctor', side_effect=RuntimeError('health failed')), \
                patch.object(ins, 'compatible_schema', side_effect=schema):
            self.invoke('--release', str(self.v2), status=1)
        self.assertEqual(ins.read_json(self.prefix / 'update-journal.json')['status'], 'pending')
        self.assertTrue((self.prefix / '.install-lock').exists())

    def test_uninstall_keeps_the_recorded_service_mode(self):
        self.invoke('--release', str(self.v1), role='worker')  # args() 带 --no-service
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output), patch.object(ins, 'run', side_effect=AssertionError('service commands')):
            code = ins.main([a for a in self.args('worker') if a != '--no-service'] + ['--uninstall'])
        self.assertEqual(code, 0, output.getvalue())
        self.assertIsNone(ins.current_version(self.prefix))
        # 反过来：服务模式安装的前缀带着 --no-service 卸载，也沿用记录的模式（服务照常移除）。
        self.invoke('--release', str(self.v1), role='worker')
        record = ins.read_json(self.prefix / 'installation.json')
        record['config']['service'] = True
        ins.write_json(self.prefix / 'installation.json', record)
        with patch.object(ins.Services, 'remove') as remove, patch.object(ins.Services, 'check_ownership'), \
                patch.object(ins.os, 'getuid', return_value=501, create=True), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(ins.main(self.args('worker') + ['--uninstall']), 0)
        remove.assert_called()

    def test_oversized_archive_is_refused_before_it_is_read(self):
        with patch.object(ins, 'MAX_ARCHIVE_BYTES', self.v1.stat().st_size - 1),                 patch.object(Path, 'read_bytes', side_effect=AssertionError('archive read')):
            self.assertIn('size/file limit', self.invoke('--release', str(self.v1), role='worker', status=1))

    def test_legacy_install_without_journal_upgrades_and_records_six_steps(self):
        self.invoke('--release', str(self.v1), role='worker')
        (self.prefix / 'update-journal.json').unlink()
        self.invoke('--release', str(self.v2), role='worker')
        journal = ins.read_json(self.prefix / 'update-journal.json')
        self.assertEqual(journal['status'], 'done')
        self.assertEqual(journal['operation'], 'upgrade')
        self.assertEqual([s['name'] for s in journal['steps']], list(ins.JOURNAL_STEPS))
        for step in journal['steps']:
            self.assertTrue(step['started_at'])
            self.assertTrue(step['finished_at'])
        self.invoke('--rollback', role='worker')
        self.assertEqual(ins.read_json(self.prefix / 'update-journal.json')['operation'], 'rollback')

    def test_offline_doctor_imports_target_interpreter_without_start_or_listener(self):
        self.invoke('--release', str(self.v1))
        config = ins.read_json(self.prefix / 'installation.json')['config']
        with patch.object(ins.Services, 'start', side_effect=AssertionError('service start')), \
                patch.object(socket.socket, 'bind', side_effect=AssertionError('listener')), \
                patch.object(ins, 'run', wraps=ins.run) as run:
            result = ins.doctor(config, desired={'controller': 'stopped'})
        self.assertTrue(result['ok'])
        self.assertEqual(result['components']['controller']['imports'], 'verified')
        self.assertEqual(run.call_args[0][0], [config['python'], '-B', '-c', 'import agent_collab.controller'])
        self.assertEqual(run.call_args.kwargs['cwd'], self.prefix / 'versions/1.0.0')

    def test_downgrade_requires_explicit_override_and_still_checks_schema(self):
        self.invoke('--release', str(self.v2))
        before = (self.prefix / 'installation.json').read_bytes()
        self.assertIn('--allow-downgrade', self.invoke('--release', str(self.v1), status=1))
        self.assertEqual((self.prefix / 'installation.json').read_bytes(), before)
        self.invoke('--release', str(self.v1), '--allow-downgrade')
        self.assertEqual(ins.current_version(self.prefix), '1.0.0')
        self.invoke('--release', str(self.v2))
        with patch.object(ins, 'compatible_schema', side_effect=ValueError('schema mismatch')):
            self.invoke('--release', str(self.v1), '--allow-downgrade', status=1)
        self.assertEqual(ins.current_version(self.prefix), '2.0.0')

    def test_compatibility_reject_precedes_stop_or_switch_and_degraded_continues(self):
        self.stopped_install()
        with self.fakes(), patch.object(ins, 'release_compatibility', side_effect=ValueError('release compatibility reject')):
            self.assertIn('compatibility reject', self.main('--release', str(self.v2), expected=1))
        self.assertEqual(ins.current_version(self.prefix), '1.0.0')
        self.assertEqual(ins.read_json(self.prefix / 'update-journal.json')['steps'][0]['status'], 'failed')
        with self.fakes(), patch.object(ins, 'release_compatibility', return_value={'decision': 'degraded', 'missing_optional': ['remote_apply']}):
            self.assertIn('degraded', self.main('--release', str(self.v2)))
        self.assertEqual(ins.current_version(self.prefix), '2.0.0')


class WritebackTests(unittest.TestCase):
    setUpClass = classmethod(legacy.InstallerTests.setUpClass.__func__)
    tearDownClass = classmethod(legacy.InstallerTests.tearDownClass.__func__)
    setUp = legacy.InstallerTests.setUp

    def test_all_unreported_writeback_states_refuse_without_exposing_paths(self):
        folder = self.state / 'worker/operations'
        folder.mkdir(parents=True)
        config = {'state_dir': str(self.state), 'role': 'worker'}
        for index, state in enumerate(('claimed', 'queued', 'running', 'finished')):
            path = folder / ('private-project-' + str(index) + '.json')
            ins.write_json(path, {'state': state})
            with self.assertRaises(ValueError) as error:
                ins.refuse_active_worker(config)
            self.assertIn(state + '=1', str(error.exception))
            self.assertNotIn('private-project', str(error.exception))
            self.assertNotIn(str(self.state), str(error.exception))
        self.assertIn('4 pending writebacks', str(error.exception))
        ins.refuse_active_worker(config, allow_active=True)
        for path in folder.glob('*.json'):
            ins.write_json(path, {'state': 'reported' if '0' in path.name else 'abandoned'})
        ins.refuse_active_worker(config)

    def test_drain_waits_for_writebacks_zero_and_reports_missing_field(self):
        clock = [0]
        client = Mock()
        def sleep(seconds):
            clock[0] += seconds
        for views, expected_sleeps, message in (([2, 1, 0], 2, False), ([None], 0, True)):
            values = [dict(drain=False)] + [dict(drain=True, active_attempts=0, **({} if v is None else {'pending_writebacks': v})) for v in views]
            with patch.object(ins, 'get_node', side_effect=values), patch.object(ins, 'set_drain'), redirect_stdout(io.StringIO()) as output:
                before = clock[0]
                self.assertFalse(ins.drain_and_wait(client, 'test', 5, clock=lambda: clock[0], sleep=sleep))
            self.assertEqual(clock[0] - before, expected_sleeps)
            self.assertEqual('未核对写回' in output.getvalue(), message)

    def test_invalid_pending_writebacks_never_means_zero(self):
        for pending in (None, False, '0', -1):
            with patch.object(ins, 'get_node', return_value=dict(drain=True, active_attempts=0, pending_writebacks=pending)), \
                    patch.object(ins, 'set_drain'), self.assertRaisesRegex(ValueError, 'timed out'):
                ins.drain_and_wait(Mock(), 'test', 0)


@unittest.skipUnless(shutil.which('ssh-keygen'), 'ssh-keygen unavailable: OpenSSH signature tests skipped')
class SignatureTests(unittest.TestCase):
    setUpClass = classmethod(legacy.InstallerTests.setUpClass.__func__)
    tearDownClass = classmethod(legacy.InstallerTests.tearDownClass.__func__)
    args = legacy.InstallerTests.args
    invoke = legacy.InstallerTests.invoke

    def setUp(self):
        legacy.InstallerTests.setUp(self)
        self.archive = self.temp / 'release.tar.gz'
        shutil.copyfile(self.v1, self.archive)
        self.key, self.other = self.temp / 'key', self.temp / 'other'
        for key in (self.key, self.other):
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'fixture', '-f', str(key)],
                           check=True, capture_output=True, **ins.NO_WINDOW)
        self.signers = self.temp / 'allowed_signers'
        self.signers.write_text('agent-collab-release ' + Path(str(self.key) + '.pub').read_text())
        self.signature = Path(str(self.archive) + '.sig')
        self.sign()

    def sign(self, key=None, namespace='agent-collab-release'):
        self.signature.unlink(missing_ok=True)
        subprocess.run(['ssh-keygen', '-Y', 'sign', '-f', str(key or self.key), '-n', namespace, str(self.archive)],
                       check=True, capture_output=True, **ins.NO_WINDOW)

    def verification_args(self, *extra):
        return ins.parser().parse_args(['--signature', str(self.signature), '--allowed-signers', str(self.signers)] + list(extra))

    def test_valid_signature_and_tampered_bytes_signer_namespace_identity_rejected(self):
        archive = self.archive.read_bytes()
        args = self.verification_args()
        self.assertTrue(ins.verify_source(self.prefix, args, archive)['verified'])
        with self.assertRaisesRegex(ValueError, 'verification failed'):
            ins.verify_source(self.prefix, args, archive[:-1] + bytes([archive[-1] ^ 1]))
        for key, namespace in ((self.other, 'agent-collab-release'), (self.key, 'other-namespace')):
            self.sign(key, namespace)
            with self.assertRaisesRegex(ValueError, 'verification failed'):
                ins.verify_source(self.prefix, args, archive)
        self.sign()
        self.signers.write_text('other-identity ' + Path(str(self.key) + '.pub').read_text())
        with self.assertRaisesRegex(ValueError, 'verification failed'):
            ins.verify_source(self.prefix, args, archive)

    def test_pinned_trust_rejects_unsigned_allows_explicit_override_and_verifies_rollback(self):
        self.invoke('--release', str(self.archive), '--signature', str(self.signature), '--allowed-signers', str(self.signers), role='worker')
        self.assertEqual((self.prefix / 'trust/allowed_signers').read_bytes(), self.signers.read_bytes())
        self.assertIn('--allow-unsigned', self.invoke('--release', str(self.v2), role='worker', status=1))
        self.invoke('--release', str(self.v2), '--allow-unsigned', role='worker')
        self.invoke('--rollback', role='worker')
        self.assertEqual(ins.current_version(self.prefix), '1.0.0')
        saved = self.prefix / 'release-sources/1.0.0/archive.sig'
        saved.write_bytes(b'corrupt')
        self.invoke('--release', str(self.v2), '--allow-unsigned', role='worker')
        self.invoke('--rollback', role='worker', status=1)
        self.assertEqual(ins.current_version(self.prefix), '2.0.0')

    def test_trust_replacement_requires_flag_and_failed_signature_never_changes_pin(self):
        self.invoke('--release', str(self.archive), '--signature', str(self.signature), '--allowed-signers', str(self.signers), role='worker')
        original = (self.prefix / 'trust/allowed_signers').read_bytes()
        self.signers.write_text('agent-collab-release ' + Path(str(self.other) + '.pub').read_text())
        self.invoke('--release', str(self.archive), '--signature', str(self.signature), '--allowed-signers', str(self.signers), role='worker', status=1)
        self.invoke('--release', str(self.archive), '--signature', str(self.signature), '--allowed-signers', str(self.signers), '--replace-trust', role='worker', status=1)
        self.assertEqual((self.prefix / 'trust/allowed_signers').read_bytes(), original)
        self.sign(self.other)
        self.invoke('--release', str(self.archive), '--signature', str(self.signature), '--allowed-signers', str(self.signers), '--replace-trust', role='worker')
        self.assertEqual((self.prefix / 'trust/allowed_signers').read_bytes(), self.signers.read_bytes())

    def test_desktop_build_refuses_a_backend_signature_that_does_not_verify(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('build_desktop', legacy.ROOT / 'scripts/build_desktop.py')
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        build.verify_release_signature(self.archive, self.signature, self.signers)
        self.sign(self.other)
        with self.assertRaisesRegex(SystemExit, '核对不通过'):
            build.verify_release_signature(self.archive, self.signature, self.signers)

    def test_trust_replaced_twice_in_one_transaction_keeps_the_original(self):
        class Journal:
            value = {}
            def save(self):
                pass
        journal, pinned = Journal(), self.prefix / 'trust/allowed_signers'
        pinned.parent.mkdir(parents=True, exist_ok=True)
        pinned.write_bytes(b'K0')
        ins.remember_trust(self.prefix, journal, dict(previous_trust=b'K0', trust=b'K1'))
        pinned.write_bytes(b'K1')  # 升级时换成 K1，恢复时又换成 K2
        ins.remember_trust(self.prefix, journal, dict(previous_trust=b'K1', trust=b'K2'))
        pinned.write_bytes(b'K2')
        ins.restore_trust(self.prefix, journal)
        self.assertEqual(pinned.read_bytes(), b'K0')

    def test_failed_upgrade_with_replaced_trust_restores_the_old_pin_and_rolls_back(self):
        self.invoke('--release', str(self.archive), '--signature', str(self.signature), '--allowed-signers', str(self.signers), role='worker')
        original = (self.prefix / 'trust/allowed_signers').read_bytes()
        newer = self.temp / 'newer.tar.gz'
        shutil.copyfile(self.v2, newer)
        subprocess.run(['ssh-keygen', '-Y', 'sign', '-f', str(self.other), '-n', 'agent-collab-release', str(newer)],
                       check=True, capture_output=True, **ins.NO_WINDOW)
        replacement = self.temp / 'replacement_signers'
        replacement.write_text('agent-collab-release ' + Path(str(self.other) + '.pub').read_text())
        with patch.object(ins, 'activate_and_check', side_effect=RuntimeError('health check failed')):
            output = self.invoke('--release', str(newer), '--signature', str(newer) + '.sig', '--allowed-signers', str(replacement),
                                 '--replace-trust', role='worker', status=1)
        self.assertIn('Restored previous version: 1.0.0', output)
        self.assertEqual(ins.current_version(self.prefix), '1.0.0')
        self.assertEqual((self.prefix / 'trust/allowed_signers').read_bytes(), original)

    @unittest.skipUnless(os.name == 'nt', 'Windows 长路径写法')
    def test_windows_verbatim_paths_from_the_desktop_resource_dir_verify(self):
        # 桌面端经 Tauri 得到的资源目录是 \\?\C:\… 形式，ssh-keygen 打不开这种路径；安装器在临时副本上核对，不受影响。
        verbatim = lambda path: '\\\\?\\' + str(path.resolve())
        args = ins.parser().parse_args(['--signature', verbatim(self.signature), '--allowed-signers', verbatim(self.signers)])
        self.assertTrue(ins.verify_source(self.prefix, args, self.archive.read_bytes())['verified'])

    def old_keygen(self):
        # 夹具：不认识 -Y 的旧版 ssh-keygen（OpenSSH 8.1 之前），打印“未知选项”与用法并以 1 退出。不代表实测过的 Windows 10。
        if os.name == 'nt':
            path = self.temp / 'old-ssh-keygen.cmd'
            path.write_text('@echo unknown option -- Y 1>&2\r\n@echo usage: ssh-keygen [-q] 1>&2\r\n@exit /b 1\r\n')
        else:
            path = self.temp / 'old-ssh-keygen'
            path.write_text('#!/bin/sh\necho "ssh-keygen: illegal option -- Y" >&2\necho "usage: ssh-keygen [-q]" >&2\nexit 1\n')
            path.chmod(0o755)
        return str(path)

    def test_keygen_without_y_verify_is_unavailable_not_tampered_and_changes_nothing(self):
        with patch.object(ins, 'ssh_keygen_candidates', return_value=[self.old_keygen()]):
            with self.assertRaisesRegex(ins.VerifierUnavailable, 'does not support "ssh-keygen -Y verify"') as caught:
                ins.verify_source(self.prefix, self.verification_args(), self.archive.read_bytes())
            self.assertNotIn('verification failed', str(caught.exception))
            self.assertIn('Win32-OpenSSH', str(caught.exception))
            out = self.invoke('--release', str(self.archive), '--signature', str(self.signature), '--allowed-signers', str(self.signers),
                              role='worker', status=1)
        self.assertIn('does not support', out)
        self.assertFalse(self.prefix.exists(), '验签前置不满足时不创建前缀、不固定信任')

    def test_falls_back_to_a_capable_keygen_and_mismatch_still_rejects(self):
        capable = ins.ssh_keygen_candidates()
        with patch.object(ins, 'ssh_keygen_candidates', return_value=[self.old_keygen()] + capable):
            self.assertTrue(ins.verify_source(self.prefix, self.verification_args(), self.archive.read_bytes())['verified'])
            archive = self.archive.read_bytes()
            with self.assertRaisesRegex(ValueError, 'verification failed'):
                ins.verify_source(self.prefix, self.verification_args(), archive[:-1] + bytes([archive[-1] ^ 1]))

    def test_keygen_that_cannot_run_is_unavailable(self):
        with patch.object(ins, 'ssh_keygen_candidates', return_value=[str(self.temp / 'missing' / 'ssh-keygen')]), \
                self.assertRaisesRegex(ins.VerifierUnavailable, 'could not run'):
            ins.verify_source(self.prefix, self.verification_args(), self.archive.read_bytes())

    def test_missing_keygen_refuses_and_signed_dry_run_never_pins(self):
        with patch.object(ins, 'ssh_keygen_candidates', return_value=[]), self.assertRaisesRegex(ins.VerifierUnavailable, 'ssh-keygen was not found'):
            ins.verify_source(self.prefix, self.verification_args(), self.archive.read_bytes())
        self.invoke('--release', str(self.archive), '--signature', str(self.signature), '--allowed-signers', str(self.signers), '--dry-run', role='worker')
        self.assertFalse(self.prefix.exists())


class AtomicWriteTests(unittest.TestCase):
    def test_replace_waits_for_a_reader_holding_the_target(self):
        # Windows：别的进程正开着目标文件（桌面端并发查询本机状态、杀毒扫描）时 os.replace 被拒（WinError 5），
        # 安装器写更新记录不能因此中途失败。其他平台上这个用例同样成立（替换不受读者影响）。
        import tempfile
        import threading
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'update-journal.json'
            ins.write_json(path, {'v': 1})
            reader = path.open('rb')
            threading.Timer(0.3, reader.close).start()
            ins.write_json(path, {'v': 2})
            self.assertEqual(ins.read_json(path), {'v': 2})
