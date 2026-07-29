from __future__ import annotations

from datetime import datetime, timezone

from jinja2 import Environment, StrictUndefined

from .db import Database


DEFAULT_INVITATION_BODY = """<!doctype html>
<html lang="ru">
  <body style="font-family: Arial, sans-serif; font-size: 14px; line-height: 1.5; color: #222;">
    <p>Здравствуйте, {{ fio }}!</p>
    <p>Вам назначено обязательное тестирование <strong>«{{ test_name }}»</strong>.</p>
    <p>Просим пройти тестирование в установленный срок.</p>
  </body>
</html>"""

DEFAULT_REMINDER_BODY = """<!doctype html>
<html lang="ru">
  <body style="font-family: Arial, sans-serif; font-size: 14px; line-height: 1.5; color: #222;">
    <p>Здравствуйте, {{ fio }}!</p>
    <p>Напоминаем о необходимости пройти тестирование <strong>«{{ test_name }}»</strong>.</p>
    <p>Просим завершить тестирование в установленный срок.</p>
  </body>
</html>"""

INITIAL_TEST_MAIL: dict[str, dict[str, str]] = {
    "antiterror": {
        "invitation_subject": (
            "Обязательное прохождение курса и тестирования "
            "по антитеррористической безопасности"
        ),
        "reminder_subject": (
            "Напоминание о прохождении курса и тестирования "
            "по антитеррористической безопасности"
        ),
    },
    "anticorruption": {
        "invitation_subject": (
            "Обязательное прохождение тренинга и тестирования "
            "по антикоррупционной безопасности"
        ),
        "reminder_subject": (
            "Напоминание о прохождении тренинга и тестирования "
            "по антикоррупционной безопасности"
        ),
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_mail_templates(db: Database, templates: list[dict]) -> None:
    defaults: list[tuple[str, str, str, str, int]] = []

    for template in templates:
        tid = str(template["id"])
        name = str(template.get("name") or tid)
        initial = INITIAL_TEST_MAIL.get(tid, {})

        invitation_subject = initial.get(
            "invitation_subject",
            f"Приглашение к прохождению тестирования – {name}",
        )
        reminder_subject = initial.get(
            "reminder_subject",
            f"Напоминание о прохождении тестирования – {name}",
        )

        defaults.append(
            ("invitation", tid, invitation_subject, DEFAULT_INVITATION_BODY, 1)
        )
        defaults.append(
            ("reminder", tid, reminder_subject, DEFAULT_REMINDER_BODY, 1)
        )

    defaults.extend([
        ("reviewer", "*", "Требуется контроль прохождения тестирования – {{ test_name }}",
         "<p>Здравствуйте!</p>"
         "<p>Следующие работники не прошли тестирование <strong>«{{ test_name }}»</strong> после установленного количества напоминаний.</p>"
         "<p>Количество работников: <strong>{{ employees_count }}</strong>.</p>"
         "{% for employee in employees %}"
         "<div style='margin:0 0 16px 0;padding:12px;border:1px solid #d9d9d9;border-radius:6px'>"
         "<p style='margin:0 0 6px 0'><strong>ФИО:</strong> {{ employee.fio }}</p>"
         "<p style='margin:0 0 6px 0'><strong>E-mail:</strong> {{ employee.email }}</p>"
         "{% if employee.department %}<p style='margin:0 0 6px 0'><strong>Подразделение:</strong> {{ employee.department }}</p>{% endif %}"
         "{% if employee.position %}<p style='margin:0 0 6px 0'><strong>Должность:</strong> {{ employee.position }}</p>{% endif %}"
         "<p style='margin:0 0 6px 0'><strong>Количество направленных напоминаний:</strong> {{ employee.reminder_count }}</p>"
         "{% if employee.first_reminder_at %}<p style='margin:0 0 6px 0'><strong>Первое напоминание:</strong> {{ employee.first_reminder_at }}</p>{% endif %}"
         "{% if employee.last_reminder_at %}<p style='margin:0'><strong>Последнее напоминание:</strong> {{ employee.last_reminder_at }}</p>{% endif %}"
         "</div>{% endfor %}"
         "<p>Просим проконтролировать прохождение тестирования указанными работниками.</p>"
         "<p>Письмо сформировано автоматически.</p>", 1),
        ("technical", "*", "{{ subject }}",
         "<p>При выполнении рассылки обнаружено ошибок: <strong>{{ errors_count }}</strong>.</p>"
         "{% for error in errors %}"
         "<div style='margin:0 0 16px 0;padding:12px;border:1px solid #d9d9d9;border-radius:6px'>"
         "<p style='margin:0 0 6px 0'><strong>ФИО:</strong> {{ error.fio }}</p>"
         "<p style='margin:0 0 6px 0'><strong>E-mail:</strong> {{ error.email }}</p>"
         "<p style='margin:0 0 6px 0'><strong>Тип ошибки:</strong> {{ error.error_type }}</p>"
         "{% if error.error_text %}<p style='margin:0 0 6px 0'><strong>Текст ошибки:</strong></p>"
         "<pre style='white-space:pre-wrap;margin:0 0 6px 0'>{{ error.error_text }}</pre>{% endif %}"
         "<p style='margin:0'><strong>Дата обнаружения:</strong> {{ error.detected_at }}</p>"
         "</div>{% endfor %}", 1),
    ])

    with db.connect() as c:
        for kind, tid, subject, body, enabled in defaults:
            c.execute(
                """INSERT OR IGNORE INTO mail_templates(
                       kind, template_id, subject, body_html, enabled, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (kind, tid, subject, body, enabled, _now()),
            )

        # Однократное безопасное обновление прежнего стандартного технического шаблона.
        # Пользовательские шаблоны не изменяются.
        old_technical_body = "<p>ФИО: {{ fio }}</p><p>E-mail: {{ email }}</p><p>Тип ошибки: {{ error_type }}</p>{% if error_text %}<pre style='white-space:pre-wrap'>{{ error_text }}</pre>{% endif %}<p>Дата обнаружения: {{ detected_at }}</p>"
        new_technical_body = next(
            body
            for kind, tid, _subject, body, _enabled in defaults
            if kind == "technical" and tid == "*"
        )
        c.execute(
            """UPDATE mail_templates SET body_html = ?, updated_at = ?
               WHERE kind = 'technical' AND template_id = '*' AND body_html = ?""",
            (new_technical_body, _now(), old_technical_body),
        )

        # Однократное безопасное обновление прежнего стандартного шаблона контролирующих.
        # Пользовательские шаблоны не изменяются.
        old_reviewer_subject = "Работник игнорирует прохождение теста «{{ test_name }}»"
        old_reviewer_body = "<p>Работник не завершил обязательное тестирование после всех предусмотренных напоминаний.</p><p>ФИО: {{ fio }}<br>E-mail: {{ email }}<br>Подразделение: {{ department }}<br>Должность: {{ position }}<br>Тест: {{ test_name }}<br>Количество напоминаний: {{ reminder_count }}<br>Первое напоминание: {{ first_reminder_at }}<br>Последнее напоминание: {{ last_reminder_at }}</p>"
        new_reviewer = next(
            (subject, body)
            for kind, tid, subject, body, _enabled in defaults
            if kind == "reviewer" and tid == "*"
        )
        c.execute(
            """UPDATE mail_templates SET subject = ?, body_html = ?, updated_at = ?
               WHERE kind = 'reviewer' AND template_id = '*'
                 AND subject = ? AND body_html = ?""",
            (
                new_reviewer[0],
                new_reviewer[1],
                _now(),
                old_reviewer_subject,
                old_reviewer_body,
            ),
        )


def get_mail_template(db: Database, kind: str, template_id: str = "*") -> dict:
    with db.connect() as c:
        row = c.execute(
            "SELECT * FROM mail_templates WHERE kind=? AND template_id=?",
            (kind, template_id),
        ).fetchone()
    return dict(row) if row else {
        "kind": kind,
        "template_id": template_id,
        "subject": "",
        "body_html": "",
        "enabled": 0,
    }


def render_mail_template(
    db: Database,
    kind: str,
    template_id: str,
    context: dict,
) -> tuple[str, str, bool]:
    item = get_mail_template(db, kind, template_id)
    env = Environment(undefined=StrictUndefined, autoescape=True)
    subject = env.from_string(str(item["subject"])).render(**context)
    body = env.from_string(str(item["body_html"])).render(**context)
    return subject, body, bool(item["enabled"])
