# Documentação — Hotelly V2

Este arquivo é a **porta de entrada** da documentação do repositório.

- **Estratégico**: direção, ICP, piloto, métricas e pricing do piloto  
- **Tático**: arquitetura, domínio, dados e contratos  
- **Operacional**: como desenvolver, testar, observar e operar em produção  

> Regra: o core transacional é determinístico. IA não decide estado crítico.

---

# ÍNDICE DEFINITIVO — DOCUMENTAÇÃO HOTELLY V2

## 🔵 NÍVEL ESTRATÉGICO

**Objetivo:** definir direção, limites e critérios de sucesso
**Regra:** muda pouco, só com decisão consciente

📁 `docs/strategy/`

### S1. Visão, North Star e Tese do Produto

📄 `01_north_star.md`
**Status:** 🟢 **PRONTO (base V1 + decisões V2)**
Conteúdo:

* O que é o Hotelly
* O problema que resolve
* Proposta de valor
* North Star Metric
* Princípios não negociáveis (sem overbooking, IA com guardrails, idempotência)

---

### S2. ICP e Segmentação

📄 `02_icp_segmentation.md`
**Status:** 🟢 **PRONTO (V1)**
Conteúdo:

* Tipo de pousada
* Quem NÃO é ICP
* Contexto operacional (WhatsApp, sazonalidade, equipe pequena)

---

### S3. Estratégia de Piloto

📄 `03_pilot_strategy.md`
**Status:** 🟢 **PRONTO (decidido aqui)**
Conteúdo:

* Até 10 pousadas
* Sem HA
* Expectativa explícita de falhas
* Objetivo: observabilidade + aprendizado
* Critério de saída do piloto

---

### S4. Modelo de Receita e Pricing

📄 `04_pricing_unit_economics.md`
**Status:** 🟡 **PARCIAL (base V1)**
Aberto:

* Fee por reserva (sim/não)
* Política de piloto (gratuito / simbólico)

---

### S5. Roadmap por Capabilities

📄 `05_capability_roadmap.md`
**Status:** 🟢 **PRONTO (V1)**
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
**Status:** 🔴 **A ESCREVER**
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
**Status:** 🟢 **PRONTO**

---

📁 `docs/domain/`

### T2. Modelo de Domínio e State Machines

📄 `01_state_machines.md`
**Status:** 🟢 **PRONTO**

---

📁 `docs/data/`

### T3. Modelo de Dados — Cloud SQL (SoT)

📄 `01_sql_schema_core.sql`
📄 `02_sql_schema_ai.sql`
📄 `03_sql_schema_knowledge.sql`
**Status:** 🟡 **EM ANDAMENTO (DDL a gerar)**

---

📁 `docs/integrations/`

### T4. Contrato WhatsApp (Meta + Evolution)

📄 `whatsapp_contract.md`
**Status:** 🟢 **PRONTO**

### T5. Contrato Stripe

📄 `stripe_contract.md`
**Status:** 🟢 **PRONTO**
Decisão:

* Evento canônico: `checkout.session.completed`

---

📁 `docs/adr/`

### ADRs (decisões travadas)

* `ADR-000-base-decisions.md` ✅
* `ADR-001-database-cloud-sql.md` 🔴
* `ADR-002-ai-model-gemini-2.5-flash.md` 🔴
* `ADR-003-region-us-central1.md` 🔴
* `ADR-004-whatsapp-providers.md` 🔴

---

## 🟢 NÍVEL OPERACIONAL

**Objetivo:** rodar, testar e recuperar sem sofrimento
**Regra:** pode mudar, mas precisa estar escrito

📁 `docs/operations/`

### O1. Desenvolvimento Local

📄 `01_local_dev.md`
**Status:** 🔴 **A ESCREVER**
Conteúdo:

* Docker / compose
* Seed de dados
* Replay de webhooks
* Comando único de verificação

---

### O2. CI/CD e Ambientes

📄 `02_cicd_environments.md`
**Status:** 🔴 **A ESCREVER**

---

### O3. Testes e Regressão

📄 `03_test_plan.md`
**Status:** 🟡 **PARCIAL (V1 excelente, adaptar para SQL)**

---

### O4. Observabilidade

📄 `04_observability.md`
**Status:** 🔴 **A ESCREVER**
Conteúdo:

* Logs estruturados
* Métricas mínimas
* Alertas críticos

---

### O5. Runbook Operacional

📄 `05_runbook.md`
**Status:** 🔴 **A ESCREVER**
Conteúdo:

* Reprocessar webhook
* Resolver pagamento sem reserva
* Restore sem HA
* Cutover para HA

---

## 📌 RESUMO EXECUTIVO

* **Estratégico:** ~85% pronto
* **Tático:** ~70% pronto (principal lacuna: SQL DDL + ADRs)
* **Operacional:** ~20% pronto (onde mais dói hoje)
