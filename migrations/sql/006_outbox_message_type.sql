-- V2-S16.1: Add message_type column to outbox_events
-- Classifies outbound messages: atendimento | confirmacao | campanha

ALTER TABLE outbox_events
  ADD COLUMN IF NOT EXISTS message_type TEXT;

-- Add CHECK constraint for allowed values (NULL allowed for legacy rows)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'outbox_events_message_type_check'
  ) THEN
    ALTER TABLE outbox_events
      ADD CONSTRAINT outbox_events_message_type_check
      CHECK (message_type IS NULL OR message_type IN ('atendimento', 'confirmacao', 'campanha'));
  END IF;
END $$;
