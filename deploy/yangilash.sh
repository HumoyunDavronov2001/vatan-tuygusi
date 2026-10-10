#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
#  VATAN TUYG'USI — serverdagi eski versiyani yangisiga almashtirish
#
#  Bajaradi:  /tmp/vatan-tuygusi.zip dan yangi kodni oladi -> eski .env va
#             secrets ni SAQLAYDI -> eski nusxani /opt/vatan-eski ga zaxira
#             qiladi -> yangi kodni qo'yadi -> ornat.sh bilan qayta quradi.
#
#  Ishlatish:   sudo bash yangilash.sh
#               sudo bash yangilash.sh /yo'l/boshqa.zip     (boshqa zip bilan)
#
#  Boshqa dasturlarga (boshqa loyihalar) TEGMAYDI.
# ──────────────────────────────────────────────────────────────────────────
set -euo pipefail

QIZIL=$'\e[31m'; YASHIL=$'\e[32m'; KOK=$'\e[36m'; TOZA=$'\e[0m'
xabar(){ printf '%s==>%s %s\n' "$KOK" "$TOZA" "$1"; }
ok(){ printf '%s  ok%s %s\n' "$YASHIL" "$TOZA" "$1"; }
xato(){ printf '%s XATO%s %s\n' "$QIZIL" "$TOZA" "$1" >&2; exit 1; }

ZIP="${1:-/tmp/vatan-tuygusi.zip}"
[ "$(id -u)" -eq 0 ] || xato "root kerak:  sudo bash yangilash.sh"
[ -f "$ZIP" ] || xato "Zip topilmadi: $ZIP
   Avval laptopdan:  scp vatan-tuygusi.zip root@SERVER_IP:/tmp/"
command -v unzip >/dev/null 2>&1 || { apt-get install -y unzip >/dev/null 2>&1 || xato "unzip yo'q"; }

xabar "Yangi kod ochilmoqda: $ZIP"
rm -rf /tmp/vatan-yangi && mkdir -p /tmp/vatan-yangi
unzip -oq "$ZIP" -d /tmp/vatan-yangi
YANGI=/tmp/vatan-yangi/vatan-analiz
[ -d "$YANGI" ] || xato "Zip ichida vatan-analiz papkasi yo'q"
ok "Ochildi"

# ── Eski sozlamalarni saqlaymiz ──
if [ -f /opt/vatan/deploy/.env ]; then
    cp /opt/vatan/deploy/.env "$YANGI/deploy/.env"
    ok "Eski .env saqlandi (API kaliti, sozlamalar)"
    ESKI_ENV=1
else
    ESKI_ENV=0
fi
if [ -d /opt/vatan/deploy/secrets ]; then
    mkdir -p "$YANGI/deploy/secrets"
    cp -a /opt/vatan/deploy/secrets/. "$YANGI/deploy/secrets/" 2>/dev/null || true
    ok "Eski secrets saqlandi (cookies)"
fi
# Logo faylini ham saqlaymiz (agar qo'yilgan bo'lsa)
if [ -f /opt/vatan/static/logo.png ]; then
    cp /opt/vatan/static/logo.png "$YANGI/static/logo.png"
    ok "Logo saqlandi"
fi

# ── Kodni almashtiramiz (eski nusxa zaxira) ──
xabar "Kod almashtirilmoqda"
rm -rf /opt/vatan-eski 2>/dev/null || true
[ -d /opt/vatan ] && mv /opt/vatan /opt/vatan-eski && ok "Eski nusxa: /opt/vatan-eski (rollback uchun)"
mv "$YANGI" /opt/vatan
ok "Yangi kod: /opt/vatan"

# ── .env bo'lmasa — namunadan yaratamiz va DAVOM etamiz ──
# Standart backend "qoida" (APIsiz) — hech narsa to'ldirish shart emas.
cd /opt/vatan/deploy
if [ ! -f .env ]; then
    cp .env.example .env
    ok ".env namunadan yaratildi — VATAN_BACKEND=qoida (APIsiz, kalit shart emas)"
    echo "   (Kuchliroq Claude tahlili kerak bo'lsa keyin:"
    echo "    nano /opt/vatan/deploy/.env  ->  VATAN_BACKEND=claude va ANTHROPIC_API_KEY=...)"
fi

# ── Qayta qurish (ornat.sh: Caddy'ga tegib, konteynerni yangilaydi) ──
xabar "Qayta qurilmoqda (ornat.sh)"
bash ornat.sh

echo
ok "YANGILANDI"
echo "   Eski nusxa saqlanib turibdi: /opt/vatan-eski"
echo "   Ishonch hosil qilgach o'chiring:  rm -rf /opt/vatan-eski"
echo "   Muammo bo'lsa qaytarish:  rm -rf /opt/vatan && mv /opt/vatan-eski /opt/vatan && cd /opt/vatan/deploy && bash ornat.sh"
