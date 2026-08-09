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
]


class InferenceEvent(BaseModel):
    request_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    route: str
    provider: str
    model: str

    status: Literal["success", "error"]
    error_category: ErrorCategory | None = None
    error_message: str | None = None
    http_status: int | None = None

    total_latency_ms: float
    time_to_first_token_ms: float | None = None

    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    retry_count: int = 0
    estimated_cost_usd: float | None = None
