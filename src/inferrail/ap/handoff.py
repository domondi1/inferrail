"""Human-review handoff: integrates with the vendor's own, already-
established human-review path. Inferrail does not implement a review UI
or queue -- `HumanReviewHandoff.send` is a customer-supplied callback the
engine calls exactly once when a case needs review; the vendor's own
system (however it already works) is expected to consume it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .models import Action, ExceptionCase, Recommendation


class HandoffSendFailed(RuntimeError):
    """Raised by `engine.RecoveryEngine._do_handoff` when the customer's
    `HumanReviewHandoff.send` callback itself raises -- distinguishable
    from other errors so a caller knows specifically that no handoff row
    was recorded (the receiving system never acknowledged anything) and
    can retry later via `RecoveryEngine.ensure_handoff`, which is safe to
    call repeatedly."""


class HumanReviewHandoff(Protocol):
    def send(
        self,
        case: ExceptionCase,
        recommendation: Recommendation,
        attempt_history: tuple[dict[str, Any], ...],
    ) -> str: ...
    """Returns a `handoff_ref` -- an opaque string the vendor's own
    review system uses to acknowledge/track the case. Inferrail does not
    interpret this value; it is stored for audit only."""


@dataclass
class LoggingHandoff:
    """Reference handoff for the demo and for tests: appends a
    structured JSON Lines record to `path` and returns a locally-
    generated reference. Not a real review queue -- a vendor integrating
    for real replaces this with their own system's API/queue client."""

    path: Path
    _counter: int = field(default=0, init=False)

    def send(
        self,
        case: ExceptionCase,
        recommendation: Recommendation,
        attempt_history: tuple[dict[str, Any], ...],
    ) -> str:
        self._counter += 1
        handoff_ref = f"logging_handoff:{case.work_id}:{self._counter}"
        record = {
            "handoff_ref": handoff_ref,
            "work_id": case.work_id,
            "failure_type": case.failure_type,
            "recommended_action": recommendation.action.value,
            "reason": recommendation.reason,
            "policy_version": recommendation.policy_version,
            "attempt_history": list(attempt_history),
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        return handoff_ref


def require_human_review(recommendation: Recommendation) -> bool:
    return recommendation.action in (Action.HUMAN_REVIEW, Action.INSUFFICIENT_EVIDENCE)
