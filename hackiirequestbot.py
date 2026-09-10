import logging
import json
import sys
import os
import sqlite3
import threading
import asyncio
import urllib.request
import base64
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ChatJoinRequestHandler, ContextTypes, MessageHandler, filters

# Setup logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# =================== [ CONFIGURATION ] ===================
# Secrets are read from Render Environment Variables.
BOT_TOKEN = os.getenv("BOT_TOKEN")
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except ValueError:
    ADMIN_ID = 0
# =========================================================

# =================== GITHUB CONFIG ===================
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_OWNER = os.getenv("GITHUB_OWNER")
GITHUB_REPO = os.getenv("GITHUB_REPO")
GITHUB_FILE = os.getenv("GITHUB_FILE", "members.json")
# =====================================================

if not BOT_TOKEN or not ADMIN_ID:
    print("\n❌ ERROR: BOT_TOKEN aur ADMIN_ID Render Environment Variables me set karo!\n")
    sys.exit(1)

# IMPORTANT:
# CACHED_MESSAGES = ORIGINAL request/join-request sequence.
# START_MESSAGES = NEW /start sequence. They are intentionally separate.
CACHED_MESSAGES = []
START_MESSAGES = []
APPROVAL_MESSAGE = None  # (chat_id, msg_id)

DEFAULT_APPROVAL_TEXT = "VIP ME APPROVAL KLIYE NEECHE BUTTON PE TAP KARE 👇👇👇👇"
DEFAULT_APPROVAL_BUTTON = "✅ APPROVE ME"
GITHUB_SYNC_LOCK = threading.Lock()

# --- WEB SERVER & ANTI-SLEEP ---
class HealthCheckServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(b"Bot is Running 24/7 Deeply Active on Render!")

    def log_message(self, format, *args):
        return

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckServer)
    logging.info(f"🟢 Web Server started successfully on port {port}")
    server.serve_forever()

def self_ping_loop():
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not render_url:
        render_url = f"http://localhost:{os.environ.get('PORT', 8080)}"
    while True:
        try:
            import time
            time.sleep(15)
            if "localhost" not in render_url:
                req = urllib.request.Request(render_url, headers={'User-Agent': 'VIP-Hyper-Bot'})
                urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            logging.error(f"⚠️ Ping Note: {e}")

# --- DB, GITHUB & SYNC HELPERS ---
def init_db():
    global CACHED_MESSAGES, START_MESSAGES, APPROVAL_MESSAGE
    conn = sqlite3.connect('janeman_pro.db')
    cursor = conn.cursor()

    # ORIGINAL tables - kept intact.
    cursor.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS stats (key TEXT PRIMARY KEY, count INTEGER)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS messages_list (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, msg_id TEXT)''')

    # NEW tables only for /start flow.
    cursor.execute('''CREATE TABLE IF NOT EXISTS start_messages_list (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, msg_id TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS start_flow_users (user_id INTEGER PRIMARY KEY, final_reached INTEGER DEFAULT 0)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS approval_message (id INTEGER PRIMARY KEY CHECK (id=1), chat_id TEXT, msg_id TEXT)''')
    cursor.execute("INSERT OR IGNORE INTO settings VALUES ('auto_accept', 'OFF')")
    cursor.execute("INSERT OR IGNORE INTO stats VALUES ('total_requests', 0)")
    cursor.execute("INSERT OR IGNORE INTO stats VALUES ('accepted', 0)")
    cursor.execute("INSERT OR IGNORE INTO settings VALUES ('start_final_id', '')")
    cursor.execute("INSERT OR IGNORE INTO settings VALUES ('approval_button', ?)", (DEFAULT_APPROVAL_BUTTON,))
    conn.commit()

    # ORIGINAL request/join-request messages.
    cursor.execute("SELECT chat_id, msg_id FROM messages_list ORDER BY id ASC")
    CACHED_MESSAGES = cursor.fetchall()

    # NEW /start messages.
    cursor.execute("SELECT chat_id, msg_id FROM start_messages_list ORDER BY id ASC")
    START_MESSAGES = cursor.fetchall()
    cursor.execute("SELECT chat_id, msg_id FROM approval_message WHERE id=1")
    APPROVAL_MESSAGE = cursor.fetchone()
    conn.close()


def _github_get_members():
    if not all([GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO]):
        return []
    try:
        url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{GITHUB_FILE}"
        headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            return []
        raw = base64.b64decode(r.json()["content"]).decode()
        users = json.loads(raw)
        return [int(uid) for uid in users]
    except Exception as e:
        logging.error(f"GitHub Load Error: {e}")
        return []


def sync_users_to_github():
    """Merge local + GitHub member IDs and write the union back to members.json."""
    if not all([GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO]):
        logging.warning("GitHub sync skipped: GITHUB_TOKEN/OWNER/REPO missing")
        return False

    with GITHUB_SYNC_LOCK:
        try:
            remote_users = set(_github_get_members())
            local_users = set(get_all_users())
            users = sorted(remote_users | local_users)

            url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{GITHUB_FILE}"
            headers = {
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }

            current = requests.get(url, headers=headers, timeout=15)
            sha = current.json().get("sha") if current.status_code == 200 else None

            content = json.dumps(users, indent=2) + "\n"
            content_encoded = base64.b64encode(content.encode()).decode()
            data = {"message": "Update members.json", "content": content_encoded}
            if sha:
                data["sha"] = sha

            response = requests.put(url, headers=headers, json=data, timeout=20)
            if response.status_code in (200, 201):
                logging.info(f"GitHub members.json synced successfully: {len(users)} members")
                return True

            logging.error(f"GitHub Sync Failed [{response.status_code}]: {response.text}")
            return False
        except Exception as e:
            logging.error(f"GitHub Sync Error: {e}")
            return False


def load_users_from_github():
    users = _github_get_members()
    if not users:
        return
    try:
        conn = sqlite3.connect("janeman_pro.db")
        cursor = conn.cursor()
        for uid in users:
            cursor.execute("INSERT OR IGNORE INTO users VALUES (?)", (uid,))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"GitHub Load DB Error: {e}")


def add_user(user_id):
    conn = sqlite3.connect("janeman_pro.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users VALUES (?)", (user_id,))
    conn.commit()
    conn.close()
    sync_users_to_github()


def get_all_users():
    conn = sqlite3.connect('janeman_pro.db')
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    users = [row[0] for row in cursor.fetchall()]
    conn.close()
    return users

# --- DB HELPERS ---
def get_setting(key):
    conn = sqlite3.connect('janeman_pro.db')
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key=?", (key,))
    res = cursor.fetchone()
    conn.close()
    return res[0] if res else "OFF"


def set_setting(key, value):
    conn = sqlite3.connect('janeman_pro.db')
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

# ================= ORIGINAL REQUEST/JOIN-REQUEST MESSAGE FUNCTIONS =================
def add_saved_message(chat_id, msg_id):
    global CACHED_MESSAGES
    conn = sqlite3.connect('janeman_pro.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO messages_list (chat_id, msg_id) VALUES (?, ?)", (str(chat_id), str(msg_id)))
    conn.commit()
    cursor.execute("SELECT chat_id, msg_id FROM messages_list ORDER BY id ASC")
    CACHED_MESSAGES = cursor.fetchall()
    conn.close()


def clear_saved_messages():
    global CACHED_MESSAGES
    conn = sqlite3.connect('janeman_pro.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM messages_list")
    conn.commit()
    CACHED_MESSAGES = []
    conn.close()

# ================= NEW /START MESSAGE FUNCTIONS =================
def add_start_message(chat_id, msg_id):
    global START_MESSAGES
    conn = sqlite3.connect('janeman_pro.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO start_messages_list (chat_id, msg_id) VALUES (?, ?)", (str(chat_id), str(msg_id)))
    conn.commit()
    cursor.execute("SELECT chat_id, msg_id FROM start_messages_list ORDER BY id ASC")
    START_MESSAGES = cursor.fetchall()
    conn.close()


def clear_start_messages():
    global START_MESSAGES
    conn = sqlite3.connect('janeman_pro.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM start_messages_list")
    conn.commit()
    START_MESSAGES = []
    set_setting('start_final_id', '')
    conn.close()


def get_start_final_id():
    return get_setting('start_final_id')


def set_start_final_id(message_key):
    set_setting('start_final_id', message_key)


def mark_start_final_reached(user_id):
    conn = sqlite3.connect('janeman_pro.db')
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO start_flow_users (user_id, final_reached) VALUES (?, 1)", (user_id,))
    conn.commit()
    conn.close()


def has_start_final_reached(user_id):
    conn = sqlite3.connect('janeman_pro.db')
    cursor = conn.cursor()
    cursor.execute("SELECT final_reached FROM start_flow_users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return bool(row and row[0] == 1)


def clear_start_final_for_user(user_id):
    conn = sqlite3.connect('janeman_pro.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM start_flow_users WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

# ================= APPROVAL MESSAGE / BUTTON SETTINGS =================
def get_approval_button():
    value = get_setting("approval_button")
    return value if value and value != "OFF" else DEFAULT_APPROVAL_BUTTON


def save_approval_message(chat_id, msg_id):
    global APPROVAL_MESSAGE
    conn = sqlite3.connect("janeman_pro.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO approval_message (id, chat_id, msg_id) VALUES (1, ?, ?)", (str(chat_id), str(msg_id)))
    conn.commit()
    APPROVAL_MESSAGE = (str(chat_id), str(msg_id))
    conn.close()


def clear_approval_message():
    global APPROVAL_MESSAGE
    conn = sqlite3.connect("janeman_pro.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM approval_message")
    conn.commit()
    APPROVAL_MESSAGE = None
    conn.close()


def get_approval_menu():
    status = "Custom message saved" if APPROVAL_MESSAGE else "Default text"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📝 Message: {status}", callback_data="set_approval_message")],
        [InlineKeyboardButton(f"🔘 Button: {get_approval_button()}", callback_data="set_approval_button")],
        [InlineKeyboardButton("🗑️ Use Default Message", callback_data="clear_approval_message")],
        [InlineKeyboardButton("⬅️ Back", callback_data="start_settings")]
    ])


async def send_approval_message(bot, chat_id):
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(get_approval_button(), callback_data="approve_me")]])
    if APPROVAL_MESSAGE:
        try:
            await bot.copy_message(
                chat_id=chat_id,
                from_chat_id=int(APPROVAL_MESSAGE[0]),
                message_id=int(APPROVAL_MESSAGE[1]),
                reply_markup=keyboard
            )
            return
        except Exception as e:
            logging.error(f"Custom approval message failed, using default: {e}")
    await bot.send_message(chat_id=chat_id, text=DEFAULT_APPROVAL_TEXT, reply_markup=keyboard)


# --- STATS ---
def get_stats():
    conn = sqlite3.connect('janeman_pro.db')
    cursor = conn.cursor()
    cursor.execute("SELECT key, count FROM stats")
    res = dict(cursor.fetchall())
    conn.close()
    return res


def update_stat(key, amount=1):
    conn = sqlite3.connect('janeman_pro.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE stats SET count = count + ? WHERE key=?", (amount, key))
    conn.commit()
    conn.close()

# --- UI & HANDLERS ---
def get_main_menu():
    stats = get_stats()
    total_users = len(get_all_users())
    keyboard = [
        [InlineKeyboardButton(f"📊 Total Requests: {stats.get('total_requests', 0)}", callback_data="none")],
        [InlineKeyboardButton(f"✅ Auto-Approved: {stats.get('accepted', 0)}", callback_data="none")],
        [InlineKeyboardButton(f"👥 Database Users: {total_users}", callback_data="none")],
        [InlineKeyboardButton("⚙️ Welcome Settings", callback_data="welcome_settings"), InlineKeyboardButton("📣 Broadcast Tool", callback_data="broadcast_tool")],
        [InlineKeyboardButton("▶️ Start Message Settings", callback_data="start_settings")],
        [InlineKeyboardButton("🔄 Sync Members", callback_data="sync_members")],
        [InlineKeyboardButton("🔄 Refresh Panel", callback_data="refresh_main")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_welcome_menu():
    # ORIGINAL menu/function - remains for JOIN REQUEST flow.
    auto_status = get_setting("auto_accept")
    status_emoji = "🟢 ON (Auto Accept)" if auto_status == "ON" else "🔴 OFF (Manual/No Accept)"
    total_saved = len(CACHED_MESSAGES)
    keyboard = [
        [InlineKeyboardButton(f"Status: {status_emoji}", callback_data="toggle_auto")],
        [InlineKeyboardButton(f"➕ Add Message / Media", callback_data="edit_welcome")],
        [InlineKeyboardButton(f"🗑️ Clear All Saved ({total_saved})", callback_data="clear_welcome")],
        [InlineKeyboardButton("👁️ Test Sequence Message", callback_data="test_msg")],
        [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="refresh_main")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_start_menu():
    final_id = get_start_final_id()
    total_saved = len(START_MESSAGES)
    final_text = f"FINAL: #{final_id}" if final_id else "FINAL: Not Set"
    keyboard = [
        [InlineKeyboardButton("➕ Add Start Message / Media", callback_data="add_start")],
        [InlineKeyboardButton(f"🗑️ Clear Start Saved ({total_saved})", callback_data="clear_start")],
        [InlineKeyboardButton("🎯 Set Final Message", callback_data="set_start_final")],
        [InlineKeyboardButton(final_text, callback_data="start_final_info")],
        [InlineKeyboardButton("👁️ Test Start Sequence", callback_data="test_start")],
        [InlineKeyboardButton("✏️ Approval Message / Button", callback_data="approval_settings")],
        [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="refresh_main")]
    ]
    return InlineKeyboardMarkup(keyboard)


async def send_sequence_messages_instant(bot, chat_id):
    # ORIGINAL request/join-request sequence ONLY.
    if not CACHED_MESSAGES:
        return
    for row in CACHED_MESSAGES:
        try:
            await bot.copy_message(chat_id=chat_id, from_chat_id=int(row[0]), message_id=int(row[1]))
        except Exception as e:
            logging.error(f"Fast Delivery skipped: {e}")


async def send_start_sequence(bot, chat_id, user_id=None):
    # NEW /start sequence ONLY. If FINAL is set, delivery stops exactly at FINAL.
    if not START_MESSAGES:
        return False

    final_id = str(get_start_final_id() or "")
    reached_final = False

    for index, row in enumerate(START_MESSAGES, start=1):
        try:
            await bot.copy_message(chat_id=chat_id, from_chat_id=int(row[0]), message_id=int(row[1]))
        except Exception as e:
            logging.error(f"Start Delivery skipped: {e}")
            continue

        if final_id and str(index) == final_id:
            reached_final = True
            break

    if reached_final and user_id is not None:
        mark_start_final_reached(user_id)
    return reached_final


async def start(update, context):
    user_id = update.effective_user.id
    add_user(user_id)

    # /start flow: ONLY Start Message Settings are delivered here.
    # Welcome Settings messages are NEVER delivered by /start.
    clear_start_final_for_user(user_id)
    await send_start_sequence(context.bot, user_id, user_id)

    # Approval message/button is a separate editable setting.
    await send_approval_message(context.bot, user_id)

    if user_id == ADMIN_ID:
        await update.message.reply_text(
            "👑 **JANEMAN BOT V20** 👑",
            reply_markup=get_main_menu(),
            parse_mode="Markdown"
        )


async def handle_callbacks(update, context):
    query = update.callback_query

    # Original behavior: admin panel callbacks are admin-only, except the user-facing approve button.
    if query.data == "approve_me":
        await query.answer("✅ Approval request received!", show_alert=True)
        return

    if query.from_user.id != ADMIN_ID:
        return

    await query.answer()

    if query.data == "none":
        return

    if query.data == "sync_members":
        ok = sync_users_to_github()
        await query.answer("✅ Members synced to GitHub" if ok else "❌ GitHub sync failed", show_alert=True)
        return

    if query.data == "refresh_main":
        await query.edit_message_text("👑 **JANEMAN BOT V20** 👑", reply_markup=get_main_menu(), parse_mode="Markdown")

    elif query.data == "welcome_settings":
        await query.edit_message_text("⚙️ **Welcome Settings (Join Request)**", reply_markup=get_welcome_menu(), parse_mode="Markdown")

    elif query.data == "toggle_auto":
        new_status = "OFF" if get_setting("auto_accept") == "ON" else "ON"
        set_setting("auto_accept", new_status)
        await query.edit_message_text(f"⚙️ Status: {new_status}", reply_markup=get_welcome_menu(), parse_mode="Markdown")

    elif query.data == "edit_welcome":
        context.user_data['state'] = 'waiting_welcome'
        await query.edit_message_text("📝 **Join Request ke liye Message / Media bhejein...**")

    elif query.data == "clear_welcome":
        clear_saved_messages()
        await query.edit_message_text("🗑️ Cleared!", reply_markup=get_welcome_menu(), parse_mode="Markdown")

    elif query.data == "test_msg":
        await send_sequence_messages_instant(context.bot, ADMIN_ID)

    elif query.data == "broadcast_tool":
        context.user_data['state'] = 'waiting_broadcast'
        await query.edit_message_text("📣 **Post bhejein broadcast ke liye:**")

    # NEW /START SETTINGS
    elif query.data == "start_settings":
        await query.edit_message_text("▶️ **Start Message Settings**\n\nYe messages sirf /start dabane par jayenge.\nWelcome Settings wale messages join request par hi jayenge.", reply_markup=get_start_menu(), parse_mode="Markdown")

    elif query.data == "add_start":
        context.user_data['state'] = 'waiting_start'
        await query.edit_message_text("📝 **/start ke liye Message / Media bhejein...**")

    elif query.data == "clear_start":
        clear_start_messages()
        await query.edit_message_text("🗑️ Start messages cleared!", reply_markup=get_start_menu(), parse_mode="Markdown")

    elif query.data == "set_start_final":
        if not START_MESSAGES:
            await query.edit_message_text("❌ Pehle Start Message / Media add karo.", reply_markup=get_start_menu(), parse_mode="Markdown")
        else:
            context.user_data['state'] = 'waiting_start_final'
            await query.edit_message_text(
                "🎯 **Final message set karo**\n\n" +
                "Apne saved Start messages me se jis number ko FINAL banana hai, sirf number bhejo.\n\n" +
                "Example: `3`",
                parse_mode="Markdown"
            )

    elif query.data == "start_final_info":
        final_id = get_start_final_id()
        await query.answer(f"FINAL = {final_id or 'Not Set'}", show_alert=True)

    elif query.data == "test_start":
        await send_start_sequence(context.bot, ADMIN_ID, None)
        await send_approval_message(context.bot, ADMIN_ID)

    elif query.data == "approval_settings":
        await query.edit_message_text(
            "✏️ **Approval Message / Button Settings**\n\n"
            "Ye /start flow ke end me aane wala approval message hai.\n"
            "Welcome Settings ka Join Request message isse alag hai.",
            reply_markup=get_approval_menu(), parse_mode="Markdown"
        )

    elif query.data == "set_approval_message":
        context.user_data['state'] = 'waiting_approval_message'
        await query.edit_message_text("📝 **Approval ke liye koi bhi message/media bhejo.**\n\nYe /start ke baad APPROVE button ke saath send hoga.")

    elif query.data == "set_approval_button":
        context.user_data['state'] = 'waiting_approval_button'
        await query.edit_message_text(f"🔘 **Naya button text bhejo.**\n\nCurrent: `{get_approval_button()}`", parse_mode="Markdown")

    elif query.data == "clear_approval_message":
        clear_approval_message()
        await query.edit_message_text("✅ Default approval message restore ho gaya.", reply_markup=get_approval_menu(), parse_mode="Markdown")

    elif query.data == "start_final_info":
        await query.answer(f"FINAL = {get_start_final_id() or 'Not Set'}", show_alert=True)


async def content_handler(update, context):
    user_id = update.effective_user.id

    # ================= NEW: ADMIN REPLY TO A USER =================
    if user_id == ADMIN_ID:
        reply_to_user = context.user_data.get('reply_to_user')
        if reply_to_user:
            try:
                await context.bot.copy_message(
                    chat_id=int(reply_to_user),
                    from_chat_id=update.message.chat_id,
                    message_id=update.message.message_id
                )
                context.user_data['reply_to_user'] = None
                await update.message.reply_text(f"✅ Reply sent to UID: {reply_to_user}")
            except Exception as e:
                logging.error(f"Admin Reply Error: {e}")
                await update.message.reply_text("❌ Reply send nahi hua. User ne bot block kiya ho sakta hai.")
            return

        state = context.user_data.get('state')

        # ORIGINAL welcome/request message saving.
        if state == 'waiting_welcome':
            add_saved_message(update.message.chat_id, update.message.message_id)
            await update.message.reply_text("✅ Cached for JOIN REQUEST!")
            return

        # NEW start message saving.
        if state == 'waiting_start':
            add_start_message(update.message.chat_id, update.message.message_id)
            await update.message.reply_text(f"✅ Start message #{len(START_MESSAGES)} saved!")
            return

        # NEW final message selection.
        if state == 'waiting_start_final':
            text = (update.message.text or '').strip()
            if not text.isdigit():
                await update.message.reply_text("❌ Sirf number bhejo. Example: 3")
                return
            number = int(text)
            if number < 1 or number > len(START_MESSAGES):
                await update.message.reply_text(f"❌ Number 1 se {len(START_MESSAGES)} ke beech hona chahiye.")
                return
            set_start_final_id(str(number))
            context.user_data['state'] = None
            await update.message.reply_text(f"🎯 Start message #{number} FINAL set ho gaya.", reply_markup=get_start_menu())
            return

        # NEW approval message saving.
        if state == 'waiting_approval_message':
            save_approval_message(update.message.chat_id, update.message.message_id)
            context.user_data['state'] = None
            await update.message.reply_text("✅ Approval message/media saved! Ab /start par ye APPROVE button ke saath jayega.", reply_markup=get_approval_menu())
            return

        if state == 'waiting_approval_button':
            text = (update.message.text or '').strip()
            if not text:
                await update.message.reply_text("❌ Button ka text plain text me bhejo.")
                return
            if len(text) > 50:
                await update.message.reply_text("❌ Button text 50 characters se chhota rakho.")
                return
            set_setting('approval_button', text)
            context.user_data['state'] = None
            await update.message.reply_text("✅ Approval button text updated!", reply_markup=get_approval_menu())
            return

        # ORIGINAL broadcast behavior.
        if state == 'waiting_broadcast':
            context.user_data['state'] = None
            users = get_all_users()
            for u_id in users:
                try:
                    await context.bot.copy_message(chat_id=u_id, from_chat_id=update.message.chat_id, message_id=update.message.message_id)
                except Exception:
                    pass
            await update.message.reply_text("🏁 Broadcast Done!")
            return

        return

    # ================= NEW: USER MESSAGE AFTER /start FINAL =================
    if has_start_final_reached(user_id):
        try:
            username = update.effective_user.username
            name = update.effective_user.full_name or "Unknown"
            header = (
                "📩 **New User Message After FINAL**\n\n"
                f"👤 Name: {name}\n"
                f"🆔 UID: `{user_id}`\n"
                f"🔗 Username: @{username}" if username else
                "📩 **New User Message After FINAL**\n\n"
                f"👤 Name: {name}\n"
                f"🆔 UID: `{user_id}`\n"
                "🔗 Username: Not set"
            )
            reply_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("↩️ Reply to User", callback_data=f"reply_user:{user_id}")]
            ])
            await context.bot.send_message(ADMIN_ID, header, parse_mode="Markdown", reply_markup=reply_keyboard)
            await context.bot.copy_message(
                chat_id=ADMIN_ID,
                from_chat_id=update.message.chat_id,
                message_id=update.message.message_id
            )
        except Exception as e:
            logging.error(f"Forward User Message Error: {e}")


async def callback_reply_user(update, context):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        return
    if not query.data.startswith("reply_user:"):
        return
    try:
        user_id = int(query.data.split(":", 1)[1])
    except Exception:
        await query.answer("Invalid user", show_alert=True)
        return
    context.user_data['reply_to_user'] = user_id
    context.user_data['state'] = None
    await query.answer()
    await query.message.reply_text(f"✍️ UID `{user_id}` ko reply bhejo. /cancel_reply se cancel kar sakte ho.", parse_mode="Markdown")


async def sync_members_command(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    ok = sync_users_to_github()
    total = len(get_all_users())
    await update.message.reply_text(
        f"{'✅' if ok else '❌'} GitHub members sync {'successful' if ok else 'failed'}.\nLocal DB members: {total}"
    )


async def cancel_reply(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    context.user_data['reply_to_user'] = None
    context.user_data['state'] = None
    await update.message.reply_text("❌ Reply mode cancelled.")


async def join_request_handler(update, context):
    request = update.chat_join_request
    if not request:
        return

    # ORIGINAL JOIN REQUEST flow ONLY.
    update_stat('total_requests', 1)
    add_user(request.from_user.id)

    # IMPORTANT: /start messages are NOT sent here.
    await send_sequence_messages_instant(context.bot, request.from_user.id)

    if get_setting("auto_accept") == "ON":
        await context.bot.approve_chat_join_request(chat_id=request.chat.id, user_id=request.from_user.id)
        update_stat('accepted', 1)


def main():
    init_db()
    load_users_from_github()
    sync_users_to_github()

    threading.Thread(target=run_health_server, daemon=True).start()
    threading.Thread(target=self_ping_loop, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel_reply", cancel_reply))
    app.add_handler(CommandHandler("sync_members", sync_members_command))
    app.add_handler(CallbackQueryHandler(callback_reply_user, pattern=r"^reply_user:"))
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.add_handler(ChatJoinRequestHandler(join_request_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, content_handler))

    print("\n🟢 VIP HYPER-SPEED 24/7 ENGINE ONLINE 🟢\n")
    app.run_polling()


if __name__ == '__main__':
    main()
