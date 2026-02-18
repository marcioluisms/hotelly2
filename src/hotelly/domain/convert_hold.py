from hotelly.infra.repositories.reservations_repository import insert_reservation
from hotelly.infra.repositories.outbox_repository import emit_event
from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context

logger = get_logger(__name__)

def convert_hold(cur, hold_id, property_id, payment_id=None):
    # Step 1: Lock hold for update
    cur.execute(
        """
        SELECT id, status, checkin, checkout, total_cents, currency, conversation_id, adult_count, children_ages, guest_name
        FROM holds
        WHERE id = %s AND property_id = %s
        FOR UPDATE
        """,
        (hold_id, property_id)
    )
    row = cur.fetchone()
    
    if not row:
        return {"status": "noop"}

    hold_uuid, status, checkin, checkout, total_cents, currency, conversation_id, adult_count, children_ages, guest_name = row

    if status != 'active':
        raise ValueError(f"hold is not active (status: {status})")

    # Step 2: Insert Reservation
    reservation_id, created = insert_reservation(
        cur,
        property_id=property_id,
        hold_id=hold_uuid,
        conversation_id=conversation_id,
        checkin=checkin,
        checkout=checkout,
        total_cents=total_cents,
        currency=currency,
        guest_name=guest_name,
        adult_count=adult_count,
        children_ages=children_ages
    )

    # Step 3: Mark hold as converted
    cur.execute(
        "UPDATE holds SET status = 'converted' WHERE id = %s",
        (hold_uuid,)
    )

    # Step 4: Emit Notification Event (Only if we have all data)
    if conversation_id:
        cur.execute("SELECT contact_hash FROM conversations WHERE id = %s", (conversation_id,))
        conv_row = cur.fetchone()

        cur.execute("SELECT name FROM properties WHERE id = %s", (property_id,))
        prop_row = cur.fetchone()

        # Security guard: skip notification if contact_hash is missing to avoid worker crashes.
        if not conv_row or not conv_row[0]:
            logger.warning(
                "skipping reservation notification: contact_hash missing",
                extra={
                    "extra_fields": safe_log_context(
                        conversation_id=str(conversation_id),
                        reservation_id=reservation_id,
                    )
                },
            )
        elif prop_row:
            contact_hash = conv_row[0]
            emit_event(
                cur,
                property_id=property_id,
                event_type="whatsapp.send_message",
                aggregate_type="reservation",
                aggregate_id=reservation_id,
                payload={
                    "contact_hash": contact_hash,
                    "template": "reservation_confirmed",
                    "params": {
                        "guest_name": guest_name,
                        "property_name": prop_row[0],
                        "checkin": checkin.strftime('%Y-%m-%d'),
                        "checkout": checkout.strftime('%Y-%m-%d'),
                    },
                },
            )

    return {"status": "converted", "reservation_id": reservation_id}
