"""
CortexAI — Authentication & Authorization Middleware
======================================================
Gestisce l'autenticazione (chi sei?) e l'autorizzazione (cosa puoi fare?).

DUAL-MODE AUTH:
Ogni richiesta può autenticarsi in due modi:
1. JWT Bearer Token  → Authorization: Bearer <token>  (per utenti umani)
2. API Key           → X-API-Key: <key>               (per agenti AI / integrazioni)

RBAC (Role-Based Access Control):
Ogni utente ha un ruolo. Ogni ruolo ha un set di permessi predefiniti.
I permessi extra possono essere aggiunti per-utente nel campo permissions.

GERARCHIA RUOLI:
  super_admin   → * (tutti i permessi)
  tenant_admin  → gestione utenti + documenti + analytics + GDPR
  data_engineer → documenti + pipeline + search + analytics
  analyst       → documenti (lettura) + search + analytics (lettura)
  ai_agent      → documenti (lettura) + search (molto limitato)

FLUSSO PER OGNI RICHIESTA:
  Request → Header Authorization/X-API-Key
    → get_current_user()
      → _authenticate_jwt() OPPURE _authenticate_api_key()
      → AuthenticatedUser (con ruolo + permessi)
    → Endpoint verifica permessi con user.has_permission("documents:write")
"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone
from uuid import UUID
import hashlib

from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
import structlog

from src.api.database import get_db
from src.api.security import decode_token

logger = structlog.get_logger("cortexai.auth")

# ---------------------------------------------------------------------------
# SCHEMA PERMESSI PER RUOLO
# ---------------------------------------------------------------------------
# Mappa ruolo → lista permessi.
# "*" = wildcard, concede TUTTI i permessi (solo super_admin).
# Formato permessi: "risorsa:azione" (es. "documents:write")
# ---------------------------------------------------------------------------

ROLE_PERMISSIONS: dict[str, list[str]] = {
    "super_admin": ["*"],
    "tenant_admin": [
        "users:read", "users:write", "users:delete",
        "documents:read", "documents:write", "documents:delete",
        "search:execute",
        "analytics:read", "analytics:write",
        "gdpr:manage",
        "api_keys:manage",
        "tenant:manage",
    ],
    "data_engineer": [
        "documents:read", "documents:write", "documents:delete",
        "search:execute",
        "analytics:read", "analytics:write",
        "pipeline:manage",
    ],
    "analyst": [
        "documents:read",
        "search:execute",
        "analytics:read",
    ],
    "ai_agent": [
        "documents:read",
        "search:execute",
    ],
}


def get_effective_permissions(role: str, extra_permissions: list[str] = None) -> list[str]:
    """
    Calcola i permessi effettivi: permessi del ruolo + permessi extra.

    Args:
        role: ruolo dell'utente (es. "data_engineer")
        extra_permissions: permessi aggiuntivi assegnati individualmente

    Returns:
        Lista di tutti i permessi effettivi (deduplicati)
    """
    base = ROLE_PERMISSIONS.get(role, [])
    extra = extra_permissions or []
    # set() per deduplicare, sorted() per consistenza
    return sorted(set(base + extra))


# ---------------------------------------------------------------------------
# AUTHENTICATED USER
# ---------------------------------------------------------------------------

@dataclass
class AuthenticatedUser:
    """
    Rappresenta un utente autenticato. Creato dal middleware auth
    e iniettato in ogni endpoint come dependency.

    Uso negli endpoint:
        async def my_endpoint(user: AuthenticatedUser = Depends(get_current_user)):
            if user.has_permission("documents:write"):
                ...
    """
    user_id: UUID
    tenant_id: UUID
    role: str
    permissions: list[str] = field(default_factory=list)
    email: Optional[str] = None
    full_name: Optional[str] = None
    auth_method: str = "jwt"  # "jwt" o "api_key"

    def has_permission(self, permission: str) -> bool:
        """
        Verifica se l'utente ha un permesso specifico.
        Il wildcard "*" concede tutti i permessi (super_admin).
        """
        return "*" in self.permissions or permission in self.permissions

    def has_any_permission(self, *permissions: str) -> bool:
        """True se l'utente ha ALMENO UNO dei permessi elencati."""
        if "*" in self.permissions:
            return True
        return any(p in self.permissions for p in permissions)

    def has_all_permissions(self, *permissions: str) -> bool:
        """True se l'utente ha TUTTI i permessi elencati."""
        if "*" in self.permissions:
            return True
        return all(p in self.permissions for p in permissions)


# ---------------------------------------------------------------------------
# SECURITY SCHEMES
# ---------------------------------------------------------------------------

# HTTPBearer: estrae il token dall'header "Authorization: Bearer <token>"
# auto_error=False: non lancia 403 automaticamente se manca, gestiamo noi
bearer_scheme = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# JWT AUTHENTICATION
# ---------------------------------------------------------------------------

async def _authenticate_jwt(
    credentials: HTTPAuthorizationCredentials,
    db: AsyncSession,
) -> AuthenticatedUser:
    """
    Autentica tramite JWT Bearer Token.

    Flusso:
    1. Decodifica il JWT (verifica firma + scadenza)
    2. Estrae user_id, tenant_id, role, permissions dal payload
    3. Verifica che il token sia di tipo "access" (non "refresh")
    4. Crea e restituisce AuthenticatedUser
    """
    try:
        payload = decode_token(credentials.credentials)
    except Exception as e:
        logger.warning("jwt_invalid", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_token", "message": "Token non valido o scaduto."},
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Verifica che sia un access token (non un refresh token)
    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "wrong_token_type", "message": "Usa un access token, non un refresh token."},
        )

    return AuthenticatedUser(
        user_id=UUID(payload["sub"]),
        tenant_id=UUID(payload["tenant_id"]),
        role=payload.get("role", "analyst"),
        permissions=payload.get("permissions", []),
        email=payload.get("email"),
        auth_method="jwt",
    )


# ---------------------------------------------------------------------------
# API KEY AUTHENTICATION
# ---------------------------------------------------------------------------

async def _authenticate_api_key(
    api_key: str,
    db: AsyncSession,
) -> AuthenticatedUser:
    """
    Autentica tramite API Key (header X-API-Key).

    Flusso:
    1. Calcola SHA-256 della key ricevuta
    2. Cerca nel DB una key con lo stesso hash
    3. Verifica: attiva, non scaduta, non cancellata
    4. Aggiorna last_used_at (per monitoraggio)
    5. Crea AuthenticatedUser con i permessi della key

    SICUREZZA:
    La API key in chiaro NON e mai salvata nel DB.
    Salviamo solo l'hash SHA-256. Quando riceviamo una key,
    calcoliamo l'hash e confrontiamo. Se il DB viene compromesso,
    le key originali non sono recuperabili.
    """
    # Calcola hash della key ricevuta
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    # Cerca nel DB
    result = await db.execute(
        text("""
            SELECT ak.id, ak.tenant_id, ak.user_id, ak.permissions,
                   ak.rate_limit_rpm, ak.expires_at, ak.is_active,
                   u.role, u.email, u.full_name
            FROM api_keys ak
            JOIN users u ON ak.user_id = u.id
            WHERE ak.key_hash = :key_hash
              AND ak.deleted_at IS NULL
        """),
        {"key_hash": key_hash}
    )
    row = result.first()

    if not row:
        logger.warning("api_key_not_found", prefix=api_key[:8] if len(api_key) > 8 else "***")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_api_key", "message": "API key non valida."},
        )

    # Verifica che sia attiva
    if not row.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "api_key_disabled", "message": "API key disattivata."},
        )

    # Verifica scadenza
    if row.expires_at and row.expires_at < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "api_key_expired", "message": "API key scaduta."},
        )

    # Aggiorna last_used_at (fire-and-forget, non blocca la risposta)
    await db.execute(
        text("UPDATE api_keys SET last_used_at = NOW() WHERE id = :id"),
        {"id": row.id}
    )

    # Permessi: usa quelli della key se definiti, altrimenti quelli del ruolo
    key_permissions = row.permissions if isinstance(row.permissions, list) else []
    if key_permissions:
        permissions = key_permissions
    else:
        permissions = get_effective_permissions(row.role)

    return AuthenticatedUser(
        user_id=row.user_id,
        tenant_id=row.tenant_id,
        role=row.role,
        permissions=permissions,
        email=row.email,
        full_name=row.full_name,
        auth_method="api_key",
    )


# ---------------------------------------------------------------------------
# MAIN DEPENDENCY: get_current_user
# ---------------------------------------------------------------------------

async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> AuthenticatedUser:
    """
    Dependency principale — da usare in TUTTI gli endpoint protetti.

    Prova in ordine:
    1. JWT Bearer Token (header Authorization)
    2. API Key (header X-API-Key)
    3. Se nessuno → 401 Unauthorized

    Uso:
        @router.get("/protected")
        async def endpoint(user: AuthenticatedUser = Depends(get_current_user)):
            print(user.tenant_id, user.role)
    """

    # Tentativo 1: JWT Bearer Token
    if credentials:
        return await _authenticate_jwt(credentials, db)

    # Tentativo 2: API Key
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return await _authenticate_api_key(api_key, db)

    # Nessuna credenziale fornita
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "error": "not_authenticated",
            "message": "Fornisci un Bearer token o una API key nell'header X-API-Key.",
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


# ---------------------------------------------------------------------------
# SHORTCUT DEPENDENCIES (per endpoint con requisiti specifici)
# ---------------------------------------------------------------------------

async def require_admin(
    user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    """Richiede ruolo super_admin o tenant_admin."""
    if user.role not in ("super_admin", "tenant_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "admin_required", "message": "Questa azione richiede privilegi admin."},
        )
    return user


async def require_data_engineer(
    user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    """Richiede almeno il ruolo data_engineer (o superiore)."""
    allowed = ("super_admin", "tenant_admin", "data_engineer")
    if user.role not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "insufficient_role", "message": "Richiesto ruolo data_engineer o superiore."},
        )
    return user