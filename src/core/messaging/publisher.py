"""
CortexAI — Message Publisher
==============================
Pubblica messaggi sulle code RabbitMQ attraverso l'exchange principale.

COME FUNZIONA:
1. Serializza il messaggio Pydantic in JSON
2. Crea un aio_pika.Message con headers (priority, delivery_mode, etc.)
3. Pubblica sull'exchange cortexai.main con la routing key appropriata
4. L'exchange instrada il messaggio alla coda giusta in base alla routing key

DELIVERY MODE:
- mode=1 (transient): il messaggio vive solo in RAM. Se RabbitMQ crasha, perso.
- mode=2 (persistent): il messaggio e scritto su disco. Sopravvive ai restart.
  Noi usiamo SEMPRE mode=2 per non perdere documenti da processare.

ESEMPIO USO:
    publisher = MessagePublisher()
    await publisher.publish_ingestion(IngestionMessage(
        tenant_id=uuid, document_id=uuid, file_path="/uploads/doc.pdf",
        file_name="doc.pdf", mime_type="application/pdf", file_size_bytes=1024
    ))
"""

import json
from datetime import datetime, timezone

import aio_pika
from aio_pika import Message, DeliveryMode
import structlog

from src.core.messaging.connection import rabbitmq_manager
from src.core.messaging.schemas import (
    IngestionMessage,
    ReindexMessage,
    GDPRMessage,
    NotificationMessage,
)

logger = structlog.get_logger("cortexai.publisher")

# Nome dell'exchange principale (dichiarato da setup_topology() in connection.py)
EXCHANGE_NAME = "cortexai.main"


class MessagePublisher:
    """
    Pubblica messaggi tipizzati sulle code RabbitMQ.

    Ogni metodo publish_* accetta un messaggio Pydantic tipizzato
    e lo pubblica sulla coda corretta con la routing key appropriata.
    """

    async def _get_exchange(self) -> aio_pika.abc.AbstractExchange:
        """
        Ottiene il riferimento all'exchange principale.

        L'exchange è già stato dichiarato da setup_topology() al boot
        dell'applicazione (chiamato nel lifespan di main.py).
        get_exchange() ottiene un riferimento senza ri-dichiararlo.
        """
        channel = await rabbitmq_manager.get_channel()
        exchange = await channel.get_exchange(EXCHANGE_NAME)
        return exchange

    def _create_message(self, body: dict, priority: int = 5) -> Message:
        """
        Crea un messaggio AMQP con le proprieta corrette.

        delivery_mode=PERSISTENT: il messaggio viene scritto su disco.
        Se RabbitMQ crasha e riparte, il messaggio e ancora li.
        Questo e essenziale per non perdere documenti da processare.

        content_type=application/json: dice al consumer come deserializzare.

        message_id + timestamp: per tracciamento e deduplicazione.
        """
        return Message(
            body=json.dumps(body, default=str).encode("utf-8"),
            delivery_mode=DeliveryMode.PERSISTENT,  # Sopravvive ai restart
            content_type="application/json",
            priority=priority,
            message_id=body.get("message_id", ""),
            timestamp=datetime.now(timezone.utc),
            headers={
                "version": body.get("version", "1.0"),
                "tenant_id": str(body.get("tenant_id", "")),
            },
        )

    async def publish_ingestion(
        self,
        message: IngestionMessage,
        routing_key: str = "ingest.upload",
    ) -> None:
        """
        Pubblica un messaggio di ingestione.

        Routing keys possibili:
        - "ingest.upload"    → documento caricato dall'utente
        - "ingest.connector" → documento da connector esterno
        - "ingest.retry"     → retry di un'ingestione fallita
        """
        exchange = await self._get_exchange()
        msg = self._create_message(
            body=message.model_dump(),
            priority=message.priority.value,
        )

        await exchange.publish(msg, routing_key=routing_key)

        logger.info(
            "message_published",
            queue="cortexai.ingest",
            routing_key=routing_key,
            document_id=str(message.document_id),
            tenant_id=str(message.tenant_id),
            message_id=message.message_id,
        )

    async def publish_reindex(self, message: ReindexMessage) -> None:
        """Pubblica un messaggio di re-indicizzazione."""
        exchange = await self._get_exchange()
        msg = self._create_message(
            body=message.model_dump(),
            priority=message.priority.value,
        )

        routing_key = f"reindex.{message.reindex_type}"
        await exchange.publish(msg, routing_key=routing_key)

        logger.info(
            "message_published",
            queue="cortexai.reindex",
            routing_key=routing_key,
            tenant_id=str(message.tenant_id),
            document_count=len(message.document_ids),
        )

    async def publish_gdpr(self, message: GDPRMessage) -> None:
        """
        Pubblica un messaggio GDPR (priorita CRITICA).

        I messaggi GDPR hanno priorita 9 (massima) e vengono
        processati PRIMA di qualsiasi altro messaggio nella coda.
        """
        exchange = await self._get_exchange()
        msg = self._create_message(
            body=message.model_dump(),
            priority=message.priority.value,  # 9 = CRITICAL
        )

        routing_key = f"gdpr.{message.request_type}"
        await exchange.publish(msg, routing_key=routing_key)

        logger.info(
            "message_published",
            queue="cortexai.gdpr",
            routing_key=routing_key,
            request_type=message.request_type,
            gdpr_request_id=str(message.gdpr_request_id),
        )

    async def publish_notification(self, message: NotificationMessage) -> None:
        """Pubblica una notifica di completamento/fallimento."""
        exchange = await self._get_exchange()
        msg = self._create_message(
            body=message.model_dump(),
            priority=message.priority.value,
        )

        routing_key = f"notify.{message.operation}"
        await exchange.publish(msg, routing_key=routing_key)

        logger.info(
            "notification_published",
            operation=message.operation,
            status=message.status,
            resource_id=str(message.resource_id),
        )


# Istanza globale
publisher = MessagePublisher()