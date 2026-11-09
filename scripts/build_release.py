#!/usr/bin/env python3
"""Build local, dependency-free releases; never fetch or publish anything."""
import argparse
from contextlib import closing
import ast
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from agent_collab.installer import safe_name, schema_fingerprint, validate_payload, validate_version


def build_release(source, output, version):
    source, output = Path(source), Path(output)
    validate_version(version)
    files = {}
    for directory in ('agent_collab', 'installer'):
        for path in sorted((source / directory).rglob('*')):
            relative = path.relative_to(source).as_posix()
            if (any(part in ('__pycache__', 'node_modules', '.dev', 'tests') for part in Path(relative).parts)
                    or path.name.lower().endswith(('.pyc', '.pyo', '.token', '.db', '.db-wal', '.db-shm'))):
                continue
            if path.is_symlink():
                raise ValueError('release sources must not contain symlinks: ' + relative)
            if path.is_file():
                safe_name(relative)
                files[relative] = path.read_bytes()
    if 'agent_collab/installer.py' not in files or 'installer/entry.py' not in files:
        raise ValueError('source is missing the installer')
    schema = json.loads(files['installer/release-schema.json'])
    # Read the schema literal without importing or executing the source being packaged.
    tree = ast.parse(files['agent_collab/store.py'].decode('utf-8'))
    ddl = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == 'SCHEMA' for t in n.targets))
    with closing(sqlite3.connect(':memory:')) as db:  # with 连接对象只提交不关闭
        db.executescript(ddl)
        fingerprint = schema_fingerprint(db)
    try:
        commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'],
                                         stderr=subprocess.DEVNULL, text=True).strip()
        dirty = bool(subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain'],
                                            stderr=subprocess.DEVNULL, text=True).strip())
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = None, None
    manifest = dict(version=version, git_commit=commit, git_dirty=dirty,
                    built_at=datetime.now(timezone.utc).isoformat(), python_requires='>=3.9',
                    ui_built='agent_collab/static/index.html' in files,
                    controller_schema_fingerprint=fingerprint, **schema)
    manifest['files'] = {name: dict(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
                         for name, data in files.items()}
    files['release-manifest.json'] = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
    # 与安装器同一套校验（大小写/Unicode 别名、文件/目录冲突等）：装不上的包不生成。
    validate_payload(files)
    output.mkdir(parents=True, exist_ok=True)
    stem = output / ('agent-collab-' + version)
    paths = [Path(str(stem) + '.tar.gz'), Path(str(stem) + '.zip')]
    # built_at 每次不同：同一版本重建会得到不同内容，而安装器拒绝已有版本内容变化。不覆盖，要求换版本号。
    existing = [str(p) for p in paths if p.exists()]
    if existing:
        raise ValueError('release already exists; use a new version: ' + ', '.join(existing))
    with tarfile.open(str(paths[0]), 'w:gz') as archive:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name.endswith('.sh') else 0o644
            archive.addfile(info, io.BytesIO(data))
    with zipfile.ZipFile(str(paths[1]), 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (0o100755 if name.endswith('.sh') else 0o100644) << 16
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--source', type=Path, default=ROOT)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'dist')
    args = parser.parse_args(argv)
    for path in build_release(args.source, args.output_dir, args.version):
        print(path)


if __name__ == '__main__':
    main()
