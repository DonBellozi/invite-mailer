from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Placement:
    """Одно кадровое назначение работника."""

    department: str
    position: str
    sort_order: int = 0


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


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
        SELECT department, position, sort_order
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
                sort_order=int(row["sort_order"] or 0),
            )
            for row in rows
        ]

    return [
        Placement(
            department=str(_employee_value(employee, "department", "") or ""),
            position=str(_employee_value(employee, "position", "") or ""),
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
    """Возвращает назначения, применимые к тесту и не попавшие в исключения."""

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
    placements: list[Placement],
    *,
    separator: str = "\n",
) -> tuple[str, str]:
    """Формирует два синхронных списка без удаления повторяющихся должностей."""

    return (
        separator.join(item.department for item in placements),
        separator.join(item.position for item in placements),
    )


def placement_pairs_text(
    placements: list[Placement],
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
