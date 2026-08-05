from __future__ import annotations

import re


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def is_employee_excluded(connection, employee, template_id: str) -> bool:
    fio = normalize_text(employee["fio"])
    position = normalize_text(employee["position"])
    return connection.execute(
        """SELECT 1 FROM exclusions
           WHERE enabled=1 AND template_id IN ('*', ?)
             AND ((kind='employee' AND normalized_fio=? AND normalized_position=?)
               OR (kind='position' AND normalized_position=?))
           LIMIT 1""",
        (str(template_id), fio, position, position),
    ).fetchone() is not None


def is_employee_globally_excluded(connection, employee) -> bool:
    fio = normalize_text(employee["fio"])
    position = normalize_text(employee["position"])
    return connection.execute(
        """SELECT 1 FROM exclusions
           WHERE enabled=1 AND template_id='*'
             AND ((kind='employee' AND normalized_fio=? AND normalized_position=?)
               OR (kind='position' AND normalized_position=?))
           LIMIT 1""",
        (fio, position, position),
    ).fetchone() is not None
