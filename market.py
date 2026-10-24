"""Bozor narxlari — Qimmatyuklar (yuk_top.py) Telegram yuk kanallaridan yig'gan
yuklar bazasidan (yuk.db) FAQAT O'QIYDI, hech qachon yozmaydi.

Ikki vazifa:
  * route_stats(): yo'nalish bo'yicha o'rtacha yuk narxi (Safar qo'shishda ko'rinadi)
  * find_expensive_loads(): berilgan shahardan chiqadigan, narxi o'z yo'nalishi
    o'rtachasiga teng yoki undan yuqori, mashina hajmiga mos yangi yuklar

Bazaning joyi: YUK_DB_PATH. Berilmagan yoki o'qib bo'lmasa bu funksiyalar jim
o'chadi (available() False) — ilovaning qolgan qismi odatdagidek ishlaydi.

Shahar nomlari Qimmatyuklar bilan bir xil qoidada solishtiriladi: geo_data.json
o'sha loyihadan olingan nusxa (aliaslar: "Ташкент", "tashkent" -> "Toshkent").
"""
import difflib
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

YUK_DB_PATH = os.getenv("YUK_DB_PATH", "")

STATS_DAYS = 7           # o'rtacha narx shu necha kunlik yuklardan hisoblanadi
MIN_SAMPLES = 3          # bundan kam yuk bo'lsa o'rtacha ko'rsatilmaydi
FRESH_HOURS = 3          # botga faqat shu necha soat ichida e'lon qilingan yuklar yuboriladi
OUTLIER_STD_DEVS = 2     # o'rtachadan oldin shundan uzoq narxlar chetlashtiriladi (yuk_top.py bilan bir xil)
VOLUME_TOLERANCE = 1.03  # e'londagi hajm mashina hajmidan shuncha baravargacha oshsa ham mos deb olinadi
FUZZY_THRESHOLD = 0.82   # bazadagi notanish imlo variantlari uchun (yuk_top.py bilan bir xil)
UZS_FALLBACK_RATE = 12600.0
NAMES_CACHE_SEC = 300

_GEO_FILE = Path(__file__).parent / "geo_data.json"


# ---------------- Shahar va davlat nomlari ----------------
def _load_geo():
    try:
        with open(_GEO_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        logging.warning("geo_data.json o'qilmadi — shahar aliaslarisiz ishlaydi")
        return {}, {}
    cities = data.get("cities", {})
    aliases = dict(data.get("aliases", {}))
    for name, info in cities.items():
        aliases.setdefault(re.sub(r"[^\w']", "", name.lower()), name)
        if info.get("cyrillic"):
            aliases.setdefault(re.sub(r"[^\w']", "", info["cyrillic"].lower()), name)
    cyrillic = {name: info["cyrillic"] for name, info in cities.items() if info.get("cyrillic")}
    return aliases, cyrillic


CITY_ALIASES, CITY_CYRILLIC = _load_geo()
_ALIAS_KEYS = sorted(CITY_ALIASES, key=len, reverse=True)

# yuk_top.py dagi COUNTRY_ALIASES nusxasi + o'zbekcha kirill shakllari
COUNTRY_ALIASES = {
    "узбекистан": "O'zbekiston", "uzbekistan": "O'zbekiston", "ozbekiston": "O'zbekiston",
    "ўзбекистон": "O'zbekiston",
    "россия": "Rossiya", "russia": "Rossiya", "rossiya": "Rossiya", "рф": "Rossiya",
    "rf": "Rossiya", "росия": "Rossiya", "rosiya": "Rossiya",
    "казахстан": "Qozog'iston", "kazakhstan": "Qozog'iston", "qozogiston": "Qozog'iston",
    "қозоғистон": "Qozog'iston",
    "кыргызстан": "Qirg'iziston", "киргизия": "Qirg'iziston", "kyrgyzstan": "Qirg'iziston",
    "qirgiziston": "Qirg'iziston", "қирғизистон": "Qirg'iziston",
    "таджикистан": "Tojikiston", "tajikistan": "Tojikiston", "tojikiston": "Tojikiston",
    "тожикистон": "Tojikiston",
    "туркменистан": "Turkmaniston", "turkmenistan": "Turkmaniston",
    "turkmaniston": "Turkmaniston",
    "беларусь": "Belarus", "belarus": "Belarus", "белоруссия": "Belarus",
    "украина": "Ukraina", "ukraine": "Ukraina", "ukraina": "Ukraina",
    "китай": "Xitoy", "china": "Xitoy", "xitoy": "Xitoy", "хитой": "Xitoy",
    "турция": "Turkiya", "turkey": "Turkiya", "turkiya": "Turkiya", "туркия": "Turkiya",
    "польша": "Polsha", "poland": "Polsha", "polsha": "Polsha",
    "германия": "Germaniya", "germany": "Germaniya", "germaniya": "Germaniya",
    "литва": "Litva", "lithuania": "Litva", "латвия": "Latviya", "latvia": "Latviya",
    "эстония": "Estoniya", "estonia": "Estoniya",
    "грузия": "Gruziya", "georgia": "Gruziya",
    "азербайджан": "Ozarbayjon", "azerbaijan": "Ozarbayjon",
    "молдова": "Moldova", "чехия": "Chexiya", "венгрия": "Vengriya",
    "румыния": "Ruminiya", "болгария": "Bolgariya",
}
_COUNTRY_KEYS = sorted(COUNTRY_ALIASES, key=len, reverse=True)

# "Toshkent shahri", "г. Москва", "Samarqand viloyati" kabi qo'shimchalar
_ADMIN_WORDS_RX = re.compile(
    r"(?i)\b(sh\.?|shahri(?:dan)?|shahar(?:i(?:dan)?)?|viloyat(?:i(?:dan)?)?|"
    r"tuman(?:i(?:dan)?)?|г\.?|обл\.?|область|р-н\.?|район)\b"
)


def _key(text) -> str:
    cleaned = _ADMIN_WORDS_RX.sub(" ", str(text or ""))
    return re.sub(r"[^\w'ʻ]", "", cleaned.strip().lower()).replace("ʻ", "'")


def _title(text) -> str:
    text = str(text).strip().lower()
    return re.sub(r"(?:^|\s)\S", lambda m: m.group().upper(), text)


def resolve_place(text):
    """Erkin yozilgan joy nomi -> ("city", "Toshkent") yoki ("country", "Rossiya").
    Bo'sh bo'lsa None."""
    key = _key(text)
    if not key:
        return None
    if key in CITY_ALIASES:
        return ("city", CITY_ALIASES[key])
    country_key = key.replace("'", "")
    if country_key in COUNTRY_ALIASES:
        return ("country", COUNTRY_ALIASES[country_key])
    # "Toshkentdan", "Moskvaga", "г.Москва" — ichidan qidiramiz
    for alias in _ALIAS_KEYS:
        if len(alias) >= 4 and alias in key:
            return ("city", CITY_ALIASES[alias])
    for alias in _COUNTRY_KEYS:
        if len(alias) >= 5 and alias in country_key:
            return ("country", COUNTRY_ALIASES[alias])
    return ("city", _title(text))


def place_label(place) -> str:
    return place[1] if place else ""


def _city_matches(stored, canonical) -> bool:
    """Bazadagi nom (`stored`) shu shaharmi — aniq yoki yaqin imlo (lotin/kirill)."""
    if not stored or not canonical:
        return False
    s, t = _key(stored), _key(canonical)
    if s == t or difflib.SequenceMatcher(None, s, t).ratio() >= FUZZY_THRESHOLD:
        return True
    cyr = CITY_CYRILLIC.get(canonical)
    if cyr:
        c = _key(cyr)
        if s == c or difflib.SequenceMatcher(None, s, c).ratio() >= FUZZY_THRESHOLD:
            return True
    return False


# ---------------- Baza ----------------
_warned = False
_columns_cache: dict = {}
_names_cache: dict = {}


def available() -> bool:
    return bool(YUK_DB_PATH) and os.access(YUK_DB_PATH, os.R_OK)


def _connect():
    global _warned
    if not YUK_DB_PATH:
        return None
    try:
        # mode=ro — SQLite darajasida yozish imkonsiz (Qimmatyuklar bazasiga tegmaymiz)
        con = sqlite3.connect(f"file:{YUK_DB_PATH}?mode=ro", uri=True, timeout=15)
        con.execute("SELECT 1 FROM loads LIMIT 1")
        _warned = False
        return con
    except sqlite3.Error:
        if not _warned:
            logging.warning("Yuklar bazasi (YUK_DB_PATH=%s) o'qilmadi", YUK_DB_PATH, exc_info=True)
            _warned = True
        return None


def _has_column(con, column) -> bool:
    if column not in _columns_cache:
        cols = [r[1] for r in con.execute("PRAGMA table_info(loads)")]
        _columns_cache[column] = column in cols
    return _columns_cache[column]


def _canonical_clause(con) -> str:
    # Qayta joylangan (repost) e'lonlar o'rtachani buzmasligi uchun faqat asl nusxalar
    return " AND COALESCE(is_canonical, 1) = 1" if _has_column(con, "is_canonical") else ""


def _matching_names(con, column, place, since):
    """Joyga mos keladigan bazadagi shahar nomlari ro'yxati (`from_city` yoki `to_city`)."""
    cache_key = (column, place, int(since // NAMES_CACHE_SEC))
    if cache_key in _names_cache:
        return _names_cache[cache_key]
    names = [r[0] for r in con.execute(
        f"SELECT DISTINCT {column} FROM loads WHERE ts >= ? AND {column} IS NOT NULL", (since,))]
    matched = [n for n in names if _city_matches(n, place[1])]
    if len(_names_cache) > 500:
        _names_cache.clear()
    _names_cache[cache_key] = matched
    return matched


def _place_filter(con, side, place, since):
    """(SQL sharti, parametrlar). side: "from" yoki "to"."""
    if place[0] == "country":
        if not _has_column(con, f"{side}_country"):
            return None, []
        return f"{side}_country = ?", [place[1]]
    names = _matching_names(con, f"{side}_city", place, since)
    if not names:
        return None, []
    return f"{side}_city IN ({','.join('?' * len(names))})", names


def _exclude_outliers(values):
    if len(values) < 2:
        return values
    mean = sum(values) / len(values)
    std = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5
    if std == 0:
        return values
    return [v for v in values if abs(v - mean) <= OUTLIER_STD_DEVS * std] or values


def _mean(values):
    filtered = _exclude_outliers(values)
    return sum(filtered) / len(filtered)


def _uzs_rate(con) -> float:
    """Qimmatyuklar ishlatgan so'm kursi (oxirgi so'mdagi yukdan), bo'lmasa taxminiy."""
    row = con.execute(
        "SELECT price / price_usd FROM loads WHERE currency='UZS' AND price_usd > 0 "
        "ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    return row[0] if row and row[0] else UZS_FALLBACK_RATE


# ---------------- O'rtacha narx ----------------
def route_stats(from_text, to_text, now=None):
    """Yo'nalish bo'yicha oxirgi STATS_DAYS kundagi o'rtacha narx.

    Qaytaradi: {"available": False} — baza ulanmagan;
    {"available": True, "count": n, ...} — n < MIN_SAMPLES bo'lsa o'rtacha yo'q."""
    con = _connect()
    if not con:
        return {"available": False}
    try:
        now = now or time.time()
        since = now - STATS_DAYS * 86400
        frm, to = resolve_place(from_text), resolve_place(to_text)
        result = {
            "available": True, "from": place_label(frm), "to": place_label(to),
            "days": STATS_DAYS, "count": 0,
        }
        if not frm or not to:
            return result
        f_sql, f_args = _place_filter(con, "from", frm, since)
        t_sql, t_args = _place_filter(con, "to", to, since)
        if not f_sql or not t_sql:
            return result
        rows = con.execute(
            f"SELECT price, currency, price_usd FROM loads WHERE ts >= ? AND price_usd > 0 "
            f"AND {f_sql} AND {t_sql}{_canonical_clause(con)}",
            [since, *f_args, *t_args],
        ).fetchall()
        result["count"] = len(rows)
        if len(rows) < MIN_SAMPLES:
            return result
        avg_usd = _mean([r[2] for r in rows])
        currencies = [r[1] or "USD" for r in rows]
        main_cur = max(set(currencies), key=currencies.count)
        if main_cur == "UZS":
            uzs_prices = [r[0] for r in rows if r[1] == "UZS" and r[0]]
            avg_uzs = _mean(uzs_prices) if len(uzs_prices) >= MIN_SAMPLES else avg_usd * _uzs_rate(con)
        else:
            avg_uzs = avg_usd * _uzs_rate(con)
        result.update({
            "avg_usd": round(avg_usd, 2),
            "avg_uzs": round(avg_uzs),
            "currency": main_cur,
        })
        return result
    finally:
        con.close()


# ---------------- Qimmat yuklar ----------------
# "86 куб", "120м3", "125-130 m3", "90 kub"
_VOLUME_RX = re.compile(
    r"(?<!\d)(\d{2,3})(?:\s*[-–]\s*\d{2,3})?\s*(?:м3|m3|м³|m³|куб|kub|cbm)",
    re.IGNORECASE,
)


def load_volume(text):
    """E'lon matnidan kerakli hajm (kub). Matnda bitta aniq hajm bo'lmasa None —
    bir xabarda bir nechta yuk bo'lishi mumkin, noto'g'risini olib qo'ymaslik uchun."""
    if not text:
        return None
    values = {int(m.group(1)) for m in _VOLUME_RX.finditer(text)}
    values = {v for v in values if 10 <= v <= 200}
    return values.pop() if len(values) == 1 else None


def _baselines(con, f_sql, f_args, since):
    """{("city", to_city): avg_usd, ("country", to_country): avg_usd} — o'z yo'nalishi o'rtachasi."""
    to_country = "to_country" if _has_column(con, "to_country") else "NULL"
    rows = con.execute(
        f"SELECT to_city, {to_country}, price_usd FROM loads WHERE ts >= ? AND price_usd > 0 "
        f"AND {f_sql}{_canonical_clause(con)}",
        [since, *f_args],
    ).fetchall()
    groups: dict = {}
    for city, country, usd in rows:
        groups.setdefault(("city", city), []).append(usd)
        if country:
            groups.setdefault(("country", country), []).append(usd)
    return {k: (_mean(v), len(v)) for k, v in groups.items() if len(v) >= MIN_SAMPLES}


def find_expensive_loads(from_text, to_text, truck_volume=None, now=None):
    """`from_text` dan chiqadigan (to_text berilsa — o'sha shahar/davlatga),
    oxirgi FRESH_HOURS soatdagi, narxi o'z yo'nalishi o'rtachasiga teng yoki
    undan yuqori, `truck_volume` kubli mashinaga sig'adigan yuklar.
    Eng qimmati (o'rtachadan foizda) birinchi."""
    con = _connect()
    if not con:
        return []
    try:
        now = now or time.time()
        stats_since = now - STATS_DAYS * 86400
        fresh_since = now - FRESH_HOURS * 3600
        frm = resolve_place(from_text)
        if not frm:
            return []
        f_sql, f_args = _place_filter(con, "from", frm, stats_since)
        if not f_sql:
            return []
        conditions, args = [f_sql], list(f_args)
        to = resolve_place(to_text) if to_text else None
        if to:
            t_sql, t_args = _place_filter(con, "to", to, stats_since)
            if not t_sql:
                return []
            conditions.append(t_sql)
            args += t_args

        to_country = "l.to_country" if _has_column(con, "to_country") else "NULL"
        rows = con.execute(
            f"""SELECT l.id, l.ts, l.from_city, l.to_city, {to_country}, l.cargo, l.tons,
                       l.price, l.currency, l.price_usd, l.km, l.phone, l.channel, l.msg_id, r.text
                FROM loads l LEFT JOIN raw r ON r.id = l.raw_id
                WHERE l.ts >= ? AND l.price_usd > 0
                  AND {' AND '.join('l.' + c for c in conditions)}
                  {_canonical_clause(con).replace('is_canonical', 'l.is_canonical')}
                ORDER BY l.ts DESC""",
            [fresh_since, *args],
        ).fetchall()
        if not rows:
            return []

        baselines = _baselines(con, f_sql, f_args, stats_since)
        result = []
        for (load_id, ts, from_city, to_city, country, cargo, tons, price, currency,
             price_usd, km, phone, channel, msg_id, raw_text) in rows:
            base = baselines.get(("city", to_city)) or baselines.get(("country", country))
            if not base:
                continue
            avg_usd, count = base
            if price_usd < avg_usd:
                continue
            volume = load_volume(cargo) or load_volume(raw_text)
            if truck_volume and volume and volume > truck_volume * VOLUME_TOLERANCE:
                continue
            link = None
            if channel and str(channel).startswith("@") and msg_id:
                link = f"https://t.me/{str(channel)[1:]}/{msg_id}"
            result.append({
                "id": load_id, "ts": ts, "from": from_city, "to": to_city, "to_country": country,
                "cargo": cargo, "tons": tons, "volume": volume,
                "price": price, "currency": currency or "USD", "price_usd": price_usd,
                "avg_usd": avg_usd, "avg_count": count,
                "avg_price": avg_usd * (price / price_usd) if price else avg_usd,
                "pct": (price_usd / avg_usd - 1) * 100,
                "km": km, "phone": phone, "link": link,
            })
        result.sort(key=lambda x: x["pct"], reverse=True)
        return result
    finally:
        con.close()
