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


def _load_login_overrides(config: dict[str, Any]) -> dict[str, str]:
    """Загружает исключения из YAML и дополняет их значениями из Portainer.

    LOGIN_OVERRIDES_JSON имеет приоритет над YAML и должен содержать объект:
    {"email@domain.ru": "ad_login"}
    """
    overrides: dict[str, str] = {}

    overrides_path = config.get("identity", {}).get(
        "overrides_file", str(ROOT / "config/login_overrides.yaml")
    )
    overrides_file = Path(overrides_path)
    if overrides_file.exists():
        overrides_data = _read_yaml(overrides_file)
        for item in overrides_data.get("overrides", []):
            email = str(item.get("email", "")).strip().lower()
            login = str(item.get("login", "")).strip()
            if email and login:
                overrides[email] = login

    raw_json = os.getenv("LOGIN_OVERRIDES_JSON", "").strip()
    if not raw_json:
        return overrides

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

    for email_address, login_value in environment_overrides.items():
        email = str(email_address).strip().lower()
        login = str(login_value).strip()
        if not email or not login:
            raise RuntimeError(
                "LOGIN_OVERRIDES_JSON содержит пустой email или логин"
            )
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
class Settings:
    config: dict[str, Any]
    templates: list[dict[str, Any]]
    login_overrides: dict[str, str]
    imap: ImapSettings
    smtp: SmtpSettings
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
    templates_path = os.getenv("TEMPLATES_PATH", str(ROOT / "config/templates.yaml"))

    config = _read_yaml(config_path)
    templates_data = _read_yaml(templates_path)
    overrides = _load_login_overrides(config)

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

    secret = os.getenv("WORKER_HASH_SECRET", "")
    if len(secret) < 16:
        raise RuntimeError("WORKER_HASH_SECRET должен содержать не менее 16 символов")

    return Settings(
        config=config,
        templates=templates_data.get("templates", []),
        login_overrides=overrides,
        imap=imap,
        smtp=smtp,
        worker_hash_secret=secret,
    )
