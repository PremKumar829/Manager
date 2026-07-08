import os
import time
import asyncio
import logging
import tempfile
import threading
import requests
import itertools
from datetime import datetime, timedelta, time as dt_time
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

# MULTI-KEY SETUP: Accepts comma-separated keys from .env
GEMINI_API_KEYS_STR = os.getenv("GEMINI_API_KEYS", "")
GEMINI_KEYS = [k.strip() for k in GEMINI_API_KEYS_STR.split(",") if k.strip()]
api_key_cycle = itertools.cycle(GEMINI_KEYS) if GEMINI_KEYS else None

OWNER_NAME = "@PREMGUPTA2M"
CHANNEL_LINK = "https://t.me/+Gouc7PsDosk4MTRl"
GROUP_LINK = "https://t.me/+rSqVXbRig4BjOTc1"

# Models to try in order
GEMINI_MODEL_CHAIN = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-001"]

MAX_WARNINGS = 3          
FLOOD_MSG_LIMIT = 6       
FLOOD_WINDOW_SECONDS = 8  
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
# MULTI-KEY CLIENT GENERATOR
# ============================================================
def get_ai_client():
    """Returns a new Gemini client using the next API key in the cycle."""
    if not GEMINI_KEYS:
        return None
    return genai.Client(api_key=next(api_key_cycle))

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

TAG_BATCH_SIZE = 5           
TAG_BATCH_DELAY = 1.5        


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
def is_admin(update: Update) -> bool:
    if not update.message:
        return False
    if update.message.from_user and update.message.from_user.id == ADMIN_ID:
        return True
    if update.message.sender_chat and update.message.sender_chat.id == update.message.chat_id:
        return True
    return False

async def is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if is_admin(update):
        return True
    try:
        member = await context.bot.get_chat_member(update.message.chat_id, update.message.from_user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

def get_target_user(update: Update):
    if update.message.reply_to_message:
        return update.message.reply_to_message.from_user
    return None


# ============================================================
# AI REPLY WITH FALLBACK CHAIN & KEY ROTATION
# ============================================================
async def get_ai_reply(prompt: str, style_context: list = None, persona_text: str = None) -> str:
    if not GEMINI_KEYS:
        return "❌ Bhai, GEMINI_API_KEYS set nahi hai .env mein!"

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
        for attempt in range(2): 
            client = get_ai_client() # Get a fresh API key for each attempt
            try:
                def fetch_response():
                    response = client.models.generate_content(
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
                    await asyncio.sleep(2)  
                continue

    log.error(f"All Gemini models & keys failed. Last error: {repr(last_error)}")
    return "❌ AI abhi thoda busy hai, thodi der me try karo! (Run /testai to see the exact error)"

async def test_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not GEMINI_KEYS:
        await update.message.reply_text("❌ GEMINI_API_KEYS is missing from .env.")
        return

    await update.message.reply_text("🔧 Testing all models with rotated keys, ek second...")
    results = []
    for model_name in GEMINI_MODEL_CHAIN:
        client = get_ai_client()
        try:
            def fetch():
                r = client.models.generate_content(model=model_name, contents="Say hi in one word.")
                return r.text
            reply = await asyncio.to_thread(fetch)
            results.append(f"✅ {model_name} → {reply.strip()[:60]}")
        except Exception as e:
            results.append(f"❌ {model_name} → {repr(e)[:150]}")

    await update.message.reply_text("🔧 *AI Diagnostic Results:*\n\n" + "\n\n".join(results), parse_mode="Markdown")

# ============================================================
# START MENU & COMMANDS
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
        "• /shop, /buy <item> <text> — Spend coins\n"
        "• /mystreak — Your daily activity streak\n"
        "• /trivia — Start a multiplayer trivia round 🎯\n"
        "• /trivialeaderboard — Trivia win rankings\n"
        "• Send a voice note — I'll transcribe it 🎙️\n\n"
        "*Admin only*\n"
        "• /testai — Diagnose AI errors\n"
        "• /setrules <text> — Set group rules\n"
        "• /setwelcome <msg> — Custom welcome message\n"
        "• /setpersona <desc> / /resetpersona — AI personality\n"
        "• /automod on|off — AI toxicity detection\n"
        "• /autoapprove on|off — Join-request auto-approval\n"
        "• /setdelay <seconds> — Join-request delay\n"
        "• /slowmode <seconds> — Limit messages per user\n"
        "• /addbadword, /removebadword, /badwords — Manage words\n"
        "• /allowlink, /disallowlink, /listlinks — Manage links\n"
        "• /addstickerreact <keyword> — Auto-react with sticker\n"
        "• /removestickerreact <keyword>\n"
        "• /tagall <msg> — Tag all known members\n"
        "• /tagadmins <msg> — Tag all group admins\n"
        "• /call <msg> — Ping one member\n"
        "• /warn, /unwarn, /mute, /unmute, /kick, /ban, /unban\n"
        "• /pin (reply) — Pin a message\n"
        "• /broadcast <msg> — Send message to all groups\n\n"
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
async def set_persona(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("⚠️ Usage: `/setpersona <description>`", parse_mode="Markdown")
        return
    persona[update.message.chat_id] = text
    await update.message.reply_text("✅ Bot persona updated for this group!")

async def reset_persona(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    persona.pop(update.message.chat_id, None)
    await update.message.reply_text("✅ Persona reset to default.")

async def set_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    chat_id = update.message.chat_id
    try:
        new_time = int(context.args[0])
        if new_time < 0: raise ValueError
        approval_settings[chat_id]["delay"] = new_time
        await update.message.reply_text(f"✅ Auto-approval delay set to **{new_time} second(s)**.", parse_mode="Markdown")
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Usage: `/setdelay <seconds>`", parse_mode="Markdown")

async def toggle_autoapprove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    chat_id = update.message.chat_id
    if not context.args or context.args[0].lower() not in ("on", "off"):
        current = "ON ✅" if approval_settings[chat_id]["enabled"] else "OFF ❌"
        await update.message.reply_text(f"⚙️ Auto-approval is **{current}**.\nUsage: `/autoapprove on` or `off`", parse_mode="Markdown")
        return
    approval_settings[chat_id]["enabled"] = context.args[0].lower() == "on"
    await update.message.reply_text(f"⚙️ Auto-approval {'enabled ✅' if approval_settings[chat_id]['enabled'] else 'disabled ❌'}.")

async def set_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        return
    text = " ".join(context.args)
    if not text: return
    group_rules[update.message.chat_id] = text
    await update.message.reply_text("✅ Rules updated!")

async def show_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📜 *Group Rules:*\n\n{group_rules[update.message.chat_id]}", parse_mode="Markdown")

async def tag_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context): return
    chat_id = update.message.chat_id
    if not active_members.get(chat_id):
        await update.message.reply_text("⏳ No member data yet.")
        return
    custom_msg = " ".join(context.args) or "📢 Dhyan dein!"
    members = list(active_members[chat_id].items())
    status = await update.message.reply_text("📨 Tagging members...")
    for i in range(0, len(members), TAG_BATCH_SIZE):
        batch = members[i:i + TAG_BATCH_SIZE]
        mentions = " ".join(f"[{fname}](tg://user?id={uid})" for uid, fname in batch)
        text_to_send = f"{custom_msg}\n\n{mentions}"
        try:
            if update.message.reply_to_message:
                await update.message.reply_to_message.reply_text(text_to_send, parse_mode="Markdown")
            else:
                await context.bot.send_message(chat_id=chat_id, text=text_to_send, parse_mode="Markdown")
        except Exception: pass
        await asyncio.sleep(TAG_BATCH_DELAY)
    try: await status.delete()
    except Exception: pass

async def tag_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context): return
    try:
        admins = await context.bot.get_chat_administrators(update.message.chat_id)
        mentions = " ".join(f"[{a.user.first_name}](tg://user?id={a.user.id})" for a in admins if not a.user.is_bot)
        if mentions:
            await context.bot.send_message(update.message.chat_id, f"📢 Admins!\n\n{mentions}", parse_mode="Markdown")
    except Exception: pass

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    text = (
        "📊 *Group Stats*\n\n"
        f"• Tracked members: {len(active_members.get(chat_id, {}))}\n"
        f"• Warned users: {sum(1 for c in warnings.get(chat_id, {}).values() if c > 0)}\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# ============================================================
# MODERATION COMMANDS
# ============================================================
async def apply_warning(chat_id: int, target_user, context: ContextTypes.DEFAULT_TYPE, reason: str = "") -> str:
    warnings[chat_id][target_user.id] += 1
    count = warnings[chat_id][target_user.id]
    suffix = f" ({reason})" if reason else ""
    if count >= MAX_WARNINGS:
        try:
            await context.bot.restrict_chat_member(chat_id, target_user.id, permissions=ChatPermissions(can_send_messages=False))
            warnings[chat_id][target_user.id] = 0
            return f"🔇 {target_user.first_name} muted after {MAX_WARNINGS} warnings.{suffix}"
        except Exception as e: return f"⚠️ Mute failed: {e}"
    return f"⚠️ {target_user.first_name} warned ({count}/{MAX_WARNINGS}).{suffix}"

async def warn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context): return
    target = get_target_user(update)
    if target: await update.message.reply_text(await apply_warning(update.message.chat_id, target, context))

async def unwarn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context): return
    target = get_target_user(update)
    if target and warnings[update.message.chat_id][target.id] > 0:
        warnings[update.message.chat_id][target.id] -= 1
        await update.message.reply_text("✅ Warning removed.")

async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context): return
    target = get_target_user(update)
    if not target: return
    minutes = int(context.args[0]) if context.args else None
    until = int(time.time()) + (minutes * 60) if minutes else None
    try:
        await context.bot.restrict_chat_member(update.message.chat_id, target.id, permissions=ChatPermissions(can_send_messages=False), until_date=until)
        await update.message.reply_text(f"🔇 {target.first_name} muted.")
    except Exception as e: await update.message.reply_text(f"⚠️ Mute failed: {e}")

async def unmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context): return
    target = get_target_user(update)
    if target:
        try:
            await context.bot.restrict_chat_member(update.message.chat_id, target.id, permissions=ChatPermissions(can_send_messages=True, can_send_other_messages=True))
            await update.message.reply_text(f"🔊 {target.first_name} unmuted.")
        except Exception: pass

async def kick_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context): return
    target = get_target_user(update)
    if target:
        try:
            await context.bot.ban_chat_member(update.message.chat_id, target.id)
            await context.bot.unban_chat_member(update.message.chat_id, target.id)
            await update.message.reply_text(f"👋 {target.first_name} kicked.")
        except Exception: pass

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context): return
    target = get_target_user(update)
    if target:
        try:
            await context.bot.ban_chat_member(update.message.chat_id, target.id)
            await update.message.reply_text(f"🚫 {target.first_name} banned.")
        except Exception: pass

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context): return
    if context.args:
        try:
            await context.bot.unban_chat_member(update.message.chat_id, int(context.args[0]))
            await update.message.reply_text(f"✅ User unbanned.")
        except Exception: pass

async def pin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await is_group_admin(update, context) and update.message.reply_to_message:
        try:
            await context.bot.pin_chat_message(update.message.chat_id, update.message.reply_to_message.message_id)
            await update.message.reply_text("📌 Pinned.")
        except Exception: pass

# ============================================================
# SETTINGS & FILTERS
# ============================================================
async def add_bad_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await is_group_admin(update, context) and context.args:
        bad_words[update.message.chat_id].add(" ".join(context.args).lower())
        await update.message.reply_text("✅ Word added to filter.")

async def remove_bad_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await is_group_admin(update, context) and context.args:
        bad_words[update.message.chat_id].discard(" ".join(context.args).lower())
        await update.message.reply_text("✅ Word removed.")

async def list_bad_words(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await is_group_admin(update, context):
        words = bad_words.get(update.message.chat_id, set())
        await update.message.reply_text(f"📝 Words: {', '.join(words)}" if words else "No filtered words.")

async def set_slowmode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await is_group_admin(update, context) and context.args:
        try:
            slow_mode[update.message.chat_id] = int(context.args[0])
            await update.message.reply_text(f"🐢 Slow mode: {context.args[0]}s.")
        except ValueError: pass

async def set_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await is_group_admin(update, context) and context.args:
        custom_welcome[update.message.chat_id] = " ".join(context.args)
        await update.message.reply_text("✅ Welcome message updated.")

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    for m in update.message.new_chat_members:
        if not m.is_bot:
            txt = custom_welcome.get(chat_id, "Welcome {name}!").replace("{name}", m.first_name)
            try: await update.message.reply_text(txt)
            except Exception: pass

async def allow_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await is_group_admin(update, context) and context.args:
        link_whitelist[update.message.chat_id].add(context.args[0].lower())
        await update.message.reply_text("✅ Link allowed.")

async def disallow_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await is_group_admin(update, context) and context.args:
        link_whitelist[update.message.chat_id].discard(context.args[0].lower())
        await update.message.reply_text("✅ Link removed.")

async def list_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await is_group_admin(update, context):
        entries = link_whitelist.get(update.message.chat_id, set())
        await update.message.reply_text(f"🔗 Allowed: {', '.join(entries)}" if entries else "No custom links.")

async def add_sticker_react(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await is_group_admin(update, context) and context.args and update.message.reply_to_message and update.message.reply_to_message.sticker:
        sticker_replies[update.message.chat_id][" ".join(context.args).lower()] = update.message.reply_to_message.sticker.file_id
        await update.message.reply_text("✅ Sticker react added.")

async def remove_sticker_react(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await is_group_admin(update, context) and context.args:
        sticker_replies[update.message.chat_id].pop(" ".join(context.args).lower(), None)
        await update.message.reply_text("✅ Sticker react removed.")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID: return
    text = " ".join(context.args)
    if not text: return
    sent = 0
    for chat_id in list(known_chats):
        try:
            await context.bot.send_message(chat_id, f"📢 {text}")
            sent += 1
        except Exception: pass
    await update.message.reply_text(f"✅ Broadcast sent to {sent} chat(s).")

# ============================================================
# ECONOMY & SHOP
# ============================================================
def add_coins(chat_id: int, user_id: int, amount: int):
    coins[chat_id][user_id] += amount

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"💰 Balance: {coins[update.message.chat_id][update.message.from_user.id]} coins")

async def shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🛒 Shop: \n`/buy title <text>` - 200 coins\n`/buy shoutout <text>` - 50 coins", parse_mode="Markdown")

async def buy_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    chat_id, user = update.message.chat_id, update.message.from_user
    item = context.args[0].lower()
    cost = SHOP_ITEMS.get(item, 999999)
    if coins[chat_id][user.id] >= cost:
        coins[chat_id][user.id] -= cost
        if item == "title" and len(context.args) > 1:
            custom_titles[chat_id][user.id] = " ".join(context.args[1:])
            await update.message.reply_text("✅ Title set!")
        elif item == "shoutout":
            await context.bot.send_message(chat_id, f"📣 SHOUTOUT: {' '.join(context.args[1:])}")
    else:
        await update.message.reply_text("❌ Not enough coins.")

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid, cid = update.message.from_user.id, update.message.chat_id
    title = custom_titles.get(cid, {}).get(uid, "")
    st = streaks.get(cid, {}).get(uid, {"count": 0})
    await update.message.reply_text(f"👤 *{update.message.from_user.first_name}*\nTitle: {title}\nCoins: {coins[cid][uid]}\nStreak: {st['count']}", parse_mode="Markdown")

async def top_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    counts = message_count.get(update.message.chat_id, {})
    if counts:
        ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]
        lines = [f"{i+1}. User {uid} — {c} msgs" for i, (uid, c) in enumerate(ranked)]
        await update.message.reply_text("\n".join(lines))

# ============================================================
# SMART FEATURES (AI, TRIVIA, VOICE)
# ============================================================
async def toggle_automod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await is_group_admin(update, context) and context.args:
        automod_enabled[update.message.chat_id] = context.args[0].lower() == "on"
        await update.message.reply_text(f"⚙️ Auto-mod is now {'ON' if automod_enabled[update.message.chat_id] else 'OFF'}.")

async def ai_is_toxic(text: str) -> bool:
    if not GEMINI_KEYS: return False
    try:
        client = get_ai_client()
        r = await asyncio.to_thread(lambda: client.models.generate_content(
            model=GEMINI_MODEL_CHAIN[0],
            contents="Moderate this Hinglish chat. Reply YES if genuinely toxic/harassment, else NO.\n" + text
        ).text.strip().upper())
        return r.startswith("YES")
    except Exception: return False

async def transcribe_voice(file_path: str) -> str:
    if not GEMINI_KEYS: return "❌ AI Key missing."
    try:
        client = get_ai_client()
        def fetch():
            uploaded = client.files.upload(file=file_path)
            return client.models.generate_content(
                model=GEMINI_MODEL_CHAIN[0],
                contents=["Transcribe this strictly in spoken language.", uploaded]
            ).text
        return await asyncio.to_thread(fetch)
    except Exception as e: return f"❌ Failed: {e}"

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.voice: return
    if update.message.chat.type in ["group", "supergroup"] and not (update.message.reply_to_message and update.message.reply_to_message.from_user.id == context.bot.id):
        return
    tmp_path = None
    try:
        await context.bot.send_chat_action(update.message.chat_id, "typing")
        v = await update.message.voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp: tmp_path = tmp.name
        await v.download_to_drive(tmp_path)
        await update.message.reply_text(f"🎙️: {await transcribe_voice(tmp_path)}")
    finally:
        if tmp_path and os.path.exists(tmp_path): os.remove(tmp_path)

async def start_trivia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.message.chat_id
    if trivia_sessions.get(cid, {}).get("active"): return
    if not GEMINI_KEYS: 
        await update.message.reply_text("❌ AI API Key required.")
        return

    await update.message.reply_text("🎯 Generating trivia...")
    try:
        client = get_ai_client()
        raw = await asyncio.to_thread(lambda: client.models.generate_content(
            model=GEMINI_MODEL_CHAIN[0],
            contents="Generate 1 trivia question. Format:\nQ: <question>\nA: <option>\nB: <option>\nC: <option>\nD: <option>\nCORRECT: <letter>"
        ).text)
        d = {line.split(":", 1)[0].strip(): line.split(":", 1)[1].strip() for line in raw.strip().split("\n") if ":" in line}
        if "Q" in d and "CORRECT" in d:
            trivia_sessions[cid] = {"q": d, "active": True, "ans_by": None, "scores": trivia_sessions.get(cid, {}).get("scores", defaultdict(int))}
            await update.message.reply_text(f"🎯 {d['Q']}\nA) {d.get('A')}\nB) {d.get('B')}\nC) {d.get('C')}\nD) {d.get('D')}\n\nReply A/B/C/D! (30s)")
            context.job_queue.run_once(lambda c: trivia_sessions[cid].update({"active": False}) if not trivia_sessions[cid].get("ans_by") else None, 30)
    except Exception: await update.message.reply_text("❌ Failed.")

async def handle_trivia_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    s = trivia_sessions.get(update.message.chat_id)
    if not s or not s.get("active"): return False
    ans = update.message.text.strip().upper()
    if ans in ["A", "B", "C", "D"]:
        if ans == s["q"]["CORRECT"][:1]:
            s["active"] = False
            s["ans_by"] = update.message.from_user.id
            s["scores"][update.message.from_user.id] += 1
            add_coins(update.message.chat_id, update.message.from_user.id, TRIVIA_WIN_COINS)
            await update.message.reply_text(f"🎉 Correct {update.message.from_user.first_name}! +{TRIVIA_WIN_COINS} coins")
        else: await update.message.reply_text("❌ Galat!")
        return True
    return False

# ============================================================
# JOIN & MESSAGE HANDLERS
# ============================================================
async def join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = approval_settings[update.chat_join_request.chat.id]
    if s["enabled"]:
        await asyncio.sleep(s["delay"])
        try: await update.chat_join_request.approve()
        except Exception: pass

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    text, chat_type, user, cid = update.message.text.lower(), update.message.chat.type, update.message.from_user, update.message.chat_id
    known_chats.add(cid)

    if chat_type in ["group", "supergroup"] and not user.is_bot:
        if await handle_trivia_answer(update, context): return
        active_members[cid][user.id] = user.first_name
        message_count[cid][user.id] += 1
        add_coins(cid, user.id, COINS_PER_MESSAGE)
        chat_history[cid].append(update.message.text)

    is_grp_admin = await is_group_admin(update, context)

    if chat_type in ["group", "supergroup"] and not is_grp_admin:
        if automod_enabled[cid] and len(update.message.text.split()) >= 4:
            if await ai_is_toxic(update.message.text):
                try: await update.message.delete()
                except Exception: pass
                await context.bot.send_message(cid, await apply_warning(cid, user, context, "AI flagged"))
                return

    bot_user = context.bot.username.lower() if context.bot.username else ""
    if chat_type == "private" or (bot_user in text or (update.message.reply_to_message and update.message.reply_to_message.from_user.id == context.bot.id)):
        await context.bot.send_chat_action(cid, "typing")
        await update.message.reply_text(await get_ai_reply(update.message.text, list(chat_history[cid]), persona.get(cid)))

# ============================================================
# MAIN
# ============================================================
def main():
    if not TOKEN: log.warning("BOT_TOKEN is missing.")
    if not GEMINI_KEYS: log.warning("GEMINI_API_KEYS is missing. AI won't work.")

    threading.Thread(target=run_dummy_server, daemon=True).start()
    app = Application.builder().token(TOKEN).build()

    cmds = [
        ("start", start), ("help", help_command), ("rules", show_rules), ("stats", stats),
        ("testai", test_ai), ("balance", balance), ("shop", shop), ("buy", buy_item),
        ("profile", profile), ("trivia", start_trivia), ("setrules", set_rules),
        ("setwelcome", set_welcome), ("setpersona", set_persona), ("automod", toggle_automod),
        ("autoapprove", toggle_autoapprove), ("setdelay", set_delay), ("slowmode", set_slowmode),
        ("addbadword", add_bad_word), ("allowlink", allow_link), ("broadcast", broadcast),
        ("warn", warn_user), ("unwarn", unwarn_user), ("mute", mute_user), ("unmute", unmute_user),
        ("kick", kick_user), ("ban", ban_user), ("unban", unban_user), ("pin", pin_message)
    ]
    for name, func in cmds: app.add_handler(CommandHandler(name, func))

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(ChatJoinRequestHandler(join_request))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    log.info("🚀 Bot with Key Rotation starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
