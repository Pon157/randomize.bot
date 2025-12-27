import os
import random
import asyncio
import sqlite3
import logging
from datetime import datetime, timedelta
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
    duration = State()

class PRApplication(StatesGroup):
    age = State()
    nickname = State()
    chats_count = State()
    proofs = State()

# --- АДМИНКА ---

@dp.message(Command("admin"), F.from_user.id.in_(ADMIN_IDS))
async def cmd_admin(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Создать Лотерею", callback_data="admin_create")],
        [InlineKeyboardButton(text="🗑 Очистить участников", callback_data="admin_clear")]
    ])
    await message.answer("🛠 **Админ-панель**", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "admin_create", F.from_user.id.in_(ADMIN_IDS))
async def admin_create_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("1. Отправь текст поста (с премиум эмодзи и т.д.):")
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
    await message.answer("3. Через сколько минут выбрать победителя автоматически?")
    await state.set_state(CreateLot.duration)

@dp.message(CreateLot.duration)
async def process_time(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        minutes = int(message.text)
    except:
        return await message.answer("Введи число (минуты)!")

    set_setting("channels", data['channels'])
    clear_participants()
    
    bot_user = await bot.get_me()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Участвовать!", url=f"https://t.me/{bot_user.username}?start=join")]
    ])

    sent_lot = await bot.send_message(LOT_CHANNEL, text=data['text'], entities=data['entities'], reply_markup=kb)
    await message.answer(f"🚀 Лота запущена на {minutes} мин!")
    await state.clear()

    # Запускаем таймер завершения
    await asyncio.sleep(minutes * 60)
    await finish_giveaway(sent_lot.message_id)

async def finish_giveaway(message_id):
    users = get_participants()
    if not users:
        await bot.send_message(LOT_CHANNEL, "🔔 Лотерея окончена. Участников не было.", reply_to_message_id=message_id)
        return

    winner_id = random.choice(users)
    try:
        chat = await bot.get_chat(winner_id)
        winner_mention = f"@{chat.username}" if chat.username else f"[{chat.first_name}](tg://user?id={winner_id})"
    except:
        winner_mention = f"ID: {winner_id}"

    text = f"🎊 **Итоги розыгрыша!**\n\nПоздравляем нашего победителя: {winner_mention} 🏆\nВсего участников: {len(users)}"
    await bot.send_message(LOT_CHANNEL, text, parse_mode="Markdown", reply_to_message_id=message_id)

# --- ЛОГИКА ДЛЯ ЮЗЕРОВ ---

@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject):
    # Если юзер пришел по кнопке из канала
    if command.args == "join":
        channels_str = get_setting("channels")
        channels = channels_str.split(",") if channels_str else []
        
        not_subscribed = []
        for ch in channels:
            if not ch: continue
            try:
                member = await bot.get_chat_member(ch.strip(), message.from_user.id)
                if member.status in ["left", "kicked"]:
                    not_subscribed.append(ch)
            except:
                continue

        if not_subscribed:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Проверить подписку снова", url=f"https://t.me/{(await bot.get_me()).username}?start=join")]])
            return await message.answer(f"❌ Ты не подписан на:\n" + "\n".join(not_subscribed), reply_markup=kb)

        add_participant(message.from_user.id)
        return await message.answer("✅ **Ты успешно зарегистрирован в розыгрыше!**\nЖди результатов в канале.")

    # Обычный старт (без кнопки)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💼 Стать PR-менеджером", callback_data="apply_pr")]])
    await message.answer(f"Привет, {message.from_user.first_name}! Здесь можно подать заявку на PR или участвовать в розыгрышах через наш канал {LOT_CHANNEL}.", reply_markup=kb)

# --- PR АНКЕТА (без изменений) ---
@dp.callback_query(F.data == "apply_pr")
async def pr_start(c: CallbackQuery, state: FSMContext):
    await c.message.answer("Твой возраст?")
    await state.set_state(PRApplication.age)
    await c.answer()

@dp.message(PRApplication.age)
async def pr_age(m: Message, state: FSMContext):
    await state.update_data(age=m.text)
    await m.answer("Твой ник?")
    await state.set_state(PRApplication.nickname)

@dp.message(PRApplication.nickname)
async def pr_nick(m: Message, state: FSMContext):
    await state.update_data(nickname=m.text)
    await m.answer("Сколько чатов?")
    await state.set_state(PRApplication.chats_count)

@dp.message(PRApplication.chats_count)
async def pr_chats(m: Message, state: FSMContext):
    await state.update_data(chats_count=m.text)
    await m.answer("Скинь скриншот (пруфы):")
    await state.set_state(PRApplication.proofs)

@dp.message(PRApplication.proofs, F.photo)
async def pr_done(m: Message, state: FSMContext):
    data = await state.get_data()
    cap = f"📩 PR ЗАЯВКА\nЮзер: @{m.from_user.username}\nВозраст: {data['age']}\nНик: {data['nickname']}\nЧатов: {data['chats_count']}"
    await bot.send_photo(PR_CHAT_ID, m.photo[-1].file_id, caption=cap)
    await m.answer("✅ Отправлено!")
    await state.clear()

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
