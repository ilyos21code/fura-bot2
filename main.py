import asyncio
import logging
import os
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints
import uvicorn

import database as db
from auth import validate_init_data, InitDataError

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DEV_MODE = os.getenv("DEV_MODE", "0") == "1"

app = FastAPI(title="Fura Mini App")


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
    return await db.get_or_create_user(
        telegram_id=user["id"], full_name=user.get("first_name", "")
    )


# ---------------- Pydantic modellar ----------------
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]
Money = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class TruckIn(BaseModel):
    name: Text


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
        {"id": r[0], "name": r[1], "created_at": r[2], "repair_total": r[3]}
        for r in rows
    ]


@app.post("/api/trucks")
async def api_add_truck(payload: TruckIn, x_telegram_init_data: str = Header(default="")):
    user_id = await get_user_id(x_telegram_init_data)
    truck_id = await db.add_truck(user_id, payload.name)
    return {"id": truck_id, "name": payload.name}


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
    return [
        {
            "id": r[0],
            "truck_id": r[1],
            "truck_name": r[2],
            "status": r[3],
            "created_at": r[4],
            "finished_at": r[5],
            "income": r[6],
            "expense": r[7],
            "profit": r[6] - r[7],
        }
        for r in rows
    ]


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


# ---------------- Frontend ----------------
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.get("/")
async def serve_index():
    return FileResponse(
        os.path.join(STATIC_DIR, "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


async def run_bot():
    if not BOT_TOKEN:
        logging.warning("BOT_TOKEN berilmagan - bot ishga tushmaydi, faqat veb-server ishlaydi")
        return
    try:
        await _run_bot()
    except Exception:
        # Bot yiqilsa ham veb-server (Mini App) ishlashda davom etsin
        logging.exception("Bot to'xtadi")


async def _run_bot():
    import time
    from aiogram import Bot, Dispatcher, F
    from aiogram.filters import CommandStart
    from aiogram.types import (
        Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, MenuButtonWebApp,
    )

    webapp_url = os.getenv("WEBAPP_URL", "")
    # Server har safar qayta ishga tushganda (har bir yangi deploy'da) yangi versiya
    # belgisi qo'shiladi, shunda Telegram mini-ilovani "yangi" deb hisoblab,
    # eski keshlangan nusxa o'rniga har doim so'nggi versiyani yuklaydi.
    version_tag = str(int(time.time()))
    if webapp_url:
        separator = "&" if "?" in webapp_url else "?"
        webapp_url_versioned = f"{webapp_url}{separator}v={version_tag}"
    else:
        webapp_url_versioned = webapp_url

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    # Chat pastidagi doimiy "Ochish" tugmasi. Eski xostingdan qolgan chatlarda
    # u eski manzilga olib borardi — shuning uchun har bir chatga alohida o'rnatiladi.
    menu_button = MenuButtonWebApp(text="Ochish", web_app=WebAppInfo(url=webapp_url_versioned)) if webapp_url else None
    menu_set: set[int] = set()

    async def ensure_menu(chat_id: int):
        if not menu_button or chat_id in menu_set:
            return
        try:
            await bot.set_chat_menu_button(chat_id=chat_id, menu_button=menu_button)
            menu_set.add(chat_id)
        except Exception:
            logging.warning("Menyu tugmasi o'rnatilmadi: chat %s", chat_id)

    async def send_open(message: Message, text: str):
        if not webapp_url_versioned:
            await message.answer("Bot sozlanmoqda, WEBAPP_URL hali berilmagan.")
            return
        await ensure_menu(message.chat.id)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Ochish", web_app=WebAppInfo(url=webapp_url_versioned))]]
        )
        await message.answer(text, reply_markup=kb)

    @dp.message(CommandStart())
    async def start(message: Message):
        await send_open(message, "Assalomu alaykum! Fura reyslaringizni shu yerdan boshqaring:")

    @dp.message(F.chat.type == "private")
    async def any_message(message: Message):
        await send_open(message, "Ilovani ochish uchun pastdagi tugmani bosing:")

    if menu_button:
        try:
            await bot.set_chat_menu_button(menu_button=menu_button)
        except Exception:
            logging.warning("Umumiy menyu tugmasi o'rnatilmadi")
        # Ilovadan foydalangan barcha foydalanuvchilarga ham to'g'ri tugma
        for telegram_id in await db.list_telegram_ids():
            await ensure_menu(telegram_id)
            await asyncio.sleep(0.05)

    await dp.start_polling(bot)


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
