"""Real vs simulated receipts.

A simulated receipt comes from a demo provider (`inferrail demo`, the hosted
Cost Gateway's zero-key demo): no provider was called, no money was spent,
and its price is a made-up number. It's still a real receipt of what the
pipeline did, but its "cost" is illustrative, so it must never be added to
real spend, real budgets or real cost per outcome.

Both signals are set by the operator's own configuration (the route's
provider name and the price table's `source`), never by a caller, so a real
call can't be labelled simulated to slip past a budget. A receipt is
simulated only when *both* agree: the provider is a demo provider, and its
price (if any) is marked as a demo price.
"""

from __future__ import annotations

from inferrail.receipts.schema import InferenceReceipt

DEMO_PROVIDER_NAMES = frozenset({"demo"})
DEMO_PRICE_SOURCE_PREFIX = "DEMO"


def is_simulated(receipt: InferenceReceipt) -> bool:
    """True when the receipt records a demo call, not real provider usage."""
    if receipt.provider not in DEMO_PROVIDER_NAMES:
        return False
    if receipt.pricing is None:
        return True
    return (receipt.pricing.source or "").startswith(DEMO_PRICE_SOURCE_PREFIX)
