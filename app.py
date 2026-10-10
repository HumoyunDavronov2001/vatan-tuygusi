#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VATAN TUYG'USI — web server.

Oqim: foydalanuvchi 10 ta Instagram link kiritadi -> har biri alohida
tahlil qilinadi -> 10 talik natijadan shaxsiy PORTRET yasaladi.

    POST /seans        {linklar:[...]}  -> {"seans_id": "..."}
    GET  /seans/<id>                    -> jarayon + natijalar + portret

Ishga tushirish:
    export ANTHROPIC_API_KEY=...
    python app.py            ->  http://127.0.0.1:5057
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory

import analiz
from analiz import TahlilXatosi

BU_PAPKA = os.path.dirname(os.path.abspath(__file__))
BAZA = os.environ.get("VATAN_BAZA") or os.path.join(BU_PAPKA, "tarix.db")

HOST = os.environ.get("VATAN_HOST", "127.0.0.1")
PORT = int(os.environ.get("VATAN_PORT", "5057"))

# Bir vaqtda nechta video tahlil qilinadi. Whisper protsessorni band qiladi.
PARALLEL = int(os.environ.get("VATAN_PARALLEL", "2"))

app = Flask(__name__, static_folder=None)

# Front alohida domenda turgani uchun brauzer CORS talab qiladi.
# Vergul bilan ajratilgan ro'yxat. Bo'sh bo'lsa — CORS umuman berilmaydi
# (mahalliy ishlatishda front va API bir xil manzilda bo'ladi).
FRONT_ORIGIN = os.environ.get("VATAN_FRONT_ORIGIN", "").strip()
RUXSAT_ORIGIN = [o.strip().rstrip("/") for o in FRONT_ORIGIN.split(",") if o.strip()]


@app.after_request
def cors_sarlavhalari(javob):
    if not RUXSAT_ORIGIN:
        return javob
    kelgan = (request.headers.get("Origin") or "").rstrip("/")
    javob.headers["Vary"] = "Origin"
    if kelgan in RUXSAT_ORIGIN:
        javob.headers["Access-Control-Allow-Origin"] = kelgan
        javob.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        javob.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Sinov-Kalit"
        javob.headers["Access-Control-Max-Age"] = "600"
    return javob


SEANSLAR: dict[str, dict] = {}
QULF = threading.Lock()

# Xotirada saqlanadigan maksimal seans soni. Busiz SEANSLAR cheksiz o'sadi:
# har bir seans 10 ta to'liq tahlil natijasini (transkript bilan) ushlab turadi.
MAX_SEANS = int(os.environ.get("VATAN_MAX_SEANS", "50"))


def _seanslarni_tozala() -> None:
    """Tugagan eski seanslarni xotiradan olib tashlaydi. QULF ichida chaqiriladi."""
    if len(SEANSLAR) <= MAX_SEANS:
        return
    olib_tashlash = len(SEANSLAR) - MAX_SEANS
    for kalit in list(SEANSLAR):
        if olib_tashlash <= 0:
            break
        if SEANSLAR[kalit].get("holat") in ("tayyor", "xato"):
            del SEANSLAR[kalit]
            olib_tashlash -= 1


# ---------------------------------------------------------------------------
# Baza: video keshi + seans tarixi
# ---------------------------------------------------------------------------


@contextmanager
def baza_ochish():
    """
    SQLite ulanishi. `with sqlite3.connect(...)` faqat tranzaksiyani yopadi,
    ULANISHNI YOPMAYDI — shuning uchun bu yerda finally da close() qilinadi.
    Aks holda har so'rovda bitta ulanish sizib qoladi.
    """
    ulanish = sqlite3.connect(BAZA, timeout=10)
    ulanish.row_factory = sqlite3.Row
    try:
        with ulanish:
            yield ulanish
    finally:
        ulanish.close()


def baza_tayyorla() -> None:
    with baza_ochish() as u:
        u.execute("""
            CREATE TABLE IF NOT EXISTS tarix (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                kalit   TEXT UNIQUE,
                link    TEXT NOT NULL,
                vaqt    TEXT NOT NULL,
                muallif TEXT,
                qaror   TEXT,
                ball    INTEGER,
                natija  TEXT NOT NULL
            )
        """)
        u.execute("""
            CREATE TABLE IF NOT EXISTS seanslar (
                id       TEXT PRIMARY KEY,
                vaqt     TEXT NOT NULL,
                sarlavha TEXT,
                ortacha  INTEGER,
                jami     INTEGER,
                malumot  TEXT NOT NULL
            )
        """)


def keshdan_ol(kalit: str) -> dict | None:
    if not kalit:
        return None
    with baza_ochish() as u:
        qator = u.execute(
            "SELECT natija, vaqt FROM tarix WHERE kalit = ?", (kalit,)
        ).fetchone()
    if not qator:
        return None
    natija = json.loads(qator["natija"])
    natija["_keshdan"] = qator["vaqt"]
    return natija


def keshga_yoz(kalit: str, natija: dict) -> None:
    if not kalit:
        return
    video = natija.get("video", {})
    with baza_ochish() as u:
        u.execute(
            """INSERT INTO tarix (kalit, link, vaqt, muallif, qaror, ball, natija)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(kalit) DO UPDATE SET
                   vaqt = excluded.vaqt, qaror = excluded.qaror,
                   ball = excluded.ball, natija = excluded.natija""",
            (
                kalit, video.get("link", ""),
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                video.get("muallif", ""), natija.get("qaror", ""),
                int(natija.get("ball", 0) or 0),
                json.dumps(natija, ensure_ascii=False),
            ),
        )


def seans_saqla(seans_id: str, seans: dict) -> None:
    portret = seans.get("portret") or {}
    stat = portret.get("statistika", {})
    with baza_ochish() as u:
        u.execute(
            """INSERT INTO seanslar (id, vaqt, sarlavha, ortacha, jami, malumot)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   sarlavha = excluded.sarlavha, ortacha = excluded.ortacha,
                   jami = excluded.jami, malumot = excluded.malumot""",
            (
                seans_id, datetime.now().strftime("%Y-%m-%d %H:%M"),
                portret.get("sarlavha", ""), int(stat.get("ortacha_ball", 0) or 0),
                int(stat.get("jami", 0) or 0),
                json.dumps(seans, ensure_ascii=False),
            ),
        )


# ---------------------------------------------------------------------------
# Seans bajarilishi
# ---------------------------------------------------------------------------


def video_yangila(seans_id: str, indeks: int, **maydonlar) -> None:
    with QULF:
        seans = SEANSLAR.get(seans_id)
        if seans:
            seans["videolar"][indeks].update(maydonlar)


def seans_yangila(seans_id: str, **maydonlar) -> None:
    with QULF:
        if seans_id in SEANSLAR:
            SEANSLAR[seans_id].update(maydonlar)


def _bekormi(seans_id: str) -> bool:
    with QULF:
        return bool(SEANSLAR.get(seans_id, {}).get("bekor"))


def bitta_video(seans_id: str, indeks: int, link: str, qayta: bool) -> dict | None:
    """Bitta videoni tahlil qiladi. Xato bo'lsa None qaytaradi (seans to'xtamaydi)."""
    # Foydalanuvchi bekor qilgan bo'lsa — navbatdagi videoni boshlamaymiz.
    # Boshlangani tugaguncha davom etadi (uni uzib bo'lmaydi).
    if _bekormi(seans_id):
        video_yangila(seans_id, indeks, holat="xato", xato="Bekor qilindi.")
        return None
    video_yangila(seans_id, indeks, holat="jarayonda", qadam=0,
                  qadam_matn=analiz.QADAMLAR[0])
    try:
        _, kod = analiz.link_tekshir(link)
        kalit = kod or link

        natija = None if qayta else keshdan_ol(kalit)
        if natija is None:
            natija = analiz.tahlil_qil(
                link,
                holat=lambda i, m: video_yangila(seans_id, indeks,
                                                 qadam=i, qadam_matn=m),
            )
            keshga_yoz(kalit, natija)

        video_yangila(
            seans_id, indeks, holat="tayyor",
            qadam=len(analiz.QADAMLAR), qadam_matn="Tayyor",
            qaror=str(natija.get("qaror", "")), ball=int(natija.get("ball", 0) or 0),
            mavzu=natija.get("mavzu", ""), muallif=natija.get("video", {}).get("muallif", ""),
            natija=natija,
        )
        return natija
    except TahlilXatosi as xato:
        video_yangila(seans_id, indeks, holat="xato", xato=str(xato))
        return None
    except Exception:
        traceback.print_exc()
        video_yangila(seans_id, indeks, holat="xato",
                      xato="Kutilmagan xato. Server jurnalini tekshiring.")
        return None
    finally:
        with QULF:
            seans = SEANSLAR.get(seans_id)
            if seans:
                seans["tugagan"] = sum(
                    1 for v in seans["videolar"] if v["holat"] in ("tayyor", "xato")
                )


def seans_bajar(seans_id: str, linklar: list[str], qayta: bool) -> None:
    try:
        with ThreadPoolExecutor(max_workers=max(1, PARALLEL)) as havza:
            natijalar = list(havza.map(
                lambda p: bitta_video(seans_id, p[0], p[1], qayta),
                list(enumerate(linklar)),
            ))

        if _bekormi(seans_id):
            seans_yangila(seans_id, holat="xato",
                          xato="Tahlil siz tomondan bekor qilindi.")
            return

        muvaffaq = [n for n in natijalar if n]
        if len(muvaffaq) < analiz.MIN_PORTRET:
            # Sabablarni sanab ko'rsatamiz — "cookies yo'qligi" deb taxmin
            # qilish o'rniga haqiqatda nima bo'lganini aytamiz.
            with QULF:
                seans = SEANSLAR.get(seans_id, {})
                xatolar = [v.get("xato", "") for v in seans.get("videolar", [])
                           if v.get("holat") == "xato"]
            sanoq: dict[str, int] = {}
            for x in xatolar:
                bosh = (x or "").strip().split("\n")[0][:120]
                sanoq[bosh] = sanoq.get(bosh, 0) + 1
            royxat = "\n".join(
                f"  \u2022 {soni} ta: {sabab}"
                for sabab, soni in sorted(sanoq.items(), key=lambda x: -x[1])
            )
            seans_yangila(
                seans_id, holat="xato",
                xato=(
                    f"{len(linklar)} ta videodan faqat {len(muvaffaq)} tasi "
                    f"tahlil qilindi. Portret uchun kamida "
                    f"{analiz.MIN_PORTRET} ta kerak.\n\nSabablar:\n{royxat}"
                ),
            )
            return

        seans_yangila(seans_id, bosqich="portret")
        portret = analiz.portret_yasa(muvaffaq)
        seans_yangila(seans_id, holat="tayyor", portret=portret)

        with QULF:
            nusxa = json.loads(json.dumps(SEANSLAR.get(seans_id, {}),
                                          ensure_ascii=False))
        seans_saqla(seans_id, nusxa)
    except TahlilXatosi as xato:
        seans_yangila(seans_id, holat="xato", xato=str(xato))
    except Exception:
        traceback.print_exc()
        seans_yangila(seans_id, holat="xato",
                      xato="Kutilmagan xato. Server jurnalini tekshiring.")


# ---------------------------------------------------------------------------
# Marshrutlar
# ---------------------------------------------------------------------------


@app.get("/")
def bosh_sahifa():
    return send_from_directory(os.path.join(BU_PAPKA, "static"), "index.html")


@app.get("/config.js")
def config_js():
    """
    Front API manzilini shu fayldan oladi.
    Ishlab chiqarishda uni Caddy beradi; mahalliy ishlatishda esa shu marshrut
    kerak — busiz har yuklanishda 404 chiqardi va sozlama yuklanmasdi.
    """
    javob = send_from_directory(os.path.join(BU_PAPKA, "static"), "config.js")
    javob.headers["Cache-Control"] = "no-cache, must-revalidate"
    return javob


@app.post("/seans")
def seans_boshla():
    malumot = request.get_json(silent=True) or {}
    linklar = [str(x).strip() for x in (malumot.get("linklar") or []) if str(x).strip()]
    qayta = bool(malumot.get("qayta"))

    if len(linklar) < analiz.VIDEO_MIN or len(linklar) > analiz.VIDEO_MAX:
        return jsonify({
            "xato": f"{analiz.VIDEO_MIN} tadan {analiz.VIDEO_MAX} tagacha havola "
                    f"kerak. Hozir: {len(linklar)} ta."
        }), 400

    korilgan: dict[str, int] = {}
    for i, link in enumerate(linklar, start=1):
        try:
            toza, kod = analiz.link_tekshir(link)
        except TahlilXatosi as xato:
            return jsonify({"xato": f"{i}-link: {xato}"}), 400
        # Takror link ikki marta tahlil qilinadi: bir xil kesh kalitiga ikki
        # oqim yozadi va portret bitta videoni ikki marta hisoblaydi.
        kalit = kod or toza
        if kalit in korilgan:
            return jsonify({
                "xato": f"{i}-link {korilgan[kalit]}-link bilan bir xil. "
                        f"Har bir video faqat bir marta bo'lishi kerak."
            }), 400
        korilgan[kalit] = i

    seans_id = uuid.uuid4().hex
    with QULF:
        _seanslarni_tozala()
        SEANSLAR[seans_id] = {
            "holat": "jarayonda",
            "bosqich": "videolar",
            "jami": len(linklar),
            "tugagan": 0,
            "portret": None,
            "xato": "",
            "videolar": [
                {"link": L, "holat": "kutmoqda", "qadam": 0, "qadam_matn": "",
                 "qaror": "", "ball": 0, "mavzu": "", "muallif": "",
                 "xato": "", "natija": None}
                for L in linklar
            ],
        }
    threading.Thread(target=seans_bajar, args=(seans_id, linklar, qayta),
                     daemon=True).start()
    return jsonify({"seans_id": seans_id, "qadamlar": analiz.QADAMLAR})


@app.post("/seans/<seans_id>/bekor")
def seans_bekor(seans_id: str):
    """Tahlilni to'xtatadi. Boshlangan videolar tugaydi, qolganlari o'tkaziladi."""
    with QULF:
        seans = SEANSLAR.get(seans_id)
        if seans is None:
            return jsonify({"xato": "Seans topilmadi."}), 404
        seans["bekor"] = True
    return jsonify({"holat": "bekor qilinmoqda"})


@app.get("/seans/<seans_id>")
def seans_holati(seans_id: str):
    with QULF:
        seans = SEANSLAR.get(seans_id)
        if seans is not None:
            return jsonify(_yengil(seans))
    # Xotirada yo'q — bazadan
    with baza_ochish() as u:
        qator = u.execute(
            "SELECT malumot FROM seanslar WHERE id = ?", (seans_id,)
        ).fetchone()
    if not qator:
        return jsonify({"xato": "Seans topilmadi."}), 404
    return jsonify(json.loads(qator["malumot"]))


def _yengil(seans: dict) -> dict:
    """To'liq video natijalarini olib tashlab, yengil holat qaytaradi."""
    nusxa = {k: v for k, v in seans.items() if k != "videolar"}
    nusxa["videolar"] = [
        {k: v for k, v in vid.items() if k != "natija"} for vid in seans["videolar"]
    ]
    return nusxa


@app.get("/seans/<seans_id>/video/<int:indeks>")
def seans_video(seans_id: str, indeks: int):
    with QULF:
        seans = SEANSLAR.get(seans_id)
        if seans and 0 <= indeks < len(seans["videolar"]):
            return jsonify(seans["videolar"][indeks].get("natija") or {})
    with baza_ochish() as u:
        qator = u.execute(
            "SELECT malumot FROM seanslar WHERE id = ?", (seans_id,)
        ).fetchone()
    if qator:
        seans = json.loads(qator["malumot"])
        if 0 <= indeks < len(seans.get("videolar", [])):
            return jsonify(seans["videolar"][indeks].get("natija") or {})
    return jsonify({"xato": "Topilmadi."}), 404


@app.get("/seanslar")
def seanslar_royxati():
    with baza_ochish() as u:
        qatorlar = u.execute(
            """SELECT id, vaqt, sarlavha, ortacha, jami
               FROM seanslar ORDER BY rowid DESC LIMIT 20"""
        ).fetchall()
    return jsonify([dict(q) for q in qatorlar])


@app.get("/sogliq")
def sogliq():
    """Docker healthcheck uchun — tashqi bog'liqliksiz, tez javob."""
    return jsonify({"holat": "tirik"})


@app.get("/tekshiruv")
def tekshiruv():
    """Muhit to'g'ri sozlanganini ko'rsatadi — taxmin qilmasdan."""

    def bormi(nom: str) -> bool:
        try:
            __import__(nom)
            return True
        except ImportError:
            return False

    return jsonify({
        "ANTHROPIC_API_KEY": bool(
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        ),
        "ffmpeg": bool(analiz.ffmpeg_yoli()),
        "ffprobe": bool(analiz.ffprobe_yoli()),
        "yt_dlp": bormi("yt_dlp"),
        "anthropic": bormi("anthropic"),
        "faster_whisper": bormi("faster_whisper"),
        "pillow": bormi("PIL"),
        "backend": analiz.BACKEND,
        "model": analiz.MODEL,
        "video_min": analiz.VIDEO_MIN,
        "video_max": analiz.VIDEO_MAX,
        "video_soni": analiz.VIDEO_MAX,
        "kadr_soni": analiz.KADR_SONI,
        "whisper_model": analiz.WHISPER_MODEL,
        "ig_cookies": bool(analiz.IG_COOKIES_FAYL or analiz.IG_COOKIES_BRAUZER),
    })


@app.post("/sinov")
def sinov():
    """
    Claude ulanishini sun'iy kadr (uch rangli bayroq) bilan tekshiradi.
    Instagram'siz, faqat API zanjirini sinash uchun. Bir marta ~$0.01 turadi.

    Bu PULLI chaqiruv, shuning uchun ikki qavat himoya bor: faqat POST
    (rasm/prefetch qo'zg'ata olmaydi) va maxsus kalit (boshqa sayt yuborgan
    forma so'rovi o'tmaydi):

        export VATAN_SINOV_KALIT=xohlagan-sozingiz
        curl -s -X POST -H "X-Sinov-Kalit: $VATAN_SINOV_KALIT" \\
             http://127.0.0.1:5057/sinov
    """
    kutilgan = os.environ.get("VATAN_SINOV_KALIT", "")
    if not kutilgan:
        return jsonify({
            "xato": "Sinov o'chirilgan. Yoqish uchun VATAN_SINOV_KALIT "
                    "muhit o'zgaruvchisini o'rnating."
        }), 403
    if request.headers.get("X-Sinov-Kalit") != kutilgan:
        return jsonify({"xato": "X-Sinov-Kalit noto'g'ri."}), 403
    import shutil as sh
    import subprocess
    import tempfile

    papka = tempfile.mkdtemp(prefix="sinov_")
    try:
        bayroq = os.path.join(papka, "kadr_00.jpg")
        subprocess.run([
            analiz.ffmpeg_yoli(), "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=0x0099B5:s=600x200",
            "-f", "lavfi", "-i", "color=c=white:s=600x200",
            "-f", "lavfi", "-i", "color=c=0x1EB53A:s=600x200",
            "-filter_complex", "vstack=inputs=3", "-frames:v", "1", bayroq,
        ], capture_output=True, timeout=60)
        kadrlar = [{"vaqt": 0.0, "yorliq": "0:00", "fayl": bayroq}]
        nutq = {"matn": "", "til": "", "segmentlar": [], "xato": "sinov"}
        video = analiz.VideoMalumot(
            fayl="", caption="Mustaqilligimiz muborak bo'lsin!",
            muallif="sinov", davomiylik=5.0,
        )
        return jsonify(analiz.baholash(kadrlar, nutq, video))
    except TahlilXatosi as x:
        return jsonify({"xato": str(x)}), 500
    finally:
        sh.rmtree(papka, ignore_errors=True)


# Jadvallarni IMPORT paytida yaratamiz. Gunicorn `app:app` ni import qiladi,
# `__main__` bloki esa umuman ishlamaydi — busiz birinchi so'rovda
# "no such table" xatosi chiqardi.
baza_tayyorla()


if __name__ == "__main__":
    print(f"VATAN TUYG'USI  ->  http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, threaded=True)
