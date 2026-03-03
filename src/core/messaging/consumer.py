"""
CortexAI — Base Message Consumer
===================================
Classe base astratta per tutti i worker che consumano messaggi da RabbitMQ.

PATTERN:
Ogni worker eredita da BaseConsumer e implementa process_message().
La classe base gestisce: connessione, retry, ack/nack, error handling, DLQ.

FLUSSO DI UN MESSAGGIO:
1. RabbitMQ consegna il messaggio al consumer
2. BaseConsumer deserializza il JSON
3. BaseConsumer chiama process_message() del worker specifico
4. Se OK → ACK (messaggio rimosso dalla coda)
5. Se errore:
   a. retry_count < max_retries → NACK + requeue (rimesso in coda)
   b. retry_count >= max_retries → NACK + NO requeue → va in DLQ

ACK/NACK spiegato:
- ACK (acknowledge): "Ho processato il messaggio, puoi rimuoverlo"
- NACK (negative acknowledge): "Non sono riuscito a processarlo"
  - requeue=True: "Rimettilo in coda, riprovero dopo"
  - requeue=False: "Non riprovare, mandalo in Dead Letter Queue"
"""

import json
import asyncio
from abc import ABC, abstractmethod
from typing import Optional

from aio_pika.abc import AbstractIncomingMessage
import structlog

from src.core.messaging.connection import RabbitMQManager

logger = structlog.get_logger("cortexai.consumer")


class BaseConsumer(ABC):
    """
    Classe base per tutti i worker CortexAI.

    Per creare un nuovo worker:
    1. Eredita da BaseConsumer
    2. Implementa process_message(body: dict)
    3. Chiama start() per iniziare a consumare

    Esempio:
        class MyWorker(BaseConsumer):
            async def process_message(self, body: dict) -> None:
                print(f"Processing: {body}")

        worker = MyWorker(queue_name="cortexai.ingest")
        await worker.start()
    """

    def __init__(
        self,
        queue_name: str,
        rabbitmq_url: Optional[str] = None,
        prefetch_count: int = 1,
    ):
        """
        Args:
            queue_name: nome della coda da consumare (es. "cortexai.ingest")
            rabbitmq_url: URL AMQP. Se None, legge da settings.
            prefetch_count: quanti messaggi ricevere alla volta.
                1 = un messaggio alla volta (safe, consigliato per task pesanti)
                N = N messaggi in parallelo (piu throughput, ma piu RAM)
        """
        self.queue_name = queue_name
        self.prefetch_count = prefetch_count
        self._manager = RabbitMQManager(url=rabbitmq_url)
        self._running = False

    @abstractmethod
    async def process_message(self, body: dict) -> None:
        """
        Processa un singolo messaggio. DA IMPLEMENTARE nel worker specifico.

        Args:
            body: il messaggio deserializzato (dizionario Python)

        Raises:
            Exception: qualsiasi eccezione causa retry o invio in DLQ
        """
        pass

    async def start(self) -> None:
        """
        Avvia il consumer. Si connette a RabbitMQ e inizia a consumare.
        Blocca l'esecuzione finche non viene fermato con stop().

        NOTA: Il worker dichiara la propria topologia al connect.
        Questo è IDEMPOTENTE: se exchange/code esistono già, non succede nulla.
        Così il worker può avviarsi indipendentemente dall'API Gateway.
        """
        self._running = True
        await self._manager.connect()

        # Dichiara topologia (idempotente — safe anche se l'API l'ha già fatto)
        await self._manager.setup_topology()

        channel = await self._manager.get_channel()
        await channel.set_qos(prefetch_count=self.prefetch_count)

        # Ottieni riferimento alla coda (dichiarata da setup_topology)
        queue = await channel.get_queue(self.queue_name)

        logger.info(
            "consumer_started",
            queue=self.queue_name,
            prefetch=self.prefetch_count,
        )

        # Inizia a consumare: per ogni messaggio, chiama _handle_message
        await queue.consume(self._handle_message)

        # Mantieni il consumer attivo finche non viene fermato
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Ferma il consumer e chiude la connessione."""
        self._running = False
        await self._manager.close()
        logger.info("consumer_stopped", queue=self.queue_name)

    async def _handle_message(self, message: AbstractIncomingMessage) -> None:
        """
        Handler interno per ogni messaggio ricevuto.

        FLUSSO:
        1. Deserializza il JSON
        2. Chiama process_message() del worker specifico
        3. Gestisce successo (ACK) o fallimento (NACK/DLQ)
        """
        message_id = message.message_id or "unknown"
        body = {}

        try:
            # Deserializza il corpo del messaggio
            body = json.loads(message.body.decode("utf-8"))

            logger.info(
                "message_received",
                queue=self.queue_name,
                message_id=message_id,
                tenant_id=body.get("tenant_id", "unknown"),
            )

            # Chiama il metodo process_message del worker specifico
            await self.process_message(body)

            # SUCCESSO: rimuovi il messaggio dalla coda
            await message.ack()

            logger.info(
                "message_processed",
                queue=self.queue_name,
                message_id=message_id,
                status="success",
            )

        except json.JSONDecodeError as e:
            # Messaggio non e JSON valido → impossibile riprovare → DLQ
            logger.error(
                "message_invalid_json",
                queue=self.queue_name,
                message_id=message_id,
                error=str(e),
            )
            # requeue=False: non rimettere in coda, manda in DLQ
            await message.nack(requeue=False)

        except Exception as e:
            # Errore nel processing: valuta se riprovare o mandare in DLQ
            await self._handle_failure(message, body, e)

    async def _handle_failure(
        self,
        message: AbstractIncomingMessage,
        body: dict,
        error: Exception,
    ) -> None:
        """
        Gestisce il fallimento del processing di un messaggio.

        LOGICA:
        - Se retry_count < max_retries → NACK + requeue (riprova)
        - Se retry_count >= max_retries → NACK + no requeue (DLQ)

        NOTA: RabbitMQ non supporta nativamente un contatore retry
        per messaggio. Lo gestiamo nel payload del messaggio stesso.
        Quando facciamo requeue, il messaggio torna in coda con
        retry_count incrementato.
        """
        retry_count = body.get("retry_count", 0)
        max_retries = body.get("max_retries", 3)
        message_id = message.message_id or "unknown"

        if retry_count < max_retries:
            # Riprova: rimetti in coda
            logger.warning(
                "message_retry",
                queue=self.queue_name,
                message_id=message_id,
                retry_count=retry_count + 1,
                max_retries=max_retries,
                error=str(error),
            )
            # requeue=True: RabbitMQ rimette il messaggio in coda
            await message.nack(requeue=True)
        else:
            # Esauriti i retry: manda in Dead Letter Queue
            logger.error(
                "message_sent_to_dlq",
                queue=self.queue_name,
                message_id=message_id,
                retry_count=retry_count,
                error=str(error),
            )
            # requeue=False: il messaggio va nell'exchange DLX
            # configurato da setup_topology(), che lo instrada
            # alla coda cortexai.dlq
            await message.nack(requeue=False)