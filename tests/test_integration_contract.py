"""controller 与 node agent 之间的接口解释一致性（集成时发现的缺口回归）。"""
import unittest

from agent_collab import domain
from agent_collab.controller import merge_health


def node(**cap):
    base = dict(os='linux', arch='x86_64', tools={}, gpu=[], harness={'stub': '0.1'}, labels=[])
    base.update(cap)
    return {'node_id': 'n1', 'physical_host_id': 'h', 'failure_domain': 'd', 'capabilities': base,
            'resources': {'slots': 1, 'host_slots': 1}, 'auth_health': {}, 'provider_account_refs': {},
            'drain': False, 'status': 'online', 'last_seen_at': '2026-11-09T00:00:00Z'}


def job(harness='stub', tools=(), gpu=None):
    return {'job_id': 'j', 'protection': 'unprotected', 'contract': {
        'harness': harness, 'resources': {'slots': 1}, 'harness_params': {'model': 'gpt-6-astra', 'effort': 'xhigh'},
        'requirements': {'os': [], 'arch': [], 'tools': list(tools), 'gpu': gpu, 'labels': []}}}


def reasons(j, n):
    row = domain.explain(j, [n], [], {}, domain.seconds('2026-11-09T00:00:01Z'), {'suspect_after_s': 30, 'lease_ttl_s': 90})['nodes'][0]
    return row['reasons']


class ContractTest(unittest.TestCase):
    def test_null_tool_or_harness_is_missing(self):
        self.assertIn('tool_missing:git', reasons(job(tools=['git']), node(tools={'git': None})))
        self.assertNotIn('tool_missing:git', reasons(job(tools=['git']), node(tools={'git': 'unknown'})))
        self.assertIn('harness_missing:codex', reasons(job('codex'), node(harness={'stub': '0.1', 'codex': None})))

    def test_cuda_requirement_matches_agent_gpu_shape(self):
        self.assertIn('gpu_missing', reasons(job(gpu='cuda'), node()))
        self.assertEqual(reasons(job(gpu='cuda'), node(gpu=[{'name': 'NVIDIA GeForce RTX 4090', 'cuda': True}])), [])

    def test_auth_unknown_schedulable_expired_not_and_sticky(self):
        n = node(harness={'stub': '0.1', 'codex': '0.167.0'})
        self.assertNotIn('auth_unhealthy:codex', reasons(job('codex'), n))  # 未上报
        n['auth_health'] = {'codex': 'unknown'}
        self.assertNotIn('auth_unhealthy:codex', reasons(job('codex'), n))  # 显式 unknown 仍可调度
        n['auth_health'] = merge_health({'codex': 'expired'}, {'codex': 'unknown'})
        self.assertIn('auth_unhealthy:codex', reasons(job('codex'), n))
        self.assertEqual(merge_health({'codex': 'expired'}, {'codex': 'ok'}), {'codex': 'ok'})


if __name__ == '__main__':
    unittest.main()
