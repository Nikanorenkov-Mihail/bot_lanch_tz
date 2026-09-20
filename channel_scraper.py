"""
Чтение последнего фото из ПУБЛИЧНОГО Telegram-канала без вступления бота
в канал — через открытую веб-версию канала (t.me/s/<канал>), которую
Telegram отдаёт всем без авторизации. Официально не документировано:
если Telegram изменит вёрстку этой страницы, парсинг может сломаться —
тогда используйте пересылку фото боту в личку как запасной вариант
(см. bot.py, обработчик on_admin_menu_photo).
"""

import re

import aiohttp

PHOTO_URL_RE = re.compile(
    r'tgme_widget_message_photo_wrap["\'][^>]*style="background-image:url\(\'([^\']+)\'\)'
)


async def fetch_latest_photo(channel_username: str):
    """Возвращает (photo_url, image_bytes) для самого свежего фото-поста канала, либо None."""
    username = channel_username.lstrip("@")
    page_url = f"https://t.me/s/{username}"

    async with aiohttp.ClientSession() as session:
        async with session.get(page_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            html = await resp.text()

        urls = PHOTO_URL_RE.findall(html)
        if not urls:
            return None
        photo_url = urls[-1]  # последнее фото на странице — самое свежее сообщение

        async with session.get(photo_url, timeout=aiohttp.ClientTimeout(total=15)) as img_resp:
            img_resp.raise_for_status()
            image_bytes = await img_resp.read()

    return photo_url, image_bytes
