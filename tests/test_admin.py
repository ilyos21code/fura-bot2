"""/admin paneli: statistika, foydalanuvchilar ro'yxati, Excel eksporti va
handlerlar tartibi (admin buyrug'i umumiy "Ochish" javobidan oldin ushlanishi).

Ishga tushirish (repo ildizidan):  python -m unittest discover -s tests -v
"""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ADMIN_TELEGRAM_ID", "111, 222")
os.environ.pop("BOT_TOKEN", None)

import admin  # noqa: E402
import database as db  # noqa: E402


class ParseAdminIds(unittest.TestCase):
    def test_comma_list_with_spaces_and_garbage(self):
        self.assertEqual(admin.parse_admin_ids(" 111, 222,abc,,"), {111, 222})
        self.assertEqual(admin.parse_admin_ids(""), frozenset())


class AdminQueries(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = db.DB_PATH
        db.DB_PATH = str(Path(self._tmp.name) / "fura.db")
        await db.init_db()
        self.ali = await db.get_or_create_user(1001, "Ali Valiyev", "ali")
        self.vali = await db.get_or_create_user(1002, "Vali", "")
        import aiosqlite
        async with aiosqlite.connect(db.DB_PATH) as conn:
            # ustunlar qo'shilishidan oldingi foydalanuvchi: created_at/last_seen_at bo'sh,
            # faolligi faqat 10 kun oldin qo'shgan mashinasidan ma'lum
            cur = await conn.execute("INSERT INTO users (telegram_id, full_name) VALUES (1003, 'Eski')")
            old = cur.lastrowid
            await conn.execute("INSERT INTO trucks (user_id, name, plate, created_at) "
                               "VALUES (?, 'Volvo', '01A111AA', datetime('now', '-10 days'))", (old,))
            cur = await conn.execute("INSERT INTO trucks (user_id, name, plate, brand, volume) "
                                     "VALUES (?, 'MAN', '01A123BC', 'MAN', 92)", (self.ali,))
            truck = cur.lastrowid
            cur = await conn.execute("INSERT INTO trips (user_id, truck_id, status) VALUES (?, ?, 'active')",
                                     (self.ali, truck))
            trip = cur.lastrowid
            await conn.execute("INSERT INTO trip_legs (trip_id, from_point, to_point, price) "
                               "VALUES (?, 'Toshkent', 'Moskva', 30000000)", (trip,))
            await conn.execute("INSERT INTO trip_expenses (trip_id, category, amount) "
                               "VALUES (?, 'Yoqilg''i', 12000000)", (trip,))
            await conn.execute("INSERT INTO truck_expenses (truck_id, category, amount) "
                               "VALUES (?, 'Ta''mir', 1000000)", (truck,))
            await conn.commit()

    async def asyncTearDown(self):
        db.DB_PATH = self._orig_path
        self._tmp.cleanup()

    async def test_stats_counts(self):
        s = await db.admin_stats()
        self.assertEqual(s["users"], 3)
        self.assertEqual((s["new_1d"], s["new_7d"], s["new_30d"]), (2, 2, 3))
        self.assertEqual((s["active_1d"], s["active_30d"]), (2, 3))
        self.assertEqual((s["users_with_trucks"], s["users_with_trips"]), (2, 1))
        self.assertEqual((s["trucks"], s["trips"], s["active_trips"]), (2, 1, 1))
        self.assertIn("Foydalanuvchilar: <b>3</b>", admin.stats_text(s))

    async def test_users_rows_have_totals_and_old_user_dates(self):
        rows = {r[1]: r for r in await db.admin_users()}
        ali = rows[1001]
        self.assertEqual(ali[6:9], (1, 1, 1))                      # mashina, reys, faol reys
        self.assertEqual(ali[9:], (30000000, 12000000, 1000000))    # daromad, xarajat, ta'mir
        self.assertTrue(rows[1003][4])                              # birinchi faollik mashinadan
        text = admin.recent_users_text(await db.admin_users())
        self.assertIn("Ali Valiyev (@ali)", text)

    async def test_excel_has_three_sheets_with_profit(self):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(await admin.export_excel()))
        self.assertEqual(wb.sheetnames, ["Foydalanuvchilar", "Mashinalar", "Reyslar"])
        users = list(wb["Foydalanuvchilar"].iter_rows(values_only=True))
        ali = next(r for r in users if r[1] == 1001)
        self.assertEqual(ali[-1], 30000000 - 12000000 - 1000000)
        trip = list(wb["Reyslar"].iter_rows(values_only=True))[1]
        self.assertEqual((trip[4], trip[7], trip[-1]), ("faol", "Toshkent → Moskva", 18000000))

    async def test_html_in_names_is_escaped(self):
        await db.get_or_create_user(1004, "<b>x</b>", "")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", admin.recent_users_text(await db.admin_users()))


class HandlerOrder(unittest.TestCase):
    def test_admin_command_registered_before_catch_all(self):
        import main
        names = [h.callback.__name__ for h in main.dp.message.handlers]
        self.assertLess(names.index("bot_admin"), names.index("bot_any_message"))

    def test_admin_filter_only_matches_admin_ids(self):
        import main
        self.assertTrue(main._is_admin.resolve(SimpleNamespace(from_user=SimpleNamespace(id=222))))
        self.assertFalse(main._is_admin.resolve(SimpleNamespace(from_user=SimpleNamespace(id=333))))


if __name__ == "__main__":
    unittest.main()
