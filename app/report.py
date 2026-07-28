from __future__ import annotations

import fnmatch
import html
from datetime import datetime
from pathlib import Path

from .audience import is_explicit_template
from .db import Database
from .indigo import ResultSummary, summarize_employee_result


CSS = """
body {
  font-family: Arial, sans-serif;
  margin: 24px;
  color: #222;
  background: #f5f6f8;
}

.card {
  background: white;
  border-radius: 10px;
  padding: 18px;
  margin-bottom: 18px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, .08);
}

h1,
h2.dashboard-title {
  margin: 0 0 14px;
  font-size: 24px;
  font-weight: 700;
  line-height: 1.2;
}

.dashboard {
  display: flex;
  align-items: stretch;
  gap: 0;
}

.dashboard-global {
  flex: 1 1 46%;
  min-width: 430px;
  padding-right: 18px;
  box-sizing: border-box;
}

.dashboard-test {
  flex: 1 1 42%;
  min-width: 430px;
  padding: 0 18px;
  border-left: 2px solid #e5e7eb;
  box-sizing: border-box;
}

.dashboard-test-inner {
  height: 100%;
  padding: 0;
  box-sizing: border-box;
}

.dashboard-actions {
  flex: 0 0 190px;
  min-width: 190px;
  padding-left: 18px;
  border-left: 2px solid #e5e7eb;
  display: flex;
  flex-direction: column;
  gap: 10px;
  box-sizing: border-box;
}

.admin-link {
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

.admin-link:hover {
  background: #f2f4f7;
}

.test-header-line {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.test-header-line .dashboard-title {
  flex: 1 1 auto;
}

.export-actions {
  display: flex;
  gap: 8px;
  flex-wrap: nowrap;
}

.export-link {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 34px;
  box-sizing: border-box;
  padding: 7px 10px;
  border: 1px solid #98a2b3;
  border-radius: 7px;
  background: #fff;
  color: #344054;
  text-decoration: none;
  white-space: nowrap;
  font-size: 12px;
  font-weight: 600;
}

.export-link:hover {
  background: #f2f4f7;
  border-color: #667085;
}

.export-link-xlsx:hover {
  color: #176b36;
  border-color: #32a852;
  background: #f0fdf4;
}

.export-link-pdf:hover {
  color: #b42318;
  border-color: #f04438;
  background: #fef3f2;
}

.summary {
  display: flex;
  gap: 10px;
  flex-wrap: nowrap;
}

.summary-global {
  max-width: 500px;
}

.metric {
  min-width: 0;
  flex: 1;
  background: #eef2f7;
  padding: 11px 12px;
  border-radius: 8px;
  border: 1px solid transparent;
  text-align: left;
  font: inherit;
  color: inherit;
}

.metric strong {
  display: block;
  font-size: 22px;
  line-height: 1.1;
}

.metric-test {
  background: #f3f1e5;
}

.metric-warning {
  background: #fff8f1;
  border-color: #f3d6bd;
}

.metric-button {
  cursor: pointer;
}

.metric-button:hover {
  border-color: #d0d5dd;
  background: #e7edf5;
}

.metric-button.active {
  border-color: #d92d20;
  background: #fef3f2;
  color: #912018;
  box-shadow: 0 0 0 1px rgba(217, 45, 32, .08);
}

.dashboard-meta {
  margin: 10px 0 0;
  white-space: nowrap;
}

.meta-separator {
  margin: 0 7px;
  color: #98a2b3;
}

.filters {
  display: flex;
  gap: 12px;
  align-items: end;
  flex-wrap: wrap;
  margin: 8px 0 14px;
}

.filter-field {
  min-width: 220px;
  flex: 1;
}

.filter-field-search {
  flex: 2;
  min-width: 320px;
}

.filter-field label {
  display: block;
  margin-bottom: 5px;
  color: #475467;
  font-size: 12px;
  font-weight: 600;
}

input,
select {
  width: 100%;
  box-sizing: border-box;
  padding: 10px;
  border: 1px solid #ccd2da;
  border-radius: 6px;
  background: #fff;
}

.table-wrap {
  overflow-x: auto;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

th,
td {
  padding: 9px 8px;
  border-bottom: 1px solid #e1e5ea;
  text-align: left;
  vertical-align: top;
}

th {
  position: sticky;
  top: 0;
  background: #f0f2f5;
}

.status-completed {
  color: #176b36;
  font-weight: 600;
}

.status-failed {
  color: #8a5b00;
  font-weight: 600;
}

.status-sent {
  color: #176b36;
  font-weight: 600;
}

.status-wait {
  color: #8a5b00;
  font-weight: 600;
}

.status-error {
  color: #a21d1d;
  font-weight: 600;
}

.status-inactive {
  color: #667085;
  font-weight: 600;
}

.small {
  color: #667085;
  font-size: 12px;
}

.sync-error {
  color: #a21d1d;
}

.hidden {
  display: none !important;
}

@media (max-width: 1240px) {
  .dashboard {
    flex-wrap: wrap;
    gap: 16px;
  }

  .dashboard-global,
  .dashboard-test {
    min-width: calc(50% - 8px);
    padding: 0;
    border-left: 0;
  }

  .dashboard-test {
    border-left: 2px solid #e5e7eb;
    padding-left: 18px;
  }

  .dashboard-actions {
    flex: 1 1 100%;
    min-width: 100%;
    padding: 16px 0 0;
    border-left: 0;
    border-top: 2px solid #e5e7eb;
    flex-direction: row;
  }

  .dashboard-actions .admin-link {
    max-width: 220px;
  }
}

@media (max-width: 820px) {
  .dashboard-global,
  .dashboard-test {
    min-width: 100%;
    padding: 0;
    border-left: 0;
  }

  .dashboard-test {
    border-top: 2px solid #e5e7eb;
    padding-top: 16px;
  }

  .dashboard-actions {
    flex-direction: column;
  }

  .dashboard-actions .admin-link {
    max-width: none;
  }

  .dashboard-meta {
    white-space: normal;
  }
}
"""


def _fmt_date(value: str | None, include_time: bool = True) -> str:
    if not value:
        return ""

    try:
        pattern = "%d.%m.%Y %H:%M" if include_time else "%d.%m.%Y"
        return datetime.fromisoformat(value).strftime(pattern)
    except ValueError:
        return value


def _fmt_percent(value: float | None) -> str:
    if value is None:
        return ""

    if abs(value - round(value)) < 0.00001:
        return str(int(round(value)))

    return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def _department_applies(
    department: str | None,
    template: dict,
) -> bool:
    rule = template.get("departments", {})
    value = (department or "").lower()

    includes = rule.get("include") or ["*"]
    excludes = rule.get("exclude") or []

    included = any(
        fnmatch.fnmatch(value, str(pattern).lower())
        for pattern in includes
    )

    excluded = any(
        fnmatch.fnmatch(value, str(pattern).lower())
        for pattern in excludes
    )

    return included and not excluded


def _method_label(method: str | None) -> str:
    if method == "manual_seed":
        return "вручную"

    if method == "automatic":
        return "автоматически"

    return method or ""


def _row_status(
    employee,
    latest,
    result: ResultSummary,
    escalation=None,
) -> tuple[str, str, str]:
    if not employee["active"]:
        return (
            "Сотрудник неактивен",
            "status-inactive",
            "inactive",
        )

    if result.status == "completed":
        date_text = _fmt_date(
            result.completed_at,
            include_time=False,
        )

        return (
            f"Пройден ({date_text})",
            "status-completed",
            "completed",
        )

    if result.status == "failed":
        return (
            "Не прошел",
            "status-failed",
            "failed",
        )

    if escalation:
        return (
            "Игнорирует прохождение",
            "status-error",
            "ignoring",
        )

    if not employee["email"]:
        return (
            "Нет адреса электронной почты",
            "status-error",
            "no_email",
        )

    if latest:
        raw_status = str(
            latest["status"] or ""
        ).strip().lower()

        if raw_status in {
            "completed",
            "done",
            "passed",
        }:
            return (
                "Пройден",
                "status-completed",
                "completed",
            )

        if raw_status in {
            "failed",
            "not_passed",
        }:
            return (
                "Не прошел",
                "status-failed",
                "failed",
            )

        if raw_status == "sent":
            method = _method_label(latest["method"])
            suffix = f" – {method}" if method else ""

            return (
                f"Отправлено "
                f"{_fmt_date(latest['sent_at'])}"
                f"{suffix}",
                "status-sent",
                "sent",
            )

        if raw_status == "error":
            error_text = (
                latest["error_text"]
                or "неизвестная ошибка"
            )

            return (
                f"Ошибка: {error_text}",
                "status-error",
                "error",
            )

    return (
        "Ожидает отправки",
        "status-wait",
        "waiting",
    )


def _report_employees(
    connection,
    template: dict,
):
    if is_explicit_template(template):
        return connection.execute(
            """
            SELECT e.*
            FROM test_assignments a
            JOIN employees e
              ON e.worker_key = a.worker_key
             AND e.employment_seq = a.employment_seq
            WHERE a.template_id = ?
              AND a.active = 1
              AND e.active = 1
            ORDER BY e.department, e.fio
            """,
            (template["id"],),
        ).fetchall()

    employees = connection.execute(
        """
        SELECT *
        FROM employees
        WHERE active = 1
        ORDER BY department, fio
        """
    ).fetchall()

    return [
        employee
        for employee in employees
        if _department_applies(
            employee["department"],
            template,
        )
    ]


def build_report(
    db: Database,
    templates: list[dict],
    title: str,
    output: Path,
    indigo_enabled: bool = False,
) -> None:
    output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with db.connect() as connection:
        employee_count = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM employees
            WHERE active = 1
            """
        ).fetchone()["count"]

        last_import = connection.execute(
            """
            SELECT *
            FROM imports
            WHERE status = 'success'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        missing_email = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM employees
            WHERE active = 1
              AND (
                    email IS NULL
                    OR email = ''
                  )
            """
        ).fetchone()["count"]

        indigo_last_sync = connection.execute(
            """
            SELECT value
            FROM app_state
            WHERE key = 'indigo_last_sync'
            """
        ).fetchone()

        indigo_last_error = connection.execute(
            """
            SELECT value
            FROM app_state
            WHERE key = 'indigo_last_error'
            """
        ).fetchone()

        history_template_ids = {
            row["template_id"]
            for row in connection.execute(
                """
                SELECT DISTINCT template_id
                FROM notification_history
                """
            ).fetchall()
        }

        assignment_template_ids = {
            row["template_id"]
            for row in connection.execute(
                """
                SELECT DISTINCT template_id
                FROM test_assignments
                WHERE active = 1
                """
            ).fetchall()
        }

        visible_templates = [
            template
            for template in templates
            if template.get("enabled", True)
            or template["id"] in history_template_ids
            or template["id"] in assignment_template_ids
        ]

        rows: list[str] = []
        participant_counts: dict[str, int] = {}
        error_count = 0

        for template in visible_templates:
            employees = _report_employees(
                connection,
                template,
            )

            participant_counts[
                str(template["id"])
            ] = sum(
                1
                for employee in employees
                if employee["email"]
            )

            for employee in employees:
                latest = connection.execute(
                    """
                    SELECT *
                    FROM notification_history
                    WHERE worker_key = ?
                      AND employment_seq = ?
                      AND template_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (
                        employee["worker_key"],
                        employee["employment_seq"],
                        template["id"],
                    ),
                ).fetchone()

                result = summarize_employee_result(
                    connection,
                    employee,
                    template,
                )
                escalation = connection.execute(
                    """SELECT id FROM reviewer_notification_queue
                       WHERE worker_key = ? AND employment_seq = ? AND template_id = ?
                       LIMIT 1""",
                    (employee["worker_key"], employee["employment_seq"], template["id"]),
                ).fetchone()

                (
                    status,
                    status_class,
                    status_key,
                ) = _row_status(
                    employee,
                    latest,
                    result,
                    escalation,
                )

                if status_key == "error":
                    error_count += 1

                grade = result.grade or ""
                percent = _fmt_percent(result.percent)

                values = [
                    employee["fio"],
                    employee["email"] or "",
                    employee["login"] or "",
                    employee["department"] or "",
                    employee["position"] or "",
                    template.get(
                        "name",
                        template["id"],
                    ),
                ]

                cells = "".join(
                    f"<td>{html.escape(str(value))}</td>"
                    for value in values
                )

                template_id = html.escape(
                    str(template["id"]),
                    quote=True,
                )

                template_name = html.escape(
                    str(
                        template.get(
                            "name",
                            template["id"],
                        )
                    ),
                    quote=True,
                )

                escaped_status_key = html.escape(
                    status_key,
                    quote=True,
                )

                has_email = "1" if employee["email"] else "0"

                rows.append(
                    f"<tr "
                    f"data-template-id='{template_id}' "
                    f"data-template-name='{template_name}' "
                    f"data-has-email='{has_email}' "
                    f"data-status='{escaped_status_key}'>"
                    f"{cells}"
                    f"<td class='{status_class}'>"
                    f"{html.escape(status)}"
                    f"</td>"
                    f"<td>{html.escape(grade)}</td>"
                    f"<td>{html.escape(percent)}</td>"
                    f"</tr>"
                )

    import_text = "нет данных"

    if last_import:
        import_text = _fmt_date(
            last_import["imported_at"]
        )

    if not indigo_enabled:
        indigo_text = "отключены"

    elif indigo_last_error:
        indigo_text = (
            "<span class='sync-error'>"
            "ошибка обновления, "
            "используется последний кеш"
            "</span>"
        )

    elif indigo_last_sync:
        indigo_text = html.escape(
            _fmt_date(
                indigo_last_sync["value"]
            )
        )

    else:
        indigo_text = "еще не загружены"

    template_options = [
        "<option value=''>Все тесты</option>"
    ]

    for template in visible_templates:
        template_id_raw = str(template["id"])

        template_id = html.escape(
            template_id_raw,
            quote=True,
        )

        template_name = html.escape(
            str(
                template.get(
                    "name",
                    template["id"],
                )
            ),
            quote=True,
        )

        count = participant_counts.get(
            template_id_raw,
            0,
        )

        template_options.append(
            f"<option "
            f"value='{template_id}' "
            f"data-name='{template_name}'>"
            f"{template_name} ({count})"
            f"</option>"
        )

    status_options = """
<option value="">Все статусы</option>
<option value="completed">Пройден</option>
<option value="failed">Не прошел</option>
<option value="ignoring">Игнорирует прохождение</option>
<option value="sent">Отправлено, ожидает выполнения</option>
<option value="waiting">Ожидает отправки</option>
<option value="error">Ошибка отправки</option>
<option value="no_email">Нет e-mail</option>
""".strip()

    page = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta
  name="viewport"
  content="width=device-width, initial-scale=1"
>
<title>{html.escape(title)}</title>
<style>{CSS}</style>
</head>

<body>

<div class="card">
  <div class="dashboard">

    <section class="dashboard-global">
      <h1>{html.escape(title)}</h1>

      <div class="summary summary-global">
        <div class="metric">
          <strong>{employee_count}</strong>
          всего работников
        </div>

        <div class="metric{' metric-warning' if missing_email else ''}">
          <strong>{missing_email}</strong>
          без e-mail
        </div>

        <button
          id="error-metric"
          class="metric metric-button"
          type="button"
        >
          <strong>{error_count}</strong>
          ошибки отправки
        </button>
      </div>

      <p class="small dashboard-meta">
        Импорт: {html.escape(import_text)}
        <span class="meta-separator">|</span>
        Результаты Indigo: {indigo_text}
      </p>
    </section>

    <section
      id="test-dashboard"
      class="dashboard-test hidden"
    >
      <div class="dashboard-test-inner">
    
        <div class="test-header-line">
          <h2
            id="test-dashboard-title"
            class="dashboard-title"
          >
            Тест
          </h2>
    
          <div class="export-actions">
            <a
              id="export-xlsx"
              class="export-link export-link-xlsx"
              href="#"
            >
              Экспорт XLSX
            </a>
    
            <a
              id="export-pdf"
              class="export-link export-link-pdf"
              href="#"
            >
              Экспорт PDF
            </a>
          </div>
        </div>
    
        <div class="summary">
          <div class="metric metric-test">
            <strong id="metric-participants">0</strong>
            участников
          </div>
    
          <div class="metric metric-test">
            <strong id="metric-completed">0</strong>
            пройдено
          </div>
    
          <div class="metric metric-test">
            <strong id="metric-failed">0</strong>
            не пройдено
          </div>
    
          <div class="metric metric-test">
            <strong id="metric-waiting">0</strong>
            ожидают
          </div>
        </div>
    
      </div>
    </section>

    <aside class="dashboard-actions">
      <a
        class="admin-link"
        href="/admin/"
      >
        Импорт участников
      </a>

      <a
        class="admin-link"
        href="/admin/settings/"
      >
        ⚙ Настройки
      </a>
    </aside>

  </div>
</div>

<div class="card">
  <div class="filters">

    <div class="filter-field filter-field-search">
      <label for="text-filter">
        Поиск
      </label>

      <input
        id="text-filter"
        type="search"
        placeholder="ФИО, e-mail, логин, подразделение или должность"
      >
    </div>

    <div class="filter-field">
      <label for="test-filter">
        Тест
      </label>

      <select id="test-filter">
        {''.join(template_options)}
      </select>
    </div>

    <div class="filter-field">
      <label for="status-filter">
        Статус
      </label>

      <select id="status-filter">
        {status_options}
      </select>
    </div>

  </div>

  <p
    class="small"
    id="visible-count"
  ></p>

  <div class="table-wrap">
    <table id="result-table">
      <thead>
        <tr>
          <th>ФИО</th>
          <th>E-mail</th>
          <th>Логин</th>
          <th>Подразделение</th>
          <th>Должность</th>
          <th>Тест</th>
          <th>Статус</th>
          <th>Оценка</th>
          <th>Результат, %</th>
        </tr>
      </thead>

      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
  </div>
</div>

<script>
const textFilter =
  document.getElementById('text-filter');

const testFilter =
  document.getElementById('test-filter');

const statusFilter =
  document.getElementById('status-filter');

const visibleCount =
  document.getElementById('visible-count');

const rows = [
  ...document.querySelectorAll(
    '#result-table tbody tr'
  )
];

const errorMetric =
  document.getElementById('error-metric');

const testDashboard =
  document.getElementById('test-dashboard');

const exportXlsx =
  document.getElementById('export-xlsx');

const exportPdf =
  document.getElementById('export-pdf');
  
function updateTestDashboard() {{
  const templateId = testFilter.value;

  if (!templateId) {{
    exportXlsx.removeAttribute('href');
    exportPdf.removeAttribute('href');
    testDashboard.classList.add('hidden');
    return;
  }}

  const option =
    testFilter.selectedOptions[0];

  const testRows = rows.filter(
    row => row.dataset.templateId === templateId
  );

  const completed = testRows.filter(
    row => row.dataset.status === 'completed'
  ).length;

  const failed = testRows.filter(
    row => row.dataset.status === 'failed'
  ).length;

  const participantRows = testRows.filter(
    row => row.dataset.hasEmail === '1'
  );

  const waiting = Math.max(
    0,
    participantRows.length - completed - failed
  );

  document.getElementById(
    'test-dashboard-title'
  ).textContent =
    `Тест: ${{
      option.dataset.name
      || option.textContent
    }}`;

  document.getElementById(
    'metric-participants'
  ).textContent = participantRows.length;

  document.getElementById(
    'metric-completed'
  ).textContent = completed;

  document.getElementById(
    'metric-failed'
  ).textContent = failed;

  document.getElementById(
    'metric-waiting'
  ).textContent = waiting;

  const encodedTemplateId =
    encodeURIComponent(templateId);

  exportXlsx.href =
    `/api/reports/${{encodedTemplateId}}/xlsx`;

  exportPdf.href =
    `/api/reports/${{encodedTemplateId}}/pdf`;

  testDashboard.classList.remove('hidden');
}}


function updateErrorMetricState() {{
  const active =
    !testFilter.value
    && statusFilter.value === 'error';

  errorMetric.classList.toggle(
    'active',
    active
  );
}}


function applyFilters() {{
  const query =
    textFilter.value.trim().toLowerCase();

  const templateId =
    testFilter.value;

  const status =
    statusFilter.value;

  let count = 0;

  rows.forEach(row => {{
    const matchesText =
      !query
      || row.textContent
        .toLowerCase()
        .includes(query);

    const matchesTest =
      !templateId
      || row.dataset.templateId === templateId;

    const matchesStatus =
      !status
      || row.dataset.status === status;

    row.hidden = !(
      matchesText
      && matchesTest
      && matchesStatus
    );

    if (!row.hidden) {{
      count += 1;
    }}
  }});

  visibleCount.textContent =
    `Показано записей: ${{count}}`;

  updateTestDashboard();
  updateErrorMetricState();
}}


errorMetric.addEventListener(
  'click',
  () => {{
    const alreadyActive =
      !testFilter.value
      && statusFilter.value === 'error';

    testFilter.value = '';

    statusFilter.value =
      alreadyActive
        ? ''
        : 'error';

    textFilter.value = '';

    applyFilters();
  }}
);


textFilter.addEventListener(
  'input',
  applyFilters
);

testFilter.addEventListener(
  'change',
  applyFilters
);

statusFilter.addEventListener(
  'change',
  applyFilters
);

applyFilters();
</script>

</body>
</html>
"""

    output.write_text(
        page,
        encoding="utf-8",
    )