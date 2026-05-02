const { Telegraf } = require('telegraf');
const { WebMaxClient } = require('webmaxsocket');

const BOT_TOKEN = "8407984730:AAGVNP8TWRP7AcsrWk5xod0z8qbsW7qt3lE";
const bot = new Telegraf(BOT_TOKEN);

// Хранилище состояний пользователей
const userStates = {};

// Клавиатуры
const mainKeyboard = {
  reply_markup: {
    keyboard: [["📱 Войти по номеру", "🔑 Войти по токену"]],
    resize_keyboard: true
  }
};

const cancelKeyboard = {
  reply_markup: {
    keyboard: [["❌ Отмена"]],
    resize_keyboard: true
  }
};

// Функция проверки токена через webmaxsocket
async function checkToken(accessToken) {
    const client = new WebMaxClient({
        name: 'token_check_session',
        token: accessToken,
        deviceType: 'WEB', // Подключаемся как веб-версия
        saveToken: false,
        debug: false
    });

    try {
        await client.start();
        // Если client.start() прошел успешно, сессия активна
        const userInfo = `ID: N/A\nИмя: N/A\nТелефон: N/A`; // Библиотека может не возвращать эти данные сразу
        await client.stop();
        return { valid: true, info: userInfo };
    } catch (error) {
        await client.stop();
        return { valid: false, info: `Ошибка: ${error.message}` };
    }
}

bot.start((ctx) => {
    userStates[ctx.chat.id] = { state: "menu" };
    return ctx.reply("🔐 **MAX Account Validator**\n\nВыберите способ входа:", mainKeyboard);
});

bot.on('text', async (ctx) => {
    const chatId = ctx.chat.id;
    const text = ctx.message.text;
    const state = userStates[chatId]?.state;

    if (text === "❌ Отмена") {
        delete userStates[chatId];
        return ctx.reply("Отменено.", mainKeyboard);
    }

    if (text === "📱 Войти по номеру") {
        userStates[chatId] = { state: "waiting_phone", mode: "phone" };
        return ctx.reply("📱 Введите номер:\n`+7XXXXXXXXXX`", { parse_mode: "Markdown", ...cancelKeyboard });
    }

    if (text === "🔑 Войти по токену") {
        userStates[chatId] = { state: "waiting_token", mode: "token" };
        return ctx.reply("🔑 Вставьте токен:", cancelKeyboard);
    }

    if (state === "waiting_phone") {
        // Заглушка для входа по номеру
        delete userStates[chatId];
        return ctx.reply("⏳ Функция входа по номеру временно недоступна. Пожалуйста, воспользуйтесь входом по токену.", mainKeyboard);
    }

    if (state === "waiting_token") {
        const accessToken = text.replace(/\s/g, "");
        if (accessToken.length < 50) {
            return ctx.reply("❌ Токен слишком короткий.", cancelKeyboard);
        }
        
        await ctx.reply("🔍 Выполняю вход в аккаунт...");
        const result = await checkToken(accessToken);
        
        if (result.valid) {
            await ctx.reply(`🟢 **АККАУНТ ЖИВОЙ! Вход выполнен!**\n\n\`\`\`\n${result.info}\n\`\`\``, { parse_mode: "Markdown", ...mainKeyboard });
            // Здесь можно добавить запись в файл valid_accounts.txt
        } else {
            await ctx.reply(`🔴 **АККАУНТ МЁРТВ**\n\n${result.info}`, mainKeyboard);
        }
        delete userStates[chatId];
    }
});

bot.launch();
console.log("🤖 MAX Validator запущен...");
