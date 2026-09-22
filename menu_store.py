"""
Хранение меню с привязкой к дате, на которую оно действует.

Столовая часто выкладывает меню накануне вечером, поэтому меню на завтра
нужно принять и придержать до утра, а не отвергать как «не сегодняшнее».
Для каждой даты храним JSON с блюдами и оригинал фото.
"""

import json
from datetime import date
from pathlib import Path

import config

DATA_DIR = Path("data/menus")
DATA_DIR.parent.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _menu_file(day: date) -> Path:
    return DATA_DIR / f"{day.isoformat()}.json"


def _photo_file(day: date) -> Path:
    return DATA_DIR / f"{day.isoformat()}.jpg"


def save(menu: dict, day: date, image_bytes: bytes | None = None, method: str | None = None) -> None:
    # Флаг «разослано» сохраняем при пересохранении того же дня — чтобы повторное
    # распознавание (например, ручная проверка) не сбрасывало уже прошедшую рассылку.
    existing = _payload(day)
    broadcast = bool(existing.get("broadcast")) if existing else False
    payload = {"menu": menu, "broadcast": broadcast, "method": method}
    _menu_file(day).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if image_bytes:
        _photo_file(day).write_bytes(image_bytes)


def _payload(day: date):
    path = _menu_file(day)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_menu(day: date | None = None):
    """Меню на указанный день (по умолчанию — на сегодня), либо None."""
    payload = _payload(day or config.today())
    return payload["menu"] if payload else None


def photo_path(day: date | None = None):
    path = _photo_file(day or config.today())
    return path if path.exists() else None


def has_menu(day: date) -> bool:
    return _menu_file(day).exists()


def was_broadcast(day: date) -> bool:
    payload = _payload(day)
    return bool(payload and payload.get("broadcast"))


def recognition_method(day: date | None = None):
    """'vlm' | 'ocr' | None — каким способом распозналось сохранённое меню."""
    payload = _payload(day or config.today())
    return payload.get("method") if payload else None


def mark_broadcast(day: date) -> None:
    payload = _payload(day)
    if not payload:
        return
    payload["broadcast"] = True
    _menu_file(day).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def cleanup(keep_days: int = 7) -> None:
    """Убираем меню старше keep_days, чтобы каталог не рос бесконечно."""
    today = config.today()
    for path in list(DATA_DIR.glob("*.json")) + list(DATA_DIR.glob("*.jpg")):
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if (today - day).days > keep_days:
            path.unlink(missing_ok=True)
