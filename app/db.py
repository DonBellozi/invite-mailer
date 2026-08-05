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
    employment_started_at TEXT,
    last_seen_at TEXT NOT NULL,
    missed_imports INTEGER NOT NULL DEFAULT 0,
    inactive_since TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_employees_email
ON employees(email, active);

CREATE INDEX IF NOT EXISTS idx_employees_login
ON employees(login, active);

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

CREATE TABLE IF NOT EXISTS audience_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    saved_path TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    total_rows INTEGER NOT NULL DEFAULT 0,
    ready_rows INTEGER NOT NULL DEFAULT 0,
    warning_rows INTEGER NOT NULL DEFAULT 0,
    error_rows INTEGER NOT NULL DEFAULT 0,
    confirmed_rows INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    uploaded_by TEXT,
    metadata_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_audience_imports_template
ON audience_imports(template_id, created_at);

CREATE TABLE IF NOT EXISTS audience_import_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id INTEGER NOT NULL,
    row_number INTEGER NOT NULL,
    source_email TEXT,
    normalized_email TEXT,
    status TEXT NOT NULL,
    error_text TEXT,
    worker_key TEXT,
    employment_seq INTEGER,
    employee_fio TEXT,
    employee_email TEXT,
    employee_login TEXT,
    employee_department TEXT,
    employee_position TEXT,
    selected INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(import_id) REFERENCES audience_imports(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_audience_import_rows_import
ON audience_import_rows(import_id, status, row_number);

CREATE TABLE IF NOT EXISTS test_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id TEXT NOT NULL,
    worker_key TEXT NOT NULL,
    employment_seq INTEGER NOT NULL,
    source_import_id INTEGER,
    source_row_id INTEGER,
    assigned_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(worker_key) REFERENCES employees(worker_key),
    FOREIGN KEY(source_import_id) REFERENCES audience_imports(id),
    FOREIGN KEY(source_row_id) REFERENCES audience_import_rows(id),
    UNIQUE(template_id, worker_key, employment_seq)
);

CREATE INDEX IF NOT EXISTS idx_test_assignments_lookup
ON test_assignments(template_id, active, worker_key, employment_seq);

CREATE TABLE IF NOT EXISTS login_overrides (
    email TEXT PRIMARY KEY,
    login TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'admin',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_login_overrides_login
ON login_overrides(login);


CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('employee', 'position')),
    fio TEXT,
    position TEXT NOT NULL,
    normalized_fio TEXT NOT NULL DEFAULT '',
    normalized_position TEXT NOT NULL,
    template_id TEXT NOT NULL DEFAULT '*',
    reason TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(kind, normalized_fio, normalized_position, template_id)
);

CREATE INDEX IF NOT EXISTS idx_exclusions_lookup
ON exclusions(enabled, template_id, kind, normalized_fio, normalized_position);

CREATE TABLE IF NOT EXISTS app_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    login TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    can_view_report INTEGER NOT NULL DEFAULT 1,
    can_import INTEGER NOT NULL DEFAULT 0,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviewers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    enabled INTEGER NOT NULL DEFAULT 1,
    receives_technical_errors INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviewer_templates (
    reviewer_id INTEGER NOT NULL,
    template_id TEXT NOT NULL,
    PRIMARY KEY(reviewer_id, template_id),
    FOREIGN KEY(reviewer_id) REFERENCES reviewers(id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS test_definitions (
    id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    name TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'once',
    validity_days INTEGER,
    audience_type TEXT NOT NULL DEFAULT 'all',
    departments_include_json TEXT NOT NULL DEFAULT '["*"]',
    departments_exclude_json TEXT NOT NULL DEFAULT '[]',
    indigo_logical_test_id INTEGER,
    indigo_test_name TEXT,
    indigo_success_results_json TEXT NOT NULL DEFAULT '[]',
    indigo_failed_prefixes_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    worker_key TEXT,
    employment_seq INTEGER,
    fio TEXT,
    email TEXT,
    department TEXT,
    position TEXT,
    template_id TEXT,
    template_name TEXT,
    reminder_number INTEGER,
    recipient TEXT,
    status TEXT NOT NULL,
    details TEXT
);

CREATE INDEX IF NOT EXISTS idx_notification_journal_created
ON notification_journal(created_at);

CREATE INDEX IF NOT EXISTS idx_notification_journal_lookup
ON notification_journal(template_id, worker_key, employment_seq, event_type);

CREATE TABLE IF NOT EXISTS reviewer_notification_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_key TEXT NOT NULL,
    employment_seq INTEGER NOT NULL,
    template_id TEXT NOT NULL,
    fio TEXT,
    email TEXT,
    department TEXT,
    position TEXT,
    reminder_count INTEGER NOT NULL,
    first_reminder_at TEXT,
    last_reminder_at TEXT,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    delivered_at TEXT,
    last_error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    UNIQUE(worker_key, employment_seq, template_id),
    FOREIGN KEY(worker_key) REFERENCES employees(worker_key)
);

CREATE INDEX IF NOT EXISTS idx_reviewer_queue_status
ON reviewer_notification_queue(status, template_id, created_at);

CREATE TABLE IF NOT EXISTS reviewer_delivery_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id INTEGER NOT NULL,
    reviewer_id INTEGER NOT NULL,
    recipient_email TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    status TEXT NOT NULL,
    error_text TEXT,
    FOREIGN KEY(queue_id) REFERENCES reviewer_notification_queue(id) ON DELETE CASCADE,
    FOREIGN KEY(reviewer_id) REFERENCES reviewers(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_reviewer_delivery_queue
ON reviewer_delivery_attempts(queue_id, reviewer_id, status);


CREATE TABLE IF NOT EXISTS mail_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    template_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    body_html TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    UNIQUE(kind, template_id)
);

CREATE TABLE IF NOT EXISTS technical_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    fio TEXT,
    email TEXT,
    error_type TEXT NOT NULL,
    error_text TEXT,
    detected_at TEXT NOT NULL,
    notified_at TEXT,
    notification_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_technical_errors_fingerprint
ON technical_errors(fingerprint, detected_at);

CREATE TABLE IF NOT EXISTS indigo_attempts (
    result_id INTEGER PRIMARY KEY,
    login TEXT NOT NULL,
    logical_test_id INTEGER NOT NULL,
    source_test_id INTEGER NOT NULL,
    ph_test_id INTEGER,
    test_name TEXT NOT NULL,
    source_status INTEGER,
    time_start TEXT,
    time_end TEXT,
    percent REAL,
    source_result TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_indigo_attempt_lookup
ON indigo_attempts(logical_test_id, login, time_end);

CREATE TABLE IF NOT EXISTS indigo_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    rows_loaded INTEGER NOT NULL DEFAULT 0,
    error_text TEXT
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate(connection)

    @staticmethod
    def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
        return {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def _migrate(self, connection: sqlite3.Connection) -> None:
        reviewer_columns = self._column_names(connection, "reviewers")
        if "receives_technical_errors" not in reviewer_columns:
            connection.execute(
                "ALTER TABLE reviewers ADD COLUMN receives_technical_errors INTEGER NOT NULL DEFAULT 0"
            )

        employee_columns = self._column_names(connection, "employees")
        if "employment_started_at" not in employee_columns:
            connection.execute("ALTER TABLE employees ADD COLUMN employment_started_at TEXT")

        connection.execute(
            """
            UPDATE employees
            SET employment_started_at = first_seen_at
            WHERE employment_started_at IS NULL OR employment_started_at = ''
            """
        )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
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
