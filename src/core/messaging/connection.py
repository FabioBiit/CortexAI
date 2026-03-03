"""
CortexAI — RabbitMQ Connection Manager
========================================
Gestisce connessione a RabbitMQ con retry automatico e dichiarazione
della topologia (exchange, code, binding) via codice.

PERCHÉ LA TOPOLOGIA È DICHIARATA DAL CODICE:
RabbitMQ 4.0 tratta load_definitions come "stato completo desiderato",
sovrascrivendo gli utenti creati da RABBITMQ_DEFAULT_USER/PASS.
Poiché le credenziali DEVONO restare nel .env (mai nei file committati),
la topologia viene dichiarata programmaticamente al primo avvio.

Le operazioni declare_* di AMQP sono IDEMPOTENTI:
- Se l'exchange/coda esiste già con gli stessi parametri → noop
- Se non esiste → viene creato
- Se esiste con parametri diversi → errore (protezione)

TOPOLOGIA DICHIARATA:
  cortexai.main (topic exchange)
  ├── ingest.#    → cortexai.ingest     (DLX, priority, TTL 24h)
  ├── reindex.#   → cortexai.reindex    (DLX, priority)
  ├── gdpr.#      → cortexai.gdpr       (DLX, priority)
  └── notify.#    → cortexai.notifications

  cortexai.dlx (fanout exchange)
  └── *           → cortexai.dlq

PROTOCOLLO AMQP — Concetti chiave:
- Connection: connessione TCP al server (pesante, una per processo)
- Channel: canale logico dentro la connessione (leggero, moltiplicabile)
- Exchange: riceve messaggi e li instrada alle code in base a routing key
- Queue: coda dove i messaggi aspettano di essere consumati
- Binding: regola che collega un exchange a una queue

FLUSSO:
  Publisher → Exchange (topic) → Routing Key → Queue → Consumer
"""

import asyncio
from typing import Optional

from aio_pika import connect_robust, ExchangeType
from aio_pika.abc import AbstractRobustConnection, AbstractRobustChannel
import structlog

from src.api.config import get_settings

logger = structlog.get_logger("cortexai.messaging")


class RabbitMQManager:
    """
    Gestisce il ciclo di vita della connessione RabbitMQ e la topologia.

    Uso:
        manager = RabbitMQManager()
        await manager.connect()
        await manager.setup_topology()  # Crea exchange, code, binding
        channel = await manager.get_channel()
        await manager.close()
    """

    def __init__(self, url: Optional[str] = None):
        self._url = url or get_settings().rabbitmq_url
        self._connection: Optional[AbstractRobustConnection] = None
        self._channel: Optional[AbstractRobustChannel] = None
        self._topology_ready = False

    async def connect(self, max_retries: int = 5, retry_delay: float = 3.0) -> None:
        """
        Connessione a RabbitMQ con retry automatico.

        Perché retry? Docker Compose avvia i container in parallelo.
        RabbitMQ potrebbe non essere pronto quando l'API parte.
        depends_on verifica solo che il container sia avviato,
        NON che AMQP sia pronto ad accettare connessioni.
        """
        for attempt in range(1, max_retries + 1):
            try:
                # connect_robust: si riconnette automaticamente se cade.
                # È il modo consigliato da aio_pika per produzione.
                self._connection = await connect_robust(
                    self._url,
                    timeout=10,
                )

                # Crea il channel principale
                self._channel = await self._connection.channel()

                # QoS prefetch_count=1: il worker riceve UN messaggio
                # alla volta. Non ne riceve un altro finché non ha
                # completato (ack) il precedente.
                # Previene che un worker lento accumuli messaggi.
                await self._channel.set_qos(prefetch_count=1)

                logger.info(
                    "rabbitmq_connected",
                    attempt=attempt,
                    url=self._url.split("@")[1] if "@" in self._url else "***",
                )
                return

            except Exception as e:
                if attempt == max_retries:
                    logger.error(
                        "rabbitmq_connection_failed",
                        error=str(e),
                        attempts=max_retries,
                    )
                    raise ConnectionError(
                        f"Impossibile connettersi a RabbitMQ dopo {max_retries} tentativi: {e}"
                    )

                logger.warning(
                    "rabbitmq_connection_retry",
                    attempt=attempt,
                    max_retries=max_retries,
                    error=str(e),
                    retry_in_seconds=retry_delay,
                )
                await asyncio.sleep(retry_delay)

    async def setup_topology(self) -> None:
        """
        Dichiara la topologia completa: exchange, code, binding.

        IDEMPOTENTE: può essere chiamato più volte senza effetti collaterali.
        Se exchange/code esistono già con gli stessi parametri, non succede nulla.

        Questa funzione sostituisce definitions.json che in RabbitMQ 4.0
        sovrascriveva gli utenti (vedi rabbitmq.conf per dettagli).
        La topologia dichiarata qui corrisponde a quella documentata
        in docker/rabbitmq/definitions.json.
        """
        if self._topology_ready:
            return

        channel = await self.get_channel()

        # ------------------------------------------------------------------
        # EXCHANGE: cortexai.main (topic)
        # ------------------------------------------------------------------
        # Topic exchange: instrada messaggi in base alla routing key.
        # Pattern matching: "ingest.#" matcha "ingest.upload", "ingest.retry"
        # Il "#" matcha zero o più parole separate da punto.
        main_exchange = await channel.declare_exchange(
            "cortexai.main",
            type=ExchangeType.TOPIC,
            durable=True,       # Sopravvive al restart di RabbitMQ
            auto_delete=False,   # Non viene cancellato quando non ci sono code
        )

        # ------------------------------------------------------------------
        # EXCHANGE: cortexai.dlx (fanout)
        # ------------------------------------------------------------------
        # Dead Letter Exchange: riceve TUTTI i messaggi che falliscono
        # e li instrada alla DLQ. Fanout = manda a tutte le code collegate.
        dlx_exchange = await channel.declare_exchange(
            "cortexai.dlx",
            type=ExchangeType.FANOUT,
            durable=True,
            auto_delete=False,
        )

        # ------------------------------------------------------------------
        # CODA: cortexai.ingest
        # ------------------------------------------------------------------
        # Coda principale per ingestione documenti.
        # x-dead-letter-exchange: quando un messaggio fallisce tutti i retry,
        #   viene automaticamente spostato in cortexai.dlx → cortexai.dlq
        # x-max-priority: abilita priority queue (1-10)
        # x-message-ttl: messaggi scadono dopo 24h se non processati
        ingest_queue = await channel.declare_queue(
            "cortexai.ingest",
            durable=True,
            arguments={
                "x-dead-letter-exchange": "cortexai.dlx",
                "x-max-priority": 10,
                "x-message-ttl": 86400000,  # 24 ore in millisecondi
            },
        )
        await ingest_queue.bind(main_exchange, routing_key="ingest.#")

        # ------------------------------------------------------------------
        # CODA: cortexai.reindex
        # ------------------------------------------------------------------
        # Per batch re-indicizzazione (cambio embedding model, etc.)
        reindex_queue = await channel.declare_queue(
            "cortexai.reindex",
            durable=True,
            arguments={
                "x-dead-letter-exchange": "cortexai.dlx",
                "x-max-priority": 10,
            },
        )
        await reindex_queue.bind(main_exchange, routing_key="reindex.#")

        # ------------------------------------------------------------------
        # CODA: cortexai.gdpr
        # ------------------------------------------------------------------
        # Richieste GDPR (priorità critica, obbligo legale 30 giorni)
        gdpr_queue = await channel.declare_queue(
            "cortexai.gdpr",
            durable=True,
            arguments={
                "x-dead-letter-exchange": "cortexai.dlx",
                "x-max-priority": 10,
            },
        )
        await gdpr_queue.bind(main_exchange, routing_key="gdpr.#")

        # ------------------------------------------------------------------
        # CODA: cortexai.notifications
        # ------------------------------------------------------------------
        # Notifiche completamento/fallimento operazioni asincrone
        notifications_queue = await channel.declare_queue(
            "cortexai.notifications",
            durable=True,
        )
        await notifications_queue.bind(main_exchange, routing_key="notify.#")

        # ------------------------------------------------------------------
        # CODA: cortexai.dlq (Dead Letter Queue)
        # ------------------------------------------------------------------
        # Destinazione finale per messaggi che hanno esaurito tutti i retry.
        # Collegata al DLX exchange (fanout → tutti i messaggi finiscono qui).
        dlq_queue = await channel.declare_queue(
            "cortexai.dlq",
            durable=True,
        )
        await dlq_queue.bind(dlx_exchange)

        self._topology_ready = True
        logger.info(
            "rabbitmq_topology_ready",
            exchanges=["cortexai.main", "cortexai.dlx"],
            queues=[
                "cortexai.ingest", "cortexai.reindex", "cortexai.gdpr",
                "cortexai.notifications", "cortexai.dlq",
            ],
        )

    async def get_channel(self) -> AbstractRobustChannel:
        """
        Restituisce il channel attivo, riconnettendosi se necessario.
        """
        if self._connection is None or self._connection.is_closed:
            logger.warning("rabbitmq_reconnecting", reason="connection closed")
            await self.connect()

        if self._channel is None or self._channel.is_closed:
            self._channel = await self._connection.channel()
            await self._channel.set_qos(prefetch_count=1)

        return self._channel

    async def close(self) -> None:
        """Chiude la connessione in modo pulito."""
        if self._channel and not self._channel.is_closed:
            await self._channel.close()
        if self._connection and not self._connection.is_closed:
            await self._connection.close()
        logger.info("rabbitmq_disconnected")

    @property
    def is_connected(self) -> bool:
        return self._connection is not None and not self._connection.is_closed


# Istanza globale (singleton) — inizializzata al boot dell'app
rabbitmq_manager = RabbitMQManager()