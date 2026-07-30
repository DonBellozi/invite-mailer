from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .db import Database
from .identity import bootstrap_legacy_overrides
from .indigo import sync_indigo_results
from .logic import (
    fetch_and_import,
    rebuild_report,
    run_full,
    seed_manual,
    send_notifications,
)
from .settings import load_settings
from .runtime_settings import apply_runtime_settings, bootstrap_runtime_settings
from .reminders import process_reminders
from .technical_alerts import dispatch_technical_error_digest


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger("invite-mailer")


def _read_app_setting(db: Database, key: str, default: str) -> str:
    with db.connect() as connection:
        row = connection.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (key,),
        ).fetchone()
    return str(row["value"]) if row else default


def _indigo_sync_due(db: Database) -> bool:
    try:
        interval = int(_read_app_setting(db, "indigo_sync_interval_minutes", "15"))
    except (TypeError, ValueError):
        interval = 15
    interval = max(1, min(interval, 1440))

    last_text = _read_app_setting(db, "indigo_last_auto_sync_at", "").strip()
    if not last_text:
        return True
    try:
        last_run = datetime.fromisoformat(last_text)
        if last_run.tzinfo is None:
            last_run = last_run.replace(tzinfo=timezone.utc)
    except ValueError:
        return True

    return datetime.now(timezone.utc) >= last_run.astimezone(timezone.utc) + timedelta(minutes=interval)


def _mark_indigo_sync_attempt(db: Database) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('indigo_last_auto_sync_at',?,?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value=excluded.value, updated_at=excluded.updated_at",
            (now, now),
        )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Автоматизация рассылки приглашений")
    commands = root.add_subparsers(dest="command", required=True)

    commands.add_parser("scheduler")
    commands.add_parser("fetch")
    commands.add_parser("report")
    commands.add_parser("indigo-sync")

    reminders = commands.add_parser("reminders")
    reminders.add_argument("--dry-run", action="store_true")

    run = commands.add_parser("run")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--skip-fetch", action="store_true")

    seed = commands.add_parser("seed")
    seed.add_argument("--templates", required=True, help="Идентификаторы через запятую")
    seed.add_argument("--sent-date", required=True, help="YYYY-MM-DD")

    return root


def scheduler_mode(settings, db: Database) -> None:
    schedule = settings.config["schedule"]
    timezone = settings.config["app"].get("timezone", "Europe/Moscow")
    scheduler = BlockingScheduler(timezone=timezone)

    def fetch_job():
        try:
            # Планировщик работает в отдельном процессе, поэтому перед проверкой
            # перечитывает изменяемые через Web параметры из SQLite.
            apply_runtime_settings(settings, db)
            with db.connect() as connection:
                values = {row["key"]: row["value"] for row in connection.execute(
                    "SELECT key,value FROM app_settings WHERE key IN "
                    "('fetch_hour','fetch_minute','app_timezone','fetch_last_auto_run')"
                ).fetchall()}
            tz_name = values.get("app_timezone", timezone) or timezone
            local_now = datetime.now(ZoneInfo(tz_name))
            today = local_now.date().isoformat()
            hour = int(values.get("fetch_hour", "8"))
            minute = int(values.get("fetch_minute", "30"))
            if (local_now.hour, local_now.minute) < (hour, minute):
                return
            if values.get("fetch_last_auto_run") == today:
                return
            path = fetch_and_import(settings, db)
            with db.connect() as connection:
                connection.execute(
                    "INSERT INTO app_settings(key,value,updated_at) VALUES('fetch_last_auto_run',?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    (today, local_now.isoformat()),
                )
            rebuild_report(settings, db, sync_indigo=False)
            LOGGER.info("XLSX обработан: %s", path)
        except Exception:
            LOGGER.exception("Ошибка ежедневного получения XLSX")

    def send_job():
        try:
            summary = run_full(settings, db, dry_run=False)
            summary["technical_digest"] = dispatch_technical_error_digest(settings, db)
            LOGGER.info("Рассылка завершена: %s", json.dumps(summary, ensure_ascii=False))
        except Exception:
            LOGGER.exception("Ошибка еженедельной рассылки")

    def reminder_job():
        try:
            local_now = datetime.now(ZoneInfo(timezone))
            today = local_now.date().isoformat()
            with db.connect() as connection:
                values = {
                    row["key"]: row["value"]
                    for row in connection.execute(
                        "SELECT key, value FROM app_settings WHERE key IN ('reminders_enabled','reminder_run_hour','reminder_run_minute','reminder_run_day_of_week','reminder_last_auto_run')"
                    ).fetchall()
                }
            if values.get("reminders_enabled", "1") != "1":
                return
            hour = int(values.get("reminder_run_hour", "9"))
            minute = int(values.get("reminder_run_minute", "15"))
            day_of_week = int(values.get("reminder_run_day_of_week", "6"))
            if local_now.weekday() != day_of_week:
                return
            if (local_now.hour, local_now.minute) < (hour, minute):
                return
            if values.get("reminder_last_auto_run") == today:
                return
            summary = process_reminders(settings, db, dry_run=False)
            summary["technical_digest"] = dispatch_technical_error_digest(settings, db)
            with db.connect() as connection:
                connection.execute(
                    "INSERT INTO app_settings(key,value,updated_at) VALUES('reminder_last_auto_run',?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (today, local_now.isoformat()),
                )
            rebuild_report(settings, db, sync_indigo=False)
            LOGGER.info("Автоматические напоминания: %s", json.dumps(summary, ensure_ascii=False))
        except Exception:
            LOGGER.exception("Ошибка автоматической отправки напоминаний")

    def indigo_job():
        if not _indigo_sync_due(db):
            return
        try:
            count = sync_indigo_results(
                settings,
                db,
            )

            LOGGER.info(
                "Результаты Indigo обновлены: %s",
                count,
            )

        except Exception:
            LOGGER.exception(
                "Ошибка обновления результатов Indigo"
            )

        finally:
            _mark_indigo_sync_attempt(db)
            try:
                rebuild_report(
                    settings,
                    db,
                    sync_indigo=False,
                )

                LOGGER.info(
                    "HTML-отчет обновлен после синхронизации Indigo"
                )

            except Exception:
                LOGGER.exception(
                    "Не удалось обновить HTML-отчет"
                )


    scheduler.add_job(
        fetch_job,
        CronTrigger(minute="*", timezone=timezone),
        id="daily_fetch",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        send_job,
        CronTrigger(
            day_of_week=schedule["send_day_of_week"],
            hour=int(schedule["send_hour"]),
            minute=int(schedule["send_minute"]),
            timezone=timezone,
        ),
        id="weekly_send",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.add_job(
        reminder_job,
        CronTrigger(minute="*", timezone=timezone),
        id="automatic_reminders",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        indigo_job,
        CronTrigger(
            minute="*",
            timezone=timezone,
        ),
        id="automatic_indigo_sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    rebuild_report(settings, db)
    LOGGER.info("Планировщик запущен")
    scheduler.start()


def main() -> int:
    args = parser().parse_args()
    settings = load_settings()
    db = Database(settings.database_path)
    bootstrap_runtime_settings(settings, db)
    bootstrap_legacy_overrides(settings, db)

    if args.command == "scheduler":
        scheduler_mode(settings, db)
        return 0

    if args.command == "fetch":
        path = fetch_and_import(settings, db)
        rebuild_report(settings, db)
        print(path)
        return 0

    if args.command == "report":
        print(rebuild_report(settings, db))
        return 0

    if args.command == "reminders":
        summary = process_reminders(settings, db, dry_run=args.dry_run)
        rebuild_report(settings, db, sync_indigo=False)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.command == "indigo-sync":
        try:
            count = sync_indigo_results(settings, db)
        finally:
            # Отчет пересобирается после любой попытки синхронизации:
            # как успешной, так и завершившейся ошибкой.
            rebuild_report(
                settings,
                db,
                sync_indigo=False,
            )

        print(f"Загружено результатов Indigo: {count}")
        return 0

    if args.command == "run":
        if args.skip_fetch:
            summary = send_notifications(settings, db, dry_run=args.dry_run)
            rebuild_report(settings, db, sync_indigo=False)
        else:
            summary = run_full(settings, db, dry_run=args.dry_run)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.command == "seed":
        template_ids = [item.strip() for item in args.templates.split(",") if item.strip()]
        count = seed_manual(settings, db, template_ids, args.sent_date)
        rebuild_report(settings, db)
        print(f"Создано записей первичной рассылки: {count}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
