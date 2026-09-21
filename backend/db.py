"""SQLite storage. Every operation owns its connection; writes are transactional."""
import contextlib
import json
import sqlite3
import time
import uuid
from pathlib import Path


def uid(prefix):
    return prefix + "_" + uuid.uuid4().hex[:16]


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES(1);
CREATE TABLE IF NOT EXISTS users(
 id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
 password TEXT NOT NULL, role TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
 token TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id) ON DELETE CASCADE, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS projects(
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
 question TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL DEFAULT 0,
 created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sources(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 title TEXT NOT NULL, url TEXT NOT NULL DEFAULT '', content TEXT NOT NULL,
 kind TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', fingerprint TEXT NOT NULL,
 created REAL NOT NULL, UNIQUE(project_id, fingerprint));
CREATE TABLE IF NOT EXISTS datasets(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 name TEXT NOT NULL, content TEXT NOT NULL, metadata TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 goal TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'planner',
 config TEXT NOT NULL, state TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
 cancel_requested INTEGER NOT NULL DEFAULT 0, calls INTEGER NOT NULL DEFAULT 0,
 input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
 created REAL NOT NULL, updated REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(project_id)
 WHERE status IN ('queued','running','awaiting_approval');
CREATE TABLE IF NOT EXISTS run_sources(
 run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
 source_id TEXT NOT NULL, snapshot TEXT NOT NULL, PRIMARY KEY(run_id,source_id));
CREATE TABLE IF NOT EXISTS steps(
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
 role TEXT NOT NULL, iteration INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
 output TEXT NOT NULL DEFAULT '{}', started REAL NOT NULL, finished REAL,
 UNIQUE(run_id,role,iteration));
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, role TEXT NOT NULL DEFAULT '', message TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}',
 created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id,id);
CREATE TABLE IF NOT EXISTS approvals(
 id TEXT PRIMARY KEY, run_id TEXT UNIQUE NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
 status TEXT NOT NULL, request TEXT NOT NULL, decision TEXT NOT NULL DEFAULT '',
 decided_by TEXT REFERENCES users(id), created REAL NOT NULL, decided REAL);
CREATE TABLE IF NOT EXISTS artifacts(
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
 name TEXT NOT NULL, mime TEXT NOT NULL, content TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(run_id,name));
CREATE TABLE IF NOT EXISTS messages(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 role TEXT NOT NULL, content TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS schedules(
 id TEXT PRIMARY KEY, project_id TEXT UNIQUE NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 mode TEXT NOT NULL, interval_hours INTEGER NOT NULL, next_at REAL NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class DB:
    def __init__(self, path):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextlib.contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def all(self, sql, args=()):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, args)]

    def one(self, sql, args=()):
        rows = self.all(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        with self.connect() as conn:
            return conn.execute(sql, args).rowcount

    def event(self, run_id, kind, role, message, data=None):
        self.execute("INSERT INTO events(run_id,kind,role,message,data,created) VALUES(?,?,?,?,?,?)",
                     (run_id, kind, role, message, dumps(data or {}), time.time()))

    def artifact(self, run_id, name, mime, content):
        self.execute("""INSERT INTO artifacts VALUES(?,?,?,?,?,?) ON CONFLICT(run_id,name)
                     DO UPDATE SET content=excluded.content,created=excluded.created""",
                     (uid('art'), run_id, name, mime, content, time.time()))

    def sources(self, run_id):
        return [json.loads(r['snapshot']) for r in self.all(
            "SELECT snapshot FROM run_sources WHERE run_id=? ORDER BY source_id", (run_id,))]

    def snapshot_source(self, run_id, source):
        self.execute("INSERT OR IGNORE INTO run_sources VALUES(?,?,?)",
                     (run_id, source['id'], dumps(source)))

    def setting(self, key, default):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row['value']) if row else default

    def backup(self, target):
        with self.connect() as conn, sqlite3.connect(target) as dest:
            conn.backup(dest)
