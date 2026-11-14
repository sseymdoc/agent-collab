"""心跳不再为每次刷新最近联系时间写一条节点快照事件；真实变化照常立即写。总览的待办计数按工作包最新修订判定。"""
import copy
import unittest
from unittest import mock

from agent_collab import domain as d, protocol as p
from agent_collab.controller import NODE_EVENT_INTERVAL_S, App
from helpers import ControllerCase


class HeartbeatEventTests(ControllerCase):
    def node_events(self):
        return [e for e in self.app.store.events() if e['type'] == 'node.updated' and e['node_id'] == 'a']

    def test_repeated_heartbeats_refresh_liveness_without_snapshot_events(self):
        node = self.node('a')
        self.clock.advance(5)
        node.heartbeat()                       # 注册之后第一次刷新最近联系时间：照常写一条
        before, version = len(self.node_events()), self.app.store.get('node', 'a')['version']
        start = self.clock()
        while self.clock() - start < NODE_EVENT_INTERVAL_S - 5:   # 间隔之内、每 5 秒一次的重复心跳
            self.clock.advance(5)
            node.heartbeat()
            record = self.app.store.get('node', 'a')
            self.assertEqual(record['last_seen_at'], d.timestamp(self.clock()))
        self.assertEqual(len(self.node_events()), before)
        self.assertEqual(self.app.store.get('node', 'a')['version'], version)
        self.assertEqual(self.call('GET', '/nodes')['nodes'][0]['status'], 'online')
        self.clock.advance(5)                  # 间隔到了：补写一条，界面上的“最近联系”随之刷新
        node.heartbeat()
        self.assertEqual(len(self.node_events()), before + 1)
        self.assertEqual(self.node_events()[-1]['payload']['last_seen_at'], d.timestamp(self.clock()))
        self.assertEqual(self.node_events()[-1]['payload']['status'], 'online')

    def test_real_changes_are_written_immediately(self):
        node = self.node('a')
        node.heartbeat()
        count = len(self.node_events())
        self.clock.advance(5)
        node.heartbeat(health={'codex': 'expired'})   # 认证变化不等间隔
        self.assertEqual(len(self.node_events()), count + 1)
        self.assertEqual(self.node_events()[-1]['payload']['auth_health']['codex'], 'expired')
        self.clock.advance(40)                         # 心跳中断：巡检把它判为疑似失联，立即写
        self.app.tick()
        self.assertEqual(self.node_events()[-1]['payload']['status'], 'suspect')
        self.clock.advance(1)
        node.heartbeat()                               # 恢复在线也立即写
        self.assertEqual(self.node_events()[-1]['payload']['status'], 'online')
        self.assertEqual(len(self.node_events()), count + 3)

    def test_leases_and_offline_detection_still_follow_every_heartbeat(self):
        node = self.node('a')
        job = self.job()
        assignment = node.claim()
        for _ in range(30):                            # 150 秒：远超租约（90 秒），全靠不写事件的心跳续租
            self.clock.advance(5)
            self.assertEqual(node.heartbeat()['renewed'], [assignment['attempt_id']])
        self.app.tick()
        attempt = next(a for a in self.current(job)['attempts'] if a['attempt_id'] == assignment['attempt_id'])
        self.assertIn(attempt['status'], ('leased', 'running'))
        self.assertEqual(self.call('GET', '/nodes')['nodes'][0]['status'], 'online')


class RedundantNodeEventPruneTests(ControllerCase):
    def beat(self, node, count, **changes):
        for _ in range(count):
            self.clock.advance(5)
            node.heartbeat(**changes)

    def build_history(self):
        node = self.node('a')
        with mock.patch('agent_collab.controller.NODE_EVENT_INTERVAL_S', 0):
            self.beat(node, 6)                               # 修复之前：每次心跳一条只刷新联系时间的快照
            self.beat(node, 1, health={'codex': 'expired'})  # 真实变化：认证
            self.beat(node, 4)
            created = self.job()
            self.clock.advance(40)
            self.app.tick()                                  # 真实变化：疑似失联
            self.beat(node, 5)                               # 恢复在线（真实变化），之后又是只刷新联系时间
        return node, created

    def test_only_liveness_snapshots_go_and_replay_floor_forces_a_snapshot(self):
        node, created = self.build_history()
        events = self.app.store.events(0, 100000)
        node_events = [e for e in events if e['type'] == 'node.updated']
        others = [e['seq'] for e in events if e['type'] != 'node.updated']
        keys = {row[0] for row in self.app.store.db.execute('SELECT event_id FROM event_keys')}
        receipts = self.app.store.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]
        record, job_record = self.app.store.get('node', 'a'), self.app.store.get('job', created['job_id'])
        self.app.close()
        self.app = None
        from agent_collab.store import Store
        store = Store(self.temp / 'data')
        try:
            result = store.prune_redundant_node_events(batch=2)
            after = store.events(0, 100000)
            self.assertEqual(store.get('node', 'a'), record)               # 记录不受裁剪影响
            self.assertEqual(store.get('job', created['job_id']), job_record)
        finally:
            store.close()
        kept = [e for e in after if e['type'] == 'node.updated']
        self.assertGreater(result['deleted'], 0)
        self.assertEqual(result['deleted'], len(node_events) - len(kept))
        self.assertEqual([e['seq'] for e in after if e['type'] != 'node.updated'], others)   # 其他事件一条不少
        self.assertEqual(kept[0]['seq'], node_events[0]['seq'])                               # 最早与最新各留一条
        self.assertEqual(kept[-1]['seq'], node_events[-1]['seq'])
        states = [(e['payload']['status'], e['payload']['auth_health'].get('codex')) for e in kept]
        self.assertIn(('online', 'expired'), states)
        self.assertIn(('suspect', 'expired'), states)
        for earlier, later in zip(kept, kept[1:-1]):                                           # 留下的相邻快照之间都有真实变化
            strip = lambda e: {k: v for k, v in e['payload'].items() if k not in ('last_seen_at', 'version')}
            self.assertNotEqual(strip(earlier), strip(later))
        deleted = sorted({e['seq'] for e in node_events} - {e['seq'] for e in kept})
        self.assertEqual(result['complete_after'], deleted[-1])
        self.app = App(self.temp / "data", clock=self.clock)
        self.assertLessEqual(keys, {row[0] for row in self.app.store.db.execute('SELECT event_id FROM event_keys')})  # 去重账本一条不少
        self.assertEqual(self.app.store.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], receipts)
        floor = result['complete_after'] + 1
        self.assertEqual(self.app.min_seq(), floor)
        self.assertEqual(self.call('GET', '/events?after=0')['min_seq'], floor)
        old = self.app.sse_batch(deleted[0] - 1)                  # 游标跨过了被裁剪的区间：要求重取快照
        self.assertTrue(old[0].startswith(b'event: snapshot_required'))
        self.assertIn(('"min_seq":%d' % floor).encode(), old[0])
        fresh = self.app.sse_batch(floor - 1)                     # 水位之后的游标照常增量续读
        self.assertFalse(fresh[0].startswith(b'event: snapshot_required'))
        self.assertTrue(all(int(chunk.split(b'\n')[0][4:]) > floor - 1 for chunk in fresh if chunk.startswith(b'id: ')))
        self.restart()                                            # 水位持久保存
        self.assertEqual(self.app.min_seq(), floor)
        node.heartbeat()                                          # 裁剪之后照常心跳，新事件序号继续递增
        self.assertGreater(self.app.store.events(0, 100000)[-1]['seq'], node_events[-1]['seq'])

    def test_prune_command_requires_a_stopped_controller(self):
        self.build_history()
        from agent_collab import controller as c
        with c.ControllerLock(self.temp / 'data'):                # 运行中的 controller（serve）持有这把锁
            with self.assertRaises(SystemExit), mock.patch('sys.stderr'):
                c.main(['prune-events', '--data-dir', str(self.temp / 'data'), '--redundant-node-updates'])


def job(job_id, status='completed', requires_acceptance=False, acceptance='unverified', delivery=0, mode='manual',
        root=None, created='2026-11-14T00:00:00.000Z', coordinates=None):
    contract = {'publish': {'mode': mode, 'requires_acceptance': requires_acceptance}}
    if coordinates:
        contract['coordinates'] = {'job_id': coordinates}
    return {'job_id': job_id, 'work_package_id': root or job_id, 'status': status, 'created_at': created,
            'acceptance': {'status': acceptance}, 'delivery_version': delivery, 'contract': contract}


class OutcomeTests(unittest.TestCase):
    def test_counts_use_the_current_revision_and_the_shared_outcome(self):
        jobs = [
            job('accepted-published', requires_acceptance=True, acceptance='passed', delivery=1),   # done
            job('accepted-unpublished', requires_acceptance=True, acceptance='passed'),              # ready_to_publish
            job('no-acceptance-published', delivery=1),                                              # done
            job('still-to-accept', requires_acceptance=True),                                        # awaiting_acceptance
            # 旧修订验收为返工、最新修订已验收通过：只按最新修订算，旧修订的 rework 不是当前待办
            job('rework-root', requires_acceptance=True, acceptance='rework', delivery=1, root='pkg'),
            job('repair', requires_acceptance=True, acceptance='passed', delivery=2, root='pkg', created='2026-11-14T01:00:00.000Z'),
            # 协调回合本身不计；它还在进行时，被协调的任务记为等协调
            job('coordinated', requires_acceptance=True),
            job('round', status='running', coordinates='coordinated'),
            job('finished-round', coordinates='accepted-published'),
            job('auto-unpublished', mode='auto'),                                                   # awaiting_publication
            job('cancelled', status='cancelled'),                                                    # needs_attention
            job('running', status='running'),
        ]
        legacy = job('legacy')
        del legacy['contract']['publish']
        counts = d.outcome_counts(jobs + [legacy])
        self.assertEqual(set(counts), set(d.OUTCOMES))
        self.assertEqual(counts, {'done': 3, 'ready_to_publish': 1, 'awaiting_acceptance': 1, 'awaiting_coordination': 1,
                                  'awaiting_publication': 1, 'needs_attention': 1, 'running': 1, 'unknown': 1})

    def test_outcome_matches_the_rules_clients_already_use(self):
        self.assertEqual(d.job_outcome(job('x', status='waiting_reconcile')), 'needs_attention')
        self.assertEqual(d.job_outcome(job('x', status='failed'), coordinating=True), 'awaiting_coordination')
        self.assertEqual(d.job_outcome(job('x', requires_acceptance=True, acceptance='blocked')), 'needs_attention')
        self.assertEqual(d.job_outcome(job('x', requires_acceptance=True, acceptance='passed', delivery=1)), 'done')


class OverviewOutcomeTests(ControllerCase):
    def test_overview_reports_outcomes_beside_the_unchanged_status_counts(self):
        node = self.node('a')
        created = self.job(contract=copy.deepcopy({'goal': 'g', 'harness': 'stub', 'harness_params': {'steps': []},
                                                   'inputs_manifest': {'version': 1, 'base': None, 'files': [], 'deleted': []},
                                                   'publish': {'mode': 'manual', 'requires_acceptance': True}}))
        overview = self.call('GET', '/overview')
        self.assertEqual(overview['counts']['queued'], 1)
        self.assertEqual(overview['outcomes']['running'], 1)
        self.assertEqual(set(overview['outcomes']), set(d.OUTCOMES))
        self.assertTrue(node and created)


class DisabledConsoleTests(ControllerCase):
    def test_root_says_the_console_was_turned_off_and_where_to_go(self):
        self.app.config['web_console'] = False
        result = self.app.handle('GET', '/', {}, b'')
        self.assertEqual(result.status, 200)
        text = result.body.decode('utf-8')
        self.assertIn('浏览器控制台已停用', text)
        self.assertIn('agent-collab client pair', text)
        self.assertNotIn('not installed', text)
        self.assertEqual(self.app.handle('GET', '/assets/x.js', {}, b'').status, 404)
        self.assertEqual(self.app.handle('GET', p.API_PREFIX + '/healthz', {}, b'').status, 200)


if __name__ == '__main__':
    unittest.main()
