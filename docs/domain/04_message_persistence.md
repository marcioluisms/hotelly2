# Persistência de Mensagens (WhatsApp) — Decisão

## Decisão (MVP/Piloto)

**Não persistimos** mensagens normalizadas (inbound/outbound) no Postgres no MVP/Piloto.

Motivo: reduzir risco de PII, escopo operacional e requisitos de compliance/criptografia.

## O que fica persistido (permitido)

Para operação e debug, persistimos apenas:
- `processed_events` (dedupe/idempotência de eventos externos e tasks)
- entidades transacionais (holds, payments, reservations)
- `outbox_events` com payload mínimo (sem PII)

Além disso:
- logs devem ser redigidos (sem payload bruto).
- `contact_hash` pode existir em `conversations` (hash, sem telefone bruto).

## Quando revisitamos

Se o piloto exigir auditoria de conversa (ex.: disputas, chargeback, suporte), criar uma story específica para:
- definir schema e retenção,
- criptografia e chaves (KMS),
- política de redaction,
- controles de acesso.

Até lá: o sistema deve operar apenas com o necessário para transação e observabilidade mínima.
