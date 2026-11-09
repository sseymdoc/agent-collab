"""只移除 Caddyfile 中指定站点块（及紧邻其上的注释行），不整体覆盖历史 Caddyfile。

用法：python3 caddy_site.py remove --file /etc/caddy/Caddyfile --site https://<域名>:<端口> --out <新文件>
随后：diff 原文件 新文件 → 确认只少了该站点块 → caddy validate --config <新文件> --adapter caddyfile → 替换并 reload。
"""
import argparse
import sys


def remove_site(text, site):
    lines = text.split('\n')
    start = next((i for i, line in enumerate(lines) if line.strip() == site + ' {'), None)
    if start is None:
        raise ValueError('site block not found: ' + site)
    if sum(1 for line in lines if line.strip() == site + ' {') != 1:
        raise ValueError('site block is not unique: ' + site)
    depth, end = 0, None
    for i in range(start, len(lines)):
        # Caddyfile 引号内的大括号在本项目片段中不出现；遇到则拒绝自动处理。
        if '"' in lines[i] and ('{' in lines[i] or '}' in lines[i]):
            raise ValueError('quoted braces in block; edit manually')
        depth += lines[i].count('{') - lines[i].count('}')
        if depth == 0:
            end = i
            break
    if end is None:
        raise ValueError('unbalanced braces in site block')
    while start > 0 and lines[start - 1].lstrip().startswith('#'):
        start -= 1
    while start > 0 and not lines[start - 1].strip():
        start -= 1
    return '\n'.join(lines[:start] + lines[end + 1:])


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='cmd', required=True)
    rm = sub.add_parser('remove')
    rm.add_argument('--file', required=True)
    rm.add_argument('--site', required=True)
    rm.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    with open(args.file, encoding='utf-8') as f:
        text = f.read()
    result = remove_site(text, args.site)
    with open(args.out, 'w', encoding='utf-8') as f:
        f.write(result)
    print('removed %d lines; review with: diff %s %s' % (text.count('\n') - result.count('\n'), args.file, args.out))


if __name__ == '__main__':
    if sys.argv[1:] == ['--self-check']:
        sample = 'a.example {\n\trespond ok\n}\n\n# agent-collab controller\nhttps://x.example:8443 {\n\ttls {\n\t\tissuer acme\n\t}\n\treverse_proxy 127.0.0.1:18669\n}\nb.example {\n}\n'
        out = remove_site(sample, 'https://x.example:8443')
        assert out == 'a.example {\n\trespond ok\n}\nb.example {\n}\n', out
        for bad in ('https://missing:1', ):
            try:
                remove_site(sample, bad)
            except ValueError:
                pass
            else:
                raise AssertionError('missing site must fail')
        print('ok')
    else:
        main()
