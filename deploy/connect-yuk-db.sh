#!/usr/bin/env bash
# Fura botni Qimmatyuklar yuklar bazasiga (yuk.db) ulaydi — FAQAT O'QISH uchun.
# Shundan keyin Safar qo'shishda bozordagi o'rtacha narx ko'rinadi va qimmat yuklar
# bot chatiga yuborila boshlaydi. Qayta ishga tushirish xavfsiz.
#
#   sudo /opt/fura-bot2/app/deploy/connect-yuk-db.sh                 # yuk.db ni o'zi qidiradi
#   sudo /opt/fura-bot2/app/deploy/connect-yuk-db.sh /yo'l/yuk.db    # yoki yo'lini bering
set -euo pipefail

APP_USER=furabot2
SERVICE=fura-bot2
DROPIN_DIR=/etc/systemd/system/$SERVICE.service.d
DROPIN=$DROPIN_DIR/yuk-db.conf
VENV=/opt/fura-bot2/venv

[ "$(id -u)" -eq 0 ] || { echo "sudo bilan ishga tushiring: sudo $0 [yuk.db]" >&2; exit 1; }

DB="${1:-}"
if [ -z "$DB" ]; then
    echo "==> yuk.db qidirilmoqda..."
    mapfile -t FOUND < <(find / -xdev \( -path /proc -o -path /sys -o -path /opt/fura-bot2 \) -prune \
        -o -type f -name yuk.db -size +0 -print 2>/dev/null)
    if [ "${#FOUND[@]}" -ne 1 ]; then
        echo "yuk.db ${#FOUND[@]} ta topildi:" >&2
        printf '   %s\n' "${FOUND[@]}" >&2
        echo "Kerakligini ko'rsating: sudo $0 /yo'l/yuk.db" >&2
        exit 1
    fi
    DB="${FOUND[0]}"
fi
[ -f "$DB" ] || { echo "Fayl topilmadi: $DB" >&2; exit 1; }
DB=$(readlink -f "$DB")
DIR=$(dirname "$DB")
echo "==> Baza: $DB"

echo "==> $APP_USER ga faqat shu faylni o'qish huquqi (boshqa fayllar ochilmaydi)"
command -v setfacl >/dev/null || apt-get install -y -qq acl >/dev/null
d="$DIR"
while [ "$d" != "/" ]; do
    setfacl -m "u:$APP_USER:x" "$d"   # papkadan o'tish (ichidagilarni ko'rish emas)
    d=$(dirname "$d")
done
setfacl -m "u:$APP_USER:r" "$DB"

runuser -u "$APP_USER" -- "$VENV/bin/python" - "$DB" <<'EOF'
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=15)
n = con.execute("SELECT COUNT(*) FROM loads").fetchone()[0]
print(f"   o'qildi: {n} ta yuk")
EOF

echo "==> systemd sozlamasi: $DROPIN"
mkdir -p "$DROPIN_DIR"
{
    echo "# deploy/connect-yuk-db.sh yaratgan — Qimmatyuklar bazasi (faqat o'qish)"
    echo "[Service]"
    echo "Environment=YUK_DB_PATH=$DB"
    case "$DIR" in
        /home/*|/root|/root/*)
            # ProtectHome=true /home va /root ni butunlay yopadi — faqat shu bitta
            # fayl (o'qish rejimida) ko'rinadigan qilamiz; yonidagi .env, session
            # fayllari bot uchun ko'rinmaydi
            echo "ProtectHome=tmpfs"
            echo "BindReadOnlyPaths=$DB"
            ;;
    esac
} > "$DROPIN"

systemctl daemon-reload
systemctl restart "$SERVICE"
sleep 5
systemctl is-active -q "$SERVICE" && echo "Tayyor: $SERVICE qayta ishga tushdi." \
    || { echo "❗ $SERVICE ishga tushmadi: journalctl -u $SERVICE -n 50" >&2; exit 1; }
