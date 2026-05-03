# diamond_autovbiv.py
# FULL FIXED - КОМАНДЫ РАБОТАЮТ, БЕЗ РЕКЛАМЫ

import os
import asyncio
import re
import sqlite3
import shutil
from datetime import datetime
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, FloodWaitError
from telethon.tl.custom import Button
from telethon.tl.functions.channels import JoinChannelRequest

# ========== КОНФИГ ==========
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

SESSION_DIR = os.environ.get("SESSION_DIR", "/app/sessions")
os.makedirs(SESSION_DIR, exist_ok=True)

DB_PATH = "/app/diamond_data.db"
BACKUP_DIR = "/app/backups"
os.makedirs(BACKUP_DIR, exist_ok=True)

if not API_ID or not API_HASH or not BOT_TOKEN:
    print("❌ Ошибка: Установите переменные в Railway")
    exit(1)

# ========== ОТКЛЮЧАЕМ РЕКЛАМУ TELEGRAM ==========
# Убираем принудительную подписку на каналы

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
        """Создаёт копию БД"""
        backup_path = os.path.join(BACKUP_DIR, f"diamond_backup_user_{user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
        shutil.copy2(DB_PATH, backup_path)
        return backup_path
    
    def import_db(self, file_path):
        """Восстанавливает БД из файла"""
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

# ========== ФУНКЦИИ ==========
def clean_phone(phone):
    """Очищает номер телефона"""
    cleaned = re.sub(r'[^\d+]', '', phone)
    if not cleaned.startswith('+'):
        cleaned = '+' + cleaned
    return cleaned

def extract_phone_from_text(text):
    """Извлекает номер из любого текста"""
    patterns = [
        r'\+?\d{11,15}',
        r'\+?\d{1,3}[-.\s]?\(?\d{1,4}\)?[-.\s]?\d{1,5}[-.\s]?\d{1,5}',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return clean_phone(match.group())
    return None

def extract_code_from_text(text):
    """Извлекает код (4-8 цифр)"""
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
    
    session_path = os.path.join(SESSION_DIR, f"user_{user_id}")
    client = TelegramClient(session_path, API_ID, API_HASH)
    await client.connect()
    
    if session_string:
        client.session.set_session_str(session_string)
        if await client.is_user_authorized():
            user_clients[user_id] = client
            return client
    return None

# ========== ОБРАБОТЧИКИ КОМАНД (РАБОТАЮТ ВЕЗДЕ) ==========
async def setup_handlers():
    global bot
    
    @bot.on(events.NewMessage(pattern='/start'))
    async def start_cmd(event):
        await event.reply("""
💎 **DIAMOND AUTOVBIV BOT** 💎

/login +79991234567 — вход
/set_source — в **ЭТОЙ** группе (источник номеров и кодов)
/set_target — в **ЭТОЙ** группе (куда сливать и где "встал")
/stats — статистика
/status — статус
/export — выгрузить БД
/import — импорт БД (требуется файл)
/reset — сбросить всё
        """)
    
    @bot.on(events.NewMessage(pattern='/login (.+)'))
    async def login_cmd(event):
        user_id = event.sender_id
        phone = clean_phone(event.pattern_match.group(1).strip())
        
        await event.reply(f"📱 Отправляю код на {phone}...")
        
        session_path = os.path.join(SESSION_DIR, f"temp_{user_id}")
        client = TelegramClient(session_path, API_ID, API_HASH)
        await client.connect()
        
        try:
            await client.send_code_request(phone)
            user_clients[user_id] = client
            db.save_session(user_id, phone, "", "waiting_code")
            user_code_inputs[user_id] = {'code': '', 'phone': phone, 'attempts': 0}
            
            await event.reply(
                f"✅ Код отправлен на {phone}\n\n🔢 **Введите код через кнопки:**\n\nКод: ` `",
                buttons=get_code_keyboard()
            )
        except FloodWaitError as e:
            await event.reply(f"❌ Подождите {e.seconds} сек")
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
            await event.reply(f"✅ **Вход выполнен!**\nАккаунт: {phone}")
        except Exception as e:
            await event.reply(f"❌ Ошибка: {str(e)}")
    
    @bot.on(events.CallbackQuery())
    async def callback_handler(event):
        user_id = event.sender_id
        data = event.data.decode('utf-8')
        
        if user_id not in user_code_inputs:
            await event.answer("❌ Сначала /login", alert=True)
            return
        
        current_code = user_code_inputs[user_id]['code']
        phone = user_code_inputs[user_id]['phone']
        
        if data.isdigit():
            user_code_inputs[user_id]['code'] += data
            new_code = user_code_inputs[user_id]['code']
            await event.answer(f"Код: {new_code}")
            await event.edit(
                f"✅ Код отправлен на {phone}\n\n🔢 **Код:** `{new_code}`",
                buttons=get_code_keyboard()
            )
        
        elif data == 'del':
            user_code_inputs[user_id]['code'] = current_code[:-1]
            new_code = user_code_inputs[user_id]['code']
            await event.answer("Удалено")
            await event.edit(
                f"✅ Код отправлен на {phone}\n\n🔢 **Код:** `{new_code}`",
                buttons=get_code_keyboard()
            )
        
        elif data == 'submit':
            code = user_code_inputs[user_id]['code']
            if not code:
                await event.answer("❌ Введите код!", alert=True)
                return
            
            await event.answer(f"⏳ Проверяю...")
            client = user_clients.get(user_id)
            
            try:
                await client.sign_in(code=code)
                session_string = client.session.save()
                user_phone = (await client.get_me()).phone
                db.save_session(user_id, user_phone, session_string, "authorized")
                await event.edit(f"✅ **Вход выполнен!**\nАккаунт: {user_phone}\n\nТеперь настройте группы:\n/set_source — в группе с номерами/кодами\n/set_target — в группе для слива")
                del user_code_inputs[user_id]
            except PhoneCodeInvalidError:
                user_code_inputs[user_id]['attempts'] += 1
                if user_code_inputs[user_id]['attempts'] >= 3:
                    await event.edit(f"❌ 3 неверных попытки. /login заново")
                    del user_code_inputs[user_id]
                else:
                    user_code_inputs[user_id]['code'] = ''
                    await event.edit(f"❌ Неверный код! Попробуйте ещё раз\n\nКод: ` `", buttons=get_code_keyboard())
            except SessionPasswordNeededError:
                db.save_session(user_id, phone, "", "waiting_2fa")
                await event.edit(f"🔐 Требуется 2FA: /2fa <пароль>")
                del user_code_inputs[user_id]
            except Exception as e:
                await event.edit(f"❌ {str(e)}")
                del user_code_inputs[user_id]
    
    # ========== НАСТРОЙКА ГРУПП (РАБОТАЕТ В ГРУППАХ!) ==========
    @bot.on(events.NewMessage(pattern='/set_source'))
    async def set_source_cmd(event):
        user_id = event.sender_id
        if event.is_group:
            source_group = event.chat_id
            _, target_group = db.get_groups(user_id)
            db.set_groups(user_id, source_group, target_group)
            await event.reply(f"✅ **ГРУППА ИСТОЧНИК установлена!**\nСюда буду смотреть номера и коды.")
            
            # Запускаем слушатель
            session_string, _, _ = db.get_session(user_id)
            if session_string:
                client = await get_user_client(user_id, session_string)
                if client:
                    asyncio.create_task(start_source_listener(user_id, client, source_group))
        else:
            await event.reply("❌ Эту команду нужно писать В ГРУППЕ, которую хотите сделать источником!")
    
    @bot.on(events.NewMessage(pattern='/set_target'))
    async def set_target_cmd(event):
        user_id = event.sender_id
        if event.is_group:
            target_group = event.chat_id
            source_group, _ = db.get_groups(user_id)
            db.set_groups(user_id, source_group, target_group)
            await event.reply(f"✅ **ГРУППА ЦЕЛЬ установлена!**\nСюда буду отправлять номера и коды, здесь жду 'встал'.")
        else:
            await event.reply("❌ Эту команду нужно писать В ГРУППЕ, которую хотите сделать целью!")
    
    @bot.on(events.NewMessage(pattern='/stats'))
    async def stats_cmd(event):
        user_id = event.sender_id
        stats = db.get_stats_today(user_id)
        msg = f"📊 **СТАТИСТИКА**\n\n"
        msg += f"📱 Номеров: {stats.get('number_taken', 0)}\n"
        msg += f"🔢 Кодов: {stats.get('code_taken', 0)}\n"
        msg += f"✅ Встало: {stats.get('success', 0)}"
        await event.reply(msg)
    
    @bot.on(events.NewMessage(pattern='/status'))
    async def status_cmd(event):
        user_id = event.sender_id
        _, phone, _ = db.get_session(user_id)
        source, target = db.get_groups(user_id)
        msg = f"⚙️ **СТАТУС**\n\n"
        msg += f"👤 Аккаунт: {phone or '❌'}\n"
        msg += f"📥 Источник: {source or '❌'}\n"
        msg += f"📤 Цель: {target or '❌'}"
        await event.reply(msg)
    
    @bot.on(events.NewMessage(pattern='/export'))
    async def export_cmd(event):
        user_id = event.sender_id
        session_string, _, _ = db.get_session(user_id)
        if not session_string:
            await event.reply("❌ Сначала /login")
            return
        
        backup_path = db.export_db(user_id)
        await event.reply("📦 **Выгрузка БД...**")
        await bot.send_file(event.chat_id, backup_path, caption="💎 diamond_data.db")
    
    @bot.on(events.NewMessage(pattern='/import'))
    async def import_cmd(event):
        await event.reply("📥 **Отправьте файл .db для импорта**")
        
        @bot.on(events.NewMessage(func=lambda e: e.file and e.file.name.endswith('.db')))
        async def handle_import(msg):
            if msg.sender_id != event.sender_id:
                return
            file_path = os.path.join(BACKUP_DIR, f"import_{msg.sender_id}.db")
            await msg.download_media(file_path)
            db.import_db(file_path)
            await msg.reply("✅ **БД восстановлена!** Бот перезагружен.")
            os._exit(0)  # Перезапуск для применения
    
    @bot.on(events.NewMessage(pattern='/reset'))
    async def reset_cmd(event):
        user_id = event.sender_id
        if user_id in user_clients:
            await user_clients[user_id].disconnect()
            del user_clients[user_id]
        db.cursor.execute('DELETE FROM sessions WHERE user_id = ?', (user_id,))
        db.cursor.execute('DELETE FROM groups WHERE user_id = ?', (user_id,))
        db.conn.commit()
        await event.reply("✅ Сброшено")
    
    # ========== ОСНОВНАЯ ЛОГИКА (ЛЮБОЙ РЕГИСТР, ЛЮБОЙ ФОРМАТ) ==========
    async def start_source_listener(user_id, client, source_group_id):
        @client.on(events.NewMessage(chats=source_group_id))
        async def handle_source(event):
            if event.sender_id == (await client.get_me()).id:
                return
            
            text = event.raw_text.strip()
            text_lower = text.lower()
            
            # 1. Ищем НОМЕР (в любом формате)
            phone = extract_phone_from_text(text)
            if phone:
                db.add_pending_number(user_id, phone)
                db.add_stat(user_id, phone, 'number_taken')
                _, target_group = db.get_groups(user_id)
                if target_group:
                    await client.send_message(target_group, f"📱 **НОМЕР:** `{phone}`")
                print(f"📱 Номер: {phone}")
                return  # Чтобы не обрабатывать как код
            
            # 2. Ищем КОД (4-8 цифр)
            code = extract_code_from_text(text)
            if code:
                last_phone = db.get_last_pending_number(user_id)
                if last_phone:
                    db.update_pending_with_code(last_phone, code)
                    db.add_stat(user_id, last_phone, 'code_taken')
                    _, target_group = db.get_groups(user_id)
                    if target_group:
                        await client.send_message(target_group, f"🔢 **КОД:** `{code}`\n📱 Для номера: `{last_phone}`")
                    print(f"🔢 Код: {code} для {last_phone}")
    
    # Слушаем "встал" в целевой группе (ЛЮБОЙ РЕГИСТР)
    @bot.on(events.NewMessage())
    async def handle_target(event):
        user_id = event.sender_id
        _, target_group = db.get_groups(user_id)
        
        if not target_group or event.chat_id != target_group:
            return
        
        text = event.raw_text.lower()
        
        if 'встал' in text or 'успех' in text or 'success' in text:
            phone = extract_phone_from_text(text)
            if phone:
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
    print("✅ Бот запущен!")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
