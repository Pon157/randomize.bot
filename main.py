import os
import random
import asyncio
import sqlite3
import logging
import json
from datetime import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery

# --- НАСТРОЙКИ ---
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
PR_CHAT_ID = int(os.getenv("PR_CHAT_ID"))
LOT_CHANNEL = "@lotsvitechek" 

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- БАЗА ДАННЫХ ---
def init_db():
    with sqlite3.connect("bot_database.db") as conn:
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS lotteries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, entities TEXT, 
            channels TEXT, finish_type TEXT, finish_value TEXT, 
            status TEXT DEFAULT 'active', message_id INTEGER)""")
        cur.execute("CREATE TABLE IF NOT EXISTS participants (user_id INTEGER, lot_id INTEGER, PRIMARY KEY (user_id, lot_id))")
        conn.commit()

def create_lot_db(text, entities, channels, f_type, f_val):
    with sqlite3.connect("bot_database.db") as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO lotteries (text, entities, channels, finish_type, finish_value) VALUES (?, ?, ?, ?, ?)",
                    (text, entities, channels, f_type, f_val))
        conn.commit()
        return cur.lastrowid

def update_lot_db(lot_id, field, value):
    with sqlite3.connect("bot_database.db") as conn:
        conn.execute(f"UPDATE lotteries SET {field} = ? WHERE id = ?", (value, lot_id))

def get_lot(lot_id):
    with sqlite3.connect("bot_database.db") as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM lotteries WHERE id = ?", (lot_id,))
        return cur.fetchone()

def get_active_lots():
    with sqlite3.connect("bot_database.db") as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM lotteries WHERE status = 'active'")
        return cur.fetchall()

def close_lot_db(lot_id):
    with sqlite3.connect("bot_database.db") as conn:
        conn.execute("UPDATE lotteries SET status = 'closed' WHERE id = ?", (lot_id,))

def add_participant(user_id, lot_id):
    with sqlite3.connect("bot_database.db") as conn:
        conn.execute("INSERT OR IGNORE INTO participants (user_id, lot_id) VALUES (?, ?)", (user_id, lot_id))

def get_lot_participants(lot_id):
    with sqlite3.connect("bot_database.db") as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM participants WHERE lot_id = ?", (lot_id,))
        return [row[0] for row in cur.fetchall()]

# --- СОСТОЯНИЯ ---
class CreateLot(StatesGroup):
    text = State(); channels = State(); finish_type = State(); value = State()

class EditLot(StatesGroup):
    lot_id = State(); field = State(); new_value = State()

class PRApplication(StatesGroup):
    age = State(); nickname = State(); chats_count = State(); proofs = State()

# --- ЛОГИКА ЗАВЕРШЕНИЯ ---
async def finish_giveaway(lot_id):
    lot = get_lot(lot_id)
    if not lot or lot['status'] == 'closed': return
    close_lot_db(lot_id)
    users = get_lot_participants(lot_id)
    if not users:
        await bot.send_message(LOT_CHANNEL, f"🔔 Лотерея #{lot_id} окончена. Участников нет.", reply_to_message_id=lot['message_id'])
        return
    winner_id = random.choice(users)
    try:
        chat = await bot.get_chat(winner_id)
        mention = f"@{chat.username}" if chat.username else f"[{chat.first_name}](tg://user?id={winner_id})"
    except: mention = f"ID: {winner_id}"
    await bot.send_message(LOT_CHANNEL, f"🎊 **Итоги розыгрыша #{lot_id}!**\n\nПобедитель: {mention} 🏆\nВсего: {len(users)}", parse_mode="Markdown", reply_to_message_id=lot['message_id'])

# --- АДМИНКА ---
@dp.message(Command("admin"), F.from_user.id.in_(ADMIN_IDS))
async def cmd_admin(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Создать Лотерею", callback_data="admin_create")],
        [InlineKeyboardButton(text="📋 Список активных", callback_data="admin_list")]
    ])
    await message.answer("🛠 **Админ-панель**", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "admin_list", F.from_user.id.in_(ADMIN_IDS))
async def admin_list(callback: CallbackQuery):
    lots = get_active_lots()
    if not lots: return await callback.answer("Активных нет", show_alert=True)
    kb = [[InlineKeyboardButton(text=f"#{l['id']} | {l['finish_value']}", callback_data=f"edit_{l['id']}")] for l in lots]
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")])
    await callback.message.edit_text("Выбери лотерею:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("edit_"), F.from_user.id.in_(ADMIN_IDS))
async def edit_menu(callback: CallbackQuery):
    lot_id = int(callback.data.split("_")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Изменить Текст", callback_data=f"upd_text_{lot_id}")],
        [InlineKeyboardButton(text="🔗 Изменить Каналы", callback_data=f"upd_ch_{lot_id}")],
        [InlineKeyboardButton(text="⏰ Изменить Финиш", callback_data=f"upd_val_{lot_id}")],
        [InlineKeyboardButton(text="🛑 Завершить сейчас", callback_data=f"stop_{lot_id}")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="admin_list")]
    ])
    await callback.message.edit_text(f"Управление лотереей #{lot_id}:", reply_markup=kb)

@dp.callback_query(F.data.startswith("upd_"), F.from_user.id.in_(ADMIN_IDS))
async def start_update(callback: CallbackQuery, state: FSMContext):
    _, field, lot_id = callback.data.split("_")
    await state.update_data(lot_id=lot_id, field=field)
    prompts = {"text": "Отправь новый текст поста:", "ch": "Укажи новые каналы (@ch1 @ch2):", "val": "Укажи новое значение (Дата или Кол-во):"}
    await callback.message.answer(prompts[field])
    await state.set_state(EditLot.new_value)
    await callback.answer()

@dp.message(EditLot.new_value)
async def process_update(message: Message, state: FSMContext):
    data = await state.get_data(); lot_id = int(data['lot_id']); field = data['field']
    lot = get_lot(lot_id)
    
    if field == "text":
        ents = [e.model_dump_json() for e in message.entities] if message.entities else []
        update_lot_db(lot_id, "text", message.text)
        update_lot_db(lot_id, "entities", json.dumps(ents))
        # Сразу редактируем пост в канале
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Участвовать!", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")]])
        try: await bot.edit_message_text(chat_id=LOT_CHANNEL, message_id=lot['message_id'], text=message.text, entities=message.entities, reply_markup=kb)
        except Exception as e: await message.answer(f"Ошибка обновления в канале: {e}")
    
    elif field == "ch": update_lot_db(lot_id, "channels", message.text.replace(" ", ","))
    elif field == "val": update_lot_db(lot_id, "finish_value", message.text)
    
    await message.answer(f"✅ Параметр {field} успешно обновлен для #{lot_id}!")
    await state.clear()

@dp.callback_query(F.data.startswith("stop_"), F.from_user.id.in_(ADMIN_IDS))
async def stop_now(callback: CallbackQuery):
    await finish_giveaway(int(callback.data.split("_")[1]))
    await callback.answer("Завершено!")
    await admin_list(callback)

# --- СОЗДАНИЕ (КРАТКО) ---
@dp.callback_query(F.data == "admin_create", F.from_user.id.in_(ADMIN_IDS))
async def sc(c: CallbackQuery, s: FSMContext):
    await c.message.answer("Текст лоты:"); await s.set_state(CreateLot.text); await c.answer()

@dp.message(CreateLot.text)
async def pt(m: Message, s: FSMContext):
    ents = [e.model_dump_json() for e in m.entities] if m.entities else []
    await s.update_data(t=m.text, e=json.dumps(ents)); await m.answer("Каналы:"); await s.set_state(CreateLot.channels)

@dp.message(CreateLot.channels)
async def pc(m: Message, s: FSMContext):
    await s.update_data(c=m.text.replace(" ", ",")); await m.answer("Тип (time/count):"); await s.set_state(CreateLot.finish_type)

@dp.message(CreateLot.finish_type)
async def pft(m: Message, s: FSMContext):
    await s.update_data(ft=m.text); await m.answer("Значение (Дата или Число):"); await s.set_state(CreateLot.value)

@dp.message(CreateLot.value)
async def pf(m: Message, s: FSMContext):
    d = await s.get_data(); lot_id = create_lot_db(d['t'], d['e'], d['c'], d['ft'], m.text)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Участвовать!", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")]])
    ents = [types.MessageEntity(**json.loads(e)) for e in json.loads(d['e'])] if d['e'] else None
    sent = await bot.send_message(LOT_CHANNEL, text=d['t'], entities=ents, reply_markup=kb)
    update_lot_db(lot_id, "message_id", sent.message_id)
    await m.answer(f"🚀 Запущено #{lot_id}!"); await s.clear()

# --- ЮЗЕРЫ ---
@dp.message(Command("start"))
async def start(m: Message, cmd: CommandObject):
    if cmd.args and cmd.args.startswith("lot_"):
        lot_id = int(cmd.args.split("_")[1]); lot = get_lot(lot_id)
        if not lot or lot['status'] == 'closed': return await m.answer("⚠️ Закрыто!")
        channels = lot['channels'].split(",") if lot['channels'] != 'нет' else []
        for ch in channels:
            try:
                res = await bot.get_chat_member(ch.strip(), m.from_user.id)
                if res.status in ["left", "kicked"]: return await m.answer(f"❌ Подпишись на {ch}")
            except: continue
        add_participant(m.from_user.id, lot_id); await m.answer(f"✅ Ты в игре #{lot_id}!")
        if lot['finish_type'] == "count" and len(get_lot_participants(lot_id)) >= int(lot['finish_value']): await finish_giveaway(lot_id)
    else: await m.answer("Привет! Ищи лоты в канале.")

async def main():
    init_db(); await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
