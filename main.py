import os
import random
import asyncio
import sqlite3
import logging
from datetime import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery

# --- НАСТРОЙКА ---
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
        cur.execute("CREATE TABLE IF NOT EXISTS participants (user_id INTEGER PRIMARY KEY)")
        cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()

def add_participant(user_id):
    with sqlite3.connect("bot_database.db") as conn:
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO participants (user_id) VALUES (?)", (user_id,))
        conn.commit()

def is_participant(user_id):
    with sqlite3.connect("bot_database.db") as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM participants WHERE user_id = ?", (user_id,))
        return cur.fetchone() is not None

def get_participants():
    with sqlite3.connect("bot_database.db") as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM participants")
        return [row[0] for row in cur.fetchall()]

def clear_participants():
    with sqlite3.connect("bot_database.db") as conn:
        conn.execute("DELETE FROM participants")

def set_setting(key, value):
    with sqlite3.connect("bot_database.db") as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))

def get_setting(key):
    with sqlite3.connect("bot_database.db") as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
        res = cur.fetchone()
        return res[0] if res else None

# --- СОСТОЯНИЯ ---
class CreateLot(StatesGroup):
    text = State()
    channels = State()
    finish_type = State() # Выбор: время или кол-во
    value = State()      # Сама дата или число людей

class PRApplication(StatesGroup):
    age = State()
    nickname = State()
    chats_count = State()
    proofs = State()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
async def finish_giveaway(message_id=None):
    users = get_participants()
    set_setting("active_lot_id", "none") # Деактивируем лоту
    
    if not users:
        await bot.send_message(LOT_CHANNEL, "🔔 Лотерея окончена. Участников не было.")
        return

    winner_id = random.choice(users)
    try:
        chat = await bot.get_chat(winner_id)
        winner_mention = f"@{chat.username}" if chat.username else f"[{chat.first_name}](tg://user?id={winner_id})"
    except:
        winner_mention = f"ID: {winner_id}"

    text = f"🎊 **Итоги розыгрыша!**\n\nПоздравляем победителя: {winner_mention} 🏆\nВсего участников: {len(users)}"
    await bot.send_message(LOT_CHANNEL, text, parse_mode="Markdown")

# --- АДМИНКА ---

@dp.message(Command("admin"), F.from_user.id.in_(ADMIN_IDS))
async def cmd_admin(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Создать Лотерею", callback_data="admin_create")],
        [InlineKeyboardButton(text="🛑 Завершить досрочно", callback_data="admin_stop")],
        [InlineKeyboardButton(text="🗑 Очистить базу", callback_data="admin_clear")]
    ])
    await message.answer("🛠 **Админ-панель**", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "admin_create", F.from_user.id.in_(ADMIN_IDS))
async def admin_create_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("1. Отправь текст поста:")
    await state.set_state(CreateLot.text)
    await callback.answer()

@dp.message(CreateLot.text)
async def process_text(message: Message, state: FSMContext):
    await state.update_data(text=message.text, entities=message.entities)
    await message.answer("2. Укажи каналы через пробел (@chan1 @chan2):")
    await state.set_state(CreateLot.channels)

@dp.message(CreateLot.channels)
async def process_ch(message: Message, state: FSMContext):
    await state.update_data(channels=message.text.replace(" ", ","))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏰ По времени", callback_data="type_time")],
        [InlineKeyboardButton(text="👥 По кол-ву людей", callback_data="type_count")]
    ])
    await message.answer("3. Выбери тип завершения лоты:", reply_markup=kb)
    await state.set_state(CreateLot.finish_type)

@dp.callback_query(CreateLot.finish_type)
async def process_type(callback: CallbackQuery, state: FSMContext):
    if callback.data == "type_time":
        await state.update_data(finish_type="time")
        await callback.message.answer("Введите дату и время завершения в формате:\n`ДД.ММ.ГГГГ ЧЧ:ММ`\nПример: `31.12.2025 23:59`", parse_mode="Markdown")
    else:
        await state.update_data(finish_type="count")
        await callback.message.answer("Введите необходимое количество участников (число):")
    await state.set_state(CreateLot.value)
    await callback.answer()

@dp.message(CreateLot.value)
async def process_value(message: Message, state: FSMContext):
    data = await state.get_data()
    val = message.text
    
    set_setting("channels", data['channels'])
    set_setting("lot_type", data['finish_type'])
    clear_participants()

    if data['finish_type'] == "time":
        try:
            end_dt = datetime.strptime(val, "%d.%m.%Y %H:%M")
            now = datetime.now()
            delay = (end_dt - now).total_seconds()
            if delay <= 0: return await message.answer("Дата уже прошла!")
            set_setting("lot_value", val)
        except: return await message.answer("Неверный формат даты!")
    else:
        set_setting("lot_value", val)

    bot_user = await bot.get_me()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Участвовать!", url=f"https://t.me/{bot_user.username}?start=join")]])
    
    sent = await bot.send_message(LOT_CHANNEL, text=data['text'], entities=data['entities'], reply_markup=kb)
    set_setting("active_lot_id", str(sent.message_id))
    
    await message.answer(f"🚀 Лота запущена! Тип: {data['finish_type']}, Значение: {val}")
    await state.clear()

    if data['finish_type'] == "time":
        await asyncio.sleep(delay)
        if get_setting("active_lot_id") != "none":
            await finish_giveaway()

@dp.callback_query(F.data == "admin_stop", F.from_user.id.in_(ADMIN_IDS))
async def admin_stop_lot(callback: CallbackQuery):
    if get_setting("active_lot_id") == "none":
        return await callback.answer("Нет активных лотерей!", show_alert=True)
    await finish_giveaway()
    await callback.message.answer("🛑 Лотерея завершена досрочно!")
    await callback.answer()

@dp.callback_query(F.data == "admin_clear", F.from_user.id.in_(ADMIN_IDS))
async def admin_clear(callback: CallbackQuery):
    clear_participants()
    await callback.answer("База очищена!", show_alert=True)

# --- ЛОГИКА ЮЗЕРОВ ---

@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject):
    user_id = message.from_user.id
    
    if command.args == "join":
        if is_participant(user_id):
            return await message.answer("⚠️ Вы уже участвуете в этой лотерее!")

        channels_str = get_setting("channels")
        channels = channels_str.split(",") if channels_str else []
        not_subscribed = []
        for ch in channels:
            if not ch: continue
            try:
                member = await bot.get_chat_member(ch.strip(), user_id)
                if member.status in ["left", "kicked"]: not_subscribed.append(ch)
            except: continue

        if not_subscribed:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Проверить снова", url=f"https://t.me/{(await bot.get_me()).username}?start=join")]])
            return await message.answer("❌ Подпишитесь на каналы:\n" + "\n".join(not_subscribed), reply_markup=kb)

        add_participant(user_id)
        await message.answer("✅ Ты успешно зарегистрирован!")

        # Проверка лимита участников
        if get_setting("lot_type") == "count":
            limit = int(get_setting("lot_value"))
            current_count = len(get_participants())
            if current_count >= limit and get_setting("active_lot_id") != "none":
                await finish_giveaway()
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💼 Стать PR-менеджером", callback_data="apply_pr")]])
    await message.answer(f"Привет! Подавай заявку на PR или заглядывай в {LOT_CHANNEL} для участия в лотах.", reply_markup=kb)

# --- PR АНКЕТА ---
@dp.callback_query(F.data == "apply_pr")
async def pr_start(c: CallbackQuery, state: FSMContext):
    await c.message.answer("Твой возраст?")
    await state.set_state(PRApplication.age)
    await c.answer()

@dp.message(PRApplication.age)
async def pr_age(m: Message, state: FSMContext):
    await state.update_data(age=m.text); await m.answer("Твой ник?"); await state.set_state(PRApplication.nickname)

@dp.message(PRApplication.nickname)
async def pr_nick(m: Message, state: FSMContext):
    await state.update_data(nickname=m.text); await m.answer("Сколько чатов?"); await state.set_state(PRApplication.chats_count)

@dp.message(PRApplication.chats_count)
async def pr_chats(m: Message, state: FSMContext):
    await state.update_data(chats_count=m.text); await m.answer("Скинь скриншот:"); await state.set_state(PRApplication.proofs)

@dp.message(PRApplication.proofs, F.photo)
async def pr_done(m: Message, state: FSMContext):
    data = await state.get_data()
    cap = f"📩 PR ЗАЯВКА\nЮзер: @{m.from_user.username}\nВозраст: {data['age']}\nНик: {data['nickname']}\nЧатов: {data['chats_count']}"
    await bot.send_photo(PR_CHAT_ID, m.photo[-1].file_id, caption=cap)
    await m.answer("✅ Отправлено!"); await state.clear()

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
