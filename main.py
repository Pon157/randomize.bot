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
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

# --- 1. НАСТРОЙКИ ---
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
PR_CHAT_ID = int(os.getenv("PR_CHAT_ID"))
LOT_CHANNEL = os.getenv("LOT_CHANNEL", "@lotsvitechek")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- 2. БАЗА ДАННЫХ ---
def init_db():
    with sqlite3.connect("bot_database.db") as conn:
        cur = conn.cursor()
        # Таблица лотерей
        cur.execute("""CREATE TABLE IF NOT EXISTS lotteries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT,
            entities TEXT,
            channels TEXT,
            finish_type TEXT,
            finish_value TEXT,
            status TEXT DEFAULT 'active',
            message_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            winner_id INTEGER,
            participants_count INTEGER DEFAULT 0
        )""")
        # Таблица участников
        cur.execute("""CREATE TABLE IF NOT EXISTS participants (
            user_id INTEGER,
            lot_id INTEGER,
            username TEXT,
            full_name TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, lot_id)
        )""")
        # Таблица пользователей
        cur.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            is_admin BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.commit()

def db_query(query, params=(), fetchone=False, fetchall=False, commit=False):
    with sqlite3.connect("bot_database.db") as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, params)
        if commit:
            conn.commit()
        if fetchone:
            return cur.fetchone()
        if fetchall:
            return cur.fetchall()
        return cur.lastrowid

# --- 3. СОСТОЯНИЯ ---
class CreateLot(StatesGroup):
    text = State()
    channels = State()
    finish_type = State()
    value = State()
    participants_count = State()

class PRApplication(StatesGroup):
    age = State()
    nickname = State()
    proofs = State()

class AdminStates(StatesGroup):
    manual_count = State()
    edit_lottery = State()
    broadcast = State()

# --- 4. УТИЛИТЫ ---
async def check_user_subscription(user_id: int, channels: list) -> tuple:
    """Проверяет подписку пользователя на каналы"""
    not_subscribed = []
    for channel in channels:
        if channel.strip():
            try:
                member = await bot.get_chat_member(channel.strip(), user_id)
                if member.status in ["left", "kicked"]:
                    not_subscribed.append(channel.strip())
            except Exception as e:
                logging.error(f"Error checking subscription to {channel}: {e}")
                not_subscribed.append(channel.strip())
    return len(not_subscribed) == 0, not_subscribed

def format_time_remaining(finish_time: str) -> str:
    """Форматирует оставшееся время"""
    try:
        end_time = datetime.strptime(finish_time, "%d.%m.%Y %H:%M")
        now = datetime.now()
        if now >= end_time:
            return "Завершено"
        
        delta = end_time - now
        if delta.days > 0:
            return f"{delta.days} дн. {delta.seconds // 3600} ч."
        elif delta.seconds >= 3600:
            return f"{delta.seconds // 3600} ч. {(delta.seconds % 3600) // 60} мин."
        else:
            return f"{delta.seconds // 60} мин."
    except:
        return "Неизвестно"

# --- 5. ЛОГИКА ЗАВЕРШЕНИЯ ---
async def finish_giveaway(lot_id: int, manual: bool = False):
    """Завершает лотерею и выбирает победителя"""
    lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or lot['status'] == 'closed':
        return
    
    # Получаем всех участников
    participants = db_query("SELECT user_id, username, full_name FROM participants WHERE lot_id = ?", (lot_id,), fetchall=True)
    
    if not participants:
        message_text = f"🔔 Лотерея #{lot_id} окончена. Участников нет."
        if manual:
            message_text += "\n(Завершено вручную)"
        await bot.send_message(LOT_CHANNEL, message_text, reply_to_message_id=lot['message_id'])
        db_query("UPDATE lotteries SET status = 'closed' WHERE id = ?", (lot_id,), commit=True)
        return
    
    # Выбираем победителя
    winner = random.choice(participants)
    
    # Обновляем данные в базе
    db_query("""
        UPDATE lotteries 
        SET status = 'closed', winner_id = ?, participants_count = ?
        WHERE id = ?
    """, (winner['user_id'], len(participants), lot_id), commit=True)
    
    # Формируем сообщение
    mention = f"@{winner['username']}" if winner['username'] else f"[{winner['full_name']}](tg://user?id={winner['user_id']})"
    
    participants_list = "\n".join([f"• @{p['username']}" if p['username'] else f"• {p['full_name']}" for p in participants[:50]])
    if len(participants) > 50:
        participants_list += f"\n... и еще {len(participants) - 50} участников"
    
    message_text = f"""
🎊 **Итоги розыгрыша #{lot_id}!**

🏆 **Победитель:** {mention}

📊 **Статистика:**
• Участников: {len(participants)}
• Завершено: {datetime.now().strftime('%d.%m.%Y %H:%M')}
{'(Завершено вручную)' if manual else ''}

👥 **Список участников:**
{participants_list}
"""
    
    await bot.send_message(
        LOT_CHANNEL, 
        message_text, 
        parse_mode="Markdown", 
        reply_to_message_id=lot['message_id']
    )
    
    # Уведомляем победителя
    try:
        await bot.send_message(
            winner['user_id'],
            f"🎉 Поздравляем! Вы победили в лотерее #{lot_id}!\n\n"
            f"Ссылка на пост: https://t.me/{LOT_CHANNEL.lstrip('@')}/{lot['message_id']}"
        )
    except Exception as e:
        logging.error(f"Failed to notify winner {winner['user_id']}: {e}")

# --- 6. ОБРАБОТЧИК START ---
@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username
    full_name = message.from_user.full_name
    
    # Сохраняем пользователя в базе
    db_query("""
        INSERT OR IGNORE INTO users (user_id, username, full_name) 
        VALUES (?, ?, ?)
    """, (user_id, username, full_name), commit=True)
    
    # Обработка ссылки на лотерею
    if command.args and command.args.startswith("lot_"):
        lot_id = int(command.args.split("_")[1])
        lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
        
        if not lot:
            return await message.answer("⚠️ Розыгрыш не найден!")
        
        if lot['status'] == 'closed':
            return await message.answer("⚠️ Розыгрыш уже завершен!")
        
        # Проверка времени для временных лотерей
        if lot['finish_type'] == "time":
            try:
                finish_time = datetime.strptime(lot['finish_value'], "%d.%m.%Y %H:%M")
                if datetime.now() >= finish_time:
                    await finish_giveaway(lot_id)
                    return await message.answer("⚠️ Время участия истекло!")
            except:
                pass
        
        # Проверяем, участвует ли уже пользователь
        existing = db_query(
            "SELECT 1 FROM participants WHERE user_id = ? AND lot_id = ?", 
            (user_id, lot_id), 
            fetchone=True
        )
        
        if existing:
            participants_count = db_query(
                "SELECT COUNT(*) as count FROM participants WHERE lot_id = ?", 
                (lot_id,), 
                fetchone=True
            )['count']
            
            remaining = ""
            if lot['finish_type'] == "time":
                remaining = format_time_remaining(lot['finish_value'])
            elif lot['finish_type'] == "count":
                target = int(lot['finish_value'])
                remaining = f"Осталось {target - participants_count} участников"
            
            return await message.answer(
                f"✅ Вы уже участвуете в розыгрыше #{lot_id}!\n\n"
                f"📊 Участников: {participants_count}\n"
                f"⏱ {remaining}"
            )
        
        # Проверка подписки на каналы
        channels = [ch.strip() for ch in lot['channels'].split(",") if ch.strip()] if lot['channels'] else []
        
        if channels:
            is_subscribed, not_subscribed = await check_user_subscription(user_id, channels)
            
            if not is_subscribed:
                keyboard = InlineKeyboardBuilder()
                for channel in not_subscribed:
                    keyboard.button(text=f"📢 {channel}", url=f"https://t.me/{channel.lstrip('@')}")
                keyboard.button(text="🔄 Проверить подписку", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")
                
                channels_text = "\n".join([f"• {ch}" for ch in not_subscribed])
                return await message.answer(
                    f"❌ Для участия необходимо подписаться на каналы:\n\n{channels_text}",
                    reply_markup=keyboard.as_markup()
                )
        
        # Регистрируем участника
        db_query("""
            INSERT OR IGNORE INTO participants (user_id, lot_id, username, full_name) 
            VALUES (?, ?, ?, ?)
        """, (user_id, lot_id, username, full_name), commit=True)
        
        # Обновляем счетчик участников
        db_query("""
            UPDATE lotteries 
            SET participants_count = participants_count + 1 
            WHERE id = ?
        """, (lot_id,), commit=True)
        
        # Получаем актуальное количество участников
        participants_count = db_query(
            "SELECT COUNT(*) as count FROM participants WHERE lot_id = ?", 
            (lot_id,), 
            fetchone=True
        )['count']
        
        # Проверяем условие завершения для лотерей по количеству
        if lot['finish_type'] == "count":
            if participants_count >= int(lot['finish_value']):
                await finish_giveaway(lot_id)
                return await message.answer("🎉 Розыгрыш завершен! Победитель определен!")
        
        remaining = ""
        if lot['finish_type'] == "time":
            remaining = f"⏱ Осталось: {format_time_remaining(lot['finish_value'])}"
        elif lot['finish_type'] == "count":
            target = int(lot['finish_value'])
            remaining = f"📊 Нужно еще {target - participants_count} участников"
        
        return await message.answer(
            f"🎉 Вы успешно зарегистрированы в розыгрыше #{lot_id}!\n\n"
            f"📊 Участников: {participants_count}\n"
            f"{remaining}\n\n"
            f"Ссылка для друзей: https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}"
        )
    
    # Обычный старт
    await state.clear()
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💼 PR Заявка", callback_data="apply_pr")],
        [InlineKeyboardButton(text="📢 Активные розыгрыши", callback_data="active_lotteries")],
        [InlineKeyboardButton(text="📊 Мои участия", callback_data="my_participations")]
    ])
    
    if user_id in ADMIN_IDS:
        keyboard.inline_keyboard.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_panel")])
    
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        f"Я бот для проведения розыгрышей в канале {LOT_CHANNEL}\n\n"
        f"🎁 Участвуй в розыгрышах и выигрывай призы!",
        reply_markup=keyboard
    )

# --- 7. PR АНКЕТА ---
@dp.callback_query(F.data == "apply_pr")
async def pr_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("📝 Подача PR заявки\n\nШаг 1: Укажите ваш возраст:")
    await state.set_state(PRApplication.age)
    await callback.answer()

@dp.message(PRApplication.age)
async def pr_age(message: Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("Пожалуйста, введите число (ваш возраст):")
    
    age = int(message.text)
    if age < 16 or age > 70:
        return await message.answer("Возраст должен быть от 16 до 70 лет. Введите снова:")
    
    await state.update_data(age=age)
    await message.answer("Шаг 2: Введите ваш никнейм в Telegram:")
    await state.set_state(PRApplication.nickname)

@dp.message(PRApplication.nickname)
async def pr_nick(message: Message, state: FSMContext):
    await state.update_data(nickname=message.text)
    await message.answer("Шаг 3: Пришлите скриншоты ваших предыдущих работ (фото):\n\nМожно отправить несколько фото за раз")
    await state.set_state(PRApplication.proofs)

@dp.message(PRApplication.proofs, F.photo)
async def pr_end(message: Message, state: FSMContext):
    data = await state.get_data()
    
    # Сохраняем все фото
    photo_ids = [message.photo[-1].file_id]
    
    caption = f"""
📩 НОВАЯ PR ЗАЯВКА

👤 Пользователь: @{message.from_user.username or 'нет'}
📛 Имя: {message.from_user.full_name}
🆔 ID: {message.from_user.id}
🎂 Возраст: {data.get('age')}
📱 Никнейм: {data.get('nickname')}
⏰ Время: {datetime.now().strftime('%d.%m.%Y %H:%M')}
"""
    
    # Отправляем в PR чат
    await bot.send_photo(PR_CHAT_ID, photo_ids[0], caption=caption)
    
    # Если есть еще фото, отправляем как медиагруппу
    await message.answer("✅ Заявка отправлена! Мы свяжемся с вами в ближайшее время.")
    await state.clear()

# --- 8. АДМИН-ПАНЕЛЬ ---
@dp.callback_query(F.data == "admin_panel", F.from_user.id.in_(ADMIN_IDS))
async def admin_panel(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Создать лотерею", callback_data="admin_create")],
        [InlineKeyboardButton(text="📋 Активные лотереи", callback_data="admin_list_active")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="admin_settings")]
    ])
    await callback.message.edit_text("🛠 Админ-панель", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "admin_create", F.from_user.id.in_(ADMIN_IDS))
async def cl_init(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer(
        "🎲 Создание новой лотереи\n\n"
        "1. Пришлите текст поста (можно с эмодзи, форматированием и хештегами):"
    )
    await state.set_state(CreateLot.text)
    await callback.answer()

@dp.message(CreateLot.text)
async def cl_text(message: Message, state: FSMContext):
    ents = json.dumps([e.model_dump_json() for e in message.entities]) if message.entities else "[]"
    await state.update_data(text=message.text, entities=ents)
    await message.answer(
        "2. Укажите каналы для обязательной подписки (через запятую):\n\n"
        "Например: @channel1, @channel2\n\n"
        "Или напишите 'нет', если подписка не требуется:"
    )
    await state.set_state(CreateLot.channels)

@dp.message(CreateLot.channels)
async def cl_channels(message: Message, state: FSMContext):
    channels = message.text if message.text.lower() != 'нет' else ''
    await state.update_data(channels=channels)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏰ По времени", callback_data="set_type_time")],
        [InlineKeyboardButton(text="👥 По количеству участников", callback_data="set_type_count")],
        [InlineKeyboardButton(text("⏰ + 👥 По времени И количеству", callback_data="set_type_both"))
    ])
    await message.answer("3. Выберите тип завершения лотереи:", reply_markup=keyboard)
    await state.set_state(CreateLot.finish_type)

@dp.callback_query(F.data.startswith("set_type_"), CreateLot.finish_type)
async def cl_type(callback: CallbackQuery, state: FSMContext):
    ftype = callback.data.split("_")[2]
    await state.update_data(ftype=ftype)
    
    if ftype == "time":
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="1 час", callback_data="val_1h"), 
             InlineKeyboardButton(text="3 часа", callback_data="val_3h")],
            [InlineKeyboardButton(text="6 часов", callback_data="val_6h"), 
             InlineKeyboardButton(text="12 часов", callback_data="val_12h")],
            [InlineKeyboardButton(text="1 день", callback_data="val_1d"), 
             InlineKeyboardButton(text="3 дня", callback_data="val_3d")],
            [InlineKeyboardButton(text="1 неделя", callback_data="val_7d")],
            [InlineKeyboardButton(text("⚙️ Ввести вручную", callback_data="val_custom"))
        ])
        await callback.message.edit_text("4. Выберите время до завершения:", reply_markup=keyboard)
    elif ftype == "count":
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="10 участников", callback_data="val_10")],
            [InlineKeyboardButton(text="25 участников", callback_data="val_25")],
            [InlineKeyboardButton(text="50 участников", callback_data="val_50")],
            [InlineKeyboardButton(text="100 участников", callback_data="val_100")],
            [InlineKeyboardButton(text="250 участников", callback_data="val_250")],
            [InlineKeyboardButton(text="500 участников", callback_data="val_500")],
            [InlineKeyboardButton(text("⚙️ Ввести вручную", callback_data="val_custom"))
        ])
        await callback.message.edit_text("4. Выберите количество участников для завершения:", reply_markup=keyboard)
    elif ftype == "both":
        await callback.message.edit_text(
            "4. Введите количество участников для завершения (число):"
        )
        await state.set_state(CreateLot.participants_count)
        return
    
    await state.set_state(CreateLot.value)
    await callback.answer()

@dp.message(CreateLot.participants_count)
async def cl_participants_count(message: Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("Пожалуйста, введите число:")
    
    count = int(message.text)
    if count < 2:
        return await message.answer("Количество участников должно быть не менее 2. Введите снова:")
    
    await state.update_data(participants_count=count)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 час", callback_data="val_1h"), 
         InlineKeyboardButton(text="3 часа", callback_data="val_3h")],
        [InlineKeyboardButton(text="6 часов", callback_data="val_6h"), 
         InlineKeyboardButton(text="12 часов", callback_data="val_12h")],
        [InlineKeyboardButton(text="1 день", callback_data="val_1d"), 
         InlineKeyboardButton(text="3 дня", callback_data="val_3d")],
        [InlineKeyboardButton(text="1 неделя", callback_data="val_7d")],
        [InlineKeyboardButton(text("⚙️ Ввести вручную", callback_data="val_custom"))
    ])
    await message.answer("5. Выберите максимальное время лотереи:", reply_markup=keyboard)
    await state.set_state(CreateLot.value)

@dp.callback_query(F.data.startswith("val_"), CreateLot.value)
async def cl_final(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    val_raw = callback.data.split("_")[1]
    
    if val_raw == "custom":
        if data['ftype'] == "time":
            await callback.message.edit_text("Введите время в формате ЧАСЫ:МИНУТЫ (например, 2:30 для 2 часов 30 минут):")
            await state.set_state(AdminStates.manual_count)
        else:
            await callback.message.edit_text("Введите количество участников (число):")
            await state.set_state(AdminStates.manual_count)
        await callback.answer()
        return
    
    # Расчет значения
    finish_val = ""
    if data['ftype'] in ["time", "both"]:
        now = datetime.now()
        time_mapping = {
            "1min": timedelta(minutes=1),
            "5min": timedelta(minutes=5),
            "10min": timedelta(minutes=10),
            "15min": timedelta(minutes=15),
            "30min": timedelta(minutes=30),
            "45min": timedelta(minutes=45),
            "1h": timedelta(hours=1),
            "3h": timedelta(hours=3),
            "6h": timedelta(hours=6),
            "12h": timedelta(hours=12),
            "1d": timedelta(days=1),
            "3d": timedelta(days=3),
            "7d": timedelta(days=7)
        }
        
        if val_raw in time_mapping:
            finish_val = (now + time_mapping[val_raw]).strftime("%d.%m.%Y %H:%M")
    
    elif data['ftype'] == "count":
        finish_val = val_raw
    
    # Для комбинированного типа сохраняем оба значения
    if data['ftype'] == "both":
        finish_val = f"{data.get('participants_count')}|{finish_val}"
    
    # Создаем лотерею в базе данных
    lot_id = db_query(
        """INSERT INTO lotteries (text, entities, channels, finish_type, finish_value) 
           VALUES (?, ?, ?, ?, ?)""",
        (data['text'], data['entities'], data['channels'], data['ftype'], finish_val),
        commit=True
    )
    
    # Создаем кнопку для участия
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Участвовать!", 
            url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}"
        )
    ]])
    
    # Восстанавливаем форматирование
    ents_data = json.loads(data['entities'])
    entities = [types.MessageEntity(**json.loads(e)) for e in ents_data] if ents_data else None
    
    # Отправляем пост в канал
    sent = await bot.send_message(
        LOT_CHANNEL,
        text=data['text'],
        entities=entities,
        reply_markup=keyboard
    )
    
    # Сохраняем ID сообщения
    db_query(
        "UPDATE lotteries SET message_id = ? WHERE id = ?",
        (sent.message_id, lot_id),
        commit=True
    )
    
    # Отправляем статистику админу
    condition_text = ""
    if data['ftype'] == "time":
        condition_text = f"⏰ Завершится: {finish_val}"
    elif data['ftype'] == "count":
        condition_text = f"👥 Завершится при: {finish_val} участниках"
    elif data['ftype'] == "both":
        parts = finish_val.split("|")
        condition_text = f"👥 Завершится при {parts[0]} участниках или {parts[1]}"
    
    await callback.message.edit_text(
        f"✅ Лотерея #{lot_id} успешно создана!\n\n"
        f"📝 Тип: {data['ftype']}\n"
        f"{condition_text}\n"
        f"📢 Каналы: {data['channels'] if data['channels'] else 'не требуются'}\n\n"
        f"Ссылка на пост: https://t.me/{LOT_CHANNEL.lstrip('@')}/{sent.message_id}"
    )
    
    await state.clear()
    await callback.answer()

@dp.message(AdminStates.manual_count)
async def manual_count_input(message: Message, state: FSMContext):
    data = await state.get_data()
    user_input = message.text.strip()
    
    if data['ftype'] == "time":
        # Парсим время в формате ЧАСЫ:МИНУТЫ
        try:
            if ":" in user_input:
                hours, minutes = map(int, user_input.split(":"))
                delta = timedelta(hours=hours, minutes=minutes)
            else:
                delta = timedelta(hours=int(user_input))
            
            finish_val = (datetime.now() + delta).strftime("%d.%m.%Y %H:%M")
            await state.update_data(finish_val=finish_val)
            
            # Продолжаем создание лотереи
            await cl_final_processing(message, state, finish_val)
        except:
            await message.answer("Неверный формат времени. Введите в формате ЧАСЫ:МИНУТЫ (например, 2:30):")
    else:
        if user_input.isdigit() and int(user_input) >= 2:
            finish_val = user_input
            await state.update_data(finish_val=finish_val)
            await cl_final_processing(message, state, finish_val)
        else:
            await message.answer("Пожалуйста, введите число (не менее 2):")

async def cl_final_processing(message: Message, state: FSMContext, finish_val: str):
    """Обрабатывает финальное создание лотереи"""
    data = await state.get_data()
    
    # Создаем лотерею в базе данных
    lot_id = db_query(
        """INSERT INTO lotteries (text, entities, channels, finish_type, finish_value) 
           VALUES (?, ?, ?, ?, ?)""",
        (data['text'], data['entities'], data['channels'], data['ftype'], finish_val),
        commit=True
    )
    
    # Создаем кнопку для участия
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Участвовать!", 
            url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}"
        )
    ]])
    
    # Восстанавливаем форматирование
    ents_data = json.loads(data['entities'])
    entities = [types.MessageEntity(**json.loads(e)) for e in ents_data] if ents_data else None
    
    # Отправляем пост в канал
    sent = await bot.send_message(
        LOT_CHANNEL,
        text=data['text'],
        entities=entities,
        reply_markup=keyboard
    )
    
    # Сохраняем ID сообщения
    db_query(
        "UPDATE lotteries SET message_id = ? WHERE id = ?",
        (sent.message_id, lot_id),
        commit=True
    )
    
    # Отправляем подтверждение админу
    condition_text = ""
    if data['ftype'] == "time":
        condition_text = f"⏰ Завершится: {finish_val}"
    elif data['ftype'] == "count":
        condition_text = f"👥 Завершится при: {finish_val} участниках"
    
    await message.answer(
        f"✅ Лотерея #{lot_id} успешно создана!\n\n"
        f"📝 Тип: {data['ftype']}\n"
        f"{condition_text}\n"
        f"📢 Каналы: {data['channels'] if data['channels'] else 'не требуются'}\n\n"
        f"Ссылка на пост: https://t.me/{LOT_CHANNEL.lstrip('@')}/{sent.message_id}"
    )
    
    await state.clear()

# --- 9. УПРАВЛЕНИЕ ЛОТЕРЕЯМИ ---
@dp.callback_query(F.data == "admin_list_active", F.from_user.id.in_(ADMIN_IDS))
async def admin_list_active(callback: CallbackQuery):
    lots = db_query("SELECT * FROM lotteries WHERE status = 'active' ORDER BY id DESC", fetchall=True)
    
    if not lots:
        return await callback.answer("Нет активных лотерей", show_alert=True)
    
    text = "📋 Активные лотереи:\n\n"
    keyboard = InlineKeyboardBuilder()
    
    for lot in lots:
        participants = db_query(
            "SELECT COUNT(*) as count FROM participants WHERE lot_id = ?", 
            (lot['id'],), 
            fetchone=True
        )['count']
        
        remaining = ""
        if lot['finish_type'] == "time":
            remaining = f"⏱ {format_time_remaining(lot['finish_value'])}"
        elif lot['finish_type'] == "count":
            remaining = f"👥 {participants}/{lot['finish_value']}"
        
        text += f"#{lot['id']} - {remaining}\n"
        keyboard.button(text=f"#{lot['id']} ({participants})", callback_data=f"lot_detail_{lot['id']}")
    
    keyboard.adjust(3)
    keyboard.row(InlineKeyboardButton(text="📊 Общая статистика", callback_data="admin_stats"))
    
    await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("lot_detail_"), F.from_user.id.in_(ADMIN_IDS))
async def lot_detail(callback: CallbackQuery):
    lot_id = int(callback.data.split("_")[2])
    lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    
    if not lot:
        return await callback.answer("Лотерея не найдена", show_alert=True)
    
    participants = db_query(
        "SELECT COUNT(*) as count FROM participants WHERE lot_id = ?", 
        (lot_id,), 
        fetchone=True
    )['count']
    
    condition = ""
    if lot['finish_type'] == "time":
        condition = f"⏰ Завершится: {lot['finish_value']}"
    elif lot['finish_type'] == "count":
        condition = f"👥 Завершится при: {lot['finish_value']} участников"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏹ Завершить досрочно", callback_data=f"stop_{lot_id}")],
        [InlineKeyboardButton(text="👥 Список участников", callback_data=f"list_participants_{lot_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_list_active")]
    ])
    
    await callback.message.edit_text(
        f"📊 Детали лотереи #{lot_id}\n\n"
        f"📝 Тип: {lot['finish_type']}\n"
        f"{condition}\n"
        f"👥 Участников: {participants}\n"
        f"📢 Каналы: {lot['channels'] if lot['channels'] else 'не требуются'}\n"
        f"📅 Создана: {lot['created_at']}",
        reply_markup=keyboard
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("stop_"), F.from_user.id.in_(ADMIN_IDS))
async def manual_stop(callback: CallbackQuery):
    lot_id = int(callback.data.split("_")[1])
    await finish_giveaway(lot_id, manual=True)
    await callback.answer("✅ Лотерея завершена!")
    await admin_list_active(callback)

@dp.callback_query(F.data.startswith("list_participants_"), F.from_user.id.in_(ADMIN_IDS))
async def list_participants(callback: CallbackQuery):
    lot_id = int(callback.data.split("_")[2])
    participants = db_query(
        "SELECT username, full_name, joined_at FROM participants WHERE lot_id = ? ORDER BY joined_at",
        (lot_id,),
        fetchall=True
    )
    
    if not participants:
        return await callback.answer("Нет участников", show_alert=True)
    
    text = f"👥 Участники лотереи #{lot_id}:\n\n"
    for i, p in enumerate(participants[:50], 1):
        name = f"@{p['username']}" if p['username'] else p['full_name']
        text += f"{i}. {name}\n"
    
    if len(participants) > 50:
        text += f"\n... и еще {len(participants) - 50} участников"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"lot_detail_{lot_id}")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

# --- 10. СТАТИСТИКА ---
@dp.callback_query(F.data == "admin_stats", F.from_user.id.in_(ADMIN_IDS))
async def admin_stats(callback: CallbackQuery):
    # Общая статистика
    total_lots = db_query("SELECT COUNT(*) as count FROM lotteries", fetchone=True)['count']
    active_lots = db_query("SELECT COUNT(*) as count FROM lotteries WHERE status = 'active'", fetchone=True)['count']
    closed_lots = db_query("SELECT COUNT(*) as count FROM lotteries WHERE status = 'closed'", fetchone=True)['count']
    total_participants = db_query("SELECT COUNT(*) as count FROM participants", fetchone=True)['count']
    total_users = db_query("SELECT COUNT(*) as count FROM users", fetchone=True)['count']
    
    # Статистика по последним 7 дням
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    lots_week = db_query(
        "SELECT COUNT(*) as count FROM lotteries WHERE created_at >= ?",
        (week_ago,),
        fetchone=True
    )['count']
    
    participants_week = db_query(
        "SELECT COUNT(*) as count FROM participants WHERE joined_at >= ?",
        (week_ago,),
        fetchone=True
    )['count']
    
    text = f"""
📊 **Статистика бота:**

🎲 **Лотереи:**
• Всего создано: {total_lots}
• Активные: {active_lots}
• Завершенные: {closed_lots}
• За последние 7 дней: {lots_week}

👥 **Участники:**
• Всего записей: {total_participants}
• Уникальных пользователей: {total_users}
• За последние 7 дней: {participants_week}

📈 **Активность:**
• Канал: {LOT_CHANNEL}
• Админов: {len(ADMIN_IDS)}
"""
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список лотерей", callback_data="admin_list_active")],
        [InlineKeyboardButton(text="📊 Подробная статистика", callback_data="detailed_stats")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

# --- 11. ПОЛЬЗОВАТЕЛЬСКИЕ ФУНКЦИИ ---
@dp.callback_query(F.data == "active_lotteries")
async def user_active_lotteries(callback: CallbackQuery):
    lots = db_query(
        "SELECT id, text, finish_type, finish_value FROM lotteries WHERE status = 'active' ORDER BY id DESC LIMIT 10",
        fetchall=True
    )
    
    if not lots:
        return await callback.answer("Сейчас нет активных розыгрышей", show_alert=True)
    
    text = "🎲 Активные розыгрыши:\n\n"
    for lot in lots:
        participants = db_query(
            "SELECT COUNT(*) as count FROM participants WHERE lot_id = ?",
            (lot['id'],),
            fetchone=True
        )['count']
        
        remaining = ""
        if lot['finish_type'] == "time":
            remaining = f"⏱ {format_time_remaining(lot['finish_value'])}"
        elif lot['finish_type'] == "count":
            remaining = f"👥 {participants}/{lot['finish_value']}"
        
        # Обрезаем текст для отображения
        preview = lot['text'][:50] + "..." if len(lot['text']) > 50 else lot['text']
        text += f"🎁 #{lot['id']} - {remaining}\n{preview}\n\n"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Участвовать", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lots[0]['id']}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "my_participations")
async def my_participations(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    participations = db_query(
        """SELECT l.id, l.text, l.status, l.winner_id, l.created_at 
           FROM participants p 
           JOIN lotteries l ON p.lot_id = l.id 
           WHERE p.user_id = ? 
           ORDER BY l.id DESC""",
        (user_id,),
        fetchall=True
    )
    
    if not participations:
        return await callback.answer("Вы еще не участвовали в розыгрышах", show_alert=True)
    
    text = "📋 Мои участия:\n\n"
    wins = 0
    
    for p in participations:
        status = "🏆 Вы победили!" if p['winner_id'] == user_id else "🔄 Активна" if p['status'] == 'active' else "❌ Завершена"
        preview = p['text'][:40] + "..." if len(p['text']) > 40 else p['text']
        text += f"🎁 #{p['id']} - {status}\n{preview}\n\n"
        
        if p['winner_id'] == user_id:
            wins += 1
    
    text += f"\n📊 Итого: {len(participations)} участий, {wins} побед"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Новые розыгрыши", callback_data="active_lotteries")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💼 PR Заявка", callback_data="apply_pr")],
        [InlineKeyboardButton(text="📢 Активные розыгрыши", callback_data="active_lotteries")],
        [InlineKeyboardButton(text="📊 Мои участия", callback_data="my_participations")]
    ])
    
    if user_id in ADMIN_IDS:
        keyboard.inline_keyboard.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_panel")])
    
    await callback.message.edit_text(
        f"👋 Добро пожаловать, {callback.from_user.first_name}!\n\n"
        f"Выберите действие:",
        reply_markup=keyboard
    )
    await callback.answer()

# --- 12. КРОН-ЗАДАЧА ДЛЯ ПРОВЕРКИ ВРЕМЕНИ ---
async def check_lotteries_time():
    """Проверяет и завершает лотереи по времени"""
    while True:
        try:
            # Ищем активные лотереи с завершением по времени
            lots = db_query(
                """SELECT id, finish_value FROM lotteries 
                   WHERE status = 'active' AND finish_type IN ('time', 'both')""",
                fetchall=True
            )
            
            now = datetime.now()
            
            for lot in lots:
                try:
                    finish_time = datetime.strptime(lot['finish_value'], "%d.%m.%Y %H:%M")
                    if now >= finish_time:
                        await finish_giveaway(lot['id'])
                except:
                    continue
            
            # Проверяем каждую минуту
            await asyncio.sleep(60)
        except Exception as e:
            logging.error(f"Error in check_lotteries_time: {e}")
            await asyncio.sleep(60)

# --- 13. ЗАПУСК БОТА ---
async def on_startup():
    """Действия при запуске бота"""
    init_db()
    print("✅ База данных инициализирована")
    print(f"✅ Бот запущен как @{(await bot.get_me()).username}")
    print(f"✅ Админы: {ADMIN_IDS}")
    print(f"✅ Канал лотерей: {LOT_CHANNEL}")
    
    # Запускаем фоновую задачу проверки времени
    asyncio.create_task(check_lotteries_time())

async def main():
    await on_startup()
    print("🚀 Бот запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())