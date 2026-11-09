import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from agent_collab import artifacts as a, protocol as p

ROOT = Path(__file__).resolve().parents[1]


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.dev/tmp').mkdir(parents=True, exist_ok=True)
        self.temp = Path(tempfile.mkdtemp(dir=str(ROOT / '.dev/tmp')))
        self.addCleanup(shutil.rmtree, str(self.temp))
        self.work = self.temp / 'work'
        self.work.mkdir()

    def baseline(self):
        (self.work / 'modify').write_bytes(b'old\r\n\x00')
        (self.work / 'delete').write_bytes(b'delete')
        return a.snapshot(self.work)

    def test_default_and_custom_exclusions_preserve_materialization_and_no_deletions(self):
        names = ['keep.py', '__pycache__/cache.bin', 'pkg/__pycache__/deep/cache.bin',
                 'pkg/file.pyc', '.pytest_cache/v/cache', 'pkg/.DS_Store', 'build/deep/log.txt']
        for name in names:
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'content')
        baseline, blobs = a.snapshot(self.work, exclude=[])
        self.assertEqual(len(baseline['files']), len(names))
        restored = self.temp / 'restored'
        a.materialize(restored, baseline, blobs.__getitem__)
        self.assertTrue((restored / 'pkg/file.pyc').exists())
        (self.work / 'pkg/file.pyc').unlink()
        (self.work / 'keep.py').unlink()
        current, _ = a.snapshot(self.work, baseline)
        self.assertEqual([f['path'] for f in current['files']], ['build/deep/log.txt'])
        self.assertEqual(current['deleted'], ['keep.py'])
        integrated = a.integrate(restored, baseline, current, blobs.__getitem__, self.temp / 'backup',
                                 exclude=a.DEFAULT_EXCLUDES)
        self.assertEqual(integrated['changed'], ['keep.py'])
        self.assertTrue((restored / 'pkg/file.pyc').exists())
        custom, _ = a.snapshot(self.work, baseline, exclude=['build/**', '*.bin'])
        self.assertNotIn('build/deep/log.txt', [f['path'] for f in custom['files']])
        self.assertNotIn('build/deep/log.txt', custom['deleted'])
        self.assertIn('pkg/.DS_Store', [f['path'] for f in custom['files']])
        self.assertTrue(a.workspace_excluded('a/cache/deep/out', ['cache/**']))
        self.assertTrue(a.workspace_excluded('a/out', ['a/**/out']))
        self.assertFalse(a.workspace_excluded('a/deep/out', ['a/*']))

    def test_roundtrip_modify_add_delete_without_git(self):
        base, blobs = self.baseline()
        (self.work / 'modify').write_bytes(b'new\n\x00')
        (self.work / 'add').write_bytes(b'added\r\n')
        (self.work / 'delete').unlink()
        result, blobs = a.snapshot(self.work, base)
        self.assertEqual(result, p.validate_manifest(result))
        self.assertEqual(result['deleted'], ['delete'])
        self.assertEqual(result['base'], p.manifest_sha256(base))
        restored = self.temp / 'restored'
        a.materialize(restored, result, blobs.__getitem__)
        self.assertEqual((restored / 'modify').read_bytes(), b'new\n\x00')
        self.assertEqual((restored / 'add').read_bytes(), b'added\r\n')
        self.assertFalse((restored / 'delete').exists())
        self.assertFalse((restored / '.git').exists())
        self.assertEqual(blobs[p.manifest_sha256(result)], p.canonical_json(result))

    def test_windows_preserves_base_executable(self):
        base, _ = self.baseline()
        base['files'][0]['executable'] = True
        result, _ = a.snapshot(self.work, base, windows=True)
        self.assertTrue(result['files'][0]['executable'])

    @unittest.skipIf(os.name == 'nt', 'POSIX executable bits')
    def test_posix_executable(self):
        path = self.work / 'run'
        path.write_bytes(b'exec')
        path.chmod(0o755)
        manifest, blobs = a.snapshot(self.work)
        self.assertTrue(manifest['files'][0]['executable'])
        a.materialize(self.temp / 'restore', manifest, blobs.__getitem__)
        self.assertTrue((self.temp / 'restore/run').stat().st_mode & 0o111)

    def test_rejects_symlinks(self):
        try:
            (self.work / 'link').symlink_to(self.temp, target_is_directory=True)
        except OSError:
            self.skipTest('symlink creation unavailable on this platform')
        with self.assertRaisesRegex(ValueError, 'symbolic link'):
            a.snapshot(self.work)
        with self.assertRaisesRegex(ValueError, 'symbolic link'):
            a.safe_path(self.work, 'link/outside')

    def test_rejects_case_aliases_in_file_and_directory_components(self):
        for names in (['A', 'a'], ['Dir/a', 'dir/b']):
            manifest = dict(version=1, base=None, deleted=[], files=[
                dict(path=name, size=0, sha256=p.sha256_bytes(b''), executable=False) for name in names])
            with self.assertRaisesRegex(ValueError, 'case-conflicting'):
                a.materialize(self.temp / 'restore', manifest, lambda _: b'')

    def test_snapshot_rejects_case_conflicts(self):
        (self.work / 'A').write_bytes(b'x')
        # On case-insensitive APFS the aliases cannot coexist; simulate the walk
        # and file metadata to exercise the same cross-platform rejection.
        with patch('agent_collab.artifacts.os.walk', return_value=[(str(self.work), [], ['A', 'a'])]), \
                patch.object(Path, 'lstat', return_value=(self.work / 'A').lstat()), \
                patch.object(Path, 'read_bytes', return_value=b'x'):
            with self.assertRaisesRegex(ValueError, 'case-conflicting'):
                a.snapshot(self.work)

    def test_materialize_checks_hash_and_size_before_writing(self):
        base, _ = self.baseline()
        destination = self.temp / 'destination'
        with self.assertRaisesRegex(ValueError, 'integrity'):
            a.materialize(destination, base, lambda _: b'bad')
        self.assertEqual(list(destination.iterdir()), [])

    def test_materialize_rejects_nonempty_directory(self):
        with self.assertRaisesRegex(ValueError, 'empty'):
            a.materialize(self.work, self.baseline()[0], lambda _: b'')

    def test_integration_conflict_never_mutates_any_target(self):
        base, blobs = self.baseline()
        target = self.temp / 'target'
        a.materialize(target, base, blobs.__getitem__)
        (self.work / 'modify').write_bytes(b'new')
        (self.work / 'delete').unlink()
        (self.work / 'add').write_bytes(b'new')
        result, blobs = a.snapshot(self.work, base)
        (target / 'modify').write_bytes(b'human edit')
        output = a.integrate(target, base, result, blobs.__getitem__, self.temp / 'backup')
        self.assertEqual(output['status'], 'conflict')
        self.assertEqual(output['conflicts'], ['modify'])
        self.assertEqual((target / 'modify').read_bytes(), b'human edit')
        self.assertEqual((target / 'delete').read_bytes(), b'delete')
        self.assertFalse((target / 'add').exists())
        self.assertFalse((self.temp / 'backup').exists())

    def test_integration_backs_up_then_writes_and_deletes(self):
        base, blobs = self.baseline()
        target = self.temp / 'target'
        a.materialize(target, base, blobs.__getitem__)
        (self.work / 'modify').write_bytes(b'new')
        (self.work / 'delete').unlink()
        (self.work / 'add').write_bytes(b'added')
        result, blobs = a.snapshot(self.work, base)
        output = a.integrate(target, base, result, blobs.__getitem__, self.temp / 'backup')
        self.assertEqual(output['status'], 'integrated')
        self.assertEqual(output['changed'], ['add', 'delete', 'modify'])
        self.assertEqual((target / 'modify').read_bytes(), b'new')
        self.assertEqual((self.temp / 'backup/modify').read_bytes(), b'old\r\n\x00')
        self.assertEqual((self.temp / 'backup/delete').read_bytes(), b'delete')
        self.assertFalse((target / 'delete').exists())

    def test_integration_unreadable_target_reports_error_not_silent_conflict(self):
        # macOS LaunchAgent 访问外置卷被 TCC 拒绝（EPERM）时实测出现：不写入，并如实给出原因。
        base, blobs = self.baseline()
        target = self.temp / 'target'
        a.materialize(target, base, blobs.__getitem__)
        (self.work / 'add').write_bytes(b'agent')
        result, blobs = a.snapshot(self.work, base)
        denied = PermissionError(1, 'Operation not permitted')
        with patch.object(a, 'safe_path', side_effect=denied):
            output = a.integrate(target, base, result, blobs.__getitem__, self.temp / 'backup')
        self.assertEqual(output['status'], 'conflict')
        self.assertEqual(output['errors'], [{'path': 'add', 'error': 'PermissionError: Operation not permitted'}])
        self.assertFalse((target / 'add').exists())

    def test_integration_missing_base_and_existing_add_are_conflicts(self):
        base, blobs = self.baseline()
        target = self.temp / 'target'
        a.materialize(target, base, blobs.__getitem__)
        (target / 'modify').unlink()
        (target / 'add').write_bytes(b'human')
        (self.work / 'add').write_bytes(b'agent')
        result, blobs = a.snapshot(self.work, base)
        output = a.integrate(target, base, result, blobs.__getitem__, self.temp / 'backup')
        self.assertEqual(output['conflicts'], ['add'])
        self.assertEqual((target / 'add').read_bytes(), b'human')
        self.assertFalse((target / 'modify').exists())

    def test_integration_preserves_human_changes_to_unchanged_baseline(self):
        base, blobs = self.baseline()
        target = self.temp / 'target'
        a.materialize(target, base, blobs.__getitem__)
        (target / 'modify').write_bytes(b'human edit')
        (self.work / 'add').write_bytes(b'agent added')
        (self.work / 'delete').unlink()
        result, blobs = a.snapshot(self.work, base)
        output = a.integrate(target, base, result, blobs.__getitem__, self.temp / 'backup')
        self.assertEqual(output['status'], 'integrated')
        self.assertEqual(output['changed'], ['add', 'delete'])
        self.assertEqual((target / 'modify').read_bytes(), b'human edit')
        self.assertEqual((target / 'add').read_bytes(), b'agent added')
        self.assertFalse((target / 'delete').exists())
        self.assertFalse((self.temp / 'backup/modify').exists())
        self.assertEqual((self.temp / 'backup/delete').read_bytes(), b'delete')

    def test_integration_missing_changed_baseline_is_conflict(self):
        base, blobs = self.baseline()
        target = self.temp / 'target'
        a.materialize(target, base, blobs.__getitem__)
        (target / 'modify').unlink()
        (self.work / 'modify').write_bytes(b'agent change')
        result, blobs = a.snapshot(self.work, base)
        output = a.integrate(target, base, result, blobs.__getitem__, self.temp / 'backup')
        self.assertEqual(output['status'], 'conflict')
        self.assertEqual(output['conflicts'], ['modify'])
        self.assertFalse((target / 'modify').exists())

    def test_integrate_corrupt_download_prevents_changes(self):
        base, blobs = self.baseline()
        target = self.temp / 'target'
        a.materialize(target, base, blobs.__getitem__)
        (self.work / 'modify').write_bytes(b'new')
        result, _ = a.snapshot(self.work, base)
        with self.assertRaises(ValueError):
            a.integrate(target, base, result, lambda _: b'corrupt', self.temp / 'backup')
        self.assertEqual((target / 'modify').read_bytes(), b'old\r\n\x00')

    def test_integration_parent_file_conflict_does_not_delete_other_files(self):
        base, blobs = self.baseline()
        target = self.temp / 'target'
        a.materialize(target, base, blobs.__getitem__)
        (target / 'parent').write_bytes(b'human')
        (self.work / 'delete').unlink()
        (self.work / 'parent').mkdir()
        (self.work / 'parent/new').write_bytes(b'new')
        result, blobs = a.snapshot(self.work, base)
        output = a.integrate(target, base, result, blobs.__getitem__, self.temp / 'backup')
        self.assertEqual(output['status'], 'conflict')
        self.assertEqual(output['conflicts'], ['parent/new'])
        self.assertEqual((target / 'delete').read_bytes(), b'delete')
