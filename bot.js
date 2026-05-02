const { Telegraf } = require('telegraf');
const { WebMaxClient } = require('webmaxsocket');

const BOT_TOKEN = "8407984730:AAGuKV9CD2VC99Jl2oeL5qFnGsMj5mufWvE";

// Прокси для обхода блокировок.
// Если переменная окружения PROXY_URL не задана, бот попробует работать без прокси.
function getProxyUrl() {
  return process.env.PROXY_URL || null; // Ожидается формат socks5://login:pass@ip:port
}

const bot = new Telegraf(BOT_TOKEN);

const mainKeyboard = {
  reply_markup: {
    keyboard: [["📱 Войти по номеру"], ["🔑 Войти по токену"]],
    resize_keyboard: true
  }
};

const cancelKeyboard = {
  reply_markup: {
    keyboard: [["❌ Отмена"]],
    resize_keyboard: true
  }
};

const userStates = {};

// Функция для проверки токена с поддержкой прокси
async function checkToken(accessToken, deviceParams) {
    const clientOptions = {
        token: accessToken,
        deviceType: deviceParams.deviceType || 'DESKTOP',
        saveToken: false,
        debug: false,
        ua: deviceParams.headerUserAgent || 'Mozilla/5.0',
        appVersion: deviceParams.appVersion || '26.2.3',
        buildNumber: deviceParams.buildNumber || 23185,
        osVersion: deviceParams.osVersion || 'macOS 14.5',
        screen: deviceParams.screen || '1440x900',
        timezone: deviceParams.timezone || 'UTC',
        locale: deviceParams.locale || 'ru-RU',
        clientSessionId: deviceParams.clientSessionId || 17
    };
    
    const proxy = getProxyUrl();
    if (proxy) {
        clientOptions.proxy = proxy;
        console.log(`Использую прокси: ${proxy}`);
    }

    const client = new WebMaxClient(clientOptions);

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
        userStates[chatId] = { state: "waiting_phone" };
        return ctx.reply("📱 Введите номер:\n`+7XXXXXXXXXX`", { parse_mode: "Markdown", ...cancelKeyboard });
    }

    if (text === "🔑 Войти по токену") {
        userStates[chatId] = { state: "waiting_token" };
        return ctx.reply("🔑 Вставьте токен:", cancelKeyboard);
    }

    if (state === "waiting_phone") {
        delete userStates[chatId];
        return ctx.reply("⏳ Функция входа по номеру временно недоступна.", mainKeyboard);
    }

    if (state === "waiting_token") {
        const accessToken = text.replace(/\s/g, "");
        if (accessToken.length < 50) {
            return ctx.reply("❌ Токен слишком короткий.", cancelKeyboard);
        }
        userStates[chatId] = { state: "waiting_json", token: accessToken };
        return ctx.reply("📲 Теперь отправьте JSON с параметрами устройства одной строкой.", cancelKeyboard);
    }

    if (state === "waiting_json") {
        const accessToken = userStates[chatId].token;
        let deviceParams;
        try {
            deviceParams = JSON.parse(text);
        } catch (e) {
            return ctx.reply("❌ Неверный формат JSON. Попробуйте еще раз.", cancelKeyboard);
        }
        
        const msg = await ctx.reply("🔍 Выполняю вход в аккаунт...");
        const result = await checkToken(accessToken, deviceParams);
        
        if (result.valid) {
            await ctx.telegram.editMessageText(chatId, msg.message_id, undefined,
                `🟢 **АККАУНТ ЖИВОЙ!**\n\n\`\`\`\n${result.info}\n\`\`\``,
                { parse_mode: "Markdown", ...mainKeyboard }
            );
        } else {
            await ctx.telegram.editMessageText(chatId, msg.message_id, undefined,
                `🔴 **АККАУНТ МЁРТВ**\n\n${result.info}`,
                mainKeyboard
            );
        }
        delete userStates[chatId];
        return;
    }
});

bot.launch();
console.log("🤖 MAX Validator запущен...");
