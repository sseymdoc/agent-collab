"""Resolve service commands once; share argv/environment across consumers."""
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess

HARNESSES = ('codex', 'claude')
CODEX_CLEARED_ENV = ('CODEX_THREAD_ID', 'CODEX_INTERNAL_ORIGINATOR_OVERRIDE')
# Windows 服务由无控制台的 pythonw 运行：短命控制台子进程不加此标志会各自弹出一个窗口（每轮登录探测闪两个）。
NO_WINDOW = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
# macOS 没装开发者工具时，/usr/bin 下的这些命令只是占位程序：一运行，系统就在用户桌面弹出“安装命令行开发者工具”。
XCODE_SHIMS = ('git', 'python3')


def which(name, system=None, runner=None):
    """shutil.which，但把 macOS 上没有开发者工具时的占位程序当作没有（xcode-select -p 只查询，不触发安装提示）。"""
    path = shutil.which(name)
    if path and name in XCODE_SHIMS and (system or platform.system()) == 'Darwin' and path == '/usr/bin/' + name:
        try:
            usable = (runner or subprocess.run)(['/usr/bin/xcode-select', '-p'], capture_output=True, timeout=10,
                                                stdin=subprocess.DEVNULL).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            usable = False
        if not usable:
            # 跳过占位程序所在的 /usr/bin，接着找 PATH 里后面的目录（例如 Homebrew 装的真实工具）。
            # 后台服务（LaunchAgent）的 PATH 不含 Homebrew 目录：与 resolve_commands 一样补上。
            rest = [d for d in os.environ.get('PATH', os.defpath).split(os.pathsep) if d and d.rstrip('/') != '/usr/bin']
            rest += [d for d in ('/opt/homebrew/bin', '/usr/local/bin') if d not in rest]
            return shutil.which(name, path=os.pathsep.join(rest)) if rest else None
    return path


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
            # CreateProcess 只能直接启动这几类；.VBS/.JS/.PS1 与无扩展名脚本（如 npm 的 sh 包装）会 WinError 193。
            direct = ('.com', '.exe', '.bat', '.cmd')
            suffixes = [s for s in environment.get('PATHEXT', '.COM;.EXE;.BAT;.CMD').split(';') if s.lower() in direct]
            suffix = Path(command).suffix.lower()
            names = ([command] if suffix in direct else []) if suffix else [command + s for s in suffixes]
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
    # 原因值会作为 capabilities.harness_disabled 上报，controller 只接受字符串或 null；这里先拒绝，避免节点注册失败。
    if not isinstance(disabled, dict) or any(not isinstance(v, (str, type(None))) for v in disabled.values()):
        raise ValueError('disabled_harnesses must map harness names to a string reason or null')
    if config.get('codex_home') is not None and (not isinstance(config['codex_home'], str) or not config['codex_home']):
        raise ValueError('codex_home must be a nonempty path string')
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
            for key in CODEX_CLEARED_ENV:
                command_env.pop(key, None)
            if config.get('codex_home'):
                command_env['CODEX_HOME'] = config['codex_home']
        result[name] = dict(argv=([path] + (argv[1:] if argv else [])) if path else None,
                            env=command_env, disabled=disabled.get(name))
    return result


def credential_store_readable(command, runner=None, system=None):
    """macOS 上 Claude 的订阅登录存在登录钥匙串里，只有图形登录会话能读。
    SSH 会话实测：`security show-keychain-info` exit 36（User interaction is not allowed），
    同时 `claude auth status` 报 loggedIn=false、authMethod=none。其他平台的凭据是文件，不做此判断。"""
    if (system or platform.system()) != 'Darwin':
        return True
    try:
        # ponytail: 图形会话里钥匙串被手动锁定时，这条命令会弹一次解锁框（最多等 20 秒）；
        # 只在 CLI 已报未登录时才走到这里。真成了干扰再换 SecKeychainGetStatus。
        result = (runner or subprocess.run)(['/usr/bin/security', 'show-keychain-info'], env=command['env'],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def probe_auth_detail(name, command, runner=None, system=None):
    """返回 (ok|expired|unknown, 原因)。expired 只用于有明确依据的未登录或失效；
    当前进程读不到凭据存储时是 unknown + credential_store_unreadable，不据此推断凭据过期。"""
    if not command['argv']:
        return 'unknown', 'command_missing'
    def status():
        return (runner or subprocess.run)(command['argv'] + (['login', 'status'] if name == 'codex' else ['auth', 'status']),
            env=command['env'], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20, **NO_WINDOW)
    try:
        result = status()
        if name == 'claude':
            logged_in = json.loads(result.stdout).get('loggedIn')
            if logged_in is False:
                if not credential_store_readable(command, runner, system):
                    return 'unknown', 'credential_store_unreadable'
                if (system or platform.system()) == 'Darwin':
                    # show-keychain-info 可能刚解锁了被手动锁定的钥匙串，先前的 loggedIn=false 已过时：以重探结果为准。
                    result = status()
                    logged_in = json.loads(result.stdout).get('loggedIn')
                if logged_in is False:
                    return 'expired', 'not_logged_in'
            return ('ok', 'logged_in') if result.returncode == 0 and logged_in is True else ('unknown', 'probe_inconclusive')
        text = (result.stdout + '\n' + result.stderr).lower()
        if any(word in text for word in ('not logged in', 'not signed in', 'login required', 'token expired', 'auth expired')):
            return 'expired', 'not_logged_in'
        if result.returncode == 0 and ('logged in' in text or 'authenticated' in text):
            return 'ok', 'logged_in'
        return 'unknown', 'probe_inconclusive'
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
        return 'unknown', 'probe_failed'


def probe_auth(name, command, runner=None, system=None):
    return probe_auth_detail(name, command, runner, system)[0]
