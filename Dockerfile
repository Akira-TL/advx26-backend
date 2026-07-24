FROM node:22-bookworm-slim AS renderer

WORKDIR /renderer
COPY Sound-Visualization-Kaleidoscope-effect/particle-field/package.json ./
COPY Sound-Visualization-Kaleidoscope-effect/particle-field/package-lock.json ./
RUN npm ci
COPY Sound-Visualization-Kaleidoscope-effect/particle-field/ ./
RUN npm run build

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BACKEND_WORKER_ENABLED=1 \
    BACKEND_RENDERER_PROJECT_DIR=/app/renderer

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        fonts-liberation \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libgtk-3-0 \
        libnss3 \
        libx11-xcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        nodejs \
        xdg-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY backend/run.py ./run.py
COPY --from=renderer /renderer ./renderer
COPY --from=renderer /root/.cache/puppeteer /root/.cache/puppeteer

EXPOSE 9000
VOLUME ["/app/storage"]
CMD ["python", "run.py"]
