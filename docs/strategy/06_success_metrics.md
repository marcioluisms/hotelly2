# Hotelly V2 — Métricas de Sucesso (S6)

## Objetivo
Definir métricas objetivas para guiar execução, pilotagem e decisões de produto/engenharia.

**North Star:** Reservas **pagas e confirmadas** via WhatsApp, **sem overbooking**.

---

## Princípios
1) **Definição antes da coleta**: toda métrica tem definição, fórmula, fonte e frequência.
2) **Medição por eventos**: métricas devem ser derivadas de `outbox_events` (eventos de domínio) e `processed_events` (dedupe/retry). Evita “contagem por log”.
3) **Uma verdade para cada coisa**:
   - Dinheiro/Inventário: Postgres (transações críticas)
   - Eventos de fluxo: outbox append-only
4) **Guardrails são métricas**: “0 overbooking” e “0 duplicidade” são **SLOs**, não desejos.

---

## Escopo por fase
### Fase A — Execução (Sprints 0–2)
Foco: gates passando, fluxo E2E em staging e invariantes preservadas.

### Fase B — Piloto (até 10 pousadas, sem HA)
Foco: operação estável + aprendizado. Usuários cientes de falhas.

### Fase C — Pós-piloto
Foco: escala, HA, aumento de conversão, redução de custo por reserva.

---

## Definições canônicas (para não medir errado)
- **Conversa WhatsApp (conversation):** agrupamento lógico por hóspede + propriedade (definição do `conversation_key`).
- **Quote emitido:** evento `quote.presented` (quando existir) ou proxy: envio de opções ao hóspede.
- **Hold criado:** evento `hold.created` com noites persistidas.
- **Pagamento iniciado:** `payment.received` (ex.: checkout criado) ou `payment.created` (se separarem).
- **Pagamento confirmado:** `payment.succeeded` (após validação do provedor + registro durável).
- **Reserva confirmada:** `reservation.confirmed` (após converter hold com `inv_held--` e `inv_booked++`).
- **Overbooking:** qualquer estado em que `inv_booked + inv_held > inv_total` para alguma (property_id, room_type_id, date).

---

## Catálogo de métricas (com targets por fase)

### 1) Métrica North Star
**NS1 — Reservas pagas e confirmadas via WhatsApp (sem overbooking)**
- **Definição:** contagem de `reservation.confirmed` cuja origem é WhatsApp e com `payment.succeeded` associado.
- **Fórmula (semântica):**
  - `count(reservation.confirmed WHERE channel='whatsapp')` AND existe `payment.succeeded` ligado ao `hold_id`/`reservation_id`.
- **Fonte:** `outbox_events` (primário) + tabelas `payments/reservations` (auditoria).
- **Cadência:** diário (piloto), semanal (pós-piloto).
- **Meta:**
  - Fase A: demonstrar E2E em staging (>= 1 por execução controlada).
  - Fase B: tendência crescente semanal por pousada (meta inicial: 5–20/semana por pousada, ajustar após baseline real).
  - Fase C: otimização de conversão + custo.

**NS0 — Overbooking (SLO)**
- **Definição:** número de violações do invariante de inventário.
- **Fonte:** query em `ari_days` + checagem em transação (preferencial) + job diário.
- **Meta:** **0 em todas as fases.**
- **Alerta:** qualquer ocorrência = incidente P0 (stop-ship para novas features até corrigir).

---

### 2) Funil WhatsApp → Reserva (conversão e fricção)
**F1 — Conversas iniciadas**
- **Definição:** count de conversas novas (primeiro inbound do hóspede).
- **Fonte:** tabela `conversations` ou evento `conversation.opened`.
- **Meta (piloto):** baseline + crescimento controlado (sem meta dura sem baseline).

**F2 — Taxa de resposta inicial (TTFR)**
- **Definição:** tempo do primeiro inbound até a primeira resposta outbound do sistema.
- **Fonte:** eventos inbound/outbound (outbox de mensagens ou tabela de mensagens normalizadas).
- **Meta:**
  - Piloto: p50 < 30s; p95 < 2 min (assíncrono com Tasks).

**F3 — Taxa de Quote (quando aplicável)**
- **Definição:** % de conversas que recebem opções de disponibilidade/preço.
- **Fórmula:** `conversations_with_quote / conversations_started`.
- **Meta (piloto):** > 60% (ajustar conforme escopo do MVP).

**F4 — Taxa de criação de Hold**
- **Definição:** % de conversas com hold criado após quote.
- **Fórmula:** `holds_created / conversations_with_quote`.
- **Meta (piloto):** baseline; objetivo é identificar gargalo (UX, preço, disponibilidade).

**F5 — Pagamento iniciado**
- **Definição:** % de holds que geram checkout/link.
- **Fórmula:** `payments_initiated / holds_created`.
- **Meta (piloto):** > 70% (se fluxo estiver claro).

**F6 — Conversão Hold → Reserva (pagamento confirmado)**
- **Definição:** % de holds que viram reserva confirmada.
- **Fórmula:** `reservations_confirmed / holds_created`.
- **Meta (piloto):** baseline; objetivo é reduzir fricção e “pagamento após expiração”.

**F7 — Tempo de conversão (hold_created → reservation_confirmed)**
- **Meta (piloto):** p50 < 10 min; p95 < 60 min.

---

### 3) Confiabilidade de integrações e retry (SLO/SLA interno)
**R1 — Dedupe efetivo (webhook/task/message)**
- **Definição:** % de eventos reprocessados que viram no-op (não duplicam efeito).
- **Fonte:** `processed_events` (quantidade de hits duplicados) vs efeitos gerados.
- **Meta:** 100% de duplicados sem efeito colateral.

**R2 — ACK correto de webhooks (Stripe/WhatsApp)**
- **Definição:** 2xx só após receipt durável (processed_events e/ou task durável).
- **Proxy mensurável:** % de webhooks que, ao responder 2xx, têm `processed_events` registrado.
- **Meta:** 100%.

**R3 — Taxa de erro por etapa**
- **Definição:** erros por handler (inbound, quote, hold, payment, convert, expire).
- **Meta (piloto):** erro “hard” < 1% por etapa; “soft” (sem disponibilidade) é esperado.

**R4 — Backlog de Tasks**
- **Definição:** idade máxima da task mais antiga por fila.
- **Meta (piloto):** p95 < 2 min em filas críticas; alertar > 5 min.

**R5 — MTTR de incidentes (operacional)**
- **Definição:** tempo entre detecção e mitigação.
- **Meta (piloto):** < 4h (sem HA, foco é reduzir sofrimento).

---

### 4) Integridade de dados (incidentes silenciosos)
**D1 — Pagamentos sem reserva**
- **Definição:** pagamentos confirmados sem `reservation.confirmed` associado após janela (ex.: 15 min).
- **Meta:** ~0; qualquer aumento é sinal de bug/race.
- **Ação:** rodar playbook “payments_without_reservation” e reprocessar candidatos.

**D2 — Holds presos / vencidos ativos**
- **Definição:** holds `active` com `expires_at < now()`.
- **Meta:** 0 (job de expiração + idempotência).

**D3 — Inventário negativo / inconsistente**
- **Definição:** qualquer `inv_held < 0` ou `inv_booked < 0` ou violação do invariante.
- **Meta:** 0.

**D4 — Duplicidade proibida (constraints)**
- **Definição:** tentativas de violar UNIQUE em `reservations(property_id, hold_id)` e `payments(property_id, provider, provider_object_id)`.
- **Meta:** pode existir tentativa, mas **nunca** deve resultar em duplicidade. Contar como “evento de proteção”.

---

### 5) Segurança e privacidade (não-negociável)
**S1 — Incidentes de PII em logs**
- **Definição:** ocorrência de telefone/texto completo do hóspede ou payload bruto em log.
- **Meta:** 0.
- **Mecanismo:** lint (Gate G2) + auditoria periódica de amostra de logs.

**S2 — Rotas internas expostas**
- **Definição:** qualquer `/internal/*` acessível publicamente em prod.
- **Meta:** 0.
- **Mecanismo:** teste automático (Gate G2) + verificação manual no deploy.

---

### 6) Unit economics (medir mesmo com pricing aberto)
Mesmo com pricing em aberto, medir já (para não pilotar no escuro).

**U1 — GMV (Gross Merchandise Value) processado**
- **Definição:** soma dos valores de reservas confirmadas (antes de taxa).
- **Fonte:** `reservations.total_amount_cents` / Stripe.

**U2 — Custo variável por reserva**
- **Definição:** (custos de infra + provedores por período) / reservas confirmadas.
- **Meta (piloto):** entender baseline; depois impor teto.

**U3 — Take rate efetiva (quando houver fee)**
- **Definição:** receita Hotelly / GMV.
- **Meta:** pós-piloto.

---

### 7) Qualidade de IA (quando IA estiver ativa)
**A1 — Taxa de fallback determinístico**
- **Definição:** % de mensagens que não passam pelo modelo e seguem fluxo determinístico.
- **Uso:** controlar risco (quanto maior, menos “surpresas”).

**A2 — Taxa de “ação inválida” bloqueada**
- **Definição:** respostas do modelo fora do schema/guardrails (bloqueadas e reprocessadas).
- **Meta:** < 1% (ideal próximo de 0).

**A3 — Resolução sem humano**
- **Definição:** % de conversas que chegam em `reservation.confirmed` sem intervenção manual.

---

## Dashboards mínimos (o que você precisa enxergar todo dia)
1) **North Star + Funil:** NS1, F1–F7 por propriedade.
2) **Confiabilidade:** R1–R4 (com backlog e erro por etapa).
3) **Integridade:** D1–D4 (com lista de casos acionáveis).
4) **Segurança:** S1–S2.

---

## Alertas (stop-ship vs operacionais)
### Stop-ship (P0)
- NS0 (overbooking) > 0
- D3 (inventário negativo/inconsistente)
- S1 (PII em logs) detectado
- S2 (rota interna pública) detectado

### Operacionais (P1/P2)
- D1 (pagamento sem reserva) acima de 0 e crescendo
- D2 (holds vencidos ativos) > 0
- R4 backlog de tasks > 5 min
- R3 taxa de erro hard por etapa > 1%

---

## Cadência de revisão
- **Diário (piloto):** painel Confiabilidade + Integridade (R*, D*).
- **Semanal:** North Star + Funil (NS*, F*), com ações de produto/UX.
- **Mensal:** Unit economics (U*), com decisão de pricing.

---

## Responsáveis (mesmo trabalhando sozinho)
- **Owner de dados/definições:** você.
- **Owner de instrumentação:** você.
- **Owner de alertas/runbook:** você.

Regra: se uma métrica é importante, ela precisa ter (1) definição, (2) query/implementação, (3) alerta ou revisão periódica.
