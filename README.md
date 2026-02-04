# Hotelly V2

> **uv** escolhido por ser 10-100x mais rápido que poetry e ter lockfile nativo.

## Setup

```bash
# Criar venv e instalar dependências
uv sync --all-extras

# Rodar app local
uv run uvicorn hotelly.api.app:app --reload

# Rodar testes
uv run pytest -q
```

## Desenvolvimento local

```bash
./scripts/dev.sh
```

Sobe o servidor em http://127.0.0.1:8000 com hot-reload. Health check: `/health`.

## Context Pack

Gera um pacote de contexto para colar no início de uma nova conversa com o ChatGPT:

```bash
bash scripts/context_pack.sh
```

Cole o output na nova conversa para rehidratar o contexto do projeto.

## Smoke test worker local

Worker exposto em `http://localhost:8001` (porta configurável via `WORKER_PORT`).

```bash
# Chamar assign-room (usando internal secret - apenas local dev)
curl -X POST http://localhost:8001/tasks/reservations/assign-room \
  -H "Content-Type: application/json" \
  -H "X-Internal-Task-Secret: dev-secret" \
  -d '{
    "property_id": "pousada-staging",
    "reservation_id": "<RESERVATION_ID>",
    "room_id": "101",
    "user_id": "user_dev_local_test"
  }'
```

> **Nota:** O header `X-Internal-Task-Secret` só funciona quando `TASKS_OIDC_AUDIENCE=hotelly-tasks-local`.
> Em produção, apenas OIDC é aceito.

[ci bootstrap]
