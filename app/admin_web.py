from __future__ import annotations

import io
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from openpyxl import Workbook

from .audience import (
    AudienceImportError,
    build_issues_workbook,
    confirm_preview,
    create_preview,
    explicit_templates,
    get_preview,
)
from .db import Database
from .identity import (
    bootstrap_legacy_overrides,
    delete_override,
    list_overrides,
    save_override,
)
from .logic import rebuild_report
from .reminders import dispatch_pending_reviewer_notifications
from .settings import Settings, load_settings
from .mail_templates import ensure_mail_templates
from .mail_templates import render_mail_template
from .mailer import send_html_email
from .test_catalog import ensure_test_definitions, load_test_definitions
from .indigo import refresh_indigo_test_catalog, get_indigo_test_catalog

from .report_export import (
    PDF_MEDIA_TYPE,
    XLSX_MEDIA_TYPE,
    build_pdf,
    build_test_export_report,
    build_xlsx,
    download_headers,
    export_filename,
)
from .audience_template import (
    TEMPLATE_MEDIA_TYPE,
    build_audience_template,
    template_download_headers,
)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
security = HTTPBasic(auto_error=False)
app = FastAPI(title="Invite Mailer Admin", docs_url=None, redoc_url=None)

_settings: Settings | None = None
_db: Database | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ensure_v202_schema(db: Database) -> None:
    with db.connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS reviewers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        defaults = {
            "reminders_enabled": "1",
            "reminder_interval_days": "7",
            "reviewer_notifications_enabled": "1",
            "technical_notifications_enabled": "0",
            "technical_email": "",
            "technical_repeat_hours": "72",
            "max_reminders": "10",
            "journal_retention_days": "365",
            "reminder_run_hour": "9",
            "reminder_run_minute": "15",
            "reminder_run_day_of_week": "6",
        }
        now = _utc_now()
        for key, value in defaults.items():
            connection.execute(
                """
                INSERT OR IGNORE INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                """,
                (key, value, now),
            )
        connection.commit()



def _migrate_technical_recipient(db: Database) -> None:
    """Переносит старый отдельный технический e-mail в список контролирующих."""
    with db.connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(reviewers)").fetchall()}
        if "receives_technical_errors" not in columns:
            connection.execute(
                "ALTER TABLE reviewers ADD COLUMN receives_technical_errors INTEGER NOT NULL DEFAULT 0"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS reviewer_templates (
                reviewer_id INTEGER NOT NULL,
                template_id TEXT NOT NULL,
                PRIMARY KEY(reviewer_id, template_id),
                FOREIGN KEY(reviewer_id) REFERENCES reviewers(id) ON DELETE CASCADE
            )
            """
        )
        row = connection.execute(
            "SELECT value FROM app_settings WHERE key = 'technical_email'"
        ).fetchone()
        old_email = str(row["value"]).strip().lower() if row else ""
        if old_email:
            existing = connection.execute(
                "SELECT id FROM reviewers WHERE email = ? COLLATE NOCASE", (old_email,)
            ).fetchone()
            now = _utc_now()
            if existing:
                connection.execute(
                    "UPDATE reviewers SET receives_technical_errors = 1, enabled = 1, updated_at = ? WHERE id = ?",
                    (now, existing["id"]),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO reviewers(name, email, enabled, receives_technical_errors, created_at, updated_at)
                    VALUES (?, ?, 1, 1, ?, ?)
                    """,
                    ("Технический специалист", old_email, now, now),
                )
            connection.execute(
                "UPDATE app_settings SET value = '', updated_at = ? WHERE key = 'technical_email'",
                (now,),
            )
            connection.execute(
                "UPDATE app_settings SET value = '0', updated_at = ? WHERE key = 'technical_notifications_enabled'",
                (now,),
            )
        connection.commit()

def database() -> Database:
    global _db
    if _db is None:
        _db = Database(settings().database_path)
        bootstrap_legacy_overrides(settings(), _db)
        _ensure_v202_schema(_db)
        _migrate_technical_recipient(_db)
        ensure_test_definitions(_db, settings().templates)
        settings().templates[:] = load_test_definitions(settings(), _db)
        ensure_mail_templates(_db, settings().templates)
    return _db


def _read_setting(key: str, default: str) -> str:
    with database().connect() as connection:
        row = connection.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (key,),
        ).fetchone()
    return str(row["value"]) if row else default


def _write_setting(key: str, value: str) -> None:
    now = _utc_now()
    with database().connect() as connection:
        connection.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, now),
        )
        connection.commit()


def require_admin(
    credentials: Annotated[HTTPBasicCredentials | None, Depends(security)],
) -> str:
    expected_username = os.getenv("ADMIN_USERNAME", "").strip()
    expected_password = os.getenv("ADMIN_PASSWORD", "")

    if not expected_username or not expected_password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Не заданы ADMIN_USERNAME и ADMIN_PASSWORD",
        )

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется авторизация",
            headers={"WWW-Authenticate": "Basic"},
        )

    username_ok = secrets.compare_digest(credentials.username, expected_username)
    password_ok = secrets.compare_digest(credentials.password, expected_password)
    if not username_ok or not password_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


class ConfirmRequest(BaseModel):
    row_ids: list[int]


class LoginOverrideRequest(BaseModel):
    email: str
    login: str


class ReminderSettingsRequest(BaseModel):
    enabled: bool
    interval_days: int
    notify_reviewers: bool
    max_reminders: int
    journal_retention_days: int
    technical_repeat_hours: int = 72
    run_hour: int = 9
    run_minute: int = 15
    run_day_of_week: int = 6


class ReviewerRequest(BaseModel):
    name: str
    email: str
    enabled: bool = True
    template_ids: list[str] = []
    receives_technical_errors: bool = False


class TestEmailRequest(BaseModel):
    recipient: str
    kind: str
    template_id: str = "*"
    subject: str
    body_html: str


class TestDefinitionRequest(BaseModel):
    id: str
    enabled: bool = True
    name: str
    mode: str = "once"
    validity_days: int | None = None
    audience_type: str = "all"
    departments_include: list[str] = ["*"]
    departments_exclude: list[str] = []
    indigo_logical_test_id: int | None = None
    indigo_test_name: str = ""
    indigo_success_results: list[str] = []
    indigo_failed_prefixes: list[str] = []


def find_report_template(
    template_id: str,
) -> dict:
    settings().templates[:] = load_test_definitions(settings(), database())
    for template in settings().templates:
        if str(template.get("id")) == template_id:
            return template

    raise HTTPException(
        status_code=404,
        detail="Шаблон теста не найден",
    )


@app.exception_handler(AudienceImportError)
async def audience_error_handler(_, error: AudienceImportError):
    return JSONResponse(
        status_code=400,
        content={"detail": str(error)},
    )


@app.get("/admin/", response_class=HTMLResponse)
def admin_page(_: Annotated[str, Depends(require_admin)]) -> str:
    return ADMIN_HTML


@app.get("/admin/settings/", response_class=HTMLResponse)
def settings_page(_: Annotated[str, Depends(require_admin)]) -> str:
    return SETTINGS_HTML


@app.get("/admin/settings/general/", response_class=HTMLResponse)
def general_settings_page(_: Annotated[str, Depends(require_admin)]) -> str:
    return GENERAL_SETTINGS_HTML


@app.get("/admin/settings/reviewers/", response_class=HTMLResponse)
def reviewers_page(_: Annotated[str, Depends(require_admin)]) -> str:
    return REVIEWERS_HTML


@app.get("/admin/settings/tests/", response_class=HTMLResponse)
def tests_page(_: Annotated[str, Depends(require_admin)]) -> str:
    return TESTS_HTML


@app.get("/admin/settings/templates/", response_class=HTMLResponse)
def templates_page(_: Annotated[str, Depends(require_admin)]) -> str:
    return TEMPLATES_HTML


@app.get("/admin/journal/", response_class=HTMLResponse)
def journal_page(_: Annotated[str, Depends(require_admin)]) -> str:
    return JOURNAL_HTML


@app.get("/admin/logins/", response_class=HTMLResponse)
def login_overrides_page(_: Annotated[str, Depends(require_admin)]) -> str:
    return LOGIN_OVERRIDES_HTML


@app.get("/api/settings/reminders")
def read_reminder_settings(_: Annotated[str, Depends(require_admin)]):
    return {
        "enabled": _read_setting("reminders_enabled", "1") == "1",
        "interval_days": int(_read_setting("reminder_interval_days", "7")),
        "notify_reviewers": _read_setting(
            "reviewer_notifications_enabled", "1"
        ) == "1",
        "max_reminders": int(_read_setting("max_reminders", "10")),
        "journal_retention_days": int(_read_setting("journal_retention_days", "365")),
        "technical_repeat_hours": int(_read_setting("technical_repeat_hours", "72")),
        "run_hour": int(_read_setting("reminder_run_hour", "9")),
        "run_minute": int(_read_setting("reminder_run_minute", "15")),
        "run_day_of_week": int(_read_setting("reminder_run_day_of_week", "6")),
    }


@app.put("/api/settings/reminders")
def write_reminder_settings(
    request: ReminderSettingsRequest,
    _: Annotated[str, Depends(require_admin)],
):
    if request.interval_days < 1 or request.interval_days > 365:
        raise HTTPException(
            status_code=400,
            detail="Интервал должен быть от 1 до 365 дней",
        )
    _write_setting("reminders_enabled", "1" if request.enabled else "0")
    _write_setting("reminder_interval_days", str(request.interval_days))
    if request.max_reminders < 1 or request.max_reminders > 100:
        raise HTTPException(status_code=400, detail="Количество напоминаний должно быть от 1 до 100")
    if request.journal_retention_days < 30 or request.journal_retention_days > 3650:
        raise HTTPException(status_code=400, detail="Срок хранения журнала должен быть от 30 до 3650 дней")
    if request.technical_repeat_hours < 1 or request.technical_repeat_hours > 8760:
        raise HTTPException(status_code=400, detail="Интервал повторения технических уведомлений должен быть от 1 до 8760 часов")
    if request.run_hour < 0 or request.run_hour > 23 or request.run_minute < 0 or request.run_minute > 59:
        raise HTTPException(status_code=400, detail="Некорректное время запуска напоминаний")
    if request.run_day_of_week < 0 or request.run_day_of_week > 6:
        raise HTTPException(status_code=400, detail="Некорректный день запуска напоминаний")
    _write_setting(
        "reviewer_notifications_enabled",
        "1" if request.notify_reviewers else "0",
    )
    _write_setting("max_reminders", str(request.max_reminders))
    _write_setting("journal_retention_days", str(request.journal_retention_days))
    _write_setting("technical_repeat_hours", str(request.technical_repeat_hours))
    _write_setting("reminder_run_hour", str(request.run_hour))
    _write_setting("reminder_run_minute", str(request.run_minute))
    _write_setting("reminder_run_day_of_week", str(request.run_day_of_week))
    return read_reminder_settings(_)


@app.get("/api/reviewers")
def read_reviewers(_: Annotated[str, Depends(require_admin)]):
    settings().templates[:] = load_test_definitions(settings(), database())
    with database().connect() as connection:
        rows = connection.execute(
            """
            SELECT id, name, email, enabled, receives_technical_errors,
                   created_at, updated_at
            FROM reviewers
            ORDER BY enabled DESC, name COLLATE NOCASE, email COLLATE NOCASE
            """
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["template_ids"] = [
                str(template_row["template_id"])
                for template_row in connection.execute(
                    "SELECT template_id FROM reviewer_templates WHERE reviewer_id = ? ORDER BY template_id",
                    (row["id"],),
                ).fetchall()
            ]
            items.append(item)
    templates = [
        {"id": str(template["id"]), "name": template.get("name", template["id"])}
        for template in settings().templates
    ]
    return {"items": items, "templates": templates}


def _save_reviewer_templates(connection, reviewer_id: int, template_ids: list[str]) -> None:
    settings().templates[:] = load_test_definitions(settings(), database())
    allowed = {str(template["id"]) for template in settings().templates}
    normalized = sorted({str(value).strip() for value in template_ids if str(value).strip()})
    unknown = set(normalized) - allowed
    if unknown:
        raise HTTPException(status_code=400, detail=f"Неизвестные тесты: {', '.join(sorted(unknown))}")
    connection.execute("DELETE FROM reviewer_templates WHERE reviewer_id = ?", (reviewer_id,))
    connection.executemany(
        "INSERT INTO reviewer_templates(reviewer_id, template_id) VALUES (?, ?)",
        [(reviewer_id, template_id) for template_id in normalized],
    )


@app.post("/api/reviewers")
def create_reviewer(request: ReviewerRequest, _: Annotated[str, Depends(require_admin)]):
    name = request.name.strip()
    email = request.email.strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="Укажите имя контролирующего")
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(status_code=400, detail="Некорректный e-mail")
    now = _utc_now()
    try:
        with database().connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO reviewers(name, email, enabled, receives_technical_errors, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (name, email, int(request.enabled), int(request.receives_technical_errors), now, now),
            )
            reviewer_id = int(cursor.lastrowid)
            _save_reviewer_templates(connection, reviewer_id, request.template_ids)
    except Exception as error:
        if "UNIQUE" in str(error).upper():
            raise HTTPException(status_code=400, detail="Контролирующий с таким e-mail уже добавлен") from error
        raise
    if request.enabled and request.template_ids:
        dispatch_pending_reviewer_notifications(settings(), database(), set(request.template_ids))
    return read_reviewers(_)


@app.put("/api/reviewers/{reviewer_id}")
def update_reviewer(reviewer_id: int, request: ReviewerRequest, _: Annotated[str, Depends(require_admin)]):
    name = request.name.strip()
    email = request.email.strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="Укажите имя контролирующего")
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(status_code=400, detail="Некорректный e-mail")
    try:
        with database().connect() as connection:
            cursor = connection.execute(
                """
                UPDATE reviewers
                SET name = ?, email = ?, enabled = ?, receives_technical_errors = ?, updated_at = ?
                WHERE id = ?
                """,
                (name, email, int(request.enabled), int(request.receives_technical_errors), _utc_now(), reviewer_id),
            )
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Контролирующий не найден")
            _save_reviewer_templates(connection, reviewer_id, request.template_ids)
    except HTTPException:
        raise
    except Exception as error:
        if "UNIQUE" in str(error).upper():
            raise HTTPException(status_code=400, detail="Контролирующий с таким e-mail уже добавлен") from error
        raise
    if request.enabled and request.template_ids:
        dispatch_pending_reviewer_notifications(settings(), database(), set(request.template_ids))
    return read_reviewers(_)


@app.delete("/api/reviewers/{reviewer_id}")
def delete_reviewer(reviewer_id: int, _: Annotated[str, Depends(require_admin)]):
    with database().connect() as connection:
        cursor = connection.execute("DELETE FROM reviewers WHERE id = ?", (reviewer_id,))
    return {"deleted": cursor.rowcount > 0}


@app.get("/api/journal")
def read_journal(_: Annotated[str, Depends(require_admin)], limit: int = 500):
    limit = max(1, min(limit, 5000))
    with database().connect() as connection:
        rows = connection.execute(
            """
            SELECT id, created_at, event_type, fio, email, department, position,
                   template_name, reminder_number, recipient, status, details
            FROM notification_journal
            ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.get("/api/journal/export.xlsx")
def export_journal(_: Annotated[str, Depends(require_admin)]):
    with database().connect() as connection:
        rows = connection.execute(
            """
            SELECT created_at, event_type, fio, email, department, position,
                   template_name, reminder_number, recipient, status, details
            FROM notification_journal ORDER BY id DESC
            """
        ).fetchall()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Журнал уведомлений"
    sheet.append(["Дата", "Событие", "ФИО", "E-mail", "Подразделение", "Должность", "Тест", "Напоминание", "Получатель", "Статус", "Подробности"])
    for row in rows:
        sheet.append(list(row))
    for column in sheet.columns:
        sheet.column_dimensions[column[0].column_letter].width = min(60, max(12, max(len(str(cell.value or "")) for cell in column) + 2))
    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="notification_journal.xlsx"'},
    )


@app.post("/api/journal/cleanup")
def cleanup_journal(_: Annotated[str, Depends(require_admin)]):
    retention = int(_read_setting("journal_retention_days", "365"))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention)).replace(microsecond=0).isoformat()
    with database().connect() as connection:
        cursor = connection.execute("DELETE FROM notification_journal WHERE created_at < ?", (cutoff,))
    return {"deleted": cursor.rowcount, "retention_days": retention}


@app.get("/api/login-overrides")
def read_login_overrides(_: Annotated[str, Depends(require_admin)]):
    return {"items": list_overrides(database())}


@app.post("/api/login-overrides")
def write_login_override(
    request: LoginOverrideRequest,
    _: Annotated[str, Depends(require_admin)],
):
    try:
        item = save_override(database(), request.email, request.login)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    rebuild_report(settings(), database(), sync_indigo=False)
    return item


@app.delete("/api/login-overrides/{email}")
def remove_login_override(
    email: str,
    _: Annotated[str, Depends(require_admin)],
):
    try:
        deleted = delete_override(database(), email)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    rebuild_report(settings(), database(), sync_indigo=False)
    return {"deleted": deleted}


@app.get("/api/audience/templates")
def list_templates(_: Annotated[str, Depends(require_admin)]):
    settings().templates[:] = load_test_definitions(settings(), database())
    result = []
    for template in explicit_templates(settings()):
        result.append(
            {
                "id": template["id"],
                "name": template.get("name", template["id"]),
                "enabled": bool(template.get("enabled", True)),
            }
        )
    return {"templates": result}


@app.post("/api/audience/preview")
async def preview_upload(
    template_id: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    username: Annotated[str, Depends(require_admin)],
):
    settings().templates[:] = load_test_definitions(settings(), database())
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Размер файла превышает 20 МБ")

    return create_preview(
        settings(),
        database(),
        template_id,
        file.filename or "audience.xlsx",
        content,
        username,
    )


@app.get("/api/audience/imports/{import_id}")
def read_preview(import_id: int, _: Annotated[str, Depends(require_admin)]):
    return get_preview(database(), import_id)


@app.post("/api/audience/imports/{import_id}/confirm")
def confirm_import(
    import_id: int,
    request: ConfirmRequest,
    _: Annotated[str, Depends(require_admin)],
):
    result = confirm_preview(database(), import_id, request.row_ids)
    rebuild_report(settings(), database(), sync_indigo=False)
    return result


@app.get("/api/audience/imports/{import_id}/issues.xlsx")
def download_issues(import_id: int, _: Annotated[str, Depends(require_admin)]):
    content = build_issues_workbook(database(), import_id)
    headers = {
        "Content-Disposition": f'attachment; filename="audience_import_{import_id}_issues.xlsx"'
    }
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )

@app.get(
    "/api/reports/{template_id}/xlsx"
)
def download_test_report_xlsx(
    template_id: str,
):
    template = find_report_template(
        template_id
    )

    report = build_test_export_report(
        database(),
        template,
    )

    content = build_xlsx(report)

    filename = export_filename(
        report,
        "xlsx",
    )

    return StreamingResponse(
        io.BytesIO(content),
        media_type=XLSX_MEDIA_TYPE,
        headers=download_headers(
            filename
        ),
    )


@app.get(
    "/api/reports/{template_id}/pdf"
)
def download_test_report_pdf(
    template_id: str,
):
    template = find_report_template(
        template_id
    )

    report = build_test_export_report(
        database(),
        template,
    )

    content = build_pdf(report)

    filename = export_filename(
        report,
        "pdf",
    )

    return StreamingResponse(
        io.BytesIO(content),
        media_type=PDF_MEDIA_TYPE,
        headers=download_headers(
            filename
        ),
    )


@app.get("/api/audience/template.xlsx")
def download_audience_template(
    _: Annotated[str, Depends(require_admin)],
):
    content = build_audience_template()

    return StreamingResponse(
        io.BytesIO(content),
        media_type=TEMPLATE_MEDIA_TYPE,
        headers=template_download_headers(),
    )


@app.get("/api/health")
def health():
    database()
    return {"status": "ok"}


class MailTemplateRequest(BaseModel):
    subject: str
    body_html: str
    enabled: bool = True


@app.get("/api/settings/templates")
def list_mail_templates(_: Annotated[str, Depends(require_admin)]):
    db = database()
    settings().templates[:] = load_test_definitions(settings(), db)
    ensure_mail_templates(db, settings().templates)
    names = {str(t["id"]): str(t.get("name") or t["id"]) for t in settings().templates}
    with db.connect() as connection:
        rows = connection.execute("SELECT * FROM mail_templates ORDER BY kind, template_id").fetchall()
    return {"items": [dict(row) | {"test_name": names.get(str(row["template_id"]), "Общий шаблон")} for row in rows]}


@app.put("/api/settings/templates/{kind}/{template_id}")
def save_mail_template(kind: str, template_id: str, request: MailTemplateRequest,
                       _: Annotated[str, Depends(require_admin)]):
    if kind not in {"invitation", "reminder", "reviewer", "technical"}:
        raise HTTPException(status_code=400, detail="Неизвестный тип шаблона")
    if not request.subject.strip() or not request.body_html.strip():
        raise HTTPException(status_code=400, detail="Тема и текст письма не могут быть пустыми")
    with database().connect() as connection:
        connection.execute("""UPDATE mail_templates SET subject=?, body_html=?, enabled=?, updated_at=?
                              WHERE kind=? AND template_id=?""",
                           (request.subject.strip(), request.body_html, int(request.enabled), _utc_now(), kind, template_id))
    return {"status": "ok"}


def _display_timestamp(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "Не указано"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone().strftime("%d.%m.%Y %H:%M:%S")
    except (TypeError, ValueError):
        return text


def _technical_test_context(db: Database, limit: int = 20) -> tuple[dict, str, int]:
    with db.connect() as connection:
        rows = connection.execute(
            """
            SELECT fio, email, error_type, error_text, detected_at
            FROM technical_errors
            ORDER BY CASE WHEN notified_at IS NULL THEN 0 ELSE 1 END, detected_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    errors = [
        {
            "fio": str(row["fio"] or "Не указано"),
            "email": str(row["email"] or "Не указан"),
            "error_type": str(row["error_type"] or "Не указано"),
            "error_text": str(row["error_text"] or ""),
            "detected_at": _display_timestamp(row["detected_at"]),
        }
        for row in reversed(rows)
    ]
    source = "real"
    if not errors:
        now = datetime.now().astimezone().strftime("%d.%m.%Y %H:%M:%S")
        errors = [
            {
                "fio": "Иванов Иван Иванович",
                "email": "ivanov.ii@example.ru",
                "error_type": "Ошибка отправки письма",
                "error_text": "SMTP server temporarily unavailable",
                "detected_at": now,
            },
            {
                "fio": "Петров Петр Петрович",
                "email": "petrov.pp@example.ru",
                "error_type": "Некорректный адрес электронной почты",
                "error_text": "Mailbox does not exist",
                "detected_at": now,
            },
        ]
        source = "demo"

    first = errors[0]
    return {
        "subject": "Ошибки отправки писем",
        "fio": first["fio"],
        "email": first["email"],
        "error_type": first["error_type"],
        "error_text": first["error_text"],
        "detected_at": first["detected_at"],
        "errors": errors,
        "errors_count": len(errors),
        "displayed_errors_count": len(errors),
    }, source, len(errors)


def _reviewer_test_context(db: Database) -> tuple[dict, str, int]:
    templates = {str(item["id"]): item for item in load_test_definitions(settings(), db)}
    with db.connect() as connection:
        queue_rows = connection.execute(
            """
            SELECT * FROM reviewer_notification_queue
            ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END, created_at DESC, id DESC
            """
        ).fetchall()

        if queue_rows:
            selected_template_id = str(queue_rows[0]["template_id"])
            selected_rows = [row for row in queue_rows if str(row["template_id"]) == selected_template_id][:20]
            template = templates.get(selected_template_id, {})
            employees = [
                {
                    "fio": str(row["fio"] or "Не указано"),
                    "email": str(row["email"] or "Не указан"),
                    "department": str(row["department"] or "Не указано"),
                    "position": str(row["position"] or "Не указана"),
                    "reminder_count": int(row["reminder_count"] or 0),
                    "first_reminder_at": _display_timestamp(row["first_reminder_at"]),
                    "first_invited_at": _display_timestamp(row["first_reminder_at"]),
                    "last_reminder_at": _display_timestamp(row["last_reminder_at"]),
                }
                for row in selected_rows
            ]
            first = employees[0]
            return {
                **first,
                "test_name": str(template.get("name") or selected_template_id or "Тест"),
                "reviewer_name": "Контролирующий",
                "employees": employees,
                "employees_count": len(employees),
                "displayed_employees_count": len(employees),
            }, "real_queue", len(employees)

        employee_rows = connection.execute(
            """
            SELECT * FROM employees
            ORDER BY active DESC, updated_at DESC, fio COLLATE NOCASE
            LIMIT 5
            """
        ).fetchall()

    if employee_rows:
        template = next(iter(templates.values()), {})
        now = datetime.now().astimezone()
        employees = [
            {
                "fio": str(employee["fio"] or "Не указано"),
                "email": str(employee["email"] or "Не указан"),
                "department": str(employee["department"] or "Не указано"),
                "position": str(employee["position"] or "Не указана"),
                "reminder_count": 3,
                "first_reminder_at": (now - timedelta(days=14)).strftime("%d.%m.%Y %H:%M:%S"),
                "first_invited_at": (now - timedelta(days=14)).strftime("%d.%m.%Y %H:%M:%S"),
                "last_reminder_at": now.strftime("%d.%m.%Y %H:%M:%S"),
            }
            for employee in employee_rows
        ]
        first = employees[0]
        return {
            **first,
            "test_name": str(template.get("name") or "Тест"),
            "reviewer_name": "Контролирующий",
            "employees": employees,
            "employees_count": len(employees),
            "displayed_employees_count": len(employees),
        }, "real_employee", len(employees)

    employees = [
        {
            "fio": "Иванов Иван Иванович",
            "email": "ivanov.ii@example.ru",
            "department": "Тестовое подразделение",
            "position": "Тестовая должность",
            "reminder_count": 3,
            "first_reminder_at": "01.07.2026 09:00:00",
            "first_invited_at": "01.07.2026 09:00:00",
            "last_reminder_at": "15.07.2026 09:00:00",
        },
        {
            "fio": "Петров Петр Петрович",
            "email": "petrov.pp@example.ru",
            "department": "Демонстрационное подразделение",
            "position": "Ведущий специалист",
            "reminder_count": 3,
            "first_reminder_at": "02.07.2026 09:00:00",
            "first_invited_at": "02.07.2026 09:00:00",
            "last_reminder_at": "16.07.2026 09:00:00",
        },
    ]
    first = employees[0]
    return {
        **first,
        "test_name": "Тестовое тестирование",
        "reviewer_name": "Контролирующий",
        "employees": employees,
        "employees_count": len(employees),
        "displayed_employees_count": len(employees),
    }, "demo", len(employees)


@app.post("/api/settings/templates/test-email")
def send_test_template(request: TestEmailRequest, _: Annotated[str, Depends(require_admin)]):
    recipient = request.recipient.strip()
    if not recipient or "@" not in recipient or recipient.startswith("@") or recipient.endswith("@"):
        raise HTTPException(status_code=400, detail="Укажите корректный e-mail получателя")
    if request.kind not in {"invitation", "reminder", "reviewer", "technical"}:
        raise HTTPException(status_code=400, detail="Неизвестный тип шаблона")

    db = database()
    template_name = next(
        (str(t.get("name")) for t in load_test_definitions(settings(), db) if str(t.get("id")) == request.template_id),
        "Тест",
    )
    context = {
        "fio": "Иванов Иван Иванович", "email": recipient, "login": "ivanov.ii",
        "department": "Тестовое подразделение", "position": "Тестовая должность",
        "test_name": template_name, "reminder_number": 1, "reminder_count": 3,
        "first_reminder_at": "01.07.2026 09:00", "last_reminder_at": "15.07.2026 09:00",
        "subject": "Тестовое техническое уведомление", "error_type": "Тестовая ошибка",
        "error_text": "Проверка отображения шаблона", "detected_at": _utc_now(),
        "reviewer_name": "Контролирующий",
    }
    data_source = "demo"
    data_count = 1
    if request.kind == "technical":
        context, data_source, data_count = _technical_test_context(db)
    elif request.kind == "reviewer":
        context, data_source, data_count = _reviewer_test_context(db)

    from jinja2 import Environment, StrictUndefined
    try:
        env = Environment(undefined=StrictUndefined, autoescape=True)
        subject = env.from_string(request.subject).render(**context)
        body = env.from_string(request.body_html).render(**context)
        send_html_email(settings().smtp, recipient, subject, body)
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"Не удалось отправить тестовое письмо: {error}") from error
    return {"status": "ok", "data_source": data_source, "data_count": data_count}


@app.get("/api/settings/indigo-tests")
def list_indigo_tests(_: Annotated[str, Depends(require_admin)], refresh: bool = True):
    db = database()
    if refresh:
        try:
            refresh_indigo_test_catalog(settings(), db)
        except Exception as error:
            with db.connect() as connection:
                connection.execute("INSERT INTO app_state(key,value) VALUES('indigo_catalog_last_error',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(error),))
    return get_indigo_test_catalog(db)


@app.post("/api/settings/indigo-tests/refresh")
def force_refresh_indigo_tests(_: Annotated[str, Depends(require_admin)]):
    try:
        items = refresh_indigo_test_catalog(settings(), database())
        return {"status": "ok", "count": len(items), **get_indigo_test_catalog(database())}
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"Не удалось получить список тестов Indigo: {error}") from error


@app.get("/api/settings/tests")
def list_test_definitions(_: Annotated[str, Depends(require_admin)]):
    items = load_test_definitions(settings(), database())
    return {"items": items}


def _normalize_string_list(values: list[str], default: list[str] | None = None) -> list[str]:
    result = []
    for value in values:
        text = str(value).strip()
        if text and text not in result:
            result.append(text)
    return result or (default or [])


@app.post("/api/settings/tests")
def create_test_definition(request: TestDefinitionRequest, _: Annotated[str, Depends(require_admin)]):
    test_id = request.id.strip().lower()
    if not test_id or not all(ch.isalnum() or ch in "_-" for ch in test_id):
        raise HTTPException(status_code=400, detail="Идентификатор может содержать буквы, цифры, _ и -")
    if request.audience_type not in {"all", "explicit_list"}:
        raise HTTPException(status_code=400, detail="Неизвестный тип участников")
    import json
    now = _utc_now()
    with database().connect() as connection:
        exists = connection.execute("SELECT 1 FROM test_definitions WHERE id=?", (test_id,)).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail="Тест с таким идентификатором уже существует")
        connection.execute("""INSERT INTO test_definitions(id,enabled,name,mode,validity_days,audience_type,departments_include_json,departments_exclude_json,indigo_logical_test_id,indigo_test_name,indigo_success_results_json,indigo_failed_prefixes_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (test_id,int(request.enabled),request.name.strip(),request.mode,request.validity_days,request.audience_type,json.dumps(_normalize_string_list(request.departments_include,["*"]),ensure_ascii=False),json.dumps(_normalize_string_list(request.departments_exclude),ensure_ascii=False),request.indigo_logical_test_id,request.indigo_test_name.strip() or request.name.strip(),json.dumps(_normalize_string_list(request.indigo_success_results),ensure_ascii=False),json.dumps(_normalize_string_list(request.indigo_failed_prefixes),ensure_ascii=False),now,now))
    settings().templates[:] = load_test_definitions(settings(), database())
    ensure_mail_templates(database(), settings().templates)
    return {"status":"ok","id":test_id}


@app.put("/api/settings/tests/{test_id}")
def update_test_definition(test_id: str, request: TestDefinitionRequest, _: Annotated[str, Depends(require_admin)]):
    if request.audience_type not in {"all", "explicit_list"}:
        raise HTTPException(status_code=400, detail="Неизвестный тип участников")
    import json
    with database().connect() as connection:
        cursor = connection.execute("""UPDATE test_definitions SET enabled=?,name=?,mode=?,validity_days=?,audience_type=?,departments_include_json=?,departments_exclude_json=?,indigo_logical_test_id=?,indigo_test_name=?,indigo_success_results_json=?,indigo_failed_prefixes_json=?,updated_at=? WHERE id=?""",
            (int(request.enabled),request.name.strip(),request.mode,request.validity_days,request.audience_type,json.dumps(_normalize_string_list(request.departments_include,["*"]),ensure_ascii=False),json.dumps(_normalize_string_list(request.departments_exclude),ensure_ascii=False),request.indigo_logical_test_id,request.indigo_test_name.strip() or request.name.strip(),json.dumps(_normalize_string_list(request.indigo_success_results),ensure_ascii=False),json.dumps(_normalize_string_list(request.indigo_failed_prefixes),ensure_ascii=False),_utc_now(),test_id))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Тест не найден")
    settings().templates[:] = load_test_definitions(settings(), database())
    return {"status":"ok"}


class TestBundleRequest(BaseModel):
    test: TestDefinitionRequest
    invitation: MailTemplateRequest
    reminder: MailTemplateRequest


@app.get("/api/settings/tests/{test_id}/bundle")
def get_test_bundle(test_id: str, _: Annotated[str, Depends(require_admin)]):
    db = database()
    items = load_test_definitions(settings(), db)
    test = next((x for x in items if str(x.get("id")) == test_id), None)
    if not test:
        raise HTTPException(status_code=404, detail="Тест не найден")
    ensure_mail_templates(db, items)
    with db.connect() as connection:
        rows = connection.execute("SELECT * FROM mail_templates WHERE template_id=? AND kind IN ('invitation','reminder')", (test_id,)).fetchall()
    mail = {str(r["kind"]): dict(r) for r in rows}
    return {"test": test, "invitation": mail.get("invitation"), "reminder": mail.get("reminder")}


@app.put("/api/settings/tests/{test_id}/bundle")
def save_test_bundle(test_id: str, request: TestBundleRequest, _: Annotated[str, Depends(require_admin)]):
    update_test_definition(test_id, request.test, _)
    save_mail_template("invitation", test_id, request.invitation, _)
    save_mail_template("reminder", test_id, request.reminder, _)
    return {"status": "ok"}


@app.delete("/api/settings/tests/{test_id}")
def delete_test_definition(test_id: str, _: Annotated[str, Depends(require_admin)]):
    with database().connect() as connection:
        used = connection.execute("SELECT 1 FROM notification_history WHERE template_id=? LIMIT 1", (test_id,)).fetchone() or connection.execute("SELECT 1 FROM test_assignments WHERE template_id=? LIMIT 1", (test_id,)).fetchone()
        if used:
            connection.execute("UPDATE test_definitions SET enabled=0, updated_at=? WHERE id=?", (_utc_now(), test_id))
            return {"status":"disabled","detail":"Тест уже используется, поэтому он отключен, а не удален"}
        connection.execute("DELETE FROM reviewer_templates WHERE template_id=?", (test_id,))
        connection.execute("DELETE FROM mail_templates WHERE template_id=?", (test_id,))
        cursor = connection.execute("DELETE FROM test_definitions WHERE id=?", (test_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Тест не найден")
    settings().templates[:] = load_test_definitions(settings(), database())
    return {"status":"deleted"}


SETTINGS_HTML = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Настройки</title><style>body{font-family:Arial,sans-serif;margin:24px;color:#222;background:#f5f6f8}.card,.item{background:#fff;border-radius:10px;padding:18px;box-shadow:0 1px 4px rgba(0,0,0,.08)}.card{margin-bottom:18px}.header{display:flex;justify-content:space-between;gap:18px}.actions{display:flex;flex-direction:column;gap:10px;flex:0 0 190px;min-width:190px}.link{display:flex;align-items:center;justify-content:center;width:100%;min-height:38px;box-sizing:border-box;padding:9px 13px;border:1px solid #98a2b3;border-radius:7px;color:#344054;text-decoration:none;text-align:center;font-size:13px;font-weight:600;background:#fff}.link:hover{background:#f2f4f7}h1{margin:0 0 8px;font-size:24px}h2{margin:0 0 8px;font-size:18px}.small,p{color:#667085;font-size:13px;line-height:1.45}.grid{display:grid;grid-template-columns:repeat(2,minmax(280px,1fr));gap:18px}.item{display:flex;flex-direction:column;min-height:150px;border:1px solid #e1e5ea}.item .link{margin-top:auto;align-self:flex-start;background:#175cd3;color:#fff;border-color:#175cd3}@media(max-width:800px){.grid{grid-template-columns:1fr}.header{flex-direction:column}}</style></head><body><div class="card"><div class="header"><div><h1>Настройки</h1><div class="small">Управление рассылкой, контролирующими и журналом уведомлений.</div></div><div class="actions"><a class="link" href="/admin/">Импорт участников</a><a class="link" href="/">Отчет</a></div></div></div><div class="grid"><section class="item"><h2>Общие настройки</h2><p>Интервал и максимальное количество напоминаний, уведомление контролирующих и срок хранения журнала.</p><a class="link" href="/admin/settings/general/">Открыть</a></section><section class="item"><h2>Контролирующие</h2><p>Назначение контролирующих по тестам и получение технических ошибок.</p><a class="link" href="/admin/settings/reviewers/">Открыть</a></section><section class="item"><h2>Журнал уведомлений</h2><p>Просмотр событий рассылки, уведомлений контролирующим и технических ошибок.</p><a class="link" href="/admin/journal/">Открыть</a></section><section class="item"><h2>Сопоставление логинов</h2><p>Ручное сопоставление e-mail работников с логинами Indigo.</p><a class="link" href="/admin/logins/">Открыть</a></section><section class="item"><h2>Тесты и письма</h2><p>Добавление и изменение тестов, выбор теста Indigo, участники, приглашение и напоминание.</p><a class="link" href="/admin/settings/tests/">Открыть</a></section><section class="item"><h2>Системные шаблоны</h2><p>Уведомления контролирующим и технические сообщения.</p><a class="link" href="/admin/settings/templates/">Открыть</a></section></div></body></html>"""


GENERAL_SETTINGS_HTML = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Общие настройки</title><style>body{font-family:Arial,sans-serif;margin:24px;color:#222;background:#f5f6f8}.card{background:#fff;border-radius:10px;padding:18px;margin-bottom:18px;box-shadow:0 1px 4px rgba(0,0,0,.08)}.header{display:flex;justify-content:space-between;gap:18px}.actions{display:flex;flex-direction:column;gap:10px;flex:0 0 190px;min-width:190px}.link{display:flex;align-items:center;justify-content:center;width:100%;min-height:38px;box-sizing:border-box;padding:9px 13px;border:1px solid #98a2b3;border-radius:7px;color:#344054;text-decoration:none;text-align:center;font-size:13px;font-weight:600;background:#fff}.link:hover{background:#f2f4f7}.form{max-width:760px}.row{display:grid;grid-template-columns:1fr 200px;gap:16px;align-items:center;padding:14px 0;border-bottom:1px solid #eaecf0}label{font-weight:600}.hint,.small{color:#667085;font-size:12px}input[type=number],select{width:100%;box-sizing:border-box;padding:10px;border:1px solid #ccd2da;border-radius:6px}input[type=checkbox]{width:20px;height:20px}.save{margin-top:18px;padding:10px 16px;border:0;border-radius:7px;background:#175cd3;color:#fff;font-weight:600}.message{display:none;margin-top:14px;padding:12px;border-radius:7px}.success{display:block;background:#ecfdf3;color:#05603a}.error{display:block;background:#fef3f2;color:#912018}@media(max-width:720px){.header{flex-direction:column}.row{grid-template-columns:1fr}}</style></head><body><div class="card"><div class="header"><div><h1>Общие настройки</h1><div class="small">Параметры повторных напоминаний, контролирующих и журнала.</div></div><div class="actions"><a class="link" href="/admin/settings/">Настройки</a><a class="link" href="/">Отчет</a></div></div></div><div class="card"><form id="form" class="form"><div class="row"><div><label>Отправлять повторные напоминания</label><div class="hint">Для работников, которым приглашение уже отправлено, но тест не завершен.</div></div><input id="enabled" type="checkbox"></div><div class="row"><div><label>Интервал между напоминаниями</label><div class="hint">Количество календарных дней.</div></div><input id="interval" type="number" min="1" max="365"></div><div class="row"><div><label>День автоматической отправки</label><div class="hint">Напоминания будут отправляться один раз в неделю в выбранный день.</div></div><select id="run-day"><option value="0">Понедельник</option><option value="1">Вторник</option><option value="2">Среда</option><option value="3">Четверг</option><option value="4">Пятница</option><option value="5">Суббота</option><option value="6">Воскресенье</option></select></div><div class="row"><div><label>Время автоматической отправки</label><div class="hint">Часы и минуты по часовому поясу приложения.</div></div><div style="display:flex;gap:8px"><input id="run-hour" type="number" min="0" max="23" title="Часы"><input id="run-minute" type="number" min="0" max="59" title="Минуты"></div></div><div class="row"><div><label>Максимальное количество напоминаний</label><div class="hint">После последнего напоминания работник получает статус «Игнорирует прохождение», а контролирующим отправляется одно письмо.</div></div><input id="max-reminders" type="number" min="1" max="100"></div><div class="row"><div><label>Уведомлять контролирующих</label><div class="hint">Письмо отправляется один раз после исчерпания всех напоминаний.</div></div><input id="notify-reviewers" type="checkbox"></div><div class="row"><div><label>Повтор одинаковой технической ошибки</label><div class="hint">Повторное техническое уведомление отправляется после указанного интервала. Рекомендуемое значение – 72 часа.</div></div><input id="technical-repeat" type="number" min="1" max="8760"></div><div class="row"><div><label>Срок хранения журнала</label><div class="hint">Подробный журнал очищается автоматически. Основная история отправок сохраняется.</div></div><input id="retention" type="number" min="30" max="3650"></div><button class="save">Сохранить</button><div id="message" class="message"></div></form></div><script>async function api(u,o={}){const r=await fetch(u,o);let p={};try{p=await r.json()}catch(_){p={}}if(!r.ok)throw new Error(formatError(p,r.status));return p}function formatError(p,status){const d=p&&p.detail;if(typeof d==='string')return d;if(Array.isArray(d))return d.map(x=>{const field=Array.isArray(x.loc)?x.loc[x.loc.length-1]:'';return `${field?field+': ':''}${x.msg||JSON.stringify(x)}`}).join('; ');if(d&&typeof d==='object')return d.message||d.msg||JSON.stringify(d);return p.message||p.error||`HTTP ${status}`}function msg(t,k){const n=document.getElementById('message');n.textContent=t;n.className=`message ${k}`}async function load(){const p=await api('/api/settings/reminders');enabled.checked=p.enabled;interval.value=p.interval_days;document.getElementById('max-reminders').value=p.max_reminders;document.getElementById('notify-reviewers').checked=p.notify_reviewers;retention.value=p.journal_retention_days;document.getElementById('technical-repeat').value=p.technical_repeat_hours;document.getElementById('run-hour').value=p.run_hour;document.getElementById('run-minute').value=p.run_minute;document.getElementById('run-day').value=p.run_day_of_week}form.addEventListener('submit',async e=>{e.preventDefault();try{const p=await api('/api/settings/reminders',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:enabled.checked,interval_days:Number(interval.value),max_reminders:Number(document.getElementById('max-reminders').value),notify_reviewers:document.getElementById('notify-reviewers').checked,journal_retention_days:Number(retention.value),technical_repeat_hours:Number(document.getElementById('technical-repeat').value),run_hour:Number(document.getElementById('run-hour').value),run_minute:Number(document.getElementById('run-minute').value),run_day_of_week:Number(document.getElementById('run-day').value)})});msg(`Сохранено. Максимум напоминаний: ${p.max_reminders}.`,'success')}catch(x){msg(x.message,'error')}});load().catch(x=>msg(x.message,'error'))</script></body></html>"""


TEMPLATES_HTML = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Системные шаблоны</title><style>
body{font-family:Arial,sans-serif;margin:24px;background:#f5f6f8;color:#222}.card{background:#fff;border-radius:10px;padding:18px;margin-bottom:18px;box-shadow:0 1px 4px rgba(0,0,0,.08)}.header{display:flex;justify-content:space-between;gap:18px}.actions{display:flex;flex-direction:column;gap:10px;min-width:190px}.link,.button{display:flex;align-items:center;justify-content:center;box-sizing:border-box;padding:10px 14px;border:1px solid #98a2b3;border-radius:7px;background:#fff;color:#344054;text-decoration:none;font-weight:600;cursor:pointer}.primary{background:#175cd3;color:#fff;border-color:#175cd3}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.full{grid-column:1/-1}label{display:block;font-size:12px;font-weight:600;margin:5px 0}input,textarea{width:100%;box-sizing:border-box;padding:10px;border:1px solid #ccd2da;border-radius:6px}textarea{min-height:240px;font-family:Consolas,monospace}.small{font-size:12px;color:#667085}.message{display:none;padding:12px}.success{display:block;background:#ecfdf3}.error{display:block;background:#fef3f2}@media(max-width:800px){.grid{grid-template-columns:1fr}.full{grid-column:auto}.header{flex-direction:column}}
</style></head><body><div class="card"><div class="header"><div><h1>Системные шаблоны</h1><div class="small">Общие письма контролирующему и технические уведомления.</div></div><div class="actions"><a class="link" href="/admin/settings/">Настройки</a><a class="link" href="/">Отчет</a></div></div></div><div id="forms"></div><div id="message" class="message"></div><script>
let items=[];const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));async function api(u,o={}){const r=await fetch(u,o);let p={};try{p=await r.json()}catch(_){p={}}if(!r.ok)throw new Error(typeof p.detail==='string'?p.detail:`HTTP ${r.status}`);return p}function msg(t,k){message.textContent=t;message.className=`message ${k}`}function title(k){return k==='reviewer'?'Уведомление контролирующему':'Техническое уведомление'}function render(){forms.innerHTML=items.filter(x=>['reviewer','technical'].includes(x.kind)).map((x,i)=>`<section class="card" data-key="${x.kind}"><h2>${title(x.kind)}</h2><div class="grid"><label><input type="checkbox" data-f="enabled" ${x.enabled?'checked':''}> Использовать шаблон</label><div></div><div class="full"><label>Тема</label><input data-f="subject" value="${esc(x.subject)}"></div><div class="full"><label>HTML-текст</label><textarea data-f="body_html">${esc(x.body_html)}</textarea></div><div><label>Тестовый e-mail</label><input type="email" data-f="recipient" placeholder="user@example.ru"></div><div style="display:flex;gap:10px;align-items:end"><button class="button" data-test>Отправить тестовое</button><button class="button primary" data-save>Сохранить</button></div></div></section>`).join('');document.querySelectorAll('[data-save]').forEach(b=>b.onclick=()=>save(b.closest('section')));document.querySelectorAll('[data-test]').forEach(b=>b.onclick=()=>test(b.closest('section')))}function val(sec,f){const e=sec.querySelector(`[data-f="${f}"]`);return f==='enabled'?e.checked:e.value}async function save(sec){const k=sec.dataset.key;await api(`/api/settings/templates/${k}/*`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({subject:val(sec,'subject'),body_html:val(sec,'body_html'),enabled:val(sec,'enabled')})});msg('Шаблон сохранен.','success')}async function test(sec){try{const k=sec.dataset.key;const r=await api('/api/settings/templates/test-email',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({recipient:val(sec,'recipient'),kind:k,template_id:'*',subject:val(sec,'subject'),body_html:val(sec,'body_html')})});const source=r.data_source==='demo'?'использованы демонстрационные данные':r.data_source==='real_employee'?'использованы данные реального работника':'использованы реальные данные из базы';msg(`Тестовое письмо отправлено – ${source}${r.data_count?` (${r.data_count})`:''}.`,'success')}catch(e){msg(e.message,'error')}}async function load(){items=(await api('/api/settings/templates')).items;render()}load().catch(e=>msg(e.message,'error'));
</script></body></html>"""

TESTS_HTML = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Тесты и письма</title><style>
body{font-family:Arial,sans-serif;margin:24px;background:#f5f6f8;color:#222}.card{background:#fff;border-radius:10px;padding:18px;margin-bottom:18px;box-shadow:0 1px 4px rgba(0,0,0,.08)}.header{display:flex;justify-content:space-between;gap:18px}.actions{display:flex;flex-direction:column;gap:10px;min-width:190px}.link,.button{display:inline-flex;align-items:center;justify-content:center;box-sizing:border-box;padding:10px 14px;border:1px solid #98a2b3;border-radius:7px;background:#fff;color:#344054;text-decoration:none;font-weight:600;cursor:pointer}.primary{background:#175cd3;color:#fff;border-color:#175cd3}.danger{color:#b42318;border-color:#f04438}.layout{display:grid;grid-template-columns:280px minmax(0,1fr);gap:18px}.list button{display:block;width:100%;text-align:left;margin-bottom:8px;padding:11px;border:1px solid #d0d5dd;border-radius:7px;background:#fff}.list button.active{border-color:#175cd3;background:#eff4ff}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.full{grid-column:1/-1}label{display:block;font-size:12px;font-weight:600;margin:5px 0}input,select,textarea{width:100%;box-sizing:border-box;padding:10px;border:1px solid #ccd2da;border-radius:6px}textarea{min-height:110px}.htmlbox{min-height:260px;font-family:Consolas,monospace}.small{font-size:12px;color:#667085}.section{border-top:1px solid #e4e7ec;margin-top:20px;padding-top:16px}.message{display:none;padding:12px;margin-bottom:15px}.success{display:block;background:#ecfdf3}.error{display:block;background:#fef3f2}.row-actions{display:flex;gap:10px;flex-wrap:wrap}.badge{font-size:11px;padding:3px 7px;border-radius:10px;background:#f2f4f7}@media(max-width:900px){.layout{grid-template-columns:1fr}.grid{grid-template-columns:1fr}.full{grid-column:auto}.header{flex-direction:column}}
</style></head><body><div class="card"><div class="header"><div><h1>Тесты и письма</h1><div class="small">Настройки теста, связь с Indigo, первоначальное приглашение и напоминание на одной странице.</div></div><div class="actions"><a class="link" href="/admin/settings/">Настройки</a><a class="link" href="/">Отчет</a></div></div></div><div id="message" class="message"></div><div class="layout"><aside class="card"><div class="row-actions"><button id="add" class="button primary">Добавить тест</button></div><h3>Тесты</h3><div id="testList" class="list"></div><div class="small" id="catalogStatus"></div><button id="refreshCatalog" class="button" style="width:100%;margin-top:12px">Обновить список Indigo</button></aside><main id="editor" class="card" hidden><h2 id="formTitle"></h2><div class="grid"><div><label>Внутренний ID</label><input id="id" placeholder="antiterror"></div><div><label>Название</label><input id="name"></div><label><input id="enabled" type="checkbox" style="width:auto"> Тест включен</label><div><label>Режим</label><select id="mode"><option value="once">Однократно</option><option value="periodic">Периодически</option></select></div><div><label>Срок действия результата, дней</label><input id="validity" type="number" min="1" placeholder="Без срока"></div><div><label>Участники</label><select id="audience"><option value="all">Все действующие сотрудники</option><option value="explicit_list">Выборочный список</option></select></div><div><label>Подразделения включить, по одному в строке</label><textarea id="include"></textarea></div><div><label>Подразделения исключить</label><textarea id="exclude"></textarea></div></div><section class="section"><div class="header"><div><h3>Связь с Indigo</h3><div class="small">Список обновляется при открытии раздела. При недоступности Indigo используется последний кеш.</div></div></div><div class="grid"><div class="full"><label>Тест в Indigo</label><select id="indigoSelect"><option value="">Не выбран</option></select></div><div><label>Logical test ID</label><input id="logical" type="number" readonly></div><div><label>Название теста в Indigo</label><input id="indigoName" readonly></div><div><label>Успешные результаты</label><textarea id="success"></textarea></div><div><label>Префиксы неуспешных результатов</label><textarea id="failed"></textarea></div></div></section><section class="section"><h3>Первоначальное приглашение</h3><div class="grid"><label><input id="invEnabled" type="checkbox" style="width:auto"> Использовать шаблон</label><div></div><div class="full"><label>Тема</label><input id="invSubject"></div><div class="full"><label>HTML-текст</label><textarea id="invBody" class="htmlbox"></textarea></div><div><label>Тестовый e-mail</label><input id="invEmail" type="email"></div><div><button id="invTest" class="button">Отправить тестовое письмо</button></div></div></section><section class="section"><h3>Напоминание</h3><div class="grid"><label><input id="remEnabled" type="checkbox" style="width:auto"> Использовать шаблон</label><div></div><div class="full"><label>Тема</label><input id="remSubject"></div><div class="full"><label>HTML-текст</label><textarea id="remBody" class="htmlbox"></textarea></div><div><label>Тестовый e-mail</label><input id="remEmail" type="email"></div><div><button id="remTest" class="button">Отправить тестовое письмо</button></div></div></section><section class="section row-actions"><button id="save" class="button primary">Сохранить настройки теста</button><button id="remove" class="button danger">Удалить / отключить</button><button id="cancel" class="button">Отмена</button></section></main></div><script>
let tests=[],catalog=[],current=null,creating=false;const $=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));async function api(u,o={}){const r=await fetch(u,o);let p={};try{p=await r.json()}catch(_){p={}}if(!r.ok)throw new Error(typeof p.detail==='string'?p.detail:`HTTP ${r.status}`);return p}function msg(t,k){$('message').textContent=t;$('message').className=`message ${k}`}const lines=v=>String(v||'').split(/\r?\n|,/).map(x=>x.trim()).filter(Boolean);function renderList(){$('testList').innerHTML=tests.map(t=>`<button data-id="${esc(t.id)}" class="${current===t.id?'active':''}"><strong>${esc(t.name)}</strong><br><span class="small">${esc(t.id)} · ${t.enabled?'активен':'отключен'}</span></button>`).join('');document.querySelectorAll('[data-id]').forEach(b=>b.onclick=()=>openTest(b.dataset.id))}function renderCatalog(selectedId,selectedName){$('indigoSelect').innerHTML='<option value="">Не выбран</option>'+catalog.map((x,i)=>`<option value="${i}" ${String(x.logical_test_id)===String(selectedId)&&x.test_name===selectedName?'selected':''}>${esc(x.test_name)} · ID ${x.logical_test_id} (${x.source_rows})</option>`).join('');if(selectedId&&!catalog.some(x=>String(x.logical_test_id)===String(selectedId)&&x.test_name===selectedName))$('indigoSelect').insertAdjacentHTML('beforeend',`<option selected value="legacy">${esc(selectedName||'Тест')} · ID ${esc(selectedId)} (из настроек)</option>`)}$('indigoSelect').onchange=()=>{const i=$('indigoSelect').value;if(i===''||i==='legacy')return;const x=catalog[Number(i)];$('logical').value=x.logical_test_id;$('indigoName').value=x.test_name};function testPayload(){return {id:$('id').value.trim(),enabled:$('enabled').checked,name:$('name').value.trim(),mode:$('mode').value,validity_days:$('validity').value?Number($('validity').value):null,audience_type:$('audience').value,departments_include:lines($('include').value),departments_exclude:lines($('exclude').value),indigo_logical_test_id:$('logical').value?Number($('logical').value):null,indigo_test_name:$('indigoName').value.trim(),indigo_success_results:lines($('success').value),indigo_failed_prefixes:lines($('failed').value)}}function mail(prefix){return {subject:$(prefix+'Subject').value,body_html:$(prefix+'Body').value,enabled:$(prefix+'Enabled').checked}}function fill(b){const t=b.test,ind=t.indigo||{};current=t.id;creating=false;$('editor').hidden=false;$('formTitle').textContent=`Изменить тест «${t.name}»`;$('id').value=t.id;$('id').readOnly=true;$('name').value=t.name;$('enabled').checked=!!t.enabled;$('mode').value=t.mode||'once';$('validity').value=t.validity_days??'';$('audience').value=t.audience?'explicit_list':'all';$('include').value=(t.departments?.include||['*']).join('\n');$('exclude').value=(t.departments?.exclude||[]).join('\n');$('logical').value=ind.logical_test_id||'';$('indigoName').value=ind.test_name||'';$('success').value=(ind.success_results||[]).join('\n');$('failed').value=(ind.failed_result_prefixes||[]).join('\n');renderCatalog(ind.logical_test_id,ind.test_name);for(const [p,x] of [['inv',b.invitation],['rem',b.reminder]]){$(p+'Enabled').checked=!!x?.enabled;$(p+'Subject').value=x?.subject||'';$(p+'Body').value=x?.body_html||''}renderList();scrollTo(0,0)}async function openTest(id){const b=await api(`/api/settings/tests/${encodeURIComponent(id)}/bundle`);fill(b)}function addNew(){creating=true;current=null;$('editor').hidden=false;$('formTitle').textContent='Добавить тест';['id','name','validity','include','exclude','logical','indigoName','success','failed','invSubject','invBody','remSubject','remBody'].forEach(x=>$(x).value='');$('id').readOnly=false;$('enabled').checked=true;$('mode').value='once';$('audience').value='all';$('include').value='*';$('success').value='Отлично\nХорошо';$('failed').value='Требуется повторный';$('invEnabled').checked=true;$('remEnabled').checked=true;renderCatalog();renderList()}async function loadCatalog(force=false){const p=await api(force?'/api/settings/indigo-tests/refresh':'/api/settings/indigo-tests',{method:force?'POST':'GET'});catalog=p.items||[];$('catalogStatus').textContent=p.refreshed_at?`Кеш Indigo: ${new Date(p.refreshed_at).toLocaleString('ru-RU')}${p.error?' · ошибка обновления: '+p.error:''}`:(p.error||'Кеш Indigo пока пуст');if(!$('editor').hidden)renderCatalog($('logical').value,$('indigoName').value)}async function load(){tests=(await api('/api/settings/tests')).items;renderList();await loadCatalog(false)}$('add').onclick=addNew;$('refreshCatalog').onclick=()=>loadCatalog(true).then(()=>msg('Список тестов Indigo обновлен.','success')).catch(e=>msg(e.message,'error'));$('save').onclick=async()=>{try{const t=testPayload();if(creating){const r=await api('/api/settings/tests',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(t)});await api(`/api/settings/templates/invitation/${encodeURIComponent(r.id)}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(mail('inv'))});await api(`/api/settings/templates/reminder/${encodeURIComponent(r.id)}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(mail('rem'))});current=r.id}else await api(`/api/settings/tests/${encodeURIComponent(current)}/bundle`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({test:t,invitation:mail('inv'),reminder:mail('rem')})});tests=(await api('/api/settings/tests')).items;msg('Настройки теста и письма сохранены.','success');await openTest(current)}catch(e){msg(e.message,'error')}};$('remove').onclick=async()=>{if(!current||!confirm('Удалить тест? Используемый тест будет отключен.'))return;const r=await api(`/api/settings/tests/${encodeURIComponent(current)}`,{method:'DELETE'});msg(r.detail||'Готово.','success');$('editor').hidden=true;current=null;tests=(await api('/api/settings/tests')).items;renderList()};$('cancel').onclick=()=>{$('editor').hidden=true;current=null;renderList()};async function sendTest(kind,prefix){try{await api('/api/settings/templates/test-email',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({recipient:$(prefix+'Email').value.trim(),kind,template_id:current||$('id').value.trim(),...mail(prefix)})});msg('Тестовое письмо отправлено.','success')}catch(e){msg(e.message,'error')}}$('invTest').onclick=()=>sendTest('invitation','inv');$('remTest').onclick=()=>sendTest('reminder','rem');load().catch(e=>msg(e.message,'error'));
</script></body></html>"""

REVIEWERS_HTML = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Контролирующие</title><style>body{font-family:Arial,sans-serif;margin:24px;color:#222;background:#f5f6f8}.card{background:#fff;border-radius:10px;padding:18px;margin-bottom:18px;box-shadow:0 1px 4px rgba(0,0,0,.08)}.header{display:flex;justify-content:space-between;gap:18px}.actions{display:flex;flex-direction:column;gap:10px;flex:0 0 190px;min-width:190px}.link{display:flex;align-items:center;justify-content:center;width:100%;min-height:38px;box-sizing:border-box;padding:9px 13px;border:1px solid #98a2b3;border-radius:7px;color:#344054;text-decoration:none;text-align:center;font-size:13px;font-weight:600;background:#fff}.link:hover{background:#f2f4f7}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.full{grid-column:1/-1}label{display:block;margin:5px 0;font-size:12px;font-weight:600}input{box-sizing:border-box;padding:10px;border:1px solid #ccd2da;border-radius:6px;width:100%}.checks{display:flex;gap:14px;flex-wrap:wrap;padding:10px;border:1px solid #eaecf0;border-radius:7px}.checks label{font-weight:400}.checks input{width:auto}.button{padding:10px 14px;border:0;border-radius:7px;background:#175cd3;color:#fff;font-weight:600}.danger,.edit{padding:7px 10px;border:1px solid #98a2b3;border-radius:6px;background:#fff;cursor:pointer}.danger{color:#b42318;border-color:#f04438}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:9px 8px;border-bottom:1px solid #e1e5ea;text-align:left}.small{color:#667085;font-size:12px}.message{display:none;padding:12px;margin-top:12px}.success{display:block;background:#ecfdf3}.error{display:block;background:#fef3f2}@media(max-width:800px){.grid{grid-template-columns:1fr}.full{grid-column:auto}.header{flex-direction:column;gap:12px}}</style></head><body><div class="card"><div class="header"><div><h1>Контролирующие</h1><div class="small">Внутри системы сохраняется термин «проверяющие».</div></div><div class="actions"><a class="link" href="/admin/settings/">Настройки</a><a class="link" href="/">Отчет</a></div></div></div><div class="card"><h2 id="title">Добавить контролирующего</h2><form id="form" class="grid"><div><label>Наименование / ФИО</label><input id="name" required placeholder="Служба безопасности или Иванов И.И."></div><div><label>E-mail</label><input id="email" type="email" required></div><div class="full"><label>Контролируемые тесты</label><div id="templates" class="checks"></div></div><div class="full checks"><label><input id="technical" type="checkbox"> Получает технические ошибки</label><label><input id="enabled" type="checkbox" checked> Активен</label></div><div><button class="button">Сохранить</button> <button id="cancel" type="button" class="edit" hidden>Отмена</button></div></form><div id="message" class="message"></div></div><div class="card"><h2>Список контролирующих</h2><div class="table-wrap"><table><thead><tr><th>Наименование / ФИО</th><th>E-mail</th><th>Тесты</th><th>Технические ошибки</th><th>Статус</th><th></th></tr></thead><tbody id="body"></tbody></table></div></div><script>let items=[],templates=[],editId=null;const nameInput=document.getElementById('name'),emailInput=document.getElementById('email'),enabledInput=document.getElementById('enabled'),technicalInput=document.getElementById('technical'),formElement=document.getElementById('form'),titleElement=document.getElementById('title'),cancelButton=document.getElementById('cancel'),messageElement=document.getElementById('message'),bodyElement=document.getElementById('body');const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));async function api(u,o={}){const r=await fetch(u,o);let p={};try{p=await r.json()}catch(_){p={}}if(!r.ok)throw new Error(formatError(p,r.status));return p}function formatError(p,status){const d=p&&p.detail;if(typeof d==='string')return d;if(Array.isArray(d))return d.map(x=>{const field=Array.isArray(x.loc)?x.loc[x.loc.length-1]:'';return `${field?field+': ':''}${x.msg||JSON.stringify(x)}`}).join('; ');if(d&&typeof d==='object')return d.message||d.msg||JSON.stringify(d);return p.message||p.error||`HTTP ${status}`}function msg(t,k){messageElement.textContent=t;messageElement.className=`message ${k}`}function renderTemplates(selected=[]){document.getElementById('templates').innerHTML=templates.map(t=>`<label><input type="checkbox" data-template="${esc(t.id)}" ${selected.includes(String(t.id))?'checked':''}> ${esc(t.name)}</label>`).join('')}function selected(){return [...document.querySelectorAll('[data-template]:checked')].map(x=>x.dataset.template)}function reset(){editId=null;formElement.reset();enabledInput.checked=true;titleElement.textContent='Добавить контролирующего';cancelButton.hidden=true;renderTemplates([])}function render(){bodyElement.innerHTML=items.map(x=>`<tr><td>${esc(x.name)}</td><td>${esc(x.email)}</td><td>${esc(x.template_ids.map(id=>(templates.find(t=>String(t.id)===String(id))||{name:id}).name).join(', ')||'–')}</td><td>${x.receives_technical_errors?'Да':'Нет'}</td><td>${x.enabled?'Активен':'Отключен'}</td><td><button class="edit" data-edit="${x.id}">Изменить</button> <button class="danger" data-del="${x.id}">Удалить</button></td></tr>`).join('');document.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>{const x=items.find(i=>i.id==b.dataset.edit);editId=x.id;nameInput.value=x.name;emailInput.value=x.email;enabledInput.checked=!!x.enabled;technicalInput.checked=!!x.receives_technical_errors;renderTemplates(x.template_ids.map(String));titleElement.textContent='Изменить контролирующего';cancelButton.hidden=false;scrollTo(0,0)});document.querySelectorAll('[data-del]').forEach(b=>b.onclick=async()=>{if(confirm('Удалить контролирующего?')){await api(`/api/reviewers/${b.dataset.del}`,{method:'DELETE'});await load()}})}async function load(){const p=await api('/api/reviewers');items=p.items;templates=p.templates;renderTemplates([]);render()}formElement.onsubmit=async e=>{e.preventDefault();try{await api(editId?`/api/reviewers/${editId}`:'/api/reviewers',{method:editId?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:nameInput.value.trim(),email:emailInput.value.trim(),enabled:enabledInput.checked,receives_technical_errors:technicalInput.checked,template_ids:selected()})});reset();await load();msg('Контролирующий сохранен.','success')}catch(x){msg(x.message,'error')}};cancelButton.onclick=reset;load().catch(x=>msg(x.message,'error'))</script></body></html>"""


JOURNAL_HTML = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Журнал уведомлений</title><style>body{font-family:Arial,sans-serif;margin:24px;background:#f5f6f8;color:#222}.card{background:#fff;border-radius:10px;padding:18px;margin-bottom:18px;box-shadow:0 1px 4px rgba(0,0,0,.08)}.header{display:flex;justify-content:space-between;gap:18px}.actions{display:flex;flex-direction:column;gap:10px;flex:0 0 190px;min-width:190px}.link,button{display:flex;align-items:center;justify-content:center;width:100%;min-height:38px;box-sizing:border-box;padding:9px 13px;border:1px solid #98a2b3;border-radius:7px;background:#fff;color:#344054;text-decoration:none;text-align:center;font-size:13px;font-weight:600;cursor:pointer}.link:hover,button:hover{background:#f2f4f7}.filters{display:flex;gap:12px;margin-bottom:12px}.filters input,.filters select{padding:10px;border:1px solid #ccd2da;border-radius:6px}.filters input{flex:1}.table-wrap{overflow:auto;max-height:70vh}table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:8px;border-bottom:1px solid #e1e5ea;text-align:left;vertical-align:top}th{position:sticky;top:0;background:#f0f2f5}.small{color:#667085;font-size:12px}@media(max-width:700px){.header{flex-direction:column}.filters{flex-direction:column}}</style></head><body><div class="card"><div class="header"><div><h1>Журнал уведомлений</h1><div class="small">Подробные события хранятся в течение срока, заданного в общих настройках.</div></div><div class="actions"><a class="link" href="/api/journal/export.xlsx">Экспорт XLSX</a><a class="link" href="/admin/settings/">Настройки</a><a class="link" href="/">Отчет</a></div></div></div><div class="card"><div class="filters"><input id="search" placeholder="ФИО, e-mail, тест, получатель или подробности"><select id="status"><option value="">Все статусы</option><option value="sent">Отправлено</option><option value="error">Ошибка</option></select></div><p id="count" class="small"></p><div class="table-wrap"><table><thead><tr><th>Дата</th><th>Событие</th><th>ФИО</th><th>E-mail</th><th>Подразделение</th><th>Тест</th><th>№</th><th>Получатель</th><th>Статус</th><th>Подробности</th></tr></thead><tbody id="body"></tbody></table></div></div><script>let items=[];const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));function render(){const q=search.value.toLowerCase().trim(),st=status.value;const a=items.filter(x=>(!st||x.status===st)&&(!q||Object.values(x).join(' ').toLowerCase().includes(q)));count.textContent=`Показано записей: ${a.length}`;body.innerHTML=a.map(x=>`<tr><td>${esc(new Date(x.created_at).toLocaleString('ru-RU'))}</td><td>${esc(x.event_type)}</td><td>${esc(x.fio)}</td><td>${esc(x.email)}</td><td>${esc(x.department)}</td><td>${esc(x.template_name)}</td><td>${esc(x.reminder_number)}</td><td>${esc(x.recipient)}</td><td>${esc(x.status)}</td><td>${esc(x.details)}</td></tr>`).join('')}async function load(){const r=await fetch('/api/journal?limit=5000'),p=await r.json();items=p.items;render()}search.oninput=render;status.onchange=render;load()</script></body></html>"""


LOGIN_OVERRIDES_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Сопоставление e-mail и логина</title>
<style>
body { font-family: Arial, sans-serif; margin: 24px; color: #222; background: #f5f6f8; }
.card { background: #fff; border-radius: 10px; padding: 18px; margin-bottom: 18px; box-shadow: 0 1px 4px rgba(0,0,0,.08); }
.page-header {
  display: flex;
  align-items: stretch;
  gap: 0;
}

.page-header-main {
  flex: 1 1 auto;
  min-width: 360px;
  padding-right: 18px;
  box-sizing: border-box;
}

.page-header-template {
  flex: 0 0 190px;
  min-width: 190px;
  padding: 0 18px;
  border-left: 2px solid #e5e7eb;
  box-sizing: border-box;
}

.page-header-actions {
  flex: 0 0 190px;
  min-width: 190px;
  padding-left: 18px;
  border-left: 2px solid #e5e7eb;
  display: flex;
  flex-direction: column;
  gap: 10px;
  box-sizing: border-box;
}

.header-button {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  min-height: 38px;
  box-sizing: border-box;
  padding: 9px 13px;
  border: 1px solid #98a2b3;
  border-radius: 7px;
  color: #344054;
  text-decoration: none;
  text-align: center;
  font-size: 13px;
  font-weight: 600;
  background: #fff;
}

.header-button:hover {
  background: #f2f4f7;
}

.template-button:hover {
  color: #176b36;
  border-color: #32a852;
  background: #f0fdf4;
}

@media (max-width: 900px) {
  .page-header {
    flex-wrap: wrap;
    gap: 14px;
  }

  .page-header-main {
    flex: 1 1 100%;
    min-width: 100%;
    padding-right: 0;
  }

  .page-header-template,
  .page-header-actions {
    flex: 1 1 220px;
    min-width: 220px;
    padding: 14px 0 0;
    border-left: 0;
    border-top: 2px solid #e5e7eb;
  }
}
.header-line { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
h1 { margin: 0 0 8px; font-size: 24px; }
h2 { margin-top: 0; font-size: 18px; }
.actions { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.back-link { display: inline-block; padding: 9px 13px; border: 1px solid #98a2b3; border-radius: 7px; color: #344054; text-decoration: none; font-size: 13px; font-weight: 600; background: #fff; }
.form-grid { display: grid; grid-template-columns: minmax(280px, 2fr) minmax(220px, 1fr) auto; gap: 12px; align-items: end; }
label { display: block; margin-bottom: 5px; color: #475467; font-size: 12px; font-weight: 600; }
input, button { box-sizing: border-box; padding: 10px; border-radius: 6px; font: inherit; }
input { width: 100%; border: 1px solid #ccd2da; background: #fff; }
button { border: 0; background: #175cd3; color: #fff; font-weight: 600; cursor: pointer; }
button:hover { background: #1849a9; }
button.danger { background: #fff; color: #b42318; border: 1px solid #f04438; padding: 7px 10px; }
button.danger:hover { background: #fef3f2; }
.filters { display: flex; gap: 12px; margin: 12px 0; }
.table-wrap { overflow: auto; border: 1px solid #eaecf0; border-radius: 8px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 9px 8px; border-bottom: 1px solid #e1e5ea; text-align: left; vertical-align: middle; }
th { background: #f0f2f5; }
.small { color: #667085; font-size: 12px; }
.message { margin-top: 12px; padding: 12px; border-radius: 7px; display: none; }
.message.error { display: block; background: #fef3f2; color: #912018; }
.message.success { display: block; background: #ecfdf3; color: #05603a; }
@media (max-width: 850px) { .form-grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div class="card">
  <div class="page-header">

    <div class="page-header-main">
      <h1>Сопоставление e-mail и логина</h1>

      <div class="small">
        Используется только для работников, у которых логин Indigo
        не совпадает с частью e-mail до знака @.
        Изменения применяются сразу.
      </div>
    </div>

    <div class="page-header-actions">
      <a
        class="header-button"
        href="/admin/settings/"
      >
        ⚙ Настройки
      </a>

      <a
        class="header-button"
        href="/"
      >
        Вернуться к отчету
      </a>
    </div>

  </div>
</div>
<div class="card">
  <h2>Добавить или изменить сопоставление</h2>
  <form id="mapping-form" class="form-grid">
    <div>
      <label for="email">E-mail сотрудника</label>
      <input id="email" name="email" type="email" required placeholder="petrov.pp@example.ru">
    </div>
    <div>
      <label for="login">Логин Indigo</label>
      <input id="login" name="login" required placeholder="petrov.p">
    </div>
    <button type="submit">Сохранить</button>
  </form>
  <div id="message" class="message"></div>
</div>
<div class="card">
  <h2>Действующие сопоставления</h2>
  <div class="filters">
    <input id="search" type="search" placeholder="Поиск по e-mail, логину или ФИО">
  </div>
  <p id="count" class="small"></p>
  <div class="table-wrap">
    <table>
      <thead>
          <tr>
            <th>Работник</th>
            <th>E-mail</th>
            <th>Логин</th>
            <th>Изменено</th>
            <th></th>
          </tr>
        </thead>
      <tbody id="mapping-body"></tbody>
    </table>
  </div>
</div>
<script>
let items = [];
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
}
async function api(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
  return payload;
}
function showMessage(text, kind) {
  const node = document.getElementById('message');
  node.textContent = text;
  node.className = `message ${kind}`;
}
function formatDate(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU');
}
function render() {
  const query = document
    .getElementById('search')
    .value
    .trim()
    .toLowerCase();

  const filtered = items
    .filter(item => {
      const searchText = [
        item.fio,
        item.email,
        item.login
      ]
        .join(' ')
        .toLowerCase();

      return !query || searchText.includes(query);
    })
    .sort((left, right) => {
      const leftMissing =
        !String(left.fio || '').trim();

      const rightMissing =
        !String(right.fio || '').trim();

      /*
       * Сопоставления, для которых работник не найден
       * в текущей базе, всегда показываем первыми.
       */
      if (leftMissing !== rightMissing) {
        return leftMissing ? -1 : 1;
      }

      /*
       * Остальных сортируем по ФИО.
       */
      const fioComparison = String(
        left.fio || ''
      ).localeCompare(
        String(right.fio || ''),
        'ru',
        {
          sensitivity: 'base'
        }
      );

      if (fioComparison !== 0) {
        return fioComparison;
      }

      /*
       * При одинаковом ФИО или отсутствии ФИО
       * сортируем по адресу электронной почты.
       */
      return String(
        left.email || ''
      ).localeCompare(
        String(right.email || ''),
        'ru',
        {
          sensitivity: 'base'
        }
      );
    });

  document.getElementById(
    'count'
  ).textContent =
    `Показано сопоставлений: ${filtered.length}`;

  document.getElementById(
    'mapping-body'
  ).innerHTML = filtered.map(item => {
    const worker = item.fio
      ? escapeHtml(item.fio)
      : 'Не найден в текущей базе';

    const inactive = (
      item.fio && item.active === false
    )
      ? '<div class="small">Работник неактивен</div>'
      : '';

    return `
      <tr>
        <td>
          ${worker}
          ${inactive}
        </td>

        <td>
          ${escapeHtml(item.email)}
        </td>

        <td>
          ${escapeHtml(item.login)}
        </td>

        <td>
          ${escapeHtml(formatDate(item.updated_at))}
        </td>

        <td>
          <button
            class="danger"
            type="button"
            data-email="${escapeHtml(item.email)}"
          >
            Удалить
          </button>
        </td>
      </tr>
    `;
  }).join('');

  document
    .querySelectorAll('button[data-email]')
    .forEach(button => {
      button.addEventListener(
        'click',
        async () => {
          if (
            !confirm(
              `Удалить сопоставление для ${button.dataset.email}?`
            )
          ) {
            return;
          }

          try {
            await api(
              `/api/login-overrides/${
                encodeURIComponent(
                  button.dataset.email
                )
              }`,
              {
                method: 'DELETE'
              }
            );

            await load();

            showMessage(
              'Сопоставление удалено. Для работника снова используется часть e-mail до знака @.',
              'success'
            );
          } catch (error) {
            showMessage(
              error.message,
              'error'
            );
          }
        }
      );
    });
}
async function load() {
  const payload = await api('/api/login-overrides');
  items = payload.items;
  render();
}
document.getElementById('mapping-form').addEventListener('submit', async event => {
  event.preventDefault();
  const data = new FormData(event.target);
  try {
    const item = await api('/api/login-overrides', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email: data.get('email'), login: data.get('login')})
    });
    event.target.reset();
    await load();
    showMessage(item.fio ? `Сопоставление сохранено для: ${item.fio}` : 'Сопоставление сохранено. Сотрудник с таким e-mail пока не найден в текущей базе.', 'success');
  } catch (error) { showMessage(error.message, 'error'); }
});
document.getElementById('search').addEventListener('input', render);
load().catch(error => showMessage(error.message, 'error'));
</script>
</body>
</html>"""


ADMIN_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Импорт списков участников</title>
<style>
body { font-family: Arial, sans-serif; margin: 24px; color: #222; background: #f5f6f8; }
.card { background: #fff; border-radius: 10px; padding: 18px; margin-bottom: 18px; box-shadow: 0 1px 4px rgba(0,0,0,.08); }
.page-header {
  display: flex;
  align-items: stretch;
  gap: 0;
}

.page-header-main {
  flex: 1 1 auto;
  min-width: 360px;
  padding-right: 18px;
  box-sizing: border-box;
}

.page-header-template {
  flex: 0 0 190px;
  min-width: 190px;
  padding: 0 18px;
  border-left: 2px solid #e5e7eb;
  box-sizing: border-box;
}

.page-header-actions {
  flex: 0 0 190px;
  min-width: 190px;
  padding-left: 18px;
  border-left: 2px solid #e5e7eb;
  display: flex;
  flex-direction: column;
  gap: 10px;
  box-sizing: border-box;
}

.header-button {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  min-height: 38px;
  box-sizing: border-box;
  padding: 9px 13px;
  border: 1px solid #98a2b3;
  border-radius: 7px;
  color: #344054;
  text-decoration: none;
  text-align: center;
  font-size: 13px;
  font-weight: 600;
  background: #fff;
}

.header-button:hover {
  background: #f2f4f7;
}

.template-button:hover {
  color: #176b36;
  border-color: #32a852;
  background: #f0fdf4;
}

@media (max-width: 900px) {
  .page-header {
    flex-wrap: wrap;
    gap: 14px;
  }

  .page-header-main {
    flex: 1 1 100%;
    min-width: 100%;
    padding-right: 0;
  }

  .page-header-template,
  .page-header-actions {
    flex: 1 1 220px;
    min-width: 220px;
    padding: 14px 0 0;
    border-left: 0;
    border-top: 2px solid #e5e7eb;
  }
}
.header-line { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
h1 { margin: 0 0 8px; font-size: 24px; }
h2 { margin-top: 0; font-size: 18px; }
.back-link { display: inline-block; padding: 9px 13px; border: 1px solid #98a2b3; border-radius: 7px; color: #344054; text-decoration: none; font-size: 13px; font-weight: 600; }
.form-grid { display: grid; grid-template-columns: minmax(240px, 1fr) minmax(260px, 2fr) auto; gap: 12px; align-items: end; }
label { display: block; margin-bottom: 5px; color: #475467; font-size: 12px; font-weight: 600; }
input, select, button { box-sizing: border-box; padding: 10px; border-radius: 6px; font: inherit; }
input, select { width: 100%; border: 1px solid #ccd2da; background: #fff; }
button { border: 0; background: #175cd3; color: #fff; font-weight: 600; cursor: pointer; }
button:hover { background: #1849a9; }
button:disabled { cursor: not-allowed; opacity: .55; }
button.secondary { background: #fff; color: #344054; border: 1px solid #98a2b3; }
.actions { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.summary { display: flex; gap: 12px; flex-wrap: wrap; margin: 12px 0; }
.metric { min-width: 150px; background: #eef2f7; padding: 12px; border-radius: 8px; }
.metric strong { display: block; font-size: 22px; }
.filters { display: flex; gap: 12px; flex-wrap: wrap; margin: 14px 0; }
.filters > div { min-width: 220px; flex: 1; }
.table-wrap { overflow: auto; max-height: 62vh; border: 1px solid #eaecf0; border-radius: 8px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 9px 8px; border-bottom: 1px solid #e1e5ea; text-align: left; vertical-align: top; }
th { position: sticky; top: 0; background: #f0f2f5; z-index: 1; }
tr.ready { background: #f6fef9; }
tr.warning { background: #fffaeb; }
tr.error { background: #fff5f5; }
.status { font-weight: 600; }
.status-ready, .status-imported { color: #176b36; }
.status-warning { color: #8a5b00; }
.status-error { color: #a21d1d; }
.small { color: #667085; font-size: 12px; }
.message { margin-top: 12px; padding: 12px; border-radius: 7px; display: none; }
.message.error { display: block; background: #fef3f2; color: #912018; }
.message.success { display: block; background: #ecfdf3; color: #05603a; }
.hidden { display: none !important; }
@media (max-width: 900px) { .form-grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div class="card">
  <div class="page-header">

    <div class="page-header-main">
      <h1>Импорт списков участников</h1>

      <div class="small">
        Файл используется только для отбора по email.
        ФИО, подразделение, должность и логин берутся
        из основной ежедневной базы.
      </div>
    </div>

    <div class="page-header-template">
      <a
        class="header-button template-button"
        href="/api/audience/template.xlsx"
      >
        Скачать шаблон
      </a>
    </div>

    <div class="page-header-actions">
      <a
        class="header-button"
        href="/admin/settings/"
      >
        ⚙ Настройки
      </a>

      <a
        class="header-button"
        href="/"
      >
        Вернуться к отчету
      </a>
    </div>

  </div>
</div>

<div class="card">
  <h2>1. Загрузить и проверить файл</h2>
  <form id="upload-form" class="form-grid">
    <div>
      <label for="template-id">Тест</label>
      <select id="template-id" name="template_id" required></select>
    </div>
    <div>
      <label for="file">Файл XLSX</label>
      <input id="file" name="file" type="file" accept=".xlsx" required>
    </div>
    <button id="preview-button" type="submit">Проверить файл</button>
  </form>
  <div id="message" class="message"></div>
</div>

<div id="preview-card" class="card hidden">
  <h2>2. Предварительный результат</h2>
  <div id="file-info" class="small"></div>
  <div id="summary" class="summary"></div>
  <div class="filters">
    <div>
      <label for="text-filter">Поиск</label>
      <input id="text-filter" type="search" placeholder="Email, ФИО, подразделение или должность">
    </div>
    <div>
      <label for="status-filter">Результат проверки</label>
      <select id="status-filter">
        <option value="">Все строки</option>
        <option value="ready">Готовы</option>
        <option value="warning">Предупреждения</option>
        <option value="error">Ошибки</option>
      </select>
    </div>
  </div>
  <div class="actions">
    <button id="toggle-ready" type="button" class="secondary">Снять выбор со всех готовых</button>
    <button id="confirm-button" type="button">Подтвердить назначения</button>
    <button id="issues-button" type="button" class="secondary">Скачать ошибки и предупреждения</button>
    <span id="selected-count" class="small"></span>
  </div>
  <p id="visible-count" class="small"></p>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th></th><th>Строка</th><th>Email из файла</th><th>ФИО из основной базы</th>
          <th>Подразделение</th><th>Должность</th><th>Логин</th><th>Результат</th>
        </tr>
      </thead>
      <tbody id="result-body"></tbody>
    </table>
  </div>
</div>

<script>
const statusLabels = {
  ready: 'Готово к назначению',
  imported: 'Назначение создано',
  already_assigned: 'Уже назначен ранее',
  duplicate: 'Дубликат в файле',
  invalid: 'Некорректный email',
  not_found: 'Не найден в основной базе',
  multiple_matches: 'Несколько совпадений',
  inactive_match: 'Найден только неактивный сотрудник',
  no_login: 'Нет логина',
  excluded_by_operator: 'Исключено оператором'
};
const warningStatuses = new Set(['already_assigned', 'duplicate', 'excluded_by_operator']);
const errorStatuses = new Set(['invalid', 'not_found', 'multiple_matches', 'inactive_match', 'no_login']);
let currentPreview = null;

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
}
function category(status) {
  if (status === 'ready' || status === 'imported') return 'ready';
  if (warningStatuses.has(status)) return 'warning';
  return 'error';
}
function showMessage(text, kind) {
  const node = document.getElementById('message');
  node.textContent = text;
  node.className = `message ${kind}`;
}
function clearMessage() {
  const node = document.getElementById('message');
  node.textContent = '';
  node.className = 'message';
}
async function api(url, options = {}) {
  const response = await fetch(url, options);
  const contentType = response.headers.get('content-type') || '';
  const payload = contentType.includes('application/json') ? await response.json() : await response.text();
  if (!response.ok) throw new Error(payload.detail || payload || `HTTP ${response.status}`);
  return payload;
}
async function loadTemplates() {
  const payload = await api('/api/audience/templates');
  const select = document.getElementById('template-id');
  select.innerHTML = '';
  payload.templates.forEach(template => {
    const option = document.createElement('option');
    option.value = template.id;
    option.textContent = `${template.name}${template.enabled ? '' : ' – отключен'}`;
    select.appendChild(option);
  });
  if (!payload.templates.length) {
    const option = document.createElement('option');
    option.textContent = 'Нет шаблонов с audience.type: explicit_list';
    option.value = '';
    select.appendChild(option);
    document.getElementById('preview-button').disabled = true;
  }
}
function renderPreview(payload) {
  currentPreview = payload;
  const batch = payload.import;
  document.getElementById('preview-card').classList.remove('hidden');
  document.getElementById('file-info').textContent = `Файл: ${batch.original_filename} | импорт №${batch.id}`;
  document.getElementById('summary').innerHTML = `
    <div class="metric"><strong>${batch.total_rows}</strong>строк с email</div>
    <div class="metric"><strong>${batch.ready_rows}</strong>готовы</div>
    <div class="metric"><strong>${batch.warning_rows}</strong>предупреждения</div>
    <div class="metric"><strong>${batch.error_rows}</strong>ошибки</div>`;

  const body = document.getElementById('result-body');
  body.innerHTML = payload.rows.map(row => {
    const rowCategory = category(row.status);
    const checkbox = row.status === 'ready'
      ? `<input class="row-checkbox" type="checkbox" data-row-id="${row.id}" checked>`
      : '';
    const reason = row.error_text ? `<div class="small">${escapeHtml(row.error_text)}</div>` : '';
    return `<tr class="${rowCategory}" data-category="${rowCategory}" data-search="${escapeHtml([
      row.source_email, row.employee_fio, row.employee_department, row.employee_position, row.employee_login
    ].join(' ').toLowerCase())}">
      <td>${checkbox}</td>
      <td>${row.row_number}</td>
      <td>${escapeHtml(row.source_email)}</td>
      <td>${escapeHtml(row.employee_fio || '–')}</td>
      <td>${escapeHtml(row.employee_department || '–')}</td>
      <td>${escapeHtml(row.employee_position || '–')}</td>
      <td>${escapeHtml(row.employee_login || '–')}</td>
      <td class="status status-${rowCategory}">${escapeHtml(statusLabels[row.status] || row.status)}${reason}</td>
    </tr>`;
  }).join('');

  document.querySelectorAll('.row-checkbox').forEach(node => node.addEventListener('change', updateSelected));
  document.getElementById('confirm-button').disabled = batch.status !== 'preview';
  document.getElementById('toggle-ready').disabled = batch.status !== 'preview';
  applyFilters();
  updateSelected();
}
function selectedIds() {
  return [...document.querySelectorAll('.row-checkbox:checked')].map(node => Number(node.dataset.rowId));
}
function updateSelected() {
  const count = selectedIds().length;
  document.getElementById('selected-count').textContent = `Выбрано для назначения: ${count}`;
  document.getElementById('confirm-button').textContent = `Подтвердить ${count} назначений`;
  document.getElementById('confirm-button').disabled = !currentPreview || currentPreview.import.status !== 'preview';
}
function applyFilters() {
  const query = document.getElementById('text-filter').value.trim().toLowerCase();
  const filter = document.getElementById('status-filter').value;
  let visible = 0;
  document.querySelectorAll('#result-body tr').forEach(row => {
    const matchesText = !query || row.dataset.search.includes(query);
    const matchesStatus = !filter || row.dataset.category === filter;
    row.hidden = !(matchesText && matchesStatus);
    if (!row.hidden) visible += 1;
  });
  document.getElementById('visible-count').textContent = `Показано строк: ${visible}`;
}

document.getElementById('upload-form').addEventListener('submit', async event => {
  event.preventDefault();
  clearMessage();
  const button = document.getElementById('preview-button');
  button.disabled = true;
  button.textContent = 'Проверка...';
  try {
    const form = new FormData(event.target);
    const payload = await api('/api/audience/preview', {method: 'POST', body: form});
    renderPreview(payload);
    showMessage('Файл проверен. Просмотрите найденных сотрудников и подтвердите корректные назначения.', 'success');
  } catch (error) {
    showMessage(error.message, 'error');
  } finally {
    button.disabled = false;
    button.textContent = 'Проверить файл';
  }
});

document.getElementById('confirm-button').addEventListener('click', async () => {
  if (!currentPreview) return;
  const rowIds = selectedIds();
  if (!confirm(`Создать ${rowIds.length} назначений? Ошибочные строки будут пропущены.`)) return;
  const button = document.getElementById('confirm-button');
  button.disabled = true;
  try {
    const payload = await api(`/api/audience/imports/${currentPreview.import.id}/confirm`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({row_ids: rowIds})
    });
    renderPreview(payload.preview);
    showMessage(`Импорт завершен. Создано назначений: ${payload.imported}. Исключено оператором: ${payload.excluded}.`, 'success');
  } catch (error) {
    showMessage(error.message, 'error');
  }
});

document.getElementById('issues-button').addEventListener('click', () => {
  if (currentPreview) window.location.href = `/api/audience/imports/${currentPreview.import.id}/issues.xlsx`;
});
document.getElementById('toggle-ready').addEventListener('click', event => {
  const boxes = [...document.querySelectorAll('.row-checkbox')];
  const shouldCheck = boxes.some(box => !box.checked);
  boxes.forEach(box => box.checked = shouldCheck);
  event.target.textContent = shouldCheck ? 'Снять выбор со всех готовых' : 'Выбрать все готовые';
  updateSelected();
});
document.getElementById('text-filter').addEventListener('input', applyFilters);
document.getElementById('status-filter').addEventListener('change', applyFilters);

loadTemplates().catch(error => showMessage(error.message, 'error'));
</script>
</body>
</html>"""
