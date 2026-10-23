#!/usr/bin/env bash
# fura-bot2 avto-yangilash: GitHub `main` ga yangi commit tushsa serverga o'zi qo'yadi.
# fura-bot2-autodeploy.timer har 2 daqiqada ishga tushiradi.
#
#  1) git fetch (faqat o'qish huquqli deploy key bilan)
#  2) yangi commit bo'lsa: bazaning nusxasi → kod releases/<sha> ga → paketlar (kerak bo'lsa)
#     → sintaksis tekshiruvi → app symlink almashadi → restart
#  3) 25 soniyadan keyin tekshiruv: servis ishlayapti, sahifa 200, bot polling boshladi.
#     Bo'lmasa — oldingi versiyaga qaytariladi. Natija Telegram'da adminga yuboriladi.
set -Eeuo pipefail

BASE=/opt/fura-bot2
REPO="$BASE/repo.git"
RELEASES="$BASE/releases"
APP="$BASE/app"
VENV="$BASE/venv"
DATA=/var/lib/fura-bot2
SERVICE=fura-bot2
BRANCH=main
KEY=/etc/fura-bot2/deploy_key
KEEP_RELEASES=5
export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o UserKnownHostsFile=/etc/fura-bot2/known_hosts -o StrictHostKeyChecking=yes"

# shellcheck source=/dev/null
[ -f /etc/server-backup/config ] && . /etc/server-backup/config   # ALERT_BOT_TOKEN, ALERT_CHAT_ID
LOG() { echo "$(date '+%F %T') $*"; }
alert() {
    [ -n "${ALERT_BOT_TOKEN:-}" ] && [ -n "${ALERT_CHAT_ID:-}" ] || return 0
    curl -s -m 20 -o /dev/null "https://api.telegram.org/bot${ALERT_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${ALERT_CHAT_ID}" --data-urlencode "text=$1" || true
}

exec 9>/run/fura-bot2-autodeploy.lock
flock -n 9 || exit 0

[ -d "$REPO" ] || git clone -q --mirror git@github.com:ilyos21code/fura-bot2.git "$REPO"
git -C "$REPO" fetch -q --prune origin "+refs/heads/*:refs/heads/*"
NEW=$(git -C "$REPO" rev-parse "$BRANCH")
CUR=$(basename "$(readlink -f "$APP")")
[ "$NEW" = "$CUR" ] && exit 0
# avval ishga tushmagan commit — keyingi push'gacha qayta urinmaymiz
[ "$NEW" = "$(cat "$BASE/failed-commit" 2>/dev/null)" ] && exit 0

MSG=$(git -C "$REPO" log -1 --format=%s "$NEW")
SHORT=${NEW:0:7}
LOG "Yangi versiya: $SHORT — $MSG (hozirgi: ${CUR:0:7})"

# 1) bazaning nusxasi (migratsiyadan oldin)
if [ -f "$DATA/fura.db" ]; then
    sqlite3 "$DATA/fura.db" ".timeout 30000" ".backup '$DATA/pre-deploy-$SHORT.db'"
    chown furabot2:furabot2 "$DATA/pre-deploy-$SHORT.db"
    ls -1t "$DATA"/pre-deploy-*.db 2>/dev/null | tail -n +$((KEEP_RELEASES + 1)) | xargs -r rm -f || true
fi

# 2) kod
DIR="$RELEASES/$NEW"
rm -rf "$DIR"; mkdir -p "$DIR"
git -C "$REPO" archive "$NEW" | tar -x -C "$DIR"
chown -R root:root "$DIR"; chmod -R go-w "$DIR"
if [ ! -f "$RELEASES/$CUR/requirements.txt" ] || ! cmp -s "$DIR/requirements.txt" "$RELEASES/$CUR/requirements.txt"; then
    LOG "requirements.txt o'zgargan — paketlar o'rnatilmoqda"
    "$VENV/bin/pip" install --quiet --no-cache-dir -r "$DIR/requirements.txt"
fi
if ! "$VENV/bin/python" -m py_compile "$DIR"/*.py; then
    LOG "Sintaksis xatosi — qo'yilmadi"
    alert "❗ fura-bot2: $SHORT qo'yilmadi — Python sintaksis xatosi. Bot eski versiyada ishlayapti. Commit: $MSG"
    rm -rf "$DIR"; exit 1
fi

# 3) almashtirish va tekshiruv
PREV=$(readlink -f "$APP")
ln -sfn "$DIR" "$APP.tmp" && mv -T "$APP.tmp" "$APP"
BOTLOG=/var/log/fura-bot2/bot.log
LINES=$(wc -l < "$BOTLOG")
systemctl restart "$SERVICE"
sleep 3
PID1=$(systemctl show "$SERVICE" -p MainPID --value)
sleep 22

ok=1
systemctl is-active -q "$SERVICE" || ok=0
# shu orada yiqilib qayta ko'tarilmaganmi (PID o'zgarmagan bo'lishi kerak)
[ "$(systemctl show "$SERVICE" -p MainPID --value)" = "$PID1" ] || ok=0
[ "$(curl -s -o /dev/null -w '%{http_code}' -m 10 http://127.0.0.1:8400/)" = "200" ] || ok=0
# faqat restartdan keyingi log qatorlari
tail -n +"$((LINES + 1))" "$BOTLOG" | grep -q "Run polling for bot" || ok=0

if [ "$ok" = 1 ]; then
    LOG "Qo'yildi: $SHORT"
    alert "✅ fura-bot2 yangilandi: $MSG ($SHORT)"
    ls -1dt "$RELEASES"/*/ | tail -n +$((KEEP_RELEASES + 1)) | { grep -v "$(basename "$DIR")" || true; } | xargs -r rm -rf
else
    LOG "Tekshiruvdan o'tmadi — oldingi versiyaga qaytarilmoqda"
    ln -sfn "$PREV" "$APP.tmp" && mv -T "$APP.tmp" "$APP"
    systemctl restart "$SERVICE"
    alert "❗ fura-bot2: yangi versiya ($SHORT — $MSG) ishga tushmadi, oldingi versiyaga qaytarildi. Log: /var/log/fura-bot2/bot.log"
    # shu commitni qayta-qayta urinmaslik uchun belgilab qo'yamiz
    echo "$NEW" > "$BASE/failed-commit"
    exit 1
fi
