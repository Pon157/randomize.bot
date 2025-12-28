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
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
PR_CHAT_ID = os.getenv("PR_CHAT_ID", "0")
PR_CHAT_ID = int(PR_CHAT_ID) if PR_CHAT_ID.lstrip("-").isdigit() else 0
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
            text TEXT, entities TEXT, channels TEXT,
            finish_type TEXT, finish_value TEXT,
            status TEXT DEFAULT 'active', message_id INTEGER,
            photo TEXT, sticker TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            winner_id INTEGER, participants_count INTEGER DEFAULT 0
        )""")
        # Таблица участников
        cur.execute("""CREATE TABLE IF NOT EXISTS participants (
            user_id INTEGER, lot_id INTEGER, username TEXT, full_name TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, lot_id)
        )""")
        # Таблица пользователей бота
        cur.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
            is_admin BOOLEAN DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        
        # Миграция: добавляем колонки если база уже была создана раньше
        try: cur.execute("ALTER TABLE lotteries ADD COLUMN photo TEXT")
        except: pass
        try: cur.execute("ALTER TABLE lotteries ADD COLUMN sticker TEXT")
        except: pass
        
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
    broadcast_text = State()

# --- 4. УТИЛИТЫ ---
async def update_lottery_button(lot_id: int, count: int):
    """Обновляет текст кнопки с количеством участников в канале"""
    lot = db_query("SELECT message_id FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or not lot['message_id']: return
    
    kb = [[InlineKeyboardButton(text=f"✅ Участвовать! ({count})", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")]]
    try:
        await bot.edit_message_reply_markup(chat_id=LOT_CHANNEL, message_id=lot['message_id'], reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    except Exception as e:
        logging.error(f"Ошибка обновления кнопки: {e}")

async def check_user_subscription(user_id: int, channels_str: str) -> tuple:
    if not channels_str: return True, []
    channels = [c.strip() for c in channels_str.split(",") if c.strip()]
    not_subscribed = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch, user_id)
            if member.status in ["left", "kicked"]: not_subscribed.append(ch)
        except Exception: not_subscribed.append(ch)
    return len(not_subscribed) == 0, not_subscribed

# --- 5. ЗАВЕРШЕНИЕ РОЗЫГРЫША ---
async def finish_giveaway(lot_id: int, manual: bool = False):
    lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
    if not lot or lot['status'] == 'closed': return

    participants = db_query("SELECT user_id, username, full_name FROM participants WHERE lot_id = ?", (lot_id,), fetchall=True)
    db_query("UPDATE lotteries SET status = 'closed' WHERE id = ?", (lot_id,), commit=True)

    if not participants:
        try: await bot.send_message(LOT_CHANNEL, f"🔔 Лотерея #{lot_id} окончена. Участников нет.", reply_to_message_id=lot['message_id'])
        except: pass
        return

    winner = random.choice(participants)
    db_query("UPDATE lotteries SET winner_id = ?, participants_count = ? WHERE id = ?", (winner['user_id'], len(participants), lot_id), commit=True)

    mention = f"@{winner['username']}" if winner['username'] else f"[{winner['full_name']}](tg://user?id={winner['user_id']})"
    
    # Формируем список участников (до 10 человек)
    p_list = "\n".join([f"• @{p['username']}" if p['username'] else f"• {p['full_name']}" for p in participants[:10]])
    if len(participants) > 10: p_list += f"\n... и еще {len(participants)-10}"

    text = (f"🎊 **Итоги розыгрыша #{lot_id}!**\n"
            f"🏆 **Победитель:** {mention}\n"
            f"📊 Участников всего: {len(participants)}\n\n"
            f"👥 **Участники:**\n{p_list}\n"
            f"{'*(Завершено вручную)*' if manual else ''}")

    try:
        await bot.send_message(LOT_CHANNEL, text, parse_mode="Markdown", reply_to_message_id=lot['message_id'])
        await bot.send_message(winner['user_id'], f"🎉 Поздравляем! Вы победили в лотерее #{lot_id}!")
    except Exception as e:
        logging.error(f"Ошибка при завершении: {e}")

# --- 6. ОБРАБОТКА /START И УЧАСТИЯ ---
@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    user_id = message.from_user.id
    db_query("INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?, ?, ?)", 
             (user_id, message.from_user.username, message.from_user.full_name), commit=True)

    if command.args and command.args.startswith("lot_"):
        try:
            lot_id = int(command.args.split("_")[1])
        except: return

        lot = db_query("SELECT * FROM lotteries WHERE id = ?", (lot_id,), fetchone=True)
        if not lot or lot['status'] == 'closed': 
            return await message.answer("⚠️ Этот розыгрыш уже завершен!")

        # Проверка подписки
        is_sub, not_sub = await check_user_subscription(user_id, lot['channels'])
        if not is_sub:
            kb = InlineKeyboardBuilder()
            for ch in not_sub: kb.button(text=f"📢 {ch}", url=f"https://t.me/{ch.lstrip('@')}")
            kb.button(text="🔄 Проверить подписку", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")
            kb.adjust(1)
            return await message.answer("❌ Для участия подпишитесь на каналы:", reply_markup=kb.as_markup())

        # Регистрация участника
        try:
            db_query("INSERT INTO participants (user_id, lot_id, username, full_name) VALUES (?,?,?,?)",
                     (user_id, lot_id, message.from_user.username, message.from_user.full_name), commit=True)
            
            # Считаем новых участников и обновляем кнопку
            count_data = db_query("SELECT COUNT(*) as c FROM participants WHERE lot_id=?", (lot_id,), fetchone=True)
            new_count = count_data['c']
            db_query("UPDATE lotteries SET participants_count = ? WHERE id = ?", (new_count, lot_id), commit=True)
            
            await update_lottery_button(lot_id, new_count)
            
            # Проверка завершения по количеству
            if lot['finish_type'] == 'count' and new_count >= int(lot['finish_value']):
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
    
    await message.answer(f"👋 Привет, {message.from_user.first_name}!", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

# --- 7. СОЗДАНИЕ ЛОТЕРЕИ (АДМИН) ---
@dp.callback_query(F.data == "admin_create", F.from_user.id.in_(ADMIN_IDS))
async def admin_create_init(callback: CallbackQuery, state: FSMContext):
    await state.set_state(CreateLot.text)
    await callback.message.answer("Отправьте пост розыгрыша. Это может быть текст, фото с описанием или (премиум) стикер:")
    await callback.answer()

@dp.message(CreateLot.text)
async def admin_create_text(message: Message, state: FSMContext):
    data = {"photo": None, "sticker": None, "text": "", "entities": "[]"}
    
    if message.photo:
        data["photo"] = message.photo[-1].file_id
        data["text"] = message.caption or ""
        data["entities"] = json.dumps([e.model_dump(mode='json') for e in (message.caption_entities or [])])
    elif message.sticker:
        data["sticker"] = message.sticker.file_id
        data["text"] = "Розыгрыш по стикеру"
    else:
        data["text"] = message.text or ""
        data["entities"] = json.dumps([e.model_dump(mode='json') for e in (message.entities or [])])

    await state.update_data(**data)
    await state.set_state(CreateLot.channels)
    await message.answer("2. Введите каналы для подписки через запятую (например @chan1, @chan2) или напишите 'нет':")

@dp.message(CreateLot.channels)
async def admin_create_channels(message: Message, state: FSMContext):
    ch = "" if message.text.lower() == 'нет' else message.text
    await state.update_data(channels=ch)
    
    kb = [
        [InlineKeyboardButton(text="⏰ По времени", callback_data="type_time")],
        [InlineKeyboardButton(text="👥 По количеству", callback_data="type_count")]
    ]
    await message.answer("3. Выберите условие завершения:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(CreateLot.finish_type)

@dp.callback_query(F.data.startswith("type_"))
async def admin_create_type(callback: CallbackQuery, state: FSMContext):
    ftype = callback.data.split("_")[1]
    await state.update_data(ftype=ftype)
    
    msg = "Введите количество участников (число):" if ftype == "count" else "Через сколько часов завершить (число, например 2 или 24):"
    await callback.message.answer(msg)
    await state.set_state(AdminStates.manual_count)

@dp.message(AdminStates.manual_count)
async def admin_create_final(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Введите целое число!")
    
    data = await state.get_data()
    val = message.text
    
    if data['ftype'] == "time":
        val = (datetime.now() + timedelta(hours=int(val))).strftime("%d.%m.%Y %H:%M")

    # Сохраняем в БД
    lot_id = db_query("INSERT INTO lotteries (text, entities, channels, finish_type, finish_value, photo, sticker) VALUES (?,?,?,?,?,?,?)",
                      (data['text'], data['entities'], data['channels'], data['ftype'], val, data['photo'], data['sticker']), commit=True)
    
    # Отправка в канал
    kb = [[InlineKeyboardButton(text="✅ Участвовать! (0)", url=f"https://t.me/{(await bot.get_me()).username}?start=lot_{lot_id}")]]
    ents = [types.MessageEntity(**e) for e in json.loads(data['entities'])] if data['entities'] != "[]" else None

    try:
        if data['photo']:
            sent = await bot.send_photo(LOT_CHANNEL, data['photo'], caption=data['text'], caption_entities=ents, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        elif data['sticker']:
            # Стикеры отправляются отдельно, а сообщение с кнопкой следом
            await bot.send_sticker(LOT_CHANNEL, data['sticker'])
            sent = await bot.send_message(LOT_CHANNEL, "🎁 Участвуйте в новом розыгрыше!", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        else:
            sent = await bot.send_message(LOT_CHANNEL, data['text'], entities=ents, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        
        db_query("UPDATE lotteries SET message_id = ? WHERE id = ?", (sent.message_id, lot_id), commit=True)
        await message.answer(f"✅ Лотерея #{lot_id} успешно запущена в канале!")
    except Exception as e:
        await message.answer(f"❌ Ошибка при отправке: {e}")
    
    await state.clear()

# --- 8. УПРАВЛЕНИЕ АДМИНКОЙ ---
@dp.callback_query(F.data == "admin_panel", F.from_user.id.in_(ADMIN_IDS))
async def admin_panel(callback: CallbackQuery):
    kb = [
        [InlineKeyboardButton(text="📢 Создать розыгрыш", callback_data="admin_create")],
        [InlineKeyboardButton(text="📋 Активные лотереи", callback_data="admin_list_active")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📨 Рассылка", callback_data="admin_broadcast")]
    ]
    await callback.message.edit_text("🛠 Админ-панель", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data == "admin_list_active", F.from_user.id.in_(ADMIN_IDS))
async def admin_list_active(callback: CallbackQuery):
    lots = db_query("SELECT * FROM lotteries WHERE status='active'", fetchall=True)
    if not lots: return await callback.answer("Нет активных розыгрышей", show_alert=True)
    
    kb = InlineKeyboardBuilder()
    for l in lots:
        kb.button(text=f"🛑 Стоп #{l['id']}", callback_data=f"admin_stop_{l['id']}")
    kb.button(text="🔙 Назад", callback_data="admin_panel")
    await callback.message.edit_text("Выберите лотерею для принудительного завершения:", reply_markup=kb.adjust(1).as_markup())

@dp.callback_query(F.data.startswith("admin_stop_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_stop_lot(callback: CallbackQuery):
    lid = int(callback.data.split("_")[2])
    await finish_giveaway(lid, manual=True)
    await callback.answer("Розыгрыш завершен!")
    await admin_panel(callback)

@dp.callback_query(F.data == "admin_stats", F.from_user.id.in_(ADMIN_IDS))
async def admin_stats(callback: CallbackQuery):
    u_count = db_query("SELECT COUNT(*) as c FROM users", fetchone=True)['c']
    l_count = db_query("SELECT COUNT(*) as c FROM lotteries", fetchone=True)['c']
    kb = [[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]]
    await callback.message.edit_text(f"📊 Статистика:\nВсего пользователей: {u_count}\nВсего лотерей: {l_count}", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data == "admin_broadcast", F.from_user.id.in_(ADMIN_IDS))
async def admin_broadcast(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.broadcast_text)
    await callback.message.answer("Введите текст для рассылки всем пользователям:")
    await callback.answer()

@dp.message(AdminStates.broadcast_text)
async def admin_broadcast_send(message: Message, state: FSMContext):
    users = db_query("SELECT user_id FROM users", fetchall=True)
    count = 0
    for u in users:
        try:
            await bot.send_message(u['user_id'], message.text)
            count += 1
            await asyncio.sleep(0.05)
        except: pass
    await message.answer(f"✅ Рассылка завершена! Получили: {count} чел.")
    await state.clear()

# --- 9. ФУНКЦИИ ПОЛЬЗОВАТЕЛЯ ---
@dp.callback_query(F.data == "active_lotteries")
async def user_active_lots(callback: CallbackQuery):
    lots = db_query("SELECT * FROM lotteries WHERE status='active'", fetchall=True)
    if not lots: return await callback.answer("Сейчас нет активных розыгрышей", show_alert=True)
    
    text = "🎁 Активные розыгрыши:\n\n"
    for l in lots:
        text += f"🔹 Розыгрыш #{l['id']} — [Участвовать](https://t.me/{LOT_CHANNEL.lstrip('@')}/{l['message_id']})\n"
    
    await callback.message.answer(text, disable_web_page_preview=True)
    await callback.answer()

@dp.callback_query(F.data == "my_participations")
async def user_my_lots(callback: CallbackQuery):
    uid = callback.from_user.id
    parts = db_query("""SELECT l.id, l.status, l.winner_id FROM participants p 
                        JOIN lotteries l ON p.lot_id = l.id WHERE p.user_id=?""", (uid,), fetchall=True)
    if not parts: return await callback.answer("Вы еще не участвовали в розыгрышах", show_alert=True)
    
    text = "📊 Ваши участия:\n\n"
    for p in parts:
        status = "🏆 Победил!" if p['winner_id'] == uid else ("⏳ В процессе" if p['status'] == 'active' else "❌ Не повезло")
        text += f"ID #{p['id']} — {status}\n"
    
    await callback.message.answer(text)
    await callback.answer()

# --- 10. PR ЗАЯВКИ ---
@dp.callback_query(F.data == "apply_pr")
async def pr_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PRApplication.age)
    await callback.message.answer("1. Укажите ваш возраст:")
    await callback.answer()

@dp.message(PRApplication.age)
async def pr_age(message: Message, state: FSMContext):
    await state.update_data(age=message.text)
    await state.set_state(PRApplication.nickname)
    await message.answer("2. Ваш никнейм или ссылка на канал:")

@dp.message(PRApplication.nickname)
async def pr_nick(message: Message, state: FSMContext):
    await state.update_data(nickname=message.text)
    await state.set_state(PRApplication.proofs)
    await message.answer("3. Пришлите скриншот вашей статистики:")

@dp.message(PRApplication.proofs, F.photo)
async def pr_final(message: Message, state: FSMContext):
    data = await state.get_data()
    caption = (f"📩 **Новая PR Заявка!**\n\n"
               f"👤 Юзер: {message.from_user.full_name} (@{message.from_user.username})\n"
               f"🔞 Возраст: {data['age']}\n"
               f"🔗 Ссылка: {data['nickname']}")
    
    if PR_CHAT_ID:
        await bot.send_photo(PR_CHAT_ID, message.photo[-1].file_id, caption=caption)
    
    await message.answer("✅ Ваша заявка отправлена админам!")
    await state.clear()

# --- 11. ФОНОВЫЙ ЧЕКЕР ВРЕМЕНИ ---
async def time_checker():
    while True:
        try:
            active_time_lots = db_query("SELECT * FROM lotteries WHERE status='active' AND finish_type='time'", fetchall=True)
            now = datetime.now()
            for lot in active_time_lots:
                f_time = datetime.strptime(lot['finish_value'], "%d.%m.%Y %H:%M")
                if now >= f_time:
                    await finish_giveaway(lot['id'])
        except Exception as e:
            logging.error(f"Ошибка чекера: {e}")
        await asyncio.sleep(30)

# --- 12. ЗАПУСК ---
async def main():
    init_db()
    asyncio.create_task(time_checker())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен")
