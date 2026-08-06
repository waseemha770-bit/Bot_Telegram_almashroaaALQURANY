import logging
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# إعداد تسجيل الأخطاء
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ضع توكن البوت ورابط الـ API هنا
BOT_TOKEN = '8683097921:AAEjnNFe9AmYzNz0GqWZD1ZPGAsBOcj7iUI'
GAS_WEB_APP_URL = 'https://script.google.com/macros/s/AKfycbzE_RUUEIzyrQGj6i9K90aNNLsIICcR8pbasV807dWX4YbUcl3PRYpcCw3I0IGK8mWB/exec'

# --- دوال الربط مع Google Apps Script ---

def api_create_folder(folder_name, description=""):
    payload = {
        "action": "create_folder",
        "folderName": folder_name,
        "description": description
    }
    response = requests.post(GAS_WEB_APP_URL, json=payload)
    return response.json()

def api_get_data(sheet_name="هيكلة المجلدات والملفات"):
    payload = {
        "action": "get_data",
        "sheetName": sheet_name
    }
    response = requests.post(GAS_WEB_APP_URL, json=payload)
    return response.json()

# --- واجهة البوت ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """لوحة تحكم المشرفين الرئيسية"""
    keyboard = [
        [InlineKeyboardButton("📁 إنشاء مجلد أرشفة جديد", callback_data='btn_create_folder')],
        [InlineKeyboardButton("📊 عرض هيكلة المجلدات", callback_data='btn_get_structure')],
        [InlineKeyboardButton("⚙️ إعدادات النظام", callback_data='btn_settings')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = (
        "مرحباً بك في نظام إدارة أرشفة المشروع القرآني 🗂️\n\n"
        "الرجاء اختيار الإجراء المطلوب من القائمة أدناه:"
    )
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة النقرات على الأزرار الشفافة"""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'btn_create_folder':
        # للتوضيح: هنا يتم استدعاء دالة الـ API.
        # في بيئة الإنتاج، يفضل أخذ اسم المجلد من المستخدم عبر ConversationHandler
        await query.edit_message_text(text="جاري إنشاء المجلد للتجربة...")
        result = api_create_folder("مجلد اختبار جديد", "تم إنشاؤه عبر البوت")
        
        if result.get('status') == 'success':
            msg = f"✅ تم الإنشاء بنجاح!\nالرابط: {result.get('folderUrl')}"
        else:
            msg = f"❌ حدث خطأ: {result.get('message')}"
            
        await query.edit_message_text(text=msg)
        
    elif query.data == 'btn_get_structure':
        await query.edit_message_text(text="جاري جلب البيانات من قوقل شيت...")
        result = api_get_data()
        
        if result.get('status') == 'success':
            data = result.get('data')
            # عرض أول 5 صفوف كمثال (تجاوز صف العناوين)
            text_result = "📂 **أحدث المجلدات والملفات:**\n\n"
            for row in data[1:6]:
                if len(row) >= 2:
                    text_result += f"🔹 {row[1]} ({row[2]})\n"
            await query.edit_message_text(text=text_result, parse_mode='Markdown')
        else:
            await query.edit_message_text(text="❌ فشل جلب البيانات.")

def main():
    """تشغيل البوت"""
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    print("البوت يعمل الآن... اضغط Ctrl+C للإيقاف")
    app.run_polling()

if __name__ == '__main__':
    main()
