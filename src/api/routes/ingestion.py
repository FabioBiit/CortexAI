"""
CortexAI — Ingestion API Routes
==================================
Endpoint per triggerare l'ingestione di documenti.

FLUSSO COMPLETO:
1. Client carica un file via POST /api/v1/ingest/upload
2. API salva i metadati in PostgreSQL (tabella documents)
3. API pubblica un IngestionMessage su RabbitMQ
4. API restituisce 202 Accepted (NON 200 OK!)
5. Il worker processa il documento in background
6. Quando finito, il worker pubblica una notifica

PERCHE 202 e NON 200?
- 200 OK = "ho completato la tua richiesta"
- 202 Accepted = "ho ACCETTATO la tua richiesta, la sto processando"
  Il documento non e ancora indicizzato quando rispondiamo 202.
  Il client puo controllare lo stato con GET /api/v1/ingest/{id}/status
"""

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from uuid import UUID, uuid4
from typing import Optional

import structlog

from src.api.database import get_db
from src.api.middleware.auth import get_current_user, AuthenticatedUser
from src.api.middleware.tenant_context import set_tenant_context
from src.core.messaging.publisher import publisher
from src.core.messaging.schemas import IngestionMessage

logger = structlog.get_logger("cortexai.api.ingestion")

router = APIRouter(prefix="/ingest", tags=["Ingestion"])


@router.post(
    "/upload",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Carica un documento per ingestione",
    description=(
        "Carica un file e avvia la pipeline di ingestione asincrona. "
        "Restituisce 202 Accepted con il document_id. "
        "Usa GET /ingest/{id}/status per controllare il progresso."
    ),
)
async def upload_document(
    file: UploadFile = File(..., description="Il file da caricare (PDF, CSV, DOCX, TXT)"),
    title: Optional[str] = Form(None, description="Titolo del documento. Default: nome file."),
    classification: str = Form("internal", description="Classificazione: public, internal, confidential, restricted"),
    chunking_strategy: str = Form("recursive", description="Strategia: fixed_size, semantic, recursive, structure"),
    chunk_size: int = Form(512, description="Dimensione target dei chunk in token"),
    chunk_overlap: int = Form(50, description="Sovrapposizione tra chunk adiacenti in token"),
    user: AuthenticatedUser = Depends(set_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    """
    Carica un documento e lo invia alla pipeline di ingestione.

    Il file viene salvato nello storage locale, i metadati in PostgreSQL,
    e un messaggio viene pubblicato su RabbitMQ per il processing asincrono.
    """

    # Verifica permessi
    if not user.has_permission("documents:write"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "insufficient_permissions", "message": "Serve il permesso documents:write"},
        )

    # Validazione file
    if not file.filename:
        raise HTTPException(status_code=400, detail={"error": "no_file", "message": "Nessun file caricato"})

    # Leggi il contenuto del file
    content = await file.read()
    file_size = len(content)

    # Limite dimensione: 50MB (configurabile)
    max_size = 50 * 1024 * 1024
    if file_size > max_size:
        raise HTTPException(
            status_code=413,
            detail={"error": "file_too_large", "message": f"File troppo grande ({file_size} bytes). Max: {max_size}"},
        )

    # Genera ID documento e path di storage
    document_id = uuid4()
    file_path = f"uploads/{user.tenant_id}/{document_id}/{file.filename}"

    # TODO Fase 3: salvare il file su disco/object storage
    # Per ora logghiamo e salviamo solo i metadati

    # Calcola hash del file (per deduplicazione e versioning)
    file_hash = hashlib.sha256(content).hexdigest()

    # Salva metadati in PostgreSQL
    result = await db.execute(
        text("""
            INSERT INTO documents
                (id, tenant_id, uploaded_by, title, source_type, mime_type,
                 file_size_bytes, file_hash, classification)
            VALUES
                (:id, :tenant_id, :uploaded_by, :title, 'upload', :mime_type,
                 :file_size_bytes, :file_hash, :classification)
            RETURNING id, title, mime_type, file_size_bytes, classification, created_at
        """),
        {
            "id": str(document_id),
            "tenant_id": str(user.tenant_id),
            "uploaded_by": str(user.user_id),
            "title": title or file.filename,
            "mime_type": file.content_type,
            "file_size_bytes": file_size,
            "file_hash": file_hash,
            "classification": classification,
        },
    )
    doc_row = result.first()
    await db.commit()

    # Pubblica messaggio su RabbitMQ per processing asincrono
    await publisher.publish_ingestion(IngestionMessage(
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        document_id=document_id,
        file_path=file_path,
        file_name=file.filename,
        mime_type=file.content_type or "application/octet-stream",
        file_size_bytes=file_size,
        chunking_strategy=chunking_strategy,
        chunking_config={"chunk_size": chunk_size, "chunk_overlap": chunk_overlap},
    ))

    # Registra nell'audit log
    await db.execute(
        text("""
            INSERT INTO audit_log (tenant_id, user_id, action, resource_type, resource_id, details, status)
            VALUES (:tenant_id, :user_id, 'document.upload', 'document', :resource_id,
                    :details::jsonb, 'success')
        """),
        {
            "tenant_id": str(user.tenant_id),
            "user_id": str(user.user_id),
            "resource_id": str(document_id),
            "details": json.dumps({
                "file_name": file.filename,
                "file_size": file_size,
                "mime_type": file.content_type,
                "chunking_strategy": chunking_strategy,
            }),
        },
    )
    await db.commit()

    logger.info(
        "document_uploaded",
        document_id=str(document_id),
        file_name=file.filename,
        file_size=file_size,
        tenant_id=str(user.tenant_id),
    )

    return {
        "status": "accepted",
        "message": "Documento accettato per ingestione. Usa /ingest/{id}/status per monitorare.",
        "document_id": str(document_id),
        "file_name": file.filename,
        "file_size_bytes": file_size,
    }


@router.get(
    "/{document_id}/status",
    summary="Stato ingestione documento",
    description="Controlla lo stato di processing di un documento.",
)
async def get_ingestion_status(
    document_id: UUID,
    user: AuthenticatedUser = Depends(set_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    """Restituisce lo stato corrente dell'ingestione di un documento."""

    result = await db.execute(
        text("""
            SELECT id, title, mime_type, file_size_bytes, current_version,
                   is_active, created_at, updated_at
            FROM documents
            WHERE id = :document_id AND tenant_id = :tenant_id
        """),
        {"document_id": str(document_id), "tenant_id": str(user.tenant_id)},
    )
    doc = result.first()

    if not doc:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Documento non trovato"})

    # Cerca l'ultima versione processata
    version_result = await db.execute(
        text("""
            SELECT version, chunk_count, embedding_model, processing_duration_ms, created_at
            FROM document_versions
            WHERE document_id = :document_id
            ORDER BY version DESC LIMIT 1
        """),
        {"document_id": str(document_id)},
    )
    version = version_result.first()

    return {
        "document_id": str(doc.id),
        "title": doc.title,
        "current_version": doc.current_version,
        "latest_processing": {
            "version": version.version if version else None,
            "chunk_count": version.chunk_count if version else 0,
            "embedding_model": version.embedding_model if version else None,
            "duration_ms": version.processing_duration_ms if version else None,
            "processed_at": str(version.created_at) if version else None,
        } if version else None,
        "created_at": str(doc.created_at),
        "updated_at": str(doc.updated_at),
    }