# Runbook — Hotelly V2 (Operações)

> Documento operacional. Objetivo: manter o sistema funcional no piloto, com **zero overbooking**, **idempotência real**, e **resposta rápida a incidentes**.

## 1. Escopo

## Estado atual do repo (importante)
No momento, o serviço FastAPI expõe apenas `/health` e ainda não possui rotas implementadas para:
- `/webhooks/*`
- `/tasks/*`

Portanto, qualquer passo que mencione "reenfileirar task", "chamar handler /tasks/..." ou "endpoint interno"
deve ser tratado como **TARGET** até que as rotas/infra de Cloud Tasks estejam implementadas.

Este runbook cobre:

- Incidentes em **inventário/ARI**, **holds**, **pagamentos/Stripe**, **WhatsApp**, **Cloud Tasks**, **Cloud Run**, **Cloud SQL**.
- Rotinas operacionais (diárias/semanais) e ações de mitigação.
- Procedimentos de reprocessamento e reconciliação, priorizando **segurança transacional** e **não duplicidade**.

Fora de escopo: suporte ao cliente final (mensagens de atendimento), melhorias de produto, otimizações não urgentes.

---

## 2. Princípios (não negociáveis)

1) **Overbooking = SEV0.** Se houver qualquer evidência de inventário negativo, reserva duplicada, ou `inv_booked` incoerente: parar tudo e conter.
2) **Webhook não pode “mentir”.** Não retornar 2xx se não houve receipt durável (dedupe/outbox/task).
3) **Idempotência sempre.** Reprocessar só quando os dedupes estão em vigor (`processed_events`, `idempotency_keys`, uniques).
4) **Sem PII em logs.** Não logar payload bruto (WhatsApp/Stripe) nem texto de usuário.
5) **Mudança em produção só com rastreabilidade.** Toda correção deve virar commit/migração/registro.

---

## 3. Definições rápidas

- **correlation_id**: identificador para amarrar logs de webhook → task → transação.
- **property_id**: pousada.
- **hold_id**: bloqueio de inventário temporário.
- **provider_object_id**: id externo do provedor (Stripe `checkout.session.id`, evento do Stripe, message_id do WhatsApp).
- **processed_events**: dedupe de eventos externos/Tasks.
- **idempotency_keys**: dedupe de chamadas internas por chave.
- **outbox_events**: eventos append-only emitidos na mesma transação (rastreabilidade e reprocessamento).

---

## 4. Severidade e resposta

### SEV0 (stop-ship)
- Overbooking confirmado ou inventário negativo
- Reserva duplicada (mesmo hold ou mesmo pagamento)
- Stripe confirmado mas sistema “perde” reserva (sem trilha de reprocess)
- Vazamento de PII em logs
- Endpoint interno exposto publicamente

**Ação imediata (SEV0):**
1) **Conter**: pausar entrada (desabilitar webhook WhatsApp e/ou Stripe temporariamente ou apontar para “maintenance”).
2) **Preservar evidência**: capturar logs e métricas do intervalo.
3) **Mitigar**: corrigir o estado (com transação segura) e só então retomar.
4) **Postmortem curto**: causa raiz + fix definitivo.

### SEV1
- Backlog grande de tasks, erros 5xx sustentados, falha de webhook com retries sem convergir
- Holds presos aumentando (stuck holds) sem liberar inventário

### SEV2
- Erros intermitentes, degradação de latência, alertas de custo/DB

---

## 5. Checklist de triagem (primeiros 10 minutos)

1) **O que disparou?** (alerta, reclamação, dashboard)
2) **Impacto:** quantos properties afetados? inventário/pagamento?
3) **Último deploy:** houve revisão nova no Cloud Run?
4) **Cloud Tasks:** fila acumulando? quantas falhas/retries?
5) **Cloud SQL:** conexões saturadas? CPU/IO alto?
6) **Stripe/WhatsApp:** falha de assinatura, 5xx no endpoint, timeout?
7) **Correlacionar:** pegue um `correlation_id` (ou `hold_id`/`payment_id`) e siga o rastro.

---

## 6. Ferramentas e comandos (referência)

> Ajuste nomes de projeto/serviço/filas conforme seu `gcloud config` e padrões do repo.

### 6.1 Cloud Run
- Listar revisões / verificar status:
  - `gcloud run services describe <SERVICE> --region us-central1`
  - `gcloud run revisions list --service <SERVICE> --region us-central1`
- Rollback rápido (apontar tráfego para revisão anterior):
  - `gcloud run services update-traffic <SERVICE> --region us-central1 --to-revisions <REVISION>=100`

### 6.2 Logs (Cloud Logging)
- Filtrar por severity e correlation_id:
  - Ex.: `resource.type="cloud_run_revision" AND jsonPayload.correlation_id="<ID>"`

### 6.3 Cloud Tasks
- Ver filas:
  - `gcloud tasks queues list --location us-central1`
- Tamanho/estatísticas:
  - `gcloud tasks queues describe <QUEUE> --location us-central1`

### 6.4 Cloud SQL
- Conectar (para diagnóstico):
  - `gcloud sql connect <INSTANCE> --user=<USER> --database=<DB>`
- Ver instância:
  - `gcloud sql instances describe <INSTANCE>`

---

## 7. Playbooks (por sintoma)

### 7.1 Pagamento confirmado no Stripe, mas sem reserva (payments_without_reservation)

**Sintomas:**
- Cliente pagou, mas não recebeu confirmação.
- Registro de payment existe, reservation não.

**Causas comuns:**
- Webhook recebido mas task não foi enfileirada.
- Task falhou e ficou em retry.
- Convert falhou por hold expirado; sistema marcou para manual.

**Passos:**
1) Confirmar no Stripe o `checkout.session.id` e o evento associado.
2) Buscar `processed_events`:
   - Se **não existe**: falha de receipt (SEV1/SEV0 dependendo do volume).
3) Rodar SQL de diagnóstico (repo):
   - `docs/operations/sql/payments_without_reservation.sql`
4) Determinar a ação:
   - Se hold ainda **active** e dentro do prazo: **TARGET** — reprocessar convert via fila/endpoint interno quando `/tasks/*` existir.
   - Se hold **expired**: não criar reserva automaticamente. Aplicar política “pagamento após expiração” (manual/reacomodação/reembolso).

**Mitigação rápida:**
- **TARGET** — reenfileirar convert para um payment/hold específico (sempre idempotente) quando tasks existirem.
- Se falha recorrente: pausar webhook Stripe e corrigir receipt.

---

### 7.2 Holds presos (active com expires_at no passado)

**Sintomas:**
- `holds.active` crescendo.
- Inventário “some” (inv_held alto) sem conversão.

**Passos:**
1) Verificar backlog/falhas da fila de expire.
2) Rodar SQL:
   - `docs/operations/sql/find_stuck_holds.sql`
3) Para cada hold:
   - Confirmar que está `active` e `expires_at < now()`.
   - **TARGET** — enfileirar task de expire para o hold (quando tasks existirem).
4) Se tasks estiverem quebradas:
   - Executar um job manual de expire em lote (controlado, com limite) usando o mesmo código do worker.
5) Validar ARI pós-expiração.

**Mitigação:**
- Se a fila de expire estiver parada: reiniciar worker / revisar permissões / ajustar rate.

---

### 7.3 Falha de webhook Stripe (assinatura/5xx/timeouts)

**Sintomas:**
- Stripe mostra webhooks falhando e re-tentando.
- Aumenta “payment sem reservation”.

**Passos:**
1) Verificar se o secret de webhook no Secret Manager bate com o configurado no Stripe.
2) Checar logs do endpoint:
   - Erro de assinatura (400) → secret errado / payload alterado.
   - 5xx → erro interno (corrigir e deixar Stripe re-tentar).
3) Confirmar “receipt durável”:
   - Em sucesso, deve existir `processed_events` e/ou task enfileirada.
4) Se houver risco de duplicidade:
   - Garantir UNIQUEs e dedupe antes de reprocessar/replay.

**Mitigação:**
- Se instabilidade do serviço: rollback para revisão anterior.
- Se secret errado: corrigir secret e reprocessar eventos pendentes.

---

### 7.4 Falha WhatsApp inbound (mensagens não chegam / duplicam / fora de ordem)

**Sintomas:**
- Queda repentina de conversas novas.
- Duplicidade de mensagens gerando múltiplas ações.

**Passos:**
1) Verificar status do provedor (Meta/Evolution) e logs de webhook.
2) Confirmar dedupe:
   - message_id deve virar `processed_events(source='whatsapp', external_id=message_id)`.
3) Se duplicidade estiver passando:
   - Contenção: pausar inbound (responder 503) temporariamente.
   - Validar se UNIQUE de processed_events está aplicado.
4) Se message_id ausente/inconsistente no provedor:
   - Aplicar fallback determinístico (ex.: hash de campos + timestamp arredondado) **apenas como mitigação** e registrar issue.

---

### 7.5 Inventário inconsistente (ARI divergente de holds/reservations)

**Sintomas:**
- `inv_held` ou `inv_booked` não bate com fatos.
- Overbooking ou disponibilidade errada no quote.

**Ação:** tratar como SEV0 se houver overbooking.

**Passos:**
1) Rodar reconciliação:
   - `docs/operations/sql/reconcile_ari_vs_holds.sql`
2) Congelar mutações (se necessário):
   - pausar create_hold e convert temporariamente.
3) Identificar causa:
   - transação parcialmente aplicada (não deveria acontecer se atomicidade correta)
   - correção manual anterior sem rastreio
   - bug em expire/cancel/convert (ordem/WHERE/locks)
4) Corrigir estado:
   - Preferir re-execução idempotente de transação (expire/cancel/convert).
   - Ajuste direto em ARI só como último recurso, com registro e validação.
5) Validar:
   - `inv_total >= inv_booked + inv_held` em todas as noites afetadas
   - sem valores negativos
6) Postmortem: criar bug/patch com teste que reproduz.

---

### 7.6 Backlog alto de Cloud Tasks (fila não escoa)

**Sintomas:**
- `queue_depth` cresce.
- Latência de confirmação aumenta.

**Passos:**
1) Ver taxa de erro do worker e logs de failures.
2) Verificar limites:
   - rate, max concurrent dispatches, max attempts.
3) Verificar Cloud Run:
   - instâncias suficientes? CPU/mem? timeouts?
4) Mitigação:
   - aumentar capacidade (scale) temporariamente
   - reduzir trabalho no handler (sempre enfileirar e fazer pesado no worker)
5) Se há poison messages:
   - identificar padrão de falha, corrigir código, reprocessar.

---

### 7.7 Cloud SQL saturado (conexões/CPU/IO)

**Sintomas:**
- Erros de conexão/pool.
- Lentidão generalizada.

**Passos:**
1) Checar métricas da instância (CPU, connections, disk IO).
2) Checar pool do app (limites de conexões por instância).
3) Mitigação:
   - reduzir concorrência (Cloud Run max instances / tasks rate)
   - ajustar pool (menor) + aumentar instância do Cloud SQL se necessário
   - rollback se começou após deploy
4) Longo prazo:
   - índices faltando; queries sem filtro; N+1; falta de batch.

---

## 8. Reprocessamento seguro (reprocess_candidates)

**Quando usar:**
- Após correção de bug (receipt/task) para “pegar” eventos perdidos.

**Passos:**
1) Rodar:
   - `docs/operations/sql/reprocess_candidates.sql`
2) Reenfileirar em lotes pequenos (ex.: 50 por vez), monitorando erro/latência.
3) Validar dedupe:
   - nenhum efeito deve duplicar reserva/payment/hold.

---

## 9. Rotinas operacionais

### Diário (piloto)
- Ver `payments_without_reservation` (deve ser ~0)
- Ver `stuck_holds` (deve ser ~0)
- Ver backlog de tasks (deve voltar a ~0 após picos)
- Ver taxa de erro 5xx do webhook Stripe/WhatsApp
- Amostra de logs para garantir ausência de PII

### Semanal
- Revisar métricas do funil (WhatsApp → reserva)
- Revisar custo (Cloud Run/Tasks/SQL)
- Revisar índices e queries lentas
- Exercitar rollback (simulado) e reprocess em staging

---

## 10. Pós-incidente (sempre)

1) Linha do tempo (deploys, alertas, impacto).
2) Causa raiz (técnica e de processo).
3) Ação corretiva:
   - patch + teste que falhava antes
   - ajuste em gate/alerta/runbook
4) Ação preventiva:
   - reduzir complexidade, eliminar caminho duplicado, endurecer constraints

---

## Apêndice A — Artefatos úteis no repo

- SQL de operação:
  - `docs/operations/sql/reconcile_ari_vs_holds.sql`
  - `docs/operations/sql/find_stuck_holds.sql`
  - `docs/operations/sql/payments_without_reservation.sql`
  - `docs/operations/sql/reprocess_candidates.sql`

- Documentos relacionados:
  - `docs/strategy/06_success_metrics.md`
  - `docs/operations/04_observability.md`
  - `docs/operations/03_test_plan.md`

