# monitor_bot.py
import os
import json
import time
import threading
import asyncio
import requests
import instaloader
from instaloader import Profile, InstaloaderException
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# === Config ===
BOT_TOKEN = os.environ.get("BOT_TOKEN")
INST_USERNAME = os.environ.get("INST_USERNAME")  # Instagram username for session/login (optional)
INST_PASSWORD = os.environ.get("INST_PASSWORD")  # Instagram password for emergency login (optional)
SESSION_FILE = f"session-{INST_USERNAME}.session" if INST_USERNAME else None
WATCHLIST_FILE = "watchlist.json"
LAST_STATUS_FILE = "last_status.json"

CHECK_INTERVAL_MINUTES = 15
SLEEP_BETWEEN_REQUESTS = 8  # seconds between each profile request (tune this)
FLASK_PORT = int(os.environ.get("PORT", 8080))

flask_app = Flask("")

@flask_app.route("/")
def home():
    return "Bot is running!"

def run_flask():
    flask_app.run(host="0.0.0.0", port=FLASK_PORT)

# === Load watchlists and last_status ===
try:
    with open(WATCHLIST_FILE, "r") as f:
        watchlists = json.load(f)
except FileNotFoundError:
    watchlists = {}

try:
    with open(LAST_STATUS_FILE, "r") as f:
        last_status = json.load(f)
except FileNotFoundError:
    last_status = {}  # { "username": "ACTIVE" }
    
def save_watchlists():
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(watchlists, f, indent=2)

def save_last_status():
    # atomic-ish write
    tmp = LAST_STATUS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(last_status, f, indent=2)
    os.replace(tmp, LAST_STATUS_FILE)

# === Instaloader setup & session management ===
L = instaloader.Instaloader(dirname_pattern=None,
                            download_pictures=False,
                            download_videos=False,
                            download_comments=False,
                            save_metadata=False,
                            compress_json=False)

def ensure_instaloader_session():
    """
    Try to load session from SESSION_FILE. If not present and credentials are available,
    perform login and save session to file. Returns True if session/login successful.
    """
    if not INST_USERNAME:
        # No configured username -> we'll run no-login mode (still ok for many public profiles)
        return False
    # Try to load existing session file
    try:
        L.load_session_from_file(INST_USERNAME, filename=SESSION_FILE)
        print(f"[Instaloader] Loaded session from {SESSION_FILE}")
        return True
    except Exception:
        pass

    # Try to login with credentials and save session
    if INST_USERNAME and INST_PASSWORD:
        try:
            L.login(INST_USERNAME, INST_PASSWORD)
            # Save session to file for later runs
            try:
                L.save_session_to_file(filename=SESSION_FILE)
            except Exception:
                # fallback: save with default naming
                try:
                    L.save_session_to_file()
                except Exception:
                    pass
            print("[Instaloader] Logged in and saved session.")
            return True
        except Exception as e:
            print("[Instaloader] Login failed:", e)
            return False

    return False

# Attempt to establish session at startup (best-effort)
HAS_SESSION = ensure_instaloader_session()

# === Account status check (instaloader first if available, fallback to HTTP) ===
def check_account_status_instaloader(username):
    try:
        profile = Profile.from_username(L.context, username)
        if profile.is_private:
            return "PRIVATE"
        else:
            return "ACTIVE"
    except instaloader.exceptions.ProfileNotExistsException:
        return "BANNED / NOT FOUND"
    except InstaloaderException as ie:
        debug_msg = str(ie).lower()
        if "429" in debug_msg or "rate" in debug_msg or "login" in debug_msg:
            return "RATE LIMITED"
        # otherwise fallback to HTTP
    except Exception:
        pass
    return None  # signal to fallback

def check_account_status_http(username):
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
            "this account is private"
        ]
        if any(phrase in page_text for phrase in unavailable_phrases):
            if "this account is private" in page_text:
                return "PRIVATE"
            return "BANNED / SUSPENDED"

        if username.lower() in page_text:
            return "ACTIVE"

        snippet = page_text[:300].replace("\n", " ")
        return f"UNKNOWN RESPONSE: {snippet}"
    except Exception as e:
        return f"ERROR: {e}"

def check_account_status(username):
    """
    Returns status string: ACTIVE, PRIVATE, BANNED / NOT FOUND, RATE LIMITED, or other.
    """
    # Prefer instaloader if we have a (possibly logged-in) instaloader context
    if HAS_SESSION:
        result = check_account_status_instaloader(username)
        if result:
            return result
        # if instaloader returned None, fall through to HTTP fallback

    # Try HTTP fallback
    return check_account_status_http(username)

# === Telegram command handlers ===
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
        await update.message.reply_text(f"✅ Added {username} to your watchlist.")
    else:
        await update.message.reply_text(f"{username} is already in your watchlist.")

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
        await update.message.reply_text(f"❌ Removed {username} from your watchlist.")
    else:
        await update.message.reply_text(f"{username} not found in your watchlist.")

async def list_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if chat_id not in watchlists or not watchlists[chat_id]:
        await update.message.reply_text("📭 Your watchlist is empty.")
    else:
        await update.message.reply_text("📌 Your Watchlist:\n" + "\n".join(watchlists[chat_id]))

async def check_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /check username")
        return
    username = context.args[0].lower()
    status = check_account_status(username)
    await update.message.reply_text(f"🔎 {username} → {status}")

# Register chat (to keep track)
async def register_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if "chat_ids" not in context.application.bot_data:
        context.application.bot_data["chat_ids"] = []
    if chat_id not in context.application.bot_data["chat_ids"]:
        context.application.bot_data["chat_ids"].append(chat_id)

# === Monitoring loop ===
async def monitor_accounts(context: ContextTypes.DEFAULT_TYPE):
    """
    Called by job queue every CHECK_INTERVAL_MINUTES.
    We dedupe usernames across chats to reduce load, check them once each cycle,
    then send alerts to the chats that are watching them if status changed.
    """
    # Build a map username -> list of chat_ids watching it
    user_to_chats = {}
    for chat_id, usernames in watchlists.items():
        for u in usernames:
            user_to_chats.setdefault(u, []).append(chat_id)

    usernames = list(user_to_chats.keys())
    if not usernames:
        return

    for idx, username in enumerate(usernames):
        # Check username
        status = check_account_status(username)
        # Compare with last known status
        prev = last_status.get(username)
        if prev != status:
            # Update stored status
            last_status[username] = status
            save_last_status()
            # Notify all chats watching this user only if state is not ACTIVE or if changed
            # (you can change logic to always notify on change, even ACTIVE->ACTIVE)
            for chat_id in user_to_chats.get(username, []):
                try:
                    # send message (chat_id is stored as string earlier in watchlists)
                    await context.bot.send_message(
                        chat_id=int(chat_id),
                        text=f"⚠ ALERT: {username} → {status}"
                    )
                except Exception as e:
                    print(f"Failed to send message to {chat_id}: {e}")
        # Wait before the next request to avoid rate limits
        # Add a slightly longer wait every N requests if you want
        await asyncio.sleep(SLEEP_BETWEEN_REQUESTS)

# === Main ===
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env variable is required")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("add", add_account))
    app.add_handler(CommandHandler("remove", remove_account))
    app.add_handler(CommandHandler("list", list_accounts))
    app.add_handler(CommandHandler("check", check_account))
    app.add_handler(CommandHandler("start", register_chat))

    # Run monitor as a repeating job
    app.job_queue.run_repeating(monitor_accounts,
                                interval=CHECK_INTERVAL_MINUTES * 60,
                                first=10)

    # Start Flask server in another thread (for uptime / healthcheck)
    threading.Thread(target=run_flask, daemon=True).start()

    print("Bot starting... (press CTRL+C to stop)")
    app.run_polling()

if __name__ == "__main__":
    main()
