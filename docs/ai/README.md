# IA — Contratos, Prompts e Golden Tests

## Objetivo

Manter a camada de IA **controlada** e **testável**:
- prompts versionados,
- schemas de saída estáveis,
- golden tests para evitar regressões.

**Regra:** IA não decide estado crítico. O core transacional é determinístico.

## Estrutura

```
docs/ai/
  prompts/
    01_routing_intent.md
  schemas/
    intent_output.json
  golden_tests/
    README.md
```

## Política de segurança (PII)

- Proibido enviar payload bruto de WhatsApp/Stripe ao modelo.
- Antes de chamar o modelo, redigir:
  - telefone, email, nomes completos, documentos, endereços
  - conteúdo integral de mensagens (se não for necessário)
- Permitir apenas:
  - ids internos (uuid)
  - datas (checkin/checkout)
  - contagens (guest_count)
  - intents e parâmetros operacionais

## Versão do schema

O schema de saída deve ser tratado como contrato:
- qualquer mudança é versionada (novo arquivo ou bump de `schema_version`).
- golden tests devem ser atualizados na mesma PR.

## Integração mínima esperada (MVP)

Uso principal no MVP:
- classificar intenção do usuário (roteamento):
  - cotar / reservar
  - pagar
  - cancelar
  - falar com humano

O modelo retorna JSON validável via `intent_output.json`.
