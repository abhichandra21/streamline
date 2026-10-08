# Streamline web UI and CLI in one image.
#
#   docker compose up -d                                   # web UI on :5051
#   docker compose run --rm streamline ./recommend setup   # first-time setup
#
# State lives in bind mounts (see compose.yaml), laid out exactly as in a
# plain checkout, so the same paths and commands work with or without Docker.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STREAMLINE_HOST=0.0.0.0 \
    STREAMLINE_PORT=5051

# sqlite3 is for inspecting or fixing data/streamline.db by hand.
RUN apt-get update \
    && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --uid 1000 --create-home streamline \
    && mkdir -p data recommender/cache logs \
    && chown -R streamline:streamline /app
USER streamline

EXPOSE 5051
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request, sys; urllib.request.urlopen('http://127.0.0.1:5051/healthz', timeout=4)" || exit 1

# One worker: background jobs and their status live in the web process.
CMD ["gunicorn", "--workers", "1", "--bind", "0.0.0.0:5051", "--timeout", "120", "recommender.web:app"]
