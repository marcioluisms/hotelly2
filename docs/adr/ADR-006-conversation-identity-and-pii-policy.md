# ADR-006: Conversation identity and PII policy

## Status
Accepted

## Context
- Precisamos identificar conversas de WhatsApp por pousada (property) sem armazenar PII no pipeline de processamento.
- O sistema terá múltiplas integrações e precisa de dedupe/idempotência em webhook e worker.
- Logs NÃO PODEM conter payload/request/body/telefone/texto.
- Respostas outbound precisam resolver o destinatário (remote_jid) sem expor PII ao worker.

## Decision

### Definição de PII (para este sistema)
Os seguintes dados são classificados como PII e DEVEM ser tratados conforme as regras abaixo:
- Número de telefone (E.164 ou qualquer formato)
- Nome do contato
- Identificador enviável do provider (`remote_jid`, `wa_id`, `chat_id`)
- Conteúdo de mensagem (`text`, `payload`)
- Payload bruto do webhook
- Tokens, headers de autenticação

### Conversation key (upsert)
- conversation_key = (property_id, channel="whatsapp", contact_hash)
- Constraint: UNIQUE(property_id, channel, contact_hash)

### contact_hash generation
- DEVE usar HMAC-SHA256 (não SHA simples) para reduzir risco de brute force em identificadores como telefone.
- `contact_hash = base64url_no_padding(HMAC_SHA256(CONTACT_HASH_SECRET, f"{property_id}|whatsapp|{sender_id}"))`
- Truncar a string base64url (sem padding) para exatamente **32 caracteres** (não bytes, não hex).
- `sender_id` é o identificador do remetente retornado pelo provider (ex.: `remoteJid` do Evolution); não necessariamente E.164.
- CONTACT_HASH_SECRET é segredo (env/Secret Manager). NUNCA versionar valores.

### Allowed conversation fields (MVP)
- state: start | collecting_dates | collecting_room_type | ready_to_quote
- checkin (date|null), checkout (date|null)
- room_type (str|null), guest_count (int|null)
- created_at, updated_at, last_event_at (UTC)

### Campos proibidos no worker/task
O worker de processamento (`handle-message`) NÃO PODE receber nem persistir:
- message text/content
- phone number, sender_id raw, name
- remote_jid, wa_id, chat_id (identificadores enviáveis)
- webhook payload raw
- tokens/headers

### Idempotency strategy
- Webhook dedupe: processed_events(property_id, source="whatsapp", external_id=message_id)
- Worker/task dedupe: processed_events(property_id, source="tasks.whatsapp.handle_message", external_id=task_id)
- Rule: if receipt exists for given (property_id, source, external_id) => no-op
- Rationale: incluir property_id garante isolamento multi-tenant (evita colisão de message_id entre pousadas diferentes)

### Task contract (v1, PII-free)
Payload enviado ao worker `handle-message`:
```json
{
  "property_id": "string",
  "provider": "evolution",
  "message_id": "string",
  "contact_hash": "string (base64url sem padding, 32 chars)",
  "kind": "text | interactive | media | unknown",
  "received_at": "ISO8601 UTC",
  "correlation_id": "string"
}
```
Este contrato NÃO DEVE incluir: `to_ref`, `remote_jid`, `text`, `phone`, `payload`.

---

## Exceção controlada: contact_refs vault

### Motivação
Para enviar respostas outbound, o sistema precisa resolver `contact_hash` → `remote_jid`. Como o worker é PII-free, esta resolução DEVE ocorrer em componente isolado.

### Definição
O **contact_refs vault** é um cache criptografado que mapeia:
```
(property_id, channel, contact_hash) → remote_jid (criptografado)
```

### Requisitos obrigatórios
1. **Criptografia**: DEVE usar AES-256-GCM (ou equivalente) com chave simétrica via Secret Manager/env (`CONTACT_REFS_KEY`).
2. **TTL curto**: registros DEVEM expirar em no máximo **24 horas** (configurável, padrão 24h).
3. **Acesso restrito**: SOMENTE o componente `sender` (outbound) pode ler o vault; worker NÃO TEM acesso.
4. **Nunca logar**: remote_jid descriptografado NUNCA pode aparecer em logs.
5. **Limpeza**: registros expirados DEVEM ser removidos (TTL nativo ou job de limpeza).
6. **Escrita**: o endpoint público (inbound) grava no vault ao receber mensagem; worker não escreve.

### Fluxo outbound (resumo)
1. Worker grava `outbox_events` com `(property_id, contact_hash, response_template, correlation_id)` — sem PII.
2. Sender lê outbox, consulta vault: `contact_hash` → `remote_jid` (descriptografa).
3. Sender envia via provider usando `remote_jid`.
4. Se vault não contém entrada (TTL expirado), sender registra erro e NÃO envia.

---

## Consequences
- System requires CONTACT_HASH_SECRET em todos os ambientes (dev/prod) para gerar contact_hash estável.
- System requires CONTACT_REFS_KEY para criptografia do vault.
- contact_hash é irreversível sem secret; não é possível recuperar sender_id raw do DB.
- Limita debugging/logging; usar correlationId e metadados seguros apenas.
- Mensagens outbound falham se vault expirou (comportamento intencional: força re-interação do usuário).

## Alternatives considered
- SHA256(sender_id): rejeitado por risco de brute-force em telefones.
- Armazenar sender_id raw: rejeitado pela política de PII.
- Armazenar remote_jid no worker payload: rejeitado; worker deve ser PII-free.
- Vault sem criptografia: rejeitado; PII em repouso deve ser criptografada.
- TTL muito longo no vault (>24h): rejeitado; minimizar janela de exposição. TTL de 24h equilibra usabilidade e segurança.
