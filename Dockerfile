FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOUBAO_DATA_DIR=/data \
    DOUBAO_CONTAINER_PREVIEW=1 \
    HOME=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . ./
RUN groupadd --system doubao \
    && useradd --system --gid doubao --home-dir /data doubao \
    && mkdir -p /data/config \
    && chown -R doubao:doubao /data
USER doubao
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8782/', timeout=3).close()" || exit 1
CMD ["python", "-u", "dashboard.py"]
