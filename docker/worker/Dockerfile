# ============================================================================
# CortexAI — Ingestion Worker Dockerfile
# ============================================================================
# Identico all'API Dockerfile ma con un CMD diverso:
# l'API avvia uvicorn (server web), il worker avvia il consumer Python.
# ============================================================================

# Stage 1: Builder
FROM python:3.12-slim as builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY src/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: Runtime
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash appuser

COPY --from=builder /install /usr/local
WORKDIR /app
COPY src/ ./src/

USER appuser

# Il worker non espone porte HTTP: non e un server web,
# e un consumer che ascolta messaggi da RabbitMQ.

# Health check: il worker e vivo se il processo Python gira
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD pgrep -f "ingestion_worker" || exit 1

# CMD: avvia il worker di ingestione
# -u: unbuffered output (i log appaiono immediatamente in Docker)
CMD ["python", "-u", "-m", "src.workers.ingestion_worker"]