"""SQLite storage. One file, WAL mode, FTS5 for memory search. No server to run."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS agents (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT '',
        soul TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        tools TEXT NOT NULL DEFAULT '[]',
        autonomy TEXT NOT NULL DEFAULT 'ask',
        shell_enabled INTEGER NOT NULL DEFAULT 0,
        emoji TEXT NOT NULL DEFAULT '🙂',
        is_foreman INTEGER NOT NULL DEFAULT 0,
        daily_token_budget INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        run_id INTEGER,
        role TEXT NOT NULL,
        content TEXT NOT NULL DEFAULT '',
        meta TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS messages_agent ON messages(agent_id, id)",
    """CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        origin TEXT NOT NULL,
        status TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT '{}',
        tainted INTEGER NOT NULL DEFAULT 0,
        taint_reason TEXT NOT NULL DEFAULT '',
        hop INTEGER NOT NULL DEFAULT 0,
        parent_run_id INTEGER,
        routine_id INTEGER,
        steps INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS approvals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        agent_id TEXT NOT NULL,
        tool TEXT NOT NULL,
        args TEXT NOT NULL,
        call_id TEXT NOT NULL,
        effect TEXT NOT NULL,
        target TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL,
        why TEXT NOT NULL DEFAULT '',
        rule_eligible INTEGER NOT NULL DEFAULT 0,
        provenance TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        note TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        resolved_at REAL
    )""",
    """CREATE TABLE IF NOT EXISTS questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        agent_id TEXT NOT NULL,
        call_id TEXT NOT NULL,
        question TEXT NOT NULL,
        options TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'pending',
        answer TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        tool TEXT NOT NULL,
        target TEXT NOT NULL,
        expires_at REAL,
        revoked_at REAL,
        created_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS routines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        name TEXT NOT NULL,
        cron TEXT NOT NULL DEFAULT '',
        instructions TEXT NOT NULL,
        mode TEXT NOT NULL DEFAULT 'act',
        webhook_secret TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        running INTEGER NOT NULL DEFAULT 0,
        last_run_at REAL,
        next_run_at REAL,
        last_result TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'fact',
        content TEXT NOT NULL,
        embedding TEXT,
        pinned INTEGER NOT NULL DEFAULT 0,
        access_count INTEGER NOT NULL DEFAULT 0,
        last_accessed_at REAL,
        archived INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'agent',
        created_at REAL NOT NULL
    )""",
    "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(content, content='memories', content_rowid='id')",
    """CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
        INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content); END""",
    """CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content); END""",
    """CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
        INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content); END""",
    """CREATE TABLE IF NOT EXISTS memory_journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        action TEXT NOT NULL,
        content TEXT NOT NULL,
        by TEXT NOT NULL,
        created_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        run_id INTEGER,
        tool TEXT NOT NULL,
        summary TEXT NOT NULL,
        effect TEXT NOT NULL,
        decision TEXT NOT NULL,
        ok INTEGER NOT NULL DEFAULT 1,
        ms INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS usage (
        agent_id TEXT NOT NULL,
        day TEXT NOT NULL,
        tokens INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (agent_id, day)
    )""",
    """CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
]


# (table, column, column definition): added to databases created before the column existed.
MIGRATIONS = [
    ("approvals", "provenance", "provenance TEXT NOT NULL DEFAULT ''"),
]


class DB:
    """Thin thread-safe wrapper. The app is asyncio + a few worker threads, so one lock is plenty."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            for stmt in SCHEMA:
                self.conn.execute(stmt)
            for table, col, ddl in MIGRATIONS:
                cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                if col not in cols:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def one(self, sql: str, params: tuple | list = ()) -> dict | None:
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: tuple | list = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def insert(self, table: str, row: dict[str, Any]) -> int:
        cols = ",".join(row)
        qs = ",".join("?" for _ in row)
        with self.lock:
            cur = self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", list(row.values()))
            return int(cur.lastrowid)

    def update(self, table: str, key: str, key_value: Any, changes: dict[str, Any]) -> None:
        if not changes:
            return
        sets = ",".join(f"{k}=?" for k in changes)
        with self.lock:
            self.conn.execute(f"UPDATE {table} SET {sets} WHERE {key}=?", [*changes.values(), key_value])

    # settings ---------------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


def now() -> float:
    return time.time()
