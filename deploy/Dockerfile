# ──────────────────────────────────────────────────────────────────────────
#  VATAN TUYG'USI — API konteyneri
#
#  Ichida: Python + ffmpeg + yt-dlp + faster-whisper (model oldindan yuklangan)
#  Chiqish: gunicorn, 5057-portda
# ──────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

# ffmpeg tizim paketidan olinadi — imageio-ffmpeg dan kichikroq va tezroq.
# tini — PID 1 sifatida signallarni to'g'ri uzatadi (docker stop tez ishlaydi).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        tini \
        nodejs \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models

WORKDIR /app

# Bog'liqliklar alohida qatlamda — kod o'zgarganda qayta o'rnatilmaydi.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Whisper modelini IMAGE ICHIGA oldindan yuklaymiz.
# Busiz birinchi tahlil paytida ~500 MB yuklanadi va foydalanuvchi kutadi
# (yoki internet yo'q bo'lsa umuman ishlamaydi).
ARG WHISPER_MODEL=small
RUN python -c "from faster_whisper import WhisperModel; \
    WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')" \
    && chmod -R a+rX /models

COPY analiz.py app.py ./

# Root'dan ishlamaymiz.
RUN useradd -r -u 10001 -m -d /home/vatan vatan \
    && mkdir -p /data && chown -R vatan:vatan /data /app
USER vatan

ENV VATAN_HOST=0.0.0.0 \
    VATAN_PORT=5057 \
    VATAN_BAZA=/data/tarix.db

EXPOSE 5057

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5057/sogliq', timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]

# DIQQAT — WORKERS SONI 1 BO'LISHI SHART.
# Seanslar (SEANSLAR lug'ati) va fon oqimlari JARAYON XOTIRASIDA turadi.
# Bir nechta worker bo'lsa, so'rov boshqa workerga tushib "Seans topilmadi"
# xatosi chiqadi. Kengaytirish kerak bo'lsa — avval Redis'ga o'tish kerak.
CMD ["gunicorn", "app:app", \
     "--bind", "0.0.0.0:5057", \
     "--workers", "1", \
     "--threads", "16", \
     "--timeout", "120", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
