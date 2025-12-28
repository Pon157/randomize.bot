import os
import random
import sqlite3
import logging
import json
import asyncio
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

# --- 1. ЛОГИРОВАНИЕ И КОНФИГУРАЦИЯ ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("LotteryBotVitechek")

load_dotenv()
# Токен берем из .env как и договаривались
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
PR_CHAT_ID = int(os.getenv("PR_CHAT_ID", "0"))
LOT_CHANNEL = os.getenv("LOT_CHANNEL", "@lotsvitechek")

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- 2. ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ---
def init_db():
    """Создание всех необходимых таблиц и выполнение миграций колонок"""
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    
    # Лотереи
    cur.execute("""CREATE TABLE IF NOT EXISTS lotteries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT, entities TEXT, channels TEXT,
        finish_type TEXT, finish_value TEXT,
        status TEXT DEFAULT 'active', message_id INTEGER,
        photo TEXT, sticker TEXT, winners_count INTEGER DEFAULT 1,
        participants_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    
    # Участники
    cur.execute("""CREATE TABLE IF NOT EXISTS participants (
        user_id INTEGER, lot_id INTEGER, username TEXT, full_name TEXT,
        PRIMARY KEY (user_id, lot_id)
    )""")
    
    # Победители
    cur.execute("""CREATE TABLE IF NOT EXISTS winners (
        lot_id INTEGER, user_id INTEGER, win_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    
    # Отзывы
    cur.execute("""CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, 
        lot_id INTEGER, text TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    
    # Пользователи и реферальная система
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, 
        username TEXT, 
        full_name TEXT, 
        referrer_id INTEGER DEFAULT 0,
        refs_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # Миграция колонок (защита от OperationalError)
    cur.execute("PRAGMA table_info(lotteries)")
    l_cols = [c[1] for c in cur.fetchall()]
    if 'winners_count' not in l_cols: cur.execute("ALTER TABLE lotteries ADD COLUMN winners_count INTEGER DEFAULT 1")
    if 'photo' not in l_cols: cur.execute("ALTER TABLE lotteries ADD COLUMN photo TEXT")
    if 'sticker' not in l_cols: cur.execute("ALTER TABLE lotteries ADD COLUMN sticker TEXT")
    if 'participants_count' not in l_cols: cur.execute("ALTER TABLE lotteries ADD COLUMN participants_count INTEGER DEFAULT 0")

    cur.execute("PRAGMA table_info(users)")
    u_cols = [c[1] for c in cur.fetchall()]
    if 'referrer_id' not in u_cols: cur.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER DEFAULT 0")
    if 'refs_count' not in u_cols: cur.execute("ALTER TABLE users ADD COLUMN refs_count INTEGER DEFAULT 0")
    
    conn.commit()
    conn.close()
    logger.info("Система БД готова к работе.")

def db_query(query, params=(), fetchone=False, fetchall=False, commit=False):
    """Универсальный исполнитель запросов к БД"""
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
        logger.error(f"БД Ошибка: {e}")
        return None

# --- 3. FSM КЛАССЫ (СОСТОЯНИЯ) ---
class CreateLot(StatesGroup):
    text = State()
    winners_count = State()
    channels = State()
    finish_type = State()
    value = State()

class PRApplication(StatesGroup):
    age = State()
    nickname = State()
    proofs = State()

class LeaveReview(StatesGroup):
    lot_id = State()
    text = State()

class BroadcastState(StatesGroup):
    content = State()

# --- 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
async def check_sub_status(user_id: int, channels_str: str):
    """Проверка подписки на список каналов"""
    if not channels_str or channels_str.lower() in ['нет', 'none', '']:
        return True, []
    not_sub = []
    channels = [c.strip() for c in channels_str.split(",") if c.strip()]
    for ch in channels:
        try:
            m = await bot.get_chat_member(ch, user_id)
            if m.status in ["left", "kicked"]: not_sub.append(ch)
        except: not_sub.append(ch)
    return len(not_sub) == 0, not_sub

async def update_lottery_btn(lot_id: int, count: int):
    """Обновляет счетчик участников на кнопке в канале"""
    lot = db_query("SELECT message_id FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or not lot['message_id']: return
    kb = InlineKeyboardBuilder()
    kb.button(text=f"✅ Участвовать! ({count})", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")
    try:
        await bot.edit_message_reply_markup(chat_id=LOT_CHANNEL, message_id=lot['message_id'], reply_markup=kb.as_markup())
    except: pass

async def finish_lottery(lot_id: int):
    """Выбор победителей и закрытие лота"""
    lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or lot['status'] == 'closed': return
    
    parts = db_query("SELECT * FROM participants WHERE lot_id = ?", (lot_id,), fetchall=True)
    db_query("UPDATE lotteries SET status = 'closed' WHERE id = ?", (lot_id,), commit=True)
    
    if not parts:
        await bot.send_message(LOT_CHANNEL, f"🔔 Лотерея #{lot_id} завершена. Участников не было.")
        return

    win_count = min(len(parts), lot['winners_count'])
    winners = random.sample(parts, win_count)
    
    mentions = []
    for w in winners:
        db_query("INSERT INTO winners (lot_id, user_id) VALUES (?,?)", (lot_id, w['user_id']), commit=True)
        name = f"@{w['username']}" if w['username'] else w['full_name']
        mentions.append(name)
        try:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✍️ Отзыв", callback_data=f"rev_{lot_id}")]])
            await bot.send_message(w['user_id'], f"🎊 Победа в лотерее #{lot_id}!", reply_markup=kb)
        except: pass

    res_txt = f"🎊 **ЛОТЕРЕЯ #{lot_id} ЗАВЕРШЕНА!**\n\n🏆 Победители: {', '.join(mentions)}\n👥 Участников: {len(parts)}"
    try:
        await bot.send_message(LOT_CHANNEL, res_txt, parse_mode="Markdown", reply_to_message_id=lot['message_id'])
    except:
        await bot.send_message(LOT_CHANNEL, res_txt, parse_mode="Markdown")

# --- 5. ОБРАБОТЧИКИ КНОПОК МЕНЮ ---

@dp.callback_query(F.data == "my_stats")
async def show_profile(c: CallbackQuery):
    uid = c.from_user.id
    u = db_query("SELECT * FROM users WHERE user_id = ?", (uid,), fetchone=True)
    p = db_query("SELECT COUNT(*) as c FROM participants WHERE user_id = ?", (uid,), fetchone=True)['c']
    w = db_query("SELECT COUNT(*) as c FROM winners WHERE user_id = ?", (uid,), fetchone=True)['c']
    
    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start=ref{uid}"
    
    txt = (f"👤 **Ваш профиль**\n\n🆔 ID: `{uid}`\n🎫 Участий: {p}\n🏆 Побед: {w}\n"
           f"👥 Рефералов: {u['refs_count'] if u else 0}\n\n🔗 **Ссылка:**\n`{ref_link}`")
    await c.message.answer(txt, parse_mode="Markdown")
    await c.answer()

@dp.callback_query(F.data == "active_lots")
async def show_lots(c: CallbackQuery):
    lots = db_query("SELECT * FROM lotteries WHERE status = 'active' ORDER BY id DESC", fetchall=True)
    if not lots: return await c.answer("Активных лотов нет!", show_alert=True)
    
    msg = "📢 **Активные лотереи:**\n\n"
    for l in lots:
        msg += f"🔹 Лот #{l['id']} | 👥 {l['participants_count']} чел.\n"
    await c.message.answer(msg, parse_mode="Markdown")
    await c.answer()

# --- 6. КОМАНДА /START И РЕФЕРАЛЫ ---

@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    uid = message.from_user.id
    args = command.args
    
    # Регистрация и реф-система
    user = db_query("SELECT * FROM users WHERE user_id = ?", (uid,), fetchone=True)
    if not user:
        ref_id = 0
        if args and args.startswith("ref"):
            try:
                ref_id = int(args.replace("ref", ""))
                if ref_id == uid: ref_id = 0
            except: pass
        
        db_query("INSERT INTO users (user_id, username, full_name, referrer_id) VALUES (?,?,?,?)",
                 (uid, message.from_user.username, message.from_user.full_name, ref_id), commit=True)
        
        if ref_id != 0:
            # Обновляем счетчик пригласившего
            db_query("UPDATE users SET refs_count = refs_count + 1 WHERE user_id = ?", (ref_id,), commit=True)
            ref_data = db_query("SELECT * FROM users WHERE user_id = ?", (ref_id,), fetchone=True)
            
            # УВЕДОМЛЕНИЕ В PR ЧАТ
            if PR_CHAT_ID:
                pr_msg = (f"🤝 **Новый реферал!**\n\n"
                          f"👤 Кто пригласил: @{ref_data['username'] or ref_data['full_name']} (ID: `{ref_id}`)\n"
                          f"🆕 Кто пришел: @{message.from_user.username or message.from_user.full_name}\n"
                          f"📊 Всего рефералов у партнера: **{ref_data['refs_count']}**")
                try: await bot.send_message(PR_CHAT_ID, pr_msg, parse_mode="Markdown")
                except: pass
            
            try: await bot.send_message(ref_id, "🤝 У вас новый реферал!")
            except: pass

    # Если вход для участия
    if args and args.startswith("lot_"):
        lid = int(args.split("_")[1])
        lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lid,), fetchone=True)
        if not lot or lot['status'] == 'closed': return await message.answer("❌ Завершено.")
        
        sub_ok, channels = await check_sub_status(uid, lot['channels'])
        if not sub_ok:
            kb = InlineKeyboardBuilder()
            for ch in channels: kb.button(text=f"📢 {ch}", url=f"https://t.me/{ch.lstrip('@')}")
            kb.button(text="🔄 Проверить", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lid}")
            return await message.answer("⚠️ Подпишись на каналы:", reply_markup=kb.adjust(1).as_markup())

        try:
            db_query("INSERT INTO participants (user_id, lot_id, username, full_name) VALUES (?,?,?,?)", 
                     (uid, lid, message.from_user.username, message.from_user.full_name), commit=True)
            cnt = db_query("SELECT COUNT(*) as c FROM participants WHERE lot_id = ?", (lid,), fetchone=True)['c']
            db_query("UPDATE lotteries SET participants_count = ? WHERE id = ?", (cnt, lid), commit=True)
            await update_lottery_btn(lid, cnt)
            if lot['finish_type'] == 'count' and cnt >= int(lot['finish_value']): await finish_lottery(lid)
            await message.answer(f"✅ Ты участвуешь в лотерее #{lid}!")
        except: await message.answer("✅ Ты уже зарегистрирован!")
        return

    # Главное меню
    kb = [
        [InlineKeyboardButton(text="💬 Отзывы", callback_data="view_reviews"), InlineKeyboardButton(text="💼 PR Заявка", callback_data="apply_pr")],
        [InlineKeyboardButton(text="📢 Розыгрыши", callback_data="active_lots"), InlineKeyboardButton(text="📊 Мой профиль", callback_data="my_stats")]
    ]
    if uid in ADMIN_IDS:
        kb.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_main")])
    
    await message.answer(f"👋 Привет, {message.from_user.first_name}!", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

# --- 7. АДМИН-ПАНЕЛЬ (РАССЫЛКА ТЕПЕРЬ ТУТ) ---

@dp.callback_query(F.data == "admin_main")
async def adm_menu(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS: return
    kb = [
        [InlineKeyboardButton(text="➕ Создать лот", callback_data="adm_create")],
        [InlineKeyboardButton(text="📩 Рассылка по всем", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="📝 Список лотов", callback_data="adm_list")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="to_start")]
    ]
    await c.message.edit_text("🛠 **Панель администратора**", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")

@dp.callback_query(F.data == "adm_broadcast")
async def adm_broad_step1(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS: return
    await state.set_state(BroadcastState.content)
    await c.message.answer("📣 Отправь сообщение (текст/фото) для рассылки всем юзерам:")

@dp.message(BroadcastState.content)
async def adm_broad_step2(m: Message, state: FSMContext):
    users = db_query("SELECT user_id FROM users", fetchall=True)
    await m.answer(f"🚀 Запускаю рассылку на {len(users)} чел...")
    ok, err = 0, 0
    for u in users:
        try:
            await m.copy_to(u['user_id'])
            ok += 1
            await asyncio.sleep(0.05)
        except: err += 1
    await m.answer(f"🏁 Итог: ✅ {ok} | ❌ {err}")
    await state.clear()

# --- 8. СОЗДАНИЕ ЛОТА ---

@dp.callback_query(F.data == "adm_create")
async def adm_c1(c: CallbackQuery, state: FSMContext):
    await state.set_state(CreateLot.text)
    await c.message.answer("1️⃣ Отправь пост (Текст, Фото или Стикер):")

@dp.message(CreateLot.text)
async def adm_c2(m: Message, state: FSMContext):
    data = {
        "text": m.caption or m.text or "",
        "entities": json.dumps([e.model_dump(mode='json') for e in (m.entities or m.caption_entities or [])]),
        "photo": m.photo[-1].file_id if m.photo else None,
        "sticker": m.sticker.file_id if m.sticker else None
    }
    await state.update_data(post=data)
    await state.set_state(CreateLot.winners_count)
    await m.answer("2️⃣ Сколько победителей?")

@dp.message(CreateLot.winners_count)
async def adm_c3(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("Введи число!")
    await state.update_data(wc=int(m.text))
    await state.set_state(CreateLot.channels)
    await m.answer("3️⃣ Каналы через запятую (@chan1, @chan2) или 'нет':")

@dp.message(CreateLot.channels)
async def adm_c4(m: Message, state: FSMContext):
    await state.update_data(ch=m.text)
    kb = [[InlineKeyboardButton(text="⏰ Время", callback_data="set_t"), InlineKeyboardButton(text="👥 Кол-во", callback_data="set_c")]]
    await m.answer("4️⃣ Тип финиша:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("set_"))
async def adm_c5(c: CallbackQuery, state: FSMContext):
    t = c.data.split("_")[1]
    await state.update_data(ft="time" if t=="t" else "count")
    await c.message.answer("Введите значение (часы или число участников):")
    await state.set_state(CreateLot.value)

@dp.message(CreateLot.value)
async def adm_c_final(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("Введи число!")
    s = await state.get_data()
    post = s['post']
    val = (datetime.now() + timedelta(hours=int(m.text))).strftime("%d.%m.%Y %H:%M") if s['ft'] == 'time' else m.text
    
    lid = db_query("INSERT INTO lotteries (text, entities, channels, finish_type, finish_value, photo, sticker, winners_count) VALUES (?,?,?,?,?,?,?,?)",
                   (post['text'], post['entities'], s['ch'], s['ft'], val, post['photo'], post['sticker'], s['wc']), commit=True)
    
    kb = InlineKeyboardBuilder().button(text="✅ Участвовать! (0)", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lid}")
    if post['photo']: sent = await bot.send_photo(LOT_CHANNEL, post['photo'], caption=post['text'], reply_markup=kb.as_markup())
    elif post['sticker']: 
        await bot.send_sticker(LOT_CHANNEL, post['sticker'])
        sent = await bot.send_message(LOT_CHANNEL, "🎁 Новый розыгрыш!", reply_markup=kb.as_markup())
    else: sent = await bot.send_message(LOT_CHANNEL, post['text'], reply_markup=kb.as_markup())
    
    db_query("UPDATE lotteries SET message_id = ? WHERE id = ?", (sent.message_id, lid), commit=True)
    await m.answer(f"✅ Лот #{lid} опубликован!")
    await state.clear()

# --- 9. PR И ОТЗЫВЫ ---

@dp.callback_query(F.data == "apply_pr")
async def pr1(c: CallbackQuery, state: FSMContext):
    await state.set_state(PRApplication.age)
    await c.message.answer("Заявка на PR.\n1. Твой возраст?")

@dp.message(PRApplication.age)
async def pr2(m: Message, state: FSMContext):
    await state.update_data(age=m.text)
    await state.set_state(PRApplication.nickname)
    await m.answer("2. Ссылка на канал / ник?")

@dp.message(PRApplication.nickname)
async def pr3(m: Message, state: FSMContext):
    await state.update_data(nick=m.text)
    await state.set_state(PRApplication.proofs)
    await m.answer("3. Скриншот статистики:")

@dp.message(PRApplication.proofs, F.photo)
async def pr4(m: Message, state: FSMContext):
    d = await state.get_data()
    info = (f"📩 **ЗАЯВКА PR**\n\n👤 От: @{m.from_user.username}\n🔞 Возраст: {d['age']}\n🔗 Канал: {d['nick']}")
    if PR_CHAT_ID: await bot.send_photo(PR_CHAT_ID, m.photo[-1].file_id, caption=info)
    await m.answer("✅ Отправлено!"); await state.clear()

@dp.callback_query(F.data.startswith("rev_"))
async def rev1(c: CallbackQuery, state: FSMContext):
    await state.update_data(rlid=c.data.split("_")[1])
    await state.set_state(LeaveReview.text)
    await c.message.answer("Твой отзыв о выигрыше:")

@dp.message(LeaveReview.text)
async def rev2(m: Message, state: FSMContext):
    d = await state.get_data()
    db_query("INSERT INTO reviews (user_id, lot_id, text) VALUES (?,?,?)", (m.from_user.id, d['rlid'], m.text), commit=True)
    await m.answer("✅ Спасибо!"); await state.clear()

@dp.callback_query(F.data == "to_start")
async def back(c: CallbackQuery, state: FSMContext):
    await cmd_start(c.message, CommandObject(command="start"), state)
    await c.message.delete()

# --- 10. ФОНОВЫЙ ЦИКЛ И ЗАПУСК ---

async def scheduler():
    while True:
        try:
            active = db_query("SELECT * FROM lotteries WHERE status='active' AND finish_type='time'", fetchall=True)
            now = datetime.now()
            for l in active:
                if now >= datetime.strptime(l['finish_value'], "%d.%m.%Y %H:%M"):
                    await finish_lottery(l['id'])
        except: pass
        await asyncio.sleep(60)

async def main():
    init_db()
    asyncio.create_task(scheduler())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except: pass
