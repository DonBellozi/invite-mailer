from __future__ import annotations

import fnmatch
import logging
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .audience import is_explicit_template
from .db import Database
from .identity import get_login_overrides
from .imap_source import fetch_latest_attachment
from .indigo import summarize_employee_result, try_sync_indigo_results
from .mailer import send_html_email
from .mail_templates import ensure_mail_templates, render_mail_template
from .report import build_report
from .settings import Settings
from .test_catalog import load_test_definitions
from .technical_alerts import notify_technical_error
from .xlsx_parser import EmployeeRecord, parse_xlsx


LOGGER = logging.getLogger("invite-mailer.logic")


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _department_matches(department: str | None, rule: dict) -> bool:
    value = (department or "").lower()
    includes = rule.get("include") or ["*"]
    excludes = rule.get("exclude") or []

    included = any(fnmatch.fnmatch(value, str(pattern).lower()) for pattern in includes)
    excluded = any(fnmatch.fnmatch(value, str(pattern).lower()) for pattern in excludes)
    return included and not excluded


def _candidate_employees(connection, template: dict):
    """Возвращает только сотрудников, которым применим конкретный шаблон."""
    if is_explicit_template(template):
        return connection.execute(
            """
            SELECT e.*
            FROM test_assignments a
            JOIN employees e
              ON e.worker_key = a.worker_key
             AND e.employment_seq = a.employment_seq
            WHERE a.template_id = ?
              AND a.active = 1
              AND e.active = 1
            ORDER BY e.fio
            """,
            (template["id"],),
        ).fetchall()

    employees = connection.execute(
        "SELECT * FROM employees WHERE active = 1 ORDER BY fio"
    ).fetchall()
    return [
        employee
        for employee in employees
        if _department_matches(employee["department"], template.get("departments", {}))
    ]


def _is_due(connection, employee, template: dict, now: datetime) -> bool:
    result = summarize_employee_result(connection, employee, template, now=now)

    latest_sent = connection.execute(
        """
        SELECT * FROM notification_history
        WHERE worker_key = ? AND employment_seq = ? AND template_id = ? AND status = 'sent'
        ORDER BY sent_at DESC LIMIT 1
        """,
        (employee["worker_key"], employee["employment_seq"], template["id"]),
    ).fetchone()

    if template.get("mode", "once") == "once":
        # Уже пройденный разовый тест повторно не рассылаем, даже если запись
        # о старой ручной рассылке отсутствует.
        if result.status == "completed":
            return False
        return latest_sent is None

    validity_days = int(template.get("validity_days") or 0)
    if validity_days <= 0:
        raise ValueError(f"У периодического шаблона {template['id']} не задан validity_days")

    # Действующий успешный результат блокирует новое уведомление. Любая новая
    # успешная попытка, в том числе добровольная, переносит next_due_at вперед.
    if result.status == "completed":
        return False

    if result.next_due_at:
        cycle_start = datetime.fromisoformat(result.next_due_at)
        if now < cycle_start:
            return False
        # В новом цикле отправляем одно основное приглашение. Повторные
        # напоминания будут отдельным механизмом.
        if latest_sent and datetime.fromisoformat(latest_sent["sent_at"]) >= cycle_start:
            return False
        return True

    # Периодический тест еще ни разу не пройден: первое приглашение отправляется
    # один раз, затем ожидается результат.
    return latest_sent is None


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
                        active, employment_seq, first_seen_at, employment_started_at, last_seen_at,
                        missed_imports, inactive_since, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?, 0, NULL, ?)
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
                        timestamp,
                    ),
                )
                continue

            employment_seq = existing["employment_seq"]
            employment_started_at = existing["employment_started_at"] or existing["first_seen_at"]
            if not existing["active"]:
                employment_seq += 1
                employment_started_at = timestamp

            connection.execute(
                """
                UPDATE employees SET
                    fio = ?, email = ?, login = ?, department = ?, position = ?,
                    active = 1, employment_seq = ?, employment_started_at = ?, last_seen_at = ?,
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
                    employment_started_at,
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


def _parse_employee_file(settings: Settings, db: Database, path: Path) -> list[EmployeeRecord]:
    source = settings.config["source"]
    xlsx_cfg = settings.config["xlsx"]
    return parse_xlsx(
        path,
        xlsx_cfg["columns"],
        int(xlsx_cfg.get("header_search_rows", 20)),
        settings.worker_hash_secret,
        get_login_overrides(db),
        sheet_name=source.get("sheet_name"),
        lowercase_login=bool(settings.config.get("identity", {}).get("lowercase", True)),
    )


def _report_invalid_domains(settings: Settings, db: Database, records: list[EmployeeRecord]) -> None:
    mail_config = settings.config.get("mail", {})
    if not bool(mail_config.get("validate_domain", True)):
        return
    allowed_domains = {str(domain).strip().lower() for domain in mail_config.get("allowed_domains", []) if str(domain).strip()}
    if not allowed_domains:
        return
    for record in records:
        address = (record.email or "").strip()
        if not address or "@" not in address:
            continue
        domain = address.rsplit("@", 1)[-1].lower()
        if domain in allowed_domains:
            continue
        notify_technical_error(
            settings,
            db,
            subject="При обработке данных обнаружена ошибка",
            fio=record.fio,
            email_address=address,
            error_type="адрес электронной почты не принадлежит домену организации",
        )


def fetch_and_import(settings: Settings, db: Database) -> Path:
    source = settings.config["source"]
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
        # Даже для уже обработанного XLSX заново применяем LOGIN_OVERRIDES_JSON.
        records = _parse_employee_file(settings, db, current_file)
        _report_invalid_domains(settings, db, records)
        import_employees(db, records, int(source.get("absence_grace_imports", 1)))
        return current_file

    records = _parse_employee_file(settings, db, result.saved_path)
    _report_invalid_domains(settings, db, records)

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
    settings.templates[:] = load_test_definitions(settings, db)
    try_sync_indigo_results(settings, db)
    start = datetime.now()
    ensure_mail_templates(db, settings.templates)

    allowed_domains = {
        domain.lower() for domain in settings.config.get("mail", {}).get("allowed_domains", [])
    }
    validate_domain = bool(settings.config.get("mail", {}).get("validate_domain", True))
    delay = float(settings.config.get("mail", {}).get("send_delay_seconds", 0))

    summary = {"sent": 0, "skipped": 0, "errors": 0, "dry_run": dry_run}

    with db.connect() as connection:
        for template in settings.templates:
            if not template.get("enabled", True):
                continue

            employees = _candidate_employees(connection, template)
            for employee in employees:
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
                        error_message = f"Недопустимый почтовый домен: {domain}"
                        connection.execute(
                            """
                            INSERT INTO notification_history(
                                worker_key, employment_seq, template_id, email,
                                sent_at, status, method, error_text
                            ) VALUES (?, ?, ?, ?, ?, 'error', 'automatic', ?)
                            """,
                            (employee["worker_key"], employee["employment_seq"], template["id"], email_address, now_iso(), error_message),
                        )
                        connection.commit()
                        notify_technical_error(
                            settings, db,
                            subject="При обработке данных обнаружена ошибка",
                            fio=employee["fio"],
                            email_address=email_address,
                            error_type="адрес электронной почты не принадлежит домену организации",
                        )
                    continue

                if dry_run:
                    summary["sent"] += 1
                    continue

                try:
                    context = {
                        "fio": employee["fio"], "email": email_address,
                        "login": employee["login"], "department": employee["department"],
                        "position": employee["position"], "test_name": template.get("name", template["id"]),
                    }
                    subject, body, mail_enabled = render_mail_template(
                        db, "invitation", str(template["id"]), context
                    )
                    if not mail_enabled:
                        summary["skipped"] += 1
                        continue
                    send_html_email(settings.smtp, email_address, subject, body)
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
                    connection.commit()
                    notify_technical_error(
                        settings, db,
                        subject="При отправке сообщения обнаружена ошибка",
                        fio=employee["fio"],
                        email_address=email_address,
                        error_type="ошибка отправки SMTP",
                        error_text=str(error),
                    )

    return summary


def seed_manual(settings: Settings, db: Database, template_ids: list[str], sent_date: str) -> int:
    settings.templates[:] = load_test_definitions(settings, db)
    timestamp = datetime.fromisoformat(sent_date).replace(hour=12, minute=0, second=0).isoformat()
    templates_by_id = {template["id"]: template for template in settings.templates}
    unknown = set(template_ids) - set(templates_by_id)
    if unknown:
        raise ValueError(f"Неизвестные шаблоны: {', '.join(sorted(unknown))}")

    count = 0
    with db.connect() as connection:
        for template_id in template_ids:
            template = templates_by_id[template_id]
            employees = _candidate_employees(connection, template)
            for employee in employees:
                # Нулевая рассылка не должна отмечать уведомленными людей без email.
                if not employee["email"]:
                    continue
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


def rebuild_report(settings: Settings, db: Database, sync_indigo: bool = True) -> Path:
    settings.templates[:] = load_test_definitions(settings, db)
    if sync_indigo:
        try_sync_indigo_results(settings, db)
    output = settings.reports_path / "index.html"
    build_report(
        db,
        settings.templates,
        settings.config.get("report", {}).get("title", "Отчет"),
        output,
        indigo_enabled=settings.indigo.enabled,
    )
    return output


def run_full(settings: Settings, db: Database, dry_run: bool = False) -> dict:
    fetch_and_import(settings, db)
    summary = send_notifications(settings, db, dry_run=dry_run)
    rebuild_report(settings, db, sync_indigo=False)
    return summary
