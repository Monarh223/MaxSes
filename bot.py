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
        balance REAL DEFAULT 0.0, joined TEXT DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, operator TEXT, price REAL,
        mode TEXT DEFAULT 'БХ', status TEXT DEFAULT 'active', executor_id INTEGER,
        phone TEXT, qr_file_id TEXT, channel_msg_id INTEGER, group_id TEXT,
        group_thread_id INTEGER, requester_id INTEGER,
        created TEXT DEFAULT CURRENT_TIMESTAMP, taken TEXT, done TEXT,
        hold_until TEXT, paid INTEGER DEFAULT 0, noscan INTEGER DEFAULT 0)''')
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

    defaults = [
        ('Билайн', 12, '⚙️'), ('МТС', 14, '🔴'), ('Мегафон', 10, '🟢'),
        ('Т2', 10, '⚪'), ('Сбер', 10, '🟡'), ('Газпром', 20, '🔵'), ('Добросвязь', 14, '🟣'),
    ]
    for name, price, emoji in defaults:
        c.execute('INSERT OR IGNORE INTO operators (name, price, emoji) VALUES (?, ?, ?)', (name, price, emoji))

    c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', ('auto_backup', 'off'))
    c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', ('default_mode', 'БХ'))
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
    waiting_for_mode = State()
    waiting_for_delete_order = State()

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
    c.execute('INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?', (key, value, value))
    conn.commit()
    conn.close()

def can_submit_phone(phone: str, user_id: int) -> bool:
    """Проверяет, можно ли сдать номер (не более 2 раз в сутки МСК)"""
    conn = get_db()
    c = conn.cursor()
    now_msk = datetime.now(MOSCOW_TZ)
    today_start = now_msk.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    today_end = now_msk.replace(hour=23, minute=59, second=59, microsecond=999999).isoformat()

    # Проверка по номеру телефона
    c.execute('SELECT COUNT(*) FROM phone_submissions WHERE phone = ? AND submitted BETWEEN ? AND ?',
              (phone, today_start, today_end))
    phone_count = c.fetchone()[0]

    # Проверка по пользователю
    c.execute('SELECT COUNT(*) FROM phone_submissions WHERE user_id = ? AND submitted BETWEEN ? AND ?',
              (user_id, today_start, today_end))
    user_count = c.fetchone()[0]

    conn.close()
    return phone_count < 2 and user_count < 5  # Максимум 2 одинаковых номера, 5 всего в день

def moscow_time():
    return datetime.now(MOSCOW_TZ)

def back_to_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")]
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
        await message.answer("✅ <b>Группа добавлена и бот активирован!</b>\nИспользуйте /esim БХ или /esim ХД для запроса.")
        return

    new_status = 0 if row['active'] == 1 else 1
    c.execute('UPDATE groups SET active = ? WHERE group_id = ?', (new_status, str(message.chat.id)))
    conn.commit()
    conn.close()
    if new_status:
        await message.answer("✅ <b>Бот активирован!</b>\n/esim БХ или /esim ХД")
    else:
        await message.answer("⏸️ <b>Бот отключён.</b>")

@dp.message(Command("esim"))
async def cmd_esim(message: Message):
    if message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await message.answer("Эта команда только для групп")
        return

    args = message.text.split()
    mode = get_setting('default_mode')
    if len(args) > 1 and args[1].upper() in ['БХ', 'ХД']:
        mode = args[1].upper()

    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM groups WHERE group_id = ? AND active = 1', (str(message.chat.id),))
    if not c.fetchone():
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
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"{op['emoji']} {op['name']} · {op['price']}$ · {mode}", callback_data=f"group_req_{op['id']}_{mode}")])
    await message.answer(f"<b>📱 ВЫБЕРИТЕ ОПЕРАТОРА · {mode}</b>", reply_markup=kb)

@dp.callback_query(F.data.startswith("group_req_"))
async def group_request(callback: CallbackQuery):
    parts = callback.data.split("_")
    op_id = int(parts[2])
    mode = parts[3] if len(parts) > 3 else get_setting('default_mode')

    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM operators WHERE id = ? AND active = 1', (op_id,))
    op = c.fetchone()
    conn.close()

    if not op:
        await callback.answer("❌ Оператор не найден или выключен")
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
    msg_text = f"<b>🔔 НОВЫЙ ЗАКАЗ #{order_id}</b>\n\n📱 <b>Оператор:</b> {op['emoji']} {op['name']}\n💰 <b>Цена:</b> {op['price']}$\n🎯 <b>Режим:</b> {mode}\n⏳ <b>Дедлайн:</b> 10 минут\n\n<i>Нажмите кнопку чтобы забрать заказ</i>"

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
        await message.answer("❌ Неверный формат. Укажите: +7XXXXXXXXXX или 8XXXXXXXXXX")
        return
    data = await state.get_data()
    file_id = data.get('qr_file_id')
    order_id = data.get('order_id')
    await save_esim(message, state, file_id, phone, order_id)

async def save_esim(message: Message, state: FSMContext, file_id: str, phone: str, order_id: int = None):
    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)

    # Проверка лимита
    if not can_submit_phone(phone, user_id):
        await message.answer("❌ <b>Лимит превышен!</b>\n\nНельзя сдавать один номер более 2 раз в сутки.\nЛимит сбрасывается в 00:00 МСК.")
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
        c.execute('SELECT * FROM orders WHERE id = ?', (order_id,))
        order = c.fetchone()
        c.execute('UPDATE users SET qr_month = qr_month + 1, total_qr = total_qr + 1 WHERE user_id = ?', (user_id,))

        # Запись в историю сдачи
        c.execute('INSERT INTO phone_submissions (phone, user_id, order_id, submitted) VALUES (?, ?, ?, ?)',
                  (phone, user_id, order_id, moscow_time().isoformat()))

        conn.commit()
        c.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        user = c.fetchone()
        conn.close()

        # Отправка в группу
        if order and order['group_id'] and order['requester_id']:
            try:
                pay_kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Встал", callback_data=f"status_{order_id}_up"),
                     InlineKeyboardButton(text="🚫 Блок", callback_data=f"status_{order_id}_block")],
                    [InlineKeyboardButton(text="❌ НеСкан", callback_data=f"status_{order_id}_noscan")],
                ])

                caption = f"<b>✅ ЗАКАЗ #{order_id} ВЫПОЛНЕН</b>\n\n📱 <b>Оператор:</b> {order['operator']}\n📞 <b>Номер:</b> <code>{phone}</code>\n👤 <b>Сдатчик:</b> @{message.from_user.username or user_id}\n🎯 <b>Режим:</b> {mode}"

                await bot.send_photo(
                    chat_id=order['group_id'],
                    photo=file_id,
                    caption=caption,
                    reply_markup=pay_kb
                )
            except Exception as e:
                logger.error(f"Ошибка отправки в группу: {e}")

        await message.answer(f"<b>✅ ESIM СДАН!</b>\n\n📱 <b>Номер:</b> <code>{phone}</code>\n📊 <b>QR за месяц:</b> {user['qr_month']}\n🏆 <b>Ранг:</b> {user['rank']}\n🎯 <b>Режим:</b> {mode}")
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
        await message.answer(f"<b>✅ ESIM СДАН!</b>\n\n📱 <b>Номер:</b> <code>{phone}</code>\n📊 <b>QR за месяц:</b> {user['qr_month']}\n🏆 <b>Ранг:</b> {user['rank']}")

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
        current_mode = order['mode']
        if current_mode == 'БХ':
            # Без холда - кнопка Оплатить
            pay_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💵 Оплатить", callback_data=f"pay_{order_id}")],
            ])
            await callback.message.edit_reply_markup(reply_markup=pay_kb)
            await callback.answer("✅ Статус: Встал. Нажмите Оплатить.")

            # Уведомление сдатчику
            try:
                await bot.send_message(executor_id, f"✅ <b>Заказ #{order_id}</b>\n📱 {order['operator']} · {order['phone']}\n\nСтатус: <b>ВСТАЛ</b>\nОжидайте оплату.")
            except:
                pass
        else:
            # Холд
            await callback.message.edit_caption(
                caption=callback.message.caption + f"\n\n⏳ <b>Холд до:</b> {order['hold_until'][:19] if order['hold_until'] else 'Н/Д'}",
                reply_markup=None
            )
            await callback.answer("✅ Статус: Встал. Выплата после холда.")

            try:
                await bot.send_message(executor_id, f"✅ <b>Заказ #{order_id}</b>\n📱 {order['operator']} · {order['phone']}\n\nСтатус: <b>ВСТАЛ</b>\nРежим: Холд. Выплата после {order['hold_until'][:19] if order['hold_until'] else 'Н/Д'}.")
            except:
                pass

    elif action == "block":
        c.execute('UPDATE orders SET status = ? WHERE id = ?', ('blocked', order_id))
        conn.commit()
        await callback.message.edit_caption(caption=callback.message.caption + "\n\n🚫 <b>БЛОК</b>", reply_markup=None)
        await callback.answer("🚫 Номер заблокирован")

        try:
            await bot.send_message(executor_id, f"🚫 <b>Заказ #{order_id}</b>\n📱 {order['operator']} · {order['phone']}\n\nСтатус: <b>БЛОК</b>")
        except:
            pass

    elif action == "noscan":
        c.execute('UPDATE orders SET status = ?, noscan = 1 WHERE id = ?', ('noscan', order_id))
        conn.commit()
        await callback.message.edit_caption(caption=callback.message.caption + "\n\n❌ <b>НеСкан</b>", reply_markup=None)
        await callback.answer("❌ НеСкан")

        try:
            await bot.send_message(executor_id, f"❌ <b>Заказ #{order_id}</b>\n📱 {order['operator']} · {order['phone']}\n\nСтатус: <b>НеСкан</b>")
        except:
            pass

    conn.close()

@dp.callback_query(F.data.startswith("pay_"))
async def pay_executor(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[1])
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM orders WHERE id = ?', (order_id,))
    order = c.fetchone()
    if not order:
        await callback.answer("Заказ не найден")
        conn.close()
        return
    if order['paid']:
        await callback.answer("Уже оплачено")
        conn.close()
        return

    c.execute('UPDATE orders SET paid = 1 WHERE id = ?', (order_id,))
    c.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (order['price'], order['executor_id']))
    conn.commit()
    c.execute('SELECT balance FROM users WHERE user_id = ?', (order['executor_id'],))
    user = c.fetchone()
    conn.close()

    try:
        await bot.send_message(order['executor_id'], f"💵 <b>Выплата #{order_id}</b>\n\n💰 <b>Сумма:</b> {order['price']}$\n📱 <b>Оператор:</b> {order['operator']}\n📞 <b>Номер:</b> {order['phone']}\n\n💎 <b>Баланс:</b> {user['balance']}$")
    except:
        pass

    await callback.message.edit_caption(caption=callback.message.caption + "\n\n💵 <b>ОПЛАЧЕНО</b>", reply_markup=None)
    await callback.answer("✅ Выплата отправлена!")

# ============ АДМИН: УДАЛЕНИЕ ЗАЯВОК ============
@dp.callback_query(F.data == "admin_delete_orders")
async def admin_delete_orders(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM orders ORDER BY created DESC LIMIT 30')
    orders = c.fetchall()
    conn.close()

    if not orders:
        await callback.message.edit_text("<b>🗑️ УДАЛЕНИЕ ЗАЯВОК</b>\n\n<i>Нет заявок</i>", reply_markup=back_to_admin())
        await callback.answer()
        return

    text = "<b>🗑️ УДАЛЕНИЕ ЗАЯВОК</b>\n\nВыберите заявку для удаления:\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for o in orders:
        status_emoji = {'active': '🟡', 'taken': '🔵', 'done': '🟢', 'blocked': '🚫', 'noscan': '❌'}.get(o['status'], '⚪')
        text += f"{status_emoji} #{o['id']} {o['operator']} · {o['price']}$\n"
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"🗑️ #{o['id']} {o['operator']}", callback_data=f"delorder_{o['id']}")])

    kb.inline_keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("delorder_"))
async def delete_order(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    order_id = int(callback.data.split("_")[1])
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM orders WHERE id = ?', (order_id,))
    conn.commit()
    conn.close()
    await callback.answer(f"Заявка #{order_id} удалена")
    await admin_delete_orders(callback)

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
        [InlineKeyboardButton(text="➕ Добавить", callback_data="admin_add_channel"),
         InlineKeyboardButton(text="🗑️ Удалить", callback_data="admin_del_channel")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "admin_add_channel")
async def admin_add_channel(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("Перешлите сообщение из канала или введите @username.")
    await state.set_state(AdminStates.waiting_for_channel)
    await callback.answer()

@dp.message(AdminStates.waiting_for_channel)
async def channel_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    channel_id, username = None, None
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
        await message.answer("Перешлите сообщение или введите @username")
        return
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO channels (channel_id, username) VALUES (?, ?)', (channel_id, username))
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
    await callback.answer("Каналы удалены")
    await callback.message.edit_text("<b>📢 КАНАЛЫ</b>\n\n<i>Все удалены.</i>", reply_markup=back_to_admin())

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
    if groups:
        for g in groups:
            status = "🟢" if g['active'] else "🔴"
            text += f"{status} {g['username'] or g['group_id']}\n"
    else:
        text += "<i>Нет групп</i>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_groups")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

# ============ АДМИН: ОПЕРАТОРЫ ============
@dp.callback_query(F.data == "admin_operators")
async def admin_operators(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    ops = get_all_operators()
    text = "<b>💰 ОПЕРАТОРЫ</b>\n\n"
    for op in ops:
        text += f"{'🟢' if op['active'] else '🔴'} {op['emoji']} {op['name']} · {op['price']}$\n"
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
    await callback.message.edit_text("Введите название оператора:")
    await state.set_state(AdminStates.waiting_for_operator_name)
    await callback.answer()

@dp.message(AdminStates.waiting_for_operator_name)
async def op_name_received(message: Message, state: FSMContext):
    await state.update_data(op_name=message.text)
    await message.answer("Введите цену ($):")
    await state.set_state(AdminStates.waiting_for_operator_price)

@dp.message(AdminStates.waiting_for_operator_price)
async def op_price_received(message: Message, state: FSMContext):
    try:
        float(message.text)
    except:
        await message.answer("❌ Введите число!")
        return
    await state.update_data(op_price=float(message.text))
    await message.answer("Отправьте эмодзи:")
    await state.set_state(AdminStates.waiting_for_operator_emoji)

@dp.message(AdminStates.waiting_for_operator_emoji)
async def op_emoji_received(message: Message, state: FSMContext):
    data = await state.get_data()
    emoji = message.text.strip()[0] if message.text else '📱'
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO operators (name, price, emoji) VALUES (?, ?, ?)', (data['op_name'], data['op_price'], emoji))
    conn.commit()
    conn.close()
    await message.answer(f"✅ {emoji} {data['op_name']} · {data['op_price']}$")
    await state.clear()

@dp.callback_query(F.data == "admin_edit_op")
async def admin_edit_op(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    ops = get_all_operators()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{op['emoji']} {op['name']} · {op['price']}$", callback_data=f"editop_{op['id']}")] for op in ops
    ] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]])
    await callback.message.edit_text("<b>Выберите оператора для изменения цены:</b>", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("editop_"))
async def edit_op_price(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.update_data(edit_op_id=int(callback.data.split("_")[1]))
    await callback.message.edit_text("Введите новую цену ($):")
    await state.set_state(AdminStates.waiting_for_edit_price)
    await callback.answer()

@dp.message(AdminStates.waiting_for_edit_price)
async def edit_price_received(message: Message, state: FSMContext):
    try:
        price = float(message.text)
    except:
        await message.answer("❌ Введите число!")
        return
    data = await state.get_data()
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE operators SET price = ? WHERE id = ?', (price, data['edit_op_id']))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Цена обновлена: {price}$")
    await state.clear()

@dp.callback_query(F.data == "admin_toggle_op")
async def admin_toggle_op(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    ops = get_all_operators()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{'🟢' if op['active'] else '🔴'} {op['emoji']} {op['name']}", callback_data=f"toggleop_{op['id']}")] for op in ops
    ] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]])
    await callback.message.edit_text("<b>Вкл/Выкл оператора:</b>", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("toggleop_"))
async def toggle_operator(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    op_id = int(callback.data.split("_")[1])
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT active FROM operators WHERE id = ?', (op_id,))
    row = c.fetchone()
    new_status = 0 if row['active'] == 1 else 1
    c.execute('UPDATE operators SET active = ? WHERE id = ?', (new_status, op_id))
    conn.commit()
    conn.close()
    await callback.answer(f"{'🟢 Вкл' if new_status else '🔴 Выкл'}")
    await admin_toggle_op(callback)

@dp.callback_query(F.data == "admin_del_op")
async def admin_del_op(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    ops = get_all_operators()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🗑️ {op['emoji']} {op['name']}", callback_data=f"delop_{op['id']}")] for op in ops
    ] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]])
    await callback.message.edit_text("<b>Удалить оператора:</b>", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("delop_"))
async def delete_operator(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM operators WHERE id = ?', (int(callback.data.split("_")[1]),))
    conn.commit()
    conn.close()
    await callback.answer("Удалён")
    await admin_del_op(callback)

# ============ АДМИН: РЕЖИМ ============
@dp.callback_query(F.data == "admin_mode")
async def admin_mode(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    current = get_setting('default_mode')
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{'🟢' if current=='БХ' else '⚪'} БХ (без холда)", callback_data="set_mode_БХ")],
        [InlineKeyboardButton(text=f"{'🟢' if current=='ХД' else '⚪'} ХД (холд {HOLD_HOURS}ч)", callback_data="set_mode_ХД")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")],
    ])
    await callback.message.edit_text(f"<b>🎯 РЕЖИМ СДАЧИ: {current}</b>\n\nБХ — без холда, оплата сразу\nХД — холд {HOLD_HOURS}ч, автосписание", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("set_mode_"))
async def set_mode(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    set_setting('default_mode', callback.data.split("_")[2])
    await callback.answer("Режим изменён")
    await admin_mode(callback)

# ============ АДМИН: БД ============
@dp.callback_query(F.data == "admin_db_export")
async def admin_db_export(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    try:
        await callback.message.answer_document(FSInputFile(DB_PATH), caption=f"📦 {moscow_time().strftime('%Y-%m-%d %H:%M')} МСК")
        await callback.answer("✅")
    except Exception as e:
        await callback.answer(f"Ошибка: {e}")

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
        await message.answer("❌ Нужен .db файл!")
        return
    try:
        await bot.download(message.document, destination=DB_PATH)
        init_db()
        await message.answer("✅ БД заменена!")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
    await state.clear()

@dp.callback_query(F.data == "admin_db_auto")
async def admin_db_auto(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    current = get_setting('auto_backup')
    set_setting('auto_backup', 'off' if current == 'on' else 'on')
    await callback.answer(f"Автобэкап: {'OFF' if current=='on' else 'ON'}")
    await callback.message.edit_reply_markup(reply_markup=admin_menu())

# ============ АДМИН: ЗАЯВКА ============
@dp.callback_query(F.data == "admin_create_order")
async def admin_create_order(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    mode = get_setting('default_mode')
    ops = get_operators()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{op['emoji']} {op['name']} · {op['price']}$ · {mode}", callback_data=f"order_op_{op['id']}_{mode}")] for op in ops
    ] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]])
    await callback.message.edit_text(f"<b>📱 СОЗДАТЬ ЗАЯВКУ · {mode}</b>", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("order_op_"))
async def admin_order_created(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    parts = callback.data.split("_")
    op_id = int(parts[2])
    mode = parts[3] if len(parts) > 3 else get_setting('default_mode')

    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM operators WHERE id = ? AND active = 1', (op_id,))
    op = c.fetchone()

    channels = get_active_channels()
    if not channels:
        await callback.answer("Нет каналов")
        conn.close()
        return

    c.execute('INSERT INTO orders (operator, price, mode, status) VALUES (?, ?, ?, ?)', (op['name'], op['price'], mode, 'active'))
    order_id = c.lastrowid
    conn.commit()
    conn.close()

    bot_username = (await bot.me()).username
    deep_link = f"https://t.me/{bot_username}?start=order_{order_id}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔥 ЗАБРАТЬ ЗАКАЗ", url=deep_link)]])
    msg_text = f"<b>🔔 НОВЫЙ ЗАКАЗ #{order_id}</b>\n\n📱 <b>Оператор:</b> {op['emoji']} {op['name']}\n💰 <b>Цена:</b> {op['price']}$\n🎯 <b>Режим:</b> {mode}"

    try:
        sent = await bot.send_message(chat_id=channels[0]['channel_id'], text=msg_text, reply_markup=kb)
        conn = get_db()
        c = conn.cursor()
        c.execute('UPDATE orders SET channel_msg_id = ? WHERE id = ?', (sent.message_id, order_id))
        conn.commit()
        conn.close()
        await callback.answer("✅")
        await callback.message.edit_text(f"✅ Заявка #{order_id} в канале.")
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
    c.execute('SELECT COUNT(*) FROM orders')
    total_orders = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM orders WHERE status = "done"')
    done = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM orders WHERE status = "active"')
    active = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM orders WHERE status = "taken"')
    taken = c.fetchone()[0]
    c.execute('SELECT COALESCE(SUM(price), 0) FROM orders WHERE status = "done" AND paid = 1')
    total_paid = c.fetchone()[0]
    c.execute('SELECT COALESCE(SUM(price), 0) FROM orders WHERE status = "done"')
    total_sum = c.fetchone()[0]
    conn.close()
    text = f"<b>📊 СТАТИСТИКА</b>\n\n👥 Исполнителей: {users}\n📱 Всего заявок: {total_orders}\n🟢 Активных: {active}\n🔵 В работе: {taken}\n✅ Выполнено: {done}\n💵 Сумма: {total_sum}$\n💰 Выплачено: {total_paid}$"
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
    if not users:
        text += "<i>Нет участников</i>"
    else:
        for u in users:
            text += f"• @{u['username'] or u['user_id']} | {u['rank']} | {u['total_qr']} QR | {u['balance']}$\n"
    await callback.message.edit_text(text, reply_markup=back_to_admin())
    await callback.answer()

# ============ АДМИН: РАССЫЛКА ============
@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("Введите текст рассылки:")
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
    count = 0
    for u in users:
        try:
            await bot.send_message(u['user_id'], message.text)
            count += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await message.answer(f"✅ {count}/{len(users)}")
    await state.clear()

# ============ НАВИГАЦИЯ ============
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

# ============ КНОПКИ МЕНЮ ============
@dp.callback_query(F.data == "profile")
async def profile(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user:
        ensure_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
        user = get_user(callback.from_user.id)
    text = f"<b>👤 ПРОФИЛЬ</b>\n\n🆔 @{user['username'] or user['user_id']}\n📊 <b>Ранг:</b> {user['rank']}\n💎 <b>Бонус:</b> +{user['bonus']}$\n📱 <b>QR за месяц:</b> {user['qr_month']}\n📈 <b>Всего:</b> {user['total_qr']}\n💵 <b>Баланс:</b> {user['balance']}$"
    await callback.message.edit_text(text, reply_markup=back_to_main())
    await callback.answer()

@dp.callback_query(F.data == "operators_list")
async def operators_list(callback: CallbackQuery):
    ops = get_operators()
    text = "<b>📊 ЦЕНЫ</b>\n\n"
    for op in ops:
        text += f"{op['emoji']} <b>{op['name']}</b> · {op['price']}$\n"
    if not ops:
        text += "<i>Нет активных операторов</i>"
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
            s = {'active': '🟡', 'taken': '🔵', 'done': '🟢', 'blocked': '🚫', 'noscan': '❌'}.get(o['status'], '⚪')
            text += f"{s} #{o['id']} {o['operator']} · <code>{o['phone'] or '—'}</code> · {o['mode']}\n"
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
    text = f"<b>👥 РЕФЕРАЛЫ</b>\n\n🔗 <code>{link}</code>\n\n👥 Рефералов: {refs}\n📊 Ранг: {user['rank']}\n💎 Бонус: +{user['bonus']}$\n📱 QR за месяц: {user['qr_month']}"
    await callback.message.edit_text(text, reply_markup=back_to_main())
    await callback.answer()

@dp.callback_query(F.data == "help")
async def help_cmd(callback: CallbackQuery):
    text = "<b>ℹ️ ПОМОЩЬ</b>\n\n<b>📱 Сдать ESIM:</b> нажмите кнопку в канале\n<b>📋 В группе:</b> /esim БХ или /esim ХД\n<b>🔄 Режимы:</b> БХ — без холда, ХД — холд 2ч\n\n<b>Команды группы:</b>\n/work, /esim [БХ|ХД], /orders, /rating"
    await callback.message.edit_text(text, reply_markup=back_to_main())
    await callback.answer()

@dp.message(F.text, F.chat.type == ChatType.PRIVATE)
async def unknown_message(message: Message):
    ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer("<b>🚀 ERWINS ESIM BOT</b>\n\nИспользуйте меню:", reply_markup=main_menu())

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
        c.execute("SELECT * FROM orders WHERE status = 'done' AND mode = 'ХД' AND paid = 0 AND hold_until <= ?", (datetime.now().isoformat(),))
        orders = c.fetchall()
        for o in orders:
            c.execute('UPDATE orders SET paid = 1 WHERE id = ?', (o['id'],))
            c.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (o['price'], o['executor_id']))
            conn.commit()
            try:
                await bot.send_message(o['executor_id'], f"💵 <b>Выплата #{o['id']} (холд)</b>\n\n💰 {o['price']}$\n📱 {o['operator']}\n📞 {o['phone']}")
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
