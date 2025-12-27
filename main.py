import os
import random
import logging
import asyncio
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
# Преобразуем строку ID админов в список чисел
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]
PR_CHAT_ID = int(os.getenv("PR_CHAT_ID"))

# Включаем логирование, чтобы видеть ошибки в консоли
logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- 2. Хранилище данных (в памяти) ---
# В реальном проекте лучше использовать базу данных (SQLite/PostgreSQL)
bot_data = {
    "required_channels": [],  # Список каналов для подписки (ID или username)
    "participants": set()     # Множество ID участников
}

# --- 3. Машина состояний (FSM) для анкеты PR ---
class PRApplication(StatesGroup):
    age = State()
    nickname = State()
    chats_count = State()
    proofs = State()

# --- 4. Клавиатуры ---
def get_main_keyboard():
    kb = [
        [InlineKeyboardButton(text="🎉 Участвовать в конкурсе", callback_data="participate")],
        [InlineKeyboardButton(text="💼 Подать заявку на PR-менеджера", callback_data="apply_pr")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

# --- 5. Админские функции ---

# Команда /set_channels @channel1 @channel2
@dp.message(Command("set_channels"), F.from_user.id.in_(ADMIN_IDS))
async def set_channels(message: Message):
    args = message.text.split()[1:]
    if not args:
        await message.answer("⚠️ Укажи каналы через пробел.\nПример: `/set_channels @channel1 -10012345678`", parse_mode="Markdown")
        return
    
    bot_data["required_channels"] = args
    bot_data["participants"] = set() # Сбрасываем участников при новом конкурсе
    await message.answer(f"✅ **Список каналов обновлен:**\n" + "\n".join(args) + "\n\nУчастники сброшены.", parse_mode="Markdown")

# Команда /draw - выбрать победителя
@dp.message(Command("draw"), F.from_user.id.in_(ADMIN_IDS))
async def draw_winner(message: Message):
    participants = list(bot_data["participants"])
    if not participants:
        await message.answer("🤷‍♂️ Участников пока нет.")
        return

    winner_id = random.choice(participants)
    
    # Пробуем получить инфо о победителе
    try:
        user_chat = await bot.get_chat(winner_id)
        mention = user_chat.username if user_chat.username else user_chat.first_name
        winner_text = f"@{mention}" if user_chat.username else f"[{mention}](tg://user?id={winner_id})"
    except:
        winner_text = f"ID {winner_id}"

    await message.answer(f"🏆 **Победитель выбран!**\nПоздравляем: {winner_text}", parse_mode="Markdown")

# --- 6. Логика пользователя (Конкурс) ---

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\nВыбирай действие ниже:",
        reply_markup=get_main_keyboard()
    )

@dp.callback_query(F.data == "participate")
async def register_participant(callback: CallbackQuery):
    user_id = callback.from_user.id
    channels = bot_data["required_channels"]
    
    if not channels:
        await callback.answer("Конкурс пока не активен.", show_alert=True)
        return

    # Проверка подписок
    not_subscribed = []
    for channel in channels:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status not in ["member", "administrator", "creator"]:
                not_subscribed.append(channel)
        except Exception:
            # Если бот не админ в канале, он может не увидеть статус
            not_subscribed.append(channel + " (Бот не админ или ошибка)")

    if not_subscribed:
        text = "❌ Ты подписан не на все каналы:\n" + "\n".join(not_subscribed)
        await callback.answer(text, show_alert=True)
    else:
        bot_data["participants"].add(user_id)
        await callback.answer("✅ Ты участвуешь! Жди итогов.", show_alert=True)

# --- 7. Логика PR-менеджера (Анкета) ---

@dp.callback_query(F.data == "apply_pr")
async def start_pr_application(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("📝 **Заявка на PR-менеджера**\n\n1. Сколько вам лет?")
    await state.set_state(PRApplication.age)
    await callback.answer()

@dp.message(PRApplication.age)
async def process_age(message: Message, state: FSMContext):
    await state.update_data(age=message.text)
    await message.answer("2. Ваш ник (как к вам обращаться)?")
    await state.set_state(PRApplication.nickname)

@dp.message(PRApplication.nickname)
async def process_nick(message: Message, state: FSMContext):
    await state.update_data(nickname=message.text)
    await message.answer("3. В сколько чатов раскидываете?")
    await state.set_state(PRApplication.chats_count)

@dp.message(PRApplication.chats_count)
async def process_count(message: Message, state: FSMContext):
    await state.update_data(chats_count=message.text)
    await message.answer("4. Пришлите доказательства (скриншот статистики или работы). Отправьте **картинку**.")
    await state.set_state(PRApplication.proofs)

@dp.message(PRApplication.proofs, F.photo)
async def process_proofs(message: Message, state: FSMContext):
    data = await state.get_data()
    photo_id = message.photo[-1].file_id # Берем самое лучшее качество
    user = message.from_user
    
    # Формируем текст заявки
    caption = (
        f"🔥 **Новая заявка на PR**\n\n"
        f"👤 **Юзер:** @{user.username} (ID: {user.id})\n"
        f"🎂 **Возраст:** {data['age']}\n"
        f"🏷 **Ник:** {data['nickname']}\n"
        f"🚀 **Чатов:** {data['chats_count']}"
    )

    # Отправляем в чат заявок
    try:
        await bot.send_photo(chat_id=PR_CHAT_ID, photo=photo_id, caption=caption, parse_mode="Markdown")
        await message.answer("✅ Заявка успешно отправлена! Администратор свяжется с вами.")
    except Exception as e:
        await message.answer("Ошибка при отправке. Проверьте, что бот есть в чате заявок.")
        logging.error(e)
    
    await state.clear()

@dp.message(PRApplication.proofs)
async def process_proofs_invalid(message: Message):
    await message.answer("📸 Пожалуйста, отправьте именно скриншот (картинку).")

# --- 8. Запуск ---
async def main():
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен")
