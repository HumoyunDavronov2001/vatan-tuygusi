#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VATAN TUYG'USI — Instagram video tahlil yadrosi.

Zanjir:
    link  ->  yt-dlp (video fayli + caption)
          ->  ffmpeg (N ta kalit kadr + 16 kHz mono ovoz)
          ->  faster-whisper (nutq -> matn)
          ->  Claude (kadr + matn + caption bo'yicha yakuniy baho)

Halollik qoidasi: natijada FAQAT haqiqatda olingan signallar ko'rsatiladi.
Ovoz transkripsiyasi bo'lmasa — "bor" deb ko'rsatilmaydi, sababi yoziladi.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Sozlamalar (hammasi muhit o'zgaruvchisidan)
# ---------------------------------------------------------------------------

MODEL = os.environ.get("VATAN_MODEL", "claude-opus-5")

# Tahlil vositasi:
#   "qoida"  -> APIsiz, mahalliy: kalit so'z lug'ati + kadr rang/ramz tahlili.
#              Tashqi xizmatga umuman ulanmaydi, pul ketmaydi.
#   "claude" -> Anthropic API (sifatliroq, ammo kredit kerak).
# Standart — "qoida" (dastur mustaqil, APIsiz ishlaydi).
BACKEND = os.environ.get("VATAN_BACKEND", "qoida").strip().lower()

# Bitta seansda video soni — oraliq (3 tadan 5 tagacha).
# Foydalanuvchi kamida MIN, ko'pi bilan MAX havola kiritadi.
VIDEO_MIN = int(os.environ.get("VATAN_VIDEO_MIN", "3"))
VIDEO_MAX = int(os.environ.get("VATAN_VIDEO_MAX", "5"))
VIDEO_SONI = VIDEO_MAX   # eski nom bilan moslik

# Portret yasash uchun kamida shuncha video muvaffaqiyatli tahlil bo'lishi kerak.
MIN_PORTRET = int(os.environ.get("VATAN_MIN_PORTRET", "3"))

# Nechta kadr olinadi va uzun tomoni necha piksel bo'ladi.
# 896 px 9:16 kadr uchun ~576 vizual token beradi -> 8 kadr ~4600 token.
KADR_SONI = int(os.environ.get("VATAN_KADR_SONI", "8"))
KADR_ULCHAM = int(os.environ.get("VATAN_KADR_ULCHAM", "896"))

# Whisper modeli: tiny / base / small / medium / large-v3
WHISPER_MODEL = os.environ.get("VATAN_WHISPER_MODEL", "small")
# Bo'sh qoldirilsa — til avtomatik aniqlanadi.
WHISPER_TIL = os.environ.get("VATAN_WHISPER_TIL", "uz").strip()

# Video yuklash uchun proksi (socks5:// yoki http://).
# NEGA KERAK: server data-markazda turadi va YouTube/Instagram data-markaz
# IP'larini "bot" deb rad etadi ("tasdiqlang" xatosi). Serverda Cloudflare
# WARP'ni proksi rejimida ishga tushirib, shu yerga yozamiz:
#     VATAN_PROXY=socks5://host.docker.internal:40000
# Faqat video yuklashga ta'sir qiladi — saytning kiruvchi trafigiga tegmaydi.
PROXY = os.environ.get("VATAN_PROXY", "").strip()

# Instagram login talab qilsa — cookies kerak bo'ladi.
IG_COOKIES_FAYL = os.environ.get("VATAN_IG_COOKIES", "").strip()
IG_COOKIES_BRAUZER = os.environ.get("VATAN_IG_COOKIES_BRAUZER", "").strip()

MAX_DAVOMIYLIK = int(os.environ.get("VATAN_MAX_DAVOMIYLIK", "600"))  # soniya
MAX_HAJM_MB = int(os.environ.get("VATAN_MAX_HAJM_MB", "200"))

FFMPEG = os.environ.get("VATAN_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("VATAN_FFPROBE", "ffprobe")


class TahlilXatosi(Exception):
    """Foydalanuvchiga ko'rsatiladigan, tushunarli xato."""


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe qayerda?
# Tizimda bo'lmasa — imageio-ffmpeg bilan kelgan static binary ishlatiladi,
# shunda `sudo apt install ffmpeg` shart emas.
# ---------------------------------------------------------------------------

_FFMPEG_YOL: Optional[str] = None
_FFPROBE_YOL: Optional[str] = None


def ffmpeg_yoli() -> str:
    """ffmpeg bajariladigan fayl yo'li. Topilmasa bo'sh satr."""
    global _FFMPEG_YOL
    if _FFMPEG_YOL is None:
        yol = shutil.which(FFMPEG) or ""
        if not yol:
            try:
                import imageio_ffmpeg
                nomzod = imageio_ffmpeg.get_ffmpeg_exe()
                yol = nomzod if os.path.exists(nomzod) else ""
            except Exception:
                yol = ""
        _FFMPEG_YOL = yol
    return _FFMPEG_YOL


def ffprobe_yoli() -> str:
    """ffprobe yo'li. imageio-ffmpeg ffprobe bermaydi — bo'sh bo'lishi normal."""
    global _FFPROBE_YOL
    if _FFPROBE_YOL is None:
        _FFPROBE_YOL = shutil.which(FFPROBE) or ""
    return _FFPROBE_YOL


_FFMPEG_PAPKA: Optional[str] = None
_JS_RUNTIME: Optional[dict] = None


def js_runtime() -> dict:
    """
    yt-dlp uchun JavaScript runtime. YouTube video imzosini (nsig) ochish
    node/deno/bun talab qiladi — usiz YouTube yuklashlari BEQAROR bo'ladi
    ("No supported JavaScript runtime"). Topilgan birinchisini qaytaradi,
    aks holda bo'sh lug'at (imkon qadar ishlaydi).
    """
    global _JS_RUNTIME
    if _JS_RUNTIME is not None:
        return _JS_RUNTIME
    # (yt-dlp kaliti, tizimdagi binar nomlari). Debian'da binar "nodejs" deb
    # atalishi mumkin, lekin yt-dlp kaliti "node" bo'lishi shart.
    for kalit, nomlar in (("deno", ("deno",)),
                          ("node", ("node", "nodejs")),
                          ("bun", ("bun",))):
        for nom in nomlar:
            yol = shutil.which(nom)
            if yol:
                _JS_RUNTIME = {kalit: {"path": yol}}
                return _JS_RUNTIME
    _JS_RUNTIME = {}
    return _JS_RUNTIME


def ffmpeg_papka() -> str:
    """
    yt-dlp video+ovozni birlashtirish uchun `ffmpeg` deb ATALGAN binarni
    izlaydi. imageio-ffmpeg binarisi boshqa nomda ('ffmpeg-linux-...'),
    shuning uchun to'g'ri nomli symlink yasab, uning papkasini qaytaramiz.
    YouTube/TikTok alohida oqim beradi — busiz "ffmpeg is not installed"
    xatosi chiqadi. Tizim ffmpeg'i bo'lsa uning papkasi qaytariladi.
    """
    global _FFMPEG_PAPKA
    if _FFMPEG_PAPKA is not None:
        return _FFMPEG_PAPKA
    ffm = ffmpeg_yoli()
    if not ffm:
        _FFMPEG_PAPKA = ""
        return ""
    if os.path.basename(ffm) in ("ffmpeg", "ffmpeg.exe"):
        _FFMPEG_PAPKA = os.path.dirname(ffm)   # tizim ffmpeg'i — to'g'ri nomli
        return _FFMPEG_PAPKA
    d = os.path.join(tempfile.gettempdir(), "vatan_ffmpeg")
    try:
        os.makedirs(d, exist_ok=True)
        nishon = os.path.join(d, "ffmpeg")
        if not os.path.exists(nishon):
            os.symlink(ffm, nishon)
        pr = ffprobe_yoli()
        pr_nishon = os.path.join(d, "ffprobe")
        if pr and not os.path.exists(pr_nishon):
            os.symlink(pr, pr_nishon)
        _FFMPEG_PAPKA = d
    except Exception:
        _FFMPEG_PAPKA = os.path.dirname(ffm)   # symlink bo'lmasa — bor yo'l
    return _FFMPEG_PAPKA


# ---------------------------------------------------------------------------
# 1-qadam. Link
# ---------------------------------------------------------------------------

# Har bir platforma: (nom, host-regex, kod-regex). Kod dedup/kesh kaliti uchun.
_PLATFORMALAR = [
    ("instagram",
     re.compile(r"^https?://(www\.)?(instagram\.com|instagr\.am|ig\.me)/", re.I),
     re.compile(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]{5,})", re.I)),
    ("youtube",
     re.compile(r"^https?://(www\.|m\.)?(youtube\.com|youtu\.be|youtube-nocookie\.com)/", re.I),
     re.compile(r"(?:shorts/|watch\?v=|youtu\.be/|embed/|/v/)([A-Za-z0-9_-]{6,})", re.I)),
    ("tiktok",
     re.compile(r"^https?://(www\.|vm\.|vt\.|m\.)?tiktok\.com/", re.I),
     re.compile(r"/video/(\d{6,})|/(?:t|v)/([A-Za-z0-9]{5,})|vm\.tiktok\.com/([A-Za-z0-9]{5,})", re.I)),
    ("facebook",
     re.compile(r"^https?://(www\.|web\.|m\.|fb\.)?(facebook\.com|fb\.watch|fb\.me)/", re.I),
     re.compile(r"/(?:reel|videos|watch)/?\??v?=?(\d{6,})|fb\.watch/([A-Za-z0-9_-]{5,})|/videos/([A-Za-z0-9._-]{5,})", re.I)),
]

# Foydalanuvchiga ko'rsatiladigan platforma nomlari
PLATFORMA_NOM = {"instagram": "Instagram", "youtube": "YouTube", "tiktok": "TikTok", "facebook": "Facebook"}


def platforma_aniqla(link: str) -> str:
    """Havola qaysi platformaga tegishli. Topilmasa bo'sh satr."""
    link = (link or "").strip()
    if not link.lower().startswith(("http://", "https://")):
        link = "https://" + link
    for nom, host, _kod in _PLATFORMALAR:
        if host.match(link):
            return nom
    return ""


def link_tekshir(link: str) -> tuple[str, str]:
    """
    Linkni tozalaydi va platformasini aniqlaydi.
    Qaytaradi: (toza_link, kalit) — kalit "platforma:kod" ko'rinishida
    (masalan "youtube:abc123"), dedup va kesh uchun. Kod topilmasa
    kalit toza URL bo'ladi.
    """
    link = (link or "").strip()
    if not link:
        raise TahlilXatosi("Havola kiritilmadi.")
    if not link.lower().startswith(("http://", "https://")):
        link = "https://" + link

    for nom, host, kod_re in _PLATFORMALAR:
        if host.match(link):
            # Kodni QUERY bilan birga qidiramiz (watch?v=ID uchun shart)
            m = kod_re.search(link) or kod_re.search(link.split("?", 1)[0])
            kod = next((g for g in m.groups() if g), "") if m else ""
            # DIQQAT: toza URL yt-dlp yuklay oladigan bo'lishi SHART.
            # YouTube watch?v=ID da ID query'da — uni oddiy split("?") KESIB
            # tashlaydi va "youtube.com/watch" qoladi (video yo'q → xato).
            # Shuning uchun kanonik, yuklab bo'ladigan URL quramiz.
            if kod and nom == "youtube":
                toza = "https://www.youtube.com/watch?v=" + kod
            elif kod and nom == "instagram":
                toza = "https://www.instagram.com/reel/" + kod
            elif kod and nom == "facebook" and "watch" in link.lower() and "/watch/" not in link.lower():
                toza = "https://www.facebook.com/watch/?v=" + kod   # ID query'da
            else:
                toza = link.split("?", 1)[0].rstrip("/")   # TikTok/FB /video/id yo'lda
            kalit = (nom + ":" + kod) if kod else toza
            return toza, kalit

    raise TahlilXatosi(
        "Bu havola tanilmadi. Instagram, YouTube yoki TikTok havolasini kiriting.\n"
        "Masalan:\n"
        "  https://www.instagram.com/reel/XXXXXXXXXXX/\n"
        "  https://www.youtube.com/shorts/XXXXXXXXXXX\n"
        "  https://www.tiktok.com/@nom/video/1234567890"
    )


# ---------------------------------------------------------------------------
# 2-qadam. Videoni yuklash (yt-dlp)
# ---------------------------------------------------------------------------


@dataclass
class VideoMalumot:
    fayl: str
    caption: str = ""
    muallif: str = ""
    sarlavha: str = ""
    davomiylik: float = 0.0
    kichik_rasm: str = ""


class _JimLogger:
    """yt-dlp o'z xatosini stderr ga bosmasin — biz uni tarjima qilib beramiz."""

    def debug(self, xabar: str) -> None: pass
    def info(self, xabar: str) -> None: pass
    def warning(self, xabar: str) -> None: pass
    def error(self, xabar: str) -> None: pass


def video_yukla(link: str, papka: str) -> VideoMalumot:
    try:
        import yt_dlp
    except ImportError:
        raise TahlilXatosi(
            "yt-dlp o'rnatilmagan. Terminalda:  pip install -U yt-dlp"
        )

    opts = {
        "logger": _JimLogger(),
        "outtmpl": os.path.join(papka, "video.%(ext)s"),
        # Bir faylli mp4 bo'lsa o'shani (Instagram/TikTok) — aks holda
        # video+ovoz birlashtiriladi (YouTube). Balandlik 720 gacha —
        # ovoz va kadrlar uchun yetarli, hajm cheklangan.
        "format": ("b[ext=mp4][height<=?720]/"
                   "bv*[height<=?720]+ba/b"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
        "max_filesize": MAX_HAJM_MB * 1024 * 1024,
        "retries": 3,
        "socket_timeout": 30,
    }
    # ffmpeg papkasini beramiz — busiz YouTube/TikTok merge qilolmaydi
    _ffp = ffmpeg_papka()
    if _ffp:
        opts["ffmpeg_location"] = _ffp
    # YouTube imzosi uchun JS runtime — busiz YouTube beqaror ishlaydi
    _jsr = js_runtime()
    if _jsr:
        opts["js_runtimes"] = _jsr
    if IG_COOKIES_FAYL:
        opts["cookiefile"] = IG_COOKIES_FAYL
    elif IG_COOKIES_BRAUZER:
        opts["cookiesfrombrowser"] = (IG_COOKIES_BRAUZER, None, None, None)
    # Proksi — server (data-markaz) IP'si bloklanganda MAJBURIY.
    # YouTube/Instagram data-markaz IP'laridan "bot emasligingizni tasdiqlang"
    # deb rad etadi. Serverdagi WARP proksi (socks5) shu blokdan chiqaradi.
    if PROXY:
        opts["proxy"] = PROXY

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(link, download=True)
            if info.get("_type") == "playlist" and info.get("entries"):
                info = info["entries"][0]
            fayl = ydl.prepare_filename(info)
    except Exception as e:  # yt_dlp.utils.DownloadError va boshqalar
        raise TahlilXatosi(_yuklash_xatosini_tarjima(str(e), platforma_aniqla(link)))

    if not os.path.exists(fayl):
        # Kengaytma o'zgargan bo'lishi mumkin — papkadan qidiramiz.
        nomzodlar = [
            os.path.join(papka, n)
            for n in os.listdir(papka)
            if n.startswith("video.")
        ]
        if not nomzodlar:
            raise TahlilXatosi("Video yuklandi, lekin fayl topilmadi.")
        fayl = max(nomzodlar, key=os.path.getsize)

    davomiylik = float(info.get("duration") or 0.0)
    if davomiylik and davomiylik > MAX_DAVOMIYLIK:
        raise TahlilXatosi(
            f"Video juda uzun ({int(davomiylik)} soniya). "
            f"Chegara: {MAX_DAVOMIYLIK} soniya."
        )

    return VideoMalumot(
        fayl=fayl,
        caption=(info.get("description") or "").strip(),
        muallif=(info.get("uploader") or info.get("uploader_id") or "").strip(),
        sarlavha=(info.get("title") or "").strip(),
        davomiylik=davomiylik,
        kichik_rasm=(info.get("thumbnail") or "").strip(),
    )


_COOKIES_MASLAHATI = (
    "Yechim — brauzeringizdagi Instagram seansidan foydalaning.\n"
    "Ishga tushirishdan OLDIN quyidagilardan birini bajaring:\n"
    "  export VATAN_IG_COOKIES_BRAUZER=firefox     # yoki chrome, chromium\n"
    "  export VATAN_IG_COOKIES=~/cookies.txt   # cookies.txt fayli\n"
    "So'ng dasturni qayta ishga tushiring."
)


def _yuklash_xatosini_tarjima(xato: str, platforma: str = "") -> str:
    past = xato.lower()
    P = PLATFORMA_NOM.get(platforma, "Platforma")

    # Barcha platformalar uchun umumiy holatlar
    if "age" in past and ("confirm" in past or "restrict" in past or "sign in to confirm" in past):
        return (P + " bu videoni yosh cheklovi tufayli bermadi.\n"
                "Yosh cheklovli videolar cookies bilan ochiladi.")
    if "geo" in past or "not available in your country" in past or "blocked it in your country" in past:
        return P + " bu videoni sizning hududingizda ko'rsatmayapti (geo-blok)."
    if "video unavailable" in past or "removed" in past or "has been terminated" in past:
        return "Video topilmadi yoki o'chirilgan."

    if platforma == "youtube":
        if "sign in" in past or "not a bot" in past or "confirm you" in past:
            return ("YouTube bu videoni ko'rsatish uchun tasdiq so'rayapti "
                    "(bot emasligini). Boshqa video bilan urinib ko'ring yoki "
                    "keyinroq qayta urining.")
        if "private video" in past:
            return "Bu YouTube videosi yopiq (private)."
        if "members-only" in past or "join this channel" in past:
            return "Bu video faqat kanal a'zolari uchun."
    if platforma == "tiktok":
        if "handshake" in past or "timed out" in past or "connection" in past:
            return ("TikTok serveriga ulanib bo'lmadi. TikTok ba'zi tarmoqlardan "
                    "cheklangan bo'lishi mumkin — keyinroq urinib ko'ring.")
        if "private" in past:
            return "Bu TikTok videosi yoki profili yopiq."
    # DIQQAT: "empty media response" ni avval "login kerak" deb talqin qilgandik —
    # bu NOTO'G'RI edi. Sinov ko'rsatdi: 8 ta ochiq reel cookies'siz muammosiz
    # yuklandi. Bu javob deyarli har doim postning O'ZI yo'qligini bildiradi.
    if "empty media response" in past or "cookies" in past:
        return (
            "Instagram bu post uchun ma'lumot bermadi.\n"
            "Ehtimoliy sabablar (shu tartibda):\n"
            "  1. Post o'chirilgan yoki havola noto'g'ri\n"
            "  2. Profil yopiq (private)\n"
            "  3. Instagram shu IP dan so'rovlarni vaqtincha cheklagan\n\n"
            "Havola brauzerda ochilsa-yu shu xato chiqsa, uchinchi sabab "
            "bo'lishi mumkin — o'shanda cookies yordam beradi:\n"
            + _COOKIES_MASLAHATI
        )
    if "login" in past or "rate-limit" in past or "rate limit" in past:
        return (
            "Instagram login talab qilyapti yoki so'rovlar sonini cheklab qo'ydi.\n\n"
            + _COOKIES_MASLAHATI
        )
    if "private" in past:
        return (
            "Bu profil yopiq (private).\n"
            "Faqat siz o'sha profilga obuna bo'lgan hisobning cookies'i bilan "
            "ochiladi.\n\n" + _COOKIES_MASLAHATI
        )
    if "checkpoint" in past or "challenge" in past:
        return (
            "Instagram hisobingizni tekshirishni so'rayapti (checkpoint).\n"
            "Brauzerda Instagram'ga kiring, tekshiruvdan o'ting, keyin "
            "cookies'ni yangilang."
        )
    if "not exist" in past or "404" in past or "unavailable" in past:
        return "Video topilmadi yoki o'chirilgan."
    if "max_filesize" in past or "file is larger" in past:
        return f"Video hajmi {MAX_HAJM_MB} MB dan katta."
    if "ffmpeg" in past:
        return ("Video va ovozni birlashtirish uchun ffmpeg kerak. "
                "Konteynerda u bor; mahalliy ishga tushirishda: "
                "pip install imageio-ffmpeg")
    if "unsupported url" in past:
        return "Bu havola turi qo'llab-quvvatlanmaydi."
    return P + " videosini yuklab bo'lmadi. Texnik xabar:\n" + xato.strip()[:400]


# ---------------------------------------------------------------------------
# 3-qadam. ffmpeg — kadrlar va ovoz
# ---------------------------------------------------------------------------


def _ffmpeg_bormi() -> str:
    yol = ffmpeg_yoli()
    if not yol:
        raise TahlilXatosi(
            "ffmpeg topilmadi. Ikkita yechimdan biri:\n"
            "  pip install imageio-ffmpeg      (sudo kerak emas)\n"
            "  sudo apt install ffmpeg"
        )
    return yol


_DAVOMIYLIK_NAQSH = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")


def davomiylik_aniqla(video: str, zaxira: float = 0.0) -> float:
    """Aniq davomiylik: avval ffprobe, bo'lmasa ffmpeg chiqishidan o'qiladi."""
    probe = ffprobe_yoli()
    if probe:
        try:
            chiqish = subprocess.run(
                [
                    probe, "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    video,
                ],
                capture_output=True, text=True, timeout=30, check=True,
            ).stdout.strip()
            return float(chiqish)
        except Exception:
            pass

    # ffprobe yo'q (masalan imageio-ffmpeg holati) — ffmpeg jurnalidan olamiz.
    ffm = ffmpeg_yoli()
    if ffm:
        try:
            jurnal = subprocess.run(
                [ffm, "-nostdin", "-hide_banner", "-i", video],
                capture_output=True, text=True, timeout=30,
            ).stderr
            moslik = _DAVOMIYLIK_NAQSH.search(jurnal)
            if moslik:
                soat, daqiqa, soniya = moslik.groups()
                return int(soat) * 3600 + int(daqiqa) * 60 + float(soniya)
        except Exception:
            pass
    return zaxira


def vaqt_yorlig(soniya: float) -> str:
    soniya = max(0, int(soniya))
    return f"{soniya // 60}:{soniya % 60:02d}"


def kadr_ajrat(video: str, papka: str, davomiylik: float) -> list[dict]:
    """Video bo'ylab teng oraliqda KADR_SONI ta kadr oladi."""
    ffm = _ffmpeg_bormi()
    if davomiylik <= 0:
        davomiylik = 1.0

    # Boshi va oxiridagi qora kadrlarga tushmaslik uchun chetlardan chekinamiz.
    chekinish = min(0.5, davomiylik * 0.05)
    ichki = max(davomiylik - 2 * chekinish, 0.1)
    n = max(1, KADR_SONI)
    vaqtlar = [chekinish + ichki * (i + 0.5) / n for i in range(n)]

    kadrlar: list[dict] = []
    for i, t in enumerate(vaqtlar):
        chiqish = os.path.join(papka, f"kadr_{i:02d}.jpg")
        buyruq = [
            ffm, "-nostdin", "-y", "-loglevel", "error",
            "-ss", f"{t:.2f}", "-i", video,
            "-frames:v", "1",
            "-vf", f"scale=w={KADR_ULCHAM}:h={KADR_ULCHAM}"
                   ":force_original_aspect_ratio=decrease",
            "-q:v", "4",
            chiqish,
        ]
        try:
            subprocess.run(buyruq, capture_output=True, timeout=60, check=True)
        except Exception:
            continue  # bitta kadr chiqmasa — qolganini davom ettiramiz
        if os.path.exists(chiqish) and os.path.getsize(chiqish) > 0:
            kadrlar.append({"vaqt": t, "yorliq": vaqt_yorlig(t), "fayl": chiqish})

    if not kadrlar:
        raise TahlilXatosi("Videodan birorta ham kadr ajratib bo'lmadi.")
    return kadrlar


def ovoz_ajrat(video: str, papka: str) -> Optional[str]:
    """16 kHz mono WAV. Videoda ovoz yo'q bo'lsa — None."""
    ffm = _ffmpeg_bormi()
    chiqish = os.path.join(papka, "ovoz.wav")
    buyruq = [
        ffm, "-nostdin", "-y", "-loglevel", "error",
        "-i", video, "-vn", "-ac", "1", "-ar", "16000",
        "-f", "wav", chiqish,
    ]
    try:
        subprocess.run(buyruq, capture_output=True, timeout=300, check=True)
    except Exception:
        return None
    if os.path.exists(chiqish) and os.path.getsize(chiqish) > 1024:
        return chiqish
    return None


# ---------------------------------------------------------------------------
# 4-qadam. Nutq -> matn (faster-whisper)
# ---------------------------------------------------------------------------

_WHISPER = None
_WHISPER_QULF = threading.Lock()


def _whisper_yukla():
    global _WHISPER
    with _WHISPER_QULF:
        if _WHISPER is None:
            from faster_whisper import WhisperModel
            _WHISPER = WhisperModel(
                WHISPER_MODEL, device="cpu", compute_type="int8"
            )
        return _WHISPER


def transkripsiya(wav: str) -> dict:
    """
    Qaytaradi: {"matn":..., "til":..., "segmentlar":[{vaqt, matn}], "xato":...}
    Kutubxona yo'q bo'lsa — xato maydoni to'ldiriladi, dastur to'xtamaydi.
    """
    natija = {"matn": "", "til": "", "segmentlar": [], "xato": ""}
    try:
        model = _whisper_yukla()
    except ImportError:
        natija["xato"] = (
            "faster-whisper o'rnatilmagan (pip install faster-whisper) — "
            "ovoz tahlil qilinmadi"
        )
        return natija
    except Exception as e:
        natija["xato"] = f"Whisper modelini yuklab bo'lmadi: {e}"
        return natija

    try:
        segmentlar, info = model.transcribe(
            wav,
            language=(WHISPER_TIL or None),
            vad_filter=True,
            beam_size=5,
        )
        qismlar = []
        for s in segmentlar:
            matn = (s.text or "").strip()
            if matn:
                qismlar.append({"vaqt": vaqt_yorlig(s.start), "matn": matn})
        natija["segmentlar"] = qismlar
        natija["matn"] = " ".join(q["matn"] for q in qismlar).strip()
        natija["til"] = getattr(info, "language", "") or ""
    except Exception as e:
        natija["xato"] = f"Transkripsiya xatosi: {e}"
    return natija


# ---------------------------------------------------------------------------
# 5-qadam. Claude bahosi
# ---------------------------------------------------------------------------

TIZIM_KO_RSATMASI = """\
Sen O'zbekiston ijtimoiy tarmoq kontentini tahlil qiluvchi ekspertsan.
Vazifang: berilgan videoda VATAN TUYG'USI (vatanparvarlik ohangi) bor-yo'qligini
aniqlash va qaroringni dalillar bilan asoslash.

VATAN TUYG'USI DEB HISOBLANADI:
- O'zbekiston davlat ramzlari: bayroq, gerb, madhiya
- Vatanparvarlik qo'shiqlari, she'rlari, nutqlari ("Vatan", "Ona yurt", "O'zbekistonim")
- Mustaqillik, milliy g'urur, xalq birligi mavzusi
- Harbiy xizmat, chegarachilar, askarlarga hurmat va minnatdorchilik
- Xotira va qadrlash: urush faxriylari, shahidlar xotirasi
- Milliy qahramonlar: Amir Temur, Alisher Navoiy, Bobur, Ulug'bek, Jaloliddin Manguberdi
- Vatan tabiati yoki shaharlariga MUHABBAT IZHORI (shunchaki ko'rsatish emas)
- Sport g'alabalari: O'zbekiston jamoasi, bayroq ko'tarilishi, madhiya yangrashi
- Milliy bayramlar: Mustaqillik kuni, Konstitutsiya kuni, Xotira va qadrlash kuni
- Vatan sog'inchi, muhojirlikdan vatanga qaytish mavzusi

VATAN TUYG'USI EMAS — CHALKASHTIRMA:
- Sof milliy MADANIYAT: osh, milliy taomlar, to'y marosimi, atlas, do'ppi, raqs.
  Bularga vatanga muhabbat ma'nosi qo'shilmagan bo'lsa — vatan tuyg'usi emas.
- Videoning shunchaki O'zbekistonda suratga olingani (ko'cha, bozor, kafe, uy)
- Turizm yoki mahsulot reklamasi: vatan ramzlaridan tijorat uchun foydalanish.
  Bunday holda "QISMAN" qo'y va sababini yoz.
- Qishloq nostalgiyasi, bolalik xotiralari — vatan bilan bog'lanmasa
- Diniy mazmun — vatan bilan bog'lanmasa
- Boshqa davlat ramzlari (Turkiya, Rossiya, Qozog'iston va h.k.)
- Kinoya, satira, tanqid — "vatan" so'zi ishlatilsa ham. Buni alohida belgila.

QOIDALAR (qat'iy):
1. FAQAT quyida berilgan dalillarga tayan. Ko'rmagan narsangni ko'rdim dema.
2. Kadrlar butun videoni emas, faqat sanoqli nuqtasini ko'rsatadi. Oradagi
   voqealarni o'ylab topma.
3. Ovoz transkripsiyasi avtomatik (Whisper) va o'zbek tili uchun XATOLARGA MOYIL.
   So'zma-so'z ishonma; ma'no darajasida foydalan.
4. Qaysidir signal (ovoz yoki caption) yo'q bo'lsa — "ishonch" ballini pasaytir
   va buni "ogohlantirish" maydonida yoz.
5. Har bir dalilda manbani ko'rsat: "kadr", "ovoz" yoki "matn".
6. Odamlarni ism-sharifi bilan atama, kimligini aniqlashga urinma.
   Faqat ko'ringan narsani tasvirla ("harbiy kiyimdagi odam", "haykal").

BALL SHKALASI:
  0-24   -> qaror "YOQ"      (vatan tuyg'usi yo'q)
  25-59  -> qaror "QISMAN"   (ishorasi bor, lekin asosiy mavzu emas)
  60-100 -> qaror "BOR"      (aniq vatanparvarlik mazmuni)

Javobni faqat JSON obyekt sifatida ber, boshqa matnsiz. Barcha matn maydonlari
o'zbek tilida (lotin alifbosida) bo'lsin.
"""

JAVOB_SXEMASI = {
    "type": "object",
    "properties": {
        "qaror": {
            "type": "string",
            "enum": ["BOR", "QISMAN", "YOQ"],
            "description": "Yakuniy qaror",
        },
        "ball": {
            "type": "integer",
            "description": "0-100 oralig'ida vatan tuyg'usi kuchi",
        },
        "ishonch": {
            "type": "integer",
            "description": "0-100, qaroringga qanchalik ishonasan",
        },
        "mavzu": {
            "type": "string",
            "description": "Video nima haqida — 1-2 jumla",
        },
        "xulosa": {
            "type": "string",
            "description": "Nega shunday qaror qilinganini 2-3 jumlada tushuntir",
        },
        "dalillar": {
            "type": "array",
            "description": "Qarorni QO'LLAB-QUVVATLAYDIGAN dalillar",
            "items": {
                "type": "object",
                "properties": {
                    "manba": {"type": "string", "enum": ["kadr", "ovoz", "matn"]},
                    "izoh": {"type": "string"},
                    "vaqt": {
                        "type": "string",
                        "description": "Masalan 0:12. Noma'lum bo'lsa bo'sh satr.",
                    },
                },
                "required": ["manba", "izoh", "vaqt"],
                "additionalProperties": False,
            },
        },
        "qarshi_dalillar": {
            "type": "array",
            "description": "Qarorga QARSHI yoki uni zaiflashtiruvchi kuzatuvlar",
            "items": {
                "type": "object",
                "properties": {
                    "manba": {"type": "string", "enum": ["kadr", "ovoz", "matn"]},
                    "izoh": {"type": "string"},
                },
                "required": ["manba", "izoh"],
                "additionalProperties": False,
            },
        },
        "ramzlar": {
            "type": "array",
            "description": "Aniqlangan aniq ramzlar ro'yxati (bayroq, haykal, madhiya...)",
            "items": {"type": "string"},
        },
        "kinoya": {
            "type": "boolean",
            "description": "Mazmun kinoya/satira/tanqid ohangidami",
        },
        "ogohlantirish": {
            "type": "string",
            "description": "Tahlil cheklovlari. Yo'q bo'lsa bo'sh satr.",
        },
    },
    "required": [
        "qaror", "ball", "ishonch", "mavzu", "xulosa",
        "dalillar", "qarshi_dalillar", "ramzlar", "kinoya", "ogohlantirish",
    ],
    "additionalProperties": False,
}


# 400 xatosi shu so'zlardan birini o'z ichiga olsa — strukturali chiqish
# qo'llab-quvvatlanmagan deb hisoblanadi va keyingi variantga o'tiladi.
_SXEMA_BELGILARI = (
    "output_config", "output_format", "json_schema", "schema", "format",
)


def _json_ajrat(matn: str) -> dict:
    """Javobdan JSON obyektni ajratib oladi (kod bloki bo'lsa ham)."""
    matn = (matn or "").strip()
    if matn.startswith("```"):
        matn = re.sub(r"^```[a-zA-Z]*\s*", "", matn)
        matn = re.sub(r"\s*```$", "", matn)
    try:
        return json.loads(matn)
    except json.JSONDecodeError:
        pass
    bosh, oxir = matn.find("{"), matn.rfind("}")
    if bosh != -1 and oxir > bosh:
        return json.loads(matn[bosh:oxir + 1])
    raise TahlilXatosi("Claude javobini JSON sifatida o'qib bo'lmadi.")


def _claude_soro(tizim: str, mazmun, sxema: dict, max_tokens: int = 4000) -> dict:
    """
    Claude ga bitta so'rov yuboradi va JSON javobni qaytaradi.
    Strukturali chiqish qo'llab-quvvatlanmasa — bosqichma-bosqich chekinadi.
    """
    try:
        import anthropic
    except ImportError:
        raise TahlilXatosi(
            "anthropic kutubxonasi o'rnatilmagan:  pip install -U anthropic"
        )

    try:
        mijoz = anthropic.Anthropic()
    except Exception as e:
        raise TahlilXatosi(
            "Anthropic mijozini yaratib bo'lmadi. ANTHROPIC_API_KEY o'rnatilganmi?\n"
            f"Xabar: {e}"
        )

    asos = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": tizim,
        "messages": [{"role": "user", "content": mazmun}],
    }
    variantlar = [
        ("sxema+effort", {**asos, "output_config": {
            "effort": "medium",
            "format": {"type": "json_schema", "schema": sxema},
        }}),
        ("sxema", {**asos, "output_config": {
            "format": {"type": "json_schema", "schema": sxema},
        }}),
        ("oddiy", asos),
    ]

    oxirgi_xato: Optional[Exception] = None
    javob = None
    rejim = ""
    for nom, variant in variantlar:
        try:
            javob = mijoz.messages.create(**variant)
            rejim = nom
            break
        except anthropic.BadRequestError as e:
            # 400 ning HAMMASI sxema xatosi emas. Faqat strukturali chiqishga
            # tegishli bo'lsa chekinamiz; qolganini darhol tushuntiramiz.
            xabar = str(e).lower()
            if "credit balance" in xabar or "billing" in xabar:
                raise TahlilXatosi(
                    "Anthropic hisobingizda mablag' qolmagan.\n"
                    "console.anthropic.com -> Plans & Billing bo'limidan "
                    "kredit qo'shing."
                )
            if not any(b in xabar for b in _SXEMA_BELGILARI):
                raise TahlilXatosi(f"Anthropic so'rovni rad etdi: {e}")
            oxirgi_xato = e
            continue
        except anthropic.AuthenticationError:
            raise TahlilXatosi("ANTHROPIC_API_KEY noto'g'ri yoki muddati o'tgan.")
        except anthropic.RateLimitError:
            raise TahlilXatosi("Anthropic API limiti tugadi, birozdan keyin urining.")
        except anthropic.APIConnectionError:
            raise TahlilXatosi("Anthropic API ga ulanib bo'lmadi — internetni tekshiring.")
        except anthropic.APIStatusError as e:
            raise TahlilXatosi(f"Anthropic API xatosi ({e.status_code}): {e.message}")
    if javob is None:
        raise TahlilXatosi(f"Anthropic so'rovi rad etildi: {oxirgi_xato}")

    if getattr(javob, "stop_reason", "") == "refusal":
        raise TahlilXatosi("Model bu kontentni tahlil qilishdan bosh tortdi.")

    matn = "".join(b.text for b in javob.content if b.type == "text")
    natija = _json_ajrat(matn)
    natija["_narx"] = _narx_hisobla(javob.usage)
    natija["_model"] = getattr(javob, "model", MODEL)
    natija["_rejim"] = rejim
    return natija


QARORLAR = ("BOR", "QISMAN", "YOQ")


def natijani_normallashtir(natija: dict) -> dict:
    """
    Model javobini ishonchli shaklga keltiradi.
    Strukturali chiqish qo'llab-quvvatlanmasa ("oddiy" chekinish varianti)
    model istalgan qiymat qaytarishi mumkin: kichik harfli qaror, satr ko'rinishidagi
    ball, 100 dan katta son. Bularni FRONTEND emas, shu yer to'g'rilaydi.
    """
    qrr = str(natija.get("qaror", "")).strip().upper()
    qrr = qrr.replace("'", "").replace("\u2018", "").replace("\u2019", "")
    natija["qaror"] = qrr if qrr in QARORLAR else "YOQ"

    for maydon in ("ball", "ishonch"):
        try:
            qiymat = int(float(natija.get(maydon, 0) or 0))
        except (TypeError, ValueError):
            qiymat = 0
        natija[maydon] = max(0, min(100, qiymat))

    for maydon in ("mavzu", "xulosa", "ogohlantirish"):
        natija[maydon] = str(natija.get(maydon, "") or "")
    natija["kinoya"] = bool(natija.get("kinoya"))
    natija["ramzlar"] = [str(r) for r in (natija.get("ramzlar") or [])][:20]

    for royxat in ("dalillar", "qarshi_dalillar"):
        toza = []
        for d in (natija.get(royxat) or []):
            if not isinstance(d, dict):
                continue
            manba = str(d.get("manba", "")).strip().lower()
            toza.append({
                "manba": manba if manba in ("kadr", "ovoz", "matn") else "matn",
                "izoh": str(d.get("izoh", "") or ""),
                "vaqt": str(d.get("vaqt", "") or ""),
            })
        natija[royxat] = toza
    return natija


# ═══════════════════════════════════════════════════════════════════════════
#  QOIDAVIY TAHLIL — APIsiz, mahalliy (kalit so'z lug'ati + kadr rang tahlili)
#
#  Hech qanday tashqi xizmatga ulanmaydi. Ovoz matni (Whisper) va caption
#  ustidan vatanparvarlik lug'ati bo'yicha ball qo'yiladi; kadrlarda
#  O'zbekiston bayrog'i ranglari (ko'k–oq–yashil) qidiriladi.
#
#  MUHIM: bu ma'noni ANGLAMAYDI — faqat kalit so'z va rang belgilarini
#  sanaydi. Shu sababli natijaning "ogohlantirish" maydonida buni ochiq yozamiz.
# ═══════════════════════════════════════════════════════════════════════════

# Kiril -> lotin (o'zbek). Whisper "uz" uchun goh lotin, goh kiril chiqaradi.
_KIRIL_LOTIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "j", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "x", "ц": "s", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "",
    "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya", "ў": "o", "қ": "q",
    "ғ": "g", "ҳ": "h",
}


def _normalize(matn: str) -> str:
    """Kichik harf, kiril->lotin, apostrof variantlarini olib tashlash."""
    matn = (matn or "").lower()
    matn = "".join(_KIRIL_LOTIN.get(ch, ch) for ch in matn)
    # o' g' ning barcha apostrof ko'rinishlari -> oddiy o, g
    for ap in ("ʻ", "ʼ", "'", "`", "‘", "’", "´", "‛"):
        matn = matn.replace(ap, "")
    matn = re.sub(r"\s+", " ", matn)
    return matn


# (toifa_kaliti, vazn, [kalit so'zlar — normallashtirilgan lotin, apostrofsiz])
_TOIFALAR: list[tuple[str, int, tuple[str, ...]]] = [
    ("davlat_ramzlari", 34, (
        "bayroq", "bayrog", "davlat bayrog", "gerb", "madhiya", "gimn",
        "davlat ramz", "milliy ramz", "davlat gerbi",
    )),
    ("mustaqillik", 28, (
        "mustaqillik", "istiqlol", "ozodlik", "erkinlik", "milliy istiqlol",
        "mustaqil ozbekiston",
    )),
    ("harbiy", 28, (
        "askar", "harbiy", "chegarachi", "qoshin", "jangchi", "mudofaa",
        "harbiy xizmat", "vatan himoya", "vatanni himoya", "serjant",
        "ofitser", "harbiy qasam", "harbiy kiyim", "qurolli kuch",
    )),
    ("xotira", 22, (
        "shahid", "faxriy", "urush faxriy", "xotira va qadrlash",
        "jangchilar xotira", "ikkinchi jahon urush", "veteran",
    )),
    ("vatan_mehr", 26, (
        "vatan", "ona yurt", "ona vatan", "yurtim", "yurtimiz", "elim",
        "xalqim", "ozbekiston", "uzbekiston", "uzbekistan", "vatanparvar",
        "vatanparvarlik", "vatan tuygu", "yurt sevgi", "ona diyor", "diyorim",
    )),
    ("qahramon", 20, (
        "amir temur", "alisher navoiy", "bobur", "ulugbek", "jaloliddin",
        "manguberdi", "qahramon", "sohibqiron", "mardlik", "jasorat",
        "botirlik",
    )),
    ("bayram", 18, (
        "mustaqillik kuni", "konstitutsiya kuni", "xotira va qadrlash kuni",
        "milliy bayram", "bayrogimiz kotarildi",
    )),
    ("birlik_gurur", 18, (
        "milliy gurur", "gururlanaman", "iftixor", "hamjihatlik",
        "xalq birligi", "birdamlik", "milliy ong", "yagona xalq",
    )),
    ("sport_galaba", 12, (
        "terma jamoa", "jamoamiz", "ozbekiston terma", "madhiya yangradi",
        "oltin medal", "jahon chempion",
    )),
]

_TOIFA_NOM = {
    "davlat_ramzlari": "davlat ramzlari",
    "mustaqillik": "mustaqillik va istiqlol",
    "harbiy": "harbiy xizmat",
    "xotira": "xotira va faxriylar",
    "vatan_mehr": "vatanga muhabbat",
    "qahramon": "milliy qahramonlar",
    "bayram": "milliy bayramlar",
    "birlik_gurur": "milliy g'urur va birlik",
    "sport_galaba": "sport g'alabalari",
}

# Tijorat/reklama — ramzlar sotuv uchun ishlatilishi mumkin (QISMAN tomon).
_TIJORAT = (
    "reklama", "chegirma", "aksiya", "sotib oling", "buyurtma", "narxi",
    "sotuvda", "dostavka", "yetkazib berish", "skidka", "aksiyada", "obuna bol",
)
# Kinoya/satira belgilari.
_KINOYA = ("kinoya", "hazil", "prikol", "masxara", "ustidan kul", "satira",
           "parodiya", "trolling")
# Boshqa davlat ramzlari — vatan tuyg'usi EMAS.
_OZGA_DAVLAT = ("rossiya bayrog", "turkiya bayrog", "qozogiston bayrog",
                "amerika bayrog", "putin", "erdogan")


def _matn_tahlil(matnlar: list[tuple[str, str]]) -> dict:
    """
    matnlar: [(manba, matn)] — masalan [("ovoz", "..."), ("matn", caption)].
    Qaytaradi: toifalar, dalillar, tijorat/kinoya/o'zga-davlat bayroqlari.
    """
    faol: dict[str, dict] = {}      # toifa -> {vazn, manba, sozlar:set}
    tijorat = False
    kinoya = False
    ozga = False

    for manba, xom in matnlar:
        norm = _normalize(xom)
        if not norm:
            continue
        for toifa, vazn, sozlar in _TOIFALAR:
            for soz in sozlar:
                if soz in norm:
                    yozuv = faol.setdefault(
                        toifa, {"vazn": vazn, "manba": manba,
                                "sozlar": set(), "soni": 0})
                    yozuv["sozlar"].add(soz)
                    # Takror soni (bitta so'z hukmronlik qilmasligi uchun cheklab)
                    yozuv["soni"] += min(norm.count(soz), 6)
                    # Ovozdagi dalil captiondan ustun (ishonchliroq manba)
                    if manba == "ovoz":
                        yozuv["manba"] = "ovoz"
        if any(t in norm for t in _TIJORAT):
            tijorat = True
        if any(t in norm for t in _KINOYA):
            kinoya = True
        if any(t in norm for t in _OZGA_DAVLAT):
            ozga = True

    return {"faol": faol, "tijorat": tijorat, "kinoya": kinoya, "ozga": ozga}


def _rang_sinf(r: int, g: int, b: int) -> str:
    """Pikselni O'zbekiston bayrog'i ranglariga qarab tasniflaydi."""
    mx, mn = max(r, g, b), min(r, g, b)
    # Oq — yorqin va rangsiz
    if mn > 175 and (mx - mn) < 40:
        return "oq"
    # Juda qorong'i — hisobga olmaymiz
    if mx < 55:
        return "boshqa"
    # Ko'k (O'zbekiston bayrog'i #0099b5 — moviy/turkuaz): ko'k va yashil baland,
    # qizil past
    if b > 90 and b >= g - 20 and b > r + 25 and g > r:
        return "kok"
    # Yashil #1eb53a: yashil ustun
    if g > 90 and g > r + 30 and g > b + 25:
        return "yashil"
    # Qizil #ce1126
    if r > 110 and r > g + 55 and r > b + 55:
        return "qizil"
    return "boshqa"


def _bayroq_tahlil(fayl: str) -> dict:
    """
    Kadrda O'zbekiston bayrog'ini (ko'k-oq-yashil gorizontal tasma) qidiradi.
    Osmon(ko'k)+o't(yashil) manzarasidan farqlash uchun ORADAGI OQ TASMA shart.
    PIL bo'lmasa — bo'sh natija (dastur to'xtamaydi).
    Qaytaradi: {"bayroq": bool, "ishonch": int, "kok": float, "yashil": float}
    """
    try:
        from PIL import Image
    except Exception:
        return {"bayroq": False, "ishonch": 0, "kok": 0.0, "yashil": 0.0, "yoq": True}

    try:
        im = Image.open(fayl).convert("RGB")
    except Exception:
        return {"bayroq": False, "ishonch": 0, "kok": 0.0, "yashil": 0.0}

    # Kichraytiramiz — tez va shovqin kamroq
    en = 72
    boy = max(1, round(im.height * en / max(1, im.width)))
    im = im.resize((en, boy))
    px = im.load()

    jami = en * boy
    kok_j = yashil_j = 0
    # Har qatorning ustun sinfini va sinf bo'yicha qator indekslarini yig'amiz
    kok_qatorlar: list[int] = []
    oq_qatorlar: list[int] = []
    yashil_qatorlar: list[int] = []
    for y in range(boy):
        sanoq = {"kok": 0, "oq": 0, "yashil": 0, "qizil": 0, "boshqa": 0}
        for x in range(en):
            r, g, b = px[x, y]
            s = _rang_sinf(r, g, b)
            sanoq[s] += 1
            if s == "kok":
                kok_j += 1
            elif s == "yashil":
                yashil_j += 1
        ustun = max(sanoq, key=sanoq.get)
        # Qator "toza" bo'lsagina (ustun rang >55%) hisobga olamiz
        if sanoq[ustun] <= en * 0.55:
            continue
        if ustun == "kok":
            kok_qatorlar.append(y)
        elif ustun == "oq":
            oq_qatorlar.append(y)
        elif ustun == "yashil":
            yashil_qatorlar.append(y)

    # Bayroq = uch tasma ham sezilarli (>=8% balandlik) VA tepadan pastga
    # tartibda: ko'k o'rtasi < oq o'rtasi < yashil o'rtasi. Oradagi ingichka
    # qizil tasmalar bardosh qilinadi (o'rtacha o'rin bo'yicha solishtirish).
    chek = max(2, round(boy * 0.08))
    bayroq = False
    kok_tasma = len(kok_qatorlar)
    oq_tasma = len(oq_qatorlar)
    yashil_tasma = len(yashil_qatorlar)
    if kok_tasma >= chek and oq_tasma >= chek and yashil_tasma >= chek:
        kok_o = sum(kok_qatorlar) / kok_tasma
        oq_o = sum(oq_qatorlar) / oq_tasma
        yashil_o = sum(yashil_qatorlar) / yashil_tasma
        if kok_o < oq_o < yashil_o:
            bayroq = True

    kok_ulush = kok_j / jami
    yashil_ulush = yashil_j / jami
    ishonch = 0
    if bayroq:
        ishonch = 55 + min(20, int((kok_tasma + yashil_tasma) / boy * 40))
    return {
        "bayroq": bayroq,
        "ishonch": ishonch,
        "kok": round(kok_ulush, 3),
        "yashil": round(yashil_ulush, 3),
    }


def _baholash_qoida(kadrlar: list[dict], nutq: dict, video: VideoMalumot) -> dict:
    """APIsiz baho — kalit so'z lug'ati + kadr rang tahlili."""
    ovoz_matn = nutq.get("matn", "") or ""
    caption = video.caption or ""
    sarlavha = getattr(video, "sarlavha", "") or ""
    manbalar = []
    if ovoz_matn:
        manbalar.append(("ovoz", ovoz_matn))
    # Sarlavha + caption bitta "matn" manbasi (sarlavha ko'pincha eng toza signal)
    matn_qism = " . ".join(x for x in (sarlavha, caption) if x)
    if matn_qism:
        manbalar.append(("matn", matn_qism))

    mt = _matn_tahlil(manbalar)
    faol = mt["faol"]

    # --- Kadrlarda bayroq qidirish ---
    bayroq_kadrlar = []       # [(yorliq, ishonch)]
    kok_max = yashil_max = 0.0
    for k in kadrlar:
        bt = _bayroq_tahlil(k["fayl"])
        kok_max = max(kok_max, bt.get("kok", 0.0))
        yashil_max = max(yashil_max, bt.get("yashil", 0.0))
        if bt.get("bayroq"):
            bayroq_kadrlar.append((k["yorliq"], bt["ishonch"]))

    # Bayroq topilsa — davlat ramzlari toifasini kuchaytiramiz
    if bayroq_kadrlar:
        yozuv = faol.setdefault(
            "davlat_ramzlari", {"vazn": 34, "manba": "kadr", "sozlar": set()})
        yozuv["sozlar"].add("bayroq (kadr)")

    # --- Ball ---
    ball = 0
    for toifa, y in faol.items():
        ball += y["vazn"]
        # bir toifada bir nechta XIL so'z bo'lsa — kichik bonus
        ball += min(6, (len(y["sozlar"]) - 1) * 3)
        # so'z TAKRORLANSA (masalan qo'shiqda "vatan" ko'p marta) — kuchayadi
        ball += min(12, max(0, y.get("soni", 1) - 1) * 2)
    # Ikki va undan ortiq kuchli toifa birga kelsa — sinergiya
    kuchli = [t for t in faol if faol[t]["vazn"] >= 26]
    if len(kuchli) >= 2:
        ball += 8
    # "O'zbekiston" + vatan so'zi birga -> aniq vatanparvarlik izhori
    vm = faol.get("vatan_mehr", {}).get("sozlar", set())
    davlat_nomi = {"ozbekiston", "uzbekiston", "uzbekistan"} & vm
    vatan_soz = {"vatan", "ona yurt", "ona vatan", "yurtim", "yurtimiz",
                 "diyorim", "ona diyor"} & vm
    if davlat_nomi and vatan_soz:
        ball += 26
    ball = max(0, min(100, ball))

    # Tijorat ustun bo'lsa — BOR ga chiqarmaymiz
    if mt["tijorat"] and ball >= 60:
        ball = 59

    qaror = "BOR" if ball >= 60 else ("QISMAN" if ball >= 25 else "YOQ")

    # --- Ishonch (heuristika — hech qachon 80 dan oshmaydi) ---
    ishonch = 40
    if ovoz_matn:
        ishonch += 15
    if caption:
        ishonch += 8
    if len(faol) >= 2:
        ishonch += 12
    if bayroq_kadrlar:
        ishonch += 5
    if not ovoz_matn and not caption and not bayroq_kadrlar:
        # Signal umuman yo'q — "YO'Q" ga ishonch o'rtacha
        ishonch = 52
    ishonch = max(0, min(78, ishonch))

    # --- Dalillar ---
    dalillar = []
    for toifa, y in sorted(faol.items(), key=lambda kv: -kv[1]["vazn"]):
        sozlar = ", ".join(sorted(y["sozlar"])[:4])
        dalillar.append({
            "manba": y["manba"],
            "izoh": f"{_TOIFA_NOM.get(toifa, toifa).capitalize()}: "
                    f"belgilar — {sozlar}",
            "vaqt": "",
        })
    for yorliq, ishonch_k in bayroq_kadrlar[:3]:
        dalillar.append({
            "manba": "kadr",
            "izoh": "Kadrda O'zbekiston bayrog'i ranglari (ko'k–oq–yashil) "
                    "aniqlandi — ehtimoliy, rang tahlili bo'yicha",
            "vaqt": yorliq,
        })

    # --- Qarshi dalillar ---
    qarshi = []
    if not ovoz_matn:
        qarshi.append({"manba": "ovoz",
                       "izoh": nutq.get("xato")
                       or "Ovoz/nutq topilmadi — matnli signal cheklangan"})
    if not matn_qism:
        qarshi.append({"manba": "matn", "izoh": "Sarlavha/caption bo'sh"})
    if mt["tijorat"]:
        qarshi.append({"manba": "matn",
                       "izoh": "Tijorat/reklama so'zlari uchradi — ramzlar "
                               "sotuv uchun ishlatilgan bo'lishi mumkin"})
    if mt["ozga"]:
        qarshi.append({"manba": "matn",
                       "izoh": "Boshqa davlat ramzlari haqida gap bor — "
                               "O'zbekiston vatan tuyg'usi bo'lmasligi mumkin"})
    if kok_max > 0.12 and yashil_max > 0.12 and not bayroq_kadrlar:
        qarshi.append({"manba": "kadr",
                       "izoh": "Kadrda ko'k va yashil ko'p, lekin bayroq "
                               "tuzilishi (oq tasma) topilmadi — tabiat manzarasi "
                               "bo'lishi mumkin"})

    # --- Ramzlar ---
    ramzlar = []
    if "davlat_ramzlari" in faol:
        ramzlar.append("davlat ramzi")
    if bayroq_kadrlar:
        ramzlar.append("bayroq (ehtimoliy)")
    if "harbiy" in faol:
        ramzlar.append("harbiy")
    if "madhiya" in " ".join(
            s for y in faol.values() for s in y["sozlar"]):
        ramzlar.append("madhiya")

    # --- Mavzu (modelsiz — qisqa xulosa) ---
    if sarlavha:
        mavzu = sarlavha.strip().split("\n")[0][:160]
    elif caption:
        mavzu = caption.strip().split("\n")[0][:160]
    elif ovoz_matn:
        mavzu = "Ovoz matni asosida: " + " ".join(ovoz_matn.split()[:14])
    else:
        mavzu = "Matnsiz video — asosan vizual kontent (nutq/caption topilmadi)."

    # --- Xulosa ---
    if faol:
        nomlar = ", ".join(_TOIFA_NOM.get(t, t) for t in
                           sorted(faol, key=lambda t: -faol[t]["vazn"])[:3])
        xulosa = (f"Vatanparvarlikka oid belgilar topildi: {nomlar}. "
                  f"Kalit so'z va rang tahliliga ko'ra ball {ball}/100, "
                  f"shu sabab qaror — {qaror}.")
    else:
        xulosa = ("Vatanparvarlikka oid kalit so'z yoki bayroq ramzi "
                  "topilmadi. Shu sabab qaror — YO'Q.")

    ogoh = ("Bu — qoidaviy (modelsiz) tahlil: kalit so'zlar va kadr ranglari "
            "bo'yicha. Videoning ma'nosi, ohangi va vizual mazmuni chuqur "
            "anglanmaydi — nozik holatlar e'tibordan chetda qolishi mumkin.")

    natija = {
        "qaror": qaror,
        "ball": ball,
        "ishonch": ishonch,
        "mavzu": mavzu,
        "xulosa": xulosa,
        "dalillar": dalillar,
        "qarshi_dalillar": qarshi,
        "ramzlar": ramzlar,
        "kinoya": bool(mt["kinoya"]),
        "ogohlantirish": ogoh,
        # Portret uchun — qaysi toifalar faol bo'lgani (faqat qoida backend'da)
        "toifalar": sorted(faol.keys()),
    }
    return natijani_normallashtir(natija)


def baholash(kadrlar: list[dict], nutq: dict, video: VideoMalumot) -> dict:
    """Backend tanloviga qarab qoidaviy yoki Claude tahlilini bajaradi."""
    if BACKEND == "claude":
        return _baholash_claude(kadrlar, nutq, video)
    return _baholash_qoida(kadrlar, nutq, video)


def _baholash_claude(kadrlar: list[dict], nutq: dict, video: VideoMalumot) -> dict:
    mazmun: list[dict] = []

    # Rasmlar matndan OLDIN — hujjatdagi tavsiya shunday.
    for i, k in enumerate(kadrlar, start=1):
        with open(k["fayl"], "rb") as f:
            b64 = base64.standard_b64encode(f.read()).decode("ascii")
        mazmun.append({"type": "text", "text": f"{i}-kadr ({k['yorliq']}):"})
        mazmun.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": b64,
            },
        })

    # Matnli kontekst
    qismlar = [
        "=== VIDEO HAQIDA ===",
        f"Muallif: {video.muallif or '(nomaʻlum)'}",
        (f"Davomiylik: {vaqt_yorlig(video.davomiylik)}" if video.davomiylik > 0
         else "Davomiylik: ANIQLANMADI — kadrlar videoning boshidan olingan "
              "bo'lishi mumkin, ular butun videoni ifodalamasligi mumkin"),
        "",
        "=== CAPTION / TAVSIF ===",
        video.caption if video.caption else "(caption yo'q yoki bo'sh)",
        "",
        "=== OVOZ TRANSKRIPSIYASI ===",
    ]
    if nutq.get("matn"):
        qismlar.append(f"(avtomatik, aniqlangan til: {nutq.get('til') or 'nomaʼlum'})")
        for s in nutq["segmentlar"]:
            qismlar.append(f"[{s['vaqt']}] {s['matn']}")
    else:
        sabab = nutq.get("xato") or "videoda tushunarli nutq topilmadi"
        qismlar.append(f"(TRANSKRIPSIYA YO'Q — {sabab})")

    qismlar += [
        "",
        f"=== VAZIFA ===",
        f"Yuqoridagi {len(kadrlar)} ta kadr va matnli ma'lumot asosida videoda "
        "vatan tuyg'usi bor-yo'qligini aniqla va JSON javob ber.",
    ]
    mazmun.append({"type": "text", "text": "\n".join(qismlar)})
    return natijani_normallashtir(
        _claude_soro(TIZIM_KO_RSATMASI, mazmun, JAVOB_SXEMASI, max_tokens=4000)
    )


def _narx_hisobla(usage) -> dict:
    """
    Claude Opus 5: $5 / 1M kirish, $25 / 1M chiqish.
    Keshdan o'qilgan token ~0.1x, keshga yozilgan ~1.25x narxda — ularni
    kirish bilan bir xil hisoblash summani oshirib yuboradi.
    """
    kirish = getattr(usage, "input_tokens", 0) or 0
    kesh_oqish = getattr(usage, "cache_read_input_tokens", 0) or 0
    kesh_yozish = getattr(usage, "cache_creation_input_tokens", 0) or 0
    chiqish = getattr(usage, "output_tokens", 0) or 0
    dollar = (
        kirish * 5e-6
        + kesh_oqish * 0.5e-6      # 0.1 x
        + kesh_yozish * 6.25e-6    # 1.25 x
        + chiqish * 25e-6
    )
    return {
        "kirish_token": kirish,
        "kesh_token": kesh_oqish + kesh_yozish,
        "chiqish_token": chiqish,
        "dollar": round(dollar, 4),
    }


# ---------------------------------------------------------------------------
# To'liq zanjir
# ---------------------------------------------------------------------------

QADAMLAR = [
    "Link tekshirilmoqda",
    "Video yuklanmoqda",
    "Kadrlar ajratilmoqda",
    "Ovoz ajratilmoqda",
    "Nutq matnga o'girilmoqda",
    "Claude tahlil qilmoqda",
]


def tahlil_qil(link: str, holat: Optional[Callable[[int, str], None]] = None) -> dict:
    """
    Butun zanjirni bajaradi va natija lug'atini qaytaradi.
    `holat(indeks, matn)` — jarayonni ko'rsatish uchun (ixtiyoriy).
    """
    def belgila(i: int) -> None:
        if holat:
            holat(i, QADAMLAR[i])

    belgila(0)
    toza_link, kod = link_tekshir(link)

    papka = tempfile.mkdtemp(prefix="vatan_")
    try:
        belgila(1)
        video = video_yukla(toza_link, papka)
        davomiylik = davomiylik_aniqla(video.fayl, video.davomiylik)
        video.davomiylik = davomiylik

        davomiylik_nomalum = davomiylik <= 0
        belgila(2)
        kadrlar = kadr_ajrat(video.fayl, papka, davomiylik)

        belgila(3)
        wav = ovoz_ajrat(video.fayl, papka)

        belgila(4)
        if wav:
            nutq = transkripsiya(wav)
        else:
            nutq = {"matn": "", "til": "", "segmentlar": [],
                    "xato": "videoda ovoz yo'q"}

        belgila(5)
        baho = baholash(kadrlar, nutq, video)

        # Halollik bloki: qaysi signallar HAQIQATDA ishlatilgani.
        # Buni model emas, kod to'ldiradi.
        baho["manbalar"] = {
            "kadrlar_soni": len(kadrlar),
            "kadr_vaqtlari": [k["yorliq"] for k in kadrlar],
            "ovoz_bormi": bool(nutq.get("matn")),
            "ovoz_sababi": nutq.get("xato", ""),
            "ovoz_tili": nutq.get("til", ""),
            "caption_bormi": bool(video.caption),
            "davomiylik_nomalum": davomiylik_nomalum,
        }
        baho["video"] = {
            "link": toza_link,
            "kod": kod,
            "muallif": video.muallif,
            "davomiylik": vaqt_yorlig(davomiylik),
            "caption": video.caption,
        }
        baho["transkript"] = nutq.get("segmentlar", [])
        return baho
    finally:
        shutil.rmtree(papka, ignore_errors=True)


# ---------------------------------------------------------------------------
# Portret — 10 ta video natijasidan foydalanuvchi profilini yasash
# ---------------------------------------------------------------------------

PORTRET_KO_RSATMASI = """\
Senga bitta odam O'ZI KO'RGAN Instagram videolarining tahlil natijalari
beriladi. Vazifang — shu namuna asosida u qanday kontent iste'mol qilishini
tasvirlab berish. Bu DAVLAT AXBOROT TIZIMI uchun yoziladi: uslub rasmiy,
xolis va aniq bo'lsin.

MUHIM CHEGARALAR:
1. Bu shaxsiyat testi EMAS. Sen odamning xarakteri, siyosiy qarashi yoki
   vatanparvarligi haqida hukm chiqarmaysan. Sen faqat SHU VIDEOLAR nima
   ko'rsatayotganini tasvirlaysan.
2. "Sen vatanparvarsan" yoki "sen vatanni sevmaysan" deb YOZMA. Buning
   o'rniga: "ko'rgan videolaringning 4 tasida davlat ramzlari uchradi".
3. Namuna kichik — 10 ta video. Buni ochiq tan ol.
4. Axloqiy baho berma, nasihat qilma, "ko'proq shunday ko'r" dema.
5. Faqat berilgan tahlillarga tayan. Videolarni o'zing ko'rmagansan.
6. Ohang: qiziqarli, kuzatuvchan, hurmatli. Do'stona, lekin xushomadsiz.

Yozish uslubi: o'zbek tilida (lotin), RASMIY uslubda, "siz" deb murojaat
qil. Xolis, hujjat tilida yoz. Raqamlarni ishlat: taxminiy gap emas, dalil.
Undov belgisi va shiorlardan saqlan.

Javobni faqat JSON obyekt sifatida ber.
"""

PORTRET_SXEMASI = {
    "type": "object",
    "properties": {
        "sarlavha": {
            "type": "string",
            "description": "2-4 so'zli xolis nom, rasmiy uslubda, shiorsiz",
        },
        "bir_jumla": {
            "type": "string",
            "description": "Portretning mohiyati bitta jumlada",
        },
        "tavsif": {
            "type": "string",
            "description": "3-5 jumlalik tasvir. Raqamlarga tayan.",
        },
        "asosiy_mavzular": {
            "type": "array",
            "description": "Videolarda takrorlangan 3-6 ta mavzu",
            "items": {"type": "string"},
        },
        "kuzatuvlar": {
            "type": "array",
            "description": "3-5 ta aniq kuzatuv",
            "items": {
                "type": "object",
                "properties": {
                    "sarlavha": {"type": "string", "description": "2-4 so'z"},
                    "izoh": {"type": "string", "description": "1-2 jumla"},
                },
                "required": ["sarlavha", "izoh"],
                "additionalProperties": False,
            },
        },
        "ziddiyat": {
            "type": "string",
            "description": "Namunadagi qarama-qarshilik. Yo'q bo'lsa bo'sh satr.",
        },
        "cheklov": {
            "type": "string",
            "description": "Bu xulosaning chegarasi haqida bitta halol jumla",
        },
    },
    "required": [
        "sarlavha", "bir_jumla", "tavsif",
        "asosiy_mavzular", "kuzatuvlar", "ziddiyat", "cheklov",
    ],
    "additionalProperties": False,
}


def _portret_qoida(yaroqli, sanoq, ballar, ortacha, top_ramzlar, kinoya_soni):
    """APIsiz portret — statistikadan matn shabloniga (modelsiz)."""
    jami = len(yaroqli)
    bor, qisman, yoq = sanoq["BOR"], sanoq["QISMAN"], sanoq["YOQ"]

    # Toifalar bo'yicha sanoq (qoidaviy tahlil har videoga "toifalar" qo'yadi)
    toifa_sanoq: dict[str, int] = {}
    ovoz_bor = 0
    for n in yaroqli:
        for t in n.get("toifalar", []) or []:
            toifa_sanoq[t] = toifa_sanoq.get(t, 0) + 1
        if (n.get("manbalar") or {}).get("ovoz_bormi"):
            ovoz_bor += 1
    top_toifa = sorted(toifa_sanoq.items(), key=lambda x: -x[1])

    # --- Sarlavha ---
    if bor >= max(1, round(jami * 0.5)):
        sarlavha = "Vatanparvarlik yo'nalishi"
    elif (bor + qisman) >= max(1, round(jami * 0.5)):
        sarlavha = "Aralash, qisman vatanparvar"
    else:
        sarlavha = "Vatan mavzusi kam"

    # --- Bir jumla ---
    bir_jumla = (f"Ko'rilgan {jami} ta videoning {bor} tasida vatan tuyg'usi "
                 f"aniq, {qisman} tasida qisman, {yoq} tasida topilmadi.")

    # --- Tavsif (3-5 jumla, raqamlarga tayangan) ---
    jumlalar = [bir_jumla,
                f"O'rtacha ball {ortacha}/100 (eng past {min(ballar)}, "
                f"eng yuqori {max(ballar)})."]
    if top_toifa:
        nomlar = ", ".join(_TOIFA_NOM.get(t, t) for t, _ in top_toifa[:3])
        jumlalar.append(f"Eng ko'p uchragan mavzular: {nomlar}.")
    if top_ramzlar:
        r_nom = ", ".join(f"{r} ({s})" for r, s in top_ramzlar[:4])
        jumlalar.append(f"Aniqlangan ramzlar: {r_nom}.")
    jumlalar.append(f"Ovoz signali {ovoz_bor}/{jami} videoda mavjud edi." +
                    (f" Kinoya/satira ohangi {kinoya_soni} tasida sezildi."
                     if kinoya_soni else ""))
    tavsif = " ".join(jumlalar)

    # --- Asosiy mavzular ---
    asosiy = [_TOIFA_NOM.get(t, t) for t, _ in top_toifa[:5]]
    if not asosiy:
        asosiy = ["vatanparvarlik belgilarisiz kontent"]

    # --- Kuzatuvlar ---
    kuzatuvlar = []
    if "davlat_ramzlari" in toifa_sanoq:
        kuzatuvlar.append({
            "sarlavha": "Davlat ramzlari",
            "izoh": f"{toifa_sanoq['davlat_ramzlari']} ta videoda bayroq, gerb "
                    f"yoki madhiya belgilari uchradi."})
    if "harbiy" in toifa_sanoq:
        kuzatuvlar.append({
            "sarlavha": "Harbiy mavzu",
            "izoh": f"{toifa_sanoq['harbiy']} ta videoda harbiy xizmat bilan "
                    f"bog'liq so'zlar bor edi."})
    kuzatuvlar.append({
        "sarlavha": "O'rtacha daraja",
        "izoh": f"Namunaning o'rtacha balli {ortacha}/100 — "
                + ("yuqori." if ortacha >= 60 else
                   "o'rtacha." if ortacha >= 25 else "past.")})
    kuzatuvlar.append({
        "sarlavha": "Ovoz qamrovi",
        "izoh": f"{jami} tadan {ovoz_bor} tasida tushunarli nutq aniqlandi; "
                f"qolganida tahlil asosan kadr va captionga tayandi."})
    if kinoya_soni:
        kuzatuvlar.append({
            "sarlavha": "Kinoya ohangi",
            "izoh": f"{kinoya_soni} ta videoda hazil/kinoya belgilari bor edi — "
                    f"ular alohida talqin qilinishi kerak."})

    # --- Ziddiyat ---
    if bor >= 1 and yoq >= 1:
        ziddiyat = (f"Namuna bir xil emas: {bor} ta video aniq vatanparvar, "
                    f"{yoq} tasida esa mavzu umuman yo'q.")
    else:
        ziddiyat = ""

    cheklov = (f"Bu xulosa {jami} ta video va qoidaviy (modelsiz) tahlilga "
               f"asoslanadi — kalit so'z va rang belgilariga. Nozik yoki faqat "
               f"vizual ifodalangan mazmun hisobga olinmagan bo'lishi mumkin.")

    return {
        "sarlavha": sarlavha,
        "bir_jumla": bir_jumla,
        "tavsif": tavsif,
        "asosiy_mavzular": asosiy,
        "kuzatuvlar": kuzatuvlar,
        "ziddiyat": ziddiyat,
        "cheklov": cheklov,
    }


def portret_yasa(natijalar: list[dict]) -> dict:
    """
    Muvaffaqiyatli tahlil qilingan videolar ro'yxatidan portret yasaydi.
    Statistika (foizlar, sanoq) KOD tomonidan hisoblanadi — model emas.
    """
    yaroqli = [n for n in natijalar if n and n.get("qaror")]
    if len(yaroqli) < MIN_PORTRET:
        raise TahlilXatosi(
            f"Portret uchun kamida {MIN_PORTRET} ta video muvaffaqiyatli "
            f"tahlil bo'lishi kerak. Hozir: {len(yaroqli)} ta."
        )

    # --- Statistikani o'zimiz sanaymiz, modelga ishonmaymiz ---
    sanoq = {"BOR": 0, "QISMAN": 0, "YOQ": 0}
    for n in yaroqli:
        # Keshdagi ESKI yozuvlar normallashtirilmagan bo'lishi mumkin —
        # shuning uchun bu yerda ham qo'riqchi qoladi.
        qrr = str(n.get("qaror", "")).strip().upper()
        sanoq[qrr if qrr in sanoq else "YOQ"] += 1
    ballar = [int(n.get("ball", 0) or 0) for n in yaroqli]
    ortacha = round(sum(ballar) / len(ballar))

    ramz_sanoq: dict[str, int] = {}
    for n in yaroqli:
        for r in n.get("ramzlar", []) or []:
            kalit = str(r).strip().lower()
            if kalit:
                ramz_sanoq[kalit] = ramz_sanoq.get(kalit, 0) + 1
    top_ramzlar = sorted(ramz_sanoq.items(), key=lambda x: -x[1])[:12]

    kinoya_soni = sum(1 for n in yaroqli if n.get("kinoya"))

    # --- Model uchun matn ---
    qismlar = [
        f"Jami tahlil qilingan video: {len(yaroqli)} ta",
        f"Vatan tuyg'usi BOR: {sanoq['BOR']} ta · "
        f"QISMAN: {sanoq['QISMAN']} ta · YO'Q: {sanoq['YOQ']} ta",
        f"O'rtacha ball: {ortacha}/100 (eng past {min(ballar)}, eng yuqori {max(ballar)})",
        f"Kinoya/satira ohangidagi video: {kinoya_soni} ta",
        "",
        "Uchragan ramzlar (nechta videoda):",
    ]
    qismlar += [f"  - {r} — {s} ta" for r, s in top_ramzlar] or ["  (ramz topilmadi)"]
    qismlar += ["", "=== HAR BIR VIDEO ==="]
    for i, n in enumerate(yaroqli, start=1):
        qismlar.append(
            f"\n{i}. [{n.get('qaror')} · {n.get('ball')}/100] "
            f"{n.get('mavzu', '')}"
        )
        if n.get("xulosa"):
            qismlar.append(f"   Xulosa: {n['xulosa']}")
        dal = [d.get("izoh", "") for d in (n.get("dalillar") or [])][:3]
        if dal:
            qismlar.append("   Dalillar: " + " | ".join(dal))

    qismlar += [
        "",
        "=== VAZIFA ===",
        "Shu odam qanday kontent ko'rishini tasvirlab ber. Yuqoridagi "
        "chegaralarga qat'iy amal qil.",
    ]

    if BACKEND == "claude":
        portret = _claude_soro(
            PORTRET_KO_RSATMASI,
            [{"type": "text", "text": "\n".join(qismlar)}],
            PORTRET_SXEMASI,
            max_tokens=3000,
        )
    else:
        portret = _portret_qoida(
            yaroqli, sanoq, ballar, ortacha, top_ramzlar, kinoya_soni)

    # Raqamlar modeldan emas — koddan.
    portret["statistika"] = {
        "jami": len(yaroqli),
        "bor": sanoq["BOR"],
        "qisman": sanoq["QISMAN"],
        "yoq": sanoq["YOQ"],
        "ortacha_ball": ortacha,
        "eng_past": min(ballar),
        "eng_yuqori": max(ballar),
        "kinoya_soni": kinoya_soni,
        "ramzlar": [{"nom": r, "soni": s} for r, s in top_ramzlar],
    }
    return portret


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Foydalanish:  python analiz.py <instagram-link>")
        raise SystemExit(2)
    try:
        chiqish = tahlil_qil(
            sys.argv[1],
            holat=lambda i, m: print(f"[{i + 1}/{len(QADAMLAR)}] {m}...",
                                     file=sys.stderr),
        )
    except TahlilXatosi as xato:
        print(f"XATO: {xato}", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(chiqish, ensure_ascii=False, indent=2))
