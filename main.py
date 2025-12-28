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

# --- 1. КОНФИГУРАЦИЯ ---
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
PR_CHAT_ID = int(os.getenv("PR_CHAT_ID", "0"))
LOT_CHANNEL = os.getenv("LOT_CHANNEL", "@lotsvitechek")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- 2. БАЗА ДАННЫХ (ПОЛНАЯ СТРУКТУРА) ---
def init_db():
    with sqlite3.connect("bot_database.db") as conn:
        cur = conn.cursor()
        # Таблица лотерей
        cur.execute("""CREATE TABLE IF NOT EXISTS lotteries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT, entities TEXT, channels TEXT,
            finish_type TEXT, finish_value TEXT,
            status TEXT DEFAULT 'active', message_id INTEGER,
            photo TEXT, sticker TEXT, winners_count INTEGER DEFAULT 1,
            participants_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        # Таблица участников
        cur.execute("""CREATE TABLE IF NOT EXISTS participants (
            user_id INTEGER, lot_id INTEGER, username TEXT, full_name TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, 
            PRIMARY KEY (user_id, lot_id)
        )""")
        # Таблица победителей
        cur.execute("""CREATE TABLE IF NOT EXISTS winners (
            lot_id INTEGER, user_id INTEGER, 
            PRIMARY KEY (lot_id, user_id)
        )""")
        # Таблица отзывов
        cur.execute("""CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, 
            lot_id INTEGER, text TEXT, 
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        # Таблица пользователей
        cur.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, 
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.commit()

def db_query(query, params=(), fetchone=False, fetchall=False, commit=False):
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

class AdminStates(StatesGroup):
    broadcast_text = State()

# --- 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
async def update_lottery_button(lot_id: int, count: int):
    lot = db_query("SELECT message_id FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or not lot['message_id']: return
    kb = [[InlineKeyboardButton(text=f"✅ Участвовать! ({count})", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")]]
    try:
        await bot.edit_message_reply_markup(chat_id=LOT_CHANNEL, message_id=lot['message_id'], reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    except Exception as e:
        logging.error(f"Ошибка обновления кнопки: {e}")

async def check_sub(user_id: int, channels_str: str):
    if not channels_str or channels_str.lower() in ['нет', 'none', '']: return True, []
    not_sub = []
    channels = [c.strip() for c in channels_str.split(",") if c.strip()]
    for ch in channels:
        try:
            m = await bot.get_chat_member(ch, user_id)
            if m.status in ["left", "kicked"]: not_sub.append(ch)
        except: not_sub.append(ch)
    return len(not_sub) == 0, not_sub

async def finish_giveaway(lot_id: int):
    lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or lot['status'] == 'closed': return
    
    parts = db_query("SELECT * FROM participants WHERE lot_id = ?", (lot_id,), fetchall=True)
    db_query("UPDATE lotteries SET status = 'closed' WHERE id = ?", (lot_id,), commit=True)
    
    if not parts:
        try: await bot.send_message(LOT_CHANNEL, f"🔔 Розыгрыш #{lot_id} завершен. Участников не было.")
        except: pass
        return

    count_to_pick = min(len(parts), lot['winners_count'])
    winners_list = random.sample(parts, count_to_pick)
    
    mentions = []
    for w in winners_list:
        db_query("INSERT OR IGNORE INTO winners (lot_id, user_id) VALUES (?,?)", (lot_id, w['user_id']), commit=True)
        mentions.append(f"@{w['username']}" if w['username'] else w['full_name'])
        try:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✍️ Оставить отзыв", callback_data=f"rev_{lot_id}")]])
            await bot.send_message(w['user_id'], f"🎉 Поздравляем! Ты выиграл в лотерее #{lot_id}!\nПожалуйста, оставь отзыв по кнопке ниже:", reply_markup=kb)
        except: pass

    winners_text = ", ".join(mentions)
    res_msg = f"🎊 **Итоги лотереи #{lot_id}!**\n\n🏆 Победители: {winners_text}\n📊 Всего участников: {len(parts)}\n\nСпасибо всем за участие!"
    try:
        await bot.send_message(LOT_CHANNEL, res_msg, parse_mode="Markdown", reply_to_message_id=lot['message_id'])
    except:
        await bot.send_message(LOT_CHANNEL, res_msg, parse_mode="Markdown")

# --- 5. ОБРАБОТКА КОМАНД И ВХОДА ---
@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    user_id = message.from_user.id
    db_query("INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?,?,?)", 
             (user_id, message.from_user.username, message.from_user.full_name), commit=True)

    if command.args and command.args.startswith("lot_"):
        lot_id = int(command.args.split("_")[1])
        lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
        if not lot or lot['status'] == 'closed': 
            return await message.answer("⚠️ Извини, этот розыгрыш уже завершен.")

        is_ok, not_sub = await check_sub(user_id, lot['channels'])
        if not is_ok:
            kb = InlineKeyboardBuilder()
            for ch in not_sub: kb.button(text=f"📢 Подписаться на {ch}", url=f"https://t.me/{ch.lstrip('@')}")
            kb.button(text="🔄 Я подписался", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")
            return await message.answer("❌ Для участия нужно подписаться на каналы:", reply_markup=kb.adjust(1).as_markup())

        try:
            db_query("INSERT INTO participants (user_id, lot_id, username, full_name) VALUES (?,?,?,?)", 
                     (user_id, lot_id, message.from_user.username, message.from_user.full_name), commit=True)
            
            # Обновляем счетчик
            count_res = db_query("SELECT COUNT(*) as c FROM participants WHERE lot_id=?", (lot_id,), fetchone=True)
            new_count = count_res['c']
            db_query("UPDATE lotteries SET participants_count = ? WHERE id = ?", (new_count, lot_id), commit=True)
            await update_lottery_button(lot_id, new_count)
            
            # Если финиш по количеству
            if lot['finish_type'] == 'count' and new_count >= int(lot['finish_value']):
                await finish_giveaway(lot_id)
            
            await message.answer(f"✅ Готово! Ты участвуешь в лотерее #{lot_id}!")
        except sqlite3.IntegrityError:
            await message.answer("✅ Ты уже зарегистрирован в этом розыгрыше.")
        return

    await state.clear()
    kb = [
        [InlineKeyboardButton(text="💬 Отзывы", callback_data="view_reviews"), InlineKeyboardButton(text="💼 PR Заявка", callback_data="apply_pr")],
        [InlineKeyboardButton(text="📢 Розыгрыши", callback_data="active_lots"), InlineKeyboardButton(text="📊 Мой профиль", callback_data="my_stats")]
    ]
    if user_id in ADMIN_IDS:
        kb.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_main")])
    
    await message.answer(f"👋 Привет, {message.from_user.first_name}!\nДобро пожаловать в бота лотерей.", 
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

# --- 6. АДМИН-ПАНЕЛЬ (ПОЛНАЯ) ---
@dp.callback_query(F.data == "admin_main")
async def admin_panel(c: CallbackQuery):
    kb = [
        [InlineKeyboardButton(text="➕ Создать новый лот", callback_data="adm_create")],
        [InlineKeyboardButton(text="📝 Изменить активный лот", callback_data="adm_edit_list")],
        [InlineKeyboardButton(text="📊 Детальная статистика", callback_data="adm_full_stats")],
        [InlineKeyboardButton(text="📨 Рассылка", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="start_back")]
    ]
    await c.message.edit_text("🛠 **Панель администратора**", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")

@dp.callback_query(F.data == "adm_create")
async def adm_create_1(c: CallbackQuery, state: FSMContext):
    await state.set_state(CreateLot.text)
    await c.message.answer("1. Отправь сообщение для канала (Текст, Фото с описанием или любой Стикер):")

@dp.message(CreateLot.text)
async def adm_create_2(m: Message, state: FSMContext):
    data = {
        "p": m.photo[-1].file_id if m.photo else None,
        "s": m.sticker.file_id if m.sticker else None,
        "t": m.caption or m.text or "",
        "e": json.dumps([e.model_dump(mode='json') for e in (m.entities or m.caption_entities or [])])
    }
    await state.update_data(**data)
    await state.set_state(CreateLot.winners_count)
    await m.answer("2. Сколько победителей будет в этом розыгрыше? (напиши число):")

@dp.message(CreateLot.winners_count)
async def adm_create_3(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("Введите число!")
    await state.update_data(wc=int(m.text))
    await state.set_state(CreateLot.channels)
    await m.answer("3. Введите юзернеймы каналов для подписки через запятую (напр. @chan1, @chan2) или напиши 'нет':")

@dp.message(CreateLot.channels)
async def adm_create_4(m: Message, state: FSMContext):
    await state.update_data(ch=m.text)
    kb = [
        [InlineKeyboardButton(text="⏰ По времени (часы)", callback_data="st_time")],
        [InlineKeyboardButton(text="👥 По кол-ву участников", callback_data="st_count")]
    ]
    await m.answer("4. Выберите способ завершения розыгрыша:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("st_"))
async def adm_create_5(c: CallbackQuery, state: FSMContext):
    ftype = c.data.split("_")[1]
    await state.update_data(ft=ftype)
    txt = "Введите кол-во часов (напр. 24):" if ftype == 'time' else "Введите кол-во участников (напр. 100):"
    await c.message.answer(txt)
    await state.set_state(CreateLot.value)

@dp.message(CreateLot.value)
async def adm_create_final(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("Введите число!")
    d = await state.get_data()
    
    val = m.text
    if d['ft'] == 'time':
        val = (datetime.now() + timedelta(hours=int(m.text))).strftime("%d.%m.%Y %H:%M")

    # Сохранение
    lid = db_query("INSERT INTO lotteries (text, entities, channels, finish_type, finish_value, photo, sticker, winners_count) VALUES (?,?,?,?,?,?,?,?)",
                  (d['t'], d['e'], d['ch'], d['ft'], val, d['p'], d['s'], d['wc']), commit=True)
    
    kb = [[InlineKeyboardButton(text="✅ Участвовать! (0)", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lid}")]]
    
    try:
        if d['p']:
            sent = await bot.send_photo(LOT_CHANNEL, d['p'], caption=d['t'], reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        elif d['s']:
            await bot.send_sticker(LOT_CHANNEL, d['s'])
            sent = await bot.send_message(LOT_CHANNEL, "🎁 Участвуй в новом розыгрыше!", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        else:
            sent = await bot.send_message(LOT_CHANNEL, d['t'], reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        
        db_query("UPDATE lotteries SET message_id = ? WHERE id = ?", (sent.message_id, lid), commit=True)
        await m.answer(f"✅ Лот #{lid} успешно запущен!")
    except Exception as e:
        await m.answer(f"❌ Ошибка отправки в канал: {e}")
    
    await state.clear()

# --- 7. РЕДАКТИРОВАНИЕ И СТАТИСТИКА (ПОЛНОЕ) ---
@dp.callback_query(F.data == "adm_edit_list")
async def adm_edit_list(c: CallbackQuery):
    lots = db_query("SELECT * FROM lotteries WHERE status='active'", fetchall=True)
    if not lots: return await c.answer("Нет активных лотов для редактирования", show_alert=True)
    
    kb = InlineKeyboardBuilder()
    for l in lots:
        kb.button(text=f"⚙️ Лот #{l['id']}", callback_data=f"edch_{l['id']}")
    await c.message.edit_text("Выберите лот для изменения настроек:", reply_markup=kb.adjust(2).as_markup())

@dp.callback_query(F.data.startswith("edch_"))
async def adm_edit_choice(c: CallbackQuery):
    lid = c.data.split("_")[1]
    kb = InlineKeyboardBuilder()
    kb.button(text="Изменить Каналы", callback_data=f"edset_{lid}_channels")
    kb.button(text="Изменить Лимит/Время", callback_data=f"edset_{lid}_finish_value")
    kb.button(text="Изменить кол-во победителей", callback_data=f"edset_{lid}_winners_count")
    kb.button(text="🛑 Остановить сейчас", callback_data=f"edset_{lid}_stop")
    kb.button(text="🔙 Назад", callback_data="adm_edit_list")
    await c.message.edit_text(f"Управление лотом #{lid}:", reply_markup=kb.adjust(1).as_markup())

@dp.callback_query(F.data.startswith("edset_"))
async def adm_edit_field(c: CallbackQuery, state: FSMContext):
    _, lid, field = c.data.split("_")
    if field == "stop":
        await finish_giveaway(int(lid))
        return await c.message.answer(f"✅ Лот #{lid} принудительно завершен!")
    
    await state.update_data(lid=lid, field=field)
    await state.set_state(EditLot.new_value)
    await c.message.answer(f"Введите новое значение для поля '{field}':\n(Для времени формат: DD.MM.YYYY HH:MM)")

@dp.message(EditLot.new_value)
async def adm_edit_save(m: Message, state: FSMContext):
    d = await state.get_data()
    try:
        db_query(f"UPDATE lotteries SET {d['field']} = ? WHERE id = ?", (m.text, d['lid']), commit=True)
        await m.answer("✅ Значение успешно обновлено!")
    except Exception as e:
        await m.answer(f"❌ Ошибка базы: {e}")
    await state.clear()

@dp.callback_query(F.data == "adm_full_stats")
async def adm_full_stats(c: CallbackQuery):
    lots = db_query("SELECT * FROM lotteries ORDER BY id DESC LIMIT 15", fetchall=True)
    txt = "📊 **Статистика последних 15 лотов:**\n\n"
    for l in lots:
        txt += f"🔹 #{l['id']} | {l['status']} | 👥 {l['participants_count']} чел. | 🏆 {l['winners_count']} мест\n"
    await c.message.answer(txt, parse_mode="Markdown")

# --- 8. ОТЗЫВЫ, PR И ПРОЧЕЕ ---
@dp.callback_query(F.data == "view_reviews")
async def view_reviews(c: CallbackQuery):
    revs = db_query("SELECT r.*, u.full_name FROM reviews r JOIN users u ON r.user_id = u.user_id ORDER BY r.id DESC LIMIT 10", fetchall=True)
    if not revs: return await c.answer("Отзывов пока нет. Стань первым!", show_alert=True)
    
    txt = "💬 **Последние отзывы победителей:**\n\n"
    for r in revs:
        txt += f"👤 {r['full_name']} (Лот #{r['lot_id']}):\n«{r['text']}»\n{'-'*20}\n"
    await c.message.answer(txt, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("rev_"))
async def leave_rev_1(c: CallbackQuery, state: FSMContext):
    lid = c.data.split("_")[1]
    # Проверка, реально ли юзер победил
    win = db_query("SELECT * FROM winners WHERE lot_id=? AND user_id=?", (lid, c.from_user.id), fetchone=True)
    if not win: return await c.answer("❌ Ты не являешься победителем этого лота!", show_alert=True)
    
    await state.update_data(lid=lid)
    await state.set_state(LeaveReview.text)
    await c.message.answer("Напиши свой отзыв о выигрыше:")

@dp.message(LeaveReview.text)
async def leave_rev_2(m: Message, state: FSMContext):
    d = await state.get_data()
    db_query("INSERT INTO reviews (user_id, lot_id, text) VALUES (?,?,?)", (m.from_user.id, d['lid'], m.text), commit=True)
    await m.answer("✅ Спасибо! Твой отзыв опубликован в разделе '💬 Отзывы'.")
    await state.clear()

@dp.callback_query(F.data == "apply_pr")
async def pr_app_1(c: CallbackQuery, state: FSMContext):
    await state.set_state(PRApplication.age)
    await c.message.answer("Начинаем заполнение анкеты PR.\n1. Сколько тебе лет?")

@dp.message(PRApplication.age)
async def pr_app_2(m: Message, state: FSMContext):
    await state.update_data(age=m.text)
    await state.set_state(PRApplication.nickname)
    await m.answer("2. Твой никнейм или ссылка на канал:")

@dp.message(PRApplication.nickname)
async def pr_app_3(m: Message, state: FSMContext):
    await state.update_data(nick=m.text)
    await state.set_state(PRApplication.proofs)
    await m.answer("3. Пришли скриншот статистики (одним фото):")

@dp.message(PRApplication.proofs, F.photo)
async def pr_app_final(m: Message, state: FSMContext):
    d = await state.get_data()
    caption = f"📩 **Новая заявка на PR!**\n\n👤 От: {m.from_user.full_name} (@{m.from_user.username})\n🔞 Возраст: {d['age']}\n🔗 Ссылка: {d['nick']}"
    if PR_CHAT_ID:
        await bot.send_photo(PR_CHAT_ID, m.photo[-1].file_id, caption=caption)
    await m.answer("✅ Заявка успешно отправлена админам!")
    await state.clear()

@dp.callback_query(F.data == "start_back")
async def back_to_start(c: CallbackQuery, state: FSMContext):
    await cmd_start(c.message, CommandObject(command="start", args=None), state)
    await c.message.delete()

# --- 9. ФОНОВЫЙ ПРОВЕРЩИК ВРЕМЕНИ ---
async def scheduler():
    while True:
        try:
            active_lots = db_query("SELECT * FROM lotteries WHERE status='active' AND finish_type='time'", fetchall=True)
            now = datetime.now()
            for lot in active_lots:
                f_time = datetime.strptime(lot['finish_value'], "%d.%m.%Y %H:%M")
                if now >= f_time:
                    await finish_giveaway(lot['id'])
        except Exception as e:
            logging.error(f"Ошибка в планировщике: {e}")
        await asyncio.sleep(30)

# --- 10. ЗАПУСК БОТА ---
async def main():
    init_db()
    asyncio.create_task(scheduler())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот выключен")
