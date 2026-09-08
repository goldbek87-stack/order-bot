import logging
import os
import sqlite3
from datetime import datetime

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO)

# ---------- Sozlamalar (Render'da Environment Variables sifatida beriladi) ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
WEBHOOK_HOST = os.environ["WEBHOOK_HOST"]  # masalan: https://sizning-botingiz.onrender.com
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = WEBHOOK_HOST + WEBHOOK_PATH
PORT = int(os.environ.get("PORT", 10000))

DB_PATH = "bot.db"

# ---------- MAHSULOTLAR RO'YXATI — o'zingiznikiga moslab o'zgartiring ----------
PRODUCTS = [
    {"id": 1, "name": "Mahsulot 1", "price": 150000},
    {"id": 2, "name": "Mahsulot 2", "price": 220000},
    {"id": 3, "name": "Mahsulot 3", "price": 90000},
]


def fmt_price(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " so'm"


# ---------- Baza ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            name TEXT,
            username TEXT,
            price_visible INTEGER DEFAULT 0,
            source TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            items TEXT,
            total INTEGER,
            created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def get_or_create_user(telegram_id: int, name: str, username: str | None, source_param: str | None):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
    if row is None:
        # Agar start parametri "ads" bilan boshlansa -> reklama mijozi -> narx ko'rinadi
        price_visible = 1 if source_param and source_param.startswith("ads") else 0
        conn.execute(
            "INSERT INTO users (telegram_id, name, username, price_visible, source, created_at) VALUES (?,?,?,?,?,?)",
            (telegram_id, name, username, price_visible, source_param or "direct", datetime.now().isoformat()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
    conn.close()
    return row


def get_user(telegram_id: int):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
    conn.close()
    return row


def set_price_visible(telegram_id: int, value: bool):
    conn = db()
    conn.execute("UPDATE users SET price_visible=? WHERE telegram_id=?", (1 if value else 0, telegram_id))
    conn.commit()
    conn.close()


def save_order(telegram_id: int, items_text: str, total: int):
    conn = db()
    conn.execute(
        "INSERT INTO orders (telegram_id, items, total, created_at) VALUES (?,?,?,?)",
        (telegram_id, items_text, total, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


# ---------- Bot ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# Foydalanuvchi savati vaqtinchalik xotirada saqlanadi (oddiy loyiha uchun yetarli)
carts: dict[int, dict[int, int]] = {}


def catalog_keyboard(price_visible: bool) -> InlineKeyboardMarkup:
    kb = []
    for p in PRODUCTS:
        label = p["name"]
        if price_visible:
            label += f" — {fmt_price(p['price'])}"
        kb.append([InlineKeyboardButton(text=label, callback_data=f"add:{p['id']}")])
    kb.append([InlineKeyboardButton(text="🛒 Savatni ko'rish", callback_data="cart")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


@router.message(CommandStart())
async def start_handler(message: Message, command: CommandObject):
    source_param = command.args  # masalan: ads_instagram
    user = get_or_create_user(
        message.from_user.id,
        message.from_user.full_name,
        message.from_user.username,
        source_param,
    )
    carts[message.from_user.id] = {}
    await message.answer(
        "Assalomu alaykum! Buyurtma berish uchun mahsulotni tanlang:",
        reply_markup=catalog_keyboard(bool(user["price_visible"])),
    )


@router.callback_query(F.data.startswith("add:"))
async def add_to_cart(callback: CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    cart = carts.setdefault(callback.from_user.id, {})
    cart[product_id] = cart.get(product_id, 0) + 1
    await callback.answer("Qo'shildi ✅")


@router.callback_query(F.data == "cart")
async def show_cart(callback: CallbackQuery):
    telegram_id = callback.from_user.id
    cart = carts.get(telegram_id, {})
    if not cart:
        await callback.message.answer("Savatingiz bo'sh.")
        await callback.answer()
        return

    user = get_user(telegram_id)
    price_visible = bool(user["price_visible"])

    lines = []
    total = 0
    for pid, qty in cart.items():
        product = next(p for p in PRODUCTS if p["id"] == pid)
        subtotal = product["price"] * qty
        total += subtotal
        if price_visible:
            lines.append(f"{product['name']} x{qty} — {fmt_price(subtotal)}")
        else:
            lines.append(f"{product['name']} x{qty}")

    text = "🛒 Sizning buyurtmangiz:\n" + "\n".join(lines)
    if price_visible:
        text += f"\n\nJami: {fmt_price(total)}"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Buyurtmani tasdiqlash", callback_data="confirm")],
            [InlineKeyboardButton(text="⬅️ Katalogga qaytish", callback_data="back")],
        ]
    )
    await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "back")
async def back_to_catalog(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    await callback.message.answer("Katalog:", reply_markup=catalog_keyboard(bool(user["price_visible"])))
    await callback.answer()


@router.callback_query(F.data == "confirm")
async def confirm_order(callback: CallbackQuery):
    telegram_id = callback.from_user.id
    cart = carts.get(telegram_id, {})
    if not cart:
        await callback.answer("Savat bo'sh")
        return

    user = get_user(telegram_id)
    price_visible = bool(user["price_visible"])

    lines = []
    total = 0
    for pid, qty in cart.items():
        product = next(p for p in PRODUCTS if p["id"] == pid)
        subtotal = product["price"] * qty
        total += subtotal
        lines.append(f"{product['name']} x{qty} — {fmt_price(subtotal)}")

    items_text = "\n".join(lines)
    save_order(telegram_id, items_text, total)

    await callback.message.answer("Buyurtmangiz qabul qilindi! Tez orada siz bilan bog'lanamiz.")

    contact = f"@{user['username']}" if user["username"] else "username yo'q"
    admin_text = (
        "🆕 Yangi buyurtma!\n\n"
        f"Mijoz: {user['name']} ({contact})\n"
        f"Telegram ID: {telegram_id}\n"
        f"Manba: {user['source']}\n\n"
        f"{items_text}\n"
    )
    if price_visible:
        admin_text += f"\nJami: {fmt_price(total)}"
    else:
        admin_text += "\n(Bu mijozga narx ko'rsatilmagan — narx kelishilgan)"

    await bot.send_message(ADMIN_CHAT_ID, admin_text)
    carts[telegram_id] = {}
    await callback.answer()


# ---------- Admin buyruqlari ----------
@router.message(Command("setprice"))
async def set_price_cmd(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    if not command.args:
        await message.answer("Foydalanish: /setprice <telegram_id> on|off")
        return
    parts = command.args.split()
    if len(parts) != 2 or parts[1] not in ("on", "off"):
        await message.answer("Foydalanish: /setprice <telegram_id> on|off")
        return
    target_id, value = parts
    set_price_visible(int(target_id), value == "on")
    await message.answer(f"{target_id} uchun narx ko'rsatish: {value}")


# ---------- Webhook server (Render uchun) ----------
async def on_startup(app: web.Application):
    await bot.set_webhook(WEBHOOK_URL)


async def health(request: web.Request):
    # UptimeRobot shu manzilga ping yuborib turadi, botni uxlab qolishdan saqlaydi
    return web.Response(text="OK")


def main():
    init_db()
    app = web.Application()
    app.router.add_get("/", health)
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
