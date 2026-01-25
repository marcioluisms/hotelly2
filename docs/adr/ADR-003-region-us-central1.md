# ADR-003 — Região de implantação: us-central1 (Iowa) como padrão do Hotelly V2
**Status:** Accepted  
**Data:** 2026-01-25  
**Decisores:** Produto/Engenharia (Hotelly V2)  

## Contexto
O Hotelly V2 é um sistema multi-tenant para pousadas com fluxo crítico (inventário/holds/pagamentos) operado majoritariamente via WhatsApp, executando em serviços gerenciados na GCP (Cloud Run, Cloud SQL Postgres, Cloud Tasks, Secret Manager).

A escolha de região impacta:
- custo (Cloud Run, Cloud SQL, egress),
- latência percebida (Brasil),
- disponibilidade e maturidade de serviços,
- simplicidade operacional (ambiente único),
- risco e complexidade de HA e DR (agora e depois).

O projeto já assumiu uma fase piloto com até ~10 pousadas, sem HA no início e com evolução para HA quando necessário.

## Decisão
- A região padrão para todos os serviços de runtime e dados do Hotelly V2 será **GCP us-central1 (Iowa)**.
- Cloud SQL Postgres em us-central1 será a **fonte da verdade**.
- Serviços que devem ficar na mesma região por padrão: **Cloud Run, Cloud SQL, Cloud Tasks, Secret Manager**.
- **Sem HA no piloto** (configuração single-region / single instance), com plano explícito de upgrade para HA posteriormente.

## Drivers (por que esta decisão)
1) **Eficiência de custo e previsibilidade**
   - us-central1 tende a oferecer boa relação custo/benefício e maturidade de oferta.
2) **Simplicidade operacional (single region)**
   - Menos variação e menos moving parts durante execução solo/piloto.
3) **Coerência transacional**
   - Cloud Run e Cloud SQL na mesma região reduzem latência e risco em transações críticas.
4) **Escalonamento progressivo**
   - Permite começar simples e evoluir para HA quando houver tração/necessidade.
5) **Independência de canal**
   - WhatsApp/Stripe são integrações externas; o gargalo real é consistência e idempotência do core transacional, não “latência mínima”.

## Alternativas consideradas
### A1) Região no Brasil (ex.: southamerica-east1)
**Prós:**
- menor latência para usuários no Brasil (WhatsApp e admin),
- menor egress para integrações locais.
**Contras:**
- pode aumentar custo total em alguns componentes,
- potencialmente menos flexibilidade de oferta/quotas em certos serviços,
- risco de complexidade caso haja necessidade de multi-região cedo.

### A2) Multi-região desde o início
**Prós:**
- DR e disponibilidade superiores.
**Contras (inaceitável no piloto):**
- complexidade operacional muito maior,
- maior custo,
- risco de inconsistência e retrabalho em idempotência/replicação,
- distração do objetivo (MVP transacional rodando).

### A3) Europa/US East
**Prós:**
- opções de custo/latência alternativas.
**Contras:**
- sem benefício claro sobre us-central1 para o cenário atual.

**Escolha:** us-central1 por custo/simplicidade/maturidade, com upgrade planejado.

## Implicações e consequências
### Consequências positivas
- Infra mais simples para operar sozinho.
- Menor risco de “ajustes regionais” no meio da execução.
- Maior coerência entre runtime e banco (latência intra-região).

### Consequências negativas / riscos
- **Latência maior** para o Brasil em algumas interações (WhatsApp/admin).
  - Mitigação: pipeline assíncrono (Tasks), resposta rápida no webhook, UX tolerante a alguns segundos.
- **Conformidade/Residência de dados**
  - Se algum cliente exigir residência no Brasil, será necessária evolução (ver “Evoluções”).
- **Egress**
  - Atenção a custos de egress ao consumir integrações e eventualmente dashboards externos.

## Regras derivadas (guardrails)
1) **Co-localização**
   - Cloud Run e Cloud SQL devem permanecer na mesma região para reduzir latência e falhas em transações.
2) **Assíncrono por padrão**
   - Webhooks (WhatsApp/Stripe) devem enfileirar e retornar rápido; processamento pesado acontece em worker.
3) **Sem HA no piloto**
   - Não habilitar HA até haver gatilho claro (custo/complexidade).
4) **Backups e recuperação**
   - Configurar backups automáticos e plano de restore testado (runbook).
5) **Telemetria**
   - Monitorar latência end-to-end e backlog de tasks para detectar impacto de região.

## Gatilhos para reavaliar a decisão
Revisar a região quando ocorrer qualquer um:
1) **Demanda de residência de dados no Brasil** (contratual/compliance).
2) **Latência percebida impactando conversão** (métrica de funil e tempo de resposta).
3) **Crescimento de volume** que torne egress/custo relevantes.
4) **Requisitos de HA/SLA** formais em contrato.

## Evolução planejada (não-MVP)
- **Habilitar HA** no Cloud SQL quando houver mais de ~10 pousadas ativas ou sinais de criticidade operacional.
- **DR / backups testados**: executar restore simulado periodicamente (runbook).
- **Possível expansão regional** (Brasil) para:
  - serviços de borda (admin/UI) e/ou
  - clusters por região por tenant, se necessário.

## Checklist de aceitação
- [ ] `us-central1` definida como padrão em infra (IaC/terraform ou scripts).
- [ ] Cloud Run e Cloud SQL co-localizados em us-central1.
- [ ] Cloud Tasks na mesma região.
- [ ] Backups Cloud SQL configurados e restore documentado no runbook.
- [ ] Métricas de latência e backlog de tasks publicadas no dashboard.

## Referências
- Decisão base: GCP us-central1 (Iowa); Cloud SQL Postgres como fonte da verdade; piloto sem HA inicialmente e HA depois.
- Docs operacionais: CI/CD, observability, runbook.
