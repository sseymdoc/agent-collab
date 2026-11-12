"""本机项目目录的输入快照与成果应用（agent_collab/project.py，protocol §13.2–§13.4）。

真实的临时目录与文件；不访问网络，不调用模型。
"""
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unicodedata
import unittest
from unittest.mock import patch
import uuid

from agent_collab import project, protocol as p

ROOT = Path(__file__).resolve().parents[1]


def write(root, files):
    for name, data in files.items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode('utf-8'))


def tree(root):
    """目录里全部文件的相对路径与内容。"""
    root = Path(root)
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob('*')) if path.is_file()}


class Case(unittest.TestCase):
    def setUp(self):
        base = ROOT / '.dev' / 'tmp'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = Path(tempfile.mkdtemp(dir=str(base)))
        self.addCleanup(shutil.rmtree, str(self.temp), True)
        self.root, self.state = self.temp / 'project', self.temp / 'state'
        self.root.mkdir()

    def link(self, link, target, directory=False):
        """建一个指向别处的链接；这台机器上建不了就跳过用例。"""
        try:
            os.symlink(str(target), str(link), target_is_directory=directory)
        except (OSError, NotImplementedError):
            self.skipTest('这台机器上当前用户不能创建符号链接')


class ScanTests(Case):
    def test_default_range_leaves_out_vcs_caches_and_credentials_and_says_so(self):
        write(self.root, {'src/app.py': 'print(1)\n', 'README.md': '# demo\n', '.git/config': '[core]\n', 'node_modules/x/index.js': 'x',
                          '.env': 'SECRET=1\n', 'deploy/server.pem': 'KEY', 'src/__pycache__/app.cpython-39.pyc': b'\x00', '.venv/pyvenv.cfg': 'x',
                          'id_rsa': 'private'})
        result = project.scan(self.root)
        self.assertEqual([f['path'] for f in result['manifest']['files']], ['README.md', 'src/app.py'])
        self.assertEqual((result['files'], result['bytes']), (2, len('print(1)\n') + len('# demo\n')))
        self.assertEqual(sorted(result['excluded']['top']), ['.env', '.git/', '.venv/', 'deploy/server.pem', 'id_rsa', 'node_modules/', 'src/__pycache__/'])
        self.assertEqual((result['excluded']['count'], result['skipped'], result['manifest_sha256']), (7, [], p.manifest_sha256(result['manifest'])))
        # 排除规则可以调整：给出的列表替换缺省值。
        custom = project.scan(self.root, exclude=['.git/**', 'node_modules/**', '.venv/**', '*.pyc', 'README.md'])
        self.assertEqual([f['path'] for f in custom['manifest']['files']], ['.env', 'deploy/server.pem', 'id_rsa', 'src/app.py'])
        with self.assertRaises(ValueError):
            project.scan(self.root, exclude=['../outside'])
        with self.assertRaises(project.ProjectError) as missing:
            project.scan(self.root / 'nope')
        self.assertEqual(missing.exception.code, 'not_a_directory')

    def test_links_are_never_followed_out_of_the_selected_folder(self):
        outside = self.temp / 'outside'
        write(outside, {'secret.txt': 'do not upload', 'more/inner.txt': 'nor this'})
        write(self.root, {'kept.txt': 'in'})
        self.link(self.root / 'file-link.txt', outside / 'secret.txt')
        self.link(self.root / 'dir-link', outside, directory=True)
        result = project.scan(self.root)
        self.assertEqual([f['path'] for f in result['manifest']['files']], ['kept.txt'])
        self.assertEqual(sorted((s['path'], s['reason']) for s in result['skipped']), [('dir-link', 'link'), ('file-link.txt', 'link')])
        self.assertEqual(result['skipped_counts'], {'link': 2})

    @unittest.skipUnless(os.name == 'nt', 'Windows 目录联接')
    def test_windows_junction_is_a_reparse_point_and_is_not_entered(self):
        outside = self.temp / 'outside'
        write(outside, {'secret.txt': 'do not upload'})
        write(self.root, {'kept.txt': 'in'})
        made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(self.root / 'junction'), str(outside)], capture_output=True)
        if made.returncode != 0:
            self.skipTest('无法创建目录联接')
        result = project.scan(self.root)
        self.assertEqual(([f['path'] for f in result['manifest']['files']], result['skipped']), (['kept.txt'], [{'path': 'junction', 'reason': 'link'}]))
        os.rmdir(str(self.root / 'junction'))  # 只删联接本身，不动它指向的目录

    def test_selected_folder_may_itself_be_a_link(self):
        real = self.temp / 'real'
        write(real, {'a.txt': 'a'})
        self.link(self.temp / 'alias', real, directory=True)
        result = project.scan(self.temp / 'alias')
        self.assertEqual((result['root'], [f['path'] for f in result['manifest']['files']]), (str(real.resolve()), ['a.txt']))

    def test_file_names_are_recorded_in_nfc_and_read_back_by_their_real_name(self):
        decomposed = 'cafe\u0301.txt'  # e + 组合重音符；规范形式是 café.txt
        write(self.root, {decomposed: 'latte', '文档/说明.txt': '中文'})
        actual = sorted(os.listdir(str(self.root)))
        result = project.scan(self.root)
        names = [f['path'] for f in result['manifest']['files']]
        self.assertEqual(names, sorted([unicodedata.normalize('NFC', decomposed), '文档/说明.txt']))
        if decomposed in actual:  # 文件系统保留了分解形式：快照记下它在本机的实际名称
            self.assertEqual(result['locations'], {'café.txt': decomposed})
        entry = next(f for f in result['manifest']['files'] if f['path'].startswith('caf'))
        self.assertEqual(project.read_verified(result, entry), b'latte')

    @unittest.skipUnless(os.name == 'posix', '这些名称在 Windows 上建不出来')
    def test_names_windows_cannot_hold_are_skipped_with_a_reason(self):
        write(self.root, {'ok.txt': 'x', 'aux.txt': 'reserved', 'trailing.': 'dot', 'a:b': 'colon', 'dir /inner.txt': 'space'})
        result = project.scan(self.root)
        self.assertEqual([f['path'] for f in result['manifest']['files']], ['ok.txt'])
        self.assertEqual(result['skipped_counts'], {'name_not_portable': 4})

    def test_two_names_differing_only_in_case_cannot_form_one_input(self):
        write(self.root, {'Readme.md': 'a'})
        if (self.root / 'README.MD').exists():
            self.skipTest('这个卷不区分大小写，建不出只差大小写的两个文件')
        write(self.root, {'README.MD': 'b'})
        with self.assertRaises(project.ProjectError) as conflict:
            project.scan(self.root)
        self.assertEqual(conflict.exception.code, 'path_conflict')

    def test_oversized_and_unreadable_files_are_reported_not_silently_dropped(self):
        write(self.root, {'small.txt': 'x', 'big.bin': b'0' * 4096})
        result = project.scan(self.root, max_file_bytes=1024)
        self.assertEqual(([f['path'] for f in result['manifest']['files']], result['skipped'], result['skipped_counts']),
                         (['small.txt'], [{'path': 'big.bin', 'reason': 'too_large'}], {'too_large': 1}))
        real = project._hash_stable

        def refuse(path, status):
            if Path(path).name == 'small.txt':
                raise PermissionError(errno.EACCES, 'denied')
            return real(path, status)
        with patch.object(project, '_hash_stable', side_effect=refuse):
            result = project.scan(self.root)
        self.assertEqual((sorted(f['path'] for f in result['manifest']['files']), result['skipped']),
                         (['big.bin'], [{'path': 'small.txt', 'reason': 'unreadable'}]))

    def test_changes_during_the_scan_are_detected_and_never_become_a_mixed_snapshot(self):
        write(self.root, {'a.txt': 'a1', 'b.txt': 'b1'})
        real, calls = project._hash_stable, []

        def racing(path, status):
            calls.append(Path(path).name)
            hashed = real(path, status)
            if calls == ['a.txt', 'b.txt']:
                # 第一遍读完 b.txt 之后，已经读过的 a.txt 被改了，还多了一个新文件。
                write(self.root, {'a.txt': 'a2-longer', 'c.txt': 'new'})
            return hashed
        seen = []
        with patch.object(project, '_hash_stable', side_effect=racing):
            result = project.scan(self.root, progress=seen.append)
        content = {f['path']: f['sha256'] for f in result['manifest']['files']}
        self.assertEqual(content, {'a.txt': p.sha256_bytes(b'a2-longer'), 'b.txt': p.sha256_bytes(b'b1'), 'c.txt': p.sha256_bytes(b'new')})
        self.assertEqual([s for s in seen if s['phase'] == 'rescan'], [{'phase': 'rescan', 'attempt': 1, 'changed': 2}])

        counter = [0]

        def always_changing(path, status):
            hashed = real(path, status)
            counter[0] += 1
            write(self.root, {'churn.log': 'entry;' * counter[0]})
            return hashed
        with patch.object(project, '_hash_stable', side_effect=always_changing), self.assertRaises(project.ProjectError) as unstable:
            project.scan(self.root)
        self.assertEqual((unstable.exception.code, 'churn.log' in unstable.exception.detail['paths']), ('unstable', True))
        with self.assertRaises(project.ProjectError) as cancelled:
            project.scan(self.root, cancelled=lambda: True)
        self.assertEqual(cancelled.exception.code, 'cancelled')

    def test_executable_bit_is_recorded_where_the_platform_has_one(self):
        write(self.root, {'run.sh': '#!/bin/sh\n', 'data.txt': 'x'})
        if os.name == 'posix':
            os.chmod(str(self.root / 'run.sh'), 0o755)
            flags = {f['path']: f['executable'] for f in project.scan(self.root)['manifest']['files']}
            self.assertEqual(flags, {'data.txt': False, 'run.sh': True})
        else:
            # Windows 看不到可执行位：沿用上一份输入里同一路径的标记，新文件为 False。
            previous = {'files': [{'path': 'run.sh', 'sha256': '0' * 64, 'size': 0, 'executable': True}]}
            flags = {f['path']: f['executable'] for f in project.scan(self.root, previous=previous)['manifest']['files']}
            self.assertEqual(flags, {'data.txt': False, 'run.sh': True})

    @unittest.skipUnless(shutil.which('git'), '没有 git')
    def test_git_project_shows_branch_and_uncommitted_changes_without_requiring_git_knowledge(self):
        write(self.root, {'a.txt': 'a'})
        plain = project.scan(self.root)
        self.assertIsNone(plain['git'])  # 普通文件夹
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
        subprocess.run(['git', 'init', '-q', '-b', 'trunk', str(self.root)], check=True, env=env, capture_output=True)
        with patch('subprocess.run', wraps=subprocess.run) as run, patch.dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull):
            result = project.scan(self.root)  # 不读调用者的 Git 配置（例如 status.showUntrackedFiles=no）
        self.assertEqual((result['git']['available'], result['git']['branch'], result['git']['uncommitted']), (True, 'trunk', 1))
        # 桌面端的助手没有控制台：每次 git 查询都要带 CREATE_NO_WINDOW，否则 Windows 上各闪一个窗口。
        git_calls = [c for c in run.call_args_list if 'git' in Path(c[0][0][0]).name.lower()]
        self.assertTrue(git_calls)
        self.assertEqual({c[1].get('creationflags') for c in git_calls}, {subprocess.CREATE_NO_WINDOW if os.name == 'nt' else None})
        self.assertEqual([f['path'] for f in result['manifest']['files']], ['a.txt'])  # .git 不进输入


class StageTests(Case):
    def test_staged_copies_match_the_snapshot_and_a_changed_source_is_reported(self):
        write(self.root, {'a.txt': 'alpha', 'b.txt': 'beta', 'same-as-a.txt': 'alpha'})
        snapshot_id = project.save_snapshot(self.state, project.scan(self.root), project_id='p1')
        snapshot = project.load_snapshot(self.state, snapshot_id)
        self.assertEqual((snapshot['snapshot_id'], snapshot['project_id']), (snapshot_id, 'p1'))
        digest = {f['path']: f['sha256'] for f in snapshot['manifest']['files']}
        write(self.root, {'b.txt': 'beta changed after the scan'})
        out = self.temp / 'staging'
        staged = project.stage(snapshot, [digest['a.txt'], digest['b.txt']], out)
        self.assertEqual((staged['staged'], staged['changed']), ([digest['a.txt']], ['b.txt']))  # 改过的源文件不会以旧哈希的名义上传
        self.assertEqual({path.name: path.read_bytes() for path in out.iterdir()}, {digest['a.txt']: b'alpha'})
        write(self.root, {'a.txt': 'alpha changed later'})
        self.assertEqual((out / digest['a.txt']).read_bytes(), b'alpha')  # 暂存的副本不随源文件变化
        with self.assertRaises(project.ProjectError) as missing:
            project.load_snapshot(self.state, str(uuid.uuid4()))
        self.assertEqual(missing.exception.code, 'no_snapshot')


class ApplyCase(Case):
    def setUp(self):
        super().setUp()
        self.blobs = {}
        write(self.root, {'a.txt': 'a0', 'b.txt': 'b0', 'old.txt': 'remove me', 'keep/untouched.txt': 'same'})
        self.base = project.scan(self.root, exclude=[])['manifest']

    def result(self, changes, base=None):
        """在基线内容上做改动得到成果 manifest；changes 里值为 None 表示删除。"""
        files = {f['path']: dict(f) for f in (base or self.base)['files']}
        for name, data in changes.items():
            if data is None:
                files.pop(name, None)
            else:
                data = data.encode('utf-8') if isinstance(data, str) else data
                digest = p.sha256_bytes(data)
                self.blobs[digest] = data
                files[name] = {'path': name, 'sha256': digest, 'size': len(data), 'executable': False}
        return p.validate_manifest({'version': 1, 'base': None, 'files': list(files.values()), 'deleted': []})

    def fetch(self, digest):
        return self.blobs[digest]

    def apply(self, result, **options):
        options.setdefault('exclude', [])
        return project.apply(self.root, options.pop('base', self.base), result, self.fetch, self.state, str(uuid.uuid4()), **options)


class ApplyTests(ApplyCase):
    def test_only_the_tasks_changes_are_written_and_everything_else_is_left_alone(self):
        result = self.result({'a.txt': 'a1', 'new/added.txt': 'added', 'old.txt': None})
        write(self.root, {'local-notes.txt': 'created while the task ran', 'keep/untouched.txt': 'edited locally, not part of the result',
                          '.env': 'SECRET'})
        outline = project.plan(self.root, self.base, result, exclude=[])
        self.assertEqual([(c['path'], c['action'], c['state']) for c in outline['changes']],
                         [('a.txt', 'modify', 'clean'), ('new/added.txt', 'add', 'clean'), ('old.txt', 'delete', 'clean')])
        self.assertEqual(outline['summary'], {'add': 1, 'modify': 1, 'delete': 1, 'clean': 3, 'already': 0, 'conflict': 0, 'bytes': 7})
        before = tree(self.root)
        self.assertEqual(tree(self.root), before)  # 计划只读

        done = self.apply(result)
        self.assertEqual((done['status'], sorted(done['changed']), done['conflicts'], done['errors'], done['pending']),
                         ('applied', ['a.txt', 'new/added.txt', 'old.txt'], [], [], []))
        self.assertEqual(tree(self.root), {'.env': b'SECRET', 'a.txt': b'a1', 'b.txt': b'b0', 'keep/untouched.txt': b'edited locally, not part of the result',
                                           'local-notes.txt': b'created while the task ran', 'new/added.txt': b'added'})
        self.assertEqual(tree(done['backup']), {'a.txt': b'a0', 'old.txt': b'remove me'})  # 被替换与被删除的原文件有备份
        self.assertEqual(project.unfinished(self.state), [])
        again = self.apply(result)  # 再应用一次同一份成果：目录里已经是它的内容
        self.assertEqual((again['status'], again['changed'], sorted(again['already'])), ('applied', [], ['a.txt', 'new/added.txt', 'old.txt']))

    def test_files_edited_during_the_task_are_conflicts_and_are_not_overwritten(self):
        result = self.result({'a.txt': 'a1', 'b.txt': 'b1', 'old.txt': None, 'new.txt': 'n'})
        write(self.root, {'a.txt': 'my own edit', 'new.txt': 'i created this too'})
        (self.root / 'old.txt').write_text('i changed the file the task deletes')
        before = tree(self.root)
        refused = self.apply(result)
        self.assertEqual((refused['status'], refused['changed'], refused['backup']), ('conflict', [], None))
        self.assertEqual(sorted((c['path'], c['reason']) for c in refused['conflicts']),
                         [('a.txt', 'modified_locally'), ('new.txt', 'created_locally'), ('old.txt', 'modified_locally')])
        self.assertEqual((tree(self.root), refused['pending']), (before, ['b.txt']))  # 有冲突时缺省什么都不写

        # 明确选择“只应用没有冲突的文件”：其余照写，冲突的保留本机内容，结果是 partial 而不是 applied。
        partial = self.apply(result, skip_conflicts=True)
        self.assertEqual((partial['status'], partial['changed'], len(partial['conflicts'])), ('partial', ['b.txt'], 3))
        self.assertEqual(tree(self.root), dict(before, **{'b.txt': b'b1'}))

    def test_deleted_locally_and_identical_local_changes(self):
        result = self.result({'a.txt': 'a1', 'b.txt': 'b1', 'old.txt': None, 'new.txt': 'n'})
        (self.root / 'a.txt').unlink()                 # 任务要改的文件被本机删了：冲突
        (self.root / 'old.txt').unlink()               # 任务要删的文件本机已经删了：不算冲突
        write(self.root, {'b.txt': 'b1', 'new.txt': 'n'})  # 本机做了与成果相同的改动：不算冲突
        outline = project.plan(self.root, self.base, result, exclude=[])
        self.assertEqual({c['path']: (c['state'], c.get('reason')) for c in outline['changes']},
                         {'a.txt': ('conflict', 'deleted_locally'), 'b.txt': ('already', None), 'new.txt': ('already', None), 'old.txt': ('already', None)})

    def test_excluded_paths_are_neither_written_nor_deleted(self):
        write(self.root, {'cache/__pycache__/m.pyc': b'local cache', 'notes.log': 'local log'})
        base = project.scan(self.root, exclude=[])['manifest']
        # 成果里没有这些被排除的路径（节点的快照不含它们），还带来一个被排除的新文件。
        result = self.result({'cache/__pycache__/m.pyc': None, 'notes.log': None, 'a.txt': 'a1', 'other/__pycache__/x.pyc': b'remote cache'}, base=base)
        done = self.apply(result, base=base, exclude=['__pycache__/**', '*.log'])
        self.assertEqual((done['status'], done['changed']), ('applied', ['a.txt']))
        self.assertEqual((self.root / 'cache/__pycache__/m.pyc').read_bytes(), b'local cache')
        self.assertEqual(((self.root / 'notes.log').read_text(), (self.root / 'other').exists()), ('local log', False))

    def test_results_of_concurrent_tasks_do_not_overwrite_each_other(self):
        first = self.result({'a.txt': 'from task one'})
        second = self.result({'a.txt': 'from task two', 'b.txt': 'b from task two'})
        disjoint = self.result({'keep/untouched.txt': 'from task three'})
        self.assertEqual(self.apply(first)['status'], 'applied')
        # 第二个任务也改了 a.txt，但它的基线里 a.txt 还是旧内容：再次核对时发现目录已不是基线，保留先应用的结果。
        refused = self.apply(second)
        self.assertEqual((refused['status'], refused['conflicts'], (self.root / 'a.txt').read_text(), (self.root / 'b.txt').read_text()),
                         ('conflict', [{'path': 'a.txt', 'reason': 'modified_locally'}], 'from task one', 'b0'))
        partial = self.apply(second, skip_conflicts=True)
        self.assertEqual((partial['status'], partial['changed'], (self.root / 'a.txt').read_text()), ('partial', ['b.txt'], 'from task one'))
        # 改动互不重叠的任务可以先后应用。
        self.assertEqual((self.apply(disjoint)['status'], (self.root / 'keep/untouched.txt').read_text()), ('applied', 'from task three'))

    def test_result_paths_cannot_leave_the_project_folder(self):
        for bad in ('../escape.txt', '/abs.txt', 'C:/x.txt', 'a/../../b.txt'):
            with self.assertRaises(ValueError):
                p.validate_manifest({'version': 1, 'base': None, 'deleted': [], 'files': [{'path': bad, 'sha256': '0' * 64, 'size': 0, 'executable': False}]})
        outside = self.temp / 'outside'
        outside.mkdir()
        self.link(self.root / 'linked', outside, directory=True)
        result = self.result({'linked/planted.txt': 'x', 'a.txt': 'a1'})
        outline = project.plan(self.root, self.base, result, exclude=[])
        self.assertEqual({c['path']: (c['state'], c.get('reason')) for c in outline['changes']},
                         {'a.txt': ('clean', None), 'linked/planted.txt': ('conflict', 'unsafe_path')})
        self.apply(result, skip_conflicts=True)
        self.assertEqual(list(outside.iterdir()), [])  # 没有经链接写到目录之外

    def test_content_is_verified_and_space_is_checked_before_the_first_write(self):
        result = self.result({'a.txt': 'a1', 'b.txt': 'b1'})
        before = tree(self.root)
        digest = next(f['sha256'] for f in result['files'] if f['path'] == 'b.txt')
        good = self.blobs[digest]
        self.blobs[digest] = b'corrupted download'
        with self.assertRaises(project.ProjectError) as mismatch:
            self.apply(result)
        self.blobs[digest] = good
        with patch.object(project.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(10 ** 9, 10 ** 9, 1024)), \
                self.assertRaises(project.ProjectError) as space:
            self.apply(result)

        def refuse(outline):
            raise RuntimeError('controller did not accept the application')
        with self.assertRaises(RuntimeError):
            self.apply(result, before_write=refuse)
        self.assertEqual((mismatch.exception.code, space.exception.code, tree(self.root), project.unfinished(self.state)),
                         ('blob_mismatch', 'insufficient_space', before, []))
        left = [x for x in (self.state / 'applies').rglob('*') if x.is_file()] if (self.state / 'applies').exists() else []
        self.assertEqual(left, [])  # 三种情况都没有开始写，也没有留下操作记录（日志与目录标记都已清掉）

    def test_a_file_changed_between_review_and_write_is_not_overwritten(self):
        result = self.result({'a.txt': 'a1', 'b.txt': 'b1'})

        def edit_after_review(outline):
            write(self.root, {'b.txt': 'edited after the plan was reviewed'})
        done = self.apply(result, before_write=edit_after_review)
        self.assertEqual((done['status'], done['changed'], done['conflicts']), ('partial', ['a.txt'], [{'path': 'b.txt', 'reason': 'changed_since_plan'}]))
        self.assertEqual((self.root / 'b.txt').read_text(), 'edited after the plan was reviewed')

    def test_permission_and_disk_full_errors_end_as_partial_with_the_rest_reported(self):
        result = self.result({'a.txt': 'a1', 'b.txt': 'b1', 'c.txt': 'c1'})
        real = project.atomic_write

        def failing(code):
            def write_file(path, data, mode=0o600):
                if Path(path).name == 'b.txt':
                    raise OSError(code, os.strerror(code))
                return real(path, data, mode)
            return write_file
        with patch.object(project, 'atomic_write', side_effect=failing(errno.EACCES)):
            denied = self.apply(result)
        self.assertEqual((denied['status'], denied['changed'], [e['path'] for e in denied['errors']], denied['pending']),
                         ('partial', ['a.txt', 'c.txt'], ['b.txt'], []))
        # 磁盘写满：后面的不再尝试，如实列为未完成；先恢复现场再试。
        project.recover(self.state, denied['application_id'], 'rollback')
        self.assertEqual(tree(self.root)['a.txt'], b'a0')
        with patch.object(project, 'atomic_write', side_effect=failing(errno.ENOSPC)):
            full = self.apply(result)
        self.assertEqual((full['status'], full['changed'], [e['path'] for e in full['errors']], full['pending']),
                         ('partial', ['a.txt'], ['b.txt'], ['c.txt']))
        self.assertEqual(project.unfinished(self.state), [])  # 有了明确结果，不算中断


class SafetyTests(ApplyCase):
    def test_mapped_locations_cannot_leave_the_root(self):
        for mapped in ('../outside.txt', '/etc/passwd', 'C:/Windows/win.ini', 'a/../../x'):
            with self.assertRaises(ValueError, msg=mapped):
                project.local_path(self.root, 'a.txt', {'a.txt': mapped})

    def test_only_one_application_at_a_time_claims_a_directory(self):
        marker = project._root_marker(self.state, project.resolve_root(self.root))
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('', encoding='utf-8')  # 另一次应用刚占住目录、还没写进内容
        with self.assertRaises(project.ProjectError) as caught:
            self.apply(self.result({'a.txt': 'a1'}))
        self.assertEqual((caught.exception.code, (self.root / 'a.txt').read_text()), ('unfinished_application', 'a0'))
        # 没占到目录就没有开始：不向 controller 登记，也不留下日志。
        registered = []
        with self.assertRaises(project.ProjectError):
            self.apply(self.result({'a.txt': 'a1'}), before_write=registered.append)
        self.assertEqual(registered, [])
        self.assertEqual([d.name for d in (Path(self.state) / 'applies').iterdir() if d.name != 'roots'], [])

    def test_rollback_keeps_a_file_edited_after_the_status_check(self):
        real, calls = project._write_one, []

        def crash(root, change, *rest):
            if calls:
                raise KeyboardInterrupt()
            calls.append(change['path'])
            return real(root, change, *rest)
        application = str(uuid.uuid4())
        with patch.object(project, '_write_one', side_effect=crash), self.assertRaises(KeyboardInterrupt):
            project.apply(self.root, self.base, self.result({'a.txt': 'a1', 'b.txt': 'b1'}), self.fetch, self.state, application, exclude=[])
        real_hash = project.file_hash

        def edited_meanwhile(path):
            value = real_hash(path)
            if Path(path).name == 'a.txt':
                edited_meanwhile.calls += 1
                if edited_meanwhile.calls == 2:  # 第一次是开头的现状核对，第二次是还原前的复核：用户恰在两者之间改了它
                    write(self.root, {'a.txt': 'edited during rollback'})
                    value = real_hash(path)
            return value
        edited_meanwhile.calls = 0
        with patch.object(project, 'file_hash', side_effect=edited_meanwhile):
            rolled = project.recover(self.state, application, 'rollback')
        self.assertEqual((rolled['restored'], rolled['kept'], (self.root / 'a.txt').read_text()), ([], ['a.txt'], 'edited during rollback'))


class ProtectedPathTests(ApplyCase):
    def test_results_never_write_version_control_metadata_whatever_the_contract_excludes(self):
        result = self.result({'.GIT/hooks/pre-commit': 'echo owned', 'sub/.Hg/hgrc': 'x', 'mod/.git/config': 'x', 'a.txt': 'a1'})
        outline = project.plan(self.root, self.base, result, exclude=[])
        self.assertEqual([c['path'] for c in outline['changes']], ['a.txt'])  # 任何一级、不分大小写
        # 映射到版本库目录的路径同样不写。
        mapped = project.plan(self.root, self.base, self.result({'b.txt': 'b1'}), exclude=[], locations={'b.txt': '.git/b.txt'})
        self.assertEqual(mapped['changes'], [])
        self.assertEqual(self.apply(result)['status'], 'applied')
        self.assertFalse((self.root / '.git').exists())


class CancelTests(ApplyCase):
    """取消只在两个文件之间生效：正在写的文件写完（备份、替换、记日志），不开始下一个；取消不等于回滚。"""

    def test_cancel_between_the_first_and_second_file_stops_before_the_second(self):
        result = self.result({'a.txt': 'a1', 'b.txt': 'b1'})
        flag, seen = [], []

        def barrier(index):
            # 屏障：第一个文件已写完、第二个还没开始时请求取消。
            seen.append((index, (self.root / 'a.txt').read_text()))
            if index == 1:
                flag.append(True)
        application = str(uuid.uuid4())
        done = project.apply(self.root, self.base, result, self.fetch, self.state, application, exclude=[],
                             cancelled=lambda: bool(flag), before_file=barrier)
        self.assertEqual(seen, [(0, 'a0'), (1, 'a1')])
        self.assertEqual((done['status'], done['cancelled'], done['changed'], done['pending'], done['errors']),
                         ('partial', True, ['a.txt'], ['b.txt'], []))
        self.assertEqual(((self.root / 'a.txt').read_text(), (self.root / 'b.txt').read_text()), ('a1', 'b0'))  # 第二个文件未动
        # 第一个文件可追踪：日志记了它，备份里是原内容；这次应用已有最终结果，目录可以开始下一次应用。
        journal = Path(self.state) / 'applies' / application
        finished = [json.loads(line) for line in (journal / 'done.log').read_text().splitlines()]
        self.assertEqual([e['path'] for e in finished if not e.get('started')], ['a.txt'])  # 只有写完的那一个
        self.assertEqual((journal / 'backup' / 'a.txt').read_text(), 'a0')
        self.assertEqual(json.loads((journal / 'journal.json').read_text())['status'], 'partial')
        self.assertEqual(project.unfinished(self.state), [])
        status = project.recover(self.state, application)
        self.assertEqual((status['status'], status['written'], status['remaining']), ('partial', ['a.txt'], ['b.txt']))

    def test_cancel_before_any_write_writes_nothing_and_after_the_last_file_changes_nothing(self):
        result = self.result({'a.txt': 'a1', 'b.txt': 'b1'})
        before = tree(self.root)
        with self.assertRaises(project.ProjectError) as stopped:
            self.apply(result, cancelled=lambda: True)
        self.assertEqual((stopped.exception.code, tree(self.root), project.unfinished(self.state)), ('cancelled', before, []))
        self.assertFalse((Path(self.state) / 'applies').exists() and any(p.name != 'roots' for p in (Path(self.state) / 'applies').iterdir()))

        # 最后一个文件写完之后才到的取消：结果照实是 applied。
        flag = []
        done = self.apply(result, cancelled=lambda: bool(flag), progress=lambda value: flag.append(True) if value['done'] == 2 else None)
        self.assertEqual((done['status'], done['cancelled'], sorted(done['changed'])), ('applied', False, ['a.txt', 'b.txt']))


class HelperCancelTests(ApplyCase):
    """桌面外壳用的取消通道：`project local apply --cancel-file`。助手进程在写第二个文件之前停在屏障上，
    外壳创建取消文件，助手写完当前文件后停下并如实返回部分结果；助手进程没有被结束。"""

    def test_cancel_file_stops_the_helper_after_the_current_file(self):
        import sys
        import time
        result = self.result({'a.txt': 'a1', 'b.txt': 'b1'})
        work = Path(self.state).parent / 'helper'
        work.mkdir()
        blobs = work / 'blobs'
        blobs.mkdir()
        for digest, data in self.blobs.items():
            (blobs / digest).write_bytes(data)
        (work / 'base.json').write_text(json.dumps(self.base), encoding='utf-8')
        (work / 'result.json').write_text(json.dumps(result), encoding='utf-8')
        (work / 'none.json').write_text('[]', encoding='utf-8')
        gate, cancel = work / 'gate', work / 'cancel'
        gate.touch()
        env = dict(os.environ, AGENT_COLLAB_FAULT_APPLY_GATE=str(gate), PYTHONIOENCODING='utf-8')
        helper = subprocess.Popen([sys.executable, '-B', '-m', 'agent_collab.cli', 'project', 'local', 'apply', '--state', str(self.state),
                                   '--root', str(self.root), '--base', str(work / 'base.json'), '--result', str(work / 'result.json'),
                                   '--blobs', str(blobs), '--application', str(uuid.uuid4()), '--exclude-file', str(work / 'none.json'),
                                   '--cancel-file', str(cancel)],
                                  cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        deadline = time.time() + 60
        while not Path(str(gate) + '.1').exists() and helper.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(Path(str(gate) + '.1').exists(), helper.stderr.read() if helper.poll() is not None else 'timeout')
        self.assertEqual(((self.root / 'a.txt').read_text(), (self.root / 'b.txt').read_text()), ('a1', 'b0'))  # 第一个已写，第二个未开始
        cancel.touch()   # 外壳的取消请求
        gate.unlink()    # 放开屏障
        out, _ = helper.communicate(timeout=60)
        self.assertEqual(helper.returncode, 0)
        lines = [json.loads(line) for line in out.splitlines() if line.strip()]
        done = lines[-1]['result']
        self.assertEqual((done['status'], done['cancelled'], done['changed'], done['pending']), ('partial', True, ['a.txt'], ['b.txt']))
        self.assertEqual([line['progress']['phase'] for line in lines if 'progress' in line], ['write'])
        self.assertEqual((self.root / 'b.txt').read_text(), 'b0')


class InterruptionTests(ApplyCase):
    def interrupted(self):
        """写完第一个文件后进程被打断（窗口被关掉、掉电）。"""
        result = self.result({'a.txt': 'a1', 'b.txt': 'b1', 'new.txt': 'n', 'old.txt': None})
        real, calls = project._write_one, []

        def crash(root, change, *rest):
            if calls:
                raise KeyboardInterrupt()
            calls.append(change['path'])
            return real(root, change, *rest)
        application = str(uuid.uuid4())
        with patch.object(project, '_write_one', side_effect=crash), self.assertRaises(KeyboardInterrupt):
            project.apply(self.root, self.base, result, self.fetch, self.state, application, exclude=[])
        return result, application

    def test_interrupted_application_is_reported_blocks_the_next_one_and_can_be_resumed(self):
        result, application = self.interrupted()
        pending = project.unfinished(self.state, self.root)
        self.assertEqual([entry['application_id'] for entry in pending], [application])
        status = project.recover(self.state, application)
        self.assertEqual((status['status'], status['interrupted'], status['written'], status['remaining'], status['diverged']),
                         ('writing', True, ['a.txt'], ['b.txt', 'new.txt', 'old.txt'], []))
        with self.assertRaises(project.ProjectError) as blocked:
            self.apply(result)
        self.assertEqual((blocked.exception.code, blocked.exception.detail['application_id']), ('unfinished_application', application))

        resumed = project.recover(self.state, application, 'resume', fetch=self.fetch)
        self.assertEqual((resumed['status'], sorted(resumed['changed']), resumed['conflicts'], resumed['errors']),
                         ('applied', ['a.txt', 'b.txt', 'new.txt', 'old.txt'], [], []))
        self.assertEqual(tree(self.root), {'a.txt': b'a1', 'b.txt': b'b1', 'keep/untouched.txt': b'same', 'new.txt': b'n'})
        self.assertEqual((project.unfinished(self.state), project.recover(self.state, application)['status']), ([], 'applied'))

    def test_interrupted_application_can_be_rolled_back_without_touching_later_edits(self):
        original = tree(self.root)
        result, application = self.interrupted()
        rolled = project.recover(self.state, application, 'rollback')
        self.assertEqual((rolled['status'], rolled['restored'], rolled['kept'], tree(self.root), project.unfinished(self.state)),
                         ('rolled_back', ['a.txt'], [], original, []))

        # 另一次中断后，用户又改了已经写入的那个文件：回滚不覆盖他的修改，并如实报告。
        result, application = self.interrupted()
        write(self.root, {'a.txt': 'edited after the interrupted apply'})
        status = project.recover(self.state, application)
        self.assertEqual((status['written'], status['diverged']), ([], ['a.txt']))
        rolled = project.recover(self.state, application, 'rollback')
        self.assertEqual((rolled['status'], rolled['restored'], rolled['kept'], (self.root / 'a.txt').read_text()),
                         ('partial', [], ['a.txt'], 'edited after the interrupted apply'))

    @unittest.skipIf(os.name == 'nt', '可执行位只在 POSIX 上记录')
    def test_mode_only_change_interrupted_before_its_write_is_remaining_and_rollback_keeps_the_file(self):
        result = self.result({'a.txt': 'a1', 'b.txt': 'b0'})
        for entry in result['files']:
            if entry['path'] == 'b.txt':
                entry['executable'] = True
        real, calls = project._write_one, []

        def crash(root, change, *rest):
            if calls:
                raise KeyboardInterrupt()
            calls.append(change['path'])
            return real(root, change, *rest)
        application = str(uuid.uuid4())
        with patch.object(project, '_write_one', side_effect=crash), self.assertRaises(KeyboardInterrupt):
            project.apply(self.root, self.base, result, self.fetch, self.state, application, exclude=[])
        status = project.recover(self.state, application)
        self.assertEqual((status['written'], status['remaining'], status['diverged']), (['a.txt'], ['b.txt'], []))
        rolled = project.recover(self.state, application, 'rollback')
        self.assertEqual((rolled['status'], rolled['restored'], rolled['kept']), ('rolled_back', ['a.txt'], []))
        self.assertEqual(((self.root / 'a.txt').read_text(), (self.root / 'b.txt').read_text()), ('a0', 'b0'))
        self.assertEqual([os.stat(str(self.root / n)).st_mode & 0o111 for n in ('a.txt', 'b.txt')], [0, 0])  # 权限位也没被改

    def test_truncated_last_log_line_is_dropped_and_a_corrupt_middle_line_is_refused(self):
        result, application = self.interrupted()
        log = project._journal_dir(self.state, application) / 'done.log'
        log.write_bytes(log.read_bytes() + b'{"path": "b.t')  # 追加写到一半被打断
        self.assertEqual(project.recover(self.state, application)['written'], ['a.txt'])
        self.assertTrue(log.read_bytes().endswith(b'\n'))  # 残行已去掉，之后的追加不会接在它后面
        log.write_bytes(b'not json\n' + log.read_bytes())
        with self.assertRaises(project.ProjectError) as caught:
            project.recover(self.state, application)
        self.assertEqual(caught.exception.code, 'journal_corrupt')

    def test_resume_of_an_old_journal_never_writes_version_control_metadata(self):
        result, application = self.interrupted()
        journal = project._journal_dir(self.state, application)
        record = json.loads((journal / 'journal.json').read_text(encoding='utf-8'))
        data = b'echo owned'
        self.blobs[p.sha256_bytes(data)] = data
        hook = {'path': '.git/hooks/pre-commit', 'sha256': p.sha256_bytes(data), 'size': len(data), 'executable': True}
        record['changes'].append({'path': hook['path'], 'action': 'add', 'size': len(data), 'state': 'clean', 'expect': None})
        record['files'][hook['path']] = hook  # 旧版本写下的日志：计划里有版本库元数据
        (journal / 'journal.json').write_text(json.dumps(record), encoding='utf-8')
        resumed = project.recover(self.state, application, 'resume', fetch=self.fetch)
        self.assertIn({'path': '.git/hooks/pre-commit', 'reason': 'protected_path'}, resumed['conflicts'])
        self.assertFalse((self.root / '.git').exists())

    def test_complete_last_log_line_without_its_newline_is_terminated_before_appending(self):
        result, application = self.interrupted()
        log = project._journal_dir(self.state, application) / 'done.log'
        log.write_bytes(log.read_bytes().rstrip(b'\n'))  # 最后一条完整，换行符没来得及写
        project.recover(self.state, application)
        self.assertTrue(log.read_bytes().endswith(b'\n'))
        resumed = project.recover(self.state, application, 'resume', fetch=self.fetch)
        self.assertEqual(resumed['status'], 'applied')
        project.recover(self.state, application)  # 续写追加的记录没有和旧的连在一起

    def test_delete_recreated_as_a_directory_is_not_reported_as_done(self):
        result = self.result({'a.txt': 'a1', 'old.txt': None})
        real, calls = project._write_one, []

        def crash(root, change, *rest):
            if calls:
                raise KeyboardInterrupt()
            calls.append(change['path'])
            return real(root, change, *rest)
        application = str(uuid.uuid4())
        with patch.object(project, '_write_one', side_effect=crash), self.assertRaises(KeyboardInterrupt):
            project.apply(self.root, self.base, result, self.fetch, self.state, application, exclude=[])
        (self.root / 'old.txt').unlink()
        (self.root / 'old.txt').mkdir()
        self.assertEqual(project.recover(self.state, application)['diverged'], ['old.txt'])

    def test_same_content_file_that_appeared_without_a_write_record_is_not_deleted(self):
        result = self.result({'a.txt': 'a1', 'new.txt': 'n'})
        real, calls = project._write_one, []

        def crash(root, change, *rest):
            if calls:
                raise KeyboardInterrupt()
            calls.append(change['path'])
            return real(root, change, *rest)
        application = str(uuid.uuid4())
        with patch.object(project, '_write_one', side_effect=crash), self.assertRaises(KeyboardInterrupt):
            project.apply(self.root, self.base, result, self.fetch, self.state, application, exclude=[])
        write(self.root, {'new.txt': 'n'})  # 用户自己建了内容相同的文件；本次应用没开始写它
        status = project.recover(self.state, application)
        self.assertNotIn('new.txt', status['written'])
        project.recover(self.state, application, 'rollback')
        self.assertEqual((self.root / 'new.txt').read_text(), 'n')

    def test_written_file_without_backup_is_kept_not_deleted_on_rollback(self):
        result, application = self.interrupted()
        journal = project._journal_dir(self.state, application)
        (journal / 'backup' / 'a.txt').unlink()
        rolled = project.recover(self.state, application, 'rollback')
        self.assertEqual((rolled['status'], rolled['restored'], rolled['kept'], (self.root / 'a.txt').read_text()),
                         ('partial', [], ['a.txt'], 'a1'))

    def test_marker_left_behind_after_a_final_result_does_not_block_the_next_application(self):
        done = self.apply(self.result({'a.txt': 'a1'}))
        marker = project._root_marker(self.state, project.resolve_root(self.root))
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({'application_id': done['application_id'], 'root': str(project.resolve_root(self.root))}), encoding='utf-8')
        self.assertEqual(project.unfinished(self.state, self.root), [])
        self.assertFalse(marker.exists())
        current = project.scan(self.root, exclude=[])['manifest']
        self.assertEqual(self.apply(self.result({'a.txt': 'a2'}, base=current), base=current)['status'], 'applied')

    def test_abandoning_records_the_outcome_as_unknown_not_as_applied(self):
        result, application = self.interrupted()
        left = project.recover(self.state, application, 'abandon')
        self.assertEqual((left['status'], left['written'], len(left['remaining'])), ('unknown', ['a.txt'], 3))
        self.assertEqual(project.unfinished(self.state), [])
        with self.assertRaises(project.ProjectError) as missing:
            project.recover(self.state, str(uuid.uuid4()))
        self.assertEqual(missing.exception.code, 'no_application')


if __name__ == '__main__':
    unittest.main()
