"""codex-collaborate 旧任务的只读投影。

旧任务继续由原 launcher 管理；这里只读取其 state/report/assessment，带来源与版本标记返回，
从不写入、不重算请求哈希、不因记录为 running 而接管。用法：

    python -m agent_collab.legacy list --state-root C:\\Users\\<user>\\.claude\\codex-collaborate
"""
import argparse
import json
from pathlib import Path

SOURCE = 'codex-collaborate'
_STATE_KEYS = ('task_id', 'worker', 'status', 'session_id', 'work_package_id', 'request_hash',
               'created_at', 'started_at', 'ended_at', 'cwd', 'sandbox', 'model', 'effort', 'codex_version')


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None


def project(task_dir):
    d = Path(task_dir)
    state = _read(d / 'state.json')
    if not isinstance(state, dict) or 'task_id' not in state:
        return None
    report = _read(d / 'report.json') or {}
    assessment = _read(d / 'assessment.json') or {}
    p = {k: state.get(k) for k in _STATE_KEYS}
    p['work_package_id'] = p['work_package_id'] or p['task_id']
    p.update(
        source=SOURCE, read_only=True, authority='legacy_runner',
        # 记录中的 running 只是原 runner 的登记；进程是否存活须由原 launcher 的 status/reconcile 判断。
        recorded_status=state.get('status'), liveness='check_with_original_launcher',
        workflow_version=state.get('workflow_version'),
        report_outcome=report.get('outcome'), report_status=report.get('status'),
        recorded_acceptance=assessment.get('status', 'unverified'),
        acceptance_note='产物版本未在此重新校验；stale 判断以原 launcher summary 为准',
        observed={k: (state.get('observed') or {}).get(k) for k in ('model', 'effort', 'reasoning_effort')},
        pointer=str(d),
    )
    return p


def read_tasks(state_root):
    root = Path(state_root) / 'tasks'
    out = []
    for d in sorted(root.glob('*/*')):
        p = project(d) if d.is_dir() else None
        if p:
            out.append(p)
    out.sort(key=lambda p: p.get('created_at') or '')
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description='只读列出 codex-collaborate 旧任务')
    ap.add_argument('action', choices=['list'])
    ap.add_argument('--state-root', required=True)
    a = ap.parse_args(argv)
    print(json.dumps(read_tasks(a.state_root), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
