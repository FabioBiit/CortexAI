-- ============================================================================
-- CortexAI — PostgreSQL Schema Initialization
-- ============================================================================
-- Questo file viene eseguito AUTOMATICAMENTE al primo avvio del container
-- PostgreSQL (montato in /docker-entrypoint-initdb.d/).
--
-- COSA CREA:
-- 1. Estensioni necessarie (UUID, crypto)
-- 2. Tabelle core (tenants, users, api_keys)
-- 3. Tabelle documenti (documents, document_versions, data_lineage)
-- 4. Tabelle operazionali (audit_log, cost_tracking, gdpr_requests)
-- 5. Row-Level Security (RLS) per isolamento multi-tenant
-- 6. Indici per performance
-- 7. Dati iniziali (tenant demo + super admin)
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. ESTENSIONI
-- ---------------------------------------------------------------------------
-- pgcrypto: genera UUID v4 e hash sicuri (per password e API key)
-- pg_trgm: ricerca fuzzy su testo (utile per search autocomplete)
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- ---------------------------------------------------------------------------
-- 2. ENUM TYPES
-- ---------------------------------------------------------------------------
-- I tipi ENUM garantiscono che i valori siano sempre validi a livello DB.
-- Se il codice applicativo ha un bug e prova a inserire un ruolo inesistente,
-- PostgreSQL rifiuta l'operazione. Sicurezza a livello di dato.
-- ---------------------------------------------------------------------------

-- Tier del tenant: determina limiti, funzionalità e rate limiting
CREATE TYPE tenant_tier AS ENUM ('free', 'basic', 'pro', 'enterprise');

-- Ruoli utente: gerarchia di permessi
-- super_admin: accesso completo alla piattaforma (solo Anthropic/operatori)
-- tenant_admin: gestisce il proprio tenant (utenti, config, billing)
-- data_engineer: carica e gestisce documenti, configura pipeline
-- analyst: può solo interrogare i documenti (read-only)
-- ai_agent: accesso programmato via API con permessi limitati
CREATE TYPE user_role AS ENUM ('super_admin', 'tenant_admin', 'data_engineer', 'analyst', 'ai_agent');

-- Classificazione di sensibilità dei documenti (stile Purview)
CREATE TYPE data_classification AS ENUM ('public', 'internal', 'confidential', 'restricted');

-- Strategia di chunking usata per processare un documento
CREATE TYPE chunking_strategy AS ENUM ('fixed_size', 'semantic', 'recursive', 'structure');

-- Stato di una richiesta GDPR
CREATE TYPE gdpr_request_status AS ENUM ('pending', 'processing', 'completed', 'failed');
CREATE TYPE gdpr_request_type AS ENUM ('access', 'erasure', 'portability', 'rectification');

-- ---------------------------------------------------------------------------
-- 3. TABELLE CORE
-- ---------------------------------------------------------------------------

-- TENANTS — Le organizzazioni che usano la piattaforma
-- Ogni tenant è completamente isolato dagli altri (dati, config, limiti).
CREATE TABLE tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Identificativi
    name VARCHAR(255) NOT NULL,                    -- Nome visualizzato (es. "Acme Corp")
    slug VARCHAR(100) UNIQUE NOT NULL,             -- URL-friendly, unico (es. "acme-corp")

    -- Configurazione
    tier tenant_tier NOT NULL DEFAULT 'free',      -- Determina limiti e funzionalità
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,   -- Config flessibile (max_docs, chunking default, etc.)
    -- Esempio settings: {"max_documents": 1000, "default_chunking": "recursive", "default_llm": "anthropic"}

    -- GDPR
    gdpr_data_region VARCHAR(10) DEFAULT 'EU',     -- Regione di residenza dati (EU, US, etc.)

    -- Limiti (derivati dal tier, ma override-abili per tenant)
    max_documents INTEGER DEFAULT 100,             -- Max documenti indicizzabili
    max_queries_per_day INTEGER DEFAULT 1000,      -- Max query al giorno
    daily_budget_usd NUMERIC(10,2) DEFAULT 5.00,  -- Budget giornaliero LLM

    -- Lifecycle
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ                         -- Soft delete (GDPR: non cancelliamo subito)
);

-- Commento sulla tabella (documentazione inline nel DB)
COMMENT ON TABLE tenants IS 'Organizzazioni che usano la piattaforma. Ogni tenant è isolato.';
COMMENT ON COLUMN tenants.slug IS 'Identificativo URL-safe unico, usato nei path API e nei namespace Redis/Qdrant.';
COMMENT ON COLUMN tenants.settings IS 'Configurazione flessibile in JSONB: override per chunking, LLM, embedding model, etc.';
COMMENT ON COLUMN tenants.deleted_at IS 'Soft delete per GDPR: il record rimane ma è marcato come cancellato.';


-- USERS — Gli utenti della piattaforma
-- Ogni utente appartiene a UN tenant e ha UN ruolo.
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Relazione con tenant (ogni utente appartiene a un tenant)
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- Autenticazione
    email VARCHAR(255) NOT NULL,
    hashed_password VARCHAR(255) NOT NULL,          -- bcrypt hash (MAI il plaintext)

    -- Autorizzazione
    role user_role NOT NULL DEFAULT 'analyst',
    -- Permessi granulari aggiuntivi (override del ruolo base)
    -- Esempio: ["documents:write", "queries:export"]
    permissions JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- Profilo
    full_name VARCHAR(255),

    -- Lifecycle
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    last_login TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,                        -- Soft delete per GDPR

    -- Vincolo: email unica PER tenant (non globalmente)
    -- Due tenant diversi possono avere lo stesso utente email
    UNIQUE(tenant_id, email)
);

COMMENT ON TABLE users IS 'Utenti della piattaforma. Ogni utente appartiene a un tenant con un ruolo.';
COMMENT ON COLUMN users.hashed_password IS 'Hash bcrypt della password. Il plaintext non viene MAI memorizzato.';
COMMENT ON COLUMN users.permissions IS 'Permessi granulari aggiuntivi in formato JSON array. Sovrascrivono/estendono il ruolo base.';


-- API_KEYS — Chiavi API per accesso programmatico
-- Usate dagli agenti AI e dalle integrazioni esterne.
-- Ogni chiave ha permessi specifici e un rate limit dedicato.
CREATE TABLE api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Relazione
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,  -- Chi l'ha creata (opzionale)

    -- Autenticazione
    key_prefix VARCHAR(8) NOT NULL,                -- Primi 8 char della key (per identificazione in log)
    key_hash VARCHAR(255) NOT NULL,                -- SHA-256 dell'intera API key (MAI il plaintext)

    -- Metadata
    name VARCHAR(100) NOT NULL,                    -- Nome descrittivo (es. "MCP Agent Production")
    description TEXT,

    -- Autorizzazione
    permissions JSONB NOT NULL DEFAULT '[]'::jsonb, -- Permessi specifici di questa key
    -- Esempio: ["documents:read", "queries:execute", "mcp:invoke"]

    -- Rate limiting
    rate_limit_rpm INTEGER NOT NULL DEFAULT 60,     -- Richieste al minuto

    -- Lifecycle
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    expires_at TIMESTAMPTZ,                        -- Scadenza opzionale
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE api_keys IS 'Chiavi API per accesso programmatico. Il plaintext è mostrato SOLO alla creazione.';
COMMENT ON COLUMN api_keys.key_prefix IS 'Primi 8 caratteri della key, per identificazione nei log senza esporre il segreto.';
COMMENT ON COLUMN api_keys.key_hash IS 'SHA-256 della API key completa. Usato per autenticazione.';


-- ---------------------------------------------------------------------------
-- 4. TABELLE DOCUMENTI
-- ---------------------------------------------------------------------------

-- DOCUMENTS — Metadati dei documenti caricati
CREATE TABLE documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Relazione
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    uploaded_by UUID REFERENCES users(id) ON DELETE SET NULL,

    -- Metadata
    title VARCHAR(500) NOT NULL,
    source_type VARCHAR(50) NOT NULL DEFAULT 'upload',   -- upload, api, connector
    mime_type VARCHAR(100),                               -- application/pdf, text/csv, etc.
    file_size_bytes BIGINT,
    file_hash VARCHAR(64),                               -- SHA-256 del file originale

    -- Classificazione (stile Purview)
    classification data_classification NOT NULL DEFAULT 'internal',

    -- Versioning
    current_version INTEGER NOT NULL DEFAULT 1,

    -- Lifecycle
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

COMMENT ON TABLE documents IS 'Metadati dei documenti. Il contenuto effettivo è nei chunk (Qdrant) e nei file (storage).';


-- DOCUMENT_VERSIONS — Storico delle versioni di ogni documento
-- Ogni volta che un documento viene re-processato (chunking diverso,
-- embedding diverso), viene creata una nuova versione.
CREATE TABLE document_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Relazione
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,

    -- Versione
    version INTEGER NOT NULL,
    file_hash VARCHAR(64) NOT NULL,                     -- SHA-256 del contenuto di questa versione

    -- Processing info
    chunking_strategy chunking_strategy NOT NULL,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    chunk_config JSONB NOT NULL DEFAULT '{}'::jsonb,    -- Parametri chunking usati
    -- Esempio: {"chunk_size": 512, "overlap": 50, "min_size": 100}

    -- Embedding info
    embedding_model VARCHAR(100) NOT NULL,              -- Es. "text-embedding-3-small"
    embedding_dimension INTEGER NOT NULL,               -- Es. 1536
    qdrant_collection VARCHAR(255) NOT NULL,            -- Nome collection in Qdrant

    -- Metadata
    processing_duration_ms INTEGER,                     -- Quanto ci ha messo la pipeline
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Lifecycle
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Una sola versione N per documento
    UNIQUE(document_id, version)
);

COMMENT ON TABLE document_versions IS 'Storico versioni documenti. Ogni re-processing crea una nuova versione.';


-- DATA_LINEAGE — Traccia il percorso dei dati (stile Purview)
-- Registra ogni trasformazione: file originale → parsing → chunk → embedding → indice
CREATE TABLE data_lineage (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Relazione
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_version_id UUID REFERENCES document_versions(id) ON DELETE SET NULL,

    -- Tracciamento
    stage VARCHAR(50) NOT NULL,              -- ingestion, parsing, chunking, embedding, indexing
    processor VARCHAR(100) NOT NULL,         -- Nome del componente (es. "recursive_chunker_v1")
    input_hash VARCHAR(64),                  -- Hash dell'input a questo stage
    output_hash VARCHAR(64),                 -- Hash dell'output
    input_count INTEGER,                     -- Numero elementi in input
    output_count INTEGER,                    -- Numero elementi in output

    -- Performance
    duration_ms INTEGER,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Lifecycle
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE data_lineage IS 'Data lineage: traccia ogni trasformazione dei dati, dalla sorgente all indice.';


-- ---------------------------------------------------------------------------
-- 5. TABELLE OPERAZIONALI
-- ---------------------------------------------------------------------------

-- AUDIT_LOG — Log immutabile di tutte le azioni
-- NON ha soft delete: gli audit log sono PERMANENTI (requisito GDPR/compliance).
-- Dopo 90 giorni i campi PII vengono anonimizzati ma il log resta.
CREATE TABLE audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Chi
    tenant_id UUID NOT NULL,                        -- NO foreign key: deve sopravvivere alla cancellazione del tenant
    user_id UUID,                                   -- Chi ha eseguito l'azione (NULL = sistema)
    api_key_id UUID,                                -- Se l'azione è via API key

    -- Cosa
    action VARCHAR(100) NOT NULL,                   -- Es. "document.upload", "query.execute", "gdpr.erasure"
    resource_type VARCHAR(50),                       -- Es. "document", "user", "collection"
    resource_id UUID,                               -- ID della risorsa coinvolta

    -- Dettagli
    details JSONB NOT NULL DEFAULT '{}'::jsonb,     -- Dettagli specifici dell'azione
    ip_address INET,                                -- IP del client
    user_agent TEXT,                                -- User agent del client

    -- Risultato
    status VARCHAR(20) NOT NULL DEFAULT 'success',  -- success, failure, denied
    error_message TEXT,

    -- Quando
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Partitioning hint: in produzione, partizionare per mese su created_at
COMMENT ON TABLE audit_log IS 'Log immutabile di TUTTE le azioni. Non ha foreign key per sopravvivere a cancellazioni.';


-- COST_TRACKING — Tracciamento costi LLM per tenant
CREATE TABLE cost_tracking (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Chi
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id UUID,

    -- Cosa
    model VARCHAR(100) NOT NULL,                    -- Es. "anthropic/claude-sonnet-4-20250514"
    operation VARCHAR(50) NOT NULL,                 -- embedding, generation, evaluation, classification

    -- Costi
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd NUMERIC(10,6) NOT NULL DEFAULT 0,     -- 6 decimali per micro-costi embedding

    -- Metadata
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,    -- Dettagli aggiuntivi (query, chunk_count, etc.)

    -- Quando
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE cost_tracking IS 'Costi LLM per tenant. Usato per budget cap, alerting e analytics.';


-- GDPR_REQUESTS — Registro richieste GDPR
CREATE TABLE gdpr_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Chi
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    requested_by UUID REFERENCES users(id) ON DELETE SET NULL,
    subject_email VARCHAR(255) NOT NULL,            -- Email del soggetto dei dati

    -- Cosa
    request_type gdpr_request_type NOT NULL,
    status gdpr_request_status NOT NULL DEFAULT 'pending',

    -- Dettagli
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB,                                   -- Risultato dell'operazione

    -- Quando
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE gdpr_requests IS 'Registro formale delle richieste GDPR. Ogni richiesta viene tracciata fino al completamento.';


-- ---------------------------------------------------------------------------
-- 6. ROW-LEVEL SECURITY (RLS)
-- ---------------------------------------------------------------------------
-- RLS è il meccanismo PostgreSQL che filtra automaticamente le righe
-- in base al contesto della sessione. Ogni query vede SOLO i dati del
-- tenant corrente, ANCHE se il codice applicativo dimentica il filtro.
--
-- COME FUNZIONA:
-- 1. L'API Gateway, ad ogni richiesta, esegue:
--    SET LOCAL app.current_tenant = 'uuid-del-tenant';
-- 2. Le policy RLS controllano che tenant_id == current_setting('app.current_tenant')
-- 3. PostgreSQL filtra automaticamente TUTTE le query (SELECT, INSERT, UPDATE, DELETE)
--
-- SICUREZZA: Anche se il codice ha un bug e non filtra per tenant_id,
-- PostgreSQL lo fa comunque. Questo è il livello di sicurezza più alto
-- per il multi-tenant.
-- ---------------------------------------------------------------------------

-- Abilita RLS sulle tabelle multi-tenant
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_lineage ENABLE ROW LEVEL SECURITY;
ALTER TABLE cost_tracking ENABLE ROW LEVEL SECURITY;
ALTER TABLE gdpr_requests ENABLE ROW LEVEL SECURITY;

-- Policy per USERS: vedi solo gli utenti del tuo tenant
CREATE POLICY tenant_isolation_users ON users
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID);

-- Policy per API_KEYS
CREATE POLICY tenant_isolation_api_keys ON api_keys
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID);

-- Policy per DOCUMENTS
CREATE POLICY tenant_isolation_documents ON documents
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID);

-- Policy per DOCUMENT_VERSIONS (join con documents per il tenant_id)
CREATE POLICY tenant_isolation_doc_versions ON document_versions
    USING (document_id IN (
        SELECT id FROM documents
        WHERE tenant_id = current_setting('app.current_tenant', true)::UUID
    ));

-- Policy per DATA_LINEAGE
CREATE POLICY tenant_isolation_lineage ON data_lineage
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID);

-- Policy per COST_TRACKING
CREATE POLICY tenant_isolation_costs ON cost_tracking
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID);

-- Policy per GDPR_REQUESTS
CREATE POLICY tenant_isolation_gdpr ON gdpr_requests
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID);

-- NOTA: audit_log NON ha RLS perché i super_admin devono vedere tutto.
-- L'accesso è controllato a livello applicativo.

-- BYPASS RLS per il ruolo cortexai (usato per operazioni di sistema)
-- Il ruolo cortexai è il "superuser" del database e può vedere tutto.
-- In produzione, creeresti un ruolo separato con RLS per le connessioni app.
ALTER TABLE users FORCE ROW LEVEL SECURITY;
ALTER TABLE api_keys FORCE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;
ALTER TABLE document_versions FORCE ROW LEVEL SECURITY;
ALTER TABLE data_lineage FORCE ROW LEVEL SECURITY;
ALTER TABLE cost_tracking FORCE ROW LEVEL SECURITY;
ALTER TABLE gdpr_requests FORCE ROW LEVEL SECURITY;

-- Il ruolo owner bypassa RLS di default. Per forzare RLS anche sull'owner,
-- usiamo FORCE ROW LEVEL SECURITY sopra. Ma per le migrazioni e il seed,
-- abbiamo bisogno di un bypass. Creiamo una funzione helper:
CREATE OR REPLACE FUNCTION set_tenant_context(p_tenant_id UUID)
RETURNS void AS $$
BEGIN
    PERFORM set_config('app.current_tenant', p_tenant_id::TEXT, true);
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION set_tenant_context IS 'Setta il contesto tenant per RLS. Chiamata ad ogni richiesta API.';


-- ---------------------------------------------------------------------------
-- 7. INDICI
-- ---------------------------------------------------------------------------
-- Gli indici velocizzano le query più frequenti. Senza indici, PostgreSQL
-- deve scansionare TUTTA la tabella per ogni query (full table scan).
-- Con gli indici, trova i risultati in millisecondi.
-- ---------------------------------------------------------------------------

-- Tenants
CREATE INDEX idx_tenants_slug ON tenants(slug);
CREATE INDEX idx_tenants_active ON tenants(is_active) WHERE is_active = TRUE;

-- Users
CREATE INDEX idx_users_tenant ON users(tenant_id);
CREATE INDEX idx_users_email ON users(email);                -- Per login (ricerca per email)
CREATE INDEX idx_users_role ON users(tenant_id, role);       -- Per filtrare utenti per ruolo
CREATE INDEX idx_users_active ON users(tenant_id, is_active) WHERE is_active = TRUE;

-- API Keys
CREATE INDEX idx_api_keys_tenant ON api_keys(tenant_id);
CREATE INDEX idx_api_keys_hash ON api_keys(key_hash);        -- Per autenticazione (ricerca per hash)
CREATE INDEX idx_api_keys_active ON api_keys(is_active, expires_at)
    WHERE is_active = TRUE;

-- Documents
CREATE INDEX idx_documents_tenant ON documents(tenant_id);
CREATE INDEX idx_documents_classification ON documents(tenant_id, classification);
CREATE INDEX idx_documents_uploaded_by ON documents(uploaded_by);
CREATE INDEX idx_documents_active ON documents(tenant_id, is_active) WHERE is_active = TRUE;

-- Document Versions
CREATE INDEX idx_doc_versions_document ON document_versions(document_id);
CREATE INDEX idx_doc_versions_created ON document_versions(created_at DESC);

-- Data Lineage
CREATE INDEX idx_lineage_tenant ON data_lineage(tenant_id);
CREATE INDEX idx_lineage_doc_version ON data_lineage(document_version_id);
CREATE INDEX idx_lineage_stage ON data_lineage(tenant_id, stage);

-- Audit Log (tabella più grande, indici critici)
CREATE INDEX idx_audit_tenant ON audit_log(tenant_id);
CREATE INDEX idx_audit_action ON audit_log(action);
CREATE INDEX idx_audit_created ON audit_log(created_at DESC);  -- Per query temporali
CREATE INDEX idx_audit_resource ON audit_log(resource_type, resource_id);
-- Indice parziale: solo gli ultimi 90 giorni (i più interrogati)
CREATE INDEX idx_audit_recent ON audit_log(tenant_id, created_at DESC)
    WHERE created_at > NOW() - INTERVAL '90 days';

-- Cost Tracking
CREATE INDEX idx_costs_tenant ON cost_tracking(tenant_id);
CREATE INDEX idx_costs_tenant_date ON cost_tracking(tenant_id, created_at DESC);
CREATE INDEX idx_costs_model ON cost_tracking(tenant_id, model);

-- GDPR Requests
CREATE INDEX idx_gdpr_tenant ON gdpr_requests(tenant_id);
CREATE INDEX idx_gdpr_status ON gdpr_requests(status) WHERE status != 'completed';


-- ---------------------------------------------------------------------------
-- 8. TRIGGER — Updated_at automatico
-- ---------------------------------------------------------------------------
-- Aggiorna automaticamente la colonna updated_at quando una riga viene modificata.
-- Senza questo, dovresti farlo manualmente in ogni query UPDATE.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_tenants_updated
    BEFORE UPDATE ON tenants
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_users_updated
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_documents_updated
    BEFORE UPDATE ON documents
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_gdpr_updated
    BEFORE UPDATE ON gdpr_requests
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();


-- ---------------------------------------------------------------------------
-- 9. DATI INIZIALI (Seed)
-- ---------------------------------------------------------------------------
-- Crea un tenant demo e un super admin per poter iniziare subito.
-- Le password sono hash bcrypt. Il plaintext è nei commenti SOLO per dev.
-- ---------------------------------------------------------------------------

-- Tenant demo
INSERT INTO tenants (id, name, slug, tier, settings, max_documents, max_queries_per_day, daily_budget_usd)
VALUES (
    '00000000-0000-0000-0000-000000000001',
    'CortexAI Demo',
    'demo',
    'pro',
    '{"default_chunking": "recursive", "default_llm": "anthropic", "default_embedding": "text-embedding-3-small"}'::jsonb,
    1000,
    10000,
    10.00
);

-- Super admin per il tenant demo
-- Password: "cortexai_admin_2025" (hash bcrypt sotto)
-- NOTA: in produzione, questo utente andrebbe creato via CLI sicura, non via SQL
INSERT INTO users (id, tenant_id, email, hashed_password, role, full_name)
VALUES (
    '00000000-0000-0000-0000-000000000002',
    '00000000-0000-0000-0000-000000000001',
    'admin@cortexai.local',
    '$2b$12$LJ3m4ys3GZvZ5Kq5Kq5KqOJGz5Kq5Kq5Kq5Kq5Kq5Kq5Kq5Kq5K',  -- Placeholder, verrà rigenerato dal seed script
    'super_admin',
    'CortexAI Admin'
);

-- Secondo tenant di esempio (per testare isolamento multi-tenant)
INSERT INTO tenants (id, name, slug, tier, max_documents, max_queries_per_day, daily_budget_usd)
VALUES (
    '00000000-0000-0000-0000-000000000003',
    'Acme Corporation',
    'acme-corp',
    'basic',
    100,
    1000,
    5.00
);

-- Utente analyst per Acme (per testare RBAC)
INSERT INTO users (id, tenant_id, email, hashed_password, role, full_name)
VALUES (
    '00000000-0000-0000-0000-000000000004',
    '00000000-0000-0000-0000-000000000003',
    'analyst@acme.local',
    '$2b$12$LJ3m4ys3GZvZ5Kq5Kq5KqOJGz5Kq5Kq5Kq5Kq5Kq5Kq5Kq5Kq5K',  -- Placeholder
    'analyst',
    'Acme Analyst'
);


-- ---------------------------------------------------------------------------
-- 10. VERIFICHE FINALI
-- ---------------------------------------------------------------------------
-- Verifica che tutto sia stato creato correttamente
DO $$
DECLARE
    table_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO table_count
    FROM information_schema.tables
    WHERE table_schema = 'public' AND table_type = 'BASE TABLE';

    RAISE NOTICE '✅ CortexAI DB inizializzato: % tabelle create', table_count;
END $$;
