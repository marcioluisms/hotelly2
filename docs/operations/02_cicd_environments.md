# CI/CD e Ambientes — Hotelly V2 (`docs/operations/02_cicd_environments.md`)

## Objetivo
Definir **como** o Hotelly V2 é construído, testado e promovido entre ambientes (**dev → staging → prod**) com:
- **burocracia mínima**
- **gates objetivos**
- **segurança** (sem PII/segredos e sem rotas internas expostas)
- **confiabilidade** (idempotência, dedupe e retry corretos)

Este documento é **normativo**: se uma etapa “oficial” não existir no repo/infra, vira tarefa.

---

## Ambientes

### Local (`local`)
- Propósito: desenvolvimento e testes rápidos.
- Infra: Docker Compose (Postgres + app).
- Stripe: **test mode**.
- Dados: sintéticos/seed. Nunca PII real.

### Dev (`dev`)
- Propósito: integração contínua e validação rápida.
- Deploy: automático no merge/push na branch principal.
- Stripe: **test mode**.
- Dados: sintéticos + fixtures.
- Regra: pode quebrar, mas **gates não**.

### Staging (`staging`)
- Propósito: pré-produção (ensaio do que vai para prod).
- Deploy: promoção controlada (tag/release).
- Stripe: **test mode** (recomendado) ou “modo híbrido” apenas se necessário e isolado.
- Dados: sintéticos + cenários E2E.

### Produção (`prod`)
- Propósito: operação real.
- Deploy: promoção controlada + checklist.
- Stripe: **live mode**.
- Dados: reais (PII real existe aqui; logs nunca).

---

## Topologia recomendada por ambiente (GCP)

### Opção preferida (mais segura): **2 serviços Cloud Run**
1) **`hotelly-public`** (público)
   - Só expõe: `/webhooks/stripe/*`, `/webhooks/whatsapp/*`, `/health`
   - Faz **receipt durável** + **enqueue** (Cloud Tasks). Não processa pesado.
2) **`hotelly-worker`** (privado / auth obrigatório)
   - Só expõe: `/tasks/*`, `/internal/*` (se existir)
   - Executa o motor de domínio/transações críticas.

**Por quê:** Cloud Run é “auth por serviço”, não por rota. Separar serviços elimina o risco clássico de “rota interna exposta no público”.

### Opção mínima (aceitável no começo): **1 serviço Cloud Run público**
- Exigir verificação forte em **toda** rota pública:
  - Stripe: assinatura obrigatória
  - WhatsApp: verificação do provider
  - Tasks: header secreto + audience rígida (ou assinatura OIDC verificada)
- Rotas internas **não devem existir** no router público. (Gate G2 deve barrar.)

---

## Infra mínima por ambiente

### Cloud SQL (Postgres)
- Fonte da verdade transacional.
- Conexão Cloud Run → Cloud SQL via **Cloud SQL Connector/Auth Proxy** com IP público (conforme decisão do projeto).
- Estratégia de dados:
  - **dev/staging**: pode usar a mesma instância com **bases separadas** (`hotelly_dev`, `hotelly_staging`).
  - **prod**: instância dedicada (recomendado).

### Cloud Tasks
- Filas por ambiente (ex.: `default`, `expires`, `webhooks`).
- Tasks devem usar **OIDC** (service account) quando chamarem `hotelly-worker`.
- Retries configurados para tolerar falhas transitórias (DB/429 do provider).

### Secret Manager
- Segredos **por ambiente** (nomenclatura recomendada):
  - `hotelly-{env}-db-url` (ou host/user/pass separados)
  - `hotelly-{env}-stripe-secret-key`
  - `hotelly-{env}-stripe-webhook-secret`
  - `hotelly-{env}-whatsapp-verify-token`
  - `hotelly-{env}-whatsapp-app-secret` (se aplicável)
  - `hotelly-{env}-internal-task-secret` (se usar header)
- Regra: **zero segredos no repo**.

### Service Accounts (mínimo)
- `sa-hotelly-{env}-runtime` (Cloud Run)
  - Secret Manager Secret Accessor (apenas segredos do env)
  - Cloud SQL Client
  - Cloud Tasks Enqueuer (se o serviço enfileira)
- `sa-hotelly-{env}-tasks-invoker` (Cloud Tasks OIDC)
  - Invoker do `hotelly-worker` (Cloud Run)

---

## Estratégia de branch e versionamento (solo)
- Branch principal: `main` (ou `master`, mas escolha uma e mantenha).
- Trabalho diário: feature branch curta (`feat/...`, `fix/...`).
- Merge na principal somente com CI verde.
- Versões:
  - `v0.Y.Z` (enquanto em piloto)
  - tags são o artefato de promoção para staging/prod.

---

## CI — Pipeline (sempre)

### Estado atual (repo hoje)
No momento, o CI no repositório cobre apenas o mínimo (ex.: `compileall` e `pytest`).
Os **Quality Gates (G0–G6)** abaixo representam o **alvo normativo** do projeto.
Até estarem implementados no CI (ou em um script local padronizado), eles **não podem ser tratados como "aplicados"**.

Regra: qualquer item descrito como gate e ainda não implementado deve virar tarefa explícita (story) antes de ser usado como critério de aceite.

### Gatilhos
- Pull Request (feature → main): roda CI completo.
- Push/merge em `main`: roda CI completo + (opcional) deploy automático `dev`.
- Tag `v*`: roda CI + promove (staging/prod conforme regra abaixo).

> Nota: "CI completo" aqui significa **o que existe no repo**. Quando os gates forem implementados,
> esta seção permanece válida e passa a refletir a prática.

### Jobs mínimos (ordem)
1) **Lint/format** (rápido)
2) **Unit tests**
3) **Build Docker**
4) **Gates** (ver abaixo)
5) (opcional) **Integration tests** com Postgres (dev/staging)

### Quality Gates (hard fail)
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

- **G6 — Pricing determinístico**
  - golden tests para BPS/FIXED/PACKAGE* (quando pricing existir)

---

## CD — Promoção e Deploy

### Artefato de deploy
- **Imagem Docker** publicada no Artifact Registry (tag por commit e por versão).

### Deploy automático (dev)
- Trigger: push/merge em `main`
- Passos:
  1) CI completo (com gates)
  2) build + push da imagem (tag `sha`)
  3) deploy `hotelly-public`/`hotelly-worker` em `dev` apontando para segredos `dev`

### Promoção controlada (staging)
- Trigger: tag `v0.Y.Z` (ou release manual)
- Passos:
  1) CI completo (gates)
  2) promover **a mesma imagem** (não rebuildar) para `staging`
  3) smoke E2E (mínimo): hold → checkout → webhook → reserva confirmada (com replay de webhook)

### Promoção controlada (prod)
- Trigger: tag/release marcada como “prod”
- Passos:
  1) CI completo (gates)
  2) **migração manual** (ver política abaixo)
  3) deploy **a mesma imagem** em `prod`
  4) smoke pós-deploy (mínimo) + checagem de alertas

---

## Política de migrações (Postgres)
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

## Segurança de endpoints (regras mínimas)
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

## Checklist curto de release (staging/prod)
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

## Rollback (sem drama)
- **Rollback de app (Cloud Run):** voltar para revisão anterior (revisions).
- **Rollback de DB:** não contar com “down”.
  - usar compatibilidade (migração aditiva + código antigo ainda funciona)
  - se necessário: feature flag / desabilitar entrada (webhooks) temporariamente

---

## Convenções de nomes (sugestão)
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

## Próximo documento
- `docs/operations/03_test_plan.md` — adaptar o plano V1 para o modelo SQL/Tasks/Stripe (e transformar G3–G5 em testes “oficiais”).
