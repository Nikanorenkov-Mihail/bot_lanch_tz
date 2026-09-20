"""
Чтение последнего фото из ПУБЛИЧНОГО Telegram-канала без вступления бота
в канал — через открытую веб-версию канала (t.me/s/<канал>), которую
Telegram отдаёт всем без авторизации. Официально не документировано:
если Telegram изменит вёрстку этой страницы, парсинг может сломаться —
тогда используйте пересылку фото боту в личку как запасной вариант
(см. bot.py, обработчик on_admin_menu_photo).
"""

import html as html_mod
import logging
import re

import aiohttp

# Ищем любые background-image:url(...) на картинки телеграмовской CDN.
# Намеренно НЕ опираемся на имена CSS-классов: у элемента их бывает несколько
# и порядок меняется, из-за чего привязка к классу легко ломается.
PHOTO_URL_RE = re.compile(
    r"background-image\s*:\s*url\(['\"]?(https://[^'\")]+?\.(?:jpg|jpeg|png|webp)[^'\")]*)['\"]?\)",
    re.IGNORECASE,
)

# Аватарка канала тоже лежит в background-image, но по другому пути — отсеиваем.
SKIP_URL_PARTS = ("/file/emoji", "avatar", "/progressive/")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "ru,en;q=0.9",
}


def extract_photo_urls(html: str) -> list[str]:
    """Все ссылки на фото со страницы канала, в порядке появления (последняя — самая свежая)."""
    html = html_mod.unescape(html)  # кавычки в style могут приходить как &quot;
    urls = []
    for url in PHOTO_URL_RE.findall(html):
        if any(part in url.lower() for part in SKIP_URL_PARTS):
            continue
        urls.append(url)
    return urls


async def fetch_latest_photo(channel_username: str):
    """Возвращает (photo_url, image_bytes) для самого свежего фото-поста канала, либо None."""
    username = (channel_username or "").lstrip("@")
    if not username:
        return None
    page_url = f"https://t.me/s/{username}"

    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout) as session:
        async with session.get(page_url) as resp:
            resp.raise_for_status()
            html = await resp.text()

        urls = extract_photo_urls(html)
        logging.info(f"Страница канала загружена ({len(html)} символов), найдено фото: {len(urls)}")
        if not urls:
            return None
        photo_url = urls[-1]  # последнее фото на странице — самое свежее сообщение

        async with session.get(photo_url) as img_resp:
            img_resp.raise_for_status()
            image_bytes = await img_resp.read()

    return photo_url, image_bytes
