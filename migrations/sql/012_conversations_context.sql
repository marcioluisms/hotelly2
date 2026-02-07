-- Story 3: Add context JSONB to conversations
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS context JSONB NOT NULL DEFAULT '{}'::jsonb;
