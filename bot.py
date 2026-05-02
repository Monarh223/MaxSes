import os
import asyncio
import telebot
import logging
from webmaxsocket import MaxClient

BOT_TOKEN = "8407984730:AAGVNP8TWRP7AcsrWk5xod0z8qbsW7qt3lE"
bot = telebot.TeleBot(BOT_TOKEN)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

user_states = {}

async def max_login_full(phone, code):
    """
    Полная имитация входа в MAX через TCP-сокеты.
    Возвращает (статус, токен_сессии, информация_о_пользователе).
    """
    client = MaxClient(
        device_type="IOS",       # Имитация iPhone
        device_id=None,          # Автоматическая генерация
        phone=phone,
        code=code
    )
    
    try:
        result = await client.login()
        
        if result.get("success"):
            token = result.get("access_token")
            user_info = result.get("user", {})
            info_str = f"ID: {user_info.get('id', 'N/A')}\nИмя: {user_info.get('first_name', 'N/A')} {user_info.get('last_name', '')}\nТелефон: {user_info.get('phone', phone)}"
            return "VALID", token, info_str
        else:
            error = result.get("error", "Неизвестная ошибка")
            return "INVALID", None, error
            
    except Exception as e:
        return "ERROR", None, str(e)
    finally:
        await client.close()


async def check_session_alive(token):
    """Проверяет, жива ли сессия по токену."""
    client = MaxClient(device_type="IOS")
    try:
        alive = await client.check_session(token)
        return alive
    except:
        return False
    finally:
        await client.close()


@bot.message_handler(commands=['start'])
def start(message):
    user_states[message.chat.id] = {"state": "waiting_phone"}
    bot.reply_to(message,
        "🔐 **MAX Account Validator**\n\n"
        "📱 Введите номер телефона:\n"
        "`+7XXXXXXXXXX`",
        parse_mode="Markdown"
    )


@bot.message_handler(func=lambda m: True)
def handle_message(message):
    chat_id = message.chat.id
    text = message.text.strip()
    state = user_states.get(chat_id, {}).get("state")

    if state == "waiting_phone":
        phone = text.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        if not phone.startswith("+"):
            phone = "+7" + phone.lstrip("87")
        
        bot.reply_to(message, f"📱 Запрашиваю SMS-код для `{phone}`...", parse_mode="Markdown")
        
        # Отправляем запрос кода через TCP (имитация телефона)
        async def request():
            client = MaxClient(device_type="IOS", phone=phone)
            try:
                res = await client.request_code()
                await client.close()
                return res
            except Exception as e:
                await client.close()
                return {"success": False, "error": str(e)}
        
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(request())
        loop.close()
        
        if result.get("success"):
            user_states[chat_id] = {
                "state": "waiting_code",
                "phone": phone
            }
            bot.reply_to(message, "✅ Код отправлен!\n\n📩 Введите 6-значный код из SMS:")
        else:
            bot.reply_to(message, f"❌ Ошибка: {result.get('error', 'Не удалось отправить код')}")


    elif state == "waiting_code":
        code = text.strip()
        if not code.isdigit() or len(code) != 6:
            bot.reply_to(message, "Код должен состоять из 6 цифр.")
            return
        
        phone = user_states[chat_id]["phone"]
        bot.reply_to(message, "🔐 Выполняю вход...")
        
        async def login():
            return await max_login_full(phone, code)
        
        loop = asyncio.new_event_loop()
        status, token, info = loop.run_until_complete(login())
        loop.close()
        
        if status == "VALID":
            bot.reply_to(message,
                f"🟢 **АККАУНТ ЖИВОЙ!**\n\n"
                f"```\n{info}\n```\n\n"
                f"Сессия активна.",
                parse_mode="Markdown"
            )
            # Сохраняем в лог
            with open("valid_accounts.txt", "a", encoding="utf-8") as f:
                f.write(f"{phone} | {info}\n")
        elif status == "INVALID":
            bot.reply_to(message, f"❌ {info}\n\nПопробуйте ещё раз или /start.")
        else:
            bot.reply_to(message, f"💥 Ошибка: {info}")
        
        user_states.pop(chat_id, None)


if __name__ == "__main__":
    print("🤖 MAX Validator запущен (TCP-сокеты, имитация IOS)...")
    bot.infinity_polling()
