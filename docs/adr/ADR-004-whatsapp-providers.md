# ADR-004 — Provedores de WhatsApp (Evolution primeiro; Meta Cloud API sob demanda)
**Status:** Accepted (faseado)  
**Data:** 2026-01-25  
**Decisores:** Produto/Engenharia (Hotelly V2)  
**Contexto do projeto:** Hotelly V2 (pousadas)  

## Contexto
O Hotelly V2 depende do WhatsApp como canal primário para cotação, criação de hold, envio de link de pagamento e confirmação de reserva.

A V1 mostrou um padrão claro de falha quando existiam múltiplos caminhos e regras duplicadas (ex.: mais de um pipeline de WhatsApp, divergência de contratos, semântica errada de ACK, idempotência incompleta). Na V2, isso precisa ser evitado por desenho.

No piloto/demonstração inicial, existe um requisito operacional prático: a **pousada modelo** já usará **Evolution**, e o objetivo imediato é reduzir tempo até uma demo funcional. Ao mesmo tempo, precisamos preservar a capacidade de adicionar **Meta WhatsApp Cloud API** com mínimo atrito, sem refatorar o core transacional.

## Decisão (faseada)
### Fase 0 — Arquitetura obrigatória (desde o primeiro commit)
Independentemente do provedor implementado primeiro, a V2 terá **um único pipeline interno**, com:
- Contrato interno único: `InboundMessage`
- Dedupe/receipt antes de efeitos colaterais
- Enfileiramento assíncrono via Cloud Tasks
- Sender outbound com retry/backoff via Tasks
- Observabilidade padronizada (correlation_id + métricas)

### Fase 1 — Implementação inicial (MVP/Demo)
- Implementar **somente o adapter Evolution** para inbound/outbound.
- Propriedades terão `whatsapp_provider = evolution` (e as credenciais correspondentes) configurado por dados.
- Nenhum caminho alternativo “rápido” fora do pipeline.

### Fase 2 — Implementação sob demanda (padrão de produção)
- Implementar **Meta WhatsApp Cloud API** como segundo adapter, mantendo o mesmo pipeline e contrato.
- Em produção/piloto com múltiplos clientes, **Meta tende a ser o padrão recomendado**, por estabilidade e menor risco de bloqueio.

## Drivers (por que essa decisão)
- **Time-to-demo (técnico-operacional):** Evolution já está alinhado com a pousada modelo; reduz o tempo para demonstrar o sistema sem comprometer o core.
- **Redução de risco arquitetural:** o pipeline único evita repetir os erros da V1; Meta pode entrar depois sem refatorar o motor transacional.
- **Multi-tenant desde o início:** a escolha de provedor é por propriedade (data-driven), evitando hardcodes.
- **Prontidão para produção:** Meta permanece como alvo natural para escala/piloto real quando a demanda justificar.

## Critérios técnicos para iniciar a Fase 2 (Meta)
A Fase 2 deve ser iniciada quando ocorrer **qualquer** um dos gatilhos abaixo (objetivos e verificáveis):
1) **Multi-propriedade real:** onboarding da 2ª pousada operando o canal (não apenas seed).
2) **Confiabilidade:** taxa de falha de entrega/sessão/instabilidade no Evolution acima de um limite (ex.: >1% de mensagens outbound falhando por dia por 3 dias) ou incidentes recorrentes.
3) **Risco operacional:** qualquer evento de bloqueio/banimento, instabilidade prolongada ou necessidade de suporte que “se repete” e aumenta custo.
4) **Necessidade de funcionalidades oficiais:** templates oficiais/mensageria transacional, compliance/requisitos contratuais do cliente.
5) **Requisito comercial com SLA:** cliente exige canal oficial/suporte formal.

> Observação: Evolution pode continuar existindo como opção opt-in; Meta passa a ser o “caminho recomendado” em implantação padrão.

## Alternativas consideradas
### A1) Somente Meta desde o início
**Prós:** menor risco legal/operacional; estabilidade e suporte melhores.  
**Contras:** pode atrasar demo/piloto inicial por setup e dependências externas.

### A2) Somente Evolution para sempre
**Prós:** setup rápido em alguns cenários.  
**Contras (crítico):** risco maior de bloqueio/instabilidade e aumento do custo operacional; menor previsibilidade.

### A3) Evoluir com pipelines separados (um por provedor)
**Prós:** nenhum.  
**Contras (inaceitável):** repete V1; divergência de lógica, dedupe e ACK; risco de bugs graves em dinheiro/inventário.

**Escolha:** Fase 1 com Evolution, mantendo a arquitetura (pipeline único) que permite adicionar Meta sem retrabalho.

## Arquitetura obrigatória (pipeline único)
### Inbound
`provider webhook → adapter.parse_inbound() → normalize(InboundMessage) → dedupe/receipt → enqueue (Cloud Tasks) → worker.process_message()`

### Outbound
`internal outbound command → enqueue (Cloud Tasks) → worker.send_message() → adapter.send_outbound() → provider API`

## Requisitos não-negociáveis (para qualquer provedor)
1) **Contrato interno único (`InboundMessage`)**
   - Campos mínimos: `provider`, `property_id` (resolvido), `provider_message_id`, `from`, `timestamp`, `type`, `payload_redacted`, `correlation_id`.
2) **Dedupe/Idempotência**
   - Dedupe no ingest (antes de efeitos colaterais): `processed_events(source='whatsapp', external_id=provider_message_id)` (ou equivalente).
   - Outbound com idempotency key interna para retries (evitar spam).
3) **ACK correto no webhook**
   - Nunca responder 2xx se não houve receipt/enfileiramento durável.
   - Estratégia padrão: “receipt durável primeiro, 2xx depois”.
4) **Segurança**
   - Nunca logar payload bruto; redaction obrigatória.
   - Segredos somente em Secret Manager; nunca em commits.
   - Isolamento por propriedade (multi-tenant).
5) **Observabilidade**
   - correlation_id em todo o fluxo.
   - Métricas mínimas: `inbound_received`, `inbound_deduped`, `inbound_enqueued`, `outbound_sent`, `outbound_failed`, `provider_errors`, com labels `provider` e `property_id`.

## Notas de implementação (padrão)
### Resolução de `property_id`
- **Evolution:** mapear por `instance_id` configurado por propriedade.
- **Meta:** mapear por `phone_number_id` e/ou `business_account_id` configurado por propriedade.

### Camada de adapters (exemplo)
- `whatsapp/providers/evolution_adapter.py`
- `whatsapp/providers/meta_adapter.py`
- Interfaces: `parse_inbound()`, `send_outbound()`

### Failover
- **Sem failover automático** entre provedores no MVP (evita duplicidade e comportamento inesperado).
- Troca de provedor é operação manual (com runbook e checklist).

## Operação e suporte (runbook)
- Incidentes esperados:
  - mensagens duplicadas (retries), mensagens fora de ordem, webhooks atrasados.
  - Evolution: instância offline, sessão expirada, bloqueio.
- Ações:
  - reprocess de mensagens por correlation_id/event_id (sem reexecutar efeitos colaterais).
  - checagem de fila Tasks e taxa de falhas por provedor.
  - procedimento de troca de provedor por propriedade (manual).

## Checklist de aceitação (para fechar a decisão em código)
- [ ] Pipeline único com contrato `InboundMessage` aplicado.
- [ ] Dedupe antes de efeitos colaterais (replay do mesmo `provider_message_id` 10x → 1 processamento).
- [ ] Webhook ACK durável (receipt/enqueue antes do 2xx).
- [ ] Sem payload bruto em logs (lint/gate).
- [ ] Credenciais por propriedade via Secret Manager.
- [ ] Métricas por provedor e por propriedade.

## Referências
- Decisão base do projeto: pagamentos via Stripe; WhatsApp com opção de provedor por cliente.
- Docs operacionais: quality gates, observability, runbook (Hotelly V2).
