"""Хранение меню на сегодня в локальном JSON-файле (переживает перезапуск бота)."""

import json
from pathlib import Path

MENU_FILE = Path("data/today_menu.json")
MENU_FILE.parent.mkdir(parents=True, exist_ok=True)


def save_menu(menu: dict) -> None:
    with open(MENU_FILE, "w", encoding="utf-8") as f:
        json.dump(menu, f, ensure_ascii=False, indent=2)


def load_menu():
    if not MENU_FILE.exists():
        return None
    with open(MENU_FILE, "r", encoding="utf-8") as f:
        return json.load(f)
