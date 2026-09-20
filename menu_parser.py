"""
Извлечение меню столовой из фото — локально через Tesseract OCR, бесплатно,
без внешних платных API.

Салаты/супы/горячее меняются каждый день — их распознаём с фото.
Гарниры и напитки в этой столовой почти не меняются и оформлены на фото
одной длинной строкой через запятую — OCR там путается, поэтому берём их
из config.DEFAULT_GARNISH / config.DEFAULT_DRINKS.
"""

import io
import re
from datetime import date

import pytesseract
from PIL import Image, ImageFilter, ImageOps

import config

# Целевая ширина картинки перед распознаванием — компромисс точности и скорости
TARGET_WIDTH = 1600

MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
DATE_RE = re.compile(r"(\d{1,2})\s+(" + "|".join(MONTHS_RU) + r")\s+(\d{4})", re.IGNORECASE)

# Шапка меню («Чем наполнить Большую тарелку 21 сентября 2026»). OCR нередко
# разрывает её на две строки, поэтому ловим и заголовок, и дату, и «огрызок»
# вида «СЕНТЯБРЯ 2026» — салаты в этой столовой идут сразу после шапки.
TITLE_RE = re.compile(r"чем\s+наполн|тарелк", re.IGNORECASE)
MONTH_TAIL_RE = re.compile(r"^\W*(" + "|".join(MONTHS_RU) + r")\s*\d{0,4}\W*$", re.IGNORECASE)

# Заголовки разделов ищем В НАЧАЛЕ строки и без ограничения по длине:
# «Гарниры: 1. Рис — 80руб 2. Макароны...» идёт одной длинной строкой.
SECTION_HEADERS = [
    ("salad", re.compile(r"^\W*салат", re.IGNORECASE)),
    ("soup", re.compile(r"^\W*суп\b", re.IGNORECASE)),
    ("hot", re.compile(r"^\W*горяч", re.IGNORECASE)),
]
# После этих заголовков блюда не собираем — гарниры и напитки берём из config.
# OCR часто искажает эти слова («Гарниры»->«Гарциры», «Напитки»->«Наинуки»),
# поэтому допускаем частые варианты замены соседних букв.
STOP_RE = re.compile(r"^\W*(гар[нцп]и|гарнир|напит|наинук|наин)", re.IGNORECASE)
# Строка про комплексный обед может встретиться и в середине — это не блюдо,
# но раздел на ней не закрываем: после неё в том же разделе идут обычные блюда.
COMBO_RE = re.compile(r"комплексн", re.IGNORECASE)

TRAILING_PRICE_RE = re.compile(r"[\s\-–—:]*?(\d{2,4})\s*(?:руб\.?)?\s*$")
# Срезаем нумерацию и мусорные символы, которые OCR любит ставить в начале строки
LEADING_JUNK_RE = re.compile(r"^(?:руб\.?\s*)?[\s.,;:„“”\"'«»‹›•·°*!\-–—()\[\]{}\d]+", re.IGNORECASE)
# Номер списка OCR часто читает как букву с точкой: «3.» -> «з.», «10.» -> «ю.».
# Срезаем одиночную букву С ТОЧКОЙ в начале названия. Вариант «буква + пробел»
# намеренно не трогаем — он съедал бы настоящие короткие слова («С грибами»).
LEADING_NUMBERING_RE = re.compile(r"^[a-zA-Zа-яёА-ЯЁ]\.\s*")
CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")


def _extract_date(text: str):
    """Дата на фото меню — ищем по всему тексту, т.к. OCR рвёт шапку на строки."""
    m = DATE_RE.search(text)
    if not m:
        return None
    day, month_name, year = m.groups()
    month = MONTHS_RU.get(month_name.lower())
    try:
        return date(int(year), month, int(day))
    except (ValueError, TypeError):
        return None


def _clean_name(raw: str) -> str:
    raw = LEADING_JUNK_RE.sub("", raw)
    raw = LEADING_NUMBERING_RE.sub("", raw)
    raw = LEADING_JUNK_RE.sub("", raw)  # после срезанной «нумерации» мог остаться мусор
    # Обрубаем описание состава и всё после него. OCR часто читает "(" как "{" или "[".
    raw = re.split(r"[(\[{]", raw, maxsplit=1)[0]
    return raw.strip(" -–—:.,;").strip()


def _looks_like_dish(name: str) -> bool:
    """Отсеиваем огрызки распознавания вроде ';. миша Уда ©'."""
    letters = CYRILLIC_RE.findall(name)
    # Порог 2 буквы, чтобы не терять короткие настоящие названия («Уха», «Щи»).
    if len(letters) < 2:
        return False
    # в нормальном названии больше половины символов — буквы или пробелы
    good = sum(1 for ch in name if ch.isalpha() or ch.isspace())
    return good / max(len(name), 1) >= 0.7


def _is_bare_header(line: str) -> bool:
    """True для голого заголовка раздела («Салаты», «Суп», «Горячее») —
    короткая строка без цифр. Если на строке есть цена или длинное название,
    это уже блюдо (например «Суп харчо — 90»), а не просто заголовок."""
    return len(line) <= 14 and not any(ch.isdigit() for ch in line)


def _parse_ocr_text(text: str, start_with_salad: bool = False) -> dict:
    """start_with_salad=True — запасной проход: считаем салатами всё до первого раздела."""
    menu = {"salad": [], "soup": [], "hot": []}
    current = "salad" if start_with_salad else None
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
        if _looks_like_dish(name):
            menu[current].append({"name": name[:60], "price": price})
        buffer = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # «Комплексный обед из 3-х блюд …» — не блюдо, но раздел НЕ закрываем:
        # после него в том же разделе могут идти обычные блюда.
        if COMBO_RE.search(line):
            flush()
            continue

        # Гарниры/напитки берём из config — на этих заголовках сбор прекращаем.
        if STOP_RE.match(line):
            flush()
            current = None
            continue

        matched = next((key for key, pat in SECTION_HEADERS if pat.match(line)), None)
        if matched:
            flush()
            current = matched
            # Заголовок может быть совмещён с блюдом или сам быть блюдом, если
            # бумажный заголовок раздела пропущен: «Суп харчо — 90». Тогда строку
            # тоже записываем, а не теряем.
            if not _is_bare_header(line):
                buffer.append(line)
                if TRAILING_PRICE_RE.search(line):
                    flush()
            continue

        if TITLE_RE.search(line) or DATE_RE.search(line) or MONTH_TAIL_RE.match(line):
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


def _preprocess(image: Image.Image) -> Image.Image:
    """
    Готовим картинку к OCR. Жёсткая бинаризация съедает тонкие буквы и добавляет
    мусора, поэтому ограничиваемся автоконтрастом и лёгкой резкостью.
    """
    gray = ImageOps.grayscale(image)
    w, h = gray.size
    # Апскейл помогает на мелком тексте, но на больших фото только замедляет:
    # тянем к целевой ширине и никогда не увеличиваем больше чем вдвое.
    scale = min(max(TARGET_WIDTH / w, 1.0), 2.0)
    if scale > 1.01:
        gray = gray.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    return ImageOps.autocontrast(gray).filter(ImageFilter.SHARPEN)


def parse_menu_image(image_bytes: bytes) -> dict:
    image = Image.open(io.BytesIO(image_bytes))
    text = pytesseract.image_to_string(_preprocess(image), lang="rus", config="--psm 6")

    menu = _parse_ocr_text(text)
    if not menu["salad"] and (menu["soup"] or menu["hot"]):
        # Шапку не удалось прочитать — считаем салатами всё до первого раздела.
        fallback = _parse_ocr_text(text, start_with_salad=True)
        if fallback["salad"]:
            menu["salad"] = fallback["salad"]

    menu["garnish"] = config.DEFAULT_GARNISH
    menu["drink"] = config.DEFAULT_DRINKS
    menu["date"] = _extract_date(text)
    return menu
