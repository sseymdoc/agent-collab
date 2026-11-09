"""Codex argv, event parsing and owned process groups. No model invoked at import."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

from .. import protocol as p

MODEL = 'gpt-6-astra'
EFFORTS = ('high', 'xhigh', 'max')
CREATE_SUSPENDED = 0x00000004  # Windows CreateProcess 标志


def build_argv(command, cwd, output_dir, params=None, session_id=None, structured_report=False,
               system=None, windows_sandbox='elevated'):
    # Ported argv()/validate(): launcher.py SHA256 7862865fadc8c2a1.
    params = params or {}
    if params.get('model', MODEL) != MODEL:
        raise ValueError('model must be ' + MODEL)
    effort = params.get('effort', 'xhigh')
    if effort not in EFFORTS:
        raise ValueError('effort must be high, xhigh or max')
    sandbox = params.get('sandbox', 'read-only')
    if sandbox not in ('read-only', 'workspace-write'):
        raise ValueError('invalid sandbox')
    command = [command] if isinstance(command, str) else list(command)
    argv = command + ['exec', '--ignore-user-config', '-C', str(cwd), '--sandbox', sandbox,
        '-c', 'approval_policy="never"', '-c', 'model_reasoning_effort="' + effort + '"',
        '-c', 'sandbox_workspace_write.writable_roots=[]',
        '-c', 'sandbox_workspace_write.network_access=false',
        '-c', 'sandbox_workspace_write.exclude_tmpdir_env_var=true',
        '-c', 'sandbox_workspace_write.exclude_slash_tmp=true',
        '--model', MODEL, '--skip-git-repo-check', '--json', '-o', str(Path(output_dir) / 'result.txt')]
    windows = system == 'Windows' if system is not None else os.name == 'nt'
    if windows:
        argv += ['-c', 'windows.sandbox=' + json.dumps(windows_sandbox)]
    if structured_report:
        argv += ['--output-schema', str(Path(output_dir) / 'report-schema.json')]
    if session_id is not None:
        argv += ['resume', p.check_uuid(session_id), '-']
    else:
        argv += ['-']
    return argv


def classify_failure(text, returncode=None, abnormal=False):
    text = text.lower()
    # 状态码只按独立数字匹配：UUID、路径或行号中的“401/429”子串不能被当作认证或限额失败。
    # 关键词同样按词边界匹配："assign int"、普通测试输出里的 "authentication" 不是认证失效。
    if (re.search(r'\b(unauthorized|authentication (?:failed|expired|required)|auth expired|'
                  r'token expired|login required|sign in)\b', text)
            or re.search(r'(?<![\w-])401(?![\w-])', text)):
        return 'auth_expired'
    if any(word in text for word in ('usage limit', 'usage_limit', 'rate limit', 'rate_limit',
                                     'rate-limit', 'too many requests', 'quota')) or re.search(r'(?<![\w-])429(?![\w-])', text):
        return 'quota'
    if abnormal or (returncode is not None and (returncode < 0 or returncode >= 0x80000000)):
        return 'infra_retryable'
    return 'code_failure'


def parse_events(text, state=None):
    # Ported run_info(): launcher.py SHA256 7862865fadc8c2a1.
    state = dict(state or {})
    for line in text.splitlines():
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                continue
            kind = event.get('type')
            if kind == 'thread.started':
                state['session_id'] = p.check_uuid(event['thread_id'])
            elif kind in ('turn.failed', 'error'):
                state['cli_reported_error'] = event
            elif kind == 'turn.completed':
                state.update(turn_completed=True, usage=event.get('usage'))
        except (ValueError, KeyError):
            continue
    return state


def read_observed(codex_home, session_id):
    if not session_id or not codex_home:
        return None
    p.check_uuid(session_id)
    paths = list((Path(codex_home) / 'sessions').rglob('*' + session_id + '.jsonl'))
    observed = None
    if len(paths) == 1:
        for line in tail(paths[0], 4 * 1024 * 1024).splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get('type') == 'turn_context':
                payload = event.get('payload', {})
                observed = {key: payload.get(key) for key in (
                    'model', 'effort', 'reasoning_effort', 'cwd', 'sandbox_policy', 'approval_policy')}
                observed['source'] = str(paths[0])
    return observed


def tail(path, limit=65536):
    if not Path(path).exists():
        return ''
    with Path(path).open('rb') as stream:
        stream.seek(max(0, Path(path).stat().st_size - limit))
        return stream.read(limit).decode('utf-8', 'replace')


def process_stamp(pid):
    """Creation identity, None for confirmed exit, OSError for unknown status.

    Ported from launcher.py process_stamp, SHA256 7862865fadc8c2a1;
    unlike the reference, access errors must not be mistaken for process exit.
    """
    if type(pid) is not int or pid <= 0:
        raise ValueError('invalid child pid')
    if os.name == 'nt':
        import ctypes as ct
        from ctypes import wintypes as w
        kernel = ct.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        kernel.OpenProcess.restype = w.HANDLE
        kernel.GetProcessTimes.argtypes = [w.HANDLE] + [ct.POINTER(w.FILETIME)] * 4
        kernel.GetExitCodeProcess.argtypes = [w.HANDLE, ct.POINTER(w.DWORD)]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            error = ct.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: this PID no longer exists.
                return None
            raise OSError(error, 'cannot inspect child process')
        try:
            times = [w.FILETIME() for _ in range(4)]
            code = w.DWORD()
            if not kernel.GetExitCodeProcess(handle, ct.byref(code)):
                raise OSError(ct.get_last_error(), 'cannot inspect child exit status')
            if code.value != 259:
                return None
            if not kernel.GetProcessTimes(handle, *[ct.byref(item) for item in times]):
                raise OSError(ct.get_last_error(), 'cannot inspect child creation time')
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart=', '-o', 'stat='],
                            capture_output=True, text=True, timeout=5,
                            env=dict(os.environ, LC_ALL='C'))
    fields = result.stdout.strip().split()
    if result.returncode == 0 and len(fields) >= 2:
        return None if fields[-1].startswith('Z') else ' '.join(fields[:-1])
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    raise OSError('child identity could not be read')


def process_status(record):
    """Never signal a saved PID: it may now belong to a different process."""
    if record is None or record.get('state') == 'stopped':
        return 'gone'
    pid = record.get('pid')
    if type(pid) is not int or pid <= 0:
        return 'unknown'  # Includes a crash between spawn intent and PID recording.
    try:
        current = process_stamp(pid)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 'unknown'
    if current is None:
        # A POSIX leader can exit while its descendants still own the workspace.
        return 'gone' if os.name == 'nt' or group_empty(pid) else 'unknown'
    expected = record.get('stamp')
    if expected is None:
        return 'unknown'
    return 'alive' if current == expected else 'gone'

# Ported from ~/.claude/skills/codex-collaborate/scripts/launcher.py
# SHA256 7862865fadc8c2a1: WindowsJob, group_empty, stop_child.
class WindowsJob:
    """只容纳本次 Codex 子进程；关闭句柄即清理其后代。"""
    def __init__(self, child):
        import ctypes as ct
        from ctypes import wintypes as w
        self.ct = ct
        self.k = ct.WinDLL('kernel32', use_last_error=True)
        class Basic(ct.Structure):
            _fields_ = [('a', ct.c_longlong), ('b', ct.c_longlong), ('flags', w.DWORD),
                        ('min', ct.c_size_t), ('max', ct.c_size_t), ('count', w.DWORD),
                        ('affinity', ct.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class Io(ct.Structure):
            _fields_ = [('x' + str(i), ct.c_ulonglong) for i in range(6)]
        class Extended(ct.Structure):
            _fields_ = [('basic', Basic), ('io', Io), ('p', ct.c_size_t), ('j', ct.c_size_t),
                        ('peakp', ct.c_size_t), ('peakj', ct.c_size_t)]
        self.k.CreateJobObjectW.argtypes = [ct.c_void_p, w.LPCWSTR]
        self.k.CreateJobObjectW.restype = w.HANDLE
        self.k.SetInformationJobObject.argtypes = [w.HANDLE, ct.c_int, ct.c_void_p, w.DWORD]
        self.k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.k.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.k.QueryInformationJobObject.argtypes = [w.HANDLE, ct.c_int, ct.c_void_p, w.DWORD, ct.c_void_p]
        self.k.CloseHandle.argtypes = [w.HANDLE]
        self.h = self.k.CreateJobObjectW(None, None)
        limits = Extended()
        limits.basic.flags = 0x2000
        if not self.h or not self.k.SetInformationJobObject(self.h, 9, ct.byref(limits), ct.sizeof(limits)):
            raise OSError(ct.get_last_error(), '创建任务进程组失败')
        if not self.k.AssignProcessToJobObject(self.h, int(child._handle)):
            self.k.CloseHandle(self.h)
            raise OSError(ct.get_last_error(), '无法绑定本次 Codex 进程组')

    def resume(self, child):
        """恢复以 CREATE_SUSPENDED 创建的子进程（此前已绑定到本 Job）。"""
        ntdll = self.ct.WinDLL('ntdll')
        ntdll.NtResumeProcess.argtypes = [self.ct.c_void_p]
        status = ntdll.NtResumeProcess(int(child._handle))
        if status != 0:
            raise OSError(status, '无法恢复已绑定进程组的子进程')

    def stop(self):
        if not self.k.TerminateJobObject(self.h, 130):
            raise OSError(self.ct.get_last_error(), '取消本次进程组失败')

    def empty(self):
        class Accounting(self.ct.Structure):
            _fields_ = [('t' + str(i), self.ct.c_longlong) for i in range(4)] + [
                ('faults', self.ct.c_ulong), ('total', self.ct.c_ulong),
                ('active', self.ct.c_ulong), ('terminated', self.ct.c_ulong)]
        data = Accounting()
        if not self.k.QueryInformationJobObject(self.h, 1, self.ct.byref(data), self.ct.sizeof(data), None):
            return False
        return data.active == 0

    def close(self):
        self.k.CloseHandle(self.h)


def group_empty(pid):
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        pass  # macOS 在组内只剩僵尸进程时也返回 EPERM；交给下面按状态判断（忽略僵尸）
    if Path('/proc/self/stat').exists():
        # Linux 直接读 /proc：精简容器常没有 ps，且 PID 1 可能不回收孤儿，组内会长期留着僵尸。
        target = str(pid).encode()
        for stat in Path('/proc').glob('[0-9]*/stat'):
            try:
                data = stat.read_bytes()  # 按字节解析：comm 可以不是 UTF-8
            except (FileNotFoundError, ProcessLookupError):
                continue  # 进程刚退出
            except OSError:
                return False  # 读不到（如 hidepid、权限）不等于已退出：按未清空处理
            fields = data.rsplit(b')', 1)[-1].split()  # comm 可含空格/括号，取最后一个 ')' 之后
            if len(fields) > 2 and fields[2] == target and fields[0] not in (b'Z', b'X'):
                return False
        return True
    try:
        result = subprocess.run(['/bin/ps', '-eo', 'pgid=', '-o', 'stat='], capture_output=True, text=True)
    except OSError:
        return False
    if result.returncode:
        return False
    return not any(len(v) >= 2 and v[0] == str(pid) and not v[1].startswith('Z')
                   for v in (line.split() for line in result.stdout.splitlines()))


def stop_child(child, job):
    if job:
        job.stop()
    else:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass  # EPERM：macOS 组内只剩僵尸；是否清空由 group_empty 判定
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        if not group_empty(child.pid):
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return False
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if job.empty() if job else group_empty(child.pid):
            return True
        time.sleep(.1)
    return False


class OwnedProcess:
    """A child session/job, never an inherited shell or caller process group."""
    def __init__(self, argv, cwd, directory, stdin=b'', env=None):
        self.child = None
        self.job = None
        self.paused = False
        self.closed = False
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.stdout_path, self.stderr_path = directory / 'stdout.jsonl', directory / 'stderr.log'
        # Windows：挂起创建，绑定 Job 后再恢复，子进程来不及在绑定前派生出 Job 之外的后代。
        options = ({'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_SUSPENDED}
                   if os.name == 'nt' else {'start_new_session': True})
        # File stdin avoids blocking the agent's heartbeat on a full pipe.
        input_path = directory / 'stdin.txt'
        from ..artifacts import atomic_write
        atomic_write(input_path, stdin)
        try:
            with input_path.open('rb') as source, self.stdout_path.open('wb') as out, self.stderr_path.open('wb') as err:
                self.child = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=source,
                                              stdout=out, stderr=err, close_fds=True, **options)
                if os.name == 'nt':
                    self.job = WindowsJob(self.child)
                    self.job.resume(self.child)
        except BaseException:
            if self.child:
                self.child.kill()
                self.child.wait()
            raise

    def poll(self):
        return self.child.poll()

    def pause(self):
        # 不以组长是否在世为准：组长退出后，后代仍可能占着进程组/Job 并继续写工作区。
        if not self.paused:
            if os.name == 'nt':
                # No supported tree-wide suspend primitive in the stable launcher.
                # Fail closed; the agent requires reconciliation before reporting.
                self.stop()
                return False
            try:
                os.killpg(self.child.pid, signal.SIGSTOP)
            except ProcessLookupError:
                pass  # 进程组已空
            except PermissionError:
                self.stop()  # 无法保证挂起：失败即关闭
                return False
            self.paused = True
        return True

    def resume(self):
        if self.paused:
            try:
                os.killpg(self.child.pid, signal.SIGCONT)
            except (ProcessLookupError, PermissionError):
                pass  # macOS：对已停止进程投递 SIGTERM 会立即终止它，组内只剩僵尸时返回 EPERM
            self.paused = False

    def stop(self):
        if self.closed:
            return True
        try:
            if self.paused and os.name != 'nt':
                # 先投递 SIGTERM 再 SIGCONT：恢复后内核先处理挂起的终止信号，被挂起的后代不会先跑一段用户代码。
                try:
                    os.killpg(self.child.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            self.resume()
            stopped = stop_child(self.child, self.job)
        except OSError:
            return False
        if stopped:
            if self.job:
                self.job.close()
            self.closed = True
        return stopped


class CodexAdapter:
    def __init__(self, context, command='codex', codex_home=None, process_factory=OwnedProcess,
                 env=None, system=None, windows_sandbox='elevated'):
        self.context = context
        self.command = command
        self.env = dict(os.environ if env is None else env)
        for key in ('CODEX_THREAD_ID', 'CODEX_INTERNAL_ORIGINATOR_OVERRIDE'):
            self.env.pop(key, None)
        self.system, self.windows_sandbox = system, windows_sandbox
        self.codex_home = codex_home or self.env.get('CODEX_HOME', str(Path.home() / '.codex'))
        if codex_home:
            self.env['CODEX_HOME'] = codex_home
        self.process_factory = process_factory
        self.process = None
        self.state = {}
        self.done = False
        self.stopped = False

    def report(self):
        path = self.context.directory / 'result.txt'
        report = {'harness': 'codex', 'final_message': tail(path, 16385),
                'session_id': self.state.get('session_id'), 'usage': self.state.get('usage'),
                'cli_error': self.state.get('cli_reported_error')}
        if path.exists() and path.stat().st_size > 16384:
            report['truncated'] = True
        return report

    def tick(self):
        if self.done:
            return
        if not self.context.can_execute():
            if self.process and not self.process.pause():
                self.context.finish('failed', 'infra_retryable', 'lease expired; process tree stopped')
                self.done = True
            return
        if not self.process:
            params = self.context.contract.get('harness_params', {})
            # Native session ownership is not conveyed by v1 assignments. Start a
            # new thread from the materialized checkpoint; never blindly resume.
            argv = build_argv(self.command, self.context.work, self.context.directory, params,
                              system=self.system, windows_sandbox=self.windows_sandbox)
            self.process = self.context.start_process(self.process_factory, argv, self.context.work,
                self.context.directory, self.context.contract.get('prompt', '').encode('utf-8'), self.env)
        self.process.resume()
        self.state = parse_events(tail(self.process.stdout_path, 4 * 1024 * 1024), self.state)
        self.context.native_session_ref = self.state.get('session_id')
        code = self.process.poll()
        if code is None:
            return
        stopped = self.process.stop()
        self.context.process_stopped(stopped)
        if not stopped:
            # 进程树未确认清空：隔离而不是上报可重试失败，避免控制器在旧进程仍可写工作区时安排替代执行。
            self.context.quarantine('harness process termination could not be verified')
            self.done = True
            return
        self.state = parse_events(tail(self.process.stdout_path, 4 * 1024 * 1024), self.state)
        self.context.native_session_ref = self.state.get('session_id')
        self.context.observed = read_observed(self.codex_home, self.state.get('session_id'))
        text = tail(self.process.stderr_path) + json.dumps(self.state.get('cli_reported_error', {}))
        if code == 0 and stopped and self.state.get('turn_completed') and not self.state.get('cli_reported_error'):
            self.context.finish('succeeded')
        else:
            self.context.finish('failed', classify_failure(text, code, not stopped), 'harness did not complete successfully')
        self.done = True

    def stop(self):
        self.stopped = True
        self.done = True
        stopped = self.process.stop() if self.process else True
        self.context.process_stopped(stopped)
        return stopped


