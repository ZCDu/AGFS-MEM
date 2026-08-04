# ─── memory_backend ───
# OKF v0.2 knowledge graph with chat UI
#
# Build:  docker build -t memory-backend .
# Run:    docker run -p 8000:8000 --env-file .env memory-backend

FROM python:3.12-slim

LABEL org.opencontainers.image.title="memory_backend"
LABEL org.opencontainers.image.description="OKF v0.2 knowledge graph with chat UI"
LABEL org.opencontainers.image.version="0.2.0"

# ── System dependencies ──
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── App user (non-root) ──
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# ── Python dependencies ──
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── App code ──
COPY . .

# ── Data volume ──
RUN mkdir -p /data && chown -R appuser:appuser /app /data
VOLUME ["/data"]

# ── Runtime ──
USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
