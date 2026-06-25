"""
Telegram Bot entry point for the Pixel 10 Pro Google One Gemini Bot.

Commands:
  /start        – Show welcome message and available commands
  /login        – Begin credential capture flow (email → password → 2FA secret)
  /check_offer  – Run Google One automation and look for Gemini Pro offer
  /get_link     – Show the last captured offer link
  /status       – Show current session status and device profile
"""

import logging
import os
import sys

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import config
from device_simulator import create_device_profile
from google_automation import GoogleAutomationError, check_gemini_offer

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=config.LOG_LEVEL, format=config.LOG_FORMAT)
logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────────
AWAIT_EMAIL, AWAIT_PASSWORD, AWAIT_2FA_SECRET = range(3)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_session(chat_id: int) -> dict:
    """Return (creating if absent) the session dict for *chat_id*."""
    if chat_id not in config.SESSION_STORE:
        config.SESSION_STORE[chat_id] = {}
    return config.SESSION_STORE[chat_id]


# ── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send welcome message with command menu."""
    await update.message.reply_text(
        "🤖 *Pixel 10 Pro Google One Bot*\n\n"
        "This bot simulates a Google Pixel 10 Pro (Android 16) device, "
        "logs into your Google account, and retrieves the *12-month free "
        "Gemini Pro* offer link from Google One.\n\n"
        "📋 *Available Commands:*\n"
        "• /login – Enter your Gmail credentials & 2FA secret\n"
        "• /check\\_offer – Detect the Gemini Pro offer\n"
        "• /get\\_link – Show the last captured offer link\n"
        "• /status – View current session & device info\n\n"
        "⚠️ *Privacy Note:* Credentials are held in memory only for the "
        "duration of the session and never stored persistently.",
        parse_mode="Markdown",
    )


# ── /login conversation ───────────────────────────────────────────────────────

async def login_start(update: Update,
                      context: ContextTypes.DEFAULT_TYPE) -> int:
    """Begin the login conversation – ask for email."""
    await update.message.reply_text(
        "📧 Please enter your Gmail address:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return AWAIT_EMAIL


async def login_email(update: Update,
                      context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store the email and ask for password."""
    email = update.message.text.strip()
    context.user_data["pending_email"] = email
    await update.message.reply_text(
        f"✅ Email received: `{email}`\n\n🔒 Now enter your password:",
        parse_mode="Markdown",
    )
    return AWAIT_PASSWORD


async def login_password(update: Update,
                         context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store the password and ask for the 2FA secret."""
    password = update.message.text.strip()
    context.user_data["pending_password"] = password

    # Delete the message containing the password for security
    try:
        await update.message.delete()
    except Exception:
        pass

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            "✅ Password received.\n\n"
            "🔑 Now enter your *2FA Secret Key* (32-character base32 string).\n"
            "Example: `VPN4ONAHFANZKXZPFK2XJYLRPMEXYUCB`\n\n"
            "This is the secret key from your authenticator app setup "
            "(not the 6-digit code)."
        ),
        parse_mode="Markdown",
    )
    return AWAIT_2FA_SECRET


async def login_2fa_secret(update: Update,
                           context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate the TOTP secret, store all credentials, and finish."""
    import pyotp

    chat_id = update.effective_chat.id
    raw_secret = update.message.text.strip().upper().replace(" ", "")

    # Delete the message containing the secret for security
    try:
        await update.message.delete()
    except Exception:
        pass

    # Validate the secret is a valid base32 TOTP key
    try:
        totp = pyotp.TOTP(raw_secret)
        # Generate a test code to confirm the secret is valid
        totp.now()
    except Exception:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "❌ Invalid 2FA secret. Please provide a valid base32 string "
                "(typically 32 characters, A-Z and 2-7).\n\n"
                "🔑 Try again – enter your 2FA secret key:"
            ),
        )
        return AWAIT_2FA_SECRET

    email = context.user_data.pop("pending_email", "")
    password = context.user_data.pop("pending_password", "")

    session = _get_session(chat_id)
    session["email"] = email
    session["password"] = password
    session["totp_secret"] = raw_secret
    session["device"] = create_device_profile()
    session["offer_link"] = None

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "✅ *Credentials & 2FA secret saved.* A new Pixel 10 Pro device "
            "profile has been created for this session.\n\n"
            + session["device"].summary()
            + "\n\nUse /check\\_offer to search for the Gemini Pro offer."
        ),
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def login_cancel(update: Update,
                       context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the login conversation."""
    context.user_data.pop("pending_email", None)
    context.user_data.pop("pending_password", None)
    await update.message.reply_text(
        "❌ Login cancelled.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# ── /check_offer ──────────────────────────────────────────────────────────────

async def check_offer(update: Update,
                      context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run Google One automation and report the result."""
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)

    if not session.get("email") or not session.get("password") or not session.get("totp_secret"):
        await update.message.reply_text(
            "⚠️ No credentials found. Please use /login first."
        )
        return

    device = session.get("device")
    if not device:
        device = create_device_profile()
        session["device"] = device

    await update.message.reply_text(
        "⏳ Launching Pixel 10 Pro device simulator and logging in…\n"
        "This may take up to 60 seconds."
    )

    try:
        offer_link = check_gemini_offer(
            session["email"],
            session["password"],
            session["totp_secret"],
            device,
        )
    except GoogleAutomationError as exc:
        await update.message.reply_text(f"❌ *Error:* {exc}", parse_mode="Markdown")
        return
    except Exception as exc:
        logger.exception("Unexpected error in check_offer for chat %s", chat_id)
        await update.message.reply_text(
            f"❌ An unexpected error occurred: {exc}"
        )
        return

    if offer_link:
        session["offer_link"] = offer_link
        await update.message.reply_text(
            "🎉 *Gemini Pro Offer Found!*\n\n"
            "Click the link below to activate your 12-month free Gemini Pro:\n\n"
            f"🔗 {offer_link}\n\n"
            "_Use /get\\_link to retrieve this link again._",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "😔 No active Gemini Pro offer was detected on your Google One "
            "account at this time.\n\n"
            "The offer may not be available for your account region or may "
            "have already been activated. Try again later."
        )


# ── /get_link ─────────────────────────────────────────────────────────────────

async def get_link(update: Update,
                   context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return the last captured offer link for this session."""
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)
    link = session.get("offer_link")

    if link:
        await update.message.reply_text(
            f"🔗 *Last captured offer link:*\n\n{link}",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "ℹ️ No offer link has been captured yet. "
            "Use /check\\_offer to search for the Gemini Pro offer.",
            parse_mode="Markdown",
        )


# ── /status ───────────────────────────────────────────────────────────────────

async def status(update: Update,
                 context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current session and device profile summary."""
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)

    if not session:
        await update.message.reply_text(
            "ℹ️ No active session. Use /login to get started."
        )
        return

    email = session.get("email", "—")
    has_creds = bool(session.get("email") and session.get("password"))
    has_2fa = bool(session.get("totp_secret"))
    offer_link = session.get("offer_link")
    device = session.get("device")

    lines = [
        "📊 *Session Status*\n",
        f"Account: `{email}`",
        f"Credentials loaded: {'✅' if has_creds else '❌'}",
        f"2FA secret loaded: {'✅' if has_2fa else '❌'}",
        f"Offer link captured: {'✅' if offer_link else '❌'}",
    ]

    if device:
        lines.append("\n" + device.summary())

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
    )


# ── Application setup ─────────────────────────────────────────────────────────

def main() -> None:
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        logger.error(
            "TELEGRAM_BOT_TOKEN environment variable is not set. "
            "Set it in Replit Secrets and restart."
        )
        sys.exit(1)

    app = Application.builder().token(token).build()

    # /login conversation
    login_conv = ConversationHandler(
        entry_points=[CommandHandler("login", login_start)],
        states={
            AWAIT_EMAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_email)
            ],
            AWAIT_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_password)
            ],
            AWAIT_2FA_SECRET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_2fa_secret)
            ],
        },
        fallbacks=[CommandHandler("cancel", login_cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(login_conv)
    app.add_handler(CommandHandler("check_offer", check_offer))
    app.add_handler(CommandHandler("get_link", get_link))
    app.add_handler(CommandHandler("status", status))

    logger.info("Bot is running. Press Ctrl-C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
