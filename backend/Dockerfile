# Slim Python image — small enough to fit Fly's free build cache, big
# enough to include the wheels we need (motor, pillow, reportlab).
FROM python:3.12-slim

# System deps:
#   build-essential + libffi/libssl headers — for any C-extension wheels
#     that aren't prebuilt for slim (bcrypt, cffi, etc.)
#   curl — handy for `fly ssh console` debugging
# We remove apt caches in the same RUN to keep the layer small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libssl-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (separate layer) so code-only changes don't
# bust the pip-install cache.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy the rest of the source. .dockerignore keeps venv/, uploads/,
# __pycache__/, .env out of the build context.
COPY . .

# Fly's default internal port. fly.toml's [http_service] block maps this.
ENV PORT=8080
EXPOSE 8080

# uvicorn directly (one worker). For multi-worker, switch to gunicorn
# with uvicorn workers — but a single Fly free-tier VM has 256 MB RAM,
# which doesn't comfortably fit multiple workers anyway.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
