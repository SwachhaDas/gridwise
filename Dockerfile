FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps for PuLP CBC solver (already bundled on slim, but keep curl for health checks)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for better layer caching
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy application code
COPY app/ ./app/
COPY tests/ ./tests/

# Do NOT bake secrets. Pass DEEPSEEK_API_KEY (and optional DEEPSEEK_BASE_URL,
# DEEPSEEK_MODEL, PORT) at runtime via --env-file or -e.

EXPOSE 8000

# Bind 0.0.0.0 and honour $PORT if provided.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]