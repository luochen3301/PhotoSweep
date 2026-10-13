# syntax=docker/dockerfile:1.7
FROM --platform=$BUILDPLATFORM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS frontend-build
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run typecheck && npm run build

FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend \
    PHOTOSWEEP_DATA_DIR=/app/data \
    PHOTOSWEEP_CACHE_DIR=/app/cache \
    PHOTOSWEEP_TRASH_DIR=/trash \
    PHOTOSWEEP_ALLOWED_MEDIA_ROOTS=/media \
    PHOTOSWEEP_FRONTEND_DIST=/app/frontend/dist/client

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg gosu libimage-exiftool-perl libheif1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir --requirement requirements.lock

COPY backend ./backend
COPY alembic.ini ./
COPY docker/entrypoint.sh /usr/local/bin/photosweep-entrypoint
COPY --from=frontend-build /build/frontend/dist/client ./frontend/dist/client
RUN sed -i 's/\r$//' /usr/local/bin/photosweep-entrypoint \
    && chmod 0755 /usr/local/bin/photosweep-entrypoint \
    && groupadd --gid 1000 photosweep \
    && useradd --uid 1000 --gid 1000 --home-dir /app --no-create-home photosweep \
    && mkdir -p /app/data /app/cache /trash /media /tmp \
    && chown -R 1000:1000 /app/data /app/cache /trash /media /tmp

EXPOSE 8080
ENTRYPOINT ["photosweep-entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--proxy-headers"]
