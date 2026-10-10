# Serverga o'rnatish

## ⚠️ Server ko'p ijarali bo'lishi mumkin

Serverda 80/443 portlarni umumiy Caddy konteyneri (`caddy`) egallagan va uning
ortida boshqa loyihalar ishlayotgan bo'lishi mumkin.

Shuning uchun bu to'plam **o'z Caddy'sini ko'tarmaydi** va **birorta host
portini band qilmaydi**. U mavjud Caddy tarmog'iga qo'shiladi, Caddy esa
unga proksi qiladi.

```
                 caddy  (80/443 — TEGILMAYDI)
                    │
   ┌────────────────┼──────────────┐
   ▼                ▼              ▼
 boshqa loyiha   boshqa loyiha   vatan-front  ← YANGI
                                 vatan-api    ← YANGI
```

### Nima o'zgaradi va nima o'zgarmaydi

| | |
|---|---|
| ✅ Qo'shiladi | `vatan-api` va `vatan-front` konteynerlari (host porti yo'q) |
| ✅ Qo'shiladi | Mavjud Caddyfile oxiriga 2 ta site bloki (marker bilan) |
| ✅ Bajariladi | `caddy reload` — **restart emas**, saytlar uzilmaydi |
| ❌ Tegilmaydi | Mavjud konteynerlar, ularning tarmog'i, volume'lari |
| ❌ Tegilmaydi | 80/443 portlar, mavjud sertifikatlar |
| ❌ O'chirilmaydi | Caddyfile'dan hech nima — faqat qo'shiladi |

Har qadamda himoya bor: Caddyfile o'zgartirilishidan oldin **zaxira**
olinadi, o'zgartirilgach **`caddy validate`** bilan tekshiriladi, xato
bo'lsa **avtomatik qaytariladi**. O'rnatishdan oldin va keyin mavjud
saytlar HTTP kodi solishtiriladi.

Butunlay qaytarish: `sudo bash olib-tashla.sh`

---

## 1. Fayllarni ko'chirish

Mahalliy mashinadan:

```bash
scp vatan-tuygusi.zip root@SERVER_IP:/tmp/ && ssh root@SERVER_IP 'mkdir -p /opt && unzip -oq /tmp/vatan-tuygusi.zip -d /opt && rm -rf /opt/vatan && mv /opt/vatan-analiz /opt/vatan && echo tayyor'
```

## 2. Sozlash

```bash
cd /opt/vatan/deploy && cp .env.example .env && nano .env
```

Majburiy — faqat bitta qator: `ANTHROPIC_API_KEY=`

Kalitni ekranga chiqarmasdan:

```bash
cd /opt/vatan/deploy && read -rsp "kalit: " K && sed -i "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=$K|" .env && unset K && echo
```

Caddy sozlamalari (`CADDY_KONTEYNER`, `CADDY_TARMOQ`, `CADDY_FAYL`)
**avtomatik aniqlanadi** — qo'lda yozish shart emas.

## 3. Instagram cookies (odatda shart emas)

Ochiq postlar cookies'siz yuklanadi — sinovda 8 ta reel muammosiz olindi.
Cookies faqat yopiq profillar uchun yoki Instagram serveringiz IP sini
cheklab qo'ysa kerak bo'ladi.

```bash
scp cookies.txt root@SERVER_IP:/opt/vatan/deploy/secrets/cookies.txt
```

so'ng `.env` da izohdan chiqaring: `VATAN_IG_COOKIES=/secrets/cookies.txt`

## 4. Ishga tushirish

```bash
cd /opt/vatan/deploy && sudo bash ornat.sh
```

Birinchi qurish **10-15 daqiqa** (Whisper modeli image ichiga yuklanadi).

Tugagach:
- **https://vatan.example.uz**
- **https://vatanapi.example.uz/tekshiruv**

SSL'ni mavjud Caddy o'zi oladi — yangi domenlar uchun ham avtomatik.

---

## Qanday tekshirish (qo'lda)

O'rnatishdan **oldin** hozirgi holatni yozib oling:

```bash
docker ps --format '{{.Names}}\t{{.Status}}' | tee /tmp/oldin.txt
for d in $(grep -oE '^[a-z0-9.-]+\.[a-z]{2,}' /root/nazorat/Caddyfile | sort -u); do printf '%-32s %s\n' "$d" "$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 https://$d)"; done
```

Keyin shu buyruqni qayta yugurtirib solishtiring. `ornat.sh` buni o'zi ham
qiladi va farqni ko'rsatadi.

---

## MUHIM: nega bitta worker

`Dockerfile` da `gunicorn --workers 1 --threads 16` — **o'zgartirmang**.

Seanslar (`SEANSLAR` lug'ati) va fon oqimlari jarayon xotirasida turadi.
Ikkinchi worker qo'shilsa polling so'rovi boshqa jarayonga tushib
**"Seans topilmadi"** xatosi chiqadi. Kengaytirish kerak bo'lsa — avval
Redis'ga o'tish.

---

## Kundalik buyruqlar

```bash
cd /opt/vatan/deploy
docker compose ps
docker compose logs -f api
docker compose restart api
docker compose down                  # to'xtatish (Caddyfile bloki qoladi)
sudo bash olib-tashla.sh             # butunlay olib tashlash
```

### Kodni yangilash

Front (`static/index.html`) — Caddy'ga to'g'ridan-to'g'ri ulangan,
qurish shart emas:

```bash
scp -r static/ root@SERVER_IP:/opt/vatan/ && ssh root@SERVER_IP 'docker restart vatan-front'
```

Backend:

```bash
scp analiz.py app.py root@SERVER_IP:/opt/vatan/ && ssh root@SERVER_IP 'cd /opt/vatan/deploy && docker compose up -d --build api'
```

---

## Resurslar

Konteynerga chegara qo'yilgan (`.env` da): **1.5 yadro, 2.5 GB RAM**.
Boshqa dasturlar protsessorsiz qolmasligi uchun. Kamaytirish kerak bo'lsa
`VATAN_PARALLEL=1` qiling.

| Volume | Nima | O'chirilsa |
|---|---|---|
| `deploy_vatan-data` | SQLite: video keshi va seans tarixi | Kesh yo'qoladi, tahlillar qaytadan pulga tushadi |

Mavjud Caddy'ning `caddy-data` volume'iga **tegilmaydi** — sertifikatlar
xavfsiz.

---

## Muammolarni hal qilish

**`Caddy konteyneri topilmadi`** — `.env` ga qo'lda yozing:
```
CADDY_KONTEYNER=caddy
```

**`Caddy vatan-front ga yeta olmadi`** — tarmoq noto'g'ri aniqlangan:
```bash
docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' caddy
```
natijani `.env` ga: `CADDY_TARMOQ=...`

**Caddyfile tekshiruvdan o'tmadi** — skript avtomatik qaytaradi, sizning
sozlamangiz o'zgarmaydi. Zaxira: `Caddyfile.vatan-zaxira-SANA`

**Front ochiladi, lekin "Server sozlanmagan"** — brauzer konsolida CORS
xatosi bo'lsa, `.env` dagi `VATAN_FRONT_ORIGIN` front domeni bilan aynan
mos kelishi kerak.

**Barcha videolar "ochilmadi"** — avval havolalarni brauzerda tekshiring
(o'chirilgan yoki yopiq bo'lishi mumkin). Havolalar ishlasa-yu tizim
ololmasa, Instagram server IP sini cheklagan bo'lishi mumkin — o'shanda
cookies qo'shing va `docker compose restart api`.

---

## Xavfsizlik

- `.env` va `secrets/` git ga tushmaydi.
- API konteyneri **root'siz** (`uid 10001`).
- `/sinov` standart holda **o'chiq**.
- Autentifikatsiya yo'q — front ochiq. Cheklash kerak bo'lsa
  `caddy-blok.txt` ga `basic_auth` qo'shing.
