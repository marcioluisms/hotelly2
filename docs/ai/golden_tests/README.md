# Golden Tests — IA

## Objetivo

Garantir que mudanças em prompt/schema não quebrem o comportamento esperado.

## Formato sugerido

Criar um conjunto de casos (ex.: YAML/JSON) com:
- input redigido (sem PII)
- output esperado (JSON conforme schema)

Exemplos de casos:
- "quero reservar de 10 a 12 para 2 pessoas" -> `quote_request` com datas e guest_count
- "quero pagar agora" -> `checkout_request`
- "cancela minha reserva" -> `cancel_request`
- "quero falar com atendente" -> `human_handoff`
- mensagem ambígua -> `unknown`

## Regras

- Atualizar golden tests na mesma PR que mudar prompt/schema.
- Output deve ser validável pelo JSON Schema (`docs/ai/schemas/intent_output.json`).
