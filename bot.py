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
import menu_store
import pricing
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

# Постоянные кнопки внизу экрана — основные действия сотрудника
BTN_COLLECT = "🍽 Собрать обед"
BTN_EDIT = "✏️ Изменить текущий обед"
BTN_DECLINE = "❌ Отказаться от обеда"
BTN_ADMIN = "⚙️ Режим администратора"

# Кнопки режима администратора
BTN_SUMMARY = "📋 Сводка заказа"
BTN_REMIND = "🔔 Напомнить не ответившим"
BTN_NOTIFY_TOGGLE = "📢 Рассылка: вкл/выкл"
BTN_CHECK_MENU = "🔄 Проверить меню сейчас"
BTN_BACK = "👤 Выйти из режима администратора"


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
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SUMMARY)],
            [KeyboardButton(text=BTN_REMIND)],
            [KeyboardButton(text=BTN_NOTIFY_TOGGLE)],
            [KeyboardButton(text=BTN_CHECK_MENU)],
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
    if not menu_store.load_menu():
        await answer("Меню на сегодня ещё не пришло. Как только появится — сразу пришлю сюда.")
        return
    storage.clear_declined(tg_id)
    storage.clear_today_order(tg_id)
    await send_category(tg_id, CATEGORY_ORDER[0])


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
    storage.set_declined(tg_id, name)
    await message.answer(
        "Записал: сегодня без обеда. Если передумаешь — нажми «🍽 Собрать обед».",
        reply_markup=main_keyboard(tg_id),
    )


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
    logging.info(f"Распознано: дата={menu_date}, салатов={len(menu.get('salad', []))}, "
                 f"супов={len(menu.get('soup', []))}, горячего={len(menu.get('hot', []))}")
    if menu_date is not None and menu_date != config.today():
        warning = (
            f"⚠️ На распознанном фото дата {menu_date.strftime('%d.%m.%Y')}, "
            f"а сегодня {config.today().strftime('%d.%m.%Y')}. Похоже, это не сегодняшнее "
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
        await _check_channel_once()


def _next_weekday_check(now: datetime, hour: int, minute: int) -> datetime:
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:  # 5=суббота, 6=воскресенье
        candidate += timedelta(days=1)
    return candidate


async def _check_channel_once(force: bool = False) -> bool:
    """force=True — обработать фото, даже если оно уже обрабатывалось (ручная проверка админом)."""
    try:
        result = await channel_scraper.fetch_latest_photo(config.SOURCE_CHANNEL)
        if not result:
            logging.info("На странице канала столовой не найдено фото")
            return False
        photo_url, image_bytes = result
        if not force and photo_url == channel_state.get_last_photo_url():
            logging.info("Фото на странице канала не изменилось с прошлой проверки")
            return False
        logging.info("Новое фото меню в канале столовой, распознаю...")
        broadcast_photo = BufferedInputFile(image_bytes, filename="menu.jpg")
        await _handle_new_menu(image_bytes, broadcast_photo)
        channel_state.set_last_photo_url(photo_url)
        return True
    except Exception:
        logging.exception("Ошибка при проверке канала столовой")
        return False


async def broadcast_menu_to_employees(photo):
    """photo — либо file_id (строка, из Telegram), либо BufferedInputFile (из скрапера канала)."""
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
        price = f" — {item['price']}₽" if item.get("price") else ""
        label = f"{item['name']}{price}"[:60]
        # В callback_data кладём ИНДЕКС блюда, а не название: лимит Telegram — 64 байта,
        # а кириллическое название легко занимает 90+ байт и кнопка не отправится вовсе.
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"pick|{category}|{idx}")])
    buttons.append([InlineKeyboardButton(text="Пропустить", callback_data=f"pick|{category}|skip")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


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

    menu = menu_store.load_menu()
    if not menu or category not in CATEGORY_LABELS:
        await callback.answer("Меню на сегодня ещё не пришло — попробуй позже.", show_alert=True)
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

    next_idx = CATEGORY_ORDER.index(category) + 1
    if next_idx < len(CATEGORY_ORDER):
        await send_category(tg_id, CATEGORY_ORDER[next_idx])
    else:
        await show_final_summary(tg_id)


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
        for category, (dish, price) in data["order"].items():
            if dish:
                counts[(CATEGORY_LABELS.get(category, category), dish)] += 1
        emp_total, _ = pricing.calculate(data["order"])
        total += emp_total

    lines = ["Сводный заказ на сегодня:"]
    for (cat, dish), n in sorted(counts.items()):
        lines.append(f"{cat} — {dish}: {n} шт.")
    lines.append(f"\nИтого к оплате: {total}₽")

    if declined:
        lines.append(f"\nБез обеда сегодня: {', '.join(n for _, n in declined)}")

    answered = set(per_employee) | {tg_id for tg_id, _ in declined}
    not_answered = [n for tg_id, n in employees.all_employees().items() if tg_id not in answered]
    if not_answered:
        lines.append(f"\nЕщё не ответили: {', '.join(not_answered)}")

    return "\n".join(lines)


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


@dp.message(F.text == BTN_CHECK_MENU)
async def on_btn_check_menu(message: Message):
    """Проверить канал прямо сейчас, не дожидаясь расписания."""
    if not is_admin(message.from_user.id):
        return
    if not config.SOURCE_CHANNEL:
        await message.answer(
            "Канал столовой не задан в .env (SOURCE_CHANNEL). Можно просто переслать мне фото меню.",
            reply_markup=admin_keyboard(),
        )
        return
    await message.answer(f"Проверяю @{config.SOURCE_CHANNEL}...", reply_markup=admin_keyboard())
    found = await _check_channel_once(force=True)
    if not found:
        await message.answer(
            "Нового фото в канале не нашёл. Если меню уже опубликовано — перешлите мне фото сюда.",
            reply_markup=admin_keyboard(),
        )


async def main():
    asyncio.create_task(poll_source_channel())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
