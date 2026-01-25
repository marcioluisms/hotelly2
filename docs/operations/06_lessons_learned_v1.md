# Lições Aprendidas da V1 e Controles Preventivos na V2

## Objetivo
Transformar erros reais da V1 em **controles** (regras, gates e critérios de aceite) para a V2.

Este documento é **normativo**: qualquer item marcado como **MUST** vira requisito de implementação e de revisão.

---

## Principais erros da V1 (o que não pode se repetir)
1) **Requisito “travado” que não foi implementado** (ex.: outbox/eventos append-only).  
2) **Idempotência parcial**: header exigido, mas não persistido; webhook sem dedupe formal; reprocessamento inseguro.  
3) **Integração WhatsApp inconsistente** (múltiplas “fontes de verdade”) + logs com payload/PII.  
4) **Stripe com semântica errada**: expiração do checkout incoerente com o hold; webhook respondendo 2xx em falha interna (mata retry).  
5) **Pricing não determinístico vs especificação** (PACKAGE, FIXED vs BPS) causando divergência de preço/breakdown.  
6) **Rota interna exposta** (seed/internal em router público) e higiene ruim de repo (artefatos).  
7) **Plano de testes não seguido** (e testes quebrados).  

Esses pontos estão descritos no postmortem anexado. 

---

## Controles preventivos V2 (MUST)

### C1. “Não-negociáveis” precisam de prova objetiva
**MUST:** Todo requisito declarado como non-negotiable (ex.: idempotência, outbox, no-overbooking) deve ter:
- artefato de implementação (tabela/endpoint/job)
- teste mínimo
- evidência em CI

**Regra prática:** se está na documentação como “travado”, a story só fecha com **prova executável**.

### C2. Outbox é obrigatório (auditoria + reprocessamento + analytics)
**MUST:** Toda transação crítica gera evento em outbox **append-only** (Postgres):
- `hold.created`
- `hold.expired`
- `hold.cancelled`
- `payment.received` / `payment.succeeded` / `payment.failed`
- `reservation.confirmed`

**MUST:** o ACK de webhook/evento externo só pode retornar 2xx após:
- registrar `processed_events` (dedupe) **e**
- persistir o efeito (ou enfileirar de forma durável)

### C3. Idempotência formal em três camadas
**MUST (API):** endpoints mutantes exigem `Idempotency-Key` e persistem em `idempotency_keys`.

**MUST (Webhooks/Tasks):** qualquer handler com retry externo usa `processed_events(source, external_id)`.

**MUST (DB Constraints):** duplicidade deve ser impossível por constraint:
- `reservations(property_id, hold_id)` UNIQUE
- `payments(property_id, provider, provider_object_id)` UNIQUE

### C4. WhatsApp: uma arquitetura, um caminho, zero payload em log
**MUST:** normalização inbound única (Meta/Evolution → `InboundMessage`).

**MUST:** dedupe por `message_id` antes de qualquer efeito colateral.

**MUST:** logs nunca contêm payload bruto nem texto completo do hóspede.
- telefone: mascarado
- conteúdo: truncado/redigido

**MUST:** não pode existir “3 fontes de verdade” (service legado + handlers paralelos + provider). Um único pipeline.

### C5. Stripe: coerência de expiração + semântica correta de ACK
**MUST:** `checkout.session` expira **no máximo** em `hold.expires_at` (nunca antes por padrão).

**MUST:** webhook Stripe
- valida assinatura
- registra `processed_events`
- enfileira task de conversão ou converte dentro do worker
- retorna 2xx somente após receipt durável

**MUST:** se falhar processamento interno, retornar **5xx** (para retry) ou marcar receipt e processar assíncrono.

### C6. Pricing: determinismo testado (golden tests)
**MUST:** regras de PACKAGE (ARRIVAL vs OVERLAP) e FIXED/BPS têm:
- implementação determinística
- **golden tests** (inputs fixos → breakdown fixo)

**MUST:** qualquer alteração em pricing exige rodar suíte de regressão de pricing.

### C7. Rotas internas nunca podem ser públicas
**MUST:** `/internal/*` não pode estar montado sob `/v1/*`.

**MUST:** seed/admin interno
- só em `dev`/`staging` **ou** protegido por OIDC
- em `prod`, desabilitado por default

### C8. Test Plan é gate, não “documento bonito”
**MUST:** CI falha se a suíte mínima não rodar.

Suíte mínima V2 (obrigatória):
- concorrência create_hold na última unidade (1 sucesso / N-1 falhas)
- idempotência de webhook Stripe (mesmo evento 2x)
- corrida expire vs convert (sem inventário negativo)
- pricing: PACKAGE + FIXED/BPS (golden)

### C9. Higiene de repo e build
**MUST:** bloquear artefatos (`__pycache__`, `.pyc`, `.env`, dumps) via `.gitignore`.

**MUST:** linters/pre-commit simples:
- banir `print(`
- banir log de `payload`, `body`, `request.json()` em rotas externas

---

## Anti-patterns (proibidos)
- **“Efeito colateral antes do dedupe”**: criar hold/reserva antes de registrar receipt/event.
- **“2xx em falha interna”** para webhooks: você perde retry do provedor.
- **“Header obrigatório mas ignorado”**: cria falsa sensação de idempotência.
- **“Hardcode de property_id / pousada fixa”**: onboarding vira gambiarra.
- **“Conversa com múltiplos caminhos”**: bugs fantasmas e impossibilidade de depuração.

---

## Checklist de revisão (curto e objetivo)

### Mudança em transação crítica (hold/convert/cancel/expire)
- [ ] Tem `processed_events` (se for task/webhook)?
- [ ] Tem `Idempotency-Key` persistido (se for endpoint)?
- [ ] Tem constraints de unicidade relevantes?
- [ ] Tem teste de concorrência/race?
- [ ] Gera outbox event?

### Mudança em integração externa (WhatsApp/Stripe)
- [ ] Sem payload em log
- [ ] ACK sem 2xx em falha interna
- [ ] Dedupe antes de efeitos
- [ ] Metadata canônica (property_id, hold_id, conversation_id, currency, amount)

### Mudança em pricing
- [ ] Golden tests atualizados
- [ ] Determinismo preservado

---

## Inserções sugeridas na documentação V2
1) **docs/operations/03_test_plan.md**: adicionar a suíte mínima (concorrência/idempotência/race/pricing).
2) **docs/operations/04_observability.md**: política de redação de PII + exemplos de logs permitidos.
3) **docs/operations/05_runbook.md**: playbooks “pagamento sem reserva”, “hold preso”, “replay webhook”, “reprocess candidates”.
4) **docs/integrations/stripe_contract.md**: regra de expiração do checkout alinhada ao hold + ACK semantics.
5) **docs/integrations/whatsapp_contract.md**: normalização inbound única + dedupe obrigatório.
6) **docs/architecture/01_reference_architecture.md**: incluir outbox como componente explícito.

---

## Anexo: mapeamento direto V1 → V2
- Outbox faltando → **C2** (outbox append-only + prova em CI)
- Idempotência incompleta → **C3** (API/webhook/constraints)
- WhatsApp inconsistente + PII em logs → **C4**
- Stripe expiry/ACK errado → **C5**
- Pricing divergente → **C6**
- Seed exposto → **C7**
- Test plan ignorado → **C8**
- Higiene de repo → **C9**

