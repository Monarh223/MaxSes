import asyncio
import logging
import re
import io
import os
import sqlite3
import shutil
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    Message, CallbackQuery, FSInputFile, BufferedInputFile
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import Command
from dotenv import load_dotenv
import qrcode

# ============ .ENV ЗАГРУЗКА ============
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "0").split(",")))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env")

# ============ ЛОГГЕР ============
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============ БОТ И ДИСПЕТЧЕР ============
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# ============ ПУТИ ============
DB_PATH = "esim_bot.db"
BACKUP_DIR = "backups"

if not os.path.exists(BACKUP_DIR):
    os.makedirs(BACKUP_DIR)

# ============ БАЗА ДАННЫХ ============
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            rank TEXT DEFAULT 'Старт',
            bonus REAL DEFAULT 0.0,
            priority REAL DEFAULT 0.5,
            qr_month INTEGER DEFAULT 0,
            total_qr INTEGER DEFAULT 0,
            balance REAL DEFAULT 0.0,
            hold_balance REAL DEFAULT 0.0,
            joined TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operator TEXT,
            price REAL,
            mode TEXT DEFAULT 'БХ',
            status TEXT DEFAULT 'active',
            executor_id INTEGER,
            phone TEXT,
            qr_file_id TEXT,
            channel_msg_id INTEGER,
            group_id TEXT,
            group_thread_id INTEGER,
            requester_id INTEGER,
            report_msg_id INTEGER,
            created TEXT DEFAULT CURRENT_TIMESTAMP,
            taken TEXT,
            done TEXT,
            hold_until TEXT,
            blocked INTEGER DEFAULT 0
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS operators (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            price REAL,
            emoji TEXT DEFAULT '📱',
            active INTEGER DEFAULT 1
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT UNIQUE,
            username TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT UNIQUE,
            username TEXT,
            active INTEGER DEFAULT 0
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referral_id INTEGER UNIQUE,
            created TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Дефолтные операторы
    defaults = [
        ('Билайн', 12, '⚙️'),
        ('МТС', 14, '🔴'),
        ('Мегафон', 10, '🟢'),
        ('Т2', 10, '⚪'),
        ('Сбер', 10, '🟡'),
        ('Газпром', 20, '🔵'),
        ('Добросвязь', 14, '🟣'),
    ]
    for name, price, emoji in defaults:
        c.execute('INSERT OR IGNORE INTO operators (name, price, emoji) VALUES (?, ?, ?)',
                  (name, price, emoji))

    c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', ('auto_backup', 'off'))
    c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', ('default_mode', 'БХ'))
    c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', ('hold_hours', '4'))

    conn.commit()
    conn.close()

init_db()

# ============ FSM СОСТОЯНИЯ ============
class EsimUpload(StatesGroup):
    waiting_for_qr = State()
    waiting_for_phone = State()

class AdminStates(StatesGroup):
    waiting_for_channel = State()
    waiting_for_operator_name = State()
    waiting_for_operator_price = State()
    waiting_for_operator_emoji = State()
    waiting_for_db_file = State()
    waiting_for_broadcast = State()
    waiting_for_edit_price = State()
    waiting_for_hold_hours = State()

# ============ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ============
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def format_phone(phone: str) -> Optional[str]:
    digits = re.sub(r'\D', '', phone)
    if len(digits) == 11 and digits[0] in ['7', '8']:
        return f"+7{digits[1:]}"
    elif len(digits) == 10 and digits[0] == '9':
        return f"+7{digits}"
    return None

def get_user(user_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    return row

def ensure_user(user_id: int, username: str = None, first_name: str = None):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,))
    if not c.fetchone():
        c.execute('INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)',
                  (user_id, username, first_name))
        conn.commit()
    elif username:
        c.execute('UPDATE users SET username = ?, first_name = ? WHERE user_id = ?',
                  (username, first_name, user_id))
        conn.commit()
    conn.close()

def get_active_channels():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM channels')
    rows = c.fetchall()
    conn.close()
    return rows

def get_operators():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM operators WHERE active = 1 ORDER BY id')
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_operators():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM operators ORDER BY id')
    rows = c.fetchall()
    conn.close()
    return rows

def get_setting(key: str) -> str:
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT value FROM settings WHERE key = ?', (key,))
    row = c.fetchone()
    conn.close()
    return row['value'] if row else ''

def set_setting(key: str, value: str):
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?',
              (key, value, value))
    conn.commit()
    conn.close()

def ensure_group(group_id: str, username: str = None):
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO groups (group_id, username) VALUES (?, ?)', (group_id, username))
    conn.commit()
    conn.close()

def set_group_active(group_id: str, active: bool):
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE groups SET active = ? WHERE group_id = ?', (1 if active else 0, group_id))
    conn.commit()
    conn.close()

# ============ КЛАВИАТУРЫ ============
def main_menu():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
         InlineKeyboardButton(text="📋 Мои номера", callback_data="my_numbers")],
        [InlineKeyboardButton(text="📊 Цены", callback_data="operators_list"),
         InlineKeyboardButton(text="👥 Рефералы", callback_data="referral")],
        [InlineKeyboardButton(text="ℹ️ Помощь", callback_data="help")],
    ])
    return kb

def admin_menu():
    auto = get_setting('auto_backup')
    auto_text = f"💾 Автовыгрузка [{auto.upper()}]"
    mode = get_setting('default_mode')
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Операторы", callback_data="admin_operators"),
         InlineKeyboardButton(text="📢 Каналы", callback_data="admin_channels")],
        [InlineKeyboardButton(text="👥 Группы", callback_data="admin_groups"),
         InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="👤 Участники", callback_data="admin_users"),
         InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="📱 Создать заявку", callback_data="admin_create_order"),
         InlineKeyboardButton(text=f"🎯 Режим: {mode}", callback_data="admin_mode")],
        [InlineKeyboardButton(text="⏳ Холд (часы)", callback_data="admin_hold")],
        [InlineKeyboardButton(text="💾 Выгрузка БД", callback_data="admin_db_export"),
         InlineKeyboardButton(text="💾 Загрузка БД", callback_data="admin_db_import")],
        [InlineKeyboardButton(text=auto_text, callback_data="admin_db_auto")],
        [InlineKeyboardButton(text="🔙 Закрыть", callback_data="close")],
    ])
    return kb

def operators_keyboard():
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    ops = get_operators()
    for op in ops:
        kb.inline_keyboard.append([
            InlineKeyboardButton(
                text=f"{op['emoji']} {op['name']} · {op['price']}$",
                callback_data=f"order_op_{op['id']}"
            )
        ])
    kb.inline_keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")])
    return kb

def back_to_admin():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]
    ])

def back_to_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")]
    ])

# ============ /start ============
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    args = message.text.split()

    # Реферальная ссылка
    if len(args) > 1 and args[1].startswith("ref_"):
        referrer_id = int(args[1].replace("ref_", ""))
        if referrer_id != message.from_user.id:
            conn = get_db()
            c = conn.cursor()
            c.execute('INSERT OR IGNORE INTO referrals (referrer_id, referral_id) VALUES (?, ?)',
                      (referrer_id, message.from_user.id))
            conn.commit()
            conn.close()

    # Заказ
    if len(args) > 1 and args[1].startswith("order_"):
        order_id = int(args[1].replace("order_", ""))
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM orders WHERE id = ?', (order_id,))
        order = c.fetchone()

        if not order:
            await message.answer("❌ Заказ не найден.")
            conn.close()
            return
        if order['status'] != 'active':
            await message.answer("❌ Заказ уже занят.")
            conn.close()
            return

        c.execute('UPDATE orders SET status = ?, executor_id = ?, taken = ? WHERE id = ?',
                  ('taken', message.from_user.id, datetime.now().isoformat(), order_id))
        conn.commit()
        conn.close()

        # Удалить сообщение из канала
        if order['channel_msg_id']:
            channels = get_active_channels()
            for ch in channels:
                try:
                    await bot.delete_message(chat_id=ch['channel_id'], message_id=order['channel_msg_id'])
                except:
                    pass

        await message.answer(
            f"<b>✅ ЗАКАЗ #{order_id} ПРИНЯТ!</b>\n\n"
            f"📱 {order['operator']} · {order['price']}$\n\n"
            f"<b>Отправьте фото QR-кода и укажите номер.</b>"
        )
        await state.set_state(EsimUpload.waiting_for_qr)
        await state.update_data(order_id=order_id)
        return

    await message.answer(
        "<b>🚀 ERWINS ESIM BOT</b>\n\nВыберите действие:",
        reply_markup=main_menu()
    )

# ============ /emjid ============
@dp.message(Command("emjid"))
async def cmd_emjid(message: Message):
    text = message.text.replace('/emjid', '').strip()
    if not text:
        await message.answer("Отправьте эмодзи после команды: <code>/emjid 🚀</code>")
        return
    emoji = text[0] if text else ''
    code = hex(ord(emoji)) if emoji else ''
    await message.answer(
        f"<b>Эмодзи:</b> {emoji}\n"
        f"<b>Unicode:</b> <code>U+{code[2:].upper()}</code>\n"
        f"<b>HTML:</b> <code>&amp;#x{code[2:]};</code>"
    )

# ============ /admin ============
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещён")
        return
    await message.answer("<b>🛠️ АДМИН-ПАНЕЛЬ</b>", reply_markup=admin_menu())

# ============ /work (для групп) ============
@dp.message(Command("work"))
async def cmd_work(message: Message):
    if message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await message.answer("Эта команда только для групп")
        return

    group_id = str(message.chat.id)
    username = message.chat.username

    # Автоматически добавляем группу, если её нет
    ensure_group(group_id, username)

    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT active FROM groups WHERE group_id = ?', (group_id,))
    row = c.fetchone()
    current = row['active'] if row else 0
    new_status = 0 if current == 1 else 1
    set_group_active(group_id, new_status == 1)
    conn.close()

    if new_status:
        await message.answer("✅ <b>Бот активирован в группе!</b>\nИспользуйте /esim для запроса.")
    else:
        await message.answer("⏸️ <b>Бот отключён в группе.</b>")

# ============ /esim (в группе) ============
@dp.message(Command("esim"))
async def cmd_esim(message: Message):
    if message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await message.answer("Эта команда только для групп")
        return

    group_id = str(message.chat.id)
    ensure_group(group_id, message.chat.username)

    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT active FROM groups WHERE group_id = ?', (group_id,))
    row = c.fetchone()
    if not row or row['active'] == 0:
        conn.close()
        await message.answer("⏸️ Бот не активен. /work для включения.")
        return
    conn.close()

    ops = get_operators()
    if not ops:
        await message.answer("Нет доступных операторов.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for op in ops:
        kb.inline_keyboard.append([
            InlineKeyboardButton(
                text=f"{op['emoji']} {op['name']} · {op['price']}$",
                callback_data=f"group_req_{op['id']}"
            )
        ])

    await message.answer("<b>📱 ВЫБЕРИТЕ ОПЕРАТОРА</b>", reply_markup=kb)

# ============ ЗАПРОС ИЗ ГРУППЫ ============
@dp.callback_query(F.data.startswith("group_req_"))
async def group_request(callback: CallbackQuery):
    op_id = int(callback.data.split("_")[2])
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM operators WHERE id = ?', (op_id,))
    op = c.fetchone()
    conn.close()

    if not op:
        await callback.answer("Оператор не найден")
        return

    channels = get_active_channels()
    if not channels:
        await callback.answer("Нет каналов для заявок")
        return

    mode = get_setting('default_mode')
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT INTO orders (operator, price, mode, status, group_id, requester_id) VALUES (?, ?, ?, ?, ?, ?)',
              (op['name'], op['price'], mode, 'active', str(callback.message.chat.id), callback.from_user.id))
    order_id = c.lastrowid
    conn.commit()
    conn.close()

    bot_username = (await bot.me()).username
    deep_link = f"https://t.me/{bot_username}?start=order_{order_id}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 ЗАБРАТЬ ЗАКАЗ", url=deep_link)]
    ])

    msg_text = (
        f"<b>🔔 НОВЫЙ ЗАКАЗ #{order_id}</b>\n\n"
        f"📱 <b>Оператор:</b> {op['emoji']} {op['name']}\n"
        f"💰 <b>Цена:</b> {op['price']}$\n"
        f"🎯 <b>Режим:</b> {mode}\n"
        f"⏳ <b>Дедлайн:</b> 10 минут\n\n"
        f"<i>Нажмите кнопку чтобы забрать заказ</i>"
    )

    try:
        channel_id = channels[0]['channel_id']
        sent = await bot.send_message(chat_id=channel_id, text=msg_text, reply_markup=kb)
        conn = get_db()
        c = conn.cursor()
        c.execute('UPDATE orders SET channel_msg_id = ? WHERE id = ?',
                  (sent.message_id, order_id))
        conn.commit()
        conn.close()
        await callback.answer("✅ Заявка создана!")
    except Exception as e:
        await callback.answer(f"Ошибка: {e}")

# ============ СДАТЬ ESIM ============
@dp.callback_query(F.data == "sdat_esim")
async def sdat_esim_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("<b>📱 СДАЧА ESIM</b>\n\nОтправьте фото QR-кода.")
    await state.set_state(EsimUpload.waiting_for_qr)
    await state.update_data(order_id=None)
    await callback.answer()

@dp.message(EsimUpload.waiting_for_qr, F.photo)
async def esim_qr_received(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get('order_id')
    file_id = message.photo[-1].file_id
    caption = message.caption

    if caption:
        phone = format_phone(caption)
        if phone:
            await save_esim(message, state, file_id, phone, order_id)
            return

    await state.update_data(qr_file_id=file_id)
    await state.set_state(EsimUpload.waiting_for_phone)
    await message.answer("📱 Укажите номер телефона (+7XXXXXXXXXX или 8XXXXXXXXXX)")

@dp.message(EsimUpload.waiting_for_phone)
async def esim_phone_received(message: Message, state: FSMContext):
    phone = format_phone(message.text)
    if not phone:
        await message.answer("❌ Неверный формат.")
        return
    data = await state.get_data()
    file_id = data.get('qr_file_id')
    order_id = data.get('order_id')
    await save_esim(message, state, file_id, phone, order_id)

async def save_esim(message: Message, state: FSMContext, file_id: str, phone: str,
                    order_id: int = None):
    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)

    conn = get_db()
    c = conn.cursor()

    if order_id:
        c.execute('UPDATE orders SET status = ?, phone = ?, qr_file_id = ?, done = ? WHERE id = ?',
                  ('done', phone, file_id, datetime.now().isoformat(), order_id))
        c.execute('SELECT * FROM orders WHERE id = ?', (order_id,))
        order = c.fetchone()

        c.execute('UPDATE users SET qr_month = qr_month + 1, total_qr = total_qr + 1 WHERE user_id = ?',
                  (user_id,))
        c.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        user = c.fetchone()
        conn.commit()
        conn.close()

        # Отправляем результат в группу тому кто запросил
        if order['group_id'] and order['requester_id']:
            mode = order['mode']
            is_blocked = order['blocked']

            result_text = (
                f"<b>✅ ЗАКАЗ #{order_id} ВЫПОЛНЕН</b>\n\n"
                f"📱 <b>Оператор:</b> {order['operator']}\n"
                f"📞 <b>Номер:</b> <code>{phone}</code>\n"
                f"👤 <b>Сдал:</b> @{message.from_user.username or user_id}\n"
                f"🎯 <b>Режим:</b> {mode}"
            )

            status_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Встал", callback_data=f"status_ok_{order_id}"),
                 InlineKeyboardButton(text="🚫 Блок", callback_data=f"status_block_{order_id}")],
                [InlineKeyboardButton(text="❌ НеСкан", callback_data=f"status_noscan_{order_id}")],
            ])

            try:
                # Отправляем фото + текст
                sent = await bot.send_photo(
                    chat_id=order['group_id'],
                    photo=file_id,
                    caption=result_text,
                    reply_markup=status_kb
                )
                # Сохраняем ID сообщения с кнопками
                c2 = get_db().cursor()
                conn2 = get_db()
                c2.execute('UPDATE orders SET report_msg_id = ? WHERE id = ?',
                          (sent.message_id, order_id))
                conn2.commit()
                conn2.close()
            except Exception as e:
                logger.error(f"Не удалось отправить в группу: {e}")
        else:
            conn.close()

        await message.answer(
            f"<b>✅ ESIM СДАН!</b>\n\n"
            f"📱 <b>Номер:</b> <code>{phone}</code>\n"
            f"📊 <b>QR за месяц:</b> {user['qr_month'] if user else 0}"
        )
    else:
        c.execute('INSERT INTO orders (operator, price, mode, status, executor_id, phone, qr_file_id, done) '
                  'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                  ('Неизвестно', 0, get_setting('default_mode'), 'done', user_id, phone, file_id,
                   datetime.now().isoformat()))
        c.execute('UPDATE users SET qr_month = qr_month + 1, total_qr = total_qr + 1 WHERE user_id = ?',
                  (user_id,))
        conn.commit()
        conn.close()
        await message.answer(f"<b>✅ ESIM СДАН!</b>\n\n📱 <code>{phone}</code>")

    await state.clear()

# ============ КНОПКИ СТАТУСА (Встал/Блок/НеСкан) ============
@dp.callback_query(F.data.startswith("status_"))
async def status_handler(callback: CallbackQuery):
    parts = callback.data.split("_")
    action = parts[1]  # ok, block, noscan
    order_id = int(parts[2])

    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM orders WHERE id = ?', (order_id,))
    order = c.fetchone()

    if not order:
        await callback.answer("Заказ не найден")
        conn.close()
        return

    if action == "ok":
        # Встал
        c.execute('UPDATE orders SET blocked = 0 WHERE id = ?', (order_id,))
        conn.commit()

        if order['mode'] == 'БХ':
            # Без холда — сразу кнопка Оплатить
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💵 Оплатить", callback_data=f"pay_{order_id}")],
            ])
            await callback.message.edit_reply_markup(reply_markup=kb)
            await callback.answer("✅ Статус: Встал. Нажмите Оплатить для выплаты.")
        else:
            # ХД — с холдом
            hold_hours = int(get_setting('hold_hours') or 4)
            hold_until = datetime.now() + timedelta(hours=hold_hours)
            c.execute('UPDATE orders SET hold_until = ? WHERE id = ?',
                      (hold_until.isoformat(), order_id))
            conn.commit()

            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.edit_caption(
                callback.message.caption + f"\n\n⏳ <b>Холд до:</b> {hold_until.strftime('%H:%M %d.%m')}"
            )
            await callback.answer(f"⏳ Холд {hold_hours}ч. Средства поступят на баланс после.")

    elif action == "block":
        # Блок
        c.execute('UPDATE orders SET blocked = 1 WHERE id = ?', (order_id,))
        conn.commit()
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.edit_caption(callback.message.caption + "\n\n🚫 <b>ЗАБЛОКИРОВАНО</b>")
        await callback.answer("🚫 Номер заблокирован.")

    elif action == "noscan":
        # НеСкан
        c.execute('UPDATE orders SET blocked = 1 WHERE id = ?', (order_id,))
        conn.commit()
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.edit_caption(callback.message.caption + "\n\n❌ <b>НЕСКАН</b>")
        await callback.answer("❌ НеСкан.")

    conn.close()

# ============ КНОПКА ОПЛАТИТЬ (БХ) ============
@dp.callback_query(F.data.startswith("pay_"))
async def pay_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только админ может оплатить!")
        return

    order_id = int(callback.data.split("_")[1])
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM orders WHERE id = ?', (order_id,))
    order = c.fetchone()

    if not order:
        await callback.answer("Заказ не найден")
        conn.close()
        return

    # Начисляем баланс исполнителю
    if order['executor_id']:
        c.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?',
                  (order['price'], order['executor_id']))
        # Уведомляем исполнителя
        try:
            await bot.send_message(
                order['executor_id'],
                f"<b>💵 ВЫПЛАТА</b>\n\n"
                f"📱 Заказ #{order_id} · {order['operator']}\n"
                f"💰 Сумма: <b>{order['price']}$</b>\n"
                f"🎯 Режим: {order['mode']}\n\n"
                f"Средства зачислены на баланс."
            )
        except:
            pass

    conn.commit()
    conn.close()

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.edit_caption(
        callback.message.caption + f"\n\n💵 <b>ОПЛАЧЕНО · {order['price']}$</b>"
    )
    await callback.answer(f"✅ Выплачено {order['price']}$")

# ============ АВТО-ВЫПЛАТА ПО ХОЛДУ ============
async def hold_checker():
    while True:
        await asyncio.sleep(60)
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM orders WHERE status = 'done' AND mode = 'ХД' AND hold_until IS NOT NULL AND blocked = 0")
        orders = c.fetchall()
        for order in orders:
            hold_until = datetime.fromisoformat(order['hold_until'])
            if datetime.now() >= hold_until:
                if order['executor_id']:
                    c.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?',
                              (order['price'], order['executor_id']))
                    try:
                        await bot.send_message(
                            order['executor_id'],
                            f"<b>💵 ВЫПЛАТА (холд)</b>\n\n"
                            f"📱 Заказ #{order['id']} · {order['operator']}\n"
                            f"💰 Сумма: <b>{order['price']}$</b>\n"
                            f"Средства зачислены на баланс."
                        )
                    except:
                        pass
                c.execute('UPDATE orders SET hold_until = NULL WHERE id = ?', (order['id'],))
        conn.commit()
        conn.close()

# ============ АДМИН: КАНАЛЫ ============
@dp.callback_query(F.data == "admin_channels")
async def admin_channels(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    channels = get_active_channels()
    text = "<b>📢 КАНАЛЫ</b>\n\n"
    if channels:
        for ch in channels:
            text += f"• {ch['username'] or ch['channel_id']}\n"
    else:
        text += "<i>Нет каналов</i>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data="admin_add_channel")],
        [InlineKeyboardButton(text="🗑️ Удалить все", callback_data="admin_del_channel")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "admin_add_channel")
async def admin_add_channel(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("Перешлите сообщение из канала или @username:")
    await state.set_state(AdminStates.waiting_for_channel)
    await callback.answer()

@dp.message(AdminStates.waiting_for_channel)
async def channel_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    channel_id = None
    username = None
    if message.forward_from_chat:
        channel_id = str(message.forward_from_chat.id)
        username = message.forward_from_chat.username
    elif message.text and message.text.startswith('@'):
        username = message.text.strip()
        try:
            chat = await bot.get_chat(username)
            channel_id = str(chat.id)
        except:
            await message.answer("❌ Не удалось найти канал.")
            return
    else:
        await message.answer("Перешлите сообщение или @username.")
        return
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO channels (channel_id, username) VALUES (?, ?)',
              (channel_id, username))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Канал добавлен!")
    await state.clear()

@dp.callback_query(F.data == "admin_del_channel")
async def admin_del_channel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM channels')
    conn.commit()
    conn.close()
    await callback.answer("Каналы удалены.")
    await callback.message.edit_text("<b>📢 КАНАЛЫ</b>\n\n<i>Удалены.</i>", reply_markup=back_to_admin())

# ============ АДМИН: ГРУППЫ ============
@dp.callback_query(F.data == "admin_groups")
async def admin_groups(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM groups')
    groups = c.fetchall()
    conn.close()
    text = "<b>👥 ГРУППЫ</b>\n\n"
    for g in groups:
        status = "🟢" if g['active'] else "🔴"
        text += f"{status} {g['username'] or g['group_id']}\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑️ Удалить все", callback_data="admin_del_groups")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "admin_del_groups")
async def admin_del_groups(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM groups')
    conn.commit()
    conn.close()
    await callback.answer("Группы удалены.")
    await callback.message.edit_text("<b>👥 ГРУППЫ</b>\n\n<i>Удалены.</i>", reply_markup=back_to_admin())

# ============ АДМИН: ОПЕРАТОРЫ ============
@dp.callback_query(F.data == "admin_operators")
async def admin_operators(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    ops = get_all_operators()
    text = "<b>💰 ОПЕРАТОРЫ</b>\n\n"
    for op in ops:
        s = "🟢" if op['active'] else "🔴"
        text += f"{s} {op['emoji']} {op['name']} · {op['price']}$\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data="admin_add_op"),
         InlineKeyboardButton(text="✏️ Цена", callback_data="admin_edit_op")],
        [InlineKeyboardButton(text="🔄 Вкл/Выкл", callback_data="admin_toggle_op"),
         InlineKeyboardButton(text="🗑️ Удалить", callback_data="admin_del_op")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "admin_add_op")
async def admin_add_op(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("Название оператора:")
    await state.set_state(AdminStates.waiting_for_operator_name)
    await callback.answer()

@dp.message(AdminStates.waiting_for_operator_name)
async def op_name(message: Message, state: FSMContext):
    await state.update_data(op_name=message.text)
    await message.answer("Цена ($):")
    await state.set_state(AdminStates.waiting_for_operator_price)

@dp.message(AdminStates.waiting_for_operator_price)
async def op_price(message: Message, state: FSMContext):
    try:
        float(message.text)
    except:
        await message.answer("Число!")
        return
    await state.update_data(op_price=message.text)
    await message.answer("Эмодзи:")
    await state.set_state(AdminStates.waiting_for_operator_emoji)

@dp.message(AdminStates.waiting_for_operator_emoji)
async def op_emoji(message: Message, state: FSMContext):
    data = await state.get_data()
    emoji = message.text.strip()[0] if message.text else '📱'
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO operators (name, price, emoji) VALUES (?, ?, ?)',
              (data['op_name'], float(data['op_price']), emoji))
    conn.commit()
    conn.close()
    await message.answer(f"✅ {emoji} {data['op_name']} · {data['op_price']}$")
    await state.clear()

@dp.callback_query(F.data == "admin_edit_op")
async def admin_edit_op(callback: CallbackQuery, state: FSMContext):
    ops = get_all_operators()
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for op in ops:
        kb.inline_keyboard.append([InlineKeyboardButton(
            text=f"{op['emoji']} {op['name']} · {op['price']}$",
            callback_data=f"editop_{op['id']}"
        )])
    kb.inline_keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")])
    await callback.message.edit_text("Выберите оператора:", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("editop_"))
async def editop(callback: CallbackQuery, state: FSMContext):
    op_id = int(callback.data.split("_")[1])
    await state.update_data(edit_op_id=op_id)
    await callback.message.edit_text("Новая цена:")
    await state.set_state(AdminStates.waiting_for_edit_price)
    await callback.answer()

@dp.message(AdminStates.waiting_for_edit_price)
async def edit_price_done(message: Message, state: FSMContext):
    try:
        price = float(message.text)
    except:
        await message.answer("Число!")
        return
    data = await state.get_data()
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE operators SET price = ? WHERE id = ?', (price, data['edit_op_id']))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Цена → {price}$")
    await state.clear()

@dp.callback_query(F.data == "admin_toggle_op")
async def toggle_op(callback: CallbackQuery):
    ops = get_all_operators()
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for op in ops:
        s = "🟢" if op['active'] else "🔴"
        kb.inline_keyboard.append([InlineKeyboardButton(
            text=f"{s} {op['emoji']} {op['name']}",
            callback_data=f"toggleop_{op['id']}"
        )])
    kb.inline_keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")])
    await callback.message.edit_text("Вкл/Выкл:", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("toggleop_"))
async def toggleop_done(callback: CallbackQuery):
    op_id = int(callback.data.split("_")[1])
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT active FROM operators WHERE id = ?', (op_id,))
    row = c.fetchone()
    new = 0 if row['active'] == 1 else 1
    c.execute('UPDATE operators SET active = ? WHERE id = ?', (new, op_id))
    conn.commit()
    conn.close()
    await callback.answer(f"{'🟢 Вкл' if new else '🔴 Выкл'}")
    await toggle_op(callback)

@dp.callback_query(F.data == "admin_del_op")
async def del_op(callback: CallbackQuery):
    ops = get_all_operators()
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for op in ops:
        kb.inline_keyboard.append([InlineKeyboardButton(
            text=f"🗑️ {op['emoji']} {op['name']}",
            callback_data=f"delop_{op['id']}"
        )])
    kb.inline_keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")])
    await callback.message.edit_text("Удалить:", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("delop_"))
async def delop_done(callback: CallbackQuery):
    op_id = int(callback.data.split("_")[1])
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM operators WHERE id = ?', (op_id,))
    conn.commit()
    conn.close()
    await callback.answer("Удалён.")
    await del_op(callback)

# ============ АДМИН: РЕЖИМ / ХОЛД ============
@dp.callback_query(F.data == "admin_mode")
async def admin_mode(callback: CallbackQuery):
    current = get_setting('default_mode')
    text = f"<b>🎯 РЕЖИМ СДАЧИ</b>\n\nТекущий: <b>{current}</b>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 БХ", callback_data="set_mode_БХ"),
         InlineKeyboardButton(text="🟡 ХД", callback_data="set_mode_ХД")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("set_mode_"))
async def set_mode(callback: CallbackQuery):
    mode = callback.data.split("_")[2]
    set_setting('default_mode', mode)
    await callback.answer(f"Режим: {mode}")
    await admin_mode(callback)

@dp.callback_query(F.data == "admin_hold")
async def admin_hold(callback: CallbackQuery, state: FSMContext):
    current = get_setting('hold_hours') or '4'
    await callback.message.edit_text(f"<b>⏳ ХОЛД (часы)</b>\n\nТекущий: <b>{current}ч</b>\n\nВведите новое значение:")
    await state.set_state(AdminStates.waiting_for_hold_hours)
    await callback.answer()

@dp.message(AdminStates.waiting_for_hold_hours)
async def hold_hours_set(message: Message, state: FSMContext):
    try:
        hours = int(message.text)
    except:
        await message.answer("Число!")
        return
    set_setting('hold_hours', str(hours))
    await message.answer(f"✅ Холд: {hours}ч")
    await state.clear()

# ============ АДМИН: БД ============
@dp.callback_query(F.data == "admin_db_export")
async def admin_db_export(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    try:
        await callback.message.answer_document(FSInputFile(DB_PATH), caption="📦 БД")
        await callback.answer("✅")
    except Exception as e:
        await callback.answer(f"Ошибка: {e}")

@dp.callback_query(F.data == "admin_db_import")
async def admin_db_import(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("Отправьте .db файл:")
    await state.set_state(AdminStates.waiting_for_db_file)
    await callback.answer()

@dp.message(AdminStates.waiting_for_db_file, F.document)
async def db_file(message: Message, state: FSMContext):
    if not message.document.file_name.endswith('.db'):
        await message.answer(".db!")
        return
    try:
        await bot.download(message.document, destination=DB_PATH)
        init_db()
        await message.answer("✅ БД заменена.")
    except Exception as e:
        await message.answer(f"❌ {e}")
    await state.clear()

@dp.callback_query(F.data == "admin_db_auto")
async def admin_db_auto(callback: CallbackQuery):
    cur = get_setting('auto_backup')
    new = 'off' if cur == 'on' else 'on'
    set_setting('auto_backup', new)
    await callback.answer(f"Авто: {new.upper()}")
    await callback.message.edit_reply_markup(reply_markup=admin_menu())

# ============ АДМИН: СОЗДАТЬ ЗАЯВКУ ============
@dp.callback_query(F.data == "admin_create_order")
async def admin_create_order(callback: CallbackQuery):
    await callback.message.edit_text("<b>📱 ВЫБЕРИТЕ ОПЕРАТОРА</b>", reply_markup=operators_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("order_op_"))
async def admin_order_created(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    op_id = int(callback.data.split("_")[2])
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM operators WHERE id = ?', (op_id,))
    op = c.fetchone()
    channels = get_active_channels()
    if not channels:
        await callback.answer("Нет каналов")
        conn.close()
        return
    mode = get_setting('default_mode')
    c.execute('INSERT INTO orders (operator, price, mode, status, requester_id) VALUES (?, ?, ?, ?, ?)',
              (op['name'], op['price'], mode, 'active', callback.from_user.id))
    order_id = c.lastrowid
    conn.commit()
    conn.close()

    bot_username = (await bot.me()).username
    deep_link = f"https://t.me/{bot_username}?start=order_{order_id}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 ЗАБРАТЬ ЗАКАЗ", url=deep_link)]
    ])
    msg_text = (
        f"<b>🔔 НОВЫЙ ЗАКАЗ #{order_id}</b>\n\n"
        f"📱 {op['emoji']} {op['name']} · {op['price']}$\n"
        f"🎯 {mode}\n⏳ 10 минут"
    )
    try:
        channel_id = channels[0]['channel_id']
        sent = await bot.send_message(chat_id=channel_id, text=msg_text, reply_markup=kb)
        conn = get_db()
        c = conn.cursor()
        c.execute('UPDATE orders SET channel_msg_id = ? WHERE id = ?', (sent.message_id, order_id))
        conn.commit()
        conn.close()
        await callback.answer("✅")
        await callback.message.edit_text(f"✅ Заявка #{order_id}")
    except Exception as e:
        await callback.answer(f"Ошибка: {e}")

# ============ АДМИН: СТАТИСТИКА ============
@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM users')
    users = c.fetchone()[0]
    c.execute('SELECT COUNT(*), SUM(price) FROM orders WHERE status = "done"')
    done, total = c.fetchone()
    conn.close()
    await callback.message.edit_text(
        f"<b>📊 СТАТИСТИКА</b>\n\n👥 {users}\n✅ {done or 0}\n💵 {total or 0}$",
        reply_markup=back_to_admin()
    )
    await callback.answer()

# ============ АДМИН: УЧАСТНИКИ / РАССЫЛКА ============
@dp.callback_query(F.data == "admin_users")
async def admin_users(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM users ORDER BY total_qr DESC LIMIT 50')
    users = c.fetchall()
    conn.close()
    text = "<b>👤 УЧАСТНИКИ</b>\n\n"
    for u in users:
        name = u['username'] or u['first_name'] or u['user_id']
        text += f"@{name} | {u['rank']} | {u['total_qr']} QR\n"
    await callback.message.edit_text(text, reply_markup=back_to_admin())
    await callback.answer()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("Текст рассылки:")
    await state.set_state(AdminStates.waiting_for_broadcast)
    await callback.answer()

@dp.message(AdminStates.waiting_for_broadcast)
async def broadcast_send(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT user_id FROM users')
    users = c.fetchall()
    conn.close()
    ok = 0
    for u in users:
        try:
            await bot.send_message(u['user_id'], message.text)
            ok += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await message.answer(f"✅ {ok}/{len(users)}")
    await state.clear()

# ============ НАВИГАЦИЯ ============
@dp.callback_query(F.data == "back_main")
async def back_main(callback: CallbackQuery):
    if callback.message.chat.type == ChatType.PRIVATE:
        await callback.message.edit_text("<b>🚀 ERWINS ESIM BOT</b>\n\nВыберите действие:", reply_markup=main_menu())
    await callback.answer()

@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("<b>🛠️ АДМИН-ПАНЕЛЬ</b>", reply_markup=admin_menu())
    await callback.answer()

@dp.callback_query(F.data == "close")
async def close(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except:
        pass
    await callback.answer()

# ============ ПРОФИЛЬ / НОМЕРА / ЦЕНЫ / РЕФЕРАЛЫ / ПОМОЩЬ ============
@dp.callback_query(F.data == "profile")
async def profile(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user:
        ensure_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
        user = get_user(callback.from_user.id)
    text = (
        f"<b>👤 ПРОФИЛЬ</b>\n\n"
        f"🆔 @{user['username'] or user['user_id']}\n"
        f"📊 {user['rank']} · +{user['bonus']}$\n"
        f"📱 QR мес: {user['qr_month']} | всего: {user['total_qr']}\n"
        f"💵 Баланс: {user['balance']}$ | Холд: {user['hold_balance']}$"
    )
    await callback.message.edit_text(text, reply_markup=back_to_main())
    await callback.answer()

@dp.callback_query(F.data == "my_numbers")
async def my_numbers(callback: CallbackQuery):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM orders WHERE executor_id = ? ORDER BY created DESC LIMIT 50',
              (callback.from_user.id,))
    orders = c.fetchall()
    conn.close()
    if not orders:
        text = "<b>📋 МОИ НОМЕРА</b>\n\n<i>Нет</i>"
    else:
        text = "<b>📋 МОИ НОМЕРА</b>\n\n"
        for o in orders:
            s = {'active': '🟡', 'taken': '🔵', 'done': '🟢'}.get(o['status'], '⚪')
            text += f"{s} #{o['id']} {o['operator']} · <code>{o['phone'] or '—'}</code>\n"
    await callback.message.edit_text(text, reply_markup=back_to_main())
    await callback.answer()

@dp.callback_query(F.data == "operators_list")
async def operators_list(callback: CallbackQuery):
    ops = get_operators()
    text = "<b>📊 ЦЕНЫ</b>\n\n"
    for op in ops:
        text += f"{op['emoji']} {op['name']} · {op['price']}$\n"
    await callback.message.edit_text(text, reply_markup=back_to_main())
    await callback.answer()

@dp.callback_query(F.data == "referral")
async def referral(callback: CallbackQuery):
    ensure_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    link = f"https://t.me/{(await bot.me()).username}?start=ref_{callback.from_user.id}"
    user = get_user(callback.from_user.id)
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM referrals WHERE referrer_id = ?', (callback.from_user.id,))
    count = c.fetchone()[0]
    conn.close()
    text = (
        f"<b>👥 РЕФЕРАЛЫ</b>\n\n"
        f"🔗 <code>{link}</code>\n\n"
        f"👥 {count} чел.\n"
        f"📊 {user['rank']} · +{user['bonus']}$"
    )
    await callback.message.edit_text(text, reply_markup=back_to_main())
    await callback.answer()

@dp.callback_query(F.data == "help")
async def help_cmd(callback: CallbackQuery):
    text = (
        "<b>ℹ️ ПОМОЩЬ</b>\n\n"
        "<b>Сдать:</b> фото QR + номер\n"
        "<b>Взять:</b> кнопка в канале или /esim в группе\n"
        "/work — вкл/выкл в группе\n"
        "/emjid — ID эмодзи"
    )
    await callback.message.edit_text(text, reply_markup=back_to_main())
    await callback.answer()

# ============ ПРОЧЕЕ ============
@dp.message(F.text, F.chat.type == ChatType.PRIVATE)
async def unknown_msg(message: Message):
    ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer("<b>🚀 ERWINS ESIM BOT</b>", reply_markup=main_menu())

# ============ АВТОБЭКАП ============
async def auto_backup_task():
    while True:
        await asyncio.sleep(3600)
        if get_setting('auto_backup') == 'on':
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            path = os.path.join(BACKUP_DIR, f'backup_{ts}.db')
            shutil.copy2(DB_PATH, path)
            backups = sorted(os.listdir(BACKUP_DIR))
            while len(backups) > 48:
                os.remove(os.path.join(BACKUP_DIR, backups.pop(0)))

# ============ ЗАПУСК ============
async def main():
    logger.info("Бот запущен")
    asyncio.create_task(auto_backup_task())
    asyncio.create_task(hold_checker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
```
