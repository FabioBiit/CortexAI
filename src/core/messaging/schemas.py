"""
CortexAI — Message Schemas
============================
Definisce la struttura dei messaggi che viaggiano attraverso RabbitMQ.

PERCHE SCHEMI TIPIZZATI:
Senza schemi, i messaggi sono dizionari generici — nessuna garanzia
su quali campi contengano. Con Pydantic:
1. Validazione automatica: se un campo manca, errore immediato
2. Serializzazione: da/per JSON automatica
3. Documentazione: l'IDE mostra i campi disponibili
4. Versionamento: se lo schema cambia, i vecchi messaggi falliscono
   in modo prevedibile (non corruzione silente dei dati)

CONVENZIONE ROUTING KEY:
  cortexai.main exchange (topic type)
  ├── ingest.upload     → cortexai.ingest queue  (documento caricato via UI/API)
  ├── ingest.connector  → cortexai.ingest queue  (documento da connector esterno)
  ├── reindex.full      → cortexai.reindex queue (re-indicizzazione completa)
  ├── reindex.partial   → cortexai.reindex queue (re-indicizzazione parziale)
  ├── gdpr.erasure      → cortexai.gdpr queue   (richiesta cancellazione dati)
  ├── gdpr.access       → cortexai.gdpr queue   (richiesta accesso dati)
  └── notify.#          → cortexai.notifications (notifiche completamento)
"""

from pydantic import BaseModel, Field
from uuid import UUID, uuid4
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class MessagePriority(int, Enum):
    """
    Priorita dei messaggi RabbitMQ (0-9, dove 9 e la piu alta).
    I messaggi GDPR hanno priorita massima per compliance legale.
    """
    LOW = 1
    NORMAL = 5
    HIGH = 7
    CRITICAL = 9  # GDPR, sicurezza


class IngestionMessage(BaseModel):
    """
    Messaggio per la coda cortexai.ingest.
    Inviato quando un documento viene caricato e deve essere processato.

    CICLO DI VITA DEL MESSAGGIO:
    1. Utente carica PDF via API
    2. API salva metadati in PostgreSQL (tabella documents)
    3. API pubblica IngestionMessage su cortexai.ingest
    4. Worker consuma il messaggio
    5. Worker: parse → chunk → embed → indicizza in Qdrant
    6. Worker: aggiorna status in PostgreSQL
    7. Worker: pubblica NotificationMessage su cortexai.notifications
    """

    # Header (presente in TUTTI i tipi di messaggio)
    message_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    version: str = "1.0"  # Per gestire evoluzioni dello schema

    # Chi
    tenant_id: UUID
    user_id: Optional[UUID] = None  # None se triggerato da sistema/connector

    # Cosa
    document_id: UUID                           # ID del documento in PostgreSQL
    file_path: str                              # Path del file nello storage
    file_name: str                              # Nome originale del file
    mime_type: str                              # application/pdf, text/csv, etc.
    file_size_bytes: int

    # Configurazione processing
    chunking_strategy: str = "recursive"        # fixed_size, semantic, recursive, structure
    chunking_config: dict = Field(
        default={"chunk_size": 512, "chunk_overlap": 50},
    )
    embedding_model: str = "text-embedding-3-small"

    # Priorita e retry
    priority: MessagePriority = MessagePriority.NORMAL
    max_retries: int = 3                        # Tentativi prima di finire in DLQ
    retry_count: int = 0                        # Contatore retry corrente


class ReindexMessage(BaseModel):
    """
    Messaggio per la coda cortexai.reindex.
    Triggerato quando si vuole re-processare documenti esistenti
    (es. cambio embedding model, cambio strategia di chunking).
    """
    message_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    version: str = "1.0"

    tenant_id: UUID

    # Scope: quali documenti re-indicizzare
    document_ids: list[UUID] = []               # Se vuoto → tutti i documenti del tenant
    reindex_type: str = "full"                  # full (embed + index) o partial (solo index)

    # Nuova configurazione (se diversa dall'attuale)
    new_chunking_strategy: Optional[str] = None
    new_chunking_config: Optional[dict] = None
    new_embedding_model: Optional[str] = None

    priority: MessagePriority = MessagePriority.NORMAL


class GDPRMessage(BaseModel):
    """
    Messaggio per la coda cortexai.gdpr.
    Richieste GDPR hanno priorita CRITICA (obbligo legale).
    Deadline legale: 30 giorni dalla richiesta.
    """
    message_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    version: str = "1.0"

    tenant_id: UUID
    gdpr_request_id: UUID                       # ID in tabella gdpr_requests
    request_type: str                           # access, erasure, portability, rectification
    subject_email: str                          # Email del soggetto dei dati

    priority: MessagePriority = MessagePriority.CRITICAL


class NotificationMessage(BaseModel):
    """
    Messaggio per la coda cortexai.notifications.
    Notifica il completamento (o fallimento) di un'operazione asincrona.
    """
    message_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    version: str = "1.0"

    tenant_id: UUID
    user_id: Optional[UUID] = None

    # Risultato
    operation: str                              # ingestion, reindex, gdpr
    resource_id: UUID                           # ID della risorsa processata
    status: str                                 # completed, failed
    details: dict = Field(default_factory=dict) # Info aggiuntive (chunk_count, duration, error)

    priority: MessagePriority = MessagePriority.LOW