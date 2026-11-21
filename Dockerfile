FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY m3 ./m3
COPY docs ./docs
COPY scripts ./scripts
COPY README.md .

RUN chmod +x scripts/start.sh \
    && mkdir -p /data/uploads /data/cache

ENV PORT=8080
ENV M3_DATA_ROOT=/data
EXPOSE 8080

CMD ["sh", "scripts/start.sh"]
