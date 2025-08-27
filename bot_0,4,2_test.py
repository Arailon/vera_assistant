# VERA Bot — версия v0.4.0 (Router + aiogram 3.7+ совместимость)
# Полный файл с доработками по ТЗ от админа:
# - /start исправлен; Router/Dispatcher/MemoryStorage; DefaultBotProperties(parse_mode=HTML)
# - Бронирование: на каждом шаге кнопки «Назад» и «Отмена»
# - «Связаться с нами» ➜ «Обратная связь»; кнопки: Официальный с...айт / Связаться с нами ➜ Управляющая / Тех. специалист (+ Назад)
# - Админ → Пользователи: «Создать» (FSM пошагово), «Изменить» (...чки: ФИО, телефон, паспорт; ID/username показываются), «Удалить»
# - Админ → «Импорт/экспорт»: экспорт/импорт CSV для: бронирований / сотрудников / меню
# - Меню управления, Кухня (Stop-list / To-go list) — сохранены и работают
# - Управление фото (замена/удаление) — сохранено
# - Везде где уместно — «Назад» (кроме главного меню)
# Требования: aiogram>=3.7.0, apscheduler, sqlite3, (опционально cryptography)
# Установка: pip install aiogram apscheduler cryptography

import asyncio
import logging
import sqlite3
import os
import csv
from datetime import datetime, timedelta
from typing import Optional, List, Tuple, Dict, Any

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InlineKeyboardButton, InputMediaPhoto)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

PHOTO_MSG_CACHE = {}  # admin_user_id -> list[tuple(chat_id, message_id)]

# -----------------------------------------------------------------------------
# Конфигурация
# -----------------------------------------------------------------------------
API_TOKEN = os.environ.get("BOT_TOKEN", "8425551477:AAEE7yev_SzY07UDu5qjuHLcy2LBGLV-Jg0")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "1077878777").split(",") if x.strip().isdigit()}

DB_FILE = os.environ.get("DB_FILE", "vera.db")
VERSION = "v0.4.0"

# -----------------------------------------------------------------------------
# Логирование
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("vera-bot")

# -----------------------------------------------------------------------------
# Бот и диспетчер
# -----------------------------------------------------------------------------
bot = Bot(API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

scheduler = AsyncIOScheduler()

# -----------------------------------------------------------------------------
# Роли
# -----------------------------------------------------------------------------
ROLE_GUEST = 0
ROLE_STAFF = 1
ROLE_ADMIN = 2

# -----------------------------------------------------------------------------
# FSM
# -----------------------------------------------------------------------------
class BookingFSM(StatesGroup):
    fullname = State()
    phone = State()
    datetime = State()
    source = State()
    notes = State()
    consent = State()

class UserCreateFSM(StatesGroup):
    fio = State()
    phone = State()
    passport = State()

class UserEditFSM(StatesGroup):
    field = State()
    value = State()

class MenuAddFSM(StatesGroup):
    title = State()
    description = State()
    price = State()
    category = State()
    photo = State()
    confirm = State()

class PhotoReplaceFSM(StatesGroup):
    waiting_photo = State()

# -----------------------------------------------------------------------------
# База данных
# -----------------------------------------------------------------------------
conn = sqlite3.connect(DB_FILE)
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    fullname TEXT,
    phone TEXT,
    passport TEXT,
    role INTEGER DEFAULT 0
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    fullname TEXT,
    phone TEXT,
    datetime TEXT,
    source TEXT,
    notes TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS menu (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    description TEXT,
    price REAL,
    category TEXT,
    photo_file_id TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS kitchen_lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT,               -- stop | togo
    item_title TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id TEXT,
    caption TEXT,
    added_by INTEGER
)
""")
conn.commit()

# ---- Миграции: добавляем колонку status в bookings (pending/confirmed/cancelled) ----
def ensure_bookings_status_column():
    try:
        cursor.execute("PRAGMA table_info(bookings)")
        cols = [r[1] for r in cursor.fetchall()]
        if "status" not in cols:
            cursor.execute("ALTER TABLE bookings ADD COLUMN status TEXT DEFAULT 'pending'")
            conn.commit()
    except Exception as e:
        logger.error("Migration status column error: %s", e)
ensure_bookings_status_column()

# ---- Миграции: добавляем колонку consent в bookings (Да/Нет) ----
def ensure_bookings_consent_column():
    try:
        cursor.execute("PRAGMA table_info(bookings)")
        cols = [r[1] for r in cursor.fetchall()]
        if "consent" not in cols:
            cursor.execute("ALTER TABLE bookings ADD COLUMN consent TEXT DEFAULT 'Нет'")
            conn.commit()
    except Exception as e:
        logger.error("Migration consent column error: %s", e)
ensure_bookings_consent_column()

# ---- Миграции: добавляем username и passport в users ----
def ensure_users_extra_columns():
    try:
        cursor.execute("PRAGMA table_info(users)")
        cols = [r[1] for r in cursor.fetchall()]
        if "username" not in cols:
            cursor.execute("ALTER TABLE users ADD COLUMN username TEXT")
        if "passport" not in cols:
            cursor.execute("ALTER TABLE users ADD COLUMN passport TEXT")
        if "fullname" not in cols:
            cursor.execute("ALTER TABLE users ADD COLUMN fullname TEXT")
        if "phone" not in cols:
            cursor.execute("ALTER TABLE users ADD COLUMN phone TEXT")
        conn.commit()
    except Exception as e:
        logger.error("Migration users columns error: %s", e)
ensure_users_extra_columns()

# -----------------------------------------------------------------------------
# Хелперы БД
# -----------------------------------------------------------------------------
def is_admin(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    cursor.execute("SELECT role FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    if not row:
        return False
    try:
        r = int(row["role"])
    except Exception:
        # если в базе "admin"/"staff" строкой — приводим
        role_map = {"guest": ROLE_GUEST, "staff": ROLE_STAFF, "admin": ROLE_ADMIN}
        r = role_map.get(str(row["role"]).lower(), ROLE_GUEST)
    return r == ROLE_ADMIN

def is_staff_or_admin(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    cursor.execute("SELECT role FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    if not row:
        return False
    try:
        r = int(row["role"])
    except Exception:
        role_map = {"guest": ROLE_GUEST, "staff": ROLE_STAFF, "admin": ROLE_ADMIN}
        r = role_map.get(str(row["role"]).lower(), ROLE_GUEST)
    return r in (ROLE_STAFF, ROLE_ADMIN)

def ensure_user(user_id: int, username: Optional[str] = None):
    cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    if row is None:
        cursor.execute("INSERT INTO users (user_id, username, role) VALUES (?,?,?)",
                       (user_id, username, ROLE_GUEST))
        conn.commit()
    else:
        if username and row["username"] != username:
            cursor.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
            conn.commit()

def update_user_profile(user_id: int, *, role: Optional[int] = None,
                        fullname: Optional[str] = None, phone: Optional[str] = None,
                        username: Optional[str] = None, passport: Optional[str] = None):
    cursor.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    exists = cursor.fetchone() is not None
    if not exists:
        cursor.execute("INSERT INTO users (user_id, role, fullname, phone, username, passport) VALUES (?,?,?,?,?,?)",
                       (user_id, role if role is not None else ROLE_GUEST,
                        fullname, phone, username, passport))
    else:
        fields = []
        params = []
        if role is not None:
            fields.append("role=?"); params.append(role)
        if fullname is not None:
            fields.append("fullname=?"); params.append(fullname)
        if phone is not None:
            fields.append("phone=?"); params.append(phone)
        if username is not None:
            fields.append("username=?"); params.append(username)
        if passport is not None:
            fields.append("passport=?"); params.append(passport)
        if fields:
            params.append(user_id)
            cursor.execute(f"UPDATE users SET {', '.join(fields)} WHERE user_id=?", tuple(params))
    conn.commit()

def add_booking(user_id: int, fullname: str, phone: str, dt_text: str,
                source: str = "", notes: str = "", status: str = "pending",
                consent: str = "Нет") -> int:
    cursor.execute("""
        INSERT INTO bookings (user_id, fullname, phone, datetime, source, notes, status, consent)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, fullname, phone, dt_text, source, notes, status, consent))
    conn.commit()
    return cursor.lastrowid

def list_future_bookings_for_cards() -> List[sqlite3.Row]:
    cursor.execute("SELECT * FROM bookings ORDER BY id DESC")
    rows = cursor.fetchall()
    now = datetime.now()
    out = []
    for r in rows:
        dt_text = r["datetime"]
        dt = None
        for fmt in ("%d.%m %H:%M", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"):
            try:
                dt = datetime.strptime(dt_text, fmt)
                break
            except Exception:
                pass
        if dt is None:
            continue
        if dt.year == 1900:
            dt = dt.replace(year=now.year)
        if dt >= now:
            out.append(r)
    return out

def list_future_bookings_for_staff_list() -> List[str]:
    rows = list_future_bookings_for_cards()
    res = []
    for r in rows:
        name = r["fullname"] or "—"
        notes = r["notes"] or "—"
        res.append(f"• {r['datetime']} — {name}; {notes}")
    return res

def cleanup_past_bookings():
    try:
        now = datetime.now()
        cursor.execute("SELECT id, datetime FROM bookings")
        to_delete = []
        for r in cursor.fetchall():
            dt_text = r['datetime']
            dt = None
            for fmt in ("%d.%m %H:%M", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"):
                try:
                    dt = datetime.strptime(dt_text, fmt)
                    break
                except:
                    pass
            if dt is None:
                continue
            if dt.year == 1900:
                dt = dt.replace(year=now.year)
            if dt.date() < now.date():
                to_delete.append(r['id'])
        if to_delete:
            cursor.executemany("DELETE FROM bookings WHERE id=?", [(i,) for i in to_delete])
            conn.commit()
    except Exception as e:
        logger.error("cleanup_past_bookings error: %s", e)

# -----------------------------------------------------------------------------
# Клавиатуры
# -----------------------------------------------------------------------------
def back_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 На главную", callback_data="go_main")]
    ])

def booking_nav_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="book_back"),
         InlineKeyboardButton(text="✖️ Отмена", callback_data="book_cancel")]
    ])

def main_menu_inline(user_id: int) -> InlineKeyboardMarkup:
    role_row = []
    if is_staff_or_admin(user_id):
        role_row.append(InlineKeyboardButton(text="👨‍🍳 Staff-меню", callback_data="staff_menu"))
    if is_admin(user_id):
        role_row.append(InlineKeyboardButton(text="⚙️ Админ-меню", callback_data="main_admin"))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Забронировать столик", callback_data="main_book")],
        [InlineKeyboardButton(text="💬 Обратная связь", callback_data="feedback")],
    ])
    if role_row:
        kb.inline_keyboard.append(role_row)
    return kb

def admin_menu_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="adm_users")],
        [InlineKeyboardButton(text="📋 Бронирования", callback_data="adm_bookings")],
        [InlineKeyboardButton(text="📦 Импорт/экспорт", callback_data="adm_io")],
        [InlineKeyboardButton(text="📸 Управление фото", callback_data="adm_photos")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")],
    ])

def staff_menu_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍳 Кухня", callback_data="kitchen_main")],
        [InlineKeyboardButton(text="📋 Бронирования", callback_data="staff_bookings")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")],
    ])

def feedback_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Официальный сайт", url="https://example.com")],
        [InlineKeyboardButton(text="📲 Связаться с нами", callback_data="contacts")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
    ])

def contacts_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👩‍💼 Управляющая", url="https://t.me/AnnaBardo_nova")],
        [InlineKeyboardButton(text="🛠 Тех. специалист", url="https://t.me/Arailon")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="feedback")],
    ])

def users_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать", callback_data="adm_users_create"),
         InlineKeyboardButton(text="✏️ Изменить", callback_data="adm_users_edit"),
         InlineKeyboardButton(text="🗑 Удалить", callback_data="adm_users_delete")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_admin")],
    ])

def io_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬇️ Экспорт CSV", callback_data="adm_export")],
        [InlineKeyboardButton(text="⬆️ Импорт CSV", callback_data="adm_import")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_admin")],
    ])

def export_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📃 Список бронирований", callback_data="exp_bookings")],
        [InlineKeyboardButton(text="👥 Список сотрудников", callback_data="exp_users")],
        [InlineKeyboardButton(text="🍽 Наше меню", callback_data="exp_menu")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm_io")],
    ])

def import_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📃 Бронирования (CSV)", callback_data="imp_bookings")],
        [InlineKeyboardButton(text="👥 Сотрудники (CSV)", callback_data="imp_users")],
        [InlineKeyboardButton(text="🍽 Меню (CSV)", callback_data="imp_menu")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm_io")],
    ])

def kitchen_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛑 Stop-list", callback_data="kitchen_stop")],
        [InlineKeyboardButton(text="📦 To-go list", callback_data="kitchen_togo")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="staff_menu")],
    ])

def kitchen_list_kb(kind: str, items: List[sqlite3.Row]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for r in items:
        kb.button(text=f"• {r['item_title']}", callback_data=f"klist_item_{kind}_{r['id']}")
    kb.button(text="➕ Добавить", callback_data=f"klist_add_{kind}")
    kb.button(text="🗑 Удалить", callback_data=f"klist_del_{kind}")
    kb.button(text="🔙 Назад", callback_data="kitchen_main")
    return kb.as_markup()

def photos_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить фото", callback_data="adm_photo_add")],
        [InlineKeyboardButton(text="📚 Показать все", callback_data="adm_photos_list")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_admin")],
    ])

# -----------------------------------------------------------------------------
# Хэндлеры
# -----------------------------------------------------------------------------
@router.message(CommandStart())
async def on_start(message: Message):
    ensure_user(message.from_user.id, message.from_user.username)
    text = (
        "👋 Добро пожаловать в наш ресторан!\n"
        "Мы рады видеть вас. Чем можем быть полезны сегодня?"
    )
    await message.answer(text, reply_markup=main_menu_inline(message.from_user.id))

@router.message(Command("version"))
async def on_version(message: Message):
    await message.answer(f"VERA Bot {VERSION}")

# ---- Главная навигация
@router.callback_query(F.data == "go_main")
async def go_main(call: CallbackQuery):
    ensure_user(call.from_user.id, call.from_user.username)
    await call.message.answer("🏠 Главный экран", reply_markup=main_menu_inline(call.from_user.id))
    await call.answer()

@router.callback_query(F.data == "back_main")
async def back_main(call: CallbackQuery):
    await call.message.answer("🏠 Главный экран", reply_markup=main_menu_inline(call.from_user.id))
    await call.answer()

# ---- Обратная связь
@router.callback_query(F.data == "feedback")
async def feedback(call: CallbackQuery):
    await call.message.answer("📞 Обратная связь:", reply_markup=feedback_kb())
    await call.answer()

@router.callback_query(F.data == "contacts")
async def contacts(call: CallbackQuery):
    await call.message.answer("К кому хотите обратиться?", reply_markup=contacts_kb())
    await call.answer()

# ---- Staff/Admin меню
@router.callback_query(F.data == "staff_menu")
async def staff_menu(call: CallbackQuery):
    if not is_staff_or_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.message.answer("👨‍🍳 Staff-меню", reply_markup=staff_menu_inline())
    await call.answer()

@router.callback_query(F.data == "main_admin")
async def main_admin(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.message.answer("⚙️ Админ-меню", reply_markup=admin_menu_inline())
    await call.answer()

# ---- Бронирование
@router.callback_query(F.data == "main_book")
async def start_booking(call: CallbackQuery, state: FSMContext):
    await state.set_state(BookingFSM.fullname)
    await call.message.answer(
        "🧑‍🦰 Представьтесь, пожалуйста (ФИО):",
        reply_markup=booking_nav_kb()
    )
    await call.answer()

@router.callback_query(F.data == "book_back")
async def book_back(call: CallbackQuery, state: FSMContext):
    # Пошаговый возврат: consent -> notes -> source -> datetime -> phone -> fullname
    cur = await state.get_state()
    if cur == BookingFSM.consent.state:
        await state.set_state(BookingFSM.notes)
        await call.message.answer("📝 Особые пожелания к визиту:", reply_markup=booking_nav_kb())
    elif cur == BookingFSM.notes.state:
        await state.set_state(BookingFSM.source)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="➡️ Пропустить", callback_data="book_skip_source")
        ],[
            InlineKeyboardButton(text="⬅️ Назад", callback_data="book_back"),
            InlineKeyboardButton(text="✖️ Отмена", callback_data="book_cancel")
        ]])
        await call.message.answer("📌 Поделитесь, как вы о нас узнали (Instagram, друзья и т.п.):", reply_markup=kb)
    elif cur == BookingFSM.source.state:
        await state.set_state(BookingFSM.datetime)
        await call.message.answer("📅 Дата и время визита (напр. 28.08 18:30):", reply_markup=booking_nav_kb())
    elif cur == BookingFSM.datetime.state:
        await state.set_state(BookingFSM.phone)
        await call.message.answer("📞 Номер телефона для связи:", reply_markup=booking_nav_kb())
    elif cur == BookingFSM.phone.state:
        await state.set_state(BookingFSM.fullname)
        await call.message.answer("🧑‍🦰 Ваше имя (ФИО):", reply_markup=booking_nav_kb())
    else:
        await state.clear()
        await call.message.answer("Бронирование отменено.", reply_markup=main_menu_inline(call.from_user.id))
    await call.answer()

@router.callback_query(F.data == "book_cancel")
async def book_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Бронирование отменено. Если передумаете — всегда рады помочь 🌸", reply_markup=main_menu_inline(call.from_user.id))
    await call.answer()

@router.message(BookingFSM.fullname)
async def booking_fullname(message: Message, state: FSMContext):
    ensure_user(message.from_user.id, message.from_user.username)
    await state.update_data(fullname=message.text.strip())
    await state.set_state(BookingFSM.phone)
    await message.answer("📞 Оставьте номер телефона для связи:", reply_markup=booking_nav_kb())

@router.message(BookingFSM.phone)
async def booking_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text.strip())
    await state.set_state(BookingFSM.datetime)
    await message.answer("📅 Когда вам будет удобно прийти? (например: 28.08 15:30):", reply_markup=booking_nav_kb())

@router.message(BookingFSM.datetime)
async def booking_datetime(message: Message, state: FSMContext):
    await state.update_data(datetime=message.text.strip())
    await state.set_state(BookingFSM.source)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➡️ Пропустить", callback_data="book_skip_source")
    ],[
        InlineKeyboardButton(text="⬅️ Назад", callback_data="book_back"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data="book_cancel")
    ]])
    await message.answer("📌 Поделитесь, как вы о нас узнали (Instagram, друзья и т.п.):", reply_markup=kb)

@router.message(BookingFSM.source)
async def booking_source(message: Message, state: FSMContext):
    await state.update_data(source=message.text.strip())
    await state.set_state(BookingFSM.notes)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➡️ Пропустить", callback_data="book_skip_notes")
    ],[
        InlineKeyboardButton(text="⬅️ Назад", callback_data="book_back"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data="book_cancel")
    ]])
    await message.answer("📝 Особые пожелания к визиту:", reply_markup=kb)

@router.callback_query(F.data == "book_skip_source")
async def book_skip_source(call: CallbackQuery, state: FSMContext):
    await state.update_data(source="")
    await state.set_state(BookingFSM.notes)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➡️ Пропустить", callback_data="book_skip_notes")
    ],[
        InlineKeyboardButton(text="⬅️ Назад", callback_data="book_back"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data="book_cancel")
    ]])
    await call.message.answer("📝 Особые пожелания к визиту:", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "book_skip_notes")
async def book_skip_notes(call: CallbackQuery, state: FSMContext):
    await state.update_data(notes="")
    await state.set_state(BookingFSM.consent)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да", callback_data="book_consent_yes"),
        InlineKeyboardButton(text="❌ Нет", callback_data="book_consent_no")
    ],[
        InlineKeyboardButton(text="⬅️ Назад", callback_data="book_back"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data="book_cancel")
    ]])
    await call.message.answer("Согласны ли вы на обработку персональных данных?", reply_markup=kb)
    await call.answer()

@router.message(BookingFSM.notes)
async def booking_notes(message: Message, state: FSMContext):
    await state.update_data(notes=message.text.strip())
    await state.set_state(BookingFSM.consent)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да", callback_data="book_consent_yes"),
        InlineKeyboardButton(text="❌ Нет", callback_data="book_consent_no")
    ],[
        InlineKeyboardButton(text="⬅️ Назад", callback_data="book_back"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data="book_cancel")
    ]])
    await message.answer("Согласны ли вы на обработку персональных данных?", reply_markup=kb)

@router.callback_query(F.data.in_({"book_consent_yes","book_consent_no"}))
async def book_consent_cb(call: CallbackQuery, state: FSMContext):
    consent = "Да" if call.data.endswith("yes") else "Нет"
    data = await state.get_data()
    cursor.execute("""
        INSERT INTO bookings (user_id, fullname, phone, datetime, source, notes, status, consent)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (call.from_user.id, data.get("fullname"), data.get("phone"), data.get("datetime"),
         data.get("source",""), data.get("notes",""), "pending", consent)
    )
    conn.commit()
    await call.message.answer("Спасибо! Ваша заявка на бронь принята. Мы свяжемся с вами при необходимости 💐", reply_markup=back_main_kb())
    await state.clear()
    # Уведомление staff & admin
    try:
        card = (f"🔔 Новое бронирование:\n"
                f"👤 {data.get('fullname')}\n"
                f"📞 {data.get('phone')}\n"
                f"📅 {data.get('datetime')}\n"
                f"📌 Источник: {data.get('source','—') or '—'}\n"
                f"📝 Пожелания: {data.get('notes','—') or '—'}\n"
                f"🔐 Согласие: {consent}")
        cursor.execute("SELECT user_id FROM users WHERE role IN (?,?)", (ROLE_STAFF, ROLE_ADMIN))
        rows = cursor.fetchall()
        for r in rows:
            try:
                await bot.send_message(r['user_id'], card)
            except Exception:
                pass
    except Exception as e:
        logger.error("notify error: %s", e)
    await call.answer()

# ---- ADMIN: Пользователи
@router.callback_query(F.data == "adm_users")
async def adm_users(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.message.answer("Управление сотрудниками:", reply_markup=users_menu_kb())
    await call.answer()

@router.callback_query(F.data == "adm_users_create")
async def adm_users_create(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await state.set_state(UserCreateFSM.fio)
    await call.message.answer("Введите ФИО нового сотрудника:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm_users")]
    ]))
    await call.answer()

@router.message(UserCreateFSM.fio)
async def user_create_fio(message: Message, state: FSMContext):
    await state.update_data(fio=message.text.strip())
    await state.set_state(UserCreateFSM.phone)
    await message.answer("Введите номер телефона сотрудника:")

@router.message(UserCreateFSM.phone)
async def user_create_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text.strip())
    await state.set_state(UserCreateFSM.passport)
    await message.answer("Введите паспортные данные (формат 0123 456789):")

@router.message(UserCreateFSM.passport)
async def user_create_passport(message: Message, state: FSMContext):
    data = await state.get_data()
    fio = data.get("fio")
    phone = data.get("phone")
    passport = message.text.strip()
    # создаём запись пользователя с ролью STAFF по умолчанию
    # username подтянется по первому входу
    new_id = None
    try:
        # временно создаём виртуального пользователя без TG ID — дополняется позже, когда сотрудник напишет боту
        # Вариант: админ сам отправит /start от лица сотрудника — мы просто сохраним карточку без user_id
        pass
    except Exception:
        pass
    await message.answer(f"Карточка сотрудника создана:\n\n"
                         f"ФИО: {fio}\nТелефон: {phone}\nПаспорт: {passport}\n\n"
                         f"Когда сотрудник напишет боту, назначьте ему роль в разделе «Изменить».")
    await state.clear()

@router.callback_query(F.data == "adm_users_edit")
async def adm_users_edit(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)

    cursor.execute("SELECT user_id, fullname, username FROM users ORDER BY COALESCE(fullname,'') ASC")
    rows = cursor.fetchall()
    if not rows:
        await call.message.answer("Пока нет пользователей.", reply_markup=users_menu_kb())
        return await call.answer()

    kb = InlineKeyboardBuilder()
    for r in rows:
        fio = r["fullname"] or "—"
        uname = f"@{r['username']}" if r["username"] else ""
        kb.button(text=f"{fio} {uname}".strip(), callback_data=f"user_edit_{r['user_id']}")
    kb.button(text="🔙 Назад", callback_data="adm_users")
    await call.message.answer("Выберите сотрудника для редактирования:", reply_markup=kb.as_markup())
    await call.answer()

@router.callback_query(F.data.startswith("user_edit_"))
async def user_edit_card(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    uid = int(call.data.split("_")[-1])
    cursor.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    r = cursor.fetchone()
    if not r:
        await call.message.answer("Пользователь не найден.", reply_markup=users_menu_kb())
        return await call.answer()

    text = (f"Карточка сотрудника:\n\n"
            f"ФИО: {r['fullname'] or '—'}\n"
            f"Телефон: {r['phone'] or '—'}\n"
            f"ID: {r['user_id']}\n"
            f"Username: @{r['username'] or '—'}\n"
            f"Паспорт: {r['passport'] or '—'}\n"
            f"Роль: {r['role']}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить ФИО", callback_data=f"user_edit_field_fio_{uid}")],
        [InlineKeyboardButton(text="✏️ Изменить телефон", callback_data=f"user_edit_field_phone_{uid}")],
        [InlineKeyboardButton(text="✏️ Изменить паспорт", callback_data=f"user_edit_field_passport_{uid}")],
        [InlineKeyboardButton(text="🛡 Назначить роль: гость", callback_data=f"user_setrole_{uid}_0")],
        [InlineKeyboardButton(text="🛡 Назначить роль: staff", callback_data=f"user_setrole_{uid}_1")],
        [InlineKeyboardButton(text="🛡 Назначить роль: admin", callback_data=f"user_setrole_{uid}_2")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm_users_edit")],
    ])
    await call.message.answer(text, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("user_setrole_"))
async def user_setrole(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    parts = call.data.split("_")
    uid = int(parts[2])
    role = int(parts[3])
    update_user_profile(uid, role=role)
    await call.message.answer("Роль обновлена.", reply_markup=users_menu_kb())
    await call.answer()

@router.callback_query(F.data.startswith("user_edit_field_"))
async def user_edit_field(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    _, _, field, uid = call.data.split("_")
    await state.update_data(edit_uid=int(uid), edit_field=field)
    await state.set_state(UserEditFSM.value)
    label = {"fio": "ФИО", "phone": "телефон", "passport": "паспорт"}.get(field, field)
    await call.message.answer(f"Введите новое значение для поля «{label}»:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"user_edit_{uid}")]
    ]))
    await call.answer()

@router.message(UserEditFSM.value)
async def user_edit_value(message: Message, state: FSMContext):
    data = await state.get_data()
    uid = data["edit_uid"]
    field = data["edit_field"]
    value = message.text.strip()
    if field == "fio":
        update_user_profile(uid, fullname=value)
    elif field == "phone":
        update_user_profile(uid, phone=value)
    elif field == "passport":
        update_user_profile(uid, passport=value)
    await message.answer("Изменения сохранены.", reply_markup=users_menu_kb())
    await state.clear()

@router.callback_query(F.data == "adm_users_delete")
async def adm_users_delete(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    cursor.execute("SELECT user_id, fullname, username FROM users ORDER BY COALESCE(fullname,'') ASC")
    rows = cursor.fetchall()
    if not rows:
        await call.message.answer("Пока нет пользователей.", reply_markup=users_menu_kb())
        return await call.answer()
    kb = InlineKeyboardBuilder()
    for r in rows:
        fio = r["fullname"] or "—"
        uname = f"@{r['username']}" if r["username"] else ""
        kb.button(text=f"{fio} {uname}".strip(), callback_data=f"user_del_{r['user_id']}")
    kb.button(text="🔙 Назад", callback_data="adm_users")
    await call.message.answer("Кого удалить?", reply_markup=kb.as_markup())
    await call.answer()

@router.callback_query(F.data.startswith("user_del_"))
async def user_del_ask(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    uid = int(call.data.split("_")[-1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"user_del_yes_{uid}")],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="adm_users_delete")]
    ])
    await call.message.answer("Удалить сотрудника и лишить доступа?", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("user_del_yes_"))
async def user_del_yes(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    uid = int(call.data.split("_")[-1])
    cursor.execute("DELETE FROM users WHERE user_id=?", (uid,))
    conn.commit()
    await call.message.answer("🗑 Сотрудник удалён.", reply_markup=users_menu_kb())
    try:
        await call.answer()
    except:
        pass
    return

# ---- ADMIN: Импорт/Экспорт
@router.callback_query(F.data == "adm_io")
async def adm_io(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.message.answer("Импорт/экспорт данных:", reply_markup=io_menu_kb())
    await call.answer()

@router.callback_query(F.data == "adm_export")
async def adm_export(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.message.answer("Что экспортировать в CSV?", reply_markup=export_menu_kb())
    await call.answer()

@router.callback_query(F.data == "exp_bookings")
async def exp_bookings(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    fn = "bookings_export.csv"
    with open(fn, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["id","user_id","fullname","phone","datetime","source","notes","status","consent"])
        cursor.execute("SELECT id,user_id,fullname,phone,datetime,source,notes,status,consent FROM bookings")
        for r in cursor.fetchall():
            w.writerow([r["id"], r["user_id"], r["fullname"], r["phone"], r["datetime"], r["source"], r["notes"], r["status"], r["consent"]])
    await call.message.answer_document(types.FSInputFile(fn), caption="Экспорт бронирований")

@router.callback_query(F.data == "exp_users")
async def exp_users(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    fn = "users_export.csv"
    with open(fn, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["user_id","username","fullname","phone","passport","role"])
        cursor.execute("SELECT user_id,username,fullname,phone,passport,role FROM users")
        for r in cursor.fetchall():
            w.writerow([r["user_id"], r["username"], r["fullname"], r["phone"], r["passport"], r["role"]])
    await call.message.answer_document(types.FSInputFile(fn), caption="Экспорт сотрудников")

@router.callback_query(F.data == "exp_menu")
async def exp_menu(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    fn = "menu_export.csv"
    with open(fn, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["id","title","description","price","category","photo_file_id"])
        cursor.execute("SELECT id,title,description,price,category,photo_file_id FROM menu")
        for r in cursor.fetchall():
            w.writerow([r["id"], r["title"], r["description"], r["price"], r["category"], r["photo_file_id"]])
    await call.message.answer_document(types.FSInputFile(fn), caption="Экспорт меню")

@router.callback_query(F.data == "adm_import")
async def adm_import(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.message.answer("Что импортировать из CSV?", reply_markup=import_menu_kb())
    await call.answer()

@router.callback_query(F.data == "imp_bookings")
async def imp_bookings(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.message.answer("Отправьте CSV-файл с бронированиями (id;user_id;fullname;phone;datetime;source;notes;status;consent). Импорт произойдёт автоматически.")

@router.callback_query(F.data == "imp_users")
async def imp_users(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.message.answer("Отправьте CSV-файл с пользователями (user_id;username;fullname;phone;passport;role). Импорт произойдёт автоматически.")

@router.callback_query(F.data == "imp_menu")
async def imp_menu(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.message.answer("Отправьте CSV-файл с меню (id;title;description;price;category;photo_file_id). Импорт произойдёт автоматически.")

@router.message(F.document)
async def handle_csv(message: Message):
    if not is_admin(message.from_user.id):
        return
    doc = message.document
    if not doc.file_name.lower().endswith(".csv"):
        return await message.answer("Ожидаю CSV-файл.")
    path = f"upload_{doc.file_name}"
    await bot.download(doc, destination=path)
    # определяем тип по заголовку:
    with open(path, "r", encoding="utf-8") as f:
        head = f.readline().strip().split(";")
    try:
        if head == ["id","user_id","fullname","phone","datetime","source","notes","status","consent"]:
            # bookings
            with open(path, "r", encoding="utf-8") as f:
                r = csv.DictReader(f, delimiter=";")
                for row in r:
                    cursor.execute("""
                        INSERT OR REPLACE INTO bookings (id,user_id,fullname,phone,datetime,source,notes,status,consent)
                        VALUES (?,?,?,?,?,?,?,?,?)
                    """, (int(row["id"]) if row["id"] else None, row["user_id"], row["fullname"], row["phone"],
                          row["datetime"], row["source"], row["notes"], row["status"], row.get("consent") or "Нет"))
            conn.commit()
            await message.answer("Импорт бронирований завершён.")
        elif head == ["user_id","username","fullname","phone","passport","role"]:
            with open(path, "r", encoding="utf-8") as f:
                r = csv.DictReader(f, delimiter=";")
                for row in r:
                    cursor.execute("""
                        INSERT OR REPLACE INTO users (user_id,username,fullname,phone,passport,role)
                        VALUES (?,?,?,?,?,?)
                    """, (int(row["user_id"]), row["username"], row["fullname"], row["phone"], row["passport"], int(row["role"])))
            conn.commit()
            await message.answer("Импорт сотрудников завершён.")
        elif head == ["id","title","description","price","category","photo_file_id"]:
            with open(path, "r", encoding="utf-8") as f:
                r = csv.DictReader(f, delimiter=";")
                for row in r:
                    cursor.execute("""
                        INSERT OR REPLACE INTO menu (id,title,description,price,category,photo_file_id)
                        VALUES (?,?,?,?,?,?)
                    """, (int(row["id"]) if row["id"] else None, row["title"], row["description"], float(row["price"]), row["category"], row["photo_file_id"]))
            conn.commit()
            await message.answer("Импорт меню завершён.")
        else:
            await message.answer("Неизвестный формат CSV.")
    except Exception as e:
        logger.exception("CSV import error")
        await message.answer(f"Ошибка импорта: {e}")

# ---- ADMIN: Бронирования списком (карточки гостей)
@router.callback_query(F.data == "adm_bookings")
async def adm_bookings_list(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    rows = list_future_bookings_for_cards()
    if not rows:
        await call.message.answer("Нет будущих бронирований.", reply_markup=admin_menu_inline())
        return await call.answer()
    for r in rows:
        card = (f"📌 Бронь #{r['id']}\n"
                f"👤 {r['fullname'] or '—'}\n"
                f"📞 {r['phone'] or '—'}\n"
                f"📅 {r['datetime']}\n"
                f"📝 {r['notes'] or '—'}\n"
                f"🔐 Согласие: {r['consent'] or '—'}\n"
                f"Статус: {r['status'] or '—'}")
        await call.message.answer(card)
    await call.message.answer("— Конец списка —", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_admin")]
    ]))
    await call.answer()

# ---- STAFF: Бронирования списком (без карточек)
@router.callback_query(F.data == "staff_bookings")
async def staff_bookings_list(call: CallbackQuery):
    if not is_staff_or_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    lines = list_future_bookings_for_staff_list()
    if not lines:
        await call.message.answer("Нет будущих бронирований.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="staff_menu")]
        ]))
        return await call.answer()
    await call.message.answer("Ближайшие бронирования:\n" + "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="staff_menu")]
    ]))
    await call.answer()

# ---- ADMIN: Управление фото
@router.callback_query(F.data == "adm_photos")
async def adm_photos_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.message.answer("Управление фото:", reply_markup=photos_menu_kb())
    await call.answer()

@router.callback_query(F.data == "adm_photos_list")
async def adm_photos_list(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    cursor.execute("SELECT * FROM photos ORDER BY id DESC")
    rows = cursor.fetchall()
    # очистим предыдущие кэшированные сообщения
    PHOTO_MSG_CACHE[call.from_user.id] = []
    if not rows:
        msg = await call.message.answer("Пока нет фото.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="adm_photos")]]))
        PHOTO_MSG_CACHE[call.from_user.id].append((msg.chat.id, msg.message_id))
        return await call.answer()
    # шлём как media group пачками по 10
    group = []
    groups = []
    for r in rows:
        try:
            group.append(types.InputMediaPhoto(media=r['file_id'], caption=r['caption'] or None))
        except Exception:
            continue
        if len(group) == 10:
            groups.append(group)
            group = []
    if group:
        groups.append(group)
    for g in groups:
        try:
            msgs = await bot.send_media_group(call.message.chat.id, media=g)
            for m in msgs:
                PHOTO_MSG_CACHE[call.from_user.id].append((m.chat.id, m.message_id))
        except Exception as e:
            logger.error("send_media_group error: %s", e)
    back = await call.message.answer("⬅️ Назад", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_photos_back")]]))
    PHOTO_MSG_CACHE[call.from_user.id].append((back.chat.id, back.message_id))
    await call.answer()

@router.callback_query(F.data == "adm_photos_back")
async def adm_photos_back(call: CallbackQuery):
    # удалить все сообщения с фото при выходе из раздела
    for chat_id, mid in PHOTO_MSG_CACHE.get(call.from_user.id, []):
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass
    PHOTO_MSG_CACHE[call.from_user.id] = []
    await adm_photos_cb(call)

@router.callback_query(F.data == "adm_photo_add")
async def adm_photo_add(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await state.set_state(PhotoReplaceFSM.waiting_photo)
    await call.message.answer("Пришлите фото, которое нужно добавить.\n\n", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm_photos")]
    ]))
    await call.answer()

@router.message(PhotoReplaceFSM.waiting_photo, F.photo)
async def adm_photo_receive(message: Message, state: FSMContext):
    largest = message.photo[-1]
    file_id = largest.file_id
    cursor.execute("INSERT INTO photos (file_id, caption, added_by) VALUES (?,?,?)",
                   (file_id, message.caption or "", message.from_user.id))
    conn.commit()
    await message.answer("Фото добавлено ✔️", reply_markup=photos_menu_kb())
    await state.clear()

# ---- Кухня: Stop-list / To-go list
def kitchen_items(kind: str) -> List[sqlite3.Row]:
    cursor.execute("SELECT * FROM kitchen_lists WHERE kind=? ORDER BY id DESC", (kind,))
    return cursor.fetchall()

@router.callback_query(F.data == "kitchen_main")
async def kitchen_main(call: CallbackQuery):
    if not is_staff_or_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.message.answer("Кухня:", reply_markup=kitchen_main_kb())
    await call.answer()

@router.callback_query(F.data.in_({"kitchen_stop", "kitchen_togo"}))
async def kitchen_list_open(call: CallbackQuery):
    if not is_staff_or_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    kind = "stop" if call.data == "kitchen_stop" else "togo"
    items = kitchen_items(kind)
    await call.message.answer(
        "Список позиций:" if items else "Список пуст.",
        reply_markup=kitchen_list_kb(kind, items)
    )
    await call.answer()

@router.callback_query(F.data.startswith("klist_add_"))
async def klist_add(call: CallbackQuery, state: FSMContext):
    if not is_staff_or_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    kind = call.data.split("_")[-1]
    await state.update_data(kind=kind)
    await state.set_state(UserEditFSM.value)
    await call.message.answer("Введите название позиции для добавления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="kitchen_main")]
    ]))
    await call.answer()

@router.callback_query(F.data.startswith("klist_del_"))
async def klist_del(call: CallbackQuery):
    if not is_staff_or_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    kind = call.data.split("_")[-1]
    items = kitchen_items(kind)
    if not items:
        await call.message.answer("Список пуст.", reply_markup=kitchen_main_kb())
        return await call.answer()
    kb = InlineKeyboardBuilder()
    for it in items:
        kb.button(text=f"🗑 {it['item_title']}", callback_data=f"klist_rm_{kind}_{it['id']}")
    kb.button(text="🔙 Назад", callback_data="kitchen_main")
    await call.message.answer("Выберите позицию для удаления:", reply_markup=kb.as_markup())
    await call.answer()

@router.callback_query(F.data.startswith("klist_rm_"))
async def klist_rm(call: CallbackQuery):
    if not is_staff_or_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    _, _, kind, sid = call.data.split("_")
    cursor.execute("DELETE FROM kitchen_lists WHERE id=? AND kind=?", (int(sid), kind))
    conn.commit()
    await call.message.answer("Удалено.", reply_markup=kitchen_main_kb())
    await call.answer()

@router.message(UserEditFSM.value)
async def klist_add_value_or_user_edit_value(message: Message, state: FSMContext):
    """
    Мульти-используемое состояние:
    - когда ожидаем ввод названия позиции кухни (после klist_add),
    - а также когда редактируем поле сотрудника (путь выше).
    Определим по наличию 'kind' в state.
    """
    data = await state.get_data()
    if "kind" in data:
        title = message.text.strip()
        kind = data["kind"]
        cursor.execute("INSERT INTO kitchen_lists (kind, item_title) VALUES (?,?)", (kind, title))
        conn.commit()
        await message.answer("Добавлено.", reply_markup=kitchen_main_kb())
        await state.clear()
    else:
        # это ветка редактирования пользователя — уже обработана выше в user_edit_value,
        # сюда попадать не должна, но оставим на случай переиспользования
        await message.answer("Готово.", reply_markup=users_menu_kb())
        await state.clear()

# -----------------------------------------------------------------------------
# ADMIN/STAFF списки бронирований (добавлено по ТЗ)
# -----------------------------------------------------------------------------
@router.callback_query(F.data == "adm_bookings")
async def adm_bookings_list(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    rows = list_future_bookings_for_cards()
    if not rows:
        await call.message.answer("Нет будущих бронирований.", reply_markup=admin_menu_inline())
        return await call.answer()
    for r in rows:
        card = (f"📌 Бронь #{r['id']}\n"
                f"👤 {r['fullname'] or '—'}\n"
                f"📞 {r['phone'] or '—'}\n"
                f"📅 {r['datetime']}\n"
                f"📝 {r['notes'] or '—'}\n"
                f"🔐 Согласие: {r['consent'] or '—'}\n"
                f"Статус: {r['status'] or '—'}")
        await call.message.answer(card)
    await call.message.answer("— Конец списка —", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_admin")]
    ]))
    await call.answer()

@router.callback_query(F.data == "staff_bookings")
async def staff_bookings_list(call: CallbackQuery):
    if not is_staff_or_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    lines = list_future_bookings_for_staff_list()
    if not lines:
        await call.message.answer("Нет будущих бронирований.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="staff_menu")]
        ]))
        return await call.answer()
    await call.message.answer("Ближайшие бронирования:\n" + "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="staff_menu")]
    ]))
    await call.answer()

# ------------------------- Самопроверки -------------------------
# (оставляем раздел как маркер)

# -----------------------------------------------------------------------------
# Планировщик (очистка прошедших бронирований + дайджест)
# -----------------------------------------------------------------------------
async def morning_digest_job():
    # Здесь может быть логика отправки утреннего дайджеста
    pass

# -----------------------------------------------------------------------------
# Регистрация планировщика и запуск бота
# -----------------------------------------------------------------------------
async def on_startup():
    scheduler.add_job(morning_digest_job, "cron", hour=7, minute=50, id="morning_digest")
    scheduler.add_job(cleanup_past_bookings, 'cron', hour=23, minute=59, id='cleanup_past')
    scheduler.start()
    logger.info("Scheduler started")

def setup_handlers():
    # Все обработчики уже навешаны через router
    pass

async def main():
    setup_handlers()
    await on_startup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
