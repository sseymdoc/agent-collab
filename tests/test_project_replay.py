"""项目操作的跨进程恢复：输入登记的应答丢失后重放得到同一份基线；应用结果的上报丢失后补报，不重写文件。

真实 HTTP controller + 子进程节点（确定性步骤，不调用模型）。丢失的步骤用子进程运行 CLI，进程之间只共享配置目录；
应答丢失由 loopback 转发代理制造：请求照常送到 controller，应答不回给客户端。
"""
import json
import os
from pathlib import Path
import subprocess
import unittest

import test_e2e_local as e2e
import test_project_e2e
from test_submit_receipts import LossyProxy

PY, ROOT = e2e.PY, e2e.ROOT


@unittest.skipUnless(e2e.sockets_ok(), 'loopback socket 不可用')
class ProjectReplay(test_project_e2e.ProjectE2E):
    test_local_folder_to_node_and_back_with_conflicts_kept = None  # 只借用夹具

    def run_cli(self, *argv):
        """另起一个进程运行 CLI（与 self.cli 共用配置目录）。"""
        env = {k: v for k, v in os.environ.items() if not k.startswith('AGENT_COLLAB_')}
        env.update(AGENT_COLLAB_HOME=str(self.tmp / 'cli-home'), AGENT_COLLAB_CREDENTIAL_STORE='file', PYTHONIOENCODING='utf-8')
        done = subprocess.run([PY, '-B', '-m', 'agent_collab.cli'] + list(argv), cwd=str(ROOT), env=env,
                              capture_output=True, text=True, encoding='utf-8', timeout=120)
        text = done.stdout.strip() or done.stderr.strip()
        try:
            return done.returncode, json.loads(text)
        except ValueError:
            return done.returncode, text

    def through(self, match):
        proxy = LossyProxy(self.port, match)
        self.addCleanup(proxy.close)
        self.cli('client', 'pair', '--url', 'http://127.0.0.1:%d' % proxy.port, '--code', self.code('owner'), '--name', 'laptop')
        return proxy

    def project(self, files):
        folder = self.tmp / 'my-project'
        for name, text in files.items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(text.encode('utf-8'))
        pid = self.cli('project', 'create', '--name', 'demo')['project_id']
        self.cli('project', 'bind', pid, str(folder))
        return pid, folder

    def test_lost_baseline_answer_is_replayed_to_the_same_baseline(self):
        proxy = self.through(b'POST /api/v1/baselines ')
        pid, folder = self.project({'a.txt': 'one\n'})

        # 登记已被 controller 受理，但应答没有回来：进程以连接错误结束，给出恢复命令，回执留在本机。
        proxy.drop = True
        code, lost = self.run_cli('project', 'push', pid)
        proxy.drop = False
        self.assertEqual((code, proxy.dropped, lost['error']['code']), (3, 1, 'network'), lost)
        request_id = lost['request_id']
        self.assertEqual(lost['resume'], 'agent-collab project push %s --resume %s' % (pid, request_id))
        registered = self.api('GET', '/baselines?project_id=' + pid)['baselines']
        self.assertEqual(len(registered), 1)
        pending = self.run_cli('submit', '--pending')[1]['pending']
        self.assertEqual([(r['request_id'], r['kind']) for r in pending], [(request_id, 'baseline')])

        # 期间项目改了名、目录里的文件也变了：恢复不重新扫描、不重建名称，原样重放，得到同一份基线。
        record = self.api('GET', '/projects/' + pid)
        self.cli('project', 'set', pid, '--name', 'renamed')
        (folder / 'a.txt').write_bytes(b'changed after the lost answer\n')
        code, resumed = self.run_cli('project', 'push', pid, '--resume', request_id)
        self.assertEqual((code, resumed['baseline_id']), (0, registered[0]['baseline_id']), resumed)
        after = self.api('GET', '/baselines?project_id=' + pid)['baselines']
        self.assertEqual((len(after), after[0]['name']), (1, registered[0]['name']))
        self.assertTrue(registered[0]['name'].startswith(record['name'] + ' @ '))
        self.assertEqual(self.run_cli('submit', '--pending')[1]['pending'], [])
        self.assertEqual(self.run_cli('project', 'push', pid, '--resume', request_id)[0], 2)  # 回执已清除

        # 用户有意再推一次：新的扫描、新的登记（目录内容已变，自然是新的基线）。
        code, again = self.run_cli('project', 'push', pid)
        self.assertEqual((code, again['reused']), (0, False), again)
        self.assertNotEqual(again['request_id'], request_id)
        self.assertEqual(len(self.api('GET', '/baselines?project_id=' + pid)['baselines']), 2)

    def test_lost_application_report_is_sent_again_without_rewriting_files(self):
        self.agent('n1', 'h1', 'd1')
        proxy = self.through(b'POST /api/v1/projects/')
        pid, folder = self.project({'src/app.txt': 'version 1\n', 'README.md': 'readme\n'})
        self.cli('project', 'push', pid)
        job_id = self.run_job(pid, [{'write': {'path': 'src/app.txt', 'content': 'version 2\n'}},
                                    {'write': {'path': 'out/new.txt', 'content': 'new\n'}}], '上报丢失')

        # 登记应用意图能通过（请求行是 POST /projects/<id>/applications），结果上报的应答丢失：只丢第二个匹配的请求。
        proxy.match = b'POST /api/v1/projects/%s/applications/' % pid.encode()
        proxy.drop = True
        code, done = self.run_cli('project', 'apply', job_id)
        proxy.drop = False
        self.assertEqual((code, done['status'], done['reported'], proxy.dropped), (0, 'applied', None, 1), done)
        application_id = done['application_id']
        self.assertEqual(done['report_pending'], 'agent-collab project recover %s --report' % application_id)
        written = {name: (folder / name).stat().st_mtime_ns for name in ('src/app.txt', 'out/new.txt')}
        self.assertEqual(((folder / 'src/app.txt').read_text(encoding='utf-8'), (folder / 'out/new.txt').read_text(encoding='utf-8')),
                         ('version 2\n', 'new\n'))

        # controller 其实已经记下了（请求送到了，只是应答丢了）；本机回执还在，等待确认。
        job = self.api('GET', '/jobs/' + job_id)
        self.assertEqual([a['status'] for a in job['applications']], ['applied'])
        listed = self.run_cli('project', 'recover')[1]
        self.assertEqual(([u['application_id'] for u in listed['unreported']], listed['unfinished']), ([application_id], []))

        # 另一个进程补报：同一个 request_id 重放，controller 返回第一次的结果，没有第二条记录；文件没有重写。
        code, sent = self.run_cli('project', 'recover', application_id, '--report')
        self.assertEqual((code, sent['reported']), (0, 'applied'), sent)
        job = self.api('GET', '/jobs/' + job_id)
        self.assertEqual(([a['status'] for a in job['applications']], job['integration_status']), (['applied'], 'integrated'))
        self.assertEqual({name: (folder / name).stat().st_mtime_ns for name in written}, written)
        self.assertEqual(self.run_cli('project', 'recover')[1]['unreported'], [])
        done_log = self.tmp / 'cli-home' / 'projects' / 'applies' / application_id / 'done.log'
        entries = [json.loads(line) for line in done_log.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len([e for e in entries if not e.get('started')]), 2)  # 每个文件只写了一次

    def test_report_that_never_arrived_is_recorded_on_the_retry(self):
        """controller 根本没收到结果（不可达）：记录停在 started，补报时才第一次记下。"""
        self.agent('n1', 'h1', 'd1')
        proxy = self.through(b'POST /api/v1/projects/')
        pid, folder = self.project({'a.txt': 'a\n'})
        self.cli('project', 'push', pid)
        job_id = self.run_job(pid, [{'write': {'path': 'a.txt', 'content': 'b\n'}}], '不可达')
        proxy.match, proxy.forward, proxy.drop = b'POST /api/v1/projects/%s/applications/' % pid.encode(), False, True
        code, done = self.run_cli('project', 'apply', job_id)
        proxy.drop = False
        self.assertEqual((code, done['status'], done['reported'], (folder / 'a.txt').read_text(encoding='utf-8')),
                         (0, 'applied', None, 'b\n'), done)
        self.assertEqual([a['status'] for a in self.api('GET', '/jobs/' + job_id)['applications']], ['started'])  # 不显示为已应用
        code, sent = self.run_cli('project', 'recover', done['application_id'], '--report')
        self.assertEqual((code, sent['reported']), (0, 'applied'), sent)
        self.assertEqual(self.api('GET', '/jobs/' + job_id)['integration_status'], 'integrated')


if __name__ == '__main__':
    unittest.main()
