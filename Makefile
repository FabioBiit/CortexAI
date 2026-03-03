# ============================================================================
# CortexAI — Makefile
# ============================================================================
# Comandi rapidi per gestire il progetto. Usa "make help" per la lista.
# ============================================================================

.PHONY: help up down restart logs ps build clean setup seed test lint benchmark

# Colori per output leggibile
GREEN  := \033[0;32m
YELLOW := \033[0;33m
CYAN   := \033[0;36m
RESET  := \033[0m

# ============================================================================
# HELP — Lista tutti i comandi disponibili
# ============================================================================
help: ## Mostra questa guida
	@echo ""
	@echo "$(CYAN)╔══════════════════════════════════════════════════╗$(RESET)"
	@echo "$(CYAN)║         CortexAI — Comandi Disponibili           ║$(RESET)"
	@echo "$(CYAN)╚══════════════════════════════════════════════════╝$(RESET)"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GREEN)%-20s$(RESET) %s\n", $$1, $$2}'
	@echo ""

# ============================================================================
# SETUP — Primo avvio del progetto
# ============================================================================
setup: ## 🚀 Primo avvio: crea .env, builda e avvia tutto
	@echo "$(YELLOW)📋 Copio .env.example → .env$(RESET)"
	@[ -f .env ] || cp .env.example .env
	@echo "$(YELLOW)🔑 Genero SECRET_KEY...$(RESET)"
	@python3 -c "import secrets; print(secrets.token_hex(32))" | \
		xargs -I {} sed -i 's/<INSERISCI-UNA-STRINGA-RANDOM-DI-64-CARATTERI>/{}/g' .env 2>/dev/null || true
	@echo "$(YELLOW)🏗️  Build e avvio servizi...$(RESET)"
	@$(MAKE) build
	@$(MAKE) up
	@echo "$(YELLOW)⏳ Aspetto che i servizi siano pronti...$(RESET)"
	@sleep 15
	@echo "$(YELLOW)🤖 Scarico modelli Ollama...$(RESET)"
	@docker exec cortexai-ollama ollama pull nomic-embed-text 2>/dev/null || true
	@echo ""
	@echo "$(GREEN)✅ CortexAI è pronto!$(RESET)"
	@echo ""
	@echo "  API Docs:     http://localhost/api/docs"
	@echo "  Grafana:      http://localhost/grafana/ (admin/admin)"
	@echo "  RabbitMQ UI:  http://localhost:15672 (cortexai/***)"
	@echo "  Qdrant UI:    http://localhost:6333/dashboard"
	@echo ""

# ============================================================================
# LIFECYCLE — Gestione servizi Docker
# ============================================================================
up: ## ▶️  Avvia tutti i servizi in background
	docker compose up -d

down: ## ⏹️  Ferma tutti i servizi
	docker compose down

restart: ## 🔄 Riavvia tutti i servizi
	docker compose restart

build: ## 🏗️  Build delle immagini Docker
	docker compose build

clean: ## 🧹 Ferma tutto e CANCELLA volumi (⚠️  perdi i dati!)
	@echo "$(YELLOW)⚠️  ATTENZIONE: questo cancellerà TUTTI i dati (DB, indici, cache)$(RESET)"
	@read -p "Sei sicuro? [y/N] " confirm && [ "$$confirm" = "y" ] || exit 1
	docker compose down -v --remove-orphans

# ============================================================================
# MONITORING — Logs e stato
# ============================================================================
ps: ## 📊 Mostra stato di tutti i servizi
	docker compose ps

logs: ## 📜 Mostra logs di tutti i servizi (live)
	docker compose logs -f --tail=50

logs-api: ## 📜 Logs solo dell'API Gateway
	docker compose logs -f --tail=100 api-gateway

logs-worker: ## 📜 Logs solo del worker di ingestione
	docker compose logs -f --tail=100 ingestion-worker

logs-mcp: ## 📜 Logs solo del MCP Server
	docker compose logs -f --tail=100 mcp-server

# ============================================================================
# DEVELOPMENT — Comandi per sviluppo
# ============================================================================
seed: ## 🌱 Popola il database con dati demo
	docker exec cortexai-api python -m src.db.seed

test: ## 🧪 Esegui tutti i test
	docker exec cortexai-api pytest tests/ -v --tb=short

test-unit: ## 🧪 Esegui solo test unitari
	docker exec cortexai-api pytest tests/unit/ -v --tb=short

test-integration: ## 🧪 Esegui solo test di integrazione
	docker exec cortexai-api pytest tests/integration/ -v --tb=short

lint: ## 🔍 Controlla qualità codice (ruff + mypy)
	docker exec cortexai-api ruff check src/
	docker exec cortexai-api mypy src/ --ignore-missing-imports

format: ## ✨ Formatta il codice automaticamente
	docker exec cortexai-api ruff format src/

# ============================================================================
# BENCHMARK — Performance e qualità
# ============================================================================
benchmark: ## 📈 Esegui benchmark Redis vs Qdrant
	docker exec cortexai-api python scripts/run_benchmarks.py

# ============================================================================
# SCALING — Scala i worker
# ============================================================================
scale-workers: ## ⚡ Scala i worker di ingestione (usa N=3)
	docker compose up -d --scale ingestion-worker=$(or $(N),2)
	@echo "$(GREEN)Worker scalati a $(or $(N),2) istanze$(RESET)"

# ============================================================================
# DATABASE — Gestione migrazioni
# ============================================================================
db-migrate: ## 🗄️  Esegui migrazioni database
	docker exec cortexai-api alembic upgrade head

db-rollback: ## 🗄️  Rollback ultima migrazione
	docker exec cortexai-api alembic downgrade -1

db-shell: ## 🗄️  Apri shell PostgreSQL
	docker exec -it cortexai-postgres psql -U cortexai -d cortexai

redis-shell: ## 🗄️  Apri shell Redis
	docker exec -it cortexai-redis redis-cli
