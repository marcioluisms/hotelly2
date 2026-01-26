# Outbox — Contrato (append-only)

## Objetivo

Manter uma trilha **append-only** de eventos de domínio relevantes para:
- auditoria operacional,
- métricas (ex.: conversões, expirações),
- diagnóstico (correlação por request),
- futura integração/analytics.

**Regra:** payload **mínimo** e **sem PII**.

## Tabela

`outbox_events` (Postgres / Cloud SQL)

Campos principais:
- `property_id` (tenant)
- `event_type` (string)
- `aggregate_type` (string)
- `aggregate_id` (string)
- `occurred_at` (timestamptz)
- `correlation_id` (string, opcional)
- `payload` (jsonb, opcional)

## Event Types (catálogo mínimo)

### Holds
- `HOLD_CREATED`
- `HOLD_EXPIRED`
- `HOLD_CANCELLED`
- `HOLD_CONVERTED`

### Payments
- `PAYMENT_CREATED`
- `PAYMENT_SUCCEEDED`
- `PAYMENT_FAILED`

### Reservations
- `RESERVATION_CONFIRMED`
- `RESERVATION_CANCELLED`

### Observações
- `event_type` deve ser **estável** e usado em métricas.
- Evitar tipos "genéricos" (ex.: `UPDATED`) sem contexto.

## Aggregate Types

Valores previstos (mínimo):
- `hold`
- `payment`
- `reservation`
- `conversation`

## Payload permitido (mínimo)

O payload deve ser pequeno e não conter PII. Campos típicos:
- `hold_id`, `reservation_id`, `payment_id` (ids internos)
- `provider`, `provider_object_id` (ex.: `stripe`, `checkout.session.id`)
- `amount_cents`, `total_cents`, `currency`
- `checkin`, `checkout`
- `room_type_id`, `guest_count` (sem nomes/telefones/emails)

**Proibido no payload:**
- telefone, email, nome, endereço, documento, mensagem de chat
- payload bruto do provedor (Stripe/WhatsApp)

## Regras de escrita

- Sempre dentro da **mesma transação** que altera o estado crítico (hold/payment/reservation).
- Uma ação crítica deve emitir **exatamente um** evento outbox correspondente.
- `correlation_id` deve ser propagado do request/task.

## Retenção

Ver `docs/operations/08_retention_policy.md`.
