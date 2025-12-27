import os
import random
import logging
import asyncio
import sqlite3
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery

# --- НАСТРОЙКА ---
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
PR_CHAT_ID = int(os.getenv("PR_CHAT_ID"))

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

def get_participants():
    with sqlite3.connect("bot_database.db") as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM participants")
        return [row[0] for row in cur.fetchall()]

def clear_participants():
    with sqlite3.connect("bot_database.db") as conn:
        conn.execute("DELETE FROM participants")

def set_channels_db(channels_str):
    with sqlite3.connect("bot_database.db") as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('channels', ?)", (channels_str,))

def get_channels_db():
    with sqlite3.connect("bot_database.db") as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = 'channels'")
        res = cur.fetchone()
        return res[0].split(",") if res and res[0] else []

# --- СОСТОЯНИЯ (FSM) ---
class PRApplication(StatesGroup):
    age = State()
    nickname = State()
    chats_count = State()
    proofs = State()

# --- КЛАВИАТУРЫ ---
def get_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎉 Участвовать в розыгрыше", callback_data="participate")],
        [InlineKeyboardButton(text="💼 Стать PR-менеджером", callback_data="apply_pr")]
    ])

# --- АДМИН-КОМАНДЫ ---

@dp.message(Command("set_channels"), F.from_user.id.in_(ADMIN_IDS))
async def set_channels(message: Message):
    channels = message.text.split()[1:]
    if not channels:
        return await message.answer("⚠️ Укажи каналы через пробел: `/set_channels @chan1 @chan2`", parse_mode="Markdown")
    
    set_channels_db(",".join(channels))
    clear_participants()
    await message.answer(f"✅ Каналы установлены: {', '.join(channels)}\nБаза участников очищена.")

@dp.message(Command("draw"), F.from_user.id.in_(ADMIN_IDS))
async def draw_winner(message: Message):
    users = get_participants()
    if not users:
        return await message.answer("🤷‍♂️ Участников нет.")
    
    winner_id = random.choice(users)
    try:
        chat = await bot.get_chat(winner_id)
        name = f"@{chat.username}" if chat.username else chat.first_name
    except:
        name = f"ID: {winner_id}"
    
    await message.answer(f"🏆 Победитель: {name}\nВсего участников было: {len(users)}")

# --- ЛОГИКА КОНКУРСА ---

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(f"Привет, {message.from_user.first_name}! Жми кнопки:", reply_markup=get_main_keyboard())

@dp.callback_query(F.data == "participate")
async def participate_btn(callback: CallbackQuery):
    channels = get_channels_db()
    if not channels:
        return await callback.answer("Розыгрыш еще не начался.", show_alert=True)

    for ch in channels:
        try:
            member = await bot.get_chat_member(ch.strip(), callback.from_user.id)
            if member.status in ["left", "kicked"]:
                return await callback.answer(f"❌ Подпишись на канал {ch}!", show_alert=True)
        except Exception:
            continue # Пропускаем, если бота нет в канале

    add_participant(callback.from_user.id)
    await callback.answer("✅ Ты успешно зарегистрирован!", show_alert=True)

# --- ЛОГИКА PR-АНКЕТЫ ---

@dp.callback_query(F.data == "apply_pr")
async def pr_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("1️⃣ Напишите ваш возраст:")
    await state.set_state(PRApplication.age)
    await callback.answer()

@dp.message(PRApplication.age)
async def pr_age(message: Message, state: FSMContext):
    await state.update_data(age=message.text)
    await message.answer("2️⃣ Ваш ник (как к вам обращаться)?")
    await state.set_state(PRApplication.nickname)

@dp.message(PRApplication.nickname)
async def pr_nick(message: Message, state: FSMContext):
    await state.update_data(nickname=message.text)
    await message.answer("3️⃣ В какое количество чатов вы раскидываете рекламу?")
    await state.set_state(PRApplication.chats_count)

@dp.message(PRApplication.chats_count)
async def pr_chats(message: Message, state: FSMContext):
    await state.update_data(chats_count=message.text)
    await message.answer("4️⃣ Пришлите скриншот (доказательство работы):")
    await state.set_state(PRApplication.proofs)

@dp.message(PRApplication.proofs, F.photo)
async def pr_done(message: Message, state: FSMContext):
    data = await state.get_data()
    user = message.from_user
    
    caption = (
        f"📩 **НОВАЯ ЗАЯВКА (PR)**\n\n"
        f"👤 Юзер: @{user.username or 'нет'} (ID: `{user.id}`)\n"
        f"🎂 Возраст: {data['age']}\n"
        f"🏷 Ник: {data['nickname']}\n"
        f"📊 Чатов: {data['chats_count']}"
    )
    
    await bot.send_photo(PR_CHAT_ID, message.photo[-1].file_id, caption=caption, parse_mode="Markdown")
    await message.answer("✅ Твоя заявка отправлена админам! Ожидай ответа.")
    await state.clear()

@dp.message(PRApplication.proofs)
async def pr_wrong_format(message: Message):
    await message.answer("📸 Нужно отправить именно фото (скриншот)!")

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
