from __future__ import annotations

from datetime import datetime

from .db import Database
from .settings import Settings
from .xlsx_parser import EMAIL_RE


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def normalize_email(value: str) -> str:
    email = str(value or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise ValueError("Некорректный адрес электронной почты")
    return email


def normalize_login(value: str) -> str:
    login = str(value or "").strip().lower()
    if not login:
        raise ValueError("Логин не может быть пустым")
    if any(character.isspace() for character in login):
        raise ValueError("Логин не должен содержать пробелы")
    return login


def default_login(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.split("@", 1)[0].strip().lower() or None


def bootstrap_legacy_overrides(settings: Settings, db: Database) -> int:
    """Однократно переносит прежние YAML/JSON-исключения в SQLite.

    Записи, уже созданные через админку, не перезаписываются. После успешного
    переноса ставится отметка, поэтому удаленное в админке сопоставление не
    появится снова после перезапуска контейнера.
    """
    if not settings.login_overrides:
        return 0

    timestamp = now_iso()
    inserted = 0
    with db.connect() as connection:
        imported = connection.execute(
            "SELECT value FROM app_state WHERE key = 'login_overrides_legacy_imported'"
        ).fetchone()
        if imported:
            return 0

        for raw_email, raw_login in settings.login_overrides.items():
            try:
                email = normalize_email(raw_email)
                login = normalize_login(raw_login)
            except ValueError:
                continue
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO login_overrides(
                    email, login, source, created_at, updated_at
                ) VALUES (?, ?, 'legacy', ?, ?)
                """,
                (email, login, timestamp, timestamp),
            )
            inserted += int(cursor.rowcount or 0)

        _apply_overrides_to_employees(connection)
        connection.execute(
            """
            INSERT INTO app_state(key, value)
            VALUES('login_overrides_legacy_imported', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (timestamp,),
        )
    return inserted


def get_login_overrides(db: Database) -> dict[str, str]:
    with db.connect() as connection:
        return {
            str(row["email"]).strip().lower(): str(row["login"]).strip().lower()
            for row in connection.execute(
                "SELECT email, login FROM login_overrides ORDER BY email"
            ).fetchall()
        }


def _apply_overrides_to_employees(connection) -> None:
    employees = connection.execute(
        "SELECT worker_key, email FROM employees WHERE email IS NOT NULL AND email <> ''"
    ).fetchall()
    overrides = {
        row["email"]: row["login"]
        for row in connection.execute("SELECT email, login FROM login_overrides").fetchall()
    }
    timestamp = now_iso()
    for employee in employees:
        email = str(employee["email"]).strip().lower()
        login = overrides.get(email) or default_login(email)
        connection.execute(
            "UPDATE employees SET login = ?, updated_at = ? WHERE worker_key = ?",
            (login, timestamp, employee["worker_key"]),
        )


def list_overrides(db: Database) -> list[dict]:
    with db.connect() as connection:
        rows = connection.execute(
            """
            SELECT
                o.email,
                o.login,
                o.source,
                o.created_at,
                o.updated_at,
                e.fio,
                e.active
            FROM login_overrides o
            LEFT JOIN employees e
              ON lower(trim(e.email)) = o.email
            ORDER BY o.email, e.active DESC, e.fio
            """
        ).fetchall()

    grouped: dict[str, dict] = {}
    for row in rows:
        item = grouped.setdefault(
            row["email"],
            {
                "email": row["email"],
                "login": row["login"],
                "source": row["source"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "fio": None,
                "active": None,
            },
        )
        if item["fio"] is None and row["fio"]:
            item["fio"] = row["fio"]
            item["active"] = bool(row["active"])
    return list(grouped.values())


def save_override(db: Database, raw_email: str, raw_login: str) -> dict:
    email = normalize_email(raw_email)
    login = normalize_login(raw_login)
    timestamp = now_iso()

    with db.connect() as connection:
        conflict = connection.execute(
            "SELECT email FROM login_overrides WHERE login = ? AND email <> ?",
            (login, email),
        ).fetchone()
        if conflict:
            raise ValueError(
                f"Логин {login} уже сопоставлен с адресом {conflict['email']}"
            )

        employee_conflict = connection.execute(
            """
            SELECT fio, email
            FROM employees
            WHERE active = 1
              AND lower(trim(login)) = ?
              AND lower(trim(COALESCE(email, ''))) <> ?
            ORDER BY fio
            LIMIT 1
            """,
            (login, email),
        ).fetchone()
        if employee_conflict:
            raise ValueError(
                f"Логин {login} уже используется сотрудником "
                f"{employee_conflict['fio']} ({employee_conflict['email'] or 'нет e-mail'})"
            )

        connection.execute(
            """
            INSERT INTO login_overrides(email, login, source, created_at, updated_at)
            VALUES (?, ?, 'admin', ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                login = excluded.login,
                source = 'admin',
                updated_at = excluded.updated_at
            """,
            (email, login, timestamp, timestamp),
        )
        _apply_overrides_to_employees(connection)

        employee = connection.execute(
            """
            SELECT fio, active
            FROM employees
            WHERE lower(trim(email)) = ?
            ORDER BY active DESC, fio
            LIMIT 1
            """,
            (email,),
        ).fetchone()

    return {
        "email": email,
        "login": login,
        "fio": employee["fio"] if employee else None,
        "active": bool(employee["active"]) if employee else None,
        "updated_at": timestamp,
    }


def delete_override(db: Database, raw_email: str) -> bool:
    email = normalize_email(raw_email)
    with db.connect() as connection:
        cursor = connection.execute("DELETE FROM login_overrides WHERE email = ?", (email,))
        _apply_overrides_to_employees(connection)
        return bool(cursor.rowcount)
