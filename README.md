# CortexAI 🧠

**Enterprise Multi-Tenant RAG & AI Platform**

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green.svg)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/Docker-Compose-blue.svg)](https://docs.docker.com/compose/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> A production-grade platform for document ingestion, semantic search, and AI agent interaction
> with enterprise features: multi-tenancy, RBAC, GDPR compliance, cost control, and observability.

## 🏗️ Architecture

| Layer | Technologies |
|-------|-------------|
| **API** | FastAPI, Nginx, JWT/API Key Auth, RBAC |
| **Data** | PostgreSQL (RLS), Qdrant, Redis Stack |
| **AI** | Anthropic Claude, OpenAI, Ollama |
| **Pipeline** | RabbitMQ, PySpark, Delta Lake |
| **Observability** | Prometheus, Grafana, Structured Logging |
| **Platform** | Docker Compose, Databricks, Terraform |

## 🚀 Quick Start

```bash
git clone https://github.com/FabioBiit/CortexAI.git
cd CortexAI
# Create file .env and edit with your API keys
make setup
```

**Endpoints after startup:**
| Service | URL |
|---------|-----|
| API Docs (Swagger) | http://localhost/api/docs |
| Grafana Dashboard | http://localhost/grafana/ |
| RabbitMQ Management | http://localhost:15672 |
| Qdrant Dashboard | http://localhost:6333/dashboard |

## 📋 Development Phases

- [x] Phase 0: Project scaffolding & repository setup
- [x] Phase 1: PostgreSQL + FastAPI + Auth/RBAC + Nginx
- [ ] Phase 2: RabbitMQ + Async Workers
- [ ] Phase 3: Ingestion Pipeline (Parsing + Chunking + Embedding)
- [ ] Phase 4: Hybrid Search (Vector + Full-text + RRF)
- [ ] Phase 5: MCP Server + AI Agent Tools
- [ ] Phase 6: Multi-provider LLM (Anthropic + OpenAI + Ollama)
- [ ] Phase 7: GDPR + Encryption + Data Classification
- [ ] Phase 8: Observability (Prometheus + Grafana + Drift Detection)
- [ ] Phase 9: Databricks (Spark + Delta Lake + MLflow)
- [ ] Phase 10: Multi-cloud Deploy (Azure + AWS + GCP)

## 🐳 Services (12)

`nginx` · `api-gateway` · `mcp-server` · `postgres` · `qdrant` · `redis-stack` · `rabbitmq` · `ingestion-worker` · `prometheus` · `grafana` · `ollama` `+ Databricks CE (cloud)`

## 📄 License

MIT