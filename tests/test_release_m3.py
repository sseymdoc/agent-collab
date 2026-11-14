"""M3 release contracts: semantic ordering, manifest inspection and compatibility decisions."""
from contextlib import redirect_stdout
import copy
import io
import json
import unittest
from unittest.mock import patch

from agent_collab import release_tools as rt
from agent_collab.release import parse_version, compare_versions
from agent_collab import protocol
import test_installer as legacy


class VersionTests(unittest.TestCase):
    def test_pep440_order_dev_alpha_beta_rc_final_and_multidigit(self):
        versions = ['0.2.0.dev7', '0.2.0.dev10', '0.2.0a1.dev2', '0.2.0a1', '0.2.0a10',
                    '0.2.0b1.dev1', '0.2.0b1', '0.2.0rc1', '0.2.0rc9', '0.2.0rc10.dev1',
                    '0.2.0rc10', '0.2.0', '0.2.1.dev0', '0.10.0', '1.0.0']
        self.assertEqual(sorted(reversed(versions), key=parse_version), versions)
        for left, right in zip(versions, versions[1:]):
            self.assertEqual(compare_versions(left, right), -1)
            self.assertEqual(compare_versions(right, left), 1)
        self.assertEqual(compare_versions('01.02.03rc01', '1.2.3rc1'), 0)

    def test_invalid_versions_rejected_including_two_component_and_semver(self):
        for value in ('1.2', 'v1.2.3', '1.2.3-rc.1', '1.2.3rc', '1.2.3.dev', '1.2.3.post1',
                      '1!1.2.3', '1.2.3+local', ' 1.2.3', '1.2.3\n', '1.2.3A1', '1.2.3.4',
                      '1.2.3dev1', '1.2.3rc-1', '１.2.3', '', None, 123):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_version(value)

    def test_compare_cli_numeric_result_and_invalid_exit(self):
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(rt.main(['compare', '0.1.0rc9', '0.1.0rc10']), 0)
        self.assertEqual(json.loads(output.getvalue())['comparison'], -1)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(rt.main(['compare', 'bad', '0.1.0']), 2)


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.release = dict(version='0.2.0', protocol_version=1, features=['clients', 'remote_apply'],
                            compat=dict(worker_requires=['clients'], worker_optional=['remote_apply'],
                                        worker_min_controller='0.1.0rc10', controller_min_worker='0.1.0rc10'))
        self.controller = dict(version='0.1.0', protocol_version=1, features=['clients', 'remote_apply'],
                               compat=dict(controller_min_worker='0.1.0rc10'))

    def test_worker_accept(self):
        self.assertEqual(rt.evaluate_compat(self.release, {'controller': self.controller})['decision'], 'accept')

    def test_worker_degraded_lists_optional_features_and_effects(self):
        self.controller['features'] = ['clients']
        result = rt.evaluate_compat(self.release, self.controller)
        self.assertEqual(result['decision'], 'degraded')
        self.assertEqual(result['missing_optional'], ['remote_apply'])
        self.assertIn('写回', result['affected_features']['remote_apply'])

    def test_worker_reject_protocol_required_feature_and_both_min_versions(self):
        for change in ({'protocol_version': 2}, {'features': []}, {'version': '0.1.0rc9'},
                       {'compat': {'controller_min_worker': '0.3.0'}}):
            with self.subTest(change=change):
                self.assertEqual(rt.evaluate_compat(self.release, dict(self.controller, **change))['decision'], 'reject')

    def test_controller_rejects_old_nodes_and_accepts_compatible_fleet(self):
        nodes = [dict(node_id='worker', agent_version='0.1.0rc9')]
        self.assertEqual(rt.evaluate_compat(self.release, self.controller, 'controller', nodes)['decision'], 'reject')
        nodes[0]['agent_version'] = '0.1.0rc10'
        result = rt.evaluate_compat(self.release, self.controller, 'controller', nodes)
        self.assertEqual(result['decision'], 'degraded')
        self.assertTrue(any('未报告兼容声明' in item for item in result['unchecked']))
        nodes[0]['compat'] = {}
        self.assertEqual(rt.evaluate_compat(self.release, self.controller, 'controller', nodes)['decision'], 'degraded')
        nodes[0]['protocol_version'] = self.release['protocol_version']
        self.assertEqual(rt.evaluate_compat(self.release, self.controller, 'controller', nodes)['decision'], 'accept')
        nodes[0]['protocol_version'] = True
        self.assertEqual(rt.evaluate_compat(self.release, self.controller, 'controller', nodes)['decision'], 'reject')
        nodes[0]['protocol_version'] = self.release['protocol_version']
        nodes[0]['compat'] = {'worker_requires': ['missing']}
        self.assertEqual(rt.evaluate_compat(self.release, self.controller, 'controller', nodes)['decision'], 'reject')

    def test_legacy_metadata_and_missing_fleet_are_explicitly_unchecked(self):
        self.assertEqual(rt.evaluate_compat(self.release, self.controller, 'controller')['decision'], 'degraded')
        del self.controller['protocol_version']
        del self.controller['compat']
        result = rt.evaluate_compat(self.release, self.controller)
        self.assertEqual(result['decision'], 'degraded')
        self.assertEqual(len(result['unchecked']), 2)

    def test_invalid_minimum_worker_version_rejects_even_without_nodes(self):
        release = dict(self.release, compat=dict(self.release.get('compat') or {}, controller_min_worker=''))
        self.assertEqual(rt.evaluate_compat(release, self.controller, 'controller', [])['decision'], 'reject')

    def test_malformed_metadata_rejects(self):
        for change in ({'version': 'nope'}, {'features': 'clients'}, {'protocol_version': True}, {'compat': 'invalid'}, {'compat': []}, {'compat': False}):
            with self.subTest(change=change):
                self.assertEqual(rt.evaluate_compat(self.release, dict(self.controller, **change))['decision'], 'reject')


class ProtocolCheckTests(unittest.TestCase):
    def test_build_refuses_malformed_protocol_metadata(self):
        good = dict(PROTOCOL_VERSION=protocol.PROTOCOL_VERSION, FEATURES=protocol.FEATURES, COMPAT=protocol.COMPAT)
        legacy.builder.check_protocol(good)
        for bad in ({'PROTOCOL_VERSION': True}, {'PROTOCOL_VERSION': 0}, {'FEATURES': ('clients', 'clients')},
                    {'COMPAT': dict(protocol.COMPAT, controller_min_worker='nope')},
                    {'COMPAT': dict(protocol.COMPAT, worker_requires='clients')},
                    {'COMPAT': dict(protocol.COMPAT, client_requires=('not-a-feature',))},
                    {'COMPAT': dict(protocol.COMPAT, worker_optional=('not-a-feature',))},
                    {'COMPAT': {k: v for k, v in protocol.COMPAT.items() if k != 'worker_requires'}},
                    {'COMPAT': dict(protocol.COMPAT, workers_min_controller='9.0.0')}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                legacy.builder.check_protocol(dict(good, **bad))


class UpdaterKeyPairTests(unittest.TestCase):
    def test_signature_must_come_from_the_embedded_public_key(self):
        import base64, importlib.util
        spec = importlib.util.spec_from_file_location('build_desktop', legacy.ROOT / 'scripts/build_desktop.py')
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        wrap = lambda lines: base64.b64encode('\n'.join(lines).encode()).decode()
        blob = lambda key_id, size: base64.b64encode(b'Ed' + key_id + bytes(size)).decode()
        pubkey = wrap(['untrusted comment: public key', blob(b'AAAAAAAA', 32)])
        build.check_key_pair(pubkey, wrap(['untrusted comment: signature', blob(b'AAAAAAAA', 64), 'trusted comment: x', 'sig']), 'ok')
        with self.assertRaises(SystemExit):
            build.check_key_pair(pubkey, wrap(['untrusted comment: signature', blob(b'BBBBBBBB', 64), 'trusted comment: x', 'sig']), 'other')


class DesktopCopyTests(unittest.TestCase):
    def test_fragments_must_carry_the_same_backend(self):
        import importlib.util, tempfile
        from pathlib import Path
        spec = importlib.util.spec_from_file_location('build_desktop', legacy.ROOT / 'scripts/build_desktop.py')
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        base = dict(kind='candidate', identifier='x', desktop_version='1.0.0-dev.1', commit='c',
                    backend=dict(file='b.tar.gz', sha256='a' * 64, signed=True, manifest=dict(version='1.0.0.dev1')))
        with tempfile.TemporaryDirectory() as folder:
            dist = Path(folder)
            for target in ('windows-x86_64', 'darwin-aarch64'):
                (dist / ('platform-%s.json' % target)).write_text(json.dumps(dict(base, target=target)), encoding='utf-8')
            self.assertEqual(len(build.fragments(dist)), 2)
            for change in ({'sha256': 'b' * 64}, {'manifest': dict(version='1.0.0.dev2')}):
                other = dict(base, target='darwin-aarch64', backend=dict(base['backend'], **change))
                (dist / 'platform-darwin-aarch64.json').write_text(json.dumps(other), encoding='utf-8')
                with self.subTest(change=change), self.assertRaises(SystemExit):
                    build.fragments(dist)

    def test_formal_versions_are_final_or_release_candidates(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('build_desktop', legacy.ROOT / 'scripts/build_desktop.py')
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        for version in ('0.3.0', '0.3.0-rc.1', '10.20.30-rc.12'):
            self.assertTrue(build.version_fits('formal', version), version)
        for version in ('0.3.0-dev.22', '0.3.0-rc1', '0.3.0-rc.', '0.3.0-rc.1.2', '0.3', '0.3.0-beta.1', 'v0.3.0'):
            self.assertFalse(build.version_fits('formal', version), version)
        self.assertTrue(build.version_fits('candidate', '0.3.0-dev.22'))
        self.assertFalse(build.version_fits('candidate', '0.3.0'))

    def test_windows_tauri_command_keeps_backslashes_and_drops_outer_quotes(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('build_desktop', legacy.ROOT / 'scripts/build_desktop.py')
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        with patch.object(build.os, 'name', 'nt'):
            self.assertEqual(build.split_tauri('"C:\\Program Files\\node\\node.exe" tauri.js'),
                             ['C:\\Program Files\\node\\node.exe', 'tauri.js'])

    def test_copy_skips_generated_payload_and_local_secrets(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('build_desktop', legacy.ROOT / 'scripts/build_desktop.py')
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        skip = build.desktop_ignore('x/src-tauri/resources', ['app', 'runtime', 'release', 'licenses', 'payload.json', 'icons'])
        self.assertEqual(skip, {'runtime', 'release', 'licenses', 'payload.json'})
        self.assertEqual(build.desktop_ignore('x/resources/app', ['helper.py', 'agent_collab']), {'agent_collab'})
        self.assertEqual(build.desktop_ignore('x/src', ['lib.rs', 'admin.token', 'controller.db', 'target']),
                         {'admin.token', 'controller.db', 'target'})


class ReleaseArchiveTests(unittest.TestCase):
    setUpClass = classmethod(legacy.InstallerTests.setUpClass.__func__)
    tearDownClass = classmethod(legacy.InstallerTests.tearDownClass.__func__)
    setUp = legacy.InstallerTests.setUp

    def test_manifest_contains_literal_protocol_features_compat(self):
        manifest, _ = legacy.ins.read_release(self.v1)
        self.assertEqual(manifest['protocol_version'], protocol.PROTOCOL_VERSION)
        self.assertEqual(manifest['features'], list(protocol.FEATURES))
        self.assertEqual(manifest['compat'], json.loads(json.dumps(protocol.COMPAT)))

    def test_inspect_is_read_only_verifies_hashes_and_exposes_metadata(self):
        before = legacy.hashes(self.shared)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(rt.main(['inspect', str(self.v1)]), 0)
        info = json.loads(output.getvalue())
        for field in ('version', 'git_commit', 'git_dirty', 'protocol_version', 'features', 'compat',
                      'controller_schema_min', 'controller_schema_max', 'controller_schema_fingerprint'):
            self.assertIn(field, info)
        self.assertEqual(info['hashes'], 'verified')
        self.assertFalse(info['source_verified'])
        self.assertEqual(legacy.hashes(self.shared), before)

    def test_compat_file_cli_exit_codes_accept_degraded_reject(self):
        manifest, _ = legacy.ins.read_release(self.v1)
        info = dict(version='1.0.0', protocol_version=manifest['protocol_version'],
                    features=manifest['features'], compat=manifest['compat'])
        path = self.temp / 'controller.json'
        for changes, code, decision in (({}, 0, 'accept'), ({'features': list(protocol.COMPAT['worker_requires'])}, 1, 'degraded'),
                                         ({'protocol_version': 99}, 2, 'reject')):
            path.write_text(json.dumps({'controller': dict(info, **changes)}))
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(rt.main(['compat', '--release', str(self.v1), '--controller-info', str(path)]), code)
            self.assertEqual(json.loads(output.getvalue())['decision'], decision)

    def test_compat_online_reuses_cli_connection_and_nodes(self):
        manifest, _ = legacy.ins.read_release(self.v1)
        info = dict(version='1.0.0', protocol_version=manifest['protocol_version'],
                    features=manifest['features'], compat=manifest['compat'])
        with patch('agent_collab.cli.Conn') as conn, redirect_stdout(io.StringIO()):
            conn.return_value.send.side_effect = [(200, {'controller': info}), (200, {'nodes': []})]
            self.assertEqual(rt.main(['compat', '--release', str(self.v1), '--as', 'controller',
                                      '--controller', 'https://example.invalid', '--profile', 'test']), 0)
        self.assertEqual(conn.call_args[0][0].url, 'https://example.invalid')
        self.assertEqual([c[0] for c in conn.return_value.send.call_args_list], [('GET', '/client'), ('GET', '/nodes')])

    def test_compat_online_connection_setup_failure_prints_a_reject_decision(self):
        with patch('agent_collab.cli.Conn', side_effect=SystemExit(3)), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(rt.main(['compat', '--release', str(self.v1), '--controller', 'https://example.invalid',
                                      '--profile', 'test']), 2)
        self.assertEqual(json.loads(output.getvalue())['decision'], 'reject')

    def test_compat_online_unreachable_node_list_is_degraded(self):
        manifest, _ = legacy.ins.read_release(self.v1)
        info = dict(version='1.0.0', protocol_version=manifest['protocol_version'], features=manifest['features'], compat=manifest['compat'])
        from agent_collab import cli
        with patch('agent_collab.cli.Conn') as conn, redirect_stdout(io.StringIO()) as output:
            conn.return_value.send.side_effect = [(200, {'controller': info}), cli.Unreachable('down')]
            self.assertEqual(rt.main(['compat', '--release', str(self.v1), '--as', 'controller',
                                      '--controller', 'https://example.invalid', '--profile', 'test']), 1)
        self.assertEqual(json.loads(output.getvalue())['decision'], 'degraded')

    def test_compat_online_refusal_prints_a_reject_decision(self):
        for status in (401, 403, 404):
            with self.subTest(status=status), patch('agent_collab.cli.Conn') as conn, redirect_stdout(io.StringIO()) as output:
                conn.return_value.send.return_value = (status, {'error': {'code': 'unauthorized'}})
                self.assertEqual(rt.main(['compat', '--release', str(self.v1), '--controller', 'https://example.invalid',
                                          '--profile', 'test']), 2)
            result = json.loads(output.getvalue())
            self.assertEqual((result['decision'], result['error_type']), ('reject', 'ValueError'))
