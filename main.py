import os
import asyncio
from google import genai
from dotenv import load_dotenv
from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, ChatJoinRequestHandler
)

# --- CONFIGURATION ---
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

OWNER_NAME = "@PREMGUPTA2M"
CHANNEL_LINK = "https://t.me/your_channel_link" # Apna link dalein
GROUP_LINK = "https://t.me/your_group_link"     # Apna link dalein

# Naya AI Setup (New Google GenAI Package)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# Global Variables
group_rules = "Group ke rules abhi set nahi hain. Admin /setrules command ka use karein."

# --- 1. START & BUTTONS (DM FEATURES) ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Personal/Group start menu with premium buttons"""
    user_name = update.effective_user.first_name
    text = f"Hello {user_name}! 👋\n\nMain ek advanced AI Group Manager bot hoon."
    
    keyboard = [
        [InlineKeyboardButton("📢 Channel", url=CHANNEL_LINK), InlineKeyboardButton("👥 Group", url=GROUP_LINK)],
        [InlineKeyboardButton("👨‍💻 Owner / Admin", callback_data="owner_info")]
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles inline button clicks"""
    query = update.callback_query
    await query.answer()
    if query.data == "owner_info":
        await query.edit_message_text(f"Mere owner **{OWNER_NAME}** hain.", parse_mode='Markdown')

# --- 2. ADVANCED GROUP MANAGEMENT ---

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID: return
    if update.message.reply_to_message:
        user_to_ban = update.message.reply_to_message.from_user
        await context.bot.ban_chat_member(update.message.chat_id, user_to_ban.id)
        await update.message.reply_text(f"🔨 {user_to_ban.first_name} ko ban kar diya gaya hai.")

async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID: return
    if update.message.reply_to_message:
        user_to_mute = update.message.reply_to_message.from_user
        permissions = ChatPermissions(can_send_messages=False)
        await context.bot.restrict_chat_member(update.message.chat_id, user_to_mute.id, permissions)
        await update.message.reply_text(f"🔇 {user_to_mute.first_name} ko mute kar diya gaya hai.")

async def unmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID: return
    if update.message.reply_to_message:
        user_to_unmute = update.message.reply_to_message.from_user
        permissions = ChatPermissions(
            can_send_messages=True, can_send_media_messages=True, 
            can_send_other_messages=True, can_add_web_page_previews=True
        )
        await context.bot.restrict_chat_member(update.message.chat_id, user_to_unmute.id, permissions)
        await update.message.reply_text(f"🔊 {user_to_unmute.first_name} ab message bhej sakta hai.")

async def lock_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID: return
    permissions = ChatPermissions(can_send_messages=False)
    await context.bot.set_chat_permissions(chat_id=update.message.chat_id, permissions=permissions)
    await update.message.reply_text("🔒 Group Lock ho gaya hai. Ab sirf Admins message kar sakte hain.")

async def unlock_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID: return
    permissions = ChatPermissions(
        can_send_messages=True, can_send_media_messages=True, 
        can_send_other_messages=True, can_add_web_page_previews=True
    )
    await context.bot.set_chat_permissions(chat_id=update.message.chat_id, permissions=permissions)
    await update.message.reply_text("🔓 Group Unlock ho gaya hai.")

# --- 3. ADMIN MANUAL & CUSTOM LINK ---

async def set_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global group_rules
    if update.message.from_user.id != ADMIN_ID: return
    new_rules = " ".join(context.args)
    if not new_rules:
        await update.message.reply_text("⚠️ Type karein: `/setrules 1. Rule ek 2. Rule do`", parse_mode='Markdown')
        return
    group_rules = new_rules
    await update.message.reply_text("✅ Group Rules successfully set ho gaye hain.")

async def show_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📜 **Group Rules:**\n\n{group_rules}", parse_mode='Markdown')

async def create_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID: return
    link = await context.bot.create_chat_invite_link(
        chat_id=update.message.chat_id, creates_join_request=True
    )
    await update.message.reply_text(f"🔗 Yeh lijiye naya manual link:\n{link.invite_link}")

# --- 4. AUTO JOIN REQUEST & WELCOME MESSAGE ---

async def join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delay ke sath Join Request Accept aur Welcome Message"""
    await asyncio.sleep(5)  # 5 seconds delay
    await update.chat_join_request.approve()
    user = update.chat_join_request.from_user
    
    welcome_text = (
        f"Hello {user.first_name}! 🎉\n\n"
        f"Aapki group join request accept kar li gayi hai.\n"
        f"Group mein rules follow karein aur koi problem ho toh {OWNER_NAME} ko message karein."
    )
    try:
        await context.bot.send_message(chat_id=user.id, text=welcome_text)
    except:
        pass

# --- 5. SMART AI CHAT & ANTI-LINK DETECTOR ---

async def get_ai_reply(prompt: str) -> str:
    try:
        system_prompt = "You are a casual telegram group member. Reply in short Hinglish. Tone should be friendly. Do not act like an AI or bot. "
        
        # New Google GenAI API Call Logic
        def fetch_response():
            response = ai_client.models.generate_content(
                model='gemini-1.5-flash',
                contents=system_prompt + prompt
            )
            return response.text
            
        ai_reply = await asyncio.to_thread(fetch_response)
        return ai_reply
    except Exception as e:
        return "Haan bhai, sab theek hai."

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    text = update.message.text.lower()
    chat_type = update.message.chat.type
    user = update.message.from_user

    # Anti-Link in Group
    if chat_type in ['group', 'supergroup'] and user.id != ADMIN_ID:
        if any(link in text for link in ["http://", "https://", "t.me/", ".com", ".in"]):
            await update.message.delete()
            warning = await update.message.reply_text(f"⚠️ {user.first_name}, yahan links bhejna allowed nahi hai!")
            await asyncio.sleep(5)
            await warning.delete()
            return

    # Owner Info Detection
    if any(keyword in text for keyword in ["owner kon", "admin kon", "malik kon"]):
        await update.message.reply_text(f"Mere owner **{OWNER_NAME}** hain. 😎", parse_mode='Markdown')
        return

    # AI Chating Logic
    bot_username = context.bot.username.lower() if context.bot.username else ""
    is_reply_to_bot = (update.message.reply_to_message and update.message.reply_to_message.from_user.id == context.bot.id)
    
    if chat_type == 'private' or (chat_type in ['group', 'supergroup'] and (bot_username in text or is_reply_to_bot)):
        await context.bot.send_chat_action(chat_id=update.message.chat_id, action='typing')
        ai_reply = await get_ai_reply(update.message.text)
        await update.message.reply_text(ai_reply)

# --- MAIN FUNCTION ---

def main():
    # Build App
    app = Application.builder().token(TOKEN).build()
    
    # Register Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("mute", mute_user))
    app.add_handler(CommandHandler("unmute", unmute_user))
    app.add_handler(CommandHandler("lock", lock_group))
    app.add_handler(CommandHandler("unlock", unlock_group))
    app.add_handler(CommandHandler("setrules", set_rules))
    app.add_handler(CommandHandler("rules", show_rules))
    app.add_handler(CommandHandler("link", create_link))
    
    # Register Callbacks & Automations
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(ChatJoinRequestHandler(join_request))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))
    
    print("🚀 Bot Fixed & Running Successfully...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
