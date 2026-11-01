"""At-most-once automatic delivery; uncertain sends require operator review."""
from contextlib import contextmanager
import sqlite3
import time


class DeliveryStore:
    def __init__(self, path):
        self.path = str(path)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS deliveries(job TEXT PRIMARY KEY,owner TEXT,stream TEXT,state TEXT,summary TEXT,updated REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS incoming(key TEXT PRIMARY KEY,created REAL NOT NULL)')
            db.execute("UPDATE deliveries SET state='uncertain' WHERE state IN ('sending','preparing')")

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

    def add(self, job, owner, stream):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO deliveries VALUES(?,?,?,'waiting',NULL,?)", (job,owner,stream,time.time()))

    def claim_message(self,key):
        with self.db() as db:
            return db.execute('INSERT OR IGNORE INTO incoming VALUES(?,?)',(key,time.time())).rowcount==1

    def waiting(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM deliveries WHERE state='waiting'")]

    def state(self, job, state, summary=None):
        with self.db() as db:
            db.execute('UPDATE deliveries SET state=?,summary=COALESCE(?,summary),updated=? WHERE job=?',
                       (state,summary,time.time(),job))

    def get(self, job):
        with self.db() as db:
            row = db.execute('SELECT * FROM deliveries WHERE job=?', (job,)).fetchone()
            return dict(row) if row else None
