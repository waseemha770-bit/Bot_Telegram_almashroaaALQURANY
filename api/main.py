import os
import time
import random
import logging
import asyncio
import certifi
from fastapi import FastAPI, Request
from motor.motor_asyncio import AsyncIOMotorClient
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
app = FastAPI()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
OWNER_ID = str(os.environ.get("ADMIN_ID", "")) 
TIME_LIMIT = 30

# ==========================================
# 1. تهيئة الاتصال بـ MongoDB (النسخة السريعة)
# ==========================================
MONGODB_URI = os.environ.get("MONGODB_URI")
db = None

if MONGODB_URI:
    try:
        client = AsyncIOMotorClient(MONGODB_URI, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=5000)
        db = client['quran_lms']
        logging.info("Async MongoDB connected successfully.")
    except Exception as e:
        logging.error(f"Error connecting to MongoDB: {e}")

# ==========================================
# 2. القوائم الرئيسية (مطابقة لتصميم الصور)
# ==========================================
user_last_action = {}

USER_KB = ReplyKeyboardMarkup([
    ["مكتبة 📚 الملازم والدروس", "اختبار 📝 الثقافة القرآنية"],
    ["لوحة الشرف 🏆", "رصيدي الحالي 📊"]
], resize_keyboard=True)

ADMIN_KB = ReplyKeyboardMarkup([
    ["مكتبة 📚 الملازم والدروس", "اختبار 📝 الثقافة القرآنية"],
    ["لوحة الشرف 🏆", "رصيدي الحالي 📊"],
    ["⚙️ لوحة الإدارة والتحكم"]
], resize_keyboard=True)

async def get_keyboard(user_id):
    if str(user_id) == OWNER_ID: return ADMIN_KB
    if db is not None:
        admin = await db.admins.find_one({"_id": str(user_id)})
        if admin: return ADMIN_KB
    return USER_KB

def get_rank(score):
    if score < 50: return "مبتدئ 🌱"
    if score < 150: return "مبادر ⚡"
    if score < 300: return "مجتهد 🏅"
    return "نبراس قرآني 🔥"

async def check_spam(user_id: str) -> bool:
    now = time.time()
    last = user_last_action.get(user_id, 0)
    if now - last < 0.6: return True
    user_last_action[user_id] = now
    return False

# ==========================================
# 3. دوال التعامل مع MongoDB
# ==========================================
async def register_user(user_id, name):
    if db is not None:
        await db.users.update_one(
            {"_id": str(user_id)},
            {"$setOnInsert": {"name": name, "score": 0, "streak": 0, "answered": [], "state": "", "temp_data": {}}},
            upsert=True
        )

async def get_user_score(user_id):
    if db is not None:
        user = await db.users.find_one({"_id": str(user_id)})
        return user.get("score", 0) if user else 0
    return 0

async def set_state(user_id, state, temp_data):
    if db is not None:
        await db.users.update_one({"_id": str(user_id)}, {"$set": {"state": state, "temp_data": temp_data}})

async def get_state(user_id):
    if db is not None:
        user = await db.users.find_one({"_id": str(user_id)})
        if user: return user.get("state", ""), user.get("temp_data", {})
    return "", {}

async def get_library_data():
    categories, lessons_dict, media_dict = set(), {}, {}
    if db is not None:
        cursor = db.library.find()
        async for item in cursor:
            cat = item.get("category", "عام")
            les = item.get("lesson", "بدون عنوان")
            f_type = item.get("type")
            f_id = item.get("file_id")
            item_id = str(item.get("_id"))

            categories.add(cat)
            if cat not in lessons_dict: lessons_dict[cat] = {}
            if les not in lessons_dict[cat]: lessons_dict[cat][les] = item_id

            les_id = lessons_dict[cat][les]
            if les_id not in media_dict: media_dict[les_id] = {"title": les, "category": cat, "files": {}}
            media_dict[les_id]["files"][f_type] = f_id
            media_dict[les_id]["db_id"] = item_id

    return {"categories": list(categories), "lessons": lessons_dict, "media": media_dict}

# ==========================================
# 4. معالجة الرسائل والرفع (ملازم، فيديوهات)
# ==========================================
async def handle_media_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    kb = await get_keyboard(user_id)
    if kb == USER_KB: return

    msg = update.message
    file_id = msg.document.file_id if msg.document else msg.video.file_id if msg.video else msg.audio.file_id if msg.audio else msg.photo[-1].file_id if msg.photo else None
    media_type = "ملزمة/مستند" if msg.document else "فيديو" if msg.video else "مقطع صوتي" if msg.audio else "صورة"
    
    if not file_id: return

    if CHANNEL_ID:
        try:
            await context.bot.copy_message(chat_id=CHANNEL_ID, from_chat_id=chat_id, message_id=msg.message_id)
        except Exception as e:
            logging.error(f"Error copying to channel: {e}")

    await set_state(user_id, "WAIT_CAT", {"file_id": file_id, "type": media_type})
    await msg.reply_text("📥 **تم استلام المحتوى وتخزينه!**\n\n📁 أرسل الآن اسم **القسم** الذي ينتمي إليه:", parse_mode="Markdown")

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    text = update.message.text
    
    if await check_spam(user_id): return

    kb = await get_keyboard(user_id)
    if db is None: return await update.message.reply_text("⚠️ خطأ في الاتصال بقاعدة البيانات.")

    state, temp_data = await get_state(user_id)

    # إضافة الدروس والأسئلة (لوحة التحكم)
    if state == "WAIT_CAT":
        temp_data["category"] = text
        await set_state(user_id, "WAIT_LES", temp_data)
        return await update.message.reply_text("✅ أرسل الآن **اسم الدرس**:\n*(إذا كان الدرس موجوداً سيتم دمج الملف معه)*", parse_mode="Markdown")

    if state == "WAIT_LES":
        if db is not None:
            await db.library.insert_one({"title": text, "category": temp_data["category"], "lesson": text, "type": temp_data["type"], "file_id": temp_data["file_id"], "created_at": time.time()})
        await set_state(user_id, "", {})
        return await update.message.reply_text(f"🎉 تم الحفظ بنجاح!\nالقسم: {temp_data['category']}\nالدرس: {text}", parse_mode="Markdown")

    if state == "WAIT_Q_CAT":
        temp_data["q_cat"] = text
        await set_state(user_id, "WAIT_Q_TEXT", temp_data)
        return await update.message.reply_text("📝 أرسل الآن **نص السؤال**:")
        
    if state == "WAIT_Q_TEXT":
        temp_data["q_text"] = text
        await set_state(user_id, "WAIT_Q_CORRECT", temp_data)
        return await update.message.reply_text("✅ أرسل الآن **الإجابة الصحيحة**:")
        
    if state == "WAIT_Q_CORRECT":
        temp_data["q_correct"] = text
        await set_state(user_id, "WAIT_Q_WRONG", temp_data)
        return await update.message.reply_text("❌ أرسل الآن **الإجابات الخاطئة** مفصولة بفاصلة (,):")
        
    if state == "WAIT_Q_WRONG":
        wrongs = [w.strip() for w in text.split(',')]
        await db.questions.insert_one({"category": temp_data["q_cat"], "question": temp_data["q_text"], "correct": temp_data["q_correct"], "wrong": wrongs})
        await set_state(user_id, "", {})
        return await update.message.reply_text("🎉 تم إضافة السؤال بنجاح!", reply_markup=kb)

    # 🔹 التفاعل مع أزرار القائمة الرئيسية 🔹
    if text in ['/start', 'البداية', 'القائمة الرئيسية']:
        await register_user(user_id, update.effective_user.first_name)
        welcome_text = (
            "بسم الله الرحمن الرحيم\n"
            "أهلاً بك في منصة المشروع القرآني 📖\n\n"
            "اختر من القائمة بالأسفل 👇"
        )
        return await update.message.reply_text(welcome_text, reply_markup=kb)

    if text == 'مكتبة 📚 الملازم والدروس':
        lib_data = await get_library_data()
        cats = lib_data.get("categories", [])
        if not cats: return await update.message.reply_text("📚 المكتبة قيد التجهيز.", reply_markup=kb)
        btns = [[InlineKeyboardButton(f"{c} 📁", callback_data=f"cat_{c[:50]}")] for c in cats]
        return await update.message.reply_text("📚 **مكتبة الملازم والدروس:**\nاختر القسم المطلوب للتصفح:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if text == 'اختبار 📝 الثقافة القرآنية':
        lib_data = await get_library_data()
        cats = lib_data.get("categories", [])
        if cats:
            btns = [[InlineKeyboardButton(f"{c} 📝", callback_data=f"quiz_{c[:50]}")] for c in cats]
            btns.append([InlineKeyboardButton("اختبار عشوائي شامل 🎲", callback_data="quiz_عام")])
            return await update.message.reply_text("اختر القسم الذي تود اختباره:", reply_markup=InlineKeyboardMarkup(btns))
        else:
            return await send_question(context, chat_id, "عام", user_id)

    if text == 'رصيدي الحالي 📊':
        score = await get_user_score(user_id)
        return await update.message.reply_text(f"📊 **رصيدك التراكمي:**\n\n🏆 النقاط: *{score}*\n🎖️ التقييم: *{get_rank(score)}*", parse_mode="Markdown", reply_markup=kb)

    if text == 'لوحة الشرف 🏆':
        if db is not None:
            cursor = db.users.find().sort("score", -1).limit(5)
            top_users = await cursor.to_list(length=5)
            txt = "🏆 **لوحة الشرف - الأوائل:**\n\n"
            for idx, u in enumerate(top_users, 1):
                medal = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else "🏅"
                txt += f"{medal} {u.get('name', 'مستخدم')} — *{u.get('score', 0)} نقطة*\n"
        else:
            txt = "لوحة الشرف غير متاحة حالياً."
        return await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=kb)

    if text == '⚙️ لوحة الإدارة والتحكم' and kb == ADMIN_KB:
        btns = [
            [InlineKeyboardButton("➕ إضافة سؤال جديد", callback_data="admin_add_q")],
            [InlineKeyboardButton("🗑️ حذف درس أو محتوى", callback_data="admin_del_lib")],
            [InlineKeyboardButton("❌ إلغاء الأمر الحالي", callback_data="admin_cancel")]
        ]
        return await update.message.reply_text("⚙️ **لوحة تحكم المشرفين**\n\n- لإضافة (درس/مقطع/ملزمة): قم بإرسال الملف مباشرة للبوت.\n- لإدارة الأسئلة والمحتوى: استخدم الأزرار بالأسفل 👇", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")
        
    await update.message.reply_text("الرجاء الاختيار من القائمة السفلية 👇", reply_markup=kb)

# ==========================================
# 5. التفاعل مع الأزرار الشفافة الشاملة
# ==========================================
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id, user_id = query.message.chat_id, str(query.from_user.id)

    if await check_spam(user_id): return await query.answer("الرجاء التمهل! ✋", show_alert=False)
    if data == "ignore": return await query.answer()
    
    if data == "menu_library":
        lib_data = await get_library_data()
        cats = lib_data.get("categories", [])
        btns = [[InlineKeyboardButton(f"{c} 📁", callback_data=f"cat_{c[:50]}")] for c in cats]
        return await query.edit_message_text("📚 **مكتبة الملازم والدروس:**\nاختر القسم المطلوب للتصفح:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))

    if data == "admin_cancel":
        await set_state(user_id, "", {})
        await query.message.delete()
        return await context.bot.send_message(chat_id, "تم إلغاء الأمر بنجاح ✅")

    if data == "admin_add_q":
        await set_state(user_id, "WAIT_Q_CAT", {})
        return await query.edit_message_text("📁 أرسل اسم **القسم** الذي تريد إضافة السؤال إليه:")

    if data == "admin_del_lib":
        lib_data = await get_library_data()
        cats = lib_data.get("categories", [])
        if not cats: return await query.edit_message_text("المكتبة فارغة.")
        btns = [[InlineKeyboardButton(f"🗑️ حذف من: {c}", callback_data=f"delcat_{c[:50]}")] for c in cats]
        return await query.edit_message_text("اختر القسم الذي تريد حذف محتوى منه:", reply_markup=InlineKeyboardMarkup(btns))

    if data.startswith("delcat_"):
        cat_name = data.replace("delcat_", "")
        lib_data = await get_library_data()
        lessons = lib_data.get("lessons", {}).get(cat_name, {})
        btns = [[InlineKeyboardButton(f"❌ حذف درس: {les_name}", callback_data=f"delles_{les_id}")] for les_name, les_id in lessons.items()]
        btns.append([InlineKeyboardButton("🔙 تراجع", callback_data="admin_del_lib")])
        return await query.edit_message_text(f"⚠️ اختر الدرس الذي تريد حذفه من قسم ({cat_name}):", reply_markup=InlineKeyboardMarkup(btns))

    if data.startswith("delles_"):
        les_id = data.replace("delles_", "")
        lib_data = await get_library_data()
        lesson_data = lib_data.get("media", {}).get(les_id)
        if lesson_data and "title" in lesson_data:
             if db is not None: await db.library.delete_many({"lesson": lesson_data["title"]})
             return await query.edit_message_text(f"✅ تم حذف الدرس بنجاح!")
        return await query.edit_message_text("حدث خطأ أثناء الحذف.")

    if data.startswith("cat_"):
        cat_name = data.replace("cat_", "")
        lib_data = await get_library_data()
        lessons = lib_data.get("lessons", {}).get(cat_name, {})
        btns = [[InlineKeyboardButton(f"{les_name} 📖", callback_data=f"les_{les_id}")] for les_name, les_id in lessons.items()]
        btns.append([InlineKeyboardButton("رجوع ⬅️", callback_data="menu_library")])
        return await query.edit_message_text(f"📁 **{cat_name}**\nاختر الدرس:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    # مطابقة واجهة الدرس تماماً مع الصورة
    if data.startswith("les_"):
        les_id = data.replace("les_", "")
        lib_data = await get_library_data()
        lesson_data = lib_data.get("media", {}).get(les_id)
        if not lesson_data: return await query.answer("الدرس غير متاح.", show_alert=True)
        
        title, cat, files = lesson_data['title'], lesson_data['category'], lesson_data['files']
        btns, row = [], []
        icons = {"فيديو": "🎥", "مقطع صوتي": "🎧", "صورة": "🖼️", "ملزمة/مستند": "📚"}
        
        preferred_order = ["فيديو", "مقطع صوتي", "صورة", "ملزمة/مستند"]
        ordered_files = [ft for ft in preferred_order if ft in files.keys()]
        ordered_files.extend([ft for ft in files.keys() if ft not in preferred_order])
        
        for f_type in ordered_files:
            icon = icons.get(f_type, "📁")
            row.append(InlineKeyboardButton(f"{f_type} {icon}", callback_data=f"send_{les_id}_{f_type}"))
            if len(row) == 2:
                btns.append(row)
                row = []
        if row: btns.append(row)
        
        btns.append([InlineKeyboardButton("اختبر مدى استيعابك للدرس 📝", callback_data=f"quiz_{cat[:50]}")])
        btns.append([InlineKeyboardButton("رجوع ⬅️", callback_data=f"cat_{cat[:50]}")])
        
        txt = f"{title} 📖\nالقسم: {cat} 📁\n\nاختر المحتوى الذي تريد عرضه: 👇"
        return await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(btns))

    if data.startswith("send_"):
        parts = data.split("_")
        les_id, f_type = parts[1], parts[2]
        lib_data = await get_library_data()
        lesson_data = lib_data.get("media", {}).get(les_id)
        if not lesson_data: return await query.answer("عذراً، الملف غير متاح.")
        
        f_id, title = lesson_data['files'].get(f_type), lesson_data['title']
        await query.answer("⏳ جاري الإرسال...", show_alert=False)
        caption = f"{title} 📖"
        
        try:
            if f_type == "فيديو": await context.bot.send_video(chat_id, f_id, caption=caption)
            elif f_type == "مقطع صوتي": await context.bot.send_audio(chat_id, f_id, caption=caption)
            elif f_type == "صورة": await context.bot.send_photo(chat_id, f_id, caption=caption)
            else: await context.bot.send_document(chat_id, f_id, caption=caption)
        except Exception:
            await context.bot.send_message(chat_id, "⚠️ الملف غير متاح حالياً.")
        return

    if data.startswith("quiz_"):
        cat_name = data.replace("quiz_", "")
        await query.answer("🚀 جاري التجهيز...", show_alert=False)
        return await send_question(context, chat_id, cat_name, user_id, msg_id=query.message.message_id)

    # معالجة تفاعل الاختبار بدقة وتحديث الأزرار عند الإجابة
    if data.startswith("ans_"):
        parts = data.split("_")
        is_correct = parts[1] == "1"
        q_id, ts, is_gold, cat = parts[2], int(parts[3]), parts[4] == "1", parts[5]
        
        diff = int(time.time()) - ts
        if diff > TIME_LIMIT or diff < 0: 
            return await query.edit_message_text("⏳ *انتهى الوقت المخصص للإجابة!*", parse_mode="Markdown")
        
        pts = 10 if is_correct else 0
        if is_correct and diff <= 5: pts += 5
        if is_correct and is_gold: pts *= 2
        
        if db is not None:
            user = await db.users.find_one({"_id": str(user_id)})
            if user:
                score = user.get("score", 0) + pts
                streak = (user.get("streak", 0) + 1) if is_correct else 0
                await db.users.update_one({"_id": str(user_id)}, {"$set": {"score": score, "streak": streak}, "$push": {"answered": str(q_id)}})
        
        await query.answer(f"إجابة موفقة! (+{pts})" if is_correct else "إجابة خاطئة!", show_alert=False)
        
        new_kb = []
        for row in query.message.reply_markup.inline_keyboard:
            new_row = []
            for b in row:
                if b.callback_data == data:
                    new_row.append(InlineKeyboardButton(b.text + (" ✅" if is_correct else " ❌"), callback_data="ignore"))
                elif b.callback_data.startswith("ans_1_"): 
                    new_row.append(InlineKeyboardButton(b.text + " ✅", callback_data="ignore"))
                else:
                    new_row.append(InlineKeyboardButton(b.text, callback_data="ignore"))
            new_kb.append(new_row)
            
        new_kb.insert(-1, [InlineKeyboardButton("السؤال التالي ⏭️", callback_data=f"quiz_{cat}")])
        await query.edit_message_reply_markup(InlineKeyboardMarkup(new_kb))

# مطابقة واجهة الاختبار تماماً مع الصورة
async def send_question(context, chat_id, category, user_id=None, msg_id=None):
    if db is None: return

    user = await db.users.find_one({"_id": str(user_id)})
    answered = user.get("answered", []) if user else []
    streak = user.get("streak", 0) if user else 0
    score = user.get("score", 0) if user else 0

    query_filter = {} if category == "عام" else {"category": category}
    cursor = db.questions.find(query_filter)
    all_qs = await cursor.to_list(length=None)
    
    available = [q for q in all_qs if str(q['_id']) not in answered]
    
    if not available:
        txt = "🎉 **ما شاء الله!**\nلقد أتممت جميع الأسئلة المتاحة في هذا القسم."
        return await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=msg_id, parse_mode="Markdown") if msg_id else await context.bot.send_message(chat_id, txt, parse_mode="Markdown")

    q = random.choice(available)
    ts, is_gold = int(time.time()), 1 if random.random() < 0.15 else 0
    q_id_str = str(q['_id'])
    
    btns = [InlineKeyboardButton(q["correct"], callback_data=f"ans_1_{q_id_str}_{ts}_{is_gold}_{category}")]
    for w in q.get("wrong", []):
        if w: btns.append(InlineKeyboardButton(w, callback_data=f"ans_0_{q_id_str}_{ts}_{is_gold}_{category}"))
    random.shuffle(btns)
    
    # الترتيب العمودي الموضح في الصورة
    inline_kb = [[b] for b in btns] 
    
    # المؤشر السفلي الرائع الموضح في الصورة
    inline_kb.append([InlineKeyboardButton(f"سلسلة: {streak} أيام 🔥  |  نقاطك: {score} 🏆", callback_data="ignore")])
    
    txt = f"{category} 📁\n"
    if is_gold: txt += "سؤال مضاعف النقاط! 🌟\n\n"
    else: txt += "\n"
    txt += f"❓ {q['question']}\n\n⏱️ أمامك {TIME_LIMIT} ثانية للإجابة!"
    
    if msg_id: 
        await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=msg_id, reply_markup=InlineKeyboardMarkup(inline_kb))
    else: 
        await context.bot.send_message(chat_id, txt, reply_markup=InlineKeyboardMarkup(inline_kb))

# ==========================================
# 6. تشغيل السيرفر (FastAPI) و Vercel Hack
# ==========================================
ptb = Application.builder().token(BOT_TOKEN).build()
ptb.add_handler(CommandHandler("start", handle_messages))
ptb.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))
ptb.add_handler(MessageHandler(filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.PHOTO, handle_media_upload))
ptb.add_handler(CallbackQueryHandler(handle_callbacks))

@app.post("/{full_path:path}")
async def process_update(request: Request):
    if not ptb._initialized: await ptb.initialize()
    try:
        req_json = await request.json()
        update = Update.de_json(req_json, ptb.bot)
        await ptb.process_update(update)
        await asyncio.sleep(0.01)
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if tasks: await asyncio.wait(tasks, timeout=5.0)
    except Exception as e:
        logging.error(f"Webhook error: {e}")
    return {"status": "ok"}
