"""Durable ready transitions and single-daemon capacity reservations."""
import hashlib
import hmac
import secrets
import re
import sqlite3
import time

ACTIVE = ('dispatching', 'running', 'uncertain')


class Queue:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS observations(item TEXT PRIMARY KEY,issue INTEGER NOT NULL,
            ready INTEGER NOT NULL,generation INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS jobs(delivery TEXT PRIMARY KEY,item TEXT NOT NULL,
            issue INTEGER NOT NULL,generation INTEGER NOT NULL,url TEXT NOT NULL,
            state TEXT NOT NULL,worker TEXT,daemon TEXT,run_id TEXT,created REAL NOT NULL,
            UNIQUE(item,generation));
          CREATE TABLE IF NOT EXISTS cloud_runs(delivery TEXT PRIMARY KEY,provider TEXT NOT NULL,
            run_id TEXT NOT NULL,url TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS completions(delivery TEXT PRIMARY KEY,token_hash TEXT NOT NULL,
            expires REAL NOT NULL,pr_url TEXT);
        ''')

    def observe(self, project, items, ready_name='Ready'):
        """Commit a COMPLETE successful project snapshot; first scan only establishes baseline."""
        self.db.execute('BEGIN IMMEDIATE')
        try:
            baseline = self.db.execute("SELECT value FROM metadata WHERE key='project'").fetchone()
            if baseline and baseline['value'] != project:
                raise ValueError('Project changed; use a separate queue database')
            seen = set()
            for item in items:
                ident = item['id']; seen.add(ident)
                ready = item['status'].casefold() == ready_name.casefold() and item['open']
                previous = self.db.execute('SELECT * FROM observations WHERE item=?',(ident,)).fetchone()
                generation = previous['generation'] if previous else 0
                entered = ready and (not previous or not previous['ready'])
                if entered:
                    generation += 1
                self.db.execute('INSERT OR REPLACE INTO observations VALUES(?,?,?,?)',
                                (ident,item['number'],int(ready),generation))
                if entered and baseline:
                    key='eniveil-ready-'+hashlib.sha256(f'{project}:{ident}:{generation}'.encode()).hexdigest()
                    self.db.execute('INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?,?,NULL,NULL,NULL,?)',
                                    (key,ident,item['number'],generation,item['url'],'queued',time.time()))
                if not ready:
                    self.db.execute("UPDATE jobs SET state='cancelled' WHERE item=? AND state='queued'",(ident,))
            for row in self.db.execute('SELECT item FROM observations').fetchall():
                if row['item'] not in seen:
                    self.db.execute('UPDATE observations SET ready=0 WHERE item=?',(row['item'],))
                    self.db.execute("UPDATE jobs SET state='cancelled' WHERE item=? AND state='queued'",(row['item'],))
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('project',?)",(project,))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK'); raise

    def reserve(self, workers, daemons, busy):
        eligible=[]
        for worker in workers:
            daemon=daemons.get(worker['daemon'])
            if (worker.get('enabled',True) and worker['provider'] in ('codex','claude') and daemon
                and daemon['status']=='active' and daemon['presence']=='connected'
                and 'hub.execute' in daemon['scopes'] and busy.get(worker['daemon'],0)==0):
                eligible.append(worker)
        return self.reserve_available(eligible)

    def reserve_available(self, workers):
        """Claim one issue and configured execution slot, independent of launch backend."""
        self.db.execute('BEGIN IMMEDIATE')
        try:
            active=self.db.execute("SELECT * FROM jobs WHERE state IN ('dispatching','running','uncertain')").fetchall()
            occupied={r['daemon'] for r in active};issues={r['issue'] for r in active}
            eligible=[]
            for worker in workers:
                resource=worker.get('resource',worker.get('daemon'))
                if not resource or not worker.get('enabled',True) or resource in occupied:continue
                last=self.db.execute('SELECT max(created) FROM jobs WHERE worker=?',(worker['name'],)).fetchone()[0] or 0
                eligible.append((worker.get('priority',100),last,worker['name'],worker,resource))
            chosen=min(eligible,key=lambda x:x[:3]) if eligible else None
            job=next((r for r in self.db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created,delivery")
                      if r['issue'] not in issues),None)
            if not chosen or not job:self.db.execute('COMMIT');return None
            self.db.execute("UPDATE jobs SET state='dispatching',worker=?,daemon=? WHERE delivery=?",
                            (chosen[3]['name'],chosen[4],job['delivery']))
            self.db.execute('COMMIT');return dict(job),chosen[3]
        except BaseException:
            self.db.execute('ROLLBACK');raise

    def completion_token(self, delivery):
        token=secrets.token_urlsafe(32)
        self.db.execute('INSERT INTO completions(delivery,token_hash,expires) VALUES(?,?,?)',
                        (delivery,hashlib.sha256(token.encode()).hexdigest(),time.time()+10800))
        return token

    def complete(self, delivery, token, state, pr_url=None):
        if state not in ('succeeded','failed'):raise ValueError('Invalid completion state')
        if (state=='succeeded' or pr_url is not None) and not re.fullmatch(r'https://github\.com/MindulMendul/eniveil/pull/[1-9][0-9]*',pr_url or ''):
            raise ValueError('Success requires an eniveil development PR URL')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row=self.db.execute('SELECT c.*,j.state FROM completions c JOIN jobs j ON j.delivery=c.delivery WHERE c.delivery=?',(delivery,)).fetchone()
            if (not row or row['expires']<time.time() or not hmac.compare_digest(row['token_hash'],hashlib.sha256(token.encode()).hexdigest())):
                self.db.execute('ROLLBACK');return False
            if row['state'] in ('succeeded','failed'):
                existing=self.db.execute('SELECT pr_url FROM completions WHERE delivery=?',(delivery,)).fetchone()[0]
                self.db.execute('ROLLBACK');return row['state']==state and existing==pr_url
            if row['state'] not in ACTIVE:self.db.execute('ROLLBACK');return False
            self.db.execute('UPDATE jobs SET state=? WHERE delivery=?',(state,delivery))
            self.db.execute('UPDATE completions SET pr_url=? WHERE delivery=?',(pr_url,delivery))
            self.db.execute('COMMIT');return True
        except BaseException:
            self.db.execute('ROLLBACK');raise

    def cloud_ticket(self, delivery, provider, ticket):
        self.db.execute('INSERT INTO cloud_runs VALUES(?,?,?,?)',(delivery,provider,ticket['id'],ticket['url']))
        self.record(delivery,'running',ticket['id'])

    def record(self, delivery, state, run_id=None):
        if state not in ('running','uncertain','succeeded','failed','timed_out','rejected'):
            raise ValueError('Invalid execution state')
        self.db.execute("UPDATE jobs SET state=?,run_id=COALESCE(?,run_id) WHERE delivery=? AND state IN ('dispatching','running','uncertain')",(state,run_id,delivery))
        if run_id:self.db.execute('UPDATE jobs SET run_id=? WHERE delivery=?',(run_id,delivery))

    def reconcile(self, runs):
        for run in runs:
            if run['delivery']:
                self.db.execute("UPDATE jobs SET state=?,run_id=? WHERE delivery=? AND state IN ('dispatching','running','uncertain')",
                                (run['status'],run['id'],run['delivery']))

    def counts(self):
        return dict(self.db.execute('SELECT state,count(*) FROM jobs GROUP BY state').fetchall())
