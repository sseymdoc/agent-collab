"""Resolve service commands once; share argv/environment across consumers."""
import json
import os
from pathlib import Path
import platform
import subprocess

HARNESSES = ('codex', 'claude')


def resolve_commands(config=None, env=None, system=None, home=None, mac_dirs=None):
    config = config or {}
    environment = dict(os.environ if env is None else env)
    system = system or platform.system()
    home = Path(home or environment.get('USERPROFILE' if system == 'Windows' else 'HOME') or Path.home())
    folders = environment.get('PATH', os.defpath).split(os.pathsep)
    folders += [str(home / '.local/bin')]
    if system == 'Darwin':
        folders += list(mac_dirs if mac_dirs is not None else ('/opt/homebrew/bin', '/usr/local/bin'))

    def find(command, explicit=False):
        # Preserve launcher/symlink directory for PATH, not its resolved target.
        if system == 'Windows':
            suffixes = [s for s in environment.get('PATHEXT', '.COM;.EXE;.BAT;.CMD').split(';') if s]
            names = [command] if Path(command).suffix.lower() in [s.lower() for s in suffixes] else [command + s for s in suffixes] + [command]
            candidates = names if explicit else [str(Path(folder) / n) for folder in folders if folder for n in names]
            for value in candidates:
                path = Path(value)
                if 'windowsapps' not in [x.lower() for x in path.parts] and path.is_file():
                    return str(path.absolute())
            return None
        def runnable(value):
            return os.path.isfile(value) and os.access(value, os.X_OK)
        if explicit:
            return str(Path(command).absolute()) if runnable(command) else None
        for folder in folders:
            if folder and runnable(os.path.join(folder, command)):
                return str(Path(folder, command).absolute())
        return None

    disabled = config.get('disabled_harnesses', {})
    if not isinstance(disabled, dict):
        raise ValueError('disabled_harnesses must be an object')
    result = {}
    for name in HARNESSES:
        raw = config.get(name + '_command')
        argv = [raw] if isinstance(raw, str) else list(raw) if isinstance(raw, list) else None
        if raw is not None and (not argv or not all(isinstance(x, str) and x for x in argv) or not Path(argv[0]).is_absolute()):
            raise ValueError(name + '_command requires an absolute executable path or argv')
        path = None if name in disabled else find(argv[0] if argv else name, raw is not None)
        command_env = dict(environment)
        if path:
            command_env['PATH'] = str(Path(path).parent) + os.pathsep + environment.get('PATH', os.defpath)
        if name == 'codex':
            for key in ('CODEX_THREAD_ID', 'CODEX_INTERNAL_ORIGINATOR_OVERRIDE'):
                command_env.pop(key, None)
            if config.get('codex_home'):
                command_env['CODEX_HOME'] = config['codex_home']
        result[name] = dict(argv=([path] + (argv[1:] if argv else [])) if path else None,
                            env=command_env, disabled=disabled.get(name))
    return result


def probe_auth(name, command, runner=None):
    if not command['argv']:
        return 'unknown'
    try:
        result = (runner or subprocess.run)(command['argv'] + (['login', 'status'] if name == 'codex' else ['auth', 'status']),
            env=command['env'], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        if name == 'claude':
            logged_in = json.loads(result.stdout).get('loggedIn')
            if logged_in is False:
                return 'expired'
            return 'ok' if result.returncode == 0 and logged_in is True else 'unknown'
        text = (result.stdout + '\n' + result.stderr).lower()
        if any(word in text for word in ('not logged in', 'not signed in', 'login required', 'token expired', 'auth expired')):
            return 'expired'
        return 'ok' if result.returncode == 0 and ('logged in' in text or 'authenticated' in text) else 'unknown'
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
        return 'unknown'
