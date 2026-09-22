"""
Распознавание меню столовой с фото через VLM (мультимодальную модель) по
OpenAI-совместимому API (endpoint /chat/completions с image_url).

В отличие от локального Tesseract (menu_parser.py) модель видит фото целиком
и понимает нестандартную вёрстку, поэтому читает и салаты/супы/горячее, и
гарниры/напитки. Возвращает ту же структуру, что и menu_parser.parse_menu_image:

    {"salad": [{"name", "price"}], "soup": [...], "hot": [...],
     "garnish": [...], "drink": [...], "date": datetime.date | None}

Ключ настраивается через config.VLM_* . Модуль ничего не решает про фолбэк —
этим занимается menu_recognition.py: при исключении отсюда он откатывается на
Tesseract.
"""

import base64
import json
import logging
import re
from datetime import date

import aiohttp

import config

CATEGORIES = ("salad", "soup", "hot", "garnish", "drink")

# System-роль. Главный приоритет — дословные названия блюд: модель часто
# «нормализует»/сокращает названия, из-за чего они расходятся с меню на фото.
SYSTEM_PROMPT = (
    "Ты — точный парсер фото меню столовой. Возвращаешь строго один JSON-объект, "
    "без пояснений и без markdown.\n"
    "ГЛАВНЫЙ ПРИОРИТЕТ — абсолютно точные названия блюд. Переноси название дословно, "
    "слово в слово, на русском языке ровно как на фото: сохраняй все слова, их порядок и "
    "форму, точно различай похожие буквы (е/ё, и/й, ш/щ). "
    "НЕ сокращай, НЕ переводи, НЕ перефразируй, НЕ заменяй блюдо похожим и НЕ дополняй "
    "название от себя. Если часть названия не читается — дай наиболее вероятное прочтение "
    "по фото, но ничего не выдумывай."
)

# Просим модель вернуть СТРОГО JSON заданной формы. Держим инструкцию на русском —
# меню русскоязычное, так модель точнее переносит названия блюд как есть.
PROMPT = """Распознай это фото меню столовой. Верни ТОЛЬКО JSON, без пояснений и без ```.

Формат:
{
  "date": "YYYY-MM-DD или null",
  "salad": [{"name": "название", "price": число_или_null}],
  "soup": [...],
  "hot": [...],
  "garnish": [...],
  "drink": [...]
}

Правила:
- salad — салаты, soup — супы, hot — горячие блюда, garnish — гарниры, drink — напитки.
- name — ПОЛНОЕ название блюда дословно, как на фото: все слова в том же порядке и форме
  (например «Салат из свежих овощей», а не «Салат овощной»; «Бефстроганов из говядины»,
  а не «Бефстроганов»). Убери только порядковый номер в начале строки и описание состава
  в скобках. Больше НИЧЕГО в названии не меняй, не сокращай и не додумывай.
- price — цена в рублях целым числом; если цены нет, ставь null.
- НЕ добавляй строки про «комплексный обед»/«комплекс» как блюда — это не блюда.
- Ничего не выдумывай: если категории на фото нет, верни для неё пустой список [].
- date — дата, НА КОТОРУЮ это меню (из шапки фото); если даты нет, верни null."""


def _extract_json(content: str) -> dict:
    """Достаёт JSON-объект из ответа модели (на случай обёртки в ```json или текст)."""
    content = content.strip()
    # срезаем возможные ограждения ```json ... ```
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.DOTALL)
    if fence:
        content = fence.group(1)
    else:
        # иначе берём от первой { до последней }
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1 and end > start:
            content = content[start : end + 1]
    return json.loads(content)


def _to_price(value):
    """Приводим цену к int или None (модель может вернуть '120', 120.0, '' и т.п.)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) or None
    m = re.search(r"\d+", str(value))
    return int(m.group()) if m else None


def _to_date(value):
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _normalize_items(raw) -> list[dict]:
    """Приводим список блюд категории к [{'name': str, 'price': int|None}]."""
    items = []
    if not isinstance(raw, list):
        return items
    for entry in raw:
        if isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            price = _to_price(entry.get("price"))
        else:
            name = str(entry).strip()
            price = None
        if name:
            items.append({"name": name[:60], "price": price})
    return items


def _normalize(data: dict) -> dict:
    menu = {cat: _normalize_items(data.get(cat)) for cat in CATEGORIES}
    # Гарниры/напитки почти не меняются — если модель их не увидела, подставляем
    # проверенный ручной список из config, чтобы сотрудникам было из чего выбрать.
    if not menu["garnish"]:
        menu["garnish"] = config.DEFAULT_GARNISH
    if not menu["drink"]:
        menu["drink"] = config.DEFAULT_DRINKS
    menu["date"] = _to_date(data.get("date"))
    return menu


async def parse(image_bytes: bytes) -> dict:
    """Распознаёт меню с фото через VLM. Бросает исключение при сетевой/иной ошибке
    — вызывающий (menu_recognition) откатится на Tesseract."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": config.VLM_MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                ],
            },
        ],
    }
    headers = {"Content-Type": "application/json"}
    if config.VLM_API_KEY:
        headers["Authorization"] = f"Bearer {config.VLM_API_KEY}"

    url = f"{config.VLM_BASE_URL}/chat/completions"
    timeout = aiohttp.ClientTimeout(total=config.VLM_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()

    content = data["choices"][0]["message"]["content"]
    if isinstance(content, list):  # некоторые API отдают content частями
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    parsed = _extract_json(content)
    menu = _normalize(parsed)
    logging.info(
        "VLM распознал: салатов=%d, супов=%d, горячего=%d, гарниров=%d, напитков=%d, дата=%s",
        len(menu["salad"]), len(menu["soup"]), len(menu["hot"]),
        len(menu["garnish"]), len(menu["drink"]), menu["date"],
    )
    return menu
