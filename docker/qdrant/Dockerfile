# ============================================================================
# CortexAI — Qdrant Dockerfile
# ============================================================================
# Costruisce l'immagine Docker per il servizio Qdrant.

FROM qdrant/qdrant:v1.12.5

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*