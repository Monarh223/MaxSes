import os, asyncio, re, sqlite3, shutil
from datetime import datetime
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError
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
    print("❌ Установите API_ID, API_HASH, BOT_TOKEN в Railway")
    exit(1)

# ========== БАЗА ДАННЫХ ==========
class DB:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.c = self.conn.cursor()
        self._init_tables()
    
    def _init_tables(self):
        self.c.execute('''CREATE TABLE IF NOT EXISTS sessions 
            (user_id INTEGER PRIMARY KEY, phone TEXT, session_string TEXT, step TEXT, created_at TIMESTAMP)''')
        self.c.execute('''CREATE TABLE IF NOT EXISTS groups 
            (user_id INTEGER PRIMARY KEY, source_group INTEGER, target_group INTEGER)''')
        self.c.execute('''CREATE TABLE IF NOT EXISTS queue 
            (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, phone TEXT, status TEXT, created_at TIMESTAMP)''')
        self.c.execute('''CREATE TABLE IF NOT EXISTS pending 
            (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, phone TEXT, code TEXT, status TEXT, created_at TIMESTAMP)''')
        self.c.execute('''CREATE TABLE IF NOT EXISTS issued 
            (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, phone TEXT, request_msg_id INTEGER, target_chat_id INTEGER, status TEXT, created_at TIMESTAMP)''')
        self.c.execute('''CREATE TABLE IF NOT EXISTS stats 
            (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, phone TEXT, action TEXT, timestamp TIMESTAMP)''')
        self.conn.commit()
    
    def save_session(self, uid, phone, sess, step):
        self.c.execute('REPLACE INTO sessions (user_id, phone, session_string, step, created_at) VALUES (?,?,?,?,?)',
                       (uid, phone, sess, step, datetime.now()))
        self.conn.commit()
    
    def get_session(self, uid):
        row = self.c.execute('SELECT session_string, phone, step FROM sessions WHERE user_id=?', (uid,)).fetchone()
        return row if row else (None, None, None)
    
    def is_auth(self, uid): return bool(self.get_session(uid)[0])
    
    def set_groups(self, uid, src, tgt):
        self.c.execute('REPLACE INTO groups (user_id, source_group, target_group) VALUES (?,?,?)', (uid, src, tgt))
        self.conn.commit()
    
    def get_groups(self, uid):
        row = self.c.execute('SELECT source_group, target_group FROM groups WHERE user_id=?', (uid,)).fetchone()
        return row if row else (None, None)
    
    # ----- Очередь -----
    def add_to_queue(self, uid, phone):
        self.c.execute('INSERT INTO queue (user_id, phone, status, created_at) VALUES (?,?,?,?)',
                       (uid, phone, 'waiting', datetime.now()))
        self.conn.commit()
    
    def get_next_queued(self, uid):
        row = self.c.execute('SELECT id, phone FROM queue WHERE user_id=? AND status="waiting" ORDER BY created_at LIMIT 1', (uid,)).fetchone()
        return row if row else (None, None)
    
    def mark_queued_sent(self, qid):
        self.c.execute('UPDATE queue SET status="sent" WHERE id=?', (qid,))
        self.conn.commit()
    
    def get_queue_list(self, uid):
        return self.c.execute('SELECT phone, status, created_at FROM queue WHERE user_id=? ORDER BY created_at', (uid,)).fetchall()
    
    # ----- Выданные номера (связка с пользователем в цели) -----
    def add_issued(self, uid, phone, request_msg_id, target_chat_id):
        self.c.execute('INSERT INTO issued (user_id, phone, request_msg_id, target_chat_id, status, created_at) VALUES (?,?,?,?,?,?)',
                       (uid, phone, request_msg_id, target_chat_id, 'waiting_code', datetime.now()))
        self.conn.commit()
    
    def get_issued_by_phone(self, phone):
        row = self.c.execute('SELECT id, request_msg_id, target_chat_id, status FROM issued WHERE phone=? AND status="waiting_code"', (phone,)).fetchone()
        return row if row else (None, None, None, None)
    
    def update_issued_code(self, phone, code):
        self.c.execute('UPDATE pending SET code=?, status="code_received" WHERE phone=? AND status="waiting_code"', (code, phone))
        # также обновим статус в issued на 'code_sent' после отправки
        pass
    
    def mark_issued_success(self, phone):
        self.c.execute('UPDATE issued SET status="success" WHERE phone=? AND status="waiting_code"', (phone,))
        self.c.execute('UPDATE pending SET status="success" WHERE phone=?', (phone,))
        self.conn.commit()
    
    def mark_issued_failed(self, phone):
        self.c.execute('UPDATE issued SET status="failed" WHERE phone=? AND status="waiting_code"', (phone,))
        self.conn.commit()
    
    # ----- Привязка номеров и кодов (pending) -----
    def add_pending(self, uid, phone):
        self.c.execute('INSERT INTO pending (user_id, phone, status, created_at) VALUES (?,?,"waiting_code",?)',
                       (uid, phone, datetime.now()))
        self.conn.commit()
    
    def update_pending_code(self, phone, code):
        self.c.execute('UPDATE pending SET code=?, status="code_received" WHERE phone=? AND status="waiting_code"', (code, phone))
        self.conn.commit()
    
    def get_last_pending_phone(self, uid):
        row = self.c.execute('SELECT phone FROM pending WHERE user_id=? AND status="waiting_code" ORDER BY created_at DESC LIMIT 1', (uid,)).fetchone()
        return row[0] if row else None
    
    # ----- Статистика -----
    def add_stat(self, uid, phone, action):
        self.c.execute('INSERT INTO stats (user_id, phone, action, timestamp) VALUES (?,?,?,?)', (uid, phone, action, datetime.now()))
        self.conn.commit()
    
    def stats_today(self, uid):
        today = datetime.now().replace(hour=0, minute=0, second=0)
        d = dict(self.c.execute('SELECT action, COUNT(*) FROM stats WHERE user_id=? AND timestamp>=? GROUP BY action', (uid, today)).fetchall())
        return d.get('number_taken',0), d.get('code_taken',0), d.get('success',0)
    
    # ----- Экспорт / импорт -----
    def export_db(self, uid):
        p = os.path.join(BACKUP_DIR, f"backup_{uid}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
        shutil.copy2(DB_PATH, p)
        return p
    
    def import_db(self, path):
        shutil.copy2(path, DB_PATH)
        self.conn.close()
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.c = self.conn.cursor()
        self._init_tables()

db = DB()

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def clean_phone(p):
    p = re.sub(r'[^\d+]', '', p).lstrip('+')
    if p.startswith('8'): p = '7' + p[1:]
    if len(p) == 10: p = '7' + p
    if len(p) == 11: return '+' + p
    return None

def extract_phone(text):
    for m in re.findall(r'\+?\d{10,12}', text):
        c = clean_phone(m)
        if c: return c
    return None

def extract_code(text):
    m = re.search(r'\b(\d{4,8})\b', text)
    return m.group(1) if m else None

def code_keyboard():
    return [[Button.inline(str(i), str(i).encode()) for i in row] for row in [[1,2,3],[4,5,6],[7,8,9]]] + \
           [[Button.inline("0", b"0"), Button.inline("⌫", b"del"), Button.inline("✅", b"submit")]]

# ========== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ==========
bot_client = None
user_client = None
owner_id = None
code_inputs = {}

# ========== ОБРАБОТЧИК ЮЗЕР-КЛИЕНТА (ГРУППЫ) ==========
async def setup_user_listener():
    global user_client, owner_id
    if not user_client or not owner_id:
        return

    @user_client.on(events.NewMessage())
    async def handle_all_messages(event):
        if event.sender_id == (await user_client.get_me()).id:
            return
        chat_id = event.chat_id
        uid = owner_id
        text = event.raw_text.strip()

        # 1. Команды /set_source, /set_target
        if re.match(r'(?i)^/set_source$', text):
            if event.is_group:
                db.set_groups(uid, chat_id, db.get_groups(uid)[1])
                await event.reply(f"✅ Источник = {chat_id}")
                print(f"[INFO] Источник установлен: {chat_id}")
            return
        if re.match(r'(?i)^/set_target$', text):
            if event.is_group:
                db.set_groups(uid, db.get_groups(uid)[0], chat_id)
                await event.reply(f"✅ Цель = {chat_id}")
                print(f"[INFO] Цель установлена: {chat_id}")
            return

        # 2. Команда /очередь (только в группах, не в ЛС)
        if re.match(r'(?i)^/очередь$', text):
            queue = db.get_queue_list(uid)
            if not queue:
                await event.reply("📭 Очередь пуста.")
                return
            msg = "📋 Очередь номеров:\n"
            for phone, status, ts in queue:
                status_txt = "⏳ ожидает" if status == "waiting" else "✅ выдан"
                msg += f"• {phone} ({status_txt}) - {ts.strftime('%H:%M')}\n"
                if len(msg) > 3500:
                    await event.reply(msg)
                    msg = ""
            if msg:
                await event.reply(msg)
            return

        source, target = db.get_groups(uid)

        # 3. Обработка сообщений в группе-источнике: номера и коды
        if source and chat_id == source:
            phone = extract_phone(text)
            if phone:
                # Добавляем в очередь
                db.add_to_queue(uid, phone)
                db.add_pending(uid, phone)
                db.add_stat(uid, phone, 'number_taken')
                # Пишем в источник: "Взял в очередь: номер"
                await event.reply(f"📥 Взял в очередь: `{phone}`")
                print(f"[SOURCE] Номер {phone} добавлен в очередь")
                return
            code = extract_code(text)
            if code:
                # Находим последний ожидающий номер (тот, который недавно выдан)
                # Сначала проверим есть ли в issued с этим номером в статусе waiting_code
                last_phone = db.get_last_pending_phone(uid)
                if last_phone:
                    db.update_pending_code(last_phone, code)
                    db.add_stat(uid, last_phone, 'code_taken')
                    print(f"[SOURCE] Код {code} для {last_phone} сохранён")
                    # Теперь нужно отправить код в цель тому пользователю, кто запросил номер
                    issued_id, req_msg_id, target_chat, status = db.get_issued_by_phone(last_phone)
                    if req_msg_id and target_chat:
                        try:
                            # Отправляем код реплаем на исходное сообщение "номер" в цели
                            await user_client.send_message(target_chat, f"🔢 Код: `{code}`", reply_to=req_msg_id)
                            print(f"[SOURCE] Код {code} отправлен в цель {target_chat} (reply to {req_msg_id})")
                        except Exception as e:
                            print(f"[ERROR] Не удалось отправить код: {e}")
                    else:
                        print(f"[WARN] Нет активной выдачи для номера {last_phone}")
                return

        # 4. Обработка сообщений в группе-цели: команда "номер", "встал", "невстал"
        if target and chat_id == target:
            low = text.lower()
            # Команда "номер" – выдать следующий из очереди
            if low == 'номер':
                qid, phone = db.get_next_queued(uid)
                if phone:
                    # Помечаем номер как выданный в очереди
                    db.mark_queued_sent(qid)
                    # Сохраняем выдачу: запоминаем, кому выдали и id сообщения-запроса
                    db.add_issued(uid, phone, event.message.id, chat_id)
                    # Отвечаем в цель: выданный номер (реплаем на сообщение "номер")
                    await event.reply(f"📱 Номер: `{phone}`")
                    # Уведомляем источник: "Номер выдан, ожидайте код"
                    if source:
                        await user_client.send_message(source, f"📤 Номер `{phone}` выдан, ожидайте код")
                    print(f"[TARGET] Выдан номер {phone} пользователю {event.sender_id}")
                else:
                    await event.reply("❌ Нет номеров в очереди.")
                return
            # Обработка "встал"
            if 'встал' in low:
                phone = extract_phone(text)
                if not phone:
                    # возможно последний выданный номер
                    # можно найти в issued последний для этого чата
                    pass
                if phone:
                    db.mark_issued_success(phone)
                    db.add_stat(uid, phone, 'success')
                    await event.reply(f"✅ {phone} — ВСТАЛ!")
                    print(f"[TARGET] Успех для {phone}")
                return
            # Обработка "невстал"
            if 'невстал' in low:
                phone = extract_phone(text)
                if phone:
                    db.mark_issued_failed(phone)
                    # Можно вернуть номер в очередь? (опционально)
                    await event.reply(f"❌ {phone} — НЕ ВСТАЛ. Повторите попытку.")
                    print(f"[TARGET] Неудача для {phone}")
                return

# ========== ОБРАБОТЧИК БОТА (ЛИЧНЫЕ СООБЩЕНИЯ) ==========
async def setup_bot_handlers():
    global bot_client, user_client, owner_id, code_inputs

    @bot_client.on(events.NewMessage(pattern='/start'))
    async def start_cmd(e):
        await e.reply("💎 Diamond AutoVbiv с очередью и реплаями\n/login +7xxx\n/status\n/stats\n/export\n/import\n/restore <session_string>\n/reload\n/reset\n\nВ группах:\n/set_source\n/set_target\n/очередь\n`номер` – получить номер\n`встал` / `невстал`")

    @bot_client.on(events.NewMessage(pattern='/login (.+)'))
    async def login_cmd(e):
        global user_client, owner_id, code_inputs
        uid = e.sender_id
        phone = clean_phone(e.pattern_match.group(1))
        if not phone:
            return await e.reply("❌ Неверный номер")
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        try:
            await client.send_code_request(phone)
            user_client = client
            owner_id = uid
            db.save_session(uid, phone, "", "waiting_code")
            code_inputs[uid] = {'code': '', 'phone': phone, 'attempts': 0}
            await e.reply(f"✅ Код отправлен на {phone}\nВведите код кнопками:", buttons=code_keyboard())
        except Exception as ex:
            await e.reply(f"❌ {ex}")

    @bot_client.on(events.CallbackQuery())
    async def callback_handler(e):
        global code_inputs, user_client, owner_id
        uid = e.sender_id
        if uid not in code_inputs:
            return await e.answer("Сначала /login", alert=True)
        data = e.data.decode()
        cur = code_inputs[uid]
        if data.isdigit():
            cur['code'] += data
            await e.answer(f"Код: {cur['code']}")
            await e.edit(f"Код: `{cur['code']}`", buttons=code_keyboard())
        elif data == 'del':
            cur['code'] = cur['code'][:-1]
            await e.answer("Удалено")
            await e.edit(f"Код: `{cur['code']}`", buttons=code_keyboard())
        elif data == 'submit':
            if not cur['code']:
                return await e.answer("Введите код", alert=True)
            await e.answer("Проверка...")
            client = user_client
            if not client:
                return await e.edit("❌ Клиент потерян, /login заново")
            try:
                await client.sign_in(code=cur['code'])
                sess = client.session.save()
                me = await client.get_me()
                db.save_session(uid, me.phone, sess, "authorized")
                owner_id = uid
                await setup_user_listener()
                await e.edit(f"✅ Вход! Аккаунт: {me.phone}\nSession: `{sess}`")
                await e.client.send_message(uid, f"🔑 Сохраните session_string:\n`{sess}`")
                del code_inputs[uid]
            except PhoneCodeInvalidError:
                cur['attempts'] += 1
                if cur['attempts'] >= 3:
                    await e.edit("❌ 3 ошибки, /login заново")
                    del code_inputs[uid]
                else:
                    cur['code'] = ''
                    await e.edit(f"❌ Неверный код. Попыток: {3-cur['attempts']}\nКод: ` `", buttons=code_keyboard())
            except SessionPasswordNeededError:
                db.save_session(uid, cur['phone'], "", "waiting_2fa")
                await e.edit("🔐 Требуется 2FA: /2fa <пароль>")
                del code_inputs[uid]
            except Exception as ex:
                await e.edit(f"❌ {ex}")
                del code_inputs[uid]

    @bot_client.on(events.NewMessage(pattern='/2fa (.+)'))
    async def twofa_cmd(e):
        global user_client, owner_id
        uid = e.sender_id
        pwd = e.pattern_match.group(1)
        _, _, step = db.get_session(uid)
        client = user_client
        if not client or step != "waiting_2fa":
            return await e.reply("❌ Сначала /login")
        try:
            await client.sign_in(password=pwd)
            sess = client.session.save()
            me = await client.get_me()
            db.save_session(uid, me.phone, sess, "authorized")
            owner_id = uid
            await setup_user_listener()
            await e.reply(f"✅ Вход с 2FA! Аккаунт: {me.phone}\nSession: `{sess}`")
        except Exception as ex:
            await e.reply(f"❌ {ex}")

    @bot_client.on(events.NewMessage(pattern='/status'))
    async def status_cmd(e):
        uid = e.sender_id
        sess, phone, _ = db.get_session(uid)
        src, tgt = db.get_groups(uid)
        await e.reply(f"Аккаунт: {phone or '❌'}\nАвторизован: {'✅' if sess else '❌'}\nИсточник: {src or '❌'}\nЦель: {tgt or '❌'}")

    @bot_client.on(events.NewMessage(pattern='/stats'))
    async def stats_cmd(e):
        uid = e.sender_id
        n, c, s = db.stats_today(uid)
        await e.reply(f"📊 Сегодня:\nНомера: {n}\nКоды: {c}\nВстало: {s}")

    @bot_client.on(events.NewMessage(pattern='/export'))
    async def export_cmd(e):
        uid = e.sender_id
        if not db.is_auth(uid):
            return await e.reply("❌ Сначала /login")
        path = db.export_db(uid)
        await e.reply("📦 БД выгружена")
        await bot_client.send_file(e.chat_id, path, caption="diamond_data.db")

    @bot_client.on(events.NewMessage(pattern='/import'))
    async def import_req(e):
        uid = e.sender_id
        await e.reply("📥 Отправьте файл .db")
        @bot_client.on(events.NewMessage(func=lambda m: m.sender_id == uid and m.file and m.file.name.endswith('.db')))
        async def do_imp(msg):
            global user_client, owner_id
            path = f"/tmp/imp_{uid}.db"
            await msg.download_media(path)
            db.import_db(path)
            os.remove(path)
            sess, phone, _ = db.get_session(uid)
            if sess:
                try:
                    cl = TelegramClient(StringSession(sess), API_ID, API_HASH)
                    await cl.connect()
                    if await cl.is_user_authorized():
                        user_client = cl
                        owner_id = uid
                        await setup_user_listener()
                        await msg.reply(f"✅ Импорт OK, сессия для {phone} восстановлена")
                    else:
                        await msg.reply("⚠️ БД импортирована, но сессия недействительна. /login")
                except Exception as ex:
                    await msg.reply(f"❌ Ошибка: {ex}")
            else:
                await msg.reply("✅ Импорт выполнен, сессии нет. /login")

    @bot_client.on(events.NewMessage(pattern='/restore (.+)'))
    async def restore_cmd(e):
        global user_client, owner_id
        uid = e.sender_id
        sess_str = e.pattern_match.group(1)
        try:
            cl = TelegramClient(StringSession(sess_str), API_ID, API_HASH)
            await cl.connect()
            if await cl.is_user_authorized():
                me = await cl.get_me()
                db.save_session(uid, me.phone, sess_str, "authorized")
                user_client = cl
                owner_id = uid
                await setup_user_listener()
                await e.reply(f"✅ Сессия восстановлена для {me.phone}")
            else:
                await e.reply("❌ Невалидная session_string")
        except Exception as ex:
            await e.reply(f"❌ {ex}")

    @bot_client.on(events.NewMessage(pattern='/reload'))
    async def reload_cmd(e):
        global user_client, owner_id
        if not user_client or not owner_id:
            return await e.reply("❌ Сначала /login")
        await setup_user_listener()
        await e.reply("✅ Обработчики перезагружены")

    @bot_client.on(events.NewMessage(pattern='/reset'))
    async def reset_cmd(e):
        global user_client, owner_id, code_inputs
        uid = e.sender_id
        if user_client:
            await user_client.disconnect()
        user_client = None
        owner_id = None
        if uid in code_inputs:
            del code_inputs[uid]
        db.c.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
        db.c.execute('DELETE FROM groups WHERE user_id=?', (uid,))
        db.c.execute('DELETE FROM queue WHERE user_id=?', (uid,))
        db.c.execute('DELETE FROM pending WHERE user_id=?', (uid,))
        db.c.execute('DELETE FROM issued WHERE user_id=?', (uid,))
        db.conn.commit()
        await e.reply("✅ Сброшено. Используйте /login")

# ========== ВОССТАНОВЛЕНИЕ СЕССИИ ПРИ СТАРТЕ ==========
async def restore_session_on_start():
    global user_client, owner_id
    rows = db.c.execute('SELECT user_id, session_string FROM sessions WHERE session_string IS NOT NULL AND session_string!=""').fetchall()
    for uid, ss in rows:
        try:
            cl = TelegramClient(StringSession(ss), API_ID, API_HASH)
            await cl.connect()
            if await cl.is_user_authorized():
                user_client = cl
                owner_id = uid
                await setup_user_listener()
                print(f"[+] Сессия восстановлена для user {uid}")
                return
            else:
                print(f"[-] Сессия для {uid} невалидна")
        except Exception as e:
            print(f"[-] Ошибка восстановления {uid}: {e}")

# ========== ЗАПУСК ==========
async def main():
    global bot_client
    print("💎 Diamond AutoVbiv FINAL")
    db._init_tables()
    bot_client = TelegramClient("diamond_bot", API_ID, API_HASH, flood_sleep_threshold=0)
    await bot_client.start(bot_token=BOT_TOKEN)
    await restore_session_on_start()
    await setup_bot_handlers()
    print("✅ Бот запущен. Новая логика: очередь, реплаи, уведомления в группе источника.")
    await bot_client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
