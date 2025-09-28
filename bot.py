import requests
import json
import time
import os
import threading
import instaloader
from instaloader import Profile, InstaloaderException
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

#BOT_TOKEN = "8382132782:AAEUK3WKhF7HzNlvOLVhl51O500JEE5u8Lg"
BOT_TOKEN = os.environ.get("BOT_TOKEN")
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
    """
    Returns:
      - "ACTIVE" -> profile exists (public)
      - "BANNED / NOT FOUND" -> 404 or profile doesn't exist
      - "PRIVATE" -> profile is private (instaloader indicates private; still 'active')
      - "RATE LIMITED" -> Instagram blocked / rate-limited us
      - "ERROR: <msg>" -> other errors
    """

    # 1) Try Instaloader (no login). This will work for many public profiles.
    try:
        L = instaloader.Instaloader(dirname_pattern=None, download_pictures=False,
                                    download_videos=False, download_comments=False,
                                    save_metadata=False, compress_json=False)
        # do not call L.login(), we want no-login operation
        try:
            profile = Profile.from_username(L.context, username)
            # If we get a Profile object, the user exists.
            # Check if profile is private or public:
            if profile.is_private:
                return "PRIVATE"
            else:
                return "ACTIVE"
        except instaloader.exceptions.ProfileNotExistsException:
            return "BANNED / NOT FOUND"
        except InstaloaderException as ie:
            # Instaloader-specific problems (rate-limited, blocked, etc.)
            # We'll fall through to the fallback HTML check below.
            debug_msg = str(ie)
            # If it's an obvious rate-limit or login prompt, return RATE LIMITED
            if "429" in debug_msg or "rate" in debug_msg.lower() or "login" in debug_msg.lower():
                return "RATE LIMITED"
            # otherwise continue to fallback
        except Exception as e:
            # unexpected error from instaloader; fall back
            pass

    except Exception:
        # If instaloader import/init or context creation fails, fall back
        pass

    # 2) Fallback: plain HTTP HTML check (same logic you had, useful when instaloader fails)
    try:
        profile_url = f"https://www.instagram.com/{username}/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        }

        r = requests.get(profile_url, headers=headers, timeout=15)
        page_text = (r.text or "").lower()

        if r.status_code == 404 or "page not found" in page_text:
            return "BANNED / NOT FOUND"

        if r.status_code == 429:
            return "RATE LIMITED"

        unavailable_phrases = [
            "sorry, this page isn't available",
            "the link you followed may be broken",
            "page may have been removed",
            "page isn&#39;t available",
            "this account is private"   # sometimes helpful
        ]
        if any(phrase in page_text for phrase in unavailable_phrases):
            # note: "this account is private" means active but private — you may want PRIVATE instead
            if "this account is private" in page_text:
                return "PRIVATE"
            return "BANNED / SUSPENDED"

        # If username appears in the HTML, we typically have an active profile (public or partial)
        if username.lower() in page_text:
            return "ACTIVE"

        # If none of these match, return unknown but include a short snippet for debug
        snippet = page_text[:300].replace("\n", " ")
        return f"UNKNOWN RESPONSE: {snippet}"

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
