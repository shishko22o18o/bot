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
import shutil

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
from fastapi.staticfiles import StaticFiles
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
from PIL import Image

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
    # URL магазина теперь на том же домене, поэтому просто относительный путь
    store_url = "/"  # корень сайта
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
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _convert_image, input_path, output_path, quality)

def _convert_image(input_path, output_path, quality):
    with Image.open(input_path) as img:
        if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
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
    photos = State()

class EditProduct(StatesGroup):
    choose_field = State()
    new_value = State()

class AddPromo(StatesGroup):
    code = State()
    promo_type = State()
    discount_type = State()
    value = State()
    expires = State()
    max_uses = State()

class WheelPrize(StatesGroup):
    description = State()
    icon = State()
    type = State()
    value = State()
    probability = State()

# ==================== ХЭНДЛЕРЫ БОТА ====================
# ВСТАВЬТЕ СЮДА ВСЕ ВАШИ ХЭНДЛЕРЫ ИЗ ТЕКУЩЕГО РАБОЧЕГО КОДА
# (они остаются без изменений)
# ...

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

# Монтируем статику (все файлы из папки static будут доступны по /static/...)
os.makedirs("static/uploaded", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

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

# ==================== АДМИНСКИЕ API ====================
# (все админские эндпоинты остаются без изменений, они были в предыдущей версии)
# Я их не копирую сюда для краткости, но они должны быть здесь полностью.
# В реальном коде вставьте их из предыдущего листинга.

# ==================== АДМИН-ПАНЕЛЬ ====================
@app.get("/admin", response_class=HTMLResponse)
async def get_admin_page():
    try:
        with open("static/index.html", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return HTMLResponse(content="Файл админ-панели не найден. Создайте static/admin.html", status_code=404)

# ==================== ГЛАВНАЯ СТРАНИЦА МАГАЗИНА ====================
@app.get("/", response_class=HTMLResponse)
async def get_store():
    try:
        with open("static/index.html", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return HTMLResponse(content="Файл магазина не найден. Создайте static/index.html", status_code=404)

# ==================== ЗАПУСК ====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
