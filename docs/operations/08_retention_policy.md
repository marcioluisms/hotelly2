# Política de Retenção e Limpeza (MVP/Piloto)

## Objetivo

Evitar crescimento indefinido de tabelas e manter custo/performance estáveis no piloto.

**Regra:** nada de PII em tabelas operacionais (ver `docs/domain/04_message_persistence.md`).

## Diretrizes

- Preferir retenções simples (dias) e limpeza periódica.
- Limpeza deve ser **idempotente** e segura.
- Execução recomendada: **Cloud Scheduler + Cloud Run Job** (ou worker interno).

## Retenção por tabela (MVP)

### `processed_events`
- **Retenção:** 90 dias
- **Motivo:** dedupe de retries e auditoria operacional curta
- **Query:**
```sql
DELETE FROM processed_events
WHERE processed_at < now() - interval '90 days';
```

### `outbox_events`
- **Retenção:** 180 dias (piloto)
- **Motivo:** métricas e auditoria leve
- **Query:**
```sql
DELETE FROM outbox_events
WHERE occurred_at < now() - interval '180 days';
```

### `idempotency_keys`
- **Retenção:** 30 dias (se `expires_at` preenchido) ou 30 dias por `created_at`
- **Query (preferida):**
```sql
DELETE FROM idempotency_keys
WHERE expires_at IS NOT NULL
  AND expires_at < now();
```
**Fallback:**
```sql
DELETE FROM idempotency_keys
WHERE created_at < now() - interval '30 days';
```

### `payments`
- **Retenção:** manter (entidade de negócio)

### `holds`
- **Retenção:** manter (entidade de negócio)
- Obs: status `expired` pode ser filtrado por período em queries; não deletar no MVP.

### `reservations`
- **Retenção:** manter (entidade de negócio)

## Frequência recomendada

- **Diária** (madrugada) para `processed_events`, `outbox_events`, `idempotency_keys`.

## Observabilidade mínima

- Emitir log por tabela: contagem deletada por execução.
- Nunca logar payload de registros.

## Segurança

- Job/worker deve operar com credenciais mínimas.
- Queries devem ser executadas em transação curta.
