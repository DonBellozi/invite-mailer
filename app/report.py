from __future__ import annotations

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
input { width: 100%; max-width: 520px; padding: 10px; margin: 8px 0 14px; border: 1px solid #ccd2da; border-radius: 6px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 9px 8px; border-bottom: 1px solid #e1e5ea; text-align: left; vertical-align: top; }
th { position: sticky; top: 0; background: #f0f2f5; }
.status-sent { color: #176b36; font-weight: 600; }
.status-wait { color: #8a5b00; font-weight: 600; }
.status-error { color: #a21d1d; font-weight: 600; }
.small { color: #667085; font-size: 12px; }
"""


def _fmt_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


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

        rows: list[str] = []
        for employee in employees:
            for template in templates:
                if not template.get("enabled", True):
                    continue
                latest = connection.execute(
                    """
                    SELECT * FROM notification_history
                    WHERE worker_key = ? AND employment_seq = ? AND template_id = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (employee["worker_key"], employee["employment_seq"], template["id"]),
                ).fetchone()

                if not employee["email"]:
                    status = "Нет адреса электронной почты"
                    status_class = "status-error"
                elif latest and latest["status"] == "sent":
                    status = f"Отправлено {_fmt_date(latest['sent_at'])}"
                    status_class = "status-sent"
                elif latest and latest["status"] == "error":
                    status = f"Ошибка: {latest['error_text'] or 'неизвестная ошибка'}"
                    status_class = "status-error"
                else:
                    status = "Ожидает отправки"
                    status_class = "status-wait"

                values = [
                    employee["fio"],
                    employee["email"] or "",
                    employee["login"] or "",
                    employee["department"] or "",
                    employee["position"] or "",
                    template.get("name", template["id"]),
                ]
                cells = "".join(f"<td>{html.escape(str(value))}</td>" for value in values)
                rows.append(f"<tr>{cells}<td class='{status_class}'>{html.escape(status)}</td></tr>")

    import_text = "Нет успешно загруженных файлов"
    if last_import:
        import_text = f"{_fmt_date(last_import['imported_at'])}, сотрудников: {last_import['row_count']}"

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
  <input id="filter" type="search" placeholder="Поиск по ФИО, email, подразделению или тесту">
  <table id="result-table">
    <thead><tr><th>ФИО</th><th>Email</th><th>Логин</th><th>Подразделение</th><th>Должность</th><th>Тест</th><th>Статус</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</div>
<script>
const input = document.getElementById('filter');
const rows = [...document.querySelectorAll('#result-table tbody tr')];
input.addEventListener('input', () => {{
  const query = input.value.toLowerCase();
  rows.forEach(row => row.hidden = !row.textContent.toLowerCase().includes(query));
}});
</script>
</body>
</html>"""
    output.write_text(page, encoding="utf-8")
