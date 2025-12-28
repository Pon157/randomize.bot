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
from aiogram.types import (
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    Message, 
    CallbackQuery, 
    ContentType
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# --- 1. КОНФИГУРАЦИЯ И ЛОГИ ---
# Настраиваем подробный лог, чтобы в PM2 было видно каждое действие
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("LotteryBot")

load_dotenv()
# Токен берется из .env, как ты и просил
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
PR_CHAT_ID = int(os.getenv("PR_CHAT_ID", "0"))
LOT_CHANNEL = os.getenv("LOT_CHANNEL", "@lotsvitechek")

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- 2. БАЗА ДАННЫХ (ПОЛНАЯ СТРУКТУРА + МИГРАЦИИ) ---
def init_db():
    """Создание таблиц и проверка структуры на соответствие новым функциям"""
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
        win_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    
    # Таблица отзывов
    cur.execute("""CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        user_id INTEGER, 
        lot_id INTEGER, 
        text TEXT, 
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    
    # Таблица пользователей (с реферальной системой)
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, 
        username TEXT, 
        full_name TEXT, 
        referrer_id INTEGER DEFAULT 0,
        refs_count INTEGER DEFAULT 0,
        balance REAL DEFAULT 0.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # --- БЛОК МИГРАЦИЙ (Чтобы не было ошибок OperationalError) ---
    cur.execute("PRAGMA table_info(lotteries)")
    l_cols = [c[1] for c in cur.fetchall()]
    if 'winners_count' not in l_cols: cur.execute("ALTER TABLE lotteries ADD COLUMN winners_count INTEGER DEFAULT 1")
    if 'photo' not in l_cols: cur.execute("ALTER TABLE lotteries ADD COLUMN photo TEXT")
    if 'sticker' not in l_cols: cur.execute("ALTER TABLE lotteries ADD COLUMN sticker TEXT")
    if 'participants_count' not in l_cols: cur.execute("ALTER TABLE lotteries ADD COLUMN participants_count INTEGER DEFAULT 0")

    cur.execute("PRAGMA table_info(users)")
    u_cols = [c[1] for c in cur.fetchall()]
    if 'referrer_id' not in u_cols: cur.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER DEFAULT 0")
    if 'refs_count' not in u_cols: cur.execute("ALTER TABLE users ADD COLUMN refs_count INTEGER DEFAULT 0")
    
    conn.commit()
    conn.close()
    logger.info("База данных успешно инициализирована.")

def db_query(query, params=(), fetchone=False, fetchall=False, commit=False):
    """Обертка для удобной работы с SQLite"""
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
        logger.error(f"Ошибка БД: {e} | Запрос: {query}")
        return None

# --- 3. СОСТОЯНИЯ FSM (ПОЛНЫЙ СПИСОК) ---
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

class Broadcast(StatesGroup):
    message = State()

# --- 4. ВСПОМОГАТЕЛЬНАЯ ЛОГИКА ---
async def check_user_sub(user_id: int, channels_str: str):
    """Проверка подписки на обязательные каналы"""
    if not channels_str or channels_str.lower() in ['нет', 'none', '']:
        return True, []
    
    not_sub = []
    channels = [c.strip() for c in channels_str.split(",") if c.strip()]
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch, user_id)
            if member.status in ["left", "kicked"]:
                not_sub.append(ch)
        except Exception:
            not_sub.append(ch)
    return len(not_sub) == 0, not_sub

async def update_post_button(lot_id: int, count: int):
    """Обновление текста на кнопке в канале лотерей"""
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
        logger.debug(f"Не удалось обновить кнопку: {e}")

async def finish_giveaway_logic(lot_id: int):
    """Процедура выбора победителей и рассылки уведомлений"""
    lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or lot['status'] == 'closed': return
    
    participants = db_query("SELECT * FROM participants WHERE lot_id = ?", (lot_id,), fetchall=True)
    db_query("UPDATE lotteries SET status = 'closed' WHERE id = ?", (lot_id,), commit=True)
    
    if not participants:
        await bot.send_message(LOT_CHANNEL, f"🔔 Лотерея #{lot_id} завершена. Участников не было.")
        return

    # Выбор случайных счастливчиков
    win_count = min(len(participants), lot['winners_count'])
    winners = random.sample(participants, win_count)
    
    mentions = []
    for w in winners:
        db_query("INSERT INTO winners (lot_id, user_id) VALUES (?,?)", (lot_id, w['user_id']), commit=True)
        name = f"@{w['username']}" if w['username'] else w['full_name']
        mentions.append(name)
        
        try:
            review_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✍️ Оставить отзыв", callback_data=f"rev_{lot_id}")]
            ])
            await bot.send_message(w['user_id'], f"🎊 Ура! Ты победил в лотерее #{lot_id}!", reply_markup=review_kb)
        except Exception:
            pass

    win_text = (
        f"🎊 **ЛОТЕРЕЯ #{lot_id} ЗАВЕРШЕНА!**\n\n"
        f"🏆 Победители: {', '.join(mentions)}\n"
        f"📊 Всего участников: {len(participants)}\n\n"
        f"Бот отправил победителям инструкции в ЛС!"
    )
    
    try:
        await bot.send_message(LOT_CHANNEL, win_text, parse_mode="Markdown", reply_to_message_id=lot['message_id'])
    except Exception:
        await bot.send_message(LOT_CHANNEL, win_text, parse_mode="Markdown")

# --- 5. ОБРАБОТЧИКИ КНОПОК ПОЛЬЗОВАТЕЛЯ ---

@dp.callback_query(F.data == "my_stats")
async def process_my_stats(c: CallbackQuery):
    """Показывает профиль пользователя и реферальную ссылку"""
    uid = c.from_user.id
    user = db_query("SELECT * FROM users WHERE user_id = ?", (uid,), fetchone=True)
    
    # Считаем статистику через базу
    p_count = db_query("SELECT COUNT(*) as c FROM participants WHERE user_id = ?", (uid,), fetchone=True)['c']
    w_count = db_query("SELECT COUNT(*) as c FROM winners WHERE user_id = ?", (uid,), fetchone=True)['c']
    
    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start=ref{uid}"
    
    profile_text = (
        f"📊 **Ваш профиль в системе**\n\n"
        f"🆔 ID: `{uid}`\n"
        f"🎫 Участий в лотереях: **{p_count}**\n"
        f"🏆 Всего побед: **{w_count}**\n"
        f"👥 Приглашено друзей: **{user['refs_count'] if user else 0}**\n\n"
        f"🔗 **Твоя ссылка для приглашения:**\n`{ref_link}`\n\n"
        f"_Приглашай друзей и получай бонусы!_"
    )
    
    await c.message.answer(profile_text, parse_mode="Markdown")
    await c.answer()

@dp.callback_query(F.data == "active_lots")
async def process_active_lots(c: CallbackQuery):
    """Выводит список всех активных розыгрышей"""
    lots = db_query("SELECT * FROM lotteries WHERE status = 'active' ORDER BY id DESC", fetchall=True)
    
    if not lots:
        return await c.answer("Сейчас активных розыгрышей нет. Жди обновлений!", show_alert=True)
    
    msg = "📢 **Список активных лотерей:**\n\n"
    for l in lots:
        msg += f"🔹 **Лот #{l['id']}**\n   └ 🏆 Победителей: {l['winners_count']} | 👥 Участников: {l['participants_count']}\n"
    
    msg += f"\n👉 Участвуй в канале {LOT_CHANNEL}"
    await c.message.answer(msg, parse_mode="Markdown")
    await c.answer()

@dp.callback_query(F.data == "view_reviews")
async def process_view_reviews(c: CallbackQuery):
    """Показывает 10 последних отзывов"""
    reviews = db_query(
        "SELECT r.*, u.full_name FROM reviews r JOIN users u ON r.user_id = u.user_id ORDER BY r.id DESC LIMIT 10", 
        fetchall=True
    )
    if not reviews:
        return await c.answer("Отзывов пока нет. Будь первым!", show_alert=True)
    
    res = "💬 **Отзывы наших победителей:**\n\n"
    for r in reviews:
        res += f"👤 {r['full_name']} (Лот #{r['lot_id']}):\n«_{r['text']}_»\n{'-'*20}\n"
    
    await c.message.answer(res, parse_mode="Markdown")
    await c.answer()

# --- 6. КОМАНДА /START И РЕФЕРАЛЫ ---
@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    uid = message.from_user.id
    args = command.args
    
    # 1. Регистрация и реферальная проверка
    user_db = db_query("SELECT * FROM users WHERE user_id = ?", (uid,), fetchone=True)
    if not user_db:
        # Пытаемся достать ID пригласившего
        ref_id = 0
        if args and args.startswith("ref"):
            try:
                ref_id = int(args.replace("ref", ""))
                if ref_id == uid: ref_id = 0 # Сам себя не пригласишь
            except: pass
            
        db_query(
            "INSERT INTO users (user_id, username, full_name, referrer_id) VALUES (?,?,?,?)",
            (uid, message.from_user.username, message.from_user.full_name, ref_id),
            commit=True
        )
        
        if ref_id != 0:
            db_query("UPDATE users SET refs_count = refs_count + 1 WHERE user_id = ?", (ref_id,), commit=True)
            try:
                await bot.send_message(ref_id, "🤝 У вас новый реферал! За это полагается респект.")
            except: pass
        logger.info(f"Новый пользователь: {uid} (ref: {ref_id})")

    # 2. Если вход для участия в лотерее
    if args and args.startswith("lot_"):
        lid = int(args.split("_")[1])
        lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lid,), fetchone=True)
        
        if not lot or lot['status'] == 'closed':
            return await message.answer("❌ Извините, данный розыгрыш уже завершен или не существует.")
        
        # Проверка ОП (Обязательной Подписки)
        is_sub, channels = await check_user_sub(uid, lot['channels'])
        if not is_sub:
            kb = InlineKeyboardBuilder()
            for ch in channels:
                kb.button(text=f"📢 Подписаться на {ch}", url=f"https://t.me/{ch.lstrip('@')}")
            kb.button(text="🔄 Проверить подписку", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lid}")
            return await message.answer("⚠️ Чтобы участвовать, подпишитесь на наши ресурсы:", reply_markup=kb.adjust(1).as_markup())

        try:
            db_query(
                "INSERT INTO participants (user_id, lot_id, username, full_name) VALUES (?,?,?,?)",
                (uid, lid, message.from_user.username, message.from_user.full_name),
                commit=True
            )
            # Обновляем счетчик
            count_data = db_query("SELECT COUNT(*) as c FROM participants WHERE lot_id = ?", (lid,), fetchone=True)
            new_total = count_data['c']
            db_query("UPDATE lotteries SET participants_count = ? WHERE id = ?", (new_total, lid), commit=True)
            
            await update_post_button(lid, new_total)
            
            # Если финиш по количеству
            if lot['finish_type'] == 'count' and new_total >= int(lot['finish_value']):
                await finish_giveaway_logic(lid)
                
            await message.answer(f"✅ Успех! Ты зарегистрирован в лотерее #{lid}!")
        except sqlite3.IntegrityError:
            await message.answer("✅ Ты уже принимаешь участие в этой лотерее!")
        return

    # 3. Главное меню
    kb = [
        [InlineKeyboardButton(text="💬 Отзывы", callback_data="view_reviews"), InlineKeyboardButton(text="💼 PR Заявка", callback_data="apply_pr")],
        [InlineKeyboardButton(text="📢 Розыгрыши", callback_data="active_lots"), InlineKeyboardButton(text="📊 Мой профиль", callback_data="my_stats")]
    ]
    if uid in ADMIN_IDS:
        kb.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_main")])
        kb.append([InlineKeyboardButton(text="📩 Сделать рассылку", callback_data="adm_broadcast")])
    
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\nЯ бот для розыгрышей в канале {LOT_CHANNEL}.", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )

# --- 7. АДМИНИСТРАТИВНЫЙ БЛОК (СОЗДАНИЕ, НАСТРОЙКА, РАССЫЛКА) ---

@dp.callback_query(F.data == "admin_main")
async def process_admin_menu(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS: return
    kb = [
        [InlineKeyboardButton(text="➕ Создать новый лот", callback_data="adm_create")],
        [InlineKeyboardButton(text="📝 Список и редактор", callback_data="adm_list_edit")],
        [InlineKeyboardButton(text="🔙 В главное меню", callback_data="to_start")]
    ]
    await c.message.edit_text("🛠 **Меню администратора**", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")

@dp.callback_query(F.data == "adm_create")
async def adm_create_step1(c: CallbackQuery, state: FSMContext):
    await state.set_state(CreateLot.text)
    await c.message.answer("1️⃣ Пришлите пост. Это может быть Текст, Фото с подписью или Стикер:")

@dp.message(CreateLot.text)
async def adm_create_step2(m: Message, state: FSMContext):
    # Собираем медиа-данные
    data = {
        "text": m.caption or m.text or "",
        "entities": json.dumps([e.model_dump(mode='json') for e in (m.entities or m.caption_entities or [])]),
        "photo": m.photo[-1].file_id if m.photo else None,
        "sticker": m.sticker.file_id if m.sticker else None
    }
    await state.update_data(post=data)
    await state.set_state(CreateLot.winners_count)
    await m.answer("2️⃣ Укажите количество победителей (число):")

@dp.message(CreateLot.winners_count)
async def adm_create_step3(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("Нужно ввести число!")
    await state.update_data(wc=int(m.text))
    await state.set_state(CreateLot.channels)
    await m.answer("3️⃣ Укажите каналы для ОП через запятую (напр. @ch1, @ch2) или напишите 'нет':")

@dp.message(CreateLot.channels)
async def adm_create_step4(m: Message, state: FSMContext):
    await state.update_data(ch=m.text)
    kb = [
        [InlineKeyboardButton(text="⏰ По времени", callback_data="stype_time"), InlineKeyboardButton(text="👥 По участникам", callback_data="stype_count")]
    ]
    await m.answer("4️⃣ Выберите тип завершения лотереи:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("stype_"))
async def adm_create_step5(c: CallbackQuery, state: FSMContext):
    ftype = c.data.split("_")[1]
    await state.update_data(ft=ftype)
    txt = "Сколько часов будет идти розыгрыш?" if ftype == "time" else "При каком кол-ве участников завершить?"
    await c.message.answer(txt)
    await state.set_state(CreateLot.value)

@dp.message(CreateLot.value)
async def adm_create_finish(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("Введите число!")
    
    s_data = await state.get_data()
    post = s_data['post']
    
    val = m.text
    if s_data['ft'] == "time":
        val = (datetime.now() + timedelta(hours=int(m.text))).strftime("%d.%m.%Y %H:%M")

    # Сохраняем (ИСПРАВЛЕННЫЙ ЗАПРОС)
    lid = db_query(
        "INSERT INTO lotteries (text, entities, channels, finish_type, finish_value, photo, sticker, winners_count) VALUES (?,?,?,?,?,?,?,?)",
        (post['text'], post['entities'], s_data['ch'], s_data['ft'], val, post['photo'], post['sticker'], s_data['wc']),
        commit=True
    )
    
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
        await m.answer(f"✅ Лот #{lid} успешно опубликован!")
    except Exception as e:
        await m.answer(f"❌ Ошибка отправки: {e}")
    
    await state.clear()

# --- 8. РАССЫЛКА ПО ВСЕМ ПОЛЬЗОВАТЕЛЯМ ---
@dp.callback_query(F.data == "adm_broadcast")
async def broad_step1(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS: return
    await state.set_state(Broadcast.message)
    await c.message.answer("Отправьте сообщение (текст/фото), которое увидят ВСЕ пользователи бота:")

@dp.message(Broadcast.message)
async def broad_step2(m: Message, state: FSMContext):
    users = db_query("SELECT user_id FROM users", fetchall=True)
    count = 0
    await m.answer(f"🚀 Начинаю рассылку на {len(users)} чел...")
    
    for u in users:
        try:
            await m.copy_to(u['user_id'])
            count += 1
            await asyncio.sleep(0.05) # Защита от спам-фильтра
        except: pass
        
    await m.answer(f"🏁 Рассылка завершена. Доставлено: {count}")
    await state.clear()

# --- 9. PR ЗАЯВКИ И ОТЗЫВЫ ---
@dp.callback_query(F.data == "apply_pr")
async def pr_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(PRApplication.age)
    await c.message.answer("Заявка на PR.\n1. Ваш возраст?")

@dp.message(PRApplication.age)
async def pr_age(m: Message, state: FSMContext):
    await state.update_data(age=m.text)
    await state.set_state(PRApplication.nickname)
    await m.answer("2. Ссылка на ваш канал или никнейм?")

@dp.message(PRApplication.nickname)
async def pr_nick(m: Message, state: FSMContext):
    await state.update_data(nick=m.text)
    await state.set_state(PRApplication.proofs)
    await m.answer("3. Пришлите скриншот статистики:")

@dp.message(PRApplication.proofs, F.content_type == ContentType.PHOTO)
async def pr_final(m: Message, state: FSMContext):
    d = await state.get_data()
    info = (f"📩 **НОВАЯ ЗАЯВКА PR**\n\n"
            f"👤 От: @{m.from_user.username}\n"
            f"🔞 Возраст: {d['age']}\n"
            f"🔗 Канал: {d['nick']}")
    if PR_CHAT_ID:
        await bot.send_photo(PR_CHAT_ID, m.photo[-1].file_id, caption=info)
    await m.answer("✅ Ваша заявка отправлена на рассмотрение!")
    await state.clear()

@dp.callback_query(F.data.startswith("rev_"))
async def rev_start(c: CallbackQuery, state: FSMContext):
    lid = c.data.split("_")[1]
    await state.update_data(rlid=lid)
    await state.set_state(LeaveReview.text)
    await c.message.answer("Напишите ваше впечатление о выигрыше:")

@dp.message(LeaveReview.text)
async def rev_save(m: Message, state: FSMContext):
    d = await state.get_data()
    db_query("INSERT INTO reviews (user_id, lot_id, text) VALUES (?,?,?)", (m.from_user.id, d['rlid'], m.text), commit=True)
    await m.answer("✅ Спасибо за отзыв! Он появится в списке.")
    await state.clear()

@dp.callback_query(F.data == "to_start")
async def back_to_start(c: CallbackQuery, state: FSMContext):
    await cmd_start(c.message, CommandObject(command="start"), state)
    await c.message.delete()

# --- 10. ФОНОВЫЕ ЗАДАЧИ (ШЕДУЛЕР) ---
async def time_checker():
    """Раз в минуту проверяет, не пора ли завершить розыгрыш по времени"""
    while True:
        try:
            active_lots = db_query("SELECT * FROM lotteries WHERE status = 'active' AND finish_type = 'time'", fetchall=True)
            now = datetime.now()
            for l in active_lots:
                try:
                    f_time = datetime.strptime(l['finish_value'], "%d.%m.%Y %H:%M")
                    if now >= f_time:
                        await finish_giveaway_logic(l['id'])
                except: continue
        except Exception as e:
            logger.error(f"Ошибка шедулера: {e}")
        await asyncio.sleep(60)

# --- 11. ЗАПУСК БОТА ---
async def main():
    init_db()
    # Запуск фонового процесса проверки времени
    asyncio.create_task(time_checker())
    
    logger.info("Бот запущен и готов к работе!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен пользователем.")
