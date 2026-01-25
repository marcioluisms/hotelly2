# 03 — Cancel Hold (User/Admin)

Este documento descreve a transação crítica do Hotelly V2, com:
- objetivo e invariantes
- locks (ordem fixa) para evitar race/deadlock
- SQL/pseudocódigo de referência (PostgreSQL)

> Regra global: ao tocar várias noites, iterar sempre em ordem **(room_type_id, date ASC)**.

## Objetivo
Cancelar um hold ACTIVE por decisão de usuário/admin, liberando inventário.

## Entrada
- `property_id`
- `hold_id`
- `actor` (user/admin/system)
- `idempotency_key` (recomendado)

## Saída
- `holds.status = cancelled`
- inventário liberado

## Invariantes
- Cancelar duas vezes não pode liberar inventário duas vezes.
- Se já convertido/expirado, operação é no-op (ou erro de negócio, conforme UX).

## Locks e concorrência
- `SELECT ... FOR UPDATE` no hold.
- Ordem fixa nas noites (date ASC).

## SQL/pseudocódigo (referência)
```sql
BEGIN;

-- (Opcional) Idempotência
-- INSERT INTO idempotency_keys(property_id, scope, idempotency_key, created_at)
-- VALUES (:property_id, 'cancel_hold', :idempotency_key, now())
-- ON CONFLICT (...) DO NOTHING;

-- 1) Lock hold
SELECT status
FROM holds
WHERE id = :hold_id AND property_id = :property_id
FOR UPDATE;

-- 2) Se status != 'active' -> COMMIT (no-op)
UPDATE holds
SET status = 'cancelled', updated_at = now()
WHERE id = :hold_id AND property_id = :property_id AND status = 'active';

-- 3) Liberar inventário (inv_held--)
UPDATE ari_days
SET inv_held = inv_held - 1, updated_at = now()
WHERE property_id = :property_id
  AND room_type_id = :room_type_id
  AND date = :date
  AND inv_held >= 1;

COMMIT;
```

## Notas de produto (MVP)
- Se cancelamento acontece por “timeout do usuário”, considere usar o mesmo mecanismo de expiração (task) para simplificar.
