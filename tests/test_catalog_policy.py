"""模型目录的派单策略（protocol §12.5）：只自动选择目录已核实含该组合的节点；
目录未核实（未知或过期）时缺省等待，显式开启后才作为“未核实能力尝试”；刷新失败时沿用的旧目录带来源与时间并会过期；
探测与执行使用同一个 provider。全部是确定性用例，不调用模型。
"""
import argparse
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest

from agent_collab import domain as d, protocol as p
from agent_collab.adapters import codex
from agent_collab.node_agent import Agent, pair
from fake_controller import FakeController, no_auth
from test_clients_models import A, B, ClientCase, Tick, caps

ROOT = Path(__file__).resolve().parents[1]
ANY = {'default': {'model': A, 'effort': 'high'}, 'allowed': None}


class SchedulingTests(ClientCase):
    def rows(self, job):
        view = self.call('GET', '/jobs/' + job['job_id'] + '/explain')
        return view, {row['node_id']: row for row in view['nodes']}

    def test_known_supporting_node_is_chosen_over_an_unverified_one_and_unverified_alone_waits(self):
        self.put(expected_revision=0, codex=ANY)
        # 目录未知的节点空闲槽位更多、分数更高；目录已确认的节点仍被选中。
        unknown = self.node('unknown', capabilities=caps({}, status='unknown'), resources={'slots': 8, 'host_slots': 8, 'mem_mb': None})
        known = self.node('known', capabilities=caps({A: ['high']}), resources={'slots': 1, 'host_slots': 1, 'mem_mb': None})
        first = self.codex_job()
        view, rows = self.rows(first)
        self.assertEqual(self.current(first)['status_reason'], 'assigned:known')
        self.assertGreater(rows['unknown']['score'], rows['known']['score'])
        self.assertEqual((rows['unknown']['reasons'], rows['unknown']['model_catalog'], view['policy']),
                         (['model_catalog_unverified:unknown'], 'unknown', {'unverified_nodes': 'wait'}))
        self.assertEqual(known.claim()['job_id'], first['job_id'])

        # 已确认的节点没有空位了：第二个任务等待，不落到目录未知的节点上。
        second = self.codex_job()
        waiting = self.current(second)
        self.assertEqual(waiting['status'], 'waiting_capability')
        self.assertIn('model_catalog_unverified:unknown', waiting['status_reason'])
        self.assertIsNone(unknown.claim(status=204))

        # 显式开启“未核实能力尝试”：只影响之后受理的任务，策略写在任务上并出现在解释里；已受理的任务不变。
        settings = self.models()['settings']
        self.assertEqual(settings['codex']['unverified_nodes'], 'wait')
        self.put(expected_revision=settings['revision'], codex=dict(ANY, unverified_nodes='allow'))
        third = self.codex_job()
        self.assertEqual(third['model_policy']['unverified_nodes'], 'allow')
        view, rows = self.rows(self.current(third))
        self.assertEqual((view['policy'], view['selected_node_id'], self.current(third)['status_reason'], rows['unknown']['unverified_attempt']),
                         ({'unverified_nodes': 'allow'}, 'unknown', 'assigned:unknown', True))
        self.assertEqual(unknown.claim()['job_id'], third['job_id'])
        self.assertEqual((self.current(second)['status'], self.current(second)['model_policy']['unverified_nodes']), ('waiting_capability', 'wait'))

    def test_with_attempts_allowed_a_verified_node_is_still_preferred(self):
        self.put(expected_revision=0, codex=dict(ANY, unverified_nodes='allow'))
        self.node('unknown', capabilities=caps({}, status='unknown'), resources={'slots': 8, 'host_slots': 8, 'mem_mb': None})
        self.node('known', capabilities=caps({A: ['high']}), resources={'slots': 1, 'host_slots': 1, 'mem_mb': None})
        job = self.codex_job()
        self.assertEqual(self.current(job)['status_reason'], 'assigned:known')
        # 已核实的目录明确不含该组合的节点，开启尝试后也不会被选中：尝试只针对“未核实”，不绕过已知的不支持。
        other = self.codex_job(model=B, effort='low')
        _, rows = self.rows(other)
        self.assertIn('model_unsupported:' + B, rows['known']['reasons'])
        self.assertEqual(self.current(other)['status_reason'], 'assigned:unknown')  # 目录未核实的节点才是这次尝试的对象

    def test_older_client_saving_settings_does_not_reset_the_explicit_policy(self):
        self.put(expected_revision=0, codex=dict(ANY, unverified_nodes='allow'))
        saved = self.put(expected_revision=1, codex={'default': {'model': B, 'effort': 'low'}, 'allowed': None})  # 不认识这一项的客户端
        self.assertEqual((saved['codex']['default']['model'], saved['codex']['unverified_nodes']), (B, 'allow'))
        self.put(expected_revision=2, codex=dict(ANY, unverified_nodes='sometimes'), status=400)

    def test_catalog_expires_and_a_fresh_one_restores_the_node(self):
        self.put(expected_revision=0, codex=ANY)
        node = self.node('n', capabilities=caps({A: ['high']}))
        max_age = self.app.config['catalog_max_age_s']
        self.clock.advance(max_age + 1)
        node.heartbeat()  # 节点在线，只是目录没有再刷新
        job = self.codex_job()
        _, rows = self.rows(job)
        self.assertEqual((rows['n']['reasons'], rows['n']['model_catalog']), (['model_catalog_unverified:expired'], 'expired'))
        view = self.models()
        self.assertEqual((view['selectable'], view['catalog']['nodes']['n']['codex']['state'], view['catalog']['nodes']['n']['codex']['status'],
                          view['catalog']['max_age_s']), ({'codex': [], 'unknown_nodes': ['n']}, 'expired', 'ok', max_age))
        draft = self.call('POST', '/contracts/draft', {'template': 'codex-change', 'goal': 'g'})
        self.assertEqual([w['code'] for w in draft['warnings']], ['empty_inputs', 'no_verified_node_supports_model'])
        # 节点重新探测成功并注册：取得时间更新，任务被派出。
        fresh = caps({A: ['high']})
        fresh['models']['codex']['fetched_at'] = d.timestamp(self.clock())
        self.call('POST', '/nodes/register', dict(node.record, capabilities=fresh), token=node.token)
        self.assertEqual(node.claim()['job_id'], job['job_id'])

    def test_stale_catalog_keeps_its_original_time_and_identity_and_expires_on_schedule(self):
        self.put(expected_revision=0, codex=ANY)
        stale = caps({A: ['high']}, status='stale')
        stale['models']['codex'].update(reason='probe_failed', provider='openai')
        node = self.node('n', capabilities=stale)
        job = self.codex_job()
        _, rows = self.rows(job)
        self.assertEqual((rows['n']['eligible'], rows['n']['model_catalog'], rows['n']['unverified_attempt']), (True, 'stale', False))
        entry = self.models()['catalog']['nodes']['n']['codex']
        self.assertEqual((entry['status'], entry['state'], entry['reason'], entry['provider'], entry['cli_version'], entry['fetched_at']),
                         ('stale', 'stale', 'probe_failed', 'openai', '1', d.timestamp(1804800000.0)))
        self.assertEqual(node.claim()['job_id'], job['job_id'])
        self.clock.advance(self.app.config['catalog_max_age_s'] + 1)
        node.heartbeat([])
        later = self.codex_job()
        self.assertEqual(self.rows(later)[1]['n']['model_catalog'], 'expired')


class AgentStaleCatalogTests(unittest.TestCase):
    """节点侧：刷新失败时沿用上次成功的目录，前提是 CLI 版本与 provider 都没变。"""
    def setUp(self):
        (ROOT / '.dev/tmp').mkdir(parents=True, exist_ok=True)
        self.temp = Path(tempfile.mkdtemp(dir=str(ROOT / '.dev/tmp')))
        self.addCleanup(shutil.rmtree, str(self.temp))
        self.fake, self.clock = FakeController(), Tick()
        self.fake.features = list(p.FEATURES)
        self.data = self.temp / 'agent'
        pair(argparse.Namespace(controller='https://controller.invalid', code='one-time', node_id='node-a', data_dir=str(self.data),
                                ca_file=None, slots=1, host_slots=1, physical_host_id='host-a', failure_domain='domain-a', label=[]), self.fake)
        self.answer = self.good()

    @staticmethod
    def good(models=None):
        return {'status': 'ok', 'source': 'app-server:model/list', 'fetched_at': None, 'cli_version': None, 'reason': None, 'provider': 'openai',
                'models': models or [{'id': 'm-one', 'efforts': ['low'], 'default_effort': None}]}

    @staticmethod
    def failed(reason='probe_failed', provider=None):
        return {'status': 'unknown', 'source': 'app-server:model/list', 'fetched_at': None, 'cli_version': None, 'reason': reason,
                'provider': provider, 'models': []}

    def start(self, version='9.9'):
        capabilities = {'os': 'linux', 'arch': 'x86_64', 'tools': {}, 'gpu': [], 'harness': {'stub': '1', 'codex': version}, 'labels': []}
        agent = Agent(self.data, transport=self.fake, clock=self.clock, capabilities=capabilities, auth_runner=no_auth,
                      catalog_probe=lambda argv, env: dict(self.answer))
        self.addCleanup(agent.close)
        return agent

    def reported(self, agent, wanted):
        """推进到下一次探测并注册，返回 controller 收到的 codex 目录。"""
        agent.next_catalog = 0
        for _ in range(300):
            self.clock.now += 1
            agent.cycle()
            entry = (self.fake.registrations[-1]['capabilities'].get('models') or {}).get('codex')
            if entry and wanted(entry):
                return entry
            time.sleep(.01)
        self.fail('catalog not reported: %r' % (self.fake.registrations[-1]['capabilities'].get('models'),))

    def test_failed_refresh_reuses_the_last_success_with_its_time_until_identity_changes(self):
        agent = self.start()
        first = self.reported(agent, lambda e: e['status'] == 'ok')
        self.assertEqual((first['provider'], first['cli_version']), ('openai', '9.9'))
        d.validate_catalogs({'codex': first})

        self.answer = self.failed()
        stale = self.reported(agent, lambda e: e['status'] == 'stale')
        self.assertEqual((stale['models'], stale['fetched_at'], stale['reason'], stale['provider'], stale['cli_version']),
                         (first['models'], first['fetched_at'], 'probe_failed', 'openai', '9.9'))
        d.validate_catalogs({'codex': stale})

        # 探测读回的 provider 不是执行用的那个：旧目录不再代表执行能力。
        self.answer = self.failed('provider_mismatch', provider='corp-gateway')
        self.assertEqual(self.reported(agent, lambda e: e['status'] == 'unknown')['models'], [])

        # 重启后探测仍失败：上次成功的目录从磁盘取回，时间不变。
        self.answer = self.failed()
        agent.close()
        again = self.start()
        self.assertEqual(self.reported(again, lambda e: e['status'] == 'stale')['fetched_at'], first['fetched_at'])
        # CLI 升级了而探测失败：按旧版本取得的目录不沿用。
        again.close()
        upgraded = self.start(version='10.0')
        self.assertEqual(self.reported(upgraded, lambda e: e['cli_version'] == '10.0')['status'], 'unknown')

    def test_controller_without_provenance_support_gets_the_previous_shape(self):
        self.fake.features = [f for f in p.FEATURES if f != 'catalog_provenance']
        agent = self.start()
        first = self.reported(agent, lambda e: e['status'] == 'ok')
        self.assertNotIn('provider', first)
        self.answer = self.failed()
        degraded = self.reported(agent, lambda e: e['status'] == 'unknown')  # 没有有效期判断的 controller 不收沿用的旧目录
        self.assertEqual((degraded['models'], set(degraded)), ([], {'status', 'source', 'fetched_at', 'cli_version', 'reason', 'models'}))


@unittest.skipUnless(shutil.which('codex'), '本机没有 codex CLI')
class RealCliProviderTests(unittest.TestCase):
    """用真实的 codex CLI 与临时 CODEX_HOME 夹具核对探测的生效 provider。不读写用户的全局配置，夹具里没有凭据，不调用模型。"""
    def test_probe_ignores_the_provider_configured_in_the_user_config(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(home), True)
        (home / 'config.toml').write_text('model_provider = "fixture"\nmodel = "fixture-model"\n\n[model_providers.fixture]\n'
                                          'name = "Fixture"\nbase_url = "http://127.0.0.1:9/v1"\nwire_api = "responses"\n', encoding='utf-8')
        env = dict(os.environ, CODEX_HOME=str(home))
        for key in ('CODEX_THREAD_ID', 'CODEX_INTERNAL_ORIGINATOR_OVERRIDE'):
            env.pop(key, None)
        result = codex.model_catalog([shutil.which('codex')], env=env, timeout=60)
        if result['reason'] in ('spawn_failed', 'probe_failed'):
            self.skipTest('codex app-server 在这台机器上无法启动：%s' % result['reason'])
        # 用户配置把 provider 设成了 fixture；探测读回的必须是执行用的 provider，否则这份目录不被采用。
        self.assertEqual(result['provider'], codex.PROVIDER, result)
        self.assertIn(result['status'], ('ok', 'unknown'))


if __name__ == '__main__':
    unittest.main()
