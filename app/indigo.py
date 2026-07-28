from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .db import Database
from .settings import Settings
from .test_catalog import load_test_definitions


LOGGER = logging.getLogger("invite-mailer.indigo")
VIEW_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class ResultSummary:
    status: str | None
    completed_at: str | None
    grade: str | None
    percent: float | None
    attempts: int
    successful_attempts: int
    failed_attempts: int
    next_due_at: str | None


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat()
    return str(value)


def configured_indigo_templates(settings: Settings) -> list[dict]:
    result: list[dict] = []
    for template in settings.templates:
        indigo = template.get("indigo") or {}
        if indigo.get("logical_test_id") and indigo.get("test_name"):
            result.append(template)
    return result


def sync_indigo_results(settings: Settings, db: Database) -> int:
    settings.templates[:] = load_test_definitions(settings, db)
    if not settings.indigo.enabled:
        return 0

    templates = configured_indigo_templates(settings)
    if not templates:
        return 0

    if not VIEW_RE.match(settings.indigo.view):
        raise RuntimeError("Некорректное имя представления Indigo")

    started_at = _now_iso()
    with db.connect() as local:
        run_id = local.execute(
            "INSERT INTO indigo_sync_runs(started_at, status) VALUES (?, 'running')",
            (started_at,),
        ).lastrowid

    try:
        import psycopg2

        conditions: list[str] = []
        params: list[Any] = []
        for template in templates:
            indigo = template["indigo"]
            logical_id = int(indigo["logical_test_id"])
            test_name = str(indigo["test_name"])
            conditions.append(
                "(COALESCE(ph_test_id, test_id) = %s AND test_name = %s)"
            )
            params.extend([logical_id, test_name])

        query = f"""
            SELECT
                id,
                lower(trim(login)) AS login,
                test_id,
                ph_test_id,
                COALESCE(ph_test_id, test_id) AS logical_test_id,
                test_name,
                status,
                time_start,
                time_end,
                percent,
                result,
                archived
            FROM {settings.indigo.view}
            WHERE login IS NOT NULL
              AND trim(login) <> ''
              AND ({' OR '.join(conditions)})
            ORDER BY id
        """

        with psycopg2.connect(
            host=settings.indigo.host,
            port=settings.indigo.port,
            dbname=settings.indigo.database,
            user=settings.indigo.username,
            password=settings.indigo.password,
            sslmode=settings.indigo.sslmode,
            connect_timeout=settings.indigo.connect_timeout,
            application_name="invite-mailer-readonly",
        ) as remote:
            remote.set_session(readonly=True, autocommit=True)
            with remote.cursor() as cursor:
                cursor.execute(query, params)
                rows = cursor.fetchall()

        synced_at = _now_iso()
        configured_pairs = {
            (
                int(template["indigo"]["logical_test_id"]),
                str(template["indigo"]["test_name"]),
            )
            for template in templates
        }

        with db.connect() as local:
            for logical_id, test_name in configured_pairs:
                local.execute(
                    "DELETE FROM indigo_attempts WHERE logical_test_id = ? AND test_name = ?",
                    (logical_id, test_name),
                )

            local.executemany(
                """
                INSERT INTO indigo_attempts(
                    result_id, login, logical_test_id, source_test_id, ph_test_id,
                    test_name, source_status, time_start, time_end, percent,
                    source_result, archived, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        int(row[0]),
                        str(row[1]).strip().lower(),
                        int(row[4]),
                        int(row[2]),
                        int(row[3]) if row[3] is not None else None,
                        str(row[5]),
                        int(row[6]) if row[6] is not None else None,
                        _iso(row[7]),
                        _iso(row[8]),
                        float(row[9]) if row[9] is not None else None,
                        str(row[10]).strip() if row[10] is not None else None,
                        1 if row[11] else 0,
                        synced_at,
                    )
                    for row in rows
                ],
            )
            local.execute(
                """
                UPDATE indigo_sync_runs
                SET finished_at = ?, status = 'success', rows_loaded = ?
                WHERE id = ?
                """,
                (synced_at, len(rows), run_id),
            )
            local.execute(
                """
                INSERT INTO app_state(key, value) VALUES('indigo_last_sync', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (synced_at,),
            )
            local.execute("DELETE FROM app_state WHERE key = 'indigo_last_error'")

        LOGGER.info("Из Indigo загружено результатов: %s", len(rows))
        return len(rows)
    except Exception as error:
        finished_at = _now_iso()
        with db.connect() as local:
            local.execute(
                """
                UPDATE indigo_sync_runs
                SET finished_at = ?, status = 'error', error_text = ?
                WHERE id = ?
                """,
                (finished_at, str(error), run_id),
            )
            local.execute(
                """
                INSERT INTO app_state(key, value) VALUES('indigo_last_error', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(error),),
            )
        raise


def try_sync_indigo_results(settings: Settings, db: Database) -> bool:
    if not settings.indigo.enabled:
        return False
    try:
        sync_indigo_results(settings, db)
        return True
    except Exception:
        LOGGER.exception(
            "Не удалось обновить результаты Indigo. Используется последний локальный кеш."
        )
        return False


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _normalized_values(items: list[Any]) -> set[str]:
    return {str(item).strip().casefold() for item in items if str(item).strip()}


def _classify_result(result: str | None, indigo_config: dict) -> str | None:
    if not result:
        return None
    normalized = result.strip().casefold()
    success = _normalized_values(
        indigo_config.get("success_results", ["Отлично", "Хорошо"])
    )
    failed = _normalized_values(indigo_config.get("failed_results", []))
    failed_prefixes = [
        str(item).strip().casefold()
        for item in indigo_config.get(
            "failed_result_prefixes", ["Требуется повторный"]
        )
        if str(item).strip()
    ]

    if normalized in success:
        return "completed"
    if normalized in failed or any(normalized.startswith(prefix) for prefix in failed_prefixes):
        return "failed"
    return None


def summarize_employee_result(
    connection,
    employee,
    template: dict,
    now: datetime | None = None,
) -> ResultSummary:
    indigo_config = template.get("indigo") or {}
    logical_test_id = indigo_config.get("logical_test_id")
    login = str(employee["login"] or "").strip().lower()
    if not logical_test_id or not login:
        return ResultSummary(None, None, None, None, 0, 0, 0, None)

    params: list[Any] = [login, int(logical_test_id)]
    lower_bound_sql = ""
    # Для повторного трудоустройства старые результаты прежнего периода не учитываются.
    if int(employee["employment_seq"] or 1) > 1 and employee["employment_started_at"]:
        lower_bound_sql = " AND time_end >= ?"
        params.append(employee["employment_started_at"])

    rows = connection.execute(
        f"""
        SELECT *
        FROM indigo_attempts
        WHERE login = ?
          AND logical_test_id = ?
          AND time_end IS NOT NULL
          AND source_result IS NOT NULL
          AND percent IS NOT NULL
          {lower_bound_sql}
        ORDER BY time_end, result_id
        """,
        params,
    ).fetchall()

    completed = [
        row
        for row in rows
        if _classify_result(row["source_result"], indigo_config) == "completed"
    ]
    failed = [
        row
        for row in rows
        if _classify_result(row["source_result"], indigo_config) == "failed"
    ]

    current_time = now or datetime.now()
    latest_success = completed[-1] if completed else None
    latest_success_at = _parse_datetime(latest_success["time_end"]) if latest_success else None

    next_due_at: datetime | None = None
    result_is_current = bool(latest_success)
    if latest_success and template.get("mode", "once") == "periodic":
        validity_days = int(template.get("validity_days") or 0)
        if validity_days <= 0:
            raise ValueError(
                f"У периодического шаблона {template['id']} не задан validity_days"
            )
        next_due_at = latest_success_at + timedelta(days=validity_days) if latest_success_at else None
        result_is_current = bool(next_due_at and current_time < next_due_at)

    if result_is_current and latest_success:
        # Дата прохождения всегда берется по последней успешной попытке.
        # Оценка и процент остаются лучшими среди успешных попыток текущей занятости.
        best = max(
            completed,
            key=lambda row: (
                float(row["percent"]),
                row["time_end"] or "",
                int(row["result_id"]),
            ),
        )
        return ResultSummary(
            status="completed",
            completed_at=latest_success["time_end"],
            grade=best["source_result"],
            percent=float(best["percent"]),
            attempts=len(rows),
            successful_attempts=len(completed),
            failed_attempts=len(failed),
            next_due_at=next_due_at.replace(microsecond=0).isoformat()
            if next_due_at
            else None,
        )

    # Для истекшего периодического результата учитываем только попытки нового цикла.
    relevant_failed = failed
    if next_due_at:
        relevant_failed = [
            row
            for row in failed
            if (_parse_datetime(row["time_end"]) or datetime.min) >= next_due_at
        ]

    if relevant_failed:
        best_failed = max(
            relevant_failed,
            key=lambda row: (
                float(row["percent"]),
                row["time_end"] or "",
                int(row["result_id"]),
            ),
        )
        return ResultSummary(
            status="failed",
            completed_at=None,
            grade="Не прошел",
            percent=float(best_failed["percent"]),
            attempts=len(rows),
            successful_attempts=len(completed),
            failed_attempts=len(failed),
            next_due_at=next_due_at.replace(microsecond=0).isoformat()
            if next_due_at
            else None,
        )

    return ResultSummary(
        status=None,
        completed_at=None,
        grade=None,
        percent=None,
        attempts=len(rows),
        successful_attempts=len(completed),
        failed_attempts=len(failed),
        next_due_at=next_due_at.replace(microsecond=0).isoformat()
        if next_due_at
        else None,
    )
