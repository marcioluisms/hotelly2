# S4 — Pricing e Unit Economics (Hotelly V2)

**Status:** PRONTO (piloto definido; pricing definitivo pós-métricas)  
**Data:** 2026-01-25  

## Objetivo
Definir uma política de cobrança que:
- não atrase execução do MVP,
- seja tecnicamente simples de operar no piloto,
- gere dados para precificação definitiva,
- evite distorções (ex.: incentivos para “burlar” o sistema).

## Decisão para o piloto (até 10 pousadas)
1) **Cobrança no piloto:** **R$ 0** (gratuito) ou **valor simbólico** (se necessário por posicionamento), **sem cobrança por reserva** no MVP.
2) **Critério técnico:** reduzir variáveis enquanto estabilizamos o core transacional (inventário/holds/pagamentos) e o canal (WhatsApp).
3) **Duração:** até atingir os critérios de saída do piloto (ver `06_success_metrics.md`) ou uma data-limite operacional definida no `03_pilot_strategy.md`.

> Observação: se optar por “valor simbólico”, ele deve ser uma mensalidade fixa simples (sem fee variável), para evitar complexidade operacional e disputas por conciliação.

## Pricing definitivo (pós-piloto): opções técnicas (não decididas agora)
Ao encerrar o piloto, escolher entre 3 modelos, com base em dados:

### Opção A — Mensalidade fixa por propriedade
- **Prós:** simplicidade de cobrança e previsão de receita.
- **Contras:** menos alinhado à sazonalidade; pode reduzir adoção em pousadas muito pequenas.

### Opção B — Fee por reserva confirmada (take rate)
- **Prós:** alinha valor ao uso (paga quando converte).
- **Contras:** exige conciliação perfeita (Stripe ↔ reservation), anti-fraude e regras para cancelamentos/no-show.

### Opção C — Híbrido (mensalidade baixa + fee menor)
- **Prós:** balanceia previsibilidade e alinhamento ao uso.
- **Contras:** complexidade moderada.

## Métricas para escolher pricing (inputs obrigatórios)
Usar o `06_success_metrics.md` como base, e olhar especificamente:
- conversão WhatsApp → pagamento → reserva confirmada,
- volume de reservas/mês por pousada,
- custo operacional (suporte/reprocess),
- custo de infra (Cloud Run/SQL/Tasks) por reserva,
- taxa de disputas/exceções (pagamento sem reserva, holds presos).

## Unit economics (modelo mínimo)
### Custos diretos (por ambiente)
- Cloud Run (requests + CPU/mem)
- Cloud SQL (instância + storage + backups)
- Cloud Tasks
- Logs/monitoramento (se aplicável)
- Egress (WhatsApp/Stripe e tráfego)

### Custos operacionais
- tempo de suporte por incidente (runbook)
- custo de reconciliação (queries e reprocess)

### “Regra técnica” para evitar autoengano
Não definir pricing definitivo sem:
- 30 dias de operação com métricas estáveis (SLOs cumpridos),
- taxa baixa de exceções transacionais (pagamento sem reserva, overbooking=0),
- entendimento do custo por reserva confirmada.

## Implicações no produto (quando for cobrar)
- Implementar “billing state” por propriedade (trial/active/past_due) e políticas de grace period.
- Garantir logs e auditoria sem PII (compliance).
- Definir comportamento de degradação (ex.: “somente consulta” se inadimplente) — decisão posterior.

## Checklist
- [x] Piloto com cobrança mínima (zero ou simbólica) definida.
- [x] Métricas necessárias para precificar estão definidas no `06_success_metrics.md`.
- [ ] Após piloto: escolher opção A/B/C com base nos dados e registrar em ADR (futuro).
