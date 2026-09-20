"""Диагностика: печатает сырой OCR-текст последнего фото из канала столовой.
Запуск в контейнере:  docker compose exec lunch-bot python3 debug_ocr.py
"""

import asyncio
import io

import pytesseract
from PIL import Image, ImageOps

import channel_scraper
import config


async def main():
    result = await channel_scraper.fetch_latest_photo(config.SOURCE_CHANNEL)
    if not result:
        print("Фото в канале не найдено")
        return
    _, image_bytes = result
    img = Image.open(io.BytesIO(image_bytes))
    gray = ImageOps.grayscale(img)
    w, h = gray.size
    big = gray.resize((w * 2, h * 2), Image.LANCZOS).point(lambda p: 255 if p > 150 else 0)
    text = pytesseract.image_to_string(big, lang="rus", config="--psm 6")
    print("=== RAW OCR START ===")
    print(text)
    print("=== RAW OCR END ===")


if __name__ == "__main__":
    asyncio.run(main())
