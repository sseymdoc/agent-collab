"""agent-collab project ...：项目、本机目录绑定、输入推送与成果应用（docs/protocol.md §13）。

    agent-collab project list | show ID
    agent-collab project create --name 名称 [--model M --effort E] [--unverified-nodes wait|allow] [--exclude 模式 ...]
    agent-collab project set ID [--name 名称] [--model M --effort E | --no-model] [--unverified-nodes wait|allow|inherit] [--exclude 模式 ... | --no-exclude]
    agent-collab project archive ID | restore ID
    agent-collab project bind ID 目录 | unbind ID | bindings        # 这台机器上项目对应哪个目录；只保存在本机
    agent-collab project scan 目录 | --project ID [--exclude 模式 ...] # 只看将上传的范围，不上传
    agent-collab project push ID [--exclude 模式 ...]                # 扫描绑定目录，上传缺的内容，登记为项目的输入
    agent-collab project push ID --resume 请求ID                     # 原样重放一次没有得到答复的登记（不重新扫描）
    agent-collab project plan 任务ID [--dir 目录]                    # 成果相对本机目录的变更与冲突，不写入
    agent-collab project apply 任务ID [--dir 目录] [--skip-conflicts]
    agent-collab project recover [应用ID] [--resume | --rollback | --abandon | --report]
    # plan、apply、recover 加 --node 节点：远端写回（§13.10），经该节点的受限操作写回它本机绑定的目录，这台机器不读写项目文件

`project local ...` 是给桌面端外壳调用的本机助手：只做本机文件工作，不访问网络，输入输出都是 JSON 行。
规则与上面的命令共用 agent_collab/project.py。
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import sys
import time
import uuid

from . import cli, project as local, protocol as p


def state_dir():
    return cli.client_home() / 'projects'


def bindings_path():
    return cli.client_home() / 'projects.json'


def load_bindings():
    try:
        return json.loads(bindings_path().read_text(encoding='utf-8'))
    except OSError:
        return {}


def bound_root(conn, project_id, explicit=None):
    """项目在这台机器上的目录：命令行给出的，或此前 bind 记下的。绑定按 controller 分开，不同设备各有各的。"""
    if explicit:
        return str(local.resolve_root(explicit))
    entry = load_bindings().get(conn.controller_id or conn.url, {}).get(project_id)
    if not entry:
        cli.fail('not_bound', '这台机器上还没有为项目 %s 指定目录：agent-collab project bind %s <目录>，或用 --dir 指定' % (project_id, project_id), 2)
    return str(local.resolve_root(entry['path']))


def stable_request_id(*parts):
    """由内容派生的幂等键：同一份内容重复登记得到同一个 request_id，不会产生第二个业务对象。

    各段必须覆盖请求体里所有会变的东西：同一个 request_id 配不同的请求体会被 controller 以 409 拒绝。
    """
    return str(uuid.UUID(bytes=hashlib.sha256('\n'.join(parts).encode('utf-8')).digest()[:16], version=4))


def defaults_from(a, current=None):
    """把命令行选项合成项目默认策略；没给的项保持 current 里的值。"""
    value = json.loads(json.dumps(current)) if current else {'codex': {'model': None, 'effort': None, 'unverified_nodes': None}, 'exclude': None}
    if getattr(a, 'no_model', False):
        value['codex'].update(model=None, effort=None)
    if a.model is not None or a.effort is not None:
        value['codex'].update(model=a.model, effort=a.effort)
    if a.unverified_nodes is not None:
        value['codex']['unverified_nodes'] = None if a.unverified_nodes == 'inherit' else a.unverified_nodes
    if getattr(a, 'no_exclude', False):
        value['exclude'] = None
    elif a.exclude:
        value['exclude'] = a.exclude
    return value


def effective_excludes(record, explicit=None):
    """扫描项目目录时用的排除规则：命令行明确给出的列表原样使用；否则是项目的排除规则加上缺省规则
    （版本库元数据、依赖与构建缓存、常见凭据文件）。"""
    if explicit:
        return list(explicit)
    return list(dict.fromkeys(list(record['defaults']['exclude'] or []) + local.PROJECT_EXCLUDES))


def describe(result):
    """扫描结果里给人看的部分：将上传的范围、排除与跳过的条目。不含文件清单本身。"""
    return {k: result[k] for k in ('root', 'files', 'bytes', 'manifest_bytes', 'manifest_sha256', 'exclude', 'excluded', 'skipped',
                                    'skipped_counts', 'git', 'scanned_at')}


def push(conn, record, root, exclude=None, progress=None):
    """扫描目录、上传 controller 还没有的内容、登记为项目的输入基线。可以重复运行：已有的内容不再上传，内容没变不新建基线。"""
    limits = conn.call('GET', '/client')['controller'].get('limits', {})
    previous = None
    if os.name == 'nt' and record.get('baseline'):
        previous = json.loads(conn.blob(record['baseline']['manifest_sha256']))  # Windows 看不到可执行位：沿用上一份输入的标记
    result = local.scan(root, effective_excludes(record, exclude), max_file_bytes=limits.get('max_blob_bytes'),
                        previous=previous, progress=progress)
    if limits.get('max_json_bytes') and result['manifest_bytes'] > limits['max_json_bytes'] - 65536:
        cli.fail('manifest_too_large', '文件清单有 %d 个文件（%d 字节），超过这台 controller 单次请求的上限；增加排除规则后重试'
                 % (result['files'], result['manifest_bytes']), 2)
    files = result['manifest']['files']
    digests = sorted({f['sha256'] for f in files})
    missing = set()
    for start in range(0, len(digests), 10000):
        missing.update(conn.call('POST', '/blobs/missing', {'sha256': digests[start:start + 10000]})['missing'])
    changed, uploaded, sent = [], 0, set()
    for entry in files:
        if entry['sha256'] not in missing or entry['sha256'] in sent:
            continue
        try:
            data = local.read_verified(result, entry)
        except local.ProjectError as exc:
            if exc.code not in ('source_changed', 'unsafe_path'):
                raise
            changed.append(entry['path'])
            continue
        status, _ = conn.raw('PUT', '/blobs/' + entry['sha256'], data)
        if status not in (200, 201):
            cli.fail('upload_failed', '上传 %s 失败（HTTP %d）；已上传的内容保留，重新运行会从缺的地方继续' % (entry['path'], status), 2)
        sent.add(entry['sha256'])
        uploaded += entry['size']
        if progress:
            progress({'phase': 'upload', 'done': len(sent), 'total': len(missing), 'bytes': uploaded})
    if changed:
        # 扫描之后源文件变了：这份快照已不能代表目录，不登记为输入；已上传的内容留着，重新运行即可。
        cli.fail('source_changed', '这些文件在扫描后被修改，没有登记输入：%s；重新运行 push' % '、'.join(changed[:20]), 5)
    summary = dict(describe(result), uploaded_blobs=len(sent), uploaded_bytes=uploaded)
    if record.get('baseline') and record['baseline']['manifest_sha256'] == result['manifest_sha256']:
        return dict(summary, baseline_id=record['baseline']['baseline_id'], reused=True)
    source = {'device': platform.node(), 'platform': sys.platform, 'root_name': Path(result['root']).name, 'git': result['git'],
              'exclude': result['exclude'], 'excluded': result['excluded']['count'], 'skipped': result['skipped_counts'],
              'scanned_at': result['scanned_at']}
    # 一次扫描就是一次登记操作，有自己的操作身份（scan_id，桌面端是快照 ID）：request_id 由它派生，不由内容或扫描时间派生
    # （扫描时间只精确到秒，内容相同的两次扫描也是两次登记）。请求体在发出之前连同 request_id 存为回执，应答丢失或进程中断后
    # `project push <ID> --resume <request_id>` 原样重放，得到同一份基线；不重新扫描、不重新取时间、不重建名称。
    # 用户再运行一次 push 是新的扫描、新的登记：内容回到以前登记过的样子时也是新的基线，文件内容按哈希复用。
    scan_id = str(uuid.uuid4())
    body = {'request_id': stable_request_id('baseline', record['project_id'], scan_id),
            'name': '%s @ %s' % (record['name'], result['scanned_at']), 'manifest': result['manifest'],
            'project_id': record['project_id'], 'source': source}
    status, baseline = register_baseline(conn, body)
    return dict(summary, baseline_id=baseline['baseline_id'], reused=False, missing_blobs=baseline['missing_blobs'],
                request_id=body['request_id'])


def register_baseline(conn, body, resumed=False):
    resume = 'agent-collab project push %s --resume %s' % (body['project_id'], body['request_id'])
    status, answer = cli.send_with_receipt(conn, '/baselines', body, resume, 'baseline', resumed, what='这次输入登记')
    if not 200 <= status < 300:
        cli.out({'http_status': status, 'body': answer})
        raise SystemExit(cli.rejected(status))
    return status, answer


def job_result(conn, job_id):
    """已发布成果的应用所需的三样：任务、输入基线 manifest、成果 manifest。"""
    job = conn.call('GET', '/jobs/' + job_id)
    if not job.get('project_id'):
        cli.fail('no_project', '这个任务不属于任何项目', 2)
    if not job['delivery_checkpoint_id']:
        cli.fail('not_published', '这个任务还没有已发布的成果；只有已发布的成果可以应用到项目', 2)
    files = conn.call('GET', '/checkpoints/%s/files' % job['delivery_checkpoint_id'])['files']
    result = {'version': 1, 'base': None, 'files': files, 'deleted': []}
    base = job['contract']['inputs_manifest'] or {'version': 1, 'base': None, 'files': [], 'deleted': []}
    return job, dict(base, base=None, deleted=[]), result


def report_path(application_id):
    """应用结果待向 controller 上报的回执：本机已有最终结果，controller 还没确认。"""
    return state_dir() / 'applies' / p.check_uuid(application_id, 'application_id') / 'report.json'


def queue_report(conn, project_id, application_id, result):
    """把要上报的最终结果连同固定的 request_id 先存在本机，再发送：送不到时按同一个 request_id 补报，controller 只记一次。"""
    from .artifacts import atomic_write
    # 备份目录（result['backup']）是本机绝对路径，只在本机结果里给出，不上报：controller 不保存任何设备上的路径。
    detail = {k: result.get(k) for k in ('changed', 'already', 'pending', 'conflicts', 'errors', 'restored', 'kept',
                                         'written', 'remaining', 'diverged') if result.get(k) is not None}
    if result.get('cancelled'):
        detail['cancelled'] = True
    status = {'rolled_back': 'failed'}.get(result['status'], result['status'])
    if result['status'] == 'rolled_back':
        detail['rolled_back'] = True
    record = {'application_id': application_id, 'project_id': project_id, 'controller': conn.controller_id or conn.url,
              'url': conn.url, 'request_id': str(uuid.uuid4()), 'status': status, 'detail': detail, 'created_at': local.now()}
    atomic_write(report_path(application_id), json.dumps(record, ensure_ascii=False).encode('utf-8'))
    return record


def send_report(conn, record):
    """发送一份待上报的结果。返回 controller 记下的状态；送不到（不可达、凭据被拒）返回 None，回执保留待补报。"""
    if record['controller'] != (conn.controller_id or conn.url):
        return None  # 只报给发起这次应用的 controller，不改用别的连接
    try:
        status, body = conn.send('POST', '/projects/%s/applications/%s' % (record['project_id'], record['application_id']),
                                 {'request_id': record['request_id'], 'status': record['status'], 'detail': record['detail']})
    except cli.Unreachable:
        return None
    if status in (401, 403):
        return None  # 授权问题：处理之后用同一份回执补报，不改用别的身份
    report_path(record['application_id']).unlink()
    if 200 <= status < 300:
        return body['status']
    return 'not_recorded (HTTP %d)' % status  # 明确拒绝（例如记录已有最终结果）：重放不会改变它


def unreported():
    found = []
    for path in sorted((state_dir() / 'applies').glob('*/report.json')):
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        found.append({k: record.get(k) for k in ('application_id', 'project_id', 'url', 'status', 'created_at')})
    return found


def finish_application(conn, project_id, application_id, result):
    """把应用的最终结果如实报给 controller；只有全部写完才是 applied。送不到时返回 None，回执留待补报。"""
    return send_report(conn, queue_report(conn, project_id, application_id, result))


def _manage(a, conn):
    if a.action == 'list':
        cli.out(conn.call('GET', '/projects'))
    elif a.action == 'show':
        cli.out(conn.call('GET', '/projects/' + a.project))
    elif a.action == 'create':
        cli.out(conn.call('POST', '/projects', {'name': a.name, 'defaults': defaults_from(a)}))
    else:
        record = conn.call('GET', '/projects/' + a.project)
        body = {'expected_revision': record['revision']}  # 期间别人改过则被拒绝并返回当前值
        if a.action == 'set':
            body.update(defaults=defaults_from(a, record['defaults']), **({'name': a.name} if a.name else {}))
        else:
            body['archived'] = a.action == 'archive'
        cli.out(conn.call('PUT', '/projects/' + a.project, body))
    return 0


def _binding(a, conn):
    from .artifacts import atomic_write
    data, key = load_bindings(), conn.controller_id or conn.url
    mine = data.setdefault(key, {})
    if a.action == 'bind':
        conn.call('GET', '/projects/' + a.project)  # 确认项目存在
        mine[a.project] = {'path': str(local.resolve_root(a.directory)), 'bound_at': local.now()}
    elif a.action == 'unbind':
        mine.pop(a.project, None)
    if a.action != 'bindings':
        atomic_write(bindings_path(), json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))
    cli.out({'bindings': mine, 'file': str(bindings_path())})
    return 0


def _apply(a, conn):
    job, base, result = job_result(conn, a.job_id)
    root = bound_root(conn, job['project_id'], a.dir)
    exclude = job['contract'].get('workspace', {}).get('exclude')
    if a.action == 'plan':
        cli.out(dict(local.plan(root, base, result, exclude), job_id=job['job_id'], checkpoint_id=job['delivery_checkpoint_id']))
        return 0
    application_id, registered = str(uuid.uuid4()), []

    def started(outline):
        # 第一次写入之前登记意图；登记不上就不写。之后即使本进程被打断，controller 上的记录也停在 started，不会显示为已应用。
        conn.call('POST', '/projects/%s/applications' % job['project_id'], {
            'application_id': application_id, 'job_id': job['job_id'], 'checkpoint_id': job['delivery_checkpoint_id'],
            'device': platform.node() or 'cli', 'plan': outline['summary']})
        registered.append(application_id)
        from .artifacts import atomic_write
        atomic_write(state_dir() / 'applies' / application_id / 'context.json', json.dumps(
            {'project_id': job['project_id'], 'job_id': job['job_id'], 'controller': conn.controller_id or conn.url}).encode('utf-8'))
    stop = []

    def interrupted(signum, frame):
        # 第一次 Ctrl+C 是取消请求：正在写的文件写完后停下，结果如实记为部分完成；第二次才直接中断（之后用 recover 处理）。
        if stop:
            raise KeyboardInterrupt
        stop.append(True)
        print('已请求取消：当前文件写完后停止（再按一次 Ctrl+C 立即中断）', file=sys.stderr, flush=True)
    previous = signal.signal(signal.SIGINT, interrupted)
    try:
        done = local.apply(root, base, result, conn.blob, state_dir(), application_id, exclude,
                           skip_conflicts=a.skip_conflicts, cancelled=lambda: bool(stop), before_write=started)
    except local.ProjectError as exc:
        if registered:  # 登记之后、写入之前被取消：一个文件都没写
            finish_application(conn, job['project_id'], application_id, {'status': 'failed', 'errors': [{'reason': exc.code, 'error': exc.message}]})
        cli.fail(exc.code, exc.message, 5 if exc.code == 'unfinished_application' else 2)
    finally:
        signal.signal(signal.SIGINT, previous)
    if registered:
        # 文件已经按 done 里的结果写了；送不到 controller 时结果留在本机回执里，那边的记录停在 started，不会显示为已应用。
        done['reported'] = finish_application(conn, job['project_id'], application_id, done)
        if done['reported'] is None:
            done['report_pending'] = 'agent-collab project recover %s --report' % application_id
    cli.out(done)
    return 0 if done['status'] == 'applied' else 5


def _recover(a, conn):
    pending = local.unfinished(state_dir())
    if not a.application:
        # unfinished：写入中途中断、还没有本机结果的；unreported：本机已有结果、还没得到 controller 确认的。
        cli.out({'unfinished': pending, 'unreported': unreported()})
        return 0
    if a.report:
        try:
            record = json.loads(report_path(a.application).read_text(encoding='utf-8'))
        except OSError:
            cli.fail('no_report', '这次应用没有待上报的结果', 2)
        if record['controller'] != (conn.controller_id or conn.url):
            cli.fail('controller_mismatch', '这次应用是在另一个 controller（%s）上登记的；用配对到它的 --profile 补报' % record['url'], 2)
        reported = send_report(conn, record)
        cli.out({'application_id': a.application, 'status': record['status'], 'reported': reported})
        return 0 if reported else 3
    action = 'resume' if a.resume else 'rollback' if a.rollback else 'abandon' if a.abandon else 'status'
    try:
        report = local.recover(state_dir(), a.application, action, fetch=conn.blob if action == 'resume' else None)
    except local.ProjectError as exc:
        cli.fail(exc.code, exc.message, 2)
    if action != 'status':
        try:
            context = json.loads((state_dir() / 'applies' / a.application / 'context.json').read_text(encoding='utf-8'))
        except OSError:
            context = None
        if context:
            # 已有最终结果的记录不会被改写：重复上报得到 409，照常返回本机结果。送不到时回执留待 --report 补报。
            report['reported'] = finish_application(conn, context['project_id'], a.application, report)
    cli.out(report)
    return 0 if report['status'] in ('applied', 'rolled_back') or action == 'status' else 5


# ---------------------------------------------------------------- 远端项目写回（docs/protocol.md §13.10）
# 成果经目标节点的受限操作写回该节点上绑定的目录：这台机器不读写任何项目文件，只登记请求、等节点的结果。
# controller 按任务记录填写全部参数；这里只指名任务与节点，并带上读到的检查点与输入基线（与任务当前不一致时被拒绝）。

OP_DONE = ('succeeded', 'failed', 'timed_out')


def wait_operation(conn, op_id, timeout):
    """轮询管理操作直到有结果或到期；controller 暂时不可达时重试。返回最后一次读到的记录。"""
    deadline = time.monotonic() + timeout
    while True:
        op = cli.retrying(deadline, lambda: conn.call('GET', '/operations/' + op_id))
        if op['status'] in OP_DONE or time.monotonic() >= deadline:
            return op
        time.sleep(1)


def operation_brief(op):
    return {k: op.get(k) for k in ('op_id', 'kind', 'node_id', 'status', 'received_at', 'finished_at', 'deadline_at', 'error')}


def _remote(a, conn):
    job = conn.call('GET', '/jobs/' + a.job_id)
    if not job.get('project_id'):
        cli.fail('no_project', '这个任务不属于任何项目', 2)
    if not job['delivery_checkpoint_id']:
        cli.fail('not_published', '这个任务还没有已发布的成果；只有已发布的成果可以应用到项目', 2)
    project_id, deadline = job['project_id'], time.monotonic() + a.timeout
    if a.action == 'plan':
        body = {'request_id': str(uuid.uuid4()), 'job_id': job['job_id'], 'node_id': a.node}
        op = wait_operation(conn, cli.retrying(deadline, lambda: conn.call('POST', '/projects/%s/plans' % project_id, body))['op_id'], a.timeout)
        if op['status'] == 'succeeded':
            cli.out(dict(op['result'], job_id=job['job_id'], checkpoint_id=op['params']['checkpoint_id'], node_id=a.node, op_id=op['op_id']))
            return 0
        if op['status'] == 'failed':
            cli.fail((op['error'] or 'failed').split(':', 1)[0], '节点没能生成计划：%s' % op['error'], 2)
        cli.out({'operation': operation_brief(op)})
        return 4  # 节点在时限内没有给出结果（不在线、或还在执行）
    application_id = str(uuid.uuid4())
    # request_id 由应用 ID 派生：应答丢失后原样重发得到同一条记录，不会登记第二次、也不会让节点写第二次。
    body = {'request_id': stable_request_id('remote-apply', application_id), 'application_id': application_id, 'job_id': job['job_id'],
            'checkpoint_id': job['delivery_checkpoint_id'], 'node_id': a.node,
            'expected_base_manifest_sha256': job['inputs_manifest_sha256'], 'skip_conflicts': a.skip_conflicts}
    record = cli.retrying(deadline, lambda: conn.call('POST', '/projects/%s/applications' % project_id, body))
    return _remote_outcome(conn, record, wait_operation(conn, record['operation_id'], a.timeout))


def _remote_outcome(conn, record, op, report=None):
    """应用记录是权威状态；操作只说明节点有没有收到、有没有给出结果。"""
    path = '/projects/%s/applications/%s' % (record['project_id'], record['application_id'])
    record = cli.retrying(time.monotonic() + 60, lambda: conn.call('GET', path))
    settled = record['status'] != 'started'
    value = {'status': record['status'], 'application': record, 'operation': operation_brief(op)}
    if report is not None:
        value['report'] = report
    if not settled and op['status'] in OP_DONE:
        # 节点收到了但没在时限内给出结果，或执行中出错且无法确定写到了哪里：不是失败也不是成功。
        value['pending_confirmation'] = True
    cli.out(value)
    if not settled and op['status'] not in OP_DONE:
        return 4
    return 0 if record['status'] == 'applied' or (record.get('detail') or {}).get('rolled_back') else 5


def find_application(conn, application_id):
    for item in conn.call('GET', '/projects')['projects']:
        status, body = conn.send('GET', '/projects/%s/applications/%s' % (item['project_id'], application_id))
        if status == 200:
            return body
    cli.fail('not_found', '没有这次应用：%s' % application_id, 2)


def _remote_recover(a, conn):
    if not a.application:
        pending = [x for item in conn.call('GET', '/projects')['projects']
                   for x in conn.call('GET', '/projects/%s/applications' % item['project_id'])['applications']
                   if x.get('target_node_id') == a.node and x['status'] == 'started']
        cli.out({'unfinished': pending})
        return 0
    record = find_application(conn, a.application)
    if record.get('target_node_id') != a.node:
        cli.fail('wrong_node', '这次应用的目标节点是 %s，不是 %s' % (record.get('target_node_id') or '本机（不是远端写回）', a.node), 2)
    action = 'resume' if a.resume else 'rollback' if a.rollback else 'abandon' if a.abandon else 'status'
    deadline, body = time.monotonic() + a.timeout, {'request_id': str(uuid.uuid4()), 'action': action}
    op = cli.retrying(deadline, lambda: conn.call('POST', '/projects/%s/applications/%s/recover' % (record['project_id'], a.application), body))
    op = wait_operation(conn, op['op_id'], a.timeout)
    if op['status'] == 'failed' and not (op.get('result') or {}).get('application'):
        cli.fail((op['error'] or 'failed').split(':', 1)[0], '节点没能处理：%s' % op['error'], 2)
    report = (op.get('result') or {}).get('report')
    if action == 'status':
        cli.out({'report': report, 'operation': operation_brief(op)})
        return 0 if op['status'] == 'succeeded' else 4
    return _remote_outcome(conn, record, op, report)


# ---------------------------------------------------------------- 给桌面外壳用的本机助手

def _emit(kind, value):
    print(json.dumps({kind: value}, ensure_ascii=False), flush=True)


def _read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8')) if path else None


def _fault_gate():
    """故障注入（只用于测试）：AGENT_COLLAB_FAULT_APPLY_GATE=<文件> 时，写第 n 个文件（n≥1）之前先创建
    `<文件>.<n>` 表示已到达，然后等到 `<文件>` 不存在再继续（最多等 120 秒）。没有这个变量时不起作用。"""
    gate = os.environ.get('AGENT_COLLAB_FAULT_APPLY_GATE')
    if not gate:
        return None

    def wait(index):
        if index:
            Path('%s.%d' % (gate, index)).touch()
            deadline = time.time() + 120
            while os.path.exists(gate) and time.time() < deadline:
                time.sleep(0.05)
    return wait


def _local(argv):
    """只做本机文件工作，不访问网络、不读写凭据。标准输出逐行是 {"progress": ...}，最后一行是 {"result": ...} 或 {"error": ...}。"""
    ap = argparse.ArgumentParser(prog='agent-collab project local')
    ap.add_argument('action', choices=('scan', 'stage', 'plan', 'apply', 'recover', 'unfinished', 'materialize'))
    ap.add_argument('--root')
    ap.add_argument('--state', required=True)
    for name in ('exclude-file', 'previous', 'snapshot', 'need', 'out', 'base', 'result', 'blobs', 'application', 'locations', 'project',
                 'cancel-file'):
        ap.add_argument('--' + name)
    ap.add_argument('--max-file-bytes', type=int)
    ap.add_argument('--skip-conflicts', action='store_true')
    ap.add_argument('--recover-action', choices=('status', 'resume', 'rollback', 'abandon'), default='status')
    a = ap.parse_args(argv)
    progress = lambda value: _emit('progress', value)
    exclude = _read_json(a.exclude_file)
    # 取消通道：外壳在取消时创建这个文件。扫描、暂存在下一个文件处停下；应用与续写把当前文件写完再停下。
    cancelled = (lambda: os.path.exists(a.cancel_file)) if a.cancel_file else None
    before_file = _fault_gate()

    def staged(digest):
        # 外壳把成果内容下载到暂存目录，文件名就是内容哈希；apply 会再核对一次。
        return (Path(a.blobs) / p.check_sha256(digest)).read_bytes()
    try:
        if a.action == 'scan':
            result = local.scan(a.root, exclude, a.max_file_bytes, _read_json(a.previous), progress, cancelled)
            snapshot_id = local.save_snapshot(a.state, result, a.project)
            value = dict(describe(result), snapshot_id=snapshot_id, manifest=result['manifest'])
        elif a.action == 'stage':
            value = local.stage(local.load_snapshot(a.state, a.snapshot), _read_json(a.need), a.out, progress, cancelled)
        elif a.action == 'plan':
            value = local.plan(a.root, _read_json(a.base), _read_json(a.result), exclude, _read_json(a.locations))
        elif a.action == 'apply':
            value = local.apply(a.root, _read_json(a.base), _read_json(a.result), staged, a.state, a.application, exclude,
                                _read_json(a.locations), a.skip_conflicts, progress, cancelled, before_file=before_file)
        elif a.action == 'recover':
            value = local.recover(a.state, a.application, a.recover_action, staged if a.blobs else None, progress, cancelled)
        elif a.action == 'materialize':
            # 整包下载的落盘：目标目录必须为空或不存在；每个文件核对哈希，路径逐级拒绝链接与越界。
            from .artifacts import materialize
            target = Path(a.out)
            if target.exists() and any(target.iterdir()):
                raise local.ProjectError('target_not_empty', '下载目录必须为空或不存在')
            manifest = materialize(target, _read_json(a.result), staged)
            value = {'path': str(target), 'files': len(manifest['files']), 'bytes': sum(f['size'] for f in manifest['files'])}
        else:
            value = {'unfinished': local.unfinished(a.state, a.root)}
    except local.ProjectError as exc:
        _emit('error', exc.body())
        return 2
    except (OSError, ValueError, KeyError) as exc:
        _emit('error', {'code': 'io' if isinstance(exc, OSError) else 'invalid', 'message': '%s: %s' % (type(exc).__name__, exc), 'detail': {}})
        return 2
    _emit('result', value)
    return 0


def main(argv):
    if argv[:1] == ['local']:
        return _local(argv[1:])
    ap = argparse.ArgumentParser(prog='agent-collab project')
    sub = ap.add_subparsers(dest='action', required=True)

    def command(name, *positional, policy=False):
        parser = sub.add_parser(name)
        for item in positional:
            parser.add_argument(item)
        if policy:
            parser.add_argument('--name', required=name == 'create')
            parser.add_argument('--model')
            parser.add_argument('--effort')
            parser.add_argument('--unverified-nodes', choices=('wait', 'allow') + (('inherit',) if name == 'set' else ()))
            parser.add_argument('--exclude', action='append', metavar='模式')
            if name == 'set':
                parser.add_argument('--no-model', action='store_true', help='不再设项目默认模型，沿用工作区默认值')
                parser.add_argument('--no-exclude', action='store_true', help='不再设项目排除规则')
        cli.connection_args(parser)
        return parser
    command('list')
    command('show', 'project')
    command('create', policy=True)
    command('set', 'project', policy=True)
    command('archive', 'project')
    command('restore', 'project')
    command('bind', 'project', 'directory')
    command('unbind', 'project')
    command('bindings')
    scanning = command('scan')
    scanning.add_argument('directory', nargs='?')
    scanning.add_argument('--project')
    scanning.add_argument('--exclude', action='append', metavar='模式')
    pushing = command('push', 'project')
    pushing.add_argument('--exclude', action='append', metavar='模式', help='替换缺省的排除规则')
    pushing.add_argument('--resume', metavar='REQUEST_ID', help='原样重放一次没有得到答复的输入登记，不重新扫描')
    for name in ('plan', 'apply'):
        parser = command(name, 'job_id')
        parser.add_argument('--dir', help='应用到这个目录，而不是项目在本机绑定的目录')
        if name == 'apply':
            parser.add_argument('--skip-conflicts', action='store_true', help='只写没有冲突的文件；结果记为部分应用')
    recovering = command('recover')
    recovering.add_argument('application', nargs='?')
    choice = recovering.add_mutually_exclusive_group()
    for name in ('resume', 'rollback', 'abandon', 'report'):
        choice.add_argument('--' + name, action='store_true')
    for name in ('plan', 'apply', 'recover'):  # 远端写回（§13.10）：经该节点的受限操作写回它本机绑定的目录
        sub.choices[name].add_argument('--node', help='写回这个节点上为项目绑定的目录（节点本机 agent project-bind）')
        sub.choices[name].add_argument('--timeout', type=float, default=1860, help='等待节点结果的秒数')
    a = ap.parse_args(argv)
    if getattr(a, 'node', None) and getattr(a, 'dir', None):
        cli.fail('invalid', '--node 与 --dir 不能同时使用', 2)
    if a.action == 'scan' and a.directory and not a.project:
        try:  # 只看本机目录：不需要连接 controller
            cli.out(describe(local.scan(a.directory, a.exclude)))
        except local.ProjectError as exc:
            cli.fail(exc.code, exc.message, 2)
        return 0
    conn = cli.Conn(a)
    try:
        if a.action in ('list', 'show', 'create', 'set', 'archive', 'restore'):
            return _manage(a, conn)
        if a.action in ('bind', 'unbind', 'bindings'):
            return _binding(a, conn)
        if a.action == 'push' and a.resume:
            saved = cli.load_receipt(conn, a.resume, 'baseline')
            if saved['body']['project_id'] != a.project:
                cli.fail('invalid', '这次登记属于项目 %s' % saved['body']['project_id'], 2)
            status, baseline = register_baseline(conn, saved['body'], resumed=True)
            # 此前已登记时 controller 按 request_id 返回当时那一份；没有登记过时这一次才登记。两种情况都只有一份基线。
            cli.out({'baseline_id': baseline['baseline_id'], 'request_id': a.resume, 'http_status': status,
                     'missing_blobs': baseline['missing_blobs']})
            return 0
        if a.action in ('scan', 'push'):
            record = conn.call('GET', '/projects/' + a.project)
            root = bound_root(conn, a.project, getattr(a, 'directory', None))
            if a.action == 'scan':
                cli.out(describe(local.scan(root, effective_excludes(record, a.exclude))))
            else:
                cli.out(push(conn, record, root, a.exclude))
            return 0
        if a.action in ('plan', 'apply'):
            return _remote(a, conn) if a.node else _apply(a, conn)
        return _remote_recover(a, conn) if a.node else _recover(a, conn)
    except local.ProjectError as exc:
        cli.fail(exc.code, exc.message, 2)
