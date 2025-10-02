import requests
import json
import time
import os
import threading
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
SEARCH_ENGINE_ID = os.environ.get("SEARCH_ENGINE_ID")
#BOT_TOKEN = "8382132782:AAEUK3WKhF7HzNlvOLVhl51O500JEE5u8Lg"
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WATCHLIST_FILE = "watchlist.json"
CHECK_INTERVAL = 10  # minutes

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

# ------------------- NEW: status cache -------------------
try:
    with open("status_cache.json", "r") as f:
        status_cache = json.load(f)
except FileNotFoundError:
    status_cache = {}

def save_status_cache():
    with open("status_cache.json", "w") as f:
        json.dump(status_cache, f)
# ---------------------------------------------------------

# Save watchlists
def save_watchlists():
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(watchlists, f)


# Check Instagram account status
def check_account_status(username):
    try:
        username = username.lower()
        queries = [
            f"site:instagram.com {username}",
            f"https://www.instagram.com/{username}/",
            f"instagram.com/{username}"
        ]

        # --- Step 1: Try Google API first ---
        for query in queries:
            url = "https://www.googleapis.com/customsearch/v1"
            params = {
                "q": query,
                "key": GOOGLE_API_KEY,
                "cx": SEARCH_ENGINE_ID
            }
            r = requests.get(url, params=params, timeout=10)
            data = r.json()

            if "items" in data:
                for item in data["items"]:
                    link = item["link"].lower()
                    if link.startswith("https://www.instagram.com/"):
                        profile = link.split("instagram.com/")[1].split("/")[0]
                        if profile == username:
                            return "ACTIVE (Google)"

        # --- Step 2: Fallback to direct Instagram check ---
        insta_url = f"https://www.instagram.com/{username}/?__a=1&__d=dis"
        headers = {
            "User-Agent": "Mozilla/5.0"
        }
        insta_res = requests.get(insta_url, headers=headers, timeout=10)

        if insta_res.status_code == 200:
            return "ACTIVE (Direct)"
        elif insta_res.status_code == 404:
            return "BANNED / NOT FOUND"
        else:
            return f"ERROR: Instagram returned {insta_res.status_code}"

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

        # Immediately check status
        current_status = check_account_status(username)
        if chat_id not in status_cache:
            status_cache[chat_id] = {}
        status_cache[chat_id][username] = current_status
        save_status_cache()

        await update.message.reply_text(
            f"✅ Added {username} to your watchlist.\nStatus: {current_status}"
        )
    else:
        await update.message.reply_text(
            f"{username} is already in your watchlist."
        )

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
# Background monitoring with safeguard
async def monitor_accounts(context: ContextTypes.DEFAULT_TYPE):
    for chat_id, usernames in watchlists.items():
        if chat_id not in status_cache:
            status_cache[chat_id] = {}
        for username in usernames:
            current_status = check_account_status(username)

            # Get last known status
            last_status = status_cache[chat_id].get(username)

            # If this is the first time, store it and continue
            if last_status is None:
                status_cache[chat_id][username] = {
                    "confirmed": current_status,
                    "pending": current_status
                }
                save_status_cache()
                continue

            # Handle the dict format
            if isinstance(last_status, str):
                # Upgrade old format to new dict format
                last_status = {
                    "confirmed": last_status,
                    "pending": last_status
                }
                status_cache[chat_id][username] = last_status

            # If current status == pending (2nd time), confirm and alert if changed
            if current_status == last_status["pending"]:
                if current_status != last_status["confirmed"]:
                    status_cache[chat_id][username]["confirmed"] = current_status
                    save_status_cache()
                    await context.bot.send_message(
                        chat_id=int(chat_id),
                        text=f"⚠ ALERT: {username} status changed → {current_status}"
                    )
            else:
                # First mismatch, store it as pending
                status_cache[chat_id][username]["pending"] = current_status
                save_status_cache()

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





