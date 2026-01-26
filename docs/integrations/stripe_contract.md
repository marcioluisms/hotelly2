# Integração Stripe — Contrato (v0.1)

## Objetivo
Definir como criamos **Checkout Session** e como tratamos **webhooks** com ACK correto, idempotência e conversão **race-safe** de **HOLD → RESERVATION**.

## Princípios
- Webhook público faz somente: **verificar assinatura + receipt durável + enqueue**.
- Conversão HOLD→RESERVATION ocorre no worker em **transação crítica**.
- Dedupe obrigatório por `event.id`.
- Nunca logar payload Stripe bruto.

---

## Checkout Session

### Criação

#### Metadata canônica (mínimo)
- `property_id`
- `hold_id`
- `conversation_id` (se existir)
- `correlation_id`

#### Expiração
- `checkout_expires_at <= hold.expires_at` (nunca maior que o hold)

### Persistência
- Tabela: `payments`
- Campos mínimos:
  - `property_id`
  - `hold_id`
  - `provider = 'stripe'`
  - `provider_object_id = checkout_session_id`
  - `status = created | pending | succeeded | failed | needs_manual`
  - timestamps

#### Uniques (MVP)
- `UNIQUE(property_id, provider, provider_object_id)` (evita duplicar o mesmo objeto Stripe)

#### Uniques (decisão / opcional)
- `UNIQUE(property_id, hold_id)` **NÃO está imposto no schema core atualmente**.
  - Se imposto: impede múltiplas Checkout Sessions por hold (bom para evitar duplicidade por retry).
  - Se não imposto: permite recriar pagamento para o mesmo hold em casos de falha operacional, mas exige cuidado.

**Regra prática do MVP (independente do UNIQUE acima):**
- a criação de Checkout Session deve ser idempotente por hold via `create_idempotency_key` (ou idempotency table)
- e o processamento do webhook deve deduplicar por `event.id` + `provider_object_id`

---

## Webhook

### Endpoint público
- Método: `POST`
- Path: `/webhooks/stripe`

### Verificação (obrigatória)
- validar assinatura `Stripe-Signature` usando secret em Secret Manager
- aplicar tolerância de timestamp (ex.: 5 minutos)

### Receipt durável / Dedupe
- Tabela: `processed_events`
  - `source = 'stripe'`
  - `external_id = event.id`
- Chave de dedupe: UNIQUE `(source, external_id)`

### Semântica de resposta (ACK)
- **2xx** somente quando:
  1) assinatura válida
  2) receipt gravado (ou duplicata detectada)
  3) task foi **enfileirada com sucesso** (ou “already exists” via `task_id` determinístico)
- **4xx** quando:
  - assinatura inválida
  - payload inválido/irrecuperável
- **5xx** quando:
  - falha ao gravar receipt ou ao enfileirar task (para permitir retry do Stripe)

> Regra do MVP: não é permitido ACK 2xx sem enqueue confirmado.

---

## Eventos suportados no MVP
- `checkout.session.completed`
- (opcional) `checkout.session.expired` (para marcar payment como failed/cancelled)

Eventos fora disso:
- marcar como `ignored/unhandled` (sem falhar o webhook), mas **sempre** respeitar receipt/dedupe.

---

## Enqueue idempotente (Cloud Tasks)

### Task handler
- `/tasks/stripe/handle-event`

### Task ID determinístico (obrigatório)
- `task_id = "stripe:" + event.id`

Comportamento:
- “already exists” = sucesso (idempotente)
- falha transitória = 5xx no webhook

---

## Worker: processamento do evento

### Handler
- `/tasks/stripe/handle-event`

### Responsabilidades
- carregar o evento Stripe (via payload do task) e extrair:
  - `event.id`, `event.type`
  - `checkout_session_id` (ex.: de `data.object.id`)
  - `payment_status` (ex.: de `data.object.payment_status`)
  - `metadata.hold_id` e `metadata.property_id` (preferencial)
- registrar/atualizar `payments`:
  - `status = pending/succeeded/failed/needs_manual` conforme regra abaixo
- decidir se enfileira (ou executa) `convert_hold`

---

## Regra mínima de conversão (MVP)

**`checkout.session.completed` NÃO é sinônimo de “pago”.**

Só converter HOLD → RESERVATION se:
- `event.type == "checkout.session.completed"`
- **e** `session.payment_status == "paid"` (ou equivalente Stripe)

Caso contrário:
- atualizar `payments.status = pending` (se aguardando) **ou** `needs_manual` (se inconsistente)
- **não** converter hold.

---

## Convert Hold → Reservation (transação crítica)

### Handler sugerido
- `/tasks/stripe/convert-hold` (pode ser chamado diretamente pelo `handle-event` se a política for “um worker por evento”)

### Regras
- Lock:
  - `SELECT ... FOR UPDATE` no hold
- Se hold não está `ACTIVE` ou expirou:
  - `payments.status = needs_manual`
  - NO-OP de reserva
- Criar `reservation` com:
  - `UNIQUE(property_id, hold_id)`
- Atualizar ARI (por noite, ordem determinística por data):
  - `inv_held--`
  - `inv_booked++`
  - com guardas para nunca negativo
- Outbox:
  - `payment.succeeded`
  - `reservation.confirmed`

---

## Dedupe adicional
- Stripe replay: `processed_events(source='stripe', external_id=event.id)` impede reprocesso.
- Convert replay: `reservations UNIQUE(property_id, hold_id)` impede duplicidade.
- Payment session replay: garantido por `UNIQUE(property_id, provider, provider_object_id)` e idempotência na criação da session.

---

## Logs e PII

### Permitido
- `event.id`, `event.type`, `checkout_session_id`, `payment_id`, `hold_id`, `property_id`, `correlation_id`
- status, `duration_ms`, attempts

### Proibido
- payload Stripe completo
- email/nome/endereço
- qualquer dado de cartão

---

## Retenção (MVP)
- `processed_events` (stripe): recomendado **90 dias** (ajustável)
- `payments/reservations`: retenção de negócio (conforme política do produto)
