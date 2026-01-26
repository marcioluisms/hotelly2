# ADR-005 — Public/Worker split (Cloud Run)

**Status:** Accepted
**Data:** 2026-01-26
**Contexto:** Hotelly V2

## Contexto

O Hotelly V2 terá:
- endpoints públicos (webhooks WhatsApp/Stripe e APIs de leitura/saúde);
- processamento assíncrono (expirar holds, reconciliações, tarefas internas);
- requisitos de segurança: minimizar superfície pública, reduzir risco de execução indevida e limitar permissões.

Já foi decidido que o core transacional é o Postgres (Cloud SQL) e que a execução deve suportar idempotência e dedupe via `processed_events` e `idempotency_keys`.

## Decisão

Dividimos a execução em **dois serviços Cloud Run**:

1) **public-api**
- expõe endpoints HTTP públicos necessários (webhooks e APIs públicas mínimas);
- valida assinatura/auth do provedor (ex.: Stripe signature, WhatsApp provider signature);
- registra dedupe/idempotência quando aplicável;
- enfileira trabalho para processamento assíncrono (Cloud Tasks / Jobs), sem executar lógica pesada.

2) **worker**
- não expõe endpoints públicos (ingress interno ou sem rota pública);
- executa tarefas assíncronas e rotinas (ex.: expirar holds, confirmar pagamento, reconciliações);
- aplica transações críticas no Postgres (com locks e invariantes);
- emite `outbox_events` como trilha de auditoria (sem PII).

## Motivação

- **Segurança:** reduzimos a superfície exposta e isolamos permissões.
- **Confiabilidade:** o public-api faz trabalho mínimo e devolve 2xx rápido; o worker pode ter timeouts/retries controlados.
- **Escalabilidade:** perfis de scaling distintos (picos de webhooks vs processamento).
- **Operação:** logs e métricas por componente; troubleshooting mais rápido.

## Consequências

### Permissões e identidade
- `public-api` precisa apenas do mínimo: enfileirar tasks e acessar segredos necessários para validação de assinatura.
- `worker` precisa de acesso ao Postgres e a segredos necessários para integrações e execução de tarefas.

### Rede/Conectividade
- Ambos conectam ao Cloud SQL via Cloud SQL Connector/Auth Proxy (conforme decisão de stack).
- `worker` deve operar com ingress restrito (preferência: interno).

### Contratos internos
- Payload interno de tasks deve ser **mínimo**, sem PII, e versionado.
- O `correlation_id` deve ser propagado de `public-api` -> `worker` -> `outbox_events`.

### Observabilidade
- Métricas separadas por serviço: taxa de webhooks, latency, retries de tasks, falhas transacionais.
- Logs com redaction obrigatório (nunca payload bruto de provedor).

## Alternativas Consideradas

1) **Um único serviço**
- Simples, mas aumenta superfície pública e mistura workloads com perfis distintos.

2) **Worker como Cloud Run Jobs apenas**
- Pode funcionar para rotinas; ainda precisamos de consumo de tasks/eventos.

## Notas de Implementação

- Endpoints públicos devem fazer:
  1) validação assinatura/auth,
  2) dedupe/idempotência (quando aplicável),
  3) enqueue,
  4) responder 2xx.
- A lógica crítica deve ficar no worker e ser transacional.
