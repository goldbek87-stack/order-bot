import asyncio
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
WEBHOOK_HOST = os.environ["WEBHOOK_HOST"]
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = WEBHOOK_HOST + WEBHOOK_PATH
PORT = int(os.environ.get("PORT", 10000))

# BOT_MODE: "ads" — bu bot faqat reklama uchun, hamma mijozga narx ko'rinadi.
#           "direct" (yoki qo'yilmasa) — bu bot kelishilgan mijozlar uchun, narx ko'rinmaydi.
BOT_MODE = os.environ.get("BOT_MODE", "direct")

DB_PATH = "bot.db"

# ---------- XIZMATLAR VA NARXLAR (faqat reklama mijozlariga ko'rinadi) ----------
# "tiers": [(chegara, narx), ...] — miqdor shu chegaradan kichik/teng bo'lsa shu narx.
# Oxirgi qatorda chegara "None" bo'lishi kerak. Bitta narx bo'lsa: [(None, narx)]
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
PRINT_CATEGORY = "Fayl pechat qilish"
CUSTOM_CATEGORIES = {"Referat / Mustaqil ishi tayyorlash", "Taqdimot tayyorlash"}


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
    # admin chatidagi xabar ID'sini mijoz ID'siga bog'lab turadi — admin "reply" qilganda
    # bot qaysi mijozga javob yuborishni shundan biladi.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS relay (
            admin_msg_id INTEGER PRIMARY KEY,
            customer_id INTEGER,
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
        if BOT_MODE == "ads":
            # Bu — faqat reklama uchun bot, har bir mijozga narx ko'rsatiladi
            price_visible = 1
            source = source_param or "ads_direct"
        else:
            # Bu — kelishilgan mijozlar uchun bot, narx ko'rsatilmaydi
            price_visible = 1 if source_param and source_param.startswith("ads") else 0
            source = source_param or "direct"
        conn.execute(
            "INSERT INTO users (telegram_id, name, username, price_visible, source, created_at) VALUES (?,?,?,?,?,?)",
            (telegram_id, name, username, price_visible, source, datetime.now().isoformat()),
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


def save_relay(admin_msg_id: int, customer_id: int):
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO relay (admin_msg_id, customer_id, created_at) VALUES (?,?,?)",
        (admin_msg_id, customer_id, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_all_user_ids() -> list[int]:
    conn = db()
    rows = conn.execute("SELECT telegram_id FROM users").fetchall()
    conn.close()
    return [row["telegram_id"] for row in rows]


def get_relay_customer(admin_msg_id: int) -> int | None:
    conn = db()
    row = conn.execute("SELECT customer_id FROM relay WHERE admin_msg_id=?", (admin_msg_id,)).fetchone()
    conn.close()
    return row["customer_id"] if row else None


# ---------- Bot ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# Faqat "Narxlar" tugmasi orqali kirgan (reklama) mijozlar uchun holatlar:
carts: dict[int, dict[int, int]] = {}
awaiting_qty: dict[int, int] = {}
awaiting_details: set[int] = set()
awaiting_file: set[int] = set()
awaiting_broadcast = False


def start_keyboard(price_visible: bool) -> InlineKeyboardMarkup | None:
    if not price_visible:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💰 Narxlar", callback_data="prices")]]
    )


def categories_keyboard() -> InlineKeyboardMarkup:
    kb = []
    for idx, category in enumerate(get_categories()):
        kb.append([InlineKeyboardButton(text=category, callback_data=f"cat:{idx}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def items_keyboard(cat_idx: int) -> InlineKeyboardMarkup:
    category = get_categories()[cat_idx]
    kb = []
    for p in PRODUCTS:
        if p["category"] != category:
            continue
        kb.append([InlineKeyboardButton(text=p["name"], callback_data=f"add:{p['id']}")])
    kb.append([InlineKeyboardButton(text="⬅️ Bo'limlarga qaytish", callback_data="prices")])
    kb.append([InlineKeyboardButton(text="🛒 Savatni ko'rish", callback_data="cart")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def relay_to_admin(message: Message):
    """Mijozning istalgan xabari/faylini adminga yuboradi. Admin shu xabarga
    Telegram'ning 'Reply' funksiyasi orqali javob qaytarsa, bot buni avtomatik
    o'sha mijozga jo'natadi."""
    customer_id = message.from_user.id
    user = get_or_create_user(customer_id, message.from_user.full_name, message.from_user.username, None)
    contact = f"@{user['username']}" if user["username"] else "username yo'q"
    kind = "Reklama mijozi" if user["price_visible"] else "Kelishilgan mijoz"
    header = f"👤 {user['name']} ({contact})\nID: {customer_id} | {kind} | Manba: {user['source']}"

    info_msg = await bot.send_message(ADMIN_CHAT_ID, header)
    forwarded = await bot.copy_message(chat_id=ADMIN_CHAT_ID, from_chat_id=customer_id, message_id=message.message_id)

    save_relay(info_msg.message_id, customer_id)
    save_relay(forwarded.message_id, customer_id)


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
    price_visible = bool(user["price_visible"])

    await message.answer(
        "Assalomu alaykum! 👋\n\n"
        "Kerakli faylingizni yoki xabaringizni shu yerga yozing/yuboring — "
        "biz tez orada siz bilan bog'lanamiz.",
        reply_markup=start_keyboard(price_visible),
    )


@router.callback_query(F.data == "prices")
async def show_prices(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user or not bool(user["price_visible"]):
        await callback.answer("Bu bo'lim mavjud emas.", show_alert=True)
        return
    await callback.message.answer("Kerakli bo'limni tanlang:", reply_markup=categories_keyboard())
    await callback.answer()


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


@router.callback_query(F.data == "cart")
async def show_cart(callback: CallbackQuery):
    telegram_id = callback.from_user.id
    cart = carts.get(telegram_id, {})
    if not cart:
        await callback.message.answer("Savatingiz bo'sh.")
        await callback.answer()
        return

    lines = []
    total = 0
    for pid, qty in cart.items():
        product = PRODUCTS_BY_ID[pid]
        subtotal = calc_subtotal(product, qty)
        total += subtotal
        lines.append(f"{product['name']} — {qty} {product['unit']} — {fmt_price(subtotal)}")

    text = "🛒 Sizning buyurtmangiz:\n" + "\n".join(lines) + f"\n\nJami: {fmt_price(total)}"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Buyurtmani tasdiqlash", callback_data="confirm")],
            [InlineKeyboardButton(text="⬅️ Bo'limlarga qaytish", callback_data="prices")],
        ]
    )
    await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "confirm")
async def confirm_order(callback: CallbackQuery):
    telegram_id = callback.from_user.id
    cart = carts.get(telegram_id, {})
    if not cart:
        await callback.answer("Savat bo'sh")
        return

    user = get_user(telegram_id)
    contact = f"@{user['username']}" if user["username"] else "username yo'q"

    lines = []
    total = 0
    for pid, qty in cart.items():
        product = PRODUCTS_BY_ID[pid]
        subtotal = calc_subtotal(product, qty)
        total += subtotal
        lines.append(f"{product['name']} — {qty} {product['unit']} — {fmt_price(subtotal)}")

    items_text = "\n".join(lines)
    save_order(telegram_id, items_text, total)

    needs_file = any(PRODUCTS_BY_ID[pid]["category"] == PRINT_CATEGORY for pid in cart)
    needs_details = any(PRODUCTS_BY_ID[pid]["category"] in CUSTOM_CATEGORIES for pid in cart)

    admin_text = (
        "🆕 Yangi buyurtma (Narxlar orqali)!\n\n"
        f"Mijoz: {user['name']} ({contact})\n"
        f"ID: {telegram_id}\n\n"
        f"{items_text}\n\nJami: {fmt_price(total)}"
    )
    admin_msg = await bot.send_message(ADMIN_CHAT_ID, admin_text)
    save_relay(admin_msg.message_id, telegram_id)

    await callback.message.answer("Buyurtmangiz qabul qilindi! ✅")

    if needs_details:
        await callback.message.answer(
            "📝 Iltimos, quyidagi ma'lumotlarni bitta xabarda yozib yuboring:\n\n"
            "1) Mavzu nomi\n"
            "2) Til (o'zbek / rus / ingliz)\n"
            "3) Muddat (qachongacha kerak)\n"
            "4) Qo'shimcha talablar (agar bo'lsa)"
        )
        awaiting_details.add(telegram_id)

    if needs_file:
        await callback.message.answer(
            "📎 Endi chop etish uchun kerakli faylingizni (PDF, Word va h.k.) shu yerga yuboring."
        )
        awaiting_file.add(telegram_id)

    if not needs_file and not needs_details:
        await callback.message.answer("Tez orada siz bilan bog'lanamiz.")

    carts[telegram_id] = {}
    await callback.answer()


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


@router.message(Command("broadcast"))
async def broadcast_cmd(message: Message):
    global awaiting_broadcast
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    awaiting_broadcast = True
    await message.answer(
        "📢 Yuboriladigan xabarni yozing (matn, rasm yoki fayl bo'lishi mumkin) — "
        "keyingi xabaringiz botga murojaat qilgan BARCHA odamlarga jo'natiladi.\n\n"
        "Bekor qilish uchun /cancel yozing."
    )


@router.message(Command("cancel"))
async def cancel_cmd(message: Message):
    global awaiting_broadcast
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    awaiting_broadcast = False
    await message.answer("Bekor qilindi.")


@router.message(F.text.startswith("/"))
async def ignore_unknown_commands(message: Message):
    return  # tanilmagan buyruqlarni e'tiborsiz qoldiramiz


@router.message(F.from_user.id == ADMIN_CHAT_ID, F.reply_to_message)
async def admin_reply(message: Message):
    customer_id = get_relay_customer(message.reply_to_message.message_id)
    if customer_id is None:
        await message.answer("⚠️ Bu xabar mijozga bog'lanmagan — asl xabarga to'g'ridan-to'g'ri reply qiling.")
        return
    await bot.copy_message(chat_id=customer_id, from_chat_id=ADMIN_CHAT_ID, message_id=message.message_id)


@router.message(F.from_user.id == ADMIN_CHAT_ID)
async def admin_broadcast_or_ignore(message: Message):
    global awaiting_broadcast
    if not awaiting_broadcast:
        return  # oddiy (reply bo'lmagan) xabar — e'tiborsiz qoldiramiz

    awaiting_broadcast = False
    user_ids = get_all_user_ids()
    sent = 0
    failed = 0
    for uid in user_ids:
        if uid == ADMIN_CHAT_ID:
            continue
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=ADMIN_CHAT_ID, message_id=message.message_id)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await message.answer(f"📢 Yuborildi: {sent} ta foydalanuvchiga. Xato: {failed} ta (bloklangan/o'chirilgan).")


@router.message(F.text)
async def handle_text(message: Message):
    telegram_id = message.from_user.id
    text = message.text.strip()

    # --- Miqdor kutilayotgan bo'lsa (faqat "Narxlar" flow'ida) ---
    if telegram_id in awaiting_qty and text.isdigit():
        qty = int(text)
        if qty <= 0:
            await message.answer("Miqdor 0 dan katta bo'lishi kerak. Qaytadan yozing:")
            return

        product_id = awaiting_qty.pop(telegram_id)
        product = PRODUCTS_BY_ID[product_id]

        cart = carts.setdefault(telegram_id, {})
        cart[product_id] = cart.get(product_id, 0) + qty

        total_qty = cart[product_id]
        subtotal = calc_subtotal(product, total_qty)

        reply = (
            f"✅ Qo'shildi: {product['name']} — {qty} {product['unit']}\n"
            f"Savatingizda jami: {total_qty} {product['unit']} — {fmt_price(subtotal)}"
        )
        await message.answer(reply, reply_markup=categories_keyboard())
        return

    # --- Mavzu/talablar matni kutilayotgan bo'lsa ---
    if telegram_id in awaiting_details:
        user = get_user(telegram_id)
        contact = f"@{user['username']}" if user["username"] else "username yo'q"
        admin_text = f"📝 Mavzu/talablar — {user['name']} ({contact}), ID: {telegram_id}\n\n{text}"
        await bot.send_message(ADMIN_CHAT_ID, admin_text)
        awaiting_details.discard(telegram_id)
        await message.answer("Ma'lumot qabul qilindi ✅ Tez orada siz bilan bog'lanamiz.")
        return

    # --- Boshqa barcha holatlarda: erkin xabar sifatida adminga yo'naltiramiz ---
    await relay_to_admin(message)


@router.message(F.document | F.photo)
async def receive_file(message: Message):
    telegram_id = message.from_user.id

    if telegram_id in awaiting_file:
        user = get_user(telegram_id)
        contact = f"@{user['username']}" if user["username"] else "username yo'q"
        caption = f"📎 Fayl — {user['name']} ({contact}), ID: {telegram_id}"
        if message.document:
            await bot.send_document(ADMIN_CHAT_ID, message.document.file_id, caption=caption)
        elif message.photo:
            await bot.send_photo(ADMIN_CHAT_ID, message.photo[-1].file_id, caption=caption)
        awaiting_file.discard(telegram_id)
        await message.answer("Fayl qabul qilindi ✅ Tez orada siz bilan bog'lanamiz.")
        return

    # Kutilmagan fayl — erkin xabar sifatida adminga yo'naltiramiz
    await relay_to_admin(message)


@router.message()
async def relay_other(message: Message):
    # Boshqa turdagi xabarlar (ovozli xabar, video va h.k.) uchun ham yo'naltirish
    await relay_to_admin(message)


# ---------- Webhook server (Render uchun) ----------
async def on_startup(app: web.Application):
    await bot.set_webhook(WEBHOOK_URL)


async def health(request: web.Request):
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
