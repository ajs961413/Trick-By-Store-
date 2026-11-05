import asyncio
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import html
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
import urllib.parse
import httpx

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ----------------- CONFIGURATION ----------------- #
BOT_TOKEN = os.getenv("BOT_TOKEN", "8655496659:AAEEV-VbJDQpXfVUhHQWvzedzeM0BHL9RGM")
BOT_NAME = "Trick By Store"

ADMIN_IDS = [1136742030]

CHANNEL_USERNAME = "@trickbylo0t"
CHANNEL_URL = "https://t.me/trickbylo0t"
SUPPORT_USERNAME = "TrickBy_Support"

CURRENCY_SYMBOL = "₹"
DB_PATH = "shop_bot.db"

MIN_DEPOSIT_INR = 1.0
MIN_DEPOSIT_USDT = 0.05
USDT_RATE_INR = 100.0

MERCHANT_VPA = "malprakash1@bpunity"

SMB_API_KEY = os.getenv("SMB_API_KEY", "pi_live_85218ea4129818ab0da87038fe7162cd54cbc052e555103c")
SMB_API_SECRET = os.getenv("SMB_API_SECRET", "sk_live_d9037d775a6ffeb892f6798f0ce6ebfcafe3f628ff54957c")
SMB_CREATE_ORDER_URL = "https://smbupi.com/api/create-order"
SMB_CHECK_STATUS_URL = "https://smbupi.com/api/check-status"

USDT_WALLET_ADDRESS = "TNtuYDZGzgayG74ZKXTMGZ7h1c9HwzenY6"

USER_LAST_CLICK = {}
COOLDOWN_SECONDS = 0.8
ACTIVE_PAYMENT_POLLS = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

bot_app_instance = None
loop_instance = None


# ----------------- DATABASE HELPERS ----------------- #
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance REAL DEFAULT 0.0,
            api_key TEXT,
            referred_by INTEGER DEFAULT NULL,
            is_referral_rewarded INTEGER DEFAULT 0,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("PRAGMA table_info(users)")
    cols = [row[1] for row in cursor.fetchall()]
    if "api_key" not in cols:
        cursor.execute("ALTER TABLE users ADD COLUMN api_key TEXT")
    if "referred_by" not in cols:
        cursor.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER DEFAULT NULL")
    if "is_referral_rewarded" not in cols:
        cursor.execute("ALTER TABLE users ADD COLUMN is_referral_rewarded INTEGER DEFAULT 0")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('referral_reward', '3.0')")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            product_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            description TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER,
            item_data TEXT NOT NULL,
            is_sold INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(product_id) REFERENCES products(product_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            product_id INTEGER,
            quantity INTEGER DEFAULT 1,
            delivered_data TEXT,
            price_paid REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processed_payments (
            reference_id TEXT PRIMARY KEY,
            user_id INTEGER,
            method TEXT,
            amount_credited REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("SELECT COUNT(*) FROM products")
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO products (product_id, name, price, description) VALUES (1, ?, ?, ?)",
            ("Indian Fresh Verified Gmail", 40.0, "Fresh Verified High Quality Indian Gmail Account"),
        )
        cursor.execute(
            "INSERT INTO products (product_id, name, price, description) VALUES (2, ?, ?, ?)",
            ("Gemini 18 Month Pro Link", 37.0, "Gemini Advanced 18 Months Direct Activation Link"),
        )
    conn.commit()
    conn.close()


def get_referral_reward() -> float:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = 'referral_reward'")
    row = c.fetchone()
    conn.close()
    return float(row[0]) if row else 3.0


def set_referral_reward(val: float):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('referral_reward', ?)", (str(val),))
    conn.commit()
    conn.close()


def register_user(user_id: int, username: str, referrer_id: int = None):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, referred_by FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if not row:
        ref = None
        if referrer_id and referrer_id != user_id:
            c.execute("SELECT user_id FROM users WHERE user_id = ?", (referrer_id,))
            if c.fetchone():
                ref = referrer_id
        c.execute(
            "INSERT INTO users (user_id, username, balance, referred_by, is_referral_rewarded) VALUES (?, ?, 0.0, ?, 0)",
            (user_id, username or "Anonymous", ref),
        )
    else:
        if username:
            c.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
    conn.commit()
    conn.close()


def try_reward_referrer(user_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT referred_by, is_referral_rewarded FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if not row or not row[0] or row[1] == 1:
        conn.close()
        return None, 0.0

    referrer_id = row[0]
    reward_amt = get_referral_reward()

    try:
        c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (reward_amt, referrer_id))
        c.execute("UPDATE users SET is_referral_rewarded = 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        return referrer_id, reward_amt
    except Exception as e:
        conn.rollback()
        conn.close()
        logger.error(f"Referral reward error: {e}")
        return None, 0.0


def get_referral_stats(user_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users WHERE referred_by = ? AND is_referral_rewarded = 1", (user_id,))
    completed = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE referred_by = ? AND is_referral_rewarded = 0", (user_id,))
    pending = c.fetchone()[0]
    conn.close()
    return completed, pending


def get_user_balance(user_id: int) -> float:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return float(row[0]) if row else 0.0


def add_user_balance(user_id: int, amount: float):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()


def get_or_create_api_key(user_id: int, regenerate: bool = False) -> str:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT api_key FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if row and row[0] and not regenerate:
        key = row[0]
    else:
        key = f"tbs_{secrets.token_hex(16)}"
        c.execute("UPDATE users SET api_key = ? WHERE user_id = ?", (key, user_id))
        conn.commit()
    conn.close()
    return key


def get_all_products():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT product_id, name, price, description FROM products ORDER BY product_id ASC")
    prods = c.fetchall()
    conn.close()
    return prods


def add_new_product(name: str, price: float, description: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO products (name, price, description) VALUES (?, ?, ?)", (name, price, description))
    conn.commit()
    pid = c.lastrowid
    conn.close()
    return pid


def update_product_price(product_id: int, new_price: float):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE products SET price = ? WHERE product_id = ?", (new_price, product_id))
    conn.commit()
    conn.close()


def delete_product_by_id(product_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM products WHERE product_id = ?", (product_id,))
    c.execute("DELETE FROM inventory WHERE product_id = ? AND is_sold = 0", (product_id,))
    conn.commit()
    conn.close()


def get_available_stock_count(product_id: int) -> int:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM inventory WHERE product_id = ? AND is_sold = 0", (product_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0


def add_single_item_stock(product_id: int, formatted_item: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO inventory (product_id, item_data) VALUES (?, ?)", (product_id, formatted_item))
    conn.commit()
    conn.close()


def clear_unsold_stock(product_id: int) -> int:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM inventory WHERE product_id = ? AND is_sold = 0", (product_id,))
    cnt = c.rowcount
    conn.commit()
    conn.close()
    return cnt


def get_product_by_id(product_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT product_id, name, price, description FROM products WHERE product_id = ?", (product_id,))
    row = c.fetchone()
    conn.close()
    return row


def get_admin_dashboard_stats():
    conn = get_db_connection()
    c = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")

    c.execute("SELECT COALESCE(SUM(price_paid), 0.0) FROM orders")
    t_rev = c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(price_paid), 0.0) FROM orders WHERE date(created_at) = date(?)", (today,))
    td_rev = c.fetchone()[0]

    c.execute("SELECT COALESCE(SUM(quantity), 0) FROM orders")
    t_sold = c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(quantity), 0) FROM orders WHERE date(created_at) = date(?)", (today,))
    td_sold = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM inventory WHERE is_sold = 0")
    total_stock = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM users")
    real_users = c.fetchone()[0]

    c.execute("SELECT COALESCE(SUM(amount_credited), 0.0) FROM processed_payments")
    total_credited = c.fetchone()[0]

    ref_reward = get_referral_reward()

    conn.close()
    return {
        "t_rev": t_rev,
        "td_rev": td_rev,
        "t_sold": t_sold,
        "td_sold": td_sold,
        "total_stock": total_stock,
        "users": real_users,
        "total_credited": total_credited,
        "today_date": today,
        "ref_reward": ref_reward,
    }


def purchase_with_balance(user_id: int, product_id: int, quantity: int = 1):
    if quantity <= 0:
        return False, "INVALID_QUANTITY"

    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("SELECT price, name FROM products WHERE product_id = ?", (product_id,))
        prod = c.fetchone()
        if not prod:
            conn.rollback()
            return False, "PROD_NOT_FOUND"

        price, name = prod[0], prod[1]
        total = round(price * quantity, 2)

        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        u = c.fetchone()
        cur_bal = u[0] if u else 0.0

        if cur_bal < total:
            conn.rollback()
            return False, f"INSUFFICIENT|{total}|{cur_bal}"

        c.execute(
            "SELECT id, item_data FROM inventory WHERE product_id = ? AND is_sold = 0 LIMIT ?",
            (product_id, quantity),
        )
        items = c.fetchall()
        if len(items) < quantity:
            conn.rollback()
            return False, f"OUT_OF_STOCK|{len(items)}"

        ids = [x[0] for x in items]
        raw = [x[1] for x in items]
        q_marks = ",".join("?" for _ in ids)

        c.execute(f"UPDATE inventory SET is_sold = 1 WHERE id IN ({q_marks})", ids)
        c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (total, user_id))
        c.execute(
            "INSERT INTO orders (user_id, product_id, quantity, delivered_data, price_paid) VALUES (?, ?, ?, ?, ?)",
            (user_id, product_id, quantity, "\n---ITEM---\n".join(raw), total),
        )
        order_id = c.lastrowid
        conn.commit()
        return True, (raw, order_id, total, name, quantity)
    except Exception as e:
        conn.rollback()
        logger.error(f"Purchase error: {e}")
        return False, "SERVER_ERROR"
    finally:
        conn.close()


def get_user_orders(user_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """SELECT o.order_id, p.name, o.quantity, o.price_paid, o.delivered_data, o.created_at 
           FROM orders o 
           JOIN products p ON o.product_id = p.product_id 
           WHERE o.user_id = ? 
           ORDER BY o.order_id DESC LIMIT 5""",
        (user_id,),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def format_credentials_card(raw_items):
    formatted_list = []
    for idx, item in enumerate(raw_items, 1):
        if "||" in item:
            acc, pwd = item.split("||", 1)
            card = (
                f"╭── 🔑 <b>ACCOUNT CREDENTIALS #{idx}</b>\n"
                f"├ 👤 <b>Username/Email:</b> <code>{acc.strip()}</code>\n"
                f"╰ 🔐 <b>Password/Secret:</b> <code>{pwd.strip()}</code>"
            )
        else:
            card = (
                f"╭── 🔑 <b>DATA TOKEN #{idx}</b>\n"
                f"╰ 📦 <b>Activation Data:</b> <code>{item.strip()}</code>"
            )
        formatted_list.append(card)
    return "\n\n".join(formatted_list)


def get_product_icon(name: str) -> str:
    name_lower = name.lower()
    if "gmail" in name_lower or "mail" in name_lower:
        return "📧"
    elif "gemini" in name_lower:
        return "♊"
    elif "chatgpt" in name_lower or "gpt" in name_lower:
        return "🤖"
    elif "youtube" in name_lower:
        return "▶️"
    elif "claude" in name_lower:
        return "🧠"
    elif "netflix" in name_lower:
        return "🍿"
    elif "prime" in name_lower:
        return "🎬"
    return "📦"


# ----------------- SMB UPI GATEWAY ----------------- #
async def create_smb_order(user_id: int, user_name: str, amount: float, currency: str = "INR", prod_id: int = None, qty: int = None):
    unique_order_id = f"TBS_{user_id}_{int(time.time())}_{secrets.token_hex(3)}"
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": SMB_API_KEY,
        "X-API-Secret": SMB_API_SECRET,
    }
    desc = f"Wallet Deposit {CURRENCY_SYMBOL}{amount}" if not prod_id else f"Purchase Prod #{prod_id}"
    payload = {
        "amount": round(amount, 2),
        "currency": currency,
        "provider": "binance" if currency == "USDT" else "auto",
        "order_id": unique_order_id,
        "customer_name": user_name or f"User_{user_id}",
        "description": desc,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(SMB_CREATE_ORDER_URL, headers=headers, json=payload)
            data = resp.json()
            if resp.status_code in [200, 201] and (data.get("status") in ["success", True] or data.get("success") is True):
                order_data = data.get("data", {})
                return True, unique_order_id, order_data
            else:
                err_msg = data.get("message", "Gateway response error")
                return False, unique_order_id, err_msg
    except Exception as e:
        logger.error(f"SMB Order Creation Failed: {e}")
        return False, unique_order_id, str(e)


async def check_smb_order_status(order_id: str):
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": SMB_API_KEY,
        "X-API-Secret": SMB_API_SECRET,
    }
    payload = {"order_id": order_id}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(SMB_CHECK_STATUS_URL, headers=headers, json=payload)
            data = resp.json()
            status_field = str(data.get("status", "")).lower()
            if resp.status_code == 200 and (status_field in ["success", "true"] or data.get("success") is True):
                res_data = data.get("data", {})
                return True, res_data
            return False, data.get("message", "Check failed")
    except Exception as e:
        logger.error(f"SMB Status Check Failed: {e}")
        return False, str(e)


def is_rate_limited(user_id: int) -> bool:
    now = time.time()
    last_time = USER_LAST_CLICK.get(user_id, 0)
    if now - last_time < COOLDOWN_SECONDS:
        return True
    USER_LAST_CLICK[user_id] = now
    return False


async def check_channel_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if user_id in ADMIN_IDS:
        return True
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.warning(f"Channel check error: {e}")
        return True


def get_force_join_card(user_first_name: str):
    text = (
        "👑 <b>TRICK BY STORE • VERIFICATION DESK</b>\n"
        "╭────────────────────────────╮\n"
        f"  Welcome, <b>{html.escape(user_first_name or 'Member')}</b>!\n"
        "  To access all store features,\n"
        "  please join our official channel.\n"
        "╰────────────────────────────╯\n\n"
        f"📢 <b>Official Channel:</b> {CHANNEL_USERNAME}\n\n"
        "<i>Join the channel below, then click 'Confirm Membership'.</i>"
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel Now", url=CHANNEL_URL)],
        [InlineKeyboardButton("⚡ Confirm Membership", callback_data="verify_channel_join")],
    ])
    return text, markup


# ----------------- PAYMENT WEBHOOK ----------------- #
def record_auto_gateway_payment(ref_id: str, user_id: int, amount: float):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT 1 FROM processed_payments WHERE reference_id = ?", (ref_id,))
    if c.fetchone():
        conn.close()
        return False, 0.0

    try:
        c.execute(
            "INSERT INTO processed_payments (reference_id, user_id, method, amount_credited) VALUES (?, ?, 'SMB_AUTO', ?)",
            (ref_id, user_id, amount),
        )
        c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        conn.commit()
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        new_bal = c.fetchone()[0]
        conn.close()
        return True, new_bal
    except Exception:
        conn.rollback()
        conn.close()
        return False, 0.0


async def send_auto_payment_alert(user_id: int, amount: float, new_bal: float, ref_id: str):
    if not bot_app_instance:
        return
    text = (
        "╭── 💎 <b>WALLET TOP-UP SUCCESS</b>\n"
        f"├ 💵 <b>Credited Amount:</b> <code>{CURRENCY_SYMBOL}{amount:.2f}</code>\n"
        f"├ 💼 <b>Current Balance:</b> <code>{CURRENCY_SYMBOL}{new_bal:.2f}</code>\n"
        f"╰ 🔖 <b>Txn Reference:</b> <code>{ref_id}</code>\n\n"
        "✨ <i>Your funds are ready! Tap below to start browsing.</i>"
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🛍️ Browse Products", callback_data="back_to_menu")]])
    try:
        await bot_app_instance.bot.send_message(chat_id=user_id, text=text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Alert error: {e}")


class ResellerApiAndPaymentServer(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "online", "service": "Trick By Store Reseller API"}).encode("utf-8"))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length)

        if self.path == "/api/v1/order":
            try:
                data = json.loads(raw_body.decode("utf-8"))
                api_key = str(data.get("api_key", "")).strip()
                prod_id = int(data.get("product_id", 0))
                qty = int(data.get("quantity", 1))

                if not api_key:
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'{"error":"Missing API Key"}')
                    return

                conn = get_db_connection()
                c = conn.cursor()
                c.execute("SELECT user_id FROM users WHERE api_key = ?", (api_key,))
                u_row = c.fetchone()
                conn.close()

                if not u_row:
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b'{"error":"Invalid API Key"}')
                    return

                user_id = u_row[0]
                ok, res = purchase_with_balance(user_id, prod_id, qty)
                if not ok:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": res}).encode("utf-8"))
                    return

                raw_items, o_id, total_paid, prod_name, _ = res
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                resp_payload = {
                    "success": True,
                    "order_id": o_id,
                    "product": prod_name,
                    "quantity": qty,
                    "total_paid": total_paid,
                    "delivered_items": raw_items,
                }
                self.wfile.write(json.dumps(resp_payload).encode("utf-8"))
                return
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
                return

        try:
            payload = json.loads(raw_body.decode("utf-8"))
            order_id = payload.get("order_id") or payload.get("data", {}).get("order_id", "")
            amt = float(payload.get("amount") or payload.get("data", {}).get("amount", 0.0))
            status = str(payload.get("status") or payload.get("data", {}).get("status", "")).lower()

            valid_statuses = ["success", "paid", "completed", "txn_success"]
            if "TBS_" in order_id and status in valid_statuses:
                u_id = int(order_id.split("_")[1])
                ok, new_bal = record_auto_gateway_payment(order_id, u_id, amt)
                if ok and loop_instance and loop_instance.is_running():
                    asyncio.run_coroutine_threadsafe(
                        send_auto_payment_alert(u_id, amt, new_bal, order_id),
                        loop_instance,
                    )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"acknowledged"}')
        except Exception:
            self.send_response(400)
            self.end_headers()

    def log_message(self, format, *args):
        return


def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), ResellerApiAndPaymentServer)
    server.serve_forever()


# ----------------- KEYBOARDS ----------------- #
def get_main_reply_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🛍️ Browse Products")],
            [KeyboardButton("💳 My Wallet"), KeyboardButton("🎁 Invite & Earn")],
            [KeyboardButton("📦 Order Archive"), KeyboardButton("⚡ Developer API")],
            [KeyboardButton("📞 Support Desk")],
        ],
        resize_keyboard=True,
    )


def get_products_inline_keyboard():
    prods = get_all_products()
    keyboard = []
    if not prods:
        keyboard.append([InlineKeyboardButton("⚠️ Catalog Currently Empty", callback_data="none")])
    else:
        for p in prods:
            pid, name, price, _ = p
            stock = get_available_stock_count(pid)
            icon = get_product_icon(name)

            if stock > 0:
                btn_text = f"{icon} {name} • {CURRENCY_SYMBOL}{price:.0f} [🟢 {stock} left]"
            else:
                btn_text = f"🔒 {name} • {CURRENCY_SYMBOL}{price:.0f} [🔴 Sold Out]"

            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"prod_{pid}")])

    return InlineKeyboardMarkup(keyboard)


def get_quantity_keyboard(prod_id: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚡ 1x", callback_data=f"selqty_{prod_id}_1"),
            InlineKeyboardButton("⚡ 2x", callback_data=f"selqty_{prod_id}_2"),
            InlineKeyboardButton("⚡ 5x", callback_data=f"selqty_{prod_id}_5"),
            InlineKeyboardButton("⚡ 10x", callback_data=f"selqty_{prod_id}_10"),
        ],
        [InlineKeyboardButton("🔢 Custom Count", callback_data=f"custom_qty_{prod_id}")],
        [InlineKeyboardButton("🔙 Back to Catalog", callback_data="back_to_menu")],
    ])


def get_wallet_inline_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Instant UPI Top-up (QR)", callback_data="choose_upi_deposit")],
        [InlineKeyboardButton("💎 USDT TRC-20 (Auto-Verify)", callback_data="choose_usdt_deposit")],
        [InlineKeyboardButton("🛍️ Browse Catalog", callback_data="back_to_menu")],
    ])


def get_upi_amount_selection_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("₹10", callback_data="upi_amt_10"),
            InlineKeyboardButton("₹50", callback_data="upi_amt_50"),
            InlineKeyboardButton("₹100", callback_data="upi_amt_100"),
        ],
        [
            InlineKeyboardButton("₹200", callback_data="upi_amt_200"),
            InlineKeyboardButton("₹500", callback_data="upi_amt_500"),
            InlineKeyboardButton("₹1000", callback_data="upi_amt_1000"),
        ],
        [InlineKeyboardButton("🔢 Custom INR Amount", callback_data="upi_amt_custom")],
        [InlineKeyboardButton("🔙 Back to Wallet", callback_data="cancel_payment_flow")],
    ])


def get_usdt_amount_selection_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("0.5 USDT", callback_data="usdt_amt_0.5"),
            InlineKeyboardButton("1 USDT", callback_data="usdt_amt_1"),
            InlineKeyboardButton("2 USDT", callback_data="usdt_amt_2"),
        ],
        [
            InlineKeyboardButton("5 USDT", callback_data="usdt_amt_5"),
            InlineKeyboardButton("10 USDT", callback_data="usdt_amt_10"),
            InlineKeyboardButton("25 USDT", callback_data="usdt_amt_25"),
        ],
        [InlineKeyboardButton("🔢 Custom USDT Amount", callback_data="usdt_amt_custom")],
        [InlineKeyboardButton("🔙 Back to Wallet", callback_data="cancel_payment_flow")],
    ])


def build_admin_dashboard_text_and_markup():
    s = get_admin_dashboard_stats()
    prods = get_all_products()
    stock_breakdown = ""
    for p in prods:
        c = get_available_stock_count(p[0])
        indicator = "🟢" if c > 0 else "🔴"
        stock_breakdown += f"{indicator} <b>{p[1]}:</b> <code>{c} units</code> (₹{p[2]:.2f})\n"

    text = (
        "👑 <b>TRICK BY STORE • MASTER ADMIN CONSOLE</b>\n"
        "╭────────────────────────────╮\n"
        f"  📅 <b>System Date:</b> <code>{s['today_date']}</code>\n"
        f"  👥 <b>Active Users:</b> <code>{s['users']}</code>\n"
        f"  🎁 <b>Referral Rate:</b> <code>₹{s['ref_reward']:.2f} / invite</code>\n"
        "╰────────────────────────────╯\n\n"
        "📦 <b>INVENTORY SUMMARY:</b>\n"
        f"{stock_breakdown or '• No active inventory items\n'}"
        f"💎 <b>Total Available Units:</b> <code>{s['total_stock']} units</code>\n\n"
        "📊 <b>SALES PERFORMANCE:</b>\n"
        f"• <b>Today Sold:</b> <code>{s['td_sold']} items</code> (₹{s['td_rev']:.2f})\n"
        f"• <b>Total Sold:</b> <code>{s['t_sold']} items</code> (₹{s['t_rev']:.2f})\n"
        f"• <b>Total Wallet Credited:</b> <code>₹{s['total_credited']:.2f}</code>"
    )
    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Add Product", callback_data="adm_create_product"),
            InlineKeyboardButton("🗑️ Remove Product", callback_data="adm_delete_product_menu"),
        ],
        [
            InlineKeyboardButton("💲 Update Price", callback_data="adm_update_price_menu"),
            InlineKeyboardButton("🎁 Set Referral Reward", callback_data="adm_set_refer_bonus"),
        ],
        [
            InlineKeyboardButton("📥 Add Stock (1-by-1)", callback_data="adm_stock_menu"),
            InlineKeyboardButton("🧹 Flush Stock", callback_data="adm_rm_menu"),
        ],
        [
            InlineKeyboardButton("💳 Credit User Balance", callback_data="adm_credit_user"),
            InlineKeyboardButton("🔄 Refresh Overview", callback_data="adm_refresh_stats"),
        ],
        [
            InlineKeyboardButton("❌ Exit Admin Panel", callback_data="adm_close"),
        ],
    ])
    return text, markup


async def send_or_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, markup: InlineKeyboardMarkup):
    if update.callback_query:
        q = update.callback_query
        try:
            if q.message.photo:
                await q.message.delete()
                await context.bot.send_message(chat_id=q.message.chat_id, text=text, reply_markup=markup, parse_mode="HTML")
            else:
                await q.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            await context.bot.send_message(chat_id=q.message.chat_id, text=text, reply_markup=markup, parse_mode="HTML")
    elif update.message:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")


async def render_wallet_view(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    bal = get_user_balance(user_id)
    text = (
        "💼 <b>PREMIUM STORE WALLET</b>\n"
        "╭────────────────────────────╮\n"
        f"  💵 <b>Available Balance:</b> <code>{CURRENCY_SYMBOL}{bal:.2f}</code>\n"
        f"  💎 <b>Equivalent Crypto:</b> <code>{bal/USDT_RATE_INR:.4f} USDT</code>\n"
        f"  💱 <b>System Rate:</b> <code>1 USDT = {CURRENCY_SYMBOL}{USDT_RATE_INR:.2f}</code>\n"
        "╰────────────────────────────╯\n\n"
        "⚡ <i>Auto-credit gateways enable instant fund verification 24/7.</i>"
    )
    await send_or_edit_menu(update, context, text, get_wallet_inline_keyboard())


async def render_refer_view(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    reward = get_referral_reward()
    completed, pending = get_referral_stats(user_id)
    bot_info = await context.bot.get_me()
    refer_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"

    text = (
        "🎁 <b>REFERRAL REWARDS PORTAL</b>\n"
        "╭────────────────────────────╮\n"
        f"  Reward per referral: <b>{CURRENCY_SYMBOL}{reward:.2f}</b>\n"
        "  Funds are instantly credited to your store wallet!\n"
        "╰────────────────────────────╯\n\n"
        "🔗 <b>Your Exclusive Link:</b>\n"
        f"<code>{refer_link}</code>\n\n"
        "📊 <b>Your Statistics:</b>\n"
        f"• Verified Referrals: <code>{completed} users</code>\n"
        f"• Total Rewards Earned: <code>{CURRENCY_SYMBOL}{completed * reward:.2f}</code>\n"
        f"• Pending Verification: <code>{pending} users</code>\n\n"
        "🛡️ <i>Bonus is automatically credited once your referee verifies channel membership.</i>"
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Share Referral Link", url=f"https://t.me/share/url?url={urllib.parse.quote(refer_link)}&text={urllib.parse.quote('Join Trick By Store for premium digital accounts and services!')}")],
        [InlineKeyboardButton("🔙 Back to Catalog", callback_data="back_to_menu")],
    ])
    await send_or_edit_menu(update, context, text, markup)


async def render_api_view(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, key: str = None):
    api_key = key or get_or_create_api_key(user_id)
    bal = get_user_balance(user_id)
    api_doc = (
        "⚡ <b>DEVELOPER & RESELLER API</b>\n"
        "╭────────────────────────────╮\n"
        f"  💼 <b>Reseller Balance:</b> <code>{CURRENCY_SYMBOL}{bal:.2f}</code>\n"
        "  Connect automated order fulfillment to your platform.\n"
        "╰────────────────────────────╯\n\n"
        f"🔑 <b>API Secret Key:</b>\n<code>{api_key}</code>\n\n"
        "📌 <b>Order Endpoint:</b>\n"
        "<code>POST /api/v1/order</code>\n\n"
        "<b>Headers:</b>\n"
        "<code>Content-Type: application/json</code>\n\n"
        "<b>JSON Payload:</b>\n"
        "<code>{\n"
        f'  "api_key": "{api_key}",\n'
        '  "product_id": 1,\n'
        '  "quantity": 1\n'
        "}</code>"
    )
    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💳 Add Balance", callback_data="add_funds_menu"),
            InlineKeyboardButton("🔄 Regenerate Key", callback_data="api_regenerate"),
        ],
        [InlineKeyboardButton("🔙 Back to Catalog", callback_data="back_to_menu")],
    ])
    await send_or_edit_menu(update, context, api_doc, markup)


async def render_support_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sup_text = (
        "📞 <b>24/7 PREMIUM SUPPORT DESK</b>\n"
        "╭────────────────────────────╮\n"
        "  Need help with deposits, balances, or orders?\n"
        "  Our dedicated support team is available 24/7.\n"
        "╰────────────────────────────╯\n\n"
        f"👤 <b>Live Support Handle:</b> @{SUPPORT_USERNAME}\n"
        f"📢 <b>Official Channel:</b> {CHANNEL_USERNAME}\n"
        "⏰ <b>Response Time:</b> Within minutes\n\n"
        "<i>Click the button below to start a direct chat with support.</i>"
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Live Chat With Support", url=f"https://t.me/{SUPPORT_USERNAME}")],
        [InlineKeyboardButton("📢 Official Channel", url=CHANNEL_URL)],
        [InlineKeyboardButton("🔙 Back to Catalog", callback_data="back_to_menu")],
    ])
    await send_or_edit_menu(update, context, sup_text, markup)


async def render_orders_view(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    ords = get_user_orders(user_id)
    if not ords:
        empty_text = (
            "📦 <b>ORDER ARCHIVE</b>\n"
            "╭────────────────────────────╮\n"
            "  No order history found on this account yet.\n"
            "╰────────────────────────────╯\n\n"
            "<i>Your purchased account data will remain securely saved here.</i>"
        )
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🛍️ Browse Products", callback_data="back_to_menu")]])
        await send_or_edit_menu(update, context, empty_text, markup)
        return

    out = "📦 <b>ORDER ARCHIVE (LAST 5 TRANSACTIONS)</b>\n╭────────────────────────────╮\n"
    for o_id, name, q, price, data, date in ords:
        items = data.split("\n---ITEM---\n")
        formatted = format_credentials_card(items)
        out += (
            f"🆔 <b>Order #{o_id}</b> | <code>{date[:16]}</code>\n"
            f"📦 <b>Item:</b> {name} (x{q})\n"
            f"💵 <b>Paid:</b> {CURRENCY_SYMBOL}{price:.2f}\n"
            f"{formatted}\n"
            "────────────────────────────\n"
        )
    out += "╰────────────────────────────╯\n💡 <i>Tap any credentials code block to copy.</i>"
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍️ Browse Products", callback_data="back_to_menu")],
        [InlineKeyboardButton("📞 Support Desk", url=f"https://t.me/{SUPPORT_USERNAME}")],
    ])
    await send_or_edit_menu(update, context, out, markup)


# ----------------- MENU COMMANDS ----------------- #
async def contact_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await render_support_view(update, context)


async def terms_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    terms_text = (
        "📜 <b>TERMS OF SERVICE & USAGE POLICY</b>\n"
        "╭────────────────────────────╮\n"
        "  General service guidelines for all customers:\n"
        "╰────────────────────────────╯\n\n"
        "<b>1. Account Security:</b> Upon delivery, customers must promptly log in and change all default passwords and recovery credentials.\n\n"
        "<b>2. Fair Use:</b> Any fraudulent activities, spamming, or misuse will result in an immediate service suspension without compensation.\n\n"
        "<b>3. Instant Delivery:</b> Products and licenses are delivered automatically by the server once payment confirmation is received.\n\n"
        "<b>4. Technical Issues:</b> For gateway delays or technical difficulties, please report your Order ID to our live support desk immediately."
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📞 Contact Desk", url=f"https://t.me/{SUPPORT_USERNAME}")],
        [InlineKeyboardButton("🔙 Back to Catalog", callback_data="back_to_menu")]
    ])
    await update.message.reply_text(terms_text, reply_markup=markup, parse_mode="HTML")


async def policy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    policy_text = (
        "🛡️ <b>PRIVACY & REPLACEMENT WARRANTY</b>\n"
        "╭────────────────────────────╮\n"
        "  Committed to customer protection and data security:\n"
        "╰────────────────────────────╯\n\n"
        "🔒 <b>Privacy Policy:</b>\n"
        "• Your Telegram ID and order logs are securely stored in our encrypted database and never shared with third parties.\n\n"
        "💎 <b>Warranty & Replacement:</b>\n"
        "• All items include an instant activation warranty.\n"
        "• If you encounter any invalid login or non-working item, submit proof/screenshots to support within 24 hours of delivery.\n"
        "• Verified issues will receive an instant replacement or store balance refund."
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Claim Warranty", url=f"https://t.me/{SUPPORT_USERNAME}")],
        [InlineKeyboardButton("🔙 Back to Catalog", callback_data="back_to_menu")]
    ])
    await update.message.reply_text(policy_text, reply_markup=markup, parse_mode="HTML")


# ----------------- AUTO-POLL BACKGROUND TASK ----------------- #
async def auto_poll_payment_status(order_id: str, user_id: int, amount: float, message_id: int, context: ContextTypes.DEFAULT_TYPE, prod_id: int = None, qty: int = None):
    poll_duration = 600
    interval = 4
    elapsed = 0
    valid_statuses = ["success", "paid", "completed", "txn_success"]

    while elapsed < poll_duration:
        if not ACTIVE_PAYMENT_POLLS.get(order_id, False):
            break

        await asyncio.sleep(interval)
        elapsed += interval

        ok, res_data = await check_smb_order_status(order_id)
        if ok:
            st = str(res_data.get("status", "")).lower()
            if st in valid_statuses:
                ACTIVE_PAYMENT_POLLS.pop(order_id, None)
                utr = res_data.get("utr") or res_data.get("bank_ref") or order_id

                if prod_id and qty:
                    rec_ok, _ = record_auto_gateway_payment(utr, user_id, amount)
                    if rec_ok:
                        p_ok, p_res = purchase_with_balance(user_id, prod_id, qty)
                        if p_ok:
                            raw, o_id, total, name, quantity = p_res
                            formatted = format_credentials_card(raw)
                            msg = (
                                "🎉 <b>ORDER COMPLETED SUCCESSFULLY!</b>\n"
                                "╭────────────────────────────╮\n"
                                f"  🆔 <b>Order ID:</b> <code>#{o_id}</code>\n"
                                f"  📦 <b>Product:</b> {name}\n"
                                f"  🔢 <b>Quantity:</b> <code>{quantity} unit(s)</code>\n"
                                f"  💵 <b>Amount Paid:</b> <code>{CURRENCY_SYMBOL}{total:.2f}</code>\n"
                                f"  🔖 <b>UTR / Ref:</b> <code>{utr}</code>\n"
                                "╰────────────────────────────╯\n\n"
                                f"🔐 <b>DELIVERED DATA:</b>\n\n{formatted}\n\n"
                                "💡 <i>Saved in Order Archive. Tap any block to copy.</i>"
                            )
                            markup = InlineKeyboardMarkup([[InlineKeyboardButton("🛍️ Browse More Products", callback_data="back_to_menu")]])
                            try:
                                await context.bot.delete_message(chat_id=user_id, message_id=message_id)
                            except Exception:
                                pass
                            await context.bot.send_message(chat_id=user_id, text=msg, reply_markup=markup, parse_mode="HTML")
                            return

                rec_ok, new_bal = record_auto_gateway_payment(utr, user_id, amount)
                if rec_ok:
                    succ_msg = (
                        "✅ <b>PAYMENT CONFIRMED INSTANTLY</b>\n"
                        "╭────────────────────────────╮\n"
                        f"  💵 <b>Credited:</b> <code>{CURRENCY_SYMBOL}{amount:.2f}</code>\n"
                        f"  💼 <b>New Balance:</b> <code>{CURRENCY_SYMBOL}{new_bal:.2f}</code>\n"
                        f"  🔖 <b>UTR:</b> <code>{utr}</code>\n"
                        "╰────────────────────────────╯\n\n"
                        "✨ <i>Your wallet balance has been updated successfully!</i>"
                    )
                    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🛍️ Browse Products", callback_data="back_to_menu")]])
                    try:
                        await context.bot.delete_message(chat_id=user_id, message_id=message_id)
                    except Exception:
                        pass
                    await context.bot.send_message(chat_id=user_id, text=succ_msg, reply_markup=markup, parse_mode="HTML")
                return

    ACTIVE_PAYMENT_POLLS.pop(order_id, None)


# ----------------- SMART PAYMENT PROCESSORS ----------------- #
async def generate_and_send_upi_payment(user_id: int, user_name: str, amount: float, context: ContextTypes.DEFAULT_TYPE, previous_message=None, prod_id: int = None, qty: int = None):
    wait_msg = None
    if previous_message:
        try:
            wait_msg = await previous_message.edit_text("⏳ <i>Initializing Secure UPI Gateway...</i>", parse_mode="HTML")
        except Exception:
            pass

    ok, order_id, res_data = await create_smb_order(user_id, user_name, amount, currency="INR", prod_id=prod_id, qty=qty)
    if not ok:
        err_text = f"⚠️ <b>Gateway Notice:</b> {res_data}\nPlease retry shortly or reach out to support."
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Return to Wallet", callback_data="cancel_payment_flow")]])
        if wait_msg:
            await wait_msg.edit_text(err_text, reply_markup=markup, parse_mode="HTML")
        else:
            await context.bot.send_message(chat_id=user_id, text=err_text, reply_markup=markup, parse_mode="HTML")
        return

    gateway_intent = res_data.get("intent_url") or res_data.get("upi_intent")
    if gateway_intent:
        upi_string = gateway_intent
    else:
        upi_string = f"upi://pay?pa={MERCHANT_VPA}&pn={urllib.parse.quote(BOT_NAME)}&am={amount:.2f}&cu=INR&tr={order_id}&tn={order_id}"

    qr_img = res_data.get("qr_code") or res_data.get("qr_image") or f"https://api.qrserver.com/v1/create-qr-code/?size=350x350&data={urllib.parse.quote(upi_string)}"

    cap = (
        "⚡ <b>SCAN & PAY VIA ANY UPI APP</b>\n"
        "╭────────────────────────────╮\n"
        f"  💵 <b>Amount Payable:</b> <code>{CURRENCY_SYMBOL}{amount:.2f}</code>\n"
        f"  🆔 <b>Order ID:</b> <code>{order_id}</code>\n"
        "╰────────────────────────────╯\n\n"
        "📌 <b>Payment Steps:</b>\n"
        "1. Scan using PhonePe, Paytm, Google Pay, or BHIM.\n"
        f"2. Pay the exact amount: <b>{CURRENCY_SYMBOL}{amount:.2f}</b>.\n"
        "3. Verification completes automatically within a few seconds.\n\n"
        "🔄 <i>Auto-detecting payment confirmation in real-time...</i>"
    )

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel Transaction", callback_data=f"cancel_order_{order_id}")],
    ])

    if wait_msg:
        try:
            await wait_msg.delete()
        except Exception:
            pass

    sent_msg = await context.bot.send_photo(chat_id=user_id, photo=qr_img, caption=cap, reply_markup=markup, parse_mode="HTML")
    ACTIVE_PAYMENT_POLLS[order_id] = True
    asyncio.create_task(auto_poll_payment_status(order_id, user_id, amount, sent_msg.message_id, context, prod_id, qty))


async def generate_and_send_usdt_payment(user_id: int, user_name: str, usdt_amount: float, context: ContextTypes.DEFAULT_TYPE, previous_message=None, prod_id: int = None, qty: int = None):
    wait_msg = None
    if previous_message:
        try:
            wait_msg = await previous_message.edit_text("⏳ <i>Generating Crypto Gateway Scanner...</i>", parse_mode="HTML")
        except Exception:
            pass

    ok, order_id, res_data = await create_smb_order(user_id, user_name, usdt_amount, currency="USDT", prod_id=prod_id, qty=qty)
    inr_equiv = usdt_amount * USDT_RATE_INR
    crypto_qr = res_data.get("qr_code") or res_data.get("qr_image") or f"https://api.qrserver.com/v1/create-qr-code/?size=350x350&data={USDT_WALLET_ADDRESS}"

    cap = (
        "💎 <b>USDT (TRC-20) CRYPTO GATEWAY</b>\n"
        "╭────────────────────────────╮\n"
        f"  💎 <b>Pay Exact:</b> <code>{usdt_amount:.2f} USDT</code>\n"
        f"  💵 <b>Wallet Credited:</b> <code>{CURRENCY_SYMBOL}{inr_equiv:.2f}</code>\n"
        "╰────────────────────────────╯\n\n"
        f"📍 <b>Deposit Address (TRC20):</b>\n<code>{USDT_WALLET_ADDRESS}</code>\n\n"
        "📌 <b>Transfer Instructions:</b>\n"
        "1. Send USDT strictly using the <b>TRC-20</b> network.\n"
        "2. Payments from Binance, TrustWallet, or any exchange are supported.\n"
        "3. Funds are credited automatically upon blockchain confirmation.\n\n"
        "🔄 <i>Auto-detecting blockchain confirmation...</i>"
    )

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel Transaction", callback_data=f"cancel_order_{order_id}")],
    ])

    if wait_msg:
        try:
            await wait_msg.delete()
        except Exception:
            pass

    sent_msg = await context.bot.send_photo(chat_id=user_id, photo=crypto_qr, caption=cap, reply_markup=markup, parse_mode="HTML")
    ACTIVE_PAYMENT_POLLS[order_id] = True
    asyncio.create_task(auto_poll_payment_status(order_id, user_id, inr_equiv, sent_msg.message_id, context, prod_id, qty))


async def show_payment_options_for_order(update: Update, context: ContextTypes.DEFAULT_TYPE, prod_id: int, qty: int):
    prod = get_product_by_id(prod_id)
    if not prod:
        return
    p_id, name, price, _ = prod
    inr = price * qty
    usdt = inr / USDT_RATE_INR
    user = update.effective_user
    bal = get_user_balance(user.id)
    stock = get_available_stock_count(prod_id)

    if stock < qty:
        await send_or_edit_menu(
            update,
            context,
            f"⚠️ <b>Inventory Notice:</b>\nRequested: {qty} unit(s) | Available: {stock} unit(s).",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Catalog", callback_data="back_to_menu")]]),
        )
        return

    text = (
        "🛒 <b>CHECKOUT & PAYMENT CONFIRMATION</b>\n"
        "╭────────────────────────────╮\n"
        f"  📦 <b>Product:</b> {name}\n"
        f"  🔢 <b>Quantity:</b> <code>{qty} unit(s)</code>\n"
        f"  💵 <b>Total Price:</b> <code>{CURRENCY_SYMBOL}{inr:.2f}</code> (${usdt:.2f} USDT)\n"
        f"  💼 <b>Your Balance:</b> <code>{CURRENCY_SYMBOL}{bal:.2f}</code>\n"
        "╰────────────────────────────╯\n\n"
        "👇 <b>Select your payment method:</b>"
    )
    btn = []
    if bal >= inr:
        btn.append([InlineKeyboardButton("⚡ Pay from Balance (Instant Fulfillment)", callback_data=f"paybal_{prod_id}_{qty}")])
    btn.append([InlineKeyboardButton(f"🟢 Pay via UPI ({CURRENCY_SYMBOL}{inr:.2f})", callback_data=f"payupi_{prod_id}_{qty}")])
    btn.append([InlineKeyboardButton(f"💎 Pay via USDT (${usdt:.2f} USDT)", callback_data=f"payusdt_{prod_id}_{qty}")])
    btn.append([InlineKeyboardButton("❌ Cancel Checkout", callback_data="cancel_payment_flow")])
    await send_or_edit_menu(update, context, text, InlineKeyboardMarkup(btn))


async def execute_balance_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE, prod_id: int, qty: int):
    user = update.effective_user
    ok, res = purchase_with_balance(user.id, prod_id, qty)
    if not ok:
        await show_payment_options_for_order(update, context, prod_id, qty)
        return

    raw, o_id, total, name, quantity = res
    formatted = format_credentials_card(raw)
    msg = (
        "🎉 <b>ORDER FULFILLED SUCCESSFULLY!</b>\n"
        "╭────────────────────────────╮\n"
        f"  🆔 <b>Order ID:</b> <code>#{o_id}</code>\n"
        f"  📦 <b>Product:</b> {name}\n"
        f"  🔢 <b>Quantity:</b> <code>{quantity} unit(s)</code>\n"
        f"  💵 <b>Total Paid:</b> <code>{CURRENCY_SYMBOL}{total:.2f}</code>\n"
        "╰────────────────────────────╯\n\n"
        f"🔐 <b>DELIVERED CREDENTIALS:</b>\n\n{formatted}\n\n"
        "💡 <i>Saved in Order Archive. Tap any block to copy.</i>"
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🛍️ Browse More Products", callback_data="back_to_menu")]])
    await send_or_edit_menu(update, context, msg, markup)


# ----------------- ROUTING & HANDLERS ----------------- #
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.user_data.clear()

    referrer_id = None
    if context.args and len(context.args) > 0:
        arg = context.args[0]
        if arg.startswith("ref_"):
            try:
                referrer_id = int(arg.replace("ref_", ""))
            except ValueError:
                referrer_id = None

    register_user(user.id, user.username or user.first_name, referrer_id)

    is_joined = await check_channel_membership(user.id, context)
    if not is_joined:
        join_text, join_markup = get_force_join_card(user.first_name)
        if update.message:
            await update.message.reply_text(join_text, reply_markup=join_markup, parse_mode="HTML")
        elif update.callback_query:
            await update.callback_query.message.edit_text(join_text, reply_markup=join_markup, parse_mode="HTML")
        return

    ref_parent, rew_amt = try_reward_referrer(user.id)
    if ref_parent and bot_app_instance:
        try:
            await bot_app_instance.bot.send_message(
                chat_id=ref_parent,
                text=f"🎉 <b>Referral Bonus Received!</b>\nYour referral has verified their membership. <b>{CURRENCY_SYMBOL}{rew_amt:.2f}</b> has been credited to your wallet!",
                parse_mode="HTML",
            )
        except Exception:
            pass

    text = (
        "⚡ <b>WELCOME TO TRICK BY STORE</b>\n"
        "╭────────────────────────────╮\n"
        f"  💎 <b>USDT Rate:</b> <code>1 USDT = ₹{USDT_RATE_INR:.2f}</code>\n"
        "  ⚡ Instant automated delivery & live balance deposits.\n"
        "╰────────────────────────────╯\n\n"
        "👇 <b>Select an item below to view options and order:</b>"
    )
    if update.message:
        await update.message.reply_text(f"⚡ <b>Welcome to {BOT_NAME}!</b>", reply_markup=get_main_reply_keyboard(), parse_mode="HTML")
        await update.message.reply_text(text, reply_markup=get_products_inline_keyboard(), parse_mode="HTML")
    elif update.callback_query:
        await send_or_edit_menu(update, context, text, get_products_inline_keyboard())


async def reply_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    user = update.effective_user
    register_user(user.id, user.username or user.first_name)

    if user.id not in ADMIN_IDS and is_rate_limited(user.id):
        return

    if user.id not in ADMIN_IDS:
        is_joined = await check_channel_membership(user.id, context)
        if not is_joined:
            join_text, join_markup = get_force_join_card(user.first_name)
            await update.message.reply_text(join_text, reply_markup=join_markup, parse_mode="HTML")
            return

    # Custom UPI Deposit
    if context.user_data.get("waiting_for_custom_upi_deposit"):
        context.user_data["waiting_for_custom_upi_deposit"] = False
        try:
            val = float(text)
            if val < MIN_DEPOSIT_INR:
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔢 Try Again", callback_data="upi_amt_custom")],
                    [InlineKeyboardButton("🔙 Cancel", callback_data="cancel_payment_flow")],
                ])
                await update.message.reply_text(f"⚠️ Minimum deposit is {CURRENCY_SYMBOL}{MIN_DEPOSIT_INR:.2f}.", reply_markup=markup, parse_mode="HTML")
                return
            await generate_and_send_upi_payment(user.id, user.first_name, val, context)
            return
        except ValueError:
            await update.message.reply_text("❌ Please enter a valid numerical value.")
            return

    # Custom USDT Deposit
    if context.user_data.get("waiting_for_custom_usdt_deposit"):
        context.user_data["waiting_for_custom_usdt_deposit"] = False
        try:
            val = float(text)
            if val < MIN_DEPOSIT_USDT:
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔢 Try Again", callback_data="usdt_amt_custom")],
                    [InlineKeyboardButton("🔙 Cancel", callback_data="cancel_payment_flow")],
                ])
                await update.message.reply_text(f"⚠️ Minimum deposit is {MIN_DEPOSIT_USDT:.2f} USDT.", reply_markup=markup, parse_mode="HTML")
                return
            await generate_and_send_usdt_payment(user.id, user.first_name, val, context)
            return
        except ValueError:
            await update.message.reply_text("❌ Please enter a valid numerical value.")
            return

    # ----------------- ADMIN FLOWS ----------------- #
    if context.user_data.get("adm_flow") == "update_prod_price":
        try:
            new_p = float(text)
            pid = context.user_data.get("target_prod_id")
            update_product_price(pid, new_p)
            context.user_data.clear()
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("👑 Dashboard", callback_data="adm_back_dash")]])
            await update.message.reply_text(f"✅ <b>Price Updated Successfully!</b>\nNew Price: ₹{new_p:.2f}", reply_markup=markup, parse_mode="HTML")
            return
        except ValueError:
            await update.message.reply_text("❌ Please enter a valid numerical price:")
            return

    if context.user_data.get("adm_flow") == "set_refer_bonus":
        try:
            val = float(text)
            set_referral_reward(val)
            context.user_data.clear()
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("👑 Dashboard", callback_data="adm_back_dash")]])
            await update.message.reply_text(f"✅ <b>Referral Bonus Updated!</b>\nNew Bonus: ₹{val:.2f} per verified invite.", reply_markup=markup, parse_mode="HTML")
            return
        except ValueError:
            await update.message.reply_text("❌ Please enter a valid numerical amount:")
            return

    if context.user_data.get("adm_flow") == "new_prod_name":
        context.user_data["temp_prod_name"] = text
        context.user_data["adm_flow"] = "new_prod_price"
        await update.message.reply_text(
            f"✅ <b>Product Name:</b> {html.escape(text)}\n\n"
            "💵 Enter the <b>Price in INR (₹)</b> (e.g., 50 or 99):",
            parse_mode="HTML",
        )
        return

    if context.user_data.get("adm_flow") == "new_prod_price":
        try:
            price = float(text)
            context.user_data["temp_prod_price"] = price
            context.user_data["adm_flow"] = "new_prod_desc"
            await update.message.reply_text(
                f"✅ <b>Price:</b> ₹{price:.2f}\n\n"
                "📝 Enter a short <b>Description</b> for this product:",
                parse_mode="HTML",
            )
        except ValueError:
            await update.message.reply_text("❌ Please enter a valid numerical price:")
        return

    if context.user_data.get("adm_flow") == "new_prod_desc":
        desc = text
        name = context.user_data.get("temp_prod_name")
        price = context.user_data.get("temp_prod_price")
        context.user_data.clear()

        pid = add_new_product(name, price, desc)
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("👑 Dashboard", callback_data="adm_back_dash")]])
        await update.message.reply_text(
            f"🎉 <b>Product Registered Successfully!</b>\n\n"
            f"🆔 <b>ID:</b> #{pid}\n"
            f"📦 <b>Name:</b> {html.escape(name)}\n"
            f"💵 <b>Price:</b> ₹{price:.2f}\n"
            f"📝 <b>Description:</b> {html.escape(desc)}",
            reply_markup=markup,
            parse_mode="HTML",
        )
        return

    if context.user_data.get("adm_flow") == "stock_step_account":
        context.user_data["temp_account"] = text
        context.user_data["adm_flow"] = "stock_step_password"
        m = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Complete & Finish", callback_data="adm_back_dash")]])
        await update.message.reply_text(
            f"✅ <b>Account Stored:</b>\n<code>{html.escape(text)}</code>\n\n"
            f"🔑 <b>Send the Password / Activation Data:</b>\n"
            f"<i>(Type <code>none</code> if this item has no password)</i>",
            reply_markup=m,
            parse_mode="HTML",
        )
        return

    if context.user_data.get("adm_flow") == "stock_step_password":
        pwd = text
        acc = context.user_data.get("temp_account")
        pid = context.user_data.get("target_prod_id")

        if pwd.lower() == "none":
            full_data = acc
        else:
            full_data = f"{acc} || {pwd}"

        add_single_item_stock(pid, full_data)
        count_now = get_available_stock_count(pid)
        prod = get_product_by_id(pid)

        context.user_data["adm_flow"] = "stock_step_account"
        context.user_data.pop("temp_account", None)

        m = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛑 Complete & Finish", callback_data="adm_back_dash")],
            [InlineKeyboardButton("👑 Dashboard", callback_data="adm_back_dash")],
        ])

        await update.message.reply_text(
            f"🎉 <b>Item Added to {prod[1]}!</b>\n"
            f"📧 Identifier: <code>{html.escape(acc)}</code>\n"
            f"🔑 Password: <code>{html.escape(pwd)}</code>\n"
            f"🧊 Available Stock: <code>{count_now} Units</code>\n\n"
            f"➡️ <b>Send the next Account / Email</b> (or tap Complete below):",
            reply_markup=m,
            parse_mode="HTML",
        )
        return

    if context.user_data.get("adm_step") == "credit_user":
        context.user_data.pop("adm_step", None)
        p = text.split()
        if len(p) == 2 and p[0].isdigit():
            try:
                target_uid = int(p[0])
                credit_val = float(p[1])
                add_user_balance(target_uid, credit_val)
                bal = get_user_balance(target_uid)
                markup = InlineKeyboardMarkup([[InlineKeyboardButton("👑 Dashboard", callback_data="adm_back_dash")]])
                await update.message.reply_text(f"✅ Credited {CURRENCY_SYMBOL}{credit_val:.2f} to {target_uid}.\nUpdated Balance: {CURRENCY_SYMBOL}{bal:.2f}", reply_markup=markup, parse_mode="HTML")
            except ValueError:
                await update.message.reply_text("❌ Invalid numerical amount provided.", parse_mode="HTML")
        else:
            await update.message.reply_text("❌ Syntax: <code>&lt;user_id&gt; &lt;amount&gt;</code>", parse_mode="HTML")
        return

    if context.user_data.get("waiting_for_custom_qty"):
        prod_id = context.user_data.pop("waiting_for_custom_qty")
        if not text.isdigit() or int(text) <= 0:
            await update.message.reply_text("❌ Please enter a positive integer.")
            return
        await show_payment_options_for_order(update, context, prod_id, int(text))
        return

    # Reply keyboard handlers
    upper_t = text.upper()
    if "BROWSE PRODUCTS" in upper_t or "CATALOG" in upper_t:
        text_menu = (
            "⚡ <b>EXCLUSIVE PRODUCT CATALOG</b>\n"
            "╭────────────────────────────╮\n"
            f"  💰 <b>Live Exchange Rate:</b> <code>1 USDT = ₹{USDT_RATE_INR:.2f}</code>\n"
            "╰────────────────────────────╯\n\n"
            "👇 <b>Select an item below to check details & order:</b>"
        )
        await update.message.reply_text(text_menu, reply_markup=get_products_inline_keyboard(), parse_mode="HTML")

    elif "MY WALLET" in upper_t:
        await render_wallet_view(update, context, user.id)

    elif "INVITE & EARN" in upper_t or "REFER" in upper_t:
        await render_refer_view(update, context, user.id)

    elif "DEVELOPER API" in upper_t or "RESELLER API" in upper_t or "API" in upper_t:
        await render_api_view(update, context, user.id)

    elif "ORDER ARCHIVE" in upper_t or "ORDER HISTORY" in upper_t:
        await render_orders_view(update, context, user.id)

    elif "SUPPORT DESK" in upper_t or "SUPPORT" in upper_t:
        await render_support_view(update, context)


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    user = update.effective_user

    if user.id not in ADMIN_IDS and is_rate_limited(user.id):
        return

    if data == "verify_channel_join":
        is_joined = await check_channel_membership(user.id, context)
        if is_joined:
            try:
                await q.message.delete()
            except Exception:
                pass
            ref_parent, rew_amt = try_reward_referrer(user.id)
            if ref_parent and bot_app_instance:
                try:
                    await bot_app_instance.bot.send_message(
                        chat_id=ref_parent,
                        text=f"🎉 <b>Referral Bonus Received!</b>\nYour referral has completed verification. <b>{CURRENCY_SYMBOL}{rew_amt:.2f}</b> has been credited to your wallet!",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            await start_command(update, context)
        else:
            await q.answer("❌ You haven't joined the channel yet! Please join and verify again.", show_alert=True)
        return

    if user.id not in ADMIN_IDS:
        is_joined = await check_channel_membership(user.id, context)
        if not is_joined:
            join_text, join_markup = get_force_join_card(user.first_name)
            await send_or_edit_menu(update, context, join_text, join_markup)
            return

    if data.startswith("cancel_order_"):
        order_id = data.replace("cancel_order_", "")
        ACTIVE_PAYMENT_POLLS.pop(order_id, None)
        try:
            await q.message.delete()
        except Exception:
            pass
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 View Wallet", callback_data="back_to_wallet")],
            [InlineKeyboardButton("🛍️ Browse Products", callback_data="back_to_menu")],
        ])
        await context.bot.send_message(chat_id=user.id, text="❌ <b>Transaction Cancelled.</b>", reply_markup=markup, parse_mode="HTML")
        return

    if data == "cancel_payment_flow":
        context.user_data.clear()
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 View Wallet", callback_data="back_to_wallet")],
            [InlineKeyboardButton("🛍️ Browse Products", callback_data="back_to_menu")],
        ])
        await send_or_edit_menu(update, context, "❌ <b>Transaction Cancelled.</b>", markup)
        return

    if data == "back_to_menu":
        text_menu = (
            "⚡ <b>EXCLUSIVE PRODUCT CATALOG</b>\n"
            "╭────────────────────────────╮\n"
            f"  💰 <b>Live Exchange Rate:</b> <code>1 USDT = ₹{USDT_RATE_INR:.2f}</code>\n"
            "╰────────────────────────────╯\n\n"
            "👇 <b>Select an item below to check details & order:</b>"
        )
        await send_or_edit_menu(update, context, text_menu, get_products_inline_keyboard())

    elif data in ["back_to_wallet", "add_funds_menu"]:
        await render_wallet_view(update, context, user.id)

    elif data == "api_regenerate":
        new_key = get_or_create_api_key(user.id, regenerate=True)
        await q.answer("🔑 New API Key Generated Successfully!", show_alert=True)
        await render_api_view(update, context, user.id, key=new_key)

    # UPI Deposit Flow
    elif data == "choose_upi_deposit":
        t = f"💳 <b>Instant UPI Top-up</b>\nMinimum deposit: {CURRENCY_SYMBOL}{MIN_DEPOSIT_INR:.2f}"
        await send_or_edit_menu(update, context, t, get_upi_amount_selection_keyboard())

    elif data.startswith("upi_amt_"):
        amt = data.split("_")[2]
        if amt == "custom":
            context.user_data["waiting_for_custom_upi_deposit"] = True
            await q.message.reply_text(f"🔢 Enter INR amount (Minimum {CURRENCY_SYMBOL}{MIN_DEPOSIT_INR:.0f}):")
        else:
            await generate_and_send_upi_payment(user.id, user.first_name, float(amt), context, previous_message=q.message)

    # USDT Deposit Flow
    elif data == "choose_usdt_deposit":
        t = f"💎 <b>Instant USDT (TRC-20) Top-up</b>\nMinimum deposit: {MIN_DEPOSIT_USDT:.2f} USDT"
        await send_or_edit_menu(update, context, t, get_usdt_amount_selection_keyboard())

    elif data.startswith("usdt_amt_"):
        amt = data.split("_")[2]
        if amt == "custom":
            context.user_data["waiting_for_custom_usdt_deposit"] = True
            await q.message.reply_text(f"🔢 Enter USDT amount (Minimum {MIN_DEPOSIT_USDT:.2f} USDT):")
        else:
            await generate_and_send_usdt_payment(user.id, user.first_name, float(amt), context, previous_message=q.message)

    # Product details & checkout
    elif data.startswith("prod_"):
        pid = int(data.split("_")[1])
        prod = get_product_by_id(pid)
        if not prod:
            return
        stock = get_available_stock_count(pid)
        stock_badge = f"🟢 In Stock ({stock} units)" if stock > 0 else "🔴 Sold Out"
        t = (
            f"📦 <b>{prod[1]}</b>\n"
            f"📝 <i>{prod[3] or 'Fresh verified accounts with warranty.'}</i>\n\n"
            f"💵 <b>Price:</b> <code>{CURRENCY_SYMBOL}{prod[2]:.2f}</code> (${prod[2]/USDT_RATE_INR:.2f} USDT)\n"
            f"📊 <b>Status:</b> {stock_badge}\n\n"
            "Choose quantity below:"
        )
        await send_or_edit_menu(update, context, t, get_quantity_keyboard(pid))

    elif data.startswith("selqty_"):
        parts = data.split("_")
        await show_payment_options_for_order(update, context, int(parts[1]), int(parts[2]))

    elif data.startswith("custom_qty_"):
        pid = int(data.split("_")[2])
        context.user_data["waiting_for_custom_qty"] = pid
        await q.message.reply_text("🔢 Type desired quantity in chat (e.g. 2, 5):")

    elif data.startswith("paybal_"):
        p = data.split("_")
        await execute_balance_checkout(update, context, int(p[1]), int(p[2]))

    elif data.startswith("payupi_"):
        p = data.split("_")
        prod = get_product_by_id(int(p[1]))
        if prod:
            await generate_and_send_upi_payment(user.id, user.first_name, prod[2] * int(p[2]), context, previous_message=q.message, prod_id=int(p[1]), qty=int(p[2]))

    elif data.startswith("payusdt_"):
        p = data.split("_")
        prod = get_product_by_id(int(p[1]))
        if prod:
            req_usdt = (prod[2] * int(p[2])) / USDT_RATE_INR
            await generate_and_send_usdt_payment(user.id, user.first_name, req_usdt, context, previous_message=q.message, prod_id=int(p[1]), qty=int(p[2]))

    # ----------------- ADMIN CALLBACKS ----------------- #
    elif data in ["adm_back_dash", "adm_refresh_stats"]:
        if user.id not in ADMIN_IDS:
            return
        context.user_data.clear()
        t, m = build_admin_dashboard_text_and_markup()
        await send_or_edit_menu(update, context, t, m)

    elif data == "adm_create_product":
        if user.id not in ADMIN_IDS:
            return
        context.user_data["adm_flow"] = "new_prod_name"
        m = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="adm_back_dash")]])
        await send_or_edit_menu(
            update, context,
            "📦 <b>REGISTER NEW SERVICE</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\nSend the <b>Product Name</b> (e.g., Netflix Premium 1 Month):",
            m,
        )

    elif data == "adm_set_refer_bonus":
        if user.id not in ADMIN_IDS:
            return
        context.user_data["adm_flow"] = "set_refer_bonus"
        cur_rew = get_referral_reward()
        m = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="adm_back_dash")]])
        await send_or_edit_menu(
            update, context,
            f"🎁 <b>UPDATE REFERRAL REWARD</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\nCurrent Reward: <b>₹{cur_rew:.2f}</b>\n\n"
            "Send the new reward amount per referral in INR (e.g., 3 or 5.5):",
            m,
        )

    elif data == "adm_update_price_menu":
        if user.id not in ADMIN_IDS:
            return
        prods = get_all_products()
        kb = []
        for p in prods:
            kb.append([InlineKeyboardButton(f"💲 {p[1]} (₹{p[2]:.2f})", callback_data=f"adm_editprice_{p[0]}")])
        kb.append([InlineKeyboardButton("🔙 Dashboard", callback_data="adm_back_dash")])
        await send_or_edit_menu(update, context, "💲 <b>Select a product to update its price:</b>", InlineKeyboardMarkup(kb))

    elif data.startswith("adm_editprice_"):
        if user.id not in ADMIN_IDS:
            return
        pid = int(data.split("_")[2])
        prod = get_product_by_id(pid)
        context.user_data["adm_flow"] = "update_prod_price"
        context.user_data["target_prod_id"] = pid
        m = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="adm_back_dash")]])
        await send_or_edit_menu(
            update, context,
            f"💲 <b>Updating Price for: {prod[1]}</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\nCurrent Price: <b>₹{prod[2]:.2f}</b>\n\n"
            "Send the new price in INR:",
            m,
        )

    elif data == "adm_delete_product_menu":
        if user.id not in ADMIN_IDS:
            return
        prods = get_all_products()
        kb = []
        for p in prods:
            kb.append([InlineKeyboardButton(f"🗑️ Delete {p[1]}", callback_data=f"adm_delprod_{p[0]}")])
        kb.append([InlineKeyboardButton("🔙 Dashboard", callback_data="adm_back_dash")])
        await send_or_edit_menu(update, context, "⚠️ <b>Select a single product to remove:</b>", InlineKeyboardMarkup(kb))

    elif data.startswith("adm_delprod_"):
        if user.id not in ADMIN_IDS:
            return
        pid = int(data.split("_")[2])
        delete_product_by_id(pid)
        await q.answer("Product deleted successfully!", show_alert=True)
        t, m = build_admin_dashboard_text_and_markup()
        await send_or_edit_menu(update, context, t, m)

    elif data == "adm_stock_menu":
        if user.id not in ADMIN_IDS:
            return
        prods = get_all_products()
        kb = []
        for p in prods:
            kb.append([InlineKeyboardButton(f"📥 Add Stock to {p[1]}", callback_data=f"adm_addstockto_{p[0]}")])
        kb.append([InlineKeyboardButton("🔙 Dashboard", callback_data="adm_back_dash")])
        await send_or_edit_menu(update, context, "📦 <b>Select product to add credentials:</b>", InlineKeyboardMarkup(kb))

    elif data.startswith("adm_addstockto_"):
        if user.id not in ADMIN_IDS:
            return
        pid = int(data.split("_")[2])
        prod = get_product_by_id(pid)
        context.user_data["adm_flow"] = "stock_step_account"
        context.user_data["target_prod_id"] = pid
        m = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="adm_back_dash")]])
        await send_or_edit_menu(
            update, context,
            f"📦 <b>Adding Stock for: {prod[1]}</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👉 <b>Step 1:</b> Send the <b>Account / Email / Identifier</b>:\n"
            f"<i>(The bot will prompt for the password on the next step)</i>",
            m,
        )

    elif data == "adm_credit_user":
        if user.id not in ADMIN_IDS:
            return
        context.user_data["adm_step"] = "credit_user"
        m = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="adm_back_dash")]])
        await send_or_edit_menu(update, context, "💳 Send: <code>&lt;user_id&gt; &lt;amount&gt;</code>", m)

    elif data == "adm_rm_menu":
        if user.id not in ADMIN_IDS:
            return
        prods = get_all_products()
        kb = []
        for p in prods:
            kb.append([InlineKeyboardButton(f"🧹 Clear {p[1]} Stock", callback_data=f"adm_clear_{p[0]}")])
        kb.append([InlineKeyboardButton("🔙 Dashboard", callback_data="adm_back_dash")])
        await send_or_edit_menu(update, context, "⚠️ <b>Select product to clear unsold stock:</b>", InlineKeyboardMarkup(kb))

    elif data.startswith("adm_clear_"):
        if user.id not in ADMIN_IDS:
            return
        cnt = clear_unsold_stock(int(data.split("_")[2]))
        await q.answer(f"Cleared {cnt} items.", show_alert=True)
        t, m = build_admin_dashboard_text_and_markup()
        await send_or_edit_menu(update, context, t, m)

    elif data == "adm_close":
        if user.id not in ADMIN_IDS:
            return
        try:
            await q.message.delete()
        except Exception:
            pass


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    t, m = build_admin_dashboard_text_and_markup()
    await update.message.reply_text(t, reply_markup=m, parse_mode="HTML")


async def post_init(application):
    global loop_instance
    loop_instance = asyncio.get_running_loop()


# ----------------- MAIN INITIALIZATION ----------------- #
def main():
    global bot_app_instance
    init_db()

    thread = threading.Thread(target=run_http_server, daemon=True)
    thread.start()

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    bot_app_instance = app

    # Core Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler(["admin", "addmin"], admin_command))
    app.add_handler(CommandHandler("contact", contact_command))
    app.add_handler(CommandHandler("terms", terms_command))
    app.add_handler(CommandHandler("policy", policy_command))

    # Event Handlers
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_text_router))

    logger.info("Trick By Store Bot running stably...")
    app.run_polling()


if __name__ == "__main__":
    main()
