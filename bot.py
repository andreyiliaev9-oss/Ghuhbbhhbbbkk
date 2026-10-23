"""
Telegram-бот службы обращений «Призыва Нет»
Python 3.10+ | aiogram 3.x

Важно:
- Цвет заливки стандартных Telegram inline-кнопок нельзя задавать через Bot API.
  Цветные кнопки на скриншоте можно реализовать в Telegram Mini App (HTML/CSS),
  но не у обычной inline-клавиатуры. Здесь используются аккуратные подписи и эмодзи.
- Укажите BOT_TOKEN и ADMIN_ID в .env. Не публикуйте токен.
"""
import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "priziv_bot.db")
WORK_START = int(os.getenv("WORK_START", "9"))
WORK_END = int(os.getenv("WORK_END", "18"))
TIMEZONE_NOTE = os.getenv("TIMEZONE_NOTE", "время сервера")
COMPANY = "Призыва Нет"

if not BOT_TOKEN or not ADMIN_ID:
    raise RuntimeError("Заполните BOT_TOKEN и ADMIN_ID в файле .env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("priziv_bot")
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
scheduler = AsyncIOScheduler()


# ------------------------- БАЗА ДАННЫХ -------------------------
def db(query: str, params: tuple = (), fetch: bool = False, one: bool = False):
    with sqlite3.connect(DB_PATH, timeout=20) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.execute(query, params)
        if fetch:
            rows = cur.fetchall()
            return (rows[0] if rows else None) if one else rows
        conn.commit()
        return cur.lastrowid


def init_db():
    db("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT NOT NULL,
        joined_at TEXT NOT NULL, last_seen TEXT NOT NULL)""")
    db("""CREATE TABLE IF NOT EXISTS tickets(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
        subject TEXT NOT NULL DEFAULT 'Обращение', status TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    db("""CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER,
        user_id INTEGER NOT NULL, sender_id INTEGER NOT NULL, direction TEXT NOT NULL,
        text TEXT NOT NULL, timestamp TEXT NOT NULL)""")
    db("""CREATE TABLE IF NOT EXISTS reminders(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, text TEXT NOT NULL,
        send_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'scheduled')""")
    db("""CREATE TABLE IF NOT EXISTS templates(
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, text TEXT NOT NULL)""")
    if not db("SELECT id FROM templates LIMIT 1", fetch=True):
        db("INSERT INTO templates(title,text) VALUES(?,?)",
           ("Приветствие", "Здравствуйте! Вы обратились в компанию «Призыва Нет»."))


def now_s():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def is_working_time():
    now = datetime.now()
    return now.weekday() < 5 and WORK_START <= now.hour < WORK_END


def register_user(user: types.User):
    stamp = now_s()
    existing = db("SELECT user_id FROM users WHERE user_id=?", (user.id,), fetch=True, one=True)
    if existing:
        db("UPDATE users SET username=?, full_name=?, last_seen=? WHERE user_id=?",
           (user.username, user.full_name, stamp, user.id))
    else:
        db("INSERT INTO users(user_id,username,full_name,joined_at,last_seen) VALUES(?,?,?,?,?)",
           (user.id, user.username, user.full_name, stamp, stamp))


def active_ticket(user_id: int):
    return db("SELECT * FROM tickets WHERE user_id=? AND status IN ('open','waiting') ORDER BY id DESC LIMIT 1",
              (user_id,), fetch=True, one=True)


def create_ticket(user_id: int, subject: str = "Обращение"):
    stamp = now_s()
    return db("INSERT INTO tickets(user_id,subject,status,created_at,updated_at) VALUES(?,?, 'open',?,?)",
              (user_id, subject, stamp, stamp))


def log_message(ticket_id: Optional[int], user_id: int, sender_id: int, direction: str, text: str):
    db("INSERT INTO messages(ticket_id,user_id,sender_id,direction,text,timestamp) VALUES(?,?,?,?,?,?)",
       (ticket_id, user_id, sender_id, direction, text, now_s()))
    if ticket_id:
        db("UPDATE tickets SET updated_at=? WHERE id=?", (now_s(), ticket_id))


# ------------------------- КНОПКИ -------------------------
def user_menu():
    b = InlineKeyboardBuilder()
    b.button(text="👤 Профиль", callback_data="u:profile")
    b.button(text="🔴 Создать обращение", callback_data="u:new_ticket")
    b.button(text="📨 Мои обращения", callback_data="u:tickets")
    b.button(text="ℹ️ Информация", callback_data="u:info")
    b.adjust(1)
    return b.as_markup()


def admin_menu():
    b = InlineKeyboardBuilder()
    b.button(text="📨 Обращения", callback_data="a:tickets")
    b.button(text="👥 Пользователи", callback_data="a:users")
    b.button(text="✉️ Написать пользователю", callback_data="a:send")
    b.button(text="⏰ Напоминания", callback_data="a:reminders")
    b.button(text="📝 Шаблоны", callback_data="a:templates")
    b.button(text="📊 Сводка", callback_data="a:stats")
    b.adjust(2, 2, 2)
    return b.as_markup()


def back(callback_data="u:home", label="↩️ Назад"):
    b = InlineKeyboardBuilder()
    b.button(text=label, callback_data=callback_data)
    return b.as_markup()


def ticket_actions(ticket_id: int, is_admin: bool):
    b = InlineKeyboardBuilder()
    if is_admin:
        b.button(text="✉️ Ответить", callback_data=f"a:reply:{ticket_id}")
        b.button(text="✅ Закрыть", callback_data=f"a:close:{ticket_id}")
        b.button(text="↩️ В меню", callback_data="a:home")
    else:
        b.button(text="✍️ Дополнить обращение", callback_data=f"u:append:{ticket_id}")
        b.button(text="↩️ В меню", callback_data="u:home")
    b.adjust(1)
    return b.as_markup()


class Flow(StatesGroup):
    user_ticket_text = State()
    user_append_text = State()
    admin_reply_text = State()
    admin_target_id = State()
    admin_direct_text = State()
    reminder_user = State()
    reminder_text = State()
    reminder_time = State()
    template_title = State()
    template_text = State()


async def safe_edit(message: types.Message, text: str, markup=None):
    try:
        await message.edit_text(text, reply_markup=markup)
    except (types.TelegramBadRequest, AttributeError):
        await message.answer(text, reply_markup=markup)


async def show_ticket_to_admin(ticket_id: int, destination: types.Message):
    t = db("SELECT t.*,u.full_name,u.username FROM tickets t JOIN users u ON u.user_id=t.user_id WHERE t.id=?",
           (ticket_id,), fetch=True, one=True)
    if not t:
        await destination.answer("Обращение не найдено.")
        return
    user_name = escape(t["full_name"])
    username = f"@{escape(t['username'])}" if t["username"] else "не указан"
    msgs = db("SELECT direction,text,timestamp FROM messages WHERE ticket_id=? ORDER BY id DESC LIMIT 8",
              (ticket_id,), fetch=True)
    lines = []
    for m in reversed(msgs):
        who = "Клиент" if m["direction"] == "in" else "Оператор"
        lines.append(f"<b>{who}</b> · {escape(m['timestamp'])}\n{escape(m['text'][:900])}")
    text = (f"<b>Обращение #{ticket_id}</b> · {'🟢 открыто' if t['status']=='open' else '🟡 ожидает'}\n"
            f"👤 {user_name}\n🆔 <code>{t['user_id']}</code>\n🔗 {username}\n"
            f"📅 Создано: {escape(t['created_at'])}\n\n" + ("\n\n".join(lines) or "Сообщений пока нет."))
    await destination.answer(text, reply_markup=ticket_actions(ticket_id, True))


# ------------------------- СТАРТ И ПОЛЬЗОВАТЕЛЬ -------------------------
@dp.message(CommandStart())
async def start(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    await state.clear()
    register_user(message.from_user)
    if message.from_user.id == ADMIN_ID:
        await message.answer(f"👑 <b>Кабинет администратора · {COMPANY}</b>\n\nВыберите раздел:", reply_markup=admin_menu())
    else:
        await message.answer(
            f"Здравствуйте, {escape(message.from_user.first_name or 'друг')}!\n\n"
            f"Добро пожаловать в бот компании «{COMPANY}».\n"
            "Здесь можно создать обращение, посмотреть его статус и получить ответ специалиста.\n\n"
            "🔒 Ваш диалог ведётся через бота: личные контакты администратора пользователю не показываются.",
            reply_markup=user_menu())


@dp.message(Command("admin"))
async def admin_command(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer("👑 <b>Кабинет администратора</b>", reply_markup=admin_menu())


@dp.callback_query(F.data == "u:home")
async def user_home(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(c.message, "🏠 <b>Главное меню</b>\nВыберите нужный раздел:", user_menu())
    await c.answer()


@dp.callback_query(F.data == "a:home")
async def admin_home(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        await c.answer("Нет доступа", show_alert=True); return
    await state.clear()
    await safe_edit(c.message, "👑 <b>Кабинет администратора</b>", admin_menu())
    await c.answer()


@dp.callback_query(F.data.startswith("u:"))
async def user_callbacks(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id == ADMIN_ID:
        await c.answer("Этот раздел предназначен для пользователя."); return
    action = c.data
    if action == "u:profile":
        u = db("SELECT * FROM users WHERE user_id=?", (c.from_user.id,), fetch=True, one=True)
        count = db("SELECT COUNT(*) n FROM tickets WHERE user_id=?", (c.from_user.id,), fetch=True, one=True)["n"]
        text = (f"👤 <b>Мой профиль</b>\n\nИмя: {escape(u['full_name'])}\n"
                f"Telegram ID: <code>{u['user_id']}</code>\nРегистрация: {escape(u['joined_at'])}\n"
                f"Обращений: {count}")
        await safe_edit(c.message, text, back())
    elif action == "u:info":
        await safe_edit(c.message,
            "ℹ️ <b>Информация</b>\n\n"
            f"Компания: «{COMPANY}»\n"
            f"График: понедельник–пятница, {WORK_START:02d}:00–{WORK_END:02d}:00 ({TIMEZONE_NOTE}).\n"
            "Сообщения вне графика также принимаются. Ответ специалиста поступит в этот чат.\n\n"
            "Не отправляйте пароли, коды подтверждения и данные банковских карт.",
            back())
    elif action == "u:new_ticket":
        await state.set_state(Flow.user_ticket_text)
        await safe_edit(c.message, "🔴 <b>Новое обращение</b>\n\nОпишите вопрос одним сообщением. Можно отправить текст, фото или документ следующим сообщением.\n\nДля отмены нажмите «Назад».", back())
    elif action == "u:tickets":
        tickets = db("SELECT * FROM tickets WHERE user_id=? ORDER BY id DESC LIMIT 10", (c.from_user.id,), fetch=True)
        b = InlineKeyboardBuilder()
        for t in tickets:
            status = {"open":"🟢 открыто", "waiting":"🟡 ожидает ответа", "closed":"⚫ закрыто"}.get(t["status"], t["status"])
            b.button(text=f"#{t['id']} · {status}", callback_data=f"u:view:{t['id']}")
        b.button(text="↩️ Назад", callback_data="u:home")
        b.adjust(1)
        text = "📨 <b>Мои обращения</b>\nВыберите обращение для просмотра." if tickets else "Пока обращений нет."
        await safe_edit(c.message, text, b.as_markup())
    elif action.startswith("u:view:"):
        ticket_id = int(action.split(":")[2])
        t = db("SELECT * FROM tickets WHERE id=? AND user_id=?", (ticket_id,c.from_user.id), fetch=True, one=True)
        if not t:
            await c.answer("Обращение не найдено", show_alert=True); return
        msgs = db("SELECT direction,text,timestamp FROM messages WHERE ticket_id=? ORDER BY id DESC LIMIT 8", (ticket_id,), fetch=True)
        lines = []
        for m in reversed(msgs):
            who = "Вы" if m["direction"] == "in" else "Специалист"
            lines.append(f"<b>{who}</b> · {escape(m['timestamp'])}\n{escape(m['text'][:700])}")
        status = {"open":"🟢 открыто", "waiting":"🟡 ожидает ответа", "closed":"⚫ закрыто"}.get(t["status"],t["status"])
        await safe_edit(c.message, f"📨 <b>Обращение #{ticket_id}</b>\nСтатус: {status}\n\n" + ("\n\n".join(lines) or "Сообщений пока нет."),
                        ticket_actions(ticket_id, False) if t["status"] != "closed" else back("u:tickets"))
    elif action.startswith("u:append:"):
        ticket_id = int(action.split(":")[2])
        t = db("SELECT id FROM tickets WHERE id=? AND user_id=? AND status!='closed'", (ticket_id,c.from_user.id), fetch=True, one=True)
        if not t:
            await c.answer("Обращение закрыто или не найдено", show_alert=True); return
        await state.update_data(ticket_id=ticket_id)
        await state.set_state(Flow.user_append_text)
        await safe_edit(c.message, f"✍️ Напишите дополнение к обращению #{ticket_id}.", back("u:tickets"))
    await c.answer()


@dp.message(Flow.user_ticket_text)
async def user_ticket_received(message: types.Message, state: FSMContext):
    if message.from_user.id == ADMIN_ID: return
    if not message.text and not message.caption:
        await message.answer("Пожалуйста, отправьте обращение текстом. Вложения можно добавить после создания обращения.")
        return
    text = message.text or message.caption or ""
    tid = create_ticket(message.from_user.id)
    log_message(tid, message.from_user.id, message.from_user.id, "in", text)
    await state.clear()
    ack = "✅ Обращение принято и передано специалисту."
    if not is_working_time():
        ack += f"\nСейчас нерабочее время. Ответ поступит в рабочие часы ({WORK_START:02d}:00–{WORK_END:02d}:00, пн–пт)."
    await message.answer(ack, reply_markup=user_menu())
    # Уведомление отправляется только администратору, не в чат клиента.
    u = db("SELECT full_name,username FROM users WHERE user_id=?", (message.from_user.id,), fetch=True, one=True)
    username = "@" + escape(u["username"]) if u["username"] else "не указан"
    await bot.send_message(
        ADMIN_ID,
        f"🔔 <b>Новое обращение #{tid}</b>\n"
        f"👤 {escape(u['full_name'])}\n"
        f"🆔 <code>{message.from_user.id}</code>\n"
        f"🔗 {username}\n\n"
        f"<b>Сообщение:</b>\n{escape(text)}",
        reply_markup=ticket_actions(tid, True),
    )


@dp.message(Flow.user_append_text)
async def user_append_received(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tid = data.get("ticket_id")
    t = db("SELECT id FROM tickets WHERE id=? AND user_id=? AND status!='closed'", (tid,message.from_user.id), fetch=True, one=True)
    if not t:
        await state.clear(); await message.answer("Обращение не найдено или уже закрыто.", reply_markup=user_menu()); return
    text = message.text or message.caption or "[вложение без подписи]"
    log_message(tid, message.from_user.id, message.from_user.id, "in", text)
    await state.clear()
    await message.answer(f"✅ Дополнение добавлено к обращению #{tid}.", reply_markup=user_menu())
    await bot.send_message(ADMIN_ID, f"📩 <b>Дополнение к обращению #{tid}</b>\n"
                        f"Клиент ID: <code>{message.from_user.id}</code>\n{escape(text)}",
                        reply_markup=ticket_actions(tid, True))


# ------------------------- АДМИНСКИЕ РАЗДЕЛЫ -------------------------
@dp.callback_query(F.data.startswith("a:"))
async def admin_callbacks(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        await c.answer("Нет доступа", show_alert=True); return
    action = c.data
    if action == "a:tickets":
        rows = db("SELECT t.id,t.status,t.updated_at,u.full_name FROM tickets t JOIN users u ON u.user_id=t.user_id WHERE t.status!='closed' ORDER BY t.updated_at DESC LIMIT 30", fetch=True)
        b = InlineKeyboardBuilder()
        for t in rows:
            b.button(text=f"#{t['id']} · {t['full_name'][:22]} · {t['status']}", callback_data=f"a:view:{t['id']}")
        b.button(text="↩️ Назад", callback_data="a:home"); b.adjust(1)
        await safe_edit(c.message, "📨 <b>Активные обращения</b>\nВыберите обращение." if rows else "📨 Активных обращений нет.", b.as_markup())
    elif action.startswith("a:view:"):
        await show_ticket_to_admin(int(action.split(":")[2]), c.message)
    elif action.startswith("a:reply:"):
        tid = int(action.split(":")[2])
        t = db("SELECT id FROM tickets WHERE id=? AND status!='closed'", (tid,), fetch=True, one=True)
        if not t:
            await c.answer("Обращение закрыто", show_alert=True); return
        await state.update_data(ticket_id=tid)
        await state.set_state(Flow.admin_reply_text)
        await safe_edit(c.message, f"✉️ <b>Ответ на обращение #{tid}</b>\nОтправьте текст следующим сообщением.", back("a:tickets"))
    elif action.startswith("a:close:"):
        tid = int(action.split(":")[2])
        db("UPDATE tickets SET status='closed',updated_at=? WHERE id=?", (now_s(),tid))
        await c.answer("Обращение закрыто")
        await safe_edit(c.message, f"✅ Обращение #{tid} закрыто.", back("a:tickets"))
    elif action == "a:users":
        rows = db("SELECT u.*,COUNT(t.id) ticket_count FROM users u LEFT JOIN tickets t ON t.user_id=u.user_id GROUP BY u.user_id ORDER BY u.last_seen DESC LIMIT 20", fetch=True)
        b = InlineKeyboardBuilder()
        for u in rows:
            b.button(text=f"{u['full_name'][:24]} · {u['user_id']}", callback_data=f"a:user:{u['user_id']}")
        b.button(text="↩️ Назад", callback_data="a:home"); b.adjust(1)
        await safe_edit(c.message, "👥 <b>Пользователи</b>\nПоследние 20 пользователей:", b.as_markup())
    elif action.startswith("a:user:"):
        uid = int(action.split(":")[2])
        u = db("SELECT * FROM users WHERE user_id=?", (uid,), fetch=True, one=True)
        if not u:
            await c.answer("Пользователь не найден", show_alert=True); return
        count = db("SELECT COUNT(*) n FROM tickets WHERE user_id=?", (uid,), fetch=True, one=True)["n"]
        uname = "@"+escape(u["username"]) if u["username"] else "не указан"
        b = InlineKeyboardBuilder(); b.button(text="✉️ Написать", callback_data=f"a:sendto:{uid}")
        b.button(text="📨 Обращения", callback_data=f"a:usertickets:{uid}")
        b.button(text="↩️ Назад", callback_data="a:users"); b.adjust(1)
        await safe_edit(c.message, f"👤 <b>Карточка пользователя</b>\n\nИмя: {escape(u['full_name'])}\n"
                          f"Username: {uname}\nID: <code>{uid}</code>\nРегистрация: {escape(u['joined_at'])}\nОбращений: {count}", b.as_markup())
    elif action.startswith("a:usertickets:"):
        uid = int(action.split(":")[2])
        rows = db("SELECT id,status FROM tickets WHERE user_id=? ORDER BY id DESC LIMIT 20", (uid,), fetch=True)
        b = InlineKeyboardBuilder()
        for t in rows: b.button(text=f"#{t['id']} · {t['status']}", callback_data=f"a:view:{t['id']}")
        b.button(text="↩️ Назад", callback_data=f"a:user:{uid}"); b.adjust(1)
        await safe_edit(c.message, "📨 <b>Обращения пользователя</b>", b.as_markup())
    elif action == "a:send":
        await state.set_state(Flow.admin_target_id)
        await safe_edit(c.message, "✉️ Введите Telegram ID пользователя.", back("a:home"))
    elif action.startswith("a:sendto:"):
        uid = int(action.split(":")[2])
        await state.update_data(target_user=uid)
        await state.set_state(Flow.admin_direct_text)
        await safe_edit(c.message, f"✉️ Введите сообщение для пользователя <code>{uid}</code>.", back("a:users"))
    elif action == "a:stats":
        users = db("SELECT COUNT(*) n FROM users", fetch=True, one=True)["n"]
        open_t = db("SELECT COUNT(*) n FROM tickets WHERE status='open'", fetch=True, one=True)["n"]
        waiting = db("SELECT COUNT(*) n FROM tickets WHERE status='waiting'", fetch=True, one=True)["n"]
        closed = db("SELECT COUNT(*) n FROM tickets WHERE status='closed'", fetch=True, one=True)["n"]
        await safe_edit(c.message, f"📊 <b>Сводка</b>\n\nПользователей: {users}\n🟢 Открытых обращений: {open_t}\n🟡 Ожидают: {waiting}\n⚫ Закрытых: {closed}", back("a:home"))
    elif action == "a:reminders":
        rows = db("SELECT * FROM reminders WHERE status='scheduled' ORDER BY send_at LIMIT 20", fetch=True)
        b = InlineKeyboardBuilder(); b.button(text="➕ Создать напоминание", callback_data="a:addrem")
        b.button(text="↩️ Назад", callback_data="a:home"); b.adjust(1)
        content = "\n".join(f"#{r['id']} · {r['user_id']} · {escape(r['send_at'])}\n{escape(r['text'][:80])}" for r in rows)
        await safe_edit(c.message, "⏰ <b>Активные напоминания</b>\n\n" + (content or "Нет запланированных напоминаний."), b.as_markup())
    elif action == "a:addrem":
        await state.set_state(Flow.reminder_user)
        await safe_edit(c.message, "Введите Telegram ID получателя напоминания.", back("a:reminders"))
    elif action == "a:templates":
        rows = db("SELECT * FROM templates ORDER BY id", fetch=True)
        b = InlineKeyboardBuilder()
        for t in rows: b.button(text=f"#{t['id']} · {t['title'][:30]}", callback_data=f"a:usetemplate:{t['id']}")
        b.button(text="➕ Добавить шаблон", callback_data="a:addtemplate")
        b.button(text="↩️ Назад", callback_data="a:home"); b.adjust(1)
        await safe_edit(c.message, "📝 <b>Шаблоны сообщений</b>\nВыберите шаблон, чтобы отправить его пользователю, или добавьте новый.", b.as_markup())
    elif action.startswith("a:usetemplate:"):
        tid = int(action.split(":")[2])
        t = db("SELECT * FROM templates WHERE id=?", (tid,), fetch=True, one=True)
        if t:
            await state.update_data(template_text=t["text"])
            await state.set_state(Flow.admin_target_id)
            await safe_edit(c.message, f"Шаблон «{escape(t['title'])}» выбран.\nВведите Telegram ID получателя.", back("a:templates"))
    elif action == "a:addtemplate":
        await state.set_state(Flow.template_title)
        await safe_edit(c.message, "Введите название нового шаблона.", back("a:templates"))
    await c.answer()


@dp.message(Flow.admin_reply_text)
async def admin_reply_received(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    data = await state.get_data(); tid = data.get("ticket_id")
    t = db("SELECT * FROM tickets WHERE id=? AND status!='closed'", (tid,), fetch=True, one=True)
    if not t:
        await state.clear(); await message.answer("Обращение закрыто или не найдено.", reply_markup=admin_menu()); return
    text = message.text or message.caption or ""
    if not text:
        await message.answer("Отправьте ответ текстом."); return
    try:
        await bot.send_message(t["user_id"], f"💬 <b>Ответ специалиста по обращению #{tid}</b>\n\n{escape(text)}")
        log_message(tid, t["user_id"], ADMIN_ID, "out", text)
        db("UPDATE tickets SET status='waiting',updated_at=? WHERE id=?", (now_s(),tid))
        await message.answer("✅ Ответ доставлен. Пользователь видит ответ от имени бота.", reply_markup=admin_menu())
    except Exception:
        log.exception("Не удалось отправить ответ")
        await message.answer("❌ Не удалось доставить ответ. Проверьте, запускал ли пользователь бота.", reply_markup=admin_menu())
    await state.clear()


@dp.message(Flow.admin_target_id)
async def admin_target_received(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    try: uid = int(message.text.strip())
    except (ValueError, AttributeError):
        await message.answer("Нужен числовой Telegram ID. Попробуйте ещё раз."); return
    data = await state.get_data()
    await state.update_data(target_user=uid)
    template_text = data.get("template_text")
    if template_text:
        try:
            await bot.send_message(uid, escape(template_text))
            log_message(None, uid, ADMIN_ID, "out", template_text)
            await message.answer("✅ Сообщение по шаблону отправлено.", reply_markup=admin_menu())
            await state.clear()
        except Exception:
            await message.answer("❌ Не удалось отправить. Пользователь должен сначала запустить бота.", reply_markup=admin_menu())
            await state.clear()
        return
    await state.set_state(Flow.admin_direct_text)
    await message.answer(f"Введите текст для пользователя {uid}.", reply_markup=back("a:home"))


@dp.message(Flow.admin_direct_text)
async def admin_direct_received(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    data = await state.get_data(); uid = data.get("target_user")
    text = message.text or message.caption or ""
    if not text:
        await message.answer("Отправьте текст сообщения."); return
    try:
        await bot.send_message(uid, f"💬 <b>Сообщение от специалиста</b>\n\n{escape(text)}")
        log_message(None, uid, ADMIN_ID, "out", text)
        await message.answer("✅ Сообщение доставлено.", reply_markup=admin_menu())
    except Exception:
        await message.answer("❌ Не удалось доставить сообщение. Пользователь должен сначала запустить бота.", reply_markup=admin_menu())
    await state.clear()


@dp.message(Flow.reminder_user)
async def reminder_user(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    try: uid = int(message.text.strip())
    except (ValueError, AttributeError):
        await message.answer("Введите числовой Telegram ID."); return
    await state.update_data(rem_user=uid); await state.set_state(Flow.reminder_text)
    await message.answer("Введите текст напоминания. Можно использовать {имя}.")


@dp.message(Flow.reminder_text)
async def reminder_text(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    if not message.text:
        await message.answer("Введите текст."); return
    await state.update_data(rem_text=message.text); await state.set_state(Flow.reminder_time)
    await message.answer("Введите дату и время: YYYY-MM-DD HH:MM")


@dp.message(Flow.reminder_time)
async def reminder_time(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    try:
        dt = datetime.strptime(message.text.strip(), "%Y-%m-%d %H:%M")
        if dt <= datetime.now(): raise ValueError
    except (ValueError, AttributeError):
        await message.answer("Неверное время или дата уже прошла. Формат: YYYY-MM-DD HH:MM"); return
    data = await state.get_data()
    db("INSERT INTO reminders(user_id,text,send_at,status) VALUES(?,?,?,'scheduled')",
       (data["rem_user"], data["rem_text"], dt.strftime("%Y-%m-%d %H:%M")))
    await state.clear(); await message.answer("✅ Напоминание запланировано.", reply_markup=admin_menu())


@dp.message(Flow.template_title)
async def template_title(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    if not message.text or len(message.text) > 80:
        await message.answer("Название должно содержать до 80 символов."); return
    await state.update_data(template_title=message.text.strip()); await state.set_state(Flow.template_text)
    await message.answer("Теперь введите текст шаблона.")


@dp.message(Flow.template_text)
async def template_text(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    data = await state.get_data()
    if not message.text:
        await message.answer("Введите текст шаблона."); return
    db("INSERT INTO templates(title,text) VALUES(?,?)", (data["template_title"], message.text))
    await state.clear(); await message.answer("✅ Шаблон сохранён.", reply_markup=admin_menu())


# Catch-all private user messages when not in a form: guide them into creating a ticket.
@dp.message(F.chat.type == ChatType.PRIVATE, F.from_user.id != ADMIN_ID)
async def user_fallback(message: types.Message):
    register_user(message.from_user)
    await message.answer("Чтобы сообщение не потерялось, создайте обращение через меню ниже.", reply_markup=user_menu())


async def check_reminders():
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = db("SELECT * FROM reminders WHERE status='scheduled' AND send_at<=? ORDER BY id LIMIT 50", (stamp,), fetch=True)
    for r in rows:
        try:
            u = db("SELECT full_name FROM users WHERE user_id=?", (r["user_id"],), fetch=True, one=True)
            name = u["full_name"] if u else "Клиент"
            text = r["text"].replace("{имя}", name)
            await bot.send_message(r["user_id"], f"🔔 <b>Напоминание</b>\n\n{escape(text)}")
            db("UPDATE reminders SET status='sent' WHERE id=?", (r["id"],))
            log_message(None, r["user_id"], ADMIN_ID, "out", text)
        except Exception:
            log.exception("Ошибка отправки напоминания #%s", r["id"])
            db("UPDATE reminders SET status='error' WHERE id=?", (r["id"],))


async def main():
    init_db()
    scheduler.add_job(check_reminders, "interval", minutes=1, id="reminders", replace_existing=True)
    scheduler.start()
    log.info("Бот «Призыва Нет» запущен")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
