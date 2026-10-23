import asyncio
import logging
import os
import re
import time
from typing import Annotated

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, Message, WebAppInfo,
)
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints
import uvicorn

import database as db
from auth import validate_init_data, InitDataError

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DEV_MODE = os.getenv("DEV_MODE", "0") == "1"
MAX_TRUCKS_PER_USER = 5

app = FastAPI(title="Fura Mini App")
bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()


# ---------------- Davlat raqami ----------------
# Kirill klaviaturada yozilgan, lotinchaga o'xshash harflar (bir xil raqam ikki xil
# yozilib, takroriy qo'shilishning oldini olish uchun)
_CYR_TO_LAT = str.maketrans("АВЕКМНОРСТУХ", "ABEKMHOPCTYX")


def normalize_plate(raw: str):
    """'01 a 123 bc' -> '01A123BC'. Noto'g'ri bo'lsa None."""
    plate = re.sub(r"[\s\-_.]", "", raw.upper().translate(_CYR_TO_LAT))
    if not re.fullmatch(r"[A-Z0-9]{6,10}", plate) or not re.search(r"\d", plate):
        return None
    return plate


def format_plate(plate: str) -> str:
    """O'zbekiston raqamlarini o'qishga qulay ko'rinishga keltiradi: 01 A 123 BC / 01 123 ABC."""
    m = re.fullmatch(r"(\d{2})([A-Z])(\d{3})([A-Z]{2})", plate)
    if m:
        return " ".join(m.groups())
    m = re.fullmatch(r"(\d{2})(\d{3})([A-Z]{3})", plate)
    if m:
        return " ".join(m.groups())
    return plate


def plate_or_400(raw: str) -> str:
    plate = normalize_plate(raw)
    if not plate:
        raise HTTPException(400, "Davlat raqami noto'g'ri")
    return plate


async def get_user_id(x_telegram_init_data: str = Header(default="")) -> int:
    if DEV_MODE and not x_telegram_init_data:
        # Faqat lokal test uchun: haqiqiy Telegram ma'lumoti bo'lmasa ham ishlash imkonini beradi
        return await db.get_or_create_user(telegram_id=999999999, full_name="Dev User")
    if not BOT_TOKEN:
        # Tokensiz imzoni tekshirib bo'lmaydi - har qanday so'rovni rad etamiz
        raise HTTPException(status_code=503, detail="BOT_TOKEN sozlanmagan")
    try:
        user = validate_init_data(x_telegram_init_data, BOT_TOKEN)
    except InitDataError as e:
        raise HTTPException(status_code=401, detail=str(e))
    full_name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
    return await db.get_or_create_user(
        telegram_id=user["id"], full_name=full_name, username=user.get("username") or ""
    )


# ---------------- Pydantic modellar ----------------
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]
Money = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class TruckIn(BaseModel):
    plate: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]
    brand: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
    volume: Annotated[float, Field(gt=0, le=100000, allow_inf_nan=False)]


class WatchIn(BaseModel):
    plate: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]


class TruckExpenseIn(BaseModel):
    category: Text
    amount: Money
    note: Note = ""


class TripIn(BaseModel):
    truck_id: int


class TripExpenseIn(BaseModel):
    category: Text
    amount: Money
    note: Note = ""


class TripLegIn(BaseModel):
    from_point: Text
    to_point: Text
    price: Money


# ---------------- Trucks ----------------
@app.get("/api/trucks")
async def api_list_trucks(x_telegram_init_data: str = Header(default="")):
    user_id = await get_user_id(x_telegram_init_data)
    rows = await db.list_trucks(user_id)
    return [
        {
            "id": r[0], "name": r[1], "created_at": r[2], "repair_total": r[3],
            "plate": r[4], "brand": r[5], "volume": r[6],
        }
        for r in rows
    ]


@app.get("/api/trucks/check")
async def api_check_plate(plate: str = Query(max_length=20), x_telegram_init_data: str = Header(default="")):
    """Qo'shishning 1-qadami: raqam bo'shmi, o'zingizdami yoki boshqa akkauntdami."""
    user_id = await get_user_id(x_telegram_init_data)
    normalized = plate_or_400(plate)
    result = {"plate": normalized, "display": format_plate(normalized), "status": "free", "request": None}
    truck = await db.find_truck_by_plate(normalized)
    if truck:
        if truck[1] == user_id:
            result["status"] = "mine"
        else:
            result["status"] = "taken"
            watch = await db.get_watch(truck[0], user_id)
            result["request"] = watch[1] if watch else None
    return result


@app.post("/api/trucks")
async def api_add_truck(payload: TruckIn, x_telegram_init_data: str = Header(default="")):
    user_id = await get_user_id(x_telegram_init_data)
    plate = plate_or_400(payload.plate)
    if await db.count_trucks(user_id) >= MAX_TRUCKS_PER_USER:
        raise HTTPException(400, f"Bitta akkauntga {MAX_TRUCKS_PER_USER} tagacha mashina qo'shish mumkin")
    name = format_plate(plate)
    truck_id = await db.add_truck(user_id, name, plate, payload.brand, payload.volume)
    if truck_id is None:
        raise HTTPException(409, "Bu raqamli mashina allaqachon qo'shilgan")
    return {"id": truck_id, "name": name, "plate": plate, "brand": payload.brand, "volume": payload.volume}


@app.delete("/api/trucks/{truck_id}")
async def api_delete_truck(truck_id: int, x_telegram_init_data: str = Header(default="")):
    user_id = await get_user_id(x_telegram_init_data)
    truck = await db.get_truck(user_id, truck_id)
    if not truck:
        raise HTTPException(404, "Fura topilmadi")
    await db.delete_truck(user_id, truck_id)
    return {"ok": True}


@app.get("/api/trucks/{truck_id}/expenses")
async def api_list_truck_expenses(truck_id: int, x_telegram_init_data: str = Header(default="")):
    user_id = await get_user_id(x_telegram_init_data)
    truck = await db.get_truck(user_id, truck_id)
    if not truck:
        raise HTTPException(404, "Fura topilmadi")
    rows = await db.list_truck_expenses(truck_id)
    return [
        {"id": r[0], "category": r[1], "amount": r[2], "note": r[3], "created_at": r[4]}
        for r in rows
    ]


@app.post("/api/trucks/{truck_id}/expenses")
async def api_add_truck_expense(
    truck_id: int, payload: TruckExpenseIn, x_telegram_init_data: str = Header(default="")
):
    user_id = await get_user_id(x_telegram_init_data)
    truck = await db.get_truck(user_id, truck_id)
    if not truck:
        raise HTTPException(404, "Fura topilmadi")
    expense_id = await db.add_truck_expense(truck_id, payload.category, payload.amount, payload.note)
    return {"id": expense_id}


@app.delete("/api/trucks/{truck_id}/expenses/{expense_id}")
async def api_delete_truck_expense(
    truck_id: int, expense_id: int, x_telegram_init_data: str = Header(default="")
):
    user_id = await get_user_id(x_telegram_init_data)
    truck = await db.get_truck(user_id, truck_id)
    if not truck:
        raise HTTPException(404, "Fura topilmadi")
    await db.delete_truck_expense(user_id, expense_id)
    return {"ok": True}


# ---------------- Trips ----------------
@app.get("/api/trips")
async def api_list_trips(x_telegram_init_data: str = Header(default="")):
    user_id = await get_user_id(x_telegram_init_data)
    rows = await db.list_trips(user_id)
    return [trip_row_to_dict(r) for r in rows]


@app.get("/api/trips/active")
async def api_active_trips(x_telegram_init_data: str = Header(default="")):
    user_id = await get_user_id(x_telegram_init_data)
    rows = await db.get_active_trips(user_id)
    return [{"id": r[0], "truck_id": r[1], "truck_name": r[2]} for r in rows]


@app.post("/api/trips")
async def api_create_trip(payload: TripIn, x_telegram_init_data: str = Header(default="")):
    user_id = await get_user_id(x_telegram_init_data)
    truck = await db.get_truck(user_id, payload.truck_id)
    if not truck:
        raise HTTPException(404, "Fura topilmadi")
    existing = await db.get_active_trip_for_truck(user_id, payload.truck_id)
    if existing:
        raise HTTPException(400, "Bu fura uchun allaqachon faol reys bor")
    trip_id = await db.create_trip(user_id, payload.truck_id)
    return {"id": trip_id}


@app.post("/api/trips/{trip_id}/finish")
async def api_finish_trip(trip_id: int, x_telegram_init_data: str = Header(default="")):
    user_id = await get_user_id(x_telegram_init_data)
    trip = await db.get_trip(user_id, trip_id)
    if not trip:
        raise HTTPException(404, "Reys topilmadi")
    await db.finish_trip(user_id, trip_id)
    return {"ok": True}


@app.delete("/api/trips/{trip_id}")
async def api_delete_trip(trip_id: int, x_telegram_init_data: str = Header(default="")):
    user_id = await get_user_id(x_telegram_init_data)
    trip = await db.get_trip(user_id, trip_id)
    if not trip:
        raise HTTPException(404, "Reys topilmadi")
    await db.delete_trip(user_id, trip_id)
    return {"ok": True}


@app.get("/api/trips/{trip_id}")
async def api_get_trip(trip_id: int, x_telegram_init_data: str = Header(default="")):
    user_id = await get_user_id(x_telegram_init_data)
    trip = await db.get_trip(user_id, trip_id)
    if not trip:
        raise HTTPException(404, "Reys topilmadi")
    return await trip_detail(trip)


def trip_row_to_dict(r):
    return {
        "id": r[0], "truck_id": r[1], "truck_name": r[2], "status": r[3],
        "created_at": r[4], "finished_at": r[5],
        "income": r[6], "expense": r[7], "profit": r[6] - r[7],
    }


async def trip_detail(trip):
    trip_id = trip[0]
    expenses = await db.list_trip_expenses(trip_id)
    legs = await db.list_trip_legs(trip_id)
    income = sum(l[3] for l in legs)
    expense = sum(e[2] for e in expenses)
    return {
        "id": trip[0],
        "truck_id": trip[1],
        "truck_name": trip[2],
        "status": trip[3],
        "created_at": trip[4],
        "finished_at": trip[5],
        "income": income,
        "expense": expense,
        "profit": income - expense,
        "expenses": [
            {"id": e[0], "category": e[1], "amount": e[2], "note": e[3], "created_at": e[4]}
            for e in expenses
        ],
        "legs": [
            {"id": l[0], "from_point": l[1], "to_point": l[2], "price": l[3], "created_at": l[4]}
            for l in legs
        ],
    }


@app.post("/api/trips/{trip_id}/expenses")
async def api_add_trip_expense(
    trip_id: int, payload: TripExpenseIn, x_telegram_init_data: str = Header(default="")
):
    user_id = await get_user_id(x_telegram_init_data)
    trip = await db.get_trip(user_id, trip_id)
    if not trip:
        raise HTTPException(404, "Reys topilmadi")
    expense_id = await db.add_trip_expense(trip_id, payload.category, payload.amount, payload.note)
    return {"id": expense_id}


@app.delete("/api/trips/{trip_id}/expenses/{expense_id}")
async def api_delete_trip_expense(
    trip_id: int, expense_id: int, x_telegram_init_data: str = Header(default="")
):
    user_id = await get_user_id(x_telegram_init_data)
    trip = await db.get_trip(user_id, trip_id)
    if not trip:
        raise HTTPException(404, "Reys topilmadi")
    await db.delete_trip_expense(user_id, expense_id)
    return {"ok": True}


@app.post("/api/trips/{trip_id}/legs")
async def api_add_trip_leg(
    trip_id: int, payload: TripLegIn, x_telegram_init_data: str = Header(default="")
):
    user_id = await get_user_id(x_telegram_init_data)
    trip = await db.get_trip(user_id, trip_id)
    if not trip:
        raise HTTPException(404, "Reys topilmadi")
    leg_id = await db.add_trip_leg(
        trip_id, payload.from_point, payload.to_point, payload.price
    )
    return {"id": leg_id}


@app.delete("/api/trips/{trip_id}/legs/{leg_id}")
async def api_delete_trip_leg(
    trip_id: int, leg_id: int, x_telegram_init_data: str = Header(default="")
):
    user_id = await get_user_id(x_telegram_init_data)
    trip = await db.get_trip(user_id, trip_id)
    if not trip:
        raise HTTPException(404, "Reys topilmadi")
    await db.delete_trip_leg(user_id, leg_id)
    return {"ok": True}


# ---------------- Report ----------------
@app.get("/api/report")
async def api_report(x_telegram_init_data: str = Header(default="")):
    user_id = await get_user_id(x_telegram_init_data)
    return await db.get_report(user_id)


# ---------------- Kuzatish (boshqa akkauntdan faqat ko'rish) ----------------
def person_name(full_name, username) -> str:
    name = full_name or "Foydalanuvchi"
    return f"{name} (@{username})" if username else name


async def notify(user_id: int, text: str, reply_markup=None):
    """Bot orqali xabar yuboradi. Foydalanuvchi botni bloklagan bo'lsa jim o'tib ketadi."""
    if not bot:
        return
    telegram_id = await db.get_telegram_id(user_id)
    if not telegram_id:
        return
    try:
        await bot.send_message(telegram_id, text, reply_markup=reply_markup)
    except Exception:
        logging.warning("Xabar yuborilmadi: user_id=%s", user_id, exc_info=True)


async def decide_request(owner_id: int, watch_id: int, approve: bool):
    """So'rovni hal qiladi va kuzatuvchiga xabar beradi. Fura nomini yoki None qaytaradi."""
    req = await db.get_watch_request_for_owner(owner_id, watch_id)
    if not req or not await db.decide_watch_request(owner_id, watch_id, approve):
        return None
    _, _, watcher_tg_id, truck_name = req
    text = (
        f"✅ {truck_name} mashinasini kuzatishga ruxsat berildi. "
        "Ilovadagi «Moshinalar» bo'limida ko'rishingiz mumkin."
        if approve else
        f"❌ {truck_name} mashinasi egasi kuzatish so'rovingizni rad etdi."
    )
    if bot:
        try:
            await bot.send_message(watcher_tg_id, text)
        except Exception:
            logging.warning("Kuzatuvchiga xabar yuborilmadi", exc_info=True)
    return truck_name


@app.get("/api/sharing")
async def api_sharing(x_telegram_init_data: str = Header(default="")):
    """Moshinalar sahifasi uchun: limit, kelgan so'rovlar va kuzatilayotgan mashinalar."""
    user_id = await get_user_id(x_telegram_init_data)
    incoming = await db.list_incoming_requests(user_id)
    watching = await db.list_watching(user_id)
    return {
        "max_trucks": MAX_TRUCKS_PER_USER,
        "incoming": [
            {"id": r[0], "truck_id": r[1], "truck_name": r[2],
             "watcher_name": person_name(r[3], r[4]), "created_at": r[5]}
            for r in incoming
        ],
        "watching": [
            {"id": r[0], "status": r[1], "truck_id": r[2], "truck_name": r[3],
             "brand": r[4], "volume": r[5]}
            for r in watching
        ],
    }


@app.post("/api/watch")
async def api_request_watch(payload: WatchIn, x_telegram_init_data: str = Header(default="")):
    user_id = await get_user_id(x_telegram_init_data)
    plate = plate_or_400(payload.plate)
    truck = await db.find_truck_by_plate(plate)
    if not truck:
        raise HTTPException(404, "Bu raqamli mashina topilmadi")
    truck_id, owner_id, truck_name = truck
    if owner_id == user_id:
        raise HTTPException(400, "Bu mashina o'zingizniki")
    existing = await db.get_watch(truck_id, user_id)
    if existing:
        if existing[1] == "rejected":
            raise HTTPException(403, "Mashina egasi so'rovingizni rad etgan")
        return {"id": existing[0], "status": existing[1]}

    watch_id = await db.create_watch_request(truck_id, user_id)
    me = await db.get_user_name(user_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Ruxsat berish", callback_data=f"watch:ok:{watch_id}"),
        InlineKeyboardButton(text="❌ Rad etish", callback_data=f"watch:no:{watch_id}"),
    ]])
    await notify(
        owner_id,
        f"👀 Kuzatish so'rovi\n\n{person_name(*me)} sizning {truck_name} raqamli "
        "mashinangizni kuzatmoqchi.\n\nRuxsat bersangiz, u bu mashinaning xarajatlari va "
        "reyslarini ko'ra oladi, lekin hech narsani o'zgartira olmaydi.",
        reply_markup=kb,
    )
    return {"id": watch_id, "status": "pending"}


@app.post("/api/watch/{watch_id}/approve")
async def api_approve_watch(watch_id: int, x_telegram_init_data: str = Header(default="")):
    user_id = await get_user_id(x_telegram_init_data)
    if not await decide_request(user_id, watch_id, approve=True):
        raise HTTPException(404, "So'rov topilmadi")
    return {"ok": True}


@app.post("/api/watch/{watch_id}/reject")
async def api_reject_watch(watch_id: int, x_telegram_init_data: str = Header(default="")):
    user_id = await get_user_id(x_telegram_init_data)
    if not await decide_request(user_id, watch_id, approve=False):
        raise HTTPException(404, "So'rov topilmadi")
    return {"ok": True}


@app.delete("/api/watch/{watch_id}")
async def api_delete_watch(watch_id: int, x_telegram_init_data: str = Header(default="")):
    """Kuzatuvchi kuzatishni to'xtatadi yoki egasi kuzatuvchini olib tashlaydi."""
    user_id = await get_user_id(x_telegram_init_data)
    if not await db.delete_watch(user_id, watch_id):
        raise HTTPException(404, "So'rov topilmadi")
    return {"ok": True}


@app.get("/api/trucks/{truck_id}/watchers")
async def api_truck_watchers(truck_id: int, x_telegram_init_data: str = Header(default="")):
    user_id = await get_user_id(x_telegram_init_data)
    if not await db.get_truck(user_id, truck_id):
        raise HTTPException(404, "Fura topilmadi")
    rows = await db.list_truck_watchers(user_id, truck_id)
    return [
        {"id": r[0], "name": person_name(r[1], r[2]), "status": r[3], "created_at": r[4]}
        for r in rows
    ]


async def watched_truck_or_404(user_id: int, truck_id: int):
    truck = await db.get_watched_truck(user_id, truck_id)
    if not truck:
        raise HTTPException(404, "Fura topilmadi")
    return truck


@app.get("/api/watched/{truck_id}")
async def api_watched_truck(truck_id: int, x_telegram_init_data: str = Header(default="")):
    user_id = await get_user_id(x_telegram_init_data)
    truck = await watched_truck_or_404(user_id, truck_id)
    repairs = await db.list_truck_expenses(truck_id)
    trips = [trip_row_to_dict(r) for r in await db.list_truck_trips(truck_id)]
    repair_total = sum(r[2] for r in repairs)
    income = sum(t["income"] for t in trips)
    trip_expense = sum(t["expense"] for t in trips)
    return {
        "watch_id": truck[6],
        "truck": {
            "id": truck[0], "name": truck[1], "plate": truck[2],
            "brand": truck[3], "volume": truck[4], "created_at": truck[5],
        },
        "income": income,
        "expense": trip_expense + repair_total,
        "repair_total": repair_total,
        "profit": income - trip_expense - repair_total,
        "repairs": [
            {"id": r[0], "category": r[1], "amount": r[2], "note": r[3], "created_at": r[4]}
            for r in repairs
        ],
        "trips": trips,
    }


@app.get("/api/watched/{truck_id}/trips/{trip_id}")
async def api_watched_trip(truck_id: int, trip_id: int, x_telegram_init_data: str = Header(default="")):
    user_id = await get_user_id(x_telegram_init_data)
    await watched_truck_or_404(user_id, truck_id)
    trip = await db.get_truck_trip(truck_id, trip_id)
    if not trip:
        raise HTTPException(404, "Reys topilmadi")
    return await trip_detail(trip)


# ---------------- Frontend ----------------
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.get("/")
async def serve_index():
    return FileResponse(
        os.path.join(STATIC_DIR, "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# ---------------- Bot ----------------
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
# Server har safar qayta ishga tushganda (har bir yangi deploy'da) yangi versiya
# belgisi qo'shiladi, shunda Telegram mini-ilovani "yangi" deb hisoblab,
# eski keshlangan nusxa o'rniga har doim so'nggi versiyani yuklaydi.
if WEBAPP_URL:
    _separator = "&" if "?" in WEBAPP_URL else "?"
    WEBAPP_URL_VERSIONED = f"{WEBAPP_URL}{_separator}v={int(time.time())}"
else:
    WEBAPP_URL_VERSIONED = ""


# Chat pastidagi doimiy "Ochish" tugmasi. Eski xostingdan qolgan chatlarda u eski
# manzilga olib borardi, umumiy (default) tugma esa Telegram'da qo'llanmadi —
# shuning uchun har bir chatga alohida o'rnatiladi.
MENU_BUTTON = (
    MenuButtonWebApp(text="Ochish", web_app=WebAppInfo(url=WEBAPP_URL_VERSIONED)) if WEBAPP_URL else None
)
_menu_set: set[int] = set()


async def ensure_menu(chat_id: int):
    if not MENU_BUTTON or chat_id in _menu_set:
        return
    try:
        await bot.set_chat_menu_button(chat_id=chat_id, menu_button=MENU_BUTTON)
        _menu_set.add(chat_id)
    except Exception:
        logging.warning("Menyu tugmasi o'rnatilmadi: chat %s", chat_id)


async def send_open(message: Message, text: str):
    if not WEBAPP_URL_VERSIONED:
        await message.answer("Bot sozlanmoqda, WEBAPP_URL hali berilmagan.")
        return
    await ensure_menu(message.chat.id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Ochish", web_app=WebAppInfo(url=WEBAPP_URL_VERSIONED))]]
    )
    await message.answer(text, reply_markup=kb)


@dp.message(CommandStart())
async def bot_start(message: Message):
    await send_open(message, "Assalomu alaykum! Fura reyslaringizni shu yerdan boshqaring:")


@dp.callback_query(F.data.startswith("watch:"))
async def bot_watch_decision(callback: CallbackQuery):
    """Egasi bot xabaridagi "Ruxsat berish" / "Rad etish" tugmasini bosganda."""
    try:
        _, action, watch_id = callback.data.split(":")
        watch_id = int(watch_id)
    except ValueError:
        return await callback.answer()
    owner_id = await db.get_user_by_telegram_id(callback.from_user.id)
    approve = action == "ok"
    truck_name = await decide_request(owner_id, watch_id, approve) if owner_id else None
    if not truck_name:
        await callback.answer("Bu so'rov allaqachon ko'rib chiqilgan yoki bekor qilingan", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return
    await callback.answer("Ruxsat berildi" if approve else "Rad etildi")
    await callback.message.edit_text(
        callback.message.text + ("\n\n✅ Ruxsat berildi" if approve else "\n\n❌ Rad etildi")
    )



@dp.message(F.chat.type == "private")
async def bot_any_message(message: Message):
    await send_open(message, "Ilovani ochish uchun pastdagi tugmani bosing:")


async def setup_menu_buttons():
    if not MENU_BUTTON:
        return
    try:
        await bot.set_chat_menu_button(menu_button=MENU_BUTTON)
    except Exception:
        logging.warning("Umumiy menyu tugmasi o'rnatilmadi")
    # Ilovadan foydalangan barcha foydalanuvchilarga ham to'g'ri tugma
    for telegram_id in await db.list_telegram_ids():
        await ensure_menu(telegram_id)
        await asyncio.sleep(0.05)


async def run_bot():
    if not bot:
        logging.warning("BOT_TOKEN berilmagan - bot ishga tushmaydi, faqat veb-server ishlaydi")
        return
    try:
        await setup_menu_buttons()
        await dp.start_polling(bot)
    except Exception:
        # Bot yiqilsa ham veb-server (Mini App) ishlashda davom etsin
        logging.exception("Bot to'xtadi")


async def run_web():
    port = int(os.getenv("PORT", 8000))
    config = uvicorn.Config(app, host=os.getenv("HOST", "0.0.0.0"), port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def main():
    await db.init_db()
    await asyncio.gather(run_web(), run_bot())


if __name__ == "__main__":
    asyncio.run(main())
