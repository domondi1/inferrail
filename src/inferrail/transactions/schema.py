"""A `TaskTransaction` groups the economic events belonging to one
caller-defined task into a single aggregate record — see
docs/adr/0008-task-transactions.md for why this exists as its own schema
rather than a bigger `InferenceReceipt`.

Like `InferenceEvent` and `InferenceReceipt`, this schema has no field
that could ever hold prompt/response/tool-argument content — a structural
guarantee, not a runtime policy. `EconomicEventRef` only ever references
an existing economic event's own id, cost, and status; it never inlines
the event itself, so it cannot become a second place a payload field is
ever added.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel


class EconomicEventRef(BaseModel):
    """A pointer to one child economic event that contributed to a
    transaction's cost.

    `event_type` is `Literal["inference"]` today because that's the only
    economic event type Inferrail produces (an `InferenceReceipt`). It's a
    `Literal`, not a plain `str`, so adding a second resource type later
    (search, browser, compute, ...) is a visible, deliberate schema change
    — not something that could silently start accepting arbitrary strings.
    """

    event_type: Literal["inference"]
    event_id: str
    cost_usd: Decimal | None
    status: str


class TaskTransaction(BaseModel):
    """One caller-defined task's aggregated economic activity.

    v0.1 of this primitive only ever aggregates `InferenceReceipt`s
    sharing one attribution-attribute value (see
    `transactions.builder.build_transaction`) — it does not change how
    receipts are produced, and it is not part of the gateway's request
    path. A `TaskTransaction` is a read-side view computed from existing
    receipts, not a new thing Inferrail writes at request time.
    """

    transaction_id: str
    task_id: str
    events: list[EconomicEventRef]

    # Sum of every event's known cost; Decimal("0") if none are known.
    # Never conflate "known total is $0" with "cost is unknown" — see
    # `unknown_cost_event_count` for that distinct case, mirroring
    # cli.report's ReportGroup.
    known_total_cost_usd: Decimal
    unknown_cost_event_count: int

    # success: every event succeeded. error: every event failed. partial:
    # anything mixed, or any event itself partial — see
    # transactions.builder.build_transaction for the exact rule.
    status: Literal["success", "partial", "error"]

    started_at: datetime
    ended_at: datetime
