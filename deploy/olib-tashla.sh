#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
#  VATAN TUYG'USI — serverdan butunlay olib tashlash
#
#  Faqat O'ZIMIZNIKINI o'chiradi:
#    * vatan-api va vatan-front konteynerlari
#    * Caddyfile'dagi VATAN bloki (marker qatorlar orasi)
#  Boshqa dasturlarga, tarmoqqa, mavjud Caddy'ga TEGMAYDI.
#
#  Ishlatish:  sudo bash olib-tashla.sh
#              sudo bash olib-tashla.sh --bazani-ham   (kesh/tarixni ham o'chiradi)
# ──────────────────────────────────────────────────────────────────────────
set -euo pipefail

QIZIL=$'\e[31m'; YASHIL=$'\e[32m'; SARIQ=$'\e[33m'; KOK=$'\e[36m'; TOZA=$'\e[0m'
xabar() { printf '%s==>%s %s\n' "$KOK" "$TOZA" "$1"; }
ok()    { printf '%s  ok%s %s\n' "$YASHIL" "$TOZA" "$1"; }
ogoh()  { printf '%s  !!%s %s\n' "$SARIQ" "$TOZA" "$1"; }
xato()  { printf '%s XATO%s %s\n' "$QIZIL" "$TOZA" "$1" >&2; exit 1; }

BU="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; cd "$BU"
VAQT="$(date +%Y-%m-%d-%H%M%S)"
[ "$(id -u)" -eq 0 ] || xato "root kerak:  sudo bash olib-tashla.sh"

BAZANI_HAM=0
[ "${1:-}" = "--bazani-ham" ] && BAZANI_HAM=1

[ -f .env ] && { set -a; . ./.env; set +a; }

# ─── Caddy'ni topamiz ───
CADDY="${CADDY_KONTEYNER:-$(docker ps --format '{{.Names}}\t{{.Image}}' \
        | awk -F'\t' 'tolower($2) ~ /caddy/ && $1 !~ /^vatan-/ {print $1; exit}')}"
CFILE="${CADDY_FAYL:-}"
if [ -n "$CADDY" ] && [ -z "$CFILE" ]; then
    CFILE="$(docker inspect -f \
        '{{range .Mounts}}{{if eq .Destination "/etc/caddy/Caddyfile"}}{{.Source}}{{end}}{{end}}' \
        "$CADDY" 2>/dev/null || true)"
fi

# ─── Caddyfile'dan blokni olib tashlaymiz ───
if [ -n "$CFILE" ] && [ -f "$CFILE" ] && grep -q "VATAN TUYG'USI — boshlanish" "$CFILE"; then
    ZAXIRA="${CFILE}.vatan-olib-tashlashdan-oldin-${VAQT}"
    cp -a "$CFILE" "$ZAXIRA"
    ok "Zaxira: $ZAXIRA"

    # Faqat marker qatorlar orasidagini o'chiramiz
    awk '
        /VATAN TUYG.USI — boshlanish/ { ichida=1 }
        !ichida { print }
        /VATAN TUYG.USI — tugadi/     { ichida=0 }
    ' "$ZAXIRA" > "$CFILE"

    xabar "Yangi Caddyfile tekshirilmoqda"
    if docker exec "$CADDY" caddy validate --config /etc/caddy/Caddyfile >/tmp/vatan-och.log 2>&1; then
        docker exec "$CADDY" caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 \
            && ok "Caddy yangilandi — VATAN domenlari olib tashlandi" \
            || ogoh "Reload bajarilmadi, lekin fayl to'g'ri. Qo'lda: docker exec $CADDY caddy reload --config /etc/caddy/Caddyfile"
    else
        cat /tmp/vatan-och.log
        cp -a "$ZAXIRA" "$CFILE"
        xato "Tekshiruvdan o'tmadi — Caddyfile QAYTARILDI, hech narsa o'zgarmadi"
    fi
else
    ogoh "Caddyfile'da VATAN bloki topilmadi — o'tkazib yuborildi"
fi

# ─── Konteynerlarni o'chiramiz ───
xabar "vatan-api va vatan-front o'chirilmoqda"
if [ "$BAZANI_HAM" -eq 1 ]; then
    CADDY_TARMOQ="${CADDY_TARMOQ:-bridge}" docker compose down -v 2>/dev/null || true
    ogoh "Baza (kesh va tarix) ham o'chirildi"
else
    CADDY_TARMOQ="${CADDY_TARMOQ:-bridge}" docker compose down 2>/dev/null || true
    ok "Baza saqlab qolindi (vatan-data volume)"
fi
docker rm -f vatan-api vatan-front >/dev/null 2>&1 || true
ok "Konteynerlar o'chirildi"

# ─── Boshqa dasturlar joyidami ───
echo
xabar "Boshqa dasturlar holati"
docker ps --format '   {{.Names}}\t{{.Status}}' | grep -v '^\s*vatan-' || true

echo
ok "Olib tashlandi. Image'ni ham o'chirish uchun:  docker rmi vatan-api:1.0"
