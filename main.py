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

# جلب المتغيرات
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GAS_WEB_APP_URL = os.environ.get("GAS_WEB_APP_URL")

# --- دوال الـ API ---
def api_create_folder(folder_name, description=""):
    payload = {"action": "create_folder", "folderName": folder_name, "description": description}
    response = requests.post(GAS_WEB_APP_URL, json=payload)
    return response.json()

def api_get_data(sheet_name="هيكلة المجلدات والملفات"):
    payload = {"action": "get_data", "sheetName": sheet_name}
    response = requests.post(GAS_WEB_APP_URL, json=payload)
    return response.json()

# --- حالات المحادثة (States) ---
ASK_FOLDER_NAME = 1

# --- واجهة البوت والتفاعلات ---
async def start_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إرسال القائمة الرئيسية"""
    keyboard = [
        [InlineKeyboardButton("📁 إنشاء مجلد أرشفة جديد", callback_data='btn_create_folder')],
        [InlineKeyboardButton("📊 عرض هيكلة المجلدات", callback_data='btn_get_structure')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "مرحباً بك في نظام إدارة أرشفة المشروع القرآني 🗂️\nاختر إجراءً:"
    
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    else:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    return ConversationHandler.END

async def ask_folder_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """الخطوة الأولى: طلب اسم المجلد من المستخدم"""
    query = update.callback_query
    await query.answer()
    
    # زر لإلغاء العملية والعودة
    keyboard = [[InlineKeyboardButton("❌ إلغاء العملية", callback_data='cancel_creation')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text="✏️ **الرجاء كتابة وإرسال اسم المجلد الجديد:**", 
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    return ASK_FOLDER_NAME

async def receive_folder_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """الخطوة الثانية: استلام الاسم وإنشاء المجلد"""
    folder_name = update.message.text
    
    # رسالة مؤقتة ريثما يتم الاتصال بقوقل
    msg = await update.message.reply_text(f"⏳ جاري إنشاء المجلد '{folder_name}'...")
    
    # إرسال الطلب لـ Google Apps Script
    result = api_create_folder(folder_name, "تم الإنشاء بواسطة البوت")
    
    if result.get('status') == 'success':
        final_text = f"✅ **تم الإنشاء بنجاح!**\n\n📁 **الاسم:** {folder_name}\n🔗 **الرابط:** {result.get('folderUrl')}"
    else:
        final_text = f"❌ **حدث خطأ:** {result.get('message')}"
        
    await msg.edit_text(text=final_text, parse_mode='Markdown')
    
    # العودة للقائمة الرئيسية تلقائياً
    await start_menu(update, context)
    return ConversationHandler.END

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة الأزرار الأخرى (خارج المحادثة)"""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'btn_get_structure':
        await query.edit_message_text(text="⏳ جاري جلب البيانات من قوقل شيت...")
        result = api_get_data()
        
        keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة الرئيسية", callback_data='btn_main_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if result.get('status') == 'success':
            data = result.get('data')
            text_result = "📂 **أحدث المجلدات في الأرشيف:**\n\n"
            for row in data[1:6]:
                if len(row) >= 2:
                    text_result += f"🔹 {row[1]}\n"
            await query.edit_message_text(text=text_result, parse_mode='Markdown', reply_markup=reply_markup)
        else:
            await query.edit_message_text(text="❌ فشل جلب البيانات.", reply_markup=reply_markup)
            
    elif query.data == 'btn_main_menu' or query.data == 'cancel_creation':
        await start_menu(update, context)

# --- إعداد التطبيق والمسارات ---
ptb = Application.builder().token(BOT_TOKEN).build()

# إعداد مدير المحادثة لإنشاء المجلد
conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(ask_folder_name, pattern='^btn_create_folder$')],
    states={
        ASK_FOLDER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_folder_name)]
    },
    fallbacks=[
        CallbackQueryHandler(start_menu, pattern='^cancel_creation$'),
        CommandHandler('start', start_menu)
    ]
)

# ربط الدوال بالبوت بالترتيب
ptb.add_handler(conv_handler)
ptb.add_handler(CallbackQueryHandler(button_handler))
ptb.add_handler(CommandHandler("start", start_menu))

@app.post("/api/webhook")
async def process_update(request: Request):
    try:
        if not ptb._initialized:
            await ptb.initialize()
        data = await request.json()
        update = Update.de_json(data, ptb.bot)
        await ptb.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/")
def root():
    return {"message": "Telegram Bot is running smoothly!"}
