import os
import logging
import requests
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    ContextTypes, ConversationHandler, MessageHandler, filters
)

logging.basicConfig(level=logging.INFO)
app = FastAPI()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
GAS_WEB_APP_URL = os.environ.get("GAS_WEB_APP_URL")
CHANNEL_ID = os.environ.get("CHANNEL_ID")

# --- دوال الـ API مع قوقل شيت ---
def api_request(action, **kwargs):
    payload = {"action": action}
    payload.update(kwargs)
    res = requests.post(GAS_WEB_APP_URL, json=payload)
    return res.json()

# --- حالات المحادثة ---
ASK_FOLDER_NAME = 1
ASK_FILE = 2

async def start_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🗂 تصفح الأرشيف القرآني", callback_data='nav_root')],
        [InlineKeyboardButton("📝 اختبارات الدروس", callback_data='btn_quizzes')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "مرحباً بك في نظام إدارة الأرشيف 🗂️\nاختر من القائمة:"
    if update.message: await update.message.reply_text(text, reply_markup=reply_markup)
    else: await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    return ConversationHandler.END

async def browse_directory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    folder_id = "root" if query.data == 'nav_root' else query.data.replace('nav_', '')
    await query.edit_message_text("⏳ جاري جلب المحتويات...")
    
    res = api_request("get_contents", folderId=folder_id)
    if res.get('status') != 'success':
        await query.edit_message_text("❌ حدث خطأ في الاتصال بقوقل شيت.")
        return

    keyboard = []
    
    # إضافة الأزرار (مجلدات وملفات)
    for item in res['contents']:
        if item['type'] == 'folder':
            keyboard.append([InlineKeyboardButton(f"📁 {item['name']}", callback_data=f"nav_{item['id']}")])
        else:
            keyboard.append([InlineKeyboardButton(f"📄 {item['name']}", callback_data=f"getfile_{item['file_id']}")])
            
    # أزرار الإدارة
    control_buttons = [
        InlineKeyboardButton("➕ مجلد جديد", callback_data=f"adddir_{folder_id}"),
        InlineKeyboardButton("📤 رفع ملف", callback_data=f"upfile_{folder_id}")
    ]
    keyboard.append(control_buttons)
    
    if folder_id != "root":
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"nav_{res['parentId']}")])
    else:
        keyboard.append([InlineKeyboardButton("🏠 الرئيسية", callback_data='btn_main')])

    await query.edit_message_text(f"📂 **المسار:** {res['currentName']}\nاختر إجراءً:", reply_markup=InlineKeyboardMarkup(keyboard))

# --- إنشاء مجلد ---
async def prompt_add_folder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['parent_id'] = query.data.replace('adddir_', '')
    keyboard = [[InlineKeyboardButton("❌ إلغاء", callback_data=f"nav_{context.user_data['parent_id']}")]]
    await query.edit_message_text("✏️ **أرسل اسم المجلد الجديد:**", reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_FOLDER_NAME

async def execute_add_folder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    folder_name = update.message.text
    parent_id = context.user_data.get('parent_id', 'root')
    msg = await update.message.reply_text("⏳ جاري الإنشاء...")
    
    api_request("add_item", parentId=parent_id, name=folder_name, type="folder")
    
    update.callback_query = type('obj', (object,), {'data': f'nav_{parent_id}', 'answer': lambda: None, 'edit_message_text': msg.edit_text})()
    await browse_directory(update, context)
    return ConversationHandler.END

# --- رفع الملفات للقناة + قوقل شيت ---
async def prompt_upload_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['parent_id'] = query.data.replace('upfile_', '')
    keyboard = [[InlineKeyboardButton("❌ إلغاء", callback_data=f"nav_{context.user_data['parent_id']}")]]
    await query.edit_message_text("📤 **الرجاء إرسال الملف (فيديو، صوت، ملف، صورة)**", reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_FILE

async def execute_upload_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parent_id = context.user_data.get('parent_id', 'root')
    msg = await update.message.reply_text("⏳ جاري الأرشفة...")

    attachment = None
    file_name = "ملف"
    
    if update.message.document:
        attachment = update.message.document
        file_name = attachment.file_name
    elif update.message.video:
        attachment = update.message.video
        file_name = getattr(attachment, 'file_name', f"فيديو.mp4")
    elif update.message.audio:
        attachment = update.message.audio
        file_name = getattr(attachment, 'file_name', f"صوت.mp3")
    elif update.message.photo:
        attachment = update.message.photo[-1]
        file_name = f"صورة.jpg"

    if attachment:
        try:
            # 1. إرسال الملف לקناة تيليجرام للحصول على مساحة لا محدودة
            await context.bot.forward_message(chat_id=CHANNEL_ID, from_chat_id=update.effective_chat.id, message_id=update.message.message_id)
            
            # 2. حفظ البيانات في قوقل شيت
            api_request("add_item", parentId=parent_id, name=file_name, type="file", fileId=attachment.file_id)
            await msg.edit_text("✅ **تم رفع الملف وحفظه بنجاح!**", parse_mode='Markdown')
        except Exception as e:
            await msg.edit_text(f"❌ خطأ: تأكد من إضافة البوت كـ(مشرف) في القناة.")
            
    update.callback_query = type('obj', (object,), {'data': f'nav_{parent_id}', 'answer': lambda: None, 'edit_message_text': update.message.reply_text})()
    await browse_directory(update, context)
    return ConversationHandler.END

# --- إرسال الملف للمستخدم ---
async def send_file_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    file_id = query.data.replace('getfile_', '')
    await query.message.reply_text("⏳ جاري جلب الملف...")
    # البوت يرسل الملف فوراً من سيرفرات تيليجرام
    await context.bot.send_document(chat_id=update.effective_chat.id, document=file_id)

ptb = Application.builder().token(BOT_TOKEN).build()

conv_handler = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(prompt_add_folder, pattern='^adddir_'),
        CallbackQueryHandler(prompt_upload_file, pattern='^upfile_')
    ],
    states={
        ASK_FOLDER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, execute_add_folder)],
        ASK_FILE: [MessageHandler(filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE, execute_upload_file)]
    },
    fallbacks=[CallbackQueryHandler(browse_directory, pattern='^nav_')]
)

ptb.add_handler(conv_handler)
ptb.add_handler(CallbackQueryHandler(browse_directory, pattern='^nav_'))
ptb.add_handler(CallbackQueryHandler(send_file_to_user, pattern='^getfile_'))
ptb.add_handler(CallbackQueryHandler(start_menu, pattern='^btn_main$'))
ptb.add_handler(CommandHandler("start", start_menu))

@app.get("/{full_path:path}")
def root(full_path: str):
    return {"status": "active", "message": "Bot Server is UP!"}

@app.post("/api/webhook")
@app.post("/api/main")
@app.post("/{full_path:path}")
async def process_update(request: Request):
    try:
        if not ptb._initialized: 
            await ptb.initialize()
        data = await request.json()
        await ptb.process_update(Update.de_json(data, ptb.bot))
        return {"status": "ok"}
    except Exception as e: 
        logging.error(f"Webhook Error: {e}")
        return {"status": "error"}
