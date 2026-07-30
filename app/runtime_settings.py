from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from .db import Database
from .settings import IndigoSettings, Settings, SmtpSettings


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


def bootstrap_runtime_settings(settings: Settings, db: Database) -> None:
    """Однократно переносит интеграционные параметры в SQLite и применяет их.

    Операция идемпотентна: существующие значения из Web не перезаписываются.
    """
    domains = settings.config.get("mail", {}).get("allowed_domains", [])
    defaults = {
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
        "allowed_domains": "\n".join(str(x).strip().lower() for x in domains if str(x).strip()),
        "validate_domain": "1" if settings.config.get("mail", {}).get("validate_domain", True) else "0",
        "integration_settings_bootstrap_version": "2",
    }
    _insert_missing(db, defaults)
    apply_runtime_settings(settings, db)


def apply_runtime_settings(settings: Settings, db: Database) -> None:
    values = _read_all(db)
    smtp = SmtpSettings(
        host=values.get("smtp_host", settings.smtp.host).strip(),
        port=int(values.get("smtp_port", settings.smtp.port)),
        mode=values.get("smtp_mode", settings.smtp.mode).strip().lower(),
        username=values.get("smtp_username", settings.smtp.username).strip(),
        password=values.get("smtp_password", settings.smtp.password),
        from_email=values.get("smtp_from_email", settings.smtp.from_email).strip(),
        from_name=values.get("smtp_from_name", settings.smtp.from_name).strip(),
    )
    indigo = IndigoSettings(
        enabled=values.get("indigo_enabled", "1" if settings.indigo.enabled else "0") == "1",
        host=values.get("indigo_host", settings.indigo.host).strip(),
        port=int(values.get("indigo_port", settings.indigo.port)),
        database=values.get("indigo_database", settings.indigo.database).strip(),
        username=values.get("indigo_username", settings.indigo.username).strip(),
        password=values.get("indigo_password", settings.indigo.password),
        sslmode=values.get("indigo_sslmode", settings.indigo.sslmode).strip(),
        connect_timeout=int(values.get("indigo_connect_timeout", settings.indigo.connect_timeout)),
        view=values.get("indigo_view", settings.indigo.view).strip(),
    )
    domains = [x.strip().lower() for x in values.get("allowed_domains", "").replace(",", "\n").splitlines() if x.strip()]
    settings.config.setdefault("mail", {})["allowed_domains"] = domains
    settings.config["mail"]["validate_domain"] = values.get("validate_domain", "1") == "1"
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
