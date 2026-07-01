# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

ENV VENV=/opt/venv
RUN python -m venv "$VENV"
ENV PATH="$VENV/bin:$PATH"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

ENV VENV=/opt/venv
COPY --from=builder "$VENV" "$VENV"
ENV PATH="$VENV/bin:$PATH"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8001 \
    WEB_CONCURRENCY=2

WORKDIR /app
COPY . .

RUN adduser --disabled-password --gecos "" --uid 10001 appuser \
    && mkdir -p /app/uploads \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8001}/healthz" || exit 1

CMD ["sh", "-c", "gunicorn main:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers ${WEB_CONCURRENCY:-2} \
    --bind 0.0.0.0:${PORT:-8001} \
    --timeout 120 \
    --graceful-timeout 30 \
    --access-logfile - \
    --error-logfile - \
    --forwarded-allow-ips=*"]