"""CLI 统一入口的端到端：真实 HTTP controller + 子进程节点（确定性步骤，不调用模型）。

证明 CLI 用的是与其他客户端相同的 API、授权和状态：CLI 提交的任务另一份客户端凭据看得到，
另一客户端的操作 CLI 观察得到；凭据可单独撤销；应答不是来自配对的 controller 时停止。
"""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
import uuid

from agent_collab import cli
import test_e2e_local as e2e

PY, ROOT = e2e.PY, e2e.ROOT


@unittest.skipUnless(e2e.sockets_ok(), 'loopback socket 不可用')
class CliE2E(unittest.TestCase):
    setUp, cleanup, spawn, raw, api, wait, agent, start_agent = (
        e2e.E2E.setUp, e2e.E2E.cleanup, e2e.E2E.spawn, e2e.E2E.raw, e2e.E2E.api, e2e.E2E.wait, e2e.E2E.agent, e2e.E2E.start_agent)

    def cli(self, *argv, expect=0, text=False):
        env = {k: v for k, v in os.environ.items() if not k.startswith('AGENT_COLLAB_')}
        env['AGENT_COLLAB_HOME'] = str(self.tmp / 'cli-home')
        env['AGENT_COLLAB_CREDENTIAL_STORE'] = 'file'  # 测试不往运行者的系统凭据存储里写；系统后端另有用例
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env, clear=True), redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = cli.main(list(argv))
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, expect, (argv, stdout.getvalue(), stderr.getvalue()))
        if text:
            return stdout.getvalue()
        return json.loads(stdout.getvalue()) if stdout.getvalue().strip() else json.loads(stderr.getvalue() or 'null')

    def code(self, role):
        result = subprocess.run([PY, '-m', 'agent_collab.controller', 'client-code', '--data-dir', str(self.data), '--role', role],
                                cwd=str(ROOT), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)['code']

    def sleeper(self, title):
        return {'title': title, 'contract': {'goal': title, 'harness': 'stub', 'harness_params': {'steps': [{'sleep': 60}]},
                                             'inputs_manifest': {'version': 1, 'base': None, 'files': [], 'deleted': []}}}

    def test_cli_and_another_client_share_tasks_state_settings_and_revocation(self):
        self.agent('n1', 'h1', 'd1')
        paired = self.cli('client', 'pair', '--url', self.url, '--code', self.code('owner'), '--name', 'automation')
        saved = json.loads(Path(paired['credential_file']).read_text(encoding='utf-8'))
        self.assertNotIn(saved['token'], json.dumps(paired))  # 凭据值不出现在输出里
        if os.name == 'posix':
            self.assertEqual(os.stat(paired['credential_file']).st_mode & 0o077, 0)
        self.assertEqual(self.cli('client', 'status')['identity']['name'], 'automation')
        desk = self.raw('POST', '/clients/pair', {'code': self.code('owner'), 'name': 'desk', 'kind': 'desktop'}, auth=False)[1]['client_token']

        # 设置：CLI 写入，另一客户端读到同一份；另一客户端再改，CLI 读到新版本。
        settings = self.cli('settings', 'set', '--codex-model', 'model-a', '--codex-effort', 'high', '--allow-any')
        self.assertEqual(self.raw('GET', '/settings/models', token=desk)[1]['settings'], settings)
        status, changed = self.raw('PUT', '/settings/models', {'request_id': str(uuid.uuid4()), 'expected_revision': settings['revision'],
                                                               'claude': {'default': {'model': 'c-model', 'effort': 'max'}}}, token=desk)
        self.assertEqual(status, 200)
        self.assertEqual(self.cli('settings', 'get')['settings'], changed)

        # 目标 + 模板 + 输入目录 + 检查 → 契约 → 节点执行 → 检查 → 发布；CLI 等到停稳。
        project = self.tmp / 'project'
        project.mkdir()
        (project / 'value.txt').write_text('41\n', encoding='utf-8')
        checks = ['has-input=' + json.dumps([PY, '-c', "import sys; sys.exit(0 if open('value.txt', encoding='utf-8').read().strip() == '41' else 1)"]),
                  'wrote-result=' + json.dumps([PY, '-c', "open('agent-collab-diagnostic.txt', encoding='utf-8').read()"])]
        draft = self.cli('submit', '--template', 'diagnostic', '--goal', '链路自检', '--input-dir', str(project), '--dry-run')
        self.assertEqual((draft['preview']['harness'], [f['path'] for f in draft['preview']['inputs_manifest']['files']]), ('stub', ['value.txt']))
        self.assertEqual(self.cli('job', 'list')['jobs'], [])
        result = self.cli('submit', '--template', 'diagnostic', '--goal', '链路自检', '--input-dir', str(project),
                          '--check', checks[0], '--check', checks[1], '--wait', '--timeout', '120')
        self.assertEqual((result['status'], result['outcome'], result['node_id'], result['delivery_version']), ('completed', 'done', 'n1', 1))
        self.assertEqual(result['checks'], {'passed': True, 'results': [dict(r, duration_s=result['checks']['results'][i]['duration_s'])
                                                                         for i, r in enumerate(({'name': 'has-input', 'exit_code': 0, 'timed_out': False},
                                                                                                {'name': 'wrote-result', 'exit_code': 0, 'timed_out': False}))]})
        job_id = result['job_id']
        seen = self.raw('GET', '/jobs/' + job_id, token=desk)[1]  # 另一客户端看到同一个任务及其来源
        self.assertEqual((seen['status'], seen['created_by']['name'], seen['delivery_version']), ('completed', 'automation', 1))
        self.assertEqual(self.cli('job', 'results', job_id)['outcome'], 'done')
        self.assertEqual([j['job_id'] for j in self.cli('job', 'list')['jobs']], [job_id])
        target = self.tmp / 'download'
        artifacts = self.cli('job', 'artifacts', job_id, '--download', str(target))
        self.assertEqual((artifacts['published'], sorted(f['path'] for f in artifacts['files'])), (True, ['agent-collab-diagnostic.txt', 'value.txt']))
        self.assertEqual(((target / 'agent-collab-diagnostic.txt').read_text(encoding='utf-8'), (target / 'value.txt').read_text(encoding='utf-8')),
                         ('链路自检\n', '41\n'))
        lines = [json.loads(line) for line in self.cli('job', 'follow', job_id, '--timeout', '30', text=True).splitlines()]
        self.assertEqual((lines[-1]['summary']['outcome'], lines[0]['type']), ('done', 'job.created'))
        self.assertIn('job.published', [line.get('type') for line in lines[:-1]])

        # 另一客户端提交并取消：CLI 观察到同一状态，取消请求与确认是两个时间。
        status, slow = self.raw('POST', '/jobs', dict(self.sleeper('desk job'), request_id=str(uuid.uuid4())), token=desk)
        self.assertEqual(status, 201)
        self.wait(lambda: self.cli('job', 'results', slow['job_id'])['status'] == 'running', 60, '任务未开始执行')
        version = self.raw('GET', '/jobs/' + slow['job_id'], token=desk)[1]['version']
        self.raw('POST', '/jobs/%s/cancel' % slow['job_id'], {'request_id': str(uuid.uuid4()), 'expected_version': version}, token=desk)
        waited = self.cli('job', 'wait', slow['job_id'], '--timeout', '60', expect=5)
        self.assertEqual((waited['status'], waited['outcome']), ('cancelled', 'needs_attention'))
        self.assertTrue(waited['cancel']['requested_at'] and waited['cancel']['confirmed_at'])

        # CLI 提交任务文件并取消：另一客户端看到取消。
        job_file = self.tmp / 'job.json'
        job_file.write_text(json.dumps(self.sleeper('cli job')), encoding='utf-8')
        submitted = self.cli('submit', str(job_file))
        self.assertEqual(submitted['http_status'], 201)
        mine = submitted['body']['job_id']
        self.wait(lambda: self.raw('GET', '/jobs/' + mine, token=desk)[1]['status'] == 'running', 60, '任务未开始执行')
        self.assertEqual(self.cli('job', 'cancel', mine)['status'], 'cancel_requested')
        self.wait(lambda: self.raw('GET', '/jobs/' + mine, token=desk)[1]['status'] == 'cancelled', 60, '取消未确认')

        # 作为交互协调者：取得协调租约后，验收必须带协调令牌；不带令牌的另一客户端被拒绝。
        package = seen['work_package_id']
        lease = self.cli('api', 'POST', '/packages/%s/coordinator' % package, '--data', json.dumps(
            {'holder_kind': 'interactive', 'holder_ref': 'automation', 'ttl_s': 120, 'takeover': False, 'reason': ''}))['body']
        checkpoint = self.cli('job', 'results', job_id)['final_checkpoint_id']
        assess = {'expected_version': self.raw('GET', '/jobs/' + job_id, token=desk)[1]['version'], 'assessment_id': str(uuid.uuid4()),
                  'checkpoint_id': checkpoint, 'status': 'passed', 'method': '两项检查的执行证据', 'result': '全部通过', 'reviewer': 'automation'}
        blocked = self.raw('POST', '/jobs/%s/assess' % job_id, dict(assess, request_id=str(uuid.uuid4())), token=desk)
        self.assertEqual((blocked[0], blocked[1]['error']['details']), (409, {'reason': 'coordinator_lease'}))
        accepted = self.cli('api', 'POST', '/jobs/%s/assess' % job_id, '--data', json.dumps(assess), '--coordinator-token', lease['coordinator_token'])
        self.assertEqual((accepted['http_status'], accepted['body']['status'], accepted['body']['evidence']['checks']), (200, 'passed', ['has-input', 'wrote-result']))
        self.assertEqual(self.cli('job', 'results', job_id)['acceptance']['status'], 'passed')

        # 只读客户端提交被拒绝；撤销后 CLI 立即失效，节点与另一客户端不受影响。
        self.cli('client', 'pair', '--url', self.url, '--code', self.code('viewer'), '--profile', 'readonly')
        refused = self.cli('submit', '--template', 'diagnostic', '--goal', 'x', '--profile', 'readonly', expect=2)
        self.assertEqual(refused['body']['error']['details'], {'required_scope': 'tasks'})
        clients = self.api('GET', '/clients')['clients']
        record = next(c for c in clients if c['name'] == 'automation')
        self.api('POST', '/clients/%s/revoke' % record['client_id'], {'request_id': str(uuid.uuid4()), 'expected_version': record['version']})
        self.cli('job', 'list', expect=3)
        self.assertEqual(self.raw('GET', '/overview', token=desk)[0], 200)
        self.wait(lambda: self.api('GET', '/nodes')['nodes'][0]['status'] == 'online', 20, '节点受撤销影响')

        # 凭据绑定 controller 身份：应答来自别的 controller 时停止，不继续请求。
        path = self.cli('client', 'status', '--profile', 'readonly') and (self.tmp / 'cli-home' / 'clients' / 'readonly.json')
        tampered = json.loads(path.read_text(encoding='utf-8'))
        tampered['controller_id'] = str(uuid.uuid4())
        path.write_text(json.dumps(tampered), encoding='utf-8')
        self.assertEqual(self.cli('job', 'list', '--profile', 'readonly', expect=3)['error']['code'], 'controller_mismatch')
        self.assertEqual(self.cli('client', 'forget', '--profile', 'readonly'), {'profile': 'readonly', 'forgotten': True, 'revoked': None})
        self.assertFalse(path.exists())
