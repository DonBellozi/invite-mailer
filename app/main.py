from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .db import Database
from .logic import (
    fetch_and_import,
    rebuild_report,
    run_full,
    seed_manual,
    send_notifications,
)
from .settings import load_settings


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger("invite-mailer")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Автоматизация рассылки приглашений")
    commands = root.add_subparsers(dest="command", required=True)

    commands.add_parser("scheduler")
    commands.add_parser("fetch")
    commands.add_parser("report")

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
            path = fetch_and_import(settings, db)
            rebuild_report(settings, db)
            LOGGER.info("XLSX обработан: %s", path)
        except Exception:
            LOGGER.exception("Ошибка ежедневного получения XLSX")

    def send_job():
        try:
            summary = run_full(settings, db, dry_run=False)
            LOGGER.info("Рассылка завершена: %s", json.dumps(summary, ensure_ascii=False))
        except Exception:
            LOGGER.exception("Ошибка еженедельной рассылки")

    scheduler.add_job(
        fetch_job,
        CronTrigger(
            hour=int(schedule["fetch_hour"]),
            minute=int(schedule["fetch_minute"]),
            timezone=timezone,
        ),
        id="daily_fetch",
        replace_existing=True,
        max_instances=1,
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

    rebuild_report(settings, db)
    LOGGER.info("Планировщик запущен")
    scheduler.start()


def main() -> int:
    args = parser().parse_args()
    settings = load_settings()
    db = Database(settings.database_path)

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

    if args.command == "run":
        if args.skip_fetch:
            summary = send_notifications(settings, db, dry_run=args.dry_run)
            rebuild_report(settings, db)
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
