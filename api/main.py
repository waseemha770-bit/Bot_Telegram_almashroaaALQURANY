import os
import io
import time
import random
import logging
import asyncio
import certifi
import datetime
import html
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

async def clean_chat_history(user_id, chat_id, context):
    if db is None: return
    try:
        user = await db.users.find_one({"_id": str(user_id)})
        if user and user.get("last_msg_id"):
            await context.bot.delete_message(chat_id=chat_id, message_id=user["last_msg_id"])
    except Exception: pass 

# ==========================================
# دوال مساعدة (تاريخ، إعدادات، تحليل الروابط)
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

def parse_tg_link(link):
    parts = link.rstrip('/').split('/')
    msg_id = int(parts[-1])
    if len(parts) >= 3 and parts[-3] == 'c':
        chat_id = f"-100{parts[-2]}"
    else:
        chat_id_str = parts[-2]
        if chat_id_str.startswith("-100"): chat_id = chat_id_str
        elif chat_id_str.isdigit(): chat_id = f"-100{chat_id_str}"
        else: chat_id = f"@{chat_id_str}"
    return chat_id, msg_id

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
        [InlineKeyboardButton("🎬 ريلز (فيديو)", callback_data="utype_فيديو"), InlineKeyboardButton("📚 كامل الملزمة (نص)", callback_data="utype_نص")],
        [InlineKeyboardButton("🎧 اليوم الثقافي (صوت)", callback_data="utype_صوت"), InlineKeyboardButton("🖼️ ملخص الملزمة (صور)", callback_data="utype_صور")],
        [InlineKeyboardButton("📝 ملف إكسل (لإضافة الأسئلة)", callback_data="utype_أسئلة")],
        [InlineKeyboardButton("❌ إلغاء العملية", callback_data="admin_cancel")]
    ])

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
        [make_btn("🎬 ريلز", links["فيديو"]), make_btn("📚 كامل الملزمة", links["نص"])],
        [make_btn("🎧 اليوم الثقافي", links["صوت"]), make_btn("🖼️ ملخص الملزمة", links["صور"])]
    ]
    btns.append([InlineKeyboardButton("✨ 📝 قيم نفسك ✨", callback_data=f"quizles_{doc_id}")])
    bot_username = context.bot.username
    share_url = f"https://t.me/share/url?text=📚 إليك هذا الدرس القيم: {lesson_title}\n&url=https://t.me/{bot_username}?start=les_{doc_id}"
    btns.append([InlineKeyboardButton("🔗 شارك هذا الدرس (لتعم الفائدة)", url=share_url)])
    btns.append([InlineKeyboardButton("🔙 السابق", callback_data=f"cat_{series[:25]}"), InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu")])

    txt = f"📖 **{lesson_title}**\n📂 السلسلة: {series}\n\n👇 اختر المحتوى للانتقال إليه:"
    try:
        if message_id: await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=message_id, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))
        else: 
            if user_id: await clean_chat_history(user_id, chat_id, context)
            sent_msg = await context.bot.send_message(chat_id, txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))
            if user_id: await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
    except: pass

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
            btns.append([InlineKeyboardButton("📢 | قسم النشر والقوالب", callback_data="admin_publishing_hub")])
            btns.append([InlineKeyboardButton("🎛️ | إدارة أنواع المحتوى", callback_data="admin_content_types")])
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

    if state == "WAIT_TYPE_DATA" and await has_perm(user_id, "publish"):
        parts = text.split(',')
        name = parts[0].strip()
        icon = parts[1].strip() if len(parts) > 1 else "📁"
        t_id = f"type_{int(time.time())}"
        await db.content_types.insert_one({"_id": t_id, "name": name, "icon": icon})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}})
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة", callback_data="admin_content_types")]]
        sent_msg = await update.message.reply_text(f"✅ تم إضافة النوع ({icon} {name}) بنجاح!", reply_markup=InlineKeyboardMarkup(btns))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

    if state == "WAIT_EDIT_TYPE" and await has_perm(user_id, "publish"):
        parts = text.split(',')
        name = parts[0].strip()
        icon = parts[1].strip() if len(parts) > 1 else "📁"
        t_id = temp_data.get("edit_t_id")
        await db.content_types.update_one({"_id": t_id}, {"$set": {"name": name, "icon": icon}})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}})
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة", callback_data="admin_content_types")]]
        sent_msg = await update.message.reply_text(f"✅ تم تحديث النوع إلى ({icon} {name}) بنجاح!", reply_markup=InlineKeyboardMarkup(btns))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

    if state == "WAIT_CHAN_ID" and await has_perm(user_id, "publish"):
        ch = text.strip()
        if not ch.startswith('@') and not ch.startswith('-100'):
            sent_msg = await update.message.reply_text("⚠️ معرّف القناة يجب أن يبدأ بـ @ أو -100")
            return
        await db.settings.update_one({"_id": "channels"}, {"$addToSet": {"list": ch}}, upsert=True)
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}})
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة لإدارة القنوات", callback_data="admin_channels")]]
        sent_msg = await update.message.reply_text(f"✅ تم إضافة القناة ({ch}) بنجاح!", reply_markup=InlineKeyboardMarkup(btns))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

    if state == "WAIT_TPL_NAME":
        temp_data["tpl_name"] = text.strip()
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_TPL_CONTENT", "temp_data": temp_data}})
        await clean_chat_history(user_id, chat_id, context)
        msg = f"""✅ تم اختيار اسم القالب: **{text}**

✍️ أرسل الآن محتوى القالب وتصميمه.
استخدم هذه المتغيرات (بالأقواس المعكوفة) لكي يقوم البوت باستبدالها تلقائياً بالروابط الحقيقية:
`{{سلسلة}}` ، `{{درس}}` ، `{{تاريخ}}` ، `{{ملزمة}}` ، `{{ريلز}}` ، `{{صوت}}` ، `{{ملخص}}` ، `{{تذييل}}`

مثال لتصميم مقرر أسبوعي:
<blockquote>مقرر الأسبوع ❞</blockquote>
<b>ملزمة- {{درس}}</b>
<blockquote>يوم السبت <a href='{{ملخص}}'>إضغط هنا</a> ❞</blockquote>
{{تذييل}}"""
        sent_msg = await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

    if state == "WAIT_TPL_CONTENT":
        tpl_name = temp_data.get("tpl_name", "قالب جديد")
        await db.templates.insert_one({"name": tpl_name, "content": text})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}})
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة للقوالب", callback_data="admin_tpl_menu")]]
        sent_msg = await update.message.reply_text(f"🎉 **تم حفظ قالب ({tpl_name}) بنجاح!**\nسيكون متاحاً الآن بشكل دائم في خيارات النشر.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

    if state == "WAIT_FOOTER_TEXT":
        await db.settings.update_one({"_id": "bot_settings"}, {"$set": {"footer_text": text}}, upsert=True)
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}})
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text("✅ **تم حفظ نص/رابط التذييل بنجاح!**", parse_mode="Markdown", reply_markup=kb)
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
                sent_msg = await update.message.reply_text("🎉 **تم نشر الاستفتاء في القناة الافتراضية!**", reply_markup=kb)
                await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
                return
            except Exception as e: 
                sent_msg = await update.message.reply_text(f"❌ حدث خطأ:\n`{e}`", parse_mode="Markdown")
                await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
                return
        sent_msg = await update.message.reply_text("⚠️ لم يتم إعداد معرف القناة.")
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

    await clean_chat_history(user_id, chat_id, context)
    sent_msg = await update.message.reply_text("الرجاء استخدام الأزرار أدناه 👇", reply_markup=kb)
    await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})

# ==========================================
# معالجة تفاعلات الأزرار والنشر
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

    if data == "admin_content_types" and await has_perm(user_id, "publish"):
        types = await db.content_types.find({}).to_list(length=None)
        btns = []
        for t in types:
            btns.append([
                InlineKeyboardButton(f"{t['icon']} {t['name']}", callback_data="ignore"),
                InlineKeyboardButton("✏️ تعديل", callback_data=f"editype_{t['_id']}"),
                InlineKeyboardButton("🗑️ حذف", callback_data=f"deltype_{t['_id']}")
            ])
        btns.append([InlineKeyboardButton("➕ | إضافة نوع جديد", callback_data="add_type")])
        btns.append([InlineKeyboardButton("🔙 | رجوع للوحة", callback_data="admin_menu")])
        return await query.edit_message_text("🎛️ **إدارة أنواع المحتوى (الأزرار):**\nقم بإضافة أو تعديل الأزرار التي ستظهر للطلاب:", reply_markup=InlineKeyboardMarkup(btns))
    
    if data == "add_type":
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_TYPE_DATA"}})
        return await query.edit_message_text("✍️ أرسل **الاسم, الأيقونة** للنوع الجديد مفصولة بفاصلة\n(مثال: `بودكاست, 🎙️`):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))

    if data.startswith("deltype_"):
        t_id = data.replace("deltype_", "")
        await db.content_types.delete_one({"_id": t_id})
        return await query.edit_message_text("✅ تم الحذف بنجاح!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_content_types")]]))

    if data.startswith("editype_"):
        t_id = data.replace("editype_", "")
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_EDIT_TYPE", "temp_data": {"edit_t_id": t_id}}})
        return await query.edit_message_text("✍️ أرسل **الاسم الجديد, الأيقونة الجديدة** مفصولة بفاصلة\n(مثال: `الكتاب الشامل, 📖`):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))

    if data == "admin_publishing_hub" and await has_perm(user_id, "publish"):
        btns = [
            [InlineKeyboardButton("🚀 | نشر درس للقناة", callback_data="admin_pub_menu")],
            [InlineKeyboardButton("📡 | إدارة قنوات النشر", callback_data="admin_channels")],
            [InlineKeyboardButton("🎨 | إدارة قوالب النشر", callback_data="admin_tpl_menu")],
            [InlineKeyboardButton("🔗 | تعديل تذييل النشر", callback_data="admin_edit_footer")],
            [InlineKeyboardButton("📊 | إنشاء استفتاء للقناة", callback_data="admin_poll")],
            [InlineKeyboardButton("🔙 | رجوع للوحة الإدارة", callback_data="admin_menu")]
        ]
        return await query.edit_message_text("📢 **قسم النشر والقوالب:**\nجميع أدوات النشر وتخصيص القوالب في مكان واحد:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if data == "admin_channels" and await has_perm(user_id, "publish"):
        channels_doc = await db.settings.find_one({"_id": "channels"})
        channels = channels_doc.get("list", []) if channels_doc else []
        btns = []
        for ch in channels:
            btns.append([InlineKeyboardButton(f"🗑️ حذف ({ch})", callback_data=f"delchan_{ch}")])
        btns.append([InlineKeyboardButton("➕ | إضافة قناة جديدة", callback_data="add_chan")])
        btns.append([InlineKeyboardButton("🔙 | رجوع لقسم النشر", callback_data="admin_publishing_hub")])
        return await query.edit_message_text("📡 **إدارة قنوات النشر المتعددة:**\nأضف القنوات هنا لتتمكن من اختيارها عند النشر:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if data == "add_chan" and await has_perm(user_id, "publish"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_CHAN_ID"}})
        return await query.edit_message_text("✍️ أرسل معرّف القناة (مثال: `@almashro` أو `-100123456`):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))

    if data.startswith("delchan_") and await has_perm(user_id, "publish"):
        ch = data.replace("delchan_", "")
        await db.settings.update_one({"_id": "channels"}, {"$pull": {"list": ch}})
        return await query.edit_message_text(f"✅ تم حذف القناة ({ch}) بنجاح!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_channels")]]))

    if data == "admin_tpl_menu" and await has_perm(user_id, "publish"):
        templates = await db.templates.find({}).to_list(length=None)
        btns = []
        for t in templates:
            btns.append([InlineKeyboardButton(f"📄 | {t['name']}", callback_data="ignore")])
            btns.append([InlineKeyboardButton("🗑️ حذف هذا القالب", callback_data=f"deltpl_{str(t['_id'])}")])
        btns.append([InlineKeyboardButton("➕ | إنشاء قالب جديد", callback_data="add_tpl")])
        btns.append([InlineKeyboardButton("🔙 | رجوع لقسم النشر", callback_data="admin_publishing_hub")])
        return await query.edit_message_text("🎨 **إدارة قوالب النشر الديناميكية:**\nأنشئ قوالبك بمتغيرات ذكية:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if data == "add_tpl" and await has_perm(user_id, "publish"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_TPL_NAME"}})
        return await query.edit_message_text("✍️ أرسل الآن **اسم القالب الجديد**\n(مثال: قالب خطب الجمعة، قالب السيرة):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))

    if data.startswith("deltpl_") and await has_perm(user_id, "publish"):
        tpl_id = data.replace("deltpl_", "")
        await db.templates.delete_one({"_id": ObjectId(tpl_id)})
        return await query.edit_message_text("✅ تم حذف القالب بنجاح!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع للقوالب", callback_data="admin_tpl_menu")]]))

    if data == "admin_edit_footer" and await has_perm(user_id, "publish"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_FOOTER_TEXT"}})
        msg = "✍️ أرسل الآن **النص مع الرابط** الذي تريده أن يظهر كـ (تذييل) أسفل الدروس المنشورة:\n\n*(الوضع الافتراضي الحالي سيكون هو النص القديم إذا لم تقم بتعديله)*"
        return await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))

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

    if data == "admin_pub_menu" and await has_perm(user_id, "publish"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}})
        if "categories" not in GLOBAL_CACHE: 
            pipeline = [{"$sort": {"_id": 1}}, {"$group": {"_id": "$category", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
            cats = await db.library.aggregate(pipeline).to_list(length=None)
            GLOBAL_CACHE["categories"] = [c["_id"] for c in cats if c["_id"] and str(c["_id"]).lower() != 'nan']
        btns = [[InlineKeyboardButton(f"📁 | {c}", callback_data=f"pubc_{c[:50]}")] for c in GLOBAL_CACHE["categories"]]
        btns.append([InlineKeyboardButton("🔙 | رجوع لقسم النشر", callback_data="admin_publishing_hub")])
        return await query.edit_message_text("📢 **نشر درس:**\nاختر السلسلة التي تود نشر درس منها:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

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
        هذا الخطأ (`Message to copy not found`) يظهر عادةً عند استخدام دوال مثل `copyMessage` أو `forwardMessage` في واجهة برمجة تطبيقات تيليجرام (Telegram API)، ويعني أن البوت غير قادر على الوصول إلى الرسالة الأصلية أو العثور عليها.

لحل هذه المشكلة، يجب مراجعة عدة نقاط أساسية في إعدادات القناة والبارامترات المرسلة في الكود:

### 1. التحقق من صلاحيات البوت (Bot Permissions)
حتى يتمكن البوت من نسخ رسالة من قناة أو إرسالها إليها، يجب أن يكون **مشرفاً (Administrator)** في كلتا القناتين (المصدر والوجهة).
*   **في قناة المصدر (Source Channel):** يجب أن يمتلك صلاحية قراءة الرسائل.
*   **في قناة الوجهة (Destination Channel):** يجب أن يمتلك صلاحية **نشر الرسائل (Post Messages)**.

### 2. التحقق من صيغة معرف القناة (`from_chat_id` / `chat_id`)
تأكد من أن المعرفات التي يستخدمها النظام (سواء كان سكربت بايثون أو Google Apps Script) مكتوبة بالصيغة الصحيحة:
*   **للقنوات العامة:** يجب أن يبدأ المعرف بـ `@` (مثال: `@MyPublicChannel`).
*   **للقنوات الخاصة:** يجب أن يكون المعرف عبارة عن أرقام ويبدأ بـ `-100` (مثال: `-1001234567890`). إذا كنت تستخدم رقم تعريف (ID) عادي بدون `-100` لقناة خاصة، فلن يتعرف البوت عليها.

### 3. التحقق من رقم الرسالة (`message_id`)
الخطأ `Message to copy not found` يشير تحديداً إلى أن المتغير الخاص بـ `message_id` غير صحيح.
*   تأكد من أن الرسالة التي تحاول نسخها **لم يتم حذفها** من القناة المصدر.
*   تأكد من أن الرقم المُمرر في الكود هو رقم صحيح (Integer) وليس نصاً (String) فارغاً أو قيمة خاطئة قادمة من قاعدة البيانات أو الجداول المرتبطة بالسكربت.

### 4. مراجعة البارامترات في الطلب (API Request)
إذا كنت تستخدم نظام نشر تلقائي، تأكد من أن هيكل البيانات المرسل (Payload) يحتوي على المتغيرات الثلاثة الأساسية بشكل صحيح، هكذا:

```json
{
  "chat_id": "معرف قناة الوجهة",
  "from_chat_id": "معرف القناة المصدر التي تحتوي على الرسالة",
  "message_id": 1234
}
