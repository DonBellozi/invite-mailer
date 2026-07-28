from __future__ import annotations

import io
import os
import secrets
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

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
from .settings import Settings, load_settings

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


def database() -> Database:
    global _db
    if _db is None:
        _db = Database(settings().database_path)
        bootstrap_legacy_overrides(settings(), _db)
        _ensure_v202_schema(_db)
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


class ReviewerRequest(BaseModel):
    name: str
    email: str
    enabled: bool = True


class TechnicalSettingsRequest(BaseModel):
    enabled: bool
    email: str
    repeat_hours: int = 72


def find_report_template(
    template_id: str,
) -> dict:
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


@app.get("/admin/settings/technical/", response_class=HTMLResponse)
def technical_settings_page(_: Annotated[str, Depends(require_admin)]) -> str:
    return TECHNICAL_SETTINGS_HTML


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
    _write_setting(
        "reviewer_notifications_enabled",
        "1" if request.notify_reviewers else "0",
    )
    return read_reminder_settings(_)


@app.get("/api/settings/technical")
def read_technical_settings(_: Annotated[str, Depends(require_admin)]):
    return {
        "enabled": _read_setting("technical_notifications_enabled", "0") == "1",
        "email": _read_setting("technical_email", ""),
        "repeat_hours": int(_read_setting("technical_repeat_hours", "72")),
    }


@app.put("/api/settings/technical")
def write_technical_settings(
    request: TechnicalSettingsRequest,
    _: Annotated[str, Depends(require_admin)],
):
    email = request.email.strip().lower()
    if request.enabled and ("@" not in email or email.startswith("@") or email.endswith("@")):
        raise HTTPException(status_code=400, detail="Укажите корректный технический e-mail")
    if request.repeat_hours < 1 or request.repeat_hours > 8760:
        raise HTTPException(status_code=400, detail="Интервал должен быть от 1 до 8760 часов")
    _write_setting("technical_notifications_enabled", "1" if request.enabled else "0")
    _write_setting("technical_email", email)
    _write_setting("technical_repeat_hours", str(request.repeat_hours))
    return read_technical_settings(_)


@app.get("/api/reviewers")
def read_reviewers(_: Annotated[str, Depends(require_admin)]):
    with database().connect() as connection:
        rows = connection.execute(
            """
            SELECT id, name, email, enabled, created_at, updated_at
            FROM reviewers
            ORDER BY enabled DESC, name COLLATE NOCASE, email COLLATE NOCASE
            """
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.post("/api/reviewers")
def create_reviewer(
    request: ReviewerRequest,
    _: Annotated[str, Depends(require_admin)],
):
    name = request.name.strip()
    email = request.email.strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="Укажите имя проверяющего")
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(status_code=400, detail="Некорректный e-mail")
    now = _utc_now()
    try:
        with database().connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO reviewers (
                    name, email, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (name, email, int(request.enabled), now, now),
            )
            connection.commit()
            reviewer_id = cursor.lastrowid
            row = connection.execute(
                """
                SELECT id, name, email, enabled, created_at, updated_at
                FROM reviewers WHERE id = ?
                """,
                (reviewer_id,),
            ).fetchone()
    except Exception as error:
        if "UNIQUE" in str(error).upper():
            raise HTTPException(
                status_code=400,
                detail="Проверяющий с таким e-mail уже добавлен",
            ) from error
        raise
    return dict(row)


@app.put("/api/reviewers/{reviewer_id}")
def update_reviewer(
    reviewer_id: int,
    request: ReviewerRequest,
    _: Annotated[str, Depends(require_admin)],
):
    name = request.name.strip()
    email = request.email.strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="Укажите имя проверяющего")
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(status_code=400, detail="Некорректный e-mail")
    try:
        with database().connect() as connection:
            cursor = connection.execute(
                """
                UPDATE reviewers
                SET name = ?, email = ?, enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (name, email, int(request.enabled), _utc_now(), reviewer_id),
            )
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Проверяющий не найден")
            connection.commit()
            row = connection.execute(
                """
                SELECT id, name, email, enabled, created_at, updated_at
                FROM reviewers WHERE id = ?
                """,
                (reviewer_id,),
            ).fetchone()
    except HTTPException:
        raise
    except Exception as error:
        if "UNIQUE" in str(error).upper():
            raise HTTPException(
                status_code=400,
                detail="Проверяющий с таким e-mail уже добавлен",
            ) from error
        raise
    return dict(row)


@app.delete("/api/reviewers/{reviewer_id}")
def delete_reviewer(
    reviewer_id: int,
    _: Annotated[str, Depends(require_admin)],
):
    with database().connect() as connection:
        cursor = connection.execute(
            "DELETE FROM reviewers WHERE id = ?",
            (reviewer_id,),
        )
        connection.commit()
    return {"deleted": cursor.rowcount > 0}


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


SETTINGS_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Настройки</title>
<style>
body {
  font-family: Arial, sans-serif;
  margin: 24px;
  color: #222;
  background: #f5f6f8;
}

.card {
  background: #fff;
  border-radius: 10px;
  padding: 18px;
  margin-bottom: 18px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, .08);
}

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

h1 {
  margin: 0 0 8px;
  font-size: 24px;
}

.small {
  color: #667085;
  font-size: 12px;
}

.settings-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(280px, 1fr));
  gap: 18px;
}

.settings-item {
  display: flex;
  flex-direction: column;
  min-height: 160px;
  padding: 18px;
  border: 1px solid #e1e5ea;
  border-radius: 10px;
  background: #fff;
  box-sizing: border-box;
}

.settings-item h2 {
  margin: 0 0 8px;
  font-size: 18px;
}

.settings-item p {
  margin: 0 0 18px;
  color: #667085;
  font-size: 13px;
  line-height: 1.45;
}

.settings-item .item-action {
  margin-top: auto;
}

.settings-link,
.settings-disabled {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 38px;
  padding: 9px 13px;
  border-radius: 7px;
  box-sizing: border-box;
  font-size: 13px;
  font-weight: 600;
}

.settings-link {
  border: 1px solid #175cd3;
  color: #fff;
  background: #175cd3;
  text-decoration: none;
}

.settings-link:hover {
  background: #1849a9;
}

.settings-disabled {
  border: 1px solid #d0d5dd;
  color: #667085;
  background: #f2f4f7;
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

  .page-header-actions {
    flex: 1 1 220px;
    min-width: 220px;
    padding: 14px 0 0;
    border-left: 0;
    border-top: 2px solid #e5e7eb;
  }

  .settings-grid {
    grid-template-columns: 1fr;
  }
}
</style>
</head>
<body>
<div class="card">
  <div class="page-header">
    <div class="page-header-main">
      <h1>Настройки</h1>
      <div class="small">
        Управление параметрами рассылки, проверяющими,
        сопоставлением логинов и почтовыми шаблонами.
      </div>
    </div>

    <div class="page-header-actions">
      <a class="header-button" href="/admin/">
        Импорт участников
      </a>
      <a class="header-button" href="/">
        Вернуться к отчету
      </a>
    </div>
  </div>
</div>

<div class="settings-grid">
  <section class="settings-item">
    <h2>Общие настройки</h2>
    <p>
      Интервалы напоминаний, правила повторной отправки
      и другие общие параметры работы сервиса.
    </p>
    <div class="item-action">
      <a class="settings-link" href="/admin/settings/general/">Открыть настройки</a>
    </div>
  </section>

  <section class="settings-item">
    <h2>Проверяющие</h2>
    <p>
      Адресаты служебных уведомлений и параметры отправки
      списка работников, не прошедших тестирование.
    </p>
    <div class="item-action">
      <a class="settings-link" href="/admin/settings/reviewers/">Открыть проверяющих</a>
    </div>
  </section>

  <section class="settings-item">
    <h2>Сопоставление логинов</h2>
    <p>
      Ручное сопоставление e-mail работников с логинами Indigo,
      когда логин отличается от части адреса до знака @.
    </p>
    <div class="item-action">
      <a class="settings-link" href="/admin/logins/">
        Открыть сопоставления
      </a>
    </div>
  </section>


  <section class="settings-item">
    <h2>Технические уведомления</h2>
    <p>
      Немедленные сообщения об ошибках данных и SMTP
      с защитой от повторов в течение 72 часов.
    </p>
    <div class="item-action">
      <a class="settings-link" href="/admin/settings/technical/">Открыть настройки</a>
    </div>
  </section>

  <section class="settings-item">
    <h2>Шаблоны</h2>
    <p>
      Темы и тексты приглашений, напоминаний
      и уведомлений проверяющим.
    </p>
    <div class="item-action">
      <span class="settings-disabled">Будет добавлено в v2.0.3</span>
    </div>
  </section>
</div>
</body>
</html>"""


GENERAL_SETTINGS_HTML = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Общие настройки</title>
<style>
body{font-family:Arial,sans-serif;margin:24px;color:#222;background:#f5f6f8}.card{background:#fff;border-radius:10px;padding:18px;margin-bottom:18px;box-shadow:0 1px 4px rgba(0,0,0,.08)}h1{margin:0 0 8px;font-size:24px}h2{margin:0 0 14px;font-size:18px}.header{display:flex;justify-content:space-between;gap:18px;align-items:flex-start}.actions{display:flex;gap:10px;flex-wrap:wrap}.link{display:inline-flex;align-items:center;justify-content:center;min-height:38px;padding:9px 13px;border:1px solid #98a2b3;border-radius:7px;color:#344054;text-decoration:none;font-size:13px;font-weight:600;background:#fff}.small{color:#667085;font-size:12px}.form{max-width:680px}.row{display:grid;grid-template-columns:1fr 180px;gap:16px;align-items:center;padding:14px 0;border-bottom:1px solid #eaecf0}.row:last-child{border-bottom:0}label{font-weight:600}.hint{margin-top:4px;color:#667085;font-size:12px;line-height:1.4}input[type=number]{width:100%;box-sizing:border-box;padding:10px;border:1px solid #ccd2da;border-radius:6px}input[type=checkbox]{width:20px;height:20px}.save{margin-top:18px;padding:10px 16px;border:0;border-radius:7px;background:#175cd3;color:#fff;font-weight:600;cursor:pointer}.message{display:none;margin-top:14px;padding:12px;border-radius:7px}.message.success{display:block;background:#ecfdf3;color:#05603a}.message.error{display:block;background:#fef3f2;color:#912018}@media(max-width:720px){.header{flex-direction:column}.row{grid-template-columns:1fr}.actions{width:100%}.link{flex:1}}
</style></head><body>
<div class="card"><div class="header"><div><h1>Общие настройки</h1><div class="small">Параметры повторных напоминаний и служебных уведомлений.</div></div><div class="actions"><a class="link" href="/admin/settings/">Настройки</a><a class="link" href="/">Отчет</a></div></div></div>
<div class="card"><h2>Повторные напоминания</h2><form id="form" class="form">
<div class="row"><div><label for="enabled">Отправлять повторные напоминания</label><div class="hint">Напоминания предназначены для работников, которым приглашение уже отправлено, но тест еще не завершен.</div></div><input id="enabled" type="checkbox"></div>
<div class="row"><div><label for="interval">Интервал между напоминаниями</label><div class="hint">Количество календарных дней после предыдущего приглашения или напоминания.</div></div><input id="interval" type="number" min="1" max="365" required></div>
<div class="row"><div><label for="notify-reviewers">Уведомлять проверяющих</label><div class="hint">Разрешает отправку служебных уведомлений активным проверяющим.</div></div><input id="notify-reviewers" type="checkbox"></div>
<button class="save" type="submit">Сохранить</button><div id="message" class="message"></div></form></div>
<script>
async function api(url,options={}){const r=await fetch(url,options);const p=await r.json();if(!r.ok)throw new Error(p.detail||`HTTP ${r.status}`);return p}function msg(t,k){const n=document.getElementById('message');n.textContent=t;n.className=`message ${k}`}
async function load(){const p=await api('/api/settings/reminders');document.getElementById('enabled').checked=p.enabled;document.getElementById('interval').value=p.interval_days;document.getElementById('notify-reviewers').checked=p.notify_reviewers}
document.getElementById('form').addEventListener('submit',async e=>{e.preventDefault();try{const p=await api('/api/settings/reminders',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:document.getElementById('enabled').checked,interval_days:Number(document.getElementById('interval').value),notify_reviewers:document.getElementById('notify-reviewers').checked})});msg(`Настройки сохранены. Интервал: ${p.interval_days} дн.`,'success')}catch(error){msg(error.message,'error')}});load().catch(e=>msg(e.message,'error'));
</script></body></html>"""


REVIEWERS_HTML = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Проверяющие</title>
<style>
body{font-family:Arial,sans-serif;margin:24px;color:#222;background:#f5f6f8}.card{background:#fff;border-radius:10px;padding:18px;margin-bottom:18px;box-shadow:0 1px 4px rgba(0,0,0,.08)}h1{margin:0 0 8px;font-size:24px}h2{margin:0 0 14px;font-size:18px}.header{display:flex;justify-content:space-between;gap:18px;align-items:flex-start}.actions{display:flex;gap:10px;flex-wrap:wrap}.link{display:inline-flex;align-items:center;justify-content:center;min-height:38px;padding:9px 13px;border:1px solid #98a2b3;border-radius:7px;color:#344054;text-decoration:none;font-size:13px;font-weight:600;background:#fff}.small{color:#667085;font-size:12px}.grid{display:grid;grid-template-columns:minmax(240px,1fr) minmax(260px,1fr) auto auto;gap:12px;align-items:end}label{display:block;margin-bottom:5px;color:#475467;font-size:12px;font-weight:600}input{box-sizing:border-box;padding:10px;border:1px solid #ccd2da;border-radius:6px;width:100%}input[type=checkbox]{width:20px;height:20px}.button{padding:10px 14px;border:0;border-radius:7px;background:#175cd3;color:#fff;font-weight:600;cursor:pointer}.danger{padding:7px 10px;border:1px solid #f04438;border-radius:6px;background:#fff;color:#b42318;cursor:pointer}.table-wrap{overflow:auto;border:1px solid #eaecf0;border-radius:8px}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:9px 8px;border-bottom:1px solid #e1e5ea;text-align:left}th{background:#f0f2f5}.message{display:none;margin-top:12px;padding:12px;border-radius:7px}.message.success{display:block;background:#ecfdf3;color:#05603a}.message.error{display:block;background:#fef3f2;color:#912018}@media(max-width:850px){.header{flex-direction:column}.grid{grid-template-columns:1fr}.actions{width:100%}.link{flex:1}}
</style></head><body>
<div class="card"><div class="header"><div><h1>Проверяющие</h1><div class="small">Получатели служебных уведомлений о ходе тестирования.</div></div><div class="actions"><a class="link" href="/admin/settings/">Настройки</a><a class="link" href="/">Отчет</a></div></div></div>
<div class="card"><h2 id="form-title">Добавить проверяющего</h2><form id="form" class="grid"><div><label for="name">Имя или подразделение</label><input id="name" required placeholder="Отдел информационной безопасности"></div><div><label for="email">E-mail</label><input id="email" type="email" required placeholder="security@example.ru"></div><div><label for="enabled">Активен</label><input id="enabled" type="checkbox" checked></div><button class="button" type="submit">Сохранить</button></form><div id="message" class="message"></div></div>
<div class="card"><h2>Список проверяющих</h2><p id="count" class="small"></p><div class="table-wrap"><table><thead><tr><th>Имя</th><th>E-mail</th><th>Статус</th><th>Изменено</th><th></th></tr></thead><tbody id="body"></tbody></table></div></div>
<script>
let items=[],editId=null;function esc(v){return String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]))}async function api(u,o={}){const r=await fetch(u,o);const p=await r.json();if(!r.ok)throw new Error(p.detail||`HTTP ${r.status}`);return p}function msg(t,k){const n=document.getElementById('message');n.textContent=t;n.className=`message ${k}`}function fmt(v){if(!v)return'';const d=new Date(v);return Number.isNaN(d.getTime())?v:d.toLocaleString('ru-RU')}
function render(){document.getElementById('count').textContent=`Проверяющих: ${items.length}`;document.getElementById('body').innerHTML=items.map(x=>`<tr><td>${esc(x.name)}</td><td>${esc(x.email)}</td><td>${x.enabled?'Активен':'Отключен'}</td><td>${esc(fmt(x.updated_at))}</td><td><button class="danger" data-id="${x.id}">Удалить</button></td></tr>`).join('');document.querySelectorAll('[data-id]').forEach(b=>b.addEventListener('click',async()=>{if(!confirm('Удалить проверяющего?'))return;try{await api(`/api/reviewers/${b.dataset.id}`,{method:'DELETE'});await load();msg('Проверяющий удален.','success')}catch(e){msg(e.message,'error')}}))}
async function load(){const p=await api('/api/reviewers');items=p.items;render()}document.getElementById('form').addEventListener('submit',async e=>{e.preventDefault();try{await api('/api/reviewers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:document.getElementById('name').value,email:document.getElementById('email').value,enabled:document.getElementById('enabled').checked})});e.target.reset();document.getElementById('enabled').checked=true;await load();msg('Проверяющий сохранен.','success')}catch(error){msg(error.message,'error')}});load().catch(e=>msg(e.message,'error'));
</script></body></html>"""


TECHNICAL_SETTINGS_HTML = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Технические уведомления</title>
<style>
body{font-family:Arial,sans-serif;margin:24px;color:#222;background:#f5f6f8}.card{background:#fff;border-radius:10px;padding:18px;margin-bottom:18px;box-shadow:0 1px 4px rgba(0,0,0,.08)}h1{margin:0 0 8px;font-size:24px}h2{margin:0 0 14px;font-size:18px}.header{display:flex;justify-content:space-between;gap:18px;align-items:flex-start}.actions{display:flex;gap:10px;flex-wrap:wrap}.link{display:inline-flex;align-items:center;justify-content:center;min-height:38px;padding:9px 13px;border:1px solid #98a2b3;border-radius:7px;color:#344054;text-decoration:none;font-size:13px;font-weight:600;background:#fff}.small{color:#667085;font-size:12px}.form{max-width:760px}.row{display:grid;grid-template-columns:1fr 260px;gap:16px;align-items:center;padding:14px 0;border-bottom:1px solid #eaecf0}.row:last-child{border-bottom:0}label{font-weight:600}.hint{margin-top:4px;color:#667085;font-size:12px;line-height:1.4}input[type=email],input[type=number]{width:100%;box-sizing:border-box;padding:10px;border:1px solid #ccd2da;border-radius:6px}input[type=checkbox]{width:20px;height:20px}.save{margin-top:18px;padding:10px 16px;border:0;border-radius:7px;background:#175cd3;color:#fff;font-weight:600;cursor:pointer}.message{display:none;margin-top:14px;padding:12px;border-radius:7px}.message.success{display:block;background:#ecfdf3;color:#05603a}.message.error{display:block;background:#fef3f2;color:#912018}@media(max-width:720px){.header{flex-direction:column}.row{grid-template-columns:1fr}.actions{width:100%}.link{flex:1}}
</style></head><body>
<div class="card"><div class="header"><div><h1>Технические уведомления</h1><div class="small">Ошибки данных отправляются сразу после импорта, ошибки SMTP – сразу после неудачной попытки отправки.</div></div><div class="actions"><a class="link" href="/admin/settings/">Настройки</a><a class="link" href="/">Отчет</a></div></div></div>
<div class="card"><h2>Параметры</h2><form id="form" class="form">
<div class="row"><div><label for="enabled">Отправлять технические уведомления</label><div class="hint">При отключении ошибки продолжают записываться в базу, но письмо не отправляется.</div></div><input id="enabled" type="checkbox"></div>
<div class="row"><div><label for="email">Технический e-mail</label><div class="hint">Адрес для немедленных сообщений об ошибках файла 1С и SMTP.</div></div><input id="email" type="email" placeholder="it@example.ru"></div>
<div class="row"><div><label for="repeat">Повтор одинаковой ошибки</label><div class="hint">Одинаковая ошибка повторно отправляется не раньше указанного интервала. Рекомендуемое значение – 72 часа.</div></div><input id="repeat" type="number" min="1" max="8760" required></div>
<button class="save" type="submit">Сохранить</button><div id="message" class="message"></div></form></div>
<script>
async function api(url,options={}){const r=await fetch(url,options);const p=await r.json();if(!r.ok)throw new Error(p.detail||`HTTP ${r.status}`);return p}function msg(t,k){const n=document.getElementById('message');n.textContent=t;n.className=`message ${k}`}
async function load(){const p=await api('/api/settings/technical');document.getElementById('enabled').checked=p.enabled;document.getElementById('email').value=p.email;document.getElementById('repeat').value=p.repeat_hours}
document.getElementById('form').addEventListener('submit',async e=>{e.preventDefault();try{const p=await api('/api/settings/technical',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:document.getElementById('enabled').checked,email:document.getElementById('email').value,repeat_hours:Number(document.getElementById('repeat').value)})});msg(`Настройки сохранены. Повтор одинаковой ошибки – через ${p.repeat_hours} ч.`,'success')}catch(error){msg(error.message,'error')}});load().catch(e=>msg(e.message,'error'));
</script></body></html>"""


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
