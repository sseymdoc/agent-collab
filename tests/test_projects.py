"""项目、项目默认值与受理冻结、输入基线、成果下载与应用记录（protocol §13.1–§13.4）。只经公开 API，不调用模型。"""
import copy
import unittest

from agent_collab import domain as d, protocol as p
from helpers import contract, manifest, uid
from test_clients_models import A, B, ClientCase, caps

ANY = {'default': {'model': A, 'effort': 'high'}, 'allowed': None}


class ProjectCase(ClientCase):
    def project(self, token=None, status=201, **body):
        body.setdefault('request_id', uid())
        body.setdefault('name', 'demo')
        return self.call('POST', '/projects', body, token=token, status=status)

    def change(self, project, token=None, status=200, **body):
        body.setdefault('request_id', uid())
        body.setdefault('expected_revision', project['revision'])
        return self.call('PUT', '/projects/' + project['project_id'], body, token=token, status=status)

    def submit(self, token=None, status=201, **body):
        body.setdefault('request_id', uid())
        body.setdefault('title', 'job')
        body.setdefault('contract', contract(harness='codex', harness_params={}))
        return self.call('POST', '/jobs', body, token=token, status=status)

    def draft(self, **body):
        body.setdefault('template', 'codex-change')
        body.setdefault('goal', 'g')
        return self.call('POST', '/contracts/draft', body)


class ProjectDefaultsTests(ProjectCase):
    def test_task_override_beats_project_default_which_beats_workspace_default(self):
        self.put(expected_revision=0, codex=ANY)
        plain = self.project(name='plain')
        tuned = self.project(name='tuned', defaults={'codex': {'model': B, 'effort': 'low', 'unverified_nodes': 'allow'}})
        self.assertEqual((plain['revision'], plain['defaults']['codex'], plain['defaults']['exclude']),
                         (1, {'model': None, 'effort': None, 'unverified_nodes': None}, None))

        # 没有项目默认值：工作区默认值。
        job = self.submit(project_id=plain['project_id'])
        self.assertEqual((job['model_policy']['resolved'], job['model_policy']['source'], job['model_policy']['unverified_nodes'],
                          job['project_id'], job['project_revision'], job['model_policy']['project_revision'], job['model_policy']['settings_revision']),
                         ({'model': A, 'effort': 'high'}, {'model': 'workspace_default', 'effort': 'workspace_default'}, 'wait',
                          plain['project_id'], 1, 1, 1))
        # 项目默认值盖过工作区默认值；未核实节点的策略同样取项目的。
        job = self.submit(project_id=tuned['project_id'])
        self.assertEqual((job['model_policy']['resolved'], job['model_policy']['source'], job['model_policy']['unverified_nodes']),
                         ({'model': B, 'effort': 'low'}, {'model': 'project_default', 'effort': 'project_default'}, 'allow'))
        # 任务上显式给出的值盖过项目默认值，逐项判断来源。
        job = self.submit(project_id=tuned['project_id'], contract=contract(harness='codex', harness_params={'effort': 'max'}))
        self.assertEqual((job['model_policy']['resolved'], job['model_policy']['source']),
                         ({'model': B, 'effort': 'max'}, {'model': 'project_default', 'effort': 'task_override'}))
        # 不属于任何项目的任务照旧。
        job = self.submit()
        self.assertEqual((job['project_id'], job['project_revision'], job['model_policy']['source']['model']), (None, None, 'workspace_default'))
        self.assertNotIn('project_revision', job['model_policy'])

    def test_later_setting_changes_do_not_rewrite_admitted_jobs_and_repairs_keep_the_frozen_policy(self):
        self.put(expected_revision=0, codex=ANY)
        project = self.project(defaults={'codex': {'model': B, 'effort': 'low'}})
        queued = self.submit(project_id=project['project_id'])
        # 项目默认值与工作区默认值都改了：排队中的任务不变，新任务用新值并记下新的版本。
        changed = self.change(project, defaults={'codex': {'model': A, 'effort': 'max'}})
        self.put(expected_revision=1, codex={'default': {'model': 'model-c', 'effort': 'high'}, 'allowed': None})
        self.assertEqual(changed['revision'], 2)
        still = self.current(queued)
        self.assertEqual((still['contract']['harness_params']['model'], still['model_policy']['resolved'], still['project_revision'],
                          still['request_hash']), (B, {'model': B, 'effort': 'low'}, 1, queued['request_hash']))
        fresh = self.submit(project_id=project['project_id'])
        self.assertEqual((fresh['model_policy']['resolved'], fresh['project_revision'], fresh['model_policy']['settings_revision']),
                         ({'model': A, 'effort': 'max'}, 2, 2))
        # 根工作包冻结了受理时的项目配置版本与解析结果：同一根的后续修订沿用它，不读后来的项目默认值。
        root = self.app.get('root', queued['work_package_id'])
        self.assertEqual((root['project_id'], root['model_policy']['project_revision'], root['model_policy']['codex']['model'],
                          root['model_policy']['codex']['effort']), (project['project_id'], 1, B, 'low'))
        node = self.node('n', capabilities=caps({A: ['max', 'high'], B: ['low']}))
        a = node.claim()
        self.assertEqual(a['job_id'], queued['job_id'])
        node.complete(a, checkpoint=node.checkpoint(a, kind='final'))
        revision = self.submit(work_package_id=queued['work_package_id'], contract=contract(harness='codex', harness_params={}))
        self.assertEqual((revision['model_policy']['resolved'], revision['model_policy']['source']['model'], revision['project_id'],
                          revision['project_revision']), ({'model': B, 'effort': 'low'}, 'root_frozen', project['project_id'], 1))
        other = self.project(name='other')
        self.submit(work_package_id=revision['work_package_id'], project_id=other['project_id'], status=400)

    def test_draft_and_submit_check_both_revisions(self):
        self.put(expected_revision=0, codex=ANY)
        project = self.project(defaults={'codex': {'model': B, 'effort': 'low'}, 'exclude': ['secrets/**']})
        draft = self.draft(project_id=project['project_id'])
        self.assertEqual((draft['settings_revision'], draft['project_revision'], draft['project_id'], draft['model_policy']['resolved'],
                          draft['preview']['workspace']['exclude']), (1, 1, project['project_id'], {'model': B, 'effort': 'low'}, ['secrets/**']))
        body = {'title': draft['title'], 'contract': draft['contract'], 'project_id': project['project_id'],
                'expected_settings_revision': draft['settings_revision'], 'expected_project_revision': draft['project_revision']}
        # 草拟之后项目默认值变了：只核对工作区版本发现不了，项目版本的核对把它拦下。
        self.change(project, defaults={'codex': {'model': A, 'effort': 'max'}, 'exclude': ['secrets/**']})  # 默认策略整体替换
        error = self.submit(status=409, **body)['error']
        self.assertEqual((error['code'], error['details']['current']['revision']), ('version_mismatch', 2))
        self.assertEqual(self.call('GET', '/jobs')['jobs'], [])
        # 重新草拟后受理；之后工作区设置变了，同样被拦下。
        draft = self.draft(project_id=project['project_id'])
        body.update(contract=draft['contract'], expected_project_revision=draft['project_revision'])
        self.put(expected_revision=1, codex=dict(ANY, unverified_nodes='allow'))
        self.assertEqual(self.submit(status=409, **body)['error']['code'], 'version_mismatch')
        body['expected_settings_revision'] = 2
        job = self.submit(**body)
        self.assertEqual((job['model_policy']['resolved'], job['contract']['workspace']['exclude']), ({'model': A, 'effort': 'max'}, ['secrets/**']))

    def test_defaults_are_settings_and_must_stay_within_the_permitted_set(self):
        self.put(expected_revision=0, codex={'default': {'model': A, 'effort': 'high'}, 'allowed': {A: ['high']}})
        operator = self.pair('operator', name='op')['client_token']
        viewer = self.pair('viewer', name='v')['client_token']
        project = self.project(token=operator)  # 建项目只要 tasks
        self.project(token=viewer, status=403)
        # 默认策略属于设置：operator 改不了，改名可以。
        denied = self.change(project, token=operator, status=403, defaults={'codex': {'model': A, 'effort': 'high'}})
        self.assertEqual(denied['error']['details'], {'required_scope': 'settings'})
        renamed = self.change(project, token=operator, name='renamed')
        self.assertEqual((renamed['name'], renamed['revision']), ('renamed', 2))
        self.change(renamed, defaults={'codex': {'model': B, 'effort': 'low'}}, status=400)       # 不在许可范围
        self.change(renamed, defaults={'codex': {'model': A}}, status=400)                        # model 与 effort 要成对
        self.change(renamed, defaults={'codex': {'model': A, 'effort': 'high'}, 'exclude': ['../x']}, status=400)
        stale = self.change(project, name='again', status=409)['error']
        self.assertEqual((stale['code'], stale['details']['current']['revision']), ('version_mismatch', 2))
        view = self.call('GET', '/projects/' + project['project_id'])
        self.assertEqual([(h['revision'], h['actor']['name'], h['after']['name']) for h in view['history']], [(1, 'op', 'demo'), (2, 'op', 'renamed')])

    def test_archived_project_keeps_its_history_and_takes_no_new_work(self):
        self.put(expected_revision=0, codex=ANY)
        project = self.project()
        job = self.submit(project_id=project['project_id'])
        baseline = self.call('POST', '/baselines', {'request_id': uid(), 'name': 'in', 'manifest': manifest(), 'project_id': project['project_id']}, status=201)
        archived = self.change(project, archived=True)
        self.assertTrue(archived['archived_at'])
        for refused in (self.submit(project_id=project['project_id'], status=409),
                        self.call('POST', '/contracts/draft', {'template': 'codex-change', 'goal': 'g', 'project_id': project['project_id']}, status=409),
                        self.call('POST', '/baselines', {'request_id': uid(), 'name': 'x', 'manifest': manifest(), 'project_id': project['project_id']}, status=409)):
            self.assertEqual(refused['error']['code'], 'project_archived')
        # 历史任务、基线与结果引用都在；没有删除端点。
        self.assertEqual(self.current(job)['project_id'], project['project_id'])
        self.assertEqual([j['job_id'] for j in self.call('GET', '/jobs?project_id=' + project['project_id'])['jobs']], [job['job_id']])
        self.assertEqual([b['baseline_id'] for b in self.call('GET', '/baselines?project_id=' + project['project_id'])['baselines']], [baseline['baseline_id']])
        listed = self.call('GET', '/projects')['projects'][0]
        self.assertEqual((listed['jobs'], listed['active_jobs'], bool(listed['archived_at'])), (1, 1, True))
        self.call('DELETE', '/projects/' + project['project_id'], status=404)
        restored = self.change(archived, archived=False)
        self.assertIsNone(restored['archived_at'])
        self.submit(project_id=project['project_id'])


class ProjectInputTests(ProjectCase):
    def test_baseline_belongs_to_a_project_and_the_job_is_not_dispatched_before_inputs_are_complete(self):
        project = self.project()
        files = {'a.txt': b'alpha', 'b.txt': b'beta'}
        value = {'version': 1, 'base': None, 'deleted': [], 'files': [
            {'path': name, 'sha256': p.sha256_bytes(data), 'size': len(data), 'executable': False} for name, data in files.items()]}
        digests = [f['sha256'] for f in value['files']]
        self.assertEqual(self.call('POST', '/blobs/missing', {'sha256': digests})['missing'], sorted(digests))
        self.call('PUT', '/blobs/' + digests[0], files['a.txt'], status=201)
        self.assertEqual(self.call('POST', '/blobs/missing', {'sha256': digests})['missing'], [digests[1]])
        source = {'device': 'desk', 'root_name': 'demo', 'git': {'branch': 'main', 'dirty': 2}, 'excluded': 3}
        body = {'request_id': uid(), 'name': 'snapshot', 'manifest': value, 'project_id': project['project_id'], 'source': source}
        baseline = self.call('POST', '/baselines', body, status=201)
        self.assertEqual((baseline['project_id'], baseline['source'], baseline['missing_blobs'], baseline['files'], baseline['bytes']),
                         (project['project_id'], source, [digests[1]], 2, 9))
        # 重复登记同一请求不产生第二份基线。
        self.assertEqual(self.call('POST', '/baselines', body, status=201)['baseline_id'], baseline['baseline_id'])
        self.assertEqual(len(self.call('GET', '/baselines')['baselines']), 1)
        self.assertEqual(self.call('GET', '/projects/' + project['project_id'])['baseline']['baseline_id'], baseline['baseline_id'])

        # 没有另行指定输入时，草拟用项目最近登记的基线；内容没传完会提示，受理后任务不派单，并说明缺什么。
        draft = self.call('POST', '/contracts/draft', {'template': 'diagnostic', 'goal': 'g', 'project_id': project['project_id']})
        self.assertEqual((draft['baseline_id'], [w['code'] for w in draft['warnings']]), (baseline['baseline_id'], ['missing_blobs']))
        node = self.node('n')
        job = self.submit(project_id=project['project_id'], contract=draft['contract'], title=draft['title'])
        waiting = self.current(job)
        self.assertEqual((waiting['protection'], waiting['status']), ('preparing', 'waiting_capability'))
        self.assertIn('missing_blobs:' + digests[1], waiting['status_reason'])
        self.assertIsNone(node.claim(status=204))
        # 缺的内容补齐：恢复保护就绪，节点领到任务。提供输入的客户端此后不需要在线。
        self.call('PUT', '/blobs/' + digests[1], files['b.txt'], status=201)
        self.assertEqual(self.current(job)['protection'], 'protected')
        self.assertEqual(node.claim()['job_id'], job['job_id'])

        other = self.project(name='other')
        self.call('POST', '/contracts/draft', {'template': 'diagnostic', 'goal': 'g', 'project_id': other['project_id'],
                                               'baseline_id': baseline['baseline_id']}, status=400)


class ResultStateTests(ProjectCase):
    """有检查点、已发布、已下载、已应用到项目是四个分开的事实。"""
    def finished(self, publish=True):
        self.project_record = self.project()
        value = contract(publish={'mode': 'manual', 'requires_acceptance': False})
        job = self.job(value, project_id=self.project_record['project_id'])
        node = self.node('n')
        a = node.claim()
        digest = node.upload(b'result')
        cp = node.checkpoint(a, manifest(b'result', 'out.txt'), kind='final')
        node.complete(a, checkpoint=cp)
        if publish:
            self.mutate(job, 'publish', attempt_id=a['attempt_id'], epoch=a['epoch'], checkpoint_id=cp['checkpoint_id'], expected_base_version=0)
        return job, cp

    def start(self, job, cp, token=None, status=201, **changes):
        body = {'request_id': uid(), 'application_id': uid(), 'job_id': job['job_id'], 'checkpoint_id': cp['checkpoint_id'],
                'device': 'desk', 'plan': {'added': 1, 'modified': 0, 'deleted': 0, 'conflicts': 0}}
        body.update(changes)
        return self.call('POST', '/projects/%s/applications' % self.project_record['project_id'], body, token=token, status=status)

    def finish(self, application, status_value, token=None, status=200, detail=None):
        return self.call('POST', '/projects/%s/applications/%s' % (self.project_record['project_id'], application['application_id']),
                         {'request_id': uid(), 'status': status_value, 'detail': detail or {'changed': ['out.txt']}}, token=token, status=status)

    def test_checkpoint_published_downloaded_and_applied_are_recorded_separately(self):
        job, cp = self.finished(publish=False)
        view = self.current(job)
        self.assertEqual((view['status'], view['delivery_version'], view['downloads'], view['applications'], view['integration_status']),
                         ('completed', 0, [], [], 'none'))
        # 有检查点但未发布：可以下载查看，不能应用到项目。
        self.assertEqual(self.start(job, cp, status=409)['error']['code'], 'conflict')
        viewer = self.pair('viewer', name='viewer')['client_token']
        self.call('GET', '/checkpoints/%s/files' % cp['checkpoint_id'], token=viewer)  # 看文件清单与文本预览不留下“已下载”
        self.assertEqual(self.current(job)['downloads'], [])
        download = self.call('POST', '/jobs/%s/downloads' % job['job_id'], {'request_id': uid(), 'checkpoint_id': cp['checkpoint_id'],
                                                                            'files': 1, 'bytes': 6}, token=viewer, status=201)
        self.assertEqual((download['by']['name'], download['files'], download['manifest_sha256']), ('viewer', 1, cp['manifest_sha256']))
        view = self.current(job)
        self.assertEqual((len(view['downloads']), view['delivery_version'], view['integration_status']), (1, 0, 'none'))  # 已下载 ≠ 已发布 ≠ 已应用

        attempt = view['attempts'][0]
        self.mutate(job, 'publish', attempt_id=attempt['attempt_id'], epoch=attempt['epoch'], checkpoint_id=cp['checkpoint_id'], expected_base_version=0)
        self.assertEqual((self.current(job)['delivery_version'], self.current(job)['integration_status']), (1, 'none'))  # 已发布 ≠ 已应用

        # 开始应用只是登记意图；中断后停在 started，不算完成。
        application = self.start(job, cp)
        self.assertEqual((application['status'], application['base_manifest_sha256'], application['manifest_sha256']),
                         ('started', job['inputs_manifest_sha256'], cp['manifest_sha256']))
        self.assertEqual((self.current(job)['integration_status'], self.current(job)['applications'][0]['status']), ('none', 'started'))
        applied = self.finish(application, 'applied')
        self.assertEqual((applied['status'], self.current(job)['integration_status']), ('applied', 'integrated'))
        project = self.call('GET', '/projects/' + self.project_record['project_id'])
        self.assertEqual((project['applied']['application_id'], project['applied']['job_id']), (application['application_id'], job['job_id']))
        self.assertEqual(self.finish(application, 'failed', status=409)['error']['code'], 'conflict')  # 结果已定，不能改写

    def test_partial_unknown_and_failed_applications_are_never_recorded_as_integrated(self):
        for outcome, expected in (('partial', 'conflict'), ('conflict', 'conflict'), ('failed', 'none'), ('unknown', 'none')):
            with self.subTest(outcome=outcome):
                job, cp = self.finished()
                application = self.start(job, cp)
                detail = {'changed': ['a'], 'conflicts': ['out.txt'], 'pending': ['b'], 'backup': 'kept'}
                finished = self.finish(application, outcome, detail=detail)
                view = self.current(job)
                self.assertEqual((finished['status'], finished['detail'], view['integration_status']), (outcome, detail, expected))
                self.assertIsNone(self.call('GET', '/projects/' + self.project_record['project_id'])['applied'])
        job, cp = self.finished()
        self.assertEqual(self.finish(self.start(job, cp), 'done', status=400)['error']['code'], 'invalid')

    def test_only_the_starting_client_reports_and_other_projects_or_jobs_are_refused(self):
        job, cp = self.finished()
        one, two = self.pair('operator', name='one')['client_token'], self.pair('operator', name='two')['client_token']
        application = self.start(job, cp, token=one)
        self.assertEqual(self.finish(application, 'applied', token=two, status=403)['error']['code'], 'forbidden')
        self.assertEqual(self.current(job)['integration_status'], 'none')
        self.finish(application, 'applied', token=one)
        loose = self.job()  # 不属于这个项目的任务
        self.assertEqual(self.start(loose, cp, status=409)['error']['code'], 'conflict')
        self.assertEqual(self.start(job, cp, application_id=application['application_id'], status=409)['error']['code'], 'conflict')
        self.assertEqual(len(self.call('GET', '/projects/%s/applications' % self.project_record['project_id'])['applications']), 1)


if __name__ == '__main__':
    unittest.main()
