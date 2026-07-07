import os
import time
import asyncio
import logging
import threading
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, HTTPServer

from google import genai
from dotenv import load_dotenv
from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, ChatJoinRequestHandler
)

# ============================================================
# CONFIGURATION
# ============================================================
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

OWNER_NAME = "@PREMGUPTA2M"
CHANNEL_LINK = "https://t.me/your_channel_link"
GROUP_LINK = "https://t.me/your_group_link"

# Models to try in order. If the first one 404s / fails, bot auto-falls back
# to the next one instead of dying. Update this list any time Google
# deprecates a model — no other code changes needed.
GEMINI_MODEL_CHAIN = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-001"]

MAX_WARNINGS = 3          # Auto-mute after this many warnings
FLOOD_MSG_LIMIT = 6       # Max messages...
FLOOD_WINDOW_SECONDS = 8  # ...within this many seconds = flood
FLOOD_MUTE_MINUTES = 10

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("PrimeXAssistant")

# ============================================================
# AI CLIENT SETUP
# ============================================================
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# ============================================================
# GLOBAL STATE (in-memory — swap for a DB later if you want persistence
# across restarts)
# ============================================================
group_rules = defaultdict(lambda: "Group ke rules abhi set nahi hain.")
active_members = defaultdict(dict)        # chat_id -> {user_id: first_name}
warnings = defaultdict(lambda: defaultdict(int))  # chat_id -> {user_id: count}
message_log = defaultdict(lambda: defaultdict(lambda: deque(maxlen=FLOOD_MSG_LIMIT + 1)))
known_chats = set()                       # for /broadcast
approval_settings = defaultdict(lambda: {"enabled": True, "delay": 5})  # chat_id -> settings

TAG_BATCH_SIZE = 5           # mentions per tag message (keeps it readable)
TAG_BATCH_DELAY = 1.5        # seconds between batches (avoid Telegram rate limits)


# ============================================================
# DUMMY WEB SERVER (Render health check)
# ============================================================
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is successfully running on Render!")

    def log_message(self, format, *args):
        pass  # silence noisy default HTTP logs


def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), DummyHandler)
    server.serve_forever()


# ============================================================
# HELPERS
# ============================================================
def is_admin(update: Update) -> bool:
    if not update.message:
        return False
    if update.message.from_user and update.message.from_user.id == ADMIN_ID:
        return True
    if update.message.sender_chat and update.message.sender_chat.id == update.message.chat_id:
        return True
    return False


async def is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Checks Telegram-level admin status too (not just bot owner)."""
    if is_admin(update):
        return True
    try:
        member = await context.bot.get_chat_member(update.message.chat_id, update.message.from_user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def get_target_user(update: Update):
    """Gets the user a moderation command should target (via reply)."""
    if update.message.reply_to_message:
        return update.message.reply_to_message.from_user
    return None


# ============================================================
# AI REPLY WITH FALLBACK CHAIN
# ============================================================
async def get_ai_reply(prompt: str) -> str:
    if not ai_client:
        return "❌ Bhai, GEMINI_API_KEY set nahi hai!"

    system_prompt = (
        "You are a casual telegram group member. Reply in short Hinglish. "
        "Tone should be friendly. Do not act like an AI."
    )

    last_error = None
    for model_name in GEMINI_MODEL_CHAIN:
        try:
            def fetch_response():
                response = ai_client.models.generate_content(
                    model=model_name,
                    contents=system_prompt + "\n\nUser: " + prompt
                )
                return response.text

            reply = await asyncio.to_thread(fetch_response)
            if reply:
                return reply
        except Exception as e:
            last_error = e
            log.warning(f"Model '{model_name}' failed: {e}. Trying next fallback...")
            continue

    log.error(f"All Gemini models failed. Last error: {last_error}")
    return "❌ AI abhi thoda busy hai, thodi der me try karo!"


# ============================================================
# START MENU
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_name = update.effective_user.first_name if update.effective_user else "Admin"
        text = (
            f"Hello {user_name}! 👋\n\n"
            "Main ek advanced AI Group Manager bot hoon.\n"
            "Type /help to see everything I can do."
        )
        keyboard = [
            [InlineKeyboardButton("📢 Channel", url=CHANNEL_LINK),
             InlineKeyboardButton("👥 Group", url=GROUP_LINK)],
            [InlineKeyboardButton("👨‍💻 Owner / Admin", callback_data="owner_info")]
        ]
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        log.error(f"/start failed: {e}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*🤖 Prime X Assistant — Commands*\n\n"
        "*General*\n"
        "• /start — Welcome menu\n"
        "• /rules — Show group rules\n"
        "• /stats — Group activity stats\n\n"
        "*Admin only*\n"
        "• /setrules <text> — Set group rules\n"
        "• /autoapprove on|off — Toggle join-request auto-approval\n"
        "• /setdelay <seconds> — Join-request auto-approve delay\n"
        "• /tagall <msg> — Tag all known members (batched)\n"
        "• /tagadmins <msg> — Tag all group admins\n"
        "• /call (reply) or /call @username <msg> — Ping one member\n"
        "• /warn (reply to user) — Issue a warning\n"
        "• /unwarn (reply to user) — Remove a warning\n"
        "• /mute (reply) [minutes] — Mute a user\n"
        "• /unmute (reply) — Unmute a user\n"
        "• /kick (reply) — Remove user from group\n"
        "• /ban (reply) — Ban user from group\n"
        "• /unban <user_id> — Unban a user\n"
        "• /pin (reply) — Pin a message\n"
        "• /broadcast <msg> — Send message to all known groups\n\n"
        f"Owner: {OWNER_NAME}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "owner_info":
        await query.edit_message_text(f"Mere owner **{OWNER_NAME}** hain.", parse_mode="Markdown")


# ============================================================
# GROUP CONFIG COMMANDS
# ============================================================
async def set_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    chat_id = update.message.chat_id
    try:
        new_time = int(context.args[0])
        if new_time < 0:
            raise ValueError
        approval_settings[chat_id]["delay"] = new_time
        await update.message.reply_text(
            f"✅ Auto-approval delay for this group ab **{new_time} second(s)** set ho gaya.",
            parse_mode="Markdown"
        )
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Usage: `/setdelay <seconds>`\nExample: `/setdelay 10`", parse_mode="Markdown")


async def toggle_autoapprove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    chat_id = update.message.chat_id
    if not context.args or context.args[0].lower() not in ("on", "off"):
        current = "ON ✅" if approval_settings[chat_id]["enabled"] else "OFF ❌"
        await update.message.reply_text(
            f"⚙️ Auto-approval is currently **{current}** for this group.\n"
            "Usage: `/autoapprove on` or `/autoapprove off`",
            parse_mode="Markdown"
        )
        return
    approval_settings[chat_id]["enabled"] = context.args[0].lower() == "on"
    state = "enabled ✅ — join requests will auto-approve after the delay" if approval_settings[chat_id]["enabled"] \
        else "disabled ❌ — you'll get a DM to approve manually"
    await update.message.reply_text(f"⚙️ Auto-approval {state}.")


async def set_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    chat_id = update.message.chat_id
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("⚠️ Usage: `/setrules <rules text>`", parse_mode="Markdown")
        return
    group_rules[chat_id] = text
    await update.message.reply_text("✅ Rules updated!")


async def show_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    await update.message.reply_text(f"📜 *Group Rules:*\n\n{group_rules[chat_id]}", parse_mode="Markdown")


async def tag_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    chat_id = update.message.chat_id
    if chat_id not in active_members or not active_members[chat_id]:
        await update.message.reply_text("⏳ Abhi members ka data load nahi hua hai. Thodi der group mein messages aane dein.")
        return

    custom_msg = " ".join(context.args) or "📢 Dhyan dein!"
    members = list(active_members[chat_id].items())
    total_batches = (len(members) + TAG_BATCH_SIZE - 1) // TAG_BATCH_SIZE

    status = await update.message.reply_text(
        f"📨 Tagging {len(members)} member(s) in {total_batches} batch(es)..."
    )

    for i in range(0, len(members), TAG_BATCH_SIZE):
        batch = members[i:i + TAG_BATCH_SIZE]
        mentions = " ".join(f"[{fname}](tg://user?id={uid})" for uid, fname in batch)
        text_to_send = f"{custom_msg}\n\n{mentions}"
        try:
            if update.message.reply_to_message:
                await update.message.reply_to_message.reply_text(text_to_send, parse_mode="Markdown")
            else:
                await context.bot.send_message(chat_id=chat_id, text=text_to_send, parse_mode="Markdown")
        except Exception as e:
            log.warning(f"Tag batch failed: {e}")
        await asyncio.sleep(TAG_BATCH_DELAY)

    try:
        await status.delete()
    except Exception:
        pass


async def tag_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tags all human admins of the group in one message."""
    if not await is_group_admin(update, context):
        return
    chat_id = update.message.chat_id
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Could not fetch admins: {e}")
        return

    mentions = " ".join(
        f"[{a.user.first_name}](tg://user?id={a.user.id})" for a in admins if not a.user.is_bot
    )
    if not mentions:
        await update.message.reply_text("⚠️ No human admins found to tag.")
        return

    custom_msg = " ".join(context.args) or "📢 Admins, dhyan dein!"
    await context.bot.send_message(chat_id=chat_id, text=f"{custom_msg}\n\n{mentions}", parse_mode="Markdown")


async def call_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pings one specific member — reply to their message, or use /call @username <msg>."""
    if not await is_group_admin(update, context):
        return
    chat_id = update.message.chat_id

    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        custom_msg = " ".join(context.args) or "📢 Suniye!"
        text = f"{custom_msg} [{target.first_name}](tg://user?id={target.id})"
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        return

    if context.args:
        username = context.args[0].lstrip("@")
        custom_msg = " ".join(context.args[1:]) or "📢 Suniye!"
        # No stored user_id for a bare @username, so send a plain @mention —
        # this still triggers a notification for that user if they're in the group.
        await context.bot.send_message(chat_id=chat_id, text=f"{custom_msg} @{username}")
        return

    await update.message.reply_text(
        "⚠️ Reply to a user's message with `/call <optional message>`, "
        "or use `/call @username <message>`.",
        parse_mode="Markdown"
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    settings = approval_settings[chat_id]
    member_count = len(active_members.get(chat_id, {}))
    warned_count = sum(1 for c in warnings.get(chat_id, {}).values() if c > 0)
    auto_status = "ON ✅" if settings["enabled"] else "OFF ❌"
    text = (
        "📊 *Group Stats*\n\n"
        f"• Tracked active members: {member_count}\n"
        f"• Users with warnings: {warned_count}\n"
        f"• Auto-approval: {auto_status}\n"
        f"• Join-request delay: {settings['delay']}s\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ============================================================
# MODERATION COMMANDS
# ============================================================
async def warn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    target = get_target_user(update)
    if not target:
        await update.message.reply_text("⚠️ Reply to a user's message with /warn.")
        return

    chat_id = update.message.chat_id
    warnings[chat_id][target.id] += 1
    count = warnings[chat_id][target.id]

    if count >= MAX_WARNINGS:
        try:
            await context.bot.restrict_chat_member(
                chat_id, target.id,
                permissions=ChatPermissions(can_send_messages=False)
            )
            await update.message.reply_text(
                f"🔇 {target.first_name} ko {MAX_WARNINGS} warnings ke baad mute kar diya gaya hai."
            )
            warnings[chat_id][target.id] = 0
        except Exception as e:
            await update.message.reply_text(f"⚠️ Mute failed: {e}")
    else:
        await update.message.reply_text(
            f"⚠️ {target.first_name} ko warning {count}/{MAX_WARNINGS} mil gayi hai."
        )


async def unwarn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    target = get_target_user(update)
    if not target:
        await update.message.reply_text("⚠️ Reply to a user's message with /unwarn.")
        return
    chat_id = update.message.chat_id
    if warnings[chat_id][target.id] > 0:
        warnings[chat_id][target.id] -= 1
    await update.message.reply_text(f"✅ Warning removed. {target.first_name} now has {warnings[chat_id][target.id]} warning(s).")


async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    target = get_target_user(update)
    if not target:
        await update.message.reply_text("⚠️ Reply to a user's message with /mute [minutes].")
        return

    minutes = None
    if context.args:
        try:
            minutes = int(context.args[0])
        except ValueError:
            pass

    try:
        until_date = None
        if minutes:
            until_date = int(time.time()) + (minutes * 60)
        await context.bot.restrict_chat_member(
            update.message.chat_id, target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until_date
        )
        duration_text = f"for {minutes} minute(s)" if minutes else "indefinitely"
        await update.message.reply_text(f"🔇 {target.first_name} muted {duration_text}.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Mute failed: {e}")


async def unmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    target = get_target_user(update)
    if not target:
        await update.message.reply_text("⚠️ Reply to a user's message with /unmute.")
        return
    try:
        await context.bot.restrict_chat_member(
            update.message.chat_id, target.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True
            )
        )
        await update.message.reply_text(f"🔊 {target.first_name} unmuted.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Unmute failed: {e}")


async def kick_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    target = get_target_user(update)
    if not target:
        await update.message.reply_text("⚠️ Reply to a user's message with /kick.")
        return
    try:
        chat_id = update.message.chat_id
        await context.bot.ban_chat_member(chat_id, target.id)
        await context.bot.unban_chat_member(chat_id, target.id)  # kick, not permanent ban
        await update.message.reply_text(f"👋 {target.first_name} has been kicked.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Kick failed: {e}")


async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    target = get_target_user(update)
    if not target:
        await update.message.reply_text("⚠️ Reply to a user's message with /ban.")
        return
    try:
        await context.bot.ban_chat_member(update.message.chat_id, target.id)
        await update.message.reply_text(f"🚫 {target.first_name} has been banned.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ban failed: {e}")


async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/unban <user_id>`", parse_mode="Markdown")
        return
    try:
        user_id = int(context.args[0])
        await context.bot.unban_chat_member(update.message.chat_id, user_id)
        await update.message.reply_text(f"✅ User {user_id} unbanned.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Unban failed: {e}")


async def pin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("⚠️ Reply to the message you want to pin with /pin.")
        return
    try:
        await context.bot.pin_chat_message(
            update.message.chat_id,
            update.message.reply_to_message.message_id
        )
        await update.message.reply_text("📌 Message pinned.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Pin failed: {e}")


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.from_user or update.message.from_user.id != ADMIN_ID:
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("⚠️ Usage: `/broadcast <message>`", parse_mode="Markdown")
        return
    sent, failed = 0, 0
    for chat_id in list(known_chats):
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"📢 {text}")
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"✅ Broadcast sent to {sent} chat(s). Failed: {failed}.")


# ============================================================
# JOIN REQUEST AUTO-APPROVAL
# ============================================================
async def join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.chat_join_request.chat.id
    settings = approval_settings[chat_id]
    requester = update.chat_join_request.from_user

    if not settings["enabled"]:
        # Auto-approval is off for this group — ping the owner to approve manually.
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"🔔 *New join request*\n"
                    f"Group: {update.chat_join_request.chat.title}\n"
                    f"User: {requester.first_name} (@{requester.username or 'no_username'})\n\n"
                    f"Auto-approval is OFF for this group — approve manually from group member requests."
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            log.warning(f"Admin notify for join request failed: {e}")
        return

    await asyncio.sleep(settings["delay"])
    try:
        await update.chat_join_request.approve()
        await context.bot.send_message(
            chat_id=requester.id,
            text="Aapki group join request accept ho gayi hai. 🎉"
        )
    except Exception as e:
        log.warning(f"Join request approval failed: {e}")


# ============================================================
# ANTI-FLOOD CHECK
# ============================================================
async def check_flood(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if the user was flagged/muted for flooding."""
    chat_id = update.message.chat_id
    user_id = update.message.from_user.id
    now = time.time()

    log_deque = message_log[chat_id][user_id]
    log_deque.append(now)

    if len(log_deque) >= FLOOD_MSG_LIMIT and (now - log_deque[0]) <= FLOOD_WINDOW_SECONDS:
        try:
            until_date = int(time.time()) + (FLOOD_MUTE_MINUTES * 60)
            await context.bot.restrict_chat_member(
                chat_id, user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_date
            )
            await update.message.reply_text(
                f"🚨 {update.message.from_user.first_name} flood kar rahe the, "
                f"{FLOOD_MUTE_MINUTES} minute ke liye mute kar diya gaya."
            )
            log_deque.clear()
            return True
        except Exception as e:
            log.warning(f"Flood mute failed: {e}")
    return False


# ============================================================
# MAIN MESSAGE HANDLER
# ============================================================
async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.lower()
    chat_type = update.message.chat.type
    user = update.message.from_user
    chat_id = update.message.chat_id
    known_chats.add(chat_id)

    # Track active members for /tagall
    if chat_type in ["group", "supergroup"] and user.id != ADMIN_ID and not user.is_bot:
        active_members[chat_id][user.id] = user.first_name

    # Anti-flood (skip for admins)
    if chat_type in ["group", "supergroup"] and not await is_group_admin(update, context):
        if await check_flood(update, context):
            return

    # Anti-link
    if chat_type in ["group", "supergroup"] and not await is_group_admin(update, context):
        if any(link in text for link in ["http://", "https://", "t.me/", ".com", ".in"]):
            try:
                await update.message.delete()
                warning = await update.message.reply_text(f"⚠️ {user.first_name}, yahan links allowed nahi hai!")
                await asyncio.sleep(5)
                await warning.delete()
            except Exception as e:
                log.warning(f"Anti-link cleanup failed: {e}")
            return

    # Owner query
    if any(keyword in text for keyword in ["owner kon", "admin kon", "malik kon"]):
        await update.message.reply_text(f"Mere owner **{OWNER_NAME}** hain. 😎", parse_mode="Markdown")
        return

    # AI trigger logic
    bot_username = context.bot.username.lower() if context.bot.username else ""
    is_reply_to_bot = (
        update.message.reply_to_message
        and update.message.reply_to_message.from_user.id == context.bot.id
    )

    if chat_type == "private" or (chat_type in ["group", "supergroup"] and (bot_username in text or is_reply_to_bot)):
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            ai_reply = await get_ai_reply(update.message.text)
            await update.message.reply_text(ai_reply)
        except Exception as e:
            log.error(f"AI reply failed: {e}")


# ============================================================
# STARTUP VALIDATION
# ============================================================
def validate_config():
    problems = []
    if not TOKEN:
        problems.append("BOT_TOKEN is missing.")
    if not ADMIN_ID:
        problems.append("ADMIN_ID is missing or 0.")
    if not GEMINI_API_KEY:
        problems.append("GEMINI_API_KEY is missing — AI chat will be disabled.")
    for p in problems:
        log.warning(f"CONFIG WARNING: {p}")


# ============================================================
# MAIN
# ============================================================
def main():
    validate_config()
    threading.Thread(target=run_dummy_server, daemon=True).start()

    app = Application.builder().token(TOKEN).build()

    # General
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("rules", show_rules))
    app.add_handler(CommandHandler("stats", stats))

    # Admin config
    app.add_handler(CommandHandler("setrules", set_rules))
    app.add_handler(CommandHandler("autoapprove", toggle_autoapprove))
    app.add_handler(CommandHandler("setdelay", set_delay))
    app.add_handler(CommandHandler("tagall", tag_all))
    app.add_handler(CommandHandler("tagadmins", tag_admins))
    app.add_handler(CommandHandler("call", call_user))
    app.add_handler(CommandHandler("broadcast", broadcast))

    # Moderation
    app.add_handler(CommandHandler("warn", warn_user))
    app.add_handler(CommandHandler("unwarn", unwarn_user))
    app.add_handler(CommandHandler("mute", mute_user))
    app.add_handler(CommandHandler("unmute", unmute_user))
    app.add_handler(CommandHandler("kick", kick_user))
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("unban", unban_user))
    app.add_handler(CommandHandler("pin", pin_message))

    # Callbacks & events
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(ChatJoinRequestHandler(join_request))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))

    log.info("🚀 Prime X Assistant deployed successfully!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
