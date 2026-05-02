import asyncio
import json
import logging
from telebot import TeleBot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton
from pymax import MaxClient
from pymax.payloads import UserAgentPayload

BOT_TOKEN = "8407984730:AAGVNP8TWRP7AcsrWk5xod0z8qbsW7qt3lE"

bot = TeleBot(BOT_TOKEN)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
user_states = {}

def main_keyboard():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add(KeyboardButton("📱 Войти по номеру"), KeyboardButton("🔑 Войти по токену"))
    return markup

def cancel_keyboard():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add(KeyboardButton("❌ Отмена"))
    return markup

async def login_via_token(access_token, device_params):
    # Настраиваем точный "отпечаток" устройства
    ua = UserAgentPayload(
        device_type=device_params.get("deviceType", "DESKTOP"),
        app_version=device_params.get("appVersion", "26.2.3"),
        system_version=device_params.get("osVersion", "macOS Sonoma 14.5"),
        screen=device_params.get("screen", "1440x900 2.0x"),
        timezone=device_params.get("timezone", "Asia/Vladivostok"),
        locale=device_params.get("locale", "ru-RU"),
        device_id=device_params.get("deviceId", "581a9ea526a673bd"),
        client_session_id=device_params.get("clientSessionId", 17),
        user_agent=device_params.get("headerUserAgent", 
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.60 Safari/537.36")
    )

    client = MaxClient(
        token=access_token,
        work_dir="cache",
        headers=ua,
        tls_verify=False  # аналог MAX_TLS_INSECURE=1
    )

    try:
        await client.start()
        me = client.me
        info = f"ID: {me.id}\nИмя: {me.firstname} {me.lastname or ''}\nТелефон: {me.phone}"
        await client.stop()
        return True, info
    except Exception as e:
        await client.stop()
        return False, f"Ошибка: {e}"

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
        del user_states[chat_id]
        bot.reply_to(message, "⏳ Вход по номеру временно отключён.", reply_markup=main_keyboard())
        return

    if state == "waiting_token":
        access_token = text.replace(" ", "").replace("\n", "")
        if len(access_token) < 50:
            bot.reply_to(message, "❌ Токен слишком короткий.", reply_markup=cancel_keyboard())
            return
        user_states[chat_id] = {"state": "waiting_json", "token": access_token}
        bot.reply_to(message, "📲 Теперь отправьте JSON с параметрами устройства одной строкой:\n*deviceType*, *clientSessionId*, *headerUserAgent*, *osVersion*, *screen*, *timezone*, *locale*", parse_mode="Markdown", reply_markup=cancel_keyboard())
        return

    if state == "waiting_json":
        access_token = user_states[chat_id]["token"]
        try:
            device_params = json.loads(text)
        except json.JSONDecodeError:
            bot.reply_to(message, "❌ Неверный JSON. Попробуйте ещё раз.", reply_markup=cancel_keyboard())
            return
        if not device_params.get("deviceType") or not device_params.get("clientSessionId"):
            bot.reply_to(message, "❌ В JSON обязательно нужны поля *deviceType* и *clientSessionId*.", parse_mode="Markdown", reply_markup=cancel_keyboard())
            return
        bot.reply_to(message, "🔍 Выполняю вход в аккаунт...")
        valid, info = asyncio.run(login_via_token(access_token, device_params))
        if valid:
            bot.reply_to(message, f"🟢 **АККАУНТ ЖИВОЙ!**\n\n```\n{info}\n```", parse_mode="Markdown", reply_markup=main_keyboard())
            with open("valid_accounts.txt", "a") as f:
                f.write(f"[TOKEN] {access_token[:50]}... | {info}\n")
        else:
            bot.reply_to(message, f"🔴 **АККАУНТ МЁРТВ**\n\n{info}", reply_markup=main_keyboard())
        del user_states[chat_id]
        return

if __name__ == "__main__":
    print("🤖 MAX Validator запущен...")
    bot.infinity_polling()