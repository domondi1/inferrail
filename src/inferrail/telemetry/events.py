"""The structured record produced by every inference execution attempt.

This is deliberately operational metadata only — no prompt or response
content. See docs/adr/0003-no-payload-persistence-by-default.md for why.

Every field that we cannot currently measure trustworthily is left ``None``
rather than estimated or fabricated (e.g. cost, since we have no verified
pricing table yet; time-to-first-token, since v0.1 has no streaming path).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

ErrorCategory = Literal[
    "routing",
    "authentication",
    "rate_limit",
    "timeout",
    "invalid_request",
    "unsupported_feature",
    "provider",
    # The client disconnected/cancelled a stream — not a provider or
    # routing failure, so it doesn't fit any category above.
    "cancelled",
]


class InferenceEvent(BaseModel):
    request_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    route: str
    provider: str
    model: str

    # "partial": a stream was opened and at least one chunk was already
    # forwarded to the client before it failed or the client disconnected
    # — distinct from "error" (failed before any output reached the
    # client) so downstream analysis can tell "never worked" from "worked
    # partially then broke." See gateway/execution.py's streaming path.
    status: Literal["success", "error", "partial"]
    error_category: ErrorCategory | None = None
    error_message: str | None = None
    http_status: int | None = None

    total_latency_ms: float
    time_to_first_token_ms: float | None = None

    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    retry_count: int = 0
    estimated_cost_usd: float | None = None
