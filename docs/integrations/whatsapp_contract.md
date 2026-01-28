# Integração WhatsApp — Contrato (v0.2)

## Objetivo
Definir o contrato interno único para mensagens WhatsApp e as regras de **dedupe**, **retry**, **timeouts** e **PII** para o pipeline.

Provider inicial: **Evolution** (ver ADR-004).

> **Fonte de verdade para PII**: [ADR-006](../adr/ADR-006-conversation-identity-and-pii-policy.md)

## Princípios
- Pipeline único: **inbound (public) → normalize → dedupe/receipt → enqueue (task) → worker processa**.
- Endpoint público faz apenas **validação mínima + receipt durável + enqueue + escrita no vault**.
- Segurança: **NUNCA logar payload bruto, telefone, nome, texto integral, remote_jid**.
- Worker `handle-message` é **PII-free**: NÃO recebe nem persiste texto, telefone ou identificador enviável.
- Idempotência ponta a ponta: replays do provider e retries internos NÃO PODEM duplicar processamento.

---

## Endpoints

### Public Inbound (Evolution webhook)
- Método: `POST`
- Path: `/webhooks/whatsapp/evolution`

#### Responsabilidades do endpoint público
1. Validar payload (schema mínimo)
2. Extrair e validar `property_id`
3. Gerar `contact_hash` (HMAC-SHA256, conforme ADR-006)
4. Gravar receipt em `processed_events`
5. **Gravar no contact_refs vault**: `(property_id, "whatsapp", contact_hash) → remote_jid` (criptografado, TTL 1h)
6. Enfileirar task `handle-message` com payload PII-free

#### Semântica de resposta (ACK)
- **2xx** somente quando:
  1) payload passou validação mínima
  2) receipt/dedupe foi gravado (ou duplicata detectada)
  3) contact_refs vault foi atualizado
  4) task foi **enfileirada com sucesso** (ou detectada como "já existe" via `task_id` determinístico)
- **4xx** quando: payload inválido/irrecuperável (NÃO DEVE retriar)
- **5xx** quando: falha interna ao gravar receipt, vault ou ao enfileirar task (DEVE permitir retry do provider)

> Regra do MVP: NÃO É PERMITIDO ACK 2xx sem enqueue confirmado.

---

### Worker
- Handler: `/tasks/whatsapp/handle-message` (PII-free)
- Outbound sender: `/tasks/whatsapp/send-message` (acesso ao vault)

---

## Contrato interno: InboundMessage (payload do worker)

Objeto canônico passado para o worker `handle-message`. Este payload é **PII-free**.

```json
{
  "provider": "evolution",
  "message_id": "string (dedupe key)",
  "property_id": "string",
  "correlation_id": "string (gerado no public)",
  "contact_hash": "string (base64url sem padding, 32 chars)",
  "kind": "text | interactive | media | unknown",
  "received_at": "ISO8601 UTC"
}
```

### Campos permitidos

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `provider` | string | `"evolution"` (fixo para este provider) |
| `message_id` | string | ID único da mensagem (dedupe key, `external_id`) |
| `property_id` | string | ID da pousada (resolvido no normalize) |
| `correlation_id` | string | ID de correlação para tracing |
| `contact_hash` | string | `base64url_no_padding(HMAC_SHA256(CONTACT_HASH_SECRET, "{property_id}\|whatsapp\|{sender_id}"))[:32]` — ver ADR-006 |
| `kind` | string | Tipo da mensagem: `text`, `interactive`, `media`, `unknown` |
| `received_at` | string | Timestamp ISO8601 UTC |

### Campos PROIBIDOS no payload do worker

Os seguintes campos NÃO DEVEM estar presentes no payload do worker (conforme ADR-006):

| Campo | Motivo |
|-------|--------|
| `to_ref` / `remote_jid` / `wa_id` | PII: identificador enviável |
| `text` | PII: conteúdo da mensagem |
| `phone` / `sender_id` | PII: número de telefone |
| `payload` / `raw` | PII: dados brutos do webhook |
| `name` | PII: nome do contato |

---

## Fluxo Outbound (envio de respostas)

O worker NÃO TEM acesso a PII. Para enviar respostas, utiliza-se o **contact_refs vault** (ver ADR-006).

### Passo a passo

1. **Worker grava outbox** (PII-free):
   ```json
   {
     "event_type": "whatsapp.send_message",
     "property_id": "prop_123",
     "contact_hash": "abc123...",
     "response_template": "quote_ready",
     "response_data": {"price": 450.00},
     "correlation_id": "corr_xyz"
   }
   ```

2. **Sender lê outbox** e consulta vault:
   - Busca: `(property_id, "whatsapp", contact_hash)` → `remote_jid` (descriptografa)

3. **Sender envia via provider**:
   - Usa `remote_jid` para enviar mensagem
   - `remote_jid` NUNCA é logado

4. **Tratamento de vault expirado**:
   - Se entrada não existe (TTL expirado): registrar erro, NÃO enviar
   - Comportamento intencional: força re-interação do usuário

---

## contact_refs vault (resumo)

Definido em ADR-006. Características:

| Aspecto | Requisito |
|---------|-----------|
| Criptografia | AES-256-GCM, chave via Secret Manager (`CONTACT_REFS_KEY`) |
| TTL | Máximo 1 hora (padrão) |
| Escrita | Endpoint público (inbound) apenas |
| Leitura | Sender (outbound) apenas |
| Logging | `remote_jid` NUNCA pode ser logado |

---

## Receipt / Dedupe / Idempotência

### Receipt durável
- Tabela: `processed_events`
  - `source = 'whatsapp'`
  - `external_id = message_id`
- Chave de dedupe: UNIQUE `(source, external_id)`

### Regra de dedupe
- Se `(source, external_id)` já existe:
  - Tratar como **duplicata**
  - **NÃO reprocessar**
  - Ainda assim, garantir que o enqueue do handler seja idempotente (ver `task_id`)

---

## Enqueue idempotente (Cloud Tasks)

### Task handler
- `/tasks/whatsapp/handle-message`

### Task ID determinístico (obrigatório)
- `task_id = "whatsapp:" + message_id`

Comportamento:
- Se a criação da task retornar "already exists", considerar como sucesso e seguir para ACK 2xx.
- Se falhar por motivo transitório, retornar 5xx no public webhook.

---

## Retry e timeouts

### Inbound (public)
- Assumir retries do provider.
- Timeout curto: **3–5s** (receipt + vault + enqueue).

### Worker
- Usar Cloud Tasks com backoff padrão.
- Timeout de execução: **30–60s**.

---

## Logs e PII

### Permitido logar
- `correlation_id`, `provider`, `message_id`, `property_id`, `kind`, status, `duration_ms`
- `contact_hash` (é hash, não PII)
- Contadores/flags (ex.: `message_len`, `has_attachment`, `attempt`)

### PROIBIDO logar (conforme ADR-006)
- Payload bruto do provider
- Telefone, nome, `sender_id`
- `remote_jid`, `wa_id`, `to_ref` (identificadores enviáveis)
- `text` (conteúdo da mensagem)
- Attachments, media

---

## Retenção

| Recurso | Retenção | Notas |
|---------|----------|-------|
| `processed_events` | 30 dias | Conforme política de retenção operacional |
| `contact_refs vault` | 1 hora (TTL) | Limpeza automática; minimiza exposição de PII |
| `outbox_events` | 30 dias | Auditoria de mensagens enviadas |

> **Nota**: O sistema NÃO persiste `text` (conteúdo de mensagem). Se futuramente necessário para IA/analytics, DEVE ser tratado em componente isolado com criptografia e retenção explícita.

---

## Erros
- **4xx**: schema inválido, provider incompatível, campos ausentes
- **5xx**: falha de DB/receipt, falha no vault, falha no enqueue, falha de dependência interna
