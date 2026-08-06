import os
import logging
import requests
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(level=logging.INFO)

app = FastAPI()

# جلب المتغيرات من بيئة Vercel
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8683097921:AAEjnNFe9AmYzNz0GqWZD1ZPGAsBOcj7iUI")
GAS_WEB_APP_URL = os.environ.get("GAS_WEB_APP_URL", "https://script.google.com/macros/s/AKfycbzE_RUUEIzyrQGj6i9K90aNNLsIICcR8pbasV807dWX4YbUcl3PRYpcCw3I0IGK8mWB/exec")

# --- دوال الربط مع Google Apps Script ---
def api_create_folder(folder_name, description=""):
    payload = {"action": "create_folder", "folderName": folder_name, "description": description}
    response = requests.post(GAS_WEB_APP_URL, json=payload)
    return response.json()

def api_get_data(sheet_name="هيكلة المجلدات والملفات"):
    payload = {"action": "get_data", "sheetName": sheet_name}
    response = requests.post(GAS_WEB_APP_URL, json=payload)
    return response.json()

# --- واجهة البوت ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📁 إنشاء مجلد أرشفة جديد", callback_data='btn_create_folder')],
        [InlineKeyboardButton("📊 عرض هيكلة المجلدات", callback_data='btn_get_structure')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("مرحباً بك في نظام إدارة أرشفة المشروع القرآني 🗂️", reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == 'btn_create_folder':
        await query.edit_message_text(text="جاري إنشاء المجلد للتجربة...")
        result = api_create_folder("مجلد اختبار جديد", "تم إنشاؤه عبر البوت")
        msg = f"✅ تم الإنشاء بنجاح!\nالرابط: {result.get('folderUrl')}" if result.get('status') == 'success' else f"❌ خطأ: {result.get('message')}"
        await query.edit_message_text(text=msg)
        
    elif query.data == 'btn_get_structure':
        await query.edit_message_text(text="جاري جلب البيانات من قوقل شيت...")
        result = api_get_data()
        if result.get('status') == 'success':
            data = result.get('data')
            text_result = "📂 **أحدث المجلدات:**\n\n"
            for row in data[1:6]:
                if len(row) >= 2:
                    text_result += f"🔹 {row[1]}\n"
            await query.edit_message_text(text=text_result, parse_mode='Markdown')
        else:
            await query.edit_message_text(text="❌ فشل جلب البيانات.")

# --- إعداد تطبيق تيليجرام للعمل كـ Webhook ---
ptb = Application.builder().token(BOT_TOKEN).build()
ptb.add_handler(CommandHandler("start", start))
ptb.add_handler(CallbackQueryHandler(button_handler))

@app.post("/api/webhook")
async def process_update(request: Request):
    """نقطة الاستقبال التي يرسل إليها تيليجرام التحديثات"""
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
    return {"message": "Telegram Bot is running on Vercel Serverless!"}
