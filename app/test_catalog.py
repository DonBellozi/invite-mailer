from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .db import Database
from .settings import Settings

MIGRATION_KEY = "test_definitions_yaml_import_v1"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json(values: Any, default: list[str]) -> str:
    return json.dumps(values if isinstance(values, list) and values else default, ensure_ascii=False)


def ensure_test_definitions(db: Database, legacy_templates: list[dict[str, Any]]) -> None:
    """Однократно переносит определения тестов из YAML в SQLite.

    Повторные запуски не перезаписывают данные, измененные через Web.
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
        done = connection.execute("SELECT value FROM app_state WHERE key=?", (MIGRATION_KEY,)).fetchone()
        if done:
            return
        now = _now()
        imported = 0
        for item in legacy_templates:
            if not item.get("id"):
                continue
            audience = item.get("audience") or {}
            audience_type = "explicit_list" if str(audience.get("type", "")).strip().lower() == "explicit_list" else "all"
            departments = item.get("departments") or {}
            indigo = item.get("indigo") or {}
            cur = connection.execute(
                """
                INSERT INTO test_definitions(
                    id, enabled, name, mode, validity_days, audience_type,
                    departments_include_json, departments_exclude_json,
                    indigo_logical_test_id, indigo_test_name,
                    indigo_success_results_json, indigo_failed_prefixes_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    enabled=excluded.enabled, name=excluded.name, mode=excluded.mode,
                    validity_days=excluded.validity_days, audience_type=excluded.audience_type,
                    departments_include_json=excluded.departments_include_json,
                    departments_exclude_json=excluded.departments_exclude_json,
                    indigo_logical_test_id=excluded.indigo_logical_test_id,
                    indigo_test_name=excluded.indigo_test_name,
                    indigo_success_results_json=excluded.indigo_success_results_json,
                    indigo_failed_prefixes_json=excluded.indigo_failed_prefixes_json,
                    updated_at=excluded.updated_at
                """,
                (
                    str(item["id"]), int(bool(item.get("enabled", True))),
                    str(item.get("name") or item["id"]), str(item.get("mode") or "once"),
                    item.get("validity_days"), audience_type,
                    _json(departments.get("include"), ["*"]),
                    _json(departments.get("exclude"), []),
                    indigo.get("logical_test_id"), indigo.get("test_name"),
                    _json(indigo.get("success_results"), []),
                    _json(indigo.get("failed_result_prefixes"), []),
                    now, now,
                ),
            )
            imported += cur.rowcount
        connection.execute(
            "INSERT INTO app_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (MIGRATION_KEY, json.dumps({"at": now, "imported": imported}, ensure_ascii=False)),
        )
        connection.commit()


def _json_list(value: str | None, default: list[str]) -> list[str]:
    try:
        parsed = json.loads(value or "")
        return [str(x).strip() for x in parsed if str(x).strip()] if isinstance(parsed, list) else default
    except Exception:
        return default


def row_to_template(row: Any, legacy: dict[str, Any] | None = None) -> dict[str, Any]:
    legacy = legacy or {}
    template: dict[str, Any] = {
        "id": str(row["id"]), "enabled": bool(row["enabled"]),
        "name": str(row["name"]), "mode": str(row["mode"] or "once"),
        "validity_days": row["validity_days"],
    }
    for key in ("subject", "body_template", "reminder_subject", "reminder_body_template"):
        if legacy.get(key) is not None:
            template[key] = legacy[key]
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
            "success_results": _json_list(row["indigo_success_results_json"], []),
            "failed_result_prefixes": _json_list(row["indigo_failed_prefixes_json"], []),
        }
    return template


def load_test_definitions(settings: Settings, db: Database) -> list[dict[str, Any]]:
    ensure_test_definitions(db, settings.templates)
    with db.connect() as connection:
        rows = connection.execute("SELECT * FROM test_definitions ORDER BY name COLLATE NOCASE, id").fetchall()
    legacy_by_id = {str(item.get("id")): item for item in settings.templates if item.get("id")}
    return [row_to_template(row, legacy_by_id.get(str(row["id"]))) for row in rows]
