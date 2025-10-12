import os
import json
import time
import threading
import requests

from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
import io

# ——— Config & IDs ———
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
SEARCH_ENGINE_ID = os.environ.get("SEARCH_ENGINE_ID")

# These are your shared Drive file IDs
WATCHLIST_FILE_ID = "1CH5m1oIMb_KgmugIGHXKDrWnVKJ3UL8x"
STATUS_CACHE_FILE_ID = "139foNUDAX2-OqJzGbCMfioqSX-rLPiVp"

CHECK_INTERVAL = 10  # minutes

flask_app = Flask("")

@flask_app.route("/")
def home():
    return "Bot is running!"

def run_flask():
    flask_app.run(host="0.0.0.0", port=8080)


# ——— Google Drive helper setup ———

def get_drive_service():
    # Path to service account credentials (uploaded to Render)
    creds_path = "/opt/render/project/src/service_account.json"
    credentials = service_account.Credentials.from_service_account_file(
        creds_path,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=credentials)

drive_service = get_drive_service()

def download_json_from_drive(file_id):
    """Download JSON file from Drive, return as Python object (dict)."""
    request = drive_service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while done is False:
        status, done = downloader.next_chunk()
        # print(f"Download {int(status.progress()*100)}%")  # optional
    fh.seek(0)
    text = fh.read().decode("utf-8")
    return json.loads(text)

def upload_json_to_drive(file_id, data_obj):
    """Upload (overwrite) JSON content to the Drive file."""
    # Convert python obj to bytes
    file_bytes = io.BytesIO(json.dumps(data_obj).encode("utf-8"))
    media_body = MediaIoBaseUpload(file_bytes, mimetype="application/json", resumable=True)
    updated = drive_service.files().update(
        fileId=file_id,
        media_body=media_body
    ).execute()
    return updated


# ——— Load or initialize data ———

try:
    watchlists = download_json_from_drive(WATCHLIST_FILE_ID)
except Exception as e:
    print("Failed to load watchlists from Drive:", e)
    watchlists = {}

try:
    status_cache = download_json_from_drive(STATUS_CACHE_FILE_ID)
except Exception as e:
    print("Failed to load status_cache from Drive:", e)
    status_cache = {}

# ——— Save helpers ———

def save_watchlists():
    try:
        upload_json_to_drive(WATCHLIST_FILE_ID, watchlists)
    except Exception as e:
        print("Error saving watchlists to Drive:", e)

def save_status_cache():
    try:
        upload_json_to_drive(STATUS_CACHE_FILE_ID, status_cache)
    except Exception as e:
        print("Error saving status_cache to Drive:", e)


# ——— Instagram account status check function (unchanged) ———

def check_account_status(username):
    try:
        username = username.lower()
        queries = [
            f"site:instagram.com {username}",
            f"https://www.instagram.com/{username}/",
            f"instagram.com/{username}"
        ]

        # Try Google API
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

        # Fallback: direct Instagram
        insta_url = f"https://www.instagram.com/{username}/"
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


# ——— Telegram command handlers ———

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

        current_status = check_account_status(username)
        if chat_id not in status_cache:
            status_cache[chat_id] = {}
        status_cache[chat_id][username] = current_status
        save_status_cache()

        await update.message.reply_text(
            f"✅ Added {username} to your watchlist.\nStatus: {current_status}"
        )
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


async def monitor_accounts(context: ContextTypes.DEFAULT_TYPE):
    for chat_id, usernames in watchlists.items():
        if chat_id not in status_cache:
            status_cache[chat_id] = {}
        for username in usernames:
            current_status = check_account_status(username)
            last_status = status_cache[chat_id].get(username)

            if last_status is None:
                status_cache[chat_id][username] = {
                    "confirmed": current_status,
                    "pending": current_status
                }
                save_status_cache()
                continue

            if isinstance(last_status, str):
                last_status = {
                    "confirmed": last_status,
                    "pending": last_status
                }
                status_cache[chat_id][username] = last_status

            if current_status == last_status["pending"]:
                if current_status != last_status["confirmed"]:
                    status_cache[chat_id][username]["confirmed"] = current_status
                    save_status_cache()
                    await context.bot.send_message(
                        chat_id=int(chat_id),
                        text=f"⚠ ALERT: {username} status changed → {current_status}"
                    )
            else:
                status_cache[chat_id][username]["pending"] = current_status
                save_status_cache()


async def register_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if "chat_ids" not in context.application.bot_data:
        context.application.bot_data["chat_ids"] = []
    if chat_id not in context.application.bot_data["chat_ids"]:
        context.application.bot_data["chat_ids"].append(chat_id)


# ——— Main ———

app = ApplicationBuilder().token(BOT_TOKEN).build()

app.add_handler(CommandHandler("add", add_account))
app.add_handler(CommandHandler("remove", remove_account))
app.add_handler(CommandHandler("list", list_accounts))
app.add_handler(CommandHandler("check", check_account))
app.add_handler(CommandHandler("start", register_chat))

app.job_queue.run_repeating(monitor_accounts, interval=CHECK_INTERVAL * 60, first=10)

if __name__ == "__main__":
    threading.Thread(target=run_flask).start()
    app.run_polling()


