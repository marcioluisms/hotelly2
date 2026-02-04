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

INSERT INTO rooms (property_id, id, room_type_id, name) VALUES
    ('prop-dev-001', '101', 'rt_standard', 'Quarto 101'),
    ('prop-dev-001', '102', 'rt_standard', 'Quarto 102'),
    ('prop-dev-001', '103', 'rt_standard', 'Quarto 103'),
    ('prop-dev-001', '104', 'rt_standard', 'Quarto 104'),
    ('prop-dev-001', '105', 'rt_standard', 'Quarto 105'),
    ('prop-dev-001', '201', 'rt_suite', 'Suíte 201'),
    ('prop-dev-001', '202', 'rt_suite', 'Suíte 202')
ON CONFLICT (property_id, id) DO NOTHING;
