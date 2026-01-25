# ADR-002 — Modelo de IA: Gemini 2.5 Flash para roteamento e extração (MVP)
**Status:** Accepted  
**Data:** 2026-01-25  
**Decisores:** Produto/Engenharia (Hotelly V2)

## Contexto
O Hotelly V2 precisa interpretar mensagens do WhatsApp para conduzir o usuário por um fluxo objetivo:
1) entender intenção (ex.: consultar disponibilidade, confirmar datas, cancelar),
2) extrair slots mínimos (check-in, check-out, hóspedes, preferências),
3) acionar ações **determinísticas** no core transacional (quote/hold/pagamento/reserva),
4) responder com mensagens padronizadas e seguras.

Na V1, problemas como divergência de contratos, idempotência parcial e semântica errada em integrações mostraram que **IA não pode ser fonte da verdade para operações críticas**. O core transacional precisa ser sólido e determinístico; IA deve ser um componente auxiliar e substituível.

## Decisão
- Usar **Gemini 2.5 Flash** como modelo padrão de IA no MVP para:
  - **roteamento de intenção** (classificação),
  - **extração de slots** (datas, número de pessoas, preferências),
  - **normalização de linguagem** (ex.: reformular pergunta para o usuário com clareza),
  - **respostas de apoio** em casos não críticos (FAQ curto, esclarecimentos).

- **Proibido** usar IA para:
  - decisões de inventário (ARI),
  - criação/expiração/cancelamento/conversão de hold,
  - cálculo final de preço (pricing) e regras de negócio,
  - confirmação de pagamento/reserva,
  - qualquer ação que altere estado transacional sem validação determinística.

- A saída do modelo deve obedecer a um **schema estrito** e versionado; qualquer violação cai em **fallback determinístico**.

## Drivers (por que esta decisão)
- **Custo/latência**: modelo “flash” é adequado para tempo de resposta baixo em WhatsApp e para execução em escala.
- **Robustez**: classificação/extração são tarefas onde IA agrega valor com risco controlável.
- **Substituibilidade**: escolher um modelo não deve contaminar o domínio transacional; a IA é “pluggable”.
- **Operação segura**: reduz risco de “alucinação” impactar dinheiro/inventário.

## Alternativas consideradas
### A1) Sem IA (apenas regras/regex/menus)
**Prós:** previsibilidade total; menor risco.  
**Contras:** pior UX; maior esforço para cobrir linguagem natural; menor conversão.

### A2) Modelo maior (mais caro) para “fazer tudo”
**Prós:** potencial de melhor entendimento.  
**Contras (crítico):** custo e latência; maior superfície de risco; incentiva colocar regra de negócio no modelo.

### A3) IA para ações críticas com “confiança alta”
**Prós:** menos código.  
**Contras (inaceitável):** “confiança” não é garantia; risco de erro financeiro e overbooking.

**Escolha:** Gemini 2.5 Flash para roteamento/extração, mantendo o core determinístico.

## Contrato de integração (não negociável)
### Entrada para IA (sempre redigida)
- Texto do usuário (mensagem) **redigido** quando necessário.
- Contexto mínimo do estado da conversa (state machine):
  - estado atual,
  - slots já coletados,
  - última pergunta feita ao usuário,
  - opções válidas (ex.: “precisa de check-in/check-out”).
- **Nunca enviar** payload bruto de webhooks, tokens, segredos ou dados sensíveis não essenciais.

### Saída da IA (schema estrito)
A IA deve retornar JSON com:
- `intent`: enum (ex.: `CHECK_AVAILABILITY`, `PROVIDE_DATES`, `CONFIRM_BOOKING`, `CANCEL`, `UNKNOWN`)
- `slots`: objeto (ex.: `check_in`, `check_out`, `guests`, `room_type_pref`, etc.)
- `confidence`: 0–1 (informativo; **não** é critério único de execução)
- `reply_suggestion`: texto curto opcional (apenas para mensagens não críticas)

**Regra:** se JSON inválido, enum desconhecido, ou slots incoerentes → **fallback**.

## Guardrails de segurança e privacidade
1) **Redaction obrigatória**
   - mascarar/omitir: telefone, e-mail, documento, dados de cartão, endereços completos, tokens.
2) **Sem logging de prompts/respostas brutas**
   - logs apenas com metadados (intent, flags, timings, erro/sucesso).
3) **Least privilege**
   - a camada de IA não acessa diretamente o banco transacional; ela só sugere `intent/slots`.
4) **Separação de ambientes**
   - chaves e endpoints por dev/staging/prod via Secret Manager.

## Fallback determinístico (para garantir operação)
- Se `intent=UNKNOWN` ou `confidence` abaixo de limiar (ex.: <0.6), ou slots incompletos:
  - conduzir por perguntas determinísticas (ex.: “Qual a data de check-in?”).
- Se usuário fornece datas em formato inválido:
  - pedir confirmação com formato esperado.
- Se a IA sugerir ação crítica:
  - ignorar e seguir fluxo determinístico (state machine).

## Observabilidade e métricas
Registrar (sem PII):
- taxa de `UNKNOWN`,
- taxa de fallback,
- latência da chamada ao modelo,
- erros por tipo (timeout, schema inválido),
- distribuição de intents,
- taxa de conversão do funil com/sem fallback (para avaliar valor real da IA).

Alertas sugeridos (operacionais):
- aumento de `UNKNOWN` ou fallback acima de limite (ex.: +50% vs baseline),
- aumento de latência média acima de limite,
- erros contínuos de provider/modelo.

## Testes e qualidade (gates)
Mínimos obrigatórios:
- **Golden tests** de intents/slots (dataset versionado com casos reais e bordas):
  - datas “amanhã”, “fim de semana”, “31/01”, “31-01”, “31 janeiro”
  - mensagens com ruído (emoji, abreviações)
- Teste de schema: resposta sempre JSON válido conforme contrato.
- Teste de fallback: em respostas inválidas, o sistema pergunta e não executa ação crítica.

## Evolução planejada
- Se o custo/latência ou taxa de fallback ficar ruim:
  - ajustar prompt/guardrails,
  - melhorar normalização (pré-processamento determinístico),
  - considerar troca de modelo (sem impactar domínio).
- IA para geração de texto (templates) pode ser expandida, mantendo logs redigidos e conteúdo não crítico.

## Checklist de aceitação
- [ ] Chaves e endpoint do modelo em Secret Manager por ambiente.
- [ ] Prompt(s) e schema versionados em `docs/ai/` (ou equivalente).
- [ ] Redaction implementada antes de chamar IA.
- [ ] Sem logging de prompt/resposta bruta.
- [ ] Fallback determinístico implementado e testado.
- [ ] Golden tests de intent/slots no CI.

## Referências
- Docs operacionais: test plan, observability, runbook, quality gates.
- ADR-001 (Cloud SQL) e guia de transações críticas: core determinístico.
