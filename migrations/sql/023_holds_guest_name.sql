-- Sprint 1.8: Stripe payment link — add guest_name to holds for Worker metadata
-- The Worker reads guest_name from Stripe session metadata to close the business
-- loop (WhatsApp notification). We store it on the hold so the service can inject
-- it without fetching the conversation.

ALTER TABLE holds
  ADD COLUMN IF NOT EXISTS guest_name TEXT;
