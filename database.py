import os

import aiosqlite

# Hostingda doimiy diskka yo'naltirish uchun: DB_PATH=/var/data/fura.db
DB_PATH = os.getenv("DB_PATH", "fura.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE NOT NULL,
    full_name TEXT
);

CREATE TABLE IF NOT EXISTS trucks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS truck_expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    truck_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    amount REAL NOT NULL,
    note TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (truck_id) REFERENCES trucks(id)
);

CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    truck_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now')),
    finished_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (truck_id) REFERENCES trucks(id)
);

CREATE TABLE IF NOT EXISTS trip_expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    amount REAL NOT NULL,
    note TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (trip_id) REFERENCES trips(id)
);

CREATE TABLE IF NOT EXISTS trip_legs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL,
    from_point TEXT NOT NULL,
    to_point TEXT NOT NULL,
    price REAL NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (trip_id) REFERENCES trips(id)
);

-- Boshqa akkauntlar mashinani kuzatishi (faqat ko'rish). status: pending / approved / rejected
CREATE TABLE IF NOT EXISTS truck_watchers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    truck_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT DEFAULT (datetime('now')),
    decided_at TEXT,
    UNIQUE (truck_id, user_id),
    FOREIGN KEY (truck_id) REFERENCES trucks(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Safar manzilidan chiqadigan qimmat yuklarni bot chatiga yuborish (bitta reysda bittasi faol)
CREATE TABLE IF NOT EXISTS load_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    trip_id INTEGER NOT NULL,
    leg_id INTEGER NOT NULL UNIQUE,
    from_point TEXT NOT NULL,
    next_point TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (trip_id) REFERENCES trips(id),
    FOREIGN KEY (leg_id) REFERENCES trip_legs(id)
);

-- Foydalanuvchiga yuborilgan yuklar (bir yuk ikki marta yuborilmasin). load_id — yuk.db dagi id
CREATE TABLE IF NOT EXISTS load_alert_sent (
    user_id INTEGER NOT NULL,
    load_id INTEGER NOT NULL,
    sent_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, load_id)
);
"""

# Eski bazalarga keyin qo'shilgan ustunlar: (jadval, ustun, turi)
MIGRATIONS = [
    ("users", "username", "TEXT"),
    ("trucks", "plate", "TEXT"),
    ("trucks", "brand", "TEXT"),
    ("trucks", "volume", "REAL"),
]


async def init_db():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript(SCHEMA)
        for table, column, col_type in MIGRATIONS:
            cur = await conn.execute(f"PRAGMA table_info({table})")
            if column not in [r[1] for r in await cur.fetchall()]:
                await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        # Bitta davlat raqami butun bazada faqat bir marta bo'lishi mumkin
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_trucks_plate ON trucks(plate) WHERE plate IS NOT NULL"
        )
        await conn.commit()


async def get_or_create_user(telegram_id: int, full_name: str = "", username: str = "") -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT id, full_name, username FROM users WHERE telegram_id=?", (telegram_id,)
        )
        row = await cur.fetchone()
        if row:
            # Ism yoki username o'zgargan bo'lsa yangilab qo'yamiz (so'rovlarda ko'rinadi)
            if (row[1] or "", row[2] or "") != (full_name, username):
                await conn.execute(
                    "UPDATE users SET full_name=?, username=? WHERE id=?",
                    (full_name, username, row[0]),
                )
                await conn.commit()
            return row[0]
        cur = await conn.execute(
            "INSERT INTO users (telegram_id, full_name, username) VALUES (?, ?, ?)",
            (telegram_id, full_name, username),
        )
        await conn.commit()
        return cur.lastrowid


async def list_telegram_ids() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT telegram_id FROM users")
        return [r[0] for r in await cur.fetchall()]


async def get_telegram_id(user_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT telegram_id FROM users WHERE id=?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else None


async def get_user_name(user_id: int):
    """(full_name, username)"""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT full_name, username FROM users WHERE id=?", (user_id,))
        return await cur.fetchone() or ("", "")


async def get_user_by_telegram_id(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT id FROM users WHERE telegram_id=?", (telegram_id,))
        row = await cur.fetchone()
        return row[0] if row else None


# ---------------- Trucks ----------------
async def add_truck(user_id: int, name: str, plate: str, brand: str, volume: float):
    """Yangi fura qo'shadi. Raqam allaqachon band bo'lsa None qaytaradi."""
    async with aiosqlite.connect(DB_PATH) as conn:
        try:
            cur = await conn.execute(
                "INSERT INTO trucks (user_id, name, plate, brand, volume) VALUES (?, ?, ?, ?, ?)",
                (user_id, name, plate, brand, volume),
            )
        except aiosqlite.IntegrityError:
            return None
        await conn.commit()
        return cur.lastrowid


async def count_trucks(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM trucks WHERE user_id=?", (user_id,))
        return (await cur.fetchone())[0]


async def find_truck_by_plate(plate: str):
    """(truck_id, egasining user_id, nomi) yoki None."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT id, user_id, name FROM trucks WHERE plate=?", (plate,))
        return await cur.fetchone()


async def list_trucks(user_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """SELECT t.id, t.name, t.created_at,
                      COALESCE((SELECT SUM(amount) FROM truck_expenses WHERE truck_id=t.id), 0) as repair_total,
                      t.plate, t.brand, t.volume
               FROM trucks t WHERE t.user_id=? ORDER BY t.created_at DESC""",
            (user_id,),
        )
        return await cur.fetchall()


async def get_truck(user_id: int, truck_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT id, name FROM trucks WHERE id=? AND user_id=?", (truck_id, user_id)
        )
        return await cur.fetchone()


async def delete_truck(user_id: int, truck_id: int):
    """Furani uning ta'mirlash xarajatlari va barcha reyslari bilan birga o'chiradi."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT id FROM trucks WHERE id=? AND user_id=?", (truck_id, user_id)
        )
        if not await cur.fetchone():
            return
        trip_ids = "SELECT id FROM trips WHERE truck_id=? AND user_id=?"
        await conn.execute(f"DELETE FROM trip_expenses WHERE trip_id IN ({trip_ids})", (truck_id, user_id))
        await conn.execute(f"DELETE FROM trip_legs WHERE trip_id IN ({trip_ids})", (truck_id, user_id))
        await conn.execute(f"DELETE FROM load_alerts WHERE trip_id IN ({trip_ids})", (truck_id, user_id))
        await conn.execute("DELETE FROM trips WHERE truck_id=? AND user_id=?", (truck_id, user_id))
        await conn.execute("DELETE FROM truck_expenses WHERE truck_id=?", (truck_id,))
        await conn.execute("DELETE FROM truck_watchers WHERE truck_id=?", (truck_id,))
        await conn.execute("DELETE FROM trucks WHERE id=? AND user_id=?", (truck_id, user_id))
        await conn.commit()


async def add_truck_expense(truck_id: int, category: str, amount: float, note: str = ""):
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO truck_expenses (truck_id, category, amount, note) VALUES (?, ?, ?, ?)",
            (truck_id, category, amount, note),
        )
        await conn.commit()
        return cur.lastrowid


async def list_truck_expenses(truck_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT id, category, amount, note, created_at FROM truck_expenses WHERE truck_id=? ORDER BY created_at DESC",
            (truck_id,),
        )
        return await cur.fetchall()


async def delete_truck_expense(user_id: int, expense_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """DELETE FROM truck_expenses WHERE id=? AND truck_id IN
               (SELECT id FROM trucks WHERE user_id=?)""",
            (expense_id, user_id),
        )
        await conn.commit()


# ---------------- Trips ----------------
async def create_trip(user_id: int, truck_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO trips (user_id, truck_id, status) VALUES (?, ?, 'active')",
            (user_id, truck_id),
        )
        await conn.commit()
        return cur.lastrowid


async def get_active_trips(user_id: int):
    """Foydalanuvchining BARCHA faol reyslarini qaytaradi (har fura o'z reysiga ega bo'lishi mumkin)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """SELECT tr.id, tr.truck_id, t.name FROM trips tr
               JOIN trucks t ON t.id = tr.truck_id
               WHERE tr.user_id=? AND tr.status='active'
               ORDER BY tr.created_at DESC""",
            (user_id,),
        )
        return await cur.fetchall()


async def get_active_trip_for_truck(user_id: int, truck_id: int):
    """Berilgan furaning o'zida faol reys bor-yo'qligini tekshiradi."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """SELECT id FROM trips
               WHERE user_id=? AND truck_id=? AND status='active'
               LIMIT 1""",
            (user_id, truck_id),
        )
        return await cur.fetchone()


async def finish_trip(user_id: int, trip_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE trips SET status='finished', finished_at=datetime('now') WHERE id=? AND user_id=?",
            (trip_id, user_id),
        )
        # Reys tugadi — qimmat yuklarni yuborish ham to'xtaydi
        await conn.execute("UPDATE load_alerts SET active=0 WHERE trip_id=? AND user_id=?", (trip_id, user_id))
        await conn.commit()


async def delete_trip(user_id: int, trip_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT id FROM trips WHERE id=? AND user_id=?", (trip_id, user_id))
        if not await cur.fetchone():
            return
        await conn.execute("DELETE FROM trip_expenses WHERE trip_id=?", (trip_id,))
        await conn.execute("DELETE FROM trip_legs WHERE trip_id=?", (trip_id,))
        await conn.execute("DELETE FROM load_alerts WHERE trip_id=?", (trip_id,))
        await conn.execute("DELETE FROM trips WHERE id=? AND user_id=?", (trip_id, user_id))
        await conn.commit()


async def delete_trip_expense(user_id: int, expense_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """DELETE FROM trip_expenses WHERE id=? AND trip_id IN
               (SELECT id FROM trips WHERE user_id=?)""",
            (expense_id, user_id),
        )
        await conn.commit()


async def delete_trip_leg(user_id: int, leg_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """DELETE FROM trip_legs WHERE id=? AND trip_id IN
               (SELECT id FROM trips WHERE user_id=?)""",
            (leg_id, user_id),
        )
        if cur.rowcount:
            await conn.execute("DELETE FROM load_alerts WHERE leg_id=? AND user_id=?", (leg_id, user_id))
        await conn.commit()


async def list_trips(user_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """SELECT tr.id, tr.truck_id, t.name, tr.status, tr.created_at, tr.finished_at,
                      COALESCE((SELECT SUM(price) FROM trip_legs WHERE trip_id=tr.id), 0) as income,
                      COALESCE((SELECT SUM(amount) FROM trip_expenses WHERE trip_id=tr.id), 0) as expense
               FROM trips tr JOIN trucks t ON t.id = tr.truck_id
               WHERE tr.user_id=?
               ORDER BY tr.created_at DESC""",
            (user_id,),
        )
        return await cur.fetchall()


async def get_trip(user_id: int, trip_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """SELECT tr.id, tr.truck_id, t.name, tr.status, tr.created_at, tr.finished_at
               FROM trips tr JOIN trucks t ON t.id = tr.truck_id
               WHERE tr.id=? AND tr.user_id=?""",
            (trip_id, user_id),
        )
        return await cur.fetchone()


async def add_trip_expense(trip_id: int, category: str, amount: float, note: str = ""):
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO trip_expenses (trip_id, category, amount, note) VALUES (?, ?, ?, ?)",
            (trip_id, category, amount, note),
        )
        await conn.commit()
        return cur.lastrowid


async def list_trip_expenses(trip_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT id, category, amount, note, created_at FROM trip_expenses WHERE trip_id=? ORDER BY created_at DESC",
            (trip_id,),
        )
        return await cur.fetchall()


async def add_trip_leg(trip_id: int, from_point: str, to_point: str, price: float):
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO trip_legs (trip_id, from_point, to_point, price) VALUES (?, ?, ?, ?)",
            (trip_id, from_point, to_point, price),
        )
        await conn.commit()
        return cur.lastrowid


async def get_trip_leg(trip_id: int, leg_id: int):
    """(id, from_point, to_point) yoki None."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT id, from_point, to_point FROM trip_legs WHERE id=? AND trip_id=?", (leg_id, trip_id)
        )
        return await cur.fetchone()


async def list_trip_legs(trip_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT id, from_point, to_point, price, created_at FROM trip_legs WHERE trip_id=? ORDER BY created_at DESC",
            (trip_id,),
        )
        return await cur.fetchall()


# ---------------- Report ----------------
async def get_report(user_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        # Umumiy summalar (o'chirilgan furalarning eski reyslari hisobga olinmaydi)
        own_trips = """SELECT tr.id FROM trips tr JOIN trucks t ON t.id = tr.truck_id
                       WHERE tr.user_id=?"""
        cur = await conn.execute(
            f"SELECT COALESCE(SUM(price),0) FROM trip_legs WHERE trip_id IN ({own_trips})",
            (user_id,),
        )
        total_income = (await cur.fetchone())[0]

        cur = await conn.execute(
            f"SELECT COALESCE(SUM(amount),0) FROM trip_expenses WHERE trip_id IN ({own_trips})",
            (user_id,),
        )
        total_trip_expense = (await cur.fetchone())[0]

        cur = await conn.execute(
            """SELECT COALESCE(SUM(amount),0) FROM truck_expenses
               WHERE truck_id IN (SELECT id FROM trucks WHERE user_id=?)""",
            (user_id,),
        )
        total_repair = (await cur.fetchone())[0]

        # Oylar kesimida (reys boshlangan sana bo'yicha): avval har bir reys alohida
        # hisoblanadi, keyin oy bo'yicha jamlanadi
        cur = await conn.execute(
            """SELECT strftime('%Y-%m', tr.created_at) as month,
                      SUM(COALESCE((SELECT SUM(price) FROM trip_legs WHERE trip_id=tr.id),0)) as income,
                      SUM(COALESCE((SELECT SUM(amount) FROM trip_expenses WHERE trip_id=tr.id),0)) as expense
               FROM trips tr
               JOIN trucks t ON t.id = tr.truck_id
               WHERE tr.user_id=?
               GROUP BY month ORDER BY month DESC""",
            (user_id,),
        )
        by_month = await cur.fetchall()

        # By truck
        cur = await conn.execute(
            """SELECT t.id, t.name,
                      COALESCE((SELECT SUM(l.price) FROM trip_legs l JOIN trips tr ON tr.id=l.trip_id WHERE tr.truck_id=t.id),0) as income,
                      COALESCE((SELECT SUM(e.amount) FROM trip_expenses e JOIN trips tr ON tr.id=e.trip_id WHERE tr.truck_id=t.id),0) as trip_expense,
                      COALESCE((SELECT SUM(amount) FROM truck_expenses WHERE truck_id=t.id),0) as repair
               FROM trucks t WHERE t.user_id=?""",
            (user_id,),
        )
        by_truck = await cur.fetchall()

        return {
            "total_income": total_income,
            "total_expense": total_trip_expense + total_repair,
            "total_trip_expense": total_trip_expense,
            "total_repair": total_repair,
            "net_profit": total_income - total_trip_expense - total_repair,
            "by_month": [
                {"month": m, "income": i, "expense": e, "profit": i - e}
                for m, i, e in by_month
            ],
            "by_truck": [
                {
                    "id": tid,
                    "name": name,
                    "income": income,
                    "expense": trip_exp + repair,
                    "profit": income - trip_exp - repair,
                }
                for tid, name, income, trip_exp, repair in by_truck
            ],
        }


# ---------------- Kuzatish (boshqa akkauntdan faqat ko'rish) ----------------
async def get_watch(truck_id: int, user_id: int):
    """(id, status) yoki None."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT id, status FROM truck_watchers WHERE truck_id=? AND user_id=?",
            (truck_id, user_id),
        )
        return await cur.fetchone()


async def create_watch_request(truck_id: int, user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO truck_watchers (truck_id, user_id, status) VALUES (?, ?, 'pending')",
            (truck_id, user_id),
        )
        await conn.commit()
        return cur.lastrowid


async def get_watch_request_for_owner(owner_id: int, watch_id: int):
    """So'rov shu egaga tegishli bo'lsa: (id, status, kuzatuvchining telegram_id, fura nomi)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """SELECT w.id, w.status, u.telegram_id, t.name
               FROM truck_watchers w
               JOIN trucks t ON t.id = w.truck_id
               JOIN users u ON u.id = w.user_id
               WHERE w.id=? AND t.user_id=?""",
            (watch_id, owner_id),
        )
        return await cur.fetchone()


async def decide_watch_request(owner_id: int, watch_id: int, approve: bool) -> bool:
    """Faqat kutilayotgan so'rovni hal qiladi. Muvaffaqiyatli bo'lsa True."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """UPDATE truck_watchers SET status=?, decided_at=datetime('now')
               WHERE id=? AND status='pending'
                 AND truck_id IN (SELECT id FROM trucks WHERE user_id=?)""",
            ("approved" if approve else "rejected", watch_id, owner_id),
        )
        await conn.commit()
        return cur.rowcount > 0


async def delete_watch(user_id: int, watch_id: int) -> bool:
    """Kuzatuvchi o'zi chiqib ketishi yoki mashina egasi uni olib tashlashi mumkin."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """DELETE FROM truck_watchers WHERE id=?
               AND (user_id=? OR truck_id IN (SELECT id FROM trucks WHERE user_id=?))""",
            (watch_id, user_id, user_id),
        )
        await conn.commit()
        return cur.rowcount > 0


async def list_incoming_requests(owner_id: int):
    """Egasiga kelgan, hali hal qilinmagan so'rovlar."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """SELECT w.id, t.id, t.name, u.full_name, u.username, w.created_at
               FROM truck_watchers w
               JOIN trucks t ON t.id = w.truck_id
               JOIN users u ON u.id = w.user_id
               WHERE t.user_id=? AND w.status='pending'
               ORDER BY w.created_at DESC""",
            (owner_id,),
        )
        return await cur.fetchall()


async def list_truck_watchers(owner_id: int, truck_id: int):
    """Egasi uchun: shu mashinani kuzatayotganlar (rad etilganlardan tashqari)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """SELECT w.id, u.full_name, u.username, w.status, w.created_at
               FROM truck_watchers w
               JOIN trucks t ON t.id = w.truck_id
               JOIN users u ON u.id = w.user_id
               WHERE t.id=? AND t.user_id=? AND w.status IN ('pending', 'approved')
               ORDER BY w.created_at DESC""",
            (truck_id, owner_id),
        )
        return await cur.fetchall()


async def list_watching(user_id: int):
    """Foydalanuvchi kuzatayotgan (yoki so'rov yuborgan) mashinalar."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """SELECT w.id, w.status, t.id, t.name, t.brand, t.volume
               FROM truck_watchers w
               JOIN trucks t ON t.id = w.truck_id
               WHERE w.user_id=?
               ORDER BY w.created_at DESC""",
            (user_id,),
        )
        return await cur.fetchall()


async def get_watched_truck(user_id: int, truck_id: int):
    """Faqat ruxsat olgan kuzatuvchiga: (id, name, plate, brand, volume, created_at, watch_id)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """SELECT t.id, t.name, t.plate, t.brand, t.volume, t.created_at, w.id
               FROM trucks t
               JOIN truck_watchers w ON w.truck_id = t.id
               WHERE t.id=? AND w.user_id=? AND w.status='approved'""",
            (truck_id, user_id),
        )
        return await cur.fetchone()


async def list_truck_trips(truck_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """SELECT tr.id, tr.truck_id, t.name, tr.status, tr.created_at, tr.finished_at,
                      COALESCE((SELECT SUM(price) FROM trip_legs WHERE trip_id=tr.id), 0) as income,
                      COALESCE((SELECT SUM(amount) FROM trip_expenses WHERE trip_id=tr.id), 0) as expense
               FROM trips tr JOIN trucks t ON t.id = tr.truck_id
               WHERE tr.truck_id=?
               ORDER BY tr.created_at DESC""",
            (truck_id,),
        )
        return await cur.fetchall()


async def get_truck_trip(truck_id: int, trip_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """SELECT tr.id, tr.truck_id, t.name, tr.status, tr.created_at, tr.finished_at
               FROM trips tr JOIN trucks t ON t.id = tr.truck_id
               WHERE tr.id=? AND tr.truck_id=?""",
            (trip_id, truck_id),
        )
        return await cur.fetchone()


# ---------------- Qimmat yuklar (bot chatiga) ----------------
async def set_leg_alert(user_id: int, trip_id: int, leg_id: int, from_point: str, next_point: str) -> int:
    """Safar uchun yuborishni yoqadi. Shu reysdagi boshqa safarlarniki o'chadi —
    bir vaqtda faqat oxirgi manzildan qidiriladi."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE load_alerts SET active=0 WHERE trip_id=? AND user_id=? AND leg_id<>?",
            (trip_id, user_id, leg_id),
        )
        await conn.execute(
            """INSERT INTO load_alerts (user_id, trip_id, leg_id, from_point, next_point, active)
               VALUES (?, ?, ?, ?, ?, 1)
               ON CONFLICT(leg_id) DO UPDATE SET
                   from_point=excluded.from_point, next_point=excluded.next_point,
                   active=1, created_at=datetime('now')""",
            (user_id, trip_id, leg_id, from_point, next_point or None),
        )
        cur = await conn.execute("SELECT id FROM load_alerts WHERE leg_id=?", (leg_id,))
        alert_id = (await cur.fetchone())[0]
        await conn.commit()
        return alert_id


async def disable_alert(user_id: int, alert_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "UPDATE load_alerts SET active=0 WHERE id=? AND user_id=? AND active=1", (alert_id, user_id)
        )
        await conn.commit()
        return cur.rowcount > 0


async def disable_leg_alert(user_id: int, leg_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "UPDATE load_alerts SET active=0 WHERE leg_id=? AND user_id=? AND active=1", (leg_id, user_id)
        )
        await conn.commit()
        return cur.rowcount > 0


async def list_trip_alerts(trip_id: int):
    """Reysdagi faol yuborishlar: {leg_id: (id, from_point, next_point)}."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT leg_id, id, from_point, next_point FROM load_alerts WHERE trip_id=? AND active=1",
            (trip_id,),
        )
        return {r[0]: r[1:] for r in await cur.fetchall()}


async def list_active_alerts():
    """Faol reyslardagi yoqilgan yuborishlar:
    (id, user_id, telegram_id, from_point, next_point, mashina hajmi, mashina nomi)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """SELECT a.id, a.user_id, u.telegram_id, a.from_point, a.next_point, t.volume, t.name
               FROM load_alerts a
               JOIN trips tr ON tr.id = a.trip_id AND tr.status = 'active'
               JOIN trucks t ON t.id = tr.truck_id
               JOIN users u ON u.id = a.user_id
               WHERE a.active = 1"""
        )
        return await cur.fetchall()


async def get_active_alert(alert_id: int):
    """list_active_alerts() bilan bir xil ko'rinishda, bitta yuborish uchun."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """SELECT a.id, a.user_id, u.telegram_id, a.from_point, a.next_point, t.volume, t.name
               FROM load_alerts a
               JOIN trips tr ON tr.id = a.trip_id AND tr.status = 'active'
               JOIN trucks t ON t.id = tr.truck_id
               JOIN users u ON u.id = a.user_id
               WHERE a.id = ? AND a.active = 1""",
            (alert_id,),
        )
        return await cur.fetchone()


async def unsent_load_ids(user_id: int, load_ids: list[int]) -> set[int]:
    if not load_ids:
        return set()
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            f"SELECT load_id FROM load_alert_sent WHERE user_id=? AND load_id IN ({','.join('?' * len(load_ids))})",
            (user_id, *load_ids),
        )
        sent = {r[0] for r in await cur.fetchall()}
        return set(load_ids) - sent


async def mark_loads_sent(user_id: int, load_ids: list[int]):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executemany(
            "INSERT OR IGNORE INTO load_alert_sent (user_id, load_id) VALUES (?, ?)",
            [(user_id, i) for i in load_ids],
        )
        # Eski yozuvlar kerak emas (yuklar FRESH_HOURS dan keyin baribir yuborilmaydi)
        await conn.execute("DELETE FROM load_alert_sent WHERE sent_at < datetime('now', '-3 days')")
        await conn.commit()
