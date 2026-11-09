"""Identity of the running package, independent of cwd and install pointers."""
import json
from pathlib import Path

from . import __version__ as VERSION

_VERSION_DIRECTORY = Path(__file__).resolve().parents[1]


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
