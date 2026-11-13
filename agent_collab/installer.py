"""Offline releases and conservative install/upgrade transactions (Python 3.9+)."""
import argparse
from contextlib import ExitStack, closing, contextmanager
import hashlib
import datetime
import getpass
import io
import json
import os
import ntpath
from pathlib import Path
import platform
import plistlib
import posixpath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import time
import unicodedata
import uuid
import xml.etree.ElementTree as ET
import zipfile

MANIFEST = 'release-manifest.json'
ROLES = ('controller', 'worker', 'all-in-one')
MAX_RELEASE_BYTES = 512 * 1024 * 1024


def validate_version(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,99}', value):
        raise ValueError('invalid release version')
    # 版本号会成为 versions/<version> 目录名：Windows 设备名（CON、nul.1）与结尾的点不能用。
    try:
        safe_name(value)
    except ValueError:
        raise ValueError('invalid release version') from None
    return value


def safe_name(name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name or '\x00' in name:
        raise ValueError('unsafe release path')
    parts = name.split('/')
    for part in parts:
        if (part in ('', '.', '..') or part.rstrip(' .') != part
                or re.match(r'(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', part)
                or any(ord(c) < 32 or c in '<>"|?*' for c in part)):
            raise ValueError('unsafe release path: ' + name)
    return name


def schema_fingerprint(db):
    rows = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                      "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
    return hashlib.sha256(json.dumps(rows, separators=(',', ':')).encode()).hexdigest()


def validate_payload(files):
    if MANIFEST not in files:
        raise ValueError('release-manifest.json is missing')
    manifest = json.loads(files[MANIFEST])
    validate_version(manifest['version'])
    if manifest.get('python_requires') != '>=3.9':
        raise ValueError('unsupported Python requirement')
    if type(manifest.get('ui_built')) is not bool or manifest['ui_built'] != ('agent_collab/static/index.html' in files):
        raise ValueError('ui_built does not match the release contents')
    low, high = manifest.get('controller_schema_min'), manifest.get('controller_schema_max')
    if type(low) is not int or type(high) is not int or not 0 <= low <= high:
        raise ValueError('release must declare its supported database schema range')
    if not re.fullmatch('[0-9a-f]{64}', manifest.get('controller_schema_fingerprint', '')):
        raise ValueError('release must declare its schema fingerprint')
    declared = manifest.get('files')
    if not isinstance(declared, dict) or set(files) != set(declared) | {MANIFEST}:
        raise ValueError('archive and manifest file lists differ')
    for name, entry in declared.items():
        safe_name(name)
        if name.split('/')[0] not in ('agent_collab', 'installer'):
            raise ValueError('unexpected release root: ' + name)
        if (len(files[name]) != entry.get('size')
                or hashlib.sha256(files[name]).hexdigest() != entry.get('sha256')):
            raise ValueError('release hash/size mismatch: ' + name)
    check_release_tree(declared)
    for required in ('agent_collab/installer.py', 'agent_collab/cli.py', 'installer/entry.py'):
        if required not in declared:
            raise ValueError('incomplete release: ' + required)
    return manifest


def check_release_tree(names):
    """逐段建树检查别名与文件/目录冲突，时间与内存都与路径段总数成线性（不为每个前缀拼接字符串）。

    - 任一级文件或目录名在 NFC 归一并折叠大小写后相同：Windows/macOS 无法同时容纳；
    - 同一路径既是文件又是某个文件的父目录。
    """
    root = {}
    for name in names:
        node, parts = root, name.split('/')
        for index, part in enumerate(parts):
            entry = node.setdefault(unicodedata.normalize('NFC', part).casefold(), [part, {}, False])
            if entry[0] != part:
                raise ValueError('case-colliding release paths')
            last = index == len(parts) - 1
            if entry[1] if last else entry[2]:
                raise ValueError('release file/directory collision')
            entry[2] = entry[2] or last
            node = entry[1]


def read_release(path):
    """No extractall: validate names/types/hash in memory before touching prefix."""
    files, total = {}, 0

    def add(name, size, read):
        nonlocal total
        safe_name(name)
        if name in files:
            raise ValueError('duplicate archive member: ' + name)
        total += size
        if total > MAX_RELEASE_BYTES or len(files) >= 20000:
            raise ValueError('release exceeds size/file limit')
        files[name] = read()

    source = io.BytesIO(path) if isinstance(path, bytes) else str(path)
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            for info in archive.infolist():
                mode = info.external_attr >> 16
                if info.is_dir() or stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    raise ValueError('only regular archive files are supported')
                add(info.filename, info.file_size, lambda i=info: archive.read(i))
    else:
        if isinstance(source, io.BytesIO):
            source.seek(0)
        with tarfile.open(fileobj=source, mode='r:gz') if isinstance(source, io.BytesIO) else tarfile.open(source, 'r:gz') as archive:
            for info in archive:
                if not info.isfile():
                    raise ValueError('only regular archive files are supported')
                add(info.name, info.size, lambda i=info: archive.extractfile(i).read())
    return validate_payload(files), files


def read_installed(directory):
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('version directory is missing or is a symlink')
    files = {}
    for path in directory.rglob('*'):
        relative = path.relative_to(directory)
        # 符号链接先拒绝（含 __pycache__ 里的链接：Python 加载 .pyc 时会跟随它）。
        if path.is_symlink():
            raise ValueError('installed release contains a symlink')
        # 不带 -B 运行（如直接 python -m agent_collab）会生成 __pycache__：解释器缓存不是发行内容，
        # 不能因此让 doctor、同版本重装与回退判定版本目录被改动。
        if '__pycache__' in relative.parts:
            continue
        if path.is_file():
            files[relative.as_posix()] = path.read_bytes()
    return validate_payload(files)


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def atomic_write(path, data, mode=0o600):
    path = Path(path)
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    try:
        with temporary.open('xb') as output:
            if os.name != 'nt':
                os.chmod(str(temporary), mode)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        replace_retrying(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def replace_retrying(source, target, attempts=100, delay=0.05):
    """os.replace；Windows 上目标文件正被别的进程打开（桌面端并发查询本机状态、杀毒软件扫描）时替换会被拒
    （WinError 5/32），安装器不能因此中途失败：稍等再试。M3 干净虚拟机验证中实测：升级记录被并发读取时回退直接退出。"""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if os.name != 'nt' or attempt == attempts - 1:
                raise
            time.sleep(delay)


def write_json(path, value):
    atomic_write(path, (json.dumps(value, indent=2, sort_keys=True) + '\n').encode())


def current_version(prefix, system=None):
    if (system or platform.system()) == 'Windows':
        pointer = prefix / 'current.txt'
        return validate_version(pointer.read_text(encoding='utf-8').strip()) if pointer.exists() else None
    pointer = prefix / 'current'
    if not pointer.is_symlink():
        if pointer.exists():
            raise ValueError('current must be an installer-owned symlink')
        return None
    target = os.readlink(str(pointer))
    version = validate_version(Path(target).name)
    if target != 'versions/' + version:
        raise ValueError('current points outside versions')
    return version


def switch_current(prefix, version, system=None):
    if (system or platform.system()) == 'Windows':
        if version is None:
            (prefix / 'current.txt').unlink(missing_ok=True)
        else:
            atomic_write(prefix / 'current.txt', (version + '\n').encode())
    elif version is None:
        (prefix / 'current').unlink(missing_ok=True)
    else:
        stage = prefix / ('.current-' + uuid.uuid4().hex)
        try:
            stage.symlink_to('versions/' + validate_version(version), target_is_directory=True)
            os.replace(str(stage), str(prefix / 'current'))
        finally:
            if stage.is_symlink():
                stage.unlink()


def check_schema(state, manifest):
    database = state / 'controller' / 'controller.db'
    if not database.exists():
        return 'unknown'
    # sqlite3 连接的 with 只管事务不关闭；Windows 上未关闭的句柄会阻止回退/卸载时替换数据库。
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        version = db.execute('PRAGMA user_version').fetchone()[0]
        compatible = manifest['controller_schema_min'] <= version <= manifest['controller_schema_max']
        # Baseline WP1 has no versioned migrations. Do not infer compatibility from 0 alone.
        if version == 0:
            compatible = compatible and schema_fingerprint(db) == manifest['controller_schema_fingerprint']
    if not compatible:
        raise ValueError('database schema {} is unsupported by {}; refusing code-only rollback. '
                         'Restore a compatible backup with controller restore; restore increments '
                         'authority_generation.'.format(version, manifest['version']))
    return version


# 同 adapters.commands.NO_WINDOW；这里自己定义，安装器模块不在顶层导入包内其他模块。
NO_WINDOW = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}


def run(command, cwd=None, check=True):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    if cwd is not None:
        env['PYTHONPATH'] = str(cwd)
    # errors='replace'：本地化 Windows 的 schtasks/icacls 按控制台代码页（如 936）输出，不是 UTF-8；
    # 严格解码会让读取线程抛异常、stdout 变成 None（经 SSH 或 cmd 调用时实测）。需要解析的输出都是 ASCII。
    # 本机部署向导与本机服务面板经无控制台的助手调用这里（schtasks、icacls、whoami、python）：Windows 上不带
    # CREATE_NO_WINDOW 时每个子进程会各闪一个控制台窗口。
    result = subprocess.run([str(x) for x in command], cwd=str(cwd) if cwd else None, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='replace', **NO_WINDOW)
    if check and result.returncode:
        # Commands can contain one-time codes; never echo argv or credential-bearing output.
        raise RuntimeError('{} failed (exit {}); inspect local service logs'.format(Path(command[0]).name, result.returncode))
    return result


def package_command(config, directory, module, *args, controller_user=False, check=True):
    command = [config['python'], '-B', '-m', 'agent_collab.' + module] + list(args)
    if controller_user and config['system'] == 'Linux' and config['service']:
        command = controller_identity() + command
    return run(command, cwd=directory, check=check)


def admin_token(config):
    supplied = config.get('admin_token_file')
    if supplied:
        return Path(supplied).read_text(encoding='utf-8').strip()
    path = Path(config['state_dir']) / 'controller' / 'admin.token'
    if config['system'] == 'Linux' and config['service'] and config['role'] != 'worker':
        return run(controller_identity() + ['cat', str(path)]).stdout.strip()
    return path.read_text(encoding='utf-8').strip()


def drain_client(config):
    from .client import Client
    agent = read_json(Path(config['state_dir']) / 'worker' / 'agent.json')
    return Client(agent['controller'], admin_token(config), agent.get('ca_file'), timeout=5), agent['node_id']


def get_node(client, node_id):
    from .protocol import check_node_id
    check_node_id(node_id)
    nodes = client.json('GET', '/nodes')['nodes']
    found = next((node for node in nodes if node['node_id'] == node_id), None)
    if found is None:
        raise ValueError('paired node is not registered; cannot prove drain')
    return found


def set_drain(client, node_id, value):
    from .client import APIError
    for attempt in range(3):
        node = get_node(client, node_id)
        if node['drain'] == value:
            return
        try:
            client.json('POST', '/nodes/' + node_id + '/drain',
                        dict(request_id=str(uuid.uuid4()), expected_version=node['version'], drain=value))
            return
        except APIError as exc:
            if exc.code != 'version_mismatch' or attempt == 2:
                raise


def drain_and_wait(client, node_id, timeout, clock=time.monotonic, sleep=time.sleep, before_drain=None):
    original = get_node(client, node_id)['drain']
    if before_drain:
        before_drain(original)
    deadline = clock() + timeout
    unchecked = False
    try:
        set_drain(client, node_id, True)
        while True:
            node = get_node(client, node_id)
            if node.get('drain') is not True:
                raise ValueError('node drain changed concurrently; abort upgrade')
            if 'pending_writebacks' not in node and not unchecked:
                print('Note: pending_writebacks missing; 未核对写回操作 (legacy controller).')
                unchecked = True
            idle_writebacks = ('pending_writebacks' not in node or
                               (type(node['pending_writebacks']) is int and node['pending_writebacks'] == 0))
            if type(node.get('active_attempts')) is int and node['active_attempts'] == 0 and idle_writebacks:
                return original
            if clock() >= deadline:
                raise ValueError('drain timed out; old version remains running')
            sleep(min(1, max(0, deadline - clock())))
    except Exception:
        try:
            set_drain(client, node_id, original)
        except Exception:
            print('Drain restoration failed; node may remain drained. Review controller state.', file=sys.stderr)
        raise


def components(config):
    return ['controller', 'worker'] if config['role'] == 'all-in-one' else [config['role']]


def service_name(config, kind):
    instance = config.get('instance', '')
    if config['system'] == 'Darwin':
        return 'org.agent-collab.' + kind + ('.' + instance if instance else '')
    return 'agent-collab-' + kind + ('-' + instance if instance else '')


def service_file(config, kind):
    if config['system'] == 'Darwin':
        # 缺省实例在 prefix/services 下的旧文件名保持不变。
        return (service_name(config, kind) if config.get('instance') else 'agent-collab-' + kind) + '.plist'
    return service_name(config, kind) + ('.service' if config['system'] == 'Linux' else '.xml')


def desired_state(record, config, autostart=None):
    if record and 'desired' in record:
        desired = record['desired']
        if (not isinstance(desired, dict) or set(desired) != set(components(config))
                or any(value not in ('running', 'stopped') for value in desired.values())):
            raise ValueError('invalid desired service state')
        return dict(desired), 'recorded'
    return {kind: 'stopped' if (autostart or {}).get(kind) == 'disabled' else 'running'
            for kind in components(config)}, 'inferred'


def controller_identity():
    return ['runuser', '-u', 'agent-collab', '--'] if os.getuid() == 0 else ['sudo', '-u', 'agent-collab', '--']


def component_argv(config, kind):
    state = Path(config['state_dir']) / kind
    if kind == 'worker':
        return ['agent_collab.node_agent', 'run', '--data-dir', str(state), '--foreground']
    return ['agent_collab.controller', 'serve', '--data-dir', str(state), '--bind', config['bind'],
            '--public-origin', config['controller_url']]


def systemd_quote(value, command=False):
    text = str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    return '"' + (text.replace('$', '$$') if command else text) + '"'


def worker_python(python, exists=None):
    """Select the console-free interpreter beside the configured interpreter."""
    windows_path = '\\' in str(python)
    sibling = ntpath.join(ntpath.dirname(str(python)), 'pythonw.exe') if windows_path else str(Path(python).with_name('pythonw.exe'))
    return sibling if (exists or Path.is_file)(Path(sibling)) else str(python)


def login_name():
    # 容器里的 uid 可能没有 passwd 条目（getuser 在 3.9 抛 KeyError、3.13 起抛 OSError）；loginctl 也接受 uid。
    # Windows 上 getuser 读 USERNAME 等环境变量，都没有时（3.9 抛 ImportError）没有 uid 可退：明确报错。
    try:
        return getpass.getuser()
    except (KeyError, OSError, ImportError):
        if os.name == 'nt':
            raise ValueError('cannot determine the current Windows user name (USERNAME is not set)') from None
        return str(os.getuid())


def default_windows_workspace(env=None, username=None):
    env = os.environ if env is None else env
    return ntpath.join(env.get('SystemDrive', 'C:') + '\\', 'agent-collab-work', username or login_name())


def workspace_acl_commands(root, user_directory, user_sid, sandbox_group=False):
    """Pure argv construction. RX has no OI/CI: sandbox can traverse only."""
    if not re.fullmatch(r'S-1-(?:[0-9]+-)+[0-9]+', user_sid) or user_sid in ('S-1-5-18', 'S-1-5-19', 'S-1-5-20'):
        raise ValueError('workspace requires the current user SID')
    commands = []
    for path in dict.fromkeys((str(root), str(user_directory))):
        commands.append(['icacls', path, '/reset'])
        grants = ['*' + user_sid + ':(OI)(CI)F', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F']
        if sandbox_group:
            grants.append('CodexSandboxUsers:RX')
        commands.append(['icacls', path, '/inheritance:r', '/grant:r'] + grants)
    return commands


def configure_worker_workspace(config, runner=None):
    if 'worker' not in components(config):
        return
    identity = Path(config['state_dir']) / 'worker/agent.json'
    if not identity.exists():
        return
    data = read_json(identity)
    root = data['workspace_root'] if 'workspace_root' in data else config.get('workspace_root')
    if not root:
        return
    path = Path(root).expanduser().absolute()
    if config['system'] == 'Windows':
        # 只有生成的缺省工作区（%SystemDrive%\agent-collab-work\<用户名>）才改共享父目录的 ACL；
        # 自定义路径即使父目录恰好也叫 agent-collab-work，也属于运维自有目录，只改它自己。
        default = os.path.normcase(default_windows_workspace())
        shared = path.parent if os.path.normcase(str(path)) == default else path
        protected = {Path(path.anchor), Path.home(), Path(config['prefix']), Path(config['state_dir'])}
        if path in protected or shared in protected:
            raise ValueError('workspace ACL requires a dedicated directory, not a drive/profile/code/state root')
        shared.mkdir(parents=True, exist_ok=True)
        path.mkdir(parents=True, exist_ok=True)
        runner = runner or run
        import csv
        sid = next(csv.reader(runner(['whoami', '/user', '/fo', 'csv', '/nh']).stdout.splitlines()))[1]
        group = runner(['net', 'localgroup', 'CodexSandboxUsers'], check=False).returncode == 0
        for command in workspace_acl_commands(shared, path, sid, group):
            runner(command)
    else:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if 'workspace_root' not in data:
        data['workspace_root'] = str(path)
        write_json(identity, data)


def render_services(config, directory, user_sid=None, desired=None):
    """Pure rendering: no service registration and no user/global filesystem writes."""
    prefix, state = Path(config['prefix']), Path(config['state_dir'])
    rendered = {}
    for kind in components(config):
        name = service_file(config, kind)
        argv = [config['python'], '-B', '-m'] + component_argv(config, kind)
        if config['system'] == 'Linux':
            template = (directory / 'installer/services' / ('agent-collab-' + kind + '.service')).read_text(encoding='utf-8')
            # systemd 的 WorkingDirectory= 不做引号解析（带引号会被判为非绝对路径而拒绝加载），
            # 取整行余下部分为路径；只需转义 % 说明符。ECS 实装时发现。
            if '\n' in str(directory):
                raise ValueError('service directory cannot contain a newline')
            values = {'PREFIX': str(prefix), 'VERSION_DIR': str(directory).replace('%', '%%'),
                      'COMMAND': ' '.join(systemd_quote(s, command=True) for s in argv),
                      'CONTROLLER_STATE': systemd_quote(state / 'controller'),
                      # 维护 API 的在线备份写在数据目录旁的 backups（与升级前备份同一处）。
                      'CONTROLLER_BACKUPS': systemd_quote(state / 'backups')}
            for key, value in values.items():
                template = template.replace('@' + key + '@', value)
            rendered[name] = template.encode()
        elif config['system'] == 'Darwin':
            # No UserName: LaunchAgent inherits the logged-in user's identity.
            rendered[name] = plistlib.dumps(dict(
                Label=service_name(config, kind), ProgramArguments=argv, WorkingDirectory=str(directory),
                RunAtLoad=True, KeepAlive={'SuccessfulExit': False}, ThrottleInterval=5,
                StandardOutPath=str(state / 'logs' / (kind + '.out.log')),
                StandardErrorPath=str(state / 'logs' / (kind + '.err.log')),
                EnvironmentVariables={'PYTHONDONTWRITEBYTECODE': '1'}))
        else:
            if not user_sid or user_sid in ('S-1-5-18', 'S-1-5-19', 'S-1-5-20'):
                raise ValueError('Windows tasks require the current interactive user SID')
            task = ET.Element('Task', version='1.2', xmlns='http://schemas.microsoft.com/windows/2004/02/mit/task')
            registration = ET.SubElement(task, 'RegistrationInfo')
            ET.SubElement(registration, 'Description').text = 'agent-collab prefix: ' + str(prefix)
            triggers = ET.SubElement(task, 'Triggers')
            logon = ET.SubElement(triggers, 'LogonTrigger')
            ET.SubElement(logon, 'Enabled').text = 'true'
            ET.SubElement(logon, 'UserId').text = user_sid
            # 进程退出后由这个每分钟重复的时间触发器拉起（IgnoreNew 保证单实例）。实测：登录触发器上的重复
            # 要等下一次登录事件才生效，会话中途注册的任务（每次安装、升级）没有它；RestartOnFailure 不处理进程退出。
            timer = ET.SubElement(triggers, 'TimeTrigger')
            repetition = ET.SubElement(timer, 'Repetition')
            ET.SubElement(repetition, 'Interval').text = 'PT1M'
            ET.SubElement(repetition, 'StopAtDurationEnd').text = 'false'
            ET.SubElement(timer, 'StartBoundary').text = '2020-01-01T00:00:00'
            ET.SubElement(timer, 'Enabled').text = 'true'
            principal = ET.SubElement(ET.SubElement(task, 'Principals'), 'Principal', id='CurrentUser')
            for key, value in [('UserId', user_sid), ('LogonType', 'InteractiveToken'), ('RunLevel', 'LeastPrivilege')]:
                ET.SubElement(principal, key).text = value
            settings = ET.SubElement(task, 'Settings')
            for key, value in [('MultipleInstancesPolicy', 'IgnoreNew'), ('DisallowStartIfOnBatteries', 'false'),
                               ('StopIfGoingOnBatteries', 'false'), ('ExecutionTimeLimit', 'PT0S'),
                               ('Enabled', 'false' if (desired or {}).get(kind) == 'stopped' else 'true')]:
                ET.SubElement(settings, key).text = value
            restart = ET.SubElement(settings, 'RestartOnFailure')
            ET.SubElement(restart, 'Interval').text = 'PT1M'
            ET.SubElement(restart, 'Count').text = '3'
            action = ET.SubElement(ET.SubElement(task, 'Actions', Context='CurrentUser'), 'Exec')
            # 两个组件都用无控制台的 pythonw.exe：python.exe 由计划任务拉起时会新建控制台，Windows 11 上被默认终端接管成
            # 一个可见窗口（M3 联调实测 controller 每次启动都弹出 Windows Terminal），关掉它就结束了服务。日志经 --service-log 写文件。
            ET.SubElement(action, 'Command').text = worker_python(config['python'])
            cli = ['controller' if kind == 'controller' else 'agent'] + component_argv(config, kind)[1:]
            ET.SubElement(action, 'Arguments').text = subprocess.list2cmdline(
                ['-B', str(prefix / 'launcher.py'), '--service-log', str(state / 'logs' / (kind + '.log'))] + cli)
            ET.SubElement(action, 'WorkingDirectory').text = str(prefix)
            rendered[name] = ET.tostring(task, encoding='utf-16', xml_declaration=True)
    return rendered


class Services:
    def __init__(self, config, directory):
        self.config, self.directory = config, directory
        self.enabled = config['service']
        self.system = config['system']
        self.prefix = Path(config['prefix'])
        self.desired = {}

    def privileged(self, command, **kwargs):
        return run(([] if os.getuid() == 0 else ['sudo', '--']) + command, **kwargs)

    def prepare_account(self):
        if not self.enabled or self.system != 'Linux' or 'controller' not in components(self.config):
            return
        import pwd
        try:
            user = pwd.getpwnam('agent-collab')
            if user.pw_uid == 0:
                raise ValueError('agent-collab must be a low-privilege account')
        except KeyError:
            self.privileged(['useradd', '--system', '--user-group', '--no-create-home',
                             '--home-dir', str(Path(self.config['state_dir']) / 'controller'),
                             '--shell', '/usr/sbin/nologin', 'agent-collab'])
        directory = Path(self.config['state_dir']) / 'controller'
        self.privileged(['chown', '-R', '-P', '-h', 'agent-collab:agent-collab', str(directory)])
        self.privileged(['chmod', '700', str(directory)])
        # 服务单元把 backups 列为可写路径：它必须存在且归服务账户所有。
        self.privileged(['install', '-d', '-m', '700', '-o', 'agent-collab', '-g', 'agent-collab',
                         str(Path(self.config['state_dir']) / 'backups')])

    def destinations(self):
        for kind in components(self.config):
            if self.system == 'Linux':
                base = Path('/etc/systemd/system') if kind == 'controller' else Path.home() / '.config/systemd/user'
                yield kind, base / service_file(self.config, kind)
            elif self.system == 'Darwin':
                yield kind, Path.home() / 'Library/LaunchAgents' / (service_name(self.config, kind) + '.plist')
            else:
                yield kind, self.prefix / 'services' / service_file(self.config, kind)

    def check_ownership(self):
        if not self.enabled:
            return
        for kind, owner in self.ownership(strict=True).items():
            if owner not in ('none', 'this_prefix'):
                raise ValueError('service name belongs to another installation or cannot be verified: ' + service_name(self.config, kind))

    def ownership(self, strict=False):
        owners = {}
        for kind, path in self.destinations():
            try:
                if self.system == 'Windows':
                    result = run(['schtasks', '/Query', '/TN', service_name(self.config, kind), '/XML'], check=False)
                    if result.returncode:
                        # “不存在”与查询出错的退出码相同、文字随系统语言变化：列出全部任务确认确实没有它，否则算无法确认。
                        listed = run(['schtasks', '/Query', '/FO', 'CSV', '/NH'], check=False)
                        name = '"\\' + service_name(self.config, kind).lower() + '"'
                        absent = listed.returncode == 0 and not any(line.lower().startswith(name) for line in listed.stdout.splitlines())
                        if not absent and strict:
                            raise ValueError('cannot verify scheduled task ' + service_name(self.config, kind))
                        owners[kind] = 'none' if absent else 'unknown'
                        continue
                    description = ET.fromstring(result.stdout).find('.//{*}Description')
                    owned = description is not None and description.text == 'agent-collab prefix: ' + str(self.prefix)
                elif self.system == 'Darwin':
                    owned = Path(plistlib.loads(path.read_bytes()).get('WorkingDirectory', '/')).parent == self.prefix / 'versions'
                else:
                    owned = path.read_text(encoding='utf-8').splitlines()[0] == '# agent-collab prefix: ' + str(self.prefix)
                owners[kind] = 'this_prefix' if owned else 'other_prefix'
            except FileNotFoundError:
                if strict and self.system == 'Windows':
                    raise
                owners[kind] = 'unknown' if self.system == 'Windows' else 'none'
            except (OSError, ValueError, TypeError, IndexError, ET.ParseError):
                if strict:
                    raise
                owners[kind] = 'unknown'
        return owners

    def render(self):
        user_sid = None
        if self.system == 'Windows':
            import csv
            user_sid = next(csv.reader(run(['whoami', '/user', '/fo', 'csv', '/nh']).stdout.splitlines()))[1]
        result = render_services(self.config, self.directory, user_sid, self.desired)
        folder = self.prefix / 'services'
        folder.mkdir(exist_ok=True)
        for name, content in result.items():
            atomic_write(folder / name, content, 0o644)

    def register(self):
        self.render()
        if not self.enabled:
            return
        self.check_ownership()
        for kind, target in self.destinations():
            source = self.prefix / 'services' / service_file(self.config, kind)
            if self.system == 'Linux':
                if kind == 'controller':
                    self.privileged(['install', '-m', '644', str(source), str(target)])
                    self.privileged(['systemctl', 'daemon-reload'])
                    self.privileged(['systemctl', 'disable' if self.desired.get(kind) == 'stopped' else 'enable', target.name])
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write(target, source.read_bytes(), 0o644)
                    run(['systemctl', '--user', 'daemon-reload'])
                    run(['systemctl', '--user', 'disable' if self.desired.get(kind) == 'stopped' else 'enable', target.name])
            elif self.system == 'Darwin':
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(target, source.read_bytes(), 0o644)
                if self.desired.get(kind) == 'stopped':
                    run(['launchctl', 'disable', 'gui/' + str(os.getuid()) + '/' + service_name(self.config, kind)])
            else:
                run(['schtasks', '/Create', '/TN', service_name(self.config, kind), '/XML', str(source), '/F'])

    def stop(self, disable_autostart=False):
        if not self.enabled:
            return
        for kind, target in reversed(list(self.destinations())):
            if self.system == 'Linux':
                action = ['disable', '--now'] if disable_autostart else ['stop']
                if kind == 'controller':
                    self.privileged(['systemctl'] + action + [target.name])
                else:
                    run(['systemctl', '--user'] + action + [target.name])
            elif self.system == 'Darwin':
                label = 'gui/' + str(os.getuid()) + '/' + service_name(self.config, kind)
                if disable_autostart:
                    run(['launchctl', 'disable', label])
                run(['launchctl', 'bootout', label], check=False)
            else:
                # 结束前总是先禁用：否则每分钟的触发器可能在结束进程与安装器取得状态锁之间把它拉起。
                # start() 与期望运行的重新注册会恢复启用。
                run(['schtasks', '/Change', '/TN', service_name(self.config, kind), '/DISABLE'], check=disable_autostart)
                run(['schtasks', '/End', '/TN', service_name(self.config, kind)], check=False)

    def start(self):
        for kind, target in self.destinations():
            if self.desired.get(kind) == 'stopped':
                print('kept stopped: ' + kind)
                continue
            if not self.enabled:
                continue
            if self.system == 'Linux':
                if kind == 'controller':
                    self.privileged(['systemctl', 'enable', '--now', target.name])
                else:
                    run(['systemctl', '--user', 'enable', '--now', target.name])
            elif self.system == 'Darwin':
                domain = 'gui/' + str(os.getuid())
                label = domain + '/' + service_name(self.config, kind)
                run(['launchctl', 'enable', label])
                if run(['launchctl', 'print', label], check=False).returncode == 0:
                    run(['launchctl', 'kickstart', label])
                else:
                    run(['launchctl', 'bootstrap', domain, str(target)])
            else:
                run(['schtasks', '/Change', '/TN', service_name(self.config, kind), '/ENABLE'])
                run(['schtasks', '/Run', '/TN', service_name(self.config, kind)])

    def remove(self):
        if not self.enabled:
            return
        self.check_ownership()
        self.stop()
        for kind, target in self.destinations():
            if self.system == 'Linux':
                if kind == 'controller':
                    self.privileged(['systemctl', 'disable', target.name])
                    self.privileged(['rm', '--', str(target)])
                    self.privileged(['systemctl', 'daemon-reload'])
                else:
                    run(['systemctl', '--user', 'disable', target.name])
                    target.unlink(missing_ok=True)
                    run(['systemctl', '--user', 'daemon-reload'])
            elif self.system == 'Darwin':
                target.unlink(missing_ok=True)
            elif self.ownership().get(kind) != 'none':  # 被中断的卸载可能已经删掉了它
                run(['schtasks', '/Delete', '/TN', service_name(self.config, kind), '/F'])

    def status(self):
        result = {}
        for kind, target in self.destinations():
            if not self.enabled:
                result[kind] = 'unknown'
                continue
            if self.system == 'Linux':
                command = ['systemctl'] + (['--user'] if kind == 'worker' else []) + ['is-active', target.name]
            elif self.system == 'Darwin':
                command = ['launchctl', 'print', 'gui/' + str(os.getuid()) + '/' + service_name(self.config, kind)]
            else:
                command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                           "(Get-ScheduledTask -TaskName '" + service_name(self.config, kind) + "' -ErrorAction Stop).State"]
            try:
                response = run(command, check=False)
                text = response.stdout.strip()
                if response.returncode:
                    # systemctl is-active 用非零退出码报告 inactive/failed：保留这个状态词；其余查询失败为未知。
                    result[kind] = text if self.system == 'Linux' and text and text != 'active' else 'unknown'
                    continue
                if self.system == 'Darwin':
                    result[kind] = 'active' if 'state = running' in text else 'unknown'
                else:
                    result[kind] = 'active' if text.lower() in ('active', 'running') else (text or 'unknown')
            except OSError:
                result[kind] = 'unknown'
        return result

    def autostart(self):
        result = {}
        for kind, target in self.destinations():
            result[kind] = 'unknown'
            if not self.enabled:
                continue
            if self.system == 'Linux':
                command = ['systemctl'] + (['--user'] if kind == 'worker' else []) + ['is-enabled', target.name]
            elif self.system == 'Darwin':
                command = ['launchctl', 'print-disabled', 'gui/' + str(os.getuid())]
            else:
                command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                           "(Get-ScheduledTask -TaskName '" + service_name(self.config, kind) + "' -ErrorAction Stop).State"]
            try:
                response = run(command, check=False)
                text = response.stdout.strip()
                if self.system == 'Linux':
                    # is-enabled exits nonzero for a disabled unit.
                    if text == 'disabled':
                        result[kind] = 'disabled'
                    elif response.returncode == 0 and text == 'enabled':
                        result[kind] = 'enabled'
                elif response.returncode == 0:
                    if self.system == 'Darwin' and 'disabled services = {' in text:
                        # 旧版 macOS 输出 true/false，新版（实测 27.2）输出 disabled/enabled。
                        match = re.search(r'"' + re.escape(service_name(self.config, kind)) + r'"\s*=>\s*(true|false|disabled|enabled)', text)
                        # No override means enabled in this launchd domain.
                        result[kind] = 'disabled' if match and match[1] in ('true', 'disabled') else 'enabled'
                    elif self.system == 'Windows':
                        if text.lower() == 'disabled':
                            result[kind] = 'disabled'
                        elif text.lower() in ('ready', 'running', 'queued'):
                            result[kind] = 'enabled'
            except OSError:
                pass
        return result


def service_context(config):
    result = {}
    for kind in components(config):
        system = config['system']
        manager = ('task-scheduler' if system == 'Windows' else 'launchd' if system == 'Darwin'
                   else 'systemd-system' if kind == 'controller' and config['service'] else 'systemd-user')
        unattended = manager == 'systemd-system'
        if manager == 'systemd-user':
            try:
                response = run(['loginctl', 'show-user', login_name(), '--property=Linger', '--value'], check=False)
                unattended = response.returncode == 0 and response.stdout.strip() == 'yes'
            except OSError:
                pass
        result[kind] = dict(manager=manager, scope='system' if manager == 'systemd-system' else 'user-session',
                            runs_without_login=unattended)
    return result


def discover(config, old):
    prefix = Path(config['prefix'])
    installed = bool(old and old.get('version'))
    # 未安装时同时查看两个组件的名字，候选前缀也能发现与正式服务的冲突。
    probe_config = config if installed else dict(config, role='all-in-one', service=True)
    manager = Services(probe_config, prefix / 'versions' / ((old or {}).get('version') or 'pending'))
    autostart = manager.autostart() if installed else {}
    desired, source = desired_state(old, config, autostart) if installed else ({}, None)
    identity = None
    state = Path(config['state_dir'])
    if installed and 'worker' in components(config):
        try:
            agent = read_json(state / 'worker/agent.json')
            identity = dict(node_id=agent.get('node_id'), controller=agent.get('controller'),
                            paired=(state / 'worker/node.token').is_file())
        except (OSError, ValueError, AttributeError):
            pass
    controller_data = None
    if installed:
        try:
            controller_data = (state / 'controller/controller.db').is_file()
        except OSError:
            controller_data = False
    return dict(prefix=str(prefix), installed=installed, version=old['version'] if installed else None,
                previous_version=(old.get('previous') or {}).get('version') if installed else None,
                role=config['role'] if installed else None, instance=config.get('instance', '') if installed else None,
                service=config['service'] if installed else None, desired=desired, desired_source=source,
                services=manager.status() if installed else {}, autostart=autostart, service_owner=manager.ownership(),
                runtime=config.get('runtime') if installed else None, python=config['python'] if installed else None,
                state_dir=str(state) if installed else None, identity=identity,
                controller_data=controller_data,
                context=service_context(probe_config), interrupted=interrupted_update(config, old))


def write_launchers(config):
    prefix = Path(config['prefix'])
    # This launcher reads current.txt on Windows on every start, including scheduled tasks.
    source = '''from pathlib import Path
import json, os, sys
sys.dont_write_bytecode = True
prefix = Path(__file__).resolve().parent
if os.name == 'nt':
    version = (prefix / 'current.txt').read_text(encoding='utf-8').strip()
    if not version or Path(version).name != version or version in ('.', '..'):
        raise SystemExit('Invalid current.txt')
    current = prefix / 'versions' / version
else:
    current = (prefix / 'current').resolve(strict=True)
if current.parent != prefix / 'versions':
    raise SystemExit('Current version is outside prefix')
if sys.argv[1:2] == ['--service-log']:
    log = open(sys.argv[2], 'a', buffering=1, encoding='utf-8')
    sys.stdout = sys.stderr = log
    del sys.argv[1:3]
os.chdir(str(current))
sys.path.insert(0, str(current))
from agent_collab.cli import main
sys.exit(main())
'''
    atomic_write(prefix / 'launcher.py', source.encode(), 0o644)
    folder = prefix / 'bin'
    folder.mkdir(exist_ok=True)
    if config['system'] == 'Windows':
        # .ps1 avoids cmd.exe percent/ampersand expansion in user-selected paths.
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        text = '& {} -B {} @args\nexit $LASTEXITCODE\n'.format(quote(config['python']), quote(prefix / 'launcher.py'))
        # 带 BOM：Windows PowerShell 5.1 没有 BOM 时按 ANSI 代码页读脚本，路径里的非 ASCII 字符会变成乱码。
        atomic_write(folder / 'agent-collab.ps1', text.encode('utf-8-sig'), 0o644)
    else:
        import shlex
        text = '#!/bin/sh\nexec {} -B {} "$@"\n'.format(shlex.quote(config['python']), shlex.quote(str(prefix / 'launcher.py')))
        atomic_write(folder / 'agent-collab', text.encode(), 0o755)


def compatible_schema(config, manifest, directory):
    if config['system'] == 'Linux' and config['service'] and 'controller' in components(config):
        schema = {key: manifest[key] for key in ('version', 'controller_schema_min', 'controller_schema_max', 'controller_schema_fingerprint')}
        result = package_command(config, directory, 'installer', '_schema', '--state-dir', config['state_dir'],
                                 '--manifest', json.dumps(schema), controller_user=True)
        return json.loads(result.stdout)
    return check_schema(Path(config['state_dir']), manifest)


def doctor(config, offline=False, desired=None):
    prefix = Path(config['prefix'])
    report = dict(ok=False, current='unknown', manifest='unknown', schema='unknown',
                  services={kind: 'unknown' for kind in components(config)}, components={})
    try:
        if desired is None and (prefix / 'installation.json').is_file():
            recorded = read_json(prefix / 'installation.json')
            desired, _ = desired_state(recorded, recorded['config'])
            desired = {kind: desired[kind] for kind in components(config)}
        version = current_version(prefix, config['system'])
        report['current'] = version or 'unknown'
        if not version:
            return report
        directory = prefix / 'versions' / version
        manifest = read_installed(directory)
        if manifest['version'] != version:
            raise ValueError('current directory and manifest versions differ')
        report['manifest'] = 'verified'
        report['ui_built'] = manifest['ui_built']
        report['schema'] = compatible_schema(config, manifest, directory) if 'controller' in components(config) else 'unknown'
        services = Services(config, directory)
        report['services'] = services.status() if not offline else report['services']
        healthy = True
        for kind in components(config):
            if offline or (desired or {}).get(kind) == 'stopped':
                module = 'controller' if kind == 'controller' else 'node_agent'
                # Import from the immutable target directory with this installation's interpreter.
                # No command entry point, data initialisation, service start or socket binding.
                run([config['python'], '-B', '-c',
                     'import agent_collab.' + module], cwd=directory)
                report['components'][kind] = dict(status='offline', imports='verified', runtime='unknown')
                continue
            try:
                if kind == 'controller':
                    from .client import Client
                    address = 'http://' + config['bind']
                    result = Client(address, admin_token(config), timeout=3).json('GET', '/doctor')
                    healthy = healthy and result['database']['writable'] and result['blob_writable']
                else:
                    response = package_command(config, directory, 'node_agent', 'doctor', '--data-dir', str(Path(config['state_dir']) / 'worker'))
                    result = json.loads(response.stdout)
                    healthy = healthy and result.get('ok') is True
                report['components'][kind] = result
            except Exception as exc:
                report['components'][kind] = {'status': 'unknown', 'error_type': type(exc).__name__}
                healthy = False
        report['ok'] = healthy and (offline or not config['service'] or all(
            state == 'active' or (desired or {}).get(kind) == 'stopped'
            for kind, state in report['services'].items()))
    except Exception as exc:
        report['error_type'] = type(exc).__name__
    return report


def wait_doctor(config, timeout, desired=None):
    deadline = time.monotonic() + timeout
    while True:
        report = doctor(config, offline=not config['service'] or all(
            (desired or {}).get(kind) == 'stopped' for kind in components(config)), desired=desired)
        if report['ok'] or time.monotonic() >= deadline:
            print(json.dumps(report, ensure_ascii=False))
            if not report['ok']:
                raise RuntimeError('post-install doctor failed')
            return report
        time.sleep(1)


def refuse_active_worker(config, allow_active=False):
    if allow_active or 'worker' not in components(config):
        return
    for path in (Path(config['state_dir']) / 'worker/attempts').glob('*/meta.json'):
        if read_json(path).get('state') in ('preparing', 'running'):
            raise ValueError('Worker has preparing/running attempts; drain first, or explicitly pass --allow-active')
    counts = {}
    for path in (Path(config['state_dir']) / 'worker/operations').glob('*.json'):
        try:
            state = read_json(path).get('state')
        except (OSError, ValueError, AttributeError):
            raise ValueError('Worker writeback record unreadable; cannot prove idle') from None
        if state in ('claimed', 'queued', 'running', 'finished'):
            counts[state] = counts.get(state, 0) + 1
    if counts:
        raise ValueError('Worker has {} pending writebacks ({}); drain first, or explicitly pass --allow-active'.format(
            sum(counts.values()), ', '.join('{}={}'.format(k, v) for k, v in sorted(counts.items()))))


def wait_worker_status(config, directory, manifest, started_at, timeout=60, clock=time.monotonic, sleep=time.sleep):
    """Require a fresh heartbeat from the actual new code, not doctor reachability."""
    deadline = clock() + max(0, timeout)
    while True:
        try:
            status = read_json(Path(config['state_dir']) / 'worker/status.json')
            heartbeat = datetime.datetime.fromisoformat(status['last_heartbeat_ok_at'].replace('Z', '+00:00')).timestamp()
            if (Path(status['version_dir']).resolve() == Path(directory).resolve()
                    and status['version'] == manifest['version'] and status['git_commit'] == manifest.get('git_commit')
                    and type(status['pid']) is int and status['pid'] > 0 and heartbeat >= started_at):
                return status
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            pass
        if clock() >= deadline:
            raise RuntimeError('new worker did not report a successful heartbeat from its version directory before the timeout')
        sleep(min(1, max(0, deadline - clock())))


@contextmanager
def stopped_locks(config, code_directory=None, timeout=30):
    """Prove manual/managed processes have released their state before switching."""
    from .controller import ControllerLock
    from .node_agent import AgentLock
    with ExitStack() as stack:
        deadline = time.monotonic() + timeout
        state = Path(config['state_dir'])
        for kind in components(config):
            directory = state / kind
            if not directory.exists():
                continue
            if kind == 'controller' and config['system'] == 'Linux' and config['service']:
                # Keep the low-privilege process holding its lock until the whole switch completes.
                script = ('import sys; from agent_collab.controller import ControllerLock; '
                          'from agent_collab.installer import wait_lock; '
                          'lock=wait_lock(lambda: ControllerLock(' + repr(str(directory)) + '), timeout=' +
                          repr(max(0, deadline - time.monotonic())) + '); '
                          'print("locked", flush=True); sys.stdin.read(); lock.__exit__()')
                current = current_version(Path(config['prefix']), config['system'])
                code = code_directory or (Path(config['prefix']) / 'versions' / current)
                child = subprocess.Popen(controller_identity() + [config['python'], '-B', '-c', script],
                    cwd=str(code),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=''))
                try:
                    if child.stdout.readline().strip() != 'locked':
                        child.communicate()
                        raise ValueError('controller state lock is held or inaccessible')
                except BaseException:
                    if child.poll() is None:
                        child.kill()
                        child.communicate()
                    raise
                def release(process):
                    try:
                        process.communicate(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                        raise
                    if process.returncode:
                        raise RuntimeError('controller lock helper failed')
                stack.callback(release, child)
            else:
                lock = wait_lock(lambda: ControllerLock(directory) if kind == 'controller' else AgentLock(directory),
                                 timeout=max(0, deadline - time.monotonic()))
                stack.callback(lock.__exit__, None, None, None)
        yield


def wait_lock(factory, timeout=30, clock=time.monotonic, sleep=time.sleep):
    """停服后进程退出是异步的（Windows schtasks /End 尤其如此）：限时轮询，锁释放才算已停止。"""
    deadline = clock() + timeout
    while True:
        lock = factory()
        try:
            lock.__enter__()
            return lock
        except (ValueError, RuntimeError):  # AgentLock 抛 ValueError，ControllerLock 抛 RuntimeError
            if clock() >= deadline:
                raise
            sleep(min(.5, max(0, deadline - clock())))


def backup_controller(config, directory):
    if 'controller' not in components(config):
        return
    # Backups remain with controller-owned data, but outside its database directory.
    state = Path(config['state_dir'])
    backup = state / 'backups' / ('upgrade-' + time.strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8])
    if config['system'] == 'Linux' and config['service']:
        Services(config, directory).privileged(['install', '-d', '-m', '700', '-o', 'agent-collab',
                                               '-g', 'agent-collab', str(state / 'backups')])
    if not config['service'] and not (state / 'controller/controller.db').exists():
        return
    package_command(config, directory, 'controller', 'backup', '--data-dir', str(state / 'controller'),
                    '--dest', str(backup), controller_user=True)
    print('Controller backup: ' + str(backup))


def ensure_state(config, directory, manifest):
    state = Path(config['state_dir'])
    state.mkdir(parents=True, exist_ok=True, mode=0o711)
    for kind in components(config):
        (state / kind).mkdir(exist_ok=True, mode=0o700)
    (state / 'logs').mkdir(exist_ok=True, mode=0o700)
    manager = Services(config, directory)
    manager.prepare_account()
    if 'controller' in components(config):
        # 新前缀或卸载后重装时没有旧安装记录，但可能保留着旧数据库：先确认目标版本支持它，
        # 再运行目标代码初始化（admin-token 会打开数据库并写事件）。
        # 不在这里 exists()：安装器身份可能进不去 0700 的 controller 目录（3.9 抛 PermissionError，3.14 返回 False 而跳过检查）。
        # compatible_schema 在受管 Linux 上以 controller 账户检查，数据库不存在时返回 unknown。
        compatible_schema(config, manifest, directory)
        package_command(config, directory, 'controller', 'admin-token', '--data-dir', str(state / 'controller'), controller_user=True)


def pair_worker(config, directory, pairing_code):
    if 'worker' not in components(config):
        return
    state = Path(config['state_dir'])
    identity = [state / 'worker/agent.json', state / 'worker/node.token']
    if any(path.exists() for path in identity):
        if not all(path.is_file() for path in identity):
            raise ValueError('partial node identity exists; refusing to overwrite it')
        return
    if not pairing_code:
        print('Worker is not paired. Generate a one-time code on the controller and rerun with --pairing-code.')
        return
    args = ['pair', '--controller', config['controller_url'], '--code', pairing_code,
            '--node-id', config['node_id'], '--data-dir', str(state / 'worker')]
    for key in ('ca_file', 'physical_host_id', 'failure_domain'):
        if config.get(key):
            args += ['--' + key.replace('_', '-'), config[key]]
    result = package_command(config, directory, 'node_agent', *args, check=False)
    if result.returncode:
        # 参数里有一次性配对码，不能回显命令行；节点侧给出的错误体已经过筛选，只取其中的 message。
        try:
            reason = json.loads(result.stdout)['error']['message']
        except (ValueError, KeyError, TypeError):
            reason = 'exit {}'.format(result.returncode)
        raise RuntimeError('worker pairing failed: {}'.format(reason))


def install_version(prefix, manifest, files):
    versions = prefix / 'versions'
    versions.mkdir(exist_ok=True)
    if os.name != 'nt':
        os.chmod(str(versions), 0o755)
    directory = versions / manifest['version']
    if directory.exists():
        if read_installed(directory) != manifest:
            raise ValueError('version already exists with different contents; use a new version')
        return directory
    stage = versions / ('.staging-' + uuid.uuid4().hex)
    stage.mkdir(mode=0o755)
    try:
        for name, content in files.items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, content, 0o755 if name.endswith('.sh') else 0o644)
        if os.name != 'nt':
            for folder in [stage] + [path for path in stage.rglob('*') if path.is_dir()]:
                os.chmod(str(folder), 0o755)
        os.replace(str(stage), str(directory))
    finally:
        if stage.exists():
            shutil.rmtree(str(stage))
    return directory


RUNTIME_PYTHONS = ('python/bin/python3', 'bin/python3', 'python/python.exe', 'python.exe', 'Scripts/python.exe')


def read_runtime(source):
    """先检查完整目录树与链接关系，拒绝路径穿越后才允许写入 staging。"""
    entries = {}

    def add(name, kind, mode, content=b''):
        while name.startswith('./'):
            name = name[2:]
        name = name.rstrip('/') if kind == 'dir' else name
        if name in ('', '.') and kind == 'dir':
            return
        safe_name(name)
        if name in entries:
            raise ValueError('duplicate runtime member: ' + name)
        entries[name] = (kind, mode & 0o777, content)

    if source.is_dir():
        for path in sorted(source.rglob('*')):
            mode = path.lstat().st_mode
            name = path.relative_to(source).as_posix()
            if stat.S_ISLNK(mode):
                add(name, 'link', mode, os.readlink(str(path)).encode())
            elif stat.S_ISDIR(mode):
                add(name, 'dir', mode)
            elif stat.S_ISREG(mode):
                add(name, 'file', mode, path.read_bytes())
            else:
                raise ValueError('unsupported runtime file type')
        # 种类与权限位也参与 ID：同名同内容、但链接/目录或可执行位不同的两棵树不能共用一个已安装的运行时。
        inventory = [[name, kind, mode, len(content), hashlib.sha256(content).hexdigest()]
                     for name, (kind, mode, content) in sorted(entries.items())]
        digest = hashlib.sha256(json.dumps(inventory, separators=(',', ':')).encode()).hexdigest()
    else:
        # 对同一份字节求 ID 并解包，避免归档在两次读取之间被替换。
        archive_bytes = source.read_bytes()
        digest = hashlib.sha256(archive_bytes).hexdigest()
        import io
        if source.name.endswith('.zip'):
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                for info in archive.infolist():
                    mode = info.external_attr >> 16
                    if info.is_dir():
                        add(info.filename, 'dir', mode or 0o755)
                    elif stat.S_ISLNK(mode):
                        add(info.filename, 'link', mode, archive.read(info))
                    elif stat.S_IFMT(mode) in (0, stat.S_IFREG):
                        add(info.filename, 'file', mode or 0o644, archive.read(info))
                    else:
                        raise ValueError('unsupported runtime archive member')
        elif source.name.endswith(('.tar.gz', '.tgz')):
            with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode='r:gz') as archive:
                for info in archive:
                    if info.isdir():
                        add(info.name, 'dir', info.mode)
                    elif info.isfile():
                        add(info.name, 'file', info.mode, archive.extractfile(info).read())
                    elif info.issym() or info.islnk():
                        add(info.name, 'link' if info.issym() else 'hardlink', info.mode, info.linkname.encode())
                    else:
                        raise ValueError('unsupported runtime archive member')
        else:
            raise ValueError('--runtime requires a directory, .tar.gz, .tgz or .zip')
    aliases = {}
    for name in entries:
        parts = name.split('/')
        for index in range(1, len(parts) + 1):
            parent = '/'.join(parts[:index])
            folded = unicodedata.normalize('NFC', parent).casefold()
            if aliases.setdefault(folded, parent) != parent:
                raise ValueError('case-colliding runtime paths')
            if index < len(parts) and parent in entries and entries[parent][0] != 'dir':
                raise ValueError('runtime file/directory collision')

    def resolve(name, seen=()):
        parts = name.split('/')
        for index in range(1, len(parts) + 1):
            parent = '/'.join(parts[:index])
            entry = entries.get(parent)
            if entry and entry[0] in ('link', 'hardlink'):
                if parent in seen:
                    raise ValueError('cyclic runtime link')
                target = entry[2].decode('utf-8')
                if not target or '\\' in target or ':' in target or '\x00' in target or target.startswith('/'):
                    raise ValueError('unsafe runtime link')
                base = posixpath.dirname(parent) if entry[0] == 'link' else ''
                target = posixpath.normpath(posixpath.join(base, target, *parts[index:]))
                if target == '..' or target.startswith('../'):
                    raise ValueError('runtime link escapes runtime directory')
                return resolve(target, seen + (parent,))
        return name

    for name, (kind, mode, content) in entries.items():
        if kind in ('link', 'hardlink'):
            target = resolve(name)
            if kind == 'hardlink' and (target not in entries or entries[target][0] != 'file'):
                raise ValueError('runtime hardlink must target a regular file')
    python = next((name for name in RUNTIME_PYTHONS if name in entries), None)
    if python is None or entries.get(resolve(python), ('missing',))[0] != 'file':
        raise ValueError('runtime Python interpreter is missing')
    return digest[:16], entries, python


def probe_python(python, managed=False):
    script = 'import sys; print("%d.%d" % sys.version_info[:2]); sys.exit(sys.version_info < (3,9))'
    if managed:
        script = 'import ssl, sqlite3, ctypes, json; ' + script
    return run([python, '-B', '-c', script])


def install_runtime(config, payload):
    runtime_id, entries, python = payload
    root = Path(config['prefix']) / 'runtime'
    target = root / runtime_id
    if target.is_symlink():
        raise ValueError('runtime directory must not be a symlink')
    if target.exists():
        executable = target / python
        if target.resolve() not in executable.resolve().parents:
            raise ValueError('runtime interpreter escapes runtime directory')
        probe_python(str(executable), managed=True)
        return
    root.mkdir(exist_ok=True)
    stage = root / ('.staging-' + uuid.uuid4().hex)
    stage.mkdir(mode=0o755)
    try:
        for name, (kind, mode, content) in entries.items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if kind == 'dir':
                path.mkdir(exist_ok=True)
            elif kind == 'file':
                atomic_write(path, content, mode)
        # 所有普通文件先落盘，写文件时绝不跟随归档内链接。
        for name, (kind, mode, content) in entries.items():
            if kind == 'link':
                (stage / name).symlink_to(content.decode('utf-8'))
        pending = {name: content for name, (kind, mode, content) in entries.items() if kind == 'hardlink'}
        while pending:
            completed = []
            for name, content in pending.items():
                source = (stage / content.decode('utf-8')).resolve()
                if stage.resolve() not in source.parents:
                    raise ValueError('runtime hardlink escapes runtime directory')
                if source.is_file():
                    os.link(str(source), str(stage / name))
                    completed.append(name)
            if not completed:
                raise ValueError('unresolved runtime hardlink')
            for name in completed:
                del pending[name]
        for name, (kind, mode, content) in entries.items():
            if kind == 'link':
                resolved = (stage / name).resolve()
                if resolved != stage.resolve() and stage.resolve() not in resolved.parents:
                    raise ValueError('runtime link escapes runtime directory')
        if os.name != 'nt':
            # systemd controller 以专用账户运行，不能继承安装者的私有目录 umask。
            for path in [stage] + [path for path in stage.rglob('*') if path.is_dir() and not path.is_symlink()]:
                path.chmod(0o755)
        probe_python(str(stage / python), managed=True)
        os.replace(str(stage), str(target))
        try:
            probe_python(str(target / python), managed=True)
        except Exception:
            shutil.rmtree(str(target))
            raise
    finally:
        if stage.exists():
            shutil.rmtree(str(stage))
        if not any(root.iterdir()):
            root.rmdir()


def cleanup_runtimes(prefix, record):
    retained = set()
    for item in (record, record.get('previous')):
        if item:
            config = item['config']
            runtime = config.get('runtime') or {}
            if runtime.get('managed'):
                retained.add(runtime['id'])
            # 即使旧记录没有 runtime 元数据，也保留解释器实际所在的受管目录。
            try:
                retained.add(Path(config['python']).relative_to(prefix / 'runtime').parts[0])
            except (ValueError, IndexError):
                pass
    try:
        for path in (prefix / 'runtime').glob('*'):
            if re.fullmatch('[0-9a-f]{16}', path.name) and path.name not in retained and not path.is_symlink():
                shutil.rmtree(str(path))
    except OSError:
        print('Warning: unused runtime cleanup failed; retained for later review.', file=sys.stderr)


OPENSSH_GUIDANCE = ('release signatures are checked with "ssh-keygen -Y verify" from OpenSSH 8.1 or later. On Windows enable the '
                    'built-in optional feature "OpenSSH Client" (Settings > System > Optional features); if its version '
                    '(ssh -V) is older than 8.1, install the client from Microsoft\'s Win32-OpenSSH release MSI '
                    '(msiexec /i <OpenSSH-Win64.msi> ADDLOCAL=Client, installed to %ProgramFiles%\\OpenSSH). '
                    'macOS ships /usr/bin/ssh-keygen')
# 不认识 -Y 的旧版 ssh-keygen（OpenSSH 8.1 之前）：getopt 报未知选项并打印用法。
UNSUPPORTED = re.compile(r'(unknown|illegal|invalid) option -- ?Y|usage: ssh-keygen', re.I)


class VerifierUnavailable(ValueError):
    """这个 ssh-keygen 不能用来验签（不支持 -Y verify，或无法运行）；与签名不符分开，后者说明包或身份不对。"""


def ssh_keygen_candidates():
    """依次尝试的 ssh-keygen。Windows：系统自带的 OpenSSH 客户端（PATH 里可能先排着 Git 的 MSYS 版），
    再是 Microsoft Win32-OpenSSH 安装包的位置（它不加进 PATH），最后按 PATH；其他平台按 PATH。"""
    found = []
    if os.name == 'nt':
        for base in (Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'OpenSSH',
                     Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'OpenSSH'):
            if (base / 'ssh-keygen.exe').is_file():
                found.append(str(base / 'ssh-keygen.exe'))
    on_path = shutil.which('ssh-keygen')
    if on_path and os.path.normcase(os.path.abspath(on_path)) not in {os.path.normcase(os.path.abspath(p)) for p in found}:
        found.append(on_path)
    return found


def openssh_version(executable):
    """同目录 ssh -V 的输出，只用于说明（ssh-keygen 自己没有版本选项）。"""
    ssh = Path(executable).with_name('ssh' + Path(executable).suffix)
    try:
        result = subprocess.run([str(ssh), '-V'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, **NO_WINDOW)
        return (result.stderr or result.stdout).decode('utf-8', 'replace').strip().splitlines()[0][:80]
    except (OSError, IndexError, subprocess.SubprocessError):
        return None


def verify_signature(executable, archive, signature, signers):
    """核对的正是已经读入内存的字节：签名与 allowed_signers 写进私有临时目录再交给 ssh-keygen。
    这也避开了 ssh-keygen 打不开的路径写法（例如桌面端资源目录的 Windows 长路径 \\\\?\\C:\\…）。
    工具不支持 -Y verify 或无法运行时抛 VerifierUnavailable；签名、signer 或 namespace 不符时抛 ValueError。"""
    import tempfile
    with tempfile.TemporaryDirectory(prefix='agent-collab-verify-') as folder:
        signature_file, signers_file = Path(folder) / 'release.sig', Path(folder) / 'allowed_signers'
        signature_file.write_bytes(signature)
        signers_file.write_bytes(signers)
        try:
            result = subprocess.run([executable, '-Y', 'verify', '-f', str(signers_file), '-I', 'agent-collab-release',
                                     '-n', 'agent-collab-release', '-s', str(signature_file)],
                                    input=archive, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **NO_WINDOW)
        except OSError as exc:
            raise VerifierUnavailable('could not run %s: %s' % (executable, exc))
    if not result.returncode:
        return
    output = (result.stderr or result.stdout).decode('utf-8', 'replace').strip()
    if UNSUPPORTED.search(output):
        version = openssh_version(executable)
        raise VerifierUnavailable('%s does not support "ssh-keygen -Y verify"%s' % (executable, ' (%s)' % version if version else ''))
    if not output or result.returncode < 0:
        raise VerifierUnavailable('%s ended without a verification result (exit %s)' % (executable, result.returncode))
    raise ValueError('release signature verification failed: ' + output.splitlines()[-1][:200])


def verify_source(prefix, args, archive=None, version=None):
    """Verify exactly the bytes later unpacked/saved. Trust changes occur only after verification."""
    pinned = prefix / 'trust/allowed_signers'
    supplied = Path(args.allowed_signers).expanduser() if args.allowed_signers else None
    if pinned.is_symlink() or (prefix / 'trust').is_symlink():
        raise ValueError('trust paths must not be symlinks')
    trusted = pinned.read_bytes() if pinned.is_file() else None
    chosen = supplied.read_bytes() if supplied else trusted
    replacing = trusted is not None and chosen != trusted
    if replacing and not args.replace_trust:
        raise ValueError('allowed_signers differs from pinned trust; use --replace-trust')
    if args.replace_trust and supplied is None:
        raise ValueError('--replace-trust requires --allowed-signers')
    signature = Path(args.signature).expanduser() if args.signature else None
    if archive is None and version:
        saved = prefix / 'release-sources' / validate_version(version)
        if (saved / 'archive').is_file():
            archive = (saved / 'archive').read_bytes()
        if signature is None and (saved / 'archive.sig').is_file():
            signature = saved / 'archive.sig'
    signed = signature.read_bytes() if signature else None
    if signed is not None:
        if archive is None or chosen is None:
            raise ValueError('signature verification requires the release archive and allowed_signers')
        # 在改动安装、服务与信任之前核对；不能用的工具换下一个，签名不符立即拒绝（不再试别的工具）。
        candidates, unavailable = ssh_keygen_candidates(), []
        if not candidates:
            raise VerifierUnavailable('release signature verification unavailable: ssh-keygen was not found; ' + OPENSSH_GUIDANCE)
        for executable in candidates:
            try:
                verify_signature(executable, archive, signed, chosen)
                break
            except VerifierUnavailable as exc:
                unavailable.append(str(exc))
        else:
            raise VerifierUnavailable('release signature verification unavailable: ' + '; '.join(unavailable) + '. ' + OPENSSH_GUIDANCE)
        print('Release source verified: agent-collab-release (OpenSSH, %s).' % executable)
    elif chosen is not None and not args.allow_unsigned:
        raise ValueError('pinned/supplied trust requires --signature; use --allow-unsigned to explicitly override')
    else:
        print('Warning: 未验证来源 (unsigned release)' + ('; explicit --allow-unsigned override.' if chosen else '.'))
    if chosen is not None and trusted != chosen:
        print('Trust will be ' + ('replaced (--replace-trust).' if replacing else 'pinned on installation.'))
    return dict(archive=archive, signature=signed, trust=chosen, previous_trust=trusted,
                replace=replacing, verified=signed is not None)


def save_source(prefix, version, source):
    if source is None:
        return
    pinned = prefix / 'trust/allowed_signers'
    if (pinned.read_bytes() if pinned.is_file() else None) != source['previous_trust']:
        raise ValueError('pinned trust changed concurrently; rerun verification')
    if source['archive'] is not None:
        folder = prefix / 'release-sources' / validate_version(version)
        if folder.is_symlink() or folder.parent.is_symlink():
            raise ValueError('release source directory must not be a symlink')
        folder.mkdir(parents=True, exist_ok=True)
        atomic_write(folder / 'archive', source['archive'])
        if source['signature'] is not None:
            atomic_write(folder / 'archive.sig', source['signature'])
        else:
            (folder / 'archive.sig').unlink(missing_ok=True)
    if source['trust'] is not None:
        (prefix / 'trust').mkdir(exist_ok=True)
        atomic_write(prefix / 'trust/allowed_signers', source['trust'])


def remember_trust(journal, source):
    """信任变化要随更新一起提交：先在日志里记下原来的信任，失败回退时由 restore_trust 恢复。"""
    if source['trust'] is not None and source['trust'] != source['previous_trust']:
        previous = source['previous_trust']
        journal.value.update(trust_changed=True, trust_before=None if previous is None else previous.decode('latin-1'))
        journal.save()


def restore_trust(prefix, journal):
    if not journal.value.get('trust_changed'):
        return
    pinned, before = prefix / 'trust/allowed_signers', journal.value.get('trust_before')
    if before is None:
        pinned.unlink(missing_ok=True)
    else:
        atomic_write(pinned, before.encode('latin-1'))
    journal.value['trust_changed'] = False
    journal.save()


def release_compatibility(config, manifest):
    from .client import APIError, Client, ConnectionFailure
    from .release_tools import evaluate_compat
    state = Path(config['state_dir'])
    try:
        if 'controller' in components(config):
            address, ca, token = 'http://' + config['bind'], None, admin_token(config)
        elif (state / 'worker/agent.json').is_file():
            agent = read_json(state / 'worker/agent.json')
            address, ca = agent['controller'], agent.get('ca_file')
            # /client requires a client/admin credential; a node token is not a client token.
            token = admin_token(config)
        else:
            return dict(decision='degraded', unchecked=['未配对；未核对 controller 兼容性'])
        client = Client(address, token, ca, timeout=3)
        info = client.json('GET', '/client')
        nodes = None
        if 'controller' in components(config):
            try:
                nodes = client.json('GET', '/nodes')['nodes']
            except (APIError, ConnectionFailure):
                pass
    except (OSError, ConnectionFailure, APIError):
        return dict(decision='degraded', unchecked=['controller 不可达或缺少获授权的管理凭据；未核对在线兼容性'])
    results = [evaluate_compat(manifest, info, kind, nodes) for kind in components(config)]
    for result in results:
        if result['decision'] == 'reject':
            raise ValueError('release compatibility reject: ' + json.dumps(result, ensure_ascii=False))
    return dict(decision='degraded' if any(r['decision'] == 'degraded' for r in results) else 'accept', roles=results)


JOURNAL_STEPS = ('compat', 'verify', 'prepare', 'backup', 'switch', 'health')


def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def pid_alive(pid):
    if type(pid) is not int or pid <= 0:
        return None
    if os.name == 'nt':
        # os.kill(pid, 0) is not a liveness probe on Windows (it can terminate the process).
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False if ctypes.get_last_error() == 87 else None
        try:
            code = wintypes.DWORD()
            return code.value == 259 if kernel.GetExitCodeProcess(handle, ctypes.byref(code)) else None
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return None


def interrupted_update(config, old):
    prefix = Path(config['prefix'])
    path = prefix / 'update-journal.json'
    lock = prefix / '.install-lock'
    if not path.exists() and not lock.exists():
        return None
    try:
        journal = read_json(path) if path.exists() else None
        owner = read_json(lock / 'owner.json') if (lock / 'owner.json').is_file() else {}
        if journal and journal['status'] in ('done', 'failed') and not lock.exists():
            return None
        pid = owner.get('pid') or (journal or {}).get('pid')
        alive = pid_alive(pid)
        return dict(status='running' if alive else 'interrupted' if alive is False else 'pending',
                    pid=pid, pid_alive=alive, lock=lock.exists(),
                    journal={k: journal.get(k) for k in ('op_id', 'operation', 'from', 'to', 'status', 'steps', 'desired')}
                    if journal else None,
                    current=current_version(prefix, config['system']), recorded=(old or {}).get('version'))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return dict(status='pending', message='update journal, lock or pointer cannot be read; manual review required')


class UpdateJournal:
    def __init__(self, prefix, value):
        self.path, self.value = prefix / 'update-journal.json', value

    def save(self):
        self.value['updated_at'] = timestamp()
        write_json(self.path, self.value)

    @contextmanager
    def step(self, name):
        step = next(s for s in self.value['steps'] if s['name'] == name)
        step.update(status='running', started_at=timestamp(), finished_at=None, note='')
        self.save()
        try:
            yield step
        except Exception as exc:
            step.update(status='failed', finished_at=timestamp(), note=type(exc).__name__)
            self.save()
            raise
        else:
            step.update(status='done', finished_at=timestamp())
            self.save()

    def finish(self, status, note):
        self.value.update(status=status, note=note, finished_at=timestamp())
        self.save()


def start_desired(manager, config, desired):
    if all(desired.get(k) == 'stopped' for k in components(config)):
        for kind in components(config):
            print('kept stopped: ' + kind)
    else:
        manager.start()


def activate_and_check(config, directory, manifest, desired, manager, args):
    # all-in-one needs a listening controller only when pairing a running worker.
    code = args.pairing_code
    if config['role'] == 'all-in-one' and config['service'] and desired['controller'] == 'running':
        Services(dict(config, role='controller'), directory).start()
        if desired['worker'] == 'running' and not (Path(config['state_dir']) / 'worker/agent.json').exists():
            if not code:
                code = package_command(config, directory, 'controller', 'pairing-code', '--data-dir',
                    str(Path(config['state_dir']) / 'controller'), '--node-id', config['node_id'], controller_user=True).stdout.strip()
            wait_doctor(dict(config, role='controller'), args.health_timeout)
    if desired.get('worker') == 'running':
        pair_worker(config, directory, code)
    configure_worker_workspace(config)
    started_at = time.time()
    if config['role'] == 'all-in-one' and config['service']:
        worker_config = dict(config, role='worker')
        worker_manager = Services(worker_config, directory)
        worker_manager.desired = desired
        start_desired(worker_manager, worker_config, desired)
        if desired['controller'] == 'stopped':
            print('kept stopped: controller')
    else:
        start_desired(manager, config, desired)
    if config['service'] and desired.get('worker') == 'running':
        if (directory / 'agent_collab/release.py').is_file():
            wait_worker_status(config, directory, manifest, started_at, timeout=min(60, args.health_timeout))
        else:
            print('Note: target worker version predates status.json; health confirmed by service manager and doctor only.')
    health_options = {'desired': desired} if 'stopped' in desired.values() else {}
    return wait_doctor(config, args.health_timeout if config['service'] else 0, **health_options)


def installation_record(config, old, version, desired):
    previous = (old or {}).get('version')
    changed = previous and (previous != version or old['config']['python'] != config['python'])
    prior = old if changed else (old or {}).get('previous')
    if prior:
        prior = {k: v for k, v in prior.items() if k != 'previous'}
    return dict(version=version, config=config, desired=desired, previous=prior)


def restore_drain(journal, config, client=None):
    saved = journal.value.get('drain')
    if saved and saved['restore_required']:
        client = client or drain_client(config)
        if client[1] != saved['node_id']:
            raise ValueError('node identity changed; drain restoration requires review')
        set_drain(*client, saved['original'])
        saved['restore_required'] = False
        journal.save()


@contextmanager
def recovery_lock(prefix):
    """An OS-held recovery mutex is released even if the recovering process is killed."""
    path = prefix / '.recover-lock'
    if path.is_symlink():
        raise ValueError('recovery lock must not be a symlink')
    with path.open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if not stream.tell():
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError('another recovery owns prefix') from None
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError('another recovery owns prefix') from None
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def transaction(config, old, manifest, files, args):
    from .release import compare_versions, parse_version
    prefix = Path(config['prefix'])
    version, previous = manifest['version'], old.get('version') if old else None
    directory = prefix / 'versions' / version
    changing = previous and previous != version
    manager = Services(config, directory)
    old_config = old['config'] if previous else config
    old_directory = prefix / 'versions' / previous if previous else directory
    old_manager = Services(old_config, old_directory)
    desired, source = desired_state(old, config, old_manager.autostart()
                                    if previous and 'desired' not in old else None)
    manager.desired = old_manager.desired = desired
    if old:
        old = dict(old, desired=desired)
    journal = UpdateJournal(prefix, dict(op_id=str(uuid.uuid4()),
        operation='rollback' if args.rollback else 'upgrade' if previous else 'install',
        **{'from': previous, 'to': version}, pid=os.getpid(), started_at=timestamp(), status='running',
        desired=desired, old=old, config=config, drain=None,
        manifest_sha256=hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
        steps=[dict(name=name, status='pending', started_at=None, finished_at=None, note='') for name in JOURNAL_STEPS]))
    journal.save()
    drain, stopped, switched = None, False, False
    try:
        with journal.step('compat') as step:
            parse_version(version)
            if previous and not args.rollback and not args.allow_downgrade and compare_versions(version, previous) < 0:
                raise ValueError('downgrade refused; use --rollback or --allow-downgrade (schema checks still apply)')
            if previous:
                refuse_active_worker(config, args.allow_active)
                if 'controller' in components(config):
                    compatible_schema(old_config, manifest, old_directory)
            step['result'] = release_compatibility(config, manifest) if config['service'] else dict(
                decision='degraded', unchecked=['离线安装；未核对在线兼容性'])
            if step['result']['decision'] == 'degraded':
                print('Release compatibility: ' + json.dumps(step['result']))
        with journal.step('verify') as step:
            release_source = getattr(args, '_source', None)
            if release_source is None:
                release_source = verify_source(prefix, args, version=version)
            step['note'] = 'OpenSSH source verified' if release_source['verified'] else '未验证来源 (unsigned release)'
            if files is not None:
                install_version(prefix, manifest, files)
            elif read_installed(directory) != manifest:
                raise ValueError('installed release changed during verification')
            remember_trust(journal, release_source)
            save_source(prefix, version, release_source)
        with journal.step('prepare'):
            if getattr(args, '_runtime_payload', None):
                install_runtime(config, args._runtime_payload)
            if previous and (changing or config['service']) and desired.get('worker') == 'running' and (Path(config['state_dir']) / 'worker/agent.json').exists():
                drain = drain_client(config)
                def remember_drain(original):
                    journal.value['drain'] = dict(node_id=drain[1], original=original, restore_required=True)
                    journal.save()
                drained = drain_and_wait(*drain, timeout=args.drain_timeout, before_drain=remember_drain)
                if journal.value['drain'] is None:
                    remember_drain(drained)
            if previous and config['service']:
                stopped = True
                old_manager.stop()
            if not previous:
                ensure_state(config, directory, manifest)
        with stopped_locks(old_config, old_directory):
            with journal.step('backup') as step:
                if previous:
                    refuse_active_worker(config, args.allow_active)
                if changing:
                    compatible_schema(config, manifest, old_directory)
                    backup_controller(old_config, old_directory)
                else:
                    step['note'] = 'No version change; no controller backup needed'
            with journal.step('switch'):
                switch_current(prefix, version, config['system'])
                switched = True
                write_launchers(config)
                manager.register()
        with journal.step('health') as step:
            step['result'] = activate_and_check(config, directory, manifest, desired, manager, args)
            step['note'] = 'Stopped components checked offline; no service start or socket listener'
            restore_drain(journal, config, drain)
            write_json(prefix / 'installation.json', installation_record(config, old, version, desired))
        journal.finish('done', 'Update complete')
    except Exception:
        safe = True
        try:
            restore_trust(prefix, journal)
            switched = switched or current_version(prefix, config['system']) != previous
            if switched and config['service']:
                manager.stop()
            if previous and not switched and stopped:
                start_desired(old_manager, old_config, desired)
                print('Restarted previous services after interrupted stop.', file=sys.stderr)
            elif previous and switched:
                with stopped_locks(config):
                    target = read_installed(old_directory)
                    rollback_args = parser().parse_args(['--allow-unsigned'] if args.allow_unsigned else [])
                    rollback_source = verify_source(prefix, rollback_args, version=previous)
                    if rollback_source['archive'] is not None and read_release(rollback_source['archive'])[0] != target:
                        raise ValueError('previous release archive differs from installed release')
                    compatible_schema(old_config, target, directory)
                    switch_current(prefix, previous, config['system'])
                    write_launchers(old_config)
                    old_manager.register()
                start_desired(old_manager, old_config, desired)
                write_json(prefix / 'installation.json', old)
                print('Restored previous version: ' + previous, file=sys.stderr)
            elif switched:
                manager.remove()
                switch_current(prefix, None, config['system'])
            restore_drain(journal, old_config, drain)
        except Exception as exc:
            safe = False
            print('Automatic rollback refused/failed ({}); service state is unknown. '
                  'Use --discover and --recover after reviewing schema and service state.'.format(type(exc).__name__), file=sys.stderr)
        journal.finish('failed' if safe else 'pending', 'Previous version restored' if safe else 'Recovery required')
        raise


def recover_update(config, old, args):
    prefix = Path(config['prefix'])
    path, lock = prefix / 'update-journal.json', prefix / '.install-lock'
    if not path.is_file():
        raise ValueError('no update journal; review legacy lock/pointer manually before retrying')
    journal = UpdateJournal(prefix, read_json(path))
    value = journal.value
    if not isinstance(value, dict):
        raise ValueError('invalid update journal; manual review required')
    if value.get('status') in ('done', 'failed') and not lock.exists():
        print('No interrupted update to recover.')
        return
    owner = read_json(lock / 'owner.json') if (lock / 'owner.json').is_file() else {}
    if pid_alive(owner.get('pid', value.get('pid'))) is not False:
        raise ValueError('installer PID is live or cannot be checked; refusing recovery')
    guard = ExitStack()
    guard.enter_context(recovery_lock(prefix))
    try:
        if read_json(path) != value:
            raise ValueError('journal changed concurrently; rerun discovery')
        current_owner = read_json(lock / 'owner.json') if (lock / 'owner.json').is_file() else {}
        if current_owner != owner or pid_alive(current_owner.get('pid', value.get('pid'))) is not False:
            raise ValueError('install lock owner changed; rerun discovery')
        pointer = current_version(prefix, config['system'])
        before, target = value.get('from'), value.get('to')
        saved = value.get('old')
        target_config = value['config']
        desired, _ = desired_state({'desired': value['desired']}, target_config)
        expected = installation_record(target_config, saved, target, desired)
        if (value['operation'] not in ('install', 'upgrade', 'rollback') or
                value['status'] not in ('running', 'pending', 'done', 'failed') or
                [s['name'] for s in value['steps']] != list(JOURNAL_STEPS) or
                pointer not in (before, target) or (old or {}).get('version') not in (before, target) or
                (saved or {}).get('version') != before or
                target_config['prefix'] != str(prefix) or target_config['system'] != config['system'] or
                (old and old.get('version') == target and before != target and old != expected) or
                (old and old.get('version') == before and dict(old, desired=desired) != saved and
                 not (before is None and old.get('version') is None))):
            raise ValueError('journal, current pointer and installation record cannot be reconciled')
        validate_paths(target_config, saved)
        if saved:
            validate_paths(saved['config'], saved)
        if before == target:
            raise ValueError('same-version update cannot be located relative to switch; manual review required')
        if lock.exists():
            if set(p.name for p in lock.iterdir()) - {'owner.json'}:
                raise ValueError('unexpected install lock contents; manual review required')
            (lock / 'owner.json').unlink(missing_ok=True)
            lock.rmdir()
        lock.mkdir()
        write_json(lock / 'owner.json', dict(pid=os.getpid()))
        value.update(pid=os.getpid(), status='running', recovered_at=timestamp())
        journal.save()
        if pointer == target and old == expected and all(s['status'] == 'done' for s in value['steps']) and not args.rollback:
            journal.finish('done', 'Committed update reconciled; stale lock removed')
            return
        back = args.rollback or pointer == before
        if back and before is None:
            if pointer is not None:
                raise ValueError('first install has no previous release to roll back to')
            restore_drain(journal, target_config)
            journal.finish('failed', 'First install interrupted before switch; rerun installation')
            return
        version = before if back else target
        active_config = dict(saved['config'] if back else target_config)
        if args.admin_token_file:
            active_config['admin_token_file'] = config['admin_token_file']
        directory = prefix / 'versions' / validate_version(version)
        manifest = read_installed(directory)
        if manifest['version'] != version:
            raise ValueError('recovery directory and manifest versions differ')
        if not back and hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest() != value['manifest_sha256']:
            raise ValueError('recovery manifest differs from journal')
        if not back and any(s['status'] != 'done' for s in value['steps'][:4]):
            raise ValueError('target pointer has incomplete preparation evidence')
        if back:
            restore_trust(prefix, journal)
        source = verify_source(prefix, args, version=version)
        if source['archive'] is not None and read_release(source['archive'])[0] != manifest:
            raise ValueError('saved signed archive differs from installed release')
        compatible_schema(active_config, manifest, directory)
        refuse_active_worker(active_config, args.allow_active)
        save_source(prefix, version, source)
        manager = Services(active_config, directory)
        manager.desired = desired
        manager.check_ownership()
        if active_config['service']:
            Services(target_config, prefix / 'versions' / (pointer or target)).stop()
        with stopped_locks(active_config, directory):
            compatible_schema(active_config, manifest, directory)
            refuse_active_worker(active_config, args.allow_active)
            switch_current(prefix, version, active_config['system'])
            write_launchers(active_config)
            manager.register()
        if back:
            start_desired(manager, active_config, desired)
            wait_doctor(active_config, args.health_timeout if active_config['service'] else 0, desired=desired)
            restore_drain(journal, active_config)
            write_json(prefix / 'installation.json', dict(saved, desired=desired))
            journal.finish('failed', 'Interrupted update restored to previous version')
        else:
            switch = next(s for s in value['steps'] if s['name'] == 'switch')
            switch.update(status='done', finished_at=timestamp(), note='Reconciled target pointer')
            journal.save()
            with journal.step('health') as step:
                step['result'] = activate_and_check(active_config, directory, manifest, desired, manager, args)
                step['note'] = 'Recovery health; stopped components checked offline'
                restore_drain(journal, active_config)
                write_json(prefix / 'installation.json', expected)
            journal.finish('done', 'Interrupted update completed')
    except Exception as exc:
        journal.finish('pending', 'Recovery unresolved: ' + str(exc))
        raise
    finally:
        if value['status'] in ('done', 'failed') and lock.exists():
            (lock / 'owner.json').unlink(missing_ok=True)
            lock.rmdir()
        guard.close()


def remove_tree(path, attempts=40, delay=0.25):
    """删除目录；Windows 上被刚结束的进程占用（WinError 32/5）时稍等再试。

    计划任务的进程在 schtasks /End 之后还要一小会儿才真正退出，期间它的工作目录与已打开的文件仍被占用；
    状态锁先于这些句柄释放，所以拿到锁之后立即删除仍可能失败（卸载时实测）。
    """
    for attempt in range(attempts):
        try:
            shutil.rmtree(str(path))
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if os.name != 'nt' or attempt == attempts - 1:
                raise
            time.sleep(delay)


def runtime_in_use(runtime):
    """这个进程是不是正从 runtime 目录里运行。Windows 不能删除正在运行的解释器和它加载的 DLL（WinError 5）：
    经已安装的启动器卸载时，进程就是 runtime 里的 Python。"""
    try:
        return os.name == 'nt' and Path(runtime).resolve() in Path(sys.executable).resolve().parents
    except OSError:
        return False


def purge_runtime(prefix):
    runtime = prefix / 'runtime'
    if not runtime.exists():
        return
    if runtime_in_use(runtime):
        print('Runtime %s runs this uninstaller and was kept; run the uninstall with --purge-code again from the desktop '
              'app or another Python to remove it.' % runtime)
        return
    remove_tree(runtime)


def uninstall(config, old, purge_code):
    prefix = Path(config['prefix'])
    # 经安装后的启动器运行时，工作目录就是当前版本目录（launcher.py 会 chdir 进去）；Windows 不能删除进程自己的
    # 工作目录（WinError 32，重试也没用），先离开代码目录。
    try:
        here = Path.cwd().resolve()
        if here == prefix.resolve() / 'versions' or prefix.resolve() / 'versions' in here.parents:
            os.chdir(str(prefix))
    except OSError:
        os.chdir(str(prefix))
    # 桌面端卸载时保留后台留下的说明（Windows 安装器钩子写的）：这次卸载之后它不再成立。
    (prefix / 'desktop-uninstalled.txt').unlink(missing_ok=True)
    if not old or not old.get('version'):
        if old and purge_code and (prefix / 'versions').exists():
            remove_tree(prefix / 'versions')
        if old and purge_code:
            purge_runtime(prefix)
        print('No managed installation; state directory is retained.')
        return
    directory = prefix / 'versions' / old['version']
    manager = Services(config, directory)
    manager.remove()
    with stopped_locks(config):
        switch_current(prefix, None, config['system'])
        if purge_code:
            remove_tree(prefix / 'versions')
        elif directory.exists():
            remove_tree(directory)
        for name in ('bin', 'services'):
            if (prefix / name).exists():
                remove_tree(prefix / name)
        (prefix / 'launcher.py').unlink(missing_ok=True)
        write_json(prefix / 'installation.json', dict(version=None, previous=None, config=config))
        if purge_code:
            # 放在安装记录改写之后：运行时被保留时，再次卸载走“没有受管安装”的分支删掉它。
            purge_runtime(prefix)
    print('Uninstalled; all state, backups, workspaces and tool login configuration are retained.')


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--role', choices=ROLES)
    for name in ('release', 'version', 'prefix', 'state-dir', 'controller-url', 'pairing-code',
                 'node-id', 'python', 'runtime', 'admin-token-file', 'ca-file', 'bind', 'physical-host-id', 'failure-domain', 'workspace-root', 'instance',
                 'signature', 'allowed-signers'):
        result.add_argument('--' + name)
    for name in ('no-service', 'dry-run', 'uninstall', 'purge-code', 'rollback', 'drain', 'allow-active', 'discover',
                 'recover', 'allow-unsigned', 'replace-trust', 'allow-downgrade'):
        result.add_argument('--' + name, action='store_true')
    result.add_argument('--drain-timeout', type=float, default=300)
    result.add_argument('--health-timeout', type=float, default=60)
    return result


def configuration(args, old, system):
    if args.runtime and args.python:
        raise ValueError('--runtime and --python cannot be combined')
    saved = dict(old['config']) if old else {}
    if args.python:
        saved.pop('runtime', None)
    role = args.role or saved.get('role') or 'worker'
    managed_linux_controller = system == 'Linux' and role != 'worker' and not args.no_service
    defaults = dict(role=role, instance='', system=system, service=not args.no_service,
                    prefix=str(Path('/opt/agent-collab') if managed_linux_controller else Path.home() / '.local/share/agent-collab'),
                    state_dir=str(Path('/var/lib/agent-collab') if managed_linux_controller else Path.home() / '.local/state/agent-collab'),
                    python=sys.executable, controller_url='http://127.0.0.1:18669', bind='127.0.0.1:18669',
                    node_id='node-' + re.sub('[^a-z0-9-]', '-', platform.node().lower()).strip('-')[:48],
                    admin_token_file=None, ca_file=None, physical_host_id=None, failure_domain=None,
                    workspace_root=default_windows_workspace() if system == 'Windows' and role != 'controller' else None)
    defaults.update(saved)
    for key in defaults:
        if key == 'runtime':
            continue
        value = getattr(args, key, None)
        if value is not None:
            defaults[key] = value
    service = not args.no_service
    if old and getattr(args, 'uninstall', False):
        # 卸载沿用安装时的服务模式：用 --no-service 安装的前缀不必再带这个参数（桌面端卸载也不知道它）。
        service = service and saved.get('service', True)
    defaults.update(system=system, service=service)
    for key in ('prefix', 'state_dir', 'python', 'admin_token_file', 'ca_file', 'workspace_root'):
        if defaults.get(key):
            value = defaults[key]
            if key == 'python':
                value = shutil.which(value) or value
            path = Path(value).expanduser()
            # 令牌文件只取绝对路径、不跟随链接：/dev/stdin 经 /proc/self/fd/0 会解析成不存在的 pipe:[N]。
            defaults[key] = str(path.absolute() if key == 'admin_token_file' or
                                (key == 'python' and defaults.get('runtime', {}).get('managed')) else path.resolve())
    instance = defaults['instance']
    if instance and not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,30}', instance):
        raise ValueError('invalid instance: use 1-31 lowercase letters, digits or hyphens')
    if old and instance != saved.get('instance', ''):
        raise ValueError('instance cannot be changed on an existing installation')
    return defaults


def validate_paths(config, old):
    prefix, state = Path(config['prefix']), Path(config['state_dir'])
    if prefix == state or prefix in state.parents or state in prefix.parents:
        raise ValueError('prefix and state-dir must be disjoint; never put state under code')
    for value in config.values():
        if isinstance(value, str) and any(ord(c) < 32 for c in value):
            raise ValueError('control characters are not allowed in paths/options')
    for base, names in ((prefix, ('versions', 'runtime', 'bin', 'services', 'installation.json', 'launcher.py', 'current.txt',
                                  'update-journal.json', '.install-lock', '.recover-lock', 'trust', 'release-sources')),
                        (state, ('controller', 'worker', 'logs', 'backups'))):
        for name in names:
            if (base / name).is_symlink():
                raise ValueError('managed paths must not be symlinks: ' + str(base / name))
    if prefix in (Path(prefix.anchor), Path.home()) or state == Path(state.anchor):
        raise ValueError('use a dedicated prefix/state directory')
    if old and (old['config']['role'] != config['role'] or old['config']['state_dir'] != config['state_dir']):
        raise ValueError('role and state-dir cannot be changed on an existing installation')
    if old and old.get('version') and old['config']['service'] != config['service']:
        raise ValueError('service mode cannot be changed in place; keep --no-service consistent or use a separate reviewed prefix')


def preflight(config, args, old, operation):
    from .client import validate_url
    from .controller import parse_bind
    from .protocol import check_node_id
    validate_paths(config, old)
    validate_url(config['controller_url'])
    check_node_id(config['node_id'])
    parse_bind(config['bind'], None, None)  # controller remains loopback-only
    if not (0 <= args.drain_timeout <= 86400 and 0 <= args.health_timeout <= 600):
        raise ValueError('invalid drain/health timeout')
    if config['service'] and 'worker' in components(config) and os.name != 'nt' and os.getuid() == 0:
        raise ValueError('worker services must be installed by the logged-in non-root user')
    if config['service'] and config['system'] == 'Darwin' and os.getuid() == 0:
        raise ValueError('macOS LaunchAgents must be installed by the logged-in user')
    if config['service'] and config['system'] == 'Linux' and 'controller' in components(config):
        # ProtectHome=true deliberately hides home directories from the system controller.
        for path in (Path(config['prefix']), Path(config['state_dir']), Path(config['python'])):
            if str(path).startswith(('/home/', '/root/', '/run/user/')):
                raise ValueError('system controller needs code, state and Python outside home directories')
    if config['role'] == 'worker' and config['service'] and operation == 'install':
        if not (Path(config['state_dir']) / 'worker/agent.json').exists() and not args.pairing_code:
            raise ValueError('first worker service install requires --controller-url and --pairing-code')
    worker = Path(config['state_dir']) / 'worker'
    if old and old.get('version') and operation in ('install', 'rollback'):
        refuse_active_worker(config, args.allow_active)
    if (worker / 'agent.json').exists() != (worker / 'node.token').exists():
        raise ValueError('partial node identity exists; refusing to reset identity')
    if (worker / 'agent.json').exists():
        identity = read_json(worker / 'agent.json')
        if args.node_id and identity.get('node_id') != args.node_id:
            raise ValueError('--node-id differs from the existing identity')
        if args.controller_url and validate_url(identity['controller']) != validate_url(args.controller_url):
            raise ValueError('--controller-url differs from the existing identity')
    # 卸载不运行记录里的解释器：上一次 --purge-code 删到一半时它可能已经残缺，再次卸载正要接着删掉它。
    if not args.runtime and operation != 'uninstall':
        probe = probe_python(config['python'], managed=config.get('runtime', {}).get('managed', False))
        print('Environment: {} {} / Python {}'.format(config['system'], platform.machine(), probe.stdout.strip()))
    Services(config, Path(config['prefix']) / 'versions' / ((old or {}).get('version') or 'pending')).check_ownership()


def create_root(path, config):
    if path.exists():
        return
    try:
        path.mkdir(parents=True, mode=0o755)
        if os.name != 'nt':
            os.chmod(str(path), 0o755)
    except PermissionError:
        if not (config['system'] == 'Linux' and config['service'] and config['role'] != 'worker'):
            raise
        Services(config, path).privileged(['install', '-d', '-m', '755', '-o', str(os.getuid()),
                                          '-g', str(os.getgid()), str(path)])


def controller_listening(config):
    """Unauthenticated loopback probe: a service manager can report active before the socket answers."""
    from .client import APIError, Client, ConnectionFailure
    try:
        return Client('http://' + config['bind'], timeout=2).json('GET', '/healthz').get('ok') is True
    except (APIError, ConnectionFailure, OSError, ValueError, KeyError, AttributeError):
        return False


def service_command(argv):
    ap = argparse.ArgumentParser(prog='agent-collab service')
    ap.add_argument('action', choices=('start', 'stop', 'status'))
    ap.add_argument('--prefix', default=str(Path.home() / '.local/share/agent-collab'))
    ap.add_argument('--allow-active', action='store_true')
    ap.add_argument('--component', choices=('controller', 'worker'))
    ap.add_argument('--timeout', type=float, default=60)
    args = ap.parse_args(argv)
    report = dict(action=args.action)
    reason = 'service requires an existing managed installation with a current version'
    try:
        if not 0 <= args.timeout <= 600:
            reason = 'service timeout must be between 0 and 600 seconds'
            raise ValueError(reason)
        prefix = Path(args.prefix).expanduser().resolve()
        old = read_json(prefix / 'installation.json')
        config = old['config']
        full_config = config
        if args.component:
            if args.component not in components(config):
                reason = 'component is not part of this installation'
                raise ValueError(reason)
            config = dict(config, role=args.component)
        report['desired'] = dict(old.get('desired', {}))
        if not old.get('version') or config.get('service') is not True:
            raise ValueError(reason)
        if config['system'] != platform.system() or Path(config['prefix']).resolve() != prefix:
            reason = 'installation platform or prefix does not match this invocation'
            raise ValueError(reason)
        version = current_version(prefix, config['system'])
        if args.action != 'status' and interrupted_update(full_config, old):
            reason = 'unfinished update; use install --discover and --recover before service changes'
            raise ValueError(reason)
        if version != old['version']:
            reason = 'current pointer and installation record differ; inspect interrupted installation'
            raise ValueError(reason)
        directory = prefix / 'versions' / version
        manager = Services(config, directory)
        report['current'] = version
        # 服务名在整台机器上共用：定义属于另一个前缀的安装时，既不查询也不改动它。
        reason = 'service definition belongs to another installation or cannot be verified; use that installation\'s prefix'
        manager.check_ownership()
        if args.action == 'status':
            reason = 'could not read managed service state'
            report.update(services=manager.status(), autostart=manager.autostart(), worker=None)
            inferred = report['autostart'] if not args.component else Services(full_config, directory).autostart()
            desired, source = desired_state(old, full_config, inferred)
            report.update(desired=desired, desired_source=source, context=service_context(config))
            if 'worker' in components(config):
                try:
                    worker = read_json(Path(config['state_dir']) / 'worker/status.json')
                    worker['heartbeat_age_s'] = None
                    try:
                        stamp = datetime.datetime.fromisoformat(worker['last_heartbeat_ok_at'].replace('Z', '+00:00'))
                        worker['heartbeat_age_s'] = max(0, time.time() - stamp.timestamp())
                    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                        pass
                    report['worker'] = worker
                except (OSError, ValueError, TypeError):
                    pass
            success = all(value == 'active' for value in report['services'].values())
        else:
            reason = 'another installer or service command owns this prefix'
            lock = prefix / '.install-lock'
            lock.mkdir()
            try:
                write_json(lock / 'owner.json', dict(pid=os.getpid()))
                if read_json(prefix / 'installation.json') != old:
                    reason = 'installation changed concurrently; rerun service command'
                    raise ValueError(reason)
                if args.action == 'stop':
                    try:
                        refuse_active_worker(config, args.allow_active)
                    except ValueError as exc:
                        # 原因只含数量与状态（执行中的 attempt、待上报的写回），不含路径，原样给出。
                        reason = str(exc)
                        raise
                desired, source = desired_state(old, full_config, Services(full_config, directory).autostart()
                                                if args.component and 'desired' not in old else None)
                if args.action == 'stop':
                    reason = 'could not disable autostart and stop managed services; inspect local service logs'
                    manager.stop(disable_autostart=True)
                    reason = 'process state locks were not released before the stop timeout'
                    with stopped_locks(config, directory, timeout=args.timeout):
                        pass
                    report.update(stopped=True, autostart='disabled')
                else:
                    reason = 'installed release could not be verified'
                    manifest = read_installed(directory)
                    if manifest['version'] != version:
                        raise ValueError(reason)
                    started_at = time.time()
                    deadline = time.monotonic() + args.timeout
                    reason = 'could not enable and start managed services; inspect local service logs'
                    manager.start()
                    if 'worker' in components(config):
                        reason = 'worker has no fresh successful heartbeat matching the current directory, version, commit and pid'
                        report['worker'] = wait_worker_status(config, directory, manifest, started_at,
                                                             timeout=max(0, deadline - time.monotonic()))
                    reason = 'managed services or the local controller did not become ready before the start timeout'
                    while True:
                        states = manager.status()
                        # The service entry point needs no admin token: reuse doctor's local manifest/schema
                        # checks, then prove the controller answers on its loopback address.
                        healthy = ('controller' not in components(config) or
                                   (doctor(dict(config, role='controller'), offline=True)['ok'] and controller_listening(config)))
                        if healthy and all(value == 'active' for value in states.values()):
                            break
                        if time.monotonic() >= deadline:
                            raise RuntimeError(reason)
                        time.sleep(min(1, max(0, deadline - time.monotonic())))
                    report.update(ready=True, services=states)
                desired.update({kind: 'stopped' if args.action == 'stop' else 'running' for kind in components(config)})
                write_json(prefix / 'installation.json', dict(old, desired=desired))
                report['desired'] = desired
                success = True
            finally:
                (lock / 'owner.json').unlink(missing_ok=True)
                lock.rmdir()
        print(json.dumps(report, ensure_ascii=False))
        return 0 if success else 1
    except (ValueError, KeyError, TypeError, AttributeError, OSError, RuntimeError, sqlite3.Error,
            subprocess.SubprocessError, ET.ParseError) as exc:
        report.update(error=reason, error_type=type(exc).__name__)
        if args.action == 'start':
            report['ready'] = False
        elif args.action == 'stop':
            report['stopped'] = False
        print('Installer: ' + reason, file=sys.stderr)
        print(json.dumps(report, ensure_ascii=False))
        return 1


def main(argv=None):
    from .client import APIError, ConnectionFailure
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ['service']:
        return service_command(argv[1:])
    if argv[:1] == ['_schema']:
        internal = argparse.ArgumentParser()
        internal.add_argument('--state-dir', required=True)
        internal.add_argument('--manifest', required=True)
        args = internal.parse_args(argv[1:])
        print(json.dumps(check_schema(Path(args.state_dir), json.loads(args.manifest))))
        return 0
    checking = argv[:1] == ['doctor']
    args = parser().parse_args(argv[1:] if checking else argv)
    try:
        if sys.version_info < (3, 9):
            raise ValueError('Python >= 3.9 is required')
        system = platform.system()
        if system not in ('Linux', 'Darwin', 'Windows') or platform.machine().lower() not in (
                'x86_64', 'amd64', 'arm64', 'aarch64', 'x86', 'i386', 'i686', 'armv7l'):
            raise ValueError('unsupported OS/architecture')
        provisional = configuration(args, None, system)
        prefix = Path(provisional['prefix'])
        old = read_json(prefix / 'installation.json') if (prefix / 'installation.json').is_file() else None
        config = configuration(args, old, system)
        if args.discover:
            if old:
                config['service'] = old['config']['service']
            print(json.dumps(discover(config, old), ensure_ascii=False))
            return 0
        if checking:
            if old:
                config['service'] = old['config']['service']
            # 有意停用的组件不算不健康：与服务管理命令用同一份期望状态。
            report = doctor(config, offline=args.no_service, desired=old.get('desired') if old else None)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report['ok'] else 1
        if args.recover:
            if args.release or args.uninstall or args.drain or args.runtime:
                raise ValueError('--recover only combines with --rollback and recovery options')
            if old:
                config['service'] = old['config']['service']
            if not (0 <= args.drain_timeout <= 86400 and 0 <= args.health_timeout <= 600):
                raise ValueError('invalid drain/health timeout')
            validate_paths(config, old)
            if args.dry_run:
                print(json.dumps(interrupted_update(config, old), ensure_ascii=False))
                return 0
            recover_update(config, old, args)
            return 0
        interrupted = interrupted_update(config, old)
        if interrupted:
            raise ValueError('unfinished update or install lock; use --discover (read-only), then --recover '
                             'or --recover --rollback; do not replace current/installation.json manually')
        if sum(bool(v) for v in (args.rollback, args.uninstall, args.drain)) > 1:
            raise ValueError('choose only one of --rollback, --uninstall, --drain')
        if args.purge_code and not args.uninstall:
            raise ValueError('--purge-code requires --uninstall')
        operation = 'uninstall' if args.uninstall else 'rollback' if args.rollback else 'drain' if args.drain else 'install'
        if args.runtime and operation != 'install':
            raise ValueError('--runtime is only valid for installation; rollback uses the previous runtime')
        manifest, files = None, None
        if operation == 'install':
            if not args.release:
                raise ValueError('--release must name a local tar.gz or zip; no downloads are performed')
            # 压缩包不会比解包后的总量上限还大；先看文件大小，免得把超大的文件整个读进内存。
            if Path(args.release).stat().st_size > MAX_RELEASE_BYTES:
                raise ValueError('release exceeds size/file limit')
            archive = Path(args.release).read_bytes()
            args._source = verify_source(prefix, args, archive=archive)
            manifest, files = read_release(archive)
            from .release import compare_versions, parse_version
            parse_version(manifest['version'])
            if old and old.get('version') and not args.allow_downgrade and compare_versions(manifest['version'], old['version']) < 0:
                raise ValueError('downgrade refused; use --rollback or --allow-downgrade (schema checks still apply)')
            if args.version and args.version != manifest['version']:
                raise ValueError('--version does not match the release manifest')
            if not old and prefix.exists() and any(prefix.iterdir()):
                raise ValueError('prefix is not empty and is not a managed installation')
        elif args.release:
            raise ValueError('--release cannot be combined with this operation')
        if operation == 'rollback':
            if not old or not old.get('previous'):
                raise ValueError('no previous version is recorded')
            target = old['previous']['version']
            if args.version and args.version != target:
                raise ValueError('--rollback only targets the recorded previous version')
            manifest = read_installed(prefix / 'versions' / target)
            if manifest['version'] != target:
                raise ValueError('previous directory and manifest versions differ')
            args._source = verify_source(prefix, args, version=target)
            if args._source['archive'] is not None and read_release(args._source['archive'])[0] != manifest:
                raise ValueError('saved release archive differs from installed release')
            # Preserve the previous interpreter/configuration, plus this invocation's admin credential path.
            config = dict(old['previous']['config'], admin_token_file=config.get('admin_token_file'))
        pointer = current_version(prefix, system) if old else None
        # 卸载先删当前指针再删代码：指针已不在、记录仍有版本，是一次被中断的卸载，可以由再次卸载接着完成。
        interrupted_uninstall = operation == 'uninstall' and pointer is None
        if old and pointer != old['version'] and not interrupted_uninstall:
            raise ValueError('current pointer and installation record differ; inspect interrupted installation')
        runtime_payload = read_runtime(Path(args.runtime).expanduser()) if args.runtime else None
        args._runtime_payload = runtime_payload
        if runtime_payload:
            runtime_id, entries, python = runtime_payload
            config['python'] = str(prefix / 'runtime' / runtime_id / python)
            config['runtime'] = dict(id=runtime_id, python=config['python'], managed=True)
        preflight(config, args, old, operation)
        steps = ['Validate platform, Python, archive paths and every manifest hash (read-only)',
                 'Acquire prefix install lock; keep code and state separate']
        if operation in ('install', 'rollback'):
            steps += ['Stage immutable version ' + manifest['version'],
                      'If changing a paired worker: API drain and wait for zero attempts and pending writebacks when available',
                      'Stop managed services (or require manual processes stopped); check state locks',
                      'Check schema compatibility; back up existing controller before switching',
                      'Keep existing identity/data; initialize only missing state and pair only a new worker',
                      'Atomically switch current; render/register services and launchers',
                      'Start, run doctor, restore prior drain; on failure check schema and restore previous code']
        elif operation == 'uninstall':
            steps += ['Stop/remove owned services; verify processes stopped',
                      'Remove current pointer, launchers and active code' + (' plus all archived versions' if args.purge_code else ''),
                      'Retain ALL state, backups, business workspaces and official tool login configuration']
        else:
            steps += ['API drain this node; wait for zero attempts and available pending_writebacks; leave it drained']
        for index, step in enumerate(steps, 1):
            print('{}. {}'.format(index, step))
        print('Service management: ' + ('enabled' if config['service'] else 'disabled; runtime checks are unknown'))
        print('Reverse proxy review only:\nhttps://<DOMAIN>:<HTTPS_PORT> {\n    reverse_proxy 127.0.0.1:<LOCAL_PORT>\n}')
        print('Review placeholders and validate the complete proxy configuration before an operator reload. '
              'Provider login must be completed locally by the user.')
        if args.dry_run:
            if manifest and config['service']:
                print('Release compatibility: ' + json.dumps(release_compatibility(config, manifest)))
            if runtime_payload:
                print('Runtime structure verified; Python version/import probe runs after staging on installation.')
            print('Dry run complete: no filesystem writes or API mutations were performed.')
            return 0
        if operation == 'drain':
            client, node_id = drain_client(config)
            drain_and_wait(client, node_id, args.drain_timeout)
            print('Node drained; explicitly clear drain through the controller API when maintenance is complete.')
            return 0
        if operation == 'uninstall' and not old:
            uninstall(config, old, args.purge_code)
            return 0
        if config['system'] == 'Linux' and config['service'] and config['role'] != 'worker' and os.getuid() != 0:
            # Let sudo display its one-time password prompt in the caller's terminal.
            if subprocess.call(['sudo', '-v']) != 0:
                raise ValueError('administrator authorization is needed for the system controller service')
        if manifest and 'controller' in components(config) and old and old.get('version'):
            compatible_schema(old['config'] if runtime_payload else config, manifest, prefix / 'versions' / old['version'])
        create_root(prefix, config)
        lock = prefix / '.install-lock'
        try:
            lock.mkdir()
        except FileExistsError:
            raise ValueError('another install owns prefix; after a crash use --discover and --recover') from None
        completed = False
        try:
            write_json(lock / 'owner.json', dict(pid=os.getpid()))
            # Guard a second installer finishing between our initial read and lock acquisition.
            latest = read_json(prefix / 'installation.json') if (prefix / 'installation.json').is_file() else None
            if latest != old:
                raise ValueError('installation changed concurrently; rerun')
            if operation == 'uninstall':
                uninstall(config, old, args.purge_code)
            elif (old and not config['service'] and 'desired' in old
                  and manifest['version'] == old['version'] and config == old['config']
                  and not args.runtime and not args.allowed_signers and not args.signature
                  and (not args.pairing_code or 'worker' not in components(config)
                       or (Path(config['state_dir']) / 'worker/agent.json').is_file())):
                if read_installed(prefix / 'versions' / old['version']) != manifest:
                    raise ValueError('version already exists with different contents')
                health_options = {'desired': old['desired']} if 'stopped' in old['desired'].values() else {}
                wait_doctor(config, args.health_timeout if config['service'] else 0, **health_options)
                for kind, state in old['desired'].items():
                    if state == 'stopped':
                        print('kept stopped: ' + kind)
                print('Already installed; identity and data were preserved.')
            else:
                create_root(Path(config['state_dir']), config)
                if old is None:
                    write_json(prefix / 'installation.json', dict(version=None, previous=None, config=config))
                transaction(config, old, manifest, files, args)
            if operation != 'uninstall':
                cleanup_runtimes(prefix, read_json(prefix / 'installation.json'))
            completed = True
        except Exception:
            journal_path = prefix / 'update-journal.json'
            completed = not journal_path.exists() or read_json(journal_path).get('status') in ('done', 'failed')
            raise
        finally:
            # BaseException models process termination: preserve lock and running journal for recovery.
            if completed and lock.exists():
                (lock / 'owner.json').unlink(missing_ok=True)
                lock.rmdir()
        return 0
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, sqlite3.Error, tarfile.TarError,
            zipfile.BadZipFile, subprocess.SubprocessError, APIError, ConnectionFailure) as exc:
        print('Installer: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
