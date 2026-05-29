import asyncio
import html
import json
import logging
import os
import re
import secrets
import string
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from bson import ObjectId
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING

# =====================================================
# Config
# =====================================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

XUI_BASE_URL = os.getenv("XUI_BASE_URL", "").rstrip("/")
XUI_API_TOKEN = os.getenv("XUI_API_TOKEN", "").strip()
INBOUND_ID = int(os.getenv("INBOUND_ID", "1"))
PUBLIC_HOST = os.getenv("PUBLIC_HOST", "").strip()

BOT_BRAND = os.getenv("BOT_BRAND", "AzizVPN").strip() or "AzizVPN"
CONFIG_PREFIX = os.getenv("CONFIG_PREFIX", "AzizVPN").strip() or "AzizVPN"
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/AzizVPN").strip()
TUTORIAL_URL = os.getenv("TUTORIAL_URL", CHANNEL_URL).strip()
SUPPORT_URL = os.getenv("SUPPORT_URL", f"tg://user?id={ADMIN_ID}").strip()

CARD_NUMBER = os.getenv("CARD_NUMBER", "6037-0000-0000-0000").strip()
CARD_HOLDER = os.getenv("CARD_HOLDER", "Azizollah").strip()
BANK_NAME = os.getenv("BANK_NAME", "بانک").strip()

MIN_WALLET_CHARGE = int(os.getenv("MIN_WALLET_CHARGE", "200000"))
MAX_WALLET_CHARGE = int(os.getenv("MAX_WALLET_CHARGE", "5000000"))

MONGO_URI = os.getenv("MONGO_URI", os.getenv("DATABASE_URL", "mongodb://localhost:27017/azizvpn_bot")).strip()
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "azizvpn_bot").strip()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("azizvpn-bot")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is empty. Put it in .env or Railway Variables.")
if not XUI_BASE_URL or not XUI_API_TOKEN:
    raise RuntimeError("XUI_BASE_URL / XUI_API_TOKEN is empty.")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID is empty or invalid.")
if not PUBLIC_HOST:
    raise RuntimeError("PUBLIC_HOST is empty. Example: gy.rahyab.top")

# =====================================================
# Plans
# =====================================================
PLAN_DEFS = [
    {"key": "mini", "emoji": "🌱", "name": "Mini", "gb": 15, "prices": {30: 229_000, 60: 319_000}},
    {"key": "starter", "emoji": "⭐️", "name": "Starter", "gb": 30, "prices": {30: 439_000, 60: 619_000}},
    {"key": "standard", "emoji": "💫", "name": "Standard", "gb": 50, "prices": {30: 719_000, 60: 999_000}},
    {"key": "pro", "emoji": "💥", "name": "Pro", "gb": 75, "prices": {30: 1_049_000, 60: 1_469_000}},
    {"key": "max", "emoji": "☄️", "name": "Max", "gb": 100, "prices": {30: 1_349_000, 60: 1_899_000}},
    {"key": "ultra", "emoji": "🚀", "name": "Ultra", "gb": 150, "prices": {30: 1_999_000, 60: 2_799_000}},
    {"key": "beast", "emoji": "⚡️", "name": "Beast", "gb": 200, "prices": {30: 2_599_000, 60: 3_639_000}},
]
PLANS_BY_CALLBACK: Dict[str, Dict[str, Any]] = {}
for plan in PLAN_DEFS:
    for d, price in plan["prices"].items():
        cb = f"{plan['key']}:{d}"
        PLANS_BY_CALLBACK[cb] = {**plan, "days": d, "price": price, "callback": cb}

RENEW_TIME_PRICES = {10: 99_000, 20: 179_000, 30: 249_000}
PRICE_PER_GB_RENEW = int(os.getenv("PRICE_PER_GB_RENEW", "15000"))
RENEW_VOLUME_OPTIONS = [15, 30, 50, 75, 100, 150, 200]
FREE_TRIAL_BYTES = 200 * 1024 * 1024
FREE_TRIAL_DAYS = 1
CLEANUP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "900"))

SUB_NAME_RE = re.compile(r"^[A-Za-z0-9]{3,32}$")

# =====================================================
# MongoDB
# =====================================================
mongo_client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=8000)
db = mongo_client[MONGO_DB_NAME]

users_col = db.users
states_col = db.states
orders_col = db.orders
subs_col = db.subscriptions
wallet_col = db.wallet_transactions
admin_logs_col = db.admin_logs


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def object_id(value: str) -> ObjectId:
    return ObjectId(value)


async def init_db() -> None:
    await mongo_client.admin.command("ping")
    await users_col.create_index([("username", ASCENDING)])
    await users_col.create_index([("blocked", ASCENDING)])
    await orders_col.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    await orders_col.create_index([("status", ASCENDING), ("created_at", DESCENDING)])
    await subs_col.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    await subs_col.create_index([("xui_email", ASCENDING)], unique=True)
    await subs_col.create_index([("status", ASCENDING), ("expires_at", ASCENDING)])
    await wallet_col.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    await states_col.create_index([("updated_at", ASCENDING)])
    logger.info("MongoDB connected: %s / db=%s", MONGO_URI.split("@")[0].replace(MONGO_URI.split("://")[0] + "://", "***://") if "://" in MONGO_URI else "***", MONGO_DB_NAME)


# =====================================================
# Helpers
# =====================================================
def h(value: Any) -> str:
    return html.escape(str(value), quote=False)


def fmt_money(amount: int) -> str:
    return f"{int(amount):,}".replace(",", ",") + " تومان"


def fmt_bytes(num: int) -> str:
    num = int(num or 0)
    if num >= 1024 ** 3:
        return f"{num / (1024 ** 3):.2f} GB"
    if num >= 1024 ** 2:
        return f"{num / (1024 ** 2):.0f} MB"
    return f"{num} B"


def gb_to_bytes(gb: int) -> int:
    return int(gb) * 1024 * 1024 * 1024


def dt_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


def fmt_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return "-"
    if isinstance(dt, (int, float)):
        dt = ms_to_dt(int(dt))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def random_string(length: int = 8) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def clean_sub_name(name: str) -> str:
    return name.strip()


def config_display_name(sub_name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9]", "", sub_name.strip())[:32] or "User"
    return f"{CONFIG_PREFIX}-{clean}"


def make_xui_email(sub_name: str, user_id: int) -> str:
    # This is the visible name inside 3X-UI. Random suffix prevents duplicate errors.
    base = config_display_name(sub_name)
    safe = re.sub(r"[^A-Za-z0-9_-]", "", base)[:40]
    return f"{safe}-{str(user_id)[-4:]}-{random_string(4)}"


def set_link_label(link: str, label: str) -> str:
    base = link.split("#", 1)[0]
    return f"{base}#{quote(label, safe='')}"


def short_sub_id(sub: Dict[str, Any]) -> str:
    return str(sub.get("_id"))[-6:]


async def get_or_create_user(tg_user) -> Dict[str, Any]:
    now = now_utc()
    doc = await users_col.find_one({"_id": int(tg_user.id)})
    data = {
        "username": tg_user.username,
        "first_name": tg_user.first_name,
        "last_name": tg_user.last_name,
        "updated_at": now,
    }
    if doc is None:
        data.update({
            "_id": int(tg_user.id),
            "balance": 0,
            "blocked": False,
            "free_trial_used": False,
            "created_at": now,
        })
        await users_col.insert_one(data)
        doc = data
    else:
        await users_col.update_one({"_id": int(tg_user.id)}, {"$set": data})
        doc.update(data)
    return doc


async def set_state(user_id: int, state: str, data: Optional[Dict[str, Any]] = None) -> None:
    await states_col.update_one(
        {"_id": int(user_id)},
        {"$set": {"state": state, "data": data or {}, "updated_at": now_utc()}},
        upsert=True,
    )


async def get_state(user_id: int) -> Tuple[Optional[str], Dict[str, Any]]:
    row = await states_col.find_one({"_id": int(user_id)})
    if not row:
        return None, {}
    return row.get("state"), row.get("data") or {}


async def clear_state(user_id: int) -> None:
    await states_col.delete_one({"_id": int(user_id)})


async def is_blocked(user_id: int) -> bool:
    user = await users_col.find_one({"_id": int(user_id)})
    return bool(user and user.get("blocked"))


async def wallet_change(user_id: int, amount: int, tx_type: str, description: str = "", order_id: Optional[ObjectId] = None) -> int:
    await users_col.update_one(
        {"_id": int(user_id)},
        {"$inc": {"balance": int(amount)}, "$set": {"updated_at": now_utc()}},
        upsert=True,
    )
    await wallet_col.insert_one({
        "user_id": int(user_id),
        "amount": int(amount),
        "type": tx_type,
        "description": description,
        "order_id": order_id,
        "status": "done",
        "created_at": now_utc(),
    })
    user = await users_col.find_one({"_id": int(user_id)})
    return int(user.get("balance", 0))


# =====================================================
# Keyboards
# =====================================================
def nav_rows(back_cb: str = "back:main", include_start: bool = True) -> List[List[InlineKeyboardButton]]:
    rows = [[InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_cb)]]
    if include_start:
        rows.append([InlineKeyboardButton(text="🏠 شروع دوباره", callback_data="back:main")])
    return rows


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍 خرید اشتراک", callback_data="main:buy")],
        [InlineKeyboardButton(text="♻️ تمدید اشتراک", callback_data="main:renew"), InlineKeyboardButton(text="⚡️ اشتراک‌ها", callback_data="main:subs")],
        [InlineKeyboardButton(text="🎟 تعرفه‌ها", callback_data="main:prices"), InlineKeyboardButton(text="💳 کیف پول", callback_data="main:wallet")],
        [InlineKeyboardButton(text="📙 آموزش اتصال", url=TUTORIAL_URL), InlineKeyboardButton(text="🛰 تست رایگان", callback_data="main:trial")],
        [InlineKeyboardButton(text="👨‍💼 پشتیبانی", url=SUPPORT_URL), InlineKeyboardButton(text="🔔 اطلاع‌رسانی", url=CHANNEL_URL)],
    ])


def simple_back_kb(back_cb: str = "back:main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=nav_rows(back_cb))


def duration_kb(auto_renew: bool = False) -> InlineKeyboardMarkup:
    auto = "🎗 تمدید خودکار: روشن" if auto_renew else "🎗 تمدید خودکار: خاموش"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏰ 30 روز", callback_data="buy:duration:30"), InlineKeyboardButton(text="⏰ 60 روز", callback_data="buy:duration:60")],
        [InlineKeyboardButton(text=auto, callback_data="buy:toggle_auto")],
        *nav_rows("back:buy_name"),
    ])


def plan_kb(days: int) -> InlineKeyboardMarkup:
    rows = []
    for p in PLAN_DEFS:
        price = p["prices"][days]
        rows.append([InlineKeyboardButton(text=f"{p['emoji']} {p['name']} · {days} روز · {p['gb']} گیگ · {fmt_money(price)}", callback_data=f"buy:plan:{p['key']}:{days}")])
    rows.extend(nav_rows("back:duration"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_kb(order_id: ObjectId) -> InlineKeyboardMarkup:
    oid = str(order_id)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 کیف پول", callback_data=f"pay:wallet:{oid}"), InlineKeyboardButton(text="💸 کارت به کارت", callback_data=f"pay:card:{oid}")],
        *nav_rows("back:main"),
    ])


def send_receipt_kb(order_id: ObjectId, back_cb: str = "back:main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 ارسال رسید", callback_data=f"receipt:send:{str(order_id)}")],
        *nav_rows(back_cb),
    ])


def admin_order_kb(order_id: ObjectId) -> InlineKeyboardMarkup:
    oid = str(order_id)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تایید", callback_data=f"admin:approve:{oid}"), InlineKeyboardButton(text="❌ رد", callback_data=f"admin:reject:{oid}")]
    ])


def wallet_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 افزایش موجودی", callback_data="wallet:charge"), InlineKeyboardButton(text="💸 انتقال موجودی", callback_data="wallet:transfer")],
        *nav_rows("back:main"),
    ])


def renew_subs_kb(subs: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = []
    for s in subs:
        rows.append([InlineKeyboardButton(text=f"🔹 {s.get('display_name') or s.get('name')} · {short_sub_id(s)}", callback_data=f"renew:select:{str(s['_id'])}")])
    rows.extend(nav_rows("back:main"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def renew_options_kb(sub_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏳ تمدید زمانی", callback_data=f"renew:time_menu:{sub_id}"), InlineKeyboardButton(text="📦 تمدید حجمی", callback_data=f"renew:volume_menu:{sub_id}")],
        *nav_rows("main:renew"),
    ])


def renew_days_kb(sub_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"10 روز · {fmt_money(RENEW_TIME_PRICES[10])}", callback_data=f"renew:time:{sub_id}:10")],
        [InlineKeyboardButton(text=f"20 روز · {fmt_money(RENEW_TIME_PRICES[20])}", callback_data=f"renew:time:{sub_id}:20")],
        [InlineKeyboardButton(text=f"30 روز · {fmt_money(RENEW_TIME_PRICES[30])}", callback_data=f"renew:time:{sub_id}:30")],
        *nav_rows(f"renew:select:{sub_id}"),
    ])


def renew_volume_kb(sub_id: str) -> InlineKeyboardMarkup:
    rows = []
    for gb in RENEW_VOLUME_OPTIONS:
        rows.append([InlineKeyboardButton(text=f"{gb} گیگ · {fmt_money(gb * PRICE_PER_GB_RENEW)}", callback_data=f"renew:volume:{sub_id}:{gb}")])
    rows.extend(nav_rows(f"renew:select:{sub_id}"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 آمار", callback_data="adm:stats")],
        [InlineKeyboardButton(text="➕ افزایش موجودی", callback_data="adm:addbal"), InlineKeyboardButton(text="➖ کاهش موجودی", callback_data="adm:subbal")],
        [InlineKeyboardButton(text="⛔️ بلاک کاربر", callback_data="adm:block"), InlineKeyboardButton(text="✅ آزاد کردن کاربر", callback_data="adm:unblock")],
        [InlineKeyboardButton(text="🔁 تمدید دستی اشتراک", callback_data="adm:renew")],
        [InlineKeyboardButton(text="📣 پیام همگانی", callback_data="adm:broadcast")],
        [InlineKeyboardButton(text="🏠 منوی کاربر", callback_data="back:main")],
    ])


def admin_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="لغو و بازگشت به پنل ادمین", callback_data="adm:menu")]])

# =====================================================
# 3X-UI API
# =====================================================
class XUIClient:
    def __init__(self, base_url: str, api_token: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def request(self, method: str, path: str, json_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.request(method, url, json=json_body, timeout=60) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(f"XUI API error {resp.status}: {text[:500]}")
                try:
                    data = json.loads(text) if text else {}
                except json.JSONDecodeError:
                    raise RuntimeError(f"XUI invalid JSON: {text[:500]}")
                if data and data.get("success") is False:
                    raise RuntimeError(f"XUI failed: {data.get('msg') or data}")
                return data

    async def get_inbound(self, inbound_id: int) -> Dict[str, Any]:
        try:
            data = await self.request("GET", f"/panel/api/inbounds/get/{inbound_id}")
            obj = data.get("obj")
            if obj:
                return obj
        except Exception:
            pass
        data = await self.request("GET", "/panel/api/inbounds/list")
        obj = next((x for x in data.get("obj", []) if int(x.get("id")) == int(inbound_id)), None)
        if not obj:
            raise RuntimeError(f"Inbound {inbound_id} not found")
        return obj

    async def add_client(self, *, email: str, total_bytes: int, expiry_ms: int, tg_id: int, comment: str, inbound_id: int, label: str) -> Dict[str, Any]:
        client_uuid = str(uuid.uuid4())
        sub_id = random_string(16)
        client = {
            "email": email,
            "id": client_uuid,
            "totalGB": int(total_bytes),
            "expiryTime": int(expiry_ms),
            "tgId": int(tg_id),
            "limitIp": 0,
            "enable": True,
            "subId": sub_id,
            "reset": 0,
            "security": "auto",
            "comment": comment,
        }
        payload = {"client": client, "inboundIds": [int(inbound_id)]}
        await self.request("POST", "/panel/api/clients/add", payload)
        links = await self.get_links(email)
        raw_link = links[0] if links else await self.build_fallback_link(email=email, client_uuid=client_uuid, label=label, inbound_id=inbound_id)
        link = set_link_label(raw_link, label)
        return {"uuid": client_uuid, "sub_id": sub_id, "link": link}

    async def get_links(self, email: str) -> List[str]:
        try:
            data = await self.request("GET", f"/panel/api/clients/links/{quote(email, safe='')}")
            return data.get("obj") or []
        except Exception as exc:
            logger.warning("links failed for %s: %s", email, exc)
            return []

    async def get_client(self, email: str) -> Optional[Dict[str, Any]]:
        try:
            data = await self.request("GET", f"/panel/api/clients/get/{quote(email, safe='')}")
            return data.get("obj")
        except Exception as exc:
            logger.warning("get_client failed for %s: %s", email, exc)
            return None

    async def bulk_adjust(self, emails: List[str], add_days: int = 0, add_bytes: int = 0) -> bool:
        try:
            payload = {"emails": emails, "addDays": int(add_days), "addBytes": int(add_bytes)}
            await self.request("POST", "/panel/api/clients/bulkAdjust", payload)
            return True
        except Exception as exc:
            logger.warning("bulkAdjust failed: %s", exc)
            return False

    async def update_client(self, email: str, patch: Dict[str, Any]) -> bool:
        obj = await self.get_client(email)
        client = obj.get("client") if isinstance(obj, dict) else None
        if not client:
            return False
        client.update(patch)
        try:
            await self.request("POST", f"/panel/api/clients/update/{quote(email, safe='')}", client)
            return True
        except Exception as exc:
            logger.warning("update client failed: %s", exc)
            return False

    async def delete_client(self, email: str) -> bool:
        try:
            await self.request("POST", f"/panel/api/clients/del/{quote(email, safe='')}")
            return True
        except Exception as exc:
            logger.warning("delete client failed for %s: %s", email, exc)
            return False

    async def traffic(self, email: str) -> Optional[Dict[str, Any]]:
        try:
            data = await self.request("GET", f"/panel/api/clients/traffic/{quote(email, safe='')}")
            return data.get("obj")
        except Exception:
            return None

    async def build_fallback_link(self, email: str, client_uuid: str, label: str, inbound_id: int) -> str:
        inbound = await self.get_inbound(inbound_id)
        port = int(inbound.get("port"))
        stream = inbound.get("streamSettings") or {}
        if isinstance(stream, str):
            stream = json.loads(stream)
        network = stream.get("network", "tcp")
        security = stream.get("security", "none")
        params = {"type": network, "security": security}
        if network == "grpc":
            grpc = stream.get("grpcSettings", {}) or {}
            if grpc.get("serviceName"):
                params["serviceName"] = grpc["serviceName"]
            params["mode"] = "multi" if grpc.get("multiMode") else "gun"
        elif network == "ws":
            ws = stream.get("wsSettings", {}) or {}
            params["path"] = ws.get("path", "/")
            host = (ws.get("headers") or {}).get("Host")
            if host:
                params["host"] = host
        if security == "reality":
            reality = stream.get("realitySettings", {}) or {}
            names = reality.get("serverNames") or []
            if names:
                params["sni"] = names[0]
            if reality.get("publicKey"):
                params["pbk"] = reality["publicKey"]
            short_ids = reality.get("shortIds") or []
            if short_ids:
                params["sid"] = short_ids[0]
            params["fp"] = reality.get("fingerprint", "chrome")
            params["spx"] = reality.get("spiderX", "/")
        query = "&".join(f"{quote(str(k))}={quote(str(v), safe='')}" for k, v in params.items())
        return f"vless://{client_uuid}@{PUBLIC_HOST}:{port}?{query}#{quote(label, safe='')}"


xui = XUIClient(XUI_BASE_URL, XUI_API_TOKEN)

# =====================================================
# Telegram bot
# =====================================================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()


async def send_or_edit(message: Message, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        await message.answer(text, reply_markup=reply_markup, parse_mode="HTML", disable_web_page_preview=True)


async def show_main(message: Message, tg_user) -> None:
    user = await get_or_create_user(tg_user)
    await clear_state(tg_user.id)
    name = user.get("first_name") or user.get("username") or user.get("_id")
    text = (
        f"🌹 کاربر عزیز <b>{h(name)}</b> خوش اومدی به ربات <b>{h(BOT_BRAND)}</b>\n\n"
        "🔸 جهت خرید اشتراک روی دکمه «🛍 خرید اشتراک» ضربه بزنید."
    )
    await send_or_edit(message, text, main_menu_kb())


async def ensure_allowed(message_or_call) -> bool:
    user_id = message_or_call.from_user.id
    if await is_blocked(user_id):
        msg = "⛔️ حساب شما توسط مدیریت مسدود شده است."
        if isinstance(message_or_call, CallbackQuery):
            await message_or_call.answer(msg, show_alert=True)
        else:
            await message_or_call.answer(msg)
        return False
    return True


@dp.message(CommandStart())
async def on_start(message: Message) -> None:
    await show_main(message, message.from_user)


@dp.message(Command("admin"))
async def on_admin_cmd(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    await clear_state(message.from_user.id)
    await message.answer("👑 پنل مدیریت AzizVPN", reply_markup=admin_menu_kb())


@dp.callback_query(F.data == "back:main")
async def cb_back_main(call: CallbackQuery) -> None:
    await call.answer()
    await show_main(call.message, call.from_user)


@dp.callback_query(F.data == "adm:menu")
async def cb_admin_menu(call: CallbackQuery) -> None:
    if call.from_user.id != ADMIN_ID:
        await call.answer("دسترسی نداری", show_alert=True)
        return
    await call.answer()
    await clear_state(call.from_user.id)
    await send_or_edit(call.message, "👑 پنل مدیریت AzizVPN", admin_menu_kb())


@dp.callback_query(F.data.startswith("main:"))
async def cb_main(call: CallbackQuery) -> None:
    if not await ensure_allowed(call):
        return
    action = call.data.split(":", 1)[1]
    await get_or_create_user(call.from_user)
    await call.answer()
    if action == "buy":
        await set_state(call.from_user.id, "buy_name", {})
        text = (
            "🖋 لطفا نام اشتراک خود را وارد کنید:\n\n"
            "💡 نام اشتراک در تشخیص و مدیریت اشتراک‌ها به شما کمک خواهد کرد.\n\n"
            "🔻 هنگام وارد کردن نام اشتراک دقت فرمایید که نام مورد نظر شما نباید کمتر از 3 کاراکتر و فقط شامل حروف و اعداد انگلیسی باشد.\n\n"
            "مثال: <code>Aziz01</code>"
        )
        await send_or_edit(call.message, text, simple_back_kb())
    elif action == "wallet":
        await show_wallet(call.message, call.from_user.id)
    elif action == "prices":
        await show_prices(call.message)
    elif action == "subs":
        await show_subscriptions(call.message, call.from_user.id)
    elif action == "renew":
        await show_renew_list(call.message, call.from_user.id)
    elif action == "trial":
        await create_trial(call)


async def show_prices(message: Message) -> None:
    lines = [f"🎟 تعرفه‌های ربات <b>{h(BOT_BRAND)}</b>:\n"]
    for days in (30, 60):
        for p in PLAN_DEFS:
            lines.append(f"{p['emoji']} <b>{p['name']}</b> · {days} روزه · {p['gb']} گیگ · {fmt_money(p['prices'][days])}")
        lines.append("")
    await send_or_edit(message, "\n".join(lines), simple_back_kb())


async def show_wallet(message: Message, user_id: int) -> None:
    user = await users_col.find_one({"_id": int(user_id)}) or {"balance": 0}
    text = (
        f"💎 شناسه کاربری: <code>{user_id}</code>\n\n"
        f"💰 موجودی کیف پول: <b>{fmt_money(int(user.get('balance', 0)))}</b>\n\n"
        f"❗️ حداقل مبلغ مجاز برای افزایش موجودی {fmt_money(MIN_WALLET_CHARGE)} و حداکثر مبلغ مجاز {fmt_money(MAX_WALLET_CHARGE)} است\n\n"
        "💠 جهت افزایش موجودی کیف پول خود روی دکمه «💳 افزایش موجودی» کلیک کنید.\n\n"
        "💠 جهت انتقال موجودی کیف پول خود به یک اکانت تلگرامی دیگر روی دکمه «💸 انتقال موجودی» کلیک کنید."
    )
    await send_or_edit(message, text, wallet_kb())


async def show_subscriptions(message: Message, user_id: int) -> None:
    docs = await subs_col.find({"user_id": int(user_id), "status": {"$in": ["active", "expired"]}}).sort("created_at", DESCENDING).to_list(50)
    if not docs:
        await send_or_edit(message, "❌ در حال حاضر اکانت فعالی ندارید ابتدا یک اشتراک از بخش اشتراک‌ها تهیه کنید", simple_back_kb())
        return
    lines = ["⚡️ <b>اشتراک‌های شما</b>\n"]
    for s in docs:
        used = "-"
        traffic = await xui.traffic(s.get("xui_email"))
        if traffic:
            used = fmt_bytes(int(traffic.get("up", 0)) + int(traffic.get("down", 0)))
        lines.append(
            f"🔹 <b>{h(s.get('display_name') or s.get('name'))}</b>\n"
            f"شناسه: <code>{str(s['_id'])}</code>\n"
            f"وضعیت: <code>{h(s.get('status'))}</code>\n"
            f"حجم: {s.get('volume_gb')} گیگ | مصرف: {used}\n"
            f"انقضا: <code>{fmt_dt(s.get('expires_at'))}</code>\n"
            f"لینک:\n<code>{h(s.get('link') or '')}</code>\n"
        )
    await send_or_edit(message, "\n".join(lines), simple_back_kb())


async def show_renew_list(message: Message, user_id: int) -> None:
    limit_date = now_utc() - timedelta(days=7)
    docs = await subs_col.find({
        "user_id": int(user_id),
        "is_trial": {"$ne": True},
        "$or": [
            {"status": "active"},
            {"status": "expired", "expires_at": {"$gte": limit_date}},
        ],
    }).sort("created_at", DESCENDING).to_list(30)
    if not docs:
        text = (
            "❌ شما اشتراکی که واجد شرایط تمدید باشد ندارید.\n\n"
            "❕ توجه داشته باشید که اگر اشتراک فعالی دارید و در لیست تمدید اشتراک‌ها نیست به این معنی است که طبق الگوی مصرف اشتراک شما شرایط تمدید را ندارد.\n\n"
            "⚠️ اشتراک‌های منقضی شده فقط تا 7 روز پس از انقضا قابل تمدید می‌باشند."
        )
        await send_or_edit(message, text, simple_back_kb())
        return
    await send_or_edit(message, "♻️ لطفاً اشتراک مورد نظر برای تمدید را انتخاب کنید:", renew_subs_kb(docs))


# =====================================================
# Buying flow
# =====================================================
@dp.callback_query(F.data == "back:buy_name")
async def cb_back_buy_name(call: CallbackQuery) -> None:
    await call.answer()
    await set_state(call.from_user.id, "buy_name", {})
    await send_or_edit(call.message, "🖋 لطفا نام اشتراک خود را وارد کنید:\n\nمثال: <code>Aziz01</code>", simple_back_kb())


@dp.callback_query(F.data == "back:duration")
async def cb_back_duration(call: CallbackQuery) -> None:
    await call.answer()
    state, data = await get_state(call.from_user.id)
    auto = bool(data.get("auto_renew", False))
    await set_state(call.from_user.id, "buy_duration", data)
    await send_or_edit(call.message,
        "🖋 مدت زمان اشتراک خود را انتخاب کنید:\n\n"
        "💡 با فعال کردن «🎗تمدید خودکار» پس از خرید اشتراک، مدت زمان و ترافیک اشتراک به صورت خودکار تمدید خواهد شد.",
        duration_kb(auto),
    )


@dp.callback_query(F.data == "buy:toggle_auto")
async def cb_toggle_auto(call: CallbackQuery) -> None:
    await call.answer()
    state, data = await get_state(call.from_user.id)
    data["auto_renew"] = not bool(data.get("auto_renew"))
    await set_state(call.from_user.id, "buy_duration", data)
    await send_or_edit(call.message,
        "🖋 مدت زمان اشتراک خود را انتخاب کنید:\n\n"
        "💡 با فعال کردن «🎗تمدید خودکار» پس از خرید اشتراک، مدت زمان و ترافیک اشتراک به صورت خودکار تمدید خواهد شد.",
        duration_kb(bool(data.get("auto_renew"))),
    )


@dp.callback_query(F.data.startswith("buy:duration:"))
async def cb_buy_duration(call: CallbackQuery) -> None:
    await call.answer()
    days = int(call.data.split(":")[-1])
    state, data = await get_state(call.from_user.id)
    if not data.get("sub_name"):
        await cb_back_buy_name(call)
        return
    data["duration_days"] = days
    await set_state(call.from_user.id, "buy_plan", data)
    text = "🖋 لطفا اشتراک مورد نظر خود را انتخاب کنید:\n\n💡 تمامی اشتراک‌ها دارای تعداد کاربر نامحدود برای اتصال هستند."
    await send_or_edit(call.message, text, plan_kb(days))


@dp.callback_query(F.data.startswith("buy:plan:"))
async def cb_buy_plan(call: CallbackQuery) -> None:
    await call.answer()
    _, _, key, days_s = call.data.split(":")
    cb = f"{key}:{days_s}"
    plan = PLANS_BY_CALLBACK.get(cb)
    if not plan:
        await call.answer("پلن نامعتبر", show_alert=True)
        return
    state, data = await get_state(call.from_user.id)
    if not data.get("sub_name"):
        await cb_back_buy_name(call)
        return
    order = {
        "user_id": int(call.from_user.id),
        "kind": "purchase",
        "status": "draft",
        "amount": int(plan["price"]),
        "payment_method": None,
        "sub_name": data["sub_name"],
        "display_name": config_display_name(data["sub_name"]),
        "plan_key": plan["key"],
        "plan_name": plan["name"],
        "duration_days": int(plan["days"]),
        "volume_gb": int(plan["gb"]),
        "auto_renew": bool(data.get("auto_renew", False)),
        "created_at": now_utc(),
        "updated_at": now_utc(),
    }
    res = await orders_col.insert_one(order)
    await clear_state(call.from_user.id)
    text = (
        "🖋 روش پرداخت مورد نظر خود را انتخاب کنید:\n\n"
        f"🔹 نام اشتراک: <b>{h(order['display_name'])}</b>\n"
        f"📦 پلن: {h(plan['name'])} · {plan['gb']} گیگ · {plan['days']} روز\n"
        f"💰 مبلغ: <b>{fmt_money(plan['price'])}</b>"
    )
    await send_or_edit(call.message, text, payment_kb(res.inserted_id))


@dp.callback_query(F.data.startswith("pay:wallet:"))
async def cb_pay_wallet(call: CallbackQuery) -> None:
    if not await ensure_allowed(call):
        return
    await call.answer()
    oid = object_id(call.data.split(":")[-1])
    order = await orders_col.find_one({"_id": oid, "user_id": int(call.from_user.id)})
    if not order or order.get("status") not in ("draft", "pending_receipt"):
        await call.answer("سفارش پیدا نشد یا قابل پرداخت نیست", show_alert=True)
        return
    user = await users_col.find_one({"_id": int(call.from_user.id)}) or {"balance": 0}
    amount = int(order.get("amount", 0))
    if int(user.get("balance", 0)) < amount:
        text = (
            "❌ موجودی کیف پول کافی نیست.\n\n"
            f"💰 مبلغ سفارش: <b>{fmt_money(amount)}</b>\n"
            f"💳 موجودی شما: <b>{fmt_money(int(user.get('balance', 0)))}</b>\n\n"
            "برای شارژ حساب روی دکمه زیر بزنید."
        )
        await send_or_edit(call.message, text, InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 شارژ کیف پول", callback_data="wallet:charge")],
            *nav_rows("back:main"),
        ]))
        return
    await wallet_change(call.from_user.id, -amount, "purchase", f"payment for order {oid}", oid)
    await orders_col.update_one({"_id": oid}, {"$set": {"payment_method": "wallet", "status": "approved", "updated_at": now_utc()}})
    try:
        sub = await fulfill_order(order, approved_by=call.from_user.id)
        # Single message only; this fixes the duplicate wallet-config message.
        await send_or_edit(call.message, config_ready_text(sub, "✅ پرداخت از کیف پول انجام شد و اشتراک شما ساخته شد."), simple_back_kb())
    except Exception as exc:
        logger.exception("wallet fulfill failed")
        await wallet_change(call.from_user.id, amount, "refund", f"refund failed order {oid}", oid)
        await orders_col.update_one({"_id": oid}, {"$set": {"status": "failed", "error": str(exc), "updated_at": now_utc()}})
        await send_or_edit(call.message, f"❌ خطا در ساخت کانفیگ. مبلغ به کیف پول برگشت داده شد.\n<code>{h(exc)}</code>", simple_back_kb())


@dp.callback_query(F.data.startswith("pay:card:"))
async def cb_pay_card(call: CallbackQuery) -> None:
    await call.answer()
    oid = object_id(call.data.split(":")[-1])
    order = await orders_col.find_one({"_id": oid, "user_id": int(call.from_user.id)})
    if not order:
        await call.answer("سفارش پیدا نشد", show_alert=True)
        return
    await orders_col.update_one({"_id": oid}, {"$set": {"payment_method": "card", "status": "pending_receipt", "updated_at": now_utc()}})
    await send_or_edit(call.message, card_payment_text(order), send_receipt_kb(oid, "back:main"))


@dp.callback_query(F.data.startswith("receipt:send:"))
async def cb_send_receipt(call: CallbackQuery) -> None:
    await call.answer()
    oid = object_id(call.data.split(":")[-1])
    order = await orders_col.find_one({"_id": oid, "user_id": int(call.from_user.id)})
    if not order:
        await call.answer("سفارش پیدا نشد", show_alert=True)
        return
    await set_state(call.from_user.id, "awaiting_receipt", {"order_id": str(oid)})
    await send_or_edit(call.message, "📤 لطفاً تصویر رسید پرداخت را همینجا ارسال کنید.", simple_back_kb())


def card_payment_text(order: Dict[str, Any]) -> str:
    lines = [
        "💸 <b>پرداخت کارت به کارت</b>\n",
        f"💰 مبلغ قابل پرداخت: <b>{fmt_money(int(order.get('amount', 0)))}</b>",
        f"🏦 بانک: <b>{h(BANK_NAME)}</b>",
        f"💳 شماره کارت: <code>{h(CARD_NUMBER)}</code>",
        f"👤 صاحب کارت: <b>{h(CARD_HOLDER)}</b>",
        "",
        "⚠️ پس از واریز، روی دکمه «📤 ارسال رسید» بزنید و عکس رسید را ارسال کنید.",
    ]
    if order.get("kind") == "purchase":
        lines += [
            "",
            "🧾 <b>اطلاعات اشتراک</b>",
            f"نام: <b>{h(order.get('display_name'))}</b>",
            f"پلن: {h(order.get('plan_name'))} · {order.get('volume_gb')} گیگ · {order.get('duration_days')} روز",
        ]
    elif order.get("kind") == "wallet_charge":
        lines += ["", "🧾 نوع سفارش: شارژ کیف پول"]
    elif order.get("kind", "").startswith("renew"):
        lines += ["", "🧾 نوع سفارش: تمدید اشتراک"]
    return "\n".join(lines)


async def create_order_for_renew(user_id: int, sub_id: str, kind: str, days: int = 0, gb: int = 0) -> ObjectId:
    sub = await subs_col.find_one({"_id": object_id(sub_id), "user_id": int(user_id)})
    if not sub:
        raise RuntimeError("اشتراک پیدا نشد")
    if kind == "renew_time":
        amount = RENEW_TIME_PRICES[int(days)]
    else:
        amount = int(gb) * PRICE_PER_GB_RENEW
    order = {
        "user_id": int(user_id),
        "kind": kind,
        "status": "draft",
        "amount": amount,
        "payment_method": None,
        "subscription_id": sub["_id"],
        "sub_name": sub.get("name"),
        "display_name": sub.get("display_name"),
        "duration_days": int(days),
        "volume_gb": int(gb),
        "created_at": now_utc(),
        "updated_at": now_utc(),
    }
    res = await orders_col.insert_one(order)
    return res.inserted_id


# =====================================================
# Text message states
# =====================================================
@dp.message(F.text)
async def on_text(message: Message) -> None:
    await get_or_create_user(message.from_user)
    if message.text in ("/start", "شروع", "شروع دوباره", "🏠 شروع دوباره"):
        await show_main(message, message.from_user)
        return
    if message.from_user.id != ADMIN_ID and await is_blocked(message.from_user.id):
        await message.answer("⛔️ حساب شما مسدود شده است.")
        return

    state, data = await get_state(message.from_user.id)
    if not state:
        await message.answer("برای شروع دوباره /start را بزنید.")
        return

    if state == "buy_name":
        name = clean_sub_name(message.text)
        if not SUB_NAME_RE.fullmatch(name):
            await message.answer("❌ نام اشتراک باید حداقل 3 کاراکتر و فقط شامل حروف و اعداد انگلیسی باشد.\nمثال: <code>Aziz01</code>", parse_mode="HTML")
            return
        data = {"sub_name": name, "auto_renew": False}
        await set_state(message.from_user.id, "buy_duration", data)
        await message.answer(
            "🖋 مدت زمان اشتراک خود را انتخاب کنید:\n\n"
            "💡 با فعال کردن «🎗تمدید خودکار» پس از خرید اشتراک، مدت زمان و ترافیک اشتراک به صورت خودکار تمدید خواهد شد.",
            reply_markup=duration_kb(False),
        )
        return

    if state == "wallet_charge_amount":
        try:
            amount = int(re.sub(r"[^0-9]", "", message.text))
        except ValueError:
            await message.answer("❌ مبلغ را فقط به عدد و تومان وارد کنید.", reply_markup=simple_back_kb())
            return
        if amount < MIN_WALLET_CHARGE or amount > MAX_WALLET_CHARGE:
            await message.answer(f"❌ مبلغ باید بین {fmt_money(MIN_WALLET_CHARGE)} تا {fmt_money(MAX_WALLET_CHARGE)} باشد.", reply_markup=simple_back_kb())
            return
        order = {
            "user_id": int(message.from_user.id),
            "kind": "wallet_charge",
            "status": "pending_receipt",
            "amount": amount,
            "payment_method": "card",
            "created_at": now_utc(),
            "updated_at": now_utc(),
        }
        res = await orders_col.insert_one(order)
        await clear_state(message.from_user.id)
        order["_id"] = res.inserted_id
        await message.answer(card_payment_text(order), reply_markup=send_receipt_kb(res.inserted_id, "main:wallet"), parse_mode="HTML")
        return

    if state == "wallet_transfer_target":
        try:
            target_id = int(re.sub(r"[^0-9]", "", message.text))
        except ValueError:
            await message.answer("❌ شناسه تلگرام مقصد را عددی وارد کنید.", reply_markup=simple_back_kb())
            return
        if target_id == message.from_user.id:
            await message.answer("❌ انتقال به خودتان ممکن نیست.", reply_markup=simple_back_kb())
            return
        await set_state(message.from_user.id, "wallet_transfer_amount", {"target_id": target_id})
        await message.answer("💸 مبلغ انتقال را به تومان وارد کنید:", reply_markup=simple_back_kb("main:wallet"))
        return

    if state == "wallet_transfer_amount":
        try:
            amount = int(re.sub(r"[^0-9]", "", message.text))
        except ValueError:
            await message.answer("❌ مبلغ را عددی وارد کنید.", reply_markup=simple_back_kb())
            return
        target_id = int(data.get("target_id"))
        user = await users_col.find_one({"_id": int(message.from_user.id)}) or {"balance": 0}
        if amount <= 0 or int(user.get("balance", 0)) < amount:
            await message.answer("❌ موجودی کافی نیست.", reply_markup=simple_back_kb())
            return
        await users_col.update_one({"_id": target_id}, {"$setOnInsert": {"created_at": now_utc(), "free_trial_used": False, "blocked": False}, "$inc": {"balance": amount}, "$set": {"updated_at": now_utc()}}, upsert=True)
        await wallet_change(message.from_user.id, -amount, "transfer_out", f"transfer to {target_id}")
        await wallet_col.insert_one({"user_id": target_id, "amount": amount, "type": "transfer_in", "description": f"transfer from {message.from_user.id}", "created_at": now_utc(), "status": "done"})
        await clear_state(message.from_user.id)
        await message.answer(f"✅ مبلغ {fmt_money(amount)} به کاربر <code>{target_id}</code> منتقل شد.", reply_markup=simple_back_kb(), parse_mode="HTML")
        try:
            await bot.send_message(target_id, f"💳 مبلغ {fmt_money(amount)} از طرف کاربر <code>{message.from_user.id}</code> به کیف پول شما اضافه شد.", parse_mode="HTML")
        except Exception:
            pass
        return

    # Admin states
    if message.from_user.id == ADMIN_ID:
        await handle_admin_state_text(message, state, data)
        return


@dp.message(F.photo | F.document)
async def on_receipt_file(message: Message) -> None:
    if message.from_user.id != ADMIN_ID and await is_blocked(message.from_user.id):
        await message.answer("⛔️ حساب شما مسدود شده است.")
        return
    state, data = await get_state(message.from_user.id)
    if state == "admin_broadcast" and message.from_user.id == ADMIN_ID:
        await broadcast_admin_message(message)
        return
    if state != "awaiting_receipt":
        return
    oid = object_id(data.get("order_id"))
    order = await orders_col.find_one({"_id": oid, "user_id": int(message.from_user.id)})
    if not order:
        await message.answer("❌ سفارش پیدا نشد.", reply_markup=simple_back_kb())
        return
    if message.photo:
        file_id = message.photo[-1].file_id
        file_type = "photo"
    else:
        file_id = message.document.file_id
        file_type = "document"
    await orders_col.update_one({"_id": oid}, {"$set": {"receipt_file_id": file_id, "receipt_file_type": file_type, "status": "pending_admin", "updated_at": now_utc()}})
    await clear_state(message.from_user.id)
    await message.answer("✅ رسید شما ثبت شد و برای بررسی به ادمین ارسال شد.\nپس از تایید، نتیجه به شما اعلام می‌شود.", reply_markup=simple_back_kb())
    updated = await orders_col.find_one({"_id": oid})
    await send_order_to_admin(updated)




async def broadcast_admin_message(message: Message) -> None:
    users = users_col.find({}, {"_id": 1})
    sent = 0
    failed = 0
    async for user in users:
        try:
            await bot.copy_message(chat_id=int(user["_id"]), from_chat_id=message.chat.id, message_id=message.message_id)
            sent += 1
            await asyncio.sleep(0.04)
        except Exception:
            failed += 1
    await clear_state(ADMIN_ID)
    await message.answer(f"📣 پیام همگانی ارسال شد.\nموفق: {sent}\nناموفق: {failed}", reply_markup=admin_menu_kb())

async def send_order_to_admin(order: Dict[str, Any]) -> None:
    text = await admin_order_text(order)
    if order.get("receipt_file_type") == "photo":
        await bot.send_photo(ADMIN_ID, order["receipt_file_id"], caption=text, reply_markup=admin_order_kb(order["_id"]), parse_mode="HTML")
    elif order.get("receipt_file_type") == "document":
        await bot.send_document(ADMIN_ID, order["receipt_file_id"], caption=text, reply_markup=admin_order_kb(order["_id"]), parse_mode="HTML")
    else:
        await bot.send_message(ADMIN_ID, text, reply_markup=admin_order_kb(order["_id"]), parse_mode="HTML")


async def admin_order_text(order: Dict[str, Any]) -> str:
    user = await users_col.find_one({"_id": int(order["user_id"])}) or {}
    lines = [
        "🧾 <b>رسید جدید برای بررسی</b>",
        f"نوع: <code>{h(order.get('kind'))}</code>",
        f"کاربر: <code>{order.get('user_id')}</code> | @{h(user.get('username') or '-')}",
        f"مبلغ: <b>{fmt_money(int(order.get('amount', 0)))}</b>",
        f"وضعیت: <code>{h(order.get('status'))}</code>",
        f"Order ID: <code>{str(order.get('_id'))}</code>",
    ]
    if order.get("kind") == "purchase":
        lines += [
            "",
            f"نام: <b>{h(order.get('display_name'))}</b>",
            f"پلن: {h(order.get('plan_name'))} · {order.get('volume_gb')} گیگ · {order.get('duration_days')} روز",
        ]
    elif str(order.get("kind", "")).startswith("renew"):
        lines += ["", f"اشتراک: <code>{h(order.get('subscription_id'))}</code>", f"روز: {order.get('duration_days') or 0} | حجم: {order.get('volume_gb') or 0} GB"]
    return "\n".join(lines)


# =====================================================
# Fulfillment and subscriptions
# =====================================================
def config_ready_text(sub: Dict[str, Any], header: str = "✅ اشتراک شما آماده شد.") -> str:
    return (
        f"{header}\n\n"
        f"🔹 نام: <b>{h(sub.get('display_name') or sub.get('name'))}</b>\n"
        f"📦 حجم: <b>{sub.get('volume_gb')} گیگ</b>\n"
        f"⏳ انقضا: <code>{fmt_dt(sub.get('expires_at'))}</code>\n\n"
        "🔗 لینک اتصال:\n"
        f"<code>{h(sub.get('link') or '')}</code>"
    )


async def fulfill_order(order: Dict[str, Any], approved_by: int) -> Dict[str, Any]:
    kind = order.get("kind")
    if kind == "wallet_charge":
        await wallet_change(order["user_id"], int(order["amount"]), "charge", f"approved charge order {order['_id']}", order["_id"])
        await orders_col.update_one({"_id": order["_id"]}, {"$set": {"status": "approved", "approved_by": approved_by, "updated_at": now_utc()}})
        return {}

    if kind == "purchase":
        sub_name = order["sub_name"]
        display_name = config_display_name(sub_name)
        email = make_xui_email(sub_name, int(order["user_id"]))
        expires_at = now_utc() + timedelta(days=int(order["duration_days"]))
        total_bytes = gb_to_bytes(int(order["volume_gb"]))
        result = await xui.add_client(
            email=email,
            total_bytes=total_bytes,
            expiry_ms=dt_to_ms(expires_at),
            tg_id=int(order["user_id"]),
            comment=f"order:{order['_id']} user:{order['user_id']} name:{display_name}",
            inbound_id=INBOUND_ID,
            label=display_name,
        )
        sub = {
            "user_id": int(order["user_id"]),
            "name": sub_name,
            "display_name": display_name,
            "xui_email": email,
            "uuid": result.get("uuid"),
            "sub_id": result.get("sub_id"),
            "link": result.get("link"),
            "plan_key": order.get("plan_key"),
            "plan_name": order.get("plan_name"),
            "duration_days": int(order["duration_days"]),
            "volume_gb": int(order["volume_gb"]),
            "total_bytes": total_bytes,
            "expires_at": expires_at,
            "status": "active",
            "is_trial": False,
            "auto_renew": bool(order.get("auto_renew", False)),
            "order_id": order["_id"],
            "created_at": now_utc(),
            "updated_at": now_utc(),
        }
        res = await subs_col.insert_one(sub)
        sub["_id"] = res.inserted_id
        await orders_col.update_one({"_id": order["_id"]}, {"$set": {"status": "approved", "subscription_id": res.inserted_id, "approved_by": approved_by, "updated_at": now_utc()}})
        return sub

    if kind in ("renew_time", "renew_volume"):
        sub = await subs_col.find_one({"_id": order.get("subscription_id")})
        if not sub:
            raise RuntimeError("اشتراک برای تمدید پیدا نشد")
        add_days = int(order.get("duration_days") or 0)
        add_bytes = gb_to_bytes(int(order.get("volume_gb") or 0))
        ok = await xui.bulk_adjust([sub["xui_email"]], add_days=add_days, add_bytes=add_bytes)
        now = now_utc()
        new_expires = sub.get("expires_at") or now
        if new_expires < now:
            new_expires = now
        if add_days:
            new_expires = new_expires + timedelta(days=add_days)
        new_total = int(sub.get("total_bytes", 0)) + add_bytes
        update_doc = {"updated_at": now, "status": "active", "expires_at": new_expires, "total_bytes": new_total, "volume_gb": int(round(new_total / (1024 ** 3)))}
        if not ok:
            # Fallback update by replacing totalGB/expiryTime if bulkAdjust endpoint is not available.
            await xui.update_client(sub["xui_email"], {"totalGB": new_total, "expiryTime": dt_to_ms(new_expires), "enable": True})
        await subs_col.update_one({"_id": sub["_id"]}, {"$set": update_doc})
        await orders_col.update_one({"_id": order["_id"]}, {"$set": {"status": "approved", "approved_by": approved_by, "updated_at": now_utc()}})
        fresh = await subs_col.find_one({"_id": sub["_id"]})
        return fresh

    raise RuntimeError(f"Unknown order kind: {kind}")


# =====================================================
# Admin approve/reject
# =====================================================
@dp.callback_query(F.data.startswith("admin:approve:"))
async def cb_admin_approve(call: CallbackQuery) -> None:
    if call.from_user.id != ADMIN_ID:
        await call.answer("دسترسی نداری", show_alert=True)
        return
    await call.answer("در حال تایید...")
    oid = object_id(call.data.split(":")[-1])
    order = await orders_col.find_one({"_id": oid})
    if not order or order.get("status") not in ("pending_admin", "pending_receipt", "draft"):
        await call.message.answer("❌ سفارش پیدا نشد یا قبلاً بررسی شده.")
        return
    try:
        sub = await fulfill_order(order, approved_by=ADMIN_ID)
        await call.message.answer("✅ سفارش تایید شد.")
        if order.get("kind") == "wallet_charge":
            user = await users_col.find_one({"_id": int(order["user_id"])}) or {}
            await bot.send_message(order["user_id"], f"✅ شارژ کیف پول شما تایید شد.\n💰 موجودی جدید: <b>{fmt_money(int(user.get('balance', 0)))}</b>", parse_mode="HTML", reply_markup=simple_back_kb())
        elif order.get("kind") == "purchase":
            await bot.send_message(order["user_id"], config_ready_text(sub, "✅ پرداخت شما تایید شد. این هم کانفیگ شما:"), parse_mode="HTML", reply_markup=simple_back_kb())
        elif str(order.get("kind", "")).startswith("renew"):
            await bot.send_message(order["user_id"], config_ready_text(sub, "✅ تمدید اشتراک شما تایید و اعمال شد."), parse_mode="HTML", reply_markup=simple_back_kb())
    except Exception as exc:
        logger.exception("admin approve failed")
        await call.message.answer(f"❌ خطا در تایید/ساخت:\n<code>{h(exc)}</code>", parse_mode="HTML")


@dp.callback_query(F.data.startswith("admin:reject:"))
async def cb_admin_reject(call: CallbackQuery) -> None:
    if call.from_user.id != ADMIN_ID:
        await call.answer("دسترسی نداری", show_alert=True)
        return
    await call.answer()
    oid = call.data.split(":")[-1]
    await set_state(ADMIN_ID, "admin_reject_reason", {"order_id": oid})
    await call.message.answer("❌ دلیل رد سفارش را بنویسید:", reply_markup=admin_cancel_kb())


# =====================================================
# Wallet callbacks
# =====================================================
@dp.callback_query(F.data == "wallet:charge")
async def cb_wallet_charge(call: CallbackQuery) -> None:
    await call.answer()
    await set_state(call.from_user.id, "wallet_charge_amount", {})
    await send_or_edit(call.message, f"💳 مبلغ شارژ کیف پول را به تومان وارد کنید.\nحداقل: {fmt_money(MIN_WALLET_CHARGE)}\nحداکثر: {fmt_money(MAX_WALLET_CHARGE)}", simple_back_kb("main:wallet"))


@dp.callback_query(F.data == "wallet:transfer")
async def cb_wallet_transfer(call: CallbackQuery) -> None:
    await call.answer()
    await set_state(call.from_user.id, "wallet_transfer_target", {})
    await send_or_edit(call.message, "💸 شناسه عددی تلگرام کاربر مقصد را وارد کنید:", simple_back_kb("main:wallet"))


# =====================================================
# Renewal callbacks
# =====================================================
@dp.callback_query(F.data.startswith("renew:select:"))
async def cb_renew_select(call: CallbackQuery) -> None:
    await call.answer()
    sid = call.data.split(":")[-1]
    sub = await subs_col.find_one({"_id": object_id(sid), "user_id": int(call.from_user.id)})
    if not sub:
        await call.answer("اشتراک پیدا نشد", show_alert=True)
        return
    text = (
        f"♻️ تمدید اشتراک <b>{h(sub.get('display_name') or sub.get('name'))}</b>\n\n"
        f"حجم فعلی: {sub.get('volume_gb')} گیگ\n"
        f"انقضا: <code>{fmt_dt(sub.get('expires_at'))}</code>\n\n"
        "نوع تمدید را انتخاب کنید:"
    )
    await send_or_edit(call.message, text, renew_options_kb(sid))


@dp.callback_query(F.data.startswith("renew:time_menu:"))
async def cb_renew_time_menu(call: CallbackQuery) -> None:
    await call.answer()
    sid = call.data.split(":")[-1]
    await send_or_edit(call.message, "⏳ مدت تمدید زمانی را انتخاب کنید:", renew_days_kb(sid))


@dp.callback_query(F.data.startswith("renew:volume_menu:"))
async def cb_renew_vol_menu(call: CallbackQuery) -> None:
    await call.answer()
    sid = call.data.split(":")[-1]
    await send_or_edit(call.message, "📦 حجم تمدید را انتخاب کنید:", renew_volume_kb(sid))


@dp.callback_query(F.data.startswith("renew:time:"))
async def cb_renew_time(call: CallbackQuery) -> None:
    await call.answer()
    _, _, sid, days_s = call.data.split(":")
    oid = await create_order_for_renew(call.from_user.id, sid, "renew_time", days=int(days_s))
    order = await orders_col.find_one({"_id": oid})
    await send_or_edit(call.message, f"🖋 روش پرداخت تمدید زمانی را انتخاب کنید:\n\nمبلغ: <b>{fmt_money(order['amount'])}</b>", payment_kb(oid))


@dp.callback_query(F.data.startswith("renew:volume:"))
async def cb_renew_volume(call: CallbackQuery) -> None:
    await call.answer()
    _, _, sid, gb_s = call.data.split(":")
    oid = await create_order_for_renew(call.from_user.id, sid, "renew_volume", gb=int(gb_s))
    order = await orders_col.find_one({"_id": oid})
    await send_or_edit(call.message, f"🖋 روش پرداخت تمدید حجمی را انتخاب کنید:\n\nحجم: {gb_s} گیگ\nمبلغ: <b>{fmt_money(order['amount'])}</b>", payment_kb(oid))


# =====================================================
# Free trial
# =====================================================
async def create_trial(call: CallbackQuery) -> None:
    user = await users_col.find_one({"_id": int(call.from_user.id)})
    if user and user.get("free_trial_used"):
        await send_or_edit(call.message, "❌ شما قبلاً تست رایگان خود را دریافت کرده‌اید.", simple_back_kb())
        return
    sub_name = f"Trial{str(call.from_user.id)[-4:]}"
    display_name = config_display_name(sub_name)
    email = make_xui_email(sub_name, int(call.from_user.id))
    expires_at = now_utc() + timedelta(days=FREE_TRIAL_DAYS)
    try:
        result = await xui.add_client(
            email=email,
            total_bytes=FREE_TRIAL_BYTES,
            expiry_ms=dt_to_ms(expires_at),
            tg_id=int(call.from_user.id),
            comment=f"free trial user:{call.from_user.id}",
            inbound_id=INBOUND_ID,
            label=display_name,
        )
        sub = {
            "user_id": int(call.from_user.id),
            "name": sub_name,
            "display_name": display_name,
            "xui_email": email,
            "uuid": result.get("uuid"),
            "sub_id": result.get("sub_id"),
            "link": result.get("link"),
            "plan_key": "trial",
            "plan_name": "Free Trial",
            "duration_days": FREE_TRIAL_DAYS,
            "volume_gb": 0,
            "total_bytes": FREE_TRIAL_BYTES,
            "expires_at": expires_at,
            "status": "active",
            "is_trial": True,
            "auto_renew": False,
            "created_at": now_utc(),
            "updated_at": now_utc(),
        }
        await subs_col.insert_one(sub)
        await users_col.update_one({"_id": int(call.from_user.id)}, {"$set": {"free_trial_used": True, "updated_at": now_utc()}})
        await send_or_edit(call.message,
            "🛰 تست رایگان شما ساخته شد.\n\n"
            "📦 حجم: <b>200 مگابایت</b>\n"
            "⏳ مدت: <b>1 روز</b>\n\n"
            f"<code>{h(result.get('link'))}</code>",
            simple_back_kb(),
        )
    except Exception as exc:
        logger.exception("trial failed")
        await send_or_edit(call.message, f"❌ خطا در ساخت تست رایگان:\n<code>{h(exc)}</code>", simple_back_kb())


# =====================================================
# Admin panel
# =====================================================
@dp.callback_query(F.data.startswith("adm:"))
async def cb_admin_panel(call: CallbackQuery) -> None:
    if call.from_user.id != ADMIN_ID:
        await call.answer("دسترسی نداری", show_alert=True)
        return
    action = call.data.split(":", 1)[1]
    await call.answer()
    if action == "stats":
        total_users = await users_col.count_documents({})
        blocked = await users_col.count_documents({"blocked": True})
        active_subs = await subs_col.count_documents({"status": "active"})
        orders_pending = await orders_col.count_documents({"status": "pending_admin"})
        total_balance = 0
        async for u in users_col.find({}, {"balance": 1}):
            total_balance += int(u.get("balance", 0))
        text = (
            "📊 <b>آمار ربات</b>\n\n"
            f"👥 کاربران: <b>{total_users}</b>\n"
            f"⛔️ بلاک‌شده: <b>{blocked}</b>\n"
            f"⚡️ اشتراک فعال: <b>{active_subs}</b>\n"
            f"🧾 سفارش در انتظار: <b>{orders_pending}</b>\n"
            f"💰 مجموع موجودی کیف پول‌ها: <b>{fmt_money(total_balance)}</b>"
        )
        await send_or_edit(call.message, text, admin_menu_kb())
    elif action == "addbal":
        await set_state(ADMIN_ID, "admin_add_balance", {})
        await send_or_edit(call.message, "➕ فرمت را بفرست:\n<code>USER_ID AMOUNT</code>\nمثال: <code>995380371 200000</code>", admin_cancel_kb())
    elif action == "subbal":
        await set_state(ADMIN_ID, "admin_sub_balance", {})
        await send_or_edit(call.message, "➖ فرمت را بفرست:\n<code>USER_ID AMOUNT</code>", admin_cancel_kb())
    elif action == "block":
        await set_state(ADMIN_ID, "admin_block_user", {})
        await send_or_edit(call.message, "⛔️ شناسه عددی کاربر برای بلاک را بفرست:", admin_cancel_kb())
    elif action == "unblock":
        await set_state(ADMIN_ID, "admin_unblock_user", {})
        await send_or_edit(call.message, "✅ شناسه عددی کاربر برای آزاد کردن را بفرست:", admin_cancel_kb())
    elif action == "broadcast":
        await set_state(ADMIN_ID, "admin_broadcast", {})
        await send_or_edit(call.message, "📣 پیام همگانی را بفرست. متن، عکس یا فایل را می‌توانی ارسال کنی.", admin_cancel_kb())
    elif action == "renew":
        await set_state(ADMIN_ID, "admin_manual_renew", {})
        await send_or_edit(call.message,
            "🔁 تمدید دستی اشتراک\nفرمت:\n<code>SUB_ID_OR_EMAIL DAYS GB</code>\n\n"
            "مثال:\n<code>AzizVPN-Aziz01-0371-abcd 30 50</code>\n"
            "یا با شناسه دیتابیس اشتراک.",
            admin_cancel_kb(),
        )


async def handle_admin_state_text(message: Message, state: str, data: Dict[str, Any]) -> None:
    if state == "admin_reject_reason":
        oid = object_id(data.get("order_id"))
        reason = message.text.strip()
        order = await orders_col.find_one({"_id": oid})
        if not order:
            await message.answer("سفارش پیدا نشد.", reply_markup=admin_menu_kb())
            await clear_state(ADMIN_ID)
            return
        await orders_col.update_one({"_id": oid}, {"$set": {"status": "rejected", "reject_reason": reason, "updated_at": now_utc(), "rejected_by": ADMIN_ID}})
        await clear_state(ADMIN_ID)
        await message.answer("✅ سفارش رد شد و دلیل برای کاربر ارسال شد.", reply_markup=admin_menu_kb())
        try:
            await bot.send_message(order["user_id"], f"❌ سفارش شما رد شد.\n\nدلیل رد:\n<b>{h(reason)}</b>", parse_mode="HTML", reply_markup=simple_back_kb())
        except Exception:
            pass
        return

    if state in ("admin_add_balance", "admin_sub_balance"):
        parts = message.text.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            await message.answer("❌ فرمت اشتباه است. مثال: <code>995380371 200000</code>", parse_mode="HTML", reply_markup=admin_cancel_kb())
            return
        uid, amount = int(parts[0]), int(parts[1])
        delta = amount if state == "admin_add_balance" else -amount
        bal = await wallet_change(uid, delta, "admin_adjust", f"admin adjust by {ADMIN_ID}")
        await clear_state(ADMIN_ID)
        await message.answer(f"✅ انجام شد. موجودی جدید کاربر <code>{uid}</code>: <b>{fmt_money(bal)}</b>", parse_mode="HTML", reply_markup=admin_menu_kb())
        try:
            await bot.send_message(uid, f"💳 موجودی کیف پول شما توسط مدیریت تغییر کرد.\nموجودی جدید: <b>{fmt_money(bal)}</b>", parse_mode="HTML")
        except Exception:
            pass
        return

    if state in ("admin_block_user", "admin_unblock_user"):
        uid_text = re.sub(r"[^0-9]", "", message.text)
        if not uid_text:
            await message.answer("❌ شناسه عددی وارد کن.", reply_markup=admin_cancel_kb())
            return
        uid = int(uid_text)
        blocked = state == "admin_block_user"
        await users_col.update_one({"_id": uid}, {"$set": {"blocked": blocked, "updated_at": now_utc()}, "$setOnInsert": {"balance": 0, "free_trial_used": False, "created_at": now_utc()}}, upsert=True)
        await clear_state(ADMIN_ID)
        await message.answer(("⛔️ کاربر بلاک شد." if blocked else "✅ کاربر آزاد شد."), reply_markup=admin_menu_kb())
        return

    if state == "admin_manual_renew":
        parts = message.text.split()
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            await message.answer("❌ فرمت اشتباه است. مثال: <code>SUB_ID_OR_EMAIL 30 50</code>", parse_mode="HTML", reply_markup=admin_cancel_kb())
            return
        key, days, gb = parts[0], int(parts[1]), int(parts[2])
        query: Dict[str, Any]
        if ObjectId.is_valid(key):
            query = {"_id": ObjectId(key)}
        else:
            query = {"xui_email": key}
        sub = await subs_col.find_one(query)
        if not sub:
            await message.answer("❌ اشتراک پیدا نشد.", reply_markup=admin_cancel_kb())
            return
        add_bytes = gb_to_bytes(gb)
        await xui.bulk_adjust([sub["xui_email"]], add_days=days, add_bytes=add_bytes)
        base_exp = sub.get("expires_at") or now_utc()
        if base_exp < now_utc():
            base_exp = now_utc()
        new_exp = base_exp + timedelta(days=days)
        new_total = int(sub.get("total_bytes", 0)) + add_bytes
        await subs_col.update_one({"_id": sub["_id"]}, {"$set": {"expires_at": new_exp, "total_bytes": new_total, "volume_gb": int(round(new_total / (1024 ** 3))), "status": "active", "updated_at": now_utc()}})
        await clear_state(ADMIN_ID)
        await message.answer("✅ تمدید دستی اعمال شد.", reply_markup=admin_menu_kb())
        try:
            fresh = await subs_col.find_one({"_id": sub["_id"]})
            await bot.send_message(sub["user_id"], config_ready_text(fresh, "✅ اشتراک شما توسط مدیریت تمدید شد."), parse_mode="HTML")
        except Exception:
            pass
        return

    if state == "admin_broadcast":
        await broadcast_admin_message(message)
        return


# =====================================================
# Cleanup loop
# =====================================================
async def cleanup_loop() -> None:
    await asyncio.sleep(10)
    while True:
        try:
            await cleanup_expired()
        except Exception:
            logger.exception("cleanup loop error")
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


async def cleanup_expired() -> None:
    now = now_utc()
    async for sub in subs_col.find({"status": "active"}):
        traffic = await xui.traffic(sub.get("xui_email"))
        used = 0
        if traffic:
            used = int(traffic.get("up", 0)) + int(traffic.get("down", 0))
        expired_by_time = sub.get("expires_at") and sub["expires_at"] <= now
        expired_by_volume = used >= int(sub.get("total_bytes", 0)) > 0
        if sub.get("is_trial") and (expired_by_time or expired_by_volume):
            await xui.delete_client(sub["xui_email"])
            await subs_col.update_one({"_id": sub["_id"]}, {"$set": {"status": "deleted", "ended_at": now, "deleted_at": now, "updated_at": now}})
        elif (expired_by_time or expired_by_volume) and not sub.get("is_trial"):
            await subs_col.update_one({"_id": sub["_id"]}, {"$set": {"status": "expired", "ended_at": now, "updated_at": now}})

    delete_before = now - timedelta(days=10)
    async for sub in subs_col.find({"status": "expired", "is_trial": {"$ne": True}, "ended_at": {"$lte": delete_before}}):
        await xui.delete_client(sub["xui_email"])
        await subs_col.update_one({"_id": sub["_id"]}, {"$set": {"status": "deleted", "deleted_at": now, "updated_at": now}})


# =====================================================
# Main
# =====================================================
async def main() -> None:
    await init_db()
    # Test 3X-UI connection on startup.
    inbound = await xui.get_inbound(INBOUND_ID)
    logger.info("3X-UI connected. Inbound=%s port=%s", inbound.get("remark"), inbound.get("port"))
    asyncio.create_task(cleanup_loop())
    logger.info("Bot started: %s", BOT_BRAND)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
