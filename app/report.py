from __future__ import annotations

import fnmatch
import html
from datetime import datetime
from pathlib import Path

from .db import Database


CSS = """
body { font-family: Arial, sans-serif; margin: 24px; color: #222; background: #f5f6f8; }
.card { background: white; border-radius: 10px; padding: 18px; margin-bottom: 18px; box-shadow: 0 1px 4px rgba(0,0,0,.08); }
h1 { margin-top: 0; font-size: 24px; }
.summary { display: flex; gap: 12px; flex-wrap: wrap; }
.metric { min-width: 160px; background: #eef2f7; padding: 12px; border-radius: 8px; }
.metric strong { display: block; font-size: 22px; }
.filters { display: flex; gap: 12px; align-items: end; flex-wrap: wrap; margin: 8px 0 14px; }
.filter-field { min-width: 220px; flex: 1; }
.filter-field-search { flex: 2; min-width: 320px; }
.filter-field label { display: block; margin-bottom: 5px; color: #475467; font-size: 12px; font-weight: 600; }
input, select { width: 100%; box-sizing: border-box; padding: 10px; border: 1px solid #ccd2da; border-radius: 6px; background: #fff; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 9px 8px; border-bottom: 1px solid #e1e5ea; text-align: left; vertical-align: top; }
th { position: sticky; top: 0; background: #f0f2f5; }
.status-completed { color: #176b36; font-weight: 600; }
.status-failed { color: #8a5b00; font-weight: 600; }
.status-sent { color: #176b36; font-weight: 600; }
.status-wait { color: #8a5b00; font-weight: 600; }
.status-error { color: #a21d1d; font-weight: 600; }
.small { color: #667085; font-size: 12px; }
"""


COMPLETED_STATUSES = {"completed", "done", "passed"}
FAILED_STATUSES = {"failed", "not_passed"}


def _fmt_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def _template_applies(department: str | None, template: dict) -> bool:
    rule = template.get("departments", {})
    value = (department or "").lower()
    includes = rule.get("include") or ["*"]
    excludes = rule.get("exclude") or []

    included = any(fnmatch.fnmatch(value, str(pattern).lower()) for pattern in includes)
    excluded = any(fnmatch.fnmatch(value, str(pattern).lower()) for pattern in excludes)
    return included and not excluded


def _method_label(method: str | None) -> str:
    if method == "manual_seed":
        return "вручную"
    if method == "automatic":
        return "автоматически"
    return method or ""


def _row_status(employee, latest) -> tuple[str, str, str]:
    """Возвращает: текст статуса, CSS-класс, ключ для фильтра."""
    if not employee["email"]:
        return "Нет адреса электронной почты", "status-error", "no_email"

    if latest:
        raw_status = str(latest["status"] or "").strip().lower()

        if raw_status in COMPLETED_STATUSES:
            return "Выполнил", "status-completed", "completed"

        if raw_status in FAILED_STATUSES:
            return "Не прошел", "status-failed", "failed"

        if raw_status == "sent":
            method = _method_label(latest["method"])
            suffix = f" – {method}" if method else ""
            return (
                f"Отправлено {_fmt_date(latest['sent_at'])}{suffix}",
                "status-sent",
                "sent",
            )

        if raw_status == "error":
            error_text = latest["error_text"] or "неизвестная ошибка"
            return f"Ошибка: {error_text}", "status-error", "error"

    return "Ожидает отправки", "status-wait", "waiting"


def build_report(db: Database, templates: list[dict], title: str, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)

    with db.connect() as connection:
        employees = connection.execute(
            "SELECT * FROM employees WHERE active = 1 ORDER BY department, fio"
        ).fetchall()
        last_import = connection.execute(
            "SELECT * FROM imports WHERE status = 'success' ORDER BY id DESC LIMIT 1"
        ).fetchone()

        sent_total = connection.execute(
            "SELECT COUNT(*) AS count FROM notification_history WHERE status = 'sent'"
        ).fetchone()["count"]

        missing_email = connection.execute(
            "SELECT COUNT(*) AS count FROM employees WHERE active = 1 AND (email IS NULL OR email = '')"
        ).fetchone()["count"]

        history_template_ids = {
            row["template_id"]
            for row in connection.execute(
                "SELECT DISTINCT template_id FROM notification_history"
            ).fetchall()
        }

        visible_templates = [
            template
            for template in templates
            if template.get("enabled", True) or template["id"] in history_template_ids
        ]

        rows: list[str] = []
        for employee in employees:
            for template in visible_templates:
                if not _template_applies(employee["department"], template):
                    continue

                latest = connection.execute(
                    """
                    SELECT * FROM notification_history
                    WHERE worker_key = ? AND employment_seq = ? AND template_id = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (employee["worker_key"], employee["employment_seq"], template["id"]),
                ).fetchone()

                status, status_class, status_key = _row_status(employee, latest)

                values = [
                    employee["fio"],
                    employee["email"] or "",
                    employee["login"] or "",
                    employee["department"] or "",
                    employee["position"] or "",
                    template.get("name", template["id"]),
                ]
                cells = "".join(f"<td>{html.escape(str(value))}</td>" for value in values)
                template_id = html.escape(str(template["id"]), quote=True)
                escaped_status_key = html.escape(status_key, quote=True)
                rows.append(
                    f"<tr data-template-id='{template_id}' data-status='{escaped_status_key}'>{cells}"
                    f"<td class='{status_class}'>{html.escape(status)}</td></tr>"
                )

    import_text = "Нет успешно загруженных файлов"
    if last_import:
        import_text = f"{_fmt_date(last_import['imported_at'])}, сотрудников: {last_import['row_count']}"

    template_options = ["<option value=''>Все тесты</option>"]
    for template in visible_templates:
        template_id = html.escape(str(template["id"]), quote=True)
        template_name = html.escape(str(template.get("name", template["id"])))
        template_options.append(f"<option value='{template_id}'>{template_name}</option>")

    status_options = """
<option value="">Все статусы</option>
<option value="completed">Выполнил</option>
<option value="failed">Не прошел</option>
<option value="sent">Отправлено, ожидает выполнения</option>
<option value="waiting">Ожидает отправки</option>
<option value="error">Ошибка отправки</option>
<option value="no_email">Нет email</option>
""".strip()

    page = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="card">
  <h1>{html.escape(title)}</h1>
  <div class="summary">
    <div class="metric"><strong>{len(employees)}</strong>актуальных сотрудников</div>
    <div class="metric"><strong>{sent_total}</strong>успешных отправок</div>
    <div class="metric"><strong>{missing_email}</strong>без email</div>
  </div>
  <p class="small">Последний импорт: {html.escape(import_text)}</p>
</div>
<div class="card">
  <div class="filters">
    <div class="filter-field filter-field-search">
      <label for="text-filter">Поиск</label>
      <input id="text-filter" type="search" placeholder="ФИО, email, логин, подразделение или должность">
    </div>
    <div class="filter-field">
      <label for="test-filter">Тест</label>
      <select id="test-filter">{''.join(template_options)}</select>
    </div>
    <div class="filter-field">
      <label for="status-filter">Статус</label>
      <select id="status-filter">{status_options}</select>
    </div>
  </div>
  <p class="small" id="visible-count"></p>
  <table id="result-table">
    <thead><tr><th>ФИО</th><th>Email</th><th>Логин</th><th>Подразделение</th><th>Должность</th><th>Тест</th><th>Статус</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</div>
<script>
const textFilter = document.getElementById('text-filter');
const testFilter = document.getElementById('test-filter');
const statusFilter = document.getElementById('status-filter');
const visibleCount = document.getElementById('visible-count');
const rows = [...document.querySelectorAll('#result-table tbody tr')];

function applyFilters() {{
  const query = textFilter.value.trim().toLowerCase();
  const templateId = testFilter.value;
  const status = statusFilter.value;
  let count = 0;

  rows.forEach(row => {{
    const matchesText = !query || row.textContent.toLowerCase().includes(query);
    const matchesTest = !templateId || row.dataset.templateId === templateId;
    const matchesStatus = !status || row.dataset.status === status;
    row.hidden = !(matchesText && matchesTest && matchesStatus);
    if (!row.hidden) count += 1;
  }});

  visibleCount.textContent = `Показано записей: ${{count}}`;
}}

textFilter.addEventListener('input', applyFilters);
testFilter.addEventListener('change', applyFilters);
statusFilter.addEventListener('change', applyFilters);
applyFilters();
</script>
</body>
</html>"""
    output.write_text(page, encoding="utf-8")
