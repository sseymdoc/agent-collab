"""CLI 与自动化客户端的凭据后端（docs/adr/0002-desktop-and-single-product.md、docs/integration.md）。

system：系统凭据存储。Windows 凭据管理器、macOS 登录钥匙串、Linux Secret Service（secret-tool）。
file：当前用户配置目录里的文件，只有本人可访问；给没有图形会话的部署用。

后端在配对时确定并写进 profile，之后按它读取。两者之间没有自动回退：系统存储不可用时报错并说明
如何显式选择文件后端，不会悄悄把凭据写进文件。凭据值不出现在任何返回值或错误信息里。只用标准库。
"""
import os
import re
import shutil
import subprocess
import sys
import uuid

SERVICE = 'agent-collab-cli'
_TOKEN = re.compile(r'[A-Za-z0-9_\-]{16,512}')  # 客户端凭据是 URL 安全的 base64
_ACCOUNT = re.compile(r'[A-Za-z0-9_.:/\-]{1,200}')


class StoreError(Exception):
    """系统凭据存储不可用或操作失败；消息不含凭据。"""


def _check(account, secret=None):
    if not _ACCOUNT.fullmatch(account) or (secret is not None and not _TOKEN.fullmatch(secret)):
        raise StoreError('credential name or value has an unexpected shape')


# ---------------------------------------------------------------- Windows：凭据管理器

def _win():
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD), ('TargetName', wintypes.LPWSTR),
                    ('Comment', wintypes.LPWSTR), ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                    ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', wintypes.DWORD),
                    ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
                    ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]

    api = ctypes.WinDLL('advapi32', use_last_error=True)
    api.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredFree.argtypes = [ctypes.c_void_p]
    for function in (api.CredWriteW, api.CredReadW, api.CredDeleteW):
        function.restype = wintypes.BOOL
    return ctypes, api, CREDENTIAL


def _win_fail(ctypes, what):
    raise StoreError('%s failed (Windows error %d)' % (what, ctypes.get_last_error()))


def _win_set(target, secret):
    ctypes, api, CREDENTIAL = _win()
    blob = secret.encode('utf-8')
    buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    # CRED_TYPE_GENERIC=1；CRED_PERSIST_LOCAL_MACHINE=2：留在这台机器上这个用户名下，不随漫游配置同步。
    record = CREDENTIAL(Type=1, TargetName=target, CredentialBlobSize=len(blob), CredentialBlob=buffer, Persist=2,
                        UserName='agent-collab', Comment='agent-collab client credential')
    if not api.CredWriteW(ctypes.byref(record), 0):
        _win_fail(ctypes, 'CredWrite')


def _win_get(target):
    ctypes, api, CREDENTIAL = _win()
    pointer = ctypes.POINTER(CREDENTIAL)()
    if not api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        if ctypes.get_last_error() == 1168:  # ERROR_NOT_FOUND
            return None
        _win_fail(ctypes, 'CredRead')
    try:
        return bytes(pointer.contents.CredentialBlob[:pointer.contents.CredentialBlobSize]).decode('utf-8')
    finally:
        api.CredFree(pointer)


def _win_delete(target):
    ctypes, api, _ = _win()
    if not api.CredDeleteW(target, 1, 0) and ctypes.get_last_error() != 1168:
        _win_fail(ctypes, 'CredDelete')


# ---------------------------------------------------------------- macOS：登录钥匙串；Linux：Secret Service

def _run(argv, text_in=None):
    try:
        return subprocess.run(argv, input=text_in, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StoreError('%s could not run (%s)' % (os.path.basename(argv[0]), type(exc).__name__)) from None


SECURITY = '/usr/bin/security'


def _mac_set(account, secret):
    # 经 security 的交互模式从标准输入给出命令：凭据不出现在任何进程的命令行里。
    done = _run([SECURITY, '-i'], 'add-generic-password -U -s %s -a %s -w %s\n' % (SERVICE, account, secret))
    if done.returncode != 0 or _mac_get(account) != secret:
        raise StoreError('the login keychain did not accept the item (is this a graphical login session?)')


def _mac_get(account):
    done = _run([SECURITY, 'find-generic-password', '-s', SERVICE, '-a', account, '-w'])
    if done.returncode == 44:  # 没有这一项
        return None
    if done.returncode != 0:
        raise StoreError('the login keychain is not readable from this session (exit %d)' % done.returncode)
    return done.stdout.rstrip('\n')


def _mac_delete(account):
    done = _run([SECURITY, 'delete-generic-password', '-s', SERVICE, '-a', account])
    if done.returncode not in (0, 44):
        raise StoreError('the login keychain item could not be removed (exit %d)' % done.returncode)


def _secret_tool():
    tool = shutil.which('secret-tool')
    if not tool:
        raise StoreError('secret-tool (libsecret) is not installed')
    return tool


def _linux_set(account, secret):
    done = _run([_secret_tool(), 'store', '--label', 'agent-collab client credential', 'service', SERVICE, 'account', account], secret)
    if done.returncode != 0:
        raise StoreError('the Secret Service did not accept the item (is a keyring daemon running in this session?)')


def _linux_get(account):
    done = _run([_secret_tool(), 'lookup', 'service', SERVICE, 'account', account])
    if done.returncode != 0:
        # secret-tool 对“没有这一项”以非零退出且不输出错误；有错误输出的是服务不可达。
        if done.stderr.strip():
            raise StoreError('the Secret Service is not reachable from this session')
        return None
    return done.stdout.rstrip('\n')


def _linux_delete(account):
    _run([_secret_tool(), 'clear', 'service', SERVICE, 'account', account])


# ---------------------------------------------------------------- 统一入口

def _backend(platform=None):
    platform = platform or sys.platform
    if platform == 'win32':
        target = lambda account: SERVICE + ':' + account
        return (lambda a, s: _win_set(target(a), s)), (lambda a: _win_get(target(a))), (lambda a: _win_delete(target(a)))
    if platform == 'darwin':
        return _mac_set, _mac_get, _mac_delete
    return _linux_set, _linux_get, _linux_delete


def location(account, platform=None):
    """给人看的存放位置；不含凭据。"""
    platform = platform or sys.platform
    place = {'win32': 'Windows 凭据管理器', 'darwin': 'macOS 登录钥匙串'}.get(platform, 'Secret Service（secret-tool）')
    return '%s：%s / %s' % (place, SERVICE, account)


def store(account, secret):
    _check(account, secret)
    _backend()[0](account, secret)


def load(account):
    """返回凭据；系统存储里没有这一项时返回 None。读不了存储时抛 StoreError。"""
    _check(account)
    return _backend()[1](account)


def delete(account):
    _check(account)
    _backend()[2](account)


def probe():
    """配对之前确认系统存储在当前会话里可写可读：写入、读回并删除一项探针。失败抛 StoreError。

    配对码只能用一次，所以要在发出配对请求之前知道凭据有地方放。
    """
    account, value = 'probe/' + uuid.uuid4().hex, 'probe-' + uuid.uuid4().hex
    store(account, value)
    try:
        if load(account) != value:
            raise StoreError('the system credential store did not return what was written')
    finally:
        delete(account)


def file_problem(path):
    """凭据文件的实际访问权限是否只允许当前用户；符合返回 None，否则返回原因。

    每次读取凭据前检查，不凭创建时的设置推定：POSIX 看属主与权限位；Windows 上权限位没有意义，读回 DACL，
    任何当前用户以外的主体（包括继承来的）都算不符合。
    """
    try:
        if os.name != 'nt':
            status = os.stat(str(path))
            if status.st_uid != os.getuid():
                return 'owned by another user'
            if status.st_mode & 0o077:
                return 'mode %o grants access to group or others' % (status.st_mode & 0o777)
            return None
        from .artifacts import _current_user, _principals
        name = _current_user()[0]
        others = sorted({n for n in _principals(path) if n.casefold() != name.casefold()})
        if others:
            return 'the access list also grants: ' + ', '.join(others)
        if not _principals(path):
            return 'the access list grants nobody'
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        return 'permissions could not be read (%s)' % type(exc).__name__
