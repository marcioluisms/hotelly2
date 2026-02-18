"""Generate a Stripe Checkout test link for an existing hold.

Usage:
    DATABASE_URL=... STRIPE_SECRET_KEY=sk_test_... uv run python scripts/generate_test_link.py <hold_id>

Requires:
    - DATABASE_URL pointing to a database with the hold
    - STRIPE_SECRET_KEY with a Stripe **test** key (sk_test_...)

This script is for local/staging E2E validation only.
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: uv run python scripts/generate_test_link.py <hold_id>")
        sys.exit(2)

    hold_id = sys.argv[1]

    # Guard: require env vars
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    stripe_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not stripe_key:
        print("ERROR: STRIPE_SECRET_KEY not set")
        sys.exit(1)

    if not stripe_key.startswith("sk_test_"):
        print(f"WARNING: STRIPE_SECRET_KEY does not start with sk_test_ — are you sure this is a test key?")
        answer = input("Continue? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            sys.exit(1)

    # Import after env validation so missing DB doesn't blow up on import
    from hotelly.stripe.client import StripeClient
    from hotelly.domain.payments import create_checkout_session, HoldNotFoundError, HoldNotActiveError

    client = StripeClient(api_key=stripe_key)

    print(f"Generating checkout link for hold_id={hold_id} ...")

    try:
        result = create_checkout_session(
            hold_id,
            stripe_client=client,
            correlation_id="script:generate_test_link",
        )
    except HoldNotFoundError:
        print(f"ERROR: Hold not found: {hold_id}")
        sys.exit(1)
    except HoldNotActiveError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    print()
    print("=== Checkout Session Created ===")
    print(f"  payment_id:         {result['payment_id']}")
    print(f"  provider_object_id: {result['provider_object_id']}")
    print(f"  checkout_url:       {result['checkout_url']}")
    print()
    print("Open the checkout_url in a browser to complete the test payment.")
    print("Use Stripe test card: 4242 4242 4242 4242, any future expiry, any CVC.")


if __name__ == "__main__":
    main()
