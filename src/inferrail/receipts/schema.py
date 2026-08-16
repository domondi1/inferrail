"""The privacy-preserving economic record produced by every execution.

Deliberately a separate schema from `telemetry.events.InferenceEvent`, not
an extension of it — see
docs/adr/0005-privacy-preserving-economic-receipts.md for why. In short:
`InferenceEvent` answers "what happened, operationally" (for debugging);
`InferenceReceipt` answers "what did this cost, and to whom is that cost
attributed" (for the business). Keeping them separate means the existing
telemetry schema, its sinks, and every test that depends on them are
untouched by this feature.

Just like `InferenceEvent` (see docs/adr/0003), this schema has no field
that could ever hold prompt or response content — a structural guarantee,
covered by `test_inference_receipt_has_no_payload_fields`, not a runtime
policy that could be misconfigured on.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from inferrail.config.models import PriceEntry

# Re-exported under a receipt-scoped name: a `PricingSnapshot` is a `PriceEntry`
# copied verbatim onto a receipt at calculation time, so the receipt remains
# self-explanatory even if the catalog or an override later changes.
PricingSnapshot = PriceEntry


class InferenceReceipt(BaseModel):
    receipt_id: str
    request_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    route: str
    provider: str
    model: str

    status: Literal["success", "error"]

    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    # Both are None together, always: no price without a matching cost, and
    # never a cost without the price snapshot that produced it (see
    # inferrail.receipts.calculator).
    pricing: PricingSnapshot | None = None
    estimated_cost_usd: Decimal | None = None

    # Caller-supplied business context (see gateway.attribution). Generic by
    # design: customer, workflow, tenant, environment, project, or anything
    # else an operator wants to slice cost by. Persisted verbatim — callers
    # should not put secrets or unintended sensitive data in these values.
    attributes: dict[str, str] = Field(default_factory=dict)

    total_latency_ms: float
    retry_count: int = 0
