# CortexAI — Enterprise Multi-Tenant RAG & AI Platform
## Architecture Design Document v2.0

## Riepilogo Decisioni di Design

| Decisione | Scelta | Motivazione |
|---|---|---|
| Complessità | Full stack (12+ servizi) | Massimo impatto portfolio |
| Vector DB | Qdrant + Redis Stack | Best-of-breed per ciascun ruolo |
| LLM Strategy | Anthropic + OpenAI + Ollama | Multi-provider, adapter pattern |
| DB Relazionale | PostgreSQL | RLS nativo, JSONB, maturo |
| API Framework | FastAPI | Async, OpenAPI auto-docs, Pydantic |
| Message Broker | RabbitMQ | Leggero, affidabile, perfetto per async |
| Reverse Proxy | Nginx | Standard di fatto, SSL termination |
| Data Platform | Databricks CE → multi-cloud | Spark + Delta Lake + MLflow gratis |

## Architettura Completa — 12 Servizi

    INTERNET / AI AGENTS
            │
            ▼
      ┌──────────┐
      │  NGINX   │ ← SSL, rate limiting, security headers
      │ :80/:443 │
      └────┬─────┘
           ├── /api/*    → FastAPI Gateway (:8000)
           ├── /mcp/*    → MCP Server (:8001)
           ├── /grafana/ → Grafana (:3000)
           └── /rabbitmq → RabbitMQ UI (:15672)

      ┌─── SERVICE MESH ───────────────────────┐
      │ PostgreSQL │ Qdrant │ Redis  │ RabbitMQ │
      │  :5432     │ :6333  │ :6379  │  :5672   │
      └────────────────────────┬────────────────┘
                               │
                      Ingestion Worker(s)

      Observability: Prometheus (:9090) → Grafana (:3000)
      AI Layer: Ollama (:11434) │ Anthropic API │ OpenAI API
      Data Platform: Databricks CE (Spark + Delta Lake + MLflow)

## Guida Strumenti

**FastAPI** — API Gateway. Async, auto-docs OpenAPI, Pydantic, Dependency Injection.
**PostgreSQL** — DB relazionale. RLS per multi-tenant, JSONB, 9 tabelle, audit log.
**Qdrant** — Vector DB (Rust). Ricerca semantica, filtri payload, gRPC + REST.
**Redis Stack** — Cache + RediSearch + Sessions. Sub-millisecondo, 3 ruoli in 1.
**RabbitMQ** — Message broker. 5 code, DLX, routing intelligente, ~125MB RAM.
**Nginx** — Reverse proxy. SSL, rate limiting, security headers, 10K+ connessioni.
**Databricks CE** — PySpark, Delta Lake, MLflow. Gratuito.
**Prometheus + Grafana** — Metriche pull-based + 7 dashboard + alerting.
**Ollama** — LLM locale gratuito (nomic-embed-text, llama3.2).
**Anthropic + OpenAI** — LLM cloud (Claude per RAG, OpenAI per embedding).

## Multi-Cloud Strategy

| Componente | Local | Azure | AWS | GCP |
|---|---|---|---|---|
| Spark | Databricks CE | Azure Databricks | EMR/Glue | Dataproc |
| PostgreSQL | Docker | Azure DB for PG | RDS | Cloud SQL |
| Vector DB | Qdrant Docker | Qdrant Cloud | EC2 | GCE |
| Redis | Redis Stack | Azure Cache | ElastiCache | Memorystore |
| Queue | RabbitMQ | Service Bus | SQS | Pub/Sub |
| API | Docker Compose | Container Apps | ECS Fargate | Cloud Run |

Fase 1-9 → Local + Databricks CE (GRATIS)
Fase 10+ → Azure ($200 free) → AWS (Free Tier) → GCP ($300 free)

## 10 Fasi di Sviluppo

| Fase | Focus |
|---|---|
| 1 | PostgreSQL + FastAPI + Auth/RBAC + Nginx |
| 2 | RabbitMQ + Workers asincroni |
| 3 | Ingestione: Parsing + Chunking + Embedding |
| 4 | Search: Vettoriale + Ibrida + RRF |
| 5 | MCP: Server + Tools + Dataset-to-Tool |
| 6 | LLM: Multi-provider + RAG pipeline |
| 7 | Security: GDPR + Encryption + Classification |
| 8 | Observability: Prometheus + Grafana + Drift |
| 9 | Databricks: Notebooks + Delta Lake + MLflow |
| 10 | Polish: Tests, docs, demo, multi-cloud |