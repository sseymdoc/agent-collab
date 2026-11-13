#!/usr/bin/env python3
"""桌面发行流水线（ADR 0004）。只用标准库；不下载、不发布任何东西。

  build_desktop.py build --src <干净检出> --out <输出目录> --kind formal|candidate|internal-test --version <桌面版本>
                         --runtime <随包运行时目录> --release <后台发行包 tar.gz> --tauri <Tauri CLI 命令>
                         [--release-signature <.sig> --allowed-signers <文件>] [--update-base <URL>] [--updater-key <私钥文件>]
                         [--fixture-module <名称>] [--target-dir <cargo target>]
      组装随包载荷（runtime/、app/、release/）并写 payload.json → 按构建身份生成 Tauri 配置与环境变量 → 构建 →
      平台签名与公证（有身份时）→ 从最终产物生成更新归档与更新签名 → 写本平台的发行片段 dist/platform-<目标>.json。
  build_desktop.py manifest --dist <dist> --channel <通道> --base-url <URL> --updater-key <私钥文件> [--notes <文件>]
      合并各平台片段，写 product-manifest.json（并签名）、<通道>/latest.json 与发行说明草稿。
  build_desktop.py gate --dist <dist> [--test-pubkey <公钥文件> ...] [--require-platforms windows-x86_64,darwin-aarch64]
      发行门禁：身份与通道、内部 feature、测试公钥与不安全传输、源提交与干净状态、产物摘要与更新签名的绑定、
      签名与公证、第三方许可。任何一项不符即失败，结果写 gate-report.json。

更新签名必须对应最后实际分发的字节：Windows 在 Authenticode 签名之后对安装包签名；macOS 在应用（含嵌套代码）签名、
公证与 stapling 之后再打包更新归档并签名。重新编译或改动资源之后，旧的签名、摘要与公证结果都不再有效，要整步重来。
"""
import argparse
import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile

# ADR 0004 第 1 条。门禁按这张表核对，构建也只从这里取身份。
KINDS = {
    'formal': dict(identifier='org.agent-collab.desktop', product='Agent Collab', instance='', port='18669', channels=['stable', 'preview']),
    'candidate': dict(identifier='org.agent-collab.desktop.cand', product='Agent Collab Candidate', instance='cand', port='18769',
                      channels=['stable', 'preview']),
    'internal-test': dict(identifier='org.agent-collab.desktop.cand-internal-test', product='Agent Collab Candidate internal-test',
                          instance='itest', port='18779', channels=['internal-test']),
}
# 首版支持范围：Windows 10 22H2 起的 x64（WebView2 随系统或由安装器引导），macOS 11 起的 Apple Silicon（随包 CPython 的下限）。
MIN_OS = {'windows-x86_64': '10.0.19045', 'darwin-aarch64': '11.0'}
ROOT = Path(__file__).resolve().parents[1]
# 随桌面安装包分发的许可材料：resources/licenses/<名字> ← 源目录里的文件。
LICENSE_FILES = {'LICENSE': 'LICENSE', 'THIRD_PARTY.md': 'THIRD_PARTY.md', 'THIRD_PARTY_RUST.md': 'desktop/THIRD_PARTY_RUST.md',
                 'THIRD_PARTY_RUST_LICENSES.txt': 'desktop/THIRD_PARTY_RUST_LICENSES.txt',
                 'third-party-licenses.md': 'agent_collab/static/third-party-licenses.md'}
NO_WINDOW = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}


def sha256(path):
    digest = hashlib.sha256()
    with open(str(path), 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def run(command, cwd=None, env=None, check=True):
    print('$ ' + ' '.join(shlex.quote(str(c)) for c in command), flush=True)
    result = subprocess.run([str(c) for c in command], cwd=str(cwd) if cwd else None, env=env, **NO_WINDOW)
    if check and result.returncode:
        raise SystemExit('命令失败（退出码 %d）：%s' % (result.returncode, command[0]))
    return result


def capture(command, cwd=None):
    result = subprocess.run([str(c) for c in command], cwd=str(cwd) if cwd else None, capture_output=True, text=True, **NO_WINDOW)
    return result.returncode, (result.stdout + result.stderr).strip()


def target_name():
    machine = platform.machine().lower()
    arch = {'amd64': 'x86_64', 'x86_64': 'x86_64', 'arm64': 'aarch64', 'aarch64': 'aarch64'}.get(machine, machine)
    return ('windows' if os.name == 'nt' else 'darwin' if sys.platform == 'darwin' else 'linux') + '-' + arch


def file_label(kind, version, target, suffix):
    os_part = {'windows-x86_64': 'windows-x64', 'darwin-aarch64': 'macos-arm64'}.get(target, target)
    return 'agent-collab-desktop-%s-%s%s%s' % (version, os_part, '-internal-test' if kind == 'internal-test' else '', suffix)


def payload_manifest(resources, build):
    files = {}
    for path in sorted(resources.rglob('*')):
        if path.is_symlink():
            raise SystemExit('随包载荷里不应有链接：%s' % path)
        if path.is_file() and path.name != 'payload.json':
            files[path.relative_to(resources).as_posix()] = dict(sha256=sha256(path), size=path.stat().st_size)
    data = json.dumps(dict(schema=1, build=build, files=files), sort_keys=True, separators=(',', ':')).encode()
    (resources / 'payload.json').write_bytes(data)
    return hashlib.sha256(data).hexdigest(), len(files)


def verify_release_signature(release, signature, signers):
    """随包的后台发行包签名用安装器同一套核对（同一字节、signer 与 namespace）；核对不过就不打包，门禁据此记 signed。"""
    sys.path.insert(0, str(ROOT))
    from agent_collab import installer
    archive, signed, trusted = Path(release).read_bytes(), Path(signature).read_bytes(), Path(signers).read_bytes()
    reasons = []
    for executable in installer.ssh_keygen_candidates():
        try:
            installer.verify_signature(executable, archive, signed, trusted)
            return
        except installer.VerifierUnavailable as exc:
            reasons.append(str(exc))
        except ValueError as exc:
            raise SystemExit('后台发行包的来源签名核对不通过：%s' % exc)
    raise SystemExit('没有能核对来源签名的 ssh-keygen：%s' % ('; '.join(reasons) or '找不到 ssh-keygen'))


def tauri_signature(tauri, key, version, path):
    """Tauri updater 签名（minisign），--app-version 把版本写进受签名保护的注释，updater 据此拒绝版本错配。"""
    env = dict(os.environ, TAURI_SIGNING_PRIVATE_KEY_PASSWORD=os.environ.get('TAURI_SIGNING_PRIVATE_KEY_PASSWORD', ''))
    signature = Path(str(path) + '.sig')
    signature.unlink(missing_ok=True)
    run(tauri + ['signer', 'sign', '-f', str(key), '--app-version', version, str(path)], env=env)
    if not signature.is_file():
        raise SystemExit('签名文件没有生成：%s' % signature)
    return signature.read_text(encoding='utf-8').strip()


def trusted_comment(signature):
    """签名里受保护的注释（不做验签：验签由 updater 与外壳完成，门禁只核对它绑定的版本与文件名）。"""
    lines = base64.b64decode(signature).decode('utf-8').splitlines()
    return next((line.split(':', 1)[1].strip() for line in lines if line.startswith('trusted comment:')), '')


def git(src, *args):
    return subprocess.run(['git', '-c', 'safe.directory=*', '-C', str(src)] + list(args), capture_output=True, text=True, check=True, **NO_WINDOW).stdout.strip()


def release_manifest(path):
    with tarfile.open(str(path), 'r:gz') as archive:
        return json.load(archive.extractfile('release-manifest.json'))


def is_macho(path):
    with open(str(path), 'rb') as handle:
        return handle.read(4) in (b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xfe\xed\xfa\xcf')


def sign_nested(resources, identity):
    """macOS：随包运行时里的可执行文件与动态库在生成载荷清单之前签名（签名会改动文件），之后 Tauri 只签应用本身。
    Developer ID 带 hardened runtime 与时间戳；候选用临时签名（-），只保证封存一致，不能通过 Gatekeeper。"""
    count = 0
    for path in sorted(resources.rglob('*')):
        if path.is_file() and not path.is_symlink() and is_macho(path):
            options = [] if identity == '-' else ['--options', 'runtime', '--timestamp']
            run(['codesign', '--force'] + options + ['--sign', identity, str(path)])
            count += 1
    return count


def tar_app(app, destination):
    """macOS 更新归档：tar.gz 里是 <名称>.app 目录本身，保留链接与权限（Tauri updater 的格式）。"""
    with tarfile.open(str(destination), 'w:gz', format=tarfile.PAX_FORMAT) as archive:
        archive.add(str(app), arcname=app.name)


# ---------------------------------------------------------------- build

def build(args):
    kind, version = args.kind, args.version
    identity = KINDS[kind]
    src, out = Path(args.src).resolve(), Path(args.out).resolve()
    tauri = shlex.split(args.tauri, posix=os.name != 'nt') if isinstance(args.tauri, str) else args.tauri
    commit = git(src, 'rev-parse', 'HEAD')
    # 复制进包的是 desktop/、agent_collab/ 与许可材料：这些路径下未跟踪的文件也算改动（被忽略的生成物不算）。
    dirty = bool(git(src, 'status', '--porcelain', '--untracked-files=all', '--', 'desktop', 'agent_collab', *LICENSE_FILES.values()))
    if dirty and not args.allow_dirty:
        raise SystemExit('源检出有未提交的改动；发行构建只从干净提交进行')
    if (kind == 'formal') == ('-' in version):
        raise SystemExit('正式版的版本号不带预发布后缀，候选与内部测试版必须带')
    if args.fixture_module and kind == 'formal':
        raise SystemExit('正式版不能带测试夹具模块')
    target = target_name()
    work, dist = out / 'work', out / 'dist'
    if work.exists():
        shutil.rmtree(str(work))
    dist.mkdir(parents=True, exist_ok=True)
    shutil.copytree(str(src / 'desktop'), str(work / 'desktop'), ignore=shutil.ignore_patterns('target', 'gen', 'node_modules'))
    static = src / 'agent_collab' / 'static'
    if not (static / 'index.html').is_file():
        raise SystemExit('页面产物 agent_collab/static 不存在：先构建页面')
    shutil.copytree(str(static), str(work / 'agent_collab' / 'static'))
    resources = work / 'desktop' / 'src-tauri' / 'resources'
    shutil.copytree(str(src / 'agent_collab'), str(resources / 'app' / 'agent_collab'),
                    ignore=shutil.ignore_patterns('static', '__pycache__', '*.pyc'))
    if args.fixture_module:
        # 覆盖安装的残留验证：旧版带模块 A、新版删掉 A 加上 B（只用于测试身份）。
        name = '_m3_fixture_' + args.fixture_module
        (resources / 'app' / 'agent_collab' / (name + '.py')).write_text('FIXTURE = %r\n' % args.fixture_module, encoding='utf-8')
    shutil.copytree(str(Path(args.runtime)), str(resources / 'runtime'))
    (resources / 'release').mkdir()
    release = Path(args.release)
    shutil.copy2(str(release), str(resources / 'release' / release.name))
    backend = release_manifest(release)
    if args.release_signature:
        verify_release_signature(release, args.release_signature, args.allowed_signers)
        shutil.copy2(args.release_signature, str(resources / 'release' / (release.name + '.sig')))
        shutil.copy2(args.allowed_signers, str(resources / 'release' / 'allowed_signers'))
    # 许可材料随桌面安装包分发（在载荷清单里，受同样的核对）：本项目许可证、第三方清单、页面与 Rust 依赖的许可证文本。
    (resources / 'licenses').mkdir()
    for name, source in LICENSE_FILES.items():
        if (src / source).is_file():
            shutil.copy2(str(src / source), str(resources / 'licenses' / name))
    build_id = dict(kind=kind, desktop_version=version, backend_version=backend['version'], commit=commit)
    codesign_identity = os.environ.get('AGENT_COLLAB_CODESIGN_IDENTITY') or ('-' if kind != 'formal' else None)
    nested = sign_nested(resources, codesign_identity) if target.startswith('darwin') and codesign_identity else 0
    # Windows：有证书时，随包运行时的 exe/dll/pyd 在生成载荷清单之前用 signtool 签名（带时间戳）；安装包与主程序由 Tauri 签。
    signtool, thumbprint = os.environ.get('AGENT_COLLAB_SIGNTOOL'), os.environ.get('AGENT_COLLAB_SIGN_CERT_SHA1')
    timestamp_url = os.environ.get('AGENT_COLLAB_TIMESTAMP_URL', 'http://timestamp.digicert.com')
    if target.startswith('windows') and signtool and thumbprint:
        for path in sorted(resources.rglob('*')):
            if path.suffix.lower() in ('.exe', '.dll', '.pyd'):
                run([signtool, 'sign', '/sha1', thumbprint, '/fd', 'sha256', '/tr', timestamp_url, '/td', 'sha256', str(path)])
                nested += 1
    payload_sha, payload_files = payload_manifest(resources, build_id)
    pubkey = Path(args.updater_pubkey).read_text(encoding='utf-8').strip() if args.updater_pubkey else ''
    insecure = bool(args.update_base and args.update_base.startswith('http://'))
    if insecure and (kind == 'formal' or not args.update_base.startswith(('http://127.0.0.1', 'http://localhost'))):
        raise SystemExit('只有测试身份可以用 loopback 上的 http 更新地址')
    mac_bundle = dict(minimumSystemVersion=MIN_OS['darwin-aarch64'], hardenedRuntime=codesign_identity not in (None, '-'))
    if codesign_identity:
        mac_bundle['signingIdentity'] = codesign_identity
    bundle = dict(macOS=mac_bundle)
    if thumbprint:
        bundle['windows'] = dict(certificateThumbprint=thumbprint, digestAlgorithm='sha256', timestampUrl=timestamp_url,
                                 nsis=dict(installerHooks='./windows/hooks.nsh'))
    config = dict(identifier=identity['identifier'], productName=identity['product'], version=version,
                  bundle=bundle,
                  plugins=dict(updater=dict(pubkey=pubkey, endpoints=[], requireSignedVersion=True,
                                            dangerousInsecureTransportProtocol=insecure, windows=dict(installMode='passive'))))
    config_path = out / ('tauri-%s.json' % kind)
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=1), encoding='utf-8')
    env = dict(os.environ, AGENT_COLLAB_BUILD_KIND=kind, AGENT_COLLAB_INSTANCE=identity['instance'], AGENT_COLLAB_LOCAL_PORT=identity['port'],
               AGENT_COLLAB_COMMIT=commit, CARGO_TARGET_DIR=str(Path(args.target_dir or (out / 'target')).resolve()), CI='true')
    if args.update_base and pubkey:
        env.update(AGENT_COLLAB_UPDATE_BASE=args.update_base.rstrip('/'), AGENT_COLLAB_UPDATE_CHANNELS=','.join(identity['channels']))
    else:
        env.pop('AGENT_COLLAB_UPDATE_BASE', None)
        env.pop('AGENT_COLLAB_UPDATE_CHANNELS', None)
    features = ['automation'] if kind == 'internal-test' else []
    bundles = 'nsis' if target.startswith('windows') else 'app,dmg'
    run(tauri + ['build', '--bundles', bundles, '--config', str(config_path)] + (['--features', ','.join(features)] if features else []),
        cwd=work / 'desktop', env=env)
    bundle_dir = Path(env['CARGO_TARGET_DIR']) / 'release' / 'bundle'
    artifacts, signing = [], {}
    if target.startswith('windows'):
        setup = next((bundle_dir / 'nsis').glob('*_%s_x64-setup.exe' % version))
        final = dist / file_label(kind, version, target, '-setup.exe')
        shutil.copy2(str(setup), str(final))
        # 安装包与主程序的 Authenticode 签名由 Tauri 在打包时完成（有证书时）；这里只核对最终文件，之后才生成更新签名。
        code, detail = capture([signtool, 'verify', '/pa', str(final)]) if signtool else (None, '没有配置 signtool')
        signing = dict(platform='authenticode' if code == 0 else None, verified=code == 0, nested_signed=nested,
                       detail=('没有代码签名证书；' if not thumbprint else '') + detail[-400:], notarized=None, stapled=None)
        updater = final
        artifacts.append(dict(name=final.name, role='installer+updater'))
    else:
        app = next((bundle_dir / 'macos').glob('*.app'))
        # 应用本身由 Tauri 按 signingIdentity 签名（嵌套代码已在组装阶段签过，见 sign_nested）。
        identity_name = os.environ.get('AGENT_COLLAB_CODESIGN_IDENTITY')
        code_deep, deep = capture(['codesign', '--verify', '--deep', '--strict', '--verbose=2', str(app)])
        _, described = capture(['codesign', '-dv', str(app)])
        notarized = stapled = False
        profile = os.environ.get('AGENT_COLLAB_NOTARY_PROFILE')
        dmg_src = next((bundle_dir / 'dmg').glob('*.dmg'))
        if identity_name and profile:
            run(['xcrun', 'notarytool', 'submit', str(dmg_src), '--keychain-profile', profile, '--wait'])
            run(['xcrun', 'stapler', 'staple', str(app)])
            run(['xcrun', 'stapler', 'staple', str(dmg_src)])
            notarized = stapled = True
        code_gk, gatekeeper = capture(['spctl', '-a', '-vv', '-t', 'exec', str(app)])
        signing = dict(platform='developer_id' if identity_name and 'Authority=Developer ID' in described else ('adhoc' if 'Signature=adhoc' in described else None),
                       verified=code_deep == 0, nested_signed=nested, nested_verify=deep[-600:], sealed='Sealed Resources=none' not in described,
                       gatekeeper=code_gk == 0, gatekeeper_detail=gatekeeper[-300:], notarized=notarized, stapled=stapled)
        dmg = dist / file_label(kind, version, target, '.dmg')
        shutil.copy2(str(dmg_src), str(dmg))
        updater = dist / file_label(kind, version, target, '.app.tar.gz')
        tar_app(app, updater)  # 签名、公证与 stapling 之后的应用包
        artifacts += [dict(name=dmg.name, role='installer'), dict(name=updater.name, role='updater')]
    signature = tauri_signature(tauri, args.updater_key, version, updater) if args.updater_key else None
    if signature:
        artifacts.append(dict(name=updater.name + '.sig', role='updater-signature'))
    for item in artifacts:
        path = dist / item['name']
        item.update(sha256=sha256(path), bytes=path.stat().st_size)
    runtime_info = json.loads((Path(args.runtime) / 'RUNTIME.json').read_text(encoding='utf-8')) if (Path(args.runtime) / 'RUNTIME.json').is_file() else None
    fragment = dict(schema=1, target=target, kind=kind, identifier=identity['identifier'], product=identity['product'], instance=identity['instance'],
                    channels=identity['channels'], desktop_version=version, commit=commit, dirty=dirty, built_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    features=features, min_os=MIN_OS.get(target), artifacts=artifacts,
                    updater=dict(file=updater.name, signature=signature, pubkey_sha256=hashlib.sha256(pubkey.encode()).hexdigest() if pubkey else None,
                                 base=args.update_base, insecure_transport=insecure),
                    signing=signing, payload=dict(sha256=payload_sha, files=payload_files, fixture=args.fixture_module),
                    runtime=runtime_info,
                    license_sha256={p.name: sha256(p) for p in sorted((resources / 'licenses').iterdir())},
                    licenses=sorted(p.name for p in (resources / 'licenses').iterdir())
                    + (['runtime/' + runtime_info['license'].replace('\\', '/')] if runtime_info and runtime_info.get('license') and (resources / 'runtime' / runtime_info['license']).is_file() else []),
                    backend=dict(file=release.name, sha256=sha256(release), signed=bool(args.release_signature),
                                 manifest={k: backend.get(k) for k in ('version', 'git_commit', 'git_dirty', 'ui_built', 'protocol_version', 'features', 'compat',
                                                                      'controller_schema_min', 'controller_schema_max', 'controller_schema_fingerprint')},
                                 licensed='agent_collab/LICENSE' in backend.get('files', {}), third_party='agent_collab/THIRD_PARTY.md' in backend.get('files', {})))
    path = dist / ('platform-%s%s.json' % (target, '-internal-test' if kind == 'internal-test' else ''))
    path.write_text(json.dumps(fragment, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps(dict(fragment=str(path), artifacts=artifacts, signing=signing), ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- sign

def sign(args):
    """在唯一的签名机上补更新签名。别的构建机不持有 updater 私钥：它完成平台签名后不带 --updater-key 构建，
    把冻结的 dist 送到签名机。这里只给还没有更新签名的平台片段签名；更新包的字节必须与片段记录的摘要相同
    （冻结之后不能再改），签名私钥必须对应这个构建编进程序的公钥。"""
    dist, tauri = Path(args.dist), shlex.split(args.tauri, posix=os.name != 'nt')
    pubkey = hashlib.sha256(Path(args.updater_key + '.pub').read_text(encoding='utf-8').strip().encode()).hexdigest()
    for path in sorted(dist.glob('platform-*.json')):
        f = json.loads(path.read_text(encoding='utf-8'))
        if f['updater']['signature']:
            continue
        updater = dist / f['updater']['file']
        recorded = next((a for a in f['artifacts'] if a['name'] == updater.name), None)
        if recorded is None or not updater.is_file() or sha256(updater) != recorded['sha256']:
            raise SystemExit('%s 与平台片段记录的摘要不符或不存在：冻结之后不能再改' % updater.name)
        if f['updater']['pubkey_sha256'] != pubkey:
            raise SystemExit('%s：签名私钥与这个构建编进程序的 updater 公钥不对应' % path.name)
        f['updater']['signature'] = tauri_signature(tauri, args.updater_key, f['desktop_version'], updater)
        signature = Path(str(updater) + '.sig')
        f['artifacts'].append(dict(name=signature.name, role='updater-signature', sha256=sha256(signature), bytes=signature.stat().st_size))
        path.write_text(json.dumps(f, ensure_ascii=False, indent=1), encoding='utf-8')
        print('已签名 %s（%s）' % (updater.name, f['target']))


# ---------------------------------------------------------------- manifest

def fragments(dist):
    found = [json.loads(p.read_text(encoding='utf-8')) for p in sorted(Path(dist).glob('platform-*.json'))]
    if not found:
        raise SystemExit('dist 里没有平台片段')
    kinds = {(f['kind'], f['identifier'], f['desktop_version'], f['commit']) for f in found}
    if len(kinds) != 1:
        raise SystemExit('各平台片段的身份、版本或源提交不一致：%s' % sorted(kinds))
    return found


def manifest(args):
    if args.download_base and not args.download_base.startswith('https://'):
        raise SystemExit('--download-base 必须是 https 地址')
    dist = Path(args.dist)
    found = fragments(dist)
    first = found[0]
    if args.channel not in first['channels']:
        raise SystemExit('%s 身份没有 %s 通道' % (first['kind'], args.channel))
    backend = first['backend']
    compat = backend['manifest'].get('compat') or {}
    notes = Path(args.notes).read_text(encoding='utf-8') if args.notes else ''
    product = dict(
        schema=1, product='agent-collab', release_id=first['desktop_version'], channel=args.channel,
        identity=dict(kind=first['kind'], identifier=first['identifier'], product_name=first['product'], instance=first['instance']),
        source=dict(commit=first['commit'], dirty=any(f['dirty'] for f in found)), built_at=max(f['built_at'] for f in found),
        components=dict(desktop=dict(version=first['desktop_version']),
                        backend=dict(version=backend['manifest']['version'], file=backend['file'], sha256=backend['sha256'], signed=backend['signed']),
                        runtime=dict(python=(first.get('runtime') or {}).get('python'), openssl=(first.get('runtime') or {}).get('openssl'))),
        protocol=dict(api_version=1, backend_protocol_version=backend['manifest'].get('protocol_version'), controller_features=backend['manifest'].get('features')),
        database_schema=dict(min=backend['manifest'].get('controller_schema_min'), max=backend['manifest'].get('controller_schema_max'),
                    fingerprint=backend['manifest'].get('controller_schema_fingerprint')),
        compat=dict(compat, client_requires=compat.get('client_requires', []), client_optional=compat.get('client_optional', [])),
        upgrade_order=['worker', 'controller'],
        rollback='程序回退只在 schema 范围内进行（本机后台 install --rollback；桌面端重新安装旧安装包）；恢复旧数据库是另一个操作（agent-collab maintenance restore）',
        features=sorted({x for f in found for x in f['features']}),
        platforms={f['target']: dict(min_os=f['min_os'], artifacts=f['artifacts'], payload_sha256=f['payload']['sha256'],
                                     signed=dict(platform=f['signing'].get('platform'), verified=f['signing'].get('verified'),
                                                 notarized=f['signing'].get('notarized'), stapled=f['signing'].get('stapled')))
                   for f in found},
        third_party=['agent_collab/LICENSE 与 agent_collab/THIRD_PARTY.md（后台发行包）']
        + ['resources/licenses/' + name + '（桌面安装包）' for name in sorted(LICENSE_FILES)]
        + ['resources/runtime/' + str((first.get('runtime') or {}).get('license') or 'python/LICENSE.txt').replace('\\', '/') + '（Python 运行时）'],
    )
    channel_dir = dist / args.channel
    channel_dir.mkdir(exist_ok=True)
    data = json.dumps(product, ensure_ascii=False, indent=1, sort_keys=True).encode('utf-8')
    (channel_dir / 'product-manifest.json').write_bytes(data)
    tauri = shlex.split(args.tauri, posix=os.name != 'nt')
    tauri_signature(tauri, args.updater_key, first['desktop_version'], channel_dir / 'product-manifest.json')
    # 更新包地址：缺省与通道文件放在一起；--download-base 指向版本化的下载位置（例如 GitHub Release 附件，下载时会重定向，
    # updater 跟随重定向）。通道文件本身（latest.json、product-manifest.json 与签名）要放在不重定向的 https 地址上，
    # 桌面端取产品发行清单时不跟随重定向。
    base = (args.download_base.rstrip('/') if args.download_base else args.base_url.rstrip('/') + '/' + args.channel) + '/'
    latest = dict(version=first['desktop_version'], notes=notes, pub_date=datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z'),
                  platforms={f['target']: dict(url=base + f['updater']['file'], signature=f['updater']['signature']) for f in found if f['updater']['signature']},
                  agent_collab=dict(manifest='product-manifest.json'))
    (channel_dir / 'latest.json').write_text(json.dumps(latest, ensure_ascii=False, indent=1), encoding='utf-8')
    for f in found:
        for item in f['artifacts']:
            if item['role'] in ('updater', 'installer+updater', 'updater-signature'):
                shutil.copy2(str(dist / item['name']), str(channel_dir / item['name']))
    print(json.dumps(dict(channel=str(channel_dir), platforms=sorted(latest['platforms'])), ensure_ascii=False))


# ---------------------------------------------------------------- gate

def gate(args):
    dist = Path(args.dist)
    checks = []

    def check(name, ok, detail=''):
        checks.append(dict(check=name, ok=bool(ok), detail=detail))

    found = fragments(dist)
    first = found[0]
    kind = first['kind']
    identity = KINDS.get(kind)
    # 第三方许可材料的完整性：用运行门禁的这份源检出核对 Cargo.lock 的依赖都有许可证文本，并要求包里的文件就是它。
    import rust_licenses
    problems = rust_licenses.check_release()
    check('第三方许可材料完整（Cargo.lock 两个发行目标的依赖都在清单里，且都有许可证文本或不适用理由）', not problems, '；'.join(problems[:5]))
    expected = {name: sha256(ROOT / LICENSE_FILES[name]) for name in ('THIRD_PARTY_RUST.md', 'THIRD_PARTY_RUST_LICENSES.txt')}
    check('构建身份在 ADR 0004 的表里', identity is not None, kind)
    identity = identity or {}
    check('应用标识、实例与通道和身份一致', first['identifier'] == identity.get('identifier') and first['instance'] == identity.get('instance')
          and first['channels'] == identity.get('channels'), '%s / %s / %s' % (first['identifier'], first['instance'], first['channels']))
    check('版本号与身份相符（正式版不带预发布后缀）', (kind == 'formal') != ('-' in first['desktop_version']), first['desktop_version'])
    for f in found:
        label = f['target']
        check(label + '：automation 只在内部测试版', ('automation' in f['features']) == (kind == 'internal-test'), str(f['features']))
        check(label + '：文件名的 internal-test 标记与身份相符', all(('internal-test' in a['name']) == (kind == 'internal-test') for a in f['artifacts']))
        check(label + '：源检出干净', not f['dirty'])
        check(label + '：随包后台发行包来自同一提交且干净', f['backend']['manifest']['git_commit'] == f['commit'] and f['backend']['manifest']['git_dirty'] is False,
              '%s dirty=%s' % (f['backend']['manifest']['git_commit'], f['backend']['manifest']['git_dirty']))
        check(label + '：随包载荷有清单', bool(f['payload']['sha256']) and not (kind == 'formal' and f['payload'].get('fixture')))
        for item in f['artifacts']:
            path = dist / item['name']
            check(label + '：产物摘要与文件一致 ' + item['name'], path.is_file() and sha256(path) == item['sha256'] and path.stat().st_size == item['bytes'])
        signature = f['updater']['signature']
        check(label + '：有更新签名', bool(signature))
        if signature:
            comment = trusted_comment(signature)
            check(label + '：更新签名绑定版本与最终文件', ('version:' + f['desktop_version']) in comment and ('file:' + f['updater']['file']) in comment, comment)
        test_keys = {hashlib.sha256(Path(p).read_text(encoding='utf-8').strip().encode()).hexdigest() for p in (args.test_pubkey or [])}
        uses_test_key = f['updater']['pubkey_sha256'] in test_keys
        if kind == 'formal':
            check(label + '：正式版不带测试验签公钥', f['updater']['pubkey_sha256'] and not uses_test_key)
            check(label + '：正式版更新地址是 https、没有不安全传输', (f['updater']['base'] or '').startswith('https://') and not f['updater']['insecure_transport'], str(f['updater']['base']))
            check(label + '：平台代码签名', f['signing'].get('platform') in ('authenticode', 'developer_id') and f['signing'].get('verified'), f['signing'].get('detail') or '')
            if label.startswith('darwin'):
                check(label + '：公证与 stapling', f['signing'].get('notarized') and f['signing'].get('stapled'))
                check(label + '：Gatekeeper 接受', f['signing'].get('gatekeeper'), f['signing'].get('gatekeeper_detail') or '')
            check(label + '：后台发行包带来源签名', f['backend']['signed'])
        else:
            check(label + '：测试身份不带正式公钥（只允许列出的测试公钥）', uses_test_key or not f['updater']['pubkey_sha256'])
        check(label + '：随包后台发行包带项目许可证与第三方清单', f['backend']['licensed'] and f['backend']['third_party'])
        bundled = set(f.get('licenses') or [])
        check(label + '：许可材料随桌面安装包（项目许可证、第三方清单、页面与 Rust 依赖的许可证文本、Python 运行时许可证）',
              set(LICENSE_FILES) <= bundled and any(name.startswith('runtime/') for name in bundled),
              '缺 ' + '、'.join(sorted(set(LICENSE_FILES) - bundled) or ['运行时许可证']))
        packed = f.get('license_sha256') or {}
        check(label + '：随包的 Rust 依赖清单与许可证文本就是核对过的那一份', all(packed.get(name) == expected.get(name) for name in expected),
              '与 %s 的不同或缺少' % ROOT.name)
    required = [t for t in (args.require_platforms or '').split(',') if t]
    have = {f['target'] for f in found}
    check('发行包含要求的平台', set(required) <= have, '缺 ' + ', '.join(sorted(set(required) - have)) if set(required) - have else '')
    published = [c for c in identity.get('channels', []) if (dist / c / 'latest.json').is_file()]
    check('至少一个通道有 latest.json（先运行 manifest）', bool(published), '通道 ' + '、'.join(identity.get('channels', [])))
    for channel in identity.get('channels', []):
        latest = dist / channel / 'latest.json'
        if latest.is_file():
            value = json.loads(latest.read_text(encoding='utf-8'))
            check(channel + '/latest.json 版本一致且各平台完整', value['version'] == first['desktop_version'] and set(value['platforms']) == have,
                  str(sorted(value['platforms'])))
            check(channel + '/product-manifest.json 有签名', (dist / channel / 'product-manifest.json.sig').is_file())
    report = dict(kind=kind, version=first['desktop_version'], commit=first['commit'], passed=all(c['ok'] for c in checks), checks=checks)
    (dist / 'gate-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    for c in checks:
        print(('通过 ' if c['ok'] else '失败 ') + c['check'] + ('（' + c['detail'] + '）' if c['detail'] and not c['ok'] else ''))
    print('门禁' + ('通过' if report['passed'] else '没有通过'))
    return 0 if report['passed'] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    b = sub.add_parser('build')
    for name in ('src', 'out', 'version', 'runtime', 'release', 'tauri'):
        b.add_argument('--' + name, required=True)
    b.add_argument('--kind', required=True, choices=sorted(KINDS))
    for name in ('release-signature', 'allowed-signers', 'update-base', 'updater-key', 'updater-pubkey', 'fixture-module', 'target-dir'):
        b.add_argument('--' + name)
    b.add_argument('--allow-dirty', action='store_true', help='只用于开发试跑；门禁会拒绝')
    m = sub.add_parser('manifest')
    for name in ('dist', 'channel', 'base-url', 'updater-key', 'tauri'):
        m.add_argument('--' + name, required=True)
    m.add_argument('--notes')
    m.add_argument('--download-base', help='更新包的版本化下载地址（https）；缺省与通道文件同目录')
    s = sub.add_parser('sign', help='在签名机上给不带更新签名构建的平台片段补签')
    for name in ('dist', 'updater-key', 'tauri'):
        s.add_argument('--' + name, required=True)
    g = sub.add_parser('gate')
    g.add_argument('--dist', required=True)
    g.add_argument('--test-pubkey', action='append')
    g.add_argument('--require-platforms')
    args = parser.parse_args(argv)
    if args.command == 'build':
        if bool(args.release_signature) != bool(args.allowed_signers):
            raise SystemExit('--release-signature 与 --allowed-signers 要一起给出')
        if args.updater_key and not args.updater_pubkey:
            args.updater_pubkey = args.updater_key + '.pub'
        return build(args) or 0
    if args.command == 'manifest':
        return manifest(args) or 0
    if args.command == 'sign':
        return sign(args) or 0
    return gate(args)


if __name__ == '__main__':
    sys.exit(main())
