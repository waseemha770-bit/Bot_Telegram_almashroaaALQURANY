import os
import io
import time
import random
import logging
import asyncio
import certifi
import datetime
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
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@almashro") 
TIME_LIMIT = 30

# ==========================================
# 1. تهيئة قاعدة البيانات والفهرسة
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

@app.on_event("startup")
async def startup_db_indexes():
    if db is not None:
        try:
            await db.library.create_index([("lesson", 1)])
            await db.library.create_index([("category", 1)])
            await db.questions.create_index([("lesson", 1)])
        except: pass

GLOBAL_CACHE = {}
def clear_cache(): GLOBAL_CACHE.clear()

user_last_action = {}
async def check_spam(user_id: str) -> bool:
    now = time.time()
    last = user_last_action.get(user_id, 0)
    if now - last < 0.2: return True 
    user_last_action[user_id] = now
    return False

# ==========================================
# 🌟 دالة تنظيف الشاشة (لحذف القوائم القديمة) 🌟
# ==========================================
async def clean_chat_history(user_id, chat_id, context):
    """تقوم هذه الدالة بحذف الرسالة القديمة للبوت لتجنب عناء التمرير"""
    if db is None: return
    try:
        user = await db.users.find_one({"_id": str(user_id)})
        if user and user.get("last_msg_id"):
            await context.bot.delete_message(chat_id=chat_id, message_id=user["last_msg_id"])
    except Exception:
        pass # نتجاهل الخطأ إذا كانت الرسالة محذوفة مسبقاً

# ==========================================
# دوال مساعدة (تاريخ، إعدادات، وصلاحيات)
# ==========================================
def get_auto_arabic_date():
    months = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]
    days = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
    now = datetime.datetime.now()
    return f"{days[now.weekday()]} {now.day} {months[now.month - 1]} {now.year}م"

async def get_footer_text():
    default_footer = "إشترك\nموقع يقدم الدروس القرآنية اليومية بعدة صيغ إشترك ليصلك هدى الله\nhttps://t.me/+xeTz_rpTFx82YjM0"
    if db is not None:
        settings = await db.settings.find_one({"_id": "bot_settings"})
        if settings and "footer_text" in settings: return settings["footer_text"]
    return default_footer

async def get_admin_doc(user_id: str):
    if str(user_id) == OWNER_ID: return {"_id": OWNER_ID, "permissions": {"upload": True, "questions": True, "publish": True, "stats": True}}
    if db is not None: return await db.admins.find_one({"_id": str(user_id)})
    return None

async def has_perm(user_id: str, perm: str) -> bool:
    if str(user_id) == OWNER_ID: return True
    adm = await get_admin_doc(user_id)
    if adm and adm.get("permissions", {}).get(perm, False): return True
    return False

def get_perms_kb(perms, edit_mode=False, admin_id=None):
    def mk_btn(text, key):
        mark = "✅" if perms.get(key) else "❌"
        return InlineKeyboardButton(f"{mark} | {text}", callback_data=f"adm_tgl_{key}")
    kb = [
        [mk_btn("رفع الدروس والإكسل", "upload")], [mk_btn("إضافة الأسئلة", "questions")],
        [mk_btn("النشر والاستفتاءات", "publish")], [mk_btn("الإحصائيات والتصدير", "stats")],
    ]
    if edit_mode:
        kb.append([InlineKeyboardButton("💾 | حفظ التعديلات", callback_data=f"adm_save_{admin_id}")])
        kb.append([InlineKeyboardButton("🗑️ | حذف المشرف نهائياً", callback_data=f"deladmin_{admin_id}")])
    else: kb.append([InlineKeyboardButton("💾 | حفظ وإضافة المشرف", callback_data="adm_save_new")])
    kb.append([InlineKeyboardButton("🔙 | تراجع", callback_data="admin_manage")])
    return InlineKeyboardMarkup(kb)

# ==========================================
# الواجهة الرئيسية
# ==========================================
async def get_main_keyboard(user_id: str):
    adm = await get_admin_doc(user_id)
    if not adm: return ReplyKeyboardMarkup([["🔍 اعرف الله"]], resize_keyboard=True)
    top_row = ["🔍 اعرف الله", "⚙️ لوحة الإدارة"]
    bot_row = []
    if await has_perm(user_id, "upload"): bot_row.append("📥 استيراد إكسل")
    if await has_perm(user_id, "stats"): bot_row.append("📤 تصدير إكسل")
    kb = [top_row]
    if bot_row: kb.append(bot_row)
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)

def get_type_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 فيديو", callback_data="utype_فيديو"), InlineKeyboardButton("📚 كتاب/ملزمة", callback_data="utype_نص")],
        [InlineKeyboardButton("🎧 صوت", callback_data="utype_صوت"), InlineKeyboardButton("🖼️ صور", callback_data="utype_صور")],
        [InlineKeyboardButton("📝 ملف إكسل (لإضافة الأسئلة)", callback_data="utype_أسئلة")],
        [InlineKeyboardButton("❌ إلغاء العملية", callback_data="admin_cancel")]
    ])

# ==========================================
# مصحح الروابط 
# ==========================================
def fix_link(raw_link):
    safe_link = None
    if raw_link and str(raw_link).strip().lower() not in ['', 'nan', 'none', 'null', 'لا يوجد']:
        raw_str = str(raw_link).strip().replace(" ", "")
        ch_name = CHANNEL_ID.replace('@', '').replace('https://t.me/', '')
        if "-100" in raw_str: safe_link = f"https://t.me/{ch_name}/{raw_str.split('/')[-1]}"
        elif raw_str.isdigit(): safe_link = f"https://t.me/{ch_name}/{raw_str}"
        elif raw_str.startswith("t.me/") and raw_str.split("/")[1].isdigit(): safe_link = f"https://t.me/{ch_name}/{raw_str.split('/')[1]}"
        elif raw_str.startswith("https://t.me/") and len(raw_str.split("/")) >= 4 and raw_str.split("/")[3].isdigit(): safe_link = f"https://t.me/{ch_name}/{raw_str.split('/')[3]}"
        elif raw_str.startswith("t.me/"): safe_link = f"https://{raw_str}"
        elif "/" in raw_str and not raw_str.startswith("http"): safe_link = f"https://t.me/{raw_str}"
        else: safe_link = raw_str if raw_str.startswith("http") else f"https://{raw_str}"
    return safe_link

async def background_db_update(user_id, q_id=None, is_correct=None, lesson_view=None, cat_view=None):
    if db is None: return
    try:
        await db.users.update_one({"_id": str(user_id)}, {"$set": {"last_active": time.time()}}, upsert=True)
        if q_id and is_correct is not None:
            await db.users.update_one({"_id": str(user_id)}, {"$push": {"answered": str(q_id)}})
            inc_field = "correct_answers" if is_correct else "wrong_answers"
            await db.questions.update_one({"_id": ObjectId(q_id)}, {"$inc": {inc_field: 1}})
        if lesson_view and cat_view:
            await db.lesson_stats.update_one({"lesson": lesson_view, "category": cat_view}, {"$inc": {"views": 1}}, upsert=True)
    except: pass

async def show_lesson_ui(context, chat_id, doc_id, message_id=None, user_id=None):
    if db is None: return
    try: doc = await db.library.find_one({"_id": ObjectId(doc_id)})
    except: doc = None
    if not doc:
        txt = "⚠️ عذراً، هذا الدرس غير متوفر."
        if message_id: 
            try: await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=message_id)
            except: pass
        else: await context.bot.send_message(chat_id, txt)
        return

    lesson_title = doc.get("lesson", "بدون عنوان")
    series = doc.get("category", "عام")
    if user_id: asyncio.create_task(background_db_update(user_id, lesson_view=lesson_title, cat_view=series))
    
    cache_key = f"items_{lesson_title}"
    if cache_key not in GLOBAL_CACHE:
        cursor = db.library.find({"lesson": lesson_title})
        GLOBAL_CACHE[cache_key] = await cursor.to_list(length=None)
    items = GLOBAL_CACHE[cache_key]
    
    links = {"فيديو": None, "نص": None, "صوت": None, "صور": None}
    for item in items:
        f_type = str(item.get("type", "نص"))
        safe_link = fix_link(item.get("file_id"))
        if safe_link:
            if "فيديو" in f_type: links["فيديو"] = safe_link
            elif "صوت" in f_type: links["صوت"] = safe_link
            elif "صور" in f_type or "فلاشة" in f_type: links["صور"] = safe_link
            else: links["نص"] = safe_link

    def make_btn(text, link): return InlineKeyboardButton(text, url=link) if link else InlineKeyboardButton(text, callback_data="media_unavail")

    btns = [
        [make_btn("🎬 مشاهدة الفيديو", links["فيديو"]), make_btn("📚 قراءة الملزمة", links["نص"])],
        [make_btn("🎧 الاستماع للصوت", links["صوت"]), make_btn("🖼️ عرض الصور", links["صور"])]
    ]
    btns.append([InlineKeyboardButton("✨ 📝 ابدأ اختبار الدرس الآن ✨", callback_data=f"quizles_{doc_id}")])
    bot_username = context.bot.username
    share_url = f"https://t.me/share/url?text=📚 إليك هذا الدرس القيم: {lesson_title}\n&url=https://t.me/{bot_username}?start=les_{doc_id}"
    btns.append([InlineKeyboardButton("🔗 شارك هذا الدرس (لتعم الفائدة)", url=share_url)])
    btns.append([InlineKeyboardButton("🔙 رجوع للسلسلة", callback_data=f"cat_{series[:25]}"), InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu")])

    txt = f"📖 **{lesson_title}**\n📂 السلسلة: {series}\n\n👇 اختر المحتوى للانتقال إليه:"
    try:
        if message_id: await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=message_id, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))
        else: 
            if user_id: await clean_chat_history(user_id, chat_id, context)
            sent_msg = await context.bot.send_message(chat_id, txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))
            if user_id: await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
    except: pass

# ==========================================
# معالجة رفع الملفات والمحتوى
# ==========================================
async def handle_media_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    if not await has_perm(user_id, "upload"): return

    msg = update.message
    user = await db.users.find_one({"_id": user_id})
    state = user.get("state", "") if user else ""
    
    if state == "WAIT_EXCEL" and msg.document:
        if not msg.document.file_name.endswith(('.xlsx', '.xls')): return await msg.reply_text("⚠️ يرجى رفع ملف بصيغة Excel (.xlsx) فقط.")
        await clean_chat_history(user_id, chat_id, context)
        await msg.reply_text("⏳ جاري تحليل ملف الإكسل وتطبيق فلتر منع التكرار...")
        try:
            file = await context.bot.get_file(msg.document.file_id)
            byte_array = await file.download_as_bytearray()
            xls = pd.ExcelFile(io.BytesIO(byte_array))
            updates_log, df_lib, df_q = "", None, None

            for sheet in xls.sheet_names:
                df = pd.read_excel(xls, sheet_name=sheet)
                cols = [str(c).strip() for c in df.columns]
                if any('السؤال' in c for c in cols) and any('الصحيح' in c for c in cols): df_q = df
                elif any('السلسلة' in c for c in cols) and any('الدرس' in c or 'المحاضرة' in c for c in cols): df_lib = df

            if df_lib is not None:
                await db.library.delete_many({}) 
                count, skipped, seen_links = 0, 0, set()
                for _, row in df_lib.iterrows():
                    cat_col = next((c for c in df_lib.columns if 'السلسلة' in str(c)), None)
                    les_col = next((c for c in df_lib.columns if 'الدرس' in str(c) or 'المحاضرة' in str(c)), None)
                    type_col = next((c for c in df_lib.columns if 'النوع' in str(c)), None)
                    link_col = next((c for c in df_lib.columns if 'الرابط' in str(c)), None)

                    if les_col and cat_col and pd.notna(row.get(les_col)) and pd.notna(row.get(cat_col)):
                        t_val = str(row.get(type_col, 'نص')).strip() if type_col and pd.notna(row.get(type_col)) else 'نص'
                        l_val = str(row.get(link_col, '')).strip() if link_col and pd.notna(row.get(link_col)) else None
                        if l_val and str(l_val).lower() not in ['', 'nan', 'none', 'null']:
                            link_str = str(l_val).strip()
                            clean_id = link_str.split('/')[-1] if '/' in link_str else link_str
                            if clean_id in seen_links: skipped += 1; continue
                            seen_links.add(clean_id)
                        await db.library.insert_one({"category": str(row[cat_col]).strip(), "lesson": str(row[les_col]).strip(), "type": t_val, "file_id": l_val, "created_at": time.time()})
                        count += 1
                updates_log += f"✅ تم استيراد {count} محتوى.\n"
                if skipped > 0: updates_log += f"⚠️ تم تخطي {skipped} رابط مكرر بالملف.\n"
                
            if df_q is not None:
                await db.questions.delete_many({})
                count_q = 0
                for _, row in df_q.iterrows():
                    q_col = next((c for c in df_q.columns if 'السؤال' in str(c)), None)
                    ans_col = next((c for c in df_q.columns if 'الصحيح' in str(c)), None)
                    cat_col = next((c for c in df_q.columns if 'السلسلة' in str(c)), None)
                    les_col = next((c for c in df_q.columns if 'الدرس' in str(c) or 'المحاضرة' in str(c)), None)
                    if q_col and ans_col and pd.notna(row.get(q_col)) and pd.notna(row.get(ans_col)):
                        wrongs = [str(row[wc]).strip() for wc in [c for c in df_q.columns if 'خاطئة' in str(c) or 'خطأ' in str(c)] if pd.notna(row.get(wc))]
                        cat_val = str(row.get(cat_col, 'عام')).strip() if cat_col and pd.notna(row.get(cat_col)) else 'عام'
                        les_val = str(row.get(les_col, 'عام')).strip() if les_col and pd.notna(row.get(les_col)) else 'عام'
                        await db.questions.insert_one({"category": cat_val, "lesson": les_val, "question": str(row[q_col]).strip(), "correct": str(row[ans_col]).strip(), "wrong": wrongs, "correct_answers": 0, "wrong_answers": 0})
                        count_q += 1
                updates_log += f"✅ تم استيراد {count_q} سؤال."

            if not updates_log: return await msg.reply_text("⚠️ فشل الاستيراد: لم يتم العثور على أعمدة مطابقة.")
            await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}}, upsert=True)
            clear_cache() 
            sent_msg = await msg.reply_text(f"🎉 **تم تحديث قاعدة البيانات بنجاح!**\n\n{updates_log}", parse_mode="Markdown")
            await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
            return
        except Exception: return await msg.reply_text("❌ حدث خطأ أثناء معالجة ملف الإكسل.")

    has_media = msg.document or msg.video or msg.audio or msg.voice or msg.photo
    if not state and has_media:
        final_link, telegram_file_id = None, msg.document.file_id if msg.document else None 
        if msg.forward_from_chat and str(msg.forward_from_chat.type) == "channel":
            if msg.forward_from_chat.username: final_link = f"https://t.me/{msg.forward_from_chat.username}/{msg.forward_from_message_id}"
            else: final_link = f"https://t.me/c/{str(msg.forward_from_chat.id).replace('-100', '')}/{msg.forward_from_message_id}"
        else:
            if CHANNEL_ID:
                try:
                    chat_target = CHANNEL_ID if CHANNEL_ID.startswith('@') or CHANNEL_ID.startswith('-100') else f"@{CHANNEL_ID}"
                    copied_msg = await context.bot.copy_message(chat_id=chat_target, from_chat_id=chat_id, message_id=msg.message_id)
                    ch_name = CHANNEL_ID.replace('@', '').replace('https://t.me/', '')
                    final_link = f"https://t.me/{ch_name}/{copied_msg.message_id}"
                except: pass

        if final_link:
            msg_id = final_link.split('/')[-1]
            existing = await db.library.find_one({"file_id": {"$regex": f"(^|/){msg_id}$"}})
            if existing:
                return await msg.reply_text(f"⚠️ **تنبيه:** هذا الملف موجود مسبقاً!\n📁 السلسلة: {existing.get('category')}\n📖 المحاضرة: {existing.get('lesson')}\nتم تجاهل العملية لمنع التكرار.", parse_mode="Markdown")
            
        pipeline = [{"$sort": {"_id": 1}}, {"$group": {"_id": "$category", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        cats = await db.library.aggregate(pipeline).to_list(length=None)
        
        btns = [[InlineKeyboardButton(f"📁 | {c['_id']}", callback_data=f"uc_{str(c['doc_id'])}")] for c in cats if c['_id'] and str(c['_id']).lower() != 'nan']
        btns.append([InlineKeyboardButton("➕ | إضافة سلسلة جديدة", callback_data="uc_new")])
        btns.append([InlineKeyboardButton("❌ | إلغاء الربط", callback_data="admin_cancel")])
        
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await msg.reply_text("📥 **تم استلام المحتوى بنجاح!**\n\nاختر السلسلة التي ينتمي إليها، أو أضف واحدة جديدة:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "UPLOADING", "temp_data": {"file_id": final_link, "telegram_file_id": telegram_file_id}, "last_msg_id": sent_msg.message_id}}, upsert=True)

# ==========================================
# معالجة الرسائل النصية
# ==========================================
async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    text = update.message.text
    if await check_spam(user_id): return
    if db is None: return await update.message.reply_text("⚠️ خطأ في الاتصال بقاعدة البيانات.")

    asyncio.create_task(background_db_update(user_id))
    kb = await get_main_keyboard(user_id)
    
    if text in ['إلغاء', '❌ إلغاء', '/cancel']:
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}})
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text("✅ تم إلغاء العملية.", reply_markup=kb)
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

    if text.startswith('/start'):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}, "$setOnInsert": {"score": 0, "streak": 0, "answered": []}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        if 'les_' in text: return await show_lesson_ui(context, chat_id, text.replace('/start les_', '').strip(), user_id=user_id)
        sent_msg = await update.message.reply_text("📖 **أهلاً بك في منصة المشروع القرآني**\n\nتصفح الدروس وابدأ رحلتك المعرفية بالضغط على الزر أدناه 👇", parse_mode="Markdown", reply_markup=kb)
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

    if text == '🔍 اعرف الله':
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}})
        if "categories" not in GLOBAL_CACHE: 
            pipeline = [{"$sort": {"_id": 1}}, {"$group": {"_id": "$category", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
            cats = await db.library.aggregate(pipeline).to_list(length=None)
            GLOBAL_CACHE["categories"] = [c["_id"] for c in cats if c["_id"] and str(c["_id"]).lower() != 'nan']
        
        await clean_chat_history(user_id, chat_id, context)
        if not GLOBAL_CACHE["categories"]: 
            sent_msg = await update.message.reply_text("📚 السلاسل قيد التجهيز.", reply_markup=kb)
            await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
            return
        
        btns = [[InlineKeyboardButton(f"📂 | {c}", callback_data=f"cat_{c[:50]}")] for c in GLOBAL_CACHE["categories"]]
        sent_msg = await update.message.reply_text("📚 **المشروع القرآني:**\nيرجى اختيار السلسلة المطلوبة:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

    adm = await get_admin_doc(user_id)
    if text == '⚙️ لوحة الإدارة' and adm:
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}})
        btns = []
        if await has_perm(user_id, "publish"):
            btns.append([InlineKeyboardButton("📢 | نشر درس للقناة", callback_data="admin_pub_menu")])
            btns.append([InlineKeyboardButton("🔗 | تعديل رابط/نص الاشتراك", callback_data="admin_edit_footer")])
            btns.append([InlineKeyboardButton("📊 | إنشاء استفتاء", callback_data="admin_poll")])
        if await has_perm(user_id, "questions"):
            btns.append([InlineKeyboardButton("➕ | إضافة سؤال لدرس", callback_data="admin_add_q")])
        if await has_perm(user_id, "stats"):
            btns.append([InlineKeyboardButton("📈 | الإحصائيات الشاملة", callback_data="admin_stats")])
        if str(user_id) == OWNER_ID: 
            btns.append([InlineKeyboardButton("👥 | إدارة المشرفين", callback_data="admin_manage")])
        btns.append([InlineKeyboardButton("❌ | إغلاق اللوحة", callback_data="admin_cancel")])
        
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text("⚙️ **لوحة التحكم والإدارة:**\nاختر الإجراء المطلوب:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

    if text == '📥 استيراد إكسل' and await has_perm(user_id, "upload"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}})
        btns = [[InlineKeyboardButton("✅ نعم، متأكد", callback_data="import_confirm")], [InlineKeyboardButton("❌ الإلغاء", callback_data="admin_cancel")]]
        
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text("⚠️ سيتم مسح البيانات القديمة بالكامل.\nهل أنت متأكد من رغبتك بالاستمرار؟", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

    if text == '📤 تصدير إكسل' and await has_perm(user_id, "stats"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}})
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text("⏳ جاري تجهيز ملف الإكسل...")
        try:
            lib_data = await db.library.find({}).to_list(length=None)
            df_lib = pd.DataFrame(lib_data)
            if not df_lib.empty: df_lib = df_lib.rename(columns={"category": "السلسلة", "lesson": "المحاضرة /الدرس", "type": "النوع", "file_id": "الرابط"})[["المحاضرة /الدرس", "السلسلة", "النوع", "الرابط"]]
            else: df_lib = pd.DataFrame(columns=["المحاضرة /الدرس", "السلسلة", "النوع", "الرابط"])
            q_data = await db.questions.find({}).to_list(length=None)
            q_list = [{"السلسلة": q.get("category", ""), "المحاضرة /الدرس": q.get("lesson", ""), "السؤال": q.get("question", ""), "الإجابة_الصحيحة": q.get("correct", ""), "الإجابة_الخاطئة_1": q.get("wrong", [])[0] if len(q.get("wrong", [])) > 0 else "", "الإجابة_الخاطئة_2": q.get("wrong", [])[1] if len(q.get("wrong", [])) > 1 else ""} for q in q_data]
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df_lib.to_excel(writer, sheet_name='المشروع القرأني', index=False)
                pd.DataFrame(q_list).to_excel(writer, sheet_name='قيم_نفسك', index=False)
            output.seek(0)
            await context.bot.delete_message(chat_id=chat_id, message_id=sent_msg.message_id)
            await context.bot.send_document(chat_id, document=output, filename="قاعدة_بيانات_البوت.xlsx")
        except: pass
        return

    # ================= حالات الإدخال النصي =================
    user = await db.users.find_one({"_id": user_id})
    state = user.get("state", "") if user else ""
    temp_data = user.get("temp_data", {}) if user else {}

    if state == "WAIT_FOOTER_TEXT":
        await db.settings.update_one({"_id": "bot_settings"}, {"$set": {"footer_text": text}}, upsert=True)
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}})
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text("✅ **تم حفظ نص/رابط الاشتراك بنجاح!**", parse_mode="Markdown", reply_markup=kb)
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

    if state == "WAIT_ADMIN_ID" and user_id == OWNER_ID:
        new_admin = text.strip()
        if not new_admin.isdigit(): return await update.message.reply_text("⚠️ الآيدي يجب أن يكون أرقاماً فقط.")
        perms = {"upload": False, "questions": False, "publish": False, "stats": False}
        temp_data = {"new_admin_id": new_admin, "admin_perms": perms}
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": temp_data}})
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text(f"⚙️ **تحديد صلاحيات المشرف ({new_admin}):**\nانقر على الصلاحيات لتفعيلها ✅ أو تعطيلها ❌ ثم اضغط حفظ:", reply_markup=get_perms_kb(perms, edit_mode=False))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

    if state == "WAIT_UPL_LES_TEXT":
        temp_data["lesson"] = text
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_TYPE", "temp_data": temp_data}})
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text(f"📖 المحاضرة: **{text}**\n\n👇 ما هو **نوع** هذا المحتوى؟", parse_mode="Markdown", reply_markup=get_type_keyboard())
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

    if state == "WAIT_Q_TEXT":
        temp_data["q_text"] = text
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_Q_CORRECT", "temp_data": temp_data}})
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text("✅ ممتاز.\nأرسل الآن **الإجابة الصحيحة**:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

    if state == "WAIT_Q_CORRECT":
        temp_data["q_correct"] = text
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_Q_WRONG", "temp_data": temp_data}})
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text("❌ أرسل الآن **الإجابات الخاطئة** مفصولة بفاصلة:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

    if state == "WAIT_Q_WRONG":
        wrongs = [w.strip() for w in text.split(',') if w.strip()]
        await db.questions.insert_one({"category": temp_data.get("q_cat"), "lesson": temp_data.get("q_les"), "question": temp_data.get("q_text"), "correct": temp_data.get("q_correct"), "wrong": wrongs, "correct_answers": 0, "wrong_answers": 0})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}})
        clear_cache()
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text("🎉 **تم حفظ السؤال بنجاح!**", parse_mode="Markdown", reply_markup=kb)
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

    if state == "WAIT_POLL_Q":
        temp_data["poll_q"] = text
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_POLL_OPT", "temp_data": temp_data}})
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text("✅ أرسل الآن **خيارات الاستفتاء** مفصولة بفاصلة:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

    if state == "WAIT_POLL_OPT":
        options = [opt.strip() for opt in text.split(',') if opt.strip()]
        if len(options) < 2 or len(options) > 10: return await update.message.reply_text("⚠️ الخيارات يجب أن تكون بين 2 و 10.")
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}})
        await clean_chat_history(user_id, chat_id, context)
        if CHANNEL_ID:
            try:
                await context.bot.send_poll(chat_id=CHANNEL_ID, question=temp_data["poll_q"], options=options, is_anonymous=True)
                sent_msg = await update.message.reply_text("🎉 **تم نشر الاستفتاء في القناة!**", reply_markup=kb)
                await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
                return
            except Exception as e: 
                sent_msg = await update.message.reply_text(f"❌ حدث خطأ:\n`{e}`", parse_mode="Markdown")
                await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
                return
        sent_msg = await update.message.reply_text("⚠️ لم يتم إعداد معرف القناة.")
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

    # مسح القائمة إذا أرسل المستخدم أي نص آخر
    await clean_chat_history(user_id, chat_id, context)
    sent_msg = await update.message.reply_text("الرجاء استخدام الأزرار أدناه 👇", reply_markup=kb)
    await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})

# ==========================================
# معالجة تفاعلات الأزرار
# ==========================================
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id, user_id = query.message.chat_id, str(query.from_user.id)
    if await check_spam(user_id): return
    
    user = await db.users.find_one({"_id": user_id})
    last_msg_id = user.get("last_msg_id") if user else None
    if last_msg_id and query.message.message_id != last_msg_id:
        try: await context.bot.answer_callback_query(query.id, "⚠️ قائمة قديمة.", show_alert=True)
        except: pass
        return

    data = query.data
    if data == "media_unavail":
        try: await context.bot.answer_callback_query(query.id, "⚠️ غير متوفر.", show_alert=True)
        except: pass
        return

    try: await query.answer()
    except: pass
    if data == "ignore": return 

    if data.startswith("adm_tgl_") and user_id == OWNER_ID:
        perm_key = data.replace("adm_tgl_", "")
        temp_data = user.get("temp_data", {})
        perms = temp_data.get("admin_perms", {})
        perms[perm_key] = not perms.get(perm_key, False)
        temp_data["admin_perms"] = perms
        await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": temp_data}})
        edit_id = temp_data.get("edit_admin_id")
        try: await query.edit_message_reply_markup(get_perms_kb(perms, edit_mode=bool(edit_id), admin_id=edit_id))
        except: pass
        return

    if data == "adm_save_new" and user_id == OWNER_ID:
        temp_data = user.get("temp_data", {})
        new_id = temp_data.get("new_admin_id")
        perms = temp_data.get("admin_perms", {})
        if new_id:
            await db.admins.update_one({"_id": new_id}, {"$set": {"added_at": time.time(), "permissions": perms}}, upsert=True)
            await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}})
            return await query.edit_message_text(f"✅ تم إضافة المشرف ({new_id}) بنجاح!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_manage")]]))

    if data.startswith("adm_save_") and data != "adm_save_new" and user_id == OWNER_ID:
        target_id = data.replace("adm_save_", "")
        perms = user.get("temp_data", {}).get("admin_perms", {})
        await db.admins.update_one({"_id": target_id}, {"$set": {"permissions": perms}}, upsert=True)
        return await query.edit_message_text(f"✅ تم تحديث الصلاحيات بنجاح!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_manage")]]))

    if data == "admin_edit_footer" and await has_perm(user_id, "publish"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_FOOTER_TEXT"}})
        msg = "✍️ أرسل الآن **النص مع الرابط** الذي تريده أن يظهر كـ (تذييل) أسفل الدروس المنشورة:\n\n*(الوضع الافتراضي الحالي سيكون هو النص القديم إذا لم تقم بتعديله)*"
        return await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))

    if data == "admin_pub_menu" and await has_perm(user_id, "publish"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}})
        if "categories" not in GLOBAL_CACHE: 
            pipeline = [{"$sort": {"_id": 1}}, {"$group": {"_id": "$category", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
            cats = await db.library.aggregate(pipeline).to_list(length=None)
            GLOBAL_CACHE["categories"] = [c["_id"] for c in cats if c["_id"] and str(c["_id"]).lower() != 'nan']
        btns = [[InlineKeyboardButton(f"📁 | {c}", callback_data=f"pubc_{c[:50]}")] for c in GLOBAL_CACHE["categories"]]
        btns.append([InlineKeyboardButton("🔙 | رجوع", callback_data="admin_menu")])
        return await query.edit_message_text("📢 **نشر درس للقناة:**\nاختر السلسلة التي تود نشر درس منها:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if data.startswith("pubc_"):
        cat_name = data.replace("pubc_", "")
        pipeline = [{"$match": {"category": {"$regex": f"^{cat_name}"}}}, {"$sort": {"_id": 1}}, {"$group": {"_id": "$lesson", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        lessons = await db.library.aggregate(pipeline).to_list(length=None)
        btns = [[InlineKeyboardButton(f"📖 | {idx}- {les['_id']}", callback_data=f"publ_{str(les['doc_id'])}")] for idx, les in enumerate(lessons, 1)]
        btns.append([InlineKeyboardButton("🔙 | تراجع", callback_data="admin_pub_menu")])
        await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": {"pub_cat": cat_name}}})
        return await query.edit_message_text(f"📁 السلسلة: **{cat_name}**\nاختر الدرس المراد نشره:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if data.startswith("publ_"):
        val = data.replace("publ_", "")
        doc = await db.library.find_one({"_id": ObjectId(val)})
        lesson_name = doc["lesson"] if doc else "بدون عنوان"
        user = await db.users.find_one({"_id": user_id})
        temp_data = user.get("temp_data", {})
        temp_data["pub_les"] = lesson_name
        temp_data["pub_date"] = get_auto_arabic_date()
        
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": temp_data}})
        btns = [[InlineKeyboardButton("📝 | قالب نصي (إضغط هنا)", callback_data="pubfmt_text")], [InlineKeyboardButton("🔲 | قالب الأزرار الشفافة", callback_data="pubfmt_btns")], [InlineKeyboardButton("❌ | إلغاء", callback_data="admin_cancel")]]
        return await query.edit_message_text(f"✅ تم اختيار: **{lesson_name}**\n📅 تاريخ اليوم: **{temp_data['pub_date']}**\n\nاختر القالب الذي تفضله لتوليد المسودة:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if data.startswith("pubfmt_"):
        fmt_type = data.replace("pubfmt_", "")
        user = await db.users.find_one({"_id": user_id})
        temp_data = user.get("temp_data", {})
        cat = temp_data.get("pub_cat", "عام")
        les = temp_data.get("pub_les", "عام")
        date_txt = temp_data.get("pub_date", "")

        items = await db.library.find({"lesson": les}).to_list(length=None)
        links = {"قراءة": None, "صوت": None, "فيديو": None, "كلمات من نور": None, "فلاشة": None}
        for item in items:
            f_type = str(item.get("type", "نص"))
            safe_link = fix_link(item.get("file_id"))
            if safe_link:
                if "فيديو" in f_type: links["فيديو"] = safe_link
                elif "صوت" in f_type: links["صوت"] = safe_link
                elif "صور" in f_type or "فلاشة" in f_type: links["فلاشة"] = safe_link
                elif "كلمات" in f_type: links["كلمات من نور"] = safe_link
                else: links["قراءة"] = safe_link
        
        footer_content = await get_footer_text()
        header = f"{cat} - {les}\n\nدرس اليوم {date_txt}\n\n"
        footer = f"\n\n{footer_content}"
        
        if fmt_type == "text":
            body = ""
            for k, v in links.items():
                if v: body += f"{k}             [إضغط هنا]({v})\n"
            draft_text = header + body + footer
            temp_data["draft_format"] = "text"
            temp_data["draft_text"] = draft_text
            await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": temp_data}})
            btns = [[InlineKeyboardButton("✅ | انشر في القناة الآن", callback_data="pubconf_yes")], [InlineKeyboardButton("❌ | إلغاء", callback_data="admin_cancel")]]
            return await query.edit_message_text(f"📝 **معاينة المسودة:**\n\n{draft_text}\n\n--- \nهل تريد النشر؟", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns), disable_web_page_preview=True)
            
        elif fmt_type == "btns":
            draft_text = header + footer
            temp_data["draft_format"] = "btns"
            temp_data["draft_text"] = draft_text
            await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": temp_data}})
            inline_kb, row = [], []
            for k, v in links.items():
                if v:
                    row.append(InlineKeyboardButton(k, url=v))
                    if len(row) == 2: inline_kb.append(row); row = []
            if row: inline_kb.append(row)
            inline_kb.append([InlineKeyboardButton("✅ | انشر في القناة الآن", callback_data="pubconf_yes")])
            inline_kb.append([InlineKeyboardButton("❌ | إلغاء", callback_data="admin_cancel")])
            return await query.edit_message_text(f"🔲 **معاينة المسودة:**\n\n{draft_text}", reply_markup=InlineKeyboardMarkup(inline_kb), disable_web_page_preview=True)

    if data == "pubconf_yes":
        user = await db.users.find_one({"_id": user_id})
        temp_data = user.get("temp_data", {})
        draft_text = temp_data.get("draft_text", "")
        draft_format = temp_data.get("draft_format", "text")
        try:
            if draft_format == "text": await context.bot.send_message(chat_id=CHANNEL_ID, text=draft_text, parse_mode="Markdown", disable_web_page_preview=True)
            else:
                les = temp_data.get("pub_les", "")
                items = await db.library.find({"lesson": les}).to_list(length=None)
                links = {"قراءة": None, "صوت": None, "فيديو": None, "كلمات من نور": None, "فلاشة": None}
                for item in items:
                    f_type = str(item.get("type", "نص"))
                    safe_link = fix_link(item.get("file_id"))
                    if safe_link:
                        if "فيديو" in f_type: links["فيديو"] = safe_link
                        elif "صوت" in f_type: links["صوت"] = safe_link
                        elif "صور" in f_type or "فلاشة" in f_type: links["فلاشة"] = safe_link
                        elif "كلمات" in f_type: links["كلمات من نور"] = safe_link
                        else: links["قراءة"] = safe_link
                inline_kb, row = [], []
                for k, v in links.items():
                    if v:
                        row.append(InlineKeyboardButton(k, url=v))
                        if len(row) == 2: inline_kb.append(row); row = []
                if row: inline_kb.append(row)
                await context.bot.send_message(chat_id=CHANNEL_ID, text=draft_text, reply_markup=InlineKeyboardMarkup(inline_kb), disable_web_page_preview=True)
                
            await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": {}}})
            return await query.edit_message_text("🎉 **تم النشر في القناة بنجاح!**", parse_mode="Markdown")
        except Exception as e: return await query.edit_message_text(f"❌ حدث خطأ.\n`{e}`", parse_mode="Markdown")

    if data.startswith("utype_"):
        val = data.replace("utype_", "")
        user = await db.users.find_one({"_id": user_id})
        temp_data = user.get("temp_data", {})
        cat = temp_data.get("category", "عام")
        les = temp_data.get("lesson", "عام")
        f_link = temp_data.get("file_id")
        tg_file_id = temp_data.get("telegram_file_id")

        if val == "أسئلة":
            if not tg_file_id: return await query.edit_message_text("⚠️ يجب رفع إكسل!")
            await query.edit_message_text("⏳ جاري قراءة الملف...")
            try:
                file = await context.bot.get_file(tg_file_id)
                byte_array = await file.download_as_bytearray()
                xls = pd.ExcelFile(io.BytesIO(byte_array))
                df = pd.read_excel(xls, sheet_name=0) 
                count = 0
                for _, row in df.iterrows():
                    q_col = next((c for c in df.columns if 'سؤال' in str(c)), None)
                    ans_col = next((c for c in df.columns if 'صحيح' in str(c)), None)
                    if q_col and ans_col and pd.notna(row.get(q_col)) and pd.notna(row.get(ans_col)):
                        wrongs = [str(row[wc]).strip() for wc in [c for c in df.columns if 'خاطئة' in str(c) or 'خطأ' in str(c)] if pd.notna(row.get(wc))]
                        await db.questions.insert_one({"category": cat, "lesson": les, "question": str(row[q_col]).strip(), "correct": str(row[ans_col]).strip(), "wrong": wrongs, "correct_answers": 0, "wrong_answers": 0})
                        count += 1
                await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}})
                clear_cache()
                return await query.edit_message_text(f"🎉 تم إضافة {count} سؤال للدرس ({les}) بنجاح!")
            except: return await query.edit_message_text("❌ حدث خطأ في قراءة الملف.")
        else:
            await db.library.update_one({"category": cat, "lesson": les, "type": val}, {"$set": {"file_id": f_link, "created_at": time.time()}}, upsert=True)
            await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}})
            clear_cache()
            return await query.edit_message_text(f"🎉 **تم ربط الملف!**\n📁 السلسلة: {cat}\n📖 الدرس: {les}\n🏷️ النوع: {val}", parse_mode="Markdown")

    if data.startswith("ul_"):
        val = data.replace("ul_", "")
        if val == "new":
            await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_UPL_LES_TEXT"}})
            return await query.edit_message_text("✍️ أرسل اسم **المحاضرة الجديدة**:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
            
        doc = await db.library.find_one({"_id": ObjectId(val)})
        lesson_name = doc["lesson"] if doc else "بدون عنوان"
        user = await db.users.find_one({"_id": user_id})
        temp_data = user.get("temp_data", {})
        temp_data["lesson"] = lesson_name
        
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_TYPE", "temp_data": temp_data}})
        return await query.edit_message_text(f"📖 المحاضرة: **{lesson_name}**\n👇 ما هو **نوع** هذا المحتوى؟", parse_mode="Markdown", reply_markup=get_type_keyboard())

    if data.startswith("uc_"):
        val = data.replace("uc_", "")
        if val == "new":
            await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_UPL_CAT_TEXT"}})
            return await query.edit_message_text("✍️ أرسل اسم **السلسلة الجديدة**:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
            
        doc = await db.library.find_one({"_id": ObjectId(val)})
        cat_name = doc["category"] if doc else "عام"
        user = await db.users.find_one({"_id": user_id})
        temp_data = user.get("temp_data", {})
        temp_data["category"] = cat_name
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "UPLOADING", "temp_data": temp_data}})
        
        pipeline = [{"$match": {"category": cat_name}}, {"$sort": {"_id": 1}}, {"$group": {"_id": "$lesson", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        lessons = await db.library.aggregate(pipeline).to_list(length=None)
        
        btns = [[InlineKeyboardButton(f"📖 | {idx}- {les['_id']}", callback_data=f"ul_{str(les['doc_id'])}")] for idx, les in enumerate(lessons, 1)]
        btns.extend([[InlineKeyboardButton("➕ | إضافة محاضرة جديدة", callback_data="ul_new")], [InlineKeyboardButton("❌ | إلغاء العملية", callback_data="admin_cancel")]])
        return await query.edit_message_text(f"📁 السلسلة: **{cat_name}**\nاختر المحاضرة:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))

    if data == "admin_stats" and await has_perm(user_id, "stats"):
        await query.edit_message_text("⏳ جاري تحليل البيانات...")
        try:
            total_users = await db.users.count_documents({})
            active_users = await db.users.count_documents({"last_active": {"$gte": time.time() - 7*86400}})
            top_lessons = await db.lesson_stats.find().sort("views", -1).limit(50).to_list(length=None)
            pipeline = [{"$match": {"wrong_answers": {"$gt": 0}}}, {"$addFields": {"total_answers": {"$add": [{"$ifNull": ["$correct_answers", 0]}, "$wrong_answers"]}}}, {"$sort": {"wrong_answers": -1}}, {"$limit": 50}]
            top_wrong_qs = await db.questions.aggregate(pipeline).to_list(length=None)
            
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                pd.DataFrame([{"الطلاب المسجلين": total_users, "النشطين (آخر 7 أيام)": active_users, "التاريخ": time.strftime("%Y-%m-%d %H:%M:%S")}]).to_excel(writer, sheet_name='ملخص', index=False)
                if top_lessons: pd.DataFrame(top_lessons).rename(columns={"category": "السلسلة", "lesson": "الدرس", "views": "المشاهدات"})[["السلسلة", "الدرس", "المشاهدات"]].to_excel(writer, sheet_name='مشاهدات', index=False)
                if top_wrong_qs: pd.DataFrame([{"السلسلة": q.get("category", ""), "الدرس": q.get("lesson", ""), "السؤال": q.get("question", ""), "الخطأ": q.get("wrong_answers", 0)} for q in top_wrong_qs]).to_excel(writer, sheet_name='أخطاء', index=False)
            output.seek(0)
            await query.message.delete()
            return await context.bot.send_document(chat_id=chat_id, document=output, filename="تقرير.xlsx", caption="📊 **تقرير الإحصائيات**", parse_mode="Markdown")
        except: return await context.bot.send_message(chat_id=chat_id, text="❌ حدث خطأ.")

    if data == "admin_menu":
        adm = await get_admin_doc(user_id)
        if not adm: return
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}})
        btns = []
        if await has_perm(user_id, "publish"): 
            btns.append([InlineKeyboardButton("📢 | نشر درس للقناة", callback_data="admin_pub_menu")])
            btns.append([InlineKeyboardButton("🔗 | تعديل رابط/نص الاشتراك", callback_data="admin_edit_footer")])
            btns.append([InlineKeyboardButton("📊 | إنشاء استفتاء", callback_data="admin_poll")])
        if await has_perm(user_id, "questions"): btns.append([InlineKeyboardButton("➕ | إضافة سؤال لدرس", callback_data="admin_add_q")])
        if await has_perm(user_id, "stats"): btns.append([InlineKeyboardButton("📈 | الإحصائيات", callback_data="admin_stats")])
        if str(user_id) == OWNER_ID: btns.append([InlineKeyboardButton("👥 | إدارة المشرفين", callback_data="admin_manage")])
        btns.append([InlineKeyboardButton("❌ | إغلاق", callback_data="admin_cancel")])
        return await query.edit_message_text("⚙️ **لوحة التحكم:**", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if data == "admin_add_q" and await has_perm(user_id, "questions"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_Q_CAT"}})
        if "categories" not in GLOBAL_CACHE: 
            pipeline = [{"$sort": {"_id": 1}}, {"$group": {"_id": "$category", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
            cats = await db.library.aggregate(pipeline).to_list(length=None)
            GLOBAL_CACHE["categories"] = [c["_id"] for c in cats if c["_id"] and str(c["_id"]).lower() != 'nan']
        btns = [[InlineKeyboardButton(f"📁 | {c}", callback_data=f"qaddc_{c[:50]}")] for c in GLOBAL_CACHE["categories"]]
        btns.append([InlineKeyboardButton("🔙 | رجوع", callback_data="admin_menu")])
        return await query.edit_message_text("📝 **إضافة سؤال:**\nاختر السلسلة:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if data.startswith("qaddc_"):
        cat_name = data.replace("qaddc_", "")
        pipeline = [{"$match": {"category": {"$regex": f"^{cat_name}"}}}, {"$sort": {"_id": 1}}, {"$group": {"_id": "$lesson", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        lessons = await db.library.aggregate(pipeline).to_list(length=None)
        btns = [[InlineKeyboardButton(f"📖 | {idx}- {les['_id']}", callback_data=f"qaddl_{str(les['doc_id'])}")] for idx, les in enumerate(lessons, 1)]
        btns.append([InlineKeyboardButton("🔙 | تراجع", callback_data="admin_add_q")])
        await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": {"q_cat": cat_name}}})
        return await query.edit_message_text(f"📁 السلسلة: **{cat_name}**\nاختر الدرس:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if data.startswith("qaddl_"):
        val = data.replace("qaddl_", "")
        doc = await db.library.find_one({"_id": ObjectId(val)})
        lesson_name = doc["lesson"] if doc else "بدون عنوان"
        user = await db.users.find_one({"_id": user_id})
        temp_data = user.get("temp_data", {})
        temp_data["q_les"] = lesson_name
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_Q_TEXT", "temp_data": temp_data}})
        return await query.edit_message_text(f"📖 المحاضرة: **{lesson_name}**\n✍️ أرسل **نص السؤال**:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))

    if data == "admin_poll" and await has_perm(user_id, "publish"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_POLL_Q"}}, upsert=True)
        return await query.edit_message_text("📊 أرسل الآن **سؤال الاستفتاء**:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 تراجع", callback_data="admin_menu")]]))

    if data == "admin_manage" and user_id == OWNER_ID:
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}})
        admins = await db.admins.find({}).to_list(length=None)
        btns = [[InlineKeyboardButton(f"👤 | تعديل المشرف ({adm['_id']})", callback_data=f"editadm_{adm['_id']}")] for adm in admins]
        btns.extend([[InlineKeyboardButton("➕ | إضافة مشرف جديد", callback_data="add_admin")], [InlineKeyboardButton("🔙 | رجوع", callback_data="admin_menu")]])
        return await query.edit_message_text("👥 **إدارة المشرفين:**\nانقر للتعديل:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if data.startswith("editadm_") and user_id == OWNER_ID:
        target_id = data.replace("editadm_", "")
        adm_doc = await db.admins.find_one({"_id": target_id})
        if not adm_doc: return await query.answer("لم يتم العثور", show_alert=True)
        perms = adm_doc.get("permissions", {"upload": False, "questions": False, "publish": False, "stats": False})
        await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": {"edit_admin_id": target_id, "admin_perms": perms}}})
        return await query.edit_message_text(f"⚙️ **صلاحيات المشرف ({target_id}):**", reply_markup=get_perms_kb(perms, edit_mode=True, admin_id=target_id))

    if data == "add_admin" and user_id == OWNER_ID:
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_ADMIN_ID"}}, upsert=True)
        return await query.edit_message_text("✍️ أرسل **آيدي (ID)** المشرف الجديد:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 تراجع", callback_data="admin_manage")]]))

    if data.startswith("deladmin_") and user_id == OWNER_ID:
        adm_id = data.replace("deladmin_", "")
        await db.admins.delete_one({"_id": adm_id})
        return await query.edit_message_text(f"✅ تم سحب الصلاحيات نهائياً من ({adm_id}).", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_manage")]]))

    if data == "main_menu":
        if "categories" not in GLOBAL_CACHE: 
            pipeline = [{"$sort": {"_id": 1}}, {"$group": {"_id": "$category", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
            cats = await db.library.aggregate(pipeline).to_list(length=None)
            GLOBAL_CACHE["categories"] = [c["_id"] for c in cats if c["_id"] and str(c["_id"]).lower() != 'nan']
        btns = [[InlineKeyboardButton(f"📂 | {c}", callback_data=f"cat_{c[:50]}")] for c in GLOBAL_CACHE["categories"]]
        return await query.edit_message_text("📚 **المشروع القرآني:**\nيرجى اختيار السلسلة المطلوبة:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if data == "admin_cancel":
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "last_msg_id": None}}, upsert=True)
        await query.message.delete()
        return

    if data == "import_confirm":
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_EXCEL"}}, upsert=True)
        return await query.edit_message_text("📥 **أرسل ملف الإكسل (.xlsx) الآن...**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))

    if data.startswith("cat_"):
        cat_name = data.replace("cat_", "")
        cache_key = f"cat_les_{cat_name}"
        if cache_key not in GLOBAL_CACHE:
            pipeline = [{"$match": {"category": {"$regex": f"^{cat_name}"}}}, {"$sort": {"_id": 1}}, {"$group": {"_id": "$lesson", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
            GLOBAL_CACHE[cache_key] = await db.library.aggregate(pipeline).to_list(length=None)
        btns = [[InlineKeyboardButton(f"📖 | {idx}- {les['_id']}", callback_data=f"les_{str(les['doc_id'])}")] for idx, les in enumerate(GLOBAL_CACHE[cache_key], 1)]
        btns.append([InlineKeyboardButton("🔙 | العودة للرئيسية", callback_data="main_menu")])
        return await query.edit_message_text(f"📂 **السلسلة:**\nاختر المحاضرة المطلوب:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if data.startswith("les_"):
        return await show_lesson_ui(context, chat_id, data.replace("les_", ""), message_id=query.message.message_id, user_id=user_id)

    if data.startswith("quizles_"):
        try: await context.bot.answer_callback_query(query.id, "🚀 جاري التجهيز...", show_alert=False)
        except: pass
        doc_id = data.replace("quizles_", "")
        doc = await db.library.find_one({"_id": ObjectId(doc_id)})
        if not doc: return
        return await send_question(context, chat_id, lesson=doc.get("lesson"), user_id=user_id, msg_id=query.message.message_id, back_doc_id=doc_id)

    if data.startswith("ans_"):
        parts = data.split("_")
        is_correct = parts[1] == "1"
        q_id, ts = parts[2], int(parts[3])
        if int(time.time()) - ts > TIME_LIMIT or int(time.time()) - ts < 0: 
            return await query.edit_message_text("⏳ *انتهى الوقت المخصص للإجابة!*", parse_mode="Markdown")
        new_kb = []
        for row in query.message.reply_markup.inline_keyboard:
            new_row = []
            for b in row:
                if b.callback_data == data: new_row.append(InlineKeyboardButton(b.text + (" ✅" if is_correct else " ❌"), callback_data="ignore"))
                else: new_row.append(InlineKeyboardButton(b.text, callback_data=b.callback_data))
            new_kb.append(new_row)
        await query.edit_message_reply_markup(InlineKeyboardMarkup(new_kb))
        asyncio.create_task(background_db_update(user_id, q_id=q_id, is_correct=is_correct))
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
        else: 
            if user_id: await clean_chat_history(user_id, chat_id, context)
            sent_msg = await context.bot.send_message(chat_id, txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))
            if user_id: await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
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
    else: 
        if user_id: await clean_chat_history(user_id, chat_id, context)
        sent_msg = await context.bot.send_message(chat_id, txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_kb))
        if user_id: await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})

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
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if tasks: asyncio.gather(*tasks) 
    except Exception as e: logging.error(f"Webhook error: {e}")
    return {"status": "ok"}
