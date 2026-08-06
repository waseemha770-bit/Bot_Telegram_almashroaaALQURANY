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
MAIN_FOLDER_ID = '1EwRDUEYG7M58BBhwvD_f4oWY4-l0zUS2' # المعرف الأساسي لحماية التصفح

# --- دوال الـ API ---
def api_get_contents(folder_id=""):
    payload = {"action": "get_contents", "folderId": folder_id}
    return requests.post(GAS_WEB_APP_URL, json=payload).json()

def api_create_folder_inside(parent_id, folder_name):
    payload = {"action": "create_folder_inside", "parentId": parent_id, "folderName": folder_name}
    return requests.post(GAS_WEB_APP_URL, json=payload).json()

def api_delete_item(item_id, item_type):
    payload = {"action": "delete_item", "itemId": item_id, "itemType": item_type}
    return requests.post(GAS_WEB_APP_URL, json=payload).json()

# --- حالات المحادثة ---
ASK_FOLDER_NAME = 1

# --- دوال التصفح والواجهة ---
async def start_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("🗂 تصفح الأرشيف والمجلدات", callback_data='nav_root')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "مرحباً بك في نظام إدارة الأرشيف 🗂️\nاضغط لتصفح المجلدات:"
    
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    else:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    return ConversationHandler.END

async def browse_directory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """توليد أزرار المجلدات والملفات ديناميكياً"""
    query = update.callback_query
    await query.answer()
    
    # تحديد المجلد المطلوب (root تعني المجلد الرئيسي)
    folder_id = "" if query.data == 'nav_root' else query.data.replace('nav_', '')
    await query.edit_message_text("⏳ جاري جلب المحتويات...")
    
    res = api_get_contents(folder_id)
    if res.get('status') != 'success':
        await query.edit_message_text("❌ حدث خطأ في جلب البيانات.")
        return

    current_id = res['currentId']
    keyboard = []
    
    # 1. إضافة المجلدات كأزرار قابلة للنقر
    for item in res['contents']:
        if item['type'] == 'folder':
            # النقر على المجلد ينقلك لداخله
            keyboard.append([InlineKeyboardButton(f"📁 {item['name']}", callback_data=f"nav_{item['id']}")])
        else:
            # النقر على الملف يفتح رابطه (كمثال)
            keyboard.append([InlineKeyboardButton(f"📄 {item['name']}", url=item['url'])])
            
    # 2. أزرار التحكم والإدارة للمجلد الحالي
    control_buttons = [
        InlineKeyboardButton("➕ مجلد جديد", callback_data=f"adddir_{current_id}"),
        InlineKeyboardButton("🗑 حذف الحالي", callback_data=f"delfol_{current_id}")
    ]
    keyboard.append(control_buttons)
    
    # 3. زر الرجوع للخلف (مع حماية عدم تجاوز المجلد الرئيسي)
    if current_id != MAIN_FOLDER_ID and res.get('parentId'):
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"nav_{res['parentId']}")])
    else:
        keyboard.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data='btn_main')])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(f"📂 **المسار الحالي:** {res['currentName']}\nاختر إجراءً:", reply_markup=reply_markup, parse_mode='Markdown')

# --- دوال الإضافة والحذف ---
async def prompt_add_folder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # حفظ معرف المجلد الحالي لإضافة المجلد الجديد بداخله
    context.user_data['parent_id'] = query.data.replace('adddir_', '')
    
    keyboard = [[InlineKeyboardButton("❌ إلغاء", callback_data=f"nav_{context.user_data['parent_id']}")]]
    await query.edit_message_text("✏️ **أرسل اسم المجلد الجديد:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return ASK_FOLDER_NAME

async def execute_add_folder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    folder_name = update.message.text
    parent_id = context.user_data.get('parent_id', MAIN_FOLDER_ID)
    
    msg = await update.message.reply_text(f"⏳ جاري إنشاء '{folder_name}'...")
    api_create_folder_inside(parent_id, folder_name)
    
    await msg.delete()
    # إعادة تحميل المجلد ليرى المستخدم التحديث
    update.callback_query = type('obj', (object,), {'data': f'nav_{parent_id}', 'answer': lambda: None, 'edit_message_text': update.message.reply_text})()
    await browse_directory(update, context)
    return ConversationHandler.END

async def delete_folder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    folder_id = query.data.replace('delfol_', '')
    
    if folder_id == MAIN_FOLDER_ID:
        await query.answer("⚠️ لا يمكنك حذف المجلد الرئيسي للأرشيف!", show_alert=True)
        return
        
    await query.answer("⏳ جاري الحذف...")
    # الحذف ثم العودة للمجلد الرئيسي كإجراء افتراضي
    api_delete_item(folder_id, 'folder')
    
    query.data = 'nav_root'
    await browse_directory(update, context)

# --- إعداد التطبيق ---
ptb = Application.builder().token(BOT_TOKEN).build()

conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(prompt_add_folder, pattern='^adddir_')],
    states={ASK_FOLDER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, execute_add_folder)]},
    fallbacks=[CallbackQueryHandler(browse_directory, pattern='^nav_')]
)

ptb.add_handler(conv_handler)
ptb.add_handler(CallbackQueryHandler(browse_directory, pattern='^nav_'))
ptb.add_handler(CallbackQueryHandler(delete_folder, pattern='^delfol_'))
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
