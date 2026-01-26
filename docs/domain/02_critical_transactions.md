# Transações críticas — Guia de implementação (Hotelly V2)

**Status:** Ativo (referência de implementação)  
**Objetivo:** garantir **zero overbooking**, idempotência e concorrência correta no core transacional.

Este documento é **normativo** para as rotinas:
- `create_hold`
- `expire_hold`
- `cancel_hold`
- `confirm_payment → convert_hold`

Ele complementa:
- `docs/domain/01_state_machines.md`
- `docs/data/01_sql_schema_core.sql`
- `docs/operations/07_quality_gates.md`

---

# 1) Princípios de concorrência (para não ter bug)

## 1.1 Regra de ouro: inventário só muda dentro de transação curta

* Hold **reserva inventário** (inv_held +1 por noite)
* Confirm (após pagamento) **converte** (inv_held -1 e inv_booked +1 por noite)
* Expire/Cancel **libera** (inv_held -1 por noite)

Tudo isso em **uma transação** com lock nas linhas certas.

## 1.2 Ordem fixa de locks para evitar deadlock

Sempre que precisar tocar ARI de várias noites:

* lock/update em ordem **(room_type_id, date ASC)**

Essa ordem tem que ser idêntica em:

* create_hold
* expire_hold
* cancel_hold
* confirm_payment→convert_hold

---

# 2) Constraints e índices “faltando” (recomendo adicionar)

## 2.1 Idempotência de API (além de processed_events)

`processed_events` resolve webhooks/tasks. Para endpoints do seu próprio API (ex.: `POST /holds`), recomendo uma tabela:

### `idempotency_keys`

* `property_id`
* `scope` (text) — ex.: `create_hold`, `cancel_hold`
* `idempotency_key` (text)
* `request_hash` (text) — opcional
* `response_json` (jsonb) — ou referência
* `created_at`

**Unique:** `(property_id, scope, idempotency_key)`

Assim você responde igual em retry sem recriar hold.

## 2.2 Consistência “property-scoped” nos FKs

Hoje dá para referenciar `room_types(id)` de outra property (porque FK é só por `id`). Para reduzir risco, há duas abordagens:

* **Boa (mais limpa):** usar PK composta em entidades scoping (ex.: `(property_id, id)`), e FK sempre incluindo `property_id`.
* **MVP (aceitável):** manter como está, mas **validar em código** que `room_type.property_id == property_id` ao montar quote/hold. (Viável tecnicamente, menos migração agora.)

## 2.3 Índices operacionais mínimos

Adicione se ainda não estiverem:

* `holds(property_id, status, expires_at)` (já tem)
* `payments(property_id, hold_id, status)` (já tem)
* `processed_events(property_id, source, external_id)` unique (já tem)

---

# 3) Transação crítica: CREATE HOLD (garante zero overbooking)

## Entrada

* `property_id`
* `conversation_id`
* `quote_option_id` (inclui room_type_id, rate_plan_id, total, etc.)
* `checkin`, `checkout`
* `idempotency_key`

## Saída

* `hold_id`, `expires_at`

## SQL/pseudocódigo

```sql
BEGIN;

-- 0) Idempotência (API)
INSERT INTO idempotency_keys(property_id, scope, idempotency_key, created_at)
VALUES (:property_id, 'create_hold', :idem_key, now())
ON CONFLICT (property_id, scope, idempotency_key) DO NOTHING;

-- Se já existia, buscar hold associado e retornar (depende do seu design: pode guardar response_json)
-- SELECT response_json ...; COMMIT;

-- 1) Inserir hold como ACTIVE (ainda sem inventário aplicado? você escolhe)
INSERT INTO holds(id, property_id, conversation_id, quote_option_id, status, checkin, checkout, total_cents, currency, expires_at)
VALUES (gen_random_uuid(), :property_id, :conversation_id, :quote_option_id, 'active', :checkin, :checkout, :total_cents, :currency, :expires_at)
RETURNING id INTO :hold_id;

-- 2) Gerar linhas de noites (hold_nights) em ordem determinística
-- (isso pode ser feito no app, ou via generate_series)
-- Para cada date em [checkin, checkout):
INSERT INTO hold_nights(hold_id, property_id, room_type_id, date, qty)
VALUES (:hold_id, :property_id, :room_type_id, :date, 1);

-- 3) Atualizar ARI: reservar inventário (inv_held++)
-- IMPORTANTE: fazer updates em ordem (room_type_id, date ASC)
-- A atualização deve falhar se não houver inventário.
UPDATE ari_days
SET inv_held = inv_held + 1,
    updated_at = now()
WHERE property_id = :property_id
  AND room_type_id = :room_type_id
  AND date = :date
  AND stop_sell = false
  AND inv_total >= (inv_booked + inv_held + 1);

-- Verifique que o UPDATE afetou 1 linha para cada noite.
-- Se alguma noite afetou 0 linhas: ROLLBACK (sem hold ativo).
-- (no app: contar updates e comparar com número de noites)

COMMIT;
```

### Observação importante

O check constraint `inv_total >= inv_booked + inv_held` ajuda, mas **não substitui** a cláusula:
`inv_total >= inv_booked + inv_held + 1` no `WHERE`.
Essa cláusula é o que evita overbooking sob concorrência.

---

# 4) Transação crítica: EXPIRE HOLD (Cloud Tasks)

## Entrada

* `hold_id`
* `task_id` (para dedupe)
* “agora” (UTC)

## Saída

* hold -> expired (se ainda active)
* inventário liberado (inv_held--)

## SQL/pseudocódigo

```sql
BEGIN;

-- 0) Dedupe de task
INSERT INTO processed_events(property_id, source, external_id)
VALUES (:property_id, 'tasks', :task_id)
ON CONFLICT (property_id, source, external_id) DO NOTHING;

-- se já existia: COMMIT e exit (idempotente)

-- 1) Lock do hold
SELECT status, expires_at, quote_option_id
FROM holds
WHERE id = :hold_id AND property_id = :property_id
FOR UPDATE;

-- 2) Se status != 'active' -> COMMIT (idempotente)
-- 3) Se now() < expires_at -> COMMIT (não expira ainda)
-- 4) Marcar expired
UPDATE holds
SET status = 'expired', updated_at = now()
WHERE id = :hold_id AND status = 'active';

-- 5) Liberar ARI (inv_held--)
-- para cada noite em hold_nights, em ordem (room_type_id, date ASC)
UPDATE ari_days
SET inv_held = inv_held - 1,
    updated_at = now()
WHERE property_id = :property_id
  AND room_type_id = :room_type_id
  AND date = :date
  AND inv_held >= 1;

COMMIT;
```

---

# 5) Transação crítica: CANCEL HOLD (usuário/admin)

Mesma lógica do expire, só muda:

* verificação de permissão
* status final: `cancelled`

---

# 6) Transação crítica: STRIPE WEBHOOK → PAYMENT SUCCEEDED → CONVERT HOLD

Esse é o ponto onde bug costuma aparecer (duplicidade e race com expiração).

## Entrada

* Stripe `event.id`
* `checkout.session.id` (canonical)
* `metadata`: `property_id`, `hold_id`, `payment_id` (ou cria payment on the fly)

## Saída

* payment succeeded (idempotente)
* hold active -> converted (se ainda ativo e não expirado)
* reservation criada (unique por hold)

## SQL/pseudocódigo

```sql
BEGIN;

-- 0) Dedupe webhook
INSERT INTO processed_events(property_id, source, external_id)
VALUES (:property_id, 'stripe', :stripe_event_id)
ON CONFLICT (property_id, source, external_id) DO NOTHING;

-- se já existia: COMMIT e exit

-- 1) Upsert payment (dedupe por provider_object_id)
INSERT INTO payments(property_id, conversation_id, hold_id, provider, provider_object_id,
                     status, amount_cents, currency, created_at, updated_at)
VALUES (:property_id, :conversation_id, :hold_id, 'stripe', :checkout_session_id,
        'succeeded', :amount_cents, :currency, now(), now())
ON CONFLICT (property_id, provider, provider_object_id)
DO UPDATE SET status='succeeded', updated_at=now();

-- 2) Lock hold e validar que ainda pode converter
SELECT status, expires_at, quote_option_id
FROM holds
WHERE id = :hold_id AND property_id = :property_id
FOR UPDATE;

-- Se status != 'active': COMMIT (idempotente)
-- Se now() > expires_at: 
--   não criar reserva; (opção) marcar pagamento como "needs_manual" via status/flag, ou criar ticket/outbox
--   COMMIT

-- 3) Converter inventário: inv_held-- e inv_booked++ por noite
-- (ordem room_type_id, date ASC)
UPDATE ari_days
SET inv_held = inv_held - 1,
    inv_booked = inv_booked + 1,
    updated_at = now()
WHERE property_id = :property_id
  AND room_type_id = :room_type_id
  AND date = :date
  AND inv_held >= 1;

-- Validar que todas as noites foram atualizadas.
-- Se falhar, ROLLBACK (algo muito errado: dado inconsistente)

-- 4) Criar reservation (unique por hold)
INSERT INTO reservations(property_id, conversation_id, hold_id, status, checkin, checkout, total_cents, currency)
VALUES (:property_id, :conversation_id, :hold_id, 'confirmed', :checkin, :checkout, :total_cents, :currency)
ON CONFLICT (property_id, hold_id) DO NOTHING;

-- 5) Marcar hold como converted
UPDATE holds
SET status = 'converted', updated_at = now()
WHERE id = :hold_id AND status = 'active';

COMMIT;
```

### O que isso garante

* Stripe mandou webhook 2x: `processed_events` bloqueia.
* Worker tentou converter 2x: `reservations unique (property_id, hold_id)` bloqueia.
* Expire hold rodou ao mesmo tempo: `FOR UPDATE` no hold serializa.

---

# 7) Quote não precisa lock, mas precisa consistência

Quote pode ser `READ COMMITTED` e não “segurar” inventário.

Regras:

* quote calcula disponibilidade com base em `inv_total - inv_booked - inv_held`
* quote é “melhor esforço”: a garantia real vem no create_hold

---

# 8) Onde o assíncrono entra (sem dor)

* WhatsApp inbound: grava `messages` + dedupe → Task “handle_message”
* IA roda no task: decide ação → cria quote/hold etc.
* Delay humano: Task “send_message_delayed”
* Stripe webhook: dedupe + Task “handle_stripe_event” → converte hold em transação

---

# 9) Checklist de “implementação correta” (se falhar, vai dar bug)

* Todas as mutações de inventário usam `WHERE inv_total >= inv_booked + inv_held + delta`
* Hold, expire, cancel, convert: lock no hold com `FOR UPDATE`
* Ordem fixa de updates em ARI (room_type_id, date ASC)
* Dedupe: `processed_events` (webhooks/tasks) + `idempotency_keys` (API)
* Reservation unique por hold
* Stripe canonical: `checkout.session.completed` e `checkout.session.id`

---

## Nota de escopo (MVP)
- **Obrigatório no MVP:** locks/ordem fixa, invariantes de inventário, dedupe (`processed_events`) e transações atômicas.
- **Recomendado (MVP+):** `idempotency_keys` para endpoints internos (se expor API pública cedo), e FKs property-scoped (PK composta) quando estabilizar o schema.

## Erros clássicos que este guia evita
- Overbooking por race condition (quote ≠ hold)
- Deadlock por ordem inconsistente de locks
- Cobrança sem reserva por dedupe incompleto
- Hold “preso” por falta de idempotência/reprocess
