"""Включение/выключение рассылки меню сотрудникам — управляется /notify_on и /notify_off."""

from pathlib import Path

FLAG_FILE = Path("data/notifications_enabled.txt")
FLAG_FILE.parent.mkdir(parents=True, exist_ok=True)


def notifications_enabled() -> bool:
    if not FLAG_FILE.exists():
        return True  # по умолчанию включено
    return FLAG_FILE.read_text().strip() != "0"


def set_notifications_enabled(value: bool) -> None:
    FLAG_FILE.write_text("1" if value else "0")
