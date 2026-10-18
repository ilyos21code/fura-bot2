#!/usr/bin/env bash
# Fura Mini App (fura-bot2) ni Linux serverga o'rnatadi. Qayta ishga tushirish
# xavfsiz (idempotent) — yangilashda ham ishlatiladi. Kod /opt/fura-bot2/app da
# bo'lishi kerak (git archive). Botni ISHGA TUSHIRMAYDI.
#
#   sudo /opt/fura-bot2/app/deploy/install.sh
#   sudo nano /etc/fura-bot2/env          # BOT_TOKEN, WEBAPP_URL
#   sudo systemctl start fura-bot2
set -euo pipefail

APP_USER=furabot2
BASE_DIR=/opt/fura-bot2
APP_DIR="$BASE_DIR/app"
VENV_DIR="$BASE_DIR/venv"
DATA_DIR=/var/lib/fura-bot2
LOG_DIR=/var/log/fura-bot2
ENV_DIR=/etc/fura-bot2
ENV_FILE="$ENV_DIR/env"
PYTHON="${PYTHON:-python3}"

[ "$(id -u)" -eq 0 ] || { echo "sudo bilan ishga tushiring: sudo $0" >&2; exit 1; }
[ "$(cd "$(dirname "$0")/.." && pwd)" = "$APP_DIR" ] || { echo "Kod $APP_DIR da bo'lishi kerak" >&2; exit 1; }

echo "==> Tizim foydalanuvchisi: $APP_USER"
id -u "$APP_USER" >/dev/null 2>&1 \
    || useradd --system --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin "$APP_USER"

echo "==> Papkalar"
install -d -o root -g root -m 755 "$BASE_DIR"
install -d -o "$APP_USER" -g "$APP_USER" -m 700 "$DATA_DIR"
install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$LOG_DIR"
install -d -o root -g "$APP_USER" -m 750 "$ENV_DIR"
[ -f "$LOG_DIR/bot.log" ] || install -o "$APP_USER" -g "$APP_USER" -m 640 /dev/null "$LOG_DIR/bot.log"
chown -R root:root "$APP_DIR"
chmod -R go-w "$APP_DIR"

echo "==> Virtual muhit va paketlar"
[ -x "$VENV_DIR/bin/python" ] || "$PYTHON" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --no-cache-dir --upgrade pip
"$VENV_DIR/bin/pip" install --quiet --no-cache-dir -r "$APP_DIR/requirements.txt"
"$VENV_DIR/bin/python" -m compileall -q "$APP_DIR"

echo "==> Maxfiy sozlamalar: $ENV_FILE"
if [ ! -f "$ENV_FILE" ]; then
    printf 'BOT_TOKEN=\nWEBAPP_URL=https://fura.158-180-43-26.sslip.io\nDEV_MODE=0\n' > "$ENV_FILE"
    echo "   YANGI yaratildi — BOT_TOKEN ni yozing: sudo nano $ENV_FILE"
fi
chown root:"$APP_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

echo "==> systemd unit va logrotate"
install -o root -g root -m 644 "$APP_DIR/deploy/fura-bot2.service" /etc/systemd/system/fura-bot2.service
install -o root -g root -m 644 "$APP_DIR/deploy/logrotate.conf" /etc/logrotate.d/fura-bot2
systemctl daemon-reload
systemctl enable fura-bot2.service >/dev/null

echo
echo "Tayyor. Ishga tushirish: sudo systemctl start fura-bot2"
echo "Bir vaqtda faqat BITTA nusxa ishlasin (aks holda Telegram 409 xato beradi)."
