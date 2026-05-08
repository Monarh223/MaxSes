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
from PIL import Image

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
            created TEXT DEFAULT CURRENT_TIMESTAMP,
            taken TEXT,
            done TEXT
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
        CREATE TABLE IF NOT EXISTS button_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            button_name TEXT UNIQUE,
            text TEXT,
            emoji TEXT DEFAULT '',
            row INTEGER DEFAULT 0,
            position INTEGER DEFAULT 0
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS text_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text_key TEXT UNIQUE,
            content TEXT
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

    conn.commit()
    conn.close()

init_db()

# ============ FSM СОСТОЯНИЯ ============
class EsimUpload(StatesGroup):
    waiting_for_qr = State()
    waiting_for_phone = State()

class AdminStates(StatesGroup):
    waiting_for_channel = State()
    waiting_for_group = State()
    waiting_for_operator_name = State()
    waiting_for_operator_price = State()
    waiting_for_operator_emoji = State()
    waiting_for_button_name = State()
    waiting_for_button_text = State()
    waiting_for_button_emoji = State()
    waiting_for_db_file = State()
    waiting_for_broadcast = State()
    waiting_for_text_key = State()
    waiting_for_text_content = State()
    waiting_for_edit_operator = State()
    waiting_for_edit_price = State()
    waiting_for_delete_operator = State()
    waiting_for_mode = State()

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

def get_text(key: str) -> str:
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT content FROM text_config WHERE text_key = ?', (key,))
    row = c.fetchone()
    conn.close()
    return row['content'] if row else key

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

def get_active_groups():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM groups WHERE active = 1')
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

def generate_qr_bytes(lpa_string: str) -> io.BytesIO:
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_H,
                       box_size=10, border=4)
    qr.add_data(lpa_string)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = io.BytesIO()
    img.save(bio, 'PNG')
    bio.seek(0)
    return bio

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

def get_button_configs():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM button_config ORDER BY row, position')
    rows = c.fetchall()
    conn.close()
    return rows

async def update_channel_order_msg(order_id: int, text: str, kb=None):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT channel_msg_id FROM orders WHERE id = ?', (order_id,))
    order = c.fetchone()
    conn.close()

    if order and order['channel_msg_id']:
        channels = get_active_channels()
        for ch in channels:
            try:
                await bot.edit_message_text(
                    chat_id=ch['channel_id'],
                    message_id=order['channel_msg_id'],
                    text=text,
                    reply_markup=kb
                )
                break
            except:
                continue

# ============ КЛАВИАТУРЫ ============
def main_menu():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="📱 Сдать ESIM", callback_data="sdat_esim")],
        [InlineKeyboardButton(text="📋 Мои номера", callback_data="my_numbers")],
        [InlineKeyboardButton(text="📊 Операторы и цены", callback_data="operators_list")],
        [InlineKeyboardButton(text="👥 Рефералы", callback_data="referral")],
        [InlineKeyboardButton(text="ℹ️ Помощь", callback_data="help")],
    ])
    return kb

def admin_menu():
    auto = get_setting('auto_backup')
    auto_text = f"💾 БД: Автовыгрузка [{auto.upper()}]"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Кнопки", callback_data="admin_buttons")],
        [InlineKeyboardButton(text="📋 Тексты", callback_data="admin_texts")],
        [InlineKeyboardButton(text="💰 Операторы и цены", callback_data="admin_operators")],
        [InlineKeyboardButton(text="📢 Каналы", callback_data="admin_channels")],
        [InlineKeyboardButton(text="👥 Группы", callback_data="admin_groups")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="👤 Участники", callback_data="admin_users")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="📱 Создать заявку", callback_data="admin_create_order")],
        [InlineKeyboardButton(text="🎯 Режим сдачи", callback_data="admin_mode")],
        [InlineKeyboardButton(text="💾 БД: Выгрузка", callback_data="admin_db_export")],
        [InlineKeyboardButton(text="💾 БД: Загрузка", callback_data="admin_db_import")],
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

    # Проверяем реферальную ссылку
    if len(args) > 1 and args[1].startswith("ref_"):
        referrer_id = int(args[1].replace("ref_", ""))
        if referrer_id != message.from_user.id:
            conn = get_db()
            c = conn.cursor()
            c.execute('INSERT OR IGNORE INTO referrals (referrer_id, referral_id) VALUES (?, ?)',
                      (referrer_id, message.from_user.id))
            conn.commit()
            conn.close()

    # Проверяем deep link на заказ
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
            await message.answer("❌ Заказ уже занят другим исполнителем.")
            conn.close()
            return

        # Занять заказ
        c.execute('UPDATE orders SET status = ?, executor_id = ?, taken = ? WHERE id = ?',
                  ('taken', message.from_user.id, datetime.now().isoformat(), order_id))
        conn.commit()
        conn.close()

        # Обновить сообщение в канале
        await update_channel_order_msg(
            order_id,
            f"<b>🔒 ЗАКАЗ #{order_id} ЗАНЯТ</b>\n\n"
            f"📱 <b>Оператор:</b> {order['operator']}\n"
            f"💰 <b>Цена:</b> {order['price']}$\n"
            f"👤 <b>Исполнитель:</b> @{message.from_user.username or message.from_user.id}\n"
            f"⏳ <b>Ожидание сдачи...</b>"
        )

        await message.answer(
            f"<b>✅ ЗАКАЗ #{order_id} ПРИНЯТ!</b>\n\n"
            f"📱 {order['operator']} · {order['price']}$\n\n"
            f"<b>Отправьте фото QR-кода и укажите номер телефона.</b>"
        )
        await state.set_state(EsimUpload.waiting_for_qr)
        await state.update_data(order_id=order_id)
        return

    # Обычный /start
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

    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM groups WHERE group_id = ?', (str(message.chat.id),))
    row = c.fetchone()

    if not row:
        await message.answer("⚠️ Бот не настроен для этой группы. Админ должен добавить группу через /admin.")
        conn.close()
        return

    new_status = 0 if row['active'] == 1 else 1
    c.execute('UPDATE groups SET active = ? WHERE group_id = ?', (new_status, str(message.chat.id)))
    conn.commit()
    conn.close()

    if new_status:
        await message.answer("✅ <b>Бот активирован в группе!</b>\nИспользуйте /esim для запроса номеров.")
    else:
        await message.answer("⏸️ <b>Бот отключён в группе.</b>")

# ============ /esim (в группе) ============
@dp.message(Command("esim"))
async def cmd_esim(message: Message):
    if message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await message.answer("Эта команда только для групп")
        return

    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM groups WHERE group_id = ? AND active = 1', (str(message.chat.id),))
    if not c.fetchone():
        conn.close()
        await message.answer("⏸️ Бот не активен в группе. /work для включения.")
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

# ============ /orders (активные заявки) ============
@dp.message(Command("orders"))
async def cmd_orders(message: Message):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM orders WHERE status = "active" ORDER BY created DESC LIMIT 10')
    orders = c.fetchall()
    conn.close()

    if not orders:
        await message.answer("📭 Нет активных заявок.")
        return

    text = "<b>📋 АКТИВНЫЕ ЗАЯВКИ</b>\n\n"
    for o in orders:
        text += f"#{o['id']} · {o['operator']} · {o['price']}$ · {o['mode']}\n"

    await message.answer(text)

# ============ /rating ============
@dp.message(Command("rating"))
async def cmd_rating(message: Message):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT username, first_name, total_qr, qr_month, rank FROM users ORDER BY total_qr DESC LIMIT 20')
    users = c.fetchall()
    conn.close()

    if not users:
        await message.answer("Пока нет данных.")
        return

    text = "<b>🏆 РЕЙТИНГ ИСПОЛНИТЕЛЕЙ</b>\n\n"
    for i, u in enumerate(users, 1):
        name = u['username'] or u['first_name'] or u'—'
        text += f"{i}. {name} · {u['total_qr']} QR · {u['rank']}\n"

    await message.answer(text)

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
        await callback.answer("Нет настроенных каналов для заявок")
        return

    # Создаём заявку
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT INTO orders (operator, price, mode, status) VALUES (?, ?, ?, ?)',
              (op['name'], op['price'], get_setting('default_mode'), 'active'))
    order_id = c.lastrowid
    conn.commit()
    conn.close()

    # Формируем ссылку на бота
    bot_username = (await bot.me()).username
    deep_link = f"https://t.me/{bot_username}?start=order_{order_id}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 ЗАБРАТЬ ЗАКАЗ", url=deep_link)]
    ])

    msg_text = (
        f"<b>🔔 НОВЫЙ ЗАКАЗ #{order_id}</b>\n\n"
        f"📱 <b>Оператор:</b> {op['emoji']} {op['name']}\n"
        f"💰 <b>Цена:</b> {op['price']}$\n"
        f"🎯 <b>Режим:</b> {get_setting('default_mode')}\n"
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
        await callback.message.edit_text(
            callback.message.text + f"\n\n✅ Заявка #{order_id} отправлена в канал."
        )
    except Exception as e:
        await callback.answer(f"Ошибка: {e}")

# ============ СДАТЬ ESIM (ЛИЧКА) ============
@dp.callback_query(F.data == "sdat_esim")
async def sdat_esim_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("<b>📱 СДАЧА ESIM</b>\n\nОтправьте фото QR-кода.")
    await state.set_state(EsimUpload.waiting_for_qr)
    await state.update_data(order_id=None)
    await callback.answer()

@dp.callback_query(F.data.startswith("sdat_for_"))
async def sdat_for_order(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split("_")[2])
    await callback.message.answer("<b>📱 СДАЧА ESIM</b>\n\nОтправьте фото QR-кода.")
    await state.set_state(EsimUpload.waiting_for_qr)
    await state.update_data(order_id=order_id)
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
    await message.answer("📱 Укажите номер телефона в российском формате (+7XXXXXXXXXX или 8XXXXXXXXXX)")

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

@dp.message(EsimUpload.waiting_for_qr, F.text)
async def esim_text_instead_qr(message: Message):
    await message.answer("❌ Отправьте фото QR-кода, а не текст.")

async def save_esim(message: Message, state: FSMContext, file_id: str, phone: str,
                    order_id: int = None):
    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)

    conn = get_db()
    c = conn.cursor()

    if order_id:
        # Обновляем заказ
        c.execute('UPDATE orders SET status = ?, phone = ?, qr_file_id = ?, done = ? WHERE id = ?',
                  ('done', phone, file_id, datetime.now().isoformat(), order_id))
        c.execute('SELECT * FROM orders WHERE id = ?', (order_id,))
        order = c.fetchone()

        # Обновить счётчик
        c.execute('UPDATE users SET qr_month = qr_month + 1, total_qr = total_qr + 1 WHERE user_id = ?',
                  (user_id,))

        conn.commit()
        c.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        user = c.fetchone()
        conn.close()

        bonus = user['bonus'] if user else 0
        qr_month = user['qr_month'] if user else 0

        # Обновить сообщение в канале
        if order:
            await update_channel_order_msg(
                order_id,
                f"<b>✅ ЗАКАЗ #{order_id} ВЫПОЛНЕН</b>\n\n"
                f"📱 {order['operator']} · <code>{phone}</code>\n"
                f"👤 @{message.from_user.username or user_id}"
            )

        await message.answer(
            f"<b>✅ ESIM СДАН!</b>\n\n"
            f"📱 <b>Номер:</b> <code>{phone}</code>\n"
            f"📊 <b>Зачтено QR за месяц:</b> {qr_month}\n"
            f"💵 <b>Бонус:</b> +{bonus}$ к каждому QR\n"
            f"🏆 <b>Ранг:</b> {user['rank'] if user else 'Старт'}"
        )
    else:
        # Просто сохраняем в базу без привязки к заказу
        c.execute('INSERT INTO orders (operator, price, mode, status, executor_id, phone, qr_file_id, done) '
                  'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                  ('Неизвестно', 0, get_setting('default_mode'), 'done', user_id, phone, file_id,
                   datetime.now().isoformat()))
        c.execute('UPDATE users SET qr_month = qr_month + 1, total_qr = total_qr + 1 WHERE user_id = ?',
                  (user_id,))
        conn.commit()
        c.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        user = c.fetchone()
        conn.close()

        bonus = user['bonus'] if user else 0
        qr_month = user['qr_month'] if user else 0

        await message.answer(
            f"<b>✅ ESIM СДАН!</b>\n\n"
            f"📱 <b>Номер:</b> <code>{phone}</code>\n"
            f"📊 <b>Зачтено QR за месяц:</b> {qr_month}\n"
            f"💵 <b>Бонус:</b> +{bonus}$ к каждому QR\n"
            f"🏆 <b>Ранг:</b> {user['rank'] if user else 'Старт'}\n\n"
            f"⚠️ Заявка не была привязана к заказу."
        )

    await state.clear()

# ============ АДМИН: КАНАЛЫ ============
@dp.callback_query(F.data == "admin_channels")
async def admin_channels(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    channels = get_active_channels()
    text = "<b>📢 НАСТРОЙКА КАНАЛОВ</b>\n\n"
    if channels:
        for ch in channels:
            text += f"• {ch['username'] or ch['channel_id']}\n"
    else:
        text += "<i>Нет каналов</i>\n\n"
        text += "<b>Как добавить:</b>\n1. Добавьте бота в канал администратором\n2. Перешлите сообщение из канала сюда"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить канал", callback_data="admin_add_channel")],
        [InlineKeyboardButton(text="🗑️ Удалить канал", callback_data="admin_del_channel")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "admin_add_channel")
async def admin_add_channel(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text(
        "Перешлите любое сообщение из канала или введите @username канала.",
        reply_markup=back_to_admin()
    )
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
            await message.answer("❌ Не удалось найти канал. Проверьте что бот добавлен в него.")
            return
    else:
        await message.answer("Перешлите сообщение из канала или введите @username")
        return

    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO channels (channel_id, username) VALUES (?, ?)',
              (channel_id, username))
    conn.commit()
    conn.close()

    await message.answer(f"✅ Канал {username or channel_id} добавлен!")
    await state.clear()

@dp.callback_query(F.data == "admin_del_channel")
async def admin_del_channel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM channels')
    conn.commit()
    conn.close()
    await callback.answer("Все каналы удалены.")
    await callback.message.edit_text("<b>📢 НАСТРОЙКА КАНАЛОВ</b>\n\n<i>Все каналы удалены.</i>",
                                     reply_markup=back_to_admin())

# ============ АДМИН: ГРУППЫ ============
@dp.callback_query(F.data == "admin_groups")
async def admin_groups(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM groups')
    groups = c.fetchall()
    conn.close()

    text = "<b>👥 НАСТРОЙКА ГРУПП</b>\n\n"
    if groups:
        for g in groups:
            status = "🟢 Активна" if g['active'] else "🔴 Неактивна"
            text += f"• {g['username'] or g['group_id']} — {status}\n"
    else:
        text += "<i>Нет групп</i>"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить группу", callback_data="admin_add_group")],
        [InlineKeyboardButton(text="🗑️ Удалить все", callback_data="admin_del_groups")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "admin_add_group")
async def admin_add_group(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text(
        "Добавьте бота в группу и введите её @username или перешлите сообщение из группы.",
        reply_markup=back_to_admin()
    )
    await state.set_state(AdminStates.waiting_for_group)
    await callback.answer()

@dp.message(AdminStates.waiting_for_group)
async def group_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    group_id = None
    username = None

    if message.forward_from_chat:
        group_id = str(message.forward_from_chat.id)
        username = message.forward_from_chat.username
    elif message.text:
        username = message.text.strip()
        try:
            chat = await bot.get_chat(username)
            group_id = str(chat.id)
        except:
            await message.answer("❌ Не удалось найти группу.")
            return
    else:
        await message.answer("Введите @username группы или перешлите сообщение.")
        return

    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO groups (group_id, username) VALUES (?, ?)',
              (group_id, username))
    conn.commit()
    conn.close()

    await message.answer(f"✅ Группа добавлена! В группе напишите /work для активации.")
    await state.clear()

@dp.callback_query(F.data == "admin_del_groups")
async def admin_del_groups(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM groups')
    conn.commit()
    conn.close()
    await callback.answer("Все группы удалены.")
    await callback.message.edit_text("<b>👥 НАСТРОЙКА ГРУПП</b>\n\n<i>Все группы удалены.</i>",
                                     reply_markup=back_to_admin())

# ============ АДМИН: ОПЕРАТОРЫ ============
@dp.callback_query(F.data == "admin_operators")
async def admin_operators(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    ops = get_all_operators()
    text = "<b>💰 ОПЕРАТОРЫ И ЦЕНЫ</b>\n\n"
    for op in ops:
        status = "🟢" if op['active'] else "🔴"
        text += f"{status} {op['emoji']} {op['name']} — {op['price']}$\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data="admin_add_op")],
        [InlineKeyboardButton(text="✏️ Изменить цену", callback_data="admin_edit_op")],
        [InlineKeyboardButton(text="🔄 Вкл/Выкл", callback_data="admin_toggle_op")],
        [InlineKeyboardButton(text="🗑️ Удалить", callback_data="admin_del_op")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "admin_add_op")
async def admin_add_op(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("Введите название оператора:", reply_markup=back_to_admin())
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
        price = float(message.text)
    except:
        await message.answer("❌ Введите число!")
        return
    await state.update_data(op_price=price)
    await message.answer("Отправьте эмодзи для оператора:")
    await state.set_state(AdminStates.waiting_for_operator_emoji)

@dp.message(AdminStates.waiting_for_operator_emoji)
async def op_emoji_received(message: Message, state: FSMContext):
    data = await state.get_data()
    emoji = message.text.strip()[0] if message.text else '📱'
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO operators (name, price, emoji) VALUES (?, ?, ?)',
              (data['op_name'], data['op_price'], emoji))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Оператор {emoji} {data['op_name']} · {data['op_price']}$ добавлен!")
    await state.clear()

@dp.callback_query(F.data == "admin_edit_op")
async def admin_edit_op(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    ops = get_all_operators()
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for op in ops:
        kb.inline_keyboard.append([
            InlineKeyboardButton(
                text=f"{op['emoji']} {op['name']} · {op['price']}$",
                callback_data=f"editop_{op['id']}"
            )
        ])
    kb.inline_keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")])
    await callback.message.edit_text("<b>Выберите оператора для изменения цены:</b>", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("editop_"))
async def edit_op_price(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    op_id = int(callback.data.split("_")[1])
    await state.update_data(edit_op_id=op_id)
    await callback.message.edit_text("Введите новую цену ($):", reply_markup=back_to_admin())
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
    op_id = data['edit_op_id']
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE operators SET price = ? WHERE id = ?', (price, op_id))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Цена обновлена: {price}$")
    await state.clear()

@dp.callback_query(F.data == "admin_toggle_op")
async def admin_toggle_op(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    ops = get_all_operators()
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for op in ops:
        status = "🟢" if op['active'] else "🔴"
        kb.inline_keyboard.append([
            InlineKeyboardButton(
                text=f"{status} {op['emoji']} {op['name']}",
                callback_data=f"toggleop_{op['id']}"
            )
        ])
    kb.inline_keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")])
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
    await callback.answer(f"Статус изменён на {'🟢 Вкл' if new_status else '🔴 Выкл'}")
    await admin_toggle_op(callback)

@dp.callback_query(F.data == "admin_del_op")
async def admin_del_op(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    ops = get_all_operators()
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for op in ops:
        kb.inline_keyboard.append([
            InlineKeyboardButton(
                text=f"🗑️ {op['emoji']} {op['name']}",
                callback_data=f"delop_{op['id']}"
            )
        ])
    kb.inline_keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")])
    await callback.message.edit_text("<b>Выберите оператора для удаления:</b>", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("delop_"))
async def delete_operator(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    op_id = int(callback.data.split("_")[1])
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM operators WHERE id = ?', (op_id,))
    conn.commit()
    conn.close()
    await callback.answer("Оператор удалён.")
    await admin_del_op(callback)

# ============ АДМИН: РЕЖИМ СДАЧИ ============
@dp.callback_query(F.data == "admin_mode")
async def admin_mode(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    current = get_setting('default_mode')
    text = f"<b>🎯 РЕЖИМ СДАЧИ</b>\n\nТекущий режим: <b>{current}</b>\n\n"
    if current == 'БХ':
        text += "• <b>БХ</b> — без холда, оплачивается даже 5 минут\n• ХД — с холдом"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 БХ (без холда)", callback_data="set_mode_БХ")],
        [InlineKeyboardButton(text="🟡 ХД (с холдом)", callback_data="set_mode_ХД")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("set_mode_"))
async def set_mode(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    mode = callback.data.split("_")[2]
    set_setting('default_mode', mode)
    await callback.answer(f"Режим изменён на {mode}")
    await admin_mode(callback)

# ============ АДМИН: БД ============
@dp.callback_query(F.data == "admin_db_export")
async def admin_db_export(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    try:
        await callback.message.answer_document(
            FSInputFile(DB_PATH),
            caption=f"📦 БД от {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        await callback.answer("✅ Выгружено!")
    except Exception as e:
        await callback.answer(f"Ошибка: {e}")

@dp.callback_query(F.data == "admin_db_import")
async def admin_db_import(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("Отправьте файл .db для замены базы данных.")
    await state.set_state(AdminStates.waiting_for_db_file)
    await callback.answer()

@dp.message(AdminStates.waiting_for_db_file, F.document)
async def db_file_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    doc = message.document
    if not doc.file_name.endswith('.db'):
        await message.answer("❌ Нужен .db файл!")
        return
    try:
        await bot.download(doc, destination=DB_PATH)
        init_db()
        await message.answer("✅ База данных заменена и перезагружена!")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
    await state.clear()

@dp.callback_query(F.data == "admin_db_auto")
async def admin_db_auto(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    current = get_setting('auto_backup')
    new = 'off' if current == 'on' else 'on'
    set_setting('auto_backup', new)
    await callback.answer(f"Автовыгрузка: {new.upper()}")
    await callback.message.edit_reply_markup(reply_markup=admin_menu())

# ============ АДМИН: СОЗДАТЬ ЗАЯВКУ ============
@dp.callback_query(F.data == "admin_create_order")
async def admin_create_order(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
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
        await callback.answer("Нет каналов для заявок")
        conn.close()
        return

    c.execute('INSERT INTO orders (operator, price, mode, status) VALUES (?, ?, ?, ?)',
              (op['name'], op['price'], get_setting('default_mode'), 'active'))
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
        f"🎯 <b>Режим:</b> {get_setting('default_mode')}\n"
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
        await callback.message.edit_text(f"✅ Заявка #{order_id} отправлена в канал.")
    except Exception as e:
        await callback.answer(f"Ошибка: {e}")

# ============ АДМИН: СТАТИСТИКА ============
@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM users')
    users_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM orders')
    orders_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM orders WHERE status = "done"')
    done_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM orders WHERE status = "active"')
    active_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM orders WHERE status = "taken"')
    taken_count = c.fetchone()[0]
    c.execute('SELECT SUM(price) FROM orders WHERE status = "done"')
    total_sum = c.fetchone()[0] or 0
    conn.close()

    text = (
        f"<b>📊 СТАТИСТИКА</b>\n\n"
        f"👥 <b>Исполнителей:</b> {users_count}\n"
        f"📱 <b>Заявок всего:</b> {orders_count}\n"
        f"🟢 <b>Активных:</b> {active_count}\n"
        f"🔵 <b>В работе:</b> {taken_count}\n"
        f"✅ <b>Выполнено:</b> {done_count}\n"
        f"💵 <b>Общая сумма:</b> {total_sum}$"
    )
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
        name = u['username'] or u['first_name'] or str(u['user_id'])
        text += f"• @{name} | {u['rank']} | {u['total_qr']} QR | {u['qr_month']} мес\n"

    await callback.message.edit_text(text, reply_markup=back_to_admin())
    await callback.answer()

# ============ АДМИН: КНОПКИ ============
@dp.callback_query(F.data == "admin_buttons")
async def admin_buttons(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    buttons = get_button_configs()
    text = "<b>📝 НАСТРОЙКА КНОПОК</b>\n\n"
    if buttons:
        for b in buttons:
            text += f"• {b['emoji']} {b['text']} — <code>{b['button_name']}</code>\n"
    else:
        text += "<i>Нет кастомных кнопок. Кнопки по умолчанию активны.</i>"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить кнопку", callback_data="admin_add_button")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "admin_add_button")
async def admin_add_button(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("Введите название кнопки (латиница, без пробелов):")
    await state.set_state(AdminStates.waiting_for_button_name)
    await callback.answer()

@dp.message(AdminStates.waiting_for_button_name)
async def button_name_received(message: Message, state: FSMContext):
    await state.update_data(btn_name=message.text.strip())
    await message.answer("Введите текст кнопки:")
    await state.set_state(AdminStates.waiting_for_button_text)

@dp.message(AdminStates.waiting_for_button_text)
async def button_text_received(message: Message, state: FSMContext):
    await state.update_data(btn_text=message.text.strip())
    await message.answer("Отправьте эмодзи для кнопки:")
    await state.set_state(AdminStates.waiting_for_button_emoji)

@dp.message(AdminStates.waiting_for_button_emoji)
async def button_emoji_received(message: Message, state: FSMContext):
    data = await state.get_data()
    emoji = message.text.strip()[0] if message.text else ''
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO button_config (button_name, text, emoji) VALUES (?, ?, ?)',
              (data['btn_name'], data['btn_text'], emoji))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Кнопка {emoji} {data['btn_text']} добавлена!")
    await state.clear()

# ============ АДМИН: ТЕКСТЫ ============
@dp.callback_query(F.data == "admin_texts")
async def admin_texts(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM text_config')
    texts = c.fetchall()
    conn.close()

    text = "<b>📋 НАСТРОЙКА ТЕКСТОВ</b>\n\n"
    if texts:
        for t in texts:
            text += f"• <code>{t['text_key']}</code>\n"
    else:
        text += "<i>Нет кастомных текстов.</i>"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить текст", callback_data="admin_add_text")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "admin_add_text")
async def admin_add_text(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("Введите ключ текста (латиница):")
    await state.set_state(AdminStates.waiting_for_text_key)
    await callback.answer()

@dp.message(AdminStates.waiting_for_text_key)
async def text_key_received(message: Message, state: FSMContext):
    await state.update_data(text_key=message.text.strip())
    await message.answer("Введите содержимое текста:")
    await state.set_state(AdminStates.waiting_for_text_content)

@dp.message(AdminStates.waiting_for_text_content)
async def text_content_received(message: Message, state: FSMContext):
    data = await state.get_data()
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO text_config (text_key, content) VALUES (?, ?)',
              (data['text_key'], message.text))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Текст <code>{data['text_key']}</code> сохранён!")
    await state.clear()

# ============ АДМИН: РАССЫЛКА ============
@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("Введите сообщение для рассылки:")
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

    await message.answer(f"✅ Рассылка отправлена: {count}/{len(users)}")
    await state.clear()

# ============ НАВИГАЦИЯ ============
@dp.callback_query(F.data == "back_main")
async def back_main(callback: CallbackQuery):
    if callback.message.chat.type == ChatType.PRIVATE:
        await callback.message.edit_text(
            "<b>🚀 ERWINS ESIM BOT</b>\n\nВыберите действие:",
            reply_markup=main_menu()
        )
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
        f"💎 <b>Бонус:</b> +{user['bonus']}$ к каждому QR\n"
        f"📱 <b>Зачтено QR за месяц:</b> {user['qr_month']}\n"
        f"📈 <b>Всего QR:</b> {user['total_qr']}\n"
        f"💵 <b>Баланс:</b> {user['balance']}$"
    )
    await callback.message.edit_text(text, reply_markup=back_to_main())
    await callback.answer()

# ============ ОПЕРАТОРЫ ============
@dp.callback_query(F.data == "operators_list")
async def operators_list(callback: CallbackQuery):
    ops = get_operators()
    text = "<b>📊 ОПЕРАТОРЫ И ЦЕНЫ</b>\n\n"
    for op in ops:
        text += f"{op['emoji']} <b>{op['name']}</b> · {op['price']}$\n"
    await callback.message.edit_text(text, reply_markup=back_to_main())
    await callback.answer()

# ============ МОИ НОМЕРА ============
@dp.callback_query(F.data == "my_numbers")
async def my_numbers(callback: CallbackQuery):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM orders WHERE executor_id = ? ORDER BY created DESC LIMIT 50',
              (callback.from_user.id,))
    orders = c.fetchall()
    conn.close()

    if not orders:
        text = "<b>📋 МОИ НОМЕРА</b>\n\n<i>Нет сданных номеров</i>"
    else:
        text = "<b>📋 МОИ НОМЕРА</b>\n\n"
        for o in orders:
            status_map = {'active': '🟡', 'taken': '🔵', 'done': '🟢'}
            s = status_map.get(o['status'], '⚪')
            phone = o['phone'] or '—'
            text += f"{s} #{o['id']} {o['operator']} · <code>{phone}</code>\n"

    await callback.message.edit_text(text, reply_markup=back_to_main())
    await callback.answer()

# ============ РЕФЕРАЛЫ ============
@dp.callback_query(F.data == "referral")
async def referral(callback: CallbackQuery):
    ensure_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    link = f"https://t.me/{(await bot.me()).username}?start=ref_{callback.from_user.id}"
    user = get_user(callback.from_user.id)

    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM referrals WHERE referrer_id = ?', (callback.from_user.id,))
    ref_count = c.fetchone()[0]
    conn.close()

    text = (
        f"<b>👥 РЕФЕРАЛЬНАЯ СИСТЕМА</b>\n\n"
        f"🔗 Ваша ссылка:\n<code>{link}</code>\n\n"
        f"👥 <b>Рефералов:</b> {ref_count}\n"
        f"📊 <b>Ранг:</b> {user['rank']}\n"
        f"💎 <b>Бонус:</b> +{user['bonus']}$ к каждому QR\n"
        f"📱 <b>QR за месяц:</b> {user['qr_month']}"
    )
    await callback.message.edit_text(text, reply_markup=back_to_main())
    await callback.answer()

# ============ ПОМОЩЬ ============
@dp.callback_query(F.data == "help")
async def help_cmd(callback: CallbackQuery):
    text = (
        "<b>ℹ️ ПОМОЩЬ</b>\n\n"
        "<b>📱 Как сдать ESIM:</b>\n"
        "1. Нажмите «Сдать ESIM»\n"
        "2. Отправьте фото QR-кода\n"
        "3. Укажите номер телефона\n\n"
        "<b>🔥 Как взять заказ:</b>\n"
        "• В канале нажмите «ЗАБРАТЬ ЗАКАЗ»\n"
        "• В группе: /esim → выбрать оператора\n\n"
        "<b>📋 Команды группы:</b>\n"
        "/work — вкл/выкл бота\n"
        "/esim — запросить номер\n"
        "/orders — активные заявки\n"
        "/rating — рейтинг исполнителей\n\n"
        "<b>❓ Команды бота:</b>\n"
        "/start — главное меню\n"
        "/emjid — узнать ID эмодзи\n"
        "/admin — админ-панель"
    )
    await callback.message.edit_text(text, reply_markup=back_to_main())
    await callback.answer()

# ============ ОБРАБОТКА НЕИЗВЕСТНЫХ СООБЩЕНИЙ ============
@dp.message(F.text, F.chat.type == ChatType.PRIVATE)
async def unknown_message(message: Message):
    ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer(
        "<b>🚀 ERWINS ESIM BOT</b>\n\nИспользуйте меню:",
        reply_markup=main_menu()
    )

# ============ АВТОБЭКАП ============
async def auto_backup_task():
    while True:
        await asyncio.sleep(3600)
        if get_setting('auto_backup') == 'on':
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_path = os.path.join(BACKUP_DIR, f'backup_{timestamp}.db')
            shutil.copy2(DB_PATH, backup_path)
            logger.info(f"Автобэкап сохранён: {backup_path}")

            # Удаляем старые бэкапы (> 48 штук)
            backups = sorted(os.listdir(BACKUP_DIR))
            while len(backups) > 48:
                old = backups.pop(0)
                os.remove(os.path.join(BACKUP_DIR, old))

# ============ ЗАПУСК ============
async def main():
    logger.info("Бот запущен")
    asyncio.create_task(auto_backup_task())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
