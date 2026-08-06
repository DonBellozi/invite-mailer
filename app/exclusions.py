from __future__ import annotations

# Совместимый модуль-обертка: остальной проект продолжает импортировать функции
# из app.exclusions, а фактическая проверка выполняется по кадровым назначениям.
from .placements import (
    is_employee_excluded,
    is_employee_globally_excluded,
    normalize_text,
)

__all__ = [
    "normalize_text",
    "is_employee_excluded",
    "is_employee_globally_excluded",
]
