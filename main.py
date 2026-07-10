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

from openai import OpenAI  # Sirf OpenAI library use hogi NVIDIA ke liye
from dotenv import load_dotenv
from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, ChatJoinRequestHandler
)

# ============================================================
# CHANGELOG (bug fixes applied in this version)
# ------------------------------------------------------------
# 1. get_ai_reply() no longer silently returns None on an empty
#    AI response - it now always returns a string.
# 2. All NVIDIA API calls now have a timeout=15, so one slow
#    request can no longer hang a handler indefinitely.
# 3. Bare "except: pass" blocks (mute/unmute/kick/ban/set_delay/
#    welcome message) now log the actual error so failures are
#    visible instead of silently making the bot look "slow".
# 4. Added 3 new multiplayer games: /rps (1v1), /triviarace
#    (group race, button based), /mathduel (1v1, button based).
#    These are separate from your existing /tictactoe, /trivia,
#    /mathblitz, /wordchain - no collisions.
# ============================================================

# ============================================================
# CONFIGURATION
# ============================================================
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

OWNER_NAME = "@PREMGUPTA2M"
CHANNEL_LINK = "https://t.me/+Gouc7PsDosk4MTRl"
GROUP_LINK = "https://t.me/+rSqVXbRig4BjOTc1"

MAX_WARNINGS = 3
FLOOD_MSG_LIMIT = 6
FLOOD_WINDOW_SECONDS = 8
FLOOD_MUTE_MINUTES = 10

NVIDIA_TIMEOUT_SECONDS = 15
GAME_TIMEOUT_SECONDS = 90

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("PrimeXAssistant")

# ============================================================
# AI CLIENT SETUP (NVIDIA ONLY)
# ============================================================
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL", "meta/llama-3.3-70b-instruct")
nvidia_client = OpenAI(api_key=NVIDIA_API_KEY, base_url="https://integrate.api.nvidia.com/v1") if NVIDIA_API_KEY else None

# ============================================================
# GLOBAL STATE
# ============================================================
group_rules = defaultdict(lambda: "Group ke rules abhi set nahi hain.")
active_members = defaultdict(dict)
warnings = defaultdict(lambda: defaultdict(int))
message_log = defaultdict(lambda: defaultdict(lambda: deque(maxlen=FLOOD_MSG_LIMIT + 1)))
known_chats = set()
approval_settings = defaultdict(lambda: {"enabled": True, "delay": 5})
chat_history = defaultdict(lambda: deque(maxlen=30))
custom_welcome = {}
bad_words = defaultdict(set)
message_count = defaultdict(lambda: defaultdict(int))
slow_mode = defaultdict(int)
last_message_time = defaultdict(dict)
link_whitelist = defaultdict(set)
sticker_replies = defaultdict(dict)
discord_webhooks = {}
channel_links = defaultdict(set)

persona = {}
coins = defaultdict(lambda: defaultdict(int))
custom_titles = defaultdict(dict)
streaks = defaultdict(dict)
automod_enabled = defaultdict(bool)
trivia_sessions = {}

SHOP_ITEMS = {"title": 200, "shoutout": 50}
COINS_PER_MESSAGE = 1
TRIVIA_WIN_COINS = 15
STREAK_MILESTONE_BONUS = 50
STREAK_MILESTONE_EVERY = 7

birthdays = defaultdict(dict)
badges = defaultdict(lambda: defaultdict(set))
first_seen = defaultdict(dict)
MESSAGE_MILESTONES = [100, 500, 1000, 5000]
TIME_MILESTONES = {
    7: "🌱 1 Week Member",
    30: "🌿 1 Month Member",
    90: "🌳 3 Month Member",
    365: "🏆 1 Year Member",
}
BADGE_BONUS_COINS = 25
BIRTHDAY_BONUS_COINS = 50

TAG_BATCH_SIZE = 5
TAG_BATCH_DELAY = 1.5

promo_codes = {}
rewarded_groups = set()
BOT_ADD_REWARD = 500

# New multiplayer game rewards
RPS_WIN_COINS = 20
TRIVIA_RACE_WIN_COINS = 25
MATH_DUEL_WIN_COINS = 20

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
        pass

def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), DummyHandler)
    server.serve_forever()

# ============================================================
# HELPERS
# ============================================================
bot_admins = set()

def is_owner(user_id: int) -> bool:
    return user_id == ADMIN_ID

def is_bot_admin_id(user_id: int) -> bool:
    return user_id == ADMIN_ID or user_id in bot_admins

def is_admin(update: Update) -> bool:
    if not update.message: return False
    if update.message.from_user and is_bot_admin_id(update.message.from_user.id): return True
    if update.message.sender_chat and update.message.sender_chat.id == update.message.chat_id: return True
    return False

async def is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if is_admin(update): return True
    try:
        member = await context.bot.get_chat_member(update.message.chat_id, update.message.from_user.id)
        return member.status in ("administrator", "creator")
    except Exception as e:
        log.error(f"is_group_admin check failed: {repr(e)}")
        return False

def get_target_user(update: Update):
    if update.message.reply_to_message: return update.message.reply_to_message.from_user
    return None

# ============================================================
# BOT ADMIN PANEL
# ============================================================
async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.message.from_user.id):
        await update.message.reply_text("❌ Sirf bot owner naye admin bana sakta hai.")
        return
    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/addadmin <telegram_user_id>`", parse_mode="Markdown")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        return
    bot_admins.add(uid)
    await update.message.reply_text(f"✅ User `{uid}` ab bot admin hai.", parse_mode="Markdown")

async def remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.message.from_user.id): return
    if not context.args: return
    try: uid = int(context.args[0])
    except ValueError: return
    bot_admins.discard(uid)
    await update.message.reply_text(f"✅ User `{uid}` bot admins se remove kar diya gaya.", parse_mode="Markdown")

async def list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_admin_id(update.message.from_user.id): return
    text = f"👑 *Owner:* `{ADMIN_ID}`\n\n"
    if bot_admins:
        text += "🛡️ *Bot Admins:*\n" + "\n".join(f"• `{uid}`" for uid in bot_admins)
    else:
        text += "🛡️ Koi extra bot admin abhi set nahi hai."
    await update.message.reply_text(text, parse_mode="Markdown")

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_admin_id(update.message.from_user.id): return
    text = (
        "🛠️ *Bot Admin Panel*\n\n"
        f"👑 Owner ID: `{ADMIN_ID}`\n"
        f"🛡️ Bot Admins: {len(bot_admins)}\n"
        f"💬 Known chats: {len(known_chats)}\n\n"
        "`/addadmin <id>`, `/removeadmin <id>`, `/listadmins`, `/testai`, `/genpromo`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# ============================================================
# AI REPLY (NVIDIA ONLY)
# ============================================================
async def get_ai_reply(prompt: str, style_context: list = None, persona_text: str = None) -> str:
    if not nvidia_client:
        return "❌ NVIDIA_API_KEY missing hai!"

    style_hint = ""
    if style_context:
        sample = "\n".join(style_context[-10:])
        style_hint = "\n\nMatch their style:\n" + sample

    base_persona = persona_text or "You are a casual telegram group member. Reply in short Hinglish."
    system_prompt = base_persona + style_hint

    try:
        def fetch_nvidia():
            completion = nvidia_client.chat.completions.create(
                model=NVIDIA_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=512,
                timeout=NVIDIA_TIMEOUT_SECONDS,
            )
            return completion.choices[0].message.content
        reply = await asyncio.to_thread(fetch_nvidia)
        if reply:
            return reply
        return "🤖 AI ne khaali reply diya, dobara try karo."
    except Exception as e:
        log.error(f"NVIDIA API failed: {repr(e)}")
        return f"❌ AI abhi busy hai. Error: {repr(e)[:50]}"

async def test_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    await update.message.reply_text("🔧 Testing NVIDIA API...")
    results = []

    if nvidia_client:
        try:
            def fetch_nvidia():
                return nvidia_client.chat.completions.create(
                    model=NVIDIA_MODEL, messages=[{"role": "user", "content": "Hi"}],
                    max_tokens=20, timeout=NVIDIA_TIMEOUT_SECONDS
                ).choices[0].message.content
            reply = await asyncio.to_thread(fetch_nvidia)
            results.append(f"✅ NVIDIA ({NVIDIA_MODEL}) → {reply.strip()[:60]}")
        except Exception as e:
            results.append(f"❌ NVIDIA → {repr(e)[:100]}")
    else:
        results.append("⚪ NVIDIA not configured.")

    await update.message.reply_text("🔧 *Results:*\n\n" + "\n\n".join(results), parse_mode="Markdown")

# ============================================================
# PERSONA CUSTOMIZATION
# ============================================================
async def set_persona(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context): return
    text = " ".join(context.args)
    if not text: return
    persona[update.message.chat_id] = text
    await update.message.reply_text("✅ Bot persona updated for this group!")

async def reset_persona(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context): return
    persona.pop(update.message.chat_id, None)
    await update.message.reply_text("✅ Persona reset.")

# ============================================================
# START MENU
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name if update.effective_user else "Admin"
    text = f"Hello {user_name}! 👋\n\nMain ek advanced AI Group Manager bot hoon.\nType /help to see everything I can do."
    keyboard = [
        [InlineKeyboardButton("📢 Channel", url=CHANNEL_LINK), InlineKeyboardButton("👥 Group", url=GROUP_LINK)],
        [InlineKeyboardButton("👨‍💻 Owner / Admin", callback_data="owner_info")]
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*🤖 Prime X Assistant — Commands*\n\n"
        "*General*\n"
        "• /start — Welcome menu\n"
        "• /rules, /stats, /topmembers\n"
        "• /profile, /balance, /shop, /buy\n"
        "• /redeem <CODE> — Redeem promo codes\n"
        "• /mystreak, /setbirthday, /mybadges\n\n"
        "*Solo/Group Games*\n"
        "• /trivia — AI generated trivia (text answer)\n"
        "• /tictactoe — 1v1, reply to challenge\n"
        "• /mathblitz — group race, first to type answer\n"
        "• /wordchain — group word chain\n\n"
        "*New Multiplayer Games*\n"
        "• /rps — Rock-Paper-Scissors 1v1 (reply to challenge)\n"
        "• /triviarace — button-based group race\n"
        "• /mathduel — 1v1 button-based math duel (reply to challenge)\n\n"
        "*Admin only*\n"
        "• /testai, /adminpanel, /genpromo\n"
        "• /setrules, /setwelcome, /setpersona\n"
        "• /automod on|off, /autoapprove\n"
        "• /mute, /kick, /ban, /warn, /pin\n\n"
        f"Owner: {OWNER_NAME}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "owner_info":
        await query.edit_message_text(f"Mere owner **{OWNER_NAME}** hain.", parse_mode="Markdown")
    elif data.startswith("ttt_"):
        await handle_ttt_click(update, context)
    elif data.startswith("rps_"):
        await handle_rps_click(update, context)
    elif data.startswith("tr_"):
        await handle_trivia_race_click(update, context)
    elif data.startswith("md_"):
        await handle_math_duel_click(update, context)

# ============================================================
# GROUP CONFIG COMMANDS
# ============================================================
async def set_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context): return
    try:
        approval_settings[update.message.chat_id]["delay"] = int(context.args[0])
        await update.message.reply_text("✅ Delay updated.")
    except Exception as e:
        log.error(f"set_delay failed: {repr(e)}")

async def toggle_autoapprove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context): return
    if context.args:
        approval_settings[update.message.chat_id]["enabled"] = context.args[0].lower() == "on"
        await update.message.reply_text("✅ Auto-approval updated.")

async def set_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context): return
    group_rules[update.message.chat_id] = " ".join(context.args)
    await update.message.reply_text("✅ Rules updated!")

async def show_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📜 *Group Rules:*\n\n{group_rules[update.message.chat_id]}", parse_mode="Markdown")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📊 Members tracked: {len(active_members.get(update.message.chat_id, {}))}")

# ============================================================
# MODERATION COMMANDS
# ============================================================
async def apply_warning(chat_id: int, target_user, context: ContextTypes.DEFAULT_TYPE, reason: str = "") -> str:
    warnings[chat_id][target_user.id] += 1
    count = warnings[chat_id][target_user.id]
    if count >= MAX_WARNINGS:
        try:
            await context.bot.restrict_chat_member(chat_id, target_user.id, permissions=ChatPermissions(can_send_messages=False))
            warnings[chat_id][target_user.id] = 0
            return f"🔇 {target_user.first_name} ko mute kar diya gaya."
        except Exception as e:
            log.error(f"apply_warning mute failed: {repr(e)}")
            return "⚠️ Mute failed."
    return f"⚠️ {target_user.first_name} warned ({count}/{MAX_WARNINGS})."

async def warn_user(update, context):
    if not await is_group_admin(update, context): return
    target = get_target_user(update)
    if target: await update.message.reply_text(await apply_warning(update.message.chat_id, target, context))

async def unwarn_user(update, context):
    if not await is_group_admin(update, context): return
    target = get_target_user(update)
    if target:
        warnings[update.message.chat_id][target.id] = 0
        await update.message.reply_text("✅ Warnings cleared.")

async def mute_user(update, context):
    if not await is_group_admin(update, context): return
    target = get_target_user(update)
    if target:
        try:
            await context.bot.restrict_chat_member(update.message.chat_id, target.id, permissions=ChatPermissions(can_send_messages=False))
            await update.message.reply_text(f"🔇 {target.first_name} muted.")
        except Exception as e:
            log.error(f"mute_user failed: {repr(e)}")
            await update.message.reply_text("⚠️ Mute failed (check bot admin permissions).")

async def unmute_user(update, context):
    if not await is_group_admin(update, context): return
    target = get_target_user(update)
    if target:
        try:
            await context.bot.restrict_chat_member(update.message.chat_id, target.id, permissions=ChatPermissions(can_send_messages=True, can_send_other_messages=True))
            await update.message.reply_text(f"🔊 {target.first_name} unmuted.")
        except Exception as e:
            log.error(f"unmute_user failed: {repr(e)}")
            await update.message.reply_text("⚠️ Unmute failed (check bot admin permissions).")

async def kick_user(update, context):
    if not await is_group_admin(update, context): return
    target = get_target_user(update)
    if target:
        try:
            await context.bot.ban_chat_member(update.message.chat_id, target.id)
            await context.bot.unban_chat_member(update.message.chat_id, target.id)
            await update.message.reply_text(f"👋 {target.first_name} kicked.")
        except Exception as e:
            log.error(f"kick_user failed: {repr(e)}")
            await update.message.reply_text("⚠️ Kick failed (check bot admin permissions).")

async def ban_user(update, context):
    if not await is_group_admin(update, context): return
    target = get_target_user(update)
    if target:
        try:
            await context.bot.ban_chat_member(update.message.chat_id, target.id)
            await update.message.reply_text(f"🚫 {target.first_name} banned.")
        except Exception as e:
            log.error(f"ban_user failed: {repr(e)}")
            await update.message.reply_text("⚠️ Ban failed (check bot admin permissions).")

# ============================================================
# WELCOME & BOT ADD REWARD
# ============================================================
async def set_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context): return
    custom_welcome[update.message.chat_id] = " ".join(context.args)
    await update.message.reply_text("✅ Welcome message updated!")

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    adder = update.message.from_user

    for member in update.message.new_chat_members:
        if member.id == context.bot.id:
            if chat_id not in rewarded_groups:
                rewarded_groups.add(chat_id)
                add_coins(chat_id, adder.id, BOT_ADD_REWARD)
                await update.message.reply_text(
                    f"🎉 Thank you mujhe is group mein add karne ke liye, [{adder.first_name}](tg://user?id={adder.id})!\n\n"
                    f"🎁 As a reward, maine tumhe **{BOT_ADD_REWARD} coins** diye hain is group mein.\n"
                    f"Type /balance to check your coins!",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text("Hello everyone! Main wapas aa gaya. 👋")
            continue

        if member.is_bot:
            continue

        template = custom_welcome.get(
            chat_id,
            "🎉 Welcome {name}! Glad to have you here.\nType /rules to see the rules."
        )
        try:
            await update.message.reply_text(template.replace("{name}", member.first_name))
        except Exception as e:
            log.error(f"welcome_new_member failed: {repr(e)}")

# ============================================================
# VIRTUAL CURRENCY, PROMO CODES & SHOP
# ============================================================
def add_coins(chat_id: int, user_id: int, amount: int):
    coins[chat_id][user_id] += amount

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"💰 Balance: {coins[update.message.chat_id][update.message.from_user.id]} coins")

async def shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🛒 *Shop*\n`title` (200), `shoutout` (50)\nUsage: `/buy title VIP`", parse_mode="Markdown")

async def buy_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    item = context.args[0].lower()
    cost = SHOP_ITEMS.get(item, 999999)
    if coins[update.message.chat_id][update.message.from_user.id] < cost:
        await update.message.reply_text("❌ Not enough coins.")
        return
    coins[update.message.chat_id][update.message.from_user.id] -= cost
    if item == "title": custom_titles[update.message.chat_id][update.message.from_user.id] = " ".join(context.args[1:])
    await update.message.reply_text(f"✅ Bought {item}!")

async def gen_promo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.message.from_user.id): return
    if len(context.args) < 3:
        await update.message.reply_text("⚠️ Usage: `/genpromo <CODE> <AMOUNT> <LIMIT>`", parse_mode="Markdown")
        return
    code, amt, limit = context.args[0].upper(), int(context.args[1]), int(context.args[2])
    promo_codes[code] = {"amount": amt, "limit": limit, "used_by": set()}
    await update.message.reply_text(f"✅ Code generated: `{code}` for {amt} coins (Limit: {limit})", parse_mode="Markdown")

async def redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type == "private":
        await update.message.reply_text("⚠️ Please redeem codes inside a group!")
        return
    if not context.args: return
    code = context.args[0].upper()
    promo = promo_codes.get(code)

    if not promo:
        await update.message.reply_text("❌ Invalid code.")
        return
    if update.message.from_user.id in promo["used_by"]:
        await update.message.reply_text("⚠️ Already redeemed!")
        return
    if len(promo["used_by"]) >= promo["limit"]:
        await update.message.reply_text("❌ Code limit reached.")
        return

    promo["used_by"].add(update.message.from_user.id)
    add_coins(update.message.chat_id, update.message.from_user.id, promo["amount"])
    await update.message.reply_text(f"🎉 Redeemed! You got {promo['amount']} coins.")

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid, cid = update.message.from_user.id, update.message.chat_id
    await update.message.reply_text(f"👤 *Profile*\nCoins: {coins[cid][uid]}\nTitle: {custom_titles.get(cid,{}).get(uid, 'None')}", parse_mode="Markdown")

# ============================================================
# STREAKS & BADGES
# ============================================================
def update_streak(chat_id: int, user_id: int):
    today = datetime.utcnow().date()
    info = streaks[chat_id].get(user_id)
    if not info:
        streaks[chat_id][user_id] = {"count": 1, "last_date": today}
        return
    if today == info["last_date"]: return
    if today == info["last_date"] + timedelta(days=1):
        info["count"] += 1
        info["last_date"] = today
    else:
        info["count"] = 1
        info["last_date"] = today

async def my_streak(update, context):
    info = streaks.get(update.message.chat_id, {}).get(update.message.from_user.id, {"count": 0})
    await update.message.reply_text(f"🔥 Current streak: {info['count']} days")

async def check_message_badges(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    count = message_count.get(chat_id, {}).get(user_id, 0)
    for milestone in MESSAGE_MILESTONES:
        badge = f"💬 {milestone} Messages"
        if count >= milestone and badge not in badges[chat_id][user_id]:
            badges[chat_id][user_id].add(badge)
            add_coins(chat_id, user_id, BADGE_BONUS_COINS)

async def top_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    counts = message_count.get(update.message.chat_id, {})
    if not counts: return
    ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]
    lines = ["🏆 *Top Members*\n"]
    for i, (uid, count) in enumerate(ranked):
        name = active_members.get(update.message.chat_id, {}).get(uid, f"User {uid}")
        lines.append(f"{i + 1}. {name} — {count} msgs")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def my_badges(update, context):
    user_badges = badges.get(update.message.chat_id, {}).get(update.message.from_user.id, set())
    text = "🏅 Your Badges:\n" + "\n".join(user_badges) if user_badges else "No badges yet."
    await update.message.reply_text(text)

async def set_birthday(update, context):
    if context.args:
        birthdays[update.message.chat_id][update.message.from_user.id] = context.args[0]
        await update.message.reply_text(f"✅ Birthday set to {context.args[0]}")

# ============================================================
# GAMES (Trivia using NVIDIA, Tic-Tac-Toe, Math, WordChain)
# ============================================================
async def generate_trivia_question():
    if not nvidia_client: return None
    prompt = "Generate one fun trivia question. Format:\nQ: <question>\nA: <A>\nB: <B>\nC: <C>\nD: <D>\nCORRECT: <letter>"
    try:
        def fetch_trivia():
            return nvidia_client.chat.completions.create(
                model=NVIDIA_MODEL, messages=[{"role": "user", "content": prompt}],
                max_tokens=200, timeout=NVIDIA_TIMEOUT_SECONDS
            ).choices[0].message.content

        raw = await asyncio.to_thread(fetch_trivia)
        data = {}
        for line in raw.strip().split('\n'):
            if line.startswith("Q:"): data["question"] = line[2:].strip()
            elif line.startswith("A:"): data["A"] = line[2:].strip()
            elif line.startswith("B:"): data["B"] = line[2:].strip()
            elif line.startswith("C:"): data["C"] = line[2:].strip()
            elif line.startswith("D:"): data["D"] = line[2:].strip()
            elif line.startswith("CORRECT:"): data["correct"] = line.split(":")[1].strip().upper()[:1]
        if all(k in data for k in ["question", "A", "B", "C", "D", "correct"]): return data
    except Exception as e:
        log.error(f"Trivia error: {repr(e)}")
    return None

async def start_trivia(update, context):
    chat_id = update.message.chat_id
    if trivia_sessions.get(chat_id, {}).get("active"): return
    q = await generate_trivia_question()
    if not q: return await update.message.reply_text("❌ Failed to generate trivia from NVIDIA.")
    trivia_sessions[chat_id] = {"question": q, "active": True, "answered_by": None, "scores": trivia_sessions.get(chat_id, {}).get("scores", defaultdict(int))}
    text = f"🎯 Trivia!\n\n{q['question']}\nA) {q['A']}\nB) {q['B']}\nC) {q['C']}\nD) {q['D']}\n\nReply A/B/C/D."
    await update.message.reply_text(text)
    context.job_queue.run_once(end_trivia, 30, data={"chat_id": chat_id})

async def end_trivia(context):
    session = trivia_sessions.get(context.job.data["chat_id"])
    if session and session["active"]:
        session["active"] = False
        await context.bot.send_message(context.job.data["chat_id"], text=f"⏰ Time up! Correct answer: {session['question']['correct']}")

async def handle_trivia_answer(update, context):
    session = trivia_sessions.get(update.message.chat_id)
    if not session or not session["active"]: return False
    answer = update.message.text.strip().upper()
    if answer not in ("A", "B", "C", "D"): return False
    if answer == session["question"]["correct"]:
        session["active"] = False
        add_coins(update.message.chat_id, update.message.from_user.id, TRIVIA_WIN_COINS)
        await update.message.reply_text(f"🎉 Correct! You won {TRIVIA_WIN_COINS} coins.")
    else:
        await update.message.reply_text("❌ Wrong!")
    return True

# TTT (existing 1v1 tic-tac-toe)
ttt_games = {}
def render_ttt(b): return InlineKeyboardMarkup([[InlineKeyboardButton({" ":"➕","X":"❌","O":"⭕"}[b[r*3+c]], callback_data=f"ttt_{r*3+c}") for c in range(3)] for r in range(3)])
def check_ttt(b):
    for a,x,y in [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]:
        if b[a]!=" " and b[a]==b[x]==b[y]: return b[a]
    return "DRAW" if " " not in b else None

async def start_tictactoe(update, context):
    if not update.message.reply_to_message: return await update.message.reply_text("Reply to someone to play TTT.")
    p1, p2 = update.message.from_user, update.message.reply_to_message.from_user
    if p1.id == p2.id or p2.is_bot: return
    ttt_games[update.message.chat_id] = {"active": True, "board": [" "]*9, "turn": "X", "px": p1.id, "po": p2.id, "nx": p1.first_name, "no": p2.first_name}
    await update.message.reply_text(f"🎮 TTT: {p1.first_name} (X) vs {p2.first_name} (O)", reply_markup=render_ttt([" "]*9))

async def handle_ttt_click(update, context):
    g = ttt_games.get(update.callback_query.message.chat_id)
    if not g or not g["active"]: return await update.callback_query.answer()
    idx = int(update.callback_query.data.split("_")[1])
    pid = g["px"] if g["turn"]=="X" else g["po"]
    if update.callback_query.from_user.id != pid: return await update.callback_query.answer("Not your turn!")
    if g["board"][idx] != " ": return await update.callback_query.answer("Cell filled!")
    g["board"][idx] = g["turn"]
    winner = check_ttt(g["board"])
    if winner:
        g["active"] = False
        text = "🤝 Draw!" if winner=="DRAW" else f"🎉 {g['nx'] if winner=='X' else g['no']} won!"
        await update.callback_query.edit_message_text(text, reply_markup=render_ttt(g["board"]))
    else:
        g["turn"] = "O" if g["turn"]=="X" else "X"
        await update.callback_query.edit_message_text(f"Turn: {g['nx'] if g['turn']=='X' else g['no']}", reply_markup=render_ttt(g["board"]))

# Word Chain & Math (existing group games)
math_blitz_sessions = {}
word_chain_sessions = {}

async def start_math_blitz(update, context):
    a, b = random.randint(2,20), random.randint(2,20)
    math_blitz_sessions[update.message.chat_id] = {"active": True, "ans": a+b}
    await update.message.reply_text(f"🧮 Math Blitz! {a} + {b} = ?")

async def handle_math_blitz_answer(update, context):
    s = math_blitz_sessions.get(update.message.chat_id)
    if not s or not s["active"]: return False
    try:
        if int(update.message.text.strip()) == s["ans"]:
            s["active"] = False
            add_coins(update.message.chat_id, update.message.from_user.id, 10)
            await update.message.reply_text("🎉 Correct! Won 10 coins.")
            return True
    except Exception:
        pass
    return False

async def start_word_chain(update, context):
    word_chain_sessions[update.message.chat_id] = {"active": True, "last": "apple", "used": {"apple"}}
    await update.message.reply_text("🔗 Word Chain! Starting word: apple. Next word must start with 'e'.")

async def handle_word_chain_answer(update, context):
    s = word_chain_sessions.get(update.message.chat_id)
    if not s or not s["active"]: return False
    text = update.message.text.strip().lower()
    if not text.isalpha() or len(text)<2 or text[0] != s["last"][-1]: return False
    if text in s["used"]:
        await update.message.reply_text("❌ Already used!")
        return True
    s["used"].add(text); s["last"] = text
    add_coins(update.message.chat_id, update.message.from_user.id, 2)
    await update.message.reply_text(f"✅ {text}. Next starts with '{text[-1]}'")
    return True

# ============================================================
# NEW MULTIPLAYER GAME 1: ROCK PAPER SCISSORS (1v1, challenge based)
# ============================================================
rps_games = {}
RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
RPS_EMOJI = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}

async def start_rps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("⚠️ Reply to someone's message with `/rps` to challenge them.", parse_mode="Markdown")
        return
    p1, p2 = update.message.from_user, update.message.reply_to_message.from_user
    if p1.id == p2.id or p2.is_bot:
        await update.message.reply_text("❌ Invalid opponent.")
        return

    chat_id = update.message.chat_id
    if chat_id in rps_games and not rps_games[chat_id].get("finished", True):
        await update.message.reply_text("⚠️ Ek RPS game already chal rahi hai is group mein.")
        return

    rps_games[chat_id] = {
        "players": {p1.id: p1.first_name, p2.id: p2.first_name},
        "choices": {},
        "finished": False,
    }
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🪨 Rock", callback_data=f"rps_{chat_id}_rock"),
        InlineKeyboardButton("📄 Paper", callback_data=f"rps_{chat_id}_paper"),
        InlineKeyboardButton("✂️ Scissors", callback_data=f"rps_{chat_id}_scissors"),
    ]])
    await update.message.reply_text(
        f"✊ *Rock Paper Scissors!*\n{p1.first_name} vs {p2.first_name}\n\nBoth players, pick your move (private tap):",
        parse_mode="Markdown", reply_markup=keyboard
    )

    async def _timeout():
        await asyncio.sleep(GAME_TIMEOUT_SECONDS)
        g = rps_games.get(chat_id)
        if g and not g["finished"]:
            g["finished"] = True
            try:
                await context.bot.send_message(chat_id, "⏰ RPS timed out, game cancelled.")
            except Exception:
                pass
    asyncio.create_task(_timeout())

async def handle_rps_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, chat_id_str, choice = query.data.split("_")
    chat_id = int(chat_id_str)
    user = query.from_user

    g = rps_games.get(chat_id)
    if not g or g["finished"]:
        await query.answer("Yeh game khatam ho gayi.", show_alert=True)
        return
    if user.id not in g["players"]:
        await query.answer("Yeh game tumhare liye nahi hai.", show_alert=True)
        return
    if user.id in g["choices"]:
        await query.answer("Tum already choose kar chuke ho!", show_alert=True)
        return

    g["choices"][user.id] = choice
    await query.answer(f"Tumne {choice} choose kiya ✅")

    if len(g["choices"]) < 2:
        return

    g["finished"] = True
    (p1_id, p1_choice), (p2_id, p2_choice) = list(g["choices"].items())
    p1_name, p2_name = g["players"][p1_id], g["players"][p2_id]

    if p1_choice == p2_choice:
        result_text = f"🤝 Draw! Dono ne {RPS_EMOJI[p1_choice]} choose kiya."
    elif RPS_BEATS[p1_choice] == p2_choice:
        add_coins(chat_id, p1_id, RPS_WIN_COINS)
        result_text = f"🏆 {p1_name} won! {RPS_EMOJI[p1_choice]} beats {RPS_EMOJI[p2_choice]} (+{RPS_WIN_COINS} coins)"
    else:
        add_coins(chat_id, p2_id, RPS_WIN_COINS)
        result_text = f"🏆 {p2_name} won! {RPS_EMOJI[p2_choice]} beats {RPS_EMOJI[p1_choice]} (+{RPS_WIN_COINS} coins)"

    await query.edit_message_text(
        f"✊ *Rock Paper Scissors — Result*\n\n"
        f"{p1_name}: {RPS_EMOJI[p1_choice]}\n{p2_name}: {RPS_EMOJI[p2_choice]}\n\n{result_text}",
        parse_mode="Markdown"
    )

# ============================================================
# NEW MULTIPLAYER GAME 2: TRIVIA RACE (group, button based, first correct wins)
# ============================================================
TRIVIA_RACE_QUESTIONS = [
    {"q": "Capital of Japan kya hai?", "options": ["Seoul", "Tokyo", "Beijing", "Bangkok"], "correct": 1},
    {"q": "2 + 2 x 2 = ?", "options": ["6", "8", "4", "2"], "correct": 0},
    {"q": "Sabse bada planet kaunsa hai?", "options": ["Earth", "Mars", "Jupiter", "Saturn"], "correct": 2},
    {"q": "HTML ka full form?", "options": ["HyperText Markup Language", "High Text Markup Lang", "Home Tool ML", "None"], "correct": 0},
    {"q": "Python kis type ki language hai?", "options": ["Compiled only", "Interpreted", "Assembly", "Machine code"], "correct": 1},
    {"q": "Kitne continents hain duniya mein?", "options": ["5", "6", "7", "8"], "correct": 2},
]

trivia_race_sessions = {}

async def start_trivia_race(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if chat_id in trivia_race_sessions and not trivia_race_sessions[chat_id].get("finished", True):
        await update.message.reply_text("⚠️ Trivia race already chal rahi hai.")
        return

    q = random.choice(TRIVIA_RACE_QUESTIONS)
    trivia_race_sessions[chat_id] = {"question": q, "finished": False}

    buttons = [[InlineKeyboardButton(opt, callback_data=f"tr_{chat_id}_{i}")] for i, opt in enumerate(q["options"])]
    await update.message.reply_text(
        f"⚡ *Trivia Race!* First correct answer wins {TRIVIA_RACE_WIN_COINS} coins.\n\n❓ {q['q']}",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons)
    )

    async def _timeout():
        await asyncio.sleep(GAME_TIMEOUT_SECONDS)
        s = trivia_race_sessions.get(chat_id)
        if s and not s["finished"]:
            s["finished"] = True
            try:
                await context.bot.send_message(chat_id, "⏰ Trivia race timed out, koi jeeta nahi.")
            except Exception:
                pass
    asyncio.create_task(_timeout())

async def handle_trivia_race_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, chat_id_str, idx_str = query.data.split("_")
    chat_id, idx = int(chat_id_str), int(idx_str)
    user = query.from_user

    s = trivia_race_sessions.get(chat_id)
    if not s or s["finished"]:
        await query.answer("Yeh race khatam ho gayi.", show_alert=True)
        return

    if idx == s["question"]["correct"]:
        s["finished"] = True
        add_coins(chat_id, user.id, TRIVIA_RACE_WIN_COINS)
        await query.answer("Correct! 🎉")
        await query.edit_message_text(
            f"🏆 *{user.first_name} won the Trivia Race!* +{TRIVIA_RACE_WIN_COINS} coins 🎉\n\n"
            f"❓ {s['question']['q']}\n✅ Answer: {s['question']['options'][s['question']['correct']]}",
            parse_mode="Markdown"
        )
    else:
        await query.answer("❌ Galat jawaab, koi aur try kare!", show_alert=True)

# ============================================================
# NEW MULTIPLAYER GAME 3: MATH DUEL (1v1, challenge based, button answers)
# ============================================================
math_duel_games = {}

def _gen_math_duel_problem():
    a, b = random.randint(2, 20), random.randint(2, 20)
    op = random.choice(["+", "-", "*"])
    answer = {"+": a + b, "-": a - b, "*": a * b}[op]
    return f"{a} {op} {b}", answer

async def start_math_duel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if not update.message.reply_to_message:
        await update.message.reply_text("⚠️ Reply to someone's message with `/mathduel` to challenge them.", parse_mode="Markdown")
        return

    challenger = update.message.from_user
    target = update.message.reply_to_message.from_user
    if target.id == challenger.id or target.is_bot:
        await update.message.reply_text("❌ Invalid opponent.")
        return

    if chat_id in math_duel_games and not math_duel_games[chat_id].get("finished", True):
        await update.message.reply_text("⚠️ Math duel already chal rahi hai is group mein.")
        return

    problem, answer = _gen_math_duel_problem()
    wrong_answers = set()
    while len(wrong_answers) < 3:
        delta = random.choice([-5, -3, -2, -1, 1, 2, 3, 5])
        wrong = answer + delta
        if wrong != answer:
            wrong_answers.add(wrong)
    options = list(wrong_answers) + [answer]
    random.shuffle(options)

    math_duel_games[chat_id] = {
        "answer": answer, "finished": False,
        "players": {challenger.id: challenger.first_name, target.id: target.first_name}
    }

    buttons = [[InlineKeyboardButton(str(opt), callback_data=f"md_{chat_id}_{opt}")] for opt in options]
    await update.message.reply_text(
        f"🧮 *Math Blitz Duel!*\n{challenger.first_name} vs {target.first_name}\n\n"
        f"First to answer correctly wins {MATH_DUEL_WIN_COINS} coins.\n\n❓ {problem} = ?",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons)
    )

    async def _timeout():
        await asyncio.sleep(GAME_TIMEOUT_SECONDS)
        g = math_duel_games.get(chat_id)
        if g and not g["finished"]:
            g["finished"] = True
            try:
                await context.bot.send_message(chat_id, "⏰ Math duel timed out, koi jeeta nahi.")
            except Exception:
                pass
    asyncio.create_task(_timeout())

async def handle_math_duel_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, chat_id_str, val_str = query.data.split("_")
    chat_id, val = int(chat_id_str), int(val_str)
    user = query.from_user

    g = math_duel_games.get(chat_id)
    if not g or g["finished"]:
        await query.answer("Yeh duel khatam ho gayi.", show_alert=True)
        return
    if user.id not in g["players"]:
        await query.answer("Yeh duel tumhare liye nahi hai.", show_alert=True)
        return

    if val == g["answer"]:
        g["finished"] = True
        add_coins(chat_id, user.id, MATH_DUEL_WIN_COINS)
        await query.answer("Correct! 🎉")
        await query.edit_message_text(
            f"🏆 *{user.first_name} won the Math Duel!* +{MATH_DUEL_WIN_COINS} coins 🎉\n\n✅ Answer was: {g['answer']}",
            parse_mode="Markdown"
        )
    else:
        await query.answer("❌ Galat! Doosra try kare.", show_alert=True)

# ============================================================
# MAIN MESSAGE HANDLER
# ============================================================
async def check_flood(update, context):
    chat_id, uid = update.message.chat_id, update.message.from_user.id
    message_log[chat_id][uid].append(time.time())
    if len(message_log[chat_id][uid]) >= FLOOD_MSG_LIMIT and (time.time() - message_log[chat_id][uid][0]) <= FLOOD_WINDOW_SECONDS:
        try:
            await context.bot.restrict_chat_member(chat_id, uid, permissions=ChatPermissions(can_send_messages=False), until_date=int(time.time())+(FLOOD_MUTE_MINUTES*60))
            message_log[chat_id][uid].clear()
            await update.message.reply_text("🚨 Flooding! Muted.")
            return True
        except Exception as e:
            log.error(f"check_flood mute failed: {repr(e)}")
    return False

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    text, chat_type, user, chat_id = update.message.text.lower(), update.message.chat.type, update.message.from_user, update.message.chat_id
    known_chats.add(chat_id)

    if chat_type in ["group", "supergroup"] and not user.is_bot:
        if await handle_trivia_answer(update, context): return
        if await handle_math_blitz_answer(update, context): return
        if await handle_word_chain_answer(update, context): return

        active_members[chat_id][user.id] = user.first_name
        message_count[chat_id][user.id] += 1
        add_coins(chat_id, user.id, COINS_PER_MESSAGE)
        update_streak(chat_id, user.id)
        await check_message_badges(chat_id, user.id, context)

        if not await is_group_admin(update, context) and await check_flood(update, context): return

    if not user.is_bot: chat_history[chat_id].append(update.message.text)

    bot_uname = context.bot.username.lower() if context.bot.username else ""
    is_reply = update.message.reply_to_message and update.message.reply_to_message.from_user.id == context.bot.id

    if chat_type == "private" or (bot_uname in text or is_reply):
        stop_typing = asyncio.Event()

        async def _keep_typing():
            while not stop_typing.is_set():
                try:
                    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(stop_typing.wait(), timeout=4)
                except asyncio.TimeoutError:
                    pass

        typing_task = asyncio.create_task(_keep_typing())
        try:
            reply = await get_ai_reply(update.message.text, list(chat_history[chat_id]), persona.get(chat_id))
            if not reply:
                reply = "🤖 Kuch gadbad ho gayi, dobara try karo."
            if len(reply) > 4000:
                reply = reply[:4000] + "…"
            await update.message.reply_text(reply)
        except Exception as e:
            log.error(f"AI failed: {repr(e)}")
            try:
                await update.message.reply_text("❌ AI reply bhejte waqt error aaya. Dobara try karo.")
            except Exception as e2:
                log.error(f"Fallback reply also failed: {repr(e2)}")
        finally:
            stop_typing.set()
            typing_task.cancel()

# ============================================================
# ERROR HANDLER
# ============================================================
async def error_handler(update, context):
    log.error(f"Error: {repr(context.error)}")

# ============================================================
# MAIN
# ============================================================
def main():
    threading.Thread(target=run_dummy_server, daemon=True).start()
    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("testai", test_ai))
    app.add_handler(CommandHandler("adminpanel", admin_panel))
    app.add_handler(CommandHandler("addadmin", add_admin))
    app.add_handler(CommandHandler("removeadmin", remove_admin))
    app.add_handler(CommandHandler("listadmins", list_admins))
    app.add_handler(CommandHandler("setwelcome", set_welcome))
    app.add_handler(CommandHandler("setrules", set_rules))
    app.add_handler(CommandHandler("rules", show_rules))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("setpersona", set_persona))
    app.add_handler(CommandHandler("resetpersona", reset_persona))
    app.add_handler(CommandHandler("setdelay", set_delay))
    app.add_handler(CommandHandler("autoapprove", toggle_autoapprove))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("shop", shop))
    app.add_handler(CommandHandler("buy", buy_item))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("mystreak", my_streak))
    app.add_handler(CommandHandler("mybadges", my_badges))
    app.add_handler(CommandHandler("topmembers", top_members))
    app.add_handler(CommandHandler("setbirthday", set_birthday))

    # Existing games
    app.add_handler(CommandHandler("trivia", start_trivia))
    app.add_handler(CommandHandler("tictactoe", start_tictactoe))
    app.add_handler(CommandHandler("mathblitz", start_math_blitz))
    app.add_handler(CommandHandler("wordchain", start_word_chain))

    # New multiplayer games
    app.add_handler(CommandHandler("rps", start_rps))
    app.add_handler(CommandHandler("triviarace", start_trivia_race))
    app.add_handler(CommandHandler("mathduel", start_math_duel))

    app.add_handler(CommandHandler("genpromo", gen_promo))
    app.add_handler(CommandHandler("redeem", redeem))

    app.add_handler(CommandHandler("mute", mute_user))
    app.add_handler(CommandHandler("unmute", unmute_user))
    app.add_handler(CommandHandler("kick", kick_user))
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("warn", warn_user))
    app.add_handler(CommandHandler("unwarn", unwarn_user))

    # Events
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))
    app.add_error_handler(error_handler)

    log.info("🚀 Prime X Assistant (NVIDIA ONLY) starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
