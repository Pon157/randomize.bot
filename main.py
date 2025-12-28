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
from aiogram.utils.keyboard import InlineKeyboardBuilder

# --- 1. НАСТРОЙКИ ---
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
# Получаем список админов и чистим от пробелов
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
PR_CHAT_ID = int(os.getenv("PR_CHAT_ID", "0"))
LOT_CHANNEL = os.getenv("LOT_CHANNEL", "@lotsvitechek")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- 2. БАЗА ДАННЫХ ---
def init_db():
    with sqlite3.connect("bot_database.db") as conn:
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS lotteries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT, entities TEXT, channels TEXT,
            finish_type TEXT, finish_value TEXT,
            status TEXT DEFAULT 'active', message_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            winner_id INTEGER, participants_count INTEGER DEFAULT 0
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS participants (
            user_id INTEGER, lot_id INTEGER, username TEXT, full_name TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, lot_id)
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
            is_admin BOOLEAN DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.commit()

def db_query(query, params=(), fetchone=False, fetchall=False, commit=False):
    """Универсальная функция для работы с БД"""
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
    participants_count = State()

class PRApplication(StatesGroup):
    age = State()
    nickname = State()
    proofs = State()

class AdminStates(StatesGroup):
    manual_count = State()
    broadcast_text = State() # Добавил для рассылки

# --- 4. УТИЛИТЫ ---
async def check_user_subscription(user_id: int, channels: list) -> tuple:
    not_subscribed = []
    for channel in channels:
        if not channel.strip(): continue
        try:
            member = await bot.get_chat_member(channel.strip(), user_id)
            if member.status in ["left", "kicked"]:
                not_subscribed.append(channel.strip())
        except Exception as e:
            logging.error(f"Ошибка проверки подписки {channel}: {e}")
            not_subscribed.append(channel.strip())
    return len(not_subscribed) == 0, not_subscribed

def format_time_remaining(finish_time: str) -> str:
    try:
        end_time = datetime.strptime(finish_time, "%d.%m.%Y %H:%M")
        delta = end_time - datetime.now()
        if delta.total_seconds() <= 0: return "Завершено"
        
        days = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        
        if days > 0: return f"{days}д. {hours}ч."
        return f"{hours}ч. {minutes}м."
    except: return "Неизвестно"

# --- 5. ЛОГИКА ЗАВЕРШЕНИЯ ---
async def finish_giveaway(lot_id: int, manual: bool = False):
    """Завершает лотерею и выбирает победителя"""
    lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or lot['status'] == 'closed': return

    participants = db_query("SELECT user_id, username, full_name FROM participants WHERE lot_id = ?", (lot_id,), fetchall=True)
    
    # Если участников нет
    if not participants:
        db_query("UPDATE lotteries SET status = 'closed' WHERE id = ?", (lot_id,), commit=True)
        try:
            msg_text = f"🔔 Лотерея #{lot_id} окончена. Участников нет." + ("\n(Вручную)" if manual else "")
            await bot.send_message(LOT_CHANNEL, msg_text, reply_to_message_id=lot['message_id'])
        except: pass
        return

    # Выбор победителя
    winner = random.choice(participants)
    db_query("UPDATE lotteries SET status = 'closed', winner_id = ?, participants_count = ? WHERE id = ?", 
             (winner['user_id'], len(participants), lot_id), commit=True)

    mention = f"@{winner['username']}" if winner['username'] else f"[{winner['full_name']}](tg://user?id={winner['user_id']})"
    
    # Формируем список (до 10 человек для красоты)
    p_list = "\n".join([f"• @{p['username']}" if p['username'] else f"• {p['full_name']}" for p in participants[:10]])
    if len(participants) > 10: p_list += f"\n... и еще {len(participants)-10}"

    text = (f"🎊 **Итоги розыгрыша #{lot_id}!**\n"
            f"🏆 **Победитель:** {mention}\n"
            f"📊 Участников: {len(participants)}\n"
            f"⏱ Завершено: {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
            f"{'(Завершено вручную)' if manual else ''}\n"
            f"👥 **Участники:**\n{p_list}")

    try:
        await bot.send_message(LOT_CHANNEL, text, parse_mode="Markdown", reply_to_message_id=lot['message_id'])
        await bot.send_message(winner['user_id'], f"🎉 Поздравляем! Вы победили в лотерее #{lot_id}!\n[Ссылка на пост](https://t.me/{LOT_CHANNEL.lstrip('@')}/{lot['message_id']})", parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Ошибка отправки итогов: {e}")

# --- 6. START И РЕГИСТРАЦИЯ ---
@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    user_id = message.from_user.id
    db_query("INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?, ?, ?)", 
             (user_id, message.from_user.username, message.from_user.full_name), commit=True)

    # Если перешли по ссылке lot_ID
    if command.args and command.args.startswith("lot_"):
        lot_id = int(command.args.split("_")[1])
        lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
        
        if not lot: return await message.answer("⚠️ Розыгрыш не найден!")
        if lot['status'] == 'closed': return await message.answer("⚠️ Розыгрыш уже завершен!")

        # Проверка подписки
        ch_list = [c.strip() for c in lot['channels'].split(",") if c.strip()]
        is_sub, not_sub = await check_user_subscription(user_id, ch_list)
        
        if not is_sub:
            kb = InlineKeyboardBuilder()
            for ch in not_sub:
                kb.button(text=f"📢 {ch}", url=f"https://t.me/{ch.lstrip('@')}")
            kb.button(text="🔄 Проверить подписку", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")
            kb.adjust(1)
            return await message.answer("❌ Для участия подпишитесь на каналы:", reply_markup=kb.as_markup())

        # Регистрация
        try:
            db_query("INSERT INTO participants (user_id, lot_id, username, full_name) VALUES (?,?,?,?)",
                     (user_id, lot_id, message.from_user.username, message.from_user.full_name), commit=True)
            # Увеличиваем счетчик в лотерее
            db_query("UPDATE lotteries SET participants_count = participants_count + 1 WHERE id = ?", (lot_id,), commit=True)
            
            # Проверка завершения по количеству
            if lot['finish_type'] == 'count' or lot['finish_type'] == 'both':
                count = db_query("SELECT COUNT(*) as c FROM participants WHERE lot_id=?", (lot_id,), fetchone=True)['c']
                target = int(lot['finish_value'].split('|')[0] if '|' in lot['finish_value'] else lot['finish_value'])
                if count >= target:
                    await finish_giveaway(lot_id)
            
            await message.answer(f"✅ Вы успешно зарегистрированы в розыгрыше #{lot_id}!")
        except sqlite3.IntegrityError:
            await message.answer(f"✅ Вы уже участвуете в розыгрыше #{lot_id}!")
        return

    # Главное меню
    await state.clear()
    kb = [
        [InlineKeyboardButton(text="💼 PR Заявка", callback_data="apply_pr")],
        [InlineKeyboardButton(text="📢 Активные розыгрыши", callback_data="active_lotteries")],
        [InlineKeyboardButton(text="📊 Мои участия", callback_data="my_participations")]
    ]
    if user_id in ADMIN_IDS:
        kb.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_panel")])
    
    await message.answer(f"👋 Привет, {message.from_user.first_name}! Я бот для розыгрышей.", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

# --- 7. PR ЗАЯВКИ ---
@dp.callback_query(F.data == "apply_pr")
async def pr_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PRApplication.age)
    await callback.message.answer("📝 Шаг 1: Укажите ваш возраст:")
    await callback.answer()

@dp.message(PRApplication.age)
async def pr_age(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Введите число!")
    await state.update_data(age=message.text)
    await state.set_state(PRApplication.nickname)
    await message.answer("📝 Шаг 2: Укажите ваш никнейм/канал:")

@dp.message(PRApplication.nickname)
async def pr_nick(message: Message, state: FSMContext):
    await state.update_data(nickname=message.text)
    await state.set_state(PRApplication.proofs)
    await message.answer("📝 Шаг 3: Пришлите скриншот статистики (фото):")

@dp.message(PRApplication.proofs, F.photo)
async def pr_proofs(message: Message, state: FSMContext):
    data = await state.get_data()
    caption = (f"📩 **Новая заявка PR**\n👤: {message.from_user.full_name} (@{message.from_user.username})\n"
               f"🔞 Возраст: {data['age']}\n🔗 Канал: {data['nickname']}")
    
    if PR_CHAT_ID:
        await bot.send_photo(PR_CHAT_ID, message.photo[-1].file_id, caption=caption)
    
    await message.answer("✅ Заявка отправлена!")
    await state.clear()

# --- 8. АДМИН ПАНЕЛЬ ---
@dp.callback_query(F.data == "admin_panel", F.from_user.id.in_(ADMIN_IDS))
async def admin_panel(callback: CallbackQuery):
    kb = [
        [InlineKeyboardButton(text="📢 Создать лотерею", callback_data="admin_create")],
        [InlineKeyboardButton(text="📋 Список активных", callback_data="admin_list_active")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📨 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="admin_settings")]
    ]
    await callback.message.edit_text("🛠 Админ-панель", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

# СОЗДАНИЕ ЛОТЕРЕИ
@dp.callback_query(F.data == "admin_create", F.from_user.id.in_(ADMIN_IDS))
async def create_init(callback: CallbackQuery, state: FSMContext):
    await state.set_state(CreateLot.text)
    await callback.message.answer("1. Пришлите текст поста (можно с фото/видео, но лучше просто текст с разметкой):")
    await callback.answer()

@dp.message(CreateLot.text)
async def create_text(message: Message, state: FSMContext):
    ents = json.dumps([e.model_dump() for e in message.entities]) if message.entities else "[]"
    await state.update_data(text=message.text, entities=ents)
    await state.set_state(CreateLot.channels)
    await message.answer("2. Каналы для подписки через запятую (или 'нет'):")

@dp.message(CreateLot.channels)
async def create_channels(message: Message, state: FSMContext):
    ch = "" if message.text.lower() == 'нет' else message.text
    await state.update_data(channels=ch)
    
    # ИСПРАВЛЕННЫЕ КНОПКИ (были ошибки синтаксиса)
    kb = [
        [InlineKeyboardButton(text="⏰ По времени", callback_data="type_time")],
        [InlineKeyboardButton(text="👥 По количеству", callback_data="type_count")],
        [InlineKeyboardButton(text="⏰ + 👥 Оба условия", callback_data="type_both")]
    ]
    await message.answer("3. Тип завершения:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(CreateLot.finish_type)

@dp.callback_query(F.data.startswith("type_"), CreateLot.finish_type)
async def create_type(callback: CallbackQuery, state: FSMContext):
    ftype = callback.data.split("_")[1]
    await state.update_data(ftype=ftype)
    
    if ftype in ["time", "both"]:
        kb = [
            [InlineKeyboardButton(text="1 час", callback_data="val_1h"), InlineKeyboardButton(text="3 часа", callback_data="val_3h")],
            [InlineKeyboardButton(text="12 часов", callback_data="val_12h"), InlineKeyboardButton(text="1 день", callback_data="val_1d")],
            [InlineKeyboardButton(text="⚙️ Вручную (ЧЧ:ММ)", callback_data="val_custom")]
        ]
        await callback.message.edit_text("4. Выберите время:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        await state.set_state(CreateLot.value)
    else:
        await callback.message.edit_text("4. Введите количество участников (число):")
        await state.set_state(AdminStates.manual_count)

@dp.callback_query(F.data.startswith("val_"), CreateLot.value)
async def create_val_btn(callback: CallbackQuery, state: FSMContext):
    val = callback.data.split("_")[1]
    if val == "custom":
        await callback.message.edit_text("Введите время через сколько завершить (например '2' (часа) или '2:30'):")
        await state.set_state(AdminStates.manual_count)
        return
    
    # Расчет времени
    deltas = {"1h": 1, "3h": 3, "12h": 12, "1d": 24}
    f_time = (datetime.now() + timedelta(hours=deltas.get(val, 24))).strftime("%d.%m.%Y %H:%M")
    
    data = await state.get_data()
    if data['ftype'] == "both":
        await state.update_data(finish_val=f_time)
        await callback.message.edit_text(f"Время: {f_time}. Теперь введите мин. кол-во участников:")
        await state.set_state(CreateLot.participants_count)
    else:
        await finalize_lot(callback.message, state, f_time)

@dp.message(AdminStates.manual_count)
async def create_manual_val(message: Message, state: FSMContext):
    data = await state.get_data()
    val = message.text.strip()
    
    if data['ftype'] == "count":
        if not val.isdigit(): return await message.answer("Введите число!")
        await finalize_lot(message, state, val)
        return

    # Если ручной ввод времени
    if ":" in val or val.isdigit():
        try:
            if ":" in val: h, m = map(int, val.split(":"))
            else: h, m = int(val), 0
            f_time = (datetime.now() + timedelta(hours=h, minutes=m)).strftime("%d.%m.%Y %H:%M")
            if data['ftype'] == "both":
                await state.update_data(finish_val=f_time)
                await message.answer("Теперь введите число участников:")
                await state.set_state(CreateLot.participants_count)
            else:
                await finalize_lot(message, state, f_time)
        except: await message.answer("Ошибка формата!")

@dp.message(CreateLot.participants_count)
async def create_both_final(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Число!")
    data = await state.get_data()
    # Сохраняем как "КОЛИЧЕСТВО|ВРЕМЯ"
    combined_val = f"{message.text}|{data['finish_val']}"
    await finalize_lot(message, state, combined_val)

async def finalize_lot(message, state, f_val):
    data = await state.get_data()
    lot_id = db_query("INSERT INTO lotteries (text, entities, channels, finish_type, finish_value) VALUES (?,?,?,?,?)",
                      (data['text'], data['entities'], data['channels'], data['ftype'], f_val), commit=True)
    
    kb = [[InlineKeyboardButton(text="✅ Участвовать!", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")]]
    
    # Восстановление форматирования
    ents = [types.MessageEntity(**json.loads(e)) for e in json.loads(data['entities'])] if data['entities'] != "[]" else None
    
    sent = await bot.send_message(LOT_CHANNEL, data['text'], entities=ents, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    db_query("UPDATE lotteries SET message_id = ? WHERE id = ?", (sent.message_id, lot_id), commit=True)
    
    await message.answer(f"✅ Лотерея #{lot_id} запущена!\nТип: {data['ftype']}\nУсловие: {f_val}")
    await state.clear()

# СПИСОК И УПРАВЛЕНИЕ
@dp.callback_query(F.data == "admin_list_active", F.from_user.id.in_(ADMIN_IDS))
async def list_active(callback: CallbackQuery):
    lots = db_query("SELECT * FROM lotteries WHERE status='active'", fetchall=True)
    if not lots: return await callback.message.edit_text("Нет активных лотерей.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]]))
    
    kb = InlineKeyboardBuilder()
    text = "📋 Активные:\n"
    for l in lots:
        p_count = db_query("SELECT COUNT(*) as c FROM participants WHERE lot_id=?", (l['id'],), fetchone=True)['c']
        text += f"#{l['id']} - Участников: {p_count}\n"
        kb.button(text=f"#{l['id']}", callback_data=f"lot_det_{l['id']}")
    kb.button(text="🔙 Назад", callback_data="admin_panel")
    kb.adjust(3)
    await callback.message.edit_text(text, reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("lot_det_"), F.from_user.id.in_(ADMIN_IDS))
async def lot_detail(callback: CallbackQuery):
    lid = int(callback.data.split("_")[2])
    l = db_query("SELECT * FROM lotteries WHERE id=?", (lid,), fetchone=True)
    p_count = db_query("SELECT COUNT(*) as c FROM participants WHERE lot_id=?", (lid,), fetchone=True)['c']
    
    kb = [
        [InlineKeyboardButton(text="🛑 Остановить", callback_data=f"stop_{lid}")],
        [InlineKeyboardButton(text="👥 Участники", callback_data=f"parts_{lid}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_list_active")]
    ]
    await callback.message.edit_text(f"Лотерея #{lid}\nТип: {l['finish_type']}\nЦель: {l['finish_value']}\nУчастников: {p_count}", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("stop_"), F.from_user.id.in_(ADMIN_IDS))
async def stop_lot(callback: CallbackQuery):
    lid = int(callback.data.split("_")[1])
    await finish_giveaway(lid, manual=True)
    await callback.answer("Завершено!")
    await list_active(callback)

@dp.callback_query(F.data.startswith("parts_"), F.from_user.id.in_(ADMIN_IDS))
async def show_parts(callback: CallbackQuery):
    lid = int(callback.data.split("_")[1])
    parts = db_query("SELECT username, full_name FROM participants WHERE lot_id=?", (lid,), fetchall=True)
    text = f"Участники #{lid}:\n" + "\n".join([f"@{p['username']}" if p['username'] else p['full_name'] for p in parts[:20]])
    if len(parts)>20: text += f"\n...всего {len(parts)}"
    kb = [[InlineKeyboardButton(text="🔙 Назад", callback_data=f"lot_det_{lid}")]]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

# СТАТИСТИКА
@dp.callback_query(F.data == "admin_stats", F.from_user.id.in_(ADMIN_IDS))
async def show_stats(callback: CallbackQuery):
    users = db_query("SELECT COUNT(*) as c FROM users", fetchone=True)['c']
    lots = db_query("SELECT COUNT(*) as c FROM lotteries", fetchone=True)['c']
    active = db_query("SELECT COUNT(*) as c FROM lotteries WHERE status='active'", fetchone=True)['c']
    kb = [[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]]
    await callback.message.edit_text(f"📊 Статистика:\n👥 Юзеров: {users}\n🎲 Лотерей всего: {lots}\n▶️ Активных: {active}", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

# РАССЫЛКА (Добавил, чтобы кнопка работала)
@dp.callback_query(F.data == "admin_broadcast", F.from_user.id.in_(ADMIN_IDS))
async def ask_broadcast(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите текст для рассылки всем пользователям:")
    await state.set_state(AdminStates.broadcast_text)
    await callback.answer()

@dp.message(AdminStates.broadcast_text)
async def do_broadcast(message: Message, state: FSMContext):
    users = db_query("SELECT user_id FROM users", fetchall=True)
    count = 0
    for u in users:
        try:
            await bot.send_message(u['user_id'], message.text)
            count += 1
            await asyncio.sleep(0.05) # Чтоб не забанили
        except: pass
    await message.answer(f"✅ Рассылка завершена. Доставлено: {count}")
    await state.clear()

@dp.callback_query(F.data == "admin_settings")
async def settings_stub(callback: CallbackQuery):
    await callback.answer("Настройки пока не добавлены", show_alert=True)

# --- 9. ПОЛЬЗОВАТЕЛЬСКИЕ СПИСКИ ---
@dp.callback_query(F.data == "active_lotteries")
async def user_list_active(callback: CallbackQuery):
    lots = db_query("SELECT * FROM lotteries WHERE status='active' LIMIT 5", fetchall=True)
    if not lots: return await callback.answer("Нет активных розыгрышей", show_alert=True)
    
    text = "🎁 Активные розыгрыши:\n"
    kb = InlineKeyboardBuilder()
    for l in lots:
        text += f"#{l['id']} - {l['text'][:30]}...\n"
        kb.button(text=f"Участвовать #{l['id']}", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{l['id']}")
    kb.button(text="🔙 Назад", callback_data="back_menu")
    kb.adjust(1)
    await callback.message.edit_text(text, reply_markup=kb.as_markup())

@dp.callback_query(F.data == "my_participations")
async def user_list_my(callback: CallbackQuery):
    uid = callback.from_user.id
    parts = db_query("""SELECT l.id, l.status, l.winner_id FROM participants p 
                        JOIN lotteries l ON p.lot_id = l.id WHERE p.user_id=?""", (uid,), fetchall=True)
    if not parts: return await callback.answer("Вы нигде не участвовали", show_alert=True)
    
    text = "📊 Ваши участия:\n"
    for p in parts:
        res = "🏆 ПОБЕДА" if p['winner_id'] == uid else ("✅ Активен" if p['status']=='active' else "❌ Проигрыш")
        text += f"Lottery #{p['id']} - {res}\n"
    
    kb = [[InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu")]]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data == "back_menu")
async def back_menu(callback: CallbackQuery):
    kb = [
        [InlineKeyboardButton(text="💼 PR Заявка", callback_data="apply_pr")],
        [InlineKeyboardButton(text="📢 Активные розыгрыши", callback_data="active_lotteries")],
        [InlineKeyboardButton(text="📊 Мои участия", callback_data="my_participations")]
    ]
    await callback.message.edit_text("Главное меню", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

# --- 10. ФОНОВАЯ ЗАДАЧА ---
async def checker():
    while True:
        try:
            # Получаем все активные лотереи
            lots = db_query("SELECT * FROM lotteries WHERE status = 'active'", fetchall=True)
            now = datetime.now()
            
            for lot in lots:
                # Сколько сейчас людей
                curr_users = db_query("SELECT COUNT(*) as c FROM participants WHERE lot_id=?", (lot['id'],), fetchone=True)['c']
                
                # Логика завершения
                should_finish = False
                
                if lot['finish_type'] == 'count':
                    if curr_users >= int(lot['finish_value']): should_finish = True
                    
                elif lot['finish_type'] == 'time':
                    f_time = datetime.strptime(lot['finish_value'], "%d.%m.%Y %H:%M")
                    if now >= f_time: should_finish = True
                    
                elif lot['finish_type'] == 'both':
                    # Формат: "10|25.12.2025 15:00"
                    parts = lot['finish_value'].split('|')
                    target_count = int(parts[0])
                    target_time = datetime.strptime(parts[1], "%d.%m.%Y %H:%M")
                    
                    # Если время вышло И набралось людей (или другое условие, как решишь)
                    # Обычно в "И" нужно выполнение обоих условий, или завершение по дедлайну?
                    # Сделаем: если время вышло, проверяем набралось ли людей. Если нет - просто закрываем или продлеваем?
                    # В этом коде: завершаем если время вышло И людей хватает.
                    if now >= target_time and curr_users >= target_count:
                        should_finish = True
                
                if should_finish:
                    await finish_giveaway(lot['id'])
                    
        except Exception as e:
            logging.error(f"Ошибка в чекере: {e}")
        
        await asyncio.sleep(30) # Проверка раз в 30 сек

# --- 11. СТАРТ ---
async def main():
    init_db()
    # Запускаем чекер в фоне
    asyncio.create_task(checker())
    try:
        await dp.start_polling(bot)
    except Exception as e:
        logging.error(f"Ошибка поллинга: {e}")

if __name__ == "__main__":
    asyncio.run(main())
