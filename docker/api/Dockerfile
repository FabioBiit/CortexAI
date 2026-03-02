# ============================================================================
# CortexAI — API Gateway Dockerfile
# ============================================================================
# Costruisce l'immagine Docker per il servizio FastAPI API Gateway.
#
# MULTI-STAGE BUILD:
# Stage 1 (builder): installa le dipendenze Python
# Stage 2 (runtime): copia solo il necessario → immagine finale più leggera
#
# PERCHÉ MULTI-STAGE:
# Le dipendenze di build (gcc, headers C) pesano ~200MB ma non servono
# a runtime. Con multi-stage, l'immagine finale è ~300MB invece di ~500MB.
# ============================================================================

# ===== STAGE 1: Builder =====
FROM python:3.12-slim as builder

# Installa dipendenze di sistema necessarie per compilare pacchetti Python
# (bcrypt, asyncpg, cryptography richiedono compilazione C)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Crea directory di lavoro
WORKDIR /build

# Copia SOLO requirements.txt per sfruttare la cache dei layer Docker.
# Se requirements.txt non cambia, Docker riutilizza il layer delle dipendenze
# (che è il passo più lento della build) senza reinstallare tutto.
COPY src/requirements.txt .

# Installa dipendenze Python in una directory dedicata
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ===== STAGE 2: Runtime =====
FROM python:3.12-slim

# Installa SOLO le librerie runtime necessarie (no compilatori)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Crea utente non-root per sicurezza
# (se l'app viene compromessa, l'attaccante non ha accesso root)
RUN useradd --create-home --shell /bin/bash appuser

# Copia le dipendenze Python dallo stage builder
COPY --from=builder /install /usr/local

# Directory di lavoro dell'applicazione
WORKDIR /app

# Copia il codice sorgente
COPY src/ ./src/

# Crea directory per uploads
RUN mkdir -p /app/uploads && chown appuser:appuser /app/uploads

# Passa all'utente non-root
USER appuser

# Espone la porta (documentazione, non apre realmente la porta)
EXPOSE 8000

# Health check: Docker verifica periodicamente che l'app sia viva
HEALTHCHECK --interval=15s --timeout=5s --retries=5 --start-period=30s \
    CMD curl -f http://localhost:8000/api/health || exit 1

# Comando di avvio
# --host 0.0.0.0: ascolta su tutte le interfacce (necessario in Docker)
# --port 8000: porta interna del container
# --workers 4: 4 processi worker (1 per core CPU consigliato)
# --loop uvloop: event loop ottimizzato (2x più veloce del default)
# --http httptools: parser HTTP in C (più veloce del default)
# In sviluppo, aggiungi --reload per hot-reload del codice
CMD ["uvicorn", "src.api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "4", \
     "--loop", "uvloop", \
     "--http", "httptools"]
