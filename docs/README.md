# Documentação — Hotelly V2

Este arquivo é a **porta de entrada** da documentação do repositório.

- **Estratégico**: direção, ICP, piloto, métricas e pricing do piloto  
- **Tático**: arquitetura, domínio, dados e contratos  
- **Operacional**: como desenvolver, testar, observar e operar em produção  

> Regra: o core transacional é determinístico. IA não decide estado crítico.

---

# ÍNDICE DEFINITIVO — DOCUMENTAÇÃO HOTELLY V2

### Legenda de maturidade

- **Status** (qualidade do texto): 🟢 PRONTO | 🟡 PARCIAL | 🔴 A COMPLETAR
- **Maturidade** (o que dá para executar hoje): ✅ EXECUTÁVEL NO REPO | ⚠️ CONCEITUAL/DEPENDE DE ARTEFATOS | 🎯 TARGET (pós-MVP)

## 🔵 NÍVEL ESTRATÉGICO

**Objetivo:** definir direção, limites e critérios de sucesso
**Regra:** muda pouco, só com decisão consciente

📁 `docs/strategy/`

### S1. Visão, North Star e Tese do Produto

📄 `01_north_star.md`
**Status:** 🟡 **PARCIAL (esboço; expandir antes do piloto)**
**Maturidade:** ⚠️ **CONCEITUAL**
Conteúdo:

* O que é o Hotelly
* O problema que resolve
* Proposta de valor
* North Star Metric
* Princípios não negociáveis (sem overbooking, IA com guardrails, idempotência)

---

### S2. ICP e Segmentação

📄 `02_icp_segmentation.md`
**Status:** 🟡 **PARCIAL (esboço; expandir)**
**Maturidade:** ⚠️ **CONCEITUAL**
Conteúdo:

* Tipo de pousada
* Quem NÃO é ICP
* Contexto operacional (WhatsApp, sazonalidade, equipe pequena)

---

### S3. Estratégia de Piloto

📄 `03_pilot_strategy.md`
**Status:** 🟡 **PARCIAL (esboço; expandir critérios de saída e operação)**
**Maturidade:** ⚠️ **CONCEITUAL**
Conteúdo:

* Até 10 pousadas
* Sem HA
* Expectativa explícita de falhas
* Objetivo: observabilidade + aprendizado
* Critério de saída do piloto

---

### S4. Modelo de Receita e Pricing

📄 `04_pricing_unit_economics.md`
**Status:** 🟢 **PRONTO (orientativo; validar com dados do piloto)**
**Maturidade:** ⚠️ **CONCEITUAL**
Aberto:

* Fee por reserva (sim/não)
* Política de piloto (gratuito / simbólico)

---

### S5. Roadmap por Capabilities

📄 `05_capability_roadmap.md`
**Status:** 🟡 **PARCIAL (alto nível; detalhar por capability)**
**Maturidade:** ⚠️ **CONCEITUAL**
Conteúdo:

* Conversa
* Cotação
* Hold
* Pagamento
* Confirmação
* Admin mínimo
* Observabilidade

---

### S6. Critérios de Sucesso e Métricas

📄 `06_success_metrics.md`
**Status:** 🟢 **PRONTO**
**Maturidade:** ⚠️ **CONCEITUAL** (vira executável quando dashboards/alerts existirem)
Conteúdo:

* Conversão WhatsApp → pagamento
* Taxa de handoff humano
* Falhas de expiração
* Incidentes críticos

---

## 🟠 NÍVEL TÁTICO

**Objetivo:** definir como o sistema funciona
**Regra:** não improvisar

📁 `docs/architecture/`

### T1. Arquitetura de Referência

📄 `01_reference_architecture.md`
**Status:** 🔴 **A COMPLETAR (faltam diagramas, fronteiras e fluxos)**
**Maturidade:** ⚠️ **CONCEITUAL**

---

📁 `docs/domain/`

### T2. Modelo de Domínio e State Machines

📄 `01_state_machines.md`
**Status:** 🔴 **A COMPLETAR (transições completas + eventos/outbox + diagramas)**
**Maturidade:** ⚠️ **CONCEITUAL**

---

📁 `docs/data/`

### T3. Modelo de Dados — Cloud SQL (SoT)

📄 `01_sql_schema_core.sql`
**Status:** 🟡 **PARCIAL (core existe; alinhar inconsistências schema↔docs↔SQL)**
**Maturidade:** ✅ **EXECUTÁVEL NO REPO** (após alinhar inconsistências)

📄 `02_sql_schema_ai.sql`
**Status:** 🟢 **PRONTO (decisão: deferido pós-MVP)**
**Maturidade:** 🎯 **TARGET**

📄 `03_sql_schema_knowledge.sql`
**Status:** 🟢 **PRONTO (decisão: deferido pós-MVP)**
**Maturidade:** 🎯 **TARGET**

---

📁 `docs/integrations/`

### T4. Contrato WhatsApp (Meta + Evolution)

📄 `whatsapp_contract.md`
**Status:** 🟢 **PRONTO**
**Maturidade:** ✅ **USÁVEL COMO CONTRATO**

### T5. Contrato Stripe

📄 `stripe_contract.md`
**Status:** 🟢 **PRONTO**
**Maturidade:** ✅ **USÁVEL COMO CONTRATO**
Decisão:

* Evento canônico: `checkout.session.completed` (converter apenas se `payment_status == "paid"`)

---

📁 `docs/adr/`

### ADRs (decisões travadas)

* `ADR-000-base-decisions.md` 🟡
* `ADR-001-database-cloud-sql.md` ✅
* `ADR-002-ai-model-gemini-2.5-flash.md` ✅
* `ADR-003-region-us-central1.md` ✅
* `ADR-004-whatsapp-providers.md` ✅

---

## 🟢 NÍVEL OPERACIONAL

**Objetivo:** rodar, testar e recuperar sem sofrimento
**Regra:** pode mudar, mas precisa estar escrito

📁 `docs/operations/`

### O1. Desenvolvimento Local

📄 `01_local_dev.md`
**Status:** 🟡 **PARCIAL (conteúdo detalhado; falta tornar executável com compose/make/.env)**
**Maturidade:** ⚠️ **DEPENDE DE ARTEFATOS**
Conteúdo:

* Docker / compose
* Seed de dados
* Replay de webhooks
* Comando único de verificação

---

### O2. CI/CD e Ambientes

📄 `02_cicd_environments.md`
**Status:** 🟡 **PARCIAL (política definida; CI ainda não cobre gates)**
**Maturidade:** ⚠️ **DEPENDE DE IMPLEMENTAÇÃO**

---

### O3. Testes e Regressão

📄 `03_test_plan.md`
**Status:** 🟡 **PARCIAL (bom; falta refletir testes realmente implementados)**
**Maturidade:** ⚠️ **DEPENDE DE IMPLEMENTAÇÃO**

---

### O4. Observabilidade

📄 `04_observability.md`
**Status:** 🟡 **PARCIAL (bom; falta instrumentação/dashboards/alerts no ambiente)**
**Maturidade:** ⚠️ **DEPENDE DE IMPLEMENTAÇÃO**
Conteúdo:

* Logs estruturados
* Métricas mínimas
* Alertas críticos

---

### O5. Runbook Operacional

📄 `05_runbook.md`
**Status:** 🟡 **PARCIAL (bom; falta amarrar mitigação a comandos acionáveis e tasks)**
**Maturidade:** ⚠️ **DEPENDE DE ARTEFATOS**
Conteúdo:

* Reprocessar webhook
* Resolver pagamento sem reserva
* Restore sem HA
* Cutover para HA

---

## 📌 RESUMO EXECUTIVO

* **Estratégico:** esboços (S1/S2/S3/S5) + pricing orientativo (S4) + métricas bem definidas (S6).
* **Tático:** contratos e schema core existem; lacunas principais são **arquitetura de referência** e **state machines**.
* **Operacional:** documentação extensa, mas ainda **não 100% executável** (faltam compose/Makefile/.env e alguns comandos acionáveis).
