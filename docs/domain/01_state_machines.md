# State Machines — Hotelly V2 (MVP)

## Objetivo
Definir estados e transições mínimas do domínio para orientar:
- implementação de handlers (`/webhooks/*`, `/tasks/*`) — TARGET
- constraints no Postgres (UNIQUEs e invariantes)
- runbook e reprocessamento idempotente

**Nota:** no estado atual do repo, essas máquinas são especificação do sistema-alvo.

---

## 1) Conversation
Representa a sessão de conversa/contexto com a pousada e o hóspede.

### Estados (MVP)
- `open`: conversa ativa, ainda sem hold ativo para pagamento
- `waiting_payment`: existe um hold ativo associado aguardando pagamento
- `confirmed`: existe reserva confirmada (derivada da conversão bem-sucedida)
- `closed`: conversa encerrada (manual ou por timeout de inatividade) — opcional no MVP

### Transições (MVP)
- `open → waiting_payment`
  - gatilho: hold criado com sucesso
  - invariantes:
    - no máximo 1 hold ativo por conversa (recomendado; pode ser relaxado se o produto permitir)
- `waiting_payment → confirmed`
  - gatilho: pagamento confirmado + conversão hold→reservation concluída
- `waiting_payment → open`
  - gatilho: hold expirado/cancelado sem pagamento

### Eventos/outbox (TARGET)
- `conversation.waiting_payment`
- `conversation.confirmed`

---

## 2) Hold
Bloqueio temporário de inventário (ARI) para garantir "zero overbooking".

### Estados (MVP)
- `active`: inventário bloqueado (`inv_held` refletindo hold_nights)
- `expired`: expirou e liberou inventário
- `cancelled`: cancelado manualmente e liberou inventário (opcional no MVP)
- `converted`: convertido em reservation (inventário migra `held → booked`)

### Transições (MVP)
- `active → expired`
  - gatilho: `now() >= expires_at` e execução do expire_hold (task/worker)
  - invariantes:
    - após expiração, `inv_held` deve ter sido decrementado exatamente para cada `hold_nights`
    - não pode ficar `inv_held` negativo
- `active → converted`
  - gatilho: pagamento confirmado + conversão executada com sucesso
  - invariantes:
    - reserva única por hold: `UNIQUE(reservations.property_id, reservations.hold_id)`
    - para cada noite: `inv_held--` e `inv_booked++` (ordem determinística por data)
    - não pode ficar `inv_held` negativo
- `active → cancelled` (opcional)
  - gatilho: cancelamento manual/decisão de produto
  - invariantes: liberar inventário como no expire

### Eventos/outbox (TARGET)
- `hold.created`
- `hold.expired`
- `hold.cancelled`
- `hold.converted`

---

## 3) Payment (Stripe)
Registro interno do estado de pagamento associado a um hold.

### Estados (MVP)
- `created`: checkout session criada e persistida
- `pending`: checkout iniciado mas não confirmado como pago
- `succeeded`: confirmado como pago (ex.: `checkout.session.completed` + `payment_status == "paid"`)
- `failed`: expirado/cancelado/erro definitivo
- `needs_manual`: inconsistente (ex.: pagamento após hold expirar; dados incompletos)

### Transições (MVP)
- `created → pending`
  - gatilho: webhook indica progresso, mas não "paid"
- `pending|created → succeeded`
  - gatilho: webhook canônico confirma `paid`
  - invariantes:
    - pode disparar conversão do hold, mas a conversão é idempotente (UNIQUE reservation por hold)
- `created|pending → failed`
  - gatilho: checkout expira/cancela (opcional no MVP)
- `* → needs_manual`
  - gatilho: violação de pré-condição (ex.: hold expirado antes da confirmação; metadata faltando; conflito)

### Eventos/outbox (TARGET)
- `payment.created`
- `payment.succeeded`
- `payment.failed`
- `payment.needs_manual`

---

## 4) Reservation
Reserva confirmada (resultado final da conversão).

### Estados (MVP)
- `confirmed`
- `cancelled` (opcional no MVP)

### Invariantes (MVP)
- `UNIQUE(property_id, hold_id)` garante "no máximo 1 reserva por hold"
- ARI consistente:
  - `inv_total >= inv_booked + inv_held` para todas as noites
  - nenhum valor negativo

### Eventos/outbox (TARGET)
- `reservation.confirmed`
- `reservation.cancelled`
