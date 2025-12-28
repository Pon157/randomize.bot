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
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

# --- 1. НАСТРОЙКИ И ЛОГИ ---
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
PR_CHAT_ID = int(os.getenv("PR_CHAT_ID", "0"))
LOT_CHANNEL = os.getenv("LOT_CHANNEL", "@lotsvitechek")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- 2. РАБОТА С БАЗОЙ ДАННЫХ (С ПОДДЕРЖКОЙ МИГРАЦИЙ) ---
def init_db():
    with sqlite3.connect("bot_database.db") as conn:
        cur = conn.cursor()
        # Создание таблиц
        cur.execute("""CREATE TABLE IF NOT EXISTS lotteries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT, entities TEXT, channels TEXT,
            finish_type TEXT, finish_value TEXT,
            status TEXT DEFAULT 'active', message_id INTEGER,
            photo TEXT, sticker TEXT, winners_count INTEGER DEFAULT 1,
            participants_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS participants (
            user_id INTEGER, lot_id INTEGER, username TEXT, full_name TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (user_id, lot_id)
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS winners (
            lot_id INTEGER, user_id INTEGER, PRIMARY KEY (lot_id, user_id)
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, 
            lot_id INTEGER, text TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        
        # Проверка и добавление отсутствующих колонок (миграция)
        cur.execute("PRAGMA table_info(lotteries)")
        columns = [column[1] for column in cur.fetchall()]
        if 'winners_count' not in columns:
            cur.execute("ALTER TABLE lotteries ADD COLUMN winners_count INTEGER DEFAULT 1")
        if 'photo' not in columns:
            cur.execute("ALTER TABLE lotteries ADD COLUMN photo TEXT")
        if 'sticker' not in columns:
            cur.execute("ALTER TABLE lotteries ADD COLUMN sticker TEXT")
        if 'participants_count' not in columns:
            cur.execute("ALTER TABLE lotteries ADD COLUMN participants_count INTEGER DEFAULT 0")
        conn.commit()

def db_query(query, params=(), fetchone=False, fetchall=False, commit=False):
    with sqlite3.connect("bot_database.db") as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, params)
        if commit: conn.commit()
        if fetchone: return cur.fetchone()
        if fetchall: return cur.fetchall()
        return cur.lastrowid

# --- 3. СОСТОЯНИЯ (FSM) ---
class CreateLot(StatesGroup):
    text = State()
    winners_count = State()
    channels = State()
    finish_type = State()
    value = State()

class EditLot(StatesGroup):
    lot_id = State()
    field = State()
    new_value = State()

class PRApplication(StatesGroup):
    age = State()
    nickname = State()
    proofs = State()

class LeaveReview(StatesGroup):
    lot_id = State()
    text = State()

# --- 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
async def update_lottery_button(lot_id: int, count: int):
    lot = db_query("SELECT message_id FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or not lot['message_id']: return
    kb = [[InlineKeyboardButton(text=f"✅ Участвовать! ({count})", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")]]
    try: await bot.edit_message_reply_markup(chat_id=LOT_CHANNEL, message_id=lot['message_id'], reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    except: pass

async def check_sub(user_id: int, channels_str: str):
    if not channels_str or channels_str.lower() in ['нет', 'none', '']: return True, []
    not_sub = []
    for ch in [c.strip() for c in channels_str.split(",") if c.strip()]:
        try:
            m = await bot.get_chat_member(ch, user_id)
            if m.status in ["left", "kicked"]: not_sub.append(ch)
        except: not_sub.append(ch)
    return len(not_sub) == 0, not_sub

async def finish_giveaway(lot_id: int):
    lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or lot['status'] == 'closed': return
    
    parts = db_query("SELECT * FROM participants WHERE lot_id = ?", (lot_id,), fetchall=True)
    db_query("UPDATE lotteries SET status = 'closed' WHERE id = ?", (lot_id,), commit=True)
    
    if not parts:
        await bot.send_message(LOT_CHANNEL, f"🔔 Лотерея #{lot_id} окончена. Участников нет.")
        return

    # Выбор победителей
    count_to_pick = min(len(parts), lot['winners_count'])
    winners_list = random.sample(parts, count_to_pick)
    
    mentions = []
    for w in winners_list:
        db_query("INSERT OR IGNORE INTO winners (lot_id, user_id) VALUES (?,?)", (lot_id, w['user_id']), commit=True)
        mentions.append(f"@{w['username']}" if w['username'] else w['full_name'])
        try:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✍️ Оставить отзыв", callback_data=f"rev_{lot_id}")]])
            await bot.send_message(w['user_id'], f"🎉 Ты выиграл в лотерее #{lot_id}! Оставь свой отзыв:", reply_markup=kb)
        except: pass

    res_txt = f"🎊 **Итоги лотереи #{lot_id}!**\n🏆 Победители: {', '.join(mentions)}\n📊 Участников: {len(parts)}"
    await bot.send_message(LOT_CHANNEL, res_txt, parse_mode="Markdown", reply_to_message_id=lot['message_id'])

# --- 5. ОСНОВНЫЕ ХЕНДЛЕРЫ ---
@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    user_id = message.from_user.id
    db_query("INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?,?,?)", (user_id, message.from_user.username, message.from_user.full_name), commit=True)

    if command.args and command.args.startswith("lot_"):
        lot_id = int(command.args.split("_")[1])
        lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
        if not lot or lot['status'] == 'closed': return await message.answer("❌ Завершено.")
        
        ok, not_sub = await check_sub(user_id, lot['channels'])
        if not ok:
            kb = InlineKeyboardBuilder()
            for ch in not_sub: kb.button(text=f"📢 {ch}", url=f"https://t.me/{ch.lstrip('@')}")
            kb.button(text="🔄 Проверить", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")
            return await message.answer("❌ Подпишись на каналы:", reply_markup=kb.adjust(1).as_markup())

        try:
            db_query("INSERT INTO participants (user_id, lot_id, username, full_name) VALUES (?,?,?,?)", (user_id, lot_id, message.from_user.username, message.from_user.full_name), commit=True)
            c = db_query("SELECT COUNT(*) as c FROM participants WHERE lot_id=?", (lot_id,), fetchone=True)['c']
            db_query("UPDATE lotteries SET participants_count = ? WHERE id = ?", (c, lot_id), commit=True)
            await update_lottery_button(lot_id, c)
            if lot['finish_type'] == 'count' and c >= int(lot['finish_value']): await finish_giveaway(lot_id)
            await message.answer("✅ Участие подтверждено!")
        except: await message.answer("✅ Ты уже в деле!")
        return

    kb = [[InlineKeyboardButton(text="💬 Отзывы", callback_data="view_reviews"), InlineKeyboardButton(text="💼 PR Заявка", callback_data="apply_pr")],
          [InlineKeyboardButton(text="📢 Активные розыгрыши", callback_data="active_lots")]]
    if user_id in ADMIN_IDS: kb.append([InlineKeyboardButton(text="🛠 Админка", callback_data="admin_main")])
    await message.answer(f"👋 Привет, {message.from_user.first_name}!", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

# --- 6. АДМИН-МЕНЮ ---
@dp.callback_query(F.data == "admin_main")
async def adm_main(c: CallbackQuery):
    kb = [[InlineKeyboardButton(text="➕ Создать лот", callback_data="adm_create")],
          [InlineKeyboardButton(text="📝 Настройки лота", callback_data="adm_edit_list")],
          [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")]]
    await c.message.edit_text("🛠 Управление", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data == "adm_create")
async def adm_c1(c: CallbackQuery, state: FSMContext):
    await state.set_state(CreateLot.text)
    await c.message.answer("Пришли пост (Текст/Фото/Стикер):")

@dp.message(CreateLot.text)
async def adm_c2(m: Message, state: FSMContext):
    d = {"p": m.photo[-1].file_id if m.photo else None, "s": m.sticker.file_id if m.sticker else None,
         "t": m.caption or m.text or "", "e": json.dumps([e.model_dump(mode='json') for e in (m.entities or m.caption_entities or [])])}
    await state.update_data(**d)
    await state.set_state(CreateLot.winners_count)
    await m.answer("Сколько будет победителей?")

@dp.message(CreateLot.winners_count)
async def adm_c3(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("Напиши число!")
    await state.update_data(wc=int(m.text))
    await state.set_state(CreateLot.channels)
    await m.answer("Каналы через запятую или 'нет':")

@dp.message(CreateLot.channels)
async def adm_c4(m: Message, state: FSMContext):
    await state.update_data(ch=m.text)
    kb = [[InlineKeyboardButton(text="⏰ Время", callback_data="st_time"), InlineKeyboardButton(text="👥 Кол-во", callback_data="st_count")]]
    await m.answer("Тип завершения:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("st_"))
async def adm_c5(c: CallbackQuery, state: FSMContext):
    await state.update_data(ft=c.data.split("_")[1])
    await c.message.answer("Значение (часы или число людей):")
    await state.set_state(CreateLot.value)

@dp.message(CreateLot.value)
async def adm_c_final(m: Message, state: FSMContext):
    d = await state.get_data()
    val = (datetime.now() + timedelta(hours=int(m.text))).strftime("%d.%m.%Y %H:%M") if d['ft'] == 'time' else m.text
    lid = db_query("INSERT INTO lotteries (text, entities, channels, finish_type, finish_value, photo, sticker, winners_count) VALUES (?,?,?,?,?,?,?,?)",
                  (d['t'], d['e'], d['ch'], d['ft'], val, d['p'], d['s'], d['wc']), commit=True)
    kb = [[InlineKeyboardButton(text="✅ Участвовать! (0)", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lid}")]]
    if d['p']: sent = await bot.send_photo(LOT_CHANNEL, d['p'], caption=d['t'], reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    elif d['s']: 
        await bot.send_sticker(LOT_CHANNEL, d['s'])
        sent = await bot.send_message(LOT_CHANNEL, "🎁 Участвуй!", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    else: sent = await bot.send_message(LOT_CHANNEL, d['t'], reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    db_query("UPDATE lotteries SET message_id = ? WHERE id = ?", (sent.message_id, lid), commit=True)
    await m.answer(f"✅ Лот #{lid} запущен!"); await state.clear()

# --- 7. НАСТРОЙКА И СТАТИСТИКА ---
@dp.callback_query(F.data == "adm_edit_list")
async def adm_e1(c: CallbackQuery):
    lots = db_query("SELECT * FROM lotteries WHERE status='active'", fetchall=True)
    if not lots: return await c.answer("Нет активных лотов", show_alert=True)
    kb = InlineKeyboardBuilder()
    for l in lots: kb.button(text=f"⚙️ Лот #{l['id']}", callback_data=f"ed_ch_{l['id']}")
    await c.message.edit_text("Что настраиваем?", reply_markup=kb.adjust(2).as_markup())

@dp.callback_query(F.data.startswith("ed_ch_"))
async def adm_e2(c: CallbackQuery):
    lid = c.data.split("_")[2]
    kb = InlineKeyboardBuilder()
    kb.button(text="Каналы", callback_data=f"edf_{lid}_channels")
    kb.button(text="Лимит/Время", callback_data=f"edf_{lid}_finish_value")
    kb.button(text="Победителей", callback_data=f"edf_{lid}_winners_count")
    kb.button(text="🛑 СТОП", callback_data=f"edf_{lid}_stop")
    await c.message.edit_text(f"Лот #{lid}:", reply_markup=kb.adjust(1).as_markup())

@dp.callback_query(F.data.startswith("edf_"))
async def adm_e3(c: CallbackQuery, state: FSMContext):
    _, lid, field = c.data.split("_")
    if field == "stop":
        await finish_giveaway(int(lid))
        return await c.answer("Остановлено!")
    await state.update_data(lid=lid, field=field)
    await state.set_state(EditLot.new_value)
    await c.message.answer(f"Введи новое значение для {field}:")

@dp.message(EditLot.new_value)
async def adm_e4(m: Message, state: FSMContext):
    d = await state.get_data()
    db_query(f"UPDATE lotteries SET {d['field']} = ? WHERE id = ?", (m.text, d['lid']), commit=True)
    await m.answer("✅ Обновлено!"); await state.clear()

@dp.callback_query(F.data == "adm_stats")
async def adm_stats(c: CallbackQuery):
    lots = db_query("SELECT * FROM lotteries ORDER BY id DESC LIMIT 10", fetchall=True)
    res = "📊 **Последние лоты:**\n\n"
    for l in lots:
        res += f"#{l['id']} | {l['status']} | 👥 {l['participants_count']} чел. | 🏆 {l['winners_count']} мест\n"
    await c.message.answer(res, parse_mode="Markdown")

# --- 8. ОТЗЫВЫ И PR ---
@dp.callback_query(F.data == "view_reviews")
async def view_revs(c: CallbackQuery):
    revs = db_query("SELECT r.*, u.full_name FROM reviews r JOIN users u ON r.user_id = u.user_id ORDER BY r.id DESC LIMIT 10", fetchall=True)
    if not revs: return await c.answer("Отзывов пока нет", show_alert=True)
    txt = "💬 **Отзывы победителей:**\n\n"
    for r in revs: txt += f"👤 {r['full_name']} (Лот #{r['lot_id']}):\n«{r['text']}»\n\n"
    await c.message.answer(txt, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("rev_"))
async def leave_rev1(c: CallbackQuery, state: FSMContext):
    lid = c.data.split("_")[1]
    win = db_query("SELECT * FROM winners WHERE lot_id=? AND user_id=?", (lid, c.from_user.id), fetchone=True)
    if not win: return await c.answer("❌ Ты не побеждал!", show_alert=True)
    await state.update_data(lid=lid); await state.set_state(LeaveReview.text)
    await c.message.answer("Напиши свой отзыв:")

@dp.message(LeaveReview.text)
async def leave_rev2(m: Message, state: FSMContext):
    d = await state.get_data()
    db_query("INSERT INTO reviews (user_id, lot_id, text) VALUES (?,?,?)", (m.from_user.id, d['lid'], m.text), commit=True)
    await m.answer("✅ Отзыв опубликован!"); await state.clear()

@dp.callback_query(F.data == "apply_pr")
async def pr_1(c: CallbackQuery, state: FSMContext):
    await state.set_state(PRApplication.age); await c.message.answer("Твой возраст?")

@dp.message(PRApplication.age)
async def pr_2(m: Message, state: FSMContext):
    await state.update_data(a=m.text); await state.set_state(PRApplication.nickname); await m.answer("Твой ник/ссылка?")

@dp.message(PRApplication.nickname)
async def pr_3(m: Message, state: FSMContext):
    await state.update_data(n=m.text); await state.set_state(PRApplication.proofs); await m.answer("Пришли скрин статистики:")

@dp.message(PRApplication.proofs, F.photo)
async def pr_4(m: Message, state: FSMContext):
    d = await state.get_data()
    caption = f"📩 PR ЗАЯВКА\nЮзер: @{m.from_user.username}\nВозраст: {d['a']}\nЛинк: {d['n']}"
    if PR_CHAT_ID: await bot.send_photo(PR_CHAT_ID, m.photo[-1].file_id, caption=caption)
    await m.answer("✅ Заявка отправлена!"); await state.clear()

# --- 9. ПЛАНИРОВЩИК И ЗАПУСК ---
async def scheduler():
    while True:
        try:
            active = db_query("SELECT * FROM lotteries WHERE status='active' AND finish_type='time'", fetchall=True)
            for l in active:
                if datetime.now() >= datetime.strptime(l['finish_value'], "%d.%m.%Y %H:%M"):
                    await finish_giveaway(l['id'])
        except: pass
        await asyncio.sleep(30)

async def main():
    init_db()
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
