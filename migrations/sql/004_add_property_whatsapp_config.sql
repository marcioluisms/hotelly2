-- S10: Add whatsapp_config column for flexible provider configuration
ALTER TABLE properties ADD COLUMN IF NOT EXISTS whatsapp_config JSONB DEFAULT '{}' NOT NULL;

-- Index for efficient phone_number_id lookups
CREATE INDEX IF NOT EXISTS ix_properties_whatsapp_config_meta_phone_number_id
ON properties ((whatsapp_config -> 'meta' ->> 'phone_number_id'))
WHERE whatsapp_config -> 'meta' ->> 'phone_number_id' IS NOT NULL;
