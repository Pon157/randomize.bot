import os
import random
import sqlite3
import logging
import json
import asyncio
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

# --- 3. СОСТОЯНИЯ (FSM) ---
class CreateLot(StatesGroup):
    text = State()
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
    
    await bot.send_message(LOT_CHANNEL, f"🎊 **Итоги розыгрыша #{lot_id}!**\n\nПобедитель: {mention} 🏆\nВсего участников: {len(users)}", parse_mode="Markdown", reply_to_message_id=lot['message_id'])

# --- 5. ОБРАБОТЧИК START ---
@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    user_id = message.from_user.id
    if command.args and command.args.startswith("lot_"):
        try:
            lot_id = int(command.args.split("_")[1])
            lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
            
            if not lot: return await message.answer("❌ Лотерея не найдена.")
            if lot['status'] == 'closed': return await message.answer("⚠️ Розыгрыш уже завершен!")
            
            # Проверка времени
            if lot['finish_type'] == "time":
                try:
                    if datetime.now() >= datetime.strptime(lot['finish_value'], "%d.%m.%Y %H:%M"):
                        await finish_giveaway(lot_id)
                        return await message.answer("⚠️ Время этой лотереи вышло!")
                except: pass

            if db_query("SELECT 1 FROM participants WHERE user_id = ? AND lot_id = ?", (user_id, lot_id), fetchone=True):
                return await message.answer(f"✅ Ты уже участвуешь в лотерее #{lot_id}")

            # Проверка подписки
            channels = lot['channels'].split(",") if lot['channels'] and lot['channels'] != 'нет' else []
            for ch in channels:
                try:
                    m = await bot.get_chat_member(ch.strip(), user_id)
                    if m.status in ["left", "kicked"]:
                        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Проверить подписку", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")]])
                        return await message.answer(f"❌ Сначала подпишись на канал: {ch}", reply_markup=kb)
                except Exception: continue

            db_query("INSERT OR IGNORE INTO participants (user_id, lot_id) VALUES (?, ?)", (user_id, lot_id), commit=True)
            await message.answer(f"🎉 Регистрация на лотерею #{lot_id} прошла успешно!")

            # Проверка финиша по количеству
            if lot['finish_type'] == "count":
                res = db_query("SELECT COUNT(*) as count FROM participants WHERE lot_id = ?", (lot_id,), fetchone=True)
                if res['count'] >= int(lot['finish_value']): await finish_giveaway(lot_id)
            return
        except Exception as e:
            logging.error(f"Error in start lot: {e}")

    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💼 PR Заявка", callback_data="apply_pr")]])
    await message.answer(f"Привет, {message.from_user.first_name}! 👋\nИщи розыгрыши в нашем канале {LOT_CHANNEL}.", reply_markup=kb)

# --- 6. PR АНКЕТА ---
@dp.callback_query(F.data == "apply_pr")
async def pr_start(callback: CallbackQuery, state: FSMContext):
    await state.clear() 
    await callback.message.answer("📝 **Заявка на PR-менеджера**\n\nШаг 1: Твой возраст?")
    await state.set_state(PRApplication.age)
    await callback.answer()

@dp.message(PRApplication.age)
async def pr_age(message: Message, state: FSMContext):
    await state.update_data(age=message.text)
    await message.answer("Шаг 2: Твой никнейм в Telegram?")
    await state.set_state(PRApplication.nickname)

@dp.message(PRApplication.nickname)
async def pr_nick(message: Message, state: FSMContext):
    await state.update_data(nick=message.text)
    await message.answer("Шаг 3: Пришли скриншот своих чатов/работ (фото):")
    await state.set_state(PRApplication.proofs)

@dp.message(PRApplication.proofs, F.photo)
async def pr_end(message: Message, state: FSMContext):
    data = await state.get_data()
    caption = (f"📩 **НОВАЯ PR ЗАЯВКА**\n\n"
               f"👤 От: @{message.from_user.username}\n"
               f"🎂 Возраст: {data.get('age')}\n"
               f"🆔 Ник: {data.get('nick')}")
    await bot.send_photo(PR_CHAT_ID, message.photo[-1].file_id, caption=caption)
    await message.answer("✅ Заявка отправлена! Мы свяжемся с тобой.")
    await state.clear()

# --- 7. АДМИНКА ---
@dp.message(Command("admin"), F.from_user.id.in_(ADMIN_IDS))
async def cmd_admin(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Создать лотерею", callback_data="admin_create")],
        [InlineKeyboardButton(text="📋 Список активных", callback_data="admin_list")]
    ])
    await message.answer("🛠 **Панель администратора**", reply_markup=kb)

@dp.callback_query(F.data == "admin_list", F.from_user.id.in_(ADMIN_IDS))
async def admin_list(callback: CallbackQuery):
    lots = db_query("SELECT * FROM lotteries WHERE status = 'active'", fetchall=True)
    if not lots: return await callback.answer("Активных лотерей нет", show_alert=True)
    buttons = [[InlineKeyboardButton(text=f"Лотерея #{l['id']}", callback_data=f"edit_{l['id']}")] for l in lots]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")])
    await callback.message.edit_text("Выберите лотерею для управления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("edit_"), F.from_user.id.in_(ADMIN_IDS))
async def edit_menu(callback: CallbackQuery):
    lot_id = int(callback.data.split("_")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Текст", callback_data=f"upd_text_{lot_id}"), InlineKeyboardButton(text="🔗 Каналы", callback_data=f"upd_ch_{lot_id}")],
        [InlineKeyboardButton(text="⏰ Лимиты", callback_data=f"upd_val_{lot_id}"), InlineKeyboardButton(text="🛑 Стоп", callback_data=f"stop_{lot_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_list")]
    ])
    await callback.message.edit_text(f"Управление лотереей #{lot_id}:", reply_markup=kb)

# --- 8. СОЗДАНИЕ ЛОТЕРЕИ ---
@dp.callback_query(F.data == "admin_create", F.from_user.id.in_(ADMIN_IDS))
async def cl_init(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("1. Отправь текст для поста (с эмодзи и оформлением):")
    await state.set_state(CreateLot.text)
    await callback.answer()

@dp.message(CreateLot.text)
async def cl_text(message: Message, state: FSMContext):
    entities = json.dumps([e.model_dump_json() for e in message.entities]) if message.entities else "[]"
    await state.update_data(text=message.text, entities=entities)
    await message.answer("2. Введи каналы через пробел (например: @chan1 @chan2):")
    await state.set_state(CreateLot.channels)

@dp.message(CreateLot.channels)
async def cl_channels(message: Message, state: FSMContext):
    await state.update_data(channels=message.text.replace(" ", ","))
    await message.answer("3. Тип завершения (напиши: time или count):")
    await state.set_state(CreateLot.finish_type)

@dp.message(CreateLot.finish_type)
async def cl_type(message: Message, state: FSMContext):
    await state.update_data(ftype=message.text.lower())
    await message.answer("4. Значение (Дата ДД.ММ.ГГГГ ЧЧ:ММ ИЛИ число участников):")
    await state.set_state(CreateLot.value)

@dp.message(CreateLot.value)
async def cl_final(message: Message, state: FSMContext):
    data = await state.get_data()
    # Простая валидация даты если выбран тип time
    if data['ftype'] == "time":
        try:
            datetime.strptime(message.text, "%d.%m.%Y %H:%M")
        except:
            return await message.answer("❌ Ошибка формата! Напиши дату как: 31.12.2025 23:59")

    lot_id = db_query("INSERT INTO lotteries (text, entities, channels, finish_type, finish_value) VALUES (?, ?, ?, ?, ?)",
                      (data['text'], data['entities'], data['channels'], data['ftype'], message.text), commit=True)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Участвовать!", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")]])
    
    ents_data = json.loads(data['entities'])
    ents = [types.MessageEntity(**json.loads(e)) for e in ents_data] if ents_data else None
    
    sent = await bot.send_message(LOT_CHANNEL, text=data['text'], entities=ents, reply_markup=kb)
    db_query("UPDATE lotteries SET message_id = ? WHERE id = ?", (sent.message_id, lot_id), commit=True)
    
    await message.answer(f"🚀 Розыгрыш #{lot_id} успешно запущен в канале!")
    await state.clear()

# --- 9. ВСПОМОГАТЕЛЬНЫЕ UPDATE ---
@dp.callback_query(F.data.startswith("upd_"), F.from_user.id.in_(ADMIN_IDS))
async def update_field(callback: CallbackQuery, state: FSMContext):
    _, field, lot_id = callback.data.split("_")
    await state.update_data(lot_id=lot_id, field=field)
    await callback.message.answer("Введите новое значение:")
    await state.set_state(EditLot.new_value)
    await callback.answer()

@dp.message(EditLot.new_value)
async def save_update(message: Message, state: FSMContext):
    data = await state.get_data()
    lot_id, field = int(data['lot_id']), data['field']
    if field == "text":
        ents = json.dumps([e.model_dump_json() for e in message.entities]) if message.entities else "[]"
        db_query("UPDATE lotteries SET text = ?, entities = ? WHERE id = ?", (message.text, ents, lot_id), commit=True)
    elif field == "ch":
        db_query("UPDATE lotteries SET channels = ? WHERE id = ?", (message.text.replace(" ", ","), lot_id), commit=True)
    elif field == "val":
        db_query("UPDATE lotteries SET finish_value = ? WHERE id = ?", (message.text, lot_id), commit=True)
    await message.answer("✅ Сохранено!")
    await state.clear()

@dp.callback_query(F.data.startswith("stop_"), F.from_user.id.in_(ADMIN_IDS))
async def manual_stop(callback: CallbackQuery):
    lot_id = int(callback.data.split("_")[1])
    await finish_giveaway(lot_id)
    await callback.answer("Розыгрыш остановлен!")
    await admin_list(callback)

# --- 10. ЗАПУСК ---
async def main():
    init_db()
    print("Бот запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
