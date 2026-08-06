from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from . import admin_web as legacy
from .placements import normalize_state, state_display


app = legacy.app


class TestAllowedStatesRequest(BaseModel):
    allowed_states: list[str] = []


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _test_exists(connection, test_id: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM test_definitions WHERE id = ?",
        (str(test_id),),
    ).fetchone() is not None


def _observed_states(connection) -> dict[str, str]:
    result: dict[str, str] = {}
    rows = connection.execute(
        "SELECT DISTINCT state FROM employee_placements ORDER BY state COLLATE NOCASE"
    ).fetchall()
    for row in rows:
        raw = str(row["state"] or "").strip()
        result.setdefault(normalize_state(raw), raw)
    return result


def _state_payload(test_id: str | None = None) -> dict:
    db = legacy.database()
    with db.connect() as connection:
        states = _observed_states(connection)
        configured = False
        allowed: set[str] = set()

        if test_id is not None:
            if not _test_exists(connection, test_id):
                raise HTTPException(status_code=404, detail="Тест не найден")
            policy = connection.execute(
                "SELECT configured FROM test_state_policies WHERE test_id = ?",
                (str(test_id),),
            ).fetchone()
            configured = bool(policy and policy["configured"])
            rows = connection.execute(
                """
                SELECT state_normalized, state_display
                FROM test_allowed_states
                WHERE test_id = ?
                ORDER BY state_display COLLATE NOCASE
                """,
                (str(test_id),),
            ).fetchall()
            for row in rows:
                key = str(row["state_normalized"] or "")
                display = str(row["state_display"] or "").strip()
                states.setdefault(key, display)
                allowed.add(key)

        items = [
            {
                "value": display,
                "label": state_display(display),
                "allowed": (key in allowed) if configured else True,
            }
            for key, display in sorted(
                states.items(),
                key=lambda item: state_display(item[1]).casefold(),
            )
        ]

    return {
        "configured": configured,
        "items": items,
        "unknown_states_default": "denied" if configured else "allowed",
    }


@app.get("/api/settings/employee-states")
def list_employee_states(_: Annotated[str, Depends(legacy.require_admin)]):
    return _state_payload()


@app.get("/api/settings/tests/{test_id}/employee-states")
def read_test_employee_states(
    test_id: str,
    _: Annotated[str, Depends(legacy.require_admin)],
):
    return _state_payload(test_id)


@app.put("/api/settings/tests/{test_id}/employee-states")
def write_test_employee_states(
    test_id: str,
    payload: TestAllowedStatesRequest,
    _: Annotated[str, Depends(legacy.require_admin)],
):
    normalized: dict[str, str] = {}
    for value in payload.allowed_states:
        display = " ".join(str(value or "").split()).strip()
        normalized[normalize_state(display)] = display

    now = _utc_now()
    db = legacy.database()
    with db.connect() as connection:
        if not _test_exists(connection, test_id):
            raise HTTPException(status_code=404, detail="Тест не найден")
        connection.execute(
            """
            INSERT INTO test_state_policies(test_id, configured, updated_at)
            VALUES (?, 1, ?)
            ON CONFLICT(test_id) DO UPDATE SET
                configured = 1,
                updated_at = excluded.updated_at
            """,
            (str(test_id), now),
        )
        connection.execute(
            "DELETE FROM test_allowed_states WHERE test_id = ?",
            (str(test_id),),
        )
        connection.executemany(
            """
            INSERT INTO test_allowed_states(
                test_id, state_normalized, state_display, updated_at
            ) VALUES (?, ?, ?, ?)
            """,
            [
                (str(test_id), key, display, now)
                for key, display in normalized.items()
            ],
        )

    legacy.rebuild_report(
        legacy.settings(),
        legacy.database(),
        sync_indigo=False,
    )
    return _state_payload(test_id)


@app.get("/api/version")
def application_version():
    return {"version": "2.8.0"}


_STATE_CSS = r"""
.state-toolbar{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 12px}
.state-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:8px 14px;padding:12px;border:1px solid #d0d5dd;border-radius:7px;background:#f9fafb}
.state-item{display:flex;align-items:flex-start;gap:8px;padding:3px 0;font-size:13px;font-weight:400}
.state-item input{width:auto;margin-top:2px}
.state-empty{color:#667085;font-size:13px}
.state-policy-note{margin-top:8px;color:#667085;font-size:12px;line-height:1.45}
"""

_STATE_SECTION = r"""
<section class="section" id="employeeStateSection">
  <h3>Состояния работников из 1С</h3>
  <div class="small">Работники остаются участниками теста и учитываются в счетчиках. При неотмеченном состоянии первоначальное приглашение и напоминания временно не отправляются.</div>
  <div class="state-toolbar">
    <button id="stateSelectAll" class="button" type="button">Выбрать все</button>
    <button id="stateOnlyWork" class="button" type="button">Только «Работа»</button>
    <button id="stateSave" class="button primary" type="button">Сохранить состояния</button>
  </div>
  <div id="stateList" class="state-list"><div class="state-empty">Откройте сохраненный тест.</div></div>
  <div id="statePolicyNote" class="state-policy-note"></div>
</section>
"""

_STATE_SCRIPT = r"""
<script>
(() => {
  const stateList = document.getElementById('stateList');
  const stateNote = document.getElementById('statePolicyNote');
  const stateSave = document.getElementById('stateSave');
  let loadedStateTestId = null;

  function stateCheckboxes() {
    return [...stateList.querySelectorAll('input[type="checkbox"][data-state-value]')];
  }

  function renderStates(payload, testId) {
    loadedStateTestId = testId || null;
    stateList.innerHTML = '';
    const items = payload.items || [];
    if (!items.length) {
      const empty = document.createElement('div');
      empty.className = 'state-empty';
      empty.textContent = 'В последнем XLSX состояния работников пока не обнаружены.';
      stateList.appendChild(empty);
    } else {
      for (const item of items) {
        const label = document.createElement('label');
        label.className = 'state-item';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = !!item.allowed;
        checkbox.dataset.stateValue = String(item.value ?? '');
        const text = document.createElement('span');
        text.textContent = item.label || 'Состояние не указано';
        label.append(checkbox, text);
        stateList.appendChild(label);
      }
    }
    stateSave.disabled = !loadedStateTestId;
    stateNote.textContent = payload.configured
      ? 'Список настроен. Новые состояния из 1С будут приостанавливать уведомления, пока администратор не разрешит их.'
      : 'Список еще не сохранялся – для совместимости временно разрешены все найденные состояния.';
  }

  async function loadStates(testId) {
    if (!testId) {
      const payload = await api('/api/settings/employee-states');
      renderStates(payload, null);
      return;
    }
    const payload = await api(`/api/settings/tests/${encodeURIComponent(testId)}/employee-states`);
    renderStates(payload, testId);
  }

  document.getElementById('stateSelectAll').onclick = () => {
    stateCheckboxes().forEach(item => item.checked = true);
  };
  document.getElementById('stateOnlyWork').onclick = () => {
    stateCheckboxes().forEach(item => {
      item.checked = String(item.dataset.stateValue || '').trim().toLocaleLowerCase('ru-RU') === 'работа';
    });
  };
  stateSave.onclick = async () => {
    if (!loadedStateTestId) {
      msg('Сначала сохраните тест, затем настройте состояния работников.','error');
      return;
    }
    try {
      const allowedStates = stateCheckboxes()
        .filter(item => item.checked)
        .map(item => item.dataset.stateValue || '');
      const payload = await api(
        `/api/settings/tests/${encodeURIComponent(loadedStateTestId)}/employee-states`,
        {
          method: 'PUT',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({allowed_states: allowedStates}),
        },
      );
      renderStates(payload, loadedStateTestId);
      msg('Разрешенные состояния работников сохранены.','success');
    } catch (error) {
      msg(error.message,'error');
    }
  };

  const originalFill = fill;
  fill = function(bundle) {
    originalFill(bundle);
    loadStates(bundle.test.id).catch(error => msg(error.message,'error'));
  };

  const originalAddNew = addNew;
  addNew = function() {
    originalAddNew();
    loadStates(null).catch(error => msg(error.message,'error'));
  };
  document.getElementById('add').onclick = addNew;

  stateSave.disabled = true;
})();
</script>
"""


def _inject_state_editor(source: str) -> str:
    result = source
    result = result.replace("</style></head>", _STATE_CSS + "</style></head>", 1)
    result = result.replace(
        '<section class="section"><h3>Первоначальное приглашение',
        _STATE_SECTION + '<section class="section"><h3>Первоначальное приглашение',
        1,
    )
    result = result.replace("</body></html>", _STATE_SCRIPT + "</body></html>", 1)
    return result


legacy.TESTS_HTML = _inject_state_editor(legacy.TESTS_HTML)
