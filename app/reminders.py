from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .db import Database
from .indigo import summarize_employee_result, try_sync_indigo_results
from .mailer import send_html_email
from .mail_templates import ensure_mail_templates, render_mail_template
from .settings import Settings
from .test_catalog import load_test_definitions
from .technical_alerts import notify_technical_error

LOGGER = logging.getLogger("invite-mailer.reminders")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _setting(connection, key: str, default: str) -> str:
    row = connection.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def _template_name(template: dict) -> str:
    return str(template.get("name") or template["id"])


def _journal(connection, *, event_type: str, employee, template: dict, status: str,
             reminder_number: int | None = None, recipient: str | None = None,
             details: str | None = None) -> None:
    connection.execute(
        """
        INSERT INTO notification_journal(
            created_at, event_type, worker_key, employment_seq, fio, email,
            department, position, template_id, template_name, reminder_number,
            recipient, status, details
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now().isoformat(), event_type, employee["worker_key"], employee["employment_seq"],
            employee["fio"], employee["email"], employee["department"], employee["position"],
            str(template["id"]), _template_name(template), reminder_number, recipient, status, details,
        ),
    )


def _employees_for_template(connection, template: dict):
    audience = template.get("audience") or {}
    if audience.get("type") == "explicit_list":
        return connection.execute(
            """
            SELECT e.* FROM test_assignments a
            JOIN employees e ON e.worker_key = a.worker_key AND e.employment_seq = a.employment_seq
            WHERE a.template_id = ? AND a.active = 1 AND e.active = 1
            ORDER BY e.fio
            """,
            (str(template["id"]),),
        ).fetchall()

    # Для обычных шаблонов начальное приглашение уже является источником истины:
    # напоминания рассматривают только работников с успешной записью отправки.
    return connection.execute(
        "SELECT * FROM employees WHERE active = 1 ORDER BY fio"
    ).fetchall()


def _render_reminder(settings: Settings, db: Database, template: dict, employee, reminder_number: int) -> tuple[str, str, bool]:
    context = {
        "fio": employee["fio"], "email": employee["email"], "login": employee["login"],
        "department": employee["department"], "position": employee["position"],
        "reminder_number": reminder_number, "test_name": _template_name(template),
    }
    return render_mail_template(db, "reminder", str(template["id"]), context)


def _create_queue_item(connection, employee, template: dict, reminder_count: int,
                       first_reminder_at: str | None, last_reminder_at: str | None) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO reviewer_notification_queue(
            worker_key, employment_seq, template_id, fio, email, department, position,
            reminder_count, first_reminder_at, last_reminder_at, created_at, status,
            delivered_at, last_error, attempts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, 0)
        """,
        (
            employee["worker_key"], employee["employment_seq"], str(template["id"]),
            employee["fio"], employee["email"], employee["department"], employee["position"],
            reminder_count, first_reminder_at, last_reminder_at, utc_now().isoformat(),
        ),
    )


def _reviewer_body(row, template_name: str) -> str:
    return "".join([
        "<p>Работник не завершил обязательное тестирование после всех предусмотренных напоминаний.</p>",
        f"<p>ФИО: {html.escape(row['fio'] or 'Не указано')}<br>",
        f"E-mail: {html.escape(row['email'] or 'Не указан')}<br>",
        f"Подразделение: {html.escape(row['department'] or 'Не указано')}<br>",
        f"Должность: {html.escape(row['position'] or 'Не указана')}<br>",
        f"Тест: {html.escape(template_name)}<br>",
        f"Количество напоминаний: {int(row['reminder_count'])}<br>",
        f"Первое напоминание: {html.escape(row['first_reminder_at'] or 'Не указано')}<br>",
        f"Последнее напоминание: {html.escape(row['last_reminder_at'] or 'Не указано')}</p>",
    ])


def dispatch_pending_reviewer_notifications(settings: Settings, db: Database,
                                              template_ids: set[str] | None = None) -> dict:
    """Отправляет контролирующим сводные письма по тесту с несколькими работниками."""
    settings.templates[:] = load_test_definitions(settings, db)
    ensure_mail_templates(db, settings.templates)
    templates = {str(t["id"]): t for t in settings.templates}
    summary = {"delivered": 0, "pending": 0, "errors": 0}

    with db.connect() as connection:
        params: list[object] = []
        where = "WHERE q.status = 'pending'"
        if template_ids:
            placeholders = ",".join("?" for _ in template_ids)
            where += f" AND q.template_id IN ({placeholders})"
            params.extend(sorted(template_ids))

        queue = connection.execute(
            f"SELECT q.* FROM reviewer_notification_queue q {where} ORDER BY q.template_id, q.id",
            params,
        ).fetchall()
        if not queue:
            return summary

        rows_by_template: dict[str, list] = {}
        for row in queue:
            rows_by_template.setdefault(str(row["template_id"]), []).append(row)

        for template_id, rows in rows_by_template.items():
            template = templates.get(template_id, {"id": template_id, "name": template_id})
            reviewers = connection.execute(
                """
                SELECT r.id, r.name, r.email FROM reviewers r
                JOIN reviewer_templates rt ON rt.reviewer_id = r.id
                WHERE r.enabled = 1 AND rt.template_id = ?
                ORDER BY r.name COLLATE NOCASE
                """,
                (template_id,),
            ).fetchall()

            if not reviewers:
                summary["pending"] += len(rows)
                continue

            for reviewer in reviewers:
                unsent_rows = []
                for row in rows:
                    previous = connection.execute(
                        """SELECT 1 FROM reviewer_delivery_attempts
                           WHERE queue_id = ? AND reviewer_id = ? AND status = 'sent'
                           LIMIT 1""",
                        (row["id"], reviewer["id"]),
                    ).fetchone()
                    if not previous:
                        unsent_rows.append(row)

                if not unsent_rows:
                    continue

                employees = [
                    {
                        "fio": row["fio"] or "Не указано",
                        "email": row["email"] or "Не указан",
                        "department": row["department"] or "Не указано",
                        "position": row["position"] or "Не указана",
                        "reminder_count": int(row["reminder_count"] or 0),
                        "first_reminder_at": row["first_reminder_at"] or "Не указано",
                        "first_invited_at": row["first_reminder_at"] or "Не указано",
                        "last_reminder_at": row["last_reminder_at"] or "Не указано",
                    }
                    for row in unsent_rows
                ]
                first = employees[0]
                context = {
                    **first,
                    "test_name": _template_name(template),
                    "reviewer_name": reviewer["name"],
                    "employees": employees,
                    "employees_count": len(employees),
                    "displayed_employees_count": len(employees),
                }

                try:
                    subject, body, mail_enabled = render_mail_template(db, "reviewer", "*", context)
                    if not mail_enabled:
                        summary["pending"] += len(unsent_rows)
                        continue

                    send_html_email(settings.smtp, reviewer["email"], subject, body)
                    attempted_at = utc_now().isoformat()
                    for row in unsent_rows:
                        connection.execute(
                            """INSERT INTO reviewer_delivery_attempts(
                                   queue_id, reviewer_id, recipient_email, attempted_at, status, error_text
                               ) VALUES (?, ?, ?, ?, 'sent', NULL)""",
                            (row["id"], reviewer["id"], reviewer["email"], attempted_at),
                        )
                    summary["delivered"] += len(unsent_rows)
                except Exception as error:
                    error_text = str(error)
                    attempted_at = utc_now().isoformat()
                    for row in unsent_rows:
                        connection.execute(
                            """INSERT INTO reviewer_delivery_attempts(
                                   queue_id, reviewer_id, recipient_email, attempted_at, status, error_text
                               ) VALUES (?, ?, ?, ?, 'error', ?)""",
                            (row["id"], reviewer["id"], reviewer["email"], attempted_at, error_text),
                        )
                        connection.execute(
                            """UPDATE reviewer_notification_queue
                               SET attempts = attempts + 1, last_error = ? WHERE id = ?""",
                            (f"{reviewer['name']} <{reviewer['email']}>: {error_text}", row["id"]),
                        )
                    summary["errors"] += len(unsent_rows)
                    connection.commit()
                    notify_technical_error(
                        settings,
                        db,
                        subject="При отправке сообщения обнаружена ошибка",
                        fio=reviewer["name"],
                        email_address=reviewer["email"],
                        error_type="контролирующий не может получить сообщение",
                        error_text=error_text,
                    )

            # Запись считается доставленной только после успешной отправки всем активным
            # контролирующим, назначенным на этот тест.
            for row in rows:
                sent_count = connection.execute(
                    """SELECT COUNT(DISTINCT a.reviewer_id) AS count
                       FROM reviewer_delivery_attempts a
                       JOIN reviewers r ON r.id = a.reviewer_id AND r.enabled = 1
                       JOIN reviewer_templates rt
                         ON rt.reviewer_id = r.id AND rt.template_id = ?
                       WHERE a.queue_id = ? AND a.status = 'sent'""",
                    (template_id, row["id"]),
                ).fetchone()["count"]
                required_count = connection.execute(
                    """SELECT COUNT(*) AS count
                       FROM reviewers r
                       JOIN reviewer_templates rt ON rt.reviewer_id = r.id
                       WHERE r.enabled = 1 AND rt.template_id = ?""",
                    (template_id,),
                ).fetchone()["count"]

                if required_count > 0 and sent_count >= required_count:
                    delivered_at = utc_now().isoformat()
                    connection.execute(
                        """UPDATE reviewer_notification_queue
                           SET status = 'delivered', delivered_at = ?, last_error = NULL,
                               attempts = attempts + 1
                           WHERE id = ?""",
                        (delivered_at, row["id"]),
                    )
                    employee = connection.execute(
                        "SELECT * FROM employees WHERE worker_key = ?", (row["worker_key"],)
                    ).fetchone()
                    if employee:
                        _journal(
                            connection,
                            event_type="Уведомление контролирующему",
                            employee=employee,
                            template=template,
                            status="sent",
                            reminder_number=row["reminder_count"],
                            recipient=", ".join(r["email"] for r in reviewers),
                            details=f"Сводное письмо: {len(rows)} работников",
                        )
                else:
                    summary["pending"] += 1

    return summary


def cleanup_journal(db: Database) -> int:
    with db.connect() as connection:
        retention = int(_setting(connection, "journal_retention_days", "365"))
        cutoff = (utc_now() - timedelta(days=retention)).isoformat()
        cursor = connection.execute("DELETE FROM notification_journal WHERE created_at < ?", (cutoff,))
        return int(cursor.rowcount)


def process_reminders(settings: Settings, db: Database, dry_run: bool = False) -> dict:
    settings.templates[:] = load_test_definitions(settings, db)
    ensure_mail_templates(db, settings.templates)
    try_sync_indigo_results(settings, db)
    now = utc_now()
    summary = {"sent": 0, "skipped": 0, "escalated": 0, "errors": 0, "dry_run": dry_run}

    with db.connect() as connection:
        enabled = _setting(connection, "reminders_enabled", "1") == "1"
        interval_days = int(_setting(connection, "reminder_interval_days", "7"))
        max_reminders = int(_setting(connection, "max_reminders", "10"))
        notify_reviewers = _setting(connection, "reviewer_notifications_enabled", "1") == "1"
        if not enabled:
            return summary

        for template in settings.templates:
            if not template.get("enabled", True):
                continue
            for employee in _employees_for_template(connection, template):
                result = summarize_employee_result(connection, employee, template, now=now.replace(tzinfo=None))
                if result.status == "completed":
                    summary["skipped"] += 1
                    continue

                initial = connection.execute(
                    """
                    SELECT * FROM notification_history
                    WHERE worker_key = ? AND employment_seq = ? AND template_id = ? AND status = 'sent'
                      AND method IN ('automatic', 'manual_seed')
                    ORDER BY sent_at ASC LIMIT 1
                    """,
                    (employee["worker_key"], employee["employment_seq"], str(template["id"])),
                ).fetchone()
                if not initial:
                    summary["skipped"] += 1
                    continue

                reminders = connection.execute(
                    """
                    SELECT * FROM notification_history
                    WHERE worker_key = ? AND employment_seq = ? AND template_id = ? AND status = 'sent'
                      AND method = 'reminder'
                    ORDER BY sent_at ASC
                    """,
                    (employee["worker_key"], employee["employment_seq"], str(template["id"])),
                ).fetchall()
                reminder_count = len(reminders)
                anchor = reminders[-1]["sent_at"] if reminders else initial["sent_at"]
                try:
                    anchor_dt = datetime.fromisoformat(anchor)
                    if anchor_dt.tzinfo is None:
                        anchor_dt = anchor_dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    summary["errors"] += 1
                    continue
                if now < anchor_dt + timedelta(days=interval_days):
                    summary["skipped"] += 1
                    continue

                if reminder_count >= max_reminders:
                    if notify_reviewers:
                        _create_queue_item(
                            connection, employee, template, reminder_count,
                            reminders[0]["sent_at"] if reminders else None,
                            reminders[-1]["sent_at"] if reminders else None,
                        )
                    summary["escalated"] += 1
                    continue

                email_address = str(employee["email"] or "").strip()
                if not email_address:
                    _journal(connection, event_type="Напоминание", employee=employee, template=template,
                             status="error", reminder_number=reminder_count + 1,
                             details="Нет адреса электронной почты")
                    summary["errors"] += 1
                    continue

                if dry_run:
                    summary["sent"] += 1
                    continue

                try:
                    subject, body, mail_enabled = _render_reminder(settings, db, template, employee, reminder_count + 1)
                    if not mail_enabled:
                        summary["skipped"] += 1
                        continue
                    send_html_email(settings.smtp, email_address, subject, body)
                    connection.execute(
                        """INSERT INTO notification_history(worker_key, employment_seq, template_id, email, sent_at, status, method, error_text)
                           VALUES (?, ?, ?, ?, ?, 'sent', 'reminder', NULL)""",
                        (employee["worker_key"], employee["employment_seq"], str(template["id"]), email_address, now.isoformat()),
                    )
                    _journal(connection, event_type="Напоминание", employee=employee, template=template,
                             status="sent", reminder_number=reminder_count + 1, recipient=email_address)
                    summary["sent"] += 1
                except Exception as error:
                    error_text = str(error)
                    connection.execute(
                        """INSERT INTO notification_history(worker_key, employment_seq, template_id, email, sent_at, status, method, error_text)
                           VALUES (?, ?, ?, ?, ?, 'error', 'reminder', ?)""",
                        (employee["worker_key"], employee["employment_seq"], str(template["id"]), email_address, now.isoformat(), error_text),
                    )
                    _journal(connection, event_type="Напоминание", employee=employee, template=template,
                             status="error", reminder_number=reminder_count + 1,
                             recipient=email_address, details=error_text)
                    summary["errors"] += 1
                    connection.commit()
                    notify_technical_error(
                        settings, db, subject="При отправке сообщения обнаружена ошибка",
                        fio=employee["fio"], email_address=email_address,
                        error_type="ошибка отправки SMTP", error_text=error_text,
                    )

    if not dry_run:
        dispatch_pending_reviewer_notifications(settings, db)
        cleanup_journal(db)
    return summary
