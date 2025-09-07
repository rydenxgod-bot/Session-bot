import os
import logging
import asyncio
from pathlib import Path
from datetime import datetime

from aiohttp import web
from telethon import TelegramClient, errors as telethon_errors

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)

# ---------------- CONFIG ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 5000))
DELETE_AFTER_SEND = os.environ.get("DELETE_AFTER_SEND", "false").lower() in ("1", "true", "yes")

if not BOT_TOKEN or not API_ID or not API_HASH or not WEBHOOK_URL:
    raise SystemExit("Missing required env vars: BOT_TOKEN, API_ID, API_HASH, WEBHOOK_URL")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("sessiongen")

# ---------------- STATES ----------------
ASK_PHONE, WAIT_CODE, WAIT_2FA = range(3)


# ---------------- HELPERS ----------------
def make_session_filename(phone: str) -> Path:
    """Return a Path for the session file, based on the phone number."""
    sanitized = phone.replace(" ", "").replace("-", "")
    return Path(f"{sanitized}.session")


async def safe_send_file(bot, chat_id: int, path: Path, caption=""):
    """Send file and optionally delete afterwards."""
    await bot.send_document(chat_id=chat_id, document=path.open("rb"), caption=caption)
    if DELETE_AFTER_SEND and path.exists():
        try:
            path.unlink()
            logger.info("Deleted local session file %s", path)
        except Exception as e:
            logger.warning("Could not delete file %s: %s", path, e)


# ---------------- HANDLERS ----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome!\n\nUse /gensession to generate your Telegram `.session` file "
        "for use with Telethon / Pyrogram.\n\n⚠️ Only use this for accounts you own."
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❎ Cancelled.")
    return ConversationHandler.END


async def gensession_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📱 Send your phone number in international format (e.g. +919876543210).")
    return ASK_PHONE


async def receive_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    if not phone.startswith("+") or len(phone) < 7:
        await update.message.reply_text("⚠️ Invalid phone. Send again starting with +countrycode.")
        return ASK_PHONE

    session_path = make_session_filename(phone)
    client = TelegramClient(session_path, int(API_ID), API_HASH)

    context.user_data["phone"] = phone
    context.user_data["client"] = client
    context.user_data["session_path"] = session_path

    await update.message.reply_text("🔄 Sending login code to Telegram...")
    try:
        await client.connect()
        await client.send_code_request(phone)
        await update.message.reply_text("✉️ Code sent! Please enter the code you received.")
        return WAIT_CODE
    except telethon_errors.PhoneNumberInvalidError:
        await update.message.reply_text("❌ Invalid phone number. Try again with /gensession.")
        await client.disconnect()
        return ConversationHandler.END
    except Exception as e:
        logger.exception("Error requesting code: %s", e)
        await update.message.reply_text("❌ Error sending code. Try again later.")
        await client.disconnect()
        return ConversationHandler.END


async def receive_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    client: TelegramClient = context.user_data["client"]
    phone = context.user_data["phone"]
    session_path: Path = context.user_data["session_path"]

    try:
        await client.sign_in(phone=phone, code=code)
        await update.message.reply_text("✅ Signed in successfully! Preparing session file...")
        await client.disconnect()
        await safe_send_file(context.bot, update.effective_chat.id, session_path, caption="Here is your session file.")
        return ConversationHandler.END
    except telethon_errors.SessionPasswordNeededError:
        await update.message.reply_text("🔒 This account has 2FA enabled. Please send your password now.")
        return WAIT_2FA
    except telethon_errors.PhoneCodeInvalidError:
        await update.message.reply_text("❌ Invalid code. Start again with /gensession.")
    except telethon_errors.PhoneCodeExpiredError:
        await update.message.reply_text("❌ Code expired. Use /gensession to retry.")
    except Exception as e:
        logger.exception("Error signing in: %s", e)
        await update.message.reply_text("❌ Unexpected error during sign-in.")
    await client.disconnect()
    return ConversationHandler.END


async def receive_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    client: TelegramClient = context.user_data["client"]
    session_path: Path = context.user_data["session_path"]

    try:
        await client.sign_in(password=password)
        await update.message.reply_text("✅ 2FA accepted! Preparing session file...")
        await client.disconnect()
        await safe_send_file(context.bot, update.effective_chat.id, session_path, caption="Here is your session file.")
    except Exception as e:
        logger.exception("2FA error: %s", e)
        await update.message.reply_text("❌ Error with 2FA sign-in. Try again.")
        try:
            await client.disconnect()
        except:
            pass
    return ConversationHandler.END


# ---------------- WEBHOOK SERVER ----------------
def setup_application():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("gensession", gensession_start)],
        states={
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone)],
            WAIT_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_code)],
            WAIT_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_2fa)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        conversation_timeout=300,
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(conv)
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    return app


async def webhook_handler(request: web.Request):
    app = request.app["telegram_app"]
    data = await request.json()
    update = Update.de_json(data, app.bot)
    await app.process_update(update)
    return web.Response(status=200)


async def health_check(request: web.Request):
    return web.Response(text="OK", status=200)


async def set_webhook():
    from telegram import Bot
    bot = Bot(token=BOT_TOKEN)
    await bot.set_webhook(WEBHOOK_URL)
    logger.info("Webhook set to %s", WEBHOOK_URL)


async def main():
    telegram_app = setup_application()
    web_app = web.Application()
    web_app["telegram_app"] = telegram_app
    web_app.router.add_post("/", webhook_handler)
    web_app.router.add_get("/health", health_check)

    await set_webhook()

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    await telegram_app.initialize()
    logger.info("Bot is running on port %s", PORT)

    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down...")