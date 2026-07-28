from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from .db import Database
from .mailer import send_html_email
from .mail_templates import ensure_mail_templates, render_mail_template
from .settings import Settings
from .test_catalog import load_test_definitions

LOGGER = logging.getLogger("invite-mailer.technical-alerts")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _read_setting(connection, key: str, default: str = "") -> str:
    row = connection.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def technical_settings(db: Database) -> dict:
    with db.connect() as connection:
        try:
            repeat_hours = max(1, int(_read_setting(connection, "technical_repeat_hours", "72")))
        except ValueError:
            repeat_hours = 72
        recipients = [
            {"name": str(row["name"]), "email": str(row["email"]).strip()}
            for row in connection.execute("""
                SELECT name, email FROM reviewers
                WHERE enabled = 1 AND receives_technical_errors = 1
                ORDER BY name COLLATE NOCASE, email COLLATE NOCASE
            """).fetchall() if str(row["email"]).strip()
        ]
    return {"recipients": recipients, "repeat_hours": repeat_hours}


def _fingerprint(error_type: str, employee_email: str, error_text: str) -> str:
    value = "\n".join((error_type.strip(), employee_email.strip().lower(), error_text.strip()))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def notify_technical_error(settings: Settings, db: Database, *, subject: str, fio: str,
                           email_address: str, error_type: str, error_text: str = "") -> bool:
    """Регистрирует ошибку. Общая сводка отправляется после завершения рассылки."""
    config = technical_settings(db)
    now = utc_now()
    fingerprint = _fingerprint(error_type, email_address, error_text)
    with db.connect() as connection:
        previous = connection.execute("""
            SELECT detected_at, notified_at FROM technical_errors
            WHERE fingerprint = ? ORDER BY id DESC LIMIT 1
        """, (fingerprint,)).fetchone()
        if previous:
            stamp = previous["notified_at"] or previous["detected_at"]
            try:
                last = datetime.fromisoformat(stamp)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if now - last < timedelta(hours=config["repeat_hours"]):
                    return False
            except (ValueError, TypeError):
                pass
        connection.execute("""
            INSERT INTO technical_errors(
                fingerprint, fio, email, error_type, error_text,
                detected_at, notified_at, notification_error
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
        """, (fingerprint, fio, email_address, error_type, error_text, now.isoformat()))
    return True


def _display_timestamp(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "Не указано"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone().strftime("%d.%m.%Y %H:%M:%S")
    except (TypeError, ValueError):
        return text


def _technical_context(rows) -> dict:
    errors = [
        {
            "fio": str(row["fio"] or "Не указано"),
            "email": str(row["email"] or "Не указан"),
            "error_type": str(row["error_type"] or "Не указано"),
            "error_text": str(row["error_text"] or ""),
            "detected_at": _display_timestamp(row["detected_at"]),
        }
        for row in rows
    ]
    first = errors[0]
    return {
        "subject": "Ошибки отправки писем",
        "fio": first["fio"],
        "email": first["email"],
        "error_type": first["error_type"],
        "error_text": first["error_text"],
        "detected_at": first["detected_at"],
        "errors": errors,
        "errors_count": len(errors),
        "displayed_errors_count": len(errors),
    }

def dispatch_technical_error_digest(settings: Settings, db: Database) -> dict:
    """Отправляет одно письмо со списком всех новых ошибок текущей рассылки."""
    config = technical_settings(db)
    if not config["recipients"]:
        return {"errors": 0, "delivered": 0}
    with db.connect() as connection:
        rows = connection.execute("""
            SELECT * FROM technical_errors
            WHERE notified_at IS NULL AND (notification_error IS NULL OR notification_error = '')
            ORDER BY detected_at, id
        """).fetchall()
    if not rows:
        return {"errors": 0, "delivered": 0}

    settings.templates[:] = load_test_definitions(settings, db)
    ensure_mail_templates(db, settings.templates)
    context = _technical_context(rows)
    subject, body, enabled = render_mail_template(db, "technical", "*", context)
    if not enabled:
        return {"errors": len(rows), "delivered": 0}
    if not subject.strip(): subject = "Ошибки отправки писем"
    if not body.strip():
        body = "<p>Во время рассылки обнаружены технические ошибки.</p>" + "".join(
            f"<hr><p><strong>ФИО:</strong> {item['fio']}<br><strong>E-mail:</strong> {item['email']}<br>"
            f"<strong>Тип ошибки:</strong> {item['error_type']}<br><strong>Дата обнаружения:</strong> {item['detected_at']}</p>"
            for item in context["errors"]
        )
    delivered = 0; failures = []
    for recipient in config["recipients"]:
        try:
            send_html_email(settings.smtp, recipient["email"], subject, body)
            delivered += 1
        except Exception as error:
            LOGGER.exception("Не удалось отправить сводку технических ошибок на %s", recipient["email"])
            failures.append(f"{recipient['name']} <{recipient['email']}>: {error}")
    now_iso = utc_now().isoformat(); ids = [int(r["id"]) for r in rows]
    with db.connect() as connection:
        placeholders = ",".join("?" for _ in ids)
        if delivered:
            connection.execute(f"UPDATE technical_errors SET notified_at=?, notification_error=NULL WHERE id IN ({placeholders})", (now_iso, *ids))
        elif failures:
            connection.execute(f"UPDATE technical_errors SET notification_error=? WHERE id IN ({placeholders})", ("\n".join(failures), *ids))
    return {"errors": len(rows), "delivered": delivered}
