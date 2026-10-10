#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
#  Serverga Cloudflare WARP ni PROKSI rejimida o'rnatadi.
#
#  NEGA KERAK:
#    Server data-markazda turadi. YouTube/Instagram data-markaz IP'laridan
#    kelgan so'rovni "bot" deb rad etadi ("tasdiqlang" xatosi) — shu sabab
#    videolar yuklanmaydi. WARP proksi orqali so'rov Cloudflare IP'sidan
#    ketadi va blok yechiladi.
#
#  MUHIM: "proxy" rejimi FAQAT 127.0.0.1:40000 da socks5 proksi ochadi.
#    Serverning asosiy marshrutiga TEGMAYDI — saytlaringiz, SSH, Caddy,
#    boshqa dasturlar avvalgidek ishlaydi.
#
#  Ishlatish:   sudo bash proksi-ornat.sh
#  O'chirish:   sudo warp-cli disconnect        (yoki: sudo apt remove cloudflare-warp)
# ──────────────────────────────────────────────────────────────────────────
set -euo pipefail

QIZIL=$'\e[31m'; YASHIL=$'\e[32m'; KOK=$'\e[36m'; TOZA=$'\e[0m'
xabar(){ printf '%s==>%s %s\n' "$KOK" "$TOZA" "$1"; }
ok(){ printf '%s  ok%s %s\n' "$YASHIL" "$TOZA" "$1"; }
xato(){ printf '%s XATO%s %s\n' "$QIZIL" "$TOZA" "$1" >&2; exit 1; }

PORT=40000
[ "$(id -u)" -eq 0 ] || xato "root kerak:  sudo bash proksi-ornat.sh"

# ── 1. O'rnatish ──
if ! command -v warp-cli >/dev/null 2>&1; then
    # Kali/Debian minimal o'rnatmalarida gpg va curl bo'lmasligi mumkin
    for vosita in gpg curl; do
        command -v "$vosita" >/dev/null 2>&1 && continue
        xabar "$vosita o'rnatilmoqda (kerakli vosita)"
        apt-get update -qq
        case "$vosita" in
            gpg)  apt-get install -y gnupg ;;
            curl) apt-get install -y curl ca-certificates ;;
        esac
    done
    command -v gpg  >/dev/null 2>&1 || xato "gpg o'rnatilmadi:  apt install gnupg"
    command -v curl >/dev/null 2>&1 || xato "curl o'rnatilmadi:  apt install curl"

    xabar "Cloudflare WARP o'rnatilmoqda"
    install -d -m 0755 /usr/share/keyrings
    curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
        | gpg --yes --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
    # Kali/Debian uchun ham Ubuntu "noble" repozitoriysi mos keladi
    echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ noble main" \
        > /etc/apt/sources.list.d/cloudflare-client.list
    apt-get update -qq
    apt-get install -y cloudflare-warp
    ok "O'rnatildi"
else
    ok "WARP allaqachon o'rnatilgan"
fi

# ── 2. Daemon ──
systemctl enable --now warp-svc >/dev/null 2>&1 || true
for i in $(seq 1 15); do
    warp-cli --accept-tos status >/dev/null 2>&1 && break
    sleep 1
done
ok "warp-svc ishlayapti"

# ── 3. Ro'yxatdan o'tish (hisob kerak emas, tekin) ──
if ! warp-cli --accept-tos registration show >/dev/null 2>&1; then
    xabar "Ro'yxatdan o'tilmoqda (tekin, hisob shart emas)"
    warp-cli --accept-tos registration new >/dev/null
fi
ok "Ro'yxat tayyor"

# ── 4. PROKSI rejimi — serverning marshrutiga tegmaydi ──
xabar "Proksi rejimi yoqilmoqda (socks5 127.0.0.1:$PORT)"
warp-cli --accept-tos mode proxy >/dev/null
warp-cli --accept-tos proxy port "$PORT" >/dev/null 2>&1 || true
warp-cli --accept-tos connect >/dev/null
sleep 4
ok "Ulandi"

# ── 5. Tekshirish ──
xabar "Tekshirilmoqda"
TRACE="$(curl -s --max-time 15 --socks5-hostname "127.0.0.1:$PORT" \
         https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null || true)"
if printf '%s' "$TRACE" | grep -q '^warp=on'; then
    ok "Proksi ISHLAYAPTI (warp=on)"
else
    printf '%s\n' "$TRACE" | head -5
    xato "Proksi javob bermadi. 'warp-cli status' bilan tekshiring."
fi

# Server o'z ishini davom ettiryaptimi (marshrut buzilmaganini tasdiqlash)
if curl -s --max-time 10 -o /dev/null https://www.google.com; then
    ok "Serverning oddiy internetiga tegilmadi"
fi

# ── 6. .env ga yozamiz ──
ENVF=/opt/vatan/deploy/.env
if [ -f "$ENVF" ]; then
    QIYMAT="socks5://host.docker.internal:$PORT"
    if grep -qE '^VATAN_PROXY=' "$ENVF"; then
        sed -i "s|^VATAN_PROXY=.*|VATAN_PROXY=$QIYMAT|" "$ENVF"
    else
        printf '\n# Video yuklash proksisi (proksi-ornat.sh)\nVATAN_PROXY=%s\n' \
            "$QIYMAT" >> "$ENVF"
    fi
    ok ".env ga yozildi: VATAN_PROXY=$QIYMAT"

    xabar "vatan-api qayta ishga tushirilmoqda"
    (cd /opt/vatan/deploy && docker compose up -d --force-recreate api)
    ok "Qayta ishga tushdi"
else
    printf '\n  DIQQAT: %s topilmadi. Qo'"'"'lda qo'"'"'shing:\n' "$ENVF"
    printf '    VATAN_PROXY=socks5://host.docker.internal:%s\n' "$PORT"
fi

# ── 7. Ko'prik: WARP faqat 127.0.0.1 da tinglaydi, konteyner esa tashqi
#      manzildan ulanadi. Ko'prik busiz "Connection refused" bo'ladi.
KORPA="$(dirname "$0")/proksi-korpa.sh"
if [ -f "$KORPA" ]; then
    echo
    bash "$KORPA"
else
    printf '\n  DIQQAT: proksi-korpa.sh topilmadi — konteyner proksiga ulana olmaydi.\n'
fi

echo
ok "TAYYOR — endi videolar proksi orqali yuklanadi"
echo "   O'chirish:  sudo warp-cli disconnect"
echo "   Holat:      warp-cli status"
