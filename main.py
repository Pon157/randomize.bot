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
# 1. НАСТРОЙКИ И ЛОГИРОВАНИЕ
# =================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("LotterySystemV3")

load_dotenv()
# Токен берется из .env, как ты и просил в сохраненных инструкциях
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
PR_CHAT_ID = int(os.getenv("PR_CHAT_ID", "0"))
LOT_CHANNEL = os.getenv("LOT_CHANNEL", "@lotsvitechek")

if not TOKEN:
    logger.error("ОШИБКА: BOT_TOKEN не найден в .env файле!")
    sys.exit(1)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# =================================================================
# 2. БАЗА ДАННЫХ И МИГРАЦИИ
# =================================================================
def init_db():
    """Инициализация структуры базы данных"""
    logger.info("Проверка и инициализация базы данных...")
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    
    # Таблица розыгрышей
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
    
    # Таблица участников
    cur.execute("""
    CREATE TABLE IF NOT EXISTS participants (
        user_id INTEGER,
        lot_id INTEGER,
        username TEXT,
        full_name TEXT,
        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, lot_id)
    )""")
    
    # Таблица победителей
    cur.execute("""
    CREATE TABLE IF NOT EXISTS winners (
        lot_id INTEGER,
        user_id INTEGER,
        win_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    
    # Таблица отзывов
    cur.execute("""
    CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        lot_id INTEGER,
        text TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    
    # Таблица пользователей (Реферальная система)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        referrer_id INTEGER DEFAULT 0,
        refs_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # ПРОВЕРКА МИГРАЦИЙ (если база старая)
    cur.execute("PRAGMA table_info(users)")
    u_cols = [c[1] for c in cur.fetchall()]
    if 'referrer_id' not in u_cols:
        logger.info("Миграция: Добавление referrer_id в таблицу users")
        cur.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER DEFAULT 0")
    if 'refs_count' not in u_cols:
        logger.info("Миграция: Добавление refs_count в таблицу users")
        cur.execute("ALTER TABLE users ADD COLUMN refs_count INTEGER DEFAULT 0")

    conn.commit()
    conn.close()
    logger.info("База данных готова.")

def db_query(query, params=(), fetchone=False, fetchall=False, commit=False):
    """Единый интерфейс для работы с БД"""
    try:
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
    except Exception as e:
        logger.error(f"DATABASE ERROR: {e} | Query: {query}")
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
    user_id = State()

# =================================================================
# 4. ЛОГИКА РОЗЫГРЫШЕЙ
# =================================================================
async def check_user_subscription(user_id: int, channels_str: str):
    """Проверка подписки на каналы"""
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

async def update_button_count(lot_id: int, current_count: int):
    """Обновление счетчика на кнопке в канале"""
    lot = db_query("SELECT message_id FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or not lot['message_id']:
        return

    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"✅ Участвовать! ({current_count})", 
        url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}"
    )
    
    try:
        await bot.edit_message_reply_markup(
            chat_id=LOT_CHANNEL, 
            message_id=lot['message_id'], 
            reply_markup=kb.as_markup()
        )
    except Exception as e:
        logger.debug(f"Кнопка не обновилась (возможно нет изменений): {e}")

async def finalize_giveaway(lot_id: int):
    """Завершение розыгрыша и выбор победителей"""
    lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or lot['status'] == 'closed':
        return
    
    participants = db_query("SELECT * FROM participants WHERE lot_id = ?", (lot_id,), fetchall=True)
    db_query("UPDATE lotteries SET status = 'closed' WHERE id = ?", (lot_id,), commit=True)
    
    if not participants:
        await bot.send_message(LOT_CHANNEL, f"🔔 Розыгрыш #{lot_id} завершен, но участников не было.")
        return

    # Выбор случайных победителей
    count_to_pick = min(len(participants), lot['winners_count'])
    winners = random.sample(participants, count_to_pick)
    
    winner_mentions = []
    for winner in winners:
        db_query("INSERT INTO winners (lot_id, user_id) VALUES (?,?)", (lot_id, winner['user_id']), commit=True)
        mention = f"@{winner['username']}" if winner['username'] else f"[{winner['full_name']}](tg://user?id={winner['user_id']})"
        winner_mentions.append(mention)
        
        try:
            # Кнопка для отзыва победителя
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✍️ Оставить отзыв", callback_data=f"rev_{lot_id}")]
            ])
            await bot.send_message(
                winner['user_id'], 
                f"🎉 Поздравляем! Вы стали победителем в розыгрыше #{lot_id}!", 
                reply_markup=kb
            )
        except Exception:
            pass

    result_msg = (
        f"🎊 **ИТОГИ РОЗЫГРЫША #{lot_id}**\n\n"
        f"🏆 Победители: {', '.join(winner_mentions)}\n"
        f"📊 Всего участников: {len(participants)}\n\n"
        f"Победители, бот отписал вам в ЛС!"
    )
    
    try:
        await bot.send_message(LOT_CHANNEL, result_msg, parse_mode="Markdown", reply_to_message_id=lot['message_id'])
    except Exception:
        await bot.send_message(LOT_CHANNEL, result_msg, parse_mode="Markdown")

# =================================================================
# 5. КОМАНДА START И РЕФЕРАЛЬНЫЙ МОДУЛЬ
# =================================================================
@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    uid = message.from_user.id
    args = command.args
    logger.info(f"User {uid} started bot with args: {args}")
    
    # Регистрация нового пользователя и реферальная логика
    user_data = db_query("SELECT * FROM users WHERE user_id = ?", (uid,), fetchone=True)
    if not user_data:
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
            # Обновляем счетчик пригласившего
            db_query("UPDATE users SET refs_count = refs_count + 1 WHERE user_id = ?", (ref_id,), commit=True)
            inviter = db_query("SELECT * FROM users WHERE user_id = ?", (ref_id,), fetchone=True)
            
            # 1. Сообщение новому пользователю
            inviter_name = inviter['full_name'] if inviter else "Партнер"
            await message.answer(f"🤝 Привет! Вы приглашены пользователем **{inviter_name}**")
            
            # 2. Сообщение пригласившему
            try:
                await bot.send_message(ref_id, f"🤝 По вашей ссылке зарегистрировался новый пользователь: **{message.from_user.full_name}**")
            except:
                pass
            
            # 3. Отчет в PR чат
            if PR_CHAT_ID:
                pr_log = (
                    f"📈 **НОВЫЙ РЕФЕРАЛ**\n\n"
                    f"👤 Партнер: {inviter['full_name']} (ID: `{ref_id}`)\n"
                    f"🆕 Игрок: {message.from_user.full_name} (ID: `{uid}`)\n"
                    f"📊 Всего рефералов у партнера: **{inviter['refs_count'] + 1}**"
                )
                try:
                    await bot.send_message(PR_CHAT_ID, pr_log, parse_mode="Markdown")
                except:
                    pass

    # Обработка входа для участия в лотерее
    if args and args.startswith("lot_"):
        try:
            lot_id = int(args.split("_")[1])
            lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
            
            if not lot or lot['status'] == 'closed':
                return await message.answer("❌ Этот розыгрыш уже завершен.")
            
            # Проверка подписки
            sub_ok, channels = await check_user_subscription(uid, lot['channels'])
            if not sub_ok:
                builder = InlineKeyboardBuilder()
                for ch in channels:
                    builder.button(text=f"📢 Подписаться на {ch}", url=f"https://t.me/{ch.lstrip('@')}")
                builder.button(text="🔄 Я подписался!", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")
                return await message.answer(
                    "⚠️ Для участия необходимо подписаться на наши каналы:", 
                    reply_markup=builder.adjust(1).as_markup()
                )

            # Регистрация участия
            try:
                db_query(
                    "INSERT INTO participants (user_id, lot_id, username, full_name) VALUES (?,?,?,?)",
                    (uid, lot_id, message.from_user.username, message.from_user.full_name),
                    commit=True
                )
                # Обновляем счетчик
                res = db_query("SELECT COUNT(*) as cnt FROM participants WHERE lot_id = ?", (lot_id,), fetchone=True)
                new_count = res['cnt']
                db_query("UPDATE lotteries SET participants_count = ? WHERE id = ?", (new_count, lot_id), commit=True)
                
                await update_button_count(lot_id, new_count)
                
                # Проверка финиша по количеству
                if lot['finish_type'] == 'count' and new_count >= int(lot['finish_value']):
                    await finalize_giveaway(lot_id)
                
                await message.answer(f"✅ Вы успешно зарегистрированы в лотерее #{lot_id}!")
            except sqlite3.IntegrityError:
                await message.answer("✅ Вы уже участвуете в этой лотерее!")
            return
        except Exception as e:
            logger.error(f"Ошибка при входе в лот: {e}")

    # Обычное главное меню
    kb = [
        [InlineKeyboardButton(text="💬 Отзывы", callback_data="view_reviews"), 
         InlineKeyboardButton(text="💼 PR Заявка", callback_data="apply_pr")],
        [InlineKeyboardButton(text="📢 Розыгрыши", callback_data="active_lots"), 
         InlineKeyboardButton(text="📊 Мой профиль", callback_data="my_stats")]
    ]
    if uid in ADMIN_IDS:
        kb.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_main")])
    
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\nРады видеть тебя в нашем боте розыгрышей.", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )

# =================================================================
# 6. ЛИЧНЫЙ КАБИНЕТ И СПИСОК РЕФЕРАЛОВ
# =================================================================
@dp.callback_query(F.data == "my_stats")
async def process_my_stats(c: CallbackQuery):
    uid = c.from_user.id
    user = db_query("SELECT * FROM users WHERE user_id = ?", (uid,), fetchone=True)
    
    # Статистика из БД
    participations = db_query("SELECT COUNT(*) as c FROM participants WHERE user_id = ?", (uid,), fetchone=True)['c']
    wins = db_query("SELECT COUNT(*) as c FROM winners WHERE user_id = ?", (uid,), fetchone=True)['c']
    
    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start=ref{uid}"
    
    text = (
        f"👤 **ВАШ ПРОФИЛЬ**\n\n"
        f"🆔 Твой Telegram ID: `{uid}`\n"
        f"🎟 Участий в лотереях: **{participations}**\n"
        f"🏆 Количество побед: **{wins}**\n"
        f"👥 Приглашено друзей: **{user['refs_count'] if user else 0}**\n\n"
        f"🔗 **Твоя реферальная ссылка:**\n`{ref_link}`"
    )
    
    kb = [
        [InlineKeyboardButton(text="👥 Список моих рефералов", callback_data="my_refs_list")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="to_start")]
    ]
    await c.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")

@dp.callback_query(F.data == "my_refs_list")
async def process_my_refs_list(c: CallbackQuery):
    uid = c.from_user.id
    refs = db_query(
        "SELECT full_name, username, created_at FROM users WHERE referrer_id = ? ORDER BY created_at DESC LIMIT 30", 
        (uid,), fetchall=True
    )
    
    if not refs:
        return await c.answer("У вас пока нет рефералов. Пригласите друзей!", show_alert=True)
    
    msg = "👥 **ВАШИ РЕФЕРАЛЫ (последние 30):**\n\n"
    for i, r in enumerate(refs, 1):
        name = r['full_name']
        username = f" (@{r['username']})" if r['username'] else ""
        date = r['created_at'].split()[0]
        msg += f"{i}. {name}{username} — _{date}_\n"
    
    await c.message.answer(msg, parse_mode="Markdown")
    await c.answer()

# =================================================================
# 7. АДМИНИСТРАТИВНЫЙ МОДУЛЬ (ПОИСК, ЛОТЫ, РАССЫЛКА)
# =================================================================
@dp.callback_query(F.data == "admin_main")
async def process_admin_menu(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("⛔ Доступ только для администраторов!", show_alert=True)
        
    kb = [
        [InlineKeyboardButton(text="➕ Создать новый лот", callback_data="adm_create")],
        [InlineKeyboardButton(text="📩 Сделать рассылку", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="🔍 Информация о юзере", callback_data="adm_search_user")],
        [InlineKeyboardButton(text="📊 Общая статистика", callback_data="adm_global_stats")],
        [InlineKeyboardButton(text="🔙 Выход в меню", callback_data="to_start")]
    ]
    await c.message.edit_text("🛠 **ПАНЕЛЬ УПРАВЛЕНИЯ**", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data == "adm_global_stats")
async def process_global_stats(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS: return
    
    total_users = db_query("SELECT COUNT(*) as c FROM users", fetchone=True)['c']
    active_lots = db_query("SELECT COUNT(*) as c FROM lotteries WHERE status='active'", fetchone=True)['c']
    total_reviews = db_query("SELECT COUNT(*) as c FROM reviews", fetchone=True)['c']
    
    txt = (
        f"📊 **ГЛОБАЛЬНАЯ СТАТИСТИКА**\n\n"
        f"👤 Всего пользователей в БД: **{total_users}**\n"
        f"📢 Активных розыгрышей: **{active_lots}**\n"
        f"💬 Всего отзывов: **{total_reviews}**"
    )
    await c.message.answer(txt, parse_mode="Markdown")
    await c.answer()

@dp.callback_query(F.data == "adm_search_user")
async def process_adm_search_user(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS: return
    await state.set_state(AdminSearch.user_id)
    await c.message.answer("Введите Telegram ID пользователя для детальной проверки:")

@dp.message(AdminSearch.user_id)
async def process_adm_search_id(m: Message, state: FSMContext):
    if not m.text.isdigit():
        return await m.answer("ID должен состоять только из цифр!")
        
    target_id = int(m.text)
    user = db_query("SELECT * FROM users WHERE user_id = ?", (target_id,), fetchone=True)
    
    if not user:
        return await m.answer("❌ Пользователь не найден в базе данных.")
    
    # Сбор данных
    p_count = db_query("SELECT COUNT(*) as c FROM participants WHERE user_id = ?", (target_id,), fetchone=True)['c']
    w_count = db_query("SELECT COUNT(*) as c FROM winners WHERE user_id = ?", (target_id,), fetchone=True)['c']
    refs = db_query("SELECT full_name, user_id FROM users WHERE referrer_id = ?", (target_id,), fetchall=True)
    
    report = (
        f"🔍 **ОТЧЕТ ПО ПОЛЬЗОВАТЕЛЮ `{target_id}`**\n\n"
        f"👤 Имя: {user['full_name']}\n"
        f"🔗 Юзер: @{user['username'] if user['username'] else 'нет'}\n"
        f"📅 В базе с: {user['created_at']}\n"
        f"🎟 Участий: {p_count} | 🏆 Побед: {w_count}\n"
        f"👥 Пригласил рефералов: **{len(refs)}**\n\n"
        f"📋 **Список рефералов:**\n"
    )
    
    if refs:
        report += ", ".join([r['full_name'] for r in refs])
    else:
        report += "Список пуст."
        
    await m.answer(report)
    await state.clear()

@dp.callback_query(F.data == "adm_broadcast")
async def process_broadcast_start(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS: return
    await state.set_state(BroadcastState.content)
    await c.message.answer("📣 Отправьте сообщение (текст/фото/видео), которое нужно разослать всем:")

@dp.message(BroadcastState.content)
async def process_broadcast_send(m: Message, state: FSMContext):
    all_users = db_query("SELECT user_id FROM users", fetchall=True)
    await m.answer(f"🚀 Начинаю рассылку на {len(all_users)} пользователей...")
    
    count_ok = 0
    count_err = 0
    
    for user in all_users:
        try:
            await m.copy_to(user['user_id'])
            count_ok += 1
            await asyncio.sleep(0.05) # Плавность
        except:
            count_err += 1
            
    await m.answer(f"🏁 Рассылка завершена!\n✅ Успешно: {count_ok}\n❌ Ошибок: {count_err}")
    await state.clear()

# =================================================================
# 8. СОЗДАНИЕ РОЗЫГРЫША (ПОШАГОВО)
# =================================================================
@dp.callback_query(F.data == "adm_create")
async def adm_create_lot_1(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS: return
    await state.set_state(CreateLot.text)
    await c.message.answer("Шаг 1: Пришлите содержимое поста (текст, фото с описанием или стикер):")

@dp.message(CreateLot.text)
async def adm_create_lot_2(m: Message, state: FSMContext):
    entities_json = "[]"
    if m.entities:
        entities_json = json.dumps([e.model_dump(mode='json') for e in m.entities])
    elif m.caption_entities:
        entities_json = json.dumps([e.model_dump(mode='json') for e in m.caption_entities])

    data = {
        "text": m.caption or m.text or "",
        "entities": entities_json,
        "photo": m.photo[-1].file_id if m.photo else None,
        "sticker": m.sticker.file_id if m.sticker else None
    }
    await state.update_data(post_data=data)
    await state.set_state(CreateLot.winners_count)
    await m.answer("Шаг 2: Сколько будет победителей? (введите число)")

@dp.message(CreateLot.winners_count)
async def adm_create_lot_3(m: Message, state: FSMContext):
    if not m.text.isdigit():
        return await m.answer("Введите именно число!")
    await state.update_data(wc=int(m.text))
    await state.set_state(CreateLot.channels)
    await m.answer("Шаг 3: Каналы для ОП через запятую (напр. @chan1, @chan2) или 'нет':")

@dp.message(CreateLot.channels)
async def adm_create_lot_4(m: Message, state: FSMContext):
    await state.update_data(ch=m.text)
    kb = [
        [InlineKeyboardButton(text="⏰ По времени", callback_data="type_time"), 
         InlineKeyboardButton(text="👥 По количеству", callback_data="type_count")]
    ]
    await m.answer("Шаг 4: Выберите условие завершения:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("type_"))
async def adm_create_lot_5(c: CallbackQuery, state: FSMContext):
    finish_type = c.data.split("_")[1]
    await state.update_data(ft=finish_type)
    
    prompt = "Через сколько ЧАСОВ завершить?" if finish_type == "time" else "При скольки УЧАСТНИКАХ завершить?"
    await c.message.answer(prompt)
    await state.set_state(CreateLot.value)

@dp.message(CreateLot.value)
async def adm_create_lot_final(m: Message, state: FSMContext):
    if not m.text.isdigit():
        return await m.answer("Введите число!")
    
    user_data = await state.get_data()
    post = user_data['post_data']
    
    final_val = m.text
    if user_data['ft'] == "time":
        final_val = (datetime.now() + timedelta(hours=int(m.text))).strftime("%d.%m.%Y %H:%M")

    # Сохранение в БД
    lid = db_query(
        "INSERT INTO lotteries (text, entities, channels, finish_type, finish_value, photo, sticker, winners_count) VALUES (?,?,?,?,?,?,?,?)",
        (post['text'], post['entities'], user_data['ch'], user_data['ft'], final_val, post['photo'], post['sticker'], user_data['wc']),
        commit=True
    )
    
    # Публикация в канал
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Участвовать! (0)", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lid}")
    
    try:
        if post['photo']:
            sent = await bot.send_photo(LOT_CHANNEL, post['photo'], caption=post['text'], reply_markup=kb.as_markup())
        elif post['sticker']:
            await bot.send_sticker(LOT_CHANNEL, post['sticker'])
            sent = await bot.send_message(LOT_CHANNEL, "🎁 Участвуй в новом розыгрыше!", reply_markup=kb.as_markup())
        else:
            sent = await bot.send_message(LOT_CHANNEL, post['text'], reply_markup=kb.as_markup())
        
        db_query("UPDATE lotteries SET message_id = ? WHERE id = ?", (sent.message_id, lid), commit=True)
        await m.answer(f"✅ Лотерея #{lid} успешно создана и опубликована в канале!")
    except Exception as e:
        await m.answer(f"❌ Ошибка публикации: {e}")
    
    await state.clear()

# =================================================================
# 9. КНОПКИ ГЛАВНОГО МЕНЮ И ОТЗЫВЫ
# =================================================================
@dp.callback_query(F.data == "active_lots")
async def process_active_lots(c: CallbackQuery):
    lots = db_query("SELECT * FROM lotteries WHERE status = 'active'", fetchall=True)
    if not lots:
        return await c.answer("Сейчас нет активных розыгрышей.", show_alert=True)
    
    res = "📢 **АКТИВНЫЕ РОЗЫГРЫШИ:**\n\n"
    for l in lots:
        res += f"🔹 Лот #{l['id']} | Участников: {l['participants_count']} | Мест: {l['winners_count']}\n"
    
    await c.message.answer(res, parse_mode="Markdown")
    await c.answer()

@dp.callback_query(F.data == "view_reviews")
async def process_view_reviews(c: CallbackQuery):
    reviews = db_query(
        "SELECT r.*, u.full_name FROM reviews r JOIN users u ON r.user_id = u.user_id ORDER BY r.id DESC LIMIT 10", 
        fetchall=True
    )
    if not reviews:
        return await c.answer("Отзывов пока нет.", show_alert=True)
    
    out = "💬 **ОТЗЫВЫ ПОБЕДИТЕЛЕЙ:**\n\n"
    for r in reviews:
        out += f"👤 {r['full_name']} (Лот #{r['lot_id']}):\n«{r['text']}»\n{'-'*20}\n"
    
    await c.message.answer(out, parse_mode="Markdown")
    await c.answer()

@dp.callback_query(F.data == "apply_pr")
async def process_pr_1(c: CallbackQuery, state: FSMContext):
    await state.set_state(PRApplication.age)
    await c.message.answer("📝 Заявка на PR.\nВаш возраст?")

@dp.message(PRApplication.age)
async def process_pr_2(m: Message, state: FSMContext):
    await state.update_data(age=m.text)
    await state.set_state(PRApplication.nickname)
    await m.answer("Ваша ссылка на канал или юзернейм?")

@dp.message(PRApplication.nickname)
async def process_pr_3(m: Message, state: FSMContext):
    await state.update_data(nick=m.text)
    await state.set_state(PRApplication.proofs)
    await m.answer("Пришлите скриншот вашей статистики:")

@dp.message(PRApplication.proofs, F.content_type == ContentType.PHOTO)
async def process_pr_final(m: Message, state: FSMContext):
    data = await state.get_data()
    msg = (f"📩 **НОВАЯ ЗАЯВКА PR**\n\n"
           f"👤 От: @{m.from_user.username}\n"
           f"🔞 Возраст: {data['age']}\n"
           f"🔗 Канал: {data['nick']}")
    if PR_CHAT_ID:
        await bot.send_photo(PR_CHAT_ID, m.photo[-1].file_id, caption=msg)
    await m.answer("✅ Ваша заявка отправлена!")
    await state.clear()

@dp.callback_query(F.data.startswith("rev_"))
async def process_leave_review_start(c: CallbackQuery, state: FSMContext):
    lot_id = c.data.split("_")[1]
    await state.update_data(rev_lot_id=lot_id)
    await state.set_state(LeaveReview.text)
    await c.message.answer("Пожалуйста, напишите ваш отзыв:")

@dp.message(LeaveReview.text)
async def process_leave_review_final(m: Message, state: FSMContext):
    data = await state.get_data()
    db_query(
        "INSERT INTO reviews (user_id, lot_id, text) VALUES (?,?,?)", 
        (m.from_user.id, data['rev_lot_id'], m.text), 
        commit=True
    )
    await m.answer("✅ Спасибо за ваш отзыв!")
    await state.clear()

@dp.callback_query(F.data == "to_start")
async def process_to_start(c: CallbackQuery, state: FSMContext):
    await cmd_start(c.message, CommandObject(command="start"), state)
    await c.message.delete()

# =================================================================
# 10. ФОНОВЫЙ ТАЙМЕР И ЗАПУСК
# =================================================================
async def time_monitor():
    """Проверка лотерей по времени каждые 60 сек"""
    while True:
        try:
            active_time_lots = db_query(
                "SELECT * FROM lotteries WHERE status = 'active' AND finish_type = 'time'", 
                fetchall=True
            )
            now = datetime.now()
            for lot in active_time_lots:
                try:
                    f_time = datetime.strptime(lot['finish_value'], "%d.%m.%Y %H:%M")
                    if now >= f_time:
                        await finalize_giveaway(lot['id'])
                except:
                    continue
        except Exception as e:
            logger.error(f"Ошибка в мониторе времени: {e}")
        await asyncio.sleep(60)

async def main():
    init_db()
    # Запуск фонового процесса
    asyncio.create_task(time_monitor())
    
    logger.info("Бот запускается...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
