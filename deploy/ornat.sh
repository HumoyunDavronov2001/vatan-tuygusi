#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
#  VATAN TUYG'USI — serverga o'rnatish
#
#  BU SERVERDA BOSHQA DASTURLAR ISHLAYAPTI. Skript ularga TEGMAYDI:
#
#    * Hech qanday host portini band qilmaydi (80/443 mavjud Caddy'da qoladi)
#    * Mavjud konteynerlarni to'xtatmaydi, qayta ishga tushirmaydi
#    * Mavjud Caddyfile'ga faqat QO'SHADI, hech narsani o'chirmaydi
#    * Qo'shishdan oldin zaxira oladi, keyin TEKSHIRADI, xato bo'lsa QAYTARADI
#    * `caddy reload` ishlatadi (restart emas) — boshqa saytlar uzilmaydi
#    * O'rnatishdan oldin va keyin boshqa saytlarni tekshiradi
#
#  Ishlatish:   sudo bash ornat.sh
#  Qaytarish:   sudo bash olib-tashla.sh
# ──────────────────────────────────────────────────────────────────────────
set -euo pipefail

QIZIL=$'\e[31m'; YASHIL=$'\e[32m'; SARIQ=$'\e[33m'; KOK=$'\e[36m'; TOZA=$'\e[0m'
xabar() { printf '%s==>%s %s\n' "$KOK" "$TOZA" "$1"; }
ok()    { printf '%s  ok%s %s\n' "$YASHIL" "$TOZA" "$1"; }
ogoh()  { printf '%s  !!%s %s\n' "$SARIQ" "$TOZA" "$1"; }
xato()  { printf '%s XATO%s %s\n' "$QIZIL" "$TOZA" "$1" >&2; exit 1; }

BU="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BU"
VAQT="$(date +%Y-%m-%d-%H%M%S)"

[ "$(id -u)" -eq 0 ] || xato "root kerak:  sudo bash ornat.sh"
command -v docker >/dev/null 2>&1 || xato "Docker topilmadi"
docker compose version >/dev/null 2>&1 || xato "docker compose plugini yo'q"

# ═══════════════ 1. .env ═══════════════
xabar ".env tekshirilmoqda"
[ -f .env ] || xato ".env yo'q. Avval:  cp .env.example .env && nano .env"
set -a; . ./.env; set +a

FRONT="${VATAN_FRONT_DOMEN:-vatan.example.uz}"
API="${VATAN_API_DOMEN:-vatanapi.example.uz}"
ok "Domenlar: $FRONT · $API"

# ═══════════════ 2. Mavjud Caddy'ni topamiz ═══════════════
xabar "Serverdagi mavjud Caddy qidirilmoqda"

CADDY="${CADDY_KONTEYNER:-}"
# Bizning konteynerlar (vatan-front) ham Caddy image ishlatadi — ularni CHEKKA
# Caddy deb xato aniqlamaymiz. .env da xato saqlangan bo'lsa ham o'zini tuzatadi.
if [ -n "$CADDY" ] && printf '%s' "$CADDY" | grep -qE '^vatan-(front|api)$'; then
    xabar "Sozlangan CADDY_KONTEYNER ($CADDY) — bu bizning konteyner, e'tiborsiz qoldirildi"
    CADDY=""
fi
if [ -z "$CADDY" ]; then
    # Haqiqiy chekka proksi 80/443 portini e'lon qiladi — avval shuni izlaymiz
    CADDY="$(docker ps --format '{{.Names}}\t{{.Image}}\t{{.Ports}}' \
             | awk -F'\t' 'tolower($2) ~ /caddy/ && $1 !~ /^vatan-(front|api)$/ \
                           && ($3 ~ /:80->/ || $3 ~ /:443->/) {print $1; exit}')"
fi
if [ -z "$CADDY" ]; then
    # Port topilmasa — nomi/image'ida caddy bor, bizniki bo'lmagan birinchisi
    CADDY="$(docker ps --format '{{.Names}}\t{{.Image}}' \
             | awk -F'\t' 'tolower($2) ~ /caddy/ && $1 !~ /^vatan-(front|api)$/ {print $1; exit}')"
fi
[ -n "$CADDY" ] || xato "Ishlab turgan chekka Caddy konteyneri topilmadi.
   Agar u boshqa nom bilan bo'lsa, .env ga qo'shing:  CADDY_KONTEYNER=nomi"
ok "Caddy konteyneri: $CADDY"

# Uning tarmog'i — bizning konteynerlar ham shu tarmoqqa qo'shiladi
CADDY_TARMOQ="${CADDY_TARMOQ:-}"
if [ -z "$CADDY_TARMOQ" ]; then
    CADDY_TARMOQ="$(docker inspect -f \
        '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' "$CADDY" \
        | grep -v '^bridge$' | head -1)"
fi
[ -n "$CADDY_TARMOQ" ] || xato "Caddy tarmog'i aniqlanmadi. .env ga qo'shing: CADDY_TARMOQ=nomi"
export CADDY_TARMOQ
ok "Tarmoq: $CADDY_TARMOQ"

# Caddyfile host'da qayerda
CFILE="${CADDY_FAYL:-}"
if [ -z "$CFILE" ]; then
    CFILE="$(docker inspect -f \
        '{{range .Mounts}}{{if eq .Destination "/etc/caddy/Caddyfile"}}{{.Source}}{{end}}{{end}}' "$CADDY")"
fi
[ -n "$CFILE" ] || xato "Caddyfile mount'i topilmadi. .env ga qo'shing: CADDY_FAYL=/yo'l/Caddyfile"
[ -f "$CFILE" ] || xato "Caddyfile mavjud emas: $CFILE"
ok "Caddyfile: $CFILE"

# Aniqlangan qiymatlarni .env ga yozib qo'yamiz. Busiz `docker compose` ni
# qo'lda ishlatganda "CADDY_TARMOQ aniqlanmagan" xatosi chiqadi —
# compose .env ni o'zi o'qiydi, lekin ornat.sh ning shell o'zgaruvchisini emas.
envga_yoz() {
    local kalit="$1" qiymat="$2"
    if grep -qE "^${kalit}=" .env; then
        sed -i "s|^${kalit}=.*|${kalit}=${qiymat}|" .env
    else
        printf '\n# ornat.sh tomonidan avtomatik aniqlangan (%s)\n%s=%s\n' \
            "$VAQT" "$kalit" "$qiymat" >> .env
    fi
}
envga_yoz CADDY_KONTEYNER "$CADDY"
envga_yoz CADDY_TARMOQ    "$CADDY_TARMOQ"
envga_yoz CADDY_FAYL      "$CFILE"
ok ".env ga yozildi — endi docker compose buyruqlari to'g'ridan-to'g'ri ishlaydi"

if grep -q "VATAN TUYG'USI — boshlanish" "$CFILE"; then
    ogoh "Caddyfile'da VATAN bloki allaqachon bor — qayta qo'shilmaydi"
    BLOK_BOR=1
else
    BLOK_BOR=0
fi

# ═══════════════ 3. Portlarga TEGMASLIGIMIZNI tasdiqlaymiz ═══════════════
xabar "Port to'qnashuvi tekshirilmoqda"
if ! KONFIG="$(docker compose config 2>&1)"; then
    printf '%s\n' "$KONFIG" | head -5
    xato "docker compose config bajarilmadi — yuqoridagi xabarga qarang."
fi
if printf '%s\n' "$KONFIG" | grep -qE '^\s+-\s+(mode: ingress|"?[0-9]+:[0-9]+)'; then
    xato "docker-compose.yml host portini band qilmoqchi — bu boshqa dasturlarga
   xalaqit beradi. ports: bo'limlari olib tashlanishi kerak."
fi
ok "Bizning konteynerlar host portini band qilmaydi (tekshirildi)"

# ═══════════════ 4. HOZIRGI holatni yozib olamiz ═══════════════
xabar "Mavjud dasturlar holati yozib olinmoqda (keyin solishtiramiz)"
docker ps --format '{{.Names}}\t{{.Status}}' | sort > "/tmp/vatan-oldin-$VAQT.txt"
sed 's/^/     /' "/tmp/vatan-oldin-$VAQT.txt"

# Mavjud Caddyfile'dagi domenlarni topib, ular hozir ishlayotganini tekshiramiz
mapfile -t MAVJUD_DOMENLAR < <(
    grep -oE '^[a-z0-9.-]+\.[a-z]{2,}' "$CFILE" | sort -u | head -10
)
declare -A OLDIN_HOLAT
for d in "${MAVJUD_DOMENLAR[@]:-}"; do
    [ -z "$d" ] && continue
    kod="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "https://$d" 2>/dev/null || echo 000)"
    OLDIN_HOLAT["$d"]="$kod"
    printf '     %-34s %s\n' "$d" "$kod"
done

# ═══════════════ 5. Cookies ═══════════════
mkdir -p secrets && chmod 700 secrets
[ -f secrets/cookies.txt ] && chmod 600 secrets/cookies.txt || true

# ═══════════════ 6. Konteynerlarni qurish ═══════════════
xabar "Konteynerlar qurilmoqda (birinchi marta 10-15 daqiqa — Whisper modeli)"
docker compose build
ok "Qurildi"

xabar "Ishga tushirilmoqda (vatan-api, vatan-front)"
# --force-recreate SHART: front image o'zgarmaydi, shuning uchun oddiy `up -d`
# uni qayta yaratmaydi va ../static bind-mount'i eski papkaga bog'lanib qoladi
# (yangilash.sh papkani mv qilgach). Force-recreate yangi static'ga bog'laydi.
docker compose up -d --force-recreate
ok "Ko'tarildi"

xabar "API javob berishi kutilmoqda"
API_TIRIK=0
for i in $(seq 1 40); do
    if docker exec vatan-api python -c \
        "import urllib.request;urllib.request.urlopen('http://127.0.0.1:5057/sogliq',timeout=3)" \
        >/dev/null 2>&1; then
        API_TIRIK=1; ok "vatan-api tirik"; break
    fi
    sleep 2
done
[ "$API_TIRIK" -eq 1 ] || { docker compose logs --tail=40 api; xato "vatan-api ko'tarilmadi"; }

if docker exec "$CADDY" wget -q -O- --timeout=5 http://vatan-front/config.js >/dev/null 2>&1 \
   || docker exec "$CADDY" sh -c 'command -v curl >/dev/null && curl -fsS --max-time 5 http://vatan-front/config.js' >/dev/null 2>&1; then
    ok "Caddy vatan-front ni ko'ryapti"
else
    ogoh "Caddy vatan-front ga yeta olmadi — tarmoq nomini tekshiring"
fi

# ═══════════════ 7. Caddyfile — ZAXIRA, QO'SHISH, TEKSHIRISH ═══════════════
if [ "$BLOK_BOR" -eq 0 ]; then
    ZAXIRA="${CFILE}.vatan-zaxira-${VAQT}"
    cp -a "$CFILE" "$ZAXIRA"
    ok "Zaxira olindi: $ZAXIRA"

    xabar "Caddyfile'ga ikkita blok qo'shilmoqda"
    {
        printf '\n'
        sed -e "s|__FRONT_DOMEN__|$FRONT|" -e "s|__API_DOMEN__|$API|" "$BU/caddy-blok.txt"
    } >> "$CFILE"

    xabar "Yangi Caddyfile tekshirilmoqda (hali qo'llanmaydi)"
    if docker exec "$CADDY" caddy validate --config /etc/caddy/Caddyfile >/tmp/vatan-caddy-check.log 2>&1; then
        ok "Caddyfile to'g'ri"
    else
        cat /tmp/vatan-caddy-check.log
        cp -a "$ZAXIRA" "$CFILE"
        ogoh "Caddyfile QAYTARILDI — sizning sozlamangiz o'zgarmadi"
        docker compose down >/dev/null 2>&1 || true
        xato "Caddyfile tekshiruvdan o'tmadi (yuqoriga qarang). Hech narsa o'zgartirilmadi."
    fi

    xabar "Caddy qayta yuklanmoqda (reload — boshqa saytlar uzilmaydi)"
    if docker exec "$CADDY" caddy reload --config /etc/caddy/Caddyfile >/tmp/vatan-caddy-reload.log 2>&1; then
        ok "Reload muvaffaqiyatli"
    else
        cat /tmp/vatan-caddy-reload.log
        cp -a "$ZAXIRA" "$CFILE"
        docker exec "$CADDY" caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 || true
        ogoh "Caddyfile QAYTARILDI va eski holat tiklandi"
        xato "Reload bajarilmadi. Sizning saytlaringiz eski sozlama bilan ishlayapti."
    fi
else
    ok "Caddyfile'ga tegilmadi (blok allaqachon bor)"
fi

# ═══════════════ 8. BOSHQA DASTURLAR SHIKASTLANMAGANINI TEKSHIRAMIZ ═══════════════
echo
xabar "Boshqa dasturlar tekshirilmoqda"
sleep 3
BUZILGAN=0
for d in "${MAVJUD_DOMENLAR[@]:-}"; do
    [ -z "$d" ] && continue
    [ "$d" = "$FRONT" ] && continue
    [ "$d" = "$API" ] && continue
    yangi="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "https://$d" 2>/dev/null || echo 000)"
    eski="${OLDIN_HOLAT[$d]:-000}"
    if [ "$eski" = "$yangi" ]; then
        ok "$d — o'zgarmadi ($yangi)"
    else
        ogoh "$d — $eski dan $yangi ga o'zgardi"
        BUZILGAN=1
    fi
done

docker ps --format '{{.Names}}\t{{.Status}}' | sort > "/tmp/vatan-keyin-$VAQT.txt"
if diff <(cut -f1 "/tmp/vatan-oldin-$VAQT.txt") \
        <(cut -f1 "/tmp/vatan-keyin-$VAQT.txt" | grep -v '^vatan-') >/dev/null; then
    ok "Barcha eski konteynerlar joyida"
else
    ogoh "Konteynerlar ro'yxati o'zgardi:"
    diff "/tmp/vatan-oldin-$VAQT.txt" "/tmp/vatan-keyin-$VAQT.txt" | sed 's/^/     /' || true
    BUZILGAN=1
fi

[ "$BUZILGAN" -eq 0 ] || ogoh "Yuqoridagilarni tekshiring. Qaytarish:  sudo bash olib-tashla.sh"

# ═══════════════ 9. Yangi saytlarni tekshiramiz ═══════════════
echo
xabar "SSL sertifikati olinishi kutilmoqda (1-3 daqiqa)"
SSL_OK=0
for i in $(seq 1 45); do
    if curl -fsS --max-time 8 "https://$API/sogliq" >/dev/null 2>&1; then SSL_OK=1; break; fi
    sleep 4
done

echo
if [ "$SSL_OK" -eq 1 ]; then
    printf '%s──────────────────────────────────────────────%s\n' "$YASHIL" "$TOZA"
    ok "TAYYOR"
    echo
    echo "   Front:  https://$FRONT"
    echo "   API:    https://$API/tekshiruv"
    echo
    curl -fsS --max-time 8 "https://$API/tekshiruv" 2>/dev/null || true
    echo
else
    ogoh "SSL hali olinmadi:"
    echo "     docker logs --tail=40 $CADDY | grep -i vatan"
fi

echo
echo "Buyruqlar:"
echo "   docker compose logs -f api     # API jurnali"
echo "   docker compose restart api     # qayta ishga tushirish"
echo "   sudo bash olib-tashla.sh       # butunlay olib tashlash"
