from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .db import Database
from .settings import ImapSettings, IndigoSettings, Settings, SmtpSettings


BOOTSTRAP_VERSION = "4"

# Эти значения используются только тогда, когда SQLite уже был инициализирован,
# но отдельный ключ по какой-либо причине отсутствует. Переменные окружения в
# таком случае намеренно не используются: после первичного импорта источником
# рабочих настроек является только SQLite.
RUNTIME_DEFAULTS: dict[str, str] = {
    "imap_host": "",
    "imap_port": "993",
    "imap_ssl": "1",
    "imap_username": "",
    "imap_password": "",
    "imap_folder": "INBOX",
    "imap_from_contains": "1c-robot@",
    "imap_lookback_days": "3",
    "imap_attachment_filename": "",
    "fetch_hour": "8",
    "fetch_minute": "30",
    "app_timezone": "Europe/Moscow",
    "smtp_host": "",
    "smtp_port": "465",
    "smtp_mode": "ssl",
    "smtp_username": "",
    "smtp_password": "",
    "smtp_from_email": "",
    "smtp_from_name": "Система тестирования",
    "smtp_reply_to": "",
    "indigo_enabled": "0",
    "indigo_host": "",
    "indigo_port": "5432",
    "indigo_database": "",
    "indigo_username": "",
    "indigo_password": "",
    "indigo_sslmode": "prefer",
    "indigo_connect_timeout": "5",
    "indigo_view": "res.results_view",
    "indigo_sync_interval_minutes": "15",
    "allowed_domains": "",
    "validate_domain": "1",
    "backup_enabled": "1",
    "backup_hour": "2",
    "backup_minute": "0",
    "backup_retention": "14",
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_all(db: Database) -> dict[str, str]:
    with db.connect() as connection:
        rows = connection.execute("SELECT key, value FROM app_settings").fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows}


def _insert_missing(db: Database, values: dict[str, str]) -> None:
    now = _now()
    with db.connect() as connection:
        for key, value in values.items():
            connection.execute(
                "INSERT OR IGNORE INTO app_settings(key,value,updated_at) VALUES(?,?,?)",
                (key, value, now),
            )


def _save_bootstrap_version(db: Database) -> None:
    now = _now()
    with db.connect() as connection:
        connection.execute(
            """INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            ("integration_settings_bootstrap_version", BOOTSTRAP_VERSION, now),
        )


def _initial_values(settings: Settings) -> dict[str, str]:
    """Формирует значения только для самого первого импорта в SQLite."""
    domains = settings.config.get("mail", {}).get("allowed_domains", [])
    return {
        "imap_host": settings.imap.host,
        "imap_port": str(settings.imap.port),
        "imap_ssl": "1" if settings.imap.ssl else "0",
        "imap_username": settings.imap.username,
        "imap_password": settings.imap.password,
        "imap_folder": settings.imap.folder,
        "imap_from_contains": settings.imap.from_contains,
        "imap_lookback_days": str(settings.imap.lookback_days),
        "imap_attachment_filename": str(settings.config.get("source", {}).get("attachment_filename", "")),
        "fetch_hour": str(settings.config.get("schedule", {}).get("fetch_hour", 8)),
        "fetch_minute": str(settings.config.get("schedule", {}).get("fetch_minute", 30)),
        "app_timezone": str(settings.config.get("app", {}).get("timezone", "Europe/Moscow")),
        "smtp_host": settings.smtp.host,
        "smtp_port": str(settings.smtp.port),
        "smtp_mode": settings.smtp.mode,
        "smtp_username": settings.smtp.username,
        "smtp_password": settings.smtp.password,
        "smtp_from_email": settings.smtp.from_email,
        "smtp_from_name": settings.smtp.from_name,
        "smtp_reply_to": "",
        "indigo_enabled": "1" if settings.indigo.enabled else "0",
        "indigo_host": settings.indigo.host,
        "indigo_port": str(settings.indigo.port),
        "indigo_database": settings.indigo.database,
        "indigo_username": settings.indigo.username,
        "indigo_password": settings.indigo.password,
        "indigo_sslmode": settings.indigo.sslmode,
        "indigo_connect_timeout": str(settings.indigo.connect_timeout),
        "indigo_view": settings.indigo.view,
        "indigo_sync_interval_minutes": "15",
        "allowed_domains": "\n".join(
            str(item).strip().lower() for item in domains if str(item).strip()
        ),
        "validate_domain": (
            "1" if settings.config.get("mail", {}).get("validate_domain", True) else "0"
        ),
    }


def bootstrap_runtime_settings(settings: Settings, db: Database) -> None:
    """Инициализирует Runtime Settings и применяет их к текущему процессу.

    При первой установке значения SMTP, Indigo и доменов импортируются из
    YAML/.env в SQLite. После появления маркера инициализации внешние значения
    больше не участвуют в восстановлении отсутствующих ключей и не могут
    перезаписать настройки, сохраненные через Web UI.
    """
    current = _read_all(db)
    previous_version = current.get("integration_settings_bootstrap_version", "")

    if not previous_version:
        # Самый первый запуск: переносим существующую конфигурацию установки.
        _insert_missing(db, _initial_values(settings))
    else:
        # При обновлении не импортируем уже перенесенные SMTP/Indigo повторно.
        if previous_version != BOOTSTRAP_VERSION:
            # v4 впервые переносит IMAP, имя XLSX и расписание получения.
            initial = _initial_values(settings)
            _insert_missing(db, {key: initial[key] for key in (
                "imap_host", "imap_port", "imap_ssl", "imap_username",
                "imap_password", "imap_folder", "imap_from_contains",
                "imap_lookback_days", "imap_attachment_filename",
                "fetch_hour", "fetch_minute", "app_timezone",
            )})
        _insert_missing(db, RUNTIME_DEFAULTS)

    _save_bootstrap_version(db)
    apply_runtime_settings(settings, db)


def _int_value(values: dict[str, str], key: str) -> int:
    raw = values.get(key, RUNTIME_DEFAULTS[key])
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(RUNTIME_DEFAULTS[key])


def apply_runtime_settings(settings: Settings, db: Database) -> None:
    """Применяет рабочие настройки из SQLite без fallback к .env."""
    values = _read_all(db)

    imap = ImapSettings(
        host=values.get("imap_host", RUNTIME_DEFAULTS["imap_host"]).strip(),
        port=_int_value(values, "imap_port"),
        ssl=values.get("imap_ssl", RUNTIME_DEFAULTS["imap_ssl"]) == "1",
        username=values.get("imap_username", RUNTIME_DEFAULTS["imap_username"]).strip(),
        password=values.get("imap_password", RUNTIME_DEFAULTS["imap_password"]),
        folder=values.get("imap_folder", RUNTIME_DEFAULTS["imap_folder"]).strip() or "INBOX",
        from_contains=values.get("imap_from_contains", RUNTIME_DEFAULTS["imap_from_contains"]).strip(),
        lookback_days=_int_value(values, "imap_lookback_days"),
    )
    settings.config.setdefault("source", {})["attachment_filename"] = values.get(
        "imap_attachment_filename", RUNTIME_DEFAULTS["imap_attachment_filename"]
    ).strip()
    settings.config.setdefault("schedule", {})["fetch_hour"] = _int_value(values, "fetch_hour")
    settings.config["schedule"]["fetch_minute"] = _int_value(values, "fetch_minute")
    settings.config.setdefault("app", {})["timezone"] = values.get(
        "app_timezone", RUNTIME_DEFAULTS["app_timezone"]
    ).strip() or "Europe/Moscow"

    smtp = SmtpSettings(
        host=values.get("smtp_host", RUNTIME_DEFAULTS["smtp_host"]).strip(),
        port=_int_value(values, "smtp_port"),
        mode=values.get("smtp_mode", RUNTIME_DEFAULTS["smtp_mode"]).strip().lower(),
        username=values.get("smtp_username", RUNTIME_DEFAULTS["smtp_username"]).strip(),
        password=values.get("smtp_password", RUNTIME_DEFAULTS["smtp_password"]),
        from_email=values.get("smtp_from_email", RUNTIME_DEFAULTS["smtp_from_email"]).strip(),
        from_name=values.get("smtp_from_name", RUNTIME_DEFAULTS["smtp_from_name"]).strip(),
    )
    indigo = IndigoSettings(
        enabled=values.get("indigo_enabled", RUNTIME_DEFAULTS["indigo_enabled"]) == "1",
        host=values.get("indigo_host", RUNTIME_DEFAULTS["indigo_host"]).strip(),
        port=_int_value(values, "indigo_port"),
        database=values.get("indigo_database", RUNTIME_DEFAULTS["indigo_database"]).strip(),
        username=values.get("indigo_username", RUNTIME_DEFAULTS["indigo_username"]).strip(),
        password=values.get("indigo_password", RUNTIME_DEFAULTS["indigo_password"]),
        sslmode=values.get("indigo_sslmode", RUNTIME_DEFAULTS["indigo_sslmode"]).strip(),
        connect_timeout=_int_value(values, "indigo_connect_timeout"),
        view=values.get("indigo_view", RUNTIME_DEFAULTS["indigo_view"]).strip(),
    )
    domains = [
        item.strip().lower()
        for item in values.get("allowed_domains", RUNTIME_DEFAULTS["allowed_domains"])
        .replace(",", "\n")
        .splitlines()
        if item.strip()
    ]
    settings.config.setdefault("mail", {})["allowed_domains"] = domains
    settings.config["mail"]["validate_domain"] = (
        values.get("validate_domain", RUNTIME_DEFAULTS["validate_domain"]) == "1"
    )
    object.__setattr__(settings, "imap", imap)
    object.__setattr__(settings, "smtp", smtp)
    object.__setattr__(settings, "indigo", indigo)


def save_values(db: Database, values: dict[str, Any]) -> None:
    now = _now()
    with db.connect() as connection:
        for key, value in values.items():
            connection.execute(
                """INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, str(value), now),
            )
