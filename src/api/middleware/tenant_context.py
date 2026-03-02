"""
CortexAI — Tenant Context Middleware
======================================
Setta il contesto tenant in PostgreSQL per ogni richiesta autenticata.

PERCHÉ QUESTO MIDDLEWARE È CRITICO:
Questo è il collegamento tra l'autenticazione (chi sei?) e il
Row-Level Security di PostgreSQL (quali dati puoi vedere?).

COME FUNZIONA:
1. L'auth middleware identifica il tenant_id dal JWT/API key
2. QUESTO middleware esegue: SET LOCAL app.current_tenant = 'tenant_uuid'
3. Da quel momento, OGNI query SQL nella stessa transazione vede SOLO
   i dati di quel tenant (grazie alle policy RLS)
4. "SET LOCAL" ha effetto solo nella transazione corrente — alla fine
   della richiesta, il contesto scompare automaticamente

ESEMPIO PRATICO:
  Senza questo middleware:
    SELECT * FROM documents → Restituisce TUTTI i documenti di TUTTI i tenant ⚠️

  Con questo middleware:
    SET LOCAL app.current_tenant = 'uuid-tenant-A';
    SELECT * FROM documents → Restituisce SOLO i documenti del tenant A ✅
"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from fastapi import Depends

from src.api.database import get_db
from src.api.middleware.auth import AuthenticatedUser, get_current_user


async def set_tenant_context(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AuthenticatedUser:
    """
    Dependency che setta il contesto tenant per RLS.

    Uso negli endpoint:
    ```python
    @router.get("/documents")
    async def list_docs(
        user: AuthenticatedUser = Depends(set_tenant_context),
        db: AsyncSession = Depends(get_db),
    ):
        # Qualsiasi query qui vedrà SOLO i dati del tenant dell'utente
        result = await db.execute(select(Document))
        return result.scalars().all()
    ```

    NOTA TECNICA:
    - "SET LOCAL" ha scope limitato alla transazione corrente
    - Se la connessione viene riutilizzata dal pool, il contesto NON persiste
    - Questo è il comportamento desiderato: ogni richiesta ha il suo contesto
    - Il secondo parametro "true" in current_setting('app.current_tenant', true)
      fa sì che se il contesto non è settato, restituisce NULL invece di errore
    """
    await db.execute(
        text("SELECT set_config('app.current_tenant', :tenant_id, true)"),
        {"tenant_id": str(user.tenant_id)}
    )

    return user
