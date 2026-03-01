import asyncio
import json
import logging
import uuid
import os
import csv
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager
from io import StringIO

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
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# MongoDB
import motor.motor_asyncio

# Дополнительные библиотеки
import aiofiles

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не задан в переменных окружения!")

ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_STR.split(",") if x.strip().isdigit()]

MONGO_URL = os.getenv("MONGO_URL")
if not MONGO_URL:
    raise ValueError("❌ MONGO_URL не задан в переменных окружения!")

BASE_URL = os.getenv("WEBHOOK_URL", "https://your-app.up.railway.app")

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
client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)
db = client["bau28shop"]
products_col = db["products"]
orders_col = db["orders"]

async def init_mongodb():
    """Создание индексов для коллекций."""
    await products_col.create_index("id", unique=True)
    await products_col.create_index("category")
    await products_col.create_index("subcategory")
    await orders_col.create_index("id", unique=True)
    await orders_col.create_index("status")
    logger.info("MongoDB инициализирована.")

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
            [KeyboardButton(text="🛍 Открыть магазин", web_app=types.WebAppInfo(url="https://shishko22o18o.github.io/bau28store/"))]
        ]
    else:
        kb = [
            [KeyboardButton(text="🛍 Открыть магазин", web_app=types.WebAppInfo(url="https://shishko22o18o.github.io/bau28store/"))]
        ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_cancel_keyboard():
    kb = [[KeyboardButton(text="❌ Отмена")]]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

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
    photo = State()

class EditProduct(StatesGroup):
    choose_field = State()
    new_value = State()

# ==================== ХЭНДЛЕРЫ БОТА ====================

@dp.message(CommandStart())
async def cmd_start(message: Message):
    admin = is_admin(message.from_user.id)
    welcome = (
        f"Привет, <b>{message.from_user.first_name}</b>! 👋\n\n"
        f"Добро пожаловать в <b>Bau28Store</b>.\n"
        f"{'Вы вошли как администратор.' if admin else 'Нажми кнопку ниже, чтобы открыть каталог.'}"
    )
    await message.answer(welcome, reply_markup=get_main_keyboard(admin))

@dp.message(F.text == "❌ Отмена", StateFilter("*"))
async def cancel_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=get_main_keyboard(is_admin(message.from_user.id)))

# ==================== ОБРАБОТКА ЗАКАЗОВ ИЗ WEB APP ====================
@dp.message(F.web_app_data)
async def handle_web_app_data(message: Message):
    try:
        data = json.loads(message.web_app_data.data)
        items = data.get('items', [])
        total = data.get('total', 0)

        if not items:
            await message.answer("❌ Корзина пуста. Заказ не оформлен.")
            return

        order_id = str(uuid.uuid4().hex[:8])
        order_doc = {
            "id": order_id,
            "user_id": str(message.from_user.id),
            "user_name": message.from_user.full_name,
            "items": items,
            "total": total,
            "status": "new",
            "created_at": datetime.now()
        }
        await orders_col.insert_one(order_doc)

        receipt = "🧾 <b>Детали заказа:</b>\n\n"
        for item in items:
            name = item.get('name', 'Товар')
            qty = item.get('quantity', 1)
            price = item.get('price', 0)
            sum_price = qty * price
            receipt += f"▪️ {name} — {qty} шт. x {price} ₽ = <b>{sum_price} ₽</b>\n"
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

# ==================== ДОБАВЛЕНИЕ ТОВАРА (ТОЛЬКО АДМИН) ====================
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
    await state.set_state(AddProduct.photo)
    await message.answer("Теперь отправьте фотографию товара:")

@dp.message(AddProduct.photo, F.photo)
async def add_photo(message: Message, state: FSMContext, bot: Bot):
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    ext = file.file_path.split('.')[-1] if '.' in file.file_path else 'jpg'
    filename = f"{uuid.uuid4().hex}.{ext}"
    file_path = f"static/uploaded/{filename}"
    os.makedirs("static/uploaded", exist_ok=True)
    await bot.download_file(file.file_path, file_path)

    data = await state.get_data()
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
        "image": f"/static/uploaded/{filename}",
        "created_at": datetime.now()
    }
    await products_col.insert_one(product_doc)

    await state.clear()
    log_admin_action(message.from_user.id, f"Добавил товар ID {product_id} ({data['name']})")
    await message.answer(f"✅ Товар добавлен! ID: {product_id}", reply_markup=get_main_keyboard(True))

@dp.message(AddProduct.photo)
async def add_photo_invalid(message: Message):
    await message.answer("❌ Пожалуйста, отправьте фотографию.")

# ==================== МАССОВОЕ ДОБАВЛЕНИЕ ЧЕРЕЗ CSV ====================
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
        next(reader, None)
        for row in reader:
            try:
                if len(row) < 7:
                    errors.append(f"Недостаточно полей: {row}")
                    continue
                name, desc, price_str, cat, subcat, discount_str, is_new_str = row
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
                    "image": "",
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

# ==================== ЭКСПОРТ ТОВАРОВ В CSV ====================
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
    writer.writerow(["id", "name", "description", "price", "category", "subcategory", "discount", "is_new", "image"])
    for p in products:
        writer.writerow([p['id'], p['name'], p['description'], p['price'], p['category'], p['subcategory'], p['discount'], p['is_new'], p.get('image', '')])
    csv_data = output.getvalue().encode('utf-8')
    output.close()

    temp_file = f"/tmp/export_{message.from_user.id}.csv"
    with open(temp_file, "wb") as f:
        f.write(csv_data)
    await message.answer_document(FSInputFile(temp_file), caption="📁 Экспорт товаров")
    os.remove(temp_file)

# ==================== ДЕТАЛЬНАЯ СТАТИСТИКА ====================
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

# ==================== ПОИСК ТОВАРОВ ====================
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

# ==================== ПРОСМОТР ТОВАРОВ (АДМИН) ====================
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

# ==================== УДАЛЕНИЕ ТОВАРА ====================
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
    await products_col.delete_one({"id": product_id})
    log_admin_action(callback.from_user.id, f"Удалил товар ID {product_id} ({name})")
    await callback.message.edit_text(f"✅ Товар ID {product_id} удалён.")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "cancel_del")
async def cancel_delete(callback: CallbackQuery):
    await callback.message.delete()
    await cmd_start(callback.message)

# ==================== РЕДАКТИРОВАНИЕ ТОВАРА ====================
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
        await callback.message.edit_text("Отправьте новое фото:")

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
        update_data["subcategory"] = ""  # сбрасываем подкатегорию
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
    ext = file.file_path.split('.')[-1] if '.' in file.file_path else 'jpg'
    filename = f"{uuid.uuid4().hex}.{ext}"
    file_path = f"static/uploaded/{filename}"
    os.makedirs("static/uploaded", exist_ok=True)
    await bot.download_file(file.file_path, file_path)

    await products_col.update_one({"id": product_id}, {"$set": {"image": f"/static/uploaded/{filename}"}})
    await state.clear()
    log_admin_action(message.from_user.id, f"Изменил фото товара ID {product_id}")
    await message.answer("✅ Фото обновлено.", reply_markup=get_main_keyboard(True))

@dp.message(EditProduct.new_value)
async def edit_invalid(message: Message):
    await message.answer("❌ Ожидался текст или фото. Попробуйте ещё раз.")

# ==================== ЗАКАЗЫ (АДМИН) ====================
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
            [InlineKeyboardButton(text="✅ Отметить выполненным", callback_data=f"order_done_{o['id']}")]
        ])
        await message.answer(text, reply_markup=kb)

@dp.callback_query(lambda c: c.data.startswith("order_done_"))
async def order_done(callback: CallbackQuery):
    order_id = callback.data.split("_")[2]
    await orders_col.update_one({"id": order_id}, {"$set": {"status": "done"}})
    log_admin_action(callback.from_user.id, f"Отметил заказ #{order_id} выполненным")
    await callback.message.edit_text(f"✅ Заказ #{order_id} отмечен как выполненный.")
    await callback.answer()

# ==================== СТАТИСТИКА (АДМИН) ====================
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
    # Запускаем бота
    asyncio.create_task(dp.start_polling(bot))
    # Инициализируем MongoDB
    await init_mongodb()
    yield
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://shishko22o18o.github.io"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/products")
async def get_products():
    cursor = products_col.find({})
    products = {}
    async for doc in cursor:
        cat = doc['category']
        sub = doc.get('subcategory')
        product = {
            "id": doc['id'],
            "name": doc['name'],
            "description": doc.get('description', ''),
            "price": doc['price'],
            "discount": doc.get('discount', 0),
            "isNew": doc.get('is_new', False),
            "img": f"{BASE_URL}{doc['image']}" if doc.get('image') else "/static/uploaded/default.jpg"
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
    order_id = str(uuid.uuid4().hex[:8])
    order_doc = {
        "id": order_id,
        "user_id": order.get('user', 'unknown'),
        "user_name": order.get('user', 'unknown'),
        "items": order['items'],
        "total": order['total'],
        "status": "new",
        "created_at": datetime.now()
    }
    await orders_col.insert_one(order_doc)
    return {"status": "ok", "order_id": order_id}

from fastapi.staticfiles import StaticFiles

os.makedirs("static/uploaded", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ==================== ЗАПУСК ====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
