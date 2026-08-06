import os
import json
import time
import random
import logging
from fastapi import FastAPI, Request
from supabase import create_client, Client
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
app = FastAPI()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
OWNER_ID = str(os.environ.get("ADMIN_ID", "")) 
TIME_LIMIT = 30

# ==========================================
# 1. تهيئة الاتصال بـ Supabase
# ==========================================
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logging.info("Supabase connected successfully.")
    except Exception as e:
        logging.error(f"Error connecting to Supabase: {e}")

# ==========================================
# 2. القوائم بهوية المشروع القرآني
# ==========================================
USER_KB = ReplyKeyboardMarkup([
    ["📚 مكتبة الملازم والدروس", "📝 اختبار الثقافة القرآنية"], 
    ["🏆 لوحة الشرف", "📊 رصيدي الحالي"]
], resize_keyboard=True)

ADMIN_KB = ReplyKeyboardMarkup([
    ["📚 مكتبة الملازم والدروس", "📝 اختبار الثقافة القرآنية"], 
    ["⚙️ لوحة الإدارة والتحكم"],
    ["📈 إحصائيات التفاعل", "🏆 لوحة الشرف"]
], resize_keyboard=True)

async def get_keyboard(user_id):
    if str(user_id) == OWNER_ID: return ADMIN_KB
    if supabase:
        res = supabase.table("admins").select("id").eq("id", str(user_id)).execute()
        if res.data: return ADMIN_KB
    return USER_KB

def get_rank(score):
    if score < 50: return "مبتدئ 🌱"
    if score < 150: return "مبادر ⚡"
    if score < 300: return "مجتهد 🏅"
    return "نبراس قرآني 🔥"

# ==========================================
# 3. دوال التعامل مع Supabase (قاعدة البيانات)
# ==========================================
def register_user(user_id, name):
    res = supabase.table("users").select("id").eq("id", str(user_id)).execute()
    if not res.data:
        supabase.table("users").insert({
            "id": str(user_id), "name": name, "score": 0, "streak": 0, 
            "answered": [], "state": "", "temp_data": {}
        }).execute()

def get_user_score(user_id):
    res = supabase.table("users").select("score").eq("id", str(user_id)).execute()
    return res.data[0].get("score", 0) if res.data else 0

def set_state(user_id, state, temp_data):
    supabase.table("users").update({"state": state, "temp_data": temp_data}).eq("id", str(user_id)).execute()

def get_state(user_id):
    res = supabase.table("users").select("state, temp_data").eq("id", str(user_id)).execute()
    if res.data:
        return res.data[0].get("state", ""), res.data[0].get("temp_data", {})
    return "", {}

def add_media(category, lesson, media_type, file_id):
    supabase.table("library").insert({
        "title": lesson, "category": category, "lesson": lesson, 
        "type": media_type, "file_id": file_id
    }).execute()

def get_library_data():
    res = supabase.table("library").select("*").execute()
    categories = set()
    lessons_dict = {}
    media_dict = {}
    
    for item in res.data:
        cat = item.get("category", "عام")
        les = item.get("lesson", "بدون عنوان")
        f_type = item.get("type")
        f_id = item.get("file_id")
        item_id = str(item.get("id"))

        categories.add(cat)
        if cat not in lessons_dict: lessons_dict[cat] = {}

        if les not in lessons_dict[cat]:
            lessons_dict[cat][les] = item_id

        les_id = lessons_dict[cat][les]
        if les_id not in media_dict:
            media_dict[les_id] = {"title": les, "category": cat, "files": {}}

        media_dict[les_id]["files"][f_type] = f_id
        media_dict[les_id]["db_id"] = item_id

    return {"categories": list(categories), "lessons": lessons_dict, "media": media_dict}

def submit_answer_to_db(user_id, q_id, is_correct, points, category):
    user_res = supabase.table("users").select("score, streak, answered").eq("id", str(user_id)).execute()
    if user_res.data:
        data = user_res.data[0]
        score = data.get("score", 0) + points
        streak = (data.get("streak", 0) + 1) if is_correct else 0
        answered = data.get("answered", [])
        answered.append(str(q_id))
        
        supabase.table("users").update({"score": score, "streak": streak, "answered": answered}).eq("id", str(user_id)).execute()

# ==========================================
# 4. معالجة النصوص وحالات لوحة الإدارة
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

    set_state(user_id, "WAIT_CAT", {"file_id": file_id, "type": media_type})
    await msg.reply_text("📥 **تم استلام المحتوى!**\n\n📁 ما هو **القسم** الذي ينتمي إليه هذا المحتوى؟\n*(إذا أدخلت قسماً جديداً سيتم إنشاؤه تلقائياً)*", parse_mode="Markdown")

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    kb = await get_keyboard(user_id)

    if not supabase: return await update.message.reply_text("⚠️ خطأ في الاتصال بقاعدة البيانات.")

    state, temp_data = get_state(user_id)

    if state == "WAIT_CAT":
        temp_data["category"] = text
        set_state(user_id, "WAIT_LES", temp_data)
        return await update.message.reply_text("✅ ممتاز! أرسل الآن **اسم الدرس**:\n*(إذا كان الدرس موجوداً سيتم دمج الملف معه)*", parse_mode="Markdown")

    if state == "WAIT_LES":
        add_media(temp_data["category"], text, temp_data["type"], temp_data["file_id"])
        set_state(user_id, "", {})
        return await update.message.reply_text(f"🎉 تم الحفظ بنجاح!\nالقسم: {temp_data['category']}\nالدرس: {text}", parse_mode="Markdown")

    if state == "WAIT_Q_CAT":
        temp_data["q_cat"] = text
        set_state(user_id, "WAIT_Q_TEXT", temp_data)
        return await update.message.reply_text("📝 أرسل الآن **نص السؤال**:")
        
    if state == "WAIT_Q_TEXT":
        temp_data["q_text"] = text
        set_state(user_id, "WAIT_Q_CORRECT", temp_data)
        return await update.message.reply_text("✅ أرسل الآن **الإجابة الصحيحة**:")
        
    if state == "WAIT_Q_CORRECT":
        temp_data["q_correct"] = text
        set_state(user_id, "WAIT_Q_WRONG", temp_data)
        return await update.message.reply_text("❌ أرسل الآن **الإجابات الخاطئة** مفصولة بفاصلة (,):\n*(مثال: خطأ أول, خطأ ثاني, خطأ ثالث)*")
        
    if state == "WAIT_Q_WRONG":
        wrongs = [w.strip() for w in text.split(',')]
        supabase.table("questions").insert({
            "category": temp_data["q_cat"],
            "question": temp_data["q_text"],
            "correct": temp_data["q_correct"],
            "wrong": wrongs
        }).execute()
        set_state(user_id, "", {})
        return await update.message.reply_text("🎉 تم إضافة السؤال لقاعدة البيانات بنجاح!", reply_markup=kb)

    if text == '⚙️ لوحة الإدارة والتحكم' and kb == ADMIN_KB:
        btns = [
            [InlineKeyboardButton("➕ إضافة سؤال جديد", callback_data="admin_add_q")],
            [InlineKeyboardButton("🗑️ حذف درس أو محتوى", callback_data="admin_del_lib")],
            [InlineKeyboardButton("❌ إلغاء الأمر الحالي", callback_data="admin_cancel")]
        ]
        return await update.message.reply_text("⚙️ **لوحة تحكم المشرفين**\n\n- لإضافة (درس/مقطع/ملزمة): قم بإرسال الملف مباشرة للبوت.\n- لإدارة الأسئلة والمحتوى: استخدم الأزرار بالأسفل 👇", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if text in ['/start']:
        register_user(user_id, update.effective_user.first_name)
        return await update.message.reply_text("بسم الله الرحمن الرحيم\nأهلاً بك في **منصة المشروع القرآني** 📖\nاختر من القائمة بالأسفل 👇", parse_mode="Markdown", reply_markup=kb)

    if text in ['/score', '📊 رصيدي الحالي']:
        score = get_user_score(user_id)
        return await update.message.reply_text(f"🏆 نقاطك التراكمية: *{score}*\n🎖️ التقييم: *{get_rank(score)}*", parse_mode="Markdown", reply_markup=kb)

    if text == '📚 مكتبة الملازم والدروس':
        cats = get_library_data().get("categories", [])
        if not cats: return await update.message.reply_text("المكتبة قيد التجهيز.", reply_markup=kb)
        btns = [[InlineKeyboardButton(f"📁 {c}", callback_data=f"cat_{c[:50]}")] for c in cats]
        return await update.message.reply_text("📚 **أقسام المشروع القرآني:**\nاختر القسم المطلوب:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")
        
    if text == '📝 اختبار الثقافة القرآنية':
        cats = get_library_data().get("categories", [])
        if cats:
            btns = [[InlineKeyboardButton(f"📝 {c}", callback_data=f"quiz_{c[:50]}")] for c in cats]
            btns.append([InlineKeyboardButton("🎲 اختبار عشوائي شامل", callback_data="quiz_عام")])
            return await update.message.reply_text("اختر القسم الذي تود اختباره:", reply_markup=InlineKeyboardMarkup(btns))
        else:
            return await send_question(context, chat_id, "عام", user_id)
        
    await update.message.reply_text("نرجو اختيار أحد الخيارات من القائمة السفلية.", reply_markup=kb)

# ==========================================
# 5. التفاعل مع الأزرار الشفافة
# ==========================================
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id, user_id = query.message.chat_id, str(query.from_user.id)

    if data == "ignore": return await query.answer("مغلق")
    
    if data == "admin_cancel":
        set_state(user_id, "", {})
        await query.message.delete()
        return await context.bot.send_message(chat_id, "تم إلغاء الأمر بنجاح ✅")

    if data == "admin_add_q":
        set_state(user_id, "WAIT_Q_CAT", {})
        return await query.edit_message_text("📁 أرسل اسم **القسم** الذي تريد إضافة السؤال إليه:")

    if data == "admin_del_lib":
        cats = get_library_data().get("categories", [])
        if not cats: return await query.edit_message_text("المكتبة فارغة.")
        btns = [[InlineKeyboardButton(f"🗑️ حذف من: {c}", callback_data=f"delcat_{c[:50]}")] for c in cats]
        return await query.edit_message_text("اختر القسم الذي تريد حذف محتوى منه:", reply_markup=InlineKeyboardMarkup(btns))

    if data.startswith("delcat_"):
        cat_name = data.replace("delcat_", "")
        lessons = get_library_data().get("lessons", {}).get(cat_name, {})
        btns = [[InlineKeyboardButton(f"❌ حذف درس: {les_name}", callback_data=f"delles_{les_id}")] for les_name, les_id in lessons.items()]
        btns.append([InlineKeyboardButton("🔙 تراجع", callback_data="admin_del_lib")])
        return await query.edit_message_text(f"⚠️ اختر الدرس الذي تريد حذفه من قسم ({cat_name}):\n*(سيتم حذف الدرس وكل ملفاته)*", reply_markup=InlineKeyboardMarkup(btns))

    if data.startswith("delles_"):
        les_id = data.replace("delles_", "")
        lesson_data = get_library_data().get("media", {}).get(les_id)
        if lesson_data and "title" in lesson_data:
             title = lesson_data["title"]
             supabase.table("library").delete().eq("lesson", title).execute()
             return await query.edit_message_text(f"✅ تم حذف الدرس وكل ملفاته بنجاح!")
        return await query.edit_message_text("حدث خطأ أثناء الحذف.")

    if data.startswith("cat_"):
        cat_name = data.replace("cat_", "")
        lessons = get_library_data().get("lessons", {}).get(cat_name, {})
        btns = [[InlineKeyboardButton(f"📖 {les_name}", callback_data=f"les_{les_id}")] for les_name, les_id in lessons.items()]
        return await query.edit_message_text(f"📁 **{cat_name}**\nاختر الدرس:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if data.startswith("les_"):
        les_id = data.replace("les_", "")
        lesson_data = get_library_data().get("media", {}).get(les_id)
        if not lesson_data: return await query.answer("عذراً، الدرس غير متاح.", show_alert=True)
        
        title, cat, files = lesson_data['title'], lesson_data['category'], lesson_data['files']
        btns, row = [], []
        icons = {"فيديو": "🎥", "مقطع صوتي": "🎧", "ملزمة/مستند": "📚", "صورة": "🖼️"}
        
        for f_type, f_id in files.items():
            icon = icons.get(f_type, "📁")
            row.append(InlineKeyboardButton(f"{icon} {f_type}", callback_data=f"send_{les_id}_{f_type}"))
            if len(row) == 2:
                btns.append(row)
                row = []
        if row: btns.append(row)
        
        btns.append([InlineKeyboardButton("📝 اختبر مدى استيعابك للدرس", callback_data=f"quiz_{cat[:50]}")])
        btns.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"cat_{cat[:50]}")])
        return await query.edit_message_text(f"📖 **{title}**\n📁 القسم: {cat}\n\n👇 اختر المحتوى الذي تريد عرضه:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if data.startswith("send_"):
        parts = data.split("_")
        les_id, f_type = parts[1], parts[2]
        lesson_data = get_library_data().get("media", {}).get(les_id)
        if not lesson_data: return await query.answer("عذراً، الملف غير متاح.")
        
        f_id, title = lesson_data['files'].get(f_type), lesson_data['title']
        await query.answer("⏳ جاري إرسال المحتوى...")
        caption = f"📖 **{title}**\n({f_type})"
        
        if f_type == "فيديو": await context.bot.send_video(chat_id, f_id, caption=caption, parse_mode="Markdown")
        elif f_type == "مقطع صوتي": await context.bot.send_audio(chat_id, f_id, caption=caption, parse_mode="Markdown")
        elif f_type == "صورة": await context.bot.send_photo(chat_id, f_id, caption=caption, parse_mode="Markdown")
        else: await context.bot.send_document(chat_id, f_id, caption=caption, parse_mode="Markdown")
        return

    if data.startswith("quiz_"):
        cat_name = data.replace("quiz_", "")
        await query.answer("🚀 جاري تجهيز الاختبار...")
        return await send_question(context, chat_id, cat_name, user_id, msg_id=query.message.message_id)

    if data.startswith("ans_"):
        parts = data.split("_")
        is_correct = parts[1] == "1"
        q_id, ts, is_gold, cat = parts[2], int(parts[3]), parts[4] == "1", parts[5]
        diff = int(time.time()) - ts
        
        if diff > TIME_LIMIT: return await query.edit_message_text("⏳ *انتهى الوقت المخصص للإجابة!*", parse_mode="Markdown")
        
        pts = 10 if is_correct else 0
        if is_correct and diff <= 5: pts += 5
        if is_correct and is_gold: pts *= 2
        
        submit_answer_to_db(user_id, q_id, is_correct, pts, cat)
        await query.answer(f"✅ إجابة موفقة! (+{pts})" if is_correct else "❌ إجابة غير صحيحة!", show_alert=True)
        
        new_kb = [[InlineKeyboardButton(("✅ " if b.callback_data == data and is_correct else "❌ " if b.callback_data == data else "") + b.text, callback_data="ignore")] for row in query.message.reply_markup.inline_keyboard for b in row]
        new_kb.append([InlineKeyboardButton("⏭️ السؤال التالي", callback_data=f"quiz_{cat}")])
        await query.edit_message_reply_markup(InlineKeyboardMarkup(new_kb))

async def send_question(context, chat_id, category, user_id=None, msg_id=None):
    user_res = supabase.table("users").select("answered").eq("id", str(user_id)).execute()
    answered = user_res.data[0].get("answered", []) if user_res.data else []

    q_res = supabase.table("questions").select("*").execute()
    available = [q for q in q_res.data if str(q['id']) not in answered and (category == "عام" or q.get("category") == category)]
    
    if not available:
        txt = "🎉 لقد أتممت جميع الأسئلة المتاحة!"
        return await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=msg_id) if msg_id else await context.bot.send_message(chat_id, txt)

    q = random.choice(available)
    ts, is_gold = int(time.time()), 1 if random.random() < 0.15 else 0
    
    btns = [InlineKeyboardButton(q["correct"], callback_data=f"ans_1_{q['id']}_{ts}_{is_gold}_{category}")]
    for w in q.get("wrong", []):
        if w: btns.append(InlineKeyboardButton(w, callback_data=f"ans_0_{q['id']}_{ts}_{is_gold}_{category}"))
    random.shuffle(btns)
    
    inline_kb = [[b] for b in btns] if any(len(b.text) > 20 for b in btns) else [[btns[i], btns[i+1]] if i+1 < len(btns) else [btns[i]] for i in range(0, len(btns), 2)]
    
    txt = f"📁 *{category}*\n" + ("🌟 *سؤال مضاعف النقاط!*\n" if is_gold else "") + f"\n❓ *{q['question']}*\n⏱️ أمامك {TIME_LIMIT} ثانية للإجابة!"
    if msg_id: await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=msg_id, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_kb))
    else: await context.bot.send_message(chat_id, txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_kb))

# ==========================================
# 6. تشغيل السيرفر (FastAPI)
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
