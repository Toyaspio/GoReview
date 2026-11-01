"""Durable queue, quotas, cache, call ledger. One worker process, transactional claims."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import sqlite3
import time
import uuid
from .errors import ReviewError

ACTIVE = ('queued', 'parsing', 'analyzing', 'rendering')
STATES = {'queued': '等待处理', 'parsing': '正在解析', 'analyzing': '正在分析',
          'rendering': '正在生成报告', 'done': '已完成', 'failed': '失败'}


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs(
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, url TEXT NOT NULL, mode TEXT NOT NULL,
              state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
              cache_key TEXT, error_code TEXT, error TEXT, cache_hit INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, payload TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS quota(job_id TEXT PRIMARY KEY, day TEXT NOT NULL, owner TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY, job_id TEXT NOT NULL, ceiling INTEGER NOT NULL,
              state TEXT NOT NULL, usage TEXT, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS job_owner ON jobs(owner,created);
            CREATE INDEX IF NOT EXISTS quota_day ON quota(day);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def submit(self, owner, url, mode, *, cooldown=60, max_pending=20):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            now = time.time()
            existing = db.execute("SELECT * FROM jobs WHERE owner=? AND url=? AND mode=? AND state IN ('queued','parsing','analyzing','rendering')", (owner, url, mode)).fetchone()
            if existing:
                return dict(existing)
            last = db.execute('SELECT created,state FROM jobs WHERE owner=? ORDER BY created DESC LIMIT 1', (owner,)).fetchone()
            if last is not None and last['state'] in ACTIVE and now-last['created'] < cooldown:
                raise ReviewError('rate_limit', '提交过于频繁，请稍后再试。')
            if db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','parsing','analyzing','rendering')").fetchone()[0] >= max_pending:
                raise ReviewError('queue_full', '复盘队列已满，请稍后再试。')
            job_id = uuid.uuid4().hex[:20]
            db.execute('INSERT INTO jobs(id,owner,url,mode,state,created,updated) VALUES(?,?,?,?,?,?,?)',
                       (job_id, owner, url, mode, 'queued', now, now))
            return dict(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def get(self, job_id, owner=None):
        with self.db() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not row or (owner is not None and row['owner'] != owner):
                raise ReviewError('not_found', '任务不存在或不属于当前用户。')
            return dict(row)

    def claim(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET state='parsing',updated=? WHERE id=?", (time.time(), row['id']))
            return dict(row)

    def state(self, job_id, state, *, code=None, message=None, cache_key=None, cache_hit=False):
        if state not in STATES:
            raise ValueError('state')
        with self.db() as db:
            db.execute('UPDATE jobs SET state=?,updated=?,error_code=?,error=?,cache_key=COALESCE(?,cache_key),cache_hit=? WHERE id=?',
                       (state, time.time(), code, message, cache_key, int(cache_hit), job_id))

    def recover(self):
        with self.db() as db:
            db.execute("UPDATE jobs SET state='failed',error_code='worker_interrupted',error='Worker 上次运行中断，可能已有用量；不自动重新分析。',updated=? WHERE state IN ('parsing','analyzing','rendering')", (time.time(),))

    def reserve(self, job, daily_limit, user_daily_limit):
        if daily_limit < 0 or user_daily_limit < 0:raise ValueError('invalid daily limit')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            day = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
            total = db.execute('SELECT COUNT(*) FROM quota WHERE day=?', (day,)).fetchone()[0]
            user = db.execute('SELECT COUNT(*) FROM quota WHERE day=? AND owner=?', (day, job['owner'])).fetchone()[0]
            if (daily_limit and total >= daily_limit) or (user_daily_limit and user >= user_daily_limit):
                raise ReviewError('daily_limit', '今日分析额度已用完（失败请求保守计次）。')
            db.execute('INSERT INTO quota VALUES(?,?,?)', (job['id'], day, job['owner']))

    def cached(self, key, *, ttl=604800):
        with self.db() as db:
            row = db.execute('SELECT payload FROM cache WHERE key=? AND created>?', (key, time.time()-ttl)).fetchone()
            return json.loads(row[0]) if row else None

    def cache(self, key, payload):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?)', (key, json.dumps(payload, ensure_ascii=False, allow_nan=False), time.time()))

    def intent(self, job_id, ceiling):
        call_id = uuid.uuid4().hex
        with self.db() as db:
            db.execute('INSERT INTO calls VALUES(?,?,?,?,?,?)', (call_id, job_id, ceiling, 'uncertain', None, time.time()))
        return call_id

    def usage(self, call_id, usage):
        with self.db() as db:
            db.execute("UPDATE calls SET state='reported',usage=? WHERE id=?", (json.dumps(usage), call_id))

    def usage_for(self, job_id):
        with self.db() as db:
            rows = db.execute('SELECT ceiling,state,usage FROM calls WHERE job_id=?', (job_id,)).fetchall()
            return [{'ceiling': r['ceiling'], 'state': r['state'], 'usage': json.loads(r['usage']) if r['usage'] else None} for r in rows]
