import os
import random
import asyncio
import sqlite3
import logging
from datetime import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery

# --- 1. НАСТРОЙКА ---
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
PR_CHAT_ID = int(os.getenv("PR_CHAT_ID"))
LOT_CHANNEL = "@lotsvitechek" # Канал, куда бот будет постить лоты

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- 2. БАЗА ДАННЫХ ---
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

# --- 3. СОСТОЯНИЯ (FSM) ---
class CreateLot(StatesGroup):
    text = State()
    channels = State()

class PRApplication(StatesGroup):
    age = State()
    nickname = State()
    chats_count = State()
    proofs = State()

# --- 4. КЛАВИАТУРЫ ---
def get_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎉 Участвовать в розыгрыше", callback_data="participate")],
        [InlineKeyboardButton(text="💼 Стать PR-менеджером", callback_data="apply_pr")]
    ])

def get_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Создать пост в канал", callback_data="admin_create")],
        [InlineKeyboardButton(text="🏆 Выбрать победителя (Рандом)", callback_data="admin_draw")],
        [InlineKeyboardButton(text="🗑 Очистить базу участников", callback_data="admin_clear")]
    ])

# --- 5. АДМИН-ПАНЕЛЬ И КОНСТРУКТОР ---

@dp.message(Command("admin"), F.from_user.id.in_(ADMIN_IDS))
async def cmd_admin(message: Message):
    await message.answer("🛠 **Панель управления администратора**", reply_markup=get_admin_kb(), parse_mode="Markdown")

@dp.callback_query(F.data == "admin_create", F.from_user.id.in_(ADMIN_IDS))
async def admin_create_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("⌨️ **Отправь текст поста.**\nПоддерживаются: жирный, курсив, ссылки и ПРЕМИУМ ЭМОДЗИ.")
    await state.set_state(CreateLot.text)
    await callback.answer()

@dp.message(CreateLot.text)
async def process_lot_text(message: Message, state: FSMContext):
    # Сохраняем текст и все "фишки" оформления
    await state.update_data(text=message.text, entities=message.entities)
    await message.answer("🔗 **Теперь укажи каналы для подписки через пробел.**\nПример: `@chan1 @chan2` (или напиши `нет`)")
    await state.set_state(CreateLot.channels)

@dp.message(CreateLot.channels)
async def process_lot_channels(message: Message, state: FSMContext):
    data = await state.get_data()
    channels_raw = message.text if message.text.lower() != "нет" else ""
    
    # Сохраняем настройки каналов в БД
    set_setting("channels", channels_raw.replace(" ", ","))
    clear_participants() # Новый пост — новая очередь

    # Кнопка для поста в канал
    bot_user = await bot.get_me()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Участвовать", url=f"https://t.me/{bot_user.username}?start=join")]
    ])

    # Публикуем
    try:
        await bot.send_message(
            chat_id=LOT_CHANNEL,
            text=data['text'],
            entities=data['entities'],
            reply_markup=kb
        )
        await message.answer(f"🚀 Пост успешно опубликован в {LOT_CHANNEL}!", reply_markup=get_admin_kb())
    except Exception as e:
        await message.answer(f"❌ Ошибка публикации: {e}")
    
    await state.clear()

@dp.callback_query(F.data == "admin_draw", F.from_user.id.in_(ADMIN_IDS))
async def admin_draw(callback: CallbackQuery):
    users = get_participants()
    if not users:
        return await callback.answer("🤷‍♂️ Участников еще нет!", show_alert=True)
    
    winner_id = random.choice(users)
    try:
        chat = await bot.get_chat(winner_id)
        name = f"@{chat.username}" if chat.username else f"ID: {winner_id}"
    except:
        name = f"ID: {winner_id}"
    
    await callback.message.answer(f"🏆 **Победитель выбран!**\nРезультат: {name}\nВсего было человек: {len(users)}", parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "admin_clear", F.from_user.id.in_(ADMIN_IDS))
async def admin_clear_db(callback: CallbackQuery):
    clear_participants()
    await callback.answer("База участников полностью очищена!", show_alert=True)

# --- 6. ЮЗЕР-ЛОГИКА И ПРОВЕРКИ ---

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        f"Привет, {message.from_user.first_name}! 👋\nЯ помогу тебе участвовать в розыгрышах.\nИспользуй кнопки ниже:",
        reply_markup=get_main_kb()
    )

@dp.callback_query(F.data == "participate")
async def process_participate(callback: CallbackQuery):
    channels_str = get_setting("channels")
    channels = channels_str.split(",") if channels_str else []
    
    user_id = callback.from_user.id
    
    # Проверка подписки
    for ch in channels:
        if not ch: continue
        try:
            member = await bot.get_chat_member(ch.strip(), user_id)
            if member.status in ["left", "kicked"]:
                return await callback.answer(f"❌ Ты не подписан на {ch}!", show_alert=True)
        except Exception:
            # Если бот не админ в канале, пропускаем проверку (или выводим ошибку)
            continue

    add_participant(user_id)
    await callback.answer("✅ Ура! Ты в списке участников.", show_alert=True)

# --- 7. PR-АНКЕТА ---

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
    await message.answer("3️⃣ В сколько чатов раскидываете рекламу?")
    await state.set_state(PRApplication.chats_count)

@dp.message(PRApplication.chats_count)
async def pr_chats(message: Message, state: FSMContext):
    await state.update_data(chats_count=message.text)
    await message.answer("4️⃣ Пришлите скриншот (пруф):")
    await state.set_state(PRApplication.proofs)

@dp.message(PRApplication.proofs, F.photo)
async def pr_done(message: Message, state: FSMContext):
    data = await state.get_data()
    user = message.from_user
    
    caption = (
        f"📩 **НОВАЯ ЗАЯВКА НА PR**\n\n"
        f"👤 Юзер: @{user.username or 'нет'} (ID: `{user.id}`)\n"
        f"🎂 Возраст: {data['age']}\n"
        f"🏷 Ник: {data['nickname']}\n"
        f"📊 Чатов: {data['chats_count']}"
    )
    
    await bot.send_photo(PR_CHAT_ID, message.photo[-1].file_id, caption=caption, parse_mode="Markdown")
    await message.answer("✅ Заявка отправлена админам!")
    await state.clear()

# --- 8. ЗАПУСК ---
async def main():
    init_db()
    print("Бот запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
