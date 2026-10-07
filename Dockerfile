# Production image with Playwright Chromium + system libraries for BidNet/OpenGov auth.
FROM python:3.12-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/app/.playwright-browsers \
    M3_DATA_ROOT=/data \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    VECLIB_MAXIMUM_THREADS=1 \
    BIDNET_LOGICAL_WORKERS=5 \
    BIDNET_BROWSER_WORKERS=2 \
    BIDNET_MAX_BROWSER_PROCESSES=2 \
    BIDNET_CHECKPOINT_EVERY=10

WORKDIR /app

# WeasyPrint + Playwright OS dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    libglib2.0-0 \
    shared-mime-info \
    fonts-liberation \
    fonts-dejavu-core \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxcb1 \
    libxkbcommon0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libatspi2.0-0 \
    libxshmfence1 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install chromium \
    && python -m playwright install-deps chromium || true

COPY . .

RUN chmod +x scripts/start_railway.sh scripts/start_railway_worker.sh \
    && mkdir -p /data /app/.playwright-browsers

EXPOSE 8080

CMD ["sh", "scripts/start_railway.sh"]
