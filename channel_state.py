"""Запоминаем URL последнего обработанного фото из канала — чтобы не разбирать его повторно."""

from pathlib import Path

STATE_FILE = Path("data/last_channel_photo.txt")
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)


def get_last_photo_url():
    if not STATE_FILE.exists():
        return None
    value = STATE_FILE.read_text().strip()
    return value or None


def set_last_photo_url(url: str) -> None:
    STATE_FILE.write_text(url)
