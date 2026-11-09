"""Outbound-only node supervisor with durable outboxes and isolated attempts."""
import argparse
import datetime
import getpass
import hmac
import json
import logging
import os
from pathlib import Path
import platform
import random
import shutil
import signal
import subprocess
import sys
import time
import threading
import uuid

from . import protocol as p
from .artifacts import DEFAULT_EXCLUDES, EMPTY, atomic_write, checked_manifest, integrate, materialize, snapshot
from .client import APIError, Client, ConnectionFailure, validate_url
from .adapters.codex import CodexAdapter, process_stamp, process_status
from .adapters.claude import ClaudeAdapter
from .adapters.stub import StubAdapter
from .adapters.checks import ContractChecks
from .adapters.commands import resolve_commands, probe_auth
from .release import VERSION, release_info, version_directory

LOG = logging.getLogger(__name__)


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, value):
    atomic_write(path, p.canonical_json(value))


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z')


def probe(argv, env=None, runner=None):
    try:
        result = (runner or subprocess.run)(argv, env=env, stdin=subprocess.DEVNULL,
                                            capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return (result.stdout or result.stderr).strip() or None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def detect(config=None, commands=None, runner=None):
    if isinstance(config, str):  # Compatibility for the old Python helper.
        config = {'claude_command': config}
    config = config or {}
    commands = commands if commands is not None else resolve_commands(config)
    system = {'Darwin': 'macos', 'Windows': 'windows', 'Linux': 'linux'}.get(platform.system())
    arch = {'amd64': 'x86_64', 'x86_64': 'x86_64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine().lower())
    # 协议 §3：只列出找到的工具；版本读不到时为 "unknown"，不存在的工具为 null。
    tools = {}
    for name in ('python3', 'git', 'node'):
        path = shutil.which(name)
        version = probe([path, '--version'], runner=runner) if path else None
        # Windows 的 WindowsApps 应用执行别名（如 python3 跳转商店）不是可用工具。
        alias = bool(path) and 'windowsapps' in path.lower() and not version
        tools[name] = None if not path or alias else (version or 'unknown')
    gpu = probe(['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'], runner=runner) if shutil.which('nvidia-smi') else None
    for name, command in commands.items():
        tools[name] = probe(command['argv'] + ['--version'], env=command['env'], runner=runner) if command['argv'] else None
    identity = release_info()
    return dict(os=system, arch=arch, tools=tools,
                gpu=[{'name': n.strip(), 'cuda': True} for n in gpu.splitlines() if n.strip()] if gpu else [],
                harness={'stub': identity['version'], 'codex': tools['codex'], 'claude': tools['claude']},
                agent=identity,
                harness_paths={name: command['argv'][0] if command['argv'] else None for name, command in commands.items()},
                harness_disabled=dict(config.get('disabled_harnesses', {})),
                labels=['wsl'] if system == 'linux' and 'microsoft' in platform.release().lower() else [])


def redact(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, '[REDACTED]')
        return value
    if isinstance(value, dict):
        return {redact(key, secrets): redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    return value


def bounded_report(report):
    """Bound UTF-8 text and the canonical wire object after credential redaction."""
    def clip(record, field, limit):
        value = record.get(field)
        if isinstance(value, str) and len(value.encode('utf-8')) > limit:
            record[field] = value.encode('utf-8')[-limit:].decode('utf-8', 'ignore')
            report['truncated'] = True

    for step in report.get('steps', []):
        for field in ('stdout_tail', 'stderr_tail'):
            clip(step, field, 4096)
    for check in (report.get('checks') or {}).get('results', []):
        for field in ('stdout_tail', 'stderr_tail'):
            clip(check, field, 4096)
    clip(report, 'final_message', 16384)
    while len(p.canonical_json(report)) > 65536:
        report['truncated'] = True
        # Keep evidence identity/results whenever possible; trim check output first.
        results = (report.get('checks') or {}).get('results', [])
        tails = [(c, k) for c in results for k in ('stdout_tail', 'stderr_tail') if c.get(k)]
        if tails:
            record, key = max(tails, key=lambda item: len(p.canonical_json(item[0][item[1]])))
            record[key] = record[key][len(record[key]) // 2 + 1:]
            continue
        key = max((k for k in report if k not in ('harness', 'truncated')),
                  key=lambda k: len(p.canonical_json(report[k])))
        value = report[key]
        if isinstance(value, str):
            report[key] = value[len(value) // 2:]
        elif isinstance(value, list):
            report[key] = value[:len(value) // 2]
        else:
            report[key] = None
    return report


def saved_process(meta):
    if 'process' in meta:
        return meta['process']
    # Older metadata had no process tracking. Only a stub with no run steps is
    # evidence that no external child could have been launched.
    contract = meta['assignment']['contract']
    if contract.get('harness', 'stub') == 'stub' and not any(
            'run' in step for step in contract.get('harness_params', {}).get('steps', [])):
        return None
    return {'state': 'unknown', 'pid': None, 'stamp': None}


def restart_notice(directory, meta, status):
    record = saved_process(meta) or {}
    pid = record.get('pid')
    if type(pid) is int and pid > 0:
        command = ('Get-Process -Id %d | Select-Object Id,StartTime,Path' % pid if os.name == 'nt'
                   else 'ps -p %d -o pid=,lstart=,stat=,command=' % pid)
    else:
        command = ('Get-CimInstance Win32_Process | Select-Object ProcessId,CreationDate,CommandLine'
                   if os.name == 'nt' else 'ps -eo pid,ppid,pgid,lstart,command')
    return dict(attempt_id=meta['assignment']['attempt_id'], status=status, pid=pid,
        expected_stamp=record.get('stamp'), metadata=str(Path(directory) / 'meta.json'),
        inspect_command=command,
        action='核对创建时间和工作目录，勿仅按 PID 终止进程；确认原进程及后代退出后 agent 自动恢复领取。'
               '若进程身份不可读，请修复服务账户的查询权限；若无 PID 记录，保留隔离并人工核对，勿直接清除标记。')


class Backoff:
    def __init__(self, rng=None):
        self.rng = rng or random.Random()
        self.failures = 0

    def next(self):
        ceiling = min(60, 2 ** min(self.failures, 6))
        self.failures += 1
        return self.rng.uniform(0, ceiling)

    def reset(self):
        self.failures = 0


class Attempt:
    def __init__(self, agent, assignment, restored=None):
        self.agent = agent
        self.assignment = dict(assignment)
        self.id = p.check_uuid(assignment['attempt_id'])
        p.check_uuid(assignment['job_id'])
        self.lease_token = self.assignment.pop('lease_token', None)
        self.directory = agent.directory / 'attempts' / self.id
        configured_work = (Path(agent.config['workspace_root']).expanduser().absolute() / self.id
                           if agent.config.get('workspace_root') else self.directory / 'work')
        # Existing attempts retain their original directory across configuration changes.
        self.work = Path(restored['work']) if restored and restored.get('work') else (
            self.directory / 'work' if restored and (self.directory / 'work').exists() else configured_work)
        self.directory.mkdir(parents=True, exist_ok=True)
        lease_path = self.directory / 'lease.token'
        if restored:
            self.lease_token = None
            try:
                candidate = lease_path.read_text(encoding='utf-8')
                agent.secrets.add(candidate)
                if candidate and hmac.compare_digest(p.sha256_bytes(candidate.encode()), restored['lease_token_sha256']):
                    self.lease_token = candidate
                    if os.name != 'nt':
                        lease_path.chmod(0o600)
            except (OSError, KeyError):
                pass
        elif self.lease_token:
            agent.secrets.add(self.lease_token)
            atomic_write(lease_path, self.lease_token.encode(), 0o600)
        self.contract = self.assignment['contract']
        self.clock = agent.clock
        self.last_renewed = self.clock() if self.lease_token and not restored else None
        self.last_reconciled = None
        self.summary = dict(assignment.get('start', {}).get('summary') or {})
        self.summary.setdefault('completed', [])
        self.summary.setdefault('side_effects', [])
        self.base = None
        self.adapter = None
        self.check_runner = None
        self.native_session_ref = restored.get('native_session_ref') if restored else None
        self.observed = restored.get('observed') if restored else None
        self.meta = restored or dict(assignment=self.assignment, state='preparing', checkpoints=[],
                                    completion=None, publication=None, integration=None,
                                    process=None,
                                    lease_token_sha256=p.sha256_bytes((self.lease_token or '').encode()))
        self.events_path = self.directory / 'events.jsonl'
        self.events = []
        if self.events_path.exists():
            # Each append is fsynced. Only an incomplete last line can be torn.
            lines = self.events_path.read_bytes().splitlines(keepends=True)
            for index, line in enumerate(lines):
                try:
                    self.events.append(json.loads(line))
                except ValueError:
                    if index != len(lines) - 1 or line.endswith(b'\n'):
                        raise
                    atomic_write(self.events_path, b''.join(p.canonical_json(e) + b'\n' for e in self.events))
        if restored:
            self.base = self.meta.get('base')
            self.summary = self.meta.get('summary', self.summary)
            if self.meta['state'] in ('preparing', 'running') or self.meta.get('restart_unknown'):
                self.meta['restart_unknown'] = True
                self.refresh_restart()
            elif self.meta['state'] == 'finished_unreported' and not self.lease_token:
                self.quarantine('missing or invalid persisted lease credential')
        self.persist()

    def refresh_restart(self):
        if not self.meta.get('restart_unknown'):
            return
        status = process_status(saved_process(self.meta))
        previous = self.meta.get('restart_process_status')
        if status == 'gone':
            self.meta.update(state='abandoned', restart_unknown=False, stop_verified=True,
                             restart_process_status=status)
            self.agent.reconciled = False
            self.agent.next_heartbeat = 0
            LOG.warning('Attempt %s abandoned: prior process absent; workspace retained', self.id)
        else:
            self.meta.update(state='quarantined', restart_unknown=True, stop_verified=False,
                             restart_process_status=status, quarantine_reason='restart process ' + status)
            save(self.directory / 'quarantined.json', dict(reason=self.meta['quarantine_reason'],
                 at=now(), stop_verified=False))
            if previous != status:
                LOG.warning('Restart blocks claims: %s', json.dumps(self.agent.clean(
                    restart_notice(self.directory, self.meta, status)), ensure_ascii=False))
        if previous != status:
            self.persist()

    def start_process(self, factory, *args):
        # Persist spawn intent first: a crash before PID recording is unknown,
        # never evidence that no process exists.
        self.meta['process'] = dict(state='starting', pid=None, stamp=None)
        self.persist()
        process = factory(*args)
        # Keep the handle owned even if recording its identity fails on disk.
        owner = self.check_runner if self.check_runner is not None else self.adapter
        owner.process = process
        pid = process.child.pid
        try:
            stamp = process_stamp(pid)
        except (OSError, subprocess.TimeoutExpired):
            stamp = None
        self.meta['process'] = dict(state='running', pid=pid, stamp=stamp)
        self.persist()
        return process

    def process_stopped(self, verified):
        self.meta['stop_verified'] = verified
        if verified and self.meta.get('process') is not None:
            self.meta['process']['state'] = 'stopped'
        self.persist()

    def persist(self):
        self.meta.update(base=self.base, summary=self.summary, native_session_ref=self.native_session_ref,
                         observed=self.observed, work=str(self.work))
        save(self.directory / 'meta.json', self.agent.clean(self.meta))

    def event(self, kind, payload):
        if kind not in p.NODE_EVENT_TYPES:
            raise ValueError('unsupported node event')
        event = dict(event_id=str(uuid.uuid4()), type=kind, at=now(), payload=self.agent.clean(payload))
        fd = os.open(str(self.events_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'ab') as stream:
            stream.write(p.canonical_json(event) + b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        self.events.append(event)
        return event['event_id']

    def can_execute(self):
        if self.meta['state'] != 'running':
            return False
        if self.contract.get('recovery', {}).get('side_effects', 'manual') == 'none':
            return True
        return self.last_renewed is not None and self.clock() - self.last_renewed < self.agent.lease_ttl_s

    def fenced_body(self, **fields):
        # §4 lists epoch/token, but its fencing rule also requires generation.
        return dict(epoch=self.assignment['epoch'], lease_token=self.lease_token or '',
                    authority_generation=self.assignment['authority_generation'], **fields)

    def prepare(self):
        if self.meta['state'] != 'preparing':
            return
        start = self.assignment.get('start', {})
        sha = start.get('manifest_sha256')
        self.base = checked_manifest(json.loads(self.agent.fetch_blob(sha))) if sha else dict(EMPTY)
        if sha and not hmac.compare_digest(p.manifest_sha256(self.base), sha):
            raise ValueError('start manifest is not canonical')
        materialize(self.work, self.base, self.agent.fetch_blob)
        harness = self.contract.get('harness', 'stub')
        if harness in self.agent.commands and not self.agent.commands[harness]['argv']:
            self.finish('failed', 'capability_missing', 'harness missing or disabled')
            return
        if harness == 'stub':
            self.adapter = StubAdapter(self)
        elif harness == 'codex':
            command = self.agent.commands['codex']
            self.adapter = CodexAdapter(self, command['argv'], self.agent.config.get('codex_home'),
                env=command['env'], system=self.agent.system,
                windows_sandbox=self.agent.config.get('codex_windows_sandbox', 'elevated'))
        elif harness == 'claude':
            command = self.agent.commands['claude']
            self.adapter = ClaudeAdapter(self, command['argv'], env=command['env'])
        else:
            self.finish('failed', 'capability_missing', 'unsupported harness')
            return
        self.meta['state'] = 'running'
        self.event('attempt.started', {'workspace': str(self.work), 'runtime': self.runtime()})
        self.persist()

    def runtime(self):
        params = self.contract.get('harness_params', {})
        harness = self.contract.get('harness', 'stub')
        return dict(agent_version=self.agent.release['version'], agent_commit=self.agent.release['git_commit'], harness=harness,
                    harness_version=self.agent.capabilities.get('harness', {}).get(harness),
                    model=params.get('model', 'gpt-6-astra') if harness == 'codex' else params.get('model') if harness == 'claude' else None,
                    effort=params.get('effort', 'xhigh') if harness == 'codex' else params.get('effort') if harness == 'claude' else None,
                    observed=self.observed)

    def checkpoint(self, kind, summary=None):
        if self.meta['state'] in ('quarantined', 'abandoned'):
            raise ValueError('quarantined attempt cannot checkpoint')
        if kind not in ('baseline', 'boundary', 'final'):
            raise ValueError('invalid checkpoint kind')
        # Empty starts have no controller-owned manifest to reference as base.
        base = self.base if self.assignment.get('start', {}).get('manifest_sha256') else None
        manifest, blobs = snapshot(self.work, base, exclude=self.contract.get('workspace', {}).get('exclude'))
        for sha, data in blobs.items():
            self.agent.cache_blob(sha, data)
        checkpoint = dict(checkpoint_id=str(uuid.uuid4()), kind=kind, manifest=manifest,
                          summary=json.loads(p.canonical_json(summary or self.summary)),
                          native_session_ref=self.native_session_ref, durable=False)
        self.meta['checkpoints'].append(checkpoint)
        self.persist()
        return checkpoint['checkpoint_id']

    def finish(self, outcome, failure_class=None, failure_detail=None):
        if self.meta['state'] in ('quarantined', 'abandoned') or self.meta.get('completion'):
            return
        checkpoint_id = None
        if outcome == 'succeeded':
            if self.contract.get('checks') and self.check_runner is None:
                self.check_runner = ContractChecks(self)
                return
            if self.check_runner is not None and not self.check_runner.done:
                return
            checkpoint_id = self.checkpoint('final')
        harness = self.contract.get('harness', 'stub')
        report = self.adapter.report() if self.adapter else {
            'stub': {'harness': 'stub', 'steps': []},
            'codex': {'harness': 'codex', 'final_message': '', 'session_id': None, 'usage': None, 'cli_error': None},
            'claude': {'harness': 'claude', 'decision': None, 'notes': failure_detail or ''},
        }.get(harness, {'harness': harness})
        report['checks'] = None
        if outcome == 'succeeded':
            report['checks'] = dict(manifest_sha256=p.manifest_sha256(self.meta['checkpoints'][-1]['manifest']),
                node_id=self.agent.config['node_id'], platform=dict(os=self.agent.capabilities.get('os'),
                    release=platform.release(), machine=platform.machine(), python=platform.python_version()),
                results=self.check_runner.results if self.check_runner else [])
        report = bounded_report(self.agent.clean(report))
        self.meta['completion'] = dict(outcome=outcome, failure_class=failure_class,
            failure_detail=self.agent.clean(failure_detail), output_checkpoint_id=checkpoint_id,
            report=report, native_session_ref=self.native_session_ref, runtime=self.runtime())
        self.meta['state'] = 'finished_unreported'
        self.persist()

    def quarantine(self, reason):
        stopped = self.adapter.stop() if self.adapter else not self.meta.get('restart_unknown', False)
        if self.check_runner:
            stopped = self.check_runner.stop() and stopped
        state = 'abandoned' if self.meta['state'] == 'abandoned' else 'quarantined'
        self.meta.update(state=state, quarantine_reason=reason, stop_verified=stopped)
        save(self.directory / 'quarantined.json', dict(reason=reason, at=now(), stop_verified=stopped))
        self.persist()

    def cancel(self):
        if self.meta['state'] in ('quarantined', 'abandoned', 'reported'):
            return
        stopped = self.adapter.stop() if self.adapter else True
        if self.check_runner:
            stopped = self.check_runner.stop() and stopped
        if not stopped:
            self.quarantine('cancel could not confirm process termination')
            return
        self.meta['completion'] = None
        self.finish('cancelled', 'user_cancel', 'controller requested cancellation')

    def tick(self):
        if self.adapter and self.meta['state'] == 'running':
            try:
                if self.check_runner:
                    self.check_runner.tick()
                else:
                    self.adapter.tick()
            except (ValueError, OSError, KeyError, TypeError) as exc:
                self.adapter.stop()
                if self.check_runner:
                    self.check_runner.stop()
                failure = ('capability_missing' if isinstance(exc, FileNotFoundError) else
                           'infra_retryable' if isinstance(exc, OSError) else 'code_failure')
                self.finish('failed', failure, str(exc))

    def flush_events(self):
        if not self.events:
            return
        response = self.agent.client.json('POST', '/attempts/' + self.id + '/events',
                                          self.fenced_body(events=self.events))
        acknowledged = set(response['accepted']) | set(response['duplicates'])
        self.events = [event for event in self.events if event['event_id'] not in acknowledged]
        atomic_write(self.events_path, b''.join(p.canonical_json(event) + b'\n' for event in self.events))

    def upload_checkpoint(self, checkpoint):
        manifest = checkpoint['manifest']
        hashes = [entry['sha256'] for entry in manifest['files']] + [p.manifest_sha256(manifest)]
        for sha in dict.fromkeys(hashes):
            self.agent.client.put_blob(sha, self.agent.fetch_blob(sha))
            self.ensure_authority()
        body = self.fenced_body(**{key: value for key, value in checkpoint.items() if key != 'durable'})
        path = '/attempts/' + self.id + '/checkpoints'
        try:
            result = self.agent.client.json('POST', path, body)
        except APIError as exc:
            if exc.code != 'missing_blobs':
                raise
            for sha in exc.details.get('missing', []):
                self.agent.client.put_blob(sha, self.agent.fetch_blob(sha))
                self.ensure_authority()
            result = self.agent.client.json('POST', path, body)
        if result.get('state') != 'durable':
            raise ValueError('controller did not acknowledge a durable checkpoint')
        checkpoint['durable'] = True
        self.persist()
        self.agent.durable_count += 1
        if self.agent.fault_exit_after_checkpoint and self.agent.durable_count == self.agent.fault_exit_after_checkpoint:
            os._exit(137)

    def ensure_authority(self):
        if self.clock() >= self.agent.next_heartbeat:
            self.agent.heartbeat()
        if not self.authority_current():
            raise APIError(409, p.error_body('fenced', 'Local authority is unconfirmed'))

    def authority_current(self):
        confirmed = self.last_renewed
        if self.meta['state'] == 'finished_unreported' and self.last_reconciled is not None:
            # Succeeded attempts are no longer renewed by the controller. A
            # successful reconciliation permits replay; endpoint fencing decides.
            confirmed = max(confirmed or self.last_reconciled, self.last_reconciled)
        return (self.meta['state'] not in ('quarantined', 'abandoned') and self.agent.reconciled and
                confirmed is not None and self.clock() - confirmed < self.agent.lease_ttl_s)

    def flush(self):
        self.flush_events()  # Late events are allowed even after revocation.
        if self.meta['state'] in ('quarantined', 'abandoned', 'reported') or not self.lease_token:
            return
        if not self.authority_current():
            return
        for checkpoint in self.meta['checkpoints']:
            if not checkpoint['durable']:
                self.upload_checkpoint(checkpoint)
        completion = self.meta.get('completion')
        if not completion:
            return
        if not self.meta.get('complete_ack'):
            self.ensure_authority()
            completion = self.meta['completion']  # Heartbeat may have delivered cancellation.
            self.agent.client.json('POST', '/attempts/' + self.id + '/complete', self.fenced_body(**completion))
            self.meta['complete_ack'] = True
            self.persist()
        if completion['outcome'] == 'succeeded' and self.contract.get('publish', {}).get('mode') == 'auto':
            publication = self.meta.get('publication')
            if publication is None:
                publication = dict(request_id=str(uuid.uuid4()), checkpoint_id=completion['output_checkpoint_id'],
                                   expected_base_version=self.assignment['delivery_version'])
                self.meta['publication'] = publication
                self.persist()
            if 'receipt' not in publication and 'error' not in publication:
                try:
                    self.ensure_authority()
                    if self.meta['completion']['outcome'] != 'succeeded':
                        return
                    publication['receipt'] = self.agent.client.json('POST', '/jobs/' + self.assignment['job_id'] + '/publish',
                        self.fenced_body(attempt_id=self.id, **publication))
                except APIError as exc:
                    if exc.code == 'fenced':
                        raise
                    # Including version_mismatch: never change the expected base
                    # or create a new publication request on an application error.
                    publication['error'] = self.agent.clean(exc.body)
                self.persist()
        self.meta['state'] = 'reported'
        self.persist()


class Agent:
    def __init__(self, data_dir, transport=None, clock=time.monotonic, rng=None, capabilities=None,
                 fault_exit_after_checkpoint=None, commands=None, auth_runner=None, system=None):
        self.directory = Path(data_dir).absolute()
        self.config = load(self.directory / 'agent.json')
        p.check_node_id(self.config['node_id'])
        token = (self.directory / 'node.token').read_text(encoding='utf-8').strip()
        if not token or not hmac.compare_digest(p.sha256_bytes(token.encode()), self.config['node_token_sha256']):
            raise ValueError('node token is missing or does not match configuration')
        self.client = Client(self.config['controller'], token, self.config.get('ca_file'), transport)
        self.clock = clock
        self.system = system or platform.system()
        self.environment = dict(os.environ)
        self.commands = commands if commands is not None else resolve_commands(self.config, self.environment, self.system)
        self.release = release_info()
        self.auth_runner = auth_runner
        self.auth_probe_s = float(self.config.get('auth_probe_s', 900))
        if not 0 < self.auth_probe_s < float('inf'):
            raise ValueError('auth_probe_s must be positive and finite')
        self.auth_health = {'codex': 'unknown', 'claude': 'unknown'}
        self.auth_thread = None
        self.next_auth_probe = 0
        self.refresh_auth()
        self.rng = rng or random.Random()
        self.backoff = Backoff(self.rng)
        self.capabilities = dict(capabilities) if capabilities is not None else detect(self.config, self.commands)
        self.capabilities['agent'] = dict(self.release)
        self.capabilities['labels'] = sorted(set(self.capabilities.get('labels', []) + self.config.get('labels', [])))
        self.heartbeat_s = p.DEFAULTS['heartbeat_s']
        self.lease_ttl_s = p.DEFAULTS['lease_ttl_s']
        self.generation = self.config['authority_generation']
        self.registered = False
        self.connected = False
        self.reconciled = False
        self.drain = True
        self.dispatch_paused = True
        self.next_network = 0
        self.next_heartbeat = 0
        self.attempts = {}
        self.secrets = {token}
        for credential in (self.directory / 'attempts').glob('*/lease.token'):
            try:
                self.secrets.add(credential.read_text(encoding='utf-8'))
            except OSError:
                pass
        self.client.error_filter = self.clean
        self.client.wait_hook = self.tick_local
        self.last_error = None
        self.durable_count = 0
        self.fault_exit_after_checkpoint = fault_exit_after_checkpoint
        for path in sorted((self.directory / 'attempts').glob('*/meta.json')):
            meta = load(path)
            attempt = Attempt(self, meta['assignment'], meta)
            self.attempts[attempt.id] = attempt

    def refresh_auth(self, background=False):
        if self.clock() < self.next_auth_probe or (self.auth_thread and self.auth_thread.is_alive()):
            return
        self.next_auth_probe = self.clock() + self.auth_probe_s
        def run():
            self.auth_health = {name: probe_auth(name, command, self.auth_runner)
                                for name, command in self.commands.items()}
        if background:
            self.auth_thread = threading.Thread(target=run, name='auth-status', daemon=True)
            self.auth_thread.start()
        else:
            run()

    def clean(self, value):
        return redact(value, sorted(self.secrets, key=len, reverse=True))

    def cache_blob(self, sha, data):
        p.check_sha256(sha)
        if not hmac.compare_digest(p.sha256_bytes(data), sha):
            raise ValueError('cache blob hash mismatch')
        path = self.directory / 'blobs' / sha
        if not path.exists():
            atomic_write(path, data)

    def fetch_blob(self, sha):
        p.check_sha256(sha)
        path = self.directory / 'blobs' / sha
        if path.exists():
            data = path.read_bytes()
            if not hmac.compare_digest(p.sha256_bytes(data), sha):
                raise ValueError('cached blob hash mismatch')
            return data
        data = self.client.fetch_blob(sha)
        self.cache_blob(sha, data)
        return data

    def register(self):
        config = self.config
        record = dict(node_id=config['node_id'], physical_host_id=config['physical_host_id'],
            failure_domain=config['failure_domain'], capabilities=self.capabilities,
            runtime_profile=dict(user=getpass.getuser(), workspace_policy='isolated-attempt'),
            resources=dict(slots=config.get('slots', 1), host_slots=config.get('host_slots', 1), mem_mb=None),
            provider_account_refs=config.get('provider_account_refs', {}),
            auth_health=self.auth_health, agent_version=self.release['version'])
        result = self.client.json('POST', '/nodes/register', record)
        self.generation = result['authority_generation']
        for attempt in self.attempts.values():
            if attempt.assignment['authority_generation'] != self.generation and attempt.meta['state'] != 'reported':
                attempt.quarantine('controller generation changed')
        self.heartbeat_s = float(result['config']['heartbeat_s'])
        self.lease_ttl_s = float(result['config']['lease_ttl_s'])
        if self.heartbeat_s <= 0 or self.lease_ttl_s <= 0:
            raise ValueError('invalid controller timing configuration')
        self.registered = True

    def heartbeat(self):
        states = []
        for a in self.attempts.values():
            if a.meta['state'] == 'reported':
                continue
            state = 'unknown' if a.meta['state'] in ('quarantined', 'abandoned') else 'finished_unreported' if a.meta.get('completion') else 'running'
            states.append(dict(attempt_id=a.id, epoch=a.assignment['epoch'], state=state))
        sent_at = self.clock()
        result = self.client.json('POST', '/nodes/' + self.config['node_id'] + '/heartbeat',
            dict(authority_generation=self.generation, attempts=states,
                 auth_health=self.auth_health))
        save(self.directory / 'status.json', dict(self.release, pid=os.getpid(),
             version_dir=str(version_directory()), last_heartbeat_ok_at=now()))
        for item in result.get('revoked', []):
            if item['attempt_id'] in self.attempts:
                self.attempts[item['attempt_id']].quarantine(item['reason'])
        for identity in result.get('cancel', []):
            if identity in self.attempts:
                self.attempts[identity].cancel()
        for identity in result.get('renewed', []):
            if identity in self.attempts:
                # Conservatively count the whole network round trip against TTL.
                self.attempts[identity].last_renewed = sent_at
        self.reconciled = not result['reconcile_required']
        revoked = {item['attempt_id'] for item in result.get('revoked', [])}
        if self.reconciled:
            for entry in states:
                if entry['attempt_id'] not in revoked:
                    self.attempts[entry['attempt_id']].last_reconciled = sent_at
        if not self.reconciled:
            for attempt in self.attempts.values():
                if attempt.meta['state'] != 'reported':
                    attempt.quarantine('controller requires reconciliation')
            # Register refreshes generation; the next heartbeat still includes
            # old local attempts as unknown before dispatch can resume.
            self.registered = False
        self.drain, self.dispatch_paused = result['drain'], result['dispatch_paused']
        self.next_heartbeat = self.clock() + self.heartbeat_s * self.rng.uniform(.8, 1.2)
        if self.reconciled:
            self.integrations(result.get('integrations', []))

    def integrations(self, items):
        for item in items:
            # §11.10 routes items by authenticated heartbeat; tolerate an explicit
            # target guard from a newer controller without requiring a new field.
            if item.get('target_node_id', self.config['node_id']) != self.config['node_id']:
                continue
            job, checkpoint = p.check_uuid(item['job_id']), p.check_uuid(item['checkpoint_id'])
            directory = self.directory / 'integrations' / job / checkpoint
            path = directory / 'progress.json'
            progress = load(path) if path.exists() else dict(item=item, started=False, reported=False,
                record=dict(request_id=str(uuid.uuid4()), checkpoint_id=checkpoint,
                            status='integration_pending', detail=dict(changed=[], conflicts=[], backup=None)))
            if progress['item'] != item:
                raise ValueError('integration identity changed')
            if not path.exists():
                # Import rc4 receipts/intents before creating an rc5 delivery. Never
                # write twice when upgrading after an old execute-node integration.
                for attempt in self.attempts.values():
                    legacy = attempt.meta.get('integration')
                    if (attempt.assignment['job_id'] == job and legacy and legacy['checkpoint_id'] == checkpoint):
                        progress['record'] = legacy
                        progress['started'] = bool(attempt.meta.get('integration_started'))
                        break
                save(path, progress)
            if progress['reported']:
                # A restored controller can offer an already acknowledged item.
                # Replay the same receipt, never touch its target again.
                self.client.json('POST', '/jobs/' + job + '/integration', progress['record'])
                continue
            record = progress['record']
            if record['status'] == 'integration_pending':
                if progress['started']:
                    record.update(request_id=str(uuid.uuid4()), status='conflict', detail=dict(changed=[],
                        conflicts=['Previous integration result unknown; manual review required'], backup=str(directory / 'backup')))
                else:
                    self.client.json('POST', '/jobs/' + job + '/integration', record)
                    # Fetch/validate before recording write intent; network failure
                    # before this boundary can be retried without touching target.
                    manifests = []
                    for key in ('base_manifest_sha256', 'manifest_sha256'):
                        sha = item.get(key)
                        manifest = checked_manifest(json.loads(self.fetch_blob(sha))) if sha else dict(EMPTY)
                        if sha and p.manifest_sha256(manifest) != sha:
                            raise ValueError('integration manifest is not canonical')
                        manifests.append(manifest)
                    for entry in manifests[1]['files']:
                        self.fetch_blob(entry['sha256'])
                    progress['started'] = True
                    save(path, progress)
                    try:
                        result = integrate(item['target_path'], manifests[0], manifests[1], self.fetch_blob,
                                           directory / 'backup', exclude=item.get('exclude', DEFAULT_EXCLUDES))
                        detail = {key: result[key] for key in ('changed', 'conflicts', 'backup', 'errors') if key in result}
                        status = result['status']
                    except (ValueError, OSError):
                        status, detail = 'conflict', dict(changed=[], conflicts=['Integration interrupted; manual review required'],
                                                         backup=str(directory / 'backup'))
                    record.update(request_id=str(uuid.uuid4()), status=status, detail=detail)
                save(path, progress)
            self.client.json('POST', '/jobs/' + job + '/integration', record)
            progress['reported'] = True
            save(path, progress)

        # A controller may have committed a receipt but lost its response and
        # therefore stopped sending the item. Replay only outcomes, never writes.
        for path in (self.directory / 'integrations').glob('*/*/progress.json'):
            progress = load(path)
            if not progress['reported'] and progress['started']:
                record = progress['record']
                if record['status'] == 'integration_pending':
                    record.update(request_id=str(uuid.uuid4()), status='conflict', detail=dict(changed=[],
                        conflicts=['Previous integration result unknown; manual review required'], backup=str(path.parent / 'backup')))
                    save(path, progress)
                self.client.json('POST', '/jobs/' + progress['item']['job_id'] + '/integration', record)
                progress['reported'] = True
                save(path, progress)

    def claim(self):
        used = sum(a.contract.get('resources', {}).get('slots', 1) for a in self.attempts.values()
                   if a.meta['state'] not in ('reported', 'quarantined', 'abandoned'))
        if (not self.connected or not self.reconciled or self.drain or self.dispatch_paused or
                used >= self.config.get('slots', 1) or
                any(a.meta.get('stop_verified') is False or a.meta.get('restart_unknown') for a in self.attempts.values())):
            return
        sent_at = self.clock()
        response = self.client.json('POST', '/nodes/' + self.config['node_id'] + '/claim', {})
        if response and response.get('assignment'):
            assignment = response['assignment']
            identity = assignment['attempt_id']
            if identity in self.attempts:
                raise ValueError('controller repeated an existing attempt assignment')
            attempt = Attempt(self, assignment)
            attempt.last_renewed = sent_at
            self.attempts[identity] = attempt
            if assignment['authority_generation'] != self.generation:
                attempt.quarantine('assignment generation mismatch')

    def cycle(self, claim_allowed=True):
        self.refresh_auth(background=True)
        for attempt in self.attempts.values():
            attempt.refresh_restart()
        if self.clock() >= self.next_network:
            try:
                if not self.registered:
                    self.register()
                if not self.connected or not self.reconciled or self.clock() >= self.next_heartbeat:
                    self.heartbeat()
                self.connected = True
                for attempt in self.attempts.values():
                    try:
                        if self.reconciled:
                            attempt.prepare()
                            attempt.flush()
                        else:
                            attempt.flush_events()
                    except APIError as exc:
                        if exc.code == 'fenced':
                            attempt.quarantine('controller fenced attempt')
                        else:
                            attempt.meta['error'] = self.clean(exc.body)
                            attempt.persist()
                    except (ValueError, OSError, KeyError, TypeError) as exc:
                        if attempt.adapter:
                            attempt.adapter.stop()
                        attempt.finish('failed', 'code_failure', str(exc))
                if claim_allowed:
                    self.claim()
                self.backoff.reset()
                self.next_network = self.clock() + .25
            except ConnectionFailure:
                self.connected = False
                self.reconciled = False
                self.next_network = self.clock() + self.backoff.next()
                self.last_error = p.error_body('invalid', 'Controller unavailable; awaiting reconciliation', reason='network')
            except APIError as exc:
                self.connected = False
                self.reconciled = False
                self.next_network = self.clock() + self.backoff.next()
                self.last_error = self.clean(exc.body)
        self.tick_local()

    def tick_local(self):
        for attempt in list(self.attempts.values()):
            attempt.tick()

    def close(self):
        for attempt in self.attempts.values():
            if attempt.adapter and attempt.meta['state'] == 'running':
                attempt.quarantine('agent stopped')


def pair(args, transport=None):
    directory = Path(args.data_dir).absolute()
    p.check_node_id(args.node_id)
    if args.slots < 1 or args.host_slots < 1:
        raise ValueError('slots and host-slots must be positive')
    if (directory / 'agent.json').exists() or (directory / 'node.token').exists():
        raise ValueError('data directory is already paired; use a new directory')
    client = Client(args.controller, ca_file=args.ca_file, transport=transport)
    result = client.json('POST', '/nodes/pair', dict(code=args.code, node_id=args.node_id))
    if result['node_id'] != args.node_id or not isinstance(result['node_token'], str) or not result['node_token'].strip():
        raise ValueError('invalid pairing response')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Protocol §2 explicitly requires this client-side bearer file. Controller
    # stores hashes; agent.json stores only a hash and never the credential.
    atomic_write(directory / 'node.token', result['node_token'].encode())
    config = dict(controller=validate_url(args.controller), node_id=args.node_id,
                  controller_id=result['controller_id'], authority_generation=result['authority_generation'],
                  node_token_sha256=p.sha256_bytes(result['node_token'].encode()),
                  ca_file=str(Path(args.ca_file).absolute()) if args.ca_file else None,
                  physical_host_id=args.physical_host_id or platform.node(),
                  failure_domain=args.failure_domain or args.physical_host_id or platform.node(),
                  slots=args.slots, host_slots=args.host_slots, labels=args.label)
    save(directory / 'agent.json', config)
    return dict(node_id=args.node_id, data_dir=str(directory), paired=True)


def doctor(data_dir, transport=None, commands=None, runner=None, system=None):
    directory = Path(data_dir)
    checks = {}
    warnings = []
    config = {}
    try:
        config = load(directory / 'agent.json')
        p.check_node_id(config['node_id'])
        validate_url(config['controller'])
        checks['configuration'] = True
        token_path = directory / 'node.token'
        token = token_path.read_text(encoding='utf-8').strip()
        checks['token'] = bool(token) and hmac.compare_digest(p.sha256_bytes(token.encode()), config['node_token_sha256'])
        checks['permissions'] = os.name == 'nt' or all((path.stat().st_mode & 0o077) == 0 for path in (token_path, directory / 'agent.json'))
        client = Client(config['controller'], token, config.get('ca_file'), transport)
        checks['controller_reachable_verified_tls_or_loopback'] = client.json('GET', '/healthz').get('ok') is True
    except (OSError, ValueError, KeyError, APIError, ConnectionFailure):
        checks['controller_reachable_verified_tls_or_loopback'] = False
    try:
        root = Path(config['workspace_root']).expanduser() if config.get('workspace_root') else directory / 'attempts'
        path = root / ('doctor-' + uuid.uuid4().hex)
        atomic_write(path, b'probe')
        path.unlink()
        checks['workspace_writable'] = True
    except OSError:
        checks['workspace_writable'] = False
    commands = commands if commands is not None else resolve_commands(config, system=system)
    capabilities = detect(config, commands, runner)
    checks['harness'] = {name: bool(value) for name, value in capabilities['harness'].items()}
    if (system or platform.system()) == 'Windows' and checks['harness']['codex']:
        import ntpath
        profile = os.environ.get('USERPROFILE')
        if profile:
            try:
                if ntpath.commonpath([ntpath.abspath(str(root)), ntpath.abspath(profile)]).casefold() == ntpath.abspath(profile).casefold():
                    warnings.append('workspace_in_profile')
            except ValueError:
                pass
    blockers, secrets = [], [locals().get('token')]
    credentials_ok = True
    for path in sorted((directory / 'attempts').glob('*/meta.json')):
        try:
            meta = load(path)
            credential = path.with_name('lease.token')
            lease = credential.read_text(encoding='utf-8') if credential.exists() else None
            secrets.append(lease)
            if meta['state'] in ('preparing', 'running', 'finished_unreported'):
                credentials_ok = credentials_ok and bool(lease) and hmac.compare_digest(
                    p.sha256_bytes(lease.encode()), meta['lease_token_sha256'])
            if lease and os.name != 'nt':
                credentials_ok = credentials_ok and credential.stat().st_mode & 0o077 == 0
            if meta.get('restart_unknown'):
                status = process_status(saved_process(meta))
                if status != 'gone':
                    blockers.append(restart_notice(path.parent, meta, status))
        except (OSError, ValueError, KeyError):
            credentials_ok = False
    checks.update(attempt_credentials=credentials_ok, restart_clear=not blockers)
    result = dict(ok=all(value for key, value in checks.items() if key != 'harness'),
                  checks=checks, restart_blockers=blockers, warnings=warnings, capabilities=capabilities,
                  auth_health={name: probe_auth(name, command, runner) for name, command in commands.items()})
    return redact(result, secrets)


class AgentLock:
    def __init__(self, directory):
        self.path = Path(directory) / 'agent.lock'
        self.file = None

    def __enter__(self):
        self.file = os.fdopen(os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600), 'r+b')
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.file.write(b'0')
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise ValueError('another agent owns this data directory') from None
        return self

    def __exit__(self, *args):
        self.file.close()


def serve(args):
    with AgentLock(args.data_dir):
        agent = Agent(args.data_dir, fault_exit_after_checkpoint=args.fault_exit_after_checkpoint)
        stopped = [False]
        def stop(signum, frame):
            stopped[0] = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            first = True
            while not stopped[0]:
                agent.cycle(claim_allowed=first or not args.once)
                first = False
                if args.once and all(a.meta['state'] in ('reported', 'quarantined', 'abandoned') for a in agent.attempts.values()):
                    break
                time.sleep(.1)
        finally:
            agent.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    pairing = commands.add_parser('pair')
    for name in ('controller', 'code', 'node-id', 'data-dir'):
        pairing.add_argument('--' + name, required=True)
    for name in ('ca-file', 'physical-host-id', 'failure-domain'):
        pairing.add_argument('--' + name)
    pairing.add_argument('--slots', type=int, default=1)
    pairing.add_argument('--host-slots', type=int, default=1)
    pairing.add_argument('--label', action='append', default=[])
    run = commands.add_parser('run')
    run.add_argument('--data-dir', required=True)
    run.add_argument('--once', action='store_true')
    run.add_argument('--foreground', action='store_true', help='service-manager mode')
    run.add_argument('--fault-exit-after-checkpoint', type=int)
    detecting = commands.add_parser('detect')
    detecting.add_argument('--data-dir', help='read optional command/disable configuration')
    check = commands.add_parser('doctor')
    check.add_argument('--data-dir', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'pair':
            result = pair(args)
        elif args.command == 'detect':
            result = detect(load(Path(args.data_dir) / 'agent.json') if args.data_dir else None)
        elif args.command == 'doctor':
            result = doctor(args.data_dir)
        elif args.once or args.foreground:
            serve(args)
            return 0
        else:
            directory = Path(args.data_dir).absolute()
            load(directory / 'agent.json')
            command = [sys.executable, '-m', 'agent_collab.node_agent', 'run', '--data-dir', str(directory), '--foreground']
            if args.fault_exit_after_checkpoint:
                command += ['--fault-exit-after-checkpoint', str(args.fault_exit_after_checkpoint)]
            options = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
            with (directory / 'agent.log').open('ab') as log:
                child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                         close_fds=True, **options)
            result = dict(pid=child.pid, status='spawned', log=str(directory / 'agent.log'))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, APIError, ConnectionFailure) as exc:
        # 锁冲突信息不含路径或凭据，可以原样给出；其他错误保持笼统，避免泄露配置细节。
        known = str(exc) if str(exc) == 'another agent owns this data directory' else None
        print(json.dumps(p.error_body('conflict' if known else 'invalid',
                                      known or 'Agent command failed; check configuration and controller access')))
        return 1


if __name__ == '__main__':
    sys.exit(main())
