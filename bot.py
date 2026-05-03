# diamond_autovbiv.py
# ФИНАЛ: SOURCE (номер + код) → TARGET (номер + код + встал)

import os
import asyncio
import re
import sqlite3
from datetime import datetime
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.tl.custom import Button

# ========== КОНФИГ ДЛЯ RAILWAY ==========
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

if not API_ID or not API_HASH or not BOT_TOKEN:
    print("❌ Ошибка: Установите переменные в Railway")
    exit(1)

# ========== БАЗА ДАННЫХ ==========
class DiamondDB:
    def __init__(self):
        self.conn = sqlite3.connect('diamond_data.db', check_same_thread=False)
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
        """Сохраняем номер, ждём код"""
        self.cursor.execute('''
            INSERT INTO pending (user_id, phone, status, created_at)
            VALUES (?, ?, 'waiting_code', ?)
        ''', (user_id, phone, datetime.now()))
        self.conn.commit()
        return self.cursor.lastrowid
    
    def update_pending_with_code(self, phone, code):
        """Обновляем запись с кодом"""
        self.cursor.execute('''
            UPDATE pending SET code = ?, status = 'code_received'
            WHERE phone = ? AND status = 'waiting_code'
        ''', (code, phone))
        self.conn.commit()
    
    def mark_success(self, phone):
        """Помечаем как успешный встав"""
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
        """Получаем последний номер без кода"""
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

async def get_user_client(user_id, session_string=None):
    if user_id in user_clients:
        client = user_clients[user_id]
        if client.is_connected():
            return client
    
    if not session_string:
        session_string, _, _ = db.get_session(user_id)
        if not session_string:
            return None
    
    client = TelegramClient(f"sessions/user_{user_id}", API_ID, API_HASH)
    await client.connect()
    
    if session_string:
        client.session.set_session_str(session_string)
        if await client.is_user_authorized():
            user_clients[user_id] = client
            return client
    return None

# ========== КЛАВИАТУРА ДЛЯ ВВОДА КОДА ==========
def get_code_keyboard():
    return [
        [Button.inline("1", b"code_1"), Button.inline("2", b"code_2"), Button.inline("3", b"code_3")],
        [Button.inline("4", b"code_4"), Button.inline("5", b"code_5"), Button.inline("6", b"code_6")],
        [Button.inline("7", b"code_7"), Button.inline("8", b"code_8"), Button.inline("9", b"code_9")],
        [Button.inline("0", b"code_0"), Button.inline("⌫", b"code_backspace"), Button.inline("✅", b"code_submit")]
    ]

user_code_inputs = {}

# ========== ОБРАБОТЧИКИ КОМАНД ==========
async def setup_handlers():
    global bot
    
    @bot.on(events.NewMessage(pattern='/start'))
    async def start_cmd(event):
        await event.reply("""
💎 **DIAMOND AUTOVBIV BOT** 💎

**Схема работы:**
📌 ГРУППА 1 (SOURCE) — номера и коды
📌 ГРУППА 2 (TARGET) — сюда бот отправляет номер+код, сюда пишут "встал"

**Команды:**
/login <номер> — вход в аккаунт
/set_source — отметить ГРУППУ 1 (где номера и коды)
/set_target — отметить ГРУППУ 2 (куда отправлять и где "встал")
/stats — статистика
/status — статус

💎 **Бот сразу отправляет номер и код в TARGET**
        """, parse_mode='markdown')
    
    @bot.on(events.NewMessage(pattern='/login (.+)'))
    async def login_cmd(event):
        user_id = event.sender_id
        phone = event.pattern_match.group(1).strip()
        
        await event.reply(f"📱 Отправляю код на {phone}...")
        
        client = TelegramClient(f"sessions/temp_{user_id}", API_ID, API_HASH)
        await client.connect()
        
        try:
            await client.send_code_request(phone)
            user_clients[user_id] = client
            db.save_session(user_id, phone, "", "waiting_code")
            
            await event.reply(
                f"✅ Код отправлен на {phone}\n\n🔢 **Введите код через кнопки:**",
                buttons=get_code_keyboard(),
                parse_mode='markdown'
            )
        except Exception as e:
            await event.reply(f"❌ Ошибка: {str(e)}")
    
    @bot.on(events.CallbackQuery())
    async def callback_handler(event):
        user_id = event.sender_id
        data = event.data.decode('utf-8')
        
        _, _, step = db.get_session(user_id)
        
        if step == "waiting_code":
            if user_id not in user_code_inputs:
                session_string, phone, _ = db.get_session(user_id)
                user_code_inputs[user_id] = {'code': '', 'phone': phone}
            
            if data.startswith('code_'):
                digit = data.split('_')[1]
                if digit.isdigit():
                    user_code_inputs[user_id]['code'] += digit
                    await event.answer(f"Код: {user_code_inputs[user_id]['code']}")
                    await event.edit(
                        f"✅ **Введите код:**\n\n🔢 Текущий код: `{user_code_inputs[user_id]['code']}`",
                        buttons=get_code_keyboard()
                    )
                elif digit == 'backspace':
                    user_code_inputs[user_id]['code'] = user_code_inputs[user_id]['code'][:-1]
                    await event.answer("Удалено")
                    await event.edit(
                        f"✅ **Введите код:**\n\n🔢 Текущий код: `{user_code_inputs[user_id]['code']}`",
                        buttons=get_code_keyboard()
                    )
            
            elif data == 'code_submit':
                code = user_code_inputs[user_id].get('code', '')
                if not code:
                    await event.answer("Введите код!", alert=True)
                    return
                
                await event.answer(f"Проверяю код {code}...")
                
                client = user_clients.get(user_id)
                if client:
                    try:
                        await client.sign_in(code=code)
                        session_string = client.session.save()
                        phone = (await client.get_me()).phone
                        db.save_session(user_id, phone, session_string, "authorized")
                        await event.edit(f"✅ **Вход выполнен!**\n\nАккаунт: {phone}\n\nТеперь настройте группы:\n/set_source — группа с номерами и кодами\n/set_target — группа для отправки")
                        del user_code_inputs[user_id]
                    except SessionPasswordNeededError:
                        db.save_session(user_id, phone, "", "waiting_2fa")
                        await event.edit(f"🔐 **Требуется 2FA пароль**\n\nИспользуйте: `/2fa <пароль>`", parse_mode='markdown')
                        del user_code_inputs[user_id]
                    except Exception as e:
                        await event.edit(f"❌ Ошибка: {str(e)}")
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
            await event.reply(f"✅ **Вход выполнен!**\n\nАккаунт: {phone}\n\nТеперь настройте группы.")
        except Exception as e:
            await event.reply(f"❌ Ошибка: {str(e)}")
    
    @bot.on(events.NewMessage(pattern='/set_source'))
    async def set_source_cmd(event):
        user_id = event.sender_id
        source_group = event.chat_id
        _, target_group = db.get_groups(user_id)
        db.set_groups(user_id, source_group, target_group)
        await event.reply(f"✅ **ГРУППА 1 (SOURCE) установлена!**\n\n📌 Сюда бот будет смотреть номера и коды")
        
        # Запускаем слушатель
        session_string, _, _ = db.get_session(user_id)
        if session_string:
            client = await get_user_client(user_id, session_string)
            if client and source_group:
                await start_source_listener(user_id, client, source_group, event.chat_id)
    
    @bot.on(events.NewMessage(pattern='/set_target'))
    async def set_target_cmd(event):
        user_id = event.sender_id
        target_group = event.chat_id
        source_group, _ = db.get_groups(user_id)
        db.set_groups(user_id, source_group, target_group)
        await event.reply(f"✅ **ГРУППА 2 (TARGET) установлена!**\n\n📌 Сюда бот будет отправлять номера и коды\n📌 Сюда пишут 'встал'")
    
    @bot.on(events.NewMessage(pattern='/stats'))
    async def stats_cmd(event):
        user_id = event.sender_id
        stats = db.get_stats_today(user_id)
        msg = f"📊 **СТАТИСТИКА ЗА СЕГОДНЯ**\n\n"
        msg += f"📱 Взято номеров: {stats.get('number_taken', 0)}\n"
        msg += f"🔢 Взято кодов: {stats.get('code_taken', 0)}\n"
        msg += f"✅ Успешных вставов: {stats.get('success', 0)}\n"
        await event.reply(msg, parse_mode='markdown')
    
    @bot.on(events.NewMessage(pattern='/status'))
    async def status_cmd(event):
        user_id = event.sender_id
        session_string, phone, _ = db.get_session(user_id)
        source_group, target_group = db.get_groups(user_id)
        msg = f"⚙️ **СТАТУС**\n\n"
        msg += f"👤 Аккаунт: {phone or '❌ не авторизован'}\n"
        msg += f"📥 ГРУППА 1 (номера+коды): {source_group or '❌'}\n"
        msg += f"📤 ГРУППА 2 (отправка+встал): {target_group or '❌'}\n"
        await event.reply(msg, parse_mode='markdown')
    
    # ========== ОСНОВНАЯ ЛОГИКА ==========
    async def start_source_listener(user_id, client, source_group_id, notify_chat_id):
        """Слушаем ГРУППУ 1 — здесь номера и коды"""
        
        @client.on(events.NewMessage(chats=source_group_id))
        async def handle_source_messages(event):
            # Игнорируем свои сообщения
            if event.sender_id == (await client.get_me()).id:
                return
            
            text = event.raw_text.strip()
            
            # 1. Ищем НОМЕР
            phone_match = re.search(r'(\+?\d{10,15})', text)
            if phone_match:
                phone = phone_match.group(1)
                
                # Сохраняем номер в БД
                db.add_pending_number(user_id, phone)
                db.add_stat(user_id, phone, 'number_taken')
                
                # Отправляем НОМЕР в TARGET группу
                _, target_group = db.get_groups(user_id)
                if target_group:
                    await client.send_message(
                        target_group,
                        f"📱 **НОМЕР ДЛЯ АВТОВБИВА**\n\n`{phone}`\n\n⏳ Ожидание кода..."
                    )
                    
                    await bot.send_message(
                        notify_chat_id,
                        f"📱 **Взял номер:** `{phone}`\n➡️ Отправлен в целевую группу"
                    )
            
            # 2. Ищем КОД (4-8 цифр, не похож на номер)
            code_match = re.search(r'\b(\d{4,8})\b', text)
            if code_match and not phone_match:
                code = code_match.group(1)
                
                # Находим последний номер без кода
                last_phone = db.get_last_pending_number(user_id)
                
                if last_phone:
                    # Обновляем запись с кодом
                    db.update_pending_with_code(last_phone, code)
                    db.add_stat(user_id, last_phone, 'code_taken')
                    
                    # Отправляем КОД в TARGET группу
                    _, target_group = db.get_groups(user_id)
                    if target_group:
                        await client.send_message(
                            target_group,
                            f"🔢 **КОД ПОДТВЕРЖДЕНИЯ**\n\nКод: `{code}`\nДля номера: `{last_phone}`\n\n✅ Ожидание подтверждения 'встал'..."
                        )
                        
                        await bot.send_message(
                            notify_chat_id,
                            f"🔢 **Взял код:** `{code}` для номера `{last_phone}`\n➡️ Отправлен в целевую группу"
                        )
    
    @bot.on(events.NewMessage())
    async def handle_target_messages(event):
        """Слушаем ГРУППУ 2 — здесь пишут 'встал'"""
        user_id = event.sender_id
        _, target_group = db.get_groups(user_id)
        
        if not target_group or event.chat_id != target_group:
            return
        
        text = event.raw_text.lower()
        
        # Ищем "встал" или "успех"
        if 'встал' in text or 'успех' in text or 'success' in text:
            # Ищем номер в сообщении
            phone_match = re.search(r'(\+?\d{10,15})', text)
            if phone_match:
                phone = phone_match.group(1)
                
                # Помечаем как успех
                db.mark_success(phone)
                db.add_stat(user_id, phone, 'success')
                
                await bot.send_message(
                    event.chat_id,
                    f"✅ **{phone} — ВСТАЛ!**\n💎 Аккаунт успешно автовбит!"
                )
    
    # Запускаем слушатели
    async def start_listeners():
        db.cursor.execute('SELECT user_id FROM sessions WHERE session_string IS NOT NULL AND session_string != ""')
        users = db.cursor.fetchall()
        for (user_id,) in users:
            session_string, _, _ = db.get_session(user_id)
            source_group, _ = db.get_groups(user_id)
            if session_string and source_group:
                client = await get_user_client(user_id, session_string)
                if client:
                    await start_source_listener(user_id, client, source_group, user_id)
    
    asyncio.create_task(start_listeners())

# ========== ЗАПУСК ==========
async def main():
    global bot
    print("💎 DIAMOND AUTOVBIV BOT — ФИНАЛЬНАЯ ВЕРСИЯ")
    print(f"📡 API_ID: {API_ID}")
    
    bot = TelegramClient("diamond_bot", API_ID, API_HASH).start(bot_token=BOT_TOKEN)
    await bot.connect()
    
    await setup_handlers()
    print("✅ Бот запущен!")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
