import os
import random
import logging
import asyncio
import sqlite3
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery

# --- 1. Настройка ---
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]
PR_CHAT_ID = int(os.getenv("PR_CHAT_ID"))

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- 2. РАБОТА С БАЗОЙ ДАННЫХ ---
def init_db():
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    # Таблица участников конкурса
    cur.execute("""
        CREATE TABLE IF NOT EXISTS participants (
            user_id INTEGER PRIMARY KEY
        )
    """)
    # Таблица настроек (каналы)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    conn.close()

def add_participant(user_id):
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO participants (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def get_participants():
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM participants")
    users = [row[0] for row in cur.fetchall()]
    conn.close()
    return users

def clear_participants():
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    cur.execute("DELETE FROM participants")
    conn.commit()
    conn.close()

def set_channels_db(channels_str):
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('channels', ?)", (channels_str,))
    conn.commit()
    conn.close()

def get_channels_db():
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = 'channels'")
    result = cur.fetchone()
    conn.close()
    return result[0].split(",") if result else []

# --- 3. Состояния FSM ---
class PRApplication(StatesGroup):
    age = State()
    nickname = State()
    chats_count = State()
    proofs = State()

# --- 4. Клавиатуры ---
def get_main_keyboard():
    kb = [
        [InlineKeyboardButton(text="🎉 Участвовать", callback_data="participate")],
        [InlineKeyboardButton(text="💼 Стать PR-менеджером", callback_data="apply_pr")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

# --- 5. Админ-команды ---

@dp.message(Command("set_channels"), F.from_user.id.in_(ADMIN_IDS))
async def set_channels(message: Message):
    args = message.text.split()[1:]
    if not args:
        await message.answer("⚠️ Укажи каналы: `/set_channels @chan1 @chan2`", parse_mode="Markdown")
        return
    
    set_channels_db(",".join(args))
    clear_participants() # Сбрасываем старых участников при смене условий
    await message.answer(f"✅ Каналы сохранены в БД. Участники очищены.")

@dp.message(Command("draw"), F.from_user.id.in_(ADMIN_IDS))
async def draw_winner(message: Message):
    users = get_participants()
    if not users:
        await message.answer("🤷‍♂️ В базе данных нет участников.")
        return

    winner_id = random.choice(users)
    await message.answer(f"🏆 Победитель (ID): `{winner_id}`", parse_mode="Markdown")

# --- 6. Юзер-логика ---

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("Привет! Участвуй в розыгрыше или подавай заявку:", reply_markup=get_main_keyboard())

@dp.callback_query(F.data == "participate")
async def register_participant(callback: CallbackQuery):
    user_id = callback.from_user.id
    channels = get_channels_db()
    
    if not channels:
        await callback.answer("Конкурс еще не настроен админом.", show_alert=True)
        return

    # Проверка подписки
    for ch in channels:
        try:
            chat_member = await bot.get_chat_member(ch, user_id)
            if chat_member.status in ["left", "kicked"]:
                await callback.answer(f"❌ Подпишись на {ch}!", show_alert=True)
                return
        except Exception:
            continue # Если канал не найден или бот не админ

    add_participant(user_id)
    await callback.answer("✅ Ты в базе! Удачи!", show_alert=True)

# --- Логика PR (Анкета) остается такой же как в прошлом примере ---
@dp.callback_query(F.data == "apply_pr")
async def start_pr(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Твой возраст?")
    await state.set_state(PRApplication.age)

@dp.message(PRApplication.age)
async def pr_age(message: Message, state: FSMContext):
    await state.update_data(age=message.text)
    await message.answer("Твой ник?")
    await state.set_state(PRApplication.nickname)

@dp.message(PRApplication.nickname)
async def pr_nick(message: Message, state: FSMContext):
    await state.update_data(nickname=message.text)
    await message.answer("Сколько чатов?")
    await state.set_state(PRApplication.chats_count)

@dp.message(PRApplication.chats_count)
async def pr_chats(message: Message, state: FSMContext):
    await state.update_data(chats_count=message.text)
    await message.answer("Кидай скриншот (пруфы):")
    await state.set_state(PRApplication.proofs)

@dp.message(PRApplication.proofs, F.photo)
async def pr_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    text = (f"💼 Заявка PR:\nВозраст: {data['age']}\nНик: {data['nickname']}\nЧатов: {data['chats_count']}\n"
            f"От: @{message.from_user.username}")
    await bot.send_photo(PR_CHAT_ID, message.photo[-1].file_id, caption=text)
    await message.answer("✅ Отправлено!")
    await state.clear()

# --- 7. Запуск ---
async def main():
    init_db() # Создаем таблицы если их нет
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
