"""
Бот для сбора обедов.

Поток:
1. Меню приходит одним из трёх способов (можно использовать любой, они
   не конфликтуют): (а) бот сам опрашивает открытую веб-версию чужого
   публичного канала столовой (channel_scraper.py) — без вступления в
   канал и без пересылок; (б) бот состоит в СВОЁМ канале и ловит
   channel_post напрямую; (в) админ пересылает фото боту в личку — на
   случай, если варианты (а)/(б) недоступны.
2. Фото распознаётся через локальный OCR (menu_parser.py, бесплатно) и
   рассылается каждому зарегистрированному сотруднику кнопками выбора
   по категориям. Любую категорию можно пропустить (например, заказать
   только салат).
3. Выбор сохраняется локально (storage.py, SQLite). В конце сотрудник
   видит сводку своего заказа и подтверждает её или начинает заново —
   это защита от случайных нажатий не туда.
4. Админ в любой момент может вызвать /summary — сводный заказ для звонка
   в столовую, и /remind — разослать напоминание тем, кто не ответил.
"""

import asyncio
import logging
import re
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

import channel_scraper
import channel_state
import config
import employees
import menu_recognition
import menu_store
import pricing
import settings
import storage


logging.basicConfig(level=logging.INFO)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

CATEGORIES = [
    ("salad", "🥗 Салат"),
    ("soup", "🍲 Суп"),
    ("hot", "🍗 Горячее"),
    ("garnish", "🍚 Гарнир"),
    ("drink", "🥤 Напиток"),
]
CATEGORY_LABELS = dict(CATEGORIES)
CATEGORY_ORDER = [c for c, _ in CATEGORIES]

# Подпись способа распознавания меню — показываем админу, чтобы было видно,
# сработал VLM или локальный OCR, не заходя в логи.
METHOD_LABELS = {"vlm": "🤖 DeepSeek", "ocr": "🔤 OCR"}

# Порядок вывода заказа администратору (как удобно забирать в столовой).
# Горячее и гарнир забирают одной тарелкой, поэтому в сводке и в списке на
# получение они идут одной позицией «Горячее с гарниром». Салат и суп —
# по-прежнему отдельными строками.
HOT_GARNISH = "hot_garnish"
ADMIN_CATEGORY_ORDER = ["salad", "soup", HOT_GARNISH, "drink"]
ADMIN_CATEGORY_LABELS = {**CATEGORY_LABELS, HOT_GARNISH: "🍗 Горячее с гарниром"}


def combined_order(order: dict) -> dict:
    """Схлопывает горячее и гарнир в одну позицию — их забирают одной тарелкой.

    order: {категория: (блюдо, цена)}. Возвращает {категория: блюдо}, где
    горячее и гарнир объединены под ключом HOT_GARNISH как «Котлета + Рис».
    """
    result = {}
    for cat in ("salad", "soup", "drink"):
        dish = (order.get(cat) or (None, None))[0]
        if dish:
            result[cat] = dish
    hot = (order.get("hot") or (None, None))[0]
    garnish = (order.get("garnish") or (None, None))[0]
    parts = [p for p in (hot, garnish) if p]
    if parts:
        result[HOT_GARNISH] = " + ".join(parts)
    return result

# Кнопка на телефоне обрезается, поэтому держим подпись короткой и фиксируем
# место под цену — иначе у длинных названий цена уезжает за край экрана.
MAX_BUTTON_LABEL = 30
PRICE_FIELD_WIDTH = 8  # " · 1000₽"


def format_button_label(name: str, price) -> str:
    """Короткая подпись кнопки: название урезается, цена видна всегда."""
    if price:
        suffix = f" · {price}₽"
        budget = MAX_BUTTON_LABEL - PRICE_FIELD_WIDTH
    else:
        suffix = ""
        budget = MAX_BUTTON_LABEL
    name = name.strip()
    if len(name) > budget:
        name = name[: budget - 1].rstrip(" ,.-") + "…"
    return f"{name}{suffix}"

# Постоянные кнопки внизу экрана — основные действия сотрудника
BTN_COLLECT = "🍽 Собрать обед"
BTN_EDIT = "✏️ Изменить текущий обед"
BTN_DECLINE = "❌ Отказаться от обеда"
BTN_ADMIN = "⚙️ Режим администратора"

# Кнопки режима администратора
BTN_SUMMARY = "📋 Сводка заказа"
BTN_PERSONAL = "🧾 Персональные заказы"
BTN_REMIND = "🔔 Напомнить не ответившим"
BTN_NOTIFY_TOGGLE = "📢 Рассылка: вкл/выкл"
BTN_PICKUP = "📦 Забрать заказ"
BTN_CHECK_MENU = "🔄 Проверить меню сейчас"
BTN_RESET = "🗑 Обнулить заказы"
BTN_BACK = "👤 Выйти из режима администратора"


# tg_id -> категория, для которой ждём введённое вручную название блюда
# («Другой вариант»). Состояние транзиентное — переживать перезапуск не должно.
pending_custom: dict[int, str] = {}


def is_admin(tg_id) -> bool:
    return str(tg_id) in config.ADMIN_IDS


def main_keyboard(tg_id) -> ReplyKeyboardMarkup:
    """Клавиатура сотрудника. Админу дополнительно показываем вход в режим администратора."""
    rows = [
        [KeyboardButton(text=BTN_COLLECT)],
        [KeyboardButton(text=BTN_EDIT)],
        [KeyboardButton(text=BTN_DECLINE)],
    ]
    if is_admin(tg_id):
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def admin_keyboard() -> ReplyKeyboardMarkup:
    # По 2 кнопки в ряд. Функциональных кнопок нечётное число, поэтому «Забрать
    # заказ» стоит одной широкой сверху, а «Выйти» — одной широкой снизу.
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_PICKUP)],
            [KeyboardButton(text=BTN_SUMMARY), KeyboardButton(text=BTN_PERSONAL)],
            [KeyboardButton(text=BTN_REMIND), KeyboardButton(text=BTN_NOTIFY_TOGGLE)],
            [KeyboardButton(text=BTN_CHECK_MENU), KeyboardButton(text=BTN_RESET)],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
    )


# ---------- Регистрация сотрудника ----------

@dp.message(Command("start"))
async def cmd_start(message: Message):
    name = employees.get_name(message.from_user.id)
    if name:
        await message.answer(
            f"Привет, {name}! Выбери действие на кнопках ниже.",
            reply_markup=main_keyboard(message.from_user.id),
        )
    else:
        employees.register(message.from_user.id, message.from_user.full_name)
        await message.answer(
            f"Привет! Записал тебя как «{message.from_user.full_name}».\n"
            f"Если хочешь другое имя — напиши /rename Имя Фамилия.\n"
            f"Как только появится сегодняшнее меню, пришлю его сюда. "
            f"Собрать или изменить заказ можно кнопками ниже.",
            reply_markup=main_keyboard(message.from_user.id),
        )


@dp.message(Command("rename"))
async def cmd_rename(message: Message):
    # срезаем саму команду, в том числе форму "/rename@имя_бота"
    new_name = re.sub(r"^/rename(@\S+)?", "", message.text or "", count=1).strip()
    if not new_name:
        await message.answer("Использование: /rename Имя Фамилия")
        return
    employees.register(message.from_user.id, new_name)
    await message.answer(f"Готово, теперь ты «{new_name}».")


# ---------- Основные кнопки сотрудника ----------

async def start_collecting(tg_id: int, name: str, answer):
    """Запускает сборку обеда с первой категории. answer — функция ответа пользователю."""
    menu = menu_store.load_menu()
    if not menu:
        await answer("Меню на сегодня ещё не пришло. Как только появится — сразу пришлю сюда.")
        return
    if not any(menu.get(c) for c in CATEGORY_ORDER):
        await answer(
            "В сегодняшнем меню нет ни одного блюда — похоже, его не удалось прочитать. "
            "Сообщите ответственному, он обновит меню."
        )
        return
    pending_custom.pop(tg_id, None)
    storage.clear_declined(tg_id)
    storage.clear_today_order(tg_id)
    await send_menu_photo(tg_id)
    await send_category(tg_id, CATEGORY_ORDER[0])


async def send_menu_photo(tg_id: int) -> None:
    """Показывает оригинал фото меню — чтобы сверяться с ним по ходу заказа."""
    path = menu_store.photo_path()
    if not path:
        return
    try:
        await bot.send_photo(tg_id, FSInputFile(path), caption="Сегодняшнее меню 👆")
    except Exception:
        logging.exception(f"Не удалось отправить фото меню сотруднику {tg_id}")


@dp.message(F.text == BTN_COLLECT)
async def on_btn_collect(message: Message):
    tg_id = message.from_user.id
    name = employees.get_name(tg_id) or message.from_user.full_name
    employees.register(tg_id, name)
    await start_collecting(tg_id, name, message.answer)


@dp.message(F.text == BTN_EDIT)
async def on_btn_edit(message: Message):
    tg_id = message.from_user.id
    name = employees.get_name(tg_id) or message.from_user.full_name
    employees.register(tg_id, name)
    if not storage.get_employee_today_order(tg_id):
        await message.answer("Сегодняшнего заказа пока нет — собираю с нуля.")
    await start_collecting(tg_id, name, message.answer)


@dp.message(F.text == BTN_DECLINE)
async def on_btn_decline(message: Message):
    tg_id = message.from_user.id
    name = employees.get_name(tg_id) or message.from_user.full_name
    employees.register(tg_id, name)
    pending_custom.pop(tg_id, None)
    storage.set_declined(tg_id, name)
    await message.answer(
        "Записал: сегодня без обеда. Если передумаешь — нажми «🍽 Собрать обед».",
        reply_markup=main_keyboard(tg_id),
    )


# ---------- Приём меню из канала столовой ----------

async def _handle_new_menu(image_bytes: bytes, broadcast_photo, report=None, broadcast: bool = True) -> bool:
    """
    Распознаёт фото, определяет, на какой день это меню, и сохраняет его.
    Меню на сегодня рассылается сразу, меню на завтра придерживается до утра.
    report — функция ответа админу, приславшему фото.
    broadcast=False — только распознать и сохранить, НЕ рассылать сотрудникам
    (ручная проверка меню админом). Возвращает True, если меню принято.
    """

    async def tell(text):
        if report:
            await report(text)
        else:
            for admin_id in config.ADMIN_IDS:
                await bot.send_message(int(admin_id), text)

    try:
        # VLM (если настроен) с откатом на локальный Tesseract — см. menu_recognition.
        menu, method = await menu_recognition.recognize_menu(image_bytes)
    except Exception as e:
        logging.exception("Не удалось распознать меню с фото")
        await tell(f"⚠️ Не смог распознать меню: {e}")
        return False

    method_label = METHOD_LABELS.get(method, method or "?")
    menu_date = menu.pop("date", None)
    counts = {c: len(menu.get(c) or []) for c in ("salad", "soup", "hot")}
    logging.info(f"Распознано: дата={menu_date}, салатов={counts['salad']}, "
                 f"супов={counts['soup']}, горячего={counts['hot']}")

    if not any(counts.values()):
        await tell(
            "⚠️ С этого фото не удалось прочитать ни одного блюда. "
            "Попробуйте прислать фото покрупнее или чётче."
        )
        return False

    today = config.today()
    tomorrow = today + timedelta(days=1)
    # Дату на фото распознать удаётся не всегда — тогда считаем меню сегодняшним.
    target = menu_date or today

    if target < today:
        await tell(
            f"⚠️ Это меню на {target.strftime('%d.%m.%Y')} — оно уже прошло "
            f"(сегодня {today.strftime('%d.%m.%Y')}). Не сохраняю."
        )
        return False

    if target > tomorrow:
        await tell(
            f"⚠️ Это меню на {target.strftime('%d.%m.%Y')} — слишком далеко вперёд "
            f"(сегодня {today.strftime('%d.%m.%Y')}). Не сохраняю."
        )
        return False

    menu_store.save(menu, target, image_bytes, method=method)
    menu_store.cleanup()

    found = f"салатов — {counts['salad']}, супов — {counts['soup']}, горячего — {counts['hot']}"

    if target == tomorrow:
        await tell(
            f"✅ Меню на завтра ({target.strftime('%d.%m')}) сохранено ({method_label}): {found}.\n"
            f"Разошлю сотрудникам завтра в {config.CHECK_HOUR:02d}:{config.CHECK_MINUTE:02d}."
        )
        return True

    await tell(f"✅ Меню на сегодня распознано ({method_label}): {found}.")

    if not broadcast:
        # Ручная проверка: меню сохранено, но сотрудников не трогаем.
        # Флаг broadcast не выставляем — утренняя рассылка пройдёт как обычно.
        return True

    if menu_store.was_broadcast(target):
        # Сотрудники должны получать меню один раз в день. Если сегодня уже
        # рассылали (например, утром в 10:00), второй раз не отправляем —
        # меню просто обновляется в хранилище для /myorder и новых заказов.
        await tell("Меню сотрудникам сегодня уже рассылалось — повторно не отправляю, только обновил.")
        return True

    if not settings.notifications_enabled():
        await tell("Рассылка сотрудникам сейчас выключена — меню сохранено, но не разослано.")
        return True

    await broadcast_menu_to_employees(broadcast_photo)
    menu_store.mark_broadcast(target)
    return True


async def broadcast_today_menu() -> bool:
    """Рассылает уже сохранённое меню на сегодня (например, принятое вчера вечером)."""
    today = config.today()
    if not menu_store.has_menu(today) or menu_store.was_broadcast(today):
        return False
    if not settings.notifications_enabled():
        logging.info("Меню на сегодня есть, но рассылка выключена")
        return False
    path = menu_store.photo_path(today)
    photo = FSInputFile(path) if path else None
    logging.info("Рассылаю сохранённое меню на сегодня")
    await broadcast_menu_to_employees(photo)
    menu_store.mark_broadcast(today)
    return True


async def process_menu_photo(photo, report=None):
    """photo — aiogram PhotoSize из канала (если бот в нём состоит) или от админа в личке.

    broadcast=False: пересланное/канальное фото только распознаётся и сохраняется,
    сотрудникам НЕ рассылается. Рассылка сотрудникам бывает только плановая в
    CHECK_HOUR:CHECK_MINUTE (см. poll_source_channel). Меню, сохранённое до 10:00,
    уйдёт сотрудникам плановой рассылкой; после 10:00 — на следующий будний день.
    """
    file = await bot.get_file(photo.file_id)
    buffer = await bot.download_file(file.file_path)
    await _handle_new_menu(buffer.read(), photo.file_id, report=report, broadcast=False)


@dp.channel_post(F.photo)
async def on_channel_menu_photo(message: Message):
    logging.info(f"Получено фото из канала, chat_id={message.chat.id}")  # пригодится для CHANNEL_ID в .env
    if config.CHANNEL_ID and str(message.chat.id) != str(config.CHANNEL_ID):
        return  # фото из другого канала — не трогаем
    await process_menu_photo(message.photo[-1])


@dp.message(F.photo)
async def on_admin_menu_photo(message: Message):
    """
    Запасной вариант: админ прислал или переслал боту фото меню в личку —
    работает, даже если автоматический опрос канала (channel_scraper.py)
    почему-то не сработал.
    """
    if not is_admin(message.from_user.id):
        return
    await message.answer("Распознаю меню...")
    await process_menu_photo(message.photo[-1])


async def poll_source_channel():
    """Раз в будний день в config.CHECK_HOUR:CHECK_MINUTE проверяет чужой
    публичный канал столовой на новое фото — без вступления бота в канал."""
    if not config.SOURCE_CHANNEL:
        return
    tz = ZoneInfo(config.TIMEZONE)
    logging.info(
        f"Буду проверять @{config.SOURCE_CHANNEL} по будням в "
        f"{config.CHECK_HOUR:02d}:{config.CHECK_MINUTE:02d} ({config.TIMEZONE})"
    )
    while True:
        now = datetime.now(tz)
        next_run = _next_weekday_check(now, config.CHECK_HOUR, config.CHECK_MINUTE)
        await asyncio.sleep(max((next_run - now).total_seconds(), 1))
        # Меню могли принять ещё вчера вечером — тогда просто рассылаем его.
        if await broadcast_today_menu():
            continue
        await _check_channel_once()


def _next_weekday_check(now: datetime, hour: int, minute: int) -> datetime:
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:  # 5=суббота, 6=воскресенье
        candidate += timedelta(days=1)
    return candidate


async def _check_channel_once(force: bool = False, broadcast: bool = True) -> bool:
    """
    Забирает последнее фото из канала и обрабатывает его.
    force=True — обработать даже если это фото уже обрабатывалось (ручная проверка админом).
    broadcast=False — распознать и сохранить, но не рассылать сотрудникам.
    """
    try:
        result = await channel_scraper.fetch_latest_photo(config.SOURCE_CHANNEL)
        if not result:
            logging.info("На странице канала столовой не найдено фото")
            return False
        photo_url, image_bytes = result
        if not force and photo_url == channel_state.get_last_photo_url():
            logging.info("Фото на странице канала не изменилось с прошлой проверки")
            return False
        logging.info("Обрабатываю фото из канала столовой...")
        broadcast_photo = BufferedInputFile(image_bytes, filename="menu.jpg")
        accepted = await _handle_new_menu(image_bytes, broadcast_photo, broadcast=broadcast)
        channel_state.set_last_photo_url(photo_url)
        return accepted
    except Exception:
        logging.exception("Ошибка при проверке канала столовой")
        return False


async def broadcast_menu_to_employees(photo):
    """photo — либо file_id (строка, из Telegram), либо BufferedInputFile (из скрапера канала)."""
    # Жёсткий предохранитель: при выключенной рассылке сотрудники НЕ должны
    # получать автоматических сообщений ни по какому пути. Меню они увидят
    # только сами, когда нажмут «Собрать обед».
    if not settings.notifications_enabled():
        logging.info("Рассылка выключена — рассылку меню сотрудникам пропускаю")
        return
    for tg_id in employees.all_employees():
        try:
            # новое меню — начинаем день с чистого листа
            storage.clear_today_order(int(tg_id))
            storage.clear_declined(int(tg_id))
            # Показываем оригинал фото — если OCR где-то ошибся в названии или цене,
            # сотрудник сразу это увидит и сверит с картинкой.
            await bot.send_photo(int(tg_id), photo, caption="Сегодняшнее меню 👆")
            await send_category(int(tg_id), CATEGORY_ORDER[0])
        except Exception:
            logging.exception(f"Не удалось отправить меню сотруднику {tg_id}")


def build_keyboard(category: str, menu: dict) -> InlineKeyboardMarkup:
    items = menu.get(category) or []
    buttons = []
    for idx, item in enumerate(items):
        label = format_button_label(item["name"], item.get("price"))
        # В callback_data кладём ИНДЕКС блюда, а не название: лимит Telegram — 64 байта,
        # а кириллическое название легко занимает 90+ байт и кнопка не отправится вовсе.
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"pick|{category}|{idx}")])
    # «Другой вариант» — если парсер не увидел блюдо на фото, сотрудник (или админ)
    # вписывает название вручную, и оно попадает в заказ.
    buttons.append([InlineKeyboardButton(text="✏️ Другой вариант", callback_data=f"pick|{category}|other")])
    buttons.append([InlineKeyboardButton(text="Пропустить", callback_data=f"pick|{category}|skip")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def advance_to_next(tg_id: int, category: str):
    """Переходит к следующей категории после выбора, либо к финальной сводке."""
    next_idx = CATEGORY_ORDER.index(category) + 1
    if next_idx < len(CATEGORY_ORDER):
        await send_category(tg_id, CATEGORY_ORDER[next_idx])
    else:
        await show_final_summary(tg_id)


async def send_category(tg_id: int, category: str):
    menu = menu_store.load_menu()
    if not menu:
        return
    # Если в категории нет блюд (например, OCR не распознал салаты) — не показываем
    # пустой экран с одной кнопкой «Пропустить», а сразу переходим к следующей.
    while category is not None and not (menu.get(category) or []):
        next_idx = CATEGORY_ORDER.index(category) + 1
        category = CATEGORY_ORDER[next_idx] if next_idx < len(CATEGORY_ORDER) else None
    if category is None:
        await show_final_summary(tg_id)
        return
    label = CATEGORY_LABELS[category]
    kb = build_keyboard(category, menu)
    await bot.send_message(tg_id, f"Выбери: {label}", reply_markup=kb)


@dp.callback_query(F.data.startswith("pick|"))
async def on_pick(callback: CallbackQuery):
    _, category, choice = callback.data.split("|", 2)
    tg_id = callback.from_user.id
    name = employees.get_name(tg_id) or callback.from_user.full_name
    # Любое нажатие кнопки отменяет незавершённый ручной ввод «Другого варианта».
    pending_custom.pop(tg_id, None)

    menu = menu_store.load_menu()
    if not menu or category not in CATEGORY_LABELS:
        await callback.answer("Меню на сегодня ещё не пришло — попробуй позже.", show_alert=True)
        return

    if choice == "other":
        # Ждём от сотрудника название блюда следующим сообщением.
        pending_custom[tg_id] = category
        await callback.message.edit_text(
            f"{CATEGORY_LABELS[category]}: напиши название блюда одним сообщением."
        )
        await callback.answer()
        return

    if choice == "skip":
        storage.set_order_item(tg_id, name, category, None, None)
        shown = "—"
    else:
        items = menu.get(category) or []
        try:
            item = items[int(choice)]
        except (ValueError, IndexError):
            # Кнопка из вчерашнего/устаревшего сообщения — состав меню уже другой.
            # Просим начать заново, а не сохраняем мусор.
            await callback.answer(
                "Это меню уже неактуально. Открой /myorder или дождись нового меню.", show_alert=True
            )
            return
        storage.set_order_item(tg_id, name, category, item["name"], item.get("price"))
        shown = item["name"]

    await callback.message.edit_text(f"{CATEGORY_LABELS[category]}: {shown} ✅")
    await callback.answer()
    await advance_to_next(tg_id, category)


def format_order_lines(rows) -> list[str]:
    """rows: [(category, dish, price), ...] — то, что выбрал сотрудник."""
    order_map = {category: (dish, price) for category, dish, price in rows}
    lines = []
    for category, label in CATEGORIES:
        dish, price = order_map.get(category, (None, None))
        if dish:
            price_part = f" — {price}₽" if price else ""
            lines.append(f"{label}: {dish}{price_part}")
    if not lines:
        return ["Пусто — все категории пропущены."]

    total, reason = pricing.calculate(order_map)
    lines.append(f"\nИтого: {total}₽ ({reason})")
    return lines


async def show_final_summary(tg_id: int):
    rows = storage.get_employee_today_order(tg_id)
    lines = ["Твой заказ на сегодня:"] + format_order_lines(rows)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Всё верно", callback_data="confirm|ok"),
                InlineKeyboardButton(text="🔄 Начать заново", callback_data="confirm|restart"),
            ]
        ]
    )
    await bot.send_message(tg_id, "\n".join(lines), reply_markup=kb)


@dp.callback_query(F.data.startswith("confirm|"))
async def on_confirm(callback: CallbackQuery):
    _, action = callback.data.split("|", 1)
    tg_id = callback.from_user.id
    pending_custom.pop(tg_id, None)
    if action == "ok":
        await callback.message.edit_text("Заказ подтверждён, увидимся на обеде 🍽")
        await bot.send_message(
            tg_id, "Изменить заказ можно кнопками ниже.", reply_markup=main_keyboard(tg_id)
        )
    else:
        storage.clear_today_order(tg_id)
        await callback.message.edit_text("Начинаем заново.")
        await send_category(tg_id, CATEGORY_ORDER[0])
    await callback.answer()


@dp.message(Command("myorder"))
async def cmd_myorder(message: Message):
    """Сотрудник в любой момент может посмотреть и пересобрать свой заказ на сегодня."""
    tg_id = message.from_user.id
    if storage.is_declined(tg_id):
        await message.answer(
            "Сегодня ты отказался(ась) от обеда. Передумал(а) — нажми «🍽 Собрать обед».",
            reply_markup=main_keyboard(tg_id),
        )
        return
    if not storage.get_employee_today_order(tg_id):
        await message.answer(
            "На сегодня заказа пока нет. Нажми «🍽 Собрать обед».", reply_markup=main_keyboard(tg_id)
        )
        return
    await show_final_summary(tg_id)


# ---------- Админ: сводка и напоминания ----------

def build_summary_text() -> str:
    """Текст сводного заказа — используется и командой /summary, и кнопкой в режиме админа."""
    rows = storage.get_today_orders()
    declined = storage.get_today_declined()
    if not rows and not declined:
        return "Пока никто не сделал заказ."

    # Группируем по сотрудникам, чтобы посчитать комплексные обеды у каждого
    per_employee = {}
    for tg_id, emp_name, category, dish, price in rows:
        per_employee.setdefault(tg_id, {"name": emp_name, "order": {}})
        per_employee[tg_id]["order"][category] = (dish, price)

    counts = Counter()
    total = 0
    for data in per_employee.values():
        for category, dish in combined_order(data["order"]).items():
            counts[(category, dish)] += 1
        emp_total, _ = pricing.calculate(data["order"])
        total += emp_total

    lines = ["Сводный заказ на сегодня:"]
    for category in ADMIN_CATEGORY_ORDER:
        for (cat, dish), n in sorted(counts.items(), key=lambda kv: kv[0][1]):
            if cat == category:
                lines.append(f"{ADMIN_CATEGORY_LABELS[category]} — {dish}: {n} шт.")
    lines.append(f"\nИтого к оплате: {total}₽")

    if declined:
        lines.append(f"\nБез обеда сегодня: {', '.join(n for _, n in declined)}")

    answered = set(per_employee) | {tg_id for tg_id, _ in declined}
    not_answered = [n for tg_id, n in employees.all_employees().items() if tg_id not in answered]
    if not_answered:
        lines.append(f"\nЕщё не ответили: {', '.join(not_answered)}")

    return "\n".join(lines)


def build_personal_orders_text() -> str:
    """Каждый заказ отдельно, по именам: что выбрал сотрудник и его личный итог."""
    rows = storage.get_today_orders()
    declined = storage.get_today_declined()
    if not rows and not declined:
        return "Пока никто не сделал заказ."

    per_employee = {}
    for tg_id, emp_name, category, dish, price in rows:
        per_employee.setdefault(tg_id, {"name": emp_name, "order": {}})
        per_employee[tg_id]["order"][category] = (dish, price)

    blocks = []
    for data in sorted(per_employee.values(), key=lambda d: d["name"].lower()):
        order_map = data["order"]
        lines = [f"👤 {data['name']}"]
        has_dish = False
        for category, label in CATEGORIES:
            dish, price = order_map.get(category, (None, None))
            if dish:
                has_dish = True
                price_part = f" — {price}₽" if price else ""
                lines.append(f"  {label}: {dish}{price_part}")
        if not has_dish:
            lines.append("  (все категории пропущены)")
        total, reason = pricing.calculate(order_map)
        lines.append(f"  Итого: {total}₽ ({reason})")
        blocks.append("\n".join(lines))

    text = "Персональные заказы на сегодня:\n\n" + "\n\n".join(blocks)

    if declined:
        text += "\n\n❌ Без обеда: " + ", ".join(n for _, n in sorted(declined, key=lambda x: x[1].lower()))

    answered = set(per_employee) | {tg_id for tg_id, _ in declined}
    not_answered = [n for tg_id, n in employees.all_employees().items() if tg_id not in answered]
    if not_answered:
        text += "\n\n⏳ Не ответили: " + ", ".join(not_answered)

    return text


async def do_remind() -> int:
    """Рассылает напоминания тем, кто не ответил. Возвращает число отправленных."""
    answered = {row[0] for row in storage.get_today_orders()}
    answered |= {tg_id for tg_id, _ in storage.get_today_declined()}
    sent = 0
    for tg_id, name in employees.all_employees().items():
        if tg_id not in answered:
            try:
                await bot.send_message(int(tg_id), "Напоминание: не забудь сделать заказ обеда 🍽")
                sent += 1
            except Exception:
                logging.exception(f"Не удалось отправить напоминание {tg_id}")
    return sent


@dp.message(Command("summary"))
async def cmd_summary(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(build_summary_text())


@dp.message(Command("remind"))
async def cmd_remind(message: Message):
    if not is_admin(message.from_user.id):
        return
    sent = await do_remind()
    await message.answer(f"Напоминания отправлены: {sent}")


@dp.message(Command("notify_on"))
async def cmd_notify_on(message: Message):
    if not is_admin(message.from_user.id):
        return
    settings.set_notifications_enabled(True)
    await message.answer("Рассылка меню сотрудникам включена.")


@dp.message(Command("notify_off"))
async def cmd_notify_off(message: Message):
    if not is_admin(message.from_user.id):
        return
    settings.set_notifications_enabled(False)
    await message.answer(
        "Рассылка меню сотрудникам выключена. Канал бот продолжит проверять, "
        "но сотрудников беспокоить не будет (/notify_on — включить обратно)."
    )




# ---------- Режим администратора (всё на кнопках) ----------

@dp.message(F.text == BTN_ADMIN)
async def on_btn_admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    state = "включена" if settings.notifications_enabled() else "выключена"
    await message.answer(
        f"Режим администратора. Рассылка сейчас {state}.",
        reply_markup=admin_keyboard(),
    )


@dp.message(F.text == BTN_BACK)
async def on_btn_back(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("Обычный режим.", reply_markup=main_keyboard(message.from_user.id))


@dp.message(F.text == BTN_SUMMARY)
async def on_btn_summary(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(build_summary_text(), reply_markup=admin_keyboard())


@dp.message(F.text == BTN_PERSONAL)
async def on_btn_personal(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(build_personal_orders_text(), reply_markup=admin_keyboard())


@dp.message(F.text == BTN_REMIND)
async def on_btn_remind(message: Message):
    if not is_admin(message.from_user.id):
        return
    sent = await do_remind()
    text = f"Напоминания отправлены: {sent}" if sent else "Напоминать некому — все уже ответили."
    await message.answer(text, reply_markup=admin_keyboard())


@dp.message(F.text == BTN_NOTIFY_TOGGLE)
async def on_btn_notify_toggle(message: Message):
    if not is_admin(message.from_user.id):
        return
    new_state = not settings.notifications_enabled()
    settings.set_notifications_enabled(new_state)
    text = (
        "Рассылка меню сотрудникам ВКЛЮЧЕНА."
        if new_state
        else "Рассылка меню сотрудникам ВЫКЛЮЧЕНА. Меню продолжит распознаваться, "
             "но сотрудникам отправляться не будет."
    )
    await message.answer(text, reply_markup=admin_keyboard())




def build_pickup_text() -> str:
    """
    Список для получения заказа в столовой: по категориям, каждое блюдо на своей
    строке с количеством — чтобы удобно было сверяться при получении.
    """
    rows = storage.get_today_orders()
    if not rows:
        return "Заказов на сегодня пока нет."

    # Собираем заказ каждого сотрудника, чтобы объединить его горячее с гарниром
    per_employee = {}
    for tg_id, _emp_name, category, dish, price in rows:
        per_employee.setdefault(tg_id, {})[category] = (dish, price)

    # категория/комбо -> блюдо -> количество
    grouped = {}
    for order in per_employee.values():
        for category, dish in combined_order(order).items():
            grouped.setdefault(category, Counter())[dish] += 1

    if not grouped:
        return "Заказов на сегодня пока нет."

    lines = []
    total_items = 0
    for category in ADMIN_CATEGORY_ORDER:
        dishes = grouped.get(category)
        if not dishes:
            continue
        lines.append(f"{ADMIN_CATEGORY_LABELS[category]}")
        for dish in sorted(dishes):
            count = dishes[dish]
            total_items += count
            lines.append(f"  • {dish} — {count} шт.")
        lines.append("")

    lines.append(f"Всего порций: {total_items}")
    return "\n".join(lines)


@dp.message(F.text == BTN_PICKUP)
async def on_btn_pickup(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(build_pickup_text(), reply_markup=admin_keyboard())


@dp.message(F.text == BTN_CHECK_MENU)
async def on_btn_check_menu(message: Message):
    """Смотрит последнее сообщение канала и докладывает, на какой день там меню."""
    if not is_admin(message.from_user.id):
        return
    if not config.SOURCE_CHANNEL:
        await message.answer(
            "Канал столовой не задан в .env (SOURCE_CHANNEL). Можно просто переслать мне фото меню.",
            reply_markup=admin_keyboard(),
        )
        return

    await message.answer(f"Смотрю последнее сообщение в @{config.SOURCE_CHANNEL}...")
    # force=True: проверяем заново, даже если это фото уже разбирали.
    # broadcast=False: ручная проверка только распознаёт и сохраняет — сотрудникам
    # меню не рассылаем (утренняя рассылка пройдёт как обычно).
    await _check_channel_once(force=True, broadcast=False)

    def method_suffix(day):
        label = METHOD_LABELS.get(menu_store.recognition_method(day))
        return f", распознано: {label}" if label else ""

    today = config.today()
    tomorrow = today + timedelta(days=1)
    lines = []
    if menu_store.has_menu(today):
        state = "разослано сотрудникам" if menu_store.was_broadcast(today) else "ещё не разослано"
        lines.append(
            f"📅 На сегодня ({today.strftime('%d.%m')}): меню есть, {state}{method_suffix(today)}."
        )
    else:
        lines.append(f"📅 На сегодня ({today.strftime('%d.%m')}): меню нет.")

    if menu_store.has_menu(tomorrow):
        lines.append(
            f"📅 На завтра ({tomorrow.strftime('%d.%m')}): меню есть, "
            f"разошлю утром в {config.CHECK_HOUR:02d}:{config.CHECK_MINUTE:02d}{method_suffix(tomorrow)}."
        )
    else:
        lines.append(f"📅 На завтра ({tomorrow.strftime('%d.%m')}): меню пока нет.")

    await message.answer("\n".join(lines), reply_markup=admin_keyboard())


@dp.message(F.text == BTN_RESET)
async def on_btn_reset(message: Message):
    """Обнуление всех заказов за сегодня — с подтверждением (действие необратимое)."""
    if not is_admin(message.from_user.id):
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 Да, обнулить", callback_data="reset|yes"),
                InlineKeyboardButton(text="Отмена", callback_data="reset|no"),
            ]
        ]
    )
    await message.answer(
        "Обнулить ВСЕ заказы и отказы за сегодня? Это нельзя отменить.\n"
        "Сотрудники смогут собрать заказ заново.",
        reply_markup=kb,
    )


@dp.callback_query(F.data.startswith("reset|"))
async def on_reset_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    _, action = callback.data.split("|", 1)
    if action == "yes":
        affected = storage.clear_all_today()
        await callback.message.edit_text(f"✅ Заказы за сегодня обнулены (затронуто сотрудников: {affected}).")
    else:
        await callback.message.edit_text("Отменено — заказы не тронуты.")
    await callback.answer()


# ---------- Ручной ввод названия блюда («Другой вариант») ----------
# Регистрируется ПОСЛЕДНИМ, чтобы не перехватывать кнопки и команды: сюда
# попадает только текст, который не совпал ни с одним из обработчиков выше.
@dp.message(F.text)
async def on_custom_dish_text(message: Message):
    tg_id = message.from_user.id
    category = pending_custom.get(tg_id)
    if category is None:
        return  # пользователь не вводит блюдо вручную — прочий текст игнорируем

    name = (message.text or "").strip()
    if not name:
        await message.answer("Название пустое — напиши название блюда ещё раз.")
        return

    pending_custom.pop(tg_id, None)
    emp_name = employees.get_name(tg_id) or message.from_user.full_name
    # Цена у введённого вручную блюда неизвестна (None): в сумме по ценам меню
    # оно не учитывается, но на расчёт комплексного обеда влияет как выбранное.
    storage.set_order_item(tg_id, emp_name, category, name[:60], None)
    await message.answer(f"{CATEGORY_LABELS[category]}: {name} ✅")
    await advance_to_next(tg_id, category)


async def main():
    asyncio.create_task(poll_source_channel())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
