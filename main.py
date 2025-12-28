import os
import random
import sqlite3
import logging
import json
import asyncio
import sys
from datetime import datetime, timedelta
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    Message, 
    CallbackQuery, 
    ContentType
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# =================================================================
# 1. СИСТЕМНЫЕ НАСТРОЙКИ
# =================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("LotteryEngine_PRO")

load_dotenv()
# Токен из .env файла согласно вашему запросу
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
PR_CHAT_ID = int(os.getenv("PR_CHAT_ID", "0"))
LOT_CHANNEL = os.getenv("LOT_CHANNEL", "@lotsvitechek")

if not TOKEN:
    logger.critical("!!! BOT_TOKEN NOT FOUND IN .ENV !!!")
    sys.exit(1)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# =================================================================
# 2. АРХИТЕКТУРА БАЗЫ ДАННЫХ
# =================================================================
def init_db():
    """Создание и проверка структуры таблиц"""
    logger.info("Initializing Database...")
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    
    # Розыгрыши
    cur.execute("""
    CREATE TABLE IF NOT EXISTS lotteries (
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
    
    # Участники
    cur.execute("""
    CREATE TABLE IF NOT EXISTS participants (
        user_id INTEGER,
        lot_id INTEGER,
        username TEXT,
        full_name TEXT,
        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, lot_id)
    )""")
    
    # Победители
    cur.execute("""
    CREATE TABLE IF NOT EXISTS winners (
        lot_id INTEGER,
        user_id INTEGER,
        win_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    
    # Реферальная система и юзеры
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        referrer_id INTEGER DEFAULT 0,
        refs_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    
    # Отзывы
    cur.execute("""
    CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        lot_id INTEGER,
        text TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # Миграции для старых БД
    cur.execute("PRAGMA table_info(users)")
    cols = [c[1] for c in cur.fetchall()]
    if 'referrer_id' not in cols: cur.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER DEFAULT 0")
    if 'refs_count' not in cols: cur.execute("ALTER TABLE users ADD COLUMN refs_count INTEGER DEFAULT 0")

    conn.commit()
    conn.close()
    logger.info("Database Synchronization Complete.")

def db_query(query, params=(), fetchone=False, fetchall=False, commit=False):
    """Метод для безопасных транзакций"""
    try:
        with sqlite3.connect("bot_database.db") as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(query, params)
            if commit: conn.commit()
            if fetchone: return cur.fetchone()
            if fetchall: return cur.fetchall()
            return cur.lastrowid
    except Exception as e:
        logger.error(f"SQL Error: {e} | Query: {query}")
        return None

# =================================================================
# 3. СОСТОЯНИЯ (FSM)
# =================================================================
class CreateLot(StatesGroup):
    text = State()
    winners_count = State()
    channels = State()
    finish_type = State()
    value = State()

class EditLotState(StatesGroup):
    lot_id = State()
    field_to_edit = State()
    new_value = State()

class PRApplication(StatesGroup):
    age = State()
    nickname = State()
    proofs = State()

class LeaveReview(StatesGroup):
    lot_id = State()
    text = State()

class BroadcastState(StatesGroup):
    content = State()

class AdminSearch(StatesGroup):
    query = State()

# =================================================================
# 4. ЛОГИКА ПРОВЕРКИ И ФИНАЛИЗАЦИИ
# =================================================================
async def check_user_sub(user_id: int, channels_str: str):
    """Проверка подписки на обязательный список каналов"""
    if not channels_str or channels_str.lower() in ['нет', 'none', '']:
        return True, []
    
    not_subscribed = []
    channels_list = [c.strip() for c in channels_str.split(",") if c.strip()]
    
    for channel in channels_list:
        try:
            member = await bot.get_chat_member(channel, user_id)
            if member.status in ["left", "kicked"]:
                not_subscribed.append(channel)
        except Exception:
            not_subscribed.append(channel)
            
    return len(not_subscribed) == 0, not_subscribed

async def update_lot_card(lot_id: int, count: int):
    """Динамическое обновление кнопки в основном канале"""
    lot = db_query("SELECT message_id FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or not lot['message_id']:
        return

    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"✅ Участвовать! ({count})", 
        url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}"
    )
    
    try:
        await bot.edit_message_reply_markup(
            chat_id=LOT_CHANNEL, 
            message_id=lot['message_id'], 
            reply_markup=kb.as_markup()
        )
    except Exception as e:
        logger.debug(f"Card update skip: {e}")

async def run_final_selection(lot_id: int):
    """Механика завершения розыгрыша"""
    logger.info(f"Finalizing Lottery #{lot_id}")
    lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or lot['status'] == 'closed':
        return
    
    participants = db_query("SELECT * FROM participants WHERE lot_id = ?", (lot_id,), fetchall=True)
    db_query("UPDATE lotteries SET status = 'closed' WHERE id = ?", (lot_id,), commit=True)
    
    if not participants:
        await bot.send_message(LOT_CHANNEL, f"⚠️ Лотерея #{lot_id} завершена, но участников не оказалось.")
        return

    # Выборка
    win_count = min(len(participants), lot['winners_count'])
    winners_list = random.sample(participants, win_count)
    
    mentions = []
    for winner in winners_list:
        db_query("INSERT INTO winners (lot_id, user_id) VALUES (?,?)", (lot_id, winner['user_id']), commit=True)
        mention = f"@{winner['username']}" if winner['username'] else f"[{winner['full_name']}](tg://user?id={winner['user_id']})"
        mentions.append(mention)
        
        try:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✍️ Написать отзыв", callback_data=f"rev_{lot_id}")]
            ])
            await bot.send_message(
                winner['user_id'], 
                f"🎉 ВЫ ПОБЕДИЛИ! Розыгрыш #{lot_id} завершен удачно для вас!", 
                reply_markup=kb
            )
        except Exception:
            pass

    result_text = (
        f"🎊 **ИТОГИ РОЗЫГРЫША #{lot_id}**\n\n"
        f"🏆 Победители: {', '.join(mentions)}\n"
        f"📊 Участников: {len(participants)}\n\n"
        f"Победители, бот отправил вам инструкции в ЛС!"
    )
    
    try:
        await bot.send_message(LOT_CHANNEL, result_text, parse_mode="Markdown", reply_to_message_id=lot['message_id'])
    except Exception:
        await bot.send_message(LOT_CHANNEL, result_text, parse_mode="Markdown")

# =================================================================
# 5. ОБРАБОТКА ВХОДА (РЕФЕРАЛЫ + УЧАСТИЕ)
# =================================================================
@dp.message(Command("start"))
async def cmd_start_handler(message: Message, command: CommandObject, state: FSMContext):
    uid = message.from_user.id
    args = command.args
    
    # 5.1 Регистрация и Реферальная система
    user = db_query("SELECT * FROM users WHERE user_id = ?", (uid,), fetchone=True)
    if not user:
        ref_id = 0
        if args and args.startswith("ref"):
            try:
                ref_id = int(args.replace("ref", ""))
                if ref_id == uid: ref_id = 0
            except:
                ref_id = 0
        
        db_query(
            "INSERT INTO users (user_id, username, full_name, referrer_id) VALUES (?,?,?,?)",
            (uid, message.from_user.username, message.from_user.full_name, ref_id),
            commit=True
        )
        
        if ref_id != 0:
            db_query("UPDATE users SET refs_count = refs_count + 1 WHERE user_id = ?", (ref_id,), commit=True)
            inviter = db_query("SELECT * FROM users WHERE user_id = ?", (ref_id,), fetchone=True)
            
            await message.answer(f"👋 Привет! Вы пришли от пользователя: **{inviter['full_name'] if inviter else 'Друга'}**")
            try:
                await bot.send_message(ref_id, f"🤝 У вас новый реферал: **{message.from_user.full_name}**")
            except:
                pass
            
            if PR_CHAT_ID:
                pr_msg = (f"📈 **НОВЫЙ РЕФЕРАЛ**\n\n👤 Партнер: {inviter['full_name']} (ID: `{ref_id}`)\n"
                          f"🆕 Пользователь: {message.from_user.full_name}\n📊 Итого рефов: **{inviter['refs_count'] + 1}**")
                await bot.send_message(PR_CHAT_ID, pr_msg, parse_mode="Markdown")

    # 5.2 Участие в лоте
    if args and args.startswith("lot_"):
        try:
            lot_id = int(args.split("_")[1])
            lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
            
            if not lot or lot['status'] == 'closed':
                return await message.answer("❌ Данный розыгрыш уже завершен.")
            
            # ПРОВЕРКА НА ПОВТОРНОЕ УЧАСТИЕ
            exists = db_query("SELECT * FROM participants WHERE user_id = ? AND lot_id = ?", (uid, lot_id), fetchone=True)
            if exists:
                return await message.answer(f"⚠️ Вы уже в списке участников лотереи #{lot_id}. Ждите итогов!")

            # Обязательная подписка
            ok, channels = await check_user_sub(uid, lot['channels'])
            if not ok:
                kb = InlineKeyboardBuilder()
                for c in channels:
                    kb.button(text=f"📢 Канал {c}", url=f"https://t.me/{c.lstrip('@')}")
                kb.button(text="🔄 Проверить подписку", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")
                return await message.answer("⚠️ Вы не выполнили условия подписки:", reply_markup=kb.adjust(1).as_markup())

            # Регистрация
            db_query(
                "INSERT INTO participants (user_id, lot_id, username, full_name) VALUES (?,?,?,?)",
                (uid, lot_id, message.from_user.username, message.from_user.full_name),
                commit=True
            )
            
            # Статистика
            count_res = db_query("SELECT COUNT(*) as c FROM participants WHERE lot_id = ?", (lot_id,), fetchone=True)
            current_p = count_res['c']
            db_query("UPDATE lotteries SET participants_count = ? WHERE id = ?", (current_p, lot_id), commit=True)
            
            await update_lot_card(lot_id, current_p)
            
            # Проверка финиша по количеству
            if lot['finish_type'] == 'count' and current_p >= int(lot['finish_value']):
                await run_final_selection(lot_id)
            
            return await message.answer(f"✅ ВЫ В ИГРЕ! Ваш номер участия в лоте #{lot_id} успешно зафиксирован.")
        except Exception as e:
            logger.error(f"Join error: {e}")

    # Главное Меню
    kb = [
        [InlineKeyboardButton(text="💬 Читать отзывы", callback_data="view_reviews"), 
         InlineKeyboardButton(text="💼 Стать партнером", callback_data="apply_pr")],
        [InlineKeyboardButton(text="📢 Розыгрыши", callback_data="active_lots"), 
         InlineKeyboardButton(text="📊 Мой кабинет", callback_data="my_stats")]
    ]
    if uid in ADMIN_IDS:
        kb.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_main")])
    
    await message.answer(
        f"👋 Приветствую, {message.from_user.first_name}!\nЯ бот для проведения честных розыгрышей.", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )

# =================================================================
# 6. ЛИЧНЫЙ КАБИНЕТ
# =================================================================
@dp.callback_query(F.data == "my_stats")
async def profile_handler(c: CallbackQuery):
    uid = c.from_user.id
    user = db_query("SELECT * FROM users WHERE user_id = ?", (uid,), fetchone=True)
    p_count = db_query("SELECT COUNT(*) as c FROM participants WHERE user_id = ?", (uid,), fetchone=True)['c']
    w_count = db_query("SELECT COUNT(*) as c FROM winners WHERE user_id = ?", (uid,), fetchone=True)['c']
    
    link = f"https://t.me/{(await bot.get_me()).username}?start=ref{uid}"
    
    msg = (f"👤 **ВАШ ПРОФИЛЬ**\n\n🆔 Твой ID: `{uid}`\n"
           f"🎫 Участий в лотах: **{p_count}**\n"
           f"🏆 Побед: **{w_count}**\n"
           f"👥 Рефералов: **{user['refs_count']}**\n\n"
           f"🔗 **Твоя ссылка для приглашений:**\n`{link}`")
    
    kb = [[InlineKeyboardButton(text="👥 Список моих друзей", callback_data="my_refs_list")],
          [InlineKeyboardButton(text="🔙 В начало", callback_data="to_start")]]
    await c.message.edit_text(msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")

@dp.callback_query(F.data == "my_refs_list")
async def profile_refs_handler(c: CallbackQuery):
    uid = c.from_user.id
    refs = db_query("SELECT full_name, username, created_at FROM users WHERE referrer_id = ? ORDER BY created_at DESC LIMIT 50", (uid,), fetchall=True)
    
    if not refs:
        return await c.answer("У вас пока нет приглашенных друзей.", show_alert=True)
    
    text = "👥 **ВАШИ РЕФЕРАЛЫ:**\n\n"
    for i, r in enumerate(refs, 1):
        name = r['full_name']
        tag = f" (@{r['username']})" if r['username'] else ""
        date = r['created_at'].split()[0]
        text += f"{i}. {name}{tag} — _{date}_\n"
    
    await c.message.answer(text, parse_mode="Markdown")
    await c.answer()

# =================================================================
# 7. АДМИНИСТРИРОВАНИЕ (МЕНЮ И ПОИСК)
# =================================================================
@dp.callback_query(F.data == "admin_main")
async def admin_menu_handler(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS: return
    
    kb = [
        [InlineKeyboardButton(text="➕ Создать лот", callback_data="adm_create")],
        [InlineKeyboardButton(text="📝 Управление лотами", callback_data="adm_manage_lots")],
        [InlineKeyboardButton(text="🔍 Поиск (ID / @Юзер)", callback_data="adm_search_user")],
        [InlineKeyboardButton(text="📩 Глобальная рассылка", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="to_start")]
    ]
    await c.message.edit_text("🛠 **ПАНЕЛЬ АДМИНИСТРАТОРА**", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data == "adm_search_user")
async def admin_search_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminSearch.query)
    await c.message.answer("Введите ID пользователя или его @username для поиска в базе:")

@dp.message(AdminSearch.query)
async def admin_search_process(m: Message, state: FSMContext):
    q = m.text.replace("@", "").strip()
    
    if q.isdigit():
        user = db_query("SELECT * FROM users WHERE user_id = ?", (int(q),), fetchone=True)
    else:
        user = db_query("SELECT * FROM users WHERE username = ?", (q,), fetchone=True)
    
    if not user:
        return await m.answer("❌ Данный пользователь не найден в базе.")
    
    uid = user['user_id']
    p_data = db_query("SELECT COUNT(*) as c FROM participants WHERE user_id = ?", (uid,), fetchone=True)['c']
    w_data = db_query("SELECT COUNT(*) as c FROM winners WHERE user_id = ?", (uid,), fetchone=True)['c']
    refs = db_query("SELECT full_name FROM users WHERE referrer_id = ?", (uid,), fetchall=True)
    
    txt = (f"🔍 **ИНФОРМАЦИЯ О ПОЛЬЗОВАТЕЛЕ**\n\n"
           f"👤 Имя: {user['full_name']}\n"
           f"🔗 Юзер: @{user['username'] if user['username'] else 'None'}\n"
           f"🆔 ID: `{uid}`\n"
           f"🎫 Участий: {p_data} | 🏆 Побед: {w_data}\n"
           f"👥 Рефералов: **{len(refs)}**\n\n"
           f"📋 Список имен рефов: " + (", ".join([r['full_name'] for r in refs]) if refs else "Пуст"))
    
    await m.answer(txt); await state.clear()

# =================================================================
# 8. УПРАВЛЕНИЕ ЛОТАМИ И РЕДАКТИРОВАНИЕ
# =================================================================
@dp.callback_query(F.data == "adm_manage_lots")
async def admin_lots_list(c: CallbackQuery):
    lots = db_query("SELECT * FROM lotteries WHERE status = 'active' ORDER BY id DESC", fetchall=True)
    if not lots:
        return await c.answer("Активных лотерей не найдено.", show_alert=True)
    
    builder = InlineKeyboardBuilder()
    for l in lots:
        builder.button(text=f"Лот #{l['id']} | 👥 {l['participants_count']}", callback_data=f"manage_{l['id']}")
    
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_main"))
    await c.message.edit_text("📝 **ВЫБЕРИТЕ ЛОТ ДЛЯ РЕДАКТИРОВАНИЯ:**", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("manage_"))
async def admin_lot_manage_panel(c: CallbackQuery):
    lid = int(c.data.split("_")[1])
    l = db_query("SELECT * FROM lotteries WHERE id = ?", (lid,), fetchone=True)
    
    text = (f"⚙️ **УПРАВЛЕНИЕ ЛОТОМ #{lid}**\n\n"
            f"🔹 Победителей: **{l['winners_count']}**\n"
            f"🔹 Финиш: {l['finish_type']} ({l['finish_value']})\n"
            f"🔹 Участников: **{l['participants_count']}**\n"
            f"🔹 Каналы: `{l['channels']}`")
    
    kb = [
        [InlineKeyboardButton(text="📋 Список участников", callback_data=f"listp_{lid}")],
        [InlineKeyboardButton(text="🏆 Изменить число победителей", callback_data=f"edit_w_{lid}")],
        [InlineKeyboardButton(text="⏳ Изменить финиш", callback_data=f"edit_f_{lid}")],
        [InlineKeyboardButton(text="🛑 Остановить сейчас", callback_data=f"stop_{lid}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm_manage_lots")]
    ]
    await c.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("listp_"))
async def admin_list_participants(c: CallbackQuery):
    lid = int(c.data.split("_")[1])
    participants = db_query("SELECT full_name, user_id FROM participants WHERE lot_id = ? LIMIT 100", (lid,), fetchall=True)
    
    if not participants:
        return await c.answer("Участников еще нет.", show_alert=True)
    
    msg = f"👥 **Участники лота #{lid}:**\n\n"
    for p in participants:
        msg += f"• {p['full_name']} (`{p['user_id']}`)\n"
    
    await c.message.answer(msg); await c.answer()

@dp.callback_query(F.data.startswith("edit_w_"))
async def admin_edit_winners_start(c: CallbackQuery, state: FSMContext):
    lid = int(c.data.split("_")[3])
    await state.update_data(edit_target=lid, field="winners_count")
    await state.set_state(EditLotState.new_value)
    await c.message.answer(f"Введите НОВОЕ количество победителей для лота #{lid}:")

@dp.callback_query(F.data.startswith("edit_f_"))
async def admin_edit_finish_start(c: CallbackQuery, state: FSMContext):
    lid = int(c.data.split("_")[2])
    l = db_query("SELECT finish_type FROM lotteries WHERE id = ?", (lid,), fetchone=True)
    await state.update_data(edit_target=lid, field="finish_value", f_type=l['finish_type'])
    await state.set_state(EditLotState.new_value)
    
    prompt = "Введите число участников для финиша:" if l['finish_type'] == 'count' else "Введите через сколько ЧАСОВ финишировать (от текущего момента):"
    await c.message.answer(prompt)

@dp.message(EditLotState.new_value)
async def admin_edit_value_save(m: Message, state: FSMContext):
    if not m.text.isdigit():
        return await m.answer("Пожалуйста, введите целое число.")
    
    data = await state.get_data()
    lid = data['edit_target']
    field = data['field']
    
    final_val = m.text
    if field == "finish_value" and data.get('f_type') == 'time':
        final_val = (datetime.now() + timedelta(hours=int(m.text))).strftime("%d.%m.%Y %H:%M")
    
    db_query(f"UPDATE lotteries SET {field} = ? WHERE id = ?", (final_val, lid), commit=True)
    await m.answer(f"✅ Данные лота #{lid} успешно обновлены!"); await state.clear()

@dp.callback_query(F.data.startswith("stop_"))
async def admin_force_stop(c: CallbackQuery):
    lid = int(c.data.split("_")[1])
    await run_final_selection(lid)
    await c.answer("Лотерея завершена принудительно!", show_alert=True)
    await admin_lots_list(c)

# =================================================================
# 9. ПОШАГОВОЕ СОЗДАНИЕ ЛОТА
# =================================================================
@dp.callback_query(F.data == "adm_create")
async def create_step_1(c: CallbackQuery, state: FSMContext):
    await state.set_state(CreateLot.text)
    await c.message.answer("Шаг 1: Отправьте пост (Текст, Фото с подписью или Стикер):")

@dp.message(CreateLot.text)
async def create_step_2(m: Message, state: FSMContext):
    ents = json.dumps([e.model_dump(mode='json') for e in (m.entities or m.caption_entities or [])])
    payload = {
        "text": m.caption or m.text or "",
        "entities": ents,
        "photo": m.photo[-1].file_id if m.photo else None,
        "sticker": m.sticker.file_id if m.sticker else None
    }
    await state.update_data(post_data=payload)
    await state.set_state(CreateLot.winners_count)
    await m.answer("Шаг 2: Сколько будет победителей?")

@dp.message(CreateLot.winners_count)
async def create_step_3(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("Нужно число.")
    await state.update_data(wc=int(m.text))
    await state.set_state(CreateLot.channels)
    await m.answer("Шаг 3: Каналы для ОП через запятую (@ch1, @ch2) или 'нет':")

@dp.message(CreateLot.channels)
async def create_step_4(m: Message, state: FSMContext):
    await state.update_data(ch_list=m.text)
    kb = [[InlineKeyboardButton(text="⏳ По времени", callback_data="finish_t"), 
           InlineKeyboardButton(text="👥 По количеству", callback_data="finish_c")]]
    await m.answer("Шаг 4: Выберите тип завершения:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("finish_"))
async def create_step_5(c: CallbackQuery, state: FSMContext):
    ft = "time" if c.data == "finish_t" else "count"
    await state.update_data(ftype=ft)
    prompt = "Через сколько ЧАСОВ финиш?" if ft == "time" else "При каком КОЛИЧЕСТВЕ людей финиш?"
    await c.message.answer(prompt)
    await state.set_state(CreateLot.value)

@dp.message(CreateLot.value)
async def create_step_final(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("Нужно число.")
    s_data = await state.get_data()
    post = s_data['post_data']
    
    val = (datetime.now() + timedelta(hours=int(m.text))).strftime("%d.%m.%Y %H:%M") if s_data['ftype'] == 'time' else m.text
    
    lid = db_query(
        "INSERT INTO lotteries (text, entities, channels, finish_type, finish_value, photo, sticker, winners_count) VALUES (?,?,?,?,?,?,?,?)",
        (post['text'], post['entities'], s_data['ch_list'], s_data['ftype'], val, post['photo'], post['sticker'], s_data['wc']),
        commit=True
    )
    
    # Публикация
    kb = InlineKeyboardBuilder().button(text="✅ Участвовать! (0)", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lid}")
    if post['photo']:
        sent = await bot.send_photo(LOT_CHANNEL, post['photo'], caption=post['text'], reply_markup=kb.as_markup())
    else:
        sent = await bot.send_message(LOT_CHANNEL, post['text'], reply_markup=kb.as_markup())
    
    db_query("UPDATE lotteries SET message_id = ? WHERE id = ?", (sent.message_id, lid), commit=True)
    await m.answer(f"✅ Лотерея #{lid} успешно создана и запущена!"); await state.clear()

# =================================================================
# 10. ГЛОБАЛЬНЫЕ СЕРВИСЫ (РАССЫЛКА, ТАЙМЕРЫ)
# =================================================================
@dp.callback_query(F.data == "adm_broadcast")
async def broadcast_init(c: CallbackQuery, state: FSMContext):
    await state.set_state(BroadcastState.content)
    await c.message.answer("Введите сообщение (текст/фото) для рассылки всем юзерам:")

@dp.message(BroadcastState.content)
async def broadcast_execute(m: Message, state: FSMContext):
    targets = db_query("SELECT user_id FROM users", fetchall=True)
    await m.answer(f"🚀 Запуск рассылки на {len(targets)} пользователей..."); count = 0
    for t in targets:
        try:
            await m.copy_to(t['user_id'])
            count += 1
            await asyncio.sleep(0.05)
        except: continue
    await m.answer(f"🏁 Рассылка окончена. Получили: {count}"); await state.clear()

async def time_checker():
    """Фоновый поток для проверки времени финиша"""
    while True:
        try:
            active_lots = db_query("SELECT * FROM lotteries WHERE status='active' AND finish_type='time'", fetchall=True)
            for lot in active_lots:
                if datetime.now() >= datetime.strptime(lot['finish_value'], "%d.%m.%Y %H:%M"):
                    await run_final_selection(lot['id'])
        except Exception as e:
            logger.error(f"Timer error: {e}")
        await asyncio.sleep(60)

async def main():
    init_db()
    asyncio.create_task(time_checker())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except: pass
