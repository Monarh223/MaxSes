const { Telegraf } = require('telegraf');
const { WebMaxClient } = require('webmaxsocket');

const BOT_TOKEN = "8407984730:AAGVNP8TWRP7AcsrWk5xod0z8qbsW7qt3lE";
const bot = new Telegraf(BOT_TOKEN);

const userStates = {};

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

// Функция проверки токена через webmaxsocket с полной имитацией устройства
async function checkToken(accessToken) {
    const client = new WebMaxClient({
        name: 'token_check_session',
        token: accessToken,
        deviceType: 'DESKTOP', // <-- Имитируем десктоп, как в твоем браузере
        saveToken: false,
        debug: false,
        // Параметры из твоего скриншота с JSON'ом об устройстве
        ua: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.60 Safari/537.36',
        appVersion: '26.2.3',
        buildNumber: 23185,
        osVersion: 'macOS Sonoma 14.5',
        screen: '1440x900 2.0x',
        timezone: 'Asia/Vladivostok',
        locale: 'ru-RU',
        clientSessionId: 17
    });

    try {
        await client.start();
        // Если client.start() прошел успешно, сессия активна
        const userInfo = `Статус: Вход выполнен успешно`;
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