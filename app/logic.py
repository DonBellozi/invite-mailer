from __future__ import annotations

import fnmatch
import json
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .db import Database
from .imap_source import fetch_latest_attachment
from .mailer import send_html_email
from .report import build_report
from .settings import Settings
from .xlsx_parser import EmployeeRecord, parse_xlsx


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _department_matches(department: str | None, rule: dict) -> bool:
    value = (department or "").lower()
    includes = rule.get("include") or ["*"]
    excludes = rule.get("exclude") or []

    included = any(fnmatch.fnmatch(value, str(pattern).lower()) for pattern in includes)
    excluded = any(fnmatch.fnmatch(value, str(pattern).lower()) for pattern in excludes)
    return included and not excluded


def _template_applies(employee, template: dict) -> bool:
    return _department_matches(employee["department"], template.get("departments", {}))


def _is_due(connection, employee, template: dict, now: datetime) -> bool:
    latest = connection.execute(
        """
        SELECT * FROM notification_history
        WHERE worker_key = ? AND employment_seq = ? AND template_id = ? AND status = 'sent'
        ORDER BY sent_at DESC LIMIT 1
        """,
        (employee["worker_key"], employee["employment_seq"], template["id"]),
    ).fetchone()

    if latest is None:
        return True
    if template.get("mode", "once") == "once":
        return False

    validity_days = int(template.get("validity_days") or 0)
    if validity_days <= 0:
        raise ValueError(f"У периодического шаблона {template['id']} не задан validity_days")
    last_sent = datetime.fromisoformat(latest["sent_at"])
    return last_sent + timedelta(days=validity_days) <= now


def import_employees(db: Database, records: list[EmployeeRecord], absence_grace_imports: int) -> None:
    timestamp = now_iso()
    seen = {record.worker_key for record in records}

    with db.connect() as connection:
        existing_rows = {
            row["worker_key"]: row
            for row in connection.execute("SELECT * FROM employees").fetchall()
        }

        for record in records:
            existing = existing_rows.get(record.worker_key)
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO employees(
                        worker_key, fio, email, login, department, position,
                        active, employment_seq, first_seen_at, last_seen_at,
                        missed_imports, inactive_since, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, ?, 0, NULL, ?)
                    """,
                    (
                        record.worker_key,
                        record.fio,
                        record.email,
                        record.login,
                        record.department,
                        record.position,
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
                continue

            employment_seq = existing["employment_seq"]
            if not existing["active"]:
                employment_seq += 1

            connection.execute(
                """
                UPDATE employees SET
                    fio = ?, email = ?, login = ?, department = ?, position = ?,
                    active = 1, employment_seq = ?, last_seen_at = ?,
                    missed_imports = 0, inactive_since = NULL, updated_at = ?
                WHERE worker_key = ?
                """,
                (
                    record.fio,
                    record.email,
                    record.login,
                    record.department,
                    record.position,
                    employment_seq,
                    timestamp,
                    timestamp,
                    record.worker_key,
                ),
            )

        for key, existing in existing_rows.items():
            if key in seen or not existing["active"]:
                continue
            missed = existing["missed_imports"] + 1
            if missed >= absence_grace_imports:
                connection.execute(
                    """
                    UPDATE employees
                    SET active = 0, missed_imports = ?, inactive_since = ?, updated_at = ?
                    WHERE worker_key = ?
                    """,
                    (missed, timestamp, timestamp, key),
                )
            else:
                connection.execute(
                    "UPDATE employees SET missed_imports = ?, updated_at = ? WHERE worker_key = ?",
                    (missed, timestamp, key),
                )


def fetch_and_import(settings: Settings, db: Database) -> Path:
    source = settings.config["source"]
    xlsx_cfg = settings.config["xlsx"]
    archive_dir = settings.data_path / "archive"
    current_file = settings.data_path / "current.xlsx"

    result = fetch_latest_attachment(
        settings.imap,
        source["attachment_filename"],
        archive_dir,
    )

    with db.connect() as connection:
        duplicate = connection.execute(
            "SELECT id FROM imports WHERE file_hash = ? AND status = 'success'",
            (result.file_hash,),
        ).fetchone()
    if duplicate:
        if not current_file.exists():
            shutil.copy2(result.saved_path, current_file)
        return current_file

    records = parse_xlsx(
        result.saved_path,
        xlsx_cfg["columns"],
        int(xlsx_cfg.get("header_search_rows", 20)),
        settings.worker_hash_secret,
        settings.login_overrides,
        sheet_name=source.get("sheet_name"),
        lowercase_login=bool(settings.config.get("identity", {}).get("lowercase", True)),
    )

    min_employees = int(source.get("min_employees", 1))
    if len(records) < min_employees:
        raise ValueError(f"В XLSX найдено только {len(records)} сотрудников, минимум: {min_employees}")

    with db.connect() as connection:
        previous = connection.execute(
            "SELECT row_count FROM imports WHERE status = 'success' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if previous and previous["row_count"]:
        old = int(previous["row_count"])
        drop = max(0.0, (old - len(records)) / old * 100)
        max_drop = float(source.get("max_drop_percent", 35))
        if drop > max_drop:
            raise ValueError(
                f"Число сотрудников уменьшилось с {old} до {len(records)} ({drop:.1f}%), "
                f"что превышает допустимые {max_drop:.1f}%"
            )

    import_employees(db, records, int(source.get("absence_grace_imports", 1)))
    shutil.copy2(result.saved_path, current_file)

    with db.connect() as connection:
        connection.execute(
            """
            INSERT INTO imports(
                message_uid, message_date, sender, subject, attachment_name,
                file_hash, saved_path, row_count, status, error_text, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'success', NULL, ?)
            """,
            (
                result.uid,
                result.message_date,
                result.sender,
                result.subject,
                result.filename,
                result.file_hash,
                str(result.saved_path),
                len(records),
                now_iso(),
            ),
        )

    return current_file


def send_notifications(settings: Settings, db: Database, dry_run: bool = False) -> dict:
    start = datetime.now()
    environment = Environment(
        loader=FileSystemLoader("/"),
        undefined=StrictUndefined,
        autoescape=True,
    )

    allowed_domains = {
        domain.lower() for domain in settings.config.get("mail", {}).get("allowed_domains", [])
    }
    validate_domain = bool(settings.config.get("mail", {}).get("validate_domain", True))
    delay = float(settings.config.get("mail", {}).get("send_delay_seconds", 0))

    summary = {"sent": 0, "skipped": 0, "errors": 0, "dry_run": dry_run}

    with db.connect() as connection:
        employees = connection.execute(
            """
            SELECT *
            FROM employees
            WHERE active = 1
              AND email IS NOT NULL
              AND trim(email) <> ''
            """
        ).fetchall()

        for employee in employees:
            for template in settings.templates:
                if not template.get("enabled", True) or not _template_applies(employee, template):
                    continue
                if not _is_due(connection, employee, template, start):
                    summary["skipped"] += 1
                    continue

                email_address = employee["email"]
                if not email_address:
                    summary["errors"] += 1
                    if not dry_run:
                        connection.execute(
                            """
                            INSERT INTO notification_history(
                                worker_key, employment_seq, template_id, email,
                                sent_at, status, method, error_text
                            ) VALUES (?, ?, ?, NULL, ?, 'error', 'automatic', ?)
                            """,
                            (
                                employee["worker_key"],
                                employee["employment_seq"],
                                template["id"],
                                now_iso(),
                                "Нет адреса электронной почты",
                            ),
                        )
                    continue

                domain = email_address.rsplit("@", 1)[-1].lower()
                if validate_domain and allowed_domains and domain not in allowed_domains:
                    summary["errors"] += 1
                    if not dry_run:
                        connection.execute(
                            """
                            INSERT INTO notification_history(
                                worker_key, employment_seq, template_id, email,
                                sent_at, status, method, error_text
                            ) VALUES (?, ?, ?, ?, ?, 'error', 'automatic', ?)
                            """,
                            (
                                employee["worker_key"],
                                employee["employment_seq"],
                                template["id"],
                                email_address,
                                now_iso(),
                                f"Недопустимый почтовый домен: {domain}",
                            ),
                        )
                    continue

                if dry_run:
                    summary["sent"] += 1
                    continue

                try:
                    body = environment.get_template(template["body_template"]).render(
                        fio=employee["fio"],
                        email=email_address,
                        login=employee["login"],
                        department=employee["department"],
                        position=employee["position"],
                    )
                    send_html_email(
                        settings.smtp,
                        email_address,
                        template["subject"],
                        body,
                    )
                    connection.execute(
                        """
                        INSERT INTO notification_history(
                            worker_key, employment_seq, template_id, email,
                            sent_at, status, method, error_text
                        ) VALUES (?, ?, ?, ?, ?, 'sent', 'automatic', NULL)
                        """,
                        (
                            employee["worker_key"],
                            employee["employment_seq"],
                            template["id"],
                            email_address,
                            now_iso(),
                        ),
                    )
                    summary["sent"] += 1
                    if delay > 0:
                        time.sleep(delay)
                except Exception as error:
                    connection.execute(
                        """
                        INSERT INTO notification_history(
                            worker_key, employment_seq, template_id, email,
                            sent_at, status, method, error_text
                        ) VALUES (?, ?, ?, ?, ?, 'error', 'automatic', ?)
                        """,
                        (
                            employee["worker_key"],
                            employee["employment_seq"],
                            template["id"],
                            email_address,
                            now_iso(),
                            str(error),
                        ),
                    )
                    summary["errors"] += 1

    return summary


def seed_manual(settings: Settings, db: Database, template_ids: list[str], sent_date: str) -> int:
    timestamp = datetime.fromisoformat(sent_date).replace(hour=12, minute=0, second=0).isoformat()
    valid_ids = {template["id"] for template in settings.templates}
    unknown = set(template_ids) - valid_ids
    if unknown:
        raise ValueError(f"Неизвестные шаблоны: {', '.join(sorted(unknown))}")

    count = 0
    with db.connect() as connection:
        employees = connection.execute("SELECT * FROM employees WHERE active = 1").fetchall()
        for employee in employees:
            for template_id in template_ids:
                exists = connection.execute(
                    """
                    SELECT id FROM notification_history
                    WHERE worker_key = ? AND employment_seq = ? AND template_id = ? AND status = 'sent'
                    LIMIT 1
                    """,
                    (employee["worker_key"], employee["employment_seq"], template_id),
                ).fetchone()
                if exists:
                    continue
                connection.execute(
                    """
                    INSERT INTO notification_history(
                        worker_key, employment_seq, template_id, email,
                        sent_at, status, method, error_text
                    ) VALUES (?, ?, ?, ?, ?, 'sent', 'manual_seed', NULL)
                    """,
                    (
                        employee["worker_key"],
                        employee["employment_seq"],
                        template_id,
                        employee["email"],
                        timestamp,
                    ),
                )
                count += 1
    return count


def rebuild_report(settings: Settings, db: Database) -> Path:
    output = settings.reports_path / "index.html"
    build_report(
        db,
        settings.templates,
        settings.config.get("report", {}).get("title", "Отчет"),
        output,
    )
    return output


def run_full(settings: Settings, db: Database, dry_run: bool = False) -> dict:
    fetch_and_import(settings, db)
    summary = send_notifications(settings, db, dry_run=dry_run)
    rebuild_report(settings, db)
    return summary
