import asyncio
import logging
import os
import re
import time
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramForbiddenError
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
import market
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


OptionalText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]


class TripLegIn(BaseModel):
    from_point: Text
    to_point: Text
    price: Money
    # Manzildan chiqadigan qimmat yuklarni bot chatiga yuborish (ixtiyoriy)
    alert: bool = False
    next_point: OptionalText = ""


class LegAlertIn(BaseModel):
    next_point: OptionalText = ""


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
    alerts = await db.list_trip_alerts(trip_id)
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
            {
                "id": l[0], "from_point": l[1], "to_point": l[2], "price": l[3], "created_at": l[4],
                "alert": (
                    {"id": alerts[l[0]][0], "from_point": alerts[l[0]][1], "next_point": alerts[l[0]][2] or ""}
                    if l[0] in alerts else None
                ),
            }
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
    if payload.alert:
        if trip[3] != "active":
            raise HTTPException(400, "Reys yakunlangan")
        await enable_alert(user_id, trip_id, leg_id, payload.to_point, payload.next_point)
    return {"id": leg_id}


@app.post("/api/trips/{trip_id}/legs/{leg_id}/alert")
async def api_enable_leg_alert(
    trip_id: int, leg_id: int, payload: LegAlertIn, x_telegram_init_data: str = Header(default="")
):
    user_id = await get_user_id(x_telegram_init_data)
    trip = await db.get_trip(user_id, trip_id)
    if not trip:
        raise HTTPException(404, "Reys topilmadi")
    if trip[3] != "active":
        raise HTTPException(400, "Reys yakunlangan")
    leg = await db.get_trip_leg(trip_id, leg_id)
    if not leg:
        raise HTTPException(404, "Safar topilmadi")
    await enable_alert(user_id, trip_id, leg_id, leg[2], payload.next_point)
    return {"ok": True}


@app.delete("/api/trips/{trip_id}/legs/{leg_id}/alert")
async def api_disable_leg_alert(trip_id: int, leg_id: int, x_telegram_init_data: str = Header(default="")):
    user_id = await get_user_id(x_telegram_init_data)
    if not await db.get_trip(user_id, trip_id):
        raise HTTPException(404, "Reys topilmadi")
    await db.disable_leg_alert(user_id, leg_id)
    return {"ok": True}


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


# ---------------- Bozor narxlari (Qimmatyuklar bazasidan) ----------------
_route_cache: dict = {}
ROUTE_CACHE_SEC = 300


@app.get("/api/market/status")
async def api_market_status(x_telegram_init_data: str = Header(default="")):
    await get_user_id(x_telegram_init_data)
    return {"available": market.available()}


@app.get("/api/market/route")
async def api_market_route(
    from_point: str = Query(alias="from", max_length=200),
    to_point: str = Query(alias="to", max_length=200),
    x_telegram_init_data: str = Header(default=""),
):
    """Safar qo'shishda: shu yo'nalishda oxirgi kunlarda yuklar o'rtacha necha pul bo'lgan."""
    await get_user_id(x_telegram_init_data)
    key = (from_point.strip().lower(), to_point.strip().lower())
    cached = _route_cache.get(key)
    if cached and time.time() - cached[0] < ROUTE_CACHE_SEC:
        return cached[1]
    try:
        stats = await asyncio.to_thread(market.route_stats, from_point, to_point)
    except Exception:
        logging.exception("Bozor narxi hisoblanmadi")
        return {"available": False}
    if len(_route_cache) > 1000:
        _route_cache.clear()
    _route_cache[key] = (time.time(), stats)
    return stats


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


# ---------------- Qimmat yuklarni bot chatiga yuborish ----------------
ALERT_CHECK_SEC = int(os.getenv("ALERT_CHECK_SEC", "180"))
MAX_LOADS_PER_CHECK = 5   # bitta tekshiruvda bitta foydalanuvchiga ko'pi bilan shuncha yuk
TASHKENT_TZ = ZoneInfo("Asia/Tashkent")


def fmt_money(amount, currency) -> str:
    n = f"{round(amount):,}".replace(",", " ")
    if currency == "UZS":
        return f"{n} so'm"
    if currency == "USD":
        return f"${n}"
    if currency == "RUB":
        return f"{n} rubl"
    return f"{n} {currency}"


def fmt_phone(phone) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 9:
        digits = "998" + digits
    if len(digits) == 12 and digits.startswith("998"):
        return f"+998 {digits[3:5]} {digits[5:8]} {digits[8:10]} {digits[10:]}"
    return f"+{digits}" if digits else ""


def alert_route_text(from_point: str, next_point: str) -> str:
    return f"{from_point} → {next_point}" if next_point else f"{from_point}dan"


def stop_alert_button(alert_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(text="🔕 To'xtatish", callback_data=f"alert:off:{alert_id}")


def load_message(load: dict, alert_id: int):
    pct = round(load["pct"])
    above = "o'rtacha narxda" if pct < 1 else f"o'rtachadan {pct}% qimmat"
    details = [d for d in [
        load.get("cargo"),
        f"{load['tons']:g} t" if load.get("tons") else None,
        f"{load['volume']} kub" if load.get("volume") else None,
        f"{load['km']} km" if load.get("km") else None,
    ] if d]
    posted = datetime.fromtimestamp(load["ts"], TASHKENT_TZ)
    minutes = max(0, int((time.time() - load["ts"]) // 60))
    lines = [
        f"🔥 Qimmat yuk: {load['from']} → {load['to']}",
        f"💰 {fmt_money(load['price'], load['currency'])} — {above}",
        f"📊 Bozor o'rtachasi: {fmt_money(load['avg_price'], load['currency'])} "
        f"({load['avg_count']} ta yuk, {market.STATS_DAYS} kun)",
    ]
    if details:
        lines.append("📦 " + " · ".join(str(d) for d in details))
    if load.get("phone"):
        lines.append(f"📞 {fmt_phone(load['phone'])}")
    lines.append(f"🕒 {posted:%H:%M} ({minutes} daqiqa oldin)")
    buttons = []
    if load.get("link"):
        buttons.append(InlineKeyboardButton(text="📄 E'lonni ochish", url=load["link"]))
    buttons.append(stop_alert_button(alert_id))
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=[buttons])


_alert_lock = asyncio.Lock()   # fon tekshiruvi va yoqilgandagi darhol tekshiruv bir yukni ikki marta yubormasin
_background_tasks: set = set()


async def check_alert(alert) -> int:
    """Bitta yuborish uchun yangi qimmat yuklarni topib, bot chatiga tashlaydi. Nechta yuborilganini qaytaradi."""
    async with _alert_lock:
        return await _check_alert(alert)


async def _check_alert(alert) -> int:
    alert_id, user_id, telegram_id, from_point, next_point, volume, _ = alert
    loads = await asyncio.to_thread(market.find_expensive_loads, from_point, next_point, volume)
    if not loads:
        return 0
    unsent = await db.unsent_load_ids(user_id, [l["id"] for l in loads])
    fresh = [l for l in loads if l["id"] in unsent][:MAX_LOADS_PER_CHECK]
    sent = []
    for load in fresh:
        text, kb = load_message(load, alert_id)
        try:
            await bot.send_message(telegram_id, text, reply_markup=kb, disable_web_page_preview=True)
            sent.append(load["id"])
        except TelegramForbiddenError:
            # Foydalanuvchi botni bloklagan — behuda urinmaymiz
            await db.disable_alert(user_id, alert_id)
            break
        except Exception:
            logging.warning("Qimmat yuk yuborilmadi: user_id=%s", user_id, exc_info=True)
            break
        await asyncio.sleep(0.5)
    if sent:
        await db.mark_loads_sent(user_id, sent)
    return len(sent)


async def enable_alert(user_id: int, trip_id: int, leg_id: int, from_point: str, next_point: str):
    alert_id = await db.set_leg_alert(user_id, trip_id, leg_id, from_point, next_point)
    alert = await db.get_active_alert(alert_id)
    if not alert or not bot:
        return
    volume = alert[5]
    text = (
        f"🔔 {alert_route_text(from_point, next_point)} qimmat yuklar kuzatilmoqda.\n\n"
        "Narxi bozordagi o'rtacha narxga teng yoki undan yuqori"
        + (f", {volume:g} kub mashinangizga mos" if volume else "")
        + " yangi yuklar shu chatga yuborib turiladi.\n"
        "Reys yakunlanganda kuzatish o'zi to'xtaydi."
    )
    if not market.available():
        text += "\n\n⚠️ Hozircha yuklar bazasiga ulanib bo'lmadi — ulanishi bilan yuborila boshlaydi."
    await notify(user_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[stop_alert_button(alert_id)]]))
    # Hozir bor yuklarni darhol yuboramiz (keyingi tekshiruvni kutmasdan)
    task = asyncio.create_task(_check_alert_safe(alert))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _check_alert_safe(alert):
    try:
        await check_alert(alert)
    except Exception:
        logging.exception("Qimmat yuklar tekshiruvi xatosi: alert %s", alert[0])


async def run_alerts():
    if not bot:
        return
    if not market.YUK_DB_PATH:
        logging.info("YUK_DB_PATH berilmagan — qimmat yuklarni yuborish o'chiq")
        return
    while True:
        await asyncio.sleep(ALERT_CHECK_SEC)
        try:
            alerts = await db.list_active_alerts()
        except Exception:
            logging.exception("Yuborishlar ro'yxati o'qilmadi")
            continue
        for alert in alerts:
            await _check_alert_safe(alert)


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



@dp.callback_query(F.data.startswith("alert:off:"))
async def bot_alert_off(callback: CallbackQuery):
    """Qimmat yuk xabaridagi "To'xtatish" tugmasi."""
    try:
        alert_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        return await callback.answer()
    user_id = await db.get_user_by_telegram_id(callback.from_user.id)
    if user_id and await db.disable_alert(user_id, alert_id):
        await callback.answer("Qimmat yuklarni yuborish to'xtatildi", show_alert=True)
    else:
        await callback.answer("Allaqachon to'xtatilgan")


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
    await asyncio.gather(run_web(), run_bot(), run_alerts())


if __name__ == "__main__":
    asyncio.run(main())
