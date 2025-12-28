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
# Извлекаем токен и админов
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
PR_CHAT_ID = int(os.getenv("PR_CHAT_ID", "0"))
LOT_CHANNEL = os.getenv("LOT_CHANNEL", "@lotsvitechek")

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- 2. БАЗА ДАННЫХ С СИСТЕМОЙ МИГРАЦИЙ ---
def init_db():
    """Инициализация БД и автоматическое обновление структуры таблиц"""
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    
    # Таблица розыгрышей
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
    
    # Таблица пользователей
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, 
        username TEXT, 
        full_name TEXT, 
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # ПРОВЕРКА И ДОБАВЛЕНИЕ НЕДОСТАЮЩИХ КОЛОНОК (Fix OperationalError)
    cur.execute("PRAGMA table_info(lotteries)")
    existing_columns = [column[1] for column in cur.fetchall()]
    
    columns_to_add = {
        'winners_count': 'INTEGER DEFAULT 1',
        'photo': 'TEXT',
        'sticker': 'TEXT',
        'participants_count': 'INTEGER DEFAULT 0'
    }
    
    for col_name, col_type in columns_to_add.items():
        if col_name not in existing_columns:
            logger.info(f"Добавление недостающей колонки {col_name} в таблицу lotteries...")
            cur.execute(f"ALTER TABLE lotteries ADD COLUMN {col_name} {col_type}")
    
    conn.commit()
    conn.close()

def db_query(query, params=(), fetchone=False, fetchall=False, commit=False):
    with sqlite3.connect("bot_database.db") as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, params)
        if commit: conn.commit()
        if fetchone: return cur.fetchone()
        if fetchall: return cur.fetchall()
        return cur.lastrowid

# --- 3. СОСТОЯНИЯ FSM ---
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

# --- 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
async def update_lottery_button(lot_id: int, count: int):
    lot = db_query("SELECT message_id FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or not lot['message_id']: return
    
    kb = InlineKeyboardBuilder()
    kb.button(text=f"✅ Участвовать! ({count})", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")
    
    try:
        await bot.edit_message_reply_markup(
            chat_id=LOT_CHANNEL, 
            message_id=lot['message_id'], 
            reply_markup=kb.as_markup()
        )
    except Exception as e:
        logger.error(f"Ошибка обновления кнопки лота {lot_id}: {e}")

async def check_sub(user_id: int, channels_str: str):
    if not channels_str or channels_str.lower() in ['нет', 'none', '']:
        return True, []
    
    not_sub = []
    channels_list = [c.strip() for c in channels_str.split(",") if c.strip()]
    
    for ch in channels_list:
        try:
            member = await bot.get_chat_member(ch, user_id)
            if member.status in ["left", "kicked"]:
                not_sub.append(ch)
        except Exception:
            not_sub.append(ch)
    return len(not_sub) == 0, not_sub

async def finish_giveaway(lot_id: int):
    lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or lot['status'] == 'closed':
        return
    
    participants = db_query("SELECT * FROM participants WHERE lot_id = ?", (lot_id,), fetchall=True)
    db_query("UPDATE lotteries SET status = 'closed' WHERE id = ?", (lot_id,), commit=True)
    
    if not participants:
        await bot.send_message(LOT_CHANNEL, f"🔔 Лотерея #{lot_id} завершена. Участников не было.")
        return

    # Выбираем победителей
    count_to_pick = min(len(participants), lot['winners_count'])
    winners_list = random.sample(participants, count_to_pick)
    
    mentions = []
    for winner in winners_list:
        db_query("INSERT OR IGNORE INTO winners (lot_id, user_id) VALUES (?,?)", (lot_id, winner['user_id']), commit=True)
        name = f"@{winner['username']}" if winner['username'] else winner['full_name']
        mentions.append(name)
        
        try:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✍️ Оставить отзыв", callback_data=f"rev_{lot_id}")]
            ])
            await bot.send_message(winner['user_id'], f"🎉 Поздравляем! Ты победил в лотерее #{lot_id}!", reply_markup=kb)
        except Exception:
            pass

    results_text = (
        f"🎊 **ИТОГИ ЛОТЕРЕИ #{lot_id}!**\n\n"
        f"🏆 Победители: {', '.join(mentions)}\n"
        f"📊 Всего участников: {len(participants)}\n\n"
        f"Победителям отписал бот! 🎁"
    )
    
    try:
        await bot.send_message(LOT_CHANNEL, results_text, parse_mode="Markdown", reply_to_message_id=lot['message_id'])
    except Exception:
        await bot.send_message(LOT_CHANNEL, results_text, parse_mode="Markdown")

# --- 5. ХЕНДЛЕРЫ: СТАРТ И УЧАСТИЕ ---
@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    user_id = message.from_user.id
    db_query("INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?,?,?)", 
             (user_id, message.from_user.username, message.from_user.full_name), commit=True)

    if command.args and command.args.startswith("lot_"):
        lot_id = int(command.args.split("_")[1])
        lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
        
        if not lot or lot['status'] == 'closed':
            return await message.answer("❌ Извини, этот розыгрыш уже завершен.")
        
        is_sub, not_sub_list = await check_sub(user_id, lot['channels'])
        if not is_sub:
            builder = InlineKeyboardBuilder()
            for ch in not_sub_list:
                builder.button(text=f"📢 Подписаться на {ch}", url=f"https://t.me/{ch.lstrip('@')}")
            builder.button(text="🔄 Проверить подписку", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")
            return await message.answer("⚠️ Для участия необходимо подписаться на каналы:", reply_markup=builder.adjust(1).as_markup())

        try:
            db_query("INSERT INTO participants (user_id, lot_id, username, full_name) VALUES (?,?,?,?)", 
                     (user_id, lot_id, message.from_user.username, message.from_user.full_name), commit=True)
            
            # Обновляем счетчик
            count_data = db_query("SELECT COUNT(*) as total FROM participants WHERE lot_id=?", (lot_id,), fetchone=True)
            total = count_data['total']
            db_query("UPDATE lotteries SET participants_count = ? WHERE id = ?", (total, lot_id), commit=True)
            
            await update_lottery_button(lot_id, total)
            
            # Проверка финиша по количеству
            if lot['finish_type'] == 'count' and total >= int(lot['finish_value']):
                await finish_giveaway(lot_id)
                
            await message.answer(f"✅ Успешно! Ты в списке участников лотереи #{lot_id}!")
        except sqlite3.IntegrityError:
            await message.answer("✅ Ты уже зарегистрирован в этом розыгрыше.")
        return

    # Главное меню
    kb = [
        [InlineKeyboardButton(text="💬 Отзывы", callback_data="view_reviews"), InlineKeyboardButton(text="💼 PR Заявка", callback_data="apply_pr")],
        [InlineKeyboardButton(text="📢 Активные розыгрыши", callback_data="active_lots")]
    ]
    if user_id in ADMIN_IDS:
        kb.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_main")])
    
    await message.answer(f"👋 Привет, {message.from_user.first_name}!\nЯ бот для проведения лотерей.", 
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

# --- 6. АДМИН-ПАНЕЛЬ (ПОЛНЫЙ ФУНКЦИОНАЛ) ---
@dp.callback_query(F.data == "admin_main")
async def admin_panel(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS: return
    kb = [
        [InlineKeyboardButton(text="➕ Создать лот", callback_data="adm_create")],
        [InlineKeyboardButton(text="📝 Изменить настройки лота", callback_data="adm_edit_list")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="start_back")]
    ]
    await c.message.edit_text("🛠 **Меню администратора**", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")

@dp.callback_query(F.data == "adm_create")
async def adm_create_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(CreateLot.text)
    await c.message.answer("1️⃣ Пришлите пост для розыгрыша.\n(Текст, Фото с текстом или любой Стикер):")

@dp.message(CreateLot.text)
async def adm_create_text(m: Message, state: FSMContext):
    data = {
        "photo": m.photo[-1].file_id if m.photo else None,
        "sticker": m.sticker.file_id if m.sticker else None,
        "text": m.caption or m.text or "",
        "entities": json.dumps([e.model_dump(mode='json') for e in (m.entities or m.caption_entities or [])])
    }
    await state.update_data(lot_data=data)
    await state.set_state(CreateLot.winners_count)
    await m.answer("2️⃣ Сколько победителей будет в розыгрыше?")

@dp.message(CreateLot.winners_count)
async def adm_create_winners(m: Message, state: FSMContext):
    if not m.text.isdigit():
        return await m.answer("Введите число!")
    await state.update_data(winners_count=int(m.text))
    await state.set_state(CreateLot.channels)
    await m.answer("3️⃣ Укажите каналы для подписки через запятую (напр. @chan1, @chan2) или напишите 'нет':")

@dp.message(CreateLot.channels)
async def adm_create_channels(m: Message, state: FSMContext):
    await state.update_data(channels=m.text)
    kb = [
        [InlineKeyboardButton(text="⏰ По времени (в часах)", callback_data="type_time")],
        [InlineKeyboardButton(text="👥 По числу участников", callback_data="type_count")]
    ]
    await m.answer("4️⃣ Выберите условие завершения:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("type_"))
async def adm_create_type(c: CallbackQuery, state: FSMContext):
    finish_type = c.data.split("_")[1]
    await state.update_data(finish_type=finish_type)
    text = "Введите кол-во часов до финиша:" if finish_type == "time" else "Введите нужное кол-во участников:"
    await c.message.answer(text)
    await state.set_state(CreateLot.value)

@dp.message(CreateLot.value)
async def adm_create_final(m: Message, state: FSMContext):
    if not m.text.isdigit():
        return await m.answer("Введите число!")
    
    data = await state.get_data()
    lot_meta = data['lot_data']
    
    finish_val = m.text
    if data['finish_type'] == "time":
        finish_val = (datetime.now() + timedelta(hours=int(m.text))).strftime("%d.%m.%Y %H:%M")

    # Сохраняем в БД (ИСПРАВЛЕННЫЙ ЗАПРОС)
    lot_id = db_query(
        "INSERT INTO lotteries (text, entities, channels, finish_type, finish_value, photo, sticker, winners_count) VALUES (?,?,?,?,?,?,?,?)",
        (lot_meta['text'], lot_meta['entities'], data['channels'], data['finish_type'], finish_val, lot_meta['photo'], lot_meta['sticker'], data['winners_count']),
        commit=True
    )
    
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Участвовать! (0)", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")
    
    try:
        if lot_meta['photo']:
            sent = await bot.send_photo(LOT_CHANNEL, lot_meta['photo'], caption=lot_meta['text'], reply_markup=kb.as_markup())
        elif lot_meta['sticker']:
            await bot.send_sticker(LOT_CHANNEL, lot_meta['sticker'])
            sent = await bot.send_message(LOT_CHANNEL, "🎁 Участвуй в новом розыгрыше!", reply_markup=kb.as_markup())
        else:
            sent = await bot.send_message(LOT_CHANNEL, lot_meta['text'], reply_markup=kb.as_markup())
        
        db_query("UPDATE lotteries SET message_id = ? WHERE id = ?", (sent.message_id, lot_id), commit=True)
        await m.answer(f"✅ Лот #{lot_id} успешно опубликован в канале!")
    except Exception as e:
        await m.answer(f"❌ Ошибка публикации: {e}")
    
    await state.clear()

# --- 7. РЕДАКТИРОВАНИЕ И СТАТИСТИКА ---
@dp.callback_query(F.data == "adm_edit_list")
async def adm_edit_list(c: CallbackQuery):
    lots = db_query("SELECT * FROM lotteries WHERE status='active'", fetchall=True)
    if not lots:
        return await c.answer("Нет активных розыгрышей", show_alert=True)
    
    builder = InlineKeyboardBuilder()
    for lot in lots:
        builder.button(text=f"⚙️ Редактировать #{lot['id']}", callback_data=f"edit_lot_{lot['id']}")
    builder.adjust(1)
    await c.message.edit_text("Выберите розыгрыш для настройки:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("edit_lot_"))
async def adm_edit_options(c: CallbackQuery):
    lot_id = c.data.split("_")[2]
    kb = InlineKeyboardBuilder()
    kb.button(text="Изменить каналы", callback_data=f"setfield_{lot_id}_channels")
    kb.button(text="Изменить условие финиша", callback_data=f"setfield_{lot_id}_finish_value")
    kb.button(text="Изменить число победителей", callback_data=f"setfield_{lot_id}_winners_count")
    kb.button(text="🛑 Принудительный финиш", callback_data=f"setfield_{lot_id}_stop")
    kb.button(text="🔙 Назад", callback_data="adm_edit_list")
    await c.message.edit_text(f"Настройки розыгрыша #{lot_id}:", reply_markup=kb.adjust(1).as_markup())

@dp.callback_query(F.data.startswith("setfield_"))
async def adm_edit_field(c: CallbackQuery, state: FSMContext):
    _, lot_id, field = c.data.split("_")
    if field == "stop":
        await finish_giveaway(int(lot_id))
        return await c.message.answer(f"✅ Розыгрыш #{lot_id} завершен!")
    
    await state.update_data(edit_lid=lot_id, edit_field=field)
    await state.set_state(EditLot.new_value)
    await c.message.answer(f"Введите новое значение для поля {field}:")

@dp.message(EditLot.new_value)
async def adm_edit_save(m: Message, state: FSMContext):
    data = await state.get_data()
    db_query(f"UPDATE lotteries SET {data['edit_field']} = ? WHERE id = ?", 
             (m.text, data['edit_lid']), commit=True)
    await m.answer("✅ Данные обновлены!")
    await state.clear()

@dp.callback_query(F.data == "adm_stats")
async def adm_stats_view(c: CallbackQuery):
    lots = db_query("SELECT * FROM lotteries ORDER BY id DESC LIMIT 10", fetchall=True)
    text = "📊 **Статистика лотов (последние 10):**\n\n"
    for l in lots:
        text += f"🔹 #{l['id']} | {l['status']} | 👥 {l['participants_count']} чел. | 🏆 {l['winners_count']} мест\n"
    await c.message.answer(text, parse_mode="Markdown")

# --- 8. ОТЗЫВЫ И PR ---
@dp.callback_query(F.data == "view_reviews")
async def view_reviews(c: CallbackQuery):
    revs = db_query("SELECT r.*, u.full_name FROM reviews r JOIN users u ON r.user_id = u.user_id ORDER BY r.id DESC LIMIT 10", fetchall=True)
    if not revs:
        return await c.answer("Отзывов пока нет!", show_alert=True)
    
    text = "💬 **Отзывы наших победителей:**\n\n"
    for r in revs:
        text += f"👤 {r['full_name']} (Лот #{r['lot_id']}):\n«{r['text']}»\n{'-'*20}\n"
    await c.message.answer(text, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("rev_"))
async def leave_review_start(c: CallbackQuery, state: FSMContext):
    lot_id = c.data.split("_")[1]
    # Проверка на победителя
    win = db_query("SELECT * FROM winners WHERE lot_id=? AND user_id=?", (lot_id, c.from_user.id), fetchone=True)
    if not win:
        return await c.answer("❌ Вы не являетесь победителем этого розыгрыша!", show_alert=True)
    
    await state.update_data(rev_lid=lot_id)
    await state.set_state(LeaveReview.text)
    await c.message.answer("Пожалуйста, напишите ваш отзыв:")

@dp.message(LeaveReview.text)
async def leave_review_save(m: Message, state: FSMContext):
    data = await state.get_data()
    db_query("INSERT INTO reviews (user_id, lot_id, text) VALUES (?,?,?)", 
             (m.from_user.id, data['rev_lid'], m.text), commit=True)
    await m.answer("✅ Спасибо! Ваш отзыв очень важен для нас.")
    await state.clear()

@dp.callback_query(F.data == "apply_pr")
async def pr_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(PRApplication.age)
    await c.message.answer("Заполнение анкеты PR.\n1. Ваш возраст?")

@dp.message(PRApplication.age)
async def pr_age(m: Message, state: FSMContext):
    await state.update_data(pr_age=m.text)
    await state.set_state(PRApplication.nickname)
    await m.answer("2. Ваш никнейм или ссылка на ресурс?")

@dp.message(PRApplication.nickname)
async def pr_nick(m: Message, state: FSMContext):
    await state.update_data(pr_nick=m.text)
    await state.set_state(PRApplication.proofs)
    await m.answer("3. Отправьте скриншот вашей статистики:")

@dp.message(PRApplication.proofs, F.photo)
async def pr_final(m: Message, state: FSMContext):
    data = await state.get_data()
    msg = (f"📩 **НОВАЯ ЗАЯВКА НА PR!**\n\n"
           f"👤 От: @{m.from_user.username}\n"
           f"🔞 Возраст: {data['pr_age']}\n"
           f"🔗 Линк: {data['pr_nick']}")
    
    if PR_CHAT_ID:
        await bot.send_photo(PR_CHAT_ID, m.photo[-1].file_id, caption=msg)
    await m.answer("✅ Заявка отправлена администраторам!")
    await state.clear()

@dp.callback_query(F.data == "start_back")
async def back_to_start(c: CallbackQuery, state: FSMContext):
    await cmd_start(c.message, CommandObject(command="start", args=None), state)
    await c.message.delete()

# --- 9. ПЛАНИРОВЩИК (ПРОВЕРКА ВРЕМЕНИ) ---
async def scheduler():
    while True:
        try:
            active_lots = db_query("SELECT * FROM lotteries WHERE status='active' AND finish_type='time'", fetchall=True)
            now = datetime.now()
            for lot in active_lots:
                try:
                    f_time = datetime.strptime(lot['finish_value'], "%d.%m.%Y %H:%M")
                    if now >= f_time:
                        await finish_giveaway(lot['id'])
                except: continue
        except Exception as e:
            logger.error(f"Ошибка в планировщике: {e}")
        await asyncio.sleep(60)

# --- 10. ЗАПУСК ---
async def main():
    init_db()
    asyncio.create_task(scheduler())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
