"""
CortexAI — FastAPI Application Entry Point
=============================================
Questo è il file principale dell'API Gateway.
Configura FastAPI, registra middleware, monta le routes, e gestisce
il lifecycle dell'applicazione (startup e shutdown).

AVVIO:
  uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload

STRUTTURA DELL'APP:
┌────────────────────────────────────────────────────────────┐
│                    FastAPI Application                     │
│                                                            │
│  Middleware Stack (eseguiti in ordine per OGNI richiesta): │
│  ┌────────────────────────────────────────────────────┐    │
│  │ 1. CORS Middleware (gestione cross-origin)         │    │
│  │ 2. Observability Middleware (metriche + logging)   │    │
│  └────────────────────────────────────────────────────┘    │
│                                                            │
│  Route Groups:                                             │
│  ├── /api/health          → Health check                   │
│  ├── /api/metrics         → Prometheus metrics             │
│  ├── /api/v1/auth/*       → Autenticazione                 │
│  ├── /api/v1/ingest/*     → Ingestione documenti (Fase 2)  │
│  ├── /api/v1/documents/*  → Gestione documenti (Fase 3)    │
│  ├── /api/v1/search/*     → Ricerca (Fase 4)               │
│  ├── /api/v1/admin/*      → Amministrazione (Fase 1)       │
│  ├── /api/v1/gdpr/*       → GDPR compliance (Fase 7)       │
│  └── /api/v1/analytics/*  → Metriche e report (Fase 8)     │
└────────────────────────────────────────────────────────────┘
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.config import get_settings
from src.api.database import init_db, close_db
from src.api.middleware.observability import ObservabilityMiddleware, metrics_endpoint
from src.api.routes import health, auth, ingestion                    # ← FASE 2
from src.core.messaging.connection import rabbitmq_manager            # ← FASE 2

import structlog


# ---------------------------------------------------------------------------
# STRUCTURED LOGGING CONFIGURATION
# ---------------------------------------------------------------------------
# Configura structlog per produrre log JSON.
# In produzione, questi log vengono raccolti da Grafana Loki o ELK.
# ---------------------------------------------------------------------------

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer()  # In prod: JSONRenderer()
    ],
    wrapper_class=structlog.make_filtering_bound_logger(0),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger("cortexai")


# ---------------------------------------------------------------------------
# APPLICATION LIFECYCLE
# ---------------------------------------------------------------------------
# FastAPI supporta un "lifespan" context manager che gestisce cosa succede
# quando l'app parte (startup) e quando si ferma (shutdown).
#
# Startup: connessione al DB, verifica dipendenze, caricamento config
# Shutdown: chiusura connessioni, cleanup risorse
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Gestisce il lifecycle dell'applicazione.

    Tutto ciò che è PRIMA di "yield" viene eseguito allo STARTUP.
    Tutto ciò che è DOPO "yield" viene eseguito allo SHUTDOWN.
    """
    settings = get_settings()

    # --- STARTUP ---
    logger.info(
        "cortexai_starting",
        environment=settings.app_env,
        version="0.1.0",
    )

    # Inizializza connessione database
    try:
        await init_db()
        logger.info("database_connected", url=settings.database_url.split("@")[1])  # Log senza password
    except Exception as e:
        logger.error("database_connection_failed", error=str(e))
        raise

    # ← FASE 2: Inizializza connessione RabbitMQ e dichiara topologia
    try:
        await rabbitmq_manager.connect()
        await rabbitmq_manager.setup_topology()
        logger.info("rabbitmq_connected")
    except Exception as e:
        # Non blocchiamo l'avvio: RabbitMQ potrebbe non essere pronto.
        # Il manager si riconnetterà automaticamente al primo publish.
        logger.warning("rabbitmq_connection_deferred", error=str(e))

    logger.info("cortexai_ready", message="API Gateway pronto per ricevere richieste")

    yield  # L'app è in esecuzione

    # --- SHUTDOWN ---
    logger.info("cortexai_shutting_down")
    await rabbitmq_manager.close()                                    # ← FASE 2
    await close_db()
    logger.info("cortexai_stopped")


# ---------------------------------------------------------------------------
# FASTAPI APPLICATION
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    """
    Factory function per creare l'applicazione FastAPI.

    Perché una factory function e non un'istanza globale?
    - Testabilità: nei test puoi creare app con configurazioni diverse
    - Chiarezza: tutta la configurazione è in un posto
    - Pattern standard in applicazioni FastAPI/Flask production-grade
    """
    settings = get_settings()

    application = FastAPI(
        title="CortexAI API",
        description=(
            "Enterprise Multi-Tenant RAG & AI Platform.\n\n"
            "**Funzionalità:**\n"
            "- 🔐 Autenticazione JWT + API Key\n"
            "- 👥 Multi-tenant con Row-Level Security\n"
            "- 📄 Ingestione e chunking documenti\n"
            "- 🔍 Ricerca vettoriale + ibrida\n"
            "- 🤖 MCP Server per agenti AI\n"
            "- 📊 Osservabilità e controllo costi\n"
            "- 🛡️ GDPR compliance\n"
        ),
        version="0.1.0",
        docs_url="/api/docs",           # Swagger UI
        redoc_url="/api/redoc",          # ReDoc (alternativa più pulita)
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    # --- CORS Middleware ---
    # Permette richieste cross-origin (necessario se il frontend è su un dominio diverso)
    # In produzione, restringi allow_origins ai domini del frontend reale
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.is_development else [],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- Observability Middleware ---
    # Deve essere aggiunto DOPO CORS (l'ordine dei middleware è invertito in Starlette:
    # l'ultimo aggiunto è il primo eseguito)
    if settings.prometheus_enabled:
        application.add_middleware(ObservabilityMiddleware)

    # --- Routes ---
    # Health check (nessun prefix /v1 — è un endpoint infrastrutturale)
    application.include_router(health.router, prefix="/api")

    # Metrics endpoint (per Prometheus)
    application.add_api_route("/api/metrics", metrics_endpoint, methods=["GET"], include_in_schema=False)

    # API v1
    application.include_router(auth.router, prefix="/api/v1")
    application.include_router(ingestion.router, prefix="/api/v1")    # ← FASE 2

    # Le route seguenti verranno aggiunte nelle fasi successive:
    # application.include_router(documents.router, prefix="/api/v1")   # Fase 3
    # application.include_router(search.router, prefix="/api/v1")      # Fase 4
    # application.include_router(gdpr.router, prefix="/api/v1")        # Fase 7
    # application.include_router(analytics.router, prefix="/api/v1")   # Fase 8
    
    # application.include_router(admin.router, prefix="/api/v1")       # Fase futura (bonus)

    return application


# Istanza globale dell'app (usata da uvicorn)
app = create_app()