import requests
import threading
from telebot import TeleBot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton

BOT_TOKEN = "8407984730:AAGuKV9CD2VC99Jl2oeL5qFnGsMj5mufWvE"

bot = TeleBot(BOT_TOKEN, threaded=True)
user_states = {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.60 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
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

def check_token_http(access_token):
    """
    Имитирует вход через браузер:
    1. Загружает max.ru без токена
    2. Устанавливает токен в cookies + localStorage через JS-эмуляцию
    3. Загружает max.ru снова с токеном
    4. Проверяет, происходит ли редирект на страницу чатов
    """
    session = requests.Session()
    session.headers.update(HEADERS)
    
    try:
        # Первый заход — без токена, получаем куки сессии
        resp1 = session.get("https://max.ru", timeout=20)
        
        # Устанавливаем токен в куки
        session.cookies.set("__oneme_auth", access_token, domain=".max.ru", path="/")
        
        # Добавляем токен в заголовок Authorization
        session.headers["Authorization"] = f"Bearer {access_token}"
        
        # Второй заход — с токеном
        resp2 = session.get("https://max.ru", allow_redirects=True, timeout=20)
        
        # Проверяем результат
        final_url = resp2.url.lower()
        page_text = resp2.text.lower()
        
        # Признаки успешного входа
        if any(x in final_url for x in ["/chats", "/messenger", "/im"]):
            return True, "Вход выполнен успешно. Аккаунт живой."
        
        # Признаки успешного входа по содержимому страницы
        if any(x in page_text for x in ["список чатов", "чаты", "сообщения", "chat-list", "messenger"]):
            return True, "Вход выполнен успешно. Аккаунт живой."
        
        # Признаки неудачи
        if any(x in final_url for x in ["/auth", "/login"]):
            return False, "Токен недействителен. Редирект на страницу входа."
        
        # Проверка: есть ли на странице форма входа с телефоном
        if 'type="tel"' in page_text or 'номер телефона' in page_text:
            return False, "Токен недействителен. Показана форма входа."
        
        # Если ничего не подошло
        return False, f"Неизвестный ответ. URL: {resp2.url[:100]}"
        
    except Exception as e:
        return False, f"Ошибка соединения: {str(e)}"

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
            valid, info = check_token_http(access_token)
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
