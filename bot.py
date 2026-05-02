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

# Тот самый User-Agent, который ты присылал
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.60 Safari/537.36"

async def login_via_ws(access_token, ua_string):
    """
    Полноценная попытка входа в MAX через WebSocket.
    Возвращает (статус, сообщение).
    """
    try:
        async with websockets.connect(
            MAX_WS_URL,
            extra_headers={"User-Agent": ua_string},
            timeout=20
        ) as ws:
            # Отправляем пакет авторизации, как это делает десктопный клиент
            auth_packet = json.dumps({
                "op": "auth",
                "token": access_token,
                "device": "desktop",
                "user_agent": ua_string
            })
            await ws.send(auth_packet)
            
            # Ждём ответ
            response = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(response)
            
            if data.get("type") == "auth_ok":
                user = data.get("user", {})
                info = f"ID: {user.get('id', 'N/A')}\nИмя: {user.get('first_name', 'N/A')} {user.get('last_name', '')}\nТелефон: {user.get('phone', 'N/A')}"
                return True, info
            else:
                return False, data.get("error", "Токен не принят")
    except Exception as e:
        return False, f"Ошибка соединения: {str(e)}"

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
        # Пока оставляем заглушку для входа по номеру, мы ее заменим позже
        bot.reply_to(message, "⏳ Функция входа по номеру временно недоступна. Пожалуйста, воспользуйтесь входом по токену.", reply_markup=main_keyboard())
        user_states.pop(chat_id, None)
        return

    elif state == "waiting_token":
        access_token = text.replace(" ", "").replace("\n", "")
        if len(access_token) < 50:
            bot.reply_to(message, "❌ Токен слишком короткий.", reply_markup=cancel_keyboard())
            return
        bot.reply_to(message, "🔍 Выполняю вход в аккаунт...")
        # Запускаем асинхронную функцию входа
        valid, info = asyncio.run(login_via_ws(access_token, USER_AGENT))
        if valid:
            bot.reply_to(message, f"🟢 **АККАУНТ ЖИВОЙ! Вход выполнен!**\n\n```\n{info}\n```", parse_mode="Markdown", reply_markup=main_keyboard())
            with open("valid_accounts.txt", "a") as f:
                f.write(f"[TOKEN] {access_token[:40]}... | {info}\n")
        else:
            bot.reply_to(message, f"🔴 **АККАУНТ МЁРТВ**\n\n{info}", reply_markup=main_keyboard())
        user_states.pop(chat_id, None)

if __name__ == "__main__":
    print("🤖 MAX Validator запущен...")
    bot.infinity_polling()