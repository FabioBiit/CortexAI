"""
CortexAI — Observability Middleware
=====================================
Raccoglie metriche e log strutturati per OGNI richiesta API.

METRICHE ESPOSTE (endpoint /api/metrics):
- request_duration_seconds: quanto tempo impiega ogni richiesta (istogramma)
- requests_total: contatore richieste per endpoint e status code
- active_requests: quante richieste sono in corso ORA (gauge)

LOGGING STRUTTURATO:
Ogni richiesta genera un log JSON con: method, path, status, duration,
tenant_id, user_id, request_id. Questo formato è facilmente parsabile
da Grafana Loki, ELK, o qualsiasi log aggregator.
"""

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
import structlog

# ---------------------------------------------------------------------------
# PROMETHEUS METRICS
# ---------------------------------------------------------------------------
# Definiamo le metriche una sola volta a livello di modulo.
# Prometheus le raccoglie ogni 15 secondi dall'endpoint /metrics.
# ---------------------------------------------------------------------------

# Contatore: quante richieste per metodo, endpoint e status code
REQUEST_COUNT = Counter(
    "cortexai_requests_total",
    "Totale richieste HTTP",
    ["method", "endpoint", "status_code"],
)

# Istogramma: distribuzione delle latenze delle richieste
# I bucket definiscono le "fasce" di latenza che ci interessano
REQUEST_DURATION = Histogram(
    "cortexai_request_duration_seconds",
    "Durata richieste HTTP in secondi",
    ["method", "endpoint"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    # ^ 10ms, 50ms, 100ms, 250ms, 500ms, 1s, 2.5s, 5s, 10s
    # Queste fasce ci permettono di calcolare percentili (p50, p95, p99)
)

# Gauge: quante richieste sono in elaborazione in questo momento
ACTIVE_REQUESTS = Gauge(
    "cortexai_active_requests",
    "Richieste HTTP attualmente in elaborazione",
)

# ---------------------------------------------------------------------------
# STRUCTURED LOGGER
# ---------------------------------------------------------------------------
# structlog produce log in formato JSON, perfetti per parsing automatico.
# Esempio output:
# {"event": "request_completed", "method": "GET", "path": "/api/v1/documents",
#  "status": 200, "duration_ms": 45, "tenant_id": "abc-123", "request_id": "def-456"}
# ---------------------------------------------------------------------------

logger = structlog.get_logger("cortexai.api")


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """
    Middleware che intercetta OGNI richiesta per raccogliere metriche e log.

    Starlette (su cui FastAPI è costruito) supporta middleware che wrappano
    il ciclo request-response. Questo middleware:
    1. Prima della richiesta: registra tempo di inizio, genera request_id
    2. Dopo la risposta: calcola durata, aggiorna metriche, scrive log
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # --- PRE-REQUEST ---

        # Genera un ID unico per questa richiesta (per tracing end-to-end)
        # Se Nginx ha già generato un X-Request-ID, lo riutilizziamo
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))

        # Registra il tempo di inizio
        start_time = time.perf_counter()

        # Incrementa il contatore di richieste attive
        ACTIVE_REQUESTS.inc()

        # Normalizza il path per le metriche (evita cardinalità esplosiva)
        # Senza normalizzazione: /api/v1/documents/abc-123 e /api/v1/documents/def-456
        # sarebbero due metriche separate → milioni di time series → crash Prometheus
        endpoint = self._normalize_path(request.url.path)

        try:
            # --- EXECUTE REQUEST ---
            response = await call_next(request)

            # --- POST-REQUEST ---
            duration = time.perf_counter() - start_time

            # Aggiorna metriche Prometheus
            REQUEST_COUNT.labels(
                method=request.method,
                endpoint=endpoint,
                status_code=response.status_code,
            ).inc()

            REQUEST_DURATION.labels(
                method=request.method,
                endpoint=endpoint,
            ).observe(duration)

            # Aggiungi headers di risposta utili
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Response-Time"] = f"{duration:.3f}s"

            # Log strutturato (solo per richieste non-health e non-metrics)
            if not self._is_internal_endpoint(request.url.path):
                await logger.ainfo(
                    "request_completed",
                    method=request.method,
                    path=request.url.path,
                    status=response.status_code,
                    duration_ms=round(duration * 1000, 2),
                    request_id=request_id,
                    client_ip=request.client.host if request.client else "unknown",
                )

            return response

        except Exception as e:
            # Log dell'errore
            duration = time.perf_counter() - start_time
            await logger.aerror(
                "request_failed",
                method=request.method,
                path=request.url.path,
                error=str(e),
                duration_ms=round(duration * 1000, 2),
                request_id=request_id,
            )
            raise

        finally:
            # Decrementa SEMPRE il contatore, anche in caso di errore
            ACTIVE_REQUESTS.dec()

    @staticmethod
    def _normalize_path(path: str) -> str:
        """
        Normalizza i path per evitare cardinalità esplosiva nelle metriche.

        Esempio:
          /api/v1/documents/550e8400-e29b-41d4-a716-446655440000
          → /api/v1/documents/{id}

        Senza normalizzazione, ogni documento unico creerebbe una nuova serie
        temporale in Prometheus, portando a milioni di serie → crash.
        """
        parts = path.strip("/").split("/")
        normalized = []
        for part in parts:
            # Se sembra un UUID, sostituisci con {id}
            if len(part) == 36 and part.count("-") == 4:
                normalized.append("{id}")
            # Se è un numero, sostituisci con {id}
            elif part.isdigit():
                normalized.append("{id}")
            else:
                normalized.append(part)
        return "/" + "/".join(normalized)

    @staticmethod
    def _is_internal_endpoint(path: str) -> bool:
        """Non loggare endpoint interni (troppo rumore)."""
        return path in ("/api/health", "/api/metrics", "/health", "/metrics")


async def metrics_endpoint(request: Request) -> Response:
    """
    Endpoint che espone le metriche in formato Prometheus.
    Prometheus scrapa questo endpoint ogni 15 secondi.
    """
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
