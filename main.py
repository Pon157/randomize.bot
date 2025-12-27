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

# --- 1. НАСТРОЙКИ ---
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
PR_CHAT_ID = int(os.getenv("PR_CHAT_ID"))
LOT_CHANNEL = "@lotsvitechek" 

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- 2. БАЗА ДАННЫХ ---
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

# --- 3. СОСТОЯНИЯ ---
class CreateLot(StatesGroup):
    text = State()
    channels = State()
    finish_type = State()
    value = State()

class PRApplication(StatesGroup):
    age = State()
    nickname = State()
    proofs = State()

# --- 4. ЛОГИКА ЗАВЕРШЕНИЯ ---
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
    
    await bot.send_message(LOT_CHANNEL, f"🎊 **Итоги розыгрыша #{lot_id}!**\n\nПобедитель: {mention} 🏆\nУчастников: {len(users)}", parse_mode="Markdown", reply_to_message_id=lot['message_id'])

# --- 5. ОБРАБОТЧИК START ---
@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    user_id = message.from_user.id
    if command.args and command.args.startswith("lot_"):
        lot_id = int(command.args.split("_")[1])
        lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
        if not lot or lot['status'] == 'closed': return await message.answer("⚠️ Розыгрыш завершен!")

        if lot['finish_type'] == "time":
            if datetime.now() >= datetime.strptime(lot['finish_value'], "%d.%m.%Y %H:%M"):
                await finish_giveaway(lot_id)
                return await message.answer("⚠️ Время вышло!")

        if db_query("SELECT 1 FROM participants WHERE user_id = ? AND lot_id = ?", (user_id, lot_id), fetchone=True):
            return await message.answer(f"✅ Ты уже участвуешь в #{lot_id}")

        channels = lot['channels'].split(",") if lot['channels'] and lot['channels'] != 'нет' else []
        for ch in channels:
            try:
                m = await bot.get_chat_member(ch.strip(), user_id)
                if m.status in ["left", "kicked"]:
                    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Проверить подписку", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")]])
                    return await message.answer(f"❌ Подпишись на {ch}", reply_markup=kb)
            except: continue

        db_query("INSERT OR IGNORE INTO participants (user_id, lot_id) VALUES (?, ?)", (user_id, lot_id), commit=True)
        await message.answer(f"🎉 Записан в лотерею #{lot_id}!")

        if lot['finish_type'] == "count":
            res = db_query("SELECT COUNT(*) as count FROM participants WHERE lot_id = ?", (lot_id,), fetchone=True)
            if res['count'] >= int(lot['finish_value']): await finish_giveaway(lot_id)
        return

    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💼 PR Заявка", callback_data="apply_pr")]])
    await message.answer(f"Привет! Розыгрыши тут: {LOT_CHANNEL}", reply_markup=kb)

# --- 6. PR АНКЕТА ---
@dp.callback_query(F.data == "apply_pr")
async def pr_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("Шаг 1: Твой возраст?")
    await state.set_state(PRApplication.age); await callback.answer()

@dp.message(PRApplication.age)
async def pr_age(message: Message, state: FSMContext):
    await state.update_data(age=message.text)
    await message.answer("Шаг 2: Твой ник в ТГ?")
    await state.set_state(PRApplication.nickname)

@dp.message(PRApplication.nickname)
async def pr_nick(message: Message, state: FSMContext):
    await state.update_data(nick=message.text)
    await message.answer("Шаг 3: Пришли скрин работ (фото):")
    await state.set_state(PRApplication.proofs)

@dp.message(PRApplication.proofs, F.photo)
async def pr_end(message: Message, state: FSMContext):
    d = await state.get_data()
    cap = f"📩 PR ЗАЯВКА: @{message.from_user.username}\nВозраст: {d.get('age')}\nНик: {d.get('nick')}"
    await bot.send_photo(PR_CHAT_ID, message.photo[-1].file_id, caption=cap)
    await message.answer("✅ Отправлено!"); await state.clear()

# --- 7. АДМИНКА (КНОПКИ СОЗДАНИЯ) ---
@dp.message(Command("admin"), F.from_user.id.in_(ADMIN_IDS))
async def cmd_admin(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Создать лотерею", callback_data="admin_create")],
        [InlineKeyboardButton(text="📋 Список / Стоп", callback_data="admin_list")]
    ])
    await message.answer("🛠 Админ-панель", reply_markup=kb)

@dp.callback_query(F.data == "admin_create", F.from_user.id.in_(ADMIN_IDS))
async def cl_init(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("1. Пришли текст поста (с эмодзи):")
    await state.set_state(CreateLot.text); await callback.answer()

@dp.message(CreateLot.text)
async def cl_text(message: Message, state: FSMContext):
    ents = json.dumps([e.model_dump_json() for e in message.entities]) if message.entities else "[]"
    await state.update_data(text=message.text, entities=ents)
    await message.answer("2. Каналы через пробел (@chan1 @chan2):")
    await state.set_state(CreateLot.channels)

@dp.message(CreateLot.channels)
async def cl_channels(message: Message, state: FSMContext):
    await state.update_data(channels=message.text.replace(" ", ","))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏰ По времени", callback_data="set_type_time")],
        [InlineKeyboardButton(text="👥 По участникам", callback_data="set_type_count")]
    ])
    await message.answer("3. Выбери тип завершения:", reply_markup=kb)
    await state.set_state(CreateLot.finish_type)

@dp.callback_query(F.data.startswith("set_type_"), CreateLot.finish_type)
async def cl_type(callback: CallbackQuery, state: FSMContext):
    ftype = callback.data.split("_")[2]
    await state.update_data(ftype=ftype)
    
    if ftype == "time":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="1 час", callback_data="val_1h"), InlineKeyboardButton(text="3 часа", callback_data="val_3h")],
            [InlineKeyboardButton(text="1 день", callback_data="val_1d"), InlineKeyboardButton(text="3 дня", callback_data="val_3d")]
        ])
        await callback.message.edit_text("4. Через сколько завершить?", reply_markup=kb)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="10 чел", callback_data="val_10"), InlineKeyboardButton(text="50 чел", callback_data="val_50")],
            [InlineKeyboardButton(text="100 чел", callback_data="val_100"), InlineKeyboardButton(text="500 чел", callback_data="val_500")]
        ])
        await callback.message.edit_text("4. Сколько участников собрать?", reply_markup=kb)
    await state.set_state(CreateLot.value); await callback.answer()

@dp.callback_query(F.data.startswith("val_"), CreateLot.value)
async def cl_final(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    val_raw = callback.data.split("_")[1]
    
    # Расчет значения
    if data['ftype'] == "time":
        now = datetime.now()
        if val_raw == "1h": finish_val = (now + timedelta(hours=1)).strftime("%d.%m.%Y %H:%M")
        elif val_raw == "3h": finish_val = (now + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
        elif val_raw == "1d": finish_val = (now + timedelta(days=1)).strftime("%d.%m.%Y %H:%M")
        else: finish_val = (now + timedelta(days=3)).strftime("%d.%m.%Y %H:%M")
    else:
        finish_val = val_raw

    lot_id = db_query("INSERT INTO lotteries (text, entities, channels, finish_type, finish_value) VALUES (?, ?, ?, ?, ?)",
                      (data['text'], data['entities'], data['channels'], data['ftype'], finish_val), commit=True)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Участвовать!", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")]])
    ents_data = json.loads(data['entities'])
    ents = [types.MessageEntity(**json.loads(e)) for e in ents_data] if ents_data else None
    
    sent = await bot.send_message(LOT_CHANNEL, text=data['text'], entities=ents, reply_markup=kb)
    db_query("UPDATE lotteries SET message_id = ? WHERE id = ?", (sent.message_id, lot_id), commit=True)
    
    await callback.message.edit_text(f"🚀 Розыгрыш #{lot_id} запущен!\nУсловие: {finish_val}")
    await state.clear(); await callback.answer()

@dp.callback_query(F.data == "admin_list", F.from_user.id.in_(ADMIN_IDS))
async def admin_list(callback: CallbackQuery):
    lots = db_query("SELECT * FROM lotteries WHERE status = 'active'", fetchall=True)
    if not lots: return await callback.answer("Нет активных", show_alert=True)
    kb = [[InlineKeyboardButton(text=f"Стоп #{l['id']}", callback_data=f"stop_{l['id']}")] for l in lots]
    await callback.message.edit_text("Нажми кнопку, чтобы завершить досрочно:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("stop_"), F.from_user.id.in_(ADMIN_IDS))
async def manual_stop(callback: CallbackQuery):
    await finish_giveaway(int(callback.data.split("_")[1]))
    await callback.answer("Завершено!"); await admin_list(callback)

# --- 8. ЗАПУСК ---
async def main():
    init_db()
    print("Бот запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
