from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class WorkOutcomeRecord(BaseModel):
    """Customer-declared outcome evidence for a work_id.

    This is append-only evidence, not a lifecycle object. A work may have
    multiple outcome declarations over time; the last valid record appended
    for a given work is the current declaration.
    """

    work_id: str = Field(min_length=1)
    outcome_status: str = Field(min_length=1, max_length=128)
    recorded_at: datetime


class WorkSummary(BaseModel):
    """Derived read-side view over receipts + outcome declarations.

    All fields are computed from evidence, never stored as a primary record.
    A work missing receipts or outcome remains visible as an incomplete but
    truthful evidence record.
    """

    work_id: str
    outcome_status: str | None = None
    outcome_recorded_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    receipt_count: int = 0
    known_attributed_inference_cost_usd: Decimal | None = None
    unknown_cost_count: int = 0
    inference_status: Literal["success", "error", "partial", "unknown"] = "unknown"
