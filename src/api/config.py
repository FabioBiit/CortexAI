"""
CortexAI — Configurazione Applicazione
========================================
Carica tutte le impostazioni dalle variabili d'ambiente (file .env).

COME FUNZIONA:
Pydantic BaseSettings legge automaticamente le variabili d'ambiente.
Se una variabile non è settata, usa il valore di default.
Se una variabile è obbligatoria (senza default), l'app non parte.

Questo pattern garantisce che:
1. La configurazione è validata PRIMA che l'app parta
2. I tipi sono corretti (non rischi "60" string invece di 60 int)
3. C'è un unico punto dove trovare TUTTE le impostazioni
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from functools import lru_cache


class Settings(BaseSettings):
    """
    Impostazioni dell'applicazione.
    Ogni campo corrisponde a una variabile d'ambiente.
    Il nome della variabile è il nome del campo IN MAIUSCOLO.
    Esempio: database_url → DATABASE_URL
    """

    # Indica a Pydantic di leggere dal file .env
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,  # DATABASE_URL e database_url sono equivalenti
    )

    # --- App ---
    app_name: str = "CortexAI"
    app_env: str = Field(default="development", description="development | staging | production")
    log_level: str = Field(default="INFO", description="DEBUG | INFO | WARNING | ERROR")
    secret_key: str = Field(..., description="Chiave segreta per firmare JWT. OBBLIGATORIA.")
    # ^ Il "..." significa: NESSUN default, l'app NON parte senza questa variabile.

    # --- Database ---
    database_url: str = Field(
        ...,
        description="URL di connessione PostgreSQL async. Formato: postgresql+asyncpg://user:pass@host:port/db"
    )

    # --- Redis ---
    redis_url: str = Field(
        default="redis://redis-stack:6379/0",
        description="URL di connessione Redis"
    )

    # --- Qdrant ---
    qdrant_host: str = Field(default="qdrant", description="Hostname del server Qdrant")
    qdrant_port: int = Field(default=6333, description="Porta REST di Qdrant")

    # --- RabbitMQ ---
    rabbitmq_url: str = Field(
        default="amqp://cortexai:guest@rabbitmq:5672/cortexai",
        description="URL di connessione RabbitMQ (protocollo AMQP)"
    )

    # --- LLM Providers ---
    anthropic_api_key: str = Field(default="", description="API key Anthropic (Claude)")
    openai_api_key: str = Field(default="", description="API key OpenAI")
    ollama_base_url: str = Field(default="http://ollama:11434", description="URL base Ollama")

    # --- Modelli Default ---
    default_llm_provider: str = Field(default="anthropic")
    default_llm_model: str = Field(default="claude-sonnet-4-20250514")
    default_embedding_provider: str = Field(default="openai")
    default_embedding_model: str = Field(default="text-embedding-3-small")
    ollama_embedding_model: str = Field(default="nomic-embed-text")

    # --- JWT ---
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = Field(
        default=60,
        description="Durata token di accesso in minuti. Dopo, serve un refresh."
    )
    jwt_refresh_token_expire_days: int = Field(
        default=7,
        description="Durata refresh token in giorni."
    )

    # --- Rate Limiting ---
    rate_limit_per_minute: int = Field(default=60, description="Richieste/minuto per API key")
    rate_limit_burst: int = Field(default=20, description="Burst massimo consentito")

    # --- Cost Control ---
    default_daily_budget_usd: float = Field(default=5.00)
    cost_alert_threshold_pct: int = Field(default=80)

    # --- Observability ---
    prometheus_enabled: bool = Field(default=True)

    @property
    def is_development(self) -> bool:
        """Siamo in ambiente di sviluppo?"""
        return self.app_env == "development"

    @property
    def is_production(self) -> bool:
        """Siamo in produzione?"""
        return self.app_env == "production"


@lru_cache()
def get_settings() -> Settings:
    """
    Restituisce l'istanza delle impostazioni.

    @lru_cache() garantisce che le impostazioni vengano caricate UNA SOLA VOLTA
    e poi riutilizzate per tutte le richieste successive.
    Senza cache, ogni richiesta rileggerebbe il file .env dal disco.
    """
    return Settings()
