import os
from datetime import date, datetime, timedelta
from datetime import timezone as tz

import psycopg2


def env(name: str, default: str | None = None) -> str:
    v = os.getenv(name, default)
    if v is None or v.strip() == "":
        raise RuntimeError(f"Missing env var: {name}")
    return v


def main() -> int:
    dsn = env("DATABASE_URL")  # DSN no formato key=value (psycopg2)
    external_subject = env("SEED_EXTERNAL_SUBJECT")
    property_id = env("SEED_PROPERTY_ID", "pousada-staging")
    property_name = env("SEED_PROPERTY_NAME", "Pousada Staging")
    timezone = env("SEED_PROPERTY_TIMEZONE", "America/Sao_Paulo")
    role = env("SEED_ROLE", "owner")

    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            # 1) Property (idempotente)
            cur.execute(
                """
                insert into properties (id, name, timezone, whatsapp_config)
                values (%s, %s, %s, '{}'::jsonb)
                on conflict (id) do nothing
                """,
                (property_id, property_name, timezone),
            )

            # 2) User + role (idempotente)
            cur.execute(
                """
                with u as (
                  insert into users (external_subject, email, name)
                  values (%s, null, null)
                  on conflict (external_subject) do update set updated_at = now()
                  returning id
                )
                insert into user_property_roles (user_id, property_id, role)
                select u.id, %s, %s
                from u
                on conflict do nothing
                """,
                (external_subject, property_id, role),
            )

            # 3) Room type (idempotente)
            room_type_id = "rt_standard"
            cur.execute(
                """
                INSERT INTO room_types (property_id, id, name)
                VALUES (%s, %s, 'Quarto Standard')
                ON CONFLICT (property_id, id) DO NOTHING
                """,
                (property_id, room_type_id),
            )

            # 4) Room (idempotente)
            cur.execute(
                """
                INSERT INTO rooms (property_id, id, room_type_id, name, is_active)
                VALUES (%s, '101', %s, 'Quarto 101', true)
                ON CONFLICT (property_id, id) DO NOTHING
                """,
                (property_id, room_type_id),
            )

            # 4b) Child age buckets (idempotente)
            cur.execute(
                """
                INSERT INTO property_child_age_buckets (property_id, bucket, min_age, max_age)
                VALUES
                    (%s, 1, 0, 3),
                    (%s, 2, 4, 12),
                    (%s, 3, 13, 17)
                ON CONFLICT (property_id, bucket) DO NOTHING
                """,
                (property_id, property_id, property_id),
            )

            # 5) Hold + Reservation (idempotente)
            checkin = date.today() + timedelta(days=7)
            checkout = checkin + timedelta(days=2)
            expires_at = datetime.now(tz.utc) + timedelta(hours=2)
            total_cents = 19900
            currency = "BRL"
            guest_count = 2
            create_idem_key = "seed-staging-demo-hold"

            cur.execute(
                """
                WITH h AS (
                  INSERT INTO holds (property_id, status, checkin, checkout, expires_at, create_idempotency_key, total_cents, currency, guest_count)
                  VALUES (%s, 'active', %s, %s, %s, %s, %s, %s, %s)
                  ON CONFLICT (property_id, create_idempotency_key)
                  WHERE create_idempotency_key IS NOT NULL
                  DO UPDATE SET
                    status = EXCLUDED.status,
                    checkin = EXCLUDED.checkin,
                    checkout = EXCLUDED.checkout,
                    expires_at = EXCLUDED.expires_at,
                    total_cents = EXCLUDED.total_cents,
                    currency = EXCLUDED.currency,
                    guest_count = EXCLUDED.guest_count,
                    updated_at = now()
                  RETURNING id
                ),
                r AS (
                  INSERT INTO reservations (property_id, hold_id, status, checkin, checkout, total_cents, currency, guest_count, room_type_id)
                  SELECT %s, h.id, 'confirmed', %s, %s, %s, %s, %s, %s
                  FROM h
                  ON CONFLICT (property_id, hold_id)
                  DO UPDATE SET
                    status = EXCLUDED.status,
                    checkin = EXCLUDED.checkin,
                    checkout = EXCLUDED.checkout,
                    total_cents = EXCLUDED.total_cents,
                    currency = EXCLUDED.currency,
                    guest_count = EXCLUDED.guest_count,
                    room_type_id = EXCLUDED.room_type_id,
                    updated_at = now()
                  RETURNING id
                )
                SELECT (SELECT id FROM h) AS hold_id, (SELECT id FROM r) AS reservation_id
                """,
                (
                    property_id, checkin, checkout, expires_at, create_idem_key, total_cents, currency, guest_count,
                    property_id, checkin, checkout, total_cents, currency, guest_count, room_type_id,
                ),
            )
            hold_id, reservation_id = cur.fetchone()

    print(
        "seed ok:",
        {
            "property_id": property_id,
            "external_subject": external_subject,
            "role": role,
            "hold_id": hold_id,
            "reservation_id": reservation_id,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
