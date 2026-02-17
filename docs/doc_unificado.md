# Hotelly — Documento Unificado (Spec + Runbook + Roadmap)

> Fonte única de referência para execução do projeto Hotelly.
>
> **Atualização:** 2026-02-05  
> **Escopo:** Admin (Next.js) + Infra/Deploy + Roadmap

---

## Atualizações — decisões e URLs (2026-02-05)

- **Admin Staging:** `dash.hotelly.ia.br` → `hotelly-admin-staging` (Cloud Run)
- **Admin Prod:** `adm.hotelly.ia.br` → `hotelly-admin` (Cloud Run)
- **Public App:** `app.hotelly.ia.br` (Cloud Run)
- **Domínio técnico raiz:** `hotelly.ia.br` → redirect 308 para `hotelly.com.br`

> Nota: o Roadmap original referenciava `dashboard.hotelly.ia.br` para o Admin. A URL atual é `dash.hotelly.ia.br`.

## Decisões operacionais

- **Padrão único de tenancy/RBAC (property-scoped):** endpoints de dashboard recebem `property_id` **obrigatoriamente via `?property_id=`**, validado por `require_property_role(min_role)`; **não aceitar `property_id` no body**.


---

## 1. Spec Pack Técnico

# Spec Pack Técnico — Consolidado (v1 + v2 + v3)

> Documento consolidado atualizado em **05/02/2026** com a Fase 13 (Calendário de Preços PAX).

---

## 1) spec-pack-tecnico (v1)

# Hotelly V2 — Spec Pack técnico (consolidado da documentação)

- [GLOBAL] Core transacional é determinístico; IA não decide estado crítico.
- [GLOBAL] Transações críticas devem garantir: 0 overbooking, idempotência real e concorrência correta.
- [GLOBAL] Timestamps `*_at` são TIMESTAMPTZ e sempre UTC; `date` é DATE (sem hora).
- [GLOBAL] Nomenclatura canônica de schema/campos:
  - `*_cents` INT (valores em centavos); não usar `total_amount_cents` nem `day` nem variações fora da tabela canônica.
  - `currency` TEXT ISO-4217 (ex.: BRL).
  - `*_id` TEXT ou UUID.
- [STACK] Infra base: GCP + Cloud Run + FastAPI + Cloud SQL Postgres + Cloud Tasks + Secret Manager + Stripe + WhatsApp (Evolution primeiro; Meta sob demanda).
- [REGION] Região padrão: `us-central1` (Iowa) para Cloud Run/Cloud SQL/Cloud Tasks/Secret Manager; piloto sem HA.
- [SERVICES] Split obrigatório em 2 serviços Cloud Run:
  - `public-api`: expõe webhooks/APIs mínimas; valida assinatura/auth; faz dedupe/idempotência quando aplicável; enqueue; responde 2xx.
  - `worker`: privado (sem endpoint público / ingress interno); executa tasks/rotinas; aplica transações críticas no Postgres; emite outbox (sem PII).
- [PUBLIC ENDPOINT RULE] Endpoint público faz somente: (1) validação assinatura/auth, (2) receipt/dedupe durável quando aplicável, (3) enqueue, (4) responde 2xx; sem lógica pesada/transacional no público.
- [TASK AUTH] Cloud Tasks chamando worker devem usar OIDC (service account invoker).
- [CLOUD TASKS DEDUPE] `create_task` pode retornar 409 AlreadyExists (dedupe por nome). Isso deve ser tratado como **sucesso idempotente** (não 500).
- [DB SOURCE OF TRUTH] Fonte da verdade do schema são as migrations em `/migrations`; arquivos `docs/data/*.sql` são referência humana (não "aplicar na mão").
- [DB CONSTRAINTS] Guardrails por constraints/UNIQUE (derivados dos ADRs):
  - Dedupe eventos: `processed_events(source, external_id)` UNIQUE (manter consistente com migrations).
  - 1 reserva por hold: `reservations(property_id, hold_id)` UNIQUE.
  - Payment canonical: `payments(property_id, provider, provider_object_id)` UNIQUE (ex.: Stripe checkout.session.id).
  - Idempotency keys persistidas: `idempotency_keys(property_id, scope, key)` UNIQUE/PK.
- [DB LOCKING] Em operações que competem no mesmo hold (expire/cancel/convert): `SELECT ... FOR UPDATE` no hold.
- [DB DEADLOCK AVOIDANCE] Ao tocar várias noites, iterar sempre em ordem fixa: **(room_type_id, date ASC)**; updates de ARI devem seguir essa ordem.
- [ARI INVARIANTS] Inventário nunca negativo e nunca excedido:
  - `inv_total >= inv_booked + inv_held` para todas as noites.
  - Guardas no `WHERE` dos updates (ex.: só incrementa hold se houver saldo; só decrementa se `inv_held >= 1`).
  - Validar "1 linha por noite"; se alguma noite afetar 0 linhas => rollback (sem hold parcial / sem ajuste parcial).
- [IDEMPOTENCY] Idempotência ponta a ponta é composta por:
  - `processed_events` para eventos externos/tasks,
  - `task_id` determinístico em Cloud Tasks,
  - UNIQUEs no banco (última linha de defesa),
  - `idempotency_keys` para endpoints que aceitam Idempotency-Key (escopo + key).
- [OUTBOX] `outbox_events` é append-only, payload mínimo e sem PII.
- [OUTBOX WRITE RULE] Toda ação crítica que altera estado (hold/payment/reservation) deve escrever outbox **na mesma transação**.
- [OUTBOX PAYLOAD] Proibido no payload: telefone/email/nome/endereço/documento/texto de chat; proibido payload bruto de Stripe/WhatsApp.
- [MESSAGE PERSISTENCE] MVP/Piloto: **não persistir mensagens** (inbound/outbound) no Postgres.
  - Persistido: `processed_events`, entidades transacionais (holds/payments/reservations), `outbox_events` (mínimo, sem PII).
- [AI ROLE] IA no MVP só para roteamento/extração; sempre substituível; core continua determinístico.
- [AI INPUT] Entrada para IA deve ser redigida; nunca enviar payload bruto de webhook, tokens, segredos, ou dados sensíveis não essenciais.
- [AI OUTPUT CONTRACT] Saída da IA é JSON estrito (schema versionado); se JSON inválido/enum desconhecido/slots incoerentes => fallback determinístico.
- [AI SCHEMA v1.0] `IntentOutput` (json-schema):
  - required: `schema_version` = "1.0", `intent` ∈ {quote_request, checkout_request, cancel_request, human_handoff, unknown}, `confidence` ∈ [0,1]
  - opcionais: `entities` {checkin(date), checkout(date), guest_count(1..20), room_type_id(string)}, `reason` (<=200).
- [AI PROMPT RULE] Prompt retorna **apenas JSON**; sem PII; se incerto => `intent="unknown"` + `reason`.
- [PII DEFINITION] PII inclui: telefone (qualquer formato), conteúdo de mensagem, email, documento, endereço completo, nome (quando ligado ao contato), identificadores "sendable" (ex.: remote_jid/wa_id).
- [PII GOLD RULE] Proibido logar payload bruto/request body/mensagens/telefone/nome/remote_jid; isso é incidente.
- [CONTACT HASH] Identidade de contato no pipeline deve ser via `contact_hash` (hash com secret; não reversível sem secret).
- [CONTACT VAULT] Resolver destinatário outbound via "contact_refs vault":
  - Mapeamento: (property_id, channel, contact_hash) → remote_jid (criptografado).
  - Criptografia obrigatória: AES-256-GCM (ou equivalente) com key simétrica `CONTACT_REFS_KEY` (Secret Manager/env).
  - TTL curto: expira em no máximo 1 hora (padrão 1h, configurável).
  - Acesso restrito: somente handlers internos de envio (sender) leem o vault; `handle-message` não lê e o worker não escreve no vault.
  - Nunca logar remote_jid descriptografado.
- [VAULT WRITE] Endpoint público (inbound) grava no vault ao receber mensagem; worker não escreve no vault.
- [OUTBOUND FLOW] Worker grava outbox PII-free (com contact_hash); sender consulta vault (contact_hash→remote_jid) e envia; se vault não tiver entrada => não enviar e registrar erro (comportamento intencional).
- [WHATSAPP PIPELINE] Pipeline único obrigatório:
  - inbound (public) → normalize → receipt/dedupe → enqueue (task) → worker processa → outbox → sender (task) envia.
  - Proibido qualquer "caminho alternativo rápido" fora do pipeline (exceto debug/legado; ver `POST /tasks/whatsapp/send-message`).
- [WHATSAPP PROVIDERS] Fase 1: Evolution como adapter único; Fase 2: Meta Cloud API como segundo adapter mantendo mesmo contrato/pipeline.
- [EVOLUTION OUTBOUND ENV] Outbound via Evolution usa env vars (nomes reais do código): `EVOLUTION_BASE_URL`, `EVOLUTION_INSTANCE`, `EVOLUTION_API_KEY` (secret). `EVOLUTION_SEND_PATH` é opcional (default `/message/sendText/{instance}`).
- [WHATSAPP TASK HTTP SEMANTICS] Tasks internas (Cloud Tasks) devem retornar: **5xx** em falha transitória (timeout/rede/5xx/429) para habilitar retry; **2xx** em falha permanente (ex.: `contact_ref_not_found`, 401/403, template inválido, config/env/secret faltando) para parar retry.
- [OUTBOX DELIVERY GUARD] Envio outbound é at-least-once (retry). Para evitar duplicação, usar guard durável por `outbox_event_id` (ex.: `outbox_deliveries` com UNIQUE) e marcar `sent`/`failed_permanent` + `attempt_count` + `last_error` (PII-safe).
- [WORKER PII-FREE] Worker `handle-message` é PII-free: não recebe/persiste `text`, `phone/sender_id`, `remote_jid/wa_id`, `payload/raw`, `name`.
- [INBOUND CONTRACT] Payload canônico para worker (`InboundMessage`, PII-free):
  - `provider`: "evolution"
  - `message_id`: string (dedupe key)
  - `property_id`: string
  - `correlation_id`: string (gerado no public)
  - `contact_hash`: string (base64url sem padding, 32 chars)
  - `kind`: text|interactive|media|unknown
  - `received_at`: ISO8601 UTC
- [WHATSAPP TIMEOUT] Worker/task timeout recomendado: 30–60s.
- [STRIPE PRINCIPLES] Webhook Stripe (public) faz: verificar assinatura + receipt durável (dedupe por `event.id`) + enqueue; conversão HOLD→RESERVATION só no worker em transação crítica; nunca logar payload bruto Stripe.
- [STRIPE LOG ALLOWLIST] Permitido logar: `event.id`, `event.type`, `checkout_session_id`, `payment_id`, `hold_id`, `property_id`, `correlation_id`, status, duration_ms, attempts.
- [LOGGING FORMAT] Logs sempre estruturados (JSON por linha) com campos mínimos: severity, timestamp, service, env, correlation_id, event_name.
- [CORRELATION] Correlation ID end-to-end: request → task → DB txn → outbound.
  - Se vier `X-Correlation-Id`, validar e reutilizar; senão gerar.
  - Cloud Tasks devem propagar `X-Correlation-Id` e `X-Event-Source=tasks`.
- [METRICS LABELS] Labels permitidos (baixa cardinalidade): env, service, event_source, provider, status, error_code.
  - Proibidos como label: phone, message_id, hold_id (alta cardinalidade).
- [IDEMPOTENCY OBSERVABLE] Todo dedupe/no-op deve ser medido e/ou logado (sem PII).
- [RETENTION] Limpeza periódica (idempotente e segura); recomendação: Cloud Scheduler + Cloud Run Job (ou worker interno).
  - Frequência recomendada diária para `processed_events`, `outbox_events`, `idempotency_keys`.
  - Nunca logar payload dos registros limpos; só contagens.
- [PILOT LIMITS] Piloto: até 10 pousadas, sem HA, usuários cientes de falhas; foco em observabilidade e aprendizado.
- [QUALITY GATES] Gates normativos (quando afetar transação crítica/dinheiro/inventário):
  - G0: compileall + build docker + /health.
  - G1: migrate up em DB vazio + migrate up idempotente + constraints críticas presentes.
  - G3–G5 obrigatórios para mudanças em transações críticas (retry/idempotência/concorrência/race).
  - (Test plan) Create Hold: provar Idempotency-Key real + guarda ARI + ordem determinística + outbox na mesma transação + concorrência 20→1.
  - (Test plan) Expire Hold: dedupe por processed_events(tasks, task_id) + FOR UPDATE + inv_held-- + outbox hold.expired + replay no-op.
- [CI/CD NAMING] Segredos por ambiente (recomendado): `hotelly-{env}-db-url`, `hotelly-{env}-stripe-secret-key`, `hotelly-{env}-stripe-webhook-secret`, `hotelly-{env}-whatsapp-verify-token`, `hotelly-{env}-whatsapp-app-secret` (se aplicável), `hotelly-{env}-internal-task-secret` (se usar header).
- [CLOUD TASKS QUEUES] Filas por ambiente (recomendado): `hotelly-{env}-default`, `hotelly-{env}-expires`, `hotelly-{env}-webhooks`.
- [LOCAL DEV COMMANDS] Comandos "oficiais" esperados (se não existir no repo, vira tarefa):
  - `./scripts/dev.sh` (subir app com hot-reload)
  - `./scripts/verify.sh` (rodar checks)
  - `uv run pytest -q`
  - `python -m compileall -q src` (ou raiz)
- [INCIDENT SEV0] Stop-ship inclui: overbooking/inventário negativo, reserva duplicada, pagamento confirmado sem trilha de reprocesso, vazamento de PII em logs, endpoint interno exposto publicamente.

---

## 2) spec-pack-tecnico-v2 (v2)

# Hotelly V2 — Spec Pack Técnico (v2.0 - Atualizado 05/02/2026)

## Changelog v2.2 (05/02/2026)
- Adicionado migration 010: room_type_rates (PAX pricing)
- Adicionado endpoints GET/PUT /rates
- Atualizado schema room_type_rates com campos reais (centavos)
- Adicionado seção de Admin para página de Tarifas

## Changelog v2.1 (04/02/2026)
- Atualizado endpoints do Dashboard com occupancy, rooms, assign-room
- Atualizado schema de reservations (room_id, room_type_id)
- Adicionado regra RBAC uniforme (property_id obrigatório em todos os endpoints)
- Adicionado regra de deploy staging (rebuild → migrate → redeploy)
- Migrations atualizadas até 009

## Changelog v2.0
- Adicionado modelo de pricing por PAX (ocupação)
- Adicionado estrutura de rooms (unidades físicas)
- Adicionado políticas de criança e cancelamento
- Atualizado configurações de produção GCP
- Adicionado seção de Admin/Dashboard MVP
- Adicionado seção de Autenticação (Clerk)
- Atualizado Evolution API para v2.3.6

---

## SEÇÃO 1: REGRAS GLOBAIS

- [GLOBAL] Core transacional é determinístico; IA não decide estado crítico.
- [GLOBAL] Transações críticas devem garantir: 0 overbooking, idempotência real e concorrência correta.
- [GLOBAL] Timestamps `*_at` são TIMESTAMPTZ e sempre UTC; `date` é DATE (sem hora).
- [GLOBAL] Nomenclatura canônica de schema/campos:
  - `*_cents` INT (valores em centavos); não usar `total_amount_cents` nem `day` nem variações fora da tabela canônica.
  - `currency` TEXT ISO-4217 (ex.: BRL).
  - `*_id` TEXT ou UUID.

---

## SEÇÃO 2: STACK E INFRAESTRUTURA

- [STACK] Infra base: GCP + Cloud Run + FastAPI + Cloud SQL Postgres + Cloud Tasks + Secret Manager + Stripe + WhatsApp (Evolution API).
- [REGION] Região padrão: `us-central1` (Iowa) para Cloud Run/Cloud SQL/Cloud Tasks/Secret Manager; piloto sem HA.
- [SERVICES] Split obrigatório em 2 serviços Cloud Run:
  - `hotelly-public`: expõe webhooks/APIs; valida assinatura/auth; faz dedupe/idempotência; enqueue; responde 2xx.
  - `hotelly-worker`: privado (ingress interno); executa tasks/rotinas; aplica transações críticas no Postgres; emite outbox (sem PII).

### 2.1 Configuração de Produção GCP

```
Project: hotelly--ia
Region: us-central1

Cloud Run - hotelly-public:
  URL: https://app.hotelly.ia.br
  ENV:
    APP_ROLE=public
    TASKS_BACKEND=cloud_tasks
    GOOGLE_CLOUD_PROJECT=hotelly--ia
    GCP_PROJECT_ID=hotelly--ia
    GCP_LOCATION=us-central1
    GCP_TASKS_QUEUE=hotelly-default
    TASKS_OIDC_SERVICE_ACCOUNT=hotelly-worker@hotelly--ia.iam.gserviceaccount.com
    WORKER_BASE_URL=secret:hotelly-worker-url
    CONTACT_HASH_SECRET=secret:contact-hash-secret
    CONTACT_REFS_KEY=secret:contact-refs-key
    DATABASE_URL=secret:hotelly-database-url
    OIDC_ISSUER=secret:oidc-issuer
    OIDC_AUDIENCE=secret:oidc-audience
    OIDC_JWKS_URL=secret:oidc-jwks-url

Cloud Run - hotelly-worker:
  URL: https://hotelly-worker-678865413529.us-central1.run.app (interno)
  ENV:
    APP_ROLE=worker
    TASKS_OIDC_AUDIENCE=secret:hotelly-worker-url
    (+ mesmos secrets de DB/OIDC)

Cloud Tasks:
  Queue: hotelly-default
  Auth: OIDC com service account hotelly-worker@hotelly--ia.iam.gserviceaccount.com

Artifact Registry:
  Repositório: hotelly (NÃO hotelly-repo)
  Imagem: us-central1-docker.pkg.dev/hotelly--ia/hotelly/hotelly:latest
```

### 2.1.1 Configuração de Staging GCP (isolado)

Objetivo: staging **isolado de verdade** (DB + worker próprios) para validar E2E (Admin → API → Cloud Tasks → Worker → Outbox) sem tocar prod.

**Serviços (staging):**
- `hotelly-public-staging` (API)
- `hotelly-worker-staging` (worker)

**Cloud SQL (staging):**
- Instância: `hotelly--ia:us-central1:hotelly-sql` (mesma instância)
- Database: `hotelly_staging` (database separado)
- Usuário: `hotelly_staging_app`
- Secret: `hotelly-staging-database-url` (+ `hotelly-staging-db-password`)

**Regras operacionais críticas:**
- `WORKER_BASE_URL` (no `hotelly-public-staging`) deve apontar para o **URL canônico** do Cloud Run (`status.url`, domínio `*.a.run.app`), não para o alias `*.run.app`.
- `TASKS_BACKEND=cloud_tasks` é obrigatório (staging não pode ficar em `inline`).
- **Deploy staging sempre nesta ordem: rebuild imagem → migrate → redeploy** (worker + public). Se pular o rebuild, o worker roda com código antigo e pode dar `UndefinedColumn` ou erros similares.
- O worker-staging precisa:
  - **porta 8000** (deploy Cloud Run com `--port 8000`), pois o container do worker escuta em 8000.
  - Cloud SQL anexado (`run.googleapis.com/cloudsql-instances=hotelly--ia:us-central1:hotelly-sql`).
  - `TASKS_OIDC_AUDIENCE` alinhado com o próprio URL do worker-staging (ideal: secret dedicado `hotelly-worker-staging-url` contendo o `status.url` do worker-staging).

**⚠️ Job hotelly-migrate-staging está quebrado** — DATABASE_URL mal formatado. Usar cloud-sql-proxy manualmente para migrations.

**ENV mínimo (staging):**

`hotelly-public-staging`:
- `DATABASE_URL=secret:hotelly-staging-database-url`
- `OIDC_ISSUER=secret:oidc-issuer-dev`
- `OIDC_JWKS_URL=secret:oidc-jwks-url-dev`
- `OIDC_AUDIENCE=hotelly-api`
- `TASKS_BACKEND=cloud_tasks`
- `GCP_TASKS_QUEUE=hotelly-default`
- `WORKER_BASE_URL=https://<status.url do hotelly-worker-staging>`
- `TASKS_OIDC_SERVICE_ACCOUNT=hotelly-worker@hotelly--ia.iam.gserviceaccount.com`
- `GCP_PROJECT_ID=hotelly--ia`

`hotelly-worker-staging`:
- `APP_ROLE=worker`
- `DATABASE_URL=secret:hotelly-staging-database-url`
- `DB_PASSWORD=secret:hotelly-staging-db-password`
- `OIDC_ISSUER=secret:oidc-issuer-dev`
- `OIDC_JWKS_URL=secret:oidc-jwks-url-dev`
- `OIDC_AUDIENCE=secret:oidc-audience`
- `TASKS_OIDC_AUDIENCE=secret:hotelly-worker-staging-url`
- Cloud Run:
  - `--port 8000`
  - `--add-cloudsql-instances hotelly--ia:us-central1:hotelly-sql`


### 2.2 Autenticação (Clerk)

```
Clerk Production:
  Issuer: https://clerk.hotelly.ia.br
  JWKS: https://clerk.hotelly.ia.br/.well-known/jwks.json
  Audience: hotelly-api
  JWT Template: hotelly-api (lifetime 600s)
  
JWT Claims esperados:
  - sub: user_id (Clerk)
  - aud: hotelly-api
  - azp: application_id
  - metadata.property_ids: lista de properties do usuário
  - metadata.role: owner | manager | receptionist
```

---

## SEÇÃO 3: REGRAS DE ENDPOINTS E TASKS

- [PUBLIC ENDPOINT RULE] Endpoint público faz somente: (1) validação assinatura/auth, (2) receipt/dedupe durável quando aplicável, (3) enqueue, (4) responde 2xx; sem lógica pesada/transacional no público.
- [TASK AUTH] Cloud Tasks chamando worker devem usar OIDC (service account invoker).
- [CLOUD TASKS DEDUPE] `create_task` pode retornar 409 AlreadyExists (dedupe por nome). Isso deve ser tratado como **sucesso idempotente** (não 500).
- [WHATSAPP TIMEOUT] Worker/task timeout recomendado: 30–60s.

---

## SEÇÃO 4: BANCO DE DADOS

- [DB SOURCE OF TRUTH] Fonte da verdade do schema são as migrations em `/migrations`; arquivos `docs/data/*.sql` são referência humana.
- [DB CONSTRAINTS] Guardrails por constraints/UNIQUE:
  - Dedupe eventos: `processed_events(source, external_id)` UNIQUE.
  - 1 reserva por hold: `reservations(property_id, hold_id)` UNIQUE.
  - Payment canonical: `payments(property_id, provider, provider_object_id)` UNIQUE.
  - Idempotency keys: `idempotency_keys(property_id, scope, key)` UNIQUE/PK.
- [DB LOCKING] Em operações que competem no mesmo hold (expire/cancel/convert): `SELECT ... FOR UPDATE` no hold.
- [DB DEADLOCK AVOIDANCE] Ao tocar várias noites, iterar sempre em ordem fixa: **(room_type_id, date ASC)**.

### 4.1 Modelo de Pricing por PAX (Ocupação)

- **Estado atual (migration 010):** `price_1chd_cents`, `price_2chd_cents`, `price_3chd_cents`
- **Target (pós-Story 1):** `price_bucket1_chd_cents`, `price_bucket2_chd_cents`, `price_bucket3_chd_cents` + compat no `/rates` (GET retorna ambos; PUT aceita um ou outro; ambos divergentes ⇒ 400)

O sistema usa modelo de preço por ocupação (PAX), padrão do mercado hoteleiro brasileiro:

```sql
-- Tipos de quarto (categoria)
room_types:
  id: TEXT PK                   -- "rt_standard", "rt_suite"
  property_id: TEXT FK
  name: VARCHAR(100)            -- 'Suíte Deluxe', 'Chalé VIP'
  description: TEXT
  max_adults: INTEGER           -- capacidade máxima adultos
  max_children: INTEGER         -- capacidade máxima crianças
  max_occupancy: INTEGER        -- capacidade total
  amenities: JSONB              -- ['ar-condicionado', 'hidro']
  display_order: INTEGER
  is_active: BOOLEAN

-- Unidades físicas (quartos individuais) — migration 007
rooms:
  property_id: TEXT FK          -- tenant
  id: TEXT                      -- "101", "102", "201"
  room_type_id: TEXT FK         -- FK composta → room_types(property_id, id)
  name: TEXT                    -- "Quarto 101", "Suíte 201"
  is_active: BOOLEAN
  PK(property_id, id)

-- Tarifas por data (modelo PAX) — migration 010
room_type_rates:
  property_id: TEXT NOT NULL
  room_type_id: TEXT NOT NULL
  date: DATE NOT NULL
  
  -- Preços por ocupação adultos (em centavos)
  price_1pax_cents: INT         -- preço 1 adulto
  price_2pax_cents: INT         -- preço 2 adultos
  price_3pax_cents: INT         -- preço 3 adultos (nullable)
  price_4pax_cents: INT         -- preço 4 adultos (nullable)
  
  -- Adicional por criança (em centavos)
  price_1chd_cents: INT         -- LEGADO: adicional por criança (bucket 1 — 0–3) (nullable)
  price_2chd_cents: INT         -- LEGADO: adicional por criança (bucket 2 — 4–12) (nullable)
  price_3chd_cents: INT         -- LEGADO: adicional por criança (bucket 3 — 13–17) (nullable)
  
  -- Restrições
  min_nights: INT               -- mínimo de noites (nullable)
  max_nights: INT               -- máximo de noites (nullable)
  closed_checkin: BOOLEAN       -- não permite check-in (default false)
  closed_checkout: BOOLEAN      -- não permite check-out (default false)
  is_blocked: BOOLEAN           -- data indisponível (default false)
  
  created_at: TIMESTAMPTZ       -- default now()
  updated_at: TIMESTAMPTZ       -- default now()
  
  PRIMARY KEY (property_id, room_type_id, date)
  FOREIGN KEY (property_id) → properties(id) ON DELETE CASCADE
  FOREIGN KEY (property_id, room_type_id) → room_types(property_id, id) ON DELETE RESTRICT
  
  INDEXES:
    idx_room_type_rates_property_date (property_id, date)
    idx_room_type_rates_type_date (room_type_id, date)

-- Inventário
ari_days:
  property_id: TEXT FK
  room_type_id: TEXT FK
  date: DATE
  inv_total: INTEGER            -- total de unidades
  inv_booked: INTEGER           -- reservas confirmadas
  inv_held: INTEGER             -- holds ativos
  base_rate_cents: INTEGER      -- ⚠️ DEPRECATED: migrar para room_type_rates
  PK(property_id, room_type_id, date)
```

### 4.2 Políticas

> **LEGADO/DEPRECATED:** a tabela `child_policies` abaixo **não é usada** no plano atual. A fonte de verdade para políticas de criança é `property_child_age_buckets` (3 buckets, 0..17, sem overlap, cobertura completa).

```sql
-- Política de criança
child_policies:
  id: UUID PK
  property_id: UUID FK UNIQUE
  accepts_children: BOOLEAN
  free_child_min_age: INTEGER      -- 0
  free_child_max_age: INTEGER      -- 3 (até 3 anos grátis)
  free_child_counts_as_guest: BOOLEAN
  paid_child_min_age: INTEGER      -- 4
  paid_child_max_age: INTEGER      -- 10
  paid_child_counts_as_guest: BOOLEAN
  description: TEXT

-- Política de cancelamento
cancellation_policies:
  id: UUID PK
  property_id: UUID FK
  name: VARCHAR(100)
  penalty_type: VARCHAR(20)        -- 'free', 'partial', 'non_refundable'
  days_before_checkin: INTEGER
  penalty_percentage: DECIMAL(5,2)
  admin_fee: DECIMAL(10,2)
  description: TEXT
  is_active: BOOLEAN

cancellation_policy_periods:
  id: UUID PK
  policy_id: UUID FK
  start_date: DATE
  end_date: DATE
  is_priority: BOOLEAN
  is_continuous: BOOLEAN
```

### 4.3 Atualização de Reservations (implementado)

```sql
-- Migration 008: atribuição de quarto
ALTER TABLE reservations ADD COLUMN room_id TEXT;
-- FK composta: reservations(property_id, room_id) → rooms(property_id, id)

-- Migration 009: tipo de quarto na reserva
ALTER TABLE reservations ADD COLUMN room_type_id TEXT;
-- FK composta: reservations(property_id, room_type_id) → room_types(property_id, id)
-- Preenchido automaticamente pelo convert_hold e via COALESCE no assign-room

-- Campos planejados (futuro)
-- adults INTEGER DEFAULT 2
-- children INTEGER DEFAULT 0
-- children_ages INTEGER[]
-- original_total DECIMAL(10,2)
-- adjustment_amount DECIMAL(10,2) DEFAULT 0
-- adjustment_reason TEXT
```

### 4.4 Migrations (status atual)

```
001_initial.py           -- properties, room_types, ari_days, holds, hold_nights
002_conversations.py     -- conversations
003_contact_refs.py      -- contact_refs
004_payments.py          -- payments
005_reservations.py      -- reservations, processed_events, idempotency_keys
006_outbox_message_type.py -- outbox_events.message_type
007_add_rooms.py         -- rooms (unidades físicas)
008_add_reservation_room_id.py -- reservations.room_id
009_reservations_room_type_id.py -- reservations.room_type_id
010_room_type_rates.py   -- room_type_rates (PAX pricing) ← NOVO
```

---

## SEÇÃO 5: INVARIANTES DE INVENTÁRIO (ARI)

- [ARI INVARIANTS] Inventário nunca negativo e nunca excedido:
  - `inv_total >= inv_booked + inv_held` para todas as noites.
  - Guardas no `WHERE` dos updates (ex.: só incrementa hold se houver saldo; só decrementa se `inv_held >= 1`).
  - Validar "1 linha por noite"; se alguma noite afetar 0 linhas => rollback (sem hold parcial).

---

## SEÇÃO 6: IDEMPOTÊNCIA

- [IDEMPOTENCY] Idempotência ponta a ponta é composta por:
  - `processed_events` para eventos externos/tasks,
  - `task_id` determinístico em Cloud Tasks,
  - UNIQUEs no banco (última linha de defesa),
  - `idempotency_keys` para endpoints que aceitam Idempotency-Key.

---

## SEÇÃO 7: OUTBOX E PII

- [OUTBOX] `outbox_events` é append-only, payload mínimo e sem PII.
- [OUTBOX WRITE RULE] Toda ação crítica que altera estado (hold/payment/reservation) deve escrever outbox **na mesma transação**.
- [OUTBOX PAYLOAD] Proibido no payload: telefone/email/nome/endereço/documento/texto de chat; proibido payload bruto de Stripe/WhatsApp.
- [MESSAGE PERSISTENCE] MVP/Piloto: **não persistir mensagens** (inbound/outbound) no Postgres.
- [PII DEFINITION] PII inclui: telefone, conteúdo de mensagem, email, documento, endereço completo, nome (quando ligado ao contato), identificadores "sendable" (ex.: remote_jid/wa_id).
- [PII GOLD RULE] Proibido logar payload bruto/request body/mensagens/telefone/nome/remote_jid; isso é incidente.
- [CONTACT HASH] Identidade de contato no pipeline deve ser via `contact_hash` (hash com secret; não reversível sem secret).

---

## SEÇÃO 8: INTELIGÊNCIA ARTIFICIAL

- [AI ROLE] IA no MVP só para roteamento/extração; sempre substituível; core continua determinístico.
- [AI INPUT] Entrada para IA deve ser redigida; nunca enviar payload bruto de webhook, tokens, segredos.
- [AI OUTPUT CONTRACT] Saída da IA é JSON estrito (schema versionado); se JSON inválido/enum desconhecido => fallback determinístico.
- [AI SCHEMA v1.0] `IntentOutput`:
  - required: `schema_version` = "1.0", `intent` ∈ {quote_request, checkout_request, cancel_request, human_handoff, unknown}, `confidence` ∈ [0,1]
  - opcionais: `entities` {checkin(date), checkout(date), guest_count(1..20), room_type_id(string)}, `reason` (<=200).
- [AI PROMPT RULE] Prompt retorna **apenas JSON**; sem PII; se incerto => `intent="unknown"` + `reason`.

---

## SEÇÃO 9: WHATSAPP

- [WHATSAPP PIPELINE] Pipeline único obrigatório:
  - inbound (public) → normalize → receipt/dedupe → enqueue (task) → worker processa → outbox → sender (task) envia.
  - Proibido qualquer "caminho alternativo rápido" fora do pipeline (exceto debug/legado; ver `POST /tasks/whatsapp/send-message`).
- [WHATSAPP PROVIDERS] Evolution API v2.3.6 como adapter único no MVP.
- [WORKER PII-FREE] Worker `handle-message` é PII-free: não recebe/persiste `text`, `phone/sender_id`, `remote_jid/wa_id`, `payload/raw`, `name`.

### 9.1 Configuração Evolution API (Produção)

```
Evolution API:
  URL: https://edge.roda.ia.br/
  Instância: pousada-ia-v2
  API Key: secret:evolution-api-key
  Número WhatsApp: <redacted>
  
Webhook:
  URL: https://app.hotelly.ia.br/webhooks/whatsapp/evolution
  Headers: X-Property-Id: pousada-demo
  Events: MESSAGES_UPSERT
  
Fluxo validado:
  WhatsApp → Evolution → Hotelly Public → Cloud Tasks → Worker → Outbox ✓
```

### 9.2 Inbound Contract (PII-free)

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

---

## SEÇÃO 10: STRIPE

- [STRIPE PRINCIPLES] Webhook Stripe (public) faz: verificar assinatura + receipt durável (dedupe por `event.id`) + enqueue; conversão HOLD→RESERVATION só no worker.
- [STRIPE LOG ALLOWLIST] Permitido logar: `event.id`, `event.type`, `checkout_session_id`, `payment_id`, `hold_id`, `property_id`, `correlation_id`, status, duration_ms.

### 10.1 Configuração Stripe (Produção)

```
Secrets:
  stripe-secret-key: sk_live_... (Secret Manager)
  stripe-webhook-secret: whsec_... (Secret Manager)
  
Environment Variables (Cloud Run):
  STRIPE_SUCCESS_URL=https://app.hotelly.ia.br/stripe/success
  STRIPE_CANCEL_URL=https://app.hotelly.ia.br/stripe/cancel
  
Webhook (Stripe Dashboard):
  URL: https://app.hotelly.ia.br/webhooks/stripe
  Eventos: checkout.session.completed, payment_intent.succeeded
```

---

## SEÇÃO 11: LOGGING E OBSERVABILIDADE

- [LOGGING FORMAT] Logs sempre estruturados (JSON por linha) com campos mínimos: severity, timestamp, service, env, correlation_id, event_name.
- [CORRELATION] Correlation ID end-to-end: request → task → DB txn → outbound.
- [METRICS LABELS] Labels permitidos (baixa cardinalidade): env, service, event_source, provider, status, error_code.
  - Proibidos como label: phone, message_id, hold_id (alta cardinalidade).
- [IDEMPOTENCY OBSERVABLE] Todo dedupe/no-op deve ser medido e/ou logado (sem PII).

---

## SEÇÃO 12: ADMIN/DASHBOARD MVP

### 12.1 Escopo MVP

```
Telas obrigatórias:
  1. Dashboard (resumo do dia + métricas)
  2. Grid de Ocupação (mapa de reservas estilo Rooms)
  3. Lista de Reservas (tabela com filtros)
  4. Calendário de Preços (edição PAX por data) ← IMPLEMENTADO
  5. Edição em Lote (atualizar múltiplas datas)
  6. Configurações (política criança, cancelamento)
```

### 12.2 Endpoints do Dashboard

```
Existentes:
  GET  /auth/whoami
  GET  /me
  GET  /properties
  GET  /properties/{id}
  GET  /frontdesk/summary

  GET  /reservations
    - filtros (check-in): ?from=YYYY-MM-DD&to=YYYY-MM-DD&status=confirmed|cancelled
    - retorno: {"reservations": [{id, checkin, checkout, status, total_cents, currency, room_id, room_type_id, created_at}]}

  GET  /reservations/{id}
    - retorno: {id, checkin, checkout, status, total_cents, currency, hold_id, room_id, room_type_id, created_at}
    - room_id e room_type_id são nullable (reservas antigas podem não ter)

  POST /reservations/{id}/actions/resend-payment-link
    - 202 Accepted (idempotente)
    - gera task Cloud Tasks → worker → escreve outbox

  POST /reservations/{id}/actions/assign-room
    - body: {"room_id": "<room_id>"}
    - 202 Accepted (enqueue Cloud Task)
    - worker: valida room existe + is_active + room_type compatível
    - worker: atualiza room_id, preenche room_type_id via COALESCE quando NULL
    - worker: grava outbox 'room_assigned' com reservation_id no payload
    - dedupe: task_id determinístico (assign-room:{reservation_id}:{hash16})

  GET  /occupancy
property_id via `?property_id=` validado por RBAC
    - query: start_date, end_date (exclusivo, max 90 dias)
    - retorno: room_types com array de days (inv_total, booked, held, available)

  GET  /rooms
property_id via `?property_id=` validado por RBAC
    - retorno: [{id, room_type_id, name, is_active}]

  GET  /rates ← NOVO (Fase 13)
    - RBAC: viewer
    - query: start_date, end_date, room_type_id (opcional)
    - limite: max 366 dias de range
    - retorno: lista de rates com campos PAX
    - se room_type_id omitido: retorna todos os room_types da property

  PUT  /rates ← NOVO (Fase 13)
    - RBAC: staff
    - body: {"rates": [{room_type_id, date, price_1pax_cents, ...}]}
    - limite: max 366 rates por request
property_id via `?property_id=` validado por RBAC
    - bulk upsert via INSERT ... ON CONFLICT DO UPDATE
    - idempotente: updated_at atualizado em cada upsert
    - retorno: {"upserted": N}

  GET  /outbox
    - query obrigatória: property_id=<propertyId>
    - filtros: aggregate_type, aggregate_id, event_type, limit
    - retorno: {"events": [...]} (PII-safe, sem payload)

  GET  /payments
  POST /payments/holds/{hold_id}/checkout
  GET  /reports/ops
  GET  /reports/revenue

RBAC:
  Todos os endpoints usam require_property_role("viewer"|"staff")
  não aceitar `property_id` no body
  property_id via `?property_id=` validado por RBAC/JWT
  Contrato uniforme: sem atalhos single-tenant```


### 12.3 Regras de Alteração de Reserva

```
Ao mover reserva para novo período:
  1. Verificar disponibilidade no novo período
  2. Recalcular valor total (modelo PAX)
  3. Mostrar diferença de valor
  4. NÃO cobrar automaticamente - informar para cobrar no check-in
  5. NÃO notificar hóspede automaticamente - staff comunica manualmente
  6. Atualizar reservation com adjustment_amount e adjustment_reason
```

### 12.4 Admin Frontend (hotelly-admin)

```
Stack:
  - Next.js 14 (App Router)
  - Clerk para auth
  - Tailwind CSS (inline styles no momento)

Repo: https://github.com/marcioluisms/hotelly-admin

Páginas implementadas:
  /select-property              - Seleção de property
  /p/[propertyId]/dashboard     - Dashboard com métricas
  /p/[propertyId]/reservations  - Lista de reservas
  /p/[propertyId]/reservations/[id] - Detalhe + AssignRoomDialog
  /p/[propertyId]/rates         - Calendário de preços PAX ← NOVO
  /p/[propertyId]/frontdesk/occupancy - Grid de ocupação

Proxies API (Next.js API Routes):
  /api/p/[propertyId]/reservations
  /api/p/[propertyId]/reservations/[id]/assign-room
  /api/p/[propertyId]/rooms
  /api/p/[propertyId]/occupancy
  /api/p/[propertyId]/rates ← NOVO (GET + PUT)

Libs:
  src/lib/api.ts   - wrapper apiGet para chamadas server-side
  src/lib/rates.ts - getRates, putRates, types ← NOVO
```

### 12.4 Fluxo de Continuous Deployment (CI/CD)

Ambos os repositórios usam Cloud Build com triggers automáticos no push para a branch de produção.
Os `cloudbuild.yaml` são **environment-agnostic** — staging e produção compartilham o mesmo arquivo,
diferenciados apenas por substitution variables configuradas no trigger do GCP Console.

#### hotelly-v2 (Backend)

- **Branch de produção:** `master`
- **Arquivo:** `cloudbuild.yaml` (raiz do repositório)
- **Pipeline:** build → push → migrate (Cloud SQL Auth Proxy) → deploy-public + deploy-worker (paralelo)

Substitution variables:

| Variável | Staging (default) | Produção |
|---|---|---|
| `_IMAGE_TAG` | `staging` | `latest` |
| `_SERVICE_NAME_PUBLIC` | `hotelly-public-staging` | `hotelly-public` |
| `_SERVICE_NAME_WORKER` | `hotelly-worker-staging` | `hotelly-worker` |
| `_CLOUD_SQL_INSTANCE` | *(vazio)* | `hotelly--ia:us-central1:hotelly-sql` |
| `_DB_SECRET_NAME` | `hotelly-staging-database-url` | `hotelly-database-url` |

Secrets (Secret Manager, referenciados via `availableSecrets`):
- `DATABASE_URL` → resolvido dinamicamente pelo `_DB_SECRET_NAME`

#### hotelly-admin (Frontend)

- **Branch de produção:** `main`
- **Arquivo:** `cloudbuild.yaml` (raiz do repositório)
- **Pipeline:** build (com build-args Next.js) → push → deploy

Substitution variables:

| Variável | Staging (default) | Produção |
|---|---|---|
| `_IMAGE_TAG` | `staging` | `latest` |
| `_SERVICE_NAME` | `hotelly-admin-staging` | `hotelly-admin` |
| `_API_URL` | `https://hotelly-public-staging-dzsg3axcqq-uc.a.run.app` | `https://app.hotelly.ia.br` |
| `_ENABLE_API` | `true` | `true` |
| `_APP_ENV` | `staging` | `production` |
| `_CLERK_PUBLISHABLE_KEY` | `pk_live_...` | *(confirmar se produção usa chave diferente)* |
| `_CLERK_SIGN_IN_URL` | `/sign-in` | `/sign-in` |
| `_CLERK_SIGN_UP_URL` | `/sign-up` | `/sign-up` |
| `_CLERK_SIGN_IN_FALLBACK` | `/select-property` | `/select-property` |
| `_CLERK_SIGN_UP_FALLBACK` | `/select-property` | `/select-property` |
| `_BUILD_DATE` | *(vazio — auto)* | *(vazio — auto)* |

#### Configuração do Trigger no GCP Console

Para cada trigger:
- **Event:** Push to a branch
- **Branch regex:** `^master$` (hotelly-v2) ou `^main$` (hotelly-admin)
- **Configuration:** Cloud Build configuration file → `/cloudbuild.yaml`
- **Service Account:** Default Cloud Build SA (`<PROJECT_NUMBER>@cloudbuild.gserviceaccount.com`)
- **Substitution variables:** preencher conforme tabelas acima (override dos defaults)

#### Notas importantes

1. **Variáveis `NEXT_PUBLIC_*`** são injetadas no bundle do Next.js em **build time** via `--build-arg`.
   Configurá-las como env vars do Cloud Run não tem efeito no client-side.
2. **Migrations** rodam automaticamente no pipeline do hotelly-v2 via Cloud SQL Auth Proxy
   antes do deploy, garantindo que o schema está atualizado antes de servir tráfego.
3. **O mesmo `cloudbuild.yaml`** é usado para staging e produção — nunca duplique o arquivo.
   Toda diferença entre ambientes deve ser resolvida via substitution variables.

---

## SEÇÃO 13: QUALITY GATES

- [QUALITY GATES] Gates normativos (quando afetar transação crítica/dinheiro/inventário):
  - G0: compileall + build docker + /health.
  - G1: migrate up em DB vazio + migrate up idempotente + constraints críticas presentes.
  - G3–G5 obrigatórios para mudanças em transações críticas.
- [INCIDENT SEV0] Stop-ship inclui: overbooking/inventário negativo, reserva duplicada, pagamento confirmado sem trilha de reprocesso, vazamento de PII em logs, endpoint interno exposto publicamente.

---

## SEÇÃO 14: RETENÇÃO E LIMPEZA

- [RETENTION] Limpeza periódica (idempotente e segura); recomendação: Cloud Scheduler + Cloud Run Job.
  - Frequência recomendada diária para `processed_events`, `outbox_events`, `idempotency_keys`.
  - Nunca logar payload dos registros limpos; só contagens.

---

## SEÇÃO 15: LIMITES DO PILOTO

- [PILOT LIMITS] Piloto: até 10 pousadas, sem HA, usuários cientes de falhas; foco em observabilidade e aprendizado.
- [SUPPORT CAPACITY] Estimativa de suporte solo: 15-20 clientes confortável, 25 no limite.

---

## SEÇÃO 16: REFERÊNCIA RÁPIDA

### Comandos de Desenvolvimento

```bash
# Desenvolvimento local
./scripts/dev.sh              # subir app com hot-reload
./scripts/verify.sh           # rodar checks
uv run pytest -q              # testes
python -m compileall -q src   # verificar sintaxe

# GCP - Build
gcloud builds submit --project hotelly--ia \
  --tag us-central1-docker.pkg.dev/hotelly--ia/hotelly/hotelly:latest .

# GCP - Redeploy
gcloud run services update hotelly-public-staging --project hotelly--ia --region us-central1 \
  --update-env-vars DEPLOY_SHA=$(date +%s)

# GCP - Logs
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=hotelly-public-staging AND severity>=ERROR" \
  --project hotelly--ia --limit=10 --freshness=5m

# Evolution API
curl -X POST "https://edge.roda.ia.br/chat/findMessages/pousada-ia-v2" \
  -H "apikey: <EVOLUTION_API_KEY>" \
  -d '{"limit": 3}'

# Cloud SQL Proxy (para migrations manuais)
cloud-sql-proxy hotelly--ia:us-central1:hotelly-sql --port 15432 &
DATABASE_URL="postgresql://hotelly_staging_app:<SENHA>@127.0.0.1:15432/hotelly_staging" uv run alembic upgrade head
```

### Secrets (Secret Manager)

```
# Produção
hotelly-database-url
hotelly-worker-url
contact-hash-secret
contact-refs-key
oidc-issuer
oidc-audience
oidc-jwks-url
stripe-secret-key
stripe-webhook-secret

# Staging
hotelly-staging-database-url
hotelly-staging-db-password
hotelly-worker-staging-url
oidc-issuer-dev
oidc-jwks-url-dev
```

### URLs de Produção

```
Public API:    https://app.hotelly.ia.br
Clerk:         https://clerk.hotelly.ia.br
Evolution:     https://edge.roda.ia.br
```

### URLs de Staging (operacional)

```
Public API (staging):  https://hotelly-public-staging-678865413529.us-central1.run.app
Worker (staging):      use sempre o URL canônico do Cloud Run (status.url, domínio *.a.run.app)
Property ID (staging): pousada-staging
Rooms (staging):       101 (Standard), 102 (Standard), 201 (Suíte)
Room Types (staging):  rt_standard, rt_suite
```


---

**FIM DO SPEC PACK v2.2**

---

## 2. Runbook de Ambientes

# runbook-de-ambientes-hotelly-v4-corrigido
(Baseado em `runbook-de-ambientes-hotelly-v4`, com adições mínimas para cobrir lacunas do runbook anterior.)

Atualizado em **05/02/2026** com base na execução da Fase 13 (Calendário de Preços PAX).

---

## 0) Regras de ouro

1. **Staging precisa de worker próprio** (não reaproveitar `hotelly-worker`).
2. **`TASKS_BACKEND=inline` é proibido em staging/prod**.
3. **Worker em Cloud Run**: porta **8000** + Cloud SQL anexado.
4. **OIDC audience**: usar sempre `*.a.run.app` (canônico), nunca `*.run.app` (alias).
5. **`/outbox` exige `property_id` na query**.
6. **CI usa `uv run alembic upgrade head`** para migrations.
7. **Deploy staging**: rebuild imagem → migrate → redeploy (nesta ordem, sempre).
8. **Artifact Registry**: repositório é `hotelly`, NÃO `hotelly-repo`.
9. **Cloud SQL**: única instância `hotelly-sql`, databases separados (`hotelly` prod, `hotelly_staging` staging).

---

## 1) Ambientes e serviços (Cloud Run)

### Serviços
| Ambiente | API | Worker |
|---|---|---|
| Prod/principal | `hotelly-public` | `hotelly-worker` |
| Staging | `hotelly-public-staging` | `hotelly-worker-staging` |

### Jobs (staging)
- `hotelly-migrate-staging` — ⚠️ DATABASE_URL mal formatado, usar cloud-sql-proxy
- `hotelly-seed-staging`

### Databases (mesma instância `hotelly-sql`)
| Database | Usuário | Ambiente |
|----------|---------|----------|
| `hotelly` | `hotelly_app` | Produção |
| `hotelly_staging` | `hotelly_staging_app` | Staging |

### URLs canônicas
```bash
gcloud run services describe hotelly-worker-staging --project hotelly--ia --region us-central1 --format="value(status.url)"
```

### Artifact Registry
```bash
# Repositório correto
us-central1-docker.pkg.dev/hotelly--ia/hotelly/hotelly:latest

# Verificar repositórios existentes
gcloud artifacts repositories list --project hotelly--ia --location us-central1
```

---

## 2) Configuração obrigatória por serviço

### 2.1 `hotelly-public-staging` (API staging)
Obrigatório:
- `DATABASE_URL` → secret `hotelly-staging-database-url`
- `OIDC_ISSUER` → secret `oidc-issuer-dev`
- `OIDC_JWKS_URL` → secret `oidc-jwks-url-dev`
- `OIDC_AUDIENCE=hotelly-api`

Cloud Tasks:
- `TASKS_BACKEND=cloud_tasks`
- `GCP_TASKS_QUEUE=hotelly-default`
- `WORKER_BASE_URL=<status.url do hotelly-worker-staging>`
- `TASKS_OIDC_SERVICE_ACCOUNT=hotelly-worker@hotelly--ia.iam.gserviceaccount.com`
- `GCP_PROJECT_ID=hotelly--ia`

Verificação:
```bash
gcloud run services describe hotelly-public-staging --project hotelly--ia --region us-central1   --format="value(spec.template.spec.containers[0].env)" | rg "TASKS_BACKEND|WORKER_BASE_URL"
```

### 2.2 `hotelly-worker-staging` (worker staging)
Obrigatório:
- `APP_ROLE=worker`
- Porta: **8000**
- Cloud SQL: `hotelly--ia:us-central1:hotelly-sql`
- `DATABASE_URL` → secret `hotelly-staging-database-url`
- `DB_PASSWORD` → secret `hotelly-staging-db-password`
- `OIDC_ISSUER` → secret `oidc-issuer-dev`
- `OIDC_JWKS_URL` → secret `oidc-jwks-url-dev`
- `OIDC_AUDIENCE` → secret `oidc-audience`
- `TASKS_OIDC_AUDIENCE` → secret `hotelly-worker-staging-url` (= `status.url`)

Verificação:
```bash
gcloud run services describe hotelly-worker-staging --project hotelly--ia --region us-central1   --format="yaml(status.url,spec.template.spec.containers[0].ports,spec.template.spec.containers[0].env)"
```

### 2.3 Prod (`hotelly-public` e `hotelly-worker`)
- Secrets próprios (`hotelly-database-url`, etc.)
- **Não** apontar staging para esses serviços.

---

## 3) Deploy / promoção

**ADICIONADO PARA COBERTURA (origem A):** **Realidade atual** — `hotelly-public-staging` pode estar apontando para **imagem `:latest`**. Nessa configuração, tag `v*` **não garante** promoção se o pipeline/serviço não trocar digest/imagem de fato (trocar tag no Git não muda a revisão do Cloud Run sozinho).

### Procedimento completo (rebuild + migrate + redeploy)
```bash
# 1) Build
cd ~/projects/hotelly-v2
gcloud builds submit --project hotelly--ia   --tag us-central1-docker.pkg.dev/hotelly--ia/hotelly/hotelly:latest .

# 2) Migrate (VIA CLOUD-SQL-PROXY - job staging está quebrado)
# Terminal 1: iniciar proxy
cloud-sql-proxy hotelly--ia:us-central1:hotelly-sql --port 15432 &

# Terminal 2: rodar migration
# Para STAGING (database hotelly_staging):
DATABASE_URL="postgresql://hotelly_staging_app:<SENHA>@127.0.0.1:15432/hotelly_staging" uv run alembic upgrade head

# Para PROD (database hotelly):
DATABASE_URL="postgresql://hotelly_app:<SENHA>@127.0.0.1:15432/hotelly" uv run alembic upgrade head

# Matar proxy após uso
kill %1

# 3) Redeploy (forçar nova revisão)
gcloud run services update hotelly-public-staging --project hotelly--ia --region us-central1   --update-env-vars DEPLOY_SHA=$(date +%s)

gcloud run services update hotelly-worker-staging --project hotelly--ia --region us-central1   --update-env-vars DEPLOY_SHA=$(date +%s)
```

### Obter senhas do banco
```bash
# Senha do hotelly_app (prod)
gcloud secrets versions access latest --secret=hotelly-db-password --project=hotelly--ia

# Senha do hotelly_staging_app (staging) - está no secret staging-database-url
gcloud secrets versions access latest --secret=hotelly-staging-database-url --project=hotelly--ia
```

### Verificar imagem atual do worker
```bash
gcloud run services describe hotelly-worker-staging --project hotelly--ia --region us-central1   --format="value(spec.template.spec.containers[0].image)"
```

---

## 4) Cloud Tasks: validação operacional

### Confirmar task criada
```bash
gcloud tasks list --project hotelly--ia --location us-central1 --queue hotelly-default --limit 20
```

**ADICIONADO PARA COBERTURA (origem A):** Rodar task manualmente (debug) — útil para isolar se a falha é do handler/OIDC/worker vs criação/enfileiramento.
```bash
gcloud tasks run <TASK_NAME> --project hotelly--ia --location us-central1 --queue hotelly-default
```

### Logs do worker-staging
```bash
gcloud run services logs read hotelly-worker-staging --project hotelly--ia --region us-central1 --limit 20
```

**ADICIONADO PARA COBERTURA (origem A):** Logs de requests do worker-staging (HTTP) — filtra o log de requests do Cloud Run para ver status/latência por chamada.
```bash
gcloud logging read   'resource.type="cloud_run_revision" AND resource.labels.service_name="hotelly-worker-staging" AND logName:"run.googleapis.com%2Frequests"'   --project hotelly--ia --limit 20 --freshness 30m --format json
```

### Logs de erro (últimos 5 minutos)
```bash
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=hotelly-public-staging AND severity>=ERROR"   --project hotelly--ia --limit=10 --freshness=5m
```

---

## 5) Endpoints relevantes (Admin)

### Reservas
- Lista: `GET /reservations` (RBAC, filtros `from`/`to`/`status`)
- Detalhe: `GET /reservations/{id}` (RBAC, retorna `room_id` e `room_type_id`)
- Ação: `POST /reservations/{id}/actions/resend-payment-link` (staff, 202)
- Ação: `POST /reservations/{id}/actions/assign-room` (staff, 202, body: `{"room_id": "..."}`)

### Ocupação
- `GET /occupancy` (RBAC, query: `start_date`, `end_date` exclusivo)

### Rooms
- `GET /rooms` (RBAC, retorna id/room_type_id/name/is_active)

### Rates (PAX Pricing)
- `GET /rates` (RBAC viewer)
  - Query: `start_date`, `end_date`, `room_type_id` (opcional)
  - Limite: max 366 dias
  - Retorna: lista de rates com campos PAX
- `PUT /rates` (RBAC staff)
  - Body: `{"rates": [...]}`
  - Limite: max 366 rates
  - Bulk upsert idempotente

### Outbox
- `GET /outbox?property_id=<id>&aggregate_type=reservation&aggregate_id=<id>&limit=50`

### Worker tasks
- `POST /tasks/reservations/resend-payment-link` (OIDC)
- `POST /tasks/reservations/assign-room` (OIDC)

---

## 6) DB Staging (acesso direto)

### Via Cloud SQL Proxy (RECOMENDADO)
```bash
# Terminal 1: iniciar proxy
cloud-sql-proxy hotelly--ia:us-central1:hotelly-sql --port 15432 &

# Terminal 2: conectar ao staging
PGPASSWORD='<senha_hotelly_staging_app>' psql -h 127.0.0.1 -p 15432 -U hotelly_staging_app -d hotelly_staging

# Terminal 2: conectar ao prod
PGPASSWORD='<senha_hotelly_app>' psql -h 127.0.0.1 -p 15432 -U hotelly_app -d hotelly
```

### Verificações úteis
```sql
-- Migration aplicada
SELECT version_num FROM alembic_version;

-- Rooms
SELECT id, room_type_id, name, is_active FROM rooms WHERE property_id = 'pousada-staging';

-- Reservas com quarto
SELECT id, room_id, room_type_id, status FROM reservations WHERE property_id = 'pousada-staging';

-- Room type rates (PAX pricing)
SELECT room_type_id, date, price_1pax_cents, price_2pax_cents
FROM room_type_rates
WHERE property_id = 'pousada-staging'
ORDER BY date LIMIT 10;

-- Permissões do usuário
SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'public';
```

### Dar permissões a um usuário (como owner das tabelas)
```sql
-- Conectar como hotelly_staging_app (owner das tabelas no staging)
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO hotelly_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO hotelly_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO hotelly_app;
```

> Observação: ajuste o usuário alvo (`hotelly_app` vs `hotelly_staging_app`) conforme o objetivo; idealmente, cada DB só recebe grants do usuário do próprio ambiente.

---

## 7) Secrets Management

### Listar secrets
```bash
gcloud secrets list --project hotelly--ia
```

### Atualizar secret (ex: após trocar senha no Cloud SQL)
```bash
# Atenção: usar aspas simples para evitar expansão de caracteres especiais
echo -n 'dbname=hotelly_staging user=hotelly_staging_app password=<NOVA_SENHA> host=/cloudsql/hotelly--ia:us-central1:hotelly-sql'   | gcloud secrets versions add hotelly-staging-database-url --data-file=- --project hotelly--ia
```

### Ver valor atual
```bash
gcloud secrets versions access latest --secret=hotelly-staging-database-url --project=hotelly--ia
```

---

## 8) Troubleshooting rápido

| Problema | Causa | Solução |
|----------|-------|---------|
| 202 mas nada acontece | `TASKS_BACKEND=inline` | Setar `cloud_tasks` |
| Cloud Tasks 401 | OIDC audience errado | Atualizar secret com `status.url` canônico |
| Worker 500 `/cloudsql/...` | Cloud SQL não anexado | `--add-cloudsql-instances ...` |
| Worker `UndefinedColumn` | **Imagem desatualizada** | Rebuild + redeploy |
| AlreadyExists 409 | Dedupe por task_id | Sucesso idempotente (já tratado) |
| assign-room 422 | reservation sem room_type_id | COALESCE preenche; seed antigo |
| Teste timezone | Python vs Postgres date | Usar `CURRENT_DATE` do banco |
| password authentication failed | Senha alterada no Cloud SQL | Atualizar secret correspondente |
| Repository "hotelly-repo" not found | Nome errado | Usar `hotelly` (sem `-repo`) |
| permission denied for table | Usuário sem GRANT | Conectar como owner e dar GRANT |
| Job migrate-staging falha | DATABASE_URL mal formatado | Usar cloud-sql-proxy manualmente |

---

## 9) Auditoria de serviços obsoletos

```bash
gcloud logging read   'resource.type="cloud_run_revision" AND resource.labels.service_name=("hotelly-public" OR "hotelly-public-staging" OR "hotelly-worker" OR "hotelly-worker-staging")'   --project hotelly--ia --freshness 7d --limit 500   --format "value(resource.labels.service_name,httpRequest.status,timestamp)"
```

**ADICIONADO PARA COBERTURA (origem A):** critério operacional — serviço com **0 requests** no período (ex.: 7 dias) é candidato a obsoleto; antes de remover, confirmar dependências e plano de rollback.

---

## 10) Admin (hotelly-admin)

### Desenvolvimento local
```bash
cd ~/projects/hotelly-admin
npm run dev
# Acessa http://localhost:3000
```

### Variáveis de ambiente (.env.local)
```env
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_test_...
CLERK_SECRET_KEY=sk_test_...
NEXT_PUBLIC_HOTELLY_API_BASE_URL=https://hotelly-public-staging-678865413529.us-central1.run.app
NEXT_PUBLIC_ENABLE_API=true
```

### Rotas de proxy (Next.js API routes)
| Rota | Descrição |
|------|-----------|
| `/api/p/[propertyId]/reservations` | Lista reservas |
| `/api/p/[propertyId]/reservations/[id]/assign-room` | Atribuir quarto |
| `/api/p/[propertyId]/rooms` | Lista quartos |
| `/api/p/[propertyId]/rates` | GET/PUT tarifas PAX |
| `/api/p/[propertyId]/occupancy` | Grid de ocupação |

### Páginas principais
| Página | Descrição |
|--------|-----------|
| `/p/[propertyId]/dashboard` | Dashboard com métricas |
| `/p/[propertyId]/reservations` | Lista de reservas |
| `/p/[propertyId]/reservations/[id]` | Detalhe da reserva |
| `/p/[propertyId]/rates` | Calendário de preços PAX |
| `/p/[propertyId]/frontdesk/occupancy` | Grid de ocupação |

---

## 3. Roadmap

# Roadmap Hotelly V2 — Atualizado 05/02/2026

## ✅ FASE 1: Infraestrutura Base — CONCLUÍDA
- GCP Project: `hotelly--ia`, região `us-central1`
- Cloud Run: `hotelly-public` + `hotelly-worker`
- Cloud SQL: Postgres, migrations até 010
- Cloud Tasks: fila `hotelly-default`
- Secrets no Secret Manager

## ✅ FASE 2: Autenticação — CONCLUÍDA
- Clerk Production com domínios próprios
- JWT template `hotelly-api` com `aud=hotelly-api`
- `/auth/whoami` funcionando

## ✅ FASE 3: Dashboard Backend (V2-S11 a V2-S21) — CONCLUÍDA
- Auth/RBAC implementado
- Endpoints validados: `/me`, `/properties`, `/frontdesk/summary`, `/reservations`, `/conversations`, `/payments`, `/reports/*`
- Worker recebendo tasks via OIDC
- Permissões IAM configuradas

## ✅ FASE 4: Cloud Tasks Backend — CONCLUÍDA
- `cloud_tasks_backend.py` implementado
- Dependência `google-cloud-tasks` adicionada

## ✅ FASE 5: Webhook WhatsApp E2E — CONCLUÍDA
- Evolution API v2: instância `pousada-ia-v2` funcionando
- Fluxo completo: WhatsApp → Evolution → Hotelly Public → Cloud Tasks → Worker → Outbox

## ✅ FASE 6: Staging Isolado — CONCLUÍDA (02/02/2026)
- DB staging isolado (`hotelly_staging` + usuário `hotelly_staging_app`)
- Cloud Run staging: `hotelly-public-staging` + `hotelly-worker-staging`
- **WORKER_BASE_URL (staging)** deve usar a URL canônica do Cloud Run (`status.url`, domínio `*.a.run.app`), não o alias `*.run.app`.
- Jobs: `hotelly-migrate-staging` + `hotelly-seed-staging`
- Cloud Tasks configurado corretamente (não mais `inline`)
- OIDC audience do worker-staging isolado
- Runbook de ambientes documentado

## ✅ FASE 7: Admin MVP — Navegação e Auth — CONCLUÍDA (02/02/2026)
- Next.js 14 (App Router) + Clerk + shadcn/ui
- Property selection com URL como fonte da verdade (`/p/[propertyId]/*`)
- Guard server-side validando acesso via `GET /properties`
- `/select-property` + redirect automático com localStorage (conveniência)
- Repo: https://github.com/marcioluisms/hotelly-admin

## ✅ FASE 8: Admin MVP — Dashboard e Reservas — CONCLUÍDA (02/02/2026)

### Dashboard
- Cards de métricas consumindo `/frontdesk/summary`
- Lista de erros recentes

### Reservas
- Lista `/p/[propertyId]/reservations` com filtros por **check-in** (`from`/`to`, formato YYYY-MM-DD) e `status`
- Detalhe `/p/[propertyId]/reservations/[reservationId]`
- Backend filtra `checkin >= from` e `checkin <= to` (params `from`/`to`), não `date_from/date_to`
- Status como select

### Ações
- "Reenviar link de pagamento" via proxy server-side (evita CORS)

### Debug/Outbox
- Página `/p/[propertyId]/debug/outbox`
- Seção "Eventos" no detalhe da reserva consumindo `GET /outbox`

### Backend (correções)
- `GET /outbox` implementado (PII-safe, sem payload)
- Cloud Tasks: `AlreadyExists (409)` tratado como sucesso (dedupe)
- Seed staging com dados de teste (hold + reservation)

## ✅ FASE 9: Ocupação, Quartos e Atribuição (S11-S13) — CONCLUÍDA (04/02/2026)

### S11: GET /occupancy (por room_type)
- Endpoint `GET /occupancy` com RBAC (viewer)
- Cálculo: inv_total (ari_days), held (hold_nights ativo), booked (reservations confirmadas), available = max(0, inv_total - booked - held)

### S12: Rooms (unidades físicas) + GET /rooms
- Migration 007: tabela `rooms` (property_id, id, room_type_id, name, is_active)
- Endpoint `GET /rooms` (viewer) com testes

### S13: Atribuição de quarto + room_type_id em reservations
- Migration 008: `reservations.room_id` com FK composta → `rooms(property_id, id)`
- Migration 009: `reservations.room_type_id` com FK composta → `room_types(property_id, id)`
- `POST /reservations/{id}/actions/assign-room` (staff) → Cloud Task → worker
- Worker valida room_type_id, atualiza room_id, grava outbox `room_assigned`
- `convert_hold` preenche `room_type_id` na criação da reserva

### Fixes pós-S13 (04/02/2026)
- **GET /reservations e GET /reservations/{id}**: adicionados `room_id` e `room_type_id` no SELECT e JSON (nullable)
- **assign-room COALESCE**: worker preenche `room_type_id` da reserva via `COALESCE(room_type_id, <room.room_type_id>)` quando NULL
- **outbox payload**: evento `room_assigned` agora inclui `reservation_id` no payload
- **Imagem staging**: rebuild + redeploy resolveu `UndefinedColumn` no worker (imagem desatualizada)

### Validação E2E Staging (04/02/2026)
- `GET /reservations` → 200, 3 reservas
- `GET /rooms` → 200, 3 rooms (101, 102, 201)
- `POST assign-room {"room_id":"101"}` → 202 enqueued
- Confirmado no psql: `room_id = '101'` preenchido
- Evento `room_assigned` com `reservation_id` no outbox

### Admin (hotelly-admin)
- Grid de ocupação: `/p/[propertyId]/frontdesk/occupancy`
  - Modo "Por categoria" (`?view=types`) e "Por quarto" (`?view=rooms`)
  - Navegação 14 dias, header com totais

### CI/CD
- GitHub Actions: migração via `uv run alembic upgrade head`
- `tests/helpers.py` para helpers compartilhados de auth
- 392 testes passando

### Decisão RBAC
- Endpoints property-scoped do dashboard: **`property_id` obrigatório via `?property_id=`**, validado por `require_property_role(min_role)`
- **Não aceitar `property_id` no body**
- Contrato uniforme, sem atalhos single-tenant

## ✅ FASE 10: Admin — Assign Room na UI + Grid Real — CONCLUÍDA (04/02/2026)
- Componente `AssignRoomDialog` na página de detalhe da reserva
- API route proxy `/api/p/[propertyId]/reservations/[reservationId]/assign-room`
- `router.refresh()` após POST assign-room
- Grid `?view=rooms` com dados reais de `reservations.room_id`

## ✅ FASE 11: Stripe Webhook — CONCLUÍDA (04/02/2026)
- Endpoint `POST /webhooks/stripe` no backend (hotelly-public)
- Secret `stripe-webhook-secret` criado no GCP Secret Manager
- Verificação de assinatura Stripe via `webhook_signing_secret`
- Webhook configurado no Stripe Dashboard apontando para `https://app.hotelly.ia.br/webhooks/stripe`
- Eventos escutados: `checkout.session.completed`, `payment_intent.succeeded`
- Fluxo validado E2E: Stripe → hotelly-public → Cloud Tasks → worker → outbox

## ✅ FASE 12: Calendário de Preços PAX — CONCLUÍDA (05/02/2026)

### Decisão Arquitetural
- **Modelo PAX completo desde o início** (não evoluir base_rate_cents)
- Tabela separada `room_type_rates` com pricing por ocupação
- Preços em centavos (padrão do projeto)

### Backend
- **Migration 010**: `room_type_rates`
  - PK composta: `(property_id, room_type_id, date)`
  - FK composta: `(property_id, room_type_id) → room_types(property_id, id)`
  - Preços adultos: `price_1pax_cents`, `price_2pax_cents`, `price_3pax_cents`, `price_4pax_cents`
  - Adicionais crianças: `price_1chd_cents`, `price_2chd_cents`, `price_3chd_cents` (nullable)
  - Restrições: `min_nights`, `max_nights`, `closed_checkin`, `closed_checkout`, `is_blocked`
  - Índices: `idx_room_type_rates_property_date`, `idx_room_type_rates_type_date`
- **GET /rates**: RBAC viewer, query params `property_id`, `start_date`, `end_date`, `room_type_id` (opcional)
  - Limite: max 366 dias de range
  - Retorna lista de rates com todos os campos PAX
- **PUT /rates**: RBAC staff, bulk upsert via `INSERT ... ON CONFLICT DO UPDATE`
  - Limite: max 366 rates por request
  - `property_id` obrigatório via `?property_id=` e validado por `require_property_role(...)`; não aceitar `property_id` no body
  - Idempotente: `updated_at` atualizado em cada upsert

### Admin (hotelly-admin)
- Proxy: `src/app/api/p/[propertyId]/rates/route.ts` (GET + PUT)
- Lib: `src/lib/rates.ts` (getRates, putRates, types)
- Página: `/p/[propertyId]/rates`
- Componente: `RatesGrid`
  - Grid 14 dias por room_type
  - 4 linhas por tipo (1-4 adultos)
  - Navegação: ±1 dia, ±7 dias, date picker
  - Destaque de fim de semana (sex/sab/dom em amarelo)
  - Tracking de células alteradas (dirty)
  - Conversão automática reais ↔ centavos
  - Botão "Salvar" com contador de alterações
- Navegação: "Tarifas" adicionado ao `PropertyHeader`

### Deploy Staging (05/02/2026)
- Build: `gcloud builds submit --tag .../hotelly:latest`
- Migration manual via cloud-sql-proxy (job staging com DATABASE_URL mal formatado)
- Secret `hotelly-staging-database-url` atualizado com nova senha do `hotelly_staging_app`
- Redeploy: `gcloud run services update ... --update-env-vars DEPLOY_SHA=$(date +%s)`

### Observações
- `quote.py` ainda usa `ari_days.base_rate_cents` — migrar para PAX é story separada
- Grid de rates mostra 0.00 quando não há dados (tabela vazia)

---

## Configurações Atuais

### Ambientes Cloud Run

| Ambiente | Serviços | DB |
|----------|----------|-----|
| **Prod** | `hotelly-public`, `hotelly-worker` | `hotelly` (prod) |
| **Staging** | `hotelly-public-staging`, `hotelly-worker-staging` | `hotelly_staging` |

### Jobs Staging
- `hotelly-migrate-staging` — migrations (⚠️ DATABASE_URL mal formatado, usar cloud-sql-proxy)
- `hotelly-seed-staging` — seed idempotente

### Databases (mesma instância `hotelly-sql`)
| Database | Usuário | Uso |
|----------|---------|-----|
| `hotelly` | `hotelly_app` | Produção |
| `hotelly_staging` | `hotelly_staging_app` | Staging |

### Evolution API (Prod)
| Item | Valor |
|------|-------|
| URL | https://edge.roda.ia.br/ |
| Instância | `pousada-ia-v2` |
| API Key | `FC4A3BCF9071-4357-852C-94ABA41DA0B5` |

### Clerk / Auth
| Item | Valor |
|------|-------|
| Issuer (Prod) | https://clerk.hotelly.ia.br |
| Issuer (Dev) | via secrets `*-dev` |
| Audience | hotelly-api |
| JWT Template | hotelly-api (lifetime 600s) |

### Admin
| Item | Valor |
|------|-------|
| Repo | https://github.com/marcioluisms/hotelly-admin |
| API Staging | https://hotelly-public-staging-678865413529.us-central1.run.app |
| Worker Staging (canônico) | https://hotelly-worker-staging-dzsg3axcqq-uc.a.run.app |
| Dev local | http://localhost:3000 (WSL + portproxy) |

### Artifact Registry
| Item | Valor |
|------|-------|
| Repositório | `hotelly` (não `hotelly-repo`) |
| Imagem | `us-central1-docker.pkg.dev/hotelly--ia/hotelly/hotelly:latest` |

---

### ✅ FASE 13: Deploy Admin em Produção
- Cloudflare Pages (LEGACY; tentativa anterior, abandonada)
- Deploy oficial: **GCP Cloud Run** (ver seção 4)
- Domínios: `dash.hotelly.ia.br` (staging) e `adm.hotelly.ia.br` (prod)
- CI/CD: Cloud Build + scripts no repo (`ops/cloudrun/*`)

### ✅ FASE 14: Migrar quote.py para PAX
- `quote.py` atualmente usa `ari_days.base_rate_cents`
- Migrar para usar `room_type_rates` com lógica PAX
- Calcular preço baseado em ocupação (adultos + crianças)
- Atualização 05/02/2026: **Status: CONCLUÍDA** (commit `9b9652c`); ver evidências na seção 6.

### ✅ FASE 15: Precificação de crianças por idade (robusta) — plano em 5 stories

**Decisões fechadas**
- Buckets por `property_id`: **sem sobreposição + cobertura completa 0..17**. Se policy incompleta → quote indisponível (`reason_code=child_policy_incomplete`).
- Quote indisponível deve registrar `reason_code` (log estruturado). Resposta pública continua `None` por enquanto.

**Stories (sequenciais)**
- Story 1 (DB + endpoints): buckets + `/child-policies` + compat no `/rates` (legado + novo).
- Story 2 (Quote engine): `adult_count` + `children_ages[]`, sem fallback, `QuoteUnavailable(reason_code, meta)`.
- Story 3 (WhatsApp multi-turn): `conversations.context` com entidades normalizadas + prompts/parse estrito de idades.
- Story 4 (Admin UI): configurar buckets e editar rates por bucket.
- Story 5 (Persistência): holds/reservations com `adult_count` + `children_ages`; remover `guest_count` e remover legado quando o Admin já estiver migrado.

---

WhatsApp (Evolution) → Webhook (public) → Cloud Tasks → Worker (staging): estado validado e requisitos

Em 07/02/2026, foi validado o fluxo end-to-end no staging: POST /webhooks/whatsapp/evolution (serviço hotelly-public-staging) enfileira uma task no Cloud Tasks (hotelly-default, us-central1) que é consumida pelo hotelly-worker-staging em POST /tasks/whatsapp/handle-message, com status 200 no worker.

URLs e serviços (staging)

Worker URL (staging): https://hotelly-worker-staging-dzsg3axcqq-uc.a.run.app

Public URL (staging): https://hotelly-public-staging-678865413529.us-central1.run.app

Configuração necessária no Cloud Run (hotelly-public-staging)

Para o webhook funcionar sem 500, o hotelly-public-staging precisa estar com:

APP_ROLE=public

TASKS_BACKEND=cloud_tasks

GCP_LOCATION=us-central1

GCP_TASKS_QUEUE=hotelly-default

WORKER_BASE_URL=<worker staging url>

TASKS_OIDC_SERVICE_ACCOUNT=hotelly-worker@hotelly--ia.iam.gserviceaccount.com

TASKS_OIDC_AUDIENCE=<worker staging url>

Além disso, dois secrets são obrigatórios (causavam 500 quando ausentes):

CONTACT_HASH_SECRET (secret: contact-hash-secret)
Erro típico: RuntimeError: CONTACT_HASH_SECRET not configured

CONTACT_REFS_KEY (secret: contact-refs-key)
Erro típico: RuntimeError: CONTACT_REFS_KEY not configured

IAM mínimo validado (Cloud Run + Tasks)

Para o Cloud Tasks conseguir chamar o worker:

O serviço hotelly-worker-staging deve permitir invocação por serviceAccount:hotelly-worker@hotelly--ia.iam.gserviceaccount.com com roles/run.invoker.

O enfileiramento usa a fila hotelly-default (Cloud Tasks) e deve existir permissão de enqueue no projeto (ex.: roles/cloudtasks.enqueuer) no contexto correto.

Contrato de segurança (ADR-006) observado no código

No webhook:

PII (ex.: remote_jid, text) só existe em memória durante o processamento.

contact_hash é gerado via HMAC (CONTACT_HASH_SECRET) e não é reversível.

remote_jid é armazenado criptografado em contact_refs usando CONTACT_REFS_KEY.

O payload da task não contém PII.

Causa raiz do 500 após secrets: property_id inválido (FK)

Após configurar os secrets, o 500 passou a ser violação de FK:

Erro típico: ForeignKeyViolation ... Key (property_id)=(pousada-demo) is not present in table "properties"

Conclusão: o header X-Property-Id precisa ser um properties.id existente no staging DB.

No staging DB, foi confirmado:

properties.id existente: pousada-staging

Logo, para testar o webhook no staging, o header correto é:

X-Property-Id: pousada-staging

Teste manual recomendado (staging)

Requisição mínima para validar o pipeline (exemplo de payload compatível com o adapter):

POST {PUBLIC_URL}/webhooks/whatsapp/evolution

Headers:

Content-Type: application/json

X-Property-Id: pousada-staging

Body com shape:

data.key.id

data.key.remoteJid

data.messageType

data.message.conversation (ou extendedTextMessage.text)

Interpretação:

Se o webhook retornar 200, por contrato do handler ele só devolve 2xx quando: gravou vault (contact_refs), gravou dedupe (processed_events) e enfileirou task.

Observação crítica: Admin não está alinhado com o staging DB

Mesmo após inserir user_property_roles para pousada-staging, o Admin acessado em adm.hotelly.ia.br continuou mostrando apenas pousada-demo e redirecionando para seleção de pousada quando forçada a URL de pousada-staging. Isso indica forte evidência de que o Admin nessa URL não está apontando para o mesmo ambiente/banco do staging usado pelo hotelly-public-staging. É necessário identificar o admin staging real e/ou verificar para qual backend ele aponta para alinhar ambientes.

Nota operacional: Cloud SQL Proxy

O Cloud SQL Proxy não é necessário para o Cloud Run operar; ele só é necessário para acesso local ao banco via psql. O secret hotelly-staging-database-url está em formato DSN key=value (libpq) e normalmente vem com host=/cloudsql/... (socket). Para uso local, deve-se substituir o host socket por TCP (host=127.0.0.1 port=<porta>). Foi usado 5433 porque 5432 estava ocupada.

---

### ✅ FASE 16: Automação de Mensagens (WhatsApp)
- Garantir env/secrets no `hotelly-worker-staging`: `EVOLUTION_BASE_URL`, `EVOLUTION_INSTANCE`, `EVOLUTION_API_KEY` (secret), `CONTACT_REFS_KEY` (secret), `DATABASE_URL` (e `EVOLUTION_SEND_PATH` se necessário).
- Corrigir `POST /tasks/whatsapp/send-response`: **5xx** em falha transitória (para retry) e **2xx** em falha permanente (sem retry).
- Implementar idempotência do outbound: guard durável por `outbox_event_id` (ex.: `outbox_deliveries`).
- Validar E2E em staging (Evolution): inbound → handle-message → outbox → send-response → mensagem chega no WhatsApp.

---
Atualizado em 08/02/2026

Segue um **registro “colável”** para o agente de documentos atualizar o `doc_unificado` (somente fatos + decisões + runbook + follow-up).

---

## Atualização — Story 16 (WhatsApp send-response) + Rollout/validação em staging

### Status

* **Story 16: DONE em staging** (persistência + idempotência comprovadas via runbook + DB).
* **Follow-up recomendado:** **Task 16.1** para prova determinística de **retry transiente via Cloud Tasks** (diag hook staging bem gateado).

---

## Implementação (TO-BE já entregue no código)

### Semântica do endpoint `POST /tasks/whatsapp/send-response`

* **Falha transiente** (`timeout/rede/5xx/429`) ⇒ **HTTP 500** (habilita retry do Cloud Tasks).

  * `outbox_deliveries` permanece `status='sending'` e atualiza `attempt_count` + `last_error` **sanitizado**.
* **Falha permanente** (`401/403`, `contact_ref_not_found`, template inválido, config/env/secret faltando) ⇒ **HTTP 200** com payload **`{ ok:false, terminal:true, ... }`** (evita retry infinito).

  * `outbox_deliveries.status='failed_permanent'`, `last_error` sanitizado.
* **Idempotência (no-op terminal)**: se já `sent` ⇒ **HTTP 200** `{ ok:true, already_sent:true }` e **não chama provider**.

### Guard durável / idempotência

* Nova tabela `outbox_deliveries` com **UNIQUE(property_id, outbox_event_id)**, status (`sending|sent|failed_permanent`), `attempt_count`, `last_error`, `sent_at`, timestamps.
* **Lease anti-concorrência**: `status='sending'` com `updated_at` recente ⇒ retorna **500 lease_held** (não envia); lease stale permite takeover.

### Segurança / PII

* **Não logar** `remote_jid`, texto, nem `contact_hash` completo.
* Logs/DB: apenas `property_id`, `outbox_event_id`, `correlation_id` e erro **sanitizado**.

---

## Migrações / Alembic (lição operacional)

* O projeto aplica migrations via **Alembic** (`migrations/versions/`).
* `migrations/sql/*.sql` é histórico/manual e **não roda** no `make migrate` por si só.
* Foi necessário PR separado criando **Alembic revision** para `outbox_deliveries`.

### Observação de higiene (staging DB drift)

* Staging apresentou **drift** (schema adiantado com `alembic_version` atrasado), causando falha em migration 013 (`guest_count` ausente).
* Foi usado `alembic stamp` para alinhar versão e prosseguir com `upgrade head`.
* Após isso, `outbox_deliveries` existe e o head ficou atualizado.

---

## Runbook oficial — testar `send-response` em staging (manual)

**Regra:** o endpoint exige **Google OIDC identity token (JWT)** com:

* **audience exatamente igual ao `TASKS_OIDC_AUDIENCE`**
* chamada feita no **mesmo host** do audience

Na prática, para teste manual foi necessário **impersonation** da service account:

* `hotelly-worker@hotelly--ia.iam.gserviceaccount.com`

(Esse runbook elimina o “curl qualquer” que gerava alternância entre 401 do Cloud Run e 401 do app.)

---

## Rollout/validação em staging (fatos finais)

* Imagem do backend atualizada em `:latest` (novo digest: `sha256:a0bc723b6b87567d6dd701afac315d8b91c4025834a6e7428e7be90fea5ab89b`).
* Cloud Run `hotelly-worker-staging` redeployado para revisão **`hotelly-worker-staging-00019-mf8`** (100% tráfego).

### Validação funcional (DB + endpoint)

* Chamada manual com token OIDC correto ⇒ resposta terminal:

  * `{"ok":false,"terminal":true,"error":"contact_ref_not_found"}`
* Confirmada persistência:

  * `outbox_deliveries` criado para `(property_id='pousada-staging', outbox_event_id=8)` com
    `status='failed_permanent'`, `attempt_count=1`, `last_error='contact_ref_not_found'`.
* Confirmada idempotência:

  * reexecução não gerou duplicata (count permaneceu 1).

### `already_sent` em staging (seed controlado)

* Não havia casos reais `sent` (por ausência de `contact_refs` válidos; TTL expirado).
* Para validar `already_sent`:

  * criado `outbox_event` id=9 (cópia do 8) + inserido `outbox_delivery` `status='sent'`.
  * chamada `send-response` para `outbox_event_id=9` retornou `{ "ok": true, "already_sent": true }` e **não incrementou `attempt_count`**.

---

## Follow-up: Task 16.1 — Prova determinística de retry transiente (Cloud Tasks)

**Motivação:** validar retry transiente sem mexer em provider/infra.

### Requisitos do diag hook (somente staging)

* Gate em camadas:

  * `ENV=staging`
  * `STAGING_DIAG_ENABLE=true`
  * header `x-diag-force-transient: 1`
  * `property_id` canônico == `pousada-staging`
* Hook ocorre **depois** do lease/attempt increment e **antes** do provider call.
* Efeito:

  * grava `last_error="forced_transient"` (PII-safe)
  * mantém `status='sending'`
  * retorna **HTTP 500** para forçar retry do Cloud Tasks
* Prova (DoD):

  * Cloud Tasks attempts subindo + `outbox_deliveries.attempt_count` subindo + status permanecendo `sending`.

* Implementado **diag hook de staging** no handler `POST /tasks/whatsapp/send-response`:

  * Gates: `APP_ENV=staging`, `STAGING_DIAG_ENABLE=true`, header `x-diag-force-transient: 1`, `property_id` canônico `pousada-staging`.
  * Quando ativo, força **HTTP 500** (`error=forced_transient`) sem chamar provider.
  * Atualiza `outbox_deliveries`: incrementa `attempt_count` e seta `last_error='forced_transient'`.
  * Ajuste para evitar bloqueio por `lease_held` durante retries: grava `updated_at` como “stale” (-600s), permitindo novas tentativas incrementarem.
  * Testes unitários adicionados/ajustados em `tests/test_send_response_delivery.py`.

* Deploy staging:

  * Build+push da imagem `latest` (digest `sha256:919eaff...`).
  * Cloud Run `hotelly-worker-staging` atualizado para revisão `00022-4tk` com código novo.
  * Env vars staging ajustadas (incluindo `APP_ROLE=worker`, `APP_ENV=staging`).
  * Hook habilitado temporariamente via `STAGING_DIAG_ENABLE=true`, depois desabilitado (`false`) em nova revisão `00023-k8j`.

* Prova de retry real (Cloud Tasks + DB):

  * Criada Cloud Task com OIDC e header do hook.
  * Observado retry (5xx) e, no DB, `outbox_deliveries` para `outbox_event_id=10` com `status='sending'`, `last_error='forced_transient'` e `attempt_count` subindo (ex.: 8).

* Limpeza:

  * Task de diagnóstico removida (`diag-forced-transient-10b`).
  * Hook desabilitado em staging.

### Runbook — Provar retry transiente (Cloud Tasks) em staging

**1) Criar um outbox_event novo (no psql)**

```sql
insert into outbox_events (property_id, event_type, aggregate_type, aggregate_id, occurred_at, correlation_id, payload, message_type)
select property_id, event_type, aggregate_type, aggregate_id, now(), 'diag-forced-transient', payload, message_type
from outbox_events
where id = 8
returning id;
```

> Guarde o `id` retornado (ex.: `OUTBOX_EVENT_ID=10`).

**2) Habilitar hook (Cloud Run)**

```bash
gcloud run services update hotelly-worker-staging \
  --region us-central1 \
  --update-env-vars STAGING_DIAG_ENABLE=true,APP_ENV=staging,APP_ROLE=worker
```

**3) Criar Cloud Task (gera 5xx e retry)**

```bash
gcloud tasks create-http-task "diag-forced-transient-$OUTBOX_EVENT_ID" \
  --queue="hotelly-default" \
  --location="us-central1" \
  --url="https://hotelly-worker-staging-dzsg3axcqq-uc.a.run.app/tasks/whatsapp/send-response" \
  --method=POST \
  --header="Content-Type:application/json" \
  --header="x-diag-force-transient:1" \
  --oidc-service-account-email="hotelly-worker@hotelly--ia.iam.gserviceaccount.com" \
  --oidc-token-audience="https://hotelly-worker-staging-dzsg3axcqq-uc.a.run.app" \
  --body-content='{"property_id":"pousada-staging","outbox_event_id":'"$OUTBOX_EVENT_ID"'}'
```

**4) Evidência de retry (Cloud Tasks)**

```bash
gcloud tasks describe "diag-forced-transient-$OUTBOX_EVENT_ID" \
  --queue="hotelly-default" --location="us-central1"
```

> Ver `dispatchCount` subindo e `lastAttempt.responseStatus` com HTTP 500.

**5) Evidência no DB (psql)**

```sql
select status, attempt_count, last_error, updated_at
from outbox_deliveries
where property_id='pousada-staging' and outbox_event_id=<OUTBOX_EVENT_ID>;
```

> Esperado: `status='sending'`, `last_error='forced_transient'`, `attempt_count` subindo.

**6) Desligar e limpar**

```bash
gcloud run services update hotelly-worker-staging \
  --region us-central1 \
  --update-env-vars STAGING_DIAG_ENABLE=false
```

```bash
gcloud tasks delete "diag-forced-transient-$OUTBOX_EVENT_ID" \
  --queue="hotelly-default" --location="us-central1" --quiet
```

Staging WhatsApp — Destravamento E2E (webhook → tasks → send-response → Evolution)
Status

Fluxo completo funcionando em staging (evidência: outbox_event_id=17 com outbox_deliveries.status='sent' em 2026-02-09T13:03:22Z).

Sintoma observado

Webhook Evolution retornava 200 (“received”), porém o envio não completava.

handle-message rodava, mas send-response não era enfileirado/rodado corretamente e/ou falhava antes de enviar.

Causas-raiz confirmadas

Worker sem config de Cloud Tasks backend
Ausência de TASKS_BACKEND, GCP_*, WORKER_BASE_URL, TASKS_OIDC_SERVICE_ACCOUNT impedia handle-message de enfileirar send-response.

IAM: falta de permissão iam.serviceAccounts.actAs
Erro PermissionDenied: iam.serviceAccounts.actAs ao criar task com OIDC usando TASKS_OIDC_SERVICE_ACCOUNT.

Chaves CONTACT_* divergentes entre public e worker (InvalidTag)
public e worker usavam secrets diferentes para CONTACT_REFS_KEY/CONTACT_HASH_SECRET, gerando contact_refs.remote_jid_enc com uma chave e tentando decriptar com outra ⇒ aesgcm.decrypt InvalidTag.

Config Evolution outbound ausente no worker
Falha permanente até configurar EVOLUTION_BASE_URL, EVOLUTION_INSTANCE, EVOLUTION_API_KEY.

Erro 400 do provider por número inexistente
Testes com número fake retornavam exists:false; com número real, envio ok.

Correções aplicadas
hotelly-worker-staging (worker)

Config de Cloud Tasks:

TASKS_BACKEND=cloud_tasks

GCP_PROJECT_ID=hotelly--ia

GCP_LOCATION=us-central1

GCP_TASKS_QUEUE=hotelly-default

WORKER_BASE_URL=https://hotelly-worker-staging-dzsg3axcqq-uc.a.run.app

TASKS_OIDC_SERVICE_ACCOUNT=hotelly-worker@hotelly--ia.iam.gserviceaccount.com

TASKS_OIDC_AUDIENCE mantido via secret

IAM:

concedido roles/iam.serviceAccountUser no hotelly-worker@... para permitir actAs (OIDC Cloud Tasks).

Evolution outbound:

EVOLUTION_BASE_URL

EVOLUTION_INSTANCE

EVOLUTION_API_KEY (secret)

hotelly-public-staging (public/webhooks)

Alinhamento de secrets:

CONTACT_HASH_SECRET passou a usar contact-hash-secret-staging

CONTACT_REFS_KEY passou a usar contact-refs-key-staging

IAM secrets:

SA do public (hotelly-public@...) recebeu roles/secretmanager.secretAccessor nos secrets *-staging.

Banco (staging)

Limpeza de contact_refs para property_id='pousada-staging' e channel='whatsapp' após alinhar CONTACT_REFS_KEY, para regenerar ciphertext compatível.

Checklist “staging operacional” (WhatsApp)

Public e Worker devem usar os mesmos secrets para:

CONTACT_REFS_KEY

CONTACT_HASH_SECRET

Worker deve ter Evolution outbound configurado:

EVOLUTION_BASE_URL, EVOLUTION_INSTANCE, EVOLUTION_API_KEY

Tasks pipeline deve estar explícito:

TASKS_BACKEND=cloud_tasks, GCP_LOCATION, GCP_TASKS_QUEUE, WORKER_BASE_URL, TASKS_OIDC_SERVICE_ACCOUNT, TASKS_OIDC_AUDIENCE

Manter APP_ROLE=worker no worker (senão /tasks/* vira 404).

IAM mínimo:

enqueuer: SA do serviço que enfileira precisa de roles/cloudtasks.enqueuer

invoker: SA OIDC precisa roles/run.invoker no worker

actAs: chamador precisa permissão iam.serviceAccounts.actAs no TASKS_OIDC_SERVICE_ACCOUNT

Evidências de sucesso

Cloud Tasks executou:

/tasks/whatsapp/handle-message (200)

/tasks/whatsapp/send-response (200)

DB:

outbox_deliveries.status='sent', attempt_count=1 para outbox_event_id=17.

## Runbook — Recuperar `InvalidTag` (CONTACT_REFS_KEY mudou / desalinhou)

**Quando usar**
Se o worker/public estiverem com `CONTACT_REFS_KEY` diferente do que criptografou `contact_refs.remote_jid_enc`, o decrypt vai falhar com `cryptography.exceptions.InvalidTag`.

**Pré-condição**
Primeiro alinhar `CONTACT_REFS_KEY` (e `CONTACT_HASH_SECRET`) entre **public** e **worker**. Só depois limpar.

### Opção A — Limpar somente a property/canal afetados (recomendado)

```sql
-- Apaga refs de contato do WhatsApp para uma property (staging).
-- Isso remove ciphertext antigo (incompatível) e força regravação no próximo inbound.
delete from contact_refs
where property_id = 'pousada-staging'
  and channel = 'whatsapp';
```

### Opção B — Limpar somente refs expiradas (menos agressivo)

```sql
-- Útil se você suspeita que só parte está “podre” e quer reduzir impacto.
delete from contact_refs
where property_id = 'pousada-staging'
  and channel = 'whatsapp'
  and expires_at <= now();
```

### Verificação (antes/depois)

```sql
select count(*) as contact_refs_count
from contact_refs
where property_id = 'pousada-staging'
  and channel = 'whatsapp';
```

**Impacto esperado**

* Após limpar, `send-response` pode retornar `contact_ref_not_found` até chegar **novo inbound** (que recria o `contact_ref` com a chave correta).
* No próximo inbound válido, o `store_contact_ref` vai repopular `contact_refs` e o decrypt volta a funcionar.

**Observação**

* A limpeza não expõe PII; só remove ciphertext e metadados de referência.


---

### ✅ FASE 17: Configurações
- Endpoint de políticas (criança, cancelamento)
- UI de configuração da property

### Atualização — 2026-02-09 — S17 Políticas (crianças + cancelamento)

**Fonte da verdade (DB)**

* **Crianças:** `property_child_age_buckets` (migration `011`). 3 buckets cobrindo **0..17** sem gap/overlap.
* **Cancelamento:** `property_cancellation_policy` (migration `fe5db8079aad`). 1 linha por `property_id`.

**Endpoints (dashboard)**

* **GET `/child-policies?property_id=...`** (RBAC: `viewer`)
  Retorna os 3 buckets. Se não existir configuração, retorna default **sem persistir**:

  * bucket 1: 0..3
  * bucket 2: 4..12
  * bucket 3: 13..17

* **PUT `/child-policies?property_id=...`** (RBAC: `staff`)
  Body: `{ "buckets": [{bucket,min_age,max_age} x3] }`
  Valida cobertura 0..17 sem gap/overlap e substitui (delete+insert) transacionalmente.

* **GET `/cancellation-policy?property_id=...`** (RBAC: `viewer`)
  Retorna a policy. Se não existir configuração, retorna default **sem persistir**:

  ```json
  {
    "policy_type": "flexible",
    "free_until_days_before_checkin": 7,
    "penalty_percent": 100,
    "notes": null
  }
  ```

* **PUT `/cancellation-policy?property_id=...`** (RBAC: `staff`)
  Body:

  ```json
  {
    "policy_type": "free|flexible|non_refundable",
    "free_until_days_before_checkin": 0,
    "penalty_percent": 0,
    "notes": "texto opcional"
  }
  ```

  Validações (400 em inválido):

  * `free`: `penalty_percent = 0`, `free_until_days_before_checkin` 0..365
  * `non_refundable`: `penalty_percent = 100` **e** `free_until_days_before_checkin = 0`
  * `flexible`: `penalty_percent` 1..100 **e** `free_until_days_before_checkin` 0..365
    Persistência: upsert (INSERT … ON CONFLICT(property_id) DO UPDATE).

**Tabela `property_cancellation_policy`**

* `property_id TEXT PK FK properties(id) ON DELETE CASCADE`
* `policy_type TEXT CHECK IN ('free','flexible','non_refundable')`
* `free_until_days_before_checkin SMALLINT CHECK 0..365`
* `penalty_percent SMALLINT CHECK 0..100`
* `notes TEXT NULL`
* `updated_at TIMESTAMPTZ DEFAULT now()`
* CHECK de consistência por tipo (free/non_refundable/flexible) conforme regras acima.

**Admin (`hotelly-admin`)**

* Nova página: `/p/[propertyId]/settings` com duas seções (Crianças + Cancelamento).
* Proxies:

  * `/api/p/[propertyId]/child-policies` → backend `/child-policies?property_id=...`
  * `/api/p/[propertyId]/cancellation-policy` → backend `/cancellation-policy?property_id=...`
* Nav: item “Configurações” no `PropertyHeader`.

---

## Próximo Passo

### ✅ FASE 18: Edição em Lote de Rates
- Bulk edit: selecionar múltiplas datas
- Copiar rates de um período para outro
- Aplicar ajuste percentual
---

Correção crítica no save do RatesGrid: payload de PUT /rates agora faz merge com o RateDay “base” vindo do GET (não reconstrói com defaults), evitando zerar min_nights/flags/buckets quando o usuário edita só preço.

Room types com nome via GET /occupancy (janela 1 dia) para montar {room_type_id,name} (não via /rooms).

Seleção multi-data no header (toggle + shift-range), com highlight em todas as tabelas e filtragem da seleção para datas visíveis.

Modal de bulk edit: set value (R$) por pax; ajuste percentual com regra “vazio permanece vazio” e toggle “criar quando vazio” (vazio→0).

Copiar período: limita a ≤366 dias, copia somente dias existentes do source e persiste com PUT; faz refresh após concluir.

Se você registrar também a nota de infra (fora do escopo da S18): CI do backend foi ajustado para alembic upgrade heads por “multiple heads” e isso vira follow-up de merge revision.

---

## Troubleshooting Rápido

| Problema | Causa | Solução |
|----------|-------|---------|
| API retorna 202 mas nada acontece | `TASKS_BACKEND=inline` | Setar `TASKS_BACKEND=cloud_tasks` |
| Cloud Tasks não retry no `send-response` | Handler retorna 200 mesmo em erro | Ajustar semântica HTTP (5xx em falha transitória) + guard durável (`outbox_deliveries`) — Fase 16 |
| `send-response` retorna `contact_ref_not_found` | `contact_refs` expirado ou `CONTACT_REFS_KEY` ausente/incorreta | Garantir `CONTACT_REFS_KEY` no worker e validar TTL/fluxo inbound→outbound |
| Cloud Tasks 401 no worker | OIDC audience errado | Ajustar `TASKS_OIDC_AUDIENCE` (usar `*.a.run.app`) |
| Worker 500 `/cloudsql/...` | Cloud SQL não anexado | `--add-cloudsql-instances ...` |
| Worker `UndefinedColumn` | Imagem Docker desatualizada | Rebuild + redeploy |
| CORS no Admin | Chamada direta ao backend | Usar proxy server-side no Next |
| assign-room 422 | reservation sem room_type_id | COALESCE preenche; corrigir seed |
| Teste timezone | `date.today()` vs `CURRENT_DATE` | Usar `CURRENT_DATE` do banco |
| password authentication failed | Senha do DB alterada mas secret não atualizado | Atualizar secret no Secret Manager |
| Repository not found (build) | Nome errado do Artifact Registry | Usar `hotelly` (não `hotelly-repo`) |
| Migration staging falha | Job com DATABASE_URL mal formatado | Usar cloud-sql-proxy manualmente |

---

## ADICIONADO PARA COBERTURA (origem v3): Itens que existiam no v3 e não estavam explicitamente cobertos no v4

### Detalhes operacionais / técnicos (pós-S13) que orientam implementação e debug
- **ADICIONADO PARA COBERTURA (origem v3):** Worker/tasks: existe fallback de autenticação local para tasks via header `X-Internal-Task-Secret` (uso excepcional para debug/ambientes controlados).
- **ADICIONADO PARA COBERTURA (origem v3):** JWKS cache: em falha de assinatura/validação, fazer **refetch** do JWKS antes de falhar definitivamente (evita incidentes durante rotação de chaves).
- **ADICIONADO PARA COBERTURA (origem v3):** Ocupação: quando `available < 0`, logar WARN **PII-safe** (sinaliza inconsistência de inventário/overbooking sem vazar dados).

### Admin: páginas de debug que existiam como referência
- **ADICIONADO PARA COBERTURA (origem v3):** Debug Outbox: `/p/[propertyId]/debug/outbox` (visualiza eventos do `GET /outbox`).
- **ADICIONADO PARA COBERTURA (origem v3):** Debug Ocupação: `/p/[propertyId]/debug/occupancy`.
- **ADICIONADO PARA COBERTURA (origem v3):** Grid de ocupação: `/p/[propertyId]/frontdesk/occupancy` com navegação de janela (Hoje / ±14 dias) e modos `?view=types` e `?view=rooms` (quando aplicável).

### Documentos relacionados (referências internas do projeto)
- **ADICIONADO PARA COBERTURA (origem v3):**
  - `hotelly-admin-mvp-prompt.md` — especificação de telas do Admin
  - `spec-pack-tecnico.md` — regras técnicas backend (v1 + v2 consolidado)
  - `hotelly-admin-pos-mvp.md` — roadmap pós-MVP (Extras, CRM, Pensões, etc.)
  - `runbook-ambientes.md` — configuração staging vs prod

### Troubleshooting: casos específicos removidos do v4
- **ADICIONADO PARA COBERTURA (origem v3):** **Tag não promove código (staging usa `:latest`)** → fazer build da imagem `:latest` + forçar redeploy (ex.: `DEPLOY_SHA=$(date +%s)`).
- **ADICIONADO PARA COBERTURA (origem v3):** **`/outbox` retorna 400 (missing `property_id`)** → contrato exige `property_id=<propertyId>` na query.

---

## 4. Relatório — Migração `hotelly-admin` (Next.js 16 SSR) de Cloudflare Pages para GCP Cloud Run

**Objetivo**

- Migrar o deploy do Admin (`hotelly-admin`, Next.js 16 SSR em Node.js) de Cloudflare Pages para **GCP Cloud Run**, com build reprodutível via **Docker** + **Cloud Build**, sem alterar lógica de negócio, rotas, Clerk flows ou UI.
- Padronizar deploy oficial via scripts no repo, com **Secret Manager** para `CLERK_SECRET_KEY`.
- Garantir funcionamento local via Docker e em Cloud Run.
- Node **20.9+**.

---

### 2) Next.js “standalone” para produção

**Ação**

- Alterado `next.config.ts`:
  - Adicionado `output: "standalone"`.

**Validação**

- `npm run build` executou com sucesso.
- Confirmado que gerou `.next/standalone`:
  - `ls -la .next/standalone` mostrou `server.js` e artefatos.

**Resultado**

- Build gera bundle “standalone” (adequado para container runtime no Cloud Run).

---

### 3) Containerização (Docker)

#### 3.1 Dockerfile (multi-stage)

Criado `Dockerfile` com:

- **builder** (Node 20-alpine):
  - `npm ci`
  - `npm run build`
  - suporte a build com Clerk:
    - `ARG NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`
    - `ENV NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=$NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`
- **runner** (Node 20-alpine):
  - `ENV NODE_ENV=production`
  - `ENV PORT=8080`
  - `EXPOSE 8080`
  - copia:
    - `/app/.next/standalone` → `/app`
    - `/app/.next/static` → `/app/.next/static`
    - `/app/public` → `/app/public`
  - start: `node server.js`

#### 3.2 .dockerignore

Criado/atualizado `.dockerignore`:

- `node_modules`, `.next`, `.git`, `.vercel`, `.env*`, `npm-debug.log*`, `.DS_Store`
- adicionais: `coverage`, `dist`, `*.log`, `*.tsbuildinfo`, `.github`, `.claude`, `.vscode`

#### 3.3 Problema encontrado e correção (Clerk)

**Erro no build Docker/Cloud Build**

- Falha no `next build` por Clerk:
  - `Missing publishableKey` / prerender `/_not-found`.
- Causa: Docker build não recebia env do `.env.local`.

**Correção**

- Passar `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` como build-arg no Dockerfile (builder).

---

### 4) Scripts operacionais (repo)

Criado diretório `ops/cloudrun/` com:

- `ops/cloudrun/deploy-staging.sh`
- `ops/cloudrun/deploy-prod.sh`
- `ops/cloudrun/env.example`
- `cloudbuild.yaml` (raiz)

Comportamento:

- Scripts criam Artifact Registry repo (idempotente), fazem build via Cloud Build (com build-arg Clerk publishable) e fazem deploy no Cloud Run.
- Secrets (ex.: `CLERK_SECRET_KEY`) via Secret Manager com `--set-secrets`.

---

### 5) Deploy e domínios

- **Staging service:** `hotelly-admin-staging`  
  - URL canônica: `https://hotelly-admin-staging-dzsg3axcqq-uc.a.run.app`
  - Domínio: `dash.hotelly.ia.br` (CNAME `ghs.googlehosted.com.`)
  - Secret: `clerk-secret-key-staging`
- **Prod service:** `hotelly-admin`  
  - URL canônica: `https://hotelly-admin-dzsg3axcqq-uc.a.run.app`
  - Domínio: `adm.hotelly.ia.br` (CNAME `ghs.googlehosted.com.`)
  - Secret: `clerk-secret-key`

---

### 6) Root técnico: `hotelly.ia.br` → `hotelly.com.br`

- Criado serviço `hotelly-redirect` (fora do repo) no Cloud Run para redirecionar 308 para `https://hotelly.com.br{path}`.
- Domain mapping de `hotelly.ia.br` para `hotelly-redirect`.
- DNS no Registro.br conforme exigido pelo mapeamento (A/AAAA Google).

---

### 7) Nota registrada (pendente de consolidar no relatório)

- Variáveis `NEXT_PUBLIC_*` (ex.: API/debug/base URL) precisam estar disponíveis no **build** (Next embute no bundle). Portanto, quando forem usadas no client, devem ser passadas via **Cloud Build (`cloudbuild.yaml` + build-args)**, não apenas no `gcloud run deploy` (runtime).

---

## 5. Anexos de Execução

### 5.1 Prompt de execução — Migrar `quote.py` para `room_type_rates` (PAX pricing)

(Referência histórica: tarefa já concluída; ver seção 6 para evidências e lacunas.)

**Objetivo**: migrar `quote.py` para buscar preços em `room_type_rates` por noite, com fallback para `ari_days.base_rate_cents`.

### 5.2 Nota registrada para próxima revisão do documento

- Documentar no relatório que variáveis `NEXT_PUBLIC_*` (API/debug/base URL) precisam estar no build e por isso foram passadas via `cloudbuild.yaml` + `--build-arg`, não só no `gcloud run deploy`.

---

## 6. Registro — Quote PAX (room_type_rates) concluído e lacunas para crianças

### 6.1 O que foi feito (evidências objetivas)

- `quote_minimum` agora calcula por noite usando `room_type_rates` (PAX) com fallback para `ari_days.base_rate_cents` quando não tem rate (ou quando a coluna do pax está `NULL`).
- DB access: entrou `fetch_room_type_rates_by_date()` em `src/hotelly/infra/db.py`, buscando por intervalo `[checkin, checkout)` e retornando `date -> preços`.
- Integração: `tasks_whatsapp.py` passou `guest_count` pra `quote_minimum`.
- Validação: limites implementados (`guest_count` 1..4, `child_count` 0..3 → `ValueError`).
- Testes: cenários PAX + fallback + `NULL` em child; `./scripts/verify.sh` verde (295 passed).
- Commit: `feat: quote uses room_type_rates pax pricing` no `master` (hash `9b9652c`).

### 6.2 O que falta (e por quê)

Hoje o domínio/API só captura `guest_count` (um inteiro). Não existe `adult_count`/`child_count` vindo da API/WhatsApp, então em produção você não consegue informar crianças para o quote. Resultado:

- Em produção, `guest_count` = PAX (adultos).
- `child_count` ficou opcional com default 0 e só é exercitado nos testes por enquanto (retrocompat ok).

### 6.3 Como os “motores bons” tratam crianças (diretriz)

Padrão de mercado: separar **ocupação** e **categorias/idades**, com regras por quarto/rate plan.

- Crianças com política baseada em **idade (faixas)** e regra de preço “free / percentual / fixo por criança”.  
  Referência: Booking.com “child policies / child rates” (developer docs).
- Em occupancy-based pricing, criança pode ser ocupante “normal” ou **sempre extra**, com fees por categoria de idade.  
  Referência: Expedia Group “age categories” e “Always Extra”.

### 6.4 Plano robusto para o Hotelly (mudança de contrato)

- Entrada do fluxo:
  - Novo payload: `adult_count` + `children_ages[]` (ou `children[]` com idade).
  - Retrocompat: se vier só `guest_count`, mapear para `adult_count=guest_count` e `children_ages=[]`.

- Modelo de dados:
  - Evoluir além de `price_1chd_cents..price_3chd_cents` (quantidade) para regras por **faixa de idade** (infant/child/teen), com tipo de cobrança:
    - grátis / percentual / fixo
    - limites (ex.: 1 criança grátis até 5 anos)
    - opcionais: berço/cama extra

- Cálculo:
  - Base adultos continua em `room_type_rates.price_{N}pax_cents`.
  - Crianças: somar por noite por faixa/limite.
  - Fallback preservado: sem regra/rate → `ari_days.base_rate_cents`.

### 6.5 Referências externas (para embasar a modelagem)

- Booking.com: child policies / child rates (faixas, grátis/percentual/fixo): https://developers.booking.com/demand/docs/accommodations/child-policies
- Booking.com (connectivity): children policies and pricing: https://developers.booking.com/connectivity/docs/setting-up-children-policies-and-pricing
- Expedia Group (best practices): occupancy + age categories e “Always Extra”:  
  https://developers.expediagroup.com/supply/lodging/docs/property_mgmt_apis/product_mgmt/reference/createunit_mutation  
  https://developers.expediagroup.com/supply/lodging/docs/property_mgmt_apis/product/getting_started/requirements-best-practices/

---

## 7. Plano — Precificação de crianças por idade (5 stories)

### 7.1 Decisões fechadas

- Buckets por `property_id`: **sem sobreposição + cobertura completa 0..17**. Se houver gaps → indisponível com `reason_code=child_policy_incomplete`.
- Quando **não existir** policy/buckets para a property:
  - `GET /child-policies` retorna **default sugerido** (0–3 / 4–12 / 13–17) **sem persistir**.
  - Em **staging/dev**, seeds são mandatórios; ausência é bug de ambiente (corrigir via seed).

### 7.1.1 reason_codes mínimos do quote (reservado)

Mesmo que o retorno público do quote não mude agora, padronizar (log/diagnóstico interno):

- Dados/política: `child_policy_missing`, `child_policy_incomplete`
- Tarifas: `rate_missing`, `pax_rate_missing`, `child_rate_missing`
- Ocupação: `occupancy_exceeded`
- Datas/ARI: `invalid_dates`, `no_ari_record`, `no_inventory`, `base_rate_missing` (apenas enquanto houver caminho legado)
- Genérico: `unexpected_error`

- Implementação prática: levantar `QuoteUnavailable(reason_code=..., meta=...)` internamente; no call-site, manter retorno `None` e logar `reason_code` + contexto **normalizado** (sem PII).

### 7.2 Story 1 — DB + endpoints (escopo fechado)

Decisão operacional (migração room_type_rates): Como o sistema ainda não está em produção, o padrão é Opção A (RENAME): renomear no banco price_{1..3}chd_cents para price_bucket{1..3}_chd_cents no mesmo deploy, mantendo retrocompatibilidade apenas na API /rates (aliases de leitura/escrita por tempo limitado) para não quebrar o Admin. Opção B (ADD colunas) fica removida deste plano e só pode voltar mediante mudança explícita de decisão no documento (ex.: descoberta de dependência inevitável fora da API).

- `property_child_age_buckets`:
  - `bucket` ∈ {1,2,3}
  - `min_age/max_age` 0..17, `min_age <= max_age`
  - Sem sobreposição garantida **no DB** via **EXCLUDE constraint** usando range (incluir extensão `btree_gist` na migration).
- `room_type_rates` (migração de `room_type_rates`, decisão explícita):
  - **Opção A (RENAME — somente após confirmação)**: *rename* dos campos legados para buckets (zero duplicação):
    - `price_1chd_cents` → `price_bucket1_chd_cents`
    - `price_2chd_cents` → `price_bucket2_chd_cents`
    - `price_3chd_cents` → `price_bucket3_chd_cents` (**atenção ao underscore**)
  - **Opção B (ADD — default, não quebra Admin)**: adicionar `price_bucket{1..3}_chd_cents` mantendo `price_{1..3}chd_cents` **temporariamente**.
- `/child-policies`:
  - `GET /child-policies` e `PUT /child-policies` (dashboard) exigem `?property_id=` e validam com `require_property_role(...)`.
  - **Não aceitar `property_id` em path/query alternativo/body** (somente `?property_id=` conforme padrão do repo).
  - `PUT` valida **cobertura completa 0..17** e **sem overlap**.
- `/rates` compat (legado ↔ bucket; sem inventar rota nova):
  - `GET`: retorna **novos + legados (alias temporário)**.
  - `PUT`: aceita **novos OU legados**; se vierem **ambos** e divergirem → **400**.
  - Persistência sempre **normalizada** nos buckets (ou nas novas colunas, se Opção B). Campos legados são **alias** para compat.
property_id via `?property_id=` validado por RBAC
#### Seeds (dev/staging)
- Onde: `src/hotelly/operations/seed_staging.py` (mecanismo do repo; ex.: job `hotelly-seed-staging`).
- Requisito: **idempotente** (upsert por PK) para permitir re-execução sem duplicar dados.
- Defaults fixos: **0–3 / 4–12 / 13–17**.

#### Testes mínimos
- Aceitar default; rejeitar overlap; rejeitar gap; rejeitar fora de 0..17; e compat do `/rates`.

### 7.3 Story 2 — Quote engine (sem fallback)

- Entrada: `adult_count` + `children_ages[]` (idade obrigatória).
- Sem fallback para `ari_days.base_rate_cents`: ausência de rate/policy → indisponível com `reason_code`.
- Validar ocupação com `room_types.max_adults/max_children/max_total` (adicionar leitura em DB access).
- Emitir `QuoteUnavailable(reason_code, meta)` e log estruturado.

### 7.4 Story 3 — WhatsApp multi-turn + context

- Persistir contexto em `conversations.context` **somente com campos normalizados** (`checkin`, `checkout`, `room_type_id`, `adult_count`, `children_ages`, etc.), sem texto cru.
- Parsing inicial estrito: suportar poucos formatos (“3 e 7”, “3,7”, “3 7”); se não parsear com confiança, pedir idades.
- Prompts novos: adultos, quantidade de crianças, idades.

### 7.5 Story 4 — Admin UI

- Tela/config de buckets por property.
- Edição de rates por bucket (mesmo que simples) para não virar feature morta.

### 7.6 Story 5 — Persistência e limpeza de legado

- Persistir `adult_count` + `children_ages` onde fizer sentido (holds/reservations/quote_options).
- Remover `guest_count` do domínio/DB.
- Remover colunas legadas `price_{1..3}chd_cents` e aliases do `/rates` quando o Admin já estiver migrado.


---


## (B) doc_unificado_hotelly_v10.md


# Hotelly — Documento Unificado (Spec + Runbook + Roadmap) (v8 — 2026-02-05)

> Documento vivo. Consolida decisões técnicas, estado atual (repos/infra/domínios) e o plano de execução para fechar pricing de crianças de forma robusta.
>
> Princípios não negociáveis: **zero overbooking**, **idempotência real**, **sem PII em logs**, **rastreabilidade**.

---

## 0. Estado atual (snapshot)

### Repositórios
- **Backend:** `hotelly-v2`
- **Admin/Dashboard:** `hotelly-admin`

### Serviços e URLs (Cloud Run / us-central1)
- **hotelly-public** (já existente): atende `app.hotelly.ia.br`
- **hotelly-admin** (prod): URL do serviço Cloud Run (gerada pelo Cloud Run) + domínio custom `adm.hotelly.ia.br`
- **hotelly-admin-staging** (staging): URL do serviço Cloud Run + domínio custom (usado durante setup) `dash.hotelly.ia.br`
- **hotelly-redirect** (infra utilitária): redireciona `hotelly.ia.br` (domínio raiz) para o destino comercial (ex.: landing em `hotelly.com.br`)

### Domínios e DNS (Cloud Run Domain Mapping)
- `app.hotelly.ia.br` → `hotelly-public` (CNAME para `ghs.googlehosted.com.` via Domain Mapping)
- `adm.hotelly.ia.br` → `hotelly-admin` (CNAME `adm` → `ghs.googlehosted.com.`)
- `dash.hotelly.ia.br` → `hotelly-admin-staging` (CNAME `dash` → `ghs.googlehosted.com.`)
- `hotelly.ia.br` (root/apex) → `hotelly-redirect` (A/AAAA conforme instruído pelo `gcloud run domain-mappings create`)

Observação:
- Para subdomínios, o Domain Mapping usa **CNAME** (ex.: `adm` / `dash`).
- Para domínio raiz/apex (`hotelly.ia.br`), normalmente o provedor DNS exige **A/AAAA** (não CNAME). Foi configurado com os IPs retornados pelo `gcloud`.

---

## 1. Spec técnico (base)

### 1.1 Stack
- Python + FastAPI (backend `hotelly-v2`)
- Postgres (migrations via Alembic/SQL do repo)
- Cloud Run (serviços)
- Cloud Tasks (tarefas assíncronas em staging/prod; em dev pode rodar local/inline conforme docs)
- Secret Manager (segredos, ex.: Clerk secret key)
- Admin: Next.js (Cloud Run)

### 1.2 Princípios operacionais
- **Idempotência sempre**: qualquer ação externa (Stripe/WhatsApp/Tasks) precisa de dedupe durável.
- **PII**: não logar payload bruto (WhatsApp/Stripe) nem texto do usuário.
- **Inventário/ARI é transacional**: sem “ajuste manual” sem trilha (outbox/log/commit).

---

## 2. Pricing (estado atual no backend)

### 2.1 Modelo atual (já implementado): PAX em `room_type_rates` com fallback em `ari_days`
A função de quote mínima (`src/hotelly/domain/quote.py: quote_minimum`) agora calcula por noite:

Para cada noite do intervalo `[checkin, checkout)`:
1) valida disponibilidade via `ari_days` (inv_total - inv_booked - inv_held >= 1) e currency `BRL`.
2) tenta rate PAX em `room_type_rates` para a data.
3) se existir rate PAX:
   - `adult_base = price_{N}pax_cents` (N=1..4)
   - `child_add = price_{M}chd_cents` (M=1..3), quando `child_count>0`
     - se coluna for `NULL`, trata como **0**
   - `nightly = adult_base + child_add`
4) se não existir rate PAX para a noite, faz fallback para `ari_days.base_rate_cents` (comportamento legado)

Restrições aplicadas:
- `guest_count` (adultos) suportado: **1..4** (fora disso → `ValueError`)
- `child_count` suportado: **0..3** (fora disso → `ValueError`)
- Preços sempre em **centavos (int)**.

### 2.2 Limitação atual (por design)
Hoje o fluxo WhatsApp usa apenas `guest_count` (total), sem separar adultos/crianças e sem idades.
Então:
- **dá para precificar adultos (PAX)**
- **crianças com idade (robusto) ainda não é possível** no fluxo atual
- existe suporte técnico no quote para `child_count`, mas **a coleta/validação** (idades/política) ainda não existe.

---

## 3. Admin (hotelly-admin) — deploy e configuração

### 3.1 Build/Deploy (Cloud Build → Artifact Registry → Cloud Run)
Arquivos adicionados no repo `hotelly-admin`:
- `Dockerfile` (multi-stage, Next standalone)
- `cloudbuild.yaml` (build + push com build args)
- `ops/cloudrun/deploy-staging.sh`
- `ops/cloudrun/deploy-prod.sh`
- `.dockerignore`

Decisão importante:
- variáveis `NEXT_PUBLIC_*` precisam estar **baked no build** do Next (build args → ENV no Dockerfile) para evitar inconsistências de runtime.

Variáveis públicas usadas:
- `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`
- `NEXT_PUBLIC_ENABLE_API`
- `NEXT_PUBLIC_ENABLE_DEBUG_TOKEN`
- `NEXT_PUBLIC_HOTELLY_API_BASE_URL`

Segredo (não público):
- `CLERK_SECRET_KEY` via Secret Manager (env var com `valueFrom.secretKeyRef` no Cloud Run).

### 3.2 Clerk (Admin)
- Build-time: `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` **precisa ser `pk_*`** (publishable). `sk_*` quebra build/prerender.
- Runtime: `CLERK_SECRET_KEY` (Secret Manager).

Permissão obrigatória:
- Service Account do revision precisa de `roles/secretmanager.secretAccessor` no secret.
  - no setup atual foi concedido para `678865413529-compute@developer.gserviceaccount.com`.

### 3.3 Domínio do Admin (produção)
Escolhido: `adm.hotelly.ia.br` → serviço `hotelly-admin`.

Status esperado do Domain Mapping:
- `Ready=True`
- `CertificateProvisioned=True`
- `DomainRoutable=True`

---

## 4. Domínio raiz `hotelly.ia.br` (infra)

Contexto:
- O endereço comercial (landing page) será `hotelly.com.br`.
- O domínio `hotelly.ia.br` foi mantido como domínio técnico (ambientes/infra).

Decisão operacional:
- `hotelly.ia.br` (root) deve **redirecionar** para o domínio comercial, para evitar “domínio morto” e reduzir confusão.

Implementação:
- Serviço Cloud Run `hotelly-redirect` (Node simples) fazendo redirect 301/302.
- Domain Mapping para `hotelly.ia.br` exige registros **A/AAAA** (não CNAME).
- Após propagação + provisionamento do certificado, `curl -I https://hotelly.ia.br` deve retornar redirect e não erro TLS.

---

## 5. Runbook (Operações) — correção de alinhamento repo x docs

### 5.1 Divergência detectada e corrigida
Havia a afirmação de que o FastAPI expunha apenas `/health` e não possuía rotas `/tasks/*`.
Isso **não reflete o estado atual do repo**.

Rotas presentes no backend (exemplos):
- `/tasks/whatsapp/*`
- `/tasks/stripe/*`
- `/tasks/holds/*`
- `/tasks/conversations/*`
- `/rates`
- `/payments/*`
- `/reservations/*`
- `/rbac/check`
- `/me`

Implicação:
- Itens do runbook que tratavam tasks como “TARGET” precisam ser convertidos para “ATUAL” (com os devidos nomes/serviços/filas do ambiente).

### 5.2 Procedimento de triagem (mantido)
Mantém os passos de triagem, mas com ajuste:
- além de “último deploy”, validar também **fila Cloud Tasks** e **erros 5xx por rota**.

### 5.3 Comandos úteis (Cloud Run / Tasks / SQL)
Mantido conforme docs, com a regra: sempre fixar `--region us-central1`.

---

## 6. Roadmap: “Child pricing robusto” em 5 stories (ordem fechada)

Motivação:
- O prompt original era grande demais (misturava: modelo de dados + contratos + WhatsApp multi-turn + persistência + admin).
- Execução em stories reduz quebra de staging/tests e cria checkpoints claros.

### Reason codes (mínimo) para diagnóstico de quote
Mesmo mantendo `None` no retorno público (por enquanto), internamente (log estruturado) o quote deve emitir um `reason_code` quando indisponível.

Enum mínima sugerida (pode evoluir, mas começar com isso):
- `invalid_dates`
- `currency_not_supported`
- `ari_missing`
- `inventory_unavailable`
- `rate_missing_adult_base`
- `rate_missing_child_price`
- `child_policy_missing`
- `child_policy_incomplete`
- `occupancy_exceeded`
- `unexpected_error`

---


---

## Backlog / Security P0 (fora do Story 1)

### WhatsApp Webhooks — Segurança (Meta + Evolution)

Comportamento atual:

- **Meta (`/webhooks/whatsapp/meta`)**
  - Fora de local: `META_APP_SECRET` é obrigatório. Se ausente, o endpoint **não processa** o payload (fail-closed) e responde **200 OK** para evitar retry.
  - Local dev (`TASKS_OIDC_AUDIENCE == "hotelly-tasks-local"`): pode haver bypass, mas deve logar **warning**.

- **Evolution (`/webhooks/whatsapp/evolution`)**
  - Exigir `EVOLUTION_WEBHOOK_SECRET` e header `X-Webhook-Secret` com match.
  - Fora de local: se ausente/errado → **401**.

### Tasks Auth / OIDC Audience (ajustes)

- Cloud Tasks → Worker: exigir OIDC (service account invoker).
- `TASKS_OIDC_AUDIENCE` deve estar alinhado com o `status.url` do worker (por ambiente).


# Story 1 — DB + `/rates` + `/child-policies` (buckets 0..17)

> ⚠️ **Security P0 (webhooks/tasks auth) é fora do escopo do Story 1.**

## Objetivo
Introduzir política de crianças por idade (0–17) com até **3 buckets por property**, e permitir cadastrar preços por bucket por dia via API, sem mexer ainda em quote/WhatsApp.

## 1) Migrações (Postgres)

### 1.1 Tabela `property_child_age_buckets`
- PK `(property_id, bucket)` onde `bucket ∈ {1,2,3}`
- `min_age` / `max_age` com `0..17` e `min_age <= max_age`

### 1.2 Sem sobreposição (robusto no DB)
Preferência:
- `EXCLUDE` constraint usando range por property (requer `btree_gist`), impedindo ranges que se cruzem no mesmo `property_id`.

Alternativa (se quiser evitar extensão):
- validação no endpoint + constraint adicional simples (menos robusto contra race).

### 1.3 Cobertura completa 0..17
Regra fechada:
- buckets precisam cobrir **todo** o intervalo `0..17` **sem gaps**.
- Se policy incompleta → deve falhar (`400`) no endpoint e, no futuro, quote indisponível com `child_policy_incomplete`.

Implementação:
- Obrigatório: validação no endpoint.
- Opcional (mais robusto): trigger AFTER INSERT/UPDATE/DELETE que impede estado inválido por `property_id`.

### 1.4 `room_type_rates`: child pricing bucket-based com compat
Decisão operacional (migração room_type_rates): Como o sistema ainda não está em produção, o padrão é Opção A (RENAME): renomear no banco price_{1..3}chd_cents para price_bucket{1..3}_chd_cents no mesmo deploy, mantendo retrocompatibilidade apenas na API /rates (aliases de leitura/escrita por tempo limitado) para não quebrar o Admin. Opção B (ADD colunas) fica removida deste plano e só pode voltar mediante mudança explícita de decisão no documento (ex.: descoberta de dependência inevitável fora da API)..

Opção A (RENAME) só após confirmação explícita de dependências.
- `price_1chd_cents` → `price_bucket1_chd_cents`
- `price_2chd_cents` → `price_bucket2_chd_cents`
- `price_3chd_cents` → `price_bucket3_chd_cents`

Obs:
- A API `/rates` precisa continuar aceitando/retornando os campos legados como alias **temporário**, para não quebrar o Admin imediatamente.

---

## 2) Endpoint novo: `GET /child-policies` e `PUT /child-policies`

> Tenancy/RBAC: `property_id` obrigatório via `?property_id=`; validar com `require_property_role(min_role)`; **não aceitar `property_id` no body**.

### GET
`GET /child-policies`
- retorna os 3 buckets (`bucket`, `min_age`, `max_age`)
- se não existir policy/buckets:
  - retorna **default sugerido** (0–3 / 4–12 / 13–17) **sem persistir** (staging/dev dependem de seed)

### PUT
`PUT /child-policies`
- payload: exatamente 3 buckets
- validações:
  - buckets 1..3 presentes
  - `0 <= min_age <= max_age <= 17`
  - **cobertura completa 0..17 sem gaps** (obrigatório no endpoint)
- integridade:
  - **sem overlap** garantido **no DB** via `btree_gist` + `EXCLUDE` (revalidar na API só para erro amigável)
  - trigger de “cobertura completa” no DB é **opcional** (hardening)
- persistência em transação:
  - estratégia simples: delete por property + insert dos 3 buckets (ou upsert equivalente)

---

## 3) Ajuste do endpoint existente: `/rates` (compat)

property_id via `?property_id=` validado por RBAC

### 3.1 GET /rates
Ao ler:
- retornar campos novos:
  - `price_bucket1_chd_cents`, `price_bucket2_chd_cents`, `price_bucket3_chd_cents`
- retornar também campos legados como alias temporário:
  - `price_1chd_cents`, `price_2chd_cents`, `price_3chd_cents`
  - mesmos valores dos buckets correspondentes

### 3.2 PUT /rates
Ao escrever:
- aceitar payload com campos **novos** ou **legados**
- se vierem **ambos** e divergirem → rejeitar `400` (evita ambiguidade)
- persistência sempre **normalizada** nos buckets (ou nas novas colunas, se Opção B); legados são **alias** para compat

---

## 4) Seeds (staging/dev)

- Seed obrigatório via mecanismo do repo (job `hotelly-seed-staging`), com seed **idempotente** (upsert por PK).
- Implementação esperada no repo: `src/hotelly/operations/seed_staging.py` (ou equivalente invocado pelo job).
- Defaults fixos:
  - bucket1: 0–3
  - bucket2: 4–12
  - bucket3: 13–17

---

## 5) Testes (mínimos e completos)
- `PUT /child-policies`:
  - aceita 0–3 / 4–12 / 13–17
  - rejeita overlap (DB + validação)
  - rejeita gaps (cobertura incompleta)
  - rejeita fora de 0..17
- `/rates`:
  - GET retorna novos + legados
  - PUT aceita novos OU legados; ambos divergentes → 400
  - limite 366 permanece
  - query params/contrato permanecem

---

## Critérios de aceite (Story 1)
- Migrações sobem limpas.
- `GET/PUT /child-policies` com regra “sem overlap + cobertura 0..17”.
- `/rates` continua funcional para consumidores antigos (campos legados ainda entram/aparecem).
- `./scripts/verify.sh` verde.

---

## 7. Stories 2→5 (visão resumida)

### Story 2 — Admin/API: edição de buckets e rates bucket-based (operável)
- Admin consegue ler/editar buckets (via `/child-policies`)
- Admin consegue ler/editar rates com buckets (UI pode inicialmente usar alias, mas deve suportar novos)

### Story 3 — Parsing/WhatsApp: ocupação explícita e coleta de idades (multi-turn)
- substituir `guest_count` por `adult_count` + `children_ages`
- se crianças sem idades → fluxo pergunta idades
- persistir contexto normalizado em `conversations.context` (sem texto cru)

### Story 4 — Quote engine: pricing robusto sem fallback
- precificação por bucket por criança e por noite
- sem fallback para `ari_days.base_rate_cents`
- `reason_code` sempre que indisponível (log estruturado)

### Story 5 — Persistência transacional (holds/reservations) e E2E
- persistir ocupação (adult_count + children_ages)
- conversão hold→reservation carrega ocupação
- E2E staging: WhatsApp → quote → hold → Stripe → reserva confirmada

---

## 8. Anexos rápidos: comandos usados (referência operacional)

### Domain Mapping (Cloud Run)
- criar:
  - `gcloud beta run domain-mappings create --service <SERVICE> --domain <DOMAIN> --region us-central1 --project <PROJECT>`
- descrever:
  - `gcloud beta run domain-mappings describe --domain <DOMAIN> --region us-central1 --project <PROJECT> --format="yaml(status.conditions)"`

### Secrets (Secret Manager)
- criar secret:
  - `gcloud secrets create <NAME> --replication-policy="automatic" --project <PROJECT>`
- adicionar versão:
  - `echo -n "VALUE" | gcloud secrets versions add <NAME> --data-file=- --project <PROJECT>`
- permissões (runtime SA precisa acessar):
  - `gcloud secrets add-iam-policy-binding <NAME> --member="serviceAccount:<SA>" --role="roles/secretmanager.secretAccessor" --project <PROJECT>`

---

## 9. Pendências de documentação (curto)
- Atualizar runbook antigo: remover afirmação “só /health”, listar rotas reais.
- Registrar decisão de domínios (app/adm/root redirect) e o porquê.
- Fixar padrão de nomes de serviço e ambiente (staging/prod) e o caminho de deploy de cada repo.
---

## 10. Controle de Andamento — Registro de Execução

> **Regra:** este documento é **append-only** (não remover conteúdo). Correções devem ser registradas como *errata*.

### Execução concluída — Story 1 (Child Policies + compat /rates)

**Status:** CONCLUÍDO (staging + repos)  
**Data do registro:** 2026-02-06

#### Entregas (Banco — staging)
- Criada tabela `property_child_age_buckets` com constraint **EXCLUDE anti-overlap** (intervalos por `property_id` não podem se sobrepor).
- Renomeadas colunas (DB):  
  - `price_1chd_cents` → `price_bucket1_chd_cents`  
  - `price_2chd_cents` → `price_bucket2_chd_cents`  
  - `price_3chd_cents` → `price_bucket3_chd_cents`

#### Entregas (Backend — `hotelly-v2`)
- **Novo endpoint** `GET /child-policies`  
  - Quando não há buckets persistidos para a property: retorna **default** `[{1,0,3}, {2,4,12}, {3,13,17}]` **sem persistir**.
- **Novo endpoint** `PUT /child-policies`  
  - Validação completa: exatamente **3 buckets**, cobertura **0..17**, sem gaps/overlap; operação transacional (delete + insert).
- **Compatibilidade `/rates` (legado)**
  - `GET /rates`: retorna campos novos + aliases legados.
  - `PUT /rates`: aceita payload com campos novos **ou** legados; se ambos presentes e divergentes → **HTTP 400**.
- Atualizações internas: `db.py` e `test_quote.py` com as novas colunas.
- Seed staging de buckets default: (0–3), (4–12), (13–17).
  - Idempotência: `INSERT ... ON CONFLICT (property_id, bucket) DO NOTHING`.

#### Entregas (Frontend — `hotelly-admin`)
- Tipo `RateDay` atualizado: campos `price_bucket{1..3}_chd_cents` + legados opcionais.
- `RatesGrid.tsx` atualizado para suportar os novos campos e compat.

#### Verificações / Evidências (aceite técnico)
- `GET /child-policies` default ok (sem persistência) — coberto por `test_get_child_policies_default` (mock `fetchall_result=[]` → retorna defaults).
- RBAC/property_id ok:
  - `property_id` exclusivamente via `?property_id=` (PropertyRoleContext).
  - `GET /child-policies`: `require_property_role("viewer")`
  - `PUT /child-policies`: `require_property_role("staff")`
- Seed idempotente ok: `ON CONFLICT ... DO NOTHING`.
- Pipeline local/staging ok:
  - `uv run python -m compileall -q src tests` (sucesso)
  - `uv run pytest -q` (**336 passed total**)
  - `bash scripts/verify.sh` → “All checks passed!” (336 passed, 0 failed)

#### Observações
- Decisão implementada conforme Story 1 / Opção A (RENAME no DB; compat somente na API do `/rates`).


---

# ✅ Story 2 — Concluída (Admin: Child Policies + Rates por Bucket) — 2026-02-06

## Status
- **Implementação concluída no repo `hotelly-admin`**
- **Pendente:** checklist manual em staging (depende de deploy)

## Entregas (Admin)

### Novos arquivos
- `src/app/api/p/[propertyId]/child-policies/route.ts` — proxy API (GET/PUT)
- `src/lib/childPolicies.ts` — `getChildPolicies`, `putChildPolicies`, `validateBuckets`
- `src/app/p/[propertyId]/child-policies/page.tsx` — página (server component)
- `src/app/p/[propertyId]/child-policies/ChildPoliciesEditor.tsx` — editor (validação client-side + badge **“Sugestão”**)

### Arquivos alterados
- `PropertyHeader.tsx` — link “Crianças” na navegação
- `RatesGrid.tsx` — 3 linhas de criança com labels dinâmicas (ex.: “Criança (0–3)”), **envia apenas campos `price_bucket*_chd_cents`**, sem legados

## Regras atendidas (alinhadas ao doc_unificado)
- `GET/PUT /child-policies` via `?property_id=` (no backend). No Admin, proxy usa `propertyId` só para montar query param.
- Labels dos buckets derivadas da policy (ou fallback para “Bucket 1/2/3” se policy falhar).
- Compat `/rates`: Admin **prioriza campos bucket** e **não envia** aliases legados.

## Verificações técnicas (Admin)
- TypeScript: ✅ sem erros
- ESLint: ✅ limpo
- Build: ✅ sucesso

## Pendência obrigatória (antes de “done” definitivo)
- Executar **Checklist manual em staging (Story 2, seção 7)** após deploy do `hotelly-admin` em staging.
  - Evidência requerida: prints/IDs (property, requests) + confirmação de que nenhum request envia `price_1chd_cents..price_3chd_cents`.



---

# ✅ PROGRESSO — Story 3 concluído (WhatsApp multi-turn: adult_count + children_ages) — 2026-02-06

## Status
- **Story 3: CONCLUÍDA**
- `scripts/verify.sh`: **verde**
- Suite de testes: **348 passed**

## Entregas (conforme relatório de execução)

### Banco (staging)
- Migração aplicada: adicionada coluna `conversations.context` **JSONB** com default.

### Backend (`hotelly-v2`)

#### Domínio — `intents.py`
- `ParsedIntent` agora inclui:
  - `adult_count`
  - `children_ages`
- Adicionado `derived_guest_count()` para compatibilidade com fluxo legado (deriva `guest_count` de `adult_count + len(children_ages)`).

#### Domínio — `parsing.py`
- Novos patterns:
  - `_ADULT_PATTERNS`
  - `_CHILD_COUNT_PATTERNS`
  - `_CHILDREN_AGES_PATTERN`
  - `_STANDALONE_AGES_PATTERN`
- Novas funções:
  - `_extract_adults()`
  - `_extract_children()`
  - `_parse_age_list()`
- `parse_intent()` agora:
  - extrai `adult_count` + `children_ages`
  - trata “X pessoas” como adultos (compat)
  - calcula `missing` usando `adult_count` (em vez de `guest_count`)
- Formatos suportados para idades (com/sem “anos”):
  - “3 e 7”
  - “3,7”
  - “3 7”

#### Handler — `tasks_whatsapp.py`
- `_process_intent()` reescrito para **multi-turn**:
  1) carrega `conversations.context`
  2) merge de entidades da mensagem
  3) persiste context
  4) calcula `missing` no acumulado
- Ordem de prompts:
  - dates → room_type → adult_count → children_ages
- Se crianças mencionadas sem idades:
  - **pergunta idades**
  - **não tenta cotar**
- `guest_count` agora é **derivado internamente** (compat).

#### Templates — `templates.py`
- Novos templates:
  - `prompt_adult_count`
  - `prompt_child_count`
  - `prompt_children_ages`

### Testes
- `tests/test_parsing_children.py` — 9 testes de parsing (adultos, crianças, idades, abreviações, standalone ages).
- `tests/test_whatsapp_context.py` — 4 testes multi-turn (prompt de idades, acumulação de context, prompt de adulto, fluxo completo).
- 3 testes existentes atualizados (troca `guest_count` → `adult_count` no `missing`).
- Total: **348 passed**, `verify.sh` verde.

## Commit
- `feat: whatsapp collects adult_count and children ages (context)`

## Observações operacionais
- Persistência do contexto é **normalizada** (JSONB) para suportar multi-turn.
- Compatibilidade de `guest_count` foi mantida **como derivação**, para permitir transição sem quebra até o story de remoção.


---

# ✅ PROGRESSO — Story 4 concluído (Quote Engine: adult_count + children_ages, buckets, **sem fallback**) — 2026-02-06

## Status
- **Story 4: CONCLUÍDA**
- `scripts/verify.sh`: **verde** (observação: 1 flaky pré-existente relacionado a JWKS, **não** relacionado ao Story 4)
- Suite de testes: **348 passed**
- `compileall`: ✅
- `ruff`: ✅

## Entregas (conforme relatório de execução)

### Backend (`hotelly-v2`)

#### Domínio — `quote.py` (reescrito)
- Nova exceção: `QuoteUnavailable(reason_code, meta)` com **13 reason_codes padronizados**.
- Helper `_bucket_for_age()`:
  - mapeia idade (0..17) → bucket (1..3) via `property_child_age_buckets`.
- `quote_minimum()`:
  - nova assinatura com parâmetros primários: `adult_count` + `children_ages`.
  - **Bridge legacy**: se apenas `guest_count` for fornecido no call-site, trata como:
    - `adult_count = guest_count`
    - `children_ages = []`
- Validações fail-fast:
  - datas inválidas → `invalid_dates`
  - `adult_count` fora de 1..4 → `invalid_adult_count`
  - idades fora de 0..17 → `invalid_child_age`
- Child policy:
  - busca buckets no DB
  - valida cobertura completa 0..17 **sem gaps** (falhas com reason_code específico)
- ARI checks:
  - valida existência do registro ARI
  - valida `currency == BRL`
  - valida inventário disponível
  - cada falha com `reason_code` específico
- Pricing **sem fallback**:
  - `price_{N}pax_cents` obrigatório
  - `price_bucket{1..3}_chd_cents` por criança (por idade→bucket)
  - **proibido** fallback para `ari_days.base_rate_cents`
- Cálculo:
  - `nightly = adult_base + sum(child_prices)`
  - total = soma de nightly por noite

#### Call-site — `tasks_whatsapp.py`
- Import atualizado para `QuoteUnavailable`.
- Chamada a `quote_minimum` envolvida em `try/except QuoteUnavailable`.
- Log estruturado com `reason_code` (sem PII).
- Removido bloco `if quote is None` (substituído pelo `except`).

### Testes — `test_quote.py` (reescrito)
- 14 testes cobrindo cenários:
  - Adult-only (1 e 2 pax)
  - Com crianças (bucket pricing)
  - Child policy: missing / incomplete
  - Rate: missing / pax rate NULL / child rate NULL
  - ARI: no record / no inventory / wrong currency
  - Validações: adult_count inválido / child age inválida
  - Legacy bridge (guest_count → adult_count)
- Helper `seed_child_age_buckets` adicionado.
- Fixture `ensure_property` limpa `property_child_age_buckets` no teardown.

## Commit
- `feat: quote uses adult_count + children_ages with child buckets (no fallback)`

## Observações operacionais
- O quote agora falha com diagnóstico explícito (`reason_code`) em vez de fallback silencioso.
- Mantém compat transitória apenas via bridge `guest_count` → `adult_count` até a remoção completa no Story 5.


---

# ✅ PROGRESSO — Story 5 concluído (Persistência de ocupação + remoção de `guest_count`) — 2026-02-06

## Status
- **Story 5: CONCLUÍDA**
- `scripts/verify.sh`: **verde** (observação: 2 flakys pré-existentes relacionados a JWKS, **não** relacionados ao Story 5)
- Suite de testes: **347 passed**
- Remoção completa: `rg guest_count -t py src/ tests/` → **vazio**

## Entregas (conforme relatório de execução)

### Banco (staging)
- Migrações aplicadas:
  - `holds`:
    - adicionados `adult_count SMALLINT NOT NULL`
    - adicionados `children_ages JSONB NOT NULL DEFAULT '[]'`
    - backfill determinístico a partir de `guest_count`
    - `guest_count` **dropado**
  - `reservations`:
    - mesma estratégia (adult_count + children_ages, backfill, drop `guest_count`)
- Observação: `conversations.context` já havia sido adicionado no Story 3.

### Backend (`hotelly-v2`)

#### Repositórios
- `holds_repository.py`:
  - `insert_hold` persiste `adult_count` + `children_ages` (JSON).
- `reservations_repository.py`:
  - `insert_reservation` persiste `adult_count` + `children_ages`.

#### Domínio
- `holds.py`:
  - `create_hold` recebe `adult_count` + `children_ages`.
- `convert_hold.py`:
  - lê ocupação do hold e copia para a reservation.
- `quote.py`:
  - `adult_count` obrigatório
  - **removido bridge legacy** (`guest_count` → `adult_count`).
- `intents.py`:
  - removido `guest_count` e `derived_guest_count()`.
- `parsing.py`:
  - removido `_GUEST_PATTERNS` e `_extract_guest_count`
  - patterns de “pessoas/hóspedes/pax” absorvidos por `_ADULT_PATTERNS`.

#### Templates
- removido `prompt_guest_count`.
- `{guest_count}` → `{adult_count}` nos templates de quote.

#### Handlers
- `tasks_whatsapp.py`:
  - fluxo completo agora usa `adult_count` + `children_ages` (context → quote → hold).
- `webhooks_whatsapp.py` e `webhooks_whatsapp_meta.py`:
  - entidades agora usam `adult_count` + `children_ages`.

#### Seed (staging)
- Atualizado para `adult_count` + `children_ages`.

## Commit
- `feat: persist occupancy and remove guest_count`

## Resumo consolidado da branch
- Branch: `feat/child-age-buckets-and-bucket-rates`
- Stories 1..5 entregues e pushados:
  - Story 1 — Backend: child age buckets + bucket rates (8 files, +796)
  - Story 2 — Admin: child policies UI + bucket rates grid (6 files, +464)
  - Story 3 — WhatsApp multi-turn parsing + context (7 files, +443)
  - Story 4 — Quote engine com buckets (sem fallback) (3 files, +355)
  - Story 5 — Persistência ocupação + remoção `guest_count` (17 files, +108/-161)

## Observações operacionais
- O sistema passa a operar com ocupação **explícita** (`adult_count`, `children_ages`) de ponta a ponta no backend.
- A compat de `guest_count` foi removida conforme premissa de “não está live”.


---

# ✅ FASE 19 — Precificação de crianças por idade (robusta) — **CONCLUÍDA** — 2026-02-06

## Escopo da fase (plano em 5 stories — executado)
Objetivo: habilitar precificação robusta de crianças por idade, ponta a ponta.

### Decisões fechadas (implementadas)
- Buckets por `property_id`:
  - **sem sobreposição** + **cobertura completa 0..17**
  - policy incompleta → quote indisponível (`reason_code=child_policy_incomplete`)
- Quote indisponível:
  - registra `reason_code` em **log estruturado**
  - resposta pública continua `None` (por enquanto)

## Stories (sequenciais) — status final
- ✅ **Story 1 — DB + endpoints**
  - `property_child_age_buckets` + EXCLUDE anti-overlap
  - `/child-policies` (GET/PUT) com validação completa
  - `/rates` com compat (GET novo+aliases; PUT aceita novo ou legado, conflito=400)
- ✅ **Story 2 — Admin UI**
  - configurar buckets por property
  - editar rates por bucket com labels dinâmicas (envia só bucket fields)
- ✅ **Story 3 — WhatsApp multi-turn**
  - `conversations.context` JSONB
  - parsing estrito de idades + prompts
  - fluxo não cota sem idades
- ✅ **Story 4 — Quote engine**
  - `adult_count` + `children_ages[]`
  - buckets por idade + `QuoteUnavailable(reason_code, meta)`
  - **sem fallback** para `ari_days.base_rate_cents`
  - call-site log estruturado por `reason_code`, retorno público `None`
- ✅ **Story 5 — Persistência**
  - `holds`/`reservations` com `adult_count` + `children_ages`
  - remoção completa de `guest_count` (DB + código)
  - seeds/handlers atualizados

## Evidências de conclusão
- Todos os 5 stories entregues e pushados na branch `feat/child-age-buckets-and-bucket-rates`.
- Suite de testes e `scripts/verify.sh` verdes (flakys JWKS pré-existentes, sem relação com a fase).
- Contratos e invariantes desta fase estão atendidos (policy 0..17, quote sem fallback, reason_code em logs, remoção de `guest_count`).

## Próximos passos (fora desta fase)
- Promover para ambientes seguintes (se aplicável) e rodar checklists de deploy/monitoramento.
- (Opcional) Evoluir resposta pública para expor diagnóstico ao operador (sem vazar internals).


---

# 🔧 ERRATA / ATUALIZAÇÃO DE STATUS — FASE 19 — 2026-02-06

## Correção
Na seção **“Próximos Passos”**, a linha **“⏳ FASE 19: Precificação de crianças por idade (robusta) — plano em 5 stories”** ficou **desatualizada**.

## Status correto
- ✅ **FASE 19: CONCLUÍDA**
- Motivo: os **5 stories (1..5)** foram entregues e pushados na branch `feat/child-age-buckets-and-bucket-rates`, com `verify.sh` verde (flakys JWKS pré-existentes).

## Como ler o documento a partir daqui
- Considere a FASE 19 como **removida de “Próximos Passos”** e **encerrada**.
- A seção **“✅ FASE 19 — ... — CONCLUÍDA”** adicionada ao final deste documento é a referência vigente.
