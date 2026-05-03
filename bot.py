import requests
import threading
from telebot import TeleBot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton

BOT_TOKEN = "8407984730:AAGuKV9CD2VC99Jl2oeL5qFnGsMj5mufWvE"

bot = TeleBot(BOT_TOKEN, threaded=True)
user_states = {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.60 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://max.ru",
    "Referer": "https://max.ru/"
}

def main_keyboard():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add(KeyboardButton("📱 Войти по номеру"), KeyboardButton("🔑 Войти по токену"))
    return markup

def cancel_keyboard():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add(KeyboardButton("❌ Отмена"))
    return markup

def check_token(access_token):
    """
    Проверяет токен через HTTP. Загружает главную страницу MAX с токеном,
    и смотрит — редиректит ли на /chats или остаётся на /auth/login.
    """
    session = requests.Session()
    session.headers.update(HEADERS)
    session.cookies.set("__oneme_auth", access_token, domain=".max.ru")
    
    try:
        resp = session.get("https://max.ru", allow_redirects=True, timeout=20)
        
        if "/chats" in resp.url or "/messenger" in resp.url:
            return True, "Вход выполнен успешно. Аккаунт живой."
        elif "/auth" in resp.url or "/login" in resp.url:
            return False, "Токен недействителен. Редирект на страницу входа."
        else:
            return False, f"Неизвестный ответ. URL: {resp.url}"
    except Exception as e:
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
        user_states[chat_id] = {"state": "waiting_phone"}
        bot.reply_to(message, "📱 Введите номер:\n`+7XXXXXXXXXX`", parse_mode="Markdown", reply_markup=cancel_keyboard())
        return

    if text == "🔑 Войти по токену":
        user_states[chat_id] = {"state": "waiting_token"}
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

        msg = bot.reply_to(message, "🔍 Проверяю токен...")

        def run_check():
            valid, info = check_token(access_token)
            if valid:
                bot.edit_message_text(
                    chat_id=chat_id, message_id=msg.message_id,
                    text=f"🟢 **АККАУНТ ЖИВОЙ!**\n\n```\n{info}\n```",
                    parse_mode="Markdown"
                )
                with open("valid_accounts.txt", "a", encoding="utf-8") as f:
                    f.write(f"[TOKEN] {access_token[:50]}... | {info}\n")
            else:
                bot.edit_message_text(
                    chat_id=chat_id, message_id=msg.message_id,
                    text=f"🔴 **АККАУНТ МЁРТВ**\n\n{info}"
                )
            del user_states[chat_id]

        threading.Thread(target=run_check).start()
        return

if __name__ == "__main__":
    print("🤖 MAX Validator запущен...")
    bot.infinity_polling()
