import asyncio
import sqlite3
import logging
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- НАСТРОЙКИ ---
BOT_TOKEN = "8836985962:AAEEjRSTMV0BGVkcQIi1J2Afri1Vz8dfckc"  # ОБЯЗАТЕЛЬНО ЗАМЕНИТЕ!
ADMIN_ID = 8141670312
WORK_START = 9  # 09:00
WORK_END = 18   # 18:00

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# --- БАЗА ДАННЫХ ---
def db_query(query, params=(), fetch=False):
    conn = sqlite3.connect('priziv_bot.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(query, params)
    if fetch:
        result = cursor.fetchall()
    else:
        result = None
        conn.commit()
    conn.close()
    return result

def init_db():
    # Пользователи
    db_query('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, joined_at TEXT)''')
    # Сообщения
    db_query('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, text TEXT, direction TEXT, timestamp TEXT)''')
    # Обращения (Тикеты)
    db_query('''CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, status TEXT, created_at TEXT, updated_at TEXT)''')
    # Напоминания
    db_query('''CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, text TEXT, send_at TEXT, status TEXT)''')
    # Шаблоны
    db_query('''CREATE TABLE IF NOT EXISTS templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, text TEXT)''')
    
    # Добавляем базовый шаблон, если его нет
    if not db_query("SELECT * FROM templates", fetch=True):
        db_query("INSERT INTO templates (title, text) VALUES (?, ?)", 
                 ("Приветствие", "Здравствуйте! Вы обратились в компанию «Призыва Нет»."))

def is_working_time():
    now = datetime.now()
    # Пн-Пт (0-4) и рабочее время
    return now.weekday() < 5 and WORK_START <= now.hour < WORK_END

# --- FSM ДЛЯ АДМИНА ---
class AdminStates(StatesGroup):
    main_menu = State()
    waiting_for_user_id = State()
    waiting_for_message = State()
    waiting_for_reminder_text = State()
    waiting_for_reminder_time = State()
    waiting_for_template_title = State()
    waiting_for_template_text = State()

# ==========================================
# КЛАВИАТУРЫ (Цветные кнопки через эмодзи)
# ==========================================

def get_user_menu():
    builder = InlineKeyboardBuilder()
    # Белые кнопки
    builder.button(text="👤 Профиль", callback_data="u_profile")
    # КРАСНАЯ кнопка (вместо зеленой)
    builder.button(text="🔴 ⚡ ПОДКЛЮЧИТЬСЯ", callback_data="u_connect")
    # Белые кнопки
    builder.button(text="⚪️ ℹ️ Инфо", callback_data="u_info")
    builder.button(text="🌐 Язык", callback_data="u_lang")
    builder.adjust(1)
    return builder.as_markup()

def get_admin_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="👥 Пользователи", callback_data="a_users")
    builder.button(text="📩 Обращения", callback_data="a_tickets")
    builder.button(text="⏰ Напоминания", callback_data="a_reminders")
    builder.button(text="📝 Шаблоны", callback_data="a_templates")
    builder.adjust(2)
    return builder.as_markup()

def get_cancel_btn():
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="a_cancel")
    return builder.as_markup()

# ==========================================
# ПОЛЬЗОВАТЕЛЬСКАЯ ЧАСТЬ
# ==========================================

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    
    # Регистрация
    if not db_query("SELECT * FROM users WHERE user_id = ?", (user_id,), fetch=True):
        db_query("INSERT INTO users (user_id, username, full_name, joined_at) VALUES (?, ?, ?, ?)",
                 (user_id, message.from_user.username, message.from_user.full_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    
    text = (
        "Здравствуйте!\n\n"
        "Вы обратились в компанию «Призыва Нет».\n\n"
        "В данном чате вы можете получать уведомления, направлять обращения оператору и знакомиться с информацией о режиме работы.\n\n"
        "Выберите необходимый раздел в меню ниже."
    )
    await message.answer(text, reply_markup=get_user_menu())

@dp.callback_query(F.data.startswith("u_"))
async def user_callbacks(callback: types.CallbackQuery):
    action = callback.data
    
    if action == "u_profile":
        await callback.message.answer(f"Ваш ID: `{callback.from_user.id}`\nИмя: {callback.from_user.full_name}\nСтатус: Активен", parse_mode=ParseMode.MARKDOWN)
    
    elif action == "u_connect":
        await callback.message.answer("Вы можете направить сообщение оператору компании «Призыва Нет».\n\nПожалуйста, изложите суть вашего вопроса одним сообщением. Ответ поступит в этот чат.")
    
    elif action == "u_info":
        await callback.message.answer("ℹ️ **Информация**\n\nЭто официальный бот компании. Мы уведомляем вас о платежах и принимаем обращения.\n\nГрафик: Пн-Пт с 09:00 до 18:00.")
    
    elif action == "u_lang":
        await callback.message.answer("Смена языка временно недоступна.")
    
    await callback.answer()

# Обработка сообщений от пользователя
@dp.message(F.chat.type == "private", F.from_user.id != ADMIN_ID)
async def handle_user_message(message: types.Message):
    user_id = message.from_user.id
    text = message.text or "Медиафайл"
    
    # 1. Логируем в БД
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db_query("INSERT INTO messages (user_id, text, direction, timestamp) VALUES (?, ?, ?, ?)", (user_id, text, 'in', now_str))
    
    # 2. Создаем или обновляем тикет
    ticket = db_query("SELECT id FROM tickets WHERE user_id = ? AND status != 'closed'", (user_id,), fetch=True)
    if not ticket:
        db_query("INSERT INTO tickets (user_id, status, created_at, updated_at) VALUES (?, 'new', ?, ?)", (user_id, now_str, now_str))
    else:
        db_query("UPDATE tickets SET status = 'new', updated_at = ? WHERE id = ?", (now_str, ticket[0]['id']))

    # 3. Ответ пользователю (защита от спама)
    if is_working_time():
        reply_text = "✅ Ваше сообщение принято и передано оператору.\nСпециалист рассмотрит его в ближайшее время."
    else:
        reply_text = "✅ Ваше сообщение принято.\nСейчас нерабочее время. Специалист ответит вам в рабочие часы (Пн-Пт с 09:00 до 18:00)."
    
    # Отправляем подтверждение только если это первое сообщение в серии
    last_msg = db_query("SELECT text FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT 2", (user_id,), fetch=True)
    if len(last_msg) == 1 or "принято" not in last_msg[1]['text']: 
        await message.answer(reply_text)

    # 4. Уведомление Админу
    admin_text = (
        f"🔔 **Новое обращение**\n"
        f"👤 {message.from_user.full_name}\n"
        f"🆔 `{user_id}`\n"
        f"💬 {text}\n\n"
        f"👇 *Чтобы ответить, нажмите Reply на это сообщение*"
    )
    await bot.send_message(ADMIN_ID, admin_text, parse_mode=ParseMode.MARKDOWN)

# ==========================================
# АДМИН-ПАНЕЛЬ (FSM)
# ==========================================

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await state.set_state(AdminStates.main_menu)
    await message.answer("👑 **Панель управления «Призыва Нет»**\nВыберите раздел:", reply_markup=get_admin_menu(), parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data == "a_cancel")
async def admin_cancel(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.set_state(AdminStates.main_menu)
    await callback.message.edit_text("👑 Главное меню администратора:", reply_markup=get_admin_menu())

@dp.callback_query(F.data == "a_users")
async def admin_users(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    users = db_query("SELECT * FROM users ORDER BY joined_at DESC LIMIT 10", fetch=True)
    
    builder = InlineKeyboardBuilder()
    for u in users:
        builder.button(text=f"{u['full_name']} ({u['user_id']})", callback_data=f"view_user_{u['user_id']}")
    builder.button(text="🔙 Назад", callback_data="a_cancel")
    builder.adjust(1)
    
    await callback.message.edit_text("👥 **Последние 10 пользователей:**\nВыберите для просмотра:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("view_user_"))
async def view_user(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    user_id = int(callback.data.split("_")[2])
    
    user = db_query("SELECT * FROM users WHERE user_id = ?", (user_id,), fetch=True)[0]
    msgs_count = len(db_query("SELECT id FROM messages WHERE user_id = ?", (user_id,), fetch=True))
    
    text = f"👤 **Карточка пользователя**\nID: `{user_id}`\nИмя: {user['full_name']}\nUsername: @{user['username']}\nДата регистрации: {user['joined_at']}\nСообщений: {msgs_count}"
    
    builder = InlineKeyboardBuilder()
    builder.button(text="✉️ Написать сообщение", callback_data=f"msg_user_{user_id}")
    builder.button(text="📜 История", callback_data=f"hist_user_{user_id}")
    builder.button(text="🔙 Назад", callback_data="a_users")
    builder.adjust(1)
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data.startswith("msg_user_"))
async def start_msg_user(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    user_id = int(callback.data.split("_")[2])
    await state.update_data(target_user=user_id)
    await state.set_state(AdminStates.waiting_for_message)
    await callback.message.edit_text(f"Введите текст сообщения для пользователя `{user_id}`:", reply_markup=get_cancel_btn(), parse_mode=ParseMode.MARKDOWN)

@dp.message(AdminStates.waiting_for_message)
async def send_msg_to_user(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    data = await state.get_data()
    user_id = data['target_user']
    
    try:
        await bot.send_message(user_id, message.text)
        db_query("INSERT INTO messages (user_id, text, direction, timestamp) VALUES (?, ?, ?, ?)", 
                 (user_id, message.text, 'out', datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        await message.answer("✅ Сообщение отправлено!", reply_markup=get_admin_menu())
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}", reply_markup=get_admin_menu())
    
    await state.set_state(AdminStates.main_menu)

# --- ОТВЕТ ЧЕРЕЗ REPLY (Быстрый способ) ---
@dp.message(F.chat.type == "private", F.from_user.id == ADMIN_ID, F.reply_to_message)
async def admin_reply_handler(message: types.Message):
    if "🆔 `" in message.reply_to_message.text:
        try:
            user_id = int(message.reply_to_message.text.split("🆔 `")[1].split("`")[0])
            await bot.send_message(user_id, message.text)
            db_query("INSERT INTO messages (user_id, text, direction, timestamp) VALUES (?, ?, ?, ?)", 
                     (user_id, message.text, 'out', datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            await message.answer("✅ Ответ доставлен пользователю.")
        except Exception as e:
            await message.answer(f"❌ Ошибка: {e}")

# --- НАПОМИНАНИЯ ---
@dp.callback_query(F.data == "a_reminders")
async def admin_reminders(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    reminders = db_query("SELECT * FROM reminders WHERE status = 'scheduled'", fetch=True)
    
    text = "⏰ **Активные напоминания:**\n\n"
    if not reminders:
        text += "Нет активных напоминаний."
    for r in reminders:
        text += f"ID: {r['id']} | Юзер: {r['user_id']} | Время: {r['send_at']}\nТекст: {r['text']}\n\n"
        
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Создать напоминание", callback_data="add_reminder")
    builder.button(text="🔙 Назад", callback_data="a_cancel")
    builder.adjust(1)
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data == "add_reminder")
async def add_reminder_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.set_state(AdminStates.waiting_for_user_id)
    await callback.message.edit_text("Введите ID пользователя, которому нужно отправить напоминание:", reply_markup=get_cancel_btn())

@dp.message(AdminStates.waiting_for_user_id)
async def reminder_get_id(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
        await state.update_data(rem_user=user_id)
        await state.set_state(AdminStates.waiting_for_reminder_text)
        await message.answer("Введите текст напоминания (можно использовать {имя}):", reply_markup=get_cancel_btn())
    except ValueError:
        await message.answer("Ошибка! Введите числовой ID.")

@dp.message(AdminStates.waiting_for_reminder_text)
async def reminder_get_text(message: types.Message, state: FSMContext):
    await state.update_data(rem_text=message.text)
    await state.set_state(AdminStates.waiting_for_reminder_time)
    await message.answer("Введите дату и время отправки в формате `ГГГГ-ММ-ДД ЧЧ:ММ` (например, `2024-12-31 15:30`):", parse_mode=ParseMode.MARKDOWN, reply_markup=get_cancel_btn())

@dp.message(AdminStates.waiting_for_reminder_time)
async def reminder_get_time(message: types.Message, state: FSMContext):
    try:
        send_at = datetime.strptime(message.text, "%Y-%m-%d %H:%M")
        data = await state.get_data()
        
        db_query("INSERT INTO reminders (user_id, text, send_at, status) VALUES (?, ?, ?, 'scheduled')", 
                 (data['rem_user'], data['rem_text'], send_at.strftime("%Y-%m-%d %H:%M")))
        
        await message.answer("✅ Напоминание успешно создано!", reply_markup=get_admin_menu())
        await state.set_state(AdminStates.main_menu)
    except ValueError:
        await message.answer("❌ Неверный формат! Используйте `ГГГГ-ММ-ДД ЧЧ:ММ`.")

# --- ШАБЛОНЫ ---
@dp.callback_query(F.data == "a_templates")
async def admin_templates(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    templates = db_query("SELECT * FROM templates", fetch=True)
    
    text = "📝 **Шаблоны сообщений:**\n\n"
    for t in templates:
        text += f"ID: {t['id']} | {t['title']}\nТекст: {t['text']}\n\n"
        
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить шаблон", callback_data="add_template")
    builder.button(text="🔙 Назад", callback_data="a_cancel")
    builder.adjust(1)
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data == "add_template")
async def add_template_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.set_state(AdminStates.waiting_for_template_title)
    await callback.message.edit_text("Введите название шаблона (например, 'Напоминание об оплате'):", reply_markup=get_cancel_btn())

@dp.message(AdminStates.waiting_for_template_title)
async def template_get_title(message: types.Message, state: FSMContext):
    await state.update_data(t_title=message.text)
    await state.set_state(AdminStates.waiting_for_template_text)
    await message.answer("Введите текст шаблона:")

@dp.message(AdminStates.waiting_for_template_text)
async def template_get_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    db_query("INSERT INTO templates (title, text) VALUES (?, ?)", (data['t_title'], message.text))
    await message.answer("✅ Шаблон добавлен!", reply_markup=get_admin_menu())
    await state.set_state(AdminStates.main_menu)

# --- ОБРАЩЕНИЯ ---
@dp.callback_query(F.data == "a_tickets")
async def admin_tickets(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    tickets = db_query("SELECT * FROM tickets WHERE status != 'closed' ORDER BY updated_at DESC", fetch=True)
    
    text = "📩 **Активные обращения:**\n\n"
    if not tickets:
        text += "Нет активных обращений."
    for t in tickets:
        text += f"ID: {t['id']} | Юзер: {t['user_id']} | Статус: {t['status']}\n\n"
        
    builder = InlineKeyboardBuilder()
    for t in tickets:
        builder.button(text=f"Закрыть обращение #{t['id']}", callback_data=f"close_ticket_{t['id']}")
    builder.button(text="🔙 Назад", callback_data="a_cancel")
    builder.adjust(1)
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data.startswith("close_ticket_"))
async def close_ticket(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    ticket_id = int(callback.data.split("_")[2])
    db_query("UPDATE tickets SET status = 'closed', updated_at = ? WHERE id = ?", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ticket_id))
    await callback.answer("Обращение закрыто.")
    await admin_tickets(callback, None) # Обновляем список

# ==========================================
# ФОНОВЫЕ ЗАДАЧИ (ПЛАНИРОВЩИК)
# ==========================================

async def check_reminders():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    # Ищем напоминания, время которых наступило
    reminders = db_query("SELECT * FROM reminders WHERE status = 'scheduled' AND send_at <= ?", (now_str,), fetch=True)
    
    for r in reminders:
        try:
            # Подстановка переменных (пример)
            user = db_query("SELECT full_name FROM users WHERE user_id = ?", (r['user_id'],), fetch=True)
            name = user[0]['full_name'] if user else "Клиент"
            text = r['text'].replace("{имя}", name)
            
            await bot.send_message(r['user_id'], f"🔔 **Напоминание**\n\n{text}", parse_mode=ParseMode.MARKDOWN)
            
            # Помечаем как отправленное
            db_query("UPDATE reminders SET status = 'sent' WHERE id = ?", (r['id'],))
            # Логируем
            db_query("INSERT INTO messages (user_id, text, direction, timestamp) VALUES (?, ?, 'out', ?)", 
                     (r['user_id'], text, now_str))
        except Exception as e:
            logging.error(f"Ошибка отправки напоминания {r['id']}: {e}")
            db_query("UPDATE reminders SET status = 'error' WHERE id = ?", (r['id'],))

# ==========================================
# ЗАПУСК
# ==========================================
async def main():
    init_db()
    # Запускаем проверку напоминаний каждую минуту
    scheduler.add_job(check_reminders, 'interval', minutes=1)
    scheduler.start()
    
    print("🚀 Бот 'Призыва Нет' успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
