import os
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import google.generativeai as genai
from dotenv import load_dotenv
from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, ChatJoinRequestHandler
)

# --- CONFIGURATION ---
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

OWNER_NAME = "@PREMGUPTA2M"
CHANNEL_LINK = "https://t.me/+Gouc7PsDosk4MTRl" # Yahan apna link dalein
GROUP_LINK = "https://t.me/+rSqVXbRig4BjOTc1"     # Yahan apna link dalein

# AI Setup (Robust Checking & Stable Model Fix)
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    # 404 Error fix: Using gemini-pro instead of gemini-1.5-flash for older SDK compatibility
    model = genai.GenerativeModel('gemini-pro')
else:
    model = None

# Global Variables
group_rules = "Group ke rules abhi set nahi hain."
active_members = {}
approval_delay = 5  # Default delay 5 seconds

# --- DUMMY WEB SERVER (FOR RENDER HOSTING) ---
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is successfully running on Render!")

def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), DummyHandler)
    server.serve_forever()

# --- ADMIN CHECKER ---
def is_admin(update: Update):
    if not update.message: return False
    if update.message.from_user and update.message.from_user.id == ADMIN_ID: return True
    if update.message.sender_chat and update.message.sender_chat.id == update.message.chat_id: return True
    return False

# --- 1. START MENU ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_name = update.effective_user.first_name if update.effective_user else "Admin"
        text = f"Hello {user_name}! 👋\n\nMain ek advanced AI Group Manager bot hoon."
        keyboard = [
            [InlineKeyboardButton("📢 Channel", url=CHANNEL_LINK), InlineKeyboardButton("👥 Group", url=GROUP_LINK)],
            [InlineKeyboardButton("👨‍💻 Owner / Admin", callback_data="owner_info")]
        ]
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    except: pass

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "owner_info":
        await query.edit_message_text(f"Mere owner **{OWNER_NAME}** hain.", parse_mode='Markdown')

# --- 2. ADVANCED COMMANDS (/setdelay & /tagall) ---
async def set_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global approval_delay
    if not is_admin(update): return
    try:
        new_time = int(context.args[0])
        approval_delay = new_time
        await update.message.reply_text(f"✅ Auto-approval delay time ab **{new_time} seconds** set ho gaya hai.", parse_mode='Markdown')
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Kaise use karein: `/setdelay <seconds>`\nExample: `/setdelay 10`", parse_mode='Markdown')

async def tag_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    chat_id = update.message.chat_id
    if chat_id not in active_members or not active_members[chat_id]:
        await update.message.reply_text("⏳ Abhi members ka data load nahi hua hai. Thodi der group mein messages aane dein.")
        return
    mentions = [f"[{fname}](tg://user?id={uid})" for uid, fname in active_members[chat_id].items()]
    tag_text = " ".join(mentions[:50]) 
    custom_msg = " ".join(context.args)
    text_to_send = f"{custom_msg}\n\n{tag_text}" if custom_msg else f"📢 **Dhyan dein!**\n\n{tag_text}"
    
    if update.message.reply_to_message:
        await update.message.reply_to_message.reply_text(text_to_send, parse_mode='Markdown')
    else:
        await context.bot.send_message(chat_id=chat_id, text=text_to_send, parse_mode='Markdown')

# --- 3. AUTO JOIN REQUEST ---
async def join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await asyncio.sleep(approval_delay) 
    await update.chat_join_request.approve()
    try:
        await context.bot.send_message(chat_id=update.chat_join_request.from_user.id, text="Aapki group join request accept ho gayi hai. 🎉")
    except: pass

# --- 4. SMART AI CHAT ---
async def get_ai_reply(prompt: str) -> str:
    if not GEMINI_API_KEY or not model:
        return "❌ Bhai, API Key ya model set nahi hai!"
    try:
        system_prompt = "You are a casual telegram group member. Reply in short Hinglish. Tone should be friendly. Do not act like an AI."
        response = await asyncio.to_thread(model.generate_content, system_prompt + prompt)
        return response.text
    except Exception as e:
        return f"❌ AI Crash Error: {str(e)}"

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    text = update.message.text.lower()
    chat_type = update.message.chat.type
    user = update.message.from_user
    chat_id = update.message.chat_id

    # Save active members for /tagall
    if chat_type in ['group', 'supergroup'] and user.id != ADMIN_ID and not user.is_bot:
        if chat_id not in active_members: active_members[chat_id] = {}
        active_members[chat_id][user.id] = user.first_name

    # Anti-link setup (delete links sent by non-admins)
    if chat_type in ['group', 'supergroup'] and not is_admin(update):
        if any(link in text for link in ["http://", "https://", "t.me/", ".com", ".in"]):
            try:
                await update.message.delete()
                warning = await update.message.reply_text(f"⚠️ {user.first_name}, yahan links allowed nahi hai!")
                await asyncio.sleep(5)
                await warning.delete()
            except: pass
            return

    # Owner Query
    if any(keyword in text for keyword in ["owner kon", "admin kon", "malik kon"]):
        await update.message.reply_text(f"Mere owner **{OWNER_NAME}** hain. 😎", parse_mode='Markdown')
        return

    # AI Trigger Logic
    bot_username = context.bot.username.lower() if context.bot.username else ""
    is_reply_to_bot = (update.message.reply_to_message and update.message.reply_to_message.from_user.id == context.bot.id)
    
    # Private chat ya Group mein mention/reply hone par AI ka jawab
    if chat_type == 'private' or (chat_type in ['group', 'supergroup'] and (bot_username in text or is_reply_to_bot)):
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action='typing')
            ai_reply = await get_ai_reply(update.message.text)
            await update.message.reply_text(ai_reply)
        except: pass

def main():
    # Render ke liye dummy server background mein start karo
    threading.Thread(target=run_dummy_server, daemon=True).start()
    
    app = Application.builder().token(TOKEN).build()
    
    # Basic & Advanced Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("tagall", tag_all))
    app.add_handler(CommandHandler("setdelay", set_delay)) 
    
    # Automations & Callbacks
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(ChatJoinRequestHandler(join_request))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))
    
    print("🚀 Final Bot deployed successfully with stable gemini-pro model!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
