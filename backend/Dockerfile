# syntax=docker/dockerfile:1

# ============================================================================
# Production image for the HRMS / attendance API.
#
# Two stages:
#   builder — has the C toolchain, compiles wheels for bcrypt/cffi/etc. into
#             a self-contained virtualenv at /opt/venv.
#   runtime — copies only that venv + the app source. No compilers, no apt
#             caches, no build headers → smaller surface, smaller image.
#
# The app runs as a non-root user under gunicorn with uvicorn workers.
# ============================================================================

# -------------------------------- builder ----------------------------------
FROM python:3.12-slim AS builder

# Build-time system deps for any wheels not shipped as manylinux binaries
# (bcrypt, cffi, cryptography). Not carried into the runtime stage.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Isolated virtualenv so we can copy exactly the installed packages (and
# nothing else) into the runtime image.
ENV VENV=/opt/venv
RUN python -m venv "$VENV"
ENV PATH="$VENV/bin:$PATH"

WORKDIR /app

# Install deps first as their own layer so code-only changes don't rebuild
# the (slow) pip install.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt


# -------------------------------- runtime ----------------------------------
FROM python:3.12-slim AS runtime

# Runtime-only system deps:
#   curl — used by the HEALTHCHECK below and handy for shell debugging.
#   libffi/libssl runtime libs are already present in the slim base.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Copy the pre-built virtualenv from the builder stage.
ENV VENV=/opt/venv
COPY --from=builder "$VENV" "$VENV"
ENV PATH="$VENV/bin:$PATH"

# Python runtime hygiene: no .pyc writes, unbuffered stdout/stderr so logs
# stream straight to the platform's log collector.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    WEB_CONCURRENCY=2

WORKDIR /app

# Copy source. .dockerignore keeps venv/, uploads/, __pycache__/, .env,
# logs, tests and docs out of the build context.
COPY . .

# Run as an unprivileged user. Create it, own /app so runtime dirs (e.g. a
# local uploads/ fallback) are writable, then drop privileges.
RUN adduser --disabled-password --gecos "" --uid 10001 appuser \
    && mkdir -p /app/uploads \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# Container-level health probe. Platforms that read HEALTHCHECK (Docker,
# Compose, Swarm) will mark the container unhealthy if /healthz stops
# answering. Render/Fly use their own HTTP checks and ignore this.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8080}/healthz" || exit 1

# gunicorn manages WEB_CONCURRENCY uvicorn workers. --proxy-headers +
# forwarded-allow-ips lets uvicorn trust the load balancer's X-Forwarded-*
# headers so client IPs / scheme are correct behind a reverse proxy.
# PORT is injected by the platform at runtime (falls back to 8080 locally).
CMD ["sh", "-c", "gunicorn main:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers ${WEB_CONCURRENCY:-2} \
    --bind 0.0.0.0:${PORT:-8080} \
    --timeout 120 \
    --graceful-timeout 30 \
    --access-logfile - \
    --error-logfile - \
    --forwarded-allow-ips=*"]
