"""
CortexAI — Pydantic Schemas (Request & Response Models)
========================================================
Definisce la struttura dei dati in entrata e in uscita dalle API.

PERCHÉ PYDANTIC:
1. Validazione automatica: se un campo è sbagliato, il client riceve
   un errore chiaro (422 Unprocessable Entity) PRIMA che il codice giri
2. Serializzazione: converte automaticamente UUID, datetime, etc. in JSON
3. Documentazione: FastAPI genera la docs OpenAPI da questi modelli
4. Type safety: l'IDE può aiutarti con autocomplete e type checking

CONVENZIONE NOMI:
- *Create  → corpo della richiesta POST (creazione)
- *Update  → corpo della richiesta PATCH (modifica parziale)
- *Response → corpo della risposta (cosa vede il client)
- *InDB    → rappresentazione interna (include campi che il client non vede)
"""

from pydantic import BaseModel, Field, EmailStr, ConfigDict
from datetime import datetime
from uuid import UUID
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# ENUMS — Stessi valori del database, ma in Python
# ---------------------------------------------------------------------------

class TenantTier(str, Enum):
    FREE = "free"
    BASIC = "basic"
    PRO = "pro"
    ENTERPRISE = "enterprise"


class UserRole(str, Enum):
    SUPER_ADMIN = "super_admin"
    TENANT_ADMIN = "tenant_admin"
    DATA_ENGINEER = "data_engineer"
    ANALYST = "analyst"
    AI_AGENT = "ai_agent"


class DataClassification(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class ChunkingStrategyEnum(str, Enum):
    FIXED_SIZE = "fixed_size"
    SEMANTIC = "semantic"
    RECURSIVE = "recursive"
    STRUCTURE = "structure"


# ---------------------------------------------------------------------------
# AUTH SCHEMAS — Login, Token, Registration
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    """Richiesta di login. Email + password in chiaro (su HTTPS!)."""
    email: EmailStr                       # Pydantic valida il formato email
    password: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description="Password in chiaro. Min 8 caratteri."
    )


class TokenResponse(BaseModel):
    """Risposta dopo login riuscito. Contiene JWT access e refresh token."""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"            # Standard OAuth2
    expires_in: int = Field(description="Secondi alla scadenza dell'access token")


class TokenPayload(BaseModel):
    """
    Contenuto del JWT token (payload decodificato).
    Questi dati sono FIRMATI ma NON criptati: chiunque può leggerli.
    Non mettere MAI dati sensibili nel JWT.
    """
    sub: UUID = Field(description="Subject: user_id")
    tenant_id: UUID
    role: UserRole
    permissions: list[str] = []
    exp: datetime = Field(description="Expiration time")
    iat: datetime = Field(description="Issued at time")
    jti: str = Field(description="JWT ID: identificativo unico del token")


class RefreshTokenRequest(BaseModel):
    """Richiesta di refresh del token di accesso."""
    refresh_token: str


# ---------------------------------------------------------------------------
# TENANT SCHEMAS
# ---------------------------------------------------------------------------

class TenantCreate(BaseModel):
    """Dati per creare un nuovo tenant."""
    name: str = Field(..., min_length=2, max_length=255)
    slug: str = Field(
        ...,
        min_length=2,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9-]*[a-z0-9]$",  # Solo lettere minuscole, numeri, trattini
        description="URL-friendly identifier. Solo a-z, 0-9 e trattini."
    )
    tier: TenantTier = TenantTier.FREE
    gdpr_data_region: str = Field(default="EU", max_length=10)


class TenantResponse(BaseModel):
    """Dati del tenant restituiti al client."""
    model_config = ConfigDict(from_attributes=True)
    # ^ from_attributes=True permette di creare il modello da un oggetto SQLAlchemy
    #   Es: TenantResponse.model_validate(tenant_db_object)

    id: UUID
    name: str
    slug: str
    tier: TenantTier
    gdpr_data_region: str
    max_documents: int
    max_queries_per_day: int
    daily_budget_usd: float
    is_active: bool
    created_at: datetime


class TenantUpdate(BaseModel):
    """Dati per aggiornare un tenant (tutti opzionali)."""
    name: Optional[str] = Field(None, min_length=2, max_length=255)
    tier: Optional[TenantTier] = None
    max_documents: Optional[int] = Field(None, ge=1)
    max_queries_per_day: Optional[int] = Field(None, ge=1)
    daily_budget_usd: Optional[float] = Field(None, ge=0)
    settings: Optional[dict] = None


# ---------------------------------------------------------------------------
# USER SCHEMAS
# ---------------------------------------------------------------------------

class UserCreate(BaseModel):
    """Dati per creare un nuovo utente."""
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    full_name: Optional[str] = Field(None, max_length=255)
    role: UserRole = UserRole.ANALYST     # Default: ruolo con meno permessi


class UserResponse(BaseModel):
    """Dati dell'utente restituiti al client. La password NON è mai inclusa."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    email: str                            # EmailStr non serve in output
    full_name: Optional[str]
    role: UserRole
    permissions: list[str]
    is_active: bool
    last_login: Optional[datetime]
    created_at: datetime


class UserUpdate(BaseModel):
    """Dati per aggiornare un utente."""
    full_name: Optional[str] = Field(None, max_length=255)
    role: Optional[UserRole] = None
    permissions: Optional[list[str]] = None
    is_active: Optional[bool] = None


# ---------------------------------------------------------------------------
# API KEY SCHEMAS
# ---------------------------------------------------------------------------

class APIKeyCreate(BaseModel):
    """Dati per creare una nuova API key."""
    name: str = Field(..., min_length=2, max_length=100)
    description: Optional[str] = None
    permissions: list[str] = Field(
        default=["documents:read", "queries:execute"],
        description="Permessi della chiave. Es: ['documents:read', 'queries:execute', 'mcp:invoke']"
    )
    rate_limit_rpm: int = Field(default=60, ge=1, le=1000)
    expires_in_days: Optional[int] = Field(
        None,
        ge=1,
        le=365,
        description="Scadenza in giorni. None = non scade mai."
    )


class APIKeyResponse(BaseModel):
    """
    Risposta alla creazione di una API key.
    ATTENZIONE: il campo 'key' contiene la chiave in chiaro.
    Viene mostrata UNA SOLA VOLTA alla creazione.
    Dopo, solo il prefix è disponibile per identificazione.
    """
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    key: Optional[str] = Field(
        None,
        description="Chiave in chiaro. Mostrata SOLO alla creazione. Salvatela subito!"
    )
    key_prefix: str
    permissions: list[str]
    rate_limit_rpm: int
    expires_at: Optional[datetime]
    created_at: datetime


# ---------------------------------------------------------------------------
# DOCUMENT SCHEMAS
# ---------------------------------------------------------------------------

class DocumentCreate(BaseModel):
    """Metadati per il caricamento di un nuovo documento."""
    title: str = Field(..., min_length=1, max_length=500)
    classification: DataClassification = DataClassification.INTERNAL
    chunking_strategy: ChunkingStrategyEnum = ChunkingStrategyEnum.RECURSIVE
    chunking_config: dict = Field(
        default={"chunk_size": 512, "chunk_overlap": 50},
        description="Parametri per la strategia di chunking scelta."
    )


class DocumentResponse(BaseModel):
    """Metadati del documento restituiti al client."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    title: str
    source_type: str
    mime_type: Optional[str]
    file_size_bytes: Optional[int]
    classification: DataClassification
    current_version: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# COMMON SCHEMAS
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    """Risposta dell'endpoint /health."""
    status: str = "healthy"
    service: str = "cortexai-api"
    version: str = "0.1.0"
    environment: str
    checks: dict = Field(
        default_factory=dict,
        description="Stato di ogni dipendenza: {'postgres': 'ok', 'redis': 'ok', ...}"
    )


class PaginatedResponse(BaseModel):
    """Wrapper per risposte paginate."""
    items: list                           # Lista di risultati
    total: int                            # Totale risultati (non solo questa pagina)
    page: int                             # Pagina corrente
    page_size: int                        # Risultati per pagina
    pages: int                            # Totale pagine


class ErrorResponse(BaseModel):
    """Formato standard per gli errori API."""
    error: str                            # Codice errore (es. "unauthorized", "not_found")
    message: str                          # Messaggio leggibile
    detail: Optional[dict] = None         # Dettagli aggiuntivi (opzionale)
    request_id: Optional[str] = None      # Per tracciare l'errore nei log
