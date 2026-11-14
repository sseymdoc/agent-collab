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


STUB_STEPS = {'write': dict, 'delete': dict, 'sleep': (int, float), 'checkpoint': dict,
              'progress': dict, 'side_effect': dict, 'fail': str, 'run': list}


def validate_stub_step(step):
    """§9 stub 步骤在受理时校验形状，避免畸形契约被派单后才在节点上失败。"""
    if not isinstance(step, dict) or len(step) != 1:
        raise ValueError('stub step must contain exactly one operation')
    op, value = next(iter(step.items()))
    if op not in STUB_STEPS or isinstance(value, bool) or not isinstance(value, STUB_STEPS[op]):
        raise ValueError('unsupported or malformed stub step: ' + str(op))
    if op in ('write', 'delete'):
        p.check_rel_path(value.get('path'))
        if op == 'write' and not isinstance(value.get('content'), str):
            raise ValueError('stub write requires string content')
    elif op == 'sleep' and (not math.isfinite(value) or value < 0):
        raise ValueError('stub sleep must be a nonnegative number')
    elif op == 'fail' and value not in p.FAILURE_CLASSES:
        raise ValueError('stub fail requires a failure class')
    elif op == 'run' and (not value or any(not isinstance(arg, str) or not arg for arg in value)):
        raise ValueError('stub run requires a nonempty string argv')
    elif op == 'side_effect' and (not isinstance(value.get('op_id'), str) or not value['op_id']
                                  or value.get('state') not in ('intent', 'done', 'unknown')):
        # 与 controller 受理 attempt.side_effect 事件的条件一致，避免派单后才被拒。
        raise ValueError('stub side_effect requires nonempty op_id and state intent/done/unknown')


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
        params.setdefault('sandbox', 'workspace-write')
        if params['sandbox'] not in ('read-only', 'workspace-write') or set(params) - {'model', 'effort', 'sandbox'}:
            raise ValueError('invalid codex parameters or sandbox')
        # model 与 effort 由 controller 受理时按模型策略解析后写入（§12.4）；这里只校验形状，不带默认值。
        p.check_model(params.get('model'))
        p.check_effort(params.get('effort'))
    elif c['harness'] == 'claude':
        if set(params) != {'model', 'effort'} or any(not isinstance(v, str) or not v.strip() for v in params.values()):
            raise ValueError('claude requires explicit model and effort')
    elif set(params) - {'steps'} or not isinstance(params.get('steps', []), list):
        raise ValueError('invalid stub parameters')
    else:
        for step in params.get('steps', []):
            validate_stub_step(step)
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
                           # null：沿用根工作包冻结的 effort；给出值时由 controller 按冻结的许可范围检查（§12.4）。
                           'effort': {'type': ['string', 'null']}}}]},
        'notes': {'type': 'string'}}}

# 升级前的 controller 下发的 schema：effort 必填且只有这三个值。节点两种都接受，便于先升级节点。
LEGACY_DECISION_SCHEMA = copy.deepcopy(DECISION_SCHEMA)
LEGACY_DECISION_SCHEMA['properties']['follow_up']['anyOf'][1]['properties']['effort'] = {
    'type': 'string', 'enum': list(p.LEGACY_CODEX['efforts'])}


def validate_follow_up(value, legacy=False):
    schema = DECISION_SCHEMA['properties']['follow_up']['anyOf'][1]
    if not isinstance(value, dict) or set(value) != set(schema['required']):
        raise ValueError('follow_up requires only title, goal, acceptance, instructions and effort')
    if any(not isinstance(value[key], str) for key in ('title', 'goal', 'instructions')):
        raise ValueError('follow_up title, goal and instructions must be strings')
    string_list(value['acceptance'], 'follow_up.acceptance')
    if not value['acceptance']:
        raise ValueError('follow_up requires nonempty acceptance')
    if legacy:
        if value['effort'] not in p.LEGACY_CODEX['efforts']:
            raise ValueError('follow_up requires effort high/xhigh/max')
    elif value['effort'] is not None:
        p.check_effort(value['effort'])


def validate_decision_schema(value, check_follow_up=True, legacy=False):
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
            validate_follow_up(value['follow_up'], legacy)
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


class PolicyError(ValueError):
    """模型策略拒绝；reason 进入 API 错误的 details 或协调事件的原因。"""
    def __init__(self, reason, message=None):
        super().__init__(message or reason)
        self.reason = reason


# unverified_nodes：模型目录未核实（未知或已过期）的节点能否承接 codex 任务（§12.5）。
# wait（缺省）只派给目录已确认含该组合的节点；allow 是显式开启的“未核实能力尝试”，仍优先已确认的节点。
UNVERIFIED_NODES = ('wait', 'allow')
EMPTY_SETTINGS = {'revision': 0, 'updated_at': None, 'updated_by': None,
                  'codex': {'default': {'model': None, 'effort': None}, 'allowed': None, 'unverified_nodes': 'wait'},
                  'claude': {'default': {'model': None, 'effort': None}}}


def migrated_settings(profile):
    """升级前的部署启动时写入一次：此前写死在代码里的 Codex 组合，加上当时生效的协调 profile。"""
    value = copy.deepcopy(EMPTY_SETTINGS)
    legacy = p.LEGACY_CODEX
    value['codex'] = {'default': {'model': legacy['model'], 'effort': legacy['effort']},
                      'allowed': {legacy['model']: list(legacy['efforts'])}, 'unverified_nodes': 'wait'}
    try:
        if profile:
            value['claude']['default'] = {'model': p.check_model(profile['model']), 'effort': p.check_effort(profile['effort'])}
    except ValueError:
        pass  # profile 的取值不符合设置的字段约束：不迁入，协调回合继续直接用 profile
    return value


def permitted(allowed, model, effort):
    return allowed is None or effort in allowed.get(model, ())


def _model_pair(value, name):
    if not isinstance(value, dict) or set(value) != {'model', 'effort'}:
        raise ValueError(name + ' requires exactly model and effort')
    if value['model'] is None and value['effort'] is None:
        return dict(value)
    return {'model': p.check_model(value['model']), 'effort': p.check_effort(value['effort'])}


def validate_model_settings(codex, claude):
    if not isinstance(codex, dict) or not {'default', 'allowed'} <= set(codex) or set(codex) - {'default', 'allowed', 'unverified_nodes'}:
        raise ValueError('codex settings require default and allowed, and may set unverified_nodes')
    unverified = codex.get('unverified_nodes', 'wait')
    if unverified not in UNVERIFIED_NODES:
        raise ValueError('codex.unverified_nodes must be wait or allow')
    default, allowed = _model_pair(codex['default'], 'codex.default'), codex['allowed']
    if allowed is not None:
        if not isinstance(allowed, dict) or not allowed or len(allowed) > 64:
            raise ValueError('codex.allowed must be null or a nonempty model map')
        for model, efforts in allowed.items():
            p.check_model(model)
            if not isinstance(efforts, list) or not efforts or len(efforts) > 16 or len(set(map(str, efforts))) != len(efforts):
                raise ValueError('codex.allowed values must be nonempty lists of distinct efforts')
            for effort in efforts:
                p.check_effort(effort)
        allowed = copy.deepcopy(allowed)
        if default['model'] is not None and not permitted(allowed, default['model'], default['effort']):
            raise PolicyError('model_not_allowed', 'codex.default must be within codex.allowed')
    if not isinstance(claude, dict) or set(claude) != {'default'}:
        raise ValueError('claude settings require exactly default')
    return {'codex': {'default': default, 'allowed': allowed, 'unverified_nodes': unverified},
            'claude': {'default': _model_pair(claude['default'], 'claude.default')}}


def validate_project_defaults(value):
    """项目默认策略（§13.1）：每一项为 null 表示沿用工作区的值。model 与 effort 要么都给、要么都不给。"""
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - {'codex', 'exclude'}:
        raise ValueError('project defaults may only set codex and exclude')
    codex = {} if value.get('codex') is None else value['codex']
    if not isinstance(codex, dict) or set(codex) - {'model', 'effort', 'unverified_nodes'}:
        raise ValueError('project codex defaults may only set model, effort and unverified_nodes')
    pair = _model_pair({'model': codex.get('model'), 'effort': codex.get('effort')}, 'project codex default')
    unverified = codex.get('unverified_nodes')
    if unverified is not None and unverified not in UNVERIFIED_NODES:
        raise ValueError('project unverified_nodes must be wait, allow or null')
    exclude = value.get('exclude')
    if exclude is not None:
        if len(validate_excludes(exclude)) > 200:
            raise ValueError('too many exclude patterns')
        exclude = list(exclude)
    return {'codex': dict(pair, unverified_nodes=unverified), 'exclude': exclude}


def resolve_codex(params, base, default_source):
    """请求显式给出的值优先，其次 base（工作区默认值或根冻结值）；解析不出或不在许可范围时拒绝，不替换。"""
    requested = {key: params.get(key) for key in ('model', 'effort')}
    resolved, source = {}, {}
    for key in ('model', 'effort'):
        if requested[key] is not None:
            resolved[key], source[key] = requested[key], 'task_override'
        elif base.get(key) is not None:
            resolved[key], source[key] = base[key], default_source
        else:
            raise PolicyError('model_default_unset', 'no codex %s was given and no default is configured' % key)
    p.check_model(resolved['model'])
    p.check_effort(resolved['effort'])
    if not permitted(base.get('allowed'), resolved['model'], resolved['effort']):
        raise PolicyError('model_not_allowed', 'codex model/effort is outside the permitted set')
    return resolved, requested, source


def project_base(settings, project):
    """项目默认值叠在工作区默认值之上（§13.1）：返回 (codex 默认 pair, 来源, unverified_nodes)。许可范围始终取工作区的。"""
    pair, source = dict(settings['codex']['default']), 'workspace_default'
    unverified = settings['codex'].get('unverified_nodes', 'wait')
    if project:
        chosen = project['defaults']['codex']
        if chosen['model'] is not None:
            pair, source = {'model': chosen['model'], 'effort': chosen['effort']}, 'project_default'
        if chosen['unverified_nodes'] is not None:
            unverified = chosen['unverified_nodes']
    return pair, source, unverified


def frozen_policy(settings, resolved=None, project=None):
    """根工作包受理时冻结；之后的修订与受管修复只读它，不读后来的工作区设置或项目默认值。"""
    pair, _, unverified = project_base(settings, project)
    base = resolved or pair
    policy = {'schema': 1, 'settings_revision': settings['revision'],
              'codex': {'model': base['model'], 'effort': base['effort'], 'allowed': copy.deepcopy(settings['codex']['allowed']),
                        'unverified_nodes': unverified}}
    if project:
        policy.update(project_id=project['project_id'], project_revision=project['revision'])
    return policy


def legacy_root_policy(root_job):
    """升级前创建的根没有冻结策略：按当时写死的行为推导。"""
    params = root_job['contract']['harness_params'] if root_job['contract']['harness'] == 'codex' else {}
    model = params.get('model') or p.LEGACY_CODEX['model']
    return {'schema': 1, 'settings_revision': None, 'legacy': True,
            'codex': {'model': model, 'effort': params.get('effort') or p.LEGACY_CODEX['effort'],
                      'allowed': {model: list(p.LEGACY_CODEX['efforts'])}}}


def codex_catalog(node):
    """节点上报的 codex 模型目录。没有上报的旧节点取其适配器实际接受的固定集合，不猜测更多。"""
    models = node['capabilities'].get('models')
    if models is None:
        legacy = p.LEGACY_CODEX
        return {'status': 'legacy', 'source': 'adapter_fixed_set', 'fetched_at': None, 'reason': None,
                'cli_version': node['capabilities']['harness'].get('codex'),
                'models': [{'id': legacy['model'], 'efforts': list(legacy['efforts']), 'default_effort': legacy['effort']}]}
    return models.get('codex') or {'status': 'unknown', 'source': None, 'fetched_at': None, 'reason': 'not_reported',
                                   'cli_version': None, 'models': []}


def catalog_state(catalog, now, max_age_s):
    """目录在此刻的可用状态（§12.5）。

    ok：最近一次探测成功；stale：最近一次探测失败，沿用上一次成功的目录（节点已核对 CLI 版本与 provider 未变）；
    legacy：没有上报目录的旧节点，按其适配器的固定集合；unknown：没有可用目录；
    expired：目录的取得时间早于 max_age_s，不再当作当前能力。ok、stale、legacy 是“已核实”，其余是“未核实”。
    """
    status = catalog['status']
    if status in ('ok', 'stale'):
        try:
            if now - seconds(catalog['fetched_at']) > max_age_s:
                return 'expired'
        except (TypeError, ValueError, AttributeError):
            return 'unknown'  # 没有取得时间就无从判断新旧
    return status


VERIFIED_CATALOG = ('ok', 'stale', 'legacy')


def validate_catalogs(value):
    """节点注册里的 capabilities.models（§12.5）。provider 是探测与执行共用的模型 provider 身份，可缺省。"""
    if not isinstance(value, dict) or set(value) - {'codex', 'claude'}:
        raise ValueError('invalid models capability')
    for entry in value.values():
        required = {'status', 'source', 'fetched_at', 'cli_version', 'reason', 'models'}
        if (not isinstance(entry, dict) or not required <= set(entry) or set(entry) - required - {'provider'}
                or entry['status'] not in ('ok', 'stale', 'unknown') or not isinstance(entry['models'], list) or len(entry['models']) > 200
                or any(entry.get(k) is not None and (not isinstance(entry[k], str) or len(entry[k]) > 200)
                       for k in ('source', 'fetched_at', 'cli_version', 'reason', 'provider'))):
            raise ValueError('invalid model catalog')
        if entry['status'] == 'stale':
            seconds(entry['fetched_at'])  # 沿用的旧目录必须带它的取得时间，否则无法判断是否过期
        for model in entry['models']:
            if not isinstance(model, dict) or set(model) != {'id', 'efforts', 'default_effort'} or not isinstance(model['efforts'], list):
                raise ValueError('invalid model catalog entry')
            p.check_model(model['id'])
            for effort in model['efforts'][:32] + ([model['default_effort']] if model['default_effort'] is not None else []):
                p.check_effort(effort)
            if len(model['efforts']) > 32:
                raise ValueError('too many efforts in model catalog entry')


def selectable_codex(settings, nodes, now, max_age_s):
    """部署策略与已核实节点目录的交集；目录未核实（未知或过期）的节点单独列出，不计入可选组合。"""
    allowed, combos, unknown = settings['codex']['allowed'], {}, []
    for node in nodes:
        if node['capabilities']['harness'].get('codex') is None or node.get('retired_at'):
            continue
        catalog = codex_catalog(node)
        if catalog_state(catalog, now, max_age_s) not in VERIFIED_CATALOG:
            unknown.append(node['node_id'])
            continue
        for model in catalog['models']:
            efforts = [e for e in model['efforts'] if permitted(allowed, model['id'], e)]
            if efforts:
                entry = combos.setdefault(model['id'], {'model': model['id'], 'efforts': [], 'nodes': []})
                entry['efforts'] += [e for e in efforts if e not in entry['efforts']]
                if node['node_id'] not in entry['nodes']:
                    entry['nodes'].append(node['node_id'])
    return {'codex': list(combos.values()), 'unknown_nodes': unknown}


TEMPLATES = (
    {'id': 'codex-change', 'title': '修改并检查', 'harness': 'codex', 'diagnostic': False,
     'description': 'Codex 按目标修改工作区并运行检查；失败或发生接管时才触发协调，结果自动发布。'},
    {'id': 'codex-change-accept', 'title': '执行并验收', 'harness': 'codex', 'diagnostic': False,
     'description': 'Codex 修改并运行检查，随后做语义验收；验收通过后由你发布成果。'},
    {'id': 'diagnostic', 'title': '链路自检', 'harness': 'stub', 'diagnostic': True,
     'description': '确定性步骤，不调用模型；用于检查 controller、节点、检查与工件链路。'},
)


def draft_contract(data, inputs_manifest):
    """把模板与自然语言目标展开成可提交的契约请求；只做展开，校验与模型解析由受理路径完成。"""
    allowed = {'template', 'title', 'goal', 'acceptance', 'excludes', 'baseline_id', 'inputs_manifest', 'checks',
               'model', 'effort', 'requirements', 'request_id', 'project_id'}
    if set(data) - allowed:
        raise ValueError('unknown draft fields')
    template = next((t for t in TEMPLATES if t['id'] == data.get('template')), None)
    if template is None:
        raise ValueError('unknown template')
    goal = data.get('goal')
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError('goal is required')
    goal = goal.strip()
    acceptance, excludes = data.get('acceptance', []), data.get('excludes', [])
    string_list(acceptance, 'acceptance')
    string_list(excludes, 'excludes')
    title = data.get('title') or goal.splitlines()[0][:80]
    if not isinstance(title, str):
        raise ValueError('title must be a string')
    requirements = dict(data.get('requirements') or {})
    contract = {'goal': goal, 'acceptance': acceptance, 'excludes': excludes, 'inputs_manifest': inputs_manifest,
                'checks': data.get('checks', []), 'requirements': requirements}
    if template['harness'] == 'stub':
        if data.get('model') is not None or data.get('effort') is not None:
            raise ValueError('the diagnostic template does not take a model')
        contract.update(harness='stub', coordination={'policy': 'none'},
                        harness_params={'steps': [{'write': {'path': 'agent-collab-diagnostic.txt', 'content': goal + '\n'}}]})
        return title, contract
    checks = contract['checks'] if isinstance(contract['checks'], list) else []
    sections = ['Goal:\n' + goal]
    if acceptance:
        sections.append('Acceptance:\n' + '\n'.join('- ' + item for item in acceptance))
    if excludes:
        sections.append('Excludes:\n' + '\n'.join('- ' + item for item in excludes))
    if checks:
        sections.append('Checks run after you finish (they must pass):\n' + '\n'.join(
            '- %s: %s' % (c.get('name'), ' '.join(map(str, c.get('run', [])))) for c in checks if isinstance(c, dict)))
    tools = requirements.setdefault('tools', [])
    if isinstance(tools, list) and 'codex' not in tools:
        tools.append('codex')
    params = {key: data[key] for key in ('model', 'effort') if data.get(key) is not None}
    accept = template['id'] == 'codex-change-accept'
    contract.update(harness='codex', harness_params=dict(params, sandbox='workspace-write'), prompt='\n\n'.join(sections),
                    coordination={'policy': 'always' if accept else 'on_failure'},
                    publish={'mode': 'manual', 'requires_acceptance': True} if accept else {'mode': 'auto', 'requires_acceptance': False})
    return title, contract


def repair_contract(target, follow_up, inputs_manifest, policy):
    """Derive execution policy from the target; the model supplies only a repair request."""
    validate_follow_up(follow_up)
    codex = policy['codex']
    effort = follow_up['effort'] or codex['effort']
    if codex['model'] is None or effort is None:
        raise PolicyError('model_default_unset')
    if follow_up['effort'] is not None and not permitted(codex['allowed'], codex['model'], effort):
        raise PolicyError('effort_not_allowed')
    spec = {key: copy.deepcopy(target[key]) for key in
            ('excludes', 'requirements', 'recovery', 'resources', 'publish', 'integration', 'workspace', 'budgets',
             'checks', 'coordination')
            if key in target}
    requirements = spec.setdefault('requirements', {})
    tools = requirements.setdefault('tools', [])
    if 'codex' not in tools:
        tools.append('codex')
    spec.update(harness='codex', harness_params={'model': codex['model'], 'effort': effort, 'sandbox': 'workspace-write'},
                goal=follow_up['goal'], acceptance=copy.deepcopy(follow_up['acceptance']), inputs_manifest=inputs_manifest,
                prompt='Instructions:\n' + follow_up['instructions'] + '\n\nGoal:\n' + follow_up['goal'] +
                       '\n\nAcceptance:\n' + '\n'.join('- ' + item for item in follow_up['acceptance']) +
                       '\n\nExcludes:\n' + '\n'.join('- ' + item for item in target['excludes']))
    contract = validate_contract(spec)
    contract['budgets'] = {key: min(value, target['budgets'][key]) for key, value in contract['budgets'].items()}
    return contract


def managed_profile(config, claude_default=None, coordination=None):
    """受管协调回合实际使用的配置。

    claude_default：设置里的协调模型（§12.3），给出时覆盖 model 与 effort。
    coordination：经 API 保存的协调设置（§13.7，revision > 0 时生效）：是否启用与候选节点列表由它决定，
    config.json 的 profile 只在没有设置模型时提供 model 与 effort；没有这份设置的部署沿用 config.json。
    """
    result = _managed_profile(config)
    if coordination and coordination.get('revision', 0) > 0:
        fallback = result['profile'] or {}
        model = (claude_default or {}).get('model') or fallback.get('model')
        effort = (claude_default or {}).get('effort') if (claude_default or {}).get('model') else fallback.get('effort')
        result = {'status': 'profile_missing', 'profile_id': 'settings', 'profile': None, 'source': 'settings'}
        if not coordination['enabled']:
            result['status'] = 'disabled'
        elif coordination['node_ids'] and model and effort:
            result.update(status='ready', profile={'harness': 'claude', 'model': model, 'effort': effort,
                                                   'node_ids': list(coordination['node_ids'])})
        return result
    if result['status'] == 'ready' and claude_default and claude_default.get('model'):
        result['profile'].update(model=claude_default['model'], effort=claude_default['effort'])
    return result


def _managed_profile(config):
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


RESTING = ('completed', 'failed', 'cancelled', 'waiting_reconcile')  # 不再自行变化、等人或等下一步的状态
ENDED = ('completed', 'failed', 'cancelled')
OUTCOMES = ('running', 'awaiting_coordination', 'needs_attention', 'awaiting_acceptance', 'ready_to_publish',
            'awaiting_publication', 'done', 'unknown')


def job_outcome(job, coordinating=False):
    """任务现在处在哪一步（CLI 摘要的 outcome、总览的待办计数共用）。coordinating：有没有仍在进行的协调回合。

    运行状态、验收与发布是分开的事实：completed 只表示执行结束；要求验收而没有“通过”的才是待验收。
    契约里没有发布规则的旧记录无法判断，记 unknown，不当作待办。"""
    if job['status'] not in RESTING:
        return 'running'
    if coordinating:
        # 失败或待对账也可能触发协调回合；回合结束前它可能新建修复修订，不能先判为需人工处理。
        return 'awaiting_coordination'
    # 验收与发布都要属于当前检查点：通过或发布之后再重试，旧记录仍留在任务上，但说的是上一次执行的成果。
    current = job.get('durable_checkpoint_id')
    acceptance = job['acceptance']['status']
    if job['acceptance'].get('checkpoint_id', current) != current:
        acceptance = 'unverified'
    if job['status'] != 'completed' or acceptance in ('rework', 'blocked'):
        return 'needs_attention'
    publish = job['contract'].get('publish')
    if not isinstance(publish, dict):
        return 'unknown'
    if publish.get('requires_acceptance') and acceptance != 'passed':
        return 'awaiting_acceptance'
    if not job['delivery_version'] or job.get('delivery_checkpoint_id', current) != current:
        # 执行结束不等于成果已发布：手动发布等调用方，自动发布等执行节点完成发布。
        return 'ready_to_publish' if publish.get('mode') == 'manual' else 'awaiting_publication'
    return 'done'


def outcome_counts(jobs):
    """总览的待办计数。jobs 按写入顺序排列（Store.all 的顺序）：每个根工作包只看最后写入的一版修订
    （被取代的旧修订不再是待办），不按 created_at 判断，时钟回拨或同一毫秒也不会选错；协调回合本身不计，
    它的进行与结论归到被协调的任务。"""
    coordinating = {job['contract']['coordinates']['job_id'] for job in jobs
                    if (job['contract'].get('coordinates') or {}).get('job_id') and job['status'] not in ENDED}
    latest = {}
    for job in jobs:
        if job['contract'].get('coordinates'):
            continue
        latest[job['work_package_id']] = job
    counts = dict.fromkeys(OUTCOMES, 0)
    for job in latest.values():
        counts[job_outcome(job, job['job_id'] in coordinating)] += 1
    return counts


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
    # 受理时冻结在任务上的策略；此前受理的任务没有这一项，按缺省 wait。
    unverified_policy = (job.get('model_policy') or {}).get('unverified_nodes', 'wait')
    max_age = config.get('catalog_max_age_s', p.DEFAULTS['catalog_max_age_s'])
    # 已退役的节点只保留历史引用：不参与调度、解释和同主机容量的计算。
    nodes = [n for n in nodes if not n.get('retired_at')]
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
        if node.get('maintenance'):
            reasons.append('maintenance')
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
        catalog = codex_catalog(node) if harness == 'codex' else None
        state = catalog_state(catalog, now, max_age) if catalog else None
        unverified = False
        if state in VERIFIED_CATALOG:
            # §12.5：已核实的目录明确不含该组合就排除。
            model, effort = (job['contract']['harness_params'].get(k) for k in ('model', 'effort'))
            # 目录可能把同一模型列成多条：合并它们的 effort，与 selectable_codex 的口径一致。
            entries = [m for m in catalog['models'] if m['id'] == model]
            if not entries:
                reasons.append('model_unsupported:%s' % model)
            elif not any(effort in m['efforts'] for m in entries):
                reasons.append('effort_unsupported:%s/%s' % (model, effort))
        elif state is not None:
            # 目录未核实不等于不支持，也不等于支持：缺省不派单；任务受理时冻结的策略明确允许时才作为“未核实能力尝试”。
            unverified = True
            if unverified_policy != 'allow':
                reasons.append('model_catalog_unverified:' + state)
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
                       'capability_evidence': capability_evidence(node),
                       **({'model_catalog': state, 'unverified_attempt': unverified} if catalog else {})})
    # 目录已核实含该组合的节点总是排在未核实的节点之前；同一类里再按分数。
    eligible = sorted((row for row in result if row['eligible']),
                      key=lambda r: (bool(r.get('unverified_attempt')), -r['score'], r['node_id']))
    return {'nodes': result, 'selected_node_id': eligible[0]['node_id'] if eligible else None,
            'policy': {'unverified_nodes': unverified_policy if job['contract']['harness'] == 'codex' else None}}
