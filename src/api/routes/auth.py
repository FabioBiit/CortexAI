"""
CortexAI — Auth Routes
========================
Endpoint per autenticazione: login, registrazione, refresh token.

FLUSSO DI AUTENTICAZIONE COMPLETO:
1. POST /auth/login    → email + password → access_token + refresh_token
2. GET  /auth/me       → (con access_token) → dati utente corrente
3. POST /auth/refresh  → refresh_token → nuovo access_token
4. POST /auth/register → (admin only) → crea nuovo utente nel tenant
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from datetime import datetime, timezone

from src.api.database import get_db
from src.api.security import (
    verify_password,
    hash_password,
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_api_key,
)
from src.api.middleware.auth import (
    get_current_user,
    require_admin,
    AuthenticatedUser,
    get_effective_permissions,
)
from src.api.middleware.tenant_context import set_tenant_context
from src.api.schemas import (
    LoginRequest,
    TokenResponse,
    RefreshTokenRequest,
    UserCreate,
    UserResponse,
    APIKeyCreate,
    APIKeyResponse,
    ErrorResponse,
)


router = APIRouter(prefix="/auth", tags=["Authentication"])


# ---------------------------------------------------------------------------
# LOGIN
# ---------------------------------------------------------------------------

@router.post(
    "/login",
    response_model=TokenResponse,
    responses={401: {"model": ErrorResponse}},
    summary="Login con email e password",
    description="Autentica l'utente e restituisce JWT access + refresh token.",
)
async def login(
    request: LoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Flusso:
    1. Cerca l'utente per email (in TUTTI i tenant — l'email è unica per tenant)
    2. Verifica la password con bcrypt
    3. Genera JWT access token (vita breve) + refresh token (vita lunga)
    4. Aggiorna last_login
    5. Restituisce i token
    """

    # Cerca l'utente per email
    # NOTA: questa query NON passa per RLS perché non abbiamo ancora
    # settato il contesto tenant (non sappiamo a quale tenant appartiene)
    result = await db.execute(
        text("""
            SELECT u.id, u.tenant_id, u.email, u.hashed_password,
                   u.role, u.permissions, u.is_active, u.full_name,
                   t.is_active as tenant_active
            FROM users u
            JOIN tenants t ON u.tenant_id = t.id
            WHERE u.email = :email
              AND u.deleted_at IS NULL
        """),
        {"email": request.email}
    )
    user = result.first()

    # Utente non trovato — messaggio generico per sicurezza
    # (non rivelare se l'email esiste o no)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_credentials", "message": "Email o password non corretti."},
        )

    # Verifica password
    if not verify_password(request.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_credentials", "message": "Email o password non corretti."},
        )

    # Verifica che utente e tenant siano attivi
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "user_disabled", "message": "Account disattivato. Contatta l'admin."},
        )

    if not user.tenant_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "tenant_disabled", "message": "Organizzazione disattivata."},
        )

    # Calcola permessi effettivi
    extra_perms = user.permissions if isinstance(user.permissions, list) else []
    permissions = get_effective_permissions(user.role, extra_perms)

    # Genera token
    access_token = create_access_token(
        user_id=str(user.id),
        tenant_id=str(user.tenant_id),
        role=user.role,
        permissions=permissions,
    )
    refresh_token = create_refresh_token(
        user_id=str(user.id),
        tenant_id=str(user.tenant_id),
    )

    # Aggiorna last_login
    await db.execute(
        text("UPDATE users SET last_login = NOW() WHERE id = :id"),
        {"id": user.id}
    )
    await db.commit()

    from src.api.config import get_settings
    settings = get_settings()

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.jwt_access_token_expire_minutes * 60,  # In secondi
    )


# ---------------------------------------------------------------------------
# ME — Profilo utente corrente
# ---------------------------------------------------------------------------

@router.get(
    "/me",
    response_model=UserResponse,
    summary="Profilo utente corrente",
    description="Restituisce i dati dell'utente autenticato.",
)
async def get_me(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Restituisce i dati dell'utente corrente dal database."""

    result = await db.execute(
        text("""
            SELECT id, tenant_id, email, full_name, role, permissions,
                   is_active, last_login, created_at
            FROM users WHERE id = :id
        """),
        {"id": str(user.user_id)}
    )
    row = result.first()

    if not row:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Utente non trovato."})

    return UserResponse(
        id=row.id,
        tenant_id=row.tenant_id,
        email=row.email,
        full_name=row.full_name,
        role=row.role,
        permissions=row.permissions if isinstance(row.permissions, list) else [],
        is_active=row.is_active,
        last_login=row.last_login,
        created_at=row.created_at,
    )


# ---------------------------------------------------------------------------
# REFRESH TOKEN
# ---------------------------------------------------------------------------

@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Rinnova access token",
    description="Usa il refresh token per ottenere un nuovo access token.",
)
async def refresh_token(
    request: RefreshTokenRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Quando l'access token scade (dopo 60 minuti), il client usa il
    refresh token per ottenerne uno nuovo SENZA dover ri-inserire
    email e password.
    """
    try:
        payload = decode_token(request.refresh_token)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_refresh_token", "message": "Refresh token non valido o scaduto."},
        )

    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "wrong_token_type", "message": "Token fornito non è un refresh token."},
        )

    # Recupera i dati aggiornati dell'utente dal DB
    result = await db.execute(
        text("""
            SELECT u.id, u.tenant_id, u.role, u.permissions, u.is_active
            FROM users u
            WHERE u.id = :user_id AND u.deleted_at IS NULL
        """),
        {"user_id": payload["sub"]}
    )
    user = result.first()

    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "user_not_found", "message": "Utente non trovato o disattivato."},
        )

    extra_perms = user.permissions if isinstance(user.permissions, list) else []
    permissions = get_effective_permissions(user.role, extra_perms)

    access_token = create_access_token(
        user_id=str(user.id),
        tenant_id=str(user.tenant_id),
        role=user.role,
        permissions=permissions,
    )

    new_refresh = create_refresh_token(
        user_id=str(user.id),
        tenant_id=str(user.tenant_id),
    )

    from src.api.config import get_settings
    settings = get_settings()

    return TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh,
        expires_in=settings.jwt_access_token_expire_minutes * 60,
    )


# ---------------------------------------------------------------------------
# REGISTER USER (Admin only)
# ---------------------------------------------------------------------------

@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Registra nuovo utente (admin only)",
    description="Crea un nuovo utente nel tenant corrente. Richiede ruolo admin.",
)
async def register_user(
    request: UserCreate,
    admin: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Solo gli admin del tenant possono creare nuovi utenti."""

    # Verifica che l'email non sia già in uso nel tenant
    existing = await db.execute(
        text("""
            SELECT id FROM users
            WHERE tenant_id = :tenant_id AND email = :email AND deleted_at IS NULL
        """),
        {"tenant_id": str(admin.tenant_id), "email": request.email}
    )
    if existing.first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "email_exists", "message": "Email già registrata in questo tenant."},
        )

    # Crea l'utente
    hashed = hash_password(request.password)
    result = await db.execute(
        text("""
            INSERT INTO users (tenant_id, email, hashed_password, role, full_name)
            VALUES (:tenant_id, :email, :hashed_password, :role, :full_name)
            RETURNING id, tenant_id, email, full_name, role, permissions, is_active, created_at
        """),
        {
            "tenant_id": str(admin.tenant_id),
            "email": request.email,
            "hashed_password": hashed,
            "role": request.role.value,
            "full_name": request.full_name,
        }
    )
    row = result.first()
    await db.commit()

    return UserResponse(
        id=row.id,
        tenant_id=row.tenant_id,
        email=row.email,
        full_name=row.full_name,
        role=row.role,
        permissions=row.permissions if isinstance(row.permissions, list) else [],
        is_active=row.is_active,
        last_login=None,
        created_at=row.created_at,
    )


# ---------------------------------------------------------------------------
# API KEY MANAGEMENT
# ---------------------------------------------------------------------------

@router.post(
    "/api-keys",
    response_model=APIKeyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crea API key (admin only)",
    description="Genera una nuova API key per accesso programmatico. La key è mostrata UNA SOLA VOLTA.",
)
async def create_api_key(
    request: APIKeyCreate,
    admin: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Crea una API key per agenti AI o integrazioni.
    ATTENZIONE: la key completa è mostrata SOLO in questa risposta.
    Non c'è modo di recuperarla dopo.
    """

    key_full, key_prefix, key_hash = generate_api_key()

    # Calcola scadenza
    expires_at = None
    if request.expires_in_days:
        from datetime import timedelta
        expires_at = datetime.now(timezone.utc) + timedelta(days=request.expires_in_days)

    result = await db.execute(
        text("""
            INSERT INTO api_keys (tenant_id, user_id, key_prefix, key_hash, name,
                                  description, permissions, rate_limit_rpm, expires_at)
            VALUES (:tenant_id, :user_id, :key_prefix, :key_hash, :name,
                    :description, :permissions::jsonb, :rate_limit_rpm, :expires_at)
            RETURNING id, name, key_prefix, permissions, rate_limit_rpm, expires_at, created_at
        """),
        {
            "tenant_id": str(admin.tenant_id),
            "user_id": str(admin.user_id),
            "key_prefix": key_prefix,
            "key_hash": key_hash,
            "name": request.name,
            "description": request.description,
            "permissions": __import__("json").dumps(request.permissions),
            "rate_limit_rpm": request.rate_limit_rpm,
            "expires_at": expires_at,
        }
    )
    row = result.first()
    await db.commit()

    return APIKeyResponse(
        id=row.id,
        name=row.name,
        key=key_full,  # ⚠️ Mostrata UNA SOLA VOLTA!
        key_prefix=row.key_prefix,
        permissions=row.permissions if isinstance(row.permissions, list) else [],
        rate_limit_rpm=row.rate_limit_rpm,
        expires_at=row.expires_at,
        created_at=row.created_at,
    )
