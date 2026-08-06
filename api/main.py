import os
import json
import time
import random
import logging
import requests
from fastapi import FastAPI, Request
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
app = FastAPI()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
GAS_WEB_APP_URL = os.environ.get("GAS_WEB_APP_URL")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
OWNER_ID = str(os.environ.get("ADMIN_ID", "")) 
TIME_LIMIT = 30

USER_KB = ReplyKeyboardMarkup([
    ["📚 مكتبة الملازم والدروس", "📝 اختبار الثقافة القرآنية"], 
    ["🏆 لوحة الشرف", "📊 رصيدي الحالي"]
], resize_keyboard=True)

ADMIN_KB = ReplyKeyboardMarkup([
    ["📚 مكتبة الملازم والدروس", "📝 اختبار الثقافة القرآنية"], 
    ["📈 إحصائيات التفاعل", "🏆 لوحة الشرف"]
], resize_keyboard=True)

def api_request(action, **kwargs):
    payload = {"action": action}
    payload.update(kwargs)
    try:
        return requests.post(GAS_WEB_APP_URL, json=payload, timeout=15).json()
    except Exception as e:
        logging.error(f"GAS Error: {e}")
        return {"status": "error"}

async def get_keyboard(user_id):
    if str(user_id) == OWNER_ID or api_request("check_admin", user_id=user_id).get("is_admin"): 
        return ADMIN_KB
    return USER_KB

def get_rank(score):
    if score < 50: return "مبتدئ 🌱"
    if score < 150: return "مبادر ⚡"
    if score < 300: return "مجتهد 🏅"
    return "نبراس قرآني 🔥"

# ==========================================
# 1. نظام الرفع والتنظيم (للمشرفين)
# ==========================================
async def handle_media_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    if await get_keyboard(user_id) == USER_KB: return

    msg = update.message
    file_id = msg.document.file_id if msg.document else msg.video.file_id if msg.video else msg.audio.file_id if msg.audio else msg.photo[-1].file_id if msg.photo else None
    media_type = "ملزمة/مستند" if msg.document else "فيديو" if msg.video else "مقطع صوتي" if msg.audio else "صورة"
    
    if not file_id: return

    if CHANNEL_ID:
        await context.bot.copy_message(chat_id=CHANNEL_ID, from_chat_id=chat_id, message_id=msg.message_id)

    api_request("set_state", user_id=user_id, state="WAIT_CAT", temp_data={"file_id": file_id, "type": media_type})
    await msg.reply_text("📥 **تم الاستلام!**\n\n📁 ما هو **القسم** الذي ينتمي إليه هذا المحتوى؟\n*(مثال: سلسلة معرفة الله، دروس رمضان...)*", parse_mode="Markdown")

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    kb = await get_keyboard(user_id)

    state_res = api_request("get_state", user_id=user_id)
    state = state_res.get("state")
    temp_data = state_res.get("temp_data", {})

    if state == "WAIT_CAT":
        temp_data["category"] = text
        api_request("set_state", user_id=user_id, state="WAIT_LES", temp_data=temp_data)
        return await update.message.reply_text("✅ ممتاز! أرسل الآن **اسم الدرس** ليتم ربط المحتوى به:\n*(مثال: الدرس الأول)*", parse_mode="Markdown")

    if state == "WAIT_LES":
        api_request("add_media", category=temp_data["category"], lesson=text, media_type=temp_data["type"], file_id=temp_data["file_id"])
        api_request("set_state", user_id=user_id, state="", temp_data={})
        return await update.message.reply_text(f"🎉 تم دمج المحتوى بنجاح!\nالقسم: {temp_data['category']}\nالدرس: {text}", parse_mode="Markdown")

    if text in ['/start']:
        api_request("register_user", user_id=user_id, name=update.effective_user.first_name)
        return await update.message.reply_text("بسم الله الرحمن الرحيم\nأهلاً بك في **منصة المشروع القرآني** 📖\nاختر من القائمة بالأسفل 👇", parse_mode="Markdown", reply_markup=kb)

    if text in ['/score', '📊 رصيدي الحالي']:
        score = api_request("get_user", user_id=user_id).get("score", 0)
        return await update.message.reply_text(f"🏆 نقاطك التراكمية: *{score}*\n🎖️ التقييم: *{get_rank(score)}*", parse_mode="Markdown", reply_markup=kb)

    if text == '📚 مكتبة الملازم والدروس':
        cats = api_request("get_library").get("categories", [])
        if not cats: return await update.message.reply_text("المكتبة قيد التجهيز.", reply_markup=kb)
        # نرسل اسم القسم مباشرة في الـ callback
        btns = [[InlineKeyboardButton(f"📁 {c}", callback_data=f"cat_{c[:50]}")] for c in cats]
        return await update.message.reply_text("📚 **أقسام المشروع القرآني:**\nاختر القسم المطلوب:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")
        
    if text == '📝 اختبار الثقافة القرآنية':
        return await send_question(context, chat_id, "عام", user_id)
        
    await update.message.reply_text("نرجو اختيار أحد الخيارات من القائمة السفلية.", reply_markup=kb)

# ==========================================
# 2. واجهة تصفح الدروس المترابطة (Callbacks)
# ==========================================
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id, user_id = query.message.chat_id, str(query.from_user.id)

    if data == "ignore": return await query.answer("مغلق")

    # 1. عرض الدروس داخل القسم
    if data.startswith("cat_"):
        cat_name = data.replace("cat_", "")
        lib_data = api_request("get_library")
        lessons = lib_data.get("lessons", {}).get(cat_name, {})
        
        if not lessons: return await query.answer("لا توجد دروس هنا بعد.", show_alert=True)
        
        btns = [[InlineKeyboardButton(f"📖 {les_name}", callback_data=f"les_{les_id}")] for les_name, les_id in lessons.items()]
        return await query.edit_message_text(f"📁 **{cat_name}**\nاختر الدرس:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    # 2. عرض لوحة تحكم الدرس (مترابطة)
    if data.startswith("les_"):
        les_id = data.replace("les_", "")
        lib_data = api_request("get_library")
        lesson_data = lib_data.get("media", {}).get(les_id)
        
        if not lesson_data: return await query.answer("عذراً، الدرس غير متاح.", show_alert=True)
        
        title = lesson_data['title']
        cat = lesson_data['category']
        files = lesson_data['files'] # { "فيديو": "file_id", "مقطع صوتي": "file_id", ... }
        
        btns = []
        row = []
        
        # إنشاء أزرار للمحتويات المتوفرة فقط في هذا الدرس
        icons = {"فيديو": "🎥", "مقطع صوتي": "🎧", "ملزمة/مستند": "📚", "صورة": "🖼️"}
        for f_type, f_id in files.items():
            icon = icons.get(f_type, "📁")
            btn = InlineKeyboardButton(f"{icon} {f_type}", callback_data=f"send_{les_id}_{f_type}")
            row.append(btn)
            if len(row) == 2:
                btns.append(row)
                row = []
        if row: btns.append(row)
        
        # ربط الاختبارات بهذا الدرس المخصص
        # في الإكسل، يجب أن يكون اسم (المجموعة/القسم) هو نفس اسم الدرس لكي تظهر أسئلته
        btns.append([InlineKeyboardButton("📝 اختبر مدى استيعابك للدرس", callback_data=f"quiz_{title[:50]}")])
        btns.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"cat_{cat[:50]}")])
        
        txt = f"📖 **{title}**\n📁 القسم: {cat}\n\n👇 اختر المحتوى الذي تريد عرضه:"
        return await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    # 3. إرسال المحتوى المطلوب للدرس
    if data.startswith("send_"):
        parts = data.split("_")
        les_id = parts[1]
        f_type = parts[2]
        
        lesson_data = api_request("get_library").get("media", {}).get(les_id)
        if not lesson_data: return await query.answer("عذراً، الملف غير متاح.")
        
        f_id = lesson_data['files'].get(f_type)
        title = lesson_data['title']
        
        await query.answer("⏳ جاري إرسال المحتوى...")
        caption = f"📖 **{title}**\n({f_type})"
        
        if f_type == "فيديو": await context.bot.send_video(chat_id, f_id, caption=caption, parse_mode="Markdown")
        elif f_type == "مقطع صوتي": await context.bot.send_audio(chat_id, f_id, caption=caption, parse_mode="Markdown")
        elif f_type == "صورة": await context.bot.send_photo(chat_id, f_id, caption=caption, parse_mode="Markdown")
        else: await context.bot.send_document(chat_id, f_id, caption=caption, parse_mode="Markdown")
        return

    # 4. تشغيل الاختبار
    if data.startswith("quiz_"):
        cat_name = data.replace("quiz_", "")
        await query.answer("🚀 جاري تجهيز الاختبار...")
        return await send_question(context, chat_id, cat_name, user_id)

    # 5. معالجة الإجابات (تمت المحافظة عليها كما هي)
    if data.startswith("ans_"):
        parts = data.split("_")
        is_correct = parts[1] == "1"
        q_id, ts, is_gold, cat = parts[2], int(parts[3]), parts[4] == "1", parts[5]
        diff = int(time.time()) - ts
        
        if diff > TIME_LIMIT: return await query.edit_message_text("⏳ *انتهى الوقت المخصص للإجابة!*", parse_mode="Markdown")
        
        pts = 10 if is_correct else 0
        if is_correct and diff <= 5: pts += 5
        if is_correct and is_gold: pts *= 2
        
        api_request("submit_answer", user_id=user_id, q_id=q_id, is_correct=is_correct, points=pts, category=cat)
        msg = f"✅ إجابة موفقة! (+{pts})" if is_correct else "❌ إجابة غير صحيحة!"
        await query.answer(msg, show_alert=True)
        
        new_kb = [[InlineKeyboardButton(("✅ " if b.callback_data == data and is_correct else "❌ " if b.callback_data == data else "") + b.text, callback_data="ignore")] for row in query.message.reply_markup.inline_keyboard for b in row]
        new_kb.append([InlineKeyboardButton("⏭️ السؤال التالي", callback_data=f"quiz_{cat}")])
        await query.edit_message_reply_markup(InlineKeyboardMarkup(new_kb))

# ==========================================
# 3. دالة إرسال الأسئلة
# ==========================================
async def send_question(context, chat_id, category, user_id=None, msg_id=None):
    res = api_request("get_question", category=category, user_id=user_id)
    if res.get("status") != "success":
        txt = "🎉 لقد أتممت جميع أسئلة هذا القسم بنجاح!"
        return await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=msg_id) if msg_id else await context.bot.send_message(chat_id, txt)

    q = res["question"]
    ts, is_gold = int(time.time()), 1 if random.random() < 0.15 else 0
    btns = [InlineKeyboardButton(q["correct"], callback_data=f"ans_1_{q['id']}_{ts}_{is_gold}_{category}")]
    for i, w in enumerate(q.get("wrong", [])):
        btns.append(InlineKeyboardButton(w, callback_data=f"ans_0_{q['id']}_{ts}_{is_gold}_{category}"))
    random.shuffle(btns)
    
    inline_kb = [[b] for b in btns] if any(len(b.text) > 20 for b in btns) else [[btns[i], btns[i+1]] if i+1 < len(btns) else [btns[i]] for i in range(0, len(btns), 2)]
    
    txt = f"📁 *{category}*\n" + ("🌟 *سؤال مضاعف النقاط!*\n" if is_gold else "") + f"\n❓ *{q['question']}*\n⏱️ أمامك {TIME_LIMIT} ثانية للإجابة!"
    if msg_id: await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=msg_id, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_kb))
    else: await context.bot.send_message(chat_id, txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_kb))

# ==========================================
# 4. تشغيل السيرفر (FastAPI)
# ==========================================
ptb = Application.builder().token(BOT_TOKEN).build()
ptb.add_handler(CommandHandler("start", handle_messages))
ptb.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))
ptb.add_handler(MessageHandler(filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.PHOTO, handle_media_upload))
ptb.add_handler(CallbackQueryHandler(handle_callbacks))

@app.post("/{full_path:path}")
async def process_update(request: Request):
    if not ptb._initialized: await ptb.initialize()
    await ptb.process_update(Update.de_json(await request.json(), ptb.bot))
    return {"status": "ok"}
