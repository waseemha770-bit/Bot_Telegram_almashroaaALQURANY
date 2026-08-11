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
            
            if await db.content_types.count_documents({}) == 0:
                await db.content_types.insert_many([
                    {"_id": "type_video", "name": "ريلز", "icon": "🎬"},
                    {"_id": "type_text", "name": "كامل الملزمة", "icon": "📚"},
                    {"_id": "type_audio", "name": "اليوم الثقافي", "icon": "🎧"},
                    {"_id": "type_image", "name": "ملخص الملزمة", "icon": "🖼️"}
                ])
                await db.library.update_many({"type": "فيديو"}, {"$set": {"type": "type_video"}})
                await db.library.update_many({"type": "نص"}, {"$set": {"type": "type_text"}})
                await db.library.update_many({"type": "صوت"}, {"$set": {"type": "type_audio"}})
                await db.library.update_many({"type": {"$in": ["صور", "فلاشة"]}}, {"$set": {"type": "type_image"}})
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
# دوال مساعدة وتحليل الروابط
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

def fix_link(raw_link):
    if not raw_link or str(raw_link).strip().lower() in ['', 'nan', 'none', 'null', 'لا يوجد']:
        return None
    raw_str = str(raw_link).strip().replace(" ", "")
    if raw_str.startswith("http"): return raw_str
    if raw_str.startswith("t.me/"): return f"https://{raw_str}"
    if raw_str.isdigit():
        ch_name = CHANNEL_ID.replace('@', '').replace('https://t.me/', '')
        return f"https://t.me/{ch_name}/{raw_str}"
    if "/" in raw_str: return f"https://t.me/{raw_str}"
    return raw_str

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

async def get_type_keyboard():
    types = await db.content_types.find({}).to_list(length=None)
    kb = []
    row = []
    for t in types:
        row.append(InlineKeyboardButton(f"{t['icon']} {t['name']}", callback_data=f"utype_{t['_id']}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row: kb.append(row)
    kb.append([InlineKeyboardButton("❌ إلغاء العملية", callback_data="admin_cancel")])
    return InlineKeyboardMarkup(kb)

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
    
    types_docs = await db.content_types.find({}).to_list(length=None)
    types_dict = {t["_id"]: t for t in types_docs}
    
    links = {}
    for item in items:
        f_type = str(item.get("type", ""))
        safe_link = fix_link(item.get("file_id"))
        if safe_link: links[f_type] = safe_link

    def make_btn(text, link): return InlineKeyboardButton(text, url=link) if link else InlineKeyboardButton(text, callback_data="media_unavail")

    btns = []
    row = []
    for t_id, t_info in types_dict.items():
        if t_id in links: row.append(make_btn(f"{t_info['icon']} {t_info['name']}", links[t_id]))
        else: row.append(make_btn(f"{t_info['icon']} {t_info['name']}", None))
        if len(row) == 2:
            btns.append(row)
            row = []
    if row: btns.append(row)

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
    if not await has_perm(user_id, "upload") and not await has_perm(user_id, "questions"): return

    msg = update.message
    user = await db.users.find_one({"_id": user_id})
    state = user.get("state", "") if user else ""
    temp_data = user.get("temp_data", {}) if user else {}
    
    if state == "WAIT_Q_EXCEL" and msg.document:
        if not msg.document.file_name.endswith(('.xlsx', '.xls')):
            return await msg.reply_text("⚠️ يرجى رفع ملف الاختبار بصيغة Excel (.xlsx) فقط.")
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await msg.reply_text("⏳ جاري قراءة وتحليل ملف الاختبار (مزامنة ذكية)...")
        try:
            file = await context.bot.get_file(msg.document.file_id)
            byte_array = await file.download_as_bytearray()
            xls = pd.ExcelFile(io.BytesIO(byte_array))
            df = pd.read_excel(xls, sheet_name=0)

            cat = temp_data.get("q_cat", "عام")
            les = temp_data.get("q_les", "عام")
            count_add, count_del = 0, 0

            for _, row in df.iterrows():
                cols = [str(c).strip() for c in df.columns]
                q_col = next((c for c in cols if 'سؤال' in c), None)
                ans_col = next((c for c in cols if 'صحيح' in c), None)

                if q_col and ans_col and pd.notna(row.get(q_col)) and pd.notna(row.get(ans_col)):
                    q_val = str(row[q_col]).strip()
                    ans_val = str(row[ans_col]).strip()
                    
                    if ans_val == "حذف":
                        await db.questions.delete_many({"category": cat, "lesson": les, "question": q_val})
                        count_del += 1
                    else:
                        wrongs = [str(row[wc]).strip() for wc in df.columns if ('خاطئة' in str(wc) or 'خطأ' in str(wc)) and pd.notna(row.get(wc))]
                        await db.questions.update_one(
                            {"category": cat, "lesson": les, "question": q_val},
                            {"$set": {"correct": ans_val, "wrong": wrongs}},
                            upsert=True
                        )
                        count_add += 1

            await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}})
            clear_cache()
            await context.bot.delete_message(chat_id=chat_id, message_id=sent_msg.message_id)
            
            res_txt = f"🎉 **تمت مزامنة الاختبار بذكاء لدرس ({les})!**\n"
            if count_add > 0: res_txt += f"✅ تم إضافة/تحديث {count_add} سؤال.\n"
            if count_del > 0: res_txt += f"🗑️ تم حذف {count_del} سؤال.\n"
            
            final_msg = await msg.reply_text(res_txt, parse_mode="Markdown")
            await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": final_msg.message_id}})
            return
        except Exception as e:
            await context.bot.delete_message(chat_id=chat_id, message_id=sent_msg.message_id)
            return await msg.reply_text(f"❌ حدث خطأ أثناء قراءة ملف الإكسل:\n`{e}`", parse_mode="Markdown")

    if state == "WAIT_EXCEL" and msg.document:
        if not msg.document.file_name.endswith(('.xlsx', '.xls')): return await msg.reply_text("⚠️ يرجى رفع ملف بصيغة Excel (.xlsx) فقط.")
        await clean_chat_history(user_id, chat_id, context)
        await msg.reply_text("⏳ جاري تحليل قاعدة البيانات وتطبيق نظام المزامنة الذكية...")
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
                types_docs = await db.content_types.find({}).to_list(length=None)
                name_to_id = {t["name"].lower(): t["_id"] for t in types_docs}

                count_add, count_del = 0, 0
                for _, row in df_lib.iterrows():
                    cat_col = next((c for c in df_lib.columns if 'السلسلة' in str(c)), None)
                    les_col = next((c for c in df_lib.columns if 'الدرس' in str(c) or 'المحاضرة' in str(c)), None)
                    type_col = next((c for c in df_lib.columns if 'النوع' in str(c)), None)
                    link_col = next((c for c in df_lib.columns if 'الرابط' in str(c)), None)

                    if les_col and cat_col and pd.notna(row.get(les_col)) and pd.notna(row.get(cat_col)):
                        cat_val = str(row[cat_col]).strip()
                        les_val = str(row[les_col]).strip()
                        
                        excel_type_val = str(row.get(type_col, '')).strip()
                        t_val = "type_text"
                        for t_name, t_id in name_to_id.items():
                            if t_name in excel_type_val.lower() or excel_type_val.lower() in t_name:
                                t_val = t_id
                                break
                        if "فيديو" in excel_type_val: t_val = "type_video"
                        elif "صوت" in excel_type_val: t_val = "type_audio"
                        elif "صور" in excel_type_val or "فلاشة" in excel_type_val: t_val = "type_image"

                        l_val = str(row.get(link_col, '')).strip() if link_col and pd.notna(row.get(link_col)) else ""
                        
                        if l_val == "حذف":
                            await db.library.delete_many({"category": cat_val, "lesson": les_val, "type": t_val})
                            count_del += 1
                        elif l_val and l_val.lower() not in ['nan', 'none', 'null']:
                            link_str = l_val
                            await db.library.update_one(
                                {"category": cat_val, "lesson": les_val, "type": t_val},
                                {"$set": {"file_id": link_str, "updated_at": time.time()}},
                                upsert=True
                            )
                            count_add += 1
                
                updates_log += f"✅ تم إضافة/تحديث {count_add} محتوى دراسي.\n"
                if count_del > 0: updates_log += f"🗑️ تم حذف {count_del} محتوى دراسي.\n"
                
            if df_q is not None:
                count_q_add, count_q_del = 0, 0
                for _, row in df_q.iterrows():
                    q_col = next((c for c in df_q.columns if 'السؤال' in str(c)), None)
                    ans_col = next((c for c in df_q.columns if 'الصحيح' in str(c)), None)
                    cat_col = next((c for c in df_q.columns if 'السلسلة' in str(c)), None)
                    les_col = next((c for c in df_q.columns if 'الدرس' in str(c) or 'المحاضرة' in str(c)), None)
                    
                    if q_col and ans_col and pd.notna(row.get(q_col)) and pd.notna(row.get(ans_col)):
                        q_val = str(row[q_col]).strip()
                        ans_val = str(row[ans_col]).strip()
                        cat_val = str(row.get(cat_col, 'عام')).strip() if cat_col and pd.notna(row.get(cat_col)) else 'عام'
                        les_val = str(row.get(les_col, 'عام')).strip() if les_col and pd.notna(row.get(les_col)) else 'عام'
                        
                        if ans_val == "حذف":
                            await db.questions.delete_many({"category": cat_val, "lesson": les_val, "question": q_val})
                            count_q_del += 1
                        else:
                            wrongs = [str(row[wc]).strip() for wc in df_q.columns if ('خاطئة' in str(wc) or 'خطأ' in str(wc)) and pd.notna(row.get(wc))]
                            await db.questions.update_one(
                                {"category": cat_val, "lesson": les_val, "question": q_val},
                                {"$set": {"correct": ans_val, "wrong": wrongs}},
                                upsert=True
                            )
                            count_q_add += 1
                updates_log += f"✅ تم إضافة/تحديث {count_q_add} سؤال عام.\n"
                if count_q_del > 0: updates_log += f"🗑️ تم حذف {count_q_del} سؤال عام.\n"

            if not updates_log: return await msg.reply_text("⚠️ لم يتم العثور على أي تحديثات. تأكد من تطابق الأعمدة.")
            await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}}, upsert=True)
            clear_cache() 
            sent_msg = await msg.reply_text(f"🎉 **اكتملت المزامنة الذكية بنجاح!**\n\n{updates_log}", parse_mode="Markdown")
            await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
            return
        except Exception as e: return await msg.reply_text(f"❌ حدث خطأ أثناء المعالجة: `{e}`", parse_mode="Markdown")

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
        if await has_perm(user_id, "upload"):
            btns.append([InlineKeyboardButton("📂 | إدارة السلاسل والدروس", callback_data="admin_content_mgr")])
        if await has_perm(user_id, "publish"):
            btns.append([InlineKeyboardButton("📢 | قسم النشر والقوالب", callback_data="admin_publishing_hub")])
            btns.append([InlineKeyboardButton("🎛️ | إدارة أنواع المحتوى", callback_data="admin_content_types")])
        if await has_perm(user_id, "questions"):
            btns.append([InlineKeyboardButton("➕ | إضافة اختبار/سؤال لدرس", callback_data="admin_add_q")])
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
        sent_msg = await update.message.reply_text("📥 **الاستيراد الذكي:**\nلن يتم مسح بياناتك القديمة! سيقوم البوت بتحديث الدروس الموجودة وإضافة الجديدة.\n\nهل أنت متأكد من الاستمرار؟", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")
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

    user = await db.users.find_one({"_id": user_id})
    state = user.get("state", "") if user else ""
    temp_data = user.get("temp_data", {}) if user else {}

    if state == "WAIT_MGR_NEW_CAT":
        new_cat = text.strip()
        await db.library.insert_one({"category": new_cat, "lesson": "درس افتراضي", "type": "type_text", "file_id": None})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}})
        clear_cache()
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة", callback_data="admin_content_mgr")]]
        sent_msg = await update.message.reply_text(f"✅ تم إضافة السلسلة ({new_cat}) بنجاح!", reply_markup=InlineKeyboardMarkup(btns))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

    if state == "WAIT_MGR_EDIT_CAT":
        old_cat = temp_data.get("mgr_target_cat")
        new_cat = text.strip()
        await db.library.update_many({"category": old_cat}, {"$set": {"category": new_cat}})
        await db.questions.update_many({"category": old_cat}, {"$set": {"category": new_cat}})
        await db.lesson_stats.update_many({"category": old_cat}, {"$set": {"category": new_cat}})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}})
        clear_cache()
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة", callback_data="admin_content_mgr")]]
        sent_msg = await update.message.reply_text(f"✅ تم تعديل اسم السلسلة إلى ({new_cat}) بنجاح!", reply_markup=InlineKeyboardMarkup(btns))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

    if state == "WAIT_MGR_NEW_LES":
        target_cat = temp_data.get("mgr_target_cat")
        new_les = text.strip()
        await db.library.insert_one({"category": target_cat, "lesson": new_les, "type": "type_text", "file_id": None})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}})
        clear_cache()
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة", callback_data=f"mgr_cat_view_{target_cat}")]]
        sent_msg = await update.message.reply_text(f"✅ تم إضافة الدرس ({new_les}) بنجاح!", reply_markup=InlineKeyboardMarkup(btns))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

    if state == "WAIT_MGR_EDIT_LES":
        target_cat = temp_data.get("mgr_target_cat")
        old_les = temp_data.get("mgr_target_les")
        new_les = text.strip()
        await db.library.update_many({"category": target_cat, "lesson": old_les}, {"$set": {"lesson": new_les}})
        await db.questions.update_many({"category": target_cat, "lesson": old_les}, {"$set": {"lesson": new_les}})
        await db.lesson_stats.update_many({"category": target_cat, "lesson": old_les}, {"$set": {"lesson": new_les}})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}})
        clear_cache()
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة", callback_data=f"mgr_cat_view_{target_cat}")]]
        sent_msg = await update.message.reply_text(f"✅ تم تعديل اسم الدرس إلى ({new_les}) بنجاح!", reply_markup=InlineKeyboardMarkup(btns))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

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
استخدم المتغيرات بالأقواس المعكوفة:
`{{سلسلة}}` ، `{{درس}}` ، `{{تاريخ}}` ، `{{تذييل}}`
**ملاحظة:** استخدم أي اسم أنشأته في أنواع المحتوى لجلب رابطه (مثلاً `{{ريلز}}` أو `{{اليوم الثقافي}}`)."""
        sent_msg = await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

    if state == "WAIT_TPL_CONTENT":
        tpl_name = temp_data.get("tpl_name", "قالب جديد")
        await db.templates.insert_one({"name": tpl_name, "content": text})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}})
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة للقوالب", callback_data="admin_tpl_menu")]]
        sent_msg = await update.message.reply_text(f"🎉 **تم حفظ قالب ({tpl_name}) بنجاح!**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))
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
        sent_msg = await update.message.reply_text(f"⚙️ **تحديد صلاحيات المشرف ({new_admin}):**\nانقر للتفعيل ✅ أو التعطيل ❌ ثم حفظ:", reply_markup=get_perms_kb(perms, edit_mode=False))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
        return

    if state == "WAIT_UPL_LES_TEXT":
        temp_data["lesson"] = text
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_TYPE", "temp_data": temp_data}})
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text(f"📖 المحاضرة: **{text}**\n\n👇 ما هو **نوع** هذا المحتوى؟", parse_mode="Markdown", reply_markup=await get_type_keyboard())
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

    if data == "admin_cancel":
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "last_msg_id": None}}, upsert=True)
        await query.message.delete()
        return

    if data == "admin_menu":
        adm = await get_admin_doc(user_id)
        if not adm: return
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}})
        btns = []
        if await has_perm(user_id, "upload"):
            btns.append([InlineKeyboardButton("📂 | إدارة السلاسل والدروس", callback_data="admin_content_mgr")])
        if await has_perm(user_id, "publish"):
            btns.append([InlineKeyboardButton("📢 | قسم النشر والقوالب", callback_data="admin_publishing_hub")])
            btns.append([InlineKeyboardButton("🎛️ | إدارة أنواع المحتوى", callback_data="admin_content_types")])
        if await has_perm(user_id, "questions"):
            btns.append([InlineKeyboardButton("➕ | إضافة اختبار/سؤال لدرس", callback_data="admin_add_q")])
        if await has_perm(user_id, "stats"):
            btns.append([InlineKeyboardButton("📈 | الإحصائيات الشاملة", callback_data="admin_stats")])
        if str(user_id) == OWNER_ID: 
            btns.append([InlineKeyboardButton("👥 | إدارة المشرفين", callback_data="admin_manage")])
        btns.append([InlineKeyboardButton("❌ | إغلاق اللوحة", callback_data="admin_cancel")])
        return await query.edit_message_text("⚙️ **لوحة التحكم والإدارة:**\nاختر الإجراء المطلوب:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if data == "admin_content_mgr" and await has_perm(user_id, "upload"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}})
        if "categories" not in GLOBAL_CACHE: 
            pipeline = [{"$sort": {"_id": 1}}, {"$group": {"_id": "$category", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
            cats = await db.library.aggregate(pipeline).to_list(length=None)
            GLOBAL_CACHE["categories"] = [c["_id"] for c in cats if c["_id"] and str(c["_id"]).lower() != 'nan']
        btns = [[InlineKeyboardButton(f"📁 | {c}", callback_data=f"mgr_cat_view_{c[:40]}")] for c in GLOBAL_CACHE["categories"]]
        btns.append([InlineKeyboardButton("➕ | إضافة سلسلة جديدة", callback_data="mgr_add_cat")])
        btns.append([InlineKeyboardButton("🔙 | رجوع للوحة الإدارة", callback_data="admin_menu")])
        return await query.edit_message_text("📂 **إدارة السلاسل والدروس:**\nاختر سلسلة لتعديلها أو إضافة دروس إليها:", reply_markup=InlineKeyboardMarkup(btns))

    if data == "mgr_add_cat" and await has_perm(user_id, "upload"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_MGR_NEW_CAT"}})
        return await query.edit_message_text("✍️ أرسل اسم **السلسلة الجديدة**:\nسيتم إضافة درس افتراضي بداخلها لتأسيسها.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))

    if data.startswith("mgr_cat_view_"):
        cat_name = data.replace("mgr_cat_view_", "")
        pipeline = [{"$match": {"category": {"$regex": f"^{cat_name}"}}}, {"$sort": {"_id": 1}}, {"$group": {"_id": "$lesson", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        lessons = await db.library.aggregate(pipeline).to_list(length=None)
        
        btns = [[InlineKeyboardButton(f"📖 | {idx}- {les['_id']}", callback_data=f"mgr_les_{str(les['doc_id'])}")] for idx, les in enumerate(lessons, 1)]
        
        btns.append([InlineKeyboardButton("➕ | إضافة درس جديد", callback_data=f"mgr_add_les_{cat_name[:40]}")])
        btns.append([InlineKeyboardButton("✏️ | تعديل اسم السلسلة", callback_data=f"mgr_edit_cat_{cat_name[:40]}")])
        btns.append([InlineKeyboardButton("🗑️ | حذف السلسلة (خطير)", callback_data=f"mgr_del_cat_{cat_name[:40]}")])
        btns.append([InlineKeyboardButton("🔙 | رجوع للسلاسل", callback_data="admin_content_mgr")])
        
        return await query.edit_message_text(f"📁 السلسلة: **{cat_name}**\nيمكنك إضافة دروس جديدة أو التعديل:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if data.startswith("mgr_add_les_"):
        cat_name = data.replace("mgr_add_les_", "")
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_MGR_NEW_LES", "temp_data": {"mgr_target_cat": cat_name}}})
        return await query.edit_message_text(f"✍️ أرسل اسم **الدرس الجديد** للسلسلة ({cat_name}):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))

    if data.startswith("mgr_edit_cat_"):
        cat_name = data.replace("mgr_edit_cat_", "")
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_MGR_EDIT_CAT", "temp_data": {"mgr_target_cat": cat_name}}})
        return await query.edit_message_text(f"✍️ أرسل **الاسم الجديد** بدلاً من ({cat_name}):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))

    if data.startswith("mgr_del_cat_"):
        cat_name = data.replace("mgr_del_cat_", "")
        await db.library.delete_many({"category": {"$regex": f"^{cat_name}"}})
        await db.questions.delete_many({"category": {"$regex": f"^{cat_name}"}})
        clear_cache()
        return await query.edit_message_text(f"✅ تم حذف السلسلة ({cat_name}) بالكامل!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_content_mgr")]]))

    if data.startswith("mgr_les_"):
        doc_id = data.replace("mgr_les_", "")
        doc = await db.library.find_one({"_id": ObjectId(doc_id)})
        if not doc: return await query.answer("الدرس غير موجود", show_alert=True)
        les_name = doc["lesson"]
        cat_name = doc["category"]
        
        await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": {"mgr_target_cat": cat_name, "mgr_target_les": les_name}}})
        
        btns = [
            [InlineKeyboardButton("✏️ | تعديل اسم الدرس", callback_data="mgr_action_edit_les")],
            [InlineKeyboardButton("🗑️ | حذف الدرس", callback_data="mgr_action_del_les")],
            [InlineKeyboardButton("🔙 | رجوع لدروس السلسلة", callback_data=f"mgr_cat_view_{cat_name[:40]}")]
        ]
        return await query.edit_message_text(f"📖 الدرس: **{les_name}**\nماذا تريد أن تفعل؟", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if data == "mgr_action_edit_les":
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_MGR_EDIT_LES"}})
        les = user.get("temp_data", {}).get("mgr_target_les")
        return await query.edit_message_text(f"✍️ أرسل **الاسم الجديد** للدرس بدلاً من ({les}):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))

    if data == "mgr_action_del_les":
        cat = user.get("temp_data", {}).get("mgr_target_cat")
        les = user.get("temp_data", {}).get("mgr_target_les")
        await db.library.delete_many({"category": cat, "lesson": les})
        await db.questions.delete_many({"category": cat, "lesson": les})
        clear_cache()
        return await query.edit_message_text(f"✅ تم حذف الدرس ({les}) بالكامل!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data=f"mgr_cat_view_{cat[:40]}")]]))

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
        lesson_name = doc["lesson"] if doc else "بدون عنوان"
        user = await db.users.find_one({"_id": user_id})
        temp_data = user.get("temp_data", {})
        temp_data["pub_les"] = lesson_name
        
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": temp_data}})
        
        templates = await db.templates.find({}).to_list(length=None)
        btns = []
        for t in templates:
            btns.append([InlineKeyboardButton(f"📄 | {t['name']}", callback_data=f"pubfmt_tpl_{str(t['_id'])}")])
        btns.append([InlineKeyboardButton("📝 | قالب النص الكلاسيكي (تلقائي المسميات)", callback_data="pubfmt_text")])
        btns.append([InlineKeyboardButton("🔲 | قالب الأزرار الشفافة", callback_data="pubfmt_btns")])
        btns.append([InlineKeyboardButton("❌ | إلغاء", callback_data="admin_cancel")])
        
        return await query.edit_message_text(f"✅ تم اختيار: **{lesson_name}**\n\nاختر القالب الذي تفضله لتوليد المنشور:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if data.startswith("pubfmt_"):
        fmt_type = data.replace("pubfmt_", "")
        user = await db.users.find_one({"_id": user_id})
        temp_data = user.get("temp_data", {})
        temp_data["draft_format_key"] = fmt_type
        await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": temp_data}})

        les = temp_data.get("pub_les", "عام")
        items = await db.library.find({"lesson": les}).to_list(length=None)
        has_media = False
        for item in items:
            safe_link = fix_link(item.get("file_id"))
            if safe_link: has_media = True
        
        if has_media:
            btns = []
            btns.append([InlineKeyboardButton("🖼️/🎬 إرفاق وسائط من الدرس", callback_data="pubmed_auto")])
            btns.append([InlineKeyboardButton("📝 نص فقط (بدون وسائط)", callback_data="pubmed_none")])
            btns.append([InlineKeyboardButton("❌ إلغاء العملية", callback_data="admin_cancel")])
            return await query.edit_message_text("🎨 **تصميم المنشور:**\nهل تود إرفاق وسائط (صورة/فيديو) مع هذا المنشور لجعله أكثر جاذبية؟", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")
        else: data = "pubmed_none" 

    if data.startswith("pubmed_"):
        media_choice = data.replace("pubmed_", "")
        user = await db.users.find_one({"_id": user_id})
        temp_data = user.get("temp_data", {})
        temp_data["pub_media"] = media_choice
        fmt_type = temp_data.get("draft_format_key", "text")
        
        cat = temp_data.get("pub_cat", "عام")
        les = temp_data.get("pub_les", "عام")
        date_txt = get_auto_arabic_date()
        footer_content = await get_footer_text()

        types_docs = await db.content_types.find({}).to_list(length=None)
        ch_link = f"https://t.me/{CHANNEL_ID.replace('@','')}"
        
        dynamic_links = {t["name"]: ch_link for t in types_docs}
        media_candidate = None

        items = await db.library.find({"lesson": les}).to_list(length=None)
        for item in items:
            f_type = str(item.get("type", ""))
            safe_link = fix_link(item.get("file_id"))
            if safe_link:
                for t in types_docs:
                    if t["_id"] == f_type:
                        dynamic_links[t["name"]] = safe_link
                        if media_choice == "auto" and not media_candidate:
                            media_candidate = safe_link 
        
        temp_data["link_auto_media"] = media_candidate if media_choice == "auto" else None
        media_note = "📌 `[سيتم إرفاق وسائط الدرس إن وجدت]`\n\n" if media_choice == "auto" else ""

        if fmt_type == "btns":
            draft_text = f"{cat} - {les}\n\nدرس اليوم {date_txt}\n\n{footer_content}"
            temp_data["draft_format"] = "btns"
            temp_data["draft_text"] = draft_text
            await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": temp_data}})
            btns = [[InlineKeyboardButton("✅ | المتابعة لاختيار القناة", callback_data="pub_select_chan")], [InlineKeyboardButton("❌ | إلغاء", callback_data="admin_cancel")]]
            return await query.edit_message_text(f"🔲 **معاينة المسودة (أزرار):**\n\n{media_note}{draft_text}", reply_markup=InlineKeyboardMarkup(btns), disable_web_page_preview=True, parse_mode="Markdown")

        elif fmt_type == "text":
            safe_cat = html.escape(cat)
            safe_les = html.escape(les)
            draft_text = f"<b>{safe_cat} - {safe_les}</b>\n\nدرس اليوم {date_txt}\n\n"
            
            for t_name, t_link in dynamic_links.items():
                if t_link != ch_link: 
                    draft_text += f"<blockquote>{t_name} <a href='{t_link}'>إضغط هنا</a> ❞</blockquote>\n"
                    
            draft_text += f"\n\n{html.escape(footer_content)}"
            temp_data["draft_format"] = "html_text"
            temp_data["draft_text"] = draft_text
            await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": temp_data}})
            btns = [[InlineKeyboardButton("✅ | المتابعة لاختيار القناة", callback_data="pub_select_chan")], [InlineKeyboardButton("❌ | إلغاء", callback_data="admin_cancel")]]
            
            preview_text = f"📝 <b>معاينة المسودة:</b>\n\n{media_note.replace('`','')}{draft_text}\n\n--- \nهل تريد المتابعة؟"
            return await query.edit_message_text(preview_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns), disable_web_page_preview=True)

        elif fmt_type.startswith("tpl_"):
            tpl_id = fmt_type.replace("tpl_", "")
            tpl_doc = await db.templates.find_one({"_id": ObjectId(tpl_id)})
            if not tpl_doc: return await query.answer("القالب غير موجود!", show_alert=True)
            
            draft_text = tpl_doc["content"]
            draft_text = draft_text.replace("{سلسلة}", html.escape(cat))
            draft_text = draft_text.replace("{درس}", html.escape(les))
            draft_text = draft_text.replace("{تاريخ}", date_txt)
            draft_text = draft_text.replace("{تذييل}", html.escape(footer_content))
            
            for t_name, t_link in dynamic_links.items():
                draft_text = draft_text.replace(f"{{{t_name}}}", t_link)

            temp_data["draft_format"] = "html_dynamic"
            temp_data["draft_text"] = draft_text
            await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": temp_data}})
            
            btns = [[InlineKeyboardButton("✅ | المتابعة لاختيار القناة", callback_data="pub_select_chan")], [InlineKeyboardButton("❌ | إلغاء", callback_data="admin_cancel")]]
            try:
                preview_text = f"📝 <b>معاينة المسودة:</b>\n\n{media_note.replace('`','')}{draft_text}\n\n--- \nهل تريد المتابعة؟"
                return await query.edit_message_text(preview_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns), disable_web_page_preview=True)
            except Exception as e:
                return await query.edit_message_text(f"❌ **خطأ في كود HTML للقالب!**\n\n`{e}`", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_tpl_menu")]]))

    if data == "pub_select_chan":
        channels_doc = await db.settings.find_one({"_id": "channels"})
        channels = channels_doc.get("list", []) if channels_doc else []
        if CHANNEL_ID and CHANNEL_ID not in channels: channels.insert(0, CHANNEL_ID)
        
        btns = [[InlineKeyboardButton(f"📡 انشر في: {ch}", callback_data=f"pconf_{ch}")] for ch in channels]
        btns.append([InlineKeyboardButton("➕ إضافة قناة جديدة", callback_data="admin_channels")])
        btns.append([InlineKeyboardButton("🔙 تراجع للمسودة", callback_data="admin_pub_menu")])
        
        return await query.edit_message_text("اختر **القناة** التي تريد النشر فيها الآن:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    if data.startswith("pconf_"):
        target_channel = data.replace("pconf_", "")
        user = await db.users.find_one({"_id": user_id})
        temp_data = user.get("temp_data", {})
        draft_text = temp_data.get("draft_text", "")
        draft_format = temp_data.get("draft_format", "")
        media_link = temp_data.get("link_auto_media")
        
        inline_kb = None
        if draft_format == "btns":
            les = temp_data.get("pub_les", "")
            items = await db.library.find({"lesson": les}).to_list(length=None)
            types_docs = await db.content_types.find({}).to_list(length=None)
            inline_kb_arr, row = [], []
            for item in items:
                safe_link = fix_link(item.get("file_id"))
                f_type = str(item.get("type", ""))
                t_name = next((t["name"] for t in types_docs if t["_id"] == f_type), "رابط")
                if safe_link:
                    row.append(InlineKeyboardButton(t_name, url=safe_link))
                    if len(row) == 2: inline_kb_arr.append(row); row = []
            if row: inline_kb_arr.append(row)
            inline_kb = InlineKeyboardMarkup(inline_kb_arr)

        parse_m = "HTML" if draft_format.startswith("html") else None

        try:
            media_failed = False
            if not media_link:
                await context.bot.send_message(chat_id=target_channel, text=draft_text, parse_mode=parse_m, reply_markup=inline_kb, disable_web_page_preview=False)
            else:
                chat_from, msg_id = parse_tg_link(media_link)
                try:
                    await context.bot.copy_message(chat_id=target_channel, from_chat_id=chat_from, message_id=msg_id, caption=draft_text, parse_mode=parse_m, reply_markup=inline_kb)
                except Exception as e:
                    err_str = str(e).lower()
                    if "caption" in err_str or "too long" in err_str:
                        await context.bot.copy_message(chat_id=target_channel, from_chat_id=chat_from, message_id=msg_id)
                        await context.bot.send_message(chat_id=target_channel, text=draft_text, parse_mode=parse_m, reply_markup=inline_kb, disable_web_page_preview=False)
                    elif "not found" in err_str or "chat not found" in err_str:
                        await context.bot.send_message(chat_id=target_channel, text=draft_text, parse_mode=parse_m, reply_markup=inline_kb, disable_web_page_preview=False)
                        media_failed = True
                    else: raise e

            await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": {}}})
            btns = [[InlineKeyboardButton("🔙 | العودة لقسم النشر", callback_data="admin_publishing_hub")]]
            
            if media_failed:
                return await query.edit_message_text(f"✅ **تم نشر النص في ({target_channel})!**\n\n⚠️ *ملاحظة:* لم يتم إرفاق الوسائط لأن الرابط يشير لرسالة محذوفة.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))
            else:
                return await query.edit_message_text(f"🎉 **تم النشر بنجاح في ({target_channel})!**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))
        except Exception as e: 
            btns = [[InlineKeyboardButton("🔙 | العودة للوحة الإدارة", callback_data="admin_menu")]]
            return await query.edit_message_text(f"❌ حدث خطأ.\nتأكد أن البوت (مشرف) في القناة المقصودة وأن المعرف صحيح.\n`{e}`", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))

    if data.startswith("utype_"):
        val = data.replace("utype_", "")
        user = await db.users.find_one({"_id": user_id})
        temp_data = user.get("temp_data", {})
        cat = temp_data.get("category", "عام")
        les = temp_data.get("lesson", "عام")
        f_link = temp_data.get("file_id")
        tg_file_id = temp_data.get("telegram_file_id")

        await db.library.update_one({"category": cat, "lesson": les, "type": val}, {"$set": {"file_id": f_link, "created_at": time.time()}}, upsert=True)
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}})
        clear_cache()
        return await query.edit_message_text(f"🎉 **تم ربط الملف!**\n📁 السلسلة: {cat}\n📖 الدرس: {les}", parse_mode="Markdown")

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
        return await query.edit_message_text(f"📖 المحاضرة: **{lesson_name}**\n👇 ما هو **نوع** هذا المحتوى؟", parse_mode="Markdown", reply_markup=await get_type_keyboard())

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

    if data == "admin_add_q" and await has_perm(user_id, "questions"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_Q_CAT"}})
        if "categories" not in GLOBAL_CACHE: 
            pipeline = [{"$sort": {"_id": 1}}, {"$group": {"_id": "$category", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
            cats = await db.library.aggregate(pipeline).to_list(length=None)
            GLOBAL_CACHE["categories"] = [c["_id"] for c in cats if c["_id"] and str(c["_id"]).lower() != 'nan']
        btns = [[InlineKeyboardButton(f"📁 | {c}", callback_data=f"qaddc_{c[:50]}")] for c in GLOBAL_CACHE["categories"]]
        btns.append([InlineKeyboardButton("🔙 | رجوع للوحة الإدارة", callback_data="admin_menu")])
        return await query.edit_message_text("📝 **إضافة سؤال/اختبار:**\nاختر السلسلة:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

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
        await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": temp_data}})
        
        btns = [
            [InlineKeyboardButton("✍️ إضافة سؤال واحد (يدوياً)", callback_data="qadd_manual")],
            [InlineKeyboardButton("📥 رفع اختبار كامل (ملف إكسل)", callback_data="qadd_excel")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]
        ]
        return await query.edit_message_text(f"📖 المحاضرة: **{lesson_name}**\n\nكيف تود إضافة الأسئلة؟", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))

    if data == "qadd_manual":
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_Q_TEXT"}})
        return await query.edit_message_text("✍️ أرسل **نص السؤال**:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))

    if data == "qadd_excel":
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_Q_EXCEL"}})
        msg = """📥 **رفع ملف إكسل لاختبار الدرس**

⚠️ **لتجنب أي أخطاء أثناء الرفع، يرجى تجهيز الملف كالتالي:**
1. يجب أن يكون الملف بصيغة **Excel (.xlsx)**.
2. يجب أن يحتوي **الصف الأول** على أسماء الأعمدة التالية بدقة:
   ▫️ `السؤال` : لكتابة نص السؤال.
   ▫️ `صحيح` : لكتابة الإجابة الصحيحة.
   ▫️ `خاطئة` أو `خطأ` : لكتابة الإجابات الخاطئة (يمكنك عمل أكثر من عمود بنفس الاسم لعدة خيارات).

💡 *طريقة حذف سؤال:* اكتب نص السؤال، واكتب كلمة `حذف` في عمود "صحيح".

👇 **أرسل ملف الإكسل الآن كـ (مستند / Document):**"""
        return await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))

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
        if back_doc_id: btns.append([InlineKeyboardButton("🔙 | العودة للدرس", callback_data=f"les_{back_doc_id}")])
        btns.append([InlineKeyboardButton("🏠 | الرئيسية", callback_data="main_menu")])
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
    if back_doc_id: nav_row.append(InlineKeyboardButton("🔙 | إنهاء الاختبار", callback_data=f"les_{back_doc_id}"))
    nav_row.append(InlineKeyboardButton("🏠 | الرئيسية", callback_data="main_menu"))
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
