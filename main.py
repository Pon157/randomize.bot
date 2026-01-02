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
# 1. СИСТЕМНЫЕ НАСТРОЙКИ И ЛОГИРОВАНИЕ
# =================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("LotteryMaster_FULL")

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
PR_CHAT_ID = int(os.getenv("PR_CHAT_ID", "0"))
LOT_CHANNEL = os.getenv("LOT_CHANNEL", "@lotsvitechek")

if not TOKEN:
    logger.critical("❌ ОШИБКА: Токен не найден в .env файле!")
    sys.exit(1)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# =================================================================
# 2. БАЗА ДАННЫХ
# =================================================================
def init_db():
    logger.info("Подключение к базе данных...")
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    
    # Таблицы для лотерей
    cur.execute("""
    CREATE TABLE IF NOT EXISTS lotteries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT,
        entities TEXT,
        channels TEXT,
        finish_type TEXT,
        finish_value TEXT,
        status TEXT DEFAULT 'active',
        message_id INTEGER,
        photo TEXT,
        sticker TEXT,
        winners_count INTEGER DEFAULT 1,
        participants_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS participants (
        user_id INTEGER,
        lot_id INTEGER,
        username TEXT,
        full_name TEXT,
        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, lot_id)
    )""")
    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS winners (
        lot_id INTEGER,
        user_id INTEGER,
        win_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        referrer_id INTEGER DEFAULT 0,
        refs_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        lot_id INTEGER,
        text TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # --- НОВЫЕ ТАБЛИЦЫ ДЛЯ ОПЛАТЫ ---
    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        service TEXT,
        contact TEXT,
        links TEXT,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, 
        value TEXT
    )""")

    # Дефолтные настройки, если их нет
    defaults = [
        ('price_text', '📋 <b>Прайс-лист:</b>\n\n🔹 Обычная лота: <b>500 руб</b> / <b>250 Stars</b>\n👑 VIP Лота: <b>1000 руб</b> / <b>500 Stars</b>'),
        ('stars_link', 'https://t.me/change_me_in_admin_panel'),
        ('da_link', 'https://www.donationalerts.com/r/change_me')
    ]
    cur.executemany("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", defaults)

    # Проверка столбцов у юзеров
    cur.execute("PRAGMA table_info(users)")
    cols = [c[1] for c in cur.fetchall()]
    if 'referrer_id' not in cols: cur.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER DEFAULT 0")
    if 'refs_count' not in cols: cur.execute("ALTER TABLE users ADD COLUMN refs_count INTEGER DEFAULT 0")

    conn.commit()
    conn.close()
    logger.info("БД готова к работе.")

def db_query(query, params=(), fetchone=False, fetchall=False, commit=False):
    try:
        with sqlite3.connect("bot_database.db") as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(query, params)
            if commit: conn.commit()
            if fetchone: return cur.fetchone()
            if fetchall: return cur.fetchall()
            return cur.lastrowid
    except Exception as e:
        logger.error(f"SQL Error: {e} | Query: {query}")
        return None

# =================================================================
# 3. МАШИНА СОСТОЯНИЙ (FSM)
# =================================================================
# --- Существующие ---
class CreateLot(StatesGroup):
    text, winners_count, channels, finish_type, value = State(), State(), State(), State(), State()

class EditLotState(StatesGroup):
    lot_id, field_to_edit, new_value, finish_type_cache = State(), State(), State(), State()

class PRApplication(StatesGroup):
    age, nickname, proofs = State(), State(), State()

class LeaveReview(StatesGroup):
    lot_id, text = State(), State()

class BroadcastState(StatesGroup):
    content = State()

class AdminSearch(StatesGroup):
    query = State()

class AdminDM(StatesGroup):
    user_id, message_text = State(), State()

# --- НОВЫЕ (ДЛЯ ПОКУПКИ И НАСТРОЕК) ---
class OrderService(StatesGroup):
    choosing_service = State()
    entering_contact = State()
    entering_links = State()

class AdminSettings(StatesGroup):
    edit_key = State()
    new_value = State()

# =================================================================
# 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (ЛОТЕРЕИ)
# =================================================================
async def check_user_sub(user_id: int, channels_str: str):
    if not channels_str or channels_str.lower() in ['нет', 'none', '']: return True, []
    not_subscribed = []
    channels_list = [c.strip() for c in channels_str.split(",") if c.strip()]
    for channel in channels_list:
        try:
            member = await bot.get_chat_member(channel, user_id)
            if member.status in ["left", "kicked", "restricted"]:
                if member.status == "restricted" and not member.is_member: not_subscribed.append(channel)
                elif member.status in ["left", "kicked"]: not_subscribed.append(channel)
        except: not_subscribed.append(channel)
    return len(not_subscribed) == 0, not_subscribed

async def update_lot_card(lot_id: int, count: int):
    lot = db_query("SELECT message_id FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or not lot['message_id']: return
    me = await bot.get_me()
    kb = InlineKeyboardBuilder()
    kb.button(text=f"✅ Участвовать! ({count})", url=f"https://t.me/{me.username}?start=lot_{lot_id}")
    try: await bot.edit_message_reply_markup(chat_id=LOT_CHANNEL, message_id=lot['message_id'], reply_markup=kb.as_markup())
    except: pass

async def run_final_selection(lot_id: int):
    lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot: return
    participants = db_query("SELECT * FROM participants WHERE lot_id = ?", (lot_id,), fetchall=True)
    db_query("UPDATE lotteries SET status = 'closed' WHERE id = ?", (lot_id,), commit=True)
    
    if not participants:
        try: await bot.send_message(LOT_CHANNEL, f"⚠️ Розыгрыш #{lot_id} завершен. Участников нет.", reply_to_message_id=lot['message_id'])
        except: await bot.send_message(LOT_CHANNEL, f"⚠️ Розыгрыш #{lot_id} завершен. Участников нет.")
        return

    count_to_win = min(len(participants), lot['winners_count'])
    winners_list = random.sample(participants, count_to_win)
    mentions = []
    for winner in winners_list:
        db_query("INSERT INTO winners (lot_id, user_id) VALUES (?,?)", (lot_id, winner['user_id']), commit=True)
        safe_name = pyhtml.escape(winner['full_name'])
        mention = f"@{winner['username']}" if winner['username'] else f"<a href='tg://user?id={winner['user_id']}'>{safe_name}</a>"
        mentions.append(mention)
        try:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✍️ Оставить отзыв", callback_data=f"rev_{lot_id}")]])
            await bot.send_message(winner['user_id'], f"🎉 ПОЗДРАВЛЯЕМ! Вы выиграли в розыгрыше #{lot_id}!", reply_markup=kb)
        except: pass 

    result_text = (f"🎊 <b>ИТОГИ РОЗЫГРЫША #{lot_id}</b>\n\n🏆 Победители: {', '.join(mentions)}\n📊 Всего участников: {len(participants)}")
    try: await bot.send_message(LOT_CHANNEL, result_text, parse_mode="HTML", reply_to_message_id=lot['message_id'])
    except: await bot.send_message(LOT_CHANNEL, result_text, parse_mode="HTML")

# =================================================================
# 5. ГЛАВНЫЕ ХЕНДЛЕРЫ (START)
# =================================================================
@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    uid = message.from_user.id
    args = command.args
    user = db_query("SELECT * FROM users WHERE user_id = ?", (uid,), fetchone=True)
    if not user:
        ref_id = 0
        if args and args.startswith("ref"):
            try: ref_id = int(args.replace("ref", ""))
            except: ref_id = 0
        db_query("INSERT INTO users (user_id, username, full_name, referrer_id) VALUES (?,?,?,?)", (uid, message.from_user.username, message.from_user.full_name, ref_id), commit=True)
        if ref_id != 0:
            db_query("UPDATE users SET refs_count = refs_count + 1 WHERE user_id = ?", (ref_id,), commit=True)
            try: await bot.send_message(ref_id, "🤝 <b>У вас новый реферал!</b>", parse_mode="HTML")
            except: pass

    # Логика входа в лот
    if args and args.startswith("lot_"):
        try:
            lot_id = int(args.split("_")[1])
            lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
            if not lot: return await message.answer("❌ Розыгрыш не найден.")
            if lot['status'] == 'closed': return await message.answer("❌ Розыгрыш завершен.")
            check = db_query("SELECT 1 FROM participants WHERE user_id=? AND lot_id=?", (uid, lot_id), fetchone=True)
            if check: return await message.answer("⚠️ Вы уже участвуете!")

            is_sub, bad_channels = await check_user_sub(uid, lot['channels'])
            if not is_sub:
                kb = InlineKeyboardBuilder()
                for ch in bad_channels: kb.button(text=f"Подписаться {ch}", url=f"https://t.me/{ch.replace('@','')}")
                kb.button(text="🔄 ПРОВЕРИТЬ", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")
                kb.adjust(1)
                return await message.answer("⚠️ Подпишитесь на каналы:", reply_markup=kb.as_markup())

            db_query("INSERT INTO participants (user_id, lot_id, username, full_name) VALUES (?,?,?,?)", (uid, lot_id, message.from_user.username, message.from_user.full_name), commit=True)
            cnt = db_query("SELECT COUNT(*) as c FROM participants WHERE lot_id=?", (lot_id,), fetchone=True)['c']
            db_query("UPDATE lotteries SET participants_count=? WHERE id=?", (cnt, lot_id), commit=True)
            await update_lot_card(lot_id, cnt)
            if lot['finish_type'] == 'count' and cnt >= int(lot['finish_value']): await run_final_selection(lot_id)
            return await message.answer(f"✅ Вы в игре (Лот #{lot_id})!")
        except Exception as e: return await message.answer("Ошибка.")

    # ГЛАВНОЕ МЕНЮ
    kb = [
        [InlineKeyboardButton(text="💎 Купить рекламу/лот", callback_data="buy_service")],
        [InlineKeyboardButton(text="📢 Активные розыгрыши", callback_data="active_lots"), 
         InlineKeyboardButton(text="💬 Отзывы", callback_data="view_reviews")],
        [InlineKeyboardButton(text="💼 Партнерка", callback_data="apply_pr"),
         InlineKeyboardButton(text="📊 Профиль", callback_data="my_stats")]
    ]
    if uid in ADMIN_IDS:
        kb.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_main")])
    
    await message.answer(f"👋 Привет, {pyhtml.escape(message.from_user.first_name)}! Я бот для розыгрышей и рекламы.", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

# =================================================================
# 6. СИСТЕМА ПОКУПКИ ЛОТ (ЗАКАЗЫ) - НОВОЕ
# =================================================================
@dp.callback_query(F.data == "buy_service")
async def buy_service_start(c: CallbackQuery, state: FSMContext):
    # Берем прайс из настроек
    price_row = db_query("SELECT value FROM settings WHERE key='price_text'", fetchone=True)
    price_text = price_row['value'] if price_row else "Прайс не установлен."

    kb = InlineKeyboardBuilder()
    kb.button(text="🔹 Обычная лота", callback_data="svc_Обычная")
    kb.button(text="👑 VIP Лота", callback_data="svc_VIP")
    kb.button(text="🔙 Назад", callback_data="to_start")
    kb.adjust(1, 1, 1)

    await c.message.edit_text(price_text + "\n\n⬇️ <b>Выберите услугу:</b>", reply_markup=kb.as_markup(), parse_mode="HTML")
    await state.set_state(OrderService.choosing_service)

@dp.callback_query(OrderService.choosing_service, F.data.startswith("svc_"))
async def svc_chosen(c: CallbackQuery, state: FSMContext):
    service = c.data.split("_")[1]
    await state.update_data(service=service)
    await c.message.answer("📝 <b>Напишите ваш @username или ссылку на ЛС для связи:</b>", parse_mode="HTML")
    await state.set_state(OrderService.entering_contact)

@dp.message(OrderService.entering_contact)
async def contact_entered(m: Message, state: FSMContext):
    await state.update_data(contact=m.text)
    await m.answer("🔗 <b>Пришлите ссылку на канал/бота, для которого создаем лоту:</b>", parse_mode="HTML")
    await state.set_state(OrderService.entering_links)

@dp.message(OrderService.entering_links)
async def links_entered(m: Message, state: FSMContext):
    data = await state.get_data()
    # Сохраняем заказ
    oid = db_query("INSERT INTO orders (user_id, service, contact, links) VALUES (?,?,?,?)", 
                   (m.from_user.id, data['service'], data['contact'], m.text), commit=True)
    
    # Кнопки для админа (в PR чате)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Принять", callback_data=f"ord_ok_{oid}_{m.from_user.id}")
    kb.button(text="❌ Отклонить", callback_data=f"ord_no_{oid}_{m.from_user.id}")
    
    msg_text = (
        f"📩 <b>НОВЫЙ ЗАКАЗ #{oid}</b>\n\n"
        f"👤 Юзер: {m.from_user.mention_html()}\n"
        f"💎 Услуга: {data['service']}\n"
        f"📞 Контакт: {data['contact']}\n"
        f"🔗 Ссылка: {m.text}"
    )

    if PR_CHAT_ID:
        await bot.send_message(PR_CHAT_ID, msg_text, reply_markup=kb.as_markup(), parse_mode="HTML")
    else:
        logger.warning("PR_CHAT_ID не установлен, заказ некуда отправить.")

    await m.answer("✅ <b>Заявка отправлена!</b> Ожидайте сообщения от администратора.", parse_mode="HTML")
    await state.clear()

# --- МОДЕРАЦИЯ И ОПЛАТА ---
@dp.callback_query(F.data.startswith("ord_ok_"))
async def admin_approve_order(c: CallbackQuery):
    _, _, oid, uid = c.data.split("_")
    
    # Берем ссылки из настроек
    s_link = db_query("SELECT value FROM settings WHERE key='stars_link'", fetchone=True)['value']
    d_link = db_query("SELECT value FROM settings WHERE key='da_link'", fetchone=True)['value']

    kb = InlineKeyboardBuilder()
    kb.button(text="⭐ Оплатить Stars", url=s_link)
    kb.button(text="💳 DonationAlerts (Рубли)", url=d_link)
    kb.button(text="✅ Я ОПЛАТИЛ", callback_data=f"cli_paid_{oid}")
    kb.adjust(1)

    await bot.send_message(uid, f"🎉 <b>Ваша заявка #{oid} одобрена!</b>\n\nВыберите способ оплаты и после перевода нажмите кнопку подтверждения:", reply_markup=kb.as_markup(), parse_mode="HTML")
    await c.message.edit_text(f"{c.message.text}\n\n🟢 <b>ОДОБРЕНО (Ждем оплату)</b>", parse_mode="HTML")

@dp.callback_query(F.data.startswith("ord_no_"))
async def admin_reject_order(c: CallbackQuery):
    _, _, oid, uid = c.data.split("_")
    await bot.send_message(uid, f"❌ Ваша заявка #{oid} была отклонена администратором.")
    await c.message.edit_text(f"{c.message.text}\n\n🔴 <b>ОТКЛОНЕНО</b>", parse_mode="HTML")

@dp.callback_query(F.data.startswith("cli_paid_"))
async def client_confirm_pay(c: CallbackQuery):
    oid = c.data.split("_")[2]
    
    kb = InlineKeyboardBuilder()
    kb.button(text="💰 Деньги пришли (Выставить лот)", callback_data=f"adm_fin_{oid}")
    
    if PR_CHAT_ID:
        await bot.send_message(PR_CHAT_ID, f"‼️ <b>Клиент подтвердил оплату заказа #{oid}!</b>\nПроверьте кошельки.", reply_markup=kb.as_markup(), parse_mode="HTML")
    
    await c.message.edit_text("⏳ Уведомление отправлено админу. Ожидайте публикации.", reply_markup=None)

@dp.callback_query(F.data.startswith("adm_fin_"))
async def admin_finalize_order(c: CallbackQuery):
    oid = c.data.split("_")[2]
    order = db_query("SELECT * FROM orders WHERE id=?", (oid,), fetchone=True)
    
    if order:
        # Пост в канал
        post_text = (
            f"🎰 <b>НОВАЯ РЕКЛАМНАЯ ЛОТА</b>\n\n"
            f"💎 Тип: {order['service']}\n"
            f"🔗 Ссылка: {order['links']}\n"
            f"👤 Заказчик: {order['contact']}\n\n"
            f"🔥 Успей залететь!"
        )
        try:
            await bot.send_message(LOT_CHANNEL, post_text, parse_mode="HTML")
            await bot.send_message(order['user_id'], f"🚀 <b>Ваша лота #{oid} успешно опубликована!</b>", parse_mode="HTML")
            db_query("UPDATE orders SET status='published' WHERE id=?", (oid,), commit=True)
        except Exception as e:
            await c.answer(f"Ошибка публикации: {e}", show_alert=True)
            return

    await c.message.edit_text(f"{c.message.text}\n\n✅ <b>ОПЛАЧЕНО И ОПУБЛИКОВАНО</b>", parse_mode="HTML")

# =================================================================
# 7. АДМИН-ПАНЕЛЬ (Обновленная)
# =================================================================
@dp.callback_query(F.data == "admin_main")
async def admin_main_menu(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS: return await c.answer("Доступ запрещен")
    
    kb = [
        [InlineKeyboardButton(text="➕ Создать лот", callback_data="adm_create")],
        [InlineKeyboardButton(text="📝 Управление лотами", callback_data="adm_manage_lots")],
        # --- НОВЫЕ КНОПКИ НАСТРОЕК ---
        [InlineKeyboardButton(text="💲 Изм. Прайс", callback_data="set_edit_price_text"),
         InlineKeyboardButton(text="⭐ Ссылка Stars", callback_data="set_edit_stars_link")],
        [InlineKeyboardButton(text="💳 Ссылка DA", callback_data="set_edit_da_link")],
        # -----------------------------
        [InlineKeyboardButton(text="🔍 Поиск юзера", callback_data="adm_search_user"),
         InlineKeyboardButton(text="📩 ЛС юзеру", callback_data="adm_dm_user")],
        [InlineKeyboardButton(text="📨 Рассылка", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="to_start")]
    ]
    await c.message.edit_text("🛠 <b>АДМИН ПАНЕЛЬ</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")

# --- ЛОГИКА ИЗМЕНЕНИЯ НАСТРОЕК ---
@dp.callback_query(F.data.startswith("set_edit_"))
async def edit_setting_start(c: CallbackQuery, state: FSMContext):
    key = c.data.replace("set_edit_", "")
    await state.update_data(setting_key=key)
    await state.set_state(AdminSettings.new_value)
    
    current = db_query("SELECT value FROM settings WHERE key=?", (key,), fetchone=True)
    curr_val = current['value'] if current else "Не задано"
    
    await c.message.answer(f"✏️ <b>Редактирование {key}</b>\n\nТекущее значение:\n{pyhtml.escape(curr_val)}\n\nВведите новое значение (текст или ссылку):", parse_mode="HTML")

@dp.message(AdminSettings.new_value)
async def edit_setting_save(m: Message, state: FSMContext):
    data = await state.get_data()
    key = data['setting_key']
    new_val = m.html_text if key == 'price_text' else m.text # Для прайса сохраняем форматирование
    
    db_query("REPLACE INTO settings (key, value) VALUES (?, ?)", (key, new_val), commit=True)
    await m.answer("✅ <b>Настройка обновлена!</b>", parse_mode="HTML")
    await state.clear()

# =================================================================
# 8. ОСТАЛЬНЫЕ ФУНКЦИИ (ОТЗЫВЫ, ПАРТНЕРКА, ПРОФИЛЬ, СОЗДАНИЕ ЛОТА)
# =================================================================
# --- ВСПОМОГАТЕЛЬНЫЙ КОД ДЛЯ МЕНЮ ---
@dp.callback_query(F.data == "to_start")
async def process_back_to_start(c: CallbackQuery, state: FSMContext):
    await state.clear()
    try: await c.message.delete()
    except: pass
    await cmd_start(c.message, CommandObject(command="start"), state)

@dp.callback_query(F.data == "active_lots")
async def process_active_lots(c: CallbackQuery):
    lots = db_query("SELECT * FROM lotteries WHERE status = 'active' ORDER BY id DESC LIMIT 10", fetchall=True)
    if not lots: return await c.answer("Активных розыгрышей нет", show_alert=True)
    kb = InlineKeyboardBuilder()
    me = await bot.get_me()
    text = "📢 <b>АКТИВНЫЕ:</b>\n\n"
    for lot in lots:
        text += f"🔹 #{lot['id']} | 🏆 {lot['winners_count']} | 👥 {lot['participants_count']}\n"
        kb.button(text=f"Лот #{lot['id']}", url=f"https://t.me/{me.username}?start=lot_{lot['id']}")
    kb.button(text="🔙 Назад", callback_data="to_start")
    kb.adjust(1)
    await c.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "view_reviews")
async def process_view_reviews(c: CallbackQuery):
    reviews = db_query("SELECT r.text, r.lot_id, u.full_name FROM reviews r JOIN users u ON r.user_id = u.user_id ORDER BY r.id DESC LIMIT 5", fetchall=True)
    kb = InlineKeyboardBuilder().button(text="🔙 Назад", callback_data="to_start").as_markup()
    if not reviews: return await c.message.edit_text("💬 Отзывов пока нет.", reply_markup=kb)
    text = "💬 <b>ОТЗЫВЫ:</b>\n\n" + "\n\n".join([f"👤 {pyhtml.escape(r['full_name'])} (#{r['lot_id']}):\n<i>{pyhtml.escape(r['text'])}</i>" for r in reviews])
    await c.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "my_stats")
async def show_profile(c: CallbackQuery):
    uid = c.from_user.id
    user = db_query("SELECT * FROM users WHERE user_id=?", (uid,), fetchone=True)
    p_cnt = db_query("SELECT COUNT(*) as c FROM participants WHERE user_id=?", (uid,), fetchone=True)['c']
    w_cnt = db_query("SELECT COUNT(*) as c FROM winners WHERE user_id=?", (uid,), fetchone=True)['c']
    me = await bot.get_me()
    msg = (f"👤 <b>КАБИНЕТ</b>\n🆔: <code>{uid}</code>\n🎫 Участий: {p_cnt} | 🏆 Побед: {w_cnt}\n👥 Рефералов: {user['refs_count']}\n🔗 Ссылка: <code>https://t.me/{me.username}?start=ref{uid}</code>")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В меню", callback_data="to_start")]])
    await c.message.edit_text(msg, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "apply_pr")
async def pr_step1(c: CallbackQuery, state: FSMContext):
    await state.set_state(PRApplication.age)
    await c.message.edit_text("📝 <b>Возраст?</b>", parse_mode="HTML")

@dp.message(PRApplication.age)
async def pr_step2(m: Message, state: FSMContext):
    await state.update_data(age=m.text)
    await state.set_state(PRApplication.nickname)
    await m.answer("📝 <b>Ссылка на канал/ник?</b>", parse_mode="HTML")

@dp.message(PRApplication.nickname)
async def pr_step3(m: Message, state: FSMContext):
    await state.update_data(nick=m.text)
    await state.set_state(PRApplication.proofs)
    await m.answer("📸 <b>Скриншот статистики (фото):</b>", parse_mode="HTML")

@dp.message(PRApplication.proofs, F.content_type == ContentType.PHOTO)
async def pr_finish(m: Message, state: FSMContext):
    data = await state.get_data()
    if PR_CHAT_ID:
        cap = f"📩 <b>PR ЗАЯВКА</b>\n👤: {m.from_user.mention_html()}\nAge: {data['age']}\nLink: {data['nick']}"
        await bot.send_photo(PR_CHAT_ID, m.photo[-1].file_id, caption=cap, parse_mode="HTML")
    await m.answer("✅ Отправлено.")
    await state.clear()

# --- ОТЗЫВЫ ---
@dp.callback_query(F.data.startswith("rev_"))
async def review_start(c: CallbackQuery, state: FSMContext):
    await state.update_data(lid=c.data.split("_")[1])
    await state.set_state(LeaveReview.text)
    await c.message.answer("✍️ Напишите отзыв:")

@dp.message(LeaveReview.text)
async def review_save(m: Message, state: FSMContext):
    d = await state.get_data()
    db_query("INSERT INTO reviews (user_id, lot_id, text) VALUES (?,?,?)", (m.from_user.id, d['lid'], m.text), commit=True)
    await m.answer("✅ Спасибо!"); await state.clear()

# --- СОЗДАНИЕ ЛОТА (АДМИН) ---
@dp.callback_query(F.data == "adm_create")
async def c_1(c: CallbackQuery, state: FSMContext):
    await state.set_state(CreateLot.text)
    await c.message.answer("1. Пост (текст/фото):")

@dp.message(CreateLot.text)
async def c_2(m: Message, state: FSMContext):
    ents = json.dumps([e.model_dump(mode='json') for e in (m.entities or m.caption_entities or [])])
    pd = {"text": m.caption or m.text or "", "entities": ents, "photo": m.photo[-1].file_id if m.photo else None, "sticker": m.sticker.file_id if m.sticker else None}
    await state.update_data(post=pd)
    await state.set_state(CreateLot.winners_count)
    await m.answer("2. Кол-во победителей:")

@dp.message(CreateLot.winners_count)
async def c_3(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("Число!")
    await state.update_data(wc=int(m.text))
    await state.set_state(CreateLot.channels)
    await m.answer("3. Каналы (@a,@b) или 'нет':")

@dp.message(CreateLot.channels)
async def c_4(m: Message, state: FSMContext):
    await state.update_data(ch=m.text)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏰ Время", callback_data="ft_time"); kb.button(text="👥 Кол-во", callback_data="ft_count")
    await m.answer("4. Тип финиша:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("ft_"))
async def c_5(c: CallbackQuery, state: FSMContext):
    t = "time" if c.data=="ft_time" else "count"
    await state.update_data(ft=t)
    await c.message.edit_text("Часов?" if t=="time" else "Участников?")
    await state.set_state(CreateLot.value)

@dp.message(CreateLot.value)
async def c_fin(m: Message, state: FSMContext):
    if not m.text.isdigit(): return
    d = await state.get_data()
    val = (datetime.now()+timedelta(hours=int(m.text))).strftime("%d.%m.%Y %H:%M") if d['ft']=="time" else int(m.text)
    lid = db_query("INSERT INTO lotteries (text, entities, channels, finish_type, finish_value, photo, sticker, winners_count) VALUES (?,?,?,?,?,?,?,?)", 
                   (d['post']['text'], d['post']['entities'], d['ch'], d['ft'], val, d['post']['photo'], d['post']['sticker'], d['wc']), commit=True)
    me = await bot.get_me()
    kb = InlineKeyboardBuilder().button(text="✅ Участвовать! (0)", url=f"https://t.me/{me.username}?start=lot_{lid}").as_markup()
    try:
        if d['post']['photo']: sent = await bot.send_photo(LOT_CHANNEL, d['post']['photo'], caption=d['post']['text'], reply_markup=kb)
        elif d['post']['sticker']: await bot.send_sticker(LOT_CHANNEL, d['post']['sticker']); sent = await bot.send_message(LOT_CHANNEL, "🎁 Розыгрыш!", reply_markup=kb)
        else: sent = await bot.send_message(LOT_CHANNEL, d['post']['text'], reply_markup=kb)
        db_query("UPDATE lotteries SET message_id=? WHERE id=?", (sent.message_id, lid), commit=True)
        await m.answer("✅ Опубликовано!")
    except Exception as e: await m.answer(f"Ошибка: {e}")
    await state.clear()

# --- УПРАВЛЕНИЕ ЛОТАМИ И ПОИСК (ОСТАВЛЕНО БЕЗ ИЗМЕНЕНИЙ ДЛЯ ЭКОНОМИИ МЕСТА, ОНИ ТАКИЕ ЖЕ КАК В ПРОШЛОМ КОДЕ) ---
@dp.callback_query(F.data == "adm_manage_lots")
async def m_lots(c: CallbackQuery):
    l = db_query("SELECT * FROM lotteries ORDER BY id DESC LIMIT 10", fetchall=True)
    kb = InlineKeyboardBuilder()
    for x in l: kb.button(text=f"#{x['id']} ({x['status']})", callback_data=f"manage_{x['id']}")
    kb.button(text="🔙", callback_data="admin_main"); kb.adjust(1)
    await c.message.edit_text("Выбери:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("manage_"))
async def m_one(c: CallbackQuery):
    lid = c.data.split("_")[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛑 Стоп", callback_data=f"stop_{lid}")], [InlineKeyboardButton(text="🔙", callback_data="adm_manage_lots")]])
    await c.message.edit_text(f"Лот #{lid}", reply_markup=kb)

@dp.callback_query(F.data.startswith("stop_"))
async def stop_l(c: CallbackQuery):
    await run_final_selection(int(c.data.split("_")[1]))
    await c.answer("Остановлено")

@dp.callback_query(F.data == "adm_search_user")
async def s_u(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminSearch.query); await c.message.answer("ID или @username:")

@dp.message(AdminSearch.query)
async def s_r(m: Message, state: FSMContext):
    q = m.text.replace("@","")
    u = db_query("SELECT * FROM users WHERE user_id=? OR username=?", (q, q), fetchone=True)
    await m.answer(f"Нашел: {u['full_name']} ({u['user_id']})" if u else "Не нашел.")
    await state.clear()

@dp.callback_query(F.data == "adm_broadcast")
async def br_1(c: CallbackQuery, state: FSMContext):
    await state.set_state(BroadcastState.content); await c.message.answer("Пост:")

@dp.message(BroadcastState.content)
async def br_2(m: Message, state: FSMContext):
    u = db_query("SELECT user_id FROM users", fetchall=True)
    await m.answer(f"Рассылка на {len(u)}...")
    for x in u:
        try: await m.copy_to(x['user_id']); await asyncio.sleep(0.05)
        except: pass
    await m.answer("Готово."); await state.clear()

# =================================================================
# 9. ЗАПУСК
# =================================================================
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
    logger.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
