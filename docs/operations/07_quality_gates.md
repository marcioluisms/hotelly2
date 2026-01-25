# Quality Gates (CI) — Hotelly V2

## Objetivo
Reduzir burocracia substituindo revisão subjetiva por gates objetivos. Se um gate falha, a story não fecha.

---

## Gate G0 — Build & Startup
- `python -m compileall -q src` (ou raiz do projeto)
- Build Docker
- App sobe e responde `/health`

## Gate G1 — Migrações e Schema
- Rodar `migrate up` em banco vazio
- Rodar `migrate up` novamente (idempotente)
- Validar constraints críticas existem (script/SQL):
  - UNIQUE `processed_events(source, external_id)`
  - UNIQUE `reservations(property_id, hold_id)`
  - UNIQUE `payments(property_id, provider, provider_object_id)`

## Gate G2 — Segurança/PII (lint simples)
Falhar CI se:
- existir `print(` em código de produção
- existir log de variáveis típicas de payload: `payload`, `body`, `request.json`, `webhook` sem redação
- rotas `/internal/*` estiverem montadas no router público (teste de introspecção)

## Gate G3 — Idempotência e Retry
- Teste: mesmo webhook Stripe 2x → 1 efeito
- Teste: mesma task id 2x → no-op
- Teste: `Idempotency-Key` repetida → mesma resposta, sem duplicidade

## Gate G4 — Concorrência (No Overbooking)
- Teste concorrente (20 threads/processos): última unidade de inventário
  - esperado: 1 hold criado, 19 falhas com erro limpo

## Gate G5 — Race Expire vs Convert
- Simular convert e expire competindo
  - esperado: sem inventário negativo
  - esperado: no máximo 1 reserva

## Gate G6 — Pricing Determinístico
- Golden tests para:
  - BPS
  - FIXED
  - PACKAGE ARRIVAL
  - PACKAGE OVERLAP

---

## Regras de aplicação
- Gates G3–G5 são obrigatórios para qualquer mudança em transações críticas.
- Gates G6 é obrigatório para qualquer mudança em pricing.
- Gates G2 é obrigatório sempre.

