import os
import random
import sqlite3
import logging
import json
import asyncio
import sys
import html as pyhtml # <--- ВАЖНО: Переименовали, чтобы не конфликтовало с aiogram
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Исключаем 'html' из импортов aiogram
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
logger = logging.getLogger("LotteryMaster_FIXED")

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
PR_CHAT_ID = int(os.getenv("PR_CHAT_ID", "0"))
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
    logger.info("Подключение к базе данных...")
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    
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
    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS participants (
        user_id INTEGER,
        lot_id INTEGER,
        username TEXT,
        full_name TEXT,
        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, lot_id)
    )""")
    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS winners (
        lot_id INTEGER,
        user_id INTEGER,
        win_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        referrer_id INTEGER DEFAULT 0,
        refs_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        lot_id INTEGER,
        text TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

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
    field_to_edit = State() 
    new_value = State()
    finish_type_cache = State()

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

class AdminDM(StatesGroup):
    user_id = State()
    message_text = State()

# =================================================================
# 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =================================================================
async def check_user_sub(user_id: int, channels_str: str):
    if not channels_str or channels_str.lower() in ['нет', 'none', '']:
        return True, []
    
    not_subscribed = []
    channels_list = [c.strip() for c in channels_str.split(",") if c.strip()]
    
    for channel in channels_list:
        try:
            member = await bot.get_chat_member(channel, user_id)
            if member.status in ["left", "kicked", "restricted"]:
                if member.status == "restricted" and not member.is_member:
                     not_subscribed.append(channel)
                elif member.status in ["left", "kicked"]:
                    not_subscribed.append(channel)
        except Exception as e:
            logger.warning(f"Ошибка проверки подписки {channel}: {e}")
            not_subscribed.append(channel)
            
    return len(not_subscribed) == 0, not_subscribed

async def update_lot_card(lot_id: int, count: int):
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
        logger.debug(f"Не удалось обновить кнопку: {e}")

async def run_final_selection(lot_id: int):
    logger.info(f"Запуск финализации лота #{lot_id}")
    lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    
    if not lot:
        return
    
    participants = db_query("SELECT * FROM participants WHERE lot_id = ?", (lot_id,), fetchall=True)
    
    # Закрываем лот
    db_query("UPDATE lotteries SET status = 'closed' WHERE id = ?", (lot_id,), commit=True)
    
    if not participants:
        try:
            await bot.send_message(LOT_CHANNEL, f"⚠️ Розыгрыш #{lot_id} завершен. Участников не набралось.", reply_to_message_id=lot['message_id'])
        except:
            await bot.send_message(LOT_CHANNEL, f"⚠️ Розыгрыш #{lot_id} завершен. Участников не набралось.")
        return

    # Выбор победителей
    count_to_win = min(len(participants), lot['winners_count'])
    winners_list = random.sample(participants, count_to_win)
    
    mentions = []
    for winner in winners_list:
        db_query("INSERT INTO winners (lot_id, user_id) VALUES (?,?)", (lot_id, winner['user_id']), commit=True)
        
        # ИСПОЛЬЗУЕМ pyhtml
        safe_name = pyhtml.escape(winner['full_name'])
        mention = f"@{winner['username']}" if winner['username'] else f"<a href='tg://user?id={winner['user_id']}'>{safe_name}</a>"
        mentions.append(mention)
        
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
            pass 

    result_text = (
        f"🎊 <b>ИТОГИ РОЗЫГРЫША #{lot_id}</b>\n\n"
        f"🏆 Победители: {', '.join(mentions)}\n"
        f"📊 Всего участников: {len(participants)}\n\n"
        f"Победители получили инструкции в ЛС!"
    )
    
    try:
        await bot.send_message(LOT_CHANNEL, result_text, parse_mode="HTML", reply_to_message_id=lot['message_id'])
    except Exception:
        await bot.send_message(LOT_CHANNEL, result_text, parse_mode="HTML")

# =================================================================
# 5. START / РЕФЕРАЛЫ / УЧАСТИЕ
# =================================================================
@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    uid = message.from_user.id
    args = command.args
    
    user = db_query("SELECT * FROM users WHERE user_id = ?", (uid,), fetchone=True)
    if not user:
        ref_id = 0
        if args and args.startswith("ref"):
            try:
                possible_ref = int(args.replace("ref", ""))
                if possible_ref != uid:
                    ref_id = possible_ref
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
            
            if inviter:
                # ИСПОЛЬЗУЕМ pyhtml
                await message.answer(f"👋 Вы приглашены партнером: <b>{pyhtml.escape(inviter['full_name'])}</b>", parse_mode="HTML")
                try:
                    await bot.send_message(ref_id, f"🤝 <b>У вас новый реферал!</b>\nПользователь: {pyhtml.escape(message.from_user.full_name)}", parse_mode="HTML")
                except: pass
                
                if PR_CHAT_ID:
                    try:
                        pr_report = (
                            f"📈 <b>НОВЫЙ РЕФЕРАЛ!</b>\n\n"
                            f"👤 <b>Партнер:</b> {pyhtml.escape(inviter['full_name'])} (@{inviter['username'] or '---'})\n"
                            f"🆔 ID Партнера: <code>{ref_id}</code>\n\n"
                            f"🆕 <b>Реферал:</b> {pyhtml.escape(message.from_user.full_name)} (@{message.from_user.username or '---'})\n"
                            f"🆔 ID Реферала: <code>{uid}</code>\n\n"
                            f"📊 <b>Итого приглашено:</b> {inviter['refs_count'] + 1}"
                        )
                        await bot.send_message(PR_CHAT_ID, pr_report, parse_mode="HTML")
                    except Exception as e:
                        logger.error(f"Не удалось отправить отчет в PR чат: {e}")

    if args and args.startswith("lot_"):
        try:
            lot_id = int(args.split("_")[1])
            lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
            
            if not lot:
                return await message.answer("❌ Розыгрыш не найден.")
            if lot['status'] == 'closed':
                return await message.answer("❌ Этот розыгрыш уже завершен.")
            
            check_exist = db_query("SELECT 1 FROM participants WHERE user_id = ? AND lot_id = ?", (uid, lot_id), fetchone=True)
            if check_exist:
                return await message.answer(f"⚠️ Вы уже участвуете в лотерее #{lot_id}. Ожидайте результатов!")

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
                    "⚠️ <b>Для участия необходимо подписаться на каналы:</b>", 
                    reply_markup=kb.as_markup(),
                    parse_mode="HTML"
                )

            try:
                db_query(
                    "INSERT INTO participants (user_id, lot_id, username, full_name) VALUES (?,?,?,?)",
                    (uid, lot_id, message.from_user.username, message.from_user.full_name),
                    commit=True
                )
            except sqlite3.IntegrityError:
                return await message.answer("⚠️ Вы уже участвуете!")

            new_count_res = db_query("SELECT COUNT(*) as c FROM participants WHERE lot_id = ?", (lot_id,), fetchone=True)
            new_count = new_count_res['c']
            db_query("UPDATE lotteries SET participants_count = ? WHERE id = ?", (new_count, lot_id), commit=True)
            
            await update_lot_card(lot_id, new_count)
            
            if lot['finish_type'] == 'count' and new_count >= int(lot['finish_value']):
                await run_final_selection(lot_id)
            
            return await message.answer(f"✅ <b>УСПЕХ!</b> Вы зарегистрированы в розыгрыше #{lot_id}. Удачи!", parse_mode="HTML")

        except Exception as e:
            logger.error(f"Ошибка входа в лот: {e}")
            return await message.answer("Произошла ошибка при регистрации. Попробуйте позже.")

    kb = [
        [InlineKeyboardButton(text="💬 Читать отзывы", callback_data="view_reviews"), 
         InlineKeyboardButton(text="💼 Стать партнером", callback_data="apply_pr")],
        [InlineKeyboardButton(text="📢 Активные розыгрыши", callback_data="active_lots"), 
         InlineKeyboardButton(text="📊 Мой профиль", callback_data="my_stats")]
    ]
    if uid in ADMIN_IDS:
        kb.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_main")])
    
    # ИСПОЛЬЗУЕМ pyhtml
    text_hello = f"👋 Привет, {pyhtml.escape(message.from_user.first_name)}!\nЯ бот для проведения честных розыгрышей.\nВыбирай действие в меню:"
    await message.answer(text_hello, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")

# =================================================================
# 6. ГЛАВНОЕ МЕНЮ
# =================================================================
@dp.callback_query(F.data == "active_lots")
async def process_active_lots(c: CallbackQuery):
    lots = db_query("SELECT * FROM lotteries WHERE status = 'active' ORDER BY id DESC LIMIT 10", fetchall=True)
    if not lots:
        return await c.answer("На данный момент активных розыгрышей нет 😔", show_alert=True)
    
    text = "📢 <b>АКТИВНЫЕ РОЗЫГРЫШИ:</b>\n\n"
    kb = InlineKeyboardBuilder()
    me = await bot.get_me()
    
    for lot in lots:
        text += f"🔹 <b>Лот #{lot['id']}</b>\n"
        text += f"   🏆 Призовых мест: {lot['winners_count']}\n"
        text += f"   👥 Участников: {lot['participants_count']}\n"
        if lot['finish_type'] == 'time':
            text += f"   ⏳ Финиш: {lot['finish_value']}\n"
        else:
            text += f"   🎯 Финиш: когда наберется {lot['finish_value']} чел.\n"
        text += "-------------------\n"
        kb.button(text=f"Перейти к Лоту #{lot['id']}", url=f"https://t.me/{me.username}?start=lot_{lot['id']}")

    kb.button(text="🔙 Назад в меню", callback_data="to_start")
    kb.adjust(1)
    await c.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "view_reviews")
async def process_view_reviews(c: CallbackQuery):
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

    text = "💬 <b>ОТЗЫВЫ ПОБЕДИТЕЛЕЙ:</b>\n\n"
    for r in reviews:
        # ИСПОЛЬЗУЕМ pyhtml
        text += f"👤 <b>{pyhtml.escape(r['full_name'])}</b> (Лот #{r['lot_id']}):\n<i>{pyhtml.escape(r['text'])}</i>\n\n"
    
    await c.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "to_start")
async def process_back_to_start(c: CallbackQuery, state: FSMContext):
    await state.clear()
    try: await c.message.delete()
    except: pass
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
    
    msg = (f"👤 <b>ЛИЧНЫЙ КАБИНЕТ</b>\n\n"
           f"🆔 ID: <code>{uid}</code>\n"
           f"🎫 Участий в лотах: <b>{p_cnt}</b>\n"
           f"🏆 Побед: <b>{w_cnt}</b>\n"
           f"👥 Рефералов: <b>{user['refs_count']}</b>\n\n"
           f"🔗 <b>Твоя ссылка для друзей:</b>\n<code>{ref_link}</code>")
    
    kb = [
        [InlineKeyboardButton(text="👥 Мои рефералы", callback_data="my_refs_list")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="to_start")]
    ]
    await c.message.edit_text(msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")

@dp.callback_query(F.data == "my_refs_list")
async def show_refs_list(c: CallbackQuery):
    uid = c.from_user.id
    refs = db_query("SELECT full_name, username, created_at FROM users WHERE referrer_id = ? ORDER BY created_at DESC LIMIT 40", (uid,), fetchall=True)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 Назад", callback_data="my_stats")
    
    if not refs:
        return await c.message.edit_text("😔 Вы еще никого не пригласили.", reply_markup=kb.as_markup())
    
    text = "👥 <b>ВАШИ РЕФЕРАЛЫ (Топ 40):</b>\n\n"
    for i, r in enumerate(refs, 1):
        d = r['created_at'].split()[0]
        u = f"(@{r['username']})" if r['username'] else ""
        # ИСПОЛЬЗУЕМ pyhtml
        text += f"{i}. {pyhtml.escape(r['full_name'])} {u} — {d}\n"
        
    await c.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "apply_pr")
async def pr_step1(c: CallbackQuery, state: FSMContext):
    await state.set_state(PRApplication.age)
    await c.message.edit_text("📝 <b>ЗАЯВКА НА СОТРУДНИЧЕСТВО</b>\n\n1. Напишите ваш возраст:", parse_mode="HTML")

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
        caption = (f"📩 <b>НОВАЯ ЗАЯВКА PR</b>\n\n"
                   f"👤 От: {pyhtml.escape(m.from_user.full_name)} (@{m.from_user.username})\n"
                   f"🎂 Возраст: {pyhtml.escape(data['age'])}\n"
                   f"🔗 Канал/Ник: {pyhtml.escape(data['nick'])}\n"
                   f"🆔 ID: <code>{m.from_user.id}</code>")
        try:
            await bot.send_photo(PR_CHAT_ID, m.photo[-1].file_id, caption=caption, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Ошибка отправки PR заявки: {e}")

    await m.answer("✅ Заявка успешно отправлена администраторам!")
    await state.clear()

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
# 8. АДМИН-ПАНЕЛЬ
# =================================================================
@dp.callback_query(F.data == "admin_main")
async def admin_main_menu(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS: return await c.answer("⛔ Доступ запрещен")
    
    kb = [
        [InlineKeyboardButton(text="➕ Создать лот", callback_data="adm_create")],
        [InlineKeyboardButton(text="📝 Управление лотами", callback_data="adm_manage_lots")],
        [InlineKeyboardButton(text="🔍 Поиск (ID/@user)", callback_data="adm_search_user")],
        [InlineKeyboardButton(text="📩 Написать в ЛС", callback_data="adm_dm_user")],
        [InlineKeyboardButton(text="📨 Рассылка", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="to_start")]
    ]
    await c.message.edit_text("🛠 <b>АДМИН ПАНЕЛЬ</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")

# --- СПИСОК ЛОТОВ ---
@dp.callback_query(F.data == "adm_manage_lots")
async def admin_list_lots(c: CallbackQuery):
    lots = db_query("SELECT * FROM lotteries ORDER BY id DESC LIMIT 20", fetchall=True)
    builder = InlineKeyboardBuilder()
    if not lots:
        await c.answer("Нет лотов", show_alert=True)
    else:
        for l in lots:
            status_emoji = "🟢" if l['status'] == 'active' else "🔴"
            builder.button(text=f"{status_emoji} #{l['id']} (Уч: {l['participants_count']})", callback_data=f"manage_{l['id']}")
            
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_main"))
    await c.message.edit_text("📝 <b>ВЫБЕРИТЕ ЛОТ:</b>", reply_markup=builder.as_markup(), parse_mode="HTML")

# --- УПРАВЛЕНИЕ ЛОТОМ ---
@dp.callback_query(F.data.startswith("manage_"))
async def admin_manage_single(c: CallbackQuery):
    lid = int(c.data.split("_")[1])
    l = db_query("SELECT * FROM lotteries WHERE id = ?", (lid,), fetchone=True)
    if not l: return await c.answer("Лот не найден", show_alert=True)

    info = (f"⚙️ <b>НАСТРОЙКИ ЛОТА #{lid}</b>\n\n"
            f"Статус: <b>{l['status']}</b>\n"
            f"👥 Участников: <b>{l['participants_count']}</b>\n"
            f"🏆 Победителей: <b>{l['winners_count']}</b>\n"
            f"🏁 Тип финиша: <b>{l['finish_type']}</b>\n"
            f"🎯 Значение: <b>{l['finish_value']}</b>\n"
            f"📢 Каналы: <code>{l['channels']}</code>")
            
    kb = [
        [InlineKeyboardButton(text="👥 Список участников", callback_data=f"listp_{lid}")],
        [InlineKeyboardButton(text="🏆 Изм. кол-во победителей", callback_data=f"edit_w_{lid}")],
        [InlineKeyboardButton(text="⏳ Изм. финиш", callback_data=f"edit_f_{lid}")],
        [InlineKeyboardButton(text="📢 Изм. каналы", callback_data=f"edit_s_{lid}")],
        [InlineKeyboardButton(text="🛑 ЗАВЕРШИТЬ СЕЙЧАС", callback_data=f"stop_{lid}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm_manage_lots")]
    ]
    
    # Если лот завершен, добавляем кнопку перевыбора
    if l['status'] == 'closed':
        kb.insert(4, [InlineKeyboardButton(text="🔄 ПЕРЕВЫБРАТЬ ПОБЕДИТЕЛЕЙ", callback_data=f"reroll_{lid}")])
        
    await c.message.edit_text(info, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")

# --- СПИСОК УЧАСТНИКОВ ---
@dp.callback_query(F.data.startswith("listp_"))
async def admin_show_participants(c: CallbackQuery):
    lid = int(c.data.split("_")[1])
    parts = db_query("SELECT full_name, user_id, username FROM participants WHERE lot_id = ? LIMIT 60", (lid,), fetchall=True)
    if not parts: return await c.answer("Нет участников.", show_alert=True)
    
    text = f"👥 <b>Участники #{lid} (первые 60):</b>\n\n"
    for p in parts:
        nick = f"(@{p['username']})" if p['username'] else ""
        # ИСПОЛЬЗУЕМ pyhtml
        safe_name = pyhtml.escape(p['full_name'])
        text += f"• {safe_name} {nick} [<code>{p['user_id']}</code>]\n"
        
    await c.message.answer(text, parse_mode="HTML")
    await c.answer()

# --- ПЕРЕВЫБОР ПОБЕДИТЕЛЕЙ ---
@dp.callback_query(F.data.startswith("reroll_"))
async def reroll_winners_handler(c: CallbackQuery):
    lid = int(c.data.split("_")[1])
    # Очищаем старых победителей
    db_query("DELETE FROM winners WHERE lot_id = ?", (lid,), commit=True)
    # Временно активируем статус (в памяти), но run_final_selection сама поставит closed
    await c.answer("🔄 Перевыбираем...", show_alert=True)
    await run_final_selection(lid)

# --- РЕДАКТИРОВАНИЕ ---
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
    prompt = "Введите новое время (в часах от сейчас):" if lot['finish_type'] == 'time' else "Введите новое кол-во участников:"
    await c.message.answer(prompt)

@dp.callback_query(F.data.startswith("edit_s_"))
async def edit_subs_init(c: CallbackQuery, state: FSMContext):
    lid = int(c.data.split("_")[2])
    await state.update_data(lot_id=lid, field="channels")
    await state.set_state(EditLotState.new_value)
    await c.message.answer("Новый список каналов (@a, @b) или 'нет':")

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
    await m.answer(f"✅ Лот #{lid} обновлен!")
    await state.clear()

@dp.callback_query(F.data.startswith("stop_"))
async def force_stop_lot(c: CallbackQuery):
    lid = int(c.data.split("_")[1])
    await run_final_selection(lid)
    await c.answer("Лот остановлен!", show_alert=True)
    await admin_list_lots(c)

# --- ЛС ПОЛЬЗОВАТЕЛЮ ---
@dp.callback_query(F.data == "adm_dm_user")
async def dm_user_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminDM.user_id)
    await c.message.answer("🆔 Введите ID пользователя, которому хотите написать:")

@dp.message(AdminDM.user_id)
async def dm_user_id_input(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("❌ ID должен быть числом.")
    await state.update_data(target_id=int(m.text))
    await state.set_state(AdminDM.message_text)
    await m.answer("📩 Введите текст сообщения:")

@dp.message(AdminDM.message_text)
async def dm_user_send(m: Message, state: FSMContext):
    data = await state.get_data()
    target = data['target_id']
    try:
        # ИСПОЛЬЗУЕМ pyhtml
        await bot.send_message(target, f"📩 <b>СООБЩЕНИЕ ОТ АДМИНИСТРАТОРА:</b>\n\n{pyhtml.escape(m.text)}", parse_mode="HTML")
        await m.answer("✅ Сообщение успешно отправлено!")
    except Exception as e:
        await m.answer(f"❌ Не удалось отправить (бот заблокирован?): {e}")
    await state.clear()

# --- ПОИСК ---
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
        
    if not u: return await m.answer("❌ Пользователь не найден.")
    
    uid = u['user_id']
    p_stat = db_query("SELECT COUNT(*) as c FROM participants WHERE user_id = ?", (uid,), fetchone=True)['c']
    w_stat = db_query("SELECT COUNT(*) as c FROM winners WHERE user_id = ?", (uid,), fetchone=True)['c']
    
    # ИСПОЛЬЗУЕМ pyhtml
    txt = (f"🕵️ <b>Info on {pyhtml.escape(u['full_name'])}</b>\n\n"
           f"🆔 ID: <code>{uid}</code>\n"
           f"🔗 User: @{u['username']}\n"
           f"📅 Дата реги: {u['created_at']}\n"
           f"🎲 Участий: {p_stat} | 🏆 Побед: {w_stat}\n"
           f"👥 Привел рефералов: {u['refs_count']}")
           
    await m.answer(txt, parse_mode="HTML")
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