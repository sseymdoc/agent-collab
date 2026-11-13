"""Identity of the running package, independent of cwd and install pointers."""
import json
import re
from pathlib import Path

from . import __version__ as VERSION

_VERSION_DIRECTORY = Path(__file__).resolve().parents[1]


def parse_version(text):
    """Return a sortable key for X.Y.Z[{a|b|rc}N][.devN], a strict PEP 440 subset."""
    match = re.fullmatch(r'([0-9]+)\.([0-9]+)\.([0-9]+)(?:(a|b|rc)([0-9]+))?(?:\.dev([0-9]+))?', text) if isinstance(text, str) else None
    if match is None:
        raise ValueError('invalid version; expected X.Y.Z[{a|b|rc}N][.devN]')
    major, minor, patch, pre, number, dev = match.groups()
    # A bare .dev precedes every prerelease. Within a prerelease, .dev precedes that release.
    stage = {'a': 0, 'b': 1, 'rc': 2}.get(pre, -1 if dev is not None else 3)
    return (int(major), int(minor), int(patch), stage, int(number or 0),
            dev is None, int(dev or 0))


def compare_versions(a, b):
    left, right = parse_version(a), parse_version(b)
    return (left > right) - (left < right)


def version_directory():
    # Freeze at import so switching a "current" symlink cannot relabel old code.
    return _VERSION_DIRECTORY


def release_info(directory=None):
    try:
        root = Path(directory) if directory is not None else version_directory()
        value = json.loads((root / 'release-manifest.json').read_text(encoding='utf-8'))
        version, commit = value['version'], value.get('git_commit')
        if not isinstance(version, str) or not version.strip():
            raise ValueError('invalid release version')
        if commit is not None and not isinstance(commit, str):
            raise ValueError('invalid release commit')
        return dict(version=version, git_commit=commit)
    except (OSError, ValueError, KeyError, TypeError):
        return dict(version=VERSION, git_commit=None)
