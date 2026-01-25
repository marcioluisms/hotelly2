# Observability (Logs, Métricas, Tracing e Alertas)

**Documento:** docs/operations/04_observability.md  
**Objetivo:** garantir visibilidade operacional do Hotelly V2 (piloto e produção) com foco em **segurança**, **idempotência**, **concorrência** (anti-overbooking) e **tempo de resolução** (MTTR), sem vazamento de PII.

> Regra de ouro: se não está medido/alertado, não existe. Se está logado com PII, é incidente.

---

## 1. Escopo e prioridades

### 1.1 Prioridade do piloto
A observabilidade do piloto deve cobrir:
- **Fluxo de receita**: hold → payment → reservation.
- **Confiabilidade de ingestão**: WhatsApp inbound + Stripe webhooks + Cloud Tasks.
- **Integridade do inventário**: *overbooking = 0* e invariantes do ARI.
- **Recuperabilidade**: reprocessamento e reconciliação com rastreabilidade (processed_events + outbox).

### 1.2 Fora de escopo (no piloto)
- APM avançado com instrumentação profunda em todas as libs.
- Análise de custo por requisição no detalhe (depois do piloto).
- Tracing distribuído “perfeito” (deixar “bom o suficiente” primeiro).

---

## 2. Princípios (não negociáveis)

1) **Sem payload bruto em logs** (request body, webhook JSON, mensagens do WhatsApp).  
2) **Sem PII** em logs/metrics/traces (telefone, nome, conteúdo de mensagem, e-mail).  
3) **Logs estruturados (JSON)** sempre, com campos canônicos.  
4) **Correlation ID end-to-end**: request → task → DB txn → outbound.  
5) **Idempotência observável**: todo dedupe/no-op deve ser medido.  
6) **Alertas acionáveis**: todo alerta deve ter runbook e owner.

---

## 3. Identificadores e correlação

### 3.1 IDs canônicos (sempre que existirem)
- `correlation_id` (string, obrigatório): gerado no primeiro contato (inbound) e propagado.
- `request_id` (string): do Cloud Run (se disponível) ou gerado.
- `property_id` (string): pousada/estabelecimento.
- `conversation_id` (string)
- `hold_id` (string)
- `payment_id` (string) e `provider_object_id` (Stripe checkout.session.id)
- `reservation_id` (string)
- `idempotency_key` (string) + `idempotency_scope` (string)
- `event_source` (enum): `whatsapp_meta`, `whatsapp_evolution`, `stripe`, `tasks`, `admin`, `system`
- `external_id` (string): message_id / stripe_event_id / task_id

### 3.2 Propagação obrigatória
- Inbound HTTP: se houver header `X-Correlation-Id`, validar e reutilizar; senão gerar.
- Cloud Tasks: setar `X-Correlation-Id` e `X-Event-Source=tasks` na task.
- Stripe webhooks: correlacionar via `metadata` (hold_id/property_id/conversation_id) e registrar `stripe_event_id` como `external_id`.

---

## 4. Logs

### 4.1 Formato
- **JSON por linha** (structured logging).
- Campos mínimos em *todas* as linhas:
  - `severity` (DEBUG/INFO/WARNING/ERROR)
  - `timestamp` (ISO8601)
  - `service` (ex.: `api`, `worker`)
  - `env` (`dev|staging|prod`)
  - `correlation_id`
  - `event_name` (ver catálogo abaixo)
  - `property_id` (quando aplicável)
  - `duration_ms` (quando aplicável)
  - `status` (`success|no_op|failed|retrying`)
  - `error_code` (quando falha; enum)
  - `error_class` (ex.: `ValidationError`, `DBError`, `StripeError`)

### 4.2 Catálogo mínimo de eventos (pilot)
**Ingressos**
- `whatsapp.inbound.received`
- `stripe.webhook.received`
- `tasks.received`

**Dedupe / idempotência**
- `dedupe.hit` (no-op por processed_events)
- `idempotency.hit` (no-op por idempotency_keys)
- `outbox.appended`

**Transações críticas**
- `hold.create.started` / `hold.create.committed` / `hold.create.rejected` (inventory guard)
- `hold.expire.started` / `hold.expire.committed` / `hold.expire.no_op`
- `hold.cancel.started` / `hold.cancel.committed` / `hold.cancel.no_op`
- `hold.convert.started` / `hold.convert.committed` / `hold.convert.no_op` / `hold.convert.expired`

**Pagamentos / reservas**
- `payment.upserted`
- `reservation.created`

**Outbound**
- `whatsapp.outbound.sent`
- `whatsapp.outbound.retry`
- `whatsapp.outbound.failed`

### 4.3 Redação (redaction)
Campos proibidos em logs:
- conteúdo de mensagem
- números de telefone
- emails
- payload completo de webhooks
- nomes de hóspedes

Se precisar depurar, usar:
- **hash** (ex.: `phone_hash`)
- **prefixo parcial** (ex.: últimos 4 dígitos, se aprovado)
- **tamanho do payload** (`payload_bytes`)
- **lista de chaves** (`payload_keys`)

### 4.4 Níveis e volume
- INFO: fluxo normal e eventos de domínio (1 linha por etapa).
- WARNING: retries, no-op inesperado, degradação.
- ERROR: falha de transação, inconsistência, exceções.
- DEBUG: somente em dev/staging (bloquear em prod por padrão).

---

## 5. Métricas

### 5.1 Convenções
- Nome em `snake_case`.
- Labels (cuidado com cardinalidade):
  - permitido: `env`, `service`, `event_source`, `provider`, `status`, `error_code`
  - proibido: `phone`, `message_id`, `hold_id` (alta cardinalidade)

### 5.2 RED (API e Workers)
**API**
- `http_requests_total{route,method,status}`
- `http_request_duration_ms_bucket{route,method}`

**Workers/Tasks**
- `tasks_processed_total{queue,status}`
- `tasks_duration_ms_bucket{queue}`

### 5.3 Domínio (o que importa)
**Holds**
- `holds_created_total`
- `holds_expired_total`
- `holds_cancelled_total`
- `holds_converted_total`
- `holds_active_gauge` (por property_id só se cardinalidade controlada; caso contrário global)

**Inventário**
- `inventory_guard_rejections_total` (quando o `WHERE` falha)
- `inventory_invariant_violations_total` (detectado por checks/reconcile)

**Pagamentos/Reservas**
- `payments_received_total{provider}`
- `payments_succeeded_total{provider}`
- `payments_late_total{provider}` (pagou após expirar)
- `reservations_created_total`

**Idempotência / Dedupe**
- `processed_events_dedupe_hits_total{source}`
- `idempotency_hits_total{scope}`

**Outbox**
- `outbox_events_appended_total{event_type}`
- `outbox_lag_seconds` (tempo do evento mais antigo não processado, se houver consumidor)
  - No piloto, se não houver consumidor, registrar apenas appended.

### 5.4 SLOs recomendados (pilot)
Alinhar ao `docs/strategy/06_success_metrics.md`. Sugestão inicial:
- **Overbooking**: 0 (SLO absoluto; qualquer violação = incidente).
- **Webhook Stripe**: 99% ACK < 2s; erro 5xx < 0.5%.
- **Tasks**: backlog < 1 min (p95) em horário comercial do piloto.
- **Conversão hold→reserva**: p50 < 2 min em sandbox (depende do pagamento humano).

---

## 6. Tracing

### 6.1 Objetivo mínimo
Não é “full tracing”. É:
- rastrear **um fluxo** do início ao fim pelo `correlation_id`
- medir **latência** por etapa
- identificar **pontos de falha** (DB, Stripe, WhatsApp)

### 6.2 Implementação recomendada (GCP)
- Cloud Run + Cloud Logging já permite correlacionar por `trace` quando configurado.
- Se usar OpenTelemetry, manter **mínimo**:
  - spans: `inbound`, `db_txn`, `task_enqueue`, `outbound`
  - atributos: `correlation_id`, `event_source`, `status`, `error_code`

### 6.3 Anti-padrões
- colocar payload no span
- tags de alta cardinalidade (IDs únicos por evento) em prod

---

## 7. Dashboards (Cloud Monitoring)

### 7.1 Dashboard “Piloto — Funil”
- Inbound WhatsApp (volume, erro)
- Holds created / converted / expired (por janela)
- Payments succeeded
- Reservations created
- Conversion rate (holds_converted_total / holds_created_total)

### 7.2 Dashboard “Confiabilidade”
- Stripe webhook 2xx/5xx
- Tasks processed, retries, backlog
- Error rate por `error_code`
- Latência p50/p95 API e worker

### 7.3 Dashboard “Integridade”
- inventory_guard_rejections_total (esperado em alta demanda)
- inventory_invariant_violations_total (**deve ser 0**)
- payments_late_total
- holds_active_gauge (tendência)

---

## 8. Alertas (com severidade e ação)

### 8.1 Stop-ship (SEV-1)
Dispara e exige ação imediata:
1) `inventory_invariant_violations_total > 0` (janela 5m)
2) `reservations_created_total` aumenta sem `payments_succeeded_total` correspondente (janela 15m) *quando o fluxo exigir pagamento prévio*
3) Stripe webhook 5xx sustentado > 2% por 10m
4) Tasks backlog > 10m por 15m (fila crítica)

**Obrigatório:** linkar para o `docs/operations/05_runbook.md` (procedimentos) e registrar incidente.

### 8.2 Operacional (SEV-2/SEV-3)
- `payments_late_total` > limiar (ex.: 3/dia)
- `holds_active_gauge` crescendo sem conversão (sugere falha de outbound ou UX)
- `whatsapp.outbound.failed` acima de limiar

### 8.3 Observações práticas
- Cada alerta tem:
  - sintoma
  - hipótese provável
  - passo 1–3 (rápido)
  - queries SQL de confirmação
  - ação de mitigação (reprocess/expire/retry)

---

## 9. Pontos de instrumentação (checklist por componente)

### 9.1 Webhook WhatsApp (inbound)
- Log: `whatsapp.inbound.received` com `external_id`, `event_source`, `payload_bytes`
- Métrica: `http_requests_total` + `processed_events_dedupe_hits_total{source=whatsapp_*}`
- Task: log `tasks.enqueued` com queue e attempt = 0

### 9.2 Webhook Stripe
- Log: `stripe.webhook.received` com `stripe_event_id`
- Receipt durável: `dedupe.hit` / `processed_events.inserted`
- Métrica: 2xx/5xx, latência, dedupe hits

### 9.3 Transações críticas (DB)
Para cada transação:
- Log started + committed + (failed/no_op)
- `duration_ms` obrigatório
- Métrica de sucesso/falha e `error_code`

Erros com `error_code` padronizado:
- `INVENTORY_GUARD_FAILED`
- `HOLD_NOT_ACTIVE`
- `HOLD_EXPIRED`
- `PROCESSED_EVENT_DUPLICATE`
- `IDEMPOTENCY_KEY_REPLAY`
- `DB_SERIALIZATION_FAILURE`
- `DB_DEADLOCK_DETECTED`
- `STRIPE_SIGNATURE_INVALID`
- `WHATSAPP_PROVIDER_ERROR`

### 9.4 Outbound WhatsApp
- Log: sent/retry/failed
- Métrica: retries e falhas por provider

---

## 10. Segurança e compliance (operacional)

### 10.1 Redução de risco de PII
- Regex/linters de CI (gate) para `print(` e padrões de logging proibidos.
- Revisão obrigatória em alterações de logging em endpoints externos.
- Retenção de logs em prod: definir janela compatível com piloto (curta) e ampliar depois.

### 10.2 Segredos
- Nunca logar:
  - tokens WhatsApp
  - Stripe secrets
  - connection strings
- Se houver exceção, substituir por `***`.

---

## 11. Apêndice A — Dicionário de campos de log

| Campo | Tipo | Obrigatório | Observação |
|---|---:|---:|---|
| correlation_id | string | sim | propagado por headers/tasks |
| event_name | string | sim | catálogo do item 4.2 |
| event_source | string | sim | whatsapp/stripe/tasks/... |
| external_id | string | não | message_id / stripe_event_id / task_id |
| property_id | string | não | evitar alta cardinalidade em métricas, ok em log |
| hold_id/payment_id/reservation_id | string | não | apenas em log/tracing, não em métrica |
| duration_ms | int | não | obrigatório em transações |
| status | string | sim | success/no_op/failed/retrying |
| error_code | string | não | enum padronizado |
| payload_bytes | int | não | sempre preferir isso ao payload |

---

## 12. Apêndice B — Conjunto mínimo de alertas do piloto (checklist)
- [ ] Overbooking/invariante de inventário (SEV-1)
- [ ] Stripe webhook 5xx sustentado (SEV-1)
- [ ] Tasks backlog crítico (SEV-1)
- [ ] Payments late acima do limiar (SEV-2)
- [ ] Falha de outbound WhatsApp (SEV-2)
- [ ] Aumento de errors por `DB_SERIALIZATION_FAILURE` (SEV-2)

---

## 13. Referências internas
- docs/strategy/06_success_metrics.md
- docs/operations/07_quality_gates.md
- docs/operations/05_runbook.md
- docs/data/01_sql_schema_core.sql
