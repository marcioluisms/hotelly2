# 02 — Expire Hold (Cloud Tasks)

Este documento descreve a transação crítica do Hotelly V2, com:
- objetivo e invariantes
- locks (ordem fixa) para evitar race/deadlock
- SQL/pseudocódigo de referência (PostgreSQL)

> Regra global: ao tocar várias noites, iterar sempre em ordem **(room_type_id, date ASC)**.

## Objetivo
Expirar um hold ACTIVE após `expires_at`, liberando inventário (`inv_held--`) de forma idempotente.

## Entrada
- `property_id`
- `hold_id`
- `task_id` (para dedupe em `processed_events`)
- `now` (UTC)

## Saída
- `holds.status = expired` (se aplicável)
- inventário liberado

## Invariantes
- Expirar duas vezes não pode liberar inventário duas vezes.
- Se o hold já foi convertido/cancelado/expirado, operação é no-op.

## Locks e concorrência
- `SELECT ... FOR UPDATE` no hold para serializar com `convert_hold` e `cancel_hold`.

## SQL/pseudocódigo (referência)
```sql
BEGIN;

-- 0) Dedupe do job/task
INSERT INTO processed_events(property_id, source, external_id)
VALUES (:property_id, 'tasks', :task_id)
ON CONFLICT (property_id, source, external_id) DO NOTHING;

-- Se já existia, sair (idempotente)

-- 1) Lock do hold
SELECT status, expires_at
FROM holds
WHERE id = :hold_id AND property_id = :property_id
FOR UPDATE;

-- 2) Guardas idempotentes
-- Se status != 'active' -> COMMIT
-- Se now() < expires_at -> COMMIT (ainda não expira)

-- 3) Atualizar status
UPDATE holds
SET status = 'expired', updated_at = now()
WHERE id = :hold_id AND property_id = :property_id AND status = 'active';

-- 4) Liberar inventário por noite (ordem date ASC)
-- Para cada (room_type_id, date) em hold_nights:
UPDATE ari_days
SET inv_held = inv_held - 1, updated_at = now()
WHERE property_id = :property_id
  AND room_type_id = :room_type_id
  AND date = :date
  AND inv_held >= 1;

COMMIT;
```

## Observabilidade
- Logar: property_id, hold_id, task_id, status anterior e final (sem PII).
- Métrica: holds_expired_count, holds_expire_noop_count.
