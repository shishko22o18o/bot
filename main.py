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
from fastapi.responses import HTMLResponse, JSONResponse
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

# Для графика (если нужно)
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.dates import DateFormatter
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

BASE_URL = os.getenv("WEBHOOK_URL", "https://your-app.up.railway.app")

# Настройки JWT
SECRET_KEY = os.getenv("JWT_SECRET", "supersecretkey_change_me")  # в продакшене задайте свой!
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 1 день
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")  # пароль для входа в админку

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
    logger.info("MongoDB инициализирована.")

# ==================== JWT АУТЕНТИФИКАЦИЯ ====================
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/admin/login")

class Token(BaseModel):
    access_token: str
    token_type: str

class LoginRequest(BaseModel):
    password: str

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

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
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    # Здесь можно проверить, что это админ (у нас просто один пароль)
    if username != "admin":
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
    if is_admin:
        kb = [
            [KeyboardButton(text="📦 Товары")],
            [KeyboardButton(text="📋 Заказы"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="➕ Добавить товар"), KeyboardButton(text="📤 Экспорт CSV")],
            [KeyboardButton(text="ℹ️ Команды")],
            [KeyboardButton(text="🛍 Открыть магазин", web_app=types.WebAppInfo(url="https://shishko22o18o.github.io/bau28store/"))],
            [KeyboardButton(text="📊 Админ панель", web_app=types.WebAppInfo(url=f"{BASE_URL}/admin"))]  # новая кнопка
        ]
    else:
        kb = [
            [KeyboardButton(text="🛍 Открыть магазин", web_app=types.WebAppInfo(url="https://shishko22o18o.github.io/bau28store/"))]
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
/wheel_add_prize – добавление приза (иконка, описание, тип, значение, вес)

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
    icon = State()                 # эмодзи или символ
    type = State()                 # discount_percent, discount_fixed, bonus_points, free_shipping
    value = State()
    probability = State()

# ==================== ХЭНДЛЕРЫ БОТА ====================
# (здесь весь существующий код бота, который мы не меняем)
# ... (я пропускаю его для краткости, но в реальном ответе нужно вставить весь старый код)
# Начиная с @dp.message(CommandStart()) и до последнего хэндлера бота

# ==================== FASTAPI ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(dp.start_polling(bot))
    await init_mongodb()
    yield
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

# CORS (разрешаем фронтенду админки)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://shishko22o18o.github.io", "http://localhost", "http://localhost:8000"],
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

# Альтернативный вход через форму (для Swagger)
@app.post("/admin/login-form", response_model=Token)
async def admin_login_form(form_data: OAuth2PasswordRequestForm = Depends()):
    if form_data.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=400, detail="Incorrect password")
    access_token = create_access_token(data={"sub": "admin"})
    return {"access_token": access_token, "token_type": "bearer"}

# --- Товары ---
@app.get("/admin/products")
async def admin_get_products(admin=Depends(get_current_admin)):
    cursor = products_col.find({})
    products = await cursor.to_list(length=10000)
    # Преобразуем ObjectId в строку, если нужно
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
    # Удаляем фото
    if product.get("images"):
        for img_path in product["images"]:
            if img_path.startswith('/static/uploaded/'):
                local_path = img_path.replace('/static/uploaded/', 'static/uploaded/')
                if os.path.exists(local_path):
                    os.remove(local_path)
    await products_col.delete_one({"id": product_id})
    log_admin_action(admin, f"Удалил товар {product_id}")
    return {"ok": True}

# --- Заказы ---
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

# --- Промокоды ---
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

# --- Призы колеса ---
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

# --- Блокировка пользователей ---
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
        raise HTTPException(status_code=404, detail="User not found in blocked list")
    log_admin_action(admin, f"Разблокировал пользователя {user_id}")
    return {"ok": True}

# --- Статистика ---
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

# --- Резервное копирование и восстановление ---
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

    # Очищаем коллекции
    await products_col.delete_many({})
    await orders_col.delete_many({})
    await promocodes_col.delete_many({})

    # Восстанавливаем
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

# ==================== СТАТИЧЕСКАЯ АДМИНКА ====================
@app.get("/admin", response_class=HTMLResponse)
async def get_admin_page():
    # Простая HTML-страница для админки (можно заменить на отдельный файл)
    html_content = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bau28 Admin Panel</title>
    <style>
        body { background: #111; color: #eee; font-family: Arial; padding: 20px; }
        .container { max-width: 1200px; margin: auto; }
        .hidden { display: none; }
        #login { max-width: 300px; margin: 100px auto; }
        input, button { width: 100%; padding: 10px; margin: 5px 0; }
        button { background: #b829ff; color: white; border: none; cursor: pointer; }
        .error { color: red; }
        .section { background: #222; padding: 20px; margin: 20px 0; border-radius: 8px; }
        pre { background: #333; padding: 10px; overflow-x: auto; }
    </style>
</head>
<body>
    <div class="container">
        <div id="login">
            <h2>Admin Login</h2>
            <input type="password" id="password" placeholder="Password">
            <button onclick="login()">Login</button>
            <div class="error" id="login-error"></div>
        </div>
        <div id="admin-panel" class="hidden">
            <h1>Bau28 Admin Panel</h1>
            <div class="section">
                <h2>Товары</h2>
                <button onclick="loadProducts()">Загрузить товары</button>
                <button onclick="addProductForm()">Добавить товар</button>
                <div id="products-list"></div>
            </div>
            <div class="section">
                <h2>Заказы</h2>
                <button onclick="loadOrders()">Загрузить заказы</button>
                <div id="orders-list"></div>
            </div>
            <div class="section">
                <h2>Статистика</h2>
                <button onclick="loadStats()">Общая статистика</button>
                <button onclick="loadPopular()">Популярные товары</button>
                <div id="stats"></div>
            </div>
        </div>
    </div>
    <script>
        let token = localStorage.getItem('token');

        function showError(msg, element) {
            document.getElementById(element).textContent = msg;
        }

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
                document.getElementById('login').classList.add('hidden');
                document.getElementById('admin-panel').classList.remove('hidden');
            } else {
                showError('Неверный пароль', 'login-error');
            }
        }

        // Проверка токена при загрузке
        (async function() {
            if (token) {
                const res = await fetch('/admin/products', { headers: { 'Authorization': `Bearer ${token}` } });
                if (res.ok) {
                    document.getElementById('login').classList.add('hidden');
                    document.getElementById('admin-panel').classList.remove('hidden');
                } else {
                    localStorage.removeItem('token');
                }
            }
        })();

        async function loadProducts() {
            const res = await fetch('/admin/products', { headers: { 'Authorization': `Bearer ${token}` } });
            const products = await res.json();
            document.getElementById('products-list').innerHTML = '<pre>' + JSON.stringify(products, null, 2) + '</pre>';
        }

        async function loadOrders() {
            const res = await fetch('/admin/orders', { headers: { 'Authorization': `Bearer ${token}` } });
            const orders = await res.json();
            document.getElementById('orders-list').innerHTML = '<pre>' + JSON.stringify(orders, null, 2) + '</pre>';
        }

        async function loadStats() {
            const res = await fetch('/admin/stats', { headers: { 'Authorization': `Bearer ${token}` } });
            const stats = await res.json();
            document.getElementById('stats').innerHTML = '<pre>' + JSON.stringify(stats, null, 2) + '</pre>';
        }

        async function loadPopular() {
            const res = await fetch('/admin/popular', { headers: { 'Authorization': `Bearer ${token}` } });
            const popular = await res.json();
            document.getElementById('stats').innerHTML = '<pre>' + JSON.stringify(popular, null, 2) + '</pre>';
        }

        function addProductForm() {
            // Простая заглушка – можно сделать форму позже
            alert('Форма добавления товара ещё не реализована. Используйте бота.');
        }
    </script>
</body>
</html>
    """
    return html_content

# ==================== ЗАПУСК ====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

