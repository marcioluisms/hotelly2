# ADR-006: Conversation identity and PII policy

## Status
Accepted

## Context
- Precisamos identificar conversas de WhatsApp por pousada (property) sem armazenar PII.
- O sistema terá múltiplas integrações e precisa de dedupe/idempotência em webhook e worker.
- Logs não podem conter payload/request/body/telefone/texto.

## Decision

### Conversation key (upsert)
- conversation_key = (property_id, channel="whatsapp", contact_hash)
- Constraint: UNIQUE(property_id, channel, contact_hash)

### contact_hash generation
- Use HMAC-SHA256 (não SHA simples) para reduzir risco de brute force em identificadores como telefone.
- contact_hash = base64url(HMAC_SHA256(CONTACT_HASH_SECRET, f"{property_id}|whatsapp|{sender_id}"))
- Store truncated for index efficiency (ex.: first 32 chars).
- CONTACT_HASH_SECRET é segredo (env/Secret Manager). Nunca versionar valores.

### Allowed conversation fields (MVP)
- state: start | collecting_dates | collecting_room_type | ready_to_quote
- checkin (date|null), checkout (date|null)
- room_type (str|null), guest_count (int|null)
- created_at, updated_at, last_event_at (UTC)

### Forbidden fields
- message text/content
- phone number, sender_id raw, name
- webhook payload raw
- any tokens/headers

### Idempotency strategy
- Webhook dedupe: processed_events(source="whatsapp", external_id=message_id)
- Worker/task dedupe: processed_events(source="tasks.whatsapp.handle_message", external_id=task_id)
- Rule: if receipt exists for given (source, external_id) => no-op

### Task contract (v1, no PII)
- property_id
- message_id
- contact_hash
- kind (no content)
- received_at (UTC)

## Consequences
- System requires CONTACT_HASH_SECRET in all environments (dev/prod) to generate stable contact_hash.
- contact_hash is irreversible without secret; cannot recover raw sender_id from DB.
- Limits debugging/logging; use correlationId and safe metadata only.

## Alternatives considered
- SHA256(sender_id): rejected due to brute-force risk for phone numbers.
- Storing sender_id raw: rejected due to PII policy.
- Using external conversation id: not available consistently across providers.
