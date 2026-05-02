const { Telegraf, Markup } = require('telegraf');
const { WebMaxClient } = require('webmaxsocket');

const BOT_TOKEN = "8407984730:AAGuKV9CD2VC99Jl2oeL5qFnGsMj5mufWvE";
const bot = new Telegraf(BOT_TOKEN);

const mainKeyboard = Markup.keyboard([
  ["📱 Войти по номеру", "🔑 Войти по токену"]
]).resize();

const cancelKeyboard = Markup.keyboard([
  ["❌ Отмена"]
]).resize();

const userStates = {};

async function checkToken(accessToken, deviceParams) {
    const client = new WebMaxClient({
        token: accessToken,
        deviceType: deviceParams.deviceType || 'DESKTOP',
        saveToken: false,
        debug: true,
        ua: deviceParams.headerUserAgent || 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        appVersion: deviceParams.appVersion || '26.2.3',
        buildNumber: deviceParams.buildNumber || 23185,
        osVersion: deviceParams.osVersion || 'macOS 14.5',
        screen: deviceParams.screen || '1440x900',
        timezone: deviceParams.timezone || 'UTC',
        locale: deviceParams.locale || 'ru-RU',
        clientSessionId: deviceParams.clientSessionId || 17,
        deviceId: deviceParams.deviceId || '581a9ea526a673bd'
    });

    try {
        await client.start();
        const me = await client.getMe();
        await client.stop();
        return { valid: true, info: `ID: ${me?.id || 'N/A'}\nИмя: ${me?.first_name || 'N/A'}` };
    } catch (error) {
        await client.stop();
        return { valid: false, info: `Ошибка: ${error.message}` };
    }
}

bot.start((ctx) => {
    userStates[ctx.chat.id] = { state: "menu" };
    return ctx.reply("🔐 **MAX Account Validator**\n\nВыберите способ входа:", { ...mainKeyboard, parse_mode: "Markdown" });
});

bot.on('text', async (ctx) => {
    const chatId = ctx.chat.id;
    const text = ctx.message?.text?.trim();
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
            return ctx.reply("❌ Неверный формат JSON.", cancelKeyboard);
        }

        const msg = await ctx.reply("🔍 Выполняю вход в аккаунт...");
        const result = await checkToken(accessToken, deviceParams);

        if (result.valid) {
            await ctx.telegram.editMessageText(chatId, msg.message_id, undefined,
                `🟢 **АККАУНТ ЖИВОЙ!**\n\n\`\`\`\n${result.info}\n\`\`\``,
                { parse_mode: "Markdown" }
            );
        } else {
            await ctx.telegram.editMessageText(chatId, msg.message_id, undefined,
                `🔴 **АККАУНТ МЁРТВ**\n\n${result.info}`
            );
        }
        delete userStates[chatId];
        return;
    }
});

bot.launch();
console.log("🤖 MAX Validator запущен...");
