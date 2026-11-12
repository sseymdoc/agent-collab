"""SQLite authority. Group record changes and events in transaction()."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
import uuid

from . import protocol as p

SCHEMA = '''
CREATE TABLE IF NOT EXISTS records(kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
 PRIMARY KEY(kind,id));
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
 job_id TEXT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event_keys(event_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requests(endpoint TEXT NOT NULL, request_id TEXT NOT NULL,
 request_hash TEXT NOT NULL, status INTEGER NOT NULL, headers TEXT NOT NULL, body BLOB NOT NULL,
 PRIMARY KEY(endpoint,request_id));
CREATE TABLE IF NOT EXISTS manifest_refs(job_id TEXT NOT NULL, sha256 TEXT NOT NULL,
 PRIMARY KEY(job_id,sha256));
'''


class Store:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / 'controller.db'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript(SCHEMA)
        self._migrate_rc5()

    def _migrate_rc5(self):
        # JSON record migration: preserve immutable contracts, receipts and row versions.
        with self.transaction():
            for kind in ('job', 'job_private', 'node'):
                for row in self.db.execute('SELECT id,data FROM records WHERE kind=?', (kind,)).fetchall():
                    record = json.loads(row['data'])
                    if kind == 'node':
                        record.setdefault('capability_evidence', {})
                    else:
                        state = record if kind == 'job' else (record.get('managed') or {}).get('target_state', {})
                        if 'acceptance' in state:
                            state['acceptance'].setdefault('requested_status', None)
                            state['acceptance'].setdefault('evidence_gaps', [])
                    if record != json.loads(row['data']):
                        self.put(kind, row['id'], record)
            for row in self.db.execute("SELECT id,data FROM records WHERE kind='coordinator_trigger'").fetchall():
                parts = row['id'].split(':')
                if len(parts) != 3:
                    continue
                receipt = json.loads(row['data'])
                private = self.get('job_private', receipt['job_id'], {})
                state = (private.get('managed') or {}).get('target_state')
                if state:
                    key = '%s:%s:%s:%s' % (parts[0], parts[1], state['durable_checkpoint_id'], state['acceptance']['status'])
                    if self.get('coordinator_trigger', key) is None:
                        self.put('coordinator_trigger', key, receipt)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield self
            except BaseException:
                self.db.rollback()
                raise
            else:
                self.db.commit()

    def get(self, kind, ident, default=None):
        row = self.db.execute('SELECT data FROM records WHERE kind=? AND id=?', (kind, ident)).fetchone()
        return json.loads(row[0]) if row else default

    def all(self, kind):
        return [json.loads(row[0]) for row in self.db.execute('SELECT data FROM records WHERE kind=? ORDER BY rowid', (kind,))]

    def put(self, kind, ident, value):
        self.db.execute('INSERT INTO records VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data',
                        (kind, ident, p.canonical_json(value).decode()))

    def delete(self, kind, ident):
        self.db.execute('DELETE FROM records WHERE kind=? AND id=?', (kind, ident))

    def event(self, event_type, payload, at, job_id=None, attempt_id=None, node_id=None,
              event_id=None, stale=False, request_hash=None):
        ident = event_id or str(uuid.uuid4())
        data = {'seq': None, 'event_id': ident, 'job_id': job_id, 'attempt_id': attempt_id,
                'node_id': node_id, 'type': event_type, 'payload': payload, 'stale': stale, 'created_at': at}
        self.db.execute('INSERT INTO event_keys VALUES(?,?)', (ident, request_hash or p.value_hash(data)))
        cursor = self.db.execute('INSERT INTO events(event_id,job_id,data) VALUES(?,?,?)', (ident, job_id, '{}'))
        data['seq'] = cursor.lastrowid
        self.db.execute('UPDATE events SET data=? WHERE seq=?', (p.canonical_json(data).decode(), data['seq']))
        return data

    def events(self, after=0, limit=200, job_id=None, before=None):
        if job_id is None and before is not None:
            # 全局事件向前翻页：序号小于 before 的最近 limit 条，按序号升序返回。
            rows = self.db.execute('SELECT data FROM events WHERE seq<? ORDER BY seq DESC LIMIT ?', (before, limit)).fetchall()
            return [json.loads(row[0]) for row in reversed(rows)]
        if job_id is not None:
            # before：只取序号更小的事件，供界面向前翻页；缺省取最近的 limit 条。
            rows = self.db.execute('SELECT data FROM events WHERE job_id=? AND seq<? ORDER BY seq DESC LIMIT ?',
                                   (job_id, before if before is not None else 1 << 62, limit)).fetchall()
            return [json.loads(row[0]) for row in reversed(rows)]
        return [json.loads(row[0]) for row in self.db.execute('SELECT data FROM events WHERE seq>? ORDER BY seq LIMIT ?', (after, limit))]

    def prune_events(self, keep):
        if type(keep) is not int or keep < 0:
            raise ValueError('keep must be nonnegative')
        with self.transaction():
            self.db.execute('DELETE FROM events WHERE seq NOT IN (SELECT seq FROM events ORDER BY seq DESC LIMIT ?)', (keep,))

    def close(self):
        with self.lock:
            self.db.close()
