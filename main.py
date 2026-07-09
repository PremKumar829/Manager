import os
import time
import random
import asyncio
import logging
import tempfile
import threading
import requests
from datetime import datetime, timedelta, time as dt_time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, HTTPServer

from google import genai
from openai import OpenAI  # used for NVIDIA fallback (OpenAI-compatible API)
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
CHANNEL_LINK = "https://t.me/+Gouc7PsDosk4MTRl"
GROUP_LINK = "https://t.me/+rSqVXbRig4BjOTc1"

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

# NVIDIA NIM fallback — kicks in ONLY if every Gemini model above fails (rate limit, quota, outage).
# Get a free key at https://build.nvidia.com — pick any chat model and copy its exact model ID.
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL", "meta/llama-3.3-70b-instruct")
nvidia_client = OpenAI(api_key=NVIDIA_API_KEY, base_url="https://integrate.api.nvidia.com/v1") if NVIDIA_API_KEY else None

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
chat_history = defaultdict(lambda: deque(maxlen=30))  # chat_id -> recent messages, used so AI matches group's style
custom_welcome = {}                        # chat_id -> welcome template (use {name} placeholder)
bad_words = defaultdict(set)               # chat_id -> set of banned words/phrases
message_count = defaultdict(lambda: defaultdict(int))  # chat_id -> {user_id: total messages} for leaderboard
slow_mode = defaultdict(int)               # chat_id -> seconds between messages per user (0 = off)
last_message_time = defaultdict(dict)      # chat_id -> {user_id: last message timestamp}
link_whitelist = defaultdict(set)          # chat_id -> extra allowed link substrings
sticker_replies = defaultdict(dict)        # chat_id -> {keyword: sticker_file_id}
discord_webhooks = {}                      # chat_id -> Discord webhook URL for relaying messages
channel_links = defaultdict(set)           # channel_chat_id -> set of group_chat_ids to auto-forward into

persona = {}                               # chat_id -> custom bot personality description
coins = defaultdict(lambda: defaultdict(int))     # chat_id -> {user_id: coin balance}
custom_titles = defaultdict(dict)          # chat_id -> {user_id: purchased title}
streaks = defaultdict(dict)                # chat_id -> {user_id: {"count": int, "last_date": date}}
automod_enabled = defaultdict(bool)        # chat_id -> smart AI auto-mod on/off
trivia_sessions = {}                       # chat_id -> active trivia round state

SHOP_ITEMS = {"title": 200, "shoutout": 50}
COINS_PER_MESSAGE = 1
TRIVIA_WIN_COINS = 15
STREAK_MILESTONE_BONUS = 50
STREAK_MILESTONE_EVERY = 7

birthdays = defaultdict(dict)                       # chat_id -> {user_id: "DD-MM"}
badges = defaultdict(lambda: defaultdict(set))       # chat_id -> {user_id: {badge_names}}
first_seen = defaultdict(dict)                       # chat_id -> {user_id: datetime} — for time-based badges
MESSAGE_MILESTONES = [100, 500, 1000, 5000]
TIME_MILESTONES = {
    7: "🌱 1 Week Member",
    30: "🌿 1 Month Member",
    90: "🌳 3 Month Member",
    365: "🏆 1 Year Member",
}
BADGE_BONUS_COINS = 25
BIRTHDAY_BONUS_COINS = 50

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
bot_admins = set()  # user_ids the owner has promoted — get admin power in EVERY group, not just one


def is_owner(user_id: int) -> bool:
    return user_id == ADMIN_ID


def is_bot_admin_id(user_id: int) -> bool:
    return user_id == ADMIN_ID or user_id in bot_admins


def is_admin(update: Update) -> bool:
    if not update.message:
        return False
    if update.message.from_user and is_bot_admin_id(update.message.from_user.id):
        return True
    if update.message.sender_chat and update.message.sender_chat.id == update.message.chat_id:
        return True
    return False


async def is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True if the sender is the bot owner, a promoted bot-admin (any group),
    OR a native Telegram admin of this specific group."""
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
# BOT ADMIN PANEL (owner-only management of global bot-admins)
# ============================================================
async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.message.from_user.id):
        await update.message.reply_text("❌ Sirf bot owner naye admin bana sakta hai.")
        return
    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: `/addadmin <telegram_user_id>`\n"
            "User ka numeric ID @userinfobot se nikaal sakte ho.",
            parse_mode="Markdown"
        )
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ User ID numeric hona chahiye, e.g. `123456789`.", parse_mode="Markdown")
        return
    bot_admins.add(uid)
    await update.message.reply_text(
        f"✅ User `{uid}` ab bot admin hai — sabhi groups mein admin commands use kar sakta hai.",
        parse_mode="Markdown"
    )


async def remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.message.from_user.id):
        await update.message.reply_text("❌ Sirf bot owner admin remove kar sakta hai.")
        return
    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/removeadmin <telegram_user_id>`", parse_mode="Markdown")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        return
    bot_admins.discard(uid)
    await update.message.reply_text(f"✅ User `{uid}` bot admins se remove kar diya gaya.", parse_mode="Markdown")


async def list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_admin_id(update.message.from_user.id):
        return
    text = f"👑 *Owner:* `{ADMIN_ID}`\n\n"
    if bot_admins:
        text += "🛡️ *Bot Admins:*\n" + "\n".join(f"• `{uid}`" for uid in bot_admins)
    else:
        text += "🛡️ Koi extra bot admin abhi set nahi hai."
    await update.message.reply_text(text, parse_mode="Markdown")


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_admin_id(update.message.from_user.id):
        return
    text = (
        "🛠️ *Bot Admin Panel*\n\n"
        f"👑 Owner ID: `{ADMIN_ID}`\n"
        f"🛡️ Bot Admins: {len(bot_admins)}\n"
        f"💬 Known chats (groups + DMs): {len(known_chats)}\n\n"
        "*Owner-only commands:*\n"
        "`/addadmin <id>` — promote a user to global bot-admin\n"
        "`/removeadmin <id>` — demote them\n"
        "`/listadmins` — see current admins\n"
        "`/broadcast <msg>` — message every known chat\n"
        "`/digest`, `/testai` — diagnostics\n\n"
        "_Bot admins get full admin power in EVERY group the bot is in — "
        "not just where they're a Telegram admin._"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ============================================================
# AI REPLY WITH FALLBACK CHAIN
# ============================================================
async def get_ai_reply(prompt: str, style_context: list = None, persona_text: str = None) -> str:
    if not ai_client:
        return "❌ Bhai, GEMINI_API_KEY set nahi hai!"

    style_hint = ""
    if style_context:
        sample = "\n".join(style_context[-10:])
        style_hint = (
            "\n\nHere are some recent messages from this group/chat — match their "
            "language style, tone, and slang when you reply (don't quote them back, "
            "just absorb the vibe):\n" + sample
        )

    base_persona = persona_text or (
        "You are a casual telegram group member. Reply in short Hinglish. "
        "Tone should be friendly. Do not act like an AI."
    )
    system_prompt = base_persona + style_hint

    last_error = None
    for model_name in GEMINI_MODEL_CHAIN:
        for attempt in range(2):  # one retry per model for transient (429/503) errors
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
                log.warning(f"Model '{model_name}' attempt {attempt + 1} failed: {repr(e)}")
                if attempt == 0:
                    await asyncio.sleep(2)  # brief backoff before retrying same model
                continue

    log.error(f"All Gemini models failed. Last error: {repr(last_error)}")

    # Last resort: try NVIDIA (only if configured)
    if nvidia_client:
        try:
            def fetch_nvidia():
                completion = nvidia_client.chat.completions.create(
                    model=NVIDIA_MODEL,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt}
                    ],
                    max_tokens=512,
                )
                return completion.choices[0].message.content

            reply = await asyncio.to_thread(fetch_nvidia)
            if reply:
                log.info("Gemini exhausted — served this reply via NVIDIA fallback.")
                return reply
        except Exception as e:
            log.error(f"NVIDIA fallback also failed: {repr(e)}")

    return "❌ AI abhi thoda busy hai, thodi der me try karo! (Run /testai to see the exact error)"


async def test_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only diagnostic — shows the exact error for each model in the fallback
    chain, so you can tell if it's an invalid key, quota limit, or missing model access."""
    if not is_admin(update):
        await update.message.reply_text(
            "❌ Ye command sirf bot owner/admin use kar sakta hai. "
            "Apna ADMIN_ID .env mein double-check karo (@userinfobot se apna exact numeric ID lo)."
        )
        return
    if not ai_client:
        await update.message.reply_text("❌ GEMINI_API_KEY is missing or not loaded from .env.")
        return

    await update.message.reply_text("🔧 Testing all models, ek second...")
    results = []
    for model_name in GEMINI_MODEL_CHAIN:
        try:
            def fetch():
                r = ai_client.models.generate_content(model=model_name, contents="Say hi in one word.")
                return r.text
            reply = await asyncio.to_thread(fetch)
            results.append(f"✅ {model_name} → {reply.strip()[:60]}")
        except Exception as e:
            results.append(f"❌ {model_name} → {repr(e)[:150]}")

    if nvidia_client:
        try:
            def fetch_nvidia():
                completion = nvidia_client.chat.completions.create(
                    model=NVIDIA_MODEL,
                    messages=[{"role": "user", "content": "Say hi in one word."}],
                    max_tokens=20,
                )
                return completion.choices[0].message.content
            reply = await asyncio.to_thread(fetch_nvidia)
            results.append(f"✅ NVIDIA ({NVIDIA_MODEL}) → {reply.strip()[:60]}")
        except Exception as e:
            results.append(f"❌ NVIDIA ({NVIDIA_MODEL}) → {repr(e)[:150]}")
    else:
        results.append("⚪ NVIDIA fallback not configured (NVIDIA_API_KEY missing)")

    await update.message.reply_text("🔧 *AI Diagnostic Results:*\n\n" + "\n\n".join(results), parse_mode="Markdown")


# ============================================================
# PERSONA CUSTOMIZATION
# ============================================================
async def set_persona(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text(
            "⚠️ Usage: `/setpersona <description>`\n"
            "Example: `/setpersona ek witty sarcastic dost jo sabko roast karta hai but pyaar se`",
            parse_mode="Markdown"
        )
        return
    persona[update.message.chat_id] = text
    await update.message.reply_text("✅ Bot persona updated for this group!")


async def reset_persona(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    persona.pop(update.message.chat_id, None)
    await update.message.reply_text("✅ Persona reset to default (friendly Hinglish group member).")


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
        "• /stats — Group activity stats\n"
        "• /topmembers — Most active members leaderboard\n"
        "• /profile — Your coins, streak, and title\n"
        "• /balance — Check your coin balance\n"
        "• /shop, /buy <item> <text> — Spend coins on titles/shoutouts\n"
        "• /mystreak — Your daily activity streak\n"
        "• /setbirthday DD-MM, /mybirthday, /removebirthday — Birthday tracker 🎂\n"
        "• /mybadges — See your unlocked achievement badges 🏅\n"
        "• /trivia — Start a multiplayer trivia round 🎯\n"
        "• /trivialeaderboard — Trivia win rankings\n"
        "• /tictactoe (reply to opponent) — Challenge someone to 1v1 ❌⭕\n"
        "• /wordchain — Start a group word chain game 🔗\n"
        "• /mathblitz — Fast math race, first correct answer wins 🧮\n"
        "• Send a voice note (or reply to bot with one) — I'll transcribe it 🎙️\n\n"
        "*Admin only*\n"
        "• /testai — Diagnose AI errors (shows exact API error per model)\n"
        "• /endwordchain — Stop an active word chain\n"
        "• /adminpanel — Bot admin overview (owner + bot admins)\n"
        "• /addadmin, /removeadmin, /listadmins <id> — Owner: manage global bot admins\n"
        "• /setrules <text> — Set group rules\n"
        "• /setwelcome <msg> — Custom welcome message (use {name})\n"
        "• /setpersona <description> / /resetpersona — Customize AI personality\n"
        "• /automod on|off — Smart AI toxicity detection\n"
        "• /digest — Preview today's AI chat summary now\n"
        "• /autoapprove on|off — Toggle join-request auto-approval\n"
        "• /setdelay <seconds> — Join-request auto-approve delay\n"
        "• /slowmode <seconds> — Limit messages per user (0 = off)\n"
        "• /addbadword, /removebadword, /badwords — Manage word filter\n"
        "• /allowlink, /disallowlink, /listlinks — Manage link whitelist\n"
        "• /addstickerreact (reply to sticker) <keyword> — Auto-react with sticker\n"
        "• /removestickerreact <keyword> — Remove sticker reaction\n"
        "• /setdiscordwebhook <url> / /removediscordwebhook — Mirror chat to Discord\n"
        "• /linkchannel <channel_id> / /unlinkchannel — Auto-forward channel posts here\n"
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
async def apply_warning(chat_id: int, target_user, context: ContextTypes.DEFAULT_TYPE, reason: str = "") -> str:
    """Shared warning logic used by /warn and automated filters (bad words, etc)."""
    warnings[chat_id][target_user.id] += 1
    count = warnings[chat_id][target_user.id]
    suffix = f" ({reason})" if reason else ""

    if count >= MAX_WARNINGS:
        try:
            await context.bot.restrict_chat_member(
                chat_id, target_user.id,
                permissions=ChatPermissions(can_send_messages=False)
            )
            warnings[chat_id][target_user.id] = 0
            return f"🔇 {target_user.first_name} ko {MAX_WARNINGS} warnings ke baad mute kar diya gaya hai.{suffix}"
        except Exception as e:
            return f"⚠️ Mute failed: {e}"
    return f"⚠️ {target_user.first_name} ko warning {count}/{MAX_WARNINGS} mil gayi hai.{suffix}"


async def warn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    target = get_target_user(update)
    if not target:
        await update.message.reply_text("⚠️ Reply to a user's message with /warn.")
        return

    chat_id = update.message.chat_id
    result = await apply_warning(chat_id, target, context)
    await update.message.reply_text(result)


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


# ============================================================
# BAD WORD FILTER
# ============================================================
async def add_bad_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/addbadword <word>`", parse_mode="Markdown")
        return
    chat_id = update.message.chat_id
    word = " ".join(context.args).lower()
    bad_words[chat_id].add(word)
    await update.message.reply_text(f"✅ Added \"{word}\" to the filter list.")


async def remove_bad_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/removebadword <word>`", parse_mode="Markdown")
        return
    chat_id = update.message.chat_id
    word = " ".join(context.args).lower()
    bad_words[chat_id].discard(word)
    await update.message.reply_text(f"✅ Removed \"{word}\" from the filter list.")


async def list_bad_words(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    chat_id = update.message.chat_id
    words = bad_words.get(chat_id, set())
    if not words:
        await update.message.reply_text("📝 No filtered words set for this group.")
        return
    await update.message.reply_text("📝 *Filtered words:*\n" + ", ".join(sorted(words)), parse_mode="Markdown")


# ============================================================
# SLOW MODE
# ============================================================
async def set_slowmode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    chat_id = update.message.chat_id
    try:
        seconds = int(context.args[0])
        if seconds < 0:
            raise ValueError
        slow_mode[chat_id] = seconds
        if seconds == 0:
            await update.message.reply_text("✅ Slow mode disabled.")
        else:
            await update.message.reply_text(f"🐢 Slow mode set: 1 message every {seconds} second(s) per user.")
    except (IndexError, ValueError):
        await update.message.reply_text(
            "⚠️ Usage: `/slowmode <seconds>` (0 to disable)\nExample: `/slowmode 15`",
            parse_mode="Markdown"
        )


# ============================================================
# WELCOME MESSAGE
# ============================================================
async def set_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    chat_id = update.message.chat_id
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text(
            "⚠️ Usage: `/setwelcome <message>` — use `{name}` for the member's name.\n"
            "Example: `/setwelcome Welcome {name}! Read the pinned rules 🎉`",
            parse_mode="Markdown"
        )
        return
    custom_welcome[chat_id] = text
    await update.message.reply_text("✅ Welcome message updated!")


async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        template = custom_welcome.get(
            chat_id,
            "🎉 Welcome {name}! Glad to have you here.\n\nType /rules to see the group rules, and /help for what I can do."
        )
        text = template.replace("{name}", member.first_name)
        try:
            await update.message.reply_text(text)
        except Exception as e:
            log.warning(f"Welcome message failed: {e}")


# ============================================================
# LEADERBOARD
# ============================================================
async def top_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    counts = message_count.get(chat_id, {})
    if not counts:
        await update.message.reply_text("📊 Abhi koi activity data nahi hai.")
        return
    ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]
    lines = ["🏆 *Top Active Members*\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, count) in enumerate(ranked):
        name = active_members.get(chat_id, {}).get(uid, f"User {uid}")
        marker = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{marker} {name} — {count} messages")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ============================================================
# LINK WHITELIST
# ============================================================
async def allow_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/allowlink <domain or t.me/username>`", parse_mode="Markdown")
        return
    chat_id = update.message.chat_id
    entry = context.args[0].lower()
    link_whitelist[chat_id].add(entry)
    await update.message.reply_text(f"✅ \"{entry}\" is now allowed in this group.")


async def disallow_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/disallowlink <domain or t.me/username>`", parse_mode="Markdown")
        return
    chat_id = update.message.chat_id
    entry = context.args[0].lower()
    link_whitelist[chat_id].discard(entry)
    await update.message.reply_text(f"✅ \"{entry}\" removed from the allow-list.")


async def list_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    chat_id = update.message.chat_id
    entries = link_whitelist.get(chat_id, set())
    text = "🔗 *Always-allowed:* group/channel's own links\n"
    if entries:
        text += "*Extra whitelisted:*\n" + ", ".join(sorted(entries))
    else:
        text += "No extra whitelisted links."
    await update.message.reply_text(text, parse_mode="Markdown")


# ============================================================
# VOICE MESSAGE TRANSCRIPTION
# ============================================================
async def transcribe_voice(file_path: str) -> str:
    if not ai_client:
        return "❌ GEMINI_API_KEY set nahi hai, transcription possible nahi."

    def fetch():
        uploaded = ai_client.files.upload(file=file_path)
        response = ai_client.models.generate_content(
            model=GEMINI_MODEL_CHAIN[0],
            contents=[
                "Transcribe this voice message. Reply with ONLY the transcription "
                "in the original spoken language, no extra commentary.",
                uploaded
            ]
        )
        return response.text

    try:
        return await asyncio.to_thread(fetch)
    except Exception as e:
        log.warning(f"Voice transcription failed: {e}")
        return f"❌ Transcription failed: {e}"


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.voice:
        return
    chat_type = update.message.chat.type
    is_reply_to_bot = (
        update.message.reply_to_message
        and update.message.reply_to_message.from_user
        and update.message.reply_to_message.from_user.id == context.bot.id
    )
    # In groups only transcribe when the voice note replies to the bot;
    # in private chat, always transcribe.
    if chat_type in ["group", "supergroup"] and not is_reply_to_bot:
        return

    tmp_path = None
    try:
        await context.bot.send_chat_action(chat_id=update.message.chat_id, action="typing")
        voice_file = await update.message.voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await voice_file.download_to_drive(tmp_path)
        transcription = await transcribe_voice(tmp_path)
        await update.message.reply_text(f"🎙️ Transcription:\n{transcription}")
    except Exception as e:
        log.error(f"Voice handling failed: {e}")
        await update.message.reply_text("❌ Voice message process nahi ho paya.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


# ============================================================
# STICKER AUTO-REACT
# ============================================================
async def add_sticker_react(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    if not context.args or not update.message.reply_to_message or not update.message.reply_to_message.sticker:
        await update.message.reply_text(
            "⚠️ Reply to a sticker with `/addstickerreact <keyword>`",
            parse_mode="Markdown"
        )
        return
    chat_id = update.message.chat_id
    keyword = " ".join(context.args).lower()
    file_id = update.message.reply_to_message.sticker.file_id
    sticker_replies[chat_id][keyword] = file_id
    await update.message.reply_text(f"✅ Ab jab bhi \"{keyword}\" bola jayega, ye sticker bhej dunga.")


async def remove_sticker_react(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/removestickerreact <keyword>`", parse_mode="Markdown")
        return
    chat_id = update.message.chat_id
    keyword = " ".join(context.args).lower()
    sticker_replies[chat_id].pop(keyword, None)
    await update.message.reply_text(f"✅ \"{keyword}\" sticker-react removed.")


# ============================================================
# MULTI-PLATFORM: DISCORD RELAY
# ============================================================
def relay_to_discord(chat_id: int, author: str, text: str):
    """Blocking HTTP call — always run this via asyncio.to_thread so it
    never stalls the bot's event loop."""
    webhook_url = discord_webhooks.get(chat_id)
    if not webhook_url:
        return
    try:
        requests.post(webhook_url, json={"content": f"**{author}**: {text}"}, timeout=5)
    except Exception as e:
        log.warning(f"Discord relay failed: {e}")


async def set_discord_webhook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/setdiscordwebhook <url>`", parse_mode="Markdown")
        return
    chat_id = update.message.chat_id
    discord_webhooks[chat_id] = context.args[0]
    await update.message.reply_text("✅ Discord relay set! Group messages will now mirror to that Discord channel.")


async def remove_discord_webhook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    discord_webhooks.pop(update.message.chat_id, None)
    await update.message.reply_text("✅ Discord relay removed.")


# ============================================================
# MULTI-PLATFORM: CHANNEL → GROUP AUTO-FORWARD
# ============================================================
async def link_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run inside the group. Bot must be admin in both the channel and the group."""
    if not await is_group_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: `/linkchannel <channel_id>` (channel_id looks like -1001234567890)",
            parse_mode="Markdown"
        )
        return
    try:
        channel_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Channel ID must be numeric, e.g. `-1001234567890`.", parse_mode="Markdown")
        return
    channel_links[channel_id].add(update.message.chat_id)
    await update.message.reply_text(f"✅ Posts from channel {channel_id} will now auto-forward to this group.")


async def unlink_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/unlinkchannel <channel_id>`", parse_mode="Markdown")
        return
    try:
        channel_id = int(context.args[0])
    except ValueError:
        return
    channel_links[channel_id].discard(update.message.chat_id)
    await update.message.reply_text("✅ Unlinked.")


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.channel_post:
        return
    channel_id = update.channel_post.chat.id
    for group_id in channel_links.get(channel_id, set()):
        try:
            await context.bot.forward_message(
                chat_id=group_id,
                from_chat_id=channel_id,
                message_id=update.channel_post.message_id
            )
        except Exception as e:
            log.warning(f"Channel forward to {group_id} failed: {e}")


# ============================================================
# VIRTUAL CURRENCY + SHOP
# ============================================================
def add_coins(chat_id: int, user_id: int, amount: int):
    coins[chat_id][user_id] += amount


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    user_id = update.message.from_user.id
    await update.message.reply_text(f"💰 Your balance: {coins[chat_id][user_id]} coins")


async def shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🛒 *Shop*\n\n"
        f"• `title` — {SHOP_ITEMS['title']} coins — Set a custom title\n"
        f"• `shoutout` — {SHOP_ITEMS['shoutout']} coins — Dramatic group announcement\n\n"
        "Usage:\n`/buy title <your title>`\n`/buy shoutout <message>`\n\n"
        f"Earn coins: +{COINS_PER_MESSAGE}/message, +{TRIVIA_WIN_COINS} per trivia win, "
        f"+{STREAK_MILESTONE_BONUS} every {STREAK_MILESTONE_EVERY}-day streak."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def buy_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    user = update.message.from_user
    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/buy <item> <text>` — see `/shop`", parse_mode="Markdown")
        return

    item = context.args[0].lower()
    extra_text = " ".join(context.args[1:])
    if item not in SHOP_ITEMS:
        await update.message.reply_text("⚠️ Unknown item. Check `/shop`.", parse_mode="Markdown")
        return

    cost = SHOP_ITEMS[item]
    if coins[chat_id][user.id] < cost:
        await update.message.reply_text(f"❌ Not enough coins. Need {cost}, you have {coins[chat_id][user.id]}.")
        return

    if item == "title" and not extra_text:
        await update.message.reply_text("⚠️ Usage: `/buy title <your title>`", parse_mode="Markdown")
        return

    coins[chat_id][user.id] -= cost

    if item == "title":
        custom_titles[chat_id][user.id] = extra_text
        await update.message.reply_text(f"✅ Title set: \"{extra_text}\" 🎉")
    elif item == "shoutout":
        msg = extra_text or "Sabko dhyan dena chahiye!"
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📣 *SHOUTOUT* 📣\n\n[{user.first_name}](tg://user?id={user.id}): {msg}",
            parse_mode="Markdown"
        )


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    user = update.message.from_user
    streak_info = streaks.get(chat_id, {}).get(user.id, {"count": 0})
    title = custom_titles.get(chat_id, {}).get(user.id)
    title_line = f"🏷️ Title: {title}\n" if title else ""
    text = (
        f"👤 *Profile — {user.first_name}*\n\n"
        f"{title_line}"
        f"💬 Messages: {message_count.get(chat_id, {}).get(user.id, 0)}\n"
        f"🔥 Streak: {streak_info['count']} day(s)\n"
        f"💰 Coins: {coins[chat_id][user.id]}\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ============================================================
# STREAK SYSTEM
# ============================================================
def update_streak(chat_id: int, user_id: int):
    today = datetime.utcnow().date()
    info = streaks[chat_id].get(user_id)
    if not info:
        streaks[chat_id][user_id] = {"count": 1, "last_date": today}
        return
    last_date = info["last_date"]
    if today == last_date:
        return  # already counted today
    if today == last_date + timedelta(days=1):
        info["count"] += 1
        info["last_date"] = today
        if info["count"] % STREAK_MILESTONE_EVERY == 0:
            add_coins(chat_id, user_id, STREAK_MILESTONE_BONUS)
    else:
        info["count"] = 1
        info["last_date"] = today


async def my_streak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    info = streaks.get(chat_id, {}).get(update.message.from_user.id, {"count": 0})
    await update.message.reply_text(f"🔥 Your current streak: {info['count']} day(s) in a row")


# ============================================================
# BIRTHDAY TRACKER
# ============================================================
async def set_birthday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    user = update.message.from_user
    if not context.args:
        await update.message.reply_text("⚠️ Usage: /setbirthday DD-MM\nExample: /setbirthday 25-12")
        return
    try:
        day_str, month_str = context.args[0].split("-")
        day, month = int(day_str), int(month_str)
        if not (1 <= day <= 31 and 1 <= month <= 12):
            raise ValueError
    except (ValueError, IndexError):
        await update.message.reply_text("⚠️ Format sahi nahi hai. Usage: /setbirthday DD-MM\nExample: /setbirthday 25-12")
        return
    birthdays[chat_id][user.id] = f"{day:02d}-{month:02d}"
    await update.message.reply_text(f"✅ Birthday set: {day:02d}-{month:02d} 🎂 Us din group mein wish milegi!")


async def my_birthday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    bday = birthdays.get(chat_id, {}).get(update.message.from_user.id)
    if bday:
        await update.message.reply_text(f"🎂 Your birthday: {bday}")
    else:
        await update.message.reply_text("⚠️ Birthday set nahi hai. `/setbirthday DD-MM` use karo.", parse_mode="Markdown")


async def remove_birthday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    birthdays.get(chat_id, {}).pop(update.message.from_user.id, None)
    await update.message.reply_text("✅ Birthday removed.")


async def check_birthdays(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.utcnow().strftime("%d-%m")
    for chat_id, members in list(birthdays.items()):
        for user_id, bday in members.items():
            if bday == today:
                name = active_members.get(chat_id, {}).get(user_id, "Someone")
                try:
                    add_coins(chat_id, user_id, BIRTHDAY_BONUS_COINS)
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"🎉🎂 Happy Birthday {name}! 🎈🎊\n\n"
                            f"Sabki taraf se shubhkaamnaayein! (+{BIRTHDAY_BONUS_COINS} coins 🎁)"
                        )
                    )
                except Exception as e:
                    log.warning(f"Birthday wish failed for {chat_id}/{user_id}: {e}")


# ============================================================
# ACHIEVEMENT BADGES
# ============================================================
async def check_message_badges(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    count = message_count.get(chat_id, {}).get(user_id, 0)
    for milestone in MESSAGE_MILESTONES:
        badge_name = f"💬 {milestone} Messages"
        if count >= milestone and badge_name not in badges[chat_id][user_id]:
            badges[chat_id][user_id].add(badge_name)
            add_coins(chat_id, user_id, BADGE_BONUS_COINS)
            name = active_members.get(chat_id, {}).get(user_id, "Someone")
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🏅 {name} ne badge unlock kiya: {badge_name}! (+{BADGE_BONUS_COINS} coins)"
                )
            except Exception as e:
                log.warning(f"Badge announce failed: {e}")


async def check_time_badges(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.utcnow()
    for chat_id, members in list(first_seen.items()):
        for user_id, joined in members.items():
            days = (now - joined).days
            for threshold, badge_name in TIME_MILESTONES.items():
                if days >= threshold and badge_name not in badges[chat_id][user_id]:
                    badges[chat_id][user_id].add(badge_name)
                    add_coins(chat_id, user_id, BADGE_BONUS_COINS)
                    name = active_members.get(chat_id, {}).get(user_id, "Someone")
                    try:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"🏅 {name} ne badge unlock kiya: {badge_name}! (+{BADGE_BONUS_COINS} coins)"
                        )
                    except Exception as e:
                        log.warning(f"Time badge announce failed: {e}")


async def my_badges(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    user_badges = badges.get(chat_id, {}).get(update.message.from_user.id, set())
    if not user_badges:
        await update.message.reply_text("🏅 Abhi koi badge nahi mila. Active raho, milestones pe auto mil jayega!")
        return
    text = "🏅 Your Badges:\n\n" + "\n".join(f"• {b}" for b in sorted(user_badges))
    await update.message.reply_text(text)


# ============================================================
# SMART AI AUTO-MOD
# ============================================================
async def toggle_automod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    chat_id = update.message.chat_id
    if not context.args or context.args[0].lower() not in ("on", "off"):
        current = "ON ✅" if automod_enabled[chat_id] else "OFF ❌"
        await update.message.reply_text(
            f"⚙️ Smart AI auto-mod currently **{current}**.\nUsage: `/automod on` or `/automod off`",
            parse_mode="Markdown"
        )
        return
    automod_enabled[chat_id] = context.args[0].lower() == "on"
    state = "enabled ✅ — AI will catch genuine toxicity/harassment (casual banter is fine)" \
        if automod_enabled[chat_id] else "disabled ❌"
    await update.message.reply_text(f"⚙️ Smart AI auto-mod {state}.")


async def ai_is_toxic(text: str) -> bool:
    if not ai_client:
        return False
    try:
        def fetch():
            r = ai_client.models.generate_content(
                model=GEMINI_MODEL_CHAIN[0],
                contents=(
                    "You moderate a casual Hinglish Telegram group. Casual slang, playful "
                    "insults between friends, and mild swearing are NORMAL — do not flag those. "
                    "Only flag genuine harassment, hate speech, threats, or targeted bullying. "
                    "Respond with exactly one word: YES if toxic, NO if not.\n\n"
                    f"Message: {text}"
                )
            )
            return r.text.strip().upper()
        result = await asyncio.to_thread(fetch)
        return result.startswith("YES")
    except Exception as e:
        log.warning(f"Automod check failed: {e}")
        return False


# ============================================================
# MULTIPLAYER TRIVIA GAME
# ============================================================
TRIVIA_TIME_LIMIT = 30  # seconds


async def generate_trivia_question():
    if not ai_client:
        return None
    prompt = (
        "Generate one EASY, fun, general-knowledge trivia question suitable for a casual "
        "Hinglish Telegram group chat with regular people, not experts or students. "
        "Keep it simple — everyday topics like movies, sports, food, geography, common facts. "
        "AVOID niche, technical, academic, or hard/obscure questions. "
        "Do NOT use any markdown symbols like * _ [ ] in your response — plain text only. "
        "Respond in EXACTLY this format and nothing else:\n"
        "Q: <question>\nA: <option A>\nB: <option B>\nC: <option C>\nD: <option D>\nCORRECT: <letter>"
    )

    def fetch():
        r = ai_client.models.generate_content(model=GEMINI_MODEL_CHAIN[0], contents=prompt)
        return r.text

    try:
        raw = await asyncio.to_thread(fetch)
        data = {}
        for line in [l.strip() for l in raw.strip().split("\n") if l.strip()]:
            if line.startswith("Q:"):
                data["question"] = line[2:].strip()
            elif line.startswith("A:"):
                data["A"] = line[2:].strip()
            elif line.startswith("B:"):
                data["B"] = line[2:].strip()
            elif line.startswith("C:"):
                data["C"] = line[2:].strip()
            elif line.startswith("D:"):
                data["D"] = line[2:].strip()
            elif line.startswith("CORRECT:"):
                data["correct"] = line.split(":", 1)[1].strip().upper()[:1]
        if all(k in data for k in ["question", "A", "B", "C", "D", "correct"]):
            return data
    except Exception as e:
        log.warning(f"Trivia generation failed: {e}")
    return None


async def start_trivia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if trivia_sessions.get(chat_id, {}).get("active"):
        await update.message.reply_text("⚠️ Ek trivia round already chal raha hai!")
        return

    await update.message.reply_text("🎯 Generating trivia question...")
    q = await generate_trivia_question()
    if not q:
        await update.message.reply_text("❌ Trivia generate nahi ho paya, thodi der me try karo.")
        return

    existing_scores = trivia_sessions.get(chat_id, {}).get("scores", defaultdict(int))
    trivia_sessions[chat_id] = {
        "question": q, "active": True, "answered_by": None, "scores": existing_scores
    }

    text = (
        f"🎯 Trivia Time!\n\n{q['question']}\n\n"
        f"A) {q['A']}\nB) {q['B']}\nC) {q['C']}\nD) {q['D']}\n\n"
        f"Reply with just the letter (A/B/C/D). {TRIVIA_TIME_LIMIT} seconds!"
    )
    try:
        await update.message.reply_text(text)  # plain text — AI-generated content can break Markdown parsing
    except Exception as e:
        log.error(f"Trivia message send failed: {e}")
        trivia_sessions.pop(chat_id, None)
        await update.message.reply_text("❌ Trivia question bhejne mein error aaya, dobara try karo.")
        return

    if context.job_queue:
        context.job_queue.run_once(end_trivia_round, TRIVIA_TIME_LIMIT, data={"chat_id": chat_id})


async def end_trivia_round(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    session = trivia_sessions.get(chat_id)
    if not session or not session["active"]:
        return
    session["active"] = False
    if not session["answered_by"]:
        correct = session["question"]["correct"]
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ Time's up! Koi sahi answer nahi de paya. Correct answer tha: {correct}"
        )


async def handle_trivia_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Checks a plain A/B/C/D reply against an active trivia round. Returns True if handled."""
    chat_id = update.message.chat_id
    session = trivia_sessions.get(chat_id)
    if not session or not session["active"]:
        return False
    answer = update.message.text.strip().upper()
    if answer not in ("A", "B", "C", "D"):
        return False

    correct = session["question"]["correct"]
    if answer == correct:
        session["active"] = False
        session["answered_by"] = update.message.from_user.id
        session["scores"][update.message.from_user.id] += 1
        add_coins(chat_id, update.message.from_user.id, TRIVIA_WIN_COINS)
        await update.message.reply_text(
            f"🎉 Correct! {update.message.from_user.first_name} jeet gaye aur {TRIVIA_WIN_COINS} coins mile!"
        )
    else:
        await update.message.reply_text("❌ Galat, try again!")
    return True


async def trivia_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    scores = trivia_sessions.get(chat_id, {}).get("scores", {})
    if not scores:
        await update.message.reply_text("📊 Abhi koi trivia scores nahi hain. `/trivia` se shuru karo!", parse_mode="Markdown")
        return
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:10]
    lines = ["🏆 *Trivia Leaderboard*\n"]
    for i, (uid, score) in enumerate(ranked):
        name = active_members.get(chat_id, {}).get(uid, f"User {uid}")
        lines.append(f"{i + 1}. {name} — {score} win(s)")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ============================================================
# MULTIPLAYER GAME: TIC-TAC-TOE (1v1, inline buttons)
# ============================================================
ttt_games = {}  # chat_id -> game state
TTT_WIN_COINS = 20
TTT_LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]


def render_ttt_board(board):
    symbols = {" ": "➕", "X": "❌", "O": "⭕"}
    keyboard = []
    for r in range(3):
        row = [InlineKeyboardButton(symbols[board[r * 3 + c]], callback_data=f"ttt_{r * 3 + c}") for c in range(3)]
        keyboard.append(row)
    return InlineKeyboardMarkup(keyboard)


def check_ttt_winner(board):
    for a, b, c in TTT_LINES:
        if board[a] != " " and board[a] == board[b] == board[c]:
            return board[a]
    return "DRAW" if " " not in board else None


async def start_tictactoe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if ttt_games.get(chat_id, {}).get("active"):
        await update.message.reply_text("⚠️ Ek Tic-Tac-Toe already chal raha hai is group mein!")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "⚠️ Jisko challenge karna hai, uske message pe reply karke `/tictactoe` bhejo.",
            parse_mode="Markdown"
        )
        return
    opponent = update.message.reply_to_message.from_user
    challenger = update.message.from_user
    if opponent.id == challenger.id:
        await update.message.reply_text("⚠️ Khud se nahi khel sakte! 😅")
        return
    if opponent.is_bot:
        await update.message.reply_text("⚠️ Bot ko challenge nahi kar sakte!")
        return

    ttt_games[chat_id] = {
        "active": True, "board": [" "] * 9, "turn": "X",
        "player_x": challenger.id, "player_o": opponent.id,
        "names": {challenger.id: challenger.first_name, opponent.id: opponent.first_name}
    }
    await update.message.reply_text(
        f"🎮 *Tic-Tac-Toe!*\n❌ {challenger.first_name} vs ⭕ {opponent.first_name}\n\n{challenger.first_name}'s turn (❌)",
        reply_markup=render_ttt_board([" "] * 9),
        parse_mode="Markdown"
    )


async def handle_ttt_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    game = ttt_games.get(chat_id)
    if not game or not game["active"]:
        await query.answer("Koi active game nahi hai. /tictactoe se naya shuru karo!")
        return

    idx = int(query.data.split("_")[1])
    current_player_id = game["player_x"] if game["turn"] == "X" else game["player_o"]
    if query.from_user.id != current_player_id:
        await query.answer("⏳ Ye tumhari turn nahi hai!")
        return
    if game["board"][idx] != " ":
        await query.answer("❌ Cell already filled!")
        return

    await query.answer()
    game["board"][idx] = game["turn"]
    winner = check_ttt_winner(game["board"])

    if winner:
        game["active"] = False
        if winner == "DRAW":
            text = "🤝 Match draw ho gaya!"
        else:
            winner_id = game["player_x"] if winner == "X" else game["player_o"]
            add_coins(chat_id, winner_id, TTT_WIN_COINS)
            text = f"🎉 {game['names'][winner_id]} jeet gaye! (+{TTT_WIN_COINS} coins)"
        await query.edit_message_text(text, reply_markup=render_ttt_board(game["board"]))
        return

    game["turn"] = "O" if game["turn"] == "X" else "X"
    next_id = game["player_x"] if game["turn"] == "X" else game["player_o"]
    symbol = "❌" if game["turn"] == "X" else "⭕"
    await query.edit_message_text(
        f"🎮 *Tic-Tac-Toe*\n{game['names'][next_id]}'s turn ({symbol})",
        reply_markup=render_ttt_board(game["board"]),
        parse_mode="Markdown"
    )


# ============================================================
# MULTIPLAYER GAME: WORD CHAIN (whole group, free-for-all)
# ============================================================
word_chain_sessions = {}  # chat_id -> {"active": bool, "last_word": str, "used_words": set()}
WORD_CHAIN_STARTERS = ["apple", "orange", "tiger", "dance", "music", "cricket", "bottle", "garden", "yellow"]
WORD_CHAIN_COINS = 2


async def start_word_chain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if word_chain_sessions.get(chat_id, {}).get("active"):
        await update.message.reply_text("⚠️ Word chain already chal raha hai! `/endwordchain` se roko.", parse_mode="Markdown")
        return
    starter = random.choice(WORD_CHAIN_STARTERS)
    word_chain_sessions[chat_id] = {"active": True, "last_word": starter, "used_words": {starter}}
    await update.message.reply_text(
        f"🔗 *Word Chain Shuru!*\n\nStarting word: *{starter}*\n\n"
        f"Next word '*{starter[-1].upper()}*' se shuru hona chahiye — koi bhi member type kar sakta hai!",
        parse_mode="Markdown"
    )


async def end_word_chain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    chat_id = update.message.chat_id
    if word_chain_sessions.get(chat_id, {}).get("active"):
        word_chain_sessions[chat_id]["active"] = False
        await update.message.reply_text("🔗 Word chain end kar diya gaya.")
    else:
        await update.message.reply_text("⚠️ Koi active word chain nahi hai.")


async def handle_word_chain_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_id = update.message.chat_id
    session = word_chain_sessions.get(chat_id)
    if not session or not session["active"]:
        return False

    text = update.message.text.strip().lower()
    if not text.isalpha() or len(text) < 2:
        return False  # not a plain word — let normal chat handling continue

    required_letter = session["last_word"][-1]
    if text[0] != required_letter:
        return False  # doesn't continue the chain — ignore, don't disrupt normal chat

    if text in session["used_words"]:
        await update.message.reply_text(f"❌ \"{text}\" already use ho chuka hai! Doosra word try karo.")
        return True

    session["used_words"].add(text)
    session["last_word"] = text
    add_coins(chat_id, update.message.from_user.id, WORD_CHAIN_COINS)
    await update.message.reply_text(
        f"✅ {update.message.from_user.first_name}: *{text}*\nNext word '*{text[-1].upper()}*' se shuru!",
        parse_mode="Markdown"
    )
    return True


# ============================================================
# MULTIPLAYER GAME: MATH BLITZ (fast race, no AI needed)
# ============================================================
math_blitz_sessions = {}  # chat_id -> {"active": bool, "answer": int}
MATH_BLITZ_TIME_LIMIT = 20
MATH_BLITZ_COINS = 10


async def start_math_blitz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if math_blitz_sessions.get(chat_id, {}).get("active"):
        await update.message.reply_text("⚠️ Math blitz already chal raha hai!")
        return

    op = random.choice(["+", "-", "*"])
    if op == "*":
        a, b = random.randint(2, 12), random.randint(2, 12)
        answer = a * b
    elif op == "+":
        a, b = random.randint(2, 50), random.randint(2, 50)
        answer = a + b
    else:
        a, b = random.randint(2, 50), random.randint(2, 50)
        answer = a - b

    math_blitz_sessions[chat_id] = {"active": True, "answer": answer}
    await update.message.reply_text(
        f"🧮 *Math Blitz!*\n\n{a} {op} {b} = ?\n\nFastest sahi jawab jeetega! {MATH_BLITZ_TIME_LIMIT} seconds ⏱️",
        parse_mode="Markdown"
    )
    if context.job_queue:
        context.job_queue.run_once(end_math_blitz, MATH_BLITZ_TIME_LIMIT, data={"chat_id": chat_id})


async def end_math_blitz(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    session = math_blitz_sessions.get(chat_id)
    if session and session["active"]:
        session["active"] = False
        await context.bot.send_message(chat_id=chat_id, text=f"⏰ Time's up! Answer tha: {session['answer']}")


async def handle_math_blitz_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_id = update.message.chat_id
    session = math_blitz_sessions.get(chat_id)
    if not session or not session["active"]:
        return False
    try:
        guess = int(update.message.text.strip())
    except ValueError:
        return False
    if guess == session["answer"]:
        session["active"] = False
        add_coins(chat_id, update.message.from_user.id, MATH_BLITZ_COINS)
        await update.message.reply_text(
            f"🎉 Correct! {update.message.from_user.first_name} jeet gaye! (+{MATH_BLITZ_COINS} coins)"
        )
        return True
    return False  # wrong guess — let it pass through, don't spam "wrong" on every random number


# ============================================================
# DAILY GROUP DIGEST
# ============================================================
async def send_daily_digest(context: ContextTypes.DEFAULT_TYPE):
    for chat_id in list(known_chats):
        history = list(chat_history.get(chat_id, []))
        if len(history) < 5:
            continue
        sample = "\n".join(history[-50:])
        prompt = (
            "Summarize today's group chat highlights in 3-4 short bullet points. "
            "Hinglish, fun and casual tone:\n" + sample
        )
        try:
            summary = await get_ai_reply(prompt, persona_text=persona.get(chat_id))
            await context.bot.send_message(chat_id=chat_id, text=f"📰 Daily Digest\n\n{summary}")
        except Exception as e:
            log.warning(f"Digest failed for chat {chat_id}: {e}")


async def digest_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: preview the digest immediately instead of waiting for the daily schedule."""
    if not is_admin(update):
        return
    chat_id = update.message.chat_id
    history = list(chat_history.get(chat_id, []))
    if not history:
        await update.message.reply_text("⏳ Not enough chat history yet.")
        return
    sample = "\n".join(history[-50:])
    prompt = "Summarize today's group chat highlights in 3-4 short bullet points, Hinglish fun tone:\n" + sample
    summary = await get_ai_reply(prompt, persona_text=persona.get(chat_id))
    await update.message.reply_text(f"📰 Digest Preview\n\n{summary}")


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
    if not is_bot_admin_id(update.message.from_user.id):
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text(
            f"⚠️ Usage: `/broadcast <message>`\n📊 Will reach {len(known_chats)} known chat(s).",
            parse_mode="Markdown"
        )
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

    # Check active game answers first — these shouldn't hit any other filter
    if chat_type in ["group", "supergroup"] and not user.is_bot:
        if await handle_trivia_answer(update, context):
            return
        if await handle_math_blitz_answer(update, context):
            return
        if await handle_word_chain_answer(update, context):
            return

    # Track active members for /tagall and leaderboard
    if chat_type in ["group", "supergroup"] and user.id != ADMIN_ID and not user.is_bot:
        active_members[chat_id][user.id] = user.first_name
        message_count[chat_id][user.id] += 1
        add_coins(chat_id, user.id, COINS_PER_MESSAGE)
        update_streak(chat_id, user.id)
        if user.id not in first_seen[chat_id]:
            first_seen[chat_id][user.id] = datetime.utcnow()
        await check_message_badges(chat_id, user.id, context)

    # Remember recent messages so the AI can match this group's tone/style
    if not user.is_bot:
        chat_history[chat_id].append(update.message.text)

    is_grp_admin = await is_group_admin(update, context)

    # Slow mode (skip for admins)
    if chat_type in ["group", "supergroup"] and not is_grp_admin and slow_mode[chat_id] > 0:
        now = time.time()
        last = last_message_time[chat_id].get(user.id, 0)
        if now - last < slow_mode[chat_id]:
            try:
                await update.message.delete()
            except Exception as e:
                log.warning(f"Slow mode delete failed: {e}")
            return
        last_message_time[chat_id][user.id] = now

    # Anti-flood (skip for admins)
    if chat_type in ["group", "supergroup"] and not is_grp_admin:
        if await check_flood(update, context):
            return

    # Smart AI auto-mod (skip for admins) — only checks longer messages to keep replies fast/cheap
    if chat_type in ["group", "supergroup"] and not is_grp_admin and automod_enabled[chat_id]:
        if len(update.message.text.split()) >= 4:
            if await ai_is_toxic(update.message.text):
                try:
                    await update.message.delete()
                except Exception as e:
                    log.warning(f"Automod delete failed: {e}")
                result = await apply_warning(chat_id, user, context, reason="AI flagged toxic content")
                try:
                    await context.bot.send_message(chat_id=chat_id, text=result)
                except Exception:
                    pass
                return

    # Bad word filter (skip for admins)
    if chat_type in ["group", "supergroup"] and not is_grp_admin and bad_words.get(chat_id):
        if any(word in text for word in bad_words[chat_id]):
            try:
                await update.message.delete()
            except Exception as e:
                log.warning(f"Bad word delete failed: {e}")
            result = await apply_warning(chat_id, user, context, reason="inappropriate language")
            try:
                await context.bot.send_message(chat_id=chat_id, text=result)
            except Exception:
                pass
            return

    # Anti-link — own group/channel links and anything whitelisted always pass through
    if chat_type in ["group", "supergroup"] and not is_grp_admin:
        if any(link in text for link in ["http://", "https://", "t.me/", ".com", ".in"]):
            own_or_whitelisted = (
                GROUP_LINK.lower() in update.message.text.lower()
                or CHANNEL_LINK.lower() in update.message.text.lower()
                or any(w in text for w in link_whitelist.get(chat_id, set()))
            )
            if not own_or_whitelisted:
                try:
                    await update.message.delete()
                    warning = await update.message.reply_text(f"⚠️ {user.first_name}, yahan links allowed nahi hai!")
                    await asyncio.sleep(5)
                    await warning.delete()
                except Exception as e:
                    log.warning(f"Anti-link cleanup failed: {e}")
                return

    # Multi-platform: mirror this message to Discord if a webhook is set for this chat
    if chat_type in ["group", "supergroup"] and chat_id in discord_webhooks:
        asyncio.create_task(asyncio.to_thread(relay_to_discord, chat_id, user.first_name, update.message.text))

    # Group / channel link requests — always answer these, private or group
    if "link" in text:
        wants_channel = "channel" in text
        wants_group = "group" in text
        if wants_channel and not wants_group:
            await update.message.reply_text(f"📢 Channel link: {CHANNEL_LINK}")
            return
        if wants_group and not wants_channel:
            await update.message.reply_text(f"👥 Group link: {GROUP_LINK}")
            return
        if wants_channel or wants_group or any(k in text for k in ["link do", "link bhejo", "link chahiye"]):
            await update.message.reply_text(f"👥 Group: {GROUP_LINK}\n📢 Channel: {CHANNEL_LINK}")
            return

    # Sticker auto-react
    if chat_type in ["group", "supergroup"] and sticker_replies.get(chat_id):
        for keyword, file_id in sticker_replies[chat_id].items():
            if keyword in text:
                try:
                    await context.bot.send_sticker(chat_id=chat_id, sticker=file_id)
                except Exception as e:
                    log.warning(f"Sticker react failed: {e}")
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
            ai_reply = await get_ai_reply(
                update.message.text,
                style_context=list(chat_history[chat_id]),
                persona_text=persona.get(chat_id)
            )
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
# ============================================================
# GLOBAL ERROR HANDLER (catches anything unhandled so the bot never crashes silently)
# ============================================================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error(f"Unhandled exception: {context.error}", exc_info=context.error)


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
    app.add_handler(CommandHandler("topmembers", top_members))
    app.add_handler(CommandHandler("testai", test_ai))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("shop", shop))
    app.add_handler(CommandHandler("buy", buy_item))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("mystreak", my_streak))
    app.add_handler(CommandHandler("setbirthday", set_birthday))
    app.add_handler(CommandHandler("mybirthday", my_birthday))
    app.add_handler(CommandHandler("removebirthday", remove_birthday))
    app.add_handler(CommandHandler("mybadges", my_badges))
    app.add_handler(CommandHandler("trivia", start_trivia))
    app.add_handler(CommandHandler("trivialeaderboard", trivia_leaderboard))
    app.add_handler(CommandHandler("tictactoe", start_tictactoe))
    app.add_handler(CommandHandler("wordchain", start_word_chain))
    app.add_handler(CommandHandler("endwordchain", end_word_chain))
    app.add_handler(CommandHandler("mathblitz", start_math_blitz))

    # Bot admin panel (owner-managed, works across all groups)
    app.add_handler(CommandHandler("addadmin", add_admin))
    app.add_handler(CommandHandler("removeadmin", remove_admin))
    app.add_handler(CommandHandler("listadmins", list_admins))
    app.add_handler(CommandHandler("adminpanel", admin_panel))

    # Admin config
    app.add_handler(CommandHandler("setrules", set_rules))
    app.add_handler(CommandHandler("setwelcome", set_welcome))
    app.add_handler(CommandHandler("setpersona", set_persona))
    app.add_handler(CommandHandler("resetpersona", reset_persona))
    app.add_handler(CommandHandler("automod", toggle_automod))
    app.add_handler(CommandHandler("digest", digest_now))
    app.add_handler(CommandHandler("autoapprove", toggle_autoapprove))
    app.add_handler(CommandHandler("setdelay", set_delay))
    app.add_handler(CommandHandler("slowmode", set_slowmode))
    app.add_handler(CommandHandler("addbadword", add_bad_word))
    app.add_handler(CommandHandler("removebadword", remove_bad_word))
    app.add_handler(CommandHandler("badwords", list_bad_words))
    app.add_handler(CommandHandler("allowlink", allow_link))
    app.add_handler(CommandHandler("disallowlink", disallow_link))
    app.add_handler(CommandHandler("listlinks", list_links))
    app.add_handler(CommandHandler("addstickerreact", add_sticker_react))
    app.add_handler(CommandHandler("removestickerreact", remove_sticker_react))
    app.add_handler(CommandHandler("setdiscordwebhook", set_discord_webhook))
    app.add_handler(CommandHandler("removediscordwebhook", remove_discord_webhook))
    app.add_handler(CommandHandler("linkchannel", link_channel))
    app.add_handler(CommandHandler("unlinkchannel", unlink_channel))
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
    app.add_handler(CallbackQueryHandler(button_handler, pattern="^owner_info$"))
    app.add_handler(CallbackQueryHandler(handle_ttt_click, pattern="^ttt_"))
    app.add_handler(ChatJoinRequestHandler(join_request))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, handle_channel_post))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))

    app.add_error_handler(error_handler)

    if app.job_queue:
        app.job_queue.run_daily(send_daily_digest, time=dt_time(hour=21, minute=0))
        app.job_queue.run_daily(check_birthdays, time=dt_time(hour=3, minute=0))
        app.job_queue.run_daily(check_time_badges, time=dt_time(hour=3, minute=30))
        log.info("📰 Daily digest (21:00 UTC), 🎂 birthdays (03:00 UTC), 🏅 time badges (03:30 UTC) scheduled.")
    else:
        log.warning(
            "job_queue not available — daily digest/birthdays/badges disabled. "
            "Install with: pip install \"python-telegram-bot[job-queue]\""
        )

    log.info("🚀 Prime X Assistant deployed successfully!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
