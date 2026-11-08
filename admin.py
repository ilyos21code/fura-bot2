"""Admin panel (/admin): foydalanuvchilar soni, faolligi va ularning ma'lumotlari.

Faqat ADMIN_TELEGRAM_ID dagi akkaunt(lar) ko'radi (vergul bilan bir nechta bo'lishi
mumkin). Boshqalar uchun /admin oddiy xabar kabi javob oladi — buyruq borligi
oshkor qilinmaydi.
"""
import io
import os
from datetime import datetime, timedelta
from html import escape

import database as db


def parse_admin_ids(raw: str) -> frozenset[int]:
    ids = set()
    for part in (raw or "").replace(" ", "").split(","):
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return frozenset(ids)


ADMIN_IDS = parse_admin_ids(os.getenv("ADMIN_TELEGRAM_ID", ""))
RECENT_USERS_SHOWN = 15
TASHKENT_OFFSET = timedelta(hours=5)   # bazadagi vaqtlar UTC


def local_time(utc_text):
    """'2026-11-07 06:30:00' (UTC) -> '07.11.2026 11:30' (Toshkent)."""
    if not utc_text:
        return ""
    try:
        dt = datetime.strptime(utc_text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return utc_text
    return (dt + TASHKENT_OFFSET).strftime("%d.%m.%Y %H:%M")


def money(amount) -> str:
    return f"{round(amount or 0):,}".replace(",", " ")


def stats_text(s: dict) -> str:
    return (
        "📊 <b>DEPO HisobKitob — statistika</b>\n\n"
        f"👤 Foydalanuvchilar: <b>{s['users']}</b>\n"
        f"   🆕 yangi: bugun {s['new_1d']} · 7 kun {s['new_7d']} · 30 kun {s['new_30d']}\n"
        f"   🔥 faol: bugun {s['active_1d']} · 7 kun {s['active_7d']} · 30 kun {s['active_30d']}\n"
        f"   🚛 mashina qo'shgan: {s['users_with_trucks']} · reys ochgan: {s['users_with_trips']}\n\n"
        f"🚛 Mashinalar: <b>{s['trucks']}</b>\n"
        f"🧭 Reyslar: <b>{s['trips']}</b> (faol: {s['active_trips']}, 7 kunda: {s['trips_7d']})\n"
        f"👁 Kuzatuvchilar: {s['watchers']} · 🔔 qimmat yuk xabarnomasi: {s['load_alerts']}\n\n"
        "<i>\"Faol\" — ilovani ochgan yoki reys/xarajat kiritgan. Bugun — oxirgi 24 soat.</i>"
    )


def _person(full_name, username) -> str:
    name = escape(full_name or "—")
    return f"{name} (@{escape(username)})" if username else name


def recent_users_text(rows) -> str:
    """admin_users() qatorlaridan oxirgi faollar ro'yxati."""
    if not rows:
        return "Hali foydalanuvchi yo'q."
    lines = [f"👤 <b>Oxirgi faol {min(len(rows), RECENT_USERS_SHOWN)} foydalanuvchi</b> "
             f"(jami {len(rows)})\n"]
    for (_, _, full_name, username, first_seen, last_seen,
         trucks, trips, *_rest) in rows[:RECENT_USERS_SHOWN]:
        lines.append(f"• {_person(full_name, username)}\n"
                     f"   🚛 {trucks} · 🧭 {trips} · oxirgi: {local_time(last_seen) or '—'}"
                     f" · qo'shilgan: {local_time(first_seen) or '—'}")
    lines.append("\nHammasi — 📥 Excel tugmasida.")
    return "\n".join(lines)


def _sheet(wb, title, headers, rows, first=False):
    from openpyxl.styles import Font
    ws = wb.active if first else wb.create_sheet()
    ws.title = title
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in rows:
        ws.append(row)
    for col in ws.columns:
        width = max(len(str(c.value)) for c in col if c.value is not None) if col else 10
        ws.column_dimensions[col[0].column_letter].width = min(width + 2, 45)
    ws.freeze_panes = "A2"


def build_excel(users, trucks, trips) -> bytes:
    """Uch varaqli .xlsx: foydalanuvchilar, mashinalar, reyslar (summalar so'mda)."""
    from openpyxl import Workbook   # faqat eksportda yuklanadi — bot xotirasini band qilmasin
    wb = Workbook()
    _sheet(wb, "Foydalanuvchilar",
           ["ID", "Telegram ID", "Ism", "Username", "Qo'shilgan", "Oxirgi faollik",
            "Mashinalar", "Reyslar", "Faol reys", "Daromad", "Reys xarajati", "Ta'mirlash",
            "Sof foyda"],
           [(uid, tg, name, f"@{un}" if un else "", local_time(first), local_time(last),
             trk, trp, act, round(inc), round(texp), round(rep), round(inc - texp - rep))
            for uid, tg, name, un, first, last, trk, trp, act, inc, texp, rep in users],
           first=True)
    _sheet(wb, "Mashinalar",
           ["ID", "Egasi", "Username", "Davlat raqami", "Marka", "Hajm (m³)", "Qo'shilgan",
            "Reyslar", "Ta'mirlash", "Kuzatuvchilar"],
           [(tid, name, f"@{un}" if un else "", plate, brand, vol, local_time(created),
             trp, round(rep), watchers)
            for tid, name, un, plate, brand, vol, created, trp, rep, watchers in trucks])
    _sheet(wb, "Reyslar",
           ["ID", "Egasi", "Username", "Mashina", "Holati", "Boshlangan", "Tugagan", "Yo'nalish",
            "Daromad", "Xarajat", "Foyda"],
           [(rid, name, f"@{un}" if un else "", truck, "faol" if status == "active" else "tugagan",
             local_time(created), local_time(finished), route or "", round(inc), round(exp),
             round(inc - exp))
            for rid, name, un, truck, status, created, finished, route, inc, exp in trips])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def export_excel() -> bytes:
    return build_excel(await db.admin_users(), await db.admin_trucks(), await db.admin_trips())
