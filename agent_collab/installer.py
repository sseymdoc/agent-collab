"""Offline releases and conservative install/upgrade transactions (Python 3.9+)."""
import argparse
from contextlib import ExitStack, closing, contextmanager
import hashlib
import datetime
import getpass
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

    if zipfile.is_zipfile(str(path)):
        with zipfile.ZipFile(str(path)) as archive:
            for info in archive.infolist():
                mode = info.external_attr >> 16
                if info.is_dir() or stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    raise ValueError('only regular archive files are supported')
                add(info.filename, info.file_size, lambda i=info: archive.read(i))
    else:
        with tarfile.open(str(path), 'r:gz') as archive:
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
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


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


def drain_and_wait(client, node_id, timeout, clock=time.monotonic, sleep=time.sleep):
    original = get_node(client, node_id)['drain']
    deadline = clock() + timeout
    try:
        set_drain(client, node_id, True)
        while True:
            node = get_node(client, node_id)
            if node.get('drain') is not True:
                raise ValueError('node drain changed concurrently; abort upgrade')
            if type(node.get('active_attempts')) is int and node['active_attempts'] == 0:
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


def default_windows_workspace(env=None, username=None):
    env = os.environ if env is None else env
    return ntpath.join(env.get('SystemDrive', 'C:') + '\\', 'agent-collab-work', username or getpass.getuser())


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
                      'CONTROLLER_STATE': systemd_quote(state / 'controller')}
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
            ET.SubElement(action, 'Command').text = worker_python(config['python']) if kind == 'worker' else config['python']
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
                        owners[kind] = 'none'
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
                response = run(['loginctl', 'show-user', getpass.getuser(), '--property=Linger', '--value'], check=False)
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
                context=service_context(probe_config))


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
                report['components'][kind] = 'unknown'
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
        inventory = [[name, len(content), hashlib.sha256(content).hexdigest()]
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


def transaction(config, old, manifest, files, args):
    prefix = Path(config['prefix'])
    version, previous = manifest['version'], old.get('version') if old else None
    directory = prefix / 'versions' / version
    changing = previous and previous != version
    if previous:
        refuse_active_worker(config, args.allow_active)
    if files is not None:
        directory = install_version(prefix, manifest, files)
    manager = Services(config, directory)
    old_config = old['config'] if previous else config
    old_directory = prefix / 'versions' / previous if previous else directory
    old_manager = Services(old_config, old_directory)
    desired, source = desired_state(old, config, old_manager.autostart()
                                    if previous and 'desired' not in old else None)
    manager.desired = old_manager.desired = desired
    if old:
        old = dict(old, desired=desired)
    drain, drained, started, switched, stopped = None, None, False, False, False
    try:
        if previous and (changing or config['service']) and desired.get('worker') == 'running' and (Path(config['state_dir']) / 'worker/agent.json').exists():
            drain = drain_client(config)
            drained = drain_and_wait(*drain, timeout=args.drain_timeout)
        if previous and config['service']:
            stopped = True
            old_manager.stop()
        if not previous:
            ensure_state(config, directory, manifest)
        with stopped_locks(old_config, old_directory):
            if previous:
                refuse_active_worker(config, args.allow_active)
            if changing:
                # Repeat compatibility after stopping to close the migration/check race.
                compatible_schema(config, manifest, old_directory)
                backup_controller(old_config, old_directory)
            switch_current(prefix, version, config['system'])
            switched = True
            write_launchers(config)
            manager.register()
        # all-in-one needs a listening controller before the node can pair.
        code = args.pairing_code
        if config['role'] == 'all-in-one' and config['service'] and desired['controller'] == 'running':
            Services(dict(config, role='controller'), directory).start()
            started = True
            if desired['worker'] == 'running' and not (Path(config['state_dir']) / 'worker/agent.json').exists():
                if not code:
                    code = package_command(config, directory, 'controller', 'pairing-code', '--data-dir',
                        str(Path(config['state_dir']) / 'controller'), '--node-id', config['node_id'], controller_user=True).stdout.strip()
                controller_only = dict(config, role='controller')
                wait_doctor(controller_only, args.health_timeout)
        if desired.get('worker') == 'running':
            pair_worker(config, directory, code)
        configure_worker_workspace(config)
        started_at = time.time()
        if config['role'] == 'all-in-one' and config['service']:
            worker_manager = Services(dict(config, role='worker'), directory)
            worker_manager.desired = desired
            worker_manager.start()
            if desired['controller'] == 'stopped':
                print('kept stopped: controller')
        else:
            manager.start()
        started = config['service']
        if config['service'] and desired.get('worker') == 'running':
            if (Path(directory) / 'agent_collab/release.py').is_file():
                wait_worker_status(config, directory, manifest, started_at, timeout=min(60, args.health_timeout))
            else:
                # rc4 及更早的 worker 不写 status.json：回退到这些版本时只能用服务管理器与 doctor 确认，
                # 不能证明实际进程来自该版本目录（主控验收时另行核对进程）。
                print('Note: target worker version predates status.json; health confirmed by service manager and doctor only.')
        health_options = {'desired': desired} if 'stopped' in desired.values() else {}
        wait_doctor(config, args.health_timeout if config['service'] else 0, **health_options)
        if drain is not None:
            set_drain(*drain, drained)
            drain = None
        interpreter_changed = previous and old_config['python'] != config['python']
        record = dict(version=version, config=config, desired=desired,
                      previous=old if changing or interpreter_changed else (old or {}).get('previous'))
        # Retain one previous record, not an ever-growing nested history.
        if record['previous']:
            record['previous'] = {k: v for k, v in record['previous'].items() if k != 'previous'}
        write_json(prefix / 'installation.json', record)
    except Exception:
        if switched or stopped or started:
            try:
                if switched and config['service']:
                    manager.stop()
                if previous and not switched:
                    # A stop can fail after stopping only one component. No new code has run.
                    old_manager.start()
                    print('Restarted previous services after interrupted stop.', file=sys.stderr)
                elif previous:
                    with stopped_locks(config):
                        target = read_installed(old_directory)
                        compatible_schema(old_config, target, directory)
                        switch_current(prefix, previous, config['system'])
                        write_launchers(old_config)
                        old_manager.register()
                    old_manager.start()
                    print('Restored previous version: ' + previous, file=sys.stderr)
                else:
                    manager.remove()
                    switch_current(prefix, None, config['system'])
            except Exception as exc:
                drain = None  # do not resume claims after an unsafe/failed rollback
                print('Automatic rollback refused/failed ({}); service state is unknown. '
                      'Check service status/schema and restore backup before restarting.'.format(type(exc).__name__), file=sys.stderr)
        raise
    finally:
        if drain is not None and drained is not None:
            try:
                set_drain(*drain, drained)
            except Exception:
                print('Node may remain drained; restore its prior drain setting after review.', file=sys.stderr)


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
    if not old or not old.get('version'):
        if old and purge_code and (prefix / 'versions').exists():
            remove_tree(prefix / 'versions')
        if old and purge_code and (prefix / 'runtime').exists():
            remove_tree(prefix / 'runtime')
        print('No managed installation; state directory is retained.')
        return
    directory = prefix / 'versions' / old['version']
    manager = Services(config, directory)
    manager.remove()
    with stopped_locks(config):
        switch_current(prefix, None, config['system'])
        if purge_code:
            remove_tree(prefix / 'versions')
            if (prefix / 'runtime').exists():
                remove_tree(prefix / 'runtime')
        elif directory.exists():
            remove_tree(directory)
        for name in ('bin', 'services'):
            if (prefix / name).exists():
                remove_tree(prefix / name)
        (prefix / 'launcher.py').unlink(missing_ok=True)
        write_json(prefix / 'installation.json', dict(version=None, previous=None, config=config))
    print('Uninstalled; all state, backups, workspaces and tool login configuration are retained.')


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--role', choices=ROLES)
    for name in ('release', 'version', 'prefix', 'state-dir', 'controller-url', 'pairing-code',
                 'node-id', 'python', 'runtime', 'admin-token-file', 'ca-file', 'bind', 'physical-host-id', 'failure-domain', 'workspace-root', 'instance'):
        result.add_argument('--' + name)
    for name in ('no-service', 'dry-run', 'uninstall', 'purge-code', 'rollback', 'drain', 'allow-active', 'discover'):
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
    defaults.update(system=system, service=not args.no_service)
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
    for base, names in ((prefix, ('versions', 'runtime', 'bin', 'services', 'installation.json', 'launcher.py', 'current.txt')),
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
    if not args.runtime:
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
                if read_json(prefix / 'installation.json') != old:
                    reason = 'installation changed concurrently; rerun service command'
                    raise ValueError(reason)
                if args.action == 'stop':
                    reason = 'worker has preparing/running attempts; drain first or pass --allow-active'
                    refuse_active_worker(config, args.allow_active)
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
            report = doctor(config, offline=args.no_service)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report['ok'] else 1
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
            manifest, files = read_release(Path(args.release))
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
            # Preserve the previous interpreter/configuration, plus this invocation's admin credential path.
            config = dict(old['previous']['config'], admin_token_file=config.get('admin_token_file'))
        pointer = current_version(prefix, system) if old else None
        # 卸载先删当前指针再删代码：指针已不在、记录仍有版本，是一次被中断的卸载，可以由再次卸载接着完成。
        interrupted_uninstall = operation == 'uninstall' and pointer is None
        if old and pointer != old['version'] and not interrupted_uninstall:
            raise ValueError('current pointer and installation record differ; inspect interrupted installation')
        runtime_payload = read_runtime(Path(args.runtime).expanduser()) if args.runtime else None
        if runtime_payload:
            runtime_id, entries, python = runtime_payload
            config['python'] = str(prefix / 'runtime' / runtime_id / python)
            config['runtime'] = dict(id=runtime_id, python=config['python'], managed=True)
        preflight(config, args, old, operation)
        steps = ['Validate platform, Python, archive paths and every manifest hash (read-only)',
                 'Acquire prefix install lock; keep code and state separate']
        if operation in ('install', 'rollback'):
            steps += ['Stage immutable version ' + manifest['version'],
                      'If changing a paired worker: API drain and wait for zero active attempts',
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
            steps += ['API drain this node and wait for zero active attempts; leave it drained']
        for index, step in enumerate(steps, 1):
            print('{}. {}'.format(index, step))
        print('Service management: ' + ('enabled' if config['service'] else 'disabled; runtime checks are unknown'))
        print('Reverse proxy review only:\nhttps://<DOMAIN>:<HTTPS_PORT> {\n    reverse_proxy 127.0.0.1:<LOCAL_PORT>\n}')
        print('Review placeholders and validate the complete proxy configuration before an operator reload. '
              'Provider login must be completed locally by the user.')
        if args.dry_run:
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
            raise ValueError('another install owns prefix; after a crash, review state before removing .install-lock') from None
        try:
            # Guard a second installer finishing between our initial read and lock acquisition.
            latest = read_json(prefix / 'installation.json') if (prefix / 'installation.json').is_file() else None
            if latest != old:
                raise ValueError('installation changed concurrently; rerun')
            if runtime_payload:
                install_runtime(config, runtime_payload)
            if operation == 'uninstall':
                uninstall(config, old, args.purge_code)
            elif (old and not config['service'] and 'desired' in old
                  and manifest['version'] == old['version'] and config == old['config']
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
        finally:
            lock.rmdir()
        return 0
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, sqlite3.Error, tarfile.TarError,
            zipfile.BadZipFile, subprocess.SubprocessError, APIError, ConnectionFailure) as exc:
        print('Installer: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
