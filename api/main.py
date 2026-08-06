import os
import re
import json
import time
import random
import logging
import requests
import pandas as pd
from io import BytesIO
from fastapi import FastAPI, Request
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    MessageHandler, filters, ContextTypes
)

logging.basicConfig(level=logging.INFO)
app = FastAPI()

# ==========================================
# 1. إعدادات المتغيرات (Config)
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GAS_WEB_APP_URL = os.environ.get("GAS_WEB_APP_URL")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
OWNER_ID = str(os.environ.get("ADMIN_ID", "")) 
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

TIME_LIMIT_SECONDS = 30

# ==========================================
# 2. قوائم الأزرار (Keyboards)
# ==========================================
USER_KB = ReplyKeyboardMarkup([
    ["🎮 سؤال جديد", "🗂️ تغيير القسم"],
    ["🏆 لوحة الشرف", "📊 رصيدي الحالي"],
    ["⭐ المفضلة", "📚 المكتبة"],
    ["🚀 ابدأ من جديد"]
], resize_keyboard=True)

ADMIN_KB = ReplyKeyboardMarkup([
    ["🎮 سؤال جديد", "🧠 توليد أسئلة (AI)"],
    ["📥 استيراد إكسل", "📚 إضافة كتاب (مباشر)"],
    ["📤 تصدير إكسل", "📥 رفع مكتبة الكتب"],
    ["📢 إرسال للمجموعة", "📈 إحصائيات التفاعل"],
    ["🏆 لوحة الشرف", "🗂️ تغيير القسم"],
    ["⭐ المفضلة", "📚 المكتبة"],
    ["👥 تقرير المتسابقين", "🔗 ربط بمجموعة"],
    ["🚀 ابدأ من جديد"]
], resize_keyboard=True)

OWNER_KB = ReplyKeyboardMarkup([
    ["🎮 سؤال جديد", "🧠 توليد أسئلة (AI)"],
    ["📥 استيراد إكسل", "📚 إضافة كتاب (مباشر)"],
    ["📤 تصدير إكسل", "📥 رفع مكتبة الكتب"],
    ["📢 إرسال للمجموعة", "📈 إحصائيات التفاعل"],
    ["⚙️ إدارة المشرفين", "🔗 ربط بمجموعة"],
    ["🏆 لوحة الشرف", "📊 رصيدي الحالي"],
    ["⭐ المفضلة", "📚 المكتبة"],
    ["👥 تقرير المتسابقين", "🗂️ تغيير القسم"],
    ["🚀 ابدأ من جديد"]
], resize_keyboard=True)

# ==========================================
# 3. دوال مساعدة ونظام النقاط (Helpers & Gamification)
# ==========================================
def api_request(action, **kwargs):
    payload = {"action": action}
    payload.update(kwargs)
    try:
        res = requests.post(GAS_WEB_APP_URL, json=payload, timeout=15)
        return res.json()
    except Exception as e:
        logging.error(f"GAS API Error: {e}")
        return {"status": "error"}

def normalize_arabic(text):
    if not text: return ""
    text = text.lower()
    text = re.sub(r'[أإآا]', 'ا', text)
    text = text.replace('ة', 'ه').replace('ى', 'ي').replace('ؤ', 'و').replace('ئ', 'ي')
    text = re.sub(r'ً|ٌ|ٍ|َ|ُ|ِ|ّ|ْ', '', text)
    return text

def get_rank(score):
    if score < 50: return "مبتدئ 🌱"
    if score < 150: return "متسابق نشط ⚡"
    if score < 300: return "محترف 🏅"
    return "أسطورة المعرفة 🔥"

def build_dynamic_keyboard(buttons):
    inline_keyboard = []
    current_row = []
    current_chars = 0
    for btn in buttons:
        text_len = len(btn.text)
        if text_len > 16:
            if current_row:
                inline_keyboard.append(current_row)
                current_row = []
                current_chars = 0
            inline_keyboard.append([btn])
        else:
            if len(current_row) >= 2 or (current_chars + text_len > 32):
                inline_keyboard.append(current_row)
                current_row = [btn]
                current_chars = text_len
            else:
                current_row.append(btn)
                current_chars += text_len
    if current_row:
        inline_keyboard.append(current_row)
    return inline_keyboard

async def get_keyboard(user_id):
    if str(user_id) == OWNER_ID:
        return OWNER_KB
    # نجلب قائمة المشرفين من قوقل شيت
    res = api_request("check_admin", user_id=user_id)
    if res.get("is_admin"):
        return ADMIN_KB
    return USER_KB

# ==========================================
# 4. محرك الأسئلة والمسابقات
# ==========================================
async def send_question(context, chat_id, category, user_id=None, message_id_to_edit=None):
    res = api_request("get_question", category=category, user_id=user_id)
    
    if res.get("status") != "success" or not res.get("question"):
        msg = f"🎉 لقد أتممت جميع أسئلة هذا القسم أو لا توجد أسئلة متاحة."
        if message_id_to_edit:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id_to_edit, text=msg)
        else:
            await context.bot.send_message(chat_id=chat_id, text=msg)
        return

    q = res["question"]
    q_id = q["id"]
    timestamp = int(time.time())
    is_gold = 1 if random.random() < 0.15 else 0

    raw_buttons = [InlineKeyboardButton(q["correct"], callback_data=f"c_{q_id}_{timestamp}_{is_gold}")]
    for idx, w in enumerate(q.get("wrong", [])):
        if w: raw_buttons.append(InlineKeyboardButton(w, callback_data=f"w_{q_id}_{idx}_{timestamp}_{is_gold}"))
    
    random.shuffle(raw_buttons)
    
    # تحويل الخيارات الطويلة إلى أرقام
    needs_mapping = any(len(b.text) > 32 for b in raw_buttons)
    options_text = ""
    emojis = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣', '6️⃣']
    
    if needs_mapping:
        options_text = "\n\n*الخيارات:*\n"
        for i, b in enumerate(raw_buttons):
            options_text += f"{emojis[i]} {b.text}\n"
            b.text = emojis[i]

    inline_keyboard = build_dynamic_keyboard(raw_buttons)
    
    q_text = f"📁 *{category}*\n\n"
    if is_gold: q_text += "🌟 *سؤال ذهبي! نقاط مضاعفة!* 🌟\n\n"
    q_text += f"❓ *{q['question']}*{options_text}\n\n⏱️ أمامك {TIME_LIMIT_SECONDS} ثانية للإجابة!"

    if message_id_to_edit:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id_to_edit, text=q_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard))
    else:
        await context.bot.send_message(chat_id=chat_id, text=q_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard))

# ==========================================
# 5. معالجة الإكسل والمخططات (Excel & Charts)
# ==========================================
async def send_interaction_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ جاري توليد الرسم البياني للتفاعل...")
    res = api_request("get_stats")
    
    if not res.get("data"):
        return await msg.edit_text("⚠️ لا توجد بيانات تفاعل كافية لرسم المخطط حتى الآن.")
        
    labels = [k for k, v in res["data"].items()]
    data = [v for k, v in res["data"].items()]
    
    chart_config = {
        "type": "bar",
        "data": {
            "labels": labels,
            "datasets": [{"label": "عدد الإجابات", "data": data, "backgroundColor": "rgba(54, 162, 235, 0.7)"}]
        }
    }
    
    chart_url = f"https://quickchart.io/chart?c={json.dumps(chart_config)}&w=600&h=400&bkg=white"
    await msg.delete()
    await context.bot.send_photo(chat_id=update.effective_chat.id, photo=chart_url, caption="📊 *تقرير تفاعل المتسابقين*", parse_mode="Markdown")

# ==========================================
# 6. معالجة الأزرار الشفافة (Callback Query)
# ==========================================
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = str(query.from_user.id)
    chat_id = query.message.chat_id
    data = query.data

    if data == "ignore":
        return await query.answer("⚠️ لقد قمت بهذا الإجراء مسبقاً.", show_alert=True)

    # معالجة الإجابات
    if data.startswith("c_") or data.startswith("w_"):
        parts = data.split('_')
        is_correct = (parts[0] == 'c')
        q_id = parts[1]
        
        timestamp = int(parts[2]) if is_correct else int(parts[3])
        is_gold = int(parts[3]) == 1 if is_correct else int(parts[4]) == 1
        
        time_diff = int(time.time()) - timestamp
        
        if time_diff > TIME_LIMIT_SECONDS:
            await query.answer("⏳ انتهى الوقت!", show_alert=True)
            return await query.edit_message_text(f"⏳ *انتهى الوقت!*\nاستغرقت {time_diff} ثانية.", parse_mode="Markdown")

        points = 0
        alert_msg = ""
        
        if is_correct:
            points = 10
            alert_msg = f"✅ إجابة صحيحة! (+10)\n⏱️ الوقت: {time_diff} ثانية"
            if time_diff <= 5: 
                points += 5
                alert_msg += "\n⚡ سرعة خارقة (+5)"
            if is_gold:
                points *= 2
                alert_msg += "\n🌟 ضربة ذهبية! (النقاط x2)"
        else:
            alert_msg = f"❌ إجابة خاطئة!\n⏱️ الوقت: {time_diff} ثانية\nانكسرت سلسلة انتصاراتك!"

        # إرسال النتيجة لقوقل شيت لتحديث الرصيد والسلسلة
        api_request("submit_answer", user_id=user_id, q_id=q_id, is_correct=is_correct, points=points)

        new_kb = []
        for row in query.message.reply_markup.inline_keyboard:
            new_row = []
            for btn in row:
                if btn.callback_data and btn.callback_data.startswith('c_'):
                    new_row.append(InlineKeyboardButton("✅ " + btn.text, callback_data="ignore"))
                elif btn.callback_data == data:
                    new_row.append(InlineKeyboardButton("❌ " + btn.text, callback_data="ignore"))
                else:
                    new_row.append(InlineKeyboardButton(btn.text, callback_data="ignore"))
            new_kb.append(new_row)
        
        new_kb.append([
            InlineKeyboardButton("⭐ حفظ السؤال", callback_data=f"fav_q_{q_id}"),
            InlineKeyboardButton("⏭️ السؤال التالي", callback_data="next_q")
        ])
        
        await query.answer(alert_msg, show_alert=True)
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_kb))
        return

    if data == "next_q":
        # طلب السؤال التالي بناءً على آخر قسم
        await send_question(context, chat_id, "عام", user_id, query.message.message_id)
        return

    await query.answer()

# ==========================================
# 7. معالجة الرسائل النصية والأوامر
# ==========================================
async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    kb = await get_keyboard(user_id)

    if text in ['/start', '🚀 ابدأ من جديد']:
        api_request("register_user", user_id=user_id, name=update.effective_user.first_name)
        welcomeText = f"مرحباً بك يا *{update.effective_user.first_name}*! 🌟🎮\n\n*📋 قواعد الإجابة الصحيحة:* \n⏱️ *الوقت:* 30 ثانية للإجابة.\n⚡ *السرعة:* أول 5 ثوانٍ تمنحك (+5 نقاط).\n🔥 *السلسلة:* 3 إجابات صحيحة تضاعف نقاطك!\n🌟 *الأسئلة الذهبية:* تضاعف رصيدك.\n\nاضغط على (🎮 *سؤال جديد*) للبدء!"
        return await update.message.reply_text(welcomeText, parse_mode="Markdown", reply_markup=kb)

    if text in ['/score', '📊 رصيدي الحالي']:
        res = api_request("get_user", user_id=user_id)
        score = res.get("score", 0)
        return await update.message.reply_text(f"🏆 رصيدك الحالي: *{score} نقطة*\n🎖️ اللقب: *{get_rank(score)}*", parse_mode="Markdown", reply_markup=kb)

    if text == '📈 إحصائيات التفاعل':
        return await send_interaction_chart(update, context)

    if text == '🎮 سؤال جديد':
        return await send_question(context, chat_id, "عام", user_id)

    # يمكن إضافة بقية معالجات النصوص هنا (استيراد/تصدير/مكتبة) بنفس الطريقة
    await update.message.reply_text("تم استلام طلبك، اختر من القائمة.", reply_markup=kb)


# ==========================================
# 8. إعداد خادم Vercel (FastAPI Webhook)
# ==========================================
ptb = Application.builder().token(BOT_TOKEN).build()
ptb.add_handler(CommandHandler("start", handle_messages))
ptb.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))
ptb.add_handler(CallbackQueryHandler(handle_callbacks))

@app.get("/{full_path:path}")
def root(full_path: str):
    return {"status": "active", "message": "Pro Bot Server is UP!"}

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
