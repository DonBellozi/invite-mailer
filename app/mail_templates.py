from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined

from .db import Database


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_file(path: Any) -> str:
    try:
        p = Path(str(path))
        return p.read_text(encoding="utf-8") if p.exists() else ""
    except Exception:
        return ""


def ensure_mail_templates(db: Database, templates: list[dict]) -> None:
    defaults: list[tuple[str, str, str, str, int]] = []
    for template in templates:
        tid = str(template["id"])
        name = str(template.get("name") or tid)
        original = _read_file(template.get("body_template"))
        if not original:
            original = "<p>Здравствуйте, {{ fio }}!</p><p>Вам назначено обязательное тестирование.</p>"
        defaults.append(("invitation", tid, str(template.get("subject") or name), original, 1))
        reminder_body = _read_file(template.get("reminder_body_template")) or (
            "<p><strong>Напоминаем о необходимости пройти назначенное тестирование.</strong></p>" + original
        )
        defaults.append(("reminder", tid, str(template.get("reminder_subject") or f"Напоминание: {template.get('subject') or name}"), reminder_body, 1))
    defaults.extend([
        ("reviewer", "*", "Работник игнорирует прохождение теста «{{ test_name }}»",
         "<p>Работник не завершил обязательное тестирование после всех предусмотренных напоминаний.</p><p>ФИО: {{ fio }}<br>E-mail: {{ email }}<br>Подразделение: {{ department }}<br>Должность: {{ position }}<br>Тест: {{ test_name }}<br>Количество напоминаний: {{ reminder_count }}<br>Первое напоминание: {{ first_reminder_at }}<br>Последнее напоминание: {{ last_reminder_at }}</p>", 1),
        ("technical", "*", "{{ subject }}",
         "<p>ФИО: {{ fio }}</p><p>E-mail: {{ email }}</p><p>Тип ошибки: {{ error_type }}</p>{% if error_text %}<pre style='white-space:pre-wrap'>{{ error_text }}</pre>{% endif %}<p>Дата обнаружения: {{ detected_at }}</p>", 1),
    ])
    with db.connect() as c:
        for kind, tid, subject, body, enabled in defaults:
            c.execute("""INSERT OR IGNORE INTO mail_templates(kind, template_id, subject, body_html, enabled, updated_at)
                         VALUES (?, ?, ?, ?, ?, ?)""", (kind, tid, subject, body, enabled, _now()))


def get_mail_template(db: Database, kind: str, template_id: str = "*") -> dict:
    with db.connect() as c:
        row = c.execute("SELECT * FROM mail_templates WHERE kind=? AND template_id=?", (kind, template_id)).fetchone()
    return dict(row) if row else {"kind": kind, "template_id": template_id, "subject": "", "body_html": "", "enabled": 0}


def render_mail_template(db: Database, kind: str, template_id: str, context: dict) -> tuple[str, str, bool]:
    item = get_mail_template(db, kind, template_id)
    env = Environment(undefined=StrictUndefined, autoescape=True)
    subject = env.from_string(str(item["subject"])).render(**context)
    body = env.from_string(str(item["body_html"])).render(**context)
    return subject, body, bool(item["enabled"])
