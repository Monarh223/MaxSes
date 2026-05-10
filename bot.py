import asyncio
import logging
import re
import io
import os
import sqlite3
import shutil
from datetime import datetime, timedelta
from typing import Optional
import pytz

from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    Message, CallbackQuery, FSInputFile
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import Command
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "0").split(",")))
SEND_USERNAME = os.getenv("SEND_USERNAME", "")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

DB_PATH = "esim_bot.db"
BACKUP_DIR = "backups"
HOLD_HOURS = 2
MOSCOW_TZ = pytz.timezone("Europe/Moscow")
MIN_WITHDRAW = 10.0
SUBMIT_TIMEOUT = 300  # 5 минут на сдачу
MAX_WARNINGS = 3
BLOCK_HOURS = 1

if not os.path.exists(BACKUP_DIR):
    os.makedirs(BACKUP_DIR)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
        rank TEXT DEFAULT 'Старт', bonus REAL DEFAULT 0.0, priority REAL DEFAULT 0.5,
        qr_month INTEGER DEFAULT 0, total_qr INTEGER DEFAULT 0,
        balance REAL DEFAULT 0.0, expected_balance REAL DEFAULT 0.0,
        pending_balance REAL DEFAULT 0.0, joined TEXT DEFAULT CURRENT_TIMESTAMP,
        warnings INTEGER DEFAULT 0, blocked_until TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, operator TEXT, price REAL,
        mode TEXT DEFAULT 'БХ', status TEXT DEFAULT 'active', executor_id INTEGER,
        phone TEXT, qr_file_id TEXT, channel_msg_id INTEGER, group_id TEXT,
        group_thread_id INTEGER, requester_id INTEGER,
        created TEXT DEFAULT CURRENT_TIMESTAMP, taken TEXT, done TEXT,
        hold_until TEXT, paid INTEGER DEFAULT 0, credited INTEGER DEFAULT 0,
        blocked INTEGER DEFAULT 0, noscan INTEGER DEFAULT 0,
        taken_at TEXT, order_group_msg_id INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS operators (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, 
        price_bh REAL DEFAULT 0, price_hd REAL DEFAULT 0,
        emoji TEXT DEFAULT '📱', active INTEGER DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id TEXT UNIQUE, username TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT UNIQUE, username TEXT,
        active INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER,
        referral_id INTEGER UNIQUE, created TEXT DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS phone_submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT, user_id INTEGER,
        order_id INTEGER, submitted TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS balance_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL,
        type TEXT, description TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS withdraw_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL,
        status TEXT DEFAULT 'pending', channel_msg_id INTEGER,
        created TEXT DEFAULT CURRENT_TIMESTAMP)''')

    defaults = [
        ('Билайн', 12, 10, '⚙️'), ('МТС', 14, 12, '🔴'), ('Мегафон', 10, 8, '🟢'),
        ('Т2', 10, 8, '⚪'), ('Сбер', 10, 8, '🟡'), ('Газпром', 20, 18, '🔵'), ('Добросвязь', 14, 12, '🟣'),
    ]
    for name, bh, hd, emoji in defaults:
        c.execute('INSERT OR IGNORE INTO operators (name, price_bh, price_hd, emoji) VALUES (?, ?, ?, ?)', (name, bh, hd, emoji))

    c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', ('auto_backup', 'off'))
    c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', ('default_mode', 'БХ'))
    c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', ('work_day', 'on'))
    conn.commit()
    conn.close()

init_db()

class EsimUpload(StatesGroup):
    waiting_for_qr = State()
    waiting_for_phone = State()

class AdminStates(StatesGroup):
    waiting_for_channel = State()
    waiting_for_operator_name = State()
    waiting_for_operator_bh = State()
    waiting_for_operator_hd = State()
    waiting_for_operator_emoji = State()
    waiting_for_db_file = State()
    waiting_for_broadcast = State()
    waiting_for_edit_bh = State()
    waiting_for_edit_hd = State()
    waiting_for_pay_user = State()
    waiting_for_pay_amount = State()
    waiting_for_deduct_user = State()
    waiting_for_deduct_amount = State()

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def is_work_day():
    return get_setting('work_day') == 'on'

def is_user_blocked(user_id: int) -> bool:
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT blocked_until FROM users WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    if row and row['blocked_until']:
        if datetime.now().isoformat() < row['blocked_until']:
            return True
    return False

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
        c.execute('INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)', (user_id, username, first_name))
    elif username:
        c.execute('UPDATE users SET username = ?, first_name = ? WHERE user_id = ?', (username, first_name, user_id))
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
    c.execute('INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?', (key, value, value))
    conn.commit()
    conn.close()

def can_submit_phone(phone: str, user_id: int) -> bool:
    conn = get_db()
    c = conn.cursor()
    now_msk = datetime.now(MOSCOW_TZ)
    today_start = now_msk.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    today_end = now_msk.replace(hour=23, minute=59, second=59, microsecond=999999).isoformat()
    c.execute('SELECT COUNT(*) FROM phone_submissions WHERE phone = ? AND submitted BETWEEN ? AND ?', (phone, today_start, today_end))
    if c.fetchone()[0] >= 2:
        conn.close()
        return False
    c.execute('SELECT COUNT(*) FROM phone_submissions WHERE user_id = ? AND submitted BETWEEN ? AND ?', (user_id, today_start, today_end))
    if c.fetchone()[0] >= 5:
        conn.close()
        return False
    conn.close()
    return True

def moscow_time():
    return datetime.now(MOSCOW_TZ)

def confirm_kb(action: str, back_to: str = "admin_back"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, подтверждаю", callback_data=action)],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=back_to)],
    ])

def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
         InlineKeyboardButton(text="📋 Мои номера", callback_data="my_numbers")],
        [InlineKeyboardButton(text="📊 Цены", callback_data="operators_list"),
         InlineKeyboardButton(text="👥 Рефералы", callback_data="referral")],
        [InlineKeyboardButton(text="ℹ️ Помощь", callback_data="help")],
    ])

def admin_menu():
    auto = get_setting('auto_backup')
    wd = get_setting('work_day')
    wd_text = "🟢 Раб.день" if wd == 'on' else "🔴 Завершён"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Операторы", callback_data="admin_operators"),
         InlineKeyboardButton(text="📢 Каналы", callback_data="admin_channels")],
        [InlineKeyboardButton(text="👥 Группы", callback_data="admin_groups"),
         InlineKeyboardButton(text=wd_text, callback_data="admin_workday")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"),
         InlineKeyboardButton(text="👤 Участники", callback_data="admin_users")],
        [InlineKeyboardButton(text="💵 Выплаты", callback_data="admin_payouts"),
         InlineKeyboardButton(text="📱 Создать заявку", callback_data="admin_create_order")],
        [InlineKeyboardButton(text="🗑️ Удалить заявки", callback_data="admin_delete_orders"),
         InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="💾 БД", callback_data="admin_db_menu")],
        [InlineKeyboardButton(text="🔙 Закрыть", callback_data="close")],
    ])

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    args = message.text.split()

    if len(args) > 1 and args[1].startswith("ref_"):
        referrer_id = int(args[1].replace("ref_", ""))
        if referrer_id != message.from_user.id:
            conn = get_db()
            c = conn.cursor()
            c.execute('INSERT OR IGNORE INTO referrals (referrer_id, referral_id) VALUES (?, ?)', (referrer_id, message.from_user.id))
            conn.commit()
            conn.close()

    if len(args) > 1 and args[1].startswith("order_"):
        if not is_work_day():
            await message.answer("🔴 <b>Рабочий день завершён.</b>")
            return
        if is_user_blocked(message.from_user.id):
            await message.answer("🚫 <b>Вы временно заблокированы.</b>")
            return

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
        if order['executor_id'] == message.from_user.id:
            await message.answer("❌ Вы не можете взять свою же заявку повторно.")
            conn.close()
            return

        c.execute('UPDATE orders SET status = ?, executor_id = ?, taken = ?, taken_at = ? WHERE id = ?',
                  ('taken', message.from_user.id, datetime.now().isoformat(), datetime.now().isoformat(), order_id))
        conn.commit()
        conn.close()

        channels = get_active_channels()
        for ch in channels:
            try:
                await bot.delete_message(chat_id=ch['channel_id'], message_id=order['channel_msg_id'])
            except:
                pass

        await message.answer(f"<b>✅ ЗАКАЗ #{order_id} ПРИНЯТ!</b>\n\n📱 {order['operator']} · {order['price']}$\n🎯 {order['mode']}\n⏳ <b>5 минут на сдачу!</b>\n\n<b>Отправьте фото QR-кода.</b>")
        await state.set_state(EsimUpload.waiting_for_qr)
        await state.update_data(order_id=order_id)
        return

    await message.answer("<b>🚀 ERWINS ESIM BOT</b>\n\nВыберите действие:", reply_markup=main_menu())

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещён")
        return
    await message.answer("<b>🛠️ АДМИН-ПАНЕЛЬ</b>", reply_markup=admin_menu())

@dp.message(Command("work"))
async def cmd_work(message: Message):
    if message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await message.answer("Эта команда только для групп")
        return
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только админ")
        return
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM groups WHERE group_id = ?', (str(message.chat.id),))
    row = c.fetchone()
    if not row:
        c.execute('INSERT INTO groups (group_id, username, active) VALUES (?, ?, ?)', (str(message.chat.id), message.chat.username or str(message.chat.id), 1))
        conn.commit()
        conn.close()
        await message.answer("✅ <b>Группа добавлена и активирована!</b>\n/esim")
        return
    new_status = 0 if row['active'] else 1
    c.execute('UPDATE groups SET active = ? WHERE group_id = ?', (new_status, str(message.chat.id)))
    conn.commit()
    conn.close()
    await message.answer("✅ Бот активирован!" if new_status else "⏸️ Бот отключён.")

@dp.message(Command("esim"))
async def cmd_esim(message: Message):
    if message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return
    if not is_work_day():
        await message.answer("🔴 Рабочий день завершён.")
        return
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM groups WHERE group_id = ? AND active = 1', (str(message.chat.id),))
    if not c.fetchone():
        conn.close()
        await message.answer("⏸️ Бот не активен.")
        return
    conn.close()

    args = message.text.split()
    if len(args) > 1 and args[1].upper() in ['БХ', 'ХД']:
        await show_operators(message, args[1].upper(), edit=False)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟢 БезХолд", callback_data="esim_mode_БХ")],
            [InlineKeyboardButton(text="🟡 Холд", callback_data="esim_mode_ХД")],
        ])
        await message.answer("<b>📱 ВЫБЕРИТЕ ТИП СДАЧИ</b>", reply_markup=kb)

@dp.callback_query(F.data.startswith("esim_mode_"))
async def esim_mode_selected(callback: CallbackQuery):
    mode = callback.data.split("_")[2]
    await show_operators(callback.message, mode)
    await callback.answer()

async def show_operators(message: Message, mode: str, edit: bool = True):
    ops = get_operators()
    price_field = 'price_bh' if mode == 'БХ' else 'price_hd'
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for op in ops:
        status = "✅" if op['active'] else "❌"
        price = op[price_field]
        text = f"{status} {op['emoji']} {op['name']} · {price}$"
        cb = f"group_req_{op['id']}_{mode}" if op['active'] else "noop"
        kb.inline_keyboard.append([InlineKeyboardButton(text=text, callback_data=cb)])
    txt = f"<b>📱 ВЫБЕРИТЕ ОПЕРАТОРА · {mode}</b>"
    if edit:
        await message.edit_text(txt, reply_markup=kb)
    else:
        await message.answer(txt, reply_markup=kb)

@dp.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer("❌ Оператор недоступен")

@dp.callback_query(F.data.startswith("group_req_"))
async def group_request(callback: CallbackQuery):
    if not is_work_day():
        await callback.answer("🔴 Рабочий день завершён.")
        return
    parts = callback.data.split("_")
    op_id = int(parts[2])
    mode = parts[3] if len(parts) > 3 else 'БХ'
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM operators WHERE id = ? AND active = 1', (op_id,))
    op = c.fetchone()
    conn.close()
    if not op:
        await callback.answer("❌ Оператор недоступен")
        return
    channels = get_active_channels()
    if not channels:
        await callback.answer("Нет каналов")
        return

    price = op['price_bh'] if mode == 'БХ' else op['price_hd']
    conn = get_db()
    c = conn.cursor()
    group_id = str(callback.message.chat.id)
    requester_id = callback.from_user.id
    c.execute('INSERT INTO orders (operator, price, mode, status, group_id, requester_id, order_group_msg_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
              (op['name'], price, mode, 'active', group_id, requester_id, callback.message.message_id))
    order_id = c.lastrowid
    conn.commit()
    conn.close()

    bot_username = (await bot.me()).username
    deep_link = f"https://t.me/{bot_username}?start=order_{order_id}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔥 ЗАБРАТЬ ЗАКАЗ", url=deep_link)]])
    msg_text = f"<b>🔔 НОВЫЙ ЗАКАЗ #{order_id}</b>\n\n📱 {op['emoji']} {op['name']}\n💰 {price}$\n🎯 {mode}\n⏳ 10 минут"

    try:
        sent = await bot.send_message(chat_id=channels[0]['channel_id'], text=msg_text, reply_markup=kb)
        conn = get_db()
        c = conn.cursor()
        c.execute('UPDATE orders SET channel_msg_id = ? WHERE id = ?', (sent.message_id, order_id))
        conn.commit()
        conn.close()

        # Редактируем сообщение в группе
        await callback.message.edit_text(
            f"<b>✅ ЗАЯВКА #{order_id} СОЗДАНА</b>\n\n📱 {op['emoji']} {op['name']} · {price}$ · {mode}\n\n<i>Ожидайте исполнителя...</i>"
        )
        await callback.answer("✅ Заявка создана!")
    except Exception as e:
        await callback.answer(f"Ошибка: {e}")

# ============ СДАЧА ESIM ============
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
    await message.answer("📱 Укажите номер (+7XXXXXXXXXX)")

@dp.message(EsimUpload.waiting_for_phone)
async def esim_phone_received(message: Message, state: FSMContext):
    phone = format_phone(message.text)
    if not phone:
        await message.answer("❌ Неверный формат")
        return
    data = await state.get_data()
    await save_esim(message, state, data['qr_file_id'], phone, data.get('order_id'))

async def save_esim(message: Message, state: FSMContext, file_id: str, phone: str, order_id: int = None):
    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)
    if not can_submit_phone(phone, user_id):
        await message.answer("❌ Лимит превышен! Сброс в 00:00 МСК.")
        await state.clear()
        return

    conn = get_db()
    c = conn.cursor()
    if order_id:
        c.execute('SELECT * FROM orders WHERE id = ?', (order_id,))
        order = c.fetchone()
        mode = order['mode']
        hold_until = (datetime.now() + timedelta(hours=HOLD_HOURS)).isoformat() if mode == 'ХД' else None
        c.execute('UPDATE orders SET status = ?, phone = ?, qr_file_id = ?, done = ?, hold_until = ? WHERE id = ?',
                  ('done', phone, file_id, datetime.now().isoformat(), hold_until, order_id))
        c.execute('UPDATE users SET qr_month = qr_month + 1, total_qr = total_qr + 1, pending_balance = pending_balance + ? WHERE user_id = ?',
                  (order['price'], user_id))
        c.execute('INSERT INTO phone_submissions (phone, user_id, order_id, submitted) VALUES (?, ?, ?, ?)',
                  (phone, user_id, order_id, moscow_time().isoformat()))
        conn.commit()
        user = get_user(user_id)
        conn.close()

        if order['group_id']:
            try:
                pay_kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Встал", callback_data=f"status_{order_id}_up"),
                     InlineKeyboardButton(text="🚫 Блок", callback_data=f"status_{order_id}_block")],
                    [InlineKeyboardButton(text="❌ НеСкан", callback_data=f"status_{order_id}_noscan")],
                ])
                await bot.send_photo(chat_id=order['group_id'], photo=file_id,
                    caption=f"<b>✅ ЗАКАЗ #{order_id} ВЫПОЛНЕН</b>\n\n📱 {order['operator']}\n📞 <code>{phone}</code>\n👤 @{message.from_user.username or user_id}\n🎯 {mode}",
                    reply_markup=pay_kb)
            except Exception as e:
                logger.error(f"Ошибка отправки: {e}")

        await message.answer(f"<b>✅ ESIM СДАН!</b>\n\n📱 <code>{phone}</code>\n📊 QR: {user['qr_month']}\n💎 Предв.: {user['pending_balance']}$")
    else:
        c.execute('INSERT INTO orders (operator, price, mode, status, executor_id, phone, qr_file_id, done) VALUES (?,?,?,?,?,?,?,?)',
                  ('—', 0, 'БХ', 'done', user_id, phone, file_id, datetime.now().isoformat()))
        c.execute('UPDATE users SET qr_month = qr_month + 1, total_qr = total_qr + 1 WHERE user_id = ?', (user_id,))
        c.execute('INSERT INTO phone_submissions (phone, user_id, order_id, submitted) VALUES (?,?,?,?)', (phone, user_id, None, moscow_time().isoformat()))
        conn.commit()
        conn.close()
        await message.answer(f"<b>✅ ESIM СДАН!</b>\n\n📱 <code>{phone}</code>")
    await state.clear()

# ============ СТАТУСЫ ============
@dp.callback_query(F.data.startswith("status_"))
async def order_status_action(callback: CallbackQuery):
    parts = callback.data.split("_")
    order_id = int(parts[1])
    action = parts[2]
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM orders WHERE id = ?', (order_id,))
    order = c.fetchone()
    if not order:
        await callback.answer("Не найден")
        conn.close()
        return
    executor_id = order['executor_id']
    if action == "up":
        c.execute('UPDATE orders SET credited = 1, blocked = 0, noscan = 0 WHERE id = ?', (order_id,))
        c.execute('UPDATE users SET pending_balance = pending_balance - ?, expected_balance = expected_balance + ? WHERE user_id = ?',
                  (order['price'], order['price'], executor_id))
        conn.commit()
        conn.close()
        await callback.message.edit_caption(caption=callback.message.caption + "\n\n✅ <b>ЗАСЧИТАНО</b>", reply_markup=None)
        await callback.answer("✅ Засчитано")
        try:
            await bot.send_message(executor_id, f"✅ <b>#{order_id} ЗАСЧИТАН</b>\n💰 {order['price']}$ → Ожидаемая выплата")
        except: pass
    elif action == "block":
        c.execute('UPDATE orders SET credited = 0, blocked = 1, noscan = 0 WHERE id = ?', (order_id,))
        c.execute('UPDATE users SET pending_balance = pending_balance - ? WHERE user_id = ?', (order['price'], executor_id))
        conn.commit()
        conn.close()
        await callback.message.edit_caption(caption=callback.message.caption + "\n\n🚫 <b>БЛОК</b>", reply_markup=None)
        await callback.answer("🚫 Блок")
        try:
            await bot.send_message(executor_id, f"🚫 <b>#{order_id} БЛОК</b>\nСнято {order['price']}$")
        except: pass
    elif action == "noscan":
        c.execute('UPDATE orders SET credited = 0, blocked = 0, noscan = 1 WHERE id = ?', (order_id,))
        c.execute('UPDATE users SET pending_balance = pending_balance - ? WHERE user_id = ?', (order['price'], executor_id))
        conn.commit()
        conn.close()
        await callback.message.edit_caption(caption=callback.message.caption + "\n\n❌ <b>НеСкан</b>", reply_markup=None)
        await callback.answer("❌ НеСкан")
        try:
            await bot.send_message(executor_id, f"❌ <b>#{order_id} НеСкан</b>\nСнято {order['price']}$")
        except: pass

# ============ АДМИН: РАБОЧИЙ ДЕНЬ ============
@dp.callback_query(F.data == "admin_workday")
async def admin_workday(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    current = get_setting('work_day')
    if current == 'on':
        await callback.message.edit_text("<b>🔴 Завершить рабочий день?</b>\n\nВсе ожидаемые выплаты будут начислены на баланс.", reply_markup=confirm_kb("confirm_end_workday"))
    else:
        await callback.message.edit_text("<b>🟢 Начать рабочий день?</b>", reply_markup=confirm_kb("confirm_start_workday"))
    await callback.answer()

@dp.callback_query(F.data == "confirm_end_workday")
async def confirm_end_workday(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE users SET balance = balance + expected_balance, expected_balance = 0 WHERE expected_balance > 0')
    conn.commit()
    conn.close()
    set_setting('work_day', 'off')
    await callback.message.edit_text("🔴 <b>Рабочий день завершён.</b> Выплаты начислены.", reply_markup=back_to_admin())
    await callback.answer()

@dp.callback_query(F.data == "confirm_start_workday")
async def confirm_start_workday(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    set_setting('work_day', 'on')
    await callback.message.edit_text("🟢 <b>Рабочий день начат!</b>", reply_markup=back_to_admin())
    await callback.answer()

# ============ АДМИН: ВЫПЛАТЫ ============
@dp.callback_query(F.data == "admin_payouts")
async def admin_payouts(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💵 Выплатить всем", callback_data="confirm_pay_all")],
        [InlineKeyboardButton(text="👤 Выплатить по юзеру", callback_data="admin_pay_user")],
        [InlineKeyboardButton(text="➖ Списать у юзера", callback_data="admin_deduct_user")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")],
    ])
    await callback.message.edit_text("<b>💵 ВЫПЛАТЫ</b>", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "confirm_pay_all")
async def confirm_pay_all(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT SUM(expected_balance) FROM users')
    total = c.fetchone()[0] or 0
    conn.close()
    await callback.message.edit_text(f"<b>💵 Выплатить всем?</b>\n\nОбщая сумма: {total}$", reply_markup=confirm_kb("do_pay_all"))
    await callback.answer()

@dp.callback_query(F.data == "do_pay_all")
async def do_pay_all(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE users SET balance = balance + expected_balance, expected_balance = 0 WHERE expected_balance > 0')
    conn.commit()
    conn.close()
    await callback.message.edit_text("✅ Выплаты начислены всем!", reply_markup=back_to_admin())
    await callback.answer("✅ Готово")

@dp.callback_query(F.data == "admin_pay_user")
async def admin_pay_user(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("Введите @username или ID:")
    await state.set_state(AdminStates.waiting_for_pay_user)
    await callback.answer()

@dp.message(AdminStates.waiting_for_pay_user)
async def pay_user_received(message: Message, state: FSMContext):
    target = message.text.strip().replace('@', '')
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE user_id = ? OR username = ?', (int(target) if target.isdigit() else 0, target))
    user = c.fetchone()
    conn.close()
    if not user:
        await message.answer("❌ Не найден")
        await state.clear()
        return
    await state.update_data(pay_user_id=user['user_id'], pay_expected=user['expected_balance'])
    await message.answer(f"👤 @{user['username'] or user['user_id']}\nОжидаемая: {user['expected_balance']}$\n\nВведите сумму:")
    await state.set_state(AdminStates.waiting_for_pay_amount)

@dp.message(AdminStates.waiting_for_pay_amount)
async def pay_amount_received(message: Message, state: FSMContext):
    data = await state.get_data()
    expected = data['pay_expected']
    amount = expected if not message.text.strip() else float(message.text) if message.text.replace('.','').isdigit() else 0
    if amount > expected:
        await message.answer(f"❌ Макс: {expected}$")
        return
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE users SET balance = balance + ?, expected_balance = expected_balance - ? WHERE user_id = ?', (amount, amount, data['pay_user_id']))
    c.execute('INSERT INTO balance_history (user_id, amount, type, description) VALUES (?,?,?,?)', (data['pay_user_id'], amount, 'payout', 'Ручная'))
    conn.commit()
    conn.close()
    await message.answer(f"✅ {amount}$ выплачено")
    try:
        await bot.send_message(data['pay_user_id'], f"💵 <b>Выплата {amount}$</b>")
    except: pass
    await state.clear()

@dp.callback_query(F.data == "admin_deduct_user")
async def admin_deduct_user(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("Введите @username или ID:")
    await state.set_state(AdminStates.waiting_for_deduct_user)
    await callback.answer()

@dp.message(AdminStates.waiting_for_deduct_user)
async def deduct_user_received(message: Message, state: FSMContext):
    target = message.text.strip().replace('@', '')
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE user_id = ? OR username = ?', (int(target) if target.isdigit() else 0, target))
    user = c.fetchone()
    conn.close()
    if not user:
        await message.answer("❌ Не найден")
        await state.clear()
        return
    await state.update_data(deduct_user_id=user['user_id'], deduct_balance=user['balance'])
    await message.answer(f"👤 @{user['username'] or user['user_id']}\nБаланс: {user['balance']}$\nВведите сумму:")
    await state.set_state(AdminStates.waiting_for_deduct_amount)

@dp.message(AdminStates.waiting_for_deduct_amount)
async def deduct_amount_received(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
    except:
        await message.answer("❌ Число!")
        return
    data = await state.get_data()
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE users SET balance = MAX(0, balance - ?) WHERE user_id = ?', (amount, data['deduct_user_id']))
    c.execute('INSERT INTO balance_history (user_id, amount, type, description) VALUES (?,?,?,?)', (data['deduct_user_id'], -amount, 'deduct', 'Списание'))
    conn.commit()
    conn.close()
    await message.answer(f"✅ {amount}$ списано")
    await state.clear()

# ============ АДМИН: ОПЕРАТОРЫ ============
@dp.callback_query(F.data == "admin_operators")
async def admin_operators(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    ops = get_operators()
    text = "<b>💰 ОПЕРАТОРЫ</b>\n\n"
    for op in ops:
        text += f"{'🟢' if op['active'] else '🔴'} {op['emoji']} {op['name']} · БХ:{op['price_bh']}$ ХД:{op['price_hd']}$\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data="admin_add_op"),
         InlineKeyboardButton(text="✏️ Цены", callback_data="admin_edit_op")],
        [InlineKeyboardButton(text="🔄 Вкл/Выкл", callback_data="admin_toggle_op"),
         InlineKeyboardButton(text="🗑️ Удалить", callback_data="admin_del_op")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "admin_add_op")
async def admin_add_op(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("Название:")
    await state.set_state(AdminStates.waiting_for_operator_name)
    await callback.answer()

@dp.message(AdminStates.waiting_for_operator_name)
async def op_name_received(message: Message, state: FSMContext):
    await state.update_data(op_name=message.text)
    await message.answer("Цена БХ ($):")
    await state.set_state(AdminStates.waiting_for_operator_bh)

@dp.message(AdminStates.waiting_for_operator_bh)
async def op_bh_received(message: Message, state: FSMContext):
    try: bh = float(message.text)
    except: await message.answer("❌ Число!"); return
    await state.update_data(op_bh=bh)
    await message.answer("Цена ХД ($):")
    await state.set_state(AdminStates.waiting_for_operator_hd)

@dp.message(AdminStates.waiting_for_operator_hd)
async def op_hd_received(message: Message, state: FSMContext):
    try: hd = float(message.text)
    except: await message.answer("❌ Число!"); return
    await state.update_data(op_hd=hd)
    await message.answer("Эмодзи:")
    await state.set_state(AdminStates.waiting_for_operator_emoji)

@dp.message(AdminStates.waiting_for_operator_emoji)
async def op_emoji_received(message: Message, state: FSMContext):
    data = await state.get_data()
    emoji = message.text.strip()[0] if message.text else '📱'
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO operators (name, price_bh, price_hd, emoji) VALUES (?,?,?,?)',
              (data['op_name'], data['op_bh'], data['op_hd'], emoji))
    conn.commit()
    conn.close()
    await message.answer(f"✅ {emoji} {data['op_name']} БХ:{data['op_bh']}$ ХД:{data['op_hd']}$")
    await state.clear()

@dp.callback_query(F.data == "admin_edit_op")
async def admin_edit_op(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    ops = get_operators()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{op['emoji']} {op['name']}", callback_data=f"editop_{op['id']}")] for op in ops
    ] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]])
    await callback.message.edit_text("<b>Выберите оператора:</b>", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("editop_"))
async def edit_op_price(callback: CallbackQuery, state: FSMContext):
    await state.update_data(edit_op_id=int(callback.data.split("_")[1]))
    await callback.message.edit_text("Введите новую цену БХ ($):")
    await state.set_state(AdminStates.waiting_for_edit_bh)
    await callback.answer()

@dp.message(AdminStates.waiting_for_edit_bh)
async def edit_bh_received(message: Message, state: FSMContext):
    try: bh = float(message.text)
    except: await message.answer("❌ Число!"); return
    await state.update_data(edit_bh=bh)
    await message.answer("Введите новую цену ХД ($):")
    await state.set_state(AdminStates.waiting_for_edit_hd)

@dp.message(AdminStates.waiting_for_edit_hd)
async def edit_hd_received(message: Message, state: FSMContext):
    try: hd = float(message.text)
    except: await message.answer("❌ Число!"); return
    data = await state.get_data()
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE operators SET price_bh = ?, price_hd = ? WHERE id = ?', (data['edit_bh'], hd, data['edit_op_id']))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Обновлено")
    await state.clear()

@dp.callback_query(F.data == "admin_toggle_op")
async def admin_toggle_op(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    ops = get_operators()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{'🟢' if op['active'] else '🔴'} {op['emoji']} {op['name']}", callback_data=f"toggleop_{op['id']}")] for op in ops
    ] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]])
    await callback.message.edit_text("<b>Вкл/Выкл:</b>", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("toggleop_"))
async def toggle_operator(callback: CallbackQuery):
    op_id = int(callback.data.split("_")[1])
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT active FROM operators WHERE id = ?', (op_id,))
    r = c.fetchone()
    c.execute('UPDATE operators SET active = ? WHERE id = ?', (0 if r['active'] else 1, op_id))
    conn.commit()
    conn.close()
    await callback.answer("Изменено")
    await admin_toggle_op(callback)

@dp.callback_query(F.data == "admin_del_op")
async def admin_del_op(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    ops = get_operators()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🗑️ {op['emoji']} {op['name']}", callback_data=f"delop_{op['id']}")] for op in ops
    ] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]])
    await callback.message.edit_text("<b>Удалить:</b>", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("delop_"))
async def delete_operator(callback: CallbackQuery):
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM operators WHERE id = ?', (int(callback.data.split("_")[1]),))
    conn.commit()
    conn.close()
    await callback.answer("Удалён")
    await admin_del_op(callback)

# ============ АДМИН: КАНАЛЫ ============
@dp.callback_query(F.data == "admin_channels")
async def admin_channels(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    channels = get_active_channels()
    text = "<b>📢 КАНАЛЫ</b>\n\n" + ("".join([f"• {ch['username'] or ch['channel_id']}\n" for ch in channels]) or "<i>Нет</i>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕", callback_data="admin_add_channel"), InlineKeyboardButton(text="🗑️", callback_data="admin_del_channel")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "admin_add_channel")
async def admin_add_channel(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("Перешлите сообщение из канала или @username.")
    await state.set_state(AdminStates.waiting_for_channel)
    await callback.answer()

@dp.message(AdminStates.waiting_for_channel)
async def channel_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    channel_id, username = None, None
    if message.forward_from_chat:
        channel_id = str(message.forward_from_chat.id); username = message.forward_from_chat.username
    elif message.text and message.text.startswith('@'):
        username = message.text.strip()
        try:
            chat = await bot.get_chat(username); channel_id = str(chat.id)
        except:
            await message.answer("❌ Не найден"); return
    else:
        await message.answer("Перешлите или @username"); return
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO channels (channel_id, username) VALUES (?,?)', (channel_id, username))
    conn.commit()
    conn.close()
    await message.answer("✅ Канал добавлен!")
    await state.clear()

@dp.callback_query(F.data == "admin_del_channel")
async def admin_del_channel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("<b>Удалить все каналы?</b>", reply_markup=confirm_kb("confirm_del_channels"))
    await callback.answer()

@dp.callback_query(F.data == "confirm_del_channels")
async def confirm_del_channels(callback: CallbackQuery):
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM channels')
    conn.commit()
    conn.close()
    await callback.message.edit_text("✅ Каналы удалены", reply_markup=back_to_admin())
    await callback.answer()

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
        text += f"{'🟢' if g['active'] else '🔴'} {g['username'] or g['group_id']}\n"
    await callback.message.edit_text(text, reply_markup=back_to_admin())
    await callback.answer()

# ============ АДМИН: БД ============
@dp.callback_query(F.data == "admin_db_menu")
async def admin_db_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    auto = get_setting('auto_backup')
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💾 Выгрузка", callback_data="admin_db_export"),
         InlineKeyboardButton(text="📥 Загрузка", callback_data="admin_db_import")],
        [InlineKeyboardButton(text=f"🔄 Авто [{auto.upper()}]", callback_data="admin_db_auto")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")],
    ])
    await callback.message.edit_text("<b>💾 БАЗА ДАННЫХ</b>", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "admin_db_export")
async def admin_db_export(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    try:
        await callback.message.answer_document(FSInputFile(DB_PATH), caption=f"📦 {moscow_time().strftime('%Y-%m-%d %H:%M')}")
        await callback.answer("✅")
    except: await callback.answer("Ошибка")

@dp.callback_query(F.data == "admin_db_import")
async def admin_db_import(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("Отправьте .db файл.")
    await state.set_state(AdminStates.waiting_for_db_file)
    await callback.answer()

@dp.message(AdminStates.waiting_for_db_file, F.document)
async def db_file_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    if not message.document.file_name.endswith('.db'):
        await message.answer("❌ .db!")
        return
    try:
        await bot.download(message.document, destination=DB_PATH)
        init_db()
        await message.answer("✅ БД заменена!")
    except Exception as e:
        await message.answer(f"❌ {e}")
    await state.clear()

@dp.callback_query(F.data == "admin_db_auto")
async def admin_db_auto(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    cur = get_setting('auto_backup')
    set_setting('auto_backup', 'off' if cur == 'on' else 'on')
    await callback.answer(f"{'OFF' if cur=='on' else 'ON'}")
    await admin_db_menu(callback)

# ============ АДМИН: СТАТИСТИКА ============
@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM users'); users = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM orders'); orders = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM orders WHERE status="done"'); done = c.fetchone()[0]
    c.execute('SELECT COALESCE(SUM(price),0) FROM orders WHERE credited=1'); credited = c.fetchone()[0]
    c.execute('SELECT COALESCE(SUM(amount),0) FROM balance_history WHERE type="payout"'); paid = c.fetchone()[0]
    conn.close()
    text = f"<b>📊 СТАТИСТИКА</b>\n\n👥 Юзеров: {users}\n📱 Заявок: {orders}\n✅ Сдано: {done}\n💰 Зачтено: {credited}$\n💵 Выплачено: {paid}$"
    await callback.message.edit_text(text, reply_markup=back_to_admin())
    await callback.answer()

# ============ АДМИН: УЧАСТНИКИ ============
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
        text += f"• @{u['username'] or u['user_id']} | {u['rank']} | QR:{u['total_qr']} | ${u['balance']}\n"
    await callback.message.edit_text(text, reply_markup=back_to_admin())
    await callback.answer()

# ============ АДМИН: РАССЫЛКА ============
@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("Введите текст:")
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
    cnt = 0
    for u in users:
        try:
            await bot.send_message(u['user_id'], message.text)
            cnt += 1
            await asyncio.sleep(0.05)
        except: pass
    await message.answer(f"✅ {cnt}/{len(users)}")
    await state.clear()

# ============ АДМИН: ЗАЯВКИ ============
@dp.callback_query(F.data == "admin_create_order")
async def admin_create_order(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    ops = get_operators()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{op['emoji']} {op['name']} БХ:{op['price_bh']}$ ХД:{op['price_hd']}$", callback_data=f"admin_order_{op['id']}")] for op in ops
    ] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]])
    await callback.message.edit_text("<b>📱 ВЫБЕРИТЕ ОПЕРАТОРА</b>", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_order_"))
async def admin_order_mode(callback: CallbackQuery):
    op_id = int(callback.data.split("_")[2])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 БезХолд", callback_data=f"admin_order_do_{op_id}_БХ")],
        [InlineKeyboardButton(text="🟡 Холд", callback_data=f"admin_order_do_{op_id}_ХД")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_create_order")],
    ])
    await callback.message.edit_text("<b>Выберите режим:</b>", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_order_do_"))
async def admin_order_do(callback: CallbackQuery):
    parts = callback.data.split("_")
    op_id = int(parts[3])
    mode = parts[4]
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM operators WHERE id = ?', (op_id,))
    op = c.fetchone()
    channels = get_active_channels()
    if not channels:
        await callback.answer("Нет каналов"); conn.close(); return
    price = op['price_bh'] if mode == 'БХ' else op['price_hd']
    c.execute('INSERT INTO orders (operator, price, mode, status) VALUES (?,?,?,?)', (op['name'], price, mode, 'active'))
    oid = c.lastrowid
    conn.commit()
    conn.close()
    bot_username = (await bot.me()).username
    deep_link = f"https://t.me/{bot_username}?start=order_{oid}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔥 ЗАБРАТЬ ЗАКАЗ", url=deep_link)]])
    msg_text = f"<b>🔔 ЗАКАЗ #{oid}</b>\n\n📱 {op['emoji']} {op['name']}\n💰 {price}$\n🎯 {mode}"
    try:
        sent = await bot.send_message(chat_id=channels[0]['channel_id'], text=msg_text, reply_markup=kb)
        conn = get_db()
        c = conn.cursor()
        c.execute('UPDATE orders SET channel_msg_id = ? WHERE id = ?', (sent.message_id, oid))
        conn.commit()
        conn.close()
        await callback.message.edit_text(f"✅ Заявка #{oid} в канале.", reply_markup=back_to_admin())
        await callback.answer("✅")
    except Exception as e:
        await callback.answer(f"Ошибка: {e}")

# ============ АДМИН: УДАЛЕНИЕ ЗАЯВОК ============
@dp.callback_query(F.data == "admin_delete_orders")
async def admin_delete_orders(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM orders ORDER BY created DESC LIMIT 30')
    orders = c.fetchall()
    conn.close()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🗑️ #{o['id']} {o['operator']}", callback_data=f"confirm_del_order_{o['id']}")] for o in orders
    ] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]])
    await callback.message.edit_text("<b>🗑️ Удалить заявку:</b>", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("confirm_del_order_"))
async def confirm_del_order(callback: CallbackQuery):
    oid = int(callback.data.split("_")[3])
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM orders WHERE id = ?', (oid,))
    conn.commit()
    conn.close()
    await callback.answer(f"#{oid} удалён")
    await admin_delete_orders(callback)

# ============ ПРОФИЛЬ ============
@dp.callback_query(F.data == "profile")
async def profile(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user:
        ensure_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
        user = get_user(callback.from_user.id)
    text = (
        f"<b>👤 ПРОФИЛЬ</b>\n\n"
        f"🆔 @{user['username'] or user['user_id']}\n"
        f"📊 <b>Ранг:</b> {user['rank']}\n"
        f"💎 <b>Бонус:</b> +{user['bonus']}$\n"
        f"📱 <b>QR за месяц:</b> {user['qr_month']}\n"
        f"📈 <b>Всего QR:</b> {user['total_qr']}\n\n"
        f"💎 <b>Предв.:</b> {user['pending_balance']}$\n"
        f"⏳ <b>Ожидаемая:</b> {user['expected_balance']}$\n"
        f"💵 <b>Баланс:</b> {user['balance']}$"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 История баланса", callback_data="balance_history")],
        [InlineKeyboardButton(text="💵 Вывести", callback_data="withdraw"),
         InlineKeyboardButton(text="📱 История сдачи", callback_data="submission_history")],
        [InlineKeyboardButton(text="ℹ️ Информация", callback_data="info")],
        [InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_main")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "balance_history")
async def balance_history(callback: CallbackQuery):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM balance_history WHERE user_id = ? ORDER BY created DESC LIMIT 50', (callback.from_user.id,))
    rows = c.fetchall()
    conn.close()
    text = "<b>📋 ИСТОРИЯ БАЛАНСА</b>\n\n"
    if rows:
        for r in rows:
            sign = "+" if r['amount'] >= 0 else ""
            text += f"• {r['type']}: {sign}{r['amount']}$ ({r['created'][:10]})\n"
    else:
        text += "<i>Нет операций</i>"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 В профиль", callback_data="profile")]
    ]))
    await callback.answer()

@dp.callback_query(F.data == "submission_history")
async def submission_history(callback: CallbackQuery):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM orders WHERE executor_id = ? ORDER BY created DESC LIMIT 50', (callback.from_user.id,))
    orders = c.fetchall()
    conn.close()
    text = "<b>📱 ИСТОРИЯ СДАЧИ</b>\n\n"
    if orders:
        for o in orders:
            st = '✅' if o['credited'] else ('🚫' if o['blocked'] else ('❌' if o['noscan'] else '🟡'))
            text += f"#{o['id']} <code>{o['phone'] or '—'}</code> | {o['operator']} | {o['mode']} | {st}\n"
    else:
        text += "<i>Нет сданных</i>"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 В профиль", callback_data="profile")]
    ]))
    await callback.answer()

@dp.callback_query(F.data == "withdraw")
async def withdraw(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if user['balance'] < MIN_WITHDRAW:
        await callback.message.edit_text(
            f"<b>💵 ВЫВОД</b>\n\nБаланс: {user['balance']}$\nМин: {MIN_WITHDRAW}$\n<i>Недостаточно.</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В профиль", callback_data="profile")]]))
        await callback.answer()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💵 {MIN_WITHDRAW}$", callback_data=f"withdraw_amt_{MIN_WITHDRAW}")],
        [InlineKeyboardButton(text=f"💵 Всё ({user['balance']}$)", callback_data=f"withdraw_amt_{user['balance']}")],
        [InlineKeyboardButton(text="🔙 В профиль", callback_data="profile")],
    ])
    await callback.message.edit_text(f"<b>💵 ВЫВОД</b>\n\nБаланс: {user['balance']}$\nМин: {MIN_WITHDRAW}$\nОтправка на @{SEND_USERNAME or 'send'}", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("withdraw_amt_"))
async def withdraw_amount(callback: CallbackQuery):
    amount = float(callback.data.split("_")[2])
    user = get_user(callback.from_user.id)
    if user['balance'] < amount:
        await callback.answer("Недостаточно"); return
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE users SET balance = balance - ? WHERE user_id = ?', (amount, callback.from_user.id))
    c.execute('INSERT INTO balance_history (user_id, amount, type, description) VALUES (?,?,?,?)', (callback.from_user.id, -amount, 'withdraw', 'Вывод'))
    conn.commit()
    conn.close()
    channels = get_active_channels()
    if channels:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💵 Оплатить", callback_data=f"wd_pay_{callback.from_user.id}_{amount}")],
            [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"wd_reject_{callback.from_user.id}")],
        ])
        msg = f"<b>💵 ВЫВОД</b>\n\n👤 @{user['username'] or user['user_id']}\n💰 {amount}$\n📱 @{SEND_USERNAME}"
        try:
            await bot.send_message(chat_id=channels[0]['channel_id'], text=msg, reply_markup=kb)
        except: pass
    await callback.message.edit_text(f"✅ Заявка на {amount}$ создана.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 В профиль", callback_data="profile")]
    ]))
    await callback.answer()

@dp.callback_query(F.data == "info")
async def info(callback: CallbackQuery):
    await callback.message.edit_text(f"<b>ℹ️ ИНФО</b>\n\nМин. вывод: {MIN_WITHDRAW}$\nВывод: @{SEND_USERNAME or 'send'}\nБХ — без холда\nХД — холд {HOLD_HOURS}ч\nАнтиспам: 5 мин / 3 предупреждения", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 В профиль", callback_data="profile")]
    ]))
    await callback.answer()

@dp.callback_query(F.data == "operators_list")
async def operators_list(callback: CallbackQuery):
    ops = get_operators()
    text = "<b>📊 ЦЕНЫ БХ/ХД</b>\n\n"
    for op in ops:
        st = "✅" if op['active'] else "❌"
        text += f"{st} {op['emoji']} <b>{op['name']}</b> · {op['price_bh']}$/{op['price_hd']}$\n"
    await callback.message.edit_text(text, reply_markup=back_to_main())
    await callback.answer()

@dp.callback_query(F.data == "my_numbers")
async def my_numbers(callback: CallbackQuery):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM orders WHERE executor_id = ? ORDER BY created DESC LIMIT 50', (callback.from_user.id,))
    orders = c.fetchall()
    conn.close()
    text = "<b>📋 МОИ НОМЕРА</b>\n\n"
    if orders:
        for o in orders:
            st = '✅' if o['credited'] else ('🚫' if o['blocked'] else ('❌' if o['noscan'] else '🟡'))
            text += f"#{o['id']} <code>{o['phone'] or '—'}</code> | {o['operator']} | {o['mode']} | {st}\n"
    else:
        text += "<i>Нет</i>"
    await callback.message.edit_text(text, reply_markup=back_to_main())
    await callback.answer()

@dp.callback_query(F.data == "referral")
async def referral(callback: CallbackQuery):
    ensure_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    link = f"https://t.me/{(await bot.me()).username}?start=ref_{callback.from_user.id}"
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM referrals WHERE referrer_id = ?', (callback.from_user.id,))
    refs = c.fetchone()[0]
    conn.close()
    await callback.message.edit_text(f"<b>👥 РЕФЕРАЛЫ</b>\n\n🔗 <code>{link}</code>\n\n👥 Рефералов: {refs}", reply_markup=back_to_main())
    await callback.answer()

@dp.callback_query(F.data == "help")
async def help_cmd(callback: CallbackQuery):
    await callback.message.edit_text("<b>ℹ️ ПОМОЩЬ</b>\n\n/esim — запрос номера\nБХ/ХД — режимы\n5 мин на сдачу\n3 предупреждения = блок 1ч", reply_markup=back_to_main())
    await callback.answer()

@dp.callback_query(F.data == "back_main")
async def back_main(callback: CallbackQuery):
    await callback.message.edit_text("<b>🚀 ERWINS ESIM BOT</b>\n\nВыберите действие:", reply_markup=main_menu())
    await callback.answer()

@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("<b>🛠️ АДМИН-ПАНЕЛЬ</b>", reply_markup=admin_menu())
    await callback.answer()

@dp.callback_query(F.data == "close")
async def close(callback: CallbackQuery):
    try: await callback.message.delete()
    except: pass
    await callback.answer()

@dp.message(F.text, F.chat.type == ChatType.PRIVATE)
async def unknown_message(message: Message):
    ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer("<b>🚀 ERWINS ESIM BOT</b>", reply_markup=main_menu())

# ============ АНТИСПАМ ============
async def antispam_task():
    while True:
        await asyncio.sleep(30)
        conn = get_db()
        c = conn.cursor()
        timeout = (datetime.now() - timedelta(seconds=SUBMIT_TIMEOUT)).isoformat()
        c.execute("SELECT * FROM orders WHERE status = 'taken' AND taken_at <= ?", (timeout,))
        expired = c.fetchall()
        for o in expired:
            c.execute('UPDATE orders SET status = ?, channel_msg_id = NULL WHERE id = ?', ('active', o['id']))
            c.execute('UPDATE users SET warnings = warnings + 1 WHERE user_id = ?', (o['executor_id'],))
            c.execute('SELECT warnings FROM users WHERE user_id = ?', (o['executor_id'],))
            warns = c.fetchone()['warnings']
            if warns >= MAX_WARNINGS:
                block_until = (datetime.now() + timedelta(hours=BLOCK_HOURS)).isoformat()
                c.execute('UPDATE users SET warnings = 0, blocked_until = ? WHERE user_id = ?', (block_until, o['executor_id']))
            conn.commit()

            try:
                await bot.send_message(o['executor_id'],
                    f"⚠️ <b>Время вышло!</b> Заявка #{o['id']} возвращена.\n"
                    f"Предупреждений: {warns}/{MAX_WARNINGS}\n"
                    + ("🚫 <b>ВЫ ЗАБЛОКИРОВАНЫ НА 1 ЧАС!</b>" if warns >= MAX_WARNINGS else ""))
            except: pass

            channels = get_active_channels()
            if channels:
                bot_username = (await bot.me()).username
                deep_link = f"https://t.me/{bot_username}?start=order_{o['id']}"
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔥 ЗАБРАТЬ ЗАКАЗ", url=deep_link)]])
                msg_text = f"<b>🔄 ПОВТОР #{o['id']}</b>\n\n📱 {o['operator']}\n💰 {o['price']}$\n🎯 {o['mode']}"
                try:
                    sent = await bot.send_message(chat_id=channels[0]['channel_id'], text=msg_text, reply_markup=kb)
                    c.execute('UPDATE orders SET channel_msg_id = ? WHERE id = ?', (sent.message_id, o['id']))
                    conn.commit()
                except: pass
        conn.close()

async def auto_backup_task():
    while True:
        await asyncio.sleep(3600)
        if get_setting('auto_backup') == 'on':
            timestamp = moscow_time().strftime('%Y%m%d_%H%M%S')
            shutil.copy2(DB_PATH, os.path.join(BACKUP_DIR, f'backup_{timestamp}.db'))
            backups = sorted(os.listdir(BACKUP_DIR))
            while len(backups) > 48:
                os.remove(os.path.join(BACKUP_DIR, backups.pop(0)))

async def hold_check_task():
    while True:
        await asyncio.sleep(300)
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM orders WHERE status='done' AND mode='ХД' AND credited=1 AND paid=0 AND hold_until <= ?", (datetime.now().isoformat(),))
        for o in c.fetchall():
            c.execute('UPDATE orders SET paid=1 WHERE id=?', (o['id'],))
            c.execute('UPDATE users SET expected_balance=expected_balance-?, balance=balance+? WHERE user_id=?', (o['price'], o['price'], o['executor_id']))
            c.execute('INSERT INTO balance_history (user_id, amount, type, description) VALUES (?,?,?,?)', (o['executor_id'], o['price'], 'payout', f'Холд #{o["id"]}'))
            conn.commit()
            try: await bot.send_message(o['executor_id'], f"💵 <b>Автовыплата #{o['id']}</b>\n💰 {o['price']}$")
            except: pass
        conn.close()

async def main():
    logger.info("Бот запущен")
    asyncio.create_task(auto_backup_task())
    asyncio.create_task(hold_check_task())
    asyncio.create_task(antispam_task())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
