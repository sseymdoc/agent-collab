"""桌面外壳 Rust 依赖的许可证文本：生成 desktop/THIRD_PARTY_RUST_LICENSES.txt，并核对它是否完整。

crate 列表取自 desktop/THIRD_PARTY_RUST.md 的表；每个 crate 包根目录里的 LICENSE*、LICENCE*、COPYING*、NOTICE*、
UNLICENSE*、COPYRIGHT* 原样收录。包里没有许可证文件的 crate，从 desktop/third_party_rust/sources.json 取补充的原始文本
（上游对应提交的文件或可追溯的标准文本，核对 SHA-256），或在那里写明不适用的理由。内容相同的文本只写一次。

核对（--check，发行构建也会调用 check_release）：
- 用 `cargo metadata --locked --offline` 按两个发行目标解析 Cargo.lock：进入构建的 crate（normal 与 build 依赖，
  不含 dev）都要在表里、版本一致，表里也不能有依赖图之外的 crate；
- 每个 crate 都要有许可证文本（包内文件或补充文本），或有写明理由的不适用记录；
- 仓库里的 THIRD_PARTY_RUST_LICENSES.txt 与重新生成的内容一致。
不联网：cargo 缓存里缺 crate 就失败。

用法：python scripts/rust_licenses.py [--check] [--cargo-home <目录>] [--src <desktop/src-tauri>]
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / 'desktop' / 'THIRD_PARTY_RUST.md'
OUT = ROOT / 'desktop' / 'THIRD_PARTY_RUST_LICENSES.txt'
SUPPLEMENT = ROOT / 'desktop' / 'third_party_rust'
NAMES = re.compile(r'^(LICEN[CS]E|COPYING|NOTICE|UNLICENSE|COPYRIGHT)', re.I)
TARGETS = {'windows-x86_64': 'x86_64-pc-windows-msvc', 'darwin-aarch64': 'aarch64-apple-darwin'}


def table(path=TABLE):
    rows = re.findall(r'^\| ([A-Za-z0-9_-]+) \| ([0-9][^ |]*) \| ([^|]+?) \| [^|]+ \|$', path.read_text(encoding='utf-8'), re.M)
    return {'%s %s' % (name, version): declared.strip() for name, version, declared in rows if name != 'crate'}


def graph(src):
    """{"name version": {"license": 声明, "targets": {目标: "linked" | "build-time"}}}。
    linked：从根只经 normal 依赖到达、且不是 proc-macro（编进程序）；build-time：只经构建依赖或 proc-macro 到达。"""
    found = {}
    for label, triple in TARGETS.items():
        meta = json.loads(subprocess.run(['cargo', 'metadata', '--format-version', '1', '--locked', '--offline', '--filter-platform', triple],
                                         cwd=str(src), capture_output=True, check=True).stdout)
        packages = {p['id']: p for p in meta['packages']}
        nodes = {n['id']: n for n in meta['resolve']['nodes']}
        root = meta['resolve']['root']

        def walk(linked_only):
            seen, stack = set(), [root]
            while stack:
                for dep in nodes[stack.pop()]['deps']:
                    kinds = {k['kind'] for k in dep['dep_kinds']}
                    if linked_only and (None not in kinds or any('proc-macro' in t['kind'] for t in packages[dep['pkg']]['targets'])):
                        continue
                    if not linked_only and kinds <= {'dev'}:
                        continue
                    if dep['pkg'] not in seen:
                        seen.add(dep['pkg'])
                        stack.append(dep['pkg'])
            return seen

        linked = walk(True)
        for pid in walk(False):
            p = packages[pid]
            entry = found.setdefault('%s %s' % (p['name'], p['version']), dict(license=p['license'], targets={}))
            entry['targets'][label] = 'linked' if pid in linked else 'build-time'
    return found


def supplement(folder=SUPPLEMENT):
    """{"name version": {"files": [(文件名, 文本)], "not_applicable": 理由}}，以及核对补充文件时发现的问题。"""
    manifest = json.loads((folder / 'sources.json').read_text(encoding='utf-8'))
    problems, result = [], {}
    for crate, entry in manifest['crates'].items():
        files = []
        for name in entry.get('files', []):
            path, record = folder / name, manifest['files'].get(name)
            if record is None or not path.is_file():
                problems.append('%s：补充文件 %s 没有出处记录或不存在' % (crate, name))
                continue
            data = path.read_bytes().replace(b'\r\n', b'\n')  # 摘要按 LF 计算，与检出时的换行转换无关
            if hashlib.sha256(data).hexdigest() != record['sha256']:
                problems.append('%s：补充文件 %s 与记录的 SHA-256 不符' % (crate, name))
                continue
            files.append((name, data.decode('utf-8', 'replace')))
        result[crate] = dict(files=files, not_applicable=entry.get('not_applicable'), source=entry.get('commit'), election=entry.get('election'))
    return result, problems


def package_texts(listed, cargo_home):
    registries = sorted((Path(cargo_home) / 'registry' / 'src').glob('*'))
    texts, absent = {}, []
    for crate in listed:
        folder = next((r / crate.replace(' ', '-') for r in registries if (r / crate.replace(' ', '-')).is_dir()), None)
        if folder is None:
            absent.append(crate)
            continue
        texts[crate] = [(p.name, p.read_bytes().decode('utf-8', 'replace')) for p in sorted(folder.iterdir()) if p.is_file() and NAMES.match(p.name)]
    return texts, absent


def check(listed, deps, texts, extra):
    """完整性问题清单（空表示完整）。listed：表；deps：依赖图（None 表示没有核对依赖图）；texts：包内文本；extra：补充记录。"""
    problems = []
    if deps is not None:
        problems += ['依赖图里有、表里没有：%s' % c for c in sorted(set(deps) - set(listed))]
        problems += ['表里有、依赖图里没有：%s' % c for c in sorted(set(listed) - set(deps))]
    for crate in sorted(listed):
        if not texts.get(crate) and not extra.get(crate, {}).get('files') and not extra.get(crate, {}).get('not_applicable'):
            problems.append('没有许可证文本，也没有不适用的理由：%s（%s）' % (crate, listed[crate]))
    return problems


def render(listed, deps, texts, extra):
    groups = {}
    for crate in sorted(listed):
        if texts.get(crate):
            for name, text in texts[crate]:
                groups.setdefault(text.replace('\r\n', '\n').strip() + '\n', []).append('%s（%s）' % (crate, name))
        else:
            for name, text in extra.get(crate, {}).get('files', []):
                groups.setdefault(text.replace('\r\n', '\n').strip() + '\n', []).append('%s（补充：%s）' % (crate, name))
    supplemented = [c for c in sorted(listed) if not texts.get(c) and c in extra]
    parts = ['桌面外壳 Rust 依赖的许可证文本\n\n由 scripts/rust_licenses.py 生成。crate 列表与声明的许可证见 THIRD_PARTY_RUST.md（共 %d 个，'
             '与 Cargo.lock 按 Windows x64、macOS arm64 两个发行目标解析出的依赖一致）。每个 crate 包内的许可证文件原样收录；'
             '内容相同的文本只收录一次，前面列出用到它的 crate 与文件名。\n' % len(listed)]
    if supplemented:
        parts.append('\n下面这些 crate 的包里没有许可证文件，补充的文本取自上游发布该版本时的提交或可追溯的标准文本'
                     '（出处与 SHA-256 见 desktop/third_party_rust/sources.json）：\n')
        for crate in supplemented:
            entry = extra[crate]
            where = deps.get(crate, {}).get('targets') if deps else None
            usage = '、'.join('%s %s' % (t, '编进程序' if k == 'linked' else '只在构建期') for t, k in sorted(where.items())) if where else ''
            parts.append('- %s：声明 %s%s；上游 %s%s%s\n' % (crate, listed[crate], '（选用 %s）' % entry['election'] if entry.get('election') else '',
                                                        entry.get('source') or '—', '；' + usage if usage else '',
                                                        '；不适用：' + entry['not_applicable'] if entry.get('not_applicable') else ''))
    for text, users in sorted(groups.items(), key=lambda item: item[1][0]):
        parts.append('\n' + '=' * 78 + '\n' + ''.join('%s\n' % u for u in users) + '-' * 78 + '\n' + text)
    return ''.join(parts)


def check_release(src=None, cargo_home=None):
    """发行构建用：返回问题清单（空表示许可材料完整且与仓库里的生成文件一致）。"""
    listed = table()
    try:
        deps = graph(src or ROOT / 'desktop' / 'src-tauri')
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        return ['无法用 cargo metadata 解析依赖图：%s' % str(exc)[:200]]
    texts, absent = package_texts(listed, cargo_home or os.environ.get('CARGO_HOME') or Path.home() / '.cargo')
    extra, problems = supplement()
    problems = ['cargo 缓存里没有 %s' % c for c in absent] + problems + check(listed, deps, texts, extra)
    if not problems and OUT.read_text(encoding='utf-8') != render(listed, deps, texts, extra):
        problems.append('THIRD_PARTY_RUST_LICENSES.txt 不是由当前的表与补充文本生成的（重新运行 scripts/rust_licenses.py）')
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--cargo-home', default=os.environ.get('CARGO_HOME') or str(Path.home() / '.cargo'))
    parser.add_argument('--src', default=str(ROOT / 'desktop' / 'src-tauri'), help='含 Cargo.lock 的 desktop/src-tauri')
    parser.add_argument('--check', action='store_true', help='只核对，不写文件')
    args = parser.parse_args(argv)
    if args.check:
        problems = check_release(args.src, args.cargo_home)
        print('\n'.join(problems) or '许可材料完整')
        return 1 if problems else 0
    listed = table()
    deps = graph(args.src)
    texts, absent = package_texts(listed, args.cargo_home)
    extra, problems = supplement()
    problems = ['cargo 缓存里没有 %s' % c for c in absent] + problems + check(listed, deps, texts, extra)
    if problems:
        raise SystemExit('\n'.join(problems))
    OUT.write_bytes(render(listed, deps, texts, extra).encode('utf-8'))  # LF everywhere; write_text(newline=) needs 3.10
    print('%s：%d 个 crate，其中 %d 个用补充文本' % (OUT.relative_to(ROOT), len(listed), sum(1 for c in listed if not texts.get(c))))
    return 0


if __name__ == '__main__':
    sys.exit(main())
