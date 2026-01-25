# ADR-001 — Banco de dados: Cloud SQL for PostgreSQL como fonte da verdade
**Status:** Accepted  
**Data:** 2026-01-25  
**Decisores:** Produto/Engenharia (Hotelly V2)  

## Contexto
O Hotelly V2 precisa de um **núcleo transacional** confiável para:
- segurar inventário (ARI) sem overbooking sob concorrência,
- manter **holds** e sua expiração/cancelamento,
- converter pagamento em reserva de forma idempotente (Stripe),
- suportar retries (webhooks/tasks) com dedupe.

A V1 teve sintomas típicos de inconsciência transacional (idempotência parcial, múltiplos caminhos, semântica de ACK e reconciliação fraca). A V2 precisa de:
- **atomicidade** (tudo ou nada),
- **locks explícitos** (`SELECT ... FOR UPDATE`) onde compete,
- **constraints** (UNIQUE) como segunda linha de defesa,
- rastreabilidade (outbox + processed_events),
- operação simples no piloto (execução solo).

## Decisão
1) O banco de dados transacional do Hotelly V2 será **Cloud SQL for PostgreSQL** e será a **fonte da verdade** (SoT).
2) O acesso **Cloud Run → Cloud SQL** será feito via **Cloud SQL Connector / Auth Proxy com IAM**, usando **Public IP** e **sem Authorized Networks**.
3) **Single-region** (us-central1) e **sem HA no piloto** (até ~10 pousadas), com plano explícito de upgrade para HA quando houver gatilhos.
4) **Modelo multi-tenant:** um único banco por ambiente (dev/staging/prod), com segregação por `property_id` (app-level tenancy). Não faremos “DB por pousada” no MVP.
5) **Migrações versionadas** são obrigatórias; alterações de schema sem migração são proibidas.

## Drivers (por que esta decisão)
- **Transações e consistência fortes:** PostgreSQL entrega invariantes sob concorrência (locks + constraints) essenciais para inventário/holds/pagamentos.
- **Operação gerenciada:** Cloud SQL reduz carga operacional vs. autogerenciar Postgres.
- **Simplicidade para o piloto:** single instance sem HA reduz custo e complexidade, mantendo caminho claro para evolução.
- **Integração nativa com Cloud Run:** Connector/Auth Proxy com IAM reduz superfície (sem IP allowlist).

## Alternativas consideradas
### A1) Firestore como SoT
**Prós:** facilidade de uso e escala; menos modelagem relacional.  
**Contras (crítico para V2):** consistência/concorrência e invariantes transacionais mais difíceis; tende a reintroduzir problemas típicos da V1.

### A2) Supabase/Neon/DBaaS externo
**Prós:** setup rápido; ferramentas adicionais.  
**Contras:** maior dependência externa; variação de SLAs; integração com GCP menos direta; governança e custo menos previsíveis.

### A3) Postgres autogerenciado (VM/K8s)
**Prós:** controle total.  
**Contras (inaceitável no piloto):** custo operacional alto (backup/patching/monitoring/DR).

**Escolha:** Cloud SQL Postgres como melhor trade-off para transação + simplicidade.

## Regras derivadas (guardrails técnicos)
1) **Integridade por constraints**
   - Dedupe de eventos: `processed_events(source, external_id)` UNIQUE.
   - Reserva 1:1 por hold: `reservations(property_id, hold_id)` UNIQUE.
   - Payment canonical: `payments(property_id, provider, provider_object_id)` UNIQUE.
   - Idempotency keys persistidas: `idempotency_keys(property_id, scope, idempotency_key)` UNIQUE.
2) **Concorrência controlada**
   - Expire/Cancel/Convert: `SELECT ... FOR UPDATE` no hold.
   - Updates de ARI em ordem determinística (por `room_type_id`, `date` ASC) para reduzir deadlocks.
3) **Webhooks/Tasks são idempotentes**
   - Receipt durável (processed_events/enqueue) antes de 2xx onde aplicável.
4) **Sem PII em logs**
   - Proibido logar payload bruto (webhooks, mensagens). Redaction obrigatória.
5) **Migrações como gate**
   - CI deve falhar se schema esperado não bater (migrate up + smoke).

## Conectividade (Cloud Run → Cloud SQL)
- **Método:** Cloud SQL Connector/Auth Proxy com IAM.
- **Rede:** Public IP, **sem Authorized Networks**.
- **Por quê:** reduz setup de rede no piloto (VPC/Private IP), mantendo autenticação robusta via IAM.
- **Limitações:** dependência do proxy/connector; atenção a limites de conexão e timeouts.

### Pool e limites de conexão
- Cloud SQL tem limite de conexões; Cloud Run escala horizontalmente.
- Regras:
  - limitar `max_connections` efetivo por instância (pool pequeno e previsível),
  - preferir filas/Tasks e transações curtas,
  - evitar “chatty queries”.
- Se necessário, considerar **pooler** (ex.: PgBouncer) na evolução, não no MVP.

## Backup, PITR e manutenção
- **Backups automáticos** habilitados por ambiente.
- **PITR (Point-in-Time Recovery)** recomendado para staging/prod assim que iniciar piloto real.
- **Janela de manutenção** definida (fora do horário de maior uso).
- **Testar restore** periodicamente (runbook), especialmente antes de piloto com pagantes.

## Segurança e segregação de ambientes
- Bancos separados por ambiente (dev/staging/prod).
- Usuários/roles distintos por ambiente.
- Segredos (strings/creds) em **Secret Manager**; nunca em repositório.
- Princípio do menor privilégio:
  - app user com permissões só no schema necessário,
  - role de migração separada (aplicada em CI/CD controlado).

## Observabilidade do banco
- Habilitar Cloud SQL Insights (quando aplicável) e métricas:
  - conexões ativas,
  - latência,
  - locks/deadlocks,
  - erros por tipo.
- Logging:
  - slow queries acima de limiar definido,
  - dashboards em observability doc.
- Monitorar:
  - crescimento de `processed_events`, `outbox_events` (retenção/cleanup planejado).

## Gatilhos para habilitar HA / evoluir arquitetura
Reavaliar e/ou habilitar HA quando ocorrer qualquer um:
1) Mais de ~10 pousadas ativas e tráfego relevante,
2) necessidade de SLA/alta disponibilidade,
3) incidentes recorrentes por indisponibilidade,
4) crescimento de receita/risco que justifique custo.

Evoluções possíveis:
- habilitar HA no Cloud SQL (regional),
- revisar conectividade para Private IP (quando rede/VPC fizer sentido),
- adicionar pooler,
- estratégia de DR multi-região (quando necessário).

## Checklist de aceitação
- [ ] Cloud SQL Postgres criado em us-central1 por ambiente.
- [ ] Backups automáticos ativos; PITR definido para staging/prod quando iniciar piloto real.
- [ ] Cloud Run conecta via Cloud SQL Connector/Auth Proxy com IAM; sem Authorized Networks.
- [ ] Migrações versionadas e rodando como gate em CI.
- [ ] Constraints críticas implementadas (processed_events, reservations, payments, idempotency_keys).
- [ ] Runbook contém procedimento de restore e verificação.

## Referências
- ADR-003: região us-central1.
- Guia de transações críticas e schemas SQL do projeto (core).
- Docs operacionais: CI/CD, test plan, observability, runbook.
