"""Read-only release inspection and compatibility decisions (no third-party dependencies)."""
import argparse
import json
from pathlib import Path
import sys
import tarfile
import zipfile

from .release import compare_versions, parse_version

FEATURE_EFFECTS = {
    'catalog_provenance': '模型目录来源与过期标记', 'node_admin': '节点管理操作',
    'remote_apply': '远端项目写回', 'projects': '项目管理', 'applications': '成果应用记录',
    'coordination_settings': '协调设置', 'raw_logs': '任务原始日志', 'maintenance': '备份与恢复预检',
}
EXIT_CODES = {'accept': 0, 'degraded': 1, 'reject': 2}


def _features(value):
    if not isinstance(value, (list, tuple)) or any(not isinstance(x, str) or not x for x in value):
        raise ValueError('invalid feature list')
    return set(value)


def evaluate_compat(manifest, controller_info, role='worker', nodes=None):
    """Same decision for the CLI and installer. Missing legacy metadata is explicitly unverified."""
    result = dict(decision='accept', role=role, reasons=[], missing_required=[], missing_optional=[],
                  affected_features={}, unchecked=[])
    reasons, unchecked = result['reasons'], result['unchecked']
    try:
        if role not in ('worker', 'controller'):
            raise ValueError('invalid release role')
        controller = controller_info.get('controller', controller_info)
        version = manifest['version']
        parse_version(version)
        parse_version(controller['version'])
        target_protocol, remote_protocol = manifest.get('protocol_version'), controller.get('protocol_version')
        for value in (target_protocol, remote_protocol):
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError('invalid protocol_version')
        if target_protocol is None or remote_protocol is None:
            unchecked.append('protocol_version 未提供，未核对协议版本')
        elif target_protocol != remote_protocol:
            reasons.append('protocol_version 不同')
        compat, remote_compat = manifest.get('compat'), controller.get('compat')
        if compat is None or remote_compat is None:
            unchecked.append('旧发行清单或 controller 未提供 compat，部分最低版本要求未核对')
        compat, remote_compat = compat or {}, remote_compat or {}
        if not isinstance(compat, dict) or not isinstance(remote_compat, dict):
            raise ValueError('invalid compat declaration')
        available = _features(controller.get('features', []))
        if role == 'worker':
            required = _features(compat.get('worker_requires', []))
            optional = _features(compat.get('worker_optional', []))
            result['missing_required'] = sorted(required - available)
            result['missing_optional'] = sorted(optional - available)
            if result['missing_required']:
                reasons.append('controller 缺必需 features')
            minimum = compat.get('worker_min_controller')
            if minimum and compare_versions(controller['version'], minimum) < 0:
                reasons.append('controller 版本低于 worker_min_controller ' + minimum)
            minimum = remote_compat.get('controller_min_worker')
            if minimum and compare_versions(version, minimum) < 0:
                reasons.append('节点版本低于 controller_min_worker ' + minimum)
        else:
            # GET /client alone cannot prove the fleet's versions; online callers also read GET /nodes.
            nodes = nodes if nodes is not None else controller_info.get('nodes', controller.get('nodes'))
            if isinstance(nodes, dict):
                nodes = nodes['nodes']
            minimum = compat.get('controller_min_worker')
            if nodes is None:
                unchecked.append('未提供 nodes，未核对现有节点的最低版本与要求；先节点后 controller')
            elif not isinstance(nodes, list):
                raise ValueError('invalid nodes list')
            else:
                target_features = _features(manifest.get('features', []))
                for node in nodes:
                    if node.get('retired_at'):
                        continue
                    identity = node.get('capabilities', {}).get('agent', {})
                    node_version = node.get('agent_version') or identity.get('version')
                    label = str(node.get('node_id', 'unknown'))
                    if not node_version:
                        unchecked.append('节点 ' + label + ' 未报告版本')
                    elif minimum and compare_versions(node_version, minimum) < 0:
                        reasons.append('节点 ' + label + ' 低于 controller_min_worker ' + minimum)
                    node_compat = node.get('compat', identity.get('compat'))
                    if node_compat is None:
                        # 节点注册目前只报版本与提交，没有兼容声明：如实列为未核对，不当作“没有要求”。
                        unchecked.append('节点 ' + label + ' 未报告兼容声明，未核对它要求的 features 与最低 controller 版本')
                        node_compat = {}
                    required = _features(node_compat.get('worker_requires', []))
                    if required - target_features:
                        reasons.append('目标 controller 缺少节点 ' + label + ' 必需 features')
                    minimum_controller = node_compat.get('worker_min_controller')
                    if minimum_controller and compare_versions(version, minimum_controller) < 0:
                        reasons.append('目标 controller 低于节点 ' + label + ' 的最低版本')
                    node_protocol = node.get('protocol_version', identity.get('protocol_version'))
                    if node_protocol is None:
                        unchecked.append('节点 ' + label + ' 未报告 protocol_version')
                    elif type(node_protocol) is not int or node_protocol < 1:
                        reasons.append('节点 ' + label + ' 的 protocol_version 无效')
                    elif target_protocol is not None and node_protocol != target_protocol:
                        reasons.append('节点 ' + label + ' protocol_version 不同')
        result['affected_features'] = {name: FEATURE_EFFECTS.get(name, name)
                                       for name in result['missing_optional']}
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        reasons.append('兼容元数据无效: ' + str(exc))
    result['decision'] = 'reject' if reasons else 'degraded' if result['missing_optional'] or unchecked else 'accept'
    return result


def main(argv=None):
    from . import cli
    from .installer import read_release
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    inspect = commands.add_parser('inspect')
    inspect.add_argument('release', type=Path)
    compare = commands.add_parser('compare')
    compare.add_argument('a')
    compare.add_argument('b')
    compat = commands.add_parser('compat')
    compat.add_argument('--release', type=Path, required=True)
    compat.add_argument('--as', dest='role', choices=('worker', 'controller'), default='worker')
    source = compat.add_mutually_exclusive_group(required=True)
    source.add_argument('--controller')
    source.add_argument('--controller-info', type=Path)
    cli.connection_args(compat)
    args = parser.parse_args(argv)
    try:
        if args.command == 'compare':
            result = dict(a=args.a, b=args.b, comparison=compare_versions(args.a, args.b))
        else:
            manifest, _ = read_release(args.release)
            if args.command == 'inspect':
                result = dict(manifest, hashes='verified', source_verified=False)
            else:
                nodes = None
                if args.controller_info:
                    info = json.loads(args.controller_info.read_text(encoding='utf-8'))
                else:
                    if args.url and args.url.rstrip('/') != args.controller.rstrip('/'):
                        raise ValueError('--url and --controller differ')
                    args.url = args.controller
                    try:
                        connection = cli.Conn(args)
                    except SystemExit:
                        # profile 没有凭据或地址与配对的不同：Conn 已经在 stderr 说明了原因，这里仍按约定输出 reject。
                        raise ValueError('cannot open the controller connection for this profile')
                    status, info = connection.send('GET', '/client')
                    if status != 200:
                        raise ValueError('GET /client returned HTTP %d' % status)
                    if args.role == 'controller':
                        status, body = connection.send('GET', '/nodes')
                        if status == 200:
                            nodes = body['nodes']
                result = evaluate_compat(manifest, info, args.role, nodes)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return EXIT_CODES[result['decision']] if 'decision' in result else 0
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, zipfile.BadZipFile, cli.Unreachable) as exc:
        print(json.dumps(dict(decision='reject', error_type=type(exc).__name__, message=str(exc)), ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.exit(main())
