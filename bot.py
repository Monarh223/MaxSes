import asyncio
import logging
import os
import re
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional, Iterable

import pytz
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

# ============================================================
# DIAMOND ESIM BOT — SINGLE FILE VERSION
# Aiogram 3.x | SQLite | GitHub + Railway ready
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "0").replace(" ", "").split(",") if x.strip().isdigit()]
SEND_USERNAME = os.getenv("SEND_USERNAME", "").replace("@", "").strip()
DB_PATH = os.getenv("DB_PATH", "esim_bot.db")
BACKUP_DIR = os.getenv("BACKUP_DIR", "backups")
BOT_TITLE = os.getenv("BOT_TITLE", "DIAMOND ESIM")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден. Добавьте BOT_TOKEN в .env или переменные Railway.")

MOSCOW_TZ = pytz.timezone("Europe/Moscow")
DEFAULT_HOLD_HOURS = 2
DEFAULT_MIN_WITHDRAW = 10.0
DEFAULT_SUBMIT_TIMEOUT = 300
DEFAULT_MAX_WARNINGS = 3
DEFAULT_BLOCK_HOURS = 1

os.makedirs(BACKUP_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("diamond_esim")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# ============================================================
# FSM
# ============================================================

class EsimUpload(StatesGroup):
    waiting_for_qr = State()
    waiting_for_phone = State()

class AdminStates(StatesGroup):
    waiting_for_channel = State()
    waiting_for_operator_name = State()
    waiting_for_operator_bh = State()
    waiting_for_operator_hd = State()
    waiting_for_operator_emoji = State()
    waiting_for_edit_bh = State()
    waiting_for_edit_hd = State()
    waiting_for_db_file = State()
    waiting_for_broadcast = State()
    waiting_for_pay_user = State()
    waiting_for_pay_amount = State()
    waiting_for_deduct_user = State()
    waiting_for_deduct_amount = State()
    waiting_for_setting_key = State()
    waiting_for_setting_value = State()
    waiting_for_user_manage = State()

# ============================================================
# DATABASE
# ============================================================

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

def moscow_time() -> datetime:
    return datetime.now(MOSCOW_TZ)

def moscow_iso() -> str:
    return moscow_time().isoformat(timespec="seconds")

@contextmanager
def db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            rank TEXT DEFAULT 'Старт',
            bonus REAL DEFAULT 0.0,
            qr_month INTEGER DEFAULT 0,
            total_qr INTEGER DEFAULT 0,
            balance REAL DEFAULT 0.0,
            expected_balance REAL DEFAULT 0.0,
            pending_balance REAL DEFAULT 0.0,
            joined TEXT DEFAULT CURRENT_TIMESTAMP,
            warnings INTEGER DEFAULT 0,
            blocked_until TEXT,
            banned INTEGER DEFAULT 0,
            note TEXT
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS orders (
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
            requester_id INTEGER,
            created TEXT DEFAULT CURRENT_TIMESTAMP,
            taken TEXT,
            done TEXT,
            hold_until TEXT,
            paid INTEGER DEFAULT 0,
            credited INTEGER DEFAULT 0,
            blocked INTEGER DEFAULT 0,
            noscan INTEGER DEFAULT 0,
            taken_at TEXT,
            order_group_msg_id INTEGER,
            retry_count INTEGER DEFAULT 0
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS operators (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            price_bh REAL DEFAULT 0,
            price_hd REAL DEFAULT 0,
            emoji TEXT DEFAULT '📱',
            active_bh INTEGER DEFAULT 1,
            active_hd INTEGER DEFAULT 1
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT UNIQUE,
            username TEXT,
            invite_link TEXT
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT UNIQUE,
            username TEXT,
            active INTEGER DEFAULT 0
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referral_id INTEGER UNIQUE,
            created TEXT DEFAULT CURRENT_TIMESTAMP
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS phone_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT,
            user_id INTEGER,
            order_id INTEGER,
            submitted TEXT
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS balance_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            type TEXT,
            description TEXT,
            created TEXT DEFAULT CURRENT_TIMESTAMP
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS admin_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            action TEXT,
            details TEXT,
            created TEXT DEFAULT CURRENT_TIMESTAMP
        )''')

        # Soft migrations for old DBs
        migrations = [
            ("users", "banned", "INTEGER DEFAULT 0"),
            ("users", "note", "TEXT"),
            ("operators", "active_bh", "INTEGER DEFAULT 1"),
            ("operators", "active_hd", "INTEGER DEFAULT 1"),
            ("channels", "invite_link", "TEXT"),
            ("orders", "retry_count", "INTEGER DEFAULT 0"),
        ]
        for table, column, definition in migrations:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError:
                pass

        defaults = [
            ('Билайн', 12, 10, '⚙️'),
            ('МТС', 14, 12, '🔴'),
            ('Мегафон', 10, 8, '🟢'),
            ('Т2', 10, 8, '⚪'),
            ('Сбер', 10, 8, '🟡'),
            ('Газпром', 20, 18, '🔵'),
            ('Добросвязь', 14, 12, '🟣'),
        ]
        for name, bh, hd, emoji in defaults:
            c.execute('''INSERT OR IGNORE INTO operators
                (name, price_bh, price_hd, emoji, active_bh, active_hd)
                VALUES (?, ?, ?, ?, 1, 1)''', (name, bh, hd, emoji))

        default_settings = {
            "auto_backup": "off",
            "work_day": "on",
            "hold_hours": str(DEFAULT_HOLD_HOURS),
            "min_withdraw": str(DEFAULT_MIN_WITHDRAW),
            "submit_timeout": str(DEFAULT_SUBMIT_TIMEOUT),
            "max_warnings": str(DEFAULT_MAX_WARNINGS),
            "block_hours": str(DEFAULT_BLOCK_HOURS),
            "daily_phone_limit": "5",
            "same_phone_daily_limit": "2",
        }
        for key, value in default_settings.items():
            c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, value))

        c.execute('CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_orders_executor ON orders(executor_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_phone_sub_phone ON phone_submissions(phone, submitted)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_phone_sub_user ON phone_submissions(user_id, submitted)')

init_db()

# ============================================================
# HELPERS
# ============================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def get_setting(key: str, default: str = "") -> str:
    with db_conn() as conn:
        row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default

def set_setting(key: str, value: str):
    with db_conn() as conn:
        conn.execute('''INSERT INTO settings (key, value) VALUES (?, ?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value''', (key, value))

def setting_int(key: str, default: int) -> int:
    try:
        return int(float(get_setting(key, str(default))))
    except Exception:
        return default

def setting_float(key: str, default: float) -> float:
    try:
        return float(get_setting(key, str(default)))
    except Exception:
        return default

def is_work_day() -> bool:
    return get_setting("work_day", "on") == "on"

def log_admin(admin_id: int, action: str, details: str = ""):
    with db_conn() as conn:
        conn.execute('INSERT INTO admin_logs (admin_id, action, details) VALUES (?, ?, ?)', (admin_id, action, details))

def ensure_user(user_id: int, username: Optional[str] = None, first_name: Optional[str] = None):
    with db_conn() as conn:
        row = conn.execute('SELECT user_id FROM users WHERE user_id=?', (user_id,)).fetchone()
        if not row:
            conn.execute('INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)', (user_id, username, first_name))
        else:
            conn.execute('UPDATE users SET username=?, first_name=? WHERE user_id=?', (username, first_name, user_id))

def get_user(user_id: int):
    with db_conn() as conn:
        return conn.execute('SELECT * FROM users WHERE user_id=?', (user_id,)).fetchone()

def get_operators():
    with db_conn() as conn:
        return conn.execute('SELECT * FROM operators ORDER BY id').fetchall()

def get_active_channels():
    with db_conn() as conn:
        return conn.execute('SELECT * FROM channels ORDER BY id').fetchall()

def format_money(value) -> str:
    try:
        n = float(value)
        return f"{int(n)}$" if n.is_integer() else f"{n:.2f}$"
    except Exception:
        return f"{value}$"

def format_phone(phone: str) -> Optional[str]:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return f"+7{digits[1:]}"
    if len(digits) == 10 and digits[0] == "9":
        return f"+7{digits}"
    return None

def parse_amount(text: str) -> Optional[float]:
    if not text:
        return None
    text = text.replace(",", ".").strip()
    try:
        value = float(text)
        return value if value >= 0 else None
    except ValueError:
        return None

def user_label(user) -> str:
    if not user:
        return "—"
    username = user['username'] if 'username' in user.keys() else None
    user_id = user['user_id'] if 'user_id' in user.keys() else None
    return f"@{username}" if username else str(user_id)

def rank_by_total(total_qr: int) -> tuple[str, float]:
    if total_qr >= 500:
        return "💠 Diamond", 2.0
    if total_qr >= 200:
        return "👑 VIP", 1.0
    if total_qr >= 100:
        return "⭐ Pro", 0.5
    if total_qr >= 30:
        return "🔥 Active", 0.2
    return "🌱 Старт", 0.0

def recalc_rank(user_id: int):
    with db_conn() as conn:
        row = conn.execute('SELECT total_qr FROM users WHERE user_id=?', (user_id,)).fetchone()
        if row:
            rank, bonus = rank_by_total(row['total_qr'])
            conn.execute('UPDATE users SET rank=?, bonus=? WHERE user_id=?', (rank, bonus, user_id))

def is_user_blocked(user_id: int) -> bool:
    user = get_user(user_id)
    if not user:
        return False
    if user['banned']:
        return True
    if user['blocked_until']:
        return now_iso() < user['blocked_until']
    return False

def can_submit_phone(phone: str, user_id: int) -> bool:
    same_limit = setting_int("same_phone_daily_limit", 2)
    user_limit = setting_int("daily_phone_limit", 5)
    now = moscow_time()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    end = now.replace(hour=23, minute=59, second=59, microsecond=999999).isoformat()
    with db_conn() as conn:
        same_count = conn.execute('''SELECT COUNT(*) FROM phone_submissions
                                     WHERE phone=? AND submitted BETWEEN ? AND ?''', (phone, start, end)).fetchone()[0]
        if same_count >= same_limit:
            return False
        user_count = conn.execute('''SELECT COUNT(*) FROM phone_submissions
                                     WHERE user_id=? AND submitted BETWEEN ? AND ?''', (user_id, start, end)).fetchone()[0]
        if user_count >= user_limit:
            return False
    return True

async def safe_edit_text(msg: Message, text: str, reply_markup=None):
    try:
        await msg.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            try:
                await msg.answer(text, reply_markup=reply_markup)
            except Exception:
                logger.exception("safe_edit_text fallback failed")
    except Exception:
        logger.exception("safe_edit_text failed")

async def safe_edit_caption(msg: Message, caption: str, reply_markup=None):
    try:
        await msg.edit_caption(caption=caption, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.warning("safe_edit_caption failed: %s", e)
    except Exception:
        logger.exception("safe_edit_caption failed")

async def notify_admins(text: str):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass

# ============================================================
# KEYBOARDS
# ============================================================

def kb(rows: list[list[tuple[str, str, bool]]]) -> InlineKeyboardMarkup:
    inline = []
    for row in rows:
        buttons = []
        for text, data, is_url in row:
            if is_url:
                buttons.append(InlineKeyboardButton(text=text, url=data))
            else:
                buttons.append(InlineKeyboardButton(text=text, callback_data=data))
        inline.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=inline)

def back_to_main():
    return kb([[('⬅️ Главное меню', 'back_main', False)]])

def back_to_admin():
    return kb([[('⬅️ Админ-панель', 'admin_back', False)]])

def back_to_profile():
    return kb([[('⬅️ Профиль', 'profile', False)]])

def confirm_kb(action: str, back_to: str = "admin_back"):
    return kb([
        [('✅ Подтвердить', action, False)],
        [('❌ Отмена', back_to, False)],
    ])

def get_channel_link() -> Optional[str]:
    channels = get_active_channels()
    if not channels:
        return None
    ch = channels[0]
    username = ch['username']
    invite = ch['invite_link']
    if username:
        clean = username.replace('@', '')
        return f"https://t.me/{clean}"
    return invite

def main_menu():
    rows = [
        [('👤 Профиль', 'profile', False), ('📋 Мои номера', 'my_numbers', False)],
        [('📊 Цены', 'operators_list', False), ('🏆 ТОП', 'top_users', False)],
        [('👥 Рефералы', 'referral', False), ('ℹ️ Помощь', 'help', False)],
    ]
    link = get_channel_link()
    if link:
        rows.insert(0, [('📱 Сдать ESIM / Канал заказов', link, True)])
    else:
        rows.insert(0, [('📱 Сдать ESIM', 'no_channel', False)])
    return kb(rows)

def admin_menu():
    wd = '🟢 Раб.день' if is_work_day() else '🔴 День закрыт'
    auto = get_setting('auto_backup', 'off').upper()
    return kb([
        [('💰 Операторы', 'admin_operators', False), ('📢 Каналы', 'admin_channels', False)],
        [('👥 Группы', 'admin_groups', False), (wd, 'admin_workday', False)],
        [('📊 Статистика', 'admin_stats', False), ('👤 Участники', 'admin_users', False)],
        [('💵 Выплаты', 'admin_payouts', False), ('📱 Создать заявку', 'admin_create_order', False)],
        [('🗑️ Заявки', 'admin_delete_orders', False), ('📢 Рассылка', 'admin_broadcast', False)],
        [('⚙️ Настройки', 'admin_settings', False), ('🚫 Баны', 'admin_bans', False)],
        [('💾 БД', 'admin_db_menu', False), (f'🔄 Backup {auto}', 'admin_db_auto', False)],
        [('✖️ Закрыть', 'close', False)],
    ])

def order_status_kb(order_id: int):
    return kb([
        [('✅ Засчитать', f'status:{order_id}:up', False), ('🚫 Блок', f'status:{order_id}:block', False)],
        [('❌ НеСкан', f'status:{order_id}:noscan', False)],
    ])

# ============================================================
# TEXT TEMPLATES
# ============================================================

def title_block(title: str) -> str:
    return f"<b>💎 {BOT_TITLE}</b>\n<b>{title}</b>"

def order_card(order_id: int, emoji: str, operator: str, price: float, mode: str, retry: bool = False) -> str:
    head = "🔄 ПОВТОР ЗАКАЗА" if retry else "🔥 НОВЫЙ ЗАКАЗ"
    return (
        f"<b>{head} #{order_id}</b>\n\n"
        f"📱 <b>Оператор:</b> {emoji} {operator}\n"
        f"💰 <b>Выплата:</b> {format_money(price)}\n"
        f"🎯 <b>Режим:</b> {mode}\n"
        f"⏳ <b>Время:</b> {setting_int('submit_timeout', DEFAULT_SUBMIT_TIMEOUT) // 60} мин\n\n"
        f"⚡ Нажмите кнопку ниже, чтобы забрать заказ."
    )

# ============================================================
# COMMON COMMANDS
# ============================================================

@dp.message(Command("cancel"))
async def cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("✅ Действие отменено.", reply_markup=main_menu() if message.chat.type == ChatType.PRIVATE else None)

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    args = message.text.split(maxsplit=1)
    payload = args[1] if len(args) > 1 else ""

    if payload.startswith("ref_"):
        try:
            referrer_id = int(payload.replace("ref_", ""))
            if referrer_id != message.from_user.id:
                with db_conn() as conn:
                    conn.execute('INSERT OR IGNORE INTO referrals (referrer_id, referral_id) VALUES (?, ?)', (referrer_id, message.from_user.id))
        except Exception:
            pass

    if payload.startswith("order_"):
        await take_order_from_payload(message, state, payload)
        return

    await message.answer(
        f"<b>💎 {BOT_TITLE}</b>\n\n"
        "⚡ Быстрая сдача ESIM\n"
        "📊 Актуальные цены\n"
        "👤 Профиль, баланс и история\n\n"
        "Выберите действие:",
        reply_markup=main_menu(),
    )

async def take_order_from_payload(message: Message, state: FSMContext, payload: str):
    if not is_work_day():
        await message.answer("🔴 <b>Рабочий день завершён.</b>")
        return
    if is_user_blocked(message.from_user.id):
        await message.answer("🚫 <b>Вы временно заблокированы или забанены.</b>")
        return
    try:
        order_id = int(payload.replace("order_", ""))
    except ValueError:
        await message.answer("❌ Некорректная ссылка заказа.")
        return

    with db_conn() as conn:
        order = conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
        if not order or order['status'] != 'active':
            await message.answer("❌ Заказ не найден или уже занят.")
            return
        conn.execute('''UPDATE orders
                        SET status='taken', executor_id=?, taken=?, taken_at=?
                        WHERE id=? AND status='active' ''',
                     (message.from_user.id, now_iso(), now_iso(), order_id))

    for ch in get_active_channels():
        try:
            if order['channel_msg_id']:
                await bot.delete_message(chat_id=ch['channel_id'], message_id=order['channel_msg_id'])
        except Exception:
            pass

    await message.answer(
        f"<b>✅ ЗАКАЗ #{order_id} ПРИНЯТ</b>\n\n"
        f"📱 <b>{order['operator']}</b>\n"
        f"💰 {format_money(order['price'])}\n"
        f"🎯 {order['mode']}\n"
        f"⏳ {setting_int('submit_timeout', DEFAULT_SUBMIT_TIMEOUT)//60} минут на сдачу\n\n"
        f"Отправьте фото QR-кода. Номер можно указать в подписи к фото.",
        reply_markup=kb([[('📤 Сдать QR', f'sdat_for:{order_id}', False)]])
    )
    await state.set_state(EsimUpload.waiting_for_qr)
    await state.update_data(order_id=order_id)

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещён")
        return
    await message.answer(f"<b>🛠️ {BOT_TITLE} — АДМИН-ПАНЕЛЬ</b>", reply_markup=admin_menu())

@dp.message(Command("work"))
async def cmd_work(message: Message):
    if message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только админ")
        return
    group_id = str(message.chat.id)
    username = message.chat.username or message.chat.title or group_id
    with db_conn() as conn:
        row = conn.execute('SELECT * FROM groups WHERE group_id=?', (group_id,)).fetchone()
        if not row:
            conn.execute('INSERT INTO groups (group_id, username, active) VALUES (?, ?, 1)', (group_id, username))
            await message.answer("✅ <b>Группа добавлена и активирована.</b>\nКоманда для заявок: /esim")
            return
        new_status = 0 if row['active'] else 1
        conn.execute('UPDATE groups SET active=?, username=? WHERE group_id=?', (new_status, username, group_id))
    await message.answer("✅ <b>Бот активирован в группе.</b>" if new_status else "⏸️ <b>Бот отключён в группе.</b>")

@dp.message(Command("esim"))
async def cmd_esim(message: Message):
    if message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return
    if not is_work_day():
        await message.answer("🔴 <b>Рабочий день завершён.</b>")
        return
    with db_conn() as conn:
        active = conn.execute('SELECT active FROM groups WHERE group_id=?', (str(message.chat.id),)).fetchone()
    if not active or not active['active']:
        await message.answer("⏸️ <b>Бот не активен в этой группе.</b> Админ: /work")
        return
    parts = message.text.split()
    if len(parts) > 1 and parts[1].upper() in ['БХ', 'ХД']:
        await show_operators(message, parts[1].upper(), edit=False)
        return
    await message.answer(
        "<b>📱 ВЫБЕРИТЕ ТИП СДАЧИ</b>",
        reply_markup=kb([
            [('🟢 БХ — Без холда', 'esim_mode:БХ', False)],
            [('🟡 ХД — Холд', 'esim_mode:ХД', False)],
        ])
    )

# ============================================================
# GROUP ORDER FLOW
# ============================================================

@dp.callback_query(F.data.startswith("esim_mode:"))
async def esim_mode_selected(callback: CallbackQuery):
    mode = callback.data.split(":", 1)[1]
    await show_operators(callback.message, mode, edit=True)
    await callback.answer()

async def show_operators(message: Message, mode: str, edit: bool = True):
    price_field = 'price_bh' if mode == 'БХ' else 'price_hd'
    active_field = 'active_bh' if mode == 'БХ' else 'active_hd'
    rows = []
    for op in get_operators():
        active = op[active_field] == 1
        status = '✅' if active else '❌'
        callback_data = f"greq:{op['id']}:{mode}" if active else "noop"
        rows.append([(f"{status} {op['emoji']} {op['name']} · {format_money(op[price_field])}", callback_data, False)])
    rows.append([('⬅️ Назад', 'close', False)])
    text = f"<b>📱 ОПЕРАТОРЫ · {mode}</b>\n\nВыберите активного оператора:"
    if edit:
        await safe_edit_text(message, text, reply_markup=kb(rows))
    else:
        await message.answer(text, reply_markup=kb(rows))

@dp.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer("❌ Недоступно", show_alert=False)

@dp.callback_query(F.data.startswith("greq:"))
async def group_request(callback: CallbackQuery):
    if not is_work_day():
        await callback.answer("🔴 Рабочий день завершён")
        return
    _, op_id_str, mode = callback.data.split(":")
    op_id = int(op_id_str)
    with db_conn() as conn:
        op = conn.execute('SELECT * FROM operators WHERE id=?', (op_id,)).fetchone()
    if not op:
        await callback.answer("❌ Оператор не найден")
        return
    active_field = 'active_bh' if mode == 'БХ' else 'active_hd'
    price = op['price_bh'] if mode == 'БХ' else op['price_hd']
    if op[active_field] != 1:
        await callback.answer("❌ Этот режим отключён")
        return
    channels = get_active_channels()
    if not channels:
        await callback.answer("Нет канала для заявок", show_alert=True)
        return
    with db_conn() as conn:
        cur = conn.execute('''INSERT INTO orders
            (operator, price, mode, status, group_id, requester_id, order_group_msg_id)
            VALUES (?, ?, ?, 'active', ?, ?, ?)''',
            (op['name'], price, mode, str(callback.message.chat.id), callback.from_user.id, callback.message.message_id))
        order_id = cur.lastrowid
    bot_username = (await bot.me()).username
    deep_link = f"https://t.me/{bot_username}?start=order_{order_id}"
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔥 ЗАБРАТЬ ЗАКАЗ", url=deep_link)]])
    try:
        sent = await bot.send_message(channels[0]['channel_id'], order_card(order_id, op['emoji'], op['name'], price, mode), reply_markup=markup)
        with db_conn() as conn:
            conn.execute('UPDATE orders SET channel_msg_id=? WHERE id=?', (sent.message_id, order_id))
        await safe_edit_text(
            callback.message,
            f"<b>✅ ЗАЯВКА #{order_id} СОЗДАНА</b>\n\n"
            f"📱 {op['emoji']} <b>{op['name']}</b>\n"
            f"💰 {format_money(price)}\n"
            f"🎯 {mode}\n\n"
            f"<i>Ожидайте исполнителя.</i>"
        )
        await callback.answer("✅ Заявка создана")
    except Exception as e:
        logger.exception("group_request failed")
        await callback.answer(f"Ошибка создания заявки", show_alert=True)
        with db_conn() as conn:
            conn.execute('DELETE FROM orders WHERE id=?', (order_id,))

# ============================================================
# ESIM SUBMISSION
# ============================================================

@dp.callback_query(F.data == "no_channel")
async def no_channel(callback: CallbackQuery):
    await callback.answer("Канал ещё не настроен", show_alert=True)

@dp.callback_query(F.data.startswith("sdat_for:"))
async def sdat_for_order(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":", 1)[1])
    await state.set_state(EsimUpload.waiting_for_qr)
    await state.update_data(order_id=order_id)
    await callback.message.answer("📤 Отправьте фото QR-кода. Номер можно добавить в подпись.")
    await callback.answer()

@dp.message(EsimUpload.waiting_for_qr, F.photo)
async def esim_qr_received(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get('order_id')
    file_id = message.photo[-1].file_id
    if message.caption:
        phone = format_phone(message.caption)
        if phone:
            await save_esim(message, state, file_id, phone, order_id)
            return
    await state.update_data(qr_file_id=file_id)
    await state.set_state(EsimUpload.waiting_for_phone)
    await message.answer("📱 Укажите номер телефона в формате +7XXXXXXXXXX")

@dp.message(EsimUpload.waiting_for_qr)
async def esim_qr_wrong(message: Message):
    await message.answer("❌ Нужно отправить именно фото QR-кода.")

@dp.message(EsimUpload.waiting_for_phone)
async def esim_phone_received(message: Message, state: FSMContext):
    phone = format_phone(message.text or "")
    if not phone:
        await message.answer("❌ Неверный формат. Пример: +79991234567")
        return
    data = await state.get_data()
    await save_esim(message, state, data.get('qr_file_id'), phone, data.get('order_id'))

async def save_esim(message: Message, state: FSMContext, file_id: str, phone: str, order_id: Optional[int]):
    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)
    if is_user_blocked(user_id):
        await message.answer("🚫 Вы временно заблокированы или забанены.")
        await state.clear()
        return
    if not can_submit_phone(phone, user_id):
        await message.answer("❌ Лимит сдачи превышен. Сброс в 00:00 МСК.")
        await state.clear()
        return

    if order_id:
        with db_conn() as conn:
            order = conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
            if not order:
                await message.answer("❌ Заказ не найден.")
                await state.clear()
                return
            if order['executor_id'] != user_id:
                await message.answer("❌ Этот заказ закреплён за другим исполнителем.")
                await state.clear()
                return
            if order['status'] != 'taken':
                await message.answer("❌ Заказ уже обработан или возвращён.")
                await state.clear()
                return
            bonus = get_user(user_id)['bonus'] if get_user(user_id) else 0
            total_price = float(order['price']) + float(bonus or 0)
            hold_hours = setting_int('hold_hours', DEFAULT_HOLD_HOURS)
            hold_until = (datetime.now() + timedelta(hours=hold_hours)).isoformat(timespec="seconds") if order['mode'] == 'ХД' else None
            conn.execute('''UPDATE orders SET status='done', phone=?, qr_file_id=?, done=?, hold_until=?, price=? WHERE id=?''',
                         (phone, file_id, now_iso(), hold_until, total_price, order_id))
            conn.execute('''UPDATE users SET qr_month=qr_month+1, total_qr=total_qr+1,
                            pending_balance=pending_balance+? WHERE user_id=?''', (total_price, user_id))
            conn.execute('''INSERT INTO phone_submissions (phone, user_id, order_id, submitted)
                            VALUES (?, ?, ?, ?)''', (phone, user_id, order_id, moscow_iso()))
        recalc_rank(user_id)
        user = get_user(user_id)
        if order['group_id']:
            try:
                await bot.send_photo(
                    chat_id=order['group_id'],
                    photo=file_id,
                    caption=(
                        f"<b>✅ ЗАКАЗ #{order_id} ВЫПОЛНЕН</b>\n\n"
                        f"📱 <b>{order['operator']}</b>\n"
                        f"📞 <code>{phone}</code>\n"
                        f"👤 @{message.from_user.username or user_id}\n"
                        f"🎯 {order['mode']}\n"
                        f"💰 {format_money(total_price)}"
                    ),
                    reply_markup=order_status_kb(order_id),
                )
            except Exception:
                logger.exception("Не удалось отправить выполненный заказ в группу")
        await message.answer(
            f"<b>✅ ESIM СДАН</b>\n\n"
            f"📱 <code>{phone}</code>\n"
            f"📊 QR за месяц: {user['qr_month']}\n"
            f"💎 Предварительно: {format_money(user['pending_balance'])}\n"
            f"🏅 Ранг: {user['rank']}"
        )
    else:
        with db_conn() as conn:
            conn.execute('''INSERT INTO orders (operator, price, mode, status, executor_id, phone, qr_file_id, done)
                            VALUES ('—', 0, 'БХ', 'done', ?, ?, ?, ?)''', (user_id, phone, file_id, now_iso()))
            conn.execute('UPDATE users SET qr_month=qr_month+1, total_qr=total_qr+1 WHERE user_id=?', (user_id,))
            conn.execute('INSERT INTO phone_submissions (phone, user_id, order_id, submitted) VALUES (?, ?, NULL, ?)', (phone, user_id, moscow_iso()))
        recalc_rank(user_id)
        await message.answer(f"<b>✅ ESIM СДАН</b>\n\n📱 <code>{phone}</code>")
    await state.clear()

# ============================================================
# ORDER STATUS
# ============================================================

@dp.callback_query(F.data.startswith("status:"))
async def order_status_action(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        # В группе обычно статус может нажимать заказчик/админ. Оставляем только админов для безопасности.
        await callback.answer("⛔ Только админ", show_alert=True)
        return
    _, order_id_str, action = callback.data.split(":")
    order_id = int(order_id_str)
    with db_conn() as conn:
        order = conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
        if not order:
            await callback.answer("Не найден")
            return
        if order['credited'] or order['blocked'] or order['noscan']:
            await callback.answer("Уже обработано")
            return
        executor_id = order['executor_id']
        price = float(order['price'])
        if action == 'up':
            if order['mode'] == 'ХД':
                conn.execute('UPDATE orders SET credited=1, blocked=0, noscan=0 WHERE id=?', (order_id,))
                conn.execute('UPDATE users SET pending_balance=MAX(0, pending_balance-?), expected_balance=expected_balance+? WHERE user_id=?', (price, price, executor_id))
                user_msg = f"✅ <b>#{order_id} ЗАСЧИТАН</b>\n💰 {format_money(price)} → ожидает холда"
            else:
                conn.execute('UPDATE orders SET credited=1, paid=1, blocked=0, noscan=0 WHERE id=?', (order_id,))
                conn.execute('UPDATE users SET pending_balance=MAX(0, pending_balance-?), balance=balance+? WHERE user_id=?', (price, price, executor_id))
                conn.execute('INSERT INTO balance_history (user_id, amount, type, description) VALUES (?, ?, ?, ?)', (executor_id, price, 'credit', f'БХ #{order_id}'))
                user_msg = f"✅ <b>#{order_id} ЗАСЧИТАН</b>\n💰 {format_money(price)} начислено на баланс"
            caption_add = "\n\n✅ <b>ЗАСЧИТАНО</b>"
        elif action == 'block':
            conn.execute('UPDATE orders SET credited=0, blocked=1, noscan=0 WHERE id=?', (order_id,))
            conn.execute('UPDATE users SET pending_balance=MAX(0, pending_balance-?) WHERE user_id=?', (price, executor_id))
            user_msg = f"🚫 <b>#{order_id} БЛОК</b>\nСнято: {format_money(price)}"
            caption_add = "\n\n🚫 <b>БЛОК</b>"
        elif action == 'noscan':
            conn.execute('UPDATE orders SET credited=0, blocked=0, noscan=1 WHERE id=?', (order_id,))
            conn.execute('UPDATE users SET pending_balance=MAX(0, pending_balance-?) WHERE user_id=?', (price, executor_id))
            user_msg = f"❌ <b>#{order_id} НеСкан</b>\nСнято: {format_money(price)}"
            caption_add = "\n\n❌ <b>НеСкан</b>"
        else:
            await callback.answer("Неизвестное действие")
            return
    await safe_edit_caption(callback.message, (callback.message.caption or "") + caption_add, reply_markup=None)
    await callback.answer("Готово")
    try:
        await bot.send_message(executor_id, user_msg)
    except Exception:
        pass

# ============================================================
# ADMIN: WORKDAY / OPERATORS
# ============================================================

@dp.callback_query(F.data == "admin_workday")
async def admin_workday(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    if is_work_day():
        await safe_edit_text(callback.message, "<b>🔴 Завершить рабочий день?</b>\n\nОжидаемые выплаты будут начислены на баланс.", reply_markup=confirm_kb("confirm_end_workday"))
    else:
        await safe_edit_text(callback.message, "<b>🟢 Начать рабочий день?</b>", reply_markup=confirm_kb("confirm_start_workday"))
    await callback.answer()

@dp.callback_query(F.data == "confirm_end_workday")
async def confirm_end_workday(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    with db_conn() as conn:
        conn.execute('''INSERT INTO balance_history (user_id, amount, type, description)
                        SELECT user_id, expected_balance, 'payout', 'Закрытие рабочего дня'
                        FROM users WHERE expected_balance > 0''')
        conn.execute('UPDATE users SET balance=balance+expected_balance, expected_balance=0 WHERE expected_balance > 0')
    set_setting('work_day', 'off')
    log_admin(callback.from_user.id, 'end_workday')
    await safe_edit_text(callback.message, "🔴 <b>Рабочий день завершён.</b> Выплаты начислены.", reply_markup=back_to_admin())
    await callback.answer()

@dp.callback_query(F.data == "confirm_start_workday")
async def confirm_start_workday(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    set_setting('work_day', 'on')
    log_admin(callback.from_user.id, 'start_workday')
    await safe_edit_text(callback.message, "🟢 <b>Рабочий день начат.</b>", reply_markup=back_to_admin())
    await callback.answer()

@dp.callback_query(F.data == "admin_operators")
async def admin_operators(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    text = "<b>💰 ОПЕРАТОРЫ</b>\n\n"
    for op in get_operators():
        text += f"{op['emoji']} <b>{op['name']}</b>\nБХ: {'🟢' if op['active_bh'] else '🔴'} {format_money(op['price_bh'])} | ХД: {'🟢' if op['active_hd'] else '🔴'} {format_money(op['price_hd'])}\n\n"
    await safe_edit_text(callback.message, text, reply_markup=kb([
        [('➕ Добавить', 'admin_add_op', False), ('✏️ Цены', 'admin_edit_op', False)],
        [('🔄 БХ Вкл/Выкл', 'admin_toggle_bh', False), ('🔄 ХД Вкл/Выкл', 'admin_toggle_hd', False)],
        [('🗑️ Удалить', 'admin_del_op', False)],
        [('⬅️ Назад', 'admin_back', False)],
    ]))
    await callback.answer()

@dp.callback_query(F.data == "admin_add_op")
async def admin_add_op(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.waiting_for_operator_name)
    await safe_edit_text(callback.message, "Введите название оператора:")
    await callback.answer()

@dp.message(AdminStates.waiting_for_operator_name)
async def op_name_received(message: Message, state: FSMContext):
    await state.update_data(op_name=message.text.strip())
    await state.set_state(AdminStates.waiting_for_operator_bh)
    await message.answer("Цена БХ ($):")

@dp.message(AdminStates.waiting_for_operator_bh)
async def op_bh_received(message: Message, state: FSMContext):
    amount = parse_amount(message.text)
    if amount is None:
        await message.answer("❌ Введите число.")
        return
    await state.update_data(op_bh=amount)
    await state.set_state(AdminStates.waiting_for_operator_hd)
    await message.answer("Цена ХД ($):")

@dp.message(AdminStates.waiting_for_operator_hd)
async def op_hd_received(message: Message, state: FSMContext):
    amount = parse_amount(message.text)
    if amount is None:
        await message.answer("❌ Введите число.")
        return
    await state.update_data(op_hd=amount)
    await state.set_state(AdminStates.waiting_for_operator_emoji)
    await message.answer("Эмодзи оператора:")

@dp.message(AdminStates.waiting_for_operator_emoji)
async def op_emoji_received(message: Message, state: FSMContext):
    data = await state.get_data()
    emoji = message.text.strip()[0] if message.text else '📱'
    with db_conn() as conn:
        conn.execute('''INSERT INTO operators (name, price_bh, price_hd, emoji, active_bh, active_hd)
                        VALUES (?, ?, ?, ?, 1, 1)
                        ON CONFLICT(name) DO UPDATE SET price_bh=excluded.price_bh,
                        price_hd=excluded.price_hd, emoji=excluded.emoji''',
                     (data['op_name'], data['op_bh'], data['op_hd'], emoji))
    log_admin(message.from_user.id, 'operator_add', data['op_name'])
    await state.clear()
    await message.answer(f"✅ Оператор сохранён: {emoji} <b>{data['op_name']}</b>", reply_markup=back_to_admin())

@dp.callback_query(F.data == "admin_edit_op")
async def admin_edit_op(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    rows = [[(f"{op['emoji']} {op['name']}", f"editop:{op['id']}", False)] for op in get_operators()]
    rows.append([('⬅️ Назад', 'admin_operators', False)])
    await safe_edit_text(callback.message, "<b>Выберите оператора:</b>", reply_markup=kb(rows))
    await callback.answer()

@dp.callback_query(F.data.startswith("editop:"))
async def edit_op_price(callback: CallbackQuery, state: FSMContext):
    op_id = int(callback.data.split(":")[1])
    await state.update_data(edit_op_id=op_id)
    await state.set_state(AdminStates.waiting_for_edit_bh)
    await safe_edit_text(callback.message, "Новая цена БХ ($):")
    await callback.answer()

@dp.message(AdminStates.waiting_for_edit_bh)
async def edit_bh_received(message: Message, state: FSMContext):
    amount = parse_amount(message.text)
    if amount is None:
        await message.answer("❌ Введите число.")
        return
    await state.update_data(edit_bh=amount)
    await state.set_state(AdminStates.waiting_for_edit_hd)
    await message.answer("Новая цена ХД ($):")

@dp.message(AdminStates.waiting_for_edit_hd)
async def edit_hd_received(message: Message, state: FSMContext):
    amount = parse_amount(message.text)
    if amount is None:
        await message.answer("❌ Введите число.")
        return
    data = await state.get_data()
    with db_conn() as conn:
        conn.execute('UPDATE operators SET price_bh=?, price_hd=? WHERE id=?', (data['edit_bh'], amount, data['edit_op_id']))
    log_admin(message.from_user.id, 'operator_price_edit', str(data['edit_op_id']))
    await state.clear()
    await message.answer("✅ Цены обновлены.", reply_markup=back_to_admin())

@dp.callback_query(F.data.in_({"admin_toggle_bh", "admin_toggle_hd"}))
async def admin_toggle_mode(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    mode = 'bh' if callback.data.endswith('bh') else 'hd'
    label = 'БХ' if mode == 'bh' else 'ХД'
    field = f'active_{mode}'
    rows = [[(f"{'🟢' if op[field] else '🔴'} {label} · {op['emoji']} {op['name']}", f"tog:{mode}:{op['id']}", False)] for op in get_operators()]
    rows.append([('⬅️ Назад', 'admin_operators', False)])
    await safe_edit_text(callback.message, f"<b>🔄 Вкл/Выкл {label}</b>", reply_markup=kb(rows))
    await callback.answer()

@dp.callback_query(F.data.startswith("tog:"))
async def toggle_operator_mode(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    _, mode, op_id_str = callback.data.split(":")
    field = 'active_bh' if mode == 'bh' else 'active_hd'
    with db_conn() as conn:
        conn.execute(f'UPDATE operators SET {field}=CASE {field} WHEN 1 THEN 0 ELSE 1 END WHERE id=?', (int(op_id_str),))
    log_admin(callback.from_user.id, 'operator_toggle', callback.data)
    await callback.answer("Изменено")
    await admin_toggle_mode(callback)

@dp.callback_query(F.data == "admin_del_op")
async def admin_del_op(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    rows = [[(f"🗑️ {op['emoji']} {op['name']}", f"delop:{op['id']}", False)] for op in get_operators()]
    rows.append([('⬅️ Назад', 'admin_operators', False)])
    await safe_edit_text(callback.message, "<b>Удалить оператора:</b>", reply_markup=kb(rows))
    await callback.answer()

@dp.callback_query(F.data.startswith("delop:"))
async def delete_operator(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    op_id = int(callback.data.split(":")[1])
    with db_conn() as conn:
        conn.execute('DELETE FROM operators WHERE id=?', (op_id,))
    log_admin(callback.from_user.id, 'operator_delete', str(op_id))
    await callback.answer("Удалён")
    await admin_del_op(callback)

# ============================================================
# ADMIN: CHANNELS / GROUPS / STATS / USERS
# ============================================================

@dp.callback_query(F.data == "admin_channels")
async def admin_channels(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    channels = get_active_channels()
    text = "<b>📢 КАНАЛЫ ЗАЯВОК</b>\n\n"
    if channels:
        for ch in channels:
            link = f"@{ch['username']}" if ch['username'] else (ch['invite_link'] or ch['channel_id'])
            text += f"• {link}\n"
    else:
        text += "<i>Каналы не добавлены.</i>"
    await safe_edit_text(callback.message, text, reply_markup=kb([
        [('➕ Добавить', 'admin_add_channel', False), ('🗑️ Удалить все', 'admin_del_channel', False)],
        [('⬅️ Назад', 'admin_back', False)],
    ]))
    await callback.answer()

@dp.callback_query(F.data == "admin_add_channel")
async def admin_add_channel(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.waiting_for_channel)
    await safe_edit_text(callback.message, "Перешлите сообщение из канала, отправьте @username или invite-ссылку.\n\nБот должен быть админом канала.")
    await callback.answer()

@dp.message(AdminStates.waiting_for_channel)
async def channel_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    channel_id = username = invite_link = None
    try:
        if message.forward_from_chat:
            channel_id = str(message.forward_from_chat.id)
            username = message.forward_from_chat.username
            if not username:
                try:
                    invite_link = await bot.export_chat_invite_link(channel_id)
                except Exception:
                    invite_link = None
        elif message.text and message.text.startswith('@'):
            chat = await bot.get_chat(message.text.strip())
            channel_id = str(chat.id)
            username = chat.username
        elif message.text and (message.text.startswith('https://t.me/') or message.text.startswith('http://t.me/')):
            invite_link = message.text.strip()
            # For private invite links we cannot get channel_id reliably without being member. Store link as id fallback.
            channel_id = invite_link
        else:
            await message.answer("❌ Отправьте пересланное сообщение, @username или ссылку.")
            return
        with db_conn() as conn:
            conn.execute('''INSERT INTO channels (channel_id, username, invite_link) VALUES (?, ?, ?)
                            ON CONFLICT(channel_id) DO UPDATE SET username=excluded.username, invite_link=excluded.invite_link''',
                         (channel_id, username, invite_link))
        log_admin(message.from_user.id, 'channel_add', channel_id)
        await state.clear()
        await message.answer("✅ Канал добавлен.", reply_markup=back_to_admin())
    except Exception as e:
        logger.exception("channel add failed")
        await message.answer(f"❌ Не удалось добавить канал: {e}")

@dp.callback_query(F.data == "admin_del_channel")
async def admin_del_channel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    await safe_edit_text(callback.message, "<b>Удалить все каналы?</b>", reply_markup=confirm_kb("confirm_del_channels", "admin_channels"))
    await callback.answer()

@dp.callback_query(F.data == "confirm_del_channels")
async def confirm_del_channels(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    with db_conn() as conn:
        conn.execute('DELETE FROM channels')
    log_admin(callback.from_user.id, 'channels_delete_all')
    await safe_edit_text(callback.message, "✅ Каналы удалены.", reply_markup=back_to_admin())
    await callback.answer()

@dp.callback_query(F.data == "admin_groups")
async def admin_groups(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    with db_conn() as conn:
        groups = conn.execute('SELECT * FROM groups ORDER BY id').fetchall()
    text = "<b>👥 ГРУППЫ</b>\n\n"
    text += "\n".join([f"{'🟢' if g['active'] else '🔴'} {g['username'] or g['group_id']}" for g in groups]) or "<i>Нет групп.</i>"
    await safe_edit_text(callback.message, text, reply_markup=back_to_admin())
    await callback.answer()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    with db_conn() as conn:
        users = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        orders = conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM orders WHERE status='active'").fetchone()[0]
        taken = conn.execute("SELECT COUNT(*) FROM orders WHERE status='taken'").fetchone()[0]
        done = conn.execute("SELECT COUNT(*) FROM orders WHERE status='done'").fetchone()[0]
        credited = conn.execute('SELECT COALESCE(SUM(price),0) FROM orders WHERE credited=1').fetchone()[0]
        balance = conn.execute('SELECT COALESCE(SUM(balance),0) FROM users').fetchone()[0]
        expected = conn.execute('SELECT COALESCE(SUM(expected_balance),0) FROM users').fetchone()[0]
        pending = conn.execute('SELECT COALESCE(SUM(pending_balance),0) FROM users').fetchone()[0]
    text = (
        f"<b>📊 СТАТИСТИКА</b>\n\n"
        f"👥 Юзеров: {users}\n"
        f"📱 Заявок всего: {orders}\n"
        f"🟢 Активные: {active}\n"
        f"🟡 Взяты: {taken}\n"
        f"✅ Сдано: {done}\n\n"
        f"💰 Зачтено: {format_money(credited)}\n"
        f"💎 Предв.: {format_money(pending)}\n"
        f"⏳ Ожидает: {format_money(expected)}\n"
        f"💵 Балансы: {format_money(balance)}"
    )
    await safe_edit_text(callback.message, text, reply_markup=back_to_admin())
    await callback.answer()

@dp.callback_query(F.data == "admin_users")
async def admin_users(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    with db_conn() as conn:
        users = conn.execute('SELECT * FROM users ORDER BY total_qr DESC LIMIT 50').fetchall()
    text = "<b>👤 УЧАСТНИКИ TOP-50</b>\n\n"
    for u in users:
        ban = "🚫" if u['banned'] else ""
        text += f"{ban} {user_label(u)} | {u['rank']} | QR:{u['total_qr']} | Бал:{format_money(u['balance'])}\n"
    await safe_edit_text(callback.message, text or "Нет пользователей", reply_markup=kb([
        [('🔍 Управление юзером', 'admin_manage_user', False)],
        [('⬅️ Назад', 'admin_back', False)],
    ]))
    await callback.answer()

@dp.callback_query(F.data == "admin_manage_user")
async def admin_manage_user(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.waiting_for_user_manage)
    await safe_edit_text(callback.message, "Введите ID или @username пользователя:")
    await callback.answer()

@dp.message(AdminStates.waiting_for_user_manage)
async def admin_manage_user_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    target = message.text.strip().replace('@', '')
    with db_conn() as conn:
        if target.isdigit():
            user = conn.execute('SELECT * FROM users WHERE user_id=?', (int(target),)).fetchone()
        else:
            user = conn.execute('SELECT * FROM users WHERE username=?', (target,)).fetchone()
    if not user:
        await message.answer("❌ Пользователь не найден.")
        await state.clear()
        return
    await state.clear()
    await message.answer(
        f"<b>👤 Пользователь</b>\n\n"
        f"ID: <code>{user['user_id']}</code>\n"
        f"Username: {user_label(user)}\n"
        f"Ранг: {user['rank']}\n"
        f"QR: {user['total_qr']}\n"
        f"Баланс: {format_money(user['balance'])}\n"
        f"Ожид.: {format_money(user['expected_balance'])}\n"
        f"Предв.: {format_money(user['pending_balance'])}\n"
        f"Бан: {'да' if user['banned'] else 'нет'}",
        reply_markup=kb([
            [('🚫 Бан/Разбан', f'ban_toggle:{user["user_id"]}', False)],
            [('💸 Выплатить', 'admin_pay_user', False), ('➖ Списать', 'admin_deduct_user', False)],
            [('⬅️ Админ-панель', 'admin_back', False)],
        ])
    )

@dp.callback_query(F.data.startswith("ban_toggle:"))
async def ban_toggle(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    user_id = int(callback.data.split(":")[1])
    with db_conn() as conn:
        conn.execute('UPDATE users SET banned=CASE banned WHEN 1 THEN 0 ELSE 1 END WHERE user_id=?', (user_id,))
    log_admin(callback.from_user.id, 'ban_toggle', str(user_id))
    await callback.answer("Готово")
    await safe_edit_text(callback.message, "✅ Статус бана изменён.", reply_markup=back_to_admin())

# ============================================================
# ADMIN: PAYOUTS / ORDERS / BROADCAST / DB / SETTINGS
# ============================================================

@dp.callback_query(F.data == "admin_payouts")
async def admin_payouts(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    await safe_edit_text(callback.message, "<b>💵 ВЫПЛАТЫ</b>", reply_markup=kb([
        [('💵 Начислить всем ожид.', 'confirm_pay_all', False)],
        [('👤 Выплатить юзеру', 'admin_pay_user', False), ('➖ Списать', 'admin_deduct_user', False)],
        [('⬅️ Назад', 'admin_back', False)],
    ]))
    await callback.answer()

@dp.callback_query(F.data == "confirm_pay_all")
async def confirm_pay_all(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    with db_conn() as conn:
        total = conn.execute('SELECT COALESCE(SUM(expected_balance),0) FROM users').fetchone()[0]
    await safe_edit_text(callback.message, f"<b>Выплатить всем ожидаемые?</b>\n\nСумма: {format_money(total)}", reply_markup=confirm_kb("do_pay_all", "admin_payouts"))
    await callback.answer()

@dp.callback_query(F.data == "do_pay_all")
async def do_pay_all(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    with db_conn() as conn:
        conn.execute('''INSERT INTO balance_history (user_id, amount, type, description)
                        SELECT user_id, expected_balance, 'payout', 'Выплата всем'
                        FROM users WHERE expected_balance > 0''')
        conn.execute('UPDATE users SET balance=balance+expected_balance, expected_balance=0 WHERE expected_balance > 0')
    log_admin(callback.from_user.id, 'pay_all')
    await safe_edit_text(callback.message, "✅ Выплаты начислены всем.", reply_markup=back_to_admin())
    await callback.answer()

@dp.callback_query(F.data == "admin_pay_user")
async def admin_pay_user(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.waiting_for_pay_user)
    await safe_edit_text(callback.message, "Введите ID или @username:")
    await callback.answer()

@dp.message(AdminStates.waiting_for_pay_user)
async def pay_user_received(message: Message, state: FSMContext):
    target = message.text.strip().replace('@', '')
    with db_conn() as conn:
        user = conn.execute('SELECT * FROM users WHERE user_id=? OR username=?', (int(target) if target.isdigit() else 0, target)).fetchone()
    if not user:
        await message.answer("❌ Не найден.")
        await state.clear()
        return
    await state.update_data(pay_user_id=user['user_id'], pay_expected=user['expected_balance'])
    await state.set_state(AdminStates.waiting_for_pay_amount)
    await message.answer(f"👤 {user_label(user)}\nОжидаемая: {format_money(user['expected_balance'])}\nВведите сумму или 0 для всей ожидаемой:")

@dp.message(AdminStates.waiting_for_pay_amount)
async def pay_amount_received(message: Message, state: FSMContext):
    data = await state.get_data()
    amount = parse_amount(message.text)
    if amount is None:
        await message.answer("❌ Введите число.")
        return
    if amount == 0:
        amount = float(data['pay_expected'])
    if amount <= 0:
        await message.answer("❌ Сумма должна быть больше 0.")
        return
    if amount > float(data['pay_expected']):
        await message.answer(f"❌ Максимум: {format_money(data['pay_expected'])}")
        return
    with db_conn() as conn:
        conn.execute('UPDATE users SET balance=balance+?, expected_balance=MAX(0, expected_balance-?) WHERE user_id=?', (amount, amount, data['pay_user_id']))
        conn.execute('INSERT INTO balance_history (user_id, amount, type, description) VALUES (?, ?, ?, ?)', (data['pay_user_id'], amount, 'payout', 'Ручная выплата'))
    await state.clear()
    await message.answer(f"✅ Выплачено: {format_money(amount)}", reply_markup=back_to_admin())
    try:
        await bot.send_message(data['pay_user_id'], f"💵 <b>Выплата</b>\nСумма: {format_money(amount)}")
    except Exception:
        pass

@dp.callback_query(F.data == "admin_deduct_user")
async def admin_deduct_user(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.waiting_for_deduct_user)
    await safe_edit_text(callback.message, "Введите ID или @username:")
    await callback.answer()

@dp.message(AdminStates.waiting_for_deduct_user)
async def deduct_user_received(message: Message, state: FSMContext):
    target = message.text.strip().replace('@', '')
    with db_conn() as conn:
        user = conn.execute('SELECT * FROM users WHERE user_id=? OR username=?', (int(target) if target.isdigit() else 0, target)).fetchone()
    if not user:
        await message.answer("❌ Не найден.")
        await state.clear()
        return
    await state.update_data(deduct_user_id=user['user_id'])
    await state.set_state(AdminStates.waiting_for_deduct_amount)
    await message.answer(f"👤 {user_label(user)}\nБаланс: {format_money(user['balance'])}\nВведите сумму списания:")

@dp.message(AdminStates.waiting_for_deduct_amount)
async def deduct_amount_received(message: Message, state: FSMContext):
    amount = parse_amount(message.text)
    if amount is None or amount <= 0:
        await message.answer("❌ Введите сумму больше 0.")
        return
    data = await state.get_data()
    with db_conn() as conn:
        conn.execute('UPDATE users SET balance=MAX(0, balance-?) WHERE user_id=?', (amount, data['deduct_user_id']))
        conn.execute('INSERT INTO balance_history (user_id, amount, type, description) VALUES (?, ?, ?, ?)', (data['deduct_user_id'], -amount, 'deduct', 'Списание админом'))
    await state.clear()
    await message.answer(f"✅ Списано: {format_money(amount)}", reply_markup=back_to_admin())

@dp.callback_query(F.data == "admin_create_order")
async def admin_create_order(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    rows = [[(f"{op['emoji']} {op['name']} · БХ {format_money(op['price_bh'])} / ХД {format_money(op['price_hd'])}", f"admin_order:{op['id']}", False)] for op in get_operators()]
    rows.append([('⬅️ Назад', 'admin_back', False)])
    await safe_edit_text(callback.message, "<b>📱 ВЫБЕРИТЕ ОПЕРАТОРА</b>", reply_markup=kb(rows))
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_order:"))
async def admin_order_mode(callback: CallbackQuery):
    op_id = int(callback.data.split(":")[1])
    await safe_edit_text(callback.message, "<b>Выберите режим:</b>", reply_markup=kb([
        [('🟢 БХ', f'admin_order_do:{op_id}:БХ', False)],
        [('🟡 ХД', f'admin_order_do:{op_id}:ХД', False)],
        [('⬅️ Назад', 'admin_create_order', False)],
    ]))
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_order_do:"))
async def admin_order_do(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    _, op_id_str, mode = callback.data.split(":")
    op_id = int(op_id_str)
    channels = get_active_channels()
    if not channels:
        await callback.answer("Нет каналов", show_alert=True)
        return
    with db_conn() as conn:
        op = conn.execute('SELECT * FROM operators WHERE id=?', (op_id,)).fetchone()
        if not op:
            await callback.answer("Оператор не найден")
            return
        active_field = 'active_bh' if mode == 'БХ' else 'active_hd'
        price = op['price_bh'] if mode == 'БХ' else op['price_hd']
        if op[active_field] != 1:
            await callback.answer("Режим отключён", show_alert=True)
            return
        cur = conn.execute('INSERT INTO orders (operator, price, mode, status) VALUES (?, ?, ?, "active")', (op['name'], price, mode))
        order_id = cur.lastrowid
    bot_username = (await bot.me()).username
    deep_link = f"https://t.me/{bot_username}?start=order_{order_id}"
    try:
        sent = await bot.send_message(channels[0]['channel_id'], order_card(order_id, op['emoji'], op['name'], price, mode), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔥 ЗАБРАТЬ ЗАКАЗ", url=deep_link)]]))
        with db_conn() as conn:
            conn.execute('UPDATE orders SET channel_msg_id=? WHERE id=?', (sent.message_id, order_id))
        await safe_edit_text(callback.message, f"✅ Заявка #{order_id} отправлена в канал.", reply_markup=back_to_admin())
        await callback.answer("✅")
    except Exception:
        logger.exception("admin_order_do failed")
        await callback.answer("Ошибка отправки", show_alert=True)

@dp.callback_query(F.data == "admin_delete_orders")
async def admin_delete_orders(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    with db_conn() as conn:
        orders = conn.execute('SELECT * FROM orders ORDER BY id DESC LIMIT 30').fetchall()
    rows = [[(f"🗑️ #{o['id']} {o['operator']} · {o['status']}", f"delorder:{o['id']}", False)] for o in orders]
    rows.append([('🧹 Удалить активные', 'confirm_del_active_orders', False)])
    rows.append([('⬅️ Назад', 'admin_back', False)])
    await safe_edit_text(callback.message, "<b>🗑️ ЗАЯВКИ</b>", reply_markup=kb(rows))
    await callback.answer()

@dp.callback_query(F.data.startswith("delorder:"))
async def del_order(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    oid = int(callback.data.split(":")[1])
    with db_conn() as conn:
        conn.execute('DELETE FROM orders WHERE id=?', (oid,))
    await callback.answer(f"#{oid} удалён")
    await admin_delete_orders(callback)

@dp.callback_query(F.data == "confirm_del_active_orders")
async def confirm_del_active_orders(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    await safe_edit_text(callback.message, "Удалить все активные заявки?", reply_markup=confirm_kb("do_del_active_orders", "admin_delete_orders"))
    await callback.answer()

@dp.callback_query(F.data == "do_del_active_orders")
async def do_del_active_orders(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    with db_conn() as conn:
        conn.execute("DELETE FROM orders WHERE status='active'")
    await safe_edit_text(callback.message, "✅ Активные заявки удалены.", reply_markup=back_to_admin())
    await callback.answer()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.waiting_for_broadcast)
    await safe_edit_text(callback.message, "Введите текст рассылки:")
    await callback.answer()

@dp.message(AdminStates.waiting_for_broadcast)
async def broadcast_send(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    with db_conn() as conn:
        users = conn.execute('SELECT user_id FROM users WHERE banned=0').fetchall()
    sent = 0
    for u in users:
        try:
            await bot.send_message(u['user_id'], message.text)
            sent += 1
            await asyncio.sleep(0.04)
        except Exception:
            pass
    await state.clear()
    await message.answer(f"✅ Рассылка завершена: {sent}/{len(users)}", reply_markup=back_to_admin())

@dp.callback_query(F.data == "admin_db_menu")
async def admin_db_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    await safe_edit_text(callback.message, "<b>💾 БАЗА ДАННЫХ</b>", reply_markup=kb([
        [('📤 Выгрузить БД', 'admin_db_export', False), ('📥 Загрузить БД', 'admin_db_import', False)],
        [(f"🔄 Автобэкап: {get_setting('auto_backup', 'off').upper()}", 'admin_db_auto', False)],
        [('⬅️ Назад', 'admin_back', False)],
    ]))
    await callback.answer()

@dp.callback_query(F.data == "admin_db_export")
async def admin_db_export(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    try:
        await callback.message.answer_document(FSInputFile(DB_PATH), caption=f"📦 База данных · {moscow_time().strftime('%Y-%m-%d %H:%M')}")
        await callback.answer("✅")
    except Exception:
        await callback.answer("Ошибка", show_alert=True)

@dp.callback_query(F.data == "admin_db_import")
async def admin_db_import(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.waiting_for_db_file)
    await safe_edit_text(callback.message, "Отправьте .db файл.")
    await callback.answer()

@dp.message(AdminStates.waiting_for_db_file, F.document)
async def db_file_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    if not message.document.file_name.endswith('.db'):
        await message.answer("❌ Нужен .db файл.")
        return
    backup_name = os.path.join(BACKUP_DIR, f"before_import_{moscow_time().strftime('%Y%m%d_%H%M%S')}.db")
    try:
        if os.path.exists(DB_PATH):
            shutil.copy2(DB_PATH, backup_name)
        await bot.download(message.document, destination=DB_PATH)
        init_db()
        await state.clear()
        await message.answer("✅ БД заменена и миграции применены.", reply_markup=back_to_admin())
    except Exception as e:
        logger.exception("DB import failed")
        await message.answer(f"❌ Ошибка импорта: {e}")

@dp.callback_query(F.data == "admin_db_auto")
async def admin_db_auto(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    cur = get_setting('auto_backup', 'off')
    set_setting('auto_backup', 'off' if cur == 'on' else 'on')
    await callback.answer("Изменено")
    await admin_db_menu(callback)

@dp.callback_query(F.data == "admin_settings")
async def admin_settings(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    text = (
        "<b>⚙️ НАСТРОЙКИ</b>\n\n"
        f"hold_hours = {get_setting('hold_hours')}\n"
        f"min_withdraw = {get_setting('min_withdraw')}\n"
        f"submit_timeout = {get_setting('submit_timeout')} сек\n"
        f"max_warnings = {get_setting('max_warnings')}\n"
        f"block_hours = {get_setting('block_hours')}\n"
        f"daily_phone_limit = {get_setting('daily_phone_limit')}\n"
        f"same_phone_daily_limit = {get_setting('same_phone_daily_limit')}"
    )
    await safe_edit_text(callback.message, text, reply_markup=kb([
        [('✏️ Изменить настройку', 'admin_setting_edit', False)],
        [('⬅️ Назад', 'admin_back', False)],
    ]))
    await callback.answer()

@dp.callback_query(F.data == "admin_setting_edit")
async def admin_setting_edit(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.waiting_for_setting_key)
    await safe_edit_text(callback.message, "Введите ключ настройки. Например: <code>min_withdraw</code>")
    await callback.answer()

@dp.message(AdminStates.waiting_for_setting_key)
async def setting_key_received(message: Message, state: FSMContext):
    key_name = message.text.strip()
    allowed = {'hold_hours', 'min_withdraw', 'submit_timeout', 'max_warnings', 'block_hours', 'daily_phone_limit', 'same_phone_daily_limit', 'auto_backup', 'work_day'}
    if key_name not in allowed:
        await message.answer("❌ Такой ключ нельзя изменить.")
        return
    await state.update_data(setting_key=key_name)
    await state.set_state(AdminStates.waiting_for_setting_value)
    await message.answer(f"Введите новое значение для <code>{key_name}</code>:")

@dp.message(AdminStates.waiting_for_setting_value)
async def setting_value_received(message: Message, state: FSMContext):
    data = await state.get_data()
    value = message.text.strip()
    set_setting(data['setting_key'], value)
    await state.clear()
    await message.answer("✅ Настройка обновлена.", reply_markup=back_to_admin())

@dp.callback_query(F.data == "admin_bans")
async def admin_bans(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    with db_conn() as conn:
        banned = conn.execute('SELECT * FROM users WHERE banned=1 ORDER BY user_id DESC LIMIT 50').fetchall()
    text = "<b>🚫 БАНЫ</b>\n\n"
    text += "\n".join([f"• {user_label(u)} | ID {u['user_id']}" for u in banned]) or "<i>Нет забаненных.</i>"
    await safe_edit_text(callback.message, text, reply_markup=kb([
        [('🔍 Управление юзером', 'admin_manage_user', False)],
        [('⬅️ Назад', 'admin_back', False)],
    ]))
    await callback.answer()

# ============================================================
# USER CALLBACKS
# ============================================================

@dp.callback_query(F.data == "profile")
async def profile(callback: CallbackQuery):
    ensure_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    user = get_user(callback.from_user.id)
    text = (
        f"<b>👤 ПРОФИЛЬ</b>\n\n"
        f"🆔 {user_label(user)}\n"
        f"🏅 Ранг: <b>{user['rank']}</b>\n"
        f"🎁 Бонус к QR: +{format_money(user['bonus'])}\n"
        f"📱 QR за месяц: {user['qr_month']}\n"
        f"📈 Всего QR: {user['total_qr']}\n\n"
        f"💎 Предварительно: {format_money(user['pending_balance'])}\n"
        f"⏳ Ожидает: {format_money(user['expected_balance'])}\n"
        f"💵 Баланс: <b>{format_money(user['balance'])}</b>"
    )
    await safe_edit_text(callback.message, text, reply_markup=kb([
        [('📋 История баланса', 'balance_history', False)],
        [('💵 Вывести', 'withdraw', False), ('📱 История сдачи', 'submission_history', False)],
        [('ℹ️ Информация', 'info', False)],
        [('⬅️ Главное меню', 'back_main', False)],
    ]))
    await callback.answer()

@dp.callback_query(F.data == "balance_history")
async def balance_history(callback: CallbackQuery):
    with db_conn() as conn:
        rows = conn.execute('SELECT * FROM balance_history WHERE user_id=? ORDER BY id DESC LIMIT 50', (callback.from_user.id,)).fetchall()
    text = "<b>📋 ИСТОРИЯ БАЛАНСА</b>\n\n"
    if rows:
        for r in rows:
            sign = '+' if float(r['amount']) >= 0 else ''
            text += f"• {r['created'][:10]} · {r['type']} · {sign}{format_money(r['amount'])} · {r['description']}\n"
    else:
        text += "<i>Операций нет.</i>"
    await safe_edit_text(callback.message, text, reply_markup=back_to_profile())
    await callback.answer()

@dp.callback_query(F.data == "submission_history")
async def submission_history(callback: CallbackQuery):
    with db_conn() as conn:
        rows = conn.execute('SELECT * FROM orders WHERE executor_id=? ORDER BY id DESC LIMIT 50', (callback.from_user.id,)).fetchall()
    text = "<b>📱 ИСТОРИЯ СДАЧИ</b>\n\n"
    if rows:
        for o in rows:
            st = '✅' if o['credited'] else ('🚫' if o['blocked'] else ('❌' if o['noscan'] else '🟡'))
            text += f"#{o['id']} · <code>{o['phone'] or '—'}</code> · {o['operator']} · {o['mode']} · {st}\n"
    else:
        text += "<i>Нет сдач.</i>"
    await safe_edit_text(callback.message, text, reply_markup=back_to_profile())
    await callback.answer()

@dp.callback_query(F.data == "withdraw")
async def withdraw(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    min_w = setting_float('min_withdraw', DEFAULT_MIN_WITHDRAW)
    if float(user['balance']) < min_w:
        await safe_edit_text(callback.message, f"<b>💵 ВЫВОД</b>\n\nБаланс: {format_money(user['balance'])}\nМинимум: {format_money(min_w)}\n\n<i>Недостаточно средств.</i>", reply_markup=back_to_profile())
        await callback.answer()
        return
    await safe_edit_text(callback.message, f"<b>💵 ВЫВОД</b>\n\nБаланс: {format_money(user['balance'])}\nВывод на @{SEND_USERNAME or 'send'}", reply_markup=kb([
        [(f"💵 {format_money(min_w)}", f"wd:{min_w}", False)],
        [(f"💎 Всё ({format_money(user['balance'])})", f"wd:{user['balance']}", False)],
        [('⬅️ Профиль', 'profile', False)],
    ]))
    await callback.answer()

@dp.callback_query(F.data.startswith("wd:"))
async def withdraw_amount(callback: CallbackQuery):
    amount = parse_amount(callback.data.split(":", 1)[1])
    user = get_user(callback.from_user.id)
    if amount is None or amount <= 0 or float(user['balance']) < amount:
        await callback.answer("Недостаточно", show_alert=True)
        return
    with db_conn() as conn:
        conn.execute('UPDATE users SET balance=balance-? WHERE user_id=?', (amount, callback.from_user.id))
        conn.execute('INSERT INTO balance_history (user_id, amount, type, description) VALUES (?, ?, ?, ?)', (callback.from_user.id, -amount, 'withdraw', 'Заявка на вывод'))
    channels = get_active_channels()
    text = f"<b>💵 ЗАЯВКА НА ВЫВОД</b>\n\n👤 {user_label(user)}\nID: <code>{callback.from_user.id}</code>\n💰 {format_money(amount)}\n📱 @{SEND_USERNAME or 'send'}"
    if channels:
        try:
            await bot.send_message(channels[0]['channel_id'], text, reply_markup=kb([[('✅ Оплачено', 'bypass', False), ('❌ Закрыть', 'bypass', False)]]))
        except Exception:
            pass
    await safe_edit_text(callback.message, f"✅ Заявка на вывод создана: {format_money(amount)}", reply_markup=back_to_profile())
    await callback.answer()

@dp.callback_query(F.data == "operators_list")
async def operators_list(callback: CallbackQuery):
    text = "<b>📊 ЦЕНЫ БХ / ХД</b>\n\n"
    for op in get_operators():
        text += f"{op['emoji']} <b>{op['name']}</b>\nБХ: {'✅' if op['active_bh'] else '❌'} {format_money(op['price_bh'])} | ХД: {'✅' if op['active_hd'] else '❌'} {format_money(op['price_hd'])}\n\n"
    await safe_edit_text(callback.message, text, reply_markup=back_to_main())
    await callback.answer()

@dp.callback_query(F.data == "my_numbers")
async def my_numbers(callback: CallbackQuery):
    await submission_history(callback)

@dp.callback_query(F.data == "referral")
async def referral(callback: CallbackQuery):
    ensure_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    me = await bot.me()
    link = f"https://t.me/{me.username}?start=ref_{callback.from_user.id}"
    with db_conn() as conn:
        refs = conn.execute('SELECT COUNT(*) FROM referrals WHERE referrer_id=?', (callback.from_user.id,)).fetchone()[0]
    await safe_edit_text(callback.message, f"<b>👥 РЕФЕРАЛЫ</b>\n\n🔗 <code>{link}</code>\n\n👥 Рефералов: {refs}", reply_markup=back_to_main())
    await callback.answer()

@dp.callback_query(F.data == "top_users")
async def top_users(callback: CallbackQuery):
    with db_conn() as conn:
        users = conn.execute('SELECT * FROM users ORDER BY total_qr DESC LIMIT 10').fetchall()
    text = "<b>🏆 ТОП ИСПОЛНИТЕЛЕЙ</b>\n\n"
    if users:
        for i, u in enumerate(users, 1):
            text += f"{i}. {user_label(u)} · {u['total_qr']} QR · {u['rank']}\n"
    else:
        text += "<i>Пока пусто.</i>"
    await safe_edit_text(callback.message, text, reply_markup=back_to_main())
    await callback.answer()

@dp.callback_query(F.data == "info")
async def info(callback: CallbackQuery):
    text = (
        f"<b>ℹ️ ИНФОРМАЦИЯ</b>\n\n"
        f"💵 Мин. вывод: {format_money(setting_float('min_withdraw', DEFAULT_MIN_WITHDRAW))}\n"
        f"📱 Вывод: @{SEND_USERNAME or 'send'}\n"
        f"🟢 БХ — без холда, после засчёта сразу в баланс\n"
        f"🟡 ХД — холд {setting_int('hold_hours', DEFAULT_HOLD_HOURS)} ч\n"
        f"⏳ Время сдачи: {setting_int('submit_timeout', DEFAULT_SUBMIT_TIMEOUT)//60} мин\n"
        f"⚠️ Предупреждения: {setting_int('max_warnings', DEFAULT_MAX_WARNINGS)} = блок"
    )
    await safe_edit_text(callback.message, text, reply_markup=back_to_main())
    await callback.answer()

# ============================================================
# NAVIGATION
# ============================================================

@dp.callback_query(F.data == "back_main")
async def back_main(callback: CallbackQuery):
    await safe_edit_text(callback.message, f"<b>💎 {BOT_TITLE}</b>\n\nВыберите действие:", reply_markup=main_menu())
    await callback.answer()

@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    await safe_edit_text(callback.message, f"<b>🛠️ {BOT_TITLE} — АДМИН-ПАНЕЛЬ</b>", reply_markup=admin_menu())
    await callback.answer()

@dp.callback_query(F.data.in_({"close", "bypass"}))
async def close_or_bypass(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()

@dp.message(F.text, F.chat.type == ChatType.PRIVATE)
async def private_unknown(message: Message):
    ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer(f"<b>💎 {BOT_TITLE}</b>\n\nВыберите действие:", reply_markup=main_menu())

# ============================================================
# BACKGROUND TASKS
# ============================================================

async def antispam_task():
    while True:
        try:
            await asyncio.sleep(30)
            timeout_seconds = setting_int('submit_timeout', DEFAULT_SUBMIT_TIMEOUT)
            max_warnings = setting_int('max_warnings', DEFAULT_MAX_WARNINGS)
            block_hours = setting_int('block_hours', DEFAULT_BLOCK_HOURS)
            timeout = (datetime.now() - timedelta(seconds=timeout_seconds)).isoformat(timespec="seconds")
            with db_conn() as conn:
                expired = conn.execute("SELECT * FROM orders WHERE status='taken' AND taken_at <= ?", (timeout,)).fetchall()
            for o in expired:
                with db_conn() as conn:
                    conn.execute("UPDATE orders SET status='active', executor_id=NULL, taken=NULL, taken_at=NULL, channel_msg_id=NULL, retry_count=retry_count+1 WHERE id=?", (o['id'],))
                    conn.execute('UPDATE users SET warnings=warnings+1 WHERE user_id=?', (o['executor_id'],))
                    warns = conn.execute('SELECT warnings FROM users WHERE user_id=?', (o['executor_id'],)).fetchone()['warnings']
                    blocked = False
                    if warns >= max_warnings:
                        blocked_until = (datetime.now() + timedelta(hours=block_hours)).isoformat(timespec="seconds")
                        conn.execute('UPDATE users SET warnings=0, blocked_until=? WHERE user_id=?', (blocked_until, o['executor_id']))
                        blocked = True
                try:
                    await bot.send_message(o['executor_id'], f"⚠️ <b>Время вышло</b>\nЗаявка #{o['id']} возвращена.\nПредупреждений: {warns}/{max_warnings}" + (f"\n🚫 Блок на {block_hours} ч." if blocked else ""))
                except Exception:
                    pass
                channels = get_active_channels()
                if channels:
                    bot_username = (await bot.me()).username
                    deep_link = f"https://t.me/{bot_username}?start=order_{o['id']}"
                    try:
                        sent = await bot.send_message(channels[0]['channel_id'], order_card(o['id'], '📱', o['operator'], o['price'], o['mode'], retry=True), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔥 ЗАБРАТЬ ЗАКАЗ", url=deep_link)]]))
                        with db_conn() as conn:
                            conn.execute('UPDATE orders SET channel_msg_id=? WHERE id=?', (sent.message_id, o['id']))
                    except Exception:
                        logger.exception("retry order publish failed")
        except Exception:
            logger.exception("antispam_task error")

async def auto_backup_task():
    while True:
        try:
            await asyncio.sleep(3600)
            if get_setting('auto_backup', 'off') == 'on' and os.path.exists(DB_PATH):
                ts = moscow_time().strftime('%Y%m%d_%H%M%S')
                shutil.copy2(DB_PATH, os.path.join(BACKUP_DIR, f"backup_{ts}.db"))
                backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.db')])
                while len(backups) > 48:
                    os.remove(os.path.join(BACKUP_DIR, backups.pop(0)))
        except Exception:
            logger.exception("auto_backup_task error")

async def hold_check_task():
    while True:
        try:
            await asyncio.sleep(300)
            with db_conn() as conn:
                rows = conn.execute("""SELECT * FROM orders
                    WHERE status='done' AND mode='ХД' AND credited=1 AND paid=0
                    AND hold_until IS NOT NULL AND hold_until <= ?""", (now_iso(),)).fetchall()
                for o in rows:
                    conn.execute('UPDATE orders SET paid=1 WHERE id=?', (o['id'],))
                    conn.execute('UPDATE users SET expected_balance=MAX(0, expected_balance-?), balance=balance+? WHERE user_id=?', (o['price'], o['price'], o['executor_id']))
                    conn.execute('INSERT INTO balance_history (user_id, amount, type, description) VALUES (?, ?, ?, ?)', (o['executor_id'], o['price'], 'hold_payout', f'Холд #{o["id"]}'))
                    try:
                        await bot.send_message(o['executor_id'], f"💵 <b>Холд завершён</b>\nЗаказ #{o['id']}\nНачислено: {format_money(o['price'])}")
                    except Exception:
                        pass
        except Exception:
            logger.exception("hold_check_task error")

# ============================================================
# STARTUP
# ============================================================

async def main():
    init_db()
    me = await bot.me()
    logger.info("%s started as @%s", BOT_TITLE, me.username)
    asyncio.create_task(auto_backup_task())
    asyncio.create_task(hold_check_task())
    asyncio.create_task(antispam_task())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
