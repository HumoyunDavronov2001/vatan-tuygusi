#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
#  WARP proksisi bilan Docker konteyneri orasida KO'PRIK.
#
#  MUAMMO: `warp-cli mode proxy` socks5 ni FAQAT 127.0.0.1:40000 da ochadi.
#    Konteyner esa host'ga tashqi manzil (host.docker.internal) orqali
#    ulanadi -> "Connection refused".
#
#  YECHIM: socat host'ning docker interfeysida 40001 portini ochadi va
#    uni 127.0.0.1:40000 ga uzatadi. systemd xizmati sifatida — qayta
#    ishga tushgandan keyin ham ishlaydi.
#
#  XAVFSIZLIK: socat FAQAT docker ichki manziliga bog'lanadi (0.0.0.0 EMAS)
#    — proksi internetga ochilmaydi.
#
#  Ishlatish:  sudo bash proksi-korpa.sh
# ──────────────────────────────────────────────────────────────────────────
set -euo pipefail

QIZIL=$'\e[31m'; YASHIL=$'\e[32m'; KOK=$'\e[36m'; TOZA=$'\e[0m'
xabar(){ printf '%s==>%s %s\n' "$KOK" "$TOZA" "$1"; }
ok(){ printf '%s  ok%s %s\n' "$YASHIL" "$TOZA" "$1"; }
xato(){ printf '%s XATO%s %s\n' "$QIZIL" "$TOZA" "$1" >&2; exit 1; }

WARP_PORT=40000      # warp-cli proksi (faqat 127.0.0.1)
KORPA_PORT=40001     # konteyner ulanadigan port
[ "$(id -u)" -eq 0 ] || xato "root kerak:  sudo bash proksi-korpa.sh"

# ── 1. socat ──
command -v socat >/dev/null 2>&1 || { xabar "socat o'rnatilmoqda"; apt-get update -qq; apt-get install -y socat; }
ok "socat tayyor"

# ── 2. WARP proksisi tinglayaptimi ──
if ! timeout 12 curl -s --socks5-hostname "127.0.0.1:$WARP_PORT" \
        https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null | grep -q '^warp=on'; then
    xato "WARP proksisi 127.0.0.1:$WARP_PORT da javob bermayapti.
   Avval:  sudo bash /opt/vatan/deploy/proksi-ornat.sh"
fi
ok "WARP proksisi ishlayapti (127.0.0.1:$WARP_PORT)"

# ── 3. Konteyner host'ni qaysi manzilda ko'radi ──
HOSTGW="$(docker exec vatan-api getent hosts host.docker.internal 2>/dev/null | awk '{print $1; exit}')"
if [ -z "$HOSTGW" ]; then
    HOSTGW="$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)"
fi
[ -n "$HOSTGW" ] || xato "Host manzili aniqlanmadi (host.docker.internal).
   docker-compose.yml da extra_hosts bormi tekshiring."
ok "Konteyner host'ni ko'radi: $HOSTGW"

# ── 4. systemd xizmati (qayta yuklashdan keyin ham ishlaydi) ──
xabar "Ko'prik xizmati yaratilmoqda"
cat > /etc/systemd/system/vatan-proksi-korpa.service <<UNIT
[Unit]
Description=VATAN: WARP socks5 ko'prigi (docker -> 127.0.0.1:$WARP_PORT)
After=network-online.target warp-svc.service docker.service
Wants=warp-svc.service

[Service]
# FAQAT docker ichki manzilida tinglaydi — internetga ochilmaydi
ExecStart=/usr/bin/socat TCP-LISTEN:$KORPA_PORT,bind=$HOSTGW,fork,reuseaddr TCP:127.0.0.1:$WARP_PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now vatan-proksi-korpa >/dev/null 2>&1
sleep 2
systemctl is-active --quiet vatan-proksi-korpa || xato "Ko'prik ishga tushmadi:
   journalctl -u vatan-proksi-korpa -n 20"
ok "Ko'prik ishlayapti: $HOSTGW:$KORPA_PORT -> 127.0.0.1:$WARP_PORT"

# ── 5. .env ni yangilaymiz ──
ENVF=/opt/vatan/deploy/.env
QIYMAT="socks5://host.docker.internal:$KORPA_PORT"
if grep -qE '^VATAN_PROXY=' "$ENVF"; then
    sed -i "s|^VATAN_PROXY=.*|VATAN_PROXY=$QIYMAT|" "$ENVF"
else
    printf '\nVATAN_PROXY=%s\n' "$QIYMAT" >> "$ENVF"
fi
ok ".env: VATAN_PROXY=$QIYMAT"

xabar "vatan-api qayta ishga tushirilmoqda"
(cd /opt/vatan/deploy && docker compose up -d --force-recreate api >/dev/null)
sleep 6
ok "Qayta ishga tushdi"

# ── 6. KONTEYNER ICHIDAN tekshiramiz (haqiqiy sinov) ──
xabar "Konteyner ichidan tekshirilmoqda"
NATIJA="$(docker exec vatan-api python -c "
import socket
try:
    s=socket.create_connection(('host.docker.internal', $KORPA_PORT), timeout=8); s.close()
    print('ULANDI')
except Exception as e:
    print('XATO:', e)
" 2>&1)"
case "$NATIJA" in
    ULANDI*) ok "Konteyner proksiga ULANDI ✓" ;;
    *) xato "Konteyner ulana olmadi: $NATIJA" ;;
esac

# Haqiqiy yuklab ko'ramiz
xabar "YouTube sinovi (konteyner ichida, proksi orqali)"
SINOV="$(docker exec vatan-api python -c "
import os
os.environ['VATAN_PROXY']='socks5://host.docker.internal:$KORPA_PORT'
import yt_dlp
o={'quiet':True,'no_warnings':True,'skip_download':True,
   'proxy':'socks5://host.docker.internal:$KORPA_PORT'}
try:
    with yt_dlp.YoutubeDL(o) as y:
        i=y.extract_info('https://www.youtube.com/watch?v=V0K6rvSx9Ys', download=False)
    print('OK:', (i.get('title') or '')[:50])
except Exception as e:
    print('XATO:', str(e)[:160])
" 2>&1 | tail -1)"
echo "   $SINOV"
case "$SINOV" in
    OK:*) ok "YOUTUBE PROKSI ORQALI ISHLAYAPTI ✓" ;;
    *)    printf '%s  !!%s YouTube hali ochilmadi — yuqoridagi xabarni yuboring\n' "$QIZIL" "$TOZA" ;;
esac

echo
ok "TAYYOR"
echo "   Holat:    systemctl status vatan-proksi-korpa"
echo "   O'chirish: sudo systemctl disable --now vatan-proksi-korpa"
