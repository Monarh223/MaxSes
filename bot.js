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

// Функция проверки токена с ПОЛНОЙ кастомизацией параметров устройства
async function checkToken(accessToken, deviceParams) {
    const client = new WebMaxClient({
        name: 'token_check_session',
        token: accessToken,
        deviceType: deviceParams.deviceType || 'DESKTOP',
        saveToken: false,
        debug: false,
        ua: deviceParams.ua || deviceParams.headerUserAgent || 'Mozilla/5.0',
        appVersion: deviceParams.appVersion || '26.2.3',
        buildNumber: deviceParams.buildNumber || 23185,
        osVersion: deviceParams.osVersion || 'macOS 14.5',
        screen: deviceParams.screen || '1440x900',
        timezone: deviceParams.timezone || 'UTC',
        locale: deviceParams.locale || 'ru-RU',
        clientSessionId: deviceParams.clientSessionId || 17
    });

    try {
        await client.start();
        await client.stop();
        return { valid: true, info: 'Статус: Вход выполнен успешно' };
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
        return ctx.reply("⏳ Функция входа по номеру временно недоступна.", mainKeyboard);
    }

    if (state === "waiting_token") {
        const accessToken = text.replace(/\s/g, "");
        if (accessToken.length < 50) {
            return ctx.reply("❌ Токен слишком короткий.", cancelKeyboard);
        }
        
        // Сохраняем токен и запрашиваем JSON
        userStates[chatId] = { state: "waiting_json", token: accessToken };
        return ctx.reply(
            "📲 Теперь отправьте JSON с параметрами устройства одной строкой.\n\n" +
            "Должны быть поля: *deviceType*, *clientSessionId*, *appVersion*, *headerUserAgent*, *osVersion*, *screen*, *timezone*, *locale*.",
            { parse_mode: "Markdown", ...cancelKeyboard }
        );
    }

    if (state === "waiting_json") {
        const accessToken = userStates[chatId].token;
        let deviceParams;
        
        try {
            deviceParams = JSON.parse(text);
        } catch (e) {
            return ctx.reply("❌ Неверный формат JSON. Попробуйте еще раз.", cancelKeyboard);
        }
        
        if (!deviceParams.deviceType || !deviceParams.clientSessionId) {
            return ctx.reply("❌ В JSON обязательно должны быть поля *deviceType* и *clientSessionId*.", { parse_mode: "Markdown", ...cancelKeyboard });
        }

        await ctx.reply("🔍 Выполняю вход в аккаунт с вашими параметрами...");
        const result = await checkToken(accessToken, deviceParams);
        
        if (result.valid) {
            await ctx.reply(`🟢 **АККАУНТ ЖИВОЙ! Вход выполнен!**\n\n\`\`\`\n${result.info}\n\`\`\``, { parse_mode: "Markdown", ...mainKeyboard });
        } else {
            await ctx.reply(`🔴 **АККАУНТ МЁРТВ**\n\n${result.info}`, mainKeyboard);
        }
        delete userStates[chatId];
        return;
    }
});

bot.launch();
console.log("🤖 MAX Validator запущен...");