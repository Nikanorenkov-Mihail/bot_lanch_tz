"""
Единая точка распознавания меню с фото.

Если VLM настроен (config.vlm_enabled) — читаем меню моделью (vlm_parser).
При любой ошибке или если модель не нашла ни одного блюда — откатываемся на
локальный Tesseract OCR (menu_parser). Так бот продолжает работать даже без
сети или при недоступном endpoint.
"""

import asyncio
import logging

import config
import vlm_parser
from menu_parser import parse_menu_image

# Категории, наличие которых считаем признаком успешного распознавания.
# Гарниры/напитки не в счёт — они могут подставляться дефолтными из config.
_CORE = ("salad", "soup", "hot")


def _has_dishes(menu: dict) -> bool:
    return any(menu.get(cat) for cat in _CORE)


def _ensure_secret_drink(menu: dict) -> None:
    """Гарантирует, что «Секретный компот» есть в напитках при любом способе
    распознавания (модель могла вернуть свой список напитков без него)."""
    drinks = menu.setdefault("drink", [])
    secret = config.SECRET_DRINK["name"].strip().lower()
    if not any((d.get("name") or "").strip().lower() == secret for d in drinks):
        drinks.append(dict(config.SECRET_DRINK))


async def recognize_menu(image_bytes: bytes) -> tuple[dict, str]:
    """Возвращает (меню, способ), где способ — 'vlm' или 'ocr'.

    Меню — см. menu_parser.parse_menu_image / vlm_parser.parse.
    """
    if config.vlm_enabled():
        try:
            menu = await vlm_parser.parse(image_bytes)
            if _has_dishes(menu):
                _ensure_secret_drink(menu)
                return menu, "vlm"
            logging.warning("VLM не нашёл ни одного блюда — откат на Tesseract OCR")
        except Exception:
            logging.exception("Ошибка VLM-распознавания — откат на Tesseract OCR")

    # Tesseract синхронный и работает секунды — уводим в отдельный поток,
    # чтобы не блокировать event loop бота.
    menu = await asyncio.to_thread(parse_menu_image, image_bytes)
    _ensure_secret_drink(menu)
    return menu, "ocr"
