import os
import io
import time
import random
import logging
import asyncio
import certifi
import pandas as pd
from fastapi import FastAPI, Request
from motor.motor_asyncio import AsyncIOMotorClient
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from bson.objectid import ObjectId

logging.basicConfig(level=logging.INFO)
app = FastAPI()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = str(os.environ.get("ADMIN_ID", "")) 
CHANNEL_ID = os.environ.get("CHANNEL_ID") 
TIME_LIMIT = 30

# ==========================================
# 1. تهيئة قاعدة البيانات والكاش اللحظي
# ==========================================
MONGODB_URI = os.environ.get("MONGODB_URI")
db = None
if MONGODB_URI:
    try:
        client = AsyncIOMotorClient(MONGODB_URI, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=5000)
        db = client['quran_lms']
        logging.info("MongoDB connected successfully.")
    except Exception as e:
        logging.error(f"Error connecting to MongoDB: {e}")

GLOBAL_CACHE = {}

def clear_cache():
    GLOBAL_CACHE.clear()

user_last_action = {}

async def check_spam(user_id: str) -> bool:
    now = time.time()
    last = user_last_action.get(user_id, 0)
    if now - last < 0.25: return True
    user_last_action[user_id] = now
    return False

async def is_admin(user_id: str) -> bool:
    if str(user_id) == OWNER_ID: return True
    if db is not None:
        admin = await db.admins.find_one({"_id": str(user_id)})
        if admin: return True
    return False

# ==========================================
# 2. الواجهة الرئيسية
# ==========================================
async def get_main_keyboard(user_id: str):
    if await is_admin(user_id):
        return ReplyKeyboardMarkup([
            ["🔍 اعرف الله"],
            ["📥 استيراد إكسل", "📤 تصدير إكسل"]
        ], resize_keyboard=True)
    return ReplyKeyboardMarkup([["🔍 اعرف الله"]], resize_keyboard=True)

# ==========================================
# 3. عرض تفاصيل الدرس والوسائط
# ==========================================
async def show_lesson_ui(context, chat_id, doc_id, message_id=None):
    if db is None: return

    try: doc = await db.library.find_one({"_id": ObjectId(doc_id)})
    except: doc = None
        
    if not doc:
        txt = "⚠️ عذراً، هذا الدرس غير متوفر حالياً أو تم حذفه."
        if message_id: 
            try: await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=message_id)
            except: pass
        else: await context.bot.send_message(chat_id, txt)
        return

    lesson_title = doc.get("lesson", "بدون عنوان")
    series = doc.get("category", "عام")
    
    cache_key = f"items_{lesson_title}"
    if cache_key not in GLOBAL_CACHE:
        cursor = db.library.find({"lesson": lesson_title})
        GLOBAL_CACHE[cache_key] = await cursor.to_list(length=None)
    items = GLOBAL_CACHE[cache_key]
    
    links = {"فيديو": None, "نص": None, "صوت": None, "صور": None}
    
    for item in items:
        f_type = str(item.get("type", "نص"))
        f_link = item.get("file_id")
        
        safe_link = None
        if pd.notna(f_link) and str(f_link).strip().lower() not in ['', 'nan', 'none', 'null', 'لا يوجد']:
            safe_link = str(f_link).strip().replace(" ", "")
            if not safe_link.startswith('http'): 
                safe_link = f"https://{safe_link}"

        if safe_link:
            if "فيديو" in f_type: links["فيديو"] = safe_link
            elif "صوت" in f_type: links["صوت"] = safe_link
            elif "صور" in f_type or "صوره" in f_type: links["صور"] = safe_link
            else: links["نص"] = safe_link

    def make_btn(text, link):
        if link: return InlineKeyboardButton(text, url=link)
        return InlineKeyboardButton(text, callback_data="media_unavail")

    btns = [
        [make_btn("🎬 مشاهدة الفيديو", links["فيديو"]), make_btn("📚 قراءة الملزمة", links["نص"])],
        [make_btn("🎧 الاستماع للصوت", links["صوت"]), make_btn("🖼️ عرض الصور", links["صور"])]
    ]

    btns.append([InlineKeyboardButton("✨ 📝 ابدأ اختبار الدرس الآن ✨", callback_data=f"quizles_{doc_id}")])
    
    bot_username = context.bot.username
    share_url = f"https://t.me/share/url?text=📚 إليك هذا الدرس القيم: {lesson_title}\n&url=https://t.me/{bot_username}?start=les_{doc_id}"
    btns.append([InlineKeyboardButton("🔗 شارك هذا الدرس (لتعم الفائدة)", url=share_url)])
    
    btns.append([
        InlineKeyboardButton("🔙 رجوع للسلسلة", callback_data=f"cat_{series[:25]}"),
        InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu")
    ])

    txt = f"📖 **{lesson_title}**\n📂 السلسلة: {series}\n\n👇 اختر المحتوى للانتقال إليه:"
    
    try:
        if message_id: 
            await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=message_id, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))
        else: 
            await context.bot.send_message(chat_id, txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))
    except Exception as e:
        logging.error(f"UI Error: {e}")

# ==========================================
# 4. معالجة الرسائل، الإكسل، والتحويل من القناة
# ==========================================
async def handle_media_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    if not await is_admin(user_id): return

    msg = update.message
    user = await db.users.find_one({"_id": user_id})
    state = user.get("state", "") if user else ""
    
    if state == "WAIT_EXCEL" and msg.document:
        if not msg.document.file_name.endswith(('.xlsx', '.xls')):
            return await msg.reply_text("⚠️ يرجى رفع ملف بصيغة Excel (.xlsx) فقط.")
        
        await msg.reply_text("⏳ جاري تحليل ملف الإكسل...")
        try:
            file = await context.bot.get_file(msg.document.file_id)
            byte_array = await file.download_as_bytearray()
            xls = pd.ExcelFile(io.BytesIO(byte_array))
            
            updates_log = ""
            if 'المشروع القرأني' in xls.sheet_names:
                df_lib = pd.read_excel(xls, sheet_name='المشروع القرأني')
                await db.library.delete_many({}) 
                count = 0
                for _, row in df_lib.iterrows():
                    if pd.notna(row.get('المحاضرة /الدرس')) and pd.notna(row.get('السلسلة')):
                        await db.library.insert_one({
                            "category": str(row['السلسلة']).strip(),
                            "lesson": str(row['المحاضرة /الدرس']).strip(),
                            "type": str(row.get('النوع', 'نص')).strip() if pd.notna(row.get('النوع')) else 'نص',
                            "file_id": str(row.get('الرابط', '')).strip() if pd.notna(row.get('الرابط')) else None,
                            "created_at": time.time()
                        })
                        count += 1
                updates_log += f"✅ تم استيراد {count} درس ومحتوى.\n"
                
            if 'قيم_نفسك' in xls.sheet_names:
                df_q = pd.read_excel(xls, sheet_name='قيم_نفسك')
                await db.questions.delete_many({})
                count_q = 0
                for _, row in df_q.iterrows():
                    if pd.notna(row.get('السؤال')) and pd.notna(row.get('الإجابة_الصحيحة')):
                        wrongs = []
                        if pd.notna(row.get('الإجابة_الخاطئة_1')): wrongs.append(str(row['الإجابة_الخاطئة_1']))
                        if pd.notna(row.get('الإجابة_الخاطئة_2')): wrongs.append(str(row['الإجابة_الخاطئة_2']))
                        await db.questions.insert_one({
                            "category": str(row.get('السلسلة', 'عام')).strip(),
                            "lesson": str(row.get('المحاضرة /الدرس', 'عام')).strip(),
                            "question": str(row['السؤال']).strip(),
                            "correct": str(row['الإجابة_الصحيحة']).strip(),
                            "wrong": wrongs
                        })
                        count_q += 1
                updates_log += f"✅ تم استيراد {count_q} سؤال."

            await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}}, upsert=True)
            clear_cache() 
            return await msg.reply_text(f"🎉 **تم تحديث قاعدة البيانات بنجاح!**\n\n{updates_log}", parse_mode="Markdown")
        except Exception as e:
            return await msg.reply_text("❌ حدث خطأ أثناء معالجة ملف الإكسل.")

    # 🌟 ميزة التعرف على المحتوى المحول من القناة (أو المرفوع حديثاً) 🌟
    has_media = msg.document or msg.video or msg.audio or msg.voice or msg.photo
    if not state and has_media:
        media_type = "نص" if msg.document else "فيديو" if msg.video else "صوت" if (msg.audio or msg.voice) else "صور" if msg.photo else "نص"
        final_link = None
        
        # إذا تم التحويل من القناة (Forward)
        if msg.forward_from_chat and str(msg.forward_from_chat.type) == "channel":
            channel_username = msg.forward_from_chat.username
            if channel_username:
                final_link = f"https://t.me/{channel_username}/{msg.forward_from_message_id}"
            else:
                final_link = f"https://t.me/c/{str(msg.forward_from_chat.id).replace('-100', '')}/{msg.forward_from_message_id}"
            await msg.reply_text("📥 **تم التعرف على المحتوى المحول من القناة بنجاح!**", parse_mode="Markdown")
        
        # إذا تم الرفع المباشر للبوت
        else:
            if CHANNEL_ID:
                try:
                    copied_msg = await context.bot.copy_message(chat_id=CHANNEL_ID, from_chat_id=chat_id, message_id=msg.message_id)
                    channel_username = CHANNEL_ID.replace('@', '')
                    final_link = f"https://t.me/{channel_username}/{copied_msg.message_id}"
                except Exception as e:
                    logging.error(f"Error copying to channel: {e}")
            
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_CAT", "temp_data": {"file_id": final_link, "type": media_type}}}, upsert=True)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء الربط", callback_data="admin_cancel")]])
        await msg.reply_text("📁 أرسل الآن اسم **السلسلة** التي ينتمي إليها المحتوى:", parse_mode="Markdown", reply_markup=keyboard)


async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    text = update.message.text
    
    if await check_spam(user_id): return
    if db is None: return await update.message.reply_text("⚠️ خطأ في الاتصال بقاعدة البيانات.")

    kb = await get_main_keyboard(user_id)
    
    # استكمال عمليات الإضافة
    user = await db.users.find_one({"_id": user_id})
    state = user.get("state", "") if user else ""
    temp_data = user.get("temp_data", {}) if user else {}

    if state == "WAIT_CAT":
        temp_data["category"] = text
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_LES", "temp_data": temp_data}})
        return await update.message.reply_text("✅ ممتاز! أرسل الآن اسم **المحاضرة / الدرس**:", parse_mode="Markdown")

    if state == "WAIT_LES":
        await db.library.insert_one({"title": text, "category": temp_data["category"], "lesson": text, "type": temp_data["type"], "file_id": temp_data["file_id"], "created_at": time.time()})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}})
        clear_cache()
        return await update.message.reply_text(f"🎉 تم ربط المحتوى وتحديث البوت بنجاح!", parse_mode="Markdown")

    if text.startswith('/start'):
        await db.users.update_one({"_id": user_id}, {"$setOnInsert": {"score": 0, "streak": 0, "answered": []}}, upsert=True)
        if 'les_' in text:
            doc_id = text.replace('/start les_', '').strip()
            return await show_lesson_ui(context, chat_id, doc_id)
            
        welcome_text = "📖 **أهلاً بك في منصة المشروع القرآني**\n\nتصفح الدروس وابدأ رحلتك المعرفية بالضغط على الزر أدناه 👇"
        return await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=kb)

    if text == '🔍 اعرف الله':
        if "categories" not in GLOBAL_CACHE: 
            GLOBAL_CACHE["categories"] = await db.library.distinct("category")
        categories = GLOBAL_CACHE["categories"]
        
        if not categories: return await update.message.reply_text("📚 السلاسل قيد التجهيز.", reply_markup=kb)
        btns = [[InlineKeyboardButton(f"📂 | {c}", callback_data=f"cat_{c[:25]}")] for c in categories]
        return await update.message.reply_text("📚 **المشروع القرآني:**\nيرجى اختيار السلسلة المطلوبة:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if text == '📥 استيراد إكسل' and await is_admin(user_id):
        btns = [[InlineKeyboardButton("✅ نعم، متأكد", callback_data="import_confirm")], [InlineKeyboardButton("❌ الإلغاء", callback_data="admin_cancel")]]
        return await update.message.reply_text("⚠️ سيتم مسح البيانات القديمة. هل أنت متأكد؟", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if text == '📤 تصدير إكسل' and await is_admin(user_id):
        await update.message.reply_text("⏳ جاري تجهيز ملف الإكسل...")
        try:
            lib_data = await db.library.find({}).to_list(length=None)
            df_lib = pd.DataFrame(lib_data)
            if not df_lib.empty:
                df_lib = df_lib.rename(columns={"category": "السلسلة", "lesson": "المحاضرة /الدرس", "type": "النوع", "file_id": "الرابط"})[["المحاضرة /الدرس", "السلسلة", "النوع", "الرابط"]]
            else: df_lib = pd.DataFrame(columns=["المحاضرة /الدرس", "السلسلة", "النوع", "الرابط"])
                
            q_data = await db.questions.find({}).to_list(length=None)
            q_list = [{"السلسلة": q.get("category", ""), "المحاضرة /الدرس": q.get("lesson", ""), "السؤال": q.get("question", ""), "الإجابة_الصحيحة": q.get("correct", ""), "الإجابة_الخاطئة_1": q.get("wrong", [])[0] if len(q.get("wrong", [])) > 0 else "", "الإجابة_الخاطئة_2": q.get("wrong", [])[1] if len(q.get("wrong", [])) > 1 else ""} for q in q_data]
            
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df_lib.to_excel(writer, sheet_name='المشروع القرأني', index=False)
                pd.DataFrame(q_list).to_excel(writer, sheet_name='قيم_نفسك', index=False)
            output.seek(0)
            return await context.bot.send_document(chat_id, document=output, filename="قاعدة_بيانات_البوت.xlsx")
        except: return await update.message.reply_text("❌ حدث خطأ.")

    await update.message.reply_text("الرجاء استخدام الأزرار أدناه 👇", reply_markup=kb)

# ==========================================
# 5. التفاعل السريع مع الأزرار الشفافة
# ==========================================
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try: await query.answer()
    except: pass

    data = query.data
    chat_id, user_id = query.message.chat_id, str(query.from_user.id)

    if await check_spam(user_id): return
    if data == "ignore": return 
    
    if data == "media_unavail":
        try: await context.bot.answer_callback_query(query.id, "⚠️ هذا المحتوى غير متوفر حالياً.", show_alert=True)
        except: pass
        return

    if data == "main_menu":
        if "categories" not in GLOBAL_CACHE: GLOBAL_CACHE["categories"] = await db.library.distinct("category")
        btns = [[InlineKeyboardButton(f"📂 | {c}", callback_data=f"cat_{c[:25]}")] for c in GLOBAL_CACHE["categories"]]
        return await query.edit_message_text("📚 **المشروع القرآني:**\nيرجى اختيار السلسلة المطلوبة:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if data == "admin_cancel":
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}}, upsert=True)
        await query.message.delete()
        return

    if data == "import_confirm":
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_EXCEL"}}, upsert=True)
        return await query.edit_message_text("📥 **أرسل ملف الإكسل (.xlsx) الآن...**", parse_mode="Markdown")

    if data.startswith("cat_"):
        cat_name = data.replace("cat_", "")
        
        cache_key = f"cat_les_{cat_name}"
        if cache_key not in GLOBAL_CACHE:
            pipeline = [{"$match": {"category": {"$regex": f"^{cat_name}"}}}, {"$group": {"_id": "$lesson", "doc_id": {"$first": "$_id"}}}]
            GLOBAL_CACHE[cache_key] = await db.library.aggregate(pipeline).to_list(length=None)
        
        btns = [[InlineKeyboardButton(f"📖 | {les['_id']}", callback_data=f"les_{str(les['doc_id'])}")] for les in GLOBAL_CACHE[cache_key]]
        btns.append([InlineKeyboardButton("🔙 العودة للرئيسية", callback_data="main_menu")])
        return await query.edit_message_text(f"📂 **السلسلة:**\nاختر المحاضرة أو الدرس المطلوب:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if data.startswith("les_"):
        return await show_lesson_ui(context, chat_id, data.replace("les_", ""), message_id=query.message.message_id)

    if data.startswith("quizles_"):
        try: await context.bot.answer_callback_query(query.id, "🚀 جاري التجهيز...", show_alert=False)
        except: pass
        doc_id = data.replace("quizles_", "")
        doc = await db.library.find_one({"_id": ObjectId(doc_id)})
        if not doc: return
        return await send_question(context, chat_id, lesson=doc.get("lesson"), user_id=user_id, msg_id=query.message.message_id, back_doc_id=doc_id)

    # 🌟 تعديل الاختبار: تحديد الزر المنقور فقط 🌟
    if data.startswith("ans_"):
        parts = data.split("_")
        is_correct = parts[1] == "1"
        q_id, ts = parts[2], int(parts[3])
        
        diff = int(time.time()) - ts
        if diff > TIME_LIMIT or diff < 0: 
            return await query.edit_message_text("⏳ *انتهى الوقت المخصص للإجابة!*", parse_mode="Markdown")
        
        # تغيير الزر الذي تم نقره فقط لمنع التشتت
        new_kb = []
        for row in query.message.reply_markup.inline_keyboard:
            new_row = []
            for b in row:
                if b.callback_data == data:
                    new_row.append(InlineKeyboardButton(b.text + (" ✅" if is_correct else " ❌"), callback_data="ignore"))
                else:
                    new_row.append(InlineKeyboardButton(b.text, callback_data=b.callback_data))
            new_kb.append(new_row)
            
        await query.edit_message_reply_markup(InlineKeyboardMarkup(new_kb))
        
        if db is not None:
            await db.users.update_one({"_id": str(user_id)}, {"$push": {"answered": str(q_id)}})
        return

async def send_question(context, chat_id, lesson, user_id=None, msg_id=None, back_doc_id=None):
    if db is None: return

    user = await db.users.find_one({"_id": str(user_id)})
    answered = user.get("answered", []) if user else []

    cache_key = f"q_{lesson}"
    if cache_key not in GLOBAL_CACHE:
        GLOBAL_CACHE[cache_key] = await db.questions.find({"lesson": lesson}).to_list(length=None)
        
    available = [q for q in GLOBAL_CACHE[cache_key] if str(q['_id']) not in answered]
    
    if not available:
        txt = "🎉 **أتممت جميع أسئلة هذا الدرس بنجاح!**"
        btns = []
        if back_doc_id: btns.append([InlineKeyboardButton("🔙 العودة للدرس", callback_data=f"les_{back_doc_id}")])
        btns.append([InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu")])
        if msg_id: await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=msg_id, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))
        else: await context.bot.send_message(chat_id, txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))
        return

    q = random.choice(available)
    ts = int(time.time())
    q_id_str = str(q['_id'])
    
    btns = [InlineKeyboardButton(q["correct"], callback_data=f"ans_1_{q_id_str}_{ts}")]
    for w in q.get("wrong", []):
        if w and str(w).lower() != 'nan': btns.append(InlineKeyboardButton(w, callback_data=f"ans_0_{q_id_str}_{ts}"))
    random.shuffle(btns)
    
    inline_kb = [[b] for b in btns] 
    
    nav_row = []
    if back_doc_id: nav_row.append(InlineKeyboardButton("🔙 إنهاء الاختبار", callback_data=f"les_{back_doc_id}"))
    nav_row.append(InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu"))
    inline_kb.append(nav_row)
    
    txt = f"📖 **المحاضرة:** {lesson}\n\n❓ *{q['question']}*\n\n⏱️ أمامك {TIME_LIMIT} ثانية للإجابة!"
    
    if msg_id: await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=msg_id, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_kb))
    else: await context.bot.send_message(chat_id, txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_kb))

# ==========================================
# 6. تشغيل السيرفر (FastAPI)
# ==========================================
ptb = Application.builder().token(BOT_TOKEN).build()
ptb.add_handler(CommandHandler("start", handle_messages))
ptb.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))
ptb.add_handler(MessageHandler(filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.PHOTO, handle_media_upload))
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
        if tasks: await asyncio.wait(tasks, timeout=3.0)
    except Exception as e:
        logging.error(f"Webhook error: {e}")
    return {"status": "ok"}
