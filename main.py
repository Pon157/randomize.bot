import os
import random
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

def db_query(query, params=(), fetchone=False, fetchall=False, commit=False):
    with sqlite3.connect("bot_database.db") as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, params)
        if commit: conn.commit()
        if fetchone: return cur.fetchone()
        if fetchall: return cur.fetchall()
        return cur.lastrowid

# --- СОСТОЯНИЯ ---
class CreateLot(StatesGroup):
    text = State(); channels = State(); finish_type = State(); value = State()
class EditLot(StatesGroup):
    lot_id = State(); field = State(); new_value = State()
class PRApplication(StatesGroup):
    age = State(); nickname = State(); chats_count = State(); proofs = State()

# --- ЛОГИКА ЗАВЕРШЕНИЯ ---
async def finish_giveaway(lot_id):
    lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or lot['status'] == 'closed': return
    
    db_query("UPDATE lotteries SET status = 'closed' WHERE id = ?", (lot_id,), commit=True)
    rows = db_query("SELECT user_id FROM participants WHERE lot_id = ?", (lot_id,), fetchall=True)
    users = [r[0] for r in rows]
    
    if not users:
        await bot.send_message(LOT_CHANNEL, f"🔔 Лотерея #{lot_id} окончена. Участников нет.", reply_to_message_id=lot['message_id'])
        return

    winner_id = random.choice(users)
    try:
        chat = await bot.get_chat(winner_id)
        mention = f"@{chat.username}" if chat.username else f"[{chat.first_name}](tg://user?id={winner_id})"
    except: mention = f"ID: {winner_id}"
    
    await bot.send_message(LOT_CHANNEL, f"🎊 **Итоги розыгрыша #{lot_id}!**\n\nПобедитель: {mention} 🏆\nВсего: {len(users)}", parse_mode="Markdown", reply_to_message_id=lot['message_id'])

# --- ОБРАБОТЧИК START ---
@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject):
    user_id = message.from_user.id
    if command.args and command.args.startswith("lot_"):
        lot_id = int(command.args.split("_")[1])
        lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
        
        if not lot: return await message.answer("❌ Лотерея не найдена.")
        if lot['status'] == 'closed': return await message.answer("⚠️ Эта лотерея уже завершена!")
        
        # Проверка времени финиша при попытке входа
        if lot['finish_type'] == "time":
            try:
                if datetime.now() >= datetime.strptime(lot['finish_value'], "%d.%m.%Y %H:%M"):
                    await finish_giveaway(lot_id)
                    return await message.answer("⚠️ Время этой лотереи вышло!")
            except: pass

        if db_query("SELECT 1 FROM participants WHERE user_id = ? AND lot_id = ?", (user_id, lot_id), fetchone=True):
            return await message.answer("✅ Ты уже участвуешь!")

        channels = lot['channels'].split(",") if lot['channels'] and lot['channels'] != 'нет' else []
        for ch in channels:
            try:
                m = await bot.get_chat_member(ch.strip(), user_id)
                if m.status in ["left", "kicked"]:
                    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Проверить", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")]])
                    return await message.answer(f"❌ Подпишись на канал: {ch}", reply_markup=kb)
            except: continue

        db_query("INSERT OR IGNORE INTO participants (user_id, lot_id) VALUES (?, ?)", (user_id, lot_id), commit=True)
        await message.answer(f"🎉 Ты в списке участников #{lot_id}!")

        if lot['finish_type'] == "count":
            count = db_query("SELECT COUNT(*) FROM participants WHERE lot_id = ?", (lot_id,), fetchone=True)[0]
            if count >= int(lot['finish_value']): await finish_giveaway(lot_id)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💼 PR Заявка", callback_data="apply_pr")]])
    await message.answer("Привет! Ищи розыгрыши в канале или подай заявку на PR.", reply_markup=kb)

# --- АДМИНКА ---
@dp.message(Command("admin"), F.from_user.id.in_(ADMIN_IDS))
async def cmd_admin(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Создать Лотерею", callback_data="admin_create")],
        [InlineKeyboardButton(text="📋 Активные лоты", callback_data="admin_list")]
    ])
    await message.answer("🛠 Админ-панель", reply_markup=kb)

@dp.callback_query(F.data == "admin_list", F.from_user.id.in_(ADMIN_IDS))
async def admin_list(callback: CallbackQuery):
    lots = db_query("SELECT * FROM lotteries WHERE status = 'active'", fetchall=True)
    if not lots: return await callback.answer("Активных нет", show_alert=True)
    kb = [[InlineKeyboardButton(text=f"#{l['id']} | {l['finish_value']}", callback_data=f"edit_{l['id']}")] for l in lots]
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")])
    await callback.message.edit_text("Выбери лотерею:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("edit_"), F.from_user.id.in_(ADMIN_IDS))
async def edit_menu(callback: CallbackQuery):
    lot_id = int(callback.data.split("_")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Текст", callback_data=f"upd_text_{lot_id}"), InlineKeyboardButton(text="🔗 Каналы", callback_data=f"upd_ch_{lot_id}")],
        [InlineKeyboardButton(text="⏰ Финиш", callback_data=f"upd_val_{lot_id}"), InlineKeyboardButton(text="🛑 Стоп", callback_data=f"stop_{lot_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_list")]
    ])
    await callback.message.edit_text(f"Управление #{lot_id}:", reply_markup=kb)

@dp.callback_query(F.data.startswith("upd_"), F.from_user.id.in_(ADMIN_IDS))
async def start_update(callback: CallbackQuery, state: FSMContext):
    _, field, lot_id = callback.data.split("_")
    await state.update_data(lot_id=lot_id, field=field)
    await callback.message.answer("Отправь новое значение:")
    await state.set_state(EditLot.new_value); await callback.answer()

@dp.message(EditLot.new_value)
async def process_update(message: Message, state: FSMContext):
    d = await state.get_data(); lot_id = int(d['lot_id']); field = d['field']
    if field == "text":
        ents = json.dumps([e.model_dump_json() for e in message.entities]) if message.entities else "[]"
        db_query("UPDATE lotteries SET text = ?, entities = ? WHERE id = ?", (message.text, ents, lot_id), commit=True)
    elif field == "ch": db_query("UPDATE lotteries SET channels = ? WHERE id = ?", (message.text.replace(" ", ","), lot_id), commit=True)
    elif field == "val": db_query("UPDATE lotteries SET finish_value = ? WHERE id = ?", (message.text, lot_id), commit=True)
    await message.answer("✅ Обновлено!"); await state.clear()

@dp.callback_query
