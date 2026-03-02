"""
CortexAI — Health Check Route
================================
Verifica che tutti i servizi dipendenti siano raggiungibili.

PERCHÉ IL HEALTH CHECK È IMPORTANTE:
- Docker usa questo endpoint per sapere se il container è "healthy"
- Nginx usa questo endpoint per sapere se il backend è pronto
- Prometheus monitora lo stato di salute
- Il load balancer usa questo endpoint per instradare il traffico

COSA CONTROLLA:
- PostgreSQL: connessione + query "SELECT 1"
- Redis: connessione + PING
- Qdrant: HTTP request a /readyz
- RabbitMQ: connessione AMQP
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from src.api.database import get_db
from src.api.schemas import HealthResponse
from src.api.config import get_settings

router = APIRouter(tags=["Health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check del servizio",
    description="Verifica che l'API e tutte le dipendenze siano operative.",
)
async def health_check(db: AsyncSession = Depends(get_db)):
    """
    Controlla lo stato di ogni dipendenza e restituisce un report.
    Se una dipendenza è down, il servizio è comunque "healthy"
    ma la dipendenza specifica è marcata come "error".
    """
    settings = get_settings()
    checks = {}

    # --- PostgreSQL ---
    try:
        await db.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as e:
        checks["postgres"] = f"error: {str(e)[:100]}"

    # --- Redis ---
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        await r.ping()
        await r.aclose()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {str(e)[:100]}"

    # --- Qdrant ---
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"http://{settings.qdrant_host}:{settings.qdrant_port}/readyz")
            checks["qdrant"] = "ok" if resp.status_code == 200 else f"error: status {resp.status_code}"
    except Exception as e:
        checks["qdrant"] = f"error: {str(e)[:100]}"

    # --- RabbitMQ ---
    try:
        import aio_pika
        connection = await aio_pika.connect_robust(settings.rabbitmq_url, timeout=5)
        await connection.close()
        checks["rabbitmq"] = "ok"
    except Exception as e:
        checks["rabbitmq"] = f"error: {str(e)[:100]}"

    # Stato generale: healthy se almeno PostgreSQL è ok
    overall_status = "healthy" if checks.get("postgres") == "ok" else "degraded"

    return HealthResponse(
        status=overall_status,
        environment=settings.app_env,
        checks=checks,
    )
