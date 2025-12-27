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

# --- 1. НАСТРОЙКИ ---
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
PR_CHAT_ID = int(os.getenv("PR_CHAT_ID"))
LOT_CHANNEL = "@lotsvitechek" 

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- 2. БАЗА ДАННЫХ (УЛУЧШЕННАЯ) ---
def init_db():
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    # Таблица лотерей
    cur.execute("""
        CREATE TABLE IF NOT EXISTS lotteries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT,
            entities TEXT,
            channels TEXT,
            finish_type TEXT,
            finish_value TEXT,
            status TEXT DEFAULT 'active',
            message_id INTEGER
        )
    """)
    # Таблица участников
    cur.execute("""
        CREATE TABLE IF NOT EXISTS participants (
            user_id INTEGER,
            lot_id INTEGER,
            PRIMARY KEY (user_id, lot_id)
        )
    """)
    conn.commit()
    conn.close()

# Функции работы с БД
def create_lot_db(text, entities_json, channels, f_type, f_val):
    with sqlite3.connect("bot_database.db") as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO lotteries (text, entities, channels, finish_type, finish_value) VALUES (?, ?, ?, ?, ?)",
                    (text, entities_json, channels, f_type, f_val))
        conn.commit()
        return cur.lastrowid

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

def is_participant(user_id, lot_id):
    with sqlite3.connect("bot_database.db") as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM participants WHERE user_id = ? AND lot_id = ?", (user_id, lot_id))
        return cur.fetchone() is not None

def get_lot_participants(lot_id):
    with sqlite3.connect("bot_database.db") as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM participants WHERE lot_id = ?", (lot_id,))
        return [row[0] for row in cur.fetchall()]

# --- 3. СОСТОЯНИЯ (FSM) ---
class CreateLot(StatesGroup):
    text = State()
    channels = State()
    finish_type = State()
    value = State()

class PRApplication(StatesGroup):
    age = State()
    nickname = State()
    chats_count = State()
    proofs = State()

# --- 4. ЛОГИКА ЗАВЕРШЕНИЯ ---
async def finish_giveaway(lot_id):
    lot = get_lot(lot_id)
    if not lot or lot['status'] == 'closed': return
    
    close_lot_db(lot_id)
    users = get_lot_participants(lot_id)
    
    if not users:
        await bot.send_message(LOT_CHANNEL, f"🔔 Лотерея #{lot_id} окончена. Участников не было.", reply_to_message_id=lot['message_id'])
        return

    winner_id = random.choice(users)
    try:
        chat = await bot.get_chat(winner_id)
        winner_mention = f"@{chat.username}" if chat.username else f"[{chat.first_name}](tg://user?id={winner_id})"
    except:
        winner_mention = f"ID: {winner_id}"

    text = f"🎊 **Итоги розыгрыша #{lot_id}!**\n\nПоздравляем победителя: {winner_mention} 🏆\nВсего участников: {len(users)}"
    await bot.send_message(LOT_CHANNEL, text, parse_mode="Markdown", reply_to_message_id=lot['message_id'])

# --- 5. АДМИН-ПАНЕЛЬ ---
@dp.message(Command("admin"), F.from_user.id.in_(ADMIN_IDS))
async def cmd_admin(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Создать новую лотерею", callback_data="admin_create")],
        [InlineKeyboardButton(text="📋 Список активных лот", callback_data="admin_list")]
    ])
    await message.answer("🛠 **Панель администратора**", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "admin_list", F.from_user.id.in_(ADMIN_IDS))
async def admin_list_lots(callback: CallbackQuery):
    lots = get_active_lots()
    if not lots:
        return await callback.answer("❌ Активных лотерей нет.", show_alert=True)
    
    kb = []
    for lot in lots:
        kb.append([InlineKeyboardButton(text=f"Лота #{lot['id']} | {lot['finish_value']}", callback_data=f"manage_{lot['id']}")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")])
    
    await callback.message.edit_text("Выберите лотерею для управления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("manage_"), F.from_user.id.in_(ADMIN_IDS))
async def manage_lot(callback: CallbackQuery):
    lot_id = int(callback.data.split("_")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛑 Завершить сейчас", callback_data=f"stop_{lot_id}")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="admin_list")]
    ])
    await callback.message.edit_text(f"Управление лотереей #{lot_id}\nСтатус: Активна", reply_markup=kb)

@dp.callback_query(F.data.startswith("stop_"), F.from_user.id.in_(ADMIN_IDS))
async def stop_lot_btn(callback: CallbackQuery):
    lot_id = int(callback.data.split("_")[1])
    await finish_giveaway(lot_id)
    await callback.answer(f"Лотерея #{lot_id} завершена!")
    await admin_list_lots(callback)

# Конструктор лотереи
@dp.callback_query(F.data == "admin_create", F.from_user.id.in_(ADMIN_IDS))
async def start_create(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("1. Отправь текст для лотереи (с оформлением):")
    await state.set_state(CreateLot.text)
    await callback.answer()

@dp.message(CreateLot.text)
async def process_text(message: Message, state: FSMContext):
    ents = [e.model_dump_json() for e in message.entities] if message.entities else []
    await state.update_data(text=message.text, entities=json.dumps(ents))
    await message.answer("2. Укажи каналы через пробел (@chan1 @chan2) или 'нет':")
    await state.set_state(CreateLot.channels)

@dp.message(CreateLot.channels)
async def process_channels(message: Message, state: FSMContext):
    await state.update_data(channels=message.text.replace(" ", ","))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏰ По времени", callback_data="stype_time")],
        [InlineKeyboardButton(text="👥 По количеству людей", callback_data="stype_count")]
    ])
    await message.answer("3. Как завершаем лотерею?", reply_markup=kb)
    await state.set_state(CreateLot.finish_type)

@dp.callback_query(CreateLot.finish_type)
async def process_finish_type(callback: CallbackQuery, state: FSMContext):
    t = "time" if callback.data == "stype_time" else "count"
    await state.update_data(finish_type=t)
    prompt = "Введите дату (ДД.ММ.ГГГГ ЧЧ:ММ):" if t == "time" else "Введите нужное кол-во участников:"
    await callback.message.answer(prompt)
    await state.set_state(CreateLot.value)
    await callback.answer()

@dp.message(CreateLot.value)
async def process_final(message: Message, state: FSMContext):
    data = await state.get_data()
    lot_id = create_lot_db(data['text'], data['entities'], data['channels'], data['finish_type'], message.text)
    
    bot_info = await bot.get_me()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Участвовать!", url=f"https://t.me/{bot_info.username}?start=lot_{lot_id}")]])
    
    # Восстанавливаем сущности (эмодзи, ссылки)
    ents_data = json.loads(data['entities'])
    entities = [types.MessageEntity(**json.loads(e)) for e in ents_data] if ents_data else None
    
    sent = await bot.send_message(LOT_CHANNEL, text=data['text'], entities=entities, reply_markup=kb)
    
    with sqlite3.connect("bot_database.db") as conn:
        conn.execute("UPDATE lotteries SET message_id = ? WHERE id = ?", (sent.message_id, lot_id))
    
    await message.answer(f"🚀 Лотерея #{lot_id} опубликована в {LOT_CHANNEL}!")
    await state.clear()

    if data['finish_type'] == "time":
        try:
            end_dt = datetime.strptime(message.text, "%d.%m.%Y %H:%M")
            delay = (end_dt - datetime.now()).total_seconds()
            if delay > 0:
                async def delayed_finish(l_id, d):
                    await asyncio.sleep(d)
                    await finish_giveaway(l_id)
                asyncio.create_task(delayed_finish(lot_id, delay))
        except: pass

# --- 6. ЛОГИКА ДЛЯ ПОЛЬЗОВАТЕЛЕЙ ---
@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject):
    user_id = message.from_user.id
    if command.args and command.args.startswith("lot_"):
        lot_id = int(command.args.split("_")[1])
        lot = get_lot(lot_id)
        
        if not lot: return await message.answer("❌ Лотерея не найдена.")
        if lot['status'] == 'closed': return await message.answer("⚠️ Эта лотерея уже завершена!")
        if is_participant(user_id, lot_id): return await message.answer("⚠️ Вы уже участвуете!")

        # Проверка подписки
        channels = lot['channels'].split(",") if lot['channels'] and lot['channels'] != 'нет' else []
        for ch in channels:
            if not ch.strip(): continue
            try:
                m = await bot.get_chat_member(ch.strip(), user_id)
                if m.status in ["left", "kicked"]:
                    return await message.answer(f"❌ Сначала подпишись на канал {ch}!")
            except Exception: continue

        add_participant(user_id, lot_id)
        await message.answer(f"✅ Готово! Ты в списке участников лотереи #{lot_id}.")

        # Проверка лимита по количеству
        if lot['finish_type'] == "count":
            if len(get_lot_participants(lot_id)) >= int(lot['finish_value']):
                await finish_giveaway(lot_id)
        return

    # Обычное меню
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💼 Стать PR-менеджером", callback_data="apply_pr")]])
    await message.answer("Привет! Ищи активные розыгрыши в нашем канале или подай заявку на PR.", reply_markup=kb)

# --- 7. PR-АНКЕТА ---
@dp.callback_query(F.data == "apply_pr")
async def pr_start(c: CallbackQuery, state: FSMContext):
    await c.message.answer("Твой возраст?"); await state.set_state(PRApplication.age); await c.answer()

@dp.message(PRApplication.age)
async def pr_age(m: Message, state: FSMContext):
    await state.update_data(age=m.text); await m.answer("Твой ник?"); await state.set_state(PRApplication.nickname)

@dp.message(PRApplication.nickname)
async def pr_nick(m: Message, state: FSMContext):
    await state.update_data(nick=m.text); await m.answer("Кол-во чатов?"); await state.set_state(PRApplication.chats_count)

@dp.message(PRApplication.chats_count)
async def pr_chats(m: Message, state: FSMContext):
    await state.update_data(chats=m.text); await m.answer("Скинь скриншот (пруфы):"); await state.set_state(PRApplication.proofs)

@dp.message(PRApplication.proofs, F.photo)
async def pr_end(m: Message, state: FSMContext):
    d = await state.get_data()
    cap = f"📩 НОВАЯ PR ЗАЯВКА\nЮзер: @{m.from_user.username}\nВозраст: {d['age']}\nНик: {d['nick']}\nЧатов: {d['chats']}"
    await bot.send_photo(PR_CHAT_ID, m.photo[-1].file_id, caption=cap)
    await m.answer("✅ Твоя заявка отправлена!"); await state.clear()

# --- 8. ЗАПУСК ---
async def main():
    init_db()
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
