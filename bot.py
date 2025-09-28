import requests
import json
import time
import os
import threading
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

#BOT_TOKEN = "8382132782:AAEUK3WKhF7HzNlvOLVhl51O500JEE5u8Lg"
BOT_TOKEN = os.environ.get("BOT_TOKEN")
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY")
WATCHLIST_FILE = "watchlist.json"
CHECK_INTERVAL = 20  # minutes

flask_app = Flask("")


@flask_app.route("/")
def home():
    return "Bot is running!"


def run_flask():
    flask_app.run(host="0.0.0.0", port=8080)


# Load or initialize watchlists (per user)
try:
    with open(WATCHLIST_FILE, "r") as f:
        watchlists = json.load(f)
except FileNotFoundError:
    watchlists = {}


# Save watchlists
def save_watchlists():
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(watchlists, f)


# Check Instagram account status
def check_account_status(username):
    profile_url = f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}"

    scrape_url = "http://api.scraperapi.com"
    params = {
        "api_key": SCRAPER_API_KEY,
        "url": profile_url
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json",
    }

    try:
        r = requests.get(scrape_url, headers=headers, params=params, timeout=20)

        # Instagram returns 404 for suspended/banned
        if r.status_code == 404:
            return "BANNED / NOT FOUND"

        data = r.json()

        # If profile exists → ACTIVE
        if "data" in data and data["data"].get("user"):
            return "ACTIVE"

        return "BANNED / SUSPENDED"

    except Exception as e:
        return f"ERROR: {e}"


# Telegram commands
async def add_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if chat_id not in watchlists:
        watchlists[chat_id] = []
    if not context.args:
        await update.message.reply_text("Usage: /add username")
        return
    username = context.args[0].lower()
    if username not in watchlists[chat_id]:
        watchlists[chat_id].append(username)
        save_watchlists()
        await update.message.reply_text(
            f"✅ Added {username} to your watchlist.")
    else:
        await update.message.reply_text(
            f"{username} is already in your watchlist.")


async def remove_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if chat_id not in watchlists:
        watchlists[chat_id] = []
    if not context.args:
        await update.message.reply_text("Usage: /remove username")
        return
    username = context.args[0].lower()
    if username in watchlists[chat_id]:
        watchlists[chat_id].remove(username)
        save_watchlists()
        await update.message.reply_text(
            f"❌ Removed {username} from your watchlist.")
    else:
        await update.message.reply_text(
            f"{username} not found in your watchlist.")


async def list_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if chat_id not in watchlists or not watchlists[chat_id]:
        await update.message.reply_text("📭 Your watchlist is empty.")
    else:
        await update.message.reply_text("📌 Your Watchlist:\n" +
                                        "\n".join(watchlists[chat_id]))


async def check_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /check username")
        return
    username = context.args[0].lower()
    status = check_account_status(username)
    await update.message.reply_text(f"🔎 {username} → {status}")


# Background monitoring
async def monitor_accounts(context: ContextTypes.DEFAULT_TYPE):
    for chat_id, usernames in watchlists.items():
        for username in usernames:
            status = check_account_status(username)
            if status != "ACTIVE":
                await context.bot.send_message(
                    chat_id=int(chat_id),
                    text=f"⚠ ALERT: {username} is {status}")


# Store chat IDs whenever someone interacts with the bot
async def register_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if "chat_ids" not in context.application.bot_data:
        context.application.bot_data["chat_ids"] = []
    if chat_id not in context.application.bot_data["chat_ids"]:
        context.application.bot_data["chat_ids"].append(chat_id)


# Main
app = ApplicationBuilder().token(BOT_TOKEN).build()

# Handlers
app.add_handler(CommandHandler("add", add_account))
app.add_handler(CommandHandler("remove", remove_account))
app.add_handler(CommandHandler("list", list_accounts))
app.add_handler(CommandHandler("check", check_account))
app.add_handler(CommandHandler("start",
                               register_chat))  # registers chat automatically

app.job_queue.run_repeating(monitor_accounts,
                            interval=CHECK_INTERVAL * 60,
                            first=10)

if __name__ == "__main__":
    # Start Flask server in another thread
    threading.Thread(target=run_flask).start()

    # Start the Telegram bot

    app.run_polling()



