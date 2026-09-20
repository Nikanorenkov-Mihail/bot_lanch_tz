"""
Извлечение меню столовой из фото — локально через Tesseract OCR, бесплатно,
без внешних платных API.

Салаты/супы/горячее меняются каждый день — их распознаём с фото.
Гарниры и напитки в этой столовой почти не меняются и оформлены на фото
нестандартно (несколько блюд в одной строке) — OCR там путается, поэтому
берём их из config.DEFAULT_GARNISH / config.DEFAULT_DRINKS. Если состав
гарниров/напитков в столовой поменяется — просто отредактируйте эти
списки в config.py.
"""

import io
import re
from datetime import date

import pytesseract
from PIL import Image, ImageOps

import config

CATEGORY_PATTERNS = [
    ("salad", re.compile(r"салат", re.IGNORECASE)),
    ("soup", re.compile(r"^\s*суп\b", re.IGNORECASE)),
    ("hot", re.compile(r"горячее", re.IGNORECASE)),
]
STOP_PATTERNS = re.compile(r"гарнир|напит", re.IGNORECASE)

TRAILING_PRICE_RE = re.compile(r"[\s\-–—:]*?(\d{2,4})\s*(?:руб\.?)?\s*$")
LEADING_JUNK_RE = re.compile(
    r"^(?:руб\.?\s*)?[\s.,„\"'\u2022\-\d)]*(?:[a-zA-Zа-яёА-ЯЁ]\.?\s+)?", re.IGNORECASE
)

MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
DATE_RE = re.compile(r"(\d{1,2})\s+(" + "|".join(MONTHS_RU) + r")\s+(\d{4})", re.IGNORECASE)


def _extract_date(text: str):
    """Дата на фото меню (например 'Чем наполнить Большую тарелку 18 сентября 2026') — если найдена."""
    m = DATE_RE.search(text)
    if not m:
        return None
    day, month_name, year = m.groups()
    month = MONTHS_RU.get(month_name.lower())
    try:
        return date(int(year), month, int(day))
    except ValueError:
        return None


def _clean_name(raw: str) -> str:
    raw = LEADING_JUNK_RE.sub("", raw)
    # Обрубаем описание состава и всё после него. OCR часто читает "(" как "{" или "[".
    raw = re.split(r"[(\[{]", raw, maxsplit=1)[0]
    return raw.strip(" -–—:.,").strip()


def _parse_ocr_text(text: str) -> dict:
    menu = {"salad": [], "soup": [], "hot": []}
    current = None
    buffer = []

    def flush():
        nonlocal buffer
        if not buffer or current is None:
            buffer = []
            return
        joined = " ".join(buffer)
        m = TRAILING_PRICE_RE.search(joined)
        price = int(m.group(1)) if m else None
        name = _clean_name(joined[: m.start()] if m else joined)
        if len(name) >= 2:
            menu[current].append({"name": name[:60], "price": price})
        buffer = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if STOP_PATTERNS.search(line) and len(line) < 20:
            flush()
            current = None
            continue

        matched = next((key for key, pat in CATEGORY_PATTERNS if pat.search(line)), None)
        if matched and len(line) < 20:
            flush()
            current = matched
            continue

        # Строка-заголовок с датой ("Чем наполнить Большую тарелку 21 сентября 2026").
        # В этой столовой салаты идут сразу после неё, причём заголовок «Салаты»
        # бывает не на каждом фото — поэтому дату используем как якорь начала салатов.
        if DATE_RE.search(line):
            flush()
            current = "salad"
            continue

        if current is None:
            continue

        buffer.append(line)
        if re.search(r"\d", line) and TRAILING_PRICE_RE.search(" ".join(buffer)):
            flush()

    flush()
    return menu


def parse_menu_image(image_bytes: bytes) -> dict:
    image = Image.open(io.BytesIO(image_bytes))
    gray = ImageOps.grayscale(image)
    w, h = gray.size
    big = gray.resize((w * 2, h * 2), Image.LANCZOS)  # апскейл + бинаризация ощутимо помогают Tesseract
    big = big.point(lambda p: 255 if p > 150 else 0)

    text = pytesseract.image_to_string(big, lang="rus", config="--psm 6")
    menu = _parse_ocr_text(text)
    menu["garnish"] = config.DEFAULT_GARNISH
    menu["drink"] = config.DEFAULT_DRINKS
    menu["date"] = _extract_date(text)
    return menu
