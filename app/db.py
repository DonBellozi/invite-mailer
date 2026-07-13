from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_uid TEXT,
    message_date TEXT,
    sender TEXT,
    subject TEXT,
    attachment_name TEXT NOT NULL,
    file_hash TEXT NOT NULL UNIQUE,
    saved_path TEXT NOT NULL,
    row_count INTEGER,
    status TEXT NOT NULL,
    error_text TEXT,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS employees (
    worker_key TEXT PRIMARY KEY,
    fio TEXT NOT NULL,
    email TEXT,
    login TEXT,
    department TEXT,
    position TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    employment_seq INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    missed_imports INTEGER NOT NULL DEFAULT 0,
    inactive_since TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_key TEXT NOT NULL,
    employment_seq INTEGER NOT NULL,
    template_id TEXT NOT NULL,
    email TEXT,
    sent_at TEXT NOT NULL,
    status TEXT NOT NULL,
    method TEXT NOT NULL,
    error_text TEXT,
    FOREIGN KEY(worker_key) REFERENCES employees(worker_key)
);

CREATE INDEX IF NOT EXISTS idx_notification_lookup
ON notification_history(worker_key, employment_seq, template_id, status, sent_at);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    details TEXT
);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def get_state(self, key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_state WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO app_state(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
