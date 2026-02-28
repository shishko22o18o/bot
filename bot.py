import asyncio
import json
import logging
import os
import aiofiles
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from fastapi import FastAPI, Request, File, UploadFile, Form
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
from pydantic import BaseModel
from typing import List, Optional
import sqlite3
from datetime import datetime
import uuid

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = "8713317147:AAESC0L-U3cY9Ga6ta2w_s7TaV6WSeCVZ-k"          # Замени на свой
ADMIN_ID = > 5896826944               # Твой Telegram ID
WEBHOOK_URL = "https://твой-сервер.com" # URL твоего сервера
BASE_URL = WEBHOOK_URL

# ==================== БАЗА ДАННЫХ ====================
def init_db():
    conn = sqlite3.connect('shop.db')
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

# ==================== FSM ====================
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)

class AddProduct(StatesGroup):
    name = State()
    price = State()
    category = State()
    subcategory = State()
    discount = State()
    is_new = State()
    photo = State()

# ==================== СТАРТ ====================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("👋 Привет! Я бот-администратор магазина BAU28SHOP.\n"
                         "Команды:\n"
                         "/add — добавить товар с фото\n"
                         "/products — список последних товаров\n"
                         "/delete <id> — удалить товар (с подтверждением)\n"
                         "/deleteforce <id> — удалить товар мгновенно\n"
                         "/orders — новые заказы")

# ==================== ДОБАВЛЕНИЕ ТОВАРА ====================
@dp.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Доступ запрещён")
        return
    await state.set_state(AddProduct.name)
    await message.answer("Введите название товара:")

@dp.message(AddProduct.name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(AddProduct.price)
    await message.answer("Введите цену (только число):")

@dp.message(AddProduct.price)
async def process_price(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Цена должна быть числом. Попробуйте ещё раз:")
        return
    await state.update_data(price=int(message.text))
    await state.set_state(AddProduct.category)
    await message.answer("Введите категорию (clothes, accessories, vape, electronics):")

@dp.message(AddProduct.category)
async def process_category(message: Message, state: FSMContext):
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
async def process_subcategory(message: Message, state: FSMContext):
    sub = message.text.lower()
    if sub not in ['liquids', 'consumables', 'disposable', 'pods']:
        await message.answer("❌ Неверная подкатегория. Допустимы: liquids, consumables, disposable, pods")
        return
    await state.update_data(subcategory=sub)
    await state.set_state(AddProduct.discount)
    await message.answer("Введите скидку в процентах (0 если нет):")

@dp.message(AddProduct.discount)
async def process_discount(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Скидка должна быть числом. Попробуйте ещё раз:")
        return
    await state.update_data(discount=int(message.text))
    await state.set_state(AddProduct.is_new)
    await message.answer("Это новинка? (да/нет):")

@dp.message(AddProduct.is_new)
async def process_is_new(message: Message, state: FSMContext):
    text = message.text.lower()
    if text not in ['да', 'нет', 'yes', 'no']:
        await message.answer("❌ Ответьте 'да' или 'нет'")
        return
    is_new = 1 if text in ['да', 'yes'] else 0
    await state.update_data(is_new=is_new)
    await state.set_state(AddProduct.photo)
    await message.answer("Теперь отправьте фотографию товара:")

@dp.message(AddProduct.photo, F.photo)
async def process_photo(message: Message, state: FSMContext, bot: Bot):
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
    await message.answer(f"✅ Товар добавлен! ID: {product_id}")
    await state.clear()

@dp.message(AddProduct.photo)
async def process_photo_invalid(message: Message):
    await message.answer("❌ Пожалуйста, отправьте фотографию.")

# ==================== СПИСОК ТОВАРОВ ====================
@dp.message(Command("products"))
async def cmd_products(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    conn = sqlite3.connect('shop.db')
    c = conn.cursor()
    c.execute("SELECT id, name, price, category FROM products ORDER BY created_at DESC LIMIT 10")
    products = c.fetchall()
    conn.close()
    if not products:
        await message.answer("Товаров пока нет")
        return
    text = "📦 Последние товары:\n\n"
    for p in products:
        text += f"ID: {p[0]} | {p[1]} | {p[2]}₽ | {p[3]}\n"
    await message.answer(text)

# ==================== УДАЛЕНИЕ С ПОДТВЕРЖДЕНИЕМ ====================
@dp.message(Command("delete"))
async def cmd_delete(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Доступ запрещён")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Укажите ID товара для удаления. Например: /delete 5")
        return
    try:
        product_id = int(args[1])
    except ValueError:
        await message.answer("ID должен быть числом")
        return
    
    # Получаем информацию о товаре
    conn = sqlite3.connect('shop.db')
    c = conn.cursor()
    c.execute("SELECT name FROM products WHERE id=?", (product_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        await message.answer(f"Товар с ID {product_id} не найден")
        return
    
    # Создаём клавиатуру с подтверждением
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"confirm_del_{product_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_del")
        ]
    ])
    await message.answer(f"Вы действительно хотите удалить товар \"{row[0]}\" (ID {product_id})?", reply_markup=keyboard)

@dp.callback_query(lambda c: c.data.startswith("confirm_del_"))
async def confirm_delete(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect('shop.db')
    c = conn.cursor()
    c.execute("DELETE FROM products WHERE id=?", (product_id,))
    conn.commit()
    conn.close()
    await callback.message.edit_text(f"✅ Товар с ID {product_id} удалён.")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "cancel_del")
async def cancel_delete(callback: CallbackQuery):
    await callback.message.edit_text("❌ Удаление отменено.")
    await callback.answer()

# ==================== МГНОВЕННОЕ УДАЛЕНИЕ ====================
@dp.message(Command("deleteforce"))
async def cmd_delete_force(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Доступ запрещён")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Укажите ID товара для удаления. Например: /deleteforce 5")
        return
    try:
        product_id = int(args[1])
    except ValueError:
        await message.answer("ID должен быть числом")
        return
    
    conn = sqlite3.connect('shop.db')
    c = conn.cursor()
    c.execute("SELECT name FROM products WHERE id=?", (product_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        await message.answer(f"Товар с ID {product_id} не найден")
        return
    c.execute("DELETE FROM products WHERE id=?", (product_id,))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Товар \"{row[0]}\" (ID {product_id}) мгновенно удалён.")

# ==================== ЗАКАЗЫ ====================
@dp.message(Command("orders"))
async def cmd_orders(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    conn = sqlite3.connect('shop.db')
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE status='new' ORDER BY created_at DESC")
    orders = c.fetchall()
    conn.close()
    if not orders:
        await message.answer("Новых заказов нет")
        return
    for o in orders:
        items = json.loads(o[3])
        text = f"🛒 Новый заказ #{o[0]}\n"
        text += f"Пользователь: {o[2]}\nТовары:\n"
        for item in items:
            text += f"  • {item['name']} x{item['quantity']} = {item['price']*item['quantity']}₽\n"
        text += f"ИТОГО: {o[4]}₽\nСтатус: {o[5]}\n"
        await message.answer(text)

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
    await bot.send_message(ADMIN_ID, f"✅ Новый заказ #{order_id} на сумму {order.total}₽")
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