"""Inferrail AP Invoice-Exception Recovery.

For one eligible invoice-extraction exception: decide whether it gets one
permitted machine retry or the vendor's established human-review path,
execute the retry through a supported integration, and record the
resulting cost and outcome.

This module makes no network calls itself and requires no Inferrail-
operated service to function -- it is a local decision engine your own
process runs, exactly like the OSS gateway core (`docs/adr/0004`).
Invoice contents and any provider credentials stay in your own process;
see `adapters.py`'s module docstring for the data boundary this
maintains.

Quick start::

    from inferrail.ap import (
        ExceptionCase, PolicyConfig, RecoveryEngine, RecoveryStore,
        FixtureRetryAdapter, FieldPresenceAndConfidenceValidator, LoggingHandoff,
    )

See `docs/capabilities/ap-invoice-exception-recovery.md` and
`examples/ap_invoice_exception_recovery/` for a full walkthrough.
"""

from __future__ import annotations

from .adapters import FixtureRetryAdapter, OpenAIRetryAdapter, RetryAdapter, get_cost_estimate
from .engine import DecisionResult, RecoveryEngine
from .handoff import HandoffSendFailed, HumanReviewHandoff, LoggingHandoff
from .models import (
    Action,
    AttemptStatus,
    CheckResult,
    CostEstimate,
    DecisionStatus,
    Eligibility,
    ExceptionCase,
    FailureType,
    Recommendation,
    RetryAttemptResult,
    ReviewOutcome,
    ReviewOutcomeRecord,
    ValidationResult,
)
from .policy import PolicyConfig, authorize_retry_cost, recommend
from .store import AmbiguousRetryError, RecoveryStore
from .validation import FieldPresenceAndConfidenceValidator, Validator
from .work_economics_export import export_work_economics_events

__all__ = [
    "Action",
    "AmbiguousRetryError",
    "AttemptStatus",
    "CheckResult",
    "CostEstimate",
    "DecisionResult",
    "DecisionStatus",
    "Eligibility",
    "ExceptionCase",
    "FailureType",
    "FieldPresenceAndConfidenceValidator",
    "FixtureRetryAdapter",
    "HandoffSendFailed",
    "HumanReviewHandoff",
    "LoggingHandoff",
    "OpenAIRetryAdapter",
    "PolicyConfig",
    "RecoveryEngine",
    "RecoveryStore",
    "Recommendation",
    "RetryAdapter",
    "RetryAttemptResult",
    "ReviewOutcome",
    "ReviewOutcomeRecord",
    "ValidationResult",
    "Validator",
    "authorize_retry_cost",
    "export_work_economics_events",
    "get_cost_estimate",
    "recommend",
]
