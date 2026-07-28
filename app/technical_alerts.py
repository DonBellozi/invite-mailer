from __future__ import annotations

import hashlib
import html
import logging
from datetime import datetime, timedelta, timezone

from .db import Database
from .mailer import send_html_email
from .settings import Settings

LOGGER = logging.getLogger("invite-mailer.technical-alerts")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _read_setting(connection, key: str, default: str = "") -> str:
    row = connection.execute(
        "SELECT value FROM app_settings WHERE key = ?",
        (key,),
    ).fetchone()
    return str(row["value"]) if row else default


def technical_settings(db: Database) -> dict:
    with db.connect() as connection:
        repeat_hours_raw = _read_setting(connection, "technical_repeat_hours", "72")
        try:
            repeat_hours = max(1, int(repeat_hours_raw))
        except ValueError:
            repeat_hours = 72
        recipients = [
            {"name": str(row["name"]), "email": str(row["email"]).strip()}
            for row in connection.execute(
                """
                SELECT name, email
                FROM reviewers
                WHERE enabled = 1 AND receives_technical_errors = 1
                ORDER BY name COLLATE NOCASE, email COLLATE NOCASE
                """
            ).fetchall()
            if str(row["email"]).strip()
        ]
    return {"recipients": recipients, "repeat_hours": repeat_hours}


def _fingerprint(error_type: str, employee_email: str, error_text: str) -> str:
    value = "\n".join((error_type.strip(), employee_email.strip().lower(), error_text.strip()))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def notify_technical_error(
    settings: Settings,
    db: Database,
    *,
    subject: str,
    fio: str,
    email_address: str,
    error_type: str,
    error_text: str = "",
) -> bool:
    config = technical_settings(db)
    now = utc_now()
    now_iso = now.isoformat()
    fingerprint = _fingerprint(error_type, email_address, error_text)

    with db.connect() as connection:
        previous = connection.execute(
            """
            SELECT * FROM technical_errors
            WHERE fingerprint = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (fingerprint,),
        ).fetchone()

        should_send = bool(config["recipients"])
        if previous and previous["notified_at"]:
            try:
                notified_at = datetime.fromisoformat(previous["notified_at"])
                if notified_at.tzinfo is None:
                    notified_at = notified_at.replace(tzinfo=timezone.utc)
                if now - notified_at < timedelta(hours=config["repeat_hours"]):
                    should_send = False
            except ValueError:
                pass

        connection.execute(
            """
            INSERT INTO technical_errors(
                fingerprint, fio, email, error_type, error_text,
                detected_at, notified_at, notification_error
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            (fingerprint, fio, email_address, error_type, error_text, now_iso),
        )
        row_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    if not should_send:
        return False

    lines = [
        f"<p>ФИО: {html.escape(fio or 'Не указано')}</p>",
        f"<p>E-mail: {html.escape(email_address or 'Не указан')}</p>",
        f"<p>Тип ошибки: {html.escape(error_type)}</p>",
    ]
    if error_text:
        lines.append(f"<pre style='white-space:pre-wrap'>{html.escape(error_text)}</pre>")
    lines.append(f"<p>Дата обнаружения: {now.astimezone().strftime('%d.%m.%Y %H:%M')}</p>")
    body = "".join(lines)

    delivered = 0
    failures: list[str] = []
    for recipient in config["recipients"]:
        try:
            send_html_email(settings.smtp, recipient["email"], subject, body)
            delivered += 1
        except Exception as error:
            LOGGER.exception("Не удалось отправить техническое уведомление на %s", recipient["email"])
            failures.append(f"{recipient['name']} <{recipient['email']}>: {error}")

    with db.connect() as connection:
        connection.execute(
            "UPDATE technical_errors SET notified_at = ?, notification_error = ? WHERE id = ?",
            (
                now_iso if delivered else None,
                "\n".join(failures) if failures else None,
                row_id,
            ),
        )
    return delivered > 0
