"""Простое хранилище сотрудников: telegram_id -> имя, в JSON-файле."""

import json
from pathlib import Path

EMPLOYEES_FILE = Path("data/employees.json")
EMPLOYEES_FILE.parent.mkdir(parents=True, exist_ok=True)


def _load() -> dict:
    if not EMPLOYEES_FILE.exists():
        return {}
    with open(EMPLOYEES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    with open(EMPLOYEES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def register(tg_id: int, name: str) -> None:
    data = _load()
    data[str(tg_id)] = name
    _save(data)


def get_name(tg_id: int):
    return _load().get(str(tg_id))


def all_employees() -> dict:
    """Возвращает {telegram_id (строка): имя}."""
    return _load()
