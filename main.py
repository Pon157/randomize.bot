import os
import random
import sqlite3
import logging
import json
import asyncio
import sys
import html as pyhtml
from datetime import datetime, timedelta
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types 
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    Message, 
    CallbackQuery, 
    ContentType
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# =================================================================
# 1. СИСТЕМНЫЕ НАСТРОЙКИ
# =================================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("LotteryMaster_FULL")

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
PR_CHAT_ID = int(os.getenv("PR_CHAT_ID", "0"))
LOT_CHANNEL = os.getenv("LOT_CHANNEL", "@lotsvitechek")

if not TOKEN:
    logger.critical("❌ ОШИБКА: Токен не найден в .env!")
    sys.exit(1)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# =================================================================
# 2. БАЗА ДАННЫХ
# =================================================================
def init_db():
    logger.info("Подключение к БД...")
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    
    # --- Таблицы Розыгрышей ---
    cur.execute("""CREATE TABLE IF NOT EXISTS lotteries (
        id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, entities TEXT, channels TEXT, 
        finish_type TEXT, finish_value TEXT, status TEXT DEFAULT 'active', 
        message_id INTEGER, photo TEXT, sticker TEXT, winners_count INTEGER DEFAULT 1, 
        participants_count INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    
    cur.execute("CREATE TABLE IF NOT EXISTS participants (user_id INTEGER, lot_id INTEGER, username TEXT, full_name TEXT, joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (user_id, lot_id))")
    cur.execute("CREATE TABLE IF NOT EXISTS winners (lot_id INTEGER, user_id INTEGER, win_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    
    # --- Таблица Юзеров ---
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, 
        balance INTEGER DEFAULT 0, referrer_id INTEGER DEFAULT 0, 
        refs_count INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    
    # --- Таблица Отзывов (с поддержкой типа отзыва) ---
    cur.execute("""CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, 
        target_id INTEGER, type TEXT DEFAULT 'win', 
        text TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    
    # --- Таблицы Покупок и Настроек ---
    cur.execute("""CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, service TEXT, 
        contact TEXT, links TEXT, status TEXT DEFAULT 'pending', 
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    
    cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    
    # Проверка структуры и миграции
    cur.execute("PRAGMA table_info(reviews)")
    cols = [c[1] for c in cur.fetchall()]
    if 'type' not in cols: cur.execute("ALTER TABLE reviews ADD COLUMN type TEXT DEFAULT 'win'")
    if 'target_id' not in cols: cur.execute("ALTER TABLE reviews ADD COLUMN target_id INTEGER DEFAULT 0")

    # Дефолтные настройки
    defaults = [
        ('price_text', '📋 <b>Прайс-лист:</b>\n\n🔹 Обычная лота: 500р / 250 Stars\n👑 VIP: 1000р / 500 Stars'),
        ('stars_link', 'https://t.me/change_me'),
        ('da_link', 'https://www.donationalerts.com/r/change_me')
    ]
    cur.executemany("INSERT OR IGNORE INTO settings VALUES (?, ?)", defaults)
    
    conn.commit()
    conn.close()

def db_query(query, params=(), fetchone=False, fetchall=False, commit=False):
    with sqlite3.connect("bot_database.db") as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, params)
        if commit: conn.commit()
        if fetchone: return cur.fetchone()
        if fetchall: return cur.fetchall()
        return cur.lastrowid

# =================================================================
# 3. МАШИНА СОСТОЯНИЙ (FSM)
# =================================================================
class CreateLot(StatesGroup):
    text, winners_count, channels, finish_type, value = State(), State(), State(), State(), State()

class EditLotState(StatesGroup):
    lot_id, field, new_value = State(), State(), State()

class OrderService(StatesGroup):
    choosing_service, entering_contact, entering_links = State(), State(), State()

class AdminSettings(StatesGroup):
    edit_key, new_value, refund_amount = State(), State(), State()

class LeaveReview(StatesGroup):
    target_id, type, text = State(), State(), State()

class PRApplication(StatesGroup):
    age, nickname, proofs = State(), State(), State()

class AdminDM(StatesGroup):
    user_id, text = State(), State()

class AdminBroadcast(StatesGroup):
    content = State()

# =================================================================
# 4. ЛОГИКА РОЗЫГРЫШЕЙ
# =================================================================
async def check_user_sub(user_id: int, channels_str: str):
    if not channels_str or channels_str.lower() in ['нет', 'none', '']: return True, []
    not_subscribed = []
    for channel in [c.strip() for c in channels_str.split(",") if c.strip()]:
        try:
            member = await bot.get_chat_member(channel, user_id)
            if member.status in ["left", "kicked"]: not_subscribed.append(channel)
        except: not_subscribed.append(channel)
    return len(not_subscribed) == 0, not_subscribed

async def update_lot_card(lot_id: int, count: int):
    lot = db_query("SELECT message_id FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or not lot['message_id']: return
    me = await bot.get_me()
    kb = InlineKeyboardBuilder().button(text=f"✅ Участвовать! ({count})", url=f"https://t.me/{me.username}?start=lot_{lot_id}").as_markup()
    try: await bot.edit_message_reply_markup(chat_id=LOT_CHANNEL, message_id=lot['message_id'], reply_markup=kb)
    except: pass

async def run_final_selection(lot_id: int):
    lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or lot['status'] != 'active': return
    
    participants = db_query("SELECT * FROM participants WHERE lot_id = ?", (lot_id,), fetchall=True)
    db_query("UPDATE lotteries SET status = 'closed' WHERE id = ?", (lot_id,), commit=True)
    
    if not participants:
        try: await bot.send_message(LOT_CHANNEL, f"⚠️ Лот #{lot_id} завершен без участников.", reply_to_message_id=lot['message_id'])
        except: await bot.send_message(LOT_CHANNEL, f"⚠️ Лот #{lot_id} завершен без участников.")
        return

    winners = random.sample(participants, min(len(participants), lot['winners_count']))
    mentions = []
    for w in winners:
        db_query("INSERT INTO winners (lot_id, user_id) VALUES (?,?)", (lot_id, w['user_id']), commit=True)
        mention = f"@{w['username']}" if w['username'] else f"<a href='tg://user?id={w['user_id']}'>{pyhtml.escape(w['full_name'])}</a>"
        mentions.append(mention)
        try:
            kb = InlineKeyboardBuilder().button(text="✍️ Оставить отзыв", callback_data=f"rev_win_{lot_id}").as_markup()
            await bot.send_message(w['user_id'], f"🎉 <b>ПОБЕДА!</b> Ты выиграл в лоте #{lot_id}!", reply_markup=kb, parse_mode="HTML")
        except: pass

    res_text = f"🎊 <b>ИТОГИ #{lot_id}</b>\n🏆 Победители: {', '.join(mentions)}"
    try: await bot.send_message(LOT_CHANNEL, res_text, parse_mode="HTML", reply_to_message_id=lot['message_id'])
    except: await bot.send_message(LOT_CHANNEL, res_text, parse_mode="HTML")

# =================================================================
# 5. СТАРТ И МЕНЮ
# =================================================================
@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    uid = message.from_user.id
    user = db_query("SELECT * FROM users WHERE user_id = ?", (uid,), fetchone=True)
    
    if not user:
        ref_id = int(command.args.replace("ref", "")) if command.args and command.args.startswith("ref") else 0
        db_query("INSERT INTO users (user_id, username, full_name, referrer_id) VALUES (?,?,?,?)",
                 (uid, message.from_user.username, message.from_user.full_name, ref_id), commit=True)
        if ref_id: 
            db_query("UPDATE users SET refs_count = refs_count + 1 WHERE user_id = ?", (ref_id,), commit=True)
            try: await bot.send_message(ref_id, "🤝 Новый реферал!")
            except: pass

    # Вход в лот
    if command.args and command.args.startswith("lot_"):
        try:
            lid = int(command.args.split("_")[1])
            lot = db_query("SELECT * FROM lotteries WHERE id=?", (lid,), fetchone=True)
            if not lot or lot['status'] == 'closed': return await message.answer("❌ Лот не найден или завершен.")
            
            check = db_query("SELECT 1 FROM participants WHERE user_id=? AND lot_id=?", (uid, lid), fetchone=True)
            if check: return await message.answer("⚠️ Вы уже участвуете.")

            is_sub, bad = await check_user_sub(uid, lot['channels'])
            if not is_sub:
                kb = InlineKeyboardBuilder()
                for c in bad: kb.button(text=f"Подписаться {c}", url=f"https://t.me/{c.replace('@','')}")
                kb.button(text="🔄 Проверить", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lid}")
                kb.adjust(1)
                return await message.answer("⚠️ Подпишитесь на каналы:", reply_markup=kb.as_markup())

            db_query("INSERT INTO participants (user_id, lot_id, username, full_name) VALUES (?,?,?,?)", (uid, lid, message.from_user.username, message.from_user.full_name), commit=True)
            cnt = db_query("SELECT COUNT(*) as c FROM participants WHERE lot_id=?", (lid,), fetchone=True)['c']
            db_query("UPDATE lotteries SET participants_count=? WHERE id=?", (cnt, lid), commit=True)
            await update_lot_card(lid, cnt)
            await message.answer("✅ Вы участвуете!")
            if lot['finish_type'] == 'count' and cnt >= int(lot['finish_value']): await run_final_selection(lid)
            return
        except: return await message.answer("Ошибка лота.")

    await show_main_menu(message)

async def show_main_menu(message: Message):
    user = db_query("SELECT balance FROM users WHERE user_id = ?", (message.from_user.id,), fetchone=True)
    bal = user['balance'] if user else 0
    kb = InlineKeyboardBuilder()
    kb.button(text=f"💎 Купить рекламу/лот", callback_data="buy_service")
    kb.button(text="📢 Розыгрыши", callback_data="active_lots")
    kb.button(text="💬 Отзывы", callback_data="view_reviews")
    kb.button(text="💼 Партнерка", callback_data="apply_pr")
    kb.button(text="📊 Профиль", callback_data="my_stats")
    if message.from_user.id in ADMIN_IDS: kb.button(text="🛠 Админка", callback_data="admin_main")
    kb.adjust(1, 2, 2, 1)
    await message.answer(f"👋 Привет! Баланс: <b>{bal}★</b>\nВыбери действие:", reply_markup=kb.as_markup(), parse_mode="HTML")

# =================================================================
# 6. СИСТЕМА ПОКУПКИ (ЗАКАЗЫ)
# =================================================================
@dp.callback_query(F.data == "buy_service")
async def buy_start(c: CallbackQuery, state: FSMContext):
    pt = db_query("SELECT value FROM settings WHERE key='price_text'", fetchone=True)['value']
    kb = InlineKeyboardBuilder()
    kb.button(text="🔹 Обычная лота", callback_data="svc_Обычная")
    kb.button(text="👑 VIP Лота", callback_data="svc_VIP")
    kb.button(text="🔙 Назад", callback_data="to_start")
    kb.adjust(1, 1, 1)
    await c.message.edit_text(f"{pt}\n\n⬇️ <b>Выберите услугу:</b>", reply_markup=kb.as_markup(), parse_mode="HTML")
    await state.set_state(OrderService.choosing_service)

@dp.callback_query(OrderService.choosing_service, F.data.startswith("svc_"))
async def svc_chosen(c: CallbackQuery, state: FSMContext):
    await state.update_data(svc=c.data.split("_")[1])
    await c.message.answer("📝 Введите контакт для связи (@username):")
    await state.set_state(OrderService.entering_contact)

@dp.message(OrderService.entering_contact)
async def contact_step(m: Message, state: FSMContext):
    await state.update_data(cnt=m.text)
    await m.answer("🔗 Пришлите ссылку на канал/бота:")
    await state.set_state(OrderService.entering_links)

@dp.message(OrderService.entering_links)
async def links_step(m: Message, state: FSMContext):
    d = await state.get_data()
    oid = db_query("INSERT INTO orders (user_id, service, contact, links) VALUES (?,?,?,?)", 
                   (m.from_user.id, d['svc'], d['cnt'], m.text), commit=True)
    
    akb = InlineKeyboardBuilder()
    akb.button(text="✅ Принять", callback_data=f"ord_ok_{oid}_{m.from_user.id}")
    akb.button(text="❌ Отклонить", callback_data=f"ord_no_{oid}_{m.from_user.id}")
    
    if PR_CHAT_ID:
        msg = f"📩 <b>ЗАКАЗ #{oid}</b>\n👤: {m.from_user.mention_html()}\n💎: {d['svc']}\n📞: {d['cnt']}\n🔗: {m.text}"
        await bot.send_message(PR_CHAT_ID, msg, reply_markup=akb.as_markup(), parse_mode="HTML")
    
    await m.answer("✅ Заявка отправлена модераторам!")
    await state.clear()

# --- Обработка заказа Админом ---
@dp.callback_query(F.data.startswith("ord_ok_"))
async def adm_ord_ok(c: CallbackQuery):
    _, _, oid, uid = c.data.split("_")
    s_l = db_query("SELECT value FROM settings WHERE key='stars_link'", fetchone=True)['value']
    d_l = db_query("SELECT value FROM settings WHERE key='da_link'", fetchone=True)['value']
    
    kb = InlineKeyboardBuilder()
    kb.button(text="⭐ Оплата Stars", url=s_l)
    kb.button(text="💳 DonationAlerts", url=d_l)
    kb.button(text="✅ Я ОПЛАТИЛ", callback_data=f"cli_paid_{oid}")
    kb.adjust(1)
    
    await bot.send_message(uid, f"🎉 Заявка #{oid} одобрена! Оплатите и нажмите кнопку:", reply_markup=kb.as_markup())
    await c.message.edit_text(f"{c.message.text}\n\n🟢 ОДОБРЕНО (Ждем оплату)")

@dp.callback_query(F.data.startswith("cli_paid_"))
async def client_paid(c: CallbackQuery):
    oid = c.data.split("_")[2]
    kb = InlineKeyboardBuilder().button(text="💰 Деньги пришли (Опубликовать)", callback_data=f"adm_fin_{oid}").as_markup()
    if PR_CHAT_ID: await bot.send_message(PR_CHAT_ID, f"‼️ Юзер оплатил заказ #{oid}", reply_markup=kb)
    await c.answer("Уведомление отправлено!")

@dp.callback_query(F.data.startswith("adm_fin_"))
async def adm_final(c: CallbackQuery):
    oid = c.data.split("_")[2]
    o = db_query("SELECT * FROM orders WHERE id=?", (oid,), fetchone=True)
    if o:
        # Публикация
        txt = f"🎰 <b>НОВАЯ РЕКЛАМНАЯ ЛОТА</b>\n\n💎 Тип: {o['service']}\n🔗 Ссылка: {o['links']}\n👤 Заказчик: {o['contact']}"
        await bot.send_message(LOT_CHANNEL, txt, parse_mode="HTML")
        
        # Кнопка отзыва
        kb = InlineKeyboardBuilder().button(text="✍️ Оставить отзыв о покупке", callback_data=f"rev_ord_{oid}").as_markup()
        await bot.send_message(o['user_id'], f"🚀 Ваша лота #{oid} опубликована! Теперь вы можете оставить отзыв.", reply_markup=kb)
        
        db_query("UPDATE orders SET status='published' WHERE id=?", (oid,), commit=True)
    await c.message.edit_text(f"{c.message.text}\n\n✅ ОПУБЛИКОВАНО")

@dp.callback_query(F.data.startswith("ord_no_"))
async def adm_reject(c: CallbackQuery):
    _, _, oid, uid = c.data.split("_")
    kb = InlineKeyboardBuilder().button(text="💰 Возврат на баланс", callback_data=f"ref_{uid}_{oid}").as_markup()
    await bot.send_message(uid, f"❌ Заявка #{oid} отклонена.")
    await c.message.edit_text(f"{c.message.text}\n\n🔴 ОТКЛОНЕНО", reply_markup=kb)

@dp.callback_query(F.data.startswith("ref_"))
async def ref_start(c: CallbackQuery, state: FSMContext):
    _, uid, oid = c.data.split("_")
    await state.update_data(ruid=uid, roid=oid)
    await c.message.answer("Сумма возврата (число):")
    await state.set_state(AdminSettings.refund_amount)

@dp.message(AdminSettings.refund_amount)
async def ref_end(m: Message, state: FSMContext):
    if not m.text.isdigit(): return
    d = await state.get_data()
    db_query("UPDATE users SET balance = balance + ? WHERE user_id = ?", (int(m.text), d['ruid']), commit=True)
    await bot.send_message(d['ruid'], f"💰 Вам начислен возврат {m.text}★ за заказ #{d['roid']}")
    await m.answer("✅ Возврат выполнен."); await state.clear()

# =================================================================
# 7. ОТЗЫВЫ (Универсальные)
# =================================================================
@dp.callback_query(F.data.startswith("rev_"))
async def rev_start(c: CallbackQuery, state: FSMContext):
    # rev_win_123 или rev_ord_45
    p = c.data.split("_")
    rtype, tid = p[1], p[2]
    
    # Проверка дубля
    col = "target_id"
    check = db_query(f"SELECT 1 FROM reviews WHERE user_id=? AND target_id=? AND type=?", (c.from_user.id, tid, rtype), fetchone=True)
    if check: return await c.answer("Вы уже оставили отзыв!", show_alert=True)
    
    await state.update_data(rt=rtype, tid=tid)
    await c.message.answer(f"✍️ Напишите ваш отзыв ({'о победе' if rtype=='win' else 'о покупке'}):")
    await state.set_state(LeaveReview.text)

@dp.message(LeaveReview.text)
async def rev_save(m: Message, state: FSMContext):
    d = await state.get_data()
    db_query("INSERT INTO reviews (user_id, target_id, type, text) VALUES (?,?,?,?)", 
             (m.from_user.id, d['tid'], d['rt'], m.text), commit=True)
    await m.answer("✅ Отзыв опубликован!"); await state.clear()

@dp.callback_query(F.data == "view_reviews")
async def show_reviews(c: CallbackQuery):
    rvs = db_query("SELECT r.text, r.type, u.full_name FROM reviews r JOIN users u ON r.user_id=u.user_id ORDER BY r.id DESC LIMIT 10", fetchall=True)
    kb = InlineKeyboardBuilder().button(text="🔙", callback_data="to_start").as_markup()
    if not rvs: return await c.message.edit_text("Отзывов нет.", reply_markup=kb)
    txt = "💬 <b>ОТЗЫВЫ:</b>\n\n"
    for r in rvs:
        icon = "🏆" if r['type']=='win' else "💎"
        txt += f"{icon} <b>{pyhtml.escape(r['full_name'])}</b>:\n<i>{pyhtml.escape(r['text'])}</i>\n\n"
    await c.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")

# =================================================================
# 8. АДМИН ПАНЕЛЬ
# =================================================================
@dp.callback_query(F.data == "admin_main")
async def adm_main(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS: return
    kb = [
        [InlineKeyboardButton(text="➕ Создать Лот", callback_data="adm_create")],
        [InlineKeyboardButton(text="📝 Лоты", callback_data="adm_lots"), InlineKeyboardButton(text="📨 Рассылка", callback_data="adm_br")],
        [InlineKeyboardButton(text="💲 Прайс", callback_data="set_edit_price_text"), InlineKeyboardButton(text="⭐ Stars URL", callback_data="set_edit_stars_link")],
        [InlineKeyboardButton(text="💳 DA URL", callback_data="set_edit_da_link")],
        [InlineKeyboardButton(text="🔙", callback_data="to_start")]
    ]
    await c.message.edit_text("🛠 <b>АДМИНКА</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")

# --- Редактор настроек ---
@dp.callback_query(F.data.startswith("set_edit_"))
async def adm_set_start(c: CallbackQuery, state: FSMContext):
    k = c.data.replace("set_edit_", "")
    await state.update_data(k=k)
    await c.message.answer(f"Введите новое значение для {k}:")
    await state.set_state(AdminSettings.new_value)

@dp.message(AdminSettings.new_value)
async def adm_set_save(m: Message, state: FSMContext):
    d = await state.get_data()
    val = m.html_text if d['k']=='price_text' else m.text
    db_query("REPLACE INTO settings (key, value) VALUES (?,?)", (d['k'], val), commit=True)
    await m.answer("✅ Сохранено"); await state.clear()

# --- Создание лота ---
@dp.callback_query(F.data == "adm_create")
async def cr_1(c: CallbackQuery, state: FSMContext):
    await state.set_state(CreateLot.text); await c.message.answer("1. Пост (Текст/Фото):")

@dp.message(CreateLot.text)
async def cr_2(m: Message, state: FSMContext):
    ents = json.dumps([e.model_dump(mode='json') for e in (m.entities or m.caption_entities or [])])
    pd = {"text": m.caption or m.text or "", "ent": ents, "ph": m.photo[-1].file_id if m.photo else None}
    await state.update_data(pd=pd)
    await state.set_state(CreateLot.winners_count); await m.answer("2. Победителей:")

@dp.message(CreateLot.winners_count)
async def cr_3(m: Message, state: FSMContext):
    if not m.text.isdigit(): return
    await state.update_data(wc=int(m.text))
    await state.set_state(CreateLot.channels); await m.answer("3. Каналы (@a,@b) или нет:")

@dp.message(CreateLot.channels)
async def cr_4(m: Message, state: FSMContext):
    await state.update_data(ch=m.text)
    kb = InlineKeyboardBuilder(); kb.button(text="Время", callback_data="ft_time"); kb.button(text="Люди", callback_data="ft_count")
    await m.answer("4. Тип финиша:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("ft_"))
async def cr_5(c: CallbackQuery, state: FSMContext):
    t = "time" if c.data=="ft_time" else "count"
    await state.update_data(ft=t)
    await c.message.edit_text("Часов?" if t=="time" else "Сколько людей?")
    await state.set_state(CreateLot.value)

@dp.message(CreateLot.value)
async def cr_fin(m: Message, state: FSMContext):
    if not m.text.isdigit(): return
    d = await state.get_data()
    val = (datetime.now()+timedelta(hours=int(m.text))).strftime("%d.%m.%Y %H:%M") if d['ft']=="time" else int(m.text)
    lid = db_query("INSERT INTO lotteries (text, entities, channels, finish_type, finish_value, photo, winners_count) VALUES (?,?,?,?,?,?,?)",
             (d['pd']['text'], d['pd']['ent'], d['ch'], d['ft'], val, d['pd']['ph'], d['wc']), commit=True)
    me = await bot.get_me()
    kb = InlineKeyboardBuilder().button(text="✅ Участвовать!", url=f"https://t.me/{me.username}?start=lot_{lid}").as_markup()
    try:
        if d['pd']['ph']: sent = await bot.send_photo(LOT_CHANNEL, d['pd']['ph'], caption=d['pd']['text'], reply_markup=kb)
        else: sent = await bot.send_message(LOT_CHANNEL, d['pd']['text'], reply_markup=kb)
        db_query("UPDATE lotteries SET message_id=? WHERE id=?", (sent.message_id, lid), commit=True)
        await m.answer("✅ Опубликовано!")
    except Exception as e: await m.answer(f"Err: {e}")
    await state.clear()

# --- Рассылка ---
@dp.callback_query(F.data == "adm_br")
async def br_1(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminBroadcast.content); await c.message.answer("Пост:")
@dp.message(AdminBroadcast.content)
async def br_2(m: Message, state: FSMContext):
    u = db_query("SELECT user_id FROM users", fetchall=True)
    await m.answer(f"Рассылка {len(u)}...")
    for x in u:
        try: await m.copy_to(x['user_id']); await asyncio.sleep(0.05)
        except: pass
    await m.answer("Готово"); await state.clear()

# --- Управление лотами ---
@dp.callback_query(F.data == "adm_lots")
async def m_lots(c: CallbackQuery):
    l = db_query("SELECT * FROM lotteries ORDER BY id DESC LIMIT 10", fetchall=True)
    kb = InlineKeyboardBuilder()
    for x in l: kb.button(text=f"#{x['id']} ({x['status']})", callback_data=f"man_{x['id']}")
    kb.button(text="🔙", callback_data="admin_main"); kb.adjust(1)
    await c.message.edit_text("Лоты:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("man_"))
async def m_one(c: CallbackQuery):
    lid = c.data.split("_")[1]
    kb = InlineKeyboardBuilder().button(text="🛑 СТОП", callback_data=f"stop_{lid}").button(text="🔙", callback_data="adm_lots").adjust(1).as_markup()
    await c.message.edit_text(f"Лот #{lid}", reply_markup=kb)

@dp.callback_query(F.data.startswith("stop_"))
async def stop_l(c: CallbackQuery):
    lid = int(c.data.split("_")[1])
    await run_final_selection(lid)
    await c.answer("Остановлено")

# =================================================================
# 9. ОСТАЛЬНОЕ
# =================================================================
@dp.callback_query(F.data == "to_start")
async def back(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.delete(); await cmd_start(c.message, CommandObject(command="start"), state)

@dp.callback_query(F.data == "active_lots")
async def act(c: CallbackQuery):
    l = db_query("SELECT * FROM lotteries WHERE status='active' LIMIT 10", fetchall=True)
    if not l: return await c.answer("Пусто", show_alert=True)
    kb = InlineKeyboardBuilder()
    me = await bot.get_me()
    for x in l: kb.button(text=f"Лот #{x['id']}", url=f"https://t.me/{me.username}?start=lot_{x['id']}")
    kb.button(text="🔙", callback_data="to_start"); kb.adjust(1)
    await c.message.edit_text("Активные:", reply_markup=kb.as_markup())

@dp.callback_query(F.data == "my_stats")
async def stats(c: CallbackQuery):
    u = db_query("SELECT * FROM users WHERE user_id=?", (c.from_user.id,), fetchone=True)
    p = db_query("SELECT COUNT(*) as c FROM participants WHERE user_id=?", (c.from_user.id,), fetchone=True)['c']
    w = db_query("SELECT COUNT(*) as c FROM winners WHERE user_id=?", (c.from_user.id,), fetchone=True)['c']
    me = await bot.get_me()
    t = f"👤 ID: {c.from_user.id}\nУчастий: {p} | Побед: {w}\nРефералов: {u['refs_count']}\n🔗 `https://t.me/{me.username}?start=ref{c.from_user.id}`"
    kb = InlineKeyboardBuilder().button(text="🔙", callback_data="to_start").as_markup()
    await c.message.edit_text(t, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "apply_pr")
async def pr_1(c: CallbackQuery, state: FSMContext):
    await state.set_state(PRApplication.age); await c.message.edit_text("Возраст?")
@dp.message(PRApplication.age)
async def pr_2(m: Message, state: FSMContext):
    await state.update_data(age=m.text); await state.set_state(PRApplication.nickname); await m.answer("Ссылка/Ник?")
@dp.message(PRApplication.nickname)
async def pr_3(m: Message, state: FSMContext):
    await state.update_data(nick=m.text); await state.set_state(PRApplication.proofs); await m.answer("Скрин статы:")
@dp.message(PRApplication.proofs, F.content_type == ContentType.PHOTO)
async def pr_4(m: Message, state: FSMContext):
    d = await state.get_data()
    if PR_CHAT_ID: await bot.send_photo(PR_CHAT_ID, m.photo[-1].file_id, caption=f"PR заявка\n{m.from_user.mention_html()}\nAge: {d['age']}\nLink: {d['nick']}")
    await m.answer("✅ Отправлено"); await state.clear()

# --- ЛС юзеру (админ) ---
@dp.callback_query(F.data == "adm_dm_user")
async def dm_1(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS: return
    await state.set_state(AdminDM.user_id); await c.message.answer("ID юзера:")
@dp.message(AdminDM.user_id)
async def dm_2(m: Message, state: FSMContext):
    await state.update_data(uid=m.text); await state.set_state(AdminDM.text); await m.answer("Текст:")
@dp.message(AdminDM.text)
async def dm_3(m: Message, state: FSMContext):
    d = await state.get_data()
    try: await bot.send_message(d['uid'], f"📩 <b>Админ:</b>\n{m.text}", parse_mode="HTML"); await m.answer("✅")
    except: await m.answer("❌")
    await state.clear()

async def time_monitor():
    while True:
        try:
            lots = db_query("SELECT * FROM lotteries WHERE status='active' AND finish_type='time'", fetchall=True)
            for l in lots:
                if datetime.now() >= datetime.strptime(l['finish_value'], "%d.%m.%Y %H:%M"): await run_final_selection(l['id'])
        except: pass
        await asyncio.sleep(60)

async def main():
    init_db()
    asyncio.create_task(time_monitor())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
