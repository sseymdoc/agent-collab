"""Deterministic steps; zero-based completed indexes are checkpointed at boundaries."""
import os
import sys

from .. import protocol as p
from ..artifacts import atomic_write, safe_path
from .codex import OwnedProcess, tail


class StubAdapter:
    def __init__(self, context, process_factory=OwnedProcess):
        self.context = context
        self.steps = context.contract.get('harness_params', {}).get('steps', [])
        self.completed = set(context.summary.get('completed', []))
        if any(type(i) is not int or i < 0 or i >= len(self.steps) for i in self.completed):
            raise ValueError('invalid completed step indexes')
        self.process_factory = process_factory
        self.process = None
        self.sleep_until = None
        self.done = False
        self.stopped = False
        self.results = {i: dict(index=i, op=next(iter(self.steps[i]), None),
                                status='skipped_from_checkpoint', exit_code=None, stdout_tail='', stderr_tail='')
                        for i in self.completed}
        self.index = None
        self.truncated = False

    def report(self):
        if self.process and self.index in self.results:
            self.capture(self.results[self.index], self.process.poll())
        report = {'harness': 'stub', 'steps': [self.results[i] for i in sorted(self.results)]}
        if self.truncated:
            report['truncated'] = True
        return report

    def capture(self, result, code):
        result.update(exit_code=code, stdout_tail=tail(self.process.stdout_path, 4097),
                      stderr_tail=tail(self.process.stderr_path, 4097))
        self.truncated = self.truncated or any(path.exists() and path.stat().st_size > 4096
                                               for path in (self.process.stdout_path, self.process.stderr_path))

    def tick(self):
        if self.done:
            return
        if not self.context.can_execute():
            if self.process and not self.process.pause():
                self.context.finish('failed', 'infra_retryable', 'lease expired; process tree stopped')
                self.done = True
            return
        if self.process:
            self.process.resume()
        index = next((i for i in range(len(self.steps)) if i not in self.completed), None)
        if index is None:
            self.done = True
            self.context.finish('succeeded')
            return
        step = self.steps[index]
        self.index = index
        result = self.results.setdefault(index, dict(index=index, op=None, status='failed',
                                                     exit_code=None, stdout_tail='', stderr_tail=''))
        if not isinstance(step, dict) or len(step) != 1:
            raise ValueError('stub step must contain exactly one operation')
        op, value = next(iter(step.items()))
        result['op'] = op
        if op == 'write':
            path = safe_path(self.context.work, value['path'])
            atomic_write(path, value['content'].encode('utf-8'), 0o755 if value.get('executable') else 0o644)
        elif op == 'delete':
            path = safe_path(self.context.work, value['path'])
            if path.exists():
                path.unlink()
        elif op == 'sleep':
            if not isinstance(value, (float, int)) or value < 0:
                raise ValueError('sleep must be nonnegative')
            if self.sleep_until is None:
                self.sleep_until = self.context.clock() + value
            if self.context.clock() < self.sleep_until:
                return
            self.sleep_until = None
        elif op == 'checkpoint':
            completed = sorted(self.completed | {index})
            summary = dict(self.context.summary, completed=completed,
                           pending=[i for i in range(len(self.steps)) if i not in completed])
            # The executed sequence is authoritative; caller-supplied completed
            # cannot cause an unexecuted step to be skipped after recovery.
            self.context.checkpoint(value.get('kind', 'boundary'), summary)
        elif op in ('progress', 'side_effect'):
            self.context.event('attempt.' + op, value)
            if op == 'side_effect':
                effects = {item['op_id']: item for item in self.context.summary.get('side_effects', [])}
                effects[value['op_id']] = value
                self.context.summary['side_effects'] = list(effects.values())
        elif op == 'fail':
            if value not in p.FAILURE_CLASSES:
                raise ValueError('invalid failure_class')
            self.done = True
            self.context.finish('failed', value, 'stub fail step')
            return
        elif op == 'run':
            if not isinstance(value, list) or not value or not all(isinstance(x, str) for x in value):
                raise ValueError('run must be an argv list')
            if not self.process:
                # 协议 §9：argv 中的 "{python}" 替换为 agent 自身解释器，使同一契约可在 Windows/macOS/Linux 执行。
                value = [sys.executable if x == '{python}' else x for x in value]
                self.process = self.context.start_process(self.process_factory, value, self.context.work,
                                                          self.context.directory / ('step-' + str(index)))
            code = self.process.poll()
            if code is None:
                return
            stopped = self.process.stop()
            self.context.process_stopped(stopped)
            self.capture(result, code)
            if not stopped:
                # 进程树未确认清空：隔离（与 codex/claude/契约检查一致），不让控制器另起替代执行。
                self.done = True
                self.context.quarantine('stub process termination could not be verified')
                return
            self.process = None
            if code != 0:
                self.done = True
                self.context.finish('failed', 'code_failure', 'stub command failed')
                return
        else:
            raise ValueError('unknown stub step: ' + str(op))
        self.completed.add(index)
        result['status'] = 'done'
        self.context.summary['completed'] = sorted(self.completed)

    def stop(self):
        self.stopped = True
        self.done = True
        stopped = self.process.stop() if self.process else True
        self.context.process_stopped(stopped)
        return stopped
