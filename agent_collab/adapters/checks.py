"""Sequential contract checks polled by the supervisor, never blocking heartbeats."""
import math
import re
import sys

from .codex import OwnedProcess, tail


class ContractChecks:
    def __init__(self, context, process_factory=OwnedProcess):
        self.context, self.process_factory = context, process_factory
        self.checks = context.contract.get('checks', [])
        names = set()
        if not isinstance(self.checks, list) or len(self.checks) > 16:
            raise ValueError('invalid contract checks')
        for check in self.checks:
            if not isinstance(check, dict):
                raise ValueError('invalid contract check')
            name, argv, timeout = check.get('name'), check.get('run'), check.get('timeout_s', 600)
            if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', name) or name in names
                    or not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv)
                    or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 3600):
                raise ValueError('invalid contract check')
            names.add(name)
        self.results = []
        self.process = None
        self.started = None
        self.done = False

    def tick(self):
        if not self.context.can_execute():
            if self.process and not self.process.pause():
                self.context.quarantine('check lease expired; process stopped')
            return
        if len(self.results) == len(self.checks):
            self.done = True
            self.context.finish('succeeded')
            return
        check = self.checks[len(self.results)]
        argv = [sys.executable if x == '{python}' else x for x in check['run']]
        if self.process is None:
            self.started = self.context.clock()
            try:
                # 输出目录用检查序号而不是契约里的名字：合法名字 ".." 会让目录落到 attempt 根并覆盖 harness 输出。
                self.process = self.context.start_process(self.process_factory, argv, self.context.work,
                    self.context.directory / 'checks' / str(len(self.results)), b'', self.context.agent.environment)
            except OSError:
                # start_process 可能已启动并登记了子进程，随后才在落盘时失败：先停掉它，确认不了就隔离。
                if self.process is not None:
                    stopped = self.process.stop()
                    self.process = None
                    if not stopped:
                        self.context.process_stopped(False)
                        self.context.quarantine('check process termination could not be verified')
                        return
                self.context.process_stopped(True)
                self.results.append(dict(name=check['name'], argv=argv, exit_code=None, timed_out=False,
                    duration_s=max(0, self.context.clock() - self.started), stdout_tail='', stderr_tail='Check could not start'))
                return
        self.process.resume()
        code = self.process.poll()
        timed_out = code is None and self.context.clock() - self.started >= check.get('timeout_s', 600)
        if code is None and not timed_out:
            return
        stopped = self.process.stop()
        self.context.process_stopped(stopped)
        if not stopped:
            self.context.quarantine('check process termination could not be verified')
            return
        self.results.append(dict(name=check['name'], argv=argv, exit_code=None if timed_out else code,
            timed_out=timed_out, duration_s=max(0, self.context.clock() - self.started),
            stdout_tail=tail(self.process.stdout_path, 4096), stderr_tail=tail(self.process.stderr_path, 4096)))
        self.process = None

    def stop(self):
        stopped = self.process.stop() if self.process else True
        self.context.process_stopped(stopped)
        return stopped
