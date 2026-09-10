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
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ChatJoinRequestHandler, ContextTypes, MessageHandler, filters
)

# ============================================================
# CONFIG
# ============================================================
# IMPORTANT:
# Set these as environment variables on Render/Hostinger.
# Do NOT put your real bot token inside this file.
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_OWNER = os.getenv("GITHUB_OWNER")
GITHUB_REPO = os.getenv("GITHUB_REPO")
GITHUB_FILE = os.getenv("GITHUB_FILE", "members.json")

DB_FILE = "janeman_pro.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

if not BOT_TOKEN or not ADMIN_ID:
    print("ERROR: BOT_TOKEN aur ADMIN_ID environment variables set karo.")
    sys.exit(1)

CACHED_MESSAGES = []


# ============================================================
# WEB SERVER / RENDER COMPATIBILITY
# ============================================================
class HealthCheckServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Bot is running.")

    def log_message(self, format, *args):
        return


def run_health_server():
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthCheckServer)
    logger.info("Health server started on port %s", port)
    server.serve_forever()


def self_ping_loop():
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not render_url:
        return

    while True:
        try:
            import time
            time.sleep(30)
            req = urllib.request.Request(
                render_url,
                headers={"User-Agent": "VIP-Hyper-Bot"}
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            logger.debug("Ping: %s", e)


# ============================================================
# DATABASE
# ============================================================
def db():
    return sqlite3.connect(DB_FILE, timeout=30)


def init_db():
    global CACHED_MESSAGES

    conn = db()
    cursor = conn.cursor()

    cursor.execute(
        "CREATE TABLE IF NOT EXISTS settings "
        "(key TEXT PRIMARY KEY, value TEXT)"
    )
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS stats "
        "(key TEXT PRIMARY KEY, count INTEGER)"
    )
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS users "
        "(user_id INTEGER PRIMARY KEY)"
    )
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS messages_list "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, msg_id TEXT)"
    )

    # New feature: mark which saved message is the final message.
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS flow_settings "
        "(key TEXT PRIMARY KEY, value TEXT)"
    )

    # New feature: users who have reached the final message.
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS flow_users "
        "(user_id INTEGER PRIMARY KEY, reached_final INTEGER DEFAULT 0)"
    )

    cursor.execute(
        "INSERT OR IGNORE INTO settings VALUES ('auto_accept', 'OFF')"
    )
    cursor.execute(
        "INSERT OR IGNORE INTO stats VALUES ('total_requests', 0)"
    )
    cursor.execute(
        "INSERT OR IGNORE INTO stats VALUES ('accepted', 0)"
    )
    cursor.execute(
        "INSERT OR IGNORE INTO flow_settings VALUES ('final_message_id', '')"
    )

    conn.commit()

    cursor.execute(
        "SELECT chat_id, msg_id FROM messages_list ORDER BY id ASC"
    )
    CACHED_MESSAGES = cursor.fetchall()
    conn.close()


def get_setting(key):
    conn = db()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key=?", (key,))
    res = cursor.fetchone()
    conn.close()
    return res[0] if res else "OFF"


def set_setting(key, value):
    conn = db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO settings VALUES (?, ?)",
        (key, value)
    )
    conn.commit()
    conn.close()


def get_flow_setting(key):
    conn = db()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM flow_settings WHERE key=?", (key,))
    res = cursor.fetchone()
    conn.close()
    return res[0] if res else ""


def set_flow_setting(key, value):
    conn = db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO flow_settings VALUES (?, ?)",
        (key, str(value))
    )
    conn.commit()
    conn.close()


def mark_user_reached_final(user_id):
    conn = db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO flow_users(user_id, reached_final) VALUES (?, 1)",
        (user_id,)
    )
    conn.commit()
    conn.close()


def user_reached_final(user_id):
    conn = db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT reached_final FROM flow_users WHERE user_id=?",
        (user_id,)
    )
    res = cursor.fetchone()
    conn.close()
    return bool(res and res[0] == 1)


# ============================================================
# GITHUB MEMBER BACKUP - MERGE, NEVER WIPE EXISTING MEMBERS
# ============================================================
def get_all_users():
    conn = db()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users ORDER BY user_id")
    users = [row[0] for row in cursor.fetchall()]
    conn.close()
    return users


def add_user(user_id):
    conn = db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO users VALUES (?)",
        (int(user_id),)
    )
    conn.commit()
    conn.close()
    sync_users_to_github()


def github_configured():
    return all([GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO])


def load_users_from_github():
    """Load members.json and MERGE its members into local DB."""
    if not github_configured():
        logger.warning("GitHub backup not configured.")
        return set()

    try:
        url = (
            f"https://api.github.com/repos/{GITHUB_OWNER}/"
            f"{GITHUB_REPO}/contents/{GITHUB_FILE}"
        )
        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json"
        }

        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 404:
            logger.info("members.json does not exist yet.")
            return set()

        r.raise_for_status()

        data = r.json()
        raw = base64.b64decode(data["content"]).decode("utf-8")
        members = json.loads(raw)

        if not isinstance(members, list):
            logger.warning("members.json is not a list; ignoring it.")
            return set()

        members = {int(x) for x in members}

        conn = db()
        cursor = conn.cursor()
        for uid in members:
            cursor.execute(
                "INSERT OR IGNORE INTO users VALUES (?)",
                (uid,)
            )
        conn.commit()
        conn.close()

        logger.info("Restored %s members from GitHub.", len(members))
        return members

    except Exception as e:
        logger.error("GitHub Load Error: %s", e)
        return set()


def sync_users_to_github():
    """
    Merge local DB + existing members.json and write the UNION.
    This prevents a redeploy with an empty/new DB from deleting old members.
    """
    if not github_configured():
        return

    try:
        url = (
            f"https://api.github.com/repos/{GITHUB_OWNER}/"
            f"{GITHUB_REPO}/contents/{GITHUB_FILE}"
        )
        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json"
        }

        existing_members = set()
        sha = None

        r = requests.get(url, headers=headers, timeout=15)

        if r.status_code == 200:
            data = r.json()
            sha = data.get("sha")
            try:
                raw = base64.b64decode(data["content"]).decode("utf-8")
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    existing_members = {int(x) for x in parsed}
            except Exception:
                logger.warning("Could not parse existing members.json.")
        elif r.status_code != 404:
            logger.error("GitHub GET failed: %s", r.text)
            return

        local_members = set(get_all_users())
        merged = sorted(existing_members | local_members)

        # Also keep local DB aligned with the merged backup.
        conn = db()
        cursor = conn.cursor()
        for uid in merged:
            cursor.execute(
                "INSERT OR IGNORE INTO users VALUES (?)",
                (uid,)
            )
        conn.commit()
        conn.close()

        content = json.dumps(merged, indent=2)
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

        payload = {
            "message": "Update members.json",
            "content": encoded
        }
        if sha:
            payload["sha"] = sha

        response = requests.put(
            url,
            headers=headers,
            json=payload,
            timeout=20
        )

        if response.status_code not in (200, 201):
            logger.error("GitHub Sync Failed: %s", response.text)
        else:
            logger.info("GitHub members synced: %s members.", len(merged))

    except Exception as e:
        logger.error("GitHub Sync Error: %s", e)


# ============================================================
# SAVED MESSAGE / FLOW HELPERS
# ============================================================
def add_saved_message(chat_id, msg_id):
    global CACHED_MESSAGES

    conn = db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO messages_list(chat_id, msg_id) VALUES (?, ?)",
        (str(chat_id), str(msg_id))
    )
    conn.commit()

    cursor.execute(
        "SELECT chat_id, msg_id FROM messages_list ORDER BY id ASC"
    )
    CACHED_MESSAGES = cursor.fetchall()
    conn.close()


def clear_saved_messages():
    global CACHED_MESSAGES

    conn = db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM messages_list")
    cursor.execute(
        "INSERT OR REPLACE INTO flow_settings(key, value) "
        "VALUES ('final_message_id', '')"
    )
    conn.commit()
    CACHED_MESSAGES = []
    conn.close()


def get_saved_messages_with_ids():
    conn = db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, chat_id, msg_id FROM messages_list ORDER BY id ASC"
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


async def send_sequence_messages_instant(bot, chat_id, mark_final=True):
    rows = get_saved_messages_with_ids()
    if not rows:
        return

    final_id = get_flow_setting("final_message_id")

    for row_id, source_chat, source_msg in rows:
        try:
            await bot.copy_message(
                chat_id=chat_id,
                from_chat_id=int(source_chat),
                message_id=int(source_msg)
            )

            if mark_final and final_id and str(row_id) == str(final_id):
                mark_user_reached_final(chat_id)

        except Exception as e:
            logger.error("Message delivery failed: %s", e)


# ============================================================
# ADMIN NOTIFICATION + REPLY
# ============================================================
def admin_user_text(user):
    username = f"@{user.username}" if user.username else "No username"
    name = (user.full_name or "Unknown").replace("\n", " ")
    return (
        f"👤 User Reached Final Step\n\n"
        f"Name: {name}\n"
        f"Username: {username}\n"
        f"UID: {user.id}\n\n"
        f"Reply karne ke liye neeche button dabao."
    )


async def notify_admin_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id == ADMIN_ID:
        return

    user = update.effective_user

    # Only forward after the user has reached the configured final message.
    if not user_reached_final(user.id):
        return

    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_user_text(user),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "↩️ Reply to User",
                    callback_data=f"reply_user:{user.id}"
                )]
            ])
        )

        await context.bot.copy_message(
            chat_id=ADMIN_ID,
            from_chat_id=update.message.chat_id,
            message_id=update.message.message_id
        )

    except Exception as e:
        logger.error("Admin notification failed: %s", e)


# ============================================================
# ADMIN PANEL UI
# ============================================================
def get_main_menu():
    stats = get_stats()
    total_users = len(get_all_users())

    keyboard = [
        [InlineKeyboardButton(
            f"📊 Total Requests: {stats.get('total_requests', 0)}",
            callback_data="none"
        )],
        [InlineKeyboardButton(
            f"✅ Auto-Approved: {stats.get('accepted', 0)}",
            callback_data="none"
        )],
        [InlineKeyboardButton(
            f"👥 Database Users: {total_users}",
            callback_data="none"
        )],
        [
            InlineKeyboardButton(
                "⚙️ Welcome Settings",
                callback_data="welcome_settings"
            ),
            InlineKeyboardButton(
                "📣 Broadcast Tool",
                callback_data="broadcast_tool"
            )
        ],
        [InlineKeyboardButton(
            "🔄 Refresh Panel",
            callback_data="refresh_main"
        )]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_welcome_menu():
    auto_status = get_setting("auto_accept")
    status_text = (
        "🟢 ON (Auto Accept)"
        if auto_status == "ON"
        else "🔴 OFF (Manual/No Accept)"
    )

    rows = get_saved_messages_with_ids()
    final_id = get_flow_setting("final_message_id")

    keyboard = [
        [InlineKeyboardButton(
            f"Status: {status_text}",
            callback_data="toggle_auto"
        )],
        [InlineKeyboardButton(
            "➕ Add Message / Media",
            callback_data="edit_welcome"
        )],
        [InlineKeyboardButton(
            "🎯 Set Final Message",
            callback_data="set_final_menu"
        )],
        [InlineKeyboardButton(
            "🗑️ Clear All Saved",
            callback_data="clear_welcome"
        )],
        [InlineKeyboardButton(
            "👁️ Test Sequence Message",
            callback_data="test_msg"
        )],
        [InlineKeyboardButton(
            "⬅️ Back to Main Menu",
            callback_data="refresh_main"
        )]
    ]

    if rows:
        keyboard.insert(
            2,
            [InlineKeyboardButton(
                f"Final: #{final_id}" if final_id else "Final: Not Set",
                callback_data="set_final_menu"
            )]
        )

    return InlineKeyboardMarkup(keyboard)


def get_final_menu():
    rows = get_saved_messages_with_ids()
    final_id = get_flow_setting("final_message_id")

    keyboard = []

    if rows:
        for row_id, _, _ in rows:
            mark = " ✅ FINAL" if str(row_id) == str(final_id) else ""
            keyboard.append([
                InlineKeyboardButton(
                    f"Message #{row_id}{mark}",
                    callback_data=f"set_final:{row_id}"
                )
            ])

    keyboard.append([
        InlineKeyboardButton(
            "❌ Remove Final",
            callback_data="remove_final"
        )
    ])
    keyboard.append([
        InlineKeyboardButton(
            "⬅️ Back",
            callback_data="welcome_settings"
        )
    ])

    return InlineKeyboardMarkup(keyboard)


# ============================================================
# STATS
# ============================================================
def get_stats():
    conn = db()
    cursor = conn.cursor()
    cursor.execute("SELECT key, count FROM stats")
    res = dict(cursor.fetchall())
    conn.close()
    return res


def update_stat(key, amount=1):
    conn = db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE stats SET count = count + ? WHERE key=?",
        (amount, key)
    )
    conn.commit()
    conn.close()


# ============================================================
# /START
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id)

    # Existing behavior preserved: approval button.
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "✅ APPROVE ME",
            callback_data="approve_me"
        )]
    ])

    await update.message.reply_text(
        "VIP ME APPROVAL KLIYE NEECHE BUTTON PE TAP KARE 👇👇👇👇",
        reply_markup=keyboard
    )

    if user.id == ADMIN_ID:
        await update.message.reply_text(
            "👑 JANEMAN BOT V20 👑",
            reply_markup=get_main_menu()
        )


# ============================================================
# CALLBACKS
# ============================================================
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # User-facing approve button.
    if query.data == "approve_me":
        await query.answer(
            "✅ Approval request received!",
            show_alert=True
        )
        return

    # Everything below is admin-only.
    if query.from_user.id != ADMIN_ID:
        return

    if query.data == "none":
        return

    if query.data == "refresh_main":
        await query.edit_message_text(
            "👑 JANEMAN BOT V20 👑",
            reply_markup=get_main_menu()
        )

    elif query.data == "welcome_settings":
        await query.edit_message_text(
            "⚙️ Settings",
            reply_markup=get_welcome_menu()
        )

    elif query.data == "toggle_auto":
        new_status = (
            "OFF"
            if get_setting("auto_accept") == "ON"
            else "ON"
        )
        set_setting("auto_accept", new_status)

        await query.edit_message_text(
            f"⚙️ Status: {new_status}",
            reply_markup=get_welcome_menu()
        )

    elif query.data == "edit_welcome":
        context.user_data["state"] = "waiting_welcome"
        await query.edit_message_text(
            "📝 Ab message / photo / video / voice / media bhejo.\n"
            "Har baar bheja hua item sequence mein save hoga.\n\n"
            "Message save hone ke baad dobara Add Message dabao."
        )

    elif query.data == "set_final_menu":
        rows = get_saved_messages_with_ids()
        if not rows:
            await query.edit_message_text(
                "❌ Pehle kam se kam ek message/media save karo.",
                reply_markup=get_welcome_menu()
            )
            return

        await query.edit_message_text(
            "🎯 Kaunsa saved message FINAL hoga?\n"
            "Us final message ke baad user ka next message UID ke saath admin ko milega.",
            reply_markup=get_final_menu()
        )

    elif query.data.startswith("set_final:"):
        row_id = query.data.split(":", 1)[1]
        set_flow_setting("final_message_id", row_id)

        await query.edit_message_text(
            f"✅ Message #{row_id} FINAL set ho gaya.\n\n"
            "Final message tak pahunchne ke baad user ka next message "
            "admin ko UID ke saath forward hoga.",
            reply_markup=get_welcome_menu()
        )

    elif query.data == "remove_final":
        set_flow_setting("final_message_id", "")
        await query.edit_message_text(
            "❌ Final message remove kar diya.",
            reply_markup=get_welcome_menu()
        )

    elif query.data == "clear_welcome":
        clear_saved_messages()
        await query.edit_message_text(
            "🗑️ Cleared!",
            reply_markup=get_welcome_menu()
        )

    elif query.data == "broadcast_tool":
        context.user_data["state"] = "waiting_broadcast"
        await query.edit_message_text(
            "📣 Post bhejo broadcast ke liye:"
        )

    elif query.data == "test_msg":
        await send_sequence_messages_instant(
            context.bot,
            ADMIN_ID,
            mark_final=False
        )

    elif query.data.startswith("reply_user:"):
        target_uid = query.data.split(":", 1)[1]

        context.user_data["reply_to_uid"] = int(target_uid)
        context.user_data["state"] = "waiting_admin_reply"

        await query.message.reply_text(
            f"✍️ UID {target_uid} ko reply bhejo.\n"
            f"Text, photo, video, voice ya media bhej sakte ho.\n"
            f"Cancel ke liye /cancel_reply"
        )


# ============================================================
# ADMIN CONTENT HANDLER
# ============================================================
async def content_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id = update.effective_user.id

    # Admin actions.
    if user_id == ADMIN_ID:
        state = context.user_data.get("state")

        if state == "waiting_welcome":
            add_saved_message(
                update.message.chat_id,
                update.message.message_id
            )
            await update.message.reply_text(
                "✅ Message/media saved.\n"
                "Agar ye FINAL hai to Welcome Settings → Set Final Message se select karo."
            )
            context.user_data["state"] = None
            return

        if state == "waiting_broadcast":
            context.user_data["state"] = None
            users = get_all_users()
            sent = 0

            for uid in users:
                try:
                    await context.bot.copy_message(
                        chat_id=uid,
                        from_chat_id=update.message.chat_id,
                        message_id=update.message.message_id
                    )
                    sent += 1
                except Exception:
                    pass

            await update.message.reply_text(
                f"🏁 Broadcast Done!\nSent: {sent}/{len(users)}"
            )
            return

        if state == "waiting_admin_reply":
            target_uid = context.user_data.get("reply_to_uid")

            if not target_uid:
                context.user_data["state"] = None
                await update.message.reply_text("❌ Reply target missing.")
                return

            try:
                await context.bot.copy_message(
                    chat_id=int(target_uid),
                    from_chat_id=update.message.chat_id,
                    message_id=update.message.message_id
                )
                await update.message.reply_text(
                    f"✅ Reply sent to UID {target_uid}."
                )
            except Exception as e:
                await update.message.reply_text(
                    f"❌ Reply send failed: {e}"
                )

            context.user_data["state"] = None
            context.user_data["reply_to_uid"] = None
            return

        return

    # User message after final step.
    if user_reached_final(user_id):
        await notify_admin_user_message(update, context)


# ============================================================
# ADMIN CANCEL REPLY
# ============================================================
async def cancel_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    context.user_data["state"] = None
    context.user_data["reply_to_uid"] = None
    await update.message.reply_text("❌ Reply cancelled.")


# ============================================================
# JOIN REQUEST
# ============================================================
async def join_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    request = update.chat_join_request
    if not request:
        return

    uid = request.from_user.id

    update_stat("total_requests", 1)
    add_user(uid)

    await send_sequence_messages_instant(
        context.bot,
        uid,
        mark_final=True
    )

    if get_setting("auto_accept") == "ON":
        try:
            await context.bot.approve_chat_join_request(
                chat_id=request.chat.id,
                user_id=uid
            )
            update_stat("accepted", 1)
        except Exception as e:
            logger.error("Auto-approve failed: %s", e)


# ============================================================
# MAIN
# ============================================================
def main():
    init_db()

    # FIRST restore GitHub members into the DB.
    # Then sync the UNION back to GitHub.
    load_users_from_github()
    sync_users_to_github()

    threading.Thread(
        target=run_health_server,
        daemon=True
    ).start()

    if os.environ.get("RENDER_EXTERNAL_URL"):
        threading.Thread(
            target=self_ping_loop,
            daemon=True
        ).start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel_reply", cancel_reply))
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.add_handler(ChatJoinRequestHandler(join_request_handler))
    app.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            content_handler
        )
    )

    print("🟢 VIP BOT ONLINE 🟢")
    app.run_polling()


if __name__ == "__main__":
    main()
