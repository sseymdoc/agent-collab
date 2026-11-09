"""共享协议常量与校验；controller 与 node agent 共同依赖。

单一负责人：Claude 主控。worker 不修改本文件；需要变更时在报告中提出。
仅使用标准库，兼容 Python 3.9。
"""
import hashlib
import json
import re
import unicodedata
import uuid

PROTOCOL_VERSION = 1
API_PREFIX = '/api/v1'

JOB_STATUSES = (
    'queued',              # 已受理，等待调度
    'waiting_capability',  # 无合格节点；status_reason 说明缺什么
    'running',             # 当前 attempt 已领取或执行中
    'recovering',          # 当前 attempt 失联/失败可恢复，等待新 attempt
    'waiting_reconcile',   # 不满足自动恢复条件，需人工对账
    'cancel_requested',    # 用户已请求取消，尚未确认停止
    'cancelled',           # 停止已确认，或从未开始
    'completed',           # 最新 attempt 正常结束，结果待验收/发布
    'failed',              # 确定性失败或预算耗尽
)
JOB_FINAL = ('cancelled', 'failed')

ATTEMPT_STATUSES = (
    'assigned',   # 调度器已选定节点，等待领取
    'leased',     # 节点已领取，持有租约
    'running',    # 节点报告已启动
    'succeeded', 'failed', 'cancelled',
    'lost',       # 租约过期且未确认结束
    'superseded', # 已被更高 epoch 取代；不得发布
)
ATTEMPT_ACTIVE = ('assigned', 'leased', 'running')

PROTECTION = ('unprotected', 'preparing', 'protected')
ACCEPTANCE = ('unverified', 'passed', 'rework', 'blocked')
INTEGRATION = ('none', 'integration_pending', 'integrated', 'conflict')
CHECKPOINT_STATES = ('uploading', 'durable', 'verified')
NODE_STATUSES = ('online', 'suspect', 'unreachable')

FAILURE_CLASSES = (
    'node_lost', 'network', 'infra_retryable',  # 可自动恢复
    'auth_expired', 'quota', 'capability_missing',  # 等待外部条件，不换机重试
    'user_cancel',  # 不自动恢复
    'code_failure',  # 确定性失败，不换节点重试
)
RECOVERABLE_FAILURES = ('node_lost', 'network', 'infra_retryable')

SIDE_EFFECTS = ('none', 'idempotent', 'manual')
HARNESSES = ('stub', 'codex')
OS_NAMES = ('windows', 'linux', 'macos')

NODE_EVENT_TYPES = ('attempt.started', 'attempt.progress', 'attempt.log', 'attempt.side_effect')

DEFAULTS = {
    'heartbeat_s': 5,
    'suspect_after_s': 30,
    'lease_ttl_s': 90,
    'assign_claim_timeout_s': 60,
    'quota_block_s': 900,
    'max_blob_bytes': 64 * 1024 * 1024,
    'max_json_bytes': 2 * 1024 * 1024,
    'sse_ping_s': 15,
}

_HEX64 = re.compile(r'[0-9a-f]{64}')
_NODE_ID = re.compile(r'[a-z0-9][a-z0-9-]{0,62}')


def check_uuid(value, name='id'):
    if not isinstance(value, str):
        raise ValueError(name + ' 必须是小写 UUID 字符串')
    try:
        ok = str(uuid.UUID(value)) == value
    except ValueError:
        ok = False
    if not ok:
        raise ValueError(name + ' 必须是小写 UUID 字符串')
    return value


def check_node_id(value):
    if not isinstance(value, str) or not _NODE_ID.fullmatch(value):
        raise ValueError('node_id 只允许小写字母、数字和连字符，最长 63')
    return value


def check_sha256(value, name='sha256'):
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ValueError(name + ' 必须是 64 位小写十六进制')
    return value


def canonical_json(value):
    """哈希与幂等比较使用的唯一序列化。"""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def value_hash(value):
    return sha256_bytes(canonical_json(value))


def check_rel_path(path):
    """manifest 路径：POSIX 相对路径，NFC，禁止绝对路径、盘符、反斜杠、.. 与空段。"""
    if not isinstance(path, str) or not path or len(path) > 1024:
        raise ValueError('路径必须是非空字符串')
    if unicodedata.normalize('NFC', path) != path:
        raise ValueError('路径必须为 NFC 规范形式：' + path)
    if '\\' in path or '\x00' in path or path.startswith('/') or re.match(r'^[A-Za-z]:', path):
        raise ValueError('路径必须是 POSIX 相对路径：' + path)
    parts = path.split('/')
    if any(p in ('', '.', '..') for p in parts):
        raise ValueError('路径含空段、. 或 ..：' + path)
    return path


def case_collisions(paths):
    """返回大小写折叠后冲突的路径组；Windows/macOS 默认文件系统不能同时容纳它们。"""
    groups = {}
    for p in paths:
        groups.setdefault(p.casefold(), []).append(p)
    return sorted(sorted(v) for v in groups.values() if len(v) > 1)


def validate_manifest(manifest):
    """检查 manifest 结构；不检查 blob 是否存在。返回规范化副本。

    {"version": 1, "base": <manifest_sha256|null>,
     "files": [{"path", "sha256", "size", "executable"}], "deleted": [path, ...]}
    files 是完整文件集合；deleted 是相对 base 删除的路径（base 为 null 时必须为空）。
    """
    if not isinstance(manifest, dict) or set(manifest) != {'version', 'base', 'files', 'deleted'}:
        raise ValueError('manifest 需要且只允许 version/base/files/deleted')
    if manifest['version'] != 1:
        raise ValueError('manifest.version 必须为 1')
    if manifest['base'] is not None:
        check_sha256(manifest['base'], 'manifest.base')
    if not isinstance(manifest['files'], list) or not isinstance(manifest['deleted'], list):
        raise ValueError('files/deleted 必须是列表')
    files = []
    for f in manifest['files']:
        if not isinstance(f, dict) or set(f) != {'path', 'sha256', 'size', 'executable'}:
            raise ValueError('files 项需要且只允许 path/sha256/size/executable')
        check_rel_path(f['path'])
        check_sha256(f['sha256'])
        if type(f['size']) is not int or f['size'] < 0:
            raise ValueError('size 必须是非负整数')
        if type(f['executable']) is not bool:
            raise ValueError('executable 必须是布尔值')
        files.append(dict(f))
    paths = [f['path'] for f in files]
    if len(set(paths)) != len(paths):
        raise ValueError('files 路径重复')
    deleted = [check_rel_path(p) for p in manifest['deleted']]
    if manifest['base'] is None and deleted:
        raise ValueError('无 base 的 manifest 不能有 deleted')
    if len(set(deleted)) != len(deleted) or set(deleted) & set(paths):
        raise ValueError('deleted 重复或与 files 重叠')
    for parent in paths:
        if any(p.startswith(parent + '/') for p in paths):
            raise ValueError('路径既是文件又是目录：' + parent)
    files.sort(key=lambda f: f['path'])
    return {'version': 1, 'base': manifest['base'], 'files': files, 'deleted': sorted(deleted)}


def manifest_sha256(manifest):
    return value_hash(validate_manifest(manifest))


def manifest_diff(old, new):
    """old/new 为已校验 manifest 或 None；返回 added/modified/deleted 路径列表。"""
    a = {f['path']: f for f in (old or {}).get('files', [])}
    b = {f['path']: f for f in new['files']}
    return {
        'added': sorted(set(b) - set(a)),
        'modified': sorted(p for p in set(a) & set(b) if (a[p]['sha256'], a[p]['executable']) != (b[p]['sha256'], b[p]['executable'])),
        'deleted': sorted(set(a) - set(b)),
    }


def error_body(code, message, **details):
    return {'error': {'code': code, 'message': message, 'details': details}}
