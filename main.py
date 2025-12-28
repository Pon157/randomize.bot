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

# --- 1. ЛОГИРОВАНИЕ И КОНФИГУРАЦИЯ ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
PR_CHAT_ID = int(os.getenv("PR_CHAT_ID", "0"))
LOT_CHANNEL = os.getenv("LOT_CHANNEL", "@lotsvitechek")

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- 2. БАЗА ДАННЫХ (С АВТОМАТИЧЕСКОЙ МИГРАЦИЕЙ) ---
def init_db():
    """Инициализация таблиц и проверка структуры на наличие нужных колонок"""
    conn = sqlite3.connect("bot_database.db")
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
        photo TEXT, 
        sticker TEXT, 
        winners_count INTEGER DEFAULT 1,
        participants_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    
    # Таблица победителей
    cur.execute("""CREATE TABLE IF NOT EXISTS winners (
        lot_id INTEGER, 
        user_id INTEGER, 
        PRIMARY KEY (lot_id, user_id)
    )""")
    
    # Таблица отзывов
    cur.execute("""CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        user_id INTEGER, 
        lot_id INTEGER, 
        text TEXT, 
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    
    # Таблица пользователей бота
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, 
        username TEXT, 
        full_name TEXT, 
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # ПРОВЕРКА СТРУКТУРЫ (Миграции)
    cur.execute("PRAGMA table_info(lotteries)")
    existing_cols = [c[1] for c in cur.fetchall()]
    
    needed_updates = {
        'winners_count': 'INTEGER DEFAULT 1',
        'photo': 'TEXT',
        'sticker': 'TEXT',
        'participants_count': 'INTEGER DEFAULT 0'
    }
    
    for col, definition in needed_updates.items():
        if col not in existing_cols:
            logger.info(f"Миграция: Добавление колонки {col} в lotteries...")
            cur.execute(f"ALTER TABLE lotteries ADD COLUMN {col} {definition}")
    
    conn.commit()
    conn.close()

def db_query(query, params=(), fetchone=False, fetchall=False, commit=False):
    """Универсальная функция для запросов к БД"""
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
    winners_count = State()
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

class LeaveReview(StatesGroup):
    lot_id = State()
    text = State()

# --- 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (ЛОГИКА) ---
async def update_lottery_button(lot_id: int, count: int):
    """Обновление счетчика на кнопке в канале"""
    lot = db_query("SELECT message_id FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or not lot['message_id']: return
    
    builder = InlineKeyboardBuilder()
    builder.button(text=f"✅ Участвовать! ({count})", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")
    
    try:
        await bot.edit_message_reply_markup(chat_id=LOT_CHANNEL, message_id=lot['message_id'], reply_markup=builder.as_markup())
    except Exception as e:
        logger.debug(f"Кнопка не изменилась (возможно, значение то же): {e}")

async def check_subscription(user_id: int, channels_str: str):
    """Проверка подписки на список каналов"""
    if not channels_str or channels_str.lower() in ['нет', 'none', '']:
        return True, []
    
    not_subscribed = []
    channels = [c.strip() for c in channels_str.split(",") if c.strip()]
    
    for channel in channels:
        try:
            member = await bot.get_chat_member(channel, user_id)
            if member.status in ["left", "kicked"]:
                not_subscribed.append(channel)
        except Exception:
            not_subscribed.append(channel)
    return len(not_subscribed) == 0, not_subscribed

async def finish_giveaway(lot_id: int):
    """Завершение лотереи и выбор победителей"""
    lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or lot['status'] == 'closed':
        return
    
    participants = db_query("SELECT * FROM participants WHERE lot_id = ?", (lot_id,), fetchall=True)
    db_query("UPDATE lotteries SET status = 'closed' WHERE id = ?", (lot_id,), commit=True)
    
    if not participants:
        await bot.send_message(LOT_CHANNEL, f"🔔 Лотерея #{lot_id} завершена. Участников не было.")
        return

    # Логика выбора победителей
    winners_needed = lot['winners_count']
    count_to_pick = min(len(participants), winners_needed)
    winners_list = random.sample(participants, count_to_pick)
    
    mentions = []
    for winner in winners_list:
        db_query("INSERT OR IGNORE INTO winners (lot_id, user_id) VALUES (?,?)", (lot_id, winner['user_id']), commit=True)
        user_mention = f"@{winner['username']}" if winner['username'] else winner['full_name']
        mentions.append(user_mention)
        
        try:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✍️ Оставить отзыв", callback_data=f"rev_{lot_id}")]
            ])
            await bot.send_message(winner['user_id'], f"🎉 Поздравляем! Ты победил в лотерее #{lot_id}!", reply_markup=kb)
        except: pass

    winners_text = ", ".join(mentions)
    final_msg = (
        f"🎊 **ИТОГИ ЛОТЕРЕИ #{lot_id}!**\n\n"
        f"🏆 Победители: {winners_text}\n"
        f"📊 Всего участников: {len(participants)}\n\n"
        f"Победители выбраны рандомно. Спасибо всем!"
    )
    
    try:
        await bot.send_message(LOT_CHANNEL, final_msg, parse_mode="Markdown", reply_to_message_id=lot['message_id'])
    except:
        await bot.send_message(LOT_CHANNEL, final_msg, parse_mode="Markdown")

# --- 5. ОБРАБОТКА КОМАНДЫ /START ---
@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    user_id = message.from_user.id
    # Регистрация пользователя
    db_query("INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?,?,?)", 
             (user_id, message.from_user.username, message.from_user.full_name), commit=True)

    # Если переход по ссылке на лот
    if command.args and command.args.startswith("lot_"):
        lot_id = int(command.args.split("_")[1])
        lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
        
        if not lot or lot['status'] == 'closed':
            return await message.answer("❌ Извини, этот розыгрыш уже завершен.")
        
        # Проверка ОП
        is_ok, missing = await check_subscription(user_id, lot['channels'])
        if not is_ok:
            builder = InlineKeyboardBuilder()
            for ch in missing:
                builder.button(text=f"📢 Подписаться на {ch}", url=f"https://t.me/{ch.lstrip('@')}")
            builder.button(text="🔄 Я подписался", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")
            return await message.answer("⚠️ Для участия подпишись на каналы:", reply_markup=builder.adjust(1).as_markup())

        try:
            db_query("INSERT INTO participants (user_id, lot_id, username, full_name) VALUES (?,?,?,?)", 
                     (user_id, lot_id, message.from_user.username, message.from_user.full_name), commit=True)
            
            # Обновляем стат
            p_data = db_query("SELECT COUNT(*) as total FROM participants WHERE lot_id=?", (lot_id,), fetchone=True)
            total_count = p_data['total']
            db_query("UPDATE lotteries SET participants_count = ? WHERE id = ?", (total_count, lot_id), commit=True)
            
            await update_lottery_button(lot_id, total_count)
            
            # Проверка авто-финиша
            if lot['finish_type'] == 'count' and total_count >= int(lot['finish_value']):
                await finish_giveaway(lot_id)
                
            await message.answer(f"✅ Готово! Ты участвуешь в лотерее #{lot_id}!")
        except sqlite3.IntegrityError:
            await message.answer("✅ Ты уже зарегистрирован в этом розыгрыше.")
        return

    # Главное меню
    kb = [
        [InlineKeyboardButton(text="💬 Отзывы", callback_data="view_reviews"), InlineKeyboardButton(text="💼 PR Заявка", callback_data="apply_pr")],
        [InlineKeyboardButton(text="📢 Розыгрыши", callback_data="active_lots"), InlineKeyboardButton(text="📊 Мой профиль", callback_data="my_stats")]
    ]
    if user_id in ADMIN_IDS:
        kb.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_main")])
    
    await message.answer(f"👋 Привет, {message.from_user.first_name}!\nДобро пожаловать!", 
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

# --- 6. АДМИН-ФУНКЦИОНАЛ ---
@dp.callback_query(F.data == "admin_main")
async def admin_panel(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS: return
    kb = [
        [InlineKeyboardButton(text="➕ Создать лот", callback_data="adm_create")],
        [InlineKeyboardButton(text="📝 Изменить настройки", callback_data="adm_edit_list")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats_full")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="start_back")]
    ]
    await c.message.edit_text("🛠 **Панель управления**", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")

@dp.callback_query(F.data == "adm_create")
async def adm_create_init(c: CallbackQuery, state: FSMContext):
    await state.set_state(CreateLot.text)
    await c.message.answer("1️⃣ Отправьте сообщение для лотереи (Текст, Фото с описанием или любой Стикер):")

@dp.message(CreateLot.text)
async def adm_create_post(m: Message, state: FSMContext):
    # Собираем данные поста
    post_data = {
        "text": m.caption or m.text or "",
        "entities": json.dumps([e.model_dump(mode='json') for e in (m.entities or m.caption_entities or [])]),
        "photo": m.photo[-1].file_id if m.photo else None,
        "sticker": m.sticker.file_id if m.sticker else None
    }
    await state.update_data(post=post_data)
    await state.set_state(CreateLot.winners_count)
    await m.answer("2️⃣ Сколько победителей должно быть?")

@dp.message(CreateLot.winners_count)
async def adm_create_winners(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("Введите число!")
    await state.update_data(wc=int(m.text))
    await state.set_state(CreateLot.channels)
    await m.answer("3️⃣ Введите юзернеймы каналов (через запятую) или 'нет':")

@dp.message(CreateLot.channels)
async def adm_create_subs(m: Message, state: FSMContext):
    await state.update_data(channels=m.text)
    kb = [
        [InlineKeyboardButton(text="⏰ По времени", callback_data="set_time"), InlineKeyboardButton(text="👥 По участникам", callback_data="set_count")]
    ]
    await m.answer("4️⃣ Выберите тип завершения:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("set_"))
async def adm_create_type(c: CallbackQuery, state: FSMContext):
    f_type = c.data.split("_")[1]
    await state.update_data(ft=f_type)
    msg = "Через сколько часов завершить?" if f_type == "time" else "При каком кол-ве участников завершить?"
    await c.message.answer(msg)
    await state.set_state(CreateLot.value)

@dp.message(CreateLot.value)
async def adm_create_finish(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("Введите число!")
    
    data = await state.get_data()
    post = data['post']
    
    val = m.text
    if data['ft'] == "time":
        val = (datetime.now() + timedelta(hours=int(m.text))).strftime("%d.%m.%Y %H:%M")

    # ИСПРАВЛЕННЫЙ ЗАПРОС К БД (KeyError Fix)
    lot_id = db_query(
        "INSERT INTO lotteries (text, entities, channels, finish_type, finish_value, photo, sticker, winners_count) VALUES (?,?,?,?,?,?,?,?)",
        (post['text'], post['entities'], data['channels'], data['ft'], val, post['photo'], post['sticker'], data['wc']),
        commit=True
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Участвовать! (0)", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")
    
    try:
        if post['photo']:
            sent = await bot.send_photo(LOT_CHANNEL, post['photo'], caption=post['text'], reply_markup=builder.as_markup())
        elif post['sticker']:
            await bot.send_sticker(LOT_CHANNEL, post['sticker'])
            sent = await bot.send_message(LOT_CHANNEL, "🎁 Участвуй в новом розыгрыше!", reply_markup=builder.as_markup())
        else:
            sent = await bot.send_message(LOT_CHANNEL, post['text'], reply_markup=builder.as_markup())
        
        db_query("UPDATE lotteries SET message_id = ? WHERE id = ?", (sent.message_id, lot_id), commit=True)
        await m.answer(f"✅ Розыгрыш #{lot_id} запущен!")
    except Exception as e:
        await m.answer(f"❌ Ошибка отправки: {e}")
    
    await state.clear()

# --- 7. УПРАВЛЕНИЕ ЛОТАМИ ---
@dp.callback_query(F.data == "adm_edit_list")
async def adm_edit_list(c: CallbackQuery):
    active = db_query("SELECT * FROM lotteries WHERE status='active'", fetchall=True)
    if not active: return await c.answer("Нет активных лотов", show_alert=True)
    
    builder = InlineKeyboardBuilder()
    for lot in active:
        builder.button(text=f"⚙️ Редактировать #{lot['id']}", callback_data=f"edit_l_{lot['id']}")
    builder.adjust(1)
    await c.message.edit_text("Выберите лот:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("edit_l_"))
async def adm_edit_item(c: CallbackQuery):
    lid = c.data.split("_")[2]
    kb = InlineKeyboardBuilder()
    kb.button(text="Каналы", callback_data=f"mod_{lid}_channels")
    kb.button(text="Лимит/Время", callback_data=f"mod_{lid}_finish_value")
    kb.button(text="Победители", callback_data=f"mod_{lid}_winners_count")
    kb.button(text="🛑 Принудительный Стоп", callback_data=f"mod_{lid}_stop")
    kb.button(text="🔙 Назад", callback_data="adm_edit_list")
    await c.message.edit_text(f"Управление лотом #{lid}:", reply_markup=kb.adjust(1).as_markup())

@dp.callback_query(F.data.startswith("mod_"))
async def adm_edit_field(c: CallbackQuery, state: FSMContext):
    _, lid, field = c.data.split("_")
    if field == "stop":
        await finish_giveaway(int(lid))
        return await c.message.answer(f"✅ Лот #{lid} принудительно завершен!")
    
    await state.update_data(e_lid=lid, e_field=field)
    await state.set_state(EditLot.new_value)
    await c.message.answer(f"Введите новое значение для {field}:")

@dp.message(EditLot.new_value)
async def adm_edit_save(m: Message, state: FSMContext):
    d = await state.get_data()
    db_query(f"UPDATE lotteries SET {d['e_field']} = ? WHERE id = ?", (m.text, d['e_lid']), commit=True)
    await m.answer("✅ Сохранено!")
    await state.clear()

# --- 8. ОТЗЫВЫ И PR ---
@dp.callback_query(F.data == "view_reviews")
async def view_reviews(c: CallbackQuery):
    revs = db_query("SELECT r.*, u.full_name FROM reviews r JOIN users u ON r.user_id = u.user_id ORDER BY r.id DESC LIMIT 10", fetchall=True)
    if not revs: return await c.answer("Отзывов нет", show_alert=True)
    
    res = "💬 **Отзывы победителей:**\n\n"
    for r in revs:
        res += f"👤 {r['full_name']} (Лот #{r['lot_id']}):\n«{r['text']}»\n{'-'*15}\n"
    await c.message.answer(res, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("rev_"))
async def rev_start(c: CallbackQuery, state: FSMContext):
    lid = c.data.split("_")[1]
    # Проверка на победителя
    check = db_query("SELECT * FROM winners WHERE lot_id=? AND user_id=?", (lid, c.from_user.id), fetchone=True)
    if not check: return await c.answer("❌ Ты не побеждал здесь!", show_alert=True)
    
    await state.update_data(rev_lid=lid)
    await state.set_state(LeaveReview.text)
    await c.message.answer("Напишите ваш отзыв:")

@dp.message(LeaveReview.text)
async def rev_save(m: Message, state: FSMContext):
    d = await state.get_data()
    db_query("INSERT INTO reviews (user_id, lot_id, text) VALUES (?,?,?)", (m.from_user.id, d['rev_lid'], m.text), commit=True)
    await m.answer("✅ Отзыв принят! Спасибо.")
    await state.clear()

@dp.callback_query(F.data == "apply_pr")
async def pr_init(c: CallbackQuery, state: FSMContext):
    await state.set_state(PRApplication.age)
    await c.message.answer("Заполнение PR анкеты.\n1. Сколько вам лет?")

@dp.message(PRApplication.age)
async def pr_age(m: Message, state: FSMContext):
    await state.update_data(a=m.text)
    await state.set_state(PRApplication.nickname)
    await m.answer("2. Ваш юзернейм или ссылка на канал?")

@dp.message(PRApplication.nickname)
async def pr_nick(m: Message, state: FSMContext):
    await state.update_data(n=m.text)
    await state.set_state(PRApplication.proofs)
    await m.answer("3. Пришлите скриншот статистики:")

@dp.message(PRApplication.proofs, F.photo)
async def pr_done(m: Message, state: FSMContext):
    d = await state.get_data()
    caption = (f"📩 **НОВАЯ PR ЗАЯВКА**\n\n"
               f"👤 От: @{m.from_user.username}\n"
               f"🔞 Возраст: {d['a']}\n"
               f"🔗 Ссылка: {d['n']}")
    if PR_CHAT_ID:
        await bot.send_photo(PR_CHAT_ID, m.photo[-1].file_id, caption=caption)
    await m.answer("✅ Ваша заявка передана администраторам!")
    await state.clear()

@dp.callback_query(F.data == "start_back")
async def start_back(c: CallbackQuery, state: FSMContext):
    await cmd_start(c.message, CommandObject(command="start", args=None), state)
    await c.message.delete()

# --- 9. ПЛАНИРОВЩИК (ТАЙМЕРЫ) ---
async def scheduler_task():
    while True:
        try:
            active_time_lots = db_query("SELECT * FROM lotteries WHERE status='active' AND finish_type='time'", fetchall=True)
            for lot in active_time_lots:
                try:
                    target = datetime.strptime(lot['finish_value'], "%d.%m.%Y %H:%M")
                    if datetime.now() >= target:
                        await finish_giveaway(lot['id'])
                except: continue
        except Exception as e:
            logger.error(f"Ошибка шедулера: {e}")
        await asyncio.sleep(60)

# --- 10. ЗАПУСК БОТА ---
async def main():
    init_db()
    asyncio.create_task(scheduler_task())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот выключен.")
