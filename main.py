import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import random
from collections import defaultdict

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode

# Конфигурация
BOT_TOKEN = "8575617408:AAEw8ZIi2_dAlRwbfDCc-OC0lpPvXkRNSgc"
ADMIN_IDS = [5883703466]  # ID администраторов
PR_CHAT_ID = -1003411409227  # ID чата для заявок

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Хранилище данных (в реальном проекте используйте БД)
class Storage:
    def __init__(self):
        self.giveaways = {}
        self.participants = defaultdict(set)
        self.subscription_channels = {}
        self.user_subscriptions = defaultdict(set)

storage_data = Storage()

# Состояния для FSM
class GiveawayStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_channels = State()
    waiting_for_winners_count = State()

class PRManagerStates(StatesGroup):
    waiting_for_age = State()
    waiting_for_nickname = State()
    waiting_for_chats_count = State()
    waiting_for_proof = State()

# Команда /start
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎁 Участвовать в конкурсе", callback_data="participate"),
                InlineKeyboardButton(text="📊 Проверить подписки", callback_data="check_subs")
            ],
            [
                InlineKeyboardButton(text="📝 Заявка на пиар-менеджера", callback_data="pr_application"),
                InlineKeyboardButton(text="👑 Админ панель", callback_data="admin_panel")
            ]
        ]
    )
    
    await message.answer(
        "🎉 Добро пожаловать в бот-рандомайзер для конкурсов!\n\n"
        "Выберите действие:",
        reply_markup=keyboard
    )

# Панель администратора
@dp.callback_query(lambda c: c.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("У вас нет прав администратора!", show_alert=True)
        return
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Создать конкурс", callback_data="create_giveaway"),
                InlineKeyboardButton(text="🎲 Выбрать победителей", callback_data="select_winners")
            ],
            [
                InlineKeyboardButton(text="📋 Список конкурсов", callback_data="list_giveaways"),
                InlineKeyboardButton(text="👥 Участники конкурса", callback_data="view_participants")
            ],
            [
                InlineKeyboardButton(text="🔧 Настроить каналы", callback_data="setup_channels"),
                InlineKeyboardButton(text="📨 Заявки на пиар", callback_data="view_pr_applications")
            ],
            [
                InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")
            ]
        ]
    )
    
    await callback.message.edit_text(
        "👑 Панель администратора\n\n"
        "Выберите действие:",
        reply_markup=keyboard
    )

# Создание конкурса
@dp.callback_query(lambda c: c.data == "create_giveaway")
async def create_giveaway(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("У вас нет прав!", show_alert=True)
        return
    
    await state.set_state(GiveawayStates.waiting_for_name)
    await callback.message.edit_text(
        "Введите название конкурса:"
    )

@dp.message(GiveawayStates.waiting_for_name)
async def process_giveaway_name(message: types.Message, state: FSMContext):
    await state.update_data(giveaway_name=message.text)
    await state.set_state(GiveawayStates.waiting_for_channels)
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Указать каналы", callback_data="specify_channels"),
                InlineKeyboardButton(text="❌ Без каналов", callback_data="no_channels")
            ]
        ]
    )
    
    await message.answer(
        "Нужно ли участникам подписываться на каналы?",
        reply_markup=keyboard
    )

@dp.callback_query(GiveawayStates.waiting_for_channels)
async def process_channels_choice(callback: CallbackQuery, state: FSMContext):
    if callback.data == "no_channels":
        await state.update_data(channels=[])
        await state.set_state(GiveawayStates.waiting_for_winners_count)
        await callback.message.edit_text("Введите количество победителей:")
    elif callback.data == "specify_channels":
        await callback.message.edit_text(
            "Введите ID каналов через запятую (например: -1001234567890, -1009876543210):\n\n"
            "Как получить ID канала:\n"
            "1. Добавьте бота в канал\n"
            "2. Перешлите любое сообщение из канала боту @username_to_id_bot\n"
            "3. Скопируйте полученный ID"
        )

@dp.message(GiveawayStates.waiting_for_channels)
async def process_channels_input(message: types.Message, state: FSMContext):
    try:
        channels = [int(ch.strip()) for ch in message.text.split(',')]
        await state.update_data(channels=channels)
        await state.set_state(GiveawayStates.waiting_for_winners_count)
        await message.answer("Введите количество победителей:")
    except ValueError:
        await message.answer("Неверный формат ID каналов. Попробуйте снова:")

@dp.message(GiveawayStates.waiting_for_winners_count)
async def process_winners_count(message: types.Message, state: FSMContext):
    try:
        winners_count = int(message.text)
        data = await state.get_data()
        
        giveaway_id = len(storage_data.giveaways) + 1
        storage_data.giveaways[giveaway_id] = {
            'name': data['giveaway_name'],
            'channels': data.get('channels', []),
            'winners_count': winners_count,
            'created_at': datetime.now(),
            'admin_id': message.from_user.id
        }
        
        storage_data.subscription_channels[giveaway_id] = data.get('channels', [])
        
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔗 Ссылка для участия", 
                                       url=f"https://t.me/{callback.bot.username}?start=giveaway_{giveaway_id}")
                ],
                [
                    InlineKeyboardButton(text="◀️ В админ панель", callback_data="admin_panel")
                ]
            ]
        )
        
        await message.answer(
            f"🎉 Конкурс создан!\n\n"
            f"Название: {data['giveaway_name']}\n"
            f"Каналы для подписки: {len(data.get('channels', []))}\n"
            f"Количество победителей: {winners_count}\n"
            f"ID конкурса: {giveaway_id}",
            reply_markup=keyboard
        )
        await state.clear()
    except ValueError:
        await message.answer("Введите число!")

# Участие в конкурсе
@dp.callback_query(lambda c: c.data == "participate")
async def participate(callback: CallbackQuery):
    if not storage_data.giveaways:
        await callback.answer("Нет активных конкурсов!", show_alert=True)
        return
    
    keyboard = InlineKeyboardBuilder()
    for giveaway_id, giveaway in storage_data.giveaways.items():
        keyboard.button(
            text=f"🎁 {giveaway['name']}",
            callback_data=f"join_{giveaway_id}"
        )
    keyboard.adjust(1)
    
    await callback.message.edit_text(
        "Выберите конкурс для участия:",
        reply_markup=keyboard.as_markup()
    )

@dp.callback_query(lambda c: c.data.startswith("join_"))
async def join_giveaway(callback: CallbackQuery):
    giveaway_id = int(callback.data.split("_")[1])
    giveaway = storage_data.giveaways.get(giveaway_id)
    
    if not giveaway:
        await callback.answer("Конкурс не найден!", show_alert=True)
        return
    
    user_id = callback.from_user.id
    channels = giveaway['channels']
    
    # Проверка подписки на каналы
    if channels:
        not_subscribed = []
        for channel_id in channels:
            try:
                chat_member = await bot.get_chat_member(channel_id, user_id)
                if chat_member.status in ['left', 'kicked']:
                    not_subscribed.append(channel_id)
            except:
                not_subscribed.append(channel_id)
        
        if not_subscribed:
            keyboard = InlineKeyboardBuilder()
            for channel_id in not_subscribed:
                try:
                    chat = await bot.get_chat(channel_id)
                    keyboard.button(
                        text=f"📢 {chat.title}",
                        url=f"https://t.me/{chat.username}" if chat.username else f"https://t.me/c/{str(channel_id)[4:]}"
                    )
                except:
                    continue
            
            keyboard.button(
                text="✅ Я подписался",
                callback_data=f"check_again_{giveaway_id}"
            )
            keyboard.adjust(1)
            
            await callback.message.edit_text(
                "Для участия в конкурсе необходимо подписаться на каналы:",
                reply_markup=keyboard.as_markup()
            )
            return
    
    # Добавление участника
    storage_data.participants[giveaway_id].add(user_id)
    await callback.answer(f"Вы успешно зарегистрированы в конкурсе '{giveaway['name']}'!", show_alert=True)
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="◀️ На главную", callback_data="back_to_main")
            ]
        ]
    )
    
    await callback.message.edit_text(
        f"✅ Вы успешно зарегистрированы в конкурсе '{giveaway['name']}'!\n\n"
        f"Количество участников: {len(storage_data.participants[giveaway_id])}\n"
        f"Будет выбрано победителей: {giveaway['winners_count']}",
        reply_markup=keyboard
    )

# Повторная проверка подписки
@dp.callback_query(lambda c: c.data.startswith("check_again_"))
async def check_subscriptions_again(callback: CallbackQuery):
    giveaway_id = int(callback.data.split("_")[2])
    await join_giveaway(callback)

# Выбор победителей
@dp.callback_query(lambda c: c.data == "select_winners")
async def select_winners_menu(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("У вас нет прав!", show_alert=True)
        return
    
    keyboard = InlineKeyboardBuilder()
    for giveaway_id, giveaway in storage_data.giveaways.items():
        participants_count = len(storage_data.participants.get(giveaway_id, []))
        keyboard.button(
            text=f"{giveaway['name']} ({participants_count} участ.)",
            callback_data=f"draw_{giveaway_id}"
        )
    keyboard.adjust(1)
    
    await callback.message.edit_text(
        "Выберите конкурс для выбора победителей:",
        reply_markup=keyboard.as_markup()
    )

@dp.callback_query(lambda c: c.data.startswith("draw_"))
async def draw_winners(callback: CallbackQuery):
    giveaway_id = int(callback.data.split("_")[1])
    giveaway = storage_data.giveaways.get(giveaway_id)
    
    if not giveaway:
        await callback.answer("Конкурс не найден!", show_alert=True)
        return
    
    participants = list(storage_data.participants.get(giveaway_id, []))
    
    if len(participants) < giveaway['winners_count']:
        await callback.answer(f"Недостаточно участников! Только {len(participants)} из {giveaway['winners_count']}", show_alert=True)
        return
    
    winners = random.sample(participants, giveaway['winners_count'])
    
    # Формирование списка победителей
    winners_text = "🎉 Победители конкурса!\n\n"
    for i, winner_id in enumerate(winners, 1):
        try:
            user = await bot.get_chat(winner_id)
            winners_text += f"{i}. @{user.username or 'без username'} (ID: {winner_id})\n"
        except:
            winners_text += f"{i}. ID: {winner_id}\n"
    
    winners_text += f"\nКонкурс: {giveaway['name']}"
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📢 Опубликовать результаты", 
                                   callback_data=f"publish_results_{giveaway_id}"),
            ],
            [
                InlineKeyboardButton(text="◀️ В админ панель", callback_data="admin_panel")
            ]
        ]
    )
    
    await callback.message.edit_text(
        winners_text,
        reply_markup=keyboard
    )

# Заявка на пиар-менеджера
@dp.callback_query(lambda c: c.data == "pr_application")
async def pr_application_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PRManagerStates.waiting_for_age)
    await callback.message.edit_text(
        "📝 Заявка на позицию пиар-менеджера\n\n"
        "Введите ваш возраст:"
    )

@dp.message(PRManagerStates.waiting_for_age)
async def process_pr_age(message: types.Message, state: FSMContext):
    await state.update_data(age=message.text)
    await state.set_state(PRManagerStates.waiting_for_nickname)
    await message.answer("Введите ваш никнейм (без @):")

@dp.message(PRManagerStates.waiting_for_nickname)
async def process_pr_nickname(message: types.Message, state: FSMContext):
    await state.update_data(nickname=message.text)
    await state.set_state(PRManagerStates.waiting_for_chats_count)
    await message.answer("В скольки чатах вы можете рекламировать (количество):")

@dp.message(PRManagerStates.waiting_for_chats_count)
async def process_pr_chats(message: types.Message, state: FSMContext):
    await state.update_data(chats_count=message.text)
    await state.set_state(PRManagerStates.waiting_for_proof)
    await message.answer(
        "Отправьте доказательства вашей работы:\n"
        "• Ссылки на отзывы\n"
        "• Скриншоты\n"
        "• Примеры работы\n\n"
        "Можете отправить текст, фото или документы"
    )

@dp.message(PRManagerStates.waiting_for_proof)
async def process_pr_proof(message: types.Message, state: FSMContext):
    data = await state.get_data()
    
    # Формируем заявку
    application_text = (
        "📨 Новая заявка на пиар-менеджера!\n\n"
        f"👤 Пользователь: {message.from_user.full_name}\n"
        f"🆔 ID: {message.from_user.id}\n"
        f"📅 Возраст: {data['age']}\n"
        f"🏷️ Никнейм: @{data['nickname']}\n"
        f"📊 Чатов для рекламы: {data['chats_count']}\n"
        f"📎 Доказательства:"
    )
    
    # Отправляем заявку в PR чат
    try:
        if message.text:
            await bot.send_message(PR_CHAT_ID, f"{application_text}\n{message.text}")
        elif message.photo:
            await bot.send_photo(PR_CHAT_ID, message.photo[-1].file_id, caption=application_text)
        elif message.document:
            await bot.send_document(PR_CHAT_ID, message.document.file_id, caption=application_text)
    except Exception as e:
        logger.error(f"Error sending PR application: {e}")
    
    # Создаем инлайн-кнопку для перехода в чат
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💬 Перейти в чат с заявками", 
                                   url=f"https://t.me/c/{str(PR_CHAT_ID)[4:]}")
            ],
            [
                InlineKeyboardButton(text="◀️ На главную", callback_data="back_to_main")
            ]
        ]
    )
    
    await message.answer(
        "✅ Ваша заявка отправлена!\n\n"
        "Администратор свяжется с вами в чате с заявками.",
        reply_markup=keyboard
    )
    await state.clear()

# Настройка каналов для подписки
@dp.callback_query(lambda c: c.data == "setup_channels")
async def setup_channels(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("У вас нет прав!", show_alert=True)
        return
    
    keyboard = InlineKeyboardBuilder()
    for giveaway_id, giveaway in storage_data.giveaways.items():
        channels_count = len(giveaway.get('channels', []))
        keyboard.button(
            text=f"{giveaway['name']} ({channels_count} каналов)",
            callback_data=f"edit_channels_{giveaway_id}"
        )
    keyboard.adjust(1)
    
    await callback.message.edit_text(
        "Выберите конкурс для настройки каналов:",
        reply_markup=keyboard.as_markup()
    )

# Обработка остальных callback-ов
@dp.callback_query(lambda c: c.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    await cmd_start(callback.message)

@dp.callback_query(lambda c: c.data == "check_subs")
async def check_subs(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")
            ]
        ]
    )
    
    await callback.message.edit_text(
        "Эта функция позволяет проверить подписки на все активные каналы.\n"
        "Для проверки нажмите на кнопку 'Участвовать в конкурсе' и выберите нужный конкурс.",
        reply_markup=keyboard
    )

# Запуск бота
async def main():
    logger.info("Starting bot...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
