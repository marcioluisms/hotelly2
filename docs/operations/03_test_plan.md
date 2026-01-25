# Plano de Testes — Hotelly V2 (`docs/operations/03_test_plan.md`)

## Objetivo
Garantir que o Hotelly V2 opere com **segurança transacional** e **previsibilidade operacional**, com foco em:
- **0 overbooking** sob concorrência (inventário nunca negativo e nunca excedido)
- **idempotência real** em webhooks, tasks e endpoints internos
- **semântica correta de ACK** (não matar retry do provedor por erro interno)
- **nenhum vazamento de PII/payload raw** em logs
- **replay confiável** (webhooks e tasks podem ser reprocessados com segurança)

Este documento é **normativo**: quando um teste/gate é marcado como MUST, a story relacionada só fecha quando houver prova executável em CI.

---

## Princípios
1) **Risk-based testing**: o esforço de teste escala com o risco (dinheiro/inventário > UX).
2) **Prova executável > revisão subjetiva**: gates objetivos substituem burocracia.
3) **Determinismo**: testes devem ser reproduzíveis (fixtures estáveis, tempo controlado, seeds consistentes).
4) **Isolamento**: integração com provedores é testada por “contrato” (payload fixtures + validações), e E2E real fica reservado a staging.

---

## Pirâmide de testes (o que existe e por quê)

### 1) Unit tests (rápidos, puros)
**Escopo:** validações, normalização de payloads, mapeamentos, parsing, cálculos de preço (quando aplicável).  
**Não cobre:** concorrência e atomicidade (isso é Integration).

### 2) Integration tests (Postgres + transações)
**Escopo:** todas as regras que dependem de lock, constraint, idempotência e atomicidade.  
Aqui vivem os testes que **evitam os erros da V1**.

### 3) Contract tests (provedores)
**Escopo:** garantir que os adaptadores aceitam/rejeitam payloads reais sem efeitos colaterais.  
Stripe/WhatsApp entram aqui com fixtures e validação de assinatura/campos.

### 4) E2E (staging) — mínimo e cirúrgico
**Escopo:** comprovar o fluxo completo (mensagem → hold → pagamento → reserva) e o comportamento de replay.  
Deve ser curto, repetível e rodar sob comando (script).

---

## Ambientes e dados

### Banco
- **Local/CI:** Postgres efêmero (container) + migrações aplicadas do zero.
- **Staging:** Postgres real (Cloud SQL) com migrações via pipeline.

### Dataset mínimo (fixture)
Todo teste de integração deve conseguir criar (ou reaproveitar) o conjunto mínimo:
- 1 `property`
- 1 `room_type`
- `ari_days` preenchido para um range de datas (ex.: hoje+1 até hoje+14)
- 1 `conversation` (quando necessário)
- holds/reservations/payments conforme o cenário

**Regra:** fixture deve ser pequena, mas suficiente para reproduzir concorrência (última unidade).

---

## Suites e casos mínimos (MUST)

### A) Gates de qualidade (mapeamento direto para CI)
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

**G6 — Pricing determinístico (MUST quando existir pricing)**
- golden tests (BPS/FIXED/PACKAGE) para impedir regressão

> Observação: a lista completa dos gates está em `docs/operations/07_quality_gates.md`.

---

## B) Testes de integração — transações críticas (Postgres)

### B1) CREATE HOLD (MUST)
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

### B2) EXPIRE HOLD (MUST)
**O que provar**
- Dedupe por `processed_events(source='tasks', external_id=task_id)` ou equivalente.
- `SELECT ... FOR UPDATE` no hold (evita double-free).
- Libera ARI (`inv_held--`) e marca status `expired`.
- Outbox grava `hold.expired`.

**Casos mínimos**
1) Expirar hold elegível → libera ARI e muda status.
2) Repetir a mesma task → no-op (G3).
3) Hold já cancelado/convertido → no-op.

### B3) CANCEL HOLD (MUST)
**O que provar**
- Mesmo desenho de expire: lock, liberar ARI, status `cancelled`.
- Idempotência: cancelar 2x não “desconta duas vezes”.
- Outbox `hold.cancelled`.

### B4) CONVERT HOLD (MUST)
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

## C) Testes de contrato — provedores (sem efeitos colaterais)

### C1) Stripe (MUST)
**Objetivo:** garantir parsing e validações antes de enfileirar/rodar efeitos.
- Assinatura inválida → rejeitar (4xx) sem side effect.
- Evento válido mas tipo não suportado → 2xx ou no-op documentado (sem efeitos).
- Evento duplicado → dedupe garante 1 efeito (coberto em G3/G5 via integração, mas aqui valida parsing).

**Fixtures**
- `checkout.session.completed` (ou evento adotado)
- `payment_intent.succeeded` (se usado)
- payloads com campos faltando (devem falhar limpo)

### C2) WhatsApp (MUST)
**Objetivo:** adaptadores (Meta/Evolution) convertem para um **InboundMessage** interno único.
- payload mínimo válido → gera InboundMessage
- payload com campos ausentes → rejeita limpo
- message_id repetido → dedupe é garantido no pipeline (G3), mas aqui validamos extração correta do ID

---

## D) E2E (staging) — mínimo obrigatório

### D1) Fluxo MVP (MUST)
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

### D2) Replay e recuperação (MUST)
- Reprocessar webhook Stripe (replay) sem duplicidade
- Reprocessar task de expire sem double-free
- Reprocessar convert após falha transient (DB/timeout) com idempotência preservada

---

## Segurança e privacidade (testes e lint)

### S1) PII/log hygiene (MUST)
- CI falha ao detectar padrões proibidos (Gate G2).
- Testes devem inspecionar logs em cenários críticos para garantir que **não** há payload raw.

### S2) Rotas internas (MUST)
- Teste de introspecção garante que `/internal/*` não aparece no router público.

---

## Como rodar (padrão recomendado)

### Local
- Unit:
  - `pytest -q tests/unit`
- Integration (com Postgres):
  - `docker compose up -d postgres` (ou serviço equivalente)
  - `pytest -q tests/integration`
- Contract:
  - `pytest -q tests/contract`
- Suite mínima (antes de abrir PR):
  - `pytest -q tests/unit tests/integration -k "g3 or g4 or g5"`

### CI (ordem sugerida)
1) G0 (compile/build/start)
2) G1 (migrate + constraints)
3) Unit tests
4) Integration tests (incluindo G3–G5)
5) Contract tests
6) (Opcional) E2E em staging (manual/cron de pré-release)

---

## Critérios de aceite por story (regra prática)
- Story que toca **inventário/pagamento/transação crítica**: **G3–G5 obrigatórios**.
- Story que toca **pricing**: **G6 obrigatório**.
- Story qualquer: **G0–G2 obrigatórios**.

---

## Checklist para adicionar um novo teste (rápido e consistente)
1) Identificar se a mudança é: unit, integration, contract, e2e
2) Se tocar “dinheiro/inventário”: escrever caso de replay (idempotência) + caso de concorrência/race quando aplicável
3) Fixar tempo (ex.: usar clock controlado) e usar fixture mínima
4) Garantir que logs não incluem payload/PII
5) Amarrar ao gate correspondente (G3–G6) se aplicável

---

## Troubleshooting (quando teste falha)
- **Intermitência** geralmente indica falta de lock/ordem fixa de updates (ver guia de transações críticas).
- **Duplicidade** normalmente indica ausência de UNIQUE/processed_events ou uso incorreto de idempotency_keys.
- **Inventário negativo** indica double-free (expire/cancel/convert executando mais de uma vez sem proteção).
- **Webhook “sumindo”** indica 2xx retornado cedo demais (ACK errado) — consertar para receipt durável + enqueue.

---

## Não‑objetivos (por enquanto)
- Testes de carga completos (k6/locust) antes do MVP rodar em staging.
- Cobertura alta como meta em si (cobertura é consequência; gates são meta).
- UI/admin (fora do escopo do V2 MVP inicial).

