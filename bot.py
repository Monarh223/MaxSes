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
    Message, CallbackQuery, FSInputFile, BufferedInputFile
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import Command
from dotenv import load_dotenv
import qrcode
from PIL import Image

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
        pending_balance REAL DEFAULT 0.0, joined TEXT DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, operator TEXT, price REAL,
        mode TEXT DEFAULT 'БХ', status TEXT DEFAULT 'active', executor_id INTEGER,
        phone TEXT, qr_file_id TEXT, channel_msg_id INTEGER, group_id TEXT,
        group_thread_id INTEGER, requester_id INTEGER,
        created TEXT DEFAULT CURRENT_TIMESTAMP, taken TEXT, done TEXT,
        hold_until TEXT, paid INTEGER DEFAULT 0, credited INTEGER DEFAULT 0,
        blocked INTEGER DEFAULT 0, noscan INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS operators (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, price REAL,
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
        ('Билайн', 12, '⚙️'), ('МТС', 14, '🔴'), ('Мегафон', 10, '🟢'),
        ('Т2', 10, '⚪'), ('Сбер', 10, '🟡'), ('Газпром', 20, '🔵'), ('Добросвязь', 14, '🟣'),
    ]
    for name, price, emoji in defaults:
        c.execute('INSERT OR IGNORE INTO operators (name, price, emoji) VALUES (?, ?, ?)', (name, price, emoji))

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
    waiting_for_operator_price = State()
    waiting_for_operator_emoji = State()
    waiting_for_db_file = State()
    waiting_for_broadcast = State()
    waiting_for_edit_price = State()
    waiting_for_delete_order = State()
    waiting_for_pay_user = State()
    waiting_for_pay_amount = State()
    waiting_for_deduct_user = State()
    waiting_for_deduct_amount = State()

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def is_work_day():
    return get_setting('work_day') == 'on'

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
    phone_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM phone_submissions WHERE user_id = ? AND submitted BETWEEN ? AND ?', (user_id, today_start, today_end))
    user_count = c.fetchone()[0]
    conn.close()
    return phone_count < 2 and user_count < 5

def moscow_time():
    return datetime.now(MOSCOW_TZ)

def back_to_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_main")]
    ])

def back_to_admin():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]
    ])

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
    mode = get_setting('default_mode')
    wd = get_setting('work_day')
    wd_text = "🟢 Рабочий день" if wd == 'on' else "🔴 Завершён"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Операторы", callback_data="admin_operators"),
         InlineKeyboardButton(text="📢 Каналы", callback_data="admin_channels")],
        [InlineKeyboardButton(text="👥 Группы", callback_data="admin_groups"),
         InlineKeyboardButton(text=f"🎯 Режим: {mode}", callback_data="admin_mode")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"),
         InlineKeyboardButton(text="👤 Участники", callback_data="admin_users")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast"),
         InlineKeyboardButton(text="📱 Создать заявку", callback_data="admin_create_order")],
        [InlineKeyboardButton(text="🗑️ Удалить заявки", callback_data="admin_delete_orders")],
        [InlineKeyboardButton(text=wd_text, callback_data="admin_workday")],
        [InlineKeyboardButton(text="💵 Выплатить всем", callback_data="admin_pay_all"),
         InlineKeyboardButton(text="💵 Выплатить по юзу", callback_data="admin_pay_user")],
        [InlineKeyboardButton(text="➖ Списать у юзера", callback_data="admin_deduct_user")],
        [InlineKeyboardButton(text="💾 БД: Выгрузка", callback_data="admin_db_export"),
         InlineKeyboardButton(text="💾 БД: Загрузка", callback_data="admin_db_import")],
        [InlineKeyboardButton(text=f"💾 Автобэкап [{auto.upper()}]", callback_data="admin_db_auto")],
        [InlineKeyboardButton(text="🔙 Закрыть", callback_data="close")],
    ])
    return kb

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
            await message.answer("🔴 <b>Рабочий день завершён.</b> Приём номеров остановлен.")
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

        c.execute('UPDATE orders SET status = ?, executor_id = ?, taken = ? WHERE id = ?', ('taken', message.from_user.id, datetime.now().isoformat(), order_id))
        conn.commit()
        conn.close()

        channels = get_active_channels()
        for ch in channels:
            try:
                await bot.delete_message(chat_id=ch['channel_id'], message_id=order['channel_msg_id'])
            except:
                pass

        await message.answer(f"<b>✅ ЗАКАЗ #{order_id} ПРИНЯТ!</b>\n\n📱 {order['operator']} · {order['price']}$\n🎯 {order['mode']}\n\n<b>Отправьте фото QR-кода.</b>")
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
        await message.answer("⛔ Только админ может активировать бота")
        return

    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM groups WHERE group_id = ?', (str(message.chat.id),))
    row = c.fetchone()

    if not row:
        c.execute('INSERT INTO groups (group_id, username, active) VALUES (?, ?, ?)', (str(message.chat.id), message.chat.username or str(message.chat.id), 1))
        conn.commit()
        conn.close()
        await message.answer("✅ <b>Группа добавлена и бот активирован!</b>\nИспользуйте /esim для запроса.")
        return

    new_status = 0 if row['active'] == 1 else 1
    c.execute('UPDATE groups SET active = ? WHERE group_id = ?', (new_status, str(message.chat.id)))
    conn.commit()
    conn.close()
    if new_status:
        await message.answer("✅ <b>Бот активирован!</b>\n/esim")
    else:
        await message.answer("⏸️ <b>Бот отключён.</b>")

@dp.message(Command("esim"))
async def cmd_esim(message: Message):
    if message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await message.answer("Эта команда только для групп")
        return

    if not is_work_day():
        await message.answer("🔴 <b>Рабочий день завершён.</b> Запросы не принимаются.")
        return

    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM groups WHERE group_id = ? AND active = 1', (str(message.chat.id),))
    if not c.fetchone():
        conn.close()
        await message.answer("⏸️ Бот не активен. /work для включения.")
        return
    conn.close()

    args = message.text.split()
    if len(args) > 1 and args[1].upper() in ['БХ', 'ХД']:
        mode = args[1].upper()
        await show_operators(message, mode)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟢 БезХолд (БХ)", callback_data="esim_mode_БХ")],
            [InlineKeyboardButton(text="🟡 Холд (ХД)", callback_data="esim_mode_ХД")],
        ])
        await message.answer("<b>📱 ВЫБЕРИТЕ ТИП СДАЧИ</b>", reply_markup=kb)

@dp.callback_query(F.data.startswith("esim_mode_"))
async def esim_mode_selected(callback: CallbackQuery):
    mode = callback.data.split("_")[2]
    await show_operators(callback.message, mode)
    await callback.answer()

async def show_operators(message: Message, mode: str):
    ops = get_operators()
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for op in ops:
        status = "✅" if op['active'] else "❌"
        text = f"{status} {op['emoji']} {op['name']} · {op['price']}$ · {mode}"
        cb = f"group_req_{op['id']}_{mode}" if op['active'] else "noop"
        kb.inline_keyboard.append([InlineKeyboardButton(text=text, callback_data=cb)])
    await message.edit_text(f"<b>📱 ВЫБЕРИТЕ ОПЕРАТОРА · {mode}</b>", reply_markup=kb)

@dp.callback_query(F.data.startswith("group_req_"))
async def group_request(callback: CallbackQuery):
    if not is_work_day():
        await callback.answer("🔴 Рабочий день завершён.")
        return

    parts = callback.data.split("_")
    op_id = int(parts[2])
    mode = parts[3] if len(parts) > 3 else get_setting('default_mode')

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
        await callback.answer("Нет каналов для заявок")
        return

    conn = get_db()
    c = conn.cursor()
    group_id = str(callback.message.chat.id) if callback.message.chat else None
    requester_id = callback.from_user.id
    c.execute('INSERT INTO orders (operator, price, mode, status, group_id, requester_id) VALUES (?, ?, ?, ?, ?, ?)',
              (op['name'], op['price'], mode, 'active', group_id, requester_id))
    order_id = c.lastrowid
    conn.commit()
    conn.close()

    bot_username = (await bot.me()).username
    deep_link = f"https://t.me/{bot_username}?start=order_{order_id}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔥 ЗАБРАТЬ ЗАКАЗ", url=deep_link)]])
    msg_text = f"<b>🔔 НОВЫЙ ЗАКАЗ #{order_id}</b>\n\n📱 <b>Оператор:</b> {op['emoji']} {op['name']}\n💰 <b>Цена:</b> {op['price']}$\n🎯 <b>Режим:</b> {mode}\n⏳ <b>Дедлайн:</b> 10 минут"

    try:
        sent = await bot.send_message(chat_id=channels[0]['channel_id'], text=msg_text, reply_markup=kb)
        conn = get_db()
        c = conn.cursor()
        c.execute('UPDATE orders SET channel_msg_id = ? WHERE id = ?', (sent.message_id, order_id))
        conn.commit()
        conn.close()
        await callback.answer("✅ Заявка создана!")
    except Exception as e:
        await callback.answer(f"Ошибка: {e}")

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

async def save_esim(message: Message, state: FSMContext, file_id: str, phone: str, order_id: int = None):
    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)

    if not can_submit_phone(phone, user_id):
        await message.answer("❌ <b>Лимит превышен!</b>\nНельзя сдавать один номер более 2 раз в сутки. Сброс в 00:00 МСК.")
        await state.clear()
        return

    conn = get_db()
    c = conn.cursor()

    if order_id:
        c.execute('SELECT * FROM orders WHERE id = ?', (order_id,))
        order = c.fetchone()
        mode = order['mode'] if order else get_setting('default_mode')
        hold_until = None
        if mode == 'ХД':
            hold_until = (datetime.now() + timedelta(hours=HOLD_HOURS)).isoformat()

        c.execute('UPDATE orders SET status = ?, phone = ?, qr_file_id = ?, done = ?, hold_until = ? WHERE id = ?',
                  ('done', phone, file_id, datetime.now().isoformat(), hold_until, order_id))
        c.execute('UPDATE users SET qr_month = qr_month + 1, total_qr = total_qr + 1, pending_balance = pending_balance + ? WHERE user_id = ?',
                  (order['price'], user_id))
        c.execute('INSERT INTO phone_submissions (phone, user_id, order_id, submitted) VALUES (?, ?, ?, ?)',
                  (phone, user_id, order_id, moscow_time().isoformat()))
        conn.commit()
        c.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        user = c.fetchone()
        conn.close()

        if order and order['group_id']:
            try:
                pay_kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Встал", callback_data=f"status_{order_id}_up"),
                     InlineKeyboardButton(text="🚫 Блок", callback_data=f"status_{order_id}_block")],
                    [InlineKeyboardButton(text="❌ НеСкан", callback_data=f"status_{order_id}_noscan")],
                ])
                caption = f"<b>✅ ЗАКАЗ #{order_id} ВЫПОЛНЕН</b>\n\n📱 {order['operator']}\n📞 <code>{phone}</code>\n👤 @{message.from_user.username or user_id}\n🎯 {mode}"
                await bot.send_photo(chat_id=order['group_id'], photo=file_id, caption=caption, reply_markup=pay_kb)
            except Exception as e:
                logger.error(f"Ошибка отправки в группу: {e}")

        await message.answer(f"<b>✅ ESIM СДАН!</b>\n\n📱 <code>{phone}</code>\n📊 QR за месяц: {user['qr_month']}\n🏆 Ранг: {user['rank']}\n💎 Предварительная выплата: {user['pending_balance']}$")
    else:
        c.execute('INSERT INTO orders (operator, price, mode, status, executor_id, phone, qr_file_id, done) VALUES (?, ?, ?, ?, ?, ?, ?)',
                  ('Неизвестно', 0, get_setting('default_mode'), 'done', user_id, phone, file_id, datetime.now().isoformat()))
        c.execute('UPDATE users SET qr_month = qr_month + 1, total_qr = total_qr + 1 WHERE user_id = ?', (user_id,))
        c.execute('INSERT INTO phone_submissions (phone, user_id, order_id, submitted) VALUES (?, ?, ?, ?)',
                  (phone, user_id, None, moscow_time().isoformat()))
        conn.commit()
        c.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        user = c.fetchone()
        conn.close()
        await message.answer(f"<b>✅ ESIM СДАН!</b>\n\n📱 <code>{phone}</code>\n📊 QR за месяц: {user['qr_month']}")

    await state.clear()

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
        await callback.answer("Заказ не найден")
        conn.close()
        return

    executor_id = order['executor_id']

    if action == "up":
        # Засчитать
        c.execute('UPDATE orders SET credited = 1, blocked = 0, noscan = 0 WHERE id = ?', (order_id,))
        c.execute('UPDATE users SET pending_balance = pending_balance - ?, expected_balance = expected_balance + ? WHERE user_id = ?',
                  (order['price'], order['price'], executor_id))
        conn.commit()
        conn.close()

        await callback.message.edit_caption(
            caption=callback.message.caption + "\n\n✅ <b>ЗАСЧИТАНО</b>",
            reply_markup=None
        )
        await callback.answer("✅ Засчитано")

        try:
            await bot.send_message(executor_id, f"✅ <b>Заказ #{order_id} ЗАСЧИТАН</b>\n📱 {order['operator']} · {order['phone']}\n💰 {order['price']}$ → Ожидаемая выплата")
        except:
            pass

    elif action == "block":
        c.execute('UPDATE orders SET credited = 0, blocked = 1, noscan = 0 WHERE id = ?', (order_id,))
        c.execute('UPDATE users SET pending_balance = pending_balance - ? WHERE user_id = ?', (order['price'], executor_id))
        conn.commit()
        conn.close()

        await callback.message.edit_caption(caption=callback.message.caption + "\n\n🚫 <b>БЛОК</b>", reply_markup=None)
        await callback.answer("🚫 Блок")
        try:
            await bot.send_message(executor_id, f"🚫 <b>Заказ #{order_id} БЛОК</b>\n📱 {order['operator']} · {order['phone']}\nСумма снята с предварительной выплаты.")
        except:
            pass

    elif action == "noscan":
        c.execute('UPDATE orders SET credited = 0, blocked = 0, noscan = 1 WHERE id = ?', (order_id,))
        c.execute('UPDATE users SET pending_balance = pending_balance - ? WHERE user_id = ?', (order['price'], executor_id))
        conn.commit()
        conn.close()

        await callback.message.edit_caption(caption=callback.message.caption + "\n\n❌ <b>НеСкан</b>", reply_markup=None)
        await callback.answer("❌ НеСкан")
        try:
            await bot.send_message(executor_id, f"❌ <b>Заказ #{order_id} НеСкан</b>\n📱 {order['operator']} · {order['phone']}\nСумма снята с предварительной выплаты.")
        except:
            pass

# ============ АДМИН: РАБОЧИЙ ДЕНЬ ============
@dp.callback_query(F.data == "admin_workday")
async def admin_workday(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    current = get_setting('work_day')
    if current == 'on':
        # Завершаем рабочий день - выплачиваем всем expected_balance
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT user_id, expected_balance FROM users WHERE expected_balance > 0')
        users = c.fetchall()
        for u in users:
            c.execute('UPDATE users SET balance = balance + ?, expected_balance = 0 WHERE user_id = ?', (u['expected_balance'], u['user_id']))
            c.execute('INSERT INTO balance_history (user_id, amount, type, description) VALUES (?, ?, ?, ?)',
                      (u['user_id'], u['expected_balance'], 'payout', 'Завершение рабочего дня'))
        conn.commit()
        conn.close()
        set_setting('work_day', 'off')
        await callback.answer("🔴 Рабочий день завершён. Выплаты начислены.")
    else:
        set_setting('work_day', 'on')
        await callback.answer("🟢 Рабочий день начат!")
    await callback.message.edit_reply_markup(reply_markup=admin_menu())

# ============ АДМИН: ВЫПЛАТИТЬ ВСЕМ ============
@dp.callback_query(F.data == "admin_pay_all")
async def admin_pay_all(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE users SET balance = balance + expected_balance, expected_balance = 0 WHERE expected_balance > 0')
    conn.commit()
    conn.close()
    await callback.answer("✅ Выплаты начислены всем!")
    await callback.message.edit_reply_markup(reply_markup=admin_menu())

# ============ АДМИН: ВЫПЛАТИТЬ ПО ЮЗУ ============
@dp.callback_query(F.data == "admin_pay_user")
async def admin_pay_user(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("Введите @username или ID пользователя:")
    await state.set_state(AdminStates.waiting_for_pay_user)
    await callback.answer()

@dp.message(AdminStates.waiting_for_pay_user)
async def pay_user_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    target = message.text.strip().replace('@', '')
    conn = get_db()
    c = conn.cursor()
    if target.isdigit():
        c.execute('SELECT * FROM users WHERE user_id = ?', (int(target),))
    else:
        c.execute('SELECT * FROM users WHERE username = ?', (target,))
    user = c.fetchone()
    conn.close()
    if not user:
        await message.answer("❌ Пользователь не найден.")
        await state.clear()
        return
    await state.update_data(pay_user_id=user['user_id'], pay_expected=user['expected_balance'])
    await message.answer(f"👤 @{user['username'] or user['user_id']}\n💎 Ожидаемая выплата: {user['expected_balance']}$\n\nВведите сумму к выплате (Enter для всей суммы):")
    await state.set_state(AdminStates.waiting_for_pay_amount)

@dp.message(AdminStates.waiting_for_pay_amount)
async def pay_amount_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    data = await state.get_data()
    user_id = data['pay_user_id']
    expected = data['pay_expected']
    amount = expected
    if message.text.strip():
        try:
            amount = float(message.text)
        except:
            await message.answer("❌ Введите число!")
            return
    if amount > expected:
        await message.answer(f"❌ Максимум: {expected}$")
        return

    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE users SET balance = balance + ?, expected_balance = expected_balance - ? WHERE user_id = ?', (amount, amount, user_id))
    c.execute('INSERT INTO balance_history (user_id, amount, type, description) VALUES (?, ?, ?, ?)', (user_id, amount, 'payout', 'Ручная выплата'))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Выплачено {amount}$ пользователю.")
    try:
        await bot.send_message(user_id, f"💵 <b>Выплата {amount}$</b>\nНачислено на баланс.")
    except:
        pass
    await state.clear()

# ============ АДМИН: СПИСАТЬ ============
@dp.callback_query(F.data == "admin_deduct_user")
async def admin_deduct_user(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("Введите @username или ID пользователя:")
    await state.set_state(AdminStates.waiting_for_deduct_user)
    await callback.answer()

@dp.message(AdminStates.waiting_for_deduct_user)
async def deduct_user_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    target = message.text.strip().replace('@', '')
    conn = get_db()
    c = conn.cursor()
    if target.isdigit():
        c.execute('SELECT * FROM users WHERE user_id = ?', (int(target),))
    else:
        c.execute('SELECT * FROM users WHERE username = ?', (target,))
    user = c.fetchone()
    conn.close()
    if not user:
        await message.answer("❌ Пользователь не найден.")
        await state.clear()
        return
    await state.update_data(deduct_user_id=user['user_id'], deduct_balance=user['balance'])
    await message.answer(f"👤 @{user['username'] or user['user_id']}\n💵 Баланс: {user['balance']}$\n\nВведите сумму к списанию:")
    await state.set_state(AdminStates.waiting_for_deduct_amount)

@dp.message(AdminStates.waiting_for_deduct_amount)
async def deduct_amount_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    try:
        amount = float(message.text)
    except:
        await message.answer("❌ Введите число!")
        return
    data = await state.get_data()
    user_id = data['deduct_user_id']
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE users SET balance = MAX(0, balance - ?) WHERE user_id = ?', (amount, user_id))
    c.execute('INSERT INTO balance_history (user_id, amount, type, description) VALUES (?, ?, ?, ?)', (user_id, -amount, 'deduct', 'Списание админом'))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Списано {amount}$.")
    await state.clear()

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
        f"💎 <b>Предварительная выплата:</b> {user['pending_balance']}$\n"
        f"⏳ <b>Ожидаемая выплата:</b> {user['expected_balance']}$\n"
        f"💵 <b>Баланс:</b> {user['balance']}$"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 История баланса", callback_data="balance_history")],
        [InlineKeyboardButton(text="📱 История сдачи", callback_data="submission_history")],
        [InlineKeyboardButton(text="💵 Вывести", callback_data="withdraw")],
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
    if not rows:
        text += "<i>Нет операций</i>"
    else:
        for r in rows:
            sign = "+" if r['amount'] >= 0 else ""
            emoji = "✅" if r['type'] == 'payout' else "🔴"
            text += f"{emoji} {r['type']}: {sign}{r['amount']}$ ({r['created'][:10]})\n"

    await callback.message.edit_text(text, reply_markup=back_to_main())
    await callback.answer()

@dp.callback_query(F.data == "submission_history")
async def submission_history(callback: CallbackQuery):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM orders WHERE executor_id = ? ORDER BY created DESC LIMIT 30', (callback.from_user.id,))
    orders = c.fetchall()
    conn.close()

    text = "<b>📱 ИСТОРИЯ СДАЧИ</b>\n\n"
    if not orders:
        text += "<i>Нет сданных номеров</i>"
    else:
        for o in orders:
            status_map = {'done': ('✅ Учтен' if o['credited'] else ('🚫 Блок' if o['blocked'] else '❌ НеСкан'))}
            status = status_map.get(o['status'], '⚪')
            text += f"{o['id']}) <code>{o['phone'] or '—'}</code> | {o['operator']} | {o['mode']} | {status}\n"

    await callback.message.edit_text(text, reply_markup=back_to_main())
    await callback.answer()

@dp.callback_query(F.data == "withdraw")
async def withdraw(callback: CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    if not user:
        await callback.answer("Пользователь не найден")
        return

    if user['balance'] < MIN_WITHDRAW:
        await callback.message.edit_text(
            f"<b>💵 ВЫВОД</b>\n\nВаш баланс: {user['balance']}$\nМинимальная сумма вывода: {MIN_WITHDRAW}$\n\n<i>Недостаточно средств для вывода.</i>",
            reply_markup=back_to_main()
        )
        await callback.answer()
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💵 Вывести {MIN_WITHDRAW}$", callback_data=f"withdraw_amt_{MIN_WITHDRAW}")],
        [InlineKeyboardButton(text=f"💵 Вывести всё ({user['balance']}$)", callback_data=f"withdraw_amt_{user['balance']}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="profile")],
    ])
    await callback.message.edit_text(f"<b>💵 ВЫВОД</b>\n\nВаш баланс: {user['balance']}$\nМин. сумма: {MIN_WITHDRAW}$\n\nВывод будет отправлен на @{SEND_USERNAME or 'send'}", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("withdraw_amt_"))
async def withdraw_amount(callback: CallbackQuery):
    amount = float(callback.data.split("_")[2])
    user = get_user(callback.from_user.id)

    if user['balance'] < amount:
        await callback.answer("Недостаточно средств")
        return

    # Списываем с баланса
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE users SET balance = balance - ? WHERE user_id = ?', (amount, callback.from_user.id))
    c.execute('INSERT INTO balance_history (user_id, amount, type, description) VALUES (?, ?, ?, ?)',
              (callback.from_user.id, -amount, 'withdraw', 'Вывод средств'))
    wd_id = c.lastrowid
    conn.commit()
    conn.close()

    # Создаём заявку в канал
    channels = get_active_channels()
    if channels:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💵 Оплатить", callback_data=f"wd_pay_{wd_id}")],
            [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"wd_reject_{wd_id}")],
        ])
        msg_text = f"<b>💵 ЗАЯВКА НА ВЫВОД #{wd_id}</b>\n\n👤 @{user['username'] or user['user_id']}\n💰 <b>Сумма:</b> {amount}$\n📱 @{SEND_USERNAME or 'send'}"
        try:
            sent = await bot.send_message(chat_id=channels[0]['channel_id'], text=msg_text, reply_markup=kb)
            conn = get_db()
            c = conn.cursor()
            c.execute('INSERT INTO withdraw_requests (user_id, amount, status, channel_msg_id) VALUES (?, ?, ?, ?)',
                      (callback.from_user.id, amount, 'pending', sent.message_id))
            conn.commit()
            conn.close()
        except:
            pass

    await callback.message.edit_text(f"✅ Заявка на вывод {amount}$ создана.", reply_markup=back_to_main())
    await callback.answer()

@dp.callback_query(F.data == "info")
async def info(callback: CallbackQuery):
    text = f"<b>ℹ️ ИНФОРМАЦИЯ</b>\n\nМин. сумма вывода: {MIN_WITHDRAW}$\nВывод на: @{SEND_USERNAME or 'send'}\n\nРежимы:\n🟢 БХ — без холда\n🟡 ХД — холд {HOLD_HOURS}ч"
    await callback.message.edit_text(text, reply_markup=back_to_main())
    await callback.answer()

# ============ ОСТАЛЬНЫЕ ХЕНДЛЕРЫ (БЕЗ ИЗМЕНЕНИЙ) ============
@dp.callback_query(F.data == "operators_list")
async def operators_list(callback: CallbackQuery):
    ops = get_operators()
    text = "<b>📊 ЦЕНЫ</b>\n\n"
    for op in ops:
        status = "✅" if op['active'] else "❌"
        text += f"{status} {op['emoji']} <b>{op['name']}</b> · {op['price']}$\n"
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
    if not orders:
        text += "<i>Нет номеров</i>"
    else:
        for o in orders:
            status = '✅ Учтен' if o['credited'] else ('🚫 Блок' if o['blocked'] else ('❌ НеСкан' if o['noscan'] else '🟡 Ожидание'))
            text += f"#{o['id']} <code>{o['phone'] or '—'}</code> | {o['operator']} | {o['mode']} | {status}\n"
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
    refs = c.fetchone()[0]
    conn.close()
    text = f"<b>👥 РЕФЕРАЛЫ</b>\n\n🔗 <code>{link}</code>\n\n👥 Рефералов: {refs}\n📊 Ранг: {user['rank']}"
    await callback.message.edit_text(text, reply_markup=back_to_main())
    await callback.answer()

@dp.callback_query(F.data == "help")
async def help_cmd(callback: CallbackQuery):
    text = "<b>ℹ️ ПОМОЩЬ</b>\n\n<b>📱 Сдать ESIM:</b> нажмите кнопку в канале\n<b>📋 В группе:</b> /esim\n<b>🔄 Выбор типа:</b> БезХолд / Холд\n<b>💵 Вывод:</b> через Профиль → Вывести"
    await callback.message.edit_text(text, reply_markup=back_to_main())
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
    try:
        await callback.message.delete()
    except:
        pass
    await callback.answer()

# Админ-операторы, каналы, группы, БД, статистика, участники, рассылка, удаление заявок, режим — код идентичен предыдущей версии. Здесь для краткости опущен, вставь из предыдущего сообщения блоки:
# admin_channels, admin_add_channel, channel_received, admin_del_channel,
# admin_groups, admin_operators, admin_add_op, op_name_received, op_price_received, op_emoji_received,
# admin_edit_op, edit_op_price, edit_price_received, admin_toggle_op, toggle_operator,
# admin_del_op, delete_operator, admin_mode, set_mode,
# admin_db_export, admin_db_import, db_file_received, admin_db_auto,
# admin_create_order, admin_order_created, admin_delete_orders, delete_order,
# admin_stats, admin_users, admin_broadcast, broadcast_send,
# unknown_message

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
        c.execute("SELECT * FROM orders WHERE status = 'done' AND mode = 'ХД' AND credited = 1 AND paid = 0 AND hold_until <= ?", (datetime.now().isoformat(),))
        orders = c.fetchall()
        for o in orders:
            c.execute('UPDATE orders SET paid = 1 WHERE id = ?', (o['id'],))
            c.execute('UPDATE users SET expected_balance = expected_balance - ?, balance = balance + ? WHERE user_id = ?', (o['price'], o['price'], o['executor_id']))
            c.execute('INSERT INTO balance_history (user_id, amount, type, description) VALUES (?, ?, ?, ?)', (o['executor_id'], o['price'], 'payout', f'Автовыплата по холду #{o["id"]}'))
            conn.commit()
            try:
                await bot.send_message(o['executor_id'], f"💵 <b>Автовыплата #{o['id']}</b>\n💰 {o['price']}$\n📱 {o['operator']}")
            except:
                pass
        conn.close()

async def main():
    logger.info("Бот запущен")
    asyncio.create_task(auto_backup_task())
    asyncio.create_task(hold_check_task())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
