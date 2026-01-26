SHELL := /usr/bin/env bash

POSTGRES_DB ?= hotelly
POSTGRES_USER ?= postgres
POSTGRES_PASSWORD ?= postgres
POSTGRES_PORT ?= 5432

DATABASE_URL ?= postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@localhost:$(POSTGRES_PORT)/$(POSTGRES_DB)

.PHONY: help dev db-up db-down db-logs clean migrate revision verify

help:
	@echo "Targets:"
	@echo "  make dev         - sobe Postgres local (docker compose)"
	@echo "  make migrate     - aplica migrations (alembic upgrade head)"
	@echo "  make revision    - cria migration vazia (alembic revision -m ...)"
	@echo "  make verify      - roda verificacoes (scripts/verify.sh)"
	@echo "  make clean       - derruba containers e volumes locais"

dev: db-up

db-up:
	docker compose up -d

db-down:
	docker compose down

db-logs:
	docker compose logs -f db

clean:
	docker compose down -v

migrate:
	DATABASE_URL="$(DATABASE_URL)" uv run alembic upgrade head

revision:
	@if [ -z "$(m)" ]; then echo "Use: make revision m='message'"; exit 2; fi
	DATABASE_URL="$(DATABASE_URL)" uv run alembic revision -m "$(m)"

verify:
	./scripts/verify.sh
