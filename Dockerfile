FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# libargon2 lives in the system package for cffi to find; curl is for the
# container healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libargon2-1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY app/ ./app/

# Non-root runtime user; /app/data is the volume for the SQLite database.
RUN useradd --system --create-home appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

ENV DATABASE_URL=sqlite:////app/data/solar_automation.db

EXPOSE 8000

# Migrations run on every boot, then the server starts. The port honors
# PaaS-injected $PORT (Render etc.) with the local default kept at 8000.
CMD ["sh", "-c", "python -m alembic upgrade head && exec python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
