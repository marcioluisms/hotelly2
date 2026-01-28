-- scripts/seed_dev.sql
-- Seed data for local development (idempotent)
-- Usage: psql $DATABASE_URL -f scripts/seed_dev.sql

INSERT INTO properties (id, name, timezone)
VALUES ('prop-dev-001', 'Hotel Teste Dev', 'America/Sao_Paulo')
ON CONFLICT (id) DO NOTHING;

INSERT INTO room_types (property_id, id, name) VALUES
    ('prop-dev-001', 'rt_standard', 'Quarto Standard'),
    ('prop-dev-001', 'rt_suite', 'Suíte Master')
ON CONFLICT (property_id, id) DO NOTHING;

INSERT INTO ari_days (property_id, room_type_id, date, inv_total, base_rate_cents, currency)
SELECT
    'prop-dev-001', rt.id, CURRENT_DATE + i,
    CASE WHEN rt.id = 'rt_standard' THEN 5 ELSE 2 END,
    CASE WHEN rt.id = 'rt_standard' THEN 25000 ELSE 45000 END,
    'BRL'
FROM generate_series(0, 59) AS i
CROSS JOIN (VALUES ('rt_standard'), ('rt_suite')) AS rt(id)
ON CONFLICT (property_id, room_type_id, date) DO NOTHING;
