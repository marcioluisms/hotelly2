SHELL := /usr/bin/env bash

POSTGRES_DB ?= hotelly
POSTGRES_USER ?= postgres
POSTGRES_PASSWORD ?= postgres
POSTGRES_PORT ?= 5432

DATABASE_URL ?= postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@localhost:$(POSTGRES_PORT)/$(POSTGRES_DB)

# Python preflight: verify local Postgres is reachable before running Alembic.
define _migrate_check
import os, socket, re, sys
url = os.environ["DATABASE_URL"]
m = re.search(r'@([^/:]+)(?::(\d+))?/', url)
if m:
    host, port = m.group(1), int(m.group(2) or 5432)
    if host in ("localhost", "127.0.0.1"):
        s = socket.socket()
        s.settimeout(2.0)
        try:
            s.connect((host, port))
        except OSError:
            print(f"Postgres not reachable at {host}:{port}. Run 'make db-up' (or 'docker compose up -d') and try again.")
            sys.exit(2)
        finally:
            s.close()
endef
export _migrate_check

.PHONY: help dev db-up db-down db-logs clean migrate migrate-local revision verify test test-unit test-e2e test-redaction

help:
	@echo "Targets:"
	@echo "  make dev           - sobe Postgres local (docker compose)"
	@echo "  make migrate       - aplica migrations em DATABASE_URL (sem docker)"
	@echo "  make migrate-local - sobe Postgres local e aplica migrations"
	@echo "  make revision      - cria migration vazia (alembic revision -m ...)"
	@echo "  make verify        - roda verificacoes (scripts/verify.sh)"
	@echo "  make clean         - derruba containers e volumes locais"

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
	@export DATABASE_URL="$(DATABASE_URL)" && printenv _migrate_check | uv run python -
	DATABASE_URL="$(DATABASE_URL)" uv run alembic upgrade head

migrate-local: db-up migrate

revision:
	@if [ -z "$(m)" ]; then echo "Use: make revision m='message'"; exit 2; fi
	DATABASE_URL="$(DATABASE_URL)" uv run alembic revision -m "$(m)"

verify:
	./scripts/verify.sh

test:
	DATABASE_URL="$(DATABASE_URL)" uv run pytest -q

test-unit:
	uv run pytest tests/test_hashing.py tests/test_contact_refs.py -q

test-e2e:
	DATABASE_URL="$(DATABASE_URL)" uv run pytest tests/test_e2e_flow.py -v

test-redaction:
	uv run pytest tests/test_redaction.py -v
