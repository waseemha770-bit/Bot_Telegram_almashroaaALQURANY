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
            await db.comp_answers.create_index([("user_id", 1), ("q_id", 1)], unique=True)
            
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

def get_safe_oid(doc_id):
    try: return ObjectId(doc_id)
    except: return None

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
    except: pass 

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
    if len(parts) >= 3 and parts[-3] == 'c': chat_id = f"-100{parts[-2]}"
    else:
        chat_id_str = parts[-2]
        chat_id = chat_id_str if chat_id_str.startswith("-100") else (f"-100{chat_id_str}" if chat_id_str.isdigit() else f"@{chat_id_str}")
    return chat_id, msg_id

def fix_link(raw_link):
    if not raw_link or str(raw_link).strip().lower() in ['', 'nan', 'none', 'null', 'لا يوجد']: return None
    raw_str = str(raw_link).strip().replace(" ", "")
    if raw_str.startswith("http"): return raw_str
    if raw_str.startswith("t.me/"): return f"https://{raw_str}"
    if raw_str.isdigit():
        ch_name = CHANNEL_ID.replace('@', '').replace('https://t.me/', '')
        return f"https://t.me/{ch_name}/{raw_str}"
    if "/" in raw_str: return f"https://t.me/{raw_str}"
    return raw_str

async def get_admin_doc(user_id: str):
    if str(user_id) == OWNER_ID: 
        return {"_id": OWNER_ID, "permissions": {"upload": True, "questions": True, "publish": True, "stats": True, "manage_admins": True}}
    if db is not None: return await db.admins.find_one({"_id": str(user_id)})
    return None

async def has_perm(user_id: str, perm: str) -> bool:
    if str(user_id) == OWNER_ID: return True
    adm = await get_admin_doc(user_id)
    if adm and adm.get("permissions", {}).get(perm, False): return True
    return False

def get_perms_kb(perms, edit_mode=False, admin_id=None):
    def mk_btn(text, key): return InlineKeyboardButton(f"{'✅' if perms.get(key) else '❌'} | {text}", callback_data=f"adm_tgl_{key}")
    kb = [
        [mk_btn("إدارة السلاسل والمحتوى", "upload")], 
        [mk_btn("الأسئلة والاختبارات", "questions")], 
        [mk_btn("النشر والمسابقات", "publish")], 
        [mk_btn("الإحصائيات والتصدير", "stats")],
        [mk_btn("إدارة المشرفين", "manage_admins")]
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
    return ReplyKeyboardMarkup([["🔍 اعرف الله", "⚙️ لوحة الإدارة"]], resize_keyboard=True)

async def get_type_keyboard():
    types = await db.content_types.find({}).to_list(length=None)
    kb, row = [], []
    for t in types:
        row.append(InlineKeyboardButton(f"{t['icon']} {t['name']}", callback_data=f"utype_{t['_id']}"))
        if len(row) == 2: kb.append(row); row = []
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

async def safe_edit(query, text, markup=None):
    try: await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
    except Exception as e:
        if "Message is not modified" not in str(e): logging.error(f"Edit msg error: {e}")

async def show_lesson_ui(context, chat_id, doc_id, message_id=None, user_id=None):
    if db is None: return
    oid = get_safe_oid(doc_id)
    if not oid:
        txt = "⚠️ القائمة قديمة، يرجى تحديث النظام بإرسال /start."
        if message_id: 
            try: await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=message_id, parse_mode="HTML")
            except: pass
        else: await context.bot.send_message(chat_id, txt, parse_mode="HTML")
        return
        
    doc = await db.library.find_one({"_id": oid})
    if not doc:
        txt = "⚠️ عذراً، هذا الدرس تم حذفه ولم يعد متوفراً."
        if message_id: 
            try: await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=message_id, parse_mode="HTML")
            except: pass
        else: await context.bot.send_message(chat_id, txt, parse_mode="HTML")
        return

    lesson_title, series = doc.get("lesson", "بدون عنوان"), doc.get("category", "عام")
    if user_id: asyncio.create_task(background_db_update(user_id, lesson_view=lesson_title, cat_view=series))
    
    items = await db.library.find({"lesson": lesson_title}).to_list(length=None)
    types_docs = await db.content_types.find({}).to_list(length=None)
    types_dict = {t["_id"]: t for t in types_docs}
    
    links = {}
    for item in items:
        f_type = str(item.get("type", ""))
        safe_link = fix_link(item.get("file_id"))
        if safe_link: links[f_type] = safe_link

    def make_btn(text, link): return InlineKeyboardButton(text, url=link) if link else InlineKeyboardButton(text, callback_data="media_unavail")

    btns, row = [], []
    for t_id, t_info in types_dict.items():
        row.append(make_btn(f"{t_info['icon']} {t_info['name']}", links.get(t_id)))
        if len(row) == 2: btns.append(row); row = []
    if row: btns.append(row)

    cat_doc = await db.library.find_one({"category": series})
    cat_id = str(cat_doc["_id"]) if cat_doc else str(doc_id)

    btns.append([InlineKeyboardButton("✨ 📝 قيم نفسك ✨", callback_data=f"quizles_{doc_id}")])
    share_url = f"https://t.me/share/url?text=📚 إليك هذا الدرس القيم: {lesson_title}\n&url=https://t.me/{context.bot.username}?start=les_{doc_id}"
    btns.append([InlineKeyboardButton("🔗 شارك هذا الدرس (لتعم الفائدة)", url=share_url)])
    btns.append([InlineKeyboardButton("🔙 السابق", callback_data=f"cat_{cat_id}"), InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu")])

    txt = f"📖 <b>{html.escape(lesson_title)}</b>\n📂 السلسلة: <b>{html.escape(series)}</b>\n\n👇 اختر المحتوى للانتقال إليه:"
    try:
        if message_id: await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=message_id, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
        else: 
            if user_id: await clean_chat_history(user_id, chat_id, context)
            sent_msg = await context.bot.send_message(chat_id, txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
            if user_id: await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
    except: pass

async def handle_media_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id, chat_id = str(update.effective_user.id), update.effective_chat.id
    msg = update.message
    user = await db.users.find_one({"_id": user_id})
    state, temp_data = user.get("state", ""), user.get("temp_data", {}) if user else {}
    
    if state == "WAIT_CONTENT" and (msg.document or msg.video or msg.audio or msg.voice or msg.photo or msg.text):
        if not await has_perm(user_id, "upload"): return
        await clean_chat_history(user_id, chat_id, context)
        link = None
        if msg.text and msg.text.startswith("http"): link = msg.text
        else:
            try:
                res = await context.bot.copy_message(chat_id=CHANNEL_ID, from_chat_id=chat_id, message_id=msg.message_id)
                ch_name = CHANNEL_ID.replace('@', '').replace('https://t.me/', '')
                link = f"https://t.me/{ch_name}/{res.message_id}"
            except Exception: return await msg.reply_text("❌ يرجى التأكد من رفع البوت كمشرف في القناة الافتراضية لحفظ النصوص.")
        temp_data["pending_link"] = link
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_CONTENT_TYPE", "temp_data": temp_data}}, upsert=True)
        sent_msg = await msg.reply_text("✅ <b>تم استلام الملف بنجاح!</b>\n👇 حدد نوع هذا المحتوى ليتم ربطه بالدرس:", parse_mode="HTML", reply_markup=await get_type_keyboard())
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_Q_EXCEL" and msg.document:
        if not await has_perm(user_id, "questions"): return
        if not msg.document.file_name.endswith(('.xlsx', '.xls')): return await msg.reply_text("⚠️ يرجى رفع ملف بصيغة Excel.")
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await msg.reply_text("⏳ جاري قراءة وتحليل ملف الاختبار...")
        try:
            file = await context.bot.get_file(msg.document.file_id)
            df = pd.read_excel(pd.ExcelFile(io.BytesIO(await file.download_as_bytearray())), sheet_name=0)
            cat, les, count_add, count_del = temp_data.get("q_cat", "عام"), temp_data.get("q_les", "عام"), 0, 0
            cols = list(df.columns)
            q_col = next((c for c in cols if 'سؤال' in str(c)), cols[0] if len(cols) > 0 else None)
            ans_col = next((c for c in cols if 'صحيح' in str(c)), cols[1] if len(cols) > 1 else None)
            
            if not q_col or not ans_col: raise ValueError("الملف فارغ أو تنسيق الأعمدة غير صحيح.")

            for _, row in df.iterrows():
                try:
                    if pd.notna(row.get(q_col)) and pd.notna(row.get(ans_col)):
                        q_val, ans_val = str(row[q_col]).strip(), str(row[ans_col]).strip()
                        if ans_val == "حذف":
                            await db.questions.delete_many({"category": cat, "lesson": les, "question": q_val})
                            count_del += 1
                        else:
                            wrongs = [str(row[wc]).strip() for wc in cols if wc != q_col and wc != ans_col and pd.notna(row.get(wc))]
                            await db.questions.update_one({"category": cat, "lesson": les, "question": q_val}, {"$set": {"correct": ans_val, "wrong": wrongs}}, upsert=True)
                            count_add += 1
                except Exception: continue

            await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
            await context.bot.delete_message(chat_id=chat_id, message_id=sent_msg.message_id)
            res_txt = f"🎉 <b>تمت مزامنة الاختبار لدرس ({html.escape(les)})!</b>\n" + (f"✅ تم تحديث {count_add} سؤال.\n" if count_add else "") + (f"🗑️ تم حذف {count_del} سؤال.\n" if count_del else "")
            final_msg = await msg.reply_text(res_txt, parse_mode="HTML")
            await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": final_msg.message_id}}, upsert=True)
            return
        except Exception as e:
            await context.bot.delete_message(chat_id=chat_id, message_id=sent_msg.message_id)
            return await msg.reply_text(f"❌ حدث خطأ أثناء قراءة ملف الإكسل للاختبار:\n<code>{e}</code>", parse_mode="HTML")

    if state == "WAIT_EXCEL" and msg.document:
        if not await has_perm(user_id, "upload"): return
        if not msg.document.file_name.endswith(('.xlsx', '.xls')): return await msg.reply_text("⚠️ يرجى رفع ملف بصيغة Excel (.xlsx) فقط.")
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await msg.reply_text("⏳ جاري تحليل قاعدة البيانات وتطبيق نظام المزامنة الذكية...")
        try:
            file = await context.bot.get_file(msg.document.file_id)
            xls = pd.ExcelFile(io.BytesIO(await file.download_as_bytearray()))
            updates_log, df_lib, df_q, df_struct = "", None, None, None

            for sheet in xls.sheet_names:
                df = pd.read_excel(xls, sheet_name=sheet)
                cols = [str(c).strip() for c in df.columns]
                if any('الإجراء' in c for c in cols) and any('السلسلة' in c for c in cols): df_struct = df
                elif any('السؤال' in c for c in cols) and any('الصحيح' in c for c in cols): df_q = df
                elif any('السلسلة' in c for c in cols) and any('الدرس' in c or 'المحاضرة' in c for c in cols): df_lib = df

            if df_struct is not None:
                count_s_add, count_s_edit, count_s_del = 0, 0, 0
                for _, row in df_struct.iterrows():
                    cols = [str(c).strip() for c in df_struct.columns]
                    action_col = next((c for c in cols if 'الإجراء' in c or 'إجراء' in c), None)
                    cat_col = next((c for c in cols if 'السلسلة' in c and 'جديد' not in c), None)
                    les_col = next((c for c in cols if ('الدرس' in c or 'المحاضرة' in c) and 'جديد' not in c), None)
                    new_cat_col = next((c for c in cols if 'السلسلة' in c and 'جديد' in c), None)
                    new_les_col = next((c for c in cols if ('الدرس' in c or 'المحاضرة' in c) and 'جديد' in c), None)

                    if action_col and cat_col and pd.notna(row.get(action_col)) and pd.notna(row.get(cat_col)):
                        action, cat = str(row[action_col]).strip(), str(row[cat_col]).strip()
                        les = str(row[les_col]).strip() if les_col and pd.notna(row.get(les_col)) else None
                        new_cat = str(row[new_cat_col]).strip() if new_cat_col and pd.notna(row.get(new_cat_col)) else cat
                        new_les = str(row[new_les_col]).strip() if new_les_col and pd.notna(row.get(new_les_col)) else les

                        if "حذف" in action:
                            if les:
                                await db.library.delete_many({"category": cat, "lesson": les})
                                await db.questions.delete_many({"category": cat, "lesson": les})
                            else:
                                await db.library.delete_many({"category": {"$regex": f"^{cat}"}})
                                await db.questions.delete_many({"category": {"$regex": f"^{cat}"}})
                            count_s_del += 1
                        elif "تعديل" in action:
                            if les and new_les:
                                await db.library.update_many({"category": cat, "lesson": les}, {"$set": {"category": new_cat, "lesson": new_les}})
                                await db.questions.update_many({"category": cat, "lesson": les}, {"$set": {"category": new_cat, "lesson": new_les}})
                                await db.lesson_stats.update_many({"category": cat, "lesson": les}, {"$set": {"category": new_cat, "lesson": new_les}})
                            else:
                                await db.library.update_many({"category": cat}, {"$set": {"category": new_cat}})
                                await db.questions.update_many({"category": cat}, {"$set": {"category": new_cat}})
                                await db.lesson_stats.update_many({"category": cat}, {"$set": {"category": new_cat}})
                            count_s_edit += 1
                        elif "إضافة" in action:
                            if les: await db.library.insert_one({"category": cat, "lesson": les, "type": "type_text", "file_id": None})
                            else: await db.library.insert_one({"category": cat, "lesson": "درس افتراضي", "type": "type_text", "file_id": None})
                            count_s_add += 1
                if count_s_add > 0: updates_log += f"✅ تم إضافة {count_s_add} هيكل.\n"
                if count_s_edit > 0: updates_log += f"✏️ تم تعديل {count_s_edit} هيكل.\n"
                if count_s_del > 0: updates_log += f"🗑️ تم حذف {count_s_del} هيكل.\n\n"

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
                        cat_val, les_val = str(row[cat_col]).strip(), str(row[les_col]).strip()
                        excel_type_val, t_val = str(row.get(type_col, '')).strip(), "type_text"
                        for t_name, t_id in name_to_id.items():
                            if t_name in excel_type_val.lower() or excel_type_val.lower() in t_name: t_val = t_id; break
                        if "فيديو" in excel_type_val: t_val = "type_video"
                        elif "صوت" in excel_type_val: t_val = "type_audio"
                        elif "صور" in excel_type_val or "فلاشة" in excel_type_val: t_val = "type_image"

                        l_val = str(row.get(link_col, '')).strip() if link_col and pd.notna(row.get(link_col)) else ""
                        if l_val == "حذف":
                            await db.library.delete_many({"category": cat_val, "lesson": les_val, "type": t_val})
                            count_del += 1
                        elif l_val and l_val.lower() not in ['nan', 'none', 'null']:
                            await db.library.update_one({"category": cat_val, "lesson": les_val, "type": t_val}, {"$set": {"file_id": l_val, "updated_at": time.time()}}, upsert=True)
                            count_add += 1
                if count_add > 0: updates_log += f"✅ تم مزامنة {count_add} رابط.\n"
                if count_del > 0: updates_log += f"🗑️ تم حذف {count_del} رابط.\n"
                
            if df_q is not None:
                count_q_add, count_q_del = 0, 0
                cols_q = list(df_q.columns)
                q_col, ans_col = next((c for c in cols_q if 'السؤال' in str(c)), None), next((c for c in cols_q if 'الصحيح' in str(c)), None)
                cat_col, les_col = next((c for c in cols_q if 'السلسلة' in str(c)), None), next((c for c in cols_q if 'الدرس' in str(c) or 'المحاضرة' in str(c)), None)
                
                for _, row in df_q.iterrows():
                    if q_col and ans_col and pd.notna(row.get(q_col)) and pd.notna(row.get(ans_col)):
                        q_val, ans_val = str(row[q_col]).strip(), str(row[ans_col]).strip()
                        cat_val = str(row.get(cat_col, 'عام')).strip() if cat_col and pd.notna(row.get(cat_col)) else 'عام'
                        les_val = str(row.get(les_col, 'عام')).strip() if les_col and pd.notna(row.get(les_col)) else 'عام'
                        
                        if ans_val == "حذف":
                            await db.questions.delete_many({"category": cat_val, "lesson": les_val, "question": q_val})
                            count_q_del += 1
                        else:
                            wrongs = [str(row[wc]).strip() for wc in cols_q if ('خاطئة' in str(wc) or 'خطأ' in str(wc)) and pd.notna(row.get(wc))]
                            await db.questions.update_one({"category": cat_val, "lesson": les_val, "question": q_val}, {"$set": {"correct": ans_val, "wrong": wrongs}}, upsert=True)
                            count_q_add += 1
                if count_q_add > 0: updates_log += f"✅ تم مزامنة {count_q_add} سؤال عام.\n"
                if count_q_del > 0: updates_log += f"🗑️ تم حذف {count_q_del} سؤال عام.\n"

            if not updates_log: 
                await context.bot.delete_message(chat_id=chat_id, message_id=sent_msg.message_id)
                return await msg.reply_text("⚠️ لم يتم العثور على تحديثات.")
            
            await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}}, upsert=True)
            await context.bot.delete_message(chat_id=chat_id, message_id=sent_msg.message_id)
            final_msg = await msg.reply_text(f"🎉 <b>اكتملت المزامنة الذكية بنجاح!</b>\n\n{updates_log}", parse_mode="HTML")
            await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": final_msg.message_id}}, upsert=True)
            return
        except Exception as e: return await msg.reply_text(f"❌ خطأ: <code>{e}</code>", parse_mode="HTML")

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
                return await msg.reply_text(f"⚠️ <b>تنبيه:</b> موجود مسبقاً!\n📁 السلسلة: {html.escape(existing.get('category',''))}\n📖 المحاضرة: {html.escape(existing.get('lesson',''))}", parse_mode="HTML")
            
        pipeline = [{"$group": {"_id": "$category", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        cats = await db.library.aggregate(pipeline).to_list(length=None)
        
        btns = [[InlineKeyboardButton(f"📁 | {c['_id']}", callback_data=f"uc_{str(c['doc_id'])}")] for c in cats[:90] if c['_id'] and str(c['_id']).lower() != 'nan']
        btns.append([InlineKeyboardButton("➕ | إضافة سلسلة جديدة", callback_data="uc_new")])
        btns.append([InlineKeyboardButton("❌ | إلغاء الربط", callback_data="admin_cancel")])
        
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await msg.reply_text("📥 <b>تم استلام المحتوى!</b>\nاختر السلسلة:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "UPLOADING", "temp_data": {"file_id": final_link, "telegram_file_id": telegram_file_id}, "last_msg_id": sent_msg.message_id}}, upsert=True)

# ==========================================
# معالجة الرسائل النصية الشاملة
# ==========================================
async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    text = update.message.text
    if not text: return
    if await check_spam(user_id): return
    if db is None: return await update.message.reply_text("⚠️ خطأ في الاتصال.")

    asyncio.create_task(background_db_update(user_id))
    kb = await get_main_keyboard(user_id)
    
    if text in ['إلغاء', '❌ إلغاء', '/cancel']:
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text("✅ <b>تم إلغاء العملية.</b>", parse_mode="HTML", reply_markup=kb)
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if text.startswith('/start'):
        u_name = update.message.from_user.first_name
        u_username = update.message.from_user.username
        full_name_str = f"{u_name} (@{u_username})" if u_username else u_name
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}, "name": full_name_str}, "$setOnInsert": {"score": 0, "comp_score": 0, "answered": []}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        if 'les_' in text: return await show_lesson_ui(context, chat_id, text.replace('/start les_', '').strip(), user_id=user_id)
        sent_msg = await update.message.reply_text("📖 <b>أهلاً بك في منصة المشروع القرآني</b>\n\nتصفح الدروس وابدأ رحلتك المعرفية بالضغط على الزر أدناه 👇", parse_mode="HTML", reply_markup=kb)
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if 'اعرف الله' in text:
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
        pipeline = [{"$group": {"_id": "$category", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        try: cats = await db.library.aggregate(pipeline).to_list(length=None)
        except Exception as e: logging.error(f"Agg err: {e}"); cats = []
        
        await clean_chat_history(user_id, chat_id, context)
        if not cats: 
            sent_msg = await update.message.reply_text("📚 السلاسل قيد التجهيز.", reply_markup=kb)
            await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
            return
            
        btns = [[InlineKeyboardButton(f"📂 | {c['_id']}", callback_data=f"cat_{str(c['doc_id'])}")] for c in cats[:90] if c['_id'] and str(c['_id']).lower() != 'nan']
        try:
            sent_msg = await update.message.reply_text("📚 <b>المشروع القرآني:</b>\nيرجى اختيار السلسلة المطلوبة:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
            await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        except Exception as e: logging.error(f"Error in اعرف الله: {e}")
        return

    adm = await get_admin_doc(user_id)
    if 'لوحة الإدارة' in text and adm:
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
        btns = []
        if await has_perm(user_id, "upload"): btns.append([InlineKeyboardButton("📂 | إدارة السلاسل والدروس", callback_data="admin_content_mgr")])
        if await has_perm(user_id, "publish"):
            btns.append([InlineKeyboardButton("📢 | قسم النشر والقوالب", callback_data="admin_publishing_hub")])
            btns.append([InlineKeyboardButton("🏆 | مسابقات القناة (جديد)", callback_data="admin_comp_menu")])
            btns.append([InlineKeyboardButton("🎛️ | إدارة أنواع المحتوى", callback_data="admin_content_types")])
        if await has_perm(user_id, "questions"): btns.append([InlineKeyboardButton("➕ | إضافة اختبار/سؤال لدرس", callback_data="admin_add_q")])
        if await has_perm(user_id, "stats"): btns.append([InlineKeyboardButton("📈 | الإحصائيات الشاملة", callback_data="admin_stats")])
        btns.append([InlineKeyboardButton("📥 | تصدير / استيراد قاعدة البيانات", callback_data="admin_import_export")])
        if await has_perm(user_id, "manage_admins"): btns.append([InlineKeyboardButton("👥 | إدارة المشرفين", callback_data="admin_manage")])
        btns.append([InlineKeyboardButton("❌ | إغلاق اللوحة", callback_data="admin_cancel")])
        
        await clean_chat_history(user_id, chat_id, context)
        try:
            sent_msg = await update.message.reply_text("⚙️ <b>لوحة التحكم والإدارة:</b>\nاختر الإجراء المطلوب:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
            await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        except Exception as e: logging.error(f"Error Admin: {e}")
        return

    user = await db.users.find_one({"_id": user_id})
    state, temp_data = user.get("state", ""), user.get("temp_data", {}) if user else {}

    if state == "WAIT_CONTENT":
        if not await has_perm(user_id, "upload"): return
        await clean_chat_history(user_id, chat_id, context)
        if text.startswith("http"): link = text
        else:
            try:
                res = await context.bot.copy_message(chat_id=CHANNEL_ID, from_chat_id=chat_id, message_id=update.message.message_id)
                ch_name = CHANNEL_ID.replace('@', '').replace('https://t.me/', '')
                link = f"https://t.me/{ch_name}/{res.message_id}"
            except Exception:
                return await update.message.reply_text("❌ يرجى التأكد من رفع البوت كمشرف في القناة الافتراضية لحفظ النصوص.")
        temp_data["pending_link"] = link
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_CONTENT_TYPE", "temp_data": temp_data}}, upsert=True)
        sent_msg = await update.message.reply_text("✅ <b>تم استلام المحتوى بنجاح!</b>\n👇 حدد نوع هذا المحتوى ليتم ربطه بالدرس:", parse_mode="HTML", reply_markup=await get_type_keyboard())
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_COMP_TIME":
        if not await has_perm(user_id, "publish"): return
        if not text.isdigit(): return await update.message.reply_text("⚠️ يرجى إرسال رقم (عدد الدقائق) فقط.")
        minutes = int(text)
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}})
        await clean_chat_history(user_id, chat_id, context)
        
        q_oid = get_safe_oid(temp_data.get("comp_q_id"))
        if not q_oid: return await update.message.reply_text("السؤال غير متوفر.")
        q_doc = await db.questions.find_one({"_id": q_oid})
        if not q_doc: return await update.message.reply_text("السؤال غير متوفر.")
        
        expire_ts = int(time.time()) + (minutes * 60)
        options = [(q_doc["correct"], "1")] + [(w, "0") for w in q_doc["wrong"] if w and str(w).lower() != 'nan']
        random.shuffle(options)
        
        btns = [[InlineKeyboardButton(opt_text, callback_data=f"cq_{str(q_doc['_id'])}_{is_c}_{expire_ts}")] for opt_text, is_c in options]
        msg_text = f"🏆 <b>مسابقة القناة التفاعلية</b> 🏆\n\n📁 <b>السلسلة:</b> {html.escape(q_doc['category'])}\n📖 <b>الدرس:</b> {html.escape(q_doc['lesson'])}\n⏱️ <b>تنتهي خلال:</b> {minutes} دقيقة\n\n❓ <b>السؤال:</b>\n{html.escape(q_doc['question'])}"
        
        try:
            await context.bot.send_message(chat_id=CHANNEL_ID, text=msg_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
            sent_msg = await update.message.reply_text(f"🎉 <b>تم إرسال المسابقة للقناة بنجاح!</b>\nسيتم إغلاقها تلقائياً بعد {minutes} دقيقة.", parse_mode="HTML", reply_markup=kb)
            await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        except Exception as e:
            sent_msg = await update.message.reply_text(f"❌ حدث خطأ في النشر للقناة: <code>{e}</code>", parse_mode="HTML")
            await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    # بقية إدخالات الإدارة
    if state == "WAIT_MGR_NEW_CAT":
        new_cat = text.strip()
        await db.library.insert_one({"category": new_cat, "lesson": "درس افتراضي", "type": "type_text", "file_id": None})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة لمدير المحتوى", callback_data="admin_content_mgr")]]
        sent_msg = await update.message.reply_text(f"✅ تم إضافة السلسلة الجديدة: (<b>{html.escape(new_cat)}</b>) بنجاح!", reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_MGR_EDIT_CAT":
        old_cat, new_cat = temp_data.get("mgr_target_cat"), text.strip()
        await db.library.update_many({"category": old_cat}, {"$set": {"category": new_cat}})
        await db.questions.update_many({"category": old_cat}, {"$set": {"category": new_cat}})
        await db.lesson_stats.update_many({"category": old_cat}, {"$set": {"category": new_cat}})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة لمدير المحتوى", callback_data="admin_content_mgr")]]
        sent_msg = await update.message.reply_text(f"✅ تمت العملية!\nتم تحديث اسم السلسلة وتعديل كافة الدروس والأسئلة.", reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_MGR_NEW_LES":
        target_cat, cat_id, new_les = temp_data.get("mgr_target_cat"), temp_data.get("mgr_target_cat_id", ""), text.strip()
        await db.library.insert_one({"category": target_cat, "lesson": new_les, "type": "type_text", "file_id": None})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة للسلسلة", callback_data=f"mgr_cat_view_{cat_id}")]]
        sent_msg = await update.message.reply_text(f"✅ تم إنشاء الدرس الجديد بنجاح!", reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_MGR_EDIT_LES":
        target_cat, cat_id, old_les, new_les = temp_data.get("mgr_target_cat"), temp_data.get("mgr_target_cat_id", ""), temp_data.get("mgr_target_les"), text.strip()
        await db.library.update_many({"category": target_cat, "lesson": old_les}, {"$set": {"lesson": new_les}})
        await db.questions.update_many({"category": target_cat, "lesson": old_les}, {"$set": {"lesson": new_les}})
        await db.lesson_stats.update_many({"category": target_cat, "lesson": old_les}, {"$set": {"lesson": new_les}})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة للسلسلة", callback_data=f"mgr_cat_view_{cat_id}")]]
        sent_msg = await update.message.reply_text(f"✅ تمت العملية وتحديث الروابط والأسئلة المرتبطة به.", reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_TYPE_DATA" and await has_perm(user_id, "publish"):
        parts = text.split(',')
        name, icon = parts[0].strip(), parts[1].strip() if len(parts) > 1 else "📁"
        await db.content_types.insert_one({"_id": f"type_{int(time.time())}", "name": name, "icon": icon})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة", callback_data="admin_content_types")]]
        sent_msg = await update.message.reply_text(f"✅ تم إضافة النوع ({icon} {html.escape(name)}) بنجاح!", reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_EDIT_TYPE" and await has_perm(user_id, "publish"):
        parts = text.split(',')
        name, icon = parts[0].strip(), parts[1].strip() if len(parts) > 1 else "📁"
        await db.content_types.update_one({"_id": temp_data.get("edit_t_id")}, {"$set": {"name": name, "icon": icon}})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة", callback_data="admin_content_types")]]
        sent_msg = await update.message.reply_text(f"✅ تم تحديث النوع بنجاح!", reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_CHAN_ID" and await has_perm(user_id, "publish"):
        ch = text.strip()
        if not ch.startswith('@') and not ch.startswith('-100'): return await update.message.reply_text("⚠️ معرّف القناة يجب أن يبدأ بـ @ أو -100")
        await db.settings.update_one({"_id": "channels"}, {"$addToSet": {"list": ch}}, upsert=True)
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة لإدارة القنوات", callback_data="admin_channels")]]
        sent_msg = await update.message.reply_text(f"✅ تم إضافة القناة بنجاح!", reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_TPL_NAME":
        temp_data["tpl_name"] = text.strip()
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_TPL_CONTENT", "temp_data": temp_data}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        msg = f"""✅ تم اختيار اسم القالب: <b>{html.escape(text)}</b>\n\n✍️ أرسل الآن محتوى القالب وتصميمه.\nاستخدم المتغيرات بالأقواس المعكوفة:\n<code>{{سلسلة}}</code> ، <code>{{درس}}</code> ، <code>{{تاريخ}}</code> ، <code>{{تذييل}}</code>"""
        sent_msg = await update.message.reply_text(msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_TPL_CONTENT":
        tpl_name = temp_data.get("tpl_name", "قالب جديد")
        await db.templates.insert_one({"name": tpl_name, "content": text})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة للقوالب", callback_data="admin_tpl_menu")]]
        sent_msg = await update.message.reply_text(f"🎉 <b>تم حفظ القالب بنجاح!</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_FOOTER_TEXT":
        await db.settings.update_one({"_id": "bot_settings"}, {"$set": {"footer_text": text}}, upsert=True)
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text("✅ <b>تم حفظ نص/رابط التذييل بنجاح!</b>", parse_mode="HTML", reply_markup=kb)
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_ADMIN_ID" and await has_perm(user_id, "manage_admins"):
        new_admin = text.strip()
        if not new_admin.isdigit(): return await update.message.reply_text("⚠️ الآيدي يجب أن يكون أرقاماً فقط.")
        perms = {"upload": False, "questions": False, "publish": False, "stats": False, "manage_admins": False}
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {"new_admin_id": new_admin, "admin_perms": perms}}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text(f"⚙️ <b>تحديد صلاحيات المشرف ({new_admin}):</b>\nانقر للتفعيل ✅ أو التعطيل ❌ ثم حفظ:", parse_mode="HTML", reply_markup=get_perms_kb(perms, edit_mode=False))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_UPL_LES_TEXT":
        temp_data["lesson"] = text
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_TYPE", "temp_data": temp_data}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text(f"📖 المحاضرة: <b>{html.escape(text)}</b>\n\n👇 ما هو <b>نوع</b> هذا المحتوى؟", parse_mode="HTML", reply_markup=await get_type_keyboard())
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_Q_TEXT":
        temp_data["q_text"] = text
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_Q_CORRECT", "temp_data": temp_data}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text("✅ ممتاز.\nأرسل الآن <b>الإجابة الصحيحة</b>:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_Q_CORRECT":
        temp_data["q_correct"] = text
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_Q_WRONG", "temp_data": temp_data}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text("❌ أرسل الآن <b>الإجابات الخاطئة</b> مفصولة بفاصلة:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_Q_WRONG":
        wrongs = [w.strip() for w in text.split(',') if w.strip()]
        await db.questions.insert_one({"category": temp_data.get("q_cat"), "lesson": temp_data.get("q_les"), "question": temp_data.get("q_text"), "correct": temp_data.get("q_correct"), "wrong": wrongs, "correct_answers": 0, "wrong_answers": 0})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text("🎉 <b>تم حفظ السؤال بنجاح!</b>", parse_mode="HTML", reply_markup=kb)
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    try: await update.message.reply_text("الرجاء استخدام الأزرار المتاحة 👇", reply_markup=kb)
    except: pass

# ==========================================
# معالجة تفاعلات الأزرار المعزولة بالكامل (Stateless)
# ==========================================
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id, user_id = query.message.chat_id, str(query.from_user.id)
    if await check_spam(user_id): 
        try: await query.answer("الرجاء عدم التكرار السريع", show_alert=False)
        except: pass
        return
    
    data = query.data

    # 🌟 التفاعل مع مسابقات القناة 🌟
    if data.startswith("cq_"):
        parts = data.split("_")
        q_id, is_correct, expire_ts = parts[1], parts[2] == "1", int(parts[3])
        
        if int(time.time()) > expire_ts: return await query.answer("⏳ عذراً، انتهى الوقت المخصص لهذا السؤال!", show_alert=True)
        exists = await db.comp_answers.find_one({"user_id": user_id, "q_id": q_id})
        if exists: return await query.answer("لقد أجبت على هذا السؤال مسبقاً! ⚠️", show_alert=True)
        
        await db.comp_answers.insert_one({"user_id": user_id, "q_id": q_id, "correct": is_correct})
        u_name = query.from_user.first_name
        
        if is_correct:
            await db.users.update_one({"_id": user_id}, {"$inc": {"comp_score": 10}, "$set": {"name": u_name}}, upsert=True)
            return await query.answer("إجابة صحيحة! ✅ اكتسبت 10 نقاط.", show_alert=True)
        else:
            await db.users.update_one({"_id": user_id}, {"$set": {"name": u_name}}, upsert=True) 
            return await query.answer("إجابة خاطئة! ❌", show_alert=True)

    if data == "media_unavail": return await query.answer("⚠️ غير متوفر.", show_alert=True)
    if data == "ignore": 
        try: await query.answer()
        except: pass
        return 
        
    try: await query.answer()
    except: pass
    
    user = await db.users.find_one({"_id": user_id})

    # ================= 🌟 الأزرار الأساسية 🌟 =================
    if data == "admin_cancel":
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
        await safe_edit(query, "✅ <b>تم إلغاء العملية.</b>")
        return

    if data == "main_menu":
        pipeline = [{"$group": {"_id": "$category", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        try: cats = await db.library.aggregate(pipeline).to_list(length=None)
        except Exception as e: logging.error(f"Agg err: {e}"); cats = []
        btns = [[InlineKeyboardButton(f"📂 | {c['_id']}", callback_data=f"cat_{str(c['doc_id'])}")] for c in cats[:90] if c['_id'] and str(c['_id']).lower() != 'nan']
        await safe_edit(query, "📚 <b>المشروع القرآني:</b>\nيرجى اختيار السلسلة المطلوبة:", InlineKeyboardMarkup(btns))
        return

    if data == "admin_menu":
        adm = await get_admin_doc(user_id)
        if not adm: return
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}}, upsert=True)
        btns = []
        if await has_perm(user_id, "upload"): btns.append([InlineKeyboardButton("📂 | إدارة السلاسل والدروس", callback_data="admin_content_mgr")])
        if await has_perm(user_id, "publish"):
            btns.append([InlineKeyboardButton("📢 | قسم النشر والقوالب", callback_data="admin_publishing_hub")])
            btns.append([InlineKeyboardButton("🏆 | مسابقات القناة (جديد)", callback_data="admin_comp_menu")])
            btns.append([InlineKeyboardButton("🎛️ | إدارة أنواع المحتوى", callback_data="admin_content_types")])
        if await has_perm(user_id, "questions"): btns.append([InlineKeyboardButton("➕ | إضافة اختبار/سؤال لدرس", callback_data="admin_add_q")])
        if await has_perm(user_id, "stats"): btns.append([InlineKeyboardButton("📈 | الإحصائيات الشاملة", callback_data="admin_stats")])
        btns.append([InlineKeyboardButton("📥 | تصدير / استيراد قاعدة البيانات", callback_data="admin_import_export")])
        if await has_perm(user_id, "manage_admins"): btns.append([InlineKeyboardButton("👥 | إدارة المشرفين", callback_data="admin_manage")])
        btns.append([InlineKeyboardButton("❌ | إغلاق اللوحة", callback_data="admin_cancel")])
        await safe_edit(query, "⚙️ <b>لوحة التحكم والإدارة:</b>\nاختر الإجراء المطلوب:", InlineKeyboardMarkup(btns))
        return

    # ================= 🌟 الإحصائيات والاستيراد والتصدير 🌟 =================
    if data == "admin_stats" and await has_perm(user_id, "stats"):
        await safe_edit(query, "⏳ جاري تحليل البيانات وتجهيز تقرير الإحصائيات...")
        try:
            total_users = await db.users.count_documents({})
            active_users = await db.users.count_documents({"last_active": {"$gte": time.time() - 7*86400}})
            top_lessons = await db.lesson_stats.find().sort("views", -1).limit(50).to_list(length=None)
            pipeline = [{"$match": {"wrong_answers": {"$gt": 0}}}, {"$addFields": {"total_answers": {"$add": [{"$ifNull": ["$correct_answers", 0]}, "$wrong_answers"]}}}, {"$sort": {"wrong_answers": -1}}, {"$limit": 50}]
            top_wrong_qs = await db.questions.aggregate(pipeline).to_list(length=None)
            
            all_users = await db.users.find({}).to_list(length=None)
            users_list = []
            for u in all_users:
                last_active_ts = u.get("last_active", 0)
                last_active_str = datetime.datetime.fromtimestamp(last_active_ts).strftime('%Y-%m-%d %H:%M') if last_active_ts else "غير معروف"
                users_list.append({
                    "آيدي المستخدم": u.get("_id", ""),
                    "الاسم": u.get("name", "غير متوفر"),
                    "النقاط (المسابقات)": u.get("comp_score", 0),
                    "عدد الإجابات": len(u.get("answered", [])),
                    "آخر نشاط": last_active_str
                })
            df_users = pd.DataFrame(users_list)
            
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                pd.DataFrame([{"الطلاب المسجلين": total_users, "النشطين (آخر 7 أيام)": active_users, "التاريخ": time.strftime("%Y-%m-%d %H:%M:%S")}]).to_excel(writer, sheet_name='ملخص', index=False)
                if top_lessons: pd.DataFrame(top_lessons).rename(columns={"category": "السلسلة", "lesson": "الدرس", "views": "المشاهدات"})[["السلسلة", "الدرس", "المشاهدات"]].to_excel(writer, sheet_name='مشاهدات', index=False)
                if top_wrong_qs: pd.DataFrame([{"السلسلة": q.get("category", ""), "الدرس": q.get("lesson", ""), "السؤال": q.get("question", ""), "الخطأ": q.get("wrong_answers", 0)} for q in top_wrong_qs]).to_excel(writer, sheet_name='أخطاء', index=False)
                if not df_users.empty: df_users.to_excel(writer, sheet_name='قائمة_المستخدمين', index=False)
                
            output.seek(0)
            await query.message.delete()
            await context.bot.send_document(chat_id=chat_id, document=output, filename="تقرير_الإحصائيات.xlsx", caption="📊 <b>تقرير الإحصائيات وقائمة الطلاب</b>", parse_mode="HTML")
        except Exception as e: await context.bot.send_message(chat_id=chat_id, text=f"❌ حدث خطأ: {e}")
        return

    if data == "admin_import_export":
        btns = [
            [InlineKeyboardButton("📥 استيراد ومزامنة إكسل", callback_data="import_confirm")],
            [InlineKeyboardButton("📤 تصدير قاعدة البيانات", callback_data="export_db")],
            [InlineKeyboardButton("🔙 رجوع للوحة الإدارة", callback_data="admin_menu")]
        ]
        await safe_edit(query, "📥 <b>تصدير واستيراد البيانات:</b>\nاختر الإجراء المطلوب:", InlineKeyboardMarkup(btns))
        return
        
    if data == "export_db" and await has_perm(user_id, "stats"):
        await safe_edit(query, "⏳ جاري تجهيز ملف الإكسل (المكتبة والأسئلة)...")
        try:
            lib_data = await db.library.find({}).to_list(length=None)
            df_lib = pd.DataFrame(lib_data)
            if not df_lib.empty: df_lib = df_lib.rename(columns={"category": "السلسلة", "lesson": "المحاضرة /الدرس", "type": "النوع", "file_id": "الرابط"})[["المحاضرة /الدرس", "السلسلة", "النوع", "الرابط"]]
            else: df_lib = pd.DataFrame(columns=["المحاضرة /الدرس", "السلسلة", "النوع", "الرابط"])
            
            q_data = await db.questions.find({}).to_list(length=None)
            q_list = []
            for q in q_data:
                w_list = q.get("wrong") or []
                q_list.append({
                    "السلسلة": q.get("category", ""),
                    "المحاضرة /الدرس": q.get("lesson", ""),
                    "السؤال": q.get("question", ""),
                    "الإجابة_الصحيحة": q.get("correct", ""),
                    "خاطئة_1": w_list[0] if len(w_list) > 0 else "",
                    "خاطئة_2": w_list[1] if len(w_list) > 1 else ""
                })

            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df_lib.to_excel(writer, sheet_name='المشروع القرأني', index=False)
                pd.DataFrame(q_list).to_excel(writer, sheet_name='قيم_نفسك', index=False)

            output.seek(0)
            await query.message.delete()
            await context.bot.send_document(chat_id=chat_id, document=output, filename="قاعدة_بيانات_البوت.xlsx")
        except: pass
        return

    # ================= 🌟 مسابقات القناة 🌟 =================
    if data == "admin_comp_menu" and await has_perm(user_id, "publish"):
        btns = [
            [InlineKeyboardButton("🚀 | إرسال سؤال مسابقة للقناة", callback_data="comp_send_q")],
            [InlineKeyboardButton("📊 | لوحة الشرف (أعلى المتسابقين)", callback_data="comp_leaderboard")],
            [InlineKeyboardButton("🗑️ | تصفير نقاط المتسابقين", callback_data="comp_reset")],
            [InlineKeyboardButton("🔙 | رجوع للوحة الإدارة", callback_data="admin_menu")]
        ]
        await safe_edit(query, "🏆 <b>إدارة مسابقات القناة:</b>\nأضف التفاعل والمنافسة لقناتك بسهولة!", InlineKeyboardMarkup(btns))
        return

    if data == "comp_leaderboard":
        top_users = await db.users.find({"comp_score": {"$gt": 0}}).sort("comp_score", -1).limit(15).to_list(length=None)
        if not top_users: return await safe_edit(query, "⚠️ لا يوجد متسابقين بنقاط حتى الآن.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_comp_menu")]]))
        txt, medals = "📊 <b>لوحة الشرف لأعلى المتسابقين:</b>\n\n", ["🥇", "🥈", "🥉", "🏅", "🏅"]
        for idx, u in enumerate(top_users):
            medal = medals[idx] if idx < 5 else "👤"
            txt += f"{medal} <b>{html.escape(u.get('name', 'متسابق غير معروف'))}</b>: <code>{u.get('comp_score')}</code> نقطة\n"
        await safe_edit(query, txt, InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_comp_menu")]]))
        return

    if data == "comp_reset":
        await db.users.update_many({}, {"$set": {"comp_score": 0}})
        await db.comp_answers.delete_many({})
        await safe_edit(query, "✅ <b>تم تصفير جميع النقاط بنجاح!</b>", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_comp_menu")]]))
        return

    if data == "comp_send_q":
        pipeline = [{"$group": {"_id": "$category", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        cats = await db.library.aggregate(pipeline).to_list(length=None)
        btns = [[InlineKeyboardButton(f"📁 | {c['_id']}", callback_data=f"comp_cat_{str(c['doc_id'])}")] for c in cats[:90] if c['_id'] and str(c['_id']).lower() != 'nan']
        btns.append([InlineKeyboardButton("🔙 | تراجع", callback_data="admin_comp_menu")])
        await safe_edit(query, "🚀 <b>إرسال سؤال مسابقة:</b>\nاختر السلسلة التي تود أخذ السؤال منها:", InlineKeyboardMarkup(btns))
        return

    if data.startswith("comp_cat_"):
        oid = get_safe_oid(data.replace("comp_cat_", ""))
        if not oid: return await safe_edit(query, "⚠️ القائمة قديمة، يرجى تحديث النظام بإرسال /start.")
        doc = await db.library.find_one({"_id": oid})
        if not doc: return await safe_edit(query, "⚠️ عذراً، لم يعد هذا العنصر متوفراً.")
        cat_name = doc["category"]
        pipeline = [{"$match": {"category": cat_name}}, {"$group": {"_id": "$lesson", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        lessons = await db.library.aggregate(pipeline).to_list(length=None)
        btns = [[InlineKeyboardButton(f"📖 | {idx}- {les['_id']}", callback_data=f"comp_les_{str(les['doc_id'])}")] for idx, les in enumerate(lessons[:90], 1)]
        btns.append([InlineKeyboardButton("🔙 | تراجع", callback_data="comp_send_q")])
        await safe_edit(query, f"📁 السلسلة: <b>{html.escape(cat_name)}</b>\nاختر الدرس:", InlineKeyboardMarkup(btns))
        return

    if data.startswith("comp_les_"):
        oid = get_safe_oid(data.replace("comp_les_", ""))
        if not oid: return await safe_edit(query, "⚠️ القائمة قديمة، يرجى تحديث النظام بإرسال /start.")
        doc = await db.library.find_one({"_id": oid})
        if not doc: return await safe_edit(query, "⚠️ عذراً، لم يعد هذا العنصر متوفراً.")
        
        all_qs = await db.questions.find({"lesson": doc["lesson"]}).to_list(length=None)
        if not all_qs: return await safe_edit(query, "⚠️ لا توجد أسئلة مضافة لهذا الدرس!", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 تراجع", callback_data="comp_send_q")]]))
        q = random.choice(all_qs)
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_COMP_TIME", "temp_data": {"comp_q_id": str(q["_id"])}}}, upsert=True)
        
        await safe_edit(query, f"✅ تم اختيار سؤال عشوائي من درس ({html.escape(doc['lesson'])}).\n\n✍️ <b>أرسل الآن مدة المسابقة بالدقائق</b> (مثلاً: <code>60</code>):", InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    # ================= 🌟 إدارة المشرفين والصلاحيات 🌟 =================
    if data == "admin_manage" and await has_perm(user_id, "manage_admins"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}}, upsert=True)
        admins = await db.admins.find({}).to_list(length=None)
        btns = [[InlineKeyboardButton(f"👤 | تعديل المشرف ({adm['_id']})", callback_data=f"editadm_{adm['_id']}")] for adm in admins[:90]]
        btns.extend([[InlineKeyboardButton("➕ | إضافة مشرف جديد", callback_data="add_admin")], [InlineKeyboardButton("🔙 | رجوع", callback_data="admin_menu")]])
        await safe_edit(query, "👥 <b>إدارة المشرفين والصلاحيات:</b>\nانقر للتعديل:", InlineKeyboardMarkup(btns))
        return

    if data.startswith("editadm_") and await has_perm(user_id, "manage_admins"):
        target_id = data.replace("editadm_", "")
        adm_doc = await db.admins.find_one({"_id": target_id})
        if not adm_doc: return await safe_edit(query, "⚠️ لم يتم العثور على المشرف.")
        perms = adm_doc.get("permissions", {"upload": False, "questions": False, "publish": False, "stats": False, "manage_admins": False})
        await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": {"edit_admin_id": target_id, "admin_perms": perms}}}, upsert=True)
        await safe_edit(query, f"⚙️ <b>صلاحيات المشرف ({target_id}):</b>", get_perms_kb(perms, edit_mode=True, admin_id=target_id))
        return

    if data == "add_admin" and await has_perm(user_id, "manage_admins"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_ADMIN_ID"}}, upsert=True)
        await safe_edit(query, "✍️ أرسل <b>آيدي (ID)</b> المشرف الجديد:", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 تراجع", callback_data="admin_manage")]]))
        return

    if data.startswith("deladmin_") and await has_perm(user_id, "manage_admins"):
        adm_id = data.replace("deladmin_", "")
        await db.admins.delete_one({"_id": adm_id})
        await safe_edit(query, f"✅ تم سحب الصلاحيات نهائياً من ({adm_id}).", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_manage")]]))
        return

    if data.startswith("adm_tgl_") and await has_perm(user_id, "manage_admins"):
        perm_key = data.replace("adm_tgl_", "")
        temp_data = user.get("temp_data", {})
        perms = temp_data.get("admin_perms", {})
        perms[perm_key] = not perms.get(perm_key, False)
        temp_data["admin_perms"] = perms
        await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": temp_data}}, upsert=True)
        edit_id = temp_data.get("edit_admin_id")
        try: await query.edit_message_reply_markup(get_perms_kb(perms, edit_mode=bool(edit_id), admin_id=edit_id))
        except: pass
        return

    if data == "adm_save_new" and await has_perm(user_id, "manage_admins"):
        temp_data = user.get("temp_data", {})
        new_id = temp_data.get("new_admin_id")
        perms = temp_data.get("admin_perms", {})
        if new_id:
            await db.admins.update_one({"_id": new_id}, {"$set": {"added_at": time.time(), "permissions": perms}}, upsert=True)
            await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
            await safe_edit(query, f"✅ تم إضافة المشرف ({new_id}) بنجاح!", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_manage")]]))
        return

    if data.startswith("adm_save_") and data != "adm_save_new" and await has_perm(user_id, "manage_admins"):
        target_id = data.replace("adm_save_", "")
        perms = user.get("temp_data", {}).get("admin_perms", {})
        await db.admins.update_one({"_id": target_id}, {"$set": {"permissions": perms}}, upsert=True)
        await safe_edit(query, f"✅ تم تحديث الصلاحيات بنجاح!", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_manage")]]))
        return

    # ================= 🌟 رسائل التأكيد والتحذير 🌟 =================
    if data.startswith("ask_deltype_"):
        t_id = data.replace("ask_deltype_", "")
        btns = [[InlineKeyboardButton("✅ نعم، احذف نهائياً", callback_data=f"deltype_{t_id}")], [InlineKeyboardButton("❌ تراجع", callback_data="admin_content_types")]]
        await safe_edit(query, "⚠️ <b>تنبيه:</b>\nهل أنت متأكد من رغبتك في حذف هذا النوع؟\nلا يمكن التراجع عن هذه الخطوة.", InlineKeyboardMarkup(btns))
        return

    if data.startswith("ask_del_cat_"):
        oid = get_safe_oid(data.replace("ask_del_cat_", ""))
        if not oid: return await safe_edit(query, "⚠️ القائمة قديمة، يرجى تحديث النظام بإرسال /start.")
        doc = await db.library.find_one({"_id": oid})
        if not doc: return await safe_edit(query, "⚠️ عذراً، لم يعد هذا العنصر متوفراً.")
        btns = [[InlineKeyboardButton("✅ نعم، احذف السلسلة بالكامل", callback_data=f"mgr_del_cat_{str(oid)}")], [InlineKeyboardButton("❌ تراجع", callback_data=f"mgr_cat_view_{str(oid)}")]]
        await safe_edit(query, f"⚠️ <b>تحذير خطير:</b>\nهل أنت متأكد من حذف السلسلة (<b>{html.escape(doc['category'])}</b>)؟\n\n<i>سيتم مسح جميع الدروس والأسئلة المرتبطة بها نهائياً!</i>", InlineKeyboardMarkup(btns))
        return

    if data == "ask_del_les":
        cat, les, cat_id = user.get("temp_data", {}).get("mgr_target_cat"), user.get("temp_data", {}).get("mgr_target_les"), user.get("temp_data", {}).get("mgr_target_cat_id")
        btns = [[InlineKeyboardButton("✅ نعم، احذف الدرس نهائياً", callback_data="mgr_action_del_les")], [InlineKeyboardButton("❌ تراجع", callback_data=f"mgr_cat_view_{cat_id}")]]
        await safe_edit(query, f"⚠️ <b>تنبيه:</b>\nهل أنت متأكد من حذف الدرس (<b>{html.escape(les)}</b>)؟\n\n<i>سيتم مسح جميع روابطه وأسئلته من قاعدة البيانات!</i>", InlineKeyboardMarkup(btns))
        return

    # ================= 🌟 إدارة المحتوى المباشر للسلاسل والدروس 🌟 =================
    if data == "admin_content_mgr" and await has_perm(user_id, "upload"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}}, upsert=True)
        pipeline = [{"$group": {"_id": "$category", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        cats = await db.library.aggregate(pipeline).to_list(length=None)
        btns = [[InlineKeyboardButton(f"📁 | {c['_id']}", callback_data=f"mgr_cat_view_{str(c['doc_id'])}")] for c in cats[:90] if c['_id'] and str(c['_id']).lower() != 'nan']
        btns.append([InlineKeyboardButton("➕ | إضافة سلسلة جديدة", callback_data="mgr_add_cat")])
        btns.append([InlineKeyboardButton("🔙 | رجوع للوحة الإدارة", callback_data="admin_menu")])
        await safe_edit(query, "📂 <b>إدارة السلاسل والدروس:</b>\nاختر سلسلة لتعديلها أو إضافة دروس إليها:", InlineKeyboardMarkup(btns))
        return

    if data == "mgr_add_cat" and await has_perm(user_id, "upload"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_MGR_NEW_CAT"}}, upsert=True)
        await safe_edit(query, "✍️ أرسل اسم <b>السلسلة الجديدة</b>:\nسيتم إضافة درس افتراضي بداخلها لتأسيسها.", InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    if data.startswith("mgr_cat_view_"):
        oid = get_safe_oid(data.replace("mgr_cat_view_", ""))
        if not oid: return await safe_edit(query, "⚠️ القائمة قديمة، يرجى تحديث النظام بإرسال /start.")
        doc = await db.library.find_one({"_id": oid})
        if not doc: return await safe_edit(query, "⚠️ السلسلة فارغة أو تم حذفها.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_content_mgr")]]))
        cat_name = doc["category"]
        
        pipeline = [{"$match": {"category": cat_name}}, {"$group": {"_id": "$lesson", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        lessons = await db.library.aggregate(pipeline).to_list(length=None)
        
        btns = [[InlineKeyboardButton(f"📖 | {idx}- {les['_id']}", callback_data=f"mgr_les_{str(les['doc_id'])}")] for idx, les in enumerate(lessons[:90], 1)]
        btns.append([InlineKeyboardButton("➕ | إضافة درس جديد", callback_data=f"mgr_add_les_{str(oid)}")])
        btns.append([InlineKeyboardButton("✏️ | تعديل اسم السلسلة", callback_data=f"mgr_edit_cat_{str(oid)}")])
        btns.append([InlineKeyboardButton("🗑️ | حذف السلسلة (خطير)", callback_data=f"ask_del_cat_{str(oid)}")])
        btns.append([InlineKeyboardButton("🔙 | رجوع للسلاسل", callback_data="admin_content_mgr")])
        await safe_edit(query, f"📁 السلسلة: <b>{html.escape(cat_name)}</b>\nيمكنك إضافة دروس جديدة أو التعديل:", InlineKeyboardMarkup(btns))
        return

    if data.startswith("mgr_add_les_"):
        oid = get_safe_oid(data.replace("mgr_add_les_", ""))
        doc = await db.library.find_one({"_id": oid}) if oid else None
        if not doc: return await query.answer("عنصر غير موجود.", show_alert=True)
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_MGR_NEW_LES", "temp_data": {"mgr_target_cat": doc["category"], "mgr_target_cat_id": str(oid)}}}, upsert=True)
        await safe_edit(query, f"✍️ أرسل اسم <b>الدرس الجديد</b> للسلسلة ({html.escape(doc['category'])}):", InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    if data.startswith("mgr_edit_cat_"):
        oid = get_safe_oid(data.replace("mgr_edit_cat_", ""))
        doc = await db.library.find_one({"_id": oid}) if oid else None
        if not doc: return await query.answer("عنصر غير موجود.", show_alert=True)
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_MGR_EDIT_CAT", "temp_data": {"mgr_target_cat": doc["category"], "mgr_target_cat_id": str(oid)}}}, upsert=True)
        await safe_edit(query, f"✍️ أرسل <b>الاسم الجديد</b> بدلاً من ({html.escape(doc['category'])}):", InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    if data.startswith("mgr_del_cat_"):
        oid = get_safe_oid(data.replace("mgr_del_cat_", ""))
        doc = await db.library.find_one({"_id": oid}) if oid else None
        if doc:
            await db.library.delete_many({"category": doc["category"]})
            await db.questions.delete_many({"category": doc["category"]})
        await safe_edit(query, f"✅ تم حذف السلسلة بالكامل!", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_content_mgr")]]))
        return

    if data.startswith("mgr_les_"):
        oid = get_safe_oid(data.replace("mgr_les_", ""))
        if not oid: return await safe_edit(query, "⚠️ القائمة قديمة، يرجى تحديث النظام بإرسال /start.")
        doc = await db.library.find_one({"_id": oid})
        if not doc: return await query.answer("الدرس غير موجود", show_alert=True)
        
        cat_doc = await db.library.find_one({"category": doc["category"]})
        cat_id = str(cat_doc["_id"]) if cat_doc else str(oid)
        
        await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": {"mgr_target_cat": doc["category"], "mgr_target_les": doc["lesson"], "mgr_target_cat_id": cat_id, "mgr_target_les_id": str(oid)}}}, upsert=True)
        
        btns = [
            [InlineKeyboardButton("🔗 | إرفاق محتوى جديد بالدرس (نص/ملف)", callback_data="mgr_attach_content")],
            [InlineKeyboardButton("✏️ | تعديل اسم الدرس", callback_data="mgr_action_edit_les")],
            [InlineKeyboardButton("🗑️ | حذف الدرس", callback_data="ask_del_les")],
            [InlineKeyboardButton("🔙 | رجوع لدروس السلسلة", callback_data=f"mgr_cat_view_{cat_id}")]
        ]
        await safe_edit(query, f"📖 الدرس: <b>{html.escape(doc['lesson'])}</b>\nماذا تريد أن تفعل؟", InlineKeyboardMarkup(btns))
        return
        
    if data == "mgr_attach_content":
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_CONTENT"}}, upsert=True)
        msg = "🔗 <b>إرفاق محتوى للدرس:</b>\n\nأرسل الآن المحتوى الذي تريده (سواء كان <b>نصاً طويلاً</b>، أو صورة، أو ملف، أو مجرد رابط). وسيقوم البوت بربطه بالدرس مباشرة."
        await safe_edit(query, msg, InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    if data == "mgr_action_edit_les":
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_MGR_EDIT_LES"}}, upsert=True)
        les = user.get("temp_data", {}).get("mgr_target_les")
        await safe_edit(query, f"✍️ أرسل <b>الاسم الجديد</b> للدرس بدلاً من ({html.escape(les)}):", InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    if data == "mgr_action_del_les":
        cat, les, cat_id = user.get("temp_data", {}).get("mgr_target_cat"), user.get("temp_data", {}).get("mgr_target_les"), user.get("temp_data", {}).get("mgr_target_cat_id")
        await db.library.delete_many({"category": cat, "lesson": les})
        await db.questions.delete_many({"category": cat, "lesson": les})
        await safe_edit(query, f"✅ تم حذف الدرس ({html.escape(les)}) بالكامل!", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data=f"mgr_cat_view_{cat_id}")]]))
        return

    if data == "admin_content_types" and await has_perm(user_id, "publish"):
        types = await db.content_types.find({}).to_list(length=None)
        btns = []
        for t in types:
            btns.append([
                InlineKeyboardButton(f"{t['icon']} {t['name']}", callback_data="ignore"),
                InlineKeyboardButton("✏️ تعديل", callback_data=f"editype_{t['_id']}"),
                InlineKeyboardButton("🗑️ حذف", callback_data=f"ask_deltype_{t['_id']}")
            ])
        btns.append([InlineKeyboardButton("➕ | إضافة نوع جديد", callback_data="add_type")])
        btns.append([InlineKeyboardButton("🔙 | رجوع للوحة", callback_data="admin_menu")])
        await safe_edit(query, "🎛️ <b>إدارة أنواع المحتوى (الأزرار):</b>\nقم بإضافة أو تعديل الأزرار التي ستظهر للطلاب:", InlineKeyboardMarkup(btns))
        return
    
    if data == "add_type":
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_TYPE_DATA"}}, upsert=True)
        await safe_edit(query, "✍️ أرسل <b>الاسم, الأيقونة</b> للنوع الجديد مفصولة بفاصلة\n(مثال: <code>بودكاست, 🎙️</code>):", InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    if data.startswith("deltype_"):
        t_id = data.replace("deltype_", "")
        await db.content_types.delete_one({"_id": t_id})
        await safe_edit(query, "✅ تم الحذف بنجاح!", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_content_types")]]))
        return

    if data.startswith("editype_"):
        t_id = data.replace("editype_", "")
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_EDIT_TYPE", "temp_data": {"edit_t_id": t_id}}}, upsert=True)
        await safe_edit(query, "✍️ أرسل <b>الاسم الجديد, الأيقونة الجديدة</b> مفصولة بفاصلة\n(مثال: <code>الكتاب الشامل, 📖</code>):", InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    if data == "admin_publishing_hub" and await has_perm(user_id, "publish"):
        btns = [
            [InlineKeyboardButton("🚀 | نشر درس للقناة", callback_data="admin_pub_menu")],
            [InlineKeyboardButton("📡 | إدارة قنوات النشر", callback_data="admin_channels")],
            [InlineKeyboardButton("🎨 | إدارة قوالب النشر", callback_data="admin_tpl_menu")],
            [InlineKeyboardButton("🔗 | تعديل تذييل النشر", callback_data="admin_edit_footer")],
            [InlineKeyboardButton("📊 | إنشاء استفتاء للقناة", callback_data="admin_poll")],
            [InlineKeyboardButton("🔙 | رجوع للوحة الإدارة", callback_data="admin_menu")]
        ]
        await safe_edit(query, "📢 <b>قسم النشر والقوالب:</b>\nجميع أدوات النشر وتخصيص القوالب في مكان واحد:", InlineKeyboardMarkup(btns))
        return

    if data == "admin_poll" and await has_perm(user_id, "publish"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_POLL_Q"}}, upsert=True)
        await safe_edit(query, "📊 أرسل الآن <b>سؤال الاستفتاء</b>:", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 تراجع", callback_data="admin_publishing_hub")]]))
        return

    if data == "admin_channels" and await has_perm(user_id, "publish"):
        channels_doc = await db.settings.find_one({"_id": "channels"})
        channels = channels_doc.get("list", []) if channels_doc else []
        btns = []
        for ch in channels:
            btns.append([InlineKeyboardButton(f"🗑️ حذف ({ch})", callback_data=f"delchan_{ch}")])
        btns.append([InlineKeyboardButton("➕ | إضافة قناة جديدة", callback_data="add_chan")])
        btns.append([InlineKeyboardButton("🔙 | رجوع لقسم النشر", callback_data="admin_publishing_hub")])
        await safe_edit(query, "📡 <b>إدارة قنوات النشر المتعددة:</b>\nأضف القنوات هنا لتتمكن من اختيارها عند النشر:", InlineKeyboardMarkup(btns))
        return

    if data == "add_chan" and await has_perm(user_id, "publish"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_CHAN_ID"}}, upsert=True)
        await safe_edit(query, "✍️ أرسل معرّف القناة (مثال: `@almashro` أو `-100123456`):", InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    if data.startswith("delchan_") and await has_perm(user_id, "publish"):
        ch = data.replace("delchan_", "")
        await db.settings.update_one({"_id": "channels"}, {"$pull": {"list": ch}})
        await safe_edit(query, f"✅ تم حذف القناة ({ch}) بنجاح!", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_channels")]]))
        return

    if data == "admin_tpl_menu" and await has_perm(user_id, "publish"):
        templates = await db.templates.find({}).to_list(length=None)
        btns = []
        for t in templates:
            btns.append([InlineKeyboardButton(f"📄 | {t['name']}", callback_data="ignore")])
            btns.append([InlineKeyboardButton("🗑️ حذف هذا القالب", callback_data=f"deltpl_{str(t['_id'])}")])
        btns.append([InlineKeyboardButton("➕ | إنشاء قالب جديد", callback_data="add_tpl")])
        btns.append([InlineKeyboardButton("🔙 | رجوع لقسم النشر", callback_data="admin_publishing_hub")])
        await safe_edit(query, "🎨 <b>إدارة قوالب النشر الديناميكية:</b>\nأنشئ قوالبك بمتغيرات ذكية:", InlineKeyboardMarkup(btns))
        return

    if data == "add_tpl" and await has_perm(user_id, "publish"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_TPL_NAME"}}, upsert=True)
        await safe_edit(query, "✍️ أرسل الآن <b>اسم القالب الجديد</b>\n(مثال: قالب خطب الجمعة، قالب السيرة):", InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    if data.startswith("deltpl_") and await has_perm(user_id, "publish"):
        tpl_id = data.replace("deltpl_", "")
        await db.templates.delete_one({"_id": ObjectId(tpl_id)})
        await safe_edit(query, "✅ تم حذف القالب بنجاح!", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع للقوالب", callback_data="admin_tpl_menu")]]))
        return

    if data == "admin_edit_footer" and await has_perm(user_id, "publish"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_FOOTER_TEXT"}}, upsert=True)
        msg = "✍️ أرسل الآن <b>النص مع الرابط</b> الذي تريده أن يظهر كـ (تذييل) أسفل الدروس المنشورة:\n\n<i>(الوضع الافتراضي الحالي سيكون هو النص القديم إذا لم تقم بتعديله)</i>"
        await safe_edit(query, msg, InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    if data == "admin_pub_menu" and await has_perm(user_id, "publish"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}}, upsert=True)
        pipeline = [{"$group": {"_id": "$category", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        cats = await db.library.aggregate(pipeline).to_list(length=None)
        btns = [[InlineKeyboardButton(f"📁 | {c['_id']}", callback_data=f"pubc_{str(c['doc_id'])}")] for c in cats[:90] if c['_id'] and str(c['_id']).lower() != 'nan']
        btns.append([InlineKeyboardButton("🔙 | رجوع لقسم النشر", callback_data="admin_publishing_hub")])
        await safe_edit(query, "📢 <b>نشر درس:</b>\nاختر السلسلة التي تود نشر درس منها:", InlineKeyboardMarkup(btns))
        return

    if data.startswith("pubc_"):
        oid = get_safe_oid(data.replace("pubc_", ""))
        if not oid: return await safe_edit(query, "⚠️ القائمة قديمة، يرجى تحديث النظام بإرسال /start.")
        doc = await db.library.find_one({"_id": oid})
        if not doc: return await safe_edit(query, "⚠️ عذراً، لم يعد هذا العنصر متوفراً.")
        cat_name = doc["category"]
        pipeline = [{"$match": {"category": cat_name}}, {"$group": {"_id": "$lesson", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        lessons = await db.library.aggregate(pipeline).to_list(length=None)
        btns = [[InlineKeyboardButton(f"📖 | {idx}- {les['_id']}", callback_data=f"publ_{str(les['doc_id'])}")] for idx, les in enumerate(lessons[:90], 1)]
        btns.append([InlineKeyboardButton("🔙 | تراجع", callback_data="admin_pub_menu")])
        await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": {"pub_cat": cat_name, "pub_cat_id": str(oid)}}}, upsert=True)
        await safe_edit(query, f"📁 السلسلة: <b>{html.escape(cat_name)}</b>\nاختر الدرس المراد نشره:", InlineKeyboardMarkup(btns))
        return

    if data.startswith("publ_"):
        oid = get_safe_oid(data.replace("publ_", ""))
        if not oid: return await safe_edit(query, "⚠️ القائمة قديمة، يرجى تحديث النظام بإرسال /start.")
        doc = await db.library.find_one({"_id": oid})
        if not doc: return await safe_edit(query, "⚠️ عذراً، لم يعد هذا العنصر متوفراً.")
        
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {"pub_les": doc["lesson"]}}}, upsert=True)
        
        templates = await db.templates.find({}).to_list(length=None)
        btns = []
        for t in templates:
            btns.append([InlineKeyboardButton(f"📄 | {t['name']}", callback_data=f"pubfmt_tpl_{str(t['_id'])}")])
        btns.append([InlineKeyboardButton("📝 | قالب النص الكلاسيكي", callback_data="pubfmt_text")])
        btns.append([InlineKeyboardButton("🔲 | قالب الأزرار الشفافة", callback_data="pubfmt_btns")])
        btns.append([InlineKeyboardButton("❌ | إلغاء العملية", callback_data="admin_cancel")])
        await safe_edit(query, f"✅ تم اختيار: <b>{html.escape(doc['lesson'])}</b>\n\nاختر القالب الذي تفضله لتوليد المنشور:", InlineKeyboardMarkup(btns))
        return

    if data.startswith("pubfmt_"):
        fmt_type = data.replace("pubfmt_", "")
        temp_data = user.get("temp_data", {})
        temp_data["draft_format_key"] = fmt_type
        await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": temp_data}}, upsert=True)

        items = await db.library.find({"lesson": temp_data.get("pub_les", "عام")}).to_list(length=None)
        has_media = any(fix_link(item.get("file_id")) for item in items)
        
        if has_media:
            btns = [
                [InlineKeyboardButton("🖼️/🎬 إرفاق وسائط من الدرس", callback_data="pubmed_auto")],
                [InlineKeyboardButton("📝 نص فقط (بدون وسائط)", callback_data="pubmed_none")],
                [InlineKeyboardButton("❌ إلغاء العملية", callback_data="admin_cancel")]
            ]
            await safe_edit(query, "🎨 <b>تصميم المنشور:</b>\nهل تود إرفاق وسائط (صورة/فيديو) مع هذا المنشور لجعله أكثر جاذبية؟", InlineKeyboardMarkup(btns))
            return
        else: data = "pubmed_none" 

    if data.startswith("pubmed_"):
        media_choice = data.replace("pubmed_", "")
        temp_data = user.get("temp_data", {})
        temp_data["pub_media"] = media_choice
        fmt_type = temp_data.get("draft_format_key", "text")
        
        cat, les = temp_data.get("pub_cat", "عام"), temp_data.get("pub_les", "عام")
        date_txt, footer_content = get_auto_arabic_date(), await get_footer_text()

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
                        if media_choice == "auto" and not media_candidate: media_candidate = safe_link 
        
        temp_data["link_auto_media"] = media_candidate if media_choice == "auto" else None
        media_note = "📌 <code>[سيتم إرفاق وسائط الدرس إن وجدت]</code>\n\n" if media_choice == "auto" else ""

        draft_text = ""
        if fmt_type == "btns":
            draft_text = f"{cat} - {les}\n\nدرس اليوم {date_txt}\n\n{footer_content}"
        elif fmt_type == "text":
            draft_text = f"<b>{html.escape(cat)} - {html.escape(les)}</b>\n\nدرس اليوم {date_txt}\n\n"
            for t_name, t_link in dynamic_links.items():
                if t_link != ch_link: draft_text += f"<blockquote>{html.escape(t_name)} <a href='{t_link}'>إضغط هنا</a> ❞</blockquote>\n"
            draft_text += f"\n\n{html.escape(footer_content)}"
        elif fmt_type.startswith("tpl_"):
            tpl_id = fmt_type.replace("tpl_", "")
            tpl_doc = await db.templates.find_one({"_id": ObjectId(tpl_id)})
            if not tpl_doc: return await safe_edit(query, "⚠️ القالب غير موجود.")
            draft_text = tpl_doc["content"].replace("{سلسلة}", html.escape(cat)).replace("{درس}", html.escape(les)).replace("{تاريخ}", date_txt).replace("{تذييل}", html.escape(footer_content))
            for t_name, t_link in dynamic_links.items(): draft_text = draft_text.replace(f"{{{t_name}}}", t_link)

        temp_data["draft_format"] = "btns" if fmt_type == "btns" else ("html_text" if fmt_type == "text" else "html_dynamic")
        temp_data["draft_text"] = draft_text
        await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": temp_data}}, upsert=True)
        
        btns = [[InlineKeyboardButton("✅ | المتابعة لاختيار القناة", callback_data="pub_select_chan")], [InlineKeyboardButton("❌ | إلغاء", callback_data="admin_cancel")]]
        try:
            if fmt_type == "btns": await safe_edit(query, f"🔲 <b>معاينة المسودة (أزرار):</b>\n\n{media_note}{html.escape(draft_text)}", InlineKeyboardMarkup(btns))
            else: await safe_edit(query, f"📝 <b>معاينة المسودة:</b>\n\n{media_note}{draft_text}\n\n--- \nهل تريد المتابعة؟", InlineKeyboardMarkup(btns))
        except Exception as e: await safe_edit(query, f"❌ **خطأ في كود HTML للقالب!**\n\n<code>{e}</code>", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_tpl_menu")]]))
        return

    if data == "pub_select_chan":
        channels_doc = await db.settings.find_one({"_id": "channels"})
        channels = channels_doc.get("list", []) if channels_doc else []
        if CHANNEL_ID and CHANNEL_ID not in channels: channels.insert(0, CHANNEL_ID)
        
        btns = [[InlineKeyboardButton(f"📡 انشر في: {ch}", callback_data=f"pconf_{ch}")] for ch in channels]
        btns.append([InlineKeyboardButton("➕ إضافة قناة جديدة", callback_data="admin_channels")])
        btns.append([InlineKeyboardButton("🔙 تراجع للمسودة", callback_data="admin_pub_menu")])
        await safe_edit(query, "اختر <b>القناة</b> التي تريد النشر فيها الآن:", InlineKeyboardMarkup(btns))
        return

    if data.startswith("pconf_"):
        target_channel = data.replace("pconf_", "")
        temp_data = user.get("temp_data", {})
        draft_text, draft_format, media_link = temp_data.get("draft_text", ""), temp_data.get("draft_format", ""), temp_data.get("link_auto_media")
        
        inline_kb = None
        if draft_format == "btns":
            les = temp_data.get("pub_les", "")
            items = await db.library.find({"lesson": les}).to_list(length=None)
            types_docs = await db.content_types.find({}).to_list(length=None)
            inline_kb_arr, row = [], []
            for item in items:
                safe_link = fix_link(item.get("file_id"))
                t_name = next((t["name"] for t in types_docs if t["_id"] == str(item.get("type", ""))), "رابط")
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

            await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": {}}}, upsert=True)
            btns = [[InlineKeyboardButton("🔙 | العودة لقسم النشر", callback_data="admin_publishing_hub")]]
            
            if media_failed:
                await safe_edit(query, f"✅ <b>تم نشر النص في ({target_channel})!</b>\n\n⚠️ <i>ملاحظة:</i> لم يتم إرفاق الوسائط لأن الرابط يشير لرسالة محذوفة.", InlineKeyboardMarkup(btns))
            else:
                await safe_edit(query, f"🎉 <b>تم النشر بنجاح في ({target_channel})!</b>", InlineKeyboardMarkup(btns))
            return
        except Exception as e: 
            await safe_edit(query, f"❌ حدث خطأ.\nتأكد أن البوت (مشرف) في القناة المقصودة وأن المعرف صحيح.\n<code>{e}</code>", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 | العودة للوحة الإدارة", callback_data="admin_menu")]]))
            return

    # ================= 🌟 إضافة وتحديث أسئلة الدروس 🌟 =================
    if data == "admin_add_q" and await has_perm(user_id, "questions"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_Q_CAT"}}, upsert=True)
        pipeline = [{"$group": {"_id": "$category", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        cats = await db.library.aggregate(pipeline).to_list(length=None)
        btns = [[InlineKeyboardButton(f"📁 | {c['_id']}", callback_data=f"qaddc_{str(c['doc_id'])}")] for c in cats[:90] if c['_id'] and str(c['_id']).lower() != 'nan']
        btns.append([InlineKeyboardButton("🔙 | رجوع للوحة الإدارة", callback_data="admin_menu")])
        await safe_edit(query, "📝 <b>إضافة سؤال/اختبار:</b>\nاختر السلسلة:", InlineKeyboardMarkup(btns))
        return

    if data.startswith("qaddc_"):
        oid = get_safe_oid(data.replace("qaddc_", ""))
        if not oid: return await safe_edit(query, "⚠️ القائمة قديمة، يرجى تحديث النظام بإرسال /start.")
        doc = await db.library.find_one({"_id": oid})
        if not doc: return await safe_edit(query, "⚠️ عذراً، لم يعد هذا العنصر متوفراً.")
        cat_name = doc["category"]
        pipeline = [{"$match": {"category": cat_name}}, {"$group": {"_id": "$lesson", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        lessons = await db.library.aggregate(pipeline).to_list(length=None)
        btns = [[InlineKeyboardButton(f"📖 | {idx}- {les['_id']}", callback_data=f"qaddl_{str(les['doc_id'])}")] for idx, les in enumerate(lessons[:90], 1)]
        btns.append([InlineKeyboardButton("🔙 | تراجع", callback_data="admin_add_q")])
        await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": {"q_cat": cat_name}}}, upsert=True)
        await safe_edit(query, f"📁 السلسلة: <b>{html.escape(cat_name)}</b>\nاختر الدرس:", InlineKeyboardMarkup(btns))
        return

    if data.startswith("qaddl_"):
        oid = get_safe_oid(data.replace("qaddl_", ""))
        if not oid: return await safe_edit(query, "⚠️ القائمة قديمة، يرجى تحديث النظام بإرسال /start.")
        doc = await db.library.find_one({"_id": oid})
        if not doc: return await safe_edit(query, "⚠️ عذراً، لم يعد هذا العنصر متوفراً.")
        lesson_name = doc["lesson"]
        
        temp_data = user.get("temp_data", {})
        temp_data["q_les"] = lesson_name
        await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": temp_data}}, upsert=True)
        
        btns = [
            [InlineKeyboardButton("✍️ إضافة سؤال واحد (يدوياً)", callback_data="qadd_manual")],
            [InlineKeyboardButton("📥 رفع اختبار كامل (ملف إكسل)", callback_data="qadd_excel")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]
        ]
        await safe_edit(query, f"📖 المحاضرة: <b>{html.escape(lesson_name)}</b>\n\nكيف تود إضافة الأسئلة؟", InlineKeyboardMarkup(btns))
        return

    if data == "qadd_manual":
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_Q_TEXT"}}, upsert=True)
        await safe_edit(query, "✍️ أرسل <b>نص السؤال</b>:", InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    if data == "qadd_excel":
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_Q_EXCEL"}}, upsert=True)
        msg = """📥 <b>رفع ملف إكسل لاختبار الدرس</b>

⚠️ <b>لتجنب أي أخطاء أثناء الرفع، يرجى تجهيز الملف كالتالي:</b>
1. يجب أن يكون الملف بصيغة <b>Excel (.xlsx)</b>.
2. يجب أن يحتوي <b>الصف الأول</b> على أسماء الأعمدة التالية بدقة:
   ▫️ <code>السؤال</code> : لكتابة نص السؤال.
   ▫️ <code>صحيح</code> : لكتابة الإجابة الصحيحة.
   ▫️ <code>خاطئة</code> أو <code>خطأ</code> : لكتابة الإجابات الخاطئة.

💡 <i>طريقة حذف سؤال:</i> اكتب نص السؤال، واكتب كلمة <code>حذف</code> في عمود "صحيح".

👇 <b>أرسل ملف الإكسل الآن كـ (مستند / Document):</b>"""
        await safe_edit(query, msg, InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    if data.startswith("cat_"):
        oid = get_safe_oid(data.replace("cat_", ""))
        if not oid: return await safe_edit(query, "⚠️ القائمة قديمة، يرجى تحديث النظام بإرسال /start.")
        doc = await db.library.find_one({"_id": oid})
        if not doc: return await safe_edit(query, "⚠️ عذراً، لم يعد هذا العنصر متوفراً.")
        cat_name = doc["category"]
        pipeline = [{"$match": {"category": cat_name}}, {"$group": {"_id": "$lesson", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        lessons = await db.library.aggregate(pipeline).to_list(length=None)
        btns = [[InlineKeyboardButton(f"📖 | {idx}- {les['_id']}", callback_data=f"les_{str(les['doc_id'])}")] for idx, les in enumerate(lessons[:90], 1)]
        btns.append([InlineKeyboardButton("🔙 | العودة للرئيسية", callback_data="main_menu")])
        await safe_edit(query, f"📂 <b>السلسلة:</b> {html.escape(cat_name)}\nاختر المحاضرة المطلوب:", InlineKeyboardMarkup(btns))
        return

    if data.startswith("les_"):
        doc_id = data.replace("les_", "")
        return await show_lesson_ui(context, chat_id, doc_id, message_id=query.message.message_id, user_id=user_id)

    if data.startswith("quizles_"):
        try: await context.bot.answer_callback_query(query.id, "🚀 جاري التجهيز...", show_alert=False)
        except: pass
        doc_id = data.replace("quizles_", "")
        oid = get_safe_oid(doc_id)
        if not oid: return await safe_edit(query, "⚠️ القائمة قديمة، يرجى تحديث النظام بإرسال /start.")
        doc = await db.library.find_one({"_id": oid})
        if not doc: return
        return await send_question(context, chat_id, lesson=doc.get("lesson"), user_id=user_id, msg_id=query.message.message_id, back_doc_id=doc_id)

    if data.startswith("ans_"):
        parts = data.split("_")
        is_correct = parts[1] == "1"
        q_id, ts = parts[2], int(parts[3])
        if int(time.time()) - ts > TIME_LIMIT or int(time.time()) - ts < 0: 
            await safe_edit(query, "⏳ <i>انتهى الوقت المخصص للإجابة!</i>")
            return
            
        new_kb = []
        for row in query.message.reply_markup.inline_keyboard[:-1]:
            new_row = []
            for b in row:
                if b.callback_data == data: new_row.append(InlineKeyboardButton(b.text + (" ✅" if is_correct else " ❌"), callback_data="ignore"))
                else: new_row.append(InlineKeyboardButton(b.text, callback_data="ignore"))
            new_kb.append(new_row)
            
        q_doc = await db.questions.find_one({"_id": ObjectId(q_id)})
        les_id = None
        if q_doc:
            lib_doc = await db.library.find_one({"lesson": q_doc["lesson"]})
            if lib_doc: les_id = str(lib_doc["_id"])
            
        if les_id:
            new_kb.append([InlineKeyboardButton("⏭️ السؤال التالي", callback_data=f"quizles_{les_id}")])
            new_kb.append([InlineKeyboardButton("🔙 إنهاء الاختبار", callback_data=f"les_{les_id}"), InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu")])
        else:
            new_kb.append(query.message.reply_markup.inline_keyboard[-1])
            
        await query.edit_message_reply_markup(InlineKeyboardMarkup(new_kb))
        asyncio.create_task(background_db_update(user_id, q_id=q_id, is_correct=is_correct))
        return

async def send_question(context, chat_id, lesson, user_id=None, msg_id=None, back_doc_id=None):
    if db is None: return
    user = await db.users.find_one({"_id": str(user_id)})
    answered = user.get("answered", []) if user else []
    all_qs = await db.questions.find({"lesson": lesson}).to_list(length=None)
    available = [q for q in all_qs if str(q['_id']) not in answered]
    
    if not available:
        txt = "🎉 <b>أتممت جميع أسئلة هذا الدرس بنجاح!</b>"
        btns = []
        if back_doc_id: btns.append([InlineKeyboardButton("🔙 | العودة للدرس", callback_data=f"les_{back_doc_id}")])
        btns.append([InlineKeyboardButton("🏠 | الرئيسية", callback_data="main_menu")])
        if msg_id: await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=msg_id, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
        else: 
            if user_id: await clean_chat_history(user_id, chat_id, context)
            sent_msg = await context.bot.send_message(chat_id, txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
            if user_id: await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
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
    txt = f"📖 <b>المحاضرة:</b> {html.escape(lesson)}\n\n❓ <i>{html.escape(q['question'])}</i>\n\n⏱️ أمامك {TIME_LIMIT} ثانية للإجابة!"
    
    if msg_id: await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=msg_id, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_kb))
    else: 
        if user_id: await clean_chat_history(user_id, chat_id, context)
        sent_msg = await context.bot.send_message(chat_id, txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_kb))
        if user_id: await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)

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
    return {"status": "ok"}    if len(parts) >= 3 and parts[-3] == 'c': chat_id = f"-100{parts[-2]}"
    else:
        chat_id_str = parts[-2]
        chat_id = chat_id_str if chat_id_str.startswith("-100") else (f"-100{chat_id_str}" if chat_id_str.isdigit() else f"@{chat_id_str}")
    return chat_id, msg_id

def fix_link(raw_link):
    if not raw_link or str(raw_link).strip().lower() in ['', 'nan', 'none', 'null', 'لا يوجد']: return None
    raw_str = str(raw_link).strip().replace(" ", "")
    if raw_str.startswith("http"): return raw_str
    if raw_str.startswith("t.me/"): return f"https://{raw_str}"
    if raw_str.isdigit():
        ch_name = CHANNEL_ID.replace('@', '').replace('https://t.me/', '')
        return f"https://t.me/{ch_name}/{raw_str}"
    if "/" in raw_str: return f"https://t.me/{raw_str}"
    return raw_str

async def get_admin_doc(user_id: str):
    if str(user_id) == OWNER_ID: 
        return {"_id": OWNER_ID, "permissions": {"upload": True, "questions": True, "publish": True, "stats": True, "manage_admins": True}}
    if db is not None: return await db.admins.find_one({"_id": str(user_id)})
    return None

async def has_perm(user_id: str, perm: str) -> bool:
    if str(user_id) == OWNER_ID: return True
    adm = await get_admin_doc(user_id)
    if adm and adm.get("permissions", {}).get(perm, False): return True
    return False

def get_perms_kb(perms, edit_mode=False, admin_id=None):
    def mk_btn(text, key): return InlineKeyboardButton(f"{'✅' if perms.get(key) else '❌'} | {text}", callback_data=f"adm_tgl_{key}")
    kb = [
        [mk_btn("إدارة السلاسل والمحتوى", "upload")], 
        [mk_btn("الأسئلة والاختبارات", "questions")], 
        [mk_btn("النشر والمسابقات", "publish")], 
        [mk_btn("الإحصائيات والتصدير", "stats")],
        [mk_btn("إدارة المشرفين", "manage_admins")]
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
    return ReplyKeyboardMarkup([["🔍 اعرف الله", "⚙️ لوحة الإدارة"]], resize_keyboard=True)

async def get_type_keyboard():
    types = await db.content_types.find({}).to_list(length=None)
    kb, row = [], []
    for t in types:
        row.append(InlineKeyboardButton(f"{t['icon']} {t['name']}", callback_data=f"utype_{t['_id']}"))
        if len(row) == 2: kb.append(row); row = []
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

async def safe_edit(query, text, markup=None):
    try: await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
    except Exception as e:
        if "Message is not modified" not in str(e): logging.error(f"Edit msg error: {e}")

async def show_lesson_ui(context, chat_id, doc_id, message_id=None, user_id=None):
    if db is None: return
    oid = get_safe_oid(doc_id)
    if not oid:
        txt = "⚠️ القائمة قديمة، يرجى تحديث النظام بإرسال /start."
        if message_id: 
            try: await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=message_id, parse_mode="HTML")
            except: pass
        else: await context.bot.send_message(chat_id, txt, parse_mode="HTML")
        return
        
    doc = await db.library.find_one({"_id": oid})
    if not doc:
        txt = "⚠️ عذراً، هذا الدرس تم حذفه ولم يعد متوفراً."
        if message_id: 
            try: await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=message_id, parse_mode="HTML")
            except: pass
        else: await context.bot.send_message(chat_id, txt, parse_mode="HTML")
        return

    lesson_title, series = doc.get("lesson", "بدون عنوان"), doc.get("category", "عام")
    if user_id: asyncio.create_task(background_db_update(user_id, lesson_view=lesson_title, cat_view=series))
    
    items = await db.library.find({"lesson": lesson_title}).to_list(length=None)
    types_docs = await db.content_types.find({}).to_list(length=None)
    types_dict = {t["_id"]: t for t in types_docs}
    
    links = {}
    for item in items:
        f_type = str(item.get("type", ""))
        safe_link = fix_link(item.get("file_id"))
        if safe_link: links[f_type] = safe_link

    def make_btn(text, link): return InlineKeyboardButton(text, url=link) if link else InlineKeyboardButton(text, callback_data="media_unavail")

    btns, row = [], []
    for t_id, t_info in types_dict.items():
        row.append(make_btn(f"{t_info['icon']} {t_info['name']}", links.get(t_id)))
        if len(row) == 2: btns.append(row); row = []
    if row: btns.append(row)

    cat_doc = await db.library.find_one({"category": series})
    cat_id = str(cat_doc["_id"]) if cat_doc else str(doc_id)

    btns.append([InlineKeyboardButton("✨ 📝 قيم نفسك ✨", callback_data=f"quizles_{doc_id}")])
    share_url = f"https://t.me/share/url?text=📚 إليك هذا الدرس القيم: {lesson_title}\n&url=https://t.me/{context.bot.username}?start=les_{doc_id}"
    btns.append([InlineKeyboardButton("🔗 شارك هذا الدرس (لتعم الفائدة)", url=share_url)])
    btns.append([InlineKeyboardButton("🔙 السابق", callback_data=f"cat_{cat_id}"), InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu")])

    txt = f"📖 <b>{html.escape(lesson_title)}</b>\n📂 السلسلة: <b>{html.escape(series)}</b>\n\n👇 اختر المحتوى للانتقال إليه:"
    try:
        if message_id: await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=message_id, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
        else: 
            if user_id: await clean_chat_history(user_id, chat_id, context)
            sent_msg = await context.bot.send_message(chat_id, txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
            if user_id: await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}})
    except: pass

async def handle_media_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id, chat_id = str(update.effective_user.id), update.effective_chat.id
    msg = update.message
    user = await db.users.find_one({"_id": user_id})
    state, temp_data = user.get("state", ""), user.get("temp_data", {}) if user else {}
    
    if state == "WAIT_CONTENT" and (msg.document or msg.video or msg.audio or msg.voice or msg.photo or msg.text):
        if not await has_perm(user_id, "upload"): return
        await clean_chat_history(user_id, chat_id, context)
        link = None
        if msg.text and msg.text.startswith("http"): link = msg.text
        else:
            try:
                res = await context.bot.copy_message(chat_id=CHANNEL_ID, from_chat_id=chat_id, message_id=msg.message_id)
                ch_name = CHANNEL_ID.replace('@', '').replace('https://t.me/', '')
                link = f"https://t.me/{ch_name}/{res.message_id}"
            except Exception: return await msg.reply_text("❌ يرجى التأكد من رفع البوت كمشرف في القناة الافتراضية لحفظ النصوص.")
        temp_data["pending_link"] = link
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_CONTENT_TYPE", "temp_data": temp_data}}, upsert=True)
        sent_msg = await msg.reply_text("✅ <b>تم استلام الملف بنجاح!</b>\n👇 حدد نوع هذا المحتوى ليتم ربطه بالدرس:", parse_mode="HTML", reply_markup=await get_type_keyboard())
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_Q_EXCEL" and msg.document:
        if not await has_perm(user_id, "questions"): return
        if not msg.document.file_name.endswith(('.xlsx', '.xls')): return await msg.reply_text("⚠️ يرجى رفع ملف بصيغة Excel.")
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await msg.reply_text("⏳ جاري قراءة وتحليل ملف الاختبار...")
        try:
            file = await context.bot.get_file(msg.document.file_id)
            df = pd.read_excel(pd.ExcelFile(io.BytesIO(await file.download_as_bytearray())), sheet_name=0)
            cat, les, count_add, count_del = temp_data.get("q_cat", "عام"), temp_data.get("q_les", "عام"), 0, 0
            cols = list(df.columns)
            q_col = next((c for c in cols if 'سؤال' in str(c)), cols[0] if len(cols) > 0 else None)
            ans_col = next((c for c in cols if 'صحيح' in str(c)), cols[1] if len(cols) > 1 else None)
            
            if not q_col or not ans_col: raise ValueError("الملف فارغ أو تنسيق الأعمدة غير صحيح.")

            for _, row in df.iterrows():
                try:
                    if pd.notna(row.get(q_col)) and pd.notna(row.get(ans_col)):
                        q_val, ans_val = str(row[q_col]).strip(), str(row[ans_col]).strip()
                        if ans_val == "حذف":
                            await db.questions.delete_many({"category": cat, "lesson": les, "question": q_val})
                            count_del += 1
                        else:
                            wrongs = [str(row[wc]).strip() for wc in cols if wc != q_col and wc != ans_col and pd.notna(row.get(wc))]
                            await db.questions.update_one({"category": cat, "lesson": les, "question": q_val}, {"$set": {"correct": ans_val, "wrong": wrongs}}, upsert=True)
                            count_add += 1
                except Exception: continue

            await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
            await context.bot.delete_message(chat_id=chat_id, message_id=sent_msg.message_id)
            res_txt = f"🎉 <b>تمت مزامنة الاختبار لدرس ({html.escape(les)})!</b>\n" + (f"✅ تم تحديث {count_add} سؤال.\n" if count_add else "") + (f"🗑️ تم حذف {count_del} سؤال.\n" if count_del else "")
            final_msg = await msg.reply_text(res_txt, parse_mode="HTML")
            await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": final_msg.message_id}}, upsert=True)
            return
        except Exception as e:
            await context.bot.delete_message(chat_id=chat_id, message_id=sent_msg.message_id)
            return await msg.reply_text(f"❌ حدث خطأ أثناء قراءة ملف الإكسل للاختبار:\n<code>{e}</code>", parse_mode="HTML")

    if state == "WAIT_EXCEL" and msg.document:
        if not await has_perm(user_id, "upload"): return
        if not msg.document.file_name.endswith(('.xlsx', '.xls')): return await msg.reply_text("⚠️ يرجى رفع ملف بصيغة Excel (.xlsx) فقط.")
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await msg.reply_text("⏳ جاري تحليل قاعدة البيانات وتطبيق نظام المزامنة الذكية...")
        try:
            file = await context.bot.get_file(msg.document.file_id)
            xls = pd.ExcelFile(io.BytesIO(await file.download_as_bytearray()))
            updates_log, df_lib, df_q, df_struct = "", None, None, None

            for sheet in xls.sheet_names:
                df = pd.read_excel(xls, sheet_name=sheet)
                cols = [str(c).strip() for c in df.columns]
                if any('الإجراء' in c for c in cols) and any('السلسلة' in c for c in cols): df_struct = df
                elif any('السؤال' in c for c in cols) and any('الصحيح' in c for c in cols): df_q = df
                elif any('السلسلة' in c for c in cols) and any('الدرس' in c or 'المحاضرة' in c for c in cols): df_lib = df

            if df_struct is not None:
                count_s_add, count_s_edit, count_s_del = 0, 0, 0
                for _, row in df_struct.iterrows():
                    cols = [str(c).strip() for c in df_struct.columns]
                    action_col = next((c for c in cols if 'الإجراء' in c or 'إجراء' in c), None)
                    cat_col = next((c for c in cols if 'السلسلة' in c and 'جديد' not in c), None)
                    les_col = next((c for c in cols if ('الدرس' in c or 'المحاضرة' in c) and 'جديد' not in c), None)
                    new_cat_col = next((c for c in cols if 'السلسلة' in c and 'جديد' in c), None)
                    new_les_col = next((c for c in cols if ('الدرس' in c or 'المحاضرة' in c) and 'جديد' in c), None)

                    if action_col and cat_col and pd.notna(row.get(action_col)) and pd.notna(row.get(cat_col)):
                        action, cat = str(row[action_col]).strip(), str(row[cat_col]).strip()
                        les = str(row[les_col]).strip() if les_col and pd.notna(row.get(les_col)) else None
                        new_cat = str(row[new_cat_col]).strip() if new_cat_col and pd.notna(row.get(new_cat_col)) else cat
                        new_les = str(row[new_les_col]).strip() if new_les_col and pd.notna(row.get(new_les_col)) else les

                        if "حذف" in action:
                            if les:
                                await db.library.delete_many({"category": cat, "lesson": les})
                                await db.questions.delete_many({"category": cat, "lesson": les})
                            else:
                                await db.library.delete_many({"category": {"$regex": f"^{cat}"}})
                                await db.questions.delete_many({"category": {"$regex": f"^{cat}"}})
                            count_s_del += 1
                        elif "تعديل" in action:
                            if les and new_les:
                                await db.library.update_many({"category": cat, "lesson": les}, {"$set": {"category": new_cat, "lesson": new_les}})
                                await db.questions.update_many({"category": cat, "lesson": les}, {"$set": {"category": new_cat, "lesson": new_les}})
                                await db.lesson_stats.update_many({"category": cat, "lesson": les}, {"$set": {"category": new_cat, "lesson": new_les}})
                            else:
                                await db.library.update_many({"category": cat}, {"$set": {"category": new_cat}})
                                await db.questions.update_many({"category": cat}, {"$set": {"category": new_cat}})
                                await db.lesson_stats.update_many({"category": cat}, {"$set": {"category": new_cat}})
                            count_s_edit += 1
                        elif "إضافة" in action:
                            if les: await db.library.insert_one({"category": cat, "lesson": les, "type": "type_text", "file_id": None})
                            else: await db.library.insert_one({"category": cat, "lesson": "درس افتراضي", "type": "type_text", "file_id": None})
                            count_s_add += 1
                if count_s_add > 0: updates_log += f"✅ تم إضافة {count_s_add} هيكل.\n"
                if count_s_edit > 0: updates_log += f"✏️ تم تعديل {count_s_edit} هيكل.\n"
                if count_s_del > 0: updates_log += f"🗑️ تم حذف {count_s_del} هيكل.\n\n"

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
                        cat_val, les_val = str(row[cat_col]).strip(), str(row[les_col]).strip()
                        excel_type_val, t_val = str(row.get(type_col, '')).strip(), "type_text"
                        for t_name, t_id in name_to_id.items():
                            if t_name in excel_type_val.lower() or excel_type_val.lower() in t_name: t_val = t_id; break
                        if "فيديو" in excel_type_val: t_val = "type_video"
                        elif "صوت" in excel_type_val: t_val = "type_audio"
                        elif "صور" in excel_type_val or "فلاشة" in excel_type_val: t_val = "type_image"

                        l_val = str(row.get(link_col, '')).strip() if link_col and pd.notna(row.get(link_col)) else ""
                        if l_val == "حذف":
                            await db.library.delete_many({"category": cat_val, "lesson": les_val, "type": t_val})
                            count_del += 1
                        elif l_val and l_val.lower() not in ['nan', 'none', 'null']:
                            await db.library.update_one({"category": cat_val, "lesson": les_val, "type": t_val}, {"$set": {"file_id": l_val, "updated_at": time.time()}}, upsert=True)
                            count_add += 1
                if count_add > 0: updates_log += f"✅ تم مزامنة {count_add} رابط.\n"
                if count_del > 0: updates_log += f"🗑️ تم حذف {count_del} رابط.\n"
                
            if df_q is not None:
                count_q_add, count_q_del = 0, 0
                cols_q = list(df_q.columns)
                q_col, ans_col = next((c for c in cols_q if 'السؤال' in str(c)), None), next((c for c in cols_q if 'الصحيح' in str(c)), None)
                cat_col, les_col = next((c for c in cols_q if 'السلسلة' in str(c)), None), next((c for c in cols_q if 'الدرس' in str(c) or 'المحاضرة' in str(c)), None)
                
                for _, row in df_q.iterrows():
                    if q_col and ans_col and pd.notna(row.get(q_col)) and pd.notna(row.get(ans_col)):
                        q_val, ans_val = str(row[q_col]).strip(), str(row[ans_col]).strip()
                        cat_val = str(row.get(cat_col, 'عام')).strip() if cat_col and pd.notna(row.get(cat_col)) else 'عام'
                        les_val = str(row.get(les_col, 'عام')).strip() if les_col and pd.notna(row.get(les_col)) else 'عام'
                        
                        if ans_val == "حذف":
                            await db.questions.delete_many({"category": cat_val, "lesson": les_val, "question": q_val})
                            count_q_del += 1
                        else:
                            wrongs = [str(row[wc]).strip() for wc in cols_q if ('خاطئة' in str(wc) or 'خطأ' in str(wc)) and pd.notna(row.get(wc))]
                            await db.questions.update_one({"category": cat_val, "lesson": les_val, "question": q_val}, {"$set": {"correct": ans_val, "wrong": wrongs}}, upsert=True)
                            count_q_add += 1
                if count_q_add > 0: updates_log += f"✅ تم مزامنة {count_q_add} سؤال عام.\n"
                if count_q_del > 0: updates_log += f"🗑️ تم حذف {count_q_del} سؤال عام.\n"

            if not updates_log: 
                await context.bot.delete_message(chat_id=chat_id, message_id=sent_msg.message_id)
                return await msg.reply_text("⚠️ لم يتم العثور على تحديثات.")
            
            await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}}, upsert=True)
            await context.bot.delete_message(chat_id=chat_id, message_id=sent_msg.message_id)
            final_msg = await msg.reply_text(f"🎉 <b>اكتملت المزامنة الذكية بنجاح!</b>\n\n{updates_log}", parse_mode="HTML")
            await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": final_msg.message_id}}, upsert=True)
            return
        except Exception as e: return await msg.reply_text(f"❌ خطأ: <code>{e}</code>", parse_mode="HTML")

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
                return await msg.reply_text(f"⚠️ <b>تنبيه:</b> موجود مسبقاً!\n📁 السلسلة: {html.escape(existing.get('category',''))}\n📖 المحاضرة: {html.escape(existing.get('lesson',''))}", parse_mode="HTML")
            
        pipeline = [{"$group": {"_id": "$category", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        cats = await db.library.aggregate(pipeline).to_list(length=None)
        
        btns = [[InlineKeyboardButton(f"📁 | {c['_id']}", callback_data=f"uc_{str(c['doc_id'])}")] for c in cats[:90] if c['_id'] and str(c['_id']).lower() != 'nan']
        btns.append([InlineKeyboardButton("➕ | إضافة سلسلة جديدة", callback_data="uc_new")])
        btns.append([InlineKeyboardButton("❌ | إلغاء الربط", callback_data="admin_cancel")])
        
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await msg.reply_text("📥 <b>تم استلام المحتوى!</b>\nاختر السلسلة:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "UPLOADING", "temp_data": {"file_id": final_link, "telegram_file_id": telegram_file_id}, "last_msg_id": sent_msg.message_id}}, upsert=True)

# ==========================================
# 🌟 التقاط المستخدمين في الرسائل النصية الشاملة 🌟
# ==========================================
async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    text = update.message.text
    if not text: return
    if await check_spam(user_id): return
    if db is None: return await update.message.reply_text("⚠️ خطأ في الاتصال.")

    asyncio.create_task(background_db_update(user_id))
    kb = await get_main_keyboard(user_id)
    
    if text in ['إلغاء', '❌ إلغاء', '/cancel']:
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text("✅ <b>تم إلغاء العملية.</b>", parse_mode="HTML", reply_markup=kb)
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if text.startswith('/start'):
        # 🌟 التقاط اسم المستخدم ومعرفه عند بدأ البوت 🌟
        u_name = update.message.from_user.first_name
        u_username = update.message.from_user.username
        full_name_str = f"{u_name} (@{u_username})" if u_username else u_name
        
        await db.users.update_one({"_id": user_id}, {
            "$set": {"state": "", "temp_data": {}, "name": full_name_str}, 
            "$setOnInsert": {"score": 0, "comp_score": 0, "answered": []}
        }, upsert=True)
        
        await clean_chat_history(user_id, chat_id, context)
        if 'les_' in text: return await show_lesson_ui(context, chat_id, text.replace('/start les_', '').strip(), user_id=user_id)
        sent_msg = await update.message.reply_text("📖 <b>أهلاً بك في منصة المشروع القرآني</b>\n\nتصفح الدروس وابدأ رحلتك المعرفية بالضغط على الزر أدناه 👇", parse_mode="HTML", reply_markup=kb)
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if 'اعرف الله' in text:
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
        pipeline = [{"$group": {"_id": "$category", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        try: cats = await db.library.aggregate(pipeline).to_list(length=None)
        except Exception as e: logging.error(f"Agg err: {e}"); cats = []
        
        await clean_chat_history(user_id, chat_id, context)
        if not cats: 
            sent_msg = await update.message.reply_text("📚 السلاسل قيد التجهيز.", reply_markup=kb)
            await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
            return
            
        btns = [[InlineKeyboardButton(f"📂 | {c['_id']}", callback_data=f"cat_{str(c['doc_id'])}")] for c in cats[:90] if c['_id'] and str(c['_id']).lower() != 'nan']
        try:
            sent_msg = await update.message.reply_text("📚 <b>المشروع القرآني:</b>\nيرجى اختيار السلسلة المطلوبة:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
            await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        except Exception as e: logging.error(f"Error in اعرف الله: {e}")
        return

    adm = await get_admin_doc(user_id)
    if 'لوحة الإدارة' in text and adm:
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
        btns = []
        if await has_perm(user_id, "upload"): btns.append([InlineKeyboardButton("📂 | إدارة السلاسل والدروس", callback_data="admin_content_mgr")])
        if await has_perm(user_id, "publish"):
            btns.append([InlineKeyboardButton("📢 | قسم النشر والقوالب", callback_data="admin_publishing_hub")])
            btns.append([InlineKeyboardButton("🏆 | مسابقات القناة (جديد)", callback_data="admin_comp_menu")])
            btns.append([InlineKeyboardButton("🎛️ | إدارة أنواع المحتوى", callback_data="admin_content_types")])
        if await has_perm(user_id, "questions"): btns.append([InlineKeyboardButton("➕ | إضافة اختبار/سؤال لدرس", callback_data="admin_add_q")])
        btns.append([InlineKeyboardButton("📥 | تصدير / استيراد قاعدة البيانات", callback_data="admin_import_export")])
        if await has_perm(user_id, "manage_admins"): btns.append([InlineKeyboardButton("👥 | إدارة المشرفين", callback_data="admin_manage")])
        btns.append([InlineKeyboardButton("❌ | إغلاق اللوحة", callback_data="admin_cancel")])
        
        await clean_chat_history(user_id, chat_id, context)
        try:
            sent_msg = await update.message.reply_text("⚙️ <b>لوحة التحكم والإدارة:</b>\nاختر الإجراء المطلوب:", reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
            await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        except Exception as e: logging.error(f"Error Admin: {e}")
        return

    user = await db.users.find_one({"_id": user_id})
    state, temp_data = user.get("state", ""), user.get("temp_data", {}) if user else {}

    if state == "WAIT_CONTENT":
        if not await has_perm(user_id, "upload"): return
        await clean_chat_history(user_id, chat_id, context)
        if text.startswith("http"): link = text
        else:
            try:
                res = await context.bot.copy_message(chat_id=CHANNEL_ID, from_chat_id=chat_id, message_id=update.message.message_id)
                ch_name = CHANNEL_ID.replace('@', '').replace('https://t.me/', '')
                link = f"https://t.me/{ch_name}/{res.message_id}"
            except Exception:
                return await update.message.reply_text("❌ يرجى التأكد من رفع البوت كمشرف في القناة الافتراضية لحفظ النصوص.")
        temp_data["pending_link"] = link
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_CONTENT_TYPE", "temp_data": temp_data}}, upsert=True)
        sent_msg = await update.message.reply_text("✅ <b>تم استلام المحتوى بنجاح!</b>\n👇 حدد نوع هذا المحتوى ليتم ربطه بالدرس:", parse_mode="HTML", reply_markup=await get_type_keyboard())
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_COMP_TIME":
        if not await has_perm(user_id, "publish"): return
        if not text.isdigit(): return await update.message.reply_text("⚠️ يرجى إرسال رقم (عدد الدقائق) فقط.")
        minutes = int(text)
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}})
        await clean_chat_history(user_id, chat_id, context)
        
        q_oid = get_safe_oid(temp_data.get("comp_q_id"))
        if not q_oid: return await update.message.reply_text("السؤال غير متوفر.")
        q_doc = await db.questions.find_one({"_id": q_oid})
        if not q_doc: return await update.message.reply_text("السؤال غير متوفر.")
        
        expire_ts = int(time.time()) + (minutes * 60)
        options = [(q_doc["correct"], "1")] + [(w, "0") for w in q_doc["wrong"] if w and str(w).lower() != 'nan']
        random.shuffle(options)
        
        btns = [[InlineKeyboardButton(opt_text, callback_data=f"cq_{str(q_doc['_id'])}_{is_c}_{expire_ts}")] for opt_text, is_c in options]
        msg_text = f"🏆 <b>مسابقة القناة التفاعلية</b> 🏆\n\n📁 <b>السلسلة:</b> {html.escape(q_doc['category'])}\n📖 <b>الدرس:</b> {html.escape(q_doc['lesson'])}\n⏱️ <b>تنتهي خلال:</b> {minutes} دقيقة\n\n❓ <b>السؤال:</b>\n{html.escape(q_doc['question'])}"
        
        try:
            await context.bot.send_message(chat_id=CHANNEL_ID, text=msg_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
            sent_msg = await update.message.reply_text(f"🎉 <b>تم إرسال المسابقة للقناة بنجاح!</b>\nسيتم إغلاقها تلقائياً بعد {minutes} دقيقة.", parse_mode="HTML", reply_markup=kb)
            await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        except Exception as e:
            sent_msg = await update.message.reply_text(f"❌ حدث خطأ في النشر للقناة: <code>{e}</code>", parse_mode="HTML")
            await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    # بقية إدخالات الإدارة
    if state == "WAIT_MGR_NEW_CAT":
        new_cat = text.strip()
        await db.library.insert_one({"category": new_cat, "lesson": "درس افتراضي", "type": "type_text", "file_id": None})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة لمدير المحتوى", callback_data="admin_content_mgr")]]
        sent_msg = await update.message.reply_text(f"✅ تم إضافة السلسلة الجديدة: (<b>{html.escape(new_cat)}</b>) بنجاح!", reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_MGR_EDIT_CAT":
        old_cat, new_cat = temp_data.get("mgr_target_cat"), text.strip()
        await db.library.update_many({"category": old_cat}, {"$set": {"category": new_cat}})
        await db.questions.update_many({"category": old_cat}, {"$set": {"category": new_cat}})
        await db.lesson_stats.update_many({"category": old_cat}, {"$set": {"category": new_cat}})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة لمدير المحتوى", callback_data="admin_content_mgr")]]
        sent_msg = await update.message.reply_text(f"✅ تمت العملية!\nتم تحديث اسم السلسلة من ({html.escape(old_cat)}) إلى (<b>{html.escape(new_cat)}</b>) وتعديل كافة الدروس والأسئلة.", reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_MGR_NEW_LES":
        target_cat, cat_id, new_les = temp_data.get("mgr_target_cat"), temp_data.get("mgr_target_cat_id", ""), text.strip()
        await db.library.insert_one({"category": target_cat, "lesson": new_les, "type": "type_text", "file_id": None})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة للسلسلة", callback_data=f"mgr_cat_view_{cat_id}")]]
        sent_msg = await update.message.reply_text(f"✅ تم إنشاء الدرس الجديد: (<b>{html.escape(new_les)}</b>) بنجاح!", reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_MGR_EDIT_LES":
        target_cat, cat_id, old_les, new_les = temp_data.get("mgr_target_cat"), temp_data.get("mgr_target_cat_id", ""), temp_data.get("mgr_target_les"), text.strip()
        await db.library.update_many({"category": target_cat, "lesson": old_les}, {"$set": {"lesson": new_les}})
        await db.questions.update_many({"category": target_cat, "lesson": old_les}, {"$set": {"lesson": new_les}})
        await db.lesson_stats.update_many({"category": target_cat, "lesson": old_les}, {"$set": {"lesson": new_les}})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة للسلسلة", callback_data=f"mgr_cat_view_{cat_id}")]]
        sent_msg = await update.message.reply_text(f"✅ تمت العملية!\nتم تعديل اسم الدرس من ({html.escape(old_les)}) إلى (<b>{html.escape(new_les)}</b>).", reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_TYPE_DATA" and await has_perm(user_id, "publish"):
        parts = text.split(',')
        name, icon = parts[0].strip(), parts[1].strip() if len(parts) > 1 else "📁"
        await db.content_types.insert_one({"_id": f"type_{int(time.time())}", "name": name, "icon": icon})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة", callback_data="admin_content_types")]]
        sent_msg = await update.message.reply_text(f"✅ تم إضافة النوع ({icon} {html.escape(name)}) بنجاح!", reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_EDIT_TYPE" and await has_perm(user_id, "publish"):
        parts = text.split(',')
        name, icon = parts[0].strip(), parts[1].strip() if len(parts) > 1 else "📁"
        await db.content_types.update_one({"_id": temp_data.get("edit_t_id")}, {"$set": {"name": name, "icon": icon}})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة", callback_data="admin_content_types")]]
        sent_msg = await update.message.reply_text(f"✅ تم تحديث النوع إلى ({icon} {html.escape(name)}) بنجاح!", reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_CHAN_ID" and await has_perm(user_id, "publish"):
        ch = text.strip()
        if not ch.startswith('@') and not ch.startswith('-100'): return await update.message.reply_text("⚠️ معرّف القناة يجب أن يبدأ بـ @ أو -100")
        await db.settings.update_one({"_id": "channels"}, {"$addToSet": {"list": ch}}, upsert=True)
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة لإدارة القنوات", callback_data="admin_channels")]]
        sent_msg = await update.message.reply_text(f"✅ تم إضافة القناة ({ch}) بنجاح!", reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_TPL_NAME":
        temp_data["tpl_name"] = text.strip()
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_TPL_CONTENT", "temp_data": temp_data}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        msg = f"""✅ تم اختيار اسم القالب: <b>{html.escape(text)}</b>\n\n✍️ أرسل الآن محتوى القالب وتصميمه.\nاستخدم المتغيرات بالأقواس المعكوفة:\n<code>{{سلسلة}}</code> ، <code>{{درس}}</code> ، <code>{{تاريخ}}</code> ، <code>{{تذييل}}</code>"""
        sent_msg = await update.message.reply_text(msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_TPL_CONTENT":
        tpl_name = temp_data.get("tpl_name", "قالب جديد")
        await db.templates.insert_one({"name": tpl_name, "content": text})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        btns = [[InlineKeyboardButton("🔙 | العودة للقوالب", callback_data="admin_tpl_menu")]]
        sent_msg = await update.message.reply_text(f"🎉 <b>تم حفظ قالب ({html.escape(tpl_name)}) بنجاح!</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_FOOTER_TEXT":
        await db.settings.update_one({"_id": "bot_settings"}, {"$set": {"footer_text": text}}, upsert=True)
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text("✅ <b>تم حفظ نص/رابط التذييل بنجاح!</b>", parse_mode="HTML", reply_markup=kb)
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_ADMIN_ID" and await has_perm(user_id, "manage_admins"):
        new_admin = text.strip()
        if not new_admin.isdigit(): return await update.message.reply_text("⚠️ الآيدي يجب أن يكون أرقاماً فقط.")
        perms = {"upload": False, "questions": False, "publish": False, "stats": False, "manage_admins": False}
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {"new_admin_id": new_admin, "admin_perms": perms}}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text(f"⚙️ <b>تحديد صلاحيات المشرف ({new_admin}):</b>\nانقر للتفعيل ✅ أو التعطيل ❌ ثم حفظ:", parse_mode="HTML", reply_markup=get_perms_kb(perms, edit_mode=False))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_UPL_LES_TEXT":
        temp_data["lesson"] = text
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_TYPE", "temp_data": temp_data}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text(f"📖 المحاضرة: <b>{html.escape(text)}</b>\n\n👇 ما هو <b>نوع</b> هذا المحتوى؟", parse_mode="HTML", reply_markup=await get_type_keyboard())
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_Q_TEXT":
        temp_data["q_text"] = text
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_Q_CORRECT", "temp_data": temp_data}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text("✅ ممتاز.\nأرسل الآن <b>الإجابة الصحيحة</b>:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_Q_CORRECT":
        temp_data["q_correct"] = text
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_Q_WRONG", "temp_data": temp_data}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text("❌ أرسل الآن <b>الإجابات الخاطئة</b> مفصولة بفاصلة:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    if state == "WAIT_Q_WRONG":
        wrongs = [w.strip() for w in text.split(',') if w.strip()]
        await db.questions.insert_one({"category": temp_data.get("q_cat"), "lesson": temp_data.get("q_les"), "question": temp_data.get("q_text"), "correct": temp_data.get("q_correct"), "wrong": wrongs, "correct_answers": 0, "wrong_answers": 0})
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
        await clean_chat_history(user_id, chat_id, context)
        sent_msg = await update.message.reply_text("🎉 <b>تم حفظ السؤال بنجاح!</b>", parse_mode="HTML", reply_markup=kb)
        await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
        return

    try: await update.message.reply_text("الرجاء استخدام الأزرار المتاحة 👇", reply_markup=kb)
    except: pass

# ==========================================
# 🌟 استخراج بيانات وتفاعل الأزرار 🌟
# ==========================================
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id, user_id = query.message.chat_id, str(query.from_user.id)
    if await check_spam(user_id): 
        try: await query.answer("الرجاء عدم التكرار السريع", show_alert=False)
        except: pass
        return
    
    data = query.data

    # 🌟 التفاعل مع مسابقات القناة 🌟
    if data.startswith("cq_"):
        parts = data.split("_")
        q_id, is_correct, expire_ts = parts[1], parts[2] == "1", int(parts[3])
        
        if int(time.time()) > expire_ts: return await query.answer("⏳ عذراً، انتهى الوقت المخصص لهذا السؤال!", show_alert=True)
        exists = await db.comp_answers.find_one({"user_id": user_id, "q_id": q_id})
        if exists: return await query.answer("لقد أجبت على هذا السؤال مسبقاً! ⚠️", show_alert=True)
        
        await db.comp_answers.insert_one({"user_id": user_id, "q_id": q_id, "correct": is_correct})
        
        # التقاط اسم المتسابق من التليجرام
        u_name = query.from_user.first_name
        
        if is_correct:
            await db.users.update_one({"_id": user_id}, {"$inc": {"comp_score": 10}, "$set": {"name": u_name}}, upsert=True)
            return await query.answer("إجابة صحيحة! ✅ اكتسبت 10 نقاط.", show_alert=True)
        else:
            await db.users.update_one({"_id": user_id}, {"$set": {"name": u_name}}, upsert=True) # حفظ اسمه حتى لو أخطأ
            return await query.answer("إجابة خاطئة! ❌", show_alert=True)

    if data == "media_unavail": return await query.answer("⚠️ غير متوفر.", show_alert=True)
    if data == "ignore": 
        try: await query.answer()
        except: pass
        return 
        
    try: await query.answer()
    except: pass
    
    user = await db.users.find_one({"_id": user_id})

    if data == "admin_cancel":
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
        await safe_edit(query, "✅ <b>تم إلغاء العملية.</b>")
        return

    if data == "main_menu":
        pipeline = [{"$group": {"_id": "$category", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        try: cats = await db.library.aggregate(pipeline).to_list(length=None)
        except Exception as e: logging.error(f"Agg err: {e}"); cats = []
        btns = [[InlineKeyboardButton(f"📂 | {c['_id']}", callback_data=f"cat_{str(c['doc_id'])}")] for c in cats[:90] if c['_id'] and str(c['_id']).lower() != 'nan']
        await safe_edit(query, "📚 <b>المشروع القرآني:</b>\nيرجى اختيار السلسلة المطلوبة:", InlineKeyboardMarkup(btns))
        return

    if data == "admin_menu":
        adm = await get_admin_doc(user_id)
        if not adm: return
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}}, upsert=True)
        btns = []
        if await has_perm(user_id, "upload"): btns.append([InlineKeyboardButton("📂 | إدارة السلاسل والدروس", callback_data="admin_content_mgr")])
        if await has_perm(user_id, "publish"):
            btns.append([InlineKeyboardButton("📢 | قسم النشر والقوالب", callback_data="admin_publishing_hub")])
            btns.append([InlineKeyboardButton("🏆 | مسابقات القناة (جديد)", callback_data="admin_comp_menu")])
            btns.append([InlineKeyboardButton("🎛️ | إدارة أنواع المحتوى", callback_data="admin_content_types")])
        if await has_perm(user_id, "questions"): btns.append([InlineKeyboardButton("➕ | إضافة اختبار/سؤال لدرس", callback_data="admin_add_q")])
        btns.append([InlineKeyboardButton("📥 | تصدير / استيراد قاعدة البيانات", callback_data="admin_import_export")])
        if await has_perm(user_id, "manage_admins"): btns.append([InlineKeyboardButton("👥 | إدارة المشرفين", callback_data="admin_manage")])
        btns.append([InlineKeyboardButton("❌ | إغلاق اللوحة", callback_data="admin_cancel")])
        await safe_edit(query, "⚙️ <b>لوحة التحكم والإدارة:</b>\nاختر الإجراء المطلوب:", InlineKeyboardMarkup(btns))
        return

    # ================= 🌟 الاستيراد والتصدير وإنشاء قائمة المستخدمين 🌟 =================
    if data == "admin_import_export":
        btns = [
            [InlineKeyboardButton("📥 استيراد ومزامنة إكسل", callback_data="import_confirm")],
            [InlineKeyboardButton("📤 تصدير قاعدة البيانات", callback_data="export_db")],
            [InlineKeyboardButton("🔙 رجوع للوحة الإدارة", callback_data="admin_menu")]
        ]
        await safe_edit(query, "📥 <b>تصدير واستيراد البيانات:</b>\nاختر الإجراء المطلوب:", InlineKeyboardMarkup(btns))
        return
        
    if data == "export_db" and await has_perm(user_id, "stats"):
        await safe_edit(query, "⏳ جاري تجهيز ملف الإكسل (المكتبة، الأسئلة، والمستخدمين)...")
        try:
            lib_data = await db.library.find({}).to_list(length=None)
            df_lib = pd.DataFrame(lib_data)
            if not df_lib.empty: df_lib = df_lib.rename(columns={"category": "السلسلة", "lesson": "المحاضرة /الدرس", "type": "النوع", "file_id": "الرابط"})[["المحاضرة /الدرس", "السلسلة", "النوع", "الرابط"]]
            else: df_lib = pd.DataFrame(columns=["المحاضرة /الدرس", "السلسلة", "النوع", "الرابط"])
            
            q_data = await db.questions.find({}).to_list(length=None)
            q_list = []
            for q in q_data:
                w_list = q.get("wrong") or []
                q_list.append({
                    "السلسلة": q.get("category", ""),
                    "المحاضرة /الدرس": q.get("lesson", ""),
                    "السؤال": q.get("question", ""),
                    "الإجابة_الصحيحة": q.get("correct", ""),
                    "خاطئة_1": w_list[0] if len(w_list) > 0 else "",
                    "خاطئة_2": w_list[1] if len(w_list) > 1 else ""
                })

            # 🌟 تصدير قائمة المستخدمين 🌟
            all_users = await db.users.find({}).to_list(length=None)
            users_list = []
            for u in all_users:
                last_active_ts = u.get("last_active", 0)
                last_active_str = datetime.datetime.fromtimestamp(last_active_ts).strftime('%Y-%m-%d %H:%M') if last_active_ts else "غير معروف"
                users_list.append({
                    "آيدي المستخدم": u.get("_id", ""),
                    "الاسم": u.get("name", "غير متوفر"),
                    "النقاط (المسابقات)": u.get("comp_score", 0),
                    "عدد الإجابات": len(u.get("answered", [])),
                    "آخر نشاط": last_active_str
                })
            df_users = pd.DataFrame(users_list)
            
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df_lib.to_excel(writer, sheet_name='المشروع القرأني', index=False)
                pd.DataFrame(q_list).to_excel(writer, sheet_name='قيم_نفسك', index=False)
                if not df_users.empty: df_users.to_excel(writer, sheet_name='قائمة_المستخدمين', index=False)

            output.seek(0)
            await query.message.delete()
            await context.bot.send_document(chat_id=chat_id, document=output, filename="قاعدة_بيانات_البوت.xlsx")
        except: pass
        return

    # ================= 🌟 مسابقات القناة 🌟 =================
    if data == "admin_comp_menu" and await has_perm(user_id, "publish"):
        btns = [
            [InlineKeyboardButton("🚀 | إرسال سؤال مسابقة للقناة", callback_data="comp_send_q")],
            [InlineKeyboardButton("📊 | لوحة الشرف (أعلى المتسابقين)", callback_data="comp_leaderboard")],
            [InlineKeyboardButton("🗑️ | تصفير نقاط المتسابقين", callback_data="comp_reset")],
            [InlineKeyboardButton("🔙 | رجوع للوحة الإدارة", callback_data="admin_menu")]
        ]
        await safe_edit(query, "🏆 <b>إدارة مسابقات القناة:</b>\nأضف التفاعل والمنافسة لقناتك بسهولة!", InlineKeyboardMarkup(btns))
        return

    if data == "comp_leaderboard":
        top_users = await db.users.find({"comp_score": {"$gt": 0}}).sort("comp_score", -1).limit(15).to_list(length=None)
        if not top_users: return await safe_edit(query, "⚠️ لا يوجد متسابقين بنقاط حتى الآن.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_comp_menu")]]))
        txt, medals = "📊 <b>لوحة الشرف لأعلى المتسابقين:</b>\n\n", ["🥇", "🥈", "🥉", "🏅", "🏅"]
        for idx, u in enumerate(top_users):
            medal = medals[idx] if idx < 5 else "👤"
            txt += f"{medal} <b>{html.escape(u.get('name', 'متسابق غير معروف'))}</b>: <code>{u.get('comp_score')}</code> نقطة\n"
        await safe_edit(query, txt, InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_comp_menu")]]))
        return

    if data == "comp_reset":
        await db.users.update_many({}, {"$set": {"comp_score": 0}})
        await db.comp_answers.delete_many({})
        await safe_edit(query, "✅ <b>تم تصفير جميع النقاط بنجاح!</b>", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_comp_menu")]]))
        return

    if data == "comp_send_q":
        pipeline = [{"$group": {"_id": "$category", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        cats = await db.library.aggregate(pipeline).to_list(length=None)
        btns = [[InlineKeyboardButton(f"📁 | {c['_id']}", callback_data=f"comp_cat_{str(c['doc_id'])}")] for c in cats[:90] if c['_id'] and str(c['_id']).lower() != 'nan']
        btns.append([InlineKeyboardButton("🔙 | تراجع", callback_data="admin_comp_menu")])
        await safe_edit(query, "🚀 <b>إرسال سؤال مسابقة:</b>\nاختر السلسلة التي تود أخذ السؤال منها:", InlineKeyboardMarkup(btns))
        return

    if data.startswith("comp_cat_"):
        oid = get_safe_oid(data.replace("comp_cat_", ""))
        if not oid: return await safe_edit(query, "⚠️ القائمة قديمة، يرجى تحديث النظام بإرسال /start.")
        doc = await db.library.find_one({"_id": oid})
        if not doc: return await safe_edit(query, "⚠️ عذراً، لم يعد هذا العنصر متوفراً.")
        cat_name = doc["category"]
        pipeline = [{"$match": {"category": cat_name}}, {"$group": {"_id": "$lesson", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        lessons = await db.library.aggregate(pipeline).to_list(length=None)
        btns = [[InlineKeyboardButton(f"📖 | {idx}- {les['_id']}", callback_data=f"comp_les_{str(les['doc_id'])}")] for idx, les in enumerate(lessons[:90], 1)]
        btns.append([InlineKeyboardButton("🔙 | تراجع", callback_data="comp_send_q")])
        await safe_edit(query, f"📁 السلسلة: <b>{html.escape(cat_name)}</b>\nاختر الدرس:", InlineKeyboardMarkup(btns))
        return

    if data.startswith("comp_les_"):
        oid = get_safe_oid(data.replace("comp_les_", ""))
        if not oid: return await safe_edit(query, "⚠️ القائمة قديمة، يرجى تحديث النظام بإرسال /start.")
        doc = await db.library.find_one({"_id": oid})
        if not doc: return await safe_edit(query, "⚠️ عذراً، لم يعد هذا العنصر متوفراً.")
        
        all_qs = await db.questions.find({"lesson": doc["lesson"]}).to_list(length=None)
        if not all_qs: return await safe_edit(query, "⚠️ لا توجد أسئلة مضافة لهذا الدرس!", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 تراجع", callback_data="comp_send_q")]]))
        q = random.choice(all_qs)
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_COMP_TIME", "temp_data": {"comp_q_id": str(q["_id"])}}}, upsert=True)
        
        await safe_edit(query, f"✅ تم اختيار سؤال عشوائي من درس ({html.escape(doc['lesson'])}).\n\n✍️ <b>أرسل الآن مدة المسابقة بالدقائق</b> (مثلاً: <code>60</code>):", InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    # ================= 🌟 إدارة المشرفين والصلاحيات 🌟 =================
    if data == "admin_manage" and await has_perm(user_id, "manage_admins"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}}, upsert=True)
        admins = await db.admins.find({}).to_list(length=None)
        btns = [[InlineKeyboardButton(f"👤 | تعديل المشرف ({adm['_id']})", callback_data=f"editadm_{adm['_id']}")] for adm in admins[:90]]
        btns.extend([[InlineKeyboardButton("➕ | إضافة مشرف جديد", callback_data="add_admin")], [InlineKeyboardButton("🔙 | رجوع", callback_data="admin_menu")]])
        await safe_edit(query, "👥 <b>إدارة المشرفين والصلاحيات:</b>\nانقر للتعديل:", InlineKeyboardMarkup(btns))
        return

    if data.startswith("editadm_") and await has_perm(user_id, "manage_admins"):
        target_id = data.replace("editadm_", "")
        adm_doc = await db.admins.find_one({"_id": target_id})
        if not adm_doc: return await safe_edit(query, "⚠️ لم يتم العثور على المشرف.")
        perms = adm_doc.get("permissions", {"upload": False, "questions": False, "publish": False, "stats": False, "manage_admins": False})
        await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": {"edit_admin_id": target_id, "admin_perms": perms}}}, upsert=True)
        await safe_edit(query, f"⚙️ <b>صلاحيات المشرف ({target_id}):</b>", get_perms_kb(perms, edit_mode=True, admin_id=target_id))
        return

    if data == "add_admin" and await has_perm(user_id, "manage_admins"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_ADMIN_ID"}}, upsert=True)
        await safe_edit(query, "✍️ أرسل <b>آيدي (ID)</b> المشرف الجديد:", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 تراجع", callback_data="admin_manage")]]))
        return

    if data.startswith("deladmin_") and await has_perm(user_id, "manage_admins"):
        adm_id = data.replace("deladmin_", "")
        await db.admins.delete_one({"_id": adm_id})
        await safe_edit(query, f"✅ تم سحب الصلاحيات نهائياً من ({adm_id}).", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_manage")]]))
        return

    if data.startswith("adm_tgl_") and await has_perm(user_id, "manage_admins"):
        perm_key = data.replace("adm_tgl_", "")
        temp_data = user.get("temp_data", {})
        perms = temp_data.get("admin_perms", {})
        perms[perm_key] = not perms.get(perm_key, False)
        temp_data["admin_perms"] = perms
        await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": temp_data}}, upsert=True)
        edit_id = temp_data.get("edit_admin_id")
        try: await query.edit_message_reply_markup(get_perms_kb(perms, edit_mode=bool(edit_id), admin_id=edit_id))
        except: pass
        return

    if data == "adm_save_new" and await has_perm(user_id, "manage_admins"):
        temp_data = user.get("temp_data", {})
        new_id = temp_data.get("new_admin_id")
        perms = temp_data.get("admin_perms", {})
        if new_id:
            await db.admins.update_one({"_id": new_id}, {"$set": {"added_at": time.time(), "permissions": perms}}, upsert=True)
            await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {}}}, upsert=True)
            await safe_edit(query, f"✅ تم إضافة المشرف ({new_id}) بنجاح!", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_manage")]]))
        return

    if data.startswith("adm_save_") and data != "adm_save_new" and await has_perm(user_id, "manage_admins"):
        target_id = data.replace("adm_save_", "")
        perms = user.get("temp_data", {}).get("admin_perms", {})
        await db.admins.update_one({"_id": target_id}, {"$set": {"permissions": perms}}, upsert=True)
        await safe_edit(query, f"✅ تم تحديث الصلاحيات بنجاح!", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_manage")]]))
        return

    # ================= 🌟 رسائل التأكيد والتحذير 🌟 =================
    if data.startswith("ask_deltype_"):
        t_id = data.replace("ask_deltype_", "")
        btns = [[InlineKeyboardButton("✅ نعم، احذف نهائياً", callback_data=f"deltype_{t_id}")], [InlineKeyboardButton("❌ تراجع", callback_data="admin_content_types")]]
        await safe_edit(query, "⚠️ <b>تنبيه:</b>\nهل أنت متأكد من رغبتك في حذف هذا النوع؟\nلا يمكن التراجع عن هذه الخطوة.", InlineKeyboardMarkup(btns))
        return

    if data.startswith("ask_del_cat_"):
        oid = get_safe_oid(data.replace("ask_del_cat_", ""))
        if not oid: return await safe_edit(query, "⚠️ القائمة قديمة، يرجى تحديث النظام بإرسال /start.")
        doc = await db.library.find_one({"_id": oid})
        if not doc: return await safe_edit(query, "⚠️ عذراً، لم يعد هذا العنصر متوفراً.")
        btns = [[InlineKeyboardButton("✅ نعم، احذف السلسلة بالكامل", callback_data=f"mgr_del_cat_{str(oid)}")], [InlineKeyboardButton("❌ تراجع", callback_data=f"mgr_cat_view_{str(oid)}")]]
        await safe_edit(query, f"⚠️ <b>تحذير خطير:</b>\nهل أنت متأكد من حذف السلسلة (<b>{html.escape(doc['category'])}</b>)؟\n\n<i>سيتم مسح جميع الدروس والأسئلة المرتبطة بها نهائياً!</i>", InlineKeyboardMarkup(btns))
        return

    if data == "ask_del_les":
        cat, les, cat_id = user.get("temp_data", {}).get("mgr_target_cat"), user.get("temp_data", {}).get("mgr_target_les"), user.get("temp_data", {}).get("mgr_target_cat_id")
        btns = [[InlineKeyboardButton("✅ نعم، احذف الدرس نهائياً", callback_data="mgr_action_del_les")], [InlineKeyboardButton("❌ تراجع", callback_data=f"mgr_cat_view_{cat_id}")]]
        await safe_edit(query, f"⚠️ <b>تنبيه:</b>\nهل أنت متأكد من حذف الدرس (<b>{html.escape(les)}</b>)؟\n\n<i>سيتم مسح جميع روابطه وأسئلته من قاعدة البيانات!</i>", InlineKeyboardMarkup(btns))
        return

    # ================= 🌟 إدارة المحتوى المباشر للسلاسل والدروس 🌟 =================
    if data == "admin_content_mgr" and await has_perm(user_id, "upload"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}}, upsert=True)
        pipeline = [{"$group": {"_id": "$category", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        cats = await db.library.aggregate(pipeline).to_list(length=None)
        btns = [[InlineKeyboardButton(f"📁 | {c['_id']}", callback_data=f"mgr_cat_view_{str(c['doc_id'])}")] for c in cats[:90] if c['_id'] and str(c['_id']).lower() != 'nan']
        btns.append([InlineKeyboardButton("➕ | إضافة سلسلة جديدة", callback_data="mgr_add_cat")])
        btns.append([InlineKeyboardButton("🔙 | رجوع للوحة الإدارة", callback_data="admin_menu")])
        await safe_edit(query, "📂 <b>إدارة السلاسل والدروس:</b>\nاختر سلسلة لتعديلها أو إضافة دروس إليها:", InlineKeyboardMarkup(btns))
        return

    if data == "mgr_add_cat" and await has_perm(user_id, "upload"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_MGR_NEW_CAT"}}, upsert=True)
        await safe_edit(query, "✍️ أرسل اسم <b>السلسلة الجديدة</b>:\nسيتم إضافة درس افتراضي بداخلها لتأسيسها.", InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    if data.startswith("mgr_cat_view_"):
        oid = get_safe_oid(data.replace("mgr_cat_view_", ""))
        if not oid: return await safe_edit(query, "⚠️ القائمة قديمة، يرجى تحديث النظام بإرسال /start.")
        doc = await db.library.find_one({"_id": oid})
        if not doc: return await safe_edit(query, "⚠️ السلسلة فارغة أو تم حذفها.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_content_mgr")]]))
        cat_name = doc["category"]
        
        pipeline = [{"$match": {"category": cat_name}}, {"$group": {"_id": "$lesson", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        lessons = await db.library.aggregate(pipeline).to_list(length=None)
        
        btns = [[InlineKeyboardButton(f"📖 | {idx}- {les['_id']}", callback_data=f"mgr_les_{str(les['doc_id'])}")] for idx, les in enumerate(lessons[:90], 1)]
        btns.append([InlineKeyboardButton("➕ | إضافة درس جديد", callback_data=f"mgr_add_les_{str(oid)}")])
        btns.append([InlineKeyboardButton("✏️ | تعديل اسم السلسلة", callback_data=f"mgr_edit_cat_{str(oid)}")])
        btns.append([InlineKeyboardButton("🗑️ | حذف السلسلة (خطير)", callback_data=f"ask_del_cat_{str(oid)}")])
        btns.append([InlineKeyboardButton("🔙 | رجوع للسلاسل", callback_data="admin_content_mgr")])
        await safe_edit(query, f"📁 السلسلة: <b>{html.escape(cat_name)}</b>\nيمكنك إضافة دروس جديدة أو التعديل:", InlineKeyboardMarkup(btns))
        return

    if data.startswith("mgr_add_les_"):
        oid = get_safe_oid(data.replace("mgr_add_les_", ""))
        doc = await db.library.find_one({"_id": oid}) if oid else None
        if not doc: return await safe_edit(query, "⚠️ عنصر غير موجود.")
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_MGR_NEW_LES", "temp_data": {"mgr_target_cat": doc["category"], "mgr_target_cat_id": str(oid)}}}, upsert=True)
        await safe_edit(query, f"✍️ أرسل اسم <b>الدرس الجديد</b> للسلسلة ({html.escape(doc['category'])}):", InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    if data.startswith("mgr_edit_cat_"):
        oid = get_safe_oid(data.replace("mgr_edit_cat_", ""))
        doc = await db.library.find_one({"_id": oid}) if oid else None
        if not doc: return await safe_edit(query, "⚠️ عنصر غير موجود.")
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_MGR_EDIT_CAT", "temp_data": {"mgr_target_cat": doc["category"], "mgr_target_cat_id": str(oid)}}}, upsert=True)
        await safe_edit(query, f"✍️ أرسل <b>الاسم الجديد</b> بدلاً من ({html.escape(doc['category'])}):", InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    if data.startswith("mgr_del_cat_"):
        oid = get_safe_oid(data.replace("mgr_del_cat_", ""))
        doc = await db.library.find_one({"_id": oid}) if oid else None
        if doc:
            await db.library.delete_many({"category": doc["category"]})
            await db.questions.delete_many({"category": doc["category"]})
        await safe_edit(query, f"✅ تم حذف السلسلة بالكامل!", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_content_mgr")]]))
        return

    if data.startswith("mgr_les_"):
        oid = get_safe_oid(data.replace("mgr_les_", ""))
        if not oid: return await safe_edit(query, "⚠️ القائمة قديمة، يرجى تحديث النظام بإرسال /start.")
        doc = await db.library.find_one({"_id": oid})
        if not doc: return await safe_edit(query, "⚠️ الدرس غير موجود.")
        
        cat_doc = await db.library.find_one({"category": doc["category"]})
        cat_id = str(cat_doc["_id"]) if cat_doc else str(oid)
        
        await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": {"mgr_target_cat": doc["category"], "mgr_target_les": doc["lesson"], "mgr_target_cat_id": cat_id, "mgr_target_les_id": str(oid)}}}, upsert=True)
        
        btns = [
            [InlineKeyboardButton("🔗 | إرفاق محتوى جديد بالدرس (نص/ملف)", callback_data="mgr_attach_content")],
            [InlineKeyboardButton("✏️ | تعديل اسم الدرس", callback_data="mgr_action_edit_les")],
            [InlineKeyboardButton("🗑️ | حذف الدرس", callback_data="ask_del_les")],
            [InlineKeyboardButton("🔙 | رجوع لدروس السلسلة", callback_data=f"mgr_cat_view_{cat_id}")]
        ]
        await safe_edit(query, f"📖 الدرس: <b>{html.escape(doc['lesson'])}</b>\nماذا تريد أن تفعل؟", InlineKeyboardMarkup(btns))
        return
        
    if data == "mgr_attach_content":
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_CONTENT"}}, upsert=True)
        msg = "🔗 <b>إرفاق محتوى للدرس:</b>\n\nأرسل الآن المحتوى الذي تريده (سواء كان <b>نصاً طويلاً</b>، أو صورة، أو ملف، أو مجرد رابط). وسيقوم البوت بربطه بالدرس مباشرة."
        await safe_edit(query, msg, InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    if data == "mgr_action_edit_les":
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_MGR_EDIT_LES"}}, upsert=True)
        les = user.get("temp_data", {}).get("mgr_target_les")
        await safe_edit(query, f"✍️ أرسل <b>الاسم الجديد</b> للدرس بدلاً من ({html.escape(les)}):", InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    if data == "mgr_action_del_les":
        cat, les, cat_id = user.get("temp_data", {}).get("mgr_target_cat"), user.get("temp_data", {}).get("mgr_target_les"), user.get("temp_data", {}).get("mgr_target_cat_id")
        await db.library.delete_many({"category": cat, "lesson": les})
        await db.questions.delete_many({"category": cat, "lesson": les})
        await safe_edit(query, f"✅ تم حذف الدرس ({html.escape(les)}) بالكامل!", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data=f"mgr_cat_view_{cat_id}")]]))
        return

    if data == "admin_content_types" and await has_perm(user_id, "publish"):
        types = await db.content_types.find({}).to_list(length=None)
        btns = []
        for t in types:
            btns.append([
                InlineKeyboardButton(f"{t['icon']} {t['name']}", callback_data="ignore"),
                InlineKeyboardButton("✏️ تعديل", callback_data=f"editype_{t['_id']}"),
                InlineKeyboardButton("🗑️ حذف", callback_data=f"ask_deltype_{t['_id']}")
            ])
        btns.append([InlineKeyboardButton("➕ | إضافة نوع جديد", callback_data="add_type")])
        btns.append([InlineKeyboardButton("🔙 | رجوع للوحة", callback_data="admin_menu")])
        await safe_edit(query, "🎛️ <b>إدارة أنواع المحتوى (الأزرار):</b>\nقم بإضافة أو تعديل الأزرار التي ستظهر للطلاب:", InlineKeyboardMarkup(btns))
        return
    
    if data == "add_type":
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_TYPE_DATA"}}, upsert=True)
        await safe_edit(query, "✍️ أرسل <b>الاسم, الأيقونة</b> للنوع الجديد مفصولة بفاصلة\n(مثال: <code>بودكاست, 🎙️</code>):", InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    if data.startswith("deltype_"):
        t_id = data.replace("deltype_", "")
        await db.content_types.delete_one({"_id": t_id})
        await safe_edit(query, "✅ تم الحذف بنجاح!", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_content_types")]]))
        return

    if data.startswith("editype_"):
        t_id = data.replace("editype_", "")
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_EDIT_TYPE", "temp_data": {"edit_t_id": t_id}}}, upsert=True)
        await safe_edit(query, "✍️ أرسل <b>الاسم الجديد, الأيقونة الجديدة</b> مفصولة بفاصلة\n(مثال: <code>الكتاب الشامل, 📖</code>):", InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    if data == "admin_publishing_hub" and await has_perm(user_id, "publish"):
        btns = [
            [InlineKeyboardButton("🚀 | نشر درس للقناة", callback_data="admin_pub_menu")],
            [InlineKeyboardButton("📡 | إدارة قنوات النشر", callback_data="admin_channels")],
            [InlineKeyboardButton("🎨 | إدارة قوالب النشر", callback_data="admin_tpl_menu")],
            [InlineKeyboardButton("🔗 | تعديل تذييل النشر", callback_data="admin_edit_footer")],
            [InlineKeyboardButton("📊 | إنشاء استفتاء للقناة", callback_data="admin_poll")],
            [InlineKeyboardButton("🔙 | رجوع للوحة الإدارة", callback_data="admin_menu")]
        ]
        await safe_edit(query, "📢 <b>قسم النشر والقوالب:</b>\nجميع أدوات النشر وتخصيص القوالب في مكان واحد:", InlineKeyboardMarkup(btns))
        return

    if data == "admin_poll" and await has_perm(user_id, "publish"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_POLL_Q"}}, upsert=True)
        await safe_edit(query, "📊 أرسل الآن <b>سؤال الاستفتاء</b>:", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 تراجع", callback_data="admin_publishing_hub")]]))
        return

    if data == "admin_channels" and await has_perm(user_id, "publish"):
        channels_doc = await db.settings.find_one({"_id": "channels"})
        channels = channels_doc.get("list", []) if channels_doc else []
        btns = []
        for ch in channels:
            btns.append([InlineKeyboardButton(f"🗑️ حذف ({ch})", callback_data=f"delchan_{ch}")])
        btns.append([InlineKeyboardButton("➕ | إضافة قناة جديدة", callback_data="add_chan")])
        btns.append([InlineKeyboardButton("🔙 | رجوع لقسم النشر", callback_data="admin_publishing_hub")])
        await safe_edit(query, "📡 <b>إدارة قنوات النشر المتعددة:</b>\nأضف القنوات هنا لتتمكن من اختيارها عند النشر:", InlineKeyboardMarkup(btns))
        return

    if data == "add_chan" and await has_perm(user_id, "publish"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_CHAN_ID"}}, upsert=True)
        await safe_edit(query, "✍️ أرسل معرّف القناة (مثال: `@almashro` أو `-100123456`):", InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    if data.startswith("delchan_") and await has_perm(user_id, "publish"):
        ch = data.replace("delchan_", "")
        await db.settings.update_one({"_id": "channels"}, {"$pull": {"list": ch}})
        await safe_edit(query, f"✅ تم حذف القناة ({ch}) بنجاح!", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_channels")]]))
        return

    if data == "admin_tpl_menu" and await has_perm(user_id, "publish"):
        templates = await db.templates.find({}).to_list(length=None)
        btns = []
        for t in templates:
            btns.append([InlineKeyboardButton(f"📄 | {t['name']}", callback_data="ignore")])
            btns.append([InlineKeyboardButton("🗑️ حذف هذا القالب", callback_data=f"deltpl_{str(t['_id'])}")])
        btns.append([InlineKeyboardButton("➕ | إنشاء قالب جديد", callback_data="add_tpl")])
        btns.append([InlineKeyboardButton("🔙 | رجوع لقسم النشر", callback_data="admin_publishing_hub")])
        await safe_edit(query, "🎨 <b>إدارة قوالب النشر الديناميكية:</b>\nأنشئ قوالبك بمتغيرات ذكية:", InlineKeyboardMarkup(btns))
        return

    if data == "add_tpl" and await has_perm(user_id, "publish"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_TPL_NAME"}}, upsert=True)
        await safe_edit(query, "✍️ أرسل الآن <b>اسم القالب الجديد</b>\n(مثال: قالب خطب الجمعة، قالب السيرة):", InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    if data.startswith("deltpl_") and await has_perm(user_id, "publish"):
        tpl_id = data.replace("deltpl_", "")
        await db.templates.delete_one({"_id": ObjectId(tpl_id)})
        await safe_edit(query, "✅ تم حذف القالب بنجاح!", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع للقوالب", callback_data="admin_tpl_menu")]]))
        return

    if data == "admin_edit_footer" and await has_perm(user_id, "publish"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_FOOTER_TEXT"}}, upsert=True)
        msg = "✍️ أرسل الآن <b>النص مع الرابط</b> الذي تريده أن يظهر كـ (تذييل) أسفل الدروس المنشورة:\n\n<i>(الوضع الافتراضي الحالي سيكون هو النص القديم إذا لم تقم بتعديله)</i>"
        await safe_edit(query, msg, InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    if data == "admin_pub_menu" and await has_perm(user_id, "publish"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": ""}}, upsert=True)
        pipeline = [{"$group": {"_id": "$category", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        cats = await db.library.aggregate(pipeline).to_list(length=None)
        btns = [[InlineKeyboardButton(f"📁 | {c['_id']}", callback_data=f"pubc_{str(c['doc_id'])}")] for c in cats[:90] if c['_id'] and str(c['_id']).lower() != 'nan']
        btns.append([InlineKeyboardButton("🔙 | رجوع لقسم النشر", callback_data="admin_publishing_hub")])
        await safe_edit(query, "📢 <b>نشر درس:</b>\nاختر السلسلة التي تود نشر درس منها:", InlineKeyboardMarkup(btns))
        return

    if data.startswith("pubc_"):
        oid = get_safe_oid(data.replace("pubc_", ""))
        if not oid: return await safe_edit(query, "⚠️ القائمة قديمة، يرجى تحديث النظام بإرسال /start.")
        doc = await db.library.find_one({"_id": oid})
        if not doc: return await safe_edit(query, "⚠️ عذراً، لم يعد هذا العنصر متوفراً.")
        cat_name = doc["category"]
        pipeline = [{"$match": {"category": cat_name}}, {"$group": {"_id": "$lesson", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        lessons = await db.library.aggregate(pipeline).to_list(length=None)
        btns = [[InlineKeyboardButton(f"📖 | {idx}- {les['_id']}", callback_data=f"publ_{str(les['doc_id'])}")] for idx, les in enumerate(lessons[:90], 1)]
        btns.append([InlineKeyboardButton("🔙 | تراجع", callback_data="admin_pub_menu")])
        await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": {"pub_cat": cat_name, "pub_cat_id": str(oid)}}}, upsert=True)
        await safe_edit(query, f"📁 السلسلة: <b>{html.escape(cat_name)}</b>\nاختر الدرس المراد نشره:", InlineKeyboardMarkup(btns))
        return

    if data.startswith("publ_"):
        oid = get_safe_oid(data.replace("publ_", ""))
        if not oid: return await safe_edit(query, "⚠️ القائمة قديمة، يرجى تحديث النظام بإرسال /start.")
        doc = await db.library.find_one({"_id": oid})
        if not doc: return await safe_edit(query, "⚠️ عذراً، لم يعد هذا العنصر متوفراً.")
        
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "", "temp_data": {"pub_les": doc["lesson"]}}}, upsert=True)
        
        templates = await db.templates.find({}).to_list(length=None)
        btns = []
        for t in templates:
            btns.append([InlineKeyboardButton(f"📄 | {t['name']}", callback_data=f"pubfmt_tpl_{str(t['_id'])}")])
        btns.append([InlineKeyboardButton("📝 | قالب النص الكلاسيكي", callback_data="pubfmt_text")])
        btns.append([InlineKeyboardButton("🔲 | قالب الأزرار الشفافة", callback_data="pubfmt_btns")])
        btns.append([InlineKeyboardButton("❌ | إلغاء العملية", callback_data="admin_cancel")])
        await safe_edit(query, f"✅ تم اختيار: <b>{html.escape(doc['lesson'])}</b>\n\nاختر القالب الذي تفضله لتوليد المنشور:", InlineKeyboardMarkup(btns))
        return

    if data.startswith("pubfmt_"):
        fmt_type = data.replace("pubfmt_", "")
        temp_data = user.get("temp_data", {})
        temp_data["draft_format_key"] = fmt_type
        await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": temp_data}}, upsert=True)

        items = await db.library.find({"lesson": temp_data.get("pub_les", "عام")}).to_list(length=None)
        has_media = any(fix_link(item.get("file_id")) for item in items)
        
        if has_media:
            btns = [
                [InlineKeyboardButton("🖼️/🎬 إرفاق وسائط من الدرس", callback_data="pubmed_auto")],
                [InlineKeyboardButton("📝 نص فقط (بدون وسائط)", callback_data="pubmed_none")],
                [InlineKeyboardButton("❌ إلغاء العملية", callback_data="admin_cancel")]
            ]
            await safe_edit(query, "🎨 <b>تصميم المنشور:</b>\nهل تود إرفاق وسائط (صورة/فيديو) مع هذا المنشور لجعله أكثر جاذبية؟", InlineKeyboardMarkup(btns))
            return
        else: data = "pubmed_none" 

    if data.startswith("pubmed_"):
        media_choice = data.replace("pubmed_", "")
        temp_data = user.get("temp_data", {})
        temp_data["pub_media"] = media_choice
        fmt_type = temp_data.get("draft_format_key", "text")
        
        cat, les = temp_data.get("pub_cat", "عام"), temp_data.get("pub_les", "عام")
        date_txt, footer_content = get_auto_arabic_date(), await get_footer_text()

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
                        if media_choice == "auto" and not media_candidate: media_candidate = safe_link 
        
        temp_data["link_auto_media"] = media_candidate if media_choice == "auto" else None
        media_note = "📌 <code>[سيتم إرفاق وسائط الدرس إن وجدت]</code>\n\n" if media_choice == "auto" else ""

        draft_text = ""
        if fmt_type == "btns":
            draft_text = f"{cat} - {les}\n\nدرس اليوم {date_txt}\n\n{footer_content}"
        elif fmt_type == "text":
            draft_text = f"<b>{html.escape(cat)} - {html.escape(les)}</b>\n\nدرس اليوم {date_txt}\n\n"
            for t_name, t_link in dynamic_links.items():
                if t_link != ch_link: draft_text += f"<blockquote>{html.escape(t_name)} <a href='{t_link}'>إضغط هنا</a> ❞</blockquote>\n"
            draft_text += f"\n\n{html.escape(footer_content)}"
        elif fmt_type.startswith("tpl_"):
            tpl_id = fmt_type.replace("tpl_", "")
            tpl_doc = await db.templates.find_one({"_id": ObjectId(tpl_id)})
            if not tpl_doc: return await safe_edit(query, "⚠️ القالب غير موجود.")
            draft_text = tpl_doc["content"].replace("{سلسلة}", html.escape(cat)).replace("{درس}", html.escape(les)).replace("{تاريخ}", date_txt).replace("{تذييل}", html.escape(footer_content))
            for t_name, t_link in dynamic_links.items(): draft_text = draft_text.replace(f"{{{t_name}}}", t_link)

        temp_data["draft_format"] = "btns" if fmt_type == "btns" else ("html_text" if fmt_type == "text" else "html_dynamic")
        temp_data["draft_text"] = draft_text
        await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": temp_data}}, upsert=True)
        
        btns = [[InlineKeyboardButton("✅ | المتابعة لاختيار القناة", callback_data="pub_select_chan")], [InlineKeyboardButton("❌ | إلغاء", callback_data="admin_cancel")]]
        try:
            if fmt_type == "btns": await safe_edit(query, f"🔲 <b>معاينة المسودة (أزرار):</b>\n\n{media_note}{html.escape(draft_text)}", InlineKeyboardMarkup(btns))
            else: await safe_edit(query, f"📝 <b>معاينة المسودة:</b>\n\n{media_note}{draft_text}\n\n--- \nهل تريد المتابعة؟", InlineKeyboardMarkup(btns))
        except Exception as e: await safe_edit(query, f"❌ **خطأ في كود HTML للقالب!**\n\n<code>{e}</code>", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_tpl_menu")]]))
        return

    if data == "pub_select_chan":
        channels_doc = await db.settings.find_one({"_id": "channels"})
        channels = channels_doc.get("list", []) if channels_doc else []
        if CHANNEL_ID and CHANNEL_ID not in channels: channels.insert(0, CHANNEL_ID)
        
        btns = [[InlineKeyboardButton(f"📡 انشر في: {ch}", callback_data=f"pconf_{ch}")] for ch in channels]
        btns.append([InlineKeyboardButton("➕ إضافة قناة جديدة", callback_data="admin_channels")])
        btns.append([InlineKeyboardButton("🔙 تراجع للمسودة", callback_data="admin_pub_menu")])
        await safe_edit(query, "اختر <b>القناة</b> التي تريد النشر فيها الآن:", InlineKeyboardMarkup(btns))
        return

    if data.startswith("pconf_"):
        target_channel = data.replace("pconf_", "")
        temp_data = user.get("temp_data", {})
        draft_text, draft_format, media_link = temp_data.get("draft_text", ""), temp_data.get("draft_format", ""), temp_data.get("link_auto_media")
        
        inline_kb = None
        if draft_format == "btns":
            les = temp_data.get("pub_les", "")
            items = await db.library.find({"lesson": les}).to_list(length=None)
            types_docs = await db.content_types.find({}).to_list(length=None)
            inline_kb_arr, row = [], []
            for item in items:
                safe_link = fix_link(item.get("file_id"))
                t_name = next((t["name"] for t in types_docs if t["_id"] == str(item.get("type", ""))), "رابط")
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

            await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": {}}}, upsert=True)
            btns = [[InlineKeyboardButton("🔙 | العودة لقسم النشر", callback_data="admin_publishing_hub")]]
            
            if media_failed: await safe_edit(query, f"✅ <b>تم نشر النص في ({target_channel})!</b>\n\n⚠️ <i>ملاحظة:</i> لم يتم إرفاق الوسائط لأن الرابط يشير لرسالة محذوفة.", InlineKeyboardMarkup(btns))
            else: await safe_edit(query, f"🎉 <b>تم النشر بنجاح في ({target_channel})!</b>", InlineKeyboardMarkup(btns))
            return
        except Exception as e: 
            await safe_edit(query, f"❌ حدث خطأ.\nتأكد أن البوت (مشرف) في القناة المقصودة وأن المعرف صحيح.\n<code>{e}</code>", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 | العودة للوحة الإدارة", callback_data="admin_menu")]]))
            return

    # ================= 🌟 إضافة وتحديث أسئلة الدروس 🌟 =================
    if data == "admin_add_q" and await has_perm(user_id, "questions"):
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_Q_CAT"}}, upsert=True)
        pipeline = [{"$group": {"_id": "$category", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        cats = await db.library.aggregate(pipeline).to_list(length=None)
        btns = [[InlineKeyboardButton(f"📁 | {c['_id']}", callback_data=f"qaddc_{str(c['doc_id'])}")] for c in cats[:90] if c['_id'] and str(c['_id']).lower() != 'nan']
        btns.append([InlineKeyboardButton("🔙 | رجوع للوحة الإدارة", callback_data="admin_menu")])
        await safe_edit(query, "📝 <b>إضافة سؤال/اختبار:</b>\nاختر السلسلة:", InlineKeyboardMarkup(btns))
        return

    if data.startswith("qaddc_"):
        oid = get_safe_oid(data.replace("qaddc_", ""))
        if not oid: return await safe_edit(query, "⚠️ القائمة قديمة، يرجى تحديث النظام بإرسال /start.")
        doc = await db.library.find_one({"_id": oid})
        if not doc: return await safe_edit(query, "⚠️ عذراً، لم يعد هذا العنصر متوفراً.")
        cat_name = doc["category"]
        pipeline = [{"$match": {"category": cat_name}}, {"$group": {"_id": "$lesson", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        lessons = await db.library.aggregate(pipeline).to_list(length=None)
        btns = [[InlineKeyboardButton(f"📖 | {idx}- {les['_id']}", callback_data=f"qaddl_{str(les['doc_id'])}")] for idx, les in enumerate(lessons[:90], 1)]
        btns.append([InlineKeyboardButton("🔙 | تراجع", callback_data="admin_add_q")])
        await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": {"q_cat": cat_name}}}, upsert=True)
        await safe_edit(query, f"📁 السلسلة: <b>{html.escape(cat_name)}</b>\nاختر الدرس:", InlineKeyboardMarkup(btns))
        return

    if data.startswith("qaddl_"):
        oid = get_safe_oid(data.replace("qaddl_", ""))
        if not oid: return await safe_edit(query, "⚠️ القائمة قديمة، يرجى تحديث النظام بإرسال /start.")
        doc = await db.library.find_one({"_id": oid})
        if not doc: return await safe_edit(query, "⚠️ عذراً، لم يعد هذا العنصر متوفراً.")
        lesson_name = doc["lesson"]
        
        temp_data = user.get("temp_data", {})
        temp_data["q_les"] = lesson_name
        await db.users.update_one({"_id": user_id}, {"$set": {"temp_data": temp_data}}, upsert=True)
        
        btns = [
            [InlineKeyboardButton("✍️ إضافة سؤال واحد (يدوياً)", callback_data="qadd_manual")],
            [InlineKeyboardButton("📥 رفع اختبار كامل (ملف إكسل)", callback_data="qadd_excel")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]
        ]
        await safe_edit(query, f"📖 المحاضرة: <b>{html.escape(lesson_name)}</b>\n\nكيف تود إضافة الأسئلة؟", InlineKeyboardMarkup(btns))
        return

    if data == "qadd_manual":
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_Q_TEXT"}}, upsert=True)
        await safe_edit(query, "✍️ أرسل <b>نص السؤال</b>:", InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    if data == "qadd_excel":
        await db.users.update_one({"_id": user_id}, {"$set": {"state": "WAIT_Q_EXCEL"}}, upsert=True)
        msg = """📥 <b>رفع ملف إكسل لاختبار الدرس</b>

⚠️ <b>لتجنب أي أخطاء أثناء الرفع، يرجى تجهيز الملف كالتالي:</b>
1. يجب أن يكون الملف بصيغة <b>Excel (.xlsx)</b>.
2. يجب أن يحتوي <b>الصف الأول</b> على أسماء الأعمدة التالية بدقة:
   ▫️ <code>السؤال</code> : لكتابة نص السؤال.
   ▫️ <code>صحيح</code> : لكتابة الإجابة الصحيحة.
   ▫️ <code>خاطئة</code> أو <code>خطأ</code> : لكتابة الإجابات الخاطئة.

💡 <i>طريقة حذف سؤال:</i> اكتب نص السؤال، واكتب كلمة <code>حذف</code> في عمود "صحيح".

👇 <b>أرسل ملف الإكسل الآن كـ (مستند / Document):</b>"""
        await safe_edit(query, msg, InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_cancel")]]))
        return

    if data.startswith("cat_"):
        oid = get_safe_oid(data.replace("cat_", ""))
        if not oid: return await safe_edit(query, "⚠️ القائمة قديمة، يرجى تحديث النظام بإرسال /start.")
        doc = await db.library.find_one({"_id": oid})
        if not doc: return await safe_edit(query, "⚠️ عذراً، لم يعد هذا العنصر متوفراً.")
        cat_name = doc["category"]
        pipeline = [{"$match": {"category": cat_name}}, {"$group": {"_id": "$lesson", "doc_id": {"$first": "$_id"}}}, {"$sort": {"doc_id": 1}}]
        lessons = await db.library.aggregate(pipeline).to_list(length=None)
        btns = [[InlineKeyboardButton(f"📖 | {idx}- {les['_id']}", callback_data=f"les_{str(les['doc_id'])}")] for idx, les in enumerate(lessons[:90], 1)]
        btns.append([InlineKeyboardButton("🔙 | العودة للرئيسية", callback_data="main_menu")])
        await safe_edit(query, f"📂 <b>السلسلة:</b> {html.escape(cat_name)}\nاختر المحاضرة المطلوب:", InlineKeyboardMarkup(btns))
        return

    if data.startswith("les_"):
        doc_id = data.replace("les_", "")
        return await show_lesson_ui(context, chat_id, doc_id, message_id=query.message.message_id, user_id=user_id)

    if data.startswith("quizles_"):
        try: await context.bot.answer_callback_query(query.id, "🚀 جاري التجهيز...", show_alert=False)
        except: pass
        doc_id = data.replace("quizles_", "")
        oid = get_safe_oid(doc_id)
        if not oid: return await safe_edit(query, "⚠️ القائمة قديمة، يرجى تحديث النظام بإرسال /start.")
        doc = await db.library.find_one({"_id": oid})
        if not doc: return
        return await send_question(context, chat_id, lesson=doc.get("lesson"), user_id=user_id, msg_id=query.message.message_id, back_doc_id=doc_id)

    if data.startswith("ans_"):
        parts = data.split("_")
        is_correct = parts[1] == "1"
        q_id, ts = parts[2], int(parts[3])
        if int(time.time()) - ts > TIME_LIMIT or int(time.time()) - ts < 0: 
            await safe_edit(query, "⏳ <i>انتهى الوقت المخصص للإجابة!</i>")
            return
            
        new_kb = []
        for row in query.message.reply_markup.inline_keyboard[:-1]:
            new_row = []
            for b in row:
                if b.callback_data == data: new_row.append(InlineKeyboardButton(b.text + (" ✅" if is_correct else " ❌"), callback_data="ignore"))
                else: new_row.append(InlineKeyboardButton(b.text, callback_data="ignore"))
            new_kb.append(new_row)
            
        q_doc = await db.questions.find_one({"_id": ObjectId(q_id)})
        les_id = None
        if q_doc:
            lib_doc = await db.library.find_one({"lesson": q_doc["lesson"]})
            if lib_doc: les_id = str(lib_doc["_id"])
            
        if les_id:
            new_kb.append([InlineKeyboardButton("⏭️ السؤال التالي", callback_data=f"quizles_{les_id}")])
            new_kb.append([InlineKeyboardButton("🔙 إنهاء الاختبار", callback_data=f"les_{les_id}"), InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu")])
        else:
            new_kb.append(query.message.reply_markup.inline_keyboard[-1])
            
        await query.edit_message_reply_markup(InlineKeyboardMarkup(new_kb))
        asyncio.create_task(background_db_update(user_id, q_id=q_id, is_correct=is_correct))
        return

async def send_question(context, chat_id, lesson, user_id=None, msg_id=None, back_doc_id=None):
    if db is None: return
    user = await db.users.find_one({"_id": str(user_id)})
    answered = user.get("answered", []) if user else []
    all_qs = await db.questions.find({"lesson": lesson}).to_list(length=None)
    available = [q for q in all_qs if str(q['_id']) not in answered]
    
    if not available:
        txt = "🎉 <b>أتممت جميع أسئلة هذا الدرس بنجاح!</b>"
        btns = []
        if back_doc_id: btns.append([InlineKeyboardButton("🔙 | العودة للدرس", callback_data=f"les_{back_doc_id}")])
        btns.append([InlineKeyboardButton("🏠 | الرئيسية", callback_data="main_menu")])
        if msg_id: await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=msg_id, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
        else: 
            if user_id: await clean_chat_history(user_id, chat_id, context)
            sent_msg = await context.bot.send_message(chat_id, txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
            if user_id: await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)
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
    txt = f"📖 <b>المحاضرة:</b> {html.escape(lesson)}\n\n❓ <i>{html.escape(q['question'])}</i>\n\n⏱️ أمامك {TIME_LIMIT} ثانية للإجابة!"
    
    if msg_id: await context.bot.edit_message_text(txt, chat_id=chat_id, message_id=msg_id, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_kb))
    else: 
        if user_id: await clean_chat_history(user_id, chat_id, context)
        sent_msg = await context.bot.send_message(chat_id, txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_kb))
        if user_id: await db.users.update_one({"_id": user_id}, {"$set": {"last_msg_id": sent_msg.message_id}}, upsert=True)

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
