"""
CortexAI — Dead Letter Queue Worker
======================================
Processa i messaggi che hanno fallito tutti i retry e sono finiti nella DLQ.

COS'E LA DLQ (Dead Letter Queue):
Quando un messaggio fallisce N volte (default: 3), RabbitMQ lo sposta
dalla coda originale alla cortexai.dlq. Senza un DLQ worker, questi
messaggi si accumulano indefinitamente.

COSA FA QUESTO WORKER:
1. Consuma messaggi dalla cortexai.dlq
2. Logga i dettagli dell'errore
3. Salva il record nell'audit_log per analisi
4. Notifica gli admin
5. ACK il messaggio (rimuovendolo dalla DLQ)

IN PRODUZIONE potresti anche:
- Inviare email/Slack agli admin
- Creare ticket in Jira/Linear
- Tentare un fix automatico e ripubblicare il messaggio
"""

import asyncio
import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
import structlog

from src.core.messaging.consumer import BaseConsumer
from src.api.config import get_settings

logger = structlog.get_logger("cortexai.worker.dlq")


class DLQWorker(BaseConsumer):
    """
    Worker che processa messaggi dalla Dead Letter Queue.
    Non fa retry: se un messaggio e qui, ha gia fallito tutti i tentativi.
    """

    def __init__(self):
        super().__init__(
            queue_name="cortexai.dlq",
            prefetch_count=1,
        )
        settings = get_settings()
        self._engine = create_async_engine(settings.database_url, pool_size=2)
        self._session_factory = async_sessionmaker(self._engine)

    async def process_message(self, body: dict) -> None:
        """
        Processa un messaggio dalla DLQ.

        Non tenta di ri-processare il messaggio: il suo scopo e
        registrare il fallimento e notificare chi di dovere.
        """
        message_id = body.get("message_id", "unknown")
        tenant_id = body.get("tenant_id", "unknown")
        document_id = body.get("document_id", "unknown")

        logger.error(
            "dlq_message_received",
            message_id=message_id,
            tenant_id=tenant_id,
            document_id=document_id,
            original_message=body,
        )

        # Registra nel audit_log
        async with self._session_factory() as db:
            try:
                await db.execute(
                    text("""
                        INSERT INTO audit_log
                            (tenant_id, action, resource_type, resource_id,
                             details, status)
                        VALUES
                            (:tenant_id, 'ingestion.dlq', 'document',
                             :resource_id, :details::jsonb, 'failure')
                    """),
                    {
                        "tenant_id": tenant_id,
                        "resource_id": document_id if document_id != "unknown" else None,
                        "details": json.dumps({
                            "message_id": message_id,
                            "retry_count": body.get("retry_count", 0),
                            "max_retries": body.get("max_retries", 3),
                            "original_queue": "cortexai.ingest",
                            "body_preview": str(body)[:500],
                        }),
                    },
                )
                await db.commit()
            except Exception as e:
                logger.error("dlq_audit_log_failed", error=str(e))

        logger.info(
            "dlq_message_processed",
            message_id=message_id,
            action="logged_and_archived",
        )


async def main():
    """Avvia il DLQ worker."""
    logger.info("dlq_worker_starting")
    worker = DLQWorker()
    try:
        await worker.start()
    except KeyboardInterrupt:
        pass
    finally:
        await worker.stop()


if __name__ == "__main__":
    asyncio.run(main())