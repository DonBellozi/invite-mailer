from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .db import Database
from .settings import Settings


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_test_definitions(db: Database, legacy_templates: list[dict[str, Any]]) -> None:
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
        now = _now()
        for item in legacy_templates:
            audience = item.get("audience") or {}
            audience_type = "explicit_list" if str(audience.get("type", "")).strip().lower() == "explicit_list" else "all"
            departments = item.get("departments") or {}
            indigo = item.get("indigo") or {}
            connection.execute(
                """
                INSERT OR IGNORE INTO test_definitions(
                    id, enabled, name, mode, validity_days, audience_type,
                    departments_include_json, departments_exclude_json,
                    indigo_logical_test_id, indigo_test_name,
                    indigo_success_results_json, indigo_failed_prefixes_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(item["id"]), int(bool(item.get("enabled", True))),
                    str(item.get("name") or item["id"]), str(item.get("mode") or "once"),
                    item.get("validity_days"), audience_type,
                    json.dumps(departments.get("include") or ["*"], ensure_ascii=False),
                    json.dumps(departments.get("exclude") or [], ensure_ascii=False),
                    indigo.get("logical_test_id"), indigo.get("test_name"),
                    json.dumps(indigo.get("success_results") or [], ensure_ascii=False),
                    json.dumps(indigo.get("failed_result_prefixes") or [], ensure_ascii=False),
                    now, now,
                ),
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
        "id": str(row["id"]),
        "enabled": bool(row["enabled"]),
        "name": str(row["name"]),
        "mode": str(row["mode"] or "once"),
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
