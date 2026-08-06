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
MAIN_FOLDER_ID = '1EwRDUEYG7M58BBhwvD_f4oWY4-l0zUS2'

# --- دوال الـ API ---
def api_request(action, **kwargs):
    payload = {"action": action}
    payload.update(kwargs)
    return requests.post(GAS_WEB_APP_URL, json=payload).json()

# --- حالات المحادثة ---
ASK_FOLDER_NAME = 1
ASK_FILE = 2

# --- دوال التصفح والواجهة ---
async def start_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("🗂 تصفح الأرشيف والمجلدات", callback_data='nav_root')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "مرحباً بك في نظام إدارة الأرشيف 🗂️\nاضغط لتصفح المجلدات:"
    if update.message: await update.message.reply_text(text, reply_markup=reply_markup)
    else: await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    return ConversationHandler.END

async def browse_directory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    folder_id = "" if query.data == 'nav_root' else query.data.replace('nav_', '')
    await query.edit_message_text("⏳ جاري جلب المحتويات...")
    
    res = api_request("get_contents", folderId=folder_id)
    if res.get('status') != 'success':
        await query.edit_message_text("❌ حدث خطأ في جلب البيانات.")
        return

    current_id = res['currentId']
    keyboard = []
    
    for item in res['contents']:
        if item['type'] == 'folder':
            keyboard.append([InlineKeyboardButton(f"📁 {item['name']}", callback_data=f"nav_{item['id']}")])
        else:
            keyboard.append([InlineKeyboardButton(f"📄 {item['name']}", url=item['url'])])
            
    control_buttons = [
        InlineKeyboardButton("➕ مجلد", callback_data=f"adddir_{current_id}"),
        InlineKeyboardButton("📤 رفع ملف", callback_data=f"upfile_{current_id}"),
        InlineKeyboardButton("🗑 حذف", callback_data=f"delfol_{current_id}")
    ]
    keyboard.append(control_buttons)
    
    if current_id != MAIN_FOLDER_ID and res.get('parentId'):
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"nav_{res['parentId']}")])
    else:
        keyboard.append([InlineKeyboardButton("🏠 الرئيسية", callback_data='btn_main')])

    await query.edit_message_text(
        f"📂 **المسار الحالي:** {res['currentName']}\nاختر إجراءً:", 
        reply_markup=InlineKeyboardMarkup(keyboard), 
        parse_mode='Markdown'
    )

# --- إضافة المجلدات ---
async def prompt_add_folder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['parent_id'] = query.data.replace('adddir_', '')
    keyboard = [[InlineKeyboardButton("❌ إلغاء", callback_data=f"nav_{context.user_data['parent_id']}")]]
    await query.edit_message_text("✏️ **أرسل اسم المجلد الجديد:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return ASK_FOLDER_NAME

async def execute_add_folder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    folder_name = update.message.text
    parent_id = context.user_data.get('parent_id', MAIN_FOLDER_ID)
    msg = await update.message.reply_text(f"⏳ جاري إنشاء '{folder_name}'...")
    
    api_request("create_folder_inside", parentId=parent_id, folderName=folder_name)
    await msg.delete()
    update.callback_query = type('obj', (object,), {'data': f'nav_{parent_id}', 'answer': lambda: None, 'edit_message_text': update.message.reply_text})()
    await browse_directory(update, context)
    return ConversationHandler.END

# --- رفع الملفات ---
async def prompt_upload_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['parent_id'] = query.data.replace('upfile_', '')
    keyboard = [[InlineKeyboardButton("❌ إلغاء", callback_data=f"nav_{context.user_data['parent_id']}")]]
    await query.edit_message_text(
        "📤 **الرجاء إرسال الملف الآن**\n_(كتاب PDF، صورة، مقطع صوتي، أو فيديو)_\n⚠️ الحد الأقصى للحجم: 20MB", 
        reply_markup=InlineKeyboardMarkup(keyboard), 
        parse_mode='Markdown'
    )
    return ASK_FILE

async def execute_upload_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parent_id = context.user_data.get('parent_id', MAIN_FOLDER_ID)
    msg = await update.message.reply_text("⏳ جاري سحب الملف ورفعه لقاعدة البيانات...")

    # تحديد نوع الملف المرسل
    attachment = None
    file_name = "ملف_مرفوع"
    
    if update.message.document:
        attachment = update.message.document
        file_name = attachment.file_name
    elif update.message.photo:
        attachment = update.message.photo[-1] # اختيار أعلى دقة
        file_name = f"صورة_{attachment.file_unique_id}.jpg"
    elif update.message.video:
        attachment = update.message.video
        file_name = getattr(attachment, 'file_name', f"فيديو_{attachment.file_unique_id}.mp4")
    elif update.message.audio:
        attachment = update.message.audio
        file_name = getattr(attachment, 'file_name', f"صوت_{attachment.file_unique_id}.mp3")

    if not attachment:
        await msg.edit_text("❌ لم يتم التعرف على الملف.")
        return ConversationHandler.END

    if getattr(attachment, 'file_size', 0) > 20 * 1024 * 1024:
        await msg.edit_text("❌ حجم الملف يتجاوز الحد المسموح (20MB).")
        return ConversationHandler.END

    try:
        # جلب رابط التحميل المؤقت من تيليجرام
        file_obj = await context.bot.get_file(attachment.file_id)
        
        # إرسال الرابط إلى Google Apps Script
        res = api_request("upload_file", folderId=parent_id, fileUrl=file_obj.file_path, fileName=file_name)
        
        if res.get('status') == 'success':
            await msg.edit_text(f"✅ **تم رفع الملف بنجاح!**\n🔗 [رابط الملف]({res.get('fileUrl')})", parse_mode='Markdown', disable_web_page_preview=True)
        else:
            await msg.edit_text(f"❌ خطأ: {res.get('message')}")
    except Exception as e:
        await msg.edit_text(f"❌ خطأ غير متوقع: {str(e)}")

    # العودة للمجلد لمعاينة الملف الجديد
    update.callback_query = type('obj', (object,), {'data': f'nav_{parent_id}', 'answer': lambda: None, 'edit_message_text': update.message.reply_text})()
    await browse_directory(update, context)
    return ConversationHandler.END

# --- الحذف ---
async def delete_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    folder_id = query.data.replace('delfol_', '')
    if folder_id == MAIN_FOLDER_ID:
        await query.answer("⚠️ لا يمكنك حذف المجلد الرئيسي للأرشيف!", show_alert=True)
        return
    await query.answer("⏳ جاري الحذف...")
    api_request("delete_item", itemId=folder_id, itemType='folder')
    query.data = 'nav_root'
    await browse_directory(update, context)

# --- إعداد التطبيق ---
ptb = Application.builder().token(BOT_TOKEN).build()

conv_handler = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(prompt_add_folder, pattern='^adddir_'),
        CallbackQueryHandler(prompt_upload_file, pattern='^upfile_')
    ],
    states={
        ASK_FOLDER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, execute_add_folder)],
        # فلتر يستقبل جميع أنواع الملفات
        ASK_FILE: [MessageHandler(filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE, execute_upload_file)]
    },
    fallbacks=[CallbackQueryHandler(browse_directory, pattern='^nav_')]
)

ptb.add_handler(conv_handler)
ptb.add_handler(CallbackQueryHandler(browse_directory, pattern='^nav_'))
ptb.add_handler(CallbackQueryHandler(delete_item, pattern='^delfol_'))
ptb.add_handler(CallbackQueryHandler(start_menu, pattern='^btn_main$'))
ptb.add_handler(CommandHandler("start", start_menu))

@app.post("/api/webhook")
async def process_update(request: Request):
    try:
        if not ptb._initialized: await ptb.initialize()
        await ptb.process_update(Update.de_json(await request.json(), ptb.bot))
        return {"status": "ok"}
    except Exception as e: return {"status": "error"}

@app.get("/")
def root(): return {"message": "Active"}
