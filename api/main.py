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
TIME_LIMIT = 30

# ==========================================
# 1. تهيئة قاعدة البيانات (Motor Async)
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

user_last_action = {}

async def check_spam(user_id: str) -> bool:
    now = time.time()
    last = user_last_action.get(user_id, 0)
    if now - last < 0.6: return True
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
    admin = await is_admin(user_id)
    if admin:
        return ReplyKeyboardMarkup([
            ["🔍 اعرف الله"],
            ["📥 استيراد إكسل", "📤 تصدير إكسل"]
        ], resize_keyboard=True)
    return ReplyKeyboardMarkup([
        ["🔍 اعرف الله"]
    ], resize_keyboard=True)

# ==========================================
# 3. عرض تفاصيل الدرس ومشاركته (بالمعرف الفريد)
# ==========================================
async def show_lesson_ui(context, chat_id, doc_id, message_id=None):
    if db is None: return

    # البحث عن الدرس باستخدام المعرف القصير بدلاً من الاسم الطويل
    try:
        doc = await db.library.find_one({"_id": ObjectId(doc_id)})
    except:
        doc = None
        
    if not doc:
        txt = "⚠️ هذا الدرس غير متوفر حالياً أو تم حذفه."
        if message_id: await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=message_id)
        else: await context.bot.send_message(chat_id, txt)
        return

    lesson_title = doc.get("lesson", "بدون عنوان")
    series = doc.get("category", "عام")
    
    # جلب جميع الملفات المرتبطة بنفس اسم المحاضرة
    cursor = db.library.find({"lesson": lesson_title})
    items = await cursor.to_list(length=None)
    
    btns = []
    row = []
    for item in items:
        f_type = item.get("type", "نص")
        icon = "🎥" if "فيديو" in f_type else "📚" if ("كتاب" in f_type or "نص" in f_type) else "🎧" if "صوت" in f_type else "🖼️" if "صور" in f_type else "📁"
        row.append(InlineKeyboardButton(f"{f_type} {icon}", callback_data=f"send_{str(item['_id'])}"))
        if len(row) == 2:
            btns.append(row)
            row = []
    if row: btns.append(row)

    # زر الأسئلة يستخدم أيضاً الـ ID القصير
    btns.append([InlineKeyboardButton("📝 أسئلة خاصة بالدرس", callback_data=f"quizles_{doc_id}")])
    
    bot_username = context.bot.username
    share_url = f"https://t.me/share/url?text=📚 إليك هذا الدرس القيم: {lesson_title}\n&url=https://t.me/{bot_username}?start=les_{doc_id}"
    btns.append([InlineKeyboardButton("🔗 مشاركة الدرس مع المجموعة", url=share_url)])
    
    btns.append([InlineKeyboardButton("رجوع ⬅️", callback_data=f"cat_{series[:25]}")])

    txt = f"📖 **{lesson_title}**\n📁 السلسلة: {series}\n\nاختر من المحتويات التالية: 👇"
    
    if message_id:
        await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=message_id, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))
    else:
        await context.bot.send_message(chat_id, txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))

# ==========================================
# 4. معالجة الرسائل واستيراد/تصدير الإكسل
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
        
        await msg.reply_text("⏳ جاري تحليل ملف الإكسل وتحديث قاعدة البيانات...")
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
            return await msg.reply_text(f"🎉 **تم تحديث قاعدة البيانات بنجاح!**\n\n{updates_log}", parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Excel import error: {e}")
            return await msg.reply_text("❌ حدث خطأ أثناء معالجة ملف الإكسل. تأكد من تطابق أسماء الأعمدة والشيتات.")

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    text = update.message.text
    
    if await check_spam(user_id): return
    if db is None: return await update.message.reply_text("⚠️ خطأ في الاتصال بقاعدة البيانات.")

    kb = await get_main_keyboard(user_id)
    
    if text.startswith('/start'):
        await db.users.update_one({"_id": user_id}, {"$setOnInsert": {"score": 0, "streak": 0, "answered": []}}, upsert=True)
        # ميزة استقبال رابط المشاركة 
        if 'les_' in text:
            doc_id = text.replace('/start les_', '').strip()
            return await show_lesson_ui(context, chat_id, doc_id)
            
        welcome_text = "📖 **أهلاً بك في منصة المشروع القرآني**\n\nتصفح الدروس وابدأ رحلتك المعرفية بالضغط على الزر أدناه 👇"
        return await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=kb)

    if text == '🔍 اعرف الله':
        categories = await db.library.distinct("category")
        if not categories: return await update.message.reply_text("📚 السلاسل قيد التجهيز.", reply_markup=kb)
        btns = [[InlineKeyboardButton(f"📁 {c}", callback_data=f"cat_{c[:25]}")] for c in categories]
        return await update.message.reply_text("📚 **المشروع القرآني:**\nاختر السلسلة (المجموعة) المطلوبة:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if text == '📥 استيراد إكسل' and await is_admin(user_id):
        btns = [
            [InlineKeyboardButton("✅ نعم، متأكد وموافق", callback_data="import_confirm")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]
        ]
        warn_txt = "⚠️ **تنبيه هام:**\nرفع ملف إكسل جديد سيؤدي إلى **مسح البيانات القديمة** واستبدالها بالجديدة.\nهل أنت متأكد من رغبتك بالاستمرار؟"
        return await update.message.reply_text(warn_txt, reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if text == '📤 تصدير إكسل' and await is_admin(user_id):
        await update.message.reply_text("⏳ جاري سحب البيانات وتجهيز ملف الإكسل...")
        try:
            lib_cursor = db.library.find({})
            lib_data = await lib_cursor.to_list(length=None)
            df_lib = pd.DataFrame(lib_data)
            if not df_lib.empty:
                df_lib = df_lib.rename(columns={"category": "السلسلة", "lesson": "المحاضرة /الدرس", "type": "النوع", "file_id": "الرابط"})
                df_lib = df_lib[["المحاضرة /الدرس", "السلسلة", "النوع", "الرابط"]]
            else:
                df_lib = pd.DataFrame(columns=["المحاضرة /الدرس", "السلسلة", "النوع", "الرابط"])
                
            q_cursor = db.questions.find({})
            q_data = await q_cursor.to_list(length=None)
            q_list = []
            for q in q_data:
                wrongs = q.get("wrong", [])
                q_list.append({
                    "السلسلة": q.get("category", ""),
                    "المحاضرة /الدرس": q.get("lesson", ""),
                    "السؤال": q.get("question", ""),
                    "الإجابة_الصحيحة": q.get("correct", ""),
                    "الإجابة_الخاطئة_1": wrongs[0] if len(wrongs) > 0 else "",
                    "الإجابة_الخاطئة_2": wrongs[1] if len(wrongs) > 1 else ""
                })
            df_q = pd.DataFrame(q_list)
            
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df_lib.to_excel(writer, sheet_name='المشروع القرأني', index=False)
                df_q.to_excel(writer, sheet_name='قيم_نفسك', index=False)
            
            output.seek(0)
            return await context.bot.send_document(chat_id, document=output, filename="قاعدة_بيانات_البوت.xlsx")
        except Exception as e:
            logging.error(f"Export error: {e}")
            return await update.message.reply_text("❌ حدث خطأ أثناء إنشاء ملف الإكسل.")

    await update.message.reply_text("الرجاء استخدام الأزرار أدناه 👇", reply_markup=kb)

# ==========================================
# 5. التفاعل مع الأزرار الشفافة الشاملة
# ==========================================
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id, user_id = query.message.chat_id, str(query.from_user.id)

    if await check_spam(user_id): return await query.answer("الرجاء التمهل! ✋", show_alert=False)
    if data == "ignore": return await query.answer()
    
    if data == "main_menu":
        await query.answer()
        categories = await db.library.distinct("category")
        if not categories: return await query.edit_message_text("📚 السلاسل قيد التجهيز.")
        btns = [[InlineKeyboardButton(f"📁 {c}", callback_data=f"cat_{c[:25]}")] for c in categories]
        return await query.edit_message_text("📚 **المشروع القرآني:**\nاختر السلسلة (المجموعة) المطلوبة:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if data == "admin_cancel":
        await query.answer() 
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}}, upsert=True)
        await query.message.delete()
        return await context.bot.send_message(chat_id, "✅ تم الإلغاء.")

    if data == "import_confirm":
        await query.answer() 
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_EXCEL"}}, upsert=True)
        return await query.edit_message_text("📥 **أرسل ملف الإكسل (.xlsx) الآن...**\nتأكد من أن أسماء الشيتات هي: (المشروع القرأني) و (قيم_نفسك).", parse_mode="Markdown")

    if data.startswith("cat_"):
        await query.answer()
        cat_name = data.replace("cat_", "")
        
        # استخراج المعرف القصير لكل درس لتجنب تجاوز حد 64 بايت
        pipeline = [
            {"$match": {"category": {"$regex": f"^{cat_name}"}}}, 
            {"$group": {"_id": "$lesson", "doc_id": {"$first": "$_id"}}}
        ]
        cursor = db.library.aggregate(pipeline)
        lessons = await cursor.to_list(length=None)
        
        btns = []
        for les in lessons:
            btns.append([InlineKeyboardButton(f"📖 {les['_id']}", callback_data=f"les_{str(les['doc_id'])}")])
            
        btns.append([InlineKeyboardButton("رجوع للرئيسية 🏠", callback_data="main_menu")])
        return await query.edit_message_text(f"📁 **السلسلة:**\nاختر المحاضرة أو الدرس:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if data.startswith("les_"):
        await query.answer() 
        doc_id = data.replace("les_", "")
        return await show_lesson_ui(context, chat_id, doc_id, message_id=query.message.message_id)

    if data.startswith("send_"):
        item_id = data.replace("send_", "")
        item = await db.library.find_one({"_id": ObjectId(item_id)})
        
        if not item or not item.get("file_id") or str(item.get("file_id")).lower() == 'nan':
            return await query.answer("⚠️ المحتوى غير متوفر حالياً، سيتم إضافته قريباً.", show_alert=True)
            
        f_type, f_id, title = item['type'], item['file_id'], item['lesson']
        await query.answer("⏳ جاري الإرسال...", show_alert=False)
        caption = f"{title} 📖\nالنوع: {f_type}"
        
        try:
            if "فيديو" in f_type: await context.bot.send_video(chat_id, f_id, caption=caption)
            elif "صوت" in f_type: await context.bot.send_audio(chat_id, f_id, caption=caption)
            elif "صور" in f_type: await context.bot.send_photo(chat_id, f_id, caption=caption)
            else: await context.bot.send_document(chat_id, f_id, caption=caption)
        except Exception:
            await context.bot.send_message(chat_id, "⚠️ الملف غير متوفر أو حُذف من سيرفرات تيليجرام.")
        return

    if data.startswith("quizles_"):
        await query.answer("🚀 تجهيز الأسئلة...", show_alert=False)
        doc_id = data.replace("quizles_", "")
        doc = await db.library.find_one({"_id": ObjectId(doc_id)})
        if not doc: return await query.answer("البيانات غير متاحة.", show_alert=True)
        
        les_title = doc.get("lesson")
        return await send_question(context, chat_id, lesson=les_title, user_id=user_id, msg_id=query.message.message_id)

    if data.startswith("ans_"):
        parts = data.split("_")
        is_correct = parts[1] == "1"
        q_id, ts = parts[2], int(parts[3])
        
        diff = int(time.time()) - ts
        if diff > TIME_LIMIT or diff < 0: 
            return await query.edit_message_text("⏳ *انتهى الوقت المخصص للإجابة!*", parse_mode="Markdown")
        
        user = await db.users.find_one({"_id": str(user_id)})
        if user:
            await db.users.update_one({"_id": str(user_id)}, {"$push": {"answered": str(q_id)}})
        
        await query.answer(f"إجابة موفقة! ✅" if is_correct else "إجابة خاطئة! ❌", show_alert=False)
        
        new_kb = []
        for row in query.message.reply_markup.inline_keyboard:
            new_row = []
            for b in row:
                if b.callback_data == data:
                    new_row.append(InlineKeyboardButton(b.text + (" ✅" if is_correct else " ❌"), callback_data="ignore"))
                elif b.callback_data and b.callback_data.startswith("ans_1_"): 
                    new_row.append(InlineKeyboardButton(b.text + " ✅", callback_data="ignore"))
                else:
                    new_row.append(InlineKeyboardButton(b.text, callback_data=b.callback_data))
            new_kb.append(new_row)
            
        await query.edit_message_reply_markup(InlineKeyboardMarkup(new_kb))

async def send_question(context, chat_id, lesson, user_id=None, msg_id=None):
    if db is None: return

    user = await db.users.find_one({"_id": str(user_id)})
    answered = user.get("answered", []) if user else []

    cursor = db.questions.find({"lesson": lesson})
    all_qs = await cursor.to_list(length=None)
    available = [q for q in all_qs if str(q['_id']) not in answered]
    
    if not available:
        txt = "🎉 **أتممت جميع أسئلة هذا الدرس!**"
        return await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=msg_id, parse_mode="Markdown") if msg_id else await context.bot.send_message(chat_id, txt, parse_mode="Markdown")

    q = random.choice(available)
    ts = int(time.time())
    q_id_str = str(q['_id'])
    
    btns = [InlineKeyboardButton(q["correct"], callback_data=f"ans_1_{q_id_str}_{ts}")]
    for w in q.get("wrong", []):
        if w and str(w).lower() != 'nan': btns.append(InlineKeyboardButton(w, callback_data=f"ans_0_{q_id_str}_{ts}"))
    random.shuffle(btns)
    
    inline_kb = [[b] for b in btns] 
    
    txt = f"📖 المحاضرة: {lesson}\n\n❓ *{q['question']}*\n\n⏱️ أمامك {TIME_LIMIT} ثانية للإجابة!"
    
    if msg_id: await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=msg_id, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_kb))
    else: await context.bot.send_message(chat_id, txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_kb))

# ==========================================
# 5. تشغيل السيرفر (FastAPI)
# ==========================================
ptb = Application.builder().token(BOT_TOKEN).build()
ptb.add_handler(CommandHandler("start", handle_messages))
ptb.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))
ptb.add_handler(MessageHandler(filters.Document.ALL, handle_media_upload))
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
