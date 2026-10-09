"""Card purchases of governed-run credits through Stripe Checkout.

Disabled unless `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` are both set. Credits are granted
only from a signature-verified `checkout.session.completed` event whose `payment_status` is
`paid`, at most once per Checkout session id. Returning to the success URL grants nothing.

Signature verification follows Stripe's published scheme: header `Stripe-Signature:
t=<ts>,v1=<hex>`, HMAC-SHA256 of `f"{t}.{raw_body}"` with the endpoint secret, and a timestamp
tolerance against replay. No Stripe SDK dependency.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

API = "https://api.stripe.com/v1/checkout/sessions"
TOLERANCE_SECONDS = 300


class SignatureError(ValueError):
    pass


@dataclass(frozen=True)
class StripeConfig:
    secret_key: str
    webhook_secret: str
    pack_price_cents: int
    pack_runs: int
    success_url: str
    cancel_url: str


def verify_event(
    raw_body: bytes, signature_header: str, secret: str, now: float | None = None
) -> dict[str, Any]:
    parts: dict[str, list[str]] = {}
    for item in signature_header.split(","):
        key, _, value = item.strip().partition("=")
        parts.setdefault(key, []).append(value)
    try:
        timestamp = int(parts["t"][0])
    except (KeyError, ValueError) as ex:
        raise SignatureError("missing timestamp") from ex
    now = time.time() if now is None else now
    if abs(now - timestamp) > TOLERANCE_SECONDS:
        raise SignatureError("timestamp outside tolerance")
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256
    ).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", [])):
        raise SignatureError("no matching v1 signature")
    return json.loads(raw_body)


def paid_session(event: dict[str, Any]) -> tuple[str, str, int, int] | None:
    """(session_id, workspace_id, packs, amount_total_cents) for a paid Checkout session, else
    None. `packs` comes from our own metadata, never from a client-supplied amount."""
    if event.get("type") != "checkout.session.completed":
        return None
    session = (event.get("data") or {}).get("object") or {}
    if session.get("payment_status") != "paid" or session.get("mode") != "payment":
        return None
    meta = session.get("metadata") or {}
    workspace_id, packs = meta.get("workspace_id"), meta.get("packs")
    if not workspace_id or not str(packs).isdigit():
        return None
    return session["id"], workspace_id, int(packs), int(session.get("amount_total") or 0)


async def create_session(
    cfg: StripeConfig, workspace_id: str, packs: int, client: httpx.AsyncClient
) -> str:
    """Create a one-time Checkout session and return its hosted payment URL."""
    form = {
        "mode": "payment",
        "success_url": cfg.success_url,
        "cancel_url": cfg.cancel_url,
        "client_reference_id": workspace_id,
        "metadata[workspace_id]": workspace_id,
        "metadata[packs]": str(packs),
        "line_items[0][quantity]": str(packs),
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": str(cfg.pack_price_cents),
        "line_items[0][price_data][product_data][name]": (
            f"Inferrail Hosted: {cfg.pack_runs:,} governed runs"
        ),
    }
    r = await client.post(API, data=form, auth=(cfg.secret_key, ""), timeout=30)
    r.raise_for_status()
    return r.json()["url"]
