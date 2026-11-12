"""项目闭环的端到端：真实 HTTP controller + 子进程节点（确定性步骤，不调用模型）+ CLI。

本机目录 → 推送输入（增量、可重复）→ 任务在节点执行 → 发布 → 应用回本机目录；
用户在任务期间的修改保留并显示冲突，部分应用不被记为已回写。
"""
import json
import os
from pathlib import Path
import unittest
import uuid

import test_cli_e2e
import test_e2e_local as e2e


@unittest.skipUnless(e2e.sockets_ok(), 'loopback socket 不可用')
class ProjectE2E(test_cli_e2e.CliE2E):  # 经模块引用基类：不把基类放进本模块，免得被重复发现
    test_cli_and_another_client_share_tasks_state_settings_and_revocation = None  # 只借用夹具，不重跑基类的用例

    def files(self, root):
        root = Path(root)
        return {path.relative_to(root).as_posix(): path.read_text(encoding='utf-8') for path in sorted(root.rglob('*')) if path.is_file()}

    def run_job(self, project_id, steps, title):
        """按项目最近登记的输入草拟一个确定性任务，把步骤换成给定的文件改动，提交并等它发布。"""
        draft = self.cli('api', 'POST', '/contracts/draft', '--data', json.dumps({'template': 'diagnostic', 'goal': title, 'project_id': project_id}))['body']
        self.assertEqual(draft['warnings'], [])  # 输入内容已全部上传
        contract = dict(draft['contract'], harness_params={'steps': steps})
        body = {'title': title, 'contract': contract, 'project_id': project_id, 'expected_settings_revision': draft['settings_revision'],
                'expected_project_revision': draft['project_revision']}
        job_file = self.tmp / ('job-%s.json' % uuid.uuid4().hex)
        job_file.write_text(json.dumps(body), encoding='utf-8')
        job_id = self.cli('submit', str(job_file))['body']['job_id']
        done = self.cli('job', 'wait', job_id, '--timeout', '120')
        self.assertEqual((done['outcome'], done['delivery_version']), ('done', 1))
        return job_id

    def test_local_folder_to_node_and_back_with_conflicts_kept(self):
        self.agent('n1', 'h1', 'd1')
        self.cli('client', 'pair', '--url', self.url, '--code', self.code('owner'), '--name', 'laptop')
        folder = self.tmp / 'my-project'
        for name, text in {'src/app.txt': 'version 1\n', 'README.md': 'old readme\n', 'docs/guide.txt': 'guide\n',
                           'notes.log': 'local log, excluded by the project\n', '.env': 'SECRET=never uploaded\n'}.items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text(text, encoding='utf-8')
        project = self.cli('project', 'create', '--name', 'demo', '--exclude', '*.log')
        pid = project['project_id']
        self.assertEqual(self.cli('project', 'push', pid, expect=2)['error']['code'], 'not_bound')
        self.cli('project', 'bind', pid, str(folder))

        # 推送：只上传范围内的文件；凭据与被排除的文件不在其中。再推一次内容没变，不重复上传也不新建基线。
        scanned = self.cli('project', 'scan', '--project', pid)
        self.assertEqual((scanned['files'], sorted(scanned['excluded']['top'])), (3, ['.env', 'notes.log']))
        pushed = self.cli('project', 'push', pid)
        self.assertEqual((pushed['files'], pushed['uploaded_blobs'], pushed['reused'], pushed['missing_blobs']), (3, 3, False, []))
        again = self.cli('project', 'push', pid)
        self.assertEqual((again['uploaded_blobs'], again['reused'], again['baseline_id']), (0, True, pushed['baseline_id']))
        baselines = self.api('GET', '/baselines?project_id=' + pid)['baselines']
        self.assertEqual((len(baselines), baselines[0]['source']['root_name'], baselines[0]['source']['excluded']), (1, 'my-project', 2))
        self.assertNotIn(str(folder), json.dumps(baselines))  # 本机的绝对路径不进 controller

        # 任务在节点上改一个文件、加一个文件、删一个文件；本机同时有不相干的新文件。
        job_id = self.run_job(pid, [{'write': {'path': 'src/app.txt', 'content': 'version 2 from the task\n'}},
                                    {'write': {'path': 'out/result.txt', 'content': 'produced\n'}},
                                    {'delete': {'path': 'README.md'}}], '第一次任务')
        (folder / 'scratch.txt').write_text('created locally while the task ran\n', encoding='utf-8')
        plan = self.cli('project', 'plan', job_id)
        self.assertEqual([(c['path'], c['action'], c['state']) for c in plan['changes']],
                         [('README.md', 'delete', 'clean'), ('out/result.txt', 'add', 'clean'), ('src/app.txt', 'modify', 'clean')])
        before = self.api('GET', '/jobs/' + job_id)
        self.assertEqual((before['integration_status'], before['applications'], before['downloads']), ('none', [], []))  # 已发布 ≠ 已应用

        applied = self.cli('project', 'apply', job_id)
        self.assertEqual((applied['status'], applied['reported'], sorted(applied['changed'])), ('applied', 'applied', ['README.md', 'out/result.txt', 'src/app.txt']))
        self.assertEqual(self.files(folder), {
            '.env': 'SECRET=never uploaded\n', 'docs/guide.txt': 'guide\n', 'notes.log': 'local log, excluded by the project\n',
            'out/result.txt': 'produced\n', 'scratch.txt': 'created locally while the task ran\n', 'src/app.txt': 'version 2 from the task\n'})
        after = self.api('GET', '/jobs/' + job_id)
        self.assertEqual((after['integration_status'], [a['status'] for a in after['applications']], after['applications'][0]['by']['name']),
                         ('integrated', ['applied'], 'laptop'))
        self.assertEqual(self.cli('project', 'show', pid)['applied']['job_id'], job_id)

        # 整包下载到另一个目录是另一件事：它被单独登记，不改变回写状态。
        target = self.tmp / 'download'
        self.cli('job', 'artifacts', job_id, '--download', str(target))
        self.assertEqual(sorted(self.files(target)), ['docs/guide.txt', 'out/result.txt', 'src/app.txt'])
        self.assertEqual([(d['files'], d['by']['name']) for d in self.api('GET', '/jobs/' + job_id)['downloads']], [(3, 'laptop')])

        # 第二轮：推送当前内容，任务改两个文件；任务期间用户改了其中一个。
        second = self.cli('project', 'push', pid)
        self.assertEqual((second['reused'], second['uploaded_blobs'] > 0), (False, True))
        job_id = self.run_job(pid, [{'write': {'path': 'src/app.txt', 'content': 'version 3 from the task\n'}},
                                    {'write': {'path': 'docs/guide.txt', 'content': 'guide, revised by the task\n'}}], '第二次任务')
        (folder / 'src/app.txt').write_text('my own edit, made while the task ran\n', encoding='utf-8')
        plan = self.cli('project', 'plan', job_id)
        self.assertEqual({c['path']: (c['state'], c.get('reason')) for c in plan['changes']},
                         {'docs/guide.txt': ('clean', None), 'src/app.txt': ('conflict', 'modified_locally')})
        snapshot = self.files(folder)
        refused = self.cli('project', 'apply', job_id, expect=5)
        self.assertEqual((refused['status'], refused['changed'], self.files(folder)), ('conflict', [], snapshot))  # 有冲突：什么都不写
        self.assertEqual(self.api('GET', '/jobs/' + job_id)['applications'], [])

        partial = self.cli('project', 'apply', job_id, '--skip-conflicts', expect=5)
        self.assertEqual((partial['status'], partial['reported'], partial['changed'], [c['path'] for c in partial['conflicts']]),
                         ('partial', 'partial', ['docs/guide.txt'], ['src/app.txt']))
        self.assertEqual(((folder / 'src/app.txt').read_text(encoding='utf-8'), (folder / 'docs/guide.txt').read_text(encoding='utf-8')),
                         ('my own edit, made while the task ran\n', 'guide, revised by the task\n'))
        job = self.api('GET', '/jobs/' + job_id)
        # 部分应用如实记录：任务的回写状态不是 integrated，项目“最近一次完整应用”仍是上一个任务。
        self.assertEqual((job['integration_status'], [a['status'] for a in job['applications']]), ('conflict', ['partial']))
        self.assertNotEqual(self.cli('project', 'show', pid)['applied']['job_id'], job_id)
        self.assertEqual(self.cli('project', 'recover')['unfinished'], [])
        # 应用结果里的备份目录是本机路径：只在本机输出里给出，不进 controller 的记录。
        self.assertTrue(partial['backup'])
        self.assertNotIn('backup', job['applications'][0]['detail'])
        self.assertNotIn(str(self.tmp), json.dumps(job['applications']))

        # 目录内容回到以前登记过的样子（X → Y → X）：是一次新的登记，不与当初那次请求冲突。
        current = (folder / 'docs/guide.txt').read_bytes()
        third = self.cli('project', 'push', pid)
        (folder / 'docs/guide.txt').write_bytes(b'a detour\n')
        self.assertFalse(self.cli('project', 'push', pid)['reused'])
        (folder / 'docs/guide.txt').write_bytes(current)
        back = self.cli('project', 'push', pid)
        self.assertEqual((back['reused'], back['uploaded_blobs'], back['manifest_sha256']), (False, 0, third['manifest_sha256']))
        self.assertEqual(self.cli('project', 'show', pid)['baseline']['baseline_id'], back['baseline_id'])

        # 归档后不再接新工作，历史照旧可读。
        self.cli('project', 'archive', pid)
        (folder / 'docs/guide.txt').write_bytes(b'changed after the last push\n')  # 内容有变，推送才需要登记新的输入
        self.assertEqual(self.cli('project', 'push', pid, expect=2)['body']['error']['code'], 'project_archived')
        self.assertEqual(len(self.api('GET', '/jobs?project_id=' + pid)['jobs']), 2)


if __name__ == '__main__':
    unittest.main()
