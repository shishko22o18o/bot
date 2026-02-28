import os
import json
import logging
import sqlite3
import uuid
from datetime import datetime
from typing import List, Optional

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn
import aiofiles

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("8713317147:AAESC0L-U3cY9Ga6ta2w_s7TaV6WSeCVZ-k")
ADMIN_IDS_STR = os.getenv("5896826944", "")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_STR.split(",") if x.strip().isdigit()]
if not ADMIN_IDS:
    ADMIN_IDS = [5896826944]  # укажи хотя бы один ID через переменную окружения

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://your-app.up.railway.app")
BASE_URL = WEBHOOK_URL

# ==================== БАЗА ДАННЫХ ====================
def init_db():
    conn = sqlite3.connect('shop.db')
    c = conn.cursor()
    # Таблица товаров
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
    # Таблица заказов
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
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)

# ==================== FSM ДЛЯ ДОБАВЛЕНИЯ ТОВАРА ====================
class AddProduct(StatesGroup):
    name = State()
    price = State()
    category = State()
    subcategory = State()
    discount = State()
    is_new = State()
    photo = State()

# ==================== FSM ДЛЯ РЕДАКТИРОВАНИЯ ТОВАРА ====================
class EditProduct(StatesGroup):
    choose_field = State()
    new_value = State()

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def get_main_keyboard():
    kb = [
        [KeyboardButton(text="📦 Товары")],
        [KeyboardButton(text="📋 Заказы"), KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="➕ Добавить товар")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_cancel_keyboard():
    kb = [[KeyboardButton(text="❌ Отмена")]]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

# ==================== КОМАНДА СТАРТ ====================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещён")
        return
    await message.answer("👋 Добро пожаловать в админ-панель магазина!", reply_markup=get_main_keyboard())

# ==================== ОБРАБОТКА ГЛАВНОГО МЕНЮ ====================
@dp.message(F.text == "📦 Товары")
async def show_products_menu(message: Message):
    if not is_admin(message.from_user.id):
        return
    # Показываем категории
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👕 Одежда", callback_data="list_clothes")],
        [InlineKeyboardButton(text="🕶 Аксессуары", callback_data="list_accessories")],
        [InlineKeyboardButton(text="💨 VAPE", callback_data="list_vape")],
        [InlineKeyboardButton(text="🎧 Электроника", callback_data="list_electronics")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
    ])
    await message.answer("Выберите категорию:", reply_markup=keyboard)

@dp.message(F.text == "📋 Заказы")
async def show_orders(message: Message):
    if not is_admin(message.from_user.id):
        return
    conn = sqlite3.connect('shop.db')
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
        text += f"Пользователь: {o[2]}\n"
        for item in items:
            text += f"  • {item['name']} x{item['quantity']} = {item['price']*item['quantity']}₽\n"
        text += f"ИТОГО: {o[4]}₽\n"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отметить выполненным", callback_data=f"order_done_{o[0]}")]
        ])
        await message.answer(text, reply_markup=kb)

@dp.message(F.text == "📊 Статистика")
async def show_stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    conn = sqlite3.connect('shop.db')
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM products")
    total_products = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM orders")
    total_orders = c.fetchone()[0]
    c.execute("SELECT SUM(total) FROM orders")
    total_sales = c.fetchone()[0] or 0
    conn.close()
    text = f"📊 Статистика магазина:\n\n"
    text += f"📦 Товаров: {total_products}\n"
    text += f"🛒 Заказов: {total_orders}\n"
    text += f"💰 Продаж: {total_sales}₽"
    await message.answer(text)

@dp.message(F.text == "➕ Добавить товар")
async def cmd_add(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AddProduct.name)
    await message.answer("Введите название товара:", reply_markup=get_cancel_keyboard())

# ==================== ОТМЕНА ====================
@dp.message(F.text == "❌ Отмена", StateFilter("*"))
async def cancel_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=get_main_keyboard())

# ==================== ДОБАВЛЕНИЕ ТОВАРА (FSM) ====================
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
    await bot.download_file(file.file_path, file_path)
    data = await state.get_data()
    conn = sqlite3.connect('shop.db')
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
    await message.answer(f"✅ Товар добавлен! ID: {product_id}", reply_markup=get_main_keyboard())

@dp.message(AddProduct.photo)
async def add_photo_invalid(message: Message):
    await message.answer("❌ Пожалуйста, отправьте фотографию.")

# ==================== ПРОСМОТР ТОВАРОВ ПО КАТЕГОРИЯМ ====================
async def list_products_by_category(cat: str, callback: CallbackQuery, page: int = 0):
    conn = sqlite3.connect('shop.db')
    c = conn.cursor()
    if cat == 'vape':
        # Для vape нужно показать подкатегории
        c.execute("SELECT DISTINCT subcategory FROM products WHERE category='vape' AND subcategory IS NOT NULL")
        subs = [row[0] for row in c.fetchall()]
        conn.close()
        if not subs:
            await callback.message.edit_text("В этой категории пока нет товаров.")
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=sub, callback_data=f"list_vape_{sub}_0")] for sub in subs
        ] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_categories")]])
        await callback.message.edit_text("Выберите подкатегорию:", reply_markup=kb)
        return
    else:
        # Обычные категории
        c.execute("SELECT id, name, price, discount FROM products WHERE category=? ORDER BY created_at DESC LIMIT 5 OFFSET ?", (cat, page*5))
        products = c.fetchall()
        # Получаем общее количество
        c.execute("SELECT COUNT(*) FROM products WHERE category=?", (cat,))
        total = c.fetchone()[0]
        conn.close()
        if not products:
            await callback.message.edit_text("В этой категории пока нет товаров.")
            return
        text = f"Товары в категории {cat} (страница {page+1}):\n\n"
        for p in products:
            final_price = p[3] if p[3] else p[2]
            text += f"ID: {p[0]} | {p[1]} | {final_price}₽\n"
        kb = []
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"list_{cat}_{page-1}"))
        if (page+1)*5 < total:
            nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"list_{cat}_{page+1}"))
        if nav:
            kb.append(nav)
        kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_categories")])
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(lambda c: c.data.startswith("list_"))
async def handle_list(callback: CallbackQuery):
    parts = callback.data.split('_')
    if parts[1] == 'vape' and len(parts) == 4:
        # list_vape_subcat_page
        sub = parts[2]
        page = int(parts[3])
        conn = sqlite3.connect('shop.db')
        c = conn.cursor()
        c.execute("SELECT id, name, price, discount FROM products WHERE category='vape' AND subcategory=? ORDER BY created_at DESC LIMIT 5 OFFSET ?", (sub, page*5))
        products = c.fetchall()
        c.execute("SELECT COUNT(*) FROM products WHERE category='vape' AND subcategory=?", (sub,))
        total = c.fetchone()[0]
        conn.close()
        if not products:
            await callback.message.edit_text("В этой подкатегории пока нет товаров.")
            return
        text = f"Товары в {sub} (страница {page+1}):\n\n"
        for p in products:
            final_price = p[3] if p[3] else p[2]
            text += f"ID: {p[0]} | {p[1]} | {final_price}₽\n"
        kb = []
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"list_vape_{sub}_{page-1}"))
        if (page+1)*5 < total:
            nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"list_vape_{sub}_{page+1}"))
        if nav:
            kb.append(nav)
        kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"list_vape")])
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    elif parts[1] in ['clothes', 'accessories', 'electronics']:
        cat = parts[1]
        page = int(parts[2]) if len(parts) > 2 else 0
        await list_products_by_category(cat, callback, page)
    elif parts[1] == 'vape' and len(parts) == 2:
        # Назад к подкатегориям
        conn = sqlite3.connect('shop.db')
        c = conn.cursor()
        c.execute("SELECT DISTINCT subcategory FROM products WHERE category='vape' AND subcategory IS NOT NULL")
        subs = [row[0] for row in c.fetchall()]
        conn.close()
        if not subs:
            await callback.message.edit_text("В этой категории пока нет товаров.")
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=sub, callback_data=f"list_vape_{sub}_0")] for sub in subs
        ] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_categories")]])
        await callback.message.edit_text("Выберите подкатегорию:", reply_markup=kb)

@dp.callback_query(lambda c: c.data == "back_to_categories")
async def back_to_categories(callback: CallbackQuery):
    await show_products_menu(callback.message)

@dp.callback_query(lambda c: c.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    await callback.message.delete()
    await cmd_start(callback.message)

# ==================== РЕДАКТИРОВАНИЕ И УДАЛЕНИЕ ТОВАРОВ ====================
@dp.message(F.text.startswith("/edit"))
async def cmd_edit(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Укажите ID товара. Например: /edit 5")
        return
    try:
        product_id = int(args[1])
    except ValueError:
        await message.answer("ID должен быть числом")
        return
    conn = sqlite3.connect('shop.db')
    c = conn.cursor()
    c.execute("SELECT name, price, category, subcategory, discount, is_new, image FROM products WHERE id=?", (product_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        await message.answer(f"Товар с ID {product_id} не найден")
        return
    text = f"Редактирование товара ID {product_id}:\n"
    text += f"Название: {row[0]}\nЦена: {row[1]}₽\nКатегория: {row[2]}\n"
    if row[2] == 'vape':
        text += f"Подкатегория: {row[3]}\n"
    text += f"Скидка: {row[4]}%\nНовинка: {'да' if row[5] else 'нет'}\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Название", callback_data=f"edit_{product_id}_name")],
        [InlineKeyboardButton(text="💰 Цена", callback_data=f"edit_{product_id}_price")],
        [InlineKeyboardButton(text="📁 Категория", callback_data=f"edit_{product_id}_category")],
        [InlineKeyboardButton(text="🏷 Скидка", callback_data=f"edit_{product_id}_discount")],
        [InlineKeyboardButton(text="🆕 Новинка", callback_data=f"edit_{product_id}_isnew")],
        [InlineKeyboardButton(text="🖼 Фото", callback_data=f"edit_{product_id}_photo")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_{product_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
    ])
    await message.answer(text, reply_markup=kb)

# Удаление с подтверждением
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
    conn = sqlite3.connect('shop.db')
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

# Редактирование полей
@dp.callback_query(lambda c: c.data.startswith("edit_"))
async def edit_product_field(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split('_')
    product_id = int(parts[1])
    field = parts[2]
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
        # переключение
        conn = sqlite3.connect('shop.db')
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
    conn = sqlite3.connect('shop.db')
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
    await message.answer("✅ Поле обновлено.", reply_markup=get_main_keyboard())

@dp.message(EditProduct.new_value, F.photo)
async def edit_photo(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    product_id = data['edit_id']
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    ext = file.file_path.split('.')[-1] if '.' in file.file_path else 'jpg'
    filename = f"{uuid.uuid4().hex}.{ext}"
    file_path = f"static/uploaded/{filename}"
    await bot.download_file(file.file_path, file_path)
    conn = sqlite3.connect('shop.db')
    c = conn.cursor()
    c.execute("UPDATE products SET image=? WHERE id=?", (f"/static/uploaded/{filename}", product_id))
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer("✅ Фото обновлено.", reply_markup=get_main_keyboard())

@dp.message(EditProduct.new_value)
async def edit_invalid(message: Message):
    await message.answer("❌ Ожидался текст или фото. Попробуйте ещё раз.")

# ==================== ОТМЕТКА ЗАКАЗА ВЫПОЛНЕННЫМ ====================
@dp.callback_query(lambda c: c.data.startswith("order_done_"))
async def order_done(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect('shop.db')
    c = conn.cursor()
    c.execute("UPDATE orders SET status='done' WHERE id=?", (order_id,))
    conn.commit()
    conn.close()
    await callback.message.edit_text(f"✅ Заказ #{order_id} отмечен как выполненный.")
    await callback.answer()

# ==================== API ДЛЯ МИНИ-ПРИЛОЖЕНИЯ ====================
app = FastAPI()

os.makedirs("static/uploaded", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

class OrderItem(BaseModel):
    id: str
    name: str
    quantity: int
    price: int

class OrderData(BaseModel):
    user: str
    items: List[OrderItem]
    total: int

@app.get("/api/products")
async def get_products():
    conn = sqlite3.connect('shop.db')
    c = conn.cursor()
    c.execute("SELECT * FROM products")
    rows = c.fetchall()
    conn.close()
    products = {}
    for row in rows:
        cat = row[3]
        sub = row[4]
        image = row[7]
        full_image_url = f"{BASE_URL}{image}" if image else None
        product = {
            "id": f"p{row[0]}",
            "name": row[1],
            "price": row[2],
            "discount": row[5],
            "isNew": bool(row[6]),
            "img": full_image_url or "images/tovary/default.jpg"
        }
        if cat == "vape":
            if sub not in products.get("vape", {}):
                products.setdefault("vape", {})[sub] = []
            products["vape"][sub].append(product)
        else:
            products.setdefault(cat, []).append(product)
    return products

@app.post("/api/order")
async def create_order(order: OrderData):
    conn = sqlite3.connect('shop.db')
    c = conn.cursor()
    c.execute('''INSERT INTO orders 
                 (user_id, user_name, items, total, created_at)
                 VALUES (?, ?, ?, ?, ?)''',
              (order.user, order.user, json.dumps([i.dict() for i in order.items]), 
               order.total, datetime.now()))
    order_id = c.lastrowid
    conn.commit()
    conn.close()
    # Уведомление администраторам
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, f"✅ Новый заказ #{order_id} на сумму {order.total}₽")
        except:
            pass
    return {"status": "ok", "order_id": order_id}

@app.post("/webhook")
async def webhook(request: Request):
    update = types.Update(**await request.json())
    await dp.feed_update(bot, update)
    return {"ok": True}

# ==================== ЗАПУСК ====================
async def on_startup():
    await bot.set_webhook(f"{WEBHOOK_URL}/webhook")

async def on_shutdown():
    await bot.delete_webhook()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)


