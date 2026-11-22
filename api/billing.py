"""Credit packs + a thin Stripe gateway (TEST MODE ONLY). Keys come from the environment / gitignored .env."""
from __future__ import annotations

import json
import os
from typing import Optional

CREDITS_PER_JOB = 1
CURRENCY = "inr"
PACKS = {  # amounts in paise
    "starter": {"name": "Starter", "credits": 10, "amount": 49900},
    "team": {"name": "Team", "credits": 50, "amount": 199900},
    "fleet": {"name": "Fleet", "credits": 200, "amount": 699900},
}


class StripeNotConfigured(Exception):
    pass


class StripeGateway:
    """Wraps the stripe SDK so tests can swap it out. Refuses live keys outright."""

    def __init__(self, secret_key: Optional[str] = None, webhook_secret: Optional[str] = None):
        self.secret_key = secret_key if secret_key is not None else os.environ.get("STRIPE_SECRET_KEY", "")
        self.webhook_secret = webhook_secret if webhook_secret is not None else os.environ.get("STRIPE_WEBHOOK_SECRET", "")
        if self.secret_key and not (self.secret_key.startswith("sk_test_") or self.secret_key.startswith("rk_test_")):
            # Never run against a live account from this MVP.
            self.secret_key = ""
            self.live_key_rejected = True
        else:
            self.live_key_rejected = False

    @property
    def enabled(self) -> bool:
        return bool(self.secret_key)

    @property
    def webhook_enabled(self) -> bool:
        return bool(self.webhook_secret)

    def create_checkout(self, *, user_id: int, email: str, pack_id: str, base_url: str) -> dict:
        if not self.enabled:
            raise StripeNotConfigured()
        import stripe
        p = PACKS[pack_id]
        s = stripe.checkout.Session.create(
            api_key=self.secret_key,
            mode="payment",
            customer_email=email,
            client_reference_id=str(user_id),
            line_items=[{"quantity": 1, "price_data": {
                "currency": CURRENCY, "unit_amount": p["amount"],
                "product_data": {"name": f"SiliconScout {p['name']} - {p['credits']} sourcing credits"}}}],
            metadata={"user_id": str(user_id), "pack": pack_id, "credits": str(p["credits"])},
            success_url=f"{base_url}/?session_id={{CHECKOUT_SESSION_ID}}#/billing",
            cancel_url=f"{base_url}/?cancelled=1#/billing",
        )
        return {"id": s.id, "url": s.url}

    def retrieve(self, session_id: str) -> dict:
        if not self.enabled:
            raise StripeNotConfigured()
        import stripe
        s = stripe.checkout.Session.retrieve(session_id, api_key=self.secret_key)
        return json.loads(json.dumps(s.to_dict(), default=str))

    def verify_event(self, payload: bytes, sig_header: str) -> dict:
        """Checks the Stripe-Signature header; raises on a bad signature. Returns the raw event dict."""
        if not self.webhook_enabled:
            raise StripeNotConfigured()
        import stripe
        stripe.Webhook.construct_event(payload, sig_header, self.webhook_secret)  # raises SignatureVerificationError
        return json.loads(payload)


def grant_from_session(db, s: dict, via: str) -> tuple[bool, str]:
    """Credit the account for a paid Checkout Session. Returns (granted_now, message). Safe to call twice."""
    if s.get("payment_status") != "paid":
        return False, f"payment not completed (status: {s.get('payment_status')})"
    md = s.get("metadata") or {}
    pack_id = md.get("pack")
    if pack_id not in PACKS:
        return False, "unknown pack"
    pack = PACKS[pack_id]
    # Trust our own price table, and check Stripe charged what that pack costs.
    if int(s.get("amount_total") or 0) != pack["amount"] or (s.get("currency") or "").lower() != CURRENCY:
        return False, "amount/currency mismatch"
    uid = int(md.get("user_id") or s.get("client_reference_id") or 0)
    granted = db.grant_payment(s["id"], uid, pack_id, pack["credits"], pack["amount"], CURRENCY, via)
    return granted, "credited" if granted else "already credited"
