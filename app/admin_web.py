from __future__ import annotations

import io
import os
import secrets
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


def database() -> Database:
    global _db
    if _db is None:
        _db = Database(settings().database_path)
        bootstrap_legacy_overrides(settings(), _db)
    return _db


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


@app.exception_handler(AudienceImportError)
async def audience_error_handler(_, error: AudienceImportError):
    return JSONResponse(
        status_code=400,
        content={"detail": str(error)},
    )


@app.get("/admin/", response_class=HTMLResponse)
def admin_page(_: Annotated[str, Depends(require_admin)]) -> str:
    return ADMIN_HTML


@app.get("/admin/logins/", response_class=HTMLResponse)
def login_overrides_page(_: Annotated[str, Depends(require_admin)]) -> str:
    return LOGIN_OVERRIDES_HTML


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


@app.get("/api/health")
def health():
    database()
    return {"status": "ok"}


LOGIN_OVERRIDES_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Сопоставление e-mail и логина</title>
<style>
body { font-family: Arial, sans-serif; margin: 24px; color: #222; background: #f5f6f8; }
.card { background: #fff; border-radius: 10px; padding: 18px; margin-bottom: 18px; box-shadow: 0 1px 4px rgba(0,0,0,.08); }
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
  <div class="header-line">
    <div>
      <h1>Сопоставление e-mail и логина</h1>
      <div class="small">Используется только для работников, у которых логин Indigo не совпадает с частью e-mail до знака @. Изменения применяются сразу.</div>
    </div>
    <div class="actions">
      <a class="back-link" href="/admin/">Импорт участников</a>
      <a class="back-link" href="/">Вернуться к отчету</a>
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
      <thead><tr><th>E-mail</th><th>Логин Indigo</th><th>Сотрудник</th><th>Изменено</th><th></th></tr></thead>
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
  const query = document.getElementById('search').value.trim().toLowerCase();
  const filtered = items.filter(item => !query || [item.email, item.login, item.fio].join(' ').toLowerCase().includes(query));
  document.getElementById('count').textContent = `Показано сопоставлений: ${filtered.length}`;
  document.getElementById('mapping-body').innerHTML = filtered.map(item => `
    <tr>
      <td>${escapeHtml(item.email)}</td>
      <td>${escapeHtml(item.login)}</td>
      <td>${escapeHtml(item.fio || 'Не найден в текущей базе')}${item.fio && item.active === false ? '<div class="small">Сотрудник неактивен</div>' : ''}</td>
      <td>${escapeHtml(formatDate(item.updated_at))}</td>
      <td><button class="danger" type="button" data-email="${escapeHtml(item.email)}">Удалить</button></td>
    </tr>`).join('');
  document.querySelectorAll('button[data-email]').forEach(button => button.addEventListener('click', async () => {
    if (!confirm(`Удалить сопоставление для ${button.dataset.email}?`)) return;
    try {
      await api(`/api/login-overrides/${encodeURIComponent(button.dataset.email)}`, {method: 'DELETE'});
      await load();
      showMessage('Сопоставление удалено. Для сотрудника снова используется часть e-mail до знака @.', 'success');
    } catch (error) { showMessage(error.message, 'error'); }
  }));
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
  <div class="header-line">
    <div>
      <h1>Импорт списков участников</h1>
      <div class="small">Файл используется только для отбора по email. ФИО, подразделение, должность и логин берутся из основной ежедневной базы.</div>
    </div>
    <div class="actions">
      <a class="back-link" href="/admin/logins/">Сопоставление логинов</a>
      <a class="back-link" href="/">Вернуться к отчету</a>
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
