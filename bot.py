import os
import json
import time
import threading
from telebot import TeleBot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

BOT_TOKEN = "8407984730:AAGuKV9CD2VC99Jl2oeL5qFnGsMj5mufWvE"

bot = TeleBot(BOT_TOKEN, threaded=True)
user_states = {}

def main_keyboard():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add(KeyboardButton("📱 Войти по номеру"), KeyboardButton("🔑 Войти по токену"))
    return markup

def cancel_keyboard():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add(KeyboardButton("❌ Отмена"))
    return markup

def check_token_via_selenium(access_token):
    """
    Точная копия инструкции:
    1. Открываем max.ru
    2. Выполняем JS-код для вставки токена
    3. Перезагружаем страницу
    4. Проверяем результат
    """
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.60 Safari/537.36")
    
    driver = None
    try:
        driver = webdriver.Chrome(options=chrome_options)
        
        # Шаг 1: Заходим на max.ru
        driver.get("https://max.ru")
        time.sleep(3)
        
        # Шаг 2: Вставляем токен через JS (как в инструкции)
        js_code = f"""
            localStorage.setItem('__oneme_auth', '{access_token}');
            sessionStorage.setItem('__oneme_auth', '{access_token}');
            document.cookie = '__oneme_auth={access_token}; path=/; domain=.max.ru';
        """
        driver.execute_script(js_code)
        time.sleep(1)
        
        # Шаг 3: Перезагружаем страницу
        driver.get("https://max.ru")
        time.sleep(5)
        
        # Шаг 4: Проверяем, загрузился ли интерфейс чатов
        page_source = driver.page_source.lower()
        current_url = driver.current_url.lower()
        
        if "/chats" in current_url or "/messenger" in current_url:
            driver.quit()
            return True, "Вход выполнен успешно. Аккаунт живой."
        elif "чаты" in page_source or "сообщения" in page_source:
            driver.quit()
            return True, "Вход выполнен успешно. Аккаунт живой."
        elif "/auth" in current_url or "/login" in current_url:
            driver.quit()
            return False, "Токен недействителен. Редирект на страницу входа."
        else:
            driver.quit()
            return False, f"Неизвестный ответ. URL: {current_url[:100]}"
            
    except Exception as e:
        if driver:
            driver.quit()
        return False, f"Ошибка браузера: {str(e)}"

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

        msg = bot.reply_to(message, "🔍 Выполняю вход через браузер...")

        def run_login():
            valid, info = check_token_via_selenium(access_token)
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

        threading.Thread(target=run_login).start()
        return

if __name__ == "__main__":
    print("🤖 MAX Validator запущен...")
    bot.infinity_polling()
