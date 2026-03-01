import asyncio
import json
import logging
import sqlite3
import uuid
import os
from datetime import datetime
from typing import List, Optional, Dict, Any
from contextlib import asynccontextmanager

# Импорты aiogram
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# Импорты FastAPI
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# Дополнительные библиотеки
import aiofiles

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN",)
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_STR.split(",") if x.strip().isdigit()]
if not ADMIN_IDS:
    ADMIN_IDS = []  # Задайте хотя бы один ID через переменную окружения

DB_PATH = "shop.db"
BASE_URL = os.getenv("WEBHOOK_URL", "https://bot-production-cf41.up.railway.app")  # замени на свой URL

# ==================== БАЗА ДАННЫХ ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS products
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT NOT NULL,
                  price INTEGER NOT NULL,
                  category TEXT NOT NULL,
                  subcategory TEXT,
                  discount INTEGER DEFAULT 0,
                  is_new INTEGER DEFAULT 0,
                  image TEXT,
                  created_at TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id TEXT,
                  user_name TEXT,
                  items TEXT,
                  total INTEGER,
                  status TEXT DEFAULT 'new',
                  created_at TIMESTAMP)''')
    conn.commit()
    conn.close()

init_db()

# ==================== ИНИЦИАЛИЗАЦИЯ БОТА ====================
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=storage)

# ==================== FSM ====================
class AddProduct(StatesGroup):
    name = State()
    price = State()
    category = State()
    subcategory = State()
    discount = State()
    is_new = State()
    photo = State()

class EditProduct(StatesGroup):
    choose_field = State()
    new_value = State()

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def format_price(price: int) -> str:
    return f"{price:,} ₽".replace(",", " ")

def get_product_by_id(product_id: int) -> Optional[Dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE id=?", (product_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            "id": row[0],
            "name": row[1],
            "price": row[2],
            "category": row[3],
            "subcategory": row[4],
            "discount": row[5],
            "is_new": row[6],
            "image": row[7],
            "created_at": row[8]
        }
    return None

def get_main_keyboard(is_admin: bool = False):
    if is_admin:
        kb = [
            [KeyboardButton(text="📦 Товары")],
            [KeyboardButton(text="📋 Заказы"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="➕ Добавить товар")],
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

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''INSERT INTO orders (user_id, user_name, items, total, created_at)
                     VALUES (?, ?, ?, ?, ?)''',
                  (str(message.from_user.id), message.from_user.full_name,
                   json.dumps(items), total, datetime.now()))
        order_id = c.lastrowid
        conn.commit()
        conn.close()

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
                logging.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")

    except Exception as e:
        logging.error(f"Ошибка при обработке заказа: {e}")
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
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO products
                 (name, price, category, subcategory, discount, is_new, image, created_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
              (data['name'], data['price'], data['category'], data.get('subcategory', ''),
               data['discount'], data['is_new'], f"/static/uploaded/{filename}", datetime.now()))
    product_id = c.lastrowid
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer(f"✅ Товар добавлен! ID: {product_id}", reply_markup=get_main_keyboard(True))

@dp.message(AddProduct.photo)
async def add_photo_invalid(message: Message):
    await message.answer("❌ Пожалуйста, отправьте фотографию.")

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
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, name, price, discount FROM products WHERE category=? ORDER BY created_at DESC LIMIT 5 OFFSET ?", (cat, page*5))
    products = c.fetchall()
    c.execute("SELECT COUNT(*) FROM products WHERE category=?", (cat,))
    total = c.fetchone()[0]
    conn.close()

    if not products:
        await callback.message.edit_text("В этой категории пока нет товаров.")
        return

    text = f"Товары в категории {cat} (стр. {page+1}):\n\n"
    for p in products:
        final_price = p[3] if p[3] else p[2]
        text += f"ID: {p[0]} | {p[1]} | {final_price}₽\n"

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
            InlineKeyboardButton(text=f"✏️ {p[1][:15]}...", callback_data=f"edit_{p[0]}_menu"),
            InlineKeyboardButton(text="🗑", callback_data=f"del_{p[0]}")
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
    product_id = int(callback.data.split("_")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да", callback_data=f"confirm_del_{product_id}"),
            InlineKeyboardButton(text="❌ Нет", callback_data="cancel_del")
        ]
    ])
    await callback.message.edit_text(f"Удалить товар ID {product_id}?", reply_markup=kb)

@dp.callback_query(lambda c: c.data.startswith("confirm_del_"))
async def confirm_delete(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM products WHERE id=?", (product_id,))
    conn.commit()
    conn.close()
    await callback.message.edit_text(f"✅ Товар ID {product_id} удалён.")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "cancel_del")
async def cancel_delete(callback: CallbackQuery):
    await callback.message.delete()
    await cmd_start(callback.message)

# ==================== РЕДАКТИРОВАНИЕ ТОВАРА ====================
@dp.callback_query(lambda c: c.data.startswith("edit_") and c.data.endswith("_menu"))
async def edit_product_menu(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[1])
    product = get_product_by_id(product_id)
    if not product:
        await callback.message.edit_text("Товар не найден.")
        return

    text = f"Редактирование товара ID {product_id}:\n"
    text += f"Название: {product['name']}\n"
    text += f"Цена: {product['price']}₽\n"
    text += f"Категория: {product['category']}\n"
    if product['subcategory']:
        text += f"Подкатегория: {product['subcategory']}\n"
    text += f"Скидка: {product['discount']}%\n"
    text += f"Новинка: {'да' if product['is_new'] else 'нет'}\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Название", callback_data=f"edit_{product_id}_field_name")],
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
    product_id = int(parts[1])
    field = parts[3]
    await state.update_data(edit_id=product_id, edit_field=field)
    if field == "name":
        await state.set_state(EditProduct.new_value)
        await callback.message.edit_text("Введите новое название:")
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
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT is_new FROM products WHERE id=?", (product_id,))
        row = c.fetchone()
        if row:
            new_val = 0 if row[0] else 1
            c.execute("UPDATE products SET is_new=? WHERE id=?", (new_val, product_id))
            conn.commit()
        conn.close()
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

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if field == "name":
        c.execute("UPDATE products SET name=? WHERE id=?", (new_value, product_id))
    elif field == "price":
        if not new_value.isdigit():
            await message.answer("❌ Цена должна быть числом. Попробуйте ещё раз:")
            return
        c.execute("UPDATE products SET price=? WHERE id=?", (int(new_value), product_id))
    elif field == "category":
        if new_value not in ['clothes', 'accessories', 'vape', 'electronics']:
            await message.answer("❌ Неверная категория. Допустимы: clothes, accessories, vape, electronics")
            return
        c.execute("UPDATE products SET category=?, subcategory=? WHERE id=?", (new_value, None, product_id))
    elif field == "discount":
        if not new_value.isdigit():
            await message.answer("❌ Скидка должна быть числом. Попробуйте ещё раз:")
            return
        c.execute("UPDATE products SET discount=? WHERE id=?", (int(new_value), product_id))
    conn.commit()
    conn.close()
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

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE products SET image=? WHERE id=?", (f"/static/uploaded/{filename}", product_id))
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer("✅ Фото обновлено.", reply_markup=get_main_keyboard(True))

@dp.message(EditProduct.new_value)
async def edit_invalid(message: Message):
    await message.answer("❌ Ожидался текст или фото. Попробуйте ещё раз.")

# ==================== ЗАКАЗЫ (АДМИН) ====================
@dp.message(F.text == "📋 Заказы")
async def show_orders(message: Message):
    if not is_admin(message.from_user.id):
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE status='new' ORDER BY created_at DESC")
    orders = c.fetchall()
    conn.close()
    if not orders:
        await message.answer("Новых заказов нет.")
        return
    for o in orders:
        items = json.loads(o[3])
        text = f"🛒 Заказ #{o[0]}\n"
        text += f"Покупатель: {o[2]} (ID: {o[1]})\n"
        for item in items:
            text += f"  • {item['name']} x{item['quantity']} = {item['price']*item['quantity']}₽\n"
        text += f"ИТОГО: {o[4]}₽\nСтатус: {o[5]}\n"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отметить выполненным", callback_data=f"order_done_{o[0]}")]
        ])
        await message.answer(text, reply_markup=kb)

@dp.callback_query(lambda c: c.data.startswith("order_done_"))
async def order_done(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE orders SET status='done' WHERE id=?", (order_id,))
    conn.commit()
    conn.close()
    await callback.message.edit_text(f"✅ Заказ #{order_id} отмечен как выполненный.")
    await callback.answer()

# ==================== СТАТИСТИКА (АДМИН) ====================
@dp.message(F.text == "📊 Статистика")
async def show_stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM products")
    total_products = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM orders")
    total_orders = c.fetchone()[0]
    c.execute("SELECT SUM(total) FROM orders")
    total_sales = c.fetchone()[0] or 0
    c.execute("SELECT COUNT(*) FROM orders WHERE status='new'")
    new_orders = c.fetchone()[0]
    conn.close()
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
    # Запускаем бота при старте приложения
    asyncio.create_task(dp.start_polling(bot))
    yield
    # Останавливаем бота при завершении
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
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM products")
    rows = c.fetchall()
    conn.close()
    
    products = {}
    for row in rows:
        cat = row[3]      # category
        sub = row[4]      # subcategory
        image = row[7]    # image path
        full_image_url = f"{BASE_URL}{image}" if image else None
        product = {
            "id": f"p{row[0]}",
            "name": row[1],
            "price": row[2],
            "discount": row[5],
            "isNew": bool(row[6]),
            "img": full_image_url or "/static/uploaded/default.jpg"
        }
        if cat == "vape":
            if sub not in products.get("vape", {}):
                products.setdefault("vape", {})[sub] = []
            products["vape"][sub].append(product)
        else:
            products.setdefault(cat, []).append(product)
    return products

@app.post("/api/order")
async def create_order(request: Request):
    order = await request.json()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO orders 
                 (user_id, user_name, items, total, created_at)
                 VALUES (?, ?, ?, ?, ?)''',
              (order.get('user', 'unknown'), order.get('user', 'unknown'),
               json.dumps(order['items']), order['total'], datetime.now()))
    order_id = c.lastrowid
    conn.commit()
    conn.close()
    return {"status": "ok", "order_id": order_id}

# ==================== ЗАПУСК ====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)


