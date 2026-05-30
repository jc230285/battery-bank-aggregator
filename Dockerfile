FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent paths live under /data so the Coolify volume captures DB, cookies,
# HTML cache, and logs together. Headless by default; binds to all interfaces.
RUN mkdir -p /data/cache /data/state /data/logs
ENV BBA_HOST=0.0.0.0 \
    BBA_DB=/data/battery_banks.sqlite3 \
    BBA_CACHE_DIR=/data/cache \
    BBA_STATE_DIR=/data/state \
    BBA_LOG_DIR=/data/logs \
    BBA_HEADLESS=1

EXPOSE 8473
CMD ["python", "app.py"]
