-- Story 1: Child age buckets + rename rate columns
CREATE TABLE IF NOT EXISTS property_child_age_buckets (
    property_id TEXT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    bucket SMALLINT NOT NULL CHECK (bucket BETWEEN 1 AND 3),
    min_age SMALLINT NOT NULL CHECK (min_age BETWEEN 0 AND 17),
    max_age SMALLINT NOT NULL CHECK (max_age BETWEEN 0 AND 17),
    PRIMARY KEY (property_id, bucket),
    CHECK (min_age <= max_age)
);

-- Rename child rate columns to bucket-based naming
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='room_type_rates' AND column_name='price_1chd_cents') THEN
        ALTER TABLE room_type_rates RENAME COLUMN price_1chd_cents TO price_bucket1_chd_cents;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='room_type_rates' AND column_name='price_2chd_cents') THEN
        ALTER TABLE room_type_rates RENAME COLUMN price_2chd_cents TO price_bucket2_chd_cents;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='room_type_rates' AND column_name='price_3chd_cents') THEN
        ALTER TABLE room_type_rates RENAME COLUMN price_3chd_cents TO price_bucket3_chd_cents;
    END IF;
END $$;
