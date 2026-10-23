"""
Бот «Призыва Нет» — самостоятельная версия.
Зависимости: aiogram 3.x, apscheduler 3.x.
Токен и ID администратора задаются переменными окружения; dotenv не требуется.
"""
import asyncio
import logging
import os
import sqlite3
from datetime import datetime
from html import escape
from pathlib import Path

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DB_PATH", str(APP_DIR / "priziv_bot.db")))
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "8141670312").strip()
WORK_START = int(os.getenv("WORK_START", "9"))
WORK_END = int(os.getenv("WORK_END", "18"))
TIMEZONE_NOTE = os.getenv("TIMEZONE_NOTE", "время сервера")

if not BOT_TOKEN or BOT_TOKEN == "ВАШ_НОВЫЙ_ТОКЕН_ЗДЕСЬ":
    raise RuntimeError(
        "Не задан BOT_TOKEN. Добавьте переменную BOT_TOKEN в настройках хостинга "
        "и перезапустите приложение."
    )
try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError as exc:
    raise RuntimeError("ADMIN_ID должен быть числовым Telegram ID администратора.") from exc

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("priziv_net")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
scheduler = AsyncIOScheduler()


# ---------- DATABASE ----------
def db(query, params=(), fetch=False, one=False):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH, timeout=20) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(query, params)
        if fetch:
            rows = cur.fetchall()
            return rows[0] if one and rows else (None if one else rows)
        return cur.lastrowid


def init_db():
    with sqlite3.connect(DB_PATH, timeout=20) as conn:
        conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT NOT NULL,
            joined_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            subject TEXT NOT NULL DEFAULT 'Обращение',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER,
            user_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            direction TEXT NOT NULL,
            timestamp TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            send_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'scheduled'
        );
        CREATE TABLE IF NOT EXISTS templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            text TEXT NOT NULL
        );
        """)
        count = conn.execute("SELECT COUNT(*) FROM templates").fetchone()[0]
        if count == 0:
            conn.execute(
                "INSERT INTO templates(title,text) VALUES(?,?)",
                ("Приветствие", "Здравствуйте! Вы обратились в компанию «Призыва Нет»."),
            )


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def is_working_time():
    now = datetime.now()
    return now.weekday() < 5 and WORK_START <= now.hour < WORK_END


def register_user(user: types.User):
    existing = db("SELECT user_id FROM users WHERE user_id=?", (user.id,), fetch=True, one=True)
    if existing:
        db(
            "UPDATE users SET username=?, full_name=? WHERE user_id=?",
            (user.username, user.full_name, user.id),
        )
    else:
        db(
            "INSERT INTO users(user_id,username,full_name,joined_at) VALUES(?,?,?,?)",
            (user.id, user.username, user.full_name, now_str()),
        )


def user_menu():
    b = InlineKeyboardBuilder()
    b.button(text="👤 Мой профиль", callback_data="u:profile")
    b.button(text="🔴 Создать обращение", callback_data="u:new")
    b.button(text="📨 Мои обращения", callback_data="u:tickets")
    b.button(text="ℹ️ Информация", callback_data="u:info")
    b.adjust(1)
    return b.as_markup()


def admin_menu():
    b = InlineKeyboardBuilder()
    b.button(text="📩 Обращения", callback_data="a:tickets")
    b.button(text="👥 Пользователи", callback_data="a:users")
    b.button(text="⏰ Напоминания", callback_data="a:reminders")
    b.button(text="📝 Шаблоны", callback_data="a:templates")
    b.button(text="📊 Статистика", callback_data="a:stats")
    b.adjust(2, 2, 1)
    return b.as_markup()


def back_button(target="u:home", label="🔙 Назад"):
    b = InlineKeyboardBuilder()
    b.button(text=label, callback_data=target)
    return b.as_markup()


def ticket_actions(ticket_id, is_admin=False):
    b = InlineKeyboardBuilder()
    if is_admin:
        b.button(text="✉️ Ответить", callback_data=f"a:reply:{ticket_id}")
        b.button(text="👤 Карточка клиента", callback_data=f"a:client:{ticket_id}")
        b.button(text="🔒 Закрыть", callback_data=f"a:close:{ticket_id}")
        b.button(text="🔙 К обращениям", callback_data="a:tickets")
        b.adjust(1)
    else:
        b.button(text="✍️ Дополнить обращение", callback_data=f"u:add:{ticket_id}")
        b.button(text="🔙 К моим обращениям", callback_data="u:tickets")
        b.adjust(1)
    return b.as_markup()


class Flow(StatesGroup):
    user_message = State()
    admin_reply = State()
    admin_broadcast_user_id = State()
    admin_broadcast_text = State()
    reminder_user_id = State()
    reminder_text = State()
    reminder_time = State()
    template_title = State()
    template_text = State()


async def safe_edit(callback: types.CallbackQuery, text: str, reply_markup=None):
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await callback.message.answer(text, reply_markup=reply_markup)


async def show_ticket_to_admin(ticket_id: int):
    ticket = db("SELECT * FROM tickets WHERE id=?", (ticket_id,), fetch=True, one=True)
    if not ticket:
        return
    user = db("SELECT * FROM users WHERE user_id=?", (ticket["user_id"],), fetch=True, one=True)
    latest = db(
        "SELECT text FROM messages WHERE ticket_id=? ORDER BY id DESC LIMIT 1",
        (ticket_id,), fetch=True, one=True
    )
    username = f"@{escape(user['username'])}" if user and user["username"] else "не указан"
    body = escape(latest["text"]) if latest else "Сообщение отсутствует"
    await bot.send_message(
        ADMIN_ID,
        f"📩 <b>Обращение #{ticket_id}</b>\n"
        f"Статус: <b>{escape(ticket['status'])}</b>\n"
        f"👤 {escape(user['full_name'] if user else 'Неизвестный пользователь')}\n"
        f"🆔 <code>{ticket['user_id']}</code>\n"
        f"🔗 {username}\n\n<b>Последнее сообщение:</b>\n{body}",
        reply_markup=ticket_actions(ticket_id, True),
    )


async def create_ticket(user: types.User, text: str):
    register_user(user)
    stamp = now_str()
    cur_id = db(
        "INSERT INTO tickets(user_id,status,subject,created_at,updated_at) VALUES(?,?,?,?,?)",
        (user.id, "open", "Обращение", stamp, stamp),
    )
    ticket_id = cur_id
    db(
        "INSERT INTO messages(ticket_id,user_id,text,direction,timestamp) VALUES(?,?,?,?,?)",
        (ticket_id, user.id, text, "in", stamp),
    )
    if is_working_time():
        reply = "✅ Обращение принято и передано специалисту. Ответ поступит сюда, в этот чат."
    else:
        reply = (
            f"✅ Обращение принято. Сейчас нерабочее время. "
            f"Специалист ответит в рабочие часы: Пн–Пт, {WORK_START:02d}:00–{WORK_END:02d}:00 "
            f"({escape(TIMEZONE_NOTE)})."
        )
    await bot.send_message(user.id, reply, reply_markup=user_menu())
    await show_ticket_to_admin(ticket_id)


# ---------- USER ----------
@dp.message(CommandStart())
async def start(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user.id == ADMIN_ID:
        await message.answer(
            "👑 <b>Панель администратора</b>\n\nУправление обращениями, клиентами и напоминаниями.",
            reply_markup=admin_menu(),
        )
        return
    register_user(message.from_user)
    await message.answer(
        "Здравствуйте!\n\n"
        "Вы обратились в компанию «Призыва Нет».\n"
        "Здесь можно направить обращение, посмотреть его статус и получить ответ специалиста.\n\n"
        "Выберите раздел:",
        reply_markup=user_menu(),
    )


@dp.message(Command("admin"))
async def admin_command(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer("👑 <b>Панель администратора</b>", reply_markup=admin_menu())


@dp.callback_query(F.data == "u:home")
async def user_home(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(c, "Главное меню пользователя:", user_menu())
    await c.answer()


@dp.callback_query(F.data.startswith("u:"))
async def user_callbacks(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id == ADMIN_ID:
        await c.answer("Откройте раздел администратора командой /admin.", show_alert=True)
        return
    register_user(c.from_user)
    data = c.data

    if data == "u:profile":
        u = db("SELECT * FROM users WHERE user_id=?", (c.from_user.id,), fetch=True, one=True)
        count = db("SELECT COUNT(*) AS n FROM tickets WHERE user_id=?", (c.from_user.id,), fetch=True, one=True)["n"]
        text = (
            "👤 <b>Мой профиль</b>\n\n"
            f"Имя: {escape(u['full_name'])}\n"
            f"Username: {('@' + escape(u['username'])) if u['username'] else 'не указан'}\n"
            f"ID: <code>{c.from_user.id}</code>\n"
            f"Дата регистрации: {escape(u['joined_at'])}\n"
            f"Обращений: {count}"
        )
        await safe_edit(c, text, back_button())
    elif data == "u:new":
        await state.set_state(Flow.user_message)
        await safe_edit(c, "🔴 <b>Новое обращение</b>\n\nНапишите одним сообщением, чем мы можем помочь. Можно отправить текст или медиафайл.", back_button("u:home", "❌ Отмена"))
    elif data == "u:tickets":
        rows = db("SELECT * FROM tickets WHERE user_id=? ORDER BY updated_at DESC LIMIT 20", (c.from_user.id,), fetch=True)
        b = InlineKeyboardBuilder()
        lines = ["📨 <b>Мои обращения</b>\n"]
        if not rows:
            lines.append("У вас пока нет обращений.")
        for t in rows:
            lines.append(f"#{t['id']} · {escape(t['status'])} · {escape(t['updated_at'])}")
            b.button(text=f"Открыть обращение #{t['id']}", callback_data=f"u:view:{t['id']}")
        b.button(text="🔙 Назад", callback_data="u:home")
        b.adjust(1)
        await safe_edit(c, "\n".join(lines), b.as_markup())
    elif data.startswith("u:view:"):
        ticket_id = int(data.rsplit(":", 1)[1])
        t = db("SELECT * FROM tickets WHERE id=? AND user_id=?", (ticket_id, c.from_user.id), fetch=True, one=True)
        if not t:
            await c.answer("Обращение не найдено.", show_alert=True)
        else:
            msgs = db("SELECT * FROM messages WHERE ticket_id=? ORDER BY id DESC LIMIT 8", (ticket_id,), fetch=True)
            lines = [f"📨 <b>Обращение #{ticket_id}</b>\nСтатус: <b>{escape(t['status'])}</b>\n"]
            for m in reversed(msgs):
                who = "Вы" if m["direction"] == "in" else "Специалист"
                lines.append(f"<b>{who} · {escape(m['timestamp'])}</b>\n{escape(m['text'])}")
            await safe_edit(c, "\n\n".join(lines), ticket_actions(ticket_id))
    elif data.startswith("u:add:"):
        ticket_id = int(data.rsplit(":", 1)[1])
        t = db("SELECT * FROM tickets WHERE id=? AND user_id=?", (ticket_id, c.from_user.id), fetch=True, one=True)
        if not t or t["status"] == "closed":
            await c.answer("Это обращение закрыто или не найдено.", show_alert=True)
        else:
            await state.update_data(ticket_id=ticket_id)
            await state.set_state(Flow.user_message)
            await safe_edit(c, f"✍️ Напишите дополнение к обращению #{ticket_id}.", back_button(f"u:view:{ticket_id}", "❌ Отмена"))
    elif data == "u:info":
        await safe_edit(
            c,
            "ℹ️ <b>Информация</b>\n\n"
            "Вы общаетесь со специалистами компании через бота. Ваши сообщения видны уполномоченному администратору, "
            "а личные контакты администратора вам не раскрываются.\n\n"
            f"🕘 График: Пн–Пт, {WORK_START:02d}:00–{WORK_END:02d}:00 ({escape(TIMEZONE_NOTE)}).",
            back_button(),
        )
    await c.answer()


@dp.message(Flow.user_message)
async def user_message(message: types.Message, state: FSMContext):
    if message.from_user.id == ADMIN_ID:
        await message.answer("Для работы с обращениями используйте панель /admin.")
        return
    text = message.text or message.caption or "[Пользователь отправил медиафайл]"
    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    if ticket_id:
        t = db("SELECT * FROM tickets WHERE id=? AND user_id=?", (ticket_id, message.from_user.id), fetch=True, one=True)
        if not t or t["status"] == "closed":
            await state.clear()
            await message.answer("Обращение закрыто. Создайте новое обращение через меню.", reply_markup=user_menu())
            return
        stamp = now_str()
        db("INSERT INTO messages(ticket_id,user_id,text,direction,timestamp) VALUES(?,?,?,?,?)",
           (ticket_id, message.from_user.id, text, "in", stamp))
        db("UPDATE tickets SET updated_at=? WHERE id=?", (stamp, ticket_id))
        await message.answer("✅ Сообщение добавлено к обращению.", reply_markup=user_menu())
        await show_ticket_to_admin(ticket_id)
    else:
        await create_ticket(message.from_user, text)
    await state.clear()


# ---------- ADMIN ----------
@dp.callback_query(F.data.startswith("a:"))
async def admin_callbacks(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        await c.answer("Недостаточно прав.", show_alert=True)
        return
    data = c.data
    if data == "a:home":
        await state.clear()
        await safe_edit(c, "👑 <b>Панель администратора</b>", admin_menu())
    elif data == "a:tickets":
        rows = db("SELECT * FROM tickets WHERE status!='closed' ORDER BY updated_at DESC LIMIT 30", fetch=True)
        b = InlineKeyboardBuilder()
        lines = ["📩 <b>Активные обращения</b>\n"]
        if not rows:
            lines.append("Активных обращений нет.")
        for t in rows:
            u = db("SELECT full_name FROM users WHERE user_id=?", (t["user_id"],), fetch=True, one=True)
            name = u["full_name"] if u else str(t["user_id"])
            lines.append(f"#{t['id']} · {escape(name)} · {escape(t['status'])}")
            b.button(text=f"#{t['id']} · {name[:35]}", callback_data=f"a:view:{t['id']}")
        b.button(text="🔙 Назад", callback_data="a:home")
        b.adjust(1)
        await safe_edit(c, "\n".join(lines), b.as_markup())
    elif data.startswith("a:view:"):
        ticket_id = int(data.rsplit(":", 1)[1])
        t = db("SELECT * FROM tickets WHERE id=?", (ticket_id,), fetch=True, one=True)
        if not t:
            await c.answer("Обращение не найдено.", show_alert=True)
        else:
            u = db("SELECT * FROM users WHERE user_id=?", (t["user_id"],), fetch=True, one=True)
            msgs = db("SELECT * FROM messages WHERE ticket_id=? ORDER BY id DESC LIMIT 6", (ticket_id,), fetch=True)
            lines = [
                f"📩 <b>Обращение #{ticket_id}</b>",
                f"Статус: <b>{escape(t['status'])}</b>",
                f"Клиент: {escape(u['full_name'] if u else 'неизвестен')}",
                f"ID: <code>{t['user_id']}</code>",
                f"Username: {('@' + escape(u['username'])) if u and u['username'] else 'не указан'}",
                ""
            ]
            for m in reversed(msgs):
                who = "Клиент" if m["direction"] == "in" else "Ответ администратора"
                lines.append(f"<b>{who} · {escape(m['timestamp'])}</b>\n{escape(m['text'])}")
            await safe_edit(c, "\n\n".join(lines), ticket_actions(ticket_id, True))
    elif data.startswith("a:reply:"):
        ticket_id = int(data.rsplit(":", 1)[1])
        await state.update_data(ticket_id=ticket_id)
        await state.set_state(Flow.admin_reply)
        await safe_edit(c, f"✉️ Введите ответ для обращения #{ticket_id}. Он будет отправлен от имени бота.", back_button(f"a:view:{ticket_id}", "❌ Отмена"))
    elif data.startswith("a:close:"):
        ticket_id = int(data.rsplit(":", 1)[1])
        db("UPDATE tickets SET status='closed',updated_at=? WHERE id=?", (now_str(), ticket_id))
        await safe_edit(c, f"🔒 Обращение #{ticket_id} закрыто.", back_button("a:tickets", "🔙 К обращениям"))
    elif data == "a:users":
        rows = db("SELECT * FROM users ORDER BY joined_at DESC LIMIT 20", fetch=True)
        b = InlineKeyboardBuilder()
        lines = ["👥 <b>Последние пользователи</b>\n"]
        for u in rows:
            lines.append(f"{escape(u['full_name'])} · <code>{u['user_id']}</code>")
            b.button(text=f"{u['full_name'][:30]} · {u['user_id']}", callback_data=f"a:user:{u['user_id']}")
        b.button(text="🔙 Назад", callback_data="a:home")
        b.adjust(1)
        await safe_edit(c, "\n".join(lines), b.as_markup())
    elif data.startswith("a:user:"):
        uid = int(data.rsplit(":", 1)[1])
        u = db("SELECT * FROM users WHERE user_id=?", (uid,), fetch=True, one=True)
        count = db("SELECT COUNT(*) n FROM tickets WHERE user_id=?", (uid,), fetch=True, one=True)["n"]
        b = InlineKeyboardBuilder()
        b.button(text="✉️ Написать пользователю", callback_data=f"a:message:{uid}")
        b.button(text="📩 Обращения клиента", callback_data=f"a:user_tickets:{uid}")
        b.button(text="🔙 Назад", callback_data="a:users")
        b.adjust(1)
        await safe_edit(c, f"👤 <b>Карточка пользователя</b>\n\nИмя: {escape(u['full_name'])}\nUsername: {('@'+escape(u['username'])) if u['username'] else 'не указан'}\nID: <code>{uid}</code>\nРегистрация: {escape(u['joined_at'])}\nОбращений: {count}", b.as_markup())
    elif data.startswith("a:user_tickets:"):
        uid = int(data.rsplit(":", 1)[1])
        rows = db("SELECT * FROM tickets WHERE user_id=? ORDER BY updated_at DESC LIMIT 20", (uid,), fetch=True)
        b = InlineKeyboardBuilder()
        lines = [f"📩 <b>Обращения пользователя {uid}</b>\n"]
        if not rows:
            lines.append("Обращений пока нет.")
        for t in rows:
            lines.append(f"#{t['id']} · {escape(t['status'])} · {escape(t['updated_at'])}")
            b.button(text=f"Открыть #{t['id']}", callback_data=f"a:view:{t['id']}")
        b.button(text="🔙 Назад", callback_data=f"a:user:{uid}")
        b.adjust(1)
        await safe_edit(c, "\n".join(lines), b.as_markup())
    elif data.startswith("a:message:"):
        uid = int(data.rsplit(":", 1)[1])
        await state.update_data(target_user=uid)
        await state.set_state(Flow.admin_broadcast_text)
        await safe_edit(c, f"✉️ Напишите сообщение пользователю <code>{uid}</code>.", back_button(f"a:user:{uid}", "❌ Отмена"))
    elif data == "a:reminders":
        rows = db("SELECT * FROM reminders WHERE status='scheduled' ORDER BY send_at LIMIT 20", fetch=True)
        lines = ["⏰ <b>Активные напоминания</b>\n"]
        if not rows:
            lines.append("Активных напоминаний нет.")
        for r in rows:
            lines.append(f"#{r['id']} · {r['user_id']} · {escape(r['send_at'])}\n{escape(r['text'])}")
        b = InlineKeyboardBuilder()
        b.button(text="➕ Создать напоминание", callback_data="a:reminder:new")
        b.button(text="🔙 Назад", callback_data="a:home")
        b.adjust(1)
        await safe_edit(c, "\n\n".join(lines), b.as_markup())
    elif data == "a:reminder:new":
        await state.set_state(Flow.reminder_user_id)
        await safe_edit(c, "Введите Telegram ID пользователя для напоминания.", back_button("a:reminders", "❌ Отмена"))
    elif data == "a:templates":
        rows = db("SELECT * FROM templates ORDER BY id", fetch=True)
        lines = ["📝 <b>Шаблоны</b>\n"]
        for r in rows:
            lines.append(f"#{r['id']} · <b>{escape(r['title'])}</b>\n{escape(r['text'])}")
        b = InlineKeyboardBuilder()
        b.button(text="➕ Добавить шаблон", callback_data="a:template:new")
        b.button(text="🔙 Назад", callback_data="a:home")
        b.adjust(1)
        await safe_edit(c, "\n\n".join(lines), b.as_markup())
    elif data == "a:template:new":
        await state.set_state(Flow.template_title)
        await safe_edit(c, "Введите название нового шаблона.", back_button("a:templates", "❌ Отмена"))
    elif data == "a:stats":
        users = db("SELECT COUNT(*) n FROM users", fetch=True, one=True)["n"]
        active = db("SELECT COUNT(*) n FROM tickets WHERE status!='closed'", fetch=True, one=True)["n"]
        closed = db("SELECT COUNT(*) n FROM tickets WHERE status='closed'", fetch=True, one=True)["n"]
        msgs = db("SELECT COUNT(*) n FROM messages", fetch=True, one=True)["n"]
        await safe_edit(c, f"📊 <b>Статистика</b>\n\nПользователей: {users}\nАктивных обращений: {active}\nЗакрытых обращений: {closed}\nСообщений в истории: {msgs}", back_button("a:home"))
    await c.answer()


@dp.callback_query(F.data.startswith("a:client:"))
async def admin_client_shortcut(c: types.CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        await c.answer("Недостаточно прав.", show_alert=True)
        return
    tid = int(c.data.rsplit(":", 1)[1])
    t = db("SELECT user_id FROM tickets WHERE id=?", (tid,), fetch=True, one=True)
    if t:
        u = db("SELECT * FROM users WHERE user_id=?", (t["user_id"],), fetch=True, one=True)
        await safe_edit(c, f"👤 <b>Клиент обращения #{tid}</b>\n\nИмя: {escape(u['full_name'] if u else 'неизвестен')}\nID: <code>{t['user_id']}</code>\nUsername: {('@'+escape(u['username'])) if u and u['username'] else 'не указан'}", back_button(f"a:view:{tid}"))
    await c.answer()


@dp.message(Flow.admin_reply)
async def admin_reply(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    t = db("SELECT * FROM tickets WHERE id=?", (ticket_id,), fetch=True, one=True)
    if not t:
        await message.answer("Обращение не найдено.", reply_markup=admin_menu())
        await state.clear()
        return
    text = message.text or message.caption or "[Администратор отправил медиафайл]"
    try:
        await bot.send_message(t["user_id"], f"💬 <b>Ответ специалиста</b>\n\n{escape(text)}", reply_markup=user_menu())
        stamp = now_str()
        db("INSERT INTO messages(ticket_id,user_id,text,direction,timestamp) VALUES(?,?,?,?,?)",
           (ticket_id, t["user_id"], text, "out", stamp))
        db("UPDATE tickets SET updated_at=? WHERE id=?", (stamp, ticket_id))
        await message.answer("✅ Ответ отправлен пользователю.", reply_markup=admin_menu())
    except Exception:
        log.exception("Не удалось отправить ответ по тикету %s", ticket_id)
        await message.answer("❌ Не удалось отправить сообщение. Возможно, пользователь заблокировал бота.", reply_markup=admin_menu())
    await state.clear()


@dp.message(Flow.admin_broadcast_text)
async def admin_broadcast_text(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    uid = data.get("target_user")
    text = message.text or message.caption or "[Администратор отправил медиафайл]"
    try:
        await bot.send_message(uid, f"💬 <b>Сообщение специалиста</b>\n\n{escape(text)}", reply_markup=user_menu())
        stamp = now_str()
        db("INSERT INTO messages(ticket_id,user_id,text,direction,timestamp) VALUES(NULL,?,?,?,?)",
           (uid, text, "out", stamp))
        await message.answer("✅ Сообщение отправлено.", reply_markup=admin_menu())
    except Exception:
        log.exception("Не удалось отправить сообщение пользователю %s", uid)
        await message.answer("❌ Не удалось отправить сообщение этому пользователю.", reply_markup=admin_menu())
    await state.clear()


@dp.message(Flow.reminder_user_id)
async def reminder_user_id(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        uid = int((message.text or "").strip())
    except ValueError:
        await message.answer("Введите числовой Telegram ID.")
        return
    if not db("SELECT user_id FROM users WHERE user_id=?", (uid,), fetch=True, one=True):
        await message.answer("Пользователь не найден в базе. Проверьте ID или попросите его запустить бота.")
        return
    await state.update_data(reminder_user=uid)
    await state.set_state(Flow.reminder_text)
    await message.answer("Введите текст напоминания. Можно использовать {имя}.")


@dp.message(Flow.reminder_text)
async def reminder_text(message: types.Message, state: FSMContext):
    await state.update_data(reminder_text=message.text or "")
    await state.set_state(Flow.reminder_time)
    await message.answer("Введите дату и время: ГГГГ-ММ-ДД ЧЧ:ММ (по времени сервера).")


@dp.message(Flow.reminder_time)
async def reminder_time(message: types.Message, state: FSMContext):
    try:
        dt = datetime.strptime((message.text or "").strip(), "%Y-%m-%d %H:%M")
        if dt <= datetime.now():
            await message.answer("Дата должна быть в будущем. Попробуйте ещё раз.")
            return
    except ValueError:
        await message.answer("Неверный формат. Пример: 2026-12-31 15:30")
        return
    data = await state.get_data()
    db("INSERT INTO reminders(user_id,text,send_at,status) VALUES(?,?,?,'scheduled')",
       (data["reminder_user"], data["reminder_text"], dt.strftime("%Y-%m-%d %H:%M")))
    await state.clear()
    await message.answer("✅ Напоминание создано.", reply_markup=admin_menu())


@dp.message(Flow.template_title)
async def template_title(message: types.Message, state: FSMContext):
    title = (message.text or "").strip()
    if not title:
        await message.answer("Название не должно быть пустым.")
        return
    await state.update_data(template_title=title)
    await state.set_state(Flow.template_text)
    await message.answer("Введите текст шаблона.")


@dp.message(Flow.template_text)
async def template_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    text = (message.text or "").strip()
    if not text:
        await message.answer("Текст шаблона не должен быть пустым.")
        return
    db("INSERT INTO templates(title,text) VALUES(?,?)", (data["template_title"], text))
    await state.clear()
    await message.answer("✅ Шаблон добавлен.", reply_markup=admin_menu())


# ---------- REMINDERS / FALLBACK ----------
async def check_reminders():
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = db("SELECT * FROM reminders WHERE status='scheduled' AND send_at<=?", (stamp,), fetch=True)
    for r in rows:
        try:
            u = db("SELECT full_name FROM users WHERE user_id=?", (r["user_id"],), fetch=True, one=True)
            name = u["full_name"] if u else "Клиент"
            text = r["text"].replace("{имя}", name)
            await bot.send_message(r["user_id"], f"🔔 <b>Напоминание</b>\n\n{escape(text)}")
            db("UPDATE reminders SET status='sent' WHERE id=?", (r["id"],))
            db("INSERT INTO messages(ticket_id,user_id,text,direction,timestamp) VALUES(NULL,?,?,?,?)",
               (r["user_id"], text, "out", now_str()))
        except Exception:
            log.exception("Ошибка отправки напоминания #%s", r["id"])
            db("UPDATE reminders SET status='error' WHERE id=?", (r["id"],))


@dp.message(F.chat.type == "private")
async def fallback(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("Используйте кнопки панели /admin. Для ответа откройте обращение и нажмите «Ответить».")
    else:
        await message.answer("Чтобы создать обращение, откройте меню и нажмите «🔴 Создать обращение».", reply_markup=user_menu())


async def main():
    init_db()
    scheduler.add_job(check_reminders, "interval", minutes=1, id="reminders", replace_existing=True)
    scheduler.start()
    log.info("Бот запускается; база данных: %s", DB_PATH)
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
