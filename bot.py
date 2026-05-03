import os
import json
import logging
import threading
from telebot import TeleBot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton
from playwright.sync_api import sync_playwright

BOT_TOKEN = "8407984730:AAGuKV9CD2VC99Jl2oeL5qFnGsMj5mufWvE"

bot = TeleBot(BOT_TOKEN, threaded=True)
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

def check_token_via_browser(access_token):
    """
    Входит в MAX через браузер (как в инструкции).
    1. Открывает max.ru
    2. Вставляет токен в localStorage
    3. Перезагружает страницу
    4. Проверяет, загрузился ли интерфейс чатов
    """
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            
            # Шаг 1: Заходим на max.ru
            page.goto("https://max.ru", wait_until="domcontentloaded", timeout=30000)
            
            # Шаг 2: Вставляем токен в localStorage
            page.evaluate(f"""
                localStorage.setItem('__oneme_auth', '{access_token}');
            """)
            
            # Шаг 3: Перезагружаем страницу
            page.goto("https://max.ru", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(5000)  # ждём загрузку интерфейса
            
            # Шаг 4: Проверяем, есть ли интерфейс чатов
            # Если токен рабочий — на странице будет список чатов или поле поиска
            chat_list = page.query_selector('[data-testid="chat-list"]')
            search_input = page.query_selector('input[placeholder*="Поиск"]')
            login_form = page.query_selector('input[type="tel"]')  # форма входа с телефоном
            
            if chat_list or search_input:
                # Есть интерфейс чатов — аккаунт живой
                browser.close()
                return True, "Вход выполнен успешно. Аккаунт живой."
            elif login_form:
                # Показана форма входа — токен не сработал
                browser.close()
                return False, "Токен недействителен. Показана форма входа."
            else:
                # Непонятное состояние
                browser.close()
                return False, "Не удалось определить статус. Проверьте токен вручную."
                
    except Exception as e:
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
            valid, info = check_token_via_browser(access_token)
            if valid:
                bot.edit_message_text(
                    chat_id=chat_id, message_id=msg.message_id,
                    text=f"🟢 **АККАУНТ ЖИВОЙ!**\n\n```\n{info}\n```",
                    parse_mode="Markdown"
                )
                with open("valid_accounts.txt", "a") as f:
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
