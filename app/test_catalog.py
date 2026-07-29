from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .db import Database
from .settings import Settings


SEED_KEY = "test_definitions_builtin_seed_v1"
OLD_YAML_MIGRATION_KEY = "test_definitions_yaml_import_v1"

DEFAULT_TEST_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "antiterror",
        "enabled": True,
        "name": "Антитеррор",
        "mode": "once",
        "validity_days": None,
        "audience_type": "all",
        "departments_include": ["*"],
        "departments_exclude": [],
        "indigo_logical_test_id": 1715,
        "indigo_test_name": "Антитеррор",
        "indigo_success_results": ["Отлично", "Хорошо"],
        "indigo_failed_prefixes": ["Требуется повторный"],
    },
    {
        "id": "anticorruption",
        "enabled": True,
        "name": "Антикоррупция",
        "mode": "once",
        "validity_days": None,
        "audience_type": "explicit_list",
        "departments_include": ["*"],
        "departments_exclude": [],
        "indigo_logical_test_id": 1719,
        "indigo_test_name": "Антикоррупция",
        "indigo_success_results": ["Отлично", "Хорошо"],
        "indigo_failed_prefixes": ["Требуется повторный"],
    },
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json(values: Any, default: list[str]) -> str:
    source = values if isinstance(values, list) else default
    return json.dumps(source, ensure_ascii=False)


def ensure_test_definitions(
    db: Database,
    _legacy_templates: list[dict[str, Any]] | None = None,
) -> None:
    """Создает таблицу тестов и заполняет ее только на чистой установке.

    Второй аргумент сохранен для совместимости со старыми вызовами функции.
    Существующие тесты и изменения из Web-интерфейса не перезаписываются.
    """
    with db.connect() as connection:
        connection.execute(
            """
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
            )
            """
        )

        completed = connection.execute(
            "SELECT value FROM app_state WHERE key = ?",
            (SEED_KEY,),
        ).fetchone()
        if completed:
            return

        old_yaml_migration = connection.execute(
            "SELECT value FROM app_state WHERE key = ?",
            (OLD_YAML_MIGRATION_KEY,),
        ).fetchone()
        count_row = connection.execute(
            "SELECT COUNT(*) AS count FROM test_definitions"
        ).fetchone()
        table_is_empty = not count_row or int(count_row["count"]) == 0

        # При чистой установке добавляем два исходных теста.
        # На обновленной установке данные уже находятся в SQLite.
        # Если пользователь ранее удалил все тесты, старый ключ миграции
        # не позволит восстановить их автоматически.
        should_seed = table_is_empty and old_yaml_migration is None

        now = _now()
        inserted = 0

        if should_seed:
            for item in DEFAULT_TEST_DEFINITIONS:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO test_definitions (
                        id, enabled, name, mode, validity_days, audience_type,
                        departments_include_json, departments_exclude_json,
                        indigo_logical_test_id, indigo_test_name,
                        indigo_success_results_json, indigo_failed_prefixes_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["id"],
                        int(bool(item["enabled"])),
                        item["name"],
                        item["mode"],
                        item["validity_days"],
                        item["audience_type"],
                        _json(item["departments_include"], ["*"]),
                        _json(item["departments_exclude"], []),
                        item["indigo_logical_test_id"],
                        item["indigo_test_name"],
                        _json(item["indigo_success_results"], []),
                        _json(item["indigo_failed_prefixes"], []),
                        now,
                        now,
                    ),
                )
                inserted += cursor.rowcount

        connection.execute(
            """
            INSERT INTO app_state(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (
                SEED_KEY,
                json.dumps(
                    {
                        "at": now,
                        "inserted": inserted,
                        "table_was_empty": table_is_empty,
                        "old_yaml_migration_found": old_yaml_migration is not None,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        connection.commit()


def _json_list(value: str | None, default: list[str]) -> list[str]:
    try:
        parsed = json.loads(value or "")
        if not isinstance(parsed, list):
            return default
        return [str(item).strip() for item in parsed if str(item).strip()]
    except Exception:
        return default


def row_to_template(row: Any) -> dict[str, Any]:
    template: dict[str, Any] = {
        "id": str(row["id"]),
        "enabled": bool(row["enabled"]),
        "name": str(row["name"]),
        "mode": str(row["mode"] or "once"),
        "validity_days": row["validity_days"],
    }

    audience_type = str(row["audience_type"] or "all")
    if audience_type == "explicit_list":
        template["audience"] = {"type": "explicit_list"}
    else:
        template["departments"] = {
            "include": _json_list(row["departments_include_json"], ["*"]),
            "exclude": _json_list(row["departments_exclude_json"], []),
        }

    if row["indigo_logical_test_id"] or row["indigo_test_name"]:
        template["indigo"] = {
            "logical_test_id": row["indigo_logical_test_id"],
            "test_name": str(row["indigo_test_name"] or row["name"]),
            "success_results": _json_list(
                row["indigo_success_results_json"], []
            ),
            "failed_result_prefixes": _json_list(
                row["indigo_failed_prefixes_json"], []
            ),
        }

    return template


def load_test_definitions(settings: Settings, db: Database) -> list[dict[str, Any]]:
    # settings.templates передается только для совместимости со старым кодом.
    ensure_test_definitions(db, settings.templates)
    with db.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM test_definitions ORDER BY name COLLATE NOCASE, id"
        ).fetchall()
    return [row_to_template(row) for row in rows]
