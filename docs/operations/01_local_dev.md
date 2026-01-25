# Desenvolvimento Local — Hotelly V2 (`docs/operations/01_local_dev.md`)

## Objetivo
Permitir que **uma pessoa** rode o Hotelly V2 localmente com o mínimo de atrito, mantendo as mesmas garantias que importam em produção:
- **idempotência** (webhooks/tasks/mensagens)
- **0 overbooking**
- **sem PII/payload raw em logs**
- **replay confiável** (webhooks e tasks)

Este documento é **normativo**: se um comando “oficial” não existir no repo, isso vira tarefa de implementação.

---

## Pré‑requisitos
Obrigatórios:
- Docker + Docker Compose v2
- Git

Recomendados (para debug e integração com GCP):
- Python (mesma versão definida no projeto)
- `psql` (cliente Postgres)
- Google Cloud SDK (`gcloud`)
- Stripe CLI (para replay realista de webhooks)
- (Opcional) `jq`

---

## Convenções locais
- **Nada de segredos versionados.** Use `.env.local` (gitignored).
- **Nada de payload bruto em logs.** Se precisar depurar, logue apenas:
  - `correlation_id`
  - `event_id/message_id/task_id`
  - `property_id`, `hold_id`, `reservation_id`
  - códigos de erro (sem dados do hóspede)

---

## TL;DR (quickstart)
1) Subir infra local:
```bash
docker compose up -d --build
```

2) Aplicar schema (um dos dois caminhos):
- **A) Migrações (preferido)**:
```bash
docker compose exec app make migrate
# ou: docker compose exec app alembic upgrade head
```
- **B) SQL core (fallback)**:
```bash
docker compose exec -T db psql -U ${POSTGRES_USER:-postgres} -d ${POSTGRES_DB:-hotelly} < docs/data/01_sql_schema_core.sql
```

3) Seed mínimo:
```bash
docker compose exec app make seed-minimal
# ou: docker compose exec app python -m hotelly.scripts.seed_minimal
```

4) Rodar testes (gates locais):
```bash
docker compose exec app make test
# ou: docker compose exec app pytest -q
```

5) Smoke:
```bash
curl -sS http://localhost:${APP_PORT:-8000}/health
```

---

## Docker Compose (layout esperado)
A execução local deve ter, no mínimo, estes serviços:
- `db`: Postgres
- `app`: API (FastAPI)
- `worker`: consumidor de tasks (modo local) **ou** worker que processa jobs/outbox

Portas padrão recomendadas:
- API: `8000`
- Postgres: `5432`

Se o repo ainda não tiver `docker-compose.yml`, crie como parte do backlog (Sprint 0). Este documento assume que ele existe.

---

## Arquivo `.env.local` (mínimo)
Crie `.env.local` a partir de `.env.example` (se existir) ou manualmente.

Exemplo (ajuste nomes conforme o código):
```env
ENV=local
APP_PORT=8000

# Postgres local (compose)
POSTGRES_DB=hotelly
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
DATABASE_URL=postgresql://postgres:postgres@db:5432/hotelly

# Logs
LOG_LEVEL=INFO

# Tasks
TASKS_BACKEND=local  # local | inline | gcp (staging/prod)

# Stripe (para integração real)
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...

# WhatsApp (quando integrar)
WHATSAPP_PROVIDER=meta  # meta | evolution
WHATSAPP_VERIFY_TOKEN=dev-token
```

Notas:
- `TASKS_BACKEND=inline` é útil para debug (executa handlers no mesmo processo). **Proibido em staging/prod.**
- Em staging/prod, o backend é `gcp` (Cloud Tasks).

---

## Comandos “oficiais” (make targets)
Recomendação: padronizar `make` para reduzir variação local.

Targets mínimos:
- `make dev` — sobe stack local (equivalente ao compose)
- `make migrate` — aplica migrações
- `make seed-minimal` — cria 1 property, 1 room_type, ARI de um range curto
- `make test` — roda a suíte mínima (incluindo gates relevantes)
- `make lint` — lint básico (inclui gate PII/print se aplicável)
- `make e2e` — fluxo controlado (quando existir)

Se `make` não for usado, estes comandos devem existir como scripts/documentados.

---

## Banco local: operações úteis
### Entrar no Postgres
```bash
docker compose exec db psql -U ${POSTGRES_USER:-postgres} -d ${POSTGRES_DB:-hotelly}
```

### Queries de sanidade (inventário e invariantes)
**1) Checar overbooking (deve ser 0 linhas):**
```sql
SELECT property_id, room_type_id, day
FROM ari_days
WHERE (inv_booked + inv_held) > inv_total;
```

**2) Holds ativos vencidos (candidato a expire):**
```sql
SELECT id, property_id, status, expires_at
FROM holds
WHERE status = 'active' AND expires_at < now()
ORDER BY expires_at ASC;
```

**3) Pagamentos confirmados sem reserva (deve ser 0 ou virar runbook):**
```sql
SELECT p.*
FROM payments p
LEFT JOIN reservations r ON r.payment_id = p.id
WHERE p.status = 'succeeded' AND r.id IS NULL;
```

---

## Rodar a API localmente (sem container)
Use isso só se estiver iterando rápido em código Python.

Exemplo:
```bash
export $(cat .env.local | xargs)  # cuidado com espaços/quotes
python -m uvicorn hotelly.api.app:app --reload --host 0.0.0.0 --port ${APP_PORT:-8000}
```

Regras:
- Ainda assim, o Postgres deve estar no Docker.
- Logs devem continuar sem payload raw/PII.

---

## Tasks local (Cloud Tasks “simulado”)
Como Cloud Tasks não tem emulador oficial simples, a estratégia local deve ser uma destas:

### Opção A (preferida): `TASKS_BACKEND=local` + worker rodando
- `app` apenas enfileira (persistindo receipt/processed_events quando necessário)
- `worker` consome (poll) e executa handlers

Exemplo esperado:
```bash
docker compose up -d worker
docker compose logs -f worker
```

### Opção B: `TASKS_BACKEND=inline` (debug)
- Enfileiramento executa imediatamente no mesmo processo.
- Bom para depurar, ruim para simular retries e concorrência.

**Regra:** qualquer comportamento de retry/idempotência deve ser testado também no modo `local` (ou em staging com Cloud Tasks).

---

## Replay de webhooks (Stripe)
Objetivo: provar **dedupe + ACK correto** e fechar o loop `payment_succeeded → convert_hold`.

### Configurar listener local
1) Setar `STRIPE_WEBHOOK_SECRET` no `.env.local`
2) Rodar:
```bash
stripe listen --forward-to http://localhost:${APP_PORT:-8000}/webhooks/stripe
```

### Disparar eventos de teste
Exemplos (variar conforme seu fluxo):
```bash
stripe trigger checkout.session.completed
stripe trigger payment_intent.succeeded
```

### O que validar
- Repetir o mesmo evento não duplica efeito:
  - `processed_events` impede duplicidade
  - `reservations` tem UNIQUE por `(property_id, hold_id)`
- Resposta 2xx só ocorre após receipt durável (registrar processed_events e/ou task durável)

---

## Replay de inbound WhatsApp (quando existir)
Regra: **um único contrato interno** de mensagem; provider só adapta.

Exemplo genérico de POST (payload *redigido*):
```bash
curl -sS -X POST "http://localhost:${APP_PORT:-8000}/webhooks/whatsapp" \
  -H "Content-Type: application/json" \
  -H "X-Correlation-Id: dev-123" \
  -d '{
    "provider":"meta",
    "message_id":"wamid.TEST",
    "from":"+5500000000000",
    "text":"quero reservar",
    "timestamp":"2026-01-25T00:00:00Z"
  }'
```

O que validar:
- Repetir o mesmo `message_id` não processa duas vezes
- Nada do payload aparece integralmente em logs

---

## Suite mínima local (espelha os Quality Gates)
Rodar antes de fechar qualquer story relevante:

- G0 — build & startup:
```bash
docker compose exec app python -m compileall -q src
curl -sS http://localhost:${APP_PORT:-8000}/health
```

- G1 — migrações e schema:
```bash
docker compose exec app make migrate
docker compose exec app make migrate  # repetir (idempotente)
```

- G2 — segurança/PII:
```bash
docker compose exec app make lint
```

- G3–G5 (quando transações críticas existirem):
```bash
docker compose exec app make test-idempotency
docker compose exec app make test-concurrency
docker compose exec app make test-race-expire-vs-convert
```

Se os targets ainda não existirem, a story deve criá-los (ou documentar o comando equivalente).

---

## Reset completo do ambiente local
Quando o estado do banco estiver “sujo”:
```bash
docker compose down -v
docker compose up -d --build
docker compose exec app make migrate
docker compose exec app make seed-minimal
```

---

## Troubleshooting (curto e prático)
### App sobe, mas não conecta no DB
- Confirme `DATABASE_URL` (host deve ser `db` no compose, não `localhost`)
- Veja logs:
```bash
docker compose logs -f app
docker compose logs -f db
```

### Migração falha por schema “meio aplicado”
- Reset com `down -v` (ambiente de dev local é descartável)

### Duplicidade de eventos (webhook/task)
- Verifique UNIQUE em `processed_events(source, external_id)`
- Verifique que o handler grava receipt **antes** de produzir efeitos colaterais

### Overbooking no teste de concorrência
- Falta guarda no `WHERE` do update de ARI
- Falta transação envolvendo todas as noites
- Ordem de updates não determinística

---

## Checklist antes de integrar qualquer coisa “real”
- [ ] `processed_events`, `idempotency_keys`, `outbox_events` existem e estão cobertos por testes
- [ ] overbooking query retorna 0
- [ ] replay de webhook e message_id não duplica efeito
- [ ] logs sem payload bruto/PII
