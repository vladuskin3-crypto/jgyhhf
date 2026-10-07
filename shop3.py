"""
Телеграм-бот магазин (aiogram 3.x) — профиль, баланс, пополнение, каталог
Установка:  pip install aiogram
Запуск:     python shop_bot.py
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import tempfile
import time
from html import escape, unescape
from types import SimpleNamespace
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramRetryAfter
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

BOT_TOKEN = os.getenv("BOT_TOKEN", "8900240410:AAEL7VHL4CeMNPtHTI-nmGh8ue3qJhfNgt4")
ADMIN_ID = 8759747461   # единственный администратор (только этот Telegram ID)
SUPPORT = "@Sahkapq"                    # контакт поддержки
WEBAPP_URL = os.getenv("WEBAPP_URL", "")   # https-адрес мини-приложения (пусто = выключено)
MIN_TOPUP = 50                                # минимальное пополнение, ₽

# Реквизиты для пополнения через крипто-бот (меняй на свои)
PAY_BOT = "CryptoBot (@send)"        # через какой бот принимаешь
PAY_LINK = "https://t.me/send?start=IVXbX0mcEeBt"  # ссылка на счёт в CryptoBot
PAY_ACCOUNT = "@Sahkapq"             # твой @username в крипто-боте
PAY_COIN = "USDT"                    # монета
USDT_RATE = 90                       # курс: сколько ₽ в 1 USDT (для пересчёта в счёте)

# ───────── Каталог: id -> (название, цена ₽, описание, остаток) ─────────
PRODUCTS = {
    "gu70":  ["ГУ 70+",      0, "Описание товара ГУ 70+",      99],
    "gu50":  ["ГУ 50+",      0, "Описание товара ГУ 50+",      99],
    "gugk":  ["ГУ под ГК",   0, "Описание товара ГУ под ГК",   99],
    "gut2":  ["ГУ под Т2",   0, "Описание товара ГУ под Т2",   99],
    "gumts": ["ГУ под МТС",  0, "Описание товара ГУ под МТС",  99],
    "gubil": ["ГУ под Бил",  0, "Описание товара ГУ под Бил",  99],
    "gumeg": ["ГУ под Мега", 0, "Описание товара ГУ под Мега", 99],
}

# Категории каталога: ключ -> (название кнопки, список id товаров)
CATEGORIES = {
    "gos": ("🏛 Госсы",      ["gu70", "gu50"]),
    "sim": ("📱 Сим карты",  ["gugk", "gut2", "gumts", "gubil", "gumeg"]),
}


def cat_of(pid: str) -> str:
    return next(k for k, (_, ids) in CATEGORIES.items() if pid in ids)


LINE = "━━━━━━━━━━━━━━━"

# ───────── Цвета кнопок (настраиваются в админ-панели) ─────────
STYLE_NAMES = {"primary": "🔵 Синий", "success": "🟢 Зелёный", "danger": "🔴 Красный", "def": "⚪️ Обычный"}
BTN_LABELS = {
    "cat": "🛒 Каталог", "profile": "👤 Профиль", "my": "📦 Мои заказы", "sup": "💬 Поддержка",
    "manuals": "📚 Мануалы", "reviews": "⭐ Отзывы",
    "sections": "📂 Кнопки разделов", "products": "🛍 Кнопки товаров",
    "buy": "💳 Купить", "topup": "➕ Пополнить баланс", "paid": "✅ Я отправил(а) средства",
}
DEFAULT_STYLES = {"cat": "primary", "profile": "danger", "my": "danger", "sup": "primary"}
STYLES = {}

# ───────── База данных ─────────
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shop.db")
db = sqlite3.connect(DB_PATH)
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, username TEXT, balance REAL DEFAULT 0,
    joined TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, product TEXT,
    price REAL, created TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS topups(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL,
    status TEXT DEFAULT 'created',
    created TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
""")
# миграция: в старых версиях базы нет колонки price у заказов
if "price" not in [r[1] for r in db.execute("PRAGMA table_info(orders)")]:
    db.execute("ALTER TABLE orders ADD COLUMN price REAL DEFAULT 0")
db.commit()

db.execute("""CREATE TABLE IF NOT EXISTS products(
    id TEXT PRIMARY KEY, name TEXT, price INTEGER, descr TEXT,
    stock INTEGER, cat TEXT, pos INTEGER)""")
if not db.execute("SELECT 1 FROM products").fetchone():
    for _pos, (_pid, (_n, _pr, _d, _st)) in enumerate(PRODUCTS.items()):
        db.execute("INSERT INTO products VALUES(?,?,?,?,?,?,?)",
                   (_pid, _n, _pr, _d, _st, cat_of(_pid), _pos))
db.commit()


db.executescript("""
CREATE TABLE IF NOT EXISTS categories(
    key TEXT PRIMARY KEY, title TEXT, pos INTEGER);
CREATE TABLE IF NOT EXISTS btn_styles(
    key TEXT PRIMARY KEY, style TEXT);
""")
if not db.execute("SELECT 1 FROM categories").fetchone():
    for _pos, (_key, (_title, _ids)) in enumerate(CATEGORIES.items()):
        db.execute("INSERT INTO categories VALUES(?,?,?)", (_key, _title, _pos))
if not db.execute("SELECT 1 FROM btn_styles").fetchone():
    for _k, _v in DEFAULT_STYLES.items():
        db.execute("INSERT INTO btn_styles VALUES(?,?)", (_k, _v))
db.commit()


def load_products():
    """Загружает разделы и товары из базы в память (PRODUCTS и CATEGORIES)."""
    PRODUCTS.clear()
    CATEGORIES.clear()
    for key, title in db.execute("SELECT key, title FROM categories ORDER BY pos"):
        CATEGORIES[key] = (title, [])
    for pid, name, price, descr, stock, cat in db.execute(
            "SELECT id, name, price, descr, stock, cat FROM products ORDER BY pos"):
        PRODUCTS[pid] = [name, price, descr, stock]
        if cat in CATEGORIES:
            CATEGORIES[cat][1].append(pid)


def load_styles():
    STYLES.clear()
    STYLES.update(dict(db.execute("SELECT key, style FROM btn_styles")))


def st(key: str):
    """Цвет кнопки из настроек (None = обычная кнопка)."""
    return STYLES.get(key) or None


def pname(pid: str) -> str:
    return PRODUCTS[pid][0] if pid in PRODUCTS else "(товар удалён)"


load_products()
load_styles()

dp = Dispatcher()


class TopUp(StatesGroup):
    amount = State()


def get_user(u) -> tuple:
    db.execute("INSERT OR IGNORE INTO users(id, username) VALUES(?,?)", (u.id, u.username or "-"))
    db.commit()
    return db.execute("SELECT balance FROM users WHERE id=?", (u.id,)).fetchone()


def fmt(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ")


WELCOME = (
    "<b>Вас приветствует Dodo pizza🍕</b>\n\n"
    "В этом боте вы можете:\n"
    "• полезные госсы/подписи/паспорта и тд\n"
    "• быстро и хорошо все купить и оставить отзыв\n"
    "• Посмотреть мануалы, как правильно действовать.\n\n"
    "Бот работает в автоматическом режиме"
)


def menu_text(bal: float) -> str:
    """Текст главного меню: приветствие + баланс."""
    return f"{WELCOME}\n{LINE}\n💰 Баланс: <b>{fmt(bal)} ₽</b>"


# ───────── Клавиатуры ─────────
def main_menu(uid=None):
    kb = InlineKeyboardBuilder()
    if WEBAPP_URL:
        kb.button(text="🛍 Открыть магазин", web_app=WebAppInfo(url=WEBAPP_URL), style="success")
    kb.button(text="🛒 Каталог", callback_data="cat", style=st("cat"))
    kb.button(text="👤 Профиль", callback_data="profile", style=st("profile"))
    kb.button(text="📦 Мои заказы", callback_data="my", style=st("my"))
    kb.button(text="📚 Мануалы", callback_data="man", style=st("manuals"))
    kb.button(text="⭐ Отзывы", callback_data="rev", style=st("reviews"))
    kb.button(text="💬 Поддержка", callback_data="sup", style=st("sup"))
    kb.adjust(*((1, 1, 2, 2, 1) if WEBAPP_URL else (1, 2, 2, 1)))
    return kb.as_markup()


def back_kb(to="menu", text="⬅️ В меню"):
    kb = InlineKeyboardBuilder()
    kb.button(text=text, callback_data=to)
    return kb.as_markup()


def catalog_kb():
    kb = InlineKeyboardBuilder()
    for key, (title, _) in CATEGORIES.items():
        kb.button(text=title, callback_data=f"c:{key}", style=st("sections"))
    kb.button(text="⬅️ В меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


def category_kb(key: str):
    kb = InlineKeyboardBuilder()
    for pid in CATEGORIES[key][1]:
        name, price, _, stock = PRODUCTS[pid]
        kb.button(text=f"{unescape(name)} • {fmt(price)} ₽" if stock > 0 else f"{unescape(name)} • нет в наличии",
                  callback_data=f"p:{pid}", style=st("products"))
    kb.button(text="⬅️ Назад", callback_data="cat")
    kb.adjust(1)
    return kb.as_markup()


# ───────── Меню и каталог ─────────
@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    (bal,) = get_user(m.from_user)
    await m.answer(menu_text(bal), reply_markup=main_menu(m.from_user.id))


@dp.callback_query(F.data == "menu")
async def menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    (bal,) = get_user(c.from_user)
    await c.message.edit_text(menu_text(bal), reply_markup=main_menu(c.from_user.id))


@dp.callback_query(F.data == "cat")
async def catalog(c: CallbackQuery):
    await c.message.edit_text(f"🛒 <b>Каталог</b>\n{LINE}\nВыбери раздел:", reply_markup=catalog_kb())


@dp.callback_query(F.data.startswith("c:"))
async def category(c: CallbackQuery):
    key = c.data[2:]
    if key not in CATEGORIES:
        return await c.answer("Этот раздел больше недоступен", show_alert=True)
    hint = "Выбери товар:" if CATEGORIES[key][1] else "Пока здесь нет товаров."
    await c.message.edit_text(f"{escape(CATEGORIES[key][0])}\n{LINE}\n{hint}", reply_markup=category_kb(key))


@dp.callback_query(F.data.startswith("p:"))
async def product(c: CallbackQuery):
    pid = c.data[2:]
    if pid not in PRODUCTS:
        return await c.answer("Этот товар больше недоступен", show_alert=True)
    name, price, desc, stock = PRODUCTS[pid]
    kb = InlineKeyboardBuilder()
    if stock > 0:
        kb.button(text=f"💳 Купить за {fmt(price)} ₽", callback_data=f"buy:{pid}", style=st("buy"))
    kb.button(text="⬅️ Назад", callback_data=f"c:{cat_of(pid)}")
    kb.adjust(1)
    await c.message.edit_text(
        f"🛍 <b>{name}</b>\n{LINE}\n{desc}\n{LINE}\n"
        f"💰 Цена: <b>{fmt(price)} ₽</b>\n📦 В наличии: <b>{stock}</b>",
        reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("buy:"))
async def buy(c: CallbackQuery):
    pid = c.data[4:]
    if pid not in PRODUCTS:
        return await c.answer("Этот товар больше недоступен", show_alert=True)
    name, price, _, stock = PRODUCTS[pid]
    (bal,) = get_user(c.from_user)
    if stock <= 0:
        return await c.answer("Товара нет в наличии", show_alert=True)
    if bal < price:
        kb = InlineKeyboardBuilder()
        kb.button(text="➕ Пополнить баланс", callback_data="topup", style=st("topup"))
        kb.button(text="⬅️ Назад", callback_data=f"p:{pid}")
        kb.adjust(1)
        return await c.message.edit_text(
            f"😕 <b>Недостаточно средств</b>\n{LINE}\n"
            f"Цена: {fmt(price)} ₽\nБаланс: {fmt(bal)} ₽\n"
            f"Не хватает: <b>{fmt(price - bal)} ₽</b>", reply_markup=kb.as_markup())
    db.execute("UPDATE users SET balance=balance-? WHERE id=?", (price, c.from_user.id))
    cur = db.execute("INSERT INTO orders(user_id, product, price) VALUES(?,?,?)",
                     (c.from_user.id, pid, price))
    db.commit()
    PRODUCTS[pid][3] -= 1
    db.execute("UPDATE products SET stock=? WHERE id=?", (PRODUCTS[pid][3], pid))
    db.commit()
    oid = cur.lastrowid
    await c.message.edit_text(
        f"🎉 <b>Заказ #{oid} оплачен!</b>\n{LINE}\n"
        f"Товар: {name}\nСписано: {fmt(price)} ₽\nОстаток: {fmt(bal - price)} ₽\n{LINE}\n"
        f"Менеджер скоро передаст товар. Вопросы: {SUPPORT}", reply_markup=main_menu())
    if ADMIN_ID:
        u = c.from_user
        await c.bot.send_message(ADMIN_ID, f"🆕 <b>Заказ #{oid}</b> (оплачен с баланса)\n"
                                 f"Товар: {name} — {fmt(price)} ₽\nКлиент: @{u.username or '-'} (id {u.id})")


# ───────── Профиль ─────────
@dp.callback_query(F.data == "profile")
async def profile(c: CallbackQuery, state: FSMContext):
    await state.clear()
    u = c.from_user
    (bal,) = get_user(u)
    n, spent = db.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE user_id=?",
                          (u.id,)).fetchone()
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Пополнить баланс", callback_data="topup", style=st("topup"))
    kb.button(text="📦 Мои заказы", callback_data="my")
    kb.button(text="⬅️ В меню", callback_data="menu")
    kb.adjust(1)
    await c.message.edit_text(
        f"👤 <b>Мой профиль</b>\n{LINE}\n"
        f"🆔 ID: <code>{u.id}</code>\n"
        f"👤 Имя: {escape(u.first_name or '-')}\n{LINE}\n"
        f"💰 Баланс: <b>{fmt(bal)} ₽</b>\n"
        f"🛍 Покупок: <b>{n}</b>\n"
        f"💸 Потрачено: <b>{fmt(spent)} ₽</b>", reply_markup=kb.as_markup())


# ───────── Пополнение баланса ─────────
@dp.callback_query(F.data == "topup")
async def topup(c: CallbackQuery):
    kb = InlineKeyboardBuilder()
    for a in (100, 300, 500, 1000, 2000):
        kb.button(text=f"{fmt(a)} ₽", callback_data=f"ta:{a}")
    kb.button(text="✏️ Другая сумма", callback_data="ta:custom")
    kb.button(text="⬅️ Назад", callback_data="profile")
    kb.adjust(3, 2, 1, 1)
    await c.message.edit_text(f"➕ <b>Пополнение баланса</b>\n{LINE}\nВыбери сумму:",
                              reply_markup=kb.as_markup())


@dp.callback_query(F.data == "ta:custom")
async def topup_custom(c: CallbackQuery, state: FSMContext):
    await state.set_state(TopUp.amount)
    await c.message.edit_text(f"✏️ Введи сумму пополнения числом (от {MIN_TOPUP} ₽):",
                              reply_markup=back_kb("topup", "⬅️ Назад"))


@dp.message(TopUp.amount)
async def topup_amount(m: Message, state: FSMContext):
    txt = (m.text or "").replace(" ", "")
    if not txt.isdigit() or int(txt) < MIN_TOPUP or int(txt) > 100000:
        return await m.answer(f"⚠️ Введи целое число от {MIN_TOPUP} до 100 000.")
    await state.clear()
    get_user(m.from_user)
    await send_invoice(m, int(txt), m.from_user.id)


@dp.callback_query(F.data.regexp(r"^ta:\d+$"))
async def topup_fixed(c: CallbackQuery):
    amount = int(c.data[3:])
    if amount < MIN_TOPUP or amount > 100000:
        return await c.answer("Недопустимая сумма", show_alert=True)
    get_user(c.from_user)
    await send_invoice(c.message, amount, c.from_user.id, edit=True)


async def send_invoice(msg: Message, amount: int, uid: int, edit=False):
    cur = db.execute("INSERT INTO topups(user_id, amount) VALUES(?,?)", (uid, amount))
    db.commit()
    tid = cur.lastrowid
    kb = InlineKeyboardBuilder()
    kb.button(text=f"💸 Оплатить в CryptoBot", url=PAY_LINK)
    kb.button(text="✅ Я отправил(а) средства", callback_data=f"paid:{tid}", style=st("paid"))
    kb.button(text="❌ Отмена", callback_data=f"tcancel:{tid}")
    kb.adjust(1)
    text = (f"🧾 <b>Счёт на пополнение #{tid}</b>\n{LINE}\n"
            f"💵 Сумма к оплате: <b>{fmt(amount)} ₽</b>\n{LINE}\n"
            f"🪙 Оплата в {PAY_COIN}: <b>≈ {amount / USDT_RATE:.2f} {PAY_COIN}</b>\n{LINE}\n"
            f"🤖 Крипто-бот: {PAY_BOT}\n"
            f"👤 Счёт получателя: <code>{PAY_ACCOUNT}</code>\n"
            f"📝 Комментарий к переводу: <code>#{tid}</code>\n{LINE}\n"
            f"1️⃣ Отправь {PAY_COIN} на счёт выше (в крипто-боте)\n2️⃣ Нажми «Я отправил(а) средства»\n"
            f"3️⃣ После проверки баланс пополнится")
    await (msg.edit_text if edit else msg.answer)(text, reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("tcancel:"))
async def topup_cancel(c: CallbackQuery):
    db.execute("UPDATE topups SET status='cancel' WHERE id=? AND user_id=? AND status='created'",
               (c.data[8:], c.from_user.id))
    db.commit()
    await c.message.edit_text("❌ Счёт отменён.", reply_markup=back_kb("profile", "👤 В профиль"))


@dp.callback_query(F.data.startswith("paid:"))
async def topup_paid(c: CallbackQuery):
    tid = c.data[5:]
    row = db.execute("SELECT amount, status FROM topups WHERE id=? AND user_id=?",
                     (tid, c.from_user.id)).fetchone()
    if not row or row[1] != "created":
        return await c.answer("Счёт уже обработан", show_alert=True)
    db.execute("UPDATE topups SET status='pending' WHERE id=?", (tid,))
    db.commit()
    await c.message.edit_text(
        f"⏳ <b>Заявка #{tid} отправлена!</b>\n{LINE}\n"
        f"Сумма: {fmt(row[0])} ₽\nОжидай подтверждения — обычно несколько минут.\n"
        f"Вопросы: {SUPPORT}", reply_markup=back_kb("profile", "👤 В профиль"))
    if ADMIN_ID:
        u = c.from_user
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Зачислить", callback_data=f"tok:{tid}")
        kb.button(text="❌ Отклонить", callback_data=f"tno:{tid}")
        await c.bot.send_message(
            ADMIN_ID, f"💳 <b>Заявка на пополнение #{tid}</b>\n{LINE}\n"
            f"Сумма: <b>{fmt(row[0])} ₽</b>\nКлиент: @{u.username or '-'} (id {u.id})",
            reply_markup=kb.as_markup())


@dp.callback_query(F.data.regexp(r"^t(ok|no):\d+$"))
async def topup_admin(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("Нет доступа", show_alert=True)
    act, tid = c.data.split(":")
    row = db.execute("SELECT user_id, amount, status FROM topups WHERE id=?", (tid,)).fetchone()
    if not row or row[2] != "pending":
        return await c.answer("Заявка уже обработана", show_alert=True)
    uid, amount, _ = row
    if act == "tok":
        db.execute("UPDATE topups SET status='done' WHERE id=?", (tid,))
        db.execute("UPDATE users SET balance=balance+? WHERE id=?", (amount, uid))
        db.commit()
        (bal,) = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()
        await c.message.edit_text(f"{c.message.text}\n\n✅ Зачислено")
        await c.bot.send_message(uid, f"✅ <b>Баланс пополнен на {fmt(amount)} ₽</b>\n"
                                 f"💰 Текущий баланс: <b>{fmt(bal)} ₽</b>", reply_markup=main_menu())
    else:
        db.execute("UPDATE topups SET status='rejected' WHERE id=?", (tid,))
        db.commit()
        await c.message.edit_text(f"{c.message.text}\n\n❌ Отклонено")
        await c.bot.send_message(uid, f"❌ Заявка #{tid} отклонена — оплата не найдена.\n"
                                 f"Если это ошибка, напиши: {SUPPORT}")


# ───────── Заказы / поддержка / админ ─────────
@dp.callback_query(F.data == "my")
async def my_orders(c: CallbackQuery):
    rows = db.execute("SELECT id, product, price FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10",
                      (c.from_user.id,)).fetchall()
    body = "\n".join(f"#{i} • {pname(p)} • {fmt(pr)} ₽" for i, p, pr in rows) or "Пока пусто."
    await c.message.edit_text(f"📦 <b>Мои заказы</b>\n{LINE}\n{body}", reply_markup=back_kb())


@dp.callback_query(F.data == "sup")
async def support(c: CallbackQuery):
    await c.message.edit_text(f"💬 <b>Поддержка</b>\n{LINE}\nПиши: {SUPPORT}", reply_markup=back_kb())


@dp.message(Command("addbalance"))
async def add_balance(m: Message):
    """Админ: /addbalance <user_id> <сумма>"""
    if m.from_user.id != ADMIN_ID:
        return
    try:
        uid, amt = int(m.text.split()[1]), float(m.text.split()[2])
    except (IndexError, ValueError):
        return await m.answer("Формат: /addbalance <user_id> <сумма>")
    cur = db.execute("UPDATE users SET balance=balance+? WHERE id=?", (amt, uid))
    db.commit()
    if not cur.rowcount:
        return await m.answer("⚠️ Пользователь не найден (он должен хотя бы раз запустить бота).")
    await m.answer("Готово ✅")


# ───────── Админ-панель ─────────
ADM = F.from_user.id == ADMIN_ID


class Adm(StatesGroup):
    price = State()
    stock = State()
    name = State()
    aprice = State()
    adesc = State()
    cat = State()
    newcat = State()
    bcast = State()
    rname = State()
    rrate = State()
    rtext = State()
    usearch = State()
    ubal = State()


def adm_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="💰 Изменить цену", callback_data="adm:price")
    kb.button(text="📦 Изменить остаток", callback_data="adm:stock")
    kb.button(text="➕ Добавить товар", callback_data="adm:add")
    kb.button(text="🗑 Удалить товар", callback_data="adm:del")
    kb.button(text="🗂 Добавить раздел", callback_data="adm:newcat")
    kb.button(text="🎨 Цвета кнопок", callback_data="adm:colors")
    kb.button(text="📢 Объявление", callback_data="adm:bcast")
    kb.button(text="📊 Статистика", callback_data="adm:stats")
    kb.button(text="✍️ Добавить отзыв", callback_data="adm:radd")
    kb.button(text="🗑 Удалить отзыв", callback_data="adm:rdel")
    kb.button(text="👤 Пользователь", callback_data="adm:user")
    kb.button(text="⏳ Заявки на пополнение", callback_data="adm:pend")
    kb.button(text="💾 Бэкап базы", callback_data="adm:backup")
    kb.button(text="⬅️ В меню", callback_data="menu")
    kb.adjust(2, 2, 2, 2, 2, 2, 2)
    return kb.as_markup()


def adm_text() -> str:
    users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    orders, money = db.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders").fetchone()
    return (f"🛠 <b>Админ-панель</b>\n{LINE}\n"
            f"👥 Пользователей: <b>{users}</b>\n🛍 Заказов: <b>{orders}</b>\n"
            f"💰 Продано на: <b>{fmt(money)} ₽</b>\n📦 Товаров: <b>{len(PRODUCTS)}</b>\n🗂 Разделов: <b>{len(CATEGORIES)}</b>\n{LINE}\nВыбери действие:")


def plist_kb(prefix: str):
    kb = InlineKeyboardBuilder()
    for pid, (name, price, _, stock) in PRODUCTS.items():
        kb.button(text=f"{unescape(name)} • {fmt(price)} ₽ • {stock} шт", callback_data=f"{prefix}:{pid}")
    kb.button(text="⬅️ Назад", callback_data="adm")
    kb.adjust(1)
    return kb.as_markup()


@dp.message(Command("adm"), ADM)
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(adm_text(), reply_markup=adm_kb())


@dp.callback_query(F.data == "adm", ADM)
async def admin_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text(adm_text(), reply_markup=adm_kb())


@dp.callback_query(F.data.in_({"adm:price", "adm:stock", "adm:del"}), ADM)
async def admin_pick(c: CallbackQuery):
    title, prefix = {"adm:price": ("💰 Выбери товар для смены цены:", "ap"),
                     "adm:stock": ("📦 Выбери товар для смены остатка:", "as"),
                     "adm:del": ("🗑 Выбери товар для удаления:", "ad")}[c.data]
    if not PRODUCTS:
        return await c.answer("Товаров нет", show_alert=True)
    await c.message.edit_text(f"{title}", reply_markup=plist_kb(prefix))


# --- смена цены / остатка ---
@dp.callback_query(F.data.regexp(r"^(ap|as):"), ADM)
async def admin_edit_ask(c: CallbackQuery, state: FSMContext):
    kind, pid = c.data.split(":", 1)
    if pid not in PRODUCTS:
        return await c.answer("Товар не найден", show_alert=True)
    await state.set_state(Adm.price if kind == "ap" else Adm.stock)
    await state.update_data(pid=pid)
    cur = PRODUCTS[pid][1] if kind == "ap" else PRODUCTS[pid][3]
    what = "новую цену в ₽" if kind == "ap" else "новый остаток (шт)"
    await c.message.edit_text(f"✏️ <b>{PRODUCTS[pid][0]}</b>\nСейчас: <b>{fmt(cur)}</b>\n\nВведи {what} числом:",
                              reply_markup=back_kb("adm", "⬅️ Отмена"))


async def _save_num(m: Message, state: FSMContext, field: str, idx: int):
    txt = (m.text or "").replace(" ", "")
    if not txt.isdigit():
        return await m.answer("⚠️ Введи целое число, например 250.")
    pid = (await state.get_data())["pid"]
    if pid not in PRODUCTS:
        await state.clear()
        return await m.answer("Товар не найден.", reply_markup=adm_kb())
    db.execute(f"UPDATE products SET {field}=? WHERE id=?", (int(txt), pid))
    db.commit()
    PRODUCTS[pid][idx] = int(txt)
    await state.clear()
    await m.answer(f"✅ Готово: <b>{PRODUCTS[pid][0]}</b> → {fmt(int(txt))}", reply_markup=adm_kb())


@dp.message(Adm.price, ADM)
async def admin_set_price(m: Message, state: FSMContext):
    await _save_num(m, state, "price", 1)


@dp.message(Adm.stock, ADM)
async def admin_set_stock(m: Message, state: FSMContext):
    await _save_num(m, state, "stock", 3)


# --- удаление ---
@dp.callback_query(F.data.startswith("ad:"), ADM)
async def admin_del_ask(c: CallbackQuery):
    pid = c.data[3:]
    if pid not in PRODUCTS:
        return await c.answer("Товар не найден", show_alert=True)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data=f"adc:{pid}")
    kb.button(text="⬅️ Отмена", callback_data="adm:del")
    kb.adjust(1)
    await c.message.edit_text(f"🗑 Удалить <b>{PRODUCTS[pid][0]}</b>?\nЭто действие нельзя отменить.",
                              reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("adc:"), ADM)
async def admin_del_do(c: CallbackQuery):
    pid = c.data[4:]
    name = pname(pid)
    db.execute("DELETE FROM products WHERE id=?", (pid,))
    db.commit()
    load_products()
    await c.message.edit_text(f"✅ Товар <b>{name}</b> удалён.\n\n{adm_text()}", reply_markup=adm_kb())


# --- добавление: название -> цена -> описание -> категория ---
@dp.callback_query(F.data == "adm:add", ADM)
async def admin_add_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.name)
    await c.message.edit_text("➕ <b>Новый товар</b>\n\n1/4. Введи название:",
                              reply_markup=back_kb("adm", "⬅️ Отмена"))


@dp.message(Adm.name, ADM)
async def admin_add_name(m: Message, state: FSMContext):
    if not m.text or len(m.text) > 60:
        return await m.answer("⚠️ Название — текст до 60 символов.")
    await state.update_data(name=escape(m.text.strip()))
    await state.set_state(Adm.aprice)
    await m.answer("2/4. Введи цену в ₽ (числом):")


@dp.message(Adm.aprice, ADM)
async def admin_add_price(m: Message, state: FSMContext):
    txt = (m.text or "").replace(" ", "")
    if not txt.isdigit():
        return await m.answer("⚠️ Введи целое число.")
    await state.update_data(price=int(txt))
    await state.set_state(Adm.adesc)
    await m.answer("3/4. Введи описание (или «-» чтобы пропустить):")


@dp.message(Adm.adesc, ADM)
async def admin_add_desc(m: Message, state: FSMContext):
    d = (m.text or "").strip()
    await state.update_data(descr="" if d == "-" else escape(d)[:500])
    await state.set_state(Adm.cat)
    kb = InlineKeyboardBuilder()
    for key, (title, _) in CATEGORIES.items():
        kb.button(text=title, callback_data=f"ac:{key}")
    kb.button(text="⬅️ Отмена", callback_data="adm")
    kb.adjust(1)
    await m.answer("4/4. В какой раздел добавить?", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("ac:"), Adm.cat, ADM)
async def admin_add_cat(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    key = c.data[3:]
    pos = (db.execute("SELECT COALESCE(MAX(pos),0)+1 FROM products").fetchone())[0]
    pid = f"n{int(time.time() * 1000)}"
    db.execute("INSERT INTO products VALUES(?,?,?,?,?,?,?)",
               (pid, d["name"], d["price"], d["descr"] or "Описание скоро появится", 99, key, pos))
    db.commit()
    load_products()
    await state.clear()
    await c.message.edit_text(
        f"✅ Добавлено: <b>{d['name']}</b> — {fmt(d['price'])} ₽\nРаздел: {escape(CATEGORIES[key][0])}\n"
        f"Остаток по умолчанию: 99 (меняется в панели).\n\n{adm_text()}", reply_markup=adm_kb())


# --- новый раздел ---
@dp.callback_query(F.data == "adm:newcat", ADM)
async def admin_newcat_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.newcat)
    await c.message.edit_text("🗂 <b>Новый раздел</b>\n\nВведи название раздела (можно с эмодзи, до 40 символов):",
                              reply_markup=back_kb("adm", "⬅️ Отмена"))


@dp.message(Adm.newcat, ADM)
async def admin_newcat_save(m: Message, state: FSMContext):
    title = (m.text or "").strip()
    if not title or len(title) > 40:
        return await m.answer("⚠️ Название — текст до 40 символов.")
    pos = db.execute("SELECT COALESCE(MAX(pos),0)+1 FROM categories").fetchone()[0]
    key = f"s{int(time.time() * 1000)}"
    db.execute("INSERT INTO categories VALUES(?,?,?)", (key, title, pos))
    db.commit()
    load_products()
    await state.clear()
    await m.answer(f"✅ Раздел <b>{escape(title)}</b> создан. Теперь его можно выбрать при добавлении товара.\n\n"
                   f"{adm_text()}", reply_markup=adm_kb())


# --- цвета кнопок ---
COLORS_TEXT = f"🎨 <b>Цвета кнопок</b>\n{LINE}\nВыбери кнопку, у которой хочешь поменять цвет:"


def colors_kb():
    kb = InlineKeyboardBuilder()
    for key, label in BTN_LABELS.items():
        kb.button(text=f"{label} — {STYLE_NAMES[st(key) or 'def']}", callback_data=f"clr:{key}")
    kb.button(text="⬅️ Назад", callback_data="adm")
    kb.adjust(1)
    return kb.as_markup()


@dp.callback_query(F.data == "adm:colors", ADM)
async def admin_colors(c: CallbackQuery):
    await c.message.edit_text(COLORS_TEXT, reply_markup=colors_kb())


@dp.callback_query(F.data.startswith("clr:"), ADM)
async def admin_color_pick(c: CallbackQuery):
    key = c.data[4:]
    if key not in BTN_LABELS:
        return await c.answer("Кнопка не найдена", show_alert=True)
    kb = InlineKeyboardBuilder()
    kb.button(text="🔵 Синий", callback_data=f"cs:{key}:primary", style="primary")
    kb.button(text="🟢 Зелёный", callback_data=f"cs:{key}:success", style="success")
    kb.button(text="🔴 Красный", callback_data=f"cs:{key}:danger", style="danger")
    kb.button(text="⚪️ Обычный", callback_data=f"cs:{key}:def")
    kb.button(text="⬅️ Назад", callback_data="adm:colors")
    kb.adjust(2, 2, 1)
    await c.message.edit_text(
        f"🎨 <b>{escape(BTN_LABELS[key])}</b>\nСейчас: <b>{STYLE_NAMES[st(key) or 'def']}</b>\n\nВыбери новый цвет:",
        reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("cs:"), ADM)
async def admin_color_set(c: CallbackQuery):
    _, key, val = c.data.split(":")
    if key not in BTN_LABELS or val not in STYLE_NAMES:
        return await c.answer("Ошибка", show_alert=True)
    db.execute("INSERT OR REPLACE INTO btn_styles VALUES(?,?)", (key, "" if val == "def" else val))
    db.commit()
    load_styles()
    await c.answer("Цвет сохранён ✅")
    await c.message.edit_text(COLORS_TEXT, reply_markup=colors_kb())


# --- объявление всем пользователям ---
@dp.callback_query(F.data == "adm:bcast", ADM)
async def admin_bcast_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.bcast)
    n = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    await c.message.edit_text(
        f"📢 <b>Объявление</b>\n{LINE}\nПолучат: <b>{n}</b> чел.\n\n"
        f"Отправь текст объявления одним сообщением (форматирование сохранится):",
        reply_markup=back_kb("adm", "⬅️ Отмена"))


@dp.message(Adm.bcast, ADM)
async def admin_bcast_preview(m: Message, state: FSMContext):
    if not m.text or len(m.text) > 3500:
        return await m.answer("⚠️ Нужен текст до 3500 символов.")
    await state.update_data(text=m.html_text)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Отправить всем", callback_data="bc:send", style="success")
    kb.button(text="❌ Отмена", callback_data="adm", style="danger")
    kb.adjust(1)
    await m.answer(f"👀 <b>Предпросмотр</b>\n{LINE}\n{m.html_text}\n{LINE}\nОтправить всем пользователям?",
                   reply_markup=kb.as_markup())


@dp.callback_query(F.data == "bc:send", Adm.bcast, ADM)
async def admin_bcast_send(c: CallbackQuery, state: FSMContext):
    text = (await state.get_data()).get("text")
    if not text:
        return await c.answer("Нет текста объявления", show_alert=True)
    await state.clear()
    ids = [r[0] for r in db.execute("SELECT id FROM users")]
    await c.message.edit_text(f"⏳ Отправляю {len(ids)} чел., подожди...")
    ok = fail = 0
    for uid in ids:
        try:
            await c.bot.send_message(uid, text)
            ok += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await c.bot.send_message(uid, text)
                ok += 1
            except Exception:
                fail += 1
        except Exception:
            fail += 1   # пользователь заблокировал бота и т.п.
        await asyncio.sleep(0.05)   # лимит Telegram ~30 сообщений/сек
    await c.message.edit_text(
        f"✅ <b>Рассылка завершена</b>\n{LINE}\nДоставлено: <b>{ok}</b>\n"
        f"Не доставлено: <b>{fail}</b> (заблокировали бота)\n\n{adm_text()}", reply_markup=adm_kb())


# --- статистика ---
def stats_text() -> str:
    total = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    buyers = db.execute("SELECT COUNT(DISTINCT user_id) FROM orders").fetchone()[0]
    orders, money = db.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders").fetchone()
    topped = db.execute("SELECT COALESCE(SUM(amount),0) FROM topups WHERE status='done'").fetchone()[0]
    try:
        today = db.execute("SELECT COUNT(*) FROM users WHERE date(joined)=date('now')").fetchone()[0]
        week = db.execute("SELECT COUNT(*) FROM users WHERE joined>=datetime('now','-7 days')").fetchone()[0]
    except sqlite3.OperationalError:
        today = week = 0
    pct = buyers / total * 100 if total else 0
    avg = money / orders if orders else 0
    top = db.execute("SELECT product, COUNT(*) c FROM orders GROUP BY product ORDER BY c DESC LIMIT 3").fetchall()
    top_txt = "\n".join(f"{i}. {pname(p)} — {n} шт" for i, (p, n) in enumerate(top, 1)) or "Пока нет продаж"
    return (f"📊 <b>Статистика</b>\n{LINE}\n"
            f"👥 Всего в боте: <b>{total}</b>\n"
            f"🆕 Новых сегодня: <b>{today}</b> • за 7 дней: <b>{week}</b>\n{LINE}\n"
            f"🛒 Покупали: <b>{buyers}</b> чел. (<b>{pct:.1f}%</b>)\n"
            f"🛍 Заказов: <b>{orders}</b>\n"
            f"💰 Продано на: <b>{fmt(money)} ₽</b>\n"
            f"🧾 Средний чек: <b>{fmt(avg)} ₽</b>\n"
            f"➕ Пополнено: <b>{fmt(topped)} ₽</b>\n{LINE}\n"
            f"🔥 <b>Топ товаров:</b>\n{top_txt}")


@dp.callback_query(F.data == "adm:stats", ADM)
async def admin_stats(c: CallbackQuery):
    await c.message.edit_text(stats_text(), reply_markup=back_kb("adm", "⬅️ Назад"))


async def on_error(event):
    """Не даём боту падать: 'message is not modified' и прочие ошибки просто логируем."""
    if "message is not modified" not in str(event.exception):
        logging.exception("Ошибка в обработчике: %s", event.exception)
    cq = event.update.callback_query
    if cq:
        try:
            await cq.answer()
        except Exception:
            pass
    return True


try:
    dp.errors.register(on_error)
except Exception:
    pass


# ───────── Мануалы и отзывы ─────────
MANUALS = [
    ("📗 Мануал Сим карта", "https://telegra.ph/Manual-Sim-karta-10-07"),
    ("🔄 Политика возврата", "https://telegra.ph/Politika-vozrata-10-07-2"),
]

db.execute("""CREATE TABLE IF NOT EXISTS reviews(
    user_id INTEGER PRIMARY KEY, name TEXT, rating INTEGER, text TEXT,
    created TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
db.commit()


class Review(StatesGroup):
    text = State()


@dp.callback_query(F.data == "man")
async def manuals(c: CallbackQuery):
    kb = InlineKeyboardBuilder()
    for title, url in MANUALS:
        kb.button(text=title, url=url)
    kb.button(text="⬅️ В меню", callback_data="menu")
    kb.adjust(1)
    await c.message.edit_text(f"📚 <b>Мануалы</b>\n{LINE}\nВыбери материал:", reply_markup=kb.as_markup())


@dp.callback_query(F.data == "rev")
async def reviews(c: CallbackQuery, state: FSMContext):
    await state.clear()
    n, avg = db.execute("SELECT COUNT(*), COALESCE(AVG(rating),0) FROM reviews").fetchone()
    rows = db.execute("SELECT name, rating, text FROM reviews ORDER BY rowid DESC LIMIT 5").fetchall()
    body = "\n\n".join(f"{'⭐' * r} <b>{escape(nm)}</b>\n{escape(t)}" for nm, r, t in rows) \
        or "Пока нет отзывов. Будь первым!"
    head = f"⭐ Рейтинг: <b>{avg:.1f}</b> • отзывов: <b>{n}</b>\n{LINE}\n" if n else ""
    kb = InlineKeyboardBuilder()
    kb.button(text="✍️ Оставить отзыв", callback_data="rev:new", style="success")
    kb.button(text="⬅️ В меню", callback_data="menu")
    kb.adjust(1)
    await c.message.edit_text(f"⭐ <b>Отзывы</b>\n{LINE}\n{head}{body}", reply_markup=kb.as_markup())


@dp.callback_query(F.data == "rev:new")
async def review_new(c: CallbackQuery):
    kb = InlineKeyboardBuilder()
    for i in range(1, 6):
        kb.button(text="⭐" * i, callback_data=f"rr:{i}")
    kb.button(text="⬅️ Назад", callback_data="rev")
    kb.adjust(3, 2, 1)
    await c.message.edit_text(f"✍️ <b>Твой отзыв о боте</b>\n{LINE}\nПоставь оценку:", reply_markup=kb.as_markup())


@dp.callback_query(F.data.regexp(r"^rr:[1-5]$"))
async def review_rating(c: CallbackQuery, state: FSMContext):
    await state.set_state(Review.text)
    await state.update_data(rating=int(c.data[3:]))
    await c.message.edit_text(f"{'⭐' * int(c.data[3:])}\n\nТеперь напиши отзыв одним сообщением (до 500 символов):",
                              reply_markup=back_kb("rev", "⬅️ Отмена"))


@dp.message(Review.text)
async def review_save(m: Message, state: FSMContext):
    txt = (m.text or "").strip()
    if not txt or len(txt) > 500:
        return await m.answer("⚠️ Напиши отзыв текстом до 500 символов.")
    rating = (await state.get_data()).get("rating", 5)
    u = m.from_user
    name = (u.first_name or u.username or "Аноним")[:40]
    # один отзыв на пользователя: новый заменяет старый
    db.execute("INSERT OR REPLACE INTO reviews(user_id, name, rating, text) VALUES(?,?,?,?)",
               (u.id, name, rating, txt))
    db.commit()
    await state.clear()
    await m.answer("🙏 <b>Спасибо за отзыв!</b>", reply_markup=back_kb("rev", "⭐ К отзывам"))
    if ADMIN_ID:
        await m.bot.send_message(ADMIN_ID, f"⭐ <b>Новый отзыв</b> {'⭐' * rating}\n"
                                 f"От: {escape(name)} (@{u.username or '-'}, id {u.id})\n\n{escape(txt)}")


# --- админ: отзывы (создать от любого имени / удалить) ---
@dp.callback_query(F.data == "adm:radd", ADM)
async def admin_review_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.rname)
    await c.message.edit_text("✍️ <b>Новый отзыв</b>\n\n1/3. Введи имя автора (любое, до 40 символов):",
                              reply_markup=back_kb("adm", "⬅️ Отмена"))


@dp.message(Adm.rname, ADM)
async def admin_review_name(m: Message, state: FSMContext):
    name = (m.text or "").strip()
    if not name or len(name) > 40:
        return await m.answer("⚠️ Имя — текст до 40 символов.")
    await state.update_data(name=name)
    await state.set_state(Adm.rrate)
    kb = InlineKeyboardBuilder()
    for i in range(1, 6):
        kb.button(text="⭐" * i, callback_data=f"ar:{i}")
    kb.button(text="⬅️ Отмена", callback_data="adm")
    kb.adjust(3, 2, 1)
    await m.answer("2/3. Выбери оценку:", reply_markup=kb.as_markup())


@dp.callback_query(F.data.regexp(r"^ar:[1-5]$"), Adm.rrate, ADM)
async def admin_review_rate(c: CallbackQuery, state: FSMContext):
    await state.update_data(rating=int(c.data[3:]))
    await state.set_state(Adm.rtext)
    await c.message.edit_text("3/3. Напиши текст отзыва (до 500 символов):",
                              reply_markup=back_kb("adm", "⬅️ Отмена"))


@dp.message(Adm.rtext, ADM)
async def admin_review_text(m: Message, state: FSMContext):
    txt = (m.text or "").strip()
    if not txt or len(txt) > 500:
        return await m.answer("⚠️ Текст — до 500 символов.")
    d = await state.get_data()
    # отрицательный id = отзыв, добавленный админом (не совпадает с настоящими пользователями)
    db.execute("INSERT INTO reviews(user_id, name, rating, text) VALUES(?,?,?,?)",
               (-int(time.time() * 1000), d["name"], d["rating"], txt))
    db.commit()
    await state.clear()
    await m.answer(f"✅ Отзыв от <b>{escape(d['name'])}</b> {'⭐' * d['rating']} добавлен.\n\n{adm_text()}",
                   reply_markup=adm_kb())


@dp.callback_query(F.data == "adm:rdel", ADM)
async def admin_review_list(c: CallbackQuery):
    rows = db.execute("SELECT user_id, name, rating, text FROM reviews ORDER BY rowid DESC LIMIT 15").fetchall()
    if not rows:
        return await c.answer("Отзывов пока нет", show_alert=True)
    kb = InlineKeyboardBuilder()
    for uid, name, r, t in rows:
        kb.button(text=f"{r}⭐ {name}: {t[:25]}", callback_data=f"rd:{uid}")
    kb.button(text="⬅️ Назад", callback_data="adm")
    kb.adjust(1)
    await c.message.edit_text("🗑 <b>Выбери отзыв для удаления</b> (последние 15):", reply_markup=kb.as_markup())


@dp.callback_query(F.data.regexp(r"^rd:-?\d+$"), ADM)
async def admin_review_ask(c: CallbackQuery):
    uid = int(c.data[3:])
    row = db.execute("SELECT name, rating, text FROM reviews WHERE user_id=?", (uid,)).fetchone()
    if not row:
        return await c.answer("Отзыв не найден", show_alert=True)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data=f"rdc:{uid}", style="danger")
    kb.button(text="⬅️ Отмена", callback_data="adm:rdel")
    kb.adjust(1)
    await c.message.edit_text(f"🗑 Удалить отзыв?\n{LINE}\n{'⭐' * row[1]} <b>{escape(row[0])}</b>\n{escape(row[2])}",
                              reply_markup=kb.as_markup())


@dp.callback_query(F.data.regexp(r"^rdc:-?\d+$"), ADM)
async def admin_review_del(c: CallbackQuery):
    db.execute("DELETE FROM reviews WHERE user_id=?", (int(c.data[4:]),))
    db.commit()
    await c.message.edit_text(f"✅ Отзыв удалён.\n\n{adm_text()}", reply_markup=adm_kb())


# ───────── Блокировка пользователей ─────────
db.execute("CREATE TABLE IF NOT EXISTS banned(user_id INTEGER PRIMARY KEY)")
db.commit()
BANNED = {r[0] for r in db.execute("SELECT user_id FROM banned")}


async def ban_msg(handler, event, data):
    u = event.from_user
    if u and u.id in BANNED and u.id != ADMIN_ID:
        return await event.answer("🚫 Доступ ограничен.")
    return await handler(event, data)


async def ban_cb(handler, event, data):
    u = event.from_user
    if u and u.id in BANNED and u.id != ADMIN_ID:
        return await event.answer("🚫 Доступ ограничен.", show_alert=True)
    return await handler(event, data)


dp.message.outer_middleware(ban_msg)
dp.callback_query.outer_middleware(ban_cb)


# --- админ: карточка пользователя, баланс, блокировка ---
def user_card(uid: int):
    row = db.execute("SELECT username, balance, joined FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        return None
    uname, bal, joined = row
    n, spent = db.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE user_id=?", (uid,)).fetchone()
    banned = uid in BANNED
    text = (f"👤 <b>Пользователь</b>\n{LINE}\n🆔 <code>{uid}</code>\n"
            f"📛 {'@' + escape(uname) if uname and uname != '-' else 'без username'}\n"
            f"💰 Баланс: <b>{fmt(bal)} ₽</b>\n🛍 Покупок: <b>{n}</b> на <b>{fmt(spent)} ₽</b>\n"
            f"📅 В боте с: {joined or '-'}\n🚦 Статус: {'🚫 заблокирован' if banned else '✅ активен'}")
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Выдать баланс", callback_data=f"ub:+:{uid}", style="success")
    kb.button(text="➖ Списать баланс", callback_data=f"ub:-:{uid}", style="danger")
    if uid != ADMIN_ID:
        kb.button(text="✅ Разблокировать" if banned else "🚫 Заблокировать", callback_data=f"ubn:{uid}")
    kb.button(text="⬅️ Назад", callback_data="adm")
    kb.adjust(2, 1, 1)
    return text, kb.as_markup()


@dp.callback_query(F.data == "adm:user", ADM)
async def admin_user_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.usearch)
    await c.message.edit_text("👤 Введи ID или @username пользователя:", reply_markup=back_kb("adm", "⬅️ Отмена"))


@dp.message(Adm.usearch, ADM)
async def admin_user_find(m: Message, state: FSMContext):
    q = (m.text or "").strip()
    if q.isdigit():
        row = db.execute("SELECT id FROM users WHERE id=?", (int(q),)).fetchone()
    else:
        row = db.execute("SELECT id FROM users WHERE LOWER(username)=?", (q.lstrip("@").lower(),)).fetchone()
    if not row:
        return await m.answer("⚠️ Не найден. Пользователь должен хотя бы раз запустить бота. Попробуй ещё раз:")
    await state.clear()
    text, kb = user_card(row[0])
    await m.answer(text, reply_markup=kb)


@dp.callback_query(F.data.regexp(r"^ub:[+-]:\d+$"), ADM)
async def admin_user_bal(c: CallbackQuery, state: FSMContext):
    _, sign, uid = c.data.split(":")
    await state.set_state(Adm.ubal)
    await state.update_data(uid=int(uid), sign=sign)
    await c.message.edit_text(f"💰 Введи сумму для {'зачисления' if sign == '+' else 'списания'} (₽):",
                              reply_markup=back_kb("adm", "⬅️ Отмена"))


@dp.message(Adm.ubal, ADM)
async def admin_user_bal_do(m: Message, state: FSMContext):
    try:
        amt = float((m.text or "").replace(" ", "").replace(",", "."))
    except ValueError:
        amt = 0
    if not 0 < amt <= 10_000_000:
        return await m.answer("⚠️ Введи положительное число.")
    d = await state.get_data()
    row = db.execute("SELECT balance FROM users WHERE id=?", (d["uid"],)).fetchone()
    if not row:
        await state.clear()
        return await m.answer("Пользователь не найден.", reply_markup=adm_kb())
    if d["sign"] == "-" and amt > row[0]:
        return await m.answer(f"⚠️ У пользователя только {fmt(row[0])} ₽.")
    db.execute("UPDATE users SET balance=balance+? WHERE id=?", (amt if d["sign"] == "+" else -amt, d["uid"]))
    db.commit()
    await state.clear()
    text, kb = user_card(d["uid"])
    await m.answer(f"✅ Готово\n\n{text}", reply_markup=kb)


@dp.callback_query(F.data.regexp(r"^ubn:\d+$"), ADM)
async def admin_user_ban(c: CallbackQuery):
    uid = int(c.data[4:])
    if uid == ADMIN_ID:
        return await c.answer("Себя блокировать нельзя", show_alert=True)
    if uid in BANNED:
        BANNED.discard(uid)
        db.execute("DELETE FROM banned WHERE user_id=?", (uid,))
    else:
        BANNED.add(uid)
        db.execute("INSERT OR IGNORE INTO banned VALUES(?)", (uid,))
    db.commit()
    card = user_card(uid)
    if not card:
        return await c.answer("Пользователь не найден", show_alert=True)
    await c.message.edit_text(card[0], reply_markup=card[1])


# --- админ: ожидающие заявки на пополнение ---
@dp.callback_query(F.data == "adm:pend", ADM)
async def admin_pending(c: CallbackQuery):
    rows = db.execute("SELECT id, user_id, amount FROM topups WHERE status='pending' ORDER BY id LIMIT 10").fetchall()
    if not rows:
        return await c.answer("Ожидающих заявок нет ✅", show_alert=True)
    await c.answer(f"Заявок: {len(rows)}")
    for tid, uid, amount in rows:
        r = db.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Зачислить", callback_data=f"tok:{tid}", style="success")
        kb.button(text="❌ Отклонить", callback_data=f"tno:{tid}", style="danger")
        await c.message.answer(f"💳 <b>Заявка на пополнение #{tid}</b>\n{LINE}\n"
                               f"Сумма: <b>{fmt(amount)} ₽</b>\nКлиент: @{escape((r[0] if r else '-') or '-')} (id {uid})",
                               reply_markup=kb.as_markup())


# --- админ: резервная копия базы ---
@dp.callback_query(F.data == "adm:backup", ADM)
async def admin_backup(c: CallbackQuery):
    await c.answer("Готовлю копию…")
    tmp = os.path.join(tempfile.gettempdir(), "shop_backup.db")
    dst = sqlite3.connect(tmp)
    try:
        db.commit()
        db.backup(dst)   # согласованная копия, даже если бот сейчас пишет в базу
    finally:
        dst.close()
    try:
        await c.message.answer_document(
            FSInputFile(tmp, filename=f"shop_backup_{time.strftime('%Y-%m-%d_%H-%M')}.db"),
            caption="💾 Резервная копия базы (пользователи, балансы, заказы). Храни в надёжном месте.")
    finally:
        os.remove(tmp)


# ───────── Мини-приложение (Telegram WebApp) ─────────
WEB_DIR = os.path.dirname(os.path.abspath(__file__))


def check_init(init_data: str):
    """Проверяет подпись Telegram (initData) и возвращает данные пользователя или None."""
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
        got = data.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        good = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, got) or time.time() - int(data["auth_date"]) > 86400:
            return None
        return json.loads(data["user"])
    except Exception:
        return None


async def _body(req) -> dict:
    """Тело запроса как dict (при мусоре вместо JSON — пустой dict, а не ошибка 500)."""
    try:
        data = await req.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


async def index(req):
    return web.FileResponse(os.path.join(WEB_DIR, "webapp.html"),
                            headers={"Cache-Control": "no-store"})


def _err(msg, status=400):
    return web.json_response({"error": msg}, status=status)


async def api_data(req):
    u = check_init((await _body(req)).get("initData", ""))
    if not u:
        return _err("Нет доступа", 401)
    if u["id"] in BANNED:
        return _err("Доступ ограничен", 403)
    (bal,) = get_user(SimpleNamespace(id=u["id"], username=u.get("username")))
    n, spent = db.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE user_id=?", (u["id"],)).fetchone()
    orders = [{"id": i, "name": unescape(pname(p)), "price": pr} for i, p, pr in db.execute(
        "SELECT id, product, price FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 20", (u["id"],))]
    cats = [{"key": k, "title": t, "products": [
        {"id": pid, "name": unescape(PRODUCTS[pid][0]), "price": PRODUCTS[pid][1],
         "descr": unescape(PRODUCTS[pid][2]), "stock": PRODUCTS[pid][3]} for pid in ids]}
        for k, (t, ids) in CATEGORIES.items()]
    return web.json_response({"balance": bal, "count": n, "spent": spent, "orders": orders, "categories": cats})


async def api_buy(req):
    body = await _body(req)
    u = check_init(body.get("initData", ""))
    if not u:
        return _err("Нет доступа", 401)
    if u["id"] in BANNED:
        return _err("Доступ ограничен", 403)
    pid = body.get("pid")
    if not isinstance(pid, str) or pid not in PRODUCTS:
        return _err("Этот товар больше недоступен")
    name, price, _, stock = PRODUCTS[pid]
    (bal,) = get_user(SimpleNamespace(id=u["id"], username=u.get("username")))
    if stock <= 0:
        return _err("Товара нет в наличии")
    if bal < price:
        return _err("Недостаточно средств на балансе")
    db.execute("UPDATE users SET balance=balance-? WHERE id=?", (price, u["id"]))
    cur = db.execute("INSERT INTO orders(user_id, product, price) VALUES(?,?,?)", (u["id"], pid, price))
    PRODUCTS[pid][3] -= 1
    db.execute("UPDATE products SET stock=? WHERE id=?", (PRODUCTS[pid][3], pid))
    db.commit()
    try:
        await req.app["bot"].send_message(
            ADMIN_ID, f"🆕 <b>Заказ #{cur.lastrowid}</b> (мини-приложение)\nТовар: {name} — {fmt(price)} ₽\n"
                      f"Клиент: @{u.get('username') or '-'} (id {u['id']})")
    except Exception:
        logging.exception("Не удалось уведомить админа")
    return web.json_response({"ok": True, "order": cur.lastrowid, "balance": bal - price})


async def start_web(bot):
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", index)
    app.router.add_post("/api/data", api_data)
    app.router.add_post("/api/buy", api_buy)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080"))).start()
    logging.info("Мини-приложение: %s (порт %s)", WEBAPP_URL, os.getenv("PORT", "8080"))


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    if WEBAPP_URL:
        await start_web(bot)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
