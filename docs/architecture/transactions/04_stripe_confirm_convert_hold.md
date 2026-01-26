# 04 — Stripe Confirm → Convert Hold → Create Reservation

Este documento descreve a transação crítica do Hotelly V2, com:
- objetivo e invariantes
- locks (ordem fixa) para evitar race/deadlock
- SQL/pseudocódigo de referência (PostgreSQL)

> Regra global: ao tocar várias noites, iterar sempre em ordem **(room_type_id, date ASC)**.

## Objetivo
Processar pagamento confirmado (Stripe) de forma idempotente, convertendo hold ACTIVE em reserva confirmada.

## Entrada
- `property_id`
- `stripe_event_id` (dedupe)
- `checkout_session_id` (canonical object)
- `hold_id`
- `conversation_id`
- `amount_cents`, `currency`

## Saída
- `payments.status = succeeded` (upsert)
- `holds.status = converted` (se ACTIVE e não expirado)
- `reservations` criada (1:1 com hold)
- Inventário: `inv_held--` e `inv_booked++` por noite

## Invariantes
- Reprocessar o mesmo Stripe event não duplica reserva.
- Reprocessar a mesma checkout session não duplica payment.
- Corrida com expiração é serializada pelo lock no hold.
- Se hold expirou antes do pagamento: não cria reserva automaticamente (caminho manual/política).

## Locks e concorrência
- `processed_events` impede duplicidade do webhook.
- `SELECT ... FOR UPDATE` em `holds` serializa com expiração/cancelamento.
- Ordem fixa ao atualizar ARI (date ASC).

## SQL/pseudocódigo (referência)
```sql
BEGIN;

-- 0) Dedupe do webhook
INSERT INTO processed_events(property_id, source, external_id)
VALUES (:property_id, 'stripe', :stripe_event_id)
ON CONFLICT (property_id, source, external_id) DO NOTHING;

-- Se já existia, sair (idempotente)

-- 1) Upsert payment (dedupe por checkout.session.id)
INSERT INTO payments(property_id, conversation_id, hold_id, provider, provider_object_id,
                     status, amount_cents, currency, created_at, updated_at)
VALUES (:property_id, :conversation_id, :hold_id, 'stripe', :checkout_session_id,
        'succeeded', :amount_cents, :currency, now(), now())
ON CONFLICT (property_id, provider, provider_object_id)
DO UPDATE SET status='succeeded', updated_at=now();

-- 2) Lock do hold
SELECT status, expires_at
FROM holds
WHERE id = :hold_id AND property_id = :property_id
FOR UPDATE;

-- 3) Guardas
-- Se status != 'active' -> COMMIT (no-op / já processado)
-- Se now() > expires_at -> COMMIT (caminho manual: payment succeeded com hold expirado)

-- 4) Converter inventário (por noite)
UPDATE ari_days
SET inv_held = inv_held - 1,
    inv_booked = inv_booked + 1,
    updated_at = now()
WHERE property_id = :property_id
  AND room_type_id = :room_type_id
  AND date = :date
  AND inv_held >= 1;

-- Validar: atualizou 1 linha por noite (senão, rollback: dado inconsistente)

-- 5) Criar reservation (dedupe via unique property_id+hold_id)
INSERT INTO reservations(property_id, conversation_id, hold_id, status, checkin, checkout, total_cents, currency)
VALUES (:property_id, :conversation_id, :hold_id, 'confirmed', :checkin, :checkout, :total_cents, :currency)
ON CONFLICT (property_id, hold_id) DO NOTHING;

-- 6) Mark hold converted
UPDATE holds
SET status = 'converted', updated_at = now()
WHERE id = :hold_id AND property_id = :property_id AND status = 'active';

COMMIT;
```

## Caminho manual (MVP) — pagamento confirmado com hold expirado
Recomendação:
- registrar evento (outbox) e criar pendência operacional.
- política decide: remarcar/estornar/reservar manualmente se ainda houver inventário.
