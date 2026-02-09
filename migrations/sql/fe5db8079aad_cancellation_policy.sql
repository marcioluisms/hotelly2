-- Cancellation policy per property (one row per property)
CREATE TABLE IF NOT EXISTS property_cancellation_policy (
    property_id                 TEXT        NOT NULL PRIMARY KEY
                                            REFERENCES properties(id) ON DELETE CASCADE,
    policy_type                 TEXT        NOT NULL
                                            CHECK (policy_type IN ('free', 'flexible', 'non_refundable')),
    free_until_days_before_checkin SMALLINT NOT NULL
                                            CHECK (free_until_days_before_checkin BETWEEN 0 AND 365),
    penalty_percent             SMALLINT    NOT NULL
                                            CHECK (penalty_percent BETWEEN 0 AND 100),
    notes                       TEXT,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_policy_rules CHECK (
        (policy_type = 'free'
            AND penalty_percent = 0)
        OR
        (policy_type = 'non_refundable'
            AND penalty_percent = 100
            AND free_until_days_before_checkin = 0)
        OR
        (policy_type = 'flexible'
            AND penalty_percent BETWEEN 1 AND 100
            AND free_until_days_before_checkin BETWEEN 0 AND 365)
    )
);
