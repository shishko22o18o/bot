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
    """Создание индексов для коллекций."""
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
    await admin_logs_col.create_index("timestamp", -1)
    await settings_col.create_index("key", unique=True)
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
@app.get("/admin", response_class=HTMLResponse)
async def get_admin_page():
    html_content = """
    <!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bau28 Admin Pro</title>
    <!-- Chart.js для графиков -->
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            font-family: 'Inter', system-ui, sans-serif;
        }
        body {
            background-color: #0d0914;
            color: #f0f0f0;
            padding: 20px;
        }
        .container {
            max-width: 1400px;
            margin: 0 auto;
        }
        h1 {
            font-size: 2.5rem;
            margin-bottom: 1rem;
            color: #b829ff;
            text-shadow: 0 0 10px #b829ff;
        }
        .login-form {
            max-width: 400px;
            margin: 100px auto;
            background: rgba(20, 15, 28, 0.9);
            padding: 30px;
            border-radius: 20px;
            border: 1px solid #b829ff;
            box-shadow: 0 0 20px #b829ff;
        }
        .login-form h2 {
            text-align: center;
            margin-bottom: 20px;
            color: #fff;
        }
        input, select, textarea, button {
            width: 100%;
            padding: 12px;
            margin-bottom: 15px;
            border: 1px solid #333;
            border-radius: 10px;
            background: #1a1122;
            color: white;
            font-size: 1rem;
        }
        button {
            background: linear-gradient(135deg, #8a2be2, #b829ff);
            border: none;
            font-weight: bold;
            cursor: pointer;
            transition: transform 0.2s;
        }
        button:hover {
            transform: scale(1.02);
        }
        .error {
            color: #ff6b6b;
            text-align: center;
        }
        .hidden {
            display: none;
        }
        .tabs {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin-bottom: 20px;
            border-bottom: 1px solid #333;
            padding-bottom: 10px;
        }
        .tab-btn {
            flex: 1;
            min-width: 100px;
            background: #1a1122;
            border: 1px solid #b829ff;
            color: #b829ff;
            cursor: pointer;
            padding: 10px;
            border-radius: 10px;
            transition: 0.2s;
        }
        .tab-btn.active {
            background: #b829ff;
            color: white;
            box-shadow: 0 0 10px #b829ff;
        }
        .tab-content {
            display: none;
        }
        .tab-content.active {
            display: block;
        }
        .card {
            background: rgba(20, 15, 28, 0.8);
            border-radius: 15px;
            padding: 20px;
            margin-bottom: 20px;
            border: 1px solid #b829ff;
        }
        .card h3 {
            margin-bottom: 15px;
            color: #b829ff;
        }
        .flex-row {
            display: flex;
            gap: 10px;
            align-items: center;
            flex-wrap: wrap;
        }
        .item-list {
            max-height: 500px;
            overflow-y: auto;
        }
        .item {
            background: #1a1122;
            border-radius: 10px;
            padding: 15px;
            margin-bottom: 10px;
            border: 1px solid #333;
        }
        .item-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
        }
        .item-actions button {
            width: auto;
            padding: 5px 10px;
            margin: 0 2px;
            background: #333;
        }
        .item-actions button.edit { background: #4caf50; }
        .item-actions button.delete { background: #ff4444; }
        .grid-2 {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
        }
        @media (max-width: 600px) {
            .grid-2 { grid-template-columns: 1fr; }
        }
        table {
            width: 100%;
            border-collapse: collapse;
        }
        th, td {
            padding: 10px;
            text-align: left;
            border-bottom: 1px solid #333;
        }
        th {
            background: #1a1122;
        }
        .badge {
            background: #b829ff;
            padding: 2px 8px;
            border-radius: 10px;
            font-size: 0.8rem;
        }
        .status-select {
            width: auto;
            display: inline-block;
            padding: 5px;
            margin-right: 5px;
        }
        .preview-image {
            max-width: 100px;
            max-height: 100px;
            margin: 5px;
            border-radius: 5px;
            cursor: pointer;
        }
        .image-upload-preview {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin-bottom: 15px;
        }
        .image-upload-preview img {
            width: 80px;
            height: 80px;
            object-fit: cover;
            border-radius: 8px;
        }
        .remove-image {
            background: #ff4444;
            color: white;
            border: none;
            border-radius: 50%;
            width: 20px;
            height: 20px;
            cursor: pointer;
            margin-left: -20px;
            margin-top: -10px;
            font-size: 12px;
        }
        .log-entry {
            background: #1a1122;
            border-radius: 8px;
            padding: 10px;
            margin-bottom: 5px;
            border-left: 3px solid #b829ff;
        }
    </style>
</head>
<body>
    <div class="container">
        <div id="login-section" class="login-form">
            <h2>Вход в админ-панель</h2>
            <input type="password" id="password" placeholder="Пароль">
            <button onclick="login()">Войти</button>
            <div id="login-error" class="error"></div>
        </div>

        <div id="admin-section" class="hidden">
            <h1>Bau28 Admin Pro</h1>
            <div class="tabs" id="tabs">
                <button class="tab-btn active" data-tab="products">Товары</button>
                <button class="tab-btn" data-tab="orders">Заказы</button>
                <button class="tab-btn" data-tab="promos">Промокоды</button>
                <button class="tab-btn" data-tab="wheel">Призы колеса</button>
                <button class="tab-btn" data-tab="users">Пользователи</button>
                <button class="tab-btn" data-tab="stats">Статистика</button>
                <button class="tab-btn" data-tab="logs">Логи</button>
                <button class="tab-btn" data-tab="settings">Настройки</button>
                <button class="tab-btn" data-tab="backup">Резервное копирование</button>
            </div>

            <!-- ========== ТОВАРЫ (улучшенная версия) ========== -->
            <div id="tab-products" class="tab-content active">
                <div class="card">
                    <h3>Управление товарами</h3>
                    <button onclick="showAddProductForm()">➕ Добавить товар</button>
                    <div id="product-list" class="item-list"></div>
                </div>
            </div>

            <!-- ========== ЗАКАЗЫ ========== -->
            <div id="tab-orders" class="tab-content">
                <div class="card">
                    <h3>Заказы</h3>
                    <div class="flex-row">
                        <label>Фильтр по статусу:</label>
                        <select id="order-status-filter">
                            <option value="">Все</option>
                            <option value="new">Новые</option>
                            <option value="shipped">Отправленные</option>
                            <option value="done">Выполненные</option>
                            <option value="cancelled">Отменённые</option>
                        </select>
                        <button onclick="loadOrders()">Применить</button>
                        <button onclick="exportOrdersCSV()">📥 Экспорт CSV</button>
                    </div>
                    <div id="orders-list" class="item-list"></div>
                </div>
            </div>

            <!-- ========== ПРОМОКОДЫ ========== -->
            <div id="tab-promos" class="tab-content">
                <div class="card">
                    <h3>Промокоды</h3>
                    <button onclick="showAddPromoForm()">➕ Добавить промокод</button>
                    <div id="promo-list" class="item-list"></div>
                </div>
            </div>

            <!-- ========== ПРИЗЫ КОЛЕСА ========== -->
            <div id="tab-wheel" class="tab-content">
                <div class="card">
                    <h3>Призы колеса фортуны</h3>
                    <button onclick="showAddWheelPrizeForm()">➕ Добавить приз</button>
                    <div id="wheel-list" class="item-list"></div>
                </div>
            </div>

            <!-- ========== ПОЛЬЗОВАТЕЛИ ========== -->
            <div id="tab-users" class="tab-content">
                <div class="card">
                    <h3>Все пользователи</h3>
                    <div id="users-list" class="item-list">
                        <table id="users-table">
                            <thead>
                                <tr>
                                    <th>ID</th>
                                    <th>Имя</th>
                                    <th>Заказов</th>
                                    <th>Сумма</th>
                                    <th>Статус</th>
                                    <th>Действия</th>
                                </tr>
                            </thead>
                            <tbody></tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- ========== СТАТИСТИКА ========== -->
            <div id="tab-stats" class="tab-content">
                <div class="grid-2">
                    <div class="card">
                        <h3>Общая статистика</h3>
                        <div id="general-stats"></div>
                    </div>
                    <div class="card">
                        <h3>Популярные товары</h3>
                        <div id="popular-products"></div>
                    </div>
                </div>
                <div class="card">
                    <h3>Продажи по дням</h3>
                    <canvas id="salesChart" width="400" height="200"></canvas>
                </div>
            </div>

            <!-- ========== ЛОГИ ДЕЙСТВИЙ ========== -->
            <div id="tab-logs" class="tab-content">
                <div class="card">
                    <h3>Журнал действий</h3>
                    <div id="logs-list"></div>
                </div>
            </div>

            <!-- ========== НАСТРОЙКИ МАГАЗИНА ========== -->
            <div id="tab-settings" class="tab-content">
                <div class="card">
                    <h3>Настройки</h3>
                    <form id="settings-form">
                        <label>BASE_URL</label>
                        <input type="text" id="settings-base-url" value="">
                        <label>Контактный телефон</label>
                        <input type="text" id="settings-phone" value="">
                        <label>Email</label>
                        <input type="email" id="settings-email" value="">
                        <button type="button" onclick="saveSettings()">Сохранить</button>
                    </form>
                </div>
            </div>

            <!-- ========== РЕЗЕРВНОЕ КОПИРОВАНИЕ ========== -->
            <div id="tab-backup" class="tab-content">
                <div class="card">
                    <h3>Резервное копирование</h3>
                    <button onclick="createBackup()">📥 Скачать backup.json</button>
                    <div style="margin-top: 20px;">
                        <h4>Восстановление</h4>
                        <input type="file" id="restore-file" accept=".json">
                        <button onclick="restoreBackup()">📤 Восстановить</button>
                    </div>
                    <div id="backup-message"></div>
                </div>
            </div>
        </div>
    </div>

    <!-- Модальное окно добавления/редактирования товара (с загрузкой фото) -->
    <div id="product-form-modal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.8); align-items:center; justify-content:center;">
        <div style="background:#1a1122; padding:30px; border-radius:20px; max-width:600px; width:90%; max-height:90%; overflow-y:auto;">
            <h2 id="product-form-title">Добавление товара</h2>
            <form id="product-form" enctype="multipart/form-data">
                <input type="hidden" id="product-id" name="id">
                <input type="text" id="product-name" name="name" placeholder="Название *" required>
                <textarea id="product-description" name="description" placeholder="Описание" rows="3"></textarea>
                <input type="number" id="product-price" name="price" placeholder="Цена *" required>
                <select id="product-category" name="category">
                    <option value="clothes">Одежда</option>
                    <option value="accessories">Аксессуары</option>
                    <option value="vape">VAPE</option>
                    <option value="electronics">Электроника</option>
                </select>
                <input type="text" id="product-subcategory" name="subcategory" placeholder="Подкатегория (для vape)">
                <input type="number" id="product-discount" name="discount" placeholder="Скидка % (0-100)" value="0">
                <select id="product-isnew" name="is_new">
                    <option value="0">Не новинка</option>
                    <option value="1">Новинка</option>
                </select>

                <div>
                    <label>Изображения</label>
                    <input type="file" id="product-images" name="images" accept="image/*" multiple onchange="previewImages(event)">
                    <div id="image-preview" class="image-upload-preview"></div>
                </div>

                <div class="flex-row" style="margin-top:20px;">
                    <button type="button" onclick="saveProduct()">Сохранить</button>
                    <button type="button" onclick="closeProductForm()">Отмена</button>
                </div>
                <div id="product-form-error" style="color:red;"></div>
            </form>
        </div>
    </div>

    <!-- Модалка добавления промокода -->
    <div id="promo-form-modal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.8); align-items:center; justify-content:center;">
        <div style="background:#1a1122; padding:30px; border-radius:20px; max-width:500px; width:90%;">
            <h2>Добавление промокода</h2>
            <input type="text" id="promo-code" placeholder="Код *">
            <select id="promo-type">
                <option value="discount">Скидка</option>
                <option value="wheel">Колесо фортуны</option>
            </select>
            <div id="discount-fields">
                <select id="promo-discount-type">
                    <option value="percent">Процент</option>
                    <option value="fixed">Фиксированная сумма</option>
                </select>
                <input type="number" id="promo-value" placeholder="Значение (для процентов 1-100)">
            </div>
            <input type="text" id="promo-expires" placeholder="Дата окончания (ГГГГ-ММ-ДД) или 'never'">
            <input type="text" id="promo-max-uses" placeholder="Макс. использований (или 'unlimited')">
            <div class="flex-row" style="margin-top:20px;">
                <button onclick="savePromo()">Сохранить</button>
                <button onclick="closePromoForm()">Отмена</button>
            </div>
            <div id="promo-form-error" style="color:red;"></div>
        </div>
    </div>

    <!-- Модалка добавления приза колеса -->
    <div id="wheel-form-modal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.8); align-items:center; justify-content:center;">
        <div style="background:#1a1122; padding:30px; border-radius:20px; max-width:500px; width:90%;">
            <h2>Добавление приза</h2>
            <input type="text" id="wheel-description" placeholder="Описание *">
            <input type="text" id="wheel-icon" placeholder="Иконка (эмодзи)">
            <select id="wheel-type">
                <option value="percent">Процентная скидка</option>
                <option value="fixed">Фиксированная скидка (₽)</option>
                <option value="bonus">Бонусные баллы</option>
                <option value="shipping">Бесплатная доставка</option>
            </select>
            <input type="number" id="wheel-value" placeholder="Значение">
            <input type="number" id="wheel-probability" placeholder="Вероятность (вес)" value="1">
            <div class="flex-row" style="margin-top:20px;">
                <button onclick="saveWheelPrize()">Сохранить</button>
                <button onclick="closeWheelForm()">Отмена</button>
            </div>
            <div id="wheel-form-error" style="color:red;"></div>
        </div>
    </div>

    <script>
        let token = localStorage.getItem('token');
        let salesChart = null;

        // Загрузка страницы
        (async function() {
            if (token) {
                try {
                    const res = await fetch('/admin/products', { headers: { 'Authorization': `Bearer ${token}` } });
                    if (res.ok) {
                        document.getElementById('login-section').classList.add('hidden');
                        document.getElementById('admin-section').classList.remove('hidden');
                        await loadProducts();
                        await loadOrders();
                        await loadPromos();
                        await loadWheelPrizes();
                        await loadUsers();
                        await loadStats();
                        await loadLogs();
                        await loadSettings();
                    } else {
                        localStorage.removeItem('token');
                    }
                } catch (e) {
                    localStorage.removeItem('token');
                }
            }
        })();

        // Вход
        async function login() {
            const password = document.getElementById('password').value;
            const res = await fetch('/admin/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ password })
            });
            if (res.ok) {
                const data = await res.json();
                token = data.access_token;
                localStorage.setItem('token', token);
                document.getElementById('login-section').classList.add('hidden');
                document.getElementById('admin-section').classList.remove('hidden');
                await loadProducts();
                await loadOrders();
                await loadPromos();
                await loadWheelPrizes();
                await loadUsers();
                await loadStats();
                await loadLogs();
                await loadSettings();
            } else {
                document.getElementById('login-error').textContent = 'Неверный пароль';
            }
        }

        // Переключение вкладок
        document.querySelectorAll('.tab-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                const tab = btn.dataset.tab;
                document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
                document.getElementById(`tab-${tab}`).classList.add('active');
            });
        });

        // ========== ТОВАРЫ (с поддержкой нескольких фото) ==========
        async function loadProducts() {
            const res = await fetch('/admin/products', { headers: { 'Authorization': `Bearer ${token}` } });
            const products = await res.json();
            const container = document.getElementById('product-list');
            if (!products.length) {
                container.innerHTML = '<div class="item">Товаров нет</div>';
                return;
            }
            let html = '';
            products.forEach(p => {
                html += `
                    <div class="item" data-id="${p.id}">
                        <div class="item-header">
                            <strong>${p.name}</strong> (ID: ${p.id})
                            <div class="item-actions">
                                <button class="edit" onclick="editProduct('${p.id}')">✏️</button>
                                <button class="delete" onclick="deleteProduct('${p.id}')">🗑️</button>
                            </div>
                        </div>
                        <div>Цена: ${p.price} ₽, скидка: ${p.discount}%</div>
                        <div>Категория: ${p.category} / ${p.subcategory || '-'}</div>
                        <div>Описание: ${p.description || '-'}</div>
                        <div>Фото: ${p.images?.length || 0} шт.</div>
                        <div class="image-upload-preview">
                            ${(p.images || []).map(img => `<img src="${img}" class="preview-image" onclick="window.open('${img}')">`).join('')}
                        </div>
                    </div>
                `;
            });
            container.innerHTML = html;
        }

        // Предпросмотр загружаемых изображений
        function previewImages(event) {
            const preview = document.getElementById('image-preview');
            preview.innerHTML = '';
            Array.from(event.target.files).forEach(file => {
                const reader = new FileReader();
                reader.onload = (e) => {
                    const img = document.createElement('img');
                    img.src = e.target.result;
                    preview.appendChild(img);
                };
                reader.readAsDataURL(file);
            });
        }

        function showAddProductForm() {
            document.getElementById('product-form-title').textContent = 'Добавление товара';
            document.getElementById('product-id').value = '';
            document.getElementById('product-name').value = '';
            document.getElementById('product-description').value = '';
            document.getElementById('product-price').value = '';
            document.getElementById('product-category').value = 'clothes';
            document.getElementById('product-subcategory').value = '';
            document.getElementById('product-discount').value = '0';
            document.getElementById('product-isnew').value = '0';
            document.getElementById('image-preview').innerHTML = '';
            document.getElementById('product-images').value = '';
            document.getElementById('product-form-modal').style.display = 'flex';
        }

async function uploadImages(files) {
    const urls = [];
    for (let file of files) {
        const formData = new FormData();
        formData.append('file', file);
        const res = await fetch('/admin/upload', {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}` },
            body: formData
        });
        if (res.ok) {
            const data = await res.json();
            urls.push(data.url);
        }
    }
    return urls;
}

        function editProduct(id) {
            fetch(`/admin/products`, { headers: { 'Authorization': `Bearer ${token}` } })
                .then(res => res.json())
                .then(products => {
                    const product = products.find(p => p.id === id);
                    if (!product) return;
                    document.getElementById('product-form-title').textContent = 'Редактирование товара';
                    document.getElementById('product-id').value = product.id;
                    document.getElementById('product-name').value = product.name;
                    document.getElementById('product-description').value = product.description || '';
                    document.getElementById('product-price').value = product.price;
                    document.getElementById('product-category').value = product.category;
                    document.getElementById('product-subcategory').value = product.subcategory || '';
                    document.getElementById('product-discount').value = product.discount || 0;
                    document.getElementById('product-isnew').value = product.is_new ? '1' : '0';
                    // Заполняем превью существующими изображениями
                    const preview = document.getElementById('image-preview');
                    preview.innerHTML = '';
                    (product.images || []).forEach(imgUrl => {
                        const img = document.createElement('img');
                        img.src = imgUrl;
                        preview.appendChild(img);
                    });
                    document.getElementById('product-form-modal').style.display = 'flex';
                });
        }

        function closeProductForm() {
            document.getElementById('product-form-modal').style.display = 'none';
        }

        async function saveProduct() {
            const form = document.getElementById('product-form');
            const formData = new FormData(form);
            // Если редактирование, добавляем метод PUT
            const id = document.getElementById('product-id').value;
            const url = id ? `/admin/products/${id}` : '/admin/products';
            const method = id ? 'PUT' : 'POST';
            // Для обычного JSON-API мы бы отправляли JSON, но для файлов нужен FormData.
            // Предположим, бекенд поддерживает multipart/form-data. Если нет – придётся передавать как раньше.
            // Пока оставим как есть, но в реальности нужно доработать бекенд.
            // Здесь я использую fetch с FormData.
            const res = await fetch(url, {
                method: method,
                headers: { 'Authorization': `Bearer ${token}` },
                body: formData
            });
            if (res.ok) {
                closeProductForm();
                loadProducts();
            } else {
                const err = await res.text();
                document.getElementById('product-form-error').textContent = 'Ошибка: ' + err;
            }
        }

        async function deleteProduct(id) {
            if (!confirm('Удалить товар?')) return;
            const res = await fetch(`/admin/products/${id}`, {
                method: 'DELETE',
                headers: { 'Authorization': `Bearer ${token}` }
            });
            if (res.ok) {
                loadProducts();
            } else {
                alert('Ошибка удаления');
            }
        }

        // ========== ЗАКАЗЫ ==========
        async function loadOrders() {
            const status = document.getElementById('order-status-filter').value;
            const url = status ? `/admin/orders?status=${status}` : '/admin/orders';
            const res = await fetch(url, { headers: { 'Authorization': `Bearer ${token}` } });
            const orders = await res.json();
            const container = document.getElementById('orders-list');
            if (!orders.length) {
                container.innerHTML = '<div class="item">Заказов нет</div>';
                return;
            }
            let html = '';
            orders.forEach(o => {
                html += `
                    <div class="item">
                        <div class="item-header">
                            <strong>Заказ #${o.id}</strong> от ${o.user_name} (ID: ${o.user_id})
                            <span class="badge">${o.status}</span>
                        </div>
                        <div>Сумма: ${o.total} ₽, промо: ${o.promo_used || '-'}</div>
                        <div>Дата: ${new Date(o.created_at).toLocaleString()}</div>
                        <div>Товары: ${o.items.map(i => `${i.name} x${i.quantity}`).join(', ')}</div>
                        <div class="flex-row">
                            <select class="status-select" data-order-id="${o.id}">
                                <option value="new" ${o.status === 'new' ? 'selected' : ''}>Новый</option>
                                <option value="shipped" ${o.status === 'shipped' ? 'selected' : ''}>Отправлен</option>
                                <option value="done" ${o.status === 'done' ? 'selected' : ''}>Выполнен</option>
                                <option value="cancelled" ${o.status === 'cancelled' ? 'selected' : ''}>Отменён</option>
                            </select>
                            <button onclick="updateOrderStatus('${o.id}')">Сохранить</button>
                        </div>
                    </div>
                `;
            });
            container.innerHTML = html;
        }

        async function updateOrderStatus(orderId) {
            const select = document.querySelector(`select[data-order-id="${orderId}"]`);
            const newStatus = select.value;
            const res = await fetch(`/admin/orders/${orderId}?status=${newStatus}`, {
                method: 'PATCH',
                headers: { 'Authorization': `Bearer ${token}` }
            });
            if (res.ok) {
                loadOrders();
            } else {
                alert('Ошибка обновления статуса');
            }
        }

        async function exportOrdersCSV() {
            // Получаем все заказы (можно с фильтром) и формируем CSV
            const res = await fetch('/admin/orders', { headers: { 'Authorization': `Bearer ${token}` } });
            const orders = await res.json();
            let csv = 'ID,Покупатель,ID пользователя,Сумма,Статус,Дата,Товары\n';
            orders.forEach(o => {
                const items = o.items.map(i => `${i.name} (${i.quantity})`).join('; ');
                csv += `"${o.id}","${o.user_name}","${o.user_id}",${o.total},"${o.status}","${new Date(o.created_at).toLocaleString()}","${items}"\n`;
            });
            const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8;' }); // BOM для кириллицы
            const link = document.createElement('a');
            link.href = URL.createObjectURL(blob);
            link.download = `orders_${new Date().toISOString().slice(0,10)}.csv`;
            link.click();
        }

        // ========== ПРОМОКОДЫ ==========
        async function loadPromos() {
            const res = await fetch('/admin/promocodes', { headers: { 'Authorization': `Bearer ${token}` } });
            const promos = await res.json();
            const container = document.getElementById('promo-list');
            if (!promos.length) {
                container.innerHTML = '<div class="item">Промокодов нет</div>';
                return;
            }
            let html = '';
            promos.forEach(p => {
                html += `
                    <div class="item">
                        <div class="item-header">
                            <strong>${p.code}</strong> (тип: ${p.type})
                            <button class="delete" onclick="deletePromo('${p.code}')">🗑️</button>
                        </div>
                        <div>Скидка: ${p.discount_type === 'percent' ? p.value + '%' : (p.value ? p.value + '₽' : '—')}</div>
                        <div>Срок: ${p.expires_at ? new Date(p.expires_at).toLocaleDateString() : 'бессрочно'}</div>
                        <div>Использований: ${p.used_count}/${p.max_uses}</div>
                    </div>
                `;
            });
            container.innerHTML = html;
        }

        function showAddPromoForm() {
            document.getElementById('promo-form-modal').style.display = 'flex';
            document.getElementById('promo-code').value = '';
            document.getElementById('promo-type').value = 'discount';
            document.getElementById('promo-discount-type').value = 'percent';
            document.getElementById('promo-value').value = '';
            document.getElementById('promo-expires').value = '';
            document.getElementById('promo-max-uses').value = '';
        }

        function closePromoForm() {
            document.getElementById('promo-form-modal').style.display = 'none';
        }

        async function savePromo() {
            const promoData = {
                code: document.getElementById('promo-code').value.toUpperCase(),
                type: document.getElementById('promo-type').value,
                expires_at: document.getElementById('promo-expires').value === 'never' ? '9999-12-31' : document.getElementById('promo-expires').value,
                max_uses: document.getElementById('promo-max-uses').value === 'unlimited' ? 999999 : parseInt(document.getElementById('promo-max-uses').value),
            };
            if (promoData.type === 'discount') {
                promoData.discount_type = document.getElementById('promo-discount-type').value;
                promoData.value = parseInt(document.getElementById('promo-value').value);
            }
            if (!promoData.code) {
                document.getElementById('promo-form-error').textContent = 'Введите код';
                return;
            }
            const res = await fetch('/admin/promocodes', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': `Bearer ${token}`
                },
                body: JSON.stringify(promoData)
            });
            if (res.ok) {
                closePromoForm();
                loadPromos();
            } else {
                const err = await res.text();
                document.getElementById('promo-form-error').textContent = 'Ошибка: ' + err;
            }
        }

        async function deletePromo(code) {
            if (!confirm('Удалить промокод?')) return;
            const res = await fetch(`/admin/promocodes/${code}`, {
                method: 'DELETE',
                headers: { 'Authorization': `Bearer ${token}` }
            });
            if (res.ok) {
                loadPromos();
            } else {
                alert('Ошибка удаления');
            }
        }

        // ========== ПРИЗЫ КОЛЕСА ==========
        async function loadWheelPrizes() {
            const res = await fetch('/admin/wheel-prizes', { headers: { 'Authorization': `Bearer ${token}` } });
            const prizes = await res.json();
            const container = document.getElementById('wheel-list');
            if (!prizes.length) {
                container.innerHTML = '<div class="item">Призов нет</div>';
                return;
            }
            let html = '';
            prizes.forEach(p => {
                html += `
                    <div class="item">
                        <div class="item-header">
                            <strong>${p.icon} ${p.description}</strong> (ID: ${p.id})
                            <button class="delete" onclick="deleteWheelPrize('${p.id}')">🗑️</button>
                        </div>
                        <div>Тип: ${p.type}, значение: ${p.value}, вес: ${p.probability}</div>
                    </div>
                `;
            });
            container.innerHTML = html;
        }

        function showAddWheelPrizeForm() {
            document.getElementById('wheel-form-modal').style.display = 'flex';
            document.getElementById('wheel-description').value = '';
            document.getElementById('wheel-icon').value = '';
            document.getElementById('wheel-type').value = 'percent';
            document.getElementById('wheel-value').value = '';
            document.getElementById('wheel-probability').value = '1';
        }

        function closeWheelForm() {
            document.getElementById('wheel-form-modal').style.display = 'none';
        }

        async function saveWheelPrize() {
            const prizeData = {
                description: document.getElementById('wheel-description').value,
                icon: document.getElementById('wheel-icon').value || '🎁',
                type: document.getElementById('wheel-type').value,
                value: parseInt(document.getElementById('wheel-value').value),
                probability: parseInt(document.getElementById('wheel-probability').value) || 1
            };
            if (!prizeData.description || !prizeData.value) {
                document.getElementById('wheel-form-error').textContent = 'Заполните обязательные поля';
                return;
            }
            const res = await fetch('/admin/wheel-prizes', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': `Bearer ${token}`
                },
                body: JSON.stringify(prizeData)
            });
            if (res.ok) {
                closeWheelForm();
                loadWheelPrizes();
            } else {
                const err = await res.text();
                document.getElementById('wheel-form-error').textContent = 'Ошибка: ' + err;
            }
        }

        async function deleteWheelPrize(id) {
            if (!confirm('Удалить приз?')) return;
            const res = await fetch(`/admin/wheel-prizes/${id}`, {
                method: 'DELETE',
                headers: { 'Authorization': `Bearer ${token}` }
            });
            if (res.ok) {
                loadWheelPrizes();
            } else {
                alert('Ошибка удаления');
            }
        }

        // ========== ПОЛЬЗОВАТЕЛИ (сводная таблица) ==========
        async function loadUsers() {
            // Получаем всех пользователей из заказов и блокировок
            const ordersRes = await fetch('/admin/orders', { headers: { 'Authorization': `Bearer ${token}` } });
            const orders = await ordersRes.json();
            const blockedRes = await fetch('/admin/blocked-users', { headers: { 'Authorization': `Bearer ${token}` } });
            const blocked = await blockedRes.json();
            const blockedIds = new Set(blocked.map(b => b.user_id));
            
            // Группируем по user_id
            const usersMap = new Map();
            orders.forEach(o => {
                const userId = o.user_id;
                if (!usersMap.has(userId)) {
                    usersMap.set(userId, {
                        user_id: userId,
                        user_name: o.user_name,
                        orders: [],
                        total: 0
                    });
                }
                const user = usersMap.get(userId);
                user.orders.push(o);
                user.total += o.total;
            });

            const tbody = document.querySelector('#users-table tbody');
            tbody.innerHTML = '';
            for (let [userId, user] of usersMap) {
                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td>${userId}</td>
                    <td>${user.user_name}</td>
                    <td>${user.orders.length}</td>
                    <td>${user.total} ₽</td>
                    <td>${blockedIds.has(userId) ? '🔴 Заблокирован' : '🟢 Активен'}</td>
                    <td>
                        ${blockedIds.has(userId) 
                            ? `<button onclick="unblockUser('${userId}')">Разблокировать</button>` 
                            : `<button onclick="blockUser('${userId}')">Заблокировать</button>`}
                    </td>
                `;
                tbody.appendChild(tr);
            }
        }

        async function blockUser(userId) {
            const res = await fetch('/admin/blocked-users?user_id=' + userId, {
                method: 'POST',
                headers: { 'Authorization': `Bearer ${token}` }
            });
            if (res.ok) {
                loadUsers();
            } else {
                alert('Ошибка блокировки');
            }
        }

        async function unblockUser(userId) {
            const res = await fetch(`/admin/blocked-users/${userId}`, {
                method: 'DELETE',
                headers: { 'Authorization': `Bearer ${token}` }
            });
            if (res.ok) {
                loadUsers();
            } else {
                alert('Ошибка разблокировки');
            }
        }

        // ========== СТАТИСТИКА ==========
        async function loadStats() {
            const res = await fetch('/admin/stats', { headers: { 'Authorization': `Bearer ${token}` } });
            const stats = await res.json();
            document.getElementById('general-stats').innerHTML = `
                <p>📦 Товаров: ${stats.total_products}</p>
                <p>🛒 Заказов: ${stats.total_orders}</p>
                <p>💰 Сумма продаж: ${stats.total_sales} ₽</p>
                <p>🆕 Новых заказов: ${stats.new_orders}</p>
            `;
            const popRes = await fetch('/admin/popular', { headers: { 'Authorization': `Bearer ${token}` } });
            const pop = await popRes.json();
            let popHtml = '';
            if (pop.length) {
                pop.forEach(item => {
                    popHtml += `<p>${item._id}: ${item.total_quantity} шт., выручка ${item.total_revenue} ₽</p>`;
                });
            } else {
                popHtml = '<p>Нет данных</p>';
            }
            document.getElementById('popular-products').innerHTML = popHtml;

            // Загрузка детальной статистики для графика
            const detailedRes = await fetch('/admin/stats/detailed?days=30', { headers: { 'Authorization': `Bearer ${token}` } });
            const detailed = await detailedRes.json();
            const labels = detailed.map(d => d._id).reverse();
            const data = detailed.map(d => d.total).reverse();

            if (salesChart) salesChart.destroy();
            const ctx = document.getElementById('salesChart').getContext('2d');
            salesChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [{
                        label: 'Продажи, ₽',
                        data: data,
                        borderColor: '#b829ff',
                        backgroundColor: 'rgba(184,41,255,0.1)',
                        tension: 0.1
                    }]
                },
                options: {
                    responsive: true,
                    scales: {
                        y: { beginAtZero: true }
                    }
                }
            });
        }

        // ========== ЛОГИ ==========
        async function loadLogs() {
            // Предполагаем, что есть эндпоинт /admin/logs
            try {
                const res = await fetch('/admin/logs', { headers: { 'Authorization': `Bearer ${token}` } });
                if (!res.ok) {
                    document.getElementById('logs-list').innerHTML = '<p>Логи временно недоступны</p>';
                    return;
                }
                const logs = await res.json();
                const container = document.getElementById('logs-list');
                if (!logs.length) {
                    container.innerHTML = '<p>Логов нет</p>';
                    return;
                }
                container.innerHTML = logs.map(log => `
                    <div class="log-entry">
                        <strong>${new Date(log.timestamp).toLocaleString()}</strong> - ${log.admin} - ${log.action}
                    </div>
                `).join('');
            } catch (e) {
                document.getElementById('logs-list').innerHTML = '<p>Ошибка загрузки логов</p>';
            }
        }

        // ========== НАСТРОЙКИ ==========
        async function loadSettings() {
            // Предполагаем эндпоинт /admin/settings
            try {
                const res = await fetch('/admin/settings', { headers: { 'Authorization': `Bearer ${token}` } });
                if (res.ok) {
                    const settings = await res.json();
                    document.getElementById('settings-base-url').value = settings.BASE_URL || '';
                    document.getElementById('settings-phone').value = settings.phone || '';
                    document.getElementById('settings-email').value = settings.email || '';
                }
            } catch (e) {
                // игнорируем
            }
        }

        async function saveSettings() {
            const settings = {
                BASE_URL: document.getElementById('settings-base-url').value,
                phone: document.getElementById('settings-phone').value,
                email: document.getElementById('settings-email').value
            };
            const res = await fetch('/admin/settings', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': `Bearer ${token}`
                },
                body: JSON.stringify(settings)
            });
            if (res.ok) {
                alert('Настройки сохранены');
            } else {
                alert('Ошибка сохранения');
            }
        }

        // ========== РЕЗЕРВНОЕ КОПИРОВАНИЕ ==========
        async function createBackup() {
            const res = await fetch('/admin/backup', {
                method: 'POST',
                headers: { 'Authorization': `Bearer ${token}` }
            });
            const blob = await res.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `backup_${new Date().toISOString().slice(0,10)}.json`;
            a.click();
        }

        async function restoreBackup() {
            const fileInput = document.getElementById('restore-file');
            if (!fileInput.files.length) {
                alert('Выберите файл');
                return;
            }
            const file = fileInput.files[0];
            const formData = new FormData();
            formData.append('file', file);
            const res = await fetch('/admin/restore', {
                method: 'POST',
                headers: { 'Authorization': `Bearer ${token}` },
                body: formData
            });
            if (res.ok) {
                document.getElementById('backup-message').textContent = 'Восстановление успешно!';
                setTimeout(() => location.reload(), 2000);
            } else {
                const err = await res.text();
                document.getElementById('backup-message').textContent = 'Ошибка: ' + err;
            }
        }
    </script>
</body>
</html> """

    return FileResponse("static/admin.html")

# ==================== ЗАПУСК ====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
