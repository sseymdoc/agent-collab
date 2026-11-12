"""节点本机的项目目录绑定与远端项目写回（docs/protocol.md §13.10）。

- 绑定是节点主人在节点本机做的授权（`agent-collab agent project-bind --data-dir D <project_id> <目录>`），只保存在节点数据目录的
  project-bindings.json 里。controller 只知道“节点声明它绑定了项目 P（目录名）”，从不下发也不接收路径。
- controller 下发的 project_plan / project_apply / project_recover 只在这里绑定的目录上调用 project.plan / apply / recover，
  写入规则只有 project.py 那一份；应用日志与备份在节点数据目录的 projects/ 里，成果内容用节点令牌从 controller 取并核对哈希。
- 不重复执行：每项操作在确认领取前把本地状态写进 operations/<op_id>.json，开始执行前记为 running。进程重启后看到 running 的
  操作不再执行，只按本机应用日志补报能确定的结果；结果上报失败时留在本地，之后幂等补报。
- 报给 controller 的结果不含任何绝对路径：目录只给名称，备份目录不上报，错误文本里的本机路径换成目录名。
"""
import json
from pathlib import Path
import shutil
import threading
import time

from . import project, protocol as p
from .artifacts import atomic_write
from .client import APIError, Client

BINDINGS = 'project-bindings.json'
# 报给 controller 的应用明细：只有这些键，值是相对路径与原因。
DETAIL = ('changed', 'already', 'pending', 'conflicts', 'errors', 'restored', 'kept', 'written', 'remaining', 'diverged', 'summary')


def _load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def _bindings(directory):
    data = _load(Path(directory) / BINDINGS, {})
    return data if isinstance(data, dict) else {}


def root_name(path):
    return Path(path).name or 'root'


def _check_roots(root, config):
    """配置了 integration_roots 时，绑定的目录（解析链接后）必须在其中某一个之内（§13.8）。"""
    roots = config.get('integration_roots')
    if roots is not None and not any(root == r or r in root.parents for r in (Path(x).expanduser().resolve() for x in roots)):
        raise project.ProjectError('outside_integration_roots', '目录不在本节点 integration_roots 所列的范围内')


def bind(directory, project_id, folder):
    directory = Path(directory)
    config = json.loads((directory / 'agent.json').read_text(encoding='utf-8'))  # 只在已配对的节点数据目录上绑定
    p.check_uuid(project_id, 'project_id')
    root = project.resolve_root(folder)
    _check_roots(root, config)
    data = _bindings(directory)
    data[project_id] = {'path': str(root), 'bound_at': project.now()}
    atomic_write(directory / BINDINGS, json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))
    return listing(directory)


def unbind(directory, project_id):
    data = _bindings(directory)
    if data.pop(project_id, None) is not None:
        atomic_write(Path(directory) / BINDINGS, json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))
    return listing(directory)


def listing(directory):
    """本机看的绑定（含路径）。"""
    return {'bindings': [dict(b, path=_bindings(directory)[b['project_id']]['path']) for b in public(directory)],
            'file': str(Path(directory) / BINDINGS)}


def public(directory):
    """报给 controller 的绑定：项目 ID 与目录名。手工改坏的条目跳过，不影响心跳。"""
    found = []
    for project_id, entry in _bindings(directory).items():
        try:
            p.check_uuid(project_id, 'project_id')
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get('path'), str):
            found.append({'project_id': project_id, 'root_name': root_name(entry['path'])[:255]})
    return sorted(found, key=lambda b: b['project_id'])


def bound_root(directory, config, project_id):
    """执行时再核对一次绑定：目录仍在、仍是绑定时解析出的那个真实目录、仍在 integration_roots 之内。"""
    entry = _bindings(directory).get(project_id)
    if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
        raise project.ProjectError('not_bound', '这台节点没有为这个项目绑定目录')
    try:
        root = project.resolve_root(entry['path'])
    except project.ProjectError:
        raise project.ProjectError('not_a_directory', '绑定的目录已不存在或不是目录') from None
    if root != Path(entry['path']):
        raise project.ProjectError('binding_changed', '绑定的路径现在指向别的目录（被换成了链接）；在节点上重新绑定')
    _check_roots(root, config)
    return root


def bounded(value, limit):
    """结果超过上限时把最长的列表减半，并记下原来的条数；不把截断的明细当作完整结果（truncated）。"""
    while len(p.canonical_json(value)) > limit:
        lists = [k for k, v in value.items() if isinstance(v, list) and v]
        if not lists:
            break
        key = max(lists, key=lambda k: len(p.canonical_json(value[k])))
        value.setdefault('counts', {}).setdefault(key, len(value[key]))
        value[key] = value[key][:len(value[key]) // 2]
        value['truncated'] = True
    return value


class ProjectOps:
    """节点上的远端写回执行器。主循环在每次心跳后调用 pump()；执行在后台线程里一次一项。"""

    def __init__(self, agent, transport=None):
        self.agent = agent
        self.folder = agent.directory / 'operations'
        self.state_dir = agent.directory / 'projects'
        # 执行线程用自己的连接取成果内容：主循环的连接在等待时会回调 attempt 的本地推进，不能在别的线程里用。
        self.client = Client(agent.config['controller'], agent.client.token, agent.config.get('ca_file'), transport)
        self.client.error_filter = agent.clean
        self.records, self.thread = {}, None
        # ponytail: operations/*.json 不清理（每项几百字节，结果上报后去掉正文）；要回收时按 reported 且早于 N 天删除。
        for path in sorted(self.folder.glob('*.json')):
            record = _load(path, None)
            if not isinstance(record, dict) or record.get('op_id') != path.stem:
                continue
            if record['state'] == 'running':
                # 上一个进程在执行中退出：不再执行，按本机应用日志确定能确定的结果，之后补报。
                record = dict(record, state='finished', outcome=self.interrupted(record))
                self.save(record)
            self.records[record['op_id']] = record

    def save(self, record):
        atomic_write(self.folder / (record['op_id'] + '.json'), p.canonical_json(record))
        self.records[record['op_id']] = record

    def offer(self, op_id, kind, params):
        """心跳下发的操作：先在本地落盘再确认领取。见过的 op_id（含已执行、已放弃的）不再处理。"""
        if op_id not in self.records:
            self.save({'op_id': op_id, 'kind': kind, 'params': params, 'state': 'claimed', 'outcome': None, 'at': time.time()})

    def pump(self):
        """确认领取、按顺序启动执行、补报结果。网络失败向上抛出，下次心跳重来；本地状态不变。"""
        path = '/nodes/%s/operations/' % self.agent.config['node_id']
        for record in sorted(self.records.values(), key=lambda r: r.get('at', 0)):
            if record['state'] == 'claimed':
                try:
                    ack = self.agent.client.json('POST', path + record['op_id'], {'status': 'received'})
                except APIError as exc:
                    if exc.status not in (400, 404, 409):
                        raise
                    ack = None
                # 只有 controller 确认“已领取”才执行；已到期（timed_out）或不存在的放弃，绝不再执行（§13.10）。
                self.save(dict(record, state='queued' if (ack or {}).get('status') == 'received' else 'abandoned'))
        if not (self.thread and self.thread.is_alive()):
            queued = sorted((r for r in self.records.values() if r['state'] == 'queued'), key=lambda r: r.get('at', 0))
            if queued:
                record = dict(queued[0], state='running')
                self.save(record)  # 开始执行之前落盘：重启后看到 running 就不会再执行一次
                self.thread = threading.Thread(target=self.run, args=(record,), name='project-operation', daemon=True)
                self.thread.start()
        for record in list(self.records.values()):
            if record['state'] == 'finished':
                status, result, error = record['outcome']
                try:
                    self.agent.client.json('POST', path + record['op_id'], {'status': status, 'result': result, 'error': error})
                except APIError as exc:
                    if exc.status not in (400, 404, 409, 413):
                        raise
                self.save(dict(record, state='reported', outcome=None))

    def run(self, record):
        stage = self.folder / (record['op_id'] + '.blobs')
        try:
            outcome = self.execute(record['kind'], record['params'], stage)
        except Exception as exc:  # 线程里任何异常都要落成结果，否则这项操作在本进程里永远停在 running
            outcome = ('failed', None, self.describe(exc))
        finally:
            shutil.rmtree(str(stage), ignore_errors=True)
        self.save(dict(record, state='finished', outcome=list(outcome)))

    # ------------------------------------------------------------ 三种操作

    def execute(self, kind, params, stage):
        fetch = lambda sha: self.fetch(sha, stage)
        if kind == 'project_apply':
            return self.apply(params, fetch)
        root = bound_root(self.agent.directory, self.agent.config, params['project_id'])
        if kind == 'project_plan':
            base, result = self.manifests(params, fetch)
            plan = project.plan(root, base, result, params.get('exclude'))
            del plan['root']
            plan['root_name'] = root_name(root)
            if len(p.canonical_json(plan)) > 700000:
                plan['changes'].sort(key=lambda c: c['state'] != 'conflict')  # 要截断时先留冲突
            return 'succeeded', bounded(self.scrub(plan), 700000), None
        if kind == 'project_recover':
            return self.recover(root, params, fetch)
        raise ValueError('unsupported operation')

    def apply(self, params, fetch):
        aid = p.check_uuid(params['application_id'], 'application_id')
        try:
            root = bound_root(self.agent.directory, self.agent.config, params['project_id'])
            base, result = self.manifests(params, fetch)
            done = project.apply(root, base, result, fetch, self.state_dir, aid, params.get('exclude'),
                                 skip_conflicts=params.get('skip_conflicts') is True)
        except Exception as exc:
            error, settled = self.describe(exc), self.journal(aid)
            if settled is False:  # 没有应用日志：第一次写入之前就停下了，一个文件都没写
                code = exc.code if isinstance(exc, project.ProjectError) else type(exc).__name__
                return 'failed', {'application': {'status': 'failed', 'detail': {'reason': code, 'message': error, 'written': []}}}, error
            if settled:
                return 'failed', {'application': settled}, error
            raise  # 写入开始后出错、日志也没有最终结果：结果待确认，交给 recover
        return 'succeeded', {'application': self.application(done)}, None

    def recover(self, root, params, fetch):
        aid, action = p.check_uuid(params['application_id'], 'application_id'), params['action']
        try:
            recorded = json.loads((self.state_dir / 'applies' / aid / 'journal.json').read_text(encoding='utf-8'))
        except FileNotFoundError:
            if action != 'abandon':
                raise project.ProjectError('no_application', '这台节点上没有这次应用的本机记录') from None
            # 按现状结束：本机没有记录，无从核对目录现状，如实记为结果未知。
            return 'succeeded', {'report': {'status': 'unknown'},
                                 'application': {'status': 'unknown', 'detail': {'reason': 'no_local_record'}}}, None
        if Path(recorded['root']) != root:
            raise project.ProjectError('binding_changed', '这次应用写入的目录已不是项目当前绑定的目录')
        report = project.recover(self.state_dir, aid, action, fetch=fetch if action == 'resume' else None)
        if action == 'status':
            return 'succeeded', {'report': bounded(self.scrub({k: v for k, v in report.items() if k not in ('root', 'backup')}), 200000)}, None
        return 'succeeded', {'report': {'status': report['status']}, 'application': self.application(report)}, None

    def interrupted(self, record):
        """上一个进程执行到一半退出时能确定的结果。写入类操作看本机应用日志：没有日志说明一个文件都没写，
        日志已有最终结果就补报它，日志停在 writing 就如实报告未知（应用记录保持 started，等 recover）。"""
        message = 'interrupted: 节点在执行这项操作期间重启，没有再次执行'
        kind, params = record['kind'], record['params']
        try:
            settled = self.journal(p.check_uuid(params.get('application_id'), 'application_id')) if kind != 'project_plan' else None
        except ValueError:
            settled = None
        if kind == 'project_apply' and settled is False:
            return ['failed', {'application': {'status': 'failed', 'detail': {'reason': 'interrupted_before_write', 'written': []}}}, message]
        if settled and params.get('action') != 'status':
            return ['succeeded', {'application': settled}, None]
        return ['failed', None, message]

    # ------------------------------------------------------------ 辅助

    def journal(self, aid):
        """本机应用日志给出的结果：False 没有日志；None 写入中断、结果未知；否则是报给 controller 的应用结果。"""
        try:
            record = json.loads((self.state_dir / 'applies' / aid / 'journal.json').read_text(encoding='utf-8'))
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            return None
        if record.get('status') == 'writing':
            return None
        return self.application(dict(record.get('result') or {}, status=record['status']))

    def application(self, report):
        status = {'rolled_back': 'failed'}.get(report['status'], report['status'])
        detail = {k: report[k] for k in DETAIL if report.get(k) is not None}
        if report['status'] == 'rolled_back':
            detail['rolled_back'] = True
        return {'status': status, 'detail': bounded(self.scrub(detail), 200000)}

    def manifests(self, params, fetch):
        """任务的输入基线与成果 manifest：按 controller 填写的哈希取回并核对，形状与本机应用一致（base=None）。"""
        found = []
        for key in ('base_manifest_sha256', 'manifest_sha256'):
            sha = params.get(key)
            if not sha:
                found.append(None)
                continue
            manifest = p.validate_manifest(json.loads(fetch(sha)))
            if p.manifest_sha256(manifest) != sha:
                raise ValueError('manifest is not canonical')
            found.append(dict(manifest, base=None, deleted=[]))
        if found[1] is None:
            raise ValueError('result manifest is required')
        return found

    def fetch(self, sha, stage):
        """成果内容：节点令牌从 controller 取，核对哈希后暂存到这项操作自己的目录（apply 会取两次：先核对、再写入）。"""
        path = stage / p.check_sha256(sha)
        if path.is_file():
            data = path.read_bytes()
            if p.sha256_bytes(data) == sha:
                return data
        data = self.client.fetch_blob(sha)
        atomic_write(path, data)
        return data

    def scrub(self, value):
        """报出去之前：本机绑定目录换成目录名，节点数据目录换成占位符，再去掉令牌。"""
        pairs = [(entry['path'], root_name(entry['path'])) for entry in _bindings(self.agent.directory).values()
                 if isinstance(entry, dict) and isinstance(entry.get('path'), str) and entry['path']]
        pairs.append((str(self.agent.directory), '<node-data>'))
        pairs.sort(key=lambda pair: -len(pair[0]))

        def walk(item):
            if isinstance(item, str):
                for path, name in pairs:
                    item = item.replace(path, name).replace(path.replace('\\', '/'), name)
                return item
            if isinstance(item, dict):
                return {k: walk(v) for k, v in item.items()}
            if isinstance(item, list):
                return [walk(v) for v in item]
            return item
        return self.agent.clean(walk(value))

    def describe(self, exc):
        text = '%s: %s' % (exc.code, exc.message) if isinstance(exc, project.ProjectError) else '%s: %s' % (type(exc).__name__, exc)
        return self.scrub(text)[:500]
