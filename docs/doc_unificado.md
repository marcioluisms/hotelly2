# Hotelly — Base Técnica Unificada

**Objetivo:** concentrar *tudo* que é relevante para operação e manutenção do sistema (infra, contratos, banco, pipelines, endpoints, runbooks).  
**Regra de ouro:** evitar perda de informação técnica e evitar “atalhos” fora dos contratos aqui descritos.

**Precedência em conflitos:** regras deste arquivo (Doc Unificado) > ADRs e docs auxiliares. *ADRs são histórico; podem estar supersedidas.*


- Incluída segurança do webhook Evolution (secret + header).
- Incluídos: contrato de saída da IA (`IntentOutput`), retenção/limpeza, limites do piloto + capacidade de suporte.
- Incluídos: quality gates (G0/G1/G3–G5) + critérios de incidente SEV0 (stop-ship).
- Incluídas recomendações de naming por ambiente (secrets/filas) e nota de persistência de mensagens (MVP/Piloto).

---

## Governança e Fonte da Verdade

- **Este arquivo é normativo** para regras de negócio, contratos, invariantes e operação.
- **Schema do banco:** migrations em `migrations/` (Alembic) são a verdade executável; este doc define *invariantes* e *constraints* que devem existir. Divergência = bug.
- **/docs/adr/**: histórico de decisões (ADR). Não edite para “atualizar regra”; registre nova decisão e ajuste este doc.
- **/docs/operations/sql/**: scripts operacionais (consulta/limpeza). Podem ser mantidos fora deste arquivo.
- **Segredos:** aqui só entram **nomes de secrets/env vars** e regras de uso. Nunca versionar valores.

## 0) URLs e decisões atuais

### 0.1 Domínios / serviços

- **Admin Staging:** `https://dash.hotelly.ia.br` → `hotelly-admin-staging` (Cloud Run)
- **Admin Prod:** `https://adm.hotelly.ia.br` → `hotelly-admin` (Cloud Run)
- **Public App/API:** `https://app.hotelly.ia.br` (Cloud Run)
- **Domínio técnico raiz:** `hotelly.ia.br` → redirect 308 para `hotelly.com.br`

---

### 0.2 Limites do piloto e capacidade de suporte
- Piloto: até **10 pousadas**, sem HA; usuários cientes de falhas; foco em observabilidade e aprendizado.
- Estimativa de suporte (1 pessoa): **15–20** clientes confortável; **25** no limite.

## 1) Visão geral da arquitetura

### 1.1 Stack
- **GCP + Cloud Run + FastAPI + Cloud SQL Postgres + Cloud Tasks + Secret Manager**
- Integrações: **Stripe** (pagamento) e **WhatsApp via Evolution API** (MVP).

### 1.2 Split obrigatório em 2 serviços (Cloud Run)

**1) `hotelly-public`**
- Expõe APIs/webhooks públicos.
- Faz apenas:
  1) validação de auth/assinaturas
  2) *receipt/dedupe* durável quando aplicável
  3) enqueue (Cloud Tasks)
  4) responde **2xx**
- Não executa lógica pesada nem transação crítica.

**2) `hotelly-worker`**
- Privado (ingress interno).
- Consome tasks e executa transações críticas no Postgres.
- Emite **outbox** (PII-safe).

### 1.3 Princípios de design (não-negociáveis)
- Core transacional é **determinístico**: IA não decide estado crítico.
- Ações críticas precisam garantir: **0 overbooking**, **idempotência real**, **concorrência correta**.
- `*_at` é **TIMESTAMPTZ UTC**; `date` é **DATE** (sem hora).
- Valores monetários: sempre `*_cents` (INT). Moeda: `currency` ISO-4217 (ex.: `BRL`).


#### 1.3.1 Nomenclatura canônica (schema + contratos)

Convenções obrigatórias para nomes de campos no schema e documentação:

| Padrão | Tipo | Descrição | Exemplo |
|--------|------|-----------|---------|
| `*_cents` | INT | Valores monetários em centavos | `amount_cents`, `total_cents` |
| `total_cents` | INT | Valor total em holds/reservations | `holds.total_cents` |
| `amount_cents` | INT | Valor em payments | `payments.amount_cents` |
| `base_rate_cents` | INT | Diária base em ARI | `ari_days.base_rate_cents` |
| `currency` | TEXT | Código ISO 4217 | `BRL`, `USD` |
| `*_at` | TIMESTAMPTZ | Timestamps (sempre UTC) | `created_at`, `expires_at` |
| `*_id` | TEXT ou UUID | Identificadores | `property_id`, `hold_id` |
| `date` | DATE | Datas de calendário (sem hora) | `ari_days.date`, `holds.checkin` |

**Regra:** nunca usar `total_amount_cents`, `day`, ou variações não listadas acima.

---

# ÍNDICE DEFINITIVO — DOCUMENTAÇÃO HOTELLY V2

### Legenda de maturidade

- **Status** (qualidade do texto): 🟢 PRONTO | 🟡 PARCIAL | 🔴 A COMPLETAR
- **Maturidade** (o que dá para executar hoje): ✅ EXECUTÁVEL NO REPO | ⚠️ CONCEITUAL/DEPENDE DE ARTEFATOS | 🎯 TARGET (pós-MVP)

#### 1.3.2 IA — contrato de saída (`IntentOutput` v1.0)
- IA no MVP é **apenas roteamento/extração**; core segue determinístico.
- Entrada para IA deve ser **redigida**; nunca enviar payload bruto de webhook, tokens, segredos, ou PII não essencial.
- Saída da IA é **JSON estrito** (schema versionado). Se JSON inválido/enum desconhecido/slots incoerentes ⇒ fallback determinístico.

**Schema (resumo):**
- required: `schema_version` = "1.0"
- required: `intent` ∈ {`quote_request`, `checkout_request`, `cancel_request`, `human_handoff`, `unknown`}
- required: `confidence` ∈ [0,1]
- optional: `entities` { `checkin`(date), `checkout`(date), `guest_count`(1..20), `room_type_id`(string) }
- optional: `reason` (<= 200 chars)

**Regra de prompt:** retornar **apenas JSON**; sem PII; se incerto ⇒ `intent="unknown"` + `reason`.

---

## 2) Segurança, tenancy e RBAC (property-scoped)

### 2.1 Regra de ouro (uniforme)
Para endpoints “de dashboard”:
- `property_id` é **obrigatório via querystring**: `?property_id=...`
- validado por `require_property_role(min_role)`
- **não aceitar** `property_id` no body (contrato uniforme)

### 2.2 Autenticação (Clerk / JWT)
Config de produção (referência):
- Issuer: `https://clerk.hotelly.ia.br`
- JWKS: `https://clerk.hotelly.ia.br/.well-known/jwks.json`
- Audience: `hotelly-api`
- JWT Template: `hotelly-api` (lifetime ~600s)

Claims esperados:
- `sub` (user_id)
- `aud = hotelly-api`
- `metadata.property_ids` (lista de properties)
- `metadata.role ∈ {owner, manager, receptionist}`

2.2.1 Autorização DB-backed (Regra de Ouro)

A autorização é 100% baseada no banco de dados (Postgres).

O Clerk é utilizado apenas para Autenticação (identidade do usuário).

Metadados do Clerk (property_ids, role) não são consultados pelo backend para controle de acesso; a fonte da verdade são as tabelas users e user_property_roles.

### 2.5 Hierarquia de roles (RBAC) — Sprint 1.13

Definida em `src/hotelly/api/rbac.py` (`ROLE_HIERARCHY`):

| Nível | Role | Descrição |
|---|---|---|
| 0 | `viewer` | Leitura básica (reservas, quartos, ocupação). |
| 1 | `governance` | **Governança de quartos** — pode atualizar `governance_status` via `PATCH /rooms/{id}/governance`. Não pode criar/alterar reservas nem acessar endpoints financeiros. **Restrição PII:** endpoints de listagem de reservas que exigem apenas `viewer` também são acessíveis ao `governance`; isolamento total de PII nesses endpoints requer guards por endpoint (work item aberto). |
| 2 | `staff` | Operações de front-desk: check-in, check-out, atribuição de quarto. |
| 3 | `manager` | Gestão completa: tarifas, inventário, configurações. |
| 4 | `owner` | Acesso irrestrito + gerenciamento de equipe (RBAC UI). |

**Regra de autorização:** `require_property_role(min_role)` aceita qualquer role com nível ≥ `min_role`.
Exemplo: `require_property_role("governance")` aceita `governance`, `staff`, `manager` e `owner`.

**Constraint DB (`user_property_roles`):**
```sql
CHECK (role IN ('owner', 'manager', 'staff', 'viewer', 'governance'))
```
*(migração `027_governance` — Sprint 1.13)*

---


### 2.3 Webhooks WhatsApp — segurança (Meta + Evolution) [Security P0]
- **Meta** (`/webhooks/whatsapp/meta`): fora de local, `META_APP_SECRET` é obrigatório. Se ausente, **não processar** o payload (fail-closed) e responder **200 OK** para evitar retry.
- Local dev (`TASKS_OIDC_AUDIENCE == "hotelly-tasks-local"`): pode haver bypass, mas deve logar **warning**.
- **Evolution** (`/webhooks/whatsapp/evolution`): exigir `EVOLUTION_WEBHOOK_SECRET` e header `X-Webhook-Secret` com match.
- Fora de local: se ausente/errado ⇒ **401**.

### 2.4 Tasks Auth / OIDC Audience (ajuste obrigatório)
- Cloud Tasks → worker: exigir **OIDC** (service account invoker).
- `TASKS_OIDC_AUDIENCE` deve bater com a `status.url` do worker (por ambiente).

## 3) PII, outbox e identidade de contato (WhatsApp)

### 3.1 Definição e regra de ouro (PII)
PII inclui: telefone, conteúdo de mensagem, email, documento, endereço, nome ligado ao contato e identificadores “sendable” (ex.: `remote_jid` / `wa_id`).  
**É proibido** logar payload bruto, request body, mensagens, telefone, nome, `remote_jid`. Isso é incidente.

### 3.2 Worker PII-free
O worker de mensagens (`handle-message`) é **PII-free**:
- não recebe nem persiste `text`, `remote_jid`, `payload/raw`, nome, telefone.

### 3.3 Outbox (obrigatório)
- `outbox_events` é **append-only**, payload mínimo e **sem PII**.
- Toda ação crítica (hold/payment/reservation) deve escrever outbox **na mesma transação**.
- É proibido colocar no payload: telefone/email/nome/endereço/documento/texto de chat, payload bruto Stripe/WhatsApp.

### 3.4 Contact hash + “vault” de destinatário (contact_refs)
- Pipeline identifica contato por `contact_hash` (hash com secret; não reversível sem secret).
- Resolução do destinatário outbound via “vault”:
  - Mapeamento conceitual: `(property_id, channel, contact_hash) → remote_jid` **criptografado**
  - Criptografia: **AES-256-GCM** com chave simétrica **`CONTACT_REFS_KEY`** (Secret Manager/env)
  - TTL: **24 horas** (configurável via código; aumentado de 1h para melhor usabilidade sem comprometer segurança)
  - Apenas sender (envio) lê o vault; `handle-message` não lê; worker não escreve no vault
  - Nunca logar `remote_jid` descriptografado
- Se vault não tiver entrada: **não envia** (comportamento intencional) e registra erro PII-safe.

---


### 3.5 Persistência de mensagens (MVP/Piloto)
- MVP/Piloto: **não persistir mensagens** (inbound/outbound) no Postgres.
- Persistir: `processed_events`, entidades transacionais (holds/payments/reservations), e `outbox_events` (mínimo, sem PII).

## 4) Inventário (ARI) e concorrência

### 4.1 Invariantes ARI
Inventário nunca negativo e nunca excedido:
- `inv_total >= inv_booked + inv_held` para todas as noites
- Guardas no `WHERE` dos updates (incrementa hold só se houver saldo; decrementa só se `inv_held >= 1`)
- Validar “1 linha por noite”; se alguma noite afetar 0 linhas ⇒ rollback (sem hold parcial)

### 4.2 Locking e deadlock avoidance
- Operações concorrentes no mesmo hold (expire/cancel/convert): `SELECT ... FOR UPDATE` no hold.
- Ao tocar várias noites: iterar sempre em ordem fixa **(room_type_id, date ASC)**.

---

## 5) Idempotência (end-to-end)

Idempotência ponta a ponta combina:
- `processed_events` (dedupe de eventos externos/tasks)
- `task_id` determinístico (Cloud Tasks)
- UNIQUEs no banco (última linha de defesa)
- `idempotency_keys` para endpoints com `Idempotency-Key` (escopo + key)

**Cloud Tasks:** `create_task` pode retornar **409 AlreadyExists** (dedupe por nome). Isso deve ser tratado como **sucesso idempotente** (não 500).

---

## 6) Banco de dados (Postgres)

### 6.1 Fonte da verdade
- Fonte da verdade do schema são as migrations em `migrations/` (Alembic).
- Arquivos auxiliares (ex.: `docs/data/*.sql`) são referência humana, não execução.

**`guests`: Entidade global de identidade (CRM). Campos normalizados (`email`, `phone` E.164) e preferências (`profile_data` JSONB). Sprint 1.10 [CONCLUÍDO] — migration `024_guests_crm`.**

| Coluna | Tipo | Constraints |
|---|---|---|
| `id` | UUID | PK, default `gen_random_uuid()` |
| `property_id` | TEXT | NOT NULL, FK → `properties(id)` ON DELETE CASCADE |
| `email` | TEXT | nullable |
| `phone` | TEXT | nullable |
| `full_name` | TEXT | NOT NULL |
| `display_name` | TEXT | nullable |
| `document_id` | TEXT | nullable |
| `document_type` | TEXT | nullable |
| `profile_data` | JSONB | NOT NULL, default `'{}'` |
| `created_at` | TIMESTAMPTZ | NOT NULL, default `now()` |
| `updated_at` | TIMESTAMPTZ | NOT NULL, default `now()` |
| `last_stay_at` | TIMESTAMPTZ | nullable |

Índices de unicidade (parciais, apenas quando o valor está presente):
- `uq_guests_property_email` — UNIQUE `(property_id, email) WHERE email IS NOT NULL`
- `uq_guests_property_phone` — UNIQUE `(property_id, phone) WHERE phone IS NOT NULL`

### 6.2 Constraints/guardrails (essenciais)
- Dedupe eventos: `processed_events(source, external_id)` **UNIQUE**
- 1 reserva por hold: `reservations(property_id, hold_id)` **UNIQUE**
- Payment canonical: `payments(property_id, provider, provider_object_id)` **UNIQUE**
- Idempotency keys: `idempotency_keys(property_id, scope, key)` **UNIQUE/PK**

**Schema invariants (Sprint 1.9):**
- `holds.guest_name` (TEXT, nullable) — snapshot imutável do nome do hóspede gravado no momento da criação do hold. O Worker lê este campo para montar a notificação WhatsApp sem precisar consultar `conversations`. Divergência entre `holds.guest_name` e `guests.full_name` é esperada e intencional (hold = snapshot; guests = perfil vivo).
- Tabela `payments` — registra todos os pagamentos via Stripe (`provider = 'stripe'`, `provider_object_id` = checkout session ID canônico). É a fonte de verdade para reconciliação financeira; o status transita `created → succeeded | needs_manual`. Nenhuma lógica de negócio deve consultar Stripe diretamente para checar status — sempre ler `payments.status`.

**Regras de negócio adicionadas na migração 032:**
- `properties.confirmation_threshold` (NUMERIC NOT NULL DEFAULT 1.0) — fração mínima do `total_cents` que deve ser coberta por pagamentos folio capturados para que a reserva seja automaticamente confirmada. Valor `1.0` = pagamento integral exigido. Valores entre 0 e 1 permitem confirmação com pagamento parcial (ex: sinal de 30% → `0.3`). Verificado em `folio_service._maybe_auto_confirm` após cada `POST /reservations/{id}/payments`.
- `reservations.guarantee_justification` (TEXT, nullable) — texto livre obrigatório informado pelo operador ao confirmar uma reserva manualmente via "Garantir Reserva". Persiste na linha da reserva e é replicado no campo `notes` do audit log como `"Manual Guarantee: <texto>"`. Nunca preenchido em confirmações automáticas (sistema).
- `payments.justification` (TEXT, nullable) — anotação opcional associada a um pagamento Stripe para fins de rastreabilidade.

### 6.3 Modelo de pricing por ocupação (PAX) — `room_type_rates`

Tabela canônica: `room_type_rates` (PK `(property_id, room_type_id, date)`).

Campos principais (centavos):
- Adultos: `price_1pax_cents`, `price_2pax_cents`, `price_3pax_cents`, `price_4pax_cents`
- Crianças (por bucket): `price_bucket1_chd_cents`, `price_bucket2_chd_cents`, `price_bucket3_chd_cents` (nullable)

Restrições (por dia):
- `min_nights`, `max_nights`, `closed_checkin`, `closed_checkout`, `is_blocked`

Compatibilidade `/rates`:
- **GET** retorna campos bucket **e** aliases legados (`price_1chd_cents`, `price_2chd_cents`, `price_3chd_cents`)
- **PUT** aceita **bucket** ou **legado**; se ambos presentes e divergentes ⇒ **400**

> Observação histórica: havia plano de coexistência (ADD colunas). Como o sistema ainda não estava em produção, a decisão operacional foi **RENAME no DB** e retrocompatibilidade apenas na API.

### 6.4 Políticas de crianças (fonte da verdade) — `property_child_age_buckets`
- Buckets por `property_id`:
  - exatamente 3 buckets (`bucket ∈ {1,2,3}`)
  - `min_age/max_age` dentro de `0..17` e `min_age <= max_age`
  - **cobertura completa 0..17 sem gaps**
  - **sem overlap garantido no DB** via **EXCLUDE constraint** (usa range; requer extensão `btree_gist`)

### 6.5 Política de cancelamento (fonte da verdade) — `property_cancellation_policy`
- 1 linha por `property_id`
- Campos:
  - `policy_type ∈ ('free','flexible','non_refundable')`
  - `free_until_days_before_checkin` (0..365)
  - `penalty_percent` (0..100)
  - `notes` (nullable)
  - `updated_at` default now()
- Checks de consistência:
  - `free`: `penalty_percent = 0`
  - `non_refundable`: `penalty_percent = 100` e `free_until_days_before_checkin = 0`
  - `flexible`: `penalty_percent` 1..100

### 6.6 Ocupação nas entidades transacionais (estado atual)
- `holds` e `reservations` persistem:
  - `adult_count` (SMALLINT)
  - `children_ages` (JSONB, default `[]`)
  - `guest_name` (TEXT, nullable) — **snapshot histórico**: cópia do nome no momento da reserva, mantida para auditoria mesmo que o perfil do hóspede seja atualizado posteriormente.
- `guest_count` foi removido (DB + código).

**Campos de contato em `holds` (Sprint 1.10 [CONCLUÍDO] — CRM Bridge, migration 025):**
- `holds.email` (TEXT, nullable) — e-mail capturado pelo fluxo de booking; usado por `upsert_guest()` como chave primária de deduplicação.
- `holds.phone` (TEXT, nullable) — telefone E.164; usado como chave secundária de deduplicação quando `email` é nulo.
- Ambos são nullable: holds criados por fluxos que ainda não capturam contato terão `NULL`; nesse caso `upsert_guest()` cria um perfil name-only e a deduplicação passa a funcionar automaticamente assim que o upstream preencher esses campos.

**Identidade do hóspede (Sprint 1.10 [CONCLUÍDO]):**
- `reservations.guest_id` (UUID, nullable, FK → `guests(id)`) — referência ao perfil normalizado. Populado por `upsert_guest()` no momento da conversão do hold.
- `reservations.guest_name` e `reservations.guest_id` coexistem: `guest_name` é o snapshot imutável; `guest_id` é o vínculo vivo ao CRM.
- Reservas anteriores ao Sprint 1.10 têm `guest_id = NULL`; isso é esperado e não constitui erro.

---


**Nota de legado (pricing crianças):** documentação antiga pode referir colunas como `price_1chd_cents`, `price_2chd_cents`, `price_3chd_cents` (legado). A fonte de verdade atual é `room_type_rates` + `property_child_age_buckets`, com compat só via API quando necessário.

## 7) Pricing/Quote (backend)

### 7.1 Contrato atual do quote
- Parâmetros primários: `adult_count` + `children_ages[]` (idades 0..17)
- Sem fallback para `ari_days.base_rate_cents` (fallback legado removido)
- Falha controlada gera `QuoteUnavailable(reason_code, meta)`; call-site loga `reason_code` e retorna `None` para o público (por enquanto)


**Histórico:** versões antigas do spec descreviam fallback de quote para `ari_days.base_rate_cents`. O comportamento atual **não deve** depender desse campo (DEPRECATED).

### 7.2 reason_codes mínimos (padronizados)
- Dados/política: `child_policy_missing`, `child_policy_incomplete`
- Tarifas: `rate_missing`, `pax_rate_missing`, `child_rate_missing`
- Ocupação: `occupancy_exceeded`
- Datas/ARI: `invalid_dates`, `no_ari_record`, `no_inventory`
- Genérico: `unexpected_error`

---

## 8) WhatsApp (Evolution) — pipeline e tasks

### 8.1 Pipeline único (obrigatório)
`inbound (public) → normalize → receipt/dedupe → enqueue → worker(handle-message) → outbox → sender(send-response)`

É proibido criar “caminho alternativo rápido” fora do pipeline (exceto debug/legado).

### 8.2 Provider (MVP)
- Evolution API (adapter único no MVP; Meta Cloud API pode entrar depois mantendo o mesmo contrato).

Config (produção — referência):
- Evolution URL: `https://edge.roda.ia.br/`
- Webhook: `https://app.hotelly.ia.br/webhooks/whatsapp/evolution`
- Header obrigatório no webhook: `X-Property-Id: <property_id>`
- Header obrigatório no webhook: `X-Webhook-Secret: <secret>` (match com `EVOLUTION_WEBHOOK_SECRET`; fora de local, ausente/errado ⇒ **401**)

Outbound via Evolution:
- `EVOLUTION_BASE_URL`
- `EVOLUTION_INSTANCE`
- `EVOLUTION_API_KEY` (secret)
- `EVOLUTION_SEND_PATH` opcional (default `/message/sendText/{instance}`)

### 8.3 Inbound contract (PII-free) para o worker
```json
{
  "provider": "evolution",
  "message_id": "string (dedupe key)",
  "property_id": "string",
  "correlation_id": "string",
  "contact_hash": "string (base64url, 32 chars)",
  "kind": "text|interactive|media|unknown",
  "received_at": "ISO8601 UTC"
}
```

### 8.4 Semântica do endpoint de envio (task) — `POST /tasks/whatsapp/send-response`
- Falha **transiente** (timeout/rede/5xx/429) ⇒ **HTTP 500** (habilita retry do Cloud Tasks)
- Falha **permanente** (401/403, `contact_ref_not_found`, template inválido, env/secret faltando) ⇒ **HTTP 200** com payload `{ "ok": false, "terminal": true, "error": "<code>" }` (para parar retry)
- No-op idempotente: se já enviado ⇒ **HTTP 200** `{ "ok": true, "already_sent": true }` e não chama provider

### 8.5 Guard durável de idempotência no outbound — `outbox_deliveries`
- UNIQUE por `(property_id, outbox_event_id)`
- Campos operacionais:
  - `status ∈ {sending, sent, failed_permanent}`
  - `attempt_count`
  - `last_error` (sanitizado, PII-safe)
  - `sent_at`, timestamps
- Lease anti-concorrência:
  - `status='sending'` com `updated_at` recente ⇒ retorna **500 lease_held**
  - lease stale permite takeover

### 8.6 Diagnóstico controlado de retry (staging)
Foi usado um diag hook *staging-only* para provar retry transiente via Cloud Tasks:
- Gates:
  - `APP_ENV=staging`
  - `STAGING_DIAG_ENABLE=true`
  - header `x-diag-force-transient: 1`
  - `property_id` canônico `pousada-staging`
- Efeito: grava `last_error="forced_transient"`, mantém `status='sending'` e retorna **HTTP 500**.

---

## 9) Stripe

### 9.1 Princípios
Webhook Stripe (public) faz:
- validar assinatura
- receipt/dedupe durável por `event.id`
- enqueue task
- responde 2xx

Conversão HOLD→RESERVATION só no worker (transação crítica).  
Nunca logar payload bruto Stripe.

### 9.2 Log allowlist (Stripe)
Permitido logar: `event.id`, `event.type`, `checkout_session_id`, `payment_id`, `hold_id`, `property_id`, `correlation_id`, status, duration_ms, attempts.

### 9.3 Config (produção — referência)
- Secrets:
  - `stripe-secret-key`
  - `stripe-webhook-secret`
- Env vars:
  - `STRIPE_SUCCESS_URL=https://app.hotelly.ia.br/stripe/success`
  - `STRIPE_CANCEL_URL=https://app.hotelly.ia.br/stripe/cancel`
- Webhook URL: `https://app.hotelly.ia.br/webhooks/stripe`
- Eventos: `checkout.session.completed`, `payment_intent.succeeded`

---

## 10) Logging e observabilidade

### 10.1 Logs estruturados (mínimos)
Sempre JSON por linha com: `severity`, `timestamp`, `service`, `env`, `correlation_id`, `event_name`.

### 10.2 Correlation ID end-to-end
- Se vier `X-Correlation-Id`, validar e reutilizar; senão gerar.
- Cloud Tasks devem propagar:
  - `X-Correlation-Id`
  - `X-Event-Source=tasks`

### 10.3 Métricas/labels (baixa cardinalidade)
Labels permitidos: `env`, `service`, `event_source`, `provider`, `status`, `error_code`.  
Proibidos como label: phone, message_id, hold_id.

---


### 10.4 Retenção e limpeza (obrigatório)
- Limpeza periódica **idempotente e segura** (Cloud Scheduler + Cloud Run Job, ou worker interno).
- Frequência recomendada: **diária** para `processed_events`, `outbox_events`, `idempotency_keys`.
- Nunca logar payload dos registros limpos; logar apenas **contagens** (PII-safe).

## 11) Admin/Dashboard (hotelly-admin) — escopo e endpoints

### 11.1 Frontend (referência)
- Stack: Next.js 14 (App Router) + Clerk + Tailwind
- Repo: `hotelly-admin`
- Páginas relevantes:
  - `/select-property`
  - `/p/[propertyId]/dashboard`
  - `/p/[propertyId]/reservations`
  - `/p/[propertyId]/reservations/[id]`
  - `/p/[propertyId]/rates`
  - `/p/[propertyId]/frontdesk/occupancy`
  - `/p/[propertyId]/settings` (crianças + cancelamento)
  - `/p/[propertyId]/settings/team` (Gestão de membros da equipe)
  - `/p/[propertyId]/settings/categories` (CRUD de categorias de quartos — `room_types`) [CONCLUÍDO]
  - `/p/[propertyId]/settings/rooms` (CRUD de quartos físicos — `rooms`) [CONCLUÍDO]

### 11.2 Endpoints backend (dashboard)
- `GET /auth/whoami`, `GET /me`, `GET /properties`, `GET /properties/{id}`
- `GET /frontdesk/summary`
- `GET /reservations` (filtros `from`, `to`, `status`)
- `GET /reservations/{id}` (campos podem ser nullable em reservas antigas)
- `POST /reservations/{id}/actions/resend-payment-link` → 202 (task)
- `POST /reservations/{id}/actions/assign-room` → 202 (task)
- `GET /occupancy` (`start_date`, `end_date` exclusivo; max 90 dias)
- `GET /rooms` (retorna `governance_status` em cada quarto — Sprint 1.13)
- `POST /rooms` — Cria quarto físico (`name`, `room_type_id`, `is_active`). Requer `manager` ou superior. [CONCLUÍDO — proxy em `rooms/route.ts`]
- `PATCH /rooms/{room_id}` — Atualiza nome, categoria e status ativo (partial update). Requer `manager` ou superior. [CONCLUÍDO — proxy em `rooms/[roomId]/route.ts`]
- `DELETE /rooms/{room_id}` — Remove quarto. Requer `manager` ou superior. [CONCLUÍDO — proxy em `rooms/[roomId]/route.ts`]
- `POST /reservations` — Cria reserva manual (sem hold), `hold_id = NULL`. Requer `staff` ou superior. Campos: `room_type_id`, `checkin`, `checkout`, `total_cents` (obrigatórios); `currency`, `adult_count`, `guest_id`, `room_id` (opcionais). Emite `reservation.created` no outbox. Migration 029 torna `hold_id` nullable. [CONCLUÍDO — proxy em `reservations/route.ts`, UI em `/p/[propertyId]/reservations/page.tsx`] *(Sprint 1.15: UI aprimorada com Autocomplete de hóspede e Pricing Preview — ver §20)*
- `POST /reservations/actions/quote` — **Pricing Preview** (read-only, sem mutações). Calcula preço e verifica disponibilidade ARI antes da criação. Retorna sempre HTTP 200; inspecionar campo `available`. Requer `staff` ou superior. *(Sprint 1.15 [CONCLUÍDO] — ver §20)*
- `PATCH /rooms/{room_id}/governance` — atualiza `governance_status` (`dirty`→`cleaning`→`clean`). Requer role `governance` ou superior. Emite `room.governance_status_changed` no outbox. *(Sprint 1.13)*
- `GET /rates` / `PUT /rates` (contrato na seção 6.3) *(Sprint 1.15: seleção de datas na UI isolada por `room_type_id` — ver §20)*
- `GET /outbox` (PII-safe)
- `GET /payments`
- `POST /payments/holds/{hold_id}/checkout`
- `GET /child-policies` / `PUT /child-policies`
- `GET /cancellation-policy` / `PUT /cancellation-policy`
- GET /rbac/users — Lista membros e papéis da propriedade (Join com e-mails).
- POST /rbac/users/invite — Vincula usuário existente (via e-mail) a uma role.
- DELETE /rbac/users/{user_id} — Remove vínculo de acesso.
- `GET /guests` — Lista hóspedes da propriedade com busca opcional por nome/e-mail. Requer `staff` ou superior. *(Sprint 1.10 [CONCLUÍDO])*
- `POST /guests` — Cria novo perfil de hóspede. Requer `staff` ou superior. 409 em conflito de e-mail/telefone. *(Sprint 1.10 [CONCLUÍDO])*
- `PATCH /guests/{id}` — Atualiza campos do perfil (partial update). Requer `staff` ou superior. 404 se não pertencer à propriedade; 409 em conflito de unicidade. *(Sprint 1.10 [CONCLUÍDO])*
- `GET /room_types` — Lista categorias de quartos. Requer `viewer` ou superior.
- `POST /room_types` — Cria nova categoria. Requer `manager` ou superior.
- `PATCH /room_types/{id}` — Atualiza nome/descrição/capacidade (partial update). Requer `manager` ou superior. [UI CONCLUÍDA — `updateRoomType` em `src/lib/roomTypes.ts`]
- `DELETE /room_types/{id}` — Remove categoria. 409 se houver quartos vinculados (FK RESTRICT). Requer `manager` ou superior.

**RBAC:** tudo é property-scoped via `?property_id=...`.

---

## 12) Ambientes e configuração (GCP)

### 12.1 Produção (referência)
Project: `hotelly--ia`  
Region: `us-central1`

Cloud Run `hotelly-public` (env relevante):
- `APP_ROLE=public`
- `TASKS_BACKEND=cloud_tasks`
- `GCP_PROJECT_ID=hotelly--ia`
- `GCP_LOCATION=us-central1`
- `GCP_TASKS_QUEUE=hotelly-default`
- `TASKS_OIDC_SERVICE_ACCOUNT=hotelly-worker@hotelly--ia.iam.gserviceaccount.com`
- `WORKER_BASE_URL` (secret `hotelly-worker-url`)
- `CONTACT_HASH_SECRET` (secret `contact-hash-secret`)
- `CONTACT_REFS_KEY` (secret `contact-refs-key`)
- `DATABASE_URL` (secret `hotelly-database-url`)
- `OIDC_ISSUER`, `OIDC_AUDIENCE`, `OIDC_JWKS_URL`

Cloud Run `hotelly-worker`:
- `APP_ROLE=worker`
- `TASKS_OIDC_AUDIENCE` alinhado com o próprio URL do worker
- (+ mesmos secrets de DB/OIDC e provider WhatsApp/Stripe quando aplicável)

Artifact Registry:
- repositório correto: **`hotelly`** (não `hotelly-repo`)
- imagem: `us-central1-docker.pkg.dev/hotelly--ia/hotelly/hotelly:latest`

### 12.2 Staging (isolado de verdade)
Objetivo: staging isolado (DB + worker próprios) para validar E2E.

Serviços:
- `hotelly-public-staging`
- `hotelly-worker-staging`

DB staging:
- Instância: `hotelly--ia:us-central1:hotelly-sql`
- Database: `hotelly_staging`
- User: `hotelly_staging_app`
- Secrets: `hotelly-staging-database-url`, `hotelly-staging-db-password`, `hotelly-worker-staging-url`

Regras operacionais críticas:
- `WORKER_BASE_URL` deve apontar para **status.url canônico** (`*.a.run.app`), não alias `*.run.app`
- `TASKS_BACKEND=cloud_tasks` é obrigatório (staging não pode ficar em `inline`)
- worker-staging deve expor **porta 8000** e ter Cloud SQL anexado
- Ordem segura de deploy: **rebuild imagem → migrate → redeploy** (public + worker)

⚠️ Observação: job `hotelly-migrate-staging` estava quebrado (DATABASE_URL mal formatado). Preferir Cloud SQL Proxy manual.

#### Regra de Ouro — Audience OIDC (validado em 2026-02-18, Sprint 1.9)

> **`WORKER_BASE_URL` (emissor) deve ser uma string idêntica a `TASKS_OIDC_AUDIENCE` (receptor).**

| Variável | Serviço onde é configurada | Papel |
|---|---|---|
| `WORKER_BASE_URL` | `hotelly-public-staging` | Define o `audience` do token OIDC gerado por `_fetch_oidc_token` |
| `TASKS_OIDC_AUDIENCE` | `hotelly-worker-staging` | Define o `audience` esperado por `verify_task_oidc` |

`google.oauth2.id_token.verify_oauth2_token` usa **igualdade exata de string**. Qualquer divergência de formato causa `ValueError: Token has wrong audience` e rejeição silenciosa da task.

**Formato correto:** `https://<service>-<hash>-<abbrev>.a.run.app` (URL canônica do Cloud Run — `*.a.run.app`)
**Formato proibido:** `https://<service>-<hash>.<region>.run.app` (URL regional — aponta para o mesmo serviço, mas a string é diferente)

Causa raiz documentada: incidente de 2026-02-18 13:58 UTC — `WORKER_BASE_URL` estava no formato regional (`*.us-central1.run.app`) enquanto `TASKS_OIDC_AUDIENCE` usava o formato canônico (`*.a.run.app`). O token chegou ao worker, mas foi rejeitado no `verify_task_oidc`. Nenhuma lógica de negócio foi executada.

### 12.3 Ciclo Financeiro e Folio (v1.7)
**Status:** Validado em Staging.

**Regras de Ouro Financeiras:**
1. **Trava de Check-out:** O sistema aplica a política "No Balance, No Exit". O status `checked_out` é bloqueado via código (409 Conflict) se `balance_due > 0`.
2. **Resiliência de Cálculo:** Em caso de erro na consulta do Folio, o sistema deve adotar o comportamento *Fail-Closed*, impedindo o check-out por segurança.
3. **Terminologia Única:** O estado operacional pós-entrada é `in_house`. Este é o único termo válido para hóspedes atualmente na propriedade.

**Infraestrutura:**
- As migrações de banco (`folio_payments`) devem ser executadas via CI/CD (Cloud Build) para garantir paridade entre Staging e Produção.

### 12.5 CI/CD — Fluxo de Dois Estágios (Cloud Build Gen 2)

#### Mapeamento branch → ambiente

| Branch    | Ambiente   | URL pública                | Config file                   |
|-----------|------------|----------------------------|-------------------------------|
| `develop` | Staging    | `dash.hotelly.ia.br`       | `cloudbuild-staging.yaml`     |
| `master`  | Production | `admin.hotelly.ia.br`      | `cloudbuild-production.yaml`  |

Cada repositório possui **dois arquivos de configuração Cloud Build** dedicados. O trigger Gen 2 aponta para o arquivo correspondente ao branch.

#### Arquivos de configuração

**hotelly-v2 (backend):**
- `cloudbuild-staging.yaml` → deploys `hotelly-public-staging` + `hotelly-worker-staging`; migra `hotelly_staging` via secret `hotelly-staging-database-url`
- `cloudbuild-production.yaml` → deploys `hotelly-public` + `hotelly-worker`; migra prod via secret `hotelly-database-url`

**hotelly-admin (frontend):**
- `cloudbuild-staging.yaml` → deploys `hotelly-admin-staging`; `NEXT_PUBLIC_APP_ENV=staging`; API URL aponta para `hotelly-public-staging`
- `cloudbuild-production.yaml` → deploys `hotelly-admin`; `NEXT_PUBLIC_APP_ENV=production`; `_API_URL` deve ser o URL canônico `*.a.run.app` de `hotelly-public`

#### Criação dos triggers (executar uma vez por repositório)

```bash
# hotelly-v2 — Staging
gcloud builds triggers create github \
  --name="hotelly-v2-staging" \
  --repository=projects/hotelly--ia/locations/global/connections/github/repositories/hotelly-v2 \
  --branch-pattern="^develop$" \
  --build-config="cloudbuild-staging.yaml" \
  --project=hotelly--ia \
  --generation=2

# hotelly-v2 — Production
gcloud builds triggers create github \
  --name="hotelly-v2-production" \
  --repository=projects/hotelly--ia/locations/global/connections/github/repositories/hotelly-v2 \
  --branch-pattern="^master$" \
  --build-config="cloudbuild-production.yaml" \
  --project=hotelly--ia \
  --generation=2

# hotelly-admin — Staging
gcloud builds triggers create github \
  --name="hotelly-admin-staging" \
  --repository=projects/hotelly--ia/locations/global/connections/github/repositories/hotelly-admin \
  --branch-pattern="^develop$" \
  --build-config="cloudbuild-staging.yaml" \
  --project=hotelly--ia \
  --generation=2

# hotelly-admin — Production
gcloud builds triggers create github \
  --name="hotelly-admin-production" \
  --repository=projects/hotelly--ia/locations/global/connections/github/repositories/hotelly-admin \
  --branch-pattern="^master$" \
  --build-config="cloudbuild-production.yaml" \
  --project=hotelly--ia \
  --generation=2
```

#### Regras operacionais

- **NEXT_PUBLIC_* são baked no bundle** no momento do `docker build`. Alterar env vars no Cloud Run após o deploy não tem efeito. Para mudar valores: atualizar a substitution no arquivo YAML e fazer push no branch correspondente.
- **`_API_URL` em `cloudbuild-production.yaml` do admin** deve ser preenchida com o URL canônico `*.a.run.app` de `hotelly-public` antes de ativar o trigger de produção. Obter via: `gcloud run services describe hotelly-public --region=us-central1 --format="value(status.url)"`
- **Ordem de deploy segura (backend):** o step `migrate` bloqueia os steps `deploy-public` e `deploy-worker` via `waitFor`. Migrations rodam antes de qualquer redeploy.
- **OIDC audience:** ver §12.2 — `WORKER_BASE_URL` em staging e produção deve usar formato `*.a.run.app`.

---

## 13) Runbooks (operacional)

### 13.1 Dev local
```bash
./scripts/dev.sh
./scripts/verify.sh
uv run pytest -q
python -m compileall -q src
```

### 13.2 Build (GCP)
```bash
# Padrão atual: usa cloudbuild.yaml (build → push → migrate → deploy em um passo)
gcloud builds submit . --config cloudbuild.yaml

# Submissão assíncrona (retorna Build ID imediatamente, não bloqueia o terminal)
gcloud builds submit . --config cloudbuild.yaml --async
```
> ⚠️ O comando legado `--tag` não executa migrations — usar apenas para builds de emergência sem mudança de schema.

### 13.3 Redeploy (forçar nova revisão)
```bash
gcloud run services update hotelly-public-staging --project hotelly--ia --region us-central1   --update-env-vars DEPLOY_SHA=$(date +%s)

gcloud run services update hotelly-worker-staging --project hotelly--ia --region us-central1   --update-env-vars DEPLOY_SHA=$(date +%s)
```

### 13.4 Logs (Cloud Run)
```bash
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=hotelly-public-staging AND severity>=ERROR"   --project hotelly--ia --limit=10 --freshness=5m

gcloud run services logs read hotelly-worker-staging   --project hotelly--ia --region us-central1 --limit 20
```

### 13.5 Cloud Tasks (debug)
```bash
gcloud tasks list --project hotelly--ia --location us-central1 --queue hotelly-default --limit 20
gcloud tasks run <TASK_NAME> --project hotelly--ia --location us-central1 --queue hotelly-default
```

### 13.6 Migrations via Cloud SQL Proxy (manual)
> O proxy é para acesso local; Cloud Run não precisa dele para operar.
```bash
cloud-sql-proxy hotelly--ia:us-central1:hotelly-sql --port 15432 &
DATABASE_URL="postgresql://hotelly_staging_app:<SENHA>@127.0.0.1:15432/hotelly_staging" uv run alembic upgrade head
kill %1
```

### 13.7 Alembic: “single head” (higiene obrigatória)
- `scripts/verify.sh` deve exigir **exatamente 1 head** (`uv run alembic heads`).
- Se aparecer “multiple heads”, criar merge revision:
```bash
uv run alembic merge -m "merge heads" <REV_A> <REV_B> ...
```

### 13.8 Staging DB drift (lição operacional)
Caso staging esteja com schema “adiantado” e `alembic_version` atrasado:
- alinhar com `alembic stamp` (com extremo cuidado) e então `upgrade head`.

---

## 14) Troubleshooting (rápido, prático)

- “202 mas nada acontece”: `TASKS_BACKEND=inline` ou `TASKS_BACKEND` errado impede execução real.
- `401` em tasks / `Token has wrong audience`: `WORKER_BASE_URL` e `TASKS_OIDC_AUDIENCE` divergem. Ambos devem usar o formato canônico `*.a.run.app` — ver Regra de Ouro em §12.2.
- PermissionDenied `iam.serviceAccounts.actAs`: falta `roles/iam.serviceAccountUser` para o SA que cria tasks com OIDC usando `TASKS_OIDC_SERVICE_ACCOUNT`.
- `InvalidTag` (AESGCM): `CONTACT_HASH_SECRET` / `CONTACT_REFS_KEY` divergentes entre public e worker. Após alinhar, limpar `contact_refs` para regenerar.
- Provider WhatsApp 400/exists:false: número inexistente em teste; validar com número real.
- `UndefinedColumn`: imagem desatualizada / migrations não aplicadas (rebuild + migrate + redeploy).
- Artifact Registry: repo certo é **`hotelly`**.


- Evolution API (debug — buscar últimas mensagens):
```bash
curl -X POST "https://edge.roda.ia.br/chat/findMessages/<instance>" \
  -H "apikey: <EVOLUTION_API_KEY>" \
  -d '{"limit": 3}'
```
---

## 15) Secrets (referência)

**Produção (nomes):**
- `hotelly-database-url`
- `hotelly-worker-url`
- `contact-hash-secret`
- `contact-refs-key`
- `oidc-issuer`
- `oidc-audience`
- `oidc-jwks-url`
- `stripe-secret-key`
- `stripe-webhook-secret`

**Staging (nomes):**
- `hotelly-staging-database-url`
- `hotelly-staging-db-password`
- `hotelly-worker-staging-url`
- `oidc-issuer-dev`
- `oidc-jwks-url-dev`


### 15.1 Convenções recomendadas (por ambiente)
- Secrets (sugestão): `hotelly-{env}-db-url`, `hotelly-{env}-stripe-secret-key`, `hotelly-{env}-stripe-webhook-secret`, `hotelly-{env}-whatsapp-verify-token`, `hotelly-{env}-whatsapp-app-secret` (se aplicável), `hotelly-{env}-internal-task-secret` (se usar header).
- Filas Cloud Tasks (sugestão): `hotelly-{env}-default`, `hotelly-{env}-expires`, `hotelly-{env}-webhooks`.

---

## 16) Checklist de manutenção (o que sempre checar)

- Public e Worker usam os **mesmos** secrets de `CONTACT_REFS_KEY` e `CONTACT_HASH_SECRET` (por ambiente).
- `TASKS_BACKEND=cloud_tasks` em produção/staging.
- `TASKS_OIDC_AUDIENCE` e `WORKER_BASE_URL` apontam para **status.url** do worker (`*.a.run.app`).
- `alembic heads` retorna **1 head**.
- Logs não vazam PII (nunca `remote_jid`, texto, telefone, body bruto).

---

## 17) Quality gates e incidentes (normativo)

### 17.1 Gates (quando afetar transação crítica / dinheiro / inventário)
- **G0:** `compileall` + build docker + `/health`.
- **G1:** `migrate up` em DB vazio + `migrate up` idempotente + constraints críticas presentes.
- **G2:** segurança/PII (lint simples) — falhar CI se houver logs/prints com payload/body/webhook sem redação, ou se rotas internas estiverem expostas no router público.
- **G3–G5:** obrigatórios para mudanças em retry/idempotência/concorrência/race em transações críticas.
- **G6 (Transação):** funções de domínio que afetam múltiplas tabelas (ex: `convert_hold`) DEVEM ser chamadas dentro de um bloco `with txn():`. Chamadas fora de transação são proibidas e devem ser rejeitadas em code review.
- **G7 (Identidade do Hóspede):** toda conversão de hold em reserva DEVE chamar `guests_repository.upsert_guest()` na mesma transação para resolver ou criar o perfil do hóspede e preencher `reservations.guest_id`. Inserir em `reservations` sem resolver `guest_id` é proibido em código novo.

17.1.1 Trava de Segurança RBAC (P0)
- Proteção de Propriedade Órfã: O sistema impede a remoção de um usuário se ele for o único Owner restante da propriedade.
- Qualquer tentativa de auto-deleção do último Owner deve retornar 400 Bad Request.

**Test plan mínimo (transações críticas):**
- Create Hold: provar `Idempotency-Key` real + guarda ARI + ordem determinística + **outbox na mesma transação** + concorrência 20→1.
- Expire Hold: dedupe por `processed_events(tasks, task_id)` + `FOR UPDATE` + `inv_held--` + outbox `hold.expired` + replay no-op.

### 17.2 INCIDENT SEV0 (stop-ship)
- Overbooking / inventário negativo.
- Reserva duplicada.
- Pagamento confirmado sem trilha de reprocesso.
- Vazamento de PII em logs.
- Endpoint interno exposto publicamente.

---

### Orientações para (Clerk/Auth/Proxy) e evitar recaídas

**1) Definições (terminologia obrigatória)**

* **Proxy (Frontend/FAPI)** = `NEXT_PUBLIC_CLERK_PROXY_URL`, `proxyUrl` no `ClerkProvider`, endpoints tipo `__clerk`. Isso altera de onde o **browser** chama o Clerk.
* **Issuer/JWKS (Backend/OIDC)** = `OIDC_ISSUER`, `OIDC_JWKS_URL`, `OIDC_AUDIENCE`. Isso altera de onde o **backend** baixa chaves para validar JWT.
* Não chamar Issuer/JWKS de “proxy”. No doc, separar como dois tópicos distintos.

**2) Regra de baseline do Admin (padrão obrigatório)**

* Admin **NÃO** usa Satellite/Proxy FAPI por padrão.
* Proibir no staging/prod (a menos que exista story explícita):

  * `CLERK_IS_SATELLITE`, `CLERK_DOMAIN`
  * `NEXT_PUBLIC_CLERK_PROXY_URL`, `CLERK_PROXY_URL`
  * `NEXT_PUBLIC_CLERK_FRONTEND_API`
  * qualquer configuração que faça o Clerk “inventar host” (`clerk.<app-domain>`).

**3) Regra do Backend (validação JWT)**

* Backend valida tokens **somente** com:

  * `OIDC_ISSUER=https://clerk.hotelly.ia.br`
  * `OIDC_JWKS_URL=https://clerk.hotelly.ia.br/.well-known/jwks.json`
  * `OIDC_AUDIENCE=hotelly-api`
* Staging e Prod devem ter **valores próprios**, versionados e auditáveis (sem “misturar ambientes”).

**4) Regra de coerência de chaves (anti “kid mismatch”)**

* `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` e `CLERK_SECRET_KEY` devem ser do **mesmo environment/instância** do Clerk.
* Proibir combinações `pk_test` + `sk_live` (e vice-versa).
* Se houver troca de chaves/instância:

  * obrigar teste em aba anônima (cookies limpos) antes de declarar estável.

**5) Build-time vs Runtime (o erro que mais aconteceu)**

* Tudo que é `NEXT_PUBLIC_*` é **build-time** no Next.js.
* Variáveis públicas do Admin (mínimo): `NEXT_PUBLIC_HOTELLY_API_BASE_URL`, `NEXT_PUBLIC_ENABLE_API`, `NEXT_PUBLIC_ENABLE_DEBUG_TOKEN`, `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`.
* Portanto, trocar env no Cloud Run **sem rebuild** é considerado inválido (pode manter bundle antigo).
* O pipeline deve preencher `BUILD_SHA` e `BUILD_DATE` e a página `/debug/env` deve mostrar ambos.
* Regra operacional: “se `/debug/env` não refletir a mudança, o deploy não está válido”.

**6) Secrets e pinagem (evitar drift)**

* Secrets usados pelo backend (issuer/jwks/audience) devem ser referenciados por **versão fixa** (`:2` etc.), nunca `:latest`.
* O service account do Cloud Run deve ter `secretAccessor` para os secrets necessários — registrar isso como pré-requisito.

**7) Runbook mínimo de validação (sempre igual)**

* Sempre validar em aba anônima:

  1. abrir `/debug/env` e checar `APP_ENV`, `API_HOST`, `BUILD_SHA/DATE`
  2. login em `/sign-in`
  3. abrir `/select-property` (não pode loopar)
  4. abrir rota protegida `/p/<id>/...`
  5. DevTools Network: **não pode** haver request para `clerk.<app-domain>` (ex.: `clerk.dash...`)
  6. requests protegidos devem indicar auth “signed-in” (headers Clerk/middleware)

**8) Regras de mudança (governança)**

* Mudança em qualquer item abaixo só via story aprovada e checklist de validação:

  * habilitar Proxy FAPI/Satellite no Admin
  * mudar `OIDC_ISSUER`/`OIDC_JWKS_URL`/`OIDC_AUDIENCE`
  * mudar chaves Clerk (pk/sk) ou instância/environment

**9) Mensagens de erro que viram “gatilho de diagnóstico”**

* `jwk-kid-mismatch` ⇒ chaves/instância incompatíveis **ou** issuer/jwks apontando para ambiente errado.
* Loop `/sign-in ↔ /select-property` ⇒ sessão não persistiu no server; tratar como auth inválida até prova em contrário.

---

Observação importante (sobre metadata no Clerk)

Clerk metadata não é usada; a vinculação é via DB/seed
Não é um bug. É um modelo de autorização “DB-backed RBAC”: Clerk autentica (quem é você), Postgres autoriza (o que você pode fazer). Implicação prática: para dar acesso a uma property, você precisa criar/atualizar `users` e `user_property_roles` no banco (via seed script ou SQL), não no Clerk. O único risco é desalinhamento de expectativas (story/documentação dizendo “atualize metadata no Clerk” quando na verdade não tem efeito).

O backend não lê metadata.property_ids nem metadata.role do Clerk. Ele só usa sub e resolve autorização via Postgres (users + user_property_roles). Logo, a vinculação correta é no banco, não no Clerk.

---

## Como o RBAC funciona hoje (fonte da verdade)

* Autorização é **100% DB-backed** no backend:

  * `JWT.sub` → `users.external_subject` → `user_property_roles`
* **Clerk user metadata (`property_ids`, `role`) não é usado** para autorização no backend (não adianta ajustar metadata esperando liberar property).
* Hierarquia de roles (nível crescente de privilégio):

  ```
  viewer (0) < governance (1) < staff (2) < manager (3) < owner (4)
  ```
* O role `governance` é **lateral** — desenhado para equipe de governança/housekeeping. Pode atualizar o status de limpeza dos quartos mas não pode realizar check-in, check-out ou acessar dados financeiros (veja §18).

---

## Diagnóstico do incidente “Sem propriedades vinculadas”

* Sintoma: após login estável, `/select-property` mostrava “Sem propriedades vinculadas à sua conta.”
* Causa: o `sub` real do usuário logado **não tinha registro/vínculo** no Postgres STAGING.

  * O banco já tinha a property `pousada-staging`, mas estava vinculada a **outro** `external_subject`.

---

## Correção aplicada (somente dados)

* No Postgres STAGING:

  * Inserir o usuário em `users` com `external_subject = <JWT.sub>`
  * Inserir vínculo em `user_property_roles`:

    * `<external_subject>` → `pousada-staging` com role `owner`
* Resultado: `/select-property` passou a listar **“Pousada Staging (STAGING)”** e a navegação para `/p/pousada-staging/dashboard` funcionou.

---

## Runbook curto — Vincular usuário do Clerk a uma property (staging)

1. Obter o `sub` do usuário (payload do JWT no browser/DevTools).
2. Conferir se existe `users.external_subject = <sub>`.
3. Criar/atualizar `user_property_roles` para a property desejada com role adequada (ex.: `owner`).
4. Validar em aba anônima: login → `/select-property` → selecionar → `/p/<id>/dashboard`.

---

## Hardening / Reprodutibilidade — Secrets pinados (hotelly-public-staging)

* OIDC (nome de secret legado):

  * `oidc-issuer-dev:2`
  * `oidc-jwks-url-dev:2`
* Pinados adicionais:

  * `hotelly-staging-database-url:3`
  * `contact-hash-secret-staging:1`
  * `contact-refs-key-staging:1`
  * `stripe-webhook-secret-staging` — obrigatório (Sprint 1.9)
* Env fixa:

  * `OIDC_AUDIENCE=hotelly-api`

---

## Critério de aceite operacional (para futuras validações)

* Em aba anônima:

  * `/debug/env` confirma `APP_ENV` e `BUILD_SHA/DATE` do deploy.
  * Login em `/sign-in`.
  * `/select-property` não loopa e lista property.
  * `/p/<propertyId>/dashboard` abre sem redirecionar para `/sign-in`.

---

Infraestrutura e IAM
Permissões de Conta de Serviço: A Service Account do Cloud Run (hotelly-public) deve possuir obrigatoriamente o papel roles/secretmanager.secretAccessor para os segredos de Webhook e Autenticação.

Dependências de Inicialização: O serviço Cloud Run não entrará em estado Ready se não houver acesso imediato ao Secret Manager e ao Cloud SQL.

Configuração de Banco de Dados (PostgreSQL)
Enums Customizados: Ao configurar um novo banco, certifique-se de que o tipo reservation_status contenha o conjunto completo: ['pending_payment', 'confirmed', 'cancelled', 'in_house', 'checked_out'].

Casting em Consultas: Queries que utilizam operadores de comparação (= ANY ou =) com colunas do tipo Enum exigem casting explícito (ex: status::reservation_status = %s) devido às restrições do driver psycopg2.

Variáveis de Ambiente Obrigatórias
Para o funcionamento da autenticação OIDC (Clerk) e integridade (ADR-008), o serviço requer:

CLERK_SECRET_KEY: Chave privada do Clerk.

OIDC_ISSUER / OIDC_AUDIENCE: URLs de validação de token.

DATABASE_URL: String de conexão (via Cloud SQL Auth Proxy ou Unix Sockets no Cloud Run).

---

Integridade de Reserva e Prevenção de Conflitos de Quarto

## Status
Implementado (Sprint 1.11 — Availability Engine)

## Contexto
É inaceitável para a operação hoteleira que o mesmo quarto físico seja atribuído a duas reservas distintas no mesmo período. Precisamos de um algoritmo padronizado e centralizado para validar colisões.

## Decisão
Utilizaremos o algoritmo de interseção de intervalos para identificar conflitos:
Um conflito existe se: `(NovoInicio < FimExistente) AND (NovoFim > InicioExistente)`.

## Regras de Negócio
1. **Exclusividade de Check-out**: O check-out é considerado um momento de saída (manhã/meio-dia) e o novo check-in de entrada (tarde). Portanto, a comparação é estrita (`<` e `>`), permitindo que uma reserva comece no mesmo dia em que outra termina.
2. **Status Operacional**: Apenas reservas com status `confirmed`, `in_house` ou `checked_out` geram conflito. Reservas canceladas e pendentes são ignoradas.
3. **Ignorar Auto-Conflito**: Ao editar datas de uma reserva já existente, o sistema deve ignorar o próprio ID da reserva para evitar falsos positivos.

## Implementação (Sprint 1.11) — Proteção em Duas Camadas

### Camada 1 — Guarda de Aplicação (`domain/room_conflict.py`)
- Função central: `assert_no_room_conflict(cur, room_id, check_in, check_out, exclude_reservation_id, lock)`.
- Chamada com `lock=True` (emite `FOR UPDATE`) nos fluxos transacionais:
  - `POST /tasks/reservations/assign-room` — **adicionado no Sprint 1.11** (gap anterior: nenhuma verificação antes do `UPDATE reservations SET room_id`).
  - `POST /{reservation_id}/actions/modify-apply` — já existia.
  - `POST /{reservation_id}/actions/check-in` — já existia.
- `exclude_reservation_id` previne auto-conflito em reatribuições e edições de data.

### Camada 2 — Restrição de Banco de Dados (Migration `026_no_room_overlap_constraint`)
- **Constraint**: `no_physical_room_overlap` na tabela `reservations`.
- **DDL**: `EXCLUDE USING GIST (room_id WITH =, daterange(checkin, checkout, '[)') WITH &&) WHERE (room_id IS NOT NULL AND status IN ('confirmed'::reservation_status, 'in_house'::reservation_status, 'checked_out'::reservation_status))`.
- **Extensão**: `btree_gist` (disponível no Cloud SQL PostgreSQL 14+; `CREATE EXTENSION IF NOT EXISTS btree_gist`).
- **Semântica de intervalo**: bound `'[)'` (half-open) → `checkout_A == checkin_B` **não** é sobreposição → virada de quarto no mesmo dia é permitida.
- **Garantia absoluta**: mesmo que código de aplicação, script direto ou retry bypass passe pela Camada 1, o PostgreSQL rejeitará o `INSERT`/`UPDATE` com `ExclusionViolation`. Nenhuma colisão física pode persistir no banco.

## Consequências
- Garantia absoluta de integridade física dos quartos (Zero Overbooking).
- Centralização da lógica de colisão no Core do domínio; banco de dados como safety gate final.
- Conformidade com a ADR-006 (PII), proibindo o log de dados de hóspedes em caso de erro de colisão.
- `ExclusionViolation` do PostgreSQL deve ser tratada como **SEV0** se ocorrer em produção (indica falha na Camada 1 que precisa de investigação imediata).

---

### 12.2 Staging (Topologia Validada v1.3)
**Objetivo:** Ambiente de validação com paridade de dados, mas isolamento de infraestrutura.

**Domínios e URLs (Source of Truth):**
- **Frontend (Admin):** `https://dash.hotelly.ia.br`
- **Backend (API):** `https://hotelly-public-staging-678865413529.us-central1.run.app` (URL Nativa Cloud Run)

**Variáveis de Build (Frontend):**
Para que o SSR (Server-Side Rendering) do Next.js funcione, as variáveis abaixo devem ser injetadas como `build-args` no Cloud Build:
- `NEXT_PUBLIC_HOTELLY_API_BASE_URL`: Deve apontar para a URL Nativa do Backend de Staging.
- `NEXT_PUBLIC_ENABLE_API`: `true`.
- `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`: Chave pública `pk_live_...` (Production Instance), mas o Backend Staging deve ter os secrets `OIDC_*` alinhados a esta instância.

**Deploy Command (Padrão):**
```bash
gcloud builds submit --config cloudbuild.yaml \
  --substitutions=_API_URL=https://hotelly-public-staging-678865413529.us-central1.run.app,_ENABLE_API=true
```

---

### 12.4 Fluxo de Continuous Deployment (CI/CD)
**Fonte da Verdade:** GitHub. Deploys manuais via CLI local estão descontinuados.

**Triggers (Geração 2) — arquivo dedicado por ambiente:**
| Trigger GCP | Repositório | Branch | Config file |
| :--- | :--- | :--- | :--- |
| `hotelly-admin-staging` | `hotelly-admin` | `^develop$` | `cloudbuild-staging.yaml` |
| `hotelly-admin-production` | `hotelly-admin` | `^master$` | `cloudbuild-production.yaml` |
| `hotelly-v2-staging` | `hotelly-v2` | `^develop$` | `cloudbuild-staging.yaml` |
| `hotelly-v2-production` | `hotelly-v2` | `^master$` | `cloudbuild-production.yaml` |

> ℹ️ Ver §12.5 para os comandos de criação de triggers e regras operacionais completas.

**Variáveis de Substituição (embutidas nos arquivos de config — não requer GCP Console):**
| Repositório | Arquivo | Variável-chave | Valor |
| :--- | :--- | :--- | :--- |
| hotelly-admin | `cloudbuild-staging.yaml` | `_SERVICE_NAME` | `hotelly-admin-staging` |
| hotelly-admin | `cloudbuild-production.yaml` | `_SERVICE_NAME` | `hotelly-admin` |
| hotelly-v2 | `cloudbuild-staging.yaml` | `_DB_SECRET_NAME` | `hotelly-staging-database-url` |
| hotelly-v2 | `cloudbuild-production.yaml` | `_DB_SECRET_NAME` | `hotelly-database-url` |

**Ordem de Execução do Build (v2):**
`Docker Build` -> `Push Artifact Registry` -> `Database Migrate` (Alembic) -> `Cloud Run Deploy`.

**Boas Práticas — step `migrate` (lição operacional):**
- `_CLOUD_SQL_INSTANCE` **deve** ter valor padrão em `cloudbuild.yaml`. Se vazio, o proxy inicia sem instância e morre silenciosamente; o `alembic upgrade head` então falha com `Connection refused` sem mensagem clara.
- Use **poll de prontidão** em vez de `sleep` fixo: o step inicia o proxy em background e tenta `python3 socket.connect(127.0.0.1:5432)` a cada 1 s (até 30 tentativas). Conectou → segue; esgotou → falha com `ERROR: Cloud SQL Proxy did not become ready within 30 s`. Isso torna falhas de proxy imediatamente visíveis nos logs do Cloud Build.

---

Padrões

### Padrão de Localização Temporal (Timezone)
**Problema:** O servidor roda em UTC, mas a operação hoteleira é local.
**Solução:**
1. **Banco de Dados:** Sempre armazena em UTC (`timestamp with time zone`).
2. **Propriedade:** A tabela `properties` possui coluna `timezone` (ex: `America/Sao_Paulo`).
3. **Lógica de Negócio (Check-in/Hoje):**
   - O sistema converte `now_utc` para o fuso da propriedade antes de validar regras de data.
   - Regra de Check-in: Permitido se `DataLocal >= DataCheckInReserva` (suporta late check-in na madrugada).

---

### Módulo de Gestão de Acesso (RBAC UI)
**Objetivo:** Autonomia para o Owner gerenciar a equipe sem intervenção no DB.

**Endpoints (Backend `rbac.py`):**
- `GET /rbac/users`: Lista colaboradores (join seguro com emails).
- `POST /rbac/users/invite`: Vincula usuário existente à propriedade.
- `DELETE /rbac/users/{user_id}`: Remove acesso.

**Regras de Segurança (Invariantes):**
1. **Proteção de Orfandade:** É proibido remover um usuário com role `owner` se ele for o único owner ativo da propriedade. O backend deve retornar **400 Bad Request** (fail-closed).
2. **Pré-requisito de Convite:** O sistema não envia e-mails de convite externos. O usuário deve criar sua conta (login no Clerk) antes de ser adicionado pelo e-mail exato.
3. **Auditoria:** Logs de alteração de permissão devem registrar `actor_user_id`, `target_user_id` e `role`, mas nunca PII (emails/nomes) no payload do log.

---

As definições abaixo são agora verdades arquiteturais do Hotelly e devem ser registradas:

Arquitetura de Deploy (CI/CD)
Padrão de Build: O projeto utiliza Google Cloud Build com injeção de variáveis em tempo de compilação.

Variáveis Obrigatórias: Todo cloudbuild.yaml deve prever a substituição _CLOUD_SQL_INSTANCE para permitir o acesso ao banco de dados durante as etapas de build/migration.

Conexão SQL: Migrações em ambiente serverless devem utilizar o Cloud SQL Auth Proxy via socket TCP local ou Unix Socket, conforme a configuração do ambiente.

Padrões de API e Roteamento
Aliases de Rota: Para manter a compatibilidade entre o legado do frontend e a evolução do backend, é permitida a utilização de múltiplos decorators em funções de rota no FastAPI.

Consumo de Reservas: A rota padrão para adição de serviços deve ser POST /reservations/{reservation_id}/extras.

Lógica de Negócio e Estados
Normalização de Status: Para fins de lançamento de receitas, o status válido para hóspedes na propriedade é in_house.

Matriz de Permissão de Consumo:

Permitidos: confirmed, in_house.

Bloqueados: pending, cancelled, checked_out.

Imutabilidade Financeira (Snapshotting): Ao vincular um extra a uma reserva, o sistema deve obrigatoriamente copiar os valores de price_cents e pricing_mode para a tabela de vínculo. Alterações no catálogo de extras nunca devem retroagir em consumos já lançados.

Integração Frontend-Backend
Injeção de Variáveis (Next.js): Variáveis de ambiente com prefixo NEXT_PUBLIC_ (como a NEXT_PUBLIC_HOTELLY_API_BASE_URL) devem ser injetadas exclusivamente no momento do build no Cloud Build para garantir que o bundle estático aponte para o ambiente correto (Staging vs Produção).

---

---

## Notas e lacunas conhecidas (P0)

- **Evolution provider por property:** o provider é property-scoped (DB), mas **credenciais Evolution** ainda são **env-only** (não por property). Se isso mudar, atualizar contrato aqui e criar migration.
- **Retry Cloud Tasks (send-response):** o contrato correto está na seção 8.4/8.5 (500 = transiente para retry; 200 terminal). Se o código atual estiver retornando 200 em falha transiente, tratar como **bug P0** (mata retry).
- **Staging checklist (mínimo):** `EVOLUTION_*`, `CONTACT_REFS_KEY`, `CONTACT_HASH_SECRET`, `TASKS_*`, `DATABASE_URL` e OIDC precisam estar montados **no public e no worker** (por ambiente).


---

## Apêndice A — Máquinas de estado (MVP)

### State Machines — Hotelly V2 (MVP)

#### Objetivo
Definir estados e transições mínimas do domínio para orientar:
- implementação de handlers (`/webhooks/*`, `/tasks/*`) — TARGET
- constraints no Postgres (UNIQUEs e invariantes)
- runbook e reprocessamento idempotente

**Nota:** no estado atual do repo, essas máquinas são especificação do sistema-alvo.

---

#### 1) Conversation
Representa a sessão de conversa/contexto com a pousada e o hóspede.

##### Estados (MVP)
- `open`: conversa ativa, ainda sem hold ativo para pagamento
- `waiting_payment`: existe um hold ativo associado aguardando pagamento
- `confirmed`: existe reserva confirmada (derivada da conversão bem-sucedida)
- `closed`: conversa encerrada (manual ou por timeout de inatividade) — opcional no MVP

##### Transições (MVP)
- `open → waiting_payment`
  - gatilho: hold criado com sucesso
  - invariantes:
    - no máximo 1 hold ativo por conversa (recomendado; pode ser relaxado se o produto permitir)
- `waiting_payment → confirmed`
  - gatilho: pagamento confirmado + conversão hold→reservation concluída
- `waiting_payment → open`
  - gatilho: hold expirado/cancelado sem pagamento

##### Eventos/outbox (TARGET)
- `conversation.waiting_payment`
- `conversation.confirmed`

---

#### 2) Hold
Bloqueio temporário de inventário (ARI) para garantir "zero overbooking".

##### Estados (MVP)
- `active`: inventário bloqueado (`inv_held` refletindo hold_nights)
- `expired`: expirou e liberou inventário
- `cancelled`: cancelado manualmente e liberou inventário (opcional no MVP)
- `converted`: convertido em reservation (inventário migra `held → booked`)

##### Transições (MVP)
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

##### Eventos/outbox (TARGET)
- `hold.created`
- `hold.expired`
- `hold.cancelled`
- `hold.converted`

---

#### 3) Payment (Stripe)
Registro interno do estado de pagamento associado a um hold.

##### Estados (MVP)
- `created`: checkout session criada e persistida
- `pending`: checkout iniciado mas não confirmado como pago
- `succeeded`: confirmado como pago (ex.: `checkout.session.completed` + `payment_status == "paid"`)
- `failed`: expirado/cancelado/erro definitivo
- `needs_manual`: inconsistente (ex.: pagamento após hold expirar; dados incompletos)

##### Transições (MVP)
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

##### Eventos/outbox (TARGET)
- `payment.created`
- `payment.succeeded`
- `payment.failed`
- `payment.needs_manual`

---

#### 4) Reservation
Reserva confirmada (resultado final da conversão).

##### Estados (MVP)
- `pending_payment`
- `confirmed`
- `cancelled`
- `in_house`
- `checked_out`

##### Invariantes (MVP)
- `UNIQUE(property_id, hold_id)` garante "no máximo 1 reserva por hold"
- ARI consistente:
  - `inv_total >= inv_booked + inv_held` para todas as noites
  - nenhum valor negativo

##### Eventos/outbox (TARGET)
- `reservation.confirmed`
- `reservation.cancelled`

---

## Apêndice B — Outbox: catálogo mínimo de eventos

### Outbox — Contrato (append-only)

#### Objetivo

Manter uma trilha **append-only** de eventos de domínio relevantes para:
- auditoria operacional,
- métricas (ex.: conversões, expirações),
- diagnóstico (correlação por request),
- futura integração/analytics.

**Regra:** payload **mínimo** e **sem PII**.

#### Tabela

`outbox_events` (Postgres / Cloud SQL)

Campos principais:
- `property_id` (tenant)
- `event_type` (string)
- `aggregate_type` (string)
- `aggregate_id` (string)
- `occurred_at` (timestamptz)
- `correlation_id` (string, opcional)
- `payload` (jsonb, opcional)

#### Event Types (catálogo mínimo)

##### Holds
- `HOLD_CREATED`
- `HOLD_EXPIRED`
- `HOLD_CANCELLED`
- `HOLD_CONVERTED`

##### Payments
- `PAYMENT_CREATED`
- `PAYMENT_SUCCEEDED`
- `PAYMENT_FAILED`

##### Reservations
- `RESERVATION_CONFIRMED`
- `RESERVATION_CANCELLED`

##### Observações
- `event_type` deve ser **estável** e usado em métricas.
- Evitar tipos "genéricos" (ex.: `UPDATED`) sem contexto.

#### Aggregate Types

Valores previstos (mínimo):
- `hold`
- `payment`
- `reservation`
- `conversation`

#### Payload permitido (mínimo)

O payload deve ser pequeno e não conter PII. Campos típicos:
- `hold_id`, `reservation_id`, `payment_id` (ids internos)
- `provider`, `provider_object_id` (ex.: `stripe`, `checkout.session.id`)
- `amount_cents`, `total_cents`, `currency`
- `checkin`, `checkout`
- `room_type_id`, `guest_count` (sem nomes/telefones/emails)

**Proibido no payload:**
- telefone, email, nome, endereço, documento, mensagem de chat
- payload bruto do provedor (Stripe/WhatsApp)

#### Regras de escrita

- Sempre dentro da **mesma transação** que altera o estado crítico (hold/payment/reservation).
- Uma ação crítica deve emitir **exatamente um** evento outbox correspondente.
- `correlation_id` deve ser propagado do request/task.

#### Retenção

Ver `docs/operations/08_retention_policy.md`.

---

## Apêndice C — Transações críticas (SQL/pseudocódigo)

### 01 — Create Hold

Este documento descreve a transação crítica do Hotelly V2, com:
- objetivo e invariantes
- locks (ordem fixa) para evitar race/deadlock
- SQL/pseudocódigo de referência (PostgreSQL)

> Regra global: ao tocar várias noites, iterar sempre em ordem **(room_type_id, date ASC)**.

#### Objetivo
Criar um **hold** que reserva inventário com expiração, garantindo **zero overbooking** sob concorrência.

#### Entrada
- `property_id`
- `conversation_id`
- `quote_option_id` (contém `room_type_id`, `rate_plan_id`, `total_cents`)
- `checkin`, `checkout`
- `expires_at`
- `idempotency_key` (recomendado)

#### Saída
- `hold_id`, `expires_at`

#### Invariantes
- Se alguma noite não tiver disponibilidade, **nenhum inventário** deve ser reservado.
- Após sucesso: para cada noite do hold, `ari_days.inv_held` incrementa em 1 (ou `qty`).

#### Locks e concorrência
- Lock primário: **linhas de ARI** afetadas, via `UPDATE ... WHERE ... AND inv_total >= inv_booked + inv_held + 1`.
- O hold é criado dentro da mesma transação; se falhar, rollback total.

#### SQL/pseudocódigo (referência)
```sql
BEGIN;

-- (Opcional) Idempotência para endpoint interno (recomendado)
-- INSERT INTO idempotency_keys(property_id, scope, idempotency_key, created_at)
-- VALUES (:property_id, 'create_hold', :idempotency_key, now())
-- ON CONFLICT (property_id, scope, idempotency_key) DO NOTHING;
-- Se já existia, retornar a resposta gravada.

-- 1) Criar hold
INSERT INTO holds(id, property_id, conversation_id, quote_option_id, status, expires_at)
VALUES (gen_random_uuid(), :property_id, :conversation_id, :quote_option_id, 'active', :expires_at)
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

#### Falhas esperadas (e como responder)
- Sem inventário: retornar “sem disponibilidade” e não criar hold.
- Stop-sell: idem.
- Conflito de idempotency_key: retornar resposta anterior.

### 02 — Expire Hold (Cloud Tasks)

Este documento descreve a transação crítica do Hotelly V2, com:
- objetivo e invariantes
- locks (ordem fixa) para evitar race/deadlock
- SQL/pseudocódigo de referência (PostgreSQL)

> Regra global: ao tocar várias noites, iterar sempre em ordem **(room_type_id, date ASC)**.

#### Objetivo
Expirar um hold ACTIVE após `expires_at`, liberando inventário (`inv_held--`) de forma idempotente.

#### Entrada
- `property_id`
- `hold_id`
- `task_id` (para dedupe em `processed_events`)
- `now` (UTC)

#### Saída
- `holds.status = expired` (se aplicável)
- inventário liberado

#### Invariantes
- Expirar duas vezes não pode liberar inventário duas vezes.
- Se o hold já foi convertido/cancelado/expirado, operação é no-op.

#### Locks e concorrência
- `SELECT ... FOR UPDATE` no hold para serializar com `convert_hold` e `cancel_hold`.

#### SQL/pseudocódigo (referência)
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

#### Observabilidade
- Logar: property_id, hold_id, task_id, status anterior e final (sem PII).
- Métrica: holds_expired_count, holds_expire_noop_count.

### 03 — Cancel Hold (User/Admin)

Este documento descreve a transação crítica do Hotelly V2, com:
- objetivo e invariantes
- locks (ordem fixa) para evitar race/deadlock
- SQL/pseudocódigo de referência (PostgreSQL)

> Regra global: ao tocar várias noites, iterar sempre em ordem **(room_type_id, date ASC)**.

#### Objetivo
Cancelar um hold ACTIVE por decisão de usuário/admin, liberando inventário.

#### Entrada
- `property_id`
- `hold_id`
- `actor` (user/admin/system)
- `idempotency_key` (recomendado)

#### Saída
- `holds.status = cancelled`
- inventário liberado

#### Invariantes
- Cancelar duas vezes não pode liberar inventário duas vezes.
- Se já convertido/expirado, operação é no-op (ou erro de negócio, conforme UX).

#### Locks e concorrência
- `SELECT ... FOR UPDATE` no hold.
- Ordem fixa nas noites (date ASC).

#### SQL/pseudocódigo (referência)
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

#### Notas de produto (MVP)
- Se cancelamento acontece por “timeout do usuário”, considere usar o mesmo mecanismo de expiração (task) para simplificar.

### 04 — Stripe Confirm → Convert Hold → Create Reservation

> **Status: Implementado e Verificado (Sprint 1.9)**
>
> O processamento de webhook (`checkout.session.completed`) foi auditado e testado em Sprint 1.9:
> - **Signature validation:** `stripe.Webhook.construct_event` com `STRIPE_WEBHOOK_SECRET` — P0 ✔
> - **Idempotência:** `INSERT INTO processed_events ... ON CONFLICT DO NOTHING` + `rowcount == 0` → 200 "duplicate" — ✔
> - **Async/Decoupling:** webhook persiste receipt e enfileira task (`/tasks/stripe/handle-event`) via `TasksClient.enqueue_http()`. Nenhuma lógica de domínio no handler. — ✔
> - **Resposta rápida:** 200 OK retornado imediatamente após INSERT + enqueue. Sem chamadas Stripe API no webhook. — ✔
> - **Teste DoD:** `tests/test_stripe_webhook_dod.py` — 5/5 cenários (assinatura real, idempotência, payload adulterado, secret errado, rollback em falha de enqueue).
> - **Metadata Stripe (Sprint 1.9 fix):** `create_checkout_session` agora envia `metadata = {hold_id, property_id, conversation_id}` conforme contrato §3.2. Corrigido em `domain/payments.py` + `holds_repository.get_hold()` (adicionado `conversation_id` ao SELECT).

Este documento descreve a transação crítica do Hotelly V2, com:
- objetivo e invariantes
- locks (ordem fixa) para evitar race/deadlock
- SQL/pseudocódigo de referência (PostgreSQL)

> Regra global: ao tocar várias noites, iterar sempre em ordem **(room_type_id, date ASC)**.

#### Objetivo
Processar pagamento confirmado (Stripe) de forma idempotente, convertendo hold ACTIVE em reserva confirmada.

#### Entrada
- `property_id`
- `stripe_event_id` (dedupe)
- `checkout_session_id` (canonical object)
- `hold_id`
- `conversation_id`
- `amount_cents`, `currency`

#### Saída
- `payments.status = succeeded` (upsert)
- `holds.status = converted` (se ACTIVE e não expirado)
- `reservations` criada (1:1 com hold)
- Inventário: `inv_held--` e `inv_booked++` por noite

#### Invariantes
- Reprocessar o mesmo Stripe event não duplica reserva.
- Reprocessar a mesma checkout session não duplica payment.
- Corrida com expiração é serializada pelo lock no hold.
- Se hold expirou antes do pagamento: não cria reserva automaticamente (caminho manual/política).

#### Locks e concorrência
- `processed_events` impede duplicidade do webhook.
- `SELECT ... FOR UPDATE` em `holds` serializa com expiração/cancelamento.
- Ordem fixa ao atualizar ARI (date ASC).

#### SQL/pseudocódigo (referência)
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

#### Caminho manual (MVP) — pagamento confirmado com hold expirado
Recomendação:
- registrar evento (outbox) e criar pendência operacional.
- política decide: remarcar/estornar/reservar manualmente se ainda houver inventário.

---

### 05 — Pagamento Outbound (geração de link de checkout)

> **Status: Implementado e Verificado (Sprint 1.9)**

#### Fluxo

```
StripeService.create_checkout_session()
  │
  ├─ Entrada: hold_id, property_id, amount_cents, currency, idempotency_key
  │
  ├─ Metadata obrigatório injetado na sessão Stripe:
  │    { hold_id, property_id, conversation_id }
  │    (conversation_id vem de holds_repository.get_hold(), que inclui o campo desde Sprint 1.9)
  │
  ├─ Stripe retorna: checkout_session_id (cs_...), checkout_url
  │
  ├─ payments INSERT (status='created', provider='stripe', provider_object_id=cs_...)
  │
  └─ checkout_url retornado ao chamador (dashboard / WhatsApp reply)
```

#### Regras de negócio
- O metadata `{ hold_id, property_id, conversation_id }` é **obrigatório**. Sem ele o Worker não consegue localizar o hold nem enviar a notificação WhatsApp pós-pagamento.
- `guest_name` pode ser incluído no metadata para rastreabilidade, mas o Worker resolve o nome via `holds.guest_name` (não depende do metadata para isso).
- O link de checkout é idempotente via `idempotency_key` no Stripe; reenvios não criam sessões duplicadas.

#### Processamento pelo Worker (após Stripe callback)
```
Stripe webhook (checkout.session.completed)
  → hotelly-public: valida assinatura (STRIPE_WEBHOOK_SECRET P0)
  → persiste receipt + enfileira task /tasks/stripe/handle-event
  → Worker: retrieve session → atualiza payments.status
  → Se payment_status == 'paid':
      convert_hold(cur, hold_id, property_id)
        → INSERT reservations (com guest_name snapshot)
        → UPDATE holds.status = 'converted'
        → Se conversation_id e contact_hash presente:
            emit_event(whatsapp.send_message) com guest_name no payload
        → Senão: log WARNING, notificação suprimida (security guard)
```

---

## Apêndice D — Operação (Local dev, Test Plan, Observabilidade, Runbook, Retenção)

### D.1 Local dev (resumo)

Comandos canônicos:
```bash
./scripts/dev.sh
./scripts/verify.sh
uv run pytest -q
python -m compileall -q src
```

### D.2 Local dev (detalhado)

### Desenvolvimento Local — Hotelly V2 (`docs/operations/01_local_dev.md`)

#### Objetivo
Permitir que **uma pessoa** rode o Hotelly V2 localmente com o mínimo de atrito, mantendo as mesmas garantias que importam em produção:
- **idempotência** (webhooks/tasks/mensagens)
- **0 overbooking**
- **sem PII/payload raw em logs**
- **replay confiável** (webhooks e tasks)

Este documento é **normativo**: se um comando “oficial” não existir no repo, isso vira tarefa de implementação.

---

#### Pré-requisitos
Obrigatórios:
- Git
- `uv` (gerenciador de dependências e runner)
- Acesso a um Postgres (local ou remoto) configurado via `DATABASE_URL`

Recomendados (para debug e integração com GCP):
- `psql` (cliente Postgres)
- Google Cloud SDK (`gcloud`)
- Stripe CLI (para replay realista de webhooks)
- (Opcional) `jq`
- Docker (útil para subir Postgres local rapidamente)

---

#### Estado atual no repo (hoje)
O repositório **já suporta** desenvolvimento local via `uv` e script:
- `uv sync --all-extras`
- `./scripts/dev.sh` (sobe API com hot-reload)

E o repositório **ainda NÃO possui** (TARGET / backlog):
- `docker-compose.yml`
- `Makefile`
- `.env.example`

Este documento separa o que é **executável hoje** do que é **TARGET**.

---

#### Convenções locais
- **Nada de segredos versionados.** Use `.env.local` (gitignored).
- **Nada de payload bruto em logs.** Se precisar depurar, logue apenas:
  - `correlation_id`
  - `event_id/message_id/task_id`
  - `property_id`, `hold_id`, `reservation_id`
  - códigos de erro (sem dados do hóspede)

---

#### TL;DR (quickstart)
1) Instalar deps:
```bash
uv sync --all-extras
```

2) Configurar ambiente (`.env.local`) com `DATABASE_URL` apontando para um Postgres acessível.

3) Aplicar schema core (se estiver usando um DB vazio):
```bash
psql "${DATABASE_URL}" -f docs/data/01_sql_schema_core.sql
```

4) Subir a API:
```bash
./scripts/dev.sh
```

5) Rodar testes:
```bash
uv run pytest -q
```

6) Smoke:
```bash
curl -sS http://localhost:${APP_PORT:-8000}/health
```

---

#### Docker Compose (TARGET)
**TARGET / backlog:** padronizar `docker-compose.yml` (Postgres + app) e comandos únicos (Makefile/scripts).

A execução local deve ter, no mínimo, estes serviços:
- `db`: Postgres
- `app`: API (FastAPI)
- `worker`: consumidor de tasks (modo local) **ou** worker que processa jobs/outbox

Portas padrão recomendadas:
- API: `8000`
- Postgres: `5432`

Se o repo ainda não tiver `docker-compose.yml`, crie como parte do backlog (Sprint 0). Este documento assume que ele existe.

---

#### Arquivo `.env.local` (mínimo)
Crie `.env.local` manualmente (não há `.env.example` versionado hoje).

Exemplo (ajuste nomes conforme o código):
```env
ENV=local
APP_PORT=8000

### Postgres local (compose)
POSTGRES_DB=hotelly
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/hotelly

### Logs
LOG_LEVEL=INFO

### Tasks
TASKS_BACKEND=local  # local | inline | gcp (staging/prod)

### Stripe (para integração real)
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...

### WhatsApp (quando integrar)
WHATSAPP_PROVIDER=meta  # meta | evolution
WHATSAPP_VERIFY_TOKEN=dev-token
```

Notas:
- `TASKS_BACKEND=inline` é útil para debug (executa handlers no mesmo processo). **Proibido em staging/prod.**
- Em staging/prod, o backend é `gcp` (Cloud Tasks).

---

#### Comandos "oficiais" (make targets) — TARGET
Recomendação: padronizar `make` para reduzir variação local.

Targets mínimos:
- `make dev` — sobe stack local (equivalente ao compose)
- `make migrate` — aplica migrações
- `make seed-minimal` — cria 1 property, 1 room_type, ARI de um range curto
- `make test` — roda a suíte mínima (incluindo gates relevantes)
- `make lint` — lint básico (inclui gate PII/print se aplicável)
- `make e2e` — fluxo controlado (quando existir)

Se `make` não for usado, estes comandos devem existir como scripts/documentados.

---

#### Banco local: operações úteis
##### Entrar no Postgres
```bash
docker compose exec db psql -U ${POSTGRES_USER:-postgres} -d ${POSTGRES_DB:-hotelly}
```

##### Queries de sanidade (inventário e invariantes)
**1) Checar overbooking (deve ser 0 linhas):**
```sql
SELECT property_id, room_type_id, date
FROM ari_days
WHERE (inv_booked + inv_held) > inv_total;
```

**2) Holds ativos vencidos (candidato a expire):**
```sql
SELECT id, property_id, status, expires_at
FROM holds
WHERE status = 'active' AND expires_at < now()
ORDER BY expires_at ASC;
```

**3) Pagamentos confirmados sem reserva (deve ser 0 ou virar runbook):**
```sql
SELECT p.*
FROM payments p
LEFT JOIN reservations r
  ON r.property_id = p.property_id
  AND r.hold_id = p.hold_id
WHERE p.status = 'succeeded'
  AND r.id IS NULL
  AND p.created_at < now() - interval '15 minutes';
```

---

#### Rodar a API localmente (sem container)
Use isso só se estiver iterando rápido em código Python.

Exemplo:
```bash
export $(cat .env.local | xargs)  # cuidado com espaços/quotes
uv run uvicorn hotelly.api.app:app --reload --host 0.0.0.0 --port ${APP_PORT:-8000}
```

Regras:
- Ainda assim, o Postgres deve estar acessível via `DATABASE_URL` (local, Docker ou remoto).
- Logs devem continuar sem payload raw/PII.

---

#### Tasks local (Cloud Tasks “simulado”)
Como Cloud Tasks não tem emulador oficial simples, a estratégia local deve ser uma destas:

##### Opção A (preferida): `TASKS_BACKEND=local` + worker rodando
- `app` apenas enfileira (persistindo receipt/processed_events quando necessário)
- `worker` consome (poll) e executa handlers

Exemplo esperado:
```bash
docker compose up -d worker
docker compose logs -f worker
```

##### Opção B: `TASKS_BACKEND=inline` (debug)
- Enfileiramento executa imediatamente no mesmo processo.
- Bom para depurar, ruim para simular retries e concorrência.

**Regra:** qualquer comportamento de retry/idempotência deve ser testado também no modo `local` (ou em staging com Cloud Tasks).

---

#### Replay de webhooks (Stripe)
Objetivo: provar **dedupe + ACK correto** e fechar o loop `payment_succeeded → convert_hold`.

##### Configurar listener local
1) Setar `STRIPE_WEBHOOK_SECRET` no `.env.local`
2) Rodar:
```bash
stripe listen --forward-to http://localhost:${APP_PORT:-8000}/webhooks/stripe
```

##### Disparar eventos de teste
Exemplos (variar conforme seu fluxo):
```bash
stripe trigger checkout.session.completed
stripe trigger payment_intent.succeeded
```

##### O que validar
- Repetir o mesmo evento não duplica efeito:
  - `processed_events` impede duplicidade
  - `reservations` tem UNIQUE por `(property_id, hold_id)`
- Resposta 2xx só ocorre após receipt durável (registrar processed_events e/ou task durável)

---

#### Replay de inbound WhatsApp (quando existir)
Regra: **um único contrato interno** de mensagem; provider só adapta.

Exemplo genérico de POST (payload *redigido*):
```bash
curl -sS -X POST "http://localhost:${APP_PORT:-8000}/webhooks/whatsapp/evolution" \
  -H "Content-Type: application/json" \
  -H "X-Correlation-Id: dev-123" \
  -d '{
    "provider":"meta",
    "message_id":"wamid.TEST",
    "from":"+5500000000000",
    "text":"quero reservar",
    "timestamp":"2026-01-25T00:00:00Z"
  }'
```

O que validar:
- Repetir o mesmo `message_id` não processa duas vezes
- Nada do payload aparece integralmente em logs

---

#### Suite mínima local (TARGET: espelhar Quality Gates)
**Nota:** os gates G0–G8 são TARGET (ver `02_cicd_environments.md`). Enquanto não houver script oficial/CI cobrindo,
use esta seção como checklist local.

Rodar antes de fechar qualquer story relevante:

- G0 — build & startup:
```bash
docker compose exec app python -m compileall -q src
curl -sS http://localhost:${APP_PORT:-8000}/health
```

- G1 — migrações e schema:
```bash
docker compose exec app make migrate
docker compose exec app make migrate  # repetir (idempotente)
```

- G2 — segurança/PII:
```bash
docker compose exec app make lint
```

- G3–G5 (quando transações críticas existirem):
```bash
docker compose exec app make test-idempotency
docker compose exec app make test-concurrency
docker compose exec app make test-race-expire-vs-convert
```

Se os targets ainda não existirem, a story deve criá-los (ou documentar o comando equivalente).

---

#### Reset completo do ambiente local
Quando o estado do banco estiver “sujo”:
```bash
docker compose down -v
docker compose up -d --build
docker compose exec app make migrate
docker compose exec app make seed-minimal
```

---

#### Troubleshooting (curto e prático)
##### App sobe, mas não conecta no DB
- Confirme `DATABASE_URL` (host deve ser `db` no compose, não `localhost`)
- Veja logs:
```bash
docker compose logs -f app
docker compose logs -f db
```

##### Migração falha por schema “meio aplicado”
- Reset com `down -v` (ambiente de dev local é descartável)

##### Duplicidade de eventos (webhook/task)
- Verifique UNIQUE em `processed_events(source, external_id)`
- Verifique que o handler grava receipt **antes** de produzir efeitos colaterais

##### Overbooking no teste de concorrência
- Falta guarda no `WHERE` do update de ARI
- Falta transação envolvendo todas as noites
- Ordem de updates não determinística

---

#### Checklist antes de integrar qualquer coisa “real”
- [ ] `processed_events`, `idempotency_keys`, `outbox_events` existem e estão cobertos por testes
- [ ] overbooking query retorna 0
- [ ] replay de webhook e message_id não duplica efeito
- [ ] logs sem payload bruto/PII

### D.3 CI/CD e ambientes (detalhado)

### CI/CD e Ambientes — Hotelly V2 (`docs/operations/02_cicd_environments.md`)

#### Objetivo
Definir **como** o Hotelly V2 é construído, testado e promovido entre ambientes (**dev → staging → prod**) com:
- **burocracia mínima**
- **gates objetivos**
- **segurança** (sem PII/segredos e sem rotas internas expostas)
- **confiabilidade** (idempotência, dedupe e retry corretos)

Este documento é **normativo**: se uma etapa “oficial” não existir no repo/infra, vira tarefa.

---

#### Ambientes

##### Local (`local`)
- Propósito: desenvolvimento e testes rápidos.
- Infra: Docker Compose (Postgres + app).
- Stripe: **test mode**.
- Dados: sintéticos/seed. Nunca PII real.

##### Dev (`dev`)
- Propósito: integração contínua e validação rápida.
- Deploy: automático no merge/push na branch principal.
- Stripe: **test mode**.
- Dados: sintéticos + fixtures.
- Regra: pode quebrar, mas **gates não**.

##### Staging (`staging`)
- Propósito: pré-produção (ensaio do que vai para prod).
- Deploy: promoção controlada (tag/release).
- Stripe: **test mode** (recomendado) ou “modo híbrido” apenas se necessário e isolado.
- Dados: sintéticos + cenários E2E.

##### Produção (`prod`)
- Propósito: operação real.
- Deploy: promoção controlada + checklist.
- Stripe: **live mode**.
- Dados: reais (PII real existe aqui; logs nunca).

---

#### Topologia recomendada por ambiente (GCP)

##### Estado atual do repo (importante)
No momento, o serviço FastAPI no repositório expõe apenas `/health`.
Os paths `/webhooks/*` e `/tasks/*` descritos abaixo são o **TARGET** de arquitetura/infra
e só passam a ser "verdade operacional" quando estiverem implementados no código e no deploy.

Enquanto isso, trate estas seções como especificação do sistema-alvo.

##### Opção preferida (mais segura): **2 serviços Cloud Run**
1) **`hotelly-public`** (público)
   - Só expõe: `/webhooks/stripe/*`, `/webhooks/whatsapp/*`, `/health`
   - Faz **receipt durável** + **enqueue** (Cloud Tasks). Não processa pesado.
2) **`hotelly-worker`** (privado / auth obrigatório)
   - Só expõe: `/tasks/*`, `/internal/*` (se existir)
   - Executa o motor de domínio/transações críticas.

**Por quê:** Cloud Run é “auth por serviço”, não por rota. Separar serviços elimina o risco clássico de “rota interna exposta no público”.

##### Opção mínima (aceitável no começo): **1 serviço Cloud Run público**
- Exigir verificação forte em **toda** rota pública:
  - Stripe: assinatura obrigatória
  - WhatsApp: verificação do provider
  - Tasks: header secreto + audience rígida (ou assinatura OIDC verificada)
- Rotas internas **não devem existir** no router público. (Gate G2 deve barrar.)

---

#### Infra mínima por ambiente

##### Cloud SQL (Postgres)
- Fonte da verdade transacional.
- Conexão Cloud Run → Cloud SQL via **Cloud SQL Connector/Auth Proxy** com IP público (conforme decisão do projeto).
- Estratégia de dados:
  - **dev/staging**: pode usar a mesma instância com **bases separadas** (`hotelly_dev`, `hotelly_staging`).
  - **prod**: instância dedicada (recomendado).

##### Cloud Tasks
- Filas por ambiente (ex.: `default`, `expires`, `webhooks`).
- Tasks devem usar **OIDC** (service account) quando chamarem `hotelly-worker`.
- Retries configurados para tolerar falhas transitórias (DB/429 do provider).

##### Secret Manager
- Segredos **por ambiente** (nomenclatura recomendada):
  - `hotelly-{env}-db-url` (ou host/user/pass separados)
  - `hotelly-{env}-stripe-secret-key`
  - `hotelly-{env}-stripe-webhook-secret`
  - `hotelly-{env}-whatsapp-verify-token`
  - `hotelly-{env}-whatsapp-app-secret` (se aplicável)
  - `hotelly-{env}-internal-task-secret` (se usar header)
- Regra: **zero segredos no repo**.

##### Service Accounts (mínimo)
- `sa-hotelly-{env}-runtime` (Cloud Run)
  - Secret Manager Secret Accessor (apenas segredos do env)
  - Cloud SQL Client
  - Cloud Tasks Enqueuer (se o serviço enfileira)
- `sa-hotelly-{env}-tasks-invoker` (Cloud Tasks OIDC)
  - Invoker do `hotelly-worker` (Cloud Run)

---

#### Estratégia de branch e versionamento (solo)
- Branches com trigger de produção (SoT): `hotelly-admin` => `main`; `hotelly-v2` => `master`.
- Trabalho diário: feature branch curta (`feat/...`, `fix/...`).
- Merge na principal somente com CI verde.
- Versões:
  - `v0.Y.Z` (enquanto em piloto)
  - tags são o artefato de promoção para staging/prod.

---

#### CI — Pipeline (sempre)

##### Estado atual (repo hoje)
No momento, o CI no repositório cobre apenas o mínimo (ex.: `compileall` e `pytest`).
Os **Quality Gates (G0–G8)** abaixo representam o **alvo normativo** do projeto.
Até estarem implementados no CI (ou em um script local padronizado), eles **não podem ser tratados como "aplicados"**.

Regra: qualquer item descrito como gate e ainda não implementado deve virar tarefa explícita (story) antes de ser usado como critério de aceite.

##### Gatilhos
- Pull Request (feature → main): roda CI completo.
- Push/merge em `main`: roda CI completo + (opcional) deploy automático `dev`.
- Tag `v*`: roda CI + promove (staging/prod conforme regra abaixo).

> Nota: "CI completo" aqui significa **o que existe no repo**. Quando os gates forem implementados,
> esta seção permanece válida e passa a refletir a prática.

##### Jobs mínimos (ordem)
1) **Lint/format** (rápido)
2) **Unit tests**
3) **Build Docker**
4) **Gates** (ver abaixo)
5) (opcional) **Integration tests** com Postgres (dev/staging)

##### Quality Gates (hard fail)
Os gates são a régua objetiva. Se falhar, não fecha story.

**Importante:** a lista abaixo é o **TARGET** (normativo).
Marque um gate como "aplicável" somente quando houver implementação real no CI (ou script oficial versionado).

- **G0 — Build & Startup**
  - `python -m compileall -q src` (ou raiz)
  - build Docker
  - app sobe e responde `/health`

- **G1 — Migrações e schema**
  - `migrate up` em DB vazio
  - `migrate up` novamente (idempotente)
  - valida constraints críticas:
    - UNIQUE `processed_events(source, external_id)`
    - UNIQUE `reservations(property_id, hold_id)`
    - UNIQUE `payments(property_id, provider, provider_object_id)`

- **G2 — Segurança/PII**
  - falha se existir `print(` em código de produção
  - falha se houver log de `payload/body/request.json/webhook` sem redaction
  - falha se `/internal/*` estiver montado no router público

- **G3 — Idempotência e retry**
  - mesmo webhook Stripe 2x → 1 efeito
  - mesma task id 2x → no-op
  - `Idempotency-Key` repetida → mesma resposta

- **G4 — Concorrência (no overbooking)**
  - teste concorrente (última unidade): 1 sucesso, N-1 falhas limpas

- **G5 — Race expire vs convert**
  - sem inventário negativo
  - no máximo 1 reserva

- **G6 — Transaction Gate** *(domínio — ver §17.1)*
  - funções de domínio que afetam múltiplas tabelas devem ser chamadas dentro de `with txn():`

- **G7 — Guest Identity** *(domínio — ver §17.1)*
  - toda conversão de hold em reserva deve chamar `upsert_guest()` na mesma transação

- **G8 — Pricing determinístico**
  - golden tests para BPS/FIXED/PACKAGE* (quando pricing existir)

---

#### CD — Promoção e Deploy

##### Artefato de deploy
- **Imagem Docker** publicada no Artifact Registry (tag por commit e por versão).

##### Deploy automático (dev)
- Trigger: push/merge em `main`
- Passos:
  1) CI completo (com gates)
  2) build + push da imagem (tag `sha`)
  3) deploy `hotelly-public`/`hotelly-worker` em `dev` apontando para segredos `dev`

##### Promoção controlada (staging)
- Trigger: tag `v0.Y.Z` (ou release manual)
- Passos:
  1) CI completo (gates)
  2) promover **a mesma imagem** (não rebuildar) para `staging`
  3) smoke E2E (mínimo): hold → checkout → webhook → reserva confirmada (com replay de webhook)

##### Promoção controlada (prod)
- Trigger: tag/release marcada como “prod”
- Passos:
  1) CI completo (gates)
  2) **migração manual** (ver política abaixo)
  3) deploy **a mesma imagem** em `prod`
  4) smoke pós-deploy (mínimo) + checagem de alertas

---

#### Política de migrações (Postgres)
Regras para não virar incidente:
1) **Sempre forward-only** em prod (sem `down`).
2) Migrações devem ser:
   - **aditivas** primeiro (add coluna/tabela/índice),
   - depois mudança de código,
   - depois limpeza/removal (em versão futura).
3) Execução:
   - dev/staging: pode rodar automaticamente no pipeline
   - prod: **passo manual** antes do deploy (ou Cloud Run Job dedicado)

Checklist de migração prod:
- backup/point-in-time habilitado (quando houver)
- migração revisada
- plano de rollback lógico (feature flag / compatibilidade)

---

#### Segurança de endpoints (regras mínimas)
- **Webhook Stripe**
  - verificar assinatura sempre
  - regra de ACK: **2xx só após receipt durável**
- **WhatsApp inbound**
  - validar token/assinatura do provider
  - nunca logar payload bruto
- **Tasks**
  - preferir OIDC (service account) chamando serviço privado (`hotelly-worker`)
  - se usar header secreto: rotacionar e manter por env
- **Rotas internas**
  - não expor em serviço público (preferência: outro serviço)
  - Gate G2 deve impedir regressão

---

#### Checklist curto de release (staging/prod)
1) CI verde (todos gates aplicáveis).
2) Segredos do env existem e estão referenciados (sem hardcode).
3) Migrações revisadas e compatíveis.
4) Smoke E2E:
   - create hold (com idempotency)
   - replay create hold (no-op)
   - checkout session ok
   - webhook Stripe replay (no-op)
   - convert gera 1 reserva
5) Alertas principais silenciosos (fila tasks, erros 5xx, erros DB).

---

#### Rollback (sem drama)
- **Rollback de app (Cloud Run):** voltar para revisão anterior (revisions).
- **Rollback de DB:** não contar com “down”.
  - usar compatibilidade (migração aditiva + código antigo ainda funciona)
  - se necessário: feature flag / desabilitar entrada (webhooks) temporariamente

---

#### Convenções de nomes (sugestão)
- Serviços:
  - `hotelly-public-{env}`
  - `hotelly-worker-{env}`
- Cloud SQL:
  - instância: `hotelly-{env}-db` (ou `hotelly-db-prod`)
  - databases: `hotelly_dev`, `hotelly_staging`, `hotelly_prod`
- Filas Tasks:
  - `hotelly-{env}-default`
  - `hotelly-{env}-expires`
  - `hotelly-{env}-webhooks`
- Secrets:
  - `hotelly-{env}-*`

---

#### Próximo documento
- `docs/operations/03_test_plan.md` — adaptar o plano V1 para o modelo SQL/Tasks/Stripe (e transformar G3–G5 em testes “oficiais”).

### D.4 Test plan (detalhado)

### Plano de Testes — Hotelly V2 (`docs/operations/03_test_plan.md`)

#### Objetivo
Garantir que o Hotelly V2 opere com **segurança transacional** e **previsibilidade operacional**, com foco em:
- **0 overbooking** sob concorrência (inventário nunca negativo e nunca excedido)
- **idempotência real** em webhooks, tasks e endpoints internos
- **semântica correta de ACK** (não matar retry do provedor por erro interno)
- **nenhum vazamento de PII/payload raw** em logs
- **replay confiável** (webhooks e tasks podem ser reprocessados com segurança)

Este documento é **normativo**: quando um teste/gate é marcado como MUST, a story relacionada só fecha quando houver prova executável em CI.

---

#### Princípios
1) **Risk-based testing**: o esforço de teste escala com o risco (dinheiro/inventário > UX).
2) **Prova executável > revisão subjetiva**: gates objetivos substituem burocracia.
3) **Determinismo**: testes devem ser reproduzíveis (fixtures estáveis, tempo controlado, seeds consistentes).
4) **Isolamento**: integração com provedores é testada por “contrato” (payload fixtures + validações), e E2E real fica reservado a staging.

---

#### Pirâmide de testes (o que existe e por quê)

##### 1) Unit tests (rápidos, puros)
**Escopo:** validações, normalização de payloads, mapeamentos, parsing, cálculos de preço (quando aplicável).  
**Não cobre:** concorrência e atomicidade (isso é Integration).

##### 2) Integration tests (Postgres + transações)
**Escopo:** todas as regras que dependem de lock, constraint, idempotência e atomicidade.  
Aqui vivem os testes que **evitam os erros da V1**.

##### 3) Contract tests (provedores)
**Escopo:** garantir que os adaptadores aceitam/rejeitam payloads reais sem efeitos colaterais.  
Stripe/WhatsApp entram aqui com fixtures e validação de assinatura/campos.

##### 4) E2E (staging) — mínimo e cirúrgico
**Escopo:** comprovar o fluxo completo (mensagem → hold → pagamento → reserva) e o comportamento de replay.  
Deve ser curto, repetível e rodar sob comando (script).

---

#### Ambientes e dados

##### Banco
- **Local/CI:** Postgres efêmero (container) + migrações aplicadas do zero.
- **Staging:** Postgres real (Cloud SQL) com migrações via pipeline.

##### Dataset mínimo (fixture)
Todo teste de integração deve conseguir criar (ou reaproveitar) o conjunto mínimo:
- 1 `property`
- 1 `room_type`
- `ari_days` preenchido para um range de datas (ex.: hoje+1 até hoje+14)
- 1 `conversation` (quando necessário)
- holds/reservations/payments conforme o cenário

**Regra:** fixture deve ser pequena, mas suficiente para reproduzir concorrência (última unidade).

---

#### Suites e casos mínimos (MUST)

##### A) Gates de qualidade (mapeamento direto para CI)
Os gates abaixo são obrigatórios e devem falhar o CI quando não cumpridos.

**G0 — Build & Startup (MUST)**
- `python -m compileall -q src` (ou raiz)
- build do container
- app responde `/health`

**G1 — Migrações e schema (MUST)**
- migrações sobem em banco vazio
- migrações rodam novamente sem erro (idempotente)
- constraints críticas existem (verificação por SQL)

**G2 — Segurança/PII (MUST)**
- falha CI se existir `print(` em código de produção
- falha CI se houver log de `payload/body/request.json/webhook` sem redação
- falha CI se rotas `/internal/*` estiverem montadas no router público

**G3 — Idempotência e retry (MUST para eventos e jobs)**
- mesmo webhook/evento 2x → **1 efeito**
- mesma task id 2x → **no-op**
- mesma `Idempotency-Key` repetida → mesma resposta, sem duplicidade

**G4 — Concorrência (MUST para inventário)**
- teste concorrente na **última unidade**: 20 tentativas → 1 sucesso, 19 falhas limpas

**G5 — Race Expire vs Convert (MUST para pagamentos)**
- simular expire e convert competindo → sem inventário negativo e no máximo 1 reserva

**G8 — Pricing determinístico (MUST quando existir pricing)**
- golden tests (BPS/FIXED/PACKAGE) para impedir regressão

> Observação: a lista completa dos gates está em `docs/operations/07_quality_gates.md`.

---

#### B) Testes de integração — transações críticas (Postgres)

##### B1) CREATE HOLD (MUST)
**O que provar**
- `Idempotency-Key` é persistida em `idempotency_keys` (não é “de mentira”).
- ARI atualiza com guarda no `WHERE` (não permite overbooking).
- `hold_nights` é determinística (mesma ordem de noites).
- Outbox grava `hold.created` na mesma transação.

**Casos mínimos**
1) **Sucesso**: inventário disponível → hold criado + `inv_held` incrementado.
2) **Sem disponibilidade**: inventário insuficiente → rollback total (sem hold parcial).
3) **Idempotência**: repetir request com mesma chave → mesma resposta, sem duplicar.
4) **Concorrência (G4)**: 20 concorrentes na última unidade → 1 hold.

##### B2) EXPIRE HOLD (MUST)
**O que provar**
- Dedupe por `processed_events(source='tasks', external_id=task_id)` ou equivalente.
- `SELECT ... FOR UPDATE` no hold (evita double-free).
- Libera ARI (`inv_held--`) e marca status `expired`.
- Outbox grava `hold.expired`.

**Casos mínimos**
1) Expirar hold elegível → libera ARI e muda status.
2) Repetir a mesma task → no-op (G3).
3) Hold já cancelado/convertido → no-op.

##### B3) CANCEL HOLD (MUST)
**O que provar**
- Mesmo desenho de expire: lock, liberar ARI, status `cancelled`.
- Idempotência: cancelar 2x não “desconta duas vezes”.
- Outbox `hold.cancelled`.

##### B4) CONVERT HOLD (MUST)
**O que provar**
- Dedupe de evento Stripe em `processed_events(source='stripe', external_id=event_id)` (ou session id, conforme contrato).
- Payment upsert com UNIQUE `(property_id, provider, provider_object_id)`.
- Lock no hold; se hold não `active` → no-op.
- Se expirado → não cria reserva; marca payment para operação.
- Se ok → `inv_held--` e `inv_booked++` por noite (ordem fixa) + cria reserva UNIQUE por hold.
- Outbox `payment.succeeded` e `reservation.confirmed`.

**Casos mínimos**
1) Convert sucesso → 1 reserva, inventário consistente.
2) Replay do mesmo evento → no-op (G3).
3) Race expire vs convert (G5) → no máximo 1 reserva e inventário nunca negativo.
4) Pagamento após expiração → payment marcado para manual e **sem reserva**.

---

#### C) Testes de contrato — provedores (sem efeitos colaterais)

##### C1) Stripe (MUST)
**Objetivo:** garantir parsing e validações antes de enfileirar/rodar efeitos.
- Assinatura inválida → rejeitar (4xx) sem side effect.
- Evento válido mas tipo não suportado → 2xx ou no-op documentado (sem efeitos).
- Evento duplicado → dedupe garante 1 efeito (coberto em G3/G5 via integração, mas aqui valida parsing).

**Fixtures**
- `checkout.session.completed` (ou evento adotado)
- `payment_intent.succeeded` (se usado)
- payloads com campos faltando (devem falhar limpo)

##### C2) WhatsApp (MUST)
**Objetivo:** adaptadores (Meta/Evolution) convertem para um **InboundMessage** interno único.
- payload mínimo válido → gera InboundMessage
- payload com campos ausentes → rejeita limpo
- message_id repetido → dedupe é garantido no pipeline (G3), mas aqui validamos extração correta do ID

---

#### D) E2E (staging) — mínimo obrigatório

##### D1) Fluxo MVP (MUST)
**Roteiro**
1) Inbound WhatsApp (mensagem controlada)
2) Quote simples (read-only)
3) Create hold
4) Criar checkout session
5) Receber webhook Stripe
6) Convert hold → reservation confirmada
7) Outbound confirmação

**Provas obrigatórias**
- 1 hold criado
- 1 payment registrado
- 1 reservation criada
- Replays (mesma mensagem e mesmo webhook) não duplicam nada

##### D2) Replay e recuperação (MUST)
- Reprocessar webhook Stripe (replay) sem duplicidade
- Reprocessar task de expire sem double-free
- Reprocessar convert após falha transient (DB/timeout) com idempotência preservada

---

#### Segurança e privacidade (testes e lint)

##### S1) PII/log hygiene (MUST)
- CI falha ao detectar padrões proibidos (Gate G2).
- Testes devem inspecionar logs em cenários críticos para garantir que **não** há payload raw.

##### S2) Rotas internas (MUST)
- Teste de introspecção garante que `/internal/*` não aparece no router público.

---

#### Como rodar (padrão recomendado)

##### Local
- Unit:
  - `pytest -q tests/unit`
- Integration (com Postgres):
  - `docker compose up -d postgres` (ou serviço equivalente)
  - `pytest -q tests/integration`
- Contract:
  - `pytest -q tests/contract`
- Suite mínima (antes de abrir PR):
  - `pytest -q tests/unit tests/integration -k "g3 or g4 or g5"`

##### CI (ordem sugerida)
1) G0 (compile/build/start)
2) G1 (migrate + constraints)
3) Unit tests
4) Integration tests (incluindo G3–G5)
5) Contract tests
6) (Opcional) E2E em staging (manual/cron de pré-release)

---

#### Critérios de aceite por story (regra prática)
- Story que toca **inventário/pagamento/transação crítica**: **G3–G5 obrigatórios**.
- Story que toca **pricing**: **G8 obrigatório**.
- Story qualquer: **G0–G2 obrigatórios**.

---

#### Checklist para adicionar um novo teste (rápido e consistente)
1) Identificar se a mudança é: unit, integration, contract, e2e
2) Se tocar “dinheiro/inventário”: escrever caso de replay (idempotência) + caso de concorrência/race quando aplicável
3) Fixar tempo (ex.: usar clock controlado) e usar fixture mínima
4) Garantir que logs não incluem payload/PII
5) Amarrar ao gate correspondente (G3–G8) se aplicável

---

#### Troubleshooting (quando teste falha)
- **Intermitência** geralmente indica falta de lock/ordem fixa de updates (ver guia de transações críticas).
- **Duplicidade** normalmente indica ausência de UNIQUE/processed_events ou uso incorreto de idempotency_keys.
- **Inventário negativo** indica double-free (expire/cancel/convert executando mais de uma vez sem proteção).
- **Webhook “sumindo”** indica 2xx retornado cedo demais (ACK errado) — consertar para receipt durável + enqueue.

---

#### Não‑objetivos (por enquanto)
- Testes de carga completos (k6/locust) antes do MVP rodar em staging.
- Cobertura alta como meta em si (cobertura é consequência; gates são meta).
- UI/admin (fora do escopo do V2 MVP inicial).

### D.5 Observabilidade

### Observability (Logs, Métricas, Tracing e Alertas)

**Documento:** docs/operations/04_observability.md  
**Objetivo:** garantir visibilidade operacional do Hotelly V2 (piloto e produção) com foco em **segurança**, **idempotência**, **concorrência** (anti-overbooking) e **tempo de resolução** (MTTR), sem vazamento de PII.

> Regra de ouro: se não está medido/alertado, não existe. Se está logado com PII, é incidente.

---

#### 1. Escopo e prioridades

##### 1.1 Prioridade do piloto
A observabilidade do piloto deve cobrir:
- **Fluxo de receita**: hold → payment → reservation.
- **Confiabilidade de ingestão**: WhatsApp inbound + Stripe webhooks + Cloud Tasks.
- **Integridade do inventário**: *overbooking = 0* e invariantes do ARI.
- **Recuperabilidade**: reprocessamento e reconciliação com rastreabilidade (processed_events + outbox).

##### 1.2 Fora de escopo (no piloto)
- APM avançado com instrumentação profunda em todas as libs.
- Análise de custo por requisição no detalhe (depois do piloto).
- Tracing distribuído “perfeito” (deixar “bom o suficiente” primeiro).

---

#### 2. Princípios (não negociáveis)

1) **Sem payload bruto em logs** (request body, webhook JSON, mensagens do WhatsApp).  
2) **Sem PII** em logs/metrics/traces (telefone, nome, conteúdo de mensagem, e-mail).  
3) **Logs estruturados (JSON)** sempre, com campos canônicos.  
4) **Correlation ID end-to-end**: request → task → DB txn → outbound.  
5) **Idempotência observável**: todo dedupe/no-op deve ser medido.  
6) **Alertas acionáveis**: todo alerta deve ter runbook e owner.

---

#### 3. Identificadores e correlação

##### 3.1 IDs canônicos (sempre que existirem)
- `correlation_id` (string, obrigatório): gerado no primeiro contato (inbound) e propagado.
- `request_id` (string): do Cloud Run (se disponível) ou gerado.
- `property_id` (string): pousada/estabelecimento.
- `conversation_id` (string)
- `hold_id` (string)
- `payment_id` (string) e `provider_object_id` (Stripe checkout.session.id)
- `reservation_id` (string)
- `idempotency_key` (string) + `idempotency_scope` (string)
- `event_source` (enum): `whatsapp_meta`, `whatsapp_evolution`, `stripe`, `tasks`, `admin`, `system`
- `external_id` (string): message_id / stripe_event_id / task_id

##### 3.2 Propagação obrigatória
- Inbound HTTP: se houver header `X-Correlation-Id`, validar e reutilizar; senão gerar.
- Cloud Tasks: setar `X-Correlation-Id` e `X-Event-Source=tasks` na task.
- Stripe webhooks: correlacionar via `metadata` (hold_id/property_id/conversation_id) e registrar `stripe_event_id` como `external_id`.

---

#### 4. Logs

##### 4.1 Formato
- **JSON por linha** (structured logging).
- Campos mínimos em *todas* as linhas:
  - `severity` (DEBUG/INFO/WARNING/ERROR)
  - `timestamp` (ISO8601)
  - `service` (ex.: `api`, `worker`)
  - `env` (`dev|staging|prod`)
  - `correlation_id`
  - `event_name` (ver catálogo abaixo)
  - `property_id` (quando aplicável)
  - `duration_ms` (quando aplicável)
  - `status` (`success|no_op|failed|retrying`)
  - `error_code` (quando falha; enum)
  - `error_class` (ex.: `ValidationError`, `DBError`, `StripeError`)

##### 4.2 Catálogo mínimo de eventos (pilot)
**Ingressos**
- `whatsapp.inbound.received`
- `stripe.webhook.received`
- `tasks.received`

**Dedupe / idempotência**
- `dedupe.hit` (no-op por processed_events)
- `idempotency.hit` (no-op por idempotency_keys)
- `outbox.appended`

**Transações críticas**
- `hold.create.started` / `hold.create.committed` / `hold.create.rejected` (inventory guard)
- `hold.expire.started` / `hold.expire.committed` / `hold.expire.no_op`
- `hold.cancel.started` / `hold.cancel.committed` / `hold.cancel.no_op`
- `hold.convert.started` / `hold.convert.committed` / `hold.convert.no_op` / `hold.convert.expired`

**Pagamentos / reservas**
- `payment.upserted`
- `reservation.created`

**Outbound**
- `whatsapp.outbound.sent`
- `whatsapp.outbound.retry`
- `whatsapp.outbound.failed`

##### 4.3 Redação (redaction)
Campos proibidos em logs:
- conteúdo de mensagem
- números de telefone
- emails
- payload completo de webhooks
- nomes de hóspedes

Se precisar depurar, usar:
- **hash** (ex.: `phone_hash`)
- **prefixo parcial** (ex.: últimos 4 dígitos, se aprovado)
- **tamanho do payload** (`payload_bytes`)
- **lista de chaves** (`payload_keys`)

##### 4.4 Níveis e volume
- INFO: fluxo normal e eventos de domínio (1 linha por etapa).
- WARNING: retries, no-op inesperado, degradação.
- ERROR: falha de transação, inconsistência, exceções.
- DEBUG: somente em dev/staging (bloquear em prod por padrão).

---

#### 5. Métricas

##### 5.1 Convenções
- Nome em `snake_case`.
- Labels (cuidado com cardinalidade):
  - permitido: `env`, `service`, `event_source`, `provider`, `status`, `error_code`
  - proibido: `phone`, `message_id`, `hold_id` (alta cardinalidade)

##### 5.2 RED (API e Workers)
**API**
- `http_requests_total{route,method,status}`
- `http_request_duration_ms_bucket{route,method}`

**Workers/Tasks**
- `tasks_processed_total{queue,status}`
- `tasks_duration_ms_bucket{queue}`

##### 5.3 Domínio (o que importa)
**Holds**
- `holds_created_total`
- `holds_expired_total`
- `holds_cancelled_total`
- `holds_converted_total`
- `holds_active_gauge` (por property_id só se cardinalidade controlada; caso contrário global)

**Inventário**
- `inventory_guard_rejections_total` (quando o `WHERE` falha)
- `inventory_invariant_violations_total` (detectado por checks/reconcile)

**Pagamentos/Reservas**
- `payments_received_total{provider}`
- `payments_succeeded_total{provider}`
- `payments_late_total{provider}` (pagou após expirar)
- `reservations_created_total`

**Idempotência / Dedupe**
- `processed_events_dedupe_hits_total{source}`
- `idempotency_hits_total{scope}`

**Outbox**
- `outbox_events_appended_total{event_type}`
- `outbox_lag_seconds` (tempo do evento mais antigo não processado, se houver consumidor)
  - No piloto, se não houver consumidor, registrar apenas appended.

##### 5.4 SLOs recomendados (pilot)
Alinhar ao `docs/strategy/06_success_metrics.md`. Sugestão inicial:
- **Overbooking**: 0 (SLO absoluto; qualquer violação = incidente).
- **Webhook Stripe**: 99% ACK < 2s; erro 5xx < 0.5%.
- **Tasks**: backlog < 1 min (p95) em horário comercial do piloto.
- **Conversão hold→reserva**: p50 < 2 min em sandbox (depende do pagamento humano).

---

#### 6. Tracing

##### 6.1 Objetivo mínimo
Não é “full tracing”. É:
- rastrear **um fluxo** do início ao fim pelo `correlation_id`
- medir **latência** por etapa
- identificar **pontos de falha** (DB, Stripe, WhatsApp)

##### 6.2 Implementação recomendada (GCP)
- Cloud Run + Cloud Logging já permite correlacionar por `trace` quando configurado.
- Se usar OpenTelemetry, manter **mínimo**:
  - spans: `inbound`, `db_txn`, `task_enqueue`, `outbound`
  - atributos: `correlation_id`, `event_source`, `status`, `error_code`

##### 6.3 Anti-padrões
- colocar payload no span
- tags de alta cardinalidade (IDs únicos por evento) em prod

---

#### 7. Dashboards (Cloud Monitoring)

##### 7.1 Dashboard “Piloto — Funil”
- Inbound WhatsApp (volume, erro)
- Holds created / converted / expired (por janela)
- Payments succeeded
- Reservations created
- Conversion rate (holds_converted_total / holds_created_total)

##### 7.2 Dashboard “Confiabilidade”
- Stripe webhook 2xx/5xx
- Tasks processed, retries, backlog
- Error rate por `error_code`
- Latência p50/p95 API e worker

##### 7.3 Dashboard “Integridade”
- inventory_guard_rejections_total (esperado em alta demanda)
- inventory_invariant_violations_total (**deve ser 0**)
- payments_late_total
- holds_active_gauge (tendência)

---

#### 8. Alertas (com severidade e ação)

##### 8.1 Stop-ship (SEV-1)
Dispara e exige ação imediata:
1) `inventory_invariant_violations_total > 0` (janela 5m)
2) `reservations_created_total` aumenta sem `payments_succeeded_total` correspondente (janela 15m) *quando o fluxo exigir pagamento prévio*
3) Stripe webhook 5xx sustentado > 2% por 10m
4) Tasks backlog > 10m por 15m (fila crítica)

**Obrigatório:** linkar para o `docs/operations/05_runbook.md` (procedimentos) e registrar incidente.

##### 8.2 Operacional (SEV-2/SEV-3)
- `payments_late_total` > limiar (ex.: 3/dia)
- `holds_active_gauge` crescendo sem conversão (sugere falha de outbound ou UX)
- `whatsapp.outbound.failed` acima de limiar

##### 8.3 Observações práticas
- Cada alerta tem:
  - sintoma
  - hipótese provável
  - passo 1–3 (rápido)
  - queries SQL de confirmação
  - ação de mitigação (reprocess/expire/retry)

---

#### 9. Pontos de instrumentação (checklist por componente)

##### 9.1 Webhook WhatsApp (inbound)
- Log: `whatsapp.inbound.received` com `external_id`, `event_source`, `payload_bytes`
- Métrica: `http_requests_total` + `processed_events_dedupe_hits_total{source=whatsapp_*}`
- Task: log `tasks.enqueued` com queue e attempt = 0

##### 9.2 Webhook Stripe
- Log: `stripe.webhook.received` com `stripe_event_id`
- Receipt durável: `dedupe.hit` / `processed_events.inserted`
- Métrica: 2xx/5xx, latência, dedupe hits

##### 9.3 Transações críticas (DB)
Para cada transação:
- Log started + committed + (failed/no_op)
- `duration_ms` obrigatório
- Métrica de sucesso/falha e `error_code`

Erros com `error_code` padronizado:
- `INVENTORY_GUARD_FAILED`
- `HOLD_NOT_ACTIVE`
- `HOLD_EXPIRED`
- `PROCESSED_EVENT_DUPLICATE`
- `IDEMPOTENCY_KEY_REPLAY`
- `DB_SERIALIZATION_FAILURE`
- `DB_DEADLOCK_DETECTED`
- `STRIPE_SIGNATURE_INVALID`
- `WHATSAPP_PROVIDER_ERROR`

##### 9.4 Outbound WhatsApp
- Log: sent/retry/failed
- Métrica: retries e falhas por provider

---

#### 10. Segurança e compliance (operacional)

##### 10.1 Redução de risco de PII
- Regex/linters de CI (gate) para `print(` e padrões de logging proibidos.
- Revisão obrigatória em alterações de logging em endpoints externos.
- Retenção de logs em prod: definir janela compatível com piloto (curta) e ampliar depois.

##### 10.2 Segredos
- Nunca logar:
  - tokens WhatsApp
  - Stripe secrets
  - connection strings
- Se houver exceção, substituir por `***`.

---

#### 11. Apêndice A — Dicionário de campos de log

| Campo | Tipo | Obrigatório | Observação |
|---|---:|---:|---|
| correlation_id | string | sim | propagado por headers/tasks |
| event_name | string | sim | catálogo do item 4.2 |
| event_source | string | sim | whatsapp/stripe/tasks/... |
| external_id | string | não | message_id / stripe_event_id / task_id |
| property_id | string | não | evitar alta cardinalidade em métricas, ok em log |
| hold_id/payment_id/reservation_id | string | não | apenas em log/tracing, não em métrica |
| duration_ms | int | não | obrigatório em transações |
| status | string | sim | success/no_op/failed/retrying |
| error_code | string | não | enum padronizado |
| payload_bytes | int | não | sempre preferir isso ao payload |

---

#### 12. Apêndice B — Conjunto mínimo de alertas do piloto (checklist)
- [ ] Overbooking/invariante de inventário (SEV-1)
- [ ] Stripe webhook 5xx sustentado (SEV-1)
- [ ] Tasks backlog crítico (SEV-1)
- [ ] Payments late acima do limiar (SEV-2)
- [ ] Falha de outbound WhatsApp (SEV-2)
- [ ] Aumento de errors por `DB_SERIALIZATION_FAILURE` (SEV-2)

---

#### 13. Referências internas
- docs/strategy/06_success_metrics.md
- docs/operations/07_quality_gates.md
- docs/operations/05_runbook.md
- docs/data/01_sql_schema_core.sql

### D.6 Runbook

### Runbook — Hotelly V2 (Operações)

> Documento operacional. Objetivo: manter o sistema funcional no piloto, com **zero overbooking**, **idempotência real**, e **resposta rápida a incidentes**.

#### 1. Escopo

#### Estado atual do repo (importante)
No momento, o serviço FastAPI expõe apenas `/health` e ainda não possui rotas implementadas para:
- `/webhooks/*`
- `/tasks/*`

Portanto, qualquer passo que mencione "reenfileirar task", "chamar handler /tasks/..." ou "endpoint interno"
deve ser tratado como **TARGET** até que as rotas/infra de Cloud Tasks estejam implementadas.

Este runbook cobre:

- Incidentes em **inventário/ARI**, **holds**, **pagamentos/Stripe**, **WhatsApp**, **Cloud Tasks**, **Cloud Run**, **Cloud SQL**.
- Rotinas operacionais (diárias/semanais) e ações de mitigação.
- Procedimentos de reprocessamento e reconciliação, priorizando **segurança transacional** e **não duplicidade**.

Fora de escopo: suporte ao cliente final (mensagens de atendimento), melhorias de produto, otimizações não urgentes.

---

#### 2. Princípios (não negociáveis)

1) **Overbooking = SEV0.** Se houver qualquer evidência de inventário negativo, reserva duplicada, ou `inv_booked` incoerente: parar tudo e conter.
2) **Webhook não pode “mentir”.** Não retornar 2xx se não houve receipt durável (dedupe/outbox/task).
3) **Idempotência sempre.** Reprocessar só quando os dedupes estão em vigor (`processed_events`, `idempotency_keys`, uniques).
4) **Sem PII em logs.** Não logar payload bruto (WhatsApp/Stripe) nem texto de usuário.
5) **Mudança em produção só com rastreabilidade.** Toda correção deve virar commit/migração/registro.

---

#### 3. Definições rápidas

- **correlation_id**: identificador para amarrar logs de webhook → task → transação.
- **property_id**: pousada.
- **hold_id**: bloqueio de inventário temporário.
- **provider_object_id**: id externo do provedor (Stripe `checkout.session.id`, evento do Stripe, message_id do WhatsApp).
- **processed_events**: dedupe de eventos externos/Tasks.
- **idempotency_keys**: dedupe de chamadas internas por chave.
- **outbox_events**: eventos append-only emitidos na mesma transação (rastreabilidade e reprocessamento).

---

#### 4. Severidade e resposta

##### SEV0 (stop-ship)
- Overbooking confirmado ou inventário negativo
- Reserva duplicada (mesmo hold ou mesmo pagamento)
- Stripe confirmado mas sistema “perde” reserva (sem trilha de reprocess)
- Vazamento de PII em logs
- Endpoint interno exposto publicamente

**Ação imediata (SEV0):**
1) **Conter**: pausar entrada (desabilitar webhook WhatsApp e/ou Stripe temporariamente ou apontar para “maintenance”).
2) **Preservar evidência**: capturar logs e métricas do intervalo.
3) **Mitigar**: corrigir o estado (com transação segura) e só então retomar.
4) **Postmortem curto**: causa raiz + fix definitivo.

##### SEV1
- Backlog grande de tasks, erros 5xx sustentados, falha de webhook com retries sem convergir
- Holds presos aumentando (stuck holds) sem liberar inventário

##### SEV2
- Erros intermitentes, degradação de latência, alertas de custo/DB

---

#### 5. Checklist de triagem (primeiros 10 minutos)

1) **O que disparou?** (alerta, reclamação, dashboard)
2) **Impacto:** quantos properties afetados? inventário/pagamento?
3) **Último deploy:** houve revisão nova no Cloud Run?
4) **Cloud Tasks:** fila acumulando? quantas falhas/retries?
5) **Cloud SQL:** conexões saturadas? CPU/IO alto?
6) **Stripe/WhatsApp:** falha de assinatura, 5xx no endpoint, timeout?
7) **Correlacionar:** pegue um `correlation_id` (ou `hold_id`/`payment_id`) e siga o rastro.

---

#### 6. Ferramentas e comandos (referência)

> Ajuste nomes de projeto/serviço/filas conforme seu `gcloud config` e padrões do repo.

##### 6.1 Cloud Run
- Listar revisões / verificar status:
  - `gcloud run services describe <SERVICE> --region us-central1`
  - `gcloud run revisions list --service <SERVICE> --region us-central1`
- Rollback rápido (apontar tráfego para revisão anterior):
  - `gcloud run services update-traffic <SERVICE> --region us-central1 --to-revisions <REVISION>=100`

##### 6.2 Logs (Cloud Logging)
- Filtrar por severity e correlation_id:
  - Ex.: `resource.type="cloud_run_revision" AND jsonPayload.correlation_id="<ID>"`

##### 6.3 Cloud Tasks
- Ver filas:
  - `gcloud tasks queues list --location us-central1`
- Tamanho/estatísticas:
  - `gcloud tasks queues describe <QUEUE> --location us-central1`

##### 6.4 Cloud SQL
- Conectar (para diagnóstico):
  - `gcloud sql connect <INSTANCE> --user=<USER> --database=<DB>`
- Ver instância:
  - `gcloud sql instances describe <INSTANCE>`

---

#### 7. Playbooks (por sintoma)

##### 7.1 Pagamento confirmado no Stripe, mas sem reserva (payments_without_reservation)

**Sintomas:**
- Cliente pagou, mas não recebeu confirmação.
- Registro de payment existe, reservation não.

**Causas comuns:**
- Webhook recebido mas task não foi enfileirada.
- Task falhou e ficou em retry.
- Convert falhou por hold expirado; sistema marcou para manual.

**Passos:**
1) Confirmar no Stripe o `checkout.session.id` e o evento associado.
2) Buscar `processed_events`:
   - Se **não existe**: falha de receipt (SEV1/SEV0 dependendo do volume).
3) Rodar SQL de diagnóstico (repo):
   - `docs/operations/sql/payments_without_reservation.sql`
4) Determinar a ação:
   - Se hold ainda **active** e dentro do prazo: **TARGET** — reprocessar convert via fila/endpoint interno quando `/tasks/*` existir.
   - Se hold **expired**: não criar reserva automaticamente. Aplicar política “pagamento após expiração” (manual/reacomodação/reembolso).

**Mitigação rápida:**
- **TARGET** — reenfileirar convert para um payment/hold específico (sempre idempotente) quando tasks existirem.
- Se falha recorrente: pausar webhook Stripe e corrigir receipt.

---

##### 7.2 Holds presos (active com expires_at no passado)

**Sintomas:**
- `holds.active` crescendo.
- Inventário “some” (inv_held alto) sem conversão.

**Passos:**
1) Verificar backlog/falhas da fila de expire.
2) Rodar SQL:
   - `docs/operations/sql/find_stuck_holds.sql`
3) Para cada hold:
   - Confirmar que está `active` e `expires_at < now()`.
   - **TARGET** — enfileirar task de expire para o hold (quando tasks existirem).
4) Se tasks estiverem quebradas:
   - Executar um job manual de expire em lote (controlado, com limite) usando o mesmo código do worker.
5) Validar ARI pós-expiração.

**Mitigação:**
- Se a fila de expire estiver parada: reiniciar worker / revisar permissões / ajustar rate.

---

##### 7.3 Falha de webhook Stripe (assinatura/5xx/timeouts)

**Sintomas:**
- Stripe mostra webhooks falhando e re-tentando.
- Aumenta “payment sem reservation”.

**Passos:**
1) Verificar se o secret de webhook no Secret Manager bate com o configurado no Stripe.
2) Checar logs do endpoint:
   - Erro de assinatura (400) → secret errado / payload alterado.
   - 5xx → erro interno (corrigir e deixar Stripe re-tentar).
3) Confirmar “receipt durável”:
   - Em sucesso, deve existir `processed_events` e/ou task enfileirada.
4) Se houver risco de duplicidade:
   - Garantir UNIQUEs e dedupe antes de reprocessar/replay.

**Mitigação:**
- Se instabilidade do serviço: rollback para revisão anterior.
- Se secret errado: corrigir secret e reprocessar eventos pendentes.

---

##### 7.4 Falha WhatsApp inbound (mensagens não chegam / duplicam / fora de ordem)

**Sintomas:**
- Queda repentina de conversas novas.
- Duplicidade de mensagens gerando múltiplas ações.

**Passos:**
1) Verificar status do provedor (Meta/Evolution) e logs de webhook.
2) Confirmar dedupe:
   - message_id deve virar `processed_events(source='whatsapp', external_id=message_id)`.
3) Se duplicidade estiver passando:
   - Contenção: pausar inbound (responder 503) temporariamente.
   - Validar se UNIQUE de processed_events está aplicado.
4) Se message_id ausente/inconsistente no provedor:
   - Aplicar fallback determinístico (ex.: hash de campos + timestamp arredondado) **apenas como mitigação** e registrar issue.

---

##### 7.5 Inventário inconsistente (ARI divergente de holds/reservations)

**Sintomas:**
- `inv_held` ou `inv_booked` não bate com fatos.
- Overbooking ou disponibilidade errada no quote.

**Ação:** tratar como SEV0 se houver overbooking.

**Passos:**
1) Rodar reconciliação:
   - `docs/operations/sql/reconcile_ari_vs_holds.sql`
2) Congelar mutações (se necessário):
   - pausar create_hold e convert temporariamente.
3) Identificar causa:
   - transação parcialmente aplicada (não deveria acontecer se atomicidade correta)
   - correção manual anterior sem rastreio
   - bug em expire/cancel/convert (ordem/WHERE/locks)
4) Corrigir estado:
   - Preferir re-execução idempotente de transação (expire/cancel/convert).
   - Ajuste direto em ARI só como último recurso, com registro e validação.
5) Validar:
   - `inv_total >= inv_booked + inv_held` em todas as noites afetadas
   - sem valores negativos
6) Postmortem: criar bug/patch com teste que reproduz.

---

##### 7.6 Backlog alto de Cloud Tasks (fila não escoa)

**Sintomas:**
- `queue_depth` cresce.
- Latência de confirmação aumenta.

**Passos:**
1) Ver taxa de erro do worker e logs de failures.
2) Verificar limites:
   - rate, max concurrent dispatches, max attempts.
3) Verificar Cloud Run:
   - instâncias suficientes? CPU/mem? timeouts?
4) Mitigação:
   - aumentar capacidade (scale) temporariamente
   - reduzir trabalho no handler (sempre enfileirar e fazer pesado no worker)
5) Se há poison messages:
   - identificar padrão de falha, corrigir código, reprocessar.

---

##### 7.7 Cloud SQL saturado (conexões/CPU/IO)

**Sintomas:**
- Erros de conexão/pool.
- Lentidão generalizada.

**Passos:**
1) Checar métricas da instância (CPU, connections, disk IO).
2) Checar pool do app (limites de conexões por instância).
3) Mitigação:
   - reduzir concorrência (Cloud Run max instances / tasks rate)
   - ajustar pool (menor) + aumentar instância do Cloud SQL se necessário
   - rollback se começou após deploy
4) Longo prazo:
   - índices faltando; queries sem filtro; N+1; falta de batch.

---

#### 8. Reprocessamento seguro (reprocess_candidates)

**Quando usar:**
- Após correção de bug (receipt/task) para “pegar” eventos perdidos.

**Passos:**
1) Rodar:
   - `docs/operations/sql/reprocess_candidates.sql`
2) Reenfileirar em lotes pequenos (ex.: 50 por vez), monitorando erro/latência.
3) Validar dedupe:
   - nenhum efeito deve duplicar reserva/payment/hold.

---

#### 9. Rotinas operacionais

##### Diário (piloto)
- Ver `payments_without_reservation` (deve ser ~0)
- Ver `stuck_holds` (deve ser ~0)
- Ver backlog de tasks (deve voltar a ~0 após picos)
- Ver taxa de erro 5xx do webhook Stripe/WhatsApp
- Amostra de logs para garantir ausência de PII

##### Semanal
- Revisar métricas do funil (WhatsApp → reserva)
- Revisar custo (Cloud Run/Tasks/SQL)
- Revisar índices e queries lentas
- Exercitar rollback (simulado) e reprocess em staging

---

#### 10. Pós-incidente (sempre)

1) Linha do tempo (deploys, alertas, impacto).
2) Causa raiz (técnica e de processo).
3) Ação corretiva:
   - patch + teste que falhava antes
   - ajuste em gate/alerta/runbook
4) Ação preventiva:
   - reduzir complexidade, eliminar caminho duplicado, endurecer constraints

---

#### Apêndice A — Artefatos úteis no repo

- SQL de operação:
  - `docs/operations/sql/reconcile_ari_vs_holds.sql`
  - `docs/operations/sql/find_stuck_holds.sql`
  - `docs/operations/sql/payments_without_reservation.sql`
  - `docs/operations/sql/reprocess_candidates.sql`

- Documentos relacionados:
  - `docs/strategy/06_success_metrics.md`
  - `docs/operations/04_observability.md`
  - `docs/operations/03_test_plan.md`

### D.7 Retenção/limpeza

### Política de Retenção e Limpeza (MVP/Piloto)

#### Objetivo

Evitar crescimento indefinido de tabelas e manter custo/performance estáveis no piloto.

**Regra:** nada de PII em tabelas operacionais (ver `docs/domain/04_message_persistence.md`).

#### Diretrizes

- Preferir retenções simples (dias) e limpeza periódica.
- Limpeza deve ser **idempotente** e segura.
- Execução recomendada: **Cloud Scheduler + Cloud Run Job** (ou worker interno).

#### Retenção por tabela (MVP)

##### `processed_events`
- **Retenção:** 90 dias
- **Motivo:** dedupe de retries e auditoria operacional curta
- **Query:**
```sql
DELETE FROM processed_events
WHERE processed_at < now() - interval '90 days';
```

##### `outbox_events`
- **Retenção:** 180 dias (piloto)
- **Motivo:** métricas e auditoria leve
- **Query:**
```sql
DELETE FROM outbox_events
WHERE occurred_at < now() - interval '180 days';
```

##### `idempotency_keys`
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

##### `payments`
- **Retenção:** manter (entidade de negócio)

##### `holds`
- **Retenção:** manter (entidade de negócio)
- Obs: status `expired` pode ser filtrado por período em queries; não deletar no MVP.

##### `reservations`
- **Retenção:** manter (entidade de negócio)

#### Frequência recomendada

- **Diária** (madrugada) para `processed_events`, `outbox_events`, `idempotency_keys`.

#### Observabilidade mínima

- Emitir log por tabela: contagem deletada por execução.
- Nunca logar payload de registros.

#### Segurança

- Job/worker deve operar com credenciais mínimas.
- Queries devem ser executadas em transação curta.

## Apêndice E — WhatsApp Outbound: Retry & Idempotência (Sprint 1.8 — Compliant)

> **Escopo:** registro do comportamento de retry/idempotência outbound, validado e em conformidade com o contrato normativo.
> **Repo:** `hotelly-v2`
> **Última validação:** 2026-02-17 (Sprint 1.8)

### E.1 Semântica HTTP atual em falhas (impacto direto em Cloud Tasks retry)

No código atual, existem **dois** handlers relacionados a envio:

- `POST /tasks/whatsapp/send-response` (`send_response`)
- `POST /tasks/whatsapp/send-message` (`send_message`, legacy/manual)

**Confirmado (Sprint 1.8):** `send-response` segue corretamente o contrato normativo:

- **Falha transiente** (5xx do provider, timeout, rede, 429 rate-limit, RuntimeError genérico) → **HTTP 500** `{"ok": false, "error": "transient_failure"}` → Cloud Tasks **faz retry**. ✔
- **Falha permanente** (4xx exceto 429, config ausente, `contact_ref` expirado, template inválido) → **HTTP 200** com `terminal=true` → Cloud Tasks **não retry**. ✔
- **Já enviado** (`outbox_deliveries.status = 'sent'`) → **HTTP 200** `{"already_sent": true}` → idempotente. ✔

Classificação feita por `_is_permanent_failure()` em `tasks_whatsapp_send.py`. Guard de idempotência via tabela `outbox_deliveries` com lease de 60s.

**Nota:** `send-message` (legacy) retorna **não-2xx** em falhas equivalentes, portanto **habilita retry**:
- `contact_ref` ausente → **404**
- falha do provider → **500** (sem distinção permanente/transiente — retries ilimitados em erros permanentes)

### E.2 Provider Evolution: retry interno limitado

No outbound Evolution, o código faz retry interno **no máximo 1 vez** (MAX_RETRIES = 1) apenas para:
- **5xx**
- **timeout/rede**

E não faz retry para:
- **401/403 (4xx)** → falha imediata (raise)

### E.3 Dedupe / idempotência durável no outbound (o que não existe hoje)

Levantamento do audit (AS-IS):
- `outbox_events` não tem `status`, `sent_at`, `attempt_count`, `last_error` e **não é atualizado** pós-envio.
- `processed_events` **não** é escrito no outbound send (não há receipt/dedupe de entrega por `outbox_event_id`).
- Request para Evolution **não** carrega idempotency key (nem header, nem campo no payload): vai apenas `number`, `text`, `apikey`.

### E.4 Conformidade com o contrato normativo do Doc Unificado

O Doc Unificado define semântica para `send-response` onde:
- **falha transiente** → **5xx** (para permitir retry do Cloud Tasks)
- **falha permanente** → **200** com `terminal=true`

**Status (Sprint 1.8): Compliant.** `send-response` **está em conformidade** com o contrato normativo.
Implementação via `_is_permanent_failure()` + `outbox_deliveries` delivery guard. Nenhuma ação pendente.

---

## Apêndice F — Staging/Infra (Sprint 1.9 — atualizado)

> **Escopo:** estado de configuração de env vars e infra para WhatsApp e Stripe.
> **Última atualização:** 2026-02-18 (Sprint 1.9)

### F.1 Contrato real de env vars (Evolution outbound) no código

Env vars **obrigatórias** lidas pelo código para Evolution:
- `EVOLUTION_BASE_URL`
- `EVOLUTION_INSTANCE`
- `EVOLUTION_API_KEY`

Env var **opcional**:
- `EVOLUTION_SEND_PATH` (default: `"/message/sendText/{instance}"`)

### F.2 Precedência provider vs credenciais (importante)

- O **provider** (Meta vs Evolution) é escolhido por **property** via DB (`properties.whatsapp_config` JSONB, campo `outbound_provider`, default `"evolution"`).
- As **credenciais/endpoint Evolution** são **env-only globais** (`EVOLUTION_*`) — não há override por property no DB.

### F.3 Estado de env vars no `hotelly-worker-staging`

**Configuradas (Sprint 1.8):**
- `EVOLUTION_BASE_URL` ✔
- `EVOLUTION_INSTANCE` ✔
- `EVOLUTION_API_KEY` ✔

**Gaps remanescentes** (ausentes no YAML do Cloud Run do worker staging):
- `CONTACT_REFS_KEY`
- `CONTACT_HASH_SECRET`
- `EVOLUTION_SEND_PATH` (opcional, usa default `"/message/sendText/{instance}"`)

Implicação operacional: handlers que dependem das vars ausentes falham por `RuntimeError: Missing ...`.

### F.4 IAM/Secret Manager — assimetria observada

No audit:
- Worker tem acesso ao secret `contact-refs-key`
- Worker **não** tem acesso ao secret `contact-hash-secret` (binding presente apenas para o public SA)

Se o worker precisar montar `CONTACT_HASH_SECRET` (ex.: para hashing) isso quebra em staging.

### F.5 Stripe: env vars obrigatórias (Sprint 1.9)

**`hotelly-public-staging`** — obrigatórias para geração de links e validação de webhook:
- `STRIPE_SECRET_KEY` — API key Stripe (obrigatório para `StripeClient.create_checkout_session`). Provisionado no Secret Manager (`stripe-secret-key-staging`); montado na SA do Cloud Run public.
- `STRIPE_WEBHOOK_SECRET` — secret do endpoint webhook Stripe (P0 de segurança; `stripe.Webhook.construct_event` rejeitará qualquer evento sem validação de assinatura). Provisionado no Secret Manager (`stripe-webhook-secret-staging`); montado na SA do Cloud Run public.

**`hotelly-worker-staging`** — obrigatórias para task handler Stripe:
- `STRIPE_SECRET_KEY` — API key Stripe (obrigatório para `stripe.checkout.Session.retrieve` no handler `handle-event`). Mesmo secret, SA do worker.

> ⚠️ **Alerta — Regra de Ouro do Worker:** `WORKER_BASE_URL` deve coincidir **exatamente** com `TASKS_OIDC_AUDIENCE` configurado no Cloud Run worker. Qualquer divergência de formato (ex.: URL regional `*.us-central1.run.app` vs canônico `*.a.run.app`) resulta em falha silenciosa de autenticação (401/403) sem execução de lógica de negócio. Ver §12.2 para incidente documentado.

**Status:**
- [x] `STRIPE_WEBHOOK_SECRET` — provisionado em `hotelly-public-staging` (Sprint 1.9)
- [x] `STRIPE_SECRET_KEY` — provisionado em `hotelly-public-staging` e `hotelly-worker-staging` (Sprint 1.9)

---

## Apêndice G — WhatsApp Inbound/Outbound: mapa factual do fluxo e vault (AS-IS — 2026-02-08)

> **Escopo:** registrar o fluxo AS-IS com nomes de rotas, tabelas tocadas e regras do vault (contact_refs).  
> **Uso:** referência de debug/runbook.

### G.1 Diagrama factual (AS-IS)

Inbound (Evolution ou Meta) → normalização/parse → `hash_contact` → `store_contact_ref` (vault) → `processed_events` (dedupe) → enqueue `/tasks/whatsapp/handle-message` → `handle_message` → `outbox_events` → enqueue `/tasks/whatsapp/send-response` → `send_response` → `get_remote_jid` (vault) → provider (Meta/Evolution).

### G.2 Rotas e handlers (nomes exatos)

Webhooks inbound:
- `POST /webhooks/whatsapp/evolution` → `src/hotelly/api/routes/webhooks_whatsapp.py:evolution_webhook`
- `POST /webhooks/whatsapp/meta` → `src/hotelly/api/routes/webhooks_whatsapp_meta.py:meta_webhook`
- `GET /webhooks/whatsapp/meta` (verify) → `meta_webhook_verify`

Tasks internas:
- `POST /tasks/whatsapp/handle-message` → `src/hotelly/api/routes/tasks_whatsapp.py:handle_message`
- `POST /tasks/whatsapp/send-response` → `src/hotelly/api/routes/tasks_whatsapp_send.py:send_response`
- `POST /tasks/whatsapp/send-message` (legacy/manual) → `src/hotelly/api/routes/tasks_whatsapp_send.py:send_message`

Admin/debug:
- `GET /outbox` → `src/hotelly/api/routes/outbox.py:list_outbox` (requer `require_property_role("viewer")`)

### G.3 Vault `contact_refs`: criptografia e TTL (nomes exatos)

- Env var: `CONTACT_REFS_KEY`  
  - deve ser **hex 64 chars** (= 32 bytes).  
  - geração sugerida no código: `openssl rand -hex 32`
- Algoritmo: **AES-256-GCM**  
  - nonce 12 bytes; storage como base64(nonce + ciphertext)
- Persistência inbound: UPSERT em `contact_refs(property_id, channel, contact_hash)`
- Lookup outbound: `SELECT ... WHERE expires_at > now()` e decrypt em memória

### G.4 Hash de contato `contact_hash` (nomes exatos)

- Env var: `CONTACT_HASH_SECRET`
  - geração sugerida: `openssl rand -hex 32`
- Função: HMAC-SHA256 de `"{property_id}|{channel}|{sender_id}"`, output base64url sem padding, truncado para 32 chars.

### G.5 Tabelas tocadas (AS-IS)

- `contact_refs` — vault (UPSERT + SELECT/decrypt)
- `processed_events` — dedupe inbound
- `conversations` — upsert/state machine no processamento de intent
- `outbox_events` — persistência de resposta (e leitura no send-response)

---

## 18) Housekeeping / Governança de Quartos (Sprint 1.13)

### 18.1 Visão geral

O módulo de housekeeping controla o ciclo de limpeza dos quartos físicos e impõe que **nenhum check-in seja realizado em quarto não limpo**. O controle é feito via campo `governance_status` na tabela `rooms` e exposto por um endpoint dedicado com role `governance`.

### 18.2 Campo `governance_status` (tabela `rooms`)

Adicionado pela migração `027_governance` (Sprint 1.13):

```sql
ALTER TABLE rooms
  ADD COLUMN governance_status TEXT NOT NULL DEFAULT 'clean'
  CHECK (governance_status IN ('dirty', 'cleaning', 'clean'));
```

| Valor | Significado |
|---|---|
| `dirty` | Quarto ocupado / saiu hóspede — aguarda limpeza. |
| `cleaning` | Limpeza em andamento. |
| `clean` | Quarto limpo e disponível para check-in. |

**Default:** `'clean'` — todos os quartos existentes permanecem elegíveis para check-in sem necessidade de backfill.

### 18.3 Guard de check-in (guard 3e)

Localização: `src/hotelly/api/routes/reservations.py`, `POST /{reservation_id}/actions/check-in`, **passo 3e** (após validar room atribuído, antes do ADR-008 conflict check).

```python
# 3e. Governance guard: room must be clean before check-in
cur.execute(
    "SELECT governance_status FROM rooms WHERE property_id = %s AND id = %s",
    (ctx.property_id, room_id),
)
room_row = cur.fetchone()
if room_row is None or room_row[0] != "clean":
    governance_status = room_row[0] if room_row else "unknown"
    raise HTTPException(
        status_code=409,
        detail=f"Room '{room_id}' is not ready for check-in (governance_status: {governance_status})",
    )
```

**Comportamento:**
- `governance_status == 'clean'` → guard passa, check-in prossegue.
- `governance_status == 'dirty'` ou `'cleaning'` → **409 Conflict** com detalhe do status atual.
- Room não encontrada na tabela `rooms` → **409 Conflict** (`governance_status: unknown`).

**Posição no fluxo do check-in:**

| Passo | Descrição | Erro |
|---|---|---|
| 1 | Idempotency check | — |
| 2 | Lock reservation `FOR UPDATE` | 404 |
| 3a | Guard: já em `in_house` | 409 |
| 3b | Guard: status ≠ `confirmed` | 409 |
| 3c | Guard: data de check-in vs hoje (timezone) | 400 |
| 3d | Guard: `room_id` atribuído | 422 |
| **3e** | **Guard: `governance_status == 'clean'`** | **409** |
| 4 | ADR-008 room conflict check | 409 |
| 5 | UPDATE status → `in_house` | 409 |
| 6 | Outbox event `reservation.in_house` | — |
| 7 | Registro idempotency key | — |

### 18.4 Endpoint `PATCH /rooms/{room_id}/governance`

**Arquivo:** `src/hotelly/api/routes/rooms.py`

```
PATCH /rooms/{room_id}/governance?property_id={property_id}
Authorization: Bearer <token>   (min role: governance)
Content-Type: application/json

Body:   { "governance_status": "dirty" | "cleaning" | "clean" }
200:    { "id": "...", "governance_status": "..." }
403:    role insuficiente (< governance)
404:    quarto não encontrado na property
422:    valor inválido para governance_status
```

**Fluxo interno:**
1. `UPDATE rooms SET governance_status = %s WHERE property_id = %s AND id = %s RETURNING id, governance_status`
2. Se `fetchone()` retornar `None` → 404.
3. `INSERT INTO outbox_events` com `event_type = 'room.governance_status_changed'`, `aggregate_type = 'room'`, payload `{room_id, property_id, governance_status, changed_by}` (sem PII).

### 18.5 Role `governance` — acesso e restrições

| Endpoint | Acesso `governance` | Motivo |
|---|---|---|
| `GET /rooms` | ✅ Sim | Requer `viewer` (nível 0 ≤ 1) |
| `PATCH /rooms/{id}/governance` | ✅ Sim | Requer `governance` (nível 1 ≤ 1) |
| `POST /reservations/.../check-in` | ❌ Não | Requer `staff` (nível 2 > 1) → 403 |
| `POST /reservations/.../check-out` | ❌ Não | Requer `staff` → 403 |
| `GET /payments` | ❌ Não | Requer `staff` → 403 |
| `GET /reservations` (lista) | ⚠️ Sim* | Requer apenas `viewer` — ver nota abaixo |

> **⚠️ Work item aberto (PII):** `GET /reservations` e `GET /reservations/{id}` exigem apenas `viewer` e retornam dados de hóspedes (nomes, contatos). O role `governance` satisfaz `viewer` e, portanto, tecnicamente tem acesso a esses dados. Isolamento total de PII para o role `governance` requer guards por endpoint (`if ctx.role == "governance": raise HTTPException(403)`) nesses endpoints — registrado como work item para sprint seguinte.

### 18.6 Outbox event `room.governance_status_changed`

Emitido pelo `PATCH /rooms/{id}/governance` dentro da mesma transação do UPDATE:

```json
{
  "room_id": "room-101",
  "property_id": "prop-abc",
  "governance_status": "clean",
  "changed_by": "<user_uuid>"
}
```

Sem PII (nomes/emails/telefones). Segue a política de zero PII nos payloads (§3.2).

### 18.7 Migração `027_governance`

**SQL:** `migrations/sql/027_governance.sql`
**Alembic:** `migrations/versions/027_governance.py`
**down_revision:** `026_no_room_overlap_constraint`

Operações (atômicas):
1. `DROP CONSTRAINT user_property_roles_role_check` + `ADD CONSTRAINT ... CHECK (role IN ('owner', 'manager', 'staff', 'viewer', 'governance'))`.
2. `ALTER TABLE rooms ADD COLUMN governance_status TEXT NOT NULL DEFAULT 'clean' CHECK (governance_status IN ('dirty', 'cleaning', 'clean'))`.

**Downgrade:** remove a coluna `governance_status` e reverte a constraint ao conjunto original de 4 roles.

---

## 19) CRM de Hóspedes — API de Escrita (Sprint 1.10 [CONCLUÍDO])

> **Status: Implementado e Verificado (Sprint 1.10)**
>
> A leitura (`GET /guests`) foi entregue na Sprint 1.10 junto com a migração `024_guests_crm` e a identidade CRM (upsert via `guests_repository`). A escrita direta pelo painel admin (`POST /guests`, `PATCH /guests/{id}`) foi adicionada na continuação da Sprint 1.10, fechando o ciclo CRUD do CRM.

### 19.1 Visão geral

O módulo CRM expõe três endpoints para gestão de perfis de hóspedes a partir do painel admin. Todos são property-scoped (`?property_id=...`) e protegidos pelo role mínimo `staff` — garantindo que o role `governance` (housekeeping) não acesse dados de PII.

**Arquivo:** `src/hotelly/api/routes/guests.py`

### 19.2 RBAC — Restrições de acesso

| Role | Acesso aos endpoints `/guests` |
|---|---|
| `owner` | ✅ Leitura e escrita |
| `manager` | ✅ Leitura e escrita |
| `staff` (= receptionist no admin) | ✅ Leitura e escrita |
| `governance` | ❌ 403 — protege PII de housekeeping |
| `viewer` | ❌ 403 |

O guard é aplicado na camada da API (`require_property_role("staff")`); o frontend admin adiciona uma camada complementar renderizando o componente `AccessDenied` para o role `governance` antes mesmo de chamar a API.

### 19.3 Endpoint `GET /guests`

```
GET /guests?property_id={id}&search={texto}
Authorization: Bearer <token>   (min role: staff)

200: [ { "id": "...", "name": "...", "email": "...", "phone": "...",
         "document": "...", "created_at": "2026-01-15T10:00:00+00:00" }, ... ]
403: role insuficiente (governance ou viewer)
```

- `search` é opcional; quando presente, aplica `ILIKE %texto%` em `full_name` e `email` (case-insensitive).
- Resultado ordenado por `full_name ASC`, limitado a 500 registros.
- Campos mapeados da tabela: `full_name → name`, `document_id → document`.

### 19.4 Endpoint `POST /guests`

```
POST /guests?property_id={id}
Authorization: Bearer <token>   (min role: staff)
Content-Type: application/json

Body:
{
  "name":     "Maria Silva",       // obrigatório
  "email":    "maria@exemplo.com", // opcional
  "phone":    "+5511999990000",    // opcional
  "document": "123.456.789-00"     // opcional
}

201: { "id": "...", "name": "...", "email": "...", "phone": "...",
       "document": "...", "created_at": "..." }
409: e-mail ou telefone já cadastrado nesta propriedade
     (unique partial indexes `uq_guests_property_email` / `uq_guests_property_phone`)
```

- `email` é normalizado para minúsculas antes do INSERT.
- Strings vazias são tratadas como `NULL` (sem phantom records).
- Usa INSERT direto (não `upsert_guest()` do repositório — este é para resolução de identidade automática no fluxo de reservas).

### 19.5 Endpoint `PATCH /guests/{guest_id}`

```
PATCH /guests/{guest_id}?property_id={id}
Authorization: Bearer <token>   (min role: staff)
Content-Type: application/json

Body (todos os campos são opcionais — partial update):
{
  "name":     "Maria Souza",
  "email":    "novo@exemplo.com",
  "phone":    "+5511888880000",
  "document": "novo-doc"
}

200: { "id": "...", "name": "...", "email": "...", "phone": "...",
       "document": "...", "created_at": "..." }
400: nenhum campo fornecido no body
404: hóspede não encontrado ou não pertence a esta propriedade
409: novo e-mail ou telefone já pertence a outro hóspede da propriedade
```

- Apenas os campos presentes no body são alterados (partial UPDATE com cláusula SET dinâmica).
- O `property_id` é sempre incluído no `WHERE` — impede acesso cross-property.
- `email` normalizado para minúsculas; strings vazias convertidas para `NULL`.

### 19.6 Frontend (hotelly-admin)

| Artefato | Caminho |
|---|---|
| Proxy `GET` + `POST` | `src/app/api/p/[propertyId]/guests/route.ts` |
| Proxy `PATCH` | `src/app/api/p/[propertyId]/guests/[guestId]/route.ts` |
| Lib client-side | `src/lib/guests.ts` — `listGuests()`, `createGuest()`, `updateGuest()` |
| UI | `src/app/p/[propertyId]/guests/GuestList.tsx` |
| Guard RBAC | `src/app/p/[propertyId]/guests/page.tsx` — renderiza `<AccessDenied />` para role `governance` |

O componente `GuestList` usa um único `Dialog` para criação e edição, controlado pelo estado `editingGuest` (null = modo criação, Guest = modo edição). Após salvar, a lista é atualizada in-place sem reload: novos registros são inseridos em ordem alfabética; edições substituem a linha correspondente.

### 19.7 Migração `028_room_types_meta`

Adicionada para suportar a página de Categorias do admin:

```sql
-- migrations/sql/028_room_types_meta.sql
ALTER TABLE room_types
    ADD COLUMN IF NOT EXISTS description   TEXT,
    ADD COLUMN IF NOT EXISTS max_occupancy INT NOT NULL DEFAULT 2;
```

Retrocompatível: linhas existentes recebem `description = NULL` e `max_occupancy = 2`.

---

## 20) Usability & Financial Intelligence — Sprint 1.15 [CONCLUÍDO]

> **Status: Implementado — 2026-02-20**
>
> Objetivo: eliminar o erro 409 `no_ari_record` expondo um preview de preço antes da criação de reserva, e melhorar a UX nas páginas de Reservas e Tarifas.

### 20.1 Novo endpoint: `POST /reservations/actions/quote`

**Arquivo:** `src/hotelly/api/routes/reservations.py`

Endpoint de Pricing Preview para **novas** reservas (sem `reservation_id`). Read-only — nenhuma linha é escrita, nenhum lock `FOR UPDATE` é emitido.

**Request body (`QuoteRequest`):**
```json
{
  "room_type_id": "string",
  "checkin": "YYYY-MM-DD",
  "checkout": "YYYY-MM-DD",
  "adult_count": 2,
  "children_ages": []
}
```

**Resposta — sempre HTTP 200:**

| `available` | Campos adicionais | Significado |
|---|---|---|
| `true` | `total_cents`, `currency`, `nights` | Preço calculado com sucesso |
| `false` | `reason_code`, `meta` | ARI ou tarifa indisponível |

**`reason_code` possíveis:**

| Código | Causa |
|---|---|
| `no_ari_record` | Nenhuma linha em `ari_days` para uma das noites da estadia |
| `no_inventory` | `inv_total - inv_booked - inv_held < 1` para uma das noites |
| `rate_missing` | Nenhuma linha em `room_type_rates` para uma data |
| `pax_rate_missing` | Coluna `price_{N}pax_cents` é NULL |
| `child_rate_missing` | Coluna de bucket de criança é NULL |
| `child_policy_missing` | Propriedade sem configuração de faixas etárias |
| `child_policy_incomplete` | Buckets não cobrem 0..17 sem lacunas |
| `invalid_dates` | `checkin >= checkout` |
| `invalid_adult_count` | `adult_count` fora do intervalo 1..4 |

**Engine:** chama `quote_minimum()` de `domain/quote.py`, que já era usada internamente por `create_hold`. A lógica de cálculo não foi duplicada.

**Auth:** `staff` ou superior (mesmo nível que `POST /reservations`).

**BFF (hotelly-admin):**
- Proxy: `src/app/api/p/[propertyId]/reservations/actions/quote/route.ts`
- Lib: `src/lib/reservations.ts` — `quoteNewReservation()`, tipo `QuoteResponse`

---

### 20.2 CRUD de Reservas — UI aprimorada (`NewReservationDialog`)

**Arquivo:** `src/app/p/[propertyId]/reservations/NewReservationDialog.tsx`

#### 20.2.1 Autocomplete de Hóspede

O campo `ID do hóspede` (input de texto livre esperando UUID) foi substituído por um Autocomplete:

- Digitar ≥ 2 caracteres aciona `GET /guests?search=...` com debounce de 400ms
- Resultados exibidos como dropdown posicionado com `full_name` + `email`
- Ao selecionar: chip com botão `✕` para desfazer
- Ao submeter: envia `guest_id: selectedGuest?.id ?? null`
- Nenhuma biblioteca externa; padrão idêntico ao `GuestList.tsx`

#### 20.2.2 Pricing Preview

Quando `room_type_id`, `checkin`, `checkout` e `adult_count` estiverem preenchidos, o dialog dispara `POST /reservations/actions/quote` com debounce de 400ms e exibe:

| Estado | Visual |
|---|---|
| Calculando | Texto cinza "Calculando preço…" |
| `available: true` | Bloco verde com preço formatado (R$) e contagem de noites; preenche automaticamente o campo `Valor total` |
| `available: false` | Bloco âmbar com mensagem legível mapeada do `reason_code`; **botão "Criar" desabilitado** |
| Erro de rede | Mensagem vermelha inline; botão permanece habilitado (degradação graciosa) |

**Invariante:** o campo `Valor total (centavos)` continua editável para overrides manuais. O auto-preenchimento ocorre a cada nova resposta do quote.

---

### 20.3 Gestão de Tarifas — seleção de datas isolada por categoria (`RatesGrid`)

**Arquivo:** `src/app/p/[propertyId]/rates/RatesGrid.tsx`

**Problema anterior:** `selectedDates` era um `Set<string>` global. Clicar em uma data na categoria A destacava a mesma data em todas as outras categorias, e a "Edição em lote" aplicava valores em todas as categorias simultaneamente.

**Solução:**

- Adicionado estado `selectedRoomTypeId: string | null` que rastreia qual categoria tem seleção ativa.
- `toggleDate(roomTypeId, date, shiftKey)`: ao clicar em uma categoria diferente da ativa, limpa `selectedDates` e muda `selectedRoomTypeId` antes de selecionar a nova data.
- Highlight de cabeçalho e células: `selectedRoomTypeId === rt.id && selectedDates.has(d)` — bleed cross-category eliminado.
- `applyBulk` e `applyPct`: filtram `r.room_type_id !== selectedRoomTypeId` — mutações afetam apenas a categoria ativa.
- Botão **"Limpar seleção"** aparece na barra de controles quando `selectedCount > 0`.
- Seleção é limpa automaticamente após `putRates` bem-sucedido (`clearSelection()`).

**Retrocompatível:** Shift+Click para seleção de intervalo e a lógica de bulk-edit existente funcionam sem alteração dentro da categoria ativa.

## 21) Ciclo de Vida de Categorias de Quartos — Room Type Lifecycle Policy

### 21.1 Princípio (Regra de Ouro)

> **Soft Delete é o padrão para exclusões via UI.**
> A linha de `room_types` nunca é removida fisicamente pelo fluxo de dashboard — apenas `deleted_at` é carimbado. Isso garante que o histórico financeiro (reservas, tarifas de datas passadas) mantenha seu alvo de FK.

### 21.2 Abordagem em Camadas

| Camada | Ação | Quem executa | Quando |
|--------|------|-------------|--------|
| **Layer 1** (implementado — Sprint 1.16) | `UPDATE room_types SET deleted_at = now()` | Dashboard `DELETE /room_types/{id}` | Operação normal de manager |
| **Layer 2** (futuro) | `DELETE FROM room_types WHERE id = …` | Endpoint superadmin restrito | Purge explícito, somente após auditoria |

### 21.3 Pré-condições de Bloqueio (409)

A exclusão via dashboard é bloqueada se **qualquer uma** das condições abaixo for verdadeira:

1. **Quartos ativos**: existem `rooms` com `is_active = true` nesta categoria.
   → Operador deve desativar todos os quartos via `PATCH /rooms/{id}` antes de excluir a categoria.

2. **Reservas abertas**: existem `reservations` com `room_type_id = {id}` e `status NOT IN ('cancelled', 'checked_out')`.
   → Operador deve cancelar ou concluir (check-out) todas as reservas antes de excluir a categoria.

### 21.4 Efeitos Colaterais na Exclusão Bem-sucedida

Executados **na mesma transação** que o soft-delete:

| Tabela | Ação | Justificativa |
|--------|------|---------------|
| `ari_days` | `DELETE WHERE date >= CURRENT_DATE` | Dado operacional/derivado — linha de inventário futura seria fantasma |
| `room_type_rates` | `DELETE WHERE date >= CURRENT_DATE` | Dado de configuração — tarifas futuras de categoria desativada causariam respostas de quote incorretas |
| `ari_days` (passado) | **Mantido** | Histórico de ocupação para relatórios |
| `room_type_rates` (passado) | **Mantido** | Referência para recálculo de billing histórico |
| `reservations` | **Não tocado** | Histórico financeiro — imutável |
| `rooms` | **Não tocado** | Permanecem no DB com `is_active = false` até purge Layer 2 |

> **Regra:** `ON DELETE CASCADE` é permitido **apenas** para dados operacionais/derivados como `ari_days`. Dados de histórico financeiro usam `ON DELETE RESTRICT` ou `ON DELETE SET NULL`.

### 21.5 Propagação do Filtro `deleted_at IS NULL`

Todos os endpoints que lêem `room_types` devem incluir `AND deleted_at IS NULL`:

| Endpoint / Query | Status |
|-----------------|--------|
| `GET /room_types` (`list_room_types`) | ✅ Implementado |
| `PATCH /room_types/{id}` (`update_room_type`) | ✅ Implementado |
| `POST /reservations` — validação de `room_type_id` | ✅ Implementado |
| `POST /reservations/actions/quote` — validação de `room_type_id` | ✅ Implementado |
| `GET /occupancy` — CTE `room_types_for_property` | ✅ Implementado |
| `GET /occupancy/grid` — JOIN `room_types rt` | ✅ Implementado |

### 21.6 Migração `030_room_types_soft_delete`

```sql
-- migrations/sql/030_room_types_soft_delete.sql
ALTER TABLE room_types
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_room_types_deleted_at
    ON room_types (deleted_at);
```

Alembic revision: `030_room_types_soft_delete` → revises `029_reservations_hold_nullable`.

### 21.7 Endpoint atualizado: `DELETE /room_types/{id}`

- **Antes (hard delete):** `DELETE FROM room_types WHERE …` → destruía a linha e quebrava FKs históricas.
- **Depois (soft delete):** `UPDATE room_types SET deleted_at = now(), updated_at = now() WHERE … AND deleted_at IS NULL`.
- Retorna **204** em caso de sucesso; **404** se não encontrado ou já soft-deleted; **409** com `code: "active_references"` se houver quartos ativos ou reservas abertas.

---

## 22) Ciclo de Vida Profissional de Reservas — PMS Status Flow

### 22.1 Visão Geral

Reservas manuais criadas pelo dashboard percorrem um ciclo de vida explícito antes de chegarem ao status `confirmed`. Isso garante que nenhuma reserva seja considerada "confirmada" sem que o pagamento tenha sido verificado por um colaborador autorizado.

Reservas originadas por canal de vendas externo (hold-based, via `hold_id IS NOT NULL`) entram diretamente como `confirmed` porque o processamento de pagamento ocorre na plataforma de pagamentos e o evento de confirmação é recebido via webhook antes da reserva ser criada.

### 22.2 Máquina de Estados

```
                    [canal/webhook]
                          │
                          ▼
             POST /reservations    ──▶  pending_payment ──[pagamento >= threshold]──▶  confirmed (auto)
             (staff+, manual)                │                                               │
                                             │──[Garantir Reserva, manager+]────────▶  confirmed (manual)
                                             │ (staff+)                                      │ (staff+)
                                             ▼                                               ▼
                                        cancelled                                         in_house
                                                                                              │ (staff+)
                                                                                              ▼
                                                                                         checked_out
```

| Transição | Gatilho | Endpoint | Papel mínimo | Efeito colateral |
|-----------|---------|----------|-------------|-----------------|
| `pending_payment → confirmed` (auto) | Total de pagamentos folio ≥ `confirmation_threshold × total_cents` | interno (`folio_service`) | — | Audit log `changed_by = 'system'`, notes = "Payment Threshold Reached" |
| `pending_payment → confirmed` (manual) | Ação "Garantir Reserva" com justificativa obrigatória | `PATCH /reservations/{id}/status` | **manager** | Salva `guarantee_justification` na reserva; audit log `"Manual Guarantee: <texto>"` |
| `pending_payment → cancelled` | Cancelamento manual | `PATCH /reservations/{id}/status` | staff | Decrementa `inv_booked` para todas as noites da estadia |
| `confirmed → in_house` | Check-in | `POST /reservations/{id}/actions/check-in` | staff | Valida quarto atribuído e `governance_status = 'clean'` |
| `in_house → checked_out` | Check-out | `POST /reservations/{id}/actions/check-out` | staff | Valida saldo zero no folio; marca quarto como `dirty` |

Qualquer outra combinação `(from_status, to_status)` é rejeitada com **409 Conflict**.

### 22.3 Confirmação: Automática por Threshold e Manual por Garantia

A transição `pending_payment → confirmed` pode ocorrer de **duas formas**:

#### Automática (Payment Threshold)

Após cada registro de pagamento folio (`POST /reservations/{id}/payments`), o serviço `folio_service._maybe_auto_confirm` verifica:

```
total_capturedfolio / reservation.total_cents >= property.confirmation_threshold
```

Se a condição for satisfeita e a reserva ainda estiver em `pending_payment`, o status é atualizado atomicamente na mesma transação, e o audit log recebe `changed_by = 'system'`, `notes = 'Payment Threshold Reached'`. Um evento `reservation.confirmed` é emitido no outbox.

`confirmation_threshold` é um campo `NUMERIC NOT NULL DEFAULT 1.0` na tabela `properties`. O valor padrão de `1.0` exige pagamento integral. Valores menores (ex: `0.3`) permitem confirmação com pagamento parcial.

#### Manual (Garantir Reserva)

Um `manager` ou `owner` pode confirmar manualmente via `PATCH /reservations/{id}/status` com:
- `to_status: "confirmed"`
- `guarantee_justification: "<texto obrigatório e não vazio>"`

O endpoint valida que `guarantee_justification` não está vazio (HTTP 422 se omitido) e:
1. Atualiza `reservations.guarantee_justification` com o texto fornecido.
2. Registra no audit log com `changed_by = ctx.user.id`, `notes = "Manual Guarantee: <texto>"`.

Na UI, o botão **"Garantir Reserva"** (componente `GuaranteeButton`) abre um modal com textarea obrigatória — o botão de confirmação permanece desabilitado enquanto o campo estiver vazio.

### 22.4 Inventário durante `pending_payment`

No momento da criação via `POST /reservations`, o sistema executa as mesmas verificações de disponibilidade de uma reserva confirmada e chama `increment_inv_booked` para cada noite. Isso significa que **uma reserva `pending_payment` já ocupa inventário** desde o instante de sua criação — o quarto não pode ser vendido duas vezes enquanto o pagamento não for confirmado ou a reserva não for cancelada.

Se a reserva for cancelada (`pending_payment → cancelled`), `decrement_inv_booked` é chamado atomicamente na mesma transação, liberando o inventário.

### 22.5 Migração `031_pending_payment_status`

- Adiciona `'pending_payment'` ao tipo enum `reservation_status` via `ALTER TYPE ... ADD VALUE IF NOT EXISTS`.
- Recria a constraint `no_physical_room_overlap` incluindo `pending_payment` na cláusula `WHERE` (ver §24.3).
- Cria a tabela `reservation_status_logs` (ver §23).

> **Nota de migração:** `ALTER TYPE ADD VALUE` não pode ser executado dentro do mesmo bloco de transação que referencia o novo valor. O arquivo de migração Python emite `op.execute("COMMIT")` entre o `ADD VALUE` e a recriação da constraint para garantir que o valor esteja visível no catálogo antes do DDL subsequente.

---

## 23) Trilha de Auditoria e Conformidade — Reservation Status Logs

### 23.1 Propósito

Toda transição de status de uma reserva deve ser rastreável: **quem** realizou a ação, **quando**, **de qual estado** partiu, **para qual estado** foi, e com quais **notas** de justificativa. Essa trilha é exigência de conformidade PMS e insumo essencial para disputas financeiras e auditorias internas.

### 23.2 Estrutura da Tabela `reservation_status_logs`

```sql
CREATE TABLE reservation_status_logs (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    reservation_id TEXT        NOT NULL,
    property_id    TEXT        NOT NULL,
    from_status    TEXT,                       -- NULL em criação direta (futuro)
    to_status      TEXT        NOT NULL,
    changed_by     TEXT        NOT NULL,       -- Clerk user_id do operador
    changed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes          TEXT                        -- Justificativa opcional
);

CREATE INDEX idx_rsl_reservation ON reservation_status_logs (reservation_id, changed_at DESC);
CREATE INDEX idx_rsl_property    ON reservation_status_logs (property_id,    changed_at DESC);
```

| Coluna | Tipo | Descrição |
|--------|------|-----------|
| `id` | UUID | PK gerado automaticamente |
| `reservation_id` | TEXT | FK lógica para `reservations.id` (sem FK física para permitir histórico independente) |
| `property_id` | TEXT | Escopo de tenant — permite consultas de auditoria por propriedade |
| `from_status` | TEXT | Status anterior; `NULL` quando não aplicável |
| `to_status` | TEXT | Novo status após a transição |
| `changed_by` | TEXT | `ctx.user.id` — ID do usuário Clerk que executou a ação |
| `changed_at` | TIMESTAMPTZ | Timestamp da transição (UTC, default `now()`) |
| `notes` | TEXT | Campo livre preenchido pelo operador via UI (opcional) |

### 23.3 Comportamento de Escrita

A inserção em `reservation_status_logs` ocorre **dentro da mesma transação** que o `UPDATE reservations SET status = ...` no endpoint `PATCH /reservations/{id}/status`. A atomicidade garante que nunca haverá uma transição sem log nem um log sem transição correspondente.

```python
# Trecho de reservations.py — dentro de with txn() as cur:
cur.execute(
    """
    INSERT INTO reservation_status_logs
        (reservation_id, property_id, from_status, to_status, changed_by, notes)
    VALUES (%s, %s, %s, %s, %s, %s)
    """,
    (reservation_id, ctx.property_id, from_status, to_status, ctx.user.id, body.notes),
)
```

### 23.4 Índices e Padrões de Consulta

| Consulta típica | Índice utilizado |
|-----------------|-----------------|
| Histórico de uma reserva específica (timeline) | `idx_rsl_reservation` |
| Auditoria de todas as transições de uma propriedade por período | `idx_rsl_property` |

### 23.5 Idempotência

O endpoint `PATCH /reservations/{id}/status` utiliza o cabeçalho `Idempotency-Key` para evitar dupla-inserção em caso de retry. A chave é registrada na tabela `idempotency_keys` ao final da transação; requisições duplicadas retornam o corpo da resposta original sem executar novamente a transição nem inserir nova linha no log.

---

## 24) Cálculo de Ocupação e Inventário

### 24.1 `OPERATIONAL_STATUSES` — Definição Centralizada

O conjunto de status que "ocupam" inventário é definido em `src/hotelly/domain/room_conflict.py` como:

```python
OPERATIONAL_STATUSES = ("confirmed", "in_house", "checked_out", "pending_payment")
```

Essa tupla é importada por todos os componentes que precisam filtrar reservas operacionais: `occupancy.py`, `check_room_conflict()` e a constraint de banco de dados `no_physical_room_overlap`. Alterar `OPERATIONAL_STATUSES` propaga o efeito para todos os pontos de uso automaticamente — **não é necessário atualizar múltiplos arquivos manualmente**.

### 24.2 `booked_agg` — UNION ALL para Dois Tipos de Reserva

O endpoint `GET /occupancy` utiliza uma CTE `booked_agg` que combina via `UNION ALL` dois ramos distintos de contagem de noites reservadas:

```sql
booked_agg AS (
    SELECT room_type_id, date, SUM(qty) AS booked
    FROM (
        -- Ramo 1: Reservas de canal (hold-based)
        -- Conta via hold_nights para precisão de datas de hold.
        SELECT hn.room_type_id, hn.date, hn.qty
        FROM hold_nights hn
        JOIN reservations r ON r.hold_id = hn.hold_id
        WHERE r.property_id = %s
          AND r.status = ANY(%s::reservation_status[])
          AND hn.date >= %s AND hn.date < %s

        UNION ALL

        -- Ramo 2: Reservas manuais (hold_id IS NULL)
        -- Expande o intervalo checkin→checkout em linhas por noite via generate_series.
        SELECT r.room_type_id,
               gs.date::date,
               1 AS qty
        FROM reservations r
        CROSS JOIN LATERAL generate_series(
            r.checkin,
            r.checkout - interval '1 day',
            '1 day'
        ) AS gs(date)
        WHERE r.property_id = %s
          AND r.hold_id IS NULL
          AND r.status = ANY(%s::reservation_status[])
          AND r.checkin  < %s
          AND r.checkout > %s
    ) nights
    GROUP BY room_type_id, date
)
```

**Ramo 1 (hold-based):** reservas originadas de canal externo têm suas noites pré-computadas em `hold_nights`. A contagem é feita via join com essa tabela, preservando a granularidade exata definida no hold.

**Ramo 2 (manual):** reservas manuais (`hold_id IS NULL`) não têm `hold_nights`. O `CROSS JOIN LATERAL generate_series(checkin, checkout - 1 day, '1 day')` expande o intervalo de datas em uma linha por noite dinamicamente. O filtro de sobreposição `r.checkin < end_date AND r.checkout > start_date` garante que apenas reservas que se sobrepõem ao período de consulta sejam expandidas.

### 24.3 `no_physical_room_overlap` — Constraint de Exclusão Atualizada

A constraint de banco de dados que impede dupla-alocação de quartos físicos foi atualizada pela migração `031` para incluir `pending_payment`:

```sql
ALTER TABLE reservations
    ADD CONSTRAINT no_physical_room_overlap
    EXCLUDE USING GIST (
        room_id WITH =,
        daterange(checkin, checkout, '[)') WITH &&
    )
    WHERE (
        room_id IS NOT NULL
        AND status IN (
            'confirmed'::reservation_status,
            'in_house'::reservation_status,
            'checked_out'::reservation_status,
            'pending_payment'::reservation_status  -- adicionado em 031
        )
    );
```

Isso significa que qualquer tentativa de `INSERT` ou `UPDATE` que coloque dois registros de status operacional no mesmo quarto físico para datas sobrepostas será **rejeitada pelo banco de dados** — independentemente de a aplicação ter validado ou não. A constraint é a segunda camada de defesa (a primeira é `check_room_conflict()` na camada de aplicação).

### 24.4 Cálculo de Disponibilidade

Para cada combinação `(room_type_id, date)`, a disponibilidade é calculada como:

```
available = max(0, inv_total - booked - held)
```

- `inv_total` — capacidade total cadastrada em `ari_days`
- `booked` — soma de `booked_agg` (inclui `pending_payment`)
- `held` — soma de `held_agg` (holds ativos não expirados)

Se `available_raw < 0`, o endpoint registra um aviso de **overbooking** nos logs (sem PII) e retorna `available = 0` para a UI.

### 24.5 Grid Gantt (`GET /occupancy/grid`)

O endpoint `/occupancy/grid` retorna spans por quarto físico para renderização em estilo Gantt. Diferentemente de `GET /occupancy` (que agrega por `room_type_id`), este endpoint opera em nível de `room_id` individual.

- Cada reserva é retornada como um único span `(checkin, checkout, status, guest_name)` — sem expansão por dia.
- Quartos sem reservas no período aparecem com `reservations: []`.
- Filtra por `OPERATIONAL_STATUSES` (incluindo `pending_payment`).
- JOIN com `room_types` aplica `AND rt.deleted_at IS NULL` para excluir categorias soft-deleted.

---

## 25) Padrões Operacionais da Interface — UI Operational Standards

### 25.1 Campos Obrigatórios em Reservas Manuais

O dialog `NewReservationDialog` impõe dois campos obrigatórios antes de permitir a criação de uma reserva manual:

| Campo | Validação no frontend | Mensagem de erro |
|-------|-----------------------|-----------------|
| **Hóspede** | `selectedGuest !== null` | "Selecione um hóspede antes de criar a reserva." |
| **Quarto** | `roomId !== ""` | "Selecione um quarto antes de criar a reserva." |

**Mecanismo de validação:**
- O botão "Criar" fica desabilitado enquanto `missingRequired = !selectedGuest || !roomId`.
- `handleSubmit` possui verificações de retorno antecipado (`early return`) para ambos os campos, evitando qualquer chamada à API com dados incompletos.
- Labels exibem `*` em vermelho (`<span style={{ color: "#c53030" }}>*</span>`) para sinalizar obrigatoriedade.
- Bordas dos inputs ficam vermelhas (`border-color: #f87171`) enquanto os campos estão vazios.
- A opção "Sem quarto específico" foi removida do seletor de quartos — toda reserva manual requer atribuição de quarto no momento da criação.

O backend também valida: `POST /reservations` retorna **422** se `room_type_id` ou `room_id` não pertencerem à propriedade, ou se `guest_id` não for encontrado.

### 25.2 Correção de Desnormalização — `COALESCE(r.guest_name, g.full_name)`

O campo `reservations.guest_name` é uma coluna desnormalizada que é preenchida na criação da reserva com o `full_name` do hóspede no momento do cadastro. Em cenários onde o hóspede foi vinculado por um caminho não-padrão, esse campo pode ser `NULL` mesmo que `guest_id` esteja preenchido.

Ambas as funções `_list_reservations` e `_get_reservation` utilizam `COALESCE` para resolver o nome correto:

```sql
COALESCE(r.guest_name, g.full_name) AS guest_name
```

com o JOIN correspondente:

```sql
LEFT JOIN guests g ON g.id = r.guest_id AND g.property_id = r.property_id
```

Isso garante que a coluna `guest_name` retornada pela API sempre apresente o nome mais recente do cadastro quando o campo desnormalizado estiver ausente.

### 25.3 Nome Amigável do Quarto — `room_name` via LEFT JOIN

Para evitar exibição de UUIDs brutos na interface, ambas as funções de consulta de reservas incluem um JOIN adicional com a tabela `rooms`:

```sql
LEFT JOIN rooms ro ON ro.id = r.room_id AND ro.property_id = r.property_id
```

O campo `ro.name AS room_name` é retornado no JSON da reserva e exibido:

- **Lista de reservas** (`reservations/page.tsx`): coluna "Quarto" entre "Hóspede" e "Check-in"; exibe o nome (ex.: "Ap. 1") ou `–` se não atribuído.
- **Detalhe da reserva** (`[reservationId]/page.tsx`): campo "Quarto" no painel de informações; fallback para UUID truncado se `room_name` for nulo (reserva antiga sem JOIN), e "Não atribuído" se `room_id` também for nulo.

### 25.4 Botões Operacionais na Lista de Reservas

A coluna "Ações" da lista de reservas exibe botões contextuais baseados no status da reserva e no papel do usuário. A lógica de renderização ocorre **no servidor** (server component), evitando flickers de hidratação:

| Status | Botão | Cor | Condição adicional | Papel mínimo |
|--------|-------|-----|--------------------|-------------|
| `pending_payment` | **Confirmar Pgto** | Verde `#1e7e34` | — | manager |
| `confirmed` | **Check-in** | Verde `#1e7e34` | `checkin === hoje (UTC)` | staff |
| `in_house` | **Check-out** | Azul `#1d4ed8` | — | staff |

**Implementação:**

```tsx
// page.tsx (server component)
const todayISO = new Date().toISOString().split("T")[0]; // UTC

// Na célula de Ações:
{canConfirmPayment && statusStr === "pending_payment" && idStr && (
  <GuaranteeButton propertyId={propertyId} reservationId={idStr} />
)}
{statusStr === "confirmed" && checkinStr === todayISO && idStr && (
  <CheckInButton propertyId={propertyId} reservationId={idStr} status={statusStr} />
)}
{statusStr === "in_house" && idStr && (
  <CheckOutButton propertyId={propertyId} reservationId={idStr} status={statusStr} />
)}
```

Os componentes `CheckInButton` e `CheckOutButton` (em `[reservationId]/`) são importados e reutilizados diretamente na lista, mantendo a paridade de comportamento entre a visão de lista e a visão de detalhe. Após a ação bem-sucedida, `router.refresh()` recarrega os dados do servidor sem navegação de página.

### 25.5 Limpeza da Coluna JSON

A coluna de depuração JSON foi minimizada para reduzir ruído visual:

- Cabeçalho renomeado de "Detalhes" para `···` (cinza claro, peso normal).
- O elemento `<details>` exibe `···` como `<summary>`, ocupando apenas 32px de largura.
- Ao expandir, o `<pre>` é posicionado com `position: absolute; z-index: 10` para flutuar sobre as linhas da tabela, evitando empurrar o layout.
- Essa coluna destina-se exclusivamente a depuração em desenvolvimento; em produção pode ser ocultada via CSS sem impacto funcional.
