# Integração WhatsApp — Contrato (v0.1)

## Objetivo
Definir o contrato interno único para mensagens WhatsApp e as regras de **dedupe**, **retry**, **timeouts** e **PII** para o pipeline.

Provider inicial: **Evolution** (ver ADR-004).

## Princípios
- Pipeline único: **inbound (public) → normalize → dedupe/receipt → enqueue (task) → worker processa**.
- Endpoint público faz apenas **validação mínima + receipt durável + enqueue**.
- Segurança: **nunca logar payload bruto, telefone, nome, texto integral**.
- Idempotência ponta a ponta: replays do provider e retries internos não podem duplicar processamento.

---

## Endpoints

### Public Inbound (Evolution webhook)
- Método: `POST`
- Path: `/webhooks/whatsapp/evolution`

#### Semântica de resposta (ACK)
- **2xx** somente quando:
  1) payload passou validação mínima
  2) receipt/dedupe foi gravado (ou duplicata detectada)
  3) task foi **enfileirada com sucesso** (ou detectada como “já existe” via `task_id` determinístico)
- **4xx** quando: payload inválido/irrecuperável (não deve retriar)
- **5xx** quando: falha interna ao gravar receipt ou ao enfileirar task (para permitir retry do provider)

> Regra do MVP: não é permitido ACK 2xx sem enqueue confirmado.

---

### Worker
- Handler: `/tasks/whatsapp/handle-message`
- Outbound sender: `/tasks/whatsapp/send-message`

---

## Contrato interno (InboundMessage)

Objeto canônico (persistível/serializável) passado para o worker.

Campos:
- `provider`: `"evolution"`
- `message_id`: `string` (dedupe key principal; `external_id`)
- `property_id`: `string` (resolvido no normalize)
- `correlation_id`: `string` (gerado no public e propagado)
- `from_hash`: `string` (**HMAC-SHA256** do telefone E.164 com secret; armazenar base64url truncado)
- `to_ref`: `string` (identificador **enviável** provider-specific: `chat_id/remote_jid/wa_id` etc.)
  - **Pode persistir**, mas **nunca logar**
  - Criptografia app-layer é **MVP+** (no MVP contar com “at rest” do Postgres + acesso restrito)
- `message_type`: `"text" | "interactive" | "media" | "unknown"`
  - MVP: processar apenas `"text"`
- `text`: `string` (conteúdo para o produto/IA)
  - **Não logar**
  - Limite de tamanho (ex.: 2.000 chars)
- `text_redacted`: `string` (derivado para logs/telemetria; máx. 160 chars; mascarar sequências numéricas longas/URLs)
- `timestamp`: ISO8601 (do provider; se ausente, usar server time)
- `raw_ref`: opcional (ponteiro/ID para reprocesso controlado; **não** é payload bruto)

---

## Receipt / Dedupe / Idempotência

### Receipt durável
- Tabela: `processed_events`
  - `source = 'whatsapp'`
  - `external_id = message_id`
- Chave de dedupe: UNIQUE `(source, external_id)`

### Regra de dedupe
- Se `(source, external_id)` já existe:
  - tratar como **duplicata**
  - **não reprocessar**
  - ainda assim, garantir que o enqueue do handler seja idempotente (ver `task_id`)

---

## Enqueue idempotente (Cloud Tasks)

### Task handler
- `/tasks/whatsapp/handle-message`

### Task ID determinístico (obrigatório)
- `task_id = "whatsapp:" + message_id`

Comportamento:
- Se a criação da task retornar “already exists”, considerar como sucesso e seguir para ACK 2xx.
- Se falhar por motivo transitório, retornar 5xx no public webhook.

---

## Retry e timeouts

### Inbound (public)
- assumir retries do provider.
- timeout curto: **3–5s**, apenas receipt + enqueue.

### Worker
- usar Cloud Tasks com backoff padrão.
- timeout de execução: **30–60s**.

---

## Logs e PII

### Permitido logar
- `correlation_id`, `provider`, `message_id`, `property_id`, `message_type`, status, `duration_ms`
- hashes (ex.: `from_hash`)
- contadores/flags (ex.: `text_len`, `has_url`, `attempt`)

### Proibido logar
- payload bruto do provider
- telefone, nome
- `text` integral
- attachments

---

## Retenção (MVP)
- `text` (se persistido) deve ter política de retenção explícita:
  - recomendado: **60–90 dias** (ajustável)
- `processed_events`: retenção operacional (ex.: **90 dias**) ou conforme necessidade de auditoria.

---

## Erros
- **4xx**: schema inválido, provider incompatível, campos ausentes
- **5xx**: falha de DB/receipt, falha no enqueue, falha de dependência interna
