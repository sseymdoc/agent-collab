"""Pure scheduling and recovery decisions; time and persisted facts are inputs."""
import copy
from datetime import datetime, timezone
import math
import re

from . import protocol as p
from .artifacts import DEFAULT_EXCLUDES, validate_excludes


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def seconds(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def transition(record, now, **changes):
    result = copy.deepcopy(record)
    if any(result.get(k) != v for k, v in changes.items()):
        result.update(changes)
        result['version'] += 1
        if 'updated_at' in result:
            result['updated_at'] = timestamp(now)
    return result


def string_list(value, name):
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError(name + ' must be a string list')


def validate_contract(value):
    if not isinstance(value, dict):
        raise ValueError('contract must be an object')
    c = copy.deepcopy(value)
    allowed = {'goal', 'acceptance', 'excludes', 'harness', 'harness_params', 'prompt',
               'requirements', 'inputs_manifest', 'recovery', 'budgets', 'resources', 'publish', 'integration', 'coordinates', 'workspace',
               'checks', 'coordination'}
    if set(c) - allowed:
        raise ValueError('unknown contract fields')
    for field in ('goal', 'prompt'):
        c.setdefault(field, '')
        if not isinstance(c[field], str):
            raise ValueError(field + ' must be a string')
    for field in ('acceptance', 'excludes'):
        c.setdefault(field, [])
        string_list(c[field], field)
    checks = c.setdefault('checks', [])
    if not isinstance(checks, list) or len(checks) > 16:
        raise ValueError('checks must be a list of at most 16 checks')
    names = set()
    for check in checks:
        if not isinstance(check, dict) or set(check) - {'name', 'run', 'timeout_s'}:
            raise ValueError('invalid check fields')
        name, run = check.get('name'), check.get('run')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', name) or name in names:
            raise ValueError('check names must be valid and unique')
        names.add(name)
        if not isinstance(run, list) or not run or any(not isinstance(arg, str) or not arg or '\x00' in arg for arg in run):
            raise ValueError('check.run must be a nonempty string argv')
        timeout = check.setdefault('timeout_s', 600)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 3600:
            raise ValueError('check.timeout_s must be between 1 and 3600')
    if c.get('harness') not in p.HARNESSES and c.get('harness') != 'claude':
        raise ValueError('unsupported harness')
    params = c.setdefault('harness_params', {})
    if not isinstance(params, dict):
        raise ValueError('harness_params must be an object')
    if c['harness'] == 'codex':
        params.setdefault('model', 'gpt-6-astra')
        params.setdefault('effort', 'xhigh')
        params.setdefault('sandbox', 'workspace-write')
        if (params['model'] != 'gpt-6-astra' or params['effort'] not in ('high', 'xhigh', 'max')
                or params['sandbox'] not in ('read-only', 'workspace-write')
                or set(params) - {'model', 'effort', 'sandbox'}):
            raise ValueError('invalid codex model, effort or sandbox')
    elif c['harness'] == 'claude':
        if set(params) != {'model', 'effort'} or any(not isinstance(v, str) or not v.strip() for v in params.values()):
            raise ValueError('claude requires explicit model and effort')
    elif set(params) - {'steps'} or not isinstance(params.get('steps', []), list):
        raise ValueError('invalid stub parameters')
    if 'coordinates' in c:
        coordinates = c['coordinates']
        if not isinstance(coordinates, dict) or set(coordinates) != {'job_id', 'work_package_id'} or c['harness'] != 'claude':
            raise ValueError('invalid coordinates')
        for key, value in coordinates.items():
            p.check_uuid(value, key)
    defaults = {
        'requirements': {'os': [], 'arch': [], 'tools': [], 'gpu': None, 'labels': []},
        'recovery': {'auto': True, 'side_effects': 'none'},
        'budgets': {'max_attempts': 3, 'max_migrations': 2, 'max_repairs': 2, 'deadline_s': 3600},
        'resources': {'slots': 1},
        'publish': {'mode': 'auto', 'requires_acceptance': False},
        'integration': {'target_node_id': None, 'target_path': None},
        'workspace': {'exclude': list(DEFAULT_EXCLUDES)},
        'coordination': {'policy': 'always'},
    }
    for field, base in defaults.items():
        supplied = c.setdefault(field, {})
        if not isinstance(supplied, dict) or set(supplied) - set(base):
            raise ValueError('invalid ' + field)
        c[field] = dict(base, **supplied)
    validate_excludes(c['workspace']['exclude'])
    if c['coordination']['policy'] not in ('always', 'on_failure', 'none'):
        raise ValueError('invalid coordination policy')
    req = c['requirements']
    for key in ('os', 'arch', 'tools', 'labels'):
        string_list(req[key], key)
    if set(req['os']) - set(p.OS_NAMES) or set(req['arch']) - {'x86_64', 'arm64'}:
        raise ValueError('invalid os/arch')
    if req['gpu'] is not None and not isinstance(req['gpu'], (str, dict)):
        raise ValueError('gpu must be null, a name, or property object')
    if type(c['recovery']['auto']) is not bool or c['recovery']['side_effects'] not in p.SIDE_EFFECTS:
        raise ValueError('invalid recovery')
    for key, value in c['budgets'].items():
        if type(value) is not int or value < (1 if key in ('max_attempts', 'deadline_s') else 0):
            raise ValueError('invalid budget ' + key)
    if type(c['resources']['slots']) is not int or c['resources']['slots'] < 1:
        raise ValueError('slots must be a positive integer')
    if c['publish']['mode'] not in ('auto', 'manual') or type(c['publish']['requires_acceptance']) is not bool:
        raise ValueError('invalid publish policy')
    if c['integration']['target_node_id'] is not None:
        p.check_node_id(c['integration']['target_node_id'])
    if c['integration']['target_path'] is not None and not isinstance(c['integration']['target_path'], str):
        raise ValueError('target_path must be a string')
    c.setdefault('inputs_manifest', None)
    if c['inputs_manifest'] is not None:
        c['inputs_manifest'] = p.validate_manifest(c['inputs_manifest'])
        # Admission checks lineage against controller-owned checkpoints; this is structural validation.
    return c


def evidence_gaps(contract, report, manifest_sha256):
    """Only checkpoint-bound execution results can satisfy required checks."""
    checks = contract.get('checks', [])
    evidence = report.get('checks') if isinstance(report, dict) else None
    if not isinstance(evidence, dict):
        return [{'check': c['name'], 'reason': 'no_report'} for c in checks]
    if evidence.get('manifest_sha256') != manifest_sha256:
        return [{'check': c['name'], 'reason': 'manifest_mismatch'} for c in checks]
    results = evidence.get('results')
    results = results if isinstance(results, list) else []
    gaps = []
    for check in checks:
        matches = [r for r in results if isinstance(r, dict) and r.get('name') == check['name']]
        reason = None
        if not matches:
            reason = 'missing'
        elif any(r.get('timed_out') is True for r in matches):
            reason = 'timed_out'
        elif len(matches) != 1 or any(type(r.get('exit_code')) is not int or r['exit_code'] != 0 or
                                      r.get('timed_out') is not False for r in matches):
            reason = 'failed'
        if reason:
            gaps.append({'check': check['name'], 'reason': reason})
    return gaps


def capability_bound(node, harness):
    return {'agent_version': node.get('agent_version'),
            'harness_version': node['capabilities']['harness'].get(harness),
            'harness_path': node['capabilities'].get('harness_paths', {}).get(harness)}


def capability_evidence(node):
    evidence = copy.deepcopy(node.get('capability_evidence', {}))
    for harness in set(node['capabilities']['harness']) | set(evidence):
        bound = capability_bound(node, harness)
        entry = evidence.setdefault(harness, {'status': 'unproven', 'bound': bound,
                                             'last_success': None, 'last_failure': None})
        if entry['bound'] != bound:
            entry['status'] = 'unproven'
    return evidence


DECISION_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['decision', 'assessment', 'follow_up', 'notes'],
    'properties': {
        'decision': {'type': 'string', 'enum': ['accept', 'rework', 'escalate']},
        'assessment': {
            'type': 'object', 'additionalProperties': False,
            'required': ['status', 'method', 'result', 'checkpoint_id'],
            'properties': {
                'status': {'type': 'string', 'enum': ['passed', 'rework', 'blocked', 'unverified']},
                'method': {'type': 'string'}, 'result': {'type': 'string'},
                'checkpoint_id': {'type': ['string', 'null']}}},
        'follow_up': {'anyOf': [{'type': 'null'}, {
            'type': 'object', 'additionalProperties': False,
            'required': ['title', 'goal', 'acceptance', 'instructions', 'effort'],
            'properties': {'title': {'type': 'string'}, 'goal': {'type': 'string'},
                           'acceptance': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1},
                           'instructions': {'type': 'string'},
                           'effort': {'type': 'string', 'enum': ['high', 'xhigh', 'max']}}}]},
        'notes': {'type': 'string'}}}


def validate_follow_up(value):
    schema = DECISION_SCHEMA['properties']['follow_up']['anyOf'][1]
    if not isinstance(value, dict) or set(value) != set(schema['required']):
        raise ValueError('follow_up requires only title, goal, acceptance, instructions and effort')
    if any(not isinstance(value[key], str) for key in ('title', 'goal', 'instructions', 'effort')):
        raise ValueError('follow_up title, goal, instructions and effort must be strings')
    string_list(value['acceptance'], 'follow_up.acceptance')
    if not value['acceptance'] or value['effort'] not in schema['properties']['effort']['enum']:
        raise ValueError('follow_up requires nonempty acceptance and effort high/xhigh/max')


def validate_decision_schema(value, check_follow_up=True):
    """Validate the fixed wire schema; no target-dependent or contract validation.

    Only the controller skips follow_up here, so its errors cannot discard an assessment.
    """
    required = set(DECISION_SCHEMA['required'])
    if not isinstance(value, dict) or set(value) - required or (required - {'follow_up'} - set(value)):
        raise ValueError('decision fields do not match schema')
    decision, assessment = value['decision'], value['assessment']
    if decision not in ('accept', 'rework', 'escalate') or not isinstance(value['notes'], str):
        raise ValueError('invalid decision or notes')
    if not isinstance(assessment, dict) or set(assessment) != {'status', 'method', 'result', 'checkpoint_id'}:
        raise ValueError('invalid assessment fields')
    if assessment['status'] not in p.ACCEPTANCE or any(not isinstance(assessment[k], str) for k in ('method', 'result')):
        raise ValueError('invalid assessment')
    if assessment['checkpoint_id'] is not None and not isinstance(assessment['checkpoint_id'], str):
        raise ValueError('checkpoint_id must be a string or null')
    if check_follow_up:
        if 'follow_up' not in value:
            raise ValueError('follow_up is required')
        if value['follow_up'] is not None:
            validate_follow_up(value['follow_up'])
    return copy.deepcopy(value)


def validate_decision(value, check_follow_up=True):
    """Controller assessment semantics, independent of whether a repair can be admitted."""
    value = validate_decision_schema(value, check_follow_up=check_follow_up)
    decision, assessment = value['decision'], value['assessment']
    if assessment['checkpoint_id'] is not None:
        p.check_uuid(assessment['checkpoint_id'], 'checkpoint_id')
    if decision != 'escalate' and (assessment['checkpoint_id'] is None or
            assessment['status'] != {'accept': 'passed', 'rework': 'rework'}[decision]):
        raise ValueError('decision and assessment disagree')
    if check_follow_up and value['follow_up'] is not None and decision != 'rework':
        raise ValueError('follow_up requires rework')
    return value


def repair_contract(target, follow_up, inputs_manifest):
    """Derive execution policy from the target; the model supplies only a repair request."""
    validate_follow_up(follow_up)
    spec = {key: copy.deepcopy(target[key]) for key in
            ('excludes', 'requirements', 'recovery', 'resources', 'publish', 'integration', 'workspace', 'budgets',
             'checks', 'coordination')
            if key in target}
    requirements = spec.setdefault('requirements', {})
    tools = requirements.setdefault('tools', [])
    if 'codex' not in tools:
        tools.append('codex')
    spec.update(harness='codex', harness_params={'model': 'gpt-6-astra', 'effort': follow_up['effort'], 'sandbox': 'workspace-write'},
                goal=follow_up['goal'], acceptance=copy.deepcopy(follow_up['acceptance']), inputs_manifest=inputs_manifest,
                prompt='Instructions:\n' + follow_up['instructions'] + '\n\nGoal:\n' + follow_up['goal'] +
                       '\n\nAcceptance:\n' + '\n'.join('- ' + item for item in follow_up['acceptance']) +
                       '\n\nExcludes:\n' + '\n'.join('- ' + item for item in target['excludes']))
    contract = validate_contract(spec)
    contract['budgets'] = {key: min(value, target['budgets'][key]) for key, value in contract['budgets'].items()}
    return contract


def managed_profile(config):
    setting = config.get('managed_coordinator')
    profile_id = setting.get('profile_id') if isinstance(setting, dict) else None
    if not isinstance(profile_id, str):
        profile_id = None
    result = {'status': 'profile_missing', 'profile_id': profile_id, 'profile': None}
    if isinstance(setting, dict) and setting.get('enabled') is False:
        result['status'] = 'disabled'
        return result
    profiles = config.get('standby_profiles')
    profile = profiles.get(profile_id) if isinstance(profiles, dict) and isinstance(profile_id, str) else None
    if not isinstance(setting, dict) or setting.get('enabled') is not True or not isinstance(profile, dict):
        return result
    if profile.get('harness') != 'claude' or any(not isinstance(profile.get(k), str) or not profile[k].strip() for k in ('model', 'effort')):
        return result
    ids = profile.get('node_ids')
    if not isinstance(ids, list) or not ids:
        return result
    try:
        for node_id in ids:
            p.check_node_id(node_id)
    except (ValueError, TypeError):
        return result
    result.update(status='ready', profile={k: copy.deepcopy(profile[k]) for k in ('harness', 'model', 'effort', 'node_ids')})
    return result


def node_status(node, now, config):
    age = now - seconds(node['last_seen_at'])
    if age >= config['lease_ttl_s']:
        return 'unreachable'
    if age >= config['suspect_after_s']:
        return 'suspect'
    return 'online'


def budget_reason(job, counters, now, root_created_at, migration=False, repair=False):
    b = job['contract']['budgets']
    for key, delta in (('attempts', 1), ('migrations', int(migration)), ('repairs', int(repair))):
        if counters[key] + delta > b['max_' + key]:
            return 'budget_exhausted:' + key
    # No max_infra_retries exists in v1; max_attempts bounds retries too.
    if now - seconds(root_created_at) >= b['deadline_s']:
        return 'budget_exhausted:deadline_s'
    return None


def recovery_decision(job, attempt, counters=None, now=0, root_created_at=None, side_effects=None):
    failure = attempt.get('failure_class')
    if job['cancel']['requested_at'] or failure == 'user_cancel':
        return {'status': 'cancel_requested' if job['cancel']['requested_at'] else 'waiting_reconcile',
                'reason': 'user_cancel', 'recover': False}
    if any(value in ('unknown', 'intent') for value in (side_effects or {}).values()):
        # Intent without done may already have taken effect.
        return {'status': 'waiting_reconcile', 'reason': 'side_effect_unknown', 'recover': False}
    if failure == 'code_failure':
        return {'status': 'failed', 'reason': 'code_failure', 'recover': False}
    if failure in ('quota', 'auth_expired', 'capability_missing'):
        return {'status': 'waiting_capability', 'reason': failure, 'recover': False}
    reason = budget_reason(job, counters or job['counters'], now, root_created_at or job['created_at'])
    if reason:
        return {'status': 'failed', 'reason': reason, 'recover': False}
    recovery = job['contract']['recovery']
    missing = []
    if not recovery['auto']:
        missing.append('auto_recovery_disabled')
    if job['protection'] != 'protected':
        missing.append('protection_not_ready')
    if recovery['side_effects'] == 'manual':
        missing.append('manual_side_effects')
    if failure not in p.RECOVERABLE_FAILURES:
        missing.append('failure_not_retryable')
    if missing:
        return {'status': 'waiting_reconcile', 'reason': ','.join(missing), 'recover': False}
    return {'status': 'recovering', 'reason': failure, 'recover': True}


def explain(job, nodes, attempts, jobs, now, config, paused=False, quota=None,
            excluded=None, source_node=None, recovering=False):
    quota = quota or {}
    excluded = excluded or []
    active = [a for a in attempts if a['status'] in p.ATTEMPT_ACTIVE]
    weights = dict(free_slots=10.0, success_rate=2.0, same_host=1.0,
                   different_failure_domain=20.0, warm_start=1.0)
    weights.update(config.get('score_weights', {}))
    slots = job['contract']['resources']['slots']
    result = []
    for node in nodes:
        cap, req = node['capabilities'], job['contract']['requirements']
        harness, host = job['contract']['harness'], node['physical_host_id']
        node_used = sum(jobs[a['job_id']]['contract']['resources']['slots'] for a in active if a['node_id'] == node['node_id'])
        peers = [n for n in nodes if n['physical_host_id'] == host]
        peer_ids = {n['node_id'] for n in peers}
        host_used = sum(jobs[a['job_id']]['contract']['resources']['slots'] for a in active if a['node_id'] in peer_ids)
        host_limit = min(n['resources']['host_slots'] for n in peers)
        reasons = []
        if paused:
            reasons.append('dispatch_paused')
        if node['drain']:
            reasons.append('draining')
        status = node_status(node, now, config)
        if status != 'online':
            reasons.append('node_' + status)
        for key in ('os', 'arch'):
            if req[key] and cap[key] not in req[key]:
                reasons.append(key + '_mismatch')
        # 协议 §3：tools/harness 中值为 null 表示未安装或不可用。
        reasons.extend('tool_missing:' + t for t in req['tools'] if cap['tools'].get(t) is None)
        gpu = req['gpu']
        if gpu is not None:
            matches = [g for g in cap['gpu'] if (g == gpu or (isinstance(g, dict) and
                       ((isinstance(gpu, str) and (g.get('name') == gpu or (gpu == 'cuda' and g.get('cuda') is True))) or
                        (isinstance(gpu, dict) and all(g.get(k) == v for k, v in gpu.items())))))]
            if not matches:
                reasons.append('gpu_missing')
        # Protocol has no label reason code; retain the missing capability name.
        reasons.extend('label_missing:' + label for label in req['labels'] if label not in cap['labels'])
        if cap['harness'].get(harness) is None:
            reasons.append('harness_missing:' + harness)
        # 只排除已确认过期的认证；unknown 不等于失败，本地登录状态也不等于服务端接受。
        if harness != 'stub' and node['auth_health'].get(harness) == 'expired':
            reasons.append('auth_unhealthy:' + harness)
        ref = node['provider_account_refs'].get(harness)
        if ref and quota.get(harness + ':' + ref, 0) > now:
            reasons.append('account_quota_blocked:' + ref)
        if node_used + slots > node['resources']['slots']:
            reasons.append('no_node_slot')
        if host_used + slots > host_limit:
            reasons.append('no_host_slot:' + host)
        if node['node_id'] in excluded:
            reasons.append('excluded_by_request')
        if job['protection'] == 'preparing':
            reasons.append('protection_not_ready')
        ended = [a for a in attempts if a['node_id'] == node['node_id'] and a['status'] in ('succeeded', 'failed', 'lost')][-20:]
        same_host = bool(source_node and source_node['physical_host_id'] == host)
        breakdown = {
            'free_slots': (node['resources']['slots'] - node_used) / node['resources']['slots'],
            'success_rate': sum(a['status'] == 'succeeded' for a in ended) / len(ended) if ended else 0.5,
            'same_host': int(same_host and not recovering),
            'different_failure_domain': int(bool(recovering and source_node and source_node['failure_domain'] != node['failure_domain'])),
            'warm_start': int(bool(source_node and source_node['node_id'] == node['node_id'])),
        }
        breakdown = {k: v * weights[k] for k, v in breakdown.items()}
        result.append({'node_id': node['node_id'], 'eligible': not reasons, 'reasons': reasons,
                       'score': sum(breakdown.values()), 'breakdown': breakdown,
                       'capability_evidence': capability_evidence(node)})
    eligible = sorted((row for row in result if row['eligible']), key=lambda r: (-r['score'], r['node_id']))
    return {'nodes': result, 'selected_node_id': eligible[0]['node_id'] if eligible else None}
