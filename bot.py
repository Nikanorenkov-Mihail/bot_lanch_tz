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
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import channel_scraper
import channel_state
import config
import employees
import menu_store
import settings
import storage
from menu_parser import parse_menu_image


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


# ---------- Регистрация сотрудника ----------

@dp.message(Command("start"))
async def cmd_start(message: Message):
    name = employees.get_name(message.from_user.id)
    if name:
        await message.answer(f"Привет, {name}! Как только в канале появится меню — пришлю кнопки для заказа.")
    else:
        employees.register(message.from_user.id, message.from_user.full_name)
        await message.answer(
            f"Привет! Записал тебя как «{message.from_user.full_name}».\n"
            f"Если хочешь другое имя — напиши /rename Имя Фамилия.\n"
            f"Как только в канале появится сегодняшнее меню, пришлю кнопки для заказа обеда."
        )


@dp.message(Command("rename"))
async def cmd_rename(message: Message):
    new_name = message.text.replace("/rename", "", 1).strip()
    if not new_name:
        await message.answer("Использование: /rename Имя Фамилия")
        return
    employees.register(message.from_user.id, new_name)
    await message.answer(f"Готово, теперь ты «{new_name}».")


# ---------- Приём меню из канала столовой ----------

async def _handle_new_menu(image_bytes: bytes, broadcast_photo):
    """Общая логика: распознать фото, сверить дату и разослать — откуда бы оно ни пришло."""
    try:
        menu = parse_menu_image(image_bytes)
    except Exception as e:
        logging.exception("Не удалось распознать меню с фото")
        for admin_id in config.ADMIN_IDS:
            await bot.send_message(int(admin_id), f"⚠️ Не смог распознать сегодняшнее меню: {e}")
        return

    menu_date = menu.pop("date", None)
    if menu_date is not None and menu_date != date.today():
        warning = (
            f"⚠️ На распознанном фото дата {menu_date.strftime('%d.%m.%Y')}, "
            f"а сегодня {date.today().strftime('%d.%m.%Y')}. Похоже, это не сегодняшнее "
            f"меню — перешлите актуальное фото боту вручную."
        )
        for admin_id in config.ADMIN_IDS:
            await bot.send_message(int(admin_id), warning)
        return

    menu_store.save_menu(menu)

    if not settings.notifications_enabled():
        for admin_id in config.ADMIN_IDS:
            await bot.send_message(
                int(admin_id),
                "Меню распознано, но рассылка сотрудникам сейчас выключена (/notify_on, чтобы включить).",
            )
        return

    await broadcast_menu_to_employees(broadcast_photo)


async def process_menu_photo(photo):
    """photo — aiogram PhotoSize из канала (если бот в нём состоит) или от админа в личке."""
    file = await bot.get_file(photo.file_id)
    buffer = await bot.download_file(file.file_path)
    await _handle_new_menu(buffer.read(), photo.file_id)


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
    if str(message.from_user.id) not in config.ADMIN_IDS:
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
        await _check_channel_once()


def _next_weekday_check(now: datetime, hour: int, minute: int) -> datetime:
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:  # 5=суббота, 6=воскресенье
        candidate += timedelta(days=1)
    return candidate


async def _check_channel_once():
    try:
        result = await channel_scraper.fetch_latest_photo(config.SOURCE_CHANNEL)
        if not result:
            logging.info("На странице канала столовой не найдено фото")
            return
        photo_url, image_bytes = result
        if photo_url == channel_state.get_last_photo_url():
            logging.info("Фото на странице канала не изменилось с прошлой проверки")
            return
        logging.info("Новое фото меню в канале столовой, распознаю...")
        broadcast_photo = BufferedInputFile(image_bytes, filename="menu.jpg")
        await _handle_new_menu(image_bytes, broadcast_photo)
        channel_state.set_last_photo_url(photo_url)
    except Exception:
        logging.exception("Ошибка при проверке канала столовой")


async def broadcast_menu_to_employees(photo):
    """photo — либо file_id (строка, из Telegram), либо BufferedInputFile (из скрапера канала)."""
    for tg_id in employees.all_employees():
        try:
            storage.clear_today_order(int(tg_id))  # новое меню — начинаем заказ с чистого листа
            # Показываем оригинал фото — если OCR где-то ошибся в названии или цене,
            # сотрудник сразу это увидит и сверит с картинкой.
            await bot.send_photo(int(tg_id), photo, caption="Сегодняшнее меню 👆")
            await send_category(int(tg_id), CATEGORY_ORDER[0])
        except Exception:
            logging.exception(f"Не удалось отправить меню сотруднику {tg_id}")


def build_keyboard(category: str, menu: dict) -> InlineKeyboardMarkup:
    items = menu.get(category) or []
    buttons = []
    for item in items:
        price = f" — {item['price']}₽" if item.get("price") else ""
        label = f"{item['name']}{price}"[:60]
        # callback_data ограничен 64 байтами — режем название блюда
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"pick|{category}|{item['name'][:45]}")])
    buttons.append([InlineKeyboardButton(text="Пропустить", callback_data=f"pick|{category}|__skip__")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def send_category(tg_id: int, category: str):
    menu = menu_store.load_menu()
    if not menu:
        return
    label = CATEGORY_LABELS[category]
    kb = build_keyboard(category, menu)
    await bot.send_message(tg_id, f"Выбери: {label}", reply_markup=kb)


@dp.callback_query(F.data.startswith("pick|"))
async def on_pick(callback: CallbackQuery):
    _, category, dish = callback.data.split("|", 2)
    tg_id = callback.from_user.id
    name = employees.get_name(tg_id) or callback.from_user.full_name

    menu = menu_store.load_menu()
    if not menu:
        await callback.answer("Меню на сегодня ещё не пришло — попробуй позже.", show_alert=True)
        return

    price = None
    if dish != "__skip__":
        matched = False
        for item in menu.get(category, []):
            if item["name"][:45] == dish:
                price = item.get("price")
                matched = True
                break
        if not matched:
            # Кнопка из вчерашнего/устаревшего сообщения — блюда с таким именем
            # в сегодняшнем меню уже нет. Просим начать заново, а не сохраняем мусор.
            await callback.answer("Это меню уже неактуально. Нажми /start и попробуй снова.", show_alert=True)
            return
        storage.set_order_item(tg_id, name, category, dish, price)
    else:
        storage.set_order_item(tg_id, name, category, None, None)

    shown = "—" if dish == "__skip__" else dish
    await callback.message.edit_text(f"{CATEGORY_LABELS[category]}: {shown} ✅")
    await callback.answer()

    next_idx = CATEGORY_ORDER.index(category) + 1
    if next_idx < len(CATEGORY_ORDER):
        await send_category(tg_id, CATEGORY_ORDER[next_idx])
    else:
        await show_final_summary(tg_id)


def format_order_lines(rows) -> list[str]:
    """rows: [(category, dish, price), ...] — только с чем-то выбранным."""
    order_map = {category: (dish, price) for category, dish, price in rows}
    lines = []
    total = 0
    for category, label in CATEGORIES:
        dish, price = order_map.get(category, (None, None))
        if dish:
            price_part = f" — {price}₽" if price else ""
            lines.append(f"{label}: {dish}{price_part}")
            if price:
                total += price
    if not lines:
        lines.append("Пусто — все категории пропущены.")
    else:
        lines.append(f"\nИтого: {total}₽")
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
    if action == "ok":
        await callback.message.edit_text("Заказ подтверждён, увидимся на обеде 🍽")
    else:
        storage.clear_today_order(tg_id)
        await callback.message.edit_text("Начинаем заново.")
        await send_category(tg_id, CATEGORY_ORDER[0])
    await callback.answer()


@dp.message(Command("myorder"))
async def cmd_myorder(message: Message):
    """Сотрудник в любой момент может посмотреть и пересобрать свой заказ на сегодня."""
    rows = storage.get_employee_today_order(message.from_user.id)
    if not rows:
        await message.answer("На сегодня ты пока ничего не заказал(а). Дождись меню или напиши /start.")
        return
    await show_final_summary(message.from_user.id)


# ---------- Админ: сводка и напоминания ----------

@dp.message(Command("summary"))
async def cmd_summary(message: Message):
    if str(message.from_user.id) not in config.ADMIN_IDS:
        return
    rows = storage.get_today_orders()
    if not rows:
        await message.answer("Пока никто не сделал заказ.")
        return

    counts = Counter()
    total = 0
    answered_ids = set()
    for tg_id, _, category, dish, price in rows:
        answered_ids.add(tg_id)
        if not dish:
            continue
        counts[(CATEGORY_LABELS.get(category, category), dish)] += 1
        if price:
            total += price

    lines = ["Сводный заказ на сегодня:"]
    for (cat, dish), n in sorted(counts.items()):
        lines.append(f"{cat} — {dish}: {n} шт.")
    lines.append(f"\nПримерная сумма: {total}₽")

    not_answered = [n for tg_id, n in employees.all_employees().items() if tg_id not in answered_ids]
    if not_answered:
        lines.append(f"\nЕщё не ответили: {', '.join(not_answered)}")

    await message.answer("\n".join(lines))


@dp.message(Command("remind"))
async def cmd_remind(message: Message):
    if str(message.from_user.id) not in config.ADMIN_IDS:
        return
    rows = storage.get_today_orders()
    answered_ids = {row[0] for row in rows}
    sent = 0
    for tg_id, name in employees.all_employees().items():
        if tg_id not in answered_ids:
            try:
                await bot.send_message(int(tg_id), "Напоминание: не забудь сделать заказ обеда 🍽")
                sent += 1
            except Exception:
                logging.exception(f"Не удалось отправить напоминание {tg_id}")
    await message.answer(f"Напоминания отправлены: {sent}")


@dp.message(Command("notify_on"))
async def cmd_notify_on(message: Message):
    if str(message.from_user.id) not in config.ADMIN_IDS:
        return
    settings.set_notifications_enabled(True)
    await message.answer("Рассылка меню сотрудникам включена.")


@dp.message(Command("notify_off"))
async def cmd_notify_off(message: Message):
    if str(message.from_user.id) not in config.ADMIN_IDS:
        return
    settings.set_notifications_enabled(False)
    await message.answer(
        "Рассылка меню сотрудникам выключена. Канал бот продолжит проверять, "
        "но сотрудников беспокоить не будет (/notify_on — включить обратно)."
    )


async def main():
    asyncio.create_task(poll_source_channel())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
