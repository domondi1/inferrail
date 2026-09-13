"""Manual integration test: makes one real, billed call to OpenAI through
`OpenAIRetryAdapter`.

Skipped by default. Requires BOTH `OPENAI_API_KEY` (real credentials) *and*
`INFERRAIL_LIVE_TESTS=1` (a separate, explicit opt-in) to actually run --
see tests/integration/test_openai_live.py for why. Never run as part of
the default automated test suite / CI. Run explicitly with:

    OPENAI_API_KEY=<your-openai-api-key> INFERRAIL_LIVE_TESTS=1 \\
        pytest tests/integration/test_ap_openai_live.py -m integration
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from inferrail.ap.adapters import OpenAIRetryAdapter
from inferrail.ap.models import AttemptStatus, ExceptionCase, FailureType

pytestmark = pytest.mark.integration

requires_real_credentials = pytest.mark.skipif(
    not (os.environ.get("OPENAI_API_KEY") and os.environ.get("INFERRAIL_LIVE_TESTS") == "1"),
    reason=(
        "requires OPENAI_API_KEY and INFERRAIL_LIVE_TESTS=1 (explicit opt-in -- "
        "a real key merely being present is not enough by itself)"
    ),
)


@requires_real_credentials
def test_openai_retry_adapter_re_extracts_a_real_invoice_field_live() -> None:
    """Live-provider execution, clearly labeled: this makes one real,
    billed OpenAI call. The invoice text below is synthetic/local -- never
    a real customer's data -- but the call itself is real, not a fixture."""
    adapter = OpenAIRetryAdapter(
        invoice_text_by_work_id={
            "LIVE-DEMO-1": (
                "INVOICE #4471\nVendor: Acme Supplies\nTotal: $482.10\n"
                "Line items: Widgets x10 @ $48.21 = $482.10"
            )
        },
        required_fields=("invoice_number", "total"),
    )
    case = ExceptionCase(
        work_id="LIVE-DEMO-1",
        checkpoint_attempt_id="ATT-LIVE-1",
        failure_type=FailureType.LOW_CONFIDENCE.value,
        confidence=0.55,
    )
    result = adapter.retry(case)
    assert result.status in (AttemptStatus.SUCCESS, AttemptStatus.PARTIAL)
    assert result.provider == "openai"
    assert result.cost_usd is None or result.cost_usd >= Decimal("0")
