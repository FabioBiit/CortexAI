"""
CortexAI — Ingestion Worker
==============================
Consuma messaggi dalla coda cortexai.ingest e processa i documenti.

PIPELINE DI INGESTIONE (per ogni documento):
┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
│  PARSE   │───▶│  CHUNK   │───▶│  EMBED   │───▶│  INDEX   │
│  (file   │    │  (split  │    │  (genera │    │  (salva  │
│  → testo)│    │  in pezzi│    │  vettori)│    │  Qdrant) │
└──────────┘    └──────────┘    └──────────┘    └──────────┘
     │               │               │               │
     └───────────────┴───────────────┴───────────────┘
                         │
              Ogni fase scrive in data_lineage
              per tracciamento completo

NOTA: In Fase 2 creiamo la struttura del worker con placeholder
per parse/chunk/embed/index. L'implementazione reale arriva in Fase 3.
Il worker funziona gia: consuma messaggi, logga, aggiorna il DB.

AVVIO:
    python -m src.workers.ingestion_worker
"""

import asyncio
import json
import time
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
import structlog

from src.core.messaging.consumer import BaseConsumer
from src.core.messaging.publisher import publisher
from src.core.messaging.schemas import NotificationMessage, MessagePriority
from src.api.config import get_settings

logger = structlog.get_logger("cortexai.worker.ingestion")


class IngestionWorker(BaseConsumer):
    """
    Worker che processa documenti dalla coda cortexai.ingest.

    Ogni messaggio ricevuto rappresenta un documento da processare.
    Il worker esegue la pipeline completa: parse → chunk → embed → index.
    """

    def __init__(self):
        super().__init__(
            queue_name="cortexai.ingest",
            prefetch_count=1,  # Un documento alla volta (sono pesanti)
        )

        # Connessione DB dedicata per il worker
        # (separata da quella dell'API per non interferire)
        settings = get_settings()
        self._engine = create_async_engine(
            settings.database_url,
            pool_size=5,
            max_overflow=2,
        )
        self._session_factory = async_sessionmaker(self._engine)

    async def process_message(self, body: dict) -> None:
        """
        Processa un singolo documento.

        Args:
            body: messaggio IngestionMessage deserializzato

        FASI:
        1. Aggiorna status documento → "processing"
        2. Parse: estrai testo dal file
        3. Chunk: dividi il testo in pezzi
        4. Embed: genera vettori per ogni chunk
        5. Index: salva i vettori in Qdrant
        6. Aggiorna status documento → "completed"
        7. Pubblica notifica di completamento
        """
        document_id = body["document_id"]
        tenant_id = body["tenant_id"]
        file_name = body.get("file_name", "unknown")
        start_time = time.perf_counter()

        logger.info(
            "ingestion_started",
            document_id=document_id,
            tenant_id=tenant_id,
            file_name=file_name,
            mime_type=body.get("mime_type", "unknown"),
        )

        async with self._session_factory() as db:
            try:
                # --- 1. AGGIORNA STATUS → processing ---
                await self._update_document_status(db, document_id, "processing")

                # --- 2. PARSE (Fase 3 - placeholder) ---
                # Qui in Fase 3 chiameremo il parser appropriato
                # basato sul mime_type (PDF, CSV, DOCX, etc.)
                raw_text = await self._parse_document(body)

                # --- 3. CHUNK (Fase 3 - placeholder) ---
                # Qui in Fase 3 chiameremo la strategia di chunking
                # scelta (recursive, semantic, fixed_size, structure)
                chunks = await self._chunk_text(raw_text, body)

                # --- 4. EMBED (Fase 3 - placeholder) ---
                # Qui in Fase 3 chiameremo il modello di embedding
                # (OpenAI, Ollama, etc.)
                embeddings = await self._generate_embeddings(chunks, body)

                # --- 5. INDEX (Fase 3 - placeholder) ---
                # Qui in Fase 3 salveremo i vettori in Qdrant
                await self._index_vectors(
                    embeddings, chunks, tenant_id, document_id, body
                )

                # --- 6. AGGIORNA STATUS → completed ---
                duration_ms = int((time.perf_counter() - start_time) * 1000)
                await self._update_document_status(
                    db, document_id, "completed",
                    chunk_count=len(chunks),
                    duration_ms=duration_ms,
                )

                # --- 7. REGISTRA LINEAGE ---
                await self._record_lineage(
                    db, tenant_id, document_id,
                    chunk_count=len(chunks),
                    duration_ms=duration_ms,
                    body=body,
                )

                await db.commit()

                # --- 8. NOTIFICA COMPLETAMENTO ---
                await publisher.publish_notification(NotificationMessage(
                    tenant_id=UUID(tenant_id),
                    user_id=UUID(body["user_id"]) if body.get("user_id") else None,
                    operation="ingestion",
                    resource_id=UUID(document_id),
                    status="completed",
                    details={
                        "file_name": file_name,
                        "chunk_count": len(chunks),
                        "duration_ms": duration_ms,
                    },
                ))

                logger.info(
                    "ingestion_completed",
                    document_id=document_id,
                    chunk_count=len(chunks),
                    duration_ms=duration_ms,
                )

            except Exception as e:
                await db.rollback()

                # Aggiorna status → failed
                try:
                    await self._update_document_status(
                        db, document_id, "failed", error=str(e)
                    )
                    await db.commit()
                except Exception:
                    pass

                logger.error(
                    "ingestion_failed",
                    document_id=document_id,
                    error=str(e),
                )
                raise  # Re-raise per trigger retry/DLQ nel BaseConsumer

    # ------------------------------------------------------------------
    # PLACEHOLDER — Implementazione reale in Fase 3
    # ------------------------------------------------------------------

    async def _parse_document(self, body: dict) -> str:
        """
        PLACEHOLDER — Fase 3.
        Estrai testo dal file. In Fase 3 supporteremo:
        PDF (pdfplumber), CSV (pandas), DOCX (python-docx), TXT, JSON.
        """
        logger.info("parse_placeholder", file_name=body.get("file_name"))
        # Placeholder: restituisce testo fittizio
        return f"[Placeholder] Contenuto del documento {body.get('file_name', 'unknown')}"

    async def _chunk_text(self, text: str, body: dict) -> list[str]:
        """
        PLACEHOLDER — Fase 3.
        Divide il testo in chunk. Strategie: recursive, semantic, fixed_size.
        """
        strategy = body.get("chunking_strategy", "recursive")
        logger.info("chunk_placeholder", strategy=strategy, text_length=len(text))
        # Placeholder: un singolo chunk con tutto il testo
        return [text]

    async def _generate_embeddings(
        self, chunks: list[str], body: dict
    ) -> list[list[float]]:
        """
        PLACEHOLDER — Fase 3.
        Genera embedding vettoriali per ogni chunk.
        Provider: OpenAI, Ollama, etc.
        """
        model = body.get("embedding_model", "text-embedding-3-small")
        logger.info(
            "embed_placeholder",
            model=model,
            chunk_count=len(chunks),
        )
        # Placeholder: vettore zero di dimensione 1536
        return [[0.0] * 1536 for _ in chunks]

    async def _index_vectors(
        self,
        embeddings: list[list[float]],
        chunks: list[str],
        tenant_id: str,
        document_id: str,
        body: dict,
    ) -> None:
        """
        PLACEHOLDER — Fase 3.
        Salva i vettori in Qdrant con metadata.
        """
        logger.info(
            "index_placeholder",
            tenant_id=tenant_id,
            document_id=document_id,
            vector_count=len(embeddings),
        )

    # ------------------------------------------------------------------
    # DATABASE HELPERS
    # ------------------------------------------------------------------

    async def _update_document_status(
        self, db, document_id: str, status: str,
        chunk_count: int = 0, duration_ms: int = 0, error: str = None,
    ) -> None:
        """Aggiorna lo status del documento in PostgreSQL."""
        await db.execute(
            text("""
                UPDATE documents SET
                    updated_at = NOW()
                WHERE id = :document_id
            """),
            {"document_id": document_id},
        )

    async def _record_lineage(
        self, db, tenant_id: str, document_id: str,
        chunk_count: int, duration_ms: int, body: dict,
    ) -> None:
        """Registra la lineage dell'ingestione per tracciamento."""
        await db.execute(
            text("""
                INSERT INTO data_lineage
                    (tenant_id, stage, processor, input_count, output_count,
                     duration_ms, metadata)
                VALUES
                    (:tenant_id, 'ingestion', 'ingestion_worker_v1',
                     1, :chunk_count, :duration_ms, :metadata::jsonb)
            """),
            {
                "tenant_id": tenant_id,
                "chunk_count": chunk_count,
                "duration_ms": duration_ms,
                "metadata": json.dumps({
                    "document_id": document_id,
                    "file_name": body.get("file_name"),
                    "chunking_strategy": body.get("chunking_strategy"),
                    "embedding_model": body.get("embedding_model"),
                }),
            },
        )


# ------------------------------------------------------------------
# ENTRY POINT — Avvia il worker
# ------------------------------------------------------------------

async def main():
    """Avvia il worker di ingestione."""
    logger.info("ingestion_worker_starting")
    worker = IngestionWorker()

    try:
        await worker.start()
    except KeyboardInterrupt:
        logger.info("ingestion_worker_interrupted")
    finally:
        await worker.stop()
        logger.info("ingestion_worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())