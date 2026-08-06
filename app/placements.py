from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any


UNSPECIFIED_STATE_LABEL = "Состояние не указано"


@dataclass(frozen=True)
class Placement:
    """Одно кадровое назначение работника."""

    department: str
    position: str
    state: str = ""
    sort_order: int = 0


@dataclass(frozen=True)
class NotificationAvailability:
    """Доступность уведомлений для работника по конкретному тесту."""

    eligible: tuple[Placement, ...]
    active: tuple[Placement, ...]
    paused: bool
    reasons: tuple[str, ...]



def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()



def normalize_state(value: str | None) -> str:
    return normalize_text(value).replace("ё", "е")



def state_display(value: str | None) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    return text or UNSPECIFIED_STATE_LABEL



def _employee_value(employee: Any, key: str, default: Any = None) -> Any:
    try:
        value = employee[key]
    except (KeyError, TypeError, IndexError):
        return default
    return default if value is None else value



def employee_placements(connection, employee) -> list[Placement]:
    """Возвращает назначения текущего периода трудоустройства.

    Для баз, обновленных со старой версии, предусмотрен резервный вариант через
    поля department и position таблицы employees. После очередного импорта
    основным источником становится employee_placements.
    """

    rows = connection.execute(
        """
        SELECT department, position, state, sort_order
        FROM employee_placements
        WHERE worker_key = ? AND employment_seq = ?
        ORDER BY sort_order, id
        """,
        (
            _employee_value(employee, "worker_key", ""),
            int(_employee_value(employee, "employment_seq", 1)),
        ),
    ).fetchall()

    if rows:
        return [
            Placement(
                department=str(row["department"] or ""),
                position=str(row["position"] or ""),
                state=str(row["state"] or ""),
                sort_order=int(row["sort_order"] or 0),
            )
            for row in rows
        ]

    return [
        Placement(
            department=str(_employee_value(employee, "department", "") or ""),
            position=str(_employee_value(employee, "position", "") or ""),
            state="",
            sort_order=0,
        )
    ]



def department_matches(department: str | None, rule: dict | None) -> bool:
    value = (department or "").lower()
    rule = rule or {}
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



def _is_explicit_template(template: dict | None) -> bool:
    if not template:
        return False
    audience = template.get("audience") or {}
    return str(audience.get("type", "")).strip().lower() == "explicit_list"



def _exclusion_rows(connection, template_id: str | None):
    if template_id is None:
        return connection.execute(
            """
            SELECT kind, normalized_fio, normalized_position
            FROM exclusions
            WHERE enabled = 1 AND template_id = '*'
            """
        ).fetchall()

    return connection.execute(
        """
        SELECT kind, normalized_fio, normalized_position
        FROM exclusions
        WHERE enabled = 1 AND template_id IN ('*', ?)
        """,
        (str(template_id),),
    ).fetchall()



def placement_is_excluded(
    employee,
    placement: Placement,
    exclusions,
) -> bool:
    fio = normalize_text(str(_employee_value(employee, "fio", "") or ""))
    position = normalize_text(placement.position)

    for exclusion in exclusions:
        if str(exclusion["normalized_position"] or "") != position:
            continue

        kind = str(exclusion["kind"] or "")
        if kind == "position":
            return True

        if (
            kind == "employee"
            and str(exclusion["normalized_fio"] or "") == fio
        ):
            return True

    return False



def eligible_placements(
    connection,
    employee,
    template: dict | None = None,
    *,
    apply_department_rules: bool | None = None,
    template_id: str | None = None,
) -> list[Placement]:
    """Возвращает назначения, на которые распространяется тест.

    Состояние из 1С здесь намеренно не учитывается: работник остается участником
    и продолжает учитываться в счетчиках даже при временной приостановке писем.
    """

    if template_id is None and template is not None:
        template_id = str(template.get("id", ""))

    if apply_department_rules is None:
        apply_department_rules = bool(template) and not _is_explicit_template(template)

    exclusions = _exclusion_rows(connection, template_id)
    result: list[Placement] = []

    for placement in employee_placements(connection, employee):
        if (
            apply_department_rules
            and template is not None
            and not department_matches(
                placement.department,
                template.get("departments", {}),
            )
        ):
            continue

        if placement_is_excluded(employee, placement, exclusions):
            continue

        result.append(placement)

    return result



def _state_policy(connection, template_id: str) -> tuple[bool, set[str]]:
    policy = connection.execute(
        "SELECT configured FROM test_state_policies WHERE test_id = ?",
        (str(template_id),),
    ).fetchone()
    if not policy or not bool(policy["configured"]):
        return False, set()

    rows = connection.execute(
        "SELECT state_normalized FROM test_allowed_states WHERE test_id = ?",
        (str(template_id),),
    ).fetchall()
    return True, {str(row["state_normalized"] or "") for row in rows}



def notification_availability(
    connection,
    employee,
    template: dict,
) -> NotificationAvailability:
    """Определяет, можно ли сейчас отправлять сообщения по тесту.

    Пока список состояний теста ни разу не сохранен, разрешены все состояния –
    это сохраняет прежнее поведение после обновления. После первой настройки
    разрешены только явно отмеченные значения. Новые состояния из 1С поэтому
    автоматически считаются неразрешенными до решения администратора.
    """

    eligible = tuple(eligible_placements(connection, employee, template))
    if not eligible:
        return NotificationAvailability(eligible=(), active=(), paused=False, reasons=())

    configured, allowed = _state_policy(connection, str(template.get("id", "")))
    if not configured:
        return NotificationAvailability(
            eligible=eligible,
            active=eligible,
            paused=False,
            reasons=(),
        )

    active = tuple(
        placement
        for placement in eligible
        if normalize_state(placement.state) in allowed
    )
    if active:
        return NotificationAvailability(
            eligible=eligible,
            active=active,
            paused=False,
            reasons=(),
        )

    reasons: list[str] = []
    seen: set[str] = set()
    for placement in eligible:
        display = state_display(placement.state)
        key = normalize_state(placement.state)
        if key in seen:
            continue
        seen.add(key)
        reasons.append(display)

    return NotificationAvailability(
        eligible=eligible,
        active=(),
        paused=True,
        reasons=tuple(reasons),
    )



def is_employee_excluded(connection, employee, template_id: str) -> bool:
    """Совместимый интерфейс: работник исключен, если исключены все назначения."""

    return not eligible_placements(
        connection,
        employee,
        template_id=str(template_id),
        apply_department_rules=False,
    )



def is_employee_globally_excluded(connection, employee) -> bool:
    return not eligible_placements(
        connection,
        employee,
        template_id=None,
        apply_department_rules=False,
    )



def placement_columns(
    placements: list[Placement] | tuple[Placement, ...],
    *,
    separator: str = "\n",
) -> tuple[str, str]:
    """Формирует два синхронных списка без удаления повторяющихся должностей."""

    return (
        separator.join(item.department for item in placements),
        separator.join(item.position for item in placements),
    )



def placement_pairs_text(
    placements: list[Placement] | tuple[Placement, ...],
    *,
    separator: str = "; ",
) -> str:
    values: list[str] = []
    for item in placements:
        if item.department and item.position:
            values.append(f"{item.department} – {item.position}")
        else:
            values.append(item.department or item.position)
    return separator.join(value for value in values if value)
