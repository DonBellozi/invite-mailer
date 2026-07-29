from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


ROOT = Path("/opt/invite-mailer")


def _read_yaml(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _load_login_overrides() -> dict[str, str]:
    """Загружает прежние исключения из окружения для переноса в SQLite.

    Основным источником является таблица login_overrides. Переменная
    LOGIN_OVERRIDES_JSON сохранена только для обратной совместимости.
    """
    raw_json = os.getenv("LOGIN_OVERRIDES_JSON", "").strip()
    if not raw_json:
        return {}

    try:
        environment_overrides = json.loads(raw_json)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "LOGIN_OVERRIDES_JSON содержит некорректный JSON. "
            'Ожидается формат {"email@domain.ru":"login"}'
        ) from error

    if not isinstance(environment_overrides, dict):
        raise RuntimeError(
            "LOGIN_OVERRIDES_JSON должен содержать JSON-объект вида "
            '{"email@domain.ru":"login"}'
        )

    overrides: dict[str, str] = {}
    for email_address, login_value in environment_overrides.items():
        email = str(email_address).strip().lower()
        login = str(login_value).strip().lower()
        if not email or not login:
            raise RuntimeError("LOGIN_OVERRIDES_JSON содержит пустой email или логин")
        overrides[email] = login

    return overrides


@dataclass(frozen=True)
class ImapSettings:
    host: str
    port: int
    ssl: bool
    username: str
    password: str
    folder: str
    from_contains: str
    lookback_days: int


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    mode: str
    username: str
    password: str
    from_email: str
    from_name: str


@dataclass(frozen=True)
class IndigoSettings:
    enabled: bool
    host: str
    port: int
    database: str
    username: str
    password: str
    sslmode: str
    connect_timeout: int
    view: str

    def validate(self) -> None:
        if not self.enabled:
            return
        missing = [
            name
            for name, value in {
                "INDIGO_DB_HOST": self.host,
                "INDIGO_DB_NAME": self.database,
                "INDIGO_DB_USER": self.username,
                "INDIGO_DB_PASSWORD": self.password,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Для подключения к Indigo не заданы: " + ", ".join(missing)
            )


@dataclass(frozen=True)
class Settings:
    config: dict[str, Any]
    templates: list[dict[str, Any]]
    login_overrides: dict[str, str]
    imap: ImapSettings
    smtp: SmtpSettings
    indigo: IndigoSettings
    worker_hash_secret: str

    @property
    def database_path(self) -> Path:
        return Path(self.config["app"]["database_path"])

    @property
    def reports_path(self) -> Path:
        return Path(self.config["app"]["reports_path"])

    @property
    def data_path(self) -> Path:
        return Path(self.config["app"]["data_path"])


def load_settings() -> Settings:
    config_path = os.getenv("CONFIG_PATH", str(ROOT / "config/config.yaml"))

    config = _read_yaml(config_path)
    overrides = _load_login_overrides()

    imap = ImapSettings(
        host=os.environ["IMAP_HOST"],
        port=int(os.getenv("IMAP_PORT", "993")),
        ssl=_bool_env("IMAP_SSL", True),
        username=os.environ["IMAP_USERNAME"],
        password=os.environ["IMAP_PASSWORD"],
        folder=os.getenv("IMAP_FOLDER", "INBOX"),
        from_contains=os.getenv("IMAP_FROM_CONTAINS", "1c-robot@"),
        lookback_days=int(os.getenv("IMAP_LOOKBACK_DAYS", "3")),
    )

    smtp = SmtpSettings(
        host=os.environ["SMTP_HOST"],
        port=int(os.getenv("SMTP_PORT", "465")),
        mode=os.getenv("SMTP_MODE", "ssl").strip().lower(),
        username=os.getenv("SMTP_USERNAME", ""),
        password=os.getenv("SMTP_PASSWORD", ""),
        from_email=os.environ["SMTP_FROM_EMAIL"],
        from_name=os.getenv("SMTP_FROM_NAME", "Система тестирования"),
    )

    indigo_config = config.get("indigo", {})
    indigo = IndigoSettings(
        enabled=_bool_env("INDIGO_ENABLED", bool(indigo_config.get("enabled", False))),
        host=os.getenv("INDIGO_DB_HOST", str(indigo_config.get("host", ""))).strip(),
        port=int(os.getenv("INDIGO_DB_PORT", str(indigo_config.get("port", 5432)))),
        database=os.getenv(
            "INDIGO_DB_NAME", str(indigo_config.get("database", ""))
        ).strip(),
        username=os.getenv(
            "INDIGO_DB_USER", str(indigo_config.get("username", ""))
        ).strip(),
        password=os.getenv(
            "INDIGO_DB_PASSWORD", str(indigo_config.get("password", ""))
        ),
        sslmode=os.getenv(
            "INDIGO_DB_SSLMODE", str(indigo_config.get("sslmode", "prefer"))
        ).strip(),
        connect_timeout=int(
            os.getenv(
                "INDIGO_DB_CONNECT_TIMEOUT",
                str(indigo_config.get("connect_timeout", 5)),
            )
        ),
        view=str(indigo_config.get("view", "res.results_view")).strip(),
    )
    indigo.validate()

    secret = os.getenv("WORKER_HASH_SECRET", "")
    if len(secret) < 16:
        raise RuntimeError("WORKER_HASH_SECRET должен содержать не менее 16 символов")

    return Settings(
        config=config,
        templates=[],
        login_overrides=overrides,
        imap=imap,
        smtp=smtp,
        indigo=indigo,
        worker_hash_secret=secret,
    )
