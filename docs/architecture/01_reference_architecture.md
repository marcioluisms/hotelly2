# Arquitetura de Referência — Hotelly V2 (MVP)

## Objetivo
Definir a arquitetura mínima e segura do Hotelly V2 para o MVP/piloto, com foco em:
- **segurança de endpoints** (público vs interno)
- **idempotência ponta a ponta** (replay externo + retries internos)
- **transações críticas no Postgres** (zero overbooking)
- **operabilidade** (logs/triagem/runbook)

Este documento descreve o **sistema-alvo (TARGET)**. No estado atual do repo, a API expõe apenas `/health`.

---

## Região e infraestrutura base
- **Região GCP:** `us-central1` (Iowa)
- **Compute:** Cloud Run (FastAPI)
- **Banco transacional (SoT):** Cloud SQL for Postgres
- **Fila assíncrona:** Cloud Tasks
- **Segredos:** Secret Manager
- **Observabilidade:** Cloud Logging + métricas/alertas (mínimo)

---

## Componentes

### 1) Cloud Run — Serviço Público (`hotelly-public`)
Responsável por:
- receber webhooks/ingress público (Stripe/WhatsApp)
- validar assinatura/token de forma mínima
- registrar **receipt durável** (dedupe) no Postgres
- enfileirar task no Cloud Tasks (idempotente)
- responder ACK conforme contrato (**2xx só após receipt + enqueue**)

**Nunca** deve:
- executar transações críticas longas
- converter hold → reserva
- processar IA/prompting pesado

### 2) Cloud Run — Serviço Worker (`hotelly-worker`)
Responsável por:
- handlers internos acionados por Cloud Tasks (`/tasks/*`)
- executar **transações críticas** no Postgres:
  - create_hold
  - expire_hold
  - stripe_confirm/convert_hold
- emitir outbox_events (quando aplicável)

Deve ser **privado** (auth obrigatório) e invocado por:
- Cloud Tasks via OIDC (service account)

### 3) Cloud SQL — Postgres (fonte da verdade)
Responsável por:
- invariantes transacionais (locks/constraints)
- dedupe/receipt de eventos externos (`processed_events`)
- persistência de core entities (holds, hold_nights, payments, reservations, ari_days)

### 4) Cloud Tasks — filas por finalidade
Responsável por:
- retries e backoff controlados (evita "processo único" travar o público)
- desacoplar ingest público de processamento pesado/crítico

Recomendação (MVP):
- filas separadas por tipo (ex.: `webhooks`, `expires`, `default`)
- `task_id` determinístico para idempotência (ex.: `stripe:<event.id>`, `whatsapp:<message_id>`)

### 5) Secret Manager
Responsável por:
- chaves e segredos por ambiente (stripe secrets, whatsapp secrets, db url/creds)

---

## Fronteiras e superfície de rede

### Público (internet)
Deve expor apenas:
- `/health`
- `/webhooks/stripe/*` (TARGET)
- `/webhooks/whatsapp/*` (TARGET)

### Interno (apenas Cloud Tasks / operadores)
Deve expor apenas:
- `/tasks/*` (TARGET)
- `/internal/*` (se necessário; preferir evitar no MVP)

Regra: **nenhuma rota interna pode existir no serviço público**.

---

## Fluxos mínimos (MVP)

### Fluxo A — Stripe: pagamento confirmado → converter hold
1) Stripe chama `/webhooks/stripe` (público)
2) Public valida assinatura, grava `processed_events(stripe, event.id)`, enfileira task `stripe:<event.id>`
3) Worker recebe task, valida evento e regras (ex.: `payment_status == "paid"`)
4) Worker executa transação crítica: lock hold → criar reservation (UNIQUE) → atualizar ARI → marcar payment/outbox

### Fluxo B — WhatsApp inbound → processamento
1) Provider chama `/webhooks/whatsapp/<provider>` (público)
2) Public normaliza mensagem, grava `processed_events(whatsapp, message_id)`, enfileira `whatsapp:<message_id>`
3) Worker processa (IA/roteamento) e enfileira envio, se aplicável

### Fluxo C — Expiração de holds
1) Scheduler/worker identifica holds vencidos (query) ou task programada por hold
2) Worker executa transação crítica: lock hold → atualizar status → ajustar ARI (`inv_held--`) de forma segura

---

## Notas de implementação
- **Idempotência** é garantida por:
  - `processed_events` (eventos externos)
  - `task_id` determinístico (Cloud Tasks)
  - UNIQUEs no banco (ex.: reservation por hold)
  - `create_idempotency_key` para create_hold (quando aplicável)
- **Segurança**:
  - público valida assinatura/token
  - worker privado com OIDC
  - nunca logar payload bruto/PII
