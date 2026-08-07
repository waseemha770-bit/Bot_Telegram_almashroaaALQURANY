import os
import time
import random
import logging
import asyncio
import certifi
from fastapi import FastAPI, Request
from motor.motor_asyncio import AsyncIOMotorClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
app = FastAPI()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
OWNER_ID = str(os.environ.get("ADMIN_ID", "")) 
TIME_LIMIT = 30

# ==========================================
# 1. تهيئة الاتصال بقاعدة البيانات (Async Motor)
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

user_last_action = {}

async def check_spam(user_id: str) -> bool:
    now = time.time()
    last = user_last_action.get(user_id, 0)
    if now - last < 0.6:
        return True
    user_last_action[user_id] = now
    return False

def get_rank(score):
    if score < 50: return "مبتدئ 🌱"
    if score < 150: return "مبادر ⚡"
    if score < 300: return "مجتهد 🏅"
    return "نبراس قرآني 🔥"

async def is_admin(user_id: str) -> bool:
    if str(user_id) == OWNER_ID: return True
    if db is not None:
        admin = await db.admins.find_one({"_id": str(user_id)})
        if admin: return True
    return False

# ==========================================
# 2. القوائم الرئيسية (زر البداية الواجهة الرسمية)
# ==========================================
async def get_main_menu_keyboard(user_id: str):
    admin_status = await is_admin(user_id)
    keyboard = [
        [InlineKeyboardButton("📚 مكتبة الملازم والدروس", callback_data="menu_library")],
        [InlineKeyboardButton("📝 اختبار الثقافة القرآنية", callback_data="menu_quiz")],
        [InlineKeyboardButton("📊 رصيدي التراكمي", callback_data="menu_score"),
         InlineKeyboardButton("🏆 لوحة الشرف", callback_data="menu_leaderboard")]
    ]
    if admin_status:
        keyboard.append([InlineKeyboardButton("⚙️ لوحة الإدارة والتحكم", callback_data="menu_admin")])
    return InlineKeyboardMarkup(keyboard)

# ==========================================
# 3. دوال التعامل مع القاعدة (Database Helpers)
# ==========================================
async def register_user(user_id, name):
    if db is not None:
        await db.users.update_one(
            {"_id": str(user_id)},
            {"$setOnInsert": {"name": name, "score": 0, "streak": 0, "answered": [], "state": "", "temp_data": {}}},
            upsert=True
        )

async def get_user(user_id):
    if db is not None:
        return await db.users.find_one({"_id": str(user_id)})
    return None

async def set_state(user_id, state, temp_data):
    if db is not None:
        await db.users.update_one({"_id": str(user_id)}, {"$set": {"state": state, "temp_data": temp_data}})

async def get_state(user_id):
    if db is not None:
        user = await db.users.find_one({"_id": str(user_id)})
        if user:
            return user.get("state", ""), user.get("temp_data", {})
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
# 4. معالجة الرسائل النصية وحالات الإدارة
# ==========================================
async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    text = update.message.text
    
    if await check_spam(user_id): return

    await register_user(user_id, update.effective_user.first_name)
    state, temp_data = await get_state(user_id)
    admin_chk = await is_admin(user_id)

    # معالجة إضافة محتوى (ملازم/دروس)
    if state == "WAIT_CAT":
        temp_data["category"] = text
        await set_state(user_id, "WAIT_LES", temp_data)
        return await update.message.reply_text("✅ ممتاز! أرسل الآن **اسم الدرس**:\n*(إذا كان الدرس موجوداً سيتم دمج الملف معه)*", parse_mode="Markdown")

    if state == "WAIT_LES":
        if db is not None:
            await db.library.insert_one({
                "title": text, "category": temp_data["category"], "lesson": text, 
                "type": temp_data["type"], "file_id": temp_data["file_id"], "created_at": time.time()
            })
        await set_state(user_id, "", {})
        return await update.message.reply_text(f"🎉 **تم حفظ المحتوى بنجاح!**\n\n📁 القسم: {temp_data['category']}\n📖 الدرس: {text}", parse_mode="Markdown", reply_markup=await get_main_menu_keyboard(user_id))

    # معالجة إضافة الأسئلة
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
        return await update.message.reply_text("❌ أرسل الآن **الإجابات الخاطئة** مفصولة بفاصلة (,):\n*(مثال: خطأ أول, خطأ ثاني)*")
        
    if state == "WAIT_Q_WRONG":
        wrongs = [w.strip() for w in text.split(',')]
        if db is not None:
            await db.questions.insert_one({
                "category": temp_data["q_cat"],
                "question": temp_data["q_text"],
                "correct": temp_data["q_correct"],
                "wrong": wrongs
            })
        await set_state(user_id, "", {})
        return await update.message.reply_text("🎉 **تم إضافة السؤال لقاعدة البيانات بنجاح!**", parse_mode="Markdown", reply_markup=await get_main_menu_keyboard(user_id))

    if text in ['/start', 'البداية', 'القائمة الرئيسية']:
        welcome_text = (
            "📖 *بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ*\n\n"
            "أهلاً بك في البوابة الرسمية لـ **منصة المشروع القرآني**.\n"
            "نضع بين يديك محتوى علمياً وملازم مباركة واختبارات تفاعلية لتعزيز ثقافتك القرآنية.\n\n"
            "اختر ما تود إنجازه من القائمة أدناه 👇"
        )
        return await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=await get_main_menu_keyboard(user_id))

    # رد افتراضي يوجه المستخدم للقائمة
    await update.message.reply_text("الرجاء استخدام الأزرار التفاعلية أدناه للتنقل بسلاسة 👇", reply_markup=await get_main_menu_keyboard(user_id))

async def handle_media_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not await is_admin(user_id): return

    msg = update.message
    file_id = msg.document.file_id if msg.document else msg.video.file_id if msg.video else msg.audio.file_id if msg.audio else msg.photo[-1].file_id if msg.photo else None
    media_type = "ملزمة/مستند" if msg.document else "فيديو" if msg.video else "مقطع صوتي" if msg.audio else "صورة"
    
    if not file_id: return

    if CHANNEL_ID:
        try:
            await context.bot.copy_message(chat_id=CHANNEL_ID, from_chat_id=update.effective_chat.id, message_id=msg.message_id)
        except Exception as e:
            logging.error(f"Error copying to channel: {e}")

    await set_state(user_id, "WAIT_CAT", {"file_id": file_id, "type": media_type})
    
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]])
    await msg.reply_text("📥 **تم استلام الملف وتخزينه في القناة!**\n\n📁 أرسل الآن اسم **القسم** الذي ينتمي إليه هذا المحتوى:", parse_mode="Markdown", reply_markup=keyboard)

# ==========================================
# 5. معالجة التفاعل بالأسئلة والأزرار (Callbacks)
# ==========================================
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id, user_id = query.message.chat_id, str(query.from_user.id)

    if await check_spam(user_id):
        return await query.answer("الرجاء التمهل قليلاً ✋", show_alert=False)

    if data == "ignore": return await query.answer()

    if data == "main_menu":
        await set_state(user_id, "", {})
        welcome_text = "📖 **منصة المشروع القرآني**\nاختر القسم المطلوب من القائمة أدناه 👇"
        return await query.edit_message_text(welcome_text, parse_mode="Markdown", reply_markup=await get_main_menu_keyboard(user_id))

    if data == "menu_library":
        lib_data = await get_library_data()
        cats = lib_data.get("categories", [])
        if not cats:
            return await query.edit_message_text("📚 المكتبة قيد التجهيز ولم يتم إدراج أقسام بعد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع للقائمة", callback_data="main_menu")]]))
        
        btns = [[InlineKeyboardButton(f"📁 {c}", callback_data=f"cat_{c[:50]}")] for c in cats]
        btns.append([InlineKeyboardButton("🔙 رجوع للقائمة الرئيسية", callback_data="main_menu")])
        return await query.edit_message_text("📚 **مكتبة الملازم والدروس:**\nاختر القسم المناسب للتصفح:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))

    if data == "menu_quiz":
        lib_data = await get_library_data()
        cats = lib_data.get("categories", [])
        btns = [[InlineKeyboardButton(f"📝 {c}", callback_data=f"quiz_{c[:50]}")] for c in cats]
        btns.append([InlineKeyboardButton("🎲 اختبار عشوائي شامل", callback_data="quiz_عام")])
        btns.append([InlineKeyboardButton("🔙 رجوع للقائمة الرئيسية", callback_data="main_menu")])
        return await query.edit_message_text("📝 **اختبار الثقافة القرآنية:**\nاختر القسم الذي تود اختبار استيعابك فيه:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))

    if data == "menu_score":
        user = await get_user(user_id)
        score = user.get("score", 0) if user else 0
        txt = f"📊 **رصيدك التراكمي:**\n\n🏆 النقاط: *{score}*\n🎖️ التقييم الحالي: *{get_rank(score)}*"
        return await query.edit_message_text(txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")]]))

    if data == "menu_leaderboard":
        if db is not None:
            cursor = db.users.find().sort("score", -1).limit(5)
            top_users = await cursor.to_list(length=5)
            txt = "🏆 **لوحة الشرف - الأوائل:**\n\n"
            for idx, u in enumerate(top_users, 1):
                medal = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else "🏅"
                txt += f"{medal} {u.get('name', 'مستخدم')} — *{u.get('score', 0)} نقطة*\n"
        else:
            txt = "لوحة الشرف غير متاحة حالياً."
        return await query.edit_message_text(txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")]]))

    if data == "menu_admin" and await is_admin(user_id):
        btns = [
            [InlineKeyboardButton("➕ إضافة سؤال جديد للاختبار", callback_data="admin_add_q")],
            [InlineKeyboardButton("🗑️ حذف درس أو محتوى", callback_data="admin_del_lib")],
            [InlineKeyboardButton("🔙 رجوع للقائمة الرئيسية", callback_data="main_menu")]
        ]
        return await query.edit_message_text("⚙️ **لوحة تحكم المشرفين:**\n\n- لإضافة ملازم أو فيديوهات: **أرسل الملف مباشرة للبوت هنا**.\n- لإدارة الأسئلة والمحتوى: استخدم الأزرار أدناه 👇", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))

    if data == "admin_cancel":
        await set_state(user_id, "", {})
        return await query.edit_message_text("❌ تم إلغاء العملية بنجاح.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]))

    if data == "admin_add_q":
        await set_state(user_id, "WAIT_Q_CAT", {})
        return await query.edit_message_text("📁 أرسل اسم **القسم** الذي تريد إضافة السؤال إليه:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))

    if data == "admin_del_lib":
        lib_data = await get_library_data()
        cats = lib_data.get("categories", [])
        if not cats: return await query.edit_message_text("المكتبة فارغة.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_admin")]]))
        btns = [[InlineKeyboardButton(f"🗑️ حذف من: {c}", callback_data=f"delcat_{c[:50]}")] for c in cats]
        btns.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu_admin")])
        return await query.edit_message_text("اختر القسم الذي تريد حذف محتوى منه:", reply_markup=InlineKeyboardMarkup(btns))

    if data.startswith("delcat_"):
        cat_name = data.replace("delcat_", "")
        lib_data = await get_library_data()
        lessons = lib_data.get("lessons", {}).get(cat_name, {})
        btns = [[InlineKeyboardButton(f"❌ {les_name}", callback_data=f"delles_{les_id}")] for les_name, les_id in lessons.items()]
        btns.append([InlineKeyboardButton("🔙 تراجع", callback_data="admin_del_lib")])
        return await query.edit_message_text(f"⚠️ اختر الدرس للحذف من قسم ({cat_name}):", reply_markup=InlineKeyboardMarkup(btns))

    if data.startswith("delles_"):
        les_id = data.replace("delles_", "")
        lib_data = await get_library_data()
        lesson_data = lib_data.get("media", {}).get(les_id)
        if lesson_data and "title" in lesson_data:
             if db is not None:
                 await db.library.delete_many({"lesson": lesson_data["title"]})
             return await query.edit_message_text("✅ تم حذف الدرس وكل متعلقاته بنجاح!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لوحة التحكم", callback_data="menu_admin")]]))
        return await query.edit_message_text("حدث خطأ أثناء الحذف.")

    if data.startswith("cat_"):
        cat_name = data.replace("cat_", "")
        lib_data = await get_library_data()
        lessons = lib_data.get("lessons", {}).get(cat_name, {})
        btns = [[InlineKeyboardButton(f"📖 {les_name}", callback_data=f"les_{les_id}")] for les_name, les_id in lessons.items()]
        btns.append([InlineKeyboardButton("🔙 رجوع للأقسام", callback_data="menu_library")])
        return await query.edit_message_text(f"📁 **{cat_name}**\nاختر الدرس المطلوب:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))

    if data.startswith("les_"):
        les_id = data.replace("les_", "")
        lib_data = await get_library_data()
        lesson_data = lib_data.get("media", {}).get(les_id)
        if not lesson_data: return await query.answer("عذراً، الدرس غير متاح.", show_alert=True)
        
        title, cat, files = lesson_data['title'], lesson_data['category'], lesson_data['files']
        btns, row = [], []
        icons = {"فيديو": "🎥", "مقطع صوتي": "🎧", "ملزمة/مستند": "📚", "صورة": "🖼️"}
        
        for f_type in files.keys():
            icon = icons.get(f_type, "📁")
            row.append(InlineKeyboardButton(f"{icon} {f_type}", callback_data=f"send_{les_id}_{f_type}"))
            if len(row) == 2:
                btns.append(row)
                row = []
        if row: btns.append(row)
        
        btns.append([InlineKeyboardButton("📝 اختبر مدى استيعابك للدرس", callback_data=f"quiz_{cat[:50]}")])
        btns.append([InlineKeyboardButton("🔙 رجوع للدروس", callback_data=f"cat_{cat[:50]}")])
        return await query.edit_message_text(f"📖 **{title}**\n📁 القسم: {cat}\n\n👇 اختر نوع المحتوى للعرض:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))

    if data.startswith("send_"):
        parts = data.split("_")
        les_id, f_type = parts[1], parts[2]
        lib_data = await get_library_data()
        lesson_data = lib_data.get("media", {}).get(les_id)
        if not lesson_data: return await query.answer("الملف غير متاح.")
        
        f_id, title = lesson_data['files'].get(f_type), lesson_data['title']
        await query.answer("⏳ جاري إرسال المحتوى...")
        caption = f"📖 **{title}**\n({f_type})"
        
        try:
            if f_type == "فيديو": await context.bot.send_video(chat_id, f_id, caption=caption, parse_mode="Markdown")
            elif f_type == "مقطع صوتي": await context.bot.send_audio(chat_id, f_id, caption=caption, parse_mode="Markdown")
            elif f_type == "صورة": await context.bot.send_photo(chat_id, f_id, caption=caption, parse_mode="Markdown")
            else: await context.bot.send_document(chat_id, f_id, caption=caption, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"File send error: {e}")
            await context.bot.send_message(chat_id, "⚠️ عذراً، تعذر إرسال الملف.")
        return

    if data.startswith("quiz_"):
        cat_name = data.replace("quiz_", "")
        await query.answer("🚀 تجهيز السؤال...")
        return await send_question(context, chat_id, cat_name, user_id, msg_id=query.message.message_id)

    if data.startswith("ans_"):
        parts = data.split("_")
        is_correct = parts[1] == "1"
        q_id, ts, is_gold, cat = parts[2], int(parts[3]), parts[4] == "1", parts[5]
        
        diff = int(time.time()) - ts
        if diff > TIME_LIMIT or diff < 0: 
            return await query.edit_message_text("⏳ *انتهى الوقت المخصص للإجابة!*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]))
        
        pts = 10 if is_correct else 0
        if is_correct and diff <= 5: pts += 5
        if is_correct and is_gold: pts *= 2
        
        if db is not None:
            user = await db.users.find_one({"_id": str(user_id)})
            if user:
                score = user.get("score", 0) + pts
                streak = (user.get("streak", 0) + 1) if is_correct else 0
                await db.users.update_one({"_id": str(user_id)}, {"$set": {"score": score, "streak": streak}, "$push": {"answered": str(q_id)}})
        
        await query.answer(f"✅ إجابة صحيحة! (+{pts})" if is_correct else "❌ إجابة خاطئة!", show_alert=True)
        
        new_kb = [[InlineKeyboardButton(("✅ " if b.callback_data == data and is_correct else "❌ " if b.callback_data == data else "") + b.text, callback_data="ignore")] for row in query.message.reply_markup.inline_keyboard for b in row]
        new_kb.append([InlineKeyboardButton("⏭️ السؤال التالي", callback_data=f"quiz_{cat}"), InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu")])
        await query.edit_message_reply_markup(InlineKeyboardMarkup(new_kb))

async def send_question(context, chat_id, category, user_id=None, msg_id=None):
    if db is None: return

    user = await db.users.find_one({"_id": str(user_id)})
    answered = user.get("answered", []) if user else []

    query_filter = {} if category == "عام" else {"category": category}
    cursor = db.questions.find(query_filter)
    all_qs = await cursor.to_list(length=None)
    
    available = [q for q in all_qs if str(q['_id']) not in answered]
    
    if not available:
        txt = "🎉 **ما شاء الله!**\nلقد أتممت جميع الأسئلة المتاحة في هذا القسم بنجاح."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 العودة للقائمة الرئيسية", callback_data="main_menu")]])
        return await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=msg_id, parse_mode="Markdown", reply_markup=kb) if msg_id else await context.bot.send_message(chat_id, txt, parse_mode="Markdown", reply_markup=kb)

    q = random.choice(available)
    ts, is_gold = int(time.time()), 1 if random.random() < 0.15 else 0
    q_id_str = str(q['_id'])
    
    btns = [InlineKeyboardButton(q["correct"], callback_data=f"ans_1_{q_id_str}_{ts}_{is_gold}_{category}")]
    for w in q.get("wrong", []):
        if w: btns.append(InlineKeyboardButton(w, callback_data=f"ans_0_{q_id_str}_{ts}_{is_gold}_{category}"))
    random.shuffle(btns)
    
    inline_kb = [[b] for b in btns] if any(len(b.text) > 20 for b in btns) else [[btns[i], btns[i+1]] if i+1 < len(btns) else [btns[i]] for i in range(0, len(btns), 2)]
    inline_kb.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")])
    
    txt = f"📁 *القسم: {category}*\n" + ("🌟 **سؤال مضاعف النقاط!**\n" if is_gold else "") + f"\n❓ *{q['question']}*\n\n⏱️ أمامك {TIME_LIMIT} ثانية للإجابة:"
    
    if msg_id: 
        await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=msg_id, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_kb))
    else: 
        await context.bot.send_message(chat_id, txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_kb))

# ==========================================
# 6. إعداد السيرفر وتشغيل التطبيق
# ==========================================
ptb = Application.builder().token(BOT_TOKEN).build()
ptb.add_handler(CommandHandler("start", handle_messages))
ptb.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))
ptb.add_handler(MessageHandler(filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.PHOTO, handle_media_upload))
ptb.add_handler(CallbackQueryHandler(handle_callbacks))

@app.post("/{full_path:path}")
async def process_update(request: Request):
    if not ptb._initialized: 
        await ptb.initialize()
        
    try:
        req_json = await request.json()
        update = Update.de_json(req_json, ptb.bot)
        await ptb.process_update(update)
        
        await asyncio.sleep(0.01)
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if tasks:
            await asyncio.wait(tasks, timeout=5.0)
            
    except Exception as e:
        logging.error(f"Webhook error: {e}")
        
    return {"status": "ok"}
