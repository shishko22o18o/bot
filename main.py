import asyncio
import json
import logging
import uuid
import os
import csv
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager
from io import StringIO
import io

# Импорты aiogram
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, FSInputFile
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# Импорты FastAPI
from fastapi import FastAPI, Request, HTTPException, Depends, status, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
import uvicorn

# MongoDB
import motor.motor_asyncio
import certifi

# JWT
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel

# Дополнительные библиотеки
import aiofiles
from PIL import Image  # для конвертации изображений

# Для графика (опционально)
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False
    logging.warning("matplotlib не установлен, функция /stats_chart будет недоступна")

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не задан в переменных окружения!")

ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_STR.split(",") if x.strip().isdigit()]
if not ADMIN_IDS:
    logging.warning("⚠️ ADMIN_IDS не задан! Админ-функции будут недоступны.")

MONGO_URL = os.getenv("MONGO_URL")
if not MONGO_URL:
    raise ValueError("❌ MONGO_URL не задан в переменных окружения!")

BASE_URL = os.getenv("WEBHOOK_URL")
if not BASE_URL:
    raise ValueError("❌ WEBHOOK_URL не задан! Нужен для формирования ссылок на картинки и админку.")

# Настройки JWT
SECRET_KEY = os.getenv("JWT_SECRET")
if not SECRET_KEY:
    raise ValueError("❌ JWT_SECRET не задан!")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 1 день
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    raise ValueError("❌ ADMIN_PASSWORD не задан!")

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Логгер для действий администраторов
admin_logger = logging.getLogger('admin_actions')
admin_handler = logging.FileHandler('admin_actions.log', encoding='utf-8')
admin_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
admin_logger.addHandler(admin_handler)
admin_logger.setLevel(logging.INFO)

# ==================== ПОДКЛЮЧЕНИЕ К MONGODB ====================
client = motor.motor_asyncio.AsyncIOMotorClient(
    MONGO_URL,
    tlsCAFile=certifi.where(),
    serverSelectionTimeoutMS=10000,
    connectTimeoutMS=10000,
    socketTimeoutMS=30000,
    retryWrites=True
)
db = client["bau28shop"]
products_col = db["products"]
orders_col = db["orders"]
promocodes_col = db["promocodes"]
blocked_users_col = db["blocked_users"]
wheel_prizes_col = db["wheel_prizes"]
admin_logs_col = db["admin_logs"]
settings_col = db["settings"]

async def init_mongodb():
    try:
        await client.admin.command('ping')
        logger.info("✅ MongoDB ping successful")
    except Exception as e:
        logger.error(f"❌ MongoDB ping failed: {e}")
        raise

    await products_col.create_index("id", unique=True)
    await products_col.create_index("category")
    await products_col.create_index("subcategory")
    await orders_col.create_index("id", unique=True)
    await orders_col.create_index("status")
    await orders_col.create_index("created_at")
    await promocodes_col.create_index("code", unique=True)
    await promocodes_col.create_index("expires_at")
    await blocked_users_col.create_index("user_id", unique=True)
    await wheel_prizes_col.create_index("id", unique=True)

    # Индексы для новых коллекций с обработкой ошибок
    try:
        await admin_logs_col.create_index([("timestamp", -1)])
    except Exception as e:
        logger.error(f"Не удалось создать индекс для admin_logs_col: {e}")

    try:
        await settings_col.create_index("key", unique=True)
    except Exception as e:
        logger.error(f"Не удалось создать индекс для settings_col: {e}")

    logger.info("MongoDB инициализирована.")
# ==================== JWT АУТЕНТИФИКАЦИЯ ====================
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/admin/login")

class Token(BaseModel):
    access_token: str
    token_type: str

class LoginRequest(BaseModel):
    password: str

def create_access_token(data: dict, expires_delta: timedelta = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_admin(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None or username != "admin":
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    return username

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def format_price(price: int) -> str:
    return f"{price:,} ₽".replace(",", " ")

async def get_product_by_id(product_id: str) -> Optional[Dict[str, Any]]:
    return await products_col.find_one({"id": product_id})

def log_admin_action(admin_id: int, action: str):
    admin_logger.info(f"Admin {admin_id}: {action}")

def get_main_keyboard(is_admin: bool = False):
    """Клавиатура главного меню бота."""
    # URL магазина на GitHub Pages (исправлено)
    store_url = "https://shishko22o18o.github.io/bau28store/"
    if is_admin:
        kb = [
            [KeyboardButton(text="📦 Товары")],
            [KeyboardButton(text="📋 Заказы"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="➕ Добавить товар"), KeyboardButton(text="📤 Экспорт CSV")],
            [KeyboardButton(text="ℹ️ Команды")],
            [KeyboardButton(text="🛍 Открыть магазин", web_app=types.WebAppInfo(url=store_url))],
            [KeyboardButton(text="📊 Админ панель", web_app=types.WebAppInfo(url=f"{BASE_URL}/admin"))]
        ]
    else:
        kb = [
            [KeyboardButton(text="🛍 Открыть магазин", web_app=types.WebAppInfo(url=store_url))]
        ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_cancel_keyboard():
    kb = [[KeyboardButton(text="❌ Отмена")]]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_photo_done_keyboard():
    kb = [[KeyboardButton(text="✅ Готово")]]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def generate_help_text() -> str:
    return """
📋 Доступные команды администратора

📦 Товары
➕ Добавить товар (кнопка) – пошаговое добавление
/bulk_add – массовое добавление через CSV
📦 Товары (кнопка) – просмотр/редактирование товаров
/search [текст] – поиск товаров по названию

📋 Заказы
📋 Заказы (кнопка) – новые заказы с управлением статусом
/orders_all [status=...] [date=ГГГГ-ММ-ДД] – все заказы с фильтрацией
/find_order [id] – поиск заказа по номеру или ID пользователя

🎟️ Промокоды
/add_promo – создать промокод
/list_promo – список промокодов
/delete_promo [код] – удалить промокод

🎁 Колесо фортуны
/wheel_prizes – управление призами
/del_prize <id> – удалить приз

📊 Статистика
📊 Статистика (кнопка) – общая статистика
/stats_detailed – статистика по дням (7 дней)
/stats_chart – график продаж (30 дней)
/popular – топ-10 товаров по продажам

🚫 Управление пользователями
/block_user [id] – заблокировать пользователя
/unblock_user [id] – разблокировать
/list_blocked – список заблокированных

📤 Экспорт / Импорт
📤 Экспорт CSV (кнопка) – выгрузка товаров в CSV
/backup – полная резервная копия (JSON)
/restore – восстановление из JSON (с подтверждением)

❌ Отмена – отмена текущего действия в любом FSM
"""

# ==================== ФУНКЦИЯ КОНВЕРТАЦИИ ИЗОБРАЖЕНИЙ ====================
async def convert_to_jpg(input_path: str, output_path: str, quality: int = 90):
    """
    Конвертирует изображение в JPG и сохраняет по output_path.
    Запускается в отдельном потоке, чтобы не блокировать asyncio.
    """
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _convert_image, input_path, output_path, quality)

def _convert_image(input_path, output_path, quality):
    with Image.open(input_path) as img:
        # Если есть альфа-канал, конвертируем в RGB (накладываем на белый фон)
        if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
            # Создаём белый фон
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            # Если есть прозрачность, используем её как маску
            if img.mode == 'RGBA':
                background.paste(img, mask=img.split()[-1])
            else:
                background.paste(img)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        img.save(output_path, 'JPEG', quality=quality)

# ==================== ИНИЦИАЛИЗАЦИЯ БОТА ====================
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=storage)

# ==================== FSM ====================
class AddProduct(StatesGroup):
    name = State()
    description = State()
    price = State()
    category = State()
    subcategory = State()
    discount = State()
    is_new = State()
    photos = State()          # ожидание нескольких фото

class EditProduct(StatesGroup):
    choose_field = State()
    new_value = State()

class AddPromo(StatesGroup):
    code = State()
    promo_type = State()          # 'discount' или 'wheel'
    discount_type = State()        # 'percent' или 'fixed' (если promo_type == 'discount')
    value = State()                # для discount – значение скидки
    expires = State()
    max_uses = State()

class WheelPrize(StatesGroup):
    description = State()
    icon = State()
    type = State()                 # percent, fixed, bonus, shipping
    value = State()
    probability = State()

# ==================== ХЭНДЛЕРЫ БОТА ====================

# ---------- Команда /start ----------
@dp.message(CommandStart())
async def cmd_start(message: Message):
    admin = is_admin(message.from_user.id)
    welcome = (
        f"Привет, <b>{message.from_user.first_name}</b>! 👋\n\n"
        f"Добро пожаловать в <b>Bau28Store</b>.\n"
        f"{'Вы вошли как администратор.' if admin else 'Нажми кнопку ниже, чтобы открыть каталог.'}"
    )
    await message.answer(welcome, reply_markup=get_main_keyboard(admin))

# ---------- Отмена ----------
@dp.message(F.text == "❌ Отмена", StateFilter("*"))
async def cancel_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=get_main_keyboard(is_admin(message.from_user.id)))

# ---------- Справка ----------
@dp.message(F.text == "ℹ️ Команды")
async def cmd_help(message: Message):
    if not is_admin(message.from_user.id):
        return
    help_text = generate_help_text()
    await message.answer(help_text, parse_mode=None)

# ---------- Обработка заказов из Web App ----------
@dp.message(F.web_app_data)
async def handle_web_app_data(message: Message):
    blocked = await blocked_users_col.find_one({"user_id": str(message.from_user.id)})
    if blocked:
        await message.answer("⛔ Вы заблокированы и не можете оформлять заказы.")
        return

    try:
        data = json.loads(message.web_app_data.data)
        items = data.get('items', [])
        total = data.get('total', 0)
        promo_code = data.get('promo')

        if not items:
            await message.answer("❌ Корзина пуста. Заказ не оформлен.")
            return

        discount = 0
        if promo_code:
            promo = await promocodes_col.find_one({"code": promo_code})
            if promo and promo.get('expires_at', datetime.now()) > datetime.now() and promo.get('used_count', 0) < promo.get('max_uses', 999999):
                if promo.get('type') == 'discount':
                    if promo['discount_type'] == 'percent':
                        discount = int(total * promo['value'] / 100)
                    else:
                        discount = promo['value']
                    total -= discount
                    await promocodes_col.update_one({"code": promo_code}, {"$inc": {"used_count": 1}})

        order_id = str(uuid.uuid4().hex[:8])
        order_doc = {
            "id": order_id,
            "user_id": str(message.from_user.id),
            "user_name": message.from_user.full_name,
            "items": items,
            "total": total,
            "status": "new",
            "created_at": datetime.now(),
            "promo_used": promo_code if promo_code else None,
            "discount_applied": discount
        }
        await orders_col.insert_one(order_doc)

        receipt = "🧾 <b>Детали заказа:</b>\n\n"
        for item in items:
            name = item.get('name', 'Товар')
            qty = item.get('quantity', 1)
            price = item.get('price', 0)
            sum_price = qty * price
            receipt += f"▪️ {name} — {qty} шт. x {price} ₽ = <b>{sum_price} ₽</b>\n"
        if discount > 0:
            receipt += f"\n🎟️ Скидка по промокоду: -{discount} ₽\n"
        receipt += f"\n💰 <b>ИТОГО: {total} ₽</b>"

        await message.answer(f"✅ <b>Заказ #{order_id} успешно оформлен!</b>\n\n{receipt}\n\n<i>Скоро с вами свяжутся.</i>")

        user_link = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
        admin_msg = (
            f"🚨 <b>НОВЫЙ ЗАКАЗ #{order_id}</b> 🚨\n\n"
            f"👤 <b>Покупатель:</b> {user_link} (ID: <code>{message.from_user.id}</code>)\n"
            f"{receipt}"
        )
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, admin_msg)
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")

    except Exception as e:
        logger.error(f"Ошибка при обработке заказа: {e}")
        await message.answer("❌ Произошла ошибка. Попробуйте позже.")

# ---------- Добавление товара (FSM) ----------
@dp.message(F.text == "➕ Добавить товар")
async def cmd_add(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AddProduct.name)
    await message.answer("Введите название товара:", reply_markup=get_cancel_keyboard())

@dp.message(AddProduct.name)
async def add_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(AddProduct.description)
    await message.answer("Введите описание товара (можно отправить пустое):")

@dp.message(AddProduct.description)
async def add_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text or "")
    await state.set_state(AddProduct.price)
    await message.answer("Введите цену (только число):")

@dp.message(AddProduct.price)
async def add_price(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Цена должна быть числом. Попробуйте ещё раз:")
        return
    await state.update_data(price=int(message.text))
    await state.set_state(AddProduct.category)
    await message.answer("Введите категорию (clothes, accessories, vape, electronics):")

@dp.message(AddProduct.category)
async def add_category(message: Message, state: FSMContext):
    cat = message.text.lower()
    if cat not in ['clothes', 'accessories', 'vape', 'electronics']:
        await message.answer("❌ Неверная категория. Допустимы: clothes, accessories, vape, electronics")
        return
    await state.update_data(category=cat)
    if cat == 'vape':
        await state.set_state(AddProduct.subcategory)
        await message.answer("Введите подкатегорию (liquids, consumables, disposable, pods):")
    else:
        await state.update_data(subcategory="")
        await state.set_state(AddProduct.discount)
        await message.answer("Введите скидку в процентах (0 если нет):")

@dp.message(AddProduct.subcategory)
async def add_subcategory(message: Message, state: FSMContext):
    sub = message.text.lower()
    if sub not in ['liquids', 'consumables', 'disposable', 'pods']:
        await message.answer("❌ Неверная подкатегория. Допустимы: liquids, consumables, disposable, pods")
        return
    await state.update_data(subcategory=sub)
    await state.set_state(AddProduct.discount)
    await message.answer("Введите скидку в процентах (0 если нет):")

@dp.message(AddProduct.discount)
async def add_discount(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Скидка должна быть числом. Попробуйте ещё раз:")
        return
    await state.update_data(discount=int(message.text))
    await state.set_state(AddProduct.is_new)
    await message.answer("Это новинка? (да/нет):")

@dp.message(AddProduct.is_new)
async def add_is_new(message: Message, state: FSMContext):
    text = message.text.lower()
    if text not in ['да', 'нет', 'yes', 'no']:
        await message.answer("❌ Ответьте 'да' или 'нет'")
        return
    is_new = 1 if text in ['да', 'yes'] else 0
    await state.update_data(is_new=is_new)
    await state.update_data(photos=[])
    await state.set_state(AddProduct.photos)
    await message.answer(
        "Теперь отправляйте фотографии товара по одной.\n"
        "Можно загружать файлы любых форматов (PNG, HEIC, WEBP) — они будут конвертированы в JPG.\n"
        "Когда закончите, нажмите кнопку '✅ Готово'.",
        reply_markup=get_photo_done_keyboard()
    )

# Обработка фото (сжатое от Telegram)
@dp.message(AddProduct.photos, F.photo)
async def add_photo(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    photos = data.get('photos', [])

    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    temp_path = f"/tmp/temp_{uuid.uuid4().hex}.jpg"
    await bot.download_file(file.file_path, temp_path)

    # Конвертируем (по сути уже JPEG, но для единообразия используем функцию)
    out_filename = f"{uuid.uuid4().hex}.jpg"
    out_path = f"static/uploaded/{out_filename}"
    os.makedirs("static/uploaded", exist_ok=True)
    await convert_to_jpg(temp_path, out_path)

    os.remove(temp_path)

    photos.append(f"/static/uploaded/{out_filename}")
    await state.update_data(photos=photos)

    await message.answer(
        f"✅ Фото добавлено! Всего фото: {len(photos)}.\n"
        "Отправьте ещё или нажмите '✅ Готово'.",
        reply_markup=get_photo_done_keyboard()
    )

# Обработка документов-изображений
@dp.message(AddProduct.photos, F.document)
async def add_photo_document(message: Message, state: FSMContext, bot: Bot):
    # Проверим MIME-тип
    if not message.document.mime_type.startswith('image/'):
        await message.answer("❌ Пожалуйста, отправьте изображение.")
        return

    data = await state.get_data()
    photos = data.get('photos', [])

    file = await bot.get_file(message.document.file_id)
    original_ext = os.path.splitext(message.document.file_name)[1].lower()
    temp_filename = f"temp_{uuid.uuid4().hex}{original_ext}"
    temp_path = f"/tmp/{temp_filename}"
    await bot.download_file(file.file_path, temp_path)

    out_filename = f"{uuid.uuid4().hex}.jpg"
    out_path = f"static/uploaded/{out_filename}"
    os.makedirs("static/uploaded", exist_ok=True)
    await convert_to_jpg(temp_path, out_path)

    os.remove(temp_path)

    photos.append(f"/static/uploaded/{out_filename}")
    await state.update_data(photos=photos)

    await message.answer(
        f"✅ Фото добавлено! Всего фото: {len(photos)}.\n"
        "Отправьте ещё или нажмите '✅ Готово'.",
        reply_markup=get_photo_done_keyboard()
    )

@dp.message(AddProduct.photos, F.text.in_(['✅ Готово', '/done']))
async def add_photos_done(message: Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get('photos', [])
    if not photos:
        photos = []

    product_id = f"p{uuid.uuid4().hex[:8]}"
    product_doc = {
        "id": product_id,
        "name": data['name'],
        "description": data['description'],
        "price": data['price'],
        "category": data['category'],
        "subcategory": data.get('subcategory', ""),
        "discount": data['discount'],
        "is_new": data['is_new'],
        "images": photos,
        "created_at": datetime.now()
    }
    await products_col.insert_one(product_doc)

    await state.clear()
    log_admin_action(message.from_user.id, f"Добавил товар ID {product_id} ({data['name']})")
    await message.answer(f"✅ Товар добавлен! ID: {product_id}", reply_markup=get_main_keyboard(True))

@dp.message(AddProduct.photos)
async def add_photos_invalid(message: Message):
    await message.answer(
        "❌ Пожалуйста, отправьте фотографию или нажмите '✅ Готово'.",
        reply_markup=get_photo_done_keyboard()
    )

# ---------- Массовое добавление через CSV ----------
@dp.message(Command("bulk_add"))
async def cmd_bulk_add(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("Отправьте CSV-файл с товарами.\n"
                         "Формат: название,описание,цена,категория,подкатегория(если vape),скидка,новинка(0/1)\n"
                         "Пример: Футболка,Хлопок 100%,2990,clothes,,0,1")

@dp.message(F.document)
async def handle_csv(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    if not message.document.file_name.endswith('.csv'):
        await message.answer("❌ Пожалуйста, отправьте файл с расширением .csv")
        return

    file = await bot.get_file(message.document.file_id)
    file_path = f"/tmp/{message.document.file_id}.csv"
    await bot.download_file(file.file_path, file_path)

    added = 0
    errors = []
    with open(file_path, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        # Пробуем определить, есть ли заголовок
        first_row = next(reader, None)
        if first_row and first_row[0].strip().lower() == 'id':
            # Экспортный формат
            for row in reader:
                try:
                    if len(row) < 8:
                        errors.append(f"Недостаточно полей: {row}")
                        continue
                    name = row[1]
                    desc = row[2]
                    price = int(row[3])
                    cat = row[4]
                    subcat = row[5] if len(row) > 5 else ""
                    discount = int(row[6]) if row[6] else 0
                    is_new = int(row[7]) if row[7] else 0
                    product_id = f"p{uuid.uuid4().hex[:8]}"
                    product_doc = {
                        "id": product_id,
                        "name": name,
                        "description": desc,
                        "price": price,
                        "category": cat,
                        "subcategory": subcat,
                        "discount": discount,
                        "is_new": is_new,
                        "images": [],
                        "created_at": datetime.now()
                    }
                    await products_col.insert_one(product_doc)
                    added += 1
                except Exception as e:
                    errors.append(f"Ошибка в строке {row}: {e}")
        else:
            # Старый формат (без заголовка)
            rows = [first_row] + list(reader) if first_row else list(reader)
            for row in rows:
                try:
                    if len(row) < 7:
                        errors.append(f"Недостаточно полей: {row}")
                        continue
                    name, desc, price_str, cat, subcat, discount_str, is_new_str = row[:7]
                    price = int(price_str)
                    discount = int(discount_str)
                    is_new = int(is_new_str)
                    subcat = subcat if subcat else ""

                    product_id = f"p{uuid.uuid4().hex[:8]}"
                    product_doc = {
                        "id": product_id,
                        "name": name,
                        "description": desc,
                        "price": price,
                        "category": cat,
                        "subcategory": subcat,
                        "discount": discount,
                        "is_new": is_new,
                        "images": [],
                        "created_at": datetime.now()
                    }
                    await products_col.insert_one(product_doc)
                    added += 1
                except Exception as e:
                    errors.append(f"Ошибка в строке {row}: {e}")

    os.remove(file_path)
    result = f"✅ Добавлено товаров: {added}\n"
    if errors:
        result += f"❌ Ошибки ({len(errors)}):\n" + "\n".join(errors[:5])
    log_admin_action(message.from_user.id, f"Массовое добавление: +{added} товаров")
    await message.answer(result)

# ---------- Экспорт товаров в CSV ----------
@dp.message(F.text == "📤 Экспорт CSV")
@dp.message(Command("export_products"))
async def cmd_export_products(message: Message):
    if not is_admin(message.from_user.id):
        return
    cursor = products_col.find({})
    products = await cursor.to_list(length=10000)

    if not products:
        await message.answer("В базе нет товаров.")
        return

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "name", "description", "price", "category", "subcategory", "discount", "is_new", "images"])
    for p in products:
        images_str = ','.join(p.get('images', []))
        writer.writerow([p['id'], p['name'], p['description'], p['price'], p['category'], p['subcategory'], p['discount'], p['is_new'], images_str])
    csv_data = output.getvalue().encode('utf-8')
    output.close()

    temp_file = f"/tmp/export_{message.from_user.id}.csv"
    with open(temp_file, "wb") as f:
        f.write(csv_data)
    await message.answer_document(FSInputFile(temp_file), caption="📁 Экспорт товаров")
    os.remove(temp_file)

# ---------- Детальная статистика ----------
@dp.message(Command("stats_detailed"))
async def cmd_stats_detailed(message: Message):
    if not is_admin(message.from_user.id):
        return
    seven_days_ago = datetime.now() - timedelta(days=7)
    pipeline = [
        {"$match": {"created_at": {"$gt": seven_days_ago}}},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}},
            "count": {"$sum": 1},
            "total": {"$sum": "$total"}
        }},
        {"$sort": {"_id": -1}}
    ]
    cursor = orders_col.aggregate(pipeline)
    rows = await cursor.to_list(length=10)

    text = "📊 <b>Статистика по дням (последние 7 дней):</b>\n\n"
    if rows:
        for r in rows:
            text += f"📅 {r['_id']}: заказов {r['count']}, сумма {r['total'] or 0} ₽\n"
    else:
        text += "За последние 7 дней заказов нет."
    await message.answer(text)

# ---------- График статистики ----------
@dp.message(Command("stats_chart"))
async def cmd_stats_chart(message: Message):
    if not is_admin(message.from_user.id):
        return
    if not MATPLOTLIB_AVAILABLE:
        await message.answer("❌ Библиотека matplotlib не установлена.")
        return

    thirty_days_ago = datetime.now() - timedelta(days=30)
    pipeline = [
        {"$match": {"created_at": {"$gt": thirty_days_ago}}},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}},
            "total": {"$sum": "$total"}
        }},
        {"$sort": {"_id": 1}}
    ]
    cursor = orders_col.aggregate(pipeline)
    data = await cursor.to_list(length=31)

    if not data:
        await message.answer("Нет данных за последние 30 дней.")
        return

    dates = [d['_id'] for d in data]
    totals = [d['total'] for d in data]

    plt.figure(figsize=(10, 5))
    plt.plot(dates, totals, marker='o', linestyle='-', color='b')
    plt.xlabel('Дата')
    plt.ylabel('Сумма продаж (₽)')
    plt.title('Продажи за последние 30 дней')
    plt.xticks(rotation=45)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()

    await message.answer_photo(types.BufferedInputFile(buf.read(), filename="chart.png"), caption="📈 График продаж за 30 дней")

# ---------- Поиск товаров ----------
@dp.message(Command("search"))
async def cmd_search(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Укажите текст для поиска. Например: /search футболка")
        return
    query = args[1]
    cursor = products_col.find({"name": {"$regex": query, "$options": "i"}})
    results = await cursor.to_list(length=50)

    if not results:
        await message.answer(f"По запросу «{query}» ничего не найдено.")
        return
    text = f"🔍 Найденные товары по запросу «{query}»:\n\n"
    for p in results:
        text += f"ID: {p['id']} | {p['name']}\n"
    await message.answer(text)

# ---------- Просмотр товаров (админ) ----------
@dp.message(F.text == "📦 Товары")
async def show_products_menu(message: Message):
    if not is_admin(message.from_user.id):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👕 Одежда", callback_data="list_clothes_0")],
        [InlineKeyboardButton(text="🕶 Аксессуары", callback_data="list_accessories_0")],
        [InlineKeyboardButton(text="💨 VAPE", callback_data="list_vape_0")],
        [InlineKeyboardButton(text="🎧 Электроника", callback_data="list_electronics_0")],
    ])
    await message.answer("Выберите категорию:", reply_markup=kb)

async def show_product_list(cat: str, page: int, callback: CallbackQuery):
    skip = page * 5
    cursor = products_col.find({"category": cat}).sort("created_at", -1).skip(skip).limit(5)
    products = await cursor.to_list(length=5)
    total = await products_col.count_documents({"category": cat})

    if not products:
        await callback.message.edit_text("В этой категории пока нет товаров.")
        return

    text = f"Товары в категории {cat} (стр. {page+1}):\n\n"
    for p in products:
        final_price = p['price'] if not p['discount'] else p['price'] * (100 - p['discount']) // 100
        desc = p['description'][:50] + "..." if p['description'] and len(p['description']) > 50 else (p['description'] or "без описания")
        text += f"ID: {p['id']} | {p['name']} | {final_price}₽\n   Описание: {desc}\n\n"

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"list_{cat}_{page-1}"))
    if (page+1)*5 < total:
        nav_buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"list_{cat}_{page+1}"))

    inline_kb = []
    if nav_buttons:
        inline_kb.append(nav_buttons)

    for p in products:
        inline_kb.append([
            InlineKeyboardButton(text=f"✏️ {p['name'][:15]}...", callback_data=f"edit_{p['id']}_menu"),
            InlineKeyboardButton(text="🗑", callback_data=f"del_{p['id']}")
        ])

    inline_kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_categories")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=inline_kb))

@dp.callback_query(lambda c: c.data.startswith("list_"))
async def handle_list(callback: CallbackQuery):
    parts = callback.data.split('_')
    cat = parts[1]
    page = int(parts[2]) if len(parts) > 2 else 0
    await show_product_list(cat, page, callback)

@dp.callback_query(lambda c: c.data == "back_to_categories")
async def back_to_categories(callback: CallbackQuery):
    await show_products_menu(callback.message)

# ---------- Удаление товара ----------
@dp.callback_query(lambda c: c.data.startswith("del_"))
async def delete_product_confirm(callback: CallbackQuery):
    product_id = callback.data.split("_")[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да", callback_data=f"confirm_del_{product_id}"),
            InlineKeyboardButton(text="❌ Нет", callback_data="cancel_del")
        ]
    ])
    await callback.message.edit_text(f"Удалить товар ID {product_id}?", reply_markup=kb)

@dp.callback_query(lambda c: c.data.startswith("confirm_del_"))
async def confirm_delete(callback: CallbackQuery):
    product_id = callback.data.split("_")[2]
    product = await get_product_by_id(product_id)
    name = product['name'] if product else "Unknown"
    if product and 'images' in product:
        for img_path in product['images']:
            if img_path.startswith('/static/uploaded/'):
                local_path = img_path.replace('/static/uploaded/', 'static/uploaded/')
                if os.path.exists(local_path):
                    os.remove(local_path)
    await products_col.delete_one({"id": product_id})
    log_admin_action(callback.from_user.id, f"Удалил товар ID {product_id} ({name})")
    await callback.message.edit_text(f"✅ Товар ID {product_id} удалён.")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "cancel_del")
async def cancel_delete(callback: CallbackQuery):
    await callback.message.delete()
    await cmd_start(callback.message)

# ---------- Редактирование товара ----------
@dp.callback_query(lambda c: c.data.startswith("edit_") and c.data.endswith("_menu"))
async def edit_product_menu(callback: CallbackQuery):
    product_id = callback.data.split("_")[1]
    product = await get_product_by_id(product_id)
    if not product:
        await callback.message.edit_text("Товар не найден.")
        return

    text = f"Редактирование товара ID {product_id}:\n"
    text += f"Название: {product['name']}\n"
    text += f"Описание: {product['description'][:100]}...\n"
    text += f"Цена: {product['price']}₽\n"
    text += f"Категория: {product['category']}\n"
    if product['subcategory']:
        text += f"Подкатегория: {product['subcategory']}\n"
    text += f"Скидка: {product['discount']}%\n"
    text += f"Новинка: {'да' if product['is_new'] else 'нет'}\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Название", callback_data=f"edit_{product_id}_field_name")],
        [InlineKeyboardButton(text="📝 Описание", callback_data=f"edit_{product_id}_field_description")],
        [InlineKeyboardButton(text="💰 Цена", callback_data=f"edit_{product_id}_field_price")],
        [InlineKeyboardButton(text="📁 Категория", callback_data=f"edit_{product_id}_field_category")],
        [InlineKeyboardButton(text="🏷 Скидка", callback_data=f"edit_{product_id}_field_discount")],
        [InlineKeyboardButton(text="🆕 Новинка", callback_data=f"edit_{product_id}_field_isnew")],
        [InlineKeyboardButton(text="🖼 Фото", callback_data=f"edit_{product_id}_field_photo")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"list_{product['category']}_0")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)

@dp.callback_query(lambda c: c.data.startswith("edit_") and "field" in c.data)
async def edit_product_field(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split('_')
    product_id = parts[1]
    field = parts[3]
    await state.update_data(edit_id=product_id, edit_field=field)
    if field == "name":
        await state.set_state(EditProduct.new_value)
        await callback.message.edit_text("Введите новое название:")
    elif field == "description":
        await state.set_state(EditProduct.new_value)
        await callback.message.edit_text("Введите новое описание:")
    elif field == "price":
        await state.set_state(EditProduct.new_value)
        await callback.message.edit_text("Введите новую цену (число):")
    elif field == "category":
        await state.set_state(EditProduct.new_value)
        await callback.message.edit_text("Введите новую категорию (clothes, accessories, vape, electronics):")
    elif field == "discount":
        await state.set_state(EditProduct.new_value)
        await callback.message.edit_text("Введите новую скидку (число):")
    elif field == "isnew":
        product = await get_product_by_id(product_id)
        if product:
            new_val = 0 if product['is_new'] else 1
            await products_col.update_one({"id": product_id}, {"$set": {"is_new": new_val}})
            log_admin_action(callback.from_user.id, f"Изменил новинку товара ID {product_id} на {new_val}")
        await callback.message.edit_text("✅ Поле обновлено.")
        await callback.answer()
    elif field == "photo":
        await state.set_state(EditProduct.new_value)
        await callback.message.edit_text("Отправьте новое фото (старое будет удалено):")

@dp.message(EditProduct.new_value, F.text)
async def edit_text_field(message: Message, state: FSMContext):
    data = await state.get_data()
    product_id = data['edit_id']
    field = data['edit_field']
    new_value = message.text

    if field in ["price", "discount"] and not new_value.isdigit():
        await message.answer(f"❌ {field} должно быть числом. Попробуйте ещё раз:")
        return
    if field == "category" and new_value not in ['clothes', 'accessories', 'vape', 'electronics']:
        await message.answer("❌ Неверная категория. Допустимы: clothes, accessories, vape, electronics")
        return

    update_data = {}
    if field == "name":
        update_data["name"] = new_value
    elif field == "description":
        update_data["description"] = new_value
    elif field == "price":
        update_data["price"] = int(new_value)
    elif field == "category":
        update_data["category"] = new_value
        update_data["subcategory"] = ""
    elif field == "discount":
        update_data["discount"] = int(new_value)

    if update_data:
        await products_col.update_one({"id": product_id}, {"$set": update_data})
        log_admin_action(message.from_user.id, f"Изменил поле {field} товара ID {product_id} на {new_value}")

    await state.clear()
    await message.answer("✅ Поле обновлено.", reply_markup=get_main_keyboard(True))

@dp.message(EditProduct.new_value, F.photo)
async def edit_photo(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    product_id = data['edit_id']
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    temp_path = f"/tmp/temp_{uuid.uuid4().hex}.jpg"
    await bot.download_file(file.file_path, temp_path)

    out_filename = f"{uuid.uuid4().hex}.jpg"
    out_path = f"static/uploaded/{out_filename}"
    os.makedirs("static/uploaded", exist_ok=True)
    await convert_to_jpg(temp_path, out_path)
    os.remove(temp_path)

    # Удаляем старые фото
    product = await get_product_by_id(product_id)
    if product and 'images' in product:
        for img_path in product['images']:
            if img_path.startswith('/static/uploaded/'):
                local_path = img_path.replace('/static/uploaded/', 'static/uploaded/')
                if os.path.exists(local_path):
                    os.remove(local_path)

    await products_col.update_one({"id": product_id}, {"$set": {"images": [f"/static/uploaded/{out_filename}"]}})
    await state.clear()
    log_admin_action(message.from_user.id, f"Изменил фото товара ID {product_id}")
    await message.answer("✅ Фото обновлено.", reply_markup=get_main_keyboard(True))

@dp.message(EditProduct.new_value)
async def edit_invalid(message: Message):
    await message.answer("❌ Ожидался текст или фото. Попробуйте ещё раз.")

# ---------- Заказы (админ) ----------
@dp.message(F.text == "📋 Заказы")
async def show_orders(message: Message):
    if not is_admin(message.from_user.id):
        return
    cursor = orders_col.find({"status": "new"}).sort("created_at", -1)
    orders = await cursor.to_list(length=100)
    if not orders:
        await message.answer("Новых заказов нет.")
        return
    for o in orders:
        items = o['items']
        text = f"🛒 Заказ #{o['id']}\n"
        text += f"Покупатель: {o['user_name']} (ID: {o['user_id']})\n"
        for item in items:
            text += f"  • {item['name']} x{item['quantity']} = {item['price']*item['quantity']}₽\n"
        text += f"ИТОГО: {o['total']}₽\nСтатус: {o['status']}\n"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Выполнен", callback_data=f"order_status_{o['id']}_done"),
                InlineKeyboardButton(text="📦 Отправлен", callback_data=f"order_status_{o['id']}_shipped"),
                InlineKeyboardButton(text="❌ Отменён", callback_data=f"order_status_{o['id']}_cancelled")
            ]
        ])
        await message.answer(text, reply_markup=kb)

@dp.message(Command("orders_all"))
async def show_all_orders(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split()
    filter_status = None
    filter_date = None
    if len(args) > 1:
        for arg in args[1:]:
            if arg.startswith("status="):
                filter_status = arg.split("=")[1]
            elif arg.startswith("date="):
                filter_date = arg.split("=")[1]

    query = {}
    if filter_status:
        query["status"] = filter_status
    if filter_date:
        try:
            date_obj = datetime.strptime(filter_date, "%Y-%m-%d")
            query["created_at"] = {"$gte": date_obj, "$lt": date_obj + timedelta(days=1)}
        except:
            pass

    cursor = orders_col.find(query).sort("created_at", -1).limit(50)
    orders = await cursor.to_list(length=50)
    if not orders:
        await message.answer("Нет заказов по заданным критериям.")
        return

    for o in orders:
        items = o['items']
        text = f"🛒 Заказ #{o['id']}\n"
        text += f"Покупатель: {o['user_name']} (ID: {o['user_id']})\n"
        text += f"Статус: {o['status']}\n"
        for item in items:
            text += f"  • {item['name']} x{item['quantity']} = {item['price']*item['quantity']}₽\n"
        text += f"ИТОГО: {o['total']}₽\n"
        await message.answer(text)

@dp.message(Command("find_order"))
async def find_order(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Укажите номер заказа или ID пользователя: /find_order 123456")
        return
    query = args[1]
    order = await orders_col.find_one({"id": query})
    if order:
        items = order['items']
        text = f"🛒 Заказ #{order['id']}\n"
        text += f"Покупатель: {order['user_name']} (ID: {order['user_id']})\n"
        text += f"Статус: {order['status']}\n"
        for item in items:
            text += f"  • {item['name']} x{item['quantity']} = {item['price']*item['quantity']}₽\n"
        text += f"ИТОГО: {order['total']}₽"
        await message.answer(text)
        return
    cursor = orders_col.find({"user_id": query}).sort("created_at", -1).limit(5)
    orders = await cursor.to_list(length=5)
    if orders:
        for o in orders:
            items = o['items']
            text = f"🛒 Заказ #{o['id']}\n"
            text += f"Покупатель: {o['user_name']} (ID: {o['user_id']})\n"
            text += f"Статус: {o['status']}\n"
            for item in items:
                text += f"  • {item['name']} x{item['quantity']} = {item['price']*item['quantity']}₽\n"
            text += f"ИТОГО: {o['total']}₽"
            await message.answer(text)
    else:
        await message.answer("Заказ не найден.")

@dp.callback_query(lambda c: c.data.startswith("order_status_"))
async def change_order_status(callback: CallbackQuery):
    parts = callback.data.split('_')
    order_id = parts[2]
    new_status = parts[3]
    await orders_col.update_one({"id": order_id}, {"$set": {"status": new_status}})
    log_admin_action(callback.from_user.id, f"Изменил статус заказа #{order_id} на {new_status}")
    order = await orders_col.find_one({"id": order_id})
    if order:
        user_id = int(order['user_id'])
        try:
            await bot.send_message(user_id, f"🔄 Статус вашего заказа #{order_id} изменён на «{new_status}».")
        except Exception as e:
            logger.error(f"Не удалось уведомить пользователя {user_id}: {e}")
    await callback.message.edit_text(f"✅ Статус заказа #{order_id} изменён на «{new_status}».")
    await callback.answer()

# ---------- Промокоды ----------
@dp.message(Command("add_promo"))
async def cmd_add_promo(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AddPromo.code)
    await message.answer("Введите код промокода (например, SUMMER10):", reply_markup=get_cancel_keyboard())

@dp.message(AddPromo.code)
async def promo_code(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    existing = await promocodes_col.find_one({"code": code})
    if existing:
        await message.answer("❌ Такой код уже существует. Введите другой:")
        return
    await state.update_data(code=code)
    await state.set_state(AddPromo.promo_type)
    await message.answer("Выберите тип промокода: discount / wheel")

@dp.message(AddPromo.promo_type)
async def promo_type_handler(message: Message, state: FSMContext):
    t = message.text.lower()
    if t not in ['discount', 'wheel']:
        await message.answer("❌ Допустимо: discount или wheel. Попробуйте ещё раз:")
        return
    await state.update_data(promo_type=t)
    if t == 'discount':
        await state.set_state(AddPromo.discount_type)
        await message.answer("Выберите тип скидки: percent / fixed")
    else:
        await state.set_state(AddPromo.expires)
        await message.answer("Введите дату окончания в формате ГГГГ-ММ-ДД (или 'never' для бессрочного):")

@dp.message(AddPromo.discount_type)
async def promo_discount_type(message: Message, state: FSMContext):
    t = message.text.lower()
    if t not in ['percent', 'fixed']:
        await message.answer("❌ Допустимо: percent или fixed. Попробуйте ещё раз:")
        return
    await state.update_data(discount_type=t)
    await state.set_state(AddPromo.value)
    await message.answer("Введите размер скидки (для percent – число от 1 до 100, для fixed – сумма в рублях):")

@dp.message(AddPromo.value)
async def promo_value(message: Message, state: FSMContext):
    try:
        value = int(message.text)
        if value <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите положительное целое число.")
        return
    data = await state.get_data()
    if data.get('promo_type') == 'discount' and data.get('discount_type') == 'percent' and value > 100:
        await message.answer("❌ Процент не может быть больше 100.")
        return
    await state.update_data(value=value)
    await state.set_state(AddPromo.expires)
    await message.answer("Введите дату окончания в формате ГГГГ-ММ-ДД (или 'never' для бессрочного):")

@dp.message(AddPromo.expires)
async def promo_expires(message: Message, state: FSMContext):
    if message.text.lower() == 'never':
        expires = datetime(9999, 12, 31)
    else:
        try:
            expires = datetime.strptime(message.text, "%Y-%m-%d")
        except ValueError:
            await message.answer("❌ Неверный формат. Введите ГГГГ-ММ-ДД или 'never':")
            return
    await state.update_data(expires=expires)
    await state.set_state(AddPromo.max_uses)
    await message.answer("Введите максимальное количество использований (или 'unlimited'):")

@dp.message(AddPromo.max_uses)
async def promo_max_uses(message: Message, state: FSMContext):
    if message.text.lower() == 'unlimited':
        max_uses = 999999
    else:
        try:
            max_uses = int(message.text)
            if max_uses <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Введите положительное целое число или 'unlimited'.")
            return
    data = await state.get_data()
    promo_doc = {
        "code": data['code'],
        "type": data['promo_type'],
        "created_at": datetime.now()
    }
    if data['promo_type'] == 'discount':
        promo_doc.update({
            "discount_type": data['discount_type'],
            "value": data['value']
        })
    promo_doc.update({
        "expires_at": data['expires'],
        "max_uses": max_uses,
        "used_count": 0
    })
    await promocodes_col.insert_one(promo_doc)
    await state.clear()
    log_admin_action(message.from_user.id, f"Создал промокод {data['code']} типа {data['promo_type']}")
    await message.answer(f"✅ Промокод {data['code']} создан.", reply_markup=get_main_keyboard(True))

@dp.message(Command("list_promo"))
async def list_promo(message: Message):
    if not is_admin(message.from_user.id):
        return
    cursor = promocodes_col.find().sort("created_at", -1).limit(20)
    promos = await cursor.to_list(length=20)
    if not promos:
        await message.answer("Промокодов нет.")
        return
    text = "🎟️ <b>Активные промокоды:</b>\n\n"
    for p in promos:
        expires = p['expires_at'].strftime("%Y-%m-%d") if p['expires_at'] < datetime(9999,12,31) else "бессрочно"
        if p['type'] == 'discount':
            discount_info = f"{'%' if p['discount_type']=='percent' else '₽'} {p['value']}"
        else:
            discount_info = "активирует колесо"
        text += f"<b>{p['code']}</b> – {discount_info}, осталось: {p['max_uses'] - p['used_count']}/{p['max_uses']}, до {expires}\n"
    await message.answer(text)

@dp.message(Command("delete_promo"))
async def delete_promo(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Укажите код промокода: /delete_promo SUMMER10")
        return
    code = args[1].strip().upper()
    result = await promocodes_col.delete_one({"code": code})
    if result.deleted_count:
        log_admin_action(message.from_user.id, f"Удалил промокод {code}")
        await message.answer(f"✅ Промокод {code} удалён.")
    else:
        await message.answer(f"❌ Промокод {code} не найден.")

# ---------- Статистика популярности ----------
@dp.message(Command("popular"))
async def cmd_popular(message: Message):
    if not is_admin(message.from_user.id):
        return
    pipeline = [
        {"$unwind": "$items"},
        {"$group": {
            "_id": "$items.name",
            "total_quantity": {"$sum": "$items.quantity"},
            "total_revenue": {"$sum": {"$multiply": ["$items.price", "$items.quantity"]}}
        }},
        {"$sort": {"total_quantity": -1}},
        {"$limit": 10}
    ]
    cursor = orders_col.aggregate(pipeline)
    top = await cursor.to_list(length=10)
    if not top:
        await message.answer("Нет данных о продажах.")
        return
    text = "🔥 <b>Топ-10 товаров по продажам:</b>\n\n"
    for item in top:
        text += f"{item['_id']}: {item['total_quantity']} шт., выручка {item['total_revenue']} ₽\n"
    await message.answer(text)

# ---------- Экспорт всех данных (бекап) ----------
@dp.message(Command("backup"))
async def cmd_backup(message: Message):
    if not is_admin(message.from_user.id):
        return
    products = await products_col.find().to_list(length=10000)
    orders = await orders_col.find().to_list(length=10000)
    promos = await promocodes_col.find().to_list(length=1000)

    def convert_dates(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return obj

    backup = {
        "products": [{k: convert_dates(v) for k, v in p.items()} for p in products],
        "orders": [{k: convert_dates(v) for k, v in o.items()} for o in orders],
        "promocodes": [{k: convert_dates(v) for k, v in p.items()} for p in promos]
    }
    json_str = json.dumps(backup, indent=2, ensure_ascii=False)
    temp_file = f"/tmp/backup_{message.from_user.id}.json"
    with open(temp_file, "w", encoding="utf-8") as f:
        f.write(json_str)
    await message.answer_document(FSInputFile(temp_file), caption="📦 Резервная копия базы данных")
    os.remove(temp_file)

# ---------- Восстановление из бекапа ----------
@dp.message(Command("restore"))
async def cmd_restore(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("⚠️ ВНИМАНИЕ! Это действие удалит все текущие данные и заменит их из загруженного JSON-файла. Отправьте файл backup.json для восстановления.")

@dp.message(F.document)
async def handle_restore(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    if not message.document.file_name.endswith('.json'):
        await message.answer("❌ Пожалуйста, отправьте файл с расширением .json")
        return

    file = await bot.get_file(message.document.file_id)
    file_path = f"/tmp/restore_{message.from_user.id}.json"
    await bot.download_file(file.file_path, file_path)

    with open(file_path, 'r', encoding='utf-8') as f:
        try:
            backup = json.load(f)
        except Exception as e:
            await message.answer(f"❌ Ошибка парсинга JSON: {e}")
            return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтверждаю восстановление", callback_data="confirm_restore")]
    ])
    await message.answer("Восстановление удалит все текущие товары, заказы и промокоды. Вы уверены?", reply_markup=kb)
    global restore_file
    restore_file = file_path

@dp.callback_query(lambda c: c.data == "confirm_restore")
async def confirm_restore(callback: CallbackQuery):
    global restore_file
    try:
        with open(restore_file, 'r', encoding='utf-8') as f:
            backup = json.load(f)
        await products_col.delete_many({})
        await orders_col.delete_many({})
        await promocodes_col.delete_many({})

        if 'products' in backup:
            for p in backup['products']:
                if 'created_at' in p and isinstance(p['created_at'], str):
                    p['created_at'] = datetime.fromisoformat(p['created_at'])
                await products_col.insert_one(p)

        if 'orders' in backup:
            for o in backup['orders']:
                if 'created_at' in o and isinstance(o['created_at'], str):
                    o['created_at'] = datetime.fromisoformat(o['created_at'])
                await orders_col.insert_one(o)

        if 'promocodes' in backup:
            for pr in backup['promocodes']:
                if 'expires_at' in pr and isinstance(pr['expires_at'], str):
                    pr['expires_at'] = datetime.fromisoformat(pr['expires_at'])
                await promocodes_col.insert_one(pr)

        await callback.message.edit_text("✅ Восстановление выполнено успешно.")
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка при восстановлении: {e}")
    finally:
        if os.path.exists(restore_file):
            os.remove(restore_file)
    await callback.answer()

# ---------- Блокировка пользователей ----------
@dp.message(Command("block_user"))
async def cmd_block_user(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Укажите ID пользователя: /block_user 123456789")
        return
    user_id = args[1].strip()
    existing = await blocked_users_col.find_one({"user_id": user_id})
    if existing:
        await message.answer(f"Пользователь {user_id} уже заблокирован.")
        return
    await blocked_users_col.insert_one({"user_id": user_id, "blocked_at": datetime.now()})
    log_admin_action(message.from_user.id, f"Заблокировал пользователя {user_id}")
    await message.answer(f"✅ Пользователь {user_id} заблокирован.")

@dp.message(Command("unblock_user"))
async def cmd_unblock_user(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Укажите ID пользователя: /unblock_user 123456789")
        return
    user_id = args[1].strip()
    result = await blocked_users_col.delete_one({"user_id": user_id})
    if result.deleted_count:
        log_admin_action(message.from_user.id, f"Разблокировал пользователя {user_id}")
        await message.answer(f"✅ Пользователь {user_id} разблокирован.")
    else:
        await message.answer(f"❌ Пользователь {user_id} не найден в списке заблокированных.")

@dp.message(Command("list_blocked"))
async def list_blocked(message: Message):
    if not is_admin(message.from_user.id):
        return
    cursor = blocked_users_col.find().sort("blocked_at", -1).limit(50)
    blocked = await cursor.to_list(length=50)
    if not blocked:
        await message.answer("Нет заблокированных пользователей.")
        return
    text = "🚫 <b>Заблокированные пользователи:</b>\n\n"
    for b in blocked:
        text += f"ID: {b['user_id']} (с {b['blocked_at'].strftime('%Y-%m-%d')})\n"
    await message.answer(text)

# ---------- Управление призами колеса ----------
@dp.message(Command("wheel_prizes"))
async def cmd_wheel_prizes(message: Message):
    if not is_admin(message.from_user.id):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить приз", callback_data="wheel_add_prize")],
        [InlineKeyboardButton(text="📋 Список призов", callback_data="wheel_list_prizes")],
        [InlineKeyboardButton(text="🗑 Удалить приз", callback_data="wheel_del_prize")]
    ])
    await message.answer("🎁 Управление призами колеса фортуны:", reply_markup=kb)

@dp.callback_query(lambda c: c.data == "wheel_add_prize")
async def wheel_add_prize_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await state.set_state(WheelPrize.description)
    await callback.message.answer("Введите описание приза (то, что увидит пользователь):", reply_markup=get_cancel_keyboard())
    await callback.answer()

@dp.message(WheelPrize.description)
async def wheel_add_prize_desc(message: Message, state: FSMContext):
    await state.update_data(description=message.text)
    await state.set_state(WheelPrize.icon)
    await message.answer("Введите иконку для приза (например, эмодзи 🎁):", reply_markup=get_cancel_keyboard())

@dp.message(WheelPrize.icon)
async def wheel_add_prize_icon(message: Message, state: FSMContext):
    icon = message.text.strip()
    await state.update_data(icon=icon)
    await state.set_state(WheelPrize.type)
    await message.answer("Выберите тип приза: percent / fixed / bonus / shipping", reply_markup=get_cancel_keyboard())

@dp.message(WheelPrize.type)
async def wheel_add_prize_type(message: Message, state: FSMContext):
    t = message.text.lower()
    if t not in ['percent', 'fixed', 'bonus', 'shipping']:
        await message.answer("❌ Допустимо: percent, fixed, bonus, shipping")
        return
    await state.update_data(type=t)
    await state.set_state(WheelPrize.value)
    await message.answer("Введите значение (для percent – число 1-100, для fixed – сумма в рублях, для bonus – баллы, для shipping – 1):")

@dp.message(WheelPrize.value)
async def wheel_add_prize_value(message: Message, state: FSMContext):
    try:
        val = int(message.text)
        if val <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите положительное целое число.")
        return
    data = await state.get_data()
    if data['type'] == 'percent' and val > 100:
        await message.answer("❌ Процент не может быть больше 100.")
        return
    await state.update_data(value=val)
    await state.set_state(WheelPrize.probability)
    await message.answer("Введите вес приза (вероятность выпадения, по умолчанию 1). Можно оставить пустым.")

@dp.message(WheelPrize.probability)
async def wheel_add_prize_prob(message: Message, state: FSMContext):
    prob = 1
    if message.text.strip():
        try:
            prob = int(message.text)
            if prob <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Вес должен быть целым положительным числом.")
            return
    data = await state.get_data()
    prize_id = f"wp{uuid.uuid4().hex[:8]}"
    prize_doc = {
        "id": prize_id,
        "description": data['description'],
        "icon": data['icon'],
        "type": data['type'],
        "value": data['value'],
        "probability": prob,
        "created_at": datetime.now()
    }
    await wheel_prizes_col.insert_one(prize_doc)
    await state.clear()
    log_admin_action(message.from_user.id, f"Добавил приз колеса {prize_id}")
    await message.answer(f"✅ Приз добавлен! ID: {prize_id}", reply_markup=get_main_keyboard(True))

@dp.callback_query(lambda c: c.data == "wheel_list_prizes")
async def wheel_list_prizes(callback: CallbackQuery):
    cursor = wheel_prizes_col.find().sort("created_at", -1)
    prizes = await cursor.to_list(length=50)
    if not prizes:
        await callback.message.edit_text("Призов пока нет.")
        return
    text = "🎁 <b>Призы колеса фортуны:</b>\n\n"
    for p in prizes:
        text += f"ID: {p['id']} | {p['icon']} {p['description']} | {p['type']} | значение: {p['value']} | вес: {p['probability']}\n"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="wheel_back")]
    ]))

@dp.callback_query(lambda c: c.data == "wheel_del_prize")
async def wheel_del_prize_start(callback: CallbackQuery):
    await callback.message.edit_text("Введите ID приза для удаления (командой /del_prize <id>):")

@dp.message(Command("del_prize"))
async def cmd_del_prize(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Укажите ID приза: /del_prize wp123456")
        return
    prize_id = args[1].strip()
    result = await wheel_prizes_col.delete_one({"id": prize_id})
    if result.deleted_count:
        log_admin_action(message.from_user.id, f"Удалил приз {prize_id}")
        await message.answer(f"✅ Приз {prize_id} удалён.")
    else:
        await message.answer(f"❌ Приз {prize_id} не найден.")

@dp.callback_query(lambda c: c.data == "wheel_back")
async def wheel_back(callback: CallbackQuery):
    await cmd_wheel_prizes(callback.message)

# ---------- Общая статистика (кнопка) ----------
@dp.message(F.text == "📊 Статистика")
async def show_stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    total_products = await products_col.count_documents({})
    total_orders = await orders_col.count_documents({})
    pipeline = [{"$group": {"_id": None, "total_sales": {"$sum": "$total"}}}]
    cursor = orders_col.aggregate(pipeline)
    result = await cursor.to_list(length=1)
    total_sales = result[0]['total_sales'] if result else 0
    new_orders = await orders_col.count_documents({"status": "new"})

    text = (
        f"📊 <b>Статистика магазина</b>\n\n"
        f"📦 Товаров: {total_products}\n"
        f"🛒 Всего заказов: {total_orders}\n"
        f"💰 Сумма продаж: {total_sales} ₽\n"
        f"🆕 Новых заказов: {new_orders}"
    )
    await message.answer(text)

# ==================== FASTAPI ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(dp.start_polling(bot))
    await init_mongodb()
    yield
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== ПУБЛИЧНЫЕ API (для магазина) ====================
@app.get("/api/products")
async def get_products():
    cursor = products_col.find({})
    products = {}
    async for doc in cursor:
        cat = doc['category']
        sub = doc.get('subcategory')
        images = doc.get('images', [])
        full_image_urls = [f"{BASE_URL}{img}" for img in images]
        product = {
            "id": doc['id'],
            "name": doc['name'],
            "description": doc.get('description', ''),
            "price": doc['price'],
            "discount": doc.get('discount', 0),
            "isNew": doc.get('is_new', False),
            "images": full_image_urls,
            "img": full_image_urls[0] if full_image_urls else "/static/uploaded/default.jpg"
        }
        if cat == "vape":
            if cat not in products:
                products[cat] = {}
            if sub not in products[cat]:
                products[cat][sub] = []
            products[cat][sub].append(product)
        else:
            if cat not in products:
                products[cat] = []
            products[cat].append(product)
    return products

@app.post("/api/order")
async def create_order(request: Request):
    order = await request.json()
    total = order['total']
    promo_code = order.get('promo')
    discount = 0
    if promo_code:
        promo = await promocodes_col.find_one({"code": promo_code})
        if promo and promo.get('type') == 'discount' and promo.get('expires_at', datetime.now()) > datetime.now() and promo.get('used_count', 0) < promo.get('max_uses', 999999):
            if promo['discount_type'] == 'percent':
                discount = int(total * promo['value'] / 100)
            else:
                discount = promo['value']
            total -= discount
            await promocodes_col.update_one({"code": promo_code}, {"$inc": {"used_count": 1}})

    order_id = str(uuid.uuid4().hex[:8])
    order_doc = {
        "id": order_id,
        "user_id": order.get('user', 'unknown'),
        "user_name": order.get('user', 'unknown'),
        "items": order['items'],
        "total": total,
        "status": "new",
        "created_at": datetime.now(),
        "promo_used": promo_code,
        "discount_applied": discount
    }
    await orders_col.insert_one(order_doc)
    return {"status": "ok", "order_id": order_id}

from fastapi.staticfiles import StaticFiles

os.makedirs("static/uploaded", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.post("/api/check_promo")
async def check_promo(request: Request):
    try:
        data = await request.json()
        code = data.get('code', '').strip().upper()
        if not code:
            return {"valid": False, "error": "Введите код"}

        promo = await promocodes_col.find_one({"code": code})
        if not promo:
            return {"valid": False, "error": "Промокод не найден"}

        now = datetime.now()
        if promo.get('expires_at', now) < now:
            return {"valid": False, "error": "Срок действия истёк"}

        if promo.get('used_count', 0) >= promo.get('max_uses', 0):
            return {"valid": False, "error": "Промокод больше недействителен"}

        if promo['type'] == 'wheel':
            return {"valid": True, "type": "wheel", "code": code}
        else:
            return {
                "valid": True,
                "type": "discount",
                "discount": promo['value'],
                "discount_type": promo['discount_type'],
                "code": code
            }
    except Exception as e:
        logger.error(f"Ошибка проверки промокода: {e}")
        return {"valid": False, "error": "Ошибка сервера"}

@app.get("/api/wheel/prizes")
async def get_wheel_prizes():
    cursor = wheel_prizes_col.find({})
    prizes = await cursor.to_list(length=100)
    result = []
    for p in prizes:
        result.append({
            "id": p['id'],
            "description": p['description'],
            "icon": p.get('icon', '🎁'),
            "type": p['type'],
            "value": p['value'],
            "probability": p.get('probability', 1)
        })
    return result

# ==================== АДМИНСКИЕ API (защищённые) ====================
@app.post("/admin/login", response_model=Token)
async def admin_login(request: LoginRequest):
    if request.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=400, detail="Incorrect password")
    access_token = create_access_token(data={"sub": "admin"})
    return {"access_token": access_token, "token_type": "bearer"}

@app.post("/admin/login-form", response_model=Token)
async def admin_login_form(form_data: OAuth2PasswordRequestForm = Depends()):
    if form_data.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=400, detail="Incorrect password")
    access_token = create_access_token(data={"sub": "admin"})
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/admin/products")
async def admin_get_products(admin=Depends(get_current_admin)):
    cursor = products_col.find({})
    products = await cursor.to_list(length=10000)
    for p in products:
        p['_id'] = str(p['_id'])
    return products

@app.post("/admin/products")
async def admin_create_product(product: dict, admin=Depends(get_current_admin)):
    product_id = f"p{uuid.uuid4().hex[:8]}"
    product["id"] = product_id
    product["created_at"] = datetime.now()
    if "images" not in product:
        product["images"] = []
    await products_col.insert_one(product)
    log_admin_action(admin, f"Создал товар {product_id}")
    return {"id": product_id}

@app.put("/admin/products/{product_id}")
async def admin_update_product(product_id: str, product: dict, admin=Depends(get_current_admin)):
    result = await products_col.update_one({"id": product_id}, {"$set": product})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Product not found")
    log_admin_action(admin, f"Обновил товар {product_id}")
    return {"ok": True}

@app.delete("/admin/products/{product_id}")
async def admin_delete_product(product_id: str, admin=Depends(get_current_admin)):
    product = await products_col.find_one({"id": product_id})
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if product.get("images"):
        for img_path in product["images"]:
            if img_path.startswith('/static/uploaded/'):
                local_path = img_path.replace('/static/uploaded/', 'static/uploaded/')
                if os.path.exists(local_path):
                    os.remove(local_path)
    await products_col.delete_one({"id": product_id})
    log_admin_action(admin, f"Удалил товар {product_id}")
    return {"ok": True}

@app.get("/admin/orders")
async def admin_get_orders(status: Optional[str] = None, admin=Depends(get_current_admin)):
    query = {}
    if status:
        query["status"] = status
    cursor = orders_col.find(query).sort("created_at", -1)
    orders = await cursor.to_list(length=1000)
    for o in orders:
        o['_id'] = str(o['_id'])
    return orders

@app.patch("/admin/orders/{order_id}")
async def admin_update_order_status(order_id: str, status: str, admin=Depends(get_current_admin)):
    result = await orders_col.update_one({"id": order_id}, {"$set": {"status": status}})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Order not found")
    log_admin_action(admin, f"Изменил статус заказа {order_id} на {status}")
    return {"ok": True}

@app.get("/admin/promocodes")
async def admin_get_promocodes(admin=Depends(get_current_admin)):
    cursor = promocodes_col.find({})
    promos = await cursor.to_list(length=100)
    for p in promos:
        p['_id'] = str(p['_id'])
    return promos

@app.post("/admin/promocodes")
async def admin_create_promocode(promo: dict, admin=Depends(get_current_admin)):
    promo["created_at"] = datetime.now()
    promo["used_count"] = 0
    await promocodes_col.insert_one(promo)
    log_admin_action(admin, f"Создал промокод {promo.get('code')}")
    return {"ok": True}

@app.delete("/admin/promocodes/{code}")
async def admin_delete_promocode(code: str, admin=Depends(get_current_admin)):
    result = await promocodes_col.delete_one({"code": code})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Promocode not found")
    log_admin_action(admin, f"Удалил промокод {code}")
    return {"ok": True}

@app.get("/admin/wheel-prizes")
async def admin_get_wheel_prizes(admin=Depends(get_current_admin)):
    cursor = wheel_prizes_col.find({})
    prizes = await cursor.to_list(length=100)
    for p in prizes:
        p['_id'] = str(p['_id'])
    return prizes

@app.post("/admin/wheel-prizes")
async def admin_create_wheel_prize(prize: dict, admin=Depends(get_current_admin)):
    prize_id = f"wp{uuid.uuid4().hex[:8]}"
    prize["id"] = prize_id
    prize["created_at"] = datetime.now()
    await wheel_prizes_col.insert_one(prize)
    log_admin_action(admin, f"Создал приз колеса {prize_id}")
    return {"id": prize_id}

@app.delete("/admin/wheel-prizes/{prize_id}")
async def admin_delete_wheel_prize(prize_id: str, admin=Depends(get_current_admin)):
    result = await wheel_prizes_col.delete_one({"id": prize_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Prize not found")
    log_admin_action(admin, f"Удалил приз {prize_id}")
    return {"ok": True}

@app.get("/admin/blocked-users")
async def admin_get_blocked_users(admin=Depends(get_current_admin)):
    cursor = blocked_users_col.find({})
    users = await cursor.to_list(length=100)
    for u in users:
        u['_id'] = str(u['_id'])
    return users

@app.post("/admin/blocked-users")
async def admin_block_user(user_id: str, admin=Depends(get_current_admin)):
    existing = await blocked_users_col.find_one({"user_id": user_id})
    if existing:
        raise HTTPException(status_code=400, detail="User already blocked")
    await blocked_users_col.insert_one({"user_id": user_id, "blocked_at": datetime.now()})
    log_admin_action(admin, f"Заблокировал пользователя {user_id}")
    return {"ok": True}

@app.delete("/admin/blocked-users/{user_id}")
async def admin_unblock_user(user_id: str, admin=Depends(get_current_admin)):
    result = await blocked_users_col.delete_one({"user_id": user_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    log_admin_action(admin, f"Разблокировал пользователя {user_id}")
    return {"ok": True}

@app.get("/admin/stats")
async def admin_stats(admin=Depends(get_current_admin)):
    total_products = await products_col.count_documents({})
    total_orders = await orders_col.count_documents({})
    pipeline = [{"$group": {"_id": None, "total_sales": {"$sum": "$total"}}}]
    cursor = orders_col.aggregate(pipeline)
    result = await cursor.to_list(length=1)
    total_sales = result[0]['total_sales'] if result else 0
    new_orders = await orders_col.count_documents({"status": "new"})
    return {
        "total_products": total_products,
        "total_orders": total_orders,
        "total_sales": total_sales,
        "new_orders": new_orders
    }

@app.get("/admin/stats/detailed")
async def admin_stats_detailed(days: int = 7, admin=Depends(get_current_admin)):
    since = datetime.now() - timedelta(days=days)
    pipeline = [
        {"$match": {"created_at": {"$gt": since}}},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}},
            "count": {"$sum": 1},
            "total": {"$sum": "$total"}
        }},
        {"$sort": {"_id": -1}}
    ]
    cursor = orders_col.aggregate(pipeline)
    rows = await cursor.to_list(length=100)
    return rows

@app.get("/admin/popular")
async def admin_popular(limit: int = 10, admin=Depends(get_current_admin)):
    pipeline = [
        {"$unwind": "$items"},
        {"$group": {
            "_id": "$items.name",
            "total_quantity": {"$sum": "$items.quantity"},
            "total_revenue": {"$sum": {"$multiply": ["$items.price", "$items.quantity"]}}
        }},
        {"$sort": {"total_quantity": -1}},
        {"$limit": limit}
    ]
    cursor = orders_col.aggregate(pipeline)
    top = await cursor.to_list(length=limit)
    return top

@app.post("/admin/backup")
async def admin_backup(admin=Depends(get_current_admin)):
    products = await products_col.find().to_list(length=10000)
    orders = await orders_col.find().to_list(length=10000)
    promos = await promocodes_col.find().to_list(length=1000)

    def convert_dates(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return obj

    backup = {
        "products": [{k: convert_dates(v) for k, v in p.items()} for p in products],
        "orders": [{k: convert_dates(v) for k, v in o.items()} for o in orders],
        "promocodes": [{k: convert_dates(v) for k, v in p.items()} for p in promos]
    }
    return JSONResponse(content=backup)

@app.post("/admin/restore")
async def admin_restore(file: UploadFile = File(...), admin=Depends(get_current_admin)):
    if not file.filename.endswith('.json'):
        raise HTTPException(status_code=400, detail="Only JSON files allowed")
    content = await file.read()
    try:
        backup = json.loads(content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    await products_col.delete_many({})
    await orders_col.delete_many({})
    await promocodes_col.delete_many({})

    if 'products' in backup:
        for p in backup['products']:
            if 'created_at' in p and isinstance(p['created_at'], str):
                p['created_at'] = datetime.fromisoformat(p['created_at'])
            await products_col.insert_one(p)

    if 'orders' in backup:
        for o in backup['orders']:
            if 'created_at' in o and isinstance(o['created_at'], str):
                o['created_at'] = datetime.fromisoformat(o['created_at'])
            await orders_col.insert_one(o)

    if 'promocodes' in backup:
        for pr in backup['promocodes']:
            if 'expires_at' in pr and isinstance(pr['expires_at'], str):
                pr['expires_at'] = datetime.fromisoformat(pr['expires_at'])
            await promocodes_col.insert_one(pr)

    log_admin_action(admin, "Выполнил восстановление из резервной копии")
    return {"ok": True}

async def log_admin_action_db(admin_id: int, action: str, details: dict = None):
    """Запись действия администратора в базу данных."""
    log_entry = {
        "timestamp": datetime.now(),
        "admin_id": admin_id,
        "action": action,
        "details": details or {}
    }
    await admin_logs_col.insert_one(log_entry)
    # Также пишем в файловый лог (опционально)
    log_admin_action(admin_id, action)

# ==================== ДОПОЛНИТЕЛЬНЫЕ АДМИНСКИЕ API ====================

# --- Логи действий ---
@app.get("/admin/logs")
async def admin_get_logs(limit: int = 100, admin=Depends(get_current_admin)):
    cursor = admin_logs_col.find().sort("timestamp", -1).limit(limit)
    logs = await cursor.to_list(length=limit)
    for log in logs:
        log['_id'] = str(log['_id'])
    return logs

# --- Настройки магазина ---
@app.get("/admin/settings")
async def admin_get_settings(admin=Depends(get_current_admin)):
    # Получаем все настройки как объект
    settings = {}
    cursor = settings_col.find({})
    async for doc in cursor:
        settings[doc['key']] = doc['value']
    return settings

@app.post("/admin/settings")
async def admin_save_settings(settings: dict, admin=Depends(get_current_admin)):
    # Ожидается словарь вида {"key": "value", ...}
    # Сохраняем каждую пару как отдельный документ
    for key, value in settings.items():
        await settings_col.update_one(
            {"key": key},
            {"$set": {"value": value, "updated_at": datetime.now()}},
            upsert=True
        )
    log_admin_action_db(admin, "Обновил настройки", settings)
    return {"ok": True}

# --- Загрузка изображения (возвращает ссылку) ---
import shutil
from fastapi import UploadFile, File

@app.post("/admin/upload")
async def admin_upload_image(file: UploadFile = File(...), admin=Depends(get_current_admin)):
    # Проверяем, что это изображение
    if not file.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="File must be an image")
    
    # Сохраняем временный файл
    ext = os.path.splitext(file.filename)[1].lower()
    temp_filename = f"temp_{uuid.uuid4().hex}{ext}"
    temp_path = f"/tmp/{temp_filename}"
    
    with open(temp_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    # Конвертируем в JPG
    out_filename = f"{uuid.uuid4().hex}.jpg"
    out_path = f"static/uploaded/{out_filename}"
    os.makedirs("static/uploaded", exist_ok=True)
    
    # Используем ранее созданную функцию convert_to_jpg (она уже есть)
    await convert_to_jpg(temp_path, out_path)
    
    # Удаляем временный файл
    os.remove(temp_path)
    
    # Возвращаем полный URL
    image_url = f"{BASE_URL}/static/uploaded/{out_filename}"
    return {"url": image_url}

# ==================== СТАТИЧЕСКАЯ АДМИНКА ====================
from fastapi.responses import FileResponse

@app.get("/")
async def get_store():
    # Укажите путь к вашему HTML-файлу магазина
    return FileResponse("static/index.html")

# ==================== ЗАПУСК ====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)





