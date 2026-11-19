# syntax=docker/dockerfile:1

# ---- Frontend build ---------------------------------------------------------
FROM node:24-slim AS frontend-build

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- Backend runtime --------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/pyproject.toml backend/alembic.ini ./
COPY backend/app ./app
COPY backend/migrations ./migrations

# yt-dlp's YouTube extraction needs a JS runtime (Deno >= 2.3.0, auto-detected)
# plus the yt-dlp-ejs companion, which the `default` extra installs. Deno comes
# from the official multi-arch binary image, pinned. See
# https://github.com/yt-dlp/yt-dlp/wiki/EJS
COPY --from=denoland/deno:bin-2.9.8 /deno /usr/local/bin/deno

RUN pip install --no-cache-dir . && pip install --no-cache-dir "yt-dlp[default]"

# Fail the build early if a runtime piece is missing.
RUN deno --version \
    && yt-dlp --version \
    && python -c "import importlib.metadata as m; print('yt-dlp-ejs', m.version('yt-dlp-ejs'))"

COPY --from=frontend-build /build/dist ./frontend_dist

ENV DOWNLOADER_STATE_DIR=/data/state \
    DOWNLOADER_STAGING_DIR=/data/staging \
    DOWNLOADER_LIBRARY_DIR=/data/library \
    DOWNLOADER_FRONTEND_DIST_DIR=/app/frontend_dist

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/v1/health', timeout=3)"]

# `exec` makes uvicorn PID 1 so it receives SIGTERM directly. The bounded HTTP
# graceful wait stops an open SSE stream from blocking lifespan shutdown
# (queue drain/settlement); it is not a timeout for that settlement.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8080 --timeout-graceful-shutdown 5"]
