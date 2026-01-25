# 01 — Create Hold

Este documento descreve a transação crítica do Hotelly V2, com:
- objetivo e invariantes
- locks (ordem fixa) para evitar race/deadlock
- SQL/pseudocódigo de referência (PostgreSQL)

> Regra global: ao tocar várias noites, iterar sempre em ordem **(room_type_id, date ASC)**.

## Objetivo
Criar um **hold** que reserva inventário com expiração, garantindo **zero overbooking** sob concorrência.

## Entrada
- `property_id`
- `conversation_id`
- `quote_id`
- `quote_option_id` (contém `room_type_id`, `rate_plan_id`, `total_cents`)
- `checkin`, `checkout`
- `expires_at`
- `idempotency_key` (recomendado)

## Saída
- `hold_id`, `expires_at`

## Invariantes
- Se alguma noite não tiver disponibilidade, **nenhum inventário** deve ser reservado.
- Após sucesso: para cada noite do hold, `ari_days.inv_held` incrementa em 1 (ou `qty`).

## Locks e concorrência
- Lock primário: **linhas de ARI** afetadas, via `UPDATE ... WHERE ... AND inv_total >= inv_booked + inv_held + 1`.
- O hold é criado dentro da mesma transação; se falhar, rollback total.

## SQL/pseudocódigo (referência)
```sql
BEGIN;

-- (Opcional) Idempotência para endpoint interno (recomendado)
-- INSERT INTO idempotency_keys(property_id, scope, idempotency_key, created_at)
-- VALUES (:property_id, 'create_hold', :idempotency_key, now())
-- ON CONFLICT (property_id, scope, idempotency_key) DO NOTHING;
-- Se já existia, retornar a resposta gravada.

-- 1) Criar hold
INSERT INTO holds(id, property_id, conversation_id, quote_id, quote_option_id, status, expires_at)
VALUES (gen_random_uuid(), :property_id, :conversation_id, :quote_id, :quote_option_id, 'active', :expires_at)
RETURNING id;

-- 2) Inserir noites do hold (no app, ou via generate_series)
-- Para cada date em [checkin, checkout):
INSERT INTO hold_nights(hold_id, property_id, room_type_id, date, qty)
VALUES (:hold_id, :property_id, :room_type_id, :date, 1);

-- 3) Reservar inventário (uma noite por vez, em ordem date ASC)
UPDATE ari_days
SET inv_held = inv_held + 1, updated_at = now()
WHERE property_id = :property_id
  AND room_type_id = :room_type_id
  AND date = :date
  AND stop_sell = false
  AND inv_total >= (inv_booked + inv_held + 1);

-- 4) Validar: o UPDATE acima deve afetar 1 linha por noite.
-- Se alguma noite afetou 0 linhas -> ROLLBACK (sem hold).
COMMIT;
```

## Falhas esperadas (e como responder)
- Sem inventário: retornar “sem disponibilidade” e não criar hold.
- Stop-sell: idem.
- Conflito de idempotency_key: retornar resposta anterior.
