"""Manual integration test: makes one real, billed call to OpenAI.

Skipped by default. Requires BOTH `OPENAI_API_KEY` (real credentials) *and*
`INFERRAIL_LIVE_TESTS=1` (a separate, explicit opt-in) to actually run — a
real key merely being present in the environment is not enough by itself.
This is deliberate: a bare `pytest` run must never bill a real OpenAI
account just because a key happens to be exported in the caller's shell.
Never run as part of the default automated test suite / CI — see
docs/ARCHITECTURE.md and tests/README expectations in docs/PRODUCT.md. Run
explicitly with:

    OPENAI_API_KEY=<your-openai-api-key> INFERRAIL_LIVE_TESTS=1 \\
        pytest tests/integration -m integration
"""

from __future__ import annotations

import os

import pytest

from inferrail.providers.base import ChatMessage, NormalizedChatRequest
from inferrail.providers.openai import OpenAIProvider

pytestmark = pytest.mark.integration

requires_real_credentials = pytest.mark.skipif(
    not (os.environ.get("OPENAI_API_KEY") and os.environ.get("INFERRAIL_LIVE_TESTS") == "1"),
    reason=(
        "requires OPENAI_API_KEY and INFERRAIL_LIVE_TESTS=1 (explicit opt-in — "
        "this test makes one real, billed call to OpenAI)"
    ),
)


@requires_real_credentials
async def test_openai_provider_completes_a_real_request() -> None:
    provider = OpenAIProvider(
        name="openai",
        api_key=os.environ["OPENAI_API_KEY"],
        base_url="https://api.openai.com/v1",
    )

    result = await provider.complete(
        NormalizedChatRequest(
            model="gpt-4o-mini",
            messages=[ChatMessage(role="user", content="Reply with exactly one word: pong")],
            max_tokens=5,
        ),
        timeout=30,
    )

    assert result.content.strip()
