# VERA Bot — версия v0.4.0 (Router + aiogram 3.7+ совместимость)
# Полный файл с доработками по ТЗ от админа:
# - /start исправлен; Router/Dispatcher/MemoryStorage; DefaultBotProperties(parse_mode=HTML)
# - Бронирование: на каждом шаге кнопки «Назад» и «Отмена»
# - «Связаться с нами» ➜ «Обратная связь»; кнопки: Официальный сайт / Связаться с нами ➜ Управляющая / Тех. специалист (+ Назад)
# - Админ → Пользователи: «Создать» (FSM пошагово), «Изменить» (список ФИО ➜ редактирование карточки: ФИО, телефон, паспорт; ID/username показываются), «Удалить»
# - Админ → «Импорт/экспорт»: экспорт/импорт CSV для: бронирований / сотрудников / меню
# - Меню управления, Кухня (Stop-list / To-go list) — сохранены и работают
# - Управление фото (замена/удаление) — сохранено
# - Везде где уместно — «Назад» (кроме главного меню)
# Требования: aiogram>=3.7.0, apscheduler, sqlite3, (опционально cryptography)
# Установка: pip install aiogram apscheduler cryptography

import asyncio
import logging
import sqlite3
import base64
import os
import traceback
import csv
import io
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from aiogram import Bot, Dispatcher, F, types, Router
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
    BufferedInputFile, ContentType
)
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ---------------------------------------------------------
# ЛОГИ
# ---------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("vera-bot")

# ---------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------
API_TOKEN = "8425551477:AAEE7yev_SzY07UDu5qjuHLcy2LBGLV-Jg0"  # ВАШ ТОКЕН

# Добавьте сюда свой Telegram user_id, чтобы быть «супер-админом» (параллельно с базой)
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "1077878777").split(",") if x.strip().isdigit()}

DB_FILE = os.environ.get("DB_FILE", "vera.db")
KEYFILE = os.environ.get("KEYFILE", "vera.key")

VERSION = "v0.4.0-patch"

# Роли
ROLE_GUEST = 0
ROLE_STAFF = 1
ROLE_ADMIN = 2

# Контакты для «Обратной связи»
MANAGER_USERNAME = "AnnaBardo_nova"
TECH_USERNAME = "Arailon"
OFFICIAL_SITE_URL = "https://example.com"   # при необходимости замените

# Шифрование (опционально)
try:
    from cryptography.fernet import Fernet
    HAS_FERNET = True
except Exception:
    HAS_FERNET = False

def load_key() -> bytes:
    if os.path.exists(KEYFILE):
        with open(KEYFILE, "rb") as f:
            return f.read()
    else:
        if HAS_FERNET:
            k = Fernet.generate_key()
        else:
            # случайный 32-байт ключ в base64
            k = base64.urlsafe_b64encode(os.urandom(32))
        with open(KEYFILE, "wb") as f:
            f.write(k)
        return k

if HAS_FERNET:
    FERNET = Fernet(load_key())
else:
    FERNET = None

# ---------------------------------------------------------
# Утилита безопасного редактирования сообщения
# ---------------------------------------------------------
async def safe_edit(message: Message, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None):
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        try:
            if reply_markup:
                await message.answer(text, reply_markup=reply_markup)
            else:
                await message.answer(text)
        except Exception:
            pass

# ---------------------------------------------------------
# БД
# ---------------------------------------------------------
conn = sqlite3.connect(DB_FILE)
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

# Таблицы
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    role INTEGER DEFAULT 0,
    fullname TEXT,
    phone TEXT
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
CREATE TABLE IF NOT EXISTS menu_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    price REAL NOT NULL DEFAULT 0,
    category TEXT,
    photo_url TEXT,
    is_active INTEGER NOT NULL DEFAULT 1
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

# ---- Миграции: добавляем username и passport в users ----
def ensure_users_extra_columns():
    try:
        cursor.execute("PRAGMA table_info(users)")
        cols = [r[1] for r in cursor.fetchall()]
        if "username" not in cols:
            cursor.execute("ALTER TABLE users ADD COLUMN username TEXT")
        if "passport" not in cols:
            cursor.execute("ALTER TABLE users ADD COLUMN passport TEXT")
        conn.commit()
    except Exception as e:
        logger.error("Migration users extra columns error: %s", e)
ensure_users_extra_columns()

# ---------------------------------------------------------
# Хелперы ролей и доступа
# ---------------------------------------------------------
def get_role(user_id: int) -> int:
    # супер-админ через конфиг
    if user_id in ADMIN_IDS:
        return ROLE_ADMIN
    cursor.execute("SELECT role FROM users WHERE user_id=?", (user_id,))
    r = cursor.fetchone()
    if not r:
        return ROLE_GUEST
    role_value = r["role"]
    if isinstance(role_value, str):
        role_map = {"guest": ROLE_GUEST, "staff": ROLE_STAFF, "admin": ROLE_ADMIN}
        return role_map.get(role_value.lower(), ROLE_GUEST)
    return int(role_value or 0)

def is_admin(user_id: int) -> bool:
    return get_role(user_id) == ROLE_ADMIN

def is_staff(user_id: int) -> bool:
    return get_role(user_id) in (ROLE_STAFF, ROLE_ADMIN)

def set_role(user_id: int, role: int):
    cursor.execute("INSERT OR IGNORE INTO users (user_id, role) VALUES (?,?)", (user_id, role))
    cursor.execute("UPDATE users SET role=? WHERE user_id=?", (role, user_id))
    conn.commit()

def set_or_update_user(user_id: int, role: Optional[int]=None, fullname: Optional[str]=None,
                       phone: Optional[str]=None, username: Optional[str]=None, passport: Optional[str]=None):
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

# ---------------------------------------------------------
# FSM — бронирование
# ---------------------------------------------------------
class BookingFSM(StatesGroup):
    fullname = State()
    phone = State()
    datetime = State()
    source = State()
    notes = State()

# ---------------------------------------------------------
# FSM — добавление/редактирование меню
# ---------------------------------------------------------
class MenuAddFSM(StatesGroup):
    title = State()
    description = State()
    price = State()
    category = State()
    photo = State()
    confirm = State()

class EditMenuFSM(StatesGroup):
    waiting_value = State()

# ---------------------------------------------------------
# FSM — фото (админ)
# ---------------------------------------------------------
class PhotoAddFSM(StatesGroup):
    waiting_photo = State()

class PhotoEditFSM(StatesGroup):
    waiting_new_photo = State()
    photo_id = State()

# ---------------------------------------------------------
# FSM — сотрудники (админ)
# ---------------------------------------------------------
class StaffCreateFSM(StatesGroup):
    fullname = State()
    phone = State()
    user_id = State()
    passport = State()
    confirm = State()

class StaffEditFSM(StatesGroup):
    waiting_value = State()
    user_id = State()
    field = State()

# ---------------------------------------------------------
# FSM — импорт CSV
# ---------------------------------------------------------
class ImportFSM(StatesGroup):
    import_type = State()   # 'menu'|'bookings'|'staff'
    waiting_file = State()

# ---------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------
def main_menu_inline(user_id: int) -> InlineKeyboardMarkup:
    role = get_role(user_id)
    kb = [
        [InlineKeyboardButton(text="📅 Забронировать столик", callback_data="main_book")],
        [InlineKeyboardButton(text="📖 Посмотреть меню", callback_data="main_menu")],
        [InlineKeyboardButton(text="📸 Фотографии", callback_data="main_photos")],
        [InlineKeyboardButton(text="📨 Обратная связь", callback_data="main_feedback")],
    ]
    if role in (ROLE_STAFF, ROLE_ADMIN):
        kb.append([InlineKeyboardButton(text="👨‍🍳 Staff-меню", callback_data="staff_menu")])
        kb.append([InlineKeyboardButton(text="⚙️ Админ меню", callback_data="main_admin")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def back_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_main")]
    ])

def feedback_root_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Официальный сайт", url=OFFICIAL_SITE_URL)],
        [InlineKeyboardButton(text="📲 Связаться с нами", callback_data="feedback_contacts")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")]
    ])

def feedback_contacts_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👩‍💼 Управляющая", url=f"https://t.me/{MANAGER_USERNAME}")],
        [InlineKeyboardButton(text="🛠 Тех. специалист", url=f"https://t.me/{TECH_USERNAME}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_feedback")]
    ])

def admin_menu_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="adm_users")],
        [InlineKeyboardButton(text="📦 Импорт/экспорт", callback_data="adm_io")],
        [InlineKeyboardButton(text="📸 Управление фото", callback_data="adm_photos")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")],
    ])

def users_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать", callback_data="adm_users_create")],
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="adm_users_edit")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="adm_users_delete")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_admin")]
    ])

def menu_manage_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить позицию", callback_data="menu_add")],
        [InlineKeyboardButton(text="📋 Список / Редактировать", callback_data="menu_list")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="staff_menu")]
    ])

def menu_browse_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Открыть сайт", url="https://example.com/menu")],
        [InlineKeyboardButton(text="🗂 Внутри бота", callback_data="menu_inside")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")]
    ])

def menu_categories_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍽 Еда", callback_data="menu_cat_Еда")],
        [InlineKeyboardButton(text="🥤 Напитки", callback_data="menu_cat_Напитки")],
        [InlineKeyboardButton(text="🍰 Десерты", callback_data="menu_cat_Десерты")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]
    ])

def kitchen_root_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛑 Stop-list", callback_data="kitchen_list_stop")],
        [InlineKeyboardButton(text="📦 To-go list", callback_data="kitchen_list_togo")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="staff_menu")]
    ])

def booking_nav_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="book_back"),
            InlineKeyboardButton(text="✖️ Отмена", callback_data="book_cancel")
        ]
    ])

def staff_nav_kb(cancel_cb: str = "adm_users") -> InlineKeyboardMarkup:
    # универсальная навигация для FSM сотрудников
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="staff_back"),
            InlineKeyboardButton(text="✖️ Отмена", callback_data="staff_cancel")
        ]
    ])

def io_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬇️ Экспорт бронирований (CSV)", callback_data="io_export_bookings")],
        [InlineKeyboardButton(text="⬇️ Экспорт сотрудников (CSV)", callback_data="io_export_staff")],
        [InlineKeyboardButton(text="⬇️ Экспорт меню (CSV)", callback_data="io_export_menu")],
        [InlineKeyboardButton(text="⬆️ Импорт бронирований (CSV)", callback_data="io_import_bookings")],
        [InlineKeyboardButton(text="⬆️ Импорт сотрудников (CSV)", callback_data="io_import_staff")],
        [InlineKeyboardButton(text="⬆️ Импорт меню (CSV)", callback_data="io_import_menu")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_admin")]
    ])

# ---------------------------------------------------------
# Бот, Диспетчер, Router, Планировщик
# ---------------------------------------------------------
bot = Bot(
    API_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
scheduler = AsyncIOScheduler()

# ---------------------------------------------------------
# /start
# ---------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message):
    uid = message.from_user.id
    uname = message.from_user.username
    full_display_name = message.from_user.full_name
    # создаём/обновляем пользователя
    cursor.execute("INSERT OR IGNORE INTO users (user_id, role) VALUES (?, ?)", (uid, ROLE_GUEST))
    # сохраним username и ФИО, если пусто
    cursor.execute("SELECT fullname, username FROM users WHERE user_id=?", (uid,))
    r = cursor.fetchone()
    to_fullname = r["fullname"]
    to_username = r["username"] if "username" in r.keys() else None
    if not to_fullname and full_display_name:
        cursor.execute("UPDATE users SET fullname=? WHERE user_id=?", (full_display_name, uid))
    if uname and not to_username:
        cursor.execute("UPDATE users SET username=? WHERE user_id=?", (uname, uid))
    conn.commit()

    await message.answer(
        f"Добро пожаловать в кофейню <b>VERA</b>! ✨☕️\n"
        f"Мы рады видеть вас! 🌿\n\nВерсия: {VERSION}\n"
        f"☺️ Выберите действие ниже:",
        reply_markup=main_menu_inline(uid)
    )

# ---------------------------------------------------------
# Главное меню
# ---------------------------------------------------------
@router.callback_query(F.data == "back_main")
async def back_main(call: CallbackQuery):
    try:
        await call.message.edit_text("🏠 Главное меню:", reply_markup=main_menu_inline(call.from_user.id))
    except Exception:
        await call.message.answer("🏠 Главное меню:", reply_markup=main_menu_inline(call.from_user.id))
    await call.answer()

# ------ «Обратная связь» ------
@router.callback_query(F.data == "main_feedback")
async def main_feedback(call: CallbackQuery):
    await safe_edit(call.message, "📨 Обратная связь", reply_markup=feedback_root_kb())
    await call.answer()

@router.callback_query(F.data == "feedback_contacts")
async def feedback_contacts(call: CallbackQuery):
    await safe_edit(call.message, "Выберите, с кем связаться:", reply_markup=feedback_contacts_kb())
    await call.answer()

# ------ Меню ------
@router.callback_query(F.data == "main_menu")
async def main_menu_browse(call: CallbackQuery):
    kb = menu_browse_kb()
    await safe_edit(call.message, "Как вам удобнее посмотреть меню? 🌐 На сайте или прямо здесь:", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "main_photos")
async def main_photos(call: CallbackQuery):
    cursor.execute("SELECT * FROM photos ORDER BY id DESC LIMIT 10")
    rows = cursor.fetchall()
    if not rows:
        return await call.message.answer("Пока нет фотографий. Загляните позже ☺️", reply_markup=back_main_kb())
    for r in rows:
        try:
            await call.message.answer_photo(r["file_id"], caption=(r["caption"] or ""))
        except:
            pass
    await call.answer()

# ---------------------------------------------------------
# Меню (пользовательский просмотр)
# ---------------------------------------------------------
@router.callback_query(F.data == "menu_inside")
async def menu_inside(call: CallbackQuery):
    await safe_edit(call.message, "📋 Пожалуйста, выберите категорию блюд:", reply_markup=menu_categories_kb())
    await call.answer()

@router.callback_query(F.data.startswith("menu_cat_"))
async def menu_show_category(call: CallbackQuery):
    cat = call.data.split("_", 2)[-1]
    cursor.execute(
        "SELECT * FROM menu_items WHERE is_active=1 AND (category=? OR ?='' AND (category IS NULL OR category='')) ORDER BY title",
        (cat, cat)
    )
    rows = cursor.fetchall()
    if not rows:
        return await call.message.answer("Пока пусто в этой категории. Загляните чуть позже 💛", reply_markup=menu_categories_kb())
    for r in rows:
        text = f"<b>{r['title']}</b>\n"
        if r["description"]:
            text += r["description"] + "\n"
        text += f"💳 {r['price']:.2f}\n"
        if r["photo_url"]:
            if r["photo_url"].startswith("file_id:"):
                try:
                    await call.message.answer_photo(r["photo_url"].split(":", 1)[1], caption=text)
                except:
                    await call.message.answer(text)
            else:
                await call.message.answer(text)
        else:
            await call.message.answer(text)
    await call.answer()

# ---------------------------------------------------------
# Бронирование (гость) — с «Назад» и «Отмена» на каждом шаге
# ---------------------------------------------------------
@router.callback_query(F.data == "main_book")
async def booking_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(BookingFSM.fullname)
    await call.message.answer("🌸 Представьтесь, пожалуйста: имя и фамилия — чтобы мы обращались к вам красиво:",
                              reply_markup=booking_nav_kb())
    await call.answer()

@router.message(BookingFSM.fullname)
async def booking_fullname(message: Message, state: FSMContext):
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
    await message.answer("📌 Поделитесь, как вы о нас узнали (Instagram, друзья и т.п.):", reply_markup=booking_nav_kb())

@router.message(BookingFSM.source)
async def booking_source(message: Message, state: FSMContext):
    await state.update_data(source=message.text.strip())
    await state.set_state(BookingFSM.notes)
    await message.answer("📝 Особые пожелания к визиту:", reply_markup=booking_nav_kb())

@router.message(BookingFSM.notes)
async def booking_notes(message: Message, state: FSMContext):
    await state.update_data(notes=message.text.strip())
    data = await state.get_data()
    cursor.execute("""
        INSERT INTO bookings (user_id, fullname, phone, datetime, source, notes, status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (message.from_user.id, data["fullname"], data["phone"], data["datetime"], data["source"], data["notes"], "pending"))
    conn.commit()
    await message.answer("Спасибо! Ваша заявка на бронь принята. Мы свяжемся с вами при необходимости 💐", reply_markup=back_main_kb())
    await state.clear()

@router.callback_query(F.data == "book_cancel")
async def booking_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("❌ Бронирование отменено. Возвращаемся в главное меню.", reply_markup=main_menu_inline(call.from_user.id))
    await call.answer()

@router.callback_query(F.data == "book_back")
async def booking_back(call: CallbackQuery, state: FSMContext):
    current = await state.get_state()
    # Переходим на предыдущий шаг
    if current == BookingFSM.fullname.state:
        await state.clear()
        await call.message.answer("🏠 Главное меню:", reply_markup=main_menu_inline(call.from_user.id))
    elif current == BookingFSM.phone.state:
        await state.set_state(BookingFSM.fullname)
        await call.message.answer("🌸 Представьтесь, пожалуйста: имя и фамилия — чтобы мы обращались к вам красиво:",
                                  reply_markup=booking_nav_kb())
    elif current == BookingFSM.datetime.state:
        await state.set_state(BookingFSM.phone)
        await call.message.answer("📞 Оставьте номер телефона для связи:", reply_markup=booking_nav_kb())
    elif current == BookingFSM.source.state:
        await state.set_state(BookingFSM.datetime)
        await call.message.answer("📅 Когда вам будет удобно прийти? (например: 28.08 15:30):", reply_markup=booking_nav_kb())
    elif current == BookingFSM.notes.state:
        await state.set_state(BookingFSM.source)
        await call.message.answer("📌 Поделитесь, как вы о нас узнали (Instagram, друзья и т.п.):", reply_markup=booking_nav_kb())
    else:
        await state.clear()
        await call.message.answer("🏠 Главное меню:", reply_markup=main_menu_inline(call.from_user.id))
    await call.answer()

# ---------------------------------------------------------
# Напоминания о бронях (+ подтверждение/отмена)
# ---------------------------------------------------------
async def remind_single_booking(booking: dict):
    try:
        user_id = booking.get('user_id')
        bid = booking.get('id')
        text = (f"⏰ Напоминание! Ваше бронирование через 1 час:\n\n"
                f"👤 {booking.get('fullname')}\n"
                f"📅 {booking.get('datetime')}\n"
                f"📞 {booking.get('phone')}\n"
                f"📝 {booking.get('notes')}\n\n"
                f"Пожалуйста, подтвердите бронь:")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"book_confirm_{bid}"),
            InlineKeyboardButton(text="❌ Отменить", callback_data=f"book_cancel_{bid}")
        ]])
        if user_id:
            try:
                await bot.send_message(user_id, text, reply_markup=kb)
                cursor.execute("UPDATE bookings SET status=? WHERE id=?", ("pending", bid))
                conn.commit()
                scheduler.add_job(
                    auto_confirm_booking, 'date',
                    run_date=datetime.now() + timedelta(minutes=30),
                    args=[bid],
                    id=f"autoconf_{bid}_{int(datetime.now().timestamp())}"
                )
            except Exception as e:
                logger.error("send reminder error: %s", e)
        cursor.execute("SELECT user_id FROM users WHERE role=?", (ROLE_ADMIN,))
        for r in cursor.fetchall():
            try:
                await bot.send_message(r['user_id'], "Напоминание (для гостя):\n" + text)
            except:
                pass
    except Exception:
        traceback.print_exc()

async def auto_confirm_booking(booking_id: int):
    try:
        cursor.execute("SELECT status FROM bookings WHERE id=?", (booking_id,))
        r = cursor.fetchone()
        if not r:
            return
        status = r["status"] if "status" in r.keys() else None
        if status in (None, "", "pending"):
            cursor.execute("UPDATE bookings SET status=? WHERE id=?", ("confirmed", booking_id))
            conn.commit()
    except Exception as e:
        logger.error("auto_confirm_booking error: %s", e)

@router.callback_query(F.data.startswith("book_confirm_"))
async def booking_confirm_cb(call: CallbackQuery):
    try:
        bid = int(call.data.split("_")[-1])
    except:
        return await call.answer("Ошибка", show_alert=True)
    cursor.execute("UPDATE bookings SET status=? WHERE id=?", ("confirmed", bid))
    conn.commit()
    await call.message.answer("✅ Спасибо! Ваша бронь подтверждена. Ждём вас и готовим лучший столик ✨")
    await call.answer()

@router.callback_query(F.data.startswith("book_cancel_"))
async def booking_cancel_cb(call: CallbackQuery):
    try:
        bid = int(call.data.split("_")[-1])
    except:
        return await call.answer("Ошибка", show_alert=True)
    cursor.execute("UPDATE bookings SET status=? WHERE id=?", ("cancelled", bid))
    conn.commit()
    await call.message.answer("😔 Бронь отменена. Если захотите вернуться — мы всегда рады вам!")
    await call.answer()

async def remind_booking_job():
    try:
        now = datetime.now()
        cursor.execute("SELECT * FROM bookings")
        for r in cursor.fetchall():
            try:
                when = r["datetime"]
                dt = None
                for fmt in ("%d.%m %H:%M", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"):
                    try:
                        dt = datetime.strptime(when, fmt)
                        break
                    except:
                        pass
                if not dt:
                    continue
                if dt.year == 1900:
                    dt = dt.replace(year=now.year)
                delta = dt - now
                if timedelta(minutes=59) <= delta <= timedelta(minutes=61):
                    await remind_single_booking(dict(r))
            except:
                pass
    except:
        traceback.print_exc()

async def morning_digest_job():
    # можно добавить рассылку для админов
    pass

# ---------------------------------------------------------
# Управление меню (Staff/Admin) — добавление/редактирование
# ---------------------------------------------------------
@router.callback_query(F.data == "adm_menu_manage")
async def adm_menu_manage(call: CallbackQuery):
    role = get_role(call.from_user.id)
    if role not in (ROLE_STAFF, ROLE_ADMIN):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await safe_edit(call.message, "🍽 Управление меню:", reply_markup=menu_manage_inline())
    await call.answer()

@router.callback_query(F.data == "menu_add")
async def menu_add_start(call: CallbackQuery, state: FSMContext):
    role = get_role(call.from_user.id)
    if role not in (ROLE_STAFF, ROLE_ADMIN):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await state.set_state(MenuAddFSM.title)
    await call.message.answer("Введите название позиции:")
    await call.answer()

@router.message(MenuAddFSM.title)
async def menu_add_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(MenuAddFSM.description)
    await message.answer("Введите описание (или '-' чтобы пропустить):")

@router.message(MenuAddFSM.description)
async def menu_add_desc(message: Message, state: FSMContext):
    txt = message.text.strip()
    await state.update_data(description="" if txt == "-" else txt)
    await state.set_state(MenuAddFSM.price)
    await message.answer("Введите цену (число.xx):")

@router.message(MenuAddFSM.price)
async def menu_add_price(message: Message, state: FSMContext):
    try:
        price = float(message.text.replace(",", ".").strip())
    except:
        return await message.answer("Пожалуйста, введите цену числом, например: 249.00")
    await state.update_data(price=price)
    await state.set_state(MenuAddFSM.category)
    await message.answer(
        "Выберите категорию или введите свою:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Еда", callback_data="addcat_Еда"),
            InlineKeyboardButton(text="Напитки", callback_data="addcat_Напитки"),
            InlineKeyboardButton(text="Десерты", callback_data="addcat_Десерты")
        ]])
    )

@router.message(MenuAddFSM.category)
async def menu_add_category(message: Message, state: FSMContext):
    await state.update_data(category=message.text.strip())
    await state.set_state(MenuAddFSM.photo)
    await message.answer("Можно отправить фото (из Telegram) или ввести ссылку на фото (URL), или '-' чтобы пропустить:")

@router.callback_query(F.data.startswith("addcat_"))
async def addcat_cb(call: CallbackQuery, state: FSMContext):
    cat = call.data.split("_", 1)[1]
    await state.update_data(category=cat)
    await call.message.answer("Можно отправить фото (из Telegram) или ввести ссылку на фото (URL), или '-' чтобы пропустить:")
    await state.set_state(MenuAddFSM.photo)
    await call.answer()

@router.message(MenuAddFSM.photo)
async def menu_add_photo(message: Message, state: FSMContext):
    txt = message.text.strip()
    photo = "" if txt == "-" else txt
    await state.update_data(photo=photo)
    data = await state.get_data()
    text = (f"Проверьте данные:\n\n<b>{data['title']}</b>\n{data.get('description') or ''}\n"
            f"Категория: {data.get('category')}\nЦена: {data.get('price')}\nФото: {data.get('photo') or '—'}")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✔️ Подтвердить", callback_data="menu_add_confirm_yes"),
        InlineKeyboardButton(text="✖️ Отменить", callback_data="menu_add_confirm_no")
    ]])
    await state.set_state(MenuAddFSM.confirm)
    await message.answer(text, reply_markup=kb)

@router.message(F.photo, MenuAddFSM.photo)
async def menu_add_photo_file(message: Message, state: FSMContext):
    photo = message.photo[-1].file_id
    await state.update_data(photo=f"file_id:{photo}")
    data = await state.get_data()
    text = (f"Проверьте данные:\n\n<b>{data['title']}</b>\n{data.get('description') or ''}\n"
            f"Категория: {data.get('category')}\nЦена: {data.get('price')}\nФото: {'(загружено)'}")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✔️ Подтвердить", callback_data="menu_add_confirm_yes"),
        InlineKeyboardButton(text="✖️ Отменить", callback_data="menu_add_confirm_no")
    ]])
    await state.set_state(MenuAddFSM.confirm)
    await message.answer(text, reply_markup=kb)

@router.callback_query(F.data.startswith("menu_add_confirm_"))
async def menu_add_confirm_cb(call: CallbackQuery, state: FSMContext):
    if call.data.endswith("no"):
        await state.clear()
        await call.message.answer(
            "❌ Добавление отменено.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="adm_menu_manage")]])
        )
        return await call.answer()
    data = await state.get_data()
    try:
        cursor.execute(
            "INSERT INTO menu_items (title, description, price, category, photo_url, is_active) VALUES (?, ?, ?, ?, ?, ?)",
            (data.get("title"), data.get("description") or "", data.get("price") or 0.0,
             data.get("category") or "Еда", data.get("photo") or "", 1)
        )
        conn.commit()
        await call.message.answer(
            "✅ Позиция добавлена.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="adm_menu_manage")]])
        )
    except Exception as e:
        await call.message.answer("Ошибка при добавлении позиции: " + str(e))
    await state.clear()
    await call.answer()

@router.callback_query(F.data == "menu_list")
async def menu_list(call: CallbackQuery):
    role = get_role(call.from_user.id)
    if role not in (ROLE_STAFF, ROLE_ADMIN):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    cursor.execute("SELECT * FROM menu_items ORDER BY id DESC")
    rows = cursor.fetchall()
    if not rows:
        return await call.message.answer("Пока нет позиций меню.", reply_markup=menu_manage_inline())
    for r in rows:
        text = f"<b>{r['title']}</b>\n"
        if r["description"]:
            text += r["description"] + "\n"
        text += f"💳 {r['price']:.2f}\nКатегория: {r['category'] or '—'}\nАктивно: {'Да' if r['is_active'] else 'Нет'}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"menu_edit_{r['id']}")],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"menu_delete_{r['id']}")]
        ])
        if r["photo_url"] and r["photo_url"].startswith("file_id:"):
            try:
                await call.message.answer_photo(r["photo_url"].split(":", 1)[1], caption=text, reply_markup=kb)
            except:
                await call.message.answer(text, reply_markup=kb)
        else:
            await call.message.answer(text, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("menu_delete_"))
async def menu_delete(call: CallbackQuery):
    role = get_role(call.from_user.id)
    if role not in (ROLE_STAFF, ROLE_ADMIN):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    try:
        mid = int(call.data.split("_")[-1])
    except:
        return await call.answer("Неверные данные", show_alert=True)
    cursor.execute("DELETE FROM menu_items WHERE id=?", (mid,))
    conn.commit()
    await call.message.answer("🗑 Позиция удалена.")
    await call.answer()

@router.callback_query(F.data.startswith("menu_edit_"))
async def menu_edit_start(call: CallbackQuery, state: FSMContext):
    role = get_role(call.from_user.id)
    if role not in (ROLE_ADMIN, ROLE_STAFF):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    try:
        mid = int(call.data.split("_")[-1])
    except:
        return await call.answer("Неверные данные", show_alert=True)
    cursor.execute("SELECT * FROM menu_items WHERE id=?", (mid,))
    row = cursor.fetchone()
    if not row:
        return await call.answer("Позиция не найдена", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Название", callback_data=f"menu_field_{mid}_title"),
         InlineKeyboardButton(text="Описание", callback_data=f"menu_field_{mid}_description")],
        [InlineKeyboardButton(text="Цена", callback_data=f"menu_field_{mid}_price"),
         InlineKeyboardButton(text="Категория", callback_data=f"menu_field_{mid}_category")],
        [InlineKeyboardButton(text="Фото", callback_data=f"menu_field_{mid}_photo"),
         InlineKeyboardButton(text="Активность", callback_data=f"menu_field_{mid}_active")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_list")]
    ])
    await call.message.answer("Выберите поле для редактирования:", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("menu_field_"))
async def menu_field_choice(call: CallbackQuery, state: FSMContext):
    parts = call.data.split("_")
    try:
        mid = int(parts[2])
    except:
        return await call.answer("Неверный ID", show_alert=True)
    field = parts[3]
    await state.update_data(menu_edit_id=mid, menu_edit_field=field)
    if field == "photo":
        await call.message.answer(f"Отправьте новое фото (или URL) для {field} (или '-' чтобы очистить):")
    else:
        await call.message.answer(f"Введите новое значение для {field} (или '-' чтобы пусто):")
    await EditMenuFSM.waiting_value.set()
    await call.answer()

@router.message(EditMenuFSM.waiting_value)
async def menu_edit_set_value(message: Message, state: FSMContext):
    data = await state.get_data()
    mid = data.get("menu_edit_id")
    field = data.get("menu_edit_field")
    val = message.text.strip()
    if field == "price":
        if val == "-":
            val = 0.0
        else:
            try:
                val = float(val.replace(",", "."))
            except:
                return await message.answer("Цена должна быть числом. Попробуйте снова.")
        cursor.execute("UPDATE menu_items SET price=? WHERE id=?", (val, mid))
    elif field == "active":
        new_val = 0 if str(val).lower() in ("0", "no", "нет", "-") else 1
        cursor.execute("UPDATE menu_items SET is_active=? WHERE id=?", (new_val, mid))
    elif field == "photo":
        if val == "-":
            cursor.execute("UPDATE menu_items SET photo_url='' WHERE id=?", (mid,))
        else:
            cursor.execute("UPDATE menu_items SET photo_url=? WHERE id=?", (val, mid))
    else:
        if val == "-":
            val = ""
        cursor.execute(f"UPDATE menu_items SET {field}=? WHERE id=?", (val, mid))
    conn.commit()
    await state.clear()
    await message.answer("✅ Изменения сохранены.", reply_markup=menu_manage_inline())

# ---------------------------------------------------------
# Пользователи (админ)
# ---------------------------------------------------------
@router.callback_query(F.data == "adm_users")
async def adm_users(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        # красивое сообщение для сотрудника без доступа
        if is_staff(call.from_user.id):
            await call.message.answer(
                "🌟 У вас пока нет доступа к разделу администратора.\n"
                "Если считаете, что это ошибка — свяжитесь с управляющим.",
                reply_markup=main_menu_inline(call.from_user.id)
            )
            try:
                await call.answer()
            except:
                pass
            return
        return await call.answer("⛔ Нет доступа", show_alert=True)
    cursor.execute("SELECT user_id, role, fullname, phone, username, passport FROM users ORDER BY user_id DESC LIMIT 50")
    rows = cursor.fetchall()
    text = "👥 Пользователи (последние 50):\n\n"
    for r in rows:
        text += f"{r['user_id']} — роль: {r['role']}, {r['fullname'] or ''} {('/@'+r['username']) if r['username'] else ''}\n"
    await safe_edit(call.message, text, reply_markup=users_menu_kb())
    await call.answer()

# ---- Создать сотрудника ----
@router.callback_query(F.data == "adm_users_create")
async def adm_users_create(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await state.set_state(StaffCreateFSM.fullname)
    await call.message.answer("Введите ФИО сотрудника:", reply_markup=staff_nav_kb())
    await call.answer()

@router.message(StaffCreateFSM.fullname)
async def staff_create_fullname(message: Message, state: FSMContext):
    await state.update_data(fullname=message.text.strip())
    await state.set_state(StaffCreateFSM.phone)
    await message.answer("Введите номер телефона сотрудника (+7...):", reply_markup=staff_nav_kb())

@router.message(StaffCreateFSM.phone)
async def staff_create_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text.strip())
    await state.set_state(StaffCreateFSM.user_id)
    await message.answer("Введите Telegram ID сотрудника (число):", reply_markup=staff_nav_kb())

@router.message(StaffCreateFSM.user_id)
async def staff_create_userid(message: Message, state: FSMContext):
    try:
        uid = int(message.text.strip())
    except:
        return await message.answer("Введите ID числом (например, 123456789).", reply_markup=staff_nav_kb())
    await state.update_data(user_id=uid)
    await state.set_state(StaffCreateFSM.passport)
    await message.answer("Введите паспортные данные в формате 0123 456789:", reply_markup=staff_nav_kb())

@router.message(StaffCreateFSM.passport)
async def staff_create_passport(message: Message, state: FSMContext):
    await state.update_data(passport=message.text.strip())
    data = await state.get_data()

    # попробуем подтянуть username, если пользователь уже запускал бота
    cursor.execute("SELECT username FROM users WHERE user_id=?", (data["user_id"],))
    r = cursor.fetchone()
    username = r["username"] if r and r["username"] else None

    text = (f"Проверьте данные сотрудника:\n\n"
            f"👤 ФИО: {data['fullname']}\n"
            f"📞 Телефон: {data['phone']}\n"
            f"🆔 ID: {data['user_id']}\n"
            f"🟦 Username: @{username if username else '—'}\n"
            f"🪪 Паспорт: {data['passport']}\n"
            f"Роль: Сотрудник")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✔️ Подтвердить", callback_data="staff_create_confirm_yes"),
        InlineKeyboardButton(text="✖️ Отменить", callback_data="staff_create_confirm_no")
    ]])
    await state.set_state(StaffCreateFSM.confirm)
    await message.answer(text, reply_markup=kb)

@router.callback_query(F.data == "staff_create_confirm_no")
async def staff_create_cancel_confirm(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("❌ Создание карточки отменено.", reply_markup=users_menu_kb())
    await call.answer()

@router.callback_query(F.data == "staff_create_confirm_yes")
async def staff_create_confirm(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    data = await state.get_data()
    uid = data["user_id"]
    # подтягиваем username, если есть в БД (когда сотрудник ранее открывал бота)
    cursor.execute("SELECT username FROM users WHERE user_id=?", (uid,))
    r = cursor.fetchone()
    username = r["username"] if r and r["username"] else None

    # создаем/обновляем карточку
    set_or_update_user(user_id=uid, role=ROLE_STAFF,
                       fullname=data["fullname"], phone=data["phone"],
                       username=username, passport=data["passport"])
    await state.clear()
    await call.message.answer("✅ Сотрудник создан/обновлен.", reply_markup=users_menu_kb())
    await call.answer()

@router.callback_query(F.data == "staff_cancel")
async def staff_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("❌ Действие отменено.", reply_markup=users_menu_kb())
    await call.answer()

@router.callback_query(F.data == "staff_back")
async def staff_back(call: CallbackQuery, state: FSMContext):
    current = await state.get_state()
    if current == StaffCreateFSM.phone.state:
        await state.set_state(StaffCreateFSM.fullname)
        await call.message.answer("Введите ФИО сотрудника:", reply_markup=staff_nav_kb())
    elif current == StaffCreateFSM.user_id.state:
        await state.set_state(StaffCreateFSM.phone)
        await call.message.answer("Введите номер телефона сотрудника (+7...):", reply_markup=staff_nav_kb())
    elif current == StaffCreateFSM.passport.state:
        await state.set_state(StaffCreateFSM.user_id)
        await call.message.answer("Введите Telegram ID сотрудника (число):", reply_markup=staff_nav_kb())
    elif current == StaffCreateFSM.confirm.state:
        await state.set_state(StaffCreateFSM.passport)
        await call.message.answer("Введите паспортные данные в формате 0123 456789:", reply_markup=staff_nav_kb())
    else:
        await state.clear()
        await call.message.answer("🔙 Возврат в раздел «Пользователи».", reply_markup=users_menu_kb())
    await call.answer()

# ---- Изменить карточку ----
def _staff_list(role_filter: Optional[List[int]] = None):
    if role_filter:
        cursor.execute("SELECT user_id, fullname, username FROM users WHERE role IN (%s) ORDER BY fullname COLLATE NOCASE"
                       % ",".join("?"*len(role_filter)), tuple(role_filter))
    else:
        cursor.execute("SELECT user_id, fullname, username FROM users ORDER BY fullname COLLATE NOCASE")
    return cursor.fetchall()

@router.callback_query(F.data == "adm_users_edit")
async def adm_users_edit(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    rows = _staff_list(role_filter=[ROLE_STAFF, ROLE_ADMIN])
    if not rows:
        return await call.message.answer("Нет сотрудников для редактирования.", reply_markup=users_menu_kb())
    kb = []
    for r in rows:
        fio = r["fullname"] or (("@" + r["username"]) if r["username"] else f"ID {r['user_id']}")
        kb.append([InlineKeyboardButton(text=fio, callback_data=f"user_edit_{r['user_id']}")])
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="adm_users")])
    await safe_edit(call.message, "Выберите сотрудника для редактирования:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await call.answer()

@router.callback_query(F.data.startswith("user_edit_"))
async def user_edit_open(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    uid = int(call.data.split("_")[-1])
    cursor.execute("SELECT user_id, role, fullname, phone, username, passport FROM users WHERE user_id=?", (uid,))
    r = cursor.fetchone()
    if not r:
        return await call.answer("Пользователь не найден", show_alert=True)
    text = (f"Карточка сотрудника:\n\n"
            f"👤 ФИО: {r['fullname'] or '—'}\n"
            f"📞 Телефон: {r['phone'] or '—'}\n"
            f"🆔 ID: {r['user_id']}\n"
            f"🟦 Username: @{r['username'] or '—'}\n"
            f"🪪 Паспорт: {r['passport'] or '—'}\n"
            f"Роль: {'Администратор' if r['role']==ROLE_ADMIN else 'Сотрудник' if r['role']==ROLE_STAFF else 'Гость'}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Изменить ФИО", callback_data=f"user_field_{uid}_fullname")],
        [InlineKeyboardButton(text="Изменить телефон", callback_data=f"user_field_{uid}_phone")],
        [InlineKeyboardButton(text="Изменить паспорт", callback_data=f"user_field_{uid}_passport")],
        [InlineKeyboardButton(text="Изменить роль", callback_data=f"user_field_{uid}_role")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm_users_edit")]
    ])
    await safe_edit(call.message, text, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("user_field_"))
async def user_field_choice(call: CallbackQuery, state: FSMContext):
    parts = call.data.split("_")
    uid = int(parts[2]); field = parts[3]
    await state.update_data(edit_user_id=uid, edit_field=field)
    if field == "role":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Сотрудник", callback_data="set_role_staff"),
             InlineKeyboardButton(text="Администратор", callback_data="set_role_admin")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data=f"user_edit_{uid}")]
        ])
        await call.message.answer("Выберите новую роль:", reply_markup=kb)
    else:
        await call.message.answer(f"Введите новое значение для «{field}» (или '-' чтобы очистить):", reply_markup=staff_nav_kb())
        await StaffEditFSM.waiting_value.set()
        await state.set_state(StaffEditFSM.waiting_value)
    await call.answer()

@router.callback_query(F.data.in_(["set_role_staff","set_role_admin"]))
async def set_role_cb(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    uid = data.get("edit_user_id")
    if uid is None:
        return await call.answer("Сессия редактирования потеряна", show_alert=True)
    new_role = ROLE_STAFF if call.data == "set_role_staff" else ROLE_ADMIN
    set_role(uid, new_role)
    await call.message.answer("✅ Роль обновлена.", reply_markup=users_menu_kb())
    await call.answer()

@router.message(StaffEditFSM.waiting_value)
async def staff_edit_value(message: Message, state: FSMContext):
    data = await state.get_data()
    uid = data.get("edit_user_id")
    field = data.get("edit_field")
    val = message.text.strip()
    if val == "-":
        val = ""
    if field not in {"fullname","phone","passport"}:
        await state.clear()
        return await message.answer("Неверное поле.", reply_markup=users_menu_kb())
    cursor.execute(f"UPDATE users SET {field}=? WHERE user_id=?", (val, uid))
    conn.commit()
    await state.clear()
    await message.answer("✅ Изменения сохранены.", reply_markup=users_menu_kb())

@router.callback_query(F.data == "adm_users_delete")
async def adm_users_delete(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    cursor.execute("SELECT user_id, fullname, username FROM users WHERE role=?", (ROLE_STAFF,))
    rows = cursor.fetchall()
    if not rows:
        return await call.message.answer("Нет сотрудников со статусом «Сотрудник».", reply_markup=users_menu_kb())
    kb = []
    for r in rows:
        fio = r["fullname"] or (("@" + r["username"]) if r["username"] else f"ID {r['user_id']}")
        kb.append([InlineKeyboardButton(text=f"Удалить: {fio}", callback_data=f"user_del_{r['user_id']}")])
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="adm_users")])
    await safe_edit(call.message, "Выберите сотрудника для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await call.answer()

@router.callback_query(F.data.startswith("user_del_"))
async def user_del_confirm(call: CallbackQuery):
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
    await call.answer()

# ---------------------------------------------------------
# Импорт / Экспорт (CSV)
# ---------------------------------------------------------
@router.callback_query(F.data == "adm_io")
async def adm_io(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await safe_edit(call.message, "📦 Импорт/экспорт CSV. Выберите действие:", reply_markup=io_menu_kb())
    await call.answer()

# Сохранён ради совместимости (не удаляем существующий callback)
@router.callback_query(F.data == "adm_export")
async def adm_export_redirect(call: CallbackQuery):
    # старое меню "Экспорт" теперь ведёт в "Импорт/экспорт"
    await adm_io(call)

def _rows_to_csv_buffer(headers: List[str], rows: List[sqlite3.Row]) -> bytes:
    with io.StringIO() as f:
        w = csv.writer(f, delimiter=';')
        w.writerow(headers)
        for r in rows:
            w.writerow([r[h] if h in r.keys() else "" for h in headers])
        return f.getvalue().encode("utf-8")

@router.callback_query(F.data == "io_export_menu")
async def io_export_menu(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    cursor.execute("SELECT id, title, description, price, category, photo_url, is_active FROM menu_items ORDER BY id DESC")
    rows = cursor.fetchall()
    content = _rows_to_csv_buffer(["id","title","description","price","category","photo_url","is_active"], rows)
    file = BufferedInputFile(content, filename="menu_export.csv")
    await call.message.answer_document(file, caption="Экспорт меню (CSV)")
    await call.answer()

@router.callback_query(F.data == "io_export_bookings")
async def io_export_bookings(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    cursor.execute("SELECT id, user_id, fullname, phone, datetime, source, notes, status FROM bookings ORDER BY id DESC")
    rows = cursor.fetchall()
    content = _rows_to_csv_buffer(["id","user_id","fullname","phone","datetime","source","notes","status"], rows)
    file = BufferedInputFile(content, filename="bookings_export.csv")
    await call.message.answer_document(file, caption="Экспорт бронирований (CSV)")
    await call.answer()

@router.callback_query(F.data == "io_export_staff")
async def io_export_staff(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    cursor.execute("SELECT user_id, role, fullname, phone, username, passport FROM users ORDER BY user_id DESC")
    rows = cursor.fetchall()
    content = _rows_to_csv_buffer(["user_id","role","fullname","phone","username","passport"], rows)
    file = BufferedInputFile(content, filename="staff_export.csv")
    await call.message.answer_document(file, caption="Экспорт сотрудников (CSV)")
    await call.answer()

def _detect_delimiter(sample: str) -> str:
    # очень простой детектор
    return ";" if sample.count(";") >= sample.count(",") else ","

def _parse_csv_bytes(data: bytes) -> List[Dict[str, str]]:
    # поддержка utf-8 и cp1251
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            text = data.decode(enc)
            break
        except:
            text = None
    if text is None:
        raise ValueError("Не удалось декодировать CSV")
    lines = text.splitlines()
    if not lines:
        return []
    delim = _detect_delimiter(lines[0])
    reader = csv.DictReader(lines, delimiter=delim)
    return [dict(row) for row in reader]

async def _handle_import_rows(import_type: str, rows: List[Dict[str, str]]) -> str:
    inserted = 0
    updated = 0
    if import_type == "menu":
        for row in rows:
            title = (row.get("title") or "").strip()
            if not title:
                continue
            description = (row.get("description") or "").strip()
            price = row.get("price"); 
            try:
                price = float(str(price).replace(",", ".")) if price not in (None, "") else 0.0
            except:
                price = 0.0
            category = (row.get("category") or "").strip()
            photo_url = (row.get("photo_url") or "").strip()
            is_active = 1 if str(row.get("is_active","1")).strip() not in ("0","no","false","нет") else 0
            cursor.execute("""INSERT INTO menu_items (title, description, price, category, photo_url, is_active)
                              VALUES (?,?,?,?,?,?)""",
                           (title, description, price, category, photo_url, is_active))
            inserted += 1
        conn.commit()
        return f"Импорт меню: добавлено {inserted} позиций."
    elif import_type == "bookings":
        for row in rows:
            user_id = row.get("user_id")
            try:
                user_id = int(user_id) if user_id not in (None, "") else None
            except:
                user_id = None
            fullname = (row.get("fullname") or "").strip()
            phone = (row.get("phone") or "").strip()
            dt = (row.get("datetime") or "").strip()
            source = (row.get("source") or "").strip()
            notes = (row.get("notes") or "").strip()
            status = (row.get("status") or "pending").strip()
            cursor.execute("""INSERT INTO bookings (user_id, fullname, phone, datetime, source, notes, status)
                              VALUES (?,?,?,?,?,?,?)""",
                           (user_id, fullname, phone, dt, source, notes, status))
            inserted += 1
        conn.commit()
        return f"Импорт бронирований: добавлено {inserted} записей."
    elif import_type == "staff":
        for row in rows:
            try:
                user_id = int(row.get("user_id"))
            except:
                continue
            role = row.get("role")
            try:
                role = int(role)
            except:
                if isinstance(role, str):
                    role = {"guest":0,"staff":1,"admin":2}.get(role.lower(), 1)
                else:
                    role = 1
            fullname = (row.get("fullname") or "").strip()
            phone = (row.get("phone") or "").strip()
            username = (row.get("username") or "").replace("@","").strip() or None
            passport = (row.get("passport") or "").strip()
            cursor.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
            if cursor.fetchone():
                cursor.execute("""UPDATE users SET role=?, fullname=?, phone=?, username=?, passport=? WHERE user_id=?""",
                               (role, fullname, phone, username, passport, user_id))
                updated += 1
            else:
                cursor.execute("""INSERT INTO users (user_id, role, fullname, phone, username, passport)
                                  VALUES (?,?,?,?,?,?)""",
                               (user_id, role, fullname, phone, username, passport))
                inserted += 1
        conn.commit()
        return f"Импорт сотрудников: добавлено {inserted}, обновлено {updated}."
    else:
        return "Неизвестный тип импорта."

async def _read_document_bytes(message: Message) -> Optional[bytes]:
    # Пытаемся скачать файл разными способами (для aiogram 3+)
    buffer = io.BytesIO()
    try:
        await bot.download(message.document, destination=buffer)  # основной путь в aiogram 3.x
        return buffer.getvalue()
    except Exception:
        try:
            file = await bot.get_file(message.document.file_id)
            await bot.download_file(file.file_path, destination=buffer)
            return buffer.getvalue()
        except Exception as e:
            logger.error("Download CSV failed: %s", e)
            return None

@router.callback_query(F.data.in_(["io_import_menu","io_import_bookings","io_import_staff"]))
async def io_import_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    import_type = call.data.split("_")[-1]
    await state.update_data(import_type=import_type)
    await state.set_state(ImportFSM.waiting_file)
    await call.message.answer("Загрузите CSV-файл (как документ) или пришлите содержимое CSV текстом.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm_io")]
    ]))
    await call.answer()

@router.message(ImportFSM.waiting_file, F.document)
async def io_import_receive_doc(message: Message, state: FSMContext):
    data = await _read_document_bytes(message)
    if not data:
        return await message.answer("Не удалось скачать файл. Пришлите CSV как текст или другой файл.")
    try:
        rows = _parse_csv_bytes(data)
        st = await state.get_data()
        import_type = st.get("import_type")
        result = await _handle_import_rows(import_type, rows)
        await state.clear()
        await message.answer(f"✅ {result}", reply_markup=io_menu_kb())
    except Exception as e:
        await message.answer(f"Ошибка импорта: {e}")

@router.message(ImportFSM.waiting_file, F.text)
async def io_import_receive_text(message: Message, state: FSMContext):
    try:
        rows = _parse_csv_bytes(message.text.encode("utf-8"))
        st = await state.get_data()
        import_type = st.get("import_type")
        result = await _handle_import_rows(import_type, rows)
        await state.clear()
        await message.answer(f"✅ {result}", reply_markup=io_menu_kb())
    except Exception as e:
        await message.answer(f"Ошибка импорта: {e}")

# ------------------------- Управление фото (админ) -------------------------
@router.callback_query(F.data == "adm_photos")
async def adm_photos_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить фото", callback_data="adm_photos_add")],
        [InlineKeyboardButton(text="📋 Список фото", callback_data="adm_photos_list")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_admin")]
    ])
    try:
        await call.message.edit_text("📸 Управление фотографиями", reply_markup=kb)
    except:
        await call.answer()

@router.callback_query(F.data == "adm_photos_add")
async def adm_photos_add_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.message.answer("Отправьте фото (как фото, не документ). Подпись можно добавить в подписи к фото.")
    await state.set_state(PhotoAddFSM.waiting_photo)
    await call.answer()

@router.message(PhotoAddFSM.waiting_photo, F.photo)
async def adm_photos_receive(message: Message, state: FSMContext):
    fid = message.photo[-1].file_id
    caption = message.caption or ""
    cursor.execute("INSERT INTO photos (file_id, caption, added_by) VALUES (?, ?, ?)", (fid, caption, message.from_user.id))
    conn.commit()
    await state.clear()
    await message.answer("✅ Фото добавлено.", reply_markup=admin_menu_inline())

@router.callback_query(F.data == "adm_photos_list")
async def adm_photos_list(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    cursor.execute("SELECT * FROM photos ORDER BY id DESC")
    rows = cursor.fetchall()
    if not rows:
        return await call.message.answer("Пока нет фото.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="adm_photos")]
        ]))
    for r in rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🖼 Изменить фото", callback_data=f"adm_photo_edit_{r['id']}"),
            InlineKeyboardButton(text="🗑 Удалить фото", callback_data=f"adm_photo_del_{r['id']}")
        ]])
        try:
            await call.message.answer_photo(r["file_id"], caption=(r["caption"] or ""), reply_markup=kb)
        except:
            try:
                await call.message.answer((r["caption"] or ""), reply_markup=kb)
            except:
                pass
    await call.answer()

@router.callback_query(F.data.startswith("adm_photo_del_"))
async def adm_photo_del(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    try:
        pid = int(call.data.split("_")[-1])
    except:
        return await call.answer("Ошибка", show_alert=True)
    cursor.execute("DELETE FROM photos WHERE id=?", (pid,))
    conn.commit()
    await call.message.answer("🗑 Фото удалено.")
    await adm_photos_list(call)

@router.callback_query(F.data.startswith("adm_photo_edit_"))
async def adm_photo_edit(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    try:
        pid = int(call.data.split("_")[-1])
    except:
        return await call.answer("Ошибка", show_alert=True)
    await state.update_data(photo_id=pid)
    await state.set_state(PhotoEditFSM.waiting_new_photo)
    await call.message.answer("Отправьте новое фото (как фото).")

@router.message(PhotoEditFSM.waiting_new_photo, F.photo)
async def adm_photo_edit_receive(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("photo_id")
    if pid is None:
        await state.clear()
        return await message.answer("Сессия редактирования потеряна.")
    fid = message.photo[-1].file_id
    cursor.execute("UPDATE photos SET file_id=? WHERE id=?", (fid, pid))
    conn.commit()
    await message.answer("✅ Фото заменено.")
    await state.clear()

# ------------------------- Staff-меню -------------------------
@router.callback_query(F.data == "staff_menu")
async def staff_menu_cb(call: CallbackQuery):
    role = get_role(call.from_user.id)
    if role not in (ROLE_STAFF, ROLE_ADMIN):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍽 Меню (управление)", callback_data="adm_menu_manage")],
        [InlineKeyboardButton(text="🍳 Кухня", callback_data="kitchen_main")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")]
    ])
    await safe_edit(call.message, "👨‍🍳 Staff-меню", reply_markup=kb)
    await call.answer()

# ---- Кухня: таблицы и FSM ----
cursor.execute("""
CREATE TABLE IF NOT EXISTS stop_list (
   id INTEGER PRIMARY KEY AUTOINCREMENT,
   title TEXT NOT NULL
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS togo_list (
   id INTEGER PRIMARY KEY AUTOINCREMENT,
   title TEXT NOT NULL
)
""")
conn.commit()

class KitchenAddFSM(StatesGroup):
    list_type = State()  # 'stop' or 'togo'
    waiting_title = State()

@router.callback_query(F.data == "kitchen_main")
async def kitchen_main_cb(call: CallbackQuery):
    role = get_role(call.from_user.id)
    if role not in (ROLE_STAFF, ROLE_ADMIN):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await safe_edit(call.message, "🍳 Раздел «Кухня» — выберите список:", reply_markup=kitchen_root_kb())
    await call.answer()

def _kitchen_list_markup(list_type: str):
    if list_type == "stop":
        cursor.execute("SELECT * FROM stop_list ORDER BY id DESC")
    else:
        cursor.execute("SELECT * FROM togo_list ORDER BY id DESC")
    rows = cursor.fetchall()
    buttons = []
    for r in rows:
        buttons.append([InlineKeyboardButton(text=f"❌ {r['title']}", callback_data=f"kitchen_del_{list_type}_{r['id']}")])
    buttons.insert(0, [InlineKeyboardButton(text="➕ Добавить", callback_data=f"kitchen_add_{list_type}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="kitchen_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@router.callback_query(F.data.startswith("kitchen_list_"))
async def kitchen_list_cb(call: CallbackQuery):
    role = get_role(call.from_user.id)
    if role not in (ROLE_STAFF, ROLE_ADMIN):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    list_type = call.data.split("_")[-1]
    await safe_edit(call.message, f"📃 Список: {'Stop-list' if list_type=='stop' else 'To-go list'}", reply_markup=_kitchen_list_markup(list_type))
    await call.answer()

@router.callback_query(F.data.startswith("kitchen_add_"))
async def kitchen_add_cb(call: CallbackQuery, state: FSMContext):
    role = get_role(call.from_user.id)
    if role not in (ROLE_STAFF, ROLE_ADMIN):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    list_type = call.data.split("_")[-1]
    await state.update_data(list_type=list_type)
    await state.set_state(KitchenAddFSM.waiting_title)
    await call.message.answer("Введите название позиции для добавления:")

@router.message(KitchenAddFSM.waiting_title)
async def kitchen_add_save(message: Message, state: FSMContext):
    data = await state.get_data()
    list_type = data.get("list_type")
    title = message.text.strip()
    if not title:
        return await message.answer("Название не может быть пустым. Попробуйте ещё раз.")
    if list_type == "stop":
        cursor.execute("INSERT INTO stop_list (title) VALUES(?)", (title,))
    else:
        cursor.execute("INSERT INTO togo_list (title) VALUES(?)", (title,))
    conn.commit()
    await message.answer("✅ Добавлено.", reply_markup=_kitchen_list_markup(list_type))
    await state.clear()

@router.callback_query(F.data.startswith("kitchen_del_"))
async def kitchen_del_cb(call: CallbackQuery):
    role = get_role(call.from_user.id)
    if role not in (ROLE_STAFF, ROLE_ADMIN):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    _, _, list_type, id_str = call.data.split("_", 3)
    try:
        iid = int(id_str)
    except:
        return await call.answer("Ошибка", show_alert=True)
    if list_type == "stop":
        cursor.execute("DELETE FROM stop_list WHERE id=?", (iid,))
    else:
        cursor.execute("DELETE FROM togo_list WHERE id=?", (iid,))
    conn.commit()
    await safe_edit(call.message, "🗑 Удалено.", reply_markup=_kitchen_list_markup(list_type))
    await call.answer()

# ------------------------- Админ меню (корневой обработчик) -------------------------
@router.callback_query(F.data == "main_admin")
async def main_admin_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        if is_staff(call.from_user.id):
            await call.message.answer(
                "🌟 У вас пока нет доступа к разделу администратора.\n"
                "Если считаете, что это ошибка — свяжитесь с управляющим.",
                reply_markup=main_menu_inline(call.from_user.id)
            )
            try:
                await call.answer()
            except:
                pass
            return
        return await call.answer("⛔ Нет доступа", show_alert=True)
    try:
        await call.message.edit_text("⚙️ Админ меню", reply_markup=admin_menu_inline())
    except Exception:
        await call.message.answer("⚙️ Админ меню", reply_markup=admin_menu_inline())
    try:
        await call.answer()
    except:
        pass

# ------------------------- Самопроверки -------------------------
def run_self_checks():
    try:
        # menu_items insert/delete
        cursor.execute("INSERT INTO menu_items (title, price, is_active) VALUES ('TEST', 1.0, 1)")
        conn.commit()
        cursor.execute("SELECT id FROM menu_items WHERE title='TEST' ORDER BY id DESC LIMIT 1")
        r = cursor.fetchone()
        if r:
            cursor.execute("DELETE FROM menu_items WHERE id=?", (r["id"],))
            conn.commit()
        # photos insert/delete
        cursor.execute("INSERT INTO photos (file_id, caption) VALUES ('TEST_FILE_ID', 'test')")
        conn.commit()
        cursor.execute("DELETE FROM photos WHERE file_id='TEST_FILE_ID'")
        conn.commit()
        # encrypt/decrypt test (optional)
        if HAS_FERNET:
            token = FERNET.encrypt(b"hello")
            assert FERNET.decrypt(token) == b"hello"
        logger.info("Self-checks OK")
    except Exception as e:
        logger.error("Self-checks error: %s", e)

# ------------------------- Старт / Планировщик -------------------------
async def on_startup():
    scheduler.add_job(remind_booking_job, "interval", minutes=1, id="remind_every_min")
    scheduler.add_job(morning_digest_job, "cron", hour=7, minute=50, id="morning_digest")
    scheduler.start()

async def main():
    logger.info("🤖 Бот запускается... %s", VERSION)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    run_self_checks()

    dp.include_router(router)

    await on_startup()
    try:
        await dp.start_polling(bot)
    finally:
        try:
            scheduler.shutdown(wait=False)
        except:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Shutting down...")
