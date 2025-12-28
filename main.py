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
# 1. СИСТЕМНЫЕ НАСТРОЙКИ И ЛОГИРОВАНИЕ
# =================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("LotteryMaster_FINAL")

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
# Список ID админов через запятую
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
# ID чата, куда падают заявки на пиар И УВЕДОМЛЕНИЯ О РЕФЕРАЛАХ
PR_CHAT_ID = int(os.getenv("PR_CHAT_ID", "0"))
# Канал, где идут лотереи
LOT_CHANNEL = os.getenv("LOT_CHANNEL", "@lotsvitechek")

if not TOKEN:
    logger.critical("❌ ОШИБКА: Токен не найден в .env файле!")
    sys.exit(1)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# =================================================================
# 2. БАЗА ДАННЫХ
# =================================================================
def init_db():
    """Инициализация таблиц БД"""
    logger.info("Подключение к базе данных...")
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    
    # Таблица лотерей
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
    
    # Таблица юзеров (рефералка)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        referrer_id INTEGER DEFAULT 0,
        refs_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

    # Миграции (проверка колонок)
    cur.execute("PRAGMA table_info(users)")
    cols = [c[1] for c in cur.fetchall()]
    if 'referrer_id' not in cols: 
        cur.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER DEFAULT 0")
    if 'refs_count' not in cols: 
        cur.execute("ALTER TABLE users ADD COLUMN refs_count INTEGER DEFAULT 0")

    conn.commit()
    conn.close()
    logger.info("БД готова к работе.")

def db_query(query, params=(), fetchone=False, fetchall=False, commit=False):
    """Обертка для SQL запросов"""
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
# 3. МАШИНА СОСТОЯНИЙ (FSM)
# =================================================================
class CreateLot(StatesGroup):
    text = State()
    winners_count = State()
    channels = State()
    finish_type = State()
    value = State()

class EditLotState(StatesGroup):
    lot_id = State()
    field_to_edit = State() # winners_count, finish_value, channels
    new_value = State()
    finish_type_cache = State() # чтобы помнить тип финиша при редактировании

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
# 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =================================================================
async def check_user_sub(user_id: int, channels_str: str):
    """Проверка подписки юзера на каналы"""
    if not channels_str or channels_str.lower() in ['нет', 'none', '']:
        return True, []
    
    not_subscribed = []
    # Удаляем пробелы и разбиваем
    channels_list = [c.strip() for c in channels_str.split(",") if c.strip()]
    
    for channel in channels_list:
        try:
            member = await bot.get_chat_member(channel, user_id)
            if member.status in ["left", "kicked", "restricted"]:
                # Иногда restricted может означать бан, проверяем права
                if member.status == "restricted" and not member.is_member:
                     not_subscribed.append(channel)
                elif member.status in ["left", "kicked"]:
                    not_subscribed.append(channel)
        except Exception as e:
            # Если бот не админ в канале, он может не увидеть юзера, считаем что не подписан
            logger.warning(f"Ошибка проверки подписки {channel}: {e}")
            not_subscribed.append(channel)
            
    return len(not_subscribed) == 0, not_subscribed

async def update_lot_card(lot_id: int, count: int):
    """Обновление кнопки в канале с лотом"""
    lot = db_query("SELECT message_id FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or not lot['message_id']:
        return

    me = await bot.get_me()
    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"✅ Участвовать! ({count})", 
        url=f"https://t.me/{me.username}?start=lot_{lot_id}"
    )
    
    try:
        await bot.edit_message_reply_markup(
            chat_id=LOT_CHANNEL, 
            message_id=lot['message_id'], 
            reply_markup=kb.as_markup()
        )
    except Exception as e:
        logger.debug(f"Не удалось обновить кнопку (возможно не изменилась): {e}")

async def run_final_selection(lot_id: int):
    """Финализация розыгрыша"""
    logger.info(f"Запуск финализации лота #{lot_id}")
    lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or lot['status'] == 'closed':
        return
    
    participants = db_query("SELECT * FROM participants WHERE lot_id = ?", (lot_id,), fetchall=True)
    
    # Закрываем лот
    db_query("UPDATE lotteries SET status = 'closed' WHERE id = ?", (lot_id,), commit=True)
    
    if not participants:
        await bot.send_message(LOT_CHANNEL, f"⚠️ Розыгрыш #{lot_id} завершен. Участников не набралось.")
        return

    # Выбор победителей
    count_to_win = min(len(participants), lot['winners_count'])
    winners_list = random.sample(participants, count_to_win)
    
    mentions = []
    for winner in winners_list:
        # Запись победителя
        db_query("INSERT INTO winners (lot_id, user_id) VALUES (?,?)", (lot_id, winner['user_id']), commit=True)
        
        # Формирование меншна
        mention = f"@{winner['username']}" if winner['username'] else f"[{winner['full_name']}](tg://user?id={winner['user_id']})"
        mentions.append(mention)
        
        # ЛС победителю
        try:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✍️ Оставить отзыв", callback_data=f"rev_{lot_id}")]
            ])
            await bot.send_message(
                winner['user_id'], 
                f"🎉 ПОЗДРАВЛЯЕМ! Вы выиграли в розыгрыше #{lot_id}!\nСвяжитесь с администратором для получения приза.", 
                reply_markup=kb
            )
        except Exception:
            pass # Юзер мог заблокировать бота

    # Пост в канал
    result_text = (
        f"🎊 **ИТОГИ РОЗЫГРЫША #{lot_id}**\n\n"
        f"🏆 Победители: {', '.join(mentions)}\n"
        f"📊 Всего участников: {len(participants)}\n\n"
        f"Победители получили инструкции в ЛС!"
    )
    
    try:
        await bot.send_message(LOT_CHANNEL, result_text, parse_mode="Markdown", reply_to_message_id=lot['message_id'])
    except Exception:
        await bot.send_message(LOT_CHANNEL, result_text, parse_mode="Markdown")

# =================================================================
# 5. START / РЕФЕРАЛЫ / УЧАСТИЕ
# =================================================================
@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    uid = message.from_user.id
    args = command.args
    
    # 5.1 Регистрация пользователя и рефералка
    user = db_query("SELECT * FROM users WHERE user_id = ?", (uid,), fetchone=True)
    if not user:
        ref_id = 0
        if args and args.startswith("ref"):
            try:
                possible_ref = int(args.replace("ref", ""))
                if possible_ref != uid: # Нельзя пригласить самого себя
                    ref_id = possible_ref
            except:
                ref_id = 0
        
        db_query(
            "INSERT INTO users (user_id, username, full_name, referrer_id) VALUES (?,?,?,?)",
            (uid, message.from_user.username, message.from_user.full_name, ref_id),
            commit=True
        )
        logger.info(f"Новый пользователь: {uid} (ref: {ref_id})")
        
        # ЕСЛИ ЕСТЬ ПРИГЛАСИВШИЙ
        if ref_id != 0:
            db_query("UPDATE users SET refs_count = refs_count + 1 WHERE user_id = ?", (ref_id,), commit=True)
            inviter = db_query("SELECT * FROM users WHERE user_id = ?", (ref_id,), fetchone=True)
            
            if inviter:
                # 1. Сообщение тому, КТО зашел
                await message.answer(f"👋 Вы приглашены партнером: **{inviter['full_name']}**")
                
                # 2. Сообщение ТОМУ, КТО пригласил
                try:
                    await bot.send_message(ref_id, f"🤝 **У вас новый реферал!**\nПользователь: {message.from_user.full_name}")
                except: pass
                
                # 3. ОТЧЕТ В ЧАТ ЗАЯВОК (PR_CHAT_ID)
                if PR_CHAT_ID:
                    try:
                        pr_report = (
                            f"📈 **НОВЫЙ РЕФЕРАЛ!**\n\n"
                            f"👤 **Партнер:** {inviter['full_name']} (@{inviter['username'] or '---'})\n"
                            f"🆔 ID Партнера: `{ref_id}`\n\n"
                            f"🆕 **Реферал:** {message.from_user.full_name} (@{message.from_user.username or '---'})\n"
                            f"🆔 ID Реферала: `{uid}`\n\n"
                            f"📊 **Итого приглашено:** {inviter['refs_count'] + 1}"
                        )
                        await bot.send_message(PR_CHAT_ID, pr_report, parse_mode="Markdown")
                    except Exception as e:
                        logger.error(f"Не удалось отправить отчет в PR чат: {e}")

    # 5.2 Обработка входа в ЛОТ (lot_ID)
    if args and args.startswith("lot_"):
        try:
            lot_id = int(args.split("_")[1])
            lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
            
            if not lot:
                return await message.answer("❌ Розыгрыш не найден.")
            if lot['status'] == 'closed':
                return await message.answer("❌ Этот розыгрыш уже завершен.")
            
            # Проверка: уже участвует?
            check_exist = db_query("SELECT 1 FROM participants WHERE user_id = ? AND lot_id = ?", (uid, lot_id), fetchone=True)
            if check_exist:
                return await message.answer(f"⚠️ Вы уже участвуете в лотерее #{lot_id}. Ожидайте результатов!")

            # Проверка подписки
            is_sub, bad_channels = await check_user_sub(uid, lot['channels'])
            if not is_sub:
                me = await bot.get_me()
                kb = InlineKeyboardBuilder()
                for ch in bad_channels:
                    clean_ch = ch.replace("@", "").strip()
                    kb.button(text=f"📢 Подписаться на {ch}", url=f"https://t.me/{clean_ch}")
                kb.button(text="🔄 ПРОВЕРИТЬ ПОДПИСКУ", url=f"https://t.me/{me.username}?start=lot_{lot_id}")
                kb.adjust(1)
                
                return await message.answer(
                    "⚠️ **Для участия необходимо подписаться на каналы:**", 
                    reply_markup=kb.as_markup(),
                    parse_mode="Markdown"
                )

            # Добавляем участника
            try:
                db_query(
                    "INSERT INTO participants (user_id, lot_id, username, full_name) VALUES (?,?,?,?)",
                    (uid, lot_id, message.from_user.username, message.from_user.full_name),
                    commit=True
                )
            except sqlite3.IntegrityError:
                return await message.answer("⚠️ Вы уже участвуете!")

            # Обновляем счетчик
            new_count_res = db_query("SELECT COUNT(*) as c FROM participants WHERE lot_id = ?", (lot_id,), fetchone=True)
            new_count = new_count_res['c']
            db_query("UPDATE lotteries SET participants_count = ? WHERE id = ?", (new_count, lot_id), commit=True)
            
            # Обновляем кнопку в канале
            await update_lot_card(lot_id, new_count)
            
            # Проверка условия завершения (по количеству)
            if lot['finish_type'] == 'count' and new_count >= int(lot['finish_value']):
                await run_final_selection(lot_id)
            
            return await message.answer(f"✅ **УСПЕХ!** Вы зарегистрированы в розыгрыше #{lot_id}. Удачи!")

        except Exception as e:
            logger.error(f"Ошибка входа в лот: {e}")
            return await message.answer("Произошла ошибка при регистрации. Попробуйте позже.")

    # 5.3 Главное меню
    kb = [
        [InlineKeyboardButton(text="💬 Читать отзывы", callback_data="view_reviews"), 
         InlineKeyboardButton(text="💼 Стать партнером", callback_data="apply_pr")],
        [InlineKeyboardButton(text="📢 Активные розыгрыши", callback_data="active_lots"), 
         InlineKeyboardButton(text="📊 Мой профиль", callback_data="my_stats")]
    ]
    if uid in ADMIN_IDS:
        kb.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_main")])
    
    text_hello = f"👋 Привет, {message.from_user.first_name}!\nЯ бот для проведения честных розыгрышей.\nВыбирай действие в меню:"
    await message.answer(text_hello, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

# =================================================================
# 6. ОБРАБОТЧИКИ ГЛАВНОГО МЕНЮ
# =================================================================

@dp.callback_query(F.data == "active_lots")
async def process_active_lots(c: CallbackQuery):
    """Показывает список активных лотерей"""
    lots = db_query("SELECT * FROM lotteries WHERE status = 'active' ORDER BY id DESC LIMIT 10", fetchall=True)
    
    if not lots:
        return await c.answer("На данный момент активных розыгрышей нет 😔", show_alert=True)
    
    text = "📢 **АКТИВНЫЕ РОЗЫГРЫШИ:**\n\n"
    kb = InlineKeyboardBuilder()
    
    me = await bot.get_me()
    
    for lot in lots:
        text += f"🔹 **Лот #{lot['id']}**\n"
        text += f"   🏆 Призовых мест: {lot['winners_count']}\n"
        text += f"   👥 Участников: {lot['participants_count']}\n"
        if lot['finish_type'] == 'time':
            text += f"   ⏳ Финиш: {lot['finish_value']}\n"
        else:
            text += f"   🎯 Финиш: когда наберется {lot['finish_value']} чел.\n"
        text += "-------------------\n"
        
        # Кнопка перехода
        kb.button(text=f"Перейти к Лоту #{lot['id']}", url=f"https://t.me/{me.username}?start=lot_{lot['id']}")

    kb.button(text="🔙 Назад в меню", callback_data="to_start")
    kb.adjust(1)
    
    await c.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "view_reviews")
async def process_view_reviews(c: CallbackQuery):
    """Показывает последние отзывы"""
    reviews = db_query("""
        SELECT r.text, r.lot_id, u.full_name 
        FROM reviews r 
        JOIN users u ON r.user_id = u.user_id 
        ORDER BY r.id DESC LIMIT 10
    """, fetchall=True)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 Назад в меню", callback_data="to_start")
    
    if not reviews:
        await c.message.edit_text("💬 Отзывов пока нет. Станьте первым победителем и напишите!", reply_markup=kb.as_markup())
        return

    text = "💬 **ОТЗЫВЫ ПОБЕДИТЕЛЕЙ:**\n\n"
    for r in reviews:
        text += f"👤 **{r['full_name']}** (Лот #{r['lot_id']}):\n_{r['text']}_\n\n"
    
    await c.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "to_start")
async def process_back_to_start(c: CallbackQuery, state: FSMContext):
    """Возврат в главное меню"""
    await state.clear()
    try:
        await c.message.delete()
    except:
        pass
    await cmd_start(c.message, CommandObject(command="start"), state)

# =================================================================
# 7. ПРОФИЛЬ, РЕФЕРАЛЫ И ЗАЯВКА PR
# =================================================================
@dp.callback_query(F.data == "my_stats")
async def show_profile(c: CallbackQuery):
    uid = c.from_user.id
    user = db_query("SELECT * FROM users WHERE user_id = ?", (uid,), fetchone=True)
    p_cnt = db_query("SELECT COUNT(*) as c FROM participants WHERE user_id = ?", (uid,), fetchone=True)['c']
    w_cnt = db_query("SELECT COUNT(*) as c FROM winners WHERE user_id = ?", (uid,), fetchone=True)['c']
    
    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start=ref{uid}"
    
    msg = (f"👤 **ЛИЧНЫЙ КАБИНЕТ**\n\n"
           f"🆔 ID: `{uid}`\n"
           f"🎫 Участий в лотах: **{p_cnt}**\n"
           f"🏆 Побед: **{w_cnt}**\n"
           f"👥 Рефералов: **{user['refs_count']}**\n\n"
           f"🔗 **Твоя ссылка для друзей:**\n`{ref_link}`")
    
    kb = [
        [InlineKeyboardButton(text="👥 Мои рефералы", callback_data="my_refs_list")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="to_start")]
    ]
    await c.message.edit_text(msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")

@dp.callback_query(F.data == "my_refs_list")
async def show_refs_list(c: CallbackQuery):
    uid = c.from_user.id
    refs = db_query("SELECT full_name, username, created_at FROM users WHERE referrer_id = ? ORDER BY created_at DESC LIMIT 40", (uid,), fetchall=True)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 Назад", callback_data="my_stats")
    
    if not refs:
        return await c.message.edit_text("😔 Вы еще никого не пригласили.", reply_markup=kb.as_markup())
    
    text = "👥 **ВАШИ РЕФЕРАЛЫ (Топ 40):**\n\n"
    for i, r in enumerate(refs, 1):
        d = r['created_at'].split()[0]
        u = f"(@{r['username']})" if r['username'] else ""
        text += f"{i}. {r['full_name']} {u} — {d}\n"
        
    await c.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="Markdown")

# --- ЛОГИКА ЗАЯВКИ PR ---
@dp.callback_query(F.data == "apply_pr")
async def pr_step1(c: CallbackQuery, state: FSMContext):
    await state.set_state(PRApplication.age)
    await c.message.edit_text("📝 **ЗАЯВКА НА СОТРУДНИЧЕСТВО**\n\n1. Напишите ваш возраст:")

@dp.message(PRApplication.age)
async def pr_step2(m: Message, state: FSMContext):
    await state.update_data(age=m.text)
    await state.set_state(PRApplication.nickname)
    await m.answer("2. Пришлите ссылку на ваш канал или ваш юзернейм:")

@dp.message(PRApplication.nickname)
async def pr_step3(m: Message, state: FSMContext):
    await state.update_data(nick=m.text)
    await state.set_state(PRApplication.proofs)
    await m.answer("3. Пришлите скриншот статистики канала (картинкой):")

@dp.message(PRApplication.proofs, F.content_type == ContentType.PHOTO)
async def pr_finish(m: Message, state: FSMContext):
    data = await state.get_data()
    
    if PR_CHAT_ID:
        caption = (f"📩 **НОВАЯ ЗАЯВКА PR**\n\n"
                   f"👤 От: {m.from_user.full_name} (@{m.from_user.username})\n"
                   f"🎂 Возраст: {data['age']}\n"
                   f"🔗 Канал/Ник: {data['nick']}\n"
                   f"🆔 ID: `{m.from_user.id}`")
        try:
            await bot.send_photo(PR_CHAT_ID, m.photo[-1].file_id, caption=caption)
        except Exception as e:
            logger.error(f"Ошибка отправки PR заявки: {e}")

    await m.answer("✅ Заявка успешно отправлена администраторам!")
    await state.clear()

# --- ЛОГИКА ОТЗЫВОВ ---
@dp.callback_query(F.data.startswith("rev_"))
async def review_start(c: CallbackQuery, state: FSMContext):
    lot_id = c.data.split("_")[1]
    is_win = db_query("SELECT 1 FROM winners WHERE user_id = ? AND lot_id = ?", (c.from_user.id, lot_id), fetchone=True)
    if not is_win:
        return await c.answer("Вы не являетесь победителем этого розыгрыша!", show_alert=True)
        
    await state.update_data(target_lot=lot_id)
    await state.set_state(LeaveReview.text)
    await c.message.answer(f"✍️ Напишите ваш отзыв о розыгрыше #{lot_id}:")

@dp.message(LeaveReview.text)
async def review_finish(m: Message, state: FSMContext):
    data = await state.get_data()
    lot_id = data['target_lot']
    db_query("INSERT INTO reviews (user_id, lot_id, text) VALUES (?,?,?)", 
             (m.from_user.id, lot_id, m.text), commit=True)
    await m.answer("✅ Спасибо! Ваш отзыв сохранен.")
    await state.clear()

# =================================================================
# 8. АДМИН-ПАНЕЛЬ: УПРАВЛЕНИЕ И ПОИСК
# =================================================================
@dp.callback_query(F.data == "admin_main")
async def admin_main_menu(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS: return await c.answer("⛔ Доступ запрещен")
    
    kb = [
        [InlineKeyboardButton(text="➕ Создать лот", callback_data="adm_create")],
        [InlineKeyboardButton(text="📝 Управление лотами", callback_data="adm_manage_lots")],
        [InlineKeyboardButton(text="🔍 Поиск (ID/@user)", callback_data="adm_search_user")],
        [InlineKeyboardButton(text="📩 Рассылка", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="to_start")]
    ]
    await c.message.edit_text("🛠 **АДМИН ПАНЕЛЬ**", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

# --- СПИСОК ЛОТОВ ---
@dp.callback_query(F.data == "adm_manage_lots")
async def admin_list_lots(c: CallbackQuery):
    lots = db_query("SELECT * FROM lotteries WHERE status='active' ORDER BY id DESC", fetchall=True)
    builder = InlineKeyboardBuilder()
    if not lots:
        await c.answer("Нет активных лотов", show_alert=True)
    else:
        for l in lots:
            builder.button(text=f"Лот #{l['id']} (Уч: {l['participants_count']})", callback_data=f"manage_{l['id']}")
            
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_main"))
    msg_text = "📝 **ВЫБЕРИТЕ ЛОТ ДЛЯ РЕДАКТИРОВАНИЯ:**" if lots else "Нет активных лотов."
    await c.message.edit_text(msg_text, reply_markup=builder.as_markup())

# --- УПРАВЛЕНИЕ КОНКРЕТНЫМ ЛОТОМ ---
@dp.callback_query(F.data.startswith("manage_"))
async def admin_manage_single(c: CallbackQuery):
    lid = int(c.data.split("_")[1])
    l = db_query("SELECT * FROM lotteries WHERE id = ?", (lid,), fetchone=True)
    if not l:
        return await c.answer("Лот не найден (возможно удален)", show_alert=True)

    info = (f"⚙️ **НАСТРОЙКИ ЛОТА #{lid}**\n\n"
            f"👥 Участников: **{l['participants_count']}**\n"
            f"🏆 Победителей: **{l['winners_count']}**\n"
            f"🏁 Тип финиша: **{l['finish_type']}**\n"
            f"🎯 Значение финиша: **{l['finish_value']}**\n"
            f"📢 Каналы: `{l['channels']}`")
            
    kb = [
        [InlineKeyboardButton(text="👥 Список участников", callback_data=f"listp_{lid}")],
        [InlineKeyboardButton(text="🏆 Изм. кол-во победителей", callback_data=f"edit_w_{lid}")],
        [InlineKeyboardButton(text="⏳ Изм. финиш", callback_data=f"edit_f_{lid}")],
        [InlineKeyboardButton(text="📢 Изм. каналы (подписку)", callback_data=f"edit_s_{lid}")],
        [InlineKeyboardButton(text="🛑 ЗАВЕРШИТЬ СЕЙЧАС", callback_data=f"stop_{lid}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm_manage_lots")]
    ]
    await c.message.edit_text(info, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")

# --- ХЕНДЛЕР СПИСКА УЧАСТНИКОВ ---
@dp.callback_query(F.data.startswith("listp_"))
async def admin_show_participants(c: CallbackQuery):
    lid = int(c.data.split("_")[1])
    parts = db_query("SELECT full_name, user_id, username FROM participants WHERE lot_id = ? LIMIT 60", (lid,), fetchall=True)
    if not parts: return await c.answer("Нет участников.", show_alert=True)
    text = f"👥 **Участники #{lid} (первые 60):**\n\n"
    for p in parts:
        nick = f"(@{p['username']})" if p['username'] else ""
        text += f"• {p['full_name']} {nick} [`{p['user_id']}`]\n"
    await c.message.answer(text, parse_mode="Markdown")
    await c.answer()

# --- ЛОГИКА РЕДАКТИРОВАНИЯ (EditLotState) ---
@dp.callback_query(F.data.startswith("edit_w_"))
async def edit_winners_init(c: CallbackQuery, state: FSMContext):
    lid = int(c.data.split("_")[2])
    await state.update_data(lot_id=lid, field="winners_count")
    await state.set_state(EditLotState.new_value)
    await c.message.answer(f"Введите новое количество победителей для лота #{lid}:")

@dp.callback_query(F.data.startswith("edit_f_"))
async def edit_finish_init(c: CallbackQuery, state: FSMContext):
    lid = int(c.data.split("_")[2])
    lot = db_query("SELECT finish_type FROM lotteries WHERE id = ?", (lid,), fetchone=True)
    await state.update_data(lot_id=lid, field="finish_value", ftype=lot['finish_type'])
    await state.set_state(EditLotState.new_value)
    prompt = "Введите новое время финиша (в часах от текущего момента):" if lot['finish_type'] == 'time' else "Введите новое количество участников для авто-финиша:"
    await c.message.answer(prompt)

@dp.callback_query(F.data.startswith("edit_s_"))
async def edit_subs_init(c: CallbackQuery, state: FSMContext):
    lid = int(c.data.split("_")[2])
    await state.update_data(lot_id=lid, field="channels")
    await state.set_state(EditLotState.new_value)
    await c.message.answer("Отправьте новый список каналов через запятую (напр. @chan1, @chan2) или 'нет', чтобы убрать подписку:")

@dp.message(EditLotState.new_value)
async def save_edited_value(m: Message, state: FSMContext):
    data = await state.get_data()
    lid = data['lot_id']
    field = data['field']
    val = m.text
    
    if field == "winners_count":
        if not val.isdigit(): return await m.answer("Введите число!")
        final_val = int(val)
        
    elif field == "finish_value":
        if not val.isdigit(): return await m.answer("Введите число!")
        if data.get('ftype') == 'time':
            final_val = (datetime.now() + timedelta(hours=int(val))).strftime("%d.%m.%Y %H:%M")
        else:
            final_val = int(val)
            
    elif field == "channels":
        final_val = val
    
    db_query(f"UPDATE lotteries SET {field} = ? WHERE id = ?", (final_val, lid), commit=True)
    await m.answer(f"✅ Лот #{lid} обновлен! Поле: {field} -> {final_val}")
    await state.clear()

# --- ПРИНУДИТЕЛЬНАЯ ОСТАНОВКА ---
@dp.callback_query(F.data.startswith("stop_"))
async def force_stop_lot(c: CallbackQuery):
    lid = int(c.data.split("_")[1])
    await run_final_selection(lid)
    await c.answer("Лот остановлен!", show_alert=True)
    await admin_list_lots(c)

# --- ПОИСК ЮЗЕРА ---
@dp.callback_query(F.data == "adm_search_user")
async def search_u_init(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminSearch.query)
    await c.message.answer("Введите ID или @username пользователя:")

@dp.message(AdminSearch.query)
async def search_u_res(m: Message, state: FSMContext):
    q = m.text.replace("@", "").strip()
    
    if q.isdigit():
        u = db_query("SELECT * FROM users WHERE user_id = ?", (int(q),), fetchone=True)
    else:
        u = db_query("SELECT * FROM users WHERE username = ?", (q,), fetchone=True)
        
    if not u:
        return await m.answer("❌ Пользователь не найден.")
    
    uid = u['user_id']
    p_stat = db_query("SELECT COUNT(*) as c FROM participants WHERE user_id = ?", (uid,), fetchone=True)['c']
    w_stat = db_query("SELECT COUNT(*) as c FROM winners WHERE user_id = ?", (uid,), fetchone=True)['c']
    
    txt = (f"🕵️ **Dosser on {u['full_name']}**\n\n"
           f"🆔 ID: `{uid}`\n"
           f"🔗 User: @{u['username']}\n"
           f"📅 Дата реги: {u['created_at']}\n"
           f"🎲 Участий: {p_stat} | 🏆 Побед: {w_stat}\n"
           f"👥 Привел рефералов: {u['refs_count']}")
           
    await m.answer(txt)
    await state.clear()

# --- РАССЫЛКА ---
@dp.callback_query(F.data == "adm_broadcast")
async def broad_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(BroadcastState.content)
    await c.message.answer("Пришлите пост для рассылки (Текст/Фото):")

@dp.message(BroadcastState.content)
async def broad_run(m: Message, state: FSMContext):
    users = db_query("SELECT user_id FROM users", fetchall=True)
    await m.answer(f"🚀 Старт рассылки на {len(users)} чел...")
    good = 0
    bad = 0
    for u in users:
        try:
            await m.copy_to(u['user_id'])
            good += 1
            await asyncio.sleep(0.05)
        except:
            bad += 1
    await m.answer(f"🏁 Рассылка завершена.\n✅ Доставлено: {good}\n❌ Блок/Ошибки: {bad}")
    await state.clear()

# =================================================================
# 9. СОЗДАНИЕ ЛОТА (WIZARD)
# =================================================================
@dp.callback_query(F.data == "adm_create")
async def create_1(c: CallbackQuery, state: FSMContext):
    await state.set_state(CreateLot.text)
    await c.message.answer("1. Отправьте пост для канала (Текст, Фото+Подпись, Стикер):")

@dp.message(CreateLot.text)
async def create_2(m: Message, state: FSMContext):
    ents = json.dumps([e.model_dump(mode='json') for e in (m.entities or m.caption_entities or [])])
    pdata = {
        "text": m.caption or m.text or "",
        "entities": ents,
        "photo": m.photo[-1].file_id if m.photo else None,
        "sticker": m.sticker.file_id if m.sticker else None
    }
    await state.update_data(post=pdata)
    await state.set_state(CreateLot.winners_count)
    await m.answer("2. Количество победителей (число):")

@dp.message(CreateLot.winners_count)
async def create_3(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("Число!")
    await state.update_data(wc=int(m.text))
    await state.set_state(CreateLot.channels)
    await m.answer("3. Каналы для подписки через запятую (@a, @b) или 'нет':")

@dp.message(CreateLot.channels)
async def create_4(m: Message, state: FSMContext):
    await state.update_data(ch=m.text)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏰ По времени", callback_data="ft_time")
    kb.button(text="👥 По кол-ву участников", callback_data="ft_count")
    await m.answer("4. Условие завершения:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("ft_"))
async def create_5(c: CallbackQuery, state: FSMContext):
    ft = "time" if c.data == "ft_time" else "count"
    await state.update_data(ftype=ft)
    prompt = "Через сколько ЧАСОВ завершить?" if ft == "time" else "При скольки УЧАСТНИКАХ завершить?"
    await c.message.edit_text(prompt)
    await state.set_state(CreateLot.value)

@dp.message(CreateLot.value)
async def create_finish(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("Число!")
    data = await state.get_data()
    post = data['post']
    
    if data['ftype'] == 'time':
        f_val = (datetime.now() + timedelta(hours=int(m.text))).strftime("%d.%m.%Y %H:%M")
    else:
        f_val = int(m.text)
        
    lid = db_query(
        """INSERT INTO lotteries (text, entities, channels, finish_type, finish_value, photo, sticker, winners_count) 
           VALUES (?,?,?,?,?,?,?,?)""",
        (post['text'], post['entities'], data['ch'], data['ftype'], f_val, post['photo'], post['sticker'], data['wc']),
        commit=True
    )
    
    me = await bot.get_me()
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Участвовать! (0)", url=f"https://t.me/{me.username}?start=lot_{lid}")
    
    try:
        if post['photo']:
            sent = await bot.send_photo(LOT_CHANNEL, post['photo'], caption=post['text'], reply_markup=kb.as_markup())
        elif post['sticker']:
            await bot.send_sticker(LOT_CHANNEL, post['sticker'])
            sent = await bot.send_message(LOT_CHANNEL, "🎁 Новый розыгрыш! Жми кнопку ниже.", reply_markup=kb.as_markup())
        else:
            sent = await bot.send_message(LOT_CHANNEL, post['text'], reply_markup=kb.as_markup())
            
        db_query("UPDATE lotteries SET message_id = ? WHERE id = ?", (sent.message_id, lid), commit=True)
        await m.answer(f"✅ Лот #{lid} опубликован!")
        
    except Exception as e:
        await m.answer(f"❌ Ошибка публикации: {e}\nЛот создан в базе, но не в канале.")
        
    await state.clear()

# =================================================================
# 10. ЗАПУСК И ФОНОВЫЕ ЗАДАЧИ
# =================================================================
async def time_monitor():
    """Фоновая задача для проверки лотерей по времени"""
    logger.info("Монитор времени запущен")
    while True:
        try:
            lots = db_query("SELECT * FROM lotteries WHERE status='active' AND finish_type='time'", fetchall=True)
            now = datetime.now()
            for lot in lots:
                try:
                    f_time = datetime.strptime(lot['finish_value'], "%d.%m.%Y %H:%M")
                    if now >= f_time:
                        await run_final_selection(lot['id'])
                except ValueError:
                    logger.error(f"Неверный формат даты в лоте #{lot['id']}")
        except Exception as e:
            logger.error(f"Ошибка в мониторе: {e}")
        await asyncio.sleep(60)

async def main():
    init_db()
    asyncio.create_task(time_monitor())
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен и готов к работе!")
    try:
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Критическая ошибка поллинга: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
