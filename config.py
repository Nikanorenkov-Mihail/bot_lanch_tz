import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

# ID канала со столовой, откуда прилетает фото меню (число вида -100xxxxxxxxxx).
# Нужен, только если бот СОСТОИТ в канале как участник/админ — сейчас не ваш случай.
CHANNEL_ID = os.getenv("CHANNEL_ID", "")

# Username чужого ПУБЛИЧНОГО канала столовой (без @) — бот сам проверяет его
# открытую веб-версию (t.me/s/<канал>) по будням в CHECK_HOUR:CHECK_MINUTE,
# без вступления в канал и без пересылок.
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
CHECK_HOUR = int(os.getenv("CHECK_HOUR", "10"))
CHECK_MINUTE = int(os.getenv("CHECK_MINUTE", "0"))

# Telegram ID администраторов через запятую (узнать свой id можно у @userinfobot)
ADMIN_IDS = {x.strip() for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

# Гарниры и напитки в этой столовой почти не меняются день в день и на фото
# оформлены в одну строку через запятую — OCR там путается. Проще держать
# их здесь и поправить вручную, если состав вдруг изменится.
DEFAULT_GARNISH = [
    {"name": "Рис", "price": 80},
    {"name": "Макароны", "price": 80},
    {"name": "Гречка", "price": 80},
    {"name": "Картофельное пюре", "price": 120},
]
DEFAULT_DRINKS = [
    {"name": "Чай в ассортименте", "price": 50},
    {"name": "Компот с сахаром", "price": 50},
    {"name": "Напиток без сахара (каркаде)", "price": 50},
]
