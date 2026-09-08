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

# ---------- XIZMATLAR RO'YXATI ----------
# "tiers": [(chegara, narx), ...] — miqdor shu chegaradan kichik yoki teng bo'lsa shu narx qo'llanadi.
# Oxirgi qatorda chegara "None" bo'lishi kerak — bu "shundan yuqori miqdor" degani.
# Agar narx miqdordan qat'iy nazar bir xil bo'lsa, faqat bitta qator yozing: [(None, narx)]
PRODUCTS = [
    {
        "id": 1,
        "name": "Rangsiz chiqarish",
        "category": "Fayl pechat qilish",
        "unit": "varoq",
        "tiers": [(100, 500), (None, 300)],
    },
    {
        "id": 2,
        "name": "Rangli chiqarish (oddiy qog'oz)",
        "category": "Fayl pechat qilish",
        "unit": "varoq",
        "tiers": [(99, 1000), (None, 500)],
    },
    {
        "id": 3,
        "name": "Glyansiy qog'ozga chiqarish",
        "category": "Fayl pechat qilish",
        "unit": "varoq",
        "tiers": [(99, 2000), (None, 1500)],
    },
    {
        "id": 4,
        "name": "Kitob shaklida chiqarish (A5)",
        "category": "Fayl pechat qilish",
        "unit": "bet",
        "tiers": [(None, 100)],
    },
    {
        "id": 5,
        "name": "Referat / Mustaqil ishi tayyorlash",
        "category": "Referat / Mustaqil ishi tayyorlash",
        "unit": "bet",
        "tiers": [(None, 1000)],
    },
    {
        "id": 6,
        "name": "Kurs ishi tayyorlash",
        "category": "Referat / Mustaqil ishi tayyorlash",
        "unit": "bet",
        "tiers": [(None, 1000)],
    },
    {
        "id": 7,
        "name": "Taqdimot tayyorlash — rangli pechat bilan",
        "category": "Taqdimot tayyorlash",
        "unit": "bet",
        "tiers": [(None, 1500)],
    },
    {
        "id": 8,
        "name": "Taqdimot tayyorlash — rangsiz pechat bilan",
        "category": "Taqdimot tayyorlash",
        "unit": "bet",
        "tiers": [(None, 1000)],
    },
    {
        "id": 9,
        "name": "Taqdimot tayyorlash — faqat fayl (pechatsiz)",
        "category": "Taqdimot tayyorlash",
        "unit": "dona",
        "tiers": [(None, 10000)],
    },
]

PRODUCTS_BY_ID = {p["id"]: p for p in PRODUCTS}


def get_categories() -> list[str]:
    seen: list[str] = []
    for p in PRODUCTS:
        if p["category"] not in seen:
            seen.append(p["category"])
    return seen


def fmt_price(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " so'm"


def get_unit_price(product: dict, qty: int) -> int:
    for max_q, price in product["tiers"]:
        if max_q is None or qty <= max_q:
            return price
    return product["tiers"][-1][1]


def calc_subtotal(product: dict, qty: int) -> int:
    return get_unit_price(product, qty) * qty


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

# Foydalanuvchi savati: {telegram_id: {product_id: qty}}
carts: dict[int, dict[int, int]] = {}
# Miqdor kiritilishini kutayotgan foydalanuvchilar: {telegram_id: product_id}
awaiting_qty: dict[int, int] = {}


def catalog_keyboard() -> InlineKeyboardMarkup:
    kb = []
    for idx, category in enumerate(get_categories()):
        kb.append([InlineKeyboardButton(text=category, callback_data=f"cat:{idx}")])
    kb.append([InlineKeyboardButton(text="🛒 Savatni ko'rish", callback_data="cart")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def items_keyboard(cat_idx: int) -> InlineKeyboardMarkup:
    category = get_categories()[cat_idx]
    kb = []
    for p in PRODUCTS:
        if p["category"] != category:
            continue
        kb.append([InlineKeyboardButton(text=p["name"], callback_data=f"add:{p['id']}")])
    kb.append([InlineKeyboardButton(text="⬅️ Bo'limlarga qaytish", callback_data="back")])
    kb.append([InlineKeyboardButton(text="🛒 Savatni ko'rish", callback_data="cart")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


@router.message(CommandStart())
async def start_handler(message: Message, command: CommandObject):
    source_param = command.args  # masalan: ads_instagram
    get_or_create_user(
        message.from_user.id,
        message.from_user.full_name,
        message.from_user.username,
        source_param,
    )
    carts[message.from_user.id] = {}
    awaiting_qty.pop(message.from_user.id, None)
    await message.answer(
        "Assalomu alaykum! Kerakli bo'limni tanlang:",
        reply_markup=catalog_keyboard(),
    )


@router.callback_query(F.data.startswith("cat:"))
async def show_category(callback: CallbackQuery):
    cat_idx = int(callback.data.split(":")[1])
    await callback.message.answer("Xizmatni tanlang:", reply_markup=items_keyboard(cat_idx))
    await callback.answer()


@router.callback_query(F.data.startswith("add:"))
async def ask_quantity(callback: CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    product = PRODUCTS_BY_ID[product_id]
    awaiting_qty[callback.from_user.id] = product_id
    await callback.message.answer(
        f"«{product['name']}» — necha {product['unit']} kerak? Raqamda yozing (masalan: 20):"
    )
    await callback.answer()


@router.message(F.text.regexp(r"^\d+$"))
async def receive_quantity(message: Message):
    telegram_id = message.from_user.id
    if telegram_id not in awaiting_qty:
        return  # oddiy raqamli xabar, biz kutmayotgan holat

    qty = int(message.text)
    if qty <= 0:
        await message.answer("Miqdor 0 dan katta bo'lishi kerak. Qaytadan yozing:")
        return

    product_id = awaiting_qty.pop(telegram_id)
    product = PRODUCTS_BY_ID[product_id]

    cart = carts.setdefault(telegram_id, {})
    cart[product_id] = cart.get(product_id, 0) + qty

    total_qty = cart[product_id]
    subtotal = calc_subtotal(product, total_qty)

    user = get_user(telegram_id)
    price_visible = bool(user["price_visible"])

    if price_visible:
        text = (
            f"✅ Qo'shildi: {product['name']} — {qty} {product['unit']}\n"
            f"Savatingizda jami: {total_qty} {product['unit']} — {fmt_price(subtotal)}"
        )
    else:
        text = f"✅ Qo'shildi: {product['name']} — {qty} {product['unit']}"

    await message.answer(text, reply_markup=catalog_keyboard())


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
        product = PRODUCTS_BY_ID[pid]
        subtotal = calc_subtotal(product, qty)
        total += subtotal
        if price_visible:
            lines.append(f"{product['name']} — {qty} {product['unit']} — {fmt_price(subtotal)}")
        else:
            lines.append(f"{product['name']} — {qty} {product['unit']}")

    text = "🛒 Sizning buyurtmangiz:\n" + "\n".join(lines)
    if price_visible:
        text += f"\n\nJami: {fmt_price(total)}"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Buyurtmani tasdiqlash", callback_data="confirm")],
            [InlineKeyboardButton(text="⬅️ Bo'limlarga qaytish", callback_data="back")],
        ]
    )
    await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "back")
async def back_to_catalog(callback: CallbackQuery):
    await callback.message.answer("Bo'limlar:", reply_markup=catalog_keyboard())
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
        product = PRODUCTS_BY_ID[pid]
        subtotal = calc_subtotal(product, qty)
        total += subtotal
        lines.append(f"{product['name']} — {qty} {product['unit']} — {fmt_price(subtotal)}")

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
