"""
CortexAI — Database Connection Layer
======================================
Gestisce la connessione asincrona a PostgreSQL tramite SQLAlchemy.

ARCHITETTURA CONNESSIONI:
┌──────────────┐     ┌──────────────────────┐     ┌──────────────┐
│  FastAPI     │────▶│  Connection Pool     │────▶│  PostgreSQL  │
│  Request     │     │  (20 conn, async)    │     │              │
└──────────────┘     └──────────────────────┘     └──────────────┘

PERCHÉ UN CONNECTION POOL:
Aprire una connessione DB per ogni richiesta è costoso (~50ms).
Il pool mantiene N connessioni già aperte e le riutilizza.
Con 20 connessioni nel pool, 20 richieste simultanee vengono servite
senza alcuna attesa di connessione.

PERCHÉ ASYNC:
FastAPI è asincrono. Se usassimo un driver sincrono (psycopg2),
ogni query bloccherebbe l'intero event loop.
asyncpg è scritto in C/Cython ed è il driver PostgreSQL più veloce
per Python: ~3x più veloce di psycopg2 per query semplici.
"""

from sqlalchemy.ext.asyncio import (
    create_async_engine,
    async_sessionmaker,
    AsyncSession,
    AsyncEngine,
)
from sqlalchemy.orm import DeclarativeBase
from typing import AsyncGenerator

from src.api.config import get_settings


class Base(DeclarativeBase):
    """
    Classe base per tutti i modelli SQLAlchemy.
    Tutti i modelli ORM ereditano da questa classe.
    """
    pass


# ---------------------------------------------------------------------------
# Engine e Session Factory
# ---------------------------------------------------------------------------
# L'engine è il punto di connessione al database.
# Il session_factory crea sessioni (unità di lavoro) per ogni richiesta.
# ---------------------------------------------------------------------------

def create_engine() -> AsyncEngine:
    """
    Crea l'engine asincrono con connection pool configurato.

    Parametri importanti:
    - pool_size=20: mantiene 20 connessioni attive nel pool
    - max_overflow=10: permette fino a 10 connessioni extra sotto carico
    - pool_pre_ping=True: verifica che la connessione sia viva prima di usarla
      (evita errori "connection reset" dopo timeout del DB)
    - pool_recycle=3600: ricicla connessioni ogni ora
      (evita che connessioni stale rimangano nel pool)
    """
    settings = get_settings()

    engine = create_async_engine(
        settings.database_url,
        pool_size=20,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=3600,
        echo=settings.is_development,  # Log SQL queries solo in dev
    )
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """
    Crea la factory per le sessioni async.

    - expire_on_commit=False: dopo un commit, gli oggetti rimangono
      accessibili senza generare nuove query al DB.
      Senza questo flag, accedere a user.email dopo un commit
      genererebbe una nuova SELECT (inefficiente).
    """
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


# Istanze globali (inizializzate al boot dell'app)
engine = create_engine()
SessionFactory = create_session_factory(engine)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency injection per FastAPI: fornisce una sessione DB per ogni richiesta.

    COME FUNZIONA:
    1. All'inizio della richiesta: crea una nuova sessione dal pool
    2. La sessione viene usata dall'endpoint per query/insert/update
    3. Alla fine della richiesta (anche in caso di errore): chiude la sessione
       e la restituisce al pool

    ESEMPIO UTILIZZO:
    ```python
    @router.get("/users")
    async def get_users(db: AsyncSession = Depends(get_db)):
        result = await db.execute(select(User))
        return result.scalars().all()
    ```

    Il pattern "yield" garantisce che la sessione venga SEMPRE chiusa,
    anche se l'endpoint lancia un'eccezione. È l'equivalente di try/finally.
    """
    async with SessionFactory() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db() -> None:
    """
    Inizializza il database al boot dell'applicazione.
    In produzione, le migrazioni sono gestite da Alembic.
    """
    # Verifica connessione
    async with engine.begin() as conn:
        # Semplice query di verifica
        await conn.execute(
            # sqlalchemy.text() è il modo sicuro per eseguire SQL raw
            __import__("sqlalchemy").text("SELECT 1")
        )


async def close_db() -> None:
    """Chiude il pool di connessioni al shutdown dell'app."""
    await engine.dispose()
